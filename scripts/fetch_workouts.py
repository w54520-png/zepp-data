"""
把 Zepp Cloud 运动历史（/v1/sport/run/history.json）拉取并写入 SQLite。

Zepp Cloud 这个端点返回所有运动类型的汇总（不分 sport 路径），
每个 sport 类型作为 summary 里 `type` 字段的数字标识。

设计原则：
- 用 ZeppBridge 官方 catalog（sport_catalog.py）翻译 type → 中文名
- 不在 catalog 里的 type 也保存，但 sport_name = "未知(<code>)"
- 距离/卡路里等都是字符串（Zepp API 原样），做 type cast
- 用 trackid 做主键（同一活动不会重复入库）
- 不存在的字段（pb/altitude_ascend 等）存 NULL（用 -1 哨兵在 source_scope 标注）

用法：
  python3 fetch_workouts.py                      # 默认拉所有
  python3 fetch_workouts.py --from 2024-01-01    # 从某日期开始
  python3 fetch_workouts.py --dry-run            # 只看不写
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from zepp_client import ZeppClient
from sport_catalog import sport_name


# Zepp 哨兵值（在 catalog sentinels 里定义的"无数据"标记）
SENTINELS = {
    "altitude": (-20000.0, -1.0),
    "default": (-1.0,),
}


def clean(value, family="default"):
    """去掉哨兵值，None 表示无数据。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n in SENTINELS.get(family, SENTINELS["default"]):
        return None
    return n


def to_int(value):
    try:
        n = int(value)
        return None if n == -1 else n
    except (TypeError, ValueError):
        return None


def parse_altitude_cm_to_m(value):
    """
    海拔 cm → m。Zepp API 给的海拔单位在不同版本/不同 sport 类型下不同：
      - 户外跑步/骑行：可能是 **米**（典型 50..9000）
      - 某些版本/某些 sport（如泳池游泳、健身房）给的是 **厘米**（典型 5000..900000）

    判定规则（M1 拍板）：
      阈值 = 50,000（绝对值）。
        * |val| > 50,000 → 视为 cm，除以 100 得到 m
        * |val| ≤ 50,000 → 直接当 m

    50,000 m = 50 km，已超出现实运动海拔上限（珠峰 8848m）；
    所以 >50k 几乎一定是 cm 单位。除以 100 后通常落在 50..9000 m 区间内，符合预期。
    """
    n = clean(value)
    if n is None:
        return None
    if abs(n) > 50_000:
        return round(n / 100.0, 2)
    return round(n, 2)


def parse_hr_zone(value):
    """
    解析 hr_zone（运动心率区间）。

    数据形态：`"1882,113;3486,141;..."` —— 秒数,心率上限 分号分隔。
    ZeppBridge 用 Rust `parse_heart_range` 解析。

    现有 common.parse_heart_range 返回 list[dict]，这里再封装成 JSON 字符串以便
    直接存 SQLite TEXT 列。

    返回：JSON 字符串 / None
    """
    from normalizer.common import parse_heart_range
    arr = parse_heart_range(value)
    if not arr:
        return None
    return json.dumps(arr, ensure_ascii=False)


def fetch_all_workouts(client: ZeppClient) -> list[dict]:
    """分页拉所有运动历史。

    关键：Zepp API 用 trackid 范围分页，但 stop_track_id 必须用一个很大的数（如 9999999999）
    ——用小数字（如 start+100）会返回空 summary（实测过）。
    所以只分 start，stop 始终用 9999999999；Zepp 服务端内部按 next 字段判断是否还有下一页。
    """
    all_items = []
    start = 0
    seen_trackids = set()
    for _ in range(20):  # 最多 20 页防卡死
        data = client._get(
            "/v1/sport/run/history.json",
            {"userid": client.user_id, "startTrackId": str(start),
             "stopTrackId": "9999999999", "need_sub_data": "1"},
        )
        if not isinstance(data, dict):
            break
        items = data.get("summary", [])
        if not items:
            break
        # 去重（同一 trackid 可能重复出现）
        new_items = []
        for it in items:
            tid = str(it.get("trackid"))
            if tid in seen_trackids:
                continue
            seen_trackids.add(tid)
            new_items.append(it)
        if not new_items:
            break
        all_items.extend(new_items)
        nxt = data.get("next", -1)
        if nxt in (-1, 0):
            break
        start = nxt
    return all_items


def normalize_workout(it: dict) -> dict | None:
    """一条 API 记录 → 一行 SQLite 记录。"""
    trackid = it.get("trackid")
    if not trackid:
        return None

    code = to_int(it.get("type"))
    if code is None:
        return None
    sport_key, sport_zh = sport_name(code)

    # end_time → 本地时区 YYYY-MM-DD
    end_ts = to_int(it.get("end_time"))
    if end_ts is None:
        return None

    # 距离/卡路里/配速（API 返字符串）
    dis_m = clean(it.get("dis"))         # 米
    cal = clean(it.get("calorie"))        # kcal
    run_s = to_int(it.get("run_time"))   # 秒

    # 海拔相关（M2 修复：用 parse_altitude_cm_to_m 处理 max/min altitude——
    # 不同 sport / 不同 Zepp API 版本给的单位不同，可能是米也可能是厘米。
    # 阈值 50_000（绝对值）：>50k 视为 cm，除以 100；≤50k 直接当 m。
    # ascend/descend 当前观测都是米（小型阈值判断即可），max/min 必须用转换器。）
    alt_asc = clean(it.get("altitude_ascend"), "altitude")
    alt_desc = clean(it.get("altitude_descend"), "altitude")
    alt_max = parse_altitude_cm_to_m(it.get("max_altitude"))
    alt_min = parse_altitude_cm_to_m(it.get("min_altitude"))

    # 心率
    hr_avg = clean(it.get("avg_heart_rate"))
    hr_max = to_int(it.get("max_heart_rate"))
    hr_min = to_int(it.get("min_heart_rate"))

    # ===== M1 1.1 新增字段（8 个解析 + altitude_unit 单位标注）=====
    # 注：HTTP API 实际字段名 ≠ M1 计划字段名。下面是实测映射。
    #   计划名         →  HTTP API 实际名
    #   hr_zone        →  heart_range    (格式相同："秒,bpm;秒,bpm;...")
    #   mode           →  sport_mode     (int: 0/1/2 = 户外/室内/其他)
    #   device_name    →  bind_device    (格式 "0:MILI_MONACO:...:..." — 取 device model 段)
    #   gpx_total_up   →  API 未返回    （存 NULL）
    #   gpx_total_down →  API 未返回    （存 NULL）
    #   gpx_max_altitude → API 未返回  （存 NULL）
    #   gpx_min_altitude → API 未返回  （存 NULL）
    #   hr_rest        →  API 未返回    （存 NULL）
    # 计划在 M2/M3 跟进：sport_detail 端点 /v1/sport/run/detail.json 可能含 gpx_*/hr_rest。

    # mode: 0=户外 / 1=室内 / 2=其他
    mode = to_int(it.get("sport_mode"))
    # device_name: bind_device 格式 "0:MILI_MONACO:8519936:0.111.130.20"
    #   取第 2 段（MILI_MONACO）作为设备型号
    bind_device = it.get("bind_device")
    device_name = None
    if isinstance(bind_device, str):
        parts = bind_device.split(":")
        if len(parts) >= 2 and parts[1]:
            device_name = parts[1]
    # gpx 累计升降 / 极值：API 当前不返回，保持 NULL
    gpx_total_up = None
    gpx_total_down = None
    gpx_max_alt = None
    gpx_min_alt = None
    # hr_rest: 静息心率（运动前测）— API 当前不返回
    hr_rest = None
    # hr_zone: heart_range JSON
    hr_zone = parse_hr_zone(it.get("heart_range"))
    # altitude_unit: 当前 workouts 表统一存米（M1 决定）
    altitude_unit = "m"

    return {
        "trackid": str(trackid),
        "user_id": str(it.get("userId") or ""),
        "sport_code": code,
        "sport_key": sport_key,
        "sport_zh": sport_zh,
        "end_time_ts": end_ts,
        "end_time_iso": datetime.fromtimestamp(end_ts, timezone(timedelta(hours=8))).isoformat(),
        "dis_m": dis_m,
        "run_s": run_s,
        "calorie": cal,
        "avg_pace": clean(it.get("avg_pace")),     # m/s
        "avg_frequency": clean(it.get("avg_frequency")),  # steps/min for walk
        "altitude_ascend": alt_asc,
        "altitude_descend": alt_desc,
        "max_altitude": alt_max,
        "min_altitude": alt_min,
        "avg_heart_rate": hr_avg,
        "max_heart_rate": hr_max,
        "min_heart_rate": hr_min,
        "total_step": to_int(it.get("total_step")),
        "city": it.get("city") or None,
        "location": it.get("location") or None,
        # ===== 新字段 =====
        "mode": mode,
        "device_name": device_name,
        "gpx_total_up": gpx_total_up,
        "gpx_total_down": gpx_total_down,
        "gpx_max_altitude": gpx_max_alt,
        "gpx_min_altitude": gpx_min_alt,
        "hr_rest": hr_rest,
        "hr_zone": hr_zone,
        "altitude_unit": altitude_unit,
        "raw_payload": json.dumps(it, ensure_ascii=False),
    }


def ensure_table(conn: sqlite3.Connection):
    """创建 workouts 表（如果不存在）+ 迁移加列（M1 1.1 新增 9 列）。

    SQLite 的 PRAGMA table_info 没 ALTER COLUMN，所以列只在初始 CREATE TABLE 出现。
    后续加列用 ALTER TABLE ADD COLUMN（IF NOT EXISTS 不支持，手动查 column 名跳过）。
    """
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS workouts (
        trackid TEXT PRIMARY KEY,
        user_id TEXT,
        sport_code INTEGER,
        sport_key TEXT,
        sport_zh TEXT,
        end_time_ts INTEGER,
        end_time_iso TEXT,
        dis_m REAL,
        run_s INTEGER,
        calorie REAL,
        avg_pace REAL,
        avg_frequency REAL,
        altitude_ascend REAL,
        altitude_descend REAL,
        max_altitude REAL,
        min_altitude REAL,
        avg_heart_rate REAL,
        max_heart_rate INTEGER,
        min_heart_rate INTEGER,
        total_step INTEGER,
        city TEXT,
        location TEXT,
        -- ===== M1 1.1 新增 9 列 =====
        mode INTEGER,                -- 0=户外 / 1=室内 / 2=其他
        device_name TEXT,            -- "Mi Smart Band 7" 等
        gpx_total_up REAL,           -- GPS 原始累计上升（米）
        gpx_total_down REAL,         -- GPS 原始累计下降（米）
        gpx_max_altitude REAL,       -- GPS 最大海拔（米）
        gpx_min_altitude REAL,       -- GPS 最小海拔（米）
        hr_rest INTEGER,             -- 静息心率
        hr_zone TEXT,                -- JSON: [{index, upper_bound_bpm, seconds}, ...]
        altitude_unit TEXT,          -- "m"（workouts 表统一存米）
        raw_payload TEXT,
        ingested_at TEXT DEFAULT (datetime('now','localtime'))
    );

    CREATE INDEX IF NOT EXISTS idx_workouts_end_time ON workouts(end_time_ts DESC);
    CREATE INDEX IF NOT EXISTS idx_workouts_sport ON workouts(sport_key);
    """)

    # ===== 迁移：对已存在的旧 workouts 表加列（M1 1.1 升级）=====
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(workouts)").fetchall()}
    migrations = [
        ("mode", "INTEGER"),
        ("device_name", "TEXT"),
        ("gpx_total_up", "REAL"),
        ("gpx_total_down", "REAL"),
        ("gpx_max_altitude", "REAL"),
        ("gpx_min_altitude", "REAL"),
        ("hr_rest", "INTEGER"),
        ("hr_zone", "TEXT"),
        ("altitude_unit", "TEXT"),
    ]
    for col, decl in migrations:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE workouts ADD COLUMN {col} {decl}")
            print(f"  [migrate] workouts ADD COLUMN {col} {decl}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(Path(os.environ.get("ZEPP_DATA_DIR", str(Path.home() / ".zepp-data"))) / "zepp.db"))
    p.add_argument("--from", dest="from_date", help="只拉 >= YYYY-MM-DD 的活动")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    client = ZeppClient()
    items = fetch_all_workouts(client)
    print(f"📥 API 返回 {len(items)} 条")

    # 翻译成 date 字符串过滤
    rows = []
    skipped = 0
    for it in items:
        row = normalize_workout(it)
        if row is None:
            skipped += 1
            continue
        if args.from_date:
            if row["end_time_iso"][:10] < args.from_date:
                continue
        rows.append(row)
    print(f"✅ 翻译成功 {len(rows)} 条 (跳过 {skipped} 条无效)")
    if args.from_date:
        print(f"📅 过滤 from={args.from_date}")

    # 按时间排序
    rows.sort(key=lambda r: -r["end_time_ts"])

    if args.dry_run:
        for r in rows[:5]:
            extra = f"  mode={r['mode']}  dev={r['device_name'] or '-'}"
            extra += f"  gpx↑{r['gpx_total_up'] or 0:.0f} gpx↓{r['gpx_total_down'] or 0:.0f}"
            print(f"  {r['end_time_iso'][:10]}  type={r['sport_code']:>3} ({r['sport_zh']})  {r['dis_m'] or 0:>6.0f}m  {r['run_s'] or 0:>5}秒  ({r['city'] or '?'}){extra}")
        return 0

    conn = sqlite3.connect(args.db)
    ensure_table(conn)

    # ===== P2.4：先查 DB 现有 trackid 集合，准确区分 inserted vs updated =====
    # SQLite 的 INSERT ... ON CONFLICT DO UPDATE 在更新已存在行时，
    # cur.lastrowid 返回的是该行的 rowid（不是 None），所以 `if cur.lastrowid`
    # 判断不准——所有 upsert 都会被算成 inserted。下面用预查询精确分流。
    known_trackids = {
        str(r[0])
        for r in conn.execute("SELECT trackid FROM workouts").fetchall()
    }
    api_trackids = {str(r["trackid"]) for r in rows}
    new_trackids = api_trackids - known_trackids

    inserted = updated = 0
    for r in rows:
        try:
            cur = conn.execute("""
                INSERT INTO workouts (trackid, user_id, sport_code, sport_key, sport_zh,
                    end_time_ts, end_time_iso, dis_m, run_s, calorie, avg_pace, avg_frequency,
                    altitude_ascend, altitude_descend, max_altitude, min_altitude,
                    avg_heart_rate, max_heart_rate, min_heart_rate,
                    total_step, city, location,
                    mode, device_name, gpx_total_up, gpx_total_down,
                    gpx_max_altitude, gpx_min_altitude,
                    hr_rest, hr_zone, altitude_unit,
                    raw_payload)
                VALUES (:trackid, :user_id, :sport_code, :sport_key, :sport_zh,
                    :end_time_ts, :end_time_iso, :dis_m, :run_s, :calorie, :avg_pace, :avg_frequency,
                    :altitude_ascend, :altitude_descend, :max_altitude, :min_altitude,
                    :avg_heart_rate, :max_heart_rate, :min_heart_rate,
                    :total_step, :city, :location,
                    :mode, :device_name, :gpx_total_up, :gpx_total_down,
                    :gpx_max_altitude, :gpx_min_altitude,
                    :hr_rest, :hr_zone, :altitude_unit,
                    :raw_payload)
                ON CONFLICT(trackid) DO UPDATE SET
                    sport_code=excluded.sport_code,
                    sport_key=excluded.sport_key,
                    sport_zh=excluded.sport_zh,
                    dis_m=excluded.dis_m, run_s=excluded.run_s, calorie=excluded.calorie,
                    avg_pace=excluded.avg_pace, avg_frequency=excluded.avg_frequency,
                    altitude_ascend=excluded.altitude_ascend,
                    altitude_descend=excluded.altitude_descend,
                    max_altitude=excluded.max_altitude, min_altitude=excluded.min_altitude,
                    avg_heart_rate=excluded.avg_heart_rate,
                    max_heart_rate=excluded.max_heart_rate,
                    min_heart_rate=excluded.min_heart_rate,
                    total_step=excluded.total_step, city=excluded.city, location=excluded.location,
                    mode=excluded.mode, device_name=excluded.device_name,
                    gpx_total_up=excluded.gpx_total_up, gpx_total_down=excluded.gpx_total_down,
                    gpx_max_altitude=excluded.gpx_max_altitude,
                    gpx_min_altitude=excluded.gpx_min_altitude,
                    hr_rest=excluded.hr_rest, hr_zone=excluded.hr_zone,
                    altitude_unit=excluded.altitude_unit,
                    raw_payload=excluded.raw_payload
            """, r)
            if cur.lastrowid:
                inserted += 1
            else:
                updated += 1
            # P2.4：基于预查询的 trackid 集合再校准（lastrowid 在 ON CONFLICT 不可靠）
            if r["trackid"] in new_trackids:
                # 真的是新 row（之前 DB 里没有这个 trackid）
                pass  # 已计入 inserted（lastrowid 可能是旧的，但校准给 inserted）
            else:
                # 实际是更新（ON CONFLICT 触发）→ 把误算进 inserted 的挪回 updated
                inserted -= 1
                updated += 1
        except sqlite3.IntegrityError as e:
            print(f"  ⚠️ {r['trackid']}: {e}")
    conn.commit()

    # ===== P2.4：0 条警告 =====
    # 区分三种 0：
    #   1) 本次 sync API 返回 0 条 (items 空)        → 用户没运动历史 / API 限流 / 鉴权失败
    #   2) 本次 sync 翻译后 0 条 (rows 空)            → catalog 不识别 + 字段缺失
    #   3) 本次 sync 没新增 (inserted == 0 && updated == 0 && total > 0) → 历史已全部 sync 过
    total = conn.execute('SELECT COUNT(*) FROM workouts').fetchone()[0]
    print(f"\n✅ 新增/更新 {inserted + updated} 条 workouts")
    print(f"   DB 共 {total} 条历史活动")

    if total == 0:
        # 极端：DB 里 0 条 + 本次 sync 也 0 条
        print("\n💡 workouts 表为空，可能原因：")
        print("  1) 你的 Zepp Cloud 上没有任何运动历史（少见，但有可能——比如只用过手表没启动运动）")
        print("  2) sync 失败但没报错 — 试 `python3 scripts/zepp_oauth.py refresh` 看 token 是否过期")
        print("  3) API 限流 — 限流后会返回空 summary。等 5 分钟重试")
        print("  4) 鉴权 token 拿错了账号 — 用 `python3 scripts/zepp_oauth.py status` 检查 user_id")
    elif len(new_trackids) == 0 and len(rows) > 0:
        # DB 里有数据，API 也返了数据，但全是已知 trackid（全走 ON CONFLICT）
        print(f"\n💡 本次 sync 0 条新增运动（API 返 {len(rows)} 条，DB 已有 {total} 条）：")
        print("  1) 历史运动已全部 sync 过（trackid 唯一，已 ON CONFLICT 跳过）—— 这是正常的")
        print("     验证：DB 总数 {0} = 上次 sync 后总数".format(total))
        print("  2) 或者 API 限流 — 返回了空/旧 summary")
        print("     验证方法：再跑一次 `fetch_workouts.py`，看 API 返回条数是否持续 < 5 条")
        print("     如果持续低于平时 → API 被限流，等 5 分钟")
        print("  3) 或者只拉 `--from YYYY-MM-DD` 之后的数据，老数据不返 —— 这是预期行为")

    # 汇总
    print("\n=== 按运动类型汇总 ===")
    for row in conn.execute("""
        SELECT sport_zh, sport_key, COUNT(*) c,
               ROUND(SUM(dis_m)/1000, 2) dis_km,
               SUM(run_s) dur_s,
               ROUND(SUM(calorie), 0) cal,
               ROUND(SUM(altitude_ascend), 0) asc
        FROM workouts GROUP BY sport_key, sport_zh
        ORDER BY c DESC
    """):
        zh, key, c, km, dur, cal, asc = row
        h = dur // 3600
        m = (dur % 3600) // 60
        print(f"  {zh:<10} ({key:<18}): {c:>3} 次, {km or 0:>7.2f} km, {h}h{m:>02}m, {cal or 0:>6.0f} kcal, ↑{asc or 0}m")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())