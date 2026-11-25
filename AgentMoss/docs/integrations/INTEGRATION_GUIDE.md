# L3 Prompt 接入说明

适用于当前仓库的 Python 版 AgentMoss。直接调用 AgentMoss 时，将最近一条用户请求放在 `prompt_session`，先前各轮放在 `prompt_history`，每轮包含 `text` 和对应的 `actions`，按原始时间顺序提交。通过 AgentGate 接入时，用 `user_prompt` 提供当前用户请求，网关会将它映射到 `prompt_session`。接口要求见 [AgentMoss HTTP API](../../README.md#http-api) 和 [AgentGate 接入说明](../../../AgentGate/README.md)。

## 模型看到的请求

当前用户请求默认尽量完整地进入 L3。超过字符上限时，AgentMoss 保留头部和尾部并标记中间省略的内容。脚本分析报告进入 L3 时也有独立的字符上限。两个上限在 token 预算为 `0` 时仍生效。原始请求和历史继续用于 L1、L2，脚本分析与独立的 Policy 生成继续使用各自的原始输入。

启用正数预算后，AgentMoss 依次压缩历史动作、较早历史轮次的文本，逐轮省略最早的历史，再处理当前动作详情、脚本分析、匹配规则、路径事实和执行理由。以上步骤完成后仍超预算时，才进一步压缩当前用户请求，并按配置保留最低字符量。L1、L2 的命中提示和禁用规则约束完整保留。如果命中提示本身已超过预算阈值，`hard` 直接跳过 L3 安全判断，`soft` 继续裁剪其他内容并交给 L3 模型客户端。

保留的历史轮次沿用原编号。带动作但没有意图文本的轮次，在 L3 中展示为动作类型摘要，也计入预算。QwenPaw 和 DeepAgents 将当前请求的文字放在 `user_prompt`，当前轮已记录的动作仍可随轮次传入。历史意图因单轮字符上限或 token 预算被截断，或因预算被省略时，模型会收到相应标记和授权指引。涉及“继续”等指代时，模型依据可见前文确认目标与授权范围。可见信息不足时，模型应在 `reason` 中说明依据不足，并按最小权限原则判断。

## 读取判定和降级

预算由运行 AgentMoss 的一侧配置。调用方可从 Python 分析结果、AgentMoss HTTP 响应或 AgentGate 响应读取 `l3_outcome` 和 `degraded`。

| 响应字段 | 对接方可判断的情况 |
| --- | --- |
| `l3_outcome=skipped`，且 `degraded` 含 `l3_budget_floor_exceeds` | 最低内容仍超硬预算，本次跳过了 L3 安全判断。 |
| `l3_outcome=skipped`，且 `degraded` 含 `l3_budget_unreachable` | 最低内容能放入硬预算，但最终 Prompt 仍超预算，本次跳过了 L3 安全判断。 |
| `degraded` 含 `l3_budget_unreachable` 且 `l3_outcome` 为模型调用结果 | 软预算未达标，AgentMoss 仍将裁剪后的 Prompt 交给 L3 模型客户端。`l3_outcome` 反映模型判断成功、失败或熔断。 |

是否执行动作以 `decision` 为准。硬预算跳过时，部署默认的 `fail_open` 允许通过前置检查的动作进入后续流程。`fail_closed` 拒绝这次动作。后续的有状态检测仍可能拒绝动作。启用了独立 Policy 生成时，跳过 L3 安全判断的请求也可能进入 Policy 生成流程。配置与实际用量说明见 [L3 Prompt 预算配置](../deploy/ENV_VARS.md)。
