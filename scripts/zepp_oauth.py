#!/usr/bin/env python3
"""
Zepp Cloud OAuth 登录（中国服）—— 3 步流程

基于 miloce/Zepp-Life-Steps + Deep Research reverse-engineer。

工作流（按 universalLogin bundle 实现）：
  Step ① POST https://api-user.huami.com/registrations/{account}/tokens
         body: client_id, country_code, json_response, name, password,
               redirect_uri, state, token=access
         → access_token (从 redirectUri 的 ?access= 解析)

  Step ② POST https://api-mifit.zepp.com/v2/client/login
         body: grant_type=access_token, code=<access_token>, country_code,
               app_name=com.xiaomi.hm.health, third_name=huami, dn=..., ...
         → token_info.app_token + token_info.user_id + domains[]

  Step ③ GET https://api-mifit.zepp.com/v1/client/app_tokens?login_token=...
         → refresh / 确认 app_token (可选，step ② 已经有)

密码从 stdin / 环境变量 / 文件读取（不通过命令行参数，避免进 ps 历史）。

用法：
  # 密码从 stdin 读（推荐）
  python3 scripts/zepp_oauth.py login --phone 186XXXXXXXX

  # 密码从文件读（推荐 chmod 600）
  python3 scripts/zepp_oauth.py login --phone 186XXXXXXXX --password-file ~/.zepp-password

  # 密码从环境变量读
  ZEPP_PASSWORD=xxx python3 scripts/zepp_oauth.py login --phone 186XXXXXXXX

  # 邮箱登录
  python3 scripts/zepp_oauth.py login --email user@example.com
"""
from __future__ import annotations
import argparse
import getpass
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent))

# === Inline of missing login.py (was dropped on 2026-09-19) ===
DATA_DIR = Path(os.environ.get("ZEPP_DATA_DIR", str(Path.home() / ".zepp-data")))
SECRETS_DIR = DATA_DIR / ".secrets"
SECRETS_FILE = SECRETS_DIR / "token.json"


def _ensure_secrets_dir():
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(SECRETS_DIR, 0o700)
    except OSError:
        pass


def save_token(token: dict):
    """Save token to SECRETS_FILE (chmod 600)."""
    _ensure_secrets_dir()
    SECRETS_FILE.write_text(json.dumps(token, ensure_ascii=False, indent=2))
    try:
        os.chmod(SECRETS_FILE, 0o600)
    except OSError:
        pass
    return SECRETS_FILE


def token_exists() -> bool:
    return SECRETS_FILE.exists()


def _read_token():
    """读 token.json 解析为 dict。文件不存在 / 损坏时返 None。"""
    if not SECRETS_FILE.exists():
        return None
    try:
        return json.loads(SECRETS_FILE.read_text())
    except Exception:
        return None


def status_line() -> str:
    if not SECRETS_FILE.exists():
        return "未登录"
    try:
        t = json.loads(SECRETS_FILE.read_text())
    except Exception as e:
        return f"token 损坏：{e}"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    extracted = t.get("extracted_at", "?")
    region = t.get("region_host", "?")
    user = t.get("user_id", "?")
    return f"user_id={user}  region={region}  extracted_at={extracted}  today={today}"


# ===== Zepp OAuth endpoints（reverse-engineered）=====
REGISTRATION_URL = "https://api-user.huami.com/registrations/{account}/tokens"
CLIENT_LOGIN_URL = "https://api-mifit.zepp.com/v2/client/login"
APP_TOKEN_URL = "https://api-mifit.zepp.com/v1/client/app_tokens"

# ===== Headers / Defaults（取自 universalLogin bundle）=====
# Step ②③ 的 app_name：必须用 Mi Fit 退役 client id `com.xiaomi.hm.health`，
# 不能用 `com.huami.midong`（Zepp App 自己的包名）。
# Zepp 服务端按 (user_id, app_name) 维度做 session 隔离——用 `com.huami.midong` 登录会
# 把手机 Zepp App 的 session 踢下线。`com.xiaomi.hm.health` 是 Mi Fit 退役客户端，
# micw/hacking-mifit-api 等多个非官方 Huami API 客户端都用它，不会触发踢人。
APP_NAME = "com.xiaomi.hm.health"       # step ②③ (Mi Fit 退役 id, 不踢 Zepp App)
WEB_APP_NAME = "com.huami.webapp"        # step ①
CLIENT_ID = "HuaMi"                       # 硬编码 client_id
DEFAULT_DEVICE_ID = "2C8B4939-0CCD-4E94-8CBA-CB8EA6E613A1"  # Zepp App 默认
REDIRECT_URI = "https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html"
STATE = "REDIRECTION"
TOKEN_TYPE = "access"
GRANT_TYPE = "access_token"
DEFAULT_DN = "api-mifit.zepp.com,api-user.zepp.com,api-watch.zepp.com,app-analytics.zepp.com,auth.zepp.com,api-analytics.zepp.com"


def _urllib_post(url, data, headers=None):
    """POST form-encoded，返回 (status, json_body)。"""
    import urllib.parse, urllib.request, urllib.error
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body}


def _urllib_get(url, params, headers=None):
    """GET，返回 (status, json_body)。"""
    import urllib.parse, urllib.request, urllib.error
    full_url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full_url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body}


def _step1_get_access_code(account, password):
    """Step ①: POST /registrations/{account}/tokens → access_token"""
    url = REGISTRATION_URL.format(account=quote(account, safe=""))
    data = {
        "client_id": CLIENT_ID,
        "country_code": "CN",
        "json_response": "true",
        "name": account,
        "password": password,
        "redirect_uri": REDIRECT_URI,
        "state": STATE,
        "token": TOKEN_TYPE,
    }
    headers = {
        "app_name": WEB_APP_NAME,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://user.zepp.com",
        "Referer": "https://user.zepp.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    }
    status, body = _urllib_post(url, data, headers)
    if status != 200:
        return None, {"error_code": f"HTTP {status}", "stage": "step1", "response": body}
    if not body.get("ok"):
        ec = body.get("error")
        return None, {"error_code": f"01{ec}" if ec else "01??", "stage": "step1", "response": body}
    # access_token 在 redirectUri 的 ?access= query param
    parsed = urlparse(body.get("redirectUri", ""))
    access = parse_qs(parsed.query).get("access", [None])[0]
    if not access:
        # fallback：可能在 body 里
        access = body.get("access") or body.get("access_token")
    if not access:
        return None, {"error_code": "NO_ACCESS_TOKEN", "stage": "step1", "response": body}
    return access, None


def _step2_exchange_code(access_code, device_id):
    """Step ②: POST /v2/client/login → app_token + user_id"""
    data = {
        "allow_registration": "false",
        "app_name": APP_NAME,
        "app_version": "9.12.5",
        "code": access_code,
        "country_code": "CN",
        "device_id": device_id,
        "device_model": "android_phone",
        "dn": DEFAULT_DN,
        "grant_type": GRANT_TYPE,
        "lang": "zh",
        "source": "com.huami.watch.hmwatchmanager:9.12.5:151689",
        "third_name": "huami",
    }
    headers = {
        "app_name": APP_NAME,
        "appname": APP_NAME,
        "hm-privacy-ceip": "false",
        "x-request-id": str(uuid.uuid4()),
        "accept-language": "zh-CN",
        "User-Agent": "com.xiaomi.hm.health/1.0 (Android; MiFit)",
        "v": "2.0",
        "cv": "151689_9.12.5",
    }
    status, body = _urllib_post(CLIENT_LOGIN_URL, data, headers)
    if status != 200:
        return None, {"error_code": f"HTTP {status}", "stage": "step2", "response": body}
    if body.get("result") != "ok":
        return None, {"error_code": body.get("error_code", "?"), "stage": "step2", "response": body}
    ti = body.get("token_info") or {}
    if not ti.get("app_token") or not ti.get("user_id"):
        return None, {"error_code": "NO_TOKEN_INFO", "stage": "step2", "response": body}
    return ti, None


def _step3_refresh_app_token(login_token):
    """Step ③: GET /v1/client/app_tokens?login_token=... → 刷新 app_token"""
    params = {
        "app_name": APP_NAME,
        "dn": DEFAULT_DN,
        "login_token": login_token,
    }
    headers = {
        "app_name": APP_NAME,
        "hm-privacy-ceip": "false",
        "x-request-id": str(uuid.uuid4()),
        "accept-language": "zh-CN",
        "User-Agent": "com.xiaomi.hm.health/1.0 (Android; MiFit)",
        "v": "2.0",
        "cv": "151689_9.12.5",
    }
    status, body = _urllib_get(APP_TOKEN_URL, params, headers)
    if status != 200:
        return None, {"error_code": f"HTTP {status}", "stage": "step3", "response": body}
    ti = body.get("token_info") or {}
    return ti.get("app_token"), {"error_code": body.get("error_code"), "stage": "step3", "response": body}


def oauth_login(phone=None, email=None, password=None, device_id=None):
    """3 步 OAuth login。

    Returns (True, token_dict) 或 (False, error_dict)。
    """
    device_id = device_id or DEFAULT_DEVICE_ID
    if phone:
        # 国服手机号：去 +86 前缀
        phone_clean = str(phone).strip()
        if phone_clean.startswith("+86"):
            phone_clean = phone_clean[3:]
        elif phone_clean.startswith("86") and len(phone_clean) == 13:
            phone_clean = phone_clean[2:]
        account = f"+86{phone_clean}"
    elif email:
        account = email.strip()
    else:
        raise ValueError("phone 或 email 必须二选一")

    # Step ①
    access, err = _step1_get_access_code(account, password)
    if err:
        return False, err

    # Step ②
    ti, err = _step2_exchange_code(access, device_id)
    if err:
        return False, err

    # Step ③（可选 refresh）
    login_token = ti.get("login_token")
    if login_token:
        refreshed, _ = _step3_refresh_app_token(login_token)
        if refreshed:
            ti["app_token"] = refreshed

    # 解析 region_host
    region_host = "https://api-mifit-cn3.zepp.com"
    for d in ti.get("domains") or []:
        if isinstance(d, dict) and d.get("host") == "api-mifit.zepp.com":
            cn = d.get("cnames") or []
            if cn:
                region_host = "https://" + cn[0]
                break

    return True, {
        "app_token": ti["app_token"],
        "login_token": ti.get("login_token"),
        "user_id": str(ti["user_id"]),
        "region_host": region_host,
        "ttl": ti.get("ttl"),
        "app_ttl": ti.get("app_ttl"),
        "extracted_at": _today(),
        "extraction_method": "zepp_oauth_3step_cli",
    }


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def cmd_login(args):
    """login 子命令。"""
    if token_exists():
        print(f"已登录 — {status_line()}")
        print(f"  如需重新登录，先删 {SECRETS_FILE}")
        return 0

    # 0. 自动引导: 缺 ZEPP_PHONE / ZEPP_PASSWORD 时调 init.py（仅交互终端）
    _missing_creds = (
        not (args.phone or args.email)
        or not (
            args.password_file
            or os.environ.get("ZEPP_PASSWORD")
            or Path.home().joinpath(".zepp-password").exists()
        )
    )
    if _missing_creds and sys.stdin.isatty() and not os.environ.get("ZEPP_NO_BOOTSTRAP"):
        init_script = Path(__file__).parent / "init.py"
        if init_script.exists():
            print("→ 检测到缺凭据，启动首次配置引导（init.py）...")
            print()
            rc = subprocess.run(
                [sys.executable, str(init_script), "--non-interactive"]
            ).returncode
            if rc == 0:
                return 0  # 引导成功
            if rc == 3:
                # 环境完整但 OAuth 未跑（用户保持现状），继续往下走
                pass
            elif rc == 1:
                # 缺关键信息，引导已给出指引
                return 1
            elif rc in (2,):
                # 引导内部 OAuth 失败
                print(f"  ✗ init.py 引导 OAuth 失败 (exit {rc})")
                return rc

    # 1. 密码
    password = None
    if args.password_file:
        p = Path(args.password_file)
        if not p.exists():
            print(f"✗ 密码文件不存在：{p}")
            return 1
        password = p.read_text().rstrip("\n")
        print(f"✓ 密码从 {p} 读取（{len(password)} 字符，不打印）")
    elif os.environ.get("ZEPP_PASSWORD"):
        password = os.environ["ZEPP_PASSWORD"]
        print(f"✓ 密码从 ZEPP_PASSWORD 环境变量读取（{len(password)} 字符）")
    else:
        if not sys.stdin.isatty():
            print("✗ stdin 不是 terminal，请用 --password-file 或 ZEPP_PASSWORD")
            return 1
        print("请输入 Zepp 密码（输入不显示）：")
        password = getpass.getpass("Password: ")
        if not password:
            print("✗ 密码为空")
            return 1

    # 2. 账号
    if not args.phone and not args.email:
        print("✗ 必须提供 --phone 或 --email")
        return 1

    # 3. OAuth
    identifier = args.phone or args.email
    print(f"正在登录 {identifier} (国服)...")
    print()
    print("Step ①: 密码登录拿 access_token...")
    ok, result = oauth_login(phone=args.phone, email=args.email,
                             password=password, device_id=args.device_id)
    if not ok:
        ec = result.get("error_code", "?")
        stage = result.get("stage", "?")
        msg = result.get("response", {}).get("message", "")
        print(f"✗ 登录失败 ({stage}): error_code={ec}  {msg}")
        hints = {
            # Step ① - 密码登录拿 access_token
            "01401": "[Step ① 密码登录] 密码错或账号不存在（error=401）。检查 ZEPP_PASSWORD 是否正确",
            "01403": "[Step ① 密码登录] 账号被封 / 风控（error=403）。换网络或等 30s 重试",
            "01404": "[Step ① 密码登录] 账号不存在（error=404）。检查 ZEPP_PHONE 是否正确",
            "01408": "[Step ① 密码登录] 密码格式不对（error=408）。Zepp 要求 8-30 位数字+字母+至少 1 个特殊字符（如 . _ -）",
            "NO_ACCESS_TOKEN": "[Step ① 密码登录] 服务器没返回 access_token（可能 IP 被风控）。换 VPN 再试",

            # Step ② - 用 access_code 换 app_token
            "NO_TOKEN_INFO": "[Step ② 换 token] 没返回 token_info。检查 access_token 是否有效",

            # Step ③ - app_token 续期（refresh 命令也可能撞到）
            "STEP3_FAIL": "[Step ③ 续 app_token] 续期失败。跑 `python3 scripts/init.py --rotate` 重新登录",

            # 网络 / 通用
            "HTTP 400": "[网络] 请求格式错（检查 phone 格式或 client_id）",
            "HTTP 403": "[网络] IP 风控（换网络或换 VPN）",

            # refresh token 失效（跨阶段兜底）
            "0101": "[refresh token] token 真的过期。跑 `python3 scripts/init.py --rotate` 重新登录",
        }
        for k, v in hints.items():
            if ec.startswith(k) or k == ec:
                print(f"  提示：{v}")
        # 通用兜底：如果 error_code 没命中 hints，把 stage / message 完整打出来帮排查
        if not any(ec.startswith(k) or k == ec for k in hints):
            print(f"  提示：未在已知映射里，请把上面 error_code 报给 LLM 排查")
        return 1

    token = result
    print("✓ Step ①+②+③ 全部成功")
    print(f"  user_id:     {token['user_id']}")
    print(f"  region:      {token['region_host']}")
    print(f"  ttl:         {token.get('ttl')}s ({token.get('ttl', 0)//86400} 天)")
    print(f"  app_ttl:     {token.get('app_ttl')}s ({token.get('app_ttl', 0)//86400} 天)")
    print(f"  app_token:   {token['app_token'][:20]}... (不打印完整)")

    save_path = save_token(token)
    print(f"  saved:       {save_path} (chmod 600)")
    return 0


def cmd_status(args):
    print(status_line())


def cmd_refresh(args):
    """用现有 login_token 续 app_token（不踢手机 App）。

    仅当 login_token 过期（理论上 ~30 天一次）才需要重新 OAuth。
    """
    if not SECRETS_FILE.exists():
        print(f"✗ 没有 token.json，请先跑 login")
        return 1

    tok = json.loads(SECRETS_FILE.read_text())
    login_token = tok.get("login_token") or tok.get("app_token")
    if not login_token:
        print("✗ token.json 里没 login_token / app_token，无法 refresh")
        return 1

    print("→ Step ③ refresh app_token (不踢 App)...")
    refreshed, meta = _step3_refresh_app_token(login_token)
    if not refreshed:
        ec = meta.get("error_code", "?")
        print(f"✗ refresh 失败 (Step ③): error_code={ec}")
        print(f"  提示：[Step ③ 续 app_token] login_token 已失效，无法续期。需要重新走完整 OAuth login")
        print(f"  → 跑: python3 scripts/zepp_oauth.py login --phone {tok.get('user_id','')}")
        return 2  # 特殊退出码 = "需要完整 OAuth"

    old_token = tok.get("app_token")
    tok["app_token"] = refreshed
    tok["last_refresh"] = datetime.now(timezone.utc).isoformat()
    save_token(tok)

    same = old_token == refreshed
    print(f"✓ refresh 成功")
    print(f"  app_token: {'(未变 — Zepp 不 rotate)' if same else '已 rotate'}")
    print(f"  last_refresh: {tok['last_refresh']}")
    print(f"  💡 不会踢手机 Zepp App — 放心")
    return 0


def cmd_verify(args):
    """检查 token 状态。

    默认：本地 token 文件检查（不联网 — 适合 cron / 离线）
      - token.json 存在？
      - 字段完整性（app_token / login_token / region_host）
      - token.json mtime vs Zepp 默认 app_ttl=30 天（推断剩余天数）

    可选 --remote：额外调 auth.huami.com/oauth2/verify 端点
      注意：此端点只接受第三方 app 的 access_token，对 app_token 永远 401。
      这是预期行为 — 保留 --remote 让用户能看 HTTP 响应。

    退出码：
      0 = token 文件齐全 + 未过期
      1 = token 文件不存在
      2 = token 文件过期（mtime > 30 天）
      3 = 缺关键字段
      4 = 远程验证失败（仅 --remote 模式）
    """
    import time
    import urllib.error
    import urllib.request

    token_path = SECRETS_FILE
    if not token_path.exists():
        print("✗ token.json 不存在")
        print(f"  预期路径: {token_path}")
        print("  → 跑: python3 scripts/zepp_oauth.py login --phone <号>")
        return 1

    try:
        with open(token_path) as f:
            token = json.load(f)
    except Exception as e:
        print(f"✗ token.json 解析失败: {e}")
        print(f"  路径: {token_path}")
        return 3

    # 字段完整性检查
    required = ['app_token', 'login_token', 'region_host']
    missing = [k for k in required if not token.get(k)]
    if missing:
        print(f"✗ token.json 缺字段: {missing}")
        print(f"  路径: {token_path}")
        return 3

    print("=== Token 本地检查 ===")
    print(f"  user_id: {token.get('user_id', '?')}")
    print(f"  region:  {token.get('region_host', '?')}")
    app_ttl = token.get('app_ttl')
    if app_ttl:
        print(f"  app_ttl: {app_ttl} 秒 ({app_ttl // 86400} 天)")
    else:
        print(f"  app_ttl: ? (字段缺失 — 旧 token？)")

    # mtime 检查
    mtime = token_path.stat().st_mtime
    age_sec = time.time() - mtime
    age_days = age_sec / 86400
    print(f"  token.json 年龄: {age_days:.1f} 天")

    # 推断过期
    expired = False
    if app_ttl:
        remaining_days = max(0, (app_ttl - age_sec) / 86400)
        print(f"  剩余: {remaining_days:.1f} 天（基于 app_ttl 推断）")
        if remaining_days <= 0:
            expired = True
    if age_days > 30:
        print(f"  ⚠️ token 超过 30 天没更新（Zepp 默认 app_ttl=30 天）—— 可能过期")
        expired = True

    # 可选远程验证
    remote_failed = False
    if getattr(args, "remote", False):
        print()
        print("=== 远程验证 (auth.huami.com/oauth2/verify) ===")
        url = "https://auth.huami.com/oauth2/verify"
        headers = {
            "Authorization": f"Bearer {token['app_token']}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        req = urllib.request.Request(url, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode()
                print(f"  HTTP {resp.status}: {body[:200]}")
                if resp.status != 200:
                    remote_failed = True
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200] if e.fp else ""
            print(f"  HTTP {e.code}: {body}")
            if e.code == 401:
                print("  ⚠️ 401 是预期：auth.huami.com 不认 app_token")
                print("    （此端点只接受第三方 app 的 access_token）")
                print("    → skill 用的 app_token 需直接调 api-mifit.zepp.com 数据端点")
            remote_failed = True
        except Exception as e:
            print(f"  错误: {e}")
            remote_failed = True

    print()
    if remote_failed:
        return 4
    if expired:
        return 2
    print("✓ token 本地检查通过")
    print("  💡 本地检查通过 ≠ Zepp Cloud 接受（要看真反应：跑 sync 看 API 返不返数据）")
    return 0


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="OAuth 登录（3 步流程；**不踢**手机 Zepp App，依赖 APP_NAME=com.xiaomi.hm.health）")
    p_login.add_argument("--phone", help="国服手机号（11 位裸数字，代码自动加 +86 前缀；国际服用 --email）")
    p_login.add_argument("--email", help="国际服账号邮箱（不需要 --phone）")
    p_login.add_argument("--password-file", help="密码文件（chmod 600）")
    p_login.add_argument("--device-id", help="自定义 device_id")
    p_login.set_defaults(func=cmd_login)

    p_status = sub.add_parser("status", help="查看登录状态")
    p_status.set_defaults(func=cmd_status)

    p_refresh = sub.add_parser("refresh", help="续 app_token（不踢手机 App）")
    p_refresh.set_defaults(func=cmd_refresh)

    p_verify = sub.add_parser("verify", help="检查 token 状态（默认本地检查）")
    p_verify.add_argument("--remote", action="store_true", help="额外调 auth.huami.com verify 端点")
    p_verify.set_defaults(func=cmd_verify)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())