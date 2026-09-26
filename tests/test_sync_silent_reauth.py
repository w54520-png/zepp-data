"""
M6 章节：401 自动静默续期单元测试。

需求：sync 跑完后扫到 critical_auth（401）→ 静默调 zepp_oauth.py refresh / login，
重跑 sync。用户视角看不到 401（不输出到 stdout）。

覆盖：
  1. test_reauth_triggered_on_401 — 第一次 sync 返 401 → 触发 _silent_reauth → 重跑 → 第二次成功
  2. test_no_reauth_on_normal_failure — sync 返其他错误（非 critical_auth）→ 不触发 reauth
  3. test_reauth_failed_still_returns_critical — reauth 失败时 → CRITICAL 块（兜底）
  4. test_reauth_logged_to_file_not_stdout — reauth 信息写 log file，stdout 完全静默
  5. test_max_one_reauth_per_sync — 401 → reauth → 仍 401 → 不再重试（避免无限循环）
  6. test_reauth_disabled_by_env — ZEPP_NO_REAUTH=1 时不触发 reauth（debug 友好）
  7. test_reauth_skips_when_no_critical_error — 没有 401 时 _silent_reauth 不会被调用

设计原则（与 test_sync_error_visibility 一致）：
  - mock `_silent_reauth`（不调真实 OAuth）
  - mock `fetch_and_normalize` 控制 sync 的输入/输出
  - 用 tmp_path 隔离 secrets dir
  - 不破坏现有 419 测试
"""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch, MagicMock

# 让 tests/ 能 import scripts/
TESTS_DIR = Path(__file__).parent
sys.path.insert(0, str(TESTS_DIR.parent / "scripts"))

import pull_to_sqlite as pts  # noqa: E402


# ===== helpers =====

def _make_home_with_token(tmp: Path) -> Path:
    """构造一个 ~/.zepp-data/.secrets/token.json + token 写入 DATA_DIR"""
    data_dir = tmp / ".zepp-data"
    secrets_dir = data_dir / ".secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    token = {
        "user_id": "u_test_silent",
        "app_token": "fake_app_token_for_silent_test",
        "login_token": "fake_login_token_for_silent_test",
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


def _make_401_result():
    return {
        "records": 0, "diags": 0,
        "status": "fetch_error", "error": "HTTP 401: Unauthorized",
        "error_category": pts._ERROR_CATEGORY_CRITICAL_AUTH,
        "error_type": "NeedsReauth",
    }


def _make_success_result(n: int = 5):
    return {
        "records": n, "diags": 0,
        "status": "available",
        "raw_count": n,
        "last_ts_ms": None,
    }


# ===== 用例 1：401 → silent reauth → 重跑成功 =====

def test_reauth_triggered_on_401(tmp_path):
    """第一次 sync 返 401 → 触发 _silent_reauth → 重跑 → 第二次成功 → 返回 result2。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    call_count = {"n": 0}

    def fake_fetch(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] <= len(pts.STREAMS):
            # 第一次 sync：所有 stream 都返 401
            return _make_401_result()
        # 第二次 sync：所有 stream 都成功，返 10 条 records
        return _make_success_result(n=10)

    reauth_called = {"count": 0}

    def fake_silent_reauth(reason: str = "401 from sync") -> bool:
        reauth_called["count"] += 1
        return True

    with _HomeSandbox(tmp_path):
        with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch):
            with patch("pull_to_sqlite._maybe_refresh_token_before_sync", return_value=True):
                with patch("pull_to_sqlite._silent_reauth", side_effect=fake_silent_reauth):
                    args = _make_args(db_path, days=7)
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = pts.cmd_sync(args)
    out = buf.getvalue()

    assert rc == 0, f"cmd_sync 应返 0，得到 {rc}"
    # 关键断言 1：_silent_reauth 被调用了 1 次
    assert reauth_called["count"] == 1, (
        f"401 触发后 _silent_reauth 必须被调用 1 次，得到 {reauth_called['count']}"
    )
    # 关键断言 2：fetch_and_normalize 被调用了 2 * len(STREAMS) 次（两次 sync）
    assert call_count["n"] == 2 * len(pts.STREAMS), (
        f"两次 sync 应调 {2*len(pts.STREAMS)} 次 fetch_and_normalize，得到 {call_count['n']}"
    )
    # 关键断言 3：第二次 sync 成功 — 本次新增/更新 N 条 records（N = 10 * len(STREAMS)）
    expected = 10 * len(pts.STREAMS)
    assert f"本次新增/更新 {expected} 条 records" in out, (
        f"重跑成功应显示 {expected} 条，实际输出:\n{out}"
    )
    # 关键断言 4：reauth 不输出到 stdout（用户看不到）
    #    CRITICAL 块不该出现（因为 retry 后没 critical_auth）
    assert "🔴 CRITICAL" not in out, (
        f"silent reauth 成功后不应再打 CRITICAL 块；实际输出:\n{out}"
    )


# ===== 用例 2：非 401 错误不触发 reauth =====

def test_no_reauth_on_normal_failure(tmp_path):
    """sync 返其他错误（network / unavailable）→ 不触发 _silent_reauth。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    def fake_fetch_network(*a, **kw):
        # network 错误（不是 critical_auth）
        return {
            "records": 0, "diags": 0,
            "status": "fetch_error", "error": "URLError: temporary failure",
            "error_category": pts._ERROR_CATEGORY_NETWORK,
            "error_type": "NetworkError",
        }

    reauth_called = {"count": 0}

    def fake_silent_reauth(reason: str = "401 from sync") -> bool:
        reauth_called["count"] += 1
        return True  # 假装成功 — 不应该被调用

    with _HomeSandbox(tmp_path):
        with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch_network):
            with patch("pull_to_sqlite._maybe_refresh_token_before_sync", return_value=True):
                with patch("pull_to_sqlite._silent_reauth", side_effect=fake_silent_reauth):
                    args = _make_args(db_path, days=7)
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                            rc = pts.cmd_sync(args)
                buf = io.StringIO()
                with redirect_stdout(buf):
                        rc = pts.cmd_sync(args)
    out = buf.getvalue()

    assert rc == 0
    # 关键断言 1：_silent_reauth 没被调用（network 错误不触发 reauth）
    assert reauth_called["count"] == 0, (
        f"network 错误不应触发 _silent_reauth，得到 {reauth_called['count']} 次"
    )
    # 关键断言 2：CRITICAL 块不出现（这是 network 错误，不是 token 失效）
    assert "🔴 CRITICAL" not in out, f"network 错误不应有 CRITICAL，实际输出:\n{out}"
    # 关键断言 3：WARNING 块出现
    assert "🟡 WARNING" in out, f"network 错误应有 WARNING，实际输出:\n{out}"


# ===== 用例 3：reauth 失败时仍返 CRITICAL 提示用户 =====

def test_reauth_failed_still_returns_critical(tmp_path):
    """reauth 失败时 → CRITICAL 块仍打印（兜底）。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    def fake_fetch(*a, **kw):
        return _make_401_result()

    def fake_silent_reauth_fail(reason: str = "401 from sync") -> bool:
        return False  # reauth 失败

    with _HomeSandbox(tmp_path):
        with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch):
            with patch("pull_to_sqlite._maybe_refresh_token_before_sync", return_value=True):
                with patch("pull_to_sqlite._silent_reauth", side_effect=fake_silent_reauth_fail):
                    args = _make_args(db_path, days=7)
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = pts.cmd_sync(args)
    out = buf.getvalue()

    assert rc == 0
    # 关键断言 1：CRITICAL 块仍打印（兜底，用户必须被告知）
    assert "🔴 CRITICAL" in out, (
        f"reauth 失败时 CRITICAL 块必须保留（兜底），实际输出:\n{out}"
    )
    # 关键断言 2：CRITICAL 块里说明 silent reauth 已失败
    assert "silent reauth" in out or "已失败" in out, (
        f"CRITICAL 块应说明 reauth 已失败（不是凭空说再做一次），实际输出:\n{out}"
    )
    # 关键断言 3：跑 init.py / zepp_oauth.py 提示保留
    assert "init.py" in out or "zepp_oauth" in out, (
        f"用户应被告知怎么修；实际输出:\n{out}"
    )


# ===== 用例 4：reauth 信息只写 log file，不打 stdout =====

def test_reauth_logged_to_file_not_stdout(tmp_path):
    """reauth 详细信息）写 log file）_REAUTH_LOG_FILE），stdout 完全静默。

    用户视角：sync 跑完之后 stdout 不该有"silent_reauth / refresh / login / 已 rotate"等内部信息。
    这些信息只在 log file 里，调试用。
    """
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    call_count = {"n": 0}

    def fake_fetch(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] <= len(pts.STREAMS):
            return _make_401_result()
        return _make_success_result(n=3)

    def fake_silent_reauth(reason: str = "401 from sync") -> bool:
        # fake 模拟 _silent_reauth 在内部会写 log file
        pts._reauth_log(f"TEST: silent_reauth called with reason={reason}")
        pts._reauth_log("TEST: refresh path succeeded")
        return True

    # 先清空 log file（避免前一个测试的残留）
    log_path = pts._REAUTH_LOG_FILE
    if log_path.exists():
        log_path.unlink()

    captured_log_path = {"v": None}
    captured_out = {"v": None}

    with _HomeSandbox(tmp_path):
        # 重新清空（_HomeSandbox 也会重设路径到沙箱）
        captured_log_path["v"] = pts._REAUTH_LOG_FILE  # 记录沙箱里的 log path
        if captured_log_path["v"].exists():
            captured_log_path["v"].unlink()
        with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch):
            with patch("pull_to_sqlite._maybe_refresh_token_before_sync", return_value=True):
                with patch("pull_to_sqlite._silent_reauth", side_effect=fake_silent_reauth):
                    args = _make_args(db_path, days=7)
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = pts.cmd_sync(args)
        captured_out["v"] = buf.getvalue()

    out = captured_out["v"]
    log_path = captured_log_path["v"]

    assert rc == 0
    # 关键断言 1：stdout 没有 "silent_reauth" / "TEST:" / "refresh path" 等内部细节
    assert "TEST:" not in out, (
        f"reauth 内部日志不应打到 stdout，实际输出:\n{out}"
    )
    assert "silent_reauth called" not in out, (
        f"silent_reauth 调用细节不应到 stdout，实际输出:\n{out}"
    )
    assert "refresh path succeeded" not in out, (
        f"refresh 成功细节不应到 stdout，实际输出:\n{out}"
    )
    # 关键断言 2：log file 写入了（用沙箱里的路径）
    assert log_path.exists(), f"log file 应该被创建：{log_path}"
    log_content = log_path.read_text()
    assert "TEST:" in log_content, (
        f"log file 应包含 TEST: 内容（供调试），实际内容:\n{log_content}"
    )
    # 关键断言 3：log file 不在 stdout 流（额外检查 log file 是 file 不是 stdout）
    assert "reauth.log" not in out or "log file" not in out, (
        f"log file 路径不应暴露给用户，实际输出:\n{out}"
    )


# ===== 用例 5：401 → reauth → 仍 401 → 不再重试 =====

def test_max_one_reauth_per_sync(tmp_path):
    """401 → reauth → 重跑仍 401 → 不再触发第二次 reauth（避免无限循环）。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    # 不管尝试几次，所有 fetch_and_normalize 都返 401
    def fake_fetch(*a, **kw):
        return _make_401_result()

    reauth_called = {"count": 0}

    def fake_silent_reauth(reason: str = "401 from sync") -> bool:
        reauth_called["count"] += 1
        return True  # 假装成功 → 触发重跑；重跑仍 401，不应再次 reauth

    with _HomeSandbox(tmp_path):
        with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch):
            with patch("pull_to_sqlite._maybe_refresh_token_before_sync", return_value=True):
                with patch("pull_to_sqlite._silent_reauth", side_effect=fake_silent_reauth):
                    args = _make_args(db_path, days=7)
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = pts.cmd_sync(args)
    out = buf.getvalue()

    assert rc == 0
    # 关键断言 1：_silent_reauth 最多被调用 1 次（避免无限循环）
    assert reauth_called["count"] <= 1, (
        f"401 → reauth → 仍 401 不应再 reauth，得到 {reauth_called['count']} 次"
    )
    assert reauth_called["count"] == 1, (
        f"期望调用 1 次 _silent_reauth，得到 {reauth_called['count']} 次"
    )
    # 关键断言 2：CRITICAL 块最后出现（兜底）
    assert "🔴 CRITICAL" in out, (
        f"reauth 后仍 401 应兜底 CRITICAL，实际输出:\n{out}"
    )


# ===== 用例 6：ZEPP_NO_REAUTH=1 时跳过 reauth（debug 友好）=====

def test_reauth_disabled_by_env(tmp_path):
    """设 ZEPP_NO_REAUTH=1 时即使有 401 也不调 reauth（debug 时想看 CRITICAL 块）。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    def fake_fetch(*a, **kw):
        return _make_401_result()

    reauth_called = {"count": 0}

    def fake_silent_reauth(reason: str = "401 from sync") -> bool:
        reauth_called["count"] += 1
        return True

    saved_environ = dict(os.environ)
    os.environ["ZEPP_NO_REAUTH"] = "1"
    try:
        with _HomeSandbox(tmp_path):
            with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch):
                with patch("pull_to_sqlite._maybe_refresh_token_before_sync", return_value=True):
                    with patch("pull_to_sqlite._silent_reauth", side_effect=fake_silent_reauth):
                        args = _make_args(db_path, days=7)
                        buf = io.StringIO()
                        with redirect_stdout(buf):
                            rc = pts.cmd_sync(args)
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)
        os.environ.pop("ZEPP_NO_REAUTH", None)

    out = buf.getvalue()
    assert rc == 0
    # 关键断言：_silent_reauth 没被调用（env var 关掉）
    assert reauth_called["count"] == 0, (
        f"ZEPP_NO_REAUTH=1 时不应调 _silent_reauth，得到 {reauth_called['count']} 次"
    )
    # 关键断言：CRITICAL 块仍出现（debug 时要看）
    assert "🔴 CRITICAL" in out, f"debug 模式下应保留 CRITICAL 块；实际输出:\n{out}"


# ===== 用例 7：happy path（无 401）不调 _silent_reauth =====

def test_reauth_skips_when_no_critical_error(tmp_path):
    """所有 stream 都成功 → _silent_reauth 不被调用，sync 正常完成。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    def fake_fetch(*a, **kw):
        return _make_success_result(n=7)

    reauth_called = {"count": 0}

    def fake_silent_reauth(reason: str = "401 from sync") -> bool:
        reauth_called["count"] += 1
        return True

    with _HomeSandbox(tmp_path):
        with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch):
            with patch("pull_to_sqlite._maybe_refresh_token_before_sync", return_value=True):
                with patch("pull_to_sqlite._silent_reauth", side_effect=fake_silent_reauth):
                    args = _make_args(db_path, days=7)
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = pts.cmd_sync(args)
    out = buf.getvalue()

    assert rc == 0
    # 关键断言 1：_silent_reauth 完全没被调用
    assert reauth_called["count"] == 0, (
        f"happy path 不应触发 _silent_reauth，得到 {reauth_called['count']} 次"
    )
    # 关键断言 2：sync 正常完成
    expected = 7 * len(pts.STREAMS)
    assert f"本次新增/更新 {expected} 条 records" in out, (
        f"happy path 应显示 {expected} 条 records；实际输出:\n{out}"
    )
    # 关键断言 3：CRITICAL / WARNING 都不出现
    assert "🔴" not in out and "🟡" not in out, (
        f"happy path 不应有 CRITICAL/WARNING；实际输出:\n{out}"
    )
    # 关键断言 4：fetch_and_normalize 只调了一次（每个 stream 一次）
    #  不重跑 — happy path 一次过


# ===== 用例 8：_silent_reauth 自身被调时的行为 =====

def test_silent_reauth_calls_refresh_subprocess(tmp_path):
    """直接测 _silent_reauth：mock subprocess.run 模拟 refresh 成功。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    fake_proc_ok = MagicMock()
    fake_proc_ok.returncode = 0
    fake_proc_ok.stderr = ""

    with _HomeSandbox(tmp_path):
        with patch.object(pts.subprocess, "run", return_value=fake_proc_ok) as mock_run:
            ok = pts._silent_reauth(reason="test reason")
    assert ok is True, f"_silent_reauth 应返 True（refresh 成功），得到 {ok}"
    # 关键断言：subprocess.run 被调用了
    assert mock_run.called, "应调 subprocess.run 跑 zepp_oauth.py refresh"
    # 调用参数：argv 第 0 个是 python3，第 1 个是 zepp_oauth.py，最后一个是 refresh
    args, kwargs = mock_run.call_args
    argv = args[0]
    assert "zepp_oauth.py" in str(argv), f"应调 zepp_oauth.py；argv={argv}"
    assert argv[-1] == "refresh", f"应调 refresh 子命令；argv={argv}"
    # 关键断言：stdout 重定向到 DEVNULL（不污染 cmd_sync 的 stdout）
    assert kwargs.get("stdout") == pts.subprocess.DEVNULL, (
        f"stdout 应重定向到 DEVNULL；kwargs={kwargs}"
    )


def test_silent_reauth_calls_login_when_refresh_fails(tmp_path):
    """refresh 失败 → 自动 fallback 到 login（如果有 ZEPP_PHONE）。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    # refresh 返 2（_step3 失败），login 返 0（成功）
    procs = [
        MagicMock(returncode=2, stderr="refresh failed"),  # refresh
        MagicMock(returncode=0, stderr=""),                # login
    ]

    saved_environ = dict(os.environ)
    os.environ["ZEPP_PHONE"] = "13800000000"
    try:
        with _HomeSandbox(tmp_path):
            with patch.object(pts.subprocess, "run", side_effect=procs) as mock_run:
                ok = pts._silent_reauth(reason="test refresh→login")
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)
        os.environ.pop("ZEPP_PHONE", None)

    assert ok is True, f"_silent_reauth 应返 True（login 成功），得到 {ok}"
    # 关键断言：subprocess.run 被调用了 2 次（refresh + login）
    assert mock_run.call_count == 2, (
        f"应调 subprocess.run 2 次（refresh 失败 + login 成功），得到 {mock_run.call_count}"
    )
    # 第 1 次：refresh
    argv1 = mock_run.call_args_list[0][0][0]
    assert argv1[-1] == "refresh"
    # 第 2 次：login + --phone
    argv2 = mock_run.call_args_list[1][0][0]
    assert argv2[-1] == "13800000000" or "--phone" in argv2
    assert "login" in argv2


def test_silent_reauth_returns_false_when_both_fail(tmp_path):
    """refresh + login 都失败 → 返 False（外层升级到 Critical）。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    procs = [
        MagicMock(returncode=2, stderr="refresh failed"),
        MagicMock(returncode=1, stderr="login failed"),
    ]

    saved_environ = dict(os.environ)
    os.environ["ZEPP_PHONE"] = "13800000000"
    try:
        with _HomeSandbox(tmp_path):
            with patch.object(pts.subprocess, "run", side_effect=procs):
                ok = pts._silent_reauth(reason="both fail")
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)
        os.environ.pop("ZEPP_PHONE", None)

    assert ok is False, f"_silent_reauth 应返 False（都失败），得到 {ok}"


def test_silent_reauth_no_phone_skips_login(tmp_path):
    """没有 ZEPP_PHONE → refresh 失败时直接返 False（不调 login）。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    fake_proc_fail = MagicMock(returncode=2, stderr="refresh failed")

    saved_environ = dict(os.environ)
    os.environ.pop("ZEPP_PHONE", None)
    try:
        with _HomeSandbox(tmp_path):
            with patch.object(pts.subprocess, "run", return_value=fake_proc_fail) as mock_run:
                ok = pts._silent_reauth(reason="no phone")
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)

    assert ok is False, f"无 ZEPP_PHONE + refresh 失败应返 False，得到 {ok}"
    # 关键断言：subprocess.run 只被调了 1 次（refresh）；没调 login
    assert mock_run.call_count == 1, (
        f"无 ZEPP_PHONE 时不应调 login，只调 refresh；得到 {mock_run.call_count} 次"
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))