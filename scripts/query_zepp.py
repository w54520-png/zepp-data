#!/usr/bin/env python3
"""
Zepp 健康数据查询脚本（自然语言友好输出）

子命令：
  recent --days N                最近 N 天的关键指标摘要
  metric --metric M --days N     单一指标的趋势
  range --from YYYY-MM-DD --to ... 日期范围查询
  devices                       设备列表
  capabilities                  数据流能力状态
  summary                       整个 DB 摘要
  daily-stress --date YYYY-MM-DD  某一天的压力详情（Zepp App 风格：当前值 / min/max/avg / 4 区间占比 / 本地重算 / vs 昨天）
  workouts                      运动历史汇总（按类型 / 按月份 / Top N）
  workouts --list               列出所有运动（可加 --sport/--from/--limit 过滤）
  workouts --top 5              最远 / 爬升最多 / 卡路里最多 top 5

用法：
  python3 query_zepp.py recent --days 7
  python3 query_zepp.py metric --metric steps --days 30
  python3 query_zepp.py range --from 2026-09-01 --to 2026-09-18
  python3 query_zepp.py devices
  python3 query_zepp.py capabilities
  python3 query_zepp.py summary
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# 默认 ~/.zepp-data/，可用 ZEPP_DATA_DIR 覆盖
DATA_DIR = Path(os.environ.get("ZEPP_DATA_DIR", str(Path.home() / ".zepp-data")))
SECRETS_FILE = DATA_DIR / ".secrets" / "token.json"
DEFAULT_DB = DATA_DIR / "zepp.db"


def connect(db_path):
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"✗ DB 不存在：{db_path}")
        print(f"  请先跑：python3 pull_to_sqlite.py init")
        sys.exit(1)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_fresh(db_path: str = "/root/.zepp-data/zepp.db", max_age_sec: int = 300) -> bool:
    """如果 DB 数据超过 max_age_sec 秒陈旧，自动 sync。

    逻辑：
      - meta 表无 last_pull_at（首次查询）：warning + sync
      - last_pull_at 距 now ≤ max_age_sec：pass
      - 差距 > max_age_sec 但 ≤ 1 小时：温和提示 + sync
      - 差距 > 1 小时：warning（数据可能已严重陈旧）+ sync
      - last_pull_at 格式非法：warning + sync（防御）

    Returns: True 表示触发了 sync，False 表示不需要。

    出错：subprocess 非 0 退出码 → 打印 warning 但不 raise（让 query 用旧数据继续）。
    """
    db_path = Path(db_path)
    if not db_path.exists():
        # 没有 DB 就没必要 sync 了（connect() 会提示用户跑 init）
        return False

    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT value FROM meta WHERE key='last_pull_at'"
        ).fetchone()
        conn.close()
    except Exception as e:
        print(f"⚠️ 读 meta 表失败，自动跳过 ensure_fresh：{e}")
        return False

    now_utc = datetime.now(timezone.utc)
    stale_reason: str | None = None
    age_sec: float | None = None

    if row is None or not row[0]:
        stale_reason = "first_run"
    else:
        last_str = row[0]
        try:
            last_dt = datetime.fromisoformat(last_str)
            # 没 tz 就当 UTC（防御）
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            age_sec = (now_utc - last_dt).total_seconds()
        except (ValueError, TypeError):
            stale_reason = "invalid_format"
        else:
            if age_sec > max_age_sec:
                # 陈旧。陈旧程度决定提示文案
                if age_sec > 3600:
                    stale_reason = "very_stale"
                else:
                    stale_reason = "stale"

    if stale_reason is None:
        # 新鲜
        return False

    # 打提示词
    if stale_reason == "first_run":
        print("⚠️ 首次查询，自动 sync...")
    elif stale_reason == "invalid_format":
        print(f"⚠️ meta.last_pull_at 格式非法（{last_str!r}），自动 sync...")
    elif stale_reason == "very_stale":
        print(f"⏰ 上次 sync {age_sec/3600:.1f} 小时前，数据可能严重陈旧，自动拉最新...")
    else:  # stale
        print(f"⏰ 上次 sync {age_sec/60:.1f} 分钟前，自动拉最新...")

    # 调 subprocess 跑 sync（只刷 1 天缓存，不重拉历史）
    # 让 sync 的 stdout 流式输出到当前 terminal，让用户看到实时进度
    sync_script = Path(__file__).resolve().parent / "pull_to_sqlite.py"
    try:
        result = subprocess.run(
            ["python3", str(sync_script), "sync", "--days", "1"],
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        print("⚠️ 自动 sync 超时（>180s），但继续查询（可能是网络问题）")
        return True
    except Exception as e:
        print(f"⚠️ 自动 sync 启动失败：{e}，但继续查询（可能是网络问题）")
        return True

    if result.returncode == 0:
        print("✓ 自动 sync OK")
    else:
        print(f"⚠️ 自动 sync 失败（returncode={result.returncode}），但继续查询（可能是网络问题）")
    return True


def cmd_recent(args):
    """最近 N 天的关键指标摘要（按日期分组）。"""
    ensure_fresh(args.db)
    conn = connect(args.db)
    n_days = args.days
    tz = timezone(timedelta(hours=8))
    cutoff = (datetime.now(tz) - timedelta(days=n_days)).strftime("%Y-%m-%d")

    days = conn.execute("""
        SELECT DISTINCT date FROM measurements
        WHERE date >= ?
        ORDER BY date DESC
    """, (cutoff,)).fetchall()

    print(f"=== 最近 {n_days} 天健康数据 ({cutoff} ~ {datetime.now(tz).strftime('%Y-%m-%d')}) ===\n")

    key_metrics = [
        ("steps", "步数"),
        ("calories", "卡路里（活动）"),
        ("calories_total", "卡路里（总=活动+BMR）"),
        ("active_minutes", "活动分钟"),
        ("pai_daily", "PAI 当日"),
        ("pai_total", "PAI 七天累计"),
        ("device_resting_hr", "静息心率"),
        ("stress", "压力（首日曲线值）"),
        ("stress_relax_pct", "压力放松%"),
        ("sport_load_today", "训练负荷当日"),
        ("spo2", "血氧（最近测量）"),
        ("hybrid_charge_intel", "体电荷"),
    ]

    for d_row in days:
        d = d_row["date"]
        print(f"━━━ {d} ━━━")
        for metric, label in key_metrics:
            row = conn.execute("""
                SELECT value, unit FROM measurements
                WHERE date = ? AND metric = ?
                ORDER BY ts_ms DESC LIMIT 1
            """, (d, metric)).fetchone()
            if row:
                print(f"  {label:<22} {row['value']:>8.2f} {row['unit']}")

        hrv = conn.execute("""
            SELECT value FROM measurements WHERE date = ? AND metric = 'hrv_rmssd'
        """, (d,)).fetchall()
        if hrv:
            vals = [r["value"] for r in hrv]
            print(f"  {'HRV RMSSD':<22} {sum(vals)/len(vals):>8.1f} ms        [{len(vals)} 个采样]")
        print()

    print("━━━ 特殊事件 ━━━")
    apnea = conn.execute("""
        SELECT date, value FROM measurements
        WHERE metric='spo2_apnea_low' AND date >= ?
        ORDER BY ts_ms
    """, (cutoff,)).fetchall()
    if apnea:
        for r in apnea:
            print(f"  SpO2 呼吸暂停事件: {r['date']} {r['value']}%")

    conn.close()


def cmd_metric(args):
    """单一指标的趋势。"""
    ensure_fresh(args.db)
    conn = connect(args.db)
    n_days = args.days
    tz = timezone(timedelta(hours=8))
    cutoff = (datetime.now(tz) - timedelta(days=n_days)).strftime("%Y-%m-%d")

    # only-measured：过滤掉 source_scope=placeholder（Zepp 推算值，非手表实测）
    extra_where = ""
    if args.only_measured:
        extra_where = " AND source_scope != 'placeholder'"

    rows = conn.execute(f"""
        SELECT date, value, unit, source_scope, device_id, ts_ms
        FROM measurements
        WHERE metric = ? AND date >= ?{extra_where}
        ORDER BY ts_ms DESC
    """, (args.metric, cutoff)).fetchall()

    if not rows:
        print(f"✗ 没有 {args.metric} 的数据（窗口：{cutoff} ~ 今天）")
        # === P1.7 缺失数据友好提示 ===
        print(f"💡 可能原因：")
        print(f"   1) 你这段时间没戴表（try `--only-measured` 排除占位数据）")
        print(f"   2) 流 capability 是 no_records（try `python3 query_zepp.py capabilities`）")
        print(f"   3) Zepp App 还没生成这条数据（try `python3 query_zepp.py recent --days 30` 看 daily_summary）")
        # 进一步：该 metric 在 DB 里完全没数据 vs 只是窗口内没？
        total_for_metric = conn.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM measurements WHERE metric = ?",
            (args.metric,),
        ).fetchone()
        if total_for_metric[0] > 0:
            print(f"   ↳ DB 里有 {total_for_metric[0]} 条历史数据（{total_for_metric[1]} ~ {total_for_metric[2]}），试着把 --days 调大或用 `range` 子命令")
        else:
            print(f"   ↳ DB 里完全没有 {args.metric}（试 `python3 query_zepp.py capabilities` 看流是否被 sync）")
        conn.close()
        return

    # 去重每日
    seen = set()
    daily = []
    for r in rows:
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        daily.append(r)

    unit = daily[0]["unit"]
    only_note = "（仅真实手表实测，过滤占位推算值）" if args.only_measured else ""
    print(f"=== {args.metric} ({unit}) — 最近 {n_days} 天 {only_note}===\n")
    for r in daily:
        dev = r["device_id"] or ""
        scope = r["source_scope"]
        flag = " ⚠️推算" if scope == "placeholder" else ""
        print(f"  {r['date']}  {r['value']:>10.2f} {unit}  [{scope}{',' + dev[:12] if dev else ''}]{flag}")

    # 简单统计
    vals = [r["value"] for r in daily]
    print(f"\n  共 {len(vals)} 天，最小 {min(vals):.2f}，最大 {max(vals):.2f}，"
          f"平均 {sum(vals)/len(vals):.2f}")
    conn.close()


def cmd_range(args):
    """日期范围查询。"""
    ensure_fresh(args.db)
    conn = connect(args.db)
    rows = conn.execute("""
        SELECT date, metric, value, unit, source_scope
        FROM measurements
        WHERE date >= ? AND date <= ?
        ORDER BY date, metric
    """, (args.from_date, args.to_date)).fetchall()

    if not rows:
        print(f"✗ {args.from_date} ~ {args.to_date} 没有数据")
        # === P1.7 友好提示 ===
        print(f"💡 可能原因：")
        print(f"   1) 窗口里没戴表 — 试着拉宽到 --from <2024-01-01>")
        print(f"   2) sync 没拉到这区间 — 跑 `python3 pull_to_sqlite.py sync --days 90`")
        print(f"   3) 这区间没数据流 — 跑 `python3 query_zepp.py capabilities` 看流能力")
        conn.close()
        return

    print(f"=== {args.from_date} ~ {args.to_date} 共 {len(rows)} 条记录 ===\n")

    # 按日期+指标分组
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["date"]].append((r["metric"], r["value"], r["unit"]))

    for d in sorted(by_day.keys()):
        print(f"━━ {d} ━━━")
        for m, v, u in by_day:
            print(f"  {m:<22} {v:>8.2f} {u}")

    conn.close()


def cmd_devices(args):
    """设备列表。"""
    ensure_fresh(args.db)
    conn = connect(args.db)
    rows = conn.execute("""
        SELECT mac_address, device_type, firmware_version, product_id
        FROM devices
        ORDER BY mac_address
    """).fetchall()
    if not rows:
        print("无设备数据")
    else:
        print(f"=== 设备列表 ({len(rows)} 台) ===\n")
        for r in rows:
            print(f"  mac={r['mac_address']}  fw={r['firmware_version']}  "
                  f"productId={r['product_id']}  type={r['device_type']}")
    conn.close()


def cmd_capabilities(args):
    """流能力状态。"""
    ensure_fresh(args.db)
    conn = connect(args.db)
    rows = conn.execute("""
        SELECT surface, event_type, sub_type, status, sample_count, last_probed_at
        FROM capabilities
        ORDER BY surface, event_type, sub_type
    """).fetchall()
    print(f"=== 流能力状态 ({len(rows)} 条) ===\n")
    print(f"  {'surface':<12} {'event':<22} {'sub':<14} {'status':<22} {'count':>6}  last_probed")
    print("  " + "-" * 100)
    for r in rows:
        et = (r["event_type"] or "_")[:22]
        st = (r["sub_type"] or "_")[:14]
        stt = r["status"][:22]
        cnt = r["sample_count"] or 0
        ts = r["last_probed_at"] or ""
        print(f"  {r['surface']:<12} {et:<22} {st:<14} {stt:<22} {cnt:>6}  {ts}")
    conn.close()


def cmd_workouts(args):
    """运动历史汇总 / 列表 / Top N。"""
    ensure_fresh(args.db)
    conn = connect(args.db)

    # 检查 workouts 表是否存在
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workouts'"
    ).fetchone()
    if not has_table:
        print("❌ workouts 表不存在。先跑 fetch_workouts.py 拉取历史")
        conn.close()
        return

    n = conn.execute("SELECT COUNT(*) AS c FROM workouts").fetchone()["c"]
    if n == 0:
        print("❌ workouts 表为空。先跑 fetch_workouts.py 拉取历史")
        conn.close()
        return

    if args.list or args.top or args.sport or args.from_date:
        # 列表模式
        where = []
        params = []
        if args.sport:
            # 支持中文 / 英文 / alias
            from sport_catalog import SPORT_ALIAS, key_to_zh
            aliases = SPORT_ALIAS.get(args.sport, [args.sport])
            zh_list = [args.sport] + aliases
            placeholders = ",".join("?" * len(zh_list))
            where.append(f"sport_zh IN ({placeholders})")
            params.extend(zh_list)
        if args.from_date:
            where.append("date(end_time_ts, 'unixepoch', 'localtime') >= ?")
            params.append(args.from_date)
        if args.to_date:
            where.append("date(end_time_ts, 'unixepoch', 'localtime') <= ?")
            params.append(args.to_date)

        order = "dis_m DESC" if args.top == "distance" else \
                "altitude_ascend DESC" if args.top == "climb" else \
                "calorie DESC" if args.top == "calorie" else \
                "end_time_ts DESC"
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        sql = f"SELECT * FROM workouts {where_sql} ORDER BY {order}"
        if args.limit:
            sql += f" LIMIT {args.limit}"
        rows = conn.execute(sql, params).fetchall()
        print(f"=== {'Top' if args.top else '运动列表'} ===\n" if args.top else f"=== 运动列表 ===\n")
        for r in rows:
            end_dt = datetime.fromtimestamp(r["end_time_ts"], timezone(timedelta(hours=8)))
            dis = r["dis_m"] or 0
            asc = r["altitude_ascend"] or 0
            cal = r["calorie"] or 0
            dur_min = (r["run_s"] or 0) // 60
            print(f"  {end_dt.strftime('%Y-%m-%d %H:%M')}  {r['sport_zh']:<8}  {dis/1000:>6.2f}km  {dur_min:>4}min  ↑{asc:.0f}m  {cal:.0f}kcal  ({r['city'] or '?'})")
        print(f"\n共 {len(rows)} 条")
    else:
        # 汇总模式（默认）
        print(f"=== 运动历史汇总（{n} 条）===\n")
        print(f"{'类型':<10} {'key':<18} {'距离':>8} {'时长':>8} {'卡路里':>8} {'爬升':>8}")
        print(f"{'(中文)':<10} {'(英文)':<18} {'(km)':>8} {'(h:mm)':>8} {'(kcal)':>8} {'(m)':>8}")
        print("-" * 65)
        for r in conn.execute("""
            SELECT sport_zh, sport_key,
                   COUNT(*) c, ROUND(SUM(dis_m)/1000, 2) dis_km,
                   SUM(run_s) dur_s, ROUND(SUM(calorie), 0) cal,
                   ROUND(SUM(altitude_ascend), 0) asc
            FROM workouts GROUP BY sport_key, sport_zh ORDER BY c DESC
        """):
            zh, key, c, km, dur, cal, asc = r["sport_zh"], r["sport_key"], r["c"], r["dis_km"] or 0, r["dur_s"], r["cal"] or 0, r["asc"] or 0
            h = dur // 3600
            m = (dur % 3600) // 60
            print(f"{zh:<10} {key:<18} {c:>3} {km:>8.2f} {h:>3}h{m:>02}m {cal:>8.0f} {asc:>8.0f}")

        # 时间范围
        r = conn.execute("SELECT MIN(end_time_ts) mn, MAX(end_time_ts) mx FROM workouts").fetchone()
        if r["mn"]:
            d1 = datetime.fromtimestamp(r["mn"], timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
            d2 = datetime.fromtimestamp(r["mx"], timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
            print(f"\n时间范围: {d1} ~ {d2}")

    conn.close()


def cmd_daily_stress(args):
    """某一天的压力详情（Zepp App 风格输出）。

    数据源：
      1. `measurements.metric IN ('stress', 'stress_min', 'stress_max')`
         → 当前/最低/最高（avgStress / minStress / maxStress 字段直接落地）
      2. `measurements.metric IN ('stress_relax_pct', 'stress_normal_pct',
         'stress_medium_pct', 'stress_high_pct')`
         → Zepp Cloud 直接给的 4 个区间比例（值已经匹配 Zepp App 截图）
      3. `measurements.metric = 'stress'` 的 individual samples（5min 一个点）
         → 本地重算 4 区间占比，做交叉验证

    输出格式模仿 Zepp App 压力页：
      - 标题：日期 + 算法标识
      - 当前压力（最近一个 sample 的 value）
      - 最高 / 最低 / 平均
      - 4 区间分布（emoji + 百分比 + 服务器来源）
      - 本地图重算（如果曲线可用）
      - vs 昨天对比
    """
    ensure_fresh(args.db)
    conn = connect(args.db)
    tz = timezone(timedelta(hours=8))
    target_date = args.date
    if target_date is None:
        target_date = datetime.now(tz).strftime("%Y-%m-%d")

    print(f"=== {target_date} 压力（Zepp App 算法）===\n")

    # ---- 1. 顶层字段（日聚合）----
    # 多个 device/source 取最后一条（用户同一时段只有一只手表）
    daily_rows = {}
    for metric in ("stress", "stress_min", "stress_max",
                   "stress_relax_pct", "stress_normal_pct",
                   "stress_medium_pct", "stress_high_pct"):
        row = conn.execute("""
            SELECT value, unit, ts_ms FROM measurements
            WHERE date = ? AND metric = ?
            ORDER BY ts_ms DESC LIMIT 1
        """, (target_date, metric)).fetchone()
        if row:
            daily_rows[metric] = row

    if not daily_rows:
        print(f"✗ {target_date} 没有压力数据")
        print(f"  提示：跑 `python3 query_zepp.py capabilities` 看 all_day_stress 流状态")
        conn.close()
        return

    # sqlite3.Row 不支持 .get()，转成 dict
    daily_dict = {k: dict(v) for k, v in daily_rows.items()}

    # 最高/最低/平均
    # 'stress' metric 包含两类数据：avgStress（每天 1 条，ts_ms = 当天 08:00）
    # 和全天曲线的 5min 采样（多条）。从 raw avgStress 取每日均值。
    stress_avg = daily_dict.get("stress", {}).get("value")
    # 如果 stress 是日级条目（ts_ms = 当天 00:00 UTC + 0，等价于 08:00 +08:00），
    # 那就是 avgStress。如果 ts_ms 在当天其他时间，那是曲线采样。
    # 启发：日均条目恰好是"当天 ts_ms 最小"的那条。
    if stress_avg is not None:
        avg_ts = daily_dict["stress"]["ts_ms"]
        # 检查这条是不是"当天第一分钟"（日均条目）。如果不是，则找它
        sample_count = conn.execute("""
            SELECT COUNT(*) FROM measurements
            WHERE date = ? AND metric = 'stress'
        """, (target_date,)).fetchone()[0]
        if sample_count > 1:
            # 当天有多条 → 取 ts_ms 最小的那条（= 日均条目）
            daily_avg_row = conn.execute("""
                SELECT value FROM measurements
                WHERE date = ? AND metric = 'stress'
                ORDER BY ts_ms ASC LIMIT 1
            """, (target_date,)).fetchone()
            if daily_avg_row:
                stress_avg = daily_avg_row["value"]
    stress_min = daily_dict.get("stress_min", {}).get("value")
    stress_max = daily_dict.get("stress_max", {}).get("value")

    # 当前（最近一个曲线点 = ts_ms 最大的那条）
    latest_sample = conn.execute("""
        SELECT value, ts_ms FROM measurements
        WHERE date = ? AND metric = 'stress'
        ORDER BY ts_ms DESC LIMIT 1
    """, (target_date,)).fetchone()
    if latest_sample:
        sample_dt = datetime.fromtimestamp(latest_sample["ts_ms"]/1000, tz=tz)
        ts_str = sample_dt.strftime("%H:%M")
        print(f"当前压力：{latest_sample['value']:.0f}（{ts_str} 最近采样）")
    else:
        print("当前压力：N/A（无曲线采样）")

    if stress_avg is not None:
        avg_line = f"日均：{stress_avg:.0f}"
    else:
        avg_line = "日均：N/A"
    if stress_min is not None and stress_max is not None:
        avg_line += f"  /  最高：{stress_max:.0f}  /  最低：{stress_min:.0f}"
    elif stress_max is not None:
        avg_line += f"  /  最高：{stress_max:.0f}"
    elif stress_min is not None:
        avg_line += f"  /  最低：{stress_min:.0f}"
    print(avg_line)
    print()

    # ---- 2. Zepp Cloud 给的 4 个比例 ----
    relax = daily_dict.get("stress_relax_pct", {}).get("value")
    normal = daily_dict.get("stress_normal_pct", {}).get("value")
    medium = daily_dict.get("stress_medium_pct", {}).get("value")
    high = daily_dict.get("stress_high_pct", {}).get("value")

    if all(v is not None for v in (relax, normal, medium, high)):
        print("区间分布（Zepp Cloud 直接给的比例，与 Zepp App 一致）：")
        print(f"  🟢 放松 (<40)    {relax:>4.0f}%   (relaxProportion)")
        print(f"  🟢 正常 (40-59)  {normal:>4.0f}%   (normalProportion)")
        print(f"  🟡 中等 (60-79)  {medium:>4.0f}%   (mediumProportion)")
        print(f"  🔴 偏高 (≥80)    {high:>4.0f}%   (highProportion)")
        print()
    else:
        print("区间分布：服务端比例字段不完整")
        for label, key in [("放松", "stress_relax_pct"), ("正常", "stress_normal_pct"),
                           ("中等", "stress_medium_pct"), ("偏高", "stress_high_pct")]:
            row = daily_dict.get(key)
            print(f"  {label:<6} {row['value'] if row else 'N/A'}")
        print()

    # ---- 3. 本地重算（用 5min 一个点的全天曲线）----
    sys.path.insert(0, str(Path(__file__).parent))
    from normalizer.stress_breakdown import compute_stress_breakdown
    samples = conn.execute("""
        SELECT value FROM measurements
        WHERE date = ? AND metric = 'stress'
        ORDER BY ts_ms
    """, (target_date,)).fetchall()
    curve_values = [r["value"] for r in samples]

    if curve_values:
        bd = compute_stress_breakdown(curve_values)
        print(f"本地图重算（来自全天 {bd.valid_points} 个曲线采样点，过滤哨兵后）:")
        # 单区间只有 0 行时，宽度对齐
        print(f"  🟢 放松   {bd.relax_pct:>5.1f}%   ({int(round(bd.relax_pct * bd.valid_points / 100))}/{bd.valid_points})")
        print(f"  🟢 正常   {bd.normal_pct:>5.1f}%   ({int(round(bd.normal_pct * bd.valid_points / 100))}/{bd.valid_points})")
        print(f"  🟡 中等   {bd.medium_pct:>5.1f}%   ({int(round(bd.medium_pct * bd.valid_points / 100))}/{bd.valid_points})")
        print(f"  🔴 偏高   {bd.high_pct:>5.1f}%   ({int(round(bd.high_pct * bd.valid_points / 100))}/{bd.valid_points})")
        print()
    else:
        print("本地图重算：N/A（没有曲线采样）")
        print()

    # ---- 4. vs 昨天对比 ----
    yesterday_dt = datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=1)
    yesterday = yesterday_dt.strftime("%Y-%m-%d")
    y_avg_row = conn.execute("""
        SELECT value FROM measurements
        WHERE date = ? AND metric = 'stress'
        ORDER BY ts_ms DESC LIMIT 1
    """, (yesterday,)).fetchone()
    y_high_row = conn.execute("""
        SELECT value FROM measurements
        WHERE date = ? AND metric = 'stress_high_pct'
        ORDER BY ts_ms DESC LIMIT 1
    """, (yesterday,)).fetchone()

    if y_avg_row or y_high_row:
        print(f"vs 昨天（{yesterday}）:")
        if y_avg_row and stress_avg is not None:
            # 昨天可能有曲线 + 日均混合。取 ts_ms 最小的（日均条目）。
            y_avg_first = conn.execute("""
                SELECT value FROM measurements
                WHERE date = ? AND metric = 'stress'
                ORDER BY ts_ms ASC LIMIT 1
            """, (yesterday,)).fetchone()
            y_avg = y_avg_first["value"] if y_avg_first else y_avg_row["value"]
            diff = stress_avg - y_avg
            sign = "+" if diff >= 0 else ""
            print(f"  平均压力  {sign}{diff:.1f}   ({y_avg:.1f} → {stress_avg:.1f})")
        elif stress_avg is not None and not y_avg_row:
            print(f"  平均压力  无对比（昨天无数据）")
        if y_high_row and high is not None:
            y_high = y_high_row["value"]
            diff = high - y_high
            sign = "+" if diff >= 0 else ""
            print(f"  高压占比  {sign}{diff:.0f}%   ({y_high:.0f}% → {high:.0f}%)")
        elif high is not None and not y_high_row:
            print(f"  高压占比  无对比（昨天无数据）")
    else:
        print(f"vs 昨天（{yesterday}）: 无对比数据")

    conn.close()


def cmd_daily_hr(args):
    """某一天的心率详情（Zepp App 风格输出）。

    数据源：
      1. `heart_rate_band_samples` (date, ts, bpm)
         → 24h 内所有秒级/分钟级 bpm 采样（Zepp Cloud 同步时落地）
      2. `measurements.metric = 'device_max_hr'`
         → Zepp Cloud 测的最大心率（fallback 用 HUNT 211-0.64*age）

    输出格式模仿 Zepp App 心率页：
      - 标题：日期 + 算法标识
      - 当前心率（最近一个 bpm）
      - 日统计：平均 / 最高 / 最低
      - 6 区间分布（emoji + 中文标签 + 阈值 + 占比 + 分钟数）
      - vs 昨天对比（平均心率 + Zone 1 占比）

    算法（与 Zepp Cloud `heart_range` 字段一致）：
      6 区间上界 = 113 / 141 / 154 / 162 / 173 / 190 bpm（固定绝对阈值）
      区间 1 = bpm < 113       （舒缓轻松）
      区间 2 = 113 ≤ bpm < 141 （热身放松）
      区间 3 = 141 ≤ bpm < 154 （脂肪燃烧）
      区间 4 = 154 ≤ bpm < 162 （心肺强化）
      区间 5 = 162 ≤ bpm < 173 （耐力强化）
      区间 6 = bpm ≥ 173       （无氧极限）
    """
    ensure_fresh(args.db)
    conn = connect(args.db)
    tz = timezone(timedelta(hours=8))
    target_date = args.date
    if target_date is None:
        target_date = datetime.now(tz).strftime("%Y-%m-%d")

    print(f"=== {target_date} 心率（Zepp App 算法）===\n")

    # ---- 1. 从 heart_rate_band_samples 取全天 bpm 曲线 ----
    samples = conn.execute("""
        SELECT bpm, ts FROM heart_rate_band_samples
        WHERE date = ?
        ORDER BY ts
    """, (target_date,)).fetchall()
    if not samples:
        print(f"✗ {target_date} 没有心率曲线数据")
        print(f"  提示：heart_rate_band_samples 表需要 sync 时拉取 band_sleep 流")
        conn.close()
        return

    bpms = [r["bpm"] for r in samples]
    valid = [b for b in bpms if b is not None and 20 <= b <= 300]

    # 当前心率（最近一个 bpm）
    latest = samples[-1]
    sample_dt = datetime.fromtimestamp(latest["ts"], tz=tz)
    ts_str = sample_dt.strftime("%H:%M")
    print(f"当前心率：{latest['bpm']:.0f} bpm（{ts_str} 最新采样）")

    # 日统计
    avg_bpm = sum(valid) / len(valid) if valid else 0
    max_bpm = max(valid) if valid else 0
    min_bpm = min(valid) if valid else 0
    print(f"日统计：平均 {avg_bpm:.0f} / 最高 {max_bpm:.0f} / 最低 {min_bpm:.0f}（{len(valid)} 个有效采样）")
    print()

    # ---- 2. HRmax（device_max_hr 优先，fallback HUNT）----
    hr_max = 187  # 9-24 默认 device_max_hr
    if args.hr_max:
        hr_max = args.hr_max
        hr_max_src = "用户指定"
    else:
        max_hr_row = conn.execute("""
            SELECT value FROM measurements
            WHERE metric = 'device_max_hr'
            ORDER BY ts_ms DESC LIMIT 1
        """).fetchone()
        if max_hr_row:
            hr_max = int(round(max_hr_row["value"]))
            hr_max_src = f"device_max_hr（实测）"
        else:
            # fallback HUNT: 211 - 0.64*age
            profile = conn.execute("""
                SELECT birthday, gender FROM user_profile
                WHERE member_id = '-1'
            """).fetchone()
            if profile and profile["birthday"]:
                from datetime import date
                by, bm = profile["birthday"].split("-")
                bd = date(int(by), int(bm), 1)
                ty, tm, _ = map(int, target_date.split("-"))
                age = (date(ty, tm, 1) - bd).days / 365.25
                is_male = (profile["gender"] == 1)
                from normalizer.hr_zone_breakdown import estimate_hr_max
                hr_max = estimate_hr_max(age, is_male)
                hr_max_src = f"HUNT/Tanaka 估算（{age:.1f}岁, {'男' if is_male else '女'}）"
            else:
                hr_max_src = "默认 187"

    # ---- 3. 6 区间分布 ----
    sys.path.insert(0, str(Path(__file__).parent))
    from normalizer.hr_zone_breakdown import (
        compute_hr_zone_breakdown,
        ZONE_BOUNDS_BPM,
        ZONE_LABELS,
    )

    # 计算每样本平均秒数：heart_rate_band_samples 表里 ts 间隔
    if len(samples) > 1:
        ts_deltas = [samples[i]["ts"] - samples[i-1]["ts"]
                     for i in range(1, len(samples)) if samples[i]["bpm"] and samples[i-1]["bpm"]]
        avg_secs = int(round(sum(ts_deltas) / len(ts_deltas))) if ts_deltas else 60
        # clamp 合理范围（采样间隔 5s ~ 600s）
        avg_secs = max(5, min(600, avg_secs))
    else:
        avg_secs = 60

    bd = compute_hr_zone_breakdown(bpms, hr_max=hr_max, avg_sample_seconds=avg_secs)

    print(f"心率区间（按 Zepp Cloud 绝对 bpm 阈值，HRmax={hr_max} {hr_max_src}）：")
    emoji = ("🟣", "🔵", "🟢", "🟡", "🟠", "🔴")
    pcts = (bd.zone_1_recovery_pct, bd.zone_2_warmup_pct, bd.zone_3_fatburn_pct,
            bd.zone_4_cardio_pct, bd.zone_5_endurance_pct, bd.zone_6_anaerobic_pct)
    mins = (bd.zone_1_recovery_min, bd.zone_2_warmup_min, bd.zone_3_fatburn_min,
            bd.zone_4_cardio_min, bd.zone_5_endurance_min, bd.zone_6_anaerobic_min)
    # 区间阈值标签
    bounds = [0] + list(ZONE_BOUNDS_BPM)
    for i in range(6):
        lo = bounds[i]
        hi = bounds[i+1] if i < 5 else None
        if hi is None:
            band = f"(≥{lo})"
        else:
            band = f"({lo}-{hi-1})" if lo > 0 else f"(<{hi})"
        # 分钟数 → Xh Ymin
        h = mins[i] // 60
        m = mins[i] % 60
        if h > 0:
            dur_str = f"{h}h {m:>2}min"
        else:
            dur_str = f"{m:>2} min"
        print(f"  {emoji[i]} Zone {i+1} {ZONE_LABELS[i]:<4} {band:<12} {dur_str:>10}  {pcts[i]:>5.1f}%")
    print()

    # ---- 4. vs 昨天对比 ----
    yesterday_dt = datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=1)
    yesterday = yesterday_dt.strftime("%Y-%m-%d")
    y_samples = conn.execute("""
        SELECT bpm FROM heart_rate_band_samples
        WHERE date = ?
    """, (yesterday,)).fetchall()
    if y_samples:
        y_bpms = [r["bpm"] for r in y_samples if r["bpm"] is not None and 20 <= r["bpm"] <= 300]
        if y_bpms:
            y_avg = sum(y_bpms) / len(y_bpms)
            diff_avg = avg_bpm - y_avg
            sign_avg = "+" if diff_avg >= 0 else ""
            print(f"vs 昨天（{yesterday}, {len(y_bpms)} 个采样）:")
            print(f"  平均心率  {sign_avg}{diff_avg:.1f} bpm   ({y_avg:.1f} → {avg_bpm:.1f})")
            # Zone 1 占比对比（用昨天 bpm 重算）
            y_bd = compute_hr_zone_breakdown(y_bpms, hr_max=hr_max,
                                             avg_sample_seconds=avg_secs)
            diff_zone1 = bd.zone_1_recovery_pct - y_bd.zone_1_recovery_pct
            sign_z1 = "+" if diff_zone1 >= 0 else ""
            print(f"  Zone 1 占比 {sign_z1}{diff_zone1:.1f}%   "
                  f"({y_bd.zone_1_recovery_pct:.1f}% → {bd.zone_1_recovery_pct:.1f}%)")
    else:
        print(f"vs 昨天（{yesterday}）: 无对比数据")

    # 数据完整度提示
    if samples:
        first_dt = datetime.fromtimestamp(samples[0]["ts"], tz=tz)
        last_dt = datetime.fromtimestamp(samples[-1]["ts"], tz=tz)
        span_hours = (last_dt - first_dt).total_seconds() / 3600
        if span_hours < 23:
            print(f"\n⚠ 数据完整度：{first_dt.strftime('%H:%M')} → {last_dt.strftime('%H:%M')} "
                  f"（仅 {span_hours:.1f}h，缺 {24-span_hours:.1f}h；本地重算的区间占比将偏低 Zone 1）")

    conn.close()


def cmd_summary(args):
    """DB 摘要。"""
    ensure_fresh(args.db)
    conn = connect(args.db)
    print(f"=== DB 摘要 ({args.db}) ===\n")
    for t in ("raw_records", "measurements", "capabilities", "meta"):
        n = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
        print(f"  {t:<30s} {n}")

    n_streams = conn.execute(
        "SELECT COUNT(DISTINCT metric) AS c FROM measurements"
    ).fetchone()["c"]
    print(f"  {'distinct metrics':<30s} {n_streams}")

    rng = conn.execute("""
        SELECT MIN(date) AS mn, MAX(date) AS mx,
               MIN(ts_ms) AS tmn, MAX(ts_ms) AS tmx
        FROM measurements WHERE date IS NOT NULL
    """).fetchone()
    print(f"  date 范围: {rng['mn']} ~ {rng['mx']}")
    if rng["tmn"]:
        from datetime import datetime, timezone
        first = datetime.fromtimestamp(rng["tmn"]/1000, tz=timezone.utc)
        last = datetime.fromtimestamp(rng["tmx"]/1000, tz=timezone.utc)
        print(f"  ts   范围: {first.isoformat()} ~ {last.isoformat()}")

    if SECRETS_FILE.exists():
        try:
            with open(SECRETS_FILE) as f:
                t = json.load(f)
            print(f"  token: user_id={t.get('user_id','?')}  "
                  f"region={t.get('region_host','?')}")
        except Exception:
            pass

    conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=str, default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)

    p_r = sub.add_parser("recent", help="最近 N 天关键指标摘要")
    p_r.add_argument("--days", type=int, default=7)
    p_r.set_defaults(func=cmd_recent)

    p_m = sub.add_parser("metric", help="单一指标趋势")
    p_m.add_argument("--metric", required=True)
    p_m.add_argument("--days", type=int, default=30)
    p_m.add_argument("--only-measured", action="store_true",
                     help="只显示真实手表测的数据（过滤 Zepp 推算值 source_scope=placeholder）")
    p_m.set_defaults(func=cmd_metric)

    p_rg = sub.add_parser("range", help="日期范围查询")
    p_rg.add_argument("--from", dest="from_date", required=True)
    p_rg.add_argument("--to", dest="to_date", required=True)
    p_rg.set_defaults(func=cmd_range)

    p_d = sub.add_parser("devices", help="设备列表")
    p_d.set_defaults(func=cmd_devices)

    p_c = sub.add_parser("capabilities", help="流能力状态")
    p_c.set_defaults(func=cmd_capabilities)

    p_s = sub.add_parser("summary", help="DB 摘要")
    p_s.set_defaults(func=cmd_summary)

    p_ds = sub.add_parser("daily-stress", help="某一天压力详情（Zepp App 风格）")
    p_ds.add_argument("--date", help="目标日期 YYYY-MM-DD（默认今天）")
    p_ds.set_defaults(func=cmd_daily_stress)

    p_dh = sub.add_parser("daily-hr", help="某一天心率详情（Zepp App 风格：当前/bpm 统计/6 区间占比/vs 昨天）")
    p_dh.add_argument("--date", help="目标日期 YYYY-MM-DD（默认今天）")
    p_dh.add_argument("--hr-max", type=int, help="手动指定 HRmax（默认读 device_max_hr，fallback HUNT 公式）")
    p_dh.set_defaults(func=cmd_daily_hr)

    p_w = sub.add_parser("workouts", help="运动历史（按类型汇总 / 列表 / Top）")
    p_w.add_argument("--list", action="store_true", help="列出所有运动记录")
    p_w.add_argument("--top", choices=["distance", "climb", "calorie"],
                     help="按距离/爬升/卡路里排序")
    p_w.add_argument("--sport", help="过滤运动类型（中文或英文，如 hiking/徒步）")
    p_w.add_argument("--from", dest="from_date", help="起始日期 YYYY-MM-DD")
    p_w.add_argument("--to", dest="to_date", help="结束日期 YYYY-MM-DD")
    p_w.add_argument("--limit", type=int, help="最多列出 N 条")
    p_w.set_defaults(func=cmd_workouts)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()