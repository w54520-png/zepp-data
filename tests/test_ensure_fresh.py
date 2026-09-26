"""
M6 章节：ensure_fresh() 自动 sync 机制单元测试

需求：每次问数据前，如果上次 sync 距现在超过 5 分钟（300s）就自动拉最新。

覆盖：
  1. test_meta_empty        — meta 表无 last_pull_at → 触发 sync + 返 True
  2. test_meta_fresh        — last_pull_at 30s 前 → 不 sync + 返 False
  3. test_meta_stale        — last_pull_at 10min 前 → 触发 sync + 返 True
  4. test_meta_very_stale   — last_pull_at 2h 前 → 触发 sync + 返 True（警告级）
  5. test_meta_invalid_format — meta 表 last_pull_at 非法字符串 → warning + 触发 sync

所有用例都 mock subprocess.run 避免真的跑 pull_to_sqlite.py sync（会污染真实 DB）。
"""
from __future__ import annotations
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

# 把 scripts 加到 sys.path，让 import query_zepp 直接生效
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import query_zepp  # noqa: E402


def _make_db_with_meta(meta_value=None, db_missing=False):
    """构造临时 DB + meta 表。meta_value=None 表示 meta 表无该 key；db_missing=True 表示不创建 DB 文件。"""
    if db_missing:
        # 用一个肯定不会存在的路径
        return "/tmp/zepp_does_not_exist_ensure_fresh_test.db"

    tmp = tempfile.NamedTemporaryFile(prefix="zepp_ensure_fresh_", suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.execute("""
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    if meta_value is not None:
        conn.execute("INSERT INTO meta(key, value) VALUES ('last_pull_at', ?)", (meta_value,))
    conn.commit()
    conn.close()
    return tmp.name


def _iso(minutes_ago=None, hours_ago=None):
    """生成 last_pull_at ISO 字符串（UTC）。"""
    now = datetime.now(timezone.utc)
    if minutes_ago is not None:
        dt = now - timedelta(minutes=minutes_ago)
    elif hours_ago is not None:
        dt = now - timedelta(hours=hours_ago)
    else:
        dt = now
    return dt.isoformat()


# ============ 用例 1：meta 表无 last_pull_at（首次运行）============

def test_meta_empty():
    """meta 表无 last_pull_at → 触发 sync + 返 True。"""
    db_path = _make_db_with_meta(meta_value=None)

    mock_proc = MagicMock()
    mock_proc.returncode = 0

    with patch.object(query_zepp.subprocess, "run", return_value=mock_proc) as m_run:
        result = query_zepp.ensure_fresh(db_path=db_path, max_age_sec=300)

    assert result is True, f"[meta_empty] 期望 True，得到 {result!r}"
    assert m_run.call_count == 1, f"[meta_empty] 期望 subprocess.run 调 1 次，得到 {m_run.call_count}"
    # 验证命令行参数
    call_args = m_run.call_args
    cmd = call_args[0][0]
    assert "sync" in cmd, f"[meta_empty] 命令应包含 'sync'，得到 {cmd}"
    assert "--days" in cmd, f"[meta_empty] 命令应包含 '--days'，得到 {cmd}"
    assert "1" in cmd, f"[meta_empty] 命令应 days=1（缓存刷新），得到 {cmd}"

    # 清理
    try:
        os.unlink(db_path)
    except OSError:
        pass


# ============ 用例 2：last_pull_at 30 秒前（新鲜）============

def test_meta_fresh():
    """last_pull_at 30 秒前 → 不 sync + 返 False。"""
    db_path = _make_db_with_meta(meta_value=_iso(minutes_ago=0))  # 实际上 0 分钟 = 现在

    # 重写：用 seconds_ago 模拟 30 秒前
    now = datetime.now(timezone.utc)
    fresh_iso = (now - timedelta(seconds=30)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE meta SET value=? WHERE key='last_pull_at'", (fresh_iso,))
    conn.commit()
    conn.close()

    with patch.object(query_zepp.subprocess, "run") as m_run:
        result = query_zepp.ensure_fresh(db_path=db_path, max_age_sec=300)

    assert result is False, f"[meta_fresh] 期望 False，得到 {result!r}"
    assert m_run.call_count == 0, f"[meta_fresh] 30 秒前不应 sync，subprocess.run 调了 {m_run.call_count} 次"

    try:
        os.unlink(db_path)
    except OSError:
        pass


# ============ 用例 3：last_pull_at 10 分钟前（陈旧）============

def test_meta_stale():
    """last_pull_at 10 分钟前 → 触发 sync + 返 True（普通级别）。"""
    db_path = _make_db_with_meta(meta_value=_iso(minutes_ago=10))

    mock_proc = MagicMock()
    mock_proc.returncode = 0

    with patch.object(query_zepp.subprocess, "run", return_value=mock_proc) as m_run:
        result = query_zepp.ensure_fresh(db_path=db_path, max_age_sec=300)

    assert result is True, f"[meta_stale] 期望 True，得到 {result!r}"
    assert m_run.call_count == 1, f"[meta_stale] 期望 subprocess.run 调 1 次，得到 {m_run.call_count}"

    try:
        os.unlink(db_path)
    except OSError:
        pass


# ============ 用例 4：last_pull_at 2 小时前（严重陈旧）============

def test_meta_very_stale():
    """last_pull_at 2 小时前 → 触发 sync + 返 True（警告级）。"""
    db_path = _make_db_with_meta(meta_value=_iso(hours_ago=2))

    mock_proc = MagicMock()
    mock_proc.returncode = 0

    with patch.object(query_zepp.subprocess, "run", return_value=mock_proc) as m_run:
        result = query_zepp.ensure_fresh(db_path=db_path, max_age_sec=300)

    assert result is True, f"[meta_very_stale] 期望 True，得到 {result!r}"
    assert m_run.call_count == 1, f"[meta_very_stale] 期望 subprocess.run 调 1 次，得到 {m_run.call_count}"

    # 2h 应触发"严重陈旧"分支（age > 3600s → very_stale 文案）
    # 这里只验证返回 True + 调了 sync；详细文案断言放在集成层面

    try:
        os.unlink(db_path)
    except OSError:
        pass


# ============ 用例 5：last_pull_at 格式非法（防御）============

def test_meta_invalid_format():
    """meta.last_pull_at 是非法字符串 → warning + 触发 sync。"""
    db_path = _make_db_with_meta(meta_value="not-a-valid-datetime-at-all")

    mock_proc = MagicMock()
    mock_proc.returncode = 0

    with patch.object(query_zepp.subprocess, "run", return_value=mock_proc) as m_run:
        result = query_zepp.ensure_fresh(db_path=db_path, max_age_sec=300)

    assert result is True, f"[meta_invalid_format] 期望 True（防御性 sync），得到 {result!r}"
    assert m_run.call_count == 1, f"[meta_invalid_format] 期望 sync 1 次，得到 {m_run.call_count}"

    try:
        os.unlink(db_path)
    except OSError:
        pass


# ============ 额外：DB 文件不存在（早期返回，不抛异常）============

def test_db_missing_no_crash():
    """DB 文件不存在 → 返 False 不抛异常（让 connect() 自己提示用户跑 init）。"""
    fake_db = "/tmp/zepp_does_not_exist_ensure_fresh_test_xyz.db"
    if os.path.exists(fake_db):
        os.unlink(fake_db)

    with patch.object(query_zepp.subprocess, "run") as m_run:
        result = query_zepp.ensure_fresh(db_path=fake_db, max_age_sec=300)

    assert result is False, f"[db_missing] 期望 False（不 sync），得到 {result!r}"
    assert m_run.call_count == 0, f"[db_missing] 无 DB 时不应 sync，subprocess.run 调了 {m_run.call_count} 次"


# ============ 额外：subprocess 失败也不让脚本崩溃 ============

def test_subprocess_failure_does_not_crash():
    """sync subprocess 返非 0 → ensure_fresh 仍返 True 但不抛异常。"""
    db_path = _make_db_with_meta(meta_value=_iso(hours_ago=2))

    mock_proc = MagicMock()
    mock_proc.returncode = 1  # 模拟 sync 失败

    with patch.object(query_zepp.subprocess, "run", return_value=mock_proc) as m_run:
        # 不应抛任何异常
        result = query_zepp.ensure_fresh(db_path=db_path, max_age_sec=300)

    assert result is True, f"[subprocess_failure] 期望 True（触发了 sync，但失败不影响返回），得到 {result!r}"
    assert m_run.call_count == 1, f"[subprocess_failure] 期望 sync 1 次，得到 {m_run.call_count}"

    try:
        os.unlink(db_path)
    except OSError:
        pass