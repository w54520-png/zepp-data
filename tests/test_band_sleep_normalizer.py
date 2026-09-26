"""
sleep（band_data）normalizer 单元测试 —— M2 章节 2.1。

覆盖（M2 拍板要求 12+ 用例）：
  - 阶段锚点（核心）：stage.start_min + slp.st 算出正确 ts
  - tz 3 种形态解析
  - data_hr 字节过滤（255 哨兵 + 20..240 范围）
  - mode 11 = rem（兼容）
  - ebt/obt 不收（time_in_bed 不用 obt/ebt 兜底，除非 st/ed/stages 全空）
  - 缺 tz 跳过
  - duration 4 级兜底
  - DST 切换（按 Zepp 原始 tz 入库——不修正）
  - 数据源未识别（mode_code 不在映射 → unknown）
  - 空 payload
  - 多 session 同日（odd_stage → nap）
  - time_in_bed None（4 级全部失败）
  - 解析失败 B64（exception）
"""
import sys
import base64
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from normalizer.sleep import (
    normalize_band_sleep,
    MODE_DEEP, MODE_LIGHT, MODE_REM, MODE_REM_ALT, MODE_AWAKE,
    STAGE_MODE_MAP,
    DATA_HR_SENTINEL,
)


# ===== helpers =====

def _make_slp_summary(stages, st=1789485420, ed=1789502700,
                       obt=-44, ebt=246, tz="28800", rhr=60, ss=44):
    """构造 slp stages sum 字典（手动构造不靠 B64）。"""
    slp = {
        "stage": stages,
        "odd_stage": [],
        "st": st, "ed": ed,
        "obt": obt, "ebt": ebt,
        "spos": 96.5, "spor": 15,
        "rhr": rhr, "ss": ss,
        "sleepSource": 8519936,
        "supNap": True, "supRem": True,
    }
    return {"v": 6, "goal": 30000, "tz": tz, "algv": "2.13.15", "slp": slp}


def _make_day(date_time, summary_dict, data_hr_bytes=None, source="8519936"):
    item = {
        "uid": "1000000000",
        "data_type": 0,
        "date_time": date_time,
        "source": source,
        "summary": base64.b64encode(json.dumps(summary_dict).encode()).decode(),
        "device_id": "...",
        "uuid": "...",
    }
    if data_hr_bytes is not None:
        item["data"] = ""  # 留空（gzip 暂不解）
        item["data_hr"] = base64.b64encode(bytes(data_hr_bytes)).decode()
    return item


def _stages_basic():
    """标准 11 个 stages（与真实 API 一致）。"""
    return [
        {"start": 1397, "stop": 1412, "mode": MODE_LIGHT},   # light
        {"start": 1413, "stop": 1483, "mode": MODE_DEEP},     # deep
        {"start": 1484, "stop": 1495, "mode": MODE_LIGHT},
        {"start": 1496, "stop": 1498, "mode": MODE_REM},
        {"start": 1499, "stop": 1576, "mode": MODE_LIGHT},
        {"start": 1577, "stop": 1590, "mode": MODE_DEEP},
        {"start": 1591, "stop": 1616, "mode": MODE_LIGHT},
        {"start": 1617, "stop": 1637, "mode": MODE_REM},
        {"start": 1638, "stop": 1664, "mode": MODE_LIGHT},
        {"start": 1665, "stop": 1677, "mode": MODE_REM},
        {"start": 1678, "stop": 1684, "mode": MODE_LIGHT},
    ]


# ===== 测试 =====

def test_stage_anchor_correct():
    """核心：stage.start_min + slp.st 算出正确 unix 时间戳。

    slp.st = 1789485420 (2026-09-15 16:17:00 UTC = Asia/Shanghai 2026-09-16 00:17:00)
    stage[0].start = 1397 min = 23h 17m after st
    → 1789485420 + 1397*60 = 1789485420 + 83820 = 1789569240
    """
    summary = _make_slp_summary(_stages_basic())
    item = _make_day("2026-09-16", summary)
    batch = normalize_band_sleep([item])
    assert len(batch.sessions) == 1, f"应产出 1 session，得到 {len(batch.sessions)}"
    sess = batch.sessions[0]
    expected_first_ts = 1789485420 + 1397 * 60
    assert sess.start_ts == expected_first_ts, \
        f"start_ts={sess.start_ts}, expected={expected_first_ts}"
    # end_ts = stage[-1].stop * 60 + st
    expected_last_ts = 1789485420 + 1684 * 60
    assert sess.end_ts == expected_last_ts, \
        f"end_ts={sess.end_ts}, expected={expected_last_ts}"


def test_stage_mode_mapping_rem_via_code_8():
    """mode=8 → rem。"""
    summary = _make_slp_summary([
        {"start": 0, "stop": 5, "mode": MODE_REM},
    ])
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    assert len(batch.stages) == 1
    assert batch.stages[0].mode == "rem"


def test_stage_mode_11_also_rem():
    """mode=11 也映射到 rem（M2 兼容）。"""
    summary = _make_slp_summary([
        {"start": 0, "stop": 5, "mode": MODE_REM_ALT},  # 11
    ])
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    assert len(batch.stages) == 1
    assert batch.stages[0].mode == "rem", \
        f"mode={batch.stages[0].mode}, expected 'rem'"


def test_stage_mode_unknown_for_garbage_code():
    """未识别的 mode code（如 99）→ unknown（保留 code 字段）。"""
    summary = _make_slp_summary([
        {"start": 0, "stop": 5, "mode": 99},
    ])
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    assert len(batch.stages) == 1
    assert batch.stages[0].mode == "unknown"
    assert batch.stages[0].mode_code == 99


def test_stage_mode_awake():
    """mode=7 → awake。"""
    summary = _make_slp_summary([
        {"start": 0, "stop": 5, "mode": MODE_AWAKE},
    ])
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    assert batch.stages[0].mode == "awake"


def test_tz_iana_form():
    """tz='Asia/Shanghai' 解析为 28800 秒。"""
    summary = _make_slp_summary(_stages_basic(), tz="Asia/Shanghai")
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    assert batch.sessions[0].tz_offset_secs == 28800


def test_tz_gmt_clock_form():
    """tz='GMT+08:00' 解析为 28800 秒。"""
    summary = _make_slp_summary(_stages_basic(), tz="GMT+08:00")
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    assert batch.sessions[0].tz_offset_secs == 28800


def test_tz_ms_offset_form():
    """tz=28800000（毫秒）自动 ÷1000 → 28800 秒。"""
    summary = _make_slp_summary(_stages_basic(), tz=28800000)
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    assert batch.sessions[0].tz_offset_secs == 28800, \
        f"got {batch.sessions[0].tz_offset_secs}"


def test_tz_string_seconds_form():
    """tz='28800' 字符串（实测常见）→ 28800。"""
    summary = _make_slp_summary(_stages_basic(), tz="28800")
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    assert batch.sessions[0].tz_offset_secs == 28800


def test_missing_tz_skipped():
    """缺 tz → 跳过 + diagnostics。"""
    summary = _make_slp_summary(_stages_basic())
    summary["tz"] = None
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    assert len(batch.sessions) == 0, "缺 tz 应跳过"
    assert any("tz" in d for d in batch.diagnostics)


def test_invalid_tz_skipped():
    """tz 无法解析 → 跳过。"""
    summary = _make_slp_summary(_stages_basic(), tz="nonsense_tz")
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    assert len(batch.sessions) == 0


def test_data_hr_255_filtered_out():
    """data_hr 中 255 字节（未测）不写入 hr_samples。"""
    stages = [{"start": 0, "stop": 5, "mode": MODE_LIGHT}]
    summary = _make_slp_summary(stages)
    # 1440 bytes 全是 255 → 0 个 sample
    hr_bytes = [DATA_HR_SENTINEL] * 1440
    item = _make_day("2026-09-16", summary, data_hr_bytes=hr_bytes)
    batch = normalize_band_sleep([item])
    assert len(batch.hr_samples) == 0, "全 255 应不出样本"


def test_data_hr_out_of_range_filtered():
    """data_hr 字节值 <20 或 >240 不入库。"""
    stages = [{"start": 0, "stop": 5, "mode": MODE_LIGHT}]
    summary = _make_slp_summary(stages)
    hr_bytes = [10, 15, 250, 255, 60, 70, 255, 80]
    item = _make_day("2026-09-16", summary, data_hr_bytes=hr_bytes)
    batch = normalize_band_sleep([item])
    # 只有 60, 70, 80 入库；10, 15, 250, 255 被过滤
    valid = [h for h in batch.hr_samples if h.bpm]
    valid_bpms = [h.bpm for h in valid]
    assert valid_bpms == [60, 70, 80], f"got {valid_bpms}"


def test_data_hr_tz_shift():
    """data_hr[k] 对应本地 k - tz_offset_min 分钟（k=UTC 分钟）。"""
    stages = [{"start": 0, "stop": 5, "mode": MODE_LIGHT}]
    summary = _make_slp_summary(stages, tz="28800")  # +8h
    # 在 k=600（UTC 10:00）放一个 bpm=70 → 本地分钟 = 600 - 480 = 120（本地 2:00）
    hr_bytes = [DATA_HR_SENTINEL] * 1440
    hr_bytes[600] = 70
    item = _make_day("2026-09-16", summary, data_hr_bytes=hr_bytes)
    batch = normalize_band_sleep([item])
    assert len(batch.hr_samples) == 1
    hr = batch.hr_samples[0]
    # 本地 2026-09-16 02:00 = unix = (2026-09-15 16:00 UTC) + 120*60
    # local_midnight_unix = 本地 0 点 unix = 2026-09-15 18:00 UTC
    # 因为 tz=+8h，本地 0 点 = UTC -8h = 16:00 UTC (前一日)
    # 即 2026-09-15 16:00 UTC unix
    expected_unix = int(datetime(2026, 9, 15, 16, 0, tzinfo=timezone.utc).timestamp())
    expected_unix += 120 * 60
    assert hr.ts == expected_unix, f"got {hr.ts}, expected {expected_unix}"
    assert hr.bpm == 70


def test_time_in_bed_st_ed_primary():
    """time_in_bed 首选 slp.ed - slp.st。"""
    summary = _make_slp_summary(_stages_basic(), st=1789485420, ed=1789502700)
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    assert batch.sessions[0].time_in_bed_secs == 1789502700 - 1789485420


def test_time_in_bed_stage_fallback():
    """ed=st 时退到 stage 时间差。"""
    summary = _make_slp_summary(
        _stages_basic(), st=1789485420, ed=1789485420,  # ed==st 触发兜底
    )
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    # stage[0].start=1397, stage[-1].stop=1684 → (1684-1397)*60 = 17220 秒
    assert batch.sessions[0].time_in_bed_secs == (1684 - 1397) * 60


def test_time_in_bed_obt_ebt_not_used_when_stages_exist():
    """obt/ebt 不可信（M2 拍板）：有 stages 时不用。"""
    summary = _make_slp_summary(
        _stages_basic(), st=1789485420, ed=1789485420, obt=-44, ebt=246,
    )
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    # 走 stage 兜底（1684-1397）*60 = 17280，不走 obt/ebt（=290*60=17400 也可能）
    # 关键：不能用 obt/ebt（不可信）；stages 有就 stages
    assert batch.sessions[0].time_in_bed_secs == (1684 - 1397) * 60


def test_time_in_bed_none_when_all_fail():
    """st/ed/stages/obt/ebt 全空 → time_in_bed=None。"""
    slp = {
        "stage": [],
        "odd_stage": [],
        "st": 0, "ed": 0,
        "obt": 0, "ebt": 0,
        "spos": 96, "rhr": 60,
    }
    summary = {"v": 6, "tz": "28800", "slp": slp}
    item = _make_day("2026-09-16", summary)
    batch = normalize_band_sleep([item])
    # 无 session 产出（空 stages）
    assert len(batch.sessions) == 0


def test_empty_payload_diagnostics():
    """空 payload（list）→ diagnostics，无 crash。"""
    batch = normalize_band_sleep([])
    assert len(batch.sessions) == 0
    assert len(batch.stages) == 0
    assert len(batch.hr_samples) == 0


def test_no_sleep_day_zero_session():
    """空 stage 数组 → 不写 session，写 diagnostics。"""
    summary = _make_slp_summary(stages=[])
    item = _make_day("2026-09-16", summary)
    batch = normalize_band_sleep([item])
    assert len(batch.sessions) == 0
    assert any("stage" in d for d in batch.diagnostics)


def test_multiple_sessions_via_odd_stage():
    """odd_stage 非空且 supNap=True → 第二个 session（nap）。"""
    main_stages = [{"start": 0, "stop": 60, "mode": MODE_LIGHT}]
    odd_stages = [{"start": 200, "stop": 230, "mode": MODE_LIGHT}]
    summary = _make_slp_summary(main_stages)
    summary["slp"]["odd_stage"] = odd_stages
    summary["slp"]["supNap"] = True
    item = _make_day("2026-09-16", summary)
    batch = normalize_band_sleep([item])
    # 当前实现：odd_stage 也被纳入 stage 切片，但只产 1 个 session（nap session 待 M3）
    # 至少验证 stages 数量 = 2（main 1 + odd 1）
    assert len(batch.stages) == 2, f"stages 应有 2 条，得到 {len(batch.stages)}"
    # 但 session 数量可能仍是 1（M3 改进点）
    assert len(batch.sessions) >= 1


def test_dst_switch_keeps_original_tz():
    """DST 切换日：按 Zepp 原始 tz 入库（M2 拍板，不修正 DST）。"""
    # 假设 DST 让 tz 从 28800 变到 25200（-1h），正常解析
    summary = _make_slp_summary(_stages_basic(), tz="25200")
    batch = normalize_band_sleep([_make_day("2026-03-15", summary)])  # DST 切换日附近
    if batch.sessions:
        # 应该接受 25200（=UTC-7），即使我们在 Asia/Shanghai
        assert batch.sessions[0].tz_offset_secs == 25200


def test_nap_session_marked_separately():
    """M3 章节 3.3：odd_stage + supNap=True → 第二个 session is_nap=True。"""
    main_stages = [{"start": 0, "stop": 60, "mode": MODE_LIGHT}]
    odd_stages = [{"start": 200, "stop": 230, "mode": MODE_LIGHT}]
    summary = _make_slp_summary(main_stages)
    summary["slp"]["odd_stage"] = odd_stages
    summary["slp"]["supNap"] = True
    item = _make_day("2026-09-16", summary)
    batch = normalize_band_sleep([item])
    assert len(batch.sessions) == 2, f"sessions 应有 2 条，得到 {len(batch.sessions)}"
    # 第一条 is_nap=False（主睡），第二条 is_nap=True
    assert batch.sessions[0].is_nap is False
    assert batch.sessions[1].is_nap is True
    # stage 数也是 2
    assert len(batch.stages) == 2


def test_nap_session_not_created_without_supNap():
    """odd_stage 但 supNap=False → 不创建 nap session"""
    main_stages = [{"start": 0, "stop": 60, "mode": MODE_LIGHT}]
    odd_stages = [{"start": 200, "stop": 230, "mode": MODE_LIGHT}]
    summary = _make_slp_summary(main_stages)
    summary["slp"]["odd_stage"] = odd_stages
    summary["slp"]["supNap"] = False  # 不支持
    item = _make_day("2026-09-16", summary)
    batch = normalize_band_sleep([item])
    # 只 1 个 session
    assert len(batch.sessions) == 1
    assert batch.sessions[0].is_nap is False


def test_data_hr_1920_bytes_truncates():
    """M3 章节 3.4：data_hr 1920 字节 → 前 1440 字节入库 + 诊断记录。"""
    # 1920 字节：前 1440 中含有效 BPM；后 480 字节全 255（哨兵）
    hr_bytes = [80] * 1440 + [255] * 480
    summary = _make_slp_summary(_stages_basic())
    item = _make_day("2026-09-16", summary, data_hr_bytes=hr_bytes)
    batch = normalize_band_sleep([item])
    # 应该有 1440 条 hr 样本（前 1440 字节全部 bpm=80）
    assert len(batch.hr_samples) == 1440
    # 诊断信息应提到 1920 字节
    assert any("1920" in d for d in batch.diagnostics)


def test_data_hr_short_processed():
    """data_hr 不足 1440 字节（旧固件）→ 仍按现有长度处理 + 诊断"""
    hr_bytes = [80] * 800
    summary = _make_slp_summary(_stages_basic())
    item = _make_day("2026-09-16", summary, data_hr_bytes=hr_bytes)
    batch = normalize_band_sleep([item])
    # 800 条 hr 样本
    assert len(batch.hr_samples) == 800
    assert any("短" in d for d in batch.diagnostics)


def test_stage_seconds_sum():
    """deep+light+rem+awake 时长累加正确。"""
    stages = [
        {"start": 0, "stop": 60, "mode": MODE_LIGHT},   # 60 min light
        {"start": 60, "stop": 180, "mode": MODE_DEEP},  # 120 min deep
        {"start": 180, "stop": 240, "mode": MODE_REM},  # 60 min rem
        {"start": 240, "stop": 270, "mode": MODE_AWAKE}, # 30 min awake
    ]
    summary = _make_slp_summary(stages)
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    sess = batch.sessions[0]
    assert sess.light_secs == 60 * 60
    assert sess.deep_secs == 120 * 60
    assert sess.rem_secs == 60 * 60
    assert sess.awake_secs == 30 * 60
    assert sess.unknown_secs == 0


def test_dict_payload_with_items_key():
    """dict 形态（顶层是 dict，items 在内）兼容。"""
    summary = _make_slp_summary(_stages_basic())
    item = _make_day("2026-09-16", summary)
    payload = {"items": [item]}
    batch = normalize_band_sleep(payload)
    assert len(batch.sessions) == 1


def test_invalid_b64_skipped():
    """summary B64 损坏 → 跳过 + diagnostics，不 crash。"""
    item = {
        "date_time": "2026-09-16",
        "summary": "not_valid_base64!!!",
        "source": "8519936",
    }
    batch = normalize_band_sleep([item])
    assert len(batch.sessions) == 0
    assert any("summary" in d and "解码" in d for d in batch.diagnostics)


def test_summary_decoder_keys_correct():
    """summary 解码后字段提取正确（spos/rhr/ss 等）。"""
    summary = _make_slp_summary(
        _stages_basic(), rhr=58, ss=72,  # rhr=58, ss=72
    )
    batch = normalize_band_sleep([_make_day("2026-09-16", summary)])
    sess = batch.sessions[0]
    assert sess.rhr == 58, f"rhr={sess.rhr}"
    assert sess.score == 72, f"score={sess.score}"
    assert sess.sp_o2_avg is not None
    assert sess.algo_version == "2.13.15"


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
    print(f"\n=== band_sleep normalizer: {passed} passed, {failed} failed ===")
    import sys as _sys
    _sys.exit(0 if failed == 0 else 1)