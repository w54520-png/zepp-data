#!/usr/bin/env python3
"""
Zepp 健康数据看板 —— 极简版

何时用：days ≤ 7 → dashboard_7d.py（24h 曲线）；days > 7 → dashboard.py（30 天趋势）；实时数据 → query_zepp.py recent。
输出：`> ~/Desktop/dash.html` 后用 minis-open 或浏览器打开。

设计原则：一屏 915px / 顶部 hero 4 KPI / 横排主题卡片 / 活动绿·心血管红·PAI 紫·压力橙·血氧蓝 / 响应式。
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

DATA_DIR = Path(os.environ.get("ZEPP_DATA_DIR", str(Path.home() / ".zepp-data")))
DEFAULT_DB = DATA_DIR / "zepp.db"


def connect(db_path):
    """打开 DB。如果不存在返回 None（不再 sys.exit）—— 让 fetch() 走空 DB 降级路径。"""
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"💡 DB 不存在：{db_path}", file=sys.stderr)
        print(f"   跑 sync 建库：python3 scripts/pull_to_sqlite.py sync --days 7", file=sys.stderr)
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def query(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def fetch(conn, days):
    # ===== P2.6：空 DB 降级 =====
    # 4 张主表全 0 行 → DB 是空的（或者表都没建）。返回 None 让 main 走空 dashboard 渲染。
    if conn is None:
        return None
    counts = {}
    for t in ["measurements", "workouts", "sleep_sessions"]:
        try:
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            # 表不存在（pull_to_sqlite.py init 都没跑过）
            counts[t] = 0
    if all(c == 0 for c in counts.values()):
        print("💡 DB 是空的（measurements/workouts/sleep_sessions 全 0）。", file=sys.stderr)
        print("   跑 sync 建库：", file=sys.stderr)
        print("     python3 scripts/pull_to_sqlite.py init            # 首次建表", file=sys.stderr)
        print("     python3 scripts/pull_to_sqlite.py sync --days 7  # 拉 7 天数据", file=sys.stderr)
        print("   或者跑 cron sync 脚本一次性建 + 拉：python3 scripts/zepp_cron_sync.sh", file=sys.stderr)
        return None

    tz = timezone(timedelta(hours=8))
    cutoff = (datetime.now(tz) - timedelta(days=days)).strftime("%Y-%m-%d")

    # sample-level 指标需要日聚合（每条 DB record 是一次采样）
    SAMPLE_LEVEL = {"hrv_rmssd", "stress", "spo2"}

    rows = query(conn, """
        SELECT metric, date, value FROM measurements WHERE date >= ?
        ORDER BY date, metric, ts_ms
    """, (cutoff,))

    # 第一遍：按日聚合 sample-level
    daily_buckets = defaultdict(lambda: defaultdict(list))  # metric -> date -> [values]
    daily_only = defaultdict(list)  # 已经是日聚合的（每天 1 条）

    for r in rows:
        m = r["metric"]
        if m in SAMPLE_LEVEL:
            daily_buckets[m][r["date"]].append(r["value"])
        else:
            daily_only[m].append((r["date"], r["value"]))

    # 第二遍：合并成 series
    series = {}
    # sample-level 转日均
    for m, by_day in daily_buckets.items():
        series[m] = sorted((d, sum(vs) / len(vs)) for d, vs in by_day.items())
    # daily-only 直接合并
    for m, pts in daily_only.items():
        if m in series:
            # 合并（避免重复）
            existing_dates = {d for d, _ in series[m]}
            for d, v in pts:
                if d not in existing_dates:
                    series[m].append((d, v))
            series[m].sort()
        else:
            series[m] = pts

    # ===== M3 章节 3.6：补 sleep_sessions 数据到 series =====
    sleep_rows = query(conn, """
        SELECT date, score, deep_secs, light_secs, rem_secs, awake_secs
        FROM sleep_sessions
        WHERE date >= ? AND is_nap = 0
        ORDER BY date
    """, (cutoff,))

    # ===== M4 章节 4.7：压力 24h 曲线（当日）=====
    # 从 raw_records 取最近 1 条 all_day_stress 的 data 字段
    stress_24h_rows = query(conn, """
        SELECT payload, start_ts_ms FROM raw_records
        WHERE stream = 'all_day_stress'
        ORDER BY start_ts_ms DESC LIMIT 1
    """)
    stress_24h_today = []
    if stress_24h_rows:
        try:
            payload = json.loads(stress_24h_rows[0]["payload"])
            # payload 是 list / dict 形状
            items = payload.get("items", []) if isinstance(payload, dict) else payload
            if isinstance(items, list) and items:
                item = items[0] if isinstance(items[0], dict) else None
                if item:
                    data_str = item.get("data")
                    if isinstance(data_str, str):
                        try:
                            pts = json.loads(data_str)
                            if isinstance(pts, list):
                                # 转成本地分钟 (Asia/Shanghai +8)
                                for p in pts:
                                    if not isinstance(p, dict):
                                        continue
                                    v = p.get("value")
                                    t_ms = p.get("time")
                                    if v is None or t_ms is None:
                                        continue
                                    if not (1 <= v <= 100):
                                        continue
                                    # UTC ms → 本地分钟
                                    local_min = int((t_ms / 1000 + 8 * 3600) / 60) % (24 * 60)
                                    stress_24h_today.append((local_min, float(v)))
                        except Exception:
                            pass
        except Exception:
            pass
    series["stress_24h_today"] = sorted(stress_24h_today)
    sleep_by_day = defaultdict(list)
    for r in sleep_rows:
        sleep_by_day[r["date"]].append(r)
    # 每日：score/deep/light/rem 取均或最近
    if sleep_by_day:
        sleep_score, sleep_deep, sleep_light, sleep_rem = [], [], [], []
        for date_str, items in sorted(sleep_by_day.items()):
            scores = [x["score"] for x in items if x["score"]]
            deeps = [x["deep_secs"] for x in items if x["deep_secs"]]
            lights = [x["light_secs"] for x in items if x["light_secs"]]
            rems = [x["rem_secs"] for x in items if x["rem_secs"]]
            if scores: sleep_score.append((date_str, sum(scores) / len(scores)))
            if deeps: sleep_deep.append((date_str, sum(deeps) / len(deeps) / 60.0))  # sec → min
            if lights: sleep_light.append((date_str, sum(lights) / len(lights) / 60.0))
            if rems: sleep_rem.append((date_str, sum(rems) / len(rems) / 60.0))
        series["sleep_score"] = sleep_score
        series["sleep_deep"] = sleep_deep
        series["sleep_light"] = sleep_light
        series["sleep_rem"] = sleep_rem
        # ===== M4 4.6：deep+light+rem 累加 → 总睡眠小时数 =====
        sleep_total_h = []
        # 用 dict 聚合避免同日多次 nap 重复
        totals_by_day = defaultdict(lambda: {"deep": 0, "light": 0, "rem": 0, "secs": 0, "days": 0})
        for r in sleep_rows:
            d = r["date"]
            totals_by_day[d]["days"] += 1
            totals_by_day[d]["deep"] += r["deep_secs"] or 0
            totals_by_day[d]["light"] += r["light_secs"] or 0
            totals_by_day[d]["rem"] += r["rem_secs"] or 0
        for d, info in sorted(totals_by_day.items()):
            total_sec = info["deep"] + info["light"] + info["rem"]
            if total_sec > 0:
                sleep_total_h.append((d, round(total_sec / 3600.0, 2)))
        series["sleep_deep_light_rem_h"] = sleep_total_h

    # ===== M3 章节 3.6：补 weight 流数据到 series =====
    weight_rows = query(conn, """
        SELECT date, metric, value FROM measurements
        WHERE date >= ? AND stream = 'weight' AND metric IN ('weight', 'bmi', 'body_fat_rate')
        ORDER BY date
    """, (cutoff,))
    weight_by_metric = defaultdict(list)
    for r in weight_rows:
        weight_by_metric[r["metric"]].append((r["date"], r["value"]))
    for m, pts in weight_by_metric.items():
        # 每日取最后一个值（体脂秤通常每天最多 1 次）
        seen = {}
        for d, v in pts:
            seen[d] = v  # 后到的覆盖前到的（按时间序）
        series[m] = sorted(seen.items())

    summary = {
        "raw_records": query(conn, "SELECT COUNT(*) AS c FROM raw_records")[0]["c"],
        "measurements": query(conn, "SELECT COUNT(*) AS c FROM measurements")[0]["c"],
        "distinct_metrics": query(conn, "SELECT COUNT(DISTINCT metric) AS c FROM measurements")[0]["c"],
    }
    summary["date_range_start"] = cutoff
    summary["date_range_end"] = query(conn,
        "SELECT MAX(date) AS s FROM measurements WHERE date >= ?", (cutoff,))[0]["s"]
    return series, summary


# 主题分组（每组一个 row）
THEMES = [
    {"icon": "📊", "title": "活动", "color": "#10b981",
     "metrics": [
         ("steps", "步数", "步", lambda v: f"{v:,.0f}"),
         ("calories", "卡路里", "kcal", lambda v: f"{v:,.0f}"),
         ("active_minutes", "活动", "min", lambda v: f"{v:.0f}"),
     ]},
    {"icon": "❤️", "title": "心血管", "color": "#ef4444",
     "metrics": [
         ("device_resting_hr", "静息心率", "bpm", lambda v: f"{v:.0f}"),
         ("device_max_hr", "最大心率", "bpm", lambda v: f"{v:.0f}"),
         ("hrv_rmssd", "HRV 日均", "ms", lambda v: f"{v:.0f}"),
     ]},
    {"icon": "💪", "title": "PAI", "color": "#a855f7",
     "metrics": [
         ("pai_total", "7天累计", "pai", lambda v: f"{v:.0f}"),
         ("pai_daily", "今日", "pai", lambda v: f"{v:.1f}"),
         ("sport_load_today", "训练", "load", lambda v: f"{v:.0f}"),
     ]},
    {"icon": "😤", "title": "压力", "color": "#f59e0b",
     "metrics": [
         ("stress", "均值", "score", lambda v: f"{v:.0f}"),
         ("stress_min", "最低", "score", lambda v: f"{v:.0f}"),
         ("stress_max", "最高", "score", lambda v: f"{v:.0f}"),
     ]},
    {"icon": "💗", "title": "血氧", "color": "#3b82f6",
     "metrics": [
         ("spo2", "最近", "%", lambda v: f"{v:.0f}"),
         ("spo2_night_score", "夜间", "score", lambda v: f"{v:.0f}"),
         ("spo2_odi", "ODI", "ev/h", lambda v: f"{v:.1f}"),
     ]},
    # ===== M3 章节 3.6：新增睡眠 tab + 体重 tab =====
    {"icon": "💤", "title": "睡眠", "color": "#6366f1",
     "metrics": [
         ("sleep_score", "评分", "score", lambda v: f"{v:.0f}"),
         ("sleep_deep", "深睡", "min", lambda v: f"{v:.0f}"),
         ("sleep_light", "浅睡", "min", lambda v: f"{v:.0f}"),
         ("sleep_rem", "REM", "min", lambda v: f"{v:.0f}"),
     ]},
    {"icon": "⚖️", "title": "体重", "color": "#06b6d4",
     "metrics": [
         ("weight", "体重", "kg", lambda v: f"{v:.1f}"),
         ("bmi", "BMI", "kg/m²", lambda v: f"{v:.1f}"),
         ("body_fat_rate", "体脂率", "%", lambda v: f"{v:.1f}"),
     ]},
]


def render_metric_card(theme_color, label, value, unit, delta_html, badge):
    return f"""
        <div class="metric-card">
          <div class="metric-label">{label}</div>
          <div class="metric-value">{value}<span class="metric-unit">{unit}</span></div>
          <div class="metric-foot">
            <span class="delta-{delta_html['dir']}">{delta_html['icon']} {delta_html['text']}</span>
            {badge}
          </div>
        </div>"""


def compute_delta(pts):
    """计算与上一期的差值方向 + 文本。"""
    if len(pts) < 2:
        return {"dir": "flat", "icon": "—", "text": "首次记录"}
    prev, latest = pts[-2][1], pts[-1][1]
    d = latest - prev
    if d > 0:
        return {"dir": "up", "icon": "▲", "text": f"+{d:.1f}"}
    if d < 0:
        return {"dir": "down", "icon": "▼", "text": f"{d:.1f}"}
    return {"dir": "flat", "icon": "—", "text": "持平"}


def build_html(series, summary, days):
    # Hero
    steps_total = sum(p[1] for p in series.get("steps", []))
    steps_days = len(series.get("steps", []))
    def latest(metric):
        pts = series.get(metric, [])
        return pts[-1][1] if pts else 0
    pai_total_latest = latest("pai_total")
    spo2_latest = latest("spo2")
    rhr_latest = latest("device_resting_hr")

    # Themes（server-side 渲染）
    themes_html = ""
    for theme in THEMES:
        cards_html = ""
        for metric_key, label, unit, fmt in theme["metrics"]:
            pts = series.get(metric_key, [])
            if not pts:
                continue
            latest_val = pts[-1][1] if pts[-1][1] is not None else 0
            value = fmt(latest_val)
            delta = compute_delta(pts)
            days_count = len(pts)
            badge = f'<span class="badge">{days_count}天</span>'
            cards_html += render_metric_card(theme["color"], label, value, unit, delta, badge)
        if cards_html:
            themes_html += f"""
    <section class="theme">
      <div class="theme-bar" style="background:{theme['color']};"></div>
      <div class="theme-body">
        <div class="theme-title"><span class="icon">{theme['icon']}</span><span>{theme['title']}</span></div>
        <div class="theme-cards">{cards_html}</div>
      </div>
    </section>"""

    # M4 章节 4.6：chart.js 趋势线 4 个
    chart_js = _build_chart_js_section(series, days)
    chart_js_block = f"""
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
{chart_js}
</script>
""" if chart_js else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Zepp 健康看板</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg: #0a0e1a; --card: rgba(255,255,255,0.04);
    --border: rgba(255,255,255,0.08);
    --text: #e4e6eb; --dim: #8b8f9a; --faint: #5d6068;
  }}
  body {{
    font-family: -apple-system, "SF Pro", "PingFang SC", sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh; padding: 14px;
    font-feature-settings: "tnum";
  }}
  .container {{ max-width: 720px; margin: 0 auto; }}
  /* Header */
  header {{
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 12px;
  }}
  .logo {{ display: flex; align-items: center; gap: 8px; }}
  .logo h1 {{ font-size: 16px; font-weight: 600; }}
  .logo .icon {{ font-size: 20px; }}
  .meta {{ font-size: 10px; color: var(--dim); text-align: right; line-height: 1.4; }}

  /* Hero: 2x2 grid */
  .hero {{
    display: grid; grid-template-columns: repeat(2, 1fr);
    gap: 8px; margin-bottom: 12px;
  }}
  .hero-card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 14px;
  }}
  .hero-label {{
    font-size: 10px; color: var(--dim);
    text-transform: uppercase; letter-spacing: 0.04em;
  }}
  .hero-value {{
    font-size: 26px; font-weight: 700; margin-top: 4px; line-height: 1.1;
  }}
  .hero-unit {{ font-size: 11px; color: var(--dim); font-weight: 400; margin-left: 3px; }}
  .hero-sub {{ font-size: 10px; color: var(--faint); margin-top: 2px; }}

  /* Theme row */
  .theme {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; margin-bottom: 8px;
    display: flex; overflow: hidden;
  }}
  .theme-bar {{ width: 3px; flex-shrink: 0; }}
  .theme-body {{ flex: 1; padding: 10px 12px; }}
  .theme-title {{
    display: flex; align-items: center; gap: 6px;
    font-size: 12px; color: var(--dim);
    margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.04em;
  }}
  .theme-title .icon {{ font-size: 14px; }}
  .theme-cards {{
    display: grid; grid-template-columns: repeat(3, 1fr);
    gap: 8px;
  }}

  /* Metric card (compact) */
  .metric-card {{
    background: rgba(255,255,255,0.02);
    border-radius: 6px; padding: 8px 4px; text-align: center;
  }}
  .metric-label {{
    font-size: 10px; color: var(--dim);
    text-transform: uppercase; letter-spacing: 0.03em;
  }}
  .metric-value {{
    font-size: 18px; font-weight: 700; margin-top: 2px; line-height: 1.1;
  }}
  .metric-unit {{ font-size: 9px; color: var(--dim); font-weight: 400; margin-left: 2px; }}
  .metric-foot {{
    margin-top: 4px; font-size: 9px; line-height: 1.3;
    display: flex; justify-content: center; align-items: center; gap: 4px;
  }}
  .delta-up {{ color: #10b981; }}
  .delta-down {{ color: #ef4444; }}
  .delta-flat {{ color: var(--dim); }}
  .badge {{
    background: rgba(255,255,255,0.05); color: var(--faint);
    padding: 1px 4px; border-radius: 3px; font-size: 9px;
  }}

  @media (max-width: 380px) {{
    .hero-value {{ font-size: 22px; }}
    .metric-value {{ font-size: 16px; }}
    .theme-cards {{ gap: 4px; }}
  }}
</style>
</head>
<body>
<div class="container">

<header>
  <div class="logo">
    <span class="icon">⌚</span>
    <h1>Zepp 健康看板</h1>
  </div>
  <div class="meta">
    <div>{summary['date_range_start']} ~ {summary['date_range_end']}</div>
    <div>{days}天 · {summary['measurements']:,}点</div>
  </div>
</header>

<div class="hero">
  <div class="hero-card">
    <div class="hero-label">📊 总步数</div>
    <div class="hero-value">{steps_total:,.0f}<span class="hero-unit">步</span></div>
    <div class="hero-sub">{steps_days} 天</div>
  </div>
  <div class="hero-card">
    <div class="hero-label">💪 PAI 7天</div>
    <div class="hero-value">{pai_total_latest:.0f}<span class="hero-unit">pai</span></div>
    <div class="hero-sub">最近</div>
  </div>
  <div class="hero-card">
    <div class="hero-label">💗 血氧</div>
    <div class="hero-value">{spo2_latest:.0f}<span class="hero-unit">%</span></div>
    <div class="hero-sub">最近</div>
  </div>
  <div class="hero-card">
    <div class="hero-label">❤️ 静息心率</div>
    <div class="hero-value">{rhr_latest:.0f}<span class="hero-unit">bpm</span></div>
    <div class="hero-sub">最近</div>
  </div>
</div>

{themes_html}

<!-- M4 章节 4.6：4 个 chart.js 趋势线 -->
<section class="charts">
  <div class="chart-card"><h3>📈 HRV (RMSSD) - 最近 30 天</h3><div class="chart-wrap"><canvas id="hrv-chart"></canvas></div></div>
  <div class="chart-card"><h3>📊 步数 - 最近 30 天</h3><div class="chart-wrap"><canvas id="steps-chart"></canvas></div></div>
  <div class="chart-card"><h3>😤 压力均值 - 最近 30 天</h3><div class="chart-wrap"><canvas id="stress-chart"></canvas></div></div>
  <div class="chart-card"><h3>💤 睡眠时长 - 最近 30 天</h3><div class="chart-wrap"><canvas id="sleep-chart"></canvas></div></div>
</section>

<!-- M4 章节 4.7：压力 24h 曲线（当日） -->
<section class="charts-full">
  <div class="chart-card"><h3>😤 今日压力 24h 曲线</h3><div class="chart-wrap-tall"><canvas id="stress-24h-chart"></canvas></div></div>
</section>

<style>
.charts-full {{
  margin-top: 8px;
}}
.chart-wrap-tall {{
  position: relative; height: 200px;
}}
</style>

<style>
.charts {{
  display: grid; grid-template-columns: repeat(2, 1fr);
  gap: 8px; margin-top: 12px;
}}
.chart-card {{
  background: var(--card); border: 1px solid var(--border);
  border-radius: 10px; padding: 10px 12px;
}}
.chart-card h3 {{
  font-size: 11px; font-weight: 500; color: var(--dim);
  margin-bottom: 6px; letter-spacing: 0.03em;
}}
.chart-wrap {{
  position: relative; height: 140px;
}}
@media (max-width: 480px) {{
  .charts {{ grid-template-columns: 1fr; }}
}}
</style>

{chart_js_block}

</div>
</body>
</html>"""


# ===== M4 章节 4.6：chart.js 趋势线 =====

def _build_chart_js_section(series: dict, days: int) -> str:
    """生成 4 个 chart.js 趋势线（HRV / 步数 / 压力 / 睡眠时长）。

    每个 chart 用 canvas + 简化配置（line chart）。
    数据点从 series 取最近 N 天。
    """
    import json as _json
    charts = [
        ("hrv-chart", "HRV (RMSSD)", series.get("hrv_rmssd", []),
         "ms", "rgba(239, 68, 68, 0.8)", "rgba(239, 68, 68, 0.1)"),
        ("steps-chart", "步数", series.get("steps", []),
         "步", "rgba(16, 185, 129, 0.8)", "rgba(16, 185, 129, 0.1)"),
        ("stress-chart", "压力均值", series.get("stress", []),
         "score", "rgba(245, 158, 11, 0.8)", "rgba(245, 158, 11, 0.1)"),
        # 睡眠时长：优先 deep+light+rem 累加（分钟 → 小时）；fallback time_in_bed
        ("sleep-chart", "睡眠时长", series.get("sleep_deep_light_rem_h", []),
         "小时", "rgba(99, 102, 241, 0.8)", "rgba(99, 102, 241, 0.1)"),
    ]
    # M4 4.7：压力 24h 曲线（当日）—— 用 line chart，X 轴是 0..1439 分钟
    stress_24h = series.get("stress_24h_today", [])
    if stress_24h:
        labels = [f"{m // 60:02d}:{m % 60:02d}" for m, _ in stress_24h]
        values = [v for _, v in stress_24h]
        parts.append(f"""
new Chart(document.getElementById('stress-24h-chart'), {{
  type: 'line',
  data: {{
    labels: {_json.dumps(labels)},
    datasets: [{{
      label: '今日压力 24h (本地时间)',
      data: {_json.dumps(values)},
      borderColor: 'rgba(245, 158, 11, 0.8)',
      backgroundColor: 'rgba(245, 158, 11, 0.1)',
      borderWidth: 2,
      fill: true,
      tension: 0.3,
      pointRadius: 1,
      pointHoverRadius: 3,
      spanGaps: true,
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ maxTicksLimit: 6, font: {{ size: 9 }} }}, grid: {{ color: 'rgba(255,255,255,0.04)' }} }},
      y: {{ min: 0, max: 100, ticks: {{ font: {{ size: 10 }} }}, grid: {{ color: 'rgba(255,255,255,0.04)' }} }},
    }}
  }}
}});
""")
    parts = []
    for canvas_id, title, pts, unit, line_color, fill_color in charts:
        if not pts:
            continue
        labels = [str(d) for d, _ in pts[-30:]]
        values = [round(v, 1) if v is not None else None for _, v in pts[-30:]]
        parts.append(f"""
new Chart(document.getElementById('{canvas_id}'), {{
  type: 'line',
  data: {{
    labels: {_json.dumps(labels)},
    datasets: [{{
      label: '{title} ({unit})',
      data: {_json.dumps(values)},
      borderColor: '{line_color}',
      backgroundColor: '{fill_color}',
      borderWidth: 2,
      fill: true,
      tension: 0.3,
      pointRadius: 2,
      pointHoverRadius: 4,
      spanGaps: true,
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ maxTicksLimit: 8, font: {{ size: 10 }} }}, grid: {{ color: 'rgba(255,255,255,0.04)' }} }},
      y: {{ ticks: {{ font: {{ size: 10 }} }}, grid: {{ color: 'rgba(255,255,255,0.04)' }} }},
    }}
  }}
}});
""")
    return "\n".join(parts)


def _empty_dashboard(days: int) -> str:
    """空 DB / 表都不存在时，返回一个最小可用的 HTML（不抛错）。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Zepp 健康看板（空）</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, "SF Pro", "PingFang SC", sans-serif;
    background: #0a0e1a; color: #e4e6eb;
    min-height: 100vh; padding: 24px;
    display: flex; align-items: center; justify-content: center;
  }}
  .empty-card {{
    max-width: 520px; width: 100%;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 12px; padding: 32px 28px;
    text-align: center;
  }}
  .empty-icon {{ font-size: 48px; margin-bottom: 12px; }}
  .empty-title {{ font-size: 18px; font-weight: 600; margin-bottom: 8px; }}
  .empty-desc {{ font-size: 13px; color: #8b8f9a; margin-bottom: 20px; line-height: 1.6; }}
  .empty-code {{
    background: rgba(0,0,0,0.4); color: #10b981;
    font-family: "SF Mono", "Menlo", monospace;
    font-size: 12px; padding: 10px 14px;
    border-radius: 6px; text-align: left;
    margin: 6px 0;
  }}
  .empty-meta {{ font-size: 10px; color: #5d6068; margin-top: 18px; }}
</style>
</head>
<body>
<div class="empty-card">
  <div class="empty-icon">⌚</div>
  <div class="empty-title">Zepp 健康看板（{days} 天）</div>
  <div class="empty-desc">
    DB 里没有任何数据。先 OAuth 登录 + 跑 sync 把数据拉到本地 SQLite。
  </div>
  <div class="empty-code">python3 scripts/zepp_oauth.py login</div>
  <div class="empty-code">python3 scripts/pull_to_sqlite.py init</div>
  <div class="empty-code">python3 scripts/pull_to_sqlite.py sync --days {days}</div>
  <div class="empty-meta">dashboard.py · 空状态降级（P2.6）</div>
</div>
</body>
</html>
"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--db", type=str, default=str(DEFAULT_DB))
    args = p.parse_args()

    conn = connect(args.db)
    fetched = fetch(conn, args.days)
    if fetched is None:
        # DB 不存在 / 表都空 → 渲染最小空状态 HTML（不抛错）
        print(_empty_dashboard(args.days))
        return 0
    series, summary = fetched
    html = build_html(series, summary, args.days)
    print(html)


if __name__ == "__main__":
    main()