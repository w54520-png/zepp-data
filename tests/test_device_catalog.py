"""test_device_catalog.py — 验证 device_catalog.py / device_catalog.json

测试覆盖：
1. 查已知 productId 130 → GTR Mini
2. 查已知 productId 146 → Balance 2
3. 查不存在 productId → 返 unknown
4. 查已知 deviceSource 8519936 → GTR Mini
5. 查已知 deviceSource 9568512 → Balance 2
6. 查不存在 deviceSource → 返 unknown
7. product_id=None / device_source=None → 返 unknown (不抛)
8. 边界：productId=0（placeholder 自身 product_id）→ 返 placeholder
9. catalog_id 字符串正确性（不在 catalog 里造新名字）
10. all_devices() 至少返回 3 条（含 placeholder）
"""
import sys
import os

# 让 tests/ 能 import scripts/
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import device_catalog as dc


def test_product_id_gtr_mini():
    """productId 130 → GTR Mini."""
    catalog_id, name_zh = dc.device_info(130)
    assert catalog_id == "amazfit_gtr_mini", f"got {catalog_id!r}"
    assert "GTR Mini" in name_zh, f"got {name_zh!r}"
    print(f"  ✓ productId 130 → {catalog_id} / {name_zh}")


def test_product_id_balance_2():
    """productId 146 → Balance 2 (M4 新发现)."""
    catalog_id, name_zh = dc.device_info(146)
    assert catalog_id == "amazfit_balance_2", f"got {catalog_id!r}"
    assert "Balance 2" in name_zh, f"got {name_zh!r}"
    print(f"  ✓ productId 146 → {catalog_id} / {name_zh}")


def test_product_id_unknown_returns_placeholder():
    """不存在的 productId → placeholder，不抛."""
    catalog_id, name_zh = dc.device_info(99999999)
    assert catalog_id == "generic_unknown"
    assert name_zh == "未识别 Zepp 设备"
    print(f"  ✓ productId 99999999 → {catalog_id} / {name_zh}")


def test_device_source_gtr_mini():
    """deviceSource 8519936 → GTR Mini (跨字段映射)."""
    catalog_id, name_zh = dc.device_info_from_source(8519936)
    assert catalog_id == "amazfit_gtr_mini"
    assert "GTR Mini" in name_zh
    print(f"  ✓ deviceSource 8519936 → {catalog_id} / {name_zh}")


def test_device_source_balance_2():
    """deviceSource 9568512 → Balance 2 (M4 新发现)."""
    catalog_id, name_zh = dc.device_info_from_source(9568512)
    assert catalog_id == "amazfit_balance_2"
    assert "Balance 2" in name_zh
    print(f"  ✓ deviceSource 9568512 → {catalog_id} / {name_zh}")


def test_device_source_unknown_returns_placeholder():
    """不存在的 deviceSource → placeholder."""
    catalog_id, name_zh = dc.device_info_from_source(123456789)
    assert catalog_id == "generic_unknown"
    assert name_zh == "未识别 Zepp 设备"
    print(f"  ✓ deviceSource 123456789 → {catalog_id} / {name_zh}")


def test_none_input_returns_placeholder():
    """None 输入不抛异常 → placeholder."""
    cat1, name1 = dc.device_info(None)
    cat2, name2 = dc.device_info_from_source(None)
    assert cat1 == "generic_unknown"
    assert cat2 == "generic_unknown"
    print(f"  ✓ None input safe (no exception)")


def test_placeholder_self_lookup():
    """placeholder 自身的 product_id=0 应该返回 placeholder 自己."""
    catalog_id, name_zh = dc.device_info(0)
    assert catalog_id == "generic_unknown"
    print(f"  ✓ productId 0 (placeholder self) → {catalog_id} / {name_zh}")


def test_catalog_id_format():
    """catalog_id 必须是稳定的 snake_case 字符串，不能动态生成."""
    catalog_id, _ = dc.device_info(130)
    # 验证 catalog_id 真的在 catalog.json 里
    all_cats = [d["catalog_id"] for d in dc.all_devices()]
    assert catalog_id in all_cats, f"{catalog_id!r} not in {all_cats}"
    print(f"  ✓ catalog_id 来自 catalog.json（不动态生成）")


def test_all_devices_returns_at_least_three():
    """all_devices() 至少返回 3 条（含 placeholder）."""
    devices = dc.all_devices()
    assert len(devices) >= 3, f"got only {len(devices)} devices"
    has_placeholder = any(d.get("_is_placeholder") for d in devices)
    assert has_placeholder, "missing generic_unknown placeholder"
    print(f"  ✓ all_devices() returns {len(devices)} entries (≥3, with placeholder)")


# ============ 跑所有测试 ============

if __name__ == "__main__":
    tests = [
        test_product_id_gtr_mini,
        test_product_id_balance_2,
        test_product_id_unknown_returns_placeholder,
        test_device_source_gtr_mini,
        test_device_source_balance_2,
        test_device_source_unknown_returns_placeholder,
        test_none_input_returns_placeholder,
        test_placeholder_self_lookup,
        test_catalog_id_format,
        test_all_devices_returns_at_least_three,
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