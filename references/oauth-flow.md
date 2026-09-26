# OAuth 流程详解

> 拆分自 SKILL.md。OAuth 三步流程 + APP_NAME=com.xiaomi.hm.health 的根因 + Zepp 服务端 (user_id, app_name) session 隔离机制。

---

## OAuth 三步流程

`scripts/zepp_oauth.py login` 自动跑 3 步 server-side OAuth，**不依赖浏览器**，**任何沙箱环境**都能跑。

### 完整流程

1. 从 `ZEPP_PHONE` 读手机号（去掉 +86 前缀）
2. 从 `ZEPP_PASSWORD` 读密码（或 `--password-file ~/.zepp-password`）
3. 跑 3 步 OAuth：
   - **Step ①** `POST api-user.huami.com/registrations/{phone}/tokens` → access_token
   - **Step ②** `POST api-mifit.zepp.com/v2/client/login` → app_token + user_id + domains
   - **Step ③** `GET api-mifit.zepp.com/v1/client/app_tokens?login_token=...` → refresh
4. 保存到 `~/.zepp-data/.secrets/token.json`（chmod 600）

成功后输出：
```
✓ Step ①+②+③ 全部成功
  user_id: 1000000000
  region: https://api-mifit-cn3.zepp.com
  ttl: 31536000s (365 天)
  app_ttl: 2592000s (30 天)
  saved: /root/.zepp-data/.secrets/token.json (chmod 600)
```

---

## ⚠️ OAuth 不会踢手机 Zepp App

**前提**：`zepp_oauth.py` 的 `APP_NAME = "com.xiaomi.hm.health"`（必须用 Mi Fit 退役 client id）

Zepp 服务端按 `(user_id, app_name)` 维度做 session 隔离。

### 根因（如果有人把 `APP_NAME` 改回 `com.huami.midong`）

- `com.huami.midong` = **Zepp App 自己的包名**
- 我们 OAuth Step ② (`/v2/client/login`) 一直接传 `app_name=com.huami.midong`
- 服务端认为这是"Zepp App 重新登录" → 把手机 App 的 session 踢下线
- 用户每次手动登录 Zepp App 也会让我们手里的 `login_token` 失效 → 恶性循环

**这个坑 2 周前踩过**（2026-09-08 ~ 2026-09-22）。

---

## OAuth 错误码

| 错误码 | 含义 | 解决 |
|---|---|---|
| **HTTP 200 + error=401** | 密码错或账号不存在 | 检查密码或换账号 |
| **HTTP 200 + error=403** | 账号被封 / 风控 | 稍后重试 |
| **HTTP 200 + error=404** | 账号不存在 | 检查手机号 |
| **HTTP 200 + error=408** | 密码格式不对 | 改密码（数字+字母+特殊字符 ≥8位）|
| **HTTP 403 (Spring)** | IP 风控 | 换网络或等 |
| **HTTP 400 + error_code=0104** | access_token 无效（内部错误）| 重试 |
| **error_code=0108** | access_token 真的过期 | 必须 OAuth |

### Bug 修复：OAuth 登录会踢手机 Zepp App（已修 · 重大）

**症状**：
- 沙箱里跑 `zepp_oauth.py login` → 手机 Zepp App 被强制下线
- 每天 17:00 cron 401 自愈时自动 OAuth → 用户每天被踢一次
- 持续约 2 周（2026-09-08 ~ 2026-09-22）

**根因**（已实测验证）：见上方"⚠️ OAuth 不会踢手机 Zepp App"。

**修法**（已合入 skill）：

`scripts/zepp_oauth.py` 改 2 行：

```python
# 改前
APP_NAME = "com.huami.midong"

# 改后
APP_NAME = "com.xiaomi.hm.health"   # Mi Fit 退役 client id, 独立 session 不冲突
```

同时把 Step ②/③ User-Agent 改成 Mi Fit 风格（让服务端以为是 Mi Fit 客户端）。

**验证结果**（2026-09-22）：
- ✅ OAuth 登录成功
- ✅ 新 `app_token` 与旧 token 字符串完全不同（前 35 字符）→ 服务端识别为独立 session
- ✅ 数据同步 1569 条
- ✅ **手机 Zepp App 保持登录状态**（用户亲测）

**关键参考**：
- https://github.com/DhavalBhimani44/zepp-mcp — 关键参考，源码 docstring 直接写了这个根因
- https://github.com/DhavalBhimani44/zepp-mcp/issues/5 — Zepp 服务端 session 隔离的 issue

**经验教训**：
- 看到 `com.huami.midong` 这种"看起来对"的硬编码就该警觉——可能是项目作者**用不同 app_name 绕开风控**的反例
- 当时应该立刻 clone ZeppBridge / zepp-mcp 读源码，而不是反复试 OAuth 参数
- 整个挖掘路径：token TTL 假设 → HAR 路线 → 浏览器 cookie 路线 → **最终在第三方项目的源码注释里找到根因**（用了 2 周）

**为什么这一步关键**：
- 之前 cron sync 自愈链路就算写出来，也会每天踢一次 App → 用户体验差到不可用
- 改完后 cron / OAuth / refresh 三种调用都不再影响 Zepp App session
- 用户手动登录 App 也不会让我们 token 失效 → 真正的双向独立

**踩坑对比**（之前 vs 现在）：
| 操作 | 之前 | 现在 |
|---|---|---|
| 沙箱 OAuth 登录 | ❌ 踢 App | ✅ 不踢 |
| 17:00 cron sync 401 自愈 | ❌ 踢 App | ✅ 不踢 |
| 用户手动登录 Zepp App | ❌ 我 token 失效 | ✅ 我 token 独立 |
| refresh app_token | ✅ 不踢 | ✅ 不踢 |

---

## OAuth 流程约束与最佳实践

- **不要 print 完整 token**（任何调试输出必须脱敏）
- **不要把 token 写到对话历史**
- **密码只在环境变量或本地文件**（chmod 600）—— **不通过对话告诉 AI**
- **任何"拉最新数据"操作不能跳过 OAuth 检查** —— "sync 显示成功但 0 条数据"是 token 失效的典型信号

### 标准 OAuth + sync 流程

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

### sync wrapper 与手动 sync 的 OAuth 处理差异

- `zepp_cron_sync.sh` wrapper（cron 01:00 / 09:00 / 17:00 自动跑）：**第一行就 refresh token**，失败自动 OAuth
- `python3 scripts/pull_to_sqlite.py sync`（手动跑）：**不检查 token**，过期会 401 但静默

**结论**：
- 平时**用 wrapper `zepp_cron_sync.sh`** —— cron 友好，token 自动续期
- 手动查"今天步数"想拿实时数据 → **先跑 wrapper 或先 OAuth 再 sync**
