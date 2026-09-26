"""
normalize_hrv_rmssd 单元测试。

覆盖：
  - 合法 HRV (20ms) 在 1..400 范围内
  - HRV 0 哨兵 → 跳过
  - HRV 401 → 跳过
  - HRV 400 → 接受（闭区间上界）
  - HRV 1 → 接受（下界）
  - 偏移 0 → 接受
  - 偏移 7 天-1ms → 接受
  - 偏移 7 天+1ms → 跳过
  - 偏移 30 天 → 跳过（断电复同步）
  - 偏移 -1ms → 接受
  - 偏移负超大 → 跳过
  - 空 samples / 缺 startTime → diagnostics
  - 混合 batch（多个 sample）
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from normalizer.wellness import (
    normalize_hrv_rmssd, _MAX_HRV_OFFSET_MS,
)


def _base_ms():
    """2026-09-23T00:00:00Z → 1758000000000"""
    return 1_758_000_000_000


def _wrap(item):
    """构造 {items: [item]} 包络。"""
    return {"items": [item]}


def test_basic_valid_hrv():
    """合法 HRV 50ms + offset 0 → 1 条 record。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{"s": 0, "hrv": 50}],
        },
        "deviceId": "D85403FFFEE4D576",
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 1, f"应产出 1 条 record，得到 {len(b.records)}"
    r = b.records[0]
    assert r.metric == "hrv_rmssd", f"metric={r.metric}"
    assert r.value == 50.0, f"value={r.value}"
    assert r.unit == "ms"


def test_zero_hrv_is_sentinel():
    """HRV=0 是 Zepp 哨兵「没测到」，跳过。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{"s": 0, "hrv": 0}],
        }
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 0, "0 应被丢"
    assert any("HRV 数值无效" in d for d in b.diagnostics), \
        f"diagnostics 应记录: {b.diagnostics}"


def test_hrv_above_400_skipped():
    """HRV=401 超出闭区间上界 → 跳过。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{"s": 0, "hrv": 401}],
        }
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 0, "401 应被丢"
    assert any("HRV 数值无效" in d for d in b.diagnostics)


def test_hrv_at_400_accepted():
    """HRV=400 在闭区间上界上 → 接受。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{"s": 0, "hrv": 400}],
        }
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 1, f"400 应被接受，得到 {len(b.records)} 条"


def test_hrv_at_1_accepted():
    """HRV=1 在下界 → 接受。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{"s": 0, "hrv": 1}],
        }
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 1


def test_offset_zero_accepted():
    """offset = 0 → ts = base_dt。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{"s": 0, "hrv": 30}],
        }
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 1
    assert b.records[0].timestamp == datetime.fromtimestamp(_base_ms() / 1000, tz=timezone.utc)


def test_offset_just_under_7d_accepted():
    """offset = 7d - 1ms → 接受。"""
    offset = _MAX_HRV_OFFSET_MS - 1
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{"s": offset, "hrv": 30}],
        }
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 1


def test_offset_just_over_7d_skipped():
    """offset = 7d + 1ms → 跳过。"""
    offset = _MAX_HRV_OFFSET_MS + 1
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{"s": offset, "hrv": 30}],
        }
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 0, f"超过 7 天的偏移应被丢，得到 {len(b.records)} 条"
    assert any("偏移" in d and "超过 7 天" in d for d in b.diagnostics), \
        f"diagnostics 应说明偏移超限: {b.diagnostics}"


def test_offset_30d_skipped():
    """offset = 30 天 → 跳过（断电复同步伪值）。"""
    offset = 30 * 86400 * 1000
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{"s": offset, "hrv": 30}],
        }
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 0
    assert any("超过 7 天" in d for d in b.diagnostics)


def test_offset_negative_small_accepted():
    """offset = -1ms → 接受（轻微时钟回拨）。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{"s": -1, "hrv": 30}],
        }
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 1


def test_offset_negative_large_skipped():
    """offset = -30 天 → 跳过。"""
    offset = -(30 * 86400 * 1000)
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{"s": offset, "hrv": 30}],
        }
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 0
    assert any("超过 7 天" in d for d in b.diagnostics)


def test_missing_starttime_diagnostics():
    """缺 startTime → item 跳过 + diagnostics。"""
    raw = _wrap({
        "value": {"samples": [{"s": 0, "hrv": 30}]},
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 0
    assert any("startTime" in d for d in b.diagnostics)


def test_mixed_batch_some_valid_some_skipped():
    """混合 batch：3 valid + 2 skipped → 3 records。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [
                {"s": 0, "hrv": 50},                                   # ok
                {"s": 1_000, "hrv": 0},                                # sentinel
                {"s": 2_000, "hrv": 50},                               # ok
                {"s": _MAX_HRV_OFFSET_MS + 1, "hrv": 50},             # over
                {"s": 3_000, "hrv": 60},                               # ok
                {"s": 4_000, "hrv": 999},                              # over
            ],
        }
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 3, f"应产出 3 条 record，得到 {len(b.records)}"
    values = sorted(r.value for r in b.records)
    assert values == [50, 50, 60], f"values={values}"
    assert len(b.diagnostics) >= 3, f"应至少 3 个 diagnostics，得到 {len(b.diagnostics)}"


def test_empty_samples_diagnostics():
    """samples=[] → 跳过 + diagnostics。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [],
        }
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 0
    assert any("samples" in d for d in b.diagnostics)


def test_offset_field_name_fallback():
    """'offset' 字段名也支持（兼容不同 Zepp 设备协议）。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{"offset": 5_000, "hrv": 30}],
        }
    })
    b = normalize_hrv_rmssd(raw)
    assert len(b.records) == 1


def test_max_hrv_offset_constant_value():
    """_MAX_HRV_OFFSET_MS = 7 * 86400 * 1000 = 604_800_000。"""
    assert _MAX_HRV_OFFSET_MS == 7 * 86400 * 1000
    assert _MAX_HRV_OFFSET_MS == 604_800_000


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
            print(f"  ✗ {name}: {type(e).__name__} {e}")
            failed += 1
    print(f"\n=== hrv normalizer: {passed} passed, {failed} failed ===")
    import sys as _sys
    _sys.exit(0 if failed == 0 else 1)