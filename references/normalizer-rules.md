# Normalizer 设计原则与流程约束

> 拆分自 SKILL.md。normalizer 设计原则、字段名核对流程、避免陷阱、M2 拍板的 6 条实施约束、M5 候选。

---

## 实施流程约束（M2 拍板，给未来的 sub-agent）

写新 normalizer 时**必须遵守**的 6 条：

1. **开工前先调真实 API**：用 `ZeppClient()` 拉一次真实响应，贴到 `references/api-field-mapping.md` 对照表里，再开始写代码。**不要凭文档猜字段名**。
2. **先写测试再写实现**（TDD）：至少 8-12 个用例覆盖正常路径 + 边界 + 哨兵 + tz 三形态。
3. **每个章节完成立刻验证**：跑 `python3 tests/test_xxx.py` + `python3 pull_to_sqlite.py sync --days 7` 实际 sync 验证。
4. **不要回头问 AI**：plan 已拍板（决策表在 M2 计划文档里），按 plan 走。
5. **不要修改 M3 范围之外**：国际服、food、night_today、压力曲线、PRO 模式等都不做（M4 范围）。
6. **bug 触发立刻修**：30 分钟内能修就修，修不了列入 M3。

---

## Normalizer 设计原则

### 字段名核对流程

Zepp Cloud 字段命名在不同 region 间不一致（cn3 用 `vo2Max` 驼峰，us3 用 `vo2max` 全小写，eu2 用 `vo2_max` 下划线）—— normalizer 必须做**多字段 fallback**，不能写死一个名字。

**流程**：
1. 用 `ZeppClient()` 拉一次真实响应
2. 对照 `references/api-field-mapping.md` 列出候选字段名（按优先级排序）
3. 按顺序尝试，第一个非空且在合法范围内的值入库
4. 范围过滤：超出范围的视为哨兵丢弃
5. 加 unit test 覆盖：所有候选字段名 / 范围边界 / 哨兵 / 空数组 / 单元素 / 数组

### 范围与哨兵值

每个 normalizer 必须实现：
- **范围过滤**：如 VO2max 在 (1, 100) ml/kg/min 之外视为哨兵
- **保留 2 位小数**：体重等连续值精度统一
- **显式 None 处理**：用 `statistics.median(filter(None, samples))` 显式排除 None（不要把 None 当 0 算）

### TZ 3 种形态

`slp.tz`（band_data）和 weight 流 `timeZone` 字段有 3 种形态：

1. `"28800000"`（毫秒字符串）—— `offset_from_number` 自动 ÷1000
2. `"Asia/Shanghai"` / `"Asia/Seoul"`（IANA）—— `parse_timezone_text`
3. `None` 或无法解析 —— 默认 Asia/Shanghai (28800)

**关键**：band_data 的 `slp.st` 字段**没有时区后缀但实际是本地时间**——一定要从 `slp.tz` 推断，不能假定 UTC。这是 band_data vs 其它 daily-level 端点的最大差异。

---

## 避免陷阱

### 1. 不要 print 完整 token

任何调试输出必须脱敏。

### 2. 不要假设所有流都有数据

用户的设备可能没某个传感器（CN 国服 food/vo2_max 实测 items=[]）。

### 3. 不要捏造字段值

"走了 0 步" ≠ "没测步数"，哨兵值要显式处理。

### 4. daily_summary 流滞后 1-2 天

Zepp Cloud 的 `daily_summary` 流（`DailyHealth` 事件）是**Zepp 后台每天早上 8:00 统一聚合**前一天的步数 / 卡路里 / PAI / 训练负荷。

- 当天（特别是中午之前）去拉 `daily_summary`，只能拿到**当天早上 8:00 之前**的数据
- 即使你下午 sync 一次，**Zepp 后台还没算**，daily_summary 还是早上的数

**正确做法**：
- **看当天实时数据** → 用 `sample-level` 流（`hrv_rmssd` / `stress` / `spo2` / `band_data.data_hr` 等等），这些流**每次手表测量就立即推云端**
- **看当天步数 / 卡路里** → 用 `band_data.json` 的 `data_hr` 字节（每分钟一次），或 daily_summary 的 morning snapshot
- **看历史某天完整数据** → 等第二天 sync 拿 daily_summary

**SQL 验证哪些流是当天实时**：
```sql
SELECT metric, MAX(ts_ms/1000, 'unixepoch', 'localtime') latest
FROM measurements
WHERE date(ts_ms/1000, 'unixepoch', 'localtime') = date('now','localtime')
GROUP BY metric;
-- daily_summary 流最新时间戳 ≤ 早上 8:00（说明被 Zepp 锁住了）
-- sample-level 流最新时间戳 ≤ sync 时间（说明是实时）
```

**教训**：做"今天"类查询时**先想清楚要看什么** —— 步数 / 卡路里 / PAI 这种 daily metric 当天不准；心率 / HRV / 压力这种 sample-level 当天能看。

### 5. 不要修改 M3/M4 范围之外

除非在新里程碑拍板。

### 6. 不要回头问 AI

按 plan 走，自己拍板。

---

## M5 候选（normalizer 相关）

### 1. 拉最新数据必须先 OAuth 再 sync（修复方向）

**症状**：9-24 下午 refresh token 显示 `error_code=0108`，但接着跑 `sync --days 7` 还是显示"成功"——DB 里 `steps` 数字是几个小时前的旧值。

**根因**：
- `zepp_oauth.py refresh` 失败（access_token 真的过期了，刷新接口抛 `0108`）
- 但 `pull_to_sqlite.py sync` **不会主动 OAuth**——它只读 `~/.zepp-data/.secrets/token.json` 里的 `app_token` 调用 API
- 如果 app_token 已过期，所有 API 都会 `HTTP 401`，但 `sync` 不会提示"token 过期"，只会静默吞掉错误、显示 `本次新增/更新 0 条`

**修复方向（M5 候选）**：
- 让 `pull_to_sqlite.py sync` **先自动尝试 refresh**，失败再 OAuth，再失败报错
- 或者 wrapper `zepp_cron_sync.sh` 已经做 refresh，**手动 sync 流程也要走 wrapper**

### 2. DST 探测（M4.8 暂缓 → M5 候选）

边界 session 标 `dst_suspect=1`；非标准 tz offset 打 diagnostic。

### 3. 压力 24h 曲线（已完成 M4.7）

`normalizer/stress.py` 5min sample-level 解析；与睡眠 stage 切片对齐。

---

## Normalizer 模块清单（M1-M4）

| 模块 | 行数 | 解析端点 | 产出表 |
|---|---|---|---|
| `normalizer/wellness.py` | (M1) | HRV/血氧/PAI/stress 概要 | `measurements` |
| `normalizer/common.py` | (M1) | 共用工具（TZ/单位换算）| - |
| `normalizer/sleep.py` | 548 (M2) | band_data summary+detail | sleep_sessions / sleep_stage_slices / heart_rate_band_samples |
| `normalizer/body.py` | 180 (M2) | weightRecords | measurements (stream='weight') 11 BodyMetric |
| `normalizer/workout_detail.py` | 1261 (M3) | /v1/sport/run/detail.json | workout_route_points / workout_samples / workout_splits / workout_laps / workout_pauses / workout_hr_drift |
| `normalizer/food.py` | ≈280 (M4.2) | 饮食流 | food_daily |
| `normalizer/vo2_max.py` | ≈220 (M4.4) | VO2_MAX 端点 | daily_vo2_max + measurements |
| `normalizer/stress.py` | ≈260 (M4.7) | stressHistory.json subItems | stress_curve_samples |
