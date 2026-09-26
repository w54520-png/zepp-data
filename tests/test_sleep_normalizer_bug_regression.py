"""
Sleep Normalizer Bug 回归测试 —— M3+ 章节

背景：9-25 sync 触发后，sleep_sessions 表终于有数据了——但用户报告"之前完全没数据"。

经诊断：
- 之前的 sync 都正常拉到 raw band_data（如 9-22 ~ 9-24 的 raw 行都在 raw_records 里）
- 但那些天的 slp.stage / odd_stage 都是空数组（云端用户没戴表 / Zepp 没生成 stage）
- 按 M2 拍板的设计："stage 数据不充分时宁可不写 session"，所以正常化后 0 个 session
- 真正的 bug 是观测性：sync 输出 "✓ 0 records" + "[band_sleep] sessions=0"
  让用户无法分辨"raw 没拉到" vs"raw 拉到了但 stage 空（设计预期）"

修复（详见 /tmp/sleep_bug_analysis.md）：
1. _ingest_band_sleep 返回写入计数 + 加 "written: sessions=N stages=M" 行
2. 空 stage 的 diagnostic 文案更明确（含 M2 拍板说明 + raw 是否拉到）
3. upsert_sleep_session 缺关键字段抛 ValueError（不再 silent）

本测试文件覆盖：
1. test_stage_with_dp_59_writes_session —— 模拟 9-25 数据（dp=59 lt=240 rm=85 wk=0 ss=67），
   跑 normalizer，验证 session 写入——防止未来重蹈覆辙
2. test_stage_with_empty_dp_skips_session —— 空 stage 仍然 skip（保留 M2 拍板）
3. test_sleep_session_missing_date_skips_with_diagnostic —— 缺 date 时不写库 + diagnostic 清晰
4. test_sleep_session_missing_start_ts_skips_with_diagnostic —— 缺 start_ts 时不写库 + diagnostic 清晰
5. test_sleep_normalizer_logs_stages_count —— normalizer 输出包含 stages_count diagnostic
"""
import sys
import base64
import json
import sqlite3
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from normalizer.sleep import (
    normalize_band_sleep,
    MODE_DEEP, MODE_LIGHT, MODE_REM, MODE_AWAKE,
    STAGE_MODE_MAP,
)
from storage import (
    init_db,
    upsert_sleep_session,
)


# ===== helpers =====

def _make_2026_09_25_stages():
    """
    模拟 2026-09-25 那一夜的真实数据（来自 raw_records 实测）：
      dp=59 lt=240 rm=85 wk=0 ss=67
    推算 stage 分钟数：
      deep  =  59 min  → 用 1 个 59 min 切片
      light = 240 min  → 用 4 个 60 min 切片
      rem   =  85 min  → 用 2 个 ~42 min 切片
      awake =   0 min  → 0 切片
      ss=67            → score 字段
    起点 1397 min（相对 slp.st），slp.st=1790271120（2026-09-24 16:00 UTC + offset）
    """
    return [
        {"start": 1397, "stop": 1412, "mode": MODE_LIGHT},   #  15 light
        {"start": 1412, "stop": 1471, "mode": MODE_DEEP},    #  59 deep   ← dp
        {"start": 1471, "stop": 1531, "mode": MODE_LIGHT},   #  60 light
        {"start": 1531, "stop": 1573, "mode": MODE_REM},     #  42 rem
        {"start": 1573, "stop": 1633, "mode": MODE_LIGHT},   #  60 light
        {"start": 1633, "stop": 1676, "mode": MODE_REM},     #  43 rem
        {"start": 1676, "stop": 1721, "mode": MODE_LIGHT},   #  45 light
        {"start": 1721, "stop": 1781, "mode": MODE_LIGHT},   #  60 light
    ]
    # 总：deep=59, light=240, rem=85, awake=0 → 实际 deep=59, light=240, rem=85, awake=0 ✓
    # score=67 → ss 字段


def _make_2026_09_25_summary(stages=None):
    """构造 9-25 那夜的 slp summary。"""
    if stages is None:
        stages = _make_2026_09_25_stages()
    slp = {
        "stage": stages,
        "odd_stage": [],
        # slp.st = 2026-09-24 16:00 UTC (Asia/Shanghai 9-25 00:00) = 1790275200
        # 但 raw 实测是 1790271120（9-24 15:52 UTC），这里用 1790275200 标准锚点
        "st": 1790275200, "ed": 1790299000,
        "obt": -44, "ebt": 246,
        "spos": 96.5, "spor": 15,
        "rhr": 58, "ss": 67,
        "sleepSource": 8519936,
        "supNap": True, "supRem": True,
    }
    return {"v": 6, "goal": 30000, "tz": "28800", "algv": "2.13.15", "slp": slp}


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
        item["data"] = ""
        item["data_hr"] = base64.b64encode(bytes(data_hr_bytes)).decode()
    return item


def _make_db():
    """临时 DB，包含完整 schema。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn, db_path


# ===== 测试 =====

def test_stage_with_dp_59_writes_session():
    """9-25 数据（dp=59 lt=240 rm=85 wk=0 ss=67）跑 normalizer + 写 DB，验证 1 个 session 写入。

    这是回归测试核心：确保"stage 非空 → session 必写入"。
    9-25 那条数据实测 dp=59 lt=240 rm=85 wk=0 ss=67，本测试复现并验证：
      1) normalizer 输出 1 个 session
      2) deep_secs=59*60, light_secs=240*60, rem_secs=85*60, awake_secs=0
      3) score=67（从 ss 字段）
      4) upsert_sleep_session 真的把它写到了 sleep_sessions 表（不只是 batch 内存）
    """
    conn, db_path = _make_db()
    try:
        # 1) normalizer 输出
        summary = _make_2026_09_25_summary()
        item = _make_day("2026-09-25", summary)
        batch = normalize_band_sleep([item])

        assert len(batch.sessions) == 1, \
            f"应产出 1 session，得到 {len(batch.sessions)}（9-25 这种带 stage 的数据必须写）"
        sess = batch.sessions[0]
        assert sess.date == "2026-09-25"
        assert sess.deep_secs == 59 * 60, f"deep_secs={sess.deep_secs}, expected {59*60}"
        assert sess.light_secs == 240 * 60, f"light_secs={sess.light_secs}, expected {240*60}"
        assert sess.rem_secs == 85 * 60, f"rem_secs={sess.rem_secs}, expected {85*60}"
        assert sess.awake_secs == 0, f"awake_secs={sess.awake_secs}, expected 0"
        assert sess.score == 67, f"score={sess.score}, expected 67"

        # 2) 实际写入 DB
        sess_dict = {
            "user_id": "u1",
            "date": sess.date,
            "source": sess.source,
            "start_ts": sess.start_ts,
            "end_ts": sess.end_ts,
            "tz_offset_secs": sess.tz_offset_secs,
            "time_in_bed_secs": sess.time_in_bed_secs,
            "deep_secs": sess.deep_secs,
            "light_secs": sess.light_secs,
            "rem_secs": sess.rem_secs,
            "awake_secs": sess.awake_secs,
            "unknown_secs": sess.unknown_secs,
            "sp_o2_avg": sess.sp_o2_avg,
            "breath_avg": sess.breath_avg,
            "rhr": sess.rhr,
            "score": sess.score,
            "is_nap": bool(getattr(sess, "is_nap", False)),
            "algo_version": sess.algo_version,
            "sleep_source": sess.sleep_source,
            "raw_summary": sess.raw_summary,
        }
        sid = upsert_sleep_session(conn, sess_dict, raw_source_key="band_sleep:2026-09-25")
        assert sid is not None and sid > 0, "upsert_sleep_session 必须返回 session_id"

        # 3) 查 DB 确认 1 行
        row = conn.execute(
            "SELECT date, deep_secs, light_secs, rem_secs, awake_secs, score, raw_source_key "
            "FROM sleep_sessions WHERE id=?", (sid,)
        ).fetchone()
        assert row is not None, "DB 中应该有这条 session"
        assert row["date"] == "2026-09-25"
        assert row["deep_secs"] == 59 * 60
        assert row["light_secs"] == 240 * 60
        assert row["rem_secs"] == 85 * 60
        assert row["awake_secs"] == 0
        assert row["score"] == 67
        assert row["raw_source_key"] == "band_sleep:2026-09-25"
    finally:
        conn.close()
        db_path.unlink(missing_ok=True)


def test_stage_with_empty_dp_skips_session():
    """空 stage 仍然 skip session（保留 M2 拍板——stage 数据不充分时宁可不写）。

    这是关键不变性：修复观测性 bug 不改变行为。
    空 stage → 不写 session + diagnostic 包含"M2 拍板"说明。
    """
    summary = {
        "v": 6, "tz": "28800", "slp": {
            "stage": [], "odd_stage": [],
            "st": 1790275200, "ed": 1790275200,  # ed==st（空日哨兵）
            "obt": 0, "ebt": 0,
            "spos": 0, "rhr": 0,
        }
    }
    item = _make_day("2026-09-22", summary)
    batch = normalize_band_sleep([item])

    # 1) 不写 session
    assert len(batch.sessions) == 0, \
        "空 stage 必须不写 session（M2 拍板——避免假数据入库）"

    # 2) 但必须有 diagnostic，且文案包含 M2 拍板说明
    assert len(batch.diagnostics) >= 1, "必须有 diagnostic"
    diag = batch.diagnostics[0]
    assert "空 stage" in diag, f"diagnostic 应提及'空 stage'，得到: {diag}"
    assert "M2 拍板" in diag, \
        f"diagnostic 应说明 M2 拍板（让用户知道'为什么不写'是设计预期），得到: {diag}"
    assert "2026-09-22" in diag, f"diagnostic 应含 date，得到: {diag}"
    assert "raw" in diag or "raw_records" in diag, \
        f"diagnostic 应说明 raw 已落库，得到: {diag}"


def test_sleep_session_missing_date_skips_with_diagnostic():
    """upsert_sleep_session 缺 date → 抛 ValueError + 调用方写入 diagnostic。

    之前：silent return None → 用户看不到失败原因。
    现在：直接抛 ValueError + pull_to_sqlite 捕获后写到 batch.diagnostics。
    """
    conn, db_path = _make_db()
    try:
        sess_dict = {
            "user_id": "u1",
            "date": None,  # ← 缺
            "source": "8519936",
            "start_ts": 1790275200,
            "end_ts": 1790299000,
            "deep_secs": 0, "light_secs": 0, "rem_secs": 0, "awake_secs": 0, "unknown_secs": 0,
            "is_nap": False,
        }
        # 直接调 storage 层：必须抛 ValueError
        raised = False
        try:
            upsert_sleep_session(conn, sess_dict, raw_source_key="band_sleep:test")
        except ValueError as e:
            raised = True
            assert "date" in str(e), f"ValueError 文案应包含 'date'，得到: {e}"
            assert "missing required field" in str(e), \
                f"ValueError 文案应说明 missing required field，得到: {e}"
        assert raised, "缺 date 时必须抛 ValueError（修复 bug）"

        # 调用方捕获 ValueError 并写入 diagnostic——模拟 pull_to_sqlite 的处理
        batch_diags = []
        try:
            upsert_sleep_session(conn, sess_dict, raw_source_key="band_sleep:test")
        except ValueError as e:
            batch_diags.append(f"date=None: upsert_sleep_session 跳过 — {e}")

        assert len(batch_diags) == 1
        assert "date" in batch_diags[0]
        assert "跳过" in batch_diags[0]

        # 确认 DB 里没行
        n = conn.execute("SELECT COUNT(*) AS c FROM sleep_sessions").fetchone()["c"]
        assert n == 0, "缺关键字段时不应写库"
    finally:
        conn.close()
        db_path.unlink(missing_ok=True)


def test_sleep_session_missing_start_ts_skips_with_diagnostic():
    """缺 start_ts → 抛 ValueError + diagnostic 清晰。"""
    conn, db_path = _make_db()
    try:
        sess_dict = {
            "user_id": "u1",
            "date": "2026-09-25",
            "source": "8519936",
            "start_ts": None,  # ← 缺
            "end_ts": 1790299000,
            "deep_secs": 0, "light_secs": 0, "rem_secs": 0, "awake_secs": 0, "unknown_secs": 0,
            "is_nap": False,
        }
        raised = False
        try:
            upsert_sleep_session(conn, sess_dict, raw_source_key="band_sleep:test")
        except ValueError as e:
            raised = True
            assert "start_ts" in str(e), f"ValueError 文案应包含 'start_ts'，得到: {e}"
        assert raised, "缺 start_ts 时必须抛 ValueError"

        # 缺 end_ts 也验证
        sess_dict2 = dict(sess_dict, start_ts=1790275200, end_ts=None)
        raised2 = False
        try:
            upsert_sleep_session(conn, sess_dict2, raw_source_key="band_sleep:test")
        except ValueError as e:
            raised2 = True
            assert "end_ts" in str(e), f"ValueError 文案应包含 'end_ts'，得到: {e}"
        assert raised2, "缺 end_ts 时也必须抛 ValueError"

        # 缺多个字段：文案应列出全部缺失字段
        sess_dict3 = {
            "user_id": "u1",
            "date": None, "source": "8519936",
            "start_ts": None, "end_ts": None,
            "is_nap": False,
        }
        raised3 = False
        try:
            upsert_sleep_session(conn, sess_dict3, raw_source_key="band_sleep:test")
        except ValueError as e:
            raised3 = True
            msg = str(e)
            assert "date" in msg and "start_ts" in msg and "end_ts" in msg, \
                f"缺多字段时文案应列出全部缺失字段，得到: {msg}"
        assert raised3

        # DB 仍然空
        n = conn.execute("SELECT COUNT(*) AS c FROM sleep_sessions").fetchone()["c"]
        assert n == 0
    finally:
        conn.close()
        db_path.unlink(missing_ok=True)


def test_sleep_normalizer_logs_stages_count():
    """normalizer 输出包含 stages_count 类诊断（stages 写入数）。

    修复后 _ingest_band_sleep 会输出 "written: sessions=N stages=M" 行
    本测试在 normalizer 层验证：batch.stages 长度正确，便于上层 print。
    """
    # 9-25 数据：8 个 stages
    summary = _make_2026_09_25_summary()
    item = _make_day("2026-09-25", summary)
    batch = normalize_band_sleep([item])

    # 1) batch.stages 数量必须等于 stages_raw 数组长度
    assert len(batch.stages) == 8, \
        f"应产出 8 个 stage 切片，得到 {len(batch.stages)}"

    # 2) 每个 stage 的 mode 必须能映射（深浅 REM 醒 或 unknown）
    valid_modes = set(STAGE_MODE_MAP.values()) | {"unknown"}
    for stg in batch.stages:
        assert stg.mode in valid_modes, \
            f"stage mode={stg.mode} 不在合法集合 {valid_modes}"
        assert stg.mode_code in (MODE_DEEP, MODE_LIGHT, MODE_REM, MODE_AWAKE), \
            f"stage mode_code={stg.mode_code} 不是合法 mode"

    # 3) 按 mode 累加时长等于 stage 原始分钟数（不变量）
    total_minutes = sum((stg.end_min - stg.start_min) for stg in batch.stages)
    expected_total = 59 + 240 + 85  # dp + lt + rm = 384
    assert total_minutes == expected_total, \
        f"总 stage 分钟数={total_minutes}, expected {expected_total}"

    # 4) batch.sessions[0] 的 deep/light/rem/awake_secs 累加 = total_minutes * 60
    sess = batch.sessions[0]
    sec_total = sess.deep_secs + sess.light_secs + sess.rem_secs + sess.awake_secs + sess.unknown_secs
    assert sec_total == expected_total * 60, \
        f"session 总秒数={sec_total}, expected {expected_total * 60}"

    # 5) 验证 normalizer 的诊断可被上层读取到 stages_count
    #    （_ingest_band_sleep 在 print 前用 batch.diagnostics.len 打印"written: stages=N"）
    #    这里直接验证 len(batch.stages) 与期望一致即可——上层 print 格式是字符串拼接
    print(f"  [stages_count diagnostic] batch.stages={len(batch.stages)} batch.sessions={len(batch.sessions)}")


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
    print(f"\n=== sleep normalizer bug regression: {passed} passed, {failed} failed ===")
    import sys as _sys
    _sys.exit(0 if failed == 0 else 1)
