"""
M9 章节：daily_report.py 测试套件

覆盖：
  1. test_basic_output_structure       — 13 个章节标题
  2. test_mifflin_bmr_with_full_profile — height/birthday/weight 全有 → BMR=1585
  3. test_mifflin_bmr_missing_height   — height 缺失 → fallback "⚠️ 无 profile 数据"
  4. test_mifflin_bmr_missing_birthday — birthday 缺失 → fallback
  5. test_mifflin_bmr_missing_weight   — weight 流空 → fallback
  6. test_calorie_total_breakdown      — cal_total = cal_active + bmr_mifflin
  7. test_out_file_arg                 — --out 写文件
  8. test_weight_snapshot_section      — 🏋️ 体重快照节存在 + 标 "X 天前"

实现策略：
  - 直接调 generate_daily_report_sample 的 load_data + build_report（同一逻辑）
  - 用临时 SQLite DB（不污染真实 /root/.zepp-data/zepp.db）
  - patch generate_daily_report_sample.DB 指向临时 DB
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# 把 scripts + tests 加到 sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))

import generate_daily_report_sample as gen  # noqa: E402

REAL_DB = "/root/.zepp-data/zepp.db"
TARGET_DATE = "2026-09-25"

# Zepp 反人类约定：1=男, 0=女（跟主流相反）
USER_PROFILE_ROW = {
    "member_id": "-1",
    "user_id": "1000000000",  # 示例 user_id（发布版请替换为你自己的）
    "nickname": "example_user",  # 占位符 — 替换为你自己的 Zepp 昵称
    "birthday": "1990-01",  # 占位符
    "gender": 1,         # 男（Zepp 反人类约定：1=男, 0=女）
    "height": 175.0,     # 占位符
    "weight": 70.0,      # 占位符
}

# 完整 measurements schema（最小集，足够日报所有 section 不崩）
MEASUREMENTS_SCHEMA = """
CREATE TABLE measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    stream TEXT NOT NULL,
    metric TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    date TEXT,
    value REAL,
    unit TEXT,
    source_scope TEXT,
    device_id TEXT,
    extra_json TEXT,
    raw_source_key TEXT,
    ingested_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, stream, metric, ts_ms, device_id)
);
CREATE TABLE user_profile (
    member_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    nickname TEXT,
    birthday TEXT,
    gender INTEGER,
    height REAL,
    weight REAL,
    raw_source_key TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE heart_rate_band_samples (
    date TEXT NOT NULL,
    ts INTEGER,
    bpm INTEGER
);
CREATE TABLE sleep_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    date TEXT NOT NULL,
    source TEXT,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    tz_offset_secs INTEGER,
    time_in_bed_secs INTEGER,
    deep_secs INTEGER DEFAULT 0,
    light_secs INTEGER DEFAULT 0,
    rem_secs INTEGER DEFAULT 0,
    awake_secs INTEGER DEFAULT 0,
    unknown_secs INTEGER DEFAULT 0,
    sp_o2_avg REAL,
    breath_avg REAL,
    rhr INTEGER,
    score INTEGER,
    is_nap INTEGER DEFAULT 0,
    algo_version TEXT,
    sleep_source INTEGER,
    raw_summary_json TEXT,
    raw_source_key TEXT,
    ingested_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE workouts (
    trackid TEXT PRIMARY KEY,
    user_id TEXT,
    sport_zh TEXT,
    sport_type INTEGER,
    run_s INTEGER,
    dis_m REAL,
    calorie REAL,
    avg_heart_rate REAL,
    max_heart_rate REAL,
    min_heart_rate REAL,
    end_time_ts INTEGER,
    end_time_iso TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _ts_for_date(date_str: str, hour: int = 8, minute: int = 0) -> int:
    """'YYYY-MM-DD' + 08:00 北京 = 当天 00:00 UTC → ms."""
    dt = datetime.strptime(f"{date_str} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M")
    dt_utc = dt - timedelta(hours=8)
    return int(dt_utc.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _ts_for_local_dt(dt_local: datetime) -> int:
    """local datetime → ms (假设 UTC+8)."""
    dt_utc = dt_local - timedelta(hours=8)
    return int(dt_utc.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _make_db(
    *,
    date_str: str = TARGET_DATE,
    profile: dict | None = USER_PROFILE_ROW,
    with_calories: bool = True,
    with_weight_stream: bool = True,
    weight_value: float = 70.0,
    weight_ts: datetime | None = None,
    steps: int = 3226,
    calories: float = 133.0,
) -> str:
    """构造临时 SQLite DB（含足够 schema）。返回路径。"""
    tmp = tempfile.NamedTemporaryFile(prefix="zepp_daily_report_", suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.row_factory = sqlite3.Row
    conn.executescript(MEASUREMENTS_SCHEMA)

    # user_profile
    if profile:
        conn.execute("""
            INSERT INTO user_profile (member_id, user_id, nickname, birthday, gender, height, weight)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (profile["member_id"], profile["user_id"], profile["nickname"],
              profile["birthday"], profile["gender"], profile["height"], profile["weight"]))

    # daily measurements（活动 / 卡路里 / 静息 HR / max HR）
    ts = _ts_for_date(date_str)
    rows = [
        ("steps", "steps", steps, "count"),
        ("active_minutes", "active_minutes", 3, "min"),
        ("device_resting_hr", "device_resting_hr", 55, "bpm"),
        ("device_max_hr", "device_max_hr", 187, "bpm"),
        ("step_goal", "step_goal", 30000, "count"),
        ("calorie_goal", "calorie_goal", 300, "kcal"),
    ]
    if with_calories:
        rows.append(("calories", "calories", calories, "kcal"))
    for stream, metric, val, unit in rows:
        conn.execute("""
            INSERT INTO measurements (user_id, stream, metric, ts_ms, date, value, unit)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (profile["user_id"] if profile else "u1", stream, metric, ts, date_str, val, unit))

    # weight 流（如开启）—— latest weight + 同步对应 bmi
    if with_weight_stream:
        if weight_ts is None:
            weight_ts = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=10, minute=26)
        w_ts_ms = _ts_for_local_dt(weight_ts)
        conn.execute("""
            INSERT INTO measurements (user_id, stream, metric, ts_ms, date, value, unit)
            VALUES (?, 'weight', 'weight', ?, ?, ?, 'kg')
        """, (profile["user_id"] if profile else "u1", w_ts_ms, date_str, weight_value))
        conn.execute("""
            INSERT INTO measurements (user_id, stream, metric, ts_ms, date, value, unit)
            VALUES (?, 'weight', 'bmi', ?, ?, 22.9, '')
        """, (profile["user_id"] if profile else "u1", w_ts_ms, date_str))

    # meta
    conn.execute("INSERT INTO meta (key, value) VALUES ('last_pull_at', ?)",
                 (datetime.now(timezone.utc).isoformat(),))

    conn.commit()
    conn.close()
    return tmp.name


def _cleanup(path: str):
    try:
        os.unlink(path)
    except OSError:
        pass


# ============ 用例 1：基本输出结构（13 章节） ============

def test_basic_output_structure():
    """跑 2026-09-25 → 输出含 13 个章节标题。"""
    db = _make_db()
    try:
        with patch.object(gen, "DB", Path(db)):
            data = gen.load_data(TARGET_DATE)
            report = gen.build_report(data, TARGET_DATE)
        sections = [line for line in report.split("\n") if line.startswith("## ")]
        assert len(sections) == 13, f"期望 13 章节，得到 {len(sections)}: {sections}"
        # 关键章节都在
        assert "## 📡 数据完整性" in report
        assert "## 🚶 活动" in report
        assert "## 🏃 运动" in report
        assert "## 🏋️ 体重快照" in report
    finally:
        _cleanup(db)


# ============ 用例 2：完整 profile → Mifflin BMR = 1585 ============

def test_mifflin_bmr_with_full_profile():
    """height/birthday/weight 全有 → BMR = 10×70 + 6.25×175 - 5×36 + 5 ≈ 1619（占位符 profile）。"""
    db = _make_db()  # 完整 profile + weight=70
    try:
        with patch.object(gen, "DB", Path(db)):
            data = gen.load_data(TARGET_DATE)
        bmr = data.get("bmr_mifflin")
        assert bmr == 1618.8, f"期望 BMR=1618.8，得到 {bmr}"
        # 公式表达式
        assert data["bmr_formula"] == "10×70 + 6.25×175 - 5×36 + 5"
        # 报告里写明
        with patch.object(gen, "DB", Path(db)):
            report = gen.build_report(data, TARGET_DATE)
        assert "1,619 kcal" in report, f"报告缺少 1,619 kcal 节:\n{report}"
        assert "Mifflin-St Jeor" in report
        assert "总消耗：133 + 1,619 = 1,752 kcal" in report
    finally:
        _cleanup(db)


# ============ 用例 3：height 缺失 → fallback ============

def test_mifflin_bmr_missing_height():
    """user_profile.height 缺失 → fallback "⚠️ 无 profile 数据"。"""
    profile = {**USER_PROFILE_ROW}
    profile["height"] = None
    db = _make_db(profile=profile)
    try:
        with patch.object(gen, "DB", Path(db)):
            data = gen.load_data(TARGET_DATE)
            report = gen.build_report(data, TARGET_DATE)
        assert data.get("bmr_mifflin") is None
        assert "⚠️ 无 profile 数据" in report, f"报告缺 fallback 文案:\n{report}"
        # 总卡路里只算活动
        assert "总消耗：133 kcal" in report
    finally:
        _cleanup(db)


# ============ 用例 4：birthday 缺失 → fallback ============

def test_mifflin_bmr_missing_birthday():
    """user_profile.birthday 缺失 → fallback。"""
    profile = {**USER_PROFILE_ROW}
    profile["birthday"] = None
    db = _make_db(profile=profile)
    try:
        with patch.object(gen, "DB", Path(db)):
            data = gen.load_data(TARGET_DATE)
            report = gen.build_report(data, TARGET_DATE)
        assert data.get("bmr_mifflin") is None
        assert "⚠️ 无 profile 数据" in report
        assert "总消耗：133 kcal" in report
    finally:
        _cleanup(db)


# ============ 用例 5：weight 流空 → fallback ============

def test_mifflin_bmr_missing_weight():
    """measurements.weight 流无数据 → fallback。"""
    db = _make_db(with_weight_stream=False)
    try:
        with patch.object(gen, "DB", Path(db)):
            data = gen.load_data(TARGET_DATE)
            report = gen.build_report(data, TARGET_DATE)
        assert data.get("bmr_mifflin") is None
        assert "⚠️ 无 profile 数据" in report
        assert "总消耗：133 kcal" in report
    finally:
        _cleanup(db)


# ============ 用例 6：cal_total = cal_active + bmr_mifflin ============

def test_calorie_total_breakdown():
    """验证 cal_total = cal_active + bmr_mifflin。"""
    db = _make_db()  # cal_active=133, bmr_mifflin=1619
    try:
        with patch.object(gen, "DB", Path(db)):
            data = gen.load_data(TARGET_DATE)
        cal_active = data["cal_active"]
        bmr = data["bmr_mifflin"]
        cal_total = data["cal_total"]
        assert cal_active == 133.0
        assert bmr == 1618.8
        assert cal_total == 1751.8
    finally:
        _cleanup(db)


# ============ 用例 7：--out 写文件 ============

def test_out_file_arg():
    """daily_report.py --out report.md 写文件。"""
    import subprocess
    db = _make_db()
    try:
        out_path = tempfile.NamedTemporaryFile(prefix="zepp_out_", suffix=".md", delete=False).name
        os.unlink(out_path)
        try:
            # 用 PATCH 替换 gen.DB 后再调 main（避免触发真实 sync）
            # 但 main 会跑 refresh + sync —— 用 --no-sync 跳过
            # 同时需要保证导入的 load_data / build_report 走临时 DB
            # 最稳的办法：直接调 daily_report 模块，但 patch 它的 gen.DB
            from unittest.mock import patch as _patch
            import daily_report
            # daily_report 内部 load_data 是从 generate_daily_report_sample 模块名拿的
            # daily_report.gen = generate_daily_report_sample（同 module 对象）
            with _patch.object(gen, "DB", Path(db)):
                with _patch.object(sys, "argv",
                                   ["daily_report.py", "--date", TARGET_DATE,
                                    "--out", out_path, "--no-sync"]):
                    rc = daily_report.main()
            assert rc == 0
            content = Path(out_path).read_text(encoding="utf-8")
            assert "1,619 kcal" in content
            assert "1,752 kcal" in content
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
    finally:
        _cleanup(db)


# ============ 用例 8：体重快照节存在 + "X 天前" ============

def test_weight_snapshot_section():
    """🏋️ 体重快照节存在 + 标"X 天前"。"""
    # weight 数据 12 天前（容忍 12-13 天 —— 跨日边界时 days_ago_label 可能 +1）
    weight_dt = datetime.strptime(TARGET_DATE, "%Y-%m-%d") - timedelta(days=12)
    db = _make_db(weight_ts=weight_dt)
    try:
        with patch.object(gen, "DB", Path(db)):
            data = gen.load_data(TARGET_DATE)
            report = gen.build_report(data, TARGET_DATE)
        assert "## 🏋️ 体重快照" in report
        # 容忍 12 天或 13 天（跨日边界）
        assert ("12 天前" in report or "13 天前" in report), \
            f"报告缺 '12/13 天前':\n{report}"
        assert "**70.0 kg**" in report
    finally:
        _cleanup(db)