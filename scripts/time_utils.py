#!/usr/bin/env python3
"""
Zepp Data Skill — 统一时间工具库（scripts/time_utils.py）

设计原则：
- **名字明确单位**：`ms_to_*` 表示输入是毫秒，`sec_to_*` 表示输入是秒
- **永远返回 tz-aware datetime**（默认 UTC+8 北京）
- **零副作用 / 零 DB 依赖**：纯函数，可在任何上下文 import
- **覆盖 DB 里所有时间字段**：见 EPOCH_FIELDS 字典

历史背景：
- Zepp API 多次演进，DB 里**时间字段单位不统一**——
  有的表是 `*_ms` 毫秒（measurements / raw_records / workout_route_points / workout_samples），
  有的表是 `*_ts` 秒（heart_rate_band_samples / sleep_sessions / sleep_stage_slices / workouts）。
- **永远不要凭字段名猜单位**！查 EPOCH_FIELDS 字典或调 explain_field()。

踩过的坑：
- 把 `heart_rate_band_samples.ts`（秒）当成毫秒除 1000，得到 `1970-01-22`（应该是 2026 年的日期）。
- LLM / 自动化脚本**必须用本模块的函数**，不要直接 `datetime.fromtimestamp(...)`！
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional, Union


# 默认时区：用户的中国时区 UTC+8（Asia/Shanghai）
DEFAULT_TZ = timezone(timedelta(hours=8))
TZ_NAME = "Asia/Shanghai"

__all__ = [
    "DEFAULT_TZ",
    "TZ_NAME",
    "EPOCH_FIELDS",
    "ms_to_utc_dt",
    "ms_to_local_dt",
    "sec_to_local_dt",
    "parse_local_date",
    "epoch_now_ms",
    "epoch_now_sec",
    "days_ago_ms",
    "fmt_local_dt",
    "fmt_local_date",
    "explain_field",
]


# ===== DB 字段 → 函数映射（踩坑速查表） =====
#
# 表名.字段名 → (推荐函数名, 单位说明)
#
# 调用任何脚本前**先查这张表**：
#   1. 字段名带 `_ms` → 几乎都是毫秒
#   2. 字段名是 `ts` → 几乎都是秒
#   3. 不确定 → 跑 `explain_field("表名", "字段名")`
#
EPOCH_FIELDS: dict[str, tuple[str, str]] = {
    # === 毫秒字段 ===
    "measurements.ts_ms": ("ms_to_utc_dt", "Unix 毫秒 → UTC datetime"),
    "raw_records.start_ts_ms": ("ms_to_utc_dt", "Unix 毫秒 → UTC datetime"),
    "raw_records.end_ts_ms": ("ms_to_utc_dt", "Unix 毫秒 → UTC datetime"),
    "workout_route_points.ts_ms": ("ms_to_utc_dt", "Unix 毫秒 → UTC datetime"),
    "workout_samples.ts_ms": ("ms_to_utc_dt", "Unix 毫秒 → UTC datetime"),
    # === 秒字段（不要除 1000！）===
    "heart_rate_band_samples.ts": ("sec_to_local_dt", "Unix 秒 → 本地 datetime ⚠️ 不要除 1000"),
    "workouts.end_ts": ("sec_to_local_dt", "Unix 秒 → 本地 datetime ⚠️ 不要除 1000"),
    "sleep_sessions.start_ts": ("sec_to_local_dt", "Unix 秒 → 本地 datetime ⚠️ 不要除 1000"),
    "sleep_sessions.end_ts": ("sec_to_local_dt", "Unix 秒 → 本地 datetime ⚠️ 不要除 1000"),
    "sleep_stage_slices.start_ts": ("sec_to_local_dt", "Unix 秒 → 本地 datetime ⚠️ 不要除 1000"),
    # === 本地日历日字段 ===
    "measurements.date": ("parse_local_date", "本地日历日 YYYY-MM-DD → date 对象"),
    "heart_rate_band_samples.date": ("parse_local_date", "本地日历日 YYYY-MM-DD → date 对象"),
}


# ===== 核心转换函数 =====


def ms_to_utc_dt(ts_ms: Optional[int]) -> Optional[datetime]:
    """Unix 毫秒 → UTC tz-aware datetime.

    用于:
        - measurements.ts_ms
        - raw_records.start_ts_ms / raw_records.end_ts_ms
        - workout_route_points.ts_ms
        - workout_samples.ts_ms

    参数:
        ts_ms: Unix 毫秒时间戳（int 或 None）

    返回:
        tz-aware datetime（UTC 时区）；None 或负数返回 None
    """
    if ts_ms is None or ts_ms <= 0:
        return None
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)


def ms_to_local_dt(
    ts_ms: Optional[int],
    tz: timezone = DEFAULT_TZ,
) -> Optional[datetime]:
    """Unix 毫秒 → 本地时区 tz-aware datetime.

    默认转为 UTC+8（北京）。需要其他时区时传 `tz` 参数。

    参数:
        ts_ms: Unix 毫秒时间戳
        tz: 目标时区（默认 DEFAULT_TZ = UTC+8）
    """
    utc_dt = ms_to_utc_dt(ts_ms)
    if utc_dt is None:
        return None
    return utc_dt.astimezone(tz)


def sec_to_local_dt(
    ts_sec: Optional[Union[int, float]],
    tz: timezone = DEFAULT_TZ,
) -> Optional[datetime]:
    """Unix 秒 → 本地时区 tz-aware datetime.

    用于:
        - heart_rate_band_samples.ts
        - workouts.end_ts
        - sleep_sessions.start_ts / sleep_sessions.end_ts
        - sleep_stage_slices.start_ts

    ⚠️ **不要除 1000** —— 这个表存的就是秒。

    参数:
        ts_sec: Unix 秒时间戳（int / float / None）
        tz: 目标时区（默认 DEFAULT_TZ = UTC+8）
    """
    if ts_sec is None or ts_sec <= 0:
        return None
    return datetime.fromtimestamp(float(ts_sec), tz=tz)


def parse_local_date(date_str: Optional[str]) -> Optional[date]:
    """本地日历日字符串 YYYY-MM-DD → date 对象.

    用于:
        - measurements.date
        - heart_rate_band_samples.date

    返回:
        date 对象；空字符串 / 解析失败返回 None
    """
    if not date_str:
        return None
    try:
        return datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ===== 当前时间辅助函数 =====


def epoch_now_ms() -> int:
    """当前时间的 Unix 毫秒（UTC 基准）."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def epoch_now_sec() -> int:
    """当前时间的 Unix 秒（UTC 基准）."""
    return int(datetime.now(timezone.utc).timestamp())


def days_ago_ms(days: int) -> int:
    """N 天前的 Unix 毫秒（now - days * 86400 * 1000）.

    用于同步窗口：`from_ms = days_ago_ms(7)` 即"最近 7 天"。
    """
    return epoch_now_ms() - days * 86400 * 1000


# ===== 格式化辅助函数 =====


def fmt_local_dt(
    dt: Optional[datetime],
    fmt: str = "%Y-%m-%d %H:%M:%S",
) -> str:
    """datetime → 字符串（按 fmt 格式化）.

    None 输入返回空串（避免 .strftime() 抛 NoneType 异常）。
    """
    if dt is None:
        return ""
    return dt.strftime(fmt)


def fmt_local_date(date_input: Optional[Union[date, str]]) -> str:
    """date 对象 / 字符串 → 'YYYY-MM-DD' 字符串.

    接受：
        - date 对象 → strftime
        - '2026-09-25T...' ISO 字符串 → 取前 10 位
        - None / 空串 → 返回空串
    """
    if not date_input:
        return ""
    if hasattr(date_input, "strftime"):
        return date_input.strftime("%Y-%m-%d")  # type: ignore[union-attr]
    s = str(date_input)
    if len(s) >= 10:
        return s[:10]
    return s


# ===== 调试辅助 =====


def explain_field(table: str, field: str) -> str:
    """告诉用户某字段是什么单位 / 怎么解析.

    用法:
        >>> print(explain_field("heart_rate_band_samples", "ts"))
        heart_rate_band_samples.ts → sec_to_local_dt(): Unix 秒 → 本地 datetime ⚠️ 不要除 1000

    未知字段返回提示去找 schema。
    """
    key = f"{table}.{field}"
    if key in EPOCH_FIELDS:
        fn_name, desc = EPOCH_FIELDS[key]
        return f"{key} → {fn_name}(): {desc}"
    return (
        f"{key} → 未知字段，请查表 schema 或 storage.py::SCHEMA。"
        f"已注册字段: {sorted(EPOCH_FIELDS.keys())}"
    )