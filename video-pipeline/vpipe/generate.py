"""分段生成：判断每段该做什么 → 记录提交意图 → 预算预占 → 提交 → 立即保存 task_id → 并发轮询 → 下载。"""

import json
import shutil
import subprocess
import time
from pathlib import Path

from . import api, config, state as st
from .util import PipelineError, crash_point, iso, log, now, sha256_file, sha256_text, warn

UPLOAD_TTL = 70 * 3600      # apimart 上传图片保存 72 小时，留 2 小时余量


# ---------------------------------------------------------------- 计划

def plan(cfg, S, opts):
    """返回 [(seg, action, detail)]。action ∈ done / poll / new / wait_chain / blocked。"""
    out = []
    completed_fp = {}
    for i, seg in enumerate(cfg["segments"]):
        sid = seg["id"]
        a = st.latest(S, sid)
        prev_id = cfg["segments"][i - 1]["id"] if i else None
        if seg["_mode"] == "chain":
            prev_done = st.completed_attempt(S, prev_id)
            prev_ok = prev_done and completed_fp.get(prev_id) == prev_done["attempt_id"]
            if not prev_ok:
                if a is None or a["status"] not in ("submitted", "unknown", "completed"):
                    out.append((seg, "wait_chain", f"等待 {prev_id} 完成后再提交（接尾帧模式）"))
                    continue
            chain_src = _chain_source(S, prev_id) if prev_ok else None
            inputs = config.segment_inputs(cfg, seg, chain_src) if chain_src else None
        else:
            inputs = config.segment_inputs(cfg, seg)
        fp = inputs["fingerprint"] if inputs else None

        if a is None:
            out.append((seg, "new", {"origin": "initial", "inputs": inputs}))
            continue
        s = a["status"]
        if s == "completed":
            if a.get("fingerprint") is None:
                out.append((seg, "done", "旧版记录（未保存输入指纹），按已完成处理，验收报告中标为待人工确认"))
                completed_fp[sid] = a["attempt_id"]
            elif fp is None or a["fingerprint"] == fp:
                out.append((seg, "done", "已完成，输入未变化"))
                completed_fp[sid] = a["attempt_id"]
            elif _allowed(opts.get("allow_regenerate"), sid):
                out.append((seg, "new", {"origin": "regenerate", "inputs": inputs}))
            else:
                diff = _diff(a.get("inputs"), inputs)
                out.append((seg, "blocked",
                            f"输入已变化（{diff}），旧结果不再算作完成。确认要重新生成（会产生费用）请加 --allow-regenerate {sid}"))
        elif s == "submitted":
            out.append((seg, "poll", f"继续查询任务 {a['task_id']}（不重新提交）"))
        elif s in ("unknown", "intent"):
            out.append((seg, "blocked",
                        f"attempt {a['attempt_id']} 状态不明：服务端可能已接单但本地没有 task_id。"
                        f"请到 apimart 控制台核对后执行 --resolve {sid} --attempt {a['attempt_id']} "
                        f"--task-id <任务号> 或 --not-created；核对前不会自动重投"))
        else:  # failed / cancelled / rejected / not_sent / not_created
            if sid in (opts.get("authorize_retry") or set()):
                out.append((seg, "new", {"origin": "retry", "inputs": inputs}))
            else:
                out.append((seg, "blocked",
                            f"上次 attempt {a['attempt_id']} 结果是 {s}（{_short_err(a)}）。"
                            f"不会自动返工；确认要重试（会产生费用）请加 --authorize-retry {sid}"))
    return out


def _allowed(spec, sid):
    return bool(spec) and ("all" in spec or sid in spec)


def _short_err(a):
    e = a.get("error")
    return (e if isinstance(e, str) else json.dumps(e, ensure_ascii=False))[:120] if e else "无错误详情"


def _diff(old, new):
    if not old or not new:
        return "无法比对"
    o, n = old.get("spec", {}), new.get("spec", {})
    keys = [k for k in sorted(set(o) | set(n)) if o.get(k) != n.get(k)]
    names = {"prompt_sha256": "提示词", "images": "首帧/参考图", "characters": "角色参考图", "duration": "时长",
             "model": "模型", "resolution": "分辨率", "size": "画幅", "seed": "种子", "mode": "模式",
             "generate_audio": "生成音频", "pipeline": "管线版本"}
    return "、".join(names.get(k, k) for k in keys) or "指纹不同"


def _chain_source(S, prev_id):
    a = st.completed_attempt(S, prev_id)
    if not a or not a.get("last_frame"):
        return None
    p = Path(a["last_frame"])
    return {"path": str(p), "sha256": sha256_file(p) if p.exists() else None, "attempt_id": a["attempt_id"]}


# ---------------------------------------------------------------- 提交

def _image_url(client, S, out_dir, info):
    key = info["sha256"]
    cached = S["uploads"].get(key)
    if cached and now() - cached["at"] < UPLOAD_TTL:
        return cached["url"]
    log(f"  上传图片 {Path(info['path']).name} …")
    url = client.upload_image(info["path"])
    S["uploads"][key] = {"url": url, "at": now(), "name": Path(info["path"]).name}
    st.save(out_dir, S)
    return url


def build_payload(cfg, seg, inputs, urls):
    payload = {
        "model": cfg["model"], "prompt": seg["_prompt"], "duration": seg["duration"],
        "size": cfg["size"], "resolution": cfg["resolution"],
        "generate_audio": bool(cfg["generate_audio"]), "return_last_frame": True,
    }
    if cfg.get("seed") is not None:
        payload["seed"] = cfg["seed"]
    if seg["_mode"] in ("first_frame", "chain"):
        payload["image_with_roles"] = [{"url": urls[0], "role": "first_frame"}]
    elif seg["_mode"] == "reference":
        payload["image_urls"] = urls
    return payload


def submit_new(cfg, seg, origin, inputs, S, out_dir, client, budget, ledger):
    sid = seg["id"]
    if seg.get("_submit_problem"):
        raise PipelineError(seg["_submit_problem"])
    if budget is None:
        raise PipelineError("没有配置预算（project.json 的 budget，或 batch 批次文件）。为防止超支，未配置预算时禁止提交。")
    if origin in ("retry", "regenerate"):
        used = st.repairs_used(S, sid)
        if used + 1 > budget["max_repairs_per_segment"]:
            raise PipelineError(f"{sid} 的修复次数已达上限（每段 {budget['max_repairs_per_segment']} 次），不再提交")
    price = config.price_for(budget, cfg, seg)

    urls = [_image_url(client, S, out_dir, {"path": im["path"], "sha256": im["sha256"]})
            for im in inputs["images"]]
    payload = build_payload(cfg, seg, inputs, urls)

    extra = _unreserved_inflight(cfg, S, budget)   # 在新建 attempt 之前算，避免把本次自己算进去
    att = st.add_attempt(S, sid, origin, inputs, None)
    att["price"] = price
    att["request_sha256"] = sha256_text(json.dumps({**payload, "image_urls": None, "image_with_roles": None},
                                                   ensure_ascii=False, sort_keys=True))
    key = f"{cfg['_dir'].name}/{sid}/{att['attempt_id']}"
    try:
        att["reservation"] = ledger.reserve(key, price["estimate"], cfg["_dir"].name, sid, att["attempt_id"], origin,
                                            extra_inflight=extra)
    except PipelineError:
        st.attempts(S, sid).pop()          # 没预占成功，这个 attempt 从未生效
        raise
    st.save(out_dir, S)                    # 先落盘"提交意图"，再发请求
    crash_point("after_intent_saved")
    log(f"  [{sid}] 提交生成（{origin}，预占 {price['estimate']:g} {price['currency']}）…")
    try:
        task_id = client.create_video(payload)
    except api.ApiRejected as e:
        st.set_status(att, "rejected", "服务端明确拒绝，未创建任务", error={"http": e.status, "body": e.body[:2000]})
        st.save(out_dir, S)
        ledger.release(key, "rejected")
        raise PipelineError(f"[{sid}] 提交被拒绝（HTTP {e.status}）：{e.body[:300]}")
    except api.ApiNotSent as e:
        st.set_status(att, "not_sent", "请求未发出", error=str(e))
        st.save(out_dir, S)
        ledger.release(key, "not_sent")
        raise PipelineError(f"[{sid}] 网络不通，请求没有发出：{e}")
    except api.ApiAmbiguous as e:
        st.set_status(att, "unknown", "服务端可能已接单，但没拿到 task_id", error=str(e))
        st.save(out_dir, S)
        warn(f"[{sid}] 状态不明：{e}。预占额度保留；请到 apimart 控制台核对，核对前不会重投")
        return att
    st.set_status(att, "submitted", None, task_id=task_id, submitted_at=iso())
    st.save(out_dir, S)                    # 拿到 task_id 立即落盘
    log(f"  [{sid}] 已提交，任务 ID：{task_id}（attempt {att['attempt_id']}）")
    return att


def _unreserved_inflight(cfg, S, budget):
    """旧版迁移来的在途任务（没有账本预占）也要算进"在途"，按当前价格估算。"""
    extra = 0.0
    for seg in cfg["segments"]:
        for a in st.attempts(S, seg["id"]):
            if a["status"] in st.BLOCKING and not a.get("reservation"):
                extra += config.price_for(budget, cfg, seg)["estimate"]
    return extra


# ---------------------------------------------------------------- 轮询与下载

def _finish(cfg, seg, att, data, S, out_dir, ledger):
    sid = seg["id"]
    result = data.get("result") or {}
    adir = out_dir / "attempts" / sid
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"{att['attempt_id']}_result.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    vurl = api.pick_video_url(result)
    if not vurl:
        raise PipelineError(f"[{sid}] 任务完成但返回里没找到视频地址，原始结果见 {adir / (att['attempt_id'] + '_result.json')}")
    video = adir / f"{att['attempt_id']}.mp4"
    api.download(vurl, video)
    last_png = adir / f"{att['attempt_id']}_last.png"
    lurl = api.pick_last_frame_url(result)
    if lurl:
        api.download(lurl, last_png)
    else:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-0.2", "-i", str(video),
                        "-frames:v", "1", "-update", "1", str(last_png)], check=True)
    current = out_dir / f"{sid}.mp4"
    shutil.copyfile(video, current.with_name(current.name + ".part"))
    current.with_name(current.name + ".part").replace(current)
    st.set_status(att, "completed", None, video=str(video), video_sha256=sha256_file(video),
                  last_frame=str(last_png), cost=data.get("cost"), credits_cost=data.get("credits_cost"),
                  finished_at=iso())
    st.save(out_dir, S)
    if att.get("reservation") and ledger:
        ledger.settle(att["reservation"]["key"], data.get("cost"))
    log(f"  [{sid}] 完成并已下载（服务端费用字段 {data.get('cost')}）")


def run(cfg, out_dir, S, client, budget, ledger, opts):
    """执行生成。返回 (全部完成?, 被阻塞的说明列表)。"""
    deadline = now() + opts.get("max_wait", 30 * 60)
    poll_every = opts.get("poll_interval", 10)
    shown = {}
    blocked_msgs = {}
    submit_stopped = None
    while True:
        steps = plan(cfg, S, opts)
        progressed = False
        for seg, action, detail in steps:
            sid = seg["id"]
            if action == "blocked":
                blocked_msgs[sid] = detail
            elif action == "new" and submit_stopped is None:
                try:
                    submit_new(cfg, seg, detail["origin"], detail["inputs"], S, out_dir, client, budget, ledger)
                    progressed = True
                except PipelineError as e:
                    # 一段提交失败（超预算/价格不明/被拒）：停止本轮所有新提交，但继续轮询已在跑的任务
                    submit_stopped = str(e)
                    log(f"\n[停止提交] {e}")
        for seg, action, detail in plan(cfg, S, opts):
            if action != "poll":
                continue
            sid = seg["id"]
            att = st.latest(S, sid)
            try:
                data = client.task(att["task_id"])
            except api.ApiRejected as e:
                blocked_msgs[sid] = f"查询任务 {att['task_id']} 被拒绝（HTTP {e.status}），请人工核对；本地记录保留"
                continue
            except (api.ApiAmbiguous, api.ApiNotSent) as e:
                warn(f"[{sid}] 查询暂时失败（{e}），稍后再试")
                continue
            status, progress = data.get("status"), data.get("progress")
            if shown.get(sid) != (status, progress):
                log(f"  [{sid}] 状态 {status}，进度 {progress}%")
                shown[sid] = (status, progress)
            if status == "completed":
                try:
                    _finish(cfg, seg, att, data, S, out_dir, ledger)
                    progressed = True
                except (OSError, PipelineError, subprocess.CalledProcessError) as e:
                    warn(f"[{sid}] 任务已完成但下载/保存失败（{e}），保持\"已提交\"状态，稍后再试；不会重新提交")
            elif status in ("failed", "cancelled"):
                st.set_status(att, status, "服务端返回失败", error=data.get("error"), raw=data, finished_at=iso())
                st.save(out_dir, S)
                if att.get("reservation") and ledger:
                    ledger.failed(att["reservation"]["key"], data.get("cost"))
                log(f"  [{sid}] 失败：{data.get('error')}（task {att['task_id']} 已保留，不会自动重投）")
                progressed = True
        final = plan(cfg, S, opts)
        pending = [s for s, a, _ in final if a in ("poll",) or (a == "new" and submit_stopped is None)
                   or (a == "wait_chain" and submit_stopped is None)]
        for seg, action, detail in final:
            if action == "blocked":
                blocked_msgs[seg["id"]] = detail
        if not pending:
            break
        if now() > deadline:
            log(f"\n等待超过 {opts.get('max_wait', 1800) // 60} 分钟，先退出。在途任务和预占额度都已保存，"
                f"稍后重跑同一命令会继续查询，不会重新提交。")
            break
        if not progressed:
            time.sleep(poll_every)
    all_done = all(a == "done" for _, a, _ in plan(cfg, S, opts))
    msgs = [f"{sid}：{m}" for sid, m in blocked_msgs.items()
            if any(s["id"] == sid and a == "blocked" for s, a, _ in plan(cfg, S, opts))]
    if submit_stopped:
        msgs.insert(0, f"本轮停止提交：{submit_stopped}")
    return all_done, msgs
