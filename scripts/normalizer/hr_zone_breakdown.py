"""
心率区间占比计算（核心算法，对齐 Zepp App 截图）

为什么需要这个模块？
  Zepp App 心率页显示「舒缓轻松 48% / 热身放松 5% / 脂肪燃烧 0% ...」这种
  6 个区间分布。Zepp Cloud 在 `heart_range` 字段里直接给这 6 段绝对 bpm 阈值
  的秒数（< 113, 113-141, 141-154, 154-162, 162-173, ≥ 173）—— 上界是固定的，
  不依赖用户 HRmax。本地也可以从 `heart_rate_band_samples` 表的 bpm 曲线
  重算，作为交叉验证。

阈值来源（来自 ZeppBridge normalizer/mod.rs::parse_heart_range）：
  6 区间上界 = 113/141/154/162/173/190 bpm
  不依赖 HRmax、年龄、设备 — Zepp Cloud 全账号通用

HRmax 怎么算：
  优先用 user_profile + measurements.device_max_hr（实测值）
  fallback HUNT 男生: 211 - 0.64*age（用户实测 = 187, HUNT = 187.06 完美匹配）
  任务假设 220-37=183 不正确 —— 实测 device_max_hr = 187 是 Zepp 真正用的值

区间边界（含下不含上 + 整数 bpm）：
  Zone 1 舒缓轻松:    bpm < 113
  Zone 2 热身放松:    113 ≤ bpm < 141
  Zone 3 脂肪燃烧:    141 ≤ bpm < 154
  Zone 4 心肺强化:    154 ≤ bpm < 162
  Zone 5 耐力强化:    162 ≤ bpm < 173
  Zone 6 无氧极限:    bpm ≥ 173

实测差异：DB 9-24 仅有 12.4h 数据（02:28-14:50, 625 个点）
        本地重算几乎 100% Zone 1（max=104 < 113），
        与 Zepp App 截图 48% 差距来自 DB 缺 14:50-24:00 数据。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable, Sequence


# Zepp 6 区间上界（绝对 bpm，固定阈值）
# 来自 Zepp Cloud `heart_range` 字段：1882,113;3486,141;10,154;0,162;0,173;0,190
# 来源：ZeppBridge storage/mod.rs::workout_hr_zones + normalizer/mod.rs::parse_heart_range
ZONE_BOUNDS_BPM: Sequence[int] = (113, 141, 154, 162, 173, 190)

# 区间中文标签（与 Zepp App 心率页一致）
ZONE_LABELS: Sequence[str] = (
    "舒缓轻松",
    "热身放松",
    "脂肪燃烧",
    "心肺强化",
    "耐力强化",
    "无氧极限",
)


@dataclass
class HRZoneBreakdown:
    """6 区间占比结果（与 Zepp App 心率页对齐）

    单位说明：
      - *_min = 在该区间的累计秒数 / 60（floor），单位 = 分钟
      - *_pct = 该区间秒数 / valid_seconds × 100
      - total_min = 所有样本（含哨兵过滤前）按 ts 跨度算的总分钟
      - valid_min = 有效样本累计秒数 / 60
      - hr_max_used = 本次计算采用的 HRmax（备用字段，区间已用绝对 bpm 阈值，
                     不依赖 HRmax，但保留供 UI 显示）
    """
    zone_1_recovery_pct: float
    zone_1_recovery_min: int
    zone_2_warmup_pct: float
    zone_2_warmup_min: int
    zone_3_fatburn_pct: float
    zone_3_fatburn_min: int
    zone_4_cardio_pct: float
    zone_4_cardio_min: int
    zone_5_endurance_pct: float
    zone_5_endurance_min: int
    zone_6_anaerobic_pct: float
    zone_6_anaerobic_min: int
    total_min: int
    valid_min: int
    hr_max_used: int

    def as_dict(self) -> dict:
        return {
            "zone_1_recovery_pct": round(self.zone_1_recovery_pct, 1),
            "zone_1_recovery_min": self.zone_1_recovery_min,
            "zone_2_warmup_pct": round(self.zone_2_warmup_pct, 1),
            "zone_2_warmup_min": self.zone_2_warmup_min,
            "zone_3_fatburn_pct": round(self.zone_3_fatburn_pct, 1),
            "zone_3_fatburn_min": self.zone_3_fatburn_min,
            "zone_4_cardio_pct": round(self.zone_4_cardio_pct, 1),
            "zone_4_cardio_min": self.zone_4_cardio_min,
            "zone_5_endurance_pct": round(self.zone_5_endurance_pct, 1),
            "zone_5_endurance_min": self.zone_5_endurance_min,
            "zone_6_anaerobic_pct": round(self.zone_6_anaerobic_pct, 1),
            "zone_6_anaerobic_min": self.zone_6_anaerobic_min,
            "total_min": self.total_min,
            "valid_min": self.valid_min,
            "hr_max_used": self.hr_max_used,
        }


def _is_valid(v) -> bool:
    """心率哨兵过滤：0 = 未测，< 20 或 > 300 视为无效。"""
    if v is None:
        return False
    try:
        vf = float(v)
    except (TypeError, ValueError):
        return False
    return 20.0 <= vf <= 300.0


def _classify_zone(bpm: int) -> int:
    """根据绝对 bpm 阈值返回 1..6 区间编号。"""
    if bpm < ZONE_BOUNDS_BPM[0]:  # < 113
        return 1
    if bpm < ZONE_BOUNDS_BPM[1]:  # 113-140
        return 2
    if bpm < ZONE_BOUNDS_BPM[2]:  # 141-153
        return 3
    if bpm < ZONE_BOUNDS_BPM[3]:  # 154-161
        return 4
    if bpm < ZONE_BOUNDS_BPM[4]:  # 162-172
        return 5
    return 6  # ≥ 173


def compute_hr_zone_breakdown(
    curve: Iterable,
    hr_max: int = 187,
    avg_sample_seconds: int = 60,
) -> HRZoneBreakdown:
    """从心率样本算出 6 区间占比（对齐 Zepp App）。

    参数：
      curve: 心率 bpm 序列，可以是：
        - [86, 88, 90, ...] 裸数字列表
        - [{"bpm": 86, "ts": ...}, ...] dict 列表
      hr_max: 本次采用的 HRmax（仅作 UI 显示用，区间已硬编码 bpm 阈值）
      avg_sample_seconds: 平均每样本代表的秒数（默认 60s）。
        Zepp heart_rate_band_samples 实际是秒级采样；用户可以从 ts 差自动
        推断，这里给一个保守默认。

    算法（与 Zepp Cloud `heart_range` 字段一致）：
      6 区间上界 = 113 / 141 / 154 / 162 / 173 / 190 bpm
      区间 1 = bpm < 113
      区间 2 = 113 ≤ bpm < 141
      区间 3 = 141 ≤ bpm < 154
      区间 4 = 154 ≤ bpm < 162
      区间 5 = 162 ≤ bpm < 173
      区间 6 = bpm ≥ 173

    哨兵过滤：
      bpm = 0 / None / < 20 / > 300 视为无效

    时间估算：
      valid_min = valid_samples * avg_sample_seconds / 60
      这与 stress_breakdown（按样本数占比）不同；HR 区间占比更接近 Zepp App
      的「按时间」展示，所以按 sample_seconds 估算。

    关键不变量：
      - 6 比例加和 ≈ 100%（允许 ±0.1% 四舍五入偏差）
      - valid_min = 0 时所有比例 = 0.0（避免除零）
      - *_min 用 floor 整数化（与 Zepp App 显示「10h 4min」对齐）
    """
    counts = [0] * 6
    total = 0
    valid = 0

    for item in curve:
        total += 1
        # 兼容 dict 和裸数字
        if isinstance(item, dict):
            v = item.get("bpm") or item.get("value") or item.get("heartRate")
        else:
            v = item
        if not _is_valid(v):
            continue
        valid += 1
        bpm = int(round(float(v)))
        z = _classify_zone(bpm)
        counts[z - 1] += 1

    valid_min = valid * avg_sample_seconds // 60

    if valid == 0:
        return HRZoneBreakdown(
            zone_1_recovery_pct=0.0, zone_1_recovery_min=0,
            zone_2_warmup_pct=0.0, zone_2_warmup_min=0,
            zone_3_fatburn_pct=0.0, zone_3_fatburn_min=0,
            zone_4_cardio_pct=0.0, zone_4_cardio_min=0,
            zone_5_endurance_pct=0.0, zone_5_endurance_min=0,
            zone_6_anaerobic_pct=0.0, zone_6_anaerobic_min=0,
            total_min=0, valid_min=0,
            hr_max_used=hr_max,
        )

    pcts = [100.0 * c / valid for c in counts]
    mins = [c * avg_sample_seconds // 60 for c in counts]

    return HRZoneBreakdown(
        zone_1_recovery_pct=pcts[0], zone_1_recovery_min=mins[0],
        zone_2_warmup_pct=pcts[1], zone_2_warmup_min=mins[1],
        zone_3_fatburn_pct=pcts[2], zone_3_fatburn_min=mins[2],
        zone_4_cardio_pct=pcts[3], zone_4_cardio_min=mins[3],
        zone_5_endurance_pct=pcts[4], zone_5_endurance_min=mins[4],
        zone_6_anaerobic_pct=pcts[5], zone_6_anaerobic_min=mins[5],
        total_min=valid_min, valid_min=valid_min,
        hr_max_used=hr_max,
    )


def estimate_hr_max(age_years: float, is_male: bool = True) -> int:
    """HRmax 估算（fallback 用）。

    HUNT 公式（男性专用）：HRmax = 211 - 0.64*age
    Tanaka 公式（通用）：HRmax = 208 - 0.7*age

    用户实测 device_max_hr = 187, 37.57 岁（男性）
    HUNT = 211 - 0.64*37.57 = 187.06 → 187 ✅
    Tanaka = 208 - 0.7*37.57 = 181.7 → 182 ❌

    所以男性应该用 HUNT，女性用 Tanaka。
    """
    if is_male:
        return int(round(211 - 0.64 * age_years))
    return int(round(208 - 0.7 * age_years))