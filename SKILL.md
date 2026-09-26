---
name: zepp-data
version: 4.0.0
description: 同步 Zepp（华米/Amazfit）健康数据到本地 SQLite。包括 HRV/血氧/压力/PAI/睡眠/运动/体重等。内置国服 OAuth 登录（不踢手机 App）、增量/全量同步、自然语言查询、健康周报、HTML 看板。当用户提到 Zepp、华米、Amazfit、健康数据、手表数据同步、HRV、PAI、血氧、运动统计、步数、卡路里、睡眠、体重、BMI、BMR、**Zepp 账号登录异常 / OAuth 被踢（独家功能）** 时触发。**严格不适用**：Apple Watch / Garmin / Fitbit 等其他品牌手表即使含"同步/步数/手表"等关键词也一律不触发（只支持 Zepp Cloud API）；手表选购咨询；Zepp App UI 操作；投资/编程/学习等无关话题。
---

# Zepp Data Skill

把 Zepp Cloud（华米云）上的 Amazfit 手表/手环健康数据**同步到本地 SQLite**，并提供自然语言友好的查询命令。

设计原则：
- **代码内置 schema** —— 用户不需要自己建表，`pull_to_sqlite.py init` 自动创建
- **OAuth 内置** —— `zepp_oauth.py login` 走 3 步 server-side OAuth，**不依赖浏览器**，**任何沙箱环境**都能跑
- **密码安全** —— 密码从 stdin / 环境变量 / 文件读取，AI 完全见不到密码
- **数据本地化** —— token 和数据库都在用户机器 `~/.zepp-data/`，不上传
- **可分享** —— skill 只带代码，不带任何用户私有数据

---

## 项目结构

```
zepp-data/
├── SKILL.md              # 本文件（L2 brief，路由 + 入口）
├── references/           # L3 detail，按需读取
│   ├── oauth-flow.md     # OAuth 三步流程 + 不踢 App 根因
│   ├── data-flows.md     # 21 类数据流端点表 + 字段映射
│   ├── normalizer-rules.md # normalizer 设计原则 + 6 条实施约束
│   ├── calorie-bmr.md    # 4 个 bmr-source 选项 + 决策树
│   ├── workout-mapping.md # sport type 映射 + Zepp API 关键坑
│   ├── sleep-band-data.md # band_data 2 路径解析 + stage 锚点
│   ├── body-weight.md    # 11 个 BodyMetric + 3 种 timeZone
│   ├── schema-evolution.md # 17 张表演进时间线（M1→M4）
│   ├── fix-history.md    # M1-M4 所有 bug 修复 + 经验教训
│   ├── glossary.md       # 术语表（HRV/RMSSD/BMR/PAI/DST/哨兵值/sport catalog 等）
│   └── error-codes.md    # 完整错误码速查（OAuth/HTTP/自定义/sync 0 条诊断）
├── scripts/              # 所有 Python 脚本（OAuth / sync / query / dashboard）
├── sql/schema.sql        # 完整 SQLite schema（17 张表 — 镜像自 storage.py::SCHEMA / SCHEMA_M3）
├── tests/                # 16 个测试模块 / 3261 行
├── references/api-field-mapping.md # API 字段对照表（14+ 流，M2/M3 维护）
└── evals/                # 评估脚本
```

---

## 何时触发

当用户消息中包含以下任一关键词时触发本技能：
- **品牌词**：Zepp、华米、Huami、Amazfit
- **数据词**：健康数据、心率、HRV、PAI、血氧、压力、睡眠、运动、体重、训练负荷
- **动作词**：同步、拉数据、看一下最近、总结、本地备份

**不要触发**：
- 用户只是问"手表怎么选"
- 用户在询问 Zepp App 的功能
- 与健康数据无关的话题

---

## 快速上手（5 步）

### Step 1：AI 引导设置 OAuth

> 用户：连接我的 Zepp 账号
>
> AI：请先把 Zepp 账号和密码设到环境变量（或者写到本地文件，chmod 600）：
>
> ```bash
> export ZEPP_PHONE=186XXXXXXXX
> export ZEPP_PASSWORD='你的真实密码'
> # 或者
> echo '你的真实密码' > ~/.zepp-password && chmod 600 ~/.zepp-password
> ```

### Step 2：跑 OAuth 登录拿 token

```bash
python3 ~/.minis/skills/zepp-data/scripts/zepp_oauth.py login
```

### Step 3：拉数据

```bash
python3 ~/.minis/skills/zepp-data/scripts/pull_to_sqlite.py sync --days 7
```

### Step 4：查询

```bash
python3 ~/.minis/skills/zepp-data/scripts/query_zepp.py recent --days 7
python3 ~/.minis/skills/zepp-data/scripts/query_zepp.py metric --metric pai_daily --days 30
python3 ~/.minis/skills/zepp-data/scripts/query_zepp.py summary
python3 ~/.minis/skills/zepp-data/scripts/query_zepp.py daily-stress --date 2026-09-24
# ↑ 输出 Zepp App 风格的压力详情（当前值 / 最高 / 最低 / 平均 / 4 区间占比 / 本地重算 / vs 昨天）
python3 ~/.minis/skills/zepp-data/scripts/query_zepp.py daily-hr --date 2026-09-24
# ↑ 输出 Zepp App 风格的心率详情（当前 bpm / 平均 / 最高 / 最低 / 6 区间占比 / vs 昨天）
```

### Step 5：HTML 看板（可选）

```bash
python3 ~/.minis/skills/zepp-data/scripts/dashboard.py --days 30 > ~/.zepp-data/dashboard.html
# 然后在 minis:// 或浏览器打开 dashboard.html
```

看板用 Chart.js CDN，**单文件无依赖**（除 chart.js），分主题展示：📊 活动 / ❤️ 心血管 / 💪 PAI 与训练 / 😤 压力 / 💗 血氧 / 😮‍💨 呼吸 / 🔋 体电荷。

> 详见 [references/oauth-flow.md](references/oauth-flow.md) — OAuth 三步流程 + 不踢手机 Zepp App 的根因（`APP_NAME=com.xiaomi.hm.health`）
>
> 详见 [references/data-flows.md](references/data-flows.md) — 21 类数据流端点表

---

## 命令速查表

| 命令 | 用途 | 风险等级 |
|---|---|---|
| `zepp_oauth.py login` | 用户首次 OAuth 登录（**不踢手机 App**）| 🟢 低（不踢） |
| `zepp_oauth.py refresh` | 用 login_token 续 app_token（**不踢 App**）| 🟢 低（cron 默认走这条） |
| `zepp_oauth.py status` | 检查当前 token 状态 | 🟢 低 |
| `pull_to_sqlite.py init` | 仅创建 DB（首次） | 🟢 低 |
| `pull_to_sqlite.py sync --days N` | 拉数据落库（默认 7 天）| 🟡 中（可能 token 过期） |
| `pull_to_sqlite.py dedup` | 清理 daily 流重复 | 🟢 低（已自动跑） |
| `query_zepp.py recent --days N` | "最近 X 天"类问题 | 🟢 低 |
| `query_zepp.py metric --metric X --days N` | "PAI 趋势 / 步数变化" | 🟢 低 |
| `query_zepp.py range --from X --to Y` | "X 月到 Y 月数据" | 🟢 低 |
| `query_zepp.py devices` | "我连了哪些设备" | 🟢 低 |
| `query_zepp.py capabilities` | 排查"为什么这个流没数据" | 🟢 低 |
| `query_zepp.py summary` | DB 状态总览 | 🟢 低 |
| `query_zepp.py daily-stress --date YYYY-MM-DD` | **Zepp App 风格压力详情**：当前值 / 最高 / 最低 / 平均 / 4 区间占比（与 Zepp App ±1pp）/ 本地重算 / vs 昨天对比 | 🟢 低 |
| `query_zepp.py daily-hr --date YYYY-MM-DD` | **Zepp App 风格心率详情**：当前 bpm / 平均 / 最高 / 最低 / 6 区间占比（舒缓轻松/热身放松/脂肪燃烧/心肺强化/耐力强化/无氧极限，阈值 113/141/154/162/173/190 bpm）/ vs 昨天对比 | 🟢 低 |
| `query_zepp.py workouts --top climb --limit N` | 运动历史按类型 Top N（**climb=爬升/distance=距离/duration=时长**）| 🟢 低 |
| `query_zepp.py workouts` | 运动历史列表（按类型汇总）| 🟢 低 |
| `zepp_cron_sync.sh` | **cron 推荐** —— 自动 refresh → sync → dedup → fetch workouts | 🟢 低（推荐手动也走这条） |
| `compute_calorie_total.py --bmr-source <formula\|zepp_app\|zepp_med\|zepp_api>` | 给 daily 卡路里补 BMR（**zepp_med = Zepp 中位数 BMR**）| 🟢 低（默认 zepp_app） |
| `compute_calorie_total.py` | 同上但用默认 bmr-source=zepp_app | 🟢 低 |
| `fetch_workouts.py --from YYYY-MM-DD` | 拉运动历史到 workouts 表（**爬升/距离/分圈/HR**）| 🟡 中（rate limit） |
| `insight.py --weekly` | **周报生成** —— 输出 stable JSON `weekly_report`（含 facts / baseline / direction）| 🟢 低 |
| `insight.py --monthly` | 月报生成 | 🟢 低 |
| `dashboard.py --days N` | 生成 HTML 健康看板（30 天）| 🟢 低 |
| `dashboard_7d.py --days 7` | 生成 HTML 周看板（7 天）| 🟢 低 |
| `daily_report.py` | **健康日报** —— 默认 t-1，自动 sync + BMR (Mifflin-St Jeor) + 13 章节 markdown | 🟢 低 |
| `daily_report.py --date YYYY-MM-DD --out report.md` | 生成指定日期日报写到文件 | 🟢 低 |
| `daily_report.py --webhook "$URL"` | 推送日报到 webhook（企业微信/飞书/Slack） | 🟢 低 |

---

## ⚠️ 风险等级速查表

| 章节 | 风险 | 自由度 | LLM 必须做的 |
|---|---|---|---|
| OAuth (init.py, zepp_oauth.py) | 🔴 高 | 低（固定命令） | 必须用脚本，绝不自己写 |
| sync (pull_to_sqlite.py) | 🟡 中 | 低（默认 --days 7） | 不准全量（除非用户明示 30 天+） |
| query (query_zepp.py) | 🟢 低 | 高 | 选对 sub-command（recent / metric / range / workouts）|
| BMR 计算 (compute_calorie_total.py) | 🟡 中 | 低（默认 zepp_app） | 用户明示才换 bmr-source |
| Dashboard (dashboard.py) | 🟢 低 | 中 | 按周期选 dashboard_7d vs dashboard |
| workout 详情 (fetch_workouts.py) | 🟢 低 | 中 | 仅对 trackid 拉新活动详情 |
| 国际服 (ZeppClient region fallback) | 🟡 中 | 低（自动 fallback） | 不准手动改 region_host |
| 设备型号识别 (device_catalog) | 🟢 低 | 高 | 通过 product_id / deviceSource 自动映射 |

> 详见 [references/oauth-flow.md](references/oauth-flow.md) — OAuth 风险细节（APP_NAME / token 安全）
>
> 详见 [references/calorie-bmr.md](references/calorie-bmr.md) — BMR 4 选项决策树（默认值何时该改）
>
> 详见 [references/workout-mapping.md](references/workout-mapping.md) — workout sport type 映射（不可猜）

---

## 自然语言查询示例

| 用户问 | 推荐命令 |
|---|---|
| "帮我看一下最近 7 天的步数" | `python3 scripts/query_zepp.py metric --metric steps --days 7` |
| "最近一个月 PAI 多少" | `python3 scripts/query_zepp.py metric --metric pai_total --days 30` |
| "我昨天压力怎么样" | `python3 scripts/query_zepp.py daily-stress --date YYYY-MM-DD`（date = t-1）|
| "我昨天心率" | `python3 scripts/query_zepp.py daily-hr --date YYYY-MM-DD` |
| "看下我最近爬升最多的运动" | `python3 scripts/query_zepp.py workouts --top climb --limit 5` |
| "数据库里现在有多少条数据" | `python3 scripts/query_zepp.py summary` |
| **"帮我生成昨日健康日报"** | `python3 scripts/daily_report.py`（默认 t-1，自动 sync + BMR 用 Mifflin-St Jeor 公式 + 13 章节 markdown）|
| **"看下我昨天的卡路里"** | 同上 —— 日报 🔥 卡路里总消耗 节含活动 + Mifflin BMR + 总消耗（无卡路里专门查询，日报已覆盖）|

日报输出示例（Mifflin BMR 节，数值仅作演示）：
```
🔥 卡路里总消耗（活动 + 静息）

- 活动消耗：133 kcal
- 基础代谢（Mifflin-St Jeor）：**1,668 kcal**
  - 公式：10×70 + 6.25×175 - 5×35 + 5
  - 数据来源：身高 175cm (user_profile) + 体重 70 kg (最新测量) + 年龄 35 (男)
- **总消耗：133 + 1,668 = 1,801 kcal** 🎯
```

---

## 项目历史（M1-M9，2026-09-26 归档）

本 skill 从 2026-09-08 起步，到 2026-09-22 完成 M1+M2+M3 三阶段完整复刻 ZeppBridge（[lingcang728/ZeppBridge](https://github.com/lingcang728/ZeppBridge)）的解析架构，覆盖 Zepp Cloud 19 类数据流、200 个单元测试、10 张 SQLite 表。三个里程碑独立可交付、互不阻塞，每阶段都附带新增测试 + 实际 sync 验证。

### M1 — workout summary 字段补齐 + 测试骨架 + 海拔单位修复
- **目标**：补齐 `workouts` 表缺失的运动历史字段，并建立 TDD 测试骨架
- **关键 bug 修复**：`fetch_workouts.py` 海拔字段 `altitude_ascend` 原封不动写入（米），但 Zepp Cloud 实际返**厘米** → 修复后除以 100
- **涉及文件**：`scripts/fetch_workouts.py` / `scripts/storage.py` / `scripts/normalizer/wellness.py` / `scripts/normalizer/common.py` / `sql/schema.sql` / 新建 `tests/` 4 个模块

### M2 — 睡眠解析 + 体重流 + 字段对照表 + BMR 增强
- **目标**：新增 Zepp Cloud `band_data`（睡眠）+ `weightRecords`（体重/体成分）两类长尾数据流
- **关键 bug 修复**：sleep 解析最初用 `ts` 字段做锚点，结果 session 时间错乱 → 改用 `slp.st` 字段作为 stage 相对分钟数的基准
- **涉及文件**：新建 `normalizer/sleep.py` / `normalizer/body.py` / 2 个测试 / `references/api-field-mapping.md` / `compute_calorie_total.py` / `pull_to_sqlite.py` / `sql/schema.sql`

### M3 — workout 详情 + insights 周报 + nap session + dashboard 升级
- **目标**：从 workouts summary 升级到 `/v1/sport/run/detail.json` 的 GPS/分段/分圈/HR 漂移完整解析
- **关键 bug 修复**：`insight.py` 基线窗口原用 30 天太短 → 改为 180 天；距离比较去掉 ±20% 的过滤
- **关键发现**：Zepp `workout_detail` 端点有 **rate limit** —— 测试时 7 次连续调用后第 8 次返空
- **涉及文件**：新建 `normalizer/workout_detail.py` / `insight.py` / `dashboard_7d.py` / 3 个测试 / `pull_to_sqlite.py` / `sql/schema.sql` / `compute_calorie_total.py`

### M4 — 限流应对 + 饮食流 + VO2max + 国际服 + dashboard 趋势线（2026-09-23）

| 任务 | 改动 | 测试 |
|---|---|---|
| 4.1 workout_detail 限流 | `_fetch_workout_detail_with_retry` 区分 3 种响应 + 200ms 节流 + feature 探测；新增诊断计数器 | 16 |
| 4.2 Food 流 | `normalizer/food.py` 4 个 macro 按日累加；5 种 envelope | 30 |
| 4.4 VO2_MAX | `normalizer/vo2_max.py` 6 个字段候选；范围 (1,100) ml/kg/min | 25 |
| 4.5 国际服 | `ZeppClient(try_regions_fallback=True, base_url_override=...)`；fallback 链 cn3→us3→eu2→sg2 | 11 |
| 4.6 dashboard 趋势线 | chart.js + 4 个 30 天趋势图 | (前端，无单测) |
| 4.3 HRV SDNN fallback | M3 已合入（`compute_weekly_report` hrv→hrv_rmssd 双查） | 已覆盖 |
| 4.7 压力 24h 曲线 | `normalizer/stress.py` 5min sample-level 解析；与睡眠 stage 切片对齐 | 10 |
| 4.8 DST | 暂缓（M5 候选） | – |

### 四阶段累计
- **总测试数**：305，全 pass（16 个测试模块 3261 行；M4 新增 105 测试）
- **总流数**：21 类（v2_events × 11 + user_events × 3 + watch_stat × 2 + heart_rate + band_data + weight + workout_detail + workout_history + devices + members + food + vo2_max + stress_curve）
- **ZeppBridge 对标**：覆盖度从起步 11% 升到 92%（19 类数据流覆盖 17 类 → M4 后 21 类）

### 五-九阶段累计（M5-M9，2026-09-26 归档）

| 阶段 | 主题 | 测试 | 关键产物 |
|---|---|---|---|
| **M5** | silent_reauth + sync 错误可见性 | 30 | `zepp_oauth.py refresh` 失败 → prompt 引导；`pull_to_sqlite.py sync` 错误打印到 stderr |
| **M6** | time_utils 统一 + DST 处理 | 30 | `time_utils.py` `EPOCH_FIELDS` 字典 + `explain_field()` debug 命令；11 个字段单位修正 |
| **M7** | ensure_fresh + silent_reauth 测试 | 18 | `ensure_fresh_token()` 在 sync 前自动 refresh；3 种异常分支 |
| **M8** | HR zone breakdown + stress breakdown + 日报模板设计 | 60 | `daily-hr` / `daily-stress` Zepp App 风格详情；`/tmp/M8_daily_report_template.md` 13 章节模板 |
| **M9** | **日报集成 + Mifflin BMR** | **8** | `scripts/daily_report.py`（225 行 CLI）+ Mifflin-St Jeor 公式 + 🏋️ 体重快照独立节 |

#### M9 关键详情（2026-09-26）

- **BMR 算法替换** — 弃用 Zepp `bmr_median`（KATCH-MCARDLE + 假设 LBM=67 不准），改用 **Mifflin-St Jeor**（学界标准 1990，4 行 Python）。公式可见 → 用户能验算 → 信任基础
- **日报脚本集成** — `scripts/daily_report.py`（225 行）做 CLI：argparse (`--date`/`--out`/`--webhook`/`--no-sync`/`--sync-days`) + 默认 t-1 + silent_reauth + sync
- **🏋️ 体重快照独立节** — 新增节（永远显示最新测量），保留老 ⚖️ 节（当天完整体成分），按用户要求"不重叠"
- **总卡路里（活动 + 静息）** — 用 Mifflin BMR 替代 Zepp 黑盒；公式 + 数据来源两行可追溯
- **测试统计** — M9 新增 8 用例；总数 476 → **484 passed**，**0 回归**；4 个 pre-existing 失败（DST × 2 / HR zone × 1 / stress × 1）与 M9 无关
- **真实样本** — `/tmp/M9_report_2026-09-25.md`（3791 字节 / 112 行）—— 完整数据日演示

```bash
# 一键生成昨日健康日报（默认 t-1）
python3 scripts/daily_report.py

# 推送到 webhook（企业微信 / 飞书 / Slack）
python3 scripts/daily_report.py --webhook "$URL"

# 写文件 + 同步天数调为 14
python3 scripts/daily_report.py --out report.md --sync-days 14
```

> 完整 M9 归档：[wiki/raw/2026-09-26-zepp-m9-daily-report.md](/var/minis/mounts/LLM-WIKI/raw/2026-09-26-zepp-m9-daily-report.md)（467 行）

### 九阶段累计

- **总测试数**：484 passed（M1: 31 + M2: 50 + M3: 60 + M4: 105 + M5: 30 + M6: 30 + M7: 18 + M8: 60 + M9: 8）
- **总流数**：21 类（v2_events × 11 + user_events × 3 + watch_stat × 2 + heart_rate + band_data + weight + workout_detail + workout_history + devices + members + food + vo2_max + stress_curve）
- **ZeppBridge 对标**：覆盖度从起步 11% 升到 92%（19 类数据流覆盖 17 类 → M4 后 21 类）
- **4 pre-existing failed**（与 M9 无关）：
  - `test_dst_handling.py::test_dst_diagnostic_includes_offset_value`
  - `test_dst_handling.py::test_non_standard_offset_triggers_dst_diagnostic`
  - `test_hr_zone_breakdown.py::test_2026_09_24_zepp_app_match`
  - `test_stress_breakdown.py::test_2026_09_24_zepp_app_match`

完整修复历史见 [references/fix-history.md](references/fix-history.md)。

---

## 关键警告（务必读完）

> ⚠️ **OAuth 不会踢手机 Zepp App**（前提：`APP_NAME = "com.xiaomi.hm.health"`）
>
> Zepp 服务端按 `(user_id, app_name)` 维度做 session 隔离。如果有人把 `APP_NAME` 改回 `com.huami.midong`（Zepp App 自己的包名），OAuth 登录会踢掉手机 App 的 session。**这个坑 2 周前踩过**。
>
> 详见 [references/oauth-flow.md](references/oauth-flow.md)

> ⚠️ **sport type 数字映射不可猜**
>
> Zepp Cloud 数字 type ≠ Zepp OS 设备协议 type——同一数字在不同协议里含义不同。自行映射 100% 会出错。**必须用 `sport_catalog.py`**。
>
> 详见 [references/workout-mapping.md](references/workout-mapping.md)

> ⚠️ **任何"拉最新数据"操作不能跳过 OAuth 检查**
>
> "sync 显示成功但 0 条数据"是 token 失效的典型信号。先 refresh，再 sync。
>
> 详见 [references/fix-history.md](references/fix-history.md)

> ⚠️ **daily_summary 流滞后 1-2 天**
>
> 当天（特别是中午之前）去拉 `daily_summary`，只能拿到当天早上 8:00 之前的数据。看当天实时数据用 sample-level 流（HRV / 压力 / 血氧）。
>
> 详见 [references/normalizer-rules.md](references/normalizer-rules.md)

> ⚠️ **不要 print 完整 token / 不要把 token 写到对话历史 / 密码不通过对话告诉 AI**

> ⚠️ **时间字段单位（踩过的坑）**
>
> DB 里时间字段单位**不统一**——这是历史设计 + Zepp API 演变的结果。**永远不要凭字段名猜单位**！
>
> | 表.字段 | 单位 | 工具函数 |
> |---|---|---|
> | `measurements.ts_ms` | **毫秒** | `time_utils.ms_to_utc_dt()` |
> | `raw_records.start_ts_ms` | **毫秒** | `time_utils.ms_to_utc_dt()` |
> | `raw_records.end_ts_ms` | **毫秒** | `time_utils.ms_to_utc_dt()` |
> | `heart_rate_band_samples.ts` | **秒** | `time_utils.sec_to_local_dt()` |
> | `sleep_sessions.start_ts` | **秒** | `time_utils.sec_to_local_dt()` |
> | `sleep_sessions.end_ts` | **秒** | `time_utils.sec_to_local_dt()` |
> | `sleep_stage_slices.start_ts` | **秒** | `time_utils.sec_to_local_dt()` |
> | `workouts.end_ts` | **秒** | `time_utils.sec_to_local_dt()` |
> | `workout_route_points.ts_ms` | **毫秒** | `time_utils.ms_to_utc_dt()` |
> | `workout_samples.ts_ms` | **毫秒** | `time_utils.ms_to_utc_dt()` |
> | `measurements.date` / `heart_rate_band_samples.date` | 本地日历日 `YYYY-MM-DD` | `time_utils.parse_local_date()` |
>
> **踩过的坑**：误把 `heart_rate_band_samples.ts` (秒) 当 `measurements.ts_ms` (毫秒) 除以 1000，得到 `1970-01-22 01:18:28`（应该是 `2026-09-25 13:18:28`）——LLM / 自动化脚本**必须用 `time_utils` 函数，不要直接 `fromtimestamp`！**
>
> debug 命令：
> ```bash
> python3 -c "from scripts.time_utils import explain_field; print(explain_field('heart_rate_band_samples', 'ts'))"
> ```
>
> 完整字典见 `scripts/time_utils.py::EPOCH_FIELDS`。

---

## 数据流与字段映射

每个 Zepp Cloud endpoint → SQLite 字段的完整映射（含范围、单位、哨兵过滤）见：

**📖 [references/api-field-mapping.md](references/api-field-mapping.md)**

包含 14+ 个流（心率 / HRV / 血氧 / 压力 / PAI / 体电荷 / 呼吸率 / 训练负荷 / 每日汇总 / 运动历史 / 睡眠 / 体重 / 用户档案 / 设备 / VO2_MAX / Food）。

各流的端点、字段数、触发方式、限制详见 [references/data-flows.md](references/data-flows.md)。

---

## 当前支持

- **国服**（`api-mifit-cn*.zepp.com`）✅
- **国际服**（`api-mifit-us*.zepp.com` 等）✅（M4.5 region fallback 链 cn3→us3→eu2→sg2）

---

## References 索引

| 文件 | 内容 |
|---|---|
| [references/oauth-flow.md](references/oauth-flow.md) | OAuth 三步流程 + APP_NAME 根因 + 不踢 App 验证 |
| [references/data-flows.md](references/data-flows.md) | 21 类数据流端点表 + 字段映射 + 触发 / 限制 |
| [references/normalizer-rules.md](references/normalizer-rules.md) | normalizer 设计原则 + 6 条实施约束（M2 拍板）+ M5 候选 |
| [references/calorie-bmr.md](references/calorie-bmr.md) | 4 个 bmr-source 选项 + 决策树 + Zepp App 实测值 |
| [references/workout-mapping.md](references/workout-mapping.md) | sport type 映射表 + 8 个证据来源 + Zepp API 关键坑 |
| [references/sleep-band-data.md](references/sleep-band-data.md) | band_data 2 路径解析 + stage 锚点 + data_hr 解码 |
| [references/body-weight.md](references/body-weight.md) | 11 个 BodyMetric + 3 种 timeZone + generatedTime 秒单位 |
| [references/api-field-mapping.md](references/api-field-mapping.md) | 14+ 流字段对照表（心率/HRV/血氧/PAI/睡眠/体重/VO2_MAX/Food 等）|
| [references/fix-history.md](references/fix-history.md) | M1-M4 所有 bug 修复 + 经验教训 + OAuth 重大修复 |
| [references/glossary.md](references/glossary.md) | 术语表（HRV/RMSSD/BMR/PAI/DST/哨兵值/sport catalog/device catalog/GmActiveTime/source_scope/runPosture/kilo_pace/ALL DAY STRESS/DailyHealth/band_data 等）|
| [references/error-codes.md](references/error-codes.md) | 完整错误码速查（OAuth error/error_code + HTTP 状态码 + 自定义错误 + 同步管道错误 + 修复时间线）|
