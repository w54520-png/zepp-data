"""
M4 章节 4.3：HRV SDNN fallback 单元测试

背景：daily_metrics 表只有 hrv_rmssd 字段，没有 hrv 字段。
compute_weekly_report 必须先查 hrv，没有再回退 hrv_rmssd。

M3 已合入 compute_weekly_report（line 519-530）：
  recent_data = collapse_per_day_metric(conn, table, "hrv", ...)
  if not recent_data:
      recent_data = collapse_per_day_metric(conn, table, "hrv_rmssd", ...)

本测试聚焦 dual-lookup 行为。
"""
from __future__ import annotations
import os
import sqlite3
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from insight import compute_weekly_report, collapse_per_day_metric


TODAY = date(2026, 9, 23)


def _make_db(daily_metrics=None, sleep_sessions=None, workouts=None):
    """
    daily_metrics: list of (metric, date, value, source_scope, device_id)
    sleep_sessions: list of (date, source, start_ts, end_ts, tz_offset, time_in_bed, is_nap, deep, light, rem, awake)
    workouts: list of (trackid, sport_key, sport_zh, dis_m, run_s, avg_hr, end_ts, end_iso)
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE daily_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        metric TEXT,
        date TEXT,
        value REAL,
        source_scope TEXT,
        device_id TEXT
    );
    CREATE TABLE sleep_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        date TEXT,
        source TEXT,
        start_ts INTEGER,
        end_ts INTEGER,
        tz_offset_secs INTEGER,
        time_in_bed_secs INTEGER,
        deep_secs INTEGER,
        light_secs INTEGER,
        rem_secs INTEGER,
        awake_secs INTEGER,
        is_nap INTEGER DEFAULT 0
    );
    CREATE TABLE workouts (
        trackid TEXT PRIMARY KEY,
        sport_key TEXT,
        sport_zh TEXT,
        dis_m REAL,
        run_s INTEGER,
        avg_heart_rate REAL,
        end_time_ts INTEGER,
        end_time_iso TEXT
    );
    """)
    for m in (daily_metrics or []):
        conn.execute("""INSERT INTO daily_metrics (metric, date, value, source_scope, device_id)
                      VALUES (?, ?, ?, ?, ?)""", m)
    for s in (sleep_sessions or []):
        conn.execute("""INSERT INTO sleep_sessions
                      (date, source, start_ts, end_ts, tz_offset_secs, time_in_bed_secs,
                       is_nap, deep_secs, light_secs, rem_secs, awake_secs)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", s)
    for w in (workouts or []):
        conn.execute("""INSERT INTO workouts (trackid, sport_key, sport_zh, dis_m, run_s,
                      avg_heart_rate, end_time_ts, end_time_iso)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", w)
    conn.commit()
    return conn


def _recent_dates(today, n=7):
    """[today-6, today]"""
    return [(today - timedelta(days=i)).isoformat() for i in range(n)]


def _baseline_dates(today, n=28):
    """[today-34, today-7]"""
    return [(today - timedelta(days=i)).isoformat() for i in range(7, 7 + n)]


class HrvSdnnFallbackTest(unittest.TestCase):
    def test_hrv_only_uses_hrv(self):
        """有 hrv 字段 → 直接用，不回退"""
        recent = _recent_dates(TODAY)
        dms = [("hrv", d, 50.0, "user_fused", None) for d in recent]
        dms += [("hrv_rmssd", d, 999.0, "user_fused", None) for d in recent]  # hrv_rmssd 应被忽略
        db = _make_db(daily_metrics=dms)
        report = compute_weekly_report(db, today=TODAY)
        hrv_fact = next(f for f in report.facts if f.fact_id == "weekly.hrv")
        self.assertIsNotNone(hrv_fact.recent_value)
        self.assertAlmostEqual(hrv_fact.recent_value, 50.0)

    def test_no_hrv_falls_back_to_hrv_rmssd(self):
        """无 hrv 字段 → 回退 hrv_rmssd（M4 4.3 关键场景）"""
        recent = _recent_dates(TODAY)
        baseline = _baseline_dates(TODAY)
        # 只插 hrv_rmssd（无 hrv）
        dms = [("hrv_rmssd", d, 40.0, "device", "D1") for d in recent]
        dms += [("hrv_rmssd", d, 45.0, "device", "D1") for d in baseline]
        db = _make_db(daily_metrics=dms)
        report = compute_weekly_report(db, today=TODAY)
        hrv_fact = next(f for f in report.facts if f.fact_id == "weekly.hrv")
        self.assertIsNotNone(hrv_fact.recent_value)
        self.assertAlmostEqual(hrv_fact.recent_value, 40.0)
        self.assertAlmostEqual(hrv_fact.baseline_value, 45.0)

    def test_no_hrv_no_rmssd_returns_insufficient(self):
        """hrv 和 hrv_rmssd 都没有 → recent_value=None + excluded_code"""
        recent = _recent_dates(TODAY)
        dms = [("resting_hr", d, 60.0, "user_fused", None) for d in recent]
        db = _make_db(daily_metrics=dms)
        report = compute_weekly_report(db, today=TODAY)
        hrv_fact = next(f for f in report.facts if f.fact_id == "weekly.hrv")
        # recent_value 为 None
        self.assertIsNone(hrv_fact.recent_value)
        # excluded_code 应该是 weekly_no_recent_data 或类似
        self.assertIsNotNone(hrv_fact.excluded_code)

    def test_hrv_takes_priority_over_hrv_rmssd_in_recent(self):
        """recent 有 hrv + hrv_rmssd → 用 hrv；baseline 只有 hrv_rmssd → 也用 hrv_rmssd"""
        recent = _recent_dates(TODAY)
        baseline = _baseline_dates(TODAY)
        dms = [("hrv", d, 50.0, "user_fused", None) for d in recent]
        dms += [("hrv_rmssd", d, 30.0, "user_fused", None) for d in recent]  # 干扰
        dms += [("hrv_rmssd", d, 45.0, "user_fused", None) for d in baseline]  # baseline 只有 rmssd
        db = _make_db(daily_metrics=dms)
        report = compute_weekly_report(db, today=TODAY)
        hrv_fact = next(f for f in report.facts if f.fact_id == "weekly.hrv")
        # recent = 50 (hrv wins), baseline = 45 (from hrv_rmssd fallback)
        self.assertAlmostEqual(hrv_fact.recent_value, 50.0)
        self.assertAlmostEqual(hrv_fact.baseline_value, 45.0)


class HrvSdnnCollapseTest(unittest.TestCase):
    def test_collapse_hrv_only(self):
        recent = _recent_dates(TODAY)  # 倒序：[today, today-1, ..., today-6]
        dms = [("hrv", d, 50.0, "user_fused", None) for d in recent]
        db = _make_db(dms)
        # 用 last / first 反转（升序：today-6, ..., today）
        result = collapse_per_day_metric(db, "daily_metrics", "hrv",
                                          recent[-1], recent[0])
        self.assertEqual(len(result), 7)

    def test_collapse_hrv_rmssd_only(self):
        recent = _recent_dates(TODAY)
        dms = [("hrv_rmssd", d, 50.0, "user_fused", None) for d in recent]
        db = _make_db(dms)
        result = collapse_per_day_metric(db, "daily_metrics", "hrv",
                                          recent[-1], recent[0])
        self.assertEqual(len(result), 0)  # hrv 表里没东西
        # fallback
        result2 = collapse_per_day_metric(db, "daily_metrics", "hrv_rmssd",
                                           recent[-1], recent[0])
        self.assertEqual(len(result2), 7)


if __name__ == "__main__":
    unittest.main()
