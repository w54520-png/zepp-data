# 数据流支持

> 拆分自 SKILL.md。M1+M2+M3+M4 后支持的 21 类 Zepp Cloud 数据流 + user_profile 字段对照 + 重要警示。

---

## 数据流支持总览

M1+M2+M3+M4 后共支持 **21 类数据流**（v2_events × 11 + user_events × 3 + watch_stat × 2 + heart_rate + band_data + weight + workout_detail + workout_history + devices + members + food + vo2_max + stress_curve）。

17 个核心流（M4 前）一览：

| 流 | 类型 | 源端点 | 触发 | 限制 | 字段数 |
|---|---|---|---|---|---|
| hrv_rmssd | HRV (sample) | `/v2/health/heartRateVariabilityTimeSerie.json` | cron | - | 1 (ms) |
| blood_oxygen (click + odi + apnea) | SpO2 (daily+sample) | `/v2/health/spO2History.json` | cron | - | 4 种 |
| pai | PAI (daily) | `/v1/sport/pai.json` | cron | 1-2d 滞后 | 13 |
| all_day_stress | 压力 (daily+curve) | `/v1/sport/stressHistory.json` | cron | - | 8 + 5min 曲线 |
| hybrid_charge | 体电荷 (daily) | `/v2/health/hybridCharge.json` | cron | <阈值 Zepp App 不显示 | 4 |
| daily_summary | 步数/卡路里 (daily) | `/v1/sport/run/dailySummary.json` | cron | 1-2d 滞后 | 6 |
| respiratory_rate | 呼吸率 (daily) | `/v1/health/respiratoryRate.json` | cron | 数据稀（90 天仅 6 条） | 3 (avg/min/max) |
| sport_load | 训练负荷 (daily) | `/v1/sport/sportLoad.json` | cron | - | 5 |
| heart_rate | 心率 (auto/manual) | `/v2/health/heartRateHistory.json` | cron | - | (接口) |
| **band_sleep** | 睡眠 (session+stage+detail) | `/v1/data/band_data.json?query_type=summary\|detail` | cron (M2) | 1440 字节 HR 解码 | 3 张表 (sleep_sessions + sleep_stage_slices + heart_rate_band_samples) |
| **weight** | 体重/体成分 (sample) | `/users/{uid}/members/{mid}/weightRecords` | cron (M2) | 时间参数用**秒**（10 位） | 11 BodyMetric (weight/bmi/fatRate/metabolism…) |
| workouts (summary) | 运动历史 (M1) | `/v1/sport/run/history.json` | cron (M1) | `stop_track_id=9999999999` | 15+ 字段 |
| workouts (detail) | GPS/分段/分圈 (M3) | `/v1/sport/run/detail.json?trackid=` | 手动 (M3) | **rate limit**（7 次后 sleep 1-2s）| 6 张表 (route_points/samples/splits/laps/pauses/hr_drift) |
| workout_insights | 个人基线洞察 (M3) | 派生自 workouts | 手动 (M3) | 基线窗口 180 天 | 5 fact / workout |
| weekly_reports | 7 日健康周报 (M3) | 派生自 measurements | 手动 (M3) | 比较窗：7 日 vs 前 28 日 | 7 fact / 周 |
| devices | 设备清单 | `/v1/user/deviceV2.json` | cron | - | (设备列表) |
| user_profile | 生日/性别/身高/体重 | `/users/{uid}/members` | cron | **gender 反约定**（0=女 1=男）| 6 |

---

## user_profile 表（Zepp members 流）

来自 Zepp Cloud `GET /users/{id}/members` 端点。

| 字段 | 含义 | 示例 |
|---|---|---|
| member_id | `-1` 是账号主；其他数字是家庭成员 | `-1` / `1699075106000` |
| nickname | 用户昵称 | `"example_user"` |
| birthday | 生日 YYYY-MM（**无日**）| `"1990-01"` |
| **gender** | **⚠️ 反人类约定：0=女 1=男** | `1` |
| height | 身高 cm | `175.0` |
| weight | 体重 kg | `70.0` |

**重要警示**：

- **gender 是 Zepp 的反人类约定**（多数系统 0=男 1=女，**Zepp 反过来**）—— AI 分析时**注意翻转**
- **`age` 字段在 PAI raw payload 里也存在**（整数年龄），但**是 Zepp App 算的，可能错**（用户报告 1989 年生但 PAI age=38）——**以 `members.birthday` 为准**
- **Zepp 不返回生日 "日" 字段**——只到月份（YYYY-MM）——真实生日日需要从 Zepp App 本地缓存拿

**多成员场景**：如果一个 Zepp 账号有家庭成员共享手环，每个成员都有自己的 `member_id`、`birthday`、`gender`、`height`、`weight` —— `member_id='-1'` 是账号持有人（你自己），其他数字是家人。

---

## 数据流类型区分

### daily-level 流有同步滞后

Zepp Cloud 对 daily-level 流（steps / calories / PAI / sport_load / device_resting_hr / hybrid_charge）的数据**有 1-2 天滞后**。例如：
- 今天（9-19）早上 8 点 sync — 步数显示 0（实际上你已经走了 8000 步）
- 明天（9-20）早上 sync — 9-19 步数会显示完整值

这是 Zepp Cloud 服务端聚合延迟，**不是 skill bug**。**用户体验**：当天数据看 Zepp App，不要看 skill 看板。

### HRV / 压力是 sample-level 流

`hrv_rmssd` / `stress` / `spo2` 端点返回**分钟级样本数组**（不是日聚合）：
- 一条 `hrv_rmssd` record = 一个 5-min 测量点
- 一天可能有 50-200 个 sample
- 看板显示**日均**——但 Zepp App 的"日均 HRV"算法可能不同（用特定时段如晨起 HRV）
- **如果 Zepp App HRV 与 DB HRV 日均差很大**——这是 Zepp App 算法不同，不是 bug

### HYBRIDCHARGE 阈值

Zepp App 不显示 HYBRIDCHARGE 低于某阈值的值。DB 里 `hybrid_charge_intel=3` 的日子 Zepp App 显示空——**正常**。

---

## M4 新增流

| 流 | 触发 | 改动 |
|---|---|---|
| **food_daily** | cron | `normalizer/food.py` 4 个 macro（calorie/protein/fat/carbs）按日累加；5 种 envelope 兼容 |
| **vo2_max_daily** | cron | `normalizer/vo2_max.py`：6 字段候选（vo2Max/vo2max/vo2_max/vo2value/vo2_score/vo2_max_value）；范围 (1,100) |
| **stress_curve_samples** | cron | `normalizer/stress.py` 5min sample-level 解析；与睡眠 stage 切片对齐 |
| **国际服 region fallback** | cron | `ZeppClient(try_regions_fallback=True, base_url_override=...)`；fallback 链 cn3→us3→eu2→sg2 |

**实测 sync 结果**（2026-09-24）：
- `food_daily`：CN 国服实测 `items=[]`（能力探测标记 `no_records`）
- `vo2_max_daily`：CN 国服实测 `items=[]`（同上）
- `stress_curve_samples`：按日累加 ≈ 200+ sample（5min 粒度 × 7 日）

---

## 完整字段对照表

每个 Zepp Cloud endpoint → SQLite 字段的完整映射（含范围、单位、哨兵过滤）见：

**📖 [api-field-mapping.md](api-field-mapping.md)**

包含 14+ 个流：
1. 心率（自动 / 运动）
2. HRV RMSSD
3. 血氧（Click / ODI / OSA Event）
4. 压力（All-Day + 曲线）
5. PAI / 每日健康
6. 体电荷 / Readiness
7. 呼吸率
8. 训练负荷
9. 每日汇总
10. 运动历史（workouts）
11. 睡眠（band_data）—— M2 2.1
12. 体重 / 体成分 —— M2 2.2
13. 用户档案 / 设备清单
14. VO2_MAX —— M4 新增
15. Food（饮食按日累加）—— M4 新增
