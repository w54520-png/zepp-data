"""
Food（饮食）流解析 —— M4 章节 4.2。

端点：GET /v2/users/me/events?eventType=Food
无 subType（v2_events 流）。

【M3 实施发现】2026-09-23：CN 国服用户实测拉取返回空 items 数组。
M4 决策：
  - 即使当前用户没有数据，也接入 normalizer（i18n 用户可能启用）
  - 解析逻辑完全照搬 ZeppBridge Rust `food_metrics`（详见 rust
    /tmp/ZeppBridge-main/src-tauri/crates/core/src/normalizer/mod.rs:1692）

【响应形态】（基于 ZeppBridge 测试 fixture + Rust 文档）：
  envelope: {"code":1, "data":{"items":[...]}}
  item 形态（数值可能挂在顶层或 `value` 里）：
    { "date": "2026-09-18", "calories": 500, "protein": 20 }
    { "value": { "date": "2026-09-18", "calories": "300", "fat": 0 } }
    { "date": "2026-09-19", "value": { "carbs": 40 } }

【4 个 metric】（对齐 Rust food_metrics MACROS）：
  - intake_calories   (1..20000) kcal
  - intake_protein_g  (0..1000)  g
  - intake_fat_g      (0..1000)  g
  - intake_carbs_g    (0..2000)  g

【按日累加】（M4 4.2 决策）：
  - 一天可能有好几条记录（早中晚三餐，或一餐一条）
  - 按 (date, metric) 求和 → DailyMetric
  - 不取日均/不区分餐次（Zepp API 没标 mealType）

【字段候选】（对齐 Rust `MACROS[*].names`）：
  calories: ["calories", "calorie", "kcal", "energy"]
  protein:  ["protein", "proteins"]
  fat:      ["fat", "fats"]
  carbs:    ["carbohydrate", "carbohydrates", "carbs"]

【日期解析】summary_date：
  - 顶层 date / day / dayId / dateString
  - 嵌套 value.date
  - 都没有时回退到 timestamp + tz offset
"""
from __future__ import annotations
from normalizer.common import (
    DailyMetric, NormalizedBatch,
    first_number_from, first_value_from, parse_number,
    summary_date, in_range,
)

# ===== 4 个 Macro =====

_FOOD_MACROS = [
    {
        "metric": "intake_calories",
        "names": ("calories", "calorie", "kcal", "energy"),
        "unit": "kcal",
        "range": (1.0, 20000.0),       # (lo, hi) —— Rust 是 (1.0, 20000.0)
    },
    {
        "metric": "intake_protein_g",
        "names": ("protein", "proteins"),
        "unit": "g",
        "range": (0.0, 1000.0),         # Rust 是 (0.0, 1000.0)
    },
    {
        "metric": "intake_fat_g",
        "names": ("fat", "fats"),
        "unit": "g",
        "range": (0.0, 1000.0),
    },
    {
        "metric": "intake_carbs_g",
        "names": ("carbohydrate", "carbohydrates", "carbs"),
        "unit": "g",
        "range": (0.0, 2000.0),
    },
]


def normalize_food(raw: dict) -> NormalizedBatch:
    """
    解析 Food 流 → 按日累加 4 个 macro → DailyMetric 列表。

    策略（照搬 ZeppBridge food_metrics）：
      1) 从 envelope 抽 items（支持 items/data.items/data/data=list）
      2) 每条 item 取 date（顶层或 value.date）
      3) 在 顶层 + value 两个 scope 里找 4 个 macro
      4) 按 (date, metric) 累加
      5) 累加值用 4 个 macro 的范围 [lo, hi] 校验后入库
      6) 跳过原因进 diagnostics（与 Rust 一致）
    """
    batch = NormalizedBatch()
    items = _extract_items(raw)
    if not items:
        batch.capability = "no_data"  # CN 国服实测返回空数组
        return batch

    # 按 (date, metric) 累加
    per_day: dict = {}
    unknown_macros: dict = {}     # 用于 diagnostics（Zepp 不认识的字段）
    skipped = 0

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            batch.diagnostics.append(f"food item {idx}: 不是对象")
            continue
        # value 子对象（item_object 自动判断）
        nested = item.get("value") if isinstance(item.get("value"), dict) else None
        date = summary_date(item, nested)
        if not date:
            batch.diagnostics.append(f"food item {idx}: 没有可用日期")
            skipped += 1
            continue

        # 找 4 个 macro（数值可能在顶层也可能在 value 里）
        matched_any = False
        matched_fields = set()
        for macro in _FOOD_MACROS:
            v = first_number_from(item, nested, macro["names"])
            if v is None:
                continue
            matched_any = True
            for n in macro["names"]:
                matched_fields.add(n)
            key = (date, macro["metric"])
            per_day[key] = per_day.get(key, 0.0) + v

        # 探测未知字段（用于 diagnostics，不入库）—— 即便有 macro 命中也要扫
        for scope in (item, nested) if nested else (item,):
            if not isinstance(scope, dict):
                continue
            for k in scope.keys():
                if not isinstance(k, str):
                    continue
                if k in ("date", "day", "dayId", "dateString", "value",
                         "timestamp", "time", "deviceId", "device_id",
                         "userId", "memberId", "is_fused", "isFused",
                         "source_scope", "sourceScope", "eventType"):
                    continue
                if k in matched_fields:
                    continue
                # 排除数字字段（macros 命中的也算）
                if any(k == n for m in _FOOD_MACROS for n in m["names"]):
                    continue
                unknown_macros[k] = unknown_macros.get(k, 0) + 1

    # 范围校验后入库
    for (date, metric), sum_v in per_day.items():
        macro = next((m for m in _FOOD_MACROS if m["metric"] == metric), None)
        if macro is None:
            continue
        lo, hi = macro["range"]
        # 下界 ≤ 0 也接受（饮食量可能是 0）；但入库前需过滤负值
        if sum_v < 0 or not in_range(sum_v, lo, hi):
            batch.diagnostics.append(
                f"food {date} {metric}: 累加值 {sum_v} 超出 ({lo},{hi})"
            )
            continue
        # 浮点收敛到 2 位小数（饮食数据小数点后 2 位足够）
        value = round(sum_v, 2)
        batch.records.append(DailyMetric(
            metric=metric,
            date=date,
            value=float(value),
            unit=macro["unit"],
            source_scope="user_fused",  # 饮食通常是手动记录
            device_id=None,
        ))

    # diagnostics
    if skipped:
        batch.diagnostics.append(f"food: {skipped} 条记录无日期，跳过")
    if unknown_macros:
        unknown_list = ", ".join(f"{k}×{v}" for k, v in unknown_macros.items())
        batch.diagnostics.append(f"food: 未知字段 {unknown_list}")
    return batch


# ===== envelope 抽取（与 common.extract_items 兼容，但更宽松） =====

def _extract_items(raw):
    """
    抽出 items 数组。容忍 5 种 envelope：
      - 顶层 list
      - {"items": [...]}
      - {"data": [...]}  (list)
      - {"data": {"items": [...]}}
      - {"code":1, "data":{"items":[...]}}
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        return []
    # 1) 顶层 items / data
    for k in ("items", "records", "results", "list"):
        v = raw.get(k)
        if isinstance(v, list):
            return v
    # 2) data 是 list
    data = raw.get("data")
    if isinstance(data, list):
        return data
    # 3) data 是 dict，含 items
    if isinstance(data, dict):
        for k in ("items", "records", "results", "list"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    # 4) data 是字符串（编码）→ 不可安全解码
    return []
