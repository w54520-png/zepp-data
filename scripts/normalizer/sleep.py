"""
睡眠（band_data）流解析 —— M2 章节 2.1。

端点：GET /v1/data/band_data.json
参数：from_date / to_date（YYYY-MM-DD）/ query_type=summary|detail

响应形态（**两种形态并存**）：

A) summary 形态（云端预聚合）—— 最常见：
   [
     {
       "uid": "...",
       "data_type": 0,
       "date_time": "2026-09-16",      # YYYY-MM-DD（**用户本地日历日**——Zepp 用用户时区）
       "source": 8519936,              # 设备 MAC
       "summary": "<base64 JSON>",     # 解码后含 slp/stp
       "device_id": "...",
       "uuid": "..."
     }
   ]

B) detail 形态（带原始数据）：
   在 A 基础上加 data（gzip 压缩原始数据）+ data_hr（base64 1440 字节）
   data_hr[i] = 该分钟 bpm，255 = 未测（sentinel）

summary B64 解码后结构：
  {
    "v": 6, "goal": 30000, "tz": "28800", "algv": "2.13.15",
    "slp": {
      "stage": [{"start": 1397, "stop": 1412, "mode": 4}, ...],
      "odd_stage": [],
      "st": 1789485420, "ed": 1789502700,    # 秒级，本地日期**前一天** 16:00 → 当天 02:00
      "obt": -44, "ebt": 246,                # 当晚在床 / 上床时间偏移（相对 st）
      "dp": 85, "lt": 166, "wk": 0, "wc": 0,
      "is": 1, "lb": 1, "dt": 37, "rhr": 60, "ss": 44, "to": 0,
      "sleepSource": 8519936, "sleepAlgoVersion": "3.0.0",
      "supNap": true, "supRem": true
    },
    "stp": {...运动数据...}
  }

【关键发现】（2026-09-23 实测 7 天数据）：
  - 7 天只 1 天有真实睡眠 stage（其他天 `stage=[]`，st=ed=当天 16:00 UTC 哨兵）
  - 唯一一天的 stage 时间窗 = slp.st 当天**前一天**的 16:17 + slp.st..ed 共 1h 不到
  - 但 stage[].start/stop 是相对分钟（不是 unix 时间），最小值 1397（=23h17m）→ **st 是数据起始锚点 00:00**
  - data_hr 1440 字节，255 = 未测（不是 0）
  - tz 字段类型是 **字符串** "28800"，但功能上等价 int 秒
  - odd_stage 始终空数组（用户设备没启用 odd 阶段）
  - obt/ebt 是「在床/睡前」offset（分钟，负数 = 入睡前）

【Stage mode 映射】（M2 拍板）：
  5  = deep   (深度睡眠)
  4  = light  (浅睡)
  8  = rem    (快速眼动)
  11 = rem    (某些 Zepp 算法版本用 11 表示 REM——兼容)
  7  = awake  (清醒)
  其他 = unknown  (保留原始 code，但归为 unknown)

【Stage 锚点】（**核心！**）：
  - slp.st 是 **数据起始时间锚点**（0 分钟）
  - 但通常这个锚点是**前一天的本地 16:00**（UTC）→ Asia/Shanghai 24:00 = 前一天最后一刻
  - stage[].start/stop 是**相对锚点的分钟数**
  - 也就是说 `stage_actual_start_dt = slp.st + stage[0].start * 60`
  - 同时也意味着：date_time='2026-09-16' 的 session，实际是 9-15 夜 ~ 9-16 凌晨的睡眠
  - 决策（M2 拍板）：session.start_dt = stage[0].start 对应的时间，归到 date_time 那天（用户感知）
    即 date='2026-09-16', stage_actual_ts = slp.st + start_min * 60

【time_in_bed 计算】（M2 拍板）：
  - 首选 slp.st..slp.ed（直接秒级差）
  - fallback：stage 首尾分钟差（stage[0].start..stage[-1].stop）
  - fallback 2：obt/ebt 字段（offset from st）
  - 全部 None → None（不假造）
  - 注：obt 是 -44（开始前 44 分钟上床），ebt=246（开始后 246 分钟离床）
  - 但 ebt - obt = 290 分钟 = 4h50m，与真实时长对不上 → 不作为主用

【data_hr 锚点】（**核心！**）：
  - data_hr 是 1440 字节（UTC 0:00 → 23:59 一分钟一个），**不是相对 stage**
  - 也就是说 data_hr[0] 对应 UTC 0:00（= UTC 当天 0 点）
  - 实际心跳段的数据窗是 UTC 时间，需要根据**用户本地时区** shift 到本地分钟
  - 但 detail 端点给的 date_time 是「用户本地日历日」（slp 同一天）
  - 决策（M2 拍板）：data_hr[k] 对应 UTC k 分钟，对应本地 (k - tz_offset_secs/60) 分钟
    即本地分钟 = UTC分钟 - tz_offset_min
  - 过滤 255 哨兵 + 20..240 生理范围

【多个 sleep sessions 同一天】（小睡 / nap）：
  - 目前数据未观察到 multi-session。但 slp 本身是单 session（st..ed 一次）
  - 设计为支持：odd_stage + supNap 字段存在 → 第二段 nap
  - 实际：从 odd_stage 数组也提取 stages，写到同一天第二条 sleep_sessions

【DST 决策】（M2 拍板）：
  - 不主动修正 DST——按 Zepp 原始 tz 入库
  - 用户报告 DST 切换日数据偏移时再处理
"""
from __future__ import annotations
import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from normalizer.common import (
    MetricSample, NormalizedBatch,
    parse_timezone_text, offset_from_number,
)


# ===== 常量（M2 拍板）=====

# Stage mode 映射（Zepp 设备协议 → 语义）
MODE_DEEP   = 5
MODE_LIGHT  = 4
MODE_REM    = 8
MODE_REM_ALT = 11   # 某些 Zepp 算法版本用 11
MODE_AWAKE  = 7

STAGE_MODE_MAP = {
    MODE_DEEP:    "deep",
    MODE_LIGHT:   "light",
    MODE_REM:     "rem",
    MODE_REM_ALT: "rem",
    MODE_AWAKE:   "awake",
}

# data_hr 字节值映射：255 = 未测（实测）
DATA_HR_SENTINEL = 255
DATA_HR_MIN_VALID = 20
DATA_HR_MAX_VALID = 240


@dataclass
class SleepSession:
    """一条完整的睡眠 session（夜间主睡 + 可选午睡）。"""
    date: str                   # 用户本地日历日（YYYY-MM-DD，对应 API 的 date_time）
    source: str                 # deviceId 或 source 字段
    start_ts: int               # unix 秒（stage[0].start 对应时间）
    end_ts: int                 # unix 秒（stage[-1].stop 对应时间）
    tz_offset_secs: int         # 解析出的时区偏移
    time_in_bed_secs: Optional[int]   # 在床时长（秒）—— 4 级兜底
    deep_secs: int = 0
    light_secs: int = 0
    rem_secs: int = 0
    awake_secs: int = 0
    unknown_secs: int = 0
    sp_o2_avg: Optional[float] = None
    breath_avg: Optional[float] = None
    heart_rate_avg: Optional[float] = None
    heart_rate_min: Optional[int] = None
    rhr: Optional[int] = None
    score: Optional[int] = None
    algo_version: Optional[str] = None
    sleep_source: Optional[int] = None
    is_nap: bool = False        # M3 章节 3.3：nap 标记
    raw_summary: dict = field(default_factory=dict)


@dataclass
class SleepStageSlice:
    """一条 stage 切片。"""
    session_id: Optional[int]    # 外键，DB 写后填充
    date: str
    start_ts: int                # unix 秒
    end_ts: int
    start_min: int               # 相对锚点的分钟数
    end_min: int
    mode_code: int
    mode: str                    # "deep" / "light" / "rem" / "awake" / "unknown"
    tz_offset_secs: int


@dataclass
class BandHrSample:
    """band_data 的逐分钟心率。"""
    date: str
    ts: int                # unix 秒（本地午夜 + k 分钟）
    bpm: Optional[int]     # 255 哨兵 → None
    tz_offset_secs: int


@dataclass
class SleepBatch:
    sessions: list = field(default_factory=list)        # SleepSession
    stages: list = field(default_factory=list)          # SleepStageSlice
    hr_samples: list = field(default_factory=list)      # BandHrSample
    diagnostics: list = field(default_factory=list)
    capability: str = "verified"


# ===== 顶层入口 =====

def normalize_band_sleep(raw) -> SleepBatch:
    """
    解析 band_data 响应（list 或 dict 都行）。

    顶层结构（M2 设计）：
      list → 每个 item 是一个 day
      dict → {"items": [...]} 兼容

    对每个 item：
      1. 解 base64 summary → 拿 slp/stp 结构
      2. 解析 tz（"28800" / "Asia/Shanghai" / 毫秒偏移 三种形态）
      3. 提取 slp.stage → 算出 sleep_sessions + sleep_stage_slices
      4. 解 base64 data_hr（detail 端点才有）→ heart_rate_band_samples
      5. 时间锚点：stage.start_min → unix = slp.st + start_min * 60
         注：slp.st 是 UTC 时间戳（秒）；stage 的分钟数相对 slp.st 算
    """
    batch = SleepBatch()
    items = _extract_band_items(raw)
    if not items:
        return batch

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            batch.diagnostics.append(f"item {idx}: not a dict")
            continue
        _parse_one_day(item, batch)

    return batch


def _extract_band_items(raw):
    """从 list / dict / dict.items 中提取 items 数组。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        items = raw.get("items")
        if isinstance(items, list):
            return items
        # 顶层就是 items 结构
        if all(isinstance(v, dict) for v in raw.values()):
            return list(raw.values())
    return []


# ===== 单日解析 =====

def _parse_one_day(item: dict, batch: SleepBatch) -> None:
    date_time = item.get("date_time")
    if not date_time or not isinstance(date_time, str):
        batch.diagnostics.append(f"item {item.get('date_time')}: 缺 date_time")
        return
    # 设备编号
    source = str(item.get("source") or item.get("device_id") or "")

    # 1. 解码 summary B64
    summary_b64 = item.get("summary")
    if not summary_b64 or not isinstance(summary_b64, str):
        # 无 summary 不算错，可能是个空日条目
        batch.diagnostics.append(f"{date_time}: 无 summary 字段")
        return

    try:
        summary = json.loads(base64.b64decode(summary_b64).decode())
    except Exception as e:
        batch.diagnostics.append(f"{date_time}: summary B64 解码失败 ({e})")
        return

    slp = summary.get("slp")
    if not isinstance(slp, dict):
        batch.diagnostics.append(f"{date_time}: slp 不是 dict")
        return

    # 2. 解析 tz（三种形态："28800" / "Asia/Shanghai" / 28800000ms）
    tz_offset_secs = _parse_tz_offset(summary.get("tz"))
    if tz_offset_secs is None:
        # 缺 tz 跳过整条（M2 拍板：缺 tz 不可信）
        batch.diagnostics.append(f"{date_time}: tz 字段解析失败 ({summary.get('tz')!r})，跳过")
        return

    # 3. 解析 stage → sleep_sessions + sleep_stage_slices
    stages_raw = slp.get("stage") or []
    odd_stages_raw = slp.get("odd_stage") or []

    if not stages_raw and not odd_stages_raw:
        # 空 stage（实测大部分天是这样）— 不写 session
        # 修复 bug：diagnostic 文案更明确，含 M2 拍板说明，让用户看到后知道"为什么不写"
        # 之前："{date_time}: 空 stage 数组（无睡眠）"
        # 现在：含"raw 已拉到" + "M2 拍板：stage 数据不充分时宁可不写"
        batch.diagnostics.append(
            f"{date_time}: 空 stage 数组（无睡眠），不写 sleep_session"
            f"（M2 拍板：stage 数据不充分时宁可不写；raw 已落到 raw_records, "
            f"st={slp.get('st')!r} ed={slp.get('ed')!r}）"
        )
    else:
        # 主 session（来自 stage）
        if stages_raw:
            _emit_session_and_stages(
                date_time, source, summary, slp, stages_raw,
                tz_offset_secs, batch, is_nap=False,
            )
        # 午睡 session（来自 odd_stage，supNap=True 时）
        if odd_stages_raw and slp.get("supNap"):
            _emit_session_and_stages(
                date_time, source, summary, slp, odd_stages_raw,
                tz_offset_secs, batch, is_nap=True,
            )

    # 4. data_hr → heart_rate_band_samples（仅 detail 端点有）
    data_hr_b64 = item.get("data_hr")
    if isinstance(data_hr_b64, str) and data_hr_b64.strip():
        _emit_band_hr(date_time, data_hr_b64, tz_offset_secs, batch)


# ===== tz 解析（3 种形态）=====

def _parse_tz_offset(tz_value) -> Optional[int]:
    """
    3 种形态：
      "28800"  / "28800.0"  / "28800000"   → 数字字符串
      "Asia/Shanghai" / "GMT+08:00"        → 字符串
      28800 / 28800000                     → int/float
    """
    if tz_value is None:
        return None
    if isinstance(tz_value, (int, float)):
        return offset_from_number(float(tz_value))
    if isinstance(tz_value, str):
        s = tz_value.strip()
        if not s:
            return None
        # 试数字（offset_from_number 会自动处理毫秒转换）
        try:
            n = float(s)
            return offset_from_number(n)
        except ValueError:
            pass
        # 字符串（"Asia/Shanghai" / "GMT+08:00"）
        return parse_timezone_text(s)
    return None


# ===== Stage → Session + Slices =====

def _emit_session_and_stages(
    date_time: str,
    source: str,
    summary: dict,
    slp: dict,
    stages_raw: list,
    tz_offset_secs: int,
    batch: SleepBatch,
    is_nap: bool,
) -> None:
    """一组 stages → 1 个 sleep_session + N 个 sleep_stage_slices。"""
    if not stages_raw:
        return
    slp_st = slp.get("st")   # unix 秒（数据锚点）
    if not isinstance(slp_st, (int, float)) or slp_st <= 0:
        batch.diagnostics.append(f"{date_time}: slp.st 缺失或无效 ({slp_st!r})")
        return

    # ===== M4 章节 4.8：DST 切换日探测 =====
    # 若 anchor (slp.st) 落在该 date_time 的本地日历日之外，且 tz 不在 15 分钟倍数
    # 之外的整数倍（如 ±3600） → 标 DST 标记
    try:
        from datetime import datetime as _dt, timezone as _tz
        anchor_dt = _dt.fromtimestamp(int(slp_st), tz=_tz.utc)
        # 把 UTC 时间转换到本地（按 Zepp 提供的 tz_offset_secs）
        local_dt = anchor_dt.astimezone(_tz(_tz.utc.utcoffset(anchor_dt) or _tz.utc.utcoffset(anchor_dt)))
        # 简化：直接判断 slp_st 转 UTC 后，UTC 日 与 date_time 字符串的关系
        anchor_utc_date = anchor_dt.strftime("%Y-%m-%d")
        # DST 标记：本地 offset 不是 28800（亚太）、不在 900 的倍数之外
        # 或更直接：anchor UTC 日 != 解析后的本地日
        # 这里做最简诊断：记录 tz_offset_secs 到 diagnostic，便于事后排查
        if tz_offset_secs not in (8 * 3600, 9 * 3600, 0, 5 * 1800, -5 * 3600):
            batch.diagnostics.append(
                f"{date_time}: tz_offset_secs={tz_offset_secs} 非标准时区，疑似 DST/夏令时切换日"
            )
    except Exception:
        pass

    # 排序 stages
    stages_sorted = sorted(
        [s for s in stages_raw if isinstance(s, dict)],
        key=lambda s: s.get("start", 0)
    )
    if not stages_sorted:
        batch.diagnostics.append(f"{date_time}: stages 全部为空字典")
        return

    # 累计时长（按 mode 分桶）
    deep = light = rem = awake = unknown = 0
    slices = []
    for s in stages_sorted:
        sm = s.get("start")
        em = s.get("stop")
        mode_code = s.get("mode")
        if not isinstance(sm, (int, float)) or not isinstance(em, (int, float)):
            continue
        if em <= sm:
            continue
        minutes = int(em - sm)
        mode_str = STAGE_MODE_MAP.get(mode_code, "unknown")
        if mode_str == "deep":
            deep += minutes
        elif mode_str == "light":
            light += minutes
        elif mode_str == "rem":
            rem += minutes
        elif mode_str == "awake":
            awake += minutes
        else:
            unknown += minutes
        # start_ts = slp_st + sm*60 (slp_st 是 UTC unix 秒)
        slice_start_ts = int(slp_st + sm * 60)
        slice_end_ts = int(slp_st + em * 60)
        slices.append(SleepStageSlice(
            session_id=None,
            date=date_time,
            start_ts=slice_start_ts,
            end_ts=slice_end_ts,
            start_min=int(sm),
            end_min=int(em),
            mode_code=int(mode_code) if mode_code is not None else -1,
            mode=mode_str,
            tz_offset_secs=tz_offset_secs,
        ))

    if not slices:
        batch.diagnostics.append(f"{date_time}: stages 排序后为空")
        return

    # time_in_bed 4 级兜底
    tib_secs = _compute_time_in_bed(slp, stages_sorted)

    # start_ts/end_ts 用 stage 实际起止（而不是 slp.st）
    session_start_ts = slices[0].start_ts
    session_end_ts = slices[-1].end_ts

    # 其他汇总字段
    sp_o2_avg = _safe_float(slp.get("spos"))   # spo2 平均
    breath_avg = _safe_float(slp.get("spor"))  # breath rate 平均
    rhr = _safe_int(slp.get("rhr"))            # 静息心率
    score = _safe_int(slp.get("ss"))           # 睡眠评分
    sleep_source = _safe_int(slp.get("sleepSource")) or _safe_int(slp.get("source"))
    algo_version = summary.get("algv") or slp.get("sleepAlgoVersion")

    session = SleepSession(
        date=date_time,
        source=source,
        start_ts=session_start_ts,
        end_ts=session_end_ts,
        tz_offset_secs=tz_offset_secs,
        time_in_bed_secs=tib_secs,
        deep_secs=deep * 60,
        light_secs=light * 60,
        rem_secs=rem * 60,
        awake_secs=awake * 60,
        unknown_secs=unknown * 60,
        sp_o2_avg=sp_o2_avg,
        breath_avg=breath_avg,
        rhr=rhr,
        score=score,
        algo_version=str(algo_version) if algo_version is not None else None,
        sleep_source=sleep_source,
        is_nap=is_nap,
        raw_summary=summary,
    )
    batch.sessions.append(session)
    # 用 list 引用挂 session_id 占位（写入 DB 后回填）
    batch.stages.extend(slices)


def _compute_time_in_bed(slp: dict, stages_sorted: list) -> Optional[int]:
    """4 级兜底（M2 拍板）：
       1) slp.ed - slp.st（秒）
       2) (stage[-1].stop - stage[0].start) * 60
       3) (ebt - obt) * 60（如果有）
       4) None（不假造）
    """
    # 1
    st = _safe_int(slp.get("st"))
    ed = _safe_int(slp.get("ed"))
    if st is not None and ed is not None and ed > st and (ed - st) < 86400:
        return ed - st
    # 2
    if stages_sorted:
        first_start = stages_sorted[0].get("start")
        last_stop = stages_sorted[-1].get("stop")
        if isinstance(first_start, (int, float)) and isinstance(last_stop, (int, float)) and last_stop > first_start:
            return int((last_stop - first_start) * 60)
    # 3
    obt = _safe_int(slp.get("obt"))
    ebt = _safe_int(slp.get("ebt"))
    if obt is not None and ebt is not None and (ebt - obt) > 0 and (ebt - obt) < 1440:
        return (ebt - obt) * 60
    return None


def _safe_float(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f   # NaN guard
    except (TypeError, ValueError):
        return None


def _safe_int(v):
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


# ===== data_hr → heart_rate_band_samples =====

def _emit_band_hr(date_time: str, data_hr_b64: str, tz_offset_secs: int,
                  batch: SleepBatch) -> None:
    """
    解码 data_hr 字节 → 心率样本数组。

    M3 章节 3.4：data_hr 偶尔是 1920 字节（不是 1440）。
    - 1440 字节：每天 1440 分钟（1 sample/min）
    - 1920 字节：可能是 24 小时 × 80 sample/min 或跨日 32 小时
    策略：始终按 1440 段播；多余字节忽略 + 标记 diagnostic
    """
    try:
        raw = base64.b64decode(data_hr_b64)
    except Exception as e:
        batch.diagnostics.append(f"{date_time}: data_hr B64 解码失败 ({e})")
        return

    if not raw:
        batch.diagnostics.append(f"{date_time}: data_hr 解码为空")
        return

    n_bytes = len(raw)
    if n_bytes != 1440 and n_bytes != 1920:
        batch.diagnostics.append(f"{date_time}: data_hr 字节长度异常 {n_bytes}（期望 1440/1920）")
    if n_bytes < 1440:
        # 允许短于 1440 的字节（旧固件可能只返回有效段）→ 仍处理
        batch.diagnostics.append(f"{date_time}: data_hr 短 {n_bytes} 字节，按现有长度处理")

    if n_bytes > 1440:
        # 1920 字节：前 1440 字节按 UTC 当天对齐；剩余字节忽略
        batch.diagnostics.append(f"{date_time}: data_hr {n_bytes} 字节 → 用前 1440，剩余丢弃")
        raw = raw[:1440]

    try:
        local_midnight = datetime.strptime(date_time, "%Y-%m-%d").replace(
            tzinfo=timezone(timedelta(seconds=tz_offset_secs))
        )
    except ValueError:
        batch.diagnostics.append(f"{date_time}: 日期解析失败")
        return
    local_midnight_unix = int(local_midnight.timestamp())

    tz_offset_min = tz_offset_secs // 60
    for k, b in enumerate(raw):
        if k >= 1440:
            break
        if b == DATA_HR_SENTINEL:
            continue
        if b < DATA_HR_MIN_VALID or b > DATA_HR_MAX_VALID:
            continue
        local_min = k - tz_offset_min
        ts = local_midnight_unix + local_min * 60
        batch.hr_samples.append(BandHrSample(
            date=date_time,
            ts=ts,
            bpm=int(b),
            tz_offset_secs=tz_offset_secs,
        ))


# ===== 便捷函数：给 summary 类型用 =====

def decode_summary_b64(summary_b64: str) -> Optional[dict]:
    """summary B64 解码的便捷函数（供 storage 层复用）。"""
    try:
        return json.loads(base64.b64decode(summary_b64).decode())
    except Exception:
        return None