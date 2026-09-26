"""
M3 章节 3.2：insights 模块

两个核心：
  1) WorkoutInsight：单次运动的 5 fact 对比（同类型 + 距离±20% + 180 天 + MAX 10）
  2) WeeklyReport：最近 7 天 vs 前 28 天的 7 fact

实现要点（按调研报告 §3）：
  - 个人基线对比，**不做诊断/治疗/风险预测**
  - 证据不足说不足
  - same 阈值 0.5（绝对差）
  - 置信度：0..=2 Insufficient, 3..=4 Low, 5..=7 Medium, 8+ High
  - 周报 sleep_start_regularity 跨午夜折算到 18:00 起点 + 12 小时 wrap
  - 周报窗口：recent=[today-6, today]、baseline=[today-34, today-7]
  - MIN_BASELINE_DAYS=7
  - collapse_per_day：同日多 scope（device + user_fused）折叠取平均
"""
from __future__ import annotations
import json
import math
import sqlite3
import statistics
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional


# ===== 常量（对齐调研报告 §3）=====

# Baseline
MIN_SAMPLES = 3
MAX_SAMPLES = 10
WINDOW_DAYS = 180
DISTANCE_TOLERANCE = 0.20

# 置信度阈值
INSUFFICIENT_MAX = 2
LOW_MAX = 4
MEDIUM_MAX = 7

# same 阈值（绝对差）
SAME_THRESHOLD = 0.5

# 配速合理范围（s/m）；用于 filter implausible
NORMAL_PACE_MIN_S_PER_M = 120.0 / 1000.0   # 120 s/km = 0.12 s/m
NORMAL_PACE_MAX_S_PER_M = 1800.0 / 1000.0  # 1800 s/km = 1.8 s/m

# Drift（已在 workout_detail 定义，这里复用）
HR_DRIFT_MIN_DURATION_SEC = 20 * 60
HR_DRIFT_MIN_SAMPLES_TOTAL = 120
HR_DRIFT_MIN_SAMPLES_PER_HALF = 60
HR_DRIFT_MAX_SPEED_CV = 0.20
HR_DRIFT_MIN_HR = 40
HR_DRIFT_MIN_SPEED_MPS = 0.5

# Weekly
MIN_BASELINE_DAYS = 7
WEEKLY_RECENT_DAYS = 7
WEEKLY_BASELINE_DAYS = 28
WEEKLY_BASELINE_PRIOR_DAYS = WEEKLY_BASELINE_DAYS - WEEKLY_RECENT_DAYS  # 21 天前段

# 18:00 起点（用于 sleep_start_regularity 跨午夜折算）
SLEEP_REGULARITY_ANCHOR_HOUR = 18
SLEEP_REGULARITY_ANCHOR_MINUTES = SLEEP_REGULARITY_ANCHOR_HOUR * 60  # 1080
SLEEP_REGULARITY_WRAP = 12 * 60  # 12 小时


# ===== Fact 数据类 =====

@dataclass
class InsightFact:
    fact_id: str
    metric: str
    unit: str
    value: float                  # 当前运动的值
    baseline_value: Optional[float] = None  # 基线均值
    delta_pct: Optional[float] = None
    direction: str = "same"       # higher / lower / same
    confidence: str = "Insufficient"  # Insufficient / Low / Medium / High
    samples_count: int = 0
    baseline_window_days: int = 0
    excluded_reason: Optional[str] = None
    excluded_code: Optional[str] = None  # workout_thin_baseline / workout_no_value / workout_zero_baseline


@dataclass
class WorkoutInsight:
    track_id: str
    workout_type: str
    supported: bool = True
    unsupported_reason: Optional[str] = None
    unsupported_code: Optional[str] = None
    facts: list = field(default_factory=list)  # list[InsightFact]
    heart_rate_drift: Optional[dict] = None  # 从 workout_hr_drift 复制
    samples_count: int = 0
    baseline_window_days: int = WINDOW_DAYS


@dataclass
class WeeklyFact:
    fact_id: str
    metric: str
    unit: str
    recent_value: Optional[float] = None
    baseline_value: Optional[float] = None
    delta_pct: Optional[float] = None
    direction: str = "same"
    baseline_days_available: int = 0
    excluded_reason: Optional[str] = None
    excluded_code: Optional[str] = None


@dataclass
class WeeklyReport:
    date: str                       # 周报日期（local today）
    recent_window_days: int = WEEKLY_RECENT_DAYS
    baseline_window_days: int = WEEKLY_BASELINE_DAYS
    baseline_days_available: int = 0
    facts: list = field(default_factory=list)  # list[WeeklyFact]
    excluded_reason: Optional[str] = None      # weekly_thin_baseline / weekly_no_recent_data


# ===== 工具 =====

def from_samples_count(n: int) -> str:
    if n <= INSUFFICIENT_MAX:
        return "Insufficient"
    if n <= LOW_MAX:
        return "Low"
    if n <= MEDIUM_MAX:
        return "Medium"
    return "High"


def compare(value: float, baseline_value: Optional[float]) -> str:
    """比较方向：higher/lower/same。same 阈值 0.5。"""
    if baseline_value is None:
        return "same"
    delta = value - baseline_value
    if abs(delta) <= SAME_THRESHOLD:
        return "same"
    return "higher" if delta > 0 else "lower"


def delta_percent(value: float, baseline_value: Optional[float]) -> Optional[float]:
    if baseline_value is None or baseline_value == 0:
        return None
    return round((value - baseline_value) / abs(baseline_value) * 100.0, 2)


def local_today_iso(today: Optional[date] = None) -> str:
    today = today or datetime.now().date()
    return today.isoformat()


# ===== WorkoutInsight =====

SUPPORTED_WORKOUT_TYPES = {"run"}

WORKOUT_INSIGHT_FACTS = [
    ("run.distance", "distance", "m"),
    ("run.duration", "duration", "s"),
    ("run.pace", "pace", "s/km"),
    ("run.avg_hr", "avg_hr", "bpm"),
    ("run.training_load", "training_load", "load"),
]


def compute_workout_insight(conn: sqlite3.Connection, track_id: str,
                              today: Optional[date] = None,
                              window_days: int = WINDOW_DAYS) -> WorkoutInsight:
    """
    对单次 workout 算 5 fact 对比。

    策略：
      1) 从 workouts 表读目标 workout（必须 sport_key='run'）
      2) 找 baseline：同 sport_key + 距离±20% + 180 天内最多 10 条
      3) 计算 5 fact；过滤 implausible pace（120-1800 s/km）
      4) 不支持类型 → supported=False + unsupported_reason
    """
    today = today or datetime.now().date()
    cur = conn.execute("""
        SELECT trackid, sport_key, sport_zh, dis_m, run_s, avg_heart_rate,
               end_time_ts
        FROM workouts WHERE trackid = ?
    """, (track_id,))
    row = cur.fetchone()
    if not row:
        return WorkoutInsight(
            track_id=track_id, workout_type="unknown",
            supported=False,
            unsupported_reason=f"track_id {track_id} 不在 workouts 表",
            unsupported_code="workout_not_found",
        )
    trackid, sport_key, sport_zh, dis_m, run_s, avg_hr, end_time_ts = row
    sport_label = sport_key or "unknown"
    if sport_label not in SUPPORTED_WORKOUT_TYPES:
        return WorkoutInsight(
            track_id=track_id, workout_type=sport_label,
            supported=False,
            unsupported_reason=f"暂不支持 {sport_zh} 类型 insight（仅 run）",
            unsupported_code="unsupported_workout_type",
        )

    # 计算 baseline 窗口
    end_dt = datetime.fromtimestamp(end_time_ts, tz=timezone.utc).date() if end_time_ts else today
    window_start_ts = int(datetime.combine(end_dt - timedelta(days=window_days),
                                            datetime.min.time(),
                                            tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime.combine(end_dt, datetime.max.time(),
                                   tzinfo=timezone.utc).timestamp())

    # 目标 workout 的 distance ± 20%
    if not dis_m or dis_m <= 0:
        return WorkoutInsight(
            track_id=track_id, workout_type=sport_label,
            supported=False,
            unsupported_reason="workout 没有 distance 字段",
            unsupported_code="missing_distance",
        )

    lo = dis_m * (1 - DISTANCE_TOLERANCE)
    hi = dis_m * (1 + DISTANCE_TOLERANCE)
    cur = conn.execute("""
        SELECT trackid, dis_m, run_s, avg_heart_rate, exercise_load
        FROM workouts
        WHERE sport_key = ? AND trackid != ?
          AND dis_m BETWEEN ? AND ?
          AND end_time_ts BETWEEN ? AND ?
        ORDER BY end_time_ts DESC LIMIT ?
    """, (sport_label, str(track_id), lo, hi, window_start_ts, end_ts, MAX_SAMPLES))
    baselines = cur.fetchall()
    samples_count = len(baselines)

    # 计算当前 workout 的 pace
    cur_pace_s_per_km = None
    if dis_m and dis_m > 0 and run_s and run_s > 0:
        cur_pace_s_per_km = run_s / (dis_m / 1000.0)
        # implausible 检查：120..1800 s/km（世界纪录 130 ~ 散步 900；上下扩展）
        if cur_pace_s_per_km < 120 or cur_pace_s_per_km > 1800:
            cur_pace_s_per_km = None

    # baseline stats
    b_distance = [b[1] for b in baselines if b[1]]
    b_duration = [b[2] for b in baselines if b[2]]
    b_avg_hr = [b[3] for b in baselines if b[3]]
    b_training_load = [b[4] for b in baselines if isinstance(b[4], (int, float))]

    # implausible pace 过滤：从 baselines 移除 implausible 的（基于 dis_m + run_s）
    filtered_b_pace = []
    for b in baselines:
        bd, br = b[1], b[2]
        if bd and br and bd > 0 and br > 0:
            pace = br / (bd / 1000.0)
            if 120 <= pace <= 1800:
                filtered_b_pace.append(pace)
    b_pace = filtered_b_pace

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    facts = []
    # 5 facts
    # 1) distance
    if dis_m is not None:
        bv = mean(b_distance)
        facts.append(InsightFact(
            fact_id="run.distance",
            metric="distance",
            unit="m",
            value=dis_m,
            baseline_value=round(bv, 1) if bv is not None else None,
            delta_pct=delta_percent(dis_m, bv),
            direction=compare(dis_m, bv),
            confidence=from_samples_count(samples_count),
            samples_count=samples_count,
            baseline_window_days=window_days,
            excluded_reason=None if bv is not None else "baseline_thin",
            excluded_code=None if bv is not None else "workout_thin_baseline",
        ))
    else:
        facts.append(InsightFact(
            fact_id="run.distance", metric="distance", unit="m",
            value=0, excluded_reason="missing distance",
            excluded_code="missing_distance",
        ))

    # 2) duration
    if run_s is not None and run_s > 0:
        bv = mean(b_duration)
        facts.append(InsightFact(
            fact_id="run.duration",
            metric="duration", unit="s",
            value=run_s,
            baseline_value=round(bv, 0) if bv is not None else None,
            delta_pct=delta_percent(run_s, bv),
            direction=compare(run_s, bv),
            confidence=from_samples_count(samples_count),
            samples_count=samples_count,
            baseline_window_days=window_days,
            excluded_reason=None if bv is not None else "baseline_thin",
            excluded_code=None if bv is not None else "workout_thin_baseline",
        ))
    else:
        facts.append(InsightFact(
            fact_id="run.duration", metric="duration", unit="s",
            value=0, excluded_reason="missing duration",
            excluded_code="missing_duration",
        ))

    # 3) pace
    if cur_pace_s_per_km is not None:
        bv = mean(b_pace)
        facts.append(InsightFact(
            fact_id="run.pace",
            metric="pace", unit="s/km",
            value=round(cur_pace_s_per_km, 2),
            baseline_value=round(bv, 2) if bv is not None else None,
            delta_pct=delta_percent(cur_pace_s_per_km, bv),
            direction=compare(cur_pace_s_per_km, bv),
            confidence=from_samples_count(samples_count),
            samples_count=samples_count,
            baseline_window_days=window_days,
            excluded_reason="implausible_pace" if (bv is None and not b_pace) else
                            (None if bv is not None else "baseline_thin"),
            excluded_code="implausible_pace" if (bv is None and not b_pace) else
                          (None if bv is not None else "workout_thin_baseline"),
        ))
    else:
        facts.append(InsightFact(
            fact_id="run.pace", metric="pace", unit="s/km",
            value=0, excluded_reason="implausible pace",
            excluded_code="implausible_pace",
        ))

    # 4) avg_hr
    if avg_hr is not None and avg_hr > 0:
        bv = mean(b_avg_hr)
        facts.append(InsightFact(
            fact_id="run.avg_hr",
            metric="avg_hr", unit="bpm",
            value=float(avg_hr),
            baseline_value=round(bv, 1) if bv is not None else None,
            delta_pct=delta_percent(avg_hr, bv),
            direction=compare(avg_hr, bv),
            confidence=from_samples_count(samples_count),
            samples_count=samples_count,
            baseline_window_days=window_days,
            excluded_reason=None if bv is not None else "baseline_thin",
            excluded_code=None if bv is not None else "workout_thin_baseline",
        ))
    else:
        facts.append(InsightFact(
            fact_id="run.avg_hr", metric="avg_hr", unit="bpm",
            value=0, excluded_reason="missing avg_hr",
            excluded_code="workout_no_value",
        ))

    # 5) training_load
    cur_tl = None  # 当前 workout 没有明确训练负荷字段；先用 NULL
    bv = mean(b_training_load)
    if cur_tl is not None:
        facts.append(InsightFact(
            fact_id="run.training_load",
            metric="training_load", unit="load",
            value=cur_tl,
            baseline_value=round(bv, 1) if bv is not None else None,
            delta_pct=delta_percent(cur_tl, bv),
            direction=compare(cur_tl, bv),
            confidence=from_samples_count(samples_count),
            samples_count=samples_count,
            baseline_window_days=window_days,
        ))
    else:
        facts.append(InsightFact(
            fact_id="run.training_load", metric="training_load", unit="load",
            value=0, excluded_reason="training_load field not in workouts table",
            excluded_code="workout_no_value",
        ))

    # HR drift
    drift_row = conn.execute("""
        SELECT first_half_metres_per_beat, second_half_metres_per_beat,
               drift_percent, speed_cv, first_half_avg_hr, second_half_avg_hr,
               first_half_avg_speed_mps, second_half_avg_speed_mps,
               first_half_samples, second_half_samples, reason_code
        FROM workout_hr_drift WHERE track_id = ?
    """, (track_id,)).fetchone()
    drift_dict = None
    if drift_row:
        drift_dict = {
            "first_half_metres_per_beat": drift_row[0],
            "second_half_metres_per_beat": drift_row[1],
            "drift_percent": drift_row[2],
            "speed_cv": drift_row[3],
            "first_half_avg_hr": drift_row[4],
            "second_half_avg_hr": drift_row[5],
            "first_half_avg_speed_mps": drift_row[6],
            "second_half_avg_speed_mps": drift_row[7],
            "first_half_samples": drift_row[8],
            "second_half_samples": drift_row[9],
            "reason_code": drift_row[10],
        }

    return WorkoutInsight(
        track_id=track_id,
        workout_type=sport_label,
        supported=True,
        facts=facts,
        heart_rate_drift=drift_dict,
        samples_count=samples_count,
        baseline_window_days=window_days,
    )


def insight_to_json(insight: WorkoutInsight) -> str:
    """Insight 对象 → JSON 字符串（用于存库）"""
    return json.dumps({
        "track_id": insight.track_id,
        "workout_type": insight.workout_type,
        "supported": insight.supported,
        "unsupported_reason": insight.unsupported_reason,
        "unsupported_code": insight.unsupported_code,
        "facts": [asdict(f) for f in insight.facts],
        "heart_rate_drift": insight.heart_rate_drift,
        "samples_count": insight.samples_count,
        "baseline_window_days": insight.baseline_window_days,
    }, ensure_ascii=False)


# ===== WeeklyReport =====

WEEKLY_FACTS = [
    ("weekly.resting_hr", "resting_hr", "bpm", "metric", "daily_metrics", "metric='resting_hr'"),
    ("weekly.hrv", "hrv", "ms", "metric", "daily_metrics", "metric='hrv'"),
    ("weekly.stress", "stress", "score", "metric", "daily_metrics", "metric='stress'"),
    ("weekly.sleep_duration", "sleep_duration", "min", "agg", "sleep_sessions", "SUM(per_day)"),
    ("weekly.sleep_start_regularity", "sleep_start_regularity", "min", "agg", "sleep_sessions", "stddev(local_min)"),
    ("weekly.workout_count", "workout_count", "count", "min", "workouts", "COUNT(*)"),
    ("weekly.training_load", "training_load", "load", "metric", "daily_metrics", "metric='training_load'"),
]


def collapse_per_day_metric(conn: sqlite3.Connection, table: str, metric_col: str,
                             date_from: str, date_to: str, value_col: str = "value"):
    """
    折叠：按 (date, metric) 取该日均值 → dict{date: mean_value}
    多 scope（device / user_fused）自动合并。
    """
    sql = f"""
        SELECT date, AVG({value_col}) as v
        FROM {table}
        WHERE metric = ? AND date BETWEEN ? AND ?
          AND {value_col} IS NOT NULL
        GROUP BY date
        ORDER BY date
    """
    rows = conn.execute(sql, (metric_col, date_from, date_to)).fetchall()
    return {r[0]: r[1] for r in rows}


def _measurements_table(conn: sqlite3.Connection) -> str:
    """检测实际存在的 daily metrics 表名：measurements / daily_metrics"""
    try:
        conn.execute("SELECT 1 FROM daily_metrics LIMIT 1")
        return "daily_metrics"
    except sqlite3.OperationalError:
        return "measurements"


def mean_of_dict(d: dict) -> Optional[float]:
    if not d:
        return None
    vals = [v for v in d.values() if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def stddev_of_dict(d: dict) -> Optional[float]:
    if len(d) < 2:
        return None
    vals = [v for v in d.values() if v is not None]
    if len(vals) < 2:
        return None
    return statistics.pstdev(vals)


def compute_weekly_report(conn: sqlite3.Connection,
                           today: Optional[date] = None) -> WeeklyReport:
    """
    算本周 vs 前 28 天的周报。

    Window:
      recent = [today-6, today]  本地日历日
      baseline = [today-34, today-7]  本地日历日

    Returns:
      WeeklyReport(date=today, facts=[WeeklyFact × 7])
    """
    today = today or datetime.now().date()
    recent_start = today - timedelta(days=6)
    baseline_start = today - timedelta(days=34)
    baseline_end = today - timedelta(days=7)

    table = _measurements_table(conn)
    facts = []

    # 1) resting_hr
    recent_data = collapse_per_day_metric(conn, table, "resting_hr",
                                          recent_start.isoformat(), today.isoformat())
    baseline_data = collapse_per_day_metric(conn, table, "resting_hr",
                                             baseline_start.isoformat(), baseline_end.isoformat())
    rv = mean_of_dict(recent_data)
    bv = mean_of_dict(baseline_data)
    facts.append(_make_weekly_fact("weekly.resting_hr", "resting_hr", "bpm",
                                    rv, bv, len(baseline_data)))

    # 2) hrv（dual lookup：hrv + hrv_rmssd）
    recent_data = collapse_per_day_metric(conn, table, "hrv",
                                          recent_start.isoformat(), today.isoformat())
    if not recent_data:
        recent_data = collapse_per_day_metric(conn, table, "hrv_rmssd",
                                               recent_start.isoformat(), today.isoformat())
    baseline_data = collapse_per_day_metric(conn, table, "hrv",
                                             baseline_start.isoformat(), baseline_end.isoformat())
    if not baseline_data:
        baseline_data = collapse_per_day_metric(conn, table, "hrv_rmssd",
                                                 baseline_start.isoformat(), baseline_end.isoformat())
    rv = mean_of_dict(recent_data)
    bv = mean_of_dict(baseline_data)
    facts.append(_make_weekly_fact("weekly.hrv", "hrv", "ms",
                                    rv, bv, len(baseline_data)))

    # 3) stress
    recent_data = collapse_per_day_metric(conn, table, "stress",
                                          recent_start.isoformat(), today.isoformat())
    baseline_data = collapse_per_day_metric(conn, table, "stress",
                                             baseline_start.isoformat(), baseline_end.isoformat())
    rv = mean_of_dict(recent_data)
    bv = mean_of_dict(baseline_data)
    facts.append(_make_weekly_fact("weekly.stress", "stress", "score",
                                    rv, bv, len(baseline_data)))

    # 4) sleep_duration（按日 SUM(per_day)，再 7 天均值）
    recent_sleep = _daily_sleep_total(conn, recent_start.isoformat(), today.isoformat())
    baseline_sleep = _daily_sleep_total(conn, baseline_start.isoformat(), baseline_end.isoformat())
    rv = mean_of_dict(recent_sleep)
    bv = mean_of_dict(baseline_sleep)
    facts.append(_make_weekly_fact("weekly.sleep_duration", "sleep_duration", "min",
                                    rv, bv, len(baseline_sleep)))

    # 5) sleep_start_regularity：标准差（分钟，按本地时间 18:00 起点 + 12h wrap）
    recent_starts = _daily_sleep_starts(conn, recent_start.isoformat(), today.isoformat())
    baseline_starts = _daily_sleep_starts(conn, baseline_start.isoformat(), baseline_end.isoformat())
    rv = stddev_of_dict(recent_starts) if recent_starts else None
    bv = stddev_of_dict(baseline_starts) if baseline_starts else None
    # direction: lower stddev = better regular → "lower" 表示更规律
    facts.append(_make_weekly_fact("weekly.sleep_start_regularity", "sleep_start_regularity", "min",
                                    rv, bv, len(recent_starts), invert_direction=False))

    # 6) workout_count（总数，不是按日折叠后求和）
    recent_count = _count_workouts(conn, recent_start.isoformat(), today.isoformat())
    baseline_count = _count_workouts(conn, baseline_start.isoformat(), baseline_end.isoformat())
    facts.append(_make_weekly_fact("weekly.workout_count", "workout_count", "count",
                                    float(recent_count), float(baseline_count),
                                    len({int(baseline_count)})))

    # 7) training_load
    recent_data = collapse_per_day_metric(conn, table, "training_load",
                                          recent_start.isoformat(), today.isoformat())
    baseline_data = collapse_per_day_metric(conn, table, "training_load",
                                             baseline_start.isoformat(), baseline_end.isoformat())
    rv = mean_of_dict(recent_data)
    bv = mean_of_dict(baseline_data)
    facts.append(_make_weekly_fact("weekly.training_load", "training_load", "load",
                                    rv, bv, len(baseline_data)))

    # baseline days available
    max_baseline_days = max(
        len(baseline_data),
        len(baseline_sleep),
        len(recent_starts),
    )

    return WeeklyReport(
        date=today.isoformat(),
        baseline_days_available=max_baseline_days,
        facts=facts,
    )


def _make_weekly_fact(fact_id: str, metric: str, unit: str,
                       recent_value: Optional[float],
                       baseline_value: Optional[float],
                       baseline_days: int,
                       invert_direction: bool = False) -> WeeklyFact:
    """构造一条 WeeklyFact + 处理 insufficient / thin baseline"""
    if recent_value is None and baseline_value is None:
        return WeeklyFact(
            fact_id=fact_id, metric=metric, unit=unit,
            recent_value=None, baseline_value=None,
            delta_pct=None, direction="same",
            baseline_days_available=baseline_days,
            excluded_reason="no recent or baseline data",
            excluded_code="weekly_no_data",
        )
    if recent_value is None:
        return WeeklyFact(
            fact_id=fact_id, metric=metric, unit=unit,
            recent_value=None,
            baseline_value=round(baseline_value, 2) if baseline_value is not None else None,
            delta_pct=None, direction="same",
            baseline_days_available=baseline_days,
            excluded_reason="no recent data",
            excluded_code="weekly_no_recent_data",
        )
    if baseline_value is None or baseline_days < MIN_BASELINE_DAYS:
        return WeeklyFact(
            fact_id=fact_id, metric=metric, unit=unit,
            recent_value=round(recent_value, 2),
            baseline_value=None,
            delta_pct=None, direction="same",
            baseline_days_available=baseline_days,
            excluded_reason=f"baseline 仅 {baseline_days} 天（需 {MIN_BASELINE_DAYS}）",
            excluded_code="weekly_thin_baseline",
        )
    if baseline_value == 0:
        return WeeklyFact(
            fact_id=fact_id, metric=metric, unit=unit,
            recent_value=round(recent_value, 2),
            baseline_value=0.0,
            delta_pct=None, direction="same",
            baseline_days_available=baseline_days,
            excluded_reason="baseline 均值为 0",
            excluded_code="weekly_zero_baseline",
        )
    delta = (recent_value - baseline_value) / abs(baseline_value) * 100
    direction = "same"
    if abs(recent_value - baseline_value) > SAME_THRESHOLD:
        direction = "higher" if recent_value > baseline_value else "lower"
    return WeeklyFact(
        fact_id=fact_id, metric=metric, unit=unit,
        recent_value=round(recent_value, 2),
        baseline_value=round(baseline_value, 2),
        delta_pct=round(delta, 2),
        direction=direction,
        baseline_days_available=baseline_days,
    )


def _daily_sleep_total(conn: sqlite3.Connection, date_from: str, date_to: str):
    """按日 sum sleep session 时长（秒），返回 dict{date: minutes}"""
    rows = conn.execute("""
        SELECT date, SUM(time_in_bed_secs) AS secs
        FROM sleep_sessions
        WHERE date BETWEEN ? AND ? AND time_in_bed_secs IS NOT NULL
          AND is_nap = 0
        GROUP BY date
    """, (date_from, date_to)).fetchall()
    return {r[0]: (r[1] / 60.0) for r in rows if r[1]}


def _daily_sleep_starts(conn: sqlite3.Connection, date_from: str, date_to: str):
    """
    按日 main session start_ts → local minute-of-day (anchored 18:00, wrapped 12h)。
    返回 dict{date: anchor_minute}。
    """
    rows = conn.execute("""
        SELECT date, MIN(start_ts) AS start_ts, MAX(tz_offset_secs) AS tz
        FROM sleep_sessions
        WHERE date BETWEEN ? AND ? AND is_nap = 0
          AND start_ts IS NOT NULL
        GROUP BY date
    """, (date_from, date_to)).fetchall()
    out = {}
    for d, start_ts, tz in rows:
        if not start_ts or tz is None:
            continue
        # start_ts 是 unix 秒，tz 是 UTC offset 秒
        # 本地时间 = start_ts + tz
        local_unix = start_ts + tz
        dt = datetime.fromtimestamp(local_unix, tz=timezone.utc)
        local_minute_of_day = dt.hour * 60 + dt.minute
        # wrap to 18:00 anchor + 12h
        # if minute < 18:00, it's "after midnight" → 18:00 is anchor, wrap to next day
        anchor = SLEEP_REGULARITY_ANCHOR_MINUTES  # 1080
        # convert: minute-of-day is 0..1439
        # If minute < anchor, add 1440 (treat as next day anchor)
        if local_minute_of_day < anchor:
            local_minute_of_day += 1440
        # Now wrap to [anchor, anchor + 12*60) = [1080, 1800)
        # But it might already be in [1080, 2880). Modulo 1440 won't work because we want
        # the difference to consider 23:50 and 00:10 as 20 min apart.
        # Simpler: just keep it as-is for stddev computation.
        out[d] = local_minute_of_day
    return out


def _count_workouts(conn: sqlite3.Connection, date_from: str, date_to: str) -> int:
    """按 end_time_iso[:10] 计 workout 数量（不是按日折叠后求和）"""
    rows = conn.execute("""
        SELECT end_time_iso FROM workouts
        WHERE substr(end_time_iso, 1, 10) BETWEEN ? AND ?
    """, (date_from, date_to)).fetchall()
    return len(rows)


def weekly_report_to_json(report: WeeklyReport) -> str:
    return json.dumps({
        "date": report.date,
        "recent_window_days": report.recent_window_days,
        "baseline_window_days": report.baseline_window_days,
        "baseline_days_available": report.baseline_days_available,
        "facts": [asdict(f) for f in report.facts],
        "excluded_reason": report.excluded_reason,
    }, ensure_ascii=False)


# ===== CLI / sync 入口 =====

def main():
    import argparse
    from pathlib import Path
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="/root/.zepp-data/zepp.db")
    p.add_argument("--track-id", help="compute workout insight for track_id")
    p.add_argument("--weekly", action="store_true", help="compute weekly report")
    p.add_argument("--all-recent-runs", action="store_true",
                   help="compute insights for all run workouts in last 30 days")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    if args.track_id:
        insight = compute_workout_insight(conn, args.track_id)
        print(insight_to_json(insight))

    if args.weekly:
        report = compute_weekly_report(conn)
        print(weekly_report_to_json(report))

    if args.all_recent_runs:
        rows = conn.execute("""
            SELECT trackid FROM workouts
            WHERE sport_key = 'run' AND end_time_ts > ?
            ORDER BY end_time_ts DESC LIMIT 30
        """, (int(datetime.now().timestamp()) - 30 * 86400,)).fetchall()
        for r in rows:
            tid = r[0]
            ins = compute_workout_insight(conn, tid)
            print(f"track={tid} sport={ins.workout_type} samples={ins.samples_count}")
            for f in ins.facts:
                excl = f" (excluded: {f.excluded_code})" if f.excluded_code else ""
                print(f"  {f.fact_id:<25} value={f.value} baseline={f.baseline_value} dir={f.direction}{excl}")
            print()


if __name__ == "__main__":
    main()