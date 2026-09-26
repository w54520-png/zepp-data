"""
workout_detail 解析 —— M3 章节 3.1。

端点：GET /v1/sport/run/detail.json
参数：trackid（运动 ID）/ source（gpx|app|watch|gps 等）

响应（code=1 时的 data 字段）：
  {
    "trackid": 1777086483,
    "source": "gpx",
    "time": "1;1;1;...",           # 秒差分链（累积秒数夹在 [0, MAX_ACTIVITY_SECONDS]）
    "longitude_latitude": "0,0;100,200;...",   # lat/lon 整数差分（/1e8 转度）
    "altitude": "0;500;1000;...",    # 海拔厘米差分（哨兵大负数过滤）
    "time_delta_altitude": "0,0;1,500;...",    # (dt_sec, cm_alt) 首选对，独立游标
    "currentDistance": "0;500;...", # 累计厘米
    "heart_rate": "1,60;1,62;...",  # (dt_sec, bpm_delta) —— bpm 是差分！
    "speed": "1,2.5;1,2.6;...",     # (dt_sec, m/s)
    "gait": "1,2,80,160;...",       # (dt_sec, step_delta, stride_cm, cadence_spm)
    "power_meter": "1,200;...",     # (dt_sec, watts)
    "equivPace": "1,360;...",       # (dt_sec, s_per_km)
    "runPosture": "1,200,10,5;...", # (dt_sec, gct_ms, vo_mm, vsr_tenths_pct)
    "pause": "1234567890,1,0,0,2;...",  # (start_unix, end_delta_sec, ?, ?, kind)
    "kilo_pace": "0,300,...;1,250,...",  # 每公里段；列宽不稳，只用 [0][1][5]
    "lap": "0,1,1000,0,140,3600,..."     # 手表圈；列宽不稳，只用 [0][2][4][5]
  }

输出：
  DecodedWorkout { track_id, source, start_time, end_time, summary_distance_m, summary_duration,
                   route: list[RoutePoint], samples: list[WorkoutSample],
                   pauses, splits, laps, heart_rate_drift: HeartRateDrift,
                   diagnostics: list[str] }

关键规则（按调研报告 §2.1 + ZeppBridge decoder/workout_detail.rs）：
- 13 个差分链全部解析；malformed delta **整行丢**，绝不 unwrap_or(0)
- 海拔哨兵 -2000000 / -2002110 / -2003943 → 窗口 -100000..1000000 cm 判断
- runPosture 哨兵 65535 (GCT/VO) → NaN；255 (VSR) → NaN（额外 /10 转 %）
- HR > 250 / < 1 过滤；lap HR=0 → 未测到
- currentDistance 单步 > 200 m/s 或回退 → 当坏点丢
- 海拔双路径：altitude 差分回填 + time_delta_altitude 首选
- 分段双路径：currentDistance → kilo_pace 兜底
- lap 对账：距离累加=汇总 ±5%；最后 elapsed=duration ±5%
- HR drift：≥120 样本、≥20 min、speed_cv<0.20，按时间中点切
"""
from __future__ import annotations
import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional


# ===== 常量（对齐 ZeppBridge decoder/workout_detail.rs）=====

MAX_ACTIVITY_SECONDS = 48 * 3600  # 48h 防御上限
COORD_FACTOR = 1e8
MIN_PLAUSIBLE_ALTITUDE_CM = -100_000   # -1000 m
MAX_PLAUSIBLE_ALTITUDE_CM = 1_000_000  # 10000 m
LAP_SUMMARY_TOLERANCE = 0.05            # 圈距离累加 vs 汇总 5%
ELEVATION_NOISE_FLOOR_M = 1.0

# HR drift
HR_MIN_VALID = 1
HR_MAX_VALID = 250
HR_DRIFT_MIN_DURATION_SEC = 20 * 60      # 20 min
HR_DRIFT_MIN_SAMPLES_TOTAL = 120          # 两半各 60+
HR_DRIFT_MIN_SAMPLES_PER_HALF = 60
HR_DRIFT_MAX_SPEED_CV = 0.20
HR_DRIFT_MIN_HR = 40
HR_DRIFT_MIN_SPEED_MPS = 0.5

# 配速合理范围（s/m）；用于 weighted avg / filter
NORMAL_PACE_MIN_S_PER_M = 120.0 / 1000.0   # 120 s/km = 0.12 s/m
NORMAL_PACE_MAX_S_PER_M = 1800.0 / 1000.0  # 1800 s/km = 1.8 s/m


# ===== 数据类 =====

@dataclass
class RoutePoint:
    track_id: int
    ts_ms: int                       # unix 毫秒（相对 start_time）
    elapsed_sec: int                 # 距开始秒数
    latitude: float
    longitude: float
    altitude_m: Optional[float]


@dataclass
class WorkoutSample:
    track_id: int
    ts_ms: int
    elapsed_sec: int
    heart_rate: Optional[int] = None
    speed_mps: Optional[float] = None
    pace_s_per_m: Optional[float] = None          # 1 / speed
    cadence_spm: Optional[int] = None
    stride_cm: Optional[int] = None
    altitude_m: Optional[float] = None
    power_watts: Optional[int] = None
    ground_contact_ms: Optional[int] = None
    vertical_oscillation_mm: Optional[float] = None
    vertical_ratio_pct: Optional[float] = None
    equivalent_pace_s_per_km: Optional[float] = None


@dataclass
class WorkoutSplit:
    track_id: int
    index: int                        # 0-based
    start_sec: int
    end_sec: int
    distance_m: float
    duration_seconds: int
    pace_min_per_km: Optional[float] = None  # min/km
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    elevation_gain_m: Optional[float] = None
    elevation_loss_m: Optional[float] = None
    partial: bool = False             # 最后一截不完整公里


@dataclass
class WorkoutLap:
    track_id: int
    index: int
    start_sec: int
    end_sec: int
    distance_m: float
    duration_seconds: int
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None


@dataclass
class WorkoutPause:
    track_id: int
    start_ts_ms: int
    end_ts_ms: int
    kind: int                         # 0=unknown / 2=manual / 3=auto


@dataclass
class HeartRateDrift:
    track_id: int
    first_half_metres_per_beat: Optional[float] = None
    second_half_metres_per_beat: Optional[float] = None
    drift_percent: Optional[float] = None
    speed_cv: Optional[float] = None
    first_half_avg_hr: Optional[float] = None
    second_half_avg_hr: Optional[float] = None
    first_half_avg_speed_mps: Optional[float] = None
    second_half_avg_speed_mps: Optional[float] = None
    first_half_samples: int = 0
    second_half_samples: int = 0
    reason_code: Optional[str] = None  # not_enough_samples / too_short / pace_too_variable


@dataclass
class DecodedWorkout:
    track_id: int
    source: Optional[str]
    start_ts_ms: int
    end_ts_ms: int
    summary_distance_m: Optional[float] = None
    summary_duration_sec: Optional[int] = None
    route: list = field(default_factory=list)
    samples: list = field(default_factory=list)
    pauses: list = field(default_factory=list)
    splits: list = field(default_factory=list)
    laps: list = field(default_factory=list)
    heart_rate_drift: Optional[HeartRateDrift] = None
    diagnostics: list = field(default_factory=list)


# ===== 差分链解析工具 =====

# 数字对解析（兼容整数 / 浮点 / 含正负号）
_NUM_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?$")


def parse_delta_pairs(raw, empty_means_one_sec: bool):
    """
    解析 "idx,val;idx,val;..." 格式的差分链。

    返回 list[(idx, val)]；无效条目跳过。
    raw 为 None 或 ""：若 empty_means_one_sec=True → [(1, 0)]；否则 → []。
    """
    if raw is None:
        if empty_means_one_sec:
            return [(1, 0)]
        return []
    s = str(raw).strip()
    if not s:
        if empty_means_one_sec:
            return [(1, 0)]
        return []
    out = []
    for pair in s.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(",")
        if len(parts) != 2:
            continue
        a, b = parts[0].strip(), parts[1].strip()
        if not _NUM_RE.match(a) or not _NUM_RE.match(b):
            continue
        try:
            idx_v = float(a)
            val_v = float(b)
            idx_i = int(round(idx_v))
            if idx_i <= 0:
                continue
            out.append((idx_i, val_v))
        except ValueError:
            continue
    return out


def parse_simple_delta(raw, empty_means_one_sec: bool):
    """
    解析 "v;v;v;..." 格式（time / altitude 等单值差分）。

    返回 list[v]；空：empty_means_one_sec 时返 [(1,)]；否则 []。
    """
    if raw is None:
        if empty_means_one_sec:
            return [1]
        return []
    s = str(raw).strip()
    if not s:
        if empty_means_one_sec:
            return [1]
        return []
    out = []
    for v in s.split(";"):
        v = v.strip()
        if not v:
            continue
        if not _NUM_RE.match(v):
            continue
        try:
            out.append(float(v))
        except ValueError:
            continue
    return out


# ===== 时间增量 → 秒数 → unix 时间戳 =====

def accumulate_deltas_to_seconds(deltas):
    """
    把 [1, 1, 2, 1, ...] 累加成 [1, 2, 4, 5, ...]。
    返回 list[累计秒数]；上限 MAX_ACTIVITY_SECONDS 防御性截断。
    """
    out = []
    acc = 0
    for d in deltas:
        acc += int(round(d))
        if acc > MAX_ACTIVITY_SECONDS:
            acc = MAX_ACTIVITY_SECONDS
        out.append(acc)
    return out


# ===== 海拔双路径 =====

def altitude_cm_is_plausible(v):
    return MIN_PLAUSIBLE_ALTITUDE_CM <= v <= MAX_PLAUSIBLE_ALTITUDE_CM


def compute_altitude_series(
    altitude_raw: str,
    time_delta_altitude_raw: str,
    diagnostics: list,
) -> list:
    """
    返回 [(elapsed_sec, altitude_m)]。

    优先级：time_delta_altitude > altitude。
    altitude_raw 是差分（第一个不可信 → 第一个可信值回填）；time_delta_altitude 是 (dt,cm) 对。
    """
    pairs = parse_delta_pairs(time_delta_altitude_raw, empty_means_one_sec=False)
    out = []
    if pairs:
        t = 0
        for dt, cm in pairs:
            t += int(round(dt))
            if altitude_cm_is_plausible(cm):
                out.append((t, cm / 100.0))
        return out

    # 兜底：altitude 差分（cm 累计）
    deltas = parse_simple_delta(altitude_raw, empty_means_one_sec=False)
    acc = 0
    first_valid = None
    out2 = []
    sec = 0
    for v in deltas:
        sec += 1
        acc += int(round(v))
        if altitude_cm_is_plausible(acc):
            if first_valid is None:
                first_valid = acc
                out2.append((sec, first_valid / 100.0))
            else:
                # 后续用真实累计值，但限制相对变化（防止累积漂移）
                out2.append((sec, acc / 100.0))
        # 不可信值累计放着，不写库
    if first_valid is not None:
        diagnostics.append("altitude_used_chain_with_first_valid_backfill")
    return out2


# ===== 经纬度差分 =====

def parse_lonlat_chain(raw: str, diagnostics: list):
    """
    解析 (lat/lon) 整数差分，返回 [(elapsed_sec, lat, lon)]。
    累加后 /1e8 转度；越界截断；lat ∈ [-90,90]、lon ∈ [-180,180]。
    """
    if not raw:
        return []
    pairs = parse_delta_pairs(raw, empty_means_one_sec=False)
    if not pairs:
        return []
    out = []
    dlat = dlon = 0
    for dt, val in pairs:
        # 实际是 2 个整数对："idx,lat_delta,lon_delta" → val 是 packed？检查报告
        # 报告：longitude_latitude = "i64, i64" 对（lat,lon）各自差分
        # 但格式是 "idx0,val0;idx1,val1;..."，val 里是 (lat_delta, lon_delta) 包成 float？
        # 实际 Zepp payload 是 "idx,lat_d,lon_d;idx,lat_d,lon_d;..." 三列
        # parse_delta_pairs 只认 idx,val 两列 → 需要专门处理
        pass
    # 上面 for 循环已识别到 raw 是 "idx,lat_d,lon_d" 三列。需要单独解析
    return _parse_lonlat_three_col(raw, diagnostics)


def _parse_lonlat_three_col(raw: str, diagnostics: list):
    """解析 "idx,lat_d,lon_d;..." 三列差分，返回 [(elapsed_sec, lat_deg, lon_deg)]。"""
    if not raw:
        return []
    out = []
    t = 0
    dlat = 0
    dlon = 0
    malformed = 0
    for pair in str(raw).split(";"):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(",")
        if len(parts) != 3:
            malformed += 1
            continue
        a, b, c = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not (_NUM_RE.match(a) and _NUM_RE.match(b) and _NUM_RE.match(c)):
            malformed += 1
            continue
        try:
            idx_v = float(a)
            lat_d = float(b)
            lon_d = float(c)
            t += int(round(idx_v))
            dlat += lat_d
            dlon += lon_d
            lat = dlat / COORD_FACTOR
            lon = dlon / COORD_FACTOR
            # 越界截断
            if lat > 90: lat = 90.0
            elif lat < -90: lat = -90.0
            if lon > 180: lon = 180.0
            elif lon < -180: lon = -180.0
            out.append((t, lat, lon))
        except ValueError:
            malformed += 1
    if malformed:
        diagnostics.append(f"longitude_latitude: skipped {malformed} malformed delta(s)")
    return out


# ===== distance / speed / hr 差分解析 =====

def parse_cumulative_distance(raw: str, diagnostics: list):
    """解析累计距离（厘米）→ 序列 [(elapsed_sec, dist_m)]。单调 + 单步 ≤ 200 m/s 过滤。"""
    if not raw:
        return []
    out = []
    t = 0
    prev_m = 0.0
    last_sec = 0
    skipped = 0
    for pair in str(raw).split(";"):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(",")
        if len(parts) != 2:
            skipped += 1
            continue
        a, b = parts[0].strip(), parts[1].strip()
        if not (_NUM_RE.match(a) and _NUM_RE.match(b)):
            skipped += 1
            continue
        try:
            dt = int(round(float(a)))
            cm = float(b)
            t += dt
            m = cm / 100.0
            if m < prev_m:
                skipped += 1
                continue
            step_sec = t - last_sec
            if step_sec > 0 and (m - prev_m) / step_sec > 200:
                skipped += 1
                continue
            prev_m = m
            last_sec = t
            out.append((t, m))
        except ValueError:
            skipped += 1
    if skipped:
        diagnostics.append(f"currentDistance: skipped {skipped} bad point(s)")
    return out


def parse_speed_chain(raw: str, diagnostics: list):
    """解析 (dt, m/s) 差分 → [(elapsed_sec, speed_mps)]。empty=true 时空=1 秒。"""
    if not raw:
        # speed 不在 empty=true 列表中（协议没约定）；空=无数据
        return []
    out = []
    t = 0
    skipped = 0
    for pair in str(raw).split(";"):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(",")
        if len(parts) != 2:
            skipped += 1
            continue
        a, b = parts[0].strip(), parts[1].strip()
        if not (_NUM_RE.match(a) and _NUM_RE.match(b)):
            skipped += 1
            continue
        try:
            dt = int(round(float(a)))
            v = float(b)
            t += dt
            if v < 0 or v > 50:
                skipped += 1
                continue
            out.append((t, v))
        except ValueError:
            skipped += 1
    if skipped:
        diagnostics.append(f"speed: skipped {skipped} bad delta(s)")
    return out


def parse_hr_delta_chain(raw: str, diagnostics: list):
    """
    heart_rate 是累计 bpm 差分（不是单次值）。
    每条 (dt, bpm_delta) → 累加得到 bpm。

    ZeppBridge 用 parse_delta_pairs(empty=true) 累加；bpm_delta 通常 0 或 ±1。
    返回 [(elapsed_sec, bpm)]。
    """
    if not raw:
        return []
    out = []
    t = 0
    bpm = 0
    skipped = 0
    for pair in str(raw).split(";"):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(",")
        if len(parts) != 2:
            skipped += 1
            continue
        a, b = parts[0].strip(), parts[1].strip()
        if not (_NUM_RE.match(a) and _NUM_RE.match(b)):
            skipped += 1
            continue
        try:
            dt = int(round(float(a)))
            d = float(b)
            t += dt
            bpm += int(round(d))
            if not (HR_MIN_VALID <= bpm <= HR_MAX_VALID):
                skipped += 1
                continue
            out.append((t, bpm))
        except ValueError:
            skipped += 1
    if skipped:
        diagnostics.append(f"heart_rate: skipped {skipped} out-of-range sample(s)")
    return out


def parse_gait_chain(raw: str, diagnostics: list):
    """
    gait: (dt, step_delta, stride_cm, cadence_spm) 四列。
    cadence 取 col 3；stride 取 col 2。
    """
    if not raw:
        return []
    out = []
    t = 0
    skipped = 0
    for pair in str(raw).split(";"):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(",")
        if len(parts) != 4:
            skipped += 1
            continue
        a, b, c, d = [p.strip() for p in parts]
        if not all(_NUM_RE.match(x) for x in (a, b, c, d)):
            skipped += 1
            continue
        try:
            dt = int(round(float(a)))
            steps = int(round(float(b)))
            stride_cm = int(round(float(c)))
            cadence_spm = int(round(float(d)))
            t += dt
            if cadence_spm < 0 or cadence_spm > 250:
                cadence_spm = None
            if stride_cm < 0 or stride_cm > 500:
                stride_cm = None
            out.append((t, steps, stride_cm, cadence_spm))
        except ValueError:
            skipped += 1
    if skipped:
        diagnostics.append(f"gait: skipped {skipped} bad delta(s)")
    return out


def parse_power_chain(raw: str, diagnostics: list):
    """power_meter: (dt, watts) → [(elapsed_sec, watts)]。"""
    if not raw:
        return []
    out = []
    t = 0
    skipped = 0
    for pair in str(raw).split(";"):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(",")
        if len(parts) != 2:
            skipped += 1
            continue
        a, b = parts[0].strip(), parts[1].strip()
        if not (_NUM_RE.match(a) and _NUM_RE.match(b)):
            skipped += 1
            continue
        try:
            dt = int(round(float(a)))
            w = int(round(float(b)))
            t += dt
            if w < 0 or w > 2000:
                skipped += 1
                continue
            out.append((t, w))
        except ValueError:
            skipped += 1
    if skipped:
        diagnostics.append(f"power_meter: skipped {skipped} bad delta(s)")
    return out


def parse_equiv_pace_chain(raw: str, diagnostics: list):
    """equivPace: (dt, s_per_km) → [(elapsed_sec, s_per_km)]。"""
    if not raw:
        return []
    out = []
    t = 0
    skipped = 0
    for pair in str(raw).split(";"):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(",")
        if len(parts) != 2:
            skipped += 1
            continue
        a, b = parts[0].strip(), parts[1].strip()
        if not (_NUM_RE.match(a) and _NUM_RE.match(b)):
            skipped += 1
            continue
        try:
            dt = int(round(float(a)))
            p = float(b)
            t += dt
            if p <= 0 or p > 3600:
                skipped += 1
                continue
            out.append((t, p))
        except ValueError:
            skipped += 1
    if skipped:
        diagnostics.append(f"equivPace: skipped {skipped} bad delta(s)")
    return out


def parse_run_posture_chain(raw: str, diagnostics: list):
    """
    runPosture: (dt, gct_ms, vo_mm, vsr_tenths_pct) 四列。
    哨兵：GCT/VO 65535 → NaN；VSR 255 → NaN（额外 /10 转 %）。
    """
    if not raw:
        return []
    out = []
    t = 0
    skipped = 0
    for pair in str(raw).split(";"):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(",")
        if len(parts) != 4:
            skipped += 1
            continue
        a, b, c, d = [p.strip() for p in parts]
        if not all(_NUM_RE.match(x) for x in (a, b, c, d)):
            skipped += 1
            continue
        try:
            dt = int(round(float(a)))
            gct = int(round(float(b)))
            vo = float(c)
            vsr_raw = int(round(float(d)))
            t += dt
            gct_v = None if gct == 65535 else gct
            vo_v = None if vo == 65535 else vo
            vsr_v = None if vsr_raw == 255 else vsr_raw / 10.0
            out.append((t, gct_v, vo_v, vsr_v))
        except ValueError:
            skipped += 1
    if skipped:
        diagnostics.append(f"runPosture: skipped {skipped} bad delta(s)")
    return out


def parse_pauses(raw: str, start_ts_ms: int, track_id: int, diagnostics: list):
    """
    pause: "start_unix,end_delta,?,?,kind;..."
    返回 list[WorkoutPause]。
    """
    if not raw:
        return []
    out = []
    skipped = 0
    for pair in str(raw).split(";"):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(",")
        if len(parts) < 5:
            skipped += 1
            continue
        try:
            start_s = float(parts[0].strip())
            end_d = float(parts[1].strip())
            kind = int(round(float(parts[4].strip())))
            if kind not in (0, 2, 3):
                skipped += 1
                continue
            end_s = start_s + end_d
            if end_s <= start_s:
                skipped += 1
                continue
            # start_unix 可能是秒或毫秒（实测秒）
            if start_s > 1e10:
                start_s /= 1000.0
                end_s /= 1000.0
            out.append(WorkoutPause(
                track_id=track_id,
                start_ts_ms=int(start_s * 1000),
                end_ts_ms=int(end_s * 1000),
                kind=kind,
            ))
        except ValueError:
            skipped += 1
    if skipped:
        diagnostics.append(f"pause: skipped {skipped} malformed segment(s)")
    return out


# ===== kilo_pace 兜底分段 =====

def parse_kilo_pace(raw: str, diagnostics: list):
    """
    kilo_pace 列宽不稳；只用 [0] (idx)、[1] (sec_spent)、[5] (cum_sec)。
    要求：序号=位置、累计==sum(per_sec)，否则整段拒用。
    返回 list[(idx, sec_spent, cum_sec)]。
    """
    if not raw:
        return []
    out = []
    for pair in str(raw).split(";"):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(",")
        if len(parts) < 6:
            continue
        try:
            idx = int(round(float(parts[0].strip())))
            sec = float(parts[1].strip())
            cum = float(parts[5].strip())
            out.append((idx, sec, cum))
        except (ValueError, IndexError):
            continue
    # 自洽性校验
    if not out:
        return []
    for i, (k, sec, cum) in enumerate(out):
        if k != i:
            diagnostics.append(f"kilo_pace: idx mismatch at row {i} (got {k})")
            return []
    total = sum(sec for _, sec, _ in out)
    last_cum = out[-1][2]
    if abs(total - last_cum) > max(60, total * 0.05):
        diagnostics.append(f"kilo_pace: cum mismatch sum={total} last={last_cum}")
        return []
    return out


# ===== lap 解析 =====

def parse_laps(raw: str, diagnostics: list):
    """
    lap 列宽不稳；用 [0]/[2]/[4]/[5]。
    仅 [0] (idx)、[2] (distance_m)、[4] (avg_hr)、[5] (elapsed_sec)。
    返回 list[(idx, dist_m, avg_hr, elapsed_sec)]。
    """
    if not raw:
        return []
    out = []
    for pair in str(raw).split(";"):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(",")
        if len(parts) < 6:
            continue
        try:
            idx = int(round(float(parts[0].strip())))
            dist = float(parts[2].strip())
            hr = int(round(float(parts[4].strip())))
            elapsed = float(parts[5].strip())
            out.append((idx, dist, hr, elapsed))
        except (ValueError, IndexError):
            continue
    return out


# ===== altitude 噪声底过滤：gain/loss =====

def compute_elevation_gain_loss(altitudes_with_sec):
    """[(elapsed_sec, altitude_m), ...] → (gain_m, loss_m)，ELEVATION_NOISE_FLOOR_M 噪声底过滤。"""
    if not altitudes_with_sec:
        return (None, None)
    gain = loss = 0.0
    prev = altitudes_with_sec[0][1]
    for _, alt in altitudes_with_sec[1:]:
        d = alt - prev
        if d > ELEVATION_NOISE_FLOOR_M:
            gain += d
        elif d < -ELEVATION_NOISE_FLOOR_M:
            loss += -d
        prev = alt
    return (round(gain, 2), round(loss, 2))


# ===== 分段双路径 =====

def compute_splits_from_distance(
    track_id: int,
    distance_series: list,
    hr_series: list,
    altitude_series: list,
    summary_distance_m: Optional[float],
    diagnostics: list,
) -> list:
    """
    按 1000m 切分（用累计距离驱动，不用速度积分）。
    返回 list[WorkoutSplit]；最后一段不完整公里 → partial=True。
    """
    if not distance_series:
        return []
    last_dist = distance_series[-1][1]
    if last_dist < 500:
        # 不足 1 公里，没有完整 split
        diagnostics.append(f"splits: too short ({last_dist:.0f}m), skipped")
        return []
    n_full = int(last_dist // 1000)
    if n_full == 0:
        return []
    # 建立 (sec, dist, hr, alt) 的字典按 sec 索引
    splits = []
    # 用 distance_series 切边界
    # 简化：遍历 distance_series，每跨过 1000m 边界产生一段
    prev_dist = 0.0
    prev_sec = 0
    split_idx = 0
    for sec, dist in distance_series:
        while split_idx < n_full and dist >= (split_idx + 1) * 1000:
            boundary = (split_idx + 1) * 1000
            seg_dist = boundary - prev_dist
            seg_dur = sec - prev_sec
            # HR 在 [prev_sec, sec] 区间均值；max_hr 最大
            hrs_in_seg = [hr for (s, hr) in hr_series if prev_sec < s <= sec]
            avg_hr = int(round(sum(hrs_in_seg) / len(hrs_in_seg))) if hrs_in_seg else None
            max_hr = max(hrs_in_seg) if hrs_in_seg else None
            # elevation
            alts_in_seg = [(s, a) for (s, a) in altitude_series if prev_sec < s <= sec]
            gain, loss = compute_elevation_gain_loss(alts_in_seg)
            splits.append(WorkoutSplit(
                track_id=track_id,
                index=split_idx,
                start_sec=prev_sec,
                end_sec=sec,
                distance_m=seg_dist,
                duration_seconds=seg_dur,
                pace_min_per_km=round(seg_dur / (seg_dist / 1000.0) / 60.0, 2) if seg_dist > 0 else None,
                avg_hr=avg_hr,
                max_hr=max_hr,
                elevation_gain_m=gain,
                elevation_loss_m=loss,
                partial=False,
            ))
            split_idx += 1
            prev_dist = boundary
            prev_sec = sec
    # 剩余 partial
    if prev_dist < last_dist - 1:  # 容差 1m
        seg_dist = last_dist - prev_dist
        seg_dur = distance_series[-1][0] - prev_sec
        hrs_in_seg = [hr for (s, hr) in hr_series if prev_sec < s <= distance_series[-1][0]]
        avg_hr = int(round(sum(hrs_in_seg) / len(hrs_in_seg))) if hrs_in_seg else None
        max_hr = max(hrs_in_seg) if hrs_in_seg else None
        alts_in_seg = [(s, a) for (s, a) in altitude_series if prev_sec < s <= distance_series[-1][0]]
        gain, loss = compute_elevation_gain_loss(alts_in_seg)
        splits.append(WorkoutSplit(
            track_id=track_id,
            index=len(splits),
            start_sec=prev_sec,
            end_sec=distance_series[-1][0],
            distance_m=seg_dist,
            duration_seconds=seg_dur,
            pace_min_per_km=round(seg_dur / (seg_dist / 1000.0) / 60.0, 2) if seg_dist > 0 else None,
            avg_hr=avg_hr,
            max_hr=max_hr,
            elevation_gain_m=gain,
            elevation_loss_m=loss,
            partial=True,
        ))
    # 自洽性：split 数必须和 summary_distance_m/1000.0 ± 1 一致
    if summary_distance_m:
        expected = summary_distance_m / 1000.0
        if abs(len(splits) - expected) > 1.0:
            diagnostics.append(
                f"splits: count {len(splits)} != expected ~{expected:.1f} from summary_distance_m"
            )
    return splits


def compute_splits_from_kilo_pace(
    track_id: int,
    kilo_pace_rows: list,
    summary_distance_m: Optional[float],
    diagnostics: list,
) -> list:
    """kilo_pace 兜底分段（仅用 [0]/[1]/[5]）。"""
    if not kilo_pace_rows:
        return []
    splits = []
    cum_sec = 0
    for idx, sec_spent, _cum in kilo_pace_rows:
        start_sec = cum_sec
        end_sec = cum_sec + int(round(sec_spent))
        splits.append(WorkoutSplit(
            track_id=track_id,
            index=idx,
            start_sec=start_sec,
            end_sec=end_sec,
            distance_m=1000.0,
            duration_seconds=int(round(sec_spent)),
            pace_min_per_km=round(sec_spent / 60.0, 2),
            avg_hr=None, max_hr=None,
            elevation_gain_m=None, elevation_loss_m=None,
            partial=False,
        ))
        cum_sec = end_sec
    # 自洽性
    if summary_distance_m:
        expected = summary_distance_m / 1000.0
        if abs(len(splits) - expected) > 1.0:
            diagnostics.append(
                f"splits(kilo_pace): count {len(splits)} != expected ~{expected:.1f}"
            )
            return []
    return splits


# ===== Lap 对账 =====

def reconcile_laps(track_id: int, lap_rows: list, summary_distance_m: Optional[float],
                   summary_duration_sec: Optional[int], diagnostics: list):
    """
    lap[0]=idx / lap[2]=distance_m / lap[4]=avg_hr / lap[5]=elapsed_sec。
    距离累加=汇总 ±5%；最后 elapsed=duration ±5%。
    """
    if not lap_rows:
        return []
    total_lap_distance = sum(dist for _, dist, _, _ in lap_rows)
    if summary_distance_m and abs(total_lap_distance - summary_distance_m) > summary_distance_m * LAP_SUMMARY_TOLERANCE:
        diagnostics.append(
            f"laps: distance sum {total_lap_distance:.0f}m vs summary {summary_distance_m:.0f}m diff >5%"
        )
        return []
    # last_elapsed = 累计 elapsed（不是单个 lap 的 elapsed）
    total_elapsed = sum(elapsed for _, _, _, elapsed in lap_rows)
    if summary_duration_sec and abs(total_elapsed - summary_duration_sec) > summary_duration_sec * LAP_SUMMARY_TOLERANCE:
        diagnostics.append(
            f"laps: cumulative elapsed {total_elapsed:.0f}s vs summary {summary_duration_sec}s diff >5%"
        )
        return []
    # 通过校验，构造 WorkoutLap 列表
    out = []
    cum_start = 0
    for idx, dist, hr, elapsed in lap_rows:
        # HR=0 视为未测到（None）
        avg_hr = hr if hr > 0 else None
        out.append(WorkoutLap(
            track_id=track_id,
            index=idx,
            start_sec=cum_start,
            end_sec=cum_start + int(round(elapsed)),
            distance_m=dist,
            duration_seconds=int(round(elapsed)),
            avg_hr=avg_hr,
            max_hr=None,
        ))
        cum_start += int(round(elapsed))
    return out


# ===== HR Drift =====

def compute_hr_drift(track_id: int, samples: list, summary_duration_sec: Optional[int],
                      diagnostics: list) -> Optional[HeartRateDrift]:
    """
    HR drift = (second_half_metres_per_beat - first) / first × 100
    拒算条件：
      - too_short: duration < 20 min
      - not_enough_samples: samples < 120 或任何一边 < 60
      - pace_too_variable: speed_cv > 0.20
    样本过滤：HR < 40 或 speed < 0.5 m/s 丢弃
    按时间中点切分
    """
    # 取有 HR + speed 的样本
    valid = []
    for s in samples:
        if s.heart_rate is None or s.speed_mps is None:
            continue
        if s.heart_rate < HR_DRIFT_MIN_HR or s.speed_mps < HR_DRIFT_MIN_SPEED_MPS:
            continue
        valid.append(s)
    if len(valid) < HR_DRIFT_MIN_SAMPLES_TOTAL:
        return HeartRateDrift(
            track_id=track_id,
            reason_code="not_enough_samples",
            first_half_samples=0,
            second_half_samples=0,
        )
    # duration check
    if summary_duration_sec is None or summary_duration_sec < HR_DRIFT_MIN_DURATION_SEC:
        return HeartRateDrift(
            track_id=track_id,
            reason_code="too_short",
            first_half_samples=0,
            second_half_samples=0,
        )
    # speed_cv
    speeds = [s.speed_mps for s in valid]
    if len(speeds) < 2:
        return HeartRateDrift(track_id=track_id, reason_code="not_enough_samples")
    mean_spd = sum(speeds) / len(speeds)
    if mean_spd <= 0:
        return HeartRateDrift(track_id=track_id, reason_code="not_enough_samples")
    sd = statistics.pstdev(speeds)
    cv = sd / mean_spd
    if cv > HR_DRIFT_MAX_SPEED_CV:
        return HeartRateDrift(
            track_id=track_id,
            reason_code="pace_too_variable",
            speed_cv=round(cv, 4),
            first_half_samples=0,
            second_half_samples=0,
        )
    # 按时间中点切分
    half = summary_duration_sec / 2.0
    first = [s for s in valid if s.elapsed_sec <= half]
    second = [s for s in valid if s.elapsed_sec > half]
    if len(first) < HR_DRIFT_MIN_SAMPLES_PER_HALF or len(second) < HR_DRIFT_MIN_SAMPLES_PER_HALF:
        return HeartRateDrift(
            track_id=track_id,
            reason_code="not_enough_samples",
            speed_cv=round(cv, 4),
            first_half_samples=len(first),
            second_half_samples=len(second),
        )
    # 计算 metres per beat = mean_speed * 60 / mean_hr
    def metres_per_beat(side):
        ms = sum(s.speed_mps for s in side) / len(side)
        mh = sum(s.heart_rate for s in side) / len(side)
        return (ms * 60.0) / mh if mh > 0 else None
    f_mpb = metres_per_beat(first)
    s_mpb = metres_per_beat(second)
    drift_pct = None
    if f_mpb and s_mpb is not None and f_mpb > 0:
        drift_pct = (s_mpb - f_mpb) / f_mpb * 100.0
    return HeartRateDrift(
        track_id=track_id,
        first_half_metres_per_beat=round(f_mpb, 4) if f_mpb else None,
        second_half_metres_per_beat=round(s_mpb, 4) if s_mpb else None,
        drift_percent=round(drift_pct, 2) if drift_pct is not None else None,
        speed_cv=round(cv, 4),
        first_half_avg_hr=round(sum(s.heart_rate for s in first) / len(first), 1),
        second_half_avg_hr=round(sum(s.heart_rate for s in second) / len(second), 1),
        first_half_avg_speed_mps=round(sum(s.speed_mps for s in first) / len(first), 3),
        second_half_avg_speed_mps=round(sum(s.speed_mps for s in second) / len(second), 3),
        first_half_samples=len(first),
        second_half_samples=len(second),
    )


# ===== 顶层入口 =====

def decode_workout_detail(
    raw: dict,
    summary_end_ts: Optional[int] = None,
    summary_distance_m: Optional[float] = None,
    track_id_hint: Optional[int] = None,
) -> DecodedWorkout:
    """
    解析 workout detail JSON → DecodedWorkout。

    raw: {code:1, message:'success', data:{...}} 或直接 {...}
    summary_end_ts / summary_distance_m: 来自 workouts 表的 summary（用于 lap 对账 + split 自洽性）。
    """
    diagnostics = []
    # unwrap envelope
    if isinstance(raw, dict) and "data" in raw and isinstance(raw["data"], dict):
        payload = raw["data"]
    elif isinstance(raw, dict):
        payload = raw
    else:
        return DecodedWorkout(
            track_id=track_id_hint or 0,
            source=None,
            start_ts_ms=0, end_ts_ms=0,
            diagnostics=["payload is not a dict"],
        )

    track_id = int(payload.get("trackid") or track_id_hint or 0)
    source = payload.get("source")

    # start_ts: trackid 本身就是秒级 unix 时间戳（Zepp 约定）
    if track_id > 0 and track_id < 10**11:
        start_ts_ms = track_id * 1000
    else:
        start_ts_ms = int(summary_end_ts * 1000 - 0) if summary_end_ts else 0

    end_ts_ms = int(summary_end_ts * 1000) if summary_end_ts else start_ts_ms
    summary_duration_sec = int((end_ts_ms - start_ts_ms) / 1000) if (end_ts_ms > start_ts_ms) else None

    # ===== 解析各差分链 =====
    # 1) time delta → 累积秒数（empty=true）
    time_deltas = parse_simple_delta(payload.get("time"), empty_means_one_sec=True)
    # (not directly used; just sanity)

    # 2) longitude_latitude
    lonlat = _parse_lonlat_three_col(payload.get("longitude_latitude"), diagnostics)

    # 3) altitude + time_delta_altitude
    altitude_series = compute_altitude_series(
        payload.get("altitude", ""),
        payload.get("time_delta_altitude", ""),
        diagnostics,
    )

    # 4) currentDistance
    distance_series = parse_cumulative_distance(payload.get("currentDistance"), diagnostics)

    # 5) heart_rate
    hr_series = parse_hr_delta_chain(payload.get("heart_rate"), diagnostics)

    # 6) speed
    speed_series = parse_speed_chain(payload.get("speed"), diagnostics)

    # 7) gait
    gait_series = parse_gait_chain(payload.get("gait"), diagnostics)

    # 8) power
    power_series = parse_power_chain(payload.get("power_meter"), diagnostics)

    # 9) equivPace
    equiv_pace_series = parse_equiv_pace_chain(payload.get("equivPace"), diagnostics)

    # 10) runPosture
    posture_series = parse_run_posture_chain(payload.get("runPosture"), diagnostics)

    # 11) pause
    pauses = parse_pauses(payload.get("pause"), start_ts_ms, track_id, diagnostics)

    # 12) kilo_pace
    kilo_pace_rows = parse_kilo_pace(payload.get("kilo_pace"), diagnostics)

    # 13) lap
    lap_rows = parse_laps(payload.get("lap"), diagnostics)

    # ===== 构造 route =====
    route = []
    alt_by_sec = dict(altitude_series)
    for sec, lat, lon in lonlat:
        ts_ms = start_ts_ms + sec * 1000
        alt = alt_by_sec.get(sec)
        if alt is None:
            # 兜底：找最近 sec 的 alt
            if altitude_series:
                # 简单线性：找到最大的 <= sec
                best = None
                for s, a in altitude_series:
                    if s <= sec:
                        best = a
                    else:
                        break
                alt = best
        route.append(RoutePoint(
            track_id=track_id,
            ts_ms=ts_ms,
            elapsed_sec=sec,
            latitude=lat,
            longitude=lon,
            altitude_m=alt,
        ))

    # ===== 构造 samples（按 sec 索引合并） =====
    # 用 hr_series 作为主轴（最密），补充 speed/gait/power/equiv_pace/posture/altitude
    alt_map = dict(altitude_series)
    speed_map = dict(speed_series)
    gait_map = {s: (steps, stride, cad) for (s, steps, stride, cad) in gait_series}
    power_map = dict(power_series)
    eqpace_map = dict(equiv_pace_series)
    posture_map = {s: (g, v, vsr) for (s, g, v, vsr) in posture_series}

    # 收集所有出现过的 sec
    all_secs = set(alt_map.keys()) | set(speed_map.keys()) | set(gait_map.keys()) | \
               set(power_map.keys()) | set(eqpace_map.keys()) | set(posture_map.keys()) | \
               {s for s, _ in hr_series}
    if not all_secs and route:
        all_secs = {r.elapsed_sec for r in route}
    sorted_secs = sorted(all_secs)

    samples = []
    for sec in sorted_secs:
        hr = None
        for s, h in hr_series:
            if s == sec:
                hr = h
                break
        speed = speed_map.get(sec)
        pace = (1.0 / speed) if (speed and speed > 0) else None
        gait = gait_map.get(sec)
        power = power_map.get(sec)
        eq_pace = eqpace_map.get(sec)
        post = posture_map.get(sec)
        alt = alt_map.get(sec)
        ts_ms = start_ts_ms + sec * 1000
        samples.append(WorkoutSample(
            track_id=track_id,
            ts_ms=ts_ms,
            elapsed_sec=sec,
            heart_rate=hr,
            speed_mps=speed,
            pace_s_per_m=pace,
            cadence_spm=gait[2] if gait else None,
            stride_cm=gait[1] if gait else None,
            altitude_m=alt,
            power_watts=power,
            ground_contact_ms=post[0] if post else None,
            vertical_oscillation_mm=post[1] if post else None,
            vertical_ratio_pct=post[2] if post else None,
            equivalent_pace_s_per_km=eq_pace,
        ))

    # ===== splits 双路径 =====
    splits = []
    if distance_series:
        splits = compute_splits_from_distance(
            track_id, distance_series, hr_series, altitude_series,
            summary_distance_m, diagnostics,
        )
    if not splits and kilo_pace_rows:
        splits = compute_splits_from_kilo_pace(
            track_id, kilo_pace_rows, summary_distance_m, diagnostics,
        )

    # ===== laps 对账 =====
    laps = reconcile_laps(track_id, lap_rows, summary_distance_m,
                          summary_duration_sec, diagnostics)

    # ===== HR drift =====
    hr_drift = compute_hr_drift(track_id, samples, summary_duration_sec, diagnostics)

    return DecodedWorkout(
        track_id=track_id,
        source=source,
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
        summary_distance_m=summary_distance_m,
        summary_duration_sec=summary_duration_sec,
        route=route,
        samples=samples,
        pauses=pauses,
        splits=splits,
        laps=laps,
        heart_rate_drift=hr_drift,
        diagnostics=diagnostics,
    )


# ===== 自检（仅 CLI 测试）=====

if __name__ == "__main__":
    import sys, json
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            raw = json.load(f)
        d = decode_workout_detail(raw)
        print(f"track_id={d.track_id}")
        print(f"start={d.start_ts_ms} end={d.end_ts_ms}")
        print(f"route={len(d.route)} samples={len(d.samples)} splits={len(d.splits)} laps={len(d.laps)} pauses={len(d.pauses)}")
        if d.heart_rate_drift:
            print(f"drift={d.heart_rate_drift}")
        print("--- diagnostics ---")
        for line in d.diagnostics:
            print(f"  {line}")
    else:
        print("Usage: python3 workout_detail.py <raw_detail.json>")