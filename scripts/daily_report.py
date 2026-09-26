#!/usr/bin/env python3
"""
M9：健康日报生成脚本

职责：
  1. 自动 sync（拉最新数据 → DB）
  2. 从 SQLite 读 data + 渲染 markdown 日报
  3. 输出到 stdout 或指定文件
  4. 可选 webhook 推送（企业微信 / 飞书 / Slack）

用法：
  python3 scripts/daily_report.py                       # 默认 t-1 = 昨天
  python3 scripts/daily_report.py --date 2026-09-25     # 指定日期
  python3 scripts/daily_report.py --out report.md       # 写文件
  python3 scripts/daily_report.py --webhook "$URL"      # 推送到 webhook

设计：
  - 复用 tests/generate_daily_report_sample.py 的查询 + 渲染逻辑（提取成模块函数）
  - init 引导：缺 ZEPP_PHONE 时调 init.py（仿 pull_to_sqlite.py 模式）
  - token 检查 + refresh + sync（M7 silent_reauth 模式）—— 日报必须用最新数据
  - sync 用 --days 7（覆盖当天 + 前 6 天，留余量）
  - 默认 t-1：不传 --date 自动算昨天

BMR 算法：Mifflin-St Jeor（学界标准）
  男：10 × W + 6.25 × H - 5 × A + 5
  女：10 × W + 6.25 × H - 5 × A - 161
  数据来源：user_profile.height + user_profile.birthday + 最新 weight 测量
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

# ===== 路径常量 =====
_SKILL_DIR = Path(__file__).resolve().parent
_TEST_DIR = _SKILL_DIR.parent / "tests"
sys.path.insert(0, str(_TEST_DIR))   # 让 import generate_daily_report_sample 生效

# 复用查询 + 渲染逻辑
from generate_daily_report_sample import (  # noqa: E402
    load_data,
    build_report,
    mifflin_bmr,
    calculate_age_from_birthday,
    query_user_profile,
)

# 复用 skill 自带的 time_utils
sys.path.insert(0, str(_SKILL_DIR))
from time_utils import DEFAULT_TZ  # noqa: E402

DB_PATH = Path(os.environ.get("ZEPP_DATA_DIR", str(Path.home() / ".zepp-data"))) / "zepp.db"
PULL_SCRIPT = _SKILL_DIR / "pull_to_sqlite.py"
OAUTH_SCRIPT = _SKILL_DIR / "zepp_oauth.py"
INIT_SCRIPT = _SKILL_DIR / "init.py"


# ===== 引导：缺 ZEPP_PHONE 走 init.py =====
def ensure_phone_env() -> bool:
    """保证 ZEPP_PHONE 已设置；缺则调 init.py 引导用户。

    返回 True = OK（已设置或成功引导），False = 用户跳过 / 引导失败。
    """
    if os.environ.get("ZEPP_PHONE"):
        return True
    print("⚠️  ZEPP_PHONE 未设置，触发 init.py 引导...", flush=True)
    if not INIT_SCRIPT.exists():
        print(f"❌ 找不到 {INIT_SCRIPT}")
        return False
    try:
        rc = subprocess.run(
            [sys.executable, str(INIT_SCRIPT), "--non-interactive"],
            text=True, timeout=60,
        ).returncode
    except subprocess.TimeoutExpired:
        print("⚠️  init.py 超时（60s）")
        return False
    if rc != 0:
        print(f"⚠️  init.py 退出码 {rc}（密码可能未设置）")
        return False
    return bool(os.environ.get("ZEPP_PHONE"))


# ===== Sync 前自动 refresh token（M7 silent_reauth 模式）=====
def refresh_token() -> bool:
    """sync 前调 zepp_oauth.py refresh；失败不 abort，让 sync 自己兜底。

    返回 True = 继续走 sync（refresh 成功 / 失败但不算 critical）。
    """
    if not OAUTH_SCRIPT.exists():
        print(f"⚠️  找不到 {OAUTH_SCRIPT}，跳过 refresh（sync 阶段兜底）")
        return True
    print("=== Step 0: refresh token (防 sync 时 token 失效) ===", flush=True)
    try:
        proc = subprocess.run(
            [sys.executable, str(OAUTH_SCRIPT), "refresh"],
            text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("  ⚠️ refresh 超时（30s）—— 继续 sync", flush=True)
        return True
    except Exception as e:
        print(f"  ⚠️ refresh 异常 ({type(e).__name__}: {e}) —— 继续 sync", flush=True)
        return True
    rc = proc.returncode
    if rc == 0:
        return True
    # exit 2 = login_token 过期 → 提示用户跑 init.py
    if rc == 2:
        print("  ⚠️ refresh 失败（exit 2，login_token 过期）", flush=True)
        print("    请跑 `python3 scripts/init.py --rotate` 重新 OAuth", flush=True)
    else:
        print(f"  ⚠️ refresh 失败（exit {rc}）—— 继续 sync（让 sync 兜底）", flush=True)
    return True


# ===== Sync 拉数据 =====
def sync(days: int = 7) -> bool:
    """拉最近 N 天数据。days=7 覆盖当天 + 前 6 天（日报通常查 t-1）。

    返回 True = sync 成功（或部分成功），False = sync 失败但日报仍可生成（用旧数据）。
    """
    if not PULL_SCRIPT.exists():
        print(f"⚠️  找不到 {PULL_SCRIPT}，跳过 sync")
        return False
    print(f"=== Step 1: sync --days {days} (覆盖 t-1) ===", flush=True)
    try:
        proc = subprocess.run(
            [sys.executable, str(PULL_SCRIPT), "sync", "--days", str(days)],
            text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        print("  ⚠️ sync 超时（10min）", flush=True)
        return False
    except Exception as e:
        print(f"  ⚠️ sync 异常 ({type(e).__name__}: {e})", flush=True)
        return False
    rc = proc.returncode
    if rc != 0:
        print(f"  ⚠️ sync 退出码 {rc}（日报仍会生成，但数据可能不是最新）", flush=True)
        return False
    return True


# ===== Webhook 推送 =====
def push_webhook(url: str, body: str) -> bool:
    """POST 日报到 webhook（企业微信 / 飞书 / Slack 都收 markdown）。

    返回 True = HTTP 2xx，False = 失败。
    """
    try:
        import urllib.request
        import json as _json
        req = urllib.request.Request(
            url, data=_json.dumps({"markdown": body, "text": body[:200]}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = 200 <= resp.status < 300
            if ok:
                print(f"✅ webhook 推送成功（HTTP {resp.status}）", flush=True)
            else:
                print(f"⚠️  webhook HTTP {resp.status}", flush=True)
            return ok
    except Exception as e:
        print(f"⚠️  webhook 推送失败：{type(e).__name__}: {e}", flush=True)
        return False


# ===== 主流程 =====
def main():
    p = argparse.ArgumentParser(description="健康日报生成（M9）")
    p.add_argument("--date", default=None,
                   help=f"目标日期 YYYY-MM-DD（默认 t-1 = {(date.today() - timedelta(days=1))}）")
    p.add_argument("--out", default=None,
                   help="输出文件路径（默认 stdout）")
    p.add_argument("--webhook", default=None,
                   help="Webhook URL（企业微信 / 飞书 / Slack）")
    p.add_argument("--no-sync", action="store_true",
                   help="跳过 sync（用现有 DB 数据生成日报）")
    p.add_argument("--sync-days", type=int, default=7,
                   help="sync 拉取天数（默认 7，覆盖 t-1）")
    args = p.parse_args()

    # 默认 t-1
    if args.date is None:
        target_date = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        target_date = args.date

    # 1. 引导（缺 ZEPP_PHONE）
    ensure_phone_env()

    # 2. refresh + sync（除非显式 --no-sync）
    if not args.no_sync:
        refresh_token()
        sync(days=args.sync_days)
    else:
        print("=== Step 1: --no-sync 跳过 ===", flush=True)

    # 3. 生成日报
    print(f"=== Step 2: 生成日报 {target_date} ===", flush=True)
    data = load_data(target_date)
    report = build_report(data, target_date)

    # 4. 输出
    if args.out:
        out_path = Path(args.out)
        out_path.write_text(report, encoding="utf-8")
        print(f"✅ 写到 {out_path}", flush=True)
    else:
        print(report)

    # 5. webhook
    if args.webhook:
        push_webhook(args.webhook, report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())