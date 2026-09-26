"""
压力区间占比计算（核心算法，对齐 Zepp App 截图）

为什么需要这个模块？
  Zepp App 压力页显示"放松 0% / 正常 49% / 中等 41% / 偏高 10%"这种区间分布。
  Zepp Cloud 的 `all_day_stress` 端点直接给这 4 个比例（字段名 *Proportion）。
  本地也可以从 `data` 曲线（5min 一个点）重算，作为交叉验证。

Zepp 量表：1-100（0 = 未测，<1 或 >100 都视为无效）
区间边界（实测对齐 Zepp App）：
  放松:  1 ≤ v ≤ 39   (<40)
  正常: 40 ≤ v ≤ 59
  中等: 60 ≤ v ≤ 79
  偏高: 80 ≤ v ≤ 100  (>=80)

实测差异：服务端 4 比例加和 = 100%；本地重算 71 个点 47.9+42.3+9.9+0.2 = 100.3
        （四舍五入造成的差异，与 ZeppBridge 0.4pp 平均偏差一致）
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable


# 区间边界常量（与 Zepp App 截图 0/49/41/10 完全对齐）
# 用整数边界而不是 float，保证 v=39.999... vs v=40.0 的取舍确定性
BOUNDARIES = {
    "relax":  (1, 39),    # 1..=39 放松
    "normal": (40, 59),   # 40..=59 正常
    "medium": (60, 79),   # 60..=79 中等
    "high":   (80, 100),  # 80..=100 偏高
}


@dataclass
class StressBreakdown:
    """区间占比结果"""
    relax_pct: float
    normal_pct: float
    medium_pct: float
    high_pct: float
    total_points: int
    valid_points: int

    def as_dict(self) -> dict:
        return {
            "relax_pct": round(self.relax_pct, 1),
            "normal_pct": round(self.normal_pct, 1),
            "medium_pct": round(self.medium_pct, 1),
            "high_pct": round(self.high_pct, 1),
            "total_points": self.total_points,
            "valid_points": self.valid_points,
        }


def _is_valid(v) -> bool:
    """Zepp 量表从 1 开始；0 = 未测。 <1 或 >100 都视为无效。"""
    if v is None:
        return False
    try:
        vf = float(v)
    except (TypeError, ValueError):
        return False
    return 1.0 <= vf <= 100.0


def compute_stress_breakdown(curve: Iterable) -> StressBreakdown:
    """从一组 stress 值（iterable of number / {'value': N} dict）算 4 区间占比。

    输入：
      - 可以是 [44, 51, 78, ...] 这样的数字列表
      - 也可以是 [{"value": 44, "time": ...}, ...] 这样的 dict 列表
      - 也可以是 generator（lazy 友好）

    输出：
      StressBreakdown(relax_pct, normal_pct, medium_pct, high_pct,
                      total_points, valid_points)

    边界：
      1 ≤ v ≤ 39   → 放松
      40 ≤ v ≤ 59  → 正常
      60 ≤ v ≤ 79  → 中等
      80 ≤ v ≤ 100 → 偏高

    过滤：
      v < 1 或 v > 100 视为无效（0 = 未测，>100 = 异常）

    关键不变量：
      - 4 比例加和 ≈ 100%（允许 ±0.1% 四舍五入偏差）
      - 当 valid_points=0 时所有比例 = 0.0（避免除零）
    """
    relax = normal = medium = high = 0
    total = 0
    valid = 0

    for item in curve:
        total += 1
        # 兼容 dict（曲线点）和裸数字
        if isinstance(item, dict):
            v = item.get("value")
        else:
            v = item
        if not _is_valid(v):
            continue
        valid += 1
        vi = int(round(float(v)))
        if 1 <= vi <= 39:
            relax += 1
        elif 40 <= vi <= 59:
            normal += 1
        elif 60 <= vi <= 79:
            medium += 1
        else:  # 80 <= vi <= 100
            high += 1

    if valid == 0:
        return StressBreakdown(0.0, 0.0, 0.0, 0.0, total, 0)

    return StressBreakdown(
        relax_pct=100.0 * relax / valid,
        normal_pct=100.0 * normal / valid,
        medium_pct=100.0 * medium / valid,
        high_pct=100.0 * high / valid,
        total_points=total,
        valid_points=valid,
    )
