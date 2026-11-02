# DeepAgents 对接插件 v3：安全判定归 Gate/Moss

状态：0.3.0 已实现并部署验收，本机与 VM 各96项通过，实际Gate/Moss/模型/存储往返通过；证据见 v3-evidence/REPORT.md。用户已明确选择插件只做对接，并授权调整方案、实现和验证。本设计取代 v2 在插件中补偿 Moss 规则问题的部分，直接执行，不再重复征求同一职责选择。

## 职责和取舍

插件准确获取工具类型、原始参数、cwd、用户提示和会话/委派身份，调用 Gate，再按 Allow/Deny 和故障配置继续或拒绝。Moss 的关键词误认读取、路径前缀/大小写等规则问题由引擎负责；本次不改 Gate/Moss。取消 FileGuard、verified_reads、文件指纹、读取窗口解析、按目标过滤历史、执行前文件证据核对、未知副作用工具拒绝及 pwd-only 限制。

比较：继续插件补偿违背用户边界；同时改 Moss 超出本次授权范围；因此采用参考 QwenPaw 的对接职责。保留必要的参数转换、鉴权、请求大小限制和运行一致性校验，它们不判断动作是否危险。

## 接线与历史

配置默认使用 GateRuntime.load() 读取 DEEPAGENTS_GATE_*；应用可通过 load(settings["agentgate"]) 或 load(GateConfig) 复用已有配置，显式配置不混合环境变量。显式 load(Path) 仅保留旧接线兼容，不自动发现或创建独立配置文件。凭据支持 API_TOKEN 或 API_TOKEN_FILE，二者不能同时非空；启用却缺凭据或非法配置明确报错。保留 create_guarded_agent、受控 invoke/ainvoke 和 with/async with stream 接口。支持当前 LocalShellBackend、主代理和同步声明式子代理，SDK 自身的工具集合和 allow/deny 权限不扩大。用户注册的自定义工具正常送检，Gate Allow 后运行，不按工具名称限制。

沿用应用 checkpointer，不新增插件数据库。采用新状态字段 agentgate_history（schema=3），保存有界工具调用上下文。旧 agentmoss_history 不导入为完整调用史，因为 v2 只有诊断摘要；旧聊天正常保留。新命名空间使实际 v2 回滚无需把 v3 状态强改为 v2。

与 QwenPaw 一样，历史是 tool_calls：包含之前的请求及其类型/参数，不声称执行成功。拒绝、失败、并行未完成调用也不因 Moss 读取语义而从调用上下文中过滤；metadata 明示 history_semantics=tool_calls，并提供对应调用状态供诊断，Gate/Moss 是否使用由其自身协议决定。当前调用不放入自己的历史。普通返回 output 不复制、不上报；调用参数原本就可能包含写入内容，这是当前动作和调用上下文的一部分，不能把它称为普通工具输出。

最多 200 条调用、20 轮、32 KiB。超限优先整条丢弃最旧历史并标 history_truncated，不截断某条命令后冒充完整调用。已知凭据脱敏；原始执行参数不修改。检查当前动作请求超过 256 KiB 时报告接入错误，不截断后执行。

## 一致性与生命周期

稳定 UUID 根会话和 Gate session_id=deepagents:<UUID> 不变；子代理继承并回传上下文，事件按 root/scope/call_id 幂等合并。窗口内同一调用不重复执行。同根根运行互斥、叶子串行和运行临时视图保留，task 不持叶子锁等待子任务。

保留根输入白名单、外部 Command/checkpoint 注入拒绝、未完成 checkpoint 不自动重放、取消/重复取消时排空物理工具、SSE 关闭顺序以及 runtime 关闭准入互斥。这些保障接线一致性，不作为读取/写入安全规则。内部受信任工具 Command 保留更新和路由语义。

SDK profile 不得移除插件或在检查后改写工具调用；未知 tail 工具 hooks 继续明确不支持，可通过普通 middleware 在检查前改参。保持版本校验与已有 Backend 一致性要求。

Gate 明确 Deny 不执行 handler；Allow 执行原 handler；网关错误按 fail_closed 配置（默认 false）处理并记录 Bypass，不伪称安全 Allow。本地异常仅针对接入状态/传输/生命周期，不因文件是否读过、是否新文件或 Shell 内容自行判危险。

## 验收和部署

1. 用 Mock Gate Allow 证明原先受限的普通 Shell、既有文件未读修改、自定义工具均能运行，Gate Deny 均不执行；任一文件是否读过由 Gate/Moss 判定。
2. 正常读、写、Shell、自定义、task 历史均携带原类型/参数；失败/Deny 历史明确是调用上下文；不保存 output；状态和请求有界、当前调用不自包含。
3. 主/子/fork/并行/多会话、已有 checkpoint、v2→v3→v2 往返、同步及异步、SSE 断开和重复取消回归。
4. 真实 Gate/Moss 下用普通临时 Shell、文件工具及子代理验证实际判定和物理效果，不把引擎缺陷误报为插件已修复；保留实际已知局限。
5. 备份 v2 release、当前 Web/配置/依赖锁和应用 checkpoint/chat；只重启 DeepAgents Web，不变更模型、Gate/Moss 或宿主网络。实际回滚至 v2并恢复 v3，数据保留；主目录和 VM 源码哈希一致。

## 实施补充

- 原始参数结构保存在当前请求 metadata.original_arguments 和历史 metadata.history_arguments；文件适配文本保留真实路径，其他参数用带键 JSON 编码，避免不同 old_string/new_string 边界拼成同一文本。状态中的对应参数也保留结构、仅对已知凭据脱敏。
- 内部 Command 支持 update=None、dict 及 SDK update 元组语义；保留 graph/goto，GraphBubbleUp 不当成接入异常吞掉。真实声明式子代理返回父图路由已回归验证。
- LocalShellBackend 的 SDK 自身不支持 permissions 参数时保留原报错；不会静默删权限，也不承诺 SDK 没有实现的能力。

现场兼容边界：通过 SDK 恢复的逻辑状态核对 v2/v3 往返，原有聊天及 v3 调用上下文均能恢复；旧 v2 期间的调用不从诊断摘要导入为完整 v3 历史。底层 saver 的单条 checkpoint.channel_values 不是完整逻辑状态，不能据此判定通道丢失。插件不另建存储或自行回溯覆盖用户数据。


2026-10-31 本机配置入口调整：Web 使用 load()，已知旧 Web 仅替换配置入口；部署脚本只可选备份旧 JSON，不创建新 JSON，不自动修改应用服务环境。部署前由运维将上述凭据放入既有 DeepAgents 服务环境。默认 Gate 故障策略和历史限额不变；仅本机更新，未部署到 VM。
