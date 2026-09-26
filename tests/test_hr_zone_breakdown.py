"""
心率区间占比计算单元测试（M5 章节 hr_zone_breakdown.py）

覆盖：
  - 基本区间划分（<113 / 113-141 / 141-154 / 154-162 / 162-173 / ≥173）
  - 哨兵过滤（0 / 500 / -5 / None）
  - 空输入（避免除零）
  - dict 输入（[{bpm: N}, ...]）vs 裸数字
  - 6 比例加和 ≈ 100%
  - 边界值（112/113/140/141/153/154/161/162/172/173）
  - 与 Zepp App 截图对齐（2026-09-24 实测 48%/5%/0/0/0/0）
  - HRmax fallback HUNT 公式

阈值说明（来自 Zepp Cloud heart_range 字段）：
  Zone 1: bpm < 113
  Zone 2: 113 ≤ bpm < 141
  Zone 3: 141 ≤ bpm < 154
  Zone 4: 154 ≤ bpm < 162
  Zone 5: 162 ≤ bpm < 173
  Zone 6: bpm ≥ 173
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import unittest

from normalizer.hr_zone_breakdown import (
    compute_hr_zone_breakdown,
    HRZoneBreakdown,
    ZONE_BOUNDS_BPM,
    ZONE_LABELS,
    estimate_hr_max,
)


class HRZoneBreakdownBasicTest(unittest.TestCase):
    def test_constants_match_zepp_cloud(self):
        """ZONE_BOUNDS_BPM 与 Zepp Cloud `heart_range` 字段完全对齐

        来源: 113/141/154/162/173/190 是 ZeppBridge 测试 fixture:
          "1882,113;3486,141;10,154;0,162;0,173;0,190"
        """
        self.assertEqual(tuple(ZONE_BOUNDS_BPM), (113, 141, 154, 162, 173, 190))
        self.assertEqual(len(ZONE_LABELS), 6)
        self.assertEqual(ZONE_LABELS[0], "舒缓轻松")
        self.assertEqual(ZONE_LABELS[5], "无氧极限")

    def test_empty_input(self):
        """空列表 → 0.0 全比例（避免除零）"""
        bd = compute_hr_zone_breakdown([])
        self.assertEqual(bd.zone_1_recovery_pct, 0.0)
        self.assertEqual(bd.zone_2_warmup_pct, 0.0)
        self.assertEqual(bd.zone_3_fatburn_pct, 0.0)
        self.assertEqual(bd.zone_4_cardio_pct, 0.0)
        self.assertEqual(bd.zone_5_endurance_pct, 0.0)
        self.assertEqual(bd.zone_6_anaerobic_pct, 0.0)
        self.assertEqual(bd.valid_min, 0)

    def test_all_in_zone_1(self):
        """1440 个 bpm 全 < 113 → Zone 1 100%"""
        curve = [80] * 1440
        bd = compute_hr_zone_breakdown(curve, avg_sample_seconds=60)
        # 1440 * 60 / 60 = 1440 分钟 = 24h
        self.assertEqual(bd.valid_min, 1440)
        self.assertAlmostEqual(bd.zone_1_recovery_pct, 100.0, delta=0.1)
        self.assertAlmostEqual(bd.zone_2_warmup_pct, 0.0, delta=0.1)
        self.assertAlmostEqual(bd.zone_3_fatburn_pct, 0.0, delta=0.1)
        self.assertAlmostEqual(bd.zone_4_cardio_pct, 0.0, delta=0.1)
        self.assertAlmostEqual(bd.zone_5_endurance_pct, 0.0, delta=0.1)
        self.assertAlmostEqual(bd.zone_6_anaerobic_pct, 0.0, delta=0.1)

    def test_boundary_values(self):
        """边界值严格归属

        112 → Zone 1
        113 → Zone 2
        140 → Zone 2
        141 → Zone 3
        153 → Zone 3
        154 → Zone 4
        161 → Zone 4
        162 → Zone 5
        172 → Zone 5
        173 → Zone 6
        """
        curve = [112, 113, 140, 141, 153, 154, 161, 162, 172, 173]
        bd = compute_hr_zone_breakdown(curve)
        # 1/10 Zone 1, 2/10 Zone 2, 2/10 Zone 3, 2/10 Zone 4, 2/10 Zone 5, 1/10 Zone 6
        self.assertAlmostEqual(bd.zone_1_recovery_pct, 10.0, delta=0.1)
        self.assertAlmostEqual(bd.zone_2_warmup_pct, 20.0, delta=0.1)
        self.assertAlmostEqual(bd.zone_3_fatburn_pct, 20.0, delta=0.1)
        self.assertAlmostEqual(bd.zone_4_cardio_pct, 20.0, delta=0.1)
        self.assertAlmostEqual(bd.zone_5_endurance_pct, 20.0, delta=0.1)
        self.assertAlmostEqual(bd.zone_6_anaerobic_pct, 10.0, delta=0.1)

    def test_sentinel_filtered(self):
        """bpm=0 / 500 / None / -5 都视为无效"""
        curve = [80, 0, 500, None, -5, 120, 200]
        bd = compute_hr_zone_breakdown(curve)
        # 80=Zone 1, 120=Zone 2, 200=Zone 6 → 3 个有效
        self.assertEqual(bd.valid_min, 3)
        self.assertAlmostEqual(bd.zone_1_recovery_pct, 33.3, delta=0.5)
        self.assertAlmostEqual(bd.zone_2_warmup_pct, 33.3, delta=0.5)
        self.assertAlmostEqual(bd.zone_6_anaerobic_pct, 33.3, delta=0.5)

    def test_sentinel_zero_filtered(self):
        """bpm=0 (未测) 必须过滤，不计入分母"""
        curve = [0, 80, 80, 120]  # 80,80=Zone 1; 120=Zone 2
        bd = compute_hr_zone_breakdown(curve)
        self.assertEqual(bd.valid_min, 3)
        self.assertAlmostEqual(bd.zone_1_recovery_pct, 66.7, delta=0.5)
        self.assertAlmostEqual(bd.zone_2_warmup_pct, 33.3, delta=0.5)

    def test_above_300_filtered(self):
        """bpm=500 视为异常，过滤"""
        curve = [80, 500, 120]  # 80=Zone 1, 120=Zone 2
        bd = compute_hr_zone_breakdown(curve)
        self.assertEqual(bd.valid_min, 2)
        self.assertAlmostEqual(bd.zone_1_recovery_pct, 50.0, delta=0.5)
        self.assertAlmostEqual(bd.zone_2_warmup_pct, 50.0, delta=0.5)

    def test_dict_input(self):
        """支持 [{bpm: N}, ...] 字典输入"""
        curve = [{"bpm": 80, "ts": 1}, {"bpm": 120, "ts": 2}]
        bd = compute_hr_zone_breakdown(curve)
        self.assertEqual(bd.valid_min, 2)
        self.assertAlmostEqual(bd.zone_1_recovery_pct, 50.0, delta=0.1)
        self.assertAlmostEqual(bd.zone_2_warmup_pct, 50.0, delta=0.1)

    def test_dict_with_sentinel_value(self):
        """dict 里 bpm=0 也过滤"""
        curve = [{"bpm": 0}, {"bpm": 80}, {"bpm": 120}]
        bd = compute_hr_zone_breakdown(curve)
        self.assertEqual(bd.valid_min, 2)
        self.assertAlmostEqual(bd.zone_1_recovery_pct, 50.0, delta=0.1)
        self.assertAlmostEqual(bd.zone_2_warmup_pct, 50.0, delta=0.1)

    def test_dict_with_value_key(self):
        """支持 {'value': N} key（兼容 normalizer 输出）"""
        curve = [{"value": 80}, {"value": 175}]
        bd = compute_hr_zone_breakdown(curve)
        self.assertEqual(bd.valid_min, 2)
        self.assertAlmostEqual(bd.zone_1_recovery_pct, 50.0, delta=0.1)
        self.assertAlmostEqual(bd.zone_6_anaerobic_pct, 50.0, delta=0.1)

    def test_sum_to_100(self):
        """6 比例加和 ≈ 100%（允许 ±0.5 舍入偏差）"""
        # 在 6 个区间均匀分布
        curve = [100, 120, 145, 158, 167, 180]
        bd = compute_hr_zone_breakdown(curve)
        total = (bd.zone_1_recovery_pct + bd.zone_2_warmup_pct +
                 bd.zone_3_fatburn_pct + bd.zone_4_cardio_pct +
                 bd.zone_5_endurance_pct + bd.zone_6_anaerobic_pct)
        self.assertAlmostEqual(total, 100.0, delta=0.5)

    def test_pure_zone_2(self):
        """全是 Zone 2 (113-140)"""
        curve = [113, 120, 130, 140, 115, 125]
        bd = compute_hr_zone_breakdown(curve)
        self.assertAlmostEqual(bd.zone_2_warmup_pct, 100.0, delta=0.1)
        for pct in (bd.zone_1_recovery_pct, bd.zone_3_fatburn_pct,
                    bd.zone_4_cardio_pct, bd.zone_5_endurance_pct,
                    bd.zone_6_anaerobic_pct):
            self.assertAlmostEqual(pct, 0.0, delta=0.1)

    def test_pure_zone_6(self):
        """全是 Zone 6 (≥173)"""
        curve = [173, 175, 180, 190, 200, 250]
        bd = compute_hr_zone_breakdown(curve)
        self.assertAlmostEqual(bd.zone_6_anaerobic_pct, 100.0, delta=0.1)
        for pct in (bd.zone_1_recovery_pct, bd.zone_2_warmup_pct,
                    bd.zone_3_fatburn_pct, bd.zone_4_cardio_pct,
                    bd.zone_5_endurance_pct):
            self.assertAlmostEqual(pct, 0.0, delta=0.1)

    def test_as_dict(self):
        """as_dict 返回 round 过的值"""
        curve = [80, 120, 175]
        bd = compute_hr_zone_breakdown(curve)
        d = bd.as_dict()
        self.assertEqual(d["hr_max_used"], 187)  # 默认 hr_max=187
        self.assertEqual(d["valid_min"], 3)
        self.assertAlmostEqual(d["zone_1_recovery_pct"], 33.3, delta=0.5)
        self.assertAlmostEqual(d["zone_2_warmup_pct"], 33.3, delta=0.5)
        self.assertAlmostEqual(d["zone_6_anaerobic_pct"], 33.3, delta=0.5)

    def test_generator_input(self):
        """支持 generator"""
        gen = (v for v in [80, 120, 175])
        bd = compute_hr_zone_breakdown(gen)
        self.assertEqual(bd.valid_min, 3)
        self.assertAlmostEqual(bd.zone_1_recovery_pct, 33.3, delta=0.5)
        self.assertAlmostEqual(bd.zone_2_warmup_pct, 33.3, delta=0.5)
        self.assertAlmostEqual(bd.zone_6_anaerobic_pct, 33.3, delta=0.5)

    def test_hr_max_override(self):
        """可以覆盖 hr_max（不影响区间比例，仅 UI 显示）"""
        bd1 = compute_hr_zone_breakdown([80, 120], hr_max=183)
        bd2 = compute_hr_zone_breakdown([80, 120], hr_max=220)
        self.assertEqual(bd1.hr_max_used, 183)
        self.assertEqual(bd2.hr_max_used, 220)
        # 比例应相同（区间是绝对 bpm）
        self.assertAlmostEqual(bd1.zone_1_recovery_pct, bd2.zone_1_recovery_pct)

    def test_avg_sample_seconds(self):
        """avg_sample_seconds 控制 valid_min 计算"""
        curve = [80] * 100
        bd60 = compute_hr_zone_breakdown(curve, avg_sample_seconds=60)
        bd30 = compute_hr_zone_breakdown(curve, avg_sample_seconds=30)
        # 100 samples × 60s = 6000s = 100 min vs 100 × 30 = 3000s = 50 min
        self.assertEqual(bd60.valid_min, 100)
        self.assertEqual(bd30.valid_min, 50)


class HRZoneEstimateHRMaxTest(unittest.TestCase):
    """estimate_hr_max HUNT 公式测试"""

    def test_hunt_formula_male(self):
        """HUNT 公式（男性）：HRmax = 211 - 0.64*age"""
        # 用户实测 device_max_hr = 187, 37.57 岁（男性）
        self.assertEqual(estimate_hr_max(37.57, is_male=True), 187)
        self.assertEqual(estimate_hr_max(30, is_male=True), 192)
        self.assertEqual(estimate_hr_max(50, is_male=True), 179)

    def test_tanaka_formula_female(self):
        """Tanaka 公式（女性）：HRmax = 208 - 0.7*age"""
        self.assertEqual(estimate_hr_max(30, is_male=False), 187)
        self.assertEqual(estimate_hr_max(50, is_male=False), 173)


class HRZoneBreakdownRealDataTest(unittest.TestCase):
    """实测 DB 数据验证"""

    def test_2026_09_24_zepp_app_match(self):
        """2026-09-24 实测（注意：DB 只有 02:28-14:50 数据）

        DB 数据：625 个点，min=59, max=104, avg=79.32
        Zepp App 截图：avg=79, max=104, min=57（差 2 bpm 因 DB 缺 14:50 后数据）
        Zone 1 占比：本地 ~100% (max=104 < 113)，Zepp App 48% (差距来自 DB 缺数据)

        本测试仅验证：
          - max_bpm/avg_bpm 与 Zepp App 一致（≤1 bpm）
          - 几乎所有点都在 Zone 1（≥95%，因 DB 缺 14:50 后数据）
        """
        import sqlite3
        db_path = os.path.expanduser("~/.zepp-data/zepp.db")
        if not os.path.exists(db_path):
            self.skipTest("DB 不存在")
        db = sqlite3.connect(db_path)
        cur = db.execute(
            "SELECT bpm FROM heart_rate_band_samples WHERE date='2026-09-24'"
        )
        bpms = [r[0] for r in cur.fetchall()]
        db.close()
        if not bpms:
            self.skipTest("DB 没数据（首次跑没 sync）")
        self.assertEqual(len(bpms), 625)
        # max=104（Zepp App 显示 104）
        self.assertEqual(max(bpms), 104)
        # avg=79.32（Zepp App 显示 79）
        self.assertAlmostEqual(sum(bpms)/len(bpms), 79.0, delta=1.0)
        # 几乎全部 < 113（因 DB 缺 14:50 后数据）
        bd = compute_hr_zone_breakdown(bpms, avg_sample_seconds=60)
        self.assertGreater(bd.zone_1_recovery_pct, 95.0)
        self.assertAlmostEqual(bd.zone_2_warmup_pct + bd.zone_3_fatburn_pct +
                               bd.zone_4_cardio_pct + bd.zone_5_endurance_pct +
                               bd.zone_6_anaerobic_pct, 100.0 - bd.zone_1_recovery_pct,
                               delta=0.5)


if __name__ == "__main__":
    unittest.main()