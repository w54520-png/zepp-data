# 卡路里 BMR 计算

> 拆分自 SKILL.md。4 个 bmr-source 选项说明 + 决策树 + Zepp 静息心率算法。

---

## 背景

Zepp Cloud `daily_summary` 流**只给活动卡路里**（`totalCalories` 字段），没有 BMR/静息消耗字段——也就是说 Zepp API 给的"卡路里" = Apple Watch 的"活动卡路里"，**不含基础代谢**。

但 Zepp App 界面上显示的是"**总计 = 静息 + 活动**"。为了让 skill 数据跟 Zepp App 一致，提供了 `compute_calorie_total.py` 脚本。

---

## 用法

```bash
# 默认（推荐）：用 Zepp App 实测的 BMR 值（最准）
python3 compute_calorie_total.py

# 公式算（Harris-Benedict，会偏高 192 kcal）
python3 compute_calorie_total.py --bmr-source formula

# Zepp Cloud weight 流 metabolism 字段（不推荐）
python3 compute_calorie_total.py --bmr-source zepp_api

# Zepp App 历史测量中位数（M3 新增）
python3 compute_calorie_total.py --bmr-source zepp_med

# 预览不写入
python3 compute_calorie_total.py --dry-run
```

---

## 原理

```
总卡路里 = 活动卡路里 (来自 Zepp API) + 静息 BMR (本地算)
```

---

## 4 个 BMR 来源选项

| `--bmr-source` | 优点 | 缺点 |
|---|---|---|
| **`zepp_app`**（默认） | 跟 Zepp App 显示完全一致 | 需要从 Zepp App 截屏读一次（脚本里有当前快照）|
| `formula` | 自动从 `user_profile` 算（Harris-Benedict 1984 修订版）| **偏高 192 kcal**（Zepp 用 Mifflin-St Jeor 或更精细的算法）|
| `zepp_api` | 直接从 weight 流 `metabolism` 字段读 | **不推荐**——用户报告体脂秤 BMR 漂移：1436 kcal Zepp App vs 1553 kcal 体脂秤。只读取**最新一条** weight 流 BMR——不自动覆盖默认行为。手工选才生效 |
| `zepp_med` | Zepp App 历史测量中位数（M3）| 需要 ≥ 3 个测量样本 |

---

## 决策树

### 快速版（推荐直接看）

| 你的情况 | 推荐 `--bmr-source` | 理由 |
|---|---|---|
| 没 Zepp App 截图 / 没体脂秤 | `zepp_med` | 5 次中位数抗漂移（最稳定）|
| Zepp App 显示的卡路里数字和你这边对不上 | `zepp_app` | 用 Zepp App 实测 1436（与你 Zepp App 完全一致）|
| 你用 Zepp App 显示的 BMR 觉得漂移（1436↔1553）| `zepp_med` | 中位数会抗掉单次漂移 |
| 你想用纯公式算 | `formula` | Harris-Benedict（实测偏高 192 kcal）—— 学术对比用 |
| 有 Zepp App 但 Zepp 没记体重 | `formula` | 兜底 |

**默认**：如果你不确定选哪个 → 用 **`zepp_med`**（最稳）。

### 多次跑的结果一致性

- `zepp_app` / `zepp_api`：每天会变（体重秤漂移 → BMR 漂移）
- `zepp_med`：取最近 5 次中位数（稳）
- `formula`：永远固定（纯公式）

### 完整版（条件分支）

```
需要"今天跟 Zepp App 显示一致"
├─ 有 Zepp App 截屏 → zepp_app
├─ 没截屏但有 ≥3 个 weight 流 BMR 测量 → zepp_med
├─ 没截屏没 weight 数据 → formula（注意会偏高 192 kcal）
└─ 调试用 → zepp_api（用最新一条 weight BMR，不推荐日常）
```

---

## 公式（Harris-Benedict 1984）

```
男 BMR = 88.362 + 13.397×W(kg) + 4.799×H(cm) - 5.677×A
女 BMR = 447.593 + 9.247×W(kg) + 3.098×H(cm) - 4.330×A
```

**注意**：Zepp 的"静息消耗"看起来**不是全天 BMR**，而是 `TDEE - 活动卡路里`（全天总消耗减去活动部分）。这跟 Apple Health、Fitbit 的口径一致。

---

## Zepp App 实测值（示例，发布版替换为你自己的真实值）

主账号"example_user" 男 35y 175cm 70kg：
- 4876 步 / 活动 142 kcal / **静息 1436 kcal** / 总 1578 kcal
- BMR 实测 ≈ 59.8 kcal/h × 24h

---

## 写到哪

写到一个新 metric `calories_total`，`source_scope = computed_bmr:zepp_app@2026-09-22`（或 `formula`），可以通过 `query_zepp.py metric --metric calories_total` 查看。

---

## Zepp API 后续如果给 BMR 字段

直接改 `compute_calorie_total.py` 读 Zepp Cloud 的 `static_bmr` 字段，删掉静态 `ZEPP_APP_BMR` 常量。`--bmr-source zepp_api` 可以加为新的第三选项。
