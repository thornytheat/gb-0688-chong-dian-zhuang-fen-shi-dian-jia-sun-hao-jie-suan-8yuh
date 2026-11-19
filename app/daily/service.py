"""日结服务：编排 解析 -> 按站按日结算 -> 四方对账 -> 批次清单 -> 持久化。"""
import hashlib
from datetime import datetime, timezone
from decimal import Decimal

from app.errors import DomainError
from app.normalize import serial
from app.daily import rules, store

DAILY_RULE_VERSION = "2026.11.19-daily-v1"
DEFAULT_PARAMS = {"scope": "daily"}


def build(raw=None, params=None):
    """纯计算：不写库。返回完整批次（含每行四方对账）。失败抛 DomainError。"""
    if raw is None:
        from app.config import SAMPLES
        raw = (SAMPLES / "daily_demo.csv").read_bytes()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(params, (dict, type(None))):
        raise DomainError("PARAMS", "参数必须是对象")
    actual = {**DEFAULT_PARAMS, **(params or {})}

    parsed = rules.parse(raw)
    rows, blocked = rules.settle(parsed["good"])
    reconciled = {}
    totals = {"meter": None, "order": None, "aux": None, "loss": None, "pending": None}
    for row in rows:
        key = store.row_key(row)
        reconciled[key] = rules.reconcile(row)
        for field, target in (("meter_kwh", "meter"), ("order_kwh", "order"), ("aux_kwh", "aux"),
                              ("customer_loss_kwh", "loss"), ("pending_aux_kwh", "pending")):
            if row[field] is not None:
                from decimal import Decimal
                totals[target] = (totals[target] or Decimal(0)) + row[field]

    digest = hashlib.sha256(raw).hexdigest()
    run_id = "DB" + hashlib.sha256(
        (digest + str(sorted(actual.items())) + DAILY_RULE_VERSION).encode()).hexdigest()[:18]
    manifest = {
        "run_id": run_id,
        "sha256": digest,
        "rules": DAILY_RULE_VERSION,
        "params": actual,
        "created": datetime.now(timezone.utc).isoformat(),
        "bytes": len(raw),
    }
    batch = {
        "manifest": manifest,
        "rows": rows,
        "reconciled": reconciled,
        "quarantine": parsed["quarantine"],
        "input_count": parsed["input_count"],
        "blank_count": parsed["blank_count"],
        "blocked_count": len(blocked),
        "totals": {k: (None if v is None else str(v.quantize(Decimal("0.001"))))
                   for k, v in totals.items()},
    }
    # 行数守恒：输入逻辑行 = 进入结算的来源行 + 隔离行。
    used = sum(len(r["source_lines"]) for r in rows)
    if used + len(parsed["quarantine"]) != parsed["input_count"]:
        raise DomainError("INCOMPLETE", "日结输入行数不守恒")
    return batch


def run(raw=None, params=None, persist=True):
    """计算并（可选）原子落库。落库失败由 store 整批回滚后向上抛错，旧版保持。"""
    batch = build(raw, params)
    result = serial(batch)
    if not persist:
        result["save"] = None
        return result
    result["save"] = store.save_batch(batch)
    return result
