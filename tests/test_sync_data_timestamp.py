"""
M7 章节：sync 后展示"数据上传时间" + sync 前自动 refresh 的单元测试。

需求：
  - sync 完成后，stdout 应包含一行 "数据上传时间: YYYY-MM-DD HH:MM:SS (X 单位前)"
  - 数据源是 measurements.ts_ms（毫秒）—— 与 counts()::latest 一致
  - 年龄格式化（_format_age_str）：秒 / 分钟 / 小时 / 天 / 未来

覆盖：
  1. test_format_age_seconds — 30 秒前 → "30 秒前"
  2. test_format_age_minutes — 5 分钟前 → "5 分钟前"
  3. test_format_age_hours — 3.5 小时前 → "3.5 小时前"
  4. test_format_age_days — 2.3 天前 → "2.3 天前"
  5. test_format_age_future — 未来时间 → "未来时间（请检查时区）"
  6. test_sync_output_includes_data_timestamp — mock sync 输出包含"数据上传时间"行
  7. test_sync_output_skips_data_timestamp_when_empty — DB 无 measurements → 不打"数据上传时间"行
  8. test_maybe_refresh_token_runs_subprocess — _maybe_refresh_token_before_sync 调 zepp_oauth.py refresh
  9. test_maybe_refresh_token_handles_exit_2 — refresh exit 2 → 触发 _silent_reauth
  10. test_maybe_refresh_token_does_not_abort_on_exit_1 — refresh exit 1 → 不 abort，让 sync 继续

设计原则（与 M5/M6 测试一致）：
  - mock `fetch_and_normalize` / `subprocess.run` 控制 sync 的输入/输出
  - 用 tmp_path 隔离 secrets dir
  - 不破坏现有 463 测试
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch, MagicMock

# 让 tests/ 能 import scripts/
TESTS_DIR = Path(__file__).parent
sys.path.insert(0, str(TESTS_DIR.parent / "scripts"))

import pull_to_sqlite as pts  # noqa: E402
from time_utils import epoch_now_ms  # noqa: E402


# ===== helpers =====

def _make_home_with_token(tmp: Path) -> Path:
    """构造一个 ~/.zepp-data/.secrets/token.json + token 写入 DATA_DIR"""
    data_dir = tmp / ".zepp-data"
    secrets_dir = data_dir / ".secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    token = {
        "user_id": "u_test_m7",
        "app_token": "fake_app_token_for_m7_test",
        "login_token": "fake_login_token_for_m7_test",
        "region_host": "https://api-mifit-cn3.zepp.com",
        "extracted_at": "2026-09-25T00:00:00Z",
    }
    (secrets_dir / "token.json").write_text(json.dumps(token))
    # monkeypatch pull_to_sqlite 模块的路径常量
    pts.DATA_DIR = data_dir
    pts.SECRETS_DIR = secrets_dir
    pts.SECRETS_FILE = secrets_dir / "token.json"
    pts.DEFAULT_DB = data_dir / "zepp.db"
    # 重设 _REAUTH_LOG_DIR / _REAUTH_LOG_FILE 让 log 也写到沙箱
    pts._REAUTH_LOG_DIR = data_dir / "log"
    pts._REAUTH_LOG_FILE = pts._REAUTH_LOG_DIR / "reauth.log"
    return data_dir


class _HomeSandbox:
    """monkeypatch pts 路径常量 + 准备 token.json + 把 reauth log 切到沙箱。"""
    def __init__(self, tmp):
        self.tmp = tmp
        self._const_backup = None

    def __enter__(self):
        self._const_backup = {
            "DATA_DIR": pts.DATA_DIR,
            "SECRETS_DIR": pts.SECRETS_DIR,
            "SECRETS_FILE": pts.SECRETS_FILE,
            "DEFAULT_DB": pts.DEFAULT_DB,
            "_REAUTH_LOG_DIR": pts._REAUTH_LOG_DIR,
            "_REAUTH_LOG_FILE": pts._REAUTH_LOG_FILE,
        }
        _make_home_with_token(self.tmp)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for k, v in self._const_backup.items():
            setattr(pts, k, v)


def _make_args(db_path: str, days: int = 7):
    class Args:
        pass
    a = Args()
    a.db = db_path
    a.days = days
    return a


def _init_db(db_path: str) -> None:
    pts.init_db(Path(db_path))


def _make_success_result(n: int = 5, last_ts_ms: int | None = None):
    return {
        "records": n, "diags": 0,
        "status": "available",
        "raw_count": n,
        "last_ts_ms": last_ts_ms,
    }


def _seed_measurement(db_path: str, ts_ms: int) -> None:
    """向 measurements 表插一条假数据（让 MAX(ts_ms) 有值）。"""
    conn = sqlite3.connect(db_path)
    try:
        # schema 在 init_db 已经创建；直接 insert
        conn.execute("""
            INSERT INTO measurements (
                date, ts_ms, stream, metric, value, unit, source_scope,
                raw_source_key, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "2026-09-25", ts_ms, "daily", "steps", 1585, "count",
            "real_data", "test_seed", "u_test_m7",
        ))
        conn.commit()
    finally:
        conn.close()


# ===== 用例 1：_format_age_str — 秒 =====

def test_format_age_seconds():
    """30 秒前 → '30 秒前'。"""
    assert pts._format_age_str(30) == "30 秒前"
    # 边界：0 秒 → "0 秒前"
    assert pts._format_age_str(0) == "0 秒前"
    # 边界：59 秒 → "59 秒前"
    assert pts._format_age_str(59) == "59 秒前"


# ===== 用例 2：_format_age_str — 分钟 =====

def test_format_age_minutes():
    """5 分钟前 → '5 分钟前'。"""
    assert pts._format_age_str(5 * 60) == "5 分钟前"
    # 边界：60 秒 = 1 分钟
    assert pts._format_age_str(60) == "1 分钟前"
    # 边界：59 分钟
    assert pts._format_age_str(59 * 60) == "59 分钟前"


# ===== 用例 3：_format_age_str — 小时 =====

def test_format_age_hours():
    """3.5 小时前 → '3.5 小时前'（保留 1 位小数）。"""
    assert pts._format_age_str(3.5 * 3600) == "3.5 小时前"
    # 边界：3600 秒 = 1 小时
    assert pts._format_age_str(3600) == "1.0 小时前"
    # 8 小时
    assert pts._format_age_str(8 * 3600) == "8.0 小时前"


# ===== 用例 4：_format_age_str — 天 =====

def test_format_age_days():
    """2.3 天前 → '2.3 天前'。"""
    assert pts._format_age_str(2.3 * 86400) == "2.3 天前"
    # 边界：86400 秒 = 1 天
    assert pts._format_age_str(86400) == "1.0 天前"
    # 7 天
    assert pts._format_age_str(7 * 86400) == "7.0 天前"


# ===== 用例 5：_format_age_str — 未来时间 =====

def test_format_age_future():
    """未来时间（age < 0）→ '未来时间（请检查时区）'。"""
    assert pts._format_age_str(-1) == "未来时间（请检查时区）"
    assert pts._format_age_str(-3600) == "未来时间（请检查时区）"
    # 边界：0 不是未来
    assert pts._format_age_str(0) != "未来时间（请检查时区）"


# ===== 用例 6：sync 输出包含 "数据上传时间" 行 =====

def test_sync_output_includes_data_timestamp(tmp_path):
    """mock sync → DB 里有 measurements → stdout 应包含 "数据上传时间: ..." 行。

    设计要点：
      - 用 mock 让 fetch_and_normalize 不真跑 Zepp API（避免依赖网络/真实 token）
      - 但 DB 必须有 measurements 数据（让 MAX(ts_ms) 返非空），所以测试前手动 seed 一条
      - 用 recent ts_ms（10 分钟前）→ 输出应包含 "10 分钟前"（允许 9-11 分钟 — 测试运行有时间延迟）
    """
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)
    # seed 一条 10 分钟前的 measurement
    seed_ts = epoch_now_ms() - 10 * 60 * 1000
    _seed_measurement(db_path, seed_ts)

    def fake_fetch(*a, **kw):
        return _make_success_result(n=5)

    with _HomeSandbox(tmp_path):
        with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch):
            # mock _maybe_refresh_token_before_sync 直接返回 True（避免子进程调用）
            with patch.object(pts, "_maybe_refresh_token_before_sync", return_value=True):
                args = _make_args(db_path, days=7)
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = pts.cmd_sync(args)
    out = buf.getvalue()

    assert rc == 0, f"cmd_sync 应返 0，得到 {rc}"
    # 关键断言 1：输出包含 "数据上传时间:" 行
    assert "数据上传时间:" in out, (
        f"输出必须包含 '数据上传时间:' 行；实际输出:\n{out}"
    )
    # 关键断言 2：包含 "X 分钟前"（10 分钟前的 seed — 允许 9-11 分钟的舍入）
    import re
    m = re.search(r"(\d+) 分钟前", out)
    assert m is not None, f"输出必须包含 'N 分钟前' 格式；实际输出:\n{out}"
    n_minutes = int(m.group(1))
    assert 9 <= n_minutes <= 11, (
        f"age 应约 10 分钟（9-11 容忍测试运行耗时），得到 {n_minutes}；输出:\n{out}"
    )
    # 关键断言 3：包含 YYYY-MM-DD HH:MM:SS 格式的时间
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", out), (
        f"输出必须包含时间戳格式 YYYY-MM-DD HH:MM:SS；实际输出:\n{out}"
    )
    # 关键断言 4：happy path 不打印 CRITICAL / WARNING
    assert "🔴" not in out and "🟡" not in out, (
        f"happy path 不应有 CRITICAL/WARNING；实际输出:\n{out}"
    )
    # 关键断言 5：sync 主流程不变（"本次新增/更新 N 条 records" 仍存在）
    expected = 5 * len(pts.STREAMS)
    assert f"本次新增/更新 {expected} 条 records" in out, (
        f"happy path 应显示 {expected} 条；实际输出:\n{out}"
    )


# ===== 用例 7：DB 无 measurements → 不打 "数据上传时间" 行 =====

def test_sync_output_skips_data_timestamp_when_empty(tmp_path):
    """DB 里没有任何 measurements（全新 DB）→ 不打 '数据上传时间:' 行（避免噪音）。

    设计要点：
      - 这是 _compute_data_upload_time 的 None 分支
      - 用户视角：DB 是新的，sync 跑完也没数据，"数据上传时间" 没意义
    """
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)
    # 注意：不 seed 任何 measurement

    def fake_fetch(*a, **kw):
        return _make_success_result(n=3)

    with _HomeSandbox(tmp_path):
        with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch):
            with patch.object(pts, "_maybe_refresh_token_before_sync", return_value=True):
                args = _make_args(db_path, days=7)
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = pts.cmd_sync(args)
    out = buf.getvalue()

    assert rc == 0
    # 关键断言：DB 为空时不打 "数据上传时间" 行（避免噪音）
    assert "数据上传时间:" not in out, (
        f"DB 无数据时不应显示 '数据上传时间'；实际输出:\n{out}"
    )
    # 但 sync 仍正常完成
    expected = 3 * len(pts.STREAMS)
    assert f"本次新增/更新 {expected} 条 records" in out, (
        f"sync 应正常完成；实际输出:\n{out}"
    )


# ===== 用例 8：_maybe_refresh_token_before_sync 调 zepp_oauth.py refresh =====

def test_maybe_refresh_token_runs_subprocess(tmp_path):
    """_maybe_refresh_token_before_sync 真的调 subprocess 跑 zepp_oauth.py refresh。

    设计要点：
      - mock subprocess.run 让 refresh 返 exit 0
      - 断言 subprocess.run 被调用，且 argv 包含 'refresh'
      - 断言返回 True（让 sync 继续）
    """
    with _HomeSandbox(tmp_path):
        fake_proc = MagicMock()
        fake_proc.returncode = 0

        with patch.object(pts.subprocess, "run", return_value=fake_proc) as mock_run:
            ok = pts._maybe_refresh_token_before_sync()

    assert ok is True, f"refresh exit 0 应让 _maybe_refresh_token_before_sync 返 True，得到 {ok}"
    # 关键断言：subprocess.run 被调用了一次
    assert mock_run.call_count == 1, (
        f"subprocess.run 应被调用 1 次，得到 {mock_run.call_count}"
    )
    # 关键断言：argv 里包含 'refresh'
    argv = mock_run.call_args[0][0]
    assert "refresh" in argv, (
        f"argv 应包含 'refresh' 子命令；argv={argv}"
    )
    # 关键断言：argv 包含 zepp_oauth.py 路径
    assert any("zepp_oauth" in str(a) for a in argv), (
        f"argv 应指向 zepp_oauth.py；argv={argv}"
    )


# ===== 用例 9：refresh exit 2 → 触发 _silent_reauth =====

def test_maybe_refresh_token_handles_exit_2(tmp_path):
    """refresh exit 2（login_token 过期）→ _silent_reauth 被调用，不 abort sync。

    设计要点：
      - mock subprocess.run 让 refresh 返 exit 2
      - mock _silent_reauth 记录被调用
      - 断言 _silent_reauth 被调一次，_maybe_refresh_token_before_sync 仍返 True（继续 sync）
    """
    with _HomeSandbox(tmp_path):
        fake_proc = MagicMock()
        fake_proc.returncode = 2  # exit 2 = login_token 过期

        reauth_called = {"count": 0}

        def fake_silent_reauth(reason: str = "") -> bool:
            reauth_called["count"] += 1
            return True

        with patch.object(pts.subprocess, "run", return_value=fake_proc):
            with patch.object(pts, "_silent_reauth", side_effect=fake_silent_reauth):
                ok = pts._maybe_refresh_token_before_sync()

    assert ok is True, f"refresh exit 2 不应 abort sync（仍要继续），得到 {ok}"
    assert reauth_called["count"] == 1, (
        f"exit 2 必须触发 _silent_reauth；得到 {reauth_called['count']} 次"
    )


# ===== 用例 10：refresh exit 1 → 不 abort，让 M6 silent_reauth 兜底 =====

def test_maybe_refresh_token_does_not_abort_on_exit_1(tmp_path):
    """refresh exit 1（本地问题：token 文件不存在 / 缺字段）→ 不调 _silent_reauth，不 abort。

    设计要点：
      - refresh exit 1 意味着本地状态问题，不是 token 真的过期
      - 让 sync 继续走（不要双层兜底逻辑重叠）—— M6 silent_reauth 在 sync 跑完后会自动兜底
    """
    with _HomeSandbox(tmp_path):
        fake_proc = MagicMock()
        fake_proc.returncode = 1  # exit 1 = 本地问题

        reauth_called = {"count": 0}

        def fake_silent_reauth(reason: str = "") -> bool:
            reauth_called["count"] = reauth_called.get("count", 0) + 1
            return True

        with patch.object(pts.subprocess, "run", return_value=fake_proc):
            with patch.object(pts, "_silent_reauth", side_effect=fake_silent_reauth):
                ok = pts._maybe_refresh_token_before_sync()

    assert ok is True, f"refresh exit 1 不应 abort sync，得到 {ok}"
    # 关键断言：exit 1 不直接触发 _silent_reauth（让 sync 阶段 M6 silent reauth 兜底）
    assert reauth_called["count"] == 0, (
        f"refresh exit 1 不应直接触发 _silent_reauth；得到 {reauth_called['count']} 次"
    )


# ===== 用例 11（bonus）：_compute_data_upload_time 工具函数直接测 =====

def test_compute_data_upload_time_with_data(tmp_path):
    """DB 有 measurement → 返 (timestamp_str, age_str, None)。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)
    seed_ts = epoch_now_ms() - 5 * 60 * 1000  # 5 分钟前
    _seed_measurement(db_path, seed_ts)

    db = pts.init_db(Path(db_path))
    ts_str, age_str, err = pts._compute_data_upload_time(db)
    db.close()

    assert err is None, f"不应有错误：{err}"
    assert ts_str is not None, "ts_str 应有值"
    assert age_str is not None, "age_str 应有值"
    # 5 分钟前 → "5 分钟前"（允许 5-6 分钟的舍入，测试运行有时间延迟）
    import re
    m = re.match(r"(\d+) 分钟前", age_str)
    assert m is not None, f"age_str 应是 'N 分钟前' 格式，得到 {age_str!r}"
    n = int(m.group(1))
    assert 4 <= n <= 6, f"age 应约 5 分钟（4-6 容忍），得到 {n}"


# ===== 用例 12（bonus）：_compute_data_upload_time — DB 空 =====

def test_compute_data_upload_time_empty_db(tmp_path):
    """DB 没有 measurement → 返 (None, None, error_msg)。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)
    # 不 seed

    db = pts.init_db(Path(db_path))
    ts_str, age_str, err = pts._compute_data_upload_time(db)
    db.close()

    assert ts_str is None, f"空 DB 应返 None ts_str，得到 {ts_str}"
    assert age_str is None, f"空 DB 应返 None age_str，得到 {age_str}"
    assert err is not None, f"空 DB 应有 err 消息"


# ===== 用例 13（bonus）：_maybe_refresh_token_before_sync — subprocess.run 超时 =====

def test_maybe_refresh_token_handles_timeout(tmp_path):
    """subprocess.run 抛 TimeoutExpired → 不 abort，让 sync 继续（M6 兜底）。"""
    with _HomeSandbox(tmp_path):
        def fake_run(*a, **kw):
            raise pts.subprocess.TimeoutExpired(cmd=["zepp_oauth.py", "refresh"], timeout=30)
        with patch.object(pts.subprocess, "run", side_effect=fake_run):
            ok = pts._maybe_refresh_token_before_sync()

    assert ok is True, f"refresh 超时不应 abort sync，得到 {ok}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))