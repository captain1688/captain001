"""读取并校验 project.json：角色参考图、素材清单、预算与价格、旁白与符号配置。"""

import json
import os
import subprocess
from pathlib import Path

from . import PIPELINE_VERSION
from .util import PipelineError, fingerprint, read_json, sha256_file, sha256_text, warn

DEFAULTS = {
    "model": "seedance-2.0-mini",
    "resolution": "480p",
    "size": "9:16",
    "generate_audio": True,
}
MODES = ("first_frame", "reference", "chain", "text")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_MAGIC = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpeg", b"GIF8": "gif", b"RIFF": "webp"}
SYMBOLS = ("plus", "heart", "star", "666", "share")
DEFAULT_LEGEND = "【参考图对应】{items}。"


def _err(msg):
    raise PipelineError(msg)


# ---------------------------------------------------------------- 素材清单

def load_asset_manifest(project_dir, cfg):
    """本地素材清单：把 "asset:<key>" 映射到本机图片路径。清单不进仓库（.gitignore 已排除）。
    查找顺序：project.json 的 assets_manifest → 工程目录/assets.local.json →
    video-pipeline/assets.local.json → 环境变量 VP_ASSETS_MANIFEST。后面的不覆盖前面的同名 key。"""
    candidates = []
    if cfg.get("assets_manifest"):
        candidates.append(project_dir / cfg["assets_manifest"])
    candidates.append(project_dir / "assets.local.json")
    candidates.append(Path(__file__).resolve().parent.parent / "assets.local.json")
    if os.environ.get("VP_ASSETS_MANIFEST"):
        candidates.append(Path(os.environ["VP_ASSETS_MANIFEST"]))
    assets, sources = {}, []
    for p in candidates:
        data = read_json(p)
        if data is None:
            continue
        mapping = data.get("assets", data) if isinstance(data, dict) else {}
        for k, v in mapping.items():
            if k.startswith("_") or k in assets:
                continue
            path = Path(v)
            if not path.is_absolute():
                path = (p.parent / path)
            assets[k] = path.resolve()
        sources.append(str(p))
    return assets, sources


def resolve_image(ref, project_dir, assets, what):
    if not ref:
        _err(f"{what} 没有指定图片")
    if isinstance(ref, str) and ref.startswith("asset:"):
        key = ref[len("asset:"):]
        if key not in assets:
            _err(f"{what} 引用了素材 {ref}，但本地素材清单里没有这个 key。"
                 f"请在 assets.local.json 里加上 \"{key}\": \"本机图片路径\"（清单格式见 assets.local.example.json）")
        return assets[key]
    return (project_dir / ref).resolve()


def check_image(path, what):
    """确认图片真实存在、格式允许、大小不超限、能被解码。返回 {path, sha256, width, height, format}。"""
    path = Path(path)
    if not path.is_file():
        _err(f"{what} 的图片不存在：{path}。缺图会停止，不会改成纯文字生成。")
    size = path.stat().st_size
    if size == 0:
        _err(f"{what} 的图片是空文件：{path}")
    if size > MAX_IMAGE_BYTES:
        _err(f"{what} 的图片超过 20MB（apimart 上传上限）：{path}")
    with open(path, "rb") as f:
        head = f.read(12)
    fmt = next((v for k, v in IMAGE_MAGIC.items() if head.startswith(k)), None)
    if fmt == "webp" and head[8:12] != b"WEBP":
        fmt = None
    if not fmt:
        _err(f"{what} 的图片格式不支持（只接受 png / jpeg / gif / webp）：{path}")
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height", "-of", "json", str(path)],
                             capture_output=True, text=True, timeout=60)
        dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "null", "-"],
                             capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        _err("找不到 ffmpeg / ffprobe，无法校验图片能否解码")
    streams = (json.loads(out.stdout or "{}").get("streams") or [{}]) if out.returncode == 0 else [{}]
    w, h = streams[0].get("width"), streams[0].get("height")
    if out.returncode != 0 or dec.returncode != 0 or not w or not h or dec.stderr.strip():
        _err(f"{what} 的图片无法正常解码：{path}（{(dec.stderr or out.stderr).strip()[:200]}）")
    return {"path": str(path), "sha256": sha256_file(path), "width": w, "height": h, "format": fmt}


# ---------------------------------------------------------------- 预算与价格

def load_budget(project_dir, cfg, out_dir):
    """预算可以写在工程里（单条），也可以用 batch 文件让多条工程共用一个预算和账本。
    返回 None 表示没有配置预算 —— 这种情况下禁止正式提交。"""
    if cfg.get("batch"):
        bpath = (project_dir / cfg["batch"]).resolve()
        batch = read_json(bpath)
        if batch is None:
            _err(f"找不到批次配置 {bpath}")
        ledger = bpath.with_name(bpath.stem + ".ledger.json")
        batch_id = batch.get("batch_id") or bpath.stem
        src = batch
    elif cfg.get("budget"):
        ledger = out_dir / "budget.ledger.json"
        batch_id = f"project:{project_dir.name}"
        src = cfg
    else:
        return None
    b = src.get("budget") or {}
    limit = b.get("limit")
    currency = b.get("currency")
    if not isinstance(limit, (int, float)) or limit < 0:
        _err(f"预算 limit 必须是非负数字（{batch_id}）")
    if not currency:
        _err(f"预算没有写币种 currency（{batch_id}）")
    return {
        "batch_id": batch_id,
        "limit": float(limit),
        "currency": currency,
        "max_repairs_per_segment": int(b.get("max_repairs_per_segment", 0)),
        "max_repairs_total": b.get("max_repairs_total"),
        "release_failed_without_cost": bool(b.get("release_failed_without_cost", False)),
        "actual_cost_currency": b.get("actual_cost_currency"),
        "pricing": src.get("pricing") or [],
        "ledger": ledger,
    }


def price_for(budget, cfg, seg):
    """从配置里的价格表找本段的单价并估算费用。找不到或价格为空 → 价格不明，抛错，禁止提交。"""
    for item in budget["pricing"]:
        if item.get("model") != cfg["model"]:
            continue
        if item.get("resolution") not in (None, cfg["resolution"]):
            continue
        if "generate_audio" in item and bool(item["generate_audio"]) != bool(cfg["generate_audio"]):
            continue
        price, unit, cur = item.get("price"), item.get("unit"), item.get("currency")
        if not isinstance(price, (int, float)):
            _err(f"价格不明：{cfg['model']} / {cfg['resolution']} 的 price 没有填写。请到 apimart 核对后写入价格表，再提交。")
        if unit not in ("second", "task"):
            _err(f"价格表的计费单位 unit 只能是 second（按秒）或 task（按条），现在是 {unit!r}")
        if cur != budget["currency"]:
            _err(f"价格表币种 {cur!r} 和预算币种 {budget['currency']!r} 不一致；不做汇率换算，请统一后再提交")
        est = float(price) * (seg["duration"] if unit == "second" else 1)
        return {"estimate": round(est, 6), "price": float(price), "unit": unit, "currency": cur,
                "source": item.get("source"), "checked_at": item.get("checked_at")}
    _err(f"价格不明：价格表里没有 {cfg['model']} / {cfg['resolution']} / 音频={cfg['generate_audio']} 的价格。"
         f"请核对后写入价格表，再提交。")


# ---------------------------------------------------------------- 工程

def load_project(project_dir, check_images=True):
    project_dir = Path(project_dir).resolve()
    cfg_path = project_dir / "project.json"
    if not cfg_path.exists():
        _err(f"找不到 {cfg_path}")
    raw = cfg_path.read_text("utf-8")
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        _err(f"project.json 不是合法 JSON：{e}")
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    cfg["_dir"] = project_dir
    cfg["_config_version"] = sha256_text(raw + "|" + PIPELINE_VERSION)[:16]
    assets, sources = load_asset_manifest(project_dir, cfg)
    cfg["_asset_sources"] = sources

    chars = cfg.get("characters") or {}
    if not isinstance(chars, dict):
        _err("characters 必须是 {角色ID: {name, image}} 的形式")
    require_refs = cfg.get("require_character_refs", True)

    segs = cfg.get("segments") or _err("project.json 里没有 segments")
    ids = set()
    for i, seg in enumerate(segs):
        sid = seg.get("id") or _err(f"第 {i + 1} 段缺少 id")
        if sid in ids:
            _err(f"段 id 重复：{sid}")
        ids.add(sid)
        pf = project_dir / (seg.get("prompt_file") or _err(f"{sid} 缺少 prompt_file"))
        if not pf.exists():
            _err(f"{sid} 的提示词文件不存在：{pf}")
        seg["_prompt_raw"] = pf.read_text("utf-8").strip()
        if not seg["_prompt_raw"]:
            _err(f"{sid} 的提示词是空的")
        d = int(seg.get("duration", 10))
        if not 4 <= d <= 15:
            _err(f"{sid} 的 duration={d}，Seedance 2.0 只支持 4–15 秒")
        seg["duration"] = d
        _resolve_mode(seg, i, sid)
        seg_chars = seg.get("characters", list(chars) if len(segs) and chars else [])
        for cid in seg_chars:
            if cid not in chars:
                _err(f"{sid} 引用了未声明的角色 {cid}（请在 characters 里声明并指定参考图）")
        seg["_characters"] = list(seg_chars)
        seg["_submit_problem"] = None
        if require_refs and not seg_chars and seg["_mode"] != "text":
            # 只拦新的付费提交；旧工程的重剪、验收、查询在途任务不受影响
            seg["_submit_problem"] = (f"{sid} 没有声明出场角色（segments[].characters），不能提交新任务。为避免角色无图可依，"
                                      f"默认要求声明；确实没有角色时，在 project.json 写 \"require_character_refs\": false")
        if seg["_mode"] == "text" and seg_chars:
            _err(f"{sid} 是纯文字模式（mode=text），但声明了角色 {seg_chars}。有角色就必须给参考图或首帧图，不能纯文字生成。")

    # 角色参考图：全部校验（缺图即停）
    def image_info(ref, what):
        if not check_images:   # 重剪/验收/查看状态：不需要参考图，缺了也不拦
            try:
                return {"path": str(resolve_image(ref, project_dir, assets, what)), "sha256": None}
            except PipelineError:
                return {"path": None, "sha256": None}
        return check_image(resolve_image(ref, project_dir, assets, what), what)

    cfg["_characters"] = {}
    for cid, c in chars.items():
        info = image_info(c.get("image"), f"角色 {cid}")
        info.update(id=cid, name=c.get("name", cid), ref=c.get("image"))
        cfg["_characters"][cid] = info

    for seg in segs:
        sid = seg["id"]
        seg["_images"] = []   # [{role, label, info}] 按发送顺序
        if seg["_mode"] == "first_frame":
            info = image_info(seg.get("first_frame"), f"{sid} 首帧")
            seg["_images"].append({"role": "first_frame", "label": "首帧", "info": info})
            _check_aspect(cfg, info, f"{sid} 首帧")
        elif seg["_mode"] == "reference":
            for cid in seg["_characters"]:
                seg["_images"].append({"role": "reference", "label": cfg["_characters"][cid]["name"],
                                       "character": cid, "info": cfg["_characters"][cid]})
            for j, r in enumerate(seg.get("reference_images") or []):
                info = image_info(r, f"{sid} 参考图{j + 1}")
                seg["_images"].append({"role": "reference", "label": f"额外参考{j + 1}", "info": info})
            if not seg["_images"]:
                _err(f"{sid} 是参考图模式，但没有任何参考图")
            if len(seg["_images"]) > 9:
                _err(f"{sid} 参考图超过 9 张（apimart 上限）")
        seg["_prompt"] = _compose_prompt(cfg, seg)

    cfg["_narration"] = _load_narration(cfg, ids)
    cfg["_overlay"] = _load_overlay(cfg, ids)
    return cfg


def _resolve_mode(seg, i, sid):
    mode = seg.get("mode")
    if mode is None:
        if seg.get("continue_from_previous"):
            mode = "chain"
        elif seg.get("first_frame"):
            mode = "first_frame"
        elif seg.get("reference_images") or seg.get("characters"):
            mode = "reference"
        else:
            _err(f"{sid} 没有指定 mode，也没有首帧图或参考图。"
                 f"不会自动改成纯文字生成；确实要纯文字时请写 \"mode\": \"text\"")
    if mode not in MODES:
        _err(f"{sid} 的 mode 只能是 {MODES}")
    if mode == "chain" and i == 0:
        _err("第一段不能用 chain（接上一段尾帧）")
    if mode in ("first_frame", "chain") and seg.get("reference_images"):
        _err(f"{sid}：首帧模式不能同时用 reference_images（apimart 限制：image_with_roles 和 image_urls 不能同时用）")
    if mode == "first_frame" and not seg.get("first_frame"):
        _err(f"{sid} 是首帧模式但没有 first_frame")
    seg["_mode"] = mode


def _check_aspect(cfg, info, what):
    if "width" not in info:
        return
    try:
        a, b = (float(x) for x in cfg["size"].split(":"))
    except ValueError:
        return
    want, got = a / b, info["width"] / info["height"]
    if abs(got - want) / want > 0.05:
        warn(f"{what} 的宽高比 {info['width']}x{info['height']} 和画幅 {cfg['size']} 不一致，生成结果可能被裁切或变形")


def _compose_prompt(cfg, seg):
    """参考图模式下，在提示词前加一行"图几是谁"，让每个角色对应哪张图是明确的（记录进指纹）。"""
    prompt = seg["_prompt_raw"]
    if seg["_mode"] == "reference" and cfg.get("ref_legend", True):
        items = "；".join(f"图{k + 1}＝{im['label']}" for k, im in enumerate(seg["_images"]))
        legend = (cfg.get("ref_legend_template") or DEFAULT_LEGEND).format(items=items)
        prompt = legend + "\n" + prompt
    return prompt


def segment_inputs(cfg, seg, chain_source=None):
    """本段生成输入的完整记录（写进 attempt，用来判断"输入变了没有"）。"""
    imgs = [{"role": im["role"], "label": im["label"], "character": im.get("character"),
             "path": im["info"].get("path"), "sha256": im["info"].get("sha256")} for im in seg["_images"]]
    if seg["_mode"] == "chain":
        imgs = [{"role": "first_frame", "label": "上一段尾帧", "character": None,
                 "path": chain_source and chain_source.get("path"),
                 "sha256": chain_source and chain_source.get("sha256"),
                 "from_attempt": chain_source and chain_source.get("attempt_id")}]
    chars = [{"id": cid, "name": cfg["_characters"][cid]["name"], "ref": cfg["_characters"][cid].get("ref"),
              "sha256": cfg["_characters"][cid].get("sha256"),
              "sent_to_api": seg["_mode"] == "reference"} for cid in seg["_characters"]]
    spec = {
        "model": cfg["model"], "resolution": cfg["resolution"], "size": cfg["size"],
        "generate_audio": bool(cfg["generate_audio"]), "seed": cfg.get("seed"),
        "duration": seg["duration"], "mode": seg["_mode"],
        "prompt_sha256": sha256_text(seg["_prompt"]),
        "images": [{"role": i["role"], "sha256": i["sha256"]} for i in imgs],
        "characters": [{"id": c["id"], "sha256": c["sha256"]} for c in chars],
        "pipeline": PIPELINE_VERSION,
    }
    return {
        "fingerprint": fingerprint(spec),
        "spec": spec,
        "prompt": seg["_prompt"],
        "images": imgs,
        "characters": chars,
        "config_version": cfg["_config_version"],
    }


# ---------------------------------------------------------------- 旁白与符号

BANNED_NARRATION = ["加号", "十字", "爱心", "红心", "心形", "五角星", "星星", "金星", "六六六", "666",
                    "分享", "转发", "箭头", "点赞", "关注", "评论", "收藏"]


def _load_narration(cfg, seg_ids):
    n = cfg.get("narration")
    if not n:
        return None
    banned = BANNED_NARRATION + list(n.get("banned_words") or [])
    lines, seen = [], set()
    for k, line in enumerate(n.get("lines") or []):
        lid = line.get("id") or _err(f"narration.lines 第 {k + 1} 行缺少 id")
        if lid in seen:
            _err(f"旁白 id 重复：{lid}")
        seen.add(lid)
        if line.get("segment") not in seg_ids:
            _err(f"旁白 {lid} 绑定的分段 {line.get('segment')!r} 不存在")
        text = (line.get("text") or "").strip()
        if not text:
            _err(f"旁白 {lid} 没有文字")
        hit = [w for w in banned if w in text]
        if hit:
            _err(f"旁白 {lid} 念出了符号/平台行为词 {hit}。符号只在画面出现，不能读出来；请改用\"这个\"等指代")
        start = float(line.get("start", 0))
        if start < 0:
            _err(f"旁白 {lid} 的 start 不能是负数")
        lines.append({"id": lid, "segment": line["segment"], "start": start, "text": text,
                      "voice": line.get("voice", n.get("voice", "zh-CN-XiaoyiNeural")),
                      "rate": line.get("rate", n.get("rate", "+0%")),
                      "pitch": line.get("pitch", n.get("pitch", "+0Hz")),
                      "audio": line.get("audio"), "words": line.get("words")})
    provider = n.get("provider", "edge")
    if provider not in ("edge", "file", "mock"):
        _err("narration.provider 只能是 edge / file / mock")
    return {"provider": provider, "volume": float(n.get("volume", 1.0)), "lines": lines}


def _load_overlay(cfg, seg_ids):
    o = cfg.get("overlay")
    if not o:
        return None
    enabled = list(o.get("enabled_symbols") or [])
    for s in enabled:
        if s not in SYMBOLS:
            _err(f"overlay.enabled_symbols 里的 {s!r} 不认识，只支持 {SYMBOLS}")
    narr_ids = {l["id"] for l in (cfg["_narration"] or {}).get("lines", [])} if cfg.get("_narration") else set()
    events, seen = [], set()
    for k, e in enumerate(o.get("events") or []):
        eid = e.get("id") or f"event{k + 1}"
        if eid in seen:
            _err(f"符号事件 id 重复：{eid}")
        seen.add(eid)
        sym = e.get("symbol")
        if sym not in enabled:
            _err(f"符号事件 {eid} 用了 {sym!r}，但它不在本条视频的 enabled_symbols 里")
        if e.get("segment") not in seg_ids:
            _err(f"符号事件 {eid} 绑定的分段 {e.get('segment')!r} 不存在")
        anchor = e.get("anchor") or {}
        if "time" in anchor:
            kind = "manual_time"
        elif anchor.get("text"):
            kind = "word"
            if e.get("narration") not in narr_ids:
                _err(f"符号事件 {eid} 用词定位，但绑定的旁白 {e.get('narration')!r} 不存在")
            if e["narration"] and next(l for l in cfg["_narration"]["lines"] if l["id"] == e["narration"])["segment"] != e["segment"]:
                _err(f"符号事件 {eid} 的分段和它绑定的旁白不在同一段")
            if int(anchor.get("occurrence", 1)) < 1:
                _err(f"符号事件 {eid} 的 occurrence 从 1 开始数")
        elif "word_index" in anchor:
            kind = "word_index"
            if e.get("narration") not in narr_ids:
                _err(f"符号事件 {eid} 用词序号定位，但绑定的旁白 {e.get('narration')!r} 不存在")
        else:
            _err(f"符号事件 {eid} 没有 anchor（用 {{\"text\": \"这个\", \"occurrence\": 2}} 或 {{\"time\": 3.2}}）")
        pos = {**(o.get("position") or {"x": 0.5, "y": 0.3}), **(e.get("position") or {})}
        if not (0 <= pos["x"] <= 1 and 0 <= pos["y"] <= 1):
            _err(f"符号事件 {eid} 的位置 x/y 必须在 0～1 之间（相对画面宽高）")
        events.append({"id": eid, "symbol": sym, "segment": e["segment"], "narration": e.get("narration"),
                       "anchor": anchor, "anchor_kind": kind, "end_anchor": e.get("end_anchor"),
                       "lead": float(e.get("lead", o.get("lead", 0.0))),
                       "hold": float(e.get("hold", o.get("hold", 0.8))),
                       "position": pos, "size": float(e.get("size", o.get("size", 0.24))),
                       "fade": float(e.get("fade", o.get("fade", 0.12)))})
    return {"enabled": enabled, "events": events, "require_all_enabled": o.get("require_all_enabled", True),
            "assets_dir": o.get("assets_dir")}
