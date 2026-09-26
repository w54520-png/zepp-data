"""
device_id() 单元测试。

覆盖 fixtures：
  - 合法 14/16 hex id
  - 逗号分隔的 "1,app" / "1,-1" / "1,1440,app" 记账（应该取最长真 id 或 None）
  - 占位 id（single-device-firmware / unknown-device / no-device / firmware-default）
  - 数字 id（Zepp 偶尔返回 number 类型）
  - 边界：空串 / 全空白 / 不存在 / 非 dict
  - key 顺序：device_id / deviceId / deviceid / sourceDeviceId
"""
import sys
from pathlib import Path

# 把 scripts 加到 sys.path（直接调 normalizer.common）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from normalizer.common import device_id


def assert_eq(actual, expected, label):
    assert actual == expected, f"[{label}] 期望 {expected!r}，得到 {actual!r}"


def test_legal_14char_hex():
    """合法的 14 字符 hex id 直接通过。"""
    obj = {"deviceId": "D85403FFFEE4D5"}
    assert_eq(device_id(obj), "D85403FFFEE4D5", "14-char hex")


def test_legal_16char_hex():
    """16 字符 BLE MAC 风格 id。"""
    obj = {"deviceId": "D85403FFFEE4D576"}
    assert_eq(device_id(obj), "D85403FFFEE4D576", "16-char hex")


def test_comma_with_app_suffix_extracts_longest():
    """Zepp 经常返回 "1,D85403FFFEE4D576"——取最长真 id。"""
    obj = {"device_id": "1,D85403FFFEE4D576"}
    assert_eq(device_id(obj), "D85403FFFEE4D576", "comma with real id")


def test_comma_with_1_app_only_returns_none():
    """只有 "1,app" 这种 bookkeeping——没真 id，返回 None。"""
    obj = {"device_id": "1,app"}
    assert_eq(device_id(obj), None, "1,app only")


def test_comma_with_1_dash1_app_returns_none():
    """'1,-1,app'——还是只有记账值。"""
    obj = {"deviceId": "1,-1,app"}
    assert_eq(device_id(obj), None, "1,-1,app")


def test_comma_with_1440_app_returns_none():
    """'1440,app'（24h 分辨率记账）。"""
    obj = {"deviceId": "1440,app"}
    assert_eq(device_id(obj), None, "1440,app")


def test_placeholder_single_device_firmware_returns_none():
    """Zepp 占位 id（用户没戴手表时填的）。"""
    obj = {"deviceId": "single-device-firmware"}
    assert_eq(device_id(obj), None, "single-device-firmware")


def test_placeholder_unknown_device_returns_none():
    obj = {"deviceId": "unknown-device"}
    assert_eq(device_id(obj), None, "unknown-device")


def test_placeholder_no_device_returns_none():
    obj = {"device_id": "no-device"}
    assert_eq(device_id(obj), None, "no-device")


def test_placeholder_firmware_default_returns_none():
    obj = {"deviceId": "firmware-default"}
    assert_eq(device_id(obj), None, "firmware-default")


def test_numeric_device_id_returns_str():
    """Zepp 偶尔给 number 类型 deviceId。"""
    obj = {"deviceId": 12345678901234}
    # 14 位数字 → 满足 ≥8 + alphanumeric → 字符串返回
    assert_eq(device_id(obj), "12345678901234", "numeric 14-digit")


def test_too_short_id_returns_none():
    """<8 字符直接拒绝。"""
    obj = {"deviceId": "D85403"}
    assert_eq(device_id(obj), None, "6-char too short")


def test_special_chars_returns_none():
    """包含特殊字符（如 -）的 id 不匹配正则。"""
    obj = {"deviceId": "D85403F-EE4D576"}
    assert_eq(device_id(obj), None, "with dash")


def test_falls_back_to_sourceDeviceId():
    """无 deviceId 时尝试 sourceDeviceId。"""
    obj = {"sourceDeviceId": "D85403FFFEE4D576"}
    assert_eq(device_id(obj), "D85403FFFEE4D576", "sourceDeviceId fallback")


def test_falls_back_to_deviceid_lowercase():
    """deviceid 全小写也能识别。"""
    obj = {"deviceid": "D85403FFFEE4D576"}
    assert_eq(device_id(obj), "D85403FFFEE4D576", "deviceid lowercase key")


def test_missing_field_returns_none():
    """没有 id 字段。"""
    assert_eq(device_id({}), None, "empty dict")
    assert_eq(device_id({"foo": "bar"}), None, "no id field")


def test_non_dict_input_returns_none():
    """非 dict 入参安全返回 None。"""
    assert_eq(device_id(None), None, "None input")
    assert_eq(device_id([]), None, "list input")
    assert_eq(device_id("D85403FFFEE4D576"), None, "string input")


def test_empty_string_returns_none():
    obj = {"deviceId": ""}
    assert_eq(device_id(obj), None, "empty string")


def test_whitespace_only_returns_none():
    obj = {"deviceId": "   "}
    assert_eq(device_id(obj), None, "whitespace only")


def test_picks_longest_among_multiple_segments():
    """多个候选 id——取最长（罕见但 Zepp 历史 payload 有过）。"""
    obj = {"deviceId": "AB1234567,AB123456789,app"}
    # 三个候选：AB1234567(10), AB123456789(12), app(<8) → 取 AB123456789
    assert_eq(device_id(obj), "AB123456789", "pick longest")


def test_max_priority_dict_order():
    """key 优先级：device_id > deviceId > deviceid > sourceDeviceId。"""
    obj = {
        "device_id": "FIRSTID1234",
        "deviceId": "D85403FFFEE4D576",
    }
    assert_eq(device_id(obj), "FIRSTID1234", "device_id first")


if __name__ == "__main__":
    import inspect
    import sys as _sys
    fns = [
        (n, f) for n, f in globals().items()
        if n.startswith("test_") and callable(f)
    ]
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
    print(f"\n=== device_id: {passed} passed, {failed} failed ===")
    _sys.exit(0 if failed == 0 else 1)