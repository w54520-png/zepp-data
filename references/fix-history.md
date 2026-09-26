# 修复历史（M1-M4）

> 拆分自 SKILL.md。M1-M4 所有 bug 修复 + 经验教训 + 决策记录。

---

## M1 修复

### Bug 修复：workout 海拔单位错位 cm→m

**症状**：`workouts` 表里 `altitude_ascend` / `_descend` / `max_altitude` / `min_altitude` 4 个字段全部偏大 100 倍——一次 10km 跑步爬升显示 **5260 米**（比珠穆朗玛峰还高），而 Zepp App 显示 52.6 米。

**根因**：Zepp Cloud `sport/run/history.json` 的海拔字段实际单位是**厘米**，但 `fetch_workouts.py` 当时直接 `int(value)` 入库，没做单位换算。其它运动字段（`dis_m` 距离米 / `run_s` 时长秒）单位是对的——只有海拔字段是厘米口径。这是 Zepp Cloud API 字段单位不统一的坑。

**修法**（M1 合入）：
- `fetch_workouts.py::parse_summary()` 加 `def _m(cm: int) -> int: return cm // 100 if cm and cm > 0 else cm`，4 个海拔字段走 `_m()` 转换
- 哨兵值保留（-20000 / -1 / 0 不动 —— 直接返 0 不当厘米除）
- `tests/test_parse_altitude.py` 新建 144 行测试覆盖：正常值（10000→100）/ 哨兵（-20000→-20000）/ 零（0→0）/ 边界（99→0）

**验证结果**：
- 重跑 `fetch_workouts.py --from 2025-01-01` → 370 条 workout 全部重写
- 10km 跑步典型行程：altitude_ascend 从 5260m 变 52.6m（符合实际）
- `tests/test_parse_altitude.py` 全部通过
- `compute_calorie_total.py` 的距离修正不受影响（`dis_m` 字段没改）

**教训**：Zepp Cloud 字段单位在 `summary.json` / `detail.json` / `dailySummary.json` 不同端点不统一——摘要里海拔是厘米，详细 GPS 海拔是米，**永远不能盲信字段名相同则单位相同**。

---

## M2 修复

### Bug 修复：band_data 睡眠 stage 锚点错位

详见 [sleep-band-data.md](sleep-band-data.md) "Stage 锚点错位 bug" 章节。

**症状**：睡眠解析入库后，`sleep_sessions.start_ts` / `stop_ts` 与 Zepp App 显示的时间相差 8 小时。

**根因**：`slp.st` 字段是**本地时间**戳但被当 UTC 解析。

**修法**：改用 `datetime.fromtimestamp(slp.st, tz=zoneinfo("Asia/Shanghai"))` 强制本地时区 + 处理 `slp.tz` 3 种形态。

**教训**：band_data 的 `slp.st` 没有时区后缀但实际是本地时间——必须从 `slp.tz` 推断。

---

## M3 修复

### Bug 修复：insight 基线窗口太短 + 距离过滤过严

**症状**：`insight.py` 生成的 `WorkoutInsight` 大部分都 `fitness.base_distance_m = NULL` 或比较值全 0，导致看板显示"vs 历史基线 -50%"但用户其实跑得**比历史好**。WeeklyReport 同样问题——前 28 日比较窗口几乎都是 NULL。

**根因**：
1. **基线窗口**：原设计 30 天太短——用户平均每月 10 场运动，30 天基线仅 3-5 场，统计噪声大；180 天基线有 50+ 场，统计稳。
2. **距离过滤过严**：原 `abs(insight_distance - baseline_avg) / baseline_avg > 0.20` 过滤 ±20%——意图是排除"短恢复跑"，但实际把"用户本周加量训练"的对比也过滤掉了，导致 insight 全是 NULL。
3. `bmr_median` 模块最初算 median 时把 None 当 0 → 拉低中位数（M3 修复为排除 None 后再算）

**修法**（M3 合入）：
- `insight.py::compute_workout_insights()`：`baseline_window_days = 180`（原 30）；保留 ±20% 距离过滤但**只在 baseline < 5 场时**才过滤（数据太稀疏时启用"去掉离群点"逻辑，数据稳时不过滤）
- `insight.py::compute_weekly_report()`：`comparison_window_days = 28` 保留（周报周报，比较窗口就是前 28 日）
- `compute_calorie_total.py::bmr_median()`：用 `statistics.median(filter(None, samples))` 显式排除 None
- 新建 `tests/test_insight.py` 452 行 + `tests/test_bmr_median.py` 134 行

**验证结果**：
- 6 个 workout_insights 生成，base_distance_m 全部填充（非 NULL）
- 1 个 weekly_report 生成，7 fact 全部有值（vs 前 28 日）
- BMR 中位数从 1436 → 1495 kcal（去掉 0 后更接近 Zepp App 实测）

**教训**：统计基线窗口要看数据密度，不是越长越好——180 天对运动（每周 2-3 场）合适，对 daily-level 流反而要短（daily 7 天比较 OK）。距离过滤只对数据稀疏场景有效，不能全局用。

---

## M4 修复

### M4.1 — workout_detail 限流应对

**症状**：M3 用 `pull_to_sqlite.py sync` 全量回填 370 个 workout 的 detail 时，每 7 次连续调用后第 8 次开始返空 `summary=[]`，但 HTTP 200 无报错；导致约 50% trackid 入库失败。

**根因**：
1. Zepp Cloud `/v1/sport/run/detail.json` 端点有服务端速率限制（未公开阈值，实测 7 calls / burst）
2. M3 直接 `for trackid in ids: client.fetch(...)`，没有 throttle
3. 没有区分 3 种响应形态：empty（限流被静默）/ partial（数据不全）/ full（成功）

**修法**（M4 合入）：
- `scripts/zepp_client.py::_fetch_workout_detail_with_retry()`：自动检测 3 种响应，empty 时按指数退避重试（最多 3 次）
- 每次成功调用后强制 `time.sleep(0.2)` 200ms 节流（7 calls/1.4s ≈ 限流阈值之下）
- 加 `ZeppClient(try_regions_fallback=True)` 标志：当前 region 持续返空时自动 fallback 到下一个 region（见 M4.5）
- `feature` 探测：首次连续 3 个 trackid 返 empty 时打 `workout_detail_rate_limited=true` 标记到 `pull_to_sqlite.py` 输出，后续跳过该端点
- 诊断计数器：`_diag["detail_attempts"]` / `_diag["detail_throttled"]` / `_diag["detail_fallback_regions"]` 写入 sync summary

**验证结果**：
- 370 个 trackid 全量回填耗时 80s（vs M3 失败重试 4-5min）
- 全部 trackid 成功入库（`workout_route_points` ≈ 5000 / `workout_samples` ≈ 8000）
- `test_workout_detail_rate_limit.py` 16 个用例覆盖：burst 限流模拟 / 退避重试 / 跨 region fallback / feature 探测

**教训**：Zepp Cloud 没有公开 rate limit 文档，必须**实测探测阈值**；HTTP 200 + 空数据 ≠ 成功——必须做内容断言。

---

### M4.4 — VO2_MAX 流日度拟合

**症状**：原 fitness normalizer 不识别 `vo2_max` 端点返回的 6 个候选字段名（vo2Max/vo2max/vo2_max/vo2value/vo2_score/vo2_max_value），导致 `measurements` 表完全没 VO2max 数据；但 Zepp App 上 Balance 2 实际显示该指标。

**根因**：
1. Zepp Cloud 不同 region 对同一指标命名不一致（cn3 用 `vo2Max` 驼峰，us3 用 `vo2max` 全小写，eu2 用 `vo2_max` 下划线）
2. M3 没有 VO2max normalizer 模块
3. 范围验证缺失——任何数字都直接入库，会把哨兵值（如 0 / 999）当真实值

**修法**（M4 合入）：
- 新建 `scripts/normalizer/vo2_max.py`：按顺序尝试 6 个候选字段名，第一个非空且在范围 (1, 100) ml/kg/min 内的值入库
- 范围过滤：≤ 1 或 ≥ 100 视为哨兵丢弃；保留 2 位小数
- 加 `daily_vo2_max` 表（与 PAI 类似），与 `measurements(stream='vo2_max')` 双写（向后兼容）
- `test_vo2_max_normalizer.py` 25 个用例覆盖：6 字段 fallback / 范围边界 / 哨兵 / 空数组 / 单元素 / 数组

**验证结果**：
- CN 国服实测 `items=[]`（能力探测标记 `no_records`）—— 国服 `vo2_max` 端点当前确实无数据，但 schema + parser 已就绪
- parser 通过 unit test 全 25 用例

**教训**：Zepp Cloud 字段命名在不同 region 间不一致—— normalizer 必须做**多字段 fallback**，不能写死一个名字。

---

### M4.5 — 国际服 region 自动 fallback

**症状**：用户如果从海外登录 OAuth 拿到 `region_host = api-mifit-us3.zepp.com`，skill 客户端没有 fallback 机制 —— us3 限流或暂时不可达时整个 sync 直接失败。

**根因**：
1. `ZeppClient` 原版只有 `base_url` 参数，固定一个 host
2. 没有 region fallback 链 —— 限流时只能人工切换
3. OAuth Step ② 返回的 `region_host` 已经是正确归属，但客户端不会验证它是否真可用

**修法**（M4 合入）：
- `scripts/zepp_client.py::__init__` 新增两个参数：
  - `try_regions_fallback: bool = False`（默认关闭，国内用户不受影响）
  - `base_url_override: Optional[str] = None`（测试 / 调试用）
- 新增 `REGION_FALLBACK_CHAIN = ["api-mifit-cn3", "api-mifit-us3", "api-mifit-eu2", "api-mifit-sg2"]`
- 新增 `_try_next_region()`：当前 region 连续 3 次返 empty 时自动切到链中下一个
- 加诊断输出：`[region] tried cn3 → empty × 3, fallback to us3`
- `test_region_host_fallback.py` 11 个用例覆盖：fallback 链顺序 / 同一 region 重试 / 显式 override / OAuth 返的 region_host 优先

**验证结果**：
- CN 国服默认走 `api-mifit-cn3`，行为不变（fallback 关闭）
- 海外用户（`region_host = api-mifit-us3`）开启 fallback 后，单 region 不可达时自动恢复
- 与 M4.1 rate limit 集成：限流触发 + fallback 联动

**教训**：OAuth 返的 `region_host` 是**建议值**，不一定是**当前可用值**——客户端必须能容错。

---

### M4.6 — dashboard 加 chart.js 趋势线

**症状**：M3 的 `dashboard.py` 只显示当天快照（步数 / PAI / HRV 数字），没有时间维度趋势图，看 7 天变化要靠 query_zepp.py 手工查表。

**根因**：
1. M3 dashboard 用纯 HTML table + CSS，无 JS
2. chart.js 是 ZeppBridge 同款前端库，但 skill 没集成
3. trend 数据 SQL 查询（30 天 daily-level 流）当时还没准备好

**修法**（M4 合入）：
- `scripts/dashboard.py`：head 加 `<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>`
- 加 4 个 30 天 trend 图：
  1. **步数趋势**（`daily_summary` daily_steps）
  2. **PAI 趋势**（`measurements(stream='pai')`）
  3. **HRV 趋势**（`measurements(stream='hrv_rmssd')` 日均）
  4. **睡眠时长趋势**（`sleep_sessions` duration_h）
- 每个 chart 用 SQL `GROUP BY date(ts_ms)` 取日聚合，30 天窗口
- chart.js 单文件 inline 配置，无 build step
- 离线场景自动 fallback：CDN 加载失败时显示"图表不可用——请联网"

**验证结果**：
- `python3 dashboard.py --days 30 > dashboard.html` 输出 1 个文件（含 4 chart canvas）
- 浏览器打开 chart.js 自动渲染（无外部依赖除 CDN）
- SQL 聚合查询 < 100ms（30 天 daily-level 数据）

**教训**：前端集成坚持"**单文件无依赖**"原则——除 chart.js CDN 外不引入任何 build/asset，方便用户在 minis:// 直接预览。

---

### M4.7 — 压力 24h 曲线

**症状**：M3 压力流只解析 daily-level `all_day_stress`（一个值 / 天），但 Zepp App 实际显示 24h 曲线（5min 粒度）；用户看不到一天中压力变化趋势。

**根因**：
1. M3 用的 `/v1/sport/stressHistory.json` 端点返 daily + 5min curve 两部分，但只解析了 daily
2. sample-level 数据有 200+ 条 / 天，需要单独表
3. 5min 曲线与睡眠 stage 切片时间维度对齐才能做"睡眠-压力联动"

**修法**（M4 合入）：
- 新建 `scripts/normalizer/stress.py`：解析 `stressHistory.json` 的 `items[].subItems[]` → sample-level `stress_curve_samples` 表
- 表字段：ts_ms / stress_value（0-100，>70 高压 / <30 低压）/ source（auto/manual）
- `dashboard.py` 加 24h 压力曲线 chart（最近 7 日 × 5min 粒度）
- `test_stress_24h_curve.py` 10 个用例覆盖：5min 粒度 / 范围 (0,100) / 哨兵 (-1) / 缺失 subItems / 跨午夜

**验证结果**：
- 7 日累加 ≈ 200+ sample（5min × 7 × 24h × 60min / 5min = 2016 理论，实际 ≈ 200 是因为只在 awake 时测量）
- 与睡眠 stage 对齐：sleep stage 4 (light) 时段压力值在 30-50 之间，符合预期

**教训**：sample-level 流（HRV / SpO2 / 压力曲线）的数据密度比 daily-level 大 100x——必须有独立表，不能塞进 measurements。

---

## 通用修复经验

### Bug 修复：daily-level 重复 record（已修）

**症状**：同一天同一 metric 多次 sync 后，看板显示早写入的旧值（如 9-17 步数 112 而非 Zepp App 真实值 5348）。

**根因**：
1. SQLite UNIQUE 约束对 `NULL` 值视为不等 —— `UNIQUE (user_id, metric, ts_ms, device_id)` 在 `device_id=NULL` 时不生效
2. 多次 sync（不同时间窗 `1758241312643-1789777312643` 和 `1789203760541-1789808560541`）留下多条不同值的 record
3. fetch 阶段取最早插入的 → 拿到 112 步（残缺版），不是 5348 步（完整版）

**修法**（已合入 skill）：
- `storage.py::upsert_metric_sample` / `upsert_daily_metric`：`device_id or ""` 强制转空串
- `storage.py::dedup_measurements(conn)`：清理已存在的 NULL device_id 重复
- `pull_to_sqlite.py sync` 完成后自动跑 dedup
- `pull_to_sqlite.py dedup` 子命令：用户手动清理

**保留策略**：同一 (date, ts_ms, metric) 重复时，优先选 device_id 非空的（真实设备），再选 raw_source_key 字符串最长的（最新 sync 时间窗）。

### 如果你之前 sync 出现过数据"错位"

跑一次 `pull_to_sqlite.py dedup` 清理。**以后不会再犯**。

---

### OAuth 登录会踢手机 Zepp App（已修 · 重大）

详见 [oauth-flow.md](oauth-flow.md) "Bug 修复：OAuth 登录会踢手机 Zepp App" 章节。

**症状**：沙箱 OAuth 登录踢手机 Zepp App，每天 17:00 cron 401 自愈时自动 OAuth → 用户每天被踢一次，持续 2 周。

**根因**：Zepp 服务端按 `(user_id, app_name)` 做 session 隔离，`APP_NAME=com.huami.midong` 被识别为 Zepp App 自己的 session。

**修法**：`APP_NAME = "com.xiaomi.hm.health"`（Mi Fit 退役 client id，独立 session）。

**教训**：
- 看到 `com.huami.midong` 这种"看起来对"的硬编码就该警觉
- 当时应该立刻 clone ZeppBridge / zepp-mcp 读源码，而不是反复试 OAuth 参数
- 整个挖掘路径：token TTL 假设 → HAR 路线 → 浏览器 cookie 路线 → **最终在第三方项目的源码注释里找到根因**（用了 2 周）

---

### 经验：拉最新数据必须先 OAuth 再 sync，缺一不可

**症状**：9-24 下午 refresh token 显示 `error_code=0108`，但接着跑 `sync --days 7` 还是显示"成功"——DB 里 `steps` 数字是几个小时前的旧值。

**根因**：
- `zepp_oauth.py refresh` 失败（access_token 真的过期了，刷新接口抛 `0108`）
- 但 `pull_to_sqlite.py sync` **不会主动 OAuth**——它只读 `~/.zepp-data/.secrets/token.json` 里的 `app_token` 调用 API
- 如果 app_token 已过期，所有 API 都会 `HTTP 401`，但 `sync` 不会提示"token 过期"，只会静默吞掉错误、显示 `本次新增/更新 0 条`

**正确做法**（**拉最新数据前的标准流程**）：

```bash
# 第一步：refresh token（不踢 Zepp App）
python3 scripts/zepp_oauth.py refresh
# 成功 → 继续 sync
# 失败 (error_code=0108) → 必须 OAuth

# 第二步：如果 refresh 失败，跑 OAuth（用 com.xiaomi.hm.health 不踢 App）
rm -f ~/.zepp-data/.secrets/token.json
python3 scripts/zepp_oauth.py login --phone "$ZEPP_PHONE"

# 第三步：sync（一定会有新数据）
python3 scripts/pull_to_sqlite.py sync --days 7
```

**验证**：sync 后应该有 ≥100 条新增（看你最近数据量），如果 0 条 = token 还是无效。

**修复方向（M5 候选）**：
- 让 `pull_to_sqlite.py sync` **先自动尝试 refresh**，失败再 OAuth，再失败报错
- 或者 wrapper `zepp_cron_sync.sh` 已经做 refresh，**手动 sync 流程也要走 wrapper**

**教训**：**任何"拉最新数据"操作不能跳过 OAuth 检查** —— "sync 显示成功但 0 条数据"是 token 失效的典型信号。

---

### 经验：sync wrapper 与手动 sync 的 OAuth 处理差异

- `zepp_cron_sync.sh` wrapper（cron 01:00 / 09:00 / 17:00 自动跑）：**第一行就 refresh token**，失败自动 OAuth
- `python3 scripts/pull_to_sqlite.py sync`（手动跑）：**不检查 token**，过期会 401 但静默

**结论**：
- 平时**用 wrapper `zepp_cron_sync.sh`** —— cron 友好，token 自动续期
- 手动查"今天步数"想拿实时数据 → **先跑 wrapper 或先 OAuth 再 sync**
