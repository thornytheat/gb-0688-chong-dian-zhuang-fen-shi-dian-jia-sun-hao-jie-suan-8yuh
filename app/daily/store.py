"""日结持久化：版本化、复核锁定、补数可追溯、原子写入。

设计要点：
- 自然键为 (station, biz_date)，current 指针始终指向当前版本。
- 内容哈希相同 -> 复用同一 settlement_id，不新增版本（同日重跑不新增）。
- 内容变化 -> 写入新版本并记录字段级 changes，旧版本永久保留（补数可追溯）。
- 已复核（非“待复核”）日结锁定，导入不得覆盖，只在响应中列为 blocked。
- 整批先在内存算完并校验恒等式，再在单事务内落库；任何异常回滚，旧版不动。
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.errors import DomainError
from app.normalize import serial

TRACKED_FIELDS = [
    ("status", "状态"),
    ("meter_kwh", "总表 kWh"),
    ("order_kwh", "订单 kWh"),
    ("aux_kwh", "辅助用电 kWh"),
    ("customer_loss_kwh", "客户损耗 kWh"),
    ("pending_aux_kwh", "待补辅助 kWh"),
    ("gap_kwh", "差额 kWh"),
    ("order_count", "订单数"),
]


def _connection():
    path = Path(os.environ.get("DATA_DIR", "data"))
    path.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path / "daily.sqlite", timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS daily_settlements(
        settlement_id TEXT PRIMARY KEY,
        station TEXT NOT NULL,
        biz_date TEXT NOT NULL,
        version INTEGER NOT NULL,
        review TEXT NOT NULL DEFAULT '待复核',
        supersedes TEXT,
        content_hash TEXT NOT NULL,
        changes TEXT NOT NULL,
        payload TEXT NOT NULL,
        created TEXT NOT NULL,
        UNIQUE(station,biz_date,version))""")
    db.execute("""CREATE TABLE IF NOT EXISTS daily_current(
        station TEXT NOT NULL, biz_date TEXT NOT NULL,
        settlement_id TEXT NOT NULL, PRIMARY KEY(station,biz_date))""")
    db.execute("""CREATE TABLE IF NOT EXISTS daily_batches(
        run_id TEXT PRIMARY KEY, created TEXT NOT NULL, sha256 TEXT,
        params TEXT, input_count INTEGER, row_count INTEGER,
        quarantine_count INTEGER, payload TEXT NOT NULL)""")
    return db


def _content_hash(row):
    body = json.dumps(serial({k: row.get(k) for k, _ in TRACKED_FIELDS} |
                      {"codes": [c["code"] for c in row.get("codes", [])]}),
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def _settlement_id(station, biz_date, content_hash, rules):
    key = f"{station}|{biz_date}|{content_hash}|{rules}"
    return "D" + hashlib.sha256(key.encode()).hexdigest()[:19]


def _diff(old, new):
    """字段级变化清单；None（未回传/未确认）与数值显式区分。"""
    changes = []
    for field, label in TRACKED_FIELDS:
        old_v, new_v = old.get(field), new.get(field)
        if old_v != new_v:
            changes.append({"field": field, "label": label,
                            "old": "未回传" if old_v is None else str(old_v),
                            "new": "未回传" if new_v is None else str(new_v)})
    old_codes = {c["code"] for c in old.get("codes", [])}
    new_codes = {c["code"] for c in new.get("codes", [])}
    if old_codes != new_codes:
        changes.append({"field": "codes", "label": "原因码",
                        "old": ",".join(sorted(old_codes)) or "无",
                        "new": ",".join(sorted(new_codes)) or "无"})
    return changes


def save_batch(batch):
    """原子保存一整批日结。先全量校验与构造，失败则整体不落库、旧版保留。"""
    rows = batch["rows"]
    # 硬校验：所有可结算行必须满足四方恒等式，否则拒绝整批（恢复旧版）。
    for row in rows:
        if row["status"] != "异常-不可结算" and not batch["reconciled"][row_key(row)]["identity_pass"]:
            raise DomainError("INCOMPLETE", f'{row["station"]} {row["biz_date"]} 四方对账不平衡，整批退回')
    prepared = []
    for row in rows:
        record = dict(row)
        record["reconcile"] = batch["reconciled"][row_key(row)]
        chash = _content_hash(row)
        sid = _settlement_id(row["station"], row["biz_date"], chash, batch["manifest"]["rules"])
        prepared.append((row, record, chash, sid))

    summary = {"created": [], "updated": [], "unchanged": [], "blocked": [], "run_id": batch["manifest"]["run_id"]}
    db = _connection()
    try:
        with db:  # 单事务：任何异常自动回滚，current 指针与旧版本均不变
            for row, record, chash, sid in prepared:
                cur = db.execute(
                    "SELECT s.* FROM daily_settlements s JOIN daily_current c "
                    "ON s.settlement_id=c.settlement_id WHERE c.station=? AND c.biz_date=?",
                    (row["station"], row["biz_date"])).fetchone()
                now = datetime.now(timezone.utc).isoformat()
                if cur is None:
                    db.execute(
                        "INSERT INTO daily_settlements(settlement_id,station,biz_date,version,review,"
                        "supersedes,content_hash,changes,payload,created) VALUES(?,?,?,1,'待复核',NULL,?,?,?,?)",
                        (sid, row["station"], row["biz_date"], chash, "[]",
                         json.dumps(serial(record), ensure_ascii=False), now))
                    db.execute("INSERT INTO daily_current(station,biz_date,settlement_id) VALUES(?,?,?)",
                               (row["station"], row["biz_date"], sid))
                    summary["created"].append(row_key(row))
                elif cur["content_hash"] == chash:
                    # 内容一致：同日重跑不新增。
                    summary["unchanged"].append(row_key(row))
                elif cur["review"] != "待复核":
                    # 已复核日结保持原样：不覆盖，只交代冲突。
                    summary["blocked"].append({
                        "station": row["station"], "biz_date": row["biz_date"],
                        "reason": f'当前版本已{cur["review"]}（V{cur["version"]}），复核后锁定，如需调整请先解锁',
                        "incoming_status": row["status"]})
                else:
                    old_payload = json.loads(cur["payload"])
                    changes = _diff(old_payload, row)
                    new_version = cur["version"] + 1
                    db.execute(
                        "INSERT INTO daily_settlements(settlement_id,station,biz_date,version,review,"
                        "supersedes,content_hash,changes,payload,created) VALUES(?,?,?,?,'待复核',?,?,?,?,?)",
                        (sid, row["station"], row["biz_date"], new_version, cur["settlement_id"],
                         chash, json.dumps(serial(changes), ensure_ascii=False),
                         json.dumps(serial(record), ensure_ascii=False), now))
                    db.execute("UPDATE daily_current SET settlement_id=? WHERE station=? AND biz_date=?",
                               (sid, row["station"], row["biz_date"]))
                    summary["updated"].append({"key": row_key(row), "version": new_version, "changes": changes})
            db.execute(
                "INSERT OR IGNORE INTO daily_batches(run_id,created,sha256,params,input_count,"
                "row_count,quarantine_count,payload) VALUES(?,?,?,?,?,?,?,?)",
                (batch["manifest"]["run_id"], batch["manifest"]["created"], batch["manifest"]["sha256"],
                 json.dumps(batch["manifest"]["params"], ensure_ascii=False), batch["input_count"],
                 len(rows), len(batch["quarantine"]),
                 json.dumps(serial(batch), ensure_ascii=False)))
    except Exception:
        db.close()
        raise
    db.close()
    return summary


def row_key(row):
    return f'{row["station"]}@{row["biz_date"]}'


def _hydrate(r):
    data = json.loads(r["payload"])
    data["settlement_id"] = r["settlement_id"]
    data["version"] = r["version"]
    data["review"] = r["review"]
    data["supersedes"] = r["supersedes"]
    data["changes"] = json.loads(r["changes"])
    data["created"] = r["created"]
    return data


def current_list(station=None, biz_date=None, status=None):
    db = _connection()
    sql = ("SELECT s.* FROM daily_settlements s JOIN daily_current c "
           "ON s.settlement_id=c.settlement_id WHERE 1=1")
    args = []
    if station:
        sql += " AND c.station=?"; args.append(station)
    if biz_date:
        sql += " AND c.biz_date=?"; args.append(biz_date)
    sql += " ORDER BY c.biz_date DESC, c.station"
    rows = [_hydrate(r) for r in db.execute(sql, args).fetchall()]
    db.close()
    if status:
        rows = [r for r in rows if r["status"] == status]
    return rows


def fetch(settlement_id):
    db = _connection()
    r = db.execute("SELECT * FROM daily_settlements WHERE settlement_id=?", (settlement_id,)).fetchone()
    db.close()
    if r is None:
        raise DomainError("NOT_FOUND", "日结版本不存在", 404)
    return _hydrate(r)


def versions(station, biz_date):
    db = _connection()
    rows = db.execute(
        "SELECT * FROM daily_settlements WHERE station=? AND biz_date=? ORDER BY version DESC",
        (station, biz_date)).fetchall()
    db.close()
    if not rows:
        raise DomainError("NOT_FOUND", "该站日没有日结", 404)
    return [{"settlement_id": r["settlement_id"], "version": r["version"], "review": r["review"],
             "supersedes": r["supersedes"], "created": r["created"],
             "changes": json.loads(r["changes"]),
             "status": json.loads(r["payload"])["status"]} for r in rows]


def review(station, biz_date, decision, version, note=""):
    if decision not in ("通过", "退回"):
        raise DomainError("REVIEW", "请选择通过或退回")
    if not isinstance(version, int):
        raise DomainError("STALE", "版本必须为整数")
    db = _connection()
    with db:
        cur = db.execute(
            "SELECT s.settlement_id,s.version FROM daily_settlements s JOIN daily_current c "
            "ON s.settlement_id=c.settlement_id WHERE c.station=? AND c.biz_date=?",
            (station, biz_date)).fetchone()
        if cur is None:
            raise DomainError("NOT_FOUND", "该站日没有日结", 404)
        update = db.execute(
            "UPDATE daily_settlements SET review=?, version=version+1 "
            "WHERE settlement_id=? AND version=?",
            (f"{decision}" + (f"：{note}" if note else ""), cur["settlement_id"], version))
        if update.rowcount != 1:
            raise DomainError("STALE", "日结版本已更新，请刷新后复核", 409)
    db.close()
    return fetch(cur["settlement_id"])


def reopen(station, biz_date, note=""):
    """显式解锁：人工动作，留痕，之后导入才能重新覆盖。"""
    db = _connection()
    with db:
        cur = db.execute(
            "SELECT s.settlement_id FROM daily_settlements s JOIN daily_current c "
            "ON s.settlement_id=c.settlement_id WHERE c.station=? AND c.biz_date=?",
            (station, biz_date)).fetchone()
        if cur is None:
            raise DomainError("NOT_FOUND", "该站日没有日结", 404)
        db.execute("UPDATE daily_settlements SET review='待复核' WHERE settlement_id=?",
                   (cur["settlement_id"],))
    db.close()
    return fetch(cur["settlement_id"])


def batches(limit=50):
    db = _connection()
    rows = db.execute(
        "SELECT run_id,created,sha256,params,input_count,row_count,quarantine_count "
        "FROM daily_batches ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def batch(run_id):
    db = _connection()
    r = db.execute("SELECT * FROM daily_batches WHERE run_id=?", (run_id,)).fetchone()
    db.close()
    if r is None:
        raise DomainError("NOT_FOUND", "日结批次不存在", 404)
    return json.loads(r["payload"])
