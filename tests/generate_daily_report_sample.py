#!/usr/bin/env python3
"""
M8 日报设计 — 真实样本生成脚本（一次性）

输入：
  - Zepp SQLite DB（/root/.zepp-data/zepp.db）
  - 目标日期：默认 = t-1 = 2026-09-25（今天 2026-09-26）

输出：
  - Markdown 日报到 stdout
  - 同时写一份到 /tmp/M8_daily_report_2026-09-25.md

设计原则：
  - 不写 SKILL.md / scripts/ —— 只读 + 输出
  - 直接 SQL 查 DB（不依赖 query_zepp 子命令，因它会触发 sync 副作用）
  - HR 区间阈值沿用 query_zepp.py daily-hr 的逻辑（HRmax=187 device_max_hr）
"""
from __future__ import annotations
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median

# 复用 skill 自带的 time_utils（不引入新依赖）
_SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SKILL_DIR / "scripts"))
from time_utils import ms_to_local_dt, DEFAULT_TZ  # noqa: E402

DB = Path("/root/.zepp-data/zepp.db")
TARGET_DATE = "2026-09-25"  # t-1

# Device HRmax（沿用 Zepp App 算法）
HR_MAX = 187


def fmt_hms(seconds: int) -> str:
    """秒 → 'Xh Ym' / 'Xm' / 'Xh'"""
    if seconds is None or seconds <= 0:
        return "0m"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def fmt_signed(value, unit="", decimals=1) -> str:
    if value is None:
        return "—"
    if value > 0:
        return f"+{value:.{decimals}f}{unit}"
    if value < 0:
        return f"{value:.{decimals}f}{unit}"
    return f"0{unit}"


def pct_bar(pct: float, width: int = 12) -> str:
    """ASCII progress bar"""
    pct = max(0.0, min(100.0, pct))
    fill = int(round(pct / 100 * width))
    return "█" * fill + "░" * (width - fill)


def days_ago_label(ts_ms: int) -> str:
    """ms 时间戳 → 'YYYY-MM-DD（X 天前）' 北京时间."""
    local = ms_to_local_dt(ts_ms)
    if local is None:
        return "—"
    date_str = local.strftime("%Y-%m-%d")
    now_local = datetime.now(DEFAULT_TZ)
    diff_days = int((now_local - local).total_seconds() // 86400)
    if diff_days == 0:
        suffix = "今天"
    elif diff_days == 1:
        suffix = "昨天"
    else:
        suffix = f"{diff_days} 天前"
    return f"{date_str}（{suffix}）"


def bmi_category(bmi: float) -> str:
    """BMI → 国标分类中文."""
    if bmi is None:
        return ""
    if bmi < 18.5:
        return "偏瘦"
    if bmi < 24.0:
        return "偏一般"
    if bmi < 28.0:
        return "偏胖"
    return "肥胖"


def mifflin_bmr(gender: int, weight_kg: float, height_cm: float, age: int) -> float:
    """Mifflin-St Jeor 公式（学界标准，1990）。

    Zepp gender 反人类约定：1=男, 0=女。
      男 BMR = 10 × W + 6.25 × H - 5 × A + 5
      女 BMR = 10 × W + 6.25 × H - 5 × A - 161
    """
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + 5 if gender == 1 else base - 161


def calculate_age_from_birthday(birthday_str: str | None, today: datetime | None = None) -> int | None:
    """从 'YYYY-MM' 字符串算当前年龄（精确到年）。

    '1990-01' 在 2026-09-26 → 36（示例）。
    返回 None 如果 birthday_str 为空 / 格式不合法。
    """
    if not birthday_str:
        return None
    today = today or datetime.now()
    try:
        yr, mo = birthday_str.split("-")
        bd = datetime(int(yr), int(mo), 1)
        age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
        return age
    except (ValueError, AttributeError):
        return None


def query_user_profile(cur, member_id: str = "-1") -> dict:
    """从 user_profile 表读单条档案。

    返回 dict：{height, birthday, gender, weight, nickname, ...}；缺字段返 None。
    主账号用 member_id='-1'；家庭成员用实际 ID。
    """
    row = cur.execute("""
        SELECT height, birthday, gender, weight, nickname, user_id
        FROM user_profile WHERE member_id = ?
    """, (member_id,)).fetchone()
    if not row:
        return {"height": None, "birthday": None, "gender": None,
                "weight": None, "nickname": None, "user_id": None}
    return {
        "height": float(row["height"]) if row["height"] is not None else None,
        "birthday": row["birthday"],
        "gender": int(row["gender"]) if row["gender"] is not None else None,
        "weight": float(row["weight"]) if row["weight"] is not None else None,
        "nickname": row["nickname"],
        "user_id": row["user_id"],
    }


def get_conn():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    return conn


def fetch_one(cur, sql, params=()):
    r = cur.execute(sql, params).fetchone()
    return dict(r) if r else None


def load_data(date_str: str) -> dict:
    """从 DB 加载 date_str 当天所有需要的数据"""
    conn = get_conn()
    cur = conn.cursor()
    d = {}

    # ===== 1. 每日聚合（ts_ms = 当天 00:00 UTC = 08:00 北京）=====
    daily_streams = [
        "steps", "calories", "active_minutes", "device_resting_hr", "device_max_hr",
        "pai_total", "stress_min", "stress_max", "stress_relax_pct", "stress_normal_pct",
        "stress_medium_pct", "stress_high_pct", "weight", "bmi", "body_fat_rate",
        "muscle_mass", "respiratory_rate", "step_goal", "calorie_goal",
    ]
    placeholders = ",".join("?" * len(daily_streams))
    rows = cur.execute(f"""
        SELECT stream, value, unit
        FROM measurements
        WHERE date = ?
          AND stream IN ({placeholders})
          AND ts_ms = strftime('%s', date) * 1000
    """, [date_str] + daily_streams).fetchall()
    d["daily"] = {r["stream"]: {"value": r["value"], "unit": r["unit"]} for r in rows}

    # ===== 2. 7 天均值（steps / calories / active_minutes / rhr）=====
    rows = cur.execute("""
        SELECT date,
               MAX(CASE WHEN stream='steps' THEN value END) AS steps,
               MAX(CASE WHEN stream='calories' THEN value END) AS cal,
               MAX(CASE WHEN stream='active_minutes' THEN value END) AS active,
               MAX(CASE WHEN stream='device_resting_hr' THEN value END) AS rhr
        FROM measurements
        WHERE date BETWEEN date(?, '-6 days') AND ?
          AND ts_ms = strftime('%s', date) * 1000
          AND stream IN ('steps','calories','active_minutes','device_resting_hr')
        GROUP BY date
        ORDER BY date
    """, [date_str, date_str]).fetchall()
    days = [dict(r) for r in rows]
    d["history_days"] = days

    def avg(field, exclude_zero=False):
        vals = [x[field] for x in days if x[field] is not None]
        if exclude_zero:
            vals = [v for v in vals if v > 0]
        return mean(vals) if vals else None
    d["avg7_steps"] = avg("steps", exclude_zero=True)  # 7 天有效步数均值（排 0）
    d["avg7_cal"] = avg("cal", exclude_zero=True)
    d["avg7_active"] = avg("active", exclude_zero=True)
    d["avg7_rhr"] = avg("rhr", exclude_zero=False)

    # ===== 3. 压力曲线采样（24h）=====
    rows = cur.execute("""
        SELECT value, datetime(ts_ms/1000, 'unixepoch') as ts_iso
        FROM measurements
        WHERE stream='stress' AND date = ?
        ORDER BY ts_ms
    """, [date_str]).fetchall()
    stress_samples = [r["value"] for r in rows]
    # 哨兵过滤（>100 或 <0）
    stress_clean = [v for v in stress_samples if 0 <= v <= 100]
    d["stress_n_raw"] = len(stress_samples)
    d["stress_n_clean"] = len(stress_clean)
    d["stress_avg"] = round(mean(stress_clean), 1) if stress_clean else None
    d["stress_min"] = min(stress_clean) if stress_clean else None
    d["stress_max"] = max(stress_clean) if stress_clean else None
    # 区间占比（按全天重算）
    if stress_clean:
        n = len(stress_clean)
        d["stress_relax"] = round(100 * sum(1 for v in stress_clean if v < 40) / n, 1)
        d["stress_normal"] = round(100 * sum(1 for v in stress_clean if 40 <= v < 60) / n, 1)
        d["stress_medium"] = round(100 * sum(1 for v in stress_clean if 60 <= v < 80) / n, 1)
        d["stress_high"] = round(100 * sum(1 for v in stress_clean if v >= 80) / n, 1)
    else:
        d["stress_relax"] = d["stress_normal"] = d["stress_medium"] = d["stress_high"] = None

    # 上周同日压力均值（date - 7）
    prev7 = date_str  # use the same date - 7 from now
    from datetime import datetime as _dt
    dt_obj = _dt.strptime(date_str, "%Y-%m-%d")
    prev7_iso = (dt_obj - timedelta(days=7)).strftime("%Y-%m-%d")
    rows = cur.execute("""
        SELECT value FROM measurements
        WHERE stream='stress' AND date = ?
    """, [prev7_iso]).fetchall()
    prev_vals = [v["value"] for v in rows if 0 <= v["value"] <= 100]
    d["stress_avg_prev7d"] = round(mean(prev_vals), 1) if prev_vals else None

    # ===== 4. 心率（band samples）=====
    rows = cur.execute("""
        SELECT MIN(bpm) AS min_hr, MAX(bpm) AS max_hr, ROUND(AVG(bpm),1) AS avg_hr, COUNT(*) AS n
        FROM heart_rate_band_samples WHERE date = ?
    """, [date_str]).fetchone()
    d["hr_n"] = rows["n"]
    d["hr_min"] = rows["min_hr"]
    d["hr_max"] = rows["max_hr"]
    d["hr_avg"] = rows["avg_hr"]

    # 区间占比（Zepp Cloud 阈值：Z1<60%, Z2=60-70%, Z3=70-80%, Z4=80-90%, Z5=90-95%, Z6≥95% of HRmax）
    zones = [
        ("Z1 舒缓", 0, int(HR_MAX * 0.60)),
        ("Z2 热身", int(HR_MAX * 0.60), int(HR_MAX * 0.70)),
        ("Z3 脂肪燃烧", int(HR_MAX * 0.70), int(HR_MAX * 0.80)),
        ("Z4 心肺强化", int(HR_MAX * 0.80), int(HR_MAX * 0.90)),
        ("Z5 耐力强化", int(HR_MAX * 0.90), int(HR_MAX * 0.95)),
        ("Z6 无氧极限", int(HR_MAX * 0.95), 9999),
    ]
    d["hr_zones"] = []
    if d["hr_n"]:
        for name, lo, hi in zones:
            if hi >= 9999:
                c = cur.execute("""
                    SELECT COUNT(*) AS c FROM heart_rate_band_samples
                    WHERE date = ? AND bpm >= ?
                """, [date_str, lo]).fetchone()["c"]
            else:
                c = cur.execute("""
                    SELECT COUNT(*) AS c FROM heart_rate_band_samples
                    WHERE date = ? AND bpm >= ? AND bpm < ?
                """, [date_str, lo, hi]).fetchone()["c"]
            pct = round(100 * c / d["hr_n"], 1)
            d["hr_zones"].append({"name": name, "lo": lo, "hi": hi if hi < 9999 else "∞",
                                  "count": c, "pct": pct, "minutes": round(c / d["hr_n"] * 1440, 1)})

    # ===== 5. HRV（rmssd 采样）=====
    rows = cur.execute("""
        SELECT value FROM measurements
        WHERE stream='hrv_rmssd' AND date = ? ORDER BY value
    """, [date_str]).fetchall()
    hrv_vals = [r["value"] for r in rows if r["value"] > 0]
    if hrv_vals:
        d["hrv_n"] = len(hrv_vals)
        d["hrv_avg"] = round(mean(hrv_vals), 1)
        d["hrv_min"] = min(hrv_vals)
        d["hrv_max"] = max(hrv_vals)
    else:
        d["hrv_n"] = 0
        d["hrv_avg"] = d["hrv_min"] = d["hrv_max"] = None
    # 上周 HRV 对比
    rows = cur.execute("""
        SELECT value FROM measurements
        WHERE stream='hrv_rmssd' AND date = ?
    """, [prev7_iso]).fetchall()
    prev_hrv = [v["value"] for v in rows if v["value"] > 0]
    d["hrv_prev7d_avg"] = round(mean(prev_hrv), 1) if prev_hrv else None

    # ===== 6. 血氧 =====
    rows = cur.execute("""
        SELECT value FROM measurements
        WHERE stream='spo2' AND date = ? ORDER BY value
    """, [date_str]).fetchall()
    spo2_vals = [r["value"] for r in rows]
    if spo2_vals:
        d["spo2_n"] = len(spo2_vals)
        d["spo2_avg"] = round(mean(spo2_vals), 1)
        d["spo2_min"] = min(spo2_vals)
        d["spo2_max"] = max(spo2_vals)
        d["spo2_below95_pct"] = round(100 * sum(1 for v in spo2_vals if v < 95) / len(spo2_vals), 1)
    else:
        d["spo2_n"] = 0
        d["spo2_avg"] = d["spo2_min"] = d["spo2_max"] = d["spo2_below95_pct"] = None

    # ===== 7. 睡眠 =====
    sess = cur.execute("""
        SELECT * FROM sleep_sessions WHERE date = ? ORDER BY start_ts DESC LIMIT 1
    """, [date_str]).fetchone()
    if sess:
        s = dict(sess)
        d["sleep"] = {
            "time_in_bed_secs": s["time_in_bed_secs"],
            "deep_secs": s["deep_secs"],
            "light_secs": s["light_secs"],
            "rem_secs": s["rem_secs"],
            "awake_secs": s["awake_secs"],
            "score": s["score"],
            "rhr": s["rhr"],
            "sp_o2_avg": s["sp_o2_avg"],
            "is_nap": s["is_nap"],
            "start": datetime.fromtimestamp(s["start_ts"], tz=timezone(timedelta(hours=8))).strftime("%m-%d %H:%M"),
            "end": datetime.fromtimestamp(s["end_ts"], tz=timezone(timedelta(hours=8))).strftime("%m-%d %H:%M"),
        }
    else:
        d["sleep"] = None

    # ===== 8. 运动 =====
    rows = cur.execute("""
        SELECT trackid, sport_zh, run_s, dis_m, calorie,
               avg_heart_rate, max_heart_rate, min_heart_rate,
               datetime(end_time_ts, 'unixepoch') as end_iso
        FROM workouts
        WHERE date(end_time_iso) = ?
        ORDER BY end_time_ts
    """, [date_str]).fetchall()
    d["workouts"] = [dict(r) for r in rows]

    # ===== 9. meta 数据新鲜度 =====
    row = cur.execute("SELECT value FROM meta WHERE key='last_pull_at'").fetchone()
    d["last_pull_at"] = row["value"] if row else None

    # ===== 10. step_goal =====
    if "step_goal" in d["daily"]:
        d["step_goal"] = d["daily"]["step_goal"]["value"]
    else:
        d["step_goal"] = None

    # ===== 11. BMR（Mifflin-St Jeor 公式 + user_profile + 最新体重） — 总卡路里计算 =====
    # 不用 Zepp App 历史 BMR 中位数：Zepp 用 KATCH-MCARDLE + 瘦体重估算不准，
    # BMI 22.9 时瘦体重反算公式对比示例。Mifflin-St Jeor 是学界标准公式。
    cal_active = d["daily"].get("calories", {}).get("value")
    d["cal_active"] = cal_active

    profile = query_user_profile(cur, member_id="-1")  # 主账号
    weight_row = cur.execute("""
        SELECT value FROM measurements
        WHERE stream='weight' AND metric='weight'
        ORDER BY ts_ms DESC LIMIT 1
    """).fetchone()
    latest_weight = float(weight_row["value"]) if weight_row and weight_row["value"] else None
    d["latest_weight_kg"] = latest_weight

    d["profile_height_cm"] = profile.get("height")
    d["profile_birthday"] = profile.get("birthday")
    d["profile_gender"] = profile.get("gender")
    age = calculate_age_from_birthday(profile.get("birthday")) if profile.get("birthday") else None
    d["profile_age"] = age

    gender = profile.get("gender")
    height_cm = profile.get("height")
    if (latest_weight is not None and height_cm is not None
            and age is not None and gender in (0, 1)):
        bmr_mifflin = mifflin_bmr(gender, latest_weight, height_cm, age)
        d["bmr_mifflin"] = round(bmr_mifflin, 1)
        d["bmr_source"] = (f"Mifflin-St Jeor (身高 {height_cm:.0f}cm + 体重 {latest_weight:.0f}kg + "
                            f"年龄 {age}，{'男' if gender == 1 else '女'})")
        d["bmr_formula"] = (f"10×{latest_weight:.0f} + 6.25×{height_cm:.0f} - "
                            f"5×{age} {'+ 5' if gender == 1 else '- 161'}")
    else:
        d["bmr_mifflin"] = None
        d["bmr_source"] = "无可用 profile 数据"
        d["bmr_formula"] = None

    # 总卡路里 = 活动消耗 + Mifflin BMR
    d["cal_total"] = (cal_active + d["bmr_mifflin"]) if (cal_active is not None and d["bmr_mifflin"] is not None) else None

    # ===== 12. 体重快照（最新 weight + bmi） =====
    weight_row = cur.execute("""
        SELECT value, ts_ms FROM measurements
        WHERE stream='weight' AND metric='weight'
        ORDER BY ts_ms DESC LIMIT 1
    """).fetchone()
    bmi_row = cur.execute("""
        SELECT value, ts_ms FROM measurements
        WHERE stream='weight' AND metric='bmi'
        ORDER BY ts_ms DESC LIMIT 1
    """).fetchone()
    d["weight_snapshot"] = dict(weight_row) if weight_row else None
    d["bmi_snapshot"] = dict(bmi_row) if bmi_row else None

    conn.close()
    return d


def build_report(d: dict, date_str: str) -> str:
    """根据数据字典生成完整 markdown 日报"""
    daily = d["daily"]
    sleep = d["sleep"]

    # ---------- 数据完整性 ----------
    present, missing = [], []
    if daily.get("steps"): present.append("步数")
    else: missing.append("步数")
    if daily.get("calories"): present.append("卡路里")
    else: missing.append("卡路里")
    if daily.get("device_resting_hr"): present.append("静息心率")
    else: missing.append("静息心率")
    if d["hr_n"] > 0: present.append("实时心率")
    else: missing.append("实时心率")
    if d["stress_n_clean"] > 0: present.append("压力")
    else: missing.append("压力")
    if d["hrv_n"] > 0: present.append("HRV")
    else: missing.append("HRV")
    if d["spo2_n"] > 0: present.append("血氧")
    else: missing.append("血氧")
    if sleep: present.append("睡眠")
    else: missing.append("睡眠")
    if daily.get("weight"): present.append("体重")
    if daily.get("body_fat_rate"): present.append("体脂率")
    if d["workouts"]: present.append(f"运动 ×{len(d['workouts'])}")

    completeness = f"✅ 已采集：{' · '.join(present)}"
    if missing:
        completeness += f"\n⚠️ 缺失：{' · '.join(missing)}"
        if any(x in missing for x in ("HRV", "血氧", "睡眠")):
            completeness += "\n  💡 缺失通常意味着昨晚未戴手表（HRV / 血氧 / 睡眠 均需睡眠佩戴）"

    # ---------- 1. 标题 ----------
    out = []
    out.append(f"# 🌅 健康日报 · {date_str}")
    out.append("")
    out.append(f"> 数据日期：**{date_str}** （今天 {datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=1):%Y-%m-%d} 的 t-1）")
    out.append(f"> 上次同步：{d['last_pull_at'] or '未知'}")
    out.append("")
    out.append("## 📡 数据完整性")
    out.append("")
    out.append(completeness)
    out.append("")

    # ---------- 2. 活动 ----------
    out.append("## 🚶 活动")
    out.append("")
    steps = daily.get("steps", {}).get("value")
    cal = daily.get("calories", {}).get("value")
    active = daily.get("active_minutes", {}).get("value")
    avg7s = d["avg7_steps"]
    avg7c = d["avg7_cal"]
    avg7a = d["avg7_active"]

    out.append("| 指标 | 今日 | 7 天均值 | 差值 | 解读 |")
    out.append("|---|---|---|---|---|")
    if steps is not None and avg7s:
        d_pct = round((steps - avg7s) / avg7s * 100, 1)
        interp = "📈 高于均值" if d_pct > 5 else ("📉 低于均值" if d_pct < -5 else "➖ 持平")
        out.append(f"| 步数 | **{steps:,.0f}** 步 | {avg7s:,.0f} 步 | {fmt_signed(d_pct, '%')} | {interp} |")
    elif steps is not None:
        out.append(f"| 步数 | **{steps:,.0f}** 步 | — | — | 首次记录 |")
    else:
        out.append("| 步数 | — | — | — | ⚠️ 无数据（未戴表？） |")

    if cal is not None and avg7c:
        d_pct = round((cal - avg7c) / avg7c * 100, 1)
        out.append(f"| 卡路里（活动） | **{cal:,.0f}** kcal | {avg7c:,.0f} kcal | {fmt_signed(d_pct, '%')} | — |")
    elif cal is not None:
        out.append(f"| 卡路里（活动） | **{cal:,.0f}** kcal | — | — | — |")
    else:
        out.append("| 卡路里 | — | — | — | ⚠️ 无数据 |")

    if active is not None and avg7a:
        d_pct = round((active - avg7a) / avg7a * 100, 1)
        out.append(f"| 活动分钟 | **{active:,.0f}** min | {avg7a:,.0f} min | {fmt_signed(d_pct, '%')} | — |")
    elif active is not None:
        out.append(f"| 活动分钟 | **{active:,.0f}** min | — | — | — |")
    else:
        out.append("| 活动分钟 | — | — | — | ⚠️ 无数据 |")
    out.append("")

    # ---- 新增：总卡路里（活动 + Mifflin BMR），放在活动表之后 ----
    cal_active = d.get("cal_active")
    bmr_mifflin = d.get("bmr_mifflin")
    cal_total = d.get("cal_total")
    out.append("**🔥 卡路里总消耗（活动 + 静息）**")
    out.append("")
    if cal_active is not None and bmr_mifflin is not None:
        out.append(f"- 活动消耗：**{cal_active:,.0f} kcal**")
        out.append(f"- 基础代谢（Mifflin-St Jeor）：**{bmr_mifflin:,.0f} kcal**")
        out.append(f"  - 公式：`{d.get('bmr_formula', '?')}`")
        out.append(f"  - 数据来源：身高 {d['profile_height_cm']:.0f}cm (user_profile) + "
                    f"体重 {d['latest_weight_kg']:.0f} kg (最新测量) + 年龄 {d['profile_age']} "
                    f"({'男' if d['profile_gender'] == 1 else '女'})")
        out.append(f"- **总消耗：{cal_active:,.0f} + {bmr_mifflin:,.0f} = {cal_total:,.0f} kcal** 🎯")
    elif cal_active is not None:
        out.append(f"- 活动消耗：**{cal_active:,.0f} kcal**")
        out.append("- 基础代谢：⚠️ 无 profile 数据（user_profile.height/birthday 缺失）")
        out.append(f"- **总消耗：{cal_active:,.0f} kcal** 🎯")
        out.append("")
        out.append("> 💡 去 Zepp App 设置个人资料（身高 / 生日）后 BMR 才能算")
    else:
        out.append("- ⚠️ 无活动消耗数据")
    out.append("")

    # ---------- 3. 运动 ----------
    out.append("## 🏃 运动")
    out.append("")
    if d["workouts"]:
        total_cal = sum(w["calorie"] or 0 for w in d["workouts"])
        total_dis = sum(w["dis_m"] or 0 for w in d["workouts"])
        total_dur = sum(w["run_s"] or 0 for w in d["workouts"])
        out.append(f"**{len(d['workouts'])} 次训练** · 总距离 {total_dis/1000:.2f} km · 总时长 {fmt_hms(total_dur)} · 总消耗 {total_cal:.0f} kcal")
        out.append("")
        out.append("| 时间 | 类型 | 时长 | 距离 | 卡路里 | 平均心率 |")
        out.append("|---|---|---|---|---|---|")
        for w in d["workouts"]:
            t = w["end_iso"][-8:-3] if w["end_iso"] else "—"
            out.append(f"| {t} | {w['sport_zh']} | {fmt_hms(w['run_s'])} | {w['dis_m']/1000:.2f} km | {w['calorie']:.0f} kcal | {w['avg_heart_rate']:.0f} bpm |")
        out.append("")
    else:
        out.append("今日无运动记录 🛌")
        out.append("")

    # ---------- 4. 心率 ----------
    out.append("## ❤️ 心率")
    out.append("")
    if d["hr_n"]:
        out.append(f"**日均 {d['hr_avg']} bpm** · 最低 {d['hr_min']} · 最高 {d['hr_max']} · 基于 {d['hr_n']} 个采样")
        rhr = daily.get("device_resting_hr", {}).get("value")
        if rhr:
            avg7r = d["avg7_rhr"]
            d_rhr = round(rhr - avg7r, 1) if avg7r else None
            out.append(f"**静息心率 (RHR)：{rhr} bpm**" + (f" · 7d 均值 {avg7r:.0f} · {fmt_signed(d_rhr)} bpm" if d_rhr is not None else ""))
        out.append("")
        out.append("**心率区间（Zepp 算法）**")
        out.append("")
        out.append("| 区间 | 阈值 | 采样 | 占比 | 时长 |")
        out.append("|---|---|---|---|---|")
        for z in d["hr_zones"]:
            bar = pct_bar(z["pct"], 10)
            out.append(f"| {z['name']} | {z['lo']}-{z['hi']} bpm | {z['count']} | {bar} {z['pct']}% | {z['minutes']:.0f} min |")
        out.append("")
    else:
        out.append("今日无实时心率采样 ⚠️")
        out.append("")

    # ---------- 5. 压力 ----------
    out.append("## 🧠 压力")
    out.append("")
    if d["stress_n_clean"]:
        out.append(f"**日均 {d['stress_avg']}** · 最低 {d['stress_min']} · 最高 {d['stress_max']} · 基于 {d['stress_n_clean']} 个有效采样")
        if d["stress_avg_prev7d"]:
            d_stress = round(d["stress_avg"] - d["stress_avg_prev7d"], 1)
            out.append(f"vs 上周同日（{d['stress_avg_prev7d']}）: {fmt_signed(d_stress)}")
        out.append("")
        out.append("**压力区间分布**")
        out.append("")
        out.append("| 区间 | 阈值 | 占比 |")
        out.append("|---|---|---|")
        out.append(f"| 🟢 放松 | < 40 | {pct_bar(d['stress_relax'])} {d['stress_relax']}% |")
        out.append(f"| 🟢 正常 | 40-59 | {pct_bar(d['stress_normal'])} {d['stress_normal']}% |")
        out.append(f"| 🟡 中等 | 60-79 | {pct_bar(d['stress_medium'])} {d['stress_medium']}% |")
        out.append(f"| 🔴 偏高 | ≥ 80 | {pct_bar(d['stress_high'])} {d['stress_high']}% |")
        out.append("")
    else:
        out.append("今日无压力数据 ⚠️")
        out.append("")

    # ---------- 6. HRV ----------
    out.append("## 💓 HRV（RMSSD）")
    out.append("")
    if d["hrv_n"] > 0:
        delta_pct = None
        if d["hrv_prev7d_avg"]:
            delta_pct = round((d["hrv_avg"] - d["hrv_prev7d_avg"]) / d["hrv_prev7d_avg"] * 100, 1)
        line = f"**日均 {d['hrv_avg']} ms** · 最低 {d['hrv_min']} · 最高 {d['hrv_max']} · {d['hrv_n']} 个采样"
        if delta_pct is not None:
            interp = "📈 恢复力上升" if delta_pct > 5 else ("📉 恢复力下降" if delta_pct < -5 else "➖ 持平")
            line += f"\nvs 上周同日（{d['hrv_prev7d_avg']} ms）: {fmt_signed(delta_pct, '%')} {interp}"
        out.append(line)
        out.append("")
    else:
        out.append("⚠️ **昨晚没戴表 → 无 HRV 数据**")
        out.append("")
        out.append("> HRV 需要睡眠佩戴才采集。明早起床前戴稳手表，可以补测一次白天快照 HRV。")
        out.append("")

    # ---------- 7. 血氧 ----------
    out.append("## 🫁 血氧 (SpO2)")
    out.append("")
    if d["spo2_n"]:
        warn = " ⚠️ **均值偏低，建议就医**" if d["spo2_avg"] < 95 else ""
        out.append(f"**日均 {d['spo2_avg']}%** · 最低 {d['spo2_min']}% · 最高 {d['spo2_max']}% · {d['spo2_n']} 个采样{warn}")
        if d["spo2_below95_pct"] > 10:
            out.append(f"其中 {d['spo2_below95_pct']}% 的样本 < 95%（建议关注）")
        out.append("")
    else:
        out.append("⚠️ **昨晚没戴表 → 无血氧数据**")
        out.append("")
        out.append("> SpO2 需要睡眠佩戴（Zepp 会在夜间自动测几次）。白天临时测可在 Zepp App 手动触发。")
        out.append("")

    # ---------- 8. 体温 ----------
    out.append("## 🌡️ 体温")
    out.append("")
    out.append("⚠️ **未测温 → 无数据**")
    out.append("")
    out.append("> Zepp 手表目前不支持体温自动测量。如需监测，可外接体温计手测后录入 Zepp App。")
    out.append("")

    # ---------- 9. 睡眠 ----------
    out.append("## 😴 睡眠")
    out.append("")
    if sleep:
        is_nap = "（午睡）" if sleep["is_nap"] else ""
        out.append(f"**{fmt_hms(sleep['time_in_bed_secs'])}** （{sleep['start']} → {sleep['end']}）{is_nap}")
        out.append(f"分数：{sleep['score']} · RHR：{sleep['rhr']} bpm")
        out.append("")
        out.append("| 阶段 | 时长 | 占比 |")
        out.append("|---|---|---|")
        total = sleep["time_in_bed_secs"] or 1
        out.append(f"| 深睡 | {fmt_hms(sleep['deep_secs'])} | {pct_bar(100*sleep['deep_secs']/total)} {100*sleep['deep_secs']/total:.0f}% |")
        out.append(f"| 浅睡 | {fmt_hms(sleep['light_secs'])} | {pct_bar(100*sleep['light_secs']/total)} {100*sleep['light_secs']/total:.0f}% |")
        out.append(f"| REM | {fmt_hms(sleep['rem_secs'])} | {pct_bar(100*sleep['rem_secs']/total)} {100*sleep['rem_secs']/total:.0f}% |")
        if sleep["awake_secs"]:
            out.append(f"| 清醒 | {fmt_hms(sleep['awake_secs'])} | {100*sleep['awake_secs']/total:.0f}% |")
        out.append("")
    else:
        out.append("⚠️ **未戴表睡 → 无睡眠数据**")
        out.append("")

    # ---------- 10. 体重 ----------
    out.append("## ⚖️ 体重 / 体成分")
    out.append("")
    weight = daily.get("weight", {}).get("value")
    bmi = daily.get("bmi", {}).get("value")
    fat = daily.get("body_fat_rate", {}).get("value")
    muscle = daily.get("muscle_mass", {}).get("value")
    if weight or bmi or fat or muscle:
        if weight: out.append(f"- 体重：**{weight:.1f} kg**")
        if bmi: out.append(f"- BMI：{bmi:.1f}")
        if fat: out.append(f"- 体脂率：{fat:.1f}%")
        if muscle: out.append(f"- 肌肉量：{muscle:.1f} kg")
        out.append("")
    else:
        out.append("今日无体重数据（Zepp App 未测）")
        out.append("")

    # ---------- 11. 异常检测 ----------
    alerts = []
    if d["hrv_n"] > 0 and d["hrv_avg"] < 30:
        alerts.append(f"🔴 HRV 均值仅 {d['hrv_avg']} ms，**< 30 ms 偏低**，自主神经恢复力较弱，注意休息")
    if d["spo2_n"] and d["spo2_avg"] < 95:
        alerts.append(f"🔴 SpO2 均值 {d['spo2_avg']}% < 95%，**血氧偏低，建议就医**")
    if d["stress_high"] and d["stress_high"] > 10:
        alerts.append(f"🟠 压力偏高（≥80）时段占 {d['stress_high']}%，**高压时段偏多**")
    rhr = daily.get("device_resting_hr", {}).get("value")
    if rhr and rhr > 80:
        alerts.append(f"🟠 静息心率 {rhr} bpm > 80，**RHR 偏高**，可能疲劳/睡眠不足")
    if steps is not None and steps < 3000:
        alerts.append(f"🟡 步数 {steps:,.0f} < 3000，**活动量偏低**")
    # 连续 3 天 0 步
    days = d["history_days"]
    if len(days) >= 3:
        last3 = days[-3:]
        if all((x["steps"] or 0) == 0 for x in last3):
            alerts.append("🔴 **连续 3 天 0 步** — 检查手表佩戴 / 同步状态")
    if sleep:
        if sleep["time_in_bed_secs"] < 6 * 3600:
            alerts.append(f"🟡 睡眠 {fmt_hms(sleep['time_in_bed_secs'])} **< 6 小时，睡眠不足**")
        if sleep["score"] and sleep["score"] < 60:
            alerts.append(f"🟡 睡眠分数 {sleep['score']} **< 60**，质量偏低")

    out.append("## ⚠️ 异常检测")
    out.append("")
    if alerts:
        for a in alerts:
            out.append(f"- {a}")
        out.append("")
    else:
        out.append("✅ 所有指标在正常范围")
        out.append("")

    # ---------- 12. 建议 ----------
    suggestions = []
    if d["hrv_n"] == 0 or (d["hrv_avg"] and d["hrv_avg"] < 30):
        suggestions.append("🌙 **今晚戴表睡觉** —— 优先采集 HRV（恢复力核心指标）")
    if d["spo2_n"] == 0:
        suggestions.append("🌙 **今晚戴表睡觉** —— 顺手补采 SpO2 夜间曲线")
    if steps is not None and steps < 3000:
        suggestions.append("🚶 **明天中午走 10 分钟** —— 补足日均活动量")
    if d["stress_high"] and d["stress_high"] > 10:
        suggestions.append("🧘 **今晚 22:00 前睡** —— 高压时段偏多，深度睡眠是最好的恢复")
    if sleep and sleep["score"] and sleep["score"] < 70:
        suggestions.append("🛌 **延长睡眠窗口** —— 睡眠分数 67，建议多睡 30 分钟")
    if not suggestions:
        suggestions.append("✅ 各项平稳，**保持当前节奏**即可")

    out.append("## 💡 今日建议")
    out.append("")
    for s in suggestions[:3]:
        out.append(f"- {s}")
    out.append("")

    # ---------- 13. 体重快照（独立小节） ----------
    out.append("## 🏋️ 体重快照")
    out.append("")
    w_row = d.get("weight_snapshot")
    b_row = d.get("bmi_snapshot")
    if w_row or b_row:
        out.append("| 指标 | 值 | 数据生成 |")
        out.append("|---|---|---|")
        if w_row:
            w_label = days_ago_label(w_row["ts_ms"])
            out.append(f"| 体重 | **{w_row['value']:.1f} kg** | {w_label} |")
        else:
            out.append("| 体重 | 今日无体重数据 | — |")
        if b_row:
            b_label = days_ago_label(b_row["ts_ms"])
            cat = bmi_category(b_row["value"])
            cat_str = f"（{cat}）" if cat else ""
            out.append(f"| BMI | {b_row['value']:.1f}{cat_str} | {b_label} |")
        else:
            out.append("| BMI | 今日无 BMI 数据 | — |")
        out.append("")
        # 友好提示
        if w_row:
            w_local = ms_to_local_dt(w_row["ts_ms"])
            w_date = w_local.strftime("%Y-%m-%d") if w_local else "—"
        else:
            w_date = None
        if b_row:
            b_local = ms_to_local_dt(b_row["ts_ms"])
            b_date = b_local.strftime("%Y-%m-%d") if b_local else "—"
        else:
            b_date = None
        ref_date = w_date or b_date
        if ref_date:
            out.append(f"> 💡 体重不是每日测。BMI 基于 {ref_date} 那次测量。")
            out.append("> 如果需要更新，去 Zepp App 手动测一次。")
        out.append("")
    else:
        out.append("今日无体重数据（Zepp App 未测）")
        out.append("")

    # ---------- 14. 数据延迟说明 ----------
    out.append("---")
    out.append("")
    out.append(f"📦 **数据来源**：Zepp Cloud → `/root/.zepp-data/zepp.db` · 最近同步 {d['last_pull_at'] or '未知'}")
    out.append(f"⏰ **数据延迟**：今天（{datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=1):%Y-%m-%d}）的实时数据需 Zepp App 后台保活才能即时上传；后台未运行时延迟 8-12 小时")
    out.append(f"🛠️ **查询方式**：`python3 scripts/query_zepp.py daily-stress --date {date_str}` / `daily-hr --date {date_str}`")
    out.append("")

    return "\n".join(out)


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=TARGET_DATE, help="目标日期 YYYY-MM-DD（默认 t-1 = 2026-09-25）")
    p.add_argument("--out", default=None, help="输出文件路径")
    args = p.parse_args()

    report = build_report(load_data(args.date), args.date)
    print(report)
    out_path = Path(args.out or f"/tmp/M8_daily_report_{args.date}.md")
    out_path.write_text(report, encoding="utf-8")
    print(f"\n\n[也写到了 {out_path}]", file=sys.stderr)


if __name__ == "__main__":
    main()