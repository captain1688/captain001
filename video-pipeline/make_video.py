#!/usr/bin/env python3
"""用 apimart 的 Seedance 2.0 分段并发生成视频，再用 ffmpeg 拼接成片。

用法：
    python make_video.py projects/xueren            # 生成并剪辑
    python make_video.py projects/xueren --dry-run  # 只检查配置、打印请求，不花钱
    python make_video.py projects/xueren --edit-only  # 只重新剪辑已下载的片段

需要：
    环境变量 APIMART_API_KEY
    ffmpeg / ffprobe 在 PATH 中
只用 Python 标准库，无需 pip 安装。
"""

import argparse
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API_BASE = os.environ.get("APIMART_API_BASE", "https://api.apimart.ai/v1").rstrip("/")
POLL_INTERVAL = 10          # 秒
POLL_TIMEOUT = 30 * 60      # 单段最长等待 30 分钟
UPLOAD_TTL = 70 * 3600      # 上传图片官方保存 72 小时，留 2 小时余量

DEFAULTS = {
    "model": "seedance-2.0-mini",
    "resolution": "480p",
    "size": "9:16",
    "generate_audio": True,
}


def log(msg):
    print(msg, flush=True)


def die(msg):
    print(f"\n[错误] {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- HTTP

def _request(method, url, api_key, body=None, content_type=None):
    headers = {"Authorization": f"Bearer {api_key}"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        die(f"{method} {url} 返回 HTTP {e.code}：{detail}")
    except urllib.error.URLError as e:
        die(f"{method} {url} 网络错误：{e.reason}")


def post_json(path, api_key, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _request("POST", API_BASE + path, api_key, body, "application/json")


def get_json(path, api_key):
    return _request("GET", API_BASE + path, api_key)


def upload_image(path, api_key):
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(path.name)[0] or "image/png"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode("utf-8")
    body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")
    resp = _request("POST", API_BASE + "/uploads/images", api_key, body,
                    f"multipart/form-data; boundary={boundary}")
    url = resp.get("url")
    if not url:
        die(f"上传 {path} 没有拿到 url，返回：{resp}")
    return url


def download(url, dest):
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=300) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.rename(dest)


# ---------------------------------------------------------------- 结果解析

def _walk_urls(obj, path=""):
    """遍历返回 JSON，产出 (路径, url)。url 字段可能是字符串也可能是列表。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_urls(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_urls(v, f"{path}[{i}]")
    elif isinstance(obj, str) and obj.startswith("http"):
        yield path.lower(), obj


def pick_video_url(result):
    urls = list(_walk_urls(result))
    for p, u in urls:
        if "video" in p:
            return u
    for p, u in urls:
        if u.split("?")[0].lower().endswith((".mp4", ".mov")):
            return u
    return None


def pick_last_frame_url(result):
    for p, u in _walk_urls(result):
        if "last" in p or "frame" in p:
            return u
    for p, u in _walk_urls(result):
        if "image" in p or u.split("?")[0].lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            return u
    return None


# ---------------------------------------------------------------- 工程与状态

def load_project(project_dir):
    cfg_path = project_dir / "project.json"
    if not cfg_path.exists():
        die(f"找不到 {cfg_path}")
    cfg = json.loads(cfg_path.read_text("utf-8"))
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    segs = cfg.get("segments") or die("project.json 里没有 segments")
    ids = set()
    for i, seg in enumerate(segs):
        sid = seg.get("id") or die(f"第 {i + 1} 段缺少 id")
        if sid in ids:
            die(f"段 id 重复：{sid}")
        ids.add(sid)
        pf = project_dir / (seg.get("prompt_file") or die(f"{sid} 缺少 prompt_file"))
        if not pf.exists():
            die(f"{sid} 的提示词文件不存在：{pf}")
        seg["_prompt"] = pf.read_text("utf-8").strip()
        if not seg["_prompt"]:
            die(f"{sid} 的提示词是空的")
        d = int(seg.get("duration", 10))
        if not 4 <= d <= 15:
            die(f"{sid} 的 duration={d}，Seedance 2.0 只支持 4–15 秒")
        seg["duration"] = d
        cont = bool(seg.get("continue_from_previous"))
        if cont and i == 0:
            die("第一段不能设置 continue_from_previous")
        first = seg.get("first_frame")
        refs = seg.get("reference_images") or []
        # apimart 限制：首帧模式(image_with_roles)和参考图模式(image_urls)不能同时用
        if (cont or first) and refs:
            die(f"{sid}：首帧（first_frame / continue_from_previous）和 reference_images 不能同时使用，这是 API 的限制")
        if cont and first:
            die(f"{sid}：continue_from_previous 和 first_frame 只能二选一")
        if len(refs) > 9:
            die(f"{sid}：reference_images 最多 9 张")
        for p in ([first] if first else []) + refs:
            if not (project_dir / p).exists():
                die(f"{sid} 引用的图片不存在：{project_dir / p}（请先把这张图放进去，图片提示词见 refs/README.md）")
    return cfg


def load_state(out_dir):
    p = out_dir / "state.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {"uploads": {}, "segments": {}}


def save_state(out_dir, state):
    p = out_dir / "state.json"
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def image_url_for(path, state, api_key, out_dir):
    """上传本地图片，72 小时内复用同一个 URL，避免重复上传。"""
    key = str(path.resolve())
    stat = path.stat()
    cached = state["uploads"].get(key)
    if cached and cached["mtime"] == stat.st_mtime and time.time() - cached["at"] < UPLOAD_TTL:
        return cached["url"]
    log(f"  上传图片 {path.name} …")
    url = upload_image(path, api_key)
    state["uploads"][key] = {"url": url, "mtime": stat.st_mtime, "at": time.time()}
    save_state(out_dir, state)
    return url


# ---------------------------------------------------------------- 生成

def build_payload(cfg, seg, image_urls=None, first_frame_url=None):
    payload = {
        "model": cfg["model"],
        "prompt": seg["_prompt"],
        "duration": seg["duration"],
        "size": cfg["size"],
        "resolution": cfg["resolution"],
        "generate_audio": cfg["generate_audio"],
        "return_last_frame": True,
    }
    if cfg.get("seed") is not None:
        payload["seed"] = cfg["seed"]
    if first_frame_url:
        payload["image_with_roles"] = [{"url": first_frame_url, "role": "first_frame"}]
    elif image_urls:
        payload["image_urls"] = image_urls
    return payload


def poll_once(task_id, api_key):
    return get_json(f"/tasks/{task_id}?language=zh", api_key).get("data", {})


def extract_last_frame(video, dest):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-0.2", "-i", str(video),
                    "-frames:v", "1", "-update", "1", str(dest)], check=True)


def _submit(cfg, seg, prev_id, project_dir, out_dir, state, api_key, dry_run):
    sid = seg["id"]
    first_url, refs = None, []
    if seg.get("continue_from_previous"):
        if dry_run:
            first_url = f"<{prev_id} 的尾帧，自动上传>"
        else:
            # 统一上传本地保存的尾帧：API 返回的链接会过期，本地文件不会
            last_png = out_dir / f"{prev_id}_last.png"
            if not last_png.exists():
                die(f"{sid} 需要 {prev_id} 的尾帧，但找不到 {last_png}")
            first_url = image_url_for(last_png, state, api_key, out_dir)
    elif seg.get("first_frame"):
        p = project_dir / seg["first_frame"]
        first_url = f"<上传 {p.name}>" if dry_run else image_url_for(p, state, api_key, out_dir)
    for r in seg.get("reference_images") or []:
        p = project_dir / r
        refs.append(f"<上传 {p.name}>" if dry_run else image_url_for(p, state, api_key, out_dir))

    payload = build_payload(cfg, seg, refs, first_url)
    if dry_run:
        log(f"\n=== {sid}（{seg['duration']} 秒）请求预览 ===")
        shown = dict(payload, prompt=payload["prompt"][:80] + f"…（共 {len(payload['prompt'])} 字）")
        log(json.dumps(shown, ensure_ascii=False, indent=2))
        return
    resp = post_json("/videos/generations", api_key, payload)
    try:
        task_id = resp["data"][0]["task_id"]
    except (KeyError, IndexError, TypeError):
        die(f"[{sid}] 提交后没拿到 task_id，返回：{resp}")
    rec = state["segments"].setdefault(sid, {})
    rec.update(task_id=task_id, status="submitted", submitted_at=time.time())
    save_state(out_dir, state)
    log(f"  [{sid}] 已提交，任务 ID：{task_id}")


def _finish(seg, data, out_dir, state):
    sid = seg["id"]
    result = data.get("result") or {}
    (out_dir / f"{sid}_result.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    vurl = pick_video_url(result)
    if not vurl:
        die(f"[{sid}] 任务完成但没找到视频地址，原始结果见 {sid}_result.json")
    video = out_dir / f"{sid}.mp4"
    download(vurl, video)
    # 尾帧：优先用 API 返回的，没有就自己从视频里截（只有下一段要接它时才真正用到）
    last_png = out_dir / f"{sid}_last.png"
    lurl = pick_last_frame_url({k: v for k, v in result.items() if "video" not in k.lower()})
    if lurl:
        download(lurl, last_png)
    else:
        extract_last_frame(video, last_png)
    state["segments"][sid].update(status="completed", video=video.name, last_frame_url=lurl,
                                  cost=data.get("cost"), credits_cost=data.get("credits_cost"))
    save_state(out_dir, state)
    log(f"  [{sid}] 完成并已下载：{video.name}（费用 {data.get('cost')}）")


def generate(cfg, project_dir, out_dir, api_key, dry_run):
    """并发生成：互不依赖的段一次性全部提交，然后一起轮询。
    只有设置了 continue_from_previous 的段，才会等上一段完成后再提交。"""
    state = load_state(out_dir)
    segs = cfg["segments"]
    prev_of = {s["id"]: (segs[i - 1]["id"] if i else None) for i, s in enumerate(segs)}

    def rec(sid):
        return state["segments"].setdefault(sid, {})

    def done(sid):
        return rec(sid).get("status") == "completed" and (out_dir / f"{sid}.mp4").exists()

    for s in segs:
        if done(s["id"]):
            log(f"  [{s['id']}] 已生成过，跳过（要重做就删掉 output/{s['id']}.mp4 和 state.json 里的 {s['id']}）")
        elif rec(s["id"]).get("task_id"):
            log(f"  [{s['id']}] 继续查询之前提交的任务 {rec(s['id'])['task_id']}（不会重复扣费）")

    if dry_run:
        for s in segs:
            if not done(s["id"]) and not rec(s["id"]).get("task_id"):
                _submit(cfg, s, prev_of[s["id"]], project_dir, out_dir, state, api_key, True)
        waits = [s["id"] for s in segs if s.get("continue_from_previous")]
        parallel = [s["id"] for s in segs if not s.get("continue_from_previous")]
        log(f"\n并发提交：{', '.join(parallel)}" + (f"｜需等上一段完成：{', '.join(waits)}" if waits else ""))
        return

    last_shown = {}
    start = time.time()
    while not all(done(s["id"]) for s in segs):
        # 1) 提交所有"依赖已满足、还没提交"的段
        for s in segs:
            sid = s["id"]
            if done(sid) or rec(sid).get("task_id"):
                continue
            if s.get("continue_from_previous") and not done(prev_of[sid]):
                continue
            _submit(cfg, s, prev_of[sid], project_dir, out_dir, state, api_key, False)

        # 2) 轮询所有进行中的段
        for s in segs:
            sid = s["id"]
            r = rec(sid)
            if done(sid) or not r.get("task_id"):
                continue
            data = poll_once(r["task_id"], api_key)
            status, progress = data.get("status"), data.get("progress")
            if last_shown.get(sid) != (status, progress):
                log(f"  [{sid}] 状态 {status}，进度 {progress}%")
                last_shown[sid] = (status, progress)
            if status == "completed":
                _finish(s, data, out_dir, state)
            elif status in ("failed", "cancelled"):
                # 清掉任务号，方便修改提示词后重跑这一段
                tid = r.pop("task_id")
                r["status"] = status
                save_state(out_dir, state)
                die(f"[{sid}] 任务 {tid} {status}：{data.get('error')}（其他段的进度已保存，重跑只会重做失败的段）")

        if all(done(s["id"]) for s in segs):
            break
        if time.time() - start > POLL_TIMEOUT:
            die(f"等待超过 {POLL_TIMEOUT // 60} 分钟仍未全部完成；稍后重跑本命令会接着查询，不会重复扣费")
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------- 剪辑

MUSIC_DIR = Path(__file__).resolve().parent / "music"
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")


def probe(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)],
                         check=True, capture_output=True, text=True).stdout
    info = json.loads(out)
    streams = info["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    num, den = v["r_frame_rate"].split("/")
    return {"w": int(v["width"]), "h": int(v["height"]), "fps": float(num) / float(den),
            "duration": float(info["format"]["duration"]),
            "audio": any(s["codec_type"] == "audio" for s in streams)}


def resolve_bgm(edit_cfg, project_dir):
    """bgm 可以是：不填（不配乐）／"auto"（按 bgm_mood 从音乐库自动选）／文件名。"""
    bgm = (edit_cfg.get("bgm") or "").strip()
    if not bgm:
        return None
    if bgm == "auto":
        mood = edit_cfg.get("bgm_mood") or "欢快"
        folder = MUSIC_DIR / mood
        tracks = sorted(p for p in folder.glob("*") if p.suffix.lower() in AUDIO_EXT) if folder.is_dir() else []
        if not tracks:
            # 不中断：视频已经付费生成了，先出无配乐成片，补好音乐后用 --edit-only 重剪
            log(f"  [提醒] 音乐库里没有\"{mood}\"类的曲子（{folder}），这次先不配乐")
            return None
        # 同一个工程每次选同一首，不同工程分散到不同曲子
        idx = sum(project_dir.name.encode("utf-8")) % len(tracks)
        return tracks[idx]
    for base in (project_dir, MUSIC_DIR):
        p = (base / bgm)
        if p.exists():
            return p
    log(f"  [提醒] 找不到配乐文件 {bgm}（工程目录和 {MUSIC_DIR} 里都没有），这次先不配乐")
    return None


def edit(cfg, project_dir, out_dir):
    segs = cfg["segments"]
    files = [out_dir / f"{s['id']}.mp4" for s in segs]
    missing = [f.name for f in files if not f.exists()]
    if missing:
        die(f"还没生成这些片段：{', '.join(missing)}")
    infos = [probe(f) for f in files]
    w, h, fps = infos[0]["w"], infos[0]["h"], infos[0]["fps"]
    keep_audio = all(i["audio"] for i in infos)
    if not keep_audio:
        log("  有片段没有音轨，成片将不保留原声")

    edit_cfg = cfg.get("edit", {})
    bgm_path = resolve_bgm(edit_cfg, project_dir)

    args = ["ffmpeg", "-y", "-loglevel", "error"]
    for f in files:
        args += ["-i", str(f)]
    if bgm_path:
        args += ["-stream_loop", "-1", "-i", str(bgm_path)]

    one_frame = 1.0 / fps
    parts, concat_in = [], ""
    total = 0.0
    for i, seg in enumerate(segs):
        # 续接段的第一帧就是上一段的尾帧，去掉这一帧避免画面"顿一下"
        trim = one_frame if seg.get("continue_from_previous") else 0
        total += infos[i]["duration"] - trim
        parts.append(f"[{i}:v]trim=start={trim},setpts=PTS-STARTPTS,"
                     f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{i}]")
        concat_in += f"[v{i}]"
        if keep_audio:
            parts.append(f"[{i}:a]atrim=start={trim},asetpts=PTS-STARTPTS,"
                         f"aresample=48000,aformat=channel_layouts=stereo[a{i}]")
            concat_in += f"[a{i}]"
    n = len(segs)
    if keep_audio:
        parts.append(f"{concat_in}concat=n={n}:v=1:a=1[vout][aseq]")
    else:
        parts.append(f"{concat_in}concat=n={n}:v=1:a=0[vout]")

    mix = None
    if bgm_path:
        vol = float(edit_cfg.get("bgm_volume", 0.3))
        fade_in = float(edit_cfg.get("bgm_fade_in", 0.5))
        fade_out = float(edit_cfg.get("bgm_fade_out", 1.5))
        parts.append(f"[{n}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,volume={vol},"
                     f"afade=t=in:d={fade_in},afade=t=out:st={max(total - fade_out, 0):.3f}:d={fade_out},"
                     f"aresample=48000,aformat=channel_layouts=stereo[bgm]")
        if keep_audio:
            if edit_cfg.get("duck", True):
                # 有台词/音效时自动压低配乐，说完再恢复
                parts.append("[aseq]asplit=2[voice][sc]")
                parts.append("[bgm][sc]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[bgmd]")
                parts.append("[voice][bgmd]amix=inputs=2:duration=first:normalize=0[mix]")
            else:
                parts.append("[aseq][bgm]amix=inputs=2:duration=first:normalize=0[mix]")
        else:
            parts.append("[bgm]anull[mix]")
        mix = "[mix]"
        log(f"  配乐：{bgm_path.name}（音量 {vol}）")
    elif keep_audio:
        mix = "[aseq]"

    amap = None
    if mix:
        if edit_cfg.get("loudnorm", True):
            # 统一到短视频平台常用响度，避免忽大忽小
            parts.append(f"{mix}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")
            amap = "[aout]"
        else:
            amap = mix

    final = out_dir / edit_cfg.get("output", "final.mp4")
    args += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
    if amap:
        args += ["-map", amap, "-c:a", "aac", "-b:a", "192k"]
    args += ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", "-t", f"{total:.3f}", str(final)]
    log("\n=== 剪辑 ===")
    subprocess.run(args, check=True)
    log(f"  成片：{final}（{total:.1f} 秒）")
    return final


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Seedance 2.0（apimart）分段生成 + 自动剪辑")
    ap.add_argument("project", help="工程目录，例如 projects/xueren")
    ap.add_argument("--dry-run", action="store_true", help="只检查配置并打印请求，不调用付费接口")
    ap.add_argument("--edit-only", action="store_true", help="只用已下载的片段重新剪辑")
    a = ap.parse_args()

    project_dir = Path(a.project).resolve()
    cfg = load_project(project_dir)
    out_dir = project_dir / "output"
    out_dir.mkdir(exist_ok=True)

    for tool in ("ffmpeg", "ffprobe"):
        if not a.dry_run and not shutil.which(tool):
            die(f"找不到 {tool}，请先安装 ffmpeg 并加入 PATH")

    if not a.edit_only:
        api_key = os.environ.get("APIMART_API_KEY", "").strip()
        if not api_key and not a.dry_run:
            die("没有设置环境变量 APIMART_API_KEY")
        total = sum(s["duration"] for s in cfg["segments"])
        log(f"工程：{project_dir.name}｜{len(cfg['segments'])} 段，共 {total} 秒｜{cfg['model']} {cfg['resolution']} {cfg['size']}")
        generate(cfg, project_dir, out_dir, api_key, a.dry_run)
        if a.dry_run:
            log("\n[dry-run] 配置检查通过，没有调用任何付费接口。")
            return
    edit(cfg, project_dir, out_dir)


if __name__ == "__main__":
    main()
