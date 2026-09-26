# assets/ — Few-shot 样例库（给 LLM 看）

这个目录是给 **LLM 推理时的视觉/格式参考**，不是给用户看的。

## 文件清单

| 文件 | 用途 | 谁引用 |
|------|------|--------|
| [dashboard_7d_sample.html](dashboard_7d_sample.html) | 真实 dashboard 渲染样例（来自 `dashboard.py` 实际输出） | LLM 需要描述"看板长什么样"时引用 |
| [sample_output_recent.md](sample_output_recent.md) | `query_zepp.py recent --days 7` 的真实输出样式 + 解释约定 | SKILL.md 提到 "最近健康数据" 时引用 |
| [sample_output_workouts.md](sample_output_workouts.md) | `query_zepp.py workouts` 的真实输出样式 + sport code 映射 | SKILL.md 提到 "运动历史" 时引用 |

## 何时引用

- **LLM 解释 dashboard HTML 时** → 提示注入或 system message 中加 `读取 assets/dashboard_7d_sample.html` 让 LLM 知道配色/布局/指标命名
- **LLM 解释 recent 输出时** → 在 `query_zepp.py recent` 的结果后追加 `参考 assets/sample_output_recent.md 中的解释约定`
- **LLM 解释 workouts 输出时** → 同样追加 `参考 assets/sample_output_workouts.md`，特别提醒 sport code 映射

## 维护规则

1. 这些样例必须**真实**——从实际脚本输出复制，不要手写虚构
2. 脚本输出格式改了 → 同步更新对应 .md
3. 不要塞太多（< 50 KB/文件），LLM context 有限

## 不放什么

- 用户私有数据（行程、心率等真实数字）—— 只用合成样例
- 完整 HTML（dashboard_7d_sample.html 已经是单文件，方便 preview）
- 旧版本的截图 / 输出（保持 1:1 对应当前 main 分支）
