"""通用工具：日志、错误、原子写入、文件锁、哈希、测试中断点。"""

import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path


class PipelineError(Exception):
    """可预期的停止（配置错误、缺图、超预算等）。入口处统一打印并以非零码退出。"""

    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


def log(msg=""):
    print(msg, flush=True)


def warn(msg):
    print(f"  [提醒] {msg}", flush=True)


def now():
    return time.time()


def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts if ts is not None else time.time()))


def new_id(prefix):
    return f"{prefix}_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------- 测试中断点

_crash_hits = {}


def crash_point(name):
    """离线测试用：环境变量 VP_TEST_CRASH_AT 等于 name 时立即硬退出（模拟断电/强杀）。
    VP_TEST_CRASH_SKIP=N 表示前 N 次经过这个点不退出。正常使用时不设置这些变量，没有任何影响。"""
    if os.environ.get("VP_TEST_CRASH_AT") != name:
        return
    _crash_hits[name] = _crash_hits.get(name, 0) + 1
    if _crash_hits[name] > int(os.environ.get("VP_TEST_CRASH_SKIP", "0")):
        sys.stdout.flush()
        os._exit(77)


# ---------------------------------------------------------------- 哈希

def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_text(s):
    return sha256_bytes(s.encode("utf-8"))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(obj):
    return sha256_text(canonical_json(obj))


# ---------------------------------------------------------------- 原子写入

def atomic_write_bytes(path, data):
    """先写同目录临时文件并 fsync，再 os.replace 覆盖：中途断电/强杀时，
    目标文件要么是旧版本、要么是新版本，不会是写了一半的坏文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        crash_point("state_write_before_replace")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _fsync_dir(d):
    if os.name != "posix":
        return
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_json(path, obj):
    atomic_write_bytes(path, (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as e:
        raise PipelineError(f"{path} 不是合法 JSON（{e}）。不要手改状态文件；如已损坏，请停下来人工处理，不要删除后重跑。")


# ---------------------------------------------------------------- 文件锁

class FileLock:
    """跨进程排他锁。用操作系统的文件锁（POSIX fcntl / Windows msvcrt），
    进程崩溃时由系统自动释放，不会留下"永远锁住"的死锁文件。"""

    def __init__(self, path, what="资源"):
        self.path = Path(path)
        self.what = what
        self._f = None

    def acquire(self, timeout=0.0, poll=0.1):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        deadline = time.time() + timeout
        while True:
            try:
                _lock(f)
                break
            except OSError:
                if time.time() >= deadline:
                    f.close()
                    return False
                time.sleep(poll)
        try:
            f.seek(0)
            f.truncate()
            f.write(f"pid={os.getpid()} at={iso()}\n".encode())
            f.flush()
        except OSError:
            pass
        self._f = f
        return True

    def release(self):
        if self._f:
            try:
                _unlock(self._f)
            finally:
                self._f.close()
                self._f = None

    def holder(self):
        try:
            return self.path.read_text("utf-8", "replace").strip()
        except OSError:
            return "未知"

    def __enter__(self):
        if not self.acquire(timeout=60):
            raise PipelineError(f"等待{self.what}锁超时（{self.path}，持有者：{self.holder()}）")
        return self

    def __exit__(self, *exc):
        self.release()


if os.name == "nt":  # pragma: no cover - Windows 路径在 Linux 上无法测试
    import msvcrt

    def _lock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
