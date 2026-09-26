# Schema 演进时间线（M1-M4）

> 镜像自 `sql/schema.sql` 头部注释 + `scripts/storage.py` 三个常量字符串
> （SCHEMA / SCHEMA_M2 / SCHEMA_M3）。改 schema 改这两个地方之一即可，
> 然后同步更新本文件。

---

## 当前状态（2026-09-24）

**17 张用户表**（M3 末态）：

| 分组 | 张数 | 表名 |
|---|---|---|
| 基础数据层 | 3 | `raw_records` / `devices` / `user_profile` |
| 规范化标量层 | 3 | `measurements` / `capabilities` / `meta` |
| 睡眠 | 3 | `sleep_sessions` / `sleep_stage_slices` / `heart_rate_band_samples` |
| 运动详情 | 6 | `workout_route_points` / `workout_samples` / `workout_splits` / `workout_laps` / `workout_pauses` / `workout_hr_drift` |
| Insights | 2 | `workout_insights` / `weekly_reports` |
| **合计** | **17** | |

**schema 文件位置**：
- Python 源（运行时 init_db 用）：`scripts/storage.py` 里 `SCHEMA`（M1）+ `SCHEMA_M2`（空占位）+ `SCHEMA_M3`（M3 workout + insights）
- 文档镜像（人手读）：`sql/schema.sql`
- `PRAGMA user_version = 2`（见 `SCHEMA_VERSION`）
- `meta.normalizer_revision = "m3-workout-detail-2026-09-23"`

---

## 演进时间线

### M1 — workout summary 字段补齐 + 测试骨架 + 海拔单位修复

**开始**：4 张表（M1 初版 schema，与 ZeppBridge `mod.rs` 设计哲学对齐）

```
raw_records / measurements / capabilities / meta
```

**M1 内新增**：

| 表 | 用途 | 来源流 |
|---|---|---|
| `user_profile` | 成员档案（生日/性别/身高/体重） | Zepp `members` endpoint |
| `devices` | 设备清单（MAC/SN/firmware 等扁平字段） | Zepp `devices` endpoint |

**M1 末态**：6 张表

**存储介质**：
- 老的 `workouts` summary 字段直接写 `measurements` 表（stream = `workout_summary` / `workout_altitude` 等）
- **没有单独的 `workouts` 表** —— SKILL.md 里 M1 提到"补齐 `workouts` 表"是历史用语上的遗留，实际是写 `measurements`

---

### M2 — 睡眠解析 + 体重流 + 字段对照表 + BMR 增强

**新增 3 张表**（睡眠解析重构，单独成表方便查询）：

| 表 | 用途 | 来源 |
|---|---|---|
| `sleep_sessions` | 一条主睡 / 一次午睡聚合 | `band_data/sleep` summary |
| `sleep_stage_slices` | 睡眠阶段切片（deep/light/rem/awake） | `band_data/sleep` stage 数组 |
| `heart_rate_band_samples` | band_data 逐分钟心率 | `band_data/sleep` hr_band |

**M2 末态**：9 张表

**存储机制**：`init_db()` 在 SCHEMA 之后跑 `conn.executescript(SCHEMA_M2)`，但 M2 阶段 `SCHEMA_M2` 实际是空字符串占位（注释说明 idempotent migration 走 `SCHEMA` 的 `IF NOT EXISTS`）。3 张睡眠表已经在主 SCHEMA 里。

**体重 / 体成分 / BMR**：
- 不增加新表，写 `measurements` 表
- 新增 stream：`body_weight` / `body_fat_pct` / `body_muscle_mass` / `bmr_kcal` / `body_water_pct` / `body_bone_mass` / `visceral_fat` 等

---

### M3 — workout 详情 + insights 周报 + nap session + dashboard 升级

**新增 6 张 workout 详情表**（从 `/v1/sport/run/detail.json` 解析）：

| 表 | 用途 | 端点字段 |
|---|---|---|
| `workout_route_points` | GPS 轨迹点 | `pointList` |
| `workout_samples` | 逐秒采样（HR/speed/cadence/...） | `secondList` |
| `workout_splits` | 公里分段 | `kmList` |
| `workout_laps` | 手表圈（自动 lap + 手动 lap） | `lapList` |
| `workout_pauses` | 暂停（auto-pause / 手动） | `pauseList` |
| `workout_hr_drift` | HR 漂移（前/后半程对比） | 派生自 `workout_samples` |

**新增 2 张 insights 表**：

| 表 | 用途 |
|---|---|
| `workout_insights` | workout 单次 insight（vs 历史基线，180 天窗口） |
| `weekly_reports` | 周报聚合 |

**M3 末态**：17 张表（= 当前状态）

**存储机制**：`init_db()` 在 SCHEMA 之后跑 `conn.executescript(SCHEMA_M3)`。6+2 张表都用 `CREATE TABLE IF NOT EXISTS` 幂等迁移，老 DB 自动加表。

---

### M4 — 限流应对 + 饮食流 + VO2max + 国际服 + dashboard 趋势线 + 压力 24h

**M4 不增加新表**，只往 `measurements` 里写更多 stream：

| 新 stream | 来源 |
|---|---|
| `food_*` | `normalizer/food.py`（4 个 macro 按日累加） |
| `vo2_max` | `normalizer/vo2_max.py` |
| `stress_24h` | `normalizer/stress.py`（5min sample-level，与睡眠 stage 对齐） |

**M4 末态（当前）**：**17 张表**（与 M3 末态一致）

---

## 修改 schema 的工作流

> ⚠️ 改 schema **必须改 `scripts/storage.py` 里的 `SCHEMA / SCHEMA_M2 / SCHEMA_M3` 常量字符串**，
> 然后同步更新 `sql/schema.sql` 镜像文件 + 本文件。`pull_to_sqlite.py init` 会读 storage.py。

```
1. 编辑 scripts/storage.py
   - M1 表 → 改 SCHEMA 字符串
   - M3 表 → 改 SCHEMA_M3 字符串
   - SCHEMA_M2 是 M2 阶段占位字符串（基本是注释），新加 M2 表也放 SCHEMA 里
2. 同步更新 sql/schema.sql
   - 保持与 storage.py 完全一致（列名 / 类型 / 索引 / UNIQUE 约束）
   - 按 5 个分组（基础/规范化/睡眠/运动/insights）组织
   - 头部注释加版本号 + 同步日期
3. 同步更新本文件 references/schema-evolution.md
   - 加新行到对应阶段
   - 更新"当前状态"表的张数
4. 验证：
   python3 -c "import sqlite3; c=sqlite3.connect(':memory:'); \
     exec(open('sql/schema.sql').read()); \
     print(len([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'\")]))"
   # 应该输出与 storage.py 一致的张数
5. 老 DB 兼容：所有 DDL 用 `CREATE TABLE IF NOT EXISTS`，新表自动加；老 schema 变更
   走单独的 `scripts/migrate_*.py`
```

---

## 已知文档 drift**

- **历史**：SKILL.md M1 章节提到"`workouts` 表" —— 实际没有此表，workout summary
  字段一直写 `measurements`（stream = `workout_summary` 等）。M1 描述用语问题。
- **历史**：旧 `sql/schema.sql` 头部声称"4 张表"，实际只列了 5 张，且完全没提睡眠 / workout
  详情 / insights —— 与 storage.py 严重 drift。**已在 2026-09-24 重写为 17 张表完整镜像**。