#!/usr/bin/env python3
"""
Zepp 数据同步 + 落库（增量 / 全量）

子命令：
  sync   拉 Zepp 数据并落 SQLite（默认 7 天增量）
  init   仅创建 SQLite 数据库（schema 内置在 storage.py）

用法：
  python3 pull_to_sqlite.py sync                # 增量最近 7 天
  python3 pull_to_sqlite.py sync --days 90      # 增量 90 天
  python3 pull_to_sqlite.py sync --days 0       # 3 年全量
  python3 pull_to_sqlite.py init                # 只创建 DB
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from zepp_client import ZeppClient, now_ms, days_ago_ms
from zepp_client import ZeppError, NeedsReauth, Unavailable, NetworkError, RetryExhausted
from storage import (
    init_db, upsert_raw_record, upsert_metric_sample, upsert_daily_metric,
    update_capability, set_meta, counts, NORMALIZER_REVISION,
)
# M5 章节 5.5：统一时间工具（替代散落的 datetime.fromtimestamp 调用，避免单位混用 bug）
# M7 章节：新增 epoch_now_sec / epoch_now_ms 给 refresh 状态 + 数据上传时间算 age 用
from time_utils import ms_to_local_dt, sec_to_local_dt, fmt_local_dt, parse_local_date, epoch_now_sec, epoch_now_ms

# 默认 ~/.zepp-data/，可用 ZEPP_DATA_DIR 覆盖
DATA_DIR = Path(os.environ.get("ZEPP_DATA_DIR", str(Path.home() / ".zepp-data")))
SECRETS_DIR = DATA_DIR / ".secrets"
SECRETS_FILE = SECRETS_DIR / "token.json"
from normalizer.wellness import (
    normalize_hrv_rmssd, normalize_spo2, normalize_pai,
    normalize_all_day_stress, normalize_hybrid_charge,
    normalize_sport_load, normalize_daily_summary, normalize_respiratory_rate,
    normalize_lactate_threshold, normalize_heart_rate,
    normalize_second_heart_rate,
)
# M4 章节 4.2：Food 流
try:
    from normalizer.food import normalize_food
except ImportError:
    normalize_food = None
# M4 章节 4.4：VO2_MAX 流
try:
    from normalizer.vo2_max import normalize_vo2_max
except ImportError:
    normalize_vo2_max = None
from normalizer.sleep import normalize_band_sleep
from normalizer.body import normalize_weight


DEFAULT_DB = DATA_DIR / "zepp.db"


# ===== M5：错误可见性 — 错误分类 =====
# 把内层 ZeppError 归到 3 类（critical_auth / network_service / soft_unavailable），
# 让 cmd_sync 顶层能聚合输出 🔴 CRITICAL / 🟡 WARNING 块。
# 设计原则：
#   - 不抛 SystemExit / 不立即 abort — 让 cmd_sync 跑完所有流再聚合（用户能看到全景）
#   - 保留旧 behavior：单个流失败不影响其他流
#   - 兼容所有现有 fetch_error 字符串（嗅探式 fallback，避免破坏现有测试）
_ERROR_CATEGORY_CRITICAL_AUTH = "critical_auth"   # 401/403 — token 失效
_ERROR_CATEGORY_NETWORK = "network"               # URLError/Timeout/OSError/RetryExhausted
_ERROR_CATEGORY_UNAVAILABLE = "unavailable"       # 404 — 端点不存在
_ERROR_CATEGORY_UNKNOWN = "unknown"               # 其他异常（normalize_error 等）

# ===== M6：401 静默续期 =====
# 设计原则：401 是用户的"内部实现细节"，不暴露给用户。
#   - sync 跑完后扫 error_counts，遇到 critical_auth → 静默调 zepp_oauth.py refresh / login
#   - 所有 reauth 输出写 log file，stdout 完全静默
#   - 最多重试 1 次（避免无限循环）
#   - 重试后仍 401 → 升级到 Critical 提示用户（兜底，正常情况不会再有）
_REAUTH_LOG_DIR = DATA_DIR / "log"
_REAUTH_LOG_FILE = _REAUTH_LOG_DIR / "reauth.log"
_MAX_REAUTH_PER_SYNC = 1  # 最多重试 1 次


def _reauth_log(msg: str) -> None:
    """写 reauth 日志到 log file（不输出到 stdout）。

    log file 位置: ~/.zepp-data/log/reauth.log（DATA_DIR 可被 ZEPP_DATA_DIR 覆盖）
    """
    try:
        _REAUTH_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with open(_REAUTH_LOG_FILE, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        # log 写入失败不影响 reauth 主流程
        pass


def _silent_reauth(reason: str = "401 from sync") -> bool:
    """401 触发后静默续期（用户不可见）。

    1. 先调 `zepp_oauth.py refresh`（如果有 login_token，不踢手机 App）
    2. refresh 失败 → 调 `zepp_oauth.py login`（需要 ZEPP_PHONE + ZEPP_PASSWORD）
    3. 失败 → 返 False（外层升级到 Critical 提示用户）
    4. 成功 → 静默返 True，外层重跑 sync

    所有 stdout / print 重定向到 /dev/null；详细信息写 reauth.log 供调试。
    """
    oauth_script = Path(__file__).parent / "zepp_oauth.py"
    if not oauth_script.exists():
        _reauth_log(f"silent_reauth 失败：找不到 {oauth_script}")
        return False

    _reauth_log(f"开始 silent_reauth (reason={reason})")

    # === Step 1：试 refresh（用 login_token 续 app_token，不踢手机 App）===
    try:
        result = subprocess.run(
            [sys.executable, str(oauth_script), "refresh"],
            text=True, timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            _reauth_log(f"silent_reauth 成功（refresh path）— {reason}")
            return True
        # refresh 失败 — 把 stderr 写 log（不进 stdout）
        stderr = (result.stderr or "")[:500]
        _reauth_log(f"refresh 失败 (exit {result.returncode}): {stderr}")
    except subprocess.TimeoutExpired:
        _reauth_log("refresh 超时（30s）— 跳到 login")
    except Exception as e:
        _reauth_log(f"refresh 异常：{type(e).__name__}: {e}")

    # === Step 2：refresh 失败 → 走完整 OAuth login ===
    # 需要 ZEPP_PHONE + ZEPP_PASSWORD（密码从 env / file / stdin）
    phone = os.environ.get("ZEPP_PHONE", "")
    if not phone:
        _reauth_log("silent_reauth 失败：refresh 失败但 ZEPP_PHONE 未设置 — 无法 login")
        return False

    try:
        login_argv = [sys.executable, str(oauth_script), "login", "--phone", phone]
        # 如果有 ZEPP_PASSWORD_FILE，加进参数让 zepp_oauth 从文件读密码
        pw_file = os.environ.get("ZEPP_PASSWORD_FILE", "")
        if pw_file:
            login_argv.extend(["--password-file", pw_file])
        result = subprocess.run(
            login_argv,
            text=True, timeout=60,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            _reauth_log("silent_reauth 成功（login path）")
            return True
        stderr = (result.stderr or "")[:500]
        _reauth_log(f"login 失败 (exit {result.returncode}): {stderr}")
    except subprocess.TimeoutExpired:
        _reauth_log("login 超时（60s）")
    except Exception as e:
        _reauth_log(f"login 异常：{type(e).__name__}: {e}")

    _reauth_log(f"silent_reauth 全部失败 — 返 False，提示用户手动跑 init.py")
    return False


def _classify_error(err_msg: str, err_obj=None) -> str:
    """
    把异常消息（或异常对象）分类到 4 个 category 之一。

    优先看 err_obj 的类型（如果是 ZeppError 子类，直接归类）；
    否则嗅探 err_msg 的关键词（兼容 fetch_error 字符串 fallback）。
    """
    if err_obj is not None:
        if isinstance(err_obj, NeedsReauth):
            return _ERROR_CATEGORY_CRITICAL_AUTH
        if isinstance(err_obj, (NetworkError, RetryExhausted)):
            return _ERROR_CATEGORY_NETWORK
        if isinstance(err_obj, Unavailable):
            return _ERROR_CATEGORY_UNAVAILABLE
    # fallback：嗅探字符串（兼容老代码路径，异常被 str() 之后丢类型的情况）
    if not err_msg:
        return _ERROR_CATEGORY_UNKNOWN
    low = err_msg.lower()
    if "http 401" in low or "http 403" in low or "unauthorized" in low:
        return _ERROR_CATEGORY_CRITICAL_AUTH
    if "http 404" in low or "not found" in low:
        return _ERROR_CATEGORY_UNAVAILABLE
    if ("urlerror" in low or "timeout" in low or "timed out" in low
            or "temporary failure" in low or "name resolution" in low
            or "connection" in low or "reset" in low
            or "retryexhausted" in low or "http 5" in low or "http 429" in low):
        return _ERROR_CATEGORY_NETWORK
    return _ERROR_CATEGORY_UNKNOWN


def _category_emoji(category: str) -> str:
    """给分类返回 emoji 标签。"""
    return {
        _ERROR_CATEGORY_CRITICAL_AUTH: "🔴 CRITICAL",
        _ERROR_CATEGORY_NETWORK: "🟡 WARNING",
        _ERROR_CATEGORY_UNAVAILABLE: "⚠️ UNAVAIL",
        _ERROR_CATEGORY_UNKNOWN: "⚠️ ERROR",
    }.get(category, "⚠️ ERROR")


def _category_hint(category: str) -> str:
    """给分类返回用户友好的修复提示。"""
    return {
        _ERROR_CATEGORY_CRITICAL_AUTH:
            "Zepp Cloud 返 HTTP 401/403 — token 失效或过期\n"
            "  跑: python3 scripts/init.py  --rotate\n"
            "  或: python3 scripts/zepp_oauth.py login --phone \"$ZEPP_PHONE\"",
        _ERROR_CATEGORY_NETWORK:
            "网络问题或 Zepp 服务端 5xx/429 — 检查网络连接 / 等几分钟后重试",
        _ERROR_CATEGORY_UNAVAILABLE:
            "端点 404 — 可能端点下线或你的账号没有该权限",
        _ERROR_CATEGORY_UNKNOWN:
            "未知异常 — 看上面的 ✗ 行详细错误",
    }.get(category, "未知错误")


# ===== M7：sync 前自动 refresh + 数据上传时间展示 =====

# zepp_oauth.py refresh 退出码语义（来自 zepp_oauth.py::cmd_refresh）：
#   exit 0 = refresh 成功（app_token 已 rotate / 未变）
#   exit 1 = token 文件不存在 / 缺字段（本地问题，不算网络/认证失效）
#   exit 2 = refresh 失败（login_token 失效，需要重新完整 OAuth）
_REFRESH_EXIT_OK = 0
_REFRESH_EXIT_LOCAL_FAIL = 1
_REFRESH_EXIT_NEEDS_FULL_OAUTH = 2


def _format_age_str(age_sec: float) -> str:
    """M7：把秒数差转成人类可读字符串。

    设计原则：
      - < 0 → "未来时间（请检查时区）"（防止 silent_reauth 重跑导致 age 略微负值）
      - < 60 秒 → "N 秒前"
      - < 3600 秒 → "N 分钟前"
      - < 86400 秒 → "X.Y 小时前"（保留 1 位小数 —— "3.5 小时前" 比 "3 小时前" 更准）
      - >= 86400 秒 → "X.Y 天前"
    """
    if age_sec < 0:
        return "未来时间（请检查时区）"
    if age_sec < 60:
        return f"{int(age_sec)} 秒前"
    if age_sec < 3600:
        return f"{int(age_sec / 60)} 分钟前"
    if age_sec < 86400:
        return f"{age_sec / 3600:.1f} 小时前"
    return f"{age_sec / 86400:.1f} 天前"


def _maybe_refresh_token_before_sync() -> bool:
    """M7：sync 前自动 refresh token（避免 token 过期 → sync 失败 → 0 条 → 误以为是数据问题）。

    返回：True = 继续走 sync（refresh 成功 / 失败但不算 critical）；False = 不要走 sync。

    行为：
      - exit 0：refresh 成功，输出对用户可见（zepp_oauth.py 自己会打 "已 rotate"）
      - exit 2：login_token 过期 → 调 _silent_reauth() 尝试走完整 OAuth 兜底
      - exit 1 / 其他：本地问题（token 文件不存在 / 缺字段），不 abort —— 让后续 sync 报 401
        然后 M6 silent_reauth 兜底（避免双层逻辑重叠）

    设计权衡：
      - 为什么不在这里直接 abort？—— 因为 refresh 失败不等于 sync 失败；sync 阶段即使
        refresh 失败也可能拿到部分数据。让后续 M6 silent_reauth 统一兜底，逻辑分层更清晰。
      - 为什么用 subprocess 而不是直接 import zepp_oauth？—— 因为 cmd_refresh 会写
        token.json，subprocess 让 zepp_oauth 自己管理 token 写入 + 退出码，避免 pts
        直接依赖 zepp_oauth 模块内部细节。
    """
    oauth_script = Path(__file__).parent / "zepp_oauth.py"
    if not oauth_script.exists():
        print(f"  ⚠️ 找不到 {oauth_script}，跳过 refresh（sync 阶段兜底）")
        return True

    print("=== Step 0: refresh token (防 sync 时 token 失效) ===")
    try:
        proc = subprocess.run(
            [sys.executable, str(oauth_script), "refresh"],
            # capture_output=False：refresh 输出对用户可见（"已 rotate" 等提示用户能看到）
            capture_output=False,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("  ⚠️ refresh 超时（30s）—— 继续 sync（M6 silent reauth 兜底）")
        return True
    except Exception as e:
        print(f"  ⚠️ refresh 异常 ({type(e).__name__}: {e}) —— 继续 sync")
        return True

    rc = proc.returncode
    if rc == _REFRESH_EXIT_OK:
        # 成功 —— refresh 输出已对用户可见（zepp_oauth.py 自己打的）
        return True
    if rc == _REFRESH_EXIT_NEEDS_FULL_OAUTH:
        # exit 2 = login_token 过期 → 静默触发完整 OAuth（M6 _silent_reauth）
        print("  ⚠️ refresh 失败（exit 2，login_token 过期）→ 触发 silent_reauth")
        ok = _silent_reauth(reason="refresh failed before sync (exit 2)")
        if not ok:
            print("  ⚠️ silent_reauth 也失败 —— 继续 sync（M6 silent reauth 兜底）")
        return True
    # exit 1 / 其他 = 本地问题；不 abort，让 sync 跑 + M6 silent reauth 兜底
    print(f"  ⚠️ refresh 失败（exit {rc}）—— 继续 sync（M6 silent reauth 兜底）")
    return True


def _compute_data_upload_time(db) -> tuple[str | None, str | None, str | None]:
    """M7：从 DB 算"数据上传时间"展示三件套。

    返回 (timestamp_str, age_str, error_msg)：
      - timestamp_str / age_str 任意一个为 None → 没数据，不展示
      - error_msg 仅用于调试（不会打到 stdout）

    数据源：measurements.ts_ms（毫秒）—— 与 counts()::latest 一致；
    比 raw_records.end_ts_ms 更准（用户看到的 metric 最新时间）。
    """
    try:
        row = db.execute(
            "SELECT MAX(ts_ms) AS mx FROM measurements WHERE ts_ms > 0"
        ).fetchone()
    except Exception as e:
        return None, None, f"query failed: {e}"
    latest_ms = row["mx"] if row else None
    if not latest_ms:
        return None, None, "no measurements with ts_ms > 0"
    now_dt = ms_to_local_dt(epoch_now_sec() * 1000)
    latest_dt = ms_to_local_dt(latest_ms)
    if latest_dt is None:
        return None, None, f"ms_to_local_dt returned None for {latest_ms}"
    age_sec = (now_dt - latest_dt).total_seconds()
    return fmt_local_dt(latest_dt), _format_age_str(age_sec), None


STREAMS = [
    ("hrv_sdnn",        "v2_events", "hrv_sdnn",        "real_data",   None),
    ("hrv_rmssd",       "v2_events", "HRVRMSSD",        "real_data",   normalize_hrv_rmssd),
    ("stress",          "v2_events", "Charge",          "stress_data", None),
    ("hybrid_charge",   "v2_events", "Charge",          "insight_data",normalize_hybrid_charge),
    ("respiratory_rate","v2_events", "RespiratoryRate", "real_data",   normalize_respiratory_rate),
    ("daily_summary",   "v2_events", "DailyHealth",     "summary",     normalize_daily_summary),
    ("second_heart_rate","v2_events","second_heart_rate","real_data",  normalize_second_heart_rate),
    ("blood_pressure",  "v2_events", "blood_pressure",  "real_data",   None),
    ("lactate_threshold","v2_events","LactateThreshold", "summary",    normalize_lactate_threshold),
    ("emotion",         "v2_events", "Emotion",         "real_data",   None),
    ("food",            "v2_events", "Food",            None,          normalize_food),
    ("all_day_stress",  "user_events","all_day_stress", None,         normalize_all_day_stress),
    ("blood_oxygen",    "user_events","blood_oxygen",   None,         normalize_spo2),
    ("pai",             "user_events","PaiHealthInfo",  None,         normalize_pai),
    ("sport_load",      "watch_stat","SPORT_LOAD",      None,         normalize_sport_load),
    ("vo2_max",         "watch_stat","VO2_MAX",         None,         normalize_vo2_max),
    ("devices",         "static",    None,              None,         None),
    ("members",         "static",    None,              None,         None),
    ("heart_rate_auto", "heart_rate", None,             None,         None),
    # ===== M2 章节 2.1 睡眠流 =====
    ("band_sleep",      "band_data", None,              None,         None),   # 特殊 surface，自定义处理
    # ===== M2 章节 2.2 体重流 =====
    ("weight",          "weight",    None,              None,         None),   # 特殊 surface，自定义处理
    # ===== M3 章节 3.1 workout 详情 =====
    ("workout_detail",  "workout_detail", None,         None,         None),   # 特殊 surface，遍历 workouts 表拉详情
]


def fetch_and_normalize(client, label, surface, event_type, sub_type,
                         normalize_fn, from_ms, to_ms, db, user_id):
    """拉一次 + 落 raw_records + 规范化 + 落 measurements。"""
    try:
        if surface == "v2_events":
            data = client.v2_events(event_type, sub_type, from_ms, to_ms, limit=2000, reverse=True)
        elif surface == "user_events":
            data = client.user_events(event_type, sub_type, from_ms, to_ms, limit=2000, reverse=True)
        elif surface == "watch_stat":
            tz = timezone(timedelta(hours=8))
            # M5 章节 5.5：from_ms/to_ms 是毫秒，用 ms_to_local_dt 转换（避免散落 /1000）
            start_day = fmt_local_dt(ms_to_local_dt(from_ms, tz=tz), "%Y-%m-%d")
            end_day = fmt_local_dt(ms_to_local_dt(to_ms, tz=tz), "%Y-%m-%d")
            data = client.watch_statistics(event_type, start_day, end_day, limit=500, reverse=True)
        elif surface == "heart_rate":
            # client.heart_rate 单位是**秒**（自动检测毫秒但显式传秒更清晰）
            data = client.heart_rate(int(from_ms / 1000), int(to_ms / 1000),
                                      limit=2000, hr_type=2)
        elif surface == "static":
            data = client.devices() if label == "devices" else client.members()
        elif surface == "band_data":
            # M2 章节 2.1：睡眠 — 调用两次（summary + detail）
            tz_cn = timezone(timedelta(hours=8))
            # M5 章节 5.5：from_ms/to_ms 是毫秒，用 ms_to_local_dt 转换
            from_date = fmt_local_dt(ms_to_local_dt(from_ms, tz=tz_cn), "%Y-%m-%d")
            to_date = fmt_local_dt(ms_to_local_dt(to_ms, tz=tz_cn), "%Y-%m-%d")
            # 先拉 summary，7 天切片失败保留
            summary_data = client.band_data(from_date, to_date, query_type='summary',
                                             byte_length=8, device_type=0)
            # 再拉 detail（detail 同范围）
            detail_data = None
            try:
                detail_data = client.band_data(from_date, to_date, query_type='detail',
                                                byte_length=8, device_type=0)
            except Exception as e:
                print(f"      [band_data detail] {e}（仅写 summary，不阻塞）", flush=True)
            # 合并：summary 是骨架，detail 提供 data_hr
            # 修复 bug：返回实际写入的 sessions/stages/hr/raw 数；
            # 保留 outer "✓ 0 records" 不变（band_sleep 写 sleep_sessions 表，不写 measurements 表），
            # 但在 _ingest_band_sleep 内部会打 "written: sessions=N stages=M hr=K" 行让用户能直观看到
            ing = _ingest_band_sleep(db, client, user_id, summary_data, detail_data)
            return {"records": 0, "diags": ing["diags"], "status": "available",
                    "raw_count": ing["raw"], "last_ts_ms": None,
                    "sleep_written": ing}
        elif surface == "weight":
            # M2 章节 2.2：体重 / 体成分 — 按年切片
            _ingest_weight(db, client, user_id, from_ms, to_ms)
            return {"records": 0, "diags": 0, "status": "available",
                    "raw_count": 0, "last_ts_ms": None}
        elif surface == "workout_detail":
            # M3 章节 3.1：workout 详情 — 遍历 workouts 表拉详情
            _ingest_workout_detail(db, client, user_id, from_ms, to_ms)
            return {"records": 0, "diags": 0, "status": "available",
                    "raw_count": 0, "last_ts_ms": None}
        else:
            return {"records": 0, "diags": 0, "status": "unsupported"}
    except Exception as e:
        # 外层 fetch 异常 — 用 _classify_error 决定 category（critical_auth / network / unavailable）
        category = _classify_error(str(e), err_obj=e)
        return {"records": 0, "diags": 0, "status": "fetch_error", "error": str(e),
                "error_category": category, "error_type": type(e).__name__}

    items = data.get("items", []) if isinstance(data, dict) else []
    if not items:
        return {"records": 0, "diags": 0, "status": "no_records"}

    source_key_base = f"{surface}:{event_type or '_'}:{sub_type or '_'}:{from_ms}-{to_ms}"
    last_ts = None
    for idx, item in enumerate(items):
        sk = f"{source_key_base}#{idx}"
        from normalizer.common import device_id, first_number
        dev = device_id(item) if isinstance(item, dict) else None
        ts_ms = None
        if isinstance(item, dict):
            ts_ms = first_number(item, ("timestamp", "updateTime", "time"))
            if ts_ms is not None:
                ts_ms = int(ts_ms)
        upsert_raw_record(db, sk, label, event_type, sub_type, surface,
                          ts_ms, None, dev, user_id, item)
        if ts_ms and (last_ts is None or ts_ms > last_ts):
            last_ts = ts_ms
    db.commit()

    if normalize_fn is None:
        # devices / members 静态流 → 写 devices / user_profile 表
        if surface == "static" and label == "devices":
            from storage import upsert_device
            for it in items:
                upsert_device(db, it)
            db.commit()
            return {"records": len(items), "diags": 0, "status": "available_no_normalizer",
                    "raw_count": len(items), "last_ts_ms": last_ts}
        if surface == "static" and label == "members":
            from storage import upsert_user_profile
            n_profiles = 0
            for it in items:
                upsert_user_profile(db, it, source_key_base)
                n_profiles += 1
            db.commit()
            return {"records": n_profiles, "diags": 0, "status": "available_no_normalizer",
                    "raw_count": len(items), "last_ts_ms": last_ts}
        return {"records": 0, "diags": 0, "status": "available_no_normalizer",
                "raw_count": len(items), "last_ts_ms": last_ts}

    try:
        batch = normalize_fn(data)
    except Exception as e:
        # normalize 阶段异常 — 不分类（不是网络/认证问题，是 normalizer bug）
        return {"records": 0, "diags": 0, "status": "normalize_error", "error": str(e),
                "error_category": _ERROR_CATEGORY_UNKNOWN, "error_type": type(e).__name__}

    from normalizer.common import MetricSample, DailyMetric
    written = 0
    write_errors = 0
    for r in batch.records:
        try:
            if isinstance(r, MetricSample):
                upsert_metric_sample(db, r, user_id, source_key_base)
            elif isinstance(r, DailyMetric):
                upsert_daily_metric(db, r, user_id, source_key_base)
            written += 1
        except Exception as e:
            write_errors += 1
            if write_errors <= 3:
                print(f"      [write error] {type(r).__name__} {r.metric}: {e}", flush=True)
    db.commit()

    status = "available" if batch.records else "no_records"
    return {"records": written, "diags": len(batch.diagnostics),
            "status": status, "raw_count": len(items), "last_ts_ms": last_ts}


# ===== M2 章节 2.1 / 2.2 专用 ingestion =====

def _ingest_band_sleep(db, client, user_id, summary_data, detail_data) -> dict:
    """
    把 summary + detail 合并后解析，写 sleep_sessions/stages/hr_samples。

    合并策略：按 date_time 索引 detail（detail 提供 data_hr），按 date_time 索引 summary。
    同一天优先取 detail，没有 detail 时退到 summary。

    返回：{"sessions": N, "stages": M, "hr": K, "raw": R, "diags": D}
      - sessions / stages / hr 是实际写入 DB 的条数（被 upsert 接受）
      - raw 是 raw_records 落库条数（detail + summary）
      - diags 是 normalizer 的诊断数

    注：band_sleep 块不返回到 cmd_sync 的 "records" 字段，因为 record/measurement
    是另一类表（measurements.stream），sleep 用专门的 sleep_sessions 表。
    这里把真实的 sessions/stages/hr 数返回，供外层 sync 报告用。
    """
    from storage import (
        upsert_sleep_session, upsert_sleep_stage_slice, upsert_band_hr_sample,
        upsert_raw_record,
    )
    from normalizer.sleep import normalize_band_sleep

    # 1) 先落 raw_records（detail + summary 都落，但优先 detail）
    summary_items = summary_data if isinstance(summary_data, list) else (summary_data.get("items", []) if isinstance(summary_data, dict) else [])
    detail_items = detail_data if isinstance(detail_data, list) else (detail_data.get("items", []) if isinstance(detail_data, dict) else [])

    raw_written = 0
    # 写 detail raw（优先）
    for idx, item in enumerate(detail_items):
        if not isinstance(item, dict):
            continue
        from normalizer.common import first_number
        sk = f"band_data:detail:{item.get('date_time','')}_{idx}"
        ts_ms = None
        if isinstance(item, dict):
            ts_ms = int(item.get('st') * 1000) if isinstance(item.get('st'), (int, float)) else None
        upsert_raw_record(db, sk, "band_sleep", None, None, "band_data",
                          ts_ms, None, str(item.get('source') or ''), user_id, item)
        raw_written += 1

    # 写 summary raw（fallback）
    for idx, item in enumerate(summary_items):
        if not isinstance(item, dict):
            continue
        sk = f"band_data:summary:{item.get('date_time','')}_{idx}"
        upsert_raw_record(db, sk, "band_sleep", None, None, "band_data",
                          None, None, str(item.get('source') or ''), user_id, item)
        raw_written += 1
    db.commit()

    # 2) 合并：detail 优先（detail 包含 data_hr）
    detail_by_date = {it.get("date_time"): it for it in detail_items if isinstance(it, dict)}
    merged = []
    seen_dates = set()
    for it in detail_items:
        d = it.get("date_time")
        if d and d not in seen_dates:
            merged.append(it)
            seen_dates.add(d)
    for it in summary_items:
        d = it.get("date_time")
        if d and d not in seen_dates:
            merged.append(it)
            seen_dates.add(d)
    if not merged:
        print(f"      [band_sleep] raw={raw_written} sessions=0 stages=0 hr=0 diags=0 (no merged items)", flush=True)
        return {"sessions": 0, "stages": 0, "hr": 0, "raw": raw_written, "diags": 0}

    # 3) normalize
    batch = normalize_band_sleep(merged)

    # 4) 写 sessions + 回填 stage session_id
    written_sessions = 0
    written_stages = 0
    written_hr = 0
    for sess in batch.sessions:
        sess_dict = {
            "user_id": user_id,
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
        # 修复 bug：upsert_sleep_session 现在缺字段会抛 ValueError（之前是 silent None）
        # 这里捕获后写入 batch.diagnostics，让用户能看到具体哪个 session 缺了什么
        try:
            sid = upsert_sleep_session(db, sess_dict, raw_source_key="band_sleep")
            if sid:
                written_sessions += 1
        except ValueError as e:
            batch.diagnostics.append(
                f"{sess.date}: upsert_sleep_session 跳过 — {e}"
            )
        except Exception as e:
            # 其他异常（如 UNIQUE 冲突之外的 sqlite 错误）也写入 diagnostic，不让 sync 崩
            batch.diagnostics.append(
                f"{sess.date}: upsert_sleep_session 异常 ({type(e).__name__}): {e}"
            )

    # 处理 odd_stage 标记：sess.is_nap 在 normalizer 内部标注

    # 5) 写 stages
    for stg in batch.stages:
        stg_dict = {
            "session_id": None,
            "date": stg.date,
            "start_ts": stg.start_ts,
            "end_ts": stg.end_ts,
            "start_min": stg.start_min,
            "end_min": stg.end_min,
            "mode_code": stg.mode_code,
            "mode": stg.mode,
            "tz_offset_secs": stg.tz_offset_secs,
        }
        upsert_sleep_stage_slice(db, stg_dict, raw_source_key="band_sleep")
        written_stages += 1

    # 6) 写逐分钟 HR
    for hr in batch.hr_samples:
        hr_dict = {
            "date": hr.date,
            "ts": hr.ts,
            "bpm": hr.bpm,
            "tz_offset_secs": hr.tz_offset_secs,
        }
        upsert_band_hr_sample(db, hr_dict, raw_source_key="band_sleep")
        written_hr += 1

    db.commit()
    # 输出"实际写入"计数（含 raw + sessions + stages + hr），让用户能看到 band_sleep 块到底写了什么
    # 修复 bug：之前只输出 sessions=N，但外层 sync 把 records=0 显示成 "✓ 0 records"——用户看不出区别
    print(
        f"      [band_sleep] raw={raw_written} sessions={written_sessions} "
        f"stages={written_stages} hr={written_hr} diags={len(batch.diagnostics)}",
        flush=True,
    )
    # 第二行：明确"实际写入"的 sessions/stages 数（按用户预期格式）
    #   sessions 和 stages 是 sleep 库的核心；hr 通常很多，放后面不影响阅读
    #   即使 raw>0 但 sessions=0 也能一眼看出"raw 拉到了，但没解析出 session"——常见原因是 stage 空
    print(
        f"  written: sessions={written_sessions} stages={written_stages}",
        flush=True,
    )
    if batch.diagnostics:
        for d in batch.diagnostics[:5]:
            print(f"        • {d}", flush=True)
        if len(batch.diagnostics) > 5:
            print(f"        … ({len(batch.diagnostics) - 5} more)", flush=True)

    return {
        "sessions": written_sessions,
        "stages": written_stages,
        "hr": written_hr,
        "raw": raw_written,
        "diags": len(batch.diagnostics),
    }


def _ingest_weight(db, client, user_id, from_ms, to_ms) -> None:
    """
    按年切片拉 weight_records → 写 measurements 表（stream='weight'）。

    M2 章节 2.2 决策：写到 measurements 表 stream='weight'，每个 metric 一个 stream 子类。
    """
    from datetime import datetime as _dt, timezone as _tz
    from storage import upsert_metric_sample, upsert_raw_record
    from normalizer.body import normalize_weight

    # 按年切片（M2 拍板）：避免一次拉 N 年导致 API 返回截断
    tz_cn = _tz(_dt.now(_tz.utc).astimezone().utcoffset() or _dt.resolution)
    start_dt = _dt.fromtimestamp(from_ms / 1000.0, tz=_tz.utc)
    end_dt = _dt.fromtimestamp(to_ms / 1000.0, tz=_tz.utc)
    start_year = start_dt.year
    end_year = end_dt.year

    written = 0
    written_raw = 0
    diag_total: list = []
    for yr in range(start_year, end_year + 1):
        y_from = int(_dt(yr, 1, 1, tzinfo=_tz.utc).timestamp())
        y_to = int(_dt(yr + 1, 1, 1, tzinfo=_tz.utc).timestamp()) - 1
        # 限制到实际请求窗口
        y_from = max(y_from, int(from_ms / 1000))
        y_to = min(y_to, int(to_ms / 1000))
        if y_to < y_from:
            continue
        try:
            data = client.weight_records(member_id="-1", from_seconds=y_from, to_seconds=y_to, limit=300)
        except Exception as e:
            diag_total.append(f"weight {yr}: fetch error ({e})")
            continue
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            continue
        # 落 raw
        from normalizer.common import first_number
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            sk = f"weight:{yr}:{idx}"
            ts_ms = None
            ts_s = first_number(item, ("generatedTime", "generateTime", "time", "timestamp"))
            if ts_s is not None:
                # generatedTime 是秒（不是 ms）→ ×1000
                ts_ms = int(ts_s * 1000)
            upsert_raw_record(db, sk, "weight", None, None, "weight",
                              ts_ms, None, None, user_id, item)
            written_raw += 1
        db.commit()

        # normalize
        batch = normalize_weight(data)
        for r in batch.records:
            try:
                # Q2 拍板：weight 流所有 metric 都用 stream='weight'
                upsert_metric_sample(db, r, user_id, raw_source_key=f"weight:{yr}",
                                      stream_override="weight")
                written += 1
            except Exception as e:
                diag_total.append(f"weight {yr}: write error {e}")
        diag_total.extend(batch.diagnostics)
    db.commit()
    print(f"      [weight] raw={written_raw} measurements={written} diags={len(diag_total)}", flush=True)
    if diag_total:
        for d in diag_total[:5]:
            print(f"        • {d}", flush=True)


# M4 章节 4.1：workout_detail 限流应对常量
WORKOUT_DETAIL_THROTTLE_MS = 200      # 两次 detail 请求间隔（避免触发限流）
WORKOUT_DETAIL_RETRY_BACKOFF_MS = 200 # 失败重试前的等待
WORKOUT_DETAIL_NO_GPS_FEATURE_MAX = 100  # feature ≤ 此值视为无 GPS（健走/室内跑等）

# M4 章节 4.1：诊断计数器（按类目分桶）
_workout_detail_diag = {
    "no_data_silent": 0,   # code=1 但 data 缺失（无 detail）
    "fetch_error": 0,      # 网络/限流错误
    "retry_success": 0,    # 重试后成功
    "no_gps_skipped": 0,   # feature 小，无 GPS，跳过
    "ok": 0,               # 正常拉取并入库
}


def _read_workout_feature(db, trackid):
    """从 workouts.raw_payload JSON 读 feature 字段（无 GPS 探测用）。

    feature 是 Zepp history 端点每条 workout 的标志位：
      - 小数字（< 100，如 85）→ 无 GPS（健走、骑行等只需步频）
      - 包含 GPS 标记 → 含 GPS（跑步/越野等）
    raw_payload 缺失返回 None（按有 GPS 尝试）。
    """
    import json
    row = db.execute(
        "SELECT raw_payload FROM workouts WHERE trackid = ?",
        (str(trackid),),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        d = json.loads(row[0])
    except Exception:
        return None
    return d.get("feature")


def _fetch_workout_detail_with_retry(client, trackid):
    """
    M4 章节 4.1：拉 detail.json，区分 3 种响应：

    ① {"code": 1, "data": {...}}     → 正常返回 data
    ② {"code": 1}（无 data）         → 返回 ("no_data", None)
    ③ {"code": 0/其他} 或 HTTP 404    → 抛出 ZeppError（让上层重试）
    ④ 网络异常 / 限流 (429, 5xx)     → 抛出原异常，由外层 RetryExhausted 处理

    失败重试 1 次（间隔 200ms）。
    """
    from zepp_client import CloudRejected, Unavailable, NetworkError, RetryExhausted, ZeppError
    last_err = None
    for attempt in range(2):
        try:
            data = client.sport_detail(trackid, source="gpx")
            if not isinstance(data, dict):
                # 期望 dict 但收到 list/None → 视为 no_data
                return ("no_data", None)
            # code=1 + 有 data → 成功
            code = data.get("code")
            inner = data.get("data")
            if code in (1, "1") and isinstance(inner, dict) and inner:
                return ("ok", data)
            # code=1 但无 data → no_data（健走等无 detail）
            if code in (1, "1") and not inner:
                return ("no_data", data)
            # code != 1 → 业务错误，重试一次
            last_err = CloudRejected(code or -1, data.get("message", ""))
            time.sleep(WORKOUT_DETAIL_RETRY_BACKOFF_MS / 1000)
            continue
        except (CloudRejected, Unavailable) as e:
            # 业务层失败 / 404 → 整个跳过（M4 4.1 决策：code=0 时不重试太深）
            raise ZeppError(f"workout_detail {trackid}: {e}") from e
        except (NetworkError, RetryExhausted) as e:
            # 网络/限流 → 留上层自然重试
            last_err = e
            time.sleep(WORKOUT_DETAIL_RETRY_BACKOFF_MS / 1000)
            continue
    # 两次都失败
    if last_err:
        raise last_err
    return ("no_data", None)


def _ingest_workout_detail(db, client, user_id, from_ms, to_ms) -> None:
    """
    M3 章节 3.1：遍历 workouts 表，对每个 trackid 拉 detail.json，解析并写 6 张表。
    M4 章节 4.1：限流应对
      - 区分 "code=1 无 data"（no_data 静默跳过）vs 业务错误（raise 跳过）
      - 重试 1 次（间隔 200ms）
      - 每次请求间隔 200ms（避免触发 rate limit）
      - 检查 feature 字段（≤100 视为无 GPS，跳过 detail 请求）
    """
    from storage import (
        upsert_workout_route_point, upsert_workout_sample, upsert_workout_split,
        upsert_workout_lap, upsert_workout_pause, upsert_workout_hr_drift,
        delete_workout_detail, upsert_raw_record,
    )
    from normalizer.workout_detail import decode_workout_detail
    from zepp_client import ZeppError

    from_s = int(from_ms / 1000)
    to_s = int(to_ms / 1000)
    rows = db.execute("""
        SELECT trackid, end_time_ts, dis_m FROM workouts
        WHERE end_time_ts BETWEEN ? AND ?
        ORDER BY end_time_ts DESC
    """, (from_s, to_s)).fetchall()
    if not rows:
        print("      [workout_detail] no workouts in window")
        return

    written_route = written_samples = written_splits = written_laps = 0
    written_pauses = written_drift = 0
    errors = []
    seen_trackids = set()
    # 计数重置（M4 4.1）
    for k in _workout_detail_diag:
        _workout_detail_diag[k] = 0

    is_first = True
    for trackid, end_time_ts, dis_m in rows:
        if trackid in seen_trackids:
            continue
        seen_trackids.add(trackid)

        # M4 4.1：feature 探测 —— 小数字（≤ 100）说明无 GPS，省一次 detail 请求
        feature = _read_workout_feature(db, trackid)
        if feature is not None and feature <= WORKOUT_DETAIL_NO_GPS_FEATURE_MAX:
            _workout_detail_diag["no_gps_skipped"] += 1
            if not is_first:
                time.sleep(WORKOUT_DETAIL_THROTTLE_MS / 1000)
            is_first = False
            continue

        # 限流：每次请求间隔 200ms（首个请求不延迟）
        if not is_first:
            time.sleep(WORKOUT_DETAIL_THROTTLE_MS / 1000)
        is_first = False

        try:
            status, data = _fetch_workout_detail_with_retry(client, trackid)
        except ZeppError as e:
            errors.append(f"{trackid}: {e}")
            _workout_detail_diag["fetch_error"] += 1
            continue
        except Exception as e:
            errors.append(f"{trackid}: fetch error ({e})")
            _workout_detail_diag["fetch_error"] += 1
            continue

        if status == "no_data":
            # code=1 但无 data —— 静默跳过（健走等常见）
            _workout_detail_diag["no_data_silent"] += 1
            continue

        # status == "ok"
        _workout_detail_diag["ok"] += 1

        # 落 raw
        sk = f"workout_detail:{trackid}"
        upsert_raw_record(db, sk, "workout_detail", None, None,
                          "workout_detail", end_time_ts * 1000 if end_time_ts else None,
                          None, None, user_id, data)
        # 解析
        decoded = decode_workout_detail(
            data,
            summary_end_ts=end_time_ts,
            summary_distance_m=dis_m,
        )
        # 幂等：先删旧的
        delete_workout_detail(db, str(trackid))
        # 写 6 张表
        for rp in decoded.route:
            upsert_workout_route_point(db, rp)
            written_route += 1
        for s in decoded.samples:
            upsert_workout_sample(db, s)
            written_samples += 1
        for sp in decoded.splits:
            upsert_workout_split(db, sp)
            written_splits += 1
        for lp in decoded.laps:
            upsert_workout_lap(db, lp)
            written_laps += 1
        for pz in decoded.pauses:
            upsert_workout_pause(db, pz)
            written_pauses += 1
        if decoded.heart_rate_drift:
            upsert_workout_hr_drift(db, decoded.heart_rate_drift)
            written_drift += 1
    db.commit()

    print(
        f"      [workout_detail] scanned={len(rows)} "
        f"route={written_route} samples={written_samples} "
        f"splits={written_splits} laps={written_laps} "
        f"pauses={written_pauses} drift={written_drift} "
        f"[M4 diag: ok={_workout_detail_diag['ok']} "
        f"no_data={_workout_detail_diag['no_data_silent']} "
        f"no_gps={_workout_detail_diag['no_gps_skipped']} "
        f"fetch_err={_workout_detail_diag['fetch_error']}]",
        flush=True,
    )
    if errors:
        for e in errors[:3]:
            print(f"        • {e}", flush=True)

    # ===== M3 章节 3.2：顺手算 insight =====
    try:
        from insight import (
            compute_workout_insight, compute_weekly_report,
            insight_to_json, weekly_report_to_json,
        )
        from storage import upsert_workout_insight, upsert_weekly_report
        insights_written = 0
        for tid, *_ in rows:
            try:
                ins = compute_workout_insight(db, str(tid))
                upsert_workout_insight(
                    db, str(tid), insight_to_json(ins),
                    supported=ins.supported,
                    unsupported_reason=ins.unsupported_reason,
                    baseline_window_days=ins.baseline_window_days,
                    samples_count=ins.samples_count,
                )
                insights_written += 1
            except Exception as e:
                pass  # insight 失败不阻塞
        # 周报
        rep = compute_weekly_report(db)
        upsert_weekly_report(
            db, rep.date, weekly_report_to_json(rep),
            rep.baseline_window_days, rep.baseline_days_available,
        )
        db.commit()
        print(f"      [insight] insights={insights_written} weekly_report=1", flush=True)
    except Exception as e:
        print(f"      [insight] skipped: {e}", flush=True)


def cmd_init(args):
    """初始化 SQLite 数据库（仅创建 schema）。"""
    db_path = Path(args.db)
    print(f"=== 初始化 Zepp DB ===")
    print(f"DB: {db_path}")
    init_db(db_path)
    print(f"✓ 数据库已创建（schema 内置）")
    counts = {"raw_records": 0, "measurements": 0, "capabilities": 0, "meta": 1}
    print(f"当前: {counts}")
    return 0


def _run_sync_streams(client, db, user_id, from_ms, to_ms):
    """跑一遍所有 stream，返回 (total, error_counts, error_samples, sync_results)。

    单独抽出来是为了让 cmd_sync 在 401 时能重跑一次（silent reauth 后 retry）。
    """
    total = 0
    # M5：错误聚合 — 收集每个流的 fetch_error 分类 + 哪个 label 失败
    sync_results = []
    error_counts = {
        _ERROR_CATEGORY_CRITICAL_AUTH: 0,
        _ERROR_CATEGORY_NETWORK: 0,
        _ERROR_CATEGORY_UNAVAILABLE: 0,
        _ERROR_CATEGORY_UNKNOWN: 0,
    }
    error_samples = {
        _ERROR_CATEGORY_CRITICAL_AUTH: [],
        _ERROR_CATEGORY_NETWORK: [],
        _ERROR_CATEGORY_UNAVAILABLE: [],
        _ERROR_CATEGORY_UNKNOWN: [],
    }
    for label, surface, event_type, sub_type, fn in STREAMS:
        print(f"[{label:<22}] {surface:<12} {(event_type or '-'):<20} {(sub_type or '-'):<14}", end=" ")
        t0 = time.time()
        result = fetch_and_normalize(
            client, label, surface, event_type, sub_type, fn,
            from_ms, to_ms, db, user_id,
        )
        elapsed = time.time() - t0
        status = result["status"]
        recs = result["records"]
        total += recs
        update_capability(db, surface, event_type or "_", sub_type, status,
                           recs, result.get("last_ts_ms"))
        if status == "available":
            print(f"✓ {recs:>5} records ({elapsed:.2f}s)")
        elif status == "no_records":
            print(f"○ no records        ({elapsed:.2f}s)")
        elif status == "unsupported":
            print(f"✗ unsupported       ({elapsed:.2f}s)")
        elif status == "available_no_normalizer":
            print(f"⚠ raw only: {result.get('raw_count', 0)} ({elapsed:.2f}s)")
        else:
            print(f"✗ {status}: {result.get('error', '')} ({elapsed:.2f}s)")
        # M5：收集错误分类 + 哪个流失败
        sync_results.append({
            "label": label,
            "status": status,
            "error": result.get("error", ""),
            "error_category": result.get("error_category"),
            "error_type": result.get("error_type"),
            "records": recs,
        })
        if status in ("fetch_error", "normalize_error") and result.get("error_category"):
            cat = result["error_category"]
            error_counts[cat] = error_counts.get(cat, 0) + 1
            # 保留前 5 个样本
            if len(error_samples[cat]) < 5:
                error_samples[cat].append(
                    f"[{label}] {result.get('error', '')}"
                )
    return total, error_counts, error_samples, sync_results


def cmd_sync(args):
    """拉数据并落库。

    M6：401 自动恢复 — 如果 sync 检测到 critical_auth 错误 → 静默调 _silent_reauth
    重跑一次。重跑后还有 401 → 升级到 Critical 提示用户（兜底）。
    M7：sync 前自动 refresh token —— 避免 token 过期导致 sync 失败 + 用户误以为是数据问题。
    M7：sync 后展示"数据上传时间" —— 告诉用户这是几小时前的数据，不是 Zepp App 实时显示的。
    """
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB 不存在，先创建：{db_path}")
        init_db(db_path)

    # ===== M7：sync 前自动 refresh token（防 token 过期 → 0 条 → 用户误判数据问题）=====
    #   refresh 输出对用户可见（"已 rotate" 等），让用户知道 token 在刷新
    #   refresh 失败（exit 2 = login_token 过期）→ 触发 _silent_reauth 走完整 OAuth
    #   refresh 失败（exit 1 / 其他）→ 不 abort，让后续 M6 silent reauth 兜底
    _maybe_refresh_token_before_sync()

    # ===== v4.0.3+：workouts 流是独立的，不会被本 sync 拉到 ======
    # 如果用户是首次 sync 或近期重新建了 DB，workouts 表可能为空
    # → workout_detail 流会 silently 报 "no workouts in window"
    # → 用户困惑"我的运动数据怎么没了"
    # v4.0.3+：显式提示
    try:
        conn_check = sqlite3.connect(str(db_path))
        workouts_count = conn_check.execute(
            "SELECT COUNT(*) FROM workouts"
        ).fetchone()[0]
        conn_check.close()
        if workouts_count == 0:
            print("💡 workouts 表是空的 → workout_detail 流会报 no workouts。")
            print("   单独跑: python3 scripts/fetch_workouts.py --from 2026-01-01")
            print()
    except Exception:
        # 表不存在也是空 → 同样提示
        print("💡 workouts 表还不存在 → workout_detail 流会报 no workouts。")
        print("   单独跑: python3 scripts/fetch_workouts.py --from 2026-01-01")
        print()

    if not SECRETS_FILE.exists():
        print(f"✗ token 不存在：{SECRETS_FILE}")
        print(f"  请先跑：python3 pull_to_sqlite.py login")
        return 1

    # 自动引导: 缺 ZEPP_PHONE 时调 init.py（仅交互终端）
    if sys.stdin.isatty() and not os.environ.get("ZEPP_PHONE") and not os.environ.get("ZEPP_NO_BOOTSTRAP"):
        init_script = Path(__file__).parent / "init.py"
        if init_script.exists():
            print("→ 检测到缺 ZEPP_PHONE，启动首次配置引导（init.py）...")
            print()
            rc = subprocess.run(
                [sys.executable, str(init_script), "--non-interactive"]
            ).returncode
            if rc in (1, 2):
                print(f"  ✗ init.py 引导失败 (exit {rc})")
                # M5：refresh/login 失败时给用户具体修复路径
                #   rc=2 通常是 zepp_oauth.py refresh 失败 → token 失效，需重新 OAuth
                #   rc=1 通常是 OAuth login 失败 → 检查 phone/password
                if rc == 2:
                    print(f"  → token refresh 失败（exit 2）—— 需要重新走完整 OAuth")
                    print(f"  → 跑: python3 scripts/init.py --rotate")
                    print(f"     或: python3 scripts/zepp_oauth.py login --phone \"$ZEPP_PHONE\"")
                else:
                    print(f"  → 检查 ZEPP_PHONE / ZEPP_PASSWORD 是否正确")
                    print(f"  → 跑: python3 scripts/zepp_oauth.py login --phone <手机号> --password-file <密码文件>")
                return rc

    days = args.days if args.days > 0 else 365 * 3
    from_ms = days_ago_ms(days)
    to_ms = now_ms()

    print(f"=== Zepp → SQLite ===")
    print(f"窗口: 最近 {days} 天")
    print(f"DB:  {db_path}")
    print(f"Normalizer revision: {NORMALIZER_REVISION}")
    print()

    db = init_db(db_path)
    client = ZeppClient()
    user_id = client.user_id

    # ===== M6：401 自动恢复 — 第一遍 sync =====
    total, error_counts, error_samples, sync_results = _run_sync_streams(
        client, db, user_id, from_ms, to_ms,
    )

    # ===== M6：401 自动恢复 — 检测 critical_auth → silent reauth + retry =====
    # ZEPP_NO_REAUTH=1 时跳过 reauth（debug 用，让 CRITICAL 块直接出现）
    reauth_attempted = False
    reauth_succeeded = False
    critical_count = error_counts.get(_ERROR_CATEGORY_CRITICAL_AUTH, 0)
    if critical_count > 0 and not os.environ.get("ZEPP_NO_REAUTH"):
        reauth_attempted = True
        if _silent_reauth(reason=f"{critical_count} 流返 401"):
            reauth_succeeded = True
            # 重建 ZeppClient 让它重新读 token.json
            try:
                client = ZeppClient()
                user_id = client.user_id
            except Exception as e:
                _reauth_log(f"重建 ZeppClient 失败：{e}")
                reauth_succeeded = False
            if reauth_succeeded:
                # 重置 error_counts / error_samples 重跑
                print()  # 空行隔开
                total, error_counts, error_samples, sync_results = _run_sync_streams(
                    client, db, user_id, from_ms, to_ms,
                )

    db.commit()
    set_meta(db, "last_pull_at", datetime.now(timezone.utc).isoformat())
    set_meta(db, "last_pull_window_days", str(days))
    db.commit()

    print()
    print("=== DB 状态 ===")
    for k, v in counts(db).items():
        print(f"  {k:<30s} {v}")

    # sync 完成后清理 daily-level 重复（device_id NULL 历史残留）
    from storage import dedup_measurements
    deleted = dedup_measurements(db)
    if deleted:
        print(f"\n[dedup] 清理 {deleted} 条 daily-level 重复 record")

    # ===== M7：sync 后展示"数据上传时间" ——
    #   必须放在 db.close() 之前（否则 SQL 查询会失败）
    #   让用户知道这是几小时前的数据，不是 Zepp App 实时显示的
    #   数据源：measurements.ts_ms（与 counts()::latest 一致，单位毫秒）
    ts_str, age_str, ts_err = _compute_data_upload_time(db)
    if ts_str is not None and age_str is not None:
        print(f"\n  数据上传时间:           {ts_str} ({age_str})")
    elif ts_err:
        # 调试可见但不打扰用户（保持静默避免 happy path 多噪音）
        pass

    db.close()

    # === M5：错误可见性 — 聚合输出 CRITICAL / WARNING 块 ===
    # M6 改进：silent reauth 已处理过 401 — 只在 retry 后仍失败时才打 CRITICAL（兜底）
    # 不影响 happy path（error_counts 全为 0 时整块不打印）
    if any(error_counts.values()):
        print()
        print("=== ⚠️ 错误聚合 ===")
        # 顺序：critical_auth → network → unavailable → unknown
        for cat in (_ERROR_CATEGORY_CRITICAL_AUTH, _ERROR_CATEGORY_NETWORK,
                    _ERROR_CATEGORY_UNAVAILABLE, _ERROR_CATEGORY_UNKNOWN):
            n = error_counts.get(cat, 0)
            if not n:
                continue
            # M6：如果 silent reauth 失败，会进这里 — 加一行说明
            extra = ""
            if cat == _ERROR_CATEGORY_CRITICAL_AUTH and reauth_attempted and not reauth_succeeded:
                extra = "（silent reauth 已失败 — 需要手动处理）"
            print(f"{_category_emoji(cat)} ({n} 流失败){extra} — {_category_hint(cat)}")
            for s in error_samples[cat]:
                print(f"  • {s}", flush=True)

    print(f"\n本次新增/更新 {total} 条 records")

    if total == 0:
        # === sync 0 条智能诊断（P1.6） ===
        print()
        print("💡 sync 0 条诊断（按可能性排序）：")
        diag_lines = []

        # M6：0 条 + 有 critical_auth 错误 → silent reauth 已失败（否则错误不会残留）
        # 兜底：提示用户手动处理
        if error_counts.get(_ERROR_CATEGORY_CRITICAL_AUTH, 0):
            n = error_counts[_ERROR_CATEGORY_CRITICAL_AUTH]
            if reauth_attempted and not reauth_succeeded:
                diag_lines.append(
                    f"  ⚠️ 0 条因为 token 失效（{n} 流返 401）—— "
                    f"silent reauth 已失败（看 log: {_REAUTH_LOG_FILE}）"
                )
                diag_lines.append(
                    f"  → 跑: python3 scripts/init.py --rotate"
                )
                diag_lines.append(
                    f"     或: python3 scripts/zepp_oauth.py login --phone \"$ZEPP_PHONE\""
                )
            else:
                # reauth 成功但仍 0 条（不太可能；账号没问题就是没数据）
                diag_lines.append(
                    f"  ⚠️ 0 条可能因为 token 失效（{n} 流返 401）—— "
                    f"已 Critical 提示，先跑 init.py --rotate 或 zepp_oauth.py login 再 sync"
                )
        elif error_counts.get(_ERROR_CATEGORY_NETWORK, 0):
            n = error_counts[_ERROR_CATEGORY_NETWORK]
            diag_lines.append(
                f"  ⚠️ 0 条可能因为网络/服务问题（{n} 流失败）"
            )

        # 1) token 状态
        try:
            token_mtime = datetime.fromtimestamp(SECRETS_FILE.stat().st_mtime, tz=timezone.utc)
            token_age_hours = (datetime.now(timezone.utc) - token_mtime).total_seconds() / 3600
            if token_age_hours < 1:
                diag_lines.append(f"  • token.json {token_age_hours*60:.0f} 分钟前写入 — 看起来刚跑过")
            elif token_age_hours > 24 * 30:
                diag_lines.append(f"  ⚠️ token.json 已 {token_age_hours/24:.0f} 天未更新 — 可能过期。跑 `python3 scripts/zepp_oauth.py refresh`")
            else:
                diag_lines.append(f"  • token.json {token_age_hours/24:.1f} 天前更新（应该还在有效期内）")
        except FileNotFoundError:
            diag_lines.append("  ❌ token.json 不存在 — 请跑 `python3 scripts/zepp_oauth.py login`")

        # 2) 上次 sync 时间（meta 表）
        try:
            _diag_db = init_db(db_path)
            last_pull = _diag_db.execute("SELECT value FROM meta WHERE key='last_pull_at'").fetchone()
            _diag_db.close()
            if last_pull:
                last_dt = datetime.fromisoformat(last_pull[0])
                delta_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if delta_h < 1:
                    diag_lines.append(f"  💡 上次 sync 距今 {delta_h*60:.0f} 分钟 — 可能真的没新数据")
                elif delta_h < 24:
                    diag_lines.append(f"  • 上次 sync 距今 {delta_h:.1f} 小时")
                else:
                    diag_lines.append(f"  • 上次 sync 距今 {delta_h/24:.1f} 天")
        except Exception:
            pass

        # 3) 流的 capability（区分 no_records vs available）
        try:
            _cap_db = init_db(db_path)
            rows = _cap_db.execute(
                "SELECT surface, event_type, sub_type, status, last_records "
                "FROM capabilities ORDER BY surface, event_type, sub_type"
            ).fetchall()
            _cap_db.close()
            no_record = [r for r in rows if r[3] == "no_records"]
            unsupported = [r for r in rows if r[3] in ("unsupported", "fetch_error")]
            if no_record:
                names = [f"{r[0]}:{r[1]}:{r[2]}" for r in no_record[:8]]
                more = f" (+{len(no_record)-8} more)" if len(no_record) > 8 else ""
                diag_lines.append(f"  💡 {len(no_record)} 个流为 no_records: {', '.join(names)}{more}")
                diag_lines.append(f"      （stream 暂时无数据 ≠ 不支持，可能你那段窗口没戴表）")
            if unsupported:
                names = [f"{r[0]}:{r[1]}:{r[2]}" for r in unsupported[:5]]
                more = f" (+{len(unsupported)-5} more)" if len(unsupported) > 5 else ""
                diag_lines.append(f"  ⚠️ {len(unsupported)} 个流 fetch 失败/unsupported: {', '.join(names)}{more}")
            if not no_record and not unsupported:
                diag_lines.append(f"  • 全部 {len(rows)} 个流 capability 都是 available（但本次 sync 没写新 record — 检查窗口 `--days`）")
        except Exception as e:
            diag_lines.append(f"  • 读 capabilities 表失败：{e}")

        # 4) 窗口太小提示
        if days < 7:
            diag_lines.append(f"  💡 窗口只有 {days} 天 — 试着 `--days 30` 或 `--days 90` 拉更宽")

        print("\n".join(diag_lines))
    return 0


def cmd_dedup(args):
    """清理 daily-level 重复 record（device_id NULL 历史残留）。

    修历史 bug：早期 sync 留的 NULL device_id 在 SQLite UNIQUE 约束里
    视为不等，导致同一天同一 metric 多次 sync 留下多条不同值的 record。
    """
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"✗ DB 不存在：{db_path}")
        return 1
    db = init_db(db_path)
    from storage import dedup_measurements
    deleted = dedup_measurements(db)
    db.close()
    if deleted:
        print(f"✓ 清理 {deleted} 条 daily-level 重复 record")
    else:
        print("✓ 没有需要清理的重复")
    return 0


def cmd_login(args):
    """OAuth 登录（推荐 zepp_oauth.py）。

    委托给 zepp_oauth.py 跑纯 HTTP OAuth（不依赖浏览器，任何环境都能跑）。
    密码从 stdin / 环境变量 / 文件读取，AI 完全见不到密码。
    """
    import subprocess
    import sys as _sys
    oauth_script = Path(__file__).parent / "zepp_oauth.py"
    if not oauth_script.exists():
        print(f"✗ 找不到 {oauth_script}")
        return 1
    # 直接调 zepp_oauth.cmd_login
    if args.password_file or args.phone or args.email or args.device_id:
        # 传参模式
        argv = ["python3", str(oauth_script), "login"]
        if args.phone:
            argv.extend(["--phone", args.phone])
        elif args.email:
            argv.extend(["--email", args.email])
        if args.password_file:
            argv.extend(["--password-file", args.password_file])
        if args.device_id:
            argv.extend(["--device-id", args.device_id])
        # 没传 password-file 时让 zepp_oauth 自己从 stdin / env 读
        result = subprocess.run(argv, env=os.environ)
        return result.returncode
    # 无参模式：直接调 cmd_login 函数（stdin 读密码）
    from zepp_oauth import cmd_login as oauth_login_cmd
    # 构造一个简单 Namespace（user 没传 phone 时让他在提示下输）
    if not _sys.stdin.isatty():
        print("✗ 交互模式需要 terminal（stdin 不是 tty）。用 --phone/--password-file 重试。")
        return 1
    class Args: pass
    a = Args()
    a.phone = input("手机号（不带 +86）: ").strip()
    a.email = None
    a.password_file = None
    a.device_id = None
    return oauth_login_cmd(a)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    # sync
    p_sync = sub.add_parser("sync", help="拉 Zepp 数据并落 SQLite")
    p_sync.add_argument("--days", type=int, default=7,
                        help="拉最近 N 天（0 = 3 年全量）")
    p_sync.add_argument("--db", type=str, default=str(DEFAULT_DB))
    p_sync.set_defaults(func=cmd_sync)

    # init
    p_init = sub.add_parser("init", help="仅初始化 SQLite 数据库")
    p_init.add_argument("--db", type=str, default=str(DEFAULT_DB))
    p_init.set_defaults(func=cmd_init)

    # login
    p_login = sub.add_parser("login", help="OAuth 登录（推荐 zepp_oauth.py）")
    p_login.add_argument("--phone", help="手机号（不带 +86）")
    p_login.add_argument("--email", help="邮箱")
    p_login.add_argument("--password-file", help="密码文件（推荐 chmod 600）")
    p_login.add_argument("--device-id", help="自定义 device_id")
    p_login.set_defaults(func=cmd_login)

    # dedup
    p_dedup = sub.add_parser("dedup", help="清理 daily-level 重复 record（device_id NULL 历史残留）")
    p_dedup.add_argument("--db", type=str, default=str(DEFAULT_DB))
    p_dedup.set_defaults(func=cmd_dedup)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())