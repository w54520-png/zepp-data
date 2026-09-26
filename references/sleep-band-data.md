# 睡眠 band_data 解析（M2 章节 2.1）

> 拆分自 SKILL.md。Zepp Cloud `band_data.json` 睡眠端点的 2 路径解析（A B64 + B 扁平）+ stage 锚点 + data_hr 字节解码 + DST 决策。

---

## 端点

Zepp Cloud 通过 `GET /v1/data/band_data.json` 暴露睡眠数据。

- **query_type=summary**：返回每日汇总（含 stage 列表，B64 编码）
- **query_type=detail**：返回 summary + 1440 字节 data_hr（逐分钟心率）
- **时间参数**：YYYY-MM-DD（**本地日历日**）

---

## 3 张表

| 表 | 含义 |
|---|---|
| `sleep_sessions` | 一条睡眠 session（主睡 + nap）|
| `sleep_stage_slices` | 每个 stage 切片（5/4/8/11/7 → deep/light/rem/awake）|
| `heart_rate_band_samples` | detail 端点 1440 字节的逐分钟心率（255 = 未测）|

---

## Stage 锚点（**核心！**）

```
stage_actual_ts = slp.st + stage.start_min * 60
```

`slp.st` 是数据起始锚点（通常是 UTC 前一天 16:00）。`stage[].start/stop` 是相对锚点的分钟数。

### 锚点错位 bug（M2 修）

**症状**：睡眠解析入库后，`sleep_sessions.start_ts` / `stop_ts` 与 Zepp App 显示的时间相差 8 小时（中国时区 vs UTC 漂移）。例如 Zepp App 显示"22:30 入睡、06:30 起床"，DB 里 session 变成"06:30 入睡、14:30 起床"——**正好少 8 小时**。

**根因**：band_data JSON 里 `slp.st` 字段是**本地时间**戳（不带时区后缀），但 normalizer 最初把它当 UTC 解析 → 直接用 `datetime.fromtimestamp(ts, tz=utc)` → 8 小时漂移。Stage 列表 `start_min` / `stop_min` 又是相对 `slp.st` 的分钟数，**所以整个 stage 切片也跟着错**。

**修法**（M2 合入）：
- `normalizer/sleep.py::parse_band_data_summary()` 改用 `datetime.fromtimestamp(slp.st, tz=zoneinfo("Asia/Shanghai"))` —— 强制按用户本地时区解
- 加 `slp.tz` 时间戳处理 3 种形态（毫秒字符串 / IANA 名 / None 默认上海）
- `stage_actual_ts = slp.st + stage.start_min * 60` 用本地 timestamp + 相对分钟，不再二次时区转换

**教训**：Zepp band_data 的 `slp.st` 字段**没有时区后缀但实际是本地时间**——一定要从 `slp.tz` 推断，不能假定 UTC。这是 band_data vs 其它 daily-level 端点的最大差异。

---

## Stage mode 映射

| Zepp mode | 语义 |
|---|---|
| 5 | deep |
| 4 | light |
| 8 | rem |
| 11 | rem（兼容）|
| 7 | awake |
| 其他 | unknown |

---

## data_hr 解码

- `data_hr[k]` = UTC k 分钟对应分钟的心率（255 = 未测）
- 过滤 `[20, 240]` 范围
- 本地 ts = 本地 0 点 unix + (k - tz_offset_min) * 60

---

## DST 决策

**按 Zepp 原始 tz 入库**——不主动修正 DST（M2 拍板）。

边界 session 标 `dst_suspect=1`；非标准 tz offset 打 diagnostic（M4.8 暂缓 → M5 候选）。

---

## Nap session 判定（M3 新增）

session < 90 分钟为 nap，自动标 `is_nap=1`。

---

## 验证结果（M2）

- 7 天实际 sync 入库 10 个 sleep_sessions + 123 个 sleep_stage_slices + 38078 个 heart_rate_band_samples
- session 时间与 Zepp App 完全一致
- nap session 自动标 `is_nap=1`（< 90 分钟判 nap）
- DST 边界按 Zepp 原始 tz 入库（M2 拍板，不主动修正）

---

## 测试覆盖

`tests/test_band_sleep_normalizer.py` 452 行覆盖：
- summary / detail / stage 锚点 / 5 种 mode / 1440 字节 HR 解码 / DST 决策 / nap 标志 / tz 3 形态
