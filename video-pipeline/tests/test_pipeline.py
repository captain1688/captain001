"""离线测试：不读取真实密钥、不调用收费接口。所有"apimart"请求都打到本机模拟服务器。

运行：python -m unittest discover -s video-pipeline/tests -v     （在仓库根目录）
需要：ffmpeg / ffprobe。耗时约 2～4 分钟。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPE = HERE.parent
sys.path.insert(0, str(PIPE))
sys.path.insert(0, str(HERE))

from mock_apimart import MockApimart, make_video  # noqa: E402
from vpipe import config, narration  # noqa: E402

MAKE = PIPE / "make_video.py"
FAKE_KEY = "offline-test-key-not-real"


def png(path, color):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:s=480x854",
                    "-frames:v", "1", str(path)], check=True)


class Base(unittest.TestCase):
    behaviors = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vp_test_"))
        self.mock = MockApimart(self.tmp / "mock", self.behaviors).start()
        self.cache = self.tmp / "cache"
        self.music = self.tmp / "music"

    def tearDown(self):
        self.mock.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_project(self, name="p1", *, price=0.1, limit=100.0, max_repairs=0, segments=None, characters=True,
                     batch=None, narration_cfg=None, overlay=None, edit=None, extra=None):
        d = self.tmp / name
        (d / "refs").mkdir(parents=True)
        png(d / "refs" / "hero.png", "orange")
        png(d / "refs" / "s1.png", "skyblue")
        png(d / "refs" / "s2.png", "pink")
        segs = segments or [
            {"id": "seg1", "prompt_file": "seg1.txt", "duration": 4, "mode": "first_frame", "first_frame": "refs/s1.png",
             "characters": ["hero"] if characters else []},
            {"id": "seg2", "prompt_file": "seg2.txt", "duration": 4, "mode": "first_frame", "first_frame": "refs/s2.png",
             "characters": ["hero"] if characters else []},
        ]
        for s in segs:
            (d / s["prompt_file"]).write_text(f"测试提示词 {s['id']}，全程无背景音乐。", "utf-8")
        cfg = {"title": name, "model": "seedance-2.0-mini", "resolution": "480p", "size": "9:16",
               "generate_audio": True, "segments": segs, "edit": edit or {"bgm": None}}
        if characters:
            cfg["characters"] = {"hero": {"name": "测试主角", "image": "refs/hero.png"}}
        else:
            cfg["require_character_refs"] = False
        if batch:
            cfg["batch"] = os.path.relpath(batch, d)
        else:
            cfg["budget"] = {"limit": limit, "currency": "CNY", "max_repairs_per_segment": max_repairs}
            cfg["pricing"] = [{"model": "seedance-2.0-mini", "resolution": "480p", "unit": "second",
                               "price": price, "currency": "CNY", "source": "测试用假价格"}]
        if narration_cfg:
            cfg["narration"] = narration_cfg
        if overlay:
            cfg["overlay"] = overlay
        cfg.update(extra or {})
        (d / "project.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")
        return d

    def env(self, **extra):
        e = dict(os.environ)
        e.pop("APIMART_API_KEY", None)   # 绝不把真实密钥带进测试
        e.update(APIMART_API_BASE=self.mock.api_base, APIMART_API_KEY=FAKE_KEY,
                 VP_CACHE_DIR=str(self.cache), VP_MUSIC_DIR=str(self.music), PYTHONIOENCODING="utf-8")
        e.pop("VP_TEST_CRASH_AT", None)
        e.update(extra)
        return e

    def run_pipe(self, project, *args, env=None, timeout=240):
        r = subprocess.run([sys.executable, str(MAKE), str(project), *args, "--poll-interval", "1", "--max-wait", "60"],
                           capture_output=True, text=True, env=env or self.env(), timeout=timeout)
        r.out = r.stdout + r.stderr
        self.assertNotIn(FAKE_KEY, r.out, "输出里不应出现密钥")
        return r

    def state(self, project):
        return json.loads((project / "output" / "state.json").read_text("utf-8"))

    def ledger(self, project):
        return json.loads((project / "output" / "budget.ledger.json").read_text("utf-8"))


# ---------------------------------------------------------------- 1 参考图

class TestReferenceImages(Base):
    def test_missing_image_stops_without_any_request(self):
        p = self.make_project()
        (p / "refs" / "hero.png").unlink()
        r = self.run_pipe(p)
        self.assertEqual(r.returncode, 1, r.out)
        self.assertIn("不存在", r.out)
        self.assertIn("不会改成纯文字生成", r.out)
        self.assertEqual(self.mock.calls, [])

    def test_undecodable_image_stops(self):
        p = self.make_project()
        (p / "refs" / "s1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 50)
        r = self.run_pipe(p)
        self.assertEqual(r.returncode, 1, r.out)
        self.assertIn("无法正常解码", r.out)
        self.assertEqual(self.mock.calls, [])

    def test_asset_manifest_and_missing_key(self):
        p = self.make_project()
        cfg = json.loads((p / "project.json").read_text("utf-8"))
        cfg["characters"]["hero"]["image"] = "asset:hero_main"
        (p / "project.json").write_text(json.dumps(cfg, ensure_ascii=False), "utf-8")
        r = self.run_pipe(p, "--dry-run")
        self.assertEqual(r.returncode, 1, r.out)
        self.assertIn("本地素材清单里没有这个 key", r.out)
        outside = self.tmp / "private_assets"
        outside.mkdir()
        png(outside / "hero_private.png", "purple")
        (p / "assets.local.json").write_text(json.dumps({"assets": {"hero_main": str(outside / "hero_private.png")}}), "utf-8")
        r = self.run_pipe(p, "--dry-run")
        self.assertEqual(r.returncode, 0, r.out)
        self.assertIn("hero_private.png", r.out)
        self.assertEqual(self.mock.calls, [])

    def test_legacy_project_without_characters_only_blocks_paid_submit(self):
        p = self.make_project()
        cfg = json.loads((p / "project.json").read_text("utf-8"))
        cfg.pop("characters")
        for sg in cfg["segments"]:
            sg.pop("characters")
        (p / "project.json").write_text(json.dumps(cfg, ensure_ascii=False), "utf-8")
        r = self.run_pipe(p, "--status")
        self.assertEqual(r.returncode, 0, r.out)
        r = self.run_pipe(p, "--dry-run")
        self.assertEqual(r.returncode, 2, r.out)
        self.assertIn("没有声明出场角色", r.out)
        r = self.run_pipe(p)
        self.assertEqual(r.returncode, 2, r.out)
        self.assertIn("没有声明出场角色", r.out)
        self.assertEqual(self.mock.submits(), [])

    def test_upload_failure_is_clean_error_without_submit(self):
        p = self.make_project()
        env = self.env(APIMART_API_BASE="http://127.0.0.1:9/v1")   # 端口 9 无服务：连接被拒
        r = self.run_pipe(p, "--skip-edit", env=env)
        self.assertEqual(r.returncode, 2, r.out)
        self.assertIn("上传图片", r.out)
        self.assertNotIn("Traceback", r.out)
        self.assertNotIn("attempts", json.dumps(self.state(p)["segments"].get("seg1", {}).get("attempts", [])))

    def test_text_mode_with_characters_is_refused(self):
        p = self.make_project(segments=[{"id": "seg1", "prompt_file": "seg1.txt", "duration": 4, "mode": "text",
                                         "characters": ["hero"]}])
        r = self.run_pipe(p, "--dry-run")
        self.assertEqual(r.returncode, 1, r.out)
        self.assertIn("不能纯文字生成", r.out)

    def test_reference_mode_legend_maps_each_character(self):
        p = self.make_project(segments=[{"id": "seg1", "prompt_file": "seg1.txt", "duration": 4, "mode": "reference",
                                         "characters": ["hero"], "reference_images": ["refs/s1.png"]}])
        r = self.run_pipe(p, "--skip-edit")
        self.assertEqual(r.returncode, 0, r.out)
        sub = self.mock.submits()[0]["payload"]
        self.assertEqual(len(sub["image_urls"]), 2)
        self.assertNotIn("image_with_roles", sub)
        self.assertTrue(sub["prompt"].startswith("【参考图对应】图1＝测试主角；图2＝额外参考1。"))
        att = self.state(p)["segments"]["seg1"]["attempts"][0]
        self.assertEqual(att["inputs"]["characters"][0]["id"], "hero")
        self.assertTrue(att["inputs"]["characters"][0]["sent_to_api"])
        self.assertEqual(len(att["inputs"]["characters"][0]["sha256"]), 64)

    def test_reference_change_is_not_treated_as_done(self):
        p = self.make_project(max_repairs=1)
        r = self.run_pipe(p, "--skip-edit")
        self.assertEqual(r.returncode, 0, r.out)
        self.assertEqual(len(self.mock.submits()), 2)
        png(p / "refs" / "hero.png", "green")          # 角色参考图换了
        r = self.run_pipe(p, "--skip-edit")
        self.assertEqual(r.returncode, 2, r.out)
        self.assertIn("输入已变化（角色参考图）", r.out)
        self.assertEqual(len(self.mock.submits()), 2, "参考图变了也不能自动付费重做")
        r = self.run_pipe(p, "--edit-only")
        self.assertEqual(r.returncode, 0, r.out)        # 旧素材仍可重剪（不触发生成）
        r = self.run_pipe(p, "--skip-edit", "--allow-regenerate", "seg1")
        self.assertEqual(r.returncode, 2, r.out)        # seg2 仍未授权
        self.assertEqual(len(self.mock.submits()), 3)
        atts = self.state(p)["segments"]["seg1"]["attempts"]
        self.assertEqual([a["origin"] for a in atts], ["initial", "regenerate"])
        self.assertNotEqual(atts[0]["fingerprint"], atts[1]["fingerprint"])


# ---------------------------------------------------------------- 2 预算

class TestBudget(Base):
    def test_unknown_price_stops_before_submit(self):
        p = self.make_project(price=None)
        r = self.run_pipe(p, "--dry-run")
        self.assertEqual(r.returncode, 2, r.out)
        self.assertIn("价格不明", r.out)
        r = self.run_pipe(p)
        self.assertEqual(r.returncode, 2, r.out)
        self.assertIn("价格不明", r.out)
        self.assertEqual(self.mock.submits(), [])

    def test_missing_budget_refuses_submit(self):
        p = self.make_project()
        cfg = json.loads((p / "project.json").read_text("utf-8"))
        cfg.pop("budget")
        (p / "project.json").write_text(json.dumps(cfg, ensure_ascii=False), "utf-8")
        r = self.run_pipe(p)
        self.assertEqual(r.returncode, 2, r.out)
        self.assertIn("没有配置预算", r.out)
        self.assertEqual(self.mock.submits(), [])

    def test_over_budget_inside_one_project(self):
        p = self.make_project(price=1.0, limit=6.0)     # 每段 4 元，两段 8 元 > 6 元
        r = self.run_pipe(p, "--skip-edit")
        self.assertEqual(r.returncode, 2, r.out)
        self.assertIn("超出预算", r.out)
        self.assertEqual(len(self.mock.submits()), 1)
        led = self.ledger(p)
        total = sum(e["estimate"] for e in led["entries"].values() if e["state"] != "released")
        self.assertLessEqual(total, 6.0)

    def test_legacy_inflight_task_counts_against_budget(self):
        p = self.make_project(price=1.0, limit=6.0, segments=[
            {"id": "seg1", "prompt_file": "seg1.txt", "duration": 4, "mode": "first_frame", "first_frame": "refs/s1.png",
             "characters": ["hero"]},
            {"id": "seg2", "prompt_file": "seg2.txt", "duration": 4, "mode": "first_frame", "first_frame": "refs/s2.png",
             "characters": ["hero"]}])
        out = p / "output"
        out.mkdir()
        # 旧版状态：seg1 已提交但没有账本预占（模拟升级前就在跑的任务）
        (out / "state.json").write_text(json.dumps({"uploads": {}, "segments": {
            "seg1": {"task_id": "task_unknown_to_mock", "status": "submitted"}}}), "utf-8")
        r = self.run_pipe(p, "--skip-edit", "--max-wait", "3")
        self.assertIn("在途预占 4", r.out)
        self.assertIn("超出预算", r.out)
        self.assertEqual(self.mock.submits(), [], "旧版在途任务也要占预算")

    def test_concurrent_projects_share_one_budget(self):
        self.mock.behaviors = [{"kind": "ok", "post_delay": 1.5}] * 4
        batch = self.tmp / "batch.json"
        batch.write_text(json.dumps({"batch_id": "t", "budget": {"limit": 6.0, "currency": "CNY"},
                                     "pricing": [{"model": "seedance-2.0-mini", "resolution": "480p", "unit": "second",
                                                  "price": 1.0, "currency": "CNY"}]}), "utf-8")
        one = [{"id": "seg1", "prompt_file": "seg1.txt", "duration": 4, "mode": "first_frame",
                "first_frame": "refs/s1.png", "characters": ["hero"]}]
        pa = self.make_project("pa", batch=batch, segments=one)
        pb = self.make_project("pb", batch=batch, segments=[dict(one[0])])
        procs = [subprocess.Popen([sys.executable, str(MAKE), str(x), "--skip-edit", "--poll-interval", "1", "--max-wait", "60"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=self.env())
                 for x in (pa, pb)]
        outs = [pr.communicate(timeout=240)[0] for pr in procs]
        self.assertEqual(len(self.mock.submits()), 1, outs)
        self.assertEqual(sum("超出预算" in o for o in outs), 1, outs)
        led = json.loads((self.tmp / "batch.ledger.json").read_text("utf-8"))
        total = sum(e["estimate"] for e in led["entries"].values() if e["state"] != "released")
        self.assertLessEqual(total, 6.0)


# ---------------------------------------------------------------- 3 防重复扣费

class TestNoDoubleCharge(Base):
    def test_duplicate_start_is_refused(self):
        self.mock.behaviors = [{"kind": "ok", "post_delay": 3}] * 2
        p = self.make_project()
        first = subprocess.Popen([sys.executable, str(MAKE), str(p), "--skip-edit", "--poll-interval", "1", "--max-wait", "60"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=self.env())
        time.sleep(1.5)
        r = self.run_pipe(p, "--skip-edit")
        first.communicate(timeout=240)
        self.assertEqual(r.returncode, 1, r.out)
        self.assertIn("另一个进程正在处理这个工程", r.out)
        self.assertEqual(len(self.mock.submits()), 2)
        self.assertEqual(first.returncode, 0)

    def test_crash_while_writing_state_keeps_valid_file(self):
        p = self.make_project(segments=[{"id": "seg1", "prompt_file": "seg1.txt", "duration": 4, "mode": "first_frame",
                                         "first_frame": "refs/s1.png", "characters": ["hero"]}])
        # 写入顺序：恢复后保存(1) 上传缓存(2) 账本预占(3) 提交意图(4) → POST → 保存 task_id(5)。第 5 次写时断电
        r = self.run_pipe(p, "--skip-edit", env=self.env(VP_TEST_CRASH_AT="state_write_before_replace", VP_TEST_CRASH_SKIP="4"))
        self.assertEqual(r.returncode, 77, r.out)
        S = self.state(p)                                     # 能解析：不是写了一半的坏文件
        self.assertEqual(S["segments"]["seg1"]["attempts"][0]["status"], "intent")
        self.assertEqual(len(self.mock.submits()), 1)
        r = self.run_pipe(p, "--skip-edit")
        self.assertEqual(r.returncode, 2, r.out)
        self.assertIn("状态不明", r.out)
        self.assertEqual(len(self.mock.submits()), 1, "断电后重启不能自动重投")

    def test_submit_ok_but_id_not_saved_then_manual_resolve(self):
        p = self.make_project(segments=[{"id": "seg1", "prompt_file": "seg1.txt", "duration": 4, "mode": "first_frame",
                                         "first_frame": "refs/s1.png", "characters": ["hero"]}])
        r = self.run_pipe(p, "--skip-edit", env=self.env(VP_TEST_CRASH_AT="after_post_before_save"))
        self.assertEqual(r.returncode, 77, r.out)
        self.assertEqual(len(self.mock.submits()), 1)
        r = self.run_pipe(p, "--skip-edit")
        self.assertEqual(r.returncode, 2, r.out)
        self.assertIn("服务端可能已接单", r.out)
        att = self.state(p)["segments"]["seg1"]["attempts"][0]
        self.assertEqual(att["status"], "unknown")
        self.assertEqual(self.ledger(p)["entries"][att["reservation"]["key"]]["state"], "reserved",
                         "状态不明时预占不能释放")
        r = self.run_pipe(p, "--skip-edit", "--authorize-retry", "seg1")
        self.assertEqual(len(self.mock.submits()), 1, "状态不明时即使授权重试也不能重投")
        r = self.run_pipe(p, "--resolve", "seg1", "--attempt", att["attempt_id"], "--task-id", "task_mock_1")
        self.assertEqual(r.returncode, 0, r.out)
        r = self.run_pipe(p, "--skip-edit")
        self.assertEqual(r.returncode, 0, r.out)
        self.assertEqual(len(self.mock.submits()), 1)
        att = self.state(p)["segments"]["seg1"]["attempts"][0]
        self.assertEqual(att["status"], "completed")
        self.assertEqual(self.ledger(p)["entries"][att["reservation"]["key"]]["state"], "spent")

    def test_ambiguous_502_then_confirm_not_created(self):
        self.mock.behaviors = [{"kind": "accept_then_502"}, {"kind": "ok"}]
        p = self.make_project(max_repairs=1, segments=[{"id": "seg1", "prompt_file": "seg1.txt", "duration": 4,
                                                        "mode": "first_frame", "first_frame": "refs/s1.png",
                                                        "characters": ["hero"]}])
        r = self.run_pipe(p, "--skip-edit")
        self.assertEqual(r.returncode, 2, r.out)
        att = self.state(p)["segments"]["seg1"]["attempts"][0]
        self.assertEqual(att["status"], "unknown")
        r = self.run_pipe(p, "--resolve", "seg1", "--attempt", att["attempt_id"], "--not-created")
        self.assertEqual(r.returncode, 0, r.out)
        self.assertEqual(self.ledger(p)["entries"][att["reservation"]["key"]]["state"], "released")
        r = self.run_pipe(p, "--skip-edit")
        self.assertEqual(len(self.mock.submits()), 1, "确认未创建后也要明确授权才重投")
        r = self.run_pipe(p, "--skip-edit", "--authorize-retry", "seg1")
        self.assertEqual(r.returncode, 0, r.out)
        self.assertEqual(len(self.mock.submits()), 2)

    def test_failed_task_needs_authorization_and_respects_repair_limit(self):
        self.mock.behaviors = [{"kind": "fail"}, {"kind": "fail"}, {"kind": "ok"}]
        p = self.make_project(max_repairs=1, segments=[{"id": "seg1", "prompt_file": "seg1.txt", "duration": 4,
                                                        "mode": "first_frame", "first_frame": "refs/s1.png",
                                                        "characters": ["hero"]}])
        r = self.run_pipe(p, "--skip-edit")
        self.assertEqual(r.returncode, 2, r.out)
        self.assertIn("不会自动返工", r.out)
        r = self.run_pipe(p, "--skip-edit")
        self.assertEqual(len(self.mock.submits()), 1, "未授权不能重试")
        r = self.run_pipe(p, "--skip-edit", "--authorize-retry", "seg1")
        self.assertEqual(len(self.mock.submits()), 2)
        atts = self.state(p)["segments"]["seg1"]["attempts"]
        self.assertEqual([a["status"] for a in atts], ["failed", "failed"])
        self.assertEqual(atts[0]["task_id"], "task_mock_1", "失败记录必须保留原 task_id")
        self.assertNotEqual(atts[0]["attempt_id"], atts[1]["attempt_id"])
        r = self.run_pipe(p, "--skip-edit", "--authorize-retry", "seg1")
        self.assertIn("修复次数已达上限", r.out)
        self.assertEqual(len(self.mock.submits()), 2)

    def test_rejected_submit_releases_reservation(self):
        self.mock.behaviors = [{"kind": "reject"}]
        p = self.make_project(segments=[{"id": "seg1", "prompt_file": "seg1.txt", "duration": 4, "mode": "first_frame",
                                         "first_frame": "refs/s1.png", "characters": ["hero"]}])
        r = self.run_pipe(p, "--skip-edit")
        self.assertEqual(r.returncode, 2, r.out)
        att = self.state(p)["segments"]["seg1"]["attempts"][0]
        self.assertEqual(att["status"], "rejected")
        self.assertEqual(self.ledger(p)["entries"][att["reservation"]["key"]]["state"], "released")

    def test_legacy_v1_state_is_migrated(self):
        p = self.make_project(segments=[{"id": "seg1", "prompt_file": "seg1.txt", "duration": 4, "mode": "first_frame",
                                         "first_frame": "refs/s1.png", "characters": ["hero"]}])
        out = p / "output"
        out.mkdir()
        make_video(out / "seg1.mp4", 4)
        (out / "state.json").write_text(json.dumps({"uploads": {}, "segments": {
            "seg1": {"task_id": "task_old", "status": "completed", "video": "seg1.mp4"}}}), "utf-8")
        r = self.run_pipe(p)
        self.assertIn(r.returncode, (0, 3), r.out)
        self.assertEqual(self.mock.submits(), [], "旧版已完成的段不应重新生成")
        S = self.state(p)
        self.assertEqual(S["schema"], "vp-state/2")
        rep = json.loads((out / "qc" / "report.json").read_text("utf-8"))
        self.assertTrue(any("旧版记录没有保存输入指纹" in m for m in rep["manual_pending"]))


# ---------------------------------------------------------------- 4 旁白与符号时间轴

NARR = {"provider": "mock", "voice": "test", "lines": [
    {"id": "n1", "segment": "seg1", "start": 0.3, "text": "我有这个、这个，还有这个"}]}


class TestNarrationTimeline(Base):
    def _cfg(self, events, enabled=("plus", "heart", "star")):
        p = self.make_project(narration_cfg=NARR, overlay={"enabled_symbols": list(enabled), "events": events})
        return config.load_project(p)

    def test_multiple_zhege_occurrences_map_to_distinct_words(self):
        os.environ["VP_CACHE_DIR"] = str(self.cache)
        try:
            cfg = self._cfg([
                {"id": "e1", "symbol": "plus", "segment": "seg1", "narration": "n1", "anchor": {"text": "这个", "occurrence": 1}},
                {"id": "e2", "symbol": "heart", "segment": "seg1", "narration": "n1", "anchor": {"text": "这个", "occurrence": 2}},
                {"id": "e3", "symbol": "star", "segment": "seg1", "narration": "n1", "anchor": {"text": "这个", "occurrence": 3}}])
            narr = narration.synthesize(cfg, allow_tts=True)
            offsets = {"seg1": 0.0, "seg2": 4.0}
            _, events, probs = narration.resolve_timeline(cfg, narr, offsets, {"seg1": 4.0, "seg2": 4.0})
        finally:
            os.environ.pop("VP_CACHE_DIR")
        words = narr["n1"]["words"]
        text = "".join(w["text"] for w in words)
        expect = []
        start = 0
        for _ in range(3):
            i = text.index("这个", start)
            expect.append(round(0.3 + words[i]["offset"], 3))
            start = i + 2
        self.assertEqual([e["word_time"] for e in events], expect)
        self.assertEqual(len(set(expect)), 3)
        self.assertTrue(all(e["alignment"] == "word" for e in events))
        self.assertEqual([lvl for lvl, _ in probs if lvl == "error"], [])

    def test_occurrence_beyond_count_is_reported_not_guessed(self):
        os.environ["VP_CACHE_DIR"] = str(self.cache)
        try:
            cfg = self._cfg([{"id": "e4", "symbol": "plus", "segment": "seg1", "narration": "n1",
                              "anchor": {"text": "这个", "occurrence": 4}}], enabled=("plus",))
            narr = narration.synthesize(cfg, allow_tts=True)
            _, events, probs = narration.resolve_timeline(cfg, narr, {"seg1": 0.0, "seg2": 4.0}, {"seg1": 4.0, "seg2": 4.0})
        finally:
            os.environ.pop("VP_CACHE_DIR")
        self.assertEqual(events[0]["alignment"], "unaligned")
        self.assertNotIn("start", events[0])
        self.assertTrue(any("只出现了 3 次" in m for lvl, m in probs if lvl == "error"))

    def test_symbol_names_in_narration_are_refused(self):
        bad = {"provider": "mock", "lines": [{"id": "n1", "segment": "seg1", "text": "快给我一个爱心"}]}
        p = self.make_project(narration_cfg=bad)
        r = self.run_pipe(p, "--dry-run")
        self.assertEqual(r.returncode, 1, r.out)
        self.assertIn("符号只在画面出现，不能读出来", r.out)

    def test_no_word_timestamps_is_flagged(self):
        words = self.tmp / "w.json"
        words.write_text("[]", "utf-8")
        audio = self.tmp / "a.mp3"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=1.5", str(audio)], check=True)
        n = {"provider": "file", "lines": [{"id": "n1", "segment": "seg1", "start": 0.2, "text": "看这个",
                                            "audio": str(audio), "words": str(words)}]}
        p = self.make_project(narration_cfg=n, overlay={"enabled_symbols": ["plus"], "events": [
            {"id": "e1", "symbol": "plus", "segment": "seg1", "narration": "n1", "anchor": {"text": "这个"}}]})
        r = self.run_pipe(p)
        self.assertEqual(r.returncode, 1, r.out)
        self.assertIn("没有返回词级时间戳", r.out)
        self.assertIn("不会假装已精准对齐", r.out)

    def test_full_render_with_narration_symbols_bgm_and_qc(self):
        (self.music / "欢快").mkdir(parents=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=220:d=3",
                        str(self.music / "欢快" / "t.mp3")], check=True)
        events = [
            {"id": "e1", "symbol": "plus", "segment": "seg1", "narration": "n1", "anchor": {"text": "这个", "occurrence": 1},
             "position": {"x": 0.25, "y": 0.3}},
            {"id": "e2", "symbol": "heart", "segment": "seg1", "narration": "n1", "anchor": {"text": "这个", "occurrence": 2},
             "position": {"x": 0.5, "y": 0.3}},
            {"id": "e3", "symbol": "star", "segment": "seg1", "narration": "n1", "anchor": {"text": "这个", "occurrence": 3},
             "position": {"x": 0.75, "y": 0.3}},
            {"id": "e4", "symbol": "666", "segment": "seg2", "narration": "n2", "anchor": {"text": "厉害"}},
            {"id": "e5", "symbol": "share", "segment": "seg2", "narration": "n2", "anchor": {"text": "朋友"},
             "position": {"x": 0.8, "y": 0.75}}]
        narr = {"provider": "mock", "lines": [NARR["lines"][0],
                                              {"id": "n2", "segment": "seg2", "start": 0.4, "text": "真厉害，带上好朋友"}]}
        p = self.make_project(narration_cfg=narr, edit={"bgm": "auto", "bgm_mood": "欢快"},
                              overlay={"enabled_symbols": ["plus", "heart", "star", "666", "share"], "events": events})
        r = self.run_pipe(p)
        self.assertIn(r.returncode, (0, 3), r.out)
        out = p / "output"
        tl = json.loads((out / "timeline.json").read_text("utf-8"))
        self.assertEqual({e["symbol"] for e in tl["symbol_events"]}, {"plus", "heart", "star", "666", "share"})
        self.assertTrue(all(e["alignment"] == "word" for e in tl["symbol_events"]))
        e4 = next(e for e in tl["symbol_events"] if e["id"] == "e4")
        self.assertGreaterEqual(e4["start"], 4.0, "第二段的事件要加上第一段的偏移")
        rep = json.loads((out / "qc" / "report.json").read_text("utf-8"))
        self.assertTrue((out / "qc" / "final_symbols.png").exists())
        self.assertTrue((out / "qc" / "seg1_contact.png").exists())
        self.assertTrue(rep["manual_pending"])
        # 符号真的画上去了：事件时刻的成片画面与原片段同一时刻不同
        e1 = next(e for e in tl["symbol_events"] if e["id"] == "e1")
        t = (e1["start"] + e1["end"]) / 2

        def crop(path, ts):
            return subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{ts:.3f}", "-i", str(path), "-frames:v", "1",
                                   "-vf", "crop=100:100:70:206", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                                  capture_output=True).stdout
        a, b = crop(out / "final.mp4", t), crop(out / "seg1.mp4", t)
        diff = sum(abs(x - y) for x, y in zip(a, b)) / max(len(a), 1)
        self.assertGreater(diff, 20, "叠加位置应能看到符号")


# ---------------------------------------------------------------- 5 混合音轨 / 独立重剪

class TestAudioAndEdit(Base):
    behaviors = [{"kind": "ok"}, {"kind": "ok_silent"}]

    def test_mixed_audio_keeps_sound_and_fills_silence(self):
        p = self.make_project()
        r = self.run_pipe(p)
        self.assertIn(r.returncode, (0, 3), r.out)
        out = p / "output"
        tl = json.loads((out / "timeline.json").read_text("utf-8"))
        self.assertEqual([s["had_audio"] for s in tl["segments"]], [True, False])

        def vol(ss, t):
            r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(ss), "-t", str(t), "-i", str(out / "final.mp4"),
                                "-af", "volumedetect", "-vn", "-f", "null", "-"], capture_output=True, text=True)
            return float(r.stderr.split("mean_volume:")[1].split("dB")[0])
        self.assertGreater(vol(0.5, 3), -40, "有声片段的原声必须保留")
        self.assertLess(vol(4.5, 3), -60, "无声片段只补静音")
        self.assertAlmostEqual(tl["total"], 8.0, delta=0.2)

    def test_edit_only_never_calls_api_or_tts(self):
        p = self.make_project(narration_cfg={"provider": "mock", "lines": [
            {"id": "n1", "segment": "seg1", "start": 0.2, "text": "你好呀"}]})
        r = self.run_pipe(p, "--skip-edit")
        self.assertEqual(r.returncode, 0, r.out)
        before = list(self.mock.calls)
        self.mock.stop()
        try:
            r = self.run_pipe(p, "--edit-only")
            self.assertEqual(r.returncode, 1, r.out)
            self.assertIn("独立重剪只使用已有素材", r.out)
            r = self.run_pipe(p, "--tts-only")
            self.assertEqual(r.returncode, 0, r.out)
            r = self.run_pipe(p, "--edit-only")
            self.assertIn(r.returncode, (0, 3), r.out)
            self.assertTrue((p / "output" / "final.mp4").exists())
        finally:
            self.mock.start()
        self.assertEqual(self.mock.calls[:len(before)], before)

    def test_dry_run_needs_no_key_and_no_network(self):
        p = self.make_project()
        env = self.env()
        env.pop("APIMART_API_KEY")
        env["APIMART_API_BASE"] = "http://127.0.0.1:9"
        r = self.run_pipe(p, "--dry-run", env=env)
        self.assertEqual(r.returncode, 0, r.out)
        self.assertIn("没有调用任何付费接口", r.out)
        self.assertIn("预估 0.4 CNY", r.out)


class TestMakeRefs(Base):
    def _project(self, image_price=0.05, max_repairs=0):
        segs = [{"id": "seg1", "prompt_file": "seg1.txt", "duration": 4, "mode": "reference",
                 "characters": ["hero", "girl"]}]
        p = self.make_project(segments=segs, max_repairs=max_repairs)
        cfg = json.loads((p / "project.json").read_text("utf-8"))
        cfg["characters"]["girl"] = {"name": "人类小女孩", "image": "refs/girl.png",
                                     "generate": {"model": "seedream-4.5", "size": "3:4", "resolution": "2K",
                                                  "prompt": "角色设定图，棕色双马尾小女孩，纯白背景"}}
        cfg["pricing"].append({"model": "seedream-4.5", "unit": "image", "price": image_price, "currency": "CNY"})
        (p / "project.json").write_text(json.dumps(cfg, ensure_ascii=False), "utf-8")
        return p

    def test_missing_generated_ref_blocks_video_until_made(self):
        p = self._project()
        r = self.run_pipe(p)
        self.assertEqual(r.returncode, 1, r.out)
        self.assertIn("--make-refs girl", r.out)
        self.assertEqual(self.mock.calls, [])
        r = self.run_pipe(p, "--dry-run")
        self.assertEqual(r.returncode, 2, r.out)
        self.assertIn("需要生成，预估 0.05 CNY", r.out)
        r = self.run_pipe(p, "--make-refs")
        self.assertEqual(r.returncode, 0, r.out)
        self.assertTrue((p / "refs" / "girl.png").exists())
        self.assertEqual(len(self.mock.image_submits()), 1)
        self.assertEqual(self.mock.submits(), [], "生成角色图不能顺带生成视频")
        led = self.ledger(p)
        self.assertEqual([e["state"] for e in led["entries"].values()], ["spent"])
        r = self.run_pipe(p, "--make-refs")
        self.assertEqual(len(self.mock.image_submits()), 1, "已有角色图不能重复生成")
        r = self.run_pipe(p, "--skip-edit")
        self.assertEqual(r.returncode, 0, r.out)
        sub = self.mock.submits()[0]["payload"]
        self.assertEqual(len(sub["image_urls"]), 2)
        self.assertIn("图2＝人类小女孩", sub["prompt"])

    def test_make_refs_unknown_price_refuses(self):
        p = self._project(image_price=None)
        r = self.run_pipe(p, "--make-refs", "girl")
        self.assertEqual(r.returncode, 2, r.out)
        self.assertIn("价格不明", r.out)
        self.assertEqual(self.mock.image_submits(), [])

    def test_make_refs_never_overwrites_without_permission(self):
        p = self._project(max_repairs=1)
        png(p / "refs" / "girl.png", "brown")          # 用户自己放了图
        before = (p / "refs" / "girl.png").read_bytes()
        r = self.run_pipe(p, "--make-refs")
        self.assertEqual(r.returncode, 0, r.out)
        self.assertEqual(self.mock.image_submits(), [])
        self.assertEqual((p / "refs" / "girl.png").read_bytes(), before)
        r = self.run_pipe(p, "--make-refs", "girl", "--allow-regenerate", "girl")
        self.assertEqual(r.returncode, 0, r.out)
        self.assertEqual(len(self.mock.image_submits()), 1, "同一次运行只重画一次")
        self.assertNotEqual((p / "refs" / "girl.png").read_bytes(), before)
        olds = list((p / "refs" / "_old").glob("girl.*.png"))
        self.assertEqual(len(olds), 1)
        self.assertEqual(olds[0].read_bytes(), before, "旧图保留，不删除")

    def test_make_refs_ambiguous_blocks_and_resolves(self):
        self.mock.image_behaviors = [{"kind": "accept_then_502"}]
        p = self._project()
        r = self.run_pipe(p, "--make-refs")
        self.assertEqual(r.returncode, 2, r.out)
        self.assertIn("状态不明", r.out)
        r = self.run_pipe(p, "--make-refs", "--authorize-retry", "girl")
        self.assertEqual(len(self.mock.image_submits()), 1, "状态不明时不能重投")
        att = self.state(p)["refs"]["girl"]["attempts"][0]
        r = self.run_pipe(p, "--resolve", "girl", "--attempt", att["attempt_id"], "--task-id", "task_img_1")
        self.assertEqual(r.returncode, 0, r.out)
        r = self.run_pipe(p, "--make-refs")
        self.assertEqual(r.returncode, 0, r.out)
        self.assertTrue((p / "refs" / "girl.png").exists())
        self.assertEqual(len(self.mock.image_submits()), 1)


class TestChainAndQc(Base):
    def test_chain_mode_uses_previous_last_frame(self):
        p = self.make_project(segments=[
            {"id": "seg1", "prompt_file": "seg1.txt", "duration": 4, "mode": "first_frame", "first_frame": "refs/s1.png",
             "characters": ["hero"]},
            {"id": "seg2", "prompt_file": "seg2.txt", "duration": 4, "mode": "chain", "characters": ["hero"]}])
        r = self.run_pipe(p)
        self.assertIn(r.returncode, (0, 3), r.out)
        subs = self.mock.submits()
        self.assertEqual(len(subs), 2)
        self.assertEqual(subs[1]["payload"]["image_with_roles"][0]["role"], "first_frame")
        att2 = self.state(p)["segments"]["seg2"]["attempts"][0]
        self.assertEqual(att2["inputs"]["images"][0]["label"], "上一段尾帧")
        self.assertTrue(att2["inputs"]["images"][0]["from_attempt"].startswith("att_"))
        tl = json.loads((p / "output" / "timeline.json").read_text("utf-8"))
        self.assertLess(tl["total"], 8.0, "接尾帧段要去掉重复的第一帧")

    def test_narration_overrun_is_an_error_not_truncated(self):
        n = {"provider": "mock", "lines": [{"id": "n1", "segment": "seg2", "start": 3.0,
                                            "text": "这一句旁白故意很长很长很长很长很长"}]}
        p = self.make_project(narration_cfg=n)
        r = self.run_pipe(p)
        self.assertEqual(r.returncode, 1, r.out)
        self.assertIn("不会截断旁白", r.out)
        self.assertFalse((p / "output" / "final.mp4").exists())

    def test_qc_failure_reports_and_never_regenerates(self):
        self.mock.behaviors = [{"kind": "ok_black"}, {"kind": "ok"}]
        p = self.make_project(max_repairs=3)
        r = self.run_pipe(p)
        self.assertEqual(r.returncode, 3, r.out)
        rep = json.loads((p / "output" / "qc" / "report.json").read_text("utf-8"))
        self.assertEqual(rep["status"], "fail")
        self.assertTrue(any("seg1：黑帧" in f for f in rep["auto_failures"]))
        self.assertTrue((p / "output" / "qc" / "report.md").exists())
        r = self.run_pipe(p)
        self.assertEqual(len(self.mock.submits()), 2, "验收不通过不能自动付费返工")


if __name__ == "__main__":
    unittest.main()
