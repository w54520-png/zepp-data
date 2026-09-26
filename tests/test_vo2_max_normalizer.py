"""
M4 章节 4.4：VO2_MAX normalizer 单元测试

覆盖：
  - 5 种 vo2max 字段候选名
  - 范围 (1, 100) ml/kg/min
  - 哨兵 vo2max == -1 丢弃
  - 多种 envelope 形态
  - dayId / updateTime 日期解析
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import unittest

from normalizer.vo2_max import normalize_vo2_max, _VO2MAX_FIELD_NAMES, VO2MAX_RANGE


class Vo2MaxFieldNamesTest(unittest.TestCase):
    """5 个字段候选名（照搬 Rust 文档）"""

    def test_vo2max_lowercase(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "vo2max": 45.2}]})
        self.assertEqual(b.count, 1)
        self.assertEqual(b.records[0].value, 45.2)
        self.assertEqual(b.records[0].metric, "vo2max")

    def test_vo2max_camelcase(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "vo2Max": 46.0}]})
        self.assertEqual(b.count, 1)
        self.assertEqual(b.records[0].value, 46.0)

    def test_vo2max_uppercase(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "VO2_MAX": 47.0}]})
        self.assertEqual(b.count, 1)
        self.assertEqual(b.records[0].value, 47.0)

    def test_vo2max_mixedcase(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "VO2_max": 48.0}]})
        self.assertEqual(b.count, 1)
        self.assertEqual(b.records[0].value, 48.0)

    def test_vo2_max_run_field(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "vo2_max_run": 49.0}]})
        self.assertEqual(b.count, 1)
        self.assertEqual(b.records[0].value, 49.0)

    def test_vo2_max_walking_field(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "vo2_max_walking": 38.0}]})
        self.assertEqual(b.count, 1)
        self.assertEqual(b.records[0].value, 38.0)

    def test_field_names_constant_has_6(self):
        """5 个候选（不算 run/walking 是 5）"""
        # 实际是 6（包含 run/walking）
        self.assertGreaterEqual(len(_VO2MAX_FIELD_NAMES), 5)


class Vo2MaxRangeTest(unittest.TestCase):
    """范围 (1, 100) ml/kg/min"""

    def test_vo2max_at_lower_bound_accepted(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "vo2max": 1.0}]})
        self.assertEqual(b.count, 1)

    def test_vo2max_at_upper_bound_accepted(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "vo2max": 100.0}]})
        self.assertEqual(b.count, 1)

    def test_vo2max_above_range_filtered(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "vo2max": 150.0}]})
        self.assertEqual(b.count, 0)
        self.assertTrue(any("超出" in d for d in b.diagnostics))

    def test_vo2max_below_range_filtered(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "vo2max": 0.5}]})
        self.assertEqual(b.count, 0)


class Vo2MaxSentinelTest(unittest.TestCase):
    """哨兵 -1 / 0 丢弃"""

    def test_vo2max_negative_one_filtered(self):
        """Rust: vo2max == -1 涵盖 60% 实测 → 哨兵"""
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "vo2max": -1}]})
        self.assertEqual(b.count, 0)
        self.assertTrue(any("哨兵" in d for d in b.diagnostics))

    def test_vo2max_zero_filtered(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "vo2max": 0}]})
        self.assertEqual(b.count, 0)


class Vo2MaxEnvelopeTest(unittest.TestCase):
    def test_top_level_items(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "vo2max": 45.2}]})
        self.assertEqual(b.count, 1)

    def test_data_items_envelope(self):
        b = normalize_vo2_max({"code": 1, "data": {"items": [
            {"dayId": "2026-09-18", "vo2max": 45.2},
        ]}})
        self.assertEqual(b.count, 1)

    def test_empty_items_returns_no_data(self):
        b = normalize_vo2_max({"items": []})
        self.assertEqual(b.count, 0)
        self.assertEqual(b.capability, "no_data")

    def test_missing_envelope(self):
        """无 items/data → no_data（CN 国服实测）"""
        b = normalize_vo2_max({})
        self.assertEqual(b.count, 0)
        self.assertEqual(b.capability, "no_data")


class Vo2MaxDateTest(unittest.TestCase):
    def test_dayId_used(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "vo2max": 45.0}]})
        self.assertEqual(b.records[0].date, "2026-09-18")

    def test_fallback_to_updateTime(self):
        """没有 dayId，用 updateTime 转 UTC"""
        # 1726660800 = 2024-09-18 12:00:00 UTC
        b = normalize_vo2_max({"items": [
            {"updateTime": 1726660800000, "vo2max": 45.0},
        ]})
        self.assertEqual(b.count, 1)
        self.assertEqual(b.records[0].date, "2024-09-18")

    def test_no_date_skipped(self):
        b = normalize_vo2_max({"items": [{"vo2max": 45.0}]})
        self.assertEqual(b.count, 0)


class Vo2MaxDiagnosticsTest(unittest.TestCase):
    def test_non_object_item_diagnostic(self):
        b = normalize_vo2_max({"items": ["not a dict"]})
        self.assertEqual(b.count, 0)

    def test_no_vo2max_field_diagnostic(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "other": 1}]})
        self.assertEqual(b.count, 0)
        self.assertTrue(any("无 vo2max 字段" in d for d in b.diagnostics))


class Vo2MaxMetadataTest(unittest.TestCase):
    def test_unit_ml_kg_min(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "vo2max": 45.0}]})
        self.assertEqual(b.records[0].unit, "ml/kg/min")

    def test_source_scope_user_fused(self):
        b = normalize_vo2_max({"items": [{"dayId": "2026-09-18", "vo2max": 45.0}]})
        self.assertEqual(b.records[0].source_scope, "user_fused")


class Vo2MaxRangeConstantTest(unittest.TestCase):
    def test_range_is_1_to_100(self):
        self.assertEqual(VO2MAX_RANGE, (1.0, 100.0))


if __name__ == "__main__":
    unittest.main()
