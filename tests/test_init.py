"""test_init.py — 验证 init.py 的关键函数（mock stdin/stdout + 临时 HOME）。

覆盖（M4 拍板 ≥12 用例）：
1.  --status 无配置 → 友好报告
2.  --status 全配置 → 友好报告
3.  detect_env 检测 ZEPP_PHONE 环境变量
4.  detect_env 检测 ZEPP_PASSWORD 环境变量
5.  detect_env 检测 ~/.zepp-password 文件存在
6.  detect_env 检测 ~/.zepp-data/.secrets/token.json
7.  write_to_file 写完后 chmod 600
8.  write_to_file 写之前 umask 077
9.  init.py 源码 grep 永远不 print password
10. write_to_env 写到 ~/.zepp-data/.env（--mode env）
11. write_to_file 写到 ~/.zepp-password（--mode file）
12. --rotate 后 ZEPP_PHONE 不变

策略：
- monkeypatch Path.home() → tmp_path，让 init.py 读 / 写到沙箱目录
- 不真跑 main()（避免触发 OAuth 子进程）；单测各 helper
- 用 io.StringIO 替换 stdout，验证 print_summary 输出
"""
import sys
import os
import io
import re
import stat
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

# 让 tests/ 能 import scripts/
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import init as init_mod  # noqa: E402


# ============ helpers ============

class HomeSandbox:
    """monkeypatch Path.home() → tmp_path,清掉 init 模块加载时确定的路径常量。"""
    def __init__(self, tmp):
        self.tmp = tmp
        self._home_backup = None
        self._const_backup = None

    def __enter__(self):
        self._home_backup = init_mod.Path.home
        self._const_backup = {
            "DATA_DIR": init_mod.DATA_DIR,
            "SECRETS_DIR": init_mod.SECRETS_DIR,
            "SECRETS_FILE": init_mod.SECRETS_FILE,
            "ENV_FILE": init_mod.ENV_FILE,
            "PASSWORD_FILE_GLOBAL": init_mod.PASSWORD_FILE_GLOBAL,
        }
        # 强制 Path.home() 走我们临时目录
        init_mod.Path.home = classmethod(lambda cls: self.tmp)
        # 同步重置所有路径常量
        init_mod.DATA_DIR = self.tmp / ".zepp-data"
        init_mod.SECRETS_DIR = init_mod.DATA_DIR / ".secrets"
        init_mod.SECRETS_FILE = init_mod.SECRETS_DIR / "token.json"
        init_mod.ENV_FILE = init_mod.DATA_DIR / ".env"
        init_mod.PASSWORD_FILE_GLOBAL = self.tmp / ".zepp-password"
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        init_mod.Path.home = self._home_backup
        for k, v in self._const_backup.items():
            setattr(init_mod, k, v)


def _make_token(tmp: Path, user_id="u_123", region="api-us.zepp.com"):
    """造一个合法的 token.json。"""
    secret_dir = tmp / ".zepp-data" / ".secrets"
    secret_dir.mkdir(parents=True, exist_ok=True)
    secret_dir.chmod(0o700)
    token = {
        "user_id": user_id,
        "region_host": region,
        "extracted_at": "2026-09-22T00:00:00Z",
        "access_token": "fake",
    }
    (secret_dir / "token.json").write_text(json.dumps(token))
    return secret_dir / "token.json"


def _make_pw_file(tmp: Path, content="secret_pw"):
    """造一个 ~/.zepp-password 文件（设正确 mode）。"""
    p = tmp / ".zepp-password"
    p.write_text(content + "\n")
    os.chmod(p, 0o600)
    return p


# ============ 1. status 无配置 ============

def test_status_no_config():
    """全空环境 → detect_env 返 phone_set=False / token_present=False / 没有密码来源。"""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        with HomeSandbox(tmp):
            # 清掉可能的环境变量
            env_backup = {}
            for k in ("ZEPP_PHONE", "ZEPP_PASSWORD", "ZEPP_DATA_DIR"):
                env_backup[k] = os.environ.pop(k, None)
            try:
                state = init_mod.detect_env()
                assert state["phone_set"] is False
                assert state["env_password_set"] is False
                assert state["file_present"] is False
                assert state["token_present"] is False
                # 打印到 StringIO 验证 print_summary 不抛
                buf = io.StringIO()
                init_mod.print_summary(state, stream=buf)
                out = buf.getvalue()
                assert "缺 ZEPP_PHONE" in out, f"missing '缺 ZEPP_PHONE' in:\n{out}"
                assert "缺密码" in out, f"missing '缺密码' in:\n{out}"
                assert "无 token" in out, f"missing '无 token' in:\n{out}"
                print(f"  ✓ --status no config → friendly report (all 3 ✗ marks)")
            finally:
                for k, v in env_backup.items():
                    if v is not None:
                        os.environ[k] = v


# ============ 2. status 全配置 ============

def test_status_full_config():
    """ZEPP_PHONE + password file + token 都有 → detect_env 全 True。"""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        with HomeSandbox(tmp):
            # 清理
            env_backup = {}
            for k in ("ZEPP_PHONE", "ZEPP_PASSWORD", "ZEPP_DATA_DIR"):
                env_backup[k] = os.environ.pop(k, None)
            try:
                os.environ["ZEPP_PHONE"] = "186XXXXXXXX"
                _make_pw_file(tmp)
                _make_token(tmp)

                state = init_mod.detect_env()
                assert state["phone_set"] is True
                assert state["phone_masked"] == "186****XXXX", f"got {state['phone_masked']!r}"
                assert state["file_present"] is True
                assert state["token_present"] is True
                assert state["token_user_id"] == "u_123"

                buf = io.StringIO()
                init_mod.print_summary(state, stream=buf)
                out = buf.getvalue()
                assert "已设" in out
                assert "已建" in out
                assert "token.json 已存在" in out
                assert "缺" not in out, f"unexpected 缺 marker in full-config report:\n{out}"
                print(f"  ✓ --status full config → all ✓ (phone masked, token shown)")
            finally:
                for k, v in env_backup.items():
                    if v is not None:
                        os.environ[k] = v


# ============ 3. detect_env 检测 ZEPP_PHONE ============

def test_detect_env_zehp_phone():
    """ZEPP_PHONE 环境变量被 detect_env 识别。"""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        with HomeSandbox(tmp):
            env_backup = {}
            for k in ("ZEPP_PHONE", "ZEPP_PASSWORD", "ZEPP_DATA_DIR"):
                env_backup[k] = os.environ.pop(k, None)
            try:
                # 测 1: 无
                s1 = init_mod.detect_env()
                assert s1["phone_set"] is False
                # 测 2: 有
                os.environ["ZEPP_PHONE"] = "186XXXXXXXX"
                s2 = init_mod.detect_env()
                assert s2["phone_set"] is True
                assert s2["phone_masked"] == "186****XXXX"
                # 测 3: 空白字符应被 strip 后视为未设
                os.environ["ZEPP_PHONE"] = "   "
                s3 = init_mod.detect_env()
                assert s3["phone_set"] is False, f"blank phone should be False, got {s3}"
                print(f"  ✓ detect_env ZEPP_PHONE: False / True+masked / blank→False")
            finally:
                for k, v in env_backup.items():
                    if v is not None:
                        os.environ[k] = v


# ============ 4. detect_env 检测 ZEPP_PASSWORD ============

def test_detect_env_zehp_password():
    """ZEPP_PASSWORD 环境变量被 detect_env 识别（记录长度不存值）。"""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        with HomeSandbox(tmp):
            env_backup = {}
            for k in ("ZEPP_PHONE", "ZEPP_PASSWORD", "ZEPP_DATA_DIR"):
                env_backup[k] = os.environ.pop(k, None)
            try:
                s1 = init_mod.detect_env()
                assert s1["env_password_set"] is False
                assert s1["env_password_len"] == 0
                os.environ["ZEPP_PASSWORD"] = "supersecret123"
                s2 = init_mod.detect_env()
                assert s2["env_password_set"] is True
                assert s2["env_password_len"] == 14, f"got len={s2['env_password_len']}"
                # 确认 state 里没存明文密码（只有长度）
                assert "supersecret" not in repr(s2)
                print(f"  ✓ detect_env ZEPP_PASSWORD: set=True + len=14 + no plaintext leak")
            finally:
                for k, v in env_backup.items():
                    if v is not None:
                        os.environ[k] = v


# ============ 5. detect_env 检测 password file ============

def test_detect_password_file():
    """~/.zepp-password 存在 → detect_env.file_present=True。"""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        with HomeSandbox(tmp):
            env_backup = {}
            for k in ("ZEPP_PHONE", "ZEPP_PASSWORD", "ZEPP_DATA_DIR"):
                env_backup[k] = os.environ.pop(k, None)
            try:
                s1 = init_mod.detect_env()
                assert s1["file_present"] is False
                _make_pw_file(tmp)
                s2 = init_mod.detect_env()
                assert s2["file_present"] is True
                # file_mode 应是 0o600 (oct "0o600")
                assert "600" in s2["file_mode"], f"got mode={s2['file_mode']!r}"
                print(f"  ✓ detect_env password file: False → True, mode={s2['file_mode']}")
            finally:
                for k, v in env_backup.items():
                    if v is not None:
                        os.environ[k] = v


# ============ 6. detect_env 检测 token ============

def test_detect_token():
    """~/.zepp-data/.secrets/token.json 存在 → token_present=True + meta 字段可读。"""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        with HomeSandbox(tmp):
            env_backup = {}
            for k in ("ZEPP_PHONE", "ZEPP_PASSWORD", "ZEPP_DATA_DIR"):
                env_backup[k] = os.environ.pop(k, None)
            try:
                s1 = init_mod.detect_env()
                assert s1["token_present"] is False
                _make_token(tmp, user_id="u_42", region="api-eu.zepp.com")
                s2 = init_mod.detect_env()
                assert s2["token_present"] is True
                assert s2["token_user_id"] == "u_42"
                assert s2["token_region"] == "api-eu.zepp.com"
                print(f"  ✓ detect_env token: True + user_id=u_42 + region=api-eu.zepp.com")
            finally:
                for k, v in env_backup.items():
                    if v is not None:
                        os.environ[k] = v


# ============ 7. write_to_file chmod 600 ============

def test_chmod_password_file_600():
    """write_to_file 后 ~/.zepp-password 必须是 0o600。"""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        with HomeSandbox(tmp):
            env_backup = {}
            for k in ("ZEPP_PHONE", "ZEPP_PASSWORD", "ZEPP_DATA_DIR"):
                env_backup[k] = os.environ.pop(k, None)
            try:
                path = init_mod.write_to_file("mypassword")
                assert path.exists()
                mode = path.stat().st_mode & 0o777
                assert mode == 0o600, f"got mode={oct(mode)}, expected 0o600"
                # 内容确实写入了
                assert path.read_text().strip() == "mypassword"
                print(f"  ✓ write_to_file: mode={oct(mode)} (chmod 600 enforced)")
            finally:
                for k, v in env_backup.items():
                    if v is not None:
                        os.environ[k] = v


# ============ 8. write_to_file 前 umask 077 ============

def test_umask_before_write():
    """write_to_file 内部用 os.umask(0o077) 写文件；写完后必须还原原 umask。

    关键检查：
    - 写期间 umask 临时设 0o077（用 monkeypatch 探针确认）
    - 写完后还原（不能污染调用方的 mask）
    - 文件 mode 必须是 0o600
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        with HomeSandbox(tmp):
            env_backup = {}
            for k in ("ZEPP_PHONE", "ZEPP_PASSWORD", "ZEPP_DATA_DIR"):
                env_backup[k] = os.environ.pop(k, None)
            try:
                # 设置一个固定的"调用方 umask"，看 write_to_file 能否还原
                os.umask(0o022)
                pre_mask = os.umask(0o022)  # 探测 + 设 0o022
                # 真实调用前的 mask 就是 0o022
                # 包一层：用 patch 监控 os.umask 调用,确认 0o077 被传入
                import unittest.mock as _um
                umask_calls = []
                original_umask = os.umask
                def spy_umask(new):
                    umask_calls.append(new)
                    return original_umask(new)
                with _um.patch("os.umask", side_effect=spy_umask):
                    path = init_mod.write_to_file("pw")
                # 确认 0o077 在调用序列里出现过
                assert 0o077 in umask_calls, f"os.umask(0o077) never called; calls={umask_calls}"
                # 写完后当前 umask 应等于 pre (0o022)
                post = os.umask(0o022)
                assert post == pre_mask, f"umask after write_to_file = {oct(post)}, expected {oct(pre_mask)}"
                # 文件 mode 必须是 0o600
                file_mode = path.stat().st_mode & 0o777
                assert file_mode == 0o600, f"file mode {oct(file_mode)} != 0o600"
                print(f"  ✓ umask(0o077) called during write; restored to {oct(pre_mask)}; file=0o600")
            finally:
                for k, v in env_backup.items():
                    if v is not None:
                        os.environ[k] = v


# ============ 9. 永远不 print password ============

def test_never_print_password():
    """grep init.py 源码,确认没有任何 print(password) / print(pw) / echo password 等泄露。

    注意：
    - 跳过 Python 注释行（# 开头的行）
    - 跳过模块/函数 docstring 里的"绝无 print(password)"安全声明（这是设计意图描述,不是真调用）
    """
    src_path = Path(init_mod.__file__)
    src = src_path.read_text()

    # 1. 用 tokenize 剥掉所有注释 + docstring（更稳）,然后再 grep
    import tokenize
    import io as _io
    cleaned_lines = src.splitlines()  # 1-based

    # 找出所有 docstring 区间（三引号配对）
    in_doc = False
    doc_lines = set()
    triple_q = '"""'
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if not in_doc:
            # 模块 docstring 通常是文件第一个非空行; 函数 docstring 是 def 后第一行
            if s.startswith(triple_q) or (s.startswith("'''") and s.count("'''") == 1):
                in_doc = True
                if s.count(triple_q) >= 2 or s.count("'''") >= 2:
                    # 一行内开闭
                    in_doc = False
                doc_lines.add(i)
                continue
        else:
            doc_lines.add(i)
            if triple_q in s or "'''" in s:
                in_doc = False

    # 2. 跳过 # 注释
    def is_skip_line(i: int, line: str) -> bool:
        if i in doc_lines:
            return True
        if line.lstrip().startswith("#"):
            return True
        return False

    # 3. 危险模式
    bad_patterns = [
        re.compile(r"\bprint\s*\(\s*password\b"),                # print(password)
        re.compile(r"\bprint\s*\(\s*pw\b(?!\w)"),               # print(pw) 但不匹配 pw_len
        re.compile(r"\bprint\s*\(\s*f?['\"][^'\"]*\{password\}"),  # f"...{password}..."
        re.compile(r"\bprint\s*\(\s*f?['\"][^'\"]*\{pw\}"),
        re.compile(r"\bsubprocess\.(?:run|call|check_output).*password\s*="),
        re.compile(r"\blogger?\.(?:debug|info|warn|error).*password\s*=", re.I),
        re.compile(r"\bsys\.(?:stdout|stderr)\.write\s*\(\s*password"),
    ]
    violations = []
    for i, line in enumerate(src.splitlines(), 1):
        if is_skip_line(i, line):
            continue
        for pat in bad_patterns:
            if pat.search(line):
                violations.append((i, line.strip()))
    assert not violations, "found password-leaking lines:\n" + "\n".join(f"L{i}: {l}" for i, l in violations)

    # 4. 设计意图证据：read_password 周边应有"字符"字样
    assert "字符" in src, "no '字符' marker found — should print password LENGTH not value"

    # 5. 用 grep 二次确认（防止 tokenize 漏掉奇怪情况）
    import subprocess as _sp
    result = _sp.run(
        ["grep", "-nE", r"^\s*print\s*\(\s*(password|pw)\b", str(src_path)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0 or not result.stdout.strip(), \
        f"grep found password-leaking print:\n{result.stdout}"

    print(f"  ✓ no print(password)/print(pw)/echo password in init.py "
          f"(scanned {len(cleaned_lines)} lines, {len(doc_lines)} docstring lines skipped)")


# ============ 10. --mode env 写 .env ============

def test_mode_env_creates_dotenv():
    """write_to_env 写 ~/.zepp-data/.env (含 ZEPP_PHONE + ZEPP_PASSWORD, mode 600)。"""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        with HomeSandbox(tmp):
            env_backup = {}
            for k in ("ZEPP_PHONE", "ZEPP_PASSWORD", "ZEPP_DATA_DIR"):
                env_backup[k] = os.environ.pop(k, None)
            try:
                env_path = init_mod.write_to_env("186XXXXXXXX", "mysecret")
                assert env_path.exists(), f"{env_path} not created"
                content = env_path.read_text()
                assert "ZEPP_PHONE=186XXXXXXXX" in content
                assert "ZEPP_PASSWORD=mysecret" in content
                # 模式 600
                mode = env_path.stat().st_mode & 0o777
                assert mode == 0o600, f"env file mode {oct(mode)} != 0o600"
                # 再调一次应能更新（不重复行）
                env_path2 = init_mod.write_to_env("18600000000", "newpw")
                content2 = env_path2.read_text()
                assert content2.count("ZEPP_PHONE=") == 1, "duplicate ZEPP_PHONE"
                assert content2.count("ZEPP_PASSWORD=") == 1, "duplicate ZEPP_PASSWORD"
                assert "ZEPP_PHONE=18600000000" in content2
                assert "ZEPP_PASSWORD=newpw" in content2
                print(f"  ✓ write_to_env: .env created (mode 0o600), re-write idempotent")
            finally:
                for k, v in env_backup.items():
                    if v is not None:
                        os.environ[k] = v


# ============ 11. --mode file 写 ~/.zepp-password ============

def test_mode_file_creates_password_file():
    """write_to_file (--mode file 用的 helper) 写到 ~/.zepp-password, mode 600。"""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        with HomeSandbox(tmp):
            env_backup = {}
            for k in ("ZEPP_PHONE", "ZEPP_PASSWORD", "ZEPP_DATA_DIR"):
                env_backup[k] = os.environ.pop(k, None)
            try:
                path = init_mod.write_to_file("filepw")
                assert path == init_mod.PASSWORD_FILE_GLOBAL
                assert path.exists()
                assert path.read_text().strip() == "filepw"
                mode = path.stat().st_mode & 0o777
                assert mode == 0o600
                print(f"  ✓ write_to_file (--mode file): ~/.zepp-password created, mode 0o600")
            finally:
                for k, v in env_backup.items():
                    if v is not None:
                        os.environ[k] = v


# ============ 12. --rotate 保留 ZEPP_PHONE ============

def test_rotate_keeps_phone():
    """模拟 --rotate: state 里 phone 已设时,main 流程分支不应改 ZEPP_PHONE。

    我们不跑 main() (会触发 OAuth),而是验证：
    - detect_env() 读到的 phone 不被任何 helper 改写
    - write_to_file() 只动 password file,不动 phone
    - write_to_env() 显式收到 phone 参数才能写；只调 write_to_file() 时 phone 不变
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        with HomeSandbox(tmp):
            env_backup = {}
            for k in ("ZEPP_PHONE", "ZEPP_PASSWORD", "ZEPP_DATA_DIR"):
                env_backup[k] = os.environ.pop(k, None)
            try:
                # 模拟 --rotate 路径：phone 已设,只换 password
                os.environ["ZEPP_PHONE"] = "186XXXXXXXX"
                old_phone = init_mod.detect_env()["phone_masked"]
                assert old_phone == "186****XXXX"

                # 模拟 init.py main 在 --rotate 时执行 write_to_file('newpw')
                pw_path = init_mod.write_to_file("newpw")
                # phone env 应该原封不动
                assert os.environ["ZEPP_PHONE"] == "186XXXXXXXX"
                # password 文件被覆盖
                assert pw_path.read_text().strip() == "newpw"
                # detect_env 仍然能读到 phone
                new_state = init_mod.detect_env()
                assert new_state["phone_masked"] == "186****XXXX"
                assert new_state["phone_set"] is True
                print(f"  ✓ --rotate path: ZEPP_PHONE unchanged ({old_phone}); password overwritten")
            finally:
                for k, v in env_backup.items():
                    if v is not None:
                        os.environ[k] = v


# ============ 跑所有测试 ============

if __name__ == "__main__":
    tests = [
        test_status_no_config,
        test_status_full_config,
        test_detect_env_zehp_phone,
        test_detect_env_zehp_password,
        test_detect_password_file,
        test_detect_token,
        test_chmod_password_file_600,
        test_umask_before_write,
        test_never_print_password,
        test_mode_env_creates_dotenv,
        test_mode_file_creates_password_file,
        test_rotate_keeps_phone,
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