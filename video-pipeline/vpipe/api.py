"""apimart HTTP 客户端。

关键点：提交生成任务时，把失败分成三类，决定"能不能当作没扣费"：
  ApiRejected   服务端明确拒绝（4xx，带错误体）→ 可确认没有创建任务
  ApiNotSent    请求没发出去（连接被拒、DNS 失败）→ 可确认没有创建任务
  ApiAmbiguous  超时、连接中断、5xx、返回无法解析 → 服务端**可能已经接单**，状态不明
apimart 文档没有提供幂等键或"按提交内容查任务"的接口，所以状态不明时无法自动核对，
只能停下来等人工核对，不能自动重投。
"""

import json
import mimetypes
import os
import shutil
import socket
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .util import PipelineError, crash_point

DEFAULT_BASE = "https://api.apimart.ai/v1"
DEFINITE_REJECT = {400, 401, 402, 403, 404, 409, 413, 415, 422, 429}


class ApiError(Exception):
    pass


class ApiRejected(ApiError):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


class ApiNotSent(ApiError):
    pass


class ApiAmbiguous(ApiError):
    pass


def _not_sent_reason(reason):
    if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
        return True
    text = str(reason).lower()
    return "refused" in text or "name or service not known" in text or "getaddrinfo" in text


class Client:
    def __init__(self, api_key, base=None, timeout=120):
        if not api_key:
            raise PipelineError("没有设置环境变量 APIMART_API_KEY")
        self._key = api_key
        self.base = (base or os.environ.get("APIMART_API_BASE") or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout

    # 密钥只放在请求头里，不写日志、不进异常信息
    def _request(self, method, path, body=None, content_type=None):
        headers = {"Authorization": f"Bearer {self._key}"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code in DEFINITE_REJECT:
                raise ApiRejected(e.code, detail)
            raise ApiAmbiguous(f"HTTP {e.code}: {detail[:300]}")
        except urllib.error.URLError as e:
            if _not_sent_reason(e.reason):
                raise ApiNotSent(str(e.reason))
            raise ApiAmbiguous(str(e.reason))
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            raise ApiAmbiguous(f"{type(e).__name__}: {e}")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiAmbiguous(f"返回内容无法解析：{raw[:200]!r}")

    def create_video(self, payload):
        """提交生成任务。返回 task_id；失败抛 ApiRejected / ApiNotSent / ApiAmbiguous。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        resp = self._request("POST", "/videos/generations", body, "application/json")
        crash_point("after_post_before_save")
        try:
            return resp["data"][0]["task_id"]
        except (KeyError, IndexError, TypeError):
            # 200 但没有 task_id：可能已接单也可能没有，按状态不明处理
            raise ApiAmbiguous(f"提交返回里没有 task_id：{json.dumps(resp, ensure_ascii=False)[:300]}")

    def task(self, task_id):
        return self._request("GET", f"/tasks/{task_id}?language=zh").get("data", {})

    def upload_image(self, path):
        path = Path(path)
        boundary = uuid.uuid4().hex
        ctype = mimetypes.guess_type(path.name)[0] or "image/png"
        head = (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode("utf-8")
        body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")
        try:
            resp = self._request("POST", "/uploads/images", body, f"multipart/form-data; boundary={boundary}")
        except ApiError as e:
            raise PipelineError(f"上传图片 {path.name} 失败（{e}），本段没有提交任务")
        url = resp.get("url")
        if not url:
            raise PipelineError(f"上传 {path.name} 没有拿到 url")
        return url


def download(url, dest, timeout=300):
    dest = Path(dest)
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url, timeout=timeout) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
        f.flush()
        os.fsync(f.fileno())
    if tmp.stat().st_size == 0:
        tmp.unlink()
        raise PipelineError(f"下载到的文件是空的：{url[:80]}")
    os.replace(tmp, dest)


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
    rest = {k: v for k, v in (result or {}).items() if "video" not in k.lower()}
    for p, u in _walk_urls(rest):
        if "last" in p or "frame" in p:
            return u
    for p, u in _walk_urls(rest):
        if "image" in p or u.split("?")[0].lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            return u
    return None
