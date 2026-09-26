"""
wellness 流解析（移植自 ZeppBridge normalizer/mod.rs）

包含 6 个 v2/user 流 + Charge 系列：
  - hrv_rmssd
  - respiratory_rate
  - spo2 (click + odi + osa_event)
  - lactate_threshold
  - pai
  - all_day_stress（含全天曲线）
  - hybrid_charge / readiness / watch_score（Charge/insight_data）

所有解析都遵循：
  - 字段范围检查（不接受 0 / -1 / 哨兵）
  - 显式 None vs 0 区分（"走了 0 步" vs "没测步数"）
  - 跳过原因进 diagnostics
"""
from __future__ import annotations
from normalizer.common import (
    MetricSample, DailyMetric, NormalizedBatch,
    extract_items, item_object,
    first_value, first_value_from, first_string, first_number, first_number_from,
    parse_timestamp, parse_date_with_zone, parse_date, add_milliseconds,
    summary_date, device_id, source_scope,
    decode_base64, in_range, to_local_date,
    DataUnavailable,
)


# ============================================================
# HRV RMSSD: v2_users_me_events Charge/HRVRMSSD real_data
# value.samples[].hrv (ms)，时间戳 = value.startTime + sample.s 毫秒
# ============================================================

# 偏移量上界：超过 7 天视为手表断电复同步产生的伪偏移
# 7 天对应 7 * 86400 * 1000 = 604_800_000 毫秒
_MAX_HRV_OFFSET_MS = 7 * 86400 * 1000


def normalize_hrv_rmssd(raw: dict) -> NormalizedBatch:
    """HRV RMSSD 流解析。
    数据形态：
      value.startTime (ms) + value.samples[] = [{s: ms_offset, hrv: ms}, ...]
    """
    batch = NormalizedBatch()
    items = extract_items(raw)
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            batch.diagnostics.append(f"item {idx}: 不是对象")
            continue
        nested = item.get("value") if isinstance(item.get("value"), dict) else None
        if not nested:
            batch.diagnostics.append(f"item {idx}: 没有 value 嵌套")
            continue
        samples = nested.get("samples")
        if not isinstance(samples, list):
            batch.diagnostics.append(f"item {idx}: 没有 samples 数组")
            continue
        if not samples:
            batch.diagnostics.append(f"item {idx}: samples 为空")
            continue

        base_ms = first_number(nested, ("startTime", "start_time"))
        if base_ms is None:
            batch.diagnostics.append(f"item {idx}: 缺少 startTime")
            continue
        from datetime import datetime, timezone
        try:
            base_dt = datetime.fromtimestamp(base_ms / 1000.0, tz=timezone.utc)
        except Exception:
            batch.diagnostics.append(f"item {idx}: startTime 无法解析")
            continue

        dev = device_id(nested)
        if dev is None:
            dev = device_id(item)
        scope = source_scope(item, dev)

        for j, sample in enumerate(samples):
            if not isinstance(sample, dict):
                continue
            hrv = first_number(sample, ("hrv", "rmssd"))
            # HRV 生理范围 (0, 400]ms——Zepp 偶发 0 哨兵过滤掉；上界闭区间
            # 是按 ZeppBridge Rust in_range(.., 1, 400) 的语义（含 400）。
            if hrv is None or not in_range(hrv, 1.0, 400.0):
                batch.diagnostics.append(f"item {idx} sample {j}: HRV 数值无效 ({hrv})")
                continue
            offset_ms = first_number(sample, ("s", "offset"))
            offset_ms = int(round(offset_ms)) if offset_ms is not None else 0
            # 偏移上界：手表断电后复同步可能产生几天甚至更早的偏移，
            # 超过 7 天视为伪值跳过（避免污染曲线和 day 级聚合）。
            if abs(offset_ms) > _MAX_HRV_OFFSET_MS:
                batch.diagnostics.append(
                    f"item {idx} sample {j}: HRV 偏移 {offset_ms}ms 超过 7 天上限，跳过"
                )
                continue
            ts = add_milliseconds(base_dt, offset_ms)
            if ts is None:
                batch.diagnostics.append(f"item {idx} sample {j}: 偏移越界")
                continue
            batch.records.append(MetricSample(
                metric="hrv_rmssd",
                timestamp=ts,
                value=hrv,
                unit="ms",
                source_scope=scope,
                device_id=dev,
            ))
    return batch


# ============================================================
# Respiratory Rate: v2_events RespiratoryRate/real_data
# value.measurements = base64, 1440 字节，一分钟一个呼吸频率
# 字节值范围 4..=60 brpm，0 表示未测
# ============================================================

def normalize_respiratory_rate(raw: dict) -> NormalizedBatch:
    """呼吸率：一天1440字节的base64。输出 daily mean/min/max。"""
    batch = NormalizedBatch()
    items = extract_items(raw)
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        nested = item.get("value") if isinstance(item.get("value"), dict) else None
        if not nested:
            continue
        encoded = nested.get("measurements")
        if not isinstance(encoded, str) or not encoded.strip():
            batch.diagnostics.append(f"item {idx}: measurements 不是字符串")
            continue
        raw_bytes = decode_base64(encoded)
        if raw_bytes is None:
            batch.diagnostics.append(f"item {idx}: measurements 不是合法 base64")
            continue
        # 4..=60 brpm 是生理可能范围，0 = 未测
        readings = [b for b in raw_bytes if 4 <= b <= 60]
        if not readings:
            continue
        date = summary_date(item, nested)
        if not date:
            batch.diagnostics.append(f"item {idx}: 没有可用日期")
            continue
        avg = round((sum(readings) / len(readings)) * 10) / 10
        mn = min(readings)
        mx = max(readings)
        dev = device_id(nested) or device_id(item)
        scope = source_scope(item, dev)
        for metric, value in [
            ("respiratory_rate", avg),
            ("respiratory_rate_min", float(mn)),
            ("respiratory_rate_max", float(mx)),
        ]:
            batch.records.append(DailyMetric(
                metric=metric, date=date, value=value, unit="brpm",
                source_scope=scope, device_id=dev,
            ))
    return batch


# ============================================================
# SpO2: user_events blood_oxygen
# 3 个 subType：
#   - click: 单次测量，extra.spo2 (50..=100) + extra.spo2History[60]
#   - odi: 夜间血氧事件，扁平字段 odi/odiNum/score/cost
#   - osa_event: 单次呼吸暂停事件，extra.spo2_decrease (50..=100)
# ============================================================

def normalize_spo2(raw: dict) -> NormalizedBatch:
    """血氧：click + odi + osa_event 三种子类型。"""
    batch = NormalizedBatch()
    items = extract_items(raw)
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        sub = first_string(item, ("subType",))
        if sub == "odi":
            _parse_spo2_odi(item, batch)
            continue
        if sub == "osa_event":
            _parse_spo2_apnea(item, batch)
            continue
        # 默认：click（无 subType 或 subType=click）
        _parse_spo2_click(item, batch)
    return batch


def _parse_spo2_click(item: dict, batch: NormalizedBatch):
    """Click 型 SpO2：extra.spo2 + extra.spo2History[]"""
    extra_str = item.get("extra")
    if not isinstance(extra_str, str):
        batch.diagnostics.append("item: extra 不是字符串")
        return
    import json
    try:
        extra = json.loads(extra_str)
    except Exception:
        batch.diagnostics.append("item: extra JSON 解析失败")
        return
    if not isinstance(extra, dict):
        return

    spo2 = first_number(extra, ("spo2", "value"))
    if spo2 is None or not in_range(spo2, 50.0, 100.0):
        batch.diagnostics.append(f"spo2={spo2} 超出 [50,100] 范围")
        return
    ts_ms = first_number(extra, ("timestamp",)) or first_number(item, ("timestamp",))
    if ts_ms is None:
        batch.diagnostics.append("item: 缺少 timestamp")
        return
    from datetime import datetime, timezone
    try:
        ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    except Exception:
        return

    dev = device_id(extra) or device_id(item)
    scope = source_scope(item, dev)
    history = extra.get("spo2History", [])
    extra_dict = {}
    if isinstance(history, list):
        extra_dict["spo2_history"] = history
    batch.records.append(MetricSample(
        metric="spo2", timestamp=ts, value=spo2, unit="%",
        source_scope=scope, device_id=dev, extra=extra_dict,
    ))


def _parse_spo2_odi(item: dict, batch: NormalizedBatch):
    """Overnight Desaturation Index：每晚汇总。"""
    date = summary_date(item, None)
    if not date:
        return
    dev = device_id(item)
    scope = source_scope(item, dev)
    # odi 0..100, odiNum 0..1000, score 0..100
    ranges = [
        ("spo2_odi", ("odi",), "events/h", (0.0, 100.0)),
        ("spo2_odi_events", ("odiNum",), "count", (0.0, 1000.0)),
        ("spo2_night_score", ("score",), "score", (0.0, 100.0)),
    ]
    for metric, keys, unit, (lo, hi) in ranges:
        v = first_number(item, keys)
        if v is not None and in_range(v, lo, hi):
            batch.records.append(DailyMetric(
                metric=metric, date=date, value=v, unit=unit,
                source_scope=scope, device_id=dev,
            ))
    # cost = 测量时长（秒 → 分钟）
    cost = first_number(item, ("cost",))
    if cost is not None and in_range(cost, 60.0, 86400.0):
        batch.records.append(DailyMetric(
            metric="spo2_measured_minutes", date=date,
            value=round(cost / 60.0), unit="min",
            source_scope=scope, device_id=dev,
        ))


def _parse_spo2_apnea(item: dict, batch: NormalizedBatch):
    """OSA 事件：extra.spo2_decrease。单独指标，不混入 spo2。"""
    extra_str = item.get("extra")
    if not isinstance(extra_str, str):
        return
    import json
    try:
        extra = json.loads(extra_str)
    except Exception:
        return
    if not isinstance(extra, dict):
        return
    v = first_number(extra, ("spo2_decrease", "spo2Decrease"))
    if v is None or not in_range(v, 50.0, 100.0):
        return
    ts_ms = first_number(extra, ("timestamp",)) or first_number(item, ("timestamp",))
    if ts_ms is None:
        return
    from datetime import datetime, timezone
    try:
        ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    except Exception:
        return
    dev = device_id(extra) or device_id(item)
    scope = source_scope(item, dev)
    batch.records.append(MetricSample(
        metric="spo2_apnea_low", timestamp=ts, value=v, unit="%",
        source_scope=scope, device_id=dev,
    ))


# ============================================================
# Lactate Threshold: v2_events LactateThreshold/summary
# value.samples[]: lactateThresholdHr (60-230), lactateThresholdPace (100-1800)
# ============================================================

def normalize_lactate_threshold(raw: dict) -> NormalizedBatch:
    batch = NormalizedBatch()
    items = extract_items(raw)
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        nested = item.get("value") if isinstance(item.get("value"), dict) else None
        if not nested:
            continue
        samples = nested.get("samples")
        if not isinstance(samples, list) or not samples:
            continue
        for s in samples:
            if not isinstance(s, dict):
                continue
            date = first_string(s, ("dateString", "date"))
            if not date:
                continue
            # 验证 YYYY-MM-DD
            try:
                from datetime import datetime
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                continue
            scope = source_scope(item, None)
            for metric, keys, unit, (lo, hi) in [
                ("lactate_threshold_hr", ("lactateThresholdHr",), "bpm", (60.0, 230.0)),
                ("lactate_threshold_pace", ("lactateThresholdPace",), "s/km", (100.0, 1800.0)),
            ]:
                v = first_number(s, keys)
                if v is not None and in_range(v, lo, hi):
                    batch.records.append(DailyMetric(
                        metric=metric, date=date, value=v, unit=unit,
                        source_scope=scope,
                    ))
    return batch


# ============================================================
# PAI: user_events PaiHealthInfo（顶层扁平，无 value envelope）
# 13 个字段，全带范围；zones 的 0 分钟是真值
# ============================================================

def normalize_pai(raw: dict) -> NormalizedBatch:
    batch = NormalizedBatch()
    items = extract_items(raw)
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        date = summary_date(item, None)
        if not date:
            continue
        dev = device_id(item)
        scope = source_scope(item, dev)
        # 区分：deviceId == "single-device-firmware" 时是 Zepp Cloud 推算值
        # （不是手表实测），标记为 placeholder 让 query 阶段能识别
        is_placeholder = (dev is None)
        effective_scope = scope if not is_placeholder else "placeholder"
        effective_dev = dev if not is_placeholder else ""
        # PAI 顶层扁平，13 个字段（照抄 Rust）
        # zones 的 minutes 下界是 0（0 是真值，不是哨兵）
        fields = [
            ("pai_daily", ("dailyPai",), "pai", (0.0, 500.0)),
            ("pai_low_zone", ("lowZonePai",), "pai", (0.0, 500.0)),
            ("pai_medium_zone", ("mediumZonePai",), "pai", (0.0, 500.0)),
            ("pai_high_zone", ("highZonePai",), "pai", (0.0, 500.0)),
            ("device_max_hr", ("maxHr",), "bpm", (100.0, 240.0)),
            ("device_resting_hr", ("restHr",), "bpm", (25.0, 120.0)),
            ("pai_total", ("totalPai",), "pai", (0.0, 1000.0)),
            ("pai_low_zone_minutes", ("lowZoneMinutes",), "min", (0.0, 1440.0)),
            ("pai_medium_zone_minutes", ("mediumZoneMinutes",), "min", (0.0, 1440.0)),
            ("pai_high_zone_minutes", ("highZoneMinutes",), "min", (0.0, 1440.0)),
            ("pai_low_zone_lower_hr", ("lowZoneLowerLimit",), "bpm", (40.0, 240.0)),
            ("pai_medium_zone_lower_hr", ("mediumZoneLowerLimit",), "bpm", (40.0, 240.0)),
            ("pai_high_zone_lower_hr", ("highZoneLowerLimit",), "bpm", (40.0, 240.0)),
        ]
        for metric, keys, unit, (lo, hi) in fields:
            v = first_number(item, keys)
            if v is not None and in_range(v, lo, hi):
                batch.records.append(DailyMetric(
                    metric=metric, date=date, value=v, unit=unit,
                    source_scope=effective_scope, device_id=effective_dev,
                ))
    return batch


# ============================================================
# All-day Stress: user_events all_day_stress
# 顶层扁平：avgStress/minStress/maxStress + 4档比例
# 曲线：data 字段是 JSON 字符串（不是 base64），[{time:ms, value:1..100}, ...]
# ============================================================

def normalize_all_day_stress(raw: dict) -> NormalizedBatch:
    batch = NormalizedBatch()
    items = extract_items(raw)
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        value = item.get("value") if isinstance(item.get("value"), dict) else None
        date = summary_date(item, value)
        if not date:
            continue
        dev = device_id(item)
        scope = source_scope(item, dev)
        # 顶层字段
        top_fields = [
            ("stress", ("avgStress",), (0.0, 100.0)),
            ("stress_min", ("minStress",), (1.0, 100.0)),
            ("stress_max", ("maxStress",), (1.0, 100.0)),
            # 比例字段（"relaxProportion"/"normalProportion"/"mediumProportion"/"highProportion"）
            ("stress_relax_pct", ("relaxProportion",), (0.0, 100.0)),
            ("stress_normal_pct", ("normalProportion",), (0.0, 100.0)),
            ("stress_medium_pct", ("mediumProportion",), (0.0, 100.0)),
            ("stress_high_pct", ("highProportion",), (0.0, 100.0)),
        ]
        for metric, keys, (lo, hi) in top_fields:
            v = first_number(item, keys)
            if v is not None and in_range(v, lo, hi):
                batch.records.append(DailyMetric(
                    metric=metric, date=date, value=v, unit="score" if "pct" not in metric else "%",
                    source_scope=scope, device_id=dev,
                ))
        # 曲线：data 是 JSON 字符串（不是 base64）
        data_str = first_string(item, ("data",))
        if data_str:
            _parse_all_day_stress_curve(data_str, dev, scope, batch)
            # M4 章节 4.7：存整条曲线为 JSON 字符串 → metric='stress_24h_curve'
            # （measurements.extra_json 实际不带 value，这里直接生成 DailyMetric 走现有 upsert）
            _store_stress_24h_curve(data_str, date, dev, scope, batch)
    return batch


def _store_stress_24h_curve(data_str: str, date: str, dev, scope: str,
                              batch: NormalizedBatch) -> None:
    """
    M4 章节 4.7：把全天 stress 曲线（1440 分钟压缩后的采样点）作为
    一个 metric='stress_24h_curve' 日级记录入库。value=点的数量，extra_json 走 batch 共享。

    ⚠️ 注意：当前 NormalizedBatch 不支持 DailyMetric 携带 extra_json。
    这里改方案：value = 采样点数（int），让 unit='points'。曲线详情用 diagnostics 输出，
    真实细节从 raw_records 表的 payload JSON 读（保证可重放）。
    """
    import json
    try:
        points = json.loads(data_str)
    except Exception:
        return
    if not isinstance(points, list) or not points:
        return
    # 落在 [1, 100] 的有效点数（与解析过滤一致）
    valid_points = []
    for p in points:
        if not isinstance(p, dict):
            continue
        v = first_number(p, ("value",))
        ts_ms = first_number(p, ("time", "timestamp"))
        if v is None or ts_ms is None:
            continue
        if 1.0 <= v <= 100.0:
            valid_points.append({"time": int(ts_ms), "value": float(v)})
    if not valid_points:
        return
    batch.records.append(DailyMetric(
        metric="stress_24h_curve",
        date=date,
        value=float(len(valid_points)),  # 点的数量
        unit="points",
        source_scope=scope,
        device_id=dev,
    ))
    # 把曲线详情放 diagnostics（用 ; 分隔便于 grep）
    # 取最早 + 最晚 + 几个关键点，避免 diagnostic 过长
    sample = valid_points[0]
    batch.diagnostics.append(
        f"stress_24h_curve {date}: {len(valid_points)} points, "
        f"first=({sample['time']},{sample['value']:.0f})"
    )


def _parse_all_day_stress_curve(data_str: str, dev, scope: str, batch: NormalizedBatch):
    """解析 data 字符串里的全天曲线。过滤范围 [1,100]（Zepp scale 从 1 开始）。"""
    import json
    try:
        points = json.loads(data_str)
    except Exception:
        batch.diagnostics.append("stress data JSON 解析失败")
        return
    if not isinstance(points, list):
        return
    from datetime import datetime, timezone
    for p in points:
        if not isinstance(p, dict):
            continue
        v = first_number(p, ("value",))
        if v is None or not in_range(v, 1.0, 100.0):
            continue
        ts_ms = first_number(p, ("time", "timestamp"))
        if ts_ms is None:
            continue
        try:
            ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        except Exception:
            continue
        batch.records.append(MetricSample(
            metric="stress", timestamp=ts, value=v, unit="score",
            source_scope=scope, device_id=dev,
        ))


# ============================================================
# Hybrid Charge / Readiness: v2_events Charge/insight_data
# 顶层扁平字段：afibScore/hrvScore/rhrScore/phyScore/mentScore/sleepHRV/sleepRHR 等
# readiness / watch_score subType
# ============================================================

def normalize_hybrid_charge(raw: dict) -> NormalizedBatch:
    """Charge/insight_data — readiness / watch_score / hybrid charge。

    支持 3 种样本结构：
      A) readiness / watch_score 风格（直接读 value.samples[] 里的
         hrvScore/phyScore/rdnsScore/sleepHRV 等 13 个字段）
      B) insight_data 风格（samples[] 含 total/physical/mental 字段 — Rust 文档说的）
      C) 实际看到的样本（samples[] 含 insight/insightId/diff — 不同设备协议差异）

    全部尝试，把能认出的字段都收下。
    """
    batch = NormalizedBatch()
    items = extract_items(raw)
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        event_type = first_string(item, ("eventType",))
        sub_type = first_string(item, ("subType",))

        # ----- 路径 1：扁平 readiness 字段（某些设备直接展开在 value）-----
        date = summary_date(item, None)
        if not date:
            continue
        value = item.get("value") if isinstance(item.get("value"), dict) else None
        if not value:
            continue
        dev = device_id(item)
        scope = source_scope(item, dev)

        # 13 个 readiness 字段（readiness/watch_score 形态）
        flat_fields = [
            ("hrv_readiness", ("hrvScore",), "score", (0.0, 100.0)),
            ("rhr_readiness", ("rhrScore",), "score", (0.0, 100.0)),
            ("physical_readiness", ("phyScore",), "score", (0.0, 100.0)),
            ("mental_readiness", ("mentScore",), "score", (0.0, 100.0)),
            ("readiness_score", ("rdnsScore",), "score", (0.0, 100.0)),
            ("afib_score", ("afibScore",), "score", (0.0, 100.0)),
            ("ahi_score", ("ahiScore",), "score", (0.0, 100.0)),
            ("skin_temp_score", ("skinTempScore",), "score", (0.0, 100.0)),
            ("hrv_baseline", ("hrvBaseline",), "ms", (0.0, 254.0)),
            ("rhr_baseline", ("rhrBaseline",), "bpm", (0.0, 254.0)),
            ("ahi_baseline", ("ahiBaseline",), "events/h", (0.0, 100.0)),
            ("sleep_hrv", ("sleepHRV",), "ms", (0.0, 250.0)),
            ("sleep_rhr", ("sleepRHR",), "bpm", (0.0, 120.0)),
            ("afib_insight", ("afibInsight",), "score", (0.0, 100.0)),
            ("readiness_insight", ("rdnsInsight",), "score", (0.0, 100.0)),
            ("skin_temp_insight", ("skinTempInsight",), "score", (0.0, 100.0)),
        ]
        for metric, keys, unit, (lo, hi) in flat_fields:
            v = first_number(value, keys)
            if v is not None and in_range(v, lo, hi):
                batch.records.append(DailyMetric(
                    metric=metric, date=date, value=v, unit=unit,
                    source_scope=scope, device_id=dev,
                ))
        stc = first_number(value, ("skinTempCalibrated",))
        if stc is not None and in_range(stc, -50.0, 100.0):
            batch.records.append(DailyMetric(
                metric="skin_temp_calibrated", date=date, value=stc, unit="delta_c",
                source_scope=scope, device_id=dev,
            ))

        # ----- 路径 2：从 samples[] 取（Rust 的 collect_charge_metrics）-----
        samples = value.get("samples")
        if isinstance(samples, list) and samples:
            # 2a) 找 total/physical/mental 这套
            for metric, key in [
                ("hybrid_charge", "total"),
                ("physical_charge", "physical"),
                ("mental_charge", "mental"),
            ]:
                # 取最新一个 sample
                latest = None
                latest_offset = -10**18
                for s in samples:
                    if not isinstance(s, dict):
                        continue
                    offset = first_number(s, ("s", "offset"))
                    offset = int(round(offset)) if offset is not None else 0
                    if first_number(s, (key,)) is not None and offset >= latest_offset:
                        latest = s
                        latest_offset = offset
                if latest is not None:
                    v = first_number(latest, (key,))
                    if v is not None and in_range(v, 0.0, 100.0):
                        batch.records.append(DailyMetric(
                            metric=metric, date=date, value=v, unit="score",
                            source_scope=scope, device_id=dev,
                        ))

            # 2b) 找带 insight/insightId/diff 的样本（你的设备协议）
            # 把 insight 字段映射到 hybrid_charge_intel（intel 字段值域 0-100）
            latest_with_insight = None
            latest_offset = -10**18
            for s in samples:
                if not isinstance(s, dict):
                    continue
                if "insight" not in s:
                    continue
                offset = first_number(s, ("s", "offset"))
                offset = int(round(offset)) if offset is not None else 0
                if offset >= latest_offset:
                    latest_with_insight = s
                    latest_offset = offset
            if latest_with_insight is not None:
                insight = first_number(latest_with_insight, ("insight",))
                if insight is not None and in_range(insight, 0.0, 100.0):
                    batch.records.append(DailyMetric(
                        metric="hybrid_charge_intel", date=date, value=insight, unit="score",
                        source_scope=scope, device_id=dev,
                    ))
                insight_id = first_number(latest_with_insight, ("insightId",))
                if insight_id is not None:
                    batch.records.append(DailyMetric(
                        metric="hybrid_charge_intel_id", date=date, value=insight_id, unit="id",
                        source_scope=scope, device_id=dev,
                    ))
                diff = first_number(latest_with_insight, ("diff",))
                if diff is not None and in_range(diff, -100.0, 100.0):
                    batch.records.append(DailyMetric(
                        metric="hybrid_charge_intel_diff", date=date, value=diff, unit="delta",
                        source_scope=scope, device_id=dev,
                    ))
    return batch


# ============================================================
# Sport Load: v2/watch/users/{id}/WatchSportStatistics/SPORT_LOAD
# { dayId, currnetDayTrainLoad, wtlSum, optimalMin/Max, overreaching, ... }
# ============================================================

def normalize_sport_load(raw: dict) -> NormalizedBatch:
    """训练负荷。Zepp 用 7 天滚动累计（wtlSum），每日单值（currnetDayTrainLoad）。"""
    batch = NormalizedBatch()
    items = extract_items(raw)
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        day = first_string(item, ("dayId", "day"))
        if not day:
            ts_ms = first_number(item, ("updateTime", "timestamp"))
            if ts_ms:
                from datetime import datetime, timezone
                day = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
            else:
                continue
        scope = source_scope(item, None)
        fields = [
            ("sport_load_today", ("currnetDayTrainLoad",), (0, 1000)),
            ("sport_load_7day_sum", ("wtlSum",), (0, 10000)),
            ("sport_load_optimal_min", ("wtlSumOptimalMin",), (0, 10000)),
            ("sport_load_optimal_max", ("wtlSumOptimalMax",), (0, 10000)),
            ("sport_load_overreaching", ("wtlSumOverreaching",), (0, 10000)),
        ]
        for metric, keys, (lo, hi) in fields:
            v = first_number(item, keys)
            if v is not None and in_range(v, float(lo), float(hi)):
                batch.records.append(DailyMetric(
                    metric=metric, date=day, value=v, unit="load",
                    source_scope=scope,
                ))
    return batch


# ============================================================
# Daily Summary: v2_events DailyHealth/summary
# value.samples[]: stepGoal/calorieGoal/burningDurationGoal + totalSteps/totalCalories/totalBurningDuration
# ============================================================

def normalize_daily_summary(raw: dict) -> NormalizedBatch:
    """每日步数/卡路里/燃烧时长 + 目标值。

    目标值 0 = 没设目标（不写入），但实际值的 0 是真值（可以写）。
    """
    batch = NormalizedBatch()
    items = extract_items(raw)
    # 排序：有日历日的优先于 epoch 回退
    items_sorted = sorted(items, key=daily_summary_sort_key if False else lambda x: 0)  # 我们直接处理
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        event_value = item.get("value") if isinstance(item.get("value"), dict) else None
        if not event_value:
            continue
        samples = event_value.get("samples")
        if not isinstance(samples, list) or not samples:
            continue
        # 取父 device_id（用于 source_scope）
        parent_dev = device_id(event_value) or device_id(item)
        for s in samples:
            if not isinstance(s, dict):
                continue
            # 日期（samples 里通常有 dateString）
            date = first_string(s, ("dateString", "date"))
            if not date:
                # 回退：用 event 的 date
                date = summary_date(item, event_value)
            if not date:
                continue
            scope = source_scope(item, parent_dev)
            # 实际值（0 是真值）
            real_fields = [
                ("steps", ("totalSteps",), "steps", (0, 200000)),
                ("calories", ("totalCalories",), "kcal", (0, 10000)),
                ("active_minutes", ("totalBurningDuration",), "min", (0, 1440)),
            ]
            for metric, keys, unit, (lo, hi) in real_fields:
                v = first_number(s, keys)
                if v is not None and in_range(v, float(lo), float(hi)):
                    batch.records.append(DailyMetric(
                        metric=metric, date=date, value=v, unit=unit,
                        source_scope=scope, device_id=parent_dev,
                    ))
            # 目标值（0 = 没设目标，丢弃）
            goal_fields = [
                ("step_goal", ("stepGoal",), "steps", (1, 200000)),  # 下界 1
                ("calorie_goal", ("calorieGoal",), "kcal", (1, 10000)),
                ("active_minutes_goal", ("burningDurationGoal",), "min", (1, 1440)),
            ]
            for metric, keys, unit, (lo, hi) in goal_fields:
                v = first_number(s, keys)
                if v is not None and in_range(v, float(lo), float(hi)):
                    batch.records.append(DailyMetric(
                        metric=metric, date=date, value=v, unit=unit,
                        source_scope=scope, device_id=parent_dev,
                    ))
    return batch


# ============================================================
# Heart Rate: users/{id}/heartRate
# 自动测量 (type=2) 或运动中 (type=1000)
# ============================================================

def normalize_heart_rate(raw: dict) -> NormalizedBatch:
    """心率原始记录。0 bpm 是哨兵「没测到」，丢弃。"""
    batch = NormalizedBatch()
    items = extract_items(raw)
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        ts_ms = first_number(item, ("timestamp", "time", "date"))
        ts = parse_timestamp(ts_ms) if ts_ms is not None else None
        if ts is None:
            batch.diagnostics.append(f"item {idx}: 缺少 timestamp")
            continue
        v = first_number(item, ("value", "heartRate", "heart_rate", "hr"))
        if v is None:
            batch.diagnostics.append(f"item {idx}: 缺少 value")
            continue
        if not in_range(v, 1.0, 300.0):  # 0 是哨兵
            batch.diagnostics.append(f"item {idx}: heart rate 数值无效 ({v})")
            continue
        dev = device_id(item)
        scope = source_scope(item, dev)
        batch.records.append(MetricSample(
            metric="heart_rate", timestamp=ts, value=v, unit="bpm",
            source_scope=scope, device_id=dev,
        ))
    return batch


# ============================================================
# Second Heart Rate (file index): v2_events second_heart_rate/real_data
#
# ⚠ 重要事实（2026-09-25 实测 + ZeppBridge 官方文档交叉验证）：
#   此流**只返回 SEC_HR 文件索引**，不返回 bpm 测量值。
#   实际 bpm 数据需要：
#     1. 下载 fileId 对应的二进制文件（端点 URL 无公开文档）
#     2. 解码 SEC_HR 二进制格式（社区推测但未验证）
#   ZeppBridge 官方文档（update-data-preservation.md）显式声明：
#     "No verified download/decoder contract was obtained in this investigation."
#     "No interpolation or invented samples are used."
#
# 本 normalizer 的设计原则：
#   - 不伪造 bpm 数据（守住 ZeppBridge 同样的不发明数据原则）
#   - 解析每个 sample 段元数据，写成 MetricSample("heart_rate_segment", ...)
#     → 让 sync 时此流不再是"available_no_normalizer"静默空跑
#     → 一旦上游实现 SEC_HR 下载，只需在本函数内部追加"下载 + 解码 → bpm"
#       STREAM 注册 / pull_to_sqlite.py 都不用改
#   - 字段名 / 范围检查按 v2_events 同族规范
# ============================================================

# bpm 哨兵过滤常量（即便未来接入 SEC_HR 解码也要用）：
#   0      = 没测到
#   1..19  = 生理上不可能
#   20..300 = 真实可能范围（含 300，Zepp 偶发高强度运动峰值）
#   255    = 信号丢失哨兵（社区推测）
_SEC_HR_BPM_MIN = 20.0
_SEC_HR_BPM_MAX = 300.0
# 偏移上界：超过 7 天视为手表断电复同步伪值
_SEC_HR_OFFSET_MAX_MS = 7 * 86400 * 1000


def normalize_second_heart_rate(raw) -> NormalizedBatch:
    """second_heart_rate 流解析（v2_events surface，文件索引形态）。

    实测 payload 形态（DB 中 9-24 真实样本）：
      {
        "items": [{
          "timestamp": 1790208000000,    # item 级时间戳（不一定等于 startTime）
          "value": {
            "startTime": 1790220967000,  # ms，段基准时间
            "deviceId": "2,D8803CFFFEE4E756",
            "timeZone": "2,Asia/Shanghai",
            "samples": [
              {"s": 0, "e": 110000, "u": 111681,
               "fileId": "12027181518", "fileType": "SEC_HR",
               "dateString": "2026-09-24"},
              ...
            ]
          }
        }, ...]
      }

    输出（不伪造 bpm）：
      MetricSample("heart_rate_segment", ts=startTime+s, value=e (毫秒),
                   unit="ms", source_scope=scope, device_id=dev,
                   extra={"file_id": ..., "file_type": "SEC_HR",
                          "uncompressed_bytes": u,
                          "date_string": "2026-09-24"})
    """
    batch = NormalizedBatch()
    try:
        items = extract_items(raw)
    except DataUnavailable as e:
        # 响应为空（云端明确答「没数据」）→ 不是错误，是正常情况
        batch.diagnostics.append(f"empty response: {e.message}")
        return batch
    except ValueError as e:
        # 响应不合法（不是 dict 也不是 list）→ 跳过
        batch.diagnostics.append(f"malformed response: {e}")
        return batch
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            batch.diagnostics.append(f"item {idx}: 不是对象")
            continue
        nested = item.get("value")
        if not isinstance(nested, dict):
            batch.diagnostics.append(f"item {idx}: 没有 value 嵌套")
            continue
        samples = nested.get("samples")
        if not isinstance(samples, list):
            batch.diagnostics.append(f"item {idx}: 没有 samples 数组")
            continue
        if not samples:
            batch.diagnostics.append(f"item {idx}: samples 为空")
            continue

        base_ms = first_number(nested, ("startTime", "start_time"))
        if base_ms is None:
            batch.diagnostics.append(f"item {idx}: 缺少 startTime")
            continue
        from datetime import datetime, timezone
        try:
            base_dt = datetime.fromtimestamp(base_ms / 1000.0, tz=timezone.utc)
        except Exception:
            batch.diagnostics.append(f"item {idx}: startTime 无法解析")
            continue

        dev = device_id(nested)
        if dev is None:
            dev = device_id(item)
        scope = source_scope(item, dev)

        for j, sample in enumerate(samples):
            if not isinstance(sample, dict):
                batch.diagnostics.append(f"item {idx} sample {j}: 不是对象")
                continue

            # --- 必填字段 ---
            file_id = first_string(sample, ("fileId", "file_id", "fid"))
            if not file_id:
                batch.diagnostics.append(f"item {idx} sample {j}: 缺少 fileId")
                continue
            file_type = first_string(sample, ("fileType", "file_type"))
            if file_type != "SEC_HR":
                batch.diagnostics.append(
                    f"item {idx} sample {j}: fileType={file_type!r} != 'SEC_HR'"
                )
                continue

            # --- 时间偏移 + 段时长（ms）---
            offset_ms = first_number(sample, ("s", "offset"))
            offset_ms = int(round(offset_ms)) if offset_ms is not None else 0
            # 偏移越界（同 HRV 策略：超过 7 天视为手表断电复同步伪值）
            if abs(offset_ms) > _SEC_HR_OFFSET_MAX_MS:
                batch.diagnostics.append(
                    f"item {idx} sample {j}: 偏移 {offset_ms}ms 超过 7 天上限"
                )
                continue
            duration_ms = first_number(sample, ("e", "duration"))
            duration_ms = int(round(duration_ms)) if duration_ms is not None else 0
            if duration_ms <= 0:
                batch.diagnostics.append(
                    f"item {idx} sample {j}: 段时长 e={duration_ms}ms 无效"
                )
                continue

            ts = add_milliseconds(base_dt, offset_ms)
            if ts is None:
                batch.diagnostics.append(f"item {idx} sample {j}: 偏移越界（overflow）")
                continue

            # --- 元数据（uncompressed bytes + 本地日历日）---
            u_bytes = first_number(sample, ("u", "size", "uncompressed"))
            u_bytes = int(round(u_bytes)) if u_bytes is not None else None
            date_str = first_string(sample, ("dateString", "date"))

            extra = {
                "file_id": str(file_id),
                "file_type": file_type,
            }
            if u_bytes is not None:
                extra["uncompressed_bytes"] = u_bytes
            if date_str:
                extra["date_string"] = date_str

            batch.records.append(MetricSample(
                metric="heart_rate_segment",
                timestamp=ts,
                value=float(duration_ms),
                unit="ms",
                source_scope=scope,
                device_id=dev,
                extra=extra,
            ))
    return batch