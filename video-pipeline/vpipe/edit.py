"""剪辑合成：只使用已经下载好的片段和已缓存的旁白，不会调用任何生成接口。

混音规则（从下到上）：
  1. 原声：每段视频自带的声音 × edit.original_volume（默认 1.0）。
     某段没有音轨时，只给这一段补等长静音，其他段的原声照常保留。
  2. 旁白：每行旁白按"分段起点 + 行内 start"放到绝对时间 × edit.narration_volume（默认 1.0）。
  3. 人声总线 = 原声 + 旁白（直接相加，不互相压低）。
  4. 背景音乐 × edit.bgm_volume（默认 0.3），循环/截断到成片长度，淡入淡出；
     edit.duck=true（默认）时，人声总线一响就把音乐压低（侧链压缩）。
  5. 总线响度统一到 −14 LUFS，再过限幅器防爆音。
成片长度 = 各段视频长度之和。旁白或符号超出成片结尾时直接报错，不截断旁白。
"""

import json
import os
import subprocess
from pathlib import Path

from . import narration as narr_mod, state as st
from .symbols import asset_path
from .util import PipelineError, atomic_write_json, iso, log, warn

def music_dir():
    return Path(os.environ.get("VP_MUSIC_DIR") or (Path(__file__).resolve().parent.parent / "music"))
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise PipelineError(f"无法读取视频 {path}：{r.stderr.strip()[:200]}")
    info = json.loads(r.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if v is None:
        raise PipelineError(f"{path} 里没有视频流")
    num, den = v["r_frame_rate"].split("/")
    return {"w": int(v["width"]), "h": int(v["height"]), "fps": float(num) / float(den),
            "duration": float(info["format"]["duration"]),
            "audio": any(s["codec_type"] == "audio" for s in info["streams"])}


def segment_files(cfg, S, out_dir):
    """每段使用"当前输入对应的已完成 attempt"的视频。没有就报错——重剪不会触发生成。"""
    files = []
    for seg in cfg["segments"]:
        sid = seg["id"]
        a = st.completed_attempt(S, sid)
        latest = st.latest(S, sid)
        if a is None:
            raise PipelineError(f"{sid} 还没有已完成的视频。独立重剪只使用已有素材，不会去生成；请先运行生成命令。")
        if latest is not a and latest["status"] != "completed":
            warn(f"{sid} 最新的 attempt 状态是 {latest['status']}，重剪继续使用之前已完成的 {a['attempt_id']}")
        p = Path(a.get("video") or f"{sid}.mp4")
        if not p.is_absolute():
            p = out_dir / p
        if not p.exists():
            raise PipelineError(f"{sid} 的视频文件不见了：{p}")
        files.append((seg, a, p))
    return files


def resolve_bgm(edit_cfg, project_dir):
    """bgm：不填（不配乐）／"auto"（按 bgm_mood 从音乐库自动选）／文件名。缺曲子只提醒、不报错。"""
    bgm = (edit_cfg.get("bgm") or "").strip()
    if not bgm:
        return None
    if bgm == "auto":
        mood = edit_cfg.get("bgm_mood") or "欢快"
        folder = music_dir() / mood
        tracks = sorted(p for p in folder.glob("*") if p.suffix.lower() in AUDIO_EXT) if folder.is_dir() else []
        if not tracks:
            warn(f"音乐库里没有\"{mood}\"类的曲子（{folder}），这次先不配乐")
            return None
        return tracks[sum(project_dir.name.encode("utf-8")) % len(tracks)]
    for base in (project_dir, music_dir()):
        p = base / bgm
        if p.exists():
            return p
    warn(f"找不到配乐文件 {bgm}，这次先不配乐")
    return None


def build_timeline(cfg, files, allow_tts, allow_unaligned):
    infos = [probe(p) for _, _, p in files]
    fps = infos[0]["fps"]
    offsets, durations, t = {}, {}, 0.0
    trims = []
    for (seg, _, _), info in zip(files, infos):
        trim = (1.0 / fps) if seg["_mode"] == "chain" else 0.0   # 接尾帧段的第一帧与上一段尾帧重复
        offsets[seg["id"]] = t
        durations[seg["id"]] = info["duration"] - trim
        trims.append(trim)
        t += info["duration"] - trim
    total = t
    narr = narr_mod.synthesize(cfg, allow_tts) if cfg.get("_narration") else {}
    lines, events, problems = narr_mod.resolve_timeline(cfg, narr, offsets, durations)
    for ln in lines:
        if ln["end"] > total + 1e-3:
            problems.append(("error", f"旁白 {ln['id']} 结束于 {ln['end']:.2f}s，超过成片长度 {total:.2f}s；不会截断旁白，请调整 start 或文字"))
    for ev in events:
        if "end" in ev and ev["end"] > total + 1e-3:
            problems.append(("error", f"符号事件 {ev['id']} 结束于 {ev['end']:.2f}s，超过成片长度 {total:.2f}s"))
    ov = cfg.get("_overlay")
    if ov and ov["require_all_enabled"]:
        shown = {e["symbol"] for e in events if e.get("alignment") in ("word", "partial", "manual_time")}
        missing = [s for s in ov["enabled"] if s not in shown]
        if missing:
            problems.append(("error", f"配置要展示的符号 {missing} 没有任何可用的出现事件"))
    errors = [m for lvl, m in problems if lvl == "error"]
    for lvl, m in problems:
        if lvl == "error":
            log(f"  [时间轴错误] {m}")
        else:
            warn(m)
    if errors and not allow_unaligned:
        raise PipelineError("时间轴有问题，未合成成片（不会假装已精准对齐）。修正配置后重剪；"
                            "确认要先出一版、跳过这些符号的话加 --allow-unaligned")
    usable = [e for e in events if "start" in e and e.get("alignment") != "unaligned" and e["end"] <= total + 1e-3]
    return {"infos": infos, "trims": trims, "offsets": offsets, "durations": durations, "total": total,
            "lines": lines, "events": events, "usable_events": usable, "problems": problems}


def compose(cfg, S, out_dir, allow_tts=False, allow_unaligned=False):
    files = segment_files(cfg, S, out_dir)
    tl = build_timeline(cfg, files, allow_tts, allow_unaligned)
    infos, total = tl["infos"], tl["total"]
    w, h, fps = infos[0]["w"], infos[0]["h"], infos[0]["fps"]
    edit_cfg = cfg.get("edit", {})
    project_dir = cfg["_dir"]
    bgm_path = resolve_bgm(edit_cfg, project_dir)

    args = ["ffmpeg", "-y", "-loglevel", "error"]
    idx = 0
    for _, _, p in files:
        args += ["-i", str(p)]
        idx += 1
    narr_inputs = []
    for ln in tl["lines"]:
        args += ["-i", ln["audio"]]
        narr_inputs.append((idx, ln))
        idx += 1
    ov_dir = (cfg.get("_overlay") or {}).get("assets_dir")
    ov_dir = (project_dir / ov_dir) if ov_dir else None
    ov_inputs = []
    for ev in tl["usable_events"]:
        dur = ev["end"] - ev["start"]
        args += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(asset_path(ev["symbol"], ov_dir))]
        ov_inputs.append((idx, ev, dur))
        idx += 1
    bgm_idx = None
    if bgm_path:
        args += ["-stream_loop", "-1", "-i", str(bgm_path)]
        bgm_idx = idx
        idx += 1

    parts, vcat, acat = [], "", ""
    for i, ((seg, _, _), info, trim) in enumerate(zip(files, infos, tl["trims"])):
        dur = info["duration"] - trim
        parts.append(f"[{i}:v]trim=start={trim:.4f},setpts=PTS-STARTPTS,"
                     f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{i}]")
        if info["audio"]:
            parts.append(f"[{i}:a]atrim=start={trim:.4f}:end={info['duration']:.4f},asetpts=PTS-STARTPTS,"
                         f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                         f"apad=whole_dur={dur:.4f},atrim=0:{dur:.4f}[a{i}]")
        else:
            # 只给无声片段补等长静音，不影响其他片段的原声
            parts.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.4f},aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
        vcat += f"[v{i}]"
        acat += f"[a{i}]"
    n = len(files)
    parts.append(f"{vcat}concat=n={n}:v=1:a=0[vbase]")
    parts.append(f"{acat}concat=n={n}:v=0:a=1,volume={float(edit_cfg.get('original_volume', 1.0))}[orig]")

    vcur = "vbase"
    for k, (i, ev, dur) in enumerate(ov_inputs):
        size_px = max(8, int(w * ev["size"]) // 2 * 2)
        f = min(ev["fade"], dur / 3)
        parts.append(f"[{i}:v]format=rgba,scale={size_px}:-1,"
                     f"fade=t=in:st=0:d={f:.3f}:alpha=1,fade=t=out:st={dur - f:.3f}:d={f:.3f}:alpha=1,"
                     f"setpts=PTS-STARTPTS+{ev['start']:.3f}/TB[ov{k}]")
        x = f"{ev['position']['x'] * w:.1f}-overlay_w/2"
        y = f"{ev['position']['y'] * h:.1f}-overlay_h/2"
        parts.append(f"[{vcur}][ov{k}]overlay=x={x}:y={y}:eof_action=pass:"
                     f"enable='between(t,{ev['start']:.3f},{ev['end']:.3f})'[vo{k}]")
        vcur = f"vo{k}"

    voice = "orig"
    if narr_inputs:
        nv = float(edit_cfg.get("narration_volume", (cfg.get("_narration") or {}).get("volume", 1.0)))
        labels = ""
        for k, (i, ln) in enumerate(narr_inputs):
            ms = int(round(ln["start"] * 1000))
            parts.append(f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                         f"adelay={ms}|{ms},volume={nv}[n{k}]")
            labels += f"[n{k}]"
        parts.append(f"[orig]{labels}amix=inputs={1 + len(narr_inputs)}:duration=first:normalize=0[voice]")
        voice = "voice"

    mix = voice
    if bgm_idx is not None:
        vol = float(edit_cfg.get("bgm_volume", 0.3))
        fi, fo = float(edit_cfg.get("bgm_fade_in", 0.5)), float(edit_cfg.get("bgm_fade_out", 1.5))
        parts.append(f"[{bgm_idx}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,volume={vol},"
                     f"afade=t=in:d={fi},afade=t=out:st={max(total - fo, 0):.3f}:d={fo},"
                     f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[bgm]")
        if edit_cfg.get("duck", True):
            parts.append(f"[{voice}]asplit=2[vmain][vsc]")
            parts.append("[bgm][vsc]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[bgmd]")
            parts.append("[vmain][bgmd]amix=inputs=2:duration=first:normalize=0[mix]")
        else:
            parts.append(f"[{voice}][bgm]amix=inputs=2:duration=first:normalize=0[mix]")
        mix = "mix"
        log(f"  配乐：{bgm_path.name}（音量 {vol}）")
    if edit_cfg.get("loudnorm", True):
        parts.append(f"[{mix}]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,alimiter=limit=0.95[aout]")
    else:
        parts.append(f"[{mix}]alimiter=limit=0.95[aout]")

    final = out_dir / edit_cfg.get("output", "final.mp4")
    tmp = final.with_name(final.stem + ".part" + final.suffix)
    args += ["-filter_complex", ";".join(parts), "-map", f"[{vcur}]", "-map", "[aout]",
             "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f"{total:.3f}", str(tmp)]
    log("\n=== 剪辑 ===")
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise PipelineError(f"ffmpeg 合成失败：{r.stderr.strip()[-800:]}")
    tmp.replace(final)
    timeline = {
        "generated_at": iso(), "final": str(final), "total": round(total, 3),
        "segments": [{"id": seg["id"], "attempt_id": a["attempt_id"], "video": str(p),
                      "offset": round(tl["offsets"][seg["id"]], 3), "duration": round(tl["durations"][seg["id"]], 3),
                      "had_audio": info["audio"], "audio_filled_with_silence": not info["audio"]}
                     for (seg, a, p), info in zip(files, infos)],
        "narration": tl["lines"], "symbol_events": tl["events"],
        "skipped_events": [e["id"] for e in tl["events"] if e not in tl["usable_events"]],
        "problems": [{"level": l, "message": m} for l, m in tl["problems"]],
        "mix": {"original_volume": float(edit_cfg.get("original_volume", 1.0)),
                "narration_volume": float(edit_cfg.get("narration_volume", 1.0)),
                "bgm": str(bgm_path) if bgm_path else None,
                "bgm_volume": float(edit_cfg.get("bgm_volume", 0.3)) if bgm_path else None,
                "duck": bool(edit_cfg.get("duck", True)), "loudnorm": bool(edit_cfg.get("loudnorm", True))},
    }
    atomic_write_json(out_dir / "timeline.json", timeline)
    log(f"  成片：{final}（{total:.2f} 秒，符号事件 {len(tl['usable_events'])} 个，旁白 {len(tl['lines'])} 行）")
    return final, timeline
