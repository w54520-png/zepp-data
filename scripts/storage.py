"""
SQLite 落库层（移植自 ZeppBridge storage/mod.rs 设计哲学）

设计：
  - 4 表：raw_records / measurements / capabilities / meta
  - 主键 (stream, ts_ms, device_id) 实现增量 upsert
  - raw_records 完整保留原始 JSON，方便日后改 normalizer 重放
  - normalizer_revision 写在 meta，改版本自动触发重放
"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional
from normalizer.common import MetricSample, DailyMetric


SCHEMA_VERSION = 2
# 改 normalizer 逻辑时要改这个，触发自动重放
NORMALIZER_REVISION = "m3-workout-detail-2026-09-23"


SCHEMA = """
-- 原始响应（完整保留）
CREATE TABLE IF NOT EXISTS raw_records (
    source_key TEXT PRIMARY KEY,
    stream TEXT NOT NULL,           -- "hrv_rmssd" / "pai" / "spo2" ...
    event_type TEXT,
    sub_type TEXT,
    surface TEXT,                   -- "v2_events" / "user_events" / "watch_stat" / "heart_rate"
    start_ts_ms INTEGER,
    end_ts_ms INTEGER,
    device_id TEXT,
    user_id TEXT,
    payload TEXT NOT NULL,          -- 完整原始 JSON
    ingested_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_raw_stream ON raw_records(stream, start_ts_ms DESC);

-- 设备清单（扁平）
CREATE TABLE IF NOT EXISTS devices (
    mac_address TEXT PRIMARY KEY,
    device_type INTEGER,
    device_source INTEGER,
    device_id TEXT,
    sn TEXT,
    binding_status INTEGER,
    application_time INTEGER,
    last_status_update_time INTEGER,
    firmware_version TEXT,
    product_id INTEGER,
    extra_json TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- 用户档案（生日/性别/身高/体重，Zepp members 流）
CREATE TABLE IF NOT EXISTS user_profile (
    member_id TEXT PRIMARY KEY,     -- '-1' 是账号持有人；其他数字是家庭成员
    user_id TEXT NOT NULL,
    nickname TEXT,
    birthday TEXT,                   -- YYYY-MM（Zepp 实际存储的格式，例 '1990-01'）
    gender INTEGER,                   -- 0=男 1=女
    height REAL,                      -- cm
    weight REAL,                      -- kg
    raw_source_key TEXT,              -- 关联 raw_records
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_profile_user ON user_profile(user_id);

-- 规范化后的标量
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    stream TEXT NOT NULL,           -- "hrv_rmssd" / "pai_daily" / "spo2" ...
    metric TEXT NOT NULL,           -- 同 stream（用于兼容）
    ts_ms INTEGER NOT NULL,
    date TEXT,                      -- 派生本地日历日（Asia/Shanghai）
    value REAL,
    unit TEXT,
    source_scope TEXT,              -- device/user_fused/unknown
    device_id TEXT,
    extra_json TEXT,                -- 流特有扩展字段
    raw_source_key TEXT,            -- 关联 raw_records
    ingested_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, stream, metric, ts_ms, device_id)
);

CREATE INDEX IF NOT EXISTS idx_meas_stream ON measurements(stream, ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_meas_metric ON measurements(metric, ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_meas_date ON measurements(date);

-- 能力探测（哪些流有数据）
CREATE TABLE IF NOT EXISTS capabilities (
    surface TEXT NOT NULL,
    event_type TEXT NOT NULL,
    sub_type TEXT,
    status TEXT NOT NULL,           -- available / no_records / unsupported / unknown
    sample_count INTEGER DEFAULT 0,
    last_observation_ts_ms INTEGER,
    last_probed_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (surface, event_type, sub_type)
);

-- 元数据
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- ===== M2 章节 2.1 新增：睡眠相关 3 张表 =====

-- 睡眠 session（一条主睡 / 一次午睡）
CREATE TABLE IF NOT EXISTS sleep_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    date TEXT NOT NULL,                -- 本地日历日（Zepp API date_time）
    source TEXT,                      -- device MAC / source 字段
    start_ts INTEGER NOT NULL,        -- unix 秒
    end_ts INTEGER NOT NULL,
    tz_offset_secs INTEGER,           -- 解析后的时区偏移
    time_in_bed_secs INTEGER,         -- 在床时长（4 级兜底）
    deep_secs INTEGER DEFAULT 0,
    light_secs INTEGER DEFAULT 0,
    rem_secs INTEGER DEFAULT 0,
    awake_secs INTEGER DEFAULT 0,
    unknown_secs INTEGER DEFAULT 0,
    sp_o2_avg REAL,
    breath_avg REAL,
    rhr INTEGER,
    score INTEGER,
    is_nap INTEGER DEFAULT 0,         -- 1=午睡（odd_stage 提取）
    algo_version TEXT,
    sleep_source INTEGER,
    raw_summary_json TEXT,            -- 完整 decoded summary JSON（debug）
    raw_source_key TEXT,              -- 关联 raw_records
    ingested_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, source, date, start_ts, is_nap)
);
CREATE INDEX IF NOT EXISTS idx_sleep_sessions_date ON sleep_sessions(date DESC);
CREATE INDEX IF NOT EXISTS idx_sleep_sessions_start ON sleep_sessions(start_ts DESC);

-- 睡眠阶段切片
CREATE TABLE IF NOT EXISTS sleep_stage_slices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,               -- FK → sleep_sessions.id（NULL 表示 session 还没插入）
    date TEXT NOT NULL,
    start_ts INTEGER NOT NULL,        -- unix 秒
    end_ts INTEGER NOT NULL,
    start_min INTEGER NOT NULL,       -- 相对 slp.st 锚点的分钟数
    end_min INTEGER NOT NULL,
    mode_code INTEGER NOT NULL,       -- Zepp 原始数字 mode
    mode TEXT NOT NULL,               -- "deep"/"light"/"rem"/"awake"/"unknown"
    tz_offset_secs INTEGER,
    raw_source_key TEXT,
    ingested_at TEXT DEFAULT (datetime('now')),
    UNIQUE (date, start_ts, end_ts, mode_code, tz_offset_secs)
);
CREATE INDEX IF NOT EXISTS idx_stage_session ON sleep_stage_slices(session_id);
CREATE INDEX IF NOT EXISTS idx_stage_date ON sleep_stage_slices(date DESC);

-- band_data 逐分钟心率（detail 端点才有）
CREATE TABLE IF NOT EXISTS heart_rate_band_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    ts INTEGER NOT NULL,              -- unix 秒（本地时间）
    bpm INTEGER,                      -- 20..240（255 哨兵 → 不入库）
    tz_offset_secs INTEGER,
    raw_source_key TEXT,
    ingested_at TEXT DEFAULT (datetime('now')),
    UNIQUE (date, ts)
);
CREATE INDEX IF NOT EXISTS idx_band_hr_ts ON heart_rate_band_samples(ts DESC);
CREATE INDEX IF NOT EXISTS idx_band_hr_date ON heart_rate_band_samples(date DESC);
"""


# ===== M2 章节 2.1 新增表 schema（追加在 SCHEMA 末尾，不破坏老用户 DB）=====
SCHEMA_M2 = """
-- M2 章节 2.1：睡眠 3 张表的 idempotent migration
-- 老 DB 自动加表（CREATE TABLE IF NOT EXISTS 是幂等的）
-- 但 UNIQUE 约束冲突时需要 dedup；新表无历史数据，跳过 dedup
"""


SCHEMA_M3 = """
-- ===== M3 章节 3.1 workout 详情：6 张表 =====

-- 1. GPS 轨迹点
CREATE TABLE IF NOT EXISTS workout_route_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    elapsed_sec INTEGER NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    altitude_m REAL,
    UNIQUE (track_id, elapsed_sec)
);
CREATE INDEX IF NOT EXISTS idx_route_tid ON workout_route_points(track_id, elapsed_sec);

-- 2. 逐秒采样（HR / speed / 步频 / 等）
CREATE TABLE IF NOT EXISTS workout_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    elapsed_sec INTEGER NOT NULL,
    heart_rate INTEGER,
    speed_mps REAL,
    pace_s_per_m REAL,
    cadence_spm INTEGER,
    stride_cm INTEGER,
    altitude_m REAL,
    power_watts INTEGER,
    ground_contact_ms INTEGER,
    vertical_oscillation_mm REAL,
    vertical_ratio_pct REAL,
    equivalent_pace_s_per_km REAL,
    UNIQUE (track_id, elapsed_sec)
);
CREATE INDEX IF NOT EXISTS idx_samples_tid ON workout_samples(track_id, elapsed_sec);

-- 3. 公里分段
CREATE TABLE IF NOT EXISTS workout_splits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    start_sec INTEGER NOT NULL,
    end_sec INTEGER NOT NULL,
    distance_m REAL,
    duration_seconds INTEGER,
    pace_min_per_km REAL,
    avg_hr INTEGER,
    max_hr INTEGER,
    elevation_gain_m REAL,
    elevation_loss_m REAL,
    partial INTEGER DEFAULT 0,
    UNIQUE (track_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_splits_tid ON workout_splits(track_id, idx);

-- 4. 手表圈
CREATE TABLE IF NOT EXISTS workout_laps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    start_sec INTEGER NOT NULL,
    end_sec INTEGER NOT NULL,
    distance_m REAL,
    duration_seconds INTEGER,
    avg_hr INTEGER,
    max_hr INTEGER,
    UNIQUE (track_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_laps_tid ON workout_laps(track_id, idx);

-- 5. 暂停
CREATE TABLE IF NOT EXISTS workout_pauses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id TEXT NOT NULL,
    start_ts_ms INTEGER NOT NULL,
    end_ts_ms INTEGER NOT NULL,
    kind INTEGER DEFAULT 0,
    UNIQUE (track_id, start_ts_ms)
);
CREATE INDEX IF NOT EXISTS idx_pauses_tid ON workout_pauses(track_id, start_ts_ms);

-- 6. HR 漂移
CREATE TABLE IF NOT EXISTS workout_hr_drift (
    track_id TEXT PRIMARY KEY,
    first_half_metres_per_beat REAL,
    second_half_metres_per_beat REAL,
    drift_percent REAL,
    speed_cv REAL,
    first_half_avg_hr REAL,
    second_half_avg_hr REAL,
    first_half_avg_speed_mps REAL,
    second_half_avg_speed_mps REAL,
    first_half_samples INTEGER,
    second_half_samples INTEGER,
    reason_code TEXT
);

-- ===== M3 章节 3.2 insights 2 张表 =====

-- workout 单次 insight
CREATE TABLE IF NOT EXISTS workout_insights (
    track_id TEXT PRIMARY KEY,
    facts_json TEXT NOT NULL,           -- JSON: list of InsightFact
    supported INTEGER NOT NULL,         -- 0/1
    unsupported_reason TEXT,
    baseline_window_days INTEGER,
    samples_count INTEGER,
    computed_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 周报
CREATE TABLE IF NOT EXISTS weekly_reports (
    date TEXT PRIMARY KEY,              -- 本周最后一天 (Sunday/Monday)
    facts_json TEXT NOT NULL,
    baseline_window_days INTEGER,
    baseline_days_available INTEGER,
    computed_at TEXT DEFAULT (datetime('now','localtime'))
);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    """初始化数据库 + 应用 schema。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # 应用 M3 schema（workout 详情 + insights）
    try:
        conn.executescript(SCHEMA_M3)
    except Exception as e:
        print(f"  [init_db] M3 schema 应用失败: {e}")
    # PRAGMA user_version 不支持参数化
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    # 设置 normalizer revision
    cur = conn.execute("SELECT value FROM meta WHERE key='normalizer_revision'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)",
                     ("normalizer_revision", NORMALIZER_REVISION))
    else:
        conn.execute("UPDATE meta SET value=? WHERE key='normalizer_revision'",
                     (NORMALIZER_REVISION,))
    conn.commit()
    return conn


def upsert_user_profile(conn: sqlite3.Connection, item: dict, raw_source_key: str = "") -> None:
    """从 Zepp members 流写 user_profile 表。

    item 是 members endpoint 的 items 数组里的元素：
      {memberId, userId, nickname, birthday, gender, height, weight, ...}

    Zepp birthday 格式是 "YYYY-MM" 字符串（无日）。
    ⚠️ gender 含义（Zepp 反人类约定）：
       0 = 女 (female)
       1 = 男 (male)
    """
    member_id = str(item.get("memberId", ""))
    if not member_id:
        return
    user_id = str(item.get("userId", ""))
    conn.execute("""
        INSERT OR REPLACE INTO user_profile
        (member_id, user_id, nickname, birthday, gender, height, weight, raw_source_key, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """, (
        member_id,
        user_id,
        item.get("nickname"),
        item.get("birthday"),  # "1990-01" 格式（YYYY-MM，无日）
        item.get("gender"),
        item.get("height"),
        item.get("weight"),
        raw_source_key,
    ))


def upsert_device(conn: sqlite3.Connection, item: dict) -> None:
    """写 devices 表（扁平结构）。"""
    import json as _json
    ai = {}
    if item.get("additionalInfo"):
        try:
            ai = _json.loads(item["additionalInfo"])
        except Exception:
            pass
    conn.execute("""
        INSERT OR REPLACE INTO devices
        (mac_address, device_type, device_source, device_id, sn, binding_status,
         application_time, last_status_update_time, firmware_version, product_id, extra_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        item.get("macAddress"),
        item.get("deviceType"),
        item.get("deviceSource"),
        item.get("deviceId"),
        item.get("sn"),
        item.get("bindingStatus"),
        item.get("applicationTime"),
        item.get("lastStatusUpdateTime"),
        item.get("firmwareVersion"),
        ai.get("productId"),
        item.get("additionalInfo"),
    ))


# ===== raw_records 写入 =====

def upsert_raw_record(conn: sqlite3.Connection,
                       source_key: str,
                       stream: str,
                       event_type: Optional[str],
                       sub_type: Optional[str],
                       surface: str,
                       start_ts_ms: Optional[int],
                       end_ts_ms: Optional[int],
                       device_id: Optional[str],
                       user_id: Optional[str],
                       payload: dict) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO raw_records
        (source_key, stream, event_type, sub_type, surface, start_ts_ms, end_ts_ms,
         device_id, user_id, payload, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """, (
        source_key, stream, event_type, sub_type, surface, start_ts_ms, end_ts_ms,
        device_id, user_id, json.dumps(payload, ensure_ascii=False),
    ))


# ===== measurements 写入 =====

def upsert_metric_sample(conn: sqlite3.Connection, sample: MetricSample,
                          user_id: Optional[str], raw_source_key: Optional[str],
                          stream_override: Optional[str] = None) -> None:
    from normalizer.common import to_local_date
    date = to_local_date(sample.timestamp)
    extra_json = json.dumps(sample.extra, ensure_ascii=False) if sample.extra else None
    stream = stream_override if stream_override else sample.metric
    conn.execute("""
        INSERT OR REPLACE INTO measurements
        (user_id, stream, metric, ts_ms, date, value, unit, source_scope, device_id,
         extra_json, raw_source_key, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """, (
        user_id,
        stream,
        sample.metric,
        int(sample.timestamp.timestamp() * 1000),
        date,
        sample.value,
        sample.unit,
        sample.source_scope,
        # device_id NULL → 强制转 '' 让 UNIQUE 约束生效
        # （SQLite NULL != NULL，UNIQUE 约束对 NULL 不生效）
        (sample.device_id or ""),
        extra_json,
        raw_source_key,
    ))


def upsert_daily_metric(conn: sqlite3.Connection, metric: DailyMetric,
                        user_id: Optional[str], raw_source_key: Optional[str]) -> None:
    """DailyMetric 用 date + 0 时点当 ts_ms。"""
    # 把 date 转成当日 UTC 0 点的 ms
    from datetime import datetime, timezone
    try:
        d = datetime.strptime(metric.date, "%Y-%m-%d")
        ts_ms = int(d.replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        ts_ms = 0
    conn.execute("""
        INSERT OR REPLACE INTO measurements
        (user_id, stream, metric, ts_ms, date, value, unit, source_scope, device_id,
         extra_json, raw_source_key, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """, (
        user_id,
        metric.metric,
        metric.metric,
        ts_ms,
        metric.date,
        metric.value,
        metric.unit,
        metric.source_scope,
        (metric.device_id or ""),  # NULL → '' 让 UNIQUE 生效
        None,
        raw_source_key,
    ))


# ===== capabilities 写入 =====

def update_capability(conn: sqlite3.Connection,
                       surface: str, event_type: str, sub_type: Optional[str],
                       status: str, sample_count: int,
                       last_observation_ts_ms: Optional[int]) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO capabilities
        (surface, event_type, sub_type, status, sample_count, last_observation_ts_ms,
         last_probed_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
    """, (surface, event_type, sub_type or "", status, sample_count, last_observation_ts_ms))


# ===== meta =====

# ===== M2 章节 2.1 新增：sleep 3 表 upsert =====

def upsert_sleep_session(conn: sqlite3.Connection, session: dict,
                          raw_source_key: str = "") -> Optional[int]:
    """
    写入 sleep_sessions 表。返回 session_id（用于后续 stage slices FK）。

    session 字典字段：
      user_id, date, source, start_ts, end_ts, tz_offset_secs, time_in_bed_secs,
      deep_secs, light_secs, rem_secs, awake_secs, unknown_secs,
      sp_o2_avg, breath_avg, rhr, score, is_nap, algo_version, sleep_source,
      raw_summary

    修复 bug：缺关键字段（date / start_ts / end_ts）时**不再 silent 失败**——
    之前是 `return None`，调用方只能靠 "sid 为 None" 推断，但 raw 已经解析到 session，
    这种"应该写但没写"的失败最难追。
    现在直接抛 ValueError，调用方（如 pull_to_sqlite._ingest_band_sleep）
    把异常和缺失字段名打到 diagnostic，让用户看到。

    注意：upsert_sleep_stage_slice / upsert_band_hr_sample 保留旧行为（静默跳过），
    因为它们的 stage 是 session 的子集，可能因为 session 没入库而连锁跳过。
    """
    # 关键字段校验：date / start_ts / end_ts 任一缺失就抛 ValueError
    missing = []
    if not session.get("date"):
        missing.append("date")
    if not session.get("start_ts"):
        missing.append("start_ts")
    if not session.get("end_ts"):
        missing.append("end_ts")
    if missing:
        raise ValueError(
            f"upsert_sleep_session: missing required field(s) {missing} "
            f"(raw_source_key={raw_source_key!r}, session.date={session.get('date')!r})"
        )
    raw_json = None
    if session.get("raw_summary") is not None:
        try:
            raw_json = json.dumps(session["raw_summary"], ensure_ascii=False)
        except Exception:
            raw_json = None
    cur = conn.execute("""
        INSERT OR REPLACE INTO sleep_sessions
        (user_id, date, source, start_ts, end_ts, tz_offset_secs,
         time_in_bed_secs, deep_secs, light_secs, rem_secs, awake_secs, unknown_secs,
         sp_o2_avg, breath_avg, rhr, score, is_nap,
         algo_version, sleep_source, raw_summary_json, raw_source_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session.get("user_id"),
        session["date"],
        session.get("source"),
        session["start_ts"],
        session["end_ts"],
        session.get("tz_offset_secs"),
        session.get("time_in_bed_secs"),
        session.get("deep_secs", 0),
        session.get("light_secs", 0),
        session.get("rem_secs", 0),
        session.get("awake_secs", 0),
        session.get("unknown_secs", 0),
        session.get("sp_o2_avg"),
        session.get("breath_avg"),
        session.get("rhr"),
        session.get("score"),
        1 if session.get("is_nap") else 0,
        session.get("algo_version"),
        session.get("sleep_source"),
        raw_json,
        raw_source_key,
    ))
    sid = cur.lastrowid
    if sid:
        return sid
    # INSERT OR REPLACE 时 lastrowid=0，回查
    row = conn.execute("""
        SELECT id FROM sleep_sessions
        WHERE date=? AND source=? AND start_ts=? AND is_nap=?
    """, (
        session["date"],
        session.get("source") or "",
        session["start_ts"],
        1 if session.get("is_nap") else 0,
    )).fetchone()
    return row["id"] if row else None


def upsert_sleep_stage_slice(conn: sqlite3.Connection, slice: dict,
                              raw_source_key: str = "") -> None:
    """写入 sleep_stage_slices。"""
    if not slice.get("date") or not slice.get("start_ts") or not slice.get("end_ts"):
        return
    conn.execute("""
        INSERT OR REPLACE INTO sleep_stage_slices
        (session_id, date, start_ts, end_ts, start_min, end_min,
         mode_code, mode, tz_offset_secs, raw_source_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        slice.get("session_id"),
        slice["date"],
        slice["start_ts"],
        slice["end_ts"],
        slice.get("start_min"),
        slice.get("end_min"),
        slice.get("mode_code"),
        slice.get("mode", "unknown"),
        slice.get("tz_offset_secs"),
        raw_source_key,
    ))


def upsert_band_hr_sample(conn: sqlite3.Connection, sample: dict,
                            raw_source_key: str = "") -> None:
    """写入 heart_rate_band_samples（逐分钟心率）。"""
    if not sample.get("date") or not sample.get("ts") or not sample.get("bpm"):
        return
    conn.execute("""
        INSERT OR REPLACE INTO heart_rate_band_samples
        (date, ts, bpm, tz_offset_secs, raw_source_key)
        VALUES (?, ?, ?, ?, ?)
    """, (
        sample["date"],
        sample["ts"],
        sample["bpm"],
        sample.get("tz_offset_secs"),
        raw_source_key,
    ))


# ===== M3 章节 3.1 workout 详情写入函数 =====

def upsert_workout_route_point(conn: sqlite3.Connection, p) -> None:
    """写入一条 GPS 点。"""
    if not p or p.latitude is None or p.longitude is None:
        return
    conn.execute("""
        INSERT OR REPLACE INTO workout_route_points
        (track_id, ts_ms, elapsed_sec, latitude, longitude, altitude_m)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        str(p.track_id), p.ts_ms, p.elapsed_sec,
        p.latitude, p.longitude, p.altitude_m,
    ))


def upsert_workout_sample(conn: sqlite3.Connection, s) -> None:
    """写入一条逐秒采样。"""
    if not s:
        return
    conn.execute("""
        INSERT OR REPLACE INTO workout_samples
        (track_id, ts_ms, elapsed_sec, heart_rate, speed_mps, pace_s_per_m,
         cadence_spm, stride_cm, altitude_m, power_watts, ground_contact_ms,
         vertical_oscillation_mm, vertical_ratio_pct, equivalent_pace_s_per_km)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(s.track_id), s.ts_ms, s.elapsed_sec,
        s.heart_rate, s.speed_mps, s.pace_s_per_m,
        s.cadence_spm, s.stride_cm, s.altitude_m, s.power_watts,
        s.ground_contact_ms, s.vertical_oscillation_mm,
        s.vertical_ratio_pct, s.equivalent_pace_s_per_km,
    ))


def upsert_workout_split(conn: sqlite3.Connection, sp) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO workout_splits
        (track_id, idx, start_sec, end_sec, distance_m, duration_seconds,
         pace_min_per_km, avg_hr, max_hr, elevation_gain_m, elevation_loss_m, partial)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(sp.track_id), sp.index, sp.start_sec, sp.end_sec,
        sp.distance_m, sp.duration_seconds, sp.pace_min_per_km,
        sp.avg_hr, sp.max_hr, sp.elevation_gain_m, sp.elevation_loss_m,
        1 if sp.partial else 0,
    ))


def upsert_workout_lap(conn: sqlite3.Connection, lp) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO workout_laps
        (track_id, idx, start_sec, end_sec, distance_m, duration_seconds, avg_hr, max_hr)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(lp.track_id), lp.index, lp.start_sec, lp.end_sec,
        lp.distance_m, lp.duration_seconds, lp.avg_hr, lp.max_hr,
    ))


def upsert_workout_pause(conn: sqlite3.Connection, pz) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO workout_pauses (track_id, start_ts_ms, end_ts_ms, kind)
        VALUES (?, ?, ?, ?)
    """, (
        str(pz.track_id), pz.start_ts_ms, pz.end_ts_ms, pz.kind,
    ))


def upsert_workout_hr_drift(conn: sqlite3.Connection, d) -> None:
    if not d:
        return
    conn.execute("""
        INSERT OR REPLACE INTO workout_hr_drift
        (track_id, first_half_metres_per_beat, second_half_metres_per_beat,
         drift_percent, speed_cv, first_half_avg_hr, second_half_avg_hr,
         first_half_avg_speed_mps, second_half_avg_speed_mps,
         first_half_samples, second_half_samples, reason_code)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(d.track_id), d.first_half_metres_per_beat, d.second_half_metres_per_beat,
        d.drift_percent, d.speed_cv, d.first_half_avg_hr, d.second_half_avg_hr,
        d.first_half_avg_speed_mps, d.second_half_avg_speed_mps,
        d.first_half_samples, d.second_half_samples, d.reason_code,
    ))


def upsert_workout_insight(conn: sqlite3.Connection, track_id: str,
                            facts_json: str, supported: bool,
                            unsupported_reason: str = None,
                            baseline_window_days: int = None,
                            samples_count: int = None) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO workout_insights
        (track_id, facts_json, supported, unsupported_reason,
         baseline_window_days, samples_count)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        track_id, facts_json, 1 if supported else 0,
        unsupported_reason, baseline_window_days, samples_count,
    ))


def upsert_weekly_report(conn: sqlite3.Connection, date: str,
                          facts_json: str, baseline_window_days: int,
                          baseline_days_available: int) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO weekly_reports
        (date, facts_json, baseline_window_days, baseline_days_available)
        VALUES (?, ?, ?, ?)
    """, (date, facts_json, baseline_window_days, baseline_days_available))


def delete_workout_detail(conn: sqlite3.Connection, track_id: str) -> None:
    """删除某 track_id 的所有 detail 数据（重新 sync 时用）。"""
    for tbl in ["workout_route_points", "workout_samples", "workout_splits",
                "workout_laps", "workout_pauses", "workout_hr_drift"]:
        conn.execute(f"DELETE FROM {tbl} WHERE track_id = ?", (track_id,))


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    cur = conn.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else None


# ===== 查询辅助 =====

def dedup_measurements(conn: sqlite3.Connection) -> int:
    """清理 measurements 表的 daily-level 重复 record。

    历史 bug：device_id NULL 在 SQLite UNIQUE 约束里视为不等，导致同一天
    同一 metric 多次 sync 留下多条不同值的 record。
    修法：手动选最新（device_id 非空优先 + raw_source_key 字符串最长）保留，
    删其他。

    Returns: 删除的条数
    """
    DAILY_METRICS = [
        "steps", "calories", "active_minutes", "pai_daily", "pai_high_zone",
        "pai_high_zone_lower_hr", "pai_high_zone_minutes", "pai_low_zone",
        "pai_low_zone_lower_hr", "pai_low_zone_minutes", "pai_medium_zone",
        "pai_medium_zone_lower_hr", "pai_medium_zone_minutes", "pai_total",
        "sport_load_today", "sport_load_7day_sum", "sport_load_optimal_min",
        "sport_load_optimal_max", "sport_load_overreaching",
        "device_max_hr", "device_resting_hr",
        "step_goal", "calorie_goal", "active_minutes_goal",
        "hybrid_charge_intel", "hybrid_charge_intel_diff", "hybrid_charge_intel_id",
        "respiratory_rate", "respiratory_rate_min", "respiratory_rate_max",
    ]
    deleted = 0
    for metric in DAILY_METRICS:
        dup_groups = conn.execute("""
            SELECT date, ts_ms, COUNT(*) AS n
            FROM measurements
            WHERE metric = ?
            GROUP BY date, ts_ms
            HAVING n > 1
        """, (metric,)).fetchall()
        for d, ts, _ in dup_groups:
            rows = conn.execute("""
                SELECT id, device_id, raw_source_key
                FROM measurements
                WHERE metric = ? AND date = ? AND ts_ms = ?
            """, (metric, d, ts)).fetchall()
            if len(rows) <= 1:
                continue

            def score(r):
                d = r["device_id"]
                return (
                    1 if d and d != "" else 0,  # 非空字符串优先
                    len(r["raw_source_key"]),    # 字符串长（最新 sync 时间窗）
                    r["id"],
                )
            keep = max(rows, key=score)
            for r in rows:
                if r["id"] != keep["id"]:
                    conn.execute("DELETE FROM measurements WHERE id = ?", (r["id"],))
                    deleted += 1
    conn.commit()
    return deleted


def counts(conn: sqlite3.Connection) -> dict:
    """DB 当前规模统计。"""
    out = {}
    for table in ("raw_records", "measurements", "capabilities", "meta", "devices"):
        cur = conn.execute(f"SELECT COUNT(*) AS c FROM {table}")
        out[table] = cur.fetchone()["c"]
    cur = conn.execute(
        "SELECT COUNT(DISTINCT stream) AS s FROM measurements"
    )
    out["distinct_streams"] = cur.fetchone()["s"]
    cur = conn.execute(
        "SELECT MIN(ts_ms) AS mn, MAX(ts_ms) AS mx FROM measurements WHERE ts_ms > 0"
    )
    r = cur.fetchone()
    if r["mn"]:
        from datetime import datetime, timezone
        out["earliest"] = datetime.fromtimestamp(r["mn"]/1000, tz=timezone.utc).isoformat()
        out["latest"] = datetime.fromtimestamp(r["mx"]/1000, tz=timezone.utc).isoformat()
    return out