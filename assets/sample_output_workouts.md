# 示例：`query_zepp.py workouts --top climb --limit 5`

> 这是**真实输出样式**（基于 `scripts/query_zepp.py:cmd_workouts` 的渲染逻辑）。LLM 在解释运动数据时，应严格按此格式组织回答。

## 命令

```bash
python3 ~/.minis/skills/zepp-data/scripts/query_zepp.py workouts --top climb --limit 5
```

## 输出

```
=== Top ===

  2026-09-14 07:32  户外跑步  18.42km   98min  ↑867m  1132kcal  (杭州)
  2026-08-26 06:18  户外跑步  21.15km  125min  ↑743m  1398kcal  (临安)
  2026-08-12 17:45  户外跑步   8.20km   45min  ↑512m   542kcal  (莫干山)
  2026-07-28 06:02  户外跑步  15.30km   89min  ↑445m   980kcal  (杭州)
  2026-07-15 07:15  户外跑步  12.60km   72min  ↑388m   820kcal  (杭州)

共 5 条
```

## 关键约定

1. **运动类型 → 中文名** 来自 `sport_catalog.py:sport_name(code)`：
   - `1` → 户外跑步（run）
   - `2/8` → 跑步机（treadmill）
   - `13` → 健走（walking）
   - `5` → 自由训练（free_training）
   - `24` → 室内健身（indoor_fitness）
   - 未识别类型 → `未知运动(<code>)`，但仍记录
2. **距离** 是 `dis_m / 1000`，单位 km（精度 0.01km = 10m）
3. **爬升** `↑N m` 是 `altitude_ascend`（累计上升，米）。M2 修复后已统一为米，不再有 cm 单位问题
4. **卡路里** 单位 kcal，**含 BMR**（基础代谢），不是纯运动消耗
5. **城市** 来自 Zepp 反向地理编码；没有显示 `?`

## 其它子命令样式

### 汇总（默认，无 --list/--top）

```bash
python3 ~/.minis/skills/zepp-data/scripts/query_zepp.py workouts
```

```
=== 运动历史汇总（87 条）===

类型        key                次数      距离     时长    卡路里     爬升
(中文)      (英文)              (次)     (km)   (h:mm)   (kcal)     (m)
-----------------------------------------------------------------
户外跑步     run                  42   312.45    25h32m   18942     8432
健走        walking              28    98.20    18h15m    6210      215
跑步机      treadmill            12    64.80     8h42m    3980        0
自由训练    free_training         5    ──          2h08m    1100        0

时间范围: 2024-03-12 ~ 2026-09-14
```

注意：没有 `dis_m` 的运动（如 `free_training`）距离列显示 `0.00`，汇总里会显示 `--`（取决于 sport 类型是否天然无距离）。

### 按距离 Top

```bash
python3 ~/.minis/skills/zepp-data/scripts/query_zepp.py workouts --top distance --limit 5
```

排序规则：`ORDER BY dis_m DESC`。注意**跑步机也算距离**（Zepp 跑步机会"估算"距离，靠步幅 × 步数）。

### 过滤特定运动

```bash
python3 ~/.minis/skills/zepp-data/scripts/query_zepp.py workouts --sport 户外跑步 --from 2026-08-01
```

支持中文 / 英文 key / alias（`SPORT_ALIAS`）。`--sport` 也接受 `run` / `treadmill` 等英文 key。

## 常见误读

| 用户说 | 真实含义 |
|--------|---------|
| "我跑的最远的一次" | `--top distance --limit 1` |
| "我爬升最多的一次" | `--top climb --limit 1`（注意：所有非户外运动爬升都是 0） |
| "上周跑了多少" | `--sport 户外跑步 --from YYYY-MM-DD` 然后 sum |
| "我的卡路里消耗" | workouts 里的 calorie 是**含 BMR**，纯运动消耗要 `compute_calorie_total.py` 算 |
