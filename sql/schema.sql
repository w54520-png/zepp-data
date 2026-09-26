-- =============================================================================
-- Zepp Data Skill — SQLite schema (17 张表)
-- =============================================================================
--
-- ⚠️  本文件由 `scripts/storage.py::SCHEMA` (+ `SCHEMA_M2` + `SCHEMA_M3`) 手动维护的
--     镜像。改 schema 必须改 storage.py 里的三块常量字符串，本文件同步更新。
--     不要再直接编辑本文件 —— `pull_to_sqlite.py init` 会基于 storage.py 创建 DB。
--
-- 版本：SCHEMA_VERSION = 2 (来自 storage.py)
-- 同步日期：2026-09-24
-- 数据流版本：NORMALIZER_REVISION = "m3-workout-detail-2026-09-23"
--
-- =============================================================================
-- 分组索引
-- =============================================================================
--   基础数据层 (3 张)        : raw_records / devices / user_profile
--   规范化标量层 (3 张)      : measurements / capabilities / meta
--   睡眠 (3 张)              : sleep_sessions / sleep_stage_slices / heart_rate_band_samples
--   运动详情 (6 张)          : workout_route_points / workout_samples / workout_splits
--                              / workout_laps / workout_pauses / workout_hr_drift
--   Insights (2 张)          : workout_insights / weekly_reports
--
-- 历史演进：
--   M1 — workout summary 字段补齐（历史 schema 4 表：raw_records / measurements
--        / capabilities / meta）
--   M1 — 加 user_profile（成员档案）
--   M1 — 加 devices（设备清单）
--   M2 — 加 sleep_sessions / sleep_stage_slices / heart_rate_band_samples（睡眠解析）
--   M3 — 加 workout_route_points / workout_samples / workout_splits / workout_laps
--        / workout_pauses / workout_hr_drift（workout 详情）
--   M3 — 加 workout_insights / weekly_reports（insights 周报）
--
-- 注：M4（限流应对/饮食流/VO2max/国际服/dashboard 趋势线/压力 24h）**不增加新表**，
--     只往 measurements 里写更多 stream。
--
-- =============================================================================
-- 1. 基础数据层 (3 张)
-- =============================================================================

-- 1.1 原始响应（完整保留，方便日后改 normalizer 重放）
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

-- 1.2 设备清单（扁平，来自 Zepp `devices` 流）
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

-- 1.3 用户档案（生日/性别/身高/体重，Zepp members 流）
CREATE TABLE IF NOT EXISTS user_profile (
    member_id TEXT PRIMARY KEY,     -- '-1' 是账号持有人；其他数字是家庭成员
    user_id TEXT NOT NULL,
    nickname TEXT,
    birthday TEXT,                  -- YYYY-MM（Zepp 实际存储的格式，例 '1990-01'）
    gender INTEGER,                 -- ⚠️ Zepp 反人类约定：0=女 1=男
    height REAL,                    -- cm
    weight REAL,                    -- kg
    raw_source_key TEXT,            -- 关联 raw_records
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_profile_user ON user_profile(user_id);


-- =============================================================================
-- 2. 规范化标量层 (3 张)
-- =============================================================================

-- 2.1 规范化后的标量（来自 normalizer 写 measurements）
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    stream TEXT NOT NULL,           -- "hrv_rmssd" / "pai_daily" / "spo2" ...
    metric TEXT NOT NULL,           -- 同 stream（用于兼容老 query）
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

-- 2.2 能力探测（哪些流有数据）
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

-- 2.3 元数据（normalizer_revision / schema_version / 用户偏好等 KV）
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);


-- =============================================================================
-- 3. 睡眠 (3 张 — M2 章节 2.1 新增)
-- =============================================================================

-- 3.1 睡眠 session（一条主睡 / 一次午睡）
CREATE TABLE IF NOT EXISTS sleep_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    date TEXT NOT NULL,             -- 本地日历日（Zepp API date_time）
    source TEXT,                    -- device MAC / source 字段
    start_ts INTEGER NOT NULL,      -- unix 秒
    end_ts INTEGER NOT NULL,
    tz_offset_secs INTEGER,         -- 解析后的时区偏移
    time_in_bed_secs INTEGER,       -- 在床时长（4 级兜底）
    deep_secs INTEGER DEFAULT 0,
    light_secs INTEGER DEFAULT 0,
    rem_secs INTEGER DEFAULT 0,
    awake_secs INTEGER DEFAULT 0,
    unknown_secs INTEGER DEFAULT 0,
    sp_o2_avg REAL,
    breath_avg REAL,
    rhr INTEGER,
    score INTEGER,
    is_nap INTEGER DEFAULT 0,       -- 1=午睡（odd_stage 提取）
    algo_version TEXT,
    sleep_source INTEGER,
    raw_summary_json TEXT,          -- 完整 decoded summary JSON（debug）
    raw_source_key TEXT,            -- 关联 raw_records
    ingested_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, source, date, start_ts, is_nap)
);
CREATE INDEX IF NOT EXISTS idx_sleep_sessions_date ON sleep_sessions(date DESC);
CREATE INDEX IF NOT EXISTS idx_sleep_sessions_start ON sleep_sessions(start_ts DESC);

-- 3.2 睡眠阶段切片
CREATE TABLE IF NOT EXISTS sleep_stage_slices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,             -- FK → sleep_sessions.id（NULL 表示 session 还没插入）
    date TEXT NOT NULL,
    start_ts INTEGER NOT NULL,      -- unix 秒
    end_ts INTEGER NOT NULL,
    start_min INTEGER NOT NULL,     -- 相对 slp.st 锚点的分钟数
    end_min INTEGER NOT NULL,
    mode_code INTEGER NOT NULL,     -- Zepp 原始数字 mode
    mode TEXT NOT NULL,             -- "deep"/"light"/"rem"/"awake"/"unknown"
    tz_offset_secs INTEGER,
    raw_source_key TEXT,
    ingested_at TEXT DEFAULT (datetime('now')),
    UNIQUE (date, start_ts, end_ts, mode_code, tz_offset_secs)
);
CREATE INDEX IF NOT EXISTS idx_stage_session ON sleep_stage_slices(session_id);
CREATE INDEX IF NOT EXISTS idx_stage_date ON sleep_stage_slices(date DESC);

-- 3.3 band_data 逐分钟心率（detail 端点才有）
CREATE TABLE IF NOT EXISTS heart_rate_band_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    ts INTEGER NOT NULL,            -- unix 秒（本地时间）
    bpm INTEGER,                    -- 20..240（255 哨兵 → 不入库）
    tz_offset_secs INTEGER,
    raw_source_key TEXT,
    ingested_at TEXT DEFAULT (datetime('now')),
    UNIQUE (date, ts)
);
CREATE INDEX IF NOT EXISTS idx_band_hr_ts ON heart_rate_band_samples(ts DESC);
CREATE INDEX IF NOT EXISTS idx_band_hr_date ON heart_rate_band_samples(date DESC);


-- =============================================================================
-- 4. 运动详情 (6 张 — M3 章节 3.1 新增)
-- =============================================================================

-- 4.1 GPS 轨迹点
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

-- 4.2 逐秒采样（HR / speed / 步频 / 等）
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

-- 4.3 公里分段
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

-- 4.4 手表圈（用户手动按圈 / 自动 lap）
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

-- 4.5 暂停（auto-pause / 手动暂停）
CREATE TABLE IF NOT EXISTS workout_pauses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id TEXT NOT NULL,
    start_ts_ms INTEGER NOT NULL,
    end_ts_ms INTEGER NOT NULL,
    kind INTEGER DEFAULT 0,
    UNIQUE (track_id, start_ts_ms)
);
CREATE INDEX IF NOT EXISTS idx_pauses_tid ON workout_pauses(track_id, start_ts_ms);

-- 4.6 HR 漂移（前/后半程 HR 对比）
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


-- =============================================================================
-- 5. Insights (2 张 — M3 章节 3.2 新增)
-- =============================================================================

-- 5.1 workout 单次 insight（vs 历史基线）
CREATE TABLE IF NOT EXISTS workout_insights (
    track_id TEXT PRIMARY KEY,
    facts_json TEXT NOT NULL,           -- JSON: list of InsightFact
    supported INTEGER NOT NULL,         -- 0/1
    unsupported_reason TEXT,
    baseline_window_days INTEGER,
    samples_count INTEGER,
    computed_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 5.2 周报（每周聚合 insight）
CREATE TABLE IF NOT EXISTS weekly_reports (
    date TEXT PRIMARY KEY,              -- 本周最后一天 (Sunday/Monday)
    facts_json TEXT NOT NULL,
    baseline_window_days INTEGER,
    baseline_days_available INTEGER,
    computed_at TEXT DEFAULT (datetime('now','localtime'))
);


-- =============================================================================
-- 维护说明
-- =============================================================================
-- 1. 修改任何 schema → 改 `scripts/storage.py` 里 SCHEMA / SCHEMA_M2 / SCHEMA_M3
--    三个常量字符串的对应部分 → 同步更新本文件 → 跑 `pull_to_sqlite.py init` 验证
-- 2. 加新表 → 在 storage.py 对应常量字符串里加 `CREATE TABLE IF NOT EXISTS ...`
--    → 在本文件对应分组下加同一段 → 跑 `python3 -m sqlite3 <db> ".tables"` 验证
-- 3. 删除/改名表 → storage.py 优先；老 DB 用 `scripts/migrate_*.py` 单独处理
--    (init_db 用 IF NOT EXISTS + 单独 SCHEMA_M* 块演进，不破坏老用户 DB)
-- 4. UNIQUE 约束冲突（特别是 measurements.device_id NULL → ''）处理见
--    references/fix-history.md
-- =============================================================================