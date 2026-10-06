"""离线测试用的 apimart 模拟服务器。不联网、不收费，只在本机回环地址监听。

behaviors：按"第几次提交"决定服务端怎么表现，例如
    [{"kind": "ok"}, {"kind": "fail"}, {"kind": "reject"}, {"kind": "accept_then_502"}]
  ok              接单，查询两次后完成，返回视频
  ok_silent       同上，但视频没有音轨
  ok_black        同上，但画面全黑（用来测验收）
  fail            接单，随后任务失败
  reject          直接返回 400，不创建任务
  accept_then_502 创建任务后返回 502（模拟"服务端已接单、客户端没拿到 ID"）
可选字段：post_delay（秒，延迟响应提交请求）、cost（完成/失败时返回的费用）。
"""

import json
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def make_video(path, duration, audio=True, color="testsrc", freq=440):
    args = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
            "-i", f"{color}{':' if '=' in color else '='}s=480x854:r=24:d={duration}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=f={freq}:d={duration}", "-shortest", "-c:a", "aac"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True)


class MockApimart:
    def __init__(self, workdir, behaviors=None, duration=4):
        self.dir = Path(workdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.behaviors = list(behaviors or [])
        self.image_behaviors = []
        self.duration = duration
        self.calls = []
        self.tasks = {}
        self.uploads = 0
        self.lock = threading.Lock()
        self.files = self.dir / "files"
        self.files.mkdir(exist_ok=True)
        make_video(self.files / "with_audio.mp4", duration, audio=True)
        make_video(self.files / "silent.mp4", duration, audio=False, color="testsrc2")
        make_video(self.files / "black.mp4", duration, audio=True, color="color=c=black")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=480x854",
                        "-frames:v", "1", str(self.files / "last.png")], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=pink:s=600x800",
                        "-frames:v", "1", str(self.files / "gen.png")], check=True)

    # ------------------------------------------------------------ 服务器
    def start(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                auth = self.headers.get("Authorization", "")
                if self.path == "/v1/uploads/images":
                    with mock.lock:
                        mock.uploads += 1
                        n = mock.uploads
                        mock.calls.append({"type": "upload", "auth_ok": auth.startswith("Bearer ")})
                    name = re.search(rb'filename="([^"]+)"', body)
                    return self._json({"url": f"{mock.base}/files/upload_{n}.png",
                                       "filename": name.group(1).decode() if name else "x"})
                if self.path == "/v1/images/generations":
                    payload = json.loads(body)
                    with mock.lock:
                        idx = len([c for c in mock.calls if c["type"] == "image_submit"])
                        beh = (mock.image_behaviors[idx] if idx < len(mock.image_behaviors) else {"kind": "ok"})
                        mock.calls.append({"type": "image_submit", "payload": payload, "behavior": beh["kind"]})
                        tid = f"task_img_{idx + 1}"
                        if beh["kind"] != "reject":
                            mock.tasks[tid] = {"beh": {**beh, "image": True}, "polls": 0, "payload": payload}
                    if beh["kind"] == "reject":
                        return self._json({"error": {"code": 400, "message": "bad request (mock)"}}, 400)
                    if beh["kind"] == "accept_then_502":
                        return self._json({"error": "bad gateway (mock)"}, 502)
                    return self._json({"code": 200, "data": [{"status": "submitted", "task_id": tid}]})
                if self.path == "/v1/videos/generations":
                    payload = json.loads(body)
                    with mock.lock:
                        idx = len([c for c in mock.calls if c["type"] == "submit"])
                        beh = mock.behaviors[idx] if idx < len(mock.behaviors) else {"kind": "ok"}
                        mock.calls.append({"type": "submit", "payload": payload, "behavior": beh["kind"]})
                    if beh.get("post_delay"):
                        time.sleep(beh["post_delay"])
                    if beh["kind"] == "reject":
                        return self._json({"error": {"code": 400, "message": "bad request (mock)"}}, 400)
                    tid = f"task_mock_{idx + 1}"
                    with mock.lock:
                        mock.tasks[tid] = {"beh": beh, "polls": 0, "payload": payload}
                    if beh["kind"] == "accept_then_502":
                        return self._json({"error": "bad gateway (mock)"}, 502)
                    return self._json({"code": 200, "data": [{"status": "submitted", "task_id": tid}]})
                self._json({"error": "not found"}, 404)

            def do_GET(self):
                if self.path.startswith("/files/"):
                    name = self.path.split("/")[-1]
                    f = mock.files / name
                    if not f.exists():
                        f = mock.files / "last.png"
                    b = f.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                    return
                m = re.match(r"/v1/tasks/([\w-]+)", self.path)
                if not m or m.group(1) not in mock.tasks:
                    return self._json({"error": {"code": 404, "message": "Invalid task ID"}}, 404)
                tid = m.group(1)
                with mock.lock:
                    t = mock.tasks[tid]
                    t["polls"] += 1
                    mock.calls.append({"type": "poll", "task": tid})
                beh = t["beh"]
                if t["polls"] < 2:
                    return self._json({"code": 200, "data": {"id": tid, "status": "processing", "progress": 50}})
                if beh.get("image") and beh["kind"] in ("ok", "accept_then_502"):
                    return self._json({"code": 200, "data": {"id": tid, "status": "completed", "progress": 100,
                                                             "cost": 0.05, "result": {"images": [
                                                                 {"url": [f"{mock.base}/files/gen.png"], "expires_at": 1}]}}})
                if beh["kind"] == "fail":
                    return self._json({"code": 200, "data": {"id": tid, "status": "failed", "progress": 100,
                                                             "error": {"message": "mock failure"},
                                                             **({"cost": beh["cost"]} if "cost" in beh else {})}})
                video = {"ok_silent": "silent.mp4", "ok_black": "black.mp4"}.get(beh["kind"], "with_audio.mp4")
                return self._json({"code": 200, "data": {
                    "id": tid, "status": "completed", "progress": 100, "cost": beh.get("cost", 0.4),
                    "result": {"videos": [{"url": [f"{mock.base}/files/{video}"]}],
                               "last_frame_url": f"{mock.base}/files/last.png"}}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def api_base(self):
        return self.base + "/v1"

    def submits(self):
        return [c for c in self.calls if c["type"] == "submit"]

    def image_submits(self):
        return [c for c in self.calls if c["type"] == "image_submit"]
