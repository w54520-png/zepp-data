#!/usr/bin/env python3
"""
根据 user_profile 用 Harris-Benedict 公式算 BMR，把 daily 卡路里加 BMR 写回 measurements。

为什么 Zepp 原始数据没 BMR：
- Zepp Cloud daily_summary 流只给"活动卡路里" totalCalories
- 没有 BMR/resting 字段
- 解决方案：本地算 BMR → 总 = 活动 + BMR

Harris-Benedict 公式（1984 修订版）：
  男 BMR = 88.362 + 13.397×W + 4.799×H - 5.677×A
  女 BMR = 447.593 + 9.247×W + 3.098×H - 4.330×A

W=kg, H=cm, A=年龄（完整周岁）

gender 字段：Zepp 反人类约定（1=男, 0=女）—— 跟常规相反。
"""
from __future__ import annotations
import argparse
import sqlite3
from datetime import date, datetime
from pathlib import Path


def bmr_male(weight_kg: float, height_cm: float, age: int) -> float:
    return 88.362 + 13.397 * weight_kg + 4.799 * height_cm - 5.677 * age


def bmr_female(weight_kg: float, height_cm: float, age: int) -> float:
    return 447.593 + 9.247 * weight_kg + 3.098 * height_cm - 4.330 * age


def calc_age(birthday_yyyymm: str, today: date | None = None) -> int:
    """'1990-01' → 35 (示例：当前 2026 年)."""
    today = today or date.today()
    yr, mo = birthday_yyyymm.split("-")
    bd = date(int(yr), int(mo), 1)
    age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
    return age


def bmr_for_profile(gender: int, weight_kg: float, height_cm: float, age: int) -> float:
    """Zepp gender: 1=男, 0=女 (反常规)."""
    if gender == 1:
        return bmr_male(weight_kg, height_cm, age)
    elif gender == 0:
        return bmr_female(weight_kg, height_cm, age)
    else:
        raise ValueError(f"unknown gender: {gender}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="/root/.zepp-data/zepp.db")
    p.add_argument("--dry-run", action="store_true")
    # M3 章节 3.5：加 zepp_med 选项（最近 N 次 BMR 中位数）解决 Zepp 体重秤 BMR 漂移
    p.add_argument("--bmr-source", choices=["formula", "zepp_app", "zepp_api", "zepp_med"],
                   default="zepp_app",
                   help="formula=Harris-Benedict 算；zepp_app=用 Zepp App 实测值（默认，最准）；"
                        "zepp_api=用 Zepp Cloud weight 流最新 metabolism 字段（可能漂移）；"
                        "zepp_med=取最近 N 次 BMR 中位数（抗漂移，M3 推荐）")
    p.add_argument("--med-n", type=int, default=5,
                   help="zepp_med 选项的窗口大小（最近 N 次 BMR，默认 5）")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    today = date.today()

    # Zepp App 实测值（2026-09-22 从用户 Zepp App 截屏读取）：
    # - 4876 步 / 活动 142 kcal / 静息 1436 kcal / 总 1578 kcal
    # - BMR 实测 1436 ≈ 59.8 kcal/h × 24h
    # - 公式算（Harris-Benedict 男 W=69/H=172/A=37 = 1628）偏高 192 kcal
    # 结论：Zepp 用了 Mifflin-St Jeor 或更精细的算法，且"静息"不是 BMR 而是 TDEE - 活动
    # 后续 Zepp API 给出 static_bmr 字段再切换到 formula 模式
    ZEPP_APP_BMR = {
        "example_user": 1436,  # 主账号示例，男 35y 175cm 70kg（替换为你自己的 Zepp App 实测值）
        "小宝":  None,   # 家人数据，暂未读 App
    }

    # 1. 加载每个成员的 BMR
    bmr_by_nick = {}
    for row in conn.execute("SELECT user_id, nickname, birthday, gender, height, weight FROM user_profile"):
        user_id, nick, birthday, gender, height, weight = row
        if not birthday or not height or not weight:
            continue
        try:
            age = calc_age(birthday, today)
        except Exception as e:
            print(f"⚠️ {nick}: birthday parse fail: {e}")
            continue
        try:
            bmr = bmr_for_profile(gender, weight, height, age)
        except Exception as e:
            print(f"⚠️ {nick}: BMR calc fail: {e}")
            continue
        bmr_by_nick[nick] = {"bmr": bmr, "age": age, "gender": gender,
                              "height": height, "weight": weight, "user_id": user_id,
                              "source": "formula"}
        gender_str = "男" if gender == 1 else "女"
        print(f"  {nick}: {gender_str} {age}y {height}cm {weight}kg → 公式 BMR = {bmr:.0f} kcal/day")

    # 2. 叠加 Zepp App 实测值（如果用户指定 zepp_app 模式）
    if args.bmr_source == "zepp_app":
        for nick, app_bmr in ZEPP_APP_BMR.items():
            if nick in bmr_by_nick and app_bmr is not None:
                bmr_by_nick[nick]["bmr"] = app_bmr
                bmr_by_nick[nick]["source"] = f"zepp_app@2026-09-22"
                print(f"  ⚠️ 覆盖 {nick}: 用 Zepp App 实测 BMR = {app_bmr} kcal/day")
            elif nick in bmr_by_nick:
                print(f"  ⚠️ {nick} 无 Zepp App 实测值，沿用公式 BMR")
    elif args.bmr_source == "zepp_api":
        # M2 章节 2.2：从 measurements 读 metric='bmr'（stream='weight'）最新值
        # 注意：用户报告 Zepp 体脂秤 metabolism 漂移（如 1436 vs 1553），所以**不**默认
        for nick, info in bmr_by_nick.items():
            row = conn.execute("""
                SELECT value FROM measurements
                WHERE user_id = ? AND stream = 'weight' AND metric = 'bmr'
                ORDER BY ts_ms DESC LIMIT 1
            """, (info["user_id"],)).fetchone()
            if row:
                info["bmr"] = float(row[0])
                info["source"] = "zepp_api"
                print(f"  ⚠️ 覆盖 {nick}: 用 Zepp API weight 流 BMR = {info['bmr']:.0f} kcal/day")
            else:
                # 友好提示：该 nick 在 weight 流里没有任何 BMR 测量记录
                print(f"  ⚠️ {nick} 无 Zepp API BMR 数据（weight 流未 sync），沿用公式")
                print(f"     💡 提示：跑 `python3 scripts/pull_to_sqlite.py sync --days 90` 拉体重流")
                print(f"        或换 `--bmr-source formula` 用 Harris-Benedict 公式")
                print(f"        或换 `--bmr-source zepp_app` 用 Zepp App 截图静态值")
    elif args.bmr_source == "zepp_med":
        # M3 章节 3.5：取最近 N 次 BMR 的中位数（抗漂移）
        for nick, info in bmr_by_nick.items():
            rows = conn.execute("""
                SELECT value FROM measurements
                WHERE user_id = ? AND stream = 'weight' AND metric = 'bmr'
                  AND value IS NOT NULL
                ORDER BY ts_ms DESC LIMIT ?
            """, (info["user_id"], args.med_n)).fetchall()
            if not rows:
                # 友好提示：该 nick 在 weight 流里没有任何 BMR 测量记录
                print(f"  ⚠️ {nick} 无 Zepp API BMR 数据（weight 流未 sync），沿用公式")
                print(f"     💡 提示：跑 `python3 scripts/pull_to_sqlite.py sync --days 90` 拉体重流")
                print(f"        或换 `--bmr-source formula` 用 Harris-Benedict 公式")
                print(f"        或换 `--bmr-source zepp_app` 用 Zepp App 截图静态值")
                continue
            vals = [float(r[0]) for r in rows]
            vals_sorted = sorted(vals)
            n = len(vals_sorted)
            median = (vals_sorted[n // 2] if n % 2 == 1
                      else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2)
            info["bmr"] = median
            info["source"] = f"zepp_med@{args.med_n}"
            print(f"  ⚠️ 覆盖 {nick}: 最近 {n} 次 BMR {vals} → 中位数 = {median:.0f} kcal/day")

    # 2.5 BMR 数据完整性检查（友好跳过 vs 静默回退）
    # 如果用户显式指定 zepp_api / zepp_med 但 DB 里全无 weight 流 BMR 数据，
    # 主账号会静默回退到公式算，这是反直觉的：用户期待看到"无数据"的明确反馈。
    # 解决：检测主账号 BMR source，如果还是 formula（说明覆盖失败），且用户显式要 zepp_api/zepp_med，
    # 给出友好提示，让用户决定是否继续（不抛错，避免破坏 cron）。
    if args.bmr_source in ("zepp_api", "zepp_med"):
        main_nick_check = "example_user"
        main_info = bmr_by_nick.get(main_nick_check)
        if main_info and main_info["source"] == "formula":
            # 主账号 BMR 数据为空（覆盖失败）
            bmr_count = conn.execute(
                "SELECT COUNT(*) FROM measurements WHERE stream='weight' AND metric='bmr'"
            ).fetchone()[0]
            if bmr_count == 0:
                print(f"\n💡 体重 BMR 数据为空（整个 DB 共 0 条 metric='bmr' 记录）。")
                print(f"   你可能没拉过体重流：")
                print(f"   跑 `python3 scripts/pull_to_sqlite.py sync --days 90` 拉体重流")
                print(f"   或者换 --bmr-source formula 用 Harris-Benedict 公式算")
                print(f"   或者换 --bmr-source zepp_app 用 Zepp App 截图静态值 1436")
                print(f"   ⚠️ 当前会沿用公式 BMR（Harris-Benedict {main_info['bmr']:.0f} kcal/day），不抛错。\n")

    # 3. 主账号
    main_nick = "example_user"
    if main_nick not in bmr_by_nick:
        print(f"❌ 找不到 {main_nick}")
        return 1
    bmr = bmr_by_nick[main_nick]["bmr"]
    user_id = bmr_by_nick[main_nick]["user_id"]
    src = bmr_by_nick[main_nick]["source"]
    print(f"\n→ 给 {main_nick} (user_id={user_id}) 加 BMR={bmr:.0f} 到每天活动卡路里上 (来源: {src})")

    # 3. 写回 measurements：加一个新 metric "calories_total"
    rows_to_insert = []
    for row in conn.execute("""
        SELECT ts_ms, user_id, device_id, value
        FROM measurements
        WHERE metric = 'calories' AND value IS NOT NULL AND value > 0 AND user_id = ?
    """, (user_id,)):
        ts_ms, uid, did, active_cal = row
        total_cal = active_cal + bmr
        rows_to_insert.append((ts_ms, uid, did, total_cal))

    print(f"\n→ 共 {len(rows_to_insert)} 条 calories 需要补 BMR")

    if args.dry_run:
        print("(dry-run, 没写入)")
        for r in rows_to_insert[:5]:
            print(f"  ts_ms={r[0]}  active={r[3]-bmr:.0f}  total={r[3]:.0f} kcal")
        return 0

    # 4. 删旧的 calories_total 再插新的（measurements 没唯一约束）
    deleted = conn.execute("DELETE FROM measurements WHERE metric = 'calories_total'").rowcount
    print(f"→ 删了 {deleted} 条旧 calories_total")

    inserted = 0
    for ts_ms, uid, did, total_cal in rows_to_insert:
        # 算 date 字段（本地时区 YYYY-MM-DD）
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=8))
        date_str = datetime.fromtimestamp(ts_ms/1000, tz).strftime("%Y-%m-%d")
        try:
            conn.execute("""
                INSERT INTO measurements (ts_ms, user_id, device_id, stream, metric, date, value, unit, source_scope, ingested_at)
                VALUES (?, ?, ?, 'computed', 'calories_total', ?, ?, 'kcal', ?, datetime('now','localtime'))
            """, (ts_ms, uid, did or '', date_str, total_cal, f"computed_bmr:{src}"))
            inserted += 1
        except sqlite3.IntegrityError as e:
            print(f"  ⚠️ insert fail: {e}")

    conn.commit()
    print(f"\n✅ 写入 {inserted} 条 calories_total")
    print(f"  sample (前 3 条):")
    for row in conn.execute("""
        SELECT datetime(ts_ms/1000, 'unixepoch', 'localtime'), value
        FROM measurements WHERE metric='calories_total'
        ORDER BY ts_ms DESC LIMIT 3
    """):
        print(f"    {row[0]}  {row[1]:.0f} kcal")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())