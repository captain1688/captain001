#!/usr/bin/env python3
"""用 apimart 的 Seedance 2.0 分段并发生成视频，再用 ffmpeg 合成旁白、互动符号和配乐，最后逐段验收。

常用命令（在仓库根目录运行）：
    python video-pipeline/make_video.py <工程目录> --dry-run      预检：校验图片/价格/预算/时间轴，打印计划，不联网不花钱
    python video-pipeline/make_video.py <工程目录>                生成 → 旁白 → 剪辑 → 验收
    python video-pipeline/make_video.py <工程目录> --status       查看每段的尝试记录和预算账本
    python video-pipeline/make_video.py <工程目录> --tts-only     只合成旁白（edge-tts 免费在线语音）
    python video-pipeline/make_video.py <工程目录> --edit-only    只用已有素材重剪 + 验收（不调用任何生成接口）
    python video-pipeline/make_video.py <工程目录> --qc-only      只重新验收
需要：环境变量 APIMART_API_KEY（只在正式生成时读取）；ffmpeg / ffprobe 在 PATH 中。
旁白用 edge 提供方时需要 python -m pip install edge-tts。其余只用 Python 标准库。
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vpipe import PIPELINE_VERSION, budget as budget_mod, config, edit, generate, narration, qc, refs, state as st  # noqa: E402
from vpipe.util import FileLock, PipelineError, log, warn  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_PENDING, EXIT_QC_FAIL = 0, 1, 2, 3


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Seedance 2.0（apimart）分段生成 + 旁白/符号合成 + 验收")
    ap.add_argument("project", help="工程目录，例如 video-pipeline/projects/xueren")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只检查并打印计划，不联网、不花钱")
    mode.add_argument("--status", action="store_true", help="查看尝试记录和预算账本")
    mode.add_argument("--tts-only", action="store_true", help="只合成旁白并检查词级时间戳")
    mode.add_argument("--edit-only", action="store_true", help="只用已有素材重剪并验收，不生成")
    mode.add_argument("--qc-only", action="store_true", help="只重新验收")
    mode.add_argument("--resolve", metavar="SEG", help="人工核对后处理状态不明的 attempt（分段 ID 或角色 ID）")
    mode.add_argument("--make-refs", nargs="*", metavar="CHAR",
                      help="生成 project.json 里带 generate 配置、还没有图的角色参考图（会花钱）；不写角色 ID 表示全部")
    ap.add_argument("--attempt", help="配合 --resolve：要处理的 attempt ID")
    res = ap.add_mutually_exclusive_group()
    res.add_argument("--task-id", help="配合 --resolve：在 apimart 控制台查到的任务号")
    res.add_argument("--not-created", action="store_true", help="配合 --resolve：确认服务端没有创建这个任务")
    ap.add_argument("--authorize-retry", action="append", default=[], metavar="SEG",
                    help="授权对失败/被拒的分段新建一次 attempt（会产生费用，受预算和修复次数限制）")
    ap.add_argument("--allow-regenerate", action="append", default=[], metavar="SEG",
                    help="输入（提示词/参考图/参数）变了时，授权重新生成该段；写 all 表示全部")
    ap.add_argument("--allow-unaligned", action="store_true", help="时间轴有无法对齐的符号时，跳过它们先出一版")
    ap.add_argument("--skip-edit", action="store_true", help="只生成，不剪辑不验收")
    ap.add_argument("--max-wait", type=int, default=30 * 60, help="本次最长等待秒数（默认 1800）")
    ap.add_argument("--poll-interval", type=int, default=10, help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    try:
        return _main(a)
    except PipelineError as e:
        print(f"\n[停止] {e}", file=sys.stderr, flush=True)
        return e.code if e.code != 1 else EXIT_ERROR
    except KeyboardInterrupt:
        print("\n[中断] 已保存的状态和预占都保留；重跑同一命令会继续，不会重复提交。", file=sys.stderr)
        return EXIT_ERROR


def _main(a):
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise PipelineError(f"找不到 {tool}，请先安装 ffmpeg 并加入 PATH")
    project_dir = Path(a.project).resolve()
    out_dir = project_dir / "output"
    out_dir.mkdir(exist_ok=True)
    needs_images = not (a.status or a.edit_only or a.qc_only or a.resolve or a.tts_only)
    cfg = config.load_project(project_dir, check_images=needs_images,
                              allow_missing_generated=a.dry_run or a.make_refs is not None)
    budget = config.load_budget(project_dir, cfg, out_dir)
    ledger = budget_mod.Ledger(budget) if budget else None
    log(f"工程：{project_dir.name}｜{len(cfg['segments'])} 段，共 {sum(s['duration'] for s in cfg['segments'])} 秒｜"
        f"{cfg['model']} {cfg['resolution']} {cfg['size']}｜配置版本 {cfg['_config_version']}｜管线 {PIPELINE_VERSION}")

    if a.dry_run:
        return dry_run(cfg, out_dir, budget, ledger, a)
    if a.status:
        return show_status(cfg, out_dir, ledger)

    lock = FileLock(out_dir / ".run.lock", "工程运行")
    if not lock.acquire(timeout=0):
        raise PipelineError(f"另一个进程正在处理这个工程（{lock.holder()}）。同一工程不能同时运行两次，"
                            f"请等它结束；确认那个进程已经不存在时，锁会被系统自动释放。")
    try:
        S = st.load(out_dir, project_dir.name)
        for sid, aid in st.recover_interrupted(S):
            warn(f"{sid} 的 attempt {aid} 上次在提交过程中中断，已标为\"状态不明\"，等待核对，不会自动重投")
        st.save(out_dir, S)

        if a.resolve:
            if a.resolve in cfg["_characters"] and a.resolve not in {s["id"] for s in cfg["segments"]}:
                if not a.attempt or not (a.task_id or a.not_created):
                    raise PipelineError("--resolve 需要同时给 --attempt 和（--task-id 或 --not-created）")
                refs.resolve(S, a.resolve, a.attempt, a.task_id, a.not_created, out_dir, ledger)
                log(f"已记录角色图 {a.resolve} 的核对结果。")
                return EXIT_OK
            return resolve(cfg, out_dir, S, ledger, a)
        if a.make_refs is not None:
            from vpipe.api import Client
            only = set(a.make_refs) or None
            unknown = (only or set()) - {c for c, ch in cfg["_characters"].items() if ch.get("generate")}
            if unknown:
                raise PipelineError(f"这些角色没有 generate 配置，不能自动生成：{sorted(unknown)}")
            client = Client(os.environ.get("APIMART_API_KEY", "").strip())
            opts = {"authorize_retry": set(a.authorize_retry), "allow_regenerate": set(a.allow_regenerate),
                    "max_wait": a.max_wait, "poll_interval": a.poll_interval}
            done, msgs = refs.run(cfg, out_dir, S, client, budget, ledger, opts, only)
            for m in msgs:
                log(f"  ! {m}")
            if done:
                log("\n角色图都已就绪。请先打开 refs/ 人工确认长相，再预检和生成视频。")
            return EXIT_OK if done else EXIT_PENDING
        if a.tts_only:
            narr = narration.synthesize(cfg, allow_tts=True)
            _report_tts(cfg, narr)
            return EXIT_OK
        if a.qc_only:
            rep = qc.run(cfg, S, out_dir)
            return EXIT_QC_FAIL if rep["status"] == "fail" else EXIT_OK
        if a.edit_only:
            edit.compose(cfg, S, out_dir, allow_tts=False, allow_unaligned=a.allow_unaligned)
            rep = qc.run(cfg, S, out_dir)
            return EXIT_QC_FAIL if rep["status"] == "fail" else EXIT_OK

        # 正式生成
        from vpipe.api import Client
        client = Client(os.environ.get("APIMART_API_KEY", "").strip())
        opts = {"authorize_retry": set(a.authorize_retry), "allow_regenerate": set(a.allow_regenerate),
                "max_wait": a.max_wait, "poll_interval": a.poll_interval}
        done, msgs = generate.run(cfg, out_dir, S, client, budget, ledger, opts)
        for m in msgs:
            log(f"  ! {m}")
        if ledger:
            snap = ledger.snapshot()
            log(f"  预算：已花 {snap['spent']:g} + 在途预占 {snap['reserved']:g} / 上限 {snap['limit']:g} {snap['currency']}")
        if not done:
            log("\n还有分段没完成或需要处理（见上方说明），本次不剪辑。")
            return EXIT_PENDING
        if a.skip_edit:
            return EXIT_OK
        edit.compose(cfg, S, out_dir, allow_tts=True, allow_unaligned=a.allow_unaligned)
        rep = qc.run(cfg, S, out_dir)
        return EXIT_QC_FAIL if rep["status"] == "fail" else EXIT_OK
    finally:
        lock.release()


# ---------------------------------------------------------------- 预检

def dry_run(cfg, out_dir, budget, ledger, a):
    """不联网、不写状态。逐项检查，把所有问题列出来。"""
    problems = []
    S = st.load(out_dir, cfg["_dir"].name) if (out_dir / "state.json").exists() else \
        {"schema": st.SCHEMA, "uploads": {}, "segments": {}, "events": []}
    S = {**S, "segments": {k: {"attempts": [dict(x) for x in v["attempts"]]} for k, v in S["segments"].items()}}
    for sid, rec in S["segments"].items():
        for att in rec["attempts"]:
            if att["status"] == "intent":
                att["status"] = "unknown"   # 只在内存里模拟恢复，不落盘
    log("\n角色参考：")
    for cid, c in cfg["_characters"].items():
        if c.get("missing"):
            log(f"  {cid}（{c['name']}）→ {c['path']}  （还没生成）")
        else:
            log(f"  {cid}（{c['name']}）→ {c['path']}  {c['width']}x{c['height']}  sha256 {c['sha256'][:12]}…")
    if not cfg["_characters"]:
        log("  （未声明角色）")
    gen_chars = {cid: ch for cid, ch in cfg["_characters"].items() if ch.get("generate")}
    if gen_chars:
        log("\n角色参考图生成（--make-refs）：")
        S.setdefault("refs", {})
        for cid, action, detail in refs.plan(cfg, S, {"authorize_retry": set(a.authorize_retry),
                                                       "allow_regenerate": set(a.allow_regenerate)}):
            gen = gen_chars[cid]["generate"]
            line = f"  {cid}（{gen_chars[cid]['name']}）→ {gen['target'].name}｜{gen['model']} {gen['size']}"
            if action == "new":
                line += "\n      → 需要生成"
                problems.append(f"角色 {cid} 的参考图还没生成：先运行 --make-refs {cid}（会花钱），人工确认长相后再生成视频")
                if budget:
                    try:
                        p = config.price_for_image(budget, gen["model"])
                        line += f"，预估 {p['estimate']:g} {p['currency']}"
                    except PipelineError as e:
                        problems.append(str(e))
            else:
                line += f"\n      → {action}：{detail}"
                if action == "blocked":
                    problems.append(f"角色 {cid}：{detail}")
            log(line)
    log("\n分段计划：")
    opts = {"authorize_retry": set(a.authorize_retry), "allow_regenerate": set(a.allow_regenerate)}
    new_cost = 0.0
    for seg, action, detail in generate.plan(cfg, S, opts):
        imgs = "、".join(f"{im['label']}={Path(im['info']['path']).name}" for im in seg["_images"]) or \
            ("上一段尾帧" if seg["_mode"] == "chain" else "无")
        line = f"  {seg['id']}：模式 {seg['_mode']}｜角色 {seg['_characters'] or '无'}｜图片 {imgs}｜提示词 {len(seg['_prompt'])} 字"
        if action == "new" and seg.get("_submit_problem"):
            problems.append(seg["_submit_problem"])
        if action == "new":
            if budget:
                try:
                    p = config.price_for(budget, cfg, seg)
                    new_cost += p["estimate"]
                    line += f"\n      → 新建 attempt（{detail['origin']}），预估 {p['estimate']:g} {p['currency']}"
                except PipelineError as e:
                    problems.append(str(e))
                    line += "\n      → 新建 attempt，但价格不明（见下方问题）"
            else:
                line += "\n      → 新建 attempt（未配置预算，正式运行会被拒绝）"
        else:
            line += f"\n      → {action}：{detail}"
            if action == "blocked":
                problems.append(f"{seg['id']}：{detail}")
        if len(seg["_prompt"]) > 500 and cfg["model"].endswith("mini"):
            line += f"\n      ! 提示词 {len(seg['_prompt'])} 字，官方建议 mini 模型中文 500 字以内"
        log(line)
    if budget is None:
        problems.append("没有配置预算：正式运行会拒绝提交（在 project.json 写 budget+pricing，或用 batch 批次文件）")
    else:
        snap = ledger.snapshot() if ledger.path.exists() else {"spent": 0.0, "reserved": 0.0}
        total = snap["spent"] + snap["reserved"] + new_cost
        log(f"\n预算（批次 {budget['batch_id']}）：已花 {snap['spent']:g} + 在途预占 {snap['reserved']:g} + "
            f"本次新任务 {new_cost:g} = {total:g} / 上限 {budget['limit']:g} {budget['currency']}；"
            f"每段修复上限 {budget['max_repairs_per_segment']} 次")
        if total > budget["limit"] + 1e-9:
            problems.append(f"超出预算：{total:g} > {budget['limit']:g} {budget['currency']}，正式运行会在超限前停止提交")
    if cfg.get("_narration") or cfg.get("_overlay"):
        log("\n旁白与符号：")
        for ln in (cfg.get("_narration") or {}).get("lines", []):
            log(f"  旁白 {ln['id']}（{ln['segment']} +{ln['start']}s）：{ln['text']}")
        for ev in (cfg.get("_overlay") or {}).get("events", []):
            log(f"  符号 {ev['id']}：{ev['symbol']} @ {ev['segment']} / {ev.get('narration')} / {ev['anchor']}")
        try:
            narr = narration.synthesize(cfg, allow_tts=False)
            offsets, durs, t = {}, {}, 0.0
            for seg in cfg["segments"]:
                offsets[seg["id"]], durs[seg["id"]] = t, float(seg["duration"])
                t += seg["duration"]
            _, events, probs = narration.resolve_timeline(cfg, narr, offsets, durs)
            for ev in events:
                log(f"    {ev['id']} {ev['symbol']}：" + (f"{ev['start']:.2f}–{ev['end']:.2f}s（{ev['alignment']}，{ev['note']}）"
                                                       if "start" in ev else f"无法定位（{ev['note']}）"))
            problems += [m for lvl, m in probs if lvl == "error"]
        except PipelineError as e:
            log(f"  （旁白还没合成，暂不能计算精确时间；正式运行或 --tts-only 时合成：{e}）")
    log("")
    if problems:
        log("预检发现问题：")
        for p in problems:
            log(f"  ✗ {p}")
        log("\n[dry-run] 没有调用任何付费接口。")
        return EXIT_PENDING
    log("[dry-run] 检查通过，没有调用任何付费接口。")
    return EXIT_OK


def _report_tts(cfg, narr):
    offsets, durs, t = {}, {}, 0.0
    for seg in cfg["segments"]:
        offsets[seg["id"]], durs[seg["id"]] = t, float(seg["duration"])
        t += seg["duration"]
    for lid, meta in narr.items():
        log(f"  旁白 {lid}：{meta['duration']:.2f}s，{meta['alignment']}（{meta['alignment_note']}），{len(meta['words'])} 个词")
    _, events, probs = narration.resolve_timeline(cfg, narr, offsets, durs)
    for ev in events:
        log(f"  符号 {ev['id']}：" + (f"{ev['start']:.2f}–{ev['end']:.2f}s（{ev['alignment']}，{ev['note']}）"
                                    if "start" in ev else f"无法定位（{ev['note']}）"))
    for lvl, m in probs:
        if lvl == "warn":
            warn(m)
        else:
            log(f"  ✗ {m}")


# ---------------------------------------------------------------- 状态与人工核对

def show_status(cfg, out_dir, ledger):
    S = st.load(out_dir, cfg["_dir"].name) if (out_dir / "state.json").exists() else None
    if not S:
        log("还没有任何尝试记录。")
    else:
        for seg in cfg["segments"]:
            log(f"\n{seg['id']}：")
            for att in st.attempts(S, seg["id"]):
                res = att.get("reservation") or {}
                log(f"  {att['attempt_id']}  {att['status']:<11} 来源 {att.get('origin')}  task {att.get('task_id') or '-'}  "
                    f"预占 {res.get('amount', '-')} {res.get('currency', '')}  服务端费用 {att.get('cost', '-')}")
    if ledger:
        snap = ledger.snapshot() if ledger.path.exists() else None
        if snap:
            log(f"\n预算：已花 {snap['spent']:g} + 在途预占 {snap['reserved']:g}，剩余 {snap['available']:g} / "
                f"上限 {snap['limit']:g} {snap['currency']}（账本 {ledger.path}）")
    return EXIT_OK


def resolve(cfg, out_dir, S, ledger, a):
    sid = a.resolve
    if not a.attempt or not (a.task_id or a.not_created):
        raise PipelineError("--resolve 需要同时给 --attempt 和（--task-id 或 --not-created）")
    att = next((x for x in st.attempts(S, sid) if x["attempt_id"] == a.attempt), None)
    if att is None:
        raise PipelineError(f"{sid} 没有 attempt {a.attempt}")
    if att["status"] != "unknown":
        raise PipelineError(f"attempt {a.attempt} 的状态是 {att['status']}，只有\"状态不明\"的才需要人工核对")
    if a.task_id:
        st.set_status(att, "submitted", "人工核对：服务端已有任务", task_id=a.task_id, resolved_manually=True)
        st.save(out_dir, S)
        log(f"已记录 {sid} 的任务号 {a.task_id}；再运行生成命令会继续查询它（不会重新提交）。")
    else:
        st.set_status(att, "not_created", "人工核对：服务端没有这个任务", resolved_manually=True)
        st.save(out_dir, S)
        if ledger and att.get("reservation"):
            ledger.release(att["reservation"]["key"], "manual_not_created")
        log(f"已确认 {sid} 的 attempt {a.attempt} 没有创建任务，预占已释放。要重新提交请加 --authorize-retry {sid}。")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
