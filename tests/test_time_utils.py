#!/usr/bin/env python3
"""
tests/test_time_utils.py — scripts/time_utils.py 单元测试

覆盖：
1. 毫秒 → UTC / 本地 datetime
2. 秒 → 本地 datetime（**关键**：这是 1970-01-22 bug 的根因）
3. sec vs ms 单位区分（同一数值两种解释差异 ≥ 56 年）
4. None / 0 / 负数边界
5. parse_local_date / fmt_local_date
6. epoch_now_ms / epoch_now_sec / days_ago_ms
7. explain_field 已知 + 未知字段

关键回归测试：test_time_utils_REAL_BUG_REGRESSION
—— 用真实产生 1970-01-22 的值（秒被当毫秒解释）做单元测试。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

# 确保 scripts/ 在 path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from time_utils import (  # noqa: E402
    DEFAULT_TZ,
    TZ_NAME,
    EPOCH_FIELDS,
    days_ago_ms,
    epoch_now_ms,
    epoch_now_sec,
    explain_field,
    fmt_local_date,
    fmt_local_dt,
    ms_to_local_dt,
    ms_to_utc_dt,
    parse_local_date,
    sec_to_local_dt,
)


# ===== 1. ms_to_utc_dt =====


def test_ms_to_utc_dt_basic():
    """Unix 毫秒 → UTC datetime 正确."""
    # 2026-09-25 12:00:00 UTC = 1790337600000 ms
    ts = 1790337600000
    dt = ms_to_utc_dt(ts)
    assert dt is not None
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026
    assert dt.month == 9
    assert dt.day == 25
    assert dt.hour == 12
    assert dt.minute == 0
    assert dt.second == 0


def test_ms_to_local_dt_beijing():
    """Unix 毫秒 → 北京时间 (UTC+8)."""
    # 1790337600000 ms = 2026-09-25 12:00:00 UTC = 20:00:00 北京
    ts = 1790337600000
    dt = ms_to_local_dt(ts, tz=DEFAULT_TZ)
    assert dt is not None
    assert dt.utcoffset() == timedelta(hours=8)
    assert dt.hour == 20
    assert dt.day == 25
    assert dt.month == 9
    assert dt.year == 2026


def test_ms_to_utc_dt_none():
    """None 输入返回 None."""
    assert ms_to_utc_dt(None) is None


def test_ms_to_utc_dt_zero_or_negative():
    """0 或负数返回 None（防 1970-01-01 哨兵）."""
    assert ms_to_utc_dt(0) is None
    assert ms_to_utc_dt(-1) is None
    assert ms_to_utc_dt(-1790337600000) is None


# ===== 2. sec_to_local_dt（关键：1970-01-22 bug 的根因） =====


def test_sec_to_local_dt_basic():
    """Unix 秒 → 本地 datetime 正确（heart_rate_band_samples.ts 的正确解释）."""
    # 1790337600 秒 = 2026-09-25 12:00:00 UTC = 20:00:00 北京
    ts_sec = 1790337600
    dt = sec_to_local_dt(ts_sec, tz=DEFAULT_TZ)
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 9
    assert dt.day == 25
    assert dt.hour == 20
    assert dt.minute == 0
    assert dt.second == 0
    assert dt.tzinfo == DEFAULT_TZ


def test_sec_vs_ms_distinction():
    """**关键测试**：同一个数值用 sec_to 和 ms_to 解释，结果完全不同.

    这验证函数能正确区分单位——避免 LLM/脚本把秒当毫秒混用。
    """
    # 1790337600 这个值：
    #   - 当秒解释 → 2026-09-25 12:00:00 UTC
    #   - 当毫秒解释 → 1970-01-21 16:13:21 UTC（约 56 年前）
    ts = 1790337600

    # 解释为秒 → 2026
    dt_sec = sec_to_local_dt(ts, tz=timezone.utc)
    assert dt_sec is not None
    assert dt_sec.year == 2026

    # 解释为毫秒 → 1970（差异 ~56 年）
    dt_ms = ms_to_utc_dt(ts)
    assert dt_ms is not None
    assert dt_ms.year == 1970

    # 验证差异 ≥ 56 年（1970 → 2026 是 56 年）
    year_diff = dt_sec.year - dt_ms.year
    assert year_diff >= 56, f"单位区分失效：年份差只有 {year_diff}"


def test_sec_to_local_dt_none():
    """None 输入返回 None."""
    assert sec_to_local_dt(None) is None


def test_sec_to_local_dt_negative():
    """负数返回 None."""
    assert sec_to_local_dt(-1) is None
    assert sec_to_local_dt(0) is None
    assert sec_to_local_dt(-1790337600) is None


def test_sec_to_local_dt_float_input():
    """接受 float 输入（部分 API 返回浮点秒）."""
    ts = 1790337600.5
    dt = sec_to_local_dt(ts, tz=timezone.utc)
    assert dt is not None
    assert dt.year == 2026
    assert dt.second == 0  # 整数部分


# ===== 3. parse_local_date =====


def test_parse_local_date_basic():
    """'2026-09-25' → date(2026, 9, 25)."""
    d = parse_local_date("2026-09-25")
    assert d == date(2026, 9, 25)


def test_parse_local_date_invalid():
    """无效字符串返回 None（不抛异常）."""
    assert parse_local_date("garbage") is None
    assert parse_local_date("2026/09/25") is None  # 错误分隔符
    assert parse_local_date("2026-13-25") is None  # 月份非法


def test_parse_local_date_empty_and_none():
    """空串 / None 返回 None."""
    assert parse_local_date("") is None
    assert parse_local_date(None) is None


# ===== 4. epoch_now_* / days_ago_ms =====


def test_epoch_now_ms_recent():
    """当前 ms 应该比 2026-09-01 大."""
    # 2026-09-01 00:00:00 UTC = 1788240000000 ms
    ts = epoch_now_ms()
    assert ts > 1788240000000
    # current-time 年后必然 < 2100 年
    assert ts < 4102444800000  # 2100-01-01


def test_epoch_now_sec_recent():
    """当前 sec 应该比 1.7e9 大（2024-09-09 之后）."""
    ts = epoch_now_sec()
    # 1.7e9 秒 = 2023-11-14；2026 年必然更大
    assert ts > 1_700_000_000
    assert ts < 4_100_000_000  # 2100 年前


def test_days_ago_ms():
    """days=7 应该比 now 早正好 7 天（精度 1 秒）. """
    now = epoch_now_ms()
    seven_days_ago = days_ago_ms(7)
    diff = now - seven_days_ago
    expected_diff = 7 * 86400 * 1000
    # 允许 2 秒误差（两次取时间不原子）
    assert abs(diff - expected_diff) < 2000


def test_epoch_now_ms_above_2026_09_01():
    """当前 ms 严格大于 2026-09-01 00:00 UTC（防止 off-by-one）."""
    # 2026-09-01 00:00:00 UTC = 1788240000 sec = 1788240000000 ms
    assert epoch_now_ms() > 1788240000000


def test_days_ago_ms_zero():
    """days=0 返回当前时间."""
    now = epoch_now_ms()
    assert abs(days_ago_ms(0) - now) < 2000


# ===== 5. fmt_local_dt / fmt_local_date =====


def test_fmt_local_dt():
    """datetime → 字符串（默认格式 YYYY-MM-DD HH:MM:SS）."""
    dt = datetime(2026, 9, 25, 13, 18, 28, tzinfo=DEFAULT_TZ)
    assert fmt_local_dt(dt) == "2026-09-25 13:18:28"


def test_fmt_local_dt_custom_format():
    """自定义格式."""
    dt = datetime(2026, 9, 25, 13, 18, 28, tzinfo=DEFAULT_TZ)
    assert fmt_local_dt(dt, "%Y/%m/%d") == "2026/09/25"


def test_fmt_local_dt_none():
    """None 输入返回空串（不抛异常）."""
    assert fmt_local_dt(None) == ""


def test_fmt_local_date_with_string():
    """字符串输入 → 'YYYY-MM-DD' 字符串."""
    assert fmt_local_date("2026-09-25") == "2026-09-25"
    # ISO 字符串带时间部分也应正确处理
    assert fmt_local_date("2026-09-25T13:18:28+08:00") == "2026-09-25"


def test_fmt_local_date_with_date_object():
    """date 对象 → 字符串."""
    d = date(2026, 9, 25)
    assert fmt_local_date(d) == "2026-09-25"


def test_fmt_local_date_empty():
    """空值返回空串."""
    assert fmt_local_date("") == ""
    assert fmt_local_date(None) == ""


# ===== 6. explain_field =====


def test_explain_field_known():
    """已知字段返回函数名 + 单位说明."""
    result = explain_field("heart_rate_band_samples", "ts")
    assert "sec_to_local_dt" in result
    assert "不要除 1000" in result

    result2 = explain_field("measurements", "ts_ms")
    assert "ms_to_utc_dt" in result2
    assert "Unix 毫秒" in result2


def test_explain_field_unknown():
    """未知字段返回提示."""
    result = explain_field("nonexistent_table", "fake_field")
    assert "未知字段" in result
    assert "请查表 schema" in result


def test_explain_field_all_epochs_in_dict():
    """EPOCH_FIELDS 字典里所有键都能正确 explain —— 防止映射表过时."""
    for key in EPOCH_FIELDS:
        table, field = key.split(".")
        result = explain_field(table, field)
        assert "未知字段" not in result, f"{key} 在 EPOCH_FIELDS 但 explain_field 找不到"


# ===== 7. 真实 bug 回归测试 =====


def test_time_utils_REAL_BUG_REGRESSION():
    """**真实 bug 回归**：1970-01-22 01:18:28 这个值的两种解释.

    历史 bug 现场：
        - 某个 heart_rate_band_samples.ts 值（实际是秒）
        - 错误做法：`datetime.fromtimestamp(ts/1000, tz)` → 1970-01-22
        - 正确做法：`sec_to_local_dt(ts)` → 2026-09-25 13:18:28 UTC

    本测试断言：
        1. sec_to_local_dt 给出 2026 年（正确）
        2. ms_to_utc_dt 给出 1970 年（错误的解释方式）
        3. 两种解释相差 ≥ 56 年
    """
    # 1790342308 秒 = 2026-09-25 13:18:28 UTC
    # 但如果 /1000 → 1790342.308 秒 = 1970-01-21 17:19:02 UTC（差 56 年！）
    buggy_value = 1790342308

    # 正确解释（秒）
    dt_correct = sec_to_local_dt(buggy_value, tz=timezone.utc)
    assert dt_correct is not None
    assert dt_correct.year == 2026
    assert dt_correct.month == 9
    assert dt_correct.day == 25
    assert dt_correct.hour == 13
    assert dt_correct.minute == 18
    assert dt_correct.second == 28

    # 错误解释（毫秒）—— 这是 bug 现场
    dt_wrong = ms_to_utc_dt(buggy_value)
    assert dt_wrong is not None
    assert dt_wrong.year == 1970  # 1970-01-21/22 附近

    # 关键断言：两种解释相差 ≥ 56 年
    diff_years = dt_correct.year - dt_wrong.year
    assert diff_years >= 56, (
        f"回归测试失败：sec_to_local_dt 和 ms_to_utc_dt 应该给出截然不同的结果，"
        f"实际年份差只有 {diff_years}"
    )


def test_time_utils_bug_value_local_conversion():
    """回归测试 2：bug 值转北京时区也正确（2026-09-25 21:18:28）."""
    buggy_value = 1790342308  # 秒
    dt = sec_to_local_dt(buggy_value, tz=DEFAULT_TZ)  # UTC+8
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 9
    assert dt.day == 25
    assert dt.hour == 21  # 13 UTC + 8 = 21 北京
    assert dt.minute == 18
    assert dt.second == 28


# ===== 8. 模块元数据 =====


def test_module_constants():
    """模块常量正确."""
    assert TZ_NAME == "Asia/Shanghai"
    assert DEFAULT_TZ == timezone(timedelta(hours=8))
    assert DEFAULT_TZ.utcoffset(None) == timedelta(hours=8)


def test_epoch_fields_completeness():
    """EPOCH_FIELDS 至少覆盖任务要求的 12 个字段."""
    required = [
        "measurements.ts_ms",
        "measurements.date",
        "raw_records.start_ts_ms",
        "raw_records.end_ts_ms",
        "heart_rate_band_samples.ts",
        "heart_rate_band_samples.date",
        "workouts.end_ts",
        "sleep_sessions.start_ts",
        "sleep_sessions.end_ts",
        "sleep_stage_slices.start_ts",
        "workout_route_points.ts_ms",
        "workout_samples.ts_ms",
    ]
    for key in required:
        assert key in EPOCH_FIELDS, f"EPOCH_FIELDS 缺少 {key}"