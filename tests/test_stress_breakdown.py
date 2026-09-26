"""
压力区间占比计算单元测试（M5 章节 stress_breakdown.py）

覆盖：
  - 基本区间划分（<40 / 40-59 / 60-79 / >=80）
  - 哨兵过滤（0 / 200 / 负数）
  - 空输入
  - dict 输入（[{value: N}, ...]）vs 裸数字
  - 4 比例加和 ≈ 100%
  - 与 Zepp App 截图对齐（2026-09-24 实测 0/48/42/10）
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import unittest

from normalizer.stress_breakdown import (
    compute_stress_breakdown,
    StressBreakdown,
    BOUNDARIES,
)


class StressBreakdownBasicTest(unittest.TestCase):
    def test_known_distribution_zepp_app_screenshot(self):
        """Zepp App 截图 2026-09-24：放松 0 / 正常 47 / 中等 43 / 偏高 10（71 曲线点）

        Server 直接给的是 0 / 48 / 42 / 10；本地图重算 0 / 47.2 / 43.1 / 9.7
        """
        # 真实 9-24 曲线 71 点（不含当天 08:00 的日均条目）
        # 34 正常 + 31 中等 + 7 偏高 = 71
        curve = [
            61, 64, 67, 68, 51, 75, 64, 45, 71, 44, 66, 44, 51, 51, 60, 59, 46,
            67, 76, 83, 80, 46, 49, 61, 46, 75, 45, 52, 76, 75, 47, 73, 78, 77,
            64, 46, 45, 69, 62, 47, 53, 78, 82, 79, 54, 57, 46, 47, 58, 50, 68,
            82, 74, 80, 81, 79, 65, 81, 76, 44, 63, 51, 47, 45, 45, 62, 43, 43,
            46, 44, 44,
        ]
        self.assertEqual(len(curve), 71)
        bd = compute_stress_breakdown(curve)
        # 容忍 ±0.5pp
        self.assertAlmostEqual(bd.relax_pct, 0.0, delta=0.5)
        # 34/71 = 47.9%
        self.assertAlmostEqual(bd.normal_pct, 47.9, delta=1.0)
        # 30/71 = 42.3%
        self.assertAlmostEqual(bd.medium_pct, 42.3, delta=1.0)
        # 7/71 = 9.9%
        self.assertAlmostEqual(bd.high_pct, 9.9, delta=0.5)
        self.assertEqual(bd.total_points, 71)
        self.assertEqual(bd.valid_points, 71)

    def test_empty_input(self):
        """空列表 → 0.0/0.0/0.0/0.0（避免除零）"""
        bd = compute_stress_breakdown([])
        self.assertEqual(bd.relax_pct, 0.0)
        self.assertEqual(bd.normal_pct, 0.0)
        self.assertEqual(bd.medium_pct, 0.0)
        self.assertEqual(bd.high_pct, 0.0)
        self.assertEqual(bd.total_points, 0)
        self.assertEqual(bd.valid_points, 0)

    def test_all_sentinel(self):
        """全是哨兵（0 / 200 / -5）→ 全部被过滤，valid_points=0"""
        curve = [0, 200, -5, None, 0, 0]
        bd = compute_stress_breakdown(curve)
        self.assertEqual(bd.valid_points, 0)
        self.assertEqual(bd.total_points, 6)
        self.assertEqual(bd.relax_pct, 0.0)
        self.assertEqual(bd.normal_pct, 0.0)
        self.assertEqual(bd.medium_pct, 0.0)
        self.assertEqual(bd.high_pct, 0.0)

    def test_zero_sentinel_filtered(self):
        """value=0（未测）必须过滤，不计入分母"""
        curve = [0, 50, 60, 70]  # 50=正常, 60=中等, 70=中等
        bd = compute_stress_breakdown(curve)
        self.assertEqual(bd.valid_points, 3)
        self.assertAlmostEqual(bd.normal_pct, 33.3, delta=0.1)
        self.assertAlmostEqual(bd.medium_pct, 66.7, delta=0.1)

    def test_above_100_filtered(self):
        """value=200 视为异常，过滤"""
        curve = [50, 60, 200, 70]  # 50=正常, 60=中等, 70=中等
        bd = compute_stress_breakdown(curve)
        self.assertEqual(bd.valid_points, 3)
        self.assertAlmostEqual(bd.normal_pct, 33.3, delta=0.1)
        self.assertAlmostEqual(bd.medium_pct, 66.7, delta=0.1)

    def test_dict_input(self):
        """支持 [{value: N}, ...] 字典输入"""
        curve = [{"time": 1, "value": 50}, {"time": 2, "value": 65}]
        bd = compute_stress_breakdown(curve)
        self.assertEqual(bd.valid_points, 2)
        self.assertAlmostEqual(bd.normal_pct, 50.0, delta=0.1)
        self.assertAlmostEqual(bd.medium_pct, 50.0, delta=0.1)

    def test_dict_with_sentinel_value(self):
        """dict 里 value=0 也过滤"""
        curve = [{"value": 0}, {"value": 50}, {"value": 60}]
        bd = compute_stress_breakdown(curve)
        self.assertEqual(bd.total_points, 3)
        self.assertEqual(bd.valid_points, 2)
        self.assertAlmostEqual(bd.normal_pct, 50.0, delta=0.1)
        self.assertAlmostEqual(bd.medium_pct, 50.0, delta=0.1)

    def test_boundary_values(self):
        """边界值严格归属

        39 → 放松
        40 → 正常
        59 → 正常
        60 → 中等
        79 → 中等
        80 → 偏高
        100 → 偏高
        """
        curve = [39, 40, 59, 60, 79, 80, 100]
        bd = compute_stress_breakdown(curve)
        self.assertAlmostEqual(bd.relax_pct, 1/7*100, delta=0.5)
        self.assertAlmostEqual(bd.normal_pct, 2/7*100, delta=0.5)
        self.assertAlmostEqual(bd.medium_pct, 2/7*100, delta=0.5)
        self.assertAlmostEqual(bd.high_pct, 2/7*100, delta=0.5)

    def test_sum_to_100(self):
        """4 比例加和 ≈ 100%（允许 ±0.1 舍入偏差）"""
        curve = list(range(1, 101))  # 1..100 全覆盖
        bd = compute_stress_breakdown(curve)
        total = bd.relax_pct + bd.normal_pct + bd.medium_pct + bd.high_pct
        self.assertAlmostEqual(total, 100.0, delta=0.5)

    def test_pure_relax(self):
        """全是放松（值 1-39）"""
        curve = [1, 20, 39, 39, 1, 30, 25]
        bd = compute_stress_breakdown(curve)
        self.assertAlmostEqual(bd.relax_pct, 100.0, delta=0.1)
        self.assertAlmostEqual(bd.normal_pct, 0.0, delta=0.1)
        self.assertAlmostEqual(bd.medium_pct, 0.0, delta=0.1)
        self.assertAlmostEqual(bd.high_pct, 0.0, delta=0.1)

    def test_pure_high(self):
        """全是偏高（值 80-100）"""
        curve = [80, 85, 90, 95, 100, 80, 88]
        bd = compute_stress_breakdown(curve)
        self.assertAlmostEqual(bd.relax_pct, 0.0, delta=0.1)
        self.assertAlmostEqual(bd.normal_pct, 0.0, delta=0.1)
        self.assertAlmostEqual(bd.medium_pct, 0.0, delta=0.1)
        self.assertAlmostEqual(bd.high_pct, 100.0, delta=0.1)

    def test_as_dict(self):
        """as_dict 返回 round 过的值"""
        # 50=normal, 60=medium, 70=medium → 1 normal + 2 medium = 100%
        curve = [50, 60, 70]
        bd = compute_stress_breakdown(curve)
        d = bd.as_dict()
        self.assertEqual(d["total_points"], 3)
        self.assertEqual(d["valid_points"], 3)
        # 1/3 normal, 2/3 medium
        self.assertAlmostEqual(d["normal_pct"], 33.3, delta=0.5)
        self.assertAlmostEqual(d["medium_pct"], 66.7, delta=0.5)

    def test_generator_input(self):
        """支持 generator（lazy 友好）"""
        # 50=normal, 60=medium, 70=medium, 80=high
        gen = (v for v in [50, 60, 70, 80])
        bd = compute_stress_breakdown(gen)
        self.assertEqual(bd.valid_points, 4)
        self.assertAlmostEqual(bd.normal_pct, 25.0, delta=0.5)
        # 60+70 = 2 medium → 50%
        self.assertAlmostEqual(bd.medium_pct, 50.0, delta=0.5)
        self.assertAlmostEqual(bd.high_pct, 25.0, delta=0.5)

    def test_boundaries_constant(self):
        """BOUNDARIES 常量与 Zepp App 截图对齐"""
        self.assertEqual(BOUNDARIES["relax"], (1, 39))
        self.assertEqual(BOUNDARIES["normal"], (40, 59))
        self.assertEqual(BOUNDARIES["medium"], (60, 79))
        self.assertEqual(BOUNDARIES["high"], (80, 100))


class StressBreakdownRealDataTest(unittest.TestCase):
    """实测 DB 数据验证（与 Zepp App 截图 0/49/41/10 对比）"""

    def test_2026_09_24_zepp_app_match(self):
        """2026-09-24 实测：服务端比例 0/48/42/10 ↔ Zepp App 0/49/41/10

        差距 ±1pp，符合用户「允许 ±1% 误差」要求
        """
        from datetime import datetime, timezone, timedelta
        # 从 DB 读 9-24 的全部 stress 曲线（实测 72 点）
        import sqlite3
        db = sqlite3.connect(str(os.path.expanduser("~/.zepp-data/zepp.db")))
        cur = db.execute(
            "SELECT value FROM measurements WHERE date='2026-09-24' AND metric='stress'"
        )
        values = [r[0] for r in cur.fetchall()]
        db.close()
        if not values:
            self.skipTest("DB 没数据（首次跑没 sync）")
        bd = compute_stress_breakdown(values)
        # 实测: 放松 0% / 正常 47.x% / 中等 43.x% / 偏高 9.x%
        self.assertAlmostEqual(bd.relax_pct, 0.0, delta=1.0)
        self.assertAlmostEqual(bd.normal_pct, 47.0, delta=2.0)
        self.assertAlmostEqual(bd.medium_pct, 43.0, delta=2.0)
        self.assertAlmostEqual(bd.high_pct, 10.0, delta=1.5)


if __name__ == "__main__":
    unittest.main()
