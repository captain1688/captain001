"""预算硬限制与账本（*.ledger.json）。

每次提交前，在账本锁内做一次"事务"：
    已花费 + 在途预占 + 本次新任务 ≤ 预算上限  才写入一条 reserved 预占，否则停止。
多个进程（多条工程共用一个批次预算）同时提交时，锁保证它们排队检查和预占，
不会各自读到旧余额后一起通过、合起来超支。
预占只在"确认失败 / 确认没有创建任务"时释放；超时、状态不明一律不释放。
"""

from .util import FileLock, PipelineError, atomic_write_json, iso, read_json

SCHEMA = "vp-ledger/1"


class Ledger:
    def __init__(self, budget):
        self.b = budget
        self.path = budget["ledger"]
        self.lock = FileLock(self.path.with_name(self.path.name + ".lock"), "预算账本")

    def _load(self):
        data = read_json(self.path) or {"schema": SCHEMA, "batch_id": self.b["batch_id"],
                                        "currency": self.b["currency"], "entries": {}}
        if data.get("currency") != self.b["currency"]:
            raise PipelineError(f"账本 {self.path} 的币种是 {data.get('currency')}，配置是 {self.b['currency']}；"
                                f"不同币种不能混记，请用新的批次文件")
        return data

    def _save(self, data):
        atomic_write_json(self.path, data)

    @staticmethod
    def totals(data):
        spent = reserved = 0.0
        repairs = 0
        for e in data["entries"].values():
            if e["state"] == "spent":
                spent += e["actual"] if e.get("actual") is not None else e["estimate"]
            elif e["state"] == "reserved":
                reserved += e["estimate"]
            if e.get("origin") in ("retry", "regenerate") and e["state"] != "released":
                repairs += 1
        return round(spent, 6), round(reserved, 6), repairs

    def snapshot(self):
        with self.lock:
            data = self._load()
        spent, reserved, repairs = self.totals(data)
        return {"limit": self.b["limit"], "currency": self.b["currency"], "spent": spent,
                "reserved": reserved, "available": round(self.b["limit"] - spent - reserved, 6),
                "repairs_total": repairs, "entries": len(data["entries"])}

    def reserve(self, key, estimate, project, segment, attempt_id, origin, extra_inflight=0.0):
        """原子地检查并预占。超限抛 PipelineError，不写任何东西。"""
        with self.lock:
            data = self._load()
            if key in data["entries"]:
                raise PipelineError(f"账本里已经有 {key} 的记录，拒绝重复预占")
            spent, reserved, repairs = self.totals(data)
            if origin in ("retry", "regenerate") and self.b.get("max_repairs_total") is not None \
                    and repairs + 1 > int(self.b["max_repairs_total"]):
                raise PipelineError(f"批次修复次数已用完（上限 {self.b['max_repairs_total']} 次），不再提交")
            reserved += extra_inflight   # 账本外的在途任务（旧版记录）
            total = spent + reserved + estimate
            if total > self.b["limit"] + 1e-9:
                raise PipelineError(
                    f"超出预算，停止提交：已花费 {spent:g} + 在途预占 {reserved:g} + 本次 {estimate:g} "
                    f"= {total:g} {self.b['currency']}，上限 {self.b['limit']:g} {self.b['currency']}（批次 {self.b['batch_id']}）")
            data["entries"][key] = {"project": project, "segment": segment, "attempt_id": attempt_id,
                                    "origin": origin, "estimate": estimate, "actual": None,
                                    "state": "reserved", "at": iso(), "history": [[iso(), "reserved"]]}
            self._save(data)
            return {"key": key, "amount": estimate, "currency": self.b["currency"],
                    "ledger": str(self.path), "batch_id": self.b["batch_id"]}

    def _update(self, key, state, actual=None, note=None):
        with self.lock:
            data = self._load()
            e = data["entries"].get(key)
            if e is None:
                return
            e["state"] = state
            if actual is not None:
                e["actual"] = actual
            e["history"].append([iso(), state] + ([note] if note else []))
            self._save(data)

    def settle(self, key, actual_cost=None):
        """任务完成：转为已花费。服务端返回的费用只有在配置声明了同币种时才采用，否则按估算记。"""
        use = actual_cost if (actual_cost is not None and self.b.get("actual_cost_currency") == self.b["currency"]) else None
        self._update(key, "spent", use, "completed")

    def release(self, key, note):
        """只在确认没有产生费用时调用（明确拒绝 / 确认未创建 / 失败且确认不计费）。"""
        self._update(key, "released", None, note)

    def failed(self, key, actual_cost):
        """服务端明确失败。返回了费用就按费用记；明确为 0 释放；没有费用信息时按配置决定，默认保守地保留为已花费。"""
        if actual_cost is not None and actual_cost > 0:
            self._update(key, "spent", actual_cost if self.b.get("actual_cost_currency") == self.b["currency"] else None,
                         "failed_with_cost")
        elif actual_cost == 0 or self.b.get("release_failed_without_cost"):
            self._update(key, "released", None, "failed_no_cost")
        else:
            self._update(key, "spent", None, "failed_cost_unknown_kept")
