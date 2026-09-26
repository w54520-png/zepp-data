"""
体重 / 体成分（weightRecords）流解析 —— M2 章节 2.2。

端点：GET /users/{user_id}/members/{member_id}/weightRecords
参数：fromTime / toTime（**秒**，不是毫秒）

【实测响应形态】（2026-09-23 实测 200 条）：
  {
    "items": [
      {
        "userId": "1000000000",
        "memberId": "-1",
        "deviceSource": 8519936,
        "generatedTime": 1789352772,    # **秒**（10 位）
        "weightType": 5,
        "deviceId": "AABBCCDDEEFF1122",
        "summary": {                    # ← **所有数据在 summary 子对象里**！
          "weight": 70.0,
          "bmi": 22.9,
          "height": 175.0,
          "fatRate": 20.5,              # 体脂率（%）
          "bodyWaterRate": 58.2,        # 水分率（%）
          "muscleRate": 51.1,           # 肌肉率（%），不是肌肉量
          "boneMass": 3.7,              # 骨量（kg）
          "proteinRatio": 15.7,         # 蛋白质率（%）
          "visceralFat": 8.0,
          "metabolism": 1553.0,          # BMR（kcal）
          "skeletalMuscle": 30.8,       # 骨骼肌（kg）
          "bodyBalanceScore": 85,
          "timeZone": "Asia/Seoul"      # 也可能是 "Asia/Shanghai" / "28800000" / None
        }
      }
    ]
  }

【generatedTime 单位】（M2 拍板）：
  - **秒**（不是 ms）—— 与 heart_rate / v2_events / user_events 都不同
  - 不调用通用 parse_timestamp（会当秒处理）
  - 防御性：若 >= 10^10 视为毫秒，先 ÷1000

【timeZone 三种形态】（M2 拍板）：
  1. "28800000"（字符串毫秒） → offset_from_number 自动 ÷1000 → 28800
  2. "Asia/Shanghai" / "Asia/Seoul" → parse_timezone_text
  3. 28800 / 28800000 (int/float) → offset_from_number
  4. None / 无法解析 → 默认 Asia/Shanghai (28800)（M2 兜底）

【11 个 BodyMetric】（M2 拍板 / 实测字段对齐）：
  - weight (kg)              stream='weight'
  - bmi (kg/m²)              stream='weight'
  - height (cm)              stream='weight'
  - body_fat_rate (%)         api_alpha→「fatRate」字段
  - body_water_rate (%)        stream='weight'
  - muscle_mass (kg)         → 用「skeletalMuscle」字段（kg，肌肉量）
                            「muscleRate」是肌肉率（%），不是 kg
  - bone_mass (kg)           stream='weight'
  - protein_rate (%)         → 「proteinRatio」字段
  - visceral_fat (level)     stream='weight'
  - bmr (kcal)               → 「metabolism」字段
  - body_balance_score       stream='weight'

【写入策略】（Q2 拍板）：
  - 全部写 measurements 表，stream='weight'，metric=<各子 metric>
  - BMR 写 metric='bmr'，**不**自动覆盖 compute_calorie_total.py 的计算值
    （compute_calorie_total.py 加 --bmr-source zepp_api 选项，**不**默认开启）
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from normalizer.common import (
    MetricSample, NormalizedBatch,
    first_value, first_number,
    parse_timezone_text, offset_from_number,
    extract_items, item_object,
    to_local_date,
)


# ===== BodyMetric 定义（M2 拍板 + 实测字段对齐）=====

# (metric_name, API key(s), unit, (lo, hi))
BODY_METRICS = [
    ("weight",            ("weight",),              "kg",    (0.5, 600.0)),     # 主指标
    ("bmi",               ("bmi",),                 "kg/m2", (10.0, 100.0)),
    ("height",            ("height",),              "cm",    (50.0, 250.0)),
    ("body_fat_rate",     ("fatRate", "bodyFatRate"), "%",   (3.0, 70.0)),
    ("body_water_rate",   ("bodyWaterRate", "waterRate"), "%", (20.0, 90.0)),
    ("muscle_mass",       ("skeletalMuscle", "muscleMass"), "kg", (0.5, 200.0)),  # 用骨骼肌
    ("bone_mass",         ("boneMass",),            "kg",    (0.1, 20.0)),
    ("protein_rate",      ("proteinRatio", "proteinRate"), "%", (5.0, 60.0)),
    ("visceral_fat",      ("visceralFat",),         "level", (0.5, 50.0)),
    ("bmr",               ("metabolism", "bmr"),    "kcal",  (500.0, 5000.0)),
    ("body_balance_score",("bodyBalanceScore", "balanceScore"), "score", (0.0, 100.0)),
]


# ===== 顶层入口 =====

def normalize_weight(raw: dict) -> NormalizedBatch:
    """解析 weightRecords 流，输出 MetricSample 列表。"""
    batch = NormalizedBatch()
    items = extract_items(raw)
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            batch.diagnostics.append(f"item {idx}: not a dict")
            continue

        # generatedTime：必须是秒（10 位数字）
        gen_ts_s = first_number(item, ("generatedTime", "generateTime", "time", "timestamp"))
        if gen_ts_s is None or gen_ts_s <= 0:
            batch.diagnostics.append(f"item {idx}: 缺 generatedTime")
            continue
        # 区分秒 vs 毫秒（防御性）
        if abs(gen_ts_s) >= 10_000_000_000:
            gen_ts_s = gen_ts_s / 1000.0
        try:
            ts = datetime.fromtimestamp(gen_ts_s, tz=timezone.utc)
        except Exception as e:
            batch.diagnostics.append(f"item {idx}: generatedTime 无法解析 ({e})")
            continue

        # summary 子对象（实测所有指标都在 summary 里）
        # 但也兼容顶层（不同 Zepp 版本的兼容）
        summary_obj = item.get("summary")
        if not isinstance(summary_obj, dict):
            # 没 summary 子对象 — 用 item 自身
            summary_obj = item

        # 解析 timeZone（3 种形态 + None 兜底）
        tz_value = first_value(summary_obj, ("timeZone", "time_zone", "tz"))
        tz_offset_secs = _parse_tz_offset(tz_value)
        if tz_offset_secs is None:
            tz_offset_secs = 8 * 3600  # 默认 Asia/Shanghai（M2 兜底）

        # 遍历 BODY_METRICS（在 summary 子对象里找）
        for metric, keys, unit, (lo, hi) in BODY_METRICS:
            v = first_number(summary_obj, keys)
            if v is None:
                continue
            if not (lo <= v <= hi):
                batch.diagnostics.append(
                    f"item {idx} {metric}={v} 超出 [{lo},{hi}]，跳过"
                )
                continue
            from normalizer.common import device_id as _did, source_scope as _ssc
            dev = _did(item)  # device 在顶层
            scope = _ssc(item, dev)
            batch.records.append(MetricSample(
                metric=metric,
                timestamp=ts,
                value=float(v),
                unit=unit,
                source_scope=scope,
                device_id=dev,
                extra={"tz_offset_secs": tz_offset_secs},
            ))

    return batch


# ===== tz 解析（3 种形态 + None）=====

def _parse_tz_offset(tz_value) -> Optional[int]:
    """3 种形态（见 docstring 头部）。"""
    if tz_value is None:
        return None
    if isinstance(tz_value, (int, float)):
        return offset_from_number(float(tz_value))
    if isinstance(tz_value, str):
        s = tz_value.strip()
        if not s:
            return None
        # 试数字（offset_from_number 会自动处理毫秒转换）
        try:
            n = float(s)
            return offset_from_number(n)
        except ValueError:
            pass
        # 字符串（"Asia/Shanghai" / "GMT+08:00"）
        return parse_timezone_text(s)
    return None