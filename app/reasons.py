REASONS = {
  "COLUMN_COUNT":"字段数不符", "MISSING":"缺少必填值", "NUMBER":"数值格式错误",
  "RANGE":"数值越界", "UNIT":"单位不支持", "TIME":"时间缺少偏移或格式无效",
  "DUPLICATE":"相同记录重复", "CONFLICT":"同键记录冲突", "EMPTY":"文件没有数据",
  "HEADER_ONLY":"只有字段名", "BLANK_ONLY":"仅含空行", "STALE":"复核版本已更新",
  "STATE":"事件状态不符", "GAP":"采样缺口超限", "UNMAPPED":"未登记的业务编码",
  "EXPIRED":"已过有效期", "ORPHAN":"缺少关联记录", "OVERLAP":"有效区间重叠",
  "LIMIT":"业务阈值超限", "NEGATIVE":"出现负数", "CURVE":"曲线不满足单调条件",
  "REVIEW":"存在待复核差异", "INCOMPLETE":"关键记录不完整",
  # 按站按日日结原因码
  "D_HEADER":"日结字段名不符合契约", "D_KIND":"读数类型无效（仅 meter/order/aux）",
  "D_DATE":"业务日期格式错误（需 YYYY-MM-DD）", "D_UNIT":"电量单位无效（仅 kWh/MWh）",
  "D_ENERGY":"电量数值格式错误", "D_NEGATIVE":"电量为负数",
  "D_DUP_METER":"当日总表重复", "D_DUP_AUX":"当日辅助表重复",
  "D_MISS_METER":"当日总表未回传", "D_MISS_ORDER":"当日无订单数据",
  "D_MISS_AUX":"辅助表未回传，差额挂待补", "D_NEG_LOSS":"总表小于订单与辅助之和",
  "D_NOT_DAY":"读数未对齐自然日（+08:00）"
}
def rejected(row, code, detail=""):
    return {"line":row.get("_line", ""), "key":row.get("id", row.get("event_id", "")),
            "code":code, "reason":REASONS.get(code,code), "detail":detail,
            "raw":{k:v for k,v in row.items() if not k.startswith("_")}}
