# Zepp Data Skill

把 Zepp Cloud（华米云）上的 Amazfit 手表/手环健康数据**同步到本地 SQLite**。

**所有数据都存在你机器本地**——`$ZEPP_DATA_DIR`（默认 `~/.zepp-data/`），**不上传到任何服务器**。

> 📍 **Minis 应用里实际路径**（运行身份 `root`，`$HOME=/root`）
>
> | 项目 | 实际位置 |
> |---|---|
> | Skill 代码 | `/var/minis/skills/zepp-data/` |
> | 默认数据目录 (`~/.zepp-data/`) | `/root/.zepp-data/` |
> | 数据库 | `/root/.zepp-data/zepp.db`（或 `$ZEPP_DATA_DIR/zepp.db`）|
> | Token | `/root/.zepp-data/.secrets/token.json`（chmod 600）|
> | 密码文件（可选）| `/root/.zepp-password`（等价于 `~/.zepp-password`）|
>
> 如果你跑在 macOS / 自己的 Linux 上，`~/.zepp-data/` 会解析到你自己的 `$HOME` 下。

## 支持的数据

| 流 | 说明 |
|---|---|
| HRV RMSSD | 心率变异性（5min 采样）|
| 血氧 | 单次测量 + 夜间事件（ODI / 呼吸暂停）|
| PAI | 每日 + 七天累计 + 三档区间（13 字段）|
| 压力 | 日聚合 + 5min 曲线 |
| 体电荷 | Hybrid Charge / Readiness |
| 步数/卡路里 | 每日汇总 |
| 呼吸率 | avg / min / max |
| 训练负荷 | 当日 + 7 天滚动 |
| 心率 | 自动测量 + 运动中 |

完整字段清单见 [SKILL.md](SKILL.md)。

## 快速开始（任何环境）

### 1. 装好 skill

**在 Minis 应用里**：skill 已预装在 `/var/minis/skills/zepp-data/`，**无需任何操作**，直接跳到第 2 步。

**在 macOS / 自己的 Linux 上**：把 `zepp-data` 目录放到任意路径（建议 `~/skills/zepp-data/`），命令里用绝对路径调用脚本，例如：

```bash
python3 ~/skills/zepp-data/scripts/zepp_oauth.py login
```

### 2. 设环境变量（一次性）

```bash
# 国服：手机号 + 密码
export ZEPP_PHONE=186XXXXXXXX
export ZEPP_PASSWORD='你的真实密码'
```

> ⚠️ **安全**：**不要把密码发到对话**——AI 完全看不到环境变量。

### 数据目录自定义（可选）

默认所有数据存到 `~/.zepp-data/`（数据库 + token + 日志）。如果这个目录被系统保护、或者你想换个位置：

```bash
export ZEPP_DATA_DIR=/path/to/writable/dir
```

所有脚本（`pull_to_sqlite.py` / `query_zepp.py` / `daily_report.py` / `dashboard*.py` 等）都会自动读这个变量。**v4.0.1+ 所有脚本都尊重这个变量；v4.0.2+ `zepp_client.py` 也用这个变量解析 token 路径**（修复了"设了 `ZEPP_DATA_DIR` 但 token 还在 `~/.zepp-data/` 导致 401"的常见 bug）。

详见 [references/path-handling.md](references/path-handling.md)。

### 手机号格式

**国服账号**（`api-mifit-cn3.zepp.com`）：
- `--phone` 传**11 位裸数字**（代码自动加 `+86` 前缀）
- 例如 `--phone 186XXXXXXXX`（代码内部变成 `+86186057XXXXX`）
- 如果服务端不接受 `+86`，多半是**国际服账号**改用 `--email`

**国际服账号**（`api-mifit-us3.zepp.com` 等）：用 `--email user@example.com`，不需要 `--phone`。

### 3. OAuth 登录

```bash
python3 scripts/zepp_oauth.py login
```

**任何环境都能跑**（macOS / Windows / Linux / Minis / Coze）—— 不依赖浏览器。

输出：
```
✓ Step ①+②+③ 全部成功
  user_id: 1000000000
  region: https://api-mifit-cn3.zepp.com
  ttl: 31536000s (365 天)
  app_ttl: 2592000s (30 天)
  saved: ~/.zepp-data/.secrets/token.json (chmod 600)
```

### 4. 拉数据

```bash
python3 scripts/pull_to_sqlite.py sync --days 7
```

输出 DB 状态：~1000 raw records, ~2000 normalized measurements。

### 5. 查询

```bash
python3 scripts/query_zepp.py recent --days 7        # 最近 N 天摘要
python3 scripts/query_zepp.py metric --metric pai_daily --days 30  # 单一指标
python3 scripts/query_zepp.py range --from 2026-09-01 --to 2026-09-18
python3 scripts/query_zepp.py devices               # 设备列表
python3 scripts/query_zepp.py capabilities          # 流能力
python3 scripts/query_zepp.py summary               # DB 摘要
```

## 完整命令清单

### `scripts/zepp_oauth.py`

OAuth 登录（3 步流程，server-side，无浏览器依赖）：

```bash
# 推荐：从环境变量读密码
export ZEPP_PASSWORD='xxx'
python3 scripts/zepp_oauth.py login --phone 186XXXXXXXX

# 也支持从 stdin 读（交互式）
python3 scripts/zepp_oauth.py login --phone 186XXXXXXXX

# 也支持从文件读（推荐 chmod 600）
# Minis 里实际路径是 /root/.zepp-password；其它系统是 ~/.zepp-password
python3 scripts/zepp_oauth.py login --phone 186XXXXXXXX --password-file ~/.zepp-password

# 检查 token 状态
python3 scripts/zepp_oauth.py status
```

### `scripts/pull_to_sqlite.py`

拉数据并落 SQLite：

```bash
# 增量 7 天（默认）
python3 scripts/pull_to_sqlite.py sync

# 增量 90 天
python3 scripts/pull_to_sqlite.py sync --days 90

# 3 年全量
python3 scripts/pull_to_sqlite.py sync --days 0

# 仅创建 DB（不带 schema）
python3 scripts/pull_to_sqlite.py init

# 清理 daily-level 重复（device_id NULL 历史残留；sync 自动跑）
python3 scripts/pull_to_sqlite.py dedup

# 用 OAuth 流程登录
python3 scripts/pull_to_sqlite.py login --phone 186XXXXXXXX
```

> ⚠️ **`sync` 默认不拉运动历史（workouts）。** 运动流 `workout_history` + `workout_detail` 是单独的 stream，需要单独跑：
>
> ```bash
> python3 scripts/fetch_workouts.py --from 2026-01-01
> ```
>
> 否则 `workout_detail` 流会报"no workouts in window"或 `workouts` 表缺失错误。**v4.0.3+ 这个提示会在 sync 时自动出现**，v4.0.2 及之前版本需要你手动跑 fetch_workouts.py。

### `scripts/query_zepp.py`

查询 DB：

```bash
python3 scripts/query_zepp.py recent --days 7
python3 scripts/query_zepp.py metric --metric steps --days 30
python3 scripts/query_zepp.py range --from 2026-09-01 --to 2026-09-18
python3 scripts/query_zepp.py devices
python3 scripts/query_zepp.py capabilities
python3 scripts/query_zepp.py summary
```

### `scripts/dashboard.py`

生成 HTML 看板（Chart.js）：

```bash
python3 scripts/dashboard.py --days 30 > ~/.zepp-data/dashboard.html
# Minis 里等价于 /root/.zepp-data/dashboard.html；可改成 > /var/minis/workspace/dashboard.html 直接预览
```

分主题展示：
- 📊 活动（步数/卡路里/活动分钟）
- ❤️ 心血管（静息/最大心率）
- 💪 PAI 与训练（PAI + 训练负荷）
- 😤 压力（均值/极值/4 档比例）
- 💗 血氧（单次/夜间 ODI）
- 😮‍💨 呼吸（avg/min/max）
- 🔋 体电荷

输出单文件 HTML，可在任何浏览器或 minis:// 路径打开。

## 数据存放

完整路径速查见文首「Minis 应用里实际路径」表。

| 路径 | 内容 | 权限 |
|---|---|---|
| `$ZEPP_DATA_DIR/.secrets/token.json`（默认 `~/.zepp-data/.secrets/token.json`）| 你的 Zepp token（app_token + user_id + region）| chmod 600 |
| `$ZEPP_DATA_DIR/zepp.db`（默认 `~/.zepp-data/zepp.db`）| 你的健康数据 | 你自己控制 |

**默认数据目录是 `~/.zepp-data/`**（即 `$HOME/.zepp-data`）；**可用环境变量 `ZEPP_DATA_DIR` 覆盖**——所有脚本（`pull_to_sqlite.py` / `query_zepp.py` / `dashboard.py` / `zepp_oauth.py` / `init.py`）都会自动跟随，token 也会落到新目录的 `.secrets/token.json` 下。

## 当前支持

- **国服**（`api-mifit-cn*.zepp.com`）✅
- **国际服**（`api-mifit-us*.zepp.com` 等）⏳ TODO

## 安全

- 代码里 `token` / `app_token` 字段不会被 print 完整（只显示前 20 字符）
- token.json 权限 600（只有你能读）
- 分享 skill 时**不会**带你的 token 或数据——只带代码
- **密码绝不进对话历史** —— 用环境变量或本地文件

## OAuth 协议

3 步 password grant flow（reverse-engineered）：

```
Step ①  POST api-user.huami.com/registrations/{phone}/tokens
         (HTTP form, client_id=HuaMi, password, ...)
         → access_token (从 redirectUri 的 ?access= 解析)

Step ②  POST api-mifit.zepp.com/v2/client/login
         (grant_type=access_token, code=<access_token>, third_name=huami, ...)
         → app_token + user_id + domains

Step ③  GET api-mifit.zepp.com/v1/client/app_tokens?login_token=...
         → refresh / 确认 app_token
```

参考实现：
- [miloce/Zepp-Life-Steps](https://github.com/miloce/Zepp-Life-Steps/blob/main/zepp登录接口.py) — 最完整社区实现
- [rolandsz/Mi-Fit-and-Zepp-workout-exporter](https://github.com/rolandsz/Mi-Fit-and-Zepp-workout-exporter) — 用 Playwright 浏览器

## License

MITm/rolandsz/Mi-Fit-and-Zepp-workout-exporter) — 用 Playwright 浏览器

## License

MIT