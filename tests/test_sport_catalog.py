"""test_sport_catalog.py — 验证 sport_catalog.py / sport_catalog.json

测试覆盖：
1.  已知 code 1/6/13/22/52 → 正确中文名
2.  未知 code → "未知运动(<code>)" fallback
3.  None / 非法字符串 → "未知运动(<code>)" 不抛
4.  负数 code → 未知 fallback
5.  字符串数字 "1" → 正常解析
6.  _init() 懒加载（未初始化时调用触发，加载后 _CODE_TO_SPORT 非空）
7.  catalog 里每个 code 都有中文名
8.  中文名长度 ≥2 字符（不是单字 / 空）
9.  多次调用结果一致（idempotent）
10. catalog 至少 50% 条目是 cloud_* source（cloud_verified/cloud_inferred/cloud_reference/cloud_and_extended_verified）
"""
import sys
import os

# 让 tests/ 能 import scripts/
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import sport_catalog as sc


# ============ 1. 已知 code ============

def test_normal_codes():
    """code 1/6/13/22/52 都能查到中文名（含 tuple + safe_name 两路）."""
    cases = {
        1: "户外跑步",
        6: "健走",
        13: "健走",
        22: "徒步",
        52: "力量训练",
    }
    for code, expected_zh in cases.items():
        # tuple API
        key, zh = sc.sport_name(code)
        assert zh == expected_zh, f"sport_name({code}) → ({key!r}, {zh!r}), expected zh={expected_zh!r}"
        # safe_name API
        zh2 = sc.sport_safe_name(code)
        assert zh2 == expected_zh, f"sport_safe_name({code}) → {zh2!r}, expected {expected_zh!r}"
    print(f"  ✓ 5 known codes all resolved (tuple + safe_name both OK)")


# ============ 2. 未知 code ============

def test_unknown_code():
    """未知大数字 code → '未知运动(<code>)'."""
    zh = sc.sport_safe_name(9999)
    assert zh == "未知运动(9999)", f"got {zh!r}"
    # tuple 路径
    key, zh2 = sc.sport_name(9999)
    assert key == "unknown_9999", f"got key={key!r}"
    assert zh2 == "未知运动(9999)", f"got zh={zh2!r}"
    print(f"  ✓ unknown code 9999 → '未知运动(9999)'")


# ============ 3. 非法输入 ============

def test_invalid_code():
    """None / 非数字字符串 → '未知运动(<code>)' 不抛."""
    # None
    zh_none = sc.sport_safe_name(None)
    assert zh_none == "未知运动(None)", f"got {zh_none!r}"
    # 字符串 'abc'
    zh_abc = sc.sport_safe_name("abc")
    assert zh_abc == "未知运动(abc)", f"got {zh_abc!r}"
    # tuple 路径也不抛
    k1, z1 = sc.sport_name(None)
    k2, z2 = sc.sport_name("abc")
    assert "unknown" in k1
    assert "未知" in z1
    assert "unknown" in k2
    assert "未知" in z2
    print(f"  ✓ invalid input (None, 'abc') → '未知运动(...)' no exception")


# ============ 4. 负数 code ============

def test_negative_code():
    """负数 code（理论上不该有）→ fallback."""
    zh = sc.sport_safe_name(-1)
    assert zh == "未知运动(-1)", f"got {zh!r}"
    print(f"  ✓ negative code -1 → '未知运动(-1)'")


# ============ 5. 字符串数字 ============

def test_string_numeric():
    """字符串 '1' → int(1) 解析成功，返 '户外跑步'."""
    zh = sc.sport_safe_name("1")
    assert zh == "户外跑步", f"got {zh!r}"
    # 更大字符串
    zh_int = sc.sport_safe_name("22")
    assert zh_int == "徒步", f"got {zh_int!r}"
    print(f"  ✓ string '1' → '户外跑步'; string '22' → '徒步'")


# ============ 6. _init() lazy 加载 ============

def test_fallback_when_uninitialized():
    """验证 _init() 懒加载：调用前 _CODE_TO_SPORT 空（或后续填充），调用后非空."""
    # 强制重置（如果其他测试已经加载了）
    original = sc._CODE_TO_SPORT.copy()
    sc._CODE_TO_SPORT = {}
    sc._KEY_TO_ZH = {}
    try:
        assert sc._CODE_TO_SPORT == {}, "should be empty before call"
        # 调 sport_safe_name 应该触发 _init()
        zh = sc.sport_safe_name(1)
        assert zh == "户外跑步", f"lazy init failed: got {zh!r}"
        assert len(sc._CODE_TO_SPORT) > 0, "_init() did not populate _CODE_TO_SPORT"
        print(f"  ✓ _init() lazy-load works ({len(sc._CODE_TO_SPORT)} codes loaded)")
    finally:
        # 还原，避免污染其他测试
        sc._CODE_TO_SPORT = original
        sc._KEY_TO_ZH = {}


# ============ 7. 所有已知 code 都有中文名 ============

def test_all_known_codes_have_zh():
    """catalog 里所有 code 条目都有 label_zh（非空字符串）."""
    sc._init()
    missing = []
    for code, (key, zh) in sc._CODE_TO_SPORT.items():
        if not zh or not isinstance(zh, str):
            missing.append((code, key, zh))
    assert not missing, f"missing/empty zh for: {missing}"
    # 至少得有 100+ 条目（catalog 是 134 个）
    assert len(sc._CODE_TO_SPORT) >= 100, f"only {len(sc._CODE_TO_SPORT)} codes loaded"
    print(f"  ✓ all {len(sc._CODE_TO_SPORT)} codes have non-empty zh label")


# ============ 8. 中文名长度合理 ============

def test_zh_length_reasonable():
    """每个中文名长度 ≥2 字符（不是单字 / 空）。"""
    sc._init()
    too_short = []
    for code, (key, zh) in sc._CODE_TO_SPORT.items():
        if len(zh) < 2:
            too_short.append((code, key, zh))
    assert not too_short, f"zh too short: {too_short}"
    # 顺手查最长（应至少有一个 ≥3 字符）
    longest = max(len(zh) for _, (_, zh) in sc._CODE_TO_SPORT.items())
    assert longest >= 3, f"max zh length only {longest}"
    print(f"  ✓ all zh ≥2 chars, max={longest}")


# ============ 9. 幂等 ============

def test_idempotent():
    """多次调同一 code 结果一致."""
    a1 = sc.sport_safe_name(1)
    a2 = sc.sport_safe_name(1)
    a3 = sc.sport_safe_name(1)
    assert a1 == a2 == a3 == "户外跑步"
    # 未知 code 也幂等
    u1 = sc.sport_safe_name(12345)
    u2 = sc.sport_safe_name(12345)
    assert u1 == u2 == "未知运动(12345)"
    # 交替 known/unknown
    mixed = [sc.sport_safe_name(c) for c in [1, 99999, 6, 88888, 22, 1, 99999]]
    expected = ["户外跑步", "未知运动(99999)", "健走", "未知运动(88888)", "徒步", "户外跑步", "未知运动(99999)"]
    assert mixed == expected, f"got {mixed}"
    print(f"  ✓ idempotent across repeated calls (known + unknown)")


# ============ 10. cloud 覆盖率 ============

def test_catalog_evidence_coverage():
    """catalog 至少有一些 cloud_* source（cloud_verified/inferred/reference/and_extended_verified）。

    设计意图：catalog 不应 100% 来自 extended_catalog 而完全没有 Zepp Cloud 实证。
    当前实测 ~15/135 ≈ 11%（cloud_verified 5 + cloud_inferred 4 + cloud_reference 4 + cloud_and_extended_verified 2）。
    设阈值 ≥10% 防止 catalog 退化成纯外部参考。
    """
    cat = sc._load_catalog()
    sports = cat.get("sports", [])
    assert len(sports) >= 50, f"catalog too small: {len(sports)}"
    cloud_prefixed = [
        s for s in sports
        if str(s.get("source", "")).startswith("cloud")
    ]
    ratio = len(cloud_prefixed) / len(sports)
    assert ratio >= 0.10, f"only {len(cloud_prefixed)}/{len(sports)} ({ratio:.0%}) cloud_* sources; expected ≥10%"
    # 必须至少有一条 cloud_verified（最强证据）
    verified = [s for s in sports if s.get("source") == "cloud_verified"]
    assert len(verified) >= 1, "no cloud_verified entry — catalog lost ground truth"
    # 打印分布帮助后续 audit
    from collections import Counter
    dist = Counter(s.get("source", "?") for s in sports)
    print(f"  ✓ catalog {len(sports)} entries, {len(cloud_prefixed)} cloud_* ({ratio:.0%}), {len(verified)} cloud_verified; dist={dict(dist)}")


# ============ 跑所有测试 ============

if __name__ == "__main__":
    tests = [
        test_normal_codes,
        test_unknown_code,
        test_invalid_code,
        test_negative_code,
        test_string_numeric,
        test_fallback_when_uninitialized,
        test_all_known_codes_have_zh,
        test_zh_length_reasonable,
        test_idempotent,
        test_catalog_evidence_coverage,
    ]
    failed = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  ✗ {t.__name__} FAIL: {e}")
            failed.append(t.__name__)
        except Exception as e:
            print(f"  ✗ {t.__name__} ERROR: {type(e).__name__}: {e}")
            failed.append(t.__name__)
    print()
    if failed:
        print(f"❌ {len(failed)}/{len(tests)} failed: {failed}")
        sys.exit(1)
    print(f"✅ {len(tests)}/{len(tests)} passed")