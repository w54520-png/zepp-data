# 术语表 / Glossary

> LLM 读 SKILL.md 时常被专有名词卡住的快速查表。所有术语都是 Zepp Cloud API / 同步管道里的实战概念。

---

## 健康指标类

| 术语 | 含义 | 详见 |
|---|---|---|
| **HRV** | Heart Rate Variability 心率变异性。逐次心跳间期（R-R interval）的微小变化。Zepp 用 RMSSD 时域指标。 | sleep-band-data.md |
| **RMSSD** | Root Mean Square of Successive Differences。HRV 的时域统计指标，**单位 ms**。Zepp `hrv_rmssd` 流就是这个值。晨起 HRV 是恢复状态的关键指标。 | sleep-band-data.md |
| **BMR** | Basal Metabolic Rate 基础代谢率（kcal/day）。Zepp API 只给活动卡路里，BMR 要本地算。 | calorie-bmr.md |
| **TDEE** | Total Daily Energy Expenditure 全天总消耗。`TDEE = BMR + 活动卡路里`。Zepp App 显示口径。 | calorie-bmr.md |
| **PAI** | Personal Activity Intelligence。Zepp 自有分数（0-100），基于过去 7 天心率推算的运动负荷综合分。≥ 100 算达标。 | normalizer-rules.md |
| **SpO2** | 血氧饱和度（%）。Zepp 端点返 3 种：click（单次）/ odi（氧减指数）/ apnea（呼吸暂停事件）。 | data-flows.md |
| **Hybrid Charge** | 体电荷 / Readiness。Zepp 自有恢复分数（基于 HRV + 睡眠 + 压力）。Zepp App 不显示低于阈值的值。 | data-flows.md |
| **Sport Load** | 训练负荷。Zepp 基于运动强度和持续时间算的训练压力指标。 | data-flows.md |
| **Respiratory Rate** | 呼吸率（次/分钟）。daily-level 流，数据稀（90 天仅 6 条）。 | data-flows.md |
| **VO2max** | 最大摄氧量（ml/kg/min）。Zepp 6 个候选字段名，范围 (1, 100)。 | fix-history.md |

---

## Zepp 数据流 / 事件名

| 术语 | 含义 | 详见 |
|---|---|---|
| **band_data** | Zepp 睡眠 + 逐分钟心率流名（`/v1/data/band_data.json`）。M2 章节 2.1。 | sleep-band-data.md |
| **ALL DAY STRESS** | Zepp 压力全天聚合流名（`/v1/sport/stressHistory.json` daily 部分）。 | data-flows.md |
| **stress_curve_samples** | 5min 粒度压力样本（M4.7 新表）。与睡眠 stage 时间维度对齐。 | data-flows.md |
| **DailyHealth** | Zepp 日聚合事件名（`daily_summary` 流内部聚合事件）。**1-2 天滞后**。 | normalizer-rules.md |
| **v2_events** | Zepp Cloud v2 事件总线（11 类：心率/HRV/SpO2/PAI/压力/体电荷/呼吸率/训练负荷 等）。 | data-flows.md |
| **user_events** | Zepp Cloud user 级别事件（3 类：user_profile / devices / members）。 | data-flows.md |
| **watch_stat** | Zepp 手表设备统计数据流（2 类）。 | data-flows.md |
| **heart_rate** | 心率流（auto 自动测量 + manual 主动测量）。 | data-flows.md |
| **weight** | 体重 / 体成分流（`weightRecords`）。11 个 BodyMetric 字段。 | body-weight.md |
| **workouts** | 运动历史 summary + detail。M1+M3 章节。 | workout-mapping.md |
| **food_daily** | 饮食按日累加流（M4.2 新增）。4 macro：calorie/protein/fat/carbs。 | data-flows.md |
| **vo2_max_daily** | VO2_MAX daily 表（M4.4 新增）。 | data-flows.md |

---

## Zepp API 端点

| 术语 | 含义 | 详见 |
|---|---|---|
| **`/v2/client/login`** | OAuth Step ② 端点（拿 app_token）。 | oauth-flow.md |
| **`/v1/client/app_tokens`** | OAuth Step ③ 端点（拿 refresh_token）。 | oauth-flow.md |
| **`/registrations/{phone}/tokens`** | OAuth Step ① 端点（拿 access_token）。 | oauth-flow.md |
| **`/v1/sport/run/history.json`** | 运动历史 summary 端点（`stop_track_id=9999999999`）。 | workout-mapping.md |
| **`/v1/sport/run/detail.json`** | 运动详情端点（GPS/分段/分圈/HR 漂移）。**rate limit 7 calls/burst**。 | fix-history.md |
| **`/v1/data/band_data.json`** | 睡眠 + 心率端点（`query_type=summary` / `detail`）。 | sleep-band-data.md |
| **`/v1/sport/stressHistory.json`** | 压力流（daily + 5min curve）。 | data-flows.md |
| **`/v1/sport/pai.json`** | PAI daily 端点（1-2d 滞后）。 | data-flows.md |
| **`/v1/sport/run/dailySummary.json`** | 步数/卡路里 daily 端点（1-2d 滞后）。 | data-flows.md |
| **`/users/{uid}/members/{mid}/weightRecords`** | 体重 / 体成分端点（时间参数用**秒**）。 | body-weight.md |
| **`/v1/user/deviceV2.json`** | 设备清单端点。 | data-flows.md |
| **`REGION_FALLBACK_CHAIN`** | `["api-mifit-cn3", "api-mifit-us3", "api-mifit-eu2", "api-mifit-sg2"]`（M4.5）。 | fix-history.md |

---

## Zepp 内部术语

| 术语 | 含义 | 详见 |
|---|---|---|
| **APP_NAME** | OAuth 客户端 ID。**必须是 `"com.xiaomi.hm.health"`**（Mi Fit 退役 client id）—— 否则会踢手机 App。 | oauth-flow.md |
| **user_id** | Zepp 账号用户 ID（数字字符串）。 | oauth-flow.md |
| **app_token** | OAuth Step ② 返回的 Zepp Cloud 访问 token。**TTL 30 天**。 | oauth-flow.md |
| **access_token** | OAuth Step ① 返回的 access token。**TTL 365 天**。 | oauth-flow.md |
| **login_token** | OAuth Step ② 中间凭证，用于 Step ③ 换 refresh。 | oauth-flow.md |
| **region_host** | OAuth 返的建议区域 host（如 `api-mifit-cn3.zepp.com`）。**是建议值不是保证值**（M4.5 fallback 链）。 | fix-history.md |
| **device_catalog** | Zepp 设备型号映射表（productId → 中文名）。`(新增)` | — |
| **GmActiveTime** | Zepp 设备最后一次连云端时间戳（来自 `deviceV2.json`）。用于判断设备是否在线。 | normalizer-rules.md |
| **Sport Catalog** | Zepp 运动类型映射表（type 数字 → 中文名）。**由 ZeppBridge 维护**（v7, 134 个已编号运动）。**不要自行映射**。 | workout-mapping.md |
| **source_scope** | 数据来源标记。`device`（真实设备实测） / `user_fused`（Zepp 推算） / `placeholder`（占位） / `computed_bmr:xxx`（本地算的）。 | normalizer-rules.md |

---

## 字段 / 数据库术语

| 术语 | 含义 | 详见 |
|---|---|---|
| **哨兵值** | 标记"未测"的特殊数字。**常见值：`-1` / `-20000` / `-200274` / `255` / `999`**。需要显式过滤，不能当 0 算。 | normalizer-rules.md |
| **stage mode** | 睡眠阶段模式。`5=deep` / `4=light` / `8=rem` / `11=rem(兼容)` / `7=awake` / 其他=unknown。 | sleep-band-data.md |
| **slp.st** | 睡眠 session 起始锚点（band_data JSON 字段）。**本地时间戳但无时区后缀**——必须从 `slp.tz` 推断。 | sleep-band-data.md |
| **slp.tz** | 睡眠 timezone 字段。3 形态：毫秒字符串 / IANA 名 / None（默认 Asia/Shanghai）。 | normalizer-rules.md |
| **timeZone** | weightRecords 流 timezone 字段。同样 3 形态。 | body-weight.md |
| **stop_track_id** | 运动历史分页用大数 **`9999999999`**——用 `start+100` 等小数字会返空 summary。 | workout-mapping.md |
| **trackid** | Zepp 活动唯一 ID（`workouts` 表 PK）。 | workout-mapping.md |
| **raw_payload** | 完整原始 JSON（`workouts` 表 debug 字段）。 | workout-mapping.md |
| **ts_ms** | Unix timestamp 毫秒数（统一存储格式）。**注意 weightRecords 是秒（10 位）必须 `*1000`**。 | body-weight.md |

---

## Workout Detail 解码术语

| 术语 | 含义 | 详见 |
|---|---|---|
| **runPosture** | 跑步姿势三参数：**GCT**（Ground Contact Time 触地时间 ms） / **VO**（Vertical Oscillation 垂直振幅 cm） / **VSR**（Vertical Speed Ratio 垂直速度比 %）。 | workout-detail decoder |
| **kilo_pace** | 公里配速云端分段（兜底用）。当 `currentDistance` 缺失时用 kilo_pace 切段。 | workout-detail decoder |
| **currentDistance** | 累计距离（首选分段依据）。`workout_splits` 表按此切段（每 km）。 | workout-detail decoder |
| **workout_splits** | 公里分段表（每 km 平均配速/HR/海拔）。 | workout-detail decoder |
| **workout_laps** | 分圈表（用户手动按 lap 键切的圈）。 | workout-detail decoder |
| **workout_pauses** | 暂停段表（auto-pause 触发的停顿）。 | workout-detail decoder |
| **workout_hr_drift** | HR 漂移分析（前 30min vs 后 30min HR 对比）。 | workout-detail decoder |
| **workout_route_points** | GPS 轨迹点（lat/lon/海拔/时间）。 | workout-detail decoder |
| **workout_samples** | 逐秒/逐分样本（HR/配速/海拔/步频）。 | workout-detail decoder |

---

## 同步管道术语

| 术语 | 含义 | 详见 |
|---|---|---|
| **normalizer** | `scripts/normalizer/*.py` 下的字段映射函数。负责把 Zepp JSON 转 SQLite 行。 | normalizer-rules.md |
| **storage** | `scripts/storage.py` 的 `upsert_*` 函数族。SQLite UNIQUE 约束对 NULL 视为不等 → `device_id or ""` 强制转空串。 | fix-history.md |
| **dedup** | `pull_to_sqlite.py dedup` 子命令 / `storage.py::dedup_measurements(conn)`。清理 daily 流重复。 | fix-history.md |
| **feature 探测** | 首次连续 3 个 trackid 返 empty 时打 `workout_detail_rate_limited=true` 标记，后续跳过该端点（M4.1）。 | fix-history.md |
| **diagnostic 计数器** | `_diag["detail_attempts"]` / `_diag["detail_throttled"]` / `_diag["detail_fallback_regions"]` 写入 sync summary（M4.1）。 | fix-history.md |
| **baseline_window_days** | insight 基线窗口（M3：30 → 180）。180 天对运动（每周 2-3 场）合适。 | fix-history.md |
| **comparison_window_days** | 周报比较窗口（M3：周报 vs 前 28 日）。 | fix-history.md |
| **source_scope** | 见上"Zepp 内部术语"。 | — |

---

## 阶段 / 里程碑

| 术语 | 含义 | 详见 |
|---|---|---|
| **M1** | workout summary 字段补齐 + 测试骨架 + 海拔单位修复（cm→m）。 | fix-history.md |
| **M2** | 睡眠解析 + 体重流 + 字段对照表 + BMR 增强。 | fix-history.md |
| **M3** | workout 详情 + insights 周报 + nap session + dashboard 升级。 | fix-history.md |
| **M4** | 限流应对 + 饮食流 + VO2max + 国际服 + dashboard 趋势线。 | fix-history.md |
| **M5 候选** | 拉最新数据必须先 OAuth 再 sync / DST 探测 等。 | normalizer-rules.md |

---

## 易混淆术语对照

| 看起来像 | 实际差异 |
|---|---|
| `altitude_ascend` (Zepp Cloud) | **厘米** 单位（必须 `// 100`） |
| `dis_m` (Zepp Cloud) | **米** 单位（直接用） |
| `generatedTime` (weight 流) | **秒**（10 位数字，必须 `* 1000`） |
| `ts_ms` (其他流) | **毫秒**（13 位数字） |
| `data_hr[k]` (band_data) | **UTC k 分钟** 的心率（不是本地时间） |
| `slp.st` (band_data) | **本地时间戳**但无时区后缀 |
| `gender=0` (Zepp) | **女**（⚠️ Zepp 反人类约定） |
| `gender=1` (Zepp) | **男** |
| `age` (PAI raw) | **Zepp App 算的，可能错**——以 `members.birthday` 为准 |
| `summary` (workouts JSON) | 真正的字段在 `summary[]` 数组里 |
| `summary` (weight JSON) | 真正的字段在 `summary{}` 对象里 |

---

**更多**：Zepp 内部字段命名不一致（cn3 用驼峰 / us3 用全小写 / eu2 用下划线）—— normalizer 必须做**多字段 fallback**，详见 [normalizer-rules.md](normalizer-rules.md)。