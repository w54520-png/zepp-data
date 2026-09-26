"""
M4 章节 4.2：Food 流 normalizer 单元测试

覆盖：
  - 4 个 macro：calories / protein / fat / carbs
  - 范围校验（边界外丢弃）
  - 顶层 vs value 嵌套
  - 按日累加（一天多条）
  - 5 种 envelope 形态
  - 空 items / 无日期
  - 未知字段诊断
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import unittest

from normalizer.food import normalize_food, _FOOD_MACROS


def _by_metric(batch):
    """按 (date, metric) 索引 → dict[date][metric] = DailyMetric"""
    out = {}
    for r in batch.records:
        out.setdefault(r.date, {})[r.metric] = r
    return out


class FoodEnvelopeTest(unittest.TestCase):
    def test_top_level_items(self):
        raw = {"items": [{"date": "2026-09-18", "calories": 500}]}
        b = normalize_food(raw)
        self.assertEqual(b.count, 1)
        self.assertEqual(b.records[0].metric, "intake_calories")
        self.assertEqual(b.records[0].value, 500.0)
        self.assertEqual(b.records[0].date, "2026-09-18")

    def test_data_items_envelope(self):
        raw = {"code": 1, "data": {"items": [{"date": "2026-09-19", "protein": 50}]}}
        b = normalize_food(raw)
        self.assertEqual(b.count, 1)
        self.assertEqual(b.records[0].metric, "intake_protein_g")
        self.assertEqual(b.records[0].value, 50.0)

    def test_data_list_envelope(self):
        raw = {"data": [{"date": "2026-09-19", "fat": 30}]}
        b = normalize_food(raw)
        self.assertEqual(b.count, 1)
        self.assertEqual(b.records[0].metric, "intake_fat_g")

    def test_top_level_list(self):
        raw = [{"date": "2026-09-19", "carbs": 80}]
        b = normalize_food(raw)
        self.assertEqual(b.count, 1)
        self.assertEqual(b.records[0].metric, "intake_carbs_g")
        self.assertEqual(b.records[0].value, 80.0)

    def test_empty_envelope_returns_no_data(self):
        b = normalize_food({"items": []})
        self.assertEqual(b.count, 0)
        self.assertEqual(b.capability, "no_data")
        self.assertEqual(b.diagnostics, [])

    def test_missing_envelope_returns_empty(self):
        """没有 items/data → 空（CN 国服实测返回空 items）"""
        b = normalize_food({})
        self.assertEqual(b.count, 0)
        self.assertEqual(b.capability, "no_data")


class FoodFieldExtractionTest(unittest.TestCase):
    def test_top_level_calories(self):
        b = normalize_food({"items": [{"date": "2026-09-18", "calories": 700}]})
        self.assertEqual(b.records[0].metric, "intake_calories")
        self.assertEqual(b.records[0].value, 700.0)

    def test_nested_value_calories(self):
        b = normalize_food({"items": [{"date": "2026-09-18", "value": {"calories": 800}}]})
        self.assertEqual(b.records[0].metric, "intake_calories")
        self.assertEqual(b.records[0].value, 800.0)

    def test_top_level_protein(self):
        b = normalize_food({"items": [{"date": "2026-09-18", "protein": 30}]})
        self.assertEqual(b.records[0].metric, "intake_protein_g")
        self.assertEqual(b.records[0].value, 30.0)

    def test_protein_alternate_name_proteins(self):
        """'proteins' 也算 protein（Rust MACROS 双名兼容）"""
        b = normalize_food({"items": [{"date": "2026-09-18", "proteins": 30}]})
        self.assertEqual(b.records[0].metric, "intake_protein_g")

    def test_carbs_alternate_name_carbohydrate(self):
        """'carbohydrate' 也算 carbs"""
        b = normalize_food({"items": [{"date": "2026-09-18", "carbohydrate": 100}]})
        self.assertEqual(b.records[0].metric, "intake_carbs_g")

    def test_fat_alternate_name_fats(self):
        """'fats' 也算 fat"""
        b = normalize_food({"items": [{"date": "2026-09-18", "fats": 20}]})
        self.assertEqual(b.records[0].metric, "intake_fat_g")

    def test_calories_string_value_parsed(self):
        """'300' 字符串 → 300.0"""
        b = normalize_food({"items": [{"date": "2026-09-18", "calories": "300"}]})
        self.assertEqual(b.records[0].value, 300.0)


class FoodRangeValidationTest(unittest.TestCase):
    def test_calories_below_minimum_filtered(self):
        """calories = 0.5 < 1 → 丢"""
        b = normalize_food({"items": [{"date": "2026-09-18", "calories": 0.5}]})
        self.assertEqual(b.count, 0)
        self.assertTrue(any("超出" in d for d in b.diagnostics))

    def test_calories_above_maximum_filtered(self):
        """calories = 30000 > 20000 → 丢"""
        b = normalize_food({"items": [{"date": "2026-09-18", "calories": 30000}]})
        self.assertEqual(b.count, 0)

    def test_calories_at_minimum_accepted(self):
        b = normalize_food({"items": [{"date": "2026-09-18", "calories": 1}]})
        self.assertEqual(b.count, 1)

    def test_calories_at_maximum_accepted(self):
        b = normalize_food({"items": [{"date": "2026-09-18", "calories": 20000}]})
        self.assertEqual(b.count, 1)

    def test_protein_zero_accepted(self):
        """protein = 0 边界合法（低脂餐）"""
        b = normalize_food({"items": [{"date": "2026-09-18", "protein": 0}]})
        self.assertEqual(b.count, 1)
        self.assertEqual(b.records[0].value, 0.0)

    def test_protein_at_1000_accepted(self):
        b = normalize_food({"items": [{"date": "2026-09-18", "protein": 1000}]})
        self.assertEqual(b.count, 1)

    def test_protein_at_1001_filtered(self):
        b = normalize_food({"items": [{"date": "2026-09-18", "protein": 1001}]})
        self.assertEqual(b.count, 0)


class FoodDailyAccumulationTest(unittest.TestCase):
    def test_three_meals_same_day_sum(self):
        """一天 3 餐：cal 累加"""
        raw = {"items": [
            {"date": "2026-09-18", "calories": 500, "protein": 20, "fat": 15, "carbs": 60},
            {"date": "2026-09-18", "calories": 700, "protein": 30, "fat": 20, "carbs": 80},
            {"date": "2026-09-18", "calories": 300, "protein": 10, "fat": 5,  "carbs": 40},
        ]}
        b = normalize_food(raw)
        m = _by_metric(b)["2026-09-18"]
        self.assertEqual(m["intake_calories"].value, 1500.0)
        self.assertEqual(m["intake_protein_g"].value, 60.0)
        self.assertEqual(m["intake_fat_g"].value, 40.0)
        self.assertEqual(m["intake_carbs_g"].value, 180.0)

    def test_multi_day_separates(self):
        """多天数据按日分开，不累加跨日"""
        raw = {"items": [
            {"date": "2026-09-18", "calories": 500},
            {"date": "2026-09-19", "calories": 800},
        ]}
        b = normalize_food(raw)
        m = _by_metric(b)
        self.assertEqual(m["2026-09-18"]["intake_calories"].value, 500.0)
        self.assertEqual(m["2026-09-19"]["intake_calories"].value, 800.0)

    def test_mixed_top_level_and_nested_same_day(self):
        """同一 metric 一部分在顶层、一部分在 value → 累加"""
        raw = {"items": [
            {"date": "2026-09-18", "calories": 500},
            {"date": "2026-09-18", "value": {"calories": 300}},
        ]}
        b = normalize_food(raw)
        self.assertEqual(b.records[0].value, 800.0)


class FoodDiagnosticsTest(unittest.TestCase):
    def test_no_date_diagnostic(self):
        b = normalize_food({"items": [{"calories": 500}]})  # 无日期
        self.assertEqual(b.count, 0)
        self.assertTrue(any("没有可用日期" in d for d in b.diagnostics))

    def test_unknown_field_diagnostic(self):
        b = normalize_food({"items": [
            {"date": "2026-09-18", "calories": 500, "vitamin_c": 100},
        ]})
        self.assertEqual(b.count, 1)
        self.assertTrue(any("未知字段" in d and "vitamin_c" in d for d in b.diagnostics))

    def test_non_object_item_diagnostic(self):
        b = normalize_food({"items": ["not a dict", 42]})
        self.assertEqual(b.count, 0)


class FoodUnitsAndMetadataTest(unittest.TestCase):
    def test_units_assigned(self):
        b = normalize_food({"items": [
            {"date": "2026-09-18", "calories": 500, "protein": 20, "fat": 15, "carbs": 60},
        ]})
        m = _by_metric(b)["2026-09-18"]
        self.assertEqual(m["intake_calories"].unit, "kcal")
        self.assertEqual(m["intake_protein_g"].unit, "g")
        self.assertEqual(m["intake_fat_g"].unit, "g")
        self.assertEqual(m["intake_carbs_g"].unit, "g")

    def test_source_scope_user_fused(self):
        """饮食是手动记录 → user_fused"""
        b = normalize_food({"items": [{"date": "2026-09-18", "calories": 500}]})
        self.assertEqual(b.records[0].source_scope, "user_fused")

    def test_value_rounded_to_2_decimal(self):
        """0.1 + 0.2 → 0.3（不是 0.30000000000004）"""
        b = normalize_food({"items": [
            {"date": "2026-09-18", "fat": 0.1},
            {"date": "2026-09-18", "fat": 0.2},
        ]})
        self.assertEqual(b.records[0].value, 0.3)


class FoodMacrosConstantTest(unittest.TestCase):
    def test_macro_definitions_complete(self):
        """4 个 macro 都有 metric/unit/range"""
        self.assertEqual(len(_FOOD_MACROS), 4)
        metrics = {m["metric"] for m in _FOOD_MACROS}
        self.assertEqual(metrics, {
            "intake_calories", "intake_protein_g",
            "intake_fat_g", "intake_carbs_g",
        })
        for m in _FOOD_MACROS:
            self.assertIn("unit", m)
            self.assertIn("range", m)
            self.assertIn("names", m)


if __name__ == "__main__":
    unittest.main()
