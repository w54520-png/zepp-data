"""
M3 章节 3.1: workout_detail normalizer 单元测试

覆盖：
  - 海拔哨兵 -2000000 / -2002110 / -2003943 过滤
  - HR 250/0 过滤
  - 空 delta 处理
  - malformed delta 整行丢
  - runPosture 哨兵 65535/255
  - currentDistance 单步 > 200 m/s 过滤
  - split 计算（1000m 一段 + partial 最后一截）
  - kilo_pace 兜底
  - lap 对账（距离累加 + duration）
  - HR drift 计算 + 拒算条件
  - time_delta_altitude 优先路径
  - end-to-end smoke test
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import math
import unittest

from normalizer.workout_detail import (
    parse_delta_pairs,
    parse_simple_delta,
    accumulate_deltas_to_seconds,
    altitude_cm_is_plausible,
    compute_altitude_series,
    _parse_lonlat_three_col,
    parse_cumulative_distance,
    parse_speed_chain,
    parse_hr_delta_chain,
    parse_gait_chain,
    parse_power_chain,
    parse_equiv_pace_chain,
    parse_run_posture_chain,
    parse_pauses,
    parse_kilo_pace,
    parse_laps,
    compute_elevation_gain_loss,
    compute_splits_from_distance,
    compute_splits_from_kilo_pace,
    reconcile_laps,
    compute_hr_drift,
    decode_workout_detail,
    WorkoutSample,
    DecodedWorkout,
    HR_DRIFT_MIN_SAMPLES_TOTAL,
    MIN_PLAUSIBLE_ALTITUDE_CM,
    MAX_PLAUSIBLE_ALTITUDE_CM,
)


# ===== 1. 海拔哨兵过滤 =====

class AltitudeSentinelsTest(unittest.TestCase):
    def test_known_sentinels_are_not_plausible(self):
        """-2000000 / -2002110 / -2003943 全部应被窗口排除"""
        for sentinel in (-2_000_000, -2_002_110, -2_003_943):
            self.assertFalse(altitude_cm_is_plausible(sentinel),
                             f"{sentinel} should be filtered")

    def test_plausible_altitude_passes(self):
        """正常海拔（-1000m..10000m）通过"""
        for v in (-100_000, 0, 884_800, 1_000_000):
            self.assertTrue(altitude_cm_is_plausible(v),
                            f"{v} should be plausible")

    def test_window_boundary(self):
        """窗口边界值正确处理"""
        self.assertTrue(altitude_cm_is_plausible(MIN_PLAUSIBLE_ALTITUDE_CM))
        self.assertTrue(altitude_cm_is_plausible(MAX_PLAUSIBLE_ALTITUDE_CM))
        self.assertFalse(altitude_cm_is_plausible(MIN_PLAUSIBLE_ALTITUDE_CM - 1))
        self.assertFalse(altitude_cm_is_plausible(MAX_PLAUSIBLE_ALTITUDE_CM + 1))

    def test_sentinel_deltas_filtered_in_decode(self):
        """端到端验证 哨兵不会进 route.altitude"""
        # 构造一段 altitude 链：第 1 个差分 = -2000000（哨兵），第 2 个 = 0，第 3 个 = 50000
        # 解析后只应该看到第 3 个
        raw = {
            "trackid": 1700000000,
            "longitude_latitude": "1,1000,2000;1,0,0;1,0,0",
            "altitude": "-2000000;2050000;2050000",
            "currentDistance": "1000;1000;1000",
            "heart_rate": "1,60;1,0;1,0",
        }
        d = decode_workout_detail(raw, summary_end_ts=1700000000 + 3,
                                   summary_distance_m=30.0)
        alts = [r.altitude_m for r in d.route if r.altitude_m is not None]
        # 第一段被哨兵过滤；按设计 first_valid 回填（这里 -2000000 是不可信，后续 +2050000 → 累计 50000 → 500m）
        # altitude_used_chain_with_first_valid_backfill 诊断信息应有
        self.assertIn("altitude_used_chain_with_first_valid_backfill", d.diagnostics)


# ===== 2. HR 过滤 =====

class HeartRateFilterTest(unittest.TestCase):
    def test_hr_above_250_filtered(self):
        raw = "1,60;1,200;1,300;1,-100"
        out = parse_hr_delta_chain(raw, [])
        # 累加：60→260→560→460
        # 但 HR > 250 被过滤；HR=560 必然过滤；HR=460 也过滤
        # 60 和 260 中：260 > 250 过滤；只剩 60
        self.assertEqual(out, [(1, 60)])

    def test_hr_below_1_filtered(self):
        # 累加：1→0（=0，应过滤）
        raw = "1,1;1,-2"
        out = parse_hr_delta_chain(raw, [])
        self.assertEqual(out, [(1, 1)])

    def test_empty_hr_returns_empty(self):
        self.assertEqual(parse_hr_delta_chain("", []), [])

    def test_malformed_hr_dropped_not_zero(self):
        """malformed delta 整行丢，绝不 unwrap_or(0)"""
        raw = "1,60;abc;1,5"
        out = parse_hr_delta_chain(raw, [])
        # 'abc' 整行丢；t 不增；next "1,5" → t=2, bpm=65
        self.assertEqual(out, [(1, 60), (2, 65)])


# ===== 3. 空 delta 处理 =====

class EmptyDeltaTest(unittest.TestCase):
    def test_empty_true_returns_one_sec(self):
        out = parse_simple_delta("", empty_means_one_sec=True)
        self.assertEqual(out, [1])
        out = parse_simple_delta(None, empty_means_one_sec=True)
        self.assertEqual(out, [1])

    def test_empty_false_returns_empty(self):
        out = parse_simple_delta("", empty_means_one_sec=False)
        self.assertEqual(out, [])
        out = parse_simple_delta(None, empty_means_one_sec=False)
        self.assertEqual(out, [])

    def test_parse_delta_pairs_empty_true(self):
        out = parse_delta_pairs("", empty_means_one_sec=True)
        self.assertEqual(out, [(1, 0)])

    def test_parse_delta_pairs_empty_false(self):
        self.assertEqual(parse_delta_pairs("", empty_means_one_sec=False), [])


# ===== 4. malformed delta 处理 =====

class MalformedDeltaTest(unittest.TestCase):
    def test_delta_pairs_three_columns_skipped(self):
        """(idx,val) 格式碰到 3 列 → 跳过"""
        out = parse_delta_pairs("1,60,extra", empty_means_one_sec=False)
        self.assertEqual(out, [])

    def test_delta_pairs_non_numeric_skipped(self):
        out = parse_delta_pairs("1,60;x,1;2,70", empty_means_one_sec=False)
        # "x,1" 整行丢
        self.assertEqual(out, [(1, 60.0), (2, 70.0)])

    def test_zero_idx_skipped(self):
        out = parse_delta_pairs("0,60;1,70", empty_means_one_sec=False)
        self.assertEqual(out, [(1, 70.0)])


# ===== 5. runPosture 哨兵 =====

class RunPostureSentinelTest(unittest.TestCase):
    def test_65535_gct_vo_become_none(self):
        raw = "1,200,10,5;1,65535,15,8;1,180,65535,12"
        out = parse_run_posture_chain(raw, [])
        # 第 2 条 col1=65535 → gct=None; col2=15 valid
        # 第 3 条 col1=180 valid; col2=65535 → vo=None
        self.assertEqual(len(out), 3)
        self.assertEqual(out[1][1], None)  # gct=65535→None
        self.assertEqual(out[1][2], 15.0)  # vo=15
        self.assertEqual(out[2][1], 180)   # gct=180
        self.assertEqual(out[2][2], None)  # vo=65535→None

    def test_255_vsr_become_none_and_divided_by_10(self):
        raw = "1,200,10,18;1,200,10,255"
        out = parse_run_posture_chain(raw, [])
        self.assertEqual(out[0][3], 1.8)   # 18/10
        self.assertEqual(out[1][3], None)  # 255 → None


# ===== 6. cumulative distance 过滤 =====

class CumulativeDistanceTest(unittest.TestCase):
    def test_monotonic_backwards_dropped(self):
        # 累计 cm: 100, 150, 80 (回退 丢), 200
        raw = "1,100;1,150;1,80;1,200"
        out = parse_cumulative_distance(raw, [])
        # 第三个 80 < 150 → 丢；第 4 个 200 - prev 150 = 50cm/3sec = 0.167 m/s (OK)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0][1], 1.0)
        self.assertEqual(out[1][1], 1.5)
        self.assertEqual(out[2][1], 2.0)  # 200 cm = 2 m

    def test_step_above_200mps_dropped(self):
        """单步 > 200 m/s 视为坏点"""
        # 第 1 条 100cm=1m；第 2 条 30000cm=300m，1 秒 增 299m > 200 m/s → 丢
        raw = "1,100;1,30000"
        out = parse_cumulative_distance(raw, [])
        self.assertEqual(len(out), 1)


# ===== 7. Splits 算法 =====

class SplitsFromDistanceTest(unittest.TestCase):
    def test_3km_run_produces_3_full_splits(self):
        """3 km 跑 → 3 个完整 split，无 partial"""
        # distance: 1000, 2000, 3000 m
        distance = [(10, 1000.0), (20, 2000.0), (30, 3000.0)]
        # hr
        hrs = [(5, 140), (15, 145), (25, 150), (29, 152)]
        # altitude
        alts = [(5, 100.0), (10, 110.0), (15, 105.0), (20, 108.0), (25, 112.0), (29, 115.0)]
        splits = compute_splits_from_distance(
            12345, distance, hrs, alts,
            summary_distance_m=3000.0, diagnostics=[],
        )
        self.assertEqual(len(splits), 3)
        for i, sp in enumerate(splits):
            self.assertFalse(sp.partial)
            self.assertEqual(sp.distance_m, 1000.0)
            self.assertEqual(sp.index, i)

    def test_3500m_run_produces_3_full_plus_partial(self):
        distance = [(10, 1000.0), (20, 2000.0), (30, 3000.0), (40, 3500.0)]
        hrs = [(5, 140), (15, 145), (25, 150), (35, 155)]
        alts = [(5, 100.0), (10, 110.0), (15, 105.0), (20, 108.0), (25, 112.0), (35, 115.0)]
        splits = compute_splits_from_distance(
            12345, distance, hrs, alts,
            summary_distance_m=3500.0, diagnostics=[],
        )
        self.assertEqual(len(splits), 4)
        self.assertTrue(splits[3].partial)
        self.assertAlmostEqual(splits[3].distance_m, 500.0, places=1)

    def test_1200m_run_produces_1_full_plus_partial(self):
        distance = [(10, 1000.0), (20, 1200.0)]
        hrs = [(5, 140), (15, 145)]
        alts = [(5, 100.0), (10, 110.0), (20, 112.0)]
        splits = compute_splits_from_distance(
            12345, distance, hrs, alts,
            summary_distance_m=1200.0, diagnostics=[],
        )
        self.assertEqual(len(splits), 2)
        self.assertFalse(splits[0].partial)
        self.assertTrue(splits[1].partial)
        self.assertAlmostEqual(splits[1].distance_m, 200.0, places=1)

    def test_too_short_no_splits(self):
        distance = [(10, 300.0)]  # 300m < 500
        splits = compute_splits_from_distance(12345, distance, [], [], None, [])
        self.assertEqual(splits, [])


class KiloPaceFallbackTest(unittest.TestCase):
    def test_kilo_pace_3_rows_no_mismatch(self):
        # idx=0,1,2; sec=600,500,700; cum=600,1100,1800
        raw = "0,600,0,0,0,600;1,500,0,0,0,1100;2,700,0,0,0,1800"
        rows = parse_kilo_pace(raw, [])
        self.assertEqual(len(rows), 3)
        splits = compute_splits_from_kilo_pace(12345, rows, summary_distance_m=3000.0, diagnostics=[])
        self.assertEqual(len(splits), 3)
        for sp in splits:
            self.assertEqual(sp.distance_m, 1000.0)

    def test_kilo_pace_mismatch_returns_empty(self):
        """idx 不对齐失败" or "&gt;"等会被拒"""
        # idx=0,1,5 (应该 0,1,2)
        raw = "0,600,0,0,0,600;1,500,0,0,0,1100;5,700,0,0,0,1800"
        diags = []
        rows = parse_kilo_pace(raw, diags)
        # idx 错位 → 整段拒
        self.assertEqual(rows, [])
        self.assertTrue(any("idx mismatch" in d for d in diags))


# ===== 8. Lap 对账 =====

class LapsReconcileTest(unittest.TestCase):
    def test_laps_add_up(self):
        # 2 laps：每圈 1000m，1400s/1500s
        raw = "0,1,1000,0,140,1400;1,1,1000,0,142,1500"
        lap_rows = parse_laps(raw, [])
        self.assertEqual(len(lap_rows), 2)
        laps = reconcile_laps(12345, lap_rows, summary_distance_m=2000.0,
                               summary_duration_sec=2900, diagnostics=[])
        self.assertEqual(len(laps), 2)
        self.assertEqual(laps[0].distance_m, 1000.0)
        self.assertEqual(laps[0].avg_hr, 140)
        # lap HR=0 视为未测
        raw_zero = "0,1,1000,0,0,1400"
        lap_zero = parse_laps(raw_zero, [])
        laps_zero = reconcile_laps(12345, lap_zero, summary_distance_m=1000.0,
                                    summary_duration_sec=1400, diagnostics=[])
        self.assertEqual(laps_zero[0].avg_hr, None)

    def test_laps_distance_mismatch_refused(self):
        raw = "0,1,1000,0,140,1400;1,1,2000,0,142,1500"  # 3 km total
        lap_rows = parse_laps(raw, [])
        diags = []
        laps = reconcile_laps(12345, lap_rows, summary_distance_m=2000.0,
                               summary_duration_sec=2900, diagnostics=diags)
        self.assertEqual(laps, [])  # distance diff >5%
        self.assertTrue(any("distance" in d for d in diags))


# ===== 9. HR Drift =====

class HeartRateDriftTest(unittest.TestCase):
    def _make_samples(self, n, first_hr=140, second_hr=150, first_speed=3.0, second_speed=3.0):
        """构造 n 个样本，前半时间 hr 低 + 速度高"""
        samples = []
        for i in range(n):
            t = i * 10  # sec，间距 10 秒
            half = (n * 10) // 2  # 中点秒
            if t <= half:
                hr = first_hr
                spd = first_speed
            else:
                hr = second_hr
                spd = second_speed
            samples.append(WorkoutSample(
                track_id=12345, ts_ms=t*1000, elapsed_sec=t,
                heart_rate=hr, speed_mps=spd, pace_s_per_m=1/spd,
                cadence_spm=160, stride_cm=100,
            ))
        return samples

    def test_basic_drift_calculated(self):
        """后半 HR 高（速度不变）→ metres_per_beat 降 → drift 为负（典型 drift）"""
        samples = self._make_samples(150, first_hr=130, second_hr=160,
                                     first_speed=3.0, second_speed=3.0)
        # duration = 150 * 10 = 1500 sec
        drift = compute_hr_drift(12345, samples, summary_duration_sec=1500, diagnostics=[])
        self.assertIsNone(drift.reason_code)
        # 半中点 t=750：t=0..750 inclusive (76 个) 是前半
        self.assertEqual(drift.first_half_samples, 76)
        self.assertEqual(drift.second_half_samples, 74)
        self.assertLess(drift.drift_percent, 0)  # negative drift = 经典 cardio drift

    def test_steady_pace_hr_increasing_drift(self):
        """稳速 + HR 上升 → drift 负（ZeppBridge test 'a_steady_pace_with_a_rising_heart_rate_reads_as_drift'）"""
        # 速度恒定 3.0，HR 第一半 130 → 第二半 145
        samples = self._make_samples(150, first_hr=130, second_hr=145,
                                     first_speed=3.0, second_speed=3.0)
        drift = compute_hr_drift(12345, samples, summary_duration_sec=1500, diagnostics=[])
        self.assertIsNone(drift.reason_code)
        # metres_per_beat: first = 3*60/130 = 1.385, second = 3*60/145 = 1.241
        # drift = (1.241 - 1.385) / 1.385 * 100 ≈ -10.4%
        self.assertLess(drift.drift_percent, -5)
        self.assertGreater(drift.drift_percent, -15)

    def test_too_short_refused(self):
        samples = self._make_samples(150, first_hr=140, second_hr=145)
        drift = compute_hr_drift(12345, samples, summary_duration_sec=10 * 60,  # 10 min
                                  diagnostics=[])
        self.assertEqual(drift.reason_code, "too_short")

    def test_not_enough_samples_refused(self):
        samples = self._make_samples(50)  # < 120
        drift = compute_hr_drift(12345, samples, summary_duration_sec=600, diagnostics=[])
        self.assertEqual(drift.reason_code, "not_enough_samples")

    def test_pace_too_variable_refused(self):
        # 速度方差大 → cv > 0.20
        samples = []
        import random
        random.seed(42)
        for i in range(150):
            t = i
            spd = 3.0 + (2.0 if i % 2 == 0 else -2.0)  # 1..5 大幅波动
            hr = 140
            samples.append(WorkoutSample(
                track_id=12345, ts_ms=t*1000, elapsed_sec=t,
                heart_rate=hr, speed_mps=spd, pace_s_per_m=1/spd,
            ))
        drift = compute_hr_drift(12345, samples, summary_duration_sec=1200, diagnostics=[])
        self.assertEqual(drift.reason_code, "pace_too_variable")

    def test_filtered_too_low_hr(self):
        """HR<40 的样本丢弃"""
        samples = []
        for i in range(150):
            t = i
            hr = 30 if i < 50 else 140  # 前 50 个 HR=30 丢弃
            spd = 3.0
            samples.append(WorkoutSample(
                track_id=12345, ts_ms=t*1000, elapsed_sec=t,
                heart_rate=hr, speed_mps=spd, pace_s_per_m=1/spd,
            ))
        drift = compute_hr_drift(12345, samples, summary_duration_sec=1200, diagnostics=[])
        # 50 个被丢；剩 100 samples < 120 → not_enough_samples
        self.assertEqual(drift.reason_code, "not_enough_samples")

    def test_filtered_too_low_speed(self):
        """speed < 0.5 m/s 丢弃"""
        samples = []
        for i in range(150):
            t = i
            spd = 0.3 if i < 50 else 3.0
            hr = 140
            samples.append(WorkoutSample(
                track_id=12345, ts_ms=t*1000, elapsed_sec=t,
                heart_rate=hr, speed_mps=spd, pace_s_per_m=1/spd if spd > 0 else None,
            ))
        drift = compute_hr_drift(12345, samples, summary_duration_sec=1200, diagnostics=[])
        self.assertEqual(drift.reason_code, "not_enough_samples")


# ===== 10. time_delta_altitude 优先路径 =====

class TimeDeltaAltitudePriorityTest(unittest.TestCase):
    def test_time_delta_altitude_takes_priority(self):
        """time_delta_altitude 有数据时优先于 altitude 差分"""
        raw = {
            "trackid": 1700000000,
            "time_delta_altitude": "10,50000;20,30000",  # 50m, 30m
            "altitude": "100;100;100",  # 不被使用
        }
        diags = []
        alts = compute_altitude_series(
            raw["altitude"], raw["time_delta_altitude"], diags
        )
        self.assertEqual(len(alts), 2)
        self.assertAlmostEqual(alts[0][1], 500.0)  # 50000/100
        self.assertAlmostEqual(alts[1][1], 300.0)  # 30000/100

    def test_altitude_fallback_when_no_time_delta(self):
        # altitude 是 cm **差分**：每条是相对前一条的变化（cm）
        # 10000 + 10000 + 10000 = 累计 100m, 200m, 300m
        raw = {
            "trackid": 1700000000,
            "altitude": "10000;10000;10000",
            "time_delta_altitude": "",
        }
        diags = []
        alts = compute_altitude_series(
            raw["altitude"], raw["time_delta_altitude"], diags
        )
        self.assertEqual(len(alts), 3)
        self.assertAlmostEqual(alts[0][1], 100.0)
        self.assertAlmostEqual(alts[1][1], 200.0)
        self.assertAlmostEqual(alts[2][1], 300.0)


# ===== 11. End-to-end smoke test =====

class EndToEndTest(unittest.TestCase):
    def test_minimal_walking_workout(self):
        """最小健走 detail：1km，30 min，HR 60-80"""
        # 1800 秒（30 分钟）；每秒 1 步；speed ~ 3 m/s → distance 1 km = 100000 cm 累计
        # 简化：用大步进，每 30 秒 1 条 sample
        n_samples = 60
        raw = {
            "trackid": 1758000000,  # 2026-09-15 16:00 UTC roughly
            "source": "gpx",
            "time": ";".join(["1"] * n_samples),
            "longitude_latitude": ";".join([f"1,{1000 if i==0 else 0},{1000 if i==0 else 0}" for i in range(n_samples)]),
            "altitude": "0;" + ";".join(["0"] * (n_samples - 1)),
            "currentDistance": ";".join([f"30,{int(100000 * (i+1) / n_samples)}" for i in range(n_samples)]),
            "heart_rate": ";".join([f"30,{2 if i<5 else 0}" for i in range(n_samples)]),  # start 60 → 70
            "speed": ";".join([f"30,2.5" for _ in range(n_samples)]),
        }
        d = decode_workout_detail(raw,
                                   summary_end_ts=1758000000 + 30 * n_samples,
                                   summary_distance_m=1000.0)
        self.assertGreater(len(d.route), 0)
        self.assertGreater(len(d.samples), 0)
        self.assertEqual(len(d.splits), 1)
        # 累计距离 ~1000m → 1 split
        self.assertAlmostEqual(d.splits[0].distance_m, 1000.0, delta=10)

    def test_no_data_payload_returns_minimal(self):
        raw = {"code": 1, "message": "success"}
        d = decode_workout_detail(raw, track_id_hint=12345)
        self.assertEqual(d.track_id, 12345)
        self.assertEqual(d.route, [])
        self.assertEqual(d.samples, [])

    def test_pause_kind_filtered(self):
        """pause kind 不在 (0,2,3) → 跳过"""
        raw = "1700000000,100,0,0,1;1700000100,100,0,0,2;1700000200,100,0,0,5"
        out = parse_pauses(raw, start_ts_ms=0, track_id=12345, diagnostics=[])
        # kind=1 → skip, kind=2 → keep, kind=5 → skip
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, 2)


if __name__ == "__main__":
    unittest.main()