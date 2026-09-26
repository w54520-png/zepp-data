#!/usr/bin/env python3
"""
zepp-data 首次环境配置引导 —— 交互式助手

设计目标：
  - 零配置启动: oauth.py login / pull_to_sqlite.py sync 在缺 ZEPP_PHONE/ZEPP_PASSWORD
    时会自动调用本脚本进入引导
  - AI 看不到密码: 永远走 getpass（不回显、不进 stdout、不进 conversation）
  - 多路径都支持: env / file / 一次性 stdin
  - 菜单友好: A=环境变量 / B=本地文件（推荐） / C=临时输入 / D=跳过

安全约束（强制）:
  - 密码文件 chmod 600，写入前 os.umask(0o077)
  - 密码绝不入 conversation —— AI 仅调 init.py，stdout 只输出长度
  - 绝无 print(password) —— 只打印字符数
  - 不写日志文件

用法：
  python3 scripts/init.py                   # 缺什么引导什么
  python3 scripts/init.py --status          # 只看现状，不修改
  python3 scripts/init.py --force           # 强制重置（跳过检测）
  python3 scripts/init.py --rotate          # 轮换密码（保留 phone）
  python3 scripts/init.py --uninstall       # 删 token + .env + password file（保留 DB）
  python3 scripts/init.py --non-interactive # 非交互模式（仅检查）
  python3 scripts/init.py --mode env        # 跳过菜单直接写 env
  python3 scripts/init.py --mode file       # 跳过菜单直接写文件
  python3 scripts/init.py --mode once       # 跳过菜单只设进程 env
  python3 scripts/init.py --mode keep       # 维持当前状态

退出码:
   0 = OK（已配置 + OAuth 成功）
   1 = 缺关键信息或用户取消
   2 = OAuth 失败（密码错/网络风控）
   3 = 配置完成但 OAuth 未跑（--non-interactive / --status / --mode keep）
"""
from __future__ import annotations
import argparse
import getpass
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ===== 路径常量（与 zepp_oauth.py 保持一致）=====
DATA_DIR = Path(os.environ.get("ZEPP_DATA_DIR", str(Path.home() / ".zepp-data")))
SECRETS_DIR = DATA_DIR / ".secrets"
SECRETS_FILE = SECRETS_DIR / "token.json"
ENV_FILE = DATA_DIR / ".env"
PASSWORD_FILE_GLOBAL = Path.home() / ".zepp-password"

SCRIPT_DIR = Path(__file__).resolve().parent
OAUTH_SCRIPT = SCRIPT_DIR / "zepp_oauth.py"


# ====== [1. 检测环境] ======
def detect_env() -> dict:
    """返回当前环境快照（只读，不修改）。"""
    phone = os.environ.get("ZEPP_PHONE", "").strip()
    env_password = os.environ.get("ZEPP_PASSWORD", "")
    file_pw = PASSWORD_FILE_GLOBAL.exists()
    token_present = SECRETS_FILE.exists()

    token_meta = {}
    if token_present:
        try:
            token_meta = json.loads(SECRETS_FILE.read_text())
        except Exception:
            token_meta = {}

    return {
        "phone_set": bool(phone),
        "phone_masked": _mask_phone(phone) if phone else "",
        "env_password_set": bool(env_password),
        "env_password_len": len(env_password) if env_password else 0,
        "file_present": file_pw,
        "file_mode": _safe_stat_mode(PASSWORD_FILE_GLOBAL) if file_pw else "",
        "token_present": token_present,
        "token_user_id": str(token_meta.get("user_id", "")),
        "token_region": token_meta.get("region_host", ""),
        "token_extracted_at": token_meta.get("extracted_at", ""),
    }


def _mask_phone(phone: str) -> str:
    """手机号脱敏 186****XXXX 形式（占位符），避免整号泄露。"""
    if not phone:
        return ""
    p = phone.strip()
    if len(p) >= 7:
        return f"{p[:3]}****{p[-4:]}"
    return "***"


def _safe_stat_mode(path: Path) -> str:
    """stat mode 字符串，避免泄露文件内容。"""
    try:
        mode = path.stat().st_mode
        return oct(mode & 0o777)
    except OSError:
        return "?"


# ====== [2. 打印摘要] ======
def print_summary(state: dict, stream=sys.stdout) -> None:
    """打印当前环境状态摘要。"""
    print("=" * 60, file=stream)
    print("  Zepp 数据首次配置 — 当前状态", file=stream)
    print("=" * 60, file=stream)
    if state["phone_set"]:
        print(f"  ✓ ZEPP_PHONE={state['phone_masked']} 已设", file=stream)
    else:
        print(f"  ✗ 缺 ZEPP_PHONE（环境变量）", file=stream)

    if state["env_password_set"]:
        print(f"  ✓ ZEPP_PASSWORD 已设 ({state['env_password_len']} 字符)", file=stream)
    elif state["file_present"]:
        print(f"  ✓ 密码文件 {PASSWORD_FILE_GLOBAL} (mode {state['file_mode']}) 已建", file=stream)
    else:
        print(f"  ✗ 缺密码（环境变量 / 文件都没有）", file=stream)

    if state["token_present"]:
        days = _days_since(state["token_extracted_at"])
        print(
            f"  ✓ token.json 已存在"
            f" ({_safe_days_label(days)}前, region={state['token_region'] or '?'})",
            file=stream,
        )
    else:
        print(f"  ✗ 无 token（需要 OAuth 登录）", file=stream)
    print("=" * 60, file=stream)


def _days_since(date_str: str):
    """返回 token.extracted_at 距今多少天（容错返回 None）。"""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


def _safe_days_label(days):
    if days is None:
        return "?"
    if days == 0:
        return "今天"
    if days == 1:
        return "昨天"
    return f"{days} 天"


# ====== [3. 友好菜单] ======
MENU_PROMPT = """\
请选择密码保存方式（仅是存储偏好，不影响登录）:

  A. 环境变量   — 写 {env_file} (本机永久, 当前 shell 需 source)
  B. 本地文件   — 写 {pw_file} (chmod 600, 永久, 推荐日常)
  C. 临时输入   — 仅本次 Python 进程有效 (不写磁盘)
  D. 跳过       — 维持原状（仅做现状检查）

选择 [A/B/C/D]: """


def prompt_menu() -> str:
    """打印菜单并读取用户选择。"""
    if not sys.stdin.isatty():
        return "D"
    prompt = MENU_PROMPT.format(
        env_file=str(ENV_FILE),
        pw_file=str(PASSWORD_FILE_GLOBAL),
    )
    while True:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        try:
            raw = sys.stdin.readline()
        except EOFError:
            return "D"
        if not raw:
            return "D"
        choice = raw.strip().upper()
        if choice in ("A", "B", "C", "D"):
            return choice
        print(f"  无效输入 {raw.strip()!r}，请选 A/B/C/D")


# ====== [5. 接收密码 — 永远走 getpass] ======
def read_password() -> str:
    """走 getpass 不回显。仅打印长度。"""
    if not sys.stdin.isatty():
        return ""
    pw = getpass.getpass("请输入 Zepp 密码（输入不显示，屏幕不回显）: ")
    if not pw:
        print("  ✗ 密码为空")
        return ""
    # 🔒 绝无 print(password) —— 只打印长度
    print(f"  ✓ 已接收（{len(pw)} 字符，不会打印、不会进 conversation、不会进日志）")
    return pw


# ====== [4. 用户选完后] ======
def write_to_file(password: str) -> Path:
    """B 路径: 写本地文件 chmod 600 + umask 077。"""
    old_umask = os.umask(0o077)
    try:
        PASSWORD_FILE_GLOBAL.write_text(password + "\n")
        try:
            os.chmod(PASSWORD_FILE_GLOBAL, 0o600)
        except OSError as e:
            print(f"  ⚠ chmod 600 失败: {e}")
    finally:
        os.umask(old_umask)
    return PASSWORD_FILE_GLOBAL


def write_to_env(phone: str, password: str) -> Path:
    """A 路径: 写 ~/.zepp-data/.env。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)
    try:
        lines = []
        if DATA_DIR.joinpath(".env").exists():
            try:
                lines = DATA_DIR.joinpath(".env").read_text().splitlines()
            except Exception:
                lines = []
        # 替换已有的 ZEPP_PHONE / ZEPP_PASSWORD
        new_lines = []
        seen_phone = seen_pw = False
        for line in lines:
            if line.startswith("ZEPP_PHONE="):
                new_lines.append(f"ZEPP_PHONE={phone}")
                seen_phone = True
            elif line.startswith("ZEPP_PASSWORD="):
                new_lines.append(f"ZEPP_PASSWORD={password}")
                seen_pw = True
            else:
                new_lines.append(line)
        if not seen_phone:
            new_lines.append(f"ZEPP_PHONE={phone}")
        if not seen_pw:
            new_lines.append(f"ZEPP_PASSWORD={password}")
        DATA_DIR.joinpath(".env").write_text("\n".join(new_lines) + "\n")
        try:
            os.chmod(DATA_DIR / ".env", 0o600)
        except OSError:
            pass
    finally:
        os.umask(old_umask)
    return DATA_DIR / ".env"


def set_process_env(phone: str, password: str) -> None:
    """C 路径: 仅当前进程设置 os.environ，不写磁盘。"""
    if phone:
        os.environ["ZEPP_PHONE"] = phone
    if password:
        os.environ["ZEPP_PASSWORD"] = password


# ====== [6. 试 OAuth login] ======
def run_oauth_login(phone: str = "", password: str = "",
                     password_file: str = "") -> int:
    """调 zepp_oauth.py login, 转发退出码。
    返回 0 / 1 / 2.
    """
    if not OAUTH_SCRIPT.exists():
        print(f"  ✗ 找不到 {OAUTH_SCRIPT}")
        return 1
    argv = [sys.executable, str(OAUTH_SCRIPT), "login"]
    if phone:
        argv += ["--phone", phone]
    if password_file:
        argv += ["--password-file", password_file]
    # 用 Popen 但不传 password（走 zepp_oauth 自己的 env / 密码文件 / stdin 读取）
    env = os.environ.copy()
    if password and not password_file and not env.get("ZEPP_PASSWORD"):
        # C 路径：直接把当前进程的 ZEPP_PASSWORD 透传给子进程（不进 stdout）
        env["ZEPP_PASSWORD"] = password
    print()
    print("→ 调用 zepp_oauth.py login ...")
    print()
    try:
        result = subprocess.run(argv, env=env)
    except KeyboardInterrupt:
        print("\n  ✗ 用户中断")
        return 1
    return result.returncode


# ====== [7. 退出码处理] ======
def handle_oauth_exit(code: int) -> int:
    """根据 OAuth 退出码给出友好提示。"""
    print()
    print("=" * 60)
    if code == 0:
        print("  ✓ 已配好！")
        print("    跑 `python3 scripts/pull_to_sqlite.py sync --days 7` 即可")
        print("    或: python3 scripts/zepp_oauth.py status")
        return 0
    if code == 1:
        print("  ⚠ 密码错或账号问题（exit 1）")
        print()
        if sys.stdin.isatty():
            retry = input("  再试一次吗？[y/N]: ").strip().lower()
            if retry == "y":
                pw = read_password()
                if pw:
                    code2 = run_oauth_login(password=pw)
                    return handle_oauth_exit(code2)
        print("  提示: 跑 `python3 scripts/init.py --force` 重来，")
        print("        或在 Zepp App 重置密码后再试。")
        return 2
    if code == 2:
        print("  ⚠ OAuth 失败 exit 2 — 通常是 token 失效 / refresh 失败")
        print("    需重新走 OAuth login (会踢一次手机 Zepp App)。")
        print(f"    跑: rm {DATA_DIR}/.secrets/token.json && python3 scripts/init.py")
        return 2
    print(f"  ⚠ OAuth 失败 (exit {code}) — 网络/IP 风控或未知")
    print("    暂停 30 秒重试，或换网络（换 IP / 关 VPN）")
    return 2


# ====== 卸载 ======
def do_uninstall() -> int:
    """删 token + .env + password-file，保留 DB。"""
    removed = []
    for p, label in [
        (SECRETS_FILE, "token"),
        (ENV_FILE, ".env"),
        (DATA_DIR / ".env", ".env (alt path)"),
        (PASSWORD_FILE_GLOBAL, "password file"),
    ]:
        if p.exists():
            try:
                p.unlink()
                removed.append(label)
            except OSError as e:
                print(f"  ⚠ 删 {p} 失败: {e}")
    if removed:
        print(f"✓ 已删除: {', '.join(removed)}")
    else:
        print("✓ 没有需要删除的文件")
    db = DATA_DIR / "zepp.db"
    if db.exists():
        print(f"  保留 DB: {db}")
    return 0


# ====== 主流程（DAG 编排）======
def detect_readonly_home_dir() -> str | None:
    """如果默认 DATA_DIR（~/.zepp-data/）所在父目录不可写，返回建议。

    Return: None (OK) / "ZEPP_DATA_DIR" (需要设环境变量)
    """
    parent = DATA_DIR.parent
    try:
        # 用一个一次性 probe 测写权限（比 try mkdir 更轻量）
        probe = parent / f".zepp_write_probe_{os.getpid()}"
        try:
            probe.touch()
        except (PermissionError, OSError):
            return "ZEPP_DATA_DIR"
        else:
            probe.unlink(missing_ok=True)
            return None
    except Exception:
        return None


def main() -> int:
    p = argparse.ArgumentParser(
        description="zepp-data 首次环境配置引导",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--status", action="store_true", help="只显示现状，不修改")
    p.add_argument("--force", action="store_true", help="强制重置（跳过检测）")
    p.add_argument("--rotate", action="store_true", help="轮换密码（保留 phone）")
    p.add_argument("--uninstall", action="store_true", help="删 token + .env + 密码文件")
    p.add_argument("--non-interactive", action="store_true", help="非交互模式（仅检查）")
    p.add_argument(
        "--mode",
        choices=["env", "file", "once", "keep"],
        help="跳过菜单直接选路径（A=env B=file C=once D=keep）",
    )
    args = p.parse_args()

    # uninstall 提前处理
    if args.uninstall:
        return do_uninstall()

    # v4.0.3+：首次诊断 read-only home dir，提前给出环境变量推荐
    # 这样用户不用先撞 PermissionError 再去找答案
    readonly_advice = detect_readonly_home_dir()
    if readonly_advice and not os.environ.get("ZEPP_DATA_DIR"):
        print(f"⚠ 默认数据目录 {DATA_DIR} 不可写（父目录 {DATA_DIR.parent} 受系统保护）")
        print(f"  → 推荐设置环境变量指向可写位置：")
        print(f"      export ZEPP_DATA_DIR=/path/to/writable/dir")
        print(f"  → 详见 references/path-handling.md")
        print()

    # Step 1+2: 检测 + 摘要
    state = detect_env()
    print_summary(state)
    print()

    # Step --status: 只显示，退出
    if args.status:
        return 3 if not (state["phone_set"] and _any_password(state) and state["token_present"]) else 0

    # Step --mode keep: 维持原状，只检查不写
    if args.mode == "keep":
        if state["phone_set"] and _any_password(state) and state["token_present"]:
            return 3  # 配置完成，但 OAuth 不必重跑
        print("  ⚠ mode=keep 但环境不完整，缺什么:")
        if not state["phone_set"]:
            print("    - ZEPP_PHONE")
        if not _any_password(state):
            print("    - ZEPP_PASSWORD")
        if not state["token_present"]:
            print("    - token.json")
        return 1

    # --non-interactive: 仅检查, 缺关键信息给指引就退
    if args.non_interactive:
        missing = []
        if not state["phone_set"]:
            missing.append("ZEPP_PHONE")
        if not _any_password(state):
            missing.append("ZEPP_PASSWORD")
        if not state["token_present"]:
            missing.append("token.json")
        if missing:
            print(f"  ⚠ 非交互模式，环境不完整，缺: {', '.join(missing)}")
            print(f"    → 跑 `python3 scripts/init.py` 进入交互式引导")
            return 1
        print("  ✓ 非交互模式，环境完整，跳过 OAuth login")
        return 3

    # --rotate: 跳过菜单直接 C（一次性密码）
    if args.rotate:
        args.mode = "once"
        if not state["phone_set"]:
            print("  ✗ --rotate 需要先有 ZEPP_PHONE，请先跑 init.py")
            return 1

    # Step 3: 菜单（已配齐 + token 在则跳过菜单）
    if state["phone_set"] and _any_password(state) and state["token_present"] and not args.force:
        print("  ✓ 全部就绪 + token 存在，跳过交互引导。")
        print("    如需重置密码，跑 `init.py --force` 或 `--rotate`")
        return 0

    # 选路径
    if args.mode in ("env", "file", "once"):
        choice = {"env": "A", "file": "B", "once": "C"}[args.mode]
    else:
        choice = prompt_menu()

    if choice == "D":
        print("  → 维持原状，未做任何修改")
        return 3 if (state["phone_set"] and _any_password(state)) else 1

    # Step 4 + 5: 拿 phone / 拿 password
    phone = os.environ.get("ZEPP_PHONE", "").strip()
    if not phone or args.force:
        if not sys.stdin.isatty():
            print("  ✗ 缺 ZEPP_PHONE 且 stdin 不是 tty，无法交互输入")
            return 1
        raw = input("手机号（不带 +86，例如 186XXXXXXXX）: ").strip()
        if not raw:
            print("  ✗ 手机号为空")
            return 1
        phone = raw

    password = ""
    password_file_to_use = ""

    if choice == "B":
        # 写文件
        if not PASSWORD_FILE_GLOBAL.exists() or args.force:
            password = read_password()
            if not password:
                return 1
            path = write_to_file(password)
            print(f"  ✓ 已写 {path} (chmod 600)")
            # 写文件后清掉本次进程的 env，避免泄露
            os.environ.pop("ZEPP_PASSWORD", None)
            password = ""
        password_file_to_use = str(PASSWORD_FILE_GLOBAL)
    elif choice == "A":
        # 写 env 文件
        password = read_password()
        if not password:
            return 1
        env_path = write_to_env(phone, password)
        print(f"  ✓ 已写 {env_path}")
        print(f"  → 让当前 shell 生效: source {env_path}")
        # 同时设置本进程 env 以便 OAuth 透传
        os.environ["ZEPP_PHONE"] = phone
        os.environ["ZEPP_PASSWORD"] = password
        password = ""
    elif choice == "C":
        password = read_password()
        if not password:
            return 1
        set_process_env(phone, password)

    # Step 6: OAuth
    if choice == "B":
        code = run_oauth_login(phone=phone, password_file=password_file_to_use)
    else:
        code = run_oauth_login(phone=phone, password=password)

    return handle_oauth_exit(code)


def _any_password(state: dict) -> bool:
    return state["env_password_set"] or state["file_present"]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n✗ 用户中断")
        sys.exit(130)
