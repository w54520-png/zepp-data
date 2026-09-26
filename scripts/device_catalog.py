"""device_catalog.py — 把 Zepp productId/deviceSource 翻译成中文设备名

数据文件：scripts/device_catalog.json（来自 ZeppBridge catalog）

用法：
    from device_catalog import device_info, device_info_from_source
    catalog_id, name_zh = device_info(130)                # ('amazfit_gtr_mini', '跃我 GTR Mini')
    catalog_id, name_zh = device_info_from_source(9568512) # ('amazfit_balance_2', '跃我 Balance 2')
    catalog_id, name_zh = device_info(99999)              # ('generic_unknown', '未识别 Zepp 设备')

设计原则：
- catalog 跟 ZeppBridge 一样是**只读 catalog**（device_catalog.json），不允许运行时改字段
- 查不到返回 ('generic_unknown', '未识别 Zepp 设备')，不抛异常
- 用 productId 主查、deviceSource 副查（Zepp Cloud `devices` 流优先给 productId）
"""
from __future__ import annotations

import json
import os
from typing import Optional, Tuple

_CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device_catalog.json")


def _load_catalog() -> dict:
    """加载只读 catalog（每次调用重读文件，避免 stale）。"""
    with open(_CATALOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def device_info(product_id: int) -> Tuple[str, str]:
    """按 productId 查 catalog。

    Returns:
        (catalog_id, 中文名) —— 例如 ('amazfit_gtr_mini', '跃我 GTR Mini')
        找不到时返回 ('generic_unknown', '未识别 Zepp 设备')
    """
    if product_id is None:
        return _unknown()
    catalog = _load_catalog()
    for d in catalog["devices"]:
        if d.get("product_id") == int(product_id):
            return d["catalog_id"], d.get("name_zh") or d.get("display_name") or d["catalog_id"]
    return _unknown()


def device_info_from_source(device_source: int) -> Tuple[str, str]:
    """按 deviceSource 数值查 catalog。

    Returns:
        (catalog_id, 中文名) —— 例如 ('amazfit_balance_2', '跃我 Balance 2')
        找不到时返回 ('generic_unknown', '未识别 Zepp 设备')
    """
    if device_source is None:
        return _unknown()
    catalog = _load_catalog()
    for d in catalog["devices"]:
        codes = d.get("device_source_codes") or []
        if int(device_source) in codes:
            return d["catalog_id"], d.get("name_zh") or d.get("display_name") or d["catalog_id"]
    return _unknown()


def _unknown() -> Tuple[str, str]:
    return ("generic_unknown", "未识别 Zepp 设备")


def all_devices() -> list:
    """返回 catalog 中所有已知设备（含 placeholder）。给调试 / 自检用。"""
    return _load_catalog()["devices"]


if __name__ == "__main__":
    print("=== device catalog self-check ===")
    print(f"catalog file: {_CATALOG_PATH}")
    print()
    print("已知 deviceId → 中文名映射：")
    for d in all_devices():
        pid = d.get("product_id")
        codes = d.get("device_source_codes") or []
        if pid or codes:
            print(f"  productId={pid:>5}  deviceSource={codes}  → {d['name_zh']}")
    print()
    print("自检：")
    print("  device_info(130)            =", device_info(130))
    print("  device_info(146)            =", device_info(146))
    print("  device_info(99999)          =", device_info(99999))
    print("  device_info_from_source(8519936)  =", device_info_from_source(8519936))
    print("  device_info_from_source(9568512)  =", device_info_from_source(9568512))