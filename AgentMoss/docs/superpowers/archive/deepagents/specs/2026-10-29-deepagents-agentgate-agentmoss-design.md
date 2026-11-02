# DeepAgents 接入 AgentGate / AgentMoss 插件设计

> 历史 v2 文档：用户已明确插件只做对接。最新方案为 [v3 对接方案](../../../specs/2026-10-30-deepagents-integration-only-design.md)，下文的本地文件证据/路径策略和 pwd-only 限制已在 v3 撤销。

初版：2026-10-29。修订：2026-10-30（v2，会话状态方案；已补入前轮 P1/P1/P2 与本轮 P1 路径精确匹配要求）。

状态：v2 已开发并部署至现有 VM，历史改为应用会话状态，独立插件 SQLite 已停止使用。实施与验收记录见 `/home/zhouq/code/deepagents-gate-integration-20261029/v2-evidence/REPORT.md`；本文件末尾的原始评审核对记录保留为历史，不代表当前实施状态。

## 本次调整

1. 移除插件专用 `agentmoss-plugin.sqlite`、数据库读写和数据库路径参数，不另建数据库、文件日志或 Redis 保存历史。
2. 参考 QwenPaw，从 agent 自身状态取得上下文；DeepAgents 使用 LangGraph 扩展状态保存最小执行记录，随应用已有 checkpointer 保存和恢复。
3. 区分“成功动作”和“可信读取证据”。当前 Moss 会扫描历史正文中的读取关键词，故不能把全部成功动作原样传入 action_history；采用下述保守读取投影，普通工具返回文本 output 不发送、不复制。
4. 主代理、同步子代理共享根会话，执行记录通过状态字段继承、回传及去重合并；不能只解析父代理消息或子代理摘要。
5. 撤销“插件独立保障跨重启防重放”的承诺。恢复依赖既有 checkpointer；外部副作用与 checkpoint 不具备原子性，不能承诺崩溃后恰好执行一次。
6. 增加受控根入口，校验外部字典、Command 和配置，阻止外部写保留状态；内部子代理仍按可信路径继承和回传状态。
7. 明确停止流的清理顺序：关闭图流并等待叶子退出，再清理临时视图、退出根上下文并释放根运行锁。
8. 文件路径精确匹配由插件本地执行前校验负责，不依赖 Moss 的路径提取、大小写转换或前缀匹配。不能确定全部写目标时拒绝，Gate Allow 或故障放行均不能绕过。

实施约束：不修改 SDK 或 Gate/Moss 引擎；已按下述流程更新现有 VM 的 DeepAgents Web。应用原有聊天存储、checkpointer 的后端由应用决定；已有 Web 应用使用 SQLite 不等于插件要求 SQLite。

## 目标与范围

```text
DeepAgents 工具调用 → 插件本地目标/证据校验 → AgentGate → AgentMoss
                         ↓ 不通过                     ↓
                     不执行并返回错误       Allow 且本地复核通过：执行
                                           Deny：不执行并返回错误
```

用户已确认：独立中间件插件、Deny 直接拒绝该次工具调用、本轮 L1/L2、移除插件独立 SQLite 并复用 DeepAgents 会话状态。

支持当前单进程、单 worker、LocalShellBackend，主代理及声明式同步子代理，包括 general-purpose。Python 脚本、CLI、自建 Web 服务均可显式接入，不依赖 Web 请求。预编译/远程后台子代理、其他 Backend 暂不支持，组装时明确报错。

本轮路径保证收紧执行范围：可修改文件的操作须有经过验证的完整结构化目标，任意 Shell/脚本及副作用未知的自定义工具不再默认可执行。execute 首版仅保留精确匹配的固定只读命令 pwd；不承诺任意命令执行能力。详见本地校验节，不得以“沿用 Shell 能力”覆盖此限制。

不新增执行结果回报接口、Dashboard 功能、人工审批、模型配置变更或操作系统沙箱；不把集成代码放入公开 DeepAgents 仓库或 fork。

## 哪些信息真正被使用

| 信息 | 当前实际用途 | v2 处理 |
| --- | --- | --- |
| 动作类型、操作详情 | Moss L2 先读后写、动作组合等规则；旧读取规则也扫描详情中的关键词 | 成功动作保留在状态；对 Gate 仅发送可信读取投影，覆盖取舍见下文 |
| 执行是否成功 | 表示整个工具返回正常，不证明 Shell 某个分支或脚本文本已经执行 | 保留于 agent 状态；与 verified_reads 分开 |
| 文件读取结果、Shell 标准输出 | 当前相关 L1/L2 不读取，现有 L3 历史格式化也不包含普通 output | 不发送、不复制；原工具结果仍按 SDK 正常返回 |
| ask_user_question 的用户回答 | Moss 授权规则确实读取 action_history.output | 当前插件未支持其可信成功历史；本次不新增支持，不伪造授权 |
| 用户提示和各轮动作关系 | Moss 意图一致性等规则 | 发送有界 prompt_history |

源码依据：

- QwenPaw `integrations/qwenpaw-plugin/agentmoss_gate.py` 从 `agent.state.context` 提取提示词和 ToolCallBlock 的 action_type/action_detail；没有独立数据库、执行结果提取或成功关联。v2 参考其状态来源，同时保留 DeepAgents 的成功校验。
- Gate `agent_gate/v1/server/models.py` 支持 output，normalizer 优先使用调用方显式提供的 action_history，再转发给 Moss。Gate 检查记录不代表执行成功，默认 DB 历史也不按执行成功筛选。
- Moss `agent_moss/engine/logic_rules.py` 的 read_before_write 扫描历史名称或详情中的读取关键词，并从整个详情提取路径，不验证 Shell 分支是否执行、文本是否仅为写入内容；password/user deletion consent 读取 ask_user_question 的 output。
- 同一读取规则把历史和当前路径转小写，并以 `path == rp or path.startswith(rp) or rp.startswith(path)` 匹配；Linux profile 的 extract_file_paths 会同时扫描无引号与引号路径，可能从已引用的完整路径中提取子串。因此即使是实际 read_file 的记录，也不能单靠历史格式化防止被扩大授权。
- Moss `agent_moss/engine/llm_analyzer.py` 的 `_format_action_history` 只格式化动作类型和详情，不能把“可接收 output”说成“普通 output 已用于判定”。
- DeepAgents `middleware/subagents.py` 将子代理自定义状态的变化作为 Command.update 返回父图；messages 默认只返回最终摘要。PrivateStateAttr 字段会在普通子代理输入和回传时排除。

读取证据随下一次检查的历史发送，不是执行完立即上报；没有下一次调用就没有额外上报。Dashboard 仍显示当前检查和判定，不承诺显示执行结果。调用方提供的当前会话历史不改变 Gate/Moss 自身跨会话、增量状态等其他历史来源的语义。

## 方案取舍

| 方案 | 取舍 | 结论 |
| --- | --- | --- |
| 插件独立 SQLite 账本 | 另存一份数据，增加路径、恢复和维护逻辑 | 移除 |
| 仅插件进程内存历史 | 简单，但 agent 恢复后仍丢历史 | 未选择 |
| 复用 DeepAgents 会话状态 | 沿用既有状态和 checkpointer，需要处理子代理合并、证据来源及输入边界 | 用户已选择 |

不直接把 AIMessage.tool_calls 当作成功历史：它只表示模型请求调用。仅扫描父图 ToolMessage 也不能恢复子代理内部动作，消息压缩还可能移除旧消息。

## 接线与状态结构

保留配置、GateClient、ActionAdapter、中间件和组装函数。HistoryStore 改为状态提取、合并和裁剪逻辑，不管理持久化文件或数据库连接。create_guarded_agent 对外返回受控根入口，原始编译图仅由插件和可信应用内部持有。

v2 接口（已实现）：

```python
runtime = GateRuntime.load(Path("/var/lib/deepagents/agentmoss-plugin.json"))
agent = create_guarded_agent(
    runtime=runtime, model=model, backend=local_shell_backend,
    checkpointer=existing_checkpointer,  # 沿用应用配置，不要求具体存储后端
)
result = await agent.ainvoke(
    {"messages": [("user", user_message)]},
    {"configurable": {"thread_id": session_id}},
)
# 受控入口校验输入，取得根锁并绑定根上下文，完成执行/清理后释放。
# 流式接口使用后文的 async with agent.astream(...) 契约。
```

runtime 每进程创建一次，退出时先排空仍在执行的根运行，再关闭 HTTP 客户端。受控同步、异步及流式入口统一管理根上下文与运行锁，应用不再依赖手工 root_context 充当完整运行边界。没有 checkpointer 时只承诺一次图运行内历史，不承诺跨调用或重启恢复，插件不自动建立替代存储。

设计决定：中间件注册有命名空间的扩展状态字段，例如 `agentmoss_history`，保存 schema 版本、根会话、原始用户轮次及最小执行事件。字段名和序列化细节由实现确定，须兼容应用已有 state_schema，不能覆盖其他中间件字段。

每条事件包含 root_session_id、scope、parent_call_id、tool_call_id、turn_id、原工具名、标准动作类型/详情、cwd、状态、判定来源和独立的 verified_reads（默认空）。事件键为 `(root_session_id, scope, tool_call_id)`；参数相同的新调用仍是新事件。提示词按轮次保存，不复制普通返回正文、文件读取内容、Shell 输出或任何凭据。

执行前登记 attempted；Deny 为 denied；返回后按 ToolMessage.status、已知工具契约和 Shell exit_code 确定 executed_success / executed_error / unknown。Shell exit_code=0 只代表该次 Shell 总体成功，绝不据此产生读取证据。异常、取消、缺少退出码及无法确认成功的自定义结果不冒充成功。可信内部返回 Command 时合并插件状态增量，同时保留原 graph/goto/update 等语义；这不意味着外部输入的 Command 具有相同权限。

### P1：读取证据及 Gate 历史投影

verified_reads 仅由已知专用 read_file 工具的实际完成结果产生，记录经同一 Backend/ActionAdapter 解析的原始大小写规范绝对路径、文件标识/版本指纹、读取范围及来源事件键。须校验实际绑定的工具实现及 backend，不能凭工具同名就认可证据。状态为成功只是必要条件，还须按锁定版本的工具/Backend 契约排除“错误文本但 status 未标 error”、空返回或无法确认结果；不能只靠不存在异常或 status=success。无法确认则不生成证据。读取范围不代表全文件已读，沿用文件级先读后写含义，不升级为全文阅读保证。

Shell（包括看起来是单条 cat 的命令）、write_file/edit_file 的写入文本、自定义工具、task 摘要以及 glob/grep/ls 不产生 verified_reads。首版不分析任意 Shell 控制流，不靠关键词或模型声称补造读取事件；需要先读后写证据时明确调用 read_file。

在不修改 Gate/Moss 引擎的约束下，action_history 及 prompt_history.actions **只包含 verified_reads 生成的标准 read 动作，并按本次完整写目标集合精确筛选**。非修改动作无需读取授权历史，传空动作列表；修改动作只投影与已验证目标精确对应的证据。不把读过的其他文件一并发送，希望旧引擎自行精确匹配。

action_detail 只包含经引用的真实文件路径，不携带脚本、写入内容、任意说明或原始 Shell 文本。引用只是传输格式，不是授权边界：引擎即使误提取额外路径，也不能越过下节的插件本地校验。全部成功动作仍在有界 agent 状态中用于诊断，不原样传进旧引擎。显式传空列表而不是省略，避免退回 Gate 的检查历史：

```json
{
  "action_history": [
    {"action_type": "read", "action_detail": "<与本次写目标精确对应的真实文件路径>"}
  ],
  "prompt_history": [
    {"text": "<原用户提示>", "actions": [
      {"action_type": "read", "action_detail": "<与本次写目标精确对应的真实文件路径>"}
    ]}
  ]
}
```

这项保守投影会缩小依赖完整 action_history 的组合链等检查覆盖，不能继续宣称“所有成功动作均参与历史分析”。每次当前动作仍完整送检，Gate/Moss 自身其他历史来源也仍按既有语义运行，但不能声称它们已补齐丢失的执行历史或修复了引擎的关键词推断。未来若要同时传完整行为史与独立读取证据，须另行设计结构化协议并调整引擎，不在本修订中暗中扩展。

不主动发送 output、成功布尔值或完整状态对象。Gate 请求模型补出的空 output 不影响语义。拒绝、失败、当前调用及未完成并行调用不产生证据。当前待执行动作仍包含完整原始参数，例如 write_file 的写入内容；不得用历史投影裁掉当前动作。

状态和请求均有界：最多最近 200 条成功事件、20 轮用户提示及 32 KiB 历史预算；读取证据与来源事件一起保留/回收，拒绝/错误诊断也有界。metadata.history_truncated 标记裁剪，不能仅限制请求而使 checkpoint 无限增长，也不承诺窗口外完整历史或永久去重。

### P1：插件本地保证文件目标精确匹配

**责任层：中间件内的本地路径/读取证据校验器。** Moss 的文本路径规则继续作为独立安全判断，不能承担精确授权；本地校验失败时直接不调用 handler，不因 Gate Allow 或 fail_closed=false 而放行。此规则是本修订新增的插件限制，不宣称修复了 Moss 引擎本身。

匹配直接使用结构化字段，绝不从 command、action_detail、用户提示或 Shell 文本反向提取授权路径：

1. ActionAdapter 根据原始类型化工具参数、当前 backend、cwd 和 virtual_mode，生成完整目标集合 T；read_file 的完成证据也来自同一解析链。相对路径、`.`/`..`、符号链接按锁定 Backend 真实语义处理；无法解析、越界、不支持的对象类型或目标集合不完整时拒绝，不能把失败当“新文件”。
2. Linux 路径保留原始大小写和完整字符串，以规范绝对路径逐字精确相等判断；不 lower/casefold，不作 Unicode 等价折叠，不作任一方向前缀匹配，不用目录读取授权后代文件。`report.txt.bak != report.txt`、`CONFIG != config`，单个包含空格和另一段绝对路径的文件名也仍是一个完整目标。
3. 读取前后核对相同文件标识/版本指纹，例如 st_dev、st_ino、st_size、st_mtime_ns、st_ctime_ns；无法确认稳定读取则不记证据。执行写操作前必须同时匹配规范路径和仍有效的指纹，不能仅凭 inode 相等让另一硬链接路径获证；符号链接若解析结果变了也不能复用原证据。写入/删除完成后使相关旧证据失效，重新读取才能建立新证据。
4. 对每个已存在普通文件目标 t，必须存在有效 verified_read，其 canonical_path 与 t 完全相同且指纹匹配；任一目标缺证据则本地 Deny。不得沿用 Moss 的提示词中出现“create/new file”就豁免既有文件的逻辑。多目标操作只有全部目标已知且逐个通过才可执行。
5. 新文件仅在 Backend 的创建契约已核对、父目录和路径确定且明确确认不存在时不要求旧读取；权限错误、stat 失败、悬空链接或存在性未知均不算不存在。目录删除/递归操作及无法枚举完整目标的操作首版拒绝，不用一条目录证据覆盖整棵树。
6. 普通 execute 不解析任意 Shell，默认归类为目标未知并本地 Deny，包括重定向、管道、条件/复合命令、脚本/解释器、动态变量与通配展开。首版唯一固定只读允许项为完整 command 精确等于 pwd，且命令解析环境由可信应用固定；不能从命令前缀、自然语言或声明“只读”推断安全。需要文件读取/修改时使用已支持的结构化文件工具。
7. 内置只读工具及 task/write_todos 采用核对过的固定契约，task 的叶子调用仍逐次校验。不认识的工具或不清楚副作用的自定义工具一律拒绝；只有可信应用注册并经测试的完整目标解析器和执行契约才可扩展，模型或外部请求不能声明自身目标/副作用来获准。
8. 在同一叶子锁内完成目标解析、证据校验、按 T 过滤历史及 Gate 判定；Gate 返回后、handler 执行前再确认目标和指纹未变，否则拒绝。实际 handler 使用相同原始参数及 backend，其他中间件若会改目标必须在最终参数处校验，不能检查一个目标后执行另一个。

例如只有 `/w/report.txt.bak` 的证据时，`edit_file(/w/report.txt)` 在步骤 4 就被拒绝，不能先把 `.bak` 记录送给 Moss，收到 Allow 后再当作精确校验已通过。大小写不同和带嵌入路径的另外两类反例同理。本地拒绝来源记录为插件校验，Dashboard 可能没有对应分析记录，不能伪称是 Moss 返回的 Deny。

此规则保证的是插件根据已解析目标和证据进行精确匹配；不是操作系统沙箱或原子的文件事务。当前叶子锁只能协调插件自身，需约定 workspace 不受其他并发写入者篡改；指纹前后复核不能消除检查到使用之间的全部竞态。若需要抵御恶意外部文件替换，须另外设计基于文件描述符的执行或 OS 隔离，不能仅凭 resolve/stat 宣称已实现。引擎对当前动作的文本解析仍可能误报，多目标或复杂路径被引擎额外识别也可导致保守拒绝；插件不通过降低本地条件纠正其 Allow。

## P1：受控根入口及可信输入边界

外部调用只能使用 create_guarded_agent 返回的受控入口；内部子图使用可信编译图，不能再次套外部输入过滤器导致继承丢失。应用必须对原始图、checkpointer 和状态修改接口实行访问控制；插件不能防御持有这些对象的任意应用代码，也不将当前 Web 认定为已经暴露这些入口。

受控根入口至少覆盖以下规则，并在调用任何图节点、写 checkpoint 或访问工具之前校验：

1. 外部字典实行显式输入白名单。默认只接收用户 messages，校验为用户消息，不能接受调用方伪造的 AIMessage.tool_calls、ToolMessage 或插件执行记录。应用扩展输入需显式注册，不能包含保留的历史、verified_reads、scope、schema 等控制字段；出现保留字段或未知字段明确拒绝，不能将它们交给 reducer。
2. 外部 Command 默认拒绝。有确实需要的受控恢复入口时，只接受经应用授权且对应当前 interrupt 的 resume；update、goto、graph、Send 均不接受。特别拒绝 Command.update 的字典、键值对序列、状态对象等全部表示形式，不只检查 dict。resume 值是经校验的工具恢复数据，不作为新的状态更新解析，也不能授权重做副作用未知的中断运行。
3. 从已认证的应用会话绑定 thread_id，并与根 UUID 一致。外部不能通过 configurable 的 checkpoint_ns/checkpoint_id、历史上下文或替换运行配置切换可信状态；额外配置按白名单接线，访问他人 checkpoint 属应用鉴权职责。
4. invoke/ainvoke、受管 stream/astream 均经过相同边界；batch、astream_events、update_state/bulk_update_state 等入口若未专门包装，就不向外部暴露。with_config、bind 或原始图属性不能成为绕过过滤的入口。
5. 中间件、已注册应用工具及 SDK 子图属于可信执行代码。内部父子状态经 schema/root/事件键校验后传递，外部输入不得冒充此通道；不信任的自定义工具不得返回写保留字段的 Command。checkpoint 恢复只读应用已有可信存储，不从请求体接收快照。

OmitFromInput 不是上述边界的替代：统一加在共享字段上会同时阻止 SDK 子图继承，而且不能作为所有 Command 更新的防线。本方案保留内部字段传递，根边界独立执行输入白名单。不得通过过滤后重新合并原始请求、开放原始图或外部 update_state 使边界失效。

## 子代理与并发

主代理和支持的子代理均注入中间件及状态 schema。子代理继承根历史，自己的动作及独立读取证据写入状态，通过 task 的 SDK 状态回传合并到父图；父代理后续检查能使用子代理的 verified_reads，不能拿子代理摘要充当证据。

reducer 按事件键幂等合并，不能简单拼接整份继承历史；并发 task、嵌套委派和 fork 不得重复计数或覆盖兄弟分支。状态字段须可在父子间传递，不能直接标为会被 SDK 排除的 PrivateStateAttr。根输入保护由上述受控入口实行，不能只靠注释“不允许注入”。

同根会话叶子工具仍串行检查、执行和提交；task 不持叶子锁等待子代理。LangGraph 同一步并行工具可能看到相同旧状态，仅加锁不够：每次根运行从图状态初始化共享临时视图，在锁内读取最近完成动作，再以状态增量提交给图。该视图只服务当前运行，结束/异常后清理，不作为跨请求历史仓库。

同根会话两个外部图运行须拒绝或排队，避免并发覆盖 checkpoint；不同根会话独立处理。保持单进程限制，不宣称进程内锁支持多 worker。

取消等待不等于终止 Shell 线程/命令，实际执行退出前不释放叶子锁，取消结果不记为成功。子图异常、取消导致状态未回传时，不从摘要补造成功动作；后续需要读取证据时重新读取。

### P2：流式消费和资源释放顺序

受控 astream 是显式管理生命周期的异步上下文接口，不向外部只返回裸异步生成器；同步 stream 对应提供 with 和 close。调用方必须将所有消费放在上下文内，包括提前 break、SSE 断开和异常路径。例如：

```python
async with agent.astream(inputs, config, stream_mode="updates") as updates:
    async for update in updates:
        yield encode_sse(update)
        # break 或客户端断开，都必须进入上下文的确定性清理。
```

根入口持有根运行锁和根上下文，创建内部 graph.astream 并登记仍在执行的叶子任务。所有结束路径严格按以下顺序：

1. 停止消费并禁止启动新的工具调用；通知仍在进行的图消费退出。
2. 在取消保护下显式关闭内部流（异步 await aclose；同步 close）。如另有任务正在 anext/消费，先取消并等待该消费任务退出，再关闭生成器，避免并发 aclose 报“生成器仍在运行”。只在 async for 后 break 或依赖垃圾回收不算关闭。
3. 等待所有已启动的叶子执行真正结束，以及子图/节点 finally 清理完成；aclose 返回也不能替代对执行任务的跟踪和等待。保留根上下文、临时视图及叶子锁直到这一步完成，未知结果不补成成功。
4. 在可提交的执行路径完成状态提交；无法提交则保守保留未知语义，不强行写“成功”。随后清空运行临时视图、重置根上下文，最后释放根运行锁，并将完成/取消状态交还应用。

同会话新请求在步骤 4 完成前只能等待或收到 busy，不能与旧运行重叠。清理中再次收到取消也不能跳过等待和解锁顺序；若执行无法确认已退出，维持该根会话 busy/不可复用并报告错误，不能因清理超时就解锁。强制进程退出则属于前述崩溃恢复限制。

Web 的 SSE 响应生成器必须在 finally 中退出上述受管上下文；响应层的断开/取消也须显式关闭外层生成器并等待清理。既验证插件流，也验证实际 HTTP/SSE 框架的关闭路径，不能假定 StreamingResponse 或消费者 break 会自动关闭全部嵌套流。应用关闭 runtime 时同样先停止接收新根请求、排空流及叶子任务，再关闭客户端。

## 身份、恢复和迁移

- 应用 session_id 与 LangGraph thread_id 使用稳定 UUID；Gate session_id=`deepagents:<UUID>`。主/子代理共享根 ID，scope、parent_call_id 区分内层调用。
- Gate 自行生成 agent_id=`deepagents:<真实 hostname>`，Moss 类型级 agent_id 回退为 `deepagents`。二者不同，沿用单部署实例约束，不承诺多租户隔离。
- 从既有 checkpoint 恢复时验证根 ID、schema 和读取证据来源，串会话或不兼容结构明确拒绝；旧 checkpoint 没有插件状态时从空历史开始，不自动导入 AI 调用或把成功 Shell 升级为 verified_reads。
- v1 SQLite 停止读写，升级前留备份，不要求迁移、删除或继续使用；旧会话需要重新读取相关文件以建立可信证据。
- 只有 checkpoint 已保存状态能恢复。已保存 attempted/unknown 不自动重做或当作成功；窗口内已有同一事件键也不重复执行。
- 副作用已发生、结果尚未提交 checkpoint 时存在恢复空窗，不能承诺跨崩溃 exactly-once。应用禁止自动恢复被中断运行中的待定副作用工具，须先核查实际状态、以新调用继续；相同 session_id 或完成标志无法消除此空窗。这项接线要求需单独测试。

## 沿用的行为与配置

保持 SDK 0.7.32 及现有依赖兼容检查、general-purpose 原有限制和既有权限中间件。工具映射继续为 execute→bash、read_file→read、write_file→write、edit_file→edit、ls→list_dir；delete 生成仅供分析的 rm 命令；glob/grep 保留模式和真实根。映射不表示允许执行：先适用上述本地目标策略，execute 首版仅支持固定 pwd，目录/未知副作用操作拒绝。virtual_mode 不构成 OS 沙箱。

保留配置 JSON、独立受保护的 token 文件、enabled、gate_url、timeout_seconds、fail_closed 及历史/请求限额；移除 output_bytes。单请求仍限 256 KiB，待执行动作不可截断后放行，凭据不写入聊天状态。

默认超时 5 秒、fail_closed=false，与 QwenPaw 一致；有效 Deny 始终不执行 handler，返回原 tool_call_id/name、status=error 的 ToolMessage。网络/HTTP/响应错误才按故障策略 bypass 或 Deny，且必须已通过本地目标/证据校验；本地状态、上下文、路径精确匹配、目标未知及请求超限错误始终拒绝。bypass 不显示成检测 Allow，网关不可用时靠应用日志及已提交状态诊断。

Gate 缓存保持关闭，部署时核实；Moss L2 的实际文件可见性仍依赖既有 workspace 只读 ACL。本次不改变 Gate/Moss 引擎、模型、L3、宿主网络和后台进程覆盖范围。

## 后续实施与验收

以下为 v2 实施与验收范围；实际结果及证据见实施报告：

1. 实现状态 schema、幂等 reducer、独立 verified_reads、指纹核对、本地精确匹配及按目标过滤的历史投影；先验三类路径反例，再验证成功/失败/Deny 和错误文本，旧 AI 调用不伪造证据。
2. 受控根入口及流生命周期先接好，再打通状态增量、子代理继承/回传和临时视图；验证可信内部 Command 保持、外部 Command 拒绝、并发 task、fork、取消、多会话隔离和根锁释放顺序。
3. GateRuntime.load 移除第二个路径参数；去掉插件 sqlite3 操作与 output_bytes，更新 README、测试、Web 接线、部署/回滚/验证脚本。
4. 用应用既有 checkpointer 验证成功动作恢复、pending 不作成功、旧 checkpoint 从空开始；不带 checkpointer 时不声称跨运行保留。跨崩溃副作用空窗列为限制。
5. 验证本地校验器→Gate normalizer→Moss→真实工具：不发送普通 output；专用 read_file 只授权同一精确目标，Shell/错误/其他路径不提供证据；子代理证据同样受精确匹配约束。固定 Gate Allow 及网关故障也不能绕过本地拒绝。完整行为链历史不在当前保守投影覆盖承诺内，以临时文件物理效果证明。
6. 验证插件全过程不新建或写入专用 SQLite，原应用聊天与 checkpoint 存储继续工作；状态/请求字段不包含普通工具返回正文。
7. 本地测试通过后再进入 VM 更新：先阅读 SERVER.md、核对版本，备份当前插件/Web/配置/checkpoints、保留旧数据库并记录恢复方式，仅重启 deepagents-web。

旧安装器仅识别最初未接入 Web 版本，升级必须支持当前 v1 已接线 app.py，不能直接套用或静默覆盖用户修改。回滚须恢复实际备份的 v1 插件、接线、配置及兼容的 checkpoint 状态，不删除用户聊天；需验证 schema 切换，不能只恢复 app.py 就宣称可回滚。

方案评审时先核对了数据消费者和状态传递路径；实施阶段已验证 reducer、并发视图、父子状态、恢复和流关闭。旧版实施计划仍保留为历史基线，不再按其 SQLite 任务实施。

### 三项评审的专门验收

| 评审 | 必须覆盖的场景 | 预期 |
| --- | --- | --- |
| P1 读取证据 | 空历史；规则基线用 `false && cat <临时目标>; true` 证明整体退出码 0；成功写出包含 `cat <目标>` 的脚本，但不执行；随后请求修改已存在目标 | 两类假证据均不生成 verified_reads。v2 对该复合 Shell 本身以目标未知拒绝；脚本文本不授权后续目标写入。以直接规则实验保留旧引擎反例，真实 read_file 成功后才可满足本地读取条件 |
| P1 读取证据 | read_file 失败、Deny、status 未正确标错的错误内容、取消、未知结果；主/子代理路径、空格及 Unicode | 均不能制造假证据；已验证读取的投影只含对应规范路径，父图可使用真实子图证据 |
| P1 精确路径 | 读取现存 report.txt.bak 后修改另一现存 report.txt；读取 CONFIG 后修改另一现存 config；读取单个完整路径 `'/w/copy /w/target.txt'` 后修改另一现存 `/w/target.txt` | 三组先证明空历史与本地拒绝基线，再证明旧引擎加入另一文件记录会误放行；接入本地校验后全部拒绝，handler 不执行、目标内容/指纹不变，三组都在子代理继承/恢复场景验证 |
| P1 精确路径 | 确认读取目标本身后修改；多目标只读取其中一个；读取后目标被替换/修改或符号链接改向；大小写、空格、Unicode 原样保留 | 同一目标且证据有效才可通过本地条件，其他情形拒绝；Gate 返回 Allow 或 fail-open 也不改变结果 |
| P1 未知目标 | Shell 中动态写入、复合命令/脚本、目录递归操作、未注册自定义工具、存在性检查失败；完整 command 为 pwd 的正例 | 未知目标一律本地 Deny，不执行；仅固定只读正例及明确目标的结构化工具按正常链路运行，不拿模型声明或引擎提取结果作为目标集合 |
| P1 输入边界 | 外部字典直接注入保留字段；伪造 ToolMessage；Command.update 的 dict、键值对序列、状态对象；Command.goto/Send 及改 checkpoint 配置 | 在节点执行和 checkpoint 更新前拒绝，无工具副作用、无可信状态改动；不只断言最终消息 |
| P1 输入边界 | 正常用户消息；内部子代理继承/回传；经授权恢复；绕开 facade 访问原始图/状态修改的尝试 | 正常路径可用，子图证据不被 OmitFromInput 丢弃；外部不可获得原始图或不受控修改入口，未支持恢复明确拒绝 |
| P2 流清理 | 消费者提前 break、显式退出受管流、生成器异常、SSE 断开/取消、清理期间二次取消；立刻发同会话新请求 | 用真实 LangGraph 和事件屏障证明旧流已关闭、节点 finally 及叶子完成后才释放根锁；新请求此前 busy/等待，且不同根会话仍可运行 |

评审核对记录（2026-10-30）：本地以真实 LogicRulesChecker 和临时文件复现两个假读取反例：空历史 hit=true，加入跳过读取的成功 Shell 或写出脚本文本后 hit=false；加入规范 read 历史后 hit=false。已据此收紧设计。输入 schema、Command 更新形态、子代理状态传播及现有 Web 的流消费路径已按锁定源码核对；这是方案阶段的核对结论；受控入口、受管流及这些反例现已按实施报告验收。

本轮路径评审核对：在本地创建互不相同的现存临时文件，真实 LogicRulesChecker 三组结果均为空历史 hit=true、加入另一文件读取记录后 hit=false；规范路径精确比较均为 false，引用单路径的例子还产生两个提取结果。实验只验证规则，未覆盖目标文件。已将精确授权责任落到插件本地校验，并明确未知目标拒绝、Shell 能力收紧及并发文件替换的边界；这是方案阶段的状态；新校验器现已实现并按实施报告验收。


## 2026-10-30 实施补充

- 插件版本 0.2.0；状态字段为 agentmoss_history，schema=2。源代码位于 integrations/deepagents-plugin，既有 worktree 分支保留开发提交，主目录已同步代码。
- 组装时拒绝在 HarnessProfile 排除插件，拒绝位于检查之后的 profile tail 工具 hooks/动态工厂。可信改参中间件放在普通 middleware 列表，插件检查最终参数；不支持 HITL 编辑工具参数及外部 resume。现有 allow/deny 文件权限和技能保留。
- 新文件要求已知现存父目录；原生绝对路径下删除符号链接对象明确拒绝，不能用被指向文件的证据授权另一个对象。
- 读取证明核对实际 SDK 结果窗口头和非空范围，拒绝错误文本，即使 status 错标成功。拒绝的 Shell 不复制正文到状态；提示和诊断清理已知 Gate 令牌及 sk 形式的凭据。原始工具返回仍由 SDK 正常管理。
- 实际升级、回滚、再次升级、现有应用 checkpoint 恢复、真实配置模型的 HTTP/SSE 聊天均已验证；旧插件数据库保留供 v1 回滚，v2 运行不读取或写入。安装使用既有镜像源，其他依赖版本逐项核对不变。
- 一次独立审查报告了三项 Important 问题并已通过回归测试修复；审查工具最终响应受内容审核阻断，未取得完整 clean 报告，其余收尾由作者自查，详见实施报告。
