"""工程状态（output/state.json）。

每段有一串 attempt（尝试）记录，永不删除：
  intent      已记录"准备提交"，请求可能已发出也可能没发出
  submitted   拿到服务端 task_id，等待完成
  completed   成功并已下载
  failed / cancelled   服务端明确失败（保留 task_id 和完整返回）
  rejected    服务端明确拒绝创建（4xx），确认没有生成任务
  not_sent    请求没发出去（连接被拒/DNS），确认没有生成任务
  unknown     服务端可能已接单，但本地没拿到 task_id —— 必须人工核对
  not_created 人工核对后确认服务端没有这个任务
旧格式（v1：segments.<id> = {task_id, status, ...}）读取时自动迁移，不丢记录。
"""

from .util import atomic_write_json, iso, new_id, read_json

SCHEMA = "vp-state/2"
LIVE = {"intent", "submitted"}                 # 服务端可能还在跑、可能在计费
BLOCKING = {"intent", "submitted", "unknown"}   # 存在时本段不能再开新 attempt
CONFIRMED_NOT_CREATED = {"rejected", "not_sent", "not_created"}
FAILED = {"failed", "cancelled"}


def load(out_dir, project_name):
    path = out_dir / "state.json"
    data = read_json(path)
    if data is None:
        return {"schema": SCHEMA, "project": project_name, "uploads": {}, "segments": {}, "events": []}
    if data.get("schema") != SCHEMA:
        data = _migrate_v1(data, project_name)
        save(out_dir, data)
    data.setdefault("events", [])
    return data


def _migrate_v1(old, project_name):
    new = {"schema": SCHEMA, "project": project_name, "uploads": {}, "segments": {},
           "events": [{"at": iso(), "event": "migrated_from_v1"}]}
    for sid, rec in (old.get("segments") or {}).items():
        att = {
            "attempt_id": new_id("att_legacy"), "created_at": iso(rec.get("submitted_at")),
            "origin": "legacy_v1", "status": "intent", "task_id": rec.get("task_id"),
            "inputs": None, "fingerprint": None, "reservation": None,
            "history": [{"at": iso(), "status": "migrated", "note": "旧版状态迁移，原始输入未记录"}],
        }
        st = rec.get("status")
        if rec.get("task_id"):
            att["status"] = "completed" if st == "completed" else ("submitted" if st in (None, "submitted") else st)
        else:
            # 旧版没有 task_id 但记录过提交：无法确认服务端状态
            att["status"] = "unknown" if st == "submitted" else "not_created"
        for k in ("video", "last_frame_url", "cost", "credits_cost"):
            if k in rec:
                att[k] = rec[k]
        new["segments"][sid] = {"attempts": [att]}
    return new


def save(out_dir, state):
    atomic_write_json(out_dir / "state.json", state)


def seg_rec(state, sid):
    return state["segments"].setdefault(sid, {"attempts": []})


def attempts(state, sid):
    return seg_rec(state, sid)["attempts"]


def latest(state, sid):
    a = attempts(state, sid)
    return a[-1] if a else None


def add_attempt(state, sid, origin, inputs, reservation):
    att = {
        "attempt_id": new_id("att"), "created_at": iso(), "origin": origin, "status": "intent",
        "fingerprint": inputs["fingerprint"], "inputs": inputs, "reservation": reservation,
        "task_id": None, "history": [{"at": iso(), "status": "intent", "note": "已记录提交意图，尚未发请求"}],
    }
    attempts(state, sid).append(att)
    return att


def set_status(att, status, note=None, **fields):
    att["status"] = status
    att.update(fields)
    att["history"].append({"at": iso(), "status": status, **({"note": note} if note else {})})


def recover_interrupted(state):
    """上次运行停在"已记录提交意图、还没拿到 task_id"的 attempt：请求可能已经发出，
    服务端可能已经接单。统一改成 unknown，等待核对，绝不自动重投。"""
    changed = []
    items = list(state["segments"].items()) + [(f"角色图 {k}", v) for k, v in state.get("refs", {}).items()]
    for sid, rec in items:
        for att in rec["attempts"]:
            if att["status"] == "intent":
                set_status(att, "unknown", "上次运行在提交过程中中断，无法确认服务端是否已接单")
                changed.append((sid, att["attempt_id"]))
    return changed


def repairs_used(state, sid):
    """本段已用掉的修复次数 = 除首次以外、真正可能产生费用的 attempt 数。"""
    return sum(1 for a in attempts(state, sid)
               if a.get("origin") in ("retry", "regenerate") and a["status"] not in CONFIRMED_NOT_CREATED)


def log_event(state, event, **kw):
    state["events"].append({"at": iso(), "event": event, **kw})


def completed_attempt(state, sid):
    for a in reversed(attempts(state, sid)):
        if a["status"] == "completed":
            return a
    return None


def summarize(state):
    rows = []
    for sid, rec in state["segments"].items():
        for a in rec["attempts"]:
            rows.append((sid, a["attempt_id"], a["status"], a.get("task_id"), a.get("origin"),
                         (a.get("reservation") or {}).get("amount"), a.get("cost")))
    return rows
