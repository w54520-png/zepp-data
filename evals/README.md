# zepp-data 评测集 (Evaluations)

按 Anthropic Skill 规范 / 豆包 Skill 规范布局：
- **trigger 正向**：验证 skill 在该触发时被路由到
- **trigger 负向**：验证 skill 不被误触发（避免污染对话）
- **task quality**：端到端跑任务，验证输出满足期望

## 文件清单

```
evals/
├── README.md                  # 本文件
├── should-trigger.yaml        # 9 个正向 case（应当触发）
├── should-not-trigger.yaml    # 8 个负向 case（不应触发）
└── task-quality.yaml          # 4 个端到端质量 case
```

合计 **21 个 case**：
- `should_trigger=true`：9
- `should_trigger=false`：8
- end-to-end 任务：4

## 评测集合规性

按方法论要求，触发评测覆盖：

| 类别 | 正向 | 负向 |
|---|---|---|
| OAuth 不踢 App | trigger_001 | – |
| 同步数据 | trigger_002 | – |
| 7 日报告 | trigger_003 | – |
| 运动历史 | trigger_004 | – |
| 睡眠 | trigger_005 | – |
| 体重/BMI | trigger_006 | – |
| Dashboard | trigger_007 | – |
| BMR | trigger_008 | – |
| 国际服 | trigger_009 | – |
| 编程/天气/股票 | – | not_trigger_001/002/003 |
| 学习/做饭 | – | not_trigger_004/005 |
| 手表选购/咨询/Apple Watch | – | not_trigger_006/007/008 |

9 正向 + 8 负向 → **充足负样本**（方法论要求）。

## 怎么跑

### 1. 路由触发评测（trigger）

需要把每个 `user_query` 灌给 LLM，看它是否调用了 zepp-data skill：

```bash
# 推荐用小 LLM 跑批量（gpt-4o-mini / claude-haiku）：
for q in $(yq '.cases[].user_query' evals/should-trigger.yaml); do
  echo "Q: $q"
  answer="$(llm_call "$q" --tools [zepp-data,...])"
  echo "$answer" | jq '.tools_used[]?'
  echo "---"
done | tee /tmp/trigger_results.txt

# 计算 precision / recall：
# - 正向 trigger % = (LLM 用了 zepp-data 的正向 case 数) / 9
# - 负向 trigger % = (LLM 没用 zepp-data 的负向 case 数) / 8
```

### 2. 端到端质量评测（quality）

需要真实 DB（`~/.zepp-data/zepp.db`）+ 真实 token：

```bash
# 一次性准备数据：
python3 /var/minis/skills/zepp-data/scripts/pull_to_sqlite.py init
python3 /var/minis/skills/zepp-data/scripts/pull_to_sqlite.py sync --days 7
python3 /var/minis/skills/zepp-data/scripts/fetch_workouts.py --from 2025-01-01

# 跑 quality case：
yq '.cases[].id' evals/task-quality.yaml | while read id; do
  echo "=== Running $id ==="
  # 用 yq 抽 expected_steps，转成 bash，跑一遍
  # ...
done
```

更简单：直接照 `expected_steps.command` 一条一条跑，看输出符不符合 `expected_keywords`。

### 3. 离线评测（only keywords match）

如果只想做 keyword 校验（不跑真实命令）：

```bash
python3 - <<'PY'
import yaml, json, re, subprocess
for path in ['evals/should-trigger.yaml', 'evals/should-not-trigger.yaml',
             'evals/task-quality.yaml']:
    cases = yaml.safe_load(open(path))['cases']
    for c in cases:
        print(f"[{c['id']}] {c.get('user_query','')[:50]}")
PY
```

## 怎么加新 case

1. **确定类别**：trigger / quality？正 / 负？
2. **写 user_query**：用户原话，必须是真实使用场景，不能"理想化" query。
3. **写 expected_action**：不要写"应调 zepp-data"——要写**具体调哪条命令**。
4. **写 expected_keywords**：5-10 个，不可漏掉关键实体名。
5. **写 pass_criteria**：能机器判定 yes/no 的标准。

### YAML schema

```yaml
- id: trigger_xxx_xxx          # 命名：trigger_<NNN>_<slug> / not_trigger_<NNN>_<slug> / quality_<NNN>_<slug>
  category: <str>             # 用于聚合统计
  user_query: <str>           # 用户原话
  should_trigger: <bool>      # only for trigger YAML
  expected_action: <str|multiline>  # 应当/不应当做的事
  expected_output_keywords:   # list[str]，可枚举
    - kw1
    - kw2
  pass_criteria:              # list[str]，可机器判定
    - condition1
    - condition2
```

## 当前通过率

baseline 占位（无 CI 集成）：

| 类型 | case 数 | 通过 | 通过率 |
|---|---|---|---|
| should-trigger (true) | 9 | _TBD_ | – |
| should-not-trigger (false) | 8 | _TBD_ | – |
| quality (end-to-end) | 4 | _TBD_ | – |

> 第一次跑请把所有 `_TBD_` 替换成实测值；后续 commit 在 `baseline_date:` 旁添加 `last_run:` 字段。

## 后续补 case 的方向

按方法论"评测集应当丰富"原则，建议加：

1. **mixed flows**：单条 query 触发多种操作（例："同步 + 出周报"）
2. **error path**：refresh 失败 0108、token 过期、network error
3. **数据安全**：token 不出现在日志、密码不 echo
4. **database integrity**：sync 100 次 + 1 GB 数据后查询性能（bound < 200ms）
5. **idempotency**：same `sync --days 7` 跑 3 次，结果应当 byte-equal（dedup 验证）
6. **rate-limit handling**：连续 8 次 workout detail，第 8 次应 sleep 而非失败
7. **region fallback**：us3 token 过期 → 自动 fallback eu2 → sg2
