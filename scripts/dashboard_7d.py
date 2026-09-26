#!/usr/bin/env python3
"""
最近 7 天 Zepp 健康看板 —— 显示每日数据（点+柱+密度）

点击行展开，看每天的具体值 + 每天有多少样本。
"""
from __future__ import annotations
import argparse
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
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"✗ DB 不存在：{db_path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def query(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def fetch(conn, days):
    """返回 {metric: {date: {value: 日均, count: 样本数, min, max}}}"""
    tz = timezone(timedelta(hours=8))
    cutoff = (datetime.now(tz) - timedelta(days=days)).strftime("%Y-%m-%d")

    # 一次 SQL 拉所有数据
    SAMPLE_LEVEL = {"hrv_rmssd", "stress", "spo2"}

    rows = query(conn, """
        SELECT metric, date, value, ts_ms
        FROM measurements WHERE date >= ?
        ORDER BY date, metric, ts_ms
    """, (cutoff,))

    # 聚合到 daily: {date: {values, count, min, max}}
    by_metric_date = defaultdict(lambda: defaultdict(lambda: {"values": [], "count": 0}))
    for r in rows:
        m = r["metric"]
        d = r["date"]
        bucket = by_metric_date[m][d]
        bucket["values"].append(r["value"])
        bucket["count"] += 1

    # 算 daily mean + min + max
    daily = {}
    for m, dates in by_metric_date.items():
        daily[m] = {}
        for d, b in dates.items():
            vals = b["values"]
            daily[m][d] = {
                "mean": sum(vals) / len(vals),
                "count": b["count"],
                "min": min(vals),
                "max": max(vals),
            }
    return daily


KEY_METRICS = [
    ("steps", "📊 步数", "步", "high", lambda v: f"{v:,.0f}"),
    ("device_resting_hr", "❤️ 静息心率", "bpm", "low", lambda v: f"{v:.0f}"),
    ("hrv_rmssd", "💓 HRV 日均", "ms", "high", lambda v: f"{v:.0f}"),
    ("stress", "😤 压力日均", "score", "low", lambda v: f"{v:.0f}"),
    ("spo2", "🫁 血氧日均", "%", "high", lambda v: f"{v:.0f}"),
    ("calories", "🔥 卡路里", "kcal", None, lambda v: f"{v:,.0f}"),
]

# 标记 sample-level 流（数据密度看 sample 数）
SAMPLE_LEVEL = {"hrv_rmssd", "stress", "spo2"}


def sparkline_path(pts, w=120, h=28):
    """从 [{date, mean, count}] 列表画 sparkline。"""
    if len(pts) < 2:
        return ""
    vals = [p["mean"] for p in pts]
    vmin, vmax = min(vals), max(vals)
    rng = vmax - vmin or 1
    n = len(vals)
    step = w / (n - 1)
    points = []
    for i, v in enumerate(vals):
        x = i * step
        y = h - (v - vmin) / rng * h
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def day_color(v, vmin, vmax, direction):
    if vmax == vmin or direction is None:
        return "var(--text)"
    ratio = (v - vmin) / (vmax - vmin) if vmax > vmin else 0.5
    if direction == "high":
        if ratio > 0.6: return "#10b981"
        if ratio < 0.3: return "#ef4444"
        return "var(--dim)"
    if direction == "low":
        if ratio < 0.3: return "#10b981"
        if ratio > 0.6: return "#ef4444"
        return "var(--dim)"
    return "var(--text)"


def build_html(daily, days):
    blocks = []
    for metric_key, label, unit, direction, fmt in KEY_METRICS:
        dates = daily.get(metric_key, {})
        if not dates:
            blocks.append(f"""
      <details class="metric-details">
        <summary>
          <span class="label">{label}</span>
          <span class="value muted">无数据</span>
          <span class="trend"></span>
        </summary>
        <div class="detail-content muted" style="padding:8px;font-size:11px;">该指标在此期间无数据</div>
      </details>""")
            continue

        # 转成 sorted 列表
        sorted_dates = sorted(dates.keys())
        pts = [{"date": d, **dates[d]} for d in sorted_dates]
        latest = pts[-1]
        avg = sum(p["mean"] for p in pts) / len(pts)
        min_v = min(p["min"] for p in pts)
        max_v = max(p["max"] for p in pts)
        total_samples = sum(p["count"] for p in pts)

        value_str = fmt(latest["mean"])

        # 趋势箭头（与上一天比）
        if len(pts) >= 2:
            prev, curr = pts[-2]["mean"], latest["mean"]
            d = curr - prev
            if d != 0:
                if direction == "low":
                    is_good = d < 0
                elif direction == "high":
                    is_good = d > 0
                else:
                    is_good = True
                color = "#10b981" if is_good else "#ef4444"
                arrow = "▲" if d > 0 else "▼"
                delta_str = f'<span style="color:{color};font-weight:600;font-size:11px;">{arrow} {abs(d):.1f}</span>'
            else:
                delta_str = '<span style="color:var(--faint);font-size:11px;">— 持平</span>'
        else:
            delta_str = '<span style="color:var(--faint);font-size:11px;">—</span>'

        # 数据密度
        if metric_key in SAMPLE_LEVEL:
            # sample-level: 看每天多少 sample
            avg_samples = total_samples / max(len(pts), 1)
            if avg_samples >= 50:
                density = "🟢 高"
            elif avg_samples >= 10:
                density = "🟡 中"
            else:
                density = "🔴 低"
        else:
            # daily-level: 1 个值/天（正常），缺失天数是关键
            missing = days - len(pts)
            if missing == 0:
                density = "🟢 完整"
            elif missing <= 1:
                density = "🟡 缺1天"
            else:
                density = f"🔴 缺{missing}天"

        sparkline = sparkline_path(pts, w=120, h=28)
        sparkline_svg = f'<svg width="120" height="28" viewBox="0 0 120 28" class="sparkline"><polyline points="{sparkline}" fill="none" stroke="#10b981" stroke-width="1.5"/></svg>' if sparkline else ""

        # 每日明细
        day_cells = ""
        for p in pts:
            vmin, vmax = min(pp["min"] for pp in pts), max(pp["max"] for pp in pts)
            color = day_color(p["mean"], vmin, vmax, direction)
            if vmax > vmin:
                bar_pct = (p["mean"] - vmin) / (vmax - vmin) * 100
            else:
                bar_pct = 100
            count = p["count"]
            day_cells += f"""
            <div class="day">
              <div class="day-bar" style="height:{bar_pct}%; background:{color};"></div>
              <div class="day-value" style="color:{color};">{fmt(p['mean'])}</div>
              <div class="day-meta">{count} 样本</div>
              <div class="day-date">{p['date'][5:]}</div>
            </div>"""

        blocks.append(f"""
      <details class="metric-details">
        <summary>
          <span class="label">{label}</span>
          <span class="value">{value_str}<span class="unit">{unit}</span> · <span class="avg-tag">avg {fmt(avg)}</span></span>
          <span class="trend">{delta_str}<br>{sparkline_svg}</span>
        </summary>
        <div class="detail-content">
          <div class="day-grid">{day_cells}</div>
          <div class="day-legend">
            <span class="muted">min {fmt(min_v)} · max {fmt(max_v)} · {len(pts)}天 · {density}数据密度（{total_samples}样本）</span>
          </div>
        </div>
      </details>""")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Zepp · 最近 {days} 天</title>
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
  .container {{ max-width: 520px; margin: 0 auto; }}
  header {{
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 12px; padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }}
  header h1 {{ font-size: 18px; font-weight: 600; }}
  header h1 .icon {{ font-size: 22px; }}
  .sub {{ font-size: 11px; color: var(--dim); }}

  .metric-details {{ border-bottom: 1px solid var(--border); }}
  .metric-details[open] {{ background: rgba(255,255,255,0.015); }}
  .metric-details summary {{
    display: flex; align-items: center;
    padding: 10px 4px; cursor: pointer;
    list-style: none; user-select: none;
  }}
  .metric-details summary::-webkit-details-marker {{ display: none; }}
  .metric-details summary::marker {{ display: none; content: ''; }}
  .metric-details summary:hover {{ background: rgba(255,255,255,0.02); }}
  .label {{
    flex: 1; font-size: 13px; color: var(--text);
    display: flex; align-items: center; gap: 4px;
  }}
  .label::before {{
    content: '▸'; font-size: 10px; color: var(--faint);
    transition: transform 0.2s; display: inline-block;
  }}
  .metric-details[open] .label::before {{ transform: rotate(90deg); color: var(--text); }}
  .value {{
    font-size: 18px; font-weight: 700;
    text-align: right; white-space: nowrap;
  }}
  .value .unit {{ font-size: 10px; color: var(--dim); font-weight: 400; margin-left: 3px; }}
  .value .avg-tag {{
    font-size: 9px; color: var(--dim); font-weight: 400; margin-left: 6px;
    background: rgba(255,255,255,0.04); padding: 1px 5px; border-radius: 3px;
  }}
  .value.muted {{ color: var(--faint); font-weight: 400; font-size: 12px; }}
  .trend {{
    font-size: 11px; text-align: right;
    min-width: 130px; line-height: 1.4;
  }}
  .sparkline {{ vertical-align: middle; margin-top: 2px; }}

  .detail-content {{ padding: 0 4px 12px 24px; }}
  .day-grid {{
    display: flex; align-items: flex-end; gap: 4px;
    height: 92px;
    margin-bottom: 6px;
  }}
  .day {{
    flex: 1; display: flex; flex-direction: column;
    align-items: center; justify-content: flex-end;
    height: 100%;
  }}
  .day-bar {{
    width: 70%; max-width: 28px;
    border-radius: 3px 3px 0 0;
    min-height: 2px;
  }}
  .day-value {{
    font-size: 11px; font-weight: 600;
    margin-top: 4px; line-height: 1.1;
  }}
  .day-meta {{
    font-size: 8px; color: var(--dim);
    margin-top: 1px; line-height: 1.1;
  }}
  .day-date {{
    font-size: 9px; color: var(--faint);
    margin-top: 2px;
  }}
  .day-legend {{ margin-top: 4px; }}
  .day-legend .muted {{ color: var(--faint); font-size: 10px; }}

  @media (max-width: 380px) {{
    body {{ padding: 10px; }}
    .value {{ font-size: 16px; }}
    .label {{ font-size: 12px; }}
  }}
</style>
</head>
<body>
<div class="container">

<header>
  <h1><span class="icon">⌚</span> Zepp · 最近 {days} 天</h1>
  <span class="sub">点击行展开每日明细</span>
</header>

{''.join(blocks)}

</div>
</body>
</html>"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--db", type=str, default=str(DEFAULT_DB))
    args = p.parse_args()

    conn = connect(args.db)
    daily = fetch(conn, args.days)
    html = build_html(daily, args.days)
    print(html)


if __name__ == "__main__":
    main()