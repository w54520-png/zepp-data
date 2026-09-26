"""test_oauth_verify.py — 验证 zepp_oauth.py 的 verify 子命令。

verify 子命令（默认本地检查）：
  - 检查 ~/.zepp-data/.secrets/token.json 存在
  - 字段完整性（app_token / login_token / region_host）
  - mtime vs app_ttl 推断剩余天数

可选 --remote：额外调 https://auth.huami.com/oauth2/verify
  - 预期对 app_token 永远 401（此端点只认第三方 access_token）
  - 401 时打印友好提示

覆盖用例（≥7）：
  1. test_verify_returns_0_on_fresh_token            (原 200 → 本地齐全)
  2. test_verify_returns_2_on_stale_token             (原 401 → mtime>30d)
  3. test_verify_returns_1_on_missing_token_json      (保留)
  4. test_verify_returns_3_on_missing_fields          (原缺 app_token→1, 现返 3)
  5. test_verify_calculates_remaining_days            (mtime=1d → 显示 1 天)
  6. test_verify_handles_network_error                (仅 --remote 模式)
  7. test_verify_remote_401_with_helpful_message      (新增：远程 401 友好提示)

策略：
  - 通过 monkeypatch DATA_DIR（HOME 沙箱），让 verify 读 / 写临时 token.json
  - mock urllib.request.urlopen → 仅在 --remote 模式下被调用
  - 捕获 stdout（capsys）验证输出
"""
from __future__ import annotations
import io
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError

# 让 tests/ 能 import scripts/
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import zepp_oauth as oauth  # noqa: E402
import urllib.request  # noqa: E402


# ============ helpers ============

class _Args:
    """argparse.Namespace 的最小替代。verify 现在支持 --remote 标志。"""
    def __init__(self, remote=False):
        self.remote = remote


def _write_token(tmpdir, app_token="dummy_app_token_value_for_test_xxxxx",
                 user_id="1234567890", include_all=True,
                 set_mtime_age_days=None, app_ttl=86400 * 30):
    """在 tmpdir 写一个合法的 token.json。

    set_mtime_age_days: 设为 N 天前（用于模拟过期）
    app_ttl: 模拟 token 有效期（默认 30 天）
    """
    tok = {
        "app_token": app_token if include_all else "",
        "user_id": user_id,
        "region_host": "https://api-mifit-cn3.zepp.com",
        "login_token": "dummy_login_token_xxxxxxxxxxxxxxx",
        "app_ttl": app_ttl,
        "extracted_at": "2026-09-26",
    }
    secrets_dir = Path(tmpdir) / ".zepp-data" / ".secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    p = secrets_dir / "token.json"
    p.write_text(json.dumps(tok, ensure_ascii=False, indent=2))

    if set_mtime_age_days is not None:
        # 手动设 mtime 为 N 天前
        old_mtime = time.time() - set_mtime_age_days * 86400
        os.utime(p, (old_mtime, old_mtime))

    return tok, p


def _write_partial_token(tmpdir, missing_fields=()):
    """写一个 token.json，故意缺某些字段。"""
    tok = {
        "app_token": "valid_app_token_xxxxxxxxxxxxxxxxxx",
        "user_id": "1234567890",
        "region_host": "https://api-mifit-cn3.zepp.com",
        "login_token": "dummy_login_token_xxxxxxxxxxxxxxx",
        "extracted_at": "2026-09-26",
    }
    for f in missing_fields:
        tok.pop(f, None)
    secrets_dir = Path(tmpdir) / ".zepp-data" / ".secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    p = secrets_dir / "token.json"
    p.write_text(json.dumps(tok, ensure_ascii=False, indent=2))
    return tok, p


def _patch_home_to(tmpdir, monkeypatch):
    """把 zepp_oauth 的 DATA_DIR / SECRETS_DIR / SECRETS_FILE 切到 tmpdir。"""
    fake_home = Path(tmpdir)
    fake_data_dir = fake_home / ".zepp-data"
    fake_secrets_dir = fake_data_dir / ".secrets"
    monkeypatch.setattr(oauth, "DATA_DIR", fake_data_dir)
    monkeypatch.setattr(oauth, "SECRETS_DIR", fake_secrets_dir)
    monkeypatch.setattr(oauth, "SECRETS_FILE", fake_secrets_dir / "token.json")


def _make_urlopen_response(status=200, payload=None, raw_text=None):
    """构造一个 mock context-manager 风格的 urlopen 响应对象。"""
    body_text = raw_text if raw_text is not None else json.dumps(payload or {})
    body_bytes = body_text.encode("utf-8")
    resp = MagicMock()
    resp.status = status

    def _enter(_):
        return resp

    def _exit_(*_a):
        return False

    resp.__enter__ = _enter
    resp.__exit__ = _exit_
    resp.read = MagicMock(return_value=body_bytes)
    return resp


def _make_http_error(status=401, payload=None):
    """构造一个 mock HTTPError。"""
    body_text = json.dumps(payload or {"code": 0, "message": "invalid token"})
    err = HTTPError(
        url="https://auth.huami.com/oauth2/verify",
        code=status,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(body_text.encode("utf-8")),
    )
    return err


# ============ 用例 ============

def test_verify_returns_0_on_fresh_token(tmp_path, monkeypatch, capsys):
    """本地 token 文件齐全 + mtime < 30 天 → 返 0（默认本地检查，不联网）。"""
    _patch_home_to(tmp_path, monkeypatch)
    _write_token(tmp_path, app_token="valid_app_token_xxxxxxxxxxxxxxxxxx",
                 user_id="1876543210")

    # urlopen 不应该被调用（默认本地检查）
    urlopen_called = []

    def _no_call(*_a, **_kw):
        urlopen_called.append(True)
        raise AssertionError("urlopen 不应被调用（默认本地模式）")

    with patch.object(urllib.request, "urlopen", side_effect=_no_call):
        rc = oauth.cmd_verify(_Args(remote=False))

    captured = capsys.readouterr()
    assert rc == 0, f"expected 0, got {rc}; stdout={captured.out!r}"
    assert "=== Token 本地检查 ===" in captured.out
    assert "user_id:" in captured.out
    assert "1876543210" in captured.out
    assert "✓ token 本地检查通过" in captured.out
    # 不能打印完整 token
    assert "valid_app_token_xxxxxxxxxxxxxxxxxx" not in captured.out
    # 没调远程（默认）
    assert "远程验证" not in captured.out
    assert urlopen_called == [], "默认本地模式不应调 urlopen"
    print(f"  ✓ 本地齐全 + 新鲜 → rc=0, 无网络调用")


def test_verify_returns_2_on_stale_token(tmp_path, monkeypatch, capsys):
    """token.json mtime > 30 天 → 返 2（推断过期）。"""
    _patch_home_to(tmp_path, monkeypatch)
    _write_token(tmp_path, set_mtime_age_days=35)

    # urlopen 不应被调用
    urlopen_called = []

    def _no_call(*_a, **_kw):
        urlopen_called.append(True)
        raise AssertionError("urlopen 不应被调用（默认本地模式）")

    with patch.object(urllib.request, "urlopen", side_effect=_no_call):
        rc = oauth.cmd_verify(_Args(remote=False))

    captured = capsys.readouterr()
    assert rc == 2, f"expected 2, got {rc}; stdout={captured.out!r}"
    assert "30 天" in captured.out
    assert "可能过期" in captured.out
    assert urlopen_called == [], "默认本地模式不应调 urlopen"
    print(f"  ✓ token mtime 35 天 → rc=2, 提示过期")


def test_verify_returns_1_on_missing_token_json(tmp_path, monkeypatch, capsys):
    """token.json 不存在 → cmd_verify 返 1（不发起网络请求）。"""
    _patch_home_to(tmp_path, monkeypatch)
    # 不写 token.json → SECRETS_FILE 不存在

    # urlopen 不应该被调用
    urlopen_called = []

    def _no_call(*_a, **_kw):
        urlopen_called.append(True)
        raise AssertionError("urlopen 不应被调用")

    with patch.object(urllib.request, "urlopen", side_effect=_no_call):
        rc = oauth.cmd_verify(_Args(remote=False))

    captured = capsys.readouterr()
    assert rc == 1, f"expected 1, got {rc}; stdout={captured.out!r}"
    assert "token.json 不存在" in captured.out
    assert urlopen_called == [], "urlopen 不应该被调用"
    print(f"  ✓ token.json missing → rc=1, no network call")


def test_verify_returns_3_on_missing_fields(tmp_path, monkeypatch, capsys):
    """token.json 缺关键字段（app_token / login_token / region_host） → 返 3。

    之前版本缺 app_token 返 1 — 现统一"缺关键字段"返 3 更准确。
    """
    _patch_home_to(tmp_path, monkeypatch)
    _write_partial_token(tmp_path, missing_fields=["app_token", "region_host"])

    # urlopen 不应该被调用
    urlopen_called = []

    def _no_call(*_a, **_kw):
        urlopen_called.append(True)
        raise AssertionError("urlopen 不应被调用")

    with patch.object(urllib.request, "urlopen", side_effect=_no_call):
        rc = oauth.cmd_verify(_Args(remote=False))

    captured = capsys.readouterr()
    assert rc == 3, f"expected 3, got {rc}; stdout={captured.out!r}"
    assert "缺字段" in captured.out
    assert "app_token" in captured.out
    assert "region_host" in captured.out
    assert urlopen_called == [], "urlopen 不应该被调用"
    print(f"  ✓ token.json 缺关键字段 → rc=3, no network call")


def test_verify_calculates_remaining_days(tmp_path, monkeypatch, capsys):
    """token.json mtime 1 天 → 输出包含 1 天年龄。"""
    _patch_home_to(tmp_path, monkeypatch)
    _write_token(tmp_path, set_mtime_age_days=1)

    # urlopen 不应该被调用
    urlopen_called = []

    def _no_call(*_a, **_kw):
        urlopen_called.append(True)
        raise AssertionError("urlopen 不应被调用（默认本地模式）")

    with patch.object(urllib.request, "urlopen", side_effect=_no_call):
        rc = oauth.cmd_verify(_Args(remote=False))

    captured = capsys.readouterr()
    assert rc == 0, f"expected 0, got {rc}; stdout={captured.out!r}"
    # 1 天年龄
    assert "年龄:" in captured.out or "年龄" in captured.out
    assert "1.0 天" in captured.out
    assert urlopen_called == [], "默认本地模式不应调 urlopen"
    print(f"  ✓ token mtime 1 天 → rc=0, 显示 '1.0 天'")


def test_verify_handles_network_error(tmp_path, monkeypatch, capsys):
    """--remote 模式下 URLError → 返 4 + 错误信息。"""
    _patch_home_to(tmp_path, monkeypatch)
    _write_token(tmp_path)

    err = URLError("Name or service not known")

    with patch.object(urllib.request, "urlopen", side_effect=err):
        rc = oauth.cmd_verify(_Args(remote=True))

    captured = capsys.readouterr()
    assert rc == 4, f"expected 4, got {rc}; stdout={captured.out!r}"
    # 不能直接输出 traceback；应该是友好提示
    assert "Traceback" not in captured.out
    assert "错误" in captured.out
    assert "远程验证" in captured.out
    print(f"  ✓ --remote 模式 URLError → rc=4, 友好提示, no traceback")


def test_verify_remote_401_with_helpful_message(tmp_path, monkeypatch, capsys):
    """新增用例：--remote 模式远程返 401 时打印"auth.huami.com 不认 app_token"提示 + 返 4。"""
    _patch_home_to(tmp_path, monkeypatch)
    _write_token(tmp_path, app_token="my_app_token_xxxxxxxxxxxxxxxxxxxx")

    err = _make_http_error(status=401, payload={"code": 0, "message": "invalid token"})

    with patch.object(urllib.request, "urlopen", side_effect=err):
        rc = oauth.cmd_verify(_Args(remote=True))

    captured = capsys.readouterr()
    assert rc == 4, f"expected 4, got {rc}; stdout={captured.out!r}"
    # HTTP 401 必须打出
    assert "HTTP 401" in captured.out
    # 友好提示必须打出
    assert "auth.huami.com 不认 app_token" in captured.out
    assert "access_token" in captured.out  # 说明此端点只认 access_token
    # 不能打印完整 token
    assert "my_app_token_xxxxxxxxxxxxxxxxxxxx" not in captured.out
    print(f"  ✓ --remote 401 → rc=4 + 友好提示 + 不打印完整 token")


# ============ 跑所有测试 ============

if __name__ == "__main__":
    tests = [
        test_verify_returns_0_on_fresh_token,
        test_verify_returns_2_on_stale_token,
        test_verify_returns_1_on_missing_token_json,
        test_verify_returns_3_on_missing_fields,
        test_verify_calculates_remaining_days,
        test_verify_handles_network_error,
        test_verify_remote_401_with_helpful_message,
    ]
    failed = []
    for t in tests:
        try:
            t()
            print()
        except AssertionError as e:
            print(f"  ✗ {t.__name__} FAIL: {e}\n")
            failed.append(t.__name__)
        except Exception as e:
            import traceback
            print(f"  ✗ {t.__name__} ERROR: {type(e).__name__}: {e}")
            traceback.print_exc()
            print()
            failed.append(t.__name__)
    print("=" * 60)
    if failed:
        print(f"❌ {len(failed)}/{len(tests)} failed: {failed}")
        sys.exit(1)
    print(f"✅ {len(tests)}/{len(tests)} passed")