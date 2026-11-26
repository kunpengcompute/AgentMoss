# L3 Prompt 预算配置

以下配置用于当前仓库交付的 Python 版 AgentMoss。HTTP 部署时，在启动 AgentMoss 的进程环境中设置。CLI 或嵌入 Python 应用时，在执行分析的进程环境中设置。同名配置优先读取环境变量，其次读取 `agent_moss_settings.json`，最后使用默认值。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `AGENT_MOSS_LLM_MAX_PROMPT_TOKENS` | `0` | L3 安全判断 Prompt 的输入 token 估算预算。非正数关闭预算，`1` 至 `1199` 按 `1200` 处理，`1200` 及以上使用配置值。 |
| `AGENT_MOSS_LLM_BUDGET_MODE` | `hard` | 正数预算下生效。最终仍超预算时，`hard` 跳过 L3，`soft` 记录降级后继续请求。 |
| `AGENT_MOSS_LLM_FAIL_MODE` | `fail_open` | L3 调用失败或被硬预算跳过时采用的判定策略。 |
| `AGENT_MOSS_LLM_HISTORY_MAX_ITEMS` | `20` | 最近历史动作的条数上限。`0` 表示初始组装时不限条数。 |
| `AGENT_MOSS_LLM_ACTION_DETAIL_MAX_CHARS` | `500` | 单条历史动作详情的字符上限。`0` 表示初始组装时不限长度。 |
| `AGENT_MOSS_LLM_PROMPT_HISTORY_MAX_TURNS` | `20` | 先取最近指定轮数的历史，再展示其中非空的用户意图。`0` 表示初始组装时不限轮数。 |
| `AGENT_MOSS_LLM_PROMPT_TURN_MAX_CHARS` | `500` | 每轮历史意图文本的初始字符上限。 |
| `AGENT_MOSS_LLM_PROMPT_TURN_MIN_CHARS` | `100` | 预算裁剪时，较早轮次的文本压缩下限。预算不足时仍可逐轮省略。 |
| `AGENT_MOSS_LLM_PROMPT_RECENT_TURNS_FULL` | `3` | 压缩较早的历史意图文本时，最近指定数量的有意图文本的轮次保留初始长度。`0` 表示所有轮次都可参与文本压缩。 |
| `AGENT_MOSS_LLM_PROMPT_SESSION_MAX_CHARS` | `8000` | 当前用户请求的初始字符上限，始终生效。 |
| `AGENT_MOSS_LLM_PROMPT_SESSION_MIN_CHARS` | `200` | 预算裁剪当前用户请求时的保留字符下限。短于此值的请求保留已有内容。 |
| `AGENT_MOSS_LLM_SCRIPT_ANALYSIS_MAX_CHARS` | `4000` | 脚本分析报告进入 L3 时的初始字符上限，始终生效。 |

数字配置使用整数，无法解析时回退到默认值。预算、历史条数、历史动作详情上限和轮数配置中的负数按 `0` 处理。新增的文本上限和下限要求正数，下限以对应上限为封顶。无法识别的预算模式使用 `hard`。

字符上限计算保留下来的原文，截断说明会额外占用 Prompt 长度。历史窗口内的内容还可由正数 token 预算进一步压缩。

## 预算未达标时

- `hard` 跳过 L3 安全判断调用，响应包含 `l3_outcome=skipped`。计算最低内容时，保留固定模板、当前请求的最低保留量、前置检测提示和适用的标记与指引。如果最低内容也超预算，降级码为 `l3_budget_floor_exceeds`。如果最低内容能放入预算，而按裁剪步骤得到的最终 Prompt 仍超预算，降级码为 `l3_budget_unreachable`。前置检查已有否决时保留拒绝。其余情况下，`fail_open` 允许动作进入后续流程，`fail_closed` 返回拒绝。
- `soft` 带着裁剪后的 Prompt 调用 L3，并记录 `l3_budget_unreachable`。调用失败时沿用 `AGENT_MOSS_LLM_FAIL_MODE`，`l3_outcome` 反映调用结果。

## 估算与用量

预算检查针对渲染完成的 L3 Prompt，模板、截断说明和授权指引都计入估算。估算公式为 `floor(字符数 / 2.5) + 1`。`hard` 使用 `floor(生效预算 / 1.15)` 作为通过阈值，预留估算余量。`soft` 直接用生效预算与估算值比较。模型的分词方式和消息包装会影响实际输入 token 数。

模型服务返回正数 `usage.prompt_tokens` 时，AgentMoss 按该值记录实际用量。正数预算生效且用量超过预算时，记录 `l3_budget_exceeded_actual`，本次判定仍依据已经完成的分析。该字段缺失或为 `0` 时，按未上报处理。模型返回正文即计一次调用，解析失败后重试的响应分别计数。

预算未达标和实际用量超额可以通过 `GET /metrics` 的 `agent_moss_l3_budget_events_total` 查看，按 `event` 标签区分事件码。

各入口如何传递用户请求和降级字段，见 [L3 Prompt 接入说明](../integrations/INTEGRATION_GUIDE.md)。
