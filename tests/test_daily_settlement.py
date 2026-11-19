import io
from decimal import Decimal

import pytest

from app.daily import rules, service, store
from app.errors import DomainError
from app.config import SAMPLES

DEMO = (SAMPLES / "daily_demo.csv").read_bytes()
HEADER = "id,station,biz_date,kind,energy,unit,order_no\n"


def _row(rows, station, day):
    match = [r for r in rows if r["station"] == station and r["biz_date"] == day]
    assert len(match) == 1
    return match[0]


def D(value):
    return Decimal(str(value))


def _csv(text):
    return text.encode("utf-8")


# ----------------------------- 解析 -----------------------------

def test_parse_utf8_bom_and_gb18030():
    text = HEADER + "m1,站A,2026-11-17,meter,100,kWh,\n"
    parsed = rules.parse(b"\xef\xbb\xbf" + text.encode("utf-8"))
    assert parsed["input_count"] == 1 and parsed["quarantine"] == []
    parsed2 = rules.parse(text.encode("gb18030"))
    assert parsed2["good"][0]["energy"] == Decimal("100")


def test_parse_fullwidth_and_unit_conversion():
    text = HEADER + "m1,站A,2026-11-17,meter,０．０１０,MWh,\n"
    row = rules.parse(_csv(text))["good"][0]
    assert row["energy"] == Decimal("10")  # 0.010 MWh = 10 kWh


def test_unknown_energy_is_none_not_zero_for_aux():
    text = HEADER + "x1,站A,2026-11-17,aux,未知,kWh,\n"
    row = rules.parse(_csv(text))["good"][0]
    assert row["energy"] is None


def test_unknown_energy_on_meter_is_quarantined():
    text = HEADER + "m1,站A,2026-11-17,meter,未知,kWh,\n"
    parsed = rules.parse(_csv(text))
    assert parsed["good"] == [] and parsed["quarantine"][0]["code"] == "MISSING"


def test_bad_kind_date_and_column_count_quarantined():
    text = HEADER + (
        "q1,站D,2026-11-17,telemetry,10,kWh,\n"
        "q2,站D,2026/11/17,aux,10,kWh,\n"
        "short,row\n")
    parsed = rules.parse(_csv(text))
    codes = {q["code"] for q in parsed["quarantine"]}
    assert {"D_KIND", "D_DATE", "COLUMN_COUNT"} <= codes
    assert parsed["input_count"] == 3 and parsed["good"] == []


def test_negative_energy_quarantined():
    text = HEADER + "m1,站A,2026-11-17,meter,-5,kWh,\n"
    assert rules.parse(_csv(text))["quarantine"][0]["code"] == "D_NEGATIVE"


# ----------------------------- 结算与待补 -----------------------------

def test_complete_day_balances_four_ways():
    rows, blocked = rules.settle(rules.parse(DEMO)["good"])
    a17 = _row(rows, "站A", "2026-11-17")
    assert a17["status"] == "可结算-完整"
    assert a17["meter_kwh"] == Decimal("1000.000")
    assert a17["order_kwh"] == Decimal("850.000")
    assert a17["aux_kwh"] == Decimal("50.000")
    assert a17["customer_loss_kwh"] == Decimal("100.000")
    assert a17["pending_aux_kwh"] == Decimal("0.000")
    rec = rules.reconcile(a17)
    assert rec["identity_pass"] is True
    assert ("站A", _date("2026-11-17")) not in blocked


def _date(s):
    from datetime import date
    return date.fromisoformat(s)


def test_pending_aux_holds_gap_and_never_zeroes_customer_loss():
    rows, blocked = rules.settle(rules.parse(DEMO)["good"])
    a18 = _row(rows, "站A", "2026-11-18")
    assert a18["status"] == "可结算-待补辅助"
    assert a18["aux_kwh"] is None                 # 未回传，不是 0
    assert a18["customer_loss_kwh"] is None       # 客户损耗不确认，不当零
    assert a18["pending_aux_kwh"] == Decimal("110.000")  # 980-870 全挂待补
    assert a18["gap_kwh"] == Decimal("110.000")
    assert any(c["code"] == "D_MISS_AUX" for c in a18["codes"])
    rec = rules.reconcile(a18)
    assert rec["identity_pass"] is True


def test_missing_meter_blocks_day():
    rows, blocked = rules.settle(rules.parse(DEMO)["good"])
    b18 = _row(rows, "站B", "2026-11-18")
    assert b18["status"] == "异常-不可结算"
    assert b18["meter_kwh"] is None
    assert any(c["code"] == "D_MISS_METER" for c in b18["codes"])
    assert ("站B", _date("2026-11-18")) in blocked


def test_negative_gap_blocked():
    rows, blocked = rules.settle(rules.parse(DEMO)["good"])
    c18 = _row(rows, "站C", "2026-11-18")  # 500 - 480 - 40 = -20
    assert c18["gap_kwh"] == Decimal("-20.000")
    assert c18["status"] == "异常-不可结算"
    assert any(c["code"] == "D_NEG_LOSS" for c in c18["codes"])


def test_pending_zero_gap_keeps_loss_unconfirmed():
    # 待补日差额恰为 0：客户损耗仍不确认（None），绝不写 0，状态保持待补
    text = HEADER + (
        "m,站Z,2026-11-17,meter,100,kWh,\n"
        "o,站Z,2026-11-17,order,100,kWh,O1\n"
        "x,站Z,2026-11-17,aux,未知,kWh,\n")
    row = rules.settle(rules.parse(_csv(text))["good"])[0][0]
    assert row["status"] == "可结算-待补辅助"
    assert row["aux_kwh"] is None and row["customer_loss_kwh"] is None
    assert row["pending_aux_kwh"] == Decimal("0.000")
    assert rules.reconcile(row)["identity_pass"] is True


def test_multi_order_sum_and_complete_loss():
    text = HEADER + (
        "m,站Y,2026-11-17,meter,200,kWh,\n"
        "o1,站Y,2026-11-17,order,120,kWh,A\n"
        "o2,站Y,2026-11-17,order,60,kWh,B\n"
        "x,站Y,2026-11-17,aux,15,kWh,\n")
    row = rules.settle(rules.parse(_csv(text))["good"])[0][0]
    assert row["order_count"] == 2
    assert row["order_kwh"] == Decimal("180.000")
    assert row["customer_loss_kwh"] == Decimal("5.000")  # 200-180-15


def test_mwh_day_and_duplicate_meter():
    rows, blocked = rules.settle(rules.parse(DEMO)["good"])
    c17 = _row(rows, "站C", "2026-11-17")  # 1.0 MWh 总表, 0.9 MWh 订单
    assert c17["meter_kwh"] == Decimal("1000.000")
    assert c17["order_kwh"] == Decimal("900.000")
    assert c17["customer_loss_kwh"] == Decimal("70.000")
    d17 = _row(rows, "站D", "2026-11-17")
    assert d17["status"] == "异常-不可结算"
    assert any(c["code"] == "D_DUP_METER" for c in d17["codes"])


# ----------------------------- 服务编排与守恒 -----------------------------

def test_service_row_conservation_and_totals(isolate):
    result = service.run(DEMO, persist=False)
    used = sum(len(r["source_lines"]) for r in result["rows"])
    assert used + len(result["quarantine"]) == result["input_count"]
    # 隔离 4 行：坏kind、坏日期、列数、订单未知电量
    assert len(result["quarantine"]) == 4
    # 待补日的客户损耗不计入合计，待补电量单列
    assert result["totals"]["pending"] == "110.000"
    assert result["totals"]["loss"] is not None


def test_every_settleable_row_passes_identity(isolate):
    result = service.run(DEMO, persist=False)
    for row in result["rows"]:
        if row["status"] != "异常-不可结算":
            assert result["reconciled"][store.row_key(row)]["identity_pass"] is True


# ----------------------------- 持久化：幂等、补数、锁定、原子 -----------------------------

def test_rerun_same_content_is_idempotent(isolate):
    first = service.run(DEMO)
    second = service.run(DEMO)
    assert first["manifest"]["run_id"] == second["manifest"]["run_id"]
    assert second["save"]["unchanged"] and not second["save"]["updated"]
    rec = store.current_list(station="站A", biz_date="2026-11-17")[0]
    assert store.versions("站A", "2026-11-17").__len__() == 1
    assert rec["version"] == 1


def test_supplement_creates_traceable_new_version(isolate):
    service.run(DEMO)
    before = store.current_list(station="站A", biz_date="2026-11-18")[0]
    assert before["status"] == "可结算-待补辅助"
    # 下午辅助表回传：补数后重跑（只补该站日，其余仍在，单独构造同站日数据）
    supplement = (HEADER +
                  "m,站A,2026-11-18,meter,980,kWh,\n"
                  "o1,站A,2026-11-18,order,520,kWh,O1\n"
                  "o2,站A,2026-11-18,order,350,kWh,O2\n"
                  "x,站A,2026-11-18,aux,60,kWh,\n").encode("utf-8")
    out = service.run(supplement)
    assert out["save"]["updated"]
    after = store.current_list(station="站A", biz_date="2026-11-18")[0]
    assert after["version"] == 2
    assert after["status"] == "可结算-完整"
    assert D(after["aux_kwh"]) == Decimal("60.000")
    assert D(after["customer_loss_kwh"]) == Decimal("50.000")  # 980-870-60
    assert D(after["pending_aux_kwh"]) == Decimal("0.000")
    # 旧版本永久保留，changes 可追溯
    versions = store.versions("站A", "2026-11-18")
    assert len(versions) == 2 and versions[1]["version"] == 1
    fields = {c["field"] for c in after["changes"]}
    assert {"aux_kwh", "customer_loss_kwh", "pending_aux_kwh", "status"} <= fields


def test_reviewed_settlement_is_locked_and_kept(isolate):
    service.run(DEMO)
    rec = store.current_list(station="站A", biz_date="2026-11-17")[0]
    store.review("站A", "2026-11-17", "通过", rec["version"], "复核无误")
    changed = (HEADER +
               "m,站A,2026-11-17,meter,1005,kWh,\n"
               "o,站A,2026-11-17,order,850,kWh,O\n"
               "x,站A,2026-11-17,aux,50,kWh,\n").encode("utf-8")
    out = service.run(changed)
    assert any(b["station"] == "站A" and b["biz_date"] == "2026-11-17" for b in out["save"]["blocked"])
    current = store.current_list(station="站A", biz_date="2026-11-17")[0]
    assert D(current["meter_kwh"]) == Decimal("1000.000")  # 原样未被覆盖
    assert current["review"].startswith("通过")
    # 解锁后才能重新覆盖
    store.reopen("站A", "2026-11-17")
    out2 = service.run(changed)
    assert any(u["key"] == "站A@2026-11-17" for u in out2["save"]["updated"])
    assert D(store.current_list(station="站A", biz_date="2026-11-17")[0]["meter_kwh"]) == Decimal("1005.000")


def test_stale_review_rejected_with_409(isolate):
    service.run(DEMO)
    rec = store.current_list(station="站A", biz_date="2026-11-17")[0]
    store.review("站A", "2026-11-17", "通过", rec["version"])
    with pytest.raises(DomainError) as exc:
        store.review("站A", "2026-11-17", "退回", rec["version"])  # 版本已自增
    assert exc.value.status == 409


def test_failed_batch_keeps_previous_version(isolate):
    service.run(DEMO)
    before = store.current_list(station="站A", biz_date="2026-11-17")[0]
    bad_batch = {
        "manifest": {"run_id": "DBbad", "rules": service.DAILY_RULE_VERSION,
                     "created": "2026-11-19T00:00:00+00:00", "sha256": "x", "params": {}},
        "input_count": 1,
        "quarantine": [],
        "rows": [{
            "station": "站A", "biz_date": "2026-11-17", "status": "可结算-完整",
            "meter_kwh": Decimal("999"), "order_kwh": Decimal("1"), "aux_kwh": Decimal("1"),
            "customer_loss_kwh": Decimal("1"), "pending_aux_kwh": Decimal("0"),
            "gap_kwh": Decimal("997"), "order_count": 1, "source_lines": [1], "codes": [],
        }],
        "reconciled": {"站A@2026-11-17": {"identity_pass": False, "checks": [], "identity": ""}},
    }
    with pytest.raises(DomainError):
        store.save_batch(bad_batch)
    after = store.current_list(station="站A", biz_date="2026-11-17")[0]
    assert after["settlement_id"] == before["settlement_id"]  # 旧版恢复/保留
    assert after["version"] == 1


# ----------------------------- HTTP -----------------------------

def test_http_endpoints_and_export(client):
    r = client.post("/api/daily/runs", json={"text": DEMO.decode("utf-8-sig")})
    assert r.status_code == 200 and r.json["rows"]
    listing = client.get("/api/daily/settlements").json
    assert any(x["station"] == "站A" for x in listing)
    # 待补日行导出为“未回传/未确认”，绝不出现把辅助或损耗当 0
    export = client.get("/api/daily/export.csv").text
    assert "未回传" in export and "未确认" in export
    page = client.get("/daily")
    assert page.status_code == 200 and "日结" in page.text
    sid = [x for x in listing if x["biz_date"] == "2026-11-18" and x["station"] == "站A"][0]["settlement_id"]
    detail = client.get("/daily/detail?station=站A&biz_date=2026-11-18")
    assert detail.status_code == 200
    assert client.get(f"/api/daily/settlements/{sid}").status_code == 200
    assert client.get("/api/daily/versions?station=站A&biz_date=2026-11-18").status_code == 200


def test_http_review_and_preview_does_not_persist(client):
    preview = client.post("/api/daily/preview", json={"text": DEMO.decode("utf-8-sig")}).json
    assert preview["save"] is None
    client.post("/api/daily/runs", json={"text": DEMO.decode("utf-8-sig")})
    rec = client.get("/api/daily/settlements?station=站A&biz_date=2026-11-17").json[0]
    ok = client.post("/api/daily/review",
                     json={"station": "站A", "biz_date": "2026-11-17",
                           "decision": "通过", "version": rec["version"]})
    assert ok.status_code == 200
    assert client.get("/api/daily/batches").status_code == 200
