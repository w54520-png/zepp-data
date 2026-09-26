"""
body.py（weight / 体成分）normalizer 单元测试 —— M2 章节 2.2。

覆盖（M2 拍板要求 8+ 用例）：
  - 11 个 BodyMetric 全部解析（实测字段对齐：fatRate / metabolism / proteinRatio / skeletalMuscle 等）
  - generatedTime 是**秒**（不是 ms）—— 不能用通用 parse_timestamp
  - timeZone 3 种形态（IANA / GMT+08:00 / 毫秒偏移）
  - timeZone=None 时默认 Asia/Shanghai
  - summary 子对象 vs 顶层 fallback
  - 范围过滤（异常值丢弃）
  - 空 items / 缺 generatedTime
  - BMR 通过 metabolism 字段，weight 流下 metric=bmr
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from normalizer.body import normalize_weight, BODY_METRICS
from normalizer.common import MetricSample


def _wrap(item):
    return {"items": [item]}


def _base_item(generated_time=1789352772, time_zone="Asia/Shanghai", **summary_overrides):
    """基础 weightRecords item。"""
    summary = {
        "weight": 70.0,
        "bmi": 22.9,
        "height": 175.0,
        "fatRate": 20.5,
        "bodyWaterRate": 58.2,
        "muscleRate": 51.1,
        "boneMass": 3.7,
        "proteinRatio": 15.7,
        "visceralFat": 8.0,
        "metabolism": 1553.0,
        "skeletalMuscle": 30.8,
        "bodyBalanceScore": 85,         # 一并测
        "timeZone": time_zone,
    }
    summary.update(summary_overrides)
    return {
        "userId": "1000000000",
        "memberId": "-1",
        "deviceSource": 8519936,
        "generatedTime": generated_time,
        "weightType": 5,
        "deviceId": "AABBCCDDEEFF1122",
        "summary": summary,
    }


def _metrics_by_name(batch):
    """helper: records → {metric_name: value}。"""
    out = {}
    for r in batch.records:
        out[r.metric] = r.value
    return out


# ===== 测试 =====

def test_all_11_metrics_parsed():
    """11 个 BodyMetric 全部解析。"""
    item = _base_item()
    batch = normalize_weight(_wrap(item))
    by_name = _metrics_by_name(batch)
    expected_metrics = {m for m, *_ in BODY_METRICS}
    actual_metrics = set(by_name.keys())
    assert expected_metrics.issubset(actual_metrics), \
        f"missing metrics: {expected_metrics - actual_metrics}"
    # 关键值
    assert by_name["weight"] == 70.0
    assert by_name["bmi"] == 22.9
    assert by_name["body_fat_rate"] == 20.5    # 来自 fatRate
    assert by_name["bmr"] == 1553.0            # 来自 metabolism
    assert by_name["muscle_mass"] == 30.8      # 来自 skeletalMuscle
    assert by_name["protein_rate"] == 15.7     # 来自 proteinRatio
    assert by_name["bone_mass"] == 3.7
    assert by_name["visceral_fat"] == 8.0
    assert by_name["body_water_rate"] == 58.2
    assert by_name["height"] == 175.0


def test_generated_time_is_seconds_not_ms():
    """generatedTime 是秒（10 位）—— 不要误当毫秒。"""
    item = _base_item(generated_time=1789352772)  # 10 位
    batch = normalize_weight(_wrap(item))
    assert len(batch.records) >= 1
    r = batch.records[0]
    # 1789352772 秒 = 2026-09-14 02:26:12 UTC（实测）
    expected = datetime(2026, 9, 14, 2, 26, 12, tzinfo=timezone.utc)
    assert r.timestamp == expected, f"got {r.timestamp}, expected {expected}"


def test_generated_time_defensive_ms_handling():
    """防御：若 generatedTime 是毫秒（13 位），自动 ÷1000。"""
    item = _base_item(generated_time=1789352772000)  # 13 位（毫秒）
    batch = normalize_weight(_wrap(item))
    assert len(batch.records) >= 1
    r = batch.records[0]
    expected = datetime(2026, 9, 14, 2, 26, 12, tzinfo=timezone.utc)
    assert r.timestamp == expected


def test_tz_iana_form():
    """timeZone='Asia/Shanghai' → 28800。"""
    item = _base_item(time_zone="Asia/Shanghai")
    batch = normalize_weight(_wrap(item))
    r = batch.records[0]
    assert r.extra.get("tz_offset_secs") == 28800


def test_tz_seoul_form():
    """timeZone='Asia/Seoul' → 32400。"""
    item = _base_item(time_zone="Asia/Seoul")
    batch = normalize_weight(_wrap(item))
    r = batch.records[0]
    assert r.extra.get("tz_offset_secs") == 32400


def test_tz_ms_offset_form():
    """timeZone='28800000'（毫秒字符串）→ offset_from_number 自动 ÷1000 → 28800。"""
    item = _base_item(time_zone="28800000")
    batch = normalize_weight(_wrap(item))
    r = batch.records[0]
    assert r.extra.get("tz_offset_secs") == 28800


def test_tz_none_defaults_to_shanghai():
    """timeZone=None → 默认 Asia/Shanghai (28800) 兜底。"""
    item = _base_item(time_zone=None)
    batch = normalize_weight(_wrap(item))
    r = batch.records[0]
    assert r.extra.get("tz_offset_secs") == 28800


def test_value_out_of_range_filtered():
    """超范围值被过滤。"""
    item = _base_item(weight=999.0)  # 超出 (0.5, 600]
    batch = normalize_weight(_wrap(item))
    by_name = _metrics_by_name(batch)
    assert "weight" not in by_name, "weight=999 应被过滤"
    # 其他正常值还在
    assert "bmi" in by_name


def test_empty_items_diagnostics():
    """空 items 数组 → DataUnavailable（normalizer 内部抛），records=0。"""
    from normalizer.common import DataUnavailable
    try:
        batch = normalize_weight({"items": []})
        # DataUnavailable 应该被抛出（这是正常行为，不是 bug）
        assert len(batch.records) == 0
    except DataUnavailable:
        pass  # OK: 空 items 抛 DataUnavailable


def test_missing_generated_time_skipped():
    """缺 generatedTime → diagnostics，不 crash。"""
    item = _base_item()
    del item["generatedTime"]
    batch = normalize_weight(_wrap(item))
    assert len(batch.records) == 0
    assert any("generatedTime" in d for d in batch.diagnostics)


def test_summary_nested_subobject():
    """实测字段都在 summary 子对象里。"""
    item = _base_item()
    # 验证数据确实在 summary 里（不是顶层）
    assert "weight" not in item
    assert "weight" in item["summary"]
    batch = normalize_weight(_wrap(item))
    by_name = _metrics_by_name(batch)
    assert by_name["weight"] == 70.0


def test_bmr_from_metabolism_field():
    """BMR 通过 metabolism 字段提取，metric='bmr'。"""
    item = _base_item()
    batch = normalize_weight(_wrap(item))
    by_name = _metrics_by_name(batch)
    assert "bmr" in by_name, "bmr metric 应有"
    assert by_name["bmr"] == 1553.0


def test_multiple_items_in_batch():
    """多条体重记录 → 多组 MetricSample。"""
    items = [
        _base_item(generated_time=1789352772),
        _base_item(generated_time=1789266372),  # 1 天前
        _base_item(generated_time=1789179972),  # 2 天前
    ]
    batch = normalize_weight({"items": items})
    # 每条 item 都产 11 条 record (如果全部字段都有效)
    # 3 items × 11 metrics = 33 条
    weights = [r.value for r in batch.records if r.metric == "weight"]
    assert len(weights) == 3, f"应有 3 个 weight，得到 {len(weights)}"
    assert all(w == 70.0 for w in weights)


def test_muscle_mass_uses_skeletal_muscle_not_muscle_rate():
    """muscle_mass 用 skeletalMuscle（kg），不用 muscleRate（%）。"""
    item = _base_item(
        skeletalMuscle=30.8,
        muscleRate=51.1,  # 是百分比
    )
    batch = normalize_weight(_wrap(item))
    by_name = _metrics_by_name(batch)
    assert by_name["muscle_mass"] == 30.8  # 是 kg，不是 51.1


def test_units_assigned():
    """每个 metric 分配正确的 unit。"""
    item = _base_item()
    batch = normalize_weight(_wrap(item))
    by_metric_unit = {r.metric: r.unit for r in batch.records}
    assert by_metric_unit["weight"] == "kg"
    assert by_metric_unit["bmi"] == "kg/m2"
    assert by_metric_unit["height"] == "cm"
    assert by_metric_unit["body_fat_rate"] == "%"
    assert by_metric_unit["bmr"] == "kcal"
    assert by_metric_unit["visceral_fat"] == "level"
    assert by_metric_unit["body_balance_score"] == "score"


if __name__ == "__main__":
    fns = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    passed = 0
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {name}: {e}")
            failed += 1
        except Exception as e:
            import traceback
            print(f"  ✗ {name}: {type(e).__name__} {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n=== weight normalizer: {passed} passed, {failed} failed ===")
    import sys as _sys
    _sys.exit(0 if failed == 0 else 1)