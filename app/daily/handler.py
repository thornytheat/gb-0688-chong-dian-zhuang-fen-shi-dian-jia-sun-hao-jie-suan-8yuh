"""日结 HTTP 接口。所有端点挂在 /api/daily 与 /daily 页面下，与 M1-M7 互不影响。"""
import io
import csv
import json

from flask import Blueprint, Response, jsonify, render_template, request

from app.config import MAX_BYTES
from app.daily import service, store
from app.errors import DomainError
from app.normalize import serial

bp = Blueprint("daily", __name__)


def _payload():
    if request.files:
        file = request.files.get("file")
        if file is None:
            raise DomainError("FILE", "请选择文件")
        raw = file.read(MAX_BYTES + 1)
        try:
            params = json.loads(request.form.get("params", "{}"))
        except ValueError:
            raise DomainError("PARAMS", "参数格式错误")
        return raw, params
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise DomainError("BODY", "请求必须是对象")
    text = body.get("text")
    if text is not None and not isinstance(text, str):
        raise DomainError("TEXT", "数据必须为文本")
    return text, body.get("params")


@bp.route("/api/daily/runs", methods=["POST"])
def create_run():
    raw, params = _payload()
    return jsonify(service.run(raw, params, persist=True))


@bp.route("/api/daily/preview", methods=["POST"])
def preview():
    raw, params = _payload()
    return jsonify(service.run(raw, params, persist=False))


@bp.route("/api/daily/settlements")
def settlements():
    return jsonify(store.current_list(request.args.get("station"),
                                      request.args.get("biz_date"),
                                      request.args.get("status")))


@bp.route("/api/daily/settlements/<settlement_id>")
def one(settlement_id):
    return jsonify(store.fetch(settlement_id))


@bp.route("/api/daily/versions")
def history_versions():
    station, biz_date = request.args.get("station"), request.args.get("biz_date")
    if not station or not biz_date:
        raise DomainError("PARAMS", "需要 station 与 biz_date")
    return jsonify(store.versions(station, biz_date))


@bp.route("/api/daily/review", methods=["POST"])
def do_review():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise DomainError("BODY", "请求必须是对象")
    return jsonify(store.review(body.get("station"), body.get("biz_date"),
                                body.get("decision"), body.get("version"),
                                body.get("note", "")))


@bp.route("/api/daily/reopen", methods=["POST"])
def do_reopen():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise DomainError("BODY", "请求必须是对象")
    return jsonify(store.reopen(body.get("station"), body.get("biz_date"), body.get("note", "")))


@bp.route("/api/daily/batches")
def batch_list():
    return jsonify(store.batches())


@bp.route("/api/daily/batches/<run_id>")
def batch_detail(run_id):
    return jsonify(store.batch(run_id))


@bp.route("/api/daily/export.csv")
def export_csv():
    """导出当前日结台账；未知（未回传/未确认）导出为“未回传/未确认”，绝不当零。"""
    rows = store.current_list(request.args.get("station"), request.args.get("biz_date"))
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    header = ["站点", "业务日期", "状态", "总表kWh", "订单kWh", "站方辅助kWh",
              "客户损耗kWh", "待补辅助kWh", "差额kWh", "订单数", "版本", "复核", "原因码"]
    writer.writerow(header)

    def cell(value, missing="未回传"):
        return missing if value is None else value

    for r in rows:
        writer.writerow([
            r["station"], r["biz_date"], r["status"],
            cell(r["meter_kwh"]), cell(r["order_kwh"]), cell(r["aux_kwh"]),
            cell(r["customer_loss_kwh"], "未确认"), cell(r["pending_aux_kwh"], "无"),
            cell(r["gap_kwh"]), r["order_count"], r["version"], r["review"],
            ";".join(c["code"] for c in r.get("codes", [])),
        ])
    return Response("\ufeff" + stream.getvalue(), content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=daily_settlements.csv"})


# ----------------------------- 页面 -----------------------------

@bp.route("/daily")
def page_daily():
    from app.config import SAMPLES
    sample = (SAMPLES / "daily_demo.csv").read_text(encoding="utf-8-sig")
    station, biz_date, status = (request.args.get(k) for k in ("station", "biz_date", "status"))
    rows = store.current_list(station=station, biz_date=biz_date, status=status)
    batches = store.batches()
    return render_template("daily/index.html", rows=rows, batches=batches, sample=sample)


@bp.route("/daily/detail")
def page_detail():
    station, biz_date = request.args.get("station"), request.args.get("biz_date")
    if not station or not biz_date:
        return "缺少 station / biz_date", 422
    current = store.current_list(station=station, biz_date=biz_date)
    record = current[0] if current else None
    versions = store.versions(station, biz_date)
    return render_template("daily/detail.html", record=record, versions=versions,
                           station=station, biz_date=biz_date)
