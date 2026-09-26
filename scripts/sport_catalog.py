"""
Zepp Cloud sport type catalog —— 来自 ZeppBridge 项目 (lingcang728/ZeppBridge)
src/assets/workouts/catalog.json (version 7, 2026-09-10)。

把 Zepp API 返回的数字 type 翻译成人类可读的运动名。
权威来源：ZeppBridge 项目维护的 catalog.json，包含 134 个已编号运动 + 100+ 无编号运动。

设计原则（沿用 zepp-mcp / zepp-bridge）：
- 不在 catalog 里的 type → 返回 ('unknown_<type>', '未知运动(<type>)')
- 永远不"猜"——只有 catalog 里有的才翻译

为什么需要 catalog 而不是自己猜：
- Zepp App 显示的中文名 ≠ 数字 type 的字面含义
  例：type=13 在 Zepp App 里显示"健走"，但 Zepp OS 设备协议里同一个数字含义不同
- Zepp Cloud 数字 type 表 ≠ Zepp OS 设备协议 type 表（catalog notes 里明确说）
- 自行映射会把"健走"标成"登山"，把"游泳"标成"自由训练"——用户看到会怀疑 skill 写错了
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

CATALOG_PATH = Path(__file__).parent / "sport_catalog.json"


def _load_catalog() -> dict:
    with open(CATALOG_PATH) as f:
        return json.load(f)


# code → (key, 中文名)
_CODE_TO_SPORT: dict[int, tuple[str, str]] = {}
# key → 中文名（用于无 code 条目或用户输入校正）
_KEY_TO_ZH: dict[str, str] = {}

def _init():
    global _CODE_TO_SPORT, _KEY_TO_ZH
    if _CODE_TO_SPORT:
        return
    cat = _load_catalog()
    for entry in cat.get("sports", []):
        if entry.get("code") is not None:
            _CODE_TO_SPORT[entry["code"]] = (entry["key"], entry["label_zh"])
        if entry.get("key"):
            _KEY_TO_ZH[entry["key"]] = entry["label_zh"]


def sport_name(code: int | str | None) -> tuple[str, str]:
    """
    把 Zepp API 的数字 type 翻译成 (英文 key, 中文名)。

    例：sport_name(22) → ('hiking', '徒步')
        sport_name(6)  → ('walking', '健走')
        sport_name(13) → ('walking', '健走')
        sport_name(99) → ('unknown_99', '未知运动(99)')
    """
    _init()
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        return (f"unknown_{code}", f"未知运动({code})")
    if code_int in _CODE_TO_SPORT:
        return _CODE_TO_SPORT[code_int]
    return (f"unknown_{code_int}", f"未知运动({code_int})")


def key_to_zh(key: str) -> str:
    """英文 key → 中文名（用于 list_workouts 时已知 key）"""
    _init()
    return _KEY_TO_ZH.get(key, key)


def sport_safe_name(code: int | str | None) -> str:
    """
    返回中文名；code 未知时返回 '未知运动(<code>)'。

    与 sport_name() 的区别：
    - sport_name() 返回 tuple (key, 中文名)
    - sport_safe_name() 只返回中文（key 丢了）

    用法：dashboard / print 时只想显示中文名，不想处理 tuple。
    """
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        return f"未知运动({code})"
    _init()
    if code_int in _CODE_TO_SPORT:
        return _CODE_TO_SPORT[code_int][1]  # 中文名
    return f"未知运动({code_int})"


# 一些常用别名（方便 query_zepp.py 写中文查询）
SPORT_ALIAS = {
    "hiking": ["徒步", "登山", "爬山", "hike"],
    "walking": ["健走", "走路", "walk"],
    "ride": ["骑行", "骑车", "单车", "cycle"],
    "run": ["跑步", "跑", "jog"],
    "pool_swimming": ["游泳", "swim"],
    "strength": ["力量", "举铁", "撸铁", "weight"],
    "treadmill": ["跑步机"],
    "elliptical": ["椭圆机"],
    "indoor_cycling": ["室内骑行"],
    "trail_running": ["越野跑"],
    "free_training": ["自由训练"],
    "rope_skipping": ["跳绳"],
    "yoga": ["瑜伽"],
}


if __name__ == "__main__":
    # 自检：打印 catalog 元数据 + 几个示例
    cat = _load_catalog()
    print(f"=== Zepp sport catalog ===")
    print(f"version: {cat.get('version')}")
    print(f"checked_at: {cat.get('checked_at')}")
    print(f"sports 数: {len(cat.get('sports', []))}")
    print(f"已编号: {len([e for e in cat['sports'] if e.get('code') is not None])}")
    print(f"无编号: {len([e for e in cat['sports'] if e.get('code') is None])}")
    print()
    print("示例:")
    for code in [1, 6, 13, 22, 52]:
        k, zh = sport_name(code)
        print(f"  type={code:>3} → ({k}, {zh})")