# 运动历史与 Sport Catalog

> 拆分自 SKILL.md。sport type 映射表 + 8 个证据来源 + 不要自行映射的警告 + workouts 表结构 + Zepp API 关键坑。

---

## 运动历史（重要）

Zepp Cloud 通过 `/v1/sport/run/history.json` 暴露所有运动历史（不只是跑步）。每个活动有 `type` 数字字段标识运动类型。

### ⚠️ 重要：type 数字映射不可猜

| type | 显示运动（Zepp App） | 我之前错猜成 |
|---|---|---|
| 1 | 户外跑步 | ✅ |
| 6 | **健走** | ❌ "室内跑" |
| 8 | 跑步机 | ❌ "走路" |
| 13 | **健走** | ❌ "跑步机" |
| 14 | 泳池游泳 | ✅ |
| **22** | **徒步** | ✅（zepp-mcp 验证过）|
| 52 | 力量训练 | ✅ |

**Zepp Cloud 数字 type ≠ Zepp OS 设备协议 type**——同一数字在不同协议里含义不同。自行映射 100% 会出错。

**正确做法**：用 `sport_catalog.py` 翻译。本目录带了 ZeppBridge 项目维护的官方 `sport_catalog.json`（v7, 2026-09-10），含 134 个已编号运动 + 证据来源（cloud_verified / user_reported / extended_catalog 等）。

---

## 用法

```bash
# 1. 拉取所有运动历史到 workouts 表
python3 fetch_workouts.py                     # 拉所有
python3 fetch_workouts.py --from 2025-01-01   # 只拉 2025+

# 2. 查询
python3 query_zepp.py workouts                # 按类型汇总
python3 query_zepp.py workouts --top distance --limit 5   # 最远 5 次
python3 query_zepp.py workouts --top climb --limit 5      # 爬升最多 5 次
python3 query_zepp.py workouts --top calorie --limit 5    # 烧最多 5 次
python3 query_zepp.py workouts --list --sport 徒步         # 所有徒步
python3 query_zepp.py workouts --list --sport 健走 --from 2024-01-01 --to 2024-12-31 --limit 5

# 3. metric 查询支持 --only-measured（过滤 Zepp 推算值）
python3 query_zepp.py metric --metric device_resting_hr --days 30           # 默认：全部
python3 query_zepp.py metric --metric device_resting_hr --days 30 --only-measured  # 只看手表实测
```

---

## workouts 表结构

| 字段 | 含义 |
|---|---|
| `trackid` (PK) | Zepp 活动 ID |
| `sport_code` | Zepp API 数字 type |
| `sport_key` | 英文 key（'walking' / 'hiking' / 'ride'） |
| `sport_zh` | **中文名**（"健走" / "徒步" / "户外骑行"）|
| `end_time_ts` / `end_time_iso` | 结束时间 |
| `dis_m` | 距离（米）|
| `run_s` | 时长（秒）|
| `calorie` | 卡路里 |
| `avg_pace` | 平均配速 (m/s) |
| `avg_frequency` | 步频 (steps/min) |
| `altitude_ascend` / `_descend` | 爬升 / 下降 (m) |
| `max_altitude` / `min_altitude` | 最高 / 最低海拔 |
| `avg_heart_rate` / `max_heart_rate` / `min_heart_rate` | 心率 |
| `city` / `location` | 城市 / GPS location code |
| `raw_payload` | 完整原始 JSON（debug 用）|

---

## Zepp API 关键坑（已踩过）

1. **`stop_track_id` 必须用大数 `9999999999`**——用 `start+100` 等小数字会返空 summary。
2. **`code` 字段返回 `None` 不是错误**——用 `data.get('summary', [])` 而不是 `data.get('code') == 1` 判断。
3. **Zepp 服务端会重置 endpoint 状态**：刚 refresh 完 token 可能立即返 0 条，过一会又正常。
4. **分页用 `next` 字段**：如果 next != -1 就用 next 作为新的 start_track_id。
5. **哨兵值**：`altitude_ascend = -20000` / `-1` / `0` 都是"无数据"，不是真值。`sport_catalog.py` 帮你处理了。
6. **海拔字段单位是厘米**（M1 修 bug）——`altitude_ascend` / `_descend` / `max_altitude` / `min_altitude` 4 个字段必须 `// 100` 才是米。其它运动字段（`dis_m` 距离米 / `run_s` 时长秒）单位是对的——只有海拔字段是厘米口径。这是 Zepp Cloud API 字段单位不统一的坑。

---

## cron 自动拉

`zepp_cron_sync.sh` 已经包含 `fetch_workouts.py --from 2025-01-01`，每天 8h 一次自动同步最近运动历史（更老的活动不会重复拉）。

---

## 未来扩展

- `fetch_workouts.py` 加 `--full` 拉所有（不只是 2025+）
- `workouts` 表加 GPS 轨迹字段（需要拉 `/v1/sport/run/detail.json?trackid=xxx`，每个活动多一次 API call）—— M3 已实现（`workout_detail.py` 1261 行 → 6 张表）
- 看板 `dashboard.py` 加运动历史 tab

---

## sport_catalog.json 证据来源

`sport_catalog.json` 标注每个 sport 的证据来源：
- `cloud_verified` — Zepp Cloud API 验证过
- `user_reported` — 用户反馈
- `extended_catalog` — ZeppBridge 扩展 catalog
- `unspecified` — 未指定

**重要**：catalog 内 134 个已编号 sport + 100+ 无编号 sport。如果遇到 catalog 没收录的 type，**先加到 `unspecified` 而不是自己映射**——保持 catalog 完整性。
