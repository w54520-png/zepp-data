"""
M3 章节 3.2: insight 模块单元测试

覆盖（≥10 用例）：
  - 5 fact 各 1 case（higher/lower/same）
  - baseline 不足（0/2 samples → Insufficient）
  - implausible_pace 过滤
  - 周报 7 fact 各 1 case
  - sleep_start_regularity 跨午夜（23:50 + 00:10 差 20 分钟而非 23 小时）
  - 周报 baseline 不够（全 insufficient）
  - collapse_per_day（同日多 scope → mixed source）
  - confidence: from_samples_count 5 档
  - 距离±20% baseline 过滤
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from insight import (
    compute_workout_insight,
    compute_weekly_report,
    from_samples_count,
    compare,
    delta_percent,
    insight_to_json,
    weekly_report_to_json,
    collapse_per_day_metric,
    mean_of_dict,
    stddev_of_dict,
    SUPPORTED_WORKOUT_TYPES,
    DISTANCE_TOLERANCE,
    MIN_BASELINE_DAYS,
    SAME_THRESHOLD,
    SLEEP_REGULARITY_ANCHOR_MINUTES,
    SLEEP_REGULARITY_WRAP,
    _daily_sleep_starts,
    _make_weekly_fact,
)


# ===== 工具：在临时 DB 里搭 fixture =====

def make_workouts_db(rows):
    """rows: list of (trackid, sport_key, sport_zh, dis_m, run_s, avg_heart_rate,
                       end_time_ts, exercise_load, location)
    """
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE workouts (
        trackid TEXT PRIMARY KEY,
        sport_key TEXT,
        sport_zh TEXT,
        dis_m REAL,
        run_s INTEGER,
        avg_heart_rate REAL,
        max_heart_rate INTEGER,
        min_heart_rate INTEGER,
        end_time_ts INTEGER,
        end_time_iso TEXT,
        exercise_load REAL
    );
    CREATE TABLE workout_hr_drift (
        track_id TEXT PRIMARY KEY,
        first_half_metres_per_beat REAL,
        second_half_metres_per_beat REAL,
        drift_percent REAL,
        speed_cv REAL,
        first_half_avg_hr REAL,
        second_half_avg_hr REAL,
        first_half_avg_speed_mps REAL,
        second_half_avg_speed_mps REAL,
        first_half_samples INTEGER,
        second_half_samples INTEGER,
        reason_code TEXT
    );
    """)
    for r in rows:
        db.execute("""
            INSERT INTO workouts (trackid, sport_key, sport_zh, dis_m, run_s,
                                   avg_heart_rate, end_time_ts, end_time_iso,
                                   exercise_load)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, r[:9])
    db.commit()
    return db


# Test scenarios (固定 today=2026-09-23 以便窗口确定)
TODAY = date(2026, 9, 23)


def ts(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


# ===== 1. from_samples_count =====

class FromSamplesCountTest(unittest.TestCase):
    def test_insufficient(self):
        for n in [0, 1, 2]:
            self.assertEqual(from_samples_count(n), "Insufficient")

    def test_low(self):
        for n in [3, 4]:
            self.assertEqual(from_samples_count(n), "Low")

    def test_medium(self):
        for n in [5, 6, 7]:
            self.assertEqual(from_samples_count(n), "Medium")

    def test_high(self):
        for n in [8, 9, 10, 50]:
            self.assertEqual(from_samples_count(n), "High")


# ===== 2. compare 方向 =====

class CompareTest(unittest.TestCase):
    def test_same_within_threshold(self):
        self.assertEqual(compare(100.5, 100.0), "same")  # 0.5 < 0.5 阈值
        self.assertEqual(compare(100.49, 100.0), "same")

    def test_higher(self):
        self.assertEqual(compare(101.0, 100.0), "higher")

    def test_lower(self):
        self.assertEqual(compare(99.0, 100.0), "lower")

    def test_none_baseline_returns_same(self):
        self.assertEqual(compare(100.0, None), "same")


# ===== 3. delta_percent =====

class DeltaPctTest(unittest.TestCase):
    def test_positive(self):
        self.assertAlmostEqual(delta_percent(110.0, 100.0), 10.0)
    def test_negative(self):
        self.assertAlmostEqual(delta_percent(90.0, 100.0), -10.0)
    def test_zero_baseline_returns_none(self):
        self.assertIsNone(delta_percent(100.0, 0.0))
    def test_none_baseline_returns_none(self):
        self.assertIsNone(delta_percent(100.0, None))


# ===== 4. WorkoutInsight 5 facts =====

class WorkoutInsightFactsTest(unittest.TestCase):
    def test_all_facts_higher_than_baseline(self):
        """target: 6km / 1800s / pace 5min/km / 150bpm
           baseline: 5个 5km / 1500s / pace 5min/km / 140bpm
           distance ↑ (target > baseline)
           duration ↑ (target > baseline)
           pace same (5min = 5min)
           avg_hr ↑ (target > baseline)
        """
        rows = []
        for i in range(5):
            tid = f"base_{i}"
            rows.append((tid, "run", "跑步", 5000.0, 1500, 140.0,
                         ts(2026, 8, 1) + i * 86400,
                         f"2026-08-{(0+1)*1+i:02d}T00:00:00", None))
        # target
        rows.append(("target", "run", "跑步", 6000.0, 1800, 150.0,
                     ts(2026, 9, 22),
                     "2026-09-22T00:00:00", None))
        db = make_workouts_db(rows)
        ins = compute_workout_insight(db, "target", today=TODAY)
        self.assertTrue(ins.supported)
        # 5 facts present
        fact_ids = {f.fact_id for f in ins.facts}
        self.assertEqual(fact_ids, {"run.distance", "run.duration", "run.pace",
                                     "run.avg_hr", "run.training_load"})
        # distance: 6000 vs 5000 → higher
        fd = next(f for f in ins.facts if f.fact_id == "run.distance")
        self.assertEqual(fd.direction, "higher")
        # duration: 1800 vs 1500 → higher
        fdu = next(f for f in ins.facts if f.fact_id == "run.duration")
        self.assertEqual(fdu.direction, "higher")
        # pace: 1800/6 = 300 s/km vs 1500/5 = 300 → same (within 0.5)
        fp = next(f for f in ins.facts if f.fact_id == "run.pace")
        self.assertEqual(fp.direction, "same")
        # avg_hr: 150 vs 140 → higher
        fhr = next(f for f in ins.facts if f.fact_id == "run.avg_hr")
        self.assertEqual(fhr.direction, "higher")
        # samples_count = 5
        self.assertEqual(ins.samples_count, 5)
        # confidence Medium (5..7)
        self.assertEqual(fd.confidence, "Medium")

    def test_lower_than_baseline(self):
        """target: 6km / 1500s / pace 140bpm
           baseline: 5个 5km / 1500s / pace 150bpm
           distance ↑
           duration same
           pace same (1500/6=250 vs 1500/5=300 → -50 s/km → lower)
           avg_hr ↓
        """
        rows = []
        for i in range(5):
            tid = f"base_{i}"
            rows.append((tid, "run", "跑步", 5000.0, 1500, 150.0,
                         ts(2026, 8, 1) + i * 86400,
                         f"2026-08-01T00:00:00", None))
        rows.append(("target", "run", "跑步", 6000.0, 1500, 140.0,
                     ts(2026, 9, 22),
                     "2026-09-22T00:00:00", None))
        db = make_workouts_db(rows)
        ins = compute_workout_insight(db, "target", today=TODAY)
        fd = next(f for f in ins.facts if f.fact_id == "run.distance")
        self.assertEqual(fd.direction, "higher")
        fhr = next(f for f in ins.facts if f.fact_id == "run.avg_hr")
        self.assertEqual(fhr.direction, "lower")

    def test_insufficient_when_only_2_baselines(self):
        rows = []
        for i in range(2):
            rows.append((f"b{i}", "run", "跑步", 5000.0, 1500, 140.0,
                         ts(2026, 8, 1) + i * 86400,
                         "2026-08-01T00:00:00", None))
        rows.append(("target", "run", "跑步", 6000.0, 1800, 150.0,
                     ts(2026, 9, 22),
                     "2026-09-22T00:00:00", None))
        db = make_workouts_db(rows)
        ins = compute_workout_insight(db, "target", today=TODAY)
        fd = next(f for f in ins.facts if f.fact_id == "run.distance")
        self.assertEqual(fd.confidence, "Insufficient")

    def test_implausible_pace_excluded(self):
        """implausible baseline pace → baseline 为空 → fact 标 implausible_pace"""
        rows = []
        for i in range(5):
            rows.append((f"b{i}", "run", "跑步", 5000.0, 100, 140.0,
                         ts(2026, 8, 1) + i * 86400,
                         "2026-08-01T00:00:00", None))  # pace = 100/5 = 20 s/km implausible
        rows.append(("target", "run", "跑步", 5000.0, 1500, 150.0,
                     ts(2026, 9, 22),
                     "2026-09-22T00:00:00", None))
        db = make_workouts_db(rows)
        ins = compute_workout_insight(db, "target", today=TODAY)
        fp = next(f for f in ins.facts if f.fact_id == "run.pace")
        # baseline pace 都是 implausible 被丢弃 → baseline 无值
        self.assertIsNone(fp.baseline_value)
        self.assertEqual(fp.excluded_code, "implausible_pace")

    def test_distance_outside_tolerance_excluded_from_baseline(self):
        """距离±20% 过滤"""
        rows = []
        for i in range(5):
            rows.append((f"b{i}", "run", "跑步", 10000.0, 3000, 140.0,
                         ts(2026, 8, 1) + i * 86400,
                         "2026-08-01T00:00:00", None))  # 10 km (target 5km → 100% diff)
        rows.append(("target", "run", "跑步", 5000.0, 1500, 150.0,
                     ts(2026, 9, 22),
                     "2026-09-22T00:00:00", None))
        db = make_workouts_db(rows)
        ins = compute_workout_insight(db, "target", today=TODAY)
        self.assertEqual(ins.samples_count, 0)
        fd = next(f for f in ins.facts if f.fact_id == "run.distance")
        self.assertEqual(fd.confidence, "Insufficient")

    def test_unsupported_workout_type(self):
        rows = [("w1", "walking", "健走", 5000.0, 1500, 140.0,
                 ts(2026, 9, 22), "2026-09-22T00:00:00", None)]
        db = make_workouts_db(rows)
        ins = compute_workout_insight(db, "w1", today=TODAY)
        self.assertFalse(ins.supported)
        self.assertEqual(ins.unsupported_code, "unsupported_workout_type")


# ===== 5. WeeklyReport =====

class WeeklyReportTest(unittest.TestCase):
    def _make_db(self, daily_metrics=None, sleep_sessions=None, workouts=None):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
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
            db.execute("""INSERT INTO daily_metrics (metric, date, value, source_scope, device_id)
                          VALUES (?, ?, ?, ?, ?)""", m)
        for s in (sleep_sessions or []):
            db.execute("""INSERT INTO sleep_sessions
                          (date, source, start_ts, end_ts, tz_offset_secs, time_in_bed_secs,
                           is_nap, deep_secs, light_secs, rem_secs, awake_secs)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", s)
        for w in (workouts or []):
            db.execute("""INSERT INTO workouts (trackid, sport_key, sport_zh, dis_m, run_s,
                          avg_heart_rate, end_time_ts, end_time_iso)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", w)
        db.commit()
        return db

    def test_basic_weekly_report(self):
        today = TODAY
        # resting_hr: recent 7 天 avg=60; baseline 28 天 avg=65
        recent_dates = [(today - timedelta(days=d)).isoformat() for d in range(7)]
        baseline_dates = [(today - timedelta(days=7 + d)).isoformat() for d in range(28)]
        dms = []
        for d in recent_dates:
            dms.append(("resting_hr", d, 60.0, "user_fused", None))
        for d in baseline_dates:
            dms.append(("resting_hr", d, 65.0, "user_fused", None))
        # 还需要其他 metric
        dms.append(("hrv", recent_dates[0], 45.0, "user_fused", None))
        dms.append(("hrv", baseline_dates[0], 40.0, "user_fused", None))
        dms.append(("stress", recent_dates[0], 30.0, "user_fused", None))
        dms.append(("stress", baseline_dates[0], 40.0, "user_fused", None))
        dms.append(("training_load", recent_dates[0], 100.0, "user_fused", None))
        dms.append(("training_load", baseline_dates[0], 80.0, "user_fused", None))

        # workouts: recent 5 条, baseline 0
        ws = []
        for i in range(5):
            d = (today - timedelta(days=i)).isoformat()
            ws.append((f"w{i}", "run", "跑步", 5000.0, 1500, 140.0,
                       ts(2026, 9, 23 - i), f"2026-09-{23-i:02d}T00:00:00"))

        # sleep sessions: recent 7 天
        ss = []
        for i in range(7):
            d = (today - timedelta(days=i)).isoformat()
            # start_ts 23:50 + 28800 (UTC+8)
            start_local = datetime(2026, 9, 23 - i, 23, 50, tzinfo=timezone(timedelta(hours=8)))
            ss.append((d, "device", int(start_local.timestamp()), 0, 28800, 7 * 3600, 0,
                       90*60, 96*60, 90*60, 4*60))

        db = self._make_db(dms, ss, ws)
        report = compute_weekly_report(db, today=today)
        fact_ids = {f.fact_id for f in report.facts}
        self.assertEqual(fact_ids, {
            "weekly.resting_hr", "weekly.hrv", "weekly.stress",
            "weekly.sleep_duration", "weekly.sleep_start_regularity",
            "weekly.workout_count", "weekly.training_load",
        })
        # resting_hr: recent=60, baseline=65 → lower
        rhr = next(f for f in report.facts if f.fact_id == "weekly.resting_hr")
        self.assertAlmostEqual(rhr.recent_value, 60.0)
        self.assertAlmostEqual(rhr.baseline_value, 65.0)
        self.assertEqual(rhr.direction, "lower")

    def test_thin_baseline_returns_weekly_thin(self):
        today = TODAY
        dms = []
        # recent: 7 days (today-6 ~ today)
        for d in range(7):
            day = (today - timedelta(days=d)).isoformat()
            dms.append(("resting_hr", day, 60.0, "user_fused", None))
        # baseline: 3 days only (today-9, today-10, today-11)
        # collapse_per_day 只收 [today-34, today-7]，所以 day 必须在那个范围内
        for d in [9, 10, 11]:
            day = (today - timedelta(days=d)).isoformat()
            dms.append(("resting_hr", day, 65.0, "user_fused", None))
        db = self._make_db(dms, [], [])
        report = compute_weekly_report(db, today=today)
        rhr = next(f for f in report.facts if f.fact_id == "weekly.resting_hr")
        self.assertEqual(rhr.excluded_code, "weekly_thin_baseline")

    def test_collapse_per_day(self):
        """同日多 scope（device + user_fused）→ 折叠"""
        today = TODAY
        dms = [
            ("resting_hr", (today - timedelta(days=0)).isoformat(), 60.0, "device", "A"),
            ("resting_hr", (today - timedelta(days=0)).isoformat(), 64.0, "user_fused", None),
        ]
        db = self._make_db(dms, [], [])
        d = collapse_per_day_metric(db, "daily_metrics", "resting_hr",
                                     (today - timedelta(days=0)).isoformat(),
                                     today.isoformat())
        self.assertEqual(len(d), 1)  # 同日折成 1 条
        self.assertAlmostEqual(list(d.values())[0], 62.0)  # (60+64)/2

    def test_sleep_start_regularity_crosses_midnight(self):
        """23:50 + 00:10 差 20 分钟而非 23 小时"""
        today = TODAY
        # 构造 2 天 sleep starts：23:50 和 00:10（同一天算两次→ stddev 应很小）
        ss = []
        # 第 1 天：start_ts = 23:50 本地（UTC+8） = 15:50 UTC
        start1_local = datetime(2026, 9, 22, 23, 50, tzinfo=timezone(timedelta(hours=8)))
        ss.append(("2026-09-22", "device", int(start1_local.timestamp()),
                   0, 28800, 7*3600, 0, 90*60, 96*60, 90*60, 4*60))
        # 第 2 天：start_ts = 00:10 本地（UTC+8） = 前一天 16:10 UTC
        start2_local = datetime(2026, 9, 21, 0, 10, tzinfo=timezone(timedelta(hours=8)))
        ss.append(("2026-09-21", "device", int(start2_local.timestamp()),
                   0, 28800, 7*3600, 0, 90*60, 96*60, 90*60, 4*60))
        db = self._make_db([], ss, [])
        starts = _daily_sleep_starts(db, "2026-09-21", "2026-09-22")
        self.assertEqual(len(starts), 2)
        v1 = starts["2026-09-22"]  # 23:50 → anchor 加 1440 → 1430 + 1440 = 2870
        v2 = starts["2026-09-21"]  # 00:10 → < anchor → 加 1440 → 10 + 1440 = 1450
        diff = abs(v1 - v2)
        # 23:50 vs 00:10 = 20 min 差（不是 23 小时 20 分）
        self.assertAlmostEqual(diff, 20, delta=2)


# ===== 6. weekly _make_weekly_fact edge cases =====

class WeeklyFactEdgeTest(unittest.TestCase):
    def test_zero_baseline(self):
        f = _make_weekly_fact("weekly.x", "x", "u", 5.0, 0.0, baseline_days=10)
        self.assertEqual(f.excluded_code, "weekly_zero_baseline")

    def test_thin_baseline(self):
        f = _make_weekly_fact("weekly.x", "x", "u", 5.0, 10.0, baseline_days=3)
        self.assertEqual(f.excluded_code, "weekly_thin_baseline")

    def test_no_data(self):
        f = _make_weekly_fact("weekly.x", "x", "u", None, None, baseline_days=0)
        self.assertEqual(f.excluded_code, "weekly_no_data")


if __name__ == "__main__":
    unittest.main()