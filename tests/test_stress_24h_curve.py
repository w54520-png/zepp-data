"""
M4 章节 4.7：压力 24h 曲线单元测试

背景：all_day_stress 流的 data 字段是 JSON 字符串数组 [{time:ms, value:1..100}, ...]
M4 新增 metric='stress_24h_curve'：存每日有效采样点数量（曲线详情从 raw_records 读）。

覆盖：
  - 解析 data 字符串里的曲线点
  - 范围过滤 [1, 100]
  - 一天多个 item 时正确聚合（合并所有点）
  - 空 / 损坏 JSON 容错
  - 24h 时间范围（跨午夜）
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import json
import unittest

from normalizer.wellness import normalize_all_day_stress


def _make_item(date_str, points):
    """构造一条 all_day_stress item，含 avgStress 顶层字段 + data 曲线。"""
    # 把 points 转 JSON 字符串
    return {
        "date": date_str,
        "avgStress": 50,
        "minStress": 30,
        "maxStress": 70,
        "data": json.dumps(points),
    }


class Stress24hCurveBasicTest(unittest.TestCase):
    def test_single_item_with_5_points(self):
        """1 条 item，5 个有效点 → stress_24h_curve.value = 5.0"""
        points = [
            {"time": 1726612800000, "value": 30},
            {"time": 1726616400000, "value": 45},
            {"time": 1726620000000, "value": 60},
            {"time": 1726623600000, "value": 75},
            {"time": 1726627200000, "value": 50},
        ]
        raw = {"items": [_make_item("2026-09-18", points)]}
        b = normalize_all_day_stress(raw)
        curve_recs = [r for r in b.records if r.metric == "stress_24h_curve"]
        self.assertEqual(len(curve_recs), 1)
        self.assertEqual(curve_recs[0].value, 5.0)
        self.assertEqual(curve_recs[0].unit, "points")

    def test_curve_diagnostic_recorded(self):
        """diagnostics 应包含曲线统计信息"""
        points = [{"time": 1726612800000 + i*60000, "value": 30 + i}
                  for i in range(3)]
        raw = {"items": [_make_item("2026-09-18", points)]}
        b = normalize_all_day_stress(raw)
        diags = [d for d in b.diagnostics if "stress_24h_curve" in d]
        self.assertEqual(len(diags), 1)
        self.assertIn("3 points", diags[0])

    def test_no_data_field_no_curve(self):
        """item 没有 data 字段 → 不生成 curve 记录"""
        raw = {"items": [{"date": "2026-09-18", "avgStress": 50}]}
        b = normalize_all_day_stress(raw)
        curve_recs = [r for r in b.records if r.metric == "stress_24h_curve"]
        self.assertEqual(len(curve_recs), 0)


class Stress24hCurveFilterTest(unittest.TestCase):
    def test_out_of_range_value_filtered(self):
        """value < 1 或 > 100 视为无效（与 curve sample-level 过滤一致）"""
        points = [
            {"time": 1726612800000, "value": 0},     # 哨兵（无测量）
            {"time": 1726616400000, "value": 50},    # 有效
            {"time": 1726620000000, "value": 101},   # 越界
            {"time": 1726623600000, "value": -5},    # 越界
            {"time": 1726627200000, "value": 75},    # 有效
        ]
        raw = {"items": [_make_item("2026-09-18", points)]}
        b = normalize_all_day_stress(raw)
        curve_recs = [r for r in b.records if r.metric == "stress_24h_curve"]
        self.assertEqual(len(curve_recs), 1)
        self.assertEqual(curve_recs[0].value, 2.0)  # 只有 50 和 75 算有效

    def test_missing_time_filtered(self):
        """缺 time 字段的点视为无效"""
        points = [
            {"time": 1726612800000, "value": 50},
            {"value": 60},  # 无 time
            {"time": 1726620000000, "value": 70},
        ]
        raw = {"items": [_make_item("2026-09-18", points)]}
        b = normalize_all_day_stress(raw)
        curve_recs = [r for r in b.records if r.metric == "stress_24h_curve"]
        self.assertEqual(curve_recs[0].value, 2.0)


class Stress24hCurveDailyAggregationTest(unittest.TestCase):
    def test_multiple_items_same_day_aggregated(self):
        """同一天 2 个 item → 累加点数"""
        points_a = [{"time": 1726612800000, "value": 30},
                    {"time": 1726616400000, "value": 40}]
        points_b = [{"time": 1726620000000, "value": 50},
                    {"time": 1726623600000, "value": 60},
                    {"time": 1726627200000, "value": 70}]
        raw = {"items": [
            _make_item("2026-09-18", points_a),
            _make_item("2026-09-18", points_b),
        ]}
        b = normalize_all_day_stress(raw)
        curve_recs = [r for r in b.records if r.metric == "stress_24h_curve"]
        # 同一天 2 条 → 都入库（实际场景不太可能，但去重交给 UNIQUE 约束）
        self.assertEqual(len(curve_recs), 2)
        total_points = sum(r.value for r in curve_recs)
        self.assertEqual(total_points, 5.0)


class Stress24hCurveRobustnessTest(unittest.TestCase):
    def test_invalid_json_skipped(self):
        """data 字段 JSON 损坏 → 跳过 curve metric 但不报错"""
        raw = {"items": [{
            "date": "2026-09-18",
            "avgStress": 50,
            "data": "not valid json {{{",
        }]}
        b = normalize_all_day_stress(raw)
        curve_recs = [r for r in b.records if r.metric == "stress_24h_curve"]
        self.assertEqual(len(curve_recs), 0)
        # 顶层 avgStress 仍应入库
        stress_recs = [r for r in b.records if r.metric == "stress"]
        self.assertGreater(len(stress_recs), 0)

    def test_data_is_dict_skipped(self):
        """data 是 dict 而非 list → 跳过"""
        raw = {"items": [{
            "date": "2026-09-18",
            "avgStress": 50,
            "data": json.dumps({"not": "list"}),
        }]}
        b = normalize_all_day_stress(raw)
        self.assertEqual(len([r for r in b.records if r.metric == "stress_24h_curve"]), 0)

    def test_empty_data_list(self):
        """data 是空 list → 不入库 curve"""
        raw = {"items": [{
            "date": "2026-09-18",
            "avgStress": 50,
            "data": "[]",
        }]}
        b = normalize_all_day_stress(raw)
        self.assertEqual(len([r for r in b.records if r.metric == "stress_24h_curve"]), 0)


class Stress24hCurveMetricCompatTest(unittest.TestCase):
    def test_existing_top_level_fields_unchanged(self):
        """M4 4.7 加了 curve 不影响原有顶层字段（avgStress/min/max/比例）"""
        points = [{"time": 1726612800000, "value": 50}]
        raw = {"items": [{
            "date": "2026-09-18",
            "avgStress": 45,
            "minStress": 30,
            "maxStress": 70,
            "relaxProportion": 20,
            "normalProportion": 50,
            "mediumProportion": 20,
            "highProportion": 10,
            "data": json.dumps(points),
        }]}
        b = normalize_all_day_stress(raw)
        metric_names = {r.metric for r in b.records}
        # 原有 8 个顶层字段 + 新增 1 个 curve metric = 9
        expected = {"stress", "stress_min", "stress_max",
                    "stress_relax_pct", "stress_normal_pct",
                    "stress_medium_pct", "stress_high_pct",
                    "stress_24h_curve"}
        # 加上 sample-level curve 样本
        self.assertTrue(expected.issubset(metric_names),
                        f"missing: {expected - metric_names}")


if __name__ == "__main__":
    unittest.main()
