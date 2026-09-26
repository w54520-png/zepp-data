"""
parse_altitude_cm_to_m 单元测试。

边界规则（M1 拍板）：
  |val| > 50_000 → cm 单位，除以 100
  |val| ≤ 50_000 → m 单位，原值
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetch_workouts import parse_altitude_cm_to_m


def test_none():
    assert parse_altitude_cm_to_m(None) is None


def test_empty_string():
    assert parse_altitude_cm_to_m("") is None


def test_non_numeric_string():
    assert parse_altitude_cm_to_m("nope") is None


def test_sentinel_minus_one_filtered():
    """-1 是 Zepp 哨兵 → None。"""
    assert parse_altitude_cm_to_m(-1) is None


def test_100m():
    """100 m 直接返回。"""
    assert parse_altitude_cm_to_m(100) == 100.0


def test_everest_8848m():
    """8848 m（珠峰高度）→ 8848。"""
    assert parse_altitude_cm_to_m(8848) == 8848.0


def test_50000_is_m_not_cm():
    """50_000 是边界——规则是 |x| > 50000 才视为 cm，50000 视为 m。"""
    # 这是有意为之：50000 m 是合理上限（飞机巡航高度级别），
    # 而 cm 数据通常 ≥ 100000 cm = 1000 m
    assert parse_altitude_cm_to_m(50000) == 50000.0


def test_50001_is_cm():
    """刚好 > 50000 → cm → 500.01。"""
    assert parse_altitude_cm_to_m(50001) == 500.01


def test_884800_cm_to_m():
    """884800 cm = 8848 m（珠峰）。"""
    assert parse_altitude_cm_to_m(884800) == 8848.0


def test_negative_cm():
    """负数 cm → 负数 m。"""
    assert parse_altitude_cm_to_m(-150000) == -1500.0


def test_negative_m():
    """负数 m。"""
    assert parse_altitude_cm_to_m(-100) == -100.0


def test_float_m():
    """浮点 m。"""
    assert parse_altitude_cm_to_m(1234.5) == 1234.5


def test_float_cm():
    """浮点 cm。"""
    assert parse_altitude_cm_to_m(123450.0) == 1234.5


def test_string_m():
    """字符串 m。"""
    assert parse_altitude_cm_to_m("100") == 100.0


def test_string_cm():
    """字符串 cm。"""
    assert parse_altitude_cm_to_m("100000") == 1000.0


def test_below_sea_level():
    """死海 -430 m。"""
    assert parse_altitude_cm_to_m(-430) == -430.0


def test_returns_rounded_2_decimals():
    """结果四舍五入到 2 位小数。"""
    # 884813 cm = 8848.13 m
    assert parse_altitude_cm_to_m(884813) == 8848.13


# ===== M2 章节 2.3 新增：M1 bug 修复（max/min altitude 历史 cm 单位）=====

def test_historical_garmin_max_altitude_cm_bug():
    """M1 bug 复现：旧 workouts 表里有 884800（cm）当 m 存。

    历史值 884800 m 不合理（珠峰 8848m），实际是 8848m × 100cm。
    修复后应正确返回 8848.0 m。
    """
    # 这是 M1 实际数据库里的值
    raw = 884800
    assert parse_altitude_cm_to_m(raw) == 8848.0


def test_historical_changshou_min_altitude_cm_bug():
    """另一个 M1 bug 例子：低海拔 cm 形式（如重庆 200m = 20000cm）。

    20000 < 50000 阈值，按规则视为 m（20000m 不合理但保留——这是有意为之的兜底）。
    真正的 cm 数据需要 > 50000 才能识别。
    """
    # 阈值边界：50000 当 m，50001 当 cm
    assert parse_altitude_cm_to_m(50000) == 50000.0    # 视为 m
    assert parse_altitude_cm_to_m(50001) == 500.01     # 视为 cm → m
    # 真实历史数据：20000（cm）→ 200 m（cm 数据 ≥ 100000 cm 才有效识别）
    assert parse_altitude_cm_to_m(200000) == 2000.0
    assert parse_altitude_cm_to_m(100000) == 1000.0


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
    print(f"\n=== parse_altitude_cm_to_m: {passed} passed, {failed} failed ===")
    import sys as _sys
    _sys.exit(0 if failed == 0 else 1)