# Zepp OAuth / API 错误码参考

> P1.5 补充：分阶段提示（Step ①/②/③/refresh）+ 新增 STEP3_FAIL
>
> 来源：`scripts/zepp_oauth.py` 的 `hints` 字典 + 历史 P0.2 修过的 0101 / 408

## 怎么读错误信息

OAuth 失败信息会带上 `stage`（`step1` / `step2` / `step3` / `refresh`）和 `error_code`：

```
✗ 登录失败 (step1): error_code=01408
  提示：[Step ① 密码登录] 密码格式不对（error=408）。Zepp 要求 8-30 位数字+字母+至少 1 个特殊字符（如 . _ -）
```

`stage` 对应 OAuth 流程的哪一步失败：
- **step1** = 密码登录拿 `access_token`（POST `/registrations/{account}/tokens`）
- **step2** = 用 access_code 换 `app_token` + `user_id`（POST `/v2/client/login`）
- **step3** = 续 `app_token`（GET `/v1/client/app_tokens`）
- **refresh** = `zepp_oauth.py refresh` 子命令续期

---

## 错误码速查表

| error_code | 阶段 | 含义 | 处置 |
|---|---|---|---|
| `01401` | Step ① | 密码错或账号不存在（HTTP 401） | 检查 `ZEPP_PASSWORD` 是否正确 |
| `01403` | Step ① | 账号被封 / 风控（HTTP 403） | 换网络或等 30s 重试 |
| `01404` | Step ① | 账号不存在（HTTP 404） | 检查 `ZEPP_PHONE` 是否正确 |
| `01408` | Step ① | 密码格式不对（HTTP 408） | Zepp 要求 8-30 位数字+字母+至少 1 个特殊字符（如 `.` `_` `-`）。忘了原密码去 Zepp App 重置 |
| `NO_ACCESS_TOKEN` | Step ① | 服务器没返回 access_token | 可能 IP 被风控。**换 VPN 再试**（之前 P0.2 修过 408 但没补这条） |
| `NO_TOKEN_INFO` | Step ② | 没返回 token_info | 检查 access_token 是否有效（罕见，通常 Step ① 已经把锅拦下来） |
| `STEP3_FAIL` | Step ③ | 续 app_token 失败 | 跑 `python3 scripts/init.py --rotate` 重新登录（P1.5 新增） |
| `HTTP 400` | 网络 | 请求格式错 | 检查 phone 格式或 client_id |
| `HTTP 403` | 网络 | IP 风控 | 换网络或换 VPN |
| `0101` | refresh | refresh token 真的过期 | 跑 `python3 scripts/init.py --rotate` 重新登录（P1.5 把这条从 login 路径里挪到 refresh 路径，并补了提示文案） |

---

## P0.2 已修但易踩坑的几条

### `01408` / 密码格式

Zepp 国服要求密码必须含 **数字+字母+至少 1 个特殊字符**（`.`、`_`、`-` 都算），长度 8-30。如果手机 Zepp App 注册时是纯数字密码或者只用字母 + 数字，会在这里撞墙。

修法：去 Zepp App → 我 → 设置 → 账号与安全 → 修改密码，**先改成符合规则的密码**，再跑 `python3 scripts/zepp_oauth.py login`。

### `0101` / refresh token 过期

login_token / app_token 寿命有限（ttl ~30 天）。一旦过期，refresh 命令会拿到 `error_code=0101`。

注意：完整 OAuth login 会踢一次手机 Zepp App（因为 Zepp 服务端按 `(user_id, app_name)` 维度做 session 隔离）。所以推荐平时用 `refresh`，30 天一次才跑完整 login。

### `01403` / `HTTP 403` / IP 风控

Zepp 国服对单 IP 的 OAuth 频率有限制。频繁跑 login（脚本 bug 触发重试、cron 配错等）会让 IP 进临时黑名单，**等 30s ~ 5 分钟** 或换 VPN/网络就能恢复。

---

## sync 0 条的诊断（pull_to_sqlite.py P1.6）

`pull_to_sqlite.py sync` 跑完如果显示 `本次新增/更新 0 条 records`，会自动打一段诊断：

```
💡 sync 0 条诊断（按可能性排序）：
  ⚠️ token.json 已 45 天未更新 — 可能过期。跑 `python3 scripts/zepp_oauth.py refresh`
  💡 上次 sync 距今 12 分钟 — 可能真的没新数据
  💡 8 个流为 no_records: v2_events:HRVRMSSD:real_data, ...
      （stream 暂时无数据 ≠ 不支持，可能你那段窗口没戴表）
  💡 窗口只有 3 天 — 试着 `--days 30` 或 `--days 90` 拉更宽
```

**判定逻辑：**
1. **token.json 状态** → mtime > 30 天 = 大概率过期
2. **上次 sync 距今** → < 1 小时 = 很可能真的没新数据
3. **流的 capability** → no_records ≠ unsupported。`no_records` 是 Zepp 服务端返回空数据（可能你没戴表），`unsupported` 是 stream 不被识别
4. **窗口太窄** → `--days 3` 拉不到历史数据很正常

---

## query 缺失数据的诊断（query_zepp.py P1.7）

`query_zepp.py metric --metric X --days N` 如果打 `✗ 没有 X 的数据`，会再打：

```
💡 可能原因：
   1) 你这段时间没戴表（try `--only-measured` 排除占位数据）
   2) 流 capability 是 no_records（try `python3 query_zepp.py capabilities`）
   3) Zepp App 还没生成这条数据（try `python3 query_zepp.py recent --days 30` 看 daily_summary）
   ↳ DB 里有 124 条历史数据（2024-03-01 ~ 2026-09-15），试着把 --days 调大或用 `range` 子命令
```

进一步：如果 DB 里**完全没**这个 metric，会提示去查 capabilities；如果 DB 里有但窗口外，会给出历史日期范围让用户拉大 `--days`。

`query_zepp.py range --from X --to Y` 的空数据提示见 [P1.7 在 query_zepp.py 里](https://github.com/...) —— 提示 3 条：扩窗口、重新 sync、查 capabilities。

---

## 调试技巧

- **`zepp_oauth.py status`** → 看 token 状态（user_id / region / extracted_at）
- **`zepp_oauth.py refresh`** → 不踢手机 App 的续期（首选）
- **`zepp_oauth.py login`** → 完整 OAuth（会踢手机 Zepp App）
- **`init.py --rotate`** → 删 token + 重新 OAuth（最后手段）
- **`query_zepp.py capabilities`** → 看每个流的 status（available / no_records / unsupported）

把 `error_code` + `stage` 报给 LLM 通常能直接定位问题。