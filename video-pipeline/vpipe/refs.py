"""角色参考图生成（--make-refs）。走 apimart 图片接口，和视频一样受预算、提交意图、状态不明保护。

规则：
- 只处理 project.json 里写了 generate 的角色；生成结果保存到该角色 image 指定的工程内路径。
- 目标文件已存在就跳过（不论是生成的还是你自己放的），绝不覆盖；
  确实要重画用 --allow-regenerate <角色ID>，旧图会移到 refs/_old/ 保留，不删除。
- 失败或被拒后要 --authorize-retry <角色ID> 才重试；状态不明要 --resolve 人工核对。
- 生成后请人工看一眼再生成视频：视频会把这张图当作角色参考。
"""

import json
import shutil
import time
from pathlib import Path

from . import api, config, state as st
from .util import PipelineError, crash_point, fingerprint, iso, log, now, sha256_text, warn

UPLOAD_TTL = 70 * 3600
_regen_done = set()


def _rec(S, cid):
    return S.setdefault("refs", {}).setdefault(cid, {"attempts": []})


def _latest(S, cid):
    a = _rec(S, cid)["attempts"]
    return a[-1] if a else None


def _inputs(gen, ref_infos):
    spec = {"kind": "character_ref", "model": gen["model"], "size": gen["size"], "resolution": gen["resolution"],
            "prompt_sha256": sha256_text(gen["prompt"]), "extra": gen["extra"],
            "references": [i["sha256"] for i in ref_infos]}
    return {"fingerprint": fingerprint(spec), "spec": spec, "prompt": gen["prompt"],
            "images": [{"role": "reference", "path": i["path"], "sha256": i["sha256"]} for i in ref_infos]}


def plan(cfg, S, opts, only=None):
    out = []
    for cid, ch in cfg["_characters"].items():
        gen = ch.get("generate")
        if not gen or (only and cid not in only):
            continue
        a = _latest(S, cid)
        exists = gen["target"].exists()
        if a and a["status"] == "submitted":
            out.append((cid, "poll", f"继续查询任务 {a['task_id']}"))
        elif a and a["status"] in ("unknown", "intent"):
            out.append((cid, "blocked", f"角色图 attempt {a['attempt_id']} 状态不明，请到 apimart 核对后 "
                                        f"--resolve {cid} --attempt {a['attempt_id']} --task-id <任务号> 或 --not-created"))
        elif exists and (cid in _regen_done or (cid not in (opts.get("allow_regenerate") or set())
                                                and "all" not in (opts.get("allow_regenerate") or set()))):
            out.append((cid, "done", f"已有图片 {gen['target'].name}，不覆盖（要重画请加 --allow-regenerate {cid}）"))
        elif a and a["status"] in ("failed", "cancelled", "rejected", "not_sent", "not_created") and not exists:
            if cid in (opts.get("authorize_retry") or set()):
                out.append((cid, "new", "retry"))
            else:
                out.append((cid, "blocked", f"上次角色图生成结果是 {a['status']}，不会自动重试；确认要重试（会产生费用）请加 --authorize-retry {cid}"))
        else:
            out.append((cid, "new", "regenerate" if exists else "initial"))
    return out


def _submit(cfg, cid, origin, S, out_dir, client, budget, ledger):
    if budget is None:
        raise PipelineError("没有配置预算，禁止生成角色图")
    ch = cfg["_characters"][cid]
    gen = ch["generate"]
    if origin in ("retry", "regenerate"):
        used = sum(1 for a in _rec(S, cid)["attempts"]
                   if a.get("origin") in ("retry", "regenerate") and a["status"] not in st.CONFIRMED_NOT_CREATED)
        if used + 1 > budget["max_repairs_per_segment"]:
            raise PipelineError(f"角色 {cid} 的修复次数已达上限（{budget['max_repairs_per_segment']} 次）")
    price = config.price_for_image(budget, gen["model"])
    ref_infos = [config.check_image(p, f"角色 {cid} 的生成参考图") for p in gen["references"]]
    inputs = _inputs(gen, ref_infos)
    urls = []
    for info in ref_infos:
        c = S["uploads"].get(info["sha256"])
        if c and now() - c["at"] < UPLOAD_TTL:
            urls.append(c["url"])
        else:
            url = client.upload_image(info["path"])
            S["uploads"][info["sha256"]] = {"url": url, "at": now(), "name": Path(info["path"]).name}
            urls.append(url)
    payload = {"model": gen["model"], "prompt": gen["prompt"], "size": gen["size"], "n": 1, **gen["extra"]}
    if gen["resolution"]:
        payload["resolution"] = gen["resolution"]
    if urls:
        payload["image_urls"] = urls

    att = {"attempt_id": f"ref_{cid}_{time.strftime('%Y%m%d%H%M%S')}_{sha256_text(str(now()))[:6]}",
           "created_at": iso(), "origin": origin, "status": "intent", "fingerprint": inputs["fingerprint"],
           "inputs": inputs, "price": price, "reservation": None, "task_id": None,
           "request_sha256": sha256_text(json.dumps({**payload, "image_urls": None}, ensure_ascii=False, sort_keys=True)),
           "history": [{"at": iso(), "status": "intent", "note": "已记录提交意图，尚未发请求"}]}
    key = f"{cfg['_dir'].name}/ref:{cid}/{att['attempt_id']}"
    if origin == "regenerate":
        _regen_done.add(cid)          # 同一次运行里每个角色最多重画一次
    att["reservation"] = ledger.reserve(key, price["estimate"], cfg["_dir"].name, f"ref:{cid}", att["attempt_id"], origin)
    _rec(S, cid)["attempts"].append(att)
    st.save(out_dir, S)
    crash_point("after_intent_saved")
    log(f"  [角色图 {cid}] 提交生成（{gen['model']}，预占 {price['estimate']:g} {price['currency']}）…")
    try:
        task_id = client.create_image(payload)
    except api.ApiRejected as e:
        st.set_status(att, "rejected", "服务端明确拒绝", error={"http": e.status, "body": e.body[:2000]})
        st.save(out_dir, S)
        ledger.release(key, "rejected")
        raise PipelineError(f"[角色图 {cid}] 提交被拒绝（HTTP {e.status}）：{e.body[:300]}")
    except api.ApiNotSent as e:
        st.set_status(att, "not_sent", "请求未发出", error=str(e))
        st.save(out_dir, S)
        ledger.release(key, "not_sent")
        raise PipelineError(f"[角色图 {cid}] 网络不通，请求没有发出：{e}")
    except api.ApiAmbiguous as e:
        st.set_status(att, "unknown", "服务端可能已接单，但没拿到 task_id", error=str(e))
        st.save(out_dir, S)
        warn(f"[角色图 {cid}] 状态不明：{e}。预占保留；请到 apimart 核对，核对前不会重投")
        return
    st.set_status(att, "submitted", None, task_id=task_id, submitted_at=iso())
    st.save(out_dir, S)
    log(f"  [角色图 {cid}] 已提交，任务 ID：{task_id}")


def _finish(cfg, cid, att, data, S, out_dir, ledger):
    gen = cfg["_characters"][cid]["generate"]
    adir = out_dir / "attempts" / "refs" / cid
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"{att['attempt_id']}_result.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    url = api.pick_image_url(data.get("result") or {})
    if not url:
        raise PipelineError(f"[角色图 {cid}] 任务完成但没找到图片地址，原始结果见 {adir}")
    raw = adir / f"{att['attempt_id']}{gen['target'].suffix}"
    api.download(url, raw)
    info = config.check_image(raw, f"角色 {cid} 生成结果")
    if gen["target"].exists():
        if att.get("origin") != "regenerate":
            raise PipelineError(f"[角色图 {cid}] 目标位置已经有图片 {gen['target']}，不覆盖；新图保存在 {raw}")
        old = gen["target"].parent / "_old"     # 授权重画：新图到手后才把旧图移走（不删除）
        old.mkdir(exist_ok=True)
        dest = old / f"{gen['target'].stem}.{time.strftime('%Y%m%d%H%M%S')}{gen['target'].suffix}"
        shutil.move(str(gen["target"]), dest)
        log(f"  旧角色图已移到 {dest}（未删除）")
    gen["target"].parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(raw, gen["target"])
    st.set_status(att, "completed", None, image=str(gen["target"]), archive=str(raw), image_sha256=info["sha256"],
                  cost=data.get("cost"), credits_cost=data.get("credits_cost"), finished_at=iso())
    st.save(out_dir, S)
    if att.get("reservation"):
        ledger.settle(att["reservation"]["key"], data.get("cost"))
    log(f"  [角色图 {cid}] 完成：{gen['target']}（{info['width']}x{info['height']}）——请人工确认长相后再生成视频")


def run(cfg, out_dir, S, client, budget, ledger, opts, only=None):
    deadline = now() + opts.get("max_wait", 15 * 60)
    msgs, stopped = [], None
    S.setdefault("refs", {})
    while True:
        steps = plan(cfg, S, opts, only)
        for cid, action, detail in steps:
            if action == "new" and stopped is None:
                try:
                    _submit(cfg, cid, detail, S, out_dir, client, budget, ledger)
                except PipelineError as e:
                    stopped = str(e)
                    log(f"\n[停止提交] {e}")
        for cid, action, detail in plan(cfg, S, opts, only):
            if action != "poll":
                continue
            att = _latest(S, cid)
            try:
                data = client.task(att["task_id"])
            except api.ApiRejected as e:
                msgs.append(f"角色图 {cid}：查询任务被拒绝（HTTP {e.status}），请人工核对")
                continue
            except (api.ApiAmbiguous, api.ApiNotSent) as e:
                warn(f"[角色图 {cid}] 查询暂时失败（{e}），稍后再试")
                continue
            s = data.get("status")
            if s == "completed":
                try:
                    _finish(cfg, cid, att, data, S, out_dir, ledger)
                except (OSError, PipelineError) as e:
                    msgs.append(f"角色图 {cid}：任务已完成但保存失败（{e}），保持已提交状态，重跑会再试，不会重新提交")
                    stopped = stopped or "角色图保存失败"
            elif s in ("failed", "cancelled"):
                st.set_status(att, s, "服务端返回失败", error=data.get("error"), raw=data, finished_at=iso())
                st.save(out_dir, S)
                if att.get("reservation"):
                    ledger.failed(att["reservation"]["key"], data.get("cost"))
                log(f"  [角色图 {cid}] 失败：{data.get('error')}（不会自动重试）")
        final = plan(cfg, S, opts, only)
        pending = [c for c, a, _ in final if a == "poll" or (a == "new" and stopped is None)]
        if not pending or now() > deadline:
            break
        time.sleep(opts.get("poll_interval", 10))
    final = plan(cfg, S, opts, only)
    msgs += [f"角色图 {c}：{d}" for c, a, d in final if a == "blocked"]
    if stopped:
        msgs.insert(0, f"本轮停止提交：{stopped}")
    done = all(a == "done" for _, a, _ in final)
    return done, msgs


def resolve(S, cid, attempt_id, task_id, not_created, out_dir, ledger):
    att = next((a for a in _rec(S, cid)["attempts"] if a["attempt_id"] == attempt_id), None)
    if att is None or att["status"] != "unknown":
        raise PipelineError(f"角色图 {cid} 没有状态不明的 attempt {attempt_id}")
    if task_id:
        st.set_status(att, "submitted", "人工核对：服务端已有任务", task_id=task_id, resolved_manually=True)
    else:
        st.set_status(att, "not_created", "人工核对：服务端没有这个任务", resolved_manually=True)
        if ledger and att.get("reservation"):
            ledger.release(att["reservation"]["key"], "manual_not_created")
    st.save(out_dir, S)
