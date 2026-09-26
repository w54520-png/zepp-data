"""
M5 章节：sync 错误可见性单元测试。

需求：sync 主流程必须把内层错误（401 / 网络异常 / 404）显式打到外层 stdout。
让用户第一时间知道是 token 问题，不是"Zepp Cloud 没数据"。

覆盖：
  1. test_needs_reauth_raises_critical — mock sync 触发 NeedsReauth，输出含 🔴 CRITICAL + 跑 init.py 提示
  2. test_network_error_shows_warning — mock sync 触发 URLError，输出含 🟡 WARNING
  3. test_unavailable_single_stream_doesnt_kill_sync — 单流 Unavailable，其他流继续
  4. test_zero_records_with_401_shows_specific_hint — "✓ 0 records" + 401 → 输出 ⚠️ token 失效提示
  5. test_token_refresh_failure_suggests_init_script — refresh 失败 exit 2 → sync 输出建议 init.py --rotate
  6. test_classify_error_direct — _classify_error 工具函数单元测试（4 类输入 → 4 类输出）

设计原则（不破坏现有测试）：
  - 不调用真实 OAuth，只 mock ZeppClient 抛 ZeppError 子类
  - monkeypatch Path.home() → tmp_path，让 token 写到沙箱
  - capture stdout，断言包含关键提示字符串
  - 不修改 cmd_sync 接口，只 monkeypatch 内部依赖
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch, MagicMock

# 让 tests/ 能 import scripts/
TESTS_DIR = Path(__file__).parent
sys.path.insert(0, str(TESTS_DIR.parent / "scripts"))

import pull_to_sqlite as pts  # noqa: E402
from zepp_client import (
    NeedsReauth, Unavailable, NetworkError, RetryExhausted,
    ZeppError, CloudRejected,
)  # noqa: E402


# ===== helpers =====

def _make_home_with_token(tmp: Path) -> Path:
    """构造一个 ~/.zepp-data/.secrets/token.json + token 写入 DATA_DIR"""
    data_dir = tmp / ".zepp-data"
    secrets_dir = data_dir / ".secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    token = {
        "user_id": "u_test_visibility",
        "app_token": "fake_app_token_for_test",
        "login_token": "fake_login_token_for_test",
        "region_host": "https://api-mifit-cn3.zepp.com",
        "extracted_at": "2026-09-22T00:00:00Z",
    }
    (secrets_dir / "token.json").write_text(json.dumps(token))
    # monkeypatch pull_to_sqlite 模块的路径常量
    pts.DATA_DIR = data_dir
    pts.SECRETS_DIR = secrets_dir
    pts.SECRETS_FILE = secrets_dir / "token.json"
    pts.DEFAULT_DB = data_dir / "zepp.db"
    return data_dir


class _HomeSandbox:
    """monkeypatch Path.home() → tmp + 把 pts 路径常量切到沙箱。"""
    def __init__(self, tmp):
        self.tmp = tmp
        self._const_backup = None

    def __enter__(self):
        self._const_backup = {
            "DATA_DIR": pts.DATA_DIR,
            "SECRETS_DIR": pts.SECRETS_DIR,
            "SECRETS_FILE": pts.SECRETS_FILE,
            "DEFAULT_DB": pts.DEFAULT_DB,
        }
        _make_home_with_token(self.tmp)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for k, v in self._const_backup.items():
            setattr(pts, k, v)


def _make_args(db_path: str, days: int = 7):
    """造一个简单 Namespace 替代 argparse。"""
    class Args:
        pass
    a = Args()
    a.db = db_path
    a.days = days
    return a


def _init_db(db_path: str) -> None:
    """建一个空 DB（让 update_capability 不报错）。"""
    pts.init_db(Path(db_path))


# ===== 用例 1：NeedsReauth (401) → CRITICAL 块 =====

def test_needs_reauth_raises_critical(tmp_path):
    """sync 触发 NeedsReauth → 输出包含 🔴 CRITICAL + 跑 init.py 提示。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    with _HomeSandbox(tmp_path):
        # 模拟 fetch_and_normalize 内部捕获 NeedsReauth（与现有代码行为一致：
        # fetch_and_normalize 顶层 except 抓 ZeppError 转 fetch_error + 携带 category）。
        # 我们直接 mock 整个 fetch_and_normalize，模拟它返 fetch_error + critical_auth category。
        def fake_fetch(*a, **kw):
            return {
                "records": 0, "diags": 0,
                "status": "fetch_error", "error": "HTTP 401: Unauthorized",
                "error_category": pts._ERROR_CATEGORY_CRITICAL_AUTH,
                "error_type": "NeedsReauth",
            }
        with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch):
            args = _make_args(db_path, days=7)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = pts.cmd_sync(args)
    out = buf.getvalue()

    assert rc == 0, f"cmd_sync 不应抛非零（即便全 401，让 cron 看到结果），得到 {rc}"
    # 关键断言 1：包含 🔴 CRITICAL 块
    assert "🔴 CRITICAL" in out, f"输出必须包含 🔴 CRITICAL 块，实际输出:\n{out}"
    # 关键断言 2：包含跑 init.py 提示
    assert "init.py" in out, f"输出必须建议跑 init.py，实际输出:\n{out}"
    # 关键断言 3：包含 zepp_oauth.py login 备选
    assert "zepp_oauth.py" in out, f"输出必须建议跑 zepp_oauth.py，实际输出:\n{out}"
    # 关键断言 4：每行 fetch_error 都打了 ✗（用户能看到全景）
    assert "fetch_error" in out, f"每行 fetch_error 应可见，实际输出:\n{out}"
    # 关键断言 5：401 错误字符串可见
    assert "HTTP 401" in out, f"401 字符串应保留在错误信息里，实际输出:\n{out}"


# ===== 用例 2：URLError / NetworkError → WARNING 块 =====

def test_network_error_shows_warning(tmp_path):
    """sync 触发 URLError → 输出包含 🟡 WARNING。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    with _HomeSandbox(tmp_path):
        def fake_fetch(*a, **kw):
            return {
                "records": 0, "diags": 0,
                "status": "fetch_error",
                "error": "URLError: Temporary failure in name resolution",
                "error_category": pts._ERROR_CATEGORY_NETWORK,
                "error_type": "NetworkError",
            }
        with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch):
            args = _make_args(db_path, days=7)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = pts.cmd_sync(args)
    out = buf.getvalue()

    assert rc == 0
    # 关键断言 1：包含 🟡 WARNING 块
    assert "🟡 WARNING" in out, f"输出必须包含 🟡 WARNING 块，实际输出:\n{out}"
    # 关键断言 2：包含网络问题提示
    assert "网络" in out or "服务" in out, f"输出必须解释网络/服务问题，实际输出:\n{out}"
    # 关键断言 3：URLError 字符串可见
    assert "URLError" in out or "NetworkError" in out, f"网络错误字符串应保留，实际输出:\n{out}"
    # 关键断言 4：不应该有 CRITICAL（这是网络问题，不是 token 失效）
    assert "🔴 CRITICAL" not in out, f"网络问题不应触发 CRITICAL，实际输出:\n{out}"


# ===== 用例 3：单流 Unavailable，其他流继续 =====

def test_unavailable_single_stream_doesnt_kill_sync(tmp_path):
    """单个流 Unavailable（404）→ 该流失败，其他流继续（happy path 不被破坏）。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    call_count = {"n": 0}

    def fake_fetch(client, label, surface, event_type, sub_type, fn,
                    from_ms, to_ms, db, user_id):
        """第一个流返 fetch_error + unavailable category，其他流正常返 available。"""
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {
                "records": 0, "diags": 0,
                "status": "fetch_error", "error": "HTTP 404: Not Found",
                "error_category": pts._ERROR_CATEGORY_UNAVAILABLE,
                "error_type": "Unavailable",
            }
        return {
            "records": 5,
            "diags": 0,
            "status": "available",
            "raw_count": 5,
            "last_ts_ms": None,
        }
    with _HomeSandbox(tmp_path):
        with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch):
            args = _make_args(db_path, days=7)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = pts.cmd_sync(args)
    out = buf.getvalue()

    assert rc == 0
    # 关键断言 1：fetch_and_normalize 调用 N 次（每个 stream 都跑了，没中断）
    assert call_count["n"] == len(pts.STREAMS), (
        f"单流失败不应中断其他流；调用次数={call_count['n']}，"
        f"期望={len(pts.STREAMS)}"
    )
    # 关键断言 2：HTTP 404 字符串在错误行
    assert "HTTP 404" in out, f"404 字符串应保留，实际输出:\n{out}"
    # 关键断言 3：包含 ✓ records（其他流成功）
    assert "✓     5 records" in out or "✓     5" in out, f"其他流应显示成功，实际输出:\n{out}"
    # 关键断言 4：404 单流不应触发 CRITICAL（这是端点不存在，不是 token 失效）
    assert "🔴 CRITICAL" not in out, f"单流 404 不应触发 CRITICAL，实际输出:\n{out}"


# ===== 用例 4：0 条 + 401 → ⚠️ token 失效提示 =====

def test_zero_records_with_401_shows_specific_hint(tmp_path):
    """sync 0 条但内层有 401 → '💡 sync 0 条诊断' 输出 ⚠️ token 失效具体提示。

    M6 改进：silent reauth 会先尝试（refresh → login），都失败后升级到 Critical 提示用户
    （看 0 条诊断）。所以现在输出的是 'silent reauth 已失败' 的新提示，但仍包含 token 失效关键字。
    """
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    with _HomeSandbox(tmp_path):
        # 模拟 fetch_and_normalize 内部捕获 NeedsReauth → 返 fetch_error + critical_auth
        def fake_fetch(*a, **kw):
            return {
                "records": 0, "diags": 0,
                "status": "fetch_error", "error": "HTTP 401: Unauthorized",
                "error_category": pts._ERROR_CATEGORY_CRITICAL_AUTH,
                "error_type": "NeedsReauth",
            }
        with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch):
            args = _make_args(db_path, days=7)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = pts.cmd_sync(args)
    out = buf.getvalue()

    assert rc == 0
    # 关键断言 1：CRITICAL 块（reauth 失败后仍要打 Critical 兜底）
    assert "🔴 CRITICAL" in out, f"必须包含 CRITICAL 块，实际输出:\n{out}"
    # 关键断言 2：本次新增/更新 0 条 records
    assert "本次新增/更新 0 条 records" in out, f"必须显示 0 条，实际输出:\n{out}"
    # 关键断言 3：0 条诊断里必须有"token 失效"具体提示（M6 新消息：silent reauth 已失败）
    assert "token 失效" in out, (
        f"0 条诊断必须说 token 失效；实际输出:\n{out}"
    )
    # 关键断言 4：提示跑 init 或 zepp_oauth
    assert ("init.py" in out or "zepp_oauth" in out), (
        f"0 条诊断里必须说怎么修复（init.py 或 zepp_oauth）；实际输出:\n{out}"
    )


# ===== 用例 5：refresh 失败 → 建议 init.py --rotate =====

def test_token_refresh_failure_suggests_init_script(tmp_path):
    """refresh 失败时（return 2），cmd_sync 的 init 引导也返 2 时建议跑 init.py --rotate。

    模拟 cmd_sync 走 init.py 引导分支（缺 ZEPP_PHONE 走交互）→ init.py 返 2 → cmd_sync 也返 2
    → 提示用户跑 init.py --rotate 或 zepp_oauth login。
    """
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    fake_proc = MagicMock()
    fake_proc.returncode = 2  # init.py 失败 exit 2

    # 清掉 ZEPP_PHONE 让 init 引导触发
    saved_environ = dict(os.environ)
    os.environ.pop("ZEPP_PHONE", None)
    try:
        with _HomeSandbox(tmp_path):
            # 模拟 sys.stdin.isatty() == True（让 cmd_sync 走 init 引导）
            fake_stdin = MagicMock()
            fake_stdin.isatty = MagicMock(return_value=True)
            with patch.object(pts.subprocess, "run", return_value=fake_proc):
                with patch.object(pts.sys, "stdin", fake_stdin):
                    args = _make_args(db_path, days=7)
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = pts.cmd_sync(args)
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)

    out = buf.getvalue()

    # 关键断言 1：cmd_sync 返 2（init 引导失败传播）
    assert rc == 2, f"init 引导失败应让 cmd_sync 返 2，得到 {rc}"
    # 关键断言 2：建议跑 init.py --rotate
    assert "init.py" in out, f"必须建议跑 init.py --rotate，实际输出:\n{out}"
    # 关键断言 3：建议跑 zepp_oauth.py login 作为备选
    assert "zepp_oauth" in out or "login" in out, (
        f"必须建议跑 zepp_oauth.py login 作为备选，实际输出:\n{out}"
    )


# ===== 用例 6：_classify_error 工具函数直接测试 =====

def test_classify_error_direct():
    """_classify_error 应该正确把 4 类异常 / 字符串映射到 4 个 category。"""
    # 异常对象优先
    assert pts._classify_error("whatever", err_obj=NeedsReauth("x")) == pts._ERROR_CATEGORY_CRITICAL_AUTH
    assert pts._classify_error("whatever", err_obj=Unavailable("x")) == pts._ERROR_CATEGORY_UNAVAILABLE
    assert pts._classify_error("whatever", err_obj=NetworkError("x")) == pts._ERROR_CATEGORY_NETWORK
    assert pts._classify_error("whatever", err_obj=RetryExhausted("x")) == pts._ERROR_CATEGORY_NETWORK
    # 字符串 fallback
    assert pts._classify_error("HTTP 401: Unauthorized") == pts._ERROR_CATEGORY_CRITICAL_AUTH
    assert pts._classify_error("HTTP 403: Forbidden") == pts._ERROR_CATEGORY_CRITICAL_AUTH
    assert pts._classify_error("HTTP 404: Not Found") == pts._ERROR_CATEGORY_UNAVAILABLE
    assert pts._classify_error("URLError: Temporary failure in name resolution") == pts._ERROR_CATEGORY_NETWORK
    assert pts._classify_error("TimeoutError: timed out after 35s") == pts._ERROR_CATEGORY_NETWORK
    assert pts._classify_error("RetryExhausted: HTTP 503") == pts._ERROR_CATEGORY_NETWORK
    assert pts._classify_error("HTTP 429: Too Many Requests") == pts._ERROR_CATEGORY_NETWORK
    assert pts._classify_error("CloudRejected: code=0") == pts._ERROR_CATEGORY_UNKNOWN
    assert pts._classify_error("") == pts._ERROR_CATEGORY_UNKNOWN
    assert pts._classify_error("random unrelated error") == pts._ERROR_CATEGORY_UNKNOWN


# ===== 用例 7（bonus）：happy path 不变（error_counts 全 0 不打印聚合块）=====

def test_happy_path_no_error_aggregation(tmp_path):
    """所有流都成功 → 不打印 '=== ⚠️ 错误聚合 ===' 块。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    with _HomeSandbox(tmp_path):
        def fake_fetch(*a, **kw):
            return {
                "records": 3,
                "diags": 0,
                "status": "available",
                "raw_count": 3,
                "last_ts_ms": None,
            }
        with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch):
            args = _make_args(db_path, days=7)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = pts.cmd_sync(args)
    out = buf.getvalue()

    assert rc == 0
    assert "=== ⚠️ 错误聚合 ===" not in out, (
        f"happy path 不应打印错误聚合块；实际输出:\n{out}"
    )
    # 不应有任何 CRITICAL / WARNING emoji
    assert "🔴" not in out and "🟡" not in out, (
        f"happy path 不应打印 CRITICAL/WARNING；实际输出:\n{out}"
    )
    # 应该有 ✓ records（happy path）
    assert "✓     3 records" in out, f"happy path 应显示成功；实际输出:\n{out}"


# ===== 用例 8（bonus）：RetryExhausted 5xx → WARNING（不是 CRITICAL）=====

def test_retry_exhausted_5xx_is_warning(tmp_path):
    """5xx 限流重试耗尽（RetryExhausted）→ WARNING，不是 CRITICAL。"""
    db_path = str(tmp_path / "zepp.db")
    _init_db(db_path)

    with _HomeSandbox(tmp_path):
        def fake_fetch(*a, **kw):
            return {
                "records": 0, "diags": 0,
                "status": "fetch_error",
                "error": "RetryExhausted: HTTP 503: Service Unavailable",
                "error_category": pts._ERROR_CATEGORY_NETWORK,
                "error_type": "RetryExhausted",
            }
        with patch.object(pts, "fetch_and_normalize", side_effect=fake_fetch):
            args = _make_args(db_path, days=7)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = pts.cmd_sync(args)
    out = buf.getvalue()

    assert rc == 0
    assert "🟡 WARNING" in out, f"5xx 限流应触发 WARNING；实际输出:\n{out}"
    assert "🔴 CRITICAL" not in out, (
        f"5xx 限流不应触发 CRITICAL；实际输出:\n{out}"
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))