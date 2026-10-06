"""逐段验收。只检查、只出报告，验收不通过也不会自动重新生成（不会产生费用）。

自动检查：能否完整解码、画幅与分辨率、时长、音轨、黑帧、时间轴越界、符号事件覆盖。
人工确认：角色是否变脸、动作是否兑现、画面里生成的符号是否正确、口型——这些不能可靠地自动判断，
统一标为"待人工确认"，并输出联系图（缩略图拼板）方便人工看。
"""

import re
import subprocess
from pathlib import Path

from .edit import probe, segment_files
from .util import atomic_write_json, iso, log, read_json

RES_SHORT_SIDE = {"480p": 480, "720p": 720, "1080p": 1080, "4k": 2160}
MANUAL_SEGMENT = ["角色是否与参考图一致（有无变脸、换装）", "提示词里的动作是否都兑现（逐镜对照时间轴）",
                  "画面内生成的符号形状、颜色、顺序是否正确", "台词口型与发音是否对得上"]
MANUAL_FINAL = ["旁白说到指代词时，对应符号是否同步出现（看符号检查图）", "段与段衔接处是否跳变", "整体音量与配乐观感"]


def _decode_errors(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
    return (r.stderr or "").strip() if r.returncode == 0 else (r.stderr or "解码失败").strip() or "解码失败"


def _black(path, min_dur):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-vf", f"blackdetect=d={min_dur}:pix_th=0.10",
                        "-an", "-f", "null", "-"], capture_output=True, text=True)
    return [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", r.stderr)]


def contact_sheet(path, dest, duration, cols=4, rows=2, width=180):
    n = cols * rows
    rate = n / max(duration, 0.1)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
                    "-vf", f"fps={rate:.5f},scale={width}:-2,tile={cols}x{rows}:padding=4:margin=4:color=white",
                    "-frames:v", "1", str(dest)], check=True)


def symbol_sheet(final, events, dest, width=180):
    """在每个符号事件的中点各截一帧，拼成一张图，人工核对"说到这个时符号是否出现"。"""
    if not events:
        return None
    frames = []
    tmpdir = Path(dest).parent / "_sym"
    tmpdir.mkdir(exist_ok=True)
    for k, ev in enumerate(events):
        t = (ev["start"] + ev["end"]) / 2
        f = tmpdir / f"{k:02d}.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(final), "-frames:v", "1",
                        "-vf", f"scale={width}:-2", "-update", "1", str(f)], check=True)
        frames.append(f)
    if len(frames) == 1:
        frames[0].replace(dest)
        tmpdir.rmdir()
        return str(dest)
    cols = min(len(frames), 4)
    rows = (len(frames) + cols - 1) // cols
    args = ["ffmpeg", "-y", "-loglevel", "error"]
    for f in frames:
        args += ["-i", str(f)]
    pads = "".join(f"[{i}:v]" for i in range(len(frames)))
    blanks = cols * rows - len(frames)
    flt = pads
    if blanks:
        args += ["-f", "lavfi", "-i", f"color=c=white:s={width}x{width * 16 // 9}:d=1"]
        flt += "".join(f"[{len(frames)}:v]" for _ in range(blanks))
    layout = "|".join(f"{(i % cols) * width}_{(i // cols) * (width * 16 // 9)}" for i in range(cols * rows))
    args += ["-filter_complex", f"{flt}xstack=inputs={cols * rows}:layout={layout}:fill=white", "-frames:v", "1", str(dest)]
    subprocess.run(args, check=True)
    for f in frames:
        f.unlink()
    tmpdir.rmdir()
    return str(dest)


def run(cfg, S, out_dir):
    qcfg = {"duration_tolerance": 0.6, "black_min": 0.5, **(cfg.get("qc") or {})}
    qdir = out_dir / "qc"
    qdir.mkdir(exist_ok=True)
    report = {"generated_at": iso(), "project": cfg["_dir"].name, "config_version": cfg["_config_version"],
              "segments": [], "final": None, "auto_failures": [], "manual_pending": []}
    want_short = RES_SHORT_SIDE.get(cfg["resolution"])
    a_, b_ = (float(x) for x in cfg["size"].split(":"))
    want_ratio = a_ / b_
    for seg, att, path in segment_files(cfg, S, out_dir):
        sid = seg["id"]
        checks = []

        def add(name, ok, detail, level="error"):
            checks.append({"check": name, "ok": ok, "detail": detail, "level": level})
            if not ok and level == "error":
                report["auto_failures"].append(f"{sid}：{name}——{detail}")

        dec = _decode_errors(path)
        add("完整解码", not dec, dec[:300] or "无错误")
        info = probe(path)
        ratio = info["w"] / info["h"]
        add("画幅", abs(ratio - want_ratio) / want_ratio < 0.03, f"{info['w']}x{info['h']}（要求 {cfg['size']}）")
        if want_short:
            short = min(info["w"], info["h"])
            add("分辨率", abs(short - want_short) <= max(16, want_short * 0.05), f"短边 {short}（要求约 {want_short}）", "warn")
        add("时长", abs(info["duration"] - seg["duration"]) <= qcfg["duration_tolerance"],
            f"{info['duration']:.2f}s（要求 {seg['duration']}s ±{qcfg['duration_tolerance']}）")
        if cfg["generate_audio"]:
            add("音轨", info["audio"], "有音轨" if info["audio"] else "要求生成音频但片段没有音轨（成片会补静音）", "warn")
        blacks = _black(path, qcfg["black_min"])
        add("黑帧", not blacks, "无" if not blacks else "；".join(f"{a:.2f}–{b:.2f}s" for a, b in blacks))
        sheet = qdir / f"{sid}_contact.png"
        contact_sheet(path, sheet, info["duration"])
        manual = list(MANUAL_SEGMENT)
        if att.get("fingerprint") is None:
            manual.insert(0, "旧版记录没有保存输入指纹：请确认这段视频对应的就是当前提示词和参考图")
        for m in manual:
            report["manual_pending"].append(f"{sid}：{m}")
        report["segments"].append({"id": sid, "attempt_id": att["attempt_id"], "video": str(path), "checks": checks,
                                   "contact_sheet": str(sheet), "manual_pending": manual,
                                   "characters": (att.get("inputs") or {}).get("characters")})

    final = out_dir / (cfg.get("edit") or {}).get("output", "final.mp4")
    tl = read_json(out_dir / "timeline.json")
    if final.exists() and tl:
        fchecks = []

        def fadd(name, ok, detail, level="error"):
            fchecks.append({"check": name, "ok": ok, "detail": detail, "level": level})
            if not ok and level == "error":
                report["auto_failures"].append(f"成片：{name}——{detail}")

        dec = _decode_errors(final)
        fadd("完整解码", not dec, dec[:300] or "无错误")
        info = probe(final)
        fadd("时长", abs(info["duration"] - tl["total"]) <= 0.25, f"{info['duration']:.2f}s（时间轴 {tl['total']:.2f}s）")
        fadd("音轨", info["audio"], "有" if info["audio"] else "无")
        for p in tl.get("problems", []):
            fadd("时间轴", p["level"] != "error", p["message"], p["level"])
        events = tl.get("symbol_events", [])
        over = [e["id"] for e in events if "end" in e and (e["start"] < -1e-3 or e["end"] > tl["total"] + 1e-3)]
        fadd("时间轴越界", not over, "无" if not over else f"越界事件：{over}")
        enabled = (cfg.get("_overlay") or {}).get("enabled", [])
        aligned = [e for e in events if e.get("alignment") in ("word", "partial", "manual_time") and e["id"] not in tl.get("skipped_events", [])]
        missing = [s for s in enabled if s not in {e["symbol"] for e in aligned}]
        fadd("符号事件覆盖", not missing, "全部配置的符号都有出现事件" if not missing else f"缺少：{missing}")
        unaligned = [e["id"] for e in events if e.get("alignment") == "unaligned"]
        if unaligned:
            fadd("符号对齐", False, f"未能与旁白词对齐、已跳过的事件：{unaligned}")
        partial = [e["id"] for e in events if e.get("alignment") == "partial"]
        if partial:
            fadd("符号对齐", False, f"词时间戳与原文不完全一致，定位可能不准：{partial}", "warn")
        manual_t = [e["id"] for e in events if e.get("alignment") == "manual_time"]
        if manual_t:
            fadd("符号对齐", True, f"手动指定时间（未与旁白对齐）：{manual_t}", "warn")
        fsheet = qdir / "final_contact.png"
        contact_sheet(final, fsheet, info["duration"], cols=5, rows=2)
        ssheet = symbol_sheet(final, aligned, qdir / "final_symbols.png")
        for m in MANUAL_FINAL:
            report["manual_pending"].append(f"成片：{m}")
        report["final"] = {"video": str(final), "checks": fchecks, "contact_sheet": str(fsheet), "symbol_sheet": ssheet}
    else:
        report["auto_failures"].append("成片：还没有成片或 timeline.json（先运行剪辑）")

    report["status"] = "fail" if report["auto_failures"] else "pending_manual"
    atomic_write_json(qdir / "report.json", report)
    (qdir / "report.md").write_text(_markdown(report), "utf-8")
    log(f"\n=== 验收 ===\n  自动检查：{'不通过' if report['auto_failures'] else '通过'}；"
        f"待人工确认 {len(report['manual_pending'])} 项。报告：{qdir / 'report.md'}")
    for f in report["auto_failures"]:
        log(f"  ✗ {f}")
    return report


def _markdown(r):
    lines = [f"# 验收报告：{r['project']}", "", f"- 生成时间：{r['generated_at']}",
             f"- 配置版本：{r['config_version']}",
             f"- 结论：{'**自动检查不通过**' if r['status'] == 'fail' else '自动检查通过，**等待人工确认**'}",
             "- 验收不通过不会自动重新生成；需要返工请明确授权（--authorize-retry / --allow-regenerate）。", ""]
    for s in r["segments"]:
        lines += [f"## 分段 {s['id']}（attempt {s['attempt_id']}）", "", f"联系图：`{s['contact_sheet']}`", "",
                  "| 检查 | 结果 | 说明 |", "|---|---|---|"]
        lines += [f"| {c['check']} | {'✓' if c['ok'] else ('⚠' if c['level'] == 'warn' else '✗')} | {c['detail']} |"
                  for c in s["checks"]]
        if s.get("characters"):
            lines += ["", "角色参考：" + "；".join(f"{c['name']}（{c['id']}，sha256 {str(c['sha256'])[:12]}…，"
                                                  f"{'已作为参考图发送' if c['sent_to_api'] else '仅记录，首帧图承载'}）"
                                                  for c in s["characters"])]
        lines += ["", "待人工确认：", *[f"- [ ] {m}" for m in s["manual_pending"]], ""]
    if r["final"]:
        f = r["final"]
        lines += ["## 成片", "", f"联系图：`{f['contact_sheet']}`", f"符号检查图：`{f['symbol_sheet']}`", "",
                  "| 检查 | 结果 | 说明 |", "|---|---|---|"]
        lines += [f"| {c['check']} | {'✓' if c['ok'] else ('⚠' if c['level'] == 'warn' else '✗')} | {c['detail']} |"
                  for c in f["checks"]]
        lines += ["", "待人工确认：", *[f"- [ ] {m}" for m in MANUAL_FINAL], ""]
    return "\n".join(lines)
