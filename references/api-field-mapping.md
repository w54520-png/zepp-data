# Zepp API → SQLite 字段对照表

> 本文档记录每个 Zepp Cloud 流（endpoint / event type）的原始字段 → SQLite 字段映射。
> 所有映射都来自**真实 API 响应**（不是文档猜测）。
> 抓取时间：2026-09-23

---

## 目录

1. [心率（自动测量 / 运动中）](#1-心率自动测量--运动中)
2. [HRV RMSSD](#2-hrv-rmssd)
3. [血氧（Click / ODI / OSA Event）](#3-血氧click--odi--osa-event)
4. [压力（All-Day Stress + 5min 曲线）](#4-压力all-day-stress--5min-曲线)
5. [PAI / 每日健康](#5-pai--每日健康)
6. [体电荷 / Readiness](#6-体电荷--readiness)
8. [呼吸率](#8-呼吸率)
9. [训练负荷](#9-训练负荷)
10. [每日汇总（步数/卡路里）](#10-每日汇总步数卡路里)
11. [运动历史（workouts）](#11-运动历史workouts)
12. [睡眠（band_data）](#12-睡眠band_data)
13. [体重 / 体成分](#13-体重--体成分)
14. [用户档案 / 设备清单](#14-用户档案--设备清单)

---

## 1. 心率（自动测量 / 运动中）

**端点**：`GET /users/{user_id}/heartRate`
**Surface**：`heart_rate`
**Surface 字段**：无 `event_type` / `sub_type`
**时间参数**：毫秒（自动检测秒传入并转换）
**响应包络**：`{code: 1, data: {items: [...]}}`

### 请求参数

| 参数 | 类型 | 说明 |
|---|---|---|
| `startTime` | ms | 起始时间 |
| `endTime` | ms | 结束时间 |
| `limit` | int | 默认 1000 |
| `type` | int | `2` = 自动测量 / `1000` = 运动中 |

### 响应字段

| API 字段 | 含义 | 类型 | 范围 | SQLite 字段 |
|---|---|---|---|---|
| `timestamp` / `time` | 测量时间 | ms | – | `ts_ms` |
| `value` / `heartRate` / `hr` | 心率 | bpm | (1, 300)（0=未测）| `value`（metric=heart_rate）|
| `deviceId` / `device_id` | 设备 ID | str | – | `device_id` |

### 规范化函数

- `normalize_heart_rate` in `normalizer/wellness.py`
- 0 bpm 哨兵 → 丢弃
- >300 bpm → 丢弃（实测生理上界）

---

## 2. HRV RMSSD

**端点**：`GET /v2/users/me/events`
**Surface**：`v2_events`
**event_type**：`HRVRMSSD`
**sub_type**：`real_data`
**响应包络**：`{items: [{value: {startTime, samples}, deviceId}]}`

### 响应字段

| API 字段 | 含义 | 类型 | 范围 | SQLite 字段 |
|---|---|---|---|---|
| `value.startTime` | 本批 sample 基准时间 | ms | – | 配合 sample.s 算出 `ts_ms` |
| `value.samples[].s` / `offset` | 相对 startTime 的偏移 | ms | `[-7d, +7d]`（绝对值超 7d 跳过）| 加到 startTime |
| `value.samples[].hrv` / `rmssd` | RMSSD 值 | ms | (1, 400] | `value`（metric=hrv_rmssd）|
| `deviceId` | 设备 ID | str | – | `device_id` |

### 规范化函数

- `normalize_hrv_rmssd`
- `_MAX_HRV_OFFSET_MS = 7 * 86400 * 1000`（超过 7 天的偏移视为手表断电复同步伪值）

---

## 3. 血氧（Click / ODI / OSA Event）

**端点**：`GET /users/{user_id}/events`（Click + ODI）
**端点**：`GET /users/{user_id}/events/dateString`（OSA Event 夜间型）
**Surface**：`user_events`
**event_type**：`blood_oxygen`
**sub_type**：`click` / `odi` / `osa_event`

### 3.1 Click（单次手动测量）

| API 字段 | 含义 | 类型 | 范围 | SQLite 字段 |
|---|---|---|---|---|
| `extra` (JSON string) | 嵌套数据 | – | – | – |
| `extra.spo2` / `extra.value` | 血氧 | % | [50, 100] | `value`（metric=spo2）|
| `extra.timestamp` / `timestamp` | 测量时间 | ms | – | `ts_ms` |
| `extra.spo2History[]` | 60 秒历史 | array | – | `extra_json.spo2_history` |
| `deviceId` | 设备 ID | str | – | `device_id` |

### 3.2 ODI（夜间血氧事件，每日汇总）

| API 字段 | 含义 | 类型 | 范围 | SQLite metric |
|---|---|---|---|---|
| `odi` | ODI 指数 | events/h | [0, 100] | `spo2_odi` |
| `odiNum` | 事件数 | count | [0, 1000] | `spo2_odi_events` |
| `score` | 夜间评分 | score | [0, 100] | `spo2_night_score` |
| `cost` | 测量时长 | 秒 | [60, 86400] | `spo2_measured_minutes`（÷60）|
| `date` / `day` / `dayId` | 日期 | YYYY-MM-DD | – | `date` |

### 3.3 OSA Event（单次呼吸暂停）

| API 字段 | 含义 | 类型 | 范围 | SQLite metric |
|---|---|---|---|---|
| `extra.spo2_decrease` / `extra.spo2Decrease` | 最低血氧 | % | [50, 100] | `spo2_apnea_low` |
| `extra.timestamp` | 事件时间 | ms | – | `ts_ms` |

---

## 4. 压力（All-Day Stress + 5min 曲线）

**端点**：`GET /users/{user_id}/events`
**Surface**：`user_events`
**event_type**：`all_day_stress`
**sub_type**：无

### 顶层字段（每日汇总）

| API 字段 | 含义 | 类型 | 范围 | SQLite metric | unit |
|---|---|---|---|---|---|
| `avgStress` | 平均压力 | score | [0, 100] | `stress` | score |
| `minStress` | 最低压力 | score | [1, 100] | `stress_min` | score |
| `maxStress` | 最高压力 | score | [1, 100] | `stress_max` | score |
| `relaxProportion` | 放松比例 | % | [0, 100] | `stress_relax_pct` | % |
| `normalProportion` | 普通比例 | % | [0, 100] | `stress_normal_pct` | % |
| `mediumProportion` | 中等比例 | % | [0, 100] | `stress_medium_pct` | % |
| `highProportion` | 高压比例 | % | [0, 100] | `stress_high_pct` | % |

### 曲线字段（5min 间隔）

`item.data` 是 JSON 字符串（**不是 base64**），数组 `[{time: ms, value: 1..100}]`。

| API 字段 | 含义 | 类型 | 范围 | SQLite 字段 |
|---|---|---|---|---|
| `data[i].time` | 时间 | ms | – | `ts_ms`（metric=stress）|
| `data[i].value` | 压力瞬时值 | score | [1, 100] | `value` |

---

## 5. PAI / 每日健康

**端点**：`GET /users/{user_id}/events`
**Surface**：`user_events`
**event_type**：`PaiHealthInfo`
**sub_type**：无

### 字段（13 个，全带范围）

| API 字段 | 含义 | 类型 | 范围 | SQLite metric | unit |
|---|---|---|---|---|---|
| `dailyPai` | 当日 PAI | – | [0, 500] | `pai_daily` | pai |
| `lowZonePai` | 低区 PAI | – | [0, 500] | `pai_low_zone` | pai |
| `mediumZonePai` | 中区 PAI | – | [0, 500] | `pai_medium_zone` | pai |
| `highZonePai` | 高区 PAI | – | [0, 500] | `pai_high_zone` | pai |
| `maxHr` | 最大心率 | bpm | [100, 240] | `device_max_hr` | bpm |
| `restHr` | 静息心率 | bpm | [25, 120] | `device_resting_hr` | bpm |
| `totalPai` | 累计 PAI | – | [0, 1000] | `pai_total` | pai |
| `lowZoneMinutes` | 低区分钟 | min | [0, 1440] | `pai_low_zone_minutes` | min |
| `mediumZoneMinutes` | 中区分钟 | min | [0, 1440] | `pai_medium_zone_minutes` | min |
| `highZoneMinutes` | 高区分钟 | min | [0, 1440] | `pai_high_zone_minutes` | min |
| `lowZoneLowerLimit` | 低区下限 | bpm | [40, 240] | `pai_low_zone_lower_hr` | bpm |
| `mediumZoneLowerLimit` | 中区下限 | bpm | [40, 240] | `pai_medium_zone_lower_hr` | bpm |
| `highZoneLowerLimit` | 高区下限 | bpm | [40, 240] | `pai_high_zone_lower_hr` | bpm |

### 占位设备警告

`deviceId == "single-device-firmware"` 时是 Zepp Cloud 推算值（不是手表实测），标记为 `placeholder` source_scope。

---

## 6. 体电荷 / Readiness

**端点**：`GET /v2/users/me/events`
**Surface**：`v2_events`
**event_type**：`Charge`
**sub_type**：`insight_data`

### 字段

| API 字段 | 含义 | 类型 | 范围 | SQLite metric | unit |
|---|---|---|---|---|---|
| `value.hrvScore` | HRV 准备度 | score | [0, 100] | `hrv_readiness` | score |
| `value.rhrScore` | 静息心率准备度 | score | [0, 100] | `rhr_readiness` | score |
| `value.phyScore` | 身体准备度 | score | [0, 100] | `physical_readiness` | score |
| `value.mentScore` | 心理准备度 | score | [0, 100] | `mental_readiness` | score |
| `value.rdnsScore` | 综合准备度 | score | [0, 100] | `readiness_score` | score |
| `value.afibScore` | AFib 评分 | score | [0, 100] | `afib_score` | score |
| `value.ahiScore` | AHI 评分 | score | [0, 100] | `ahi_score` | score |
| `value.skinTempScore` | 皮温评分 | score | [0, 100] | `skin_temp_score` | score |
| `value.hrvBaseline` | HRV 基线 | ms | [0, 254] | `hrv_baseline` | ms |
| `value.rhrBaseline` | RHR 基线 | bpm | [0, 254] | `rhr_baseline` | bpm |
| `value.ahiBaseline` | AHI 基线 | events/h | [0, 100] | `ahi_baseline` | events/h |
| `value.sleepHRV` | 睡眠 HRV | ms | [0, 250] | `sleep_hrv` | ms |
| `value.sleepRHR` | 睡眠 RHR | bpm | [0, 120] | `sleep_rhr` | bpm |
| `value.afibInsight` | AFib 提示 | score | [0, 100] | `afib_insight` | score |
| `value.rdnsInsight` | 准备度提示 | score | [0, 100] | `readiness_insight` | score |
| `value.skinTempInsight` | 皮温提示 | score | [0, 100] | `skin_temp_insight` | score |
| `value.skinTempCalibrated` | 校准皮温差 | delta_c | [-50, 100] | `skin_temp_calibrated` | delta_c |
| `value.samples[].total` | 体电荷总值 | score | [0, 100] | `hybrid_charge` | score |
| `value.samples[].physical` | 体能 | score | [0, 100] | `physical_charge` | score |
| `value.samples[].mental` | 精神 | score | [0, 100] | `mental_charge` | score |
| `value.samples[].insight` | Zepp 提示值 | score | [0, 100] | `hybrid_charge_intel` | score |
| `value.samples[].insightId` | 提示 ID | id | – | `hybrid_charge_intel_id` | id |
| `value.samples[].diff` | 与昨日差 | delta | [-100, 100] | `hybrid_charge_intel_diff` | delta |

---

## 8. 呼吸率

**端点**：`GET /v2/users/me/events`
**Surface**：`v2_events`
**event_type**：`RespiratoryRate`
**sub_type**：`real_data`

### 字段

| API 字段 | 含义 | 类型 | 范围 | SQLite metric |
|---|---|---|---|---|
| `value.measurements` (base64) | 1440 字节（一天分钟级） | bytes | – | – |
| 字节值 | 每分钟呼吸 | brpm | [4, 60]（0=未测）| `respiratory_rate`（日均）/ `respiratory_rate_min` / `respiratory_rate_max` |

---

## 9. 训练负荷

**端点**：`GET /v2/watch/users/{user_id}/WatchSportStatistics/SPORT_LOAD`
**Surface**：`watch_stat`

### 字段

| API 字段 | 含义 | 类型 | 范围 | SQLite metric |
|---|---|---|---|---|
| `dayId` / `day` | 日期 | YYYY-MM-DD | – | `date` |
| `currnetDayTrainLoad` | 当日负荷 | – | [0, 1000] | `sport_load_today` |
| `wtlSum` | 7 天累计 | – | [0, 10000] | `sport_load_7day_sum` |
| `wtlSumOptimalMin` | 最优下限 | – | [0, 10000] | `sport_load_optimal_min` |
| `wtlSumOptimalMax` | 最优上限 | – | [0, 10000] | `sport_load_optimal_max` |
| `wtlSumOverreaching` | 过载线 | – | [0, 10000] | `sport_load_overreaching` |

---

## 10. 每日汇总（步数/卡路里）

**端点**：`GET /v2/users/me/events`
**Surface**：`v2_events`
**event_type**：`DailyHealth`
**sub_type**：`summary`

### 字段（每条 sample 是 1 天）

| API 字段 | 含义 | 类型 | 范围 | SQLite metric | unit |
|---|---|---|---|---|---|
| `value.samples[].totalSteps` | 步数 | steps | [0, 200000] | `steps` | steps |
| `value.samples[].totalCalories` | 活动卡路里 | kcal | [0, 10000] | `calories` | kcal |
| `value.samples[].totalBurningDuration` | 燃烧时长 | min | [0, 1440] | `active_minutes` | min |
| `value.samples[].stepGoal` | 步数目标 | steps | [1, 200000]（0=未设）| `step_goal` | steps |
| `value.samples[].calorieGoal` | 卡路里目标 | kcal | [1, 10000] | `calorie_goal` | kcal |
| `value.samples[].burningDurationGoal` | 活跃目标 | min | [1, 1440] | `active_minutes_goal` | min |
| `value.samples[].dateString` | 日期 | YYYY-MM-DD | – | `date` |

### 注意

`calories` **不含 BMR/静息消耗**——只是活动卡路里。BMR 用 `compute_calorie_total.py` 补。

---

## 11. 运动历史（workouts）

**端点**：`GET /v1/sport/run/history.json`（所有 sport 类型）
**真实表**：`workouts`（独立于 measurements/raw_records，由 `fetch_workouts.py` 维护）

### 字段

| API 字段 | 含义 | 类型 | 单位 | SQLite 字段 | 转换 |
|---|---|---|---|---|---|
| `trackid` | 活动 ID | str | – | `trackid` (PK) | – |
| `type` | 运动类型 | int | – | `sport_code` | `sport_catalog.py` 翻译 |
| `end_time` | 结束时间 | 秒 | – | `end_time_ts` / `end_time_iso` | – |
| `dis` | 距离 | – | 米 | `dis_m` | – |
| `run_time` | 时长 | 秒 | – | `run_s` | – |
| `calorie` | 卡路里 | – | kcal | `calorie` | – |
| `avg_pace` | 平均配速 | – | m/s | `avg_pace` | – |
| `avg_frequency` | 步频 | – | steps/min | `avg_frequency` | – |
| `altitude_ascend` | 累计上升 | – | 米 | `altitude_ascend` | 哨兵 -20000/-1 → NULL |
| `altitude_descend` | 累计下降 | – | 米 | `altitude_descend` | 同上 |
| `max_altitude` | 最高海拔 | – | **可能是米或厘米** | `max_altitude` | `parse_altitude_cm_to_m` |
| `min_altitude` | 最低海拔 | – | **可能是米或厘米** | `min_altitude` | `parse_altitude_cm_to_m` |
| `avg_heart_rate` | 平均心率 | – | bpm | `avg_heart_rate` | – |
| `max_heart_rate` | 最大心率 | – | bpm | `max_heart_rate` | – |
| `min_heart_rate` | 最小心率 | – | bpm | `min_heart_rate` | – |
| `total_step` | 总步数 | – | – | `total_step` | – |
| `city` / `location` | 城市/GPS | str | – | `city` / `location` | – |
| `sport_mode` | 模式 | int | – | `mode` | 0=户外 / 1=室内 / 2=其他 |
| `bind_device` | 绑定设备 | str | – | `device_name` | 解析 `0:MILI_MONACO:...:...` 取第 2 段 |
| `heart_range` | 心率区间 | str | – | `hr_zone`（JSON） | `parse_heart_range` 解析 `"秒,bpm;秒,bpm;..."` |
| `raw_payload` | 完整原始 | JSON | – | `raw_payload` | – |

### 海拔单位规则（M2 修复）

`max_altitude` / `min_altitude` 单位在不同版本/不同 sport 下不同：

- |val| > 50,000 → 厘米，除以 100 得到米
- |val| ≤ 50,000 → 直接当米

### 修复脚本

历史 cm 单位数据：`scripts/fix_workout_altitude_units.py`（dry-run → 真改）

---

## 12. 睡眠（band_data）

**端点**：`GET /v1/data/band_data.json`
**query_type**：`summary` / `detail`
**响应**：可能压缩 + base64 编码的睡眠段

> M2 章节 2.1 实现。详细字段见 `normalizer/sleep.py` 的 docstring 与实现。
> 表结构：`sleep_sessions` + `sleep_stage_slices` + `heart_rate_band_samples`。
> 见 `scripts/storage.py` 的 SCHEMA。

### 关键字段

| API 字段 | 含义 | 类型 | 范围 | SQLite 表 |
|---|---|---|---|---|
| `summary` (B64) | base64 编码的睡眠摘要 | str | – | – |
| `start` | 睡眠开始 | 秒 | – | `sleep_sessions.start_ts` |
| `stop` | 睡眠结束 | 秒 | – | `sleep_sessions.end_ts` |
| `timeZone` | 时区 | str | "Asia/Shanghai" / "GMT+08:00" / 毫秒偏移 | `sleep_sessions.tz_offset_secs` |
| stage `mode` | 阶段模式 | int | 5=deep / 4=light / 8|11=rem / 7=awake / 其他=unknown | `sleep_stage_slices.mode` |
| `data_hr` | 逐分钟 HR | B64 bytes | 20..240 | `heart_rate_band_samples.bpm` |

---

## 13. 体重 / 体成分

**端点**：`GET /users/{user_id}/members/{member_id}/weightRecords`
**时间参数**：**秒**（其他流都是毫秒）
**Surface**：`static`-like，独立

> M2 章节 2.2 实现。详细字段见 `normalizer/body.py` 与表 `measurements`（stream=`weight`）。

### 字段（11 个 BodyMetric）

| API 字段 | 含义 | 类型 | 范围 | SQLite 字段 |
|---|---|---|---|---|
| `weight` | 体重 | kg | (0, 600] | `value`（stream=weight）|
| `bmi` | BMI | kg/m² | (10, 100] | `metric=bmi` |
| `height` | 身高 | cm | (50, 250] | `metric=height` |
| `bodyFatRate` / `fatRate` | 体脂率 | % | [3, 70] | `metric=body_fat_rate` |
| `bodyWaterRate` / `waterRate` | 水分率 | % | [20, 90] | `metric=body_water_rate` |
| `muscleMass` | 肌肉量 | kg | (0, 200] | `metric=muscle_mass` |
| `boneMass` | 骨量 | kg | (0, 20] | `metric=bone_mass` |
| `proteinRate` | 蛋白质率 | % | [5, 60] | `metric=protein_rate` |
| `visceralFat` | 内脏脂肪 | level | [1, 50] | `metric=visceral_fat` |
| `bmr` | 基础代谢 | kcal | (500, 5000] | `metric=bmr` |
| `bodyBalanceScore` / `balanceScore` | 身体平衡评分 | score | [0, 100] | `metric=body_balance_score` |
| `generatedTime` | 测量时间 | **秒**（不是 ms） | – | `ts_ms`（×1000）|
| `timeZone` | 时区 | str/int | "Asia/Shanghai" / "GMT+08:00" / 毫秒偏移 | 解析后存 tz_offset_secs |

### 时区解析（3 种形态）

- `"Asia/Shanghai"` → 28800 秒
- `"GMT+08:00"` → 28800 秒
- `28800000`（毫秒偏移） → 28800 秒

用 `normalizer.common.parse_timezone_text` / `offset_from_number`。

---

## 14. 用户档案 / 设备清单

### 14.1 用户档案（Zepp members）

**端点**：`GET /users/{user_id}/members`
**表**：`user_profile`（独立）

| API 字段 | 含义 | 类型 | 范围 | SQLite 字段 |
|---|---|---|---|---|
| `memberId` | 成员 ID | str | `"-1"`=账号主，其他=家庭成员 | `member_id` (PK) |
| `userId` | Zepp user_id | str | – | `user_id` |
| `nickname` | 昵称 | str | – | `nickname` |
| `birthday` | 生日 | str | `"YYYY-MM"`（**无日**）| `birthday` |
| `gender` | 性别 | int | **0=女 1=男（Zepp 反人类约定）** | `gender` |
| `height` | 身高 | cm | – | `height` |
| `weight` | 体重 | kg | – | `weight` |

### 14.2 设备清单

**端点**：`GET /users/{user_id}/devices`
**表**：`devices`（独立）

| API 字段 | 含义 | SQLite 字段 |
|---|---|---|
| `macAddress` | MAC | `mac_address` (PK) |
| `deviceType` | 类型码 | `device_type` |
| `deviceSource` | 来源 | `device_source` |
| `deviceId` | Zepp 内部 ID | `device_id` |
| `sn` | 序列号 | `sn` |
| `bindingStatus` | 绑定状态 | `binding_status` |
| `applicationTime` | 应用时间 | `application_time` |
| `lastStatusUpdateTime` | 状态更新时间 | `last_status_update_time` |
| `firmwareVersion` | 固件版本 | `firmware_version` |
| `additionalInfo.productId` | 产品 ID | `product_id` |
| `additionalInfo` | 其他 JSON | `extra_json` |

---

## 附录：Surface 类型枚举

| Surface | 端点模式 | 时间单位 | 例子 |
|---|---|---|---|
| `v2_events` | `/v2/users/me/events` | ms | hrv_rmssd, blood_pressure, Charge/insight_data, Food |
| `user_events` | `/users/{id}/events` | ms | PAI, all_day_stress, blood_oxygen |
| `user_events_date_string` | `/users/{id}/events/dateString` | ISO+IANA | 夜间 SpO2 (odi / osa_event) |
| `watch_stat` | `/v2/watch/users/{id}/WatchSportStatistics/...` | 日期 | SPORT_LOAD, VO2_MAX |
| `heart_rate` | `/users/{id}/heartRate` | ms/秒 | 心率自动 / 运动 |
| `static` | `/users/{id}/devices` `/users/{id}/members` | – | devices, members |
| `weight` | `/users/{id}/members/{mid}/weightRecords` | **秒** | 体重 / 体成分 |

---

## 15. 饮食（Food）

**端点**：`GET /v2/users/me/events?eventType=Food`
**Surface**：`v2_events`
**event_type**：`Food`
**sub_type**：无
**国服实测**：2026-09-23 拉取 90 天 / 365 天均返回 `items=[]`（CN 国服未启用饮食模块）。`capabilities.status=no_records`。

### 字段（4 个 macro，按日累加）

| API 字段候选 | metric | unit | 范围 |
|---|---|---|---|
| `calories` / `calorie` / `kcal` / `energy` | `intake_calories` | kcal | (1, 20000] |
| `protein` / `proteins` | `intake_protein_g` | g | [0, 1000] |
| `fat` / `fats` | `intake_fat_g` | g | [0, 1000] |
| `carbohydrate` / `carbohydrates` / `carbs` | `intake_carbs_g` | g | [0, 2000] |

---

## 16. VO2_MAX（每日拟合有氧能力）

**端点**：`GET /v2/watch/users/{user_id}/WatchSportStatistics/VO2_MAX`
**Surface**：`watch_stat`
**国服实测**：2026-09-23 拉取 180 天返回 `items=[]`（能力探测结果 `no_records`）。

### 字段

| API 字段候选 | metric | unit | 范围 |
|---|---|---|---|
| `vo2max` / `vo2Max` / `VO2_MAX` / `VO2_max` | `vo2max` | ml/kg/min | (1, 100] |
| `vo2_max_run` | `vo2max` | ml/kg/min | (1, 100] |
| `vo2_max_walking` | `vo2max` | ml/kg/min | (1, 100] |

**哨兵**：`<=0`（实测 `vo2max == -1` 涵盖 60% workout 数据；视为缺失）。
**数据来源**：云端日度拟合 → `source_scope=user_fused`。

### 日期解析

- `dayId`（YYYY-MM-DD）优先
- fallback：`updateTime` → UTC 转 YYYY-MM-DD

### 规范化函数

- `normalize_vo2_max` in `normalizer/vo2_max.py`

### 与 workout 内的 vo2max 关系

workouts summary 里的 `vo2max` 是**单次运动**值；本流是**日度拟合**值（云端根据当天心率/运动量计算）。两者并存，分别入库到 `measurements.metric='vo2max'`。

---

## 更新日志

- 2026-09-23：初版，覆盖 M2 之前的全部流（workouts、HRV、PAI、SpO2、压力、体电荷、呼吸、训练负荷、每日汇总、心率）
- 2026-09-23：M2 章节 2.4，新增睡眠（占位）和体重章节占位；睡眠字段细节见 2.1，体重字段细节见 2.2
- 2026-09-23：M4 章节 4.2，新增 Food（饮食）流章节
- 2026-09-23：M4 章节 4.4，新增 VO2_MAX 流章节