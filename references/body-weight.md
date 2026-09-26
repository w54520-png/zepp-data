# 体重 / 体成分（M2 章节 2.2）

> 拆分自 SKILL.md。Zepp Cloud weightRecords 端点的 11 个 BodyMetric + 3 种 timeZone 形态 + generatedTime 秒单位 + 写入策略。

---

## 关键端点

Zepp Cloud 通过 `GET /users/{user_id}/members/{member_id}/weightRecords` 暴露体重 + 体成分测量历史。

- **端点**：`/users/{user_id}/members/{member_id}/weightRecords`
- **时间参数**：**秒**（不是 ms，是 10 位数字）—— 其他流都是 ms
- **嵌套**：所有数据字段都在 `summary` 子对象里（实测）

---

## 11 个 BodyMetric

| 字段名 | API 字段 | 单位 | 范围 |
|---|---|---|---|
| `weight` | `summary.weight` | kg | (0.5, 600] |
| `bmi` | `summary.bmi` | kg/m² | [10, 100] |
| `height` | `summary.height` | cm | [50, 250] |
| `body_fat_rate` | `summary.fatRate` | % | [3, 70] |
| `body_water_rate` | `summary.bodyWaterRate` | % | [20, 90] |
| `muscle_mass` | `summary.skeletalMuscle` | kg | [0.5, 200] |
| `bone_mass` | `summary.boneMass` | kg | [0.1, 20] |
| `protein_rate` | `summary.proteinRatio` | % | [5, 60] |
| `visceral_fat` | `summary.visceralFat` | level | [0.5, 50] |
| `bmr` | `summary.metabolism` | kcal | [500, 5000] |
| `body_balance_score` | `summary.bodyBalanceScore` | score | [0, 100] |

---

## 写入策略（Q2 拍板）

**所有 11 个 BodyMetric 都写到 `measurements` 表，`stream='weight'`**，`metric` 是上面 11 个名字之一。

```sql
SELECT * FROM measurements WHERE stream='weight' ORDER BY ts_ms DESC;
```

---

## timeZone 3 种形态

`weightRecords` 流 `timeZone` 字段有 3 种形态：

1. `"28800000"`（毫秒字符串）—— `offset_from_number` 自动 ÷1000
2. `"Asia/Shanghai"` / `"Asia/Seoul"`（IANA）—— `parse_timezone_text`
3. `None` 或无法解析 —— 默认 Asia/Shanghai (28800)

---

## generatedTime 秒单位

`generatedTime` 字段是**秒**（10 位数字），不是毫秒（13 位）—— 其他 daily-level 流都是 ms。

**坑**：入库前必须 `* 1000` 转成 ms，否则 SQL 查询会按 1970 年附近匹配。

---

## BMR 来源（M2 拍板）

`compute_calorie_total.py` 现在支持 4 个 `--bmr-source` 选项（M3 章节 3.5 新增 zepp_med）：

```bash
# 默认（最准）—— Zepp App 实测值
python3 compute_calorie_total.py --bmr-source zepp_app

# 公式算（Harris-Benedict 1984，偏高 192 kcal）
python3 compute_calorie_total.py --bmr-source formula

# Zepp Cloud weight 流 metabolism 字段（M2 新增，但**不推荐**——
# 用户报告体脂秤 BMR 漂移：1436 kcal Zepp App vs 1553 kcal 体脂秤）
python3 compute_calorie_total.py --bmr-source zepp_api

# Zepp App 历史测量中位数（M3）
python3 compute_calorie_total.py --bmr-source zepp_med
```

**注意**：`zepp_api` 选项只读取**最新一条** weight 流 BMR——不自动覆盖默认行为。手工选才生效。

详见 [calorie-bmr.md](calorie-bmr.md)。

---

## cron 自动拉

`zepp_cron_sync.sh` 已包含 weight 流（按年切片），自动同步最近 365 天体重历史。

---

## 测试覆盖

`tests/test_weight_normalizer.py` 250 行覆盖：
- 11 BodyMetric 字段 / 3 种 timeZone / generatedTime 秒单位 / 嵌套 summary / 范围边界 / 哨兵值
