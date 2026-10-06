"""独立旁白（TTS）+ 词级时间戳 + 符号事件时间轴。

TTS 提供方：
  edge  微软 Edge 在线语音（pip install edge-tts），流式返回 WordBoundary 词级时间戳。免费但需联网。
  file  使用你自己准备好的音频 + 词时间戳 JSON（[{"text":"这个","offset":1.2,"duration":0.3}, ...]）。
  mock  离线测试用：合成提示音，按字均匀给时间戳。只用于测试，不要用于成片。
所有结果按 (提供方, 音色, 语速, 音调, 文本) 的哈希缓存到 video-pipeline/.cache/tts/，重剪时只读缓存。

符号事件的定位：绑定"分段 ID + 旁白行 ID + 该行里第几次出现的词"，只在这一行的词时间戳里找，
不做全文搜索。时间戳不可靠（没有词级时间戳、对不上原文、找不到第 N 次出现）时明确报出来，
不会悄悄用估计值顶替。
"""

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

from .util import PipelineError, atomic_write_json, fingerprint, log, read_json, sha256_file

def cache_dir():
    base = os.environ.get("VP_CACHE_DIR") or (Path(__file__).resolve().parent.parent / ".cache")
    return Path(base) / "tts"
_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:…—\-~～\"'“”‘’（）()《》【】\[\]]+")


def norm(s):
    return _PUNCT.sub("", s or "")


# ---------------------------------------------------------------- 合成与缓存

def _key(provider, line):
    base = {"provider": provider, "voice": line["voice"], "rate": line["rate"], "pitch": line["pitch"],
            "text": line["text"], "v": 1}
    if provider == "file":
        base["audio_sha"] = sha256_file(line["_audio_path"])
        base["words_sha"] = sha256_file(line["_words_path"])
    return fingerprint(base)[:32]


def synthesize(cfg, allow_tts):
    """确保每行旁白都有音频和词时间戳。allow_tts=False 时只读缓存（独立重剪用），缺缓存就报错。"""
    n = cfg["_narration"]
    if not n:
        return {}
    CACHE_DIR = cache_dir()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for line in n["lines"]:
        if n["provider"] == "file":
            if not line.get("audio") or not line.get("words"):
                raise PipelineError(f"旁白 {line['id']} 用 file 提供方，需要同时给 audio 和 words 两个文件")
            line["_audio_path"] = (cfg["_dir"] / line["audio"]).resolve()
            line["_words_path"] = (cfg["_dir"] / line["words"]).resolve()
            for p in (line["_audio_path"], line["_words_path"]):
                if not p.exists():
                    raise PipelineError(f"旁白 {line['id']} 的文件不存在：{p}")
        key = _key(n["provider"], line)
        audio = CACHE_DIR / f"{key}.mp3"
        meta_path = CACHE_DIR / f"{key}.json"
        meta = read_json(meta_path)
        if not (meta and audio.exists()):
            if not allow_tts:
                raise PipelineError(f"旁白 {line['id']} 没有缓存音频。独立重剪只使用已有素材；"
                                    f"请先运行 --tts-only 生成旁白（edge 为免费在线语音）")
            log(f"  合成旁白 {line['id']}（{n['provider']}）…")
            if n["provider"] == "edge":
                words, boundary = _edge(line, audio)
            elif n["provider"] == "mock":
                words, boundary = _mock(line, audio)
            else:
                words, boundary = _file(line, audio)
            meta = {"line": {k: line[k] for k in ("id", "text", "voice", "rate", "pitch")},
                    "provider": n["provider"], "boundary": boundary, "words": words,
                    "duration": _duration(audio)}
            atomic_write_json(meta_path, meta)
        meta["alignment"], meta["alignment_note"] = _check_alignment(line["text"], meta)
        meta["audio"] = str(audio)
        out[line["id"]] = meta
    return out


def _duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise PipelineError(f"旁白音频无法解码：{path}")


def _edge(line, audio):  # pragma: no cover - 需要联网，离线测试不覆盖
    try:
        import edge_tts
    except ImportError:
        raise PipelineError("没有安装 edge-tts。请运行：python -m pip install edge-tts")

    async def go():
        try:
            comm = edge_tts.Communicate(line["text"], line["voice"], rate=line["rate"], pitch=line["pitch"],
                                        boundary="WordBoundary")
        except TypeError:   # 旧版本 edge-tts 没有 boundary 参数，默认就给 WordBoundary
            comm = edge_tts.Communicate(line["text"], line["voice"], rate=line["rate"], pitch=line["pitch"])
        words, kinds = [], set()
        tmp = audio.with_name(audio.name + ".part")
        with open(tmp, "wb") as f:
            async for chunk in comm.stream():
                t = chunk.get("type")
                if t == "audio":
                    f.write(chunk["data"])
                elif t in ("WordBoundary", "SentenceBoundary"):
                    kinds.add(t)
                    if t == "WordBoundary":
                        words.append({"text": chunk["text"], "offset": chunk["offset"] / 1e7,
                                      "duration": chunk["duration"] / 1e7})
        tmp.replace(audio)
        return words, ("WordBoundary" if "WordBoundary" in kinds else ("SentenceBoundary" if kinds else "none"))

    return asyncio.run(go())


def _mock(line, audio):
    """离线测试：每个汉字/字母 0.22 秒，标点停顿 0.15 秒，输出可解码的提示音和逐字时间戳。"""
    words, t = [], 0.1
    for ch in line["text"]:
        if norm(ch):
            words.append({"text": ch, "offset": round(t, 3), "duration": 0.2})
            t += 0.22
        else:
            t += 0.15
    total = t + 0.1
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=f=600:d={total:.3f}",
                    "-af", "volume=0.4", "-ac", "1", "-ar", "24000", str(audio)], check=True)
    return words, "WordBoundary"


def _file(line, audio):
    words = json.loads(Path(line["_words_path"]).read_text("utf-8"))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(line["_audio_path"]), str(audio)], check=True)
    ok = all(isinstance(w, dict) and {"text", "offset", "duration"} <= set(w) for w in words)
    return (words if ok else []), ("WordBoundary" if ok and words else "none")


def _check_alignment(text, meta):
    if meta.get("boundary") != "WordBoundary" or not meta.get("words"):
        return "none", "TTS 没有返回词级时间戳（WordBoundary），无法把符号精准对到词上"
    joined = "".join(norm(w["text"]) for w in meta["words"])
    if joined != norm(text):
        return "partial", "词时间戳拼起来和原文对不上，定位可能不准"
    return "word", "词级时间戳完整"


# ---------------------------------------------------------------- 符号事件定位

def _locate(meta, anchor):
    """在一行旁白自己的词时间戳里，找 anchor 的第 N 次出现。返回 (start, end) 相对该行开头的秒数。"""
    words = meta["words"]
    if "word_index" in anchor:
        i = int(anchor["word_index"])
        if not 0 <= i < len(words):
            raise LookupError(f"word_index={i} 超出范围（这一行共 {len(words)} 个词）")
        w = words[i]
        return w["offset"], w["offset"] + w["duration"], w["text"]
    target = norm(anchor["text"])
    occ = int(anchor.get("occurrence", 1))
    chars, owner = [], []
    for wi, w in enumerate(words):
        for ch in norm(w["text"]):
            chars.append(ch)
            owner.append(wi)
    s = "".join(chars)
    pos, start = -1, 0
    for _ in range(occ):
        pos = s.find(target, start)
        if pos < 0:
            break
        start = pos + len(target)
    if pos < 0:
        count = s.count(target)
        raise LookupError(f"这一行里\"{anchor['text']}\"只出现了 {count} 次，事件要求第 {occ} 次")
    w0, w1 = words[owner[pos]], words[owner[pos + len(target) - 1]]
    return w0["offset"], w1["offset"] + w1["duration"], anchor["text"]


def resolve_timeline(cfg, narr, seg_offsets, seg_durations):
    """计算旁白和符号事件在成片里的绝对时间。返回 (lines, events, problems)，problems 为 [(error|warn, 说明)]。"""
    lines, events, problems = [], [], []
    by_id = {}
    for line in (cfg["_narration"] or {}).get("lines", []):
        meta = narr.get(line["id"])
        if meta is None:
            continue
        t0 = seg_offsets[line["segment"]] + line["start"]
        t1 = t0 + meta["duration"]
        seg_end = seg_offsets[line["segment"]] + seg_durations[line["segment"]]
        item = {"id": line["id"], "segment": line["segment"], "start": round(t0, 3), "end": round(t1, 3),
                "audio": meta["audio"], "alignment": meta["alignment"]}
        lines.append(item)
        by_id[line["id"]] = (line, meta, t0)
        if t1 > seg_end + 1e-3:
            problems.append(("warn", f"旁白 {line['id']} 超出所属分段 {line['segment']}（结束于 {t1:.2f}s，分段结束于 {seg_end:.2f}s），会延续到下一段"))
        if meta["alignment"] != "word":
            problems.append(("warn", f"旁白 {line['id']}：{meta['alignment_note']}"))
    for e in (cfg["_overlay"] or {}).get("events", []):
        seg_start = seg_offsets[e["segment"]]
        seg_end = seg_start + seg_durations[e["segment"]]
        ev = {"id": e["id"], "symbol": e["symbol"], "segment": e["segment"], "position": e["position"],
              "size": e["size"], "fade": e["fade"], "narration": e.get("narration")}
        if e["anchor_kind"] == "manual_time":
            ev["start"] = seg_start + float(e["anchor"]["time"])
            ev["end"] = seg_start + float(e["anchor"].get("end_time", e["anchor"]["time"] + e["hold"]))
            ev["alignment"] = "manual_time"
            ev["note"] = "手动指定时间，未与旁白词对齐"
        else:
            line, meta, t0 = by_id.get(e["narration"], (None, None, None))
            if meta is None:
                ev.update(alignment="unaligned", note=f"绑定的旁白 {e['narration']} 没有可用音频")
            elif meta["alignment"] == "none":
                ev.update(alignment="unaligned", note=meta["alignment_note"])
            else:
                try:
                    ws, we, word = _locate(meta, e["anchor"])
                    end = we + e["hold"]
                    if e.get("end_anchor"):
                        end = _locate(meta, e["end_anchor"])[1]
                    ev["start"] = t0 + ws - e["lead"]
                    ev["end"] = t0 + end
                    ev["word"] = word
                    ev["word_time"] = round(t0 + ws, 3)
                    ev["alignment"] = "word" if meta["alignment"] == "word" else "partial"
                    ev["note"] = (f"旁白 {e['narration']} 第 {e['anchor'].get('occurrence', 1)} 次\"{word}\""
                                  if "text" in e["anchor"] else f"旁白 {e['narration']} 第 {e['anchor']['word_index']} 个词")
                except LookupError as err:
                    ev.update(alignment="unaligned", note=f"{e['id']}：{err}")
        if "start" in ev:
            ev["start"], ev["end"] = round(ev["start"], 3), round(ev["end"], 3)
            if ev["end"] <= ev["start"]:
                problems.append(("error", f"符号事件 {e['id']} 的结束时间不晚于开始时间"))
            if ev["start"] < seg_start - 1e-3 or ev["end"] > seg_end + 1e-3:
                problems.append(("warn", f"符号事件 {e['id']}（{ev['start']:.2f}–{ev['end']:.2f}s）超出所属分段 "
                                         f"{e['segment']}（{seg_start:.2f}–{seg_end:.2f}s）"))
        else:
            problems.append(("error", f"符号事件 {e['id']} 无法可靠定位：{ev['note']}"))
        events.append(ev)
    return lines, events, problems
