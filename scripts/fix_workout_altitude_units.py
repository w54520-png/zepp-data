"""
一次性脚本：修正 workouts 表里 max_altitude / min_altitude 的历史 cm 单位 bug。

背景（M2 章节 2.3）：
  M1 时期的 fetch_workouts.py 用 clean() 处理海拔，没区分米和厘米。
  Zepp API 在某些 sport / 某些版本下，max_altitude 和 min_altitude 给的是**厘米**
  （典型 5000..900000），导致 workouts 表里 max_altitude 出现 884800 这种值
  （其实应该是 8848 m，珠峰高度）。

规则（与 fetch_workouts.parse_altitude_cm_to_m 一致）：
  |val| > 50_000  → 视为 cm，除以 100 得到 m
  |val| ≤ 50_000  → 直接当 m
  None / 哨兵     → 不动

用法：
  python3 fix_workout_altitude_units.py --dry-run    # 只打印，不改
  python3 fix_workout_altitude_units.py             # 真改

依赖：fetch_workouts.py 里的 parse_altitude_cm_to_m 和 clean()。
"""
from __future__ import annotations
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_workouts import parse_altitude_cm_to_m, clean

# 与 fetch_workouts 保持一致的哨兵
ALT_SENTINELS = (-20000.0, -1.0)

# 阈值：与 parse_altitude_cm_to_m 一致
CM_THRESHOLD = 50_000


def needs_conversion(value):
    """判断当前值是否需要 cm → m 修正。"""
    if value is None:
        return False
    try:
        n = float(value)
    except (TypeError, ValueError):
        return False
    # 哨兵值不动
    if n in ALT_SENTINELS:
        return False
    # 已经是合理 m 范围（≤ 50000）不动
    if abs(n) <= CM_THRESHOLD:
        return False
    return True


def fix_db(db_path: str, dry_run: bool) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT trackid, max_altitude, min_altitude FROM workouts")
    rows = cur.fetchall()
    if not rows:
        print("✓ workouts 表为空，无需处理")
        return

    fixes = []  # (trackid, field, old, new)
    for row in rows:
        tid = row["trackid"]
        for field in ("max_altitude", "min_altitude"):
            raw = row[field]
            if needs_conversion(raw):
                new_value = parse_altitude_cm_to_m(raw)
                fixes.append((tid, field, raw, new_value))

    if not fixes:
        print(f"✓ 扫了 {len(rows)} 条 workout，没有需要修正的海拔记录")
        return

    print(f"扫描 {len(rows)} 条 workout，发现 {len(fixes)} 条需要修正")
    # 打印前 10 条预览
    for tid, field, old, new in fixes[:10]:
        print(f"  {tid}  {field}: {old} → {new}")
    if len(fixes) > 10:
        print(f"  ... 还有 {len(fixes) - 10} 条")

    if dry_run:
        print("\n[dry-run] 不写入；正式跑删 --dry-run")
        return

    # 真改
    for tid, field, old, new in fixes:
        conn.execute(
            f"UPDATE workouts SET {field} = ? WHERE trackid = ?",
            (new, tid),
        )
    conn.commit()
    print(f"\n✓ 已修正 {len(fixes)} 条海拔记录")

    # 二次扫描确认
    cur = conn.execute("SELECT max_altitude, min_altitude FROM workouts")
    still_bad = 0
    for row in cur.fetchall():
        for v in (row["max_altitude"], row["min_altitude"]):
            if needs_conversion(v):
                still_bad += 1
                break
    if still_bad:
        print(f"⚠ 仍有 {still_bad} 条 > 50000 的值（可能是合法数据，待人工复核）")
    else:
        print("✓ 二次扫描确认：所有海拔都在合理 m 范围")

    conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="/root/.zepp-data/zepp.db")
    p.add_argument("--dry-run", action="store_true", help="只扫描，不写入")
    args = p.parse_args()
    if not Path(args.db).exists():
        print(f"✗ DB 不存在：{args.db}")
        return 1
    fix_db(args.db, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())