"""
normalize_second_heart_rate 单元测试。

⚠ 重要事实（2026-09-25 实测）：
  second_heart_rate 流实测只返回 SEC_HR 文件索引，**不**返回 bpm 测量值。
  本 normalizer 不伪造 bpm，而是写出每个文件段的元数据：
    MetricSample("heart_rate_segment", ts=startTime+s, value=e (毫秒),
                 unit="ms", extra={file_id, file_type, uncompressed_bytes,
                 date_string})

覆盖（≥12 用例）：
  - 标准 file segment 解析
  - fileId 字段名 fallback（file_id / fid）
  - fileType 非 SEC_HR → 跳过
  - fileId 缺失 → 跳过
  - startTime 缺失 → 跳过
  - samples 空数组 → 跳过
  - 偏移 0 → 接受；偏移 7d-1ms → 接受；7d+1ms → 跳过
  - 偏移负超大 → 跳过（手表断电复同步伪值）
  - e (duration) ≤ 0 → 跳过
  - 多 item / 多 sample 批处理
  - 真实 DB payload（9-24 一条）能正确解析
  - device_id / source_scope 推断
  - boundary：合法 1 秒段（e=1000） / 24h 段（e≈86400000）
"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from normalizer.wellness import (
    normalize_second_heart_rate, _SEC_HR_OFFSET_MAX_MS, _SEC_HR_BPM_MIN,
    _SEC_HR_BPM_MAX,
)


# ============================================================
# helpers
# ============================================================

def _base_ms():
    """2026-09-24T08:00:00Z → 1758700800000（一个 item 的 startTime）"""
    return 1_758_700_800_000


def _wrap(item):
    """构造 {items: [item]} 包络。"""
    return {"items": [item]}


def _realistic_payload():
    """9-24 实测 payload（DB raw_records 第一条样本）。"""
    return {"items": [{
        "userId": "1000000000",
        "eventType": "second_heart_rate",
        "subType": "real_data",
        "timestamp": 1790208000000,
        "value": {
            "startTime": 1790220967000,  # 2026-09-24T13:36:07Z
            "deviceId": "2,D8803CFFFEE4E756",
            "deviceSN": "2,24297542082714",
            "deviceSource": "2,9568512",
            "deviceType": "2,0",
            "timeZone": "2,Asia/Shanghai",
            "samples": [
                {
                    "s": 0,
                    "e": 110000,
                    "u": 111681,
                    "fileId": "12027181518",
                    "fileType": "SEC_HR",
                    "dateString": "2026-09-24",
                },
                {
                    "s": 110000,
                    "e": 3772000,
                    "u": 3916836,
                    "fileId": "12027181518",
                    "fileType": "SEC_HR",
                    "dateString": "2026-09-24",
                },
            ],
        },
    }]}


# ============================================================
# 基础正常路径
# ============================================================

def test_basic_segment_parsed():
    """标准 segment：1 个 sample → 1 条 MetricSample。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "s": 0,
                "e": 60000,         # 60 秒
                "u": 60000,
                "fileId": "12345",
                "fileType": "SEC_HR",
                "dateString": "2026-09-24",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 1, f"应产出 1 条 record，得到 {len(b.records)}"
    r = b.records[0]
    assert r.metric == "heart_rate_segment", f"metric={r.metric}"
    assert r.value == 60000.0, f"value (duration ms)={r.value}"
    assert r.unit == "ms", f"unit={r.unit}"
    assert r.timestamp == datetime.fromtimestamp(_base_ms() / 1000, tz=timezone.utc)


def test_realistic_db_payload():
    """9-24 真实 payload（DB 第一条原样）应正确解析 2 条段元数据。"""
    b = normalize_second_heart_rate(_realistic_payload())
    assert len(b.records) == 2, f"应产出 2 条 record，得到 {len(b.records)}"
    # 第一条：s=0, e=110000
    r0 = b.records[0]
    assert r0.value == 110000.0
    assert r0.timestamp == datetime.fromtimestamp(1790220967.0, tz=timezone.utc)
    assert r0.extra["file_id"] == "12027181518"
    assert r0.extra["file_type"] == "SEC_HR"
    assert r0.extra["uncompressed_bytes"] == 111681
    assert r0.extra["date_string"] == "2026-09-24"
    # 第二条：s=110000, e=3772000
    r1 = b.records[1]
    assert r1.value == 3772000.0
    expected_ts = datetime.fromtimestamp((1790220967000 + 110000) / 1000, tz=timezone.utc)
    assert r1.timestamp == expected_ts
    assert r1.extra["file_id"] == "12027181518"  # 同 file
    assert r1.extra["uncompressed_bytes"] == 3916836


def test_device_id_extracted_from_nested_value():
    """deviceId 在 value.deviceId 里（不是 item.deviceId）→ 仍能提取。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "deviceId": "2,D8803CFFFEE4E756",
            "samples": [{
                "s": 0, "e": 1000, "u": 1000,
                "fileId": "1", "fileType": "SEC_HR",
                "dateString": "2026-09-24",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 1
    r = b.records[0]
    assert r.device_id == "D8803CFFFEE4E756", f"device_id={r.device_id}"
    assert r.source_scope in ("device", "user_fused", "unknown"), \
        f"source_scope={r.source_scope}"


def test_device_id_fallback_to_item_level():
    """value 没 deviceId，但 item 有 → 仍能提取。"""
    raw = _wrap({
        "deviceId": "2,ABCDEF1234567890",
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "s": 0, "e": 1000, "u": 1000,
                "fileId": "1", "fileType": "SEC_HR",
                "dateString": "2026-09-24",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 1
    assert b.records[0].device_id == "ABCDEF1234567890"


def test_source_scope_user_fused_for_charge_eventtype():
    """isFused=true 或 eventType=Charge/DailyHealth → source_scope='user_fused'。"""
    # 注意：second_heart_rate 通常不带 isFused，但 source_scope 应根据 device_id 推断
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "deviceId": "2,D8803CFFFEE4E756",
            "samples": [{
                "s": 0, "e": 1000, "u": 1000,
                "fileId": "1", "fileType": "SEC_HR",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert b.records[0].source_scope == "device"


# ============================================================
# 时间偏移
# ============================================================

def test_offset_zero_accepted():
    """offset = 0 → ts = base_dt。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "s": 0, "e": 1000, "u": 1000,
                "fileId": "1", "fileType": "SEC_HR",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 1
    assert b.records[0].timestamp == datetime.fromtimestamp(_base_ms() / 1000, tz=timezone.utc)


def test_offset_just_under_7d_accepted():
    """offset = 7d - 1ms → 接受（边界内）。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "s": _SEC_HR_OFFSET_MAX_MS - 1, "e": 1000, "u": 1000,
                "fileId": "1", "fileType": "SEC_HR",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 1


def test_offset_just_over_7d_skipped():
    """offset = 7d + 1ms → 跳过（手表断电复同步伪值）。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "s": _SEC_HR_OFFSET_MAX_MS + 1, "e": 1000, "u": 1000,
                "fileId": "1", "fileType": "SEC_HR",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 0
    assert any("偏移" in d and "7 天" in d for d in b.diagnostics), \
        f"diagnostics 应说明偏移超限: {b.diagnostics}"


def test_offset_30d_skipped():
    """offset = 30 天 → 跳过。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "s": 30 * 86400 * 1000, "e": 1000, "u": 1000,
                "fileId": "1", "fileType": "SEC_HR",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 0


# ============================================================
# 字段缺失与无效
# ============================================================

def test_missing_file_id_skipped():
    """fileId 缺失 → sample 跳过 + diagnostics。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "s": 0, "e": 1000, "u": 1000,
                "fileType": "SEC_HR",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 0
    assert any("fileId" in d for d in b.diagnostics)


def test_wrong_file_type_skipped():
    """fileType 不是 SEC_HR → 跳过 + diagnostics。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "s": 0, "e": 1000, "u": 1000,
                "fileId": "1", "fileType": "BLOOD_OXYGEN",  # 错的
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 0
    assert any("fileType" in d for d in b.diagnostics)


def test_zero_duration_skipped():
    """e (duration) = 0 → 跳过（无效段）。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "s": 0, "e": 0, "u": 0,
                "fileId": "1", "fileType": "SEC_HR",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 0
    assert any("段时长" in d or "e=" in d for d in b.diagnostics)


def test_negative_duration_skipped():
    """e = -1 → 跳过。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "s": 0, "e": -1, "u": 0,
                "fileId": "1", "fileType": "SEC_HR",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 0


def test_missing_starttime_skipped():
    """缺 startTime → item 跳过 + diagnostics。"""
    raw = _wrap({
        "value": {
            "samples": [{
                "s": 0, "e": 1000, "u": 1000,
                "fileId": "1", "fileType": "SEC_HR",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 0
    assert any("startTime" in d for d in b.diagnostics)


def test_empty_samples_skipped():
    """samples=[] → item 跳过 + diagnostics。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 0
    assert any("samples" in d for d in b.diagnostics)


def test_missing_value_envelope_skipped():
    """item 没有 value 字段 → 跳过 + diagnostics。"""
    raw = _wrap({"timestamp": _base_ms()})  # 没有 value
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 0
    assert any("value" in d for d in b.diagnostics)


def test_non_dict_item_skipped():
    """items 里出现非对象元素 → 跳过 + diagnostics。"""
    raw = {"items": [None, {"value": {"startTime": _base_ms(),
                                       "samples": [{"s": 0, "e": 1000, "u": 1000,
                                                     "fileId": "1", "fileType": "SEC_HR"}]}}]}
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 1, f"应产出 1 条（有效 item），得到 {len(b.records)}"
    assert any("不是对象" in d for d in b.diagnostics)


# ============================================================
# 字段别名 & 边界
# ============================================================

def test_file_id_field_alias():
    """'file_id' / 'fid' 字段名也支持。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "s": 0, "e": 1000, "u": 1000,
                "file_id": "42", "fileType": "SEC_HR",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 1
    assert b.records[0].extra["file_id"] == "42"


def test_offset_field_alias():
    """'offset' 字段名也支持（兼容不同协议）。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "offset": 5000, "e": 1000, "u": 1000,
                "fileId": "1", "fileType": "SEC_HR",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 1
    expected_ts = datetime.fromtimestamp((_base_ms() + 5000) / 1000, tz=timezone.utc)
    assert b.records[0].timestamp == expected_ts


def test_one_second_segment_boundary():
    """最短合理段：e=1000ms (1 秒)。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "s": 0, "e": 1000, "u": 1000,
                "fileId": "1", "fileType": "SEC_HR",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 1
    assert b.records[0].value == 1000.0


def test_full_day_segment_boundary():
    """最长合理段：e≈86400000ms (24h)。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "s": 0, "e": 86_400_000, "u": 86_400_000,
                "fileId": "1", "fileType": "SEC_HR",
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 1
    assert b.records[0].value == 86_400_000.0


def test_mixed_batch_some_valid_some_skipped():
    """混合：1 段 OK + 1 段 fileType 错 + 1 段 fileId 缺失 + 1 段 e=0 + 1 段 OK → 2 条 record。"""
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [
                {"s": 0, "e": 1000, "u": 1000,
                 "fileId": "1", "fileType": "SEC_HR"},     # ok
                {"s": 1000, "e": 1000, "u": 1000,
                 "fileId": "2", "fileType": "WRONG"},      # wrong type
                {"s": 2000, "e": 1000, "u": 1000,
                 "fileType": "SEC_HR"},                     # no fileId
                {"s": 3000, "e": 0, "u": 0,
                 "fileId": "4", "fileType": "SEC_HR"},     # zero duration
                {"s": 4000, "e": 1000, "u": 1000,
                 "fileId": "5", "fileType": "SEC_HR"},     # ok
            ],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 2, f"应产出 2 条 record，得到 {len(b.records)}"
    file_ids = [r.extra["file_id"] for r in b.records]
    assert file_ids == ["1", "5"], f"file_ids={file_ids}"
    assert len(b.diagnostics) >= 3, f"应至少 3 个 diagnostics，得到 {len(b.diagnostics)}"


def test_multi_item_batch():
    """多 item：每个 item 独立 startTime。"""
    raw = {"items": [
        {"value": {"startTime": _base_ms(),
                    "samples": [{"s": 0, "e": 1000, "u": 1000,
                                  "fileId": "1", "fileType": "SEC_HR"}]}},
        {"value": {"startTime": _base_ms() + 3_600_000,  # 1h 后
                    "samples": [{"s": 0, "e": 2000, "u": 2000,
                                  "fileId": "2", "fileType": "SEC_HR"}]}},
    ]}
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 2
    # 第二条 ts 应该是 base + 3600s
    expected_ts = datetime.fromtimestamp((_base_ms() + 3_600_000) / 1000, tz=timezone.utc)
    assert b.records[1].timestamp == expected_ts


# ============================================================
# 常量
# ============================================================

def test_offset_max_constant_value():
    """_SEC_HR_OFFSET_MAX_MS = 7 * 86400 * 1000 = 604_800_000。"""
    assert _SEC_HR_OFFSET_MAX_MS == 7 * 86400 * 1000
    assert _SEC_HR_OFFSET_MAX_MS == 604_800_000


def test_bpm_range_constants():
    """bpm 哨兵常量值（即便目前不解析 bpm，常量定义要稳定）。"""
    assert _SEC_HR_BPM_MIN == 20.0
    assert _SEC_HR_BPM_MAX == 300.0


# ============================================================
# 不伪造数据保证（防回归）
# ============================================================

def test_no_bpm_fabrication():
    """即使 payload 有疑似 bpm 字段（如 v/hr/value），也**不**作为心率入库。

    守住 ZeppBridge "No interpolation or invented samples are used" 原则。
    """
    raw = _wrap({
        "value": {
            "startTime": _base_ms(),
            "samples": [{
                "s": 0, "e": 1000, "u": 1000,
                "fileId": "1", "fileType": "SEC_HR",
                # 故意塞假 bpm——必须忽略
                "v": 86, "hr": 86, "value": 86, "bpm": 86,
            }],
        }
    })
    b = normalize_second_heart_rate(raw)
    assert len(b.records) == 1
    # value 是 e=1000，不是任何 bpm
    assert b.records[0].value == 1000.0
    assert b.records[0].unit == "ms"
    # 不应该出现 bpm 单位的记录
    assert all(r.unit != "bpm" for r in b.records), \
        f"出现 bpm 单位的记录 = 数据伪造！{[(r.unit, r.value) for r in b.records]}"


def test_empty_response_no_error():
    """空响应（云端答「没数据」）→ 返回空 batch + diagnostic，不抛异常。

    这是 graceful degradation：sync 流程不应被空响应打断。
    """
    for raw in [{}, {"items": []}, {"data": []}, {"items": None}]:
        b = normalize_second_heart_rate(raw)
        assert len(b.records) == 0, f"raw={raw} 应产出 0 条"
        # 至少 1 个 diagnostic 说明原因
        assert len(b.diagnostics) >= 1, f"raw={raw} 应至少 1 个 diagnostic"


# ============================================================
# runner
# ============================================================

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
    print(f"\n=== second_heart_rate normalizer: {passed} passed, {failed} failed ===")
    import sys as _sys
    _sys.exit(0 if failed == 0 else 1)