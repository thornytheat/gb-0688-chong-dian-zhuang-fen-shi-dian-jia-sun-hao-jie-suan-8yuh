"""按站按日结算领域规则。

业务恒等式（每站每日）：
    总表电量 = 订单电量 + 客户损耗 + 站方辅助用电 + 待补辅助用电
其中“待补辅助用电”专门承载辅助表未回传的部分，绝不作零、绝不计入客户损耗。
所有电量统一为 kWh，全程 Decimal 精确运算，未知电量用 None 表示而非 0。
"""
import csv
import io
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.errors import DomainError
from app.reasons import rejected

ZONE = timezone(timedelta(hours=8))
KINDS = ("meter", "order", "aux")
KIND_LABEL = {"meter": "总表", "order": "订单", "aux": "辅助用电"}
SENTINELS = {"", "-", "N/A", "NULL", "未知"}
REQUIRED = ["id", "station", "biz_date", "kind", "energy", "unit"]

# 日结原因码（沿用全局 reasons 文案表，缺失的在此补充）
DAILY_REASONS = {
    "D_HEADER": "日结字段名不符合契约",
    "D_KIND": "读数类型无效（仅 meter/order/aux）",
    "D_DATE": "业务日期格式错误（需 YYYY-MM-DD）",
    "D_UNIT": "电量单位无效（仅 kWh/MWh）",
    "D_ENERGY": "电量数值格式错误",
    "D_NEGATIVE": "电量为负数",
    "D_DUP_METER": "当日总表重复",
    "D_DUP_AUX": "当日辅助表重复",
    "D_MISS_METER": "当日总表未回传",
    "D_MISS_ORDER": "当日无订单数据",
    "D_MISS_AUX": "辅助表未回传，差额挂待补",
    "D_NEG_LOSS": "总表小于订单与辅助之和",
    "D_NOT_DAY": "读数未对齐自然日（+08:00）",
}


def _day(value):
    try:
        return date.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        raise DomainError("D_DATE", DAILY_REASONS["D_DATE"])


def _energy(cell):
    """电量解析：哨兵/空 -> None（未知，不作零）；其余 -> Decimal。"""
    text = unicodedata.normalize("NFKC", str(cell)).strip()
    if text.upper() in SENTINELS:
        return None
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise DomainError("D_ENERGY", DAILY_REASONS["D_ENERGY"])
    if not value.is_finite():
        raise DomainError("D_ENERGY", DAILY_REASONS["D_ENERGY"])
    return value


def parse(raw):
    """解析日结 CSV。返回 good 行（含解析后的 Decimal/date）与隔离行。

    与全局 normalize.rows_from 的契约保持一致：UTF-8 BOM / GB18030、严格 CSV、
    记录原始行号、字段数不符隔离、空行单列。辅助表允许 energy 为未知哨兵。
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    from app.config import MAX_BYTES
    if len(raw) > MAX_BYTES:
        raise DomainError("TOO_LARGE", "文件超过4MB", 413)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError:
            raise DomainError("ENCODING", "编码无法读取")
    if not text or not text.strip():
        raise DomainError("BLANK_ONLY", "仅含空行")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader)
    except (StopIteration, csv.Error):
        raise DomainError("D_HEADER", DAILY_REASONS["D_HEADER"])
    header = [v.strip().lstrip("\ufeff") for v in header]
    if len(header) != len(set(header)) or not set(REQUIRED) <= set(header):
        raise DomainError("D_HEADER", DAILY_REASONS["D_HEADER"])

    good, quarantine, blank, total = [], [], 0, 0
    try:
        for cells in reader:
            if not cells or not any(v.strip() for v in cells):
                blank += 1
                continue
            total += 1
            if len(cells) != len(header):
                quarantine.append(rejected({"_line": reader.line_num}, "COLUMN_COUNT", repr(cells)))
                continue
            row = dict(zip(header, (v.strip() for v in cells)))
            row["_line"] = reader.line_num
            try:
                parsed = _validate(row)
            except DomainError as exc:
                quarantine.append(rejected(row, exc.code, exc.message))
                continue
            good.append(parsed)
    except csv.Error:
        raise DomainError("CSV", "CSV引号未闭合")
    if total == 0:
        raise DomainError("HEADER_ONLY", "只有字段名")
    return {"good": good, "quarantine": quarantine, "input_count": total, "blank_count": blank}


def _validate(row):
    if not row["id"] or not row["station"]:
        raise DomainError("MISSING", "读数号或站点缺失")
    if row["kind"] not in KINDS:
        raise DomainError("D_KIND", DAILY_REASONS["D_KIND"])
    day = _day(row["biz_date"])
    if row["unit"] not in ("kWh", "MWh"):
        raise DomainError("D_UNIT", DAILY_REASONS["D_UNIT"])
    energy = _energy(row["energy"])
    # 只有辅助表允许未知电量（表示回传占位但无数值）；其余类型未知即缺失。
    if energy is None and row["kind"] != "aux":
        raise DomainError("MISSING", "总表或订单电量缺失，未知不作零")
    if energy is not None:
        energy = energy * (Decimal(1000) if row["unit"] == "MWh" else Decimal(1))
        if energy < 0:
            raise DomainError("D_NEGATIVE", DAILY_REASONS["D_NEGATIVE"])
    # 可选自然日窗口校验：start/end 存在时必须正好覆盖该业务日（+08:00）。
    window = None
    if row.get("start") and row.get("end"):
        try:
            start = datetime.fromisoformat(row["start"].replace("Z", "+00:00")).astimezone(ZONE)
            end = datetime.fromisoformat(row["end"].replace("Z", "+00:00")).astimezone(ZONE)
        except (ValueError, TypeError):
            raise DomainError("TIME", "时间缺少偏移或格式无效")
        day_start = datetime.combine(day, datetime.min.time(), ZONE)
        day_end = day_start + timedelta(days=1)
        if start != day_start or end != day_end:
            raise DomainError("D_NOT_DAY", DAILY_REASONS["D_NOT_DAY"])
        window = (start, end)
    return {
        "id": row["id"],
        "station": row["station"],
        "biz_date": day,
        "kind": row["kind"],
        "order_no": row.get("order_no", "") or "",
        "energy": energy,  # None 仅可能出现在 aux
        "line": row["_line"],
        "window": window,
    }


def _q(value):
    return None if value is None else value.quantize(Decimal("0.001"))


def settle(good):
    """按站按日聚合，生成日结行与对账结果。

    返回 (rows, blocked)：rows 为每站每日一条日结（含待补态），blocked 为硬异常站日键。
    同一站日的多张订单求和；总表/辅助表各允许唯一一条，重复即硬异常。
    """
    groups = defaultdict(lambda: {"meter": [], "order": [], "aux": []})
    for row in good:
        groups[(row["station"], row["biz_date"])][row["kind"]].append(row)

    rows = []
    blocked = set()
    for (station, day) in sorted(groups.keys(), key=lambda k: (k[0], k[1])):
        bucket = groups[(station, day)]
        meters, orders, auxs = bucket["meter"], bucket["order"], bucket["aux"]
        codes = []  # (原因码, 说明)
        status = "可结算-完整"

        # 总表：必须唯一。重复为硬异常；缺失则该站日无法出账。
        meter_energy = None
        if len(meters) > 1:
            codes.append(("D_DUP_METER", DAILY_REASONS["D_DUP_METER"]))
        elif len(meters) == 1:
            meter_energy = meters[0]["energy"]
        if meter_energy is None:
            codes.append(("D_MISS_METER", DAILY_REASONS["D_MISS_METER"]))

        # 订单：至少一条，电量求和。
        order_energy = sum((r["energy"] for r in orders), Decimal(0)) if orders else None
        if not orders:
            codes.append(("D_MISS_ORDER", DAILY_REASONS["D_MISS_ORDER"]))

        # 辅助表：唯一且数值已知才算回传；否则进入待补，绝不作零。
        aux_energy = None
        aux_pending = False
        if len(auxs) > 1:
            codes.append(("D_DUP_AUX", DAILY_REASONS["D_DUP_AUX"]))
        elif len(auxs) == 1 and auxs[0]["energy"] is not None:
            aux_energy = auxs[0]["energy"]
        else:
            aux_pending = True
            codes.append(("D_MISS_AUX", DAILY_REASONS["D_MISS_AUX"]))

        hard = any(c[0] in ("D_DUP_METER", "D_DUP_AUX", "D_MISS_METER", "D_MISS_ORDER") for c in codes)
        if hard:
            blocked.add((station, day))
            status = "异常-不可结算"
        elif aux_pending:
            status = "可结算-待补辅助"

        customer_loss = None
        pending_aux = None
        gap = None
        if meter_energy is not None and order_energy is not None:
            known_aux = aux_energy if aux_energy is not None else Decimal(0)
            gap = meter_energy - order_energy - known_aux  # 总表减订单（再减已知辅助）的差额
            if aux_pending:
                # 辅助未回传：全部差额挂待补，客户损耗不确认（不作零，单独交代）。
                pending_aux = gap
                customer_loss = None
                if gap < 0:
                    codes.append(("D_NEG_LOSS", DAILY_REASONS["D_NEG_LOSS"]))
                    status = "异常-不可结算"
                    blocked.add((station, day))
            else:
                customer_loss = gap
                pending_aux = Decimal(0)
                if gap < 0:
                    codes.append(("D_NEG_LOSS", DAILY_REASONS["D_NEG_LOSS"]))
                    status = "异常-不可结算"
                    blocked.add((station, day))

        rows.append({
            "station": station,
            "biz_date": day.isoformat(),
            "status": status,
            "meter_kwh": _q(meter_energy),
            "order_kwh": _q(order_energy),
            "aux_kwh": _q(aux_energy),                 # None 表示未回传，而非 0
            "customer_loss_kwh": _q(customer_loss),    # 待补态不确认客户损耗 -> None
            "pending_aux_kwh": _q(pending_aux),        # 未回传部分的明确交代
            "gap_kwh": _q(gap),
            "order_count": len(orders),
            "source_lines": sorted({r["line"] for r in meters + orders + auxs}),
            "codes": [{"code": c, "reason": m} for c, m in codes],
        })
    return rows, blocked


def reconcile(row):
    """四方对账：校验恒等式并返回逐项核对结果。None（未知）单独标注，不参与求和。"""
    def dec(value):
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))

    checks = []

    def add(name, value, ok, note=""):
        checks.append({"item": name, "kwh": None if value is None else str(_q(dec(value))),
                       "pass": bool(ok), "note": note})

    meter = dec(row["meter_kwh"])
    order = dec(row["order_kwh"])
    aux = dec(row["aux_kwh"])
    loss = dec(row["customer_loss_kwh"])
    pending = dec(row["pending_aux_kwh"])

    add("总表电量", meter, meter is not None, "唯一总表" if meter is not None else "未回传")
    add("订单电量", order, order is not None, f'{row["order_count"]} 张订单求和')
    add("站方辅助用电", aux, aux is not None, "已回传" if aux is not None else "未回传，不作零")
    add("客户损耗", loss, loss is not None, "待补态暂不确认" if loss is None else "总表-订单-辅助")
    add("待补辅助用电", pending, pending is not None, "未回传部分挂账" if pending is not None else "无待补")

    balanced = False
    if meter is not None and order is not None:
        rhs = order + (aux if aux is not None else Decimal(0)) \
            + (loss if loss is not None else Decimal(0)) \
            + (pending if pending is not None else Decimal(0))
        balanced = (meter == rhs)
    return {"checks": checks, "identity_pass": balanced,
            "identity": "总表 = 订单 + 客户损耗 + 辅助用电 + 待补辅助用电"}
