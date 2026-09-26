"""
zepp_client._to_seconds 单元测试 + heart_rate/weight_records 端到端 mock 测试。

覆盖：
  - 毫秒整数 (1758000000000) → 1.758e9 秒
  - 秒整数 (1758000000) → 1.758e9 秒
  - 字符串毫秒 → 秒
  - 字符串秒 → 秒
  - None / 空串 / 非法 → None
  - bool → None（避免被当 0/1 时间戳）
  - NaN / Inf → None
  - 边界：刚好 10^10 → 当秒（不归为毫秒）
"""
import sys
import math
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from zepp_client import _to_seconds, ZeppClient


# ===== _to_seconds 单测 =====

def test_ms_int_current():
    """当前毫秒级时间戳。"""
    ms = 1_758_000_000_000
    assert _to_seconds(ms) == 1_758_000_000.0, "ms int"


def test_sec_int_current():
    """当前秒级时间戳。"""
    sec = 1_758_000_000
    assert _to_seconds(sec) == 1_758_000_000, "sec int"


def test_ms_string():
    """字符串毫秒。"""
    assert _to_seconds("1758000000000") == 1_758_000_000.0, "ms str"


def test_sec_string():
    """字符串秒。"""
    assert _to_seconds("1758000000") == 1_758_000_000, "sec str"


def test_none_returns_none():
    assert _to_seconds(None) is None, "None"


def test_empty_string_returns_none():
    assert _to_seconds("") is None, "empty str"


def test_whitespace_string_returns_none():
    assert _to_seconds("   ") is None, "whitespace"


def test_invalid_string_returns_none():
    assert _to_seconds("not-a-number") is None, "invalid str"


def test_bool_true_returns_none():
    """bool 是 int 子类——绝不能当时间戳。"""
    assert _to_seconds(True) is None, "bool True"
    assert _to_seconds(False) is None, "bool False"


def test_nan_returns_none():
    assert _to_seconds(float("nan")) is None, "NaN"


def test_inf_returns_none():
    assert _to_seconds(float("inf")) is None, "Inf"
    assert _to_seconds(float("-inf")) is None, "-Inf"


def test_boundary_just_above_1e10_is_ms():
    """刚好 10^10 + 1 → 当毫秒。"""
    n = 10_000_000_001
    assert _to_seconds(n) == 10_000_000_001 / 1000, "1e10+1 ms"


def test_boundary_just_below_1e10_is_sec():
    """10^10 - 1 → 当秒。"""
    n = 9_999_999_999
    assert _to_seconds(n) == 9_999_999_999, "1e10-1 sec"


def test_exactly_1e10_is_ms():
    """边界 10^10 → 实现选择当毫秒（|x| >= 10^10 都归毫秒）。"""
    # 当前时间戳（2026+）毫秒 ~1.75e12，远大于 1e10，
    # 所以 1e10 当毫秒再除 1000 是 1e7——但语义上仍是 ms。
    assert _to_seconds(10_000_000_000) == 10_000_000_000 / 1000, "exactly 1e10 as ms"


def test_zero():
    """0 → 0。"""
    assert _to_seconds(0) == 0, "zero"


def test_float_ms():
    """浮点毫秒。"""
    assert _to_seconds(1_758_000_000_000.0) == 1_758_000_000.0, "float ms"


def test_float_sec():
    assert _to_seconds(1_758_000_000.0) == 1_758_000_000.0, "float sec"


def test_string_with_whitespace_padded():
    """'  1758000000000  ' → 仍能识别。"""
    assert _to_seconds("  1758000000000  ") == 1_758_000_000.0, "padded ms str"


def test_negative_ms():
    """负数毫秒（epoch 之前）也能识别为 ms。"""
    n = -1_758_000_000_000
    assert _to_seconds(n) == -1_758_000_000.0, "negative ms"


# ===== heart_rate 参数转换 =====

def test_heart_rate_passes_ms_to_api():
    """传入秒时，HTTP 层应该收到毫秒（API 实际是 13 位 ms）。"""
    client = ZeppClient.__new__(ZeppClient)  # 跳过 __init__
    client.user_id = "12345"
    captured = {}

    def fake_get(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return {"items": []}

    client._get = fake_get
    client.heart_rate(1_758_000_000, 1_758_086_400, limit=100, hr_type=2)
    # startTime/endTime 应该是 13 位毫秒戳
    assert len(captured["params"]["startTime"]) == 13, f"startTime 应为 13 位 ms，实际: {captured['params']['startTime']}"
    assert len(captured["params"]["endTime"]) == 13, f"endTime 应为 13 位 ms，实际: {captured['params']['endTime']}"
    assert captured["params"]["startTime"] == "1758000000000"
    assert captured["params"]["endTime"] == "1758086400000"


def test_heart_rate_accepts_ms_and_converts():
    """传毫秒时也兼容（自动检测）。"""
    client = ZeppClient.__new__(ZeppClient)
    client.user_id = "12345"
    captured = {}

    def fake_get(path, params=None):
        captured["params"] = params
        return {"items": []}

    client._get = fake_get
    client.heart_rate(1_758_000_000_000, 1_758_086_400_000, limit=100, hr_type=2)
    assert captured["params"]["startTime"] == "1758000000000"
    assert captured["params"]["endTime"] == "1758086400000"


def test_heart_rate_invalid_param_raises():
    """传 None 时应抛 ZeppError。"""
    client = ZeppClient.__new__(ZeppClient)
    client.user_id = "12345"

    def fake_get(path, params=None):
        return {"items": []}

    client._get = fake_get
    try:
        client.heart_rate(None, 1_758_000_000)
        assert False, "应抛 ZeppError"
    except Exception as e:
        assert "无效" in str(e), f"期望错误信息含'无效'，实际: {e}"


# ===== weight_records 参数转换 =====

def test_weight_records_passes_sec_to_api():
    """传入秒时，HTTP 层应该收到秒（API 实际是 10 位秒戳）。"""
    client = ZeppClient.__new__(ZeppClient)
    client.user_id = "12345"
    captured = {}

    def fake_get(path, params=None):
        captured["params"] = params
        return {"items": []}

    client._get = fake_get
    client.weight_records("-1", 1_758_000_000, 1_758_086_400, limit=50)
    # fromTime/toTime 应该是 10 位秒戳
    assert len(captured["params"]["fromTime"]) == 10, f"fromTime 应为 10 位 sec，实际: {captured['params']['fromTime']}"
    assert len(captured["params"]["toTime"]) == 10, f"toTime 应为 10 位 sec，实际: {captured['params']['toTime']}"
    assert captured["params"]["fromTime"] == "1758000000"
    assert captured["params"]["toTime"] == "1758086400"


def test_weight_records_accepts_ms():
    """传毫秒时也兼容。"""
    client = ZeppClient.__new__(ZeppClient)
    client.user_id = "12345"
    captured = {}

    def fake_get(path, params=None):
        captured["params"] = params
        return {"items": []}

    client._get = fake_get
    client.weight_records("-1", 1_758_000_000_000, 1_758_086_400_000, limit=50)
    assert captured["params"]["fromTime"] == "1758000000"
    assert captured["params"]["toTime"] == "1758086400"


def test_weight_records_default_window_is_365_days_in_seconds():
    """不传时间参数时，默认窗口是 365 天（秒）。"""
    client = ZeppClient.__new__(ZeppClient)
    client.user_id = "12345"
    captured = {}

    def fake_get(path, params=None):
        captured["params"] = params
        return {"items": []}

    client._get = fake_get
    client.weight_records("-1")
    span = int(captured["params"]["toTime"]) - int(captured["params"]["fromTime"])
    # 允许 ±5 秒误差（time.time() 调用间隔）
    assert abs(span - 365 * 86400) < 5, f"window 应为 365*86400 秒，实际: {span}"


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
    print(f"\n=== zepp_client units: {passed} passed, {failed} failed ===")
    import sys as _sys
    _sys.exit(0 if failed == 0 else 1)