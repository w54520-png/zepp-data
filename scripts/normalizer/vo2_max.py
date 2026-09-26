"""
VO2_MAX 流解析 —— M4 章节 4.4。

端点：GET /v2/watch/users/{user_id}/WatchSportStatistics/VO2_MAX
Surface：watch_stat

【M3 实施发现】当前用户实测 VO2_MAX 端点返回空 items（capability=no_records）。
M4 决策：即使当前没数据也接入（参考 sport_load 写法，照搬 Rust 字段候选）。

【响应形态】（基于 ZeppBridge Rust 文档）：
  envelope: {"code": 1, "data": {"items": [...]}}
  item 形态（候选字段来自 ZeppBridge normalizer:1085-1095）：
    {
      "dayId": "2026-09-18",
      "vo2max": 45.2,           # ml/kg/min
      "vo2Max": 45.2,           # 兼容驼峰
      "VO2_MAX": 45.2,          # 全大写
      "vo2_max_run": 48.5,      # 跑步专用
      "vo2_max_walking": 38.0,  # 健走专用
      "updateTime": 1726612800000
    }

【metric 与范围】（对齐 Rust metric_fields + range）：
  - vo2max                (1, 100) ml/kg/min（合理范围；<=0 是哨兵）

【日期解析】dayId 优先；fallback：updateTime → UTC 转 YYYY-MM-DD。
"""
from __future__ import annotations
from normalizer.common import (
    DailyMetric, NormalizedBatch,
    extract_items, first_number, first_string,
    in_range,
)

# 字段候选（对齐 ZeppBridge normalizer:1085-1095）
_VO2MAX_FIELD_NAMES = (
    "vo2max", "vo2Max", "VO2_MAX", "VO2_max",
    "vo2_max_run", "vo2_max_walking",
)

# 范围 (1, 100) ml/kg/min —— 极值涵盖专业运动员
# Rust 文档说 "vo2max == -1" 是哨兵（涵盖 60% 本地 workout）
VO2MAX_RANGE = (1.0, 100.0)


def normalize_vo2_max(raw: dict) -> NormalizedBatch:
    """
    解析 VO2_MAX 流 → 每日 vo2max DailyMetric。

    策略：
      1) 抽 items（兼容 5 种 envelope，与 sport_load 同源）
      2) 每条 item 取日期（dayId / updateTime）
      3) 在候选字段里找 vo2max（数值可能在顶层）
      4) 范围校验后入库
    """
    batch = NormalizedBatch()
    # extract_items 会在空响应/无 items 时抛 DataUnavailable 或 ValueError
    try:
        items = extract_items(raw)
    except Exception:
        batch.capability = "no_data"  # 当前 CN 用户实测
        return batch
    if not items:
        batch.capability = "no_data"
        return batch

    from datetime import datetime, timezone

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            batch.diagnostics.append(f"vo2_max item {idx}: 不是对象")
            continue
        # 日期：dayId 优先
        day = first_string(item, ("dayId", "day"))
        if not day:
            ts_ms = first_number(item, ("updateTime", "timestamp"))
            if ts_ms:
                day = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
            else:
                batch.diagnostics.append(f"vo2_max item {idx}: 缺少日期字段")
                continue
        # vo2max 值（按候选顺序取第一个）
        v = first_number(item, _VO2MAX_FIELD_NAMES)
        if v is None:
            batch.diagnostics.append(f"vo2_max item {idx} ({day}): 无 vo2max 字段")
            continue
        # 哨兵：<=0 视为缺失（实测 vo2max == -1 是常见哨兵）
        if v <= 0:
            batch.diagnostics.append(f"vo2_max item {idx} ({day}): vo2max={v} 哨兵")
            continue
        # 范围校验
        if not in_range(v, *VO2MAX_RANGE):
            batch.diagnostics.append(
                f"vo2_max item {idx} ({day}): {v} 超出 ({VO2MAX_RANGE[0]}, {VO2MAX_RANGE[1]})"
            )
            continue
        scope = "user_fused"  # VO2max 是云端拟合值
        batch.records.append(DailyMetric(
            metric="vo2max",
            date=day,
            value=v,
            unit="ml/kg/min",
            source_scope=scope,
            device_id=None,
        ))
    return batch
