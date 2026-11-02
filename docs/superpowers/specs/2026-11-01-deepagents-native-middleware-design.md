# DeepAgents 官方 Middleware 对接方案（v0.4）

状态：用户已确认使用官方 SDK、插件只做对接、应用管理请求并发和流关闭；指定 context 字段为 moss_info。2026-11-01 用户进一步明确授权继续方案设计、开发及真实接口验证，本轮依此实施，不重复询问同一授权。

## 目标与方案选择

采用官方 create_deep_agent(middleware=...)、声明式 subagents[].middleware 和原生 invoke/ainvoke/stream/astream；不提供替代图工厂或图代理。dcode Hooks/扩展适合官方终端宿主，不是本次自建 SDK 应用的接入入口；继续自定义工厂/运行代理违背用户确认的职责边界。因此选择原生 SDK Middleware。

必须保留：Gate Allow 后执行原工具、明确 Deny 不执行；Shell/文件/自定义/task 调用上下文；稳定根会话和主子身份；主/声明式同步子代理（含 general-purpose/fork）；子历史返回父状态；checkpointer 持久化；同步、异步和原生流式；环境变量/应用配置；无插件数据库、无 Moss 规则补偿、无普通工具 output 上报。

不改 Gate/Moss 引擎，不扩展到 dcode、远端/异步子代理或未安装中间件的编译图。第一阶段沿用已验证依赖版本（deepagents 0.7.32 等），不宣称任意版本兼容。

## 对外接入

导出 AgentMossMiddleware 与 GateRuntime。GateRuntime 只持有配置、HTTP 客户端、脱敏和关闭能力；不管理根运行、锁或图。移除 create_guarded_agent、GuardedAgent、assembly.py、compat.py、entrypoint.py。

应用创建 LocalShellBackend，并给 AgentMossMiddleware(runtime, backend) 传入与实际工具相同的 backend；通过官方 create_deep_agent 创建主图。每个隔离型子代理显式安装中间件，默认 general-purpose 也显式声明，子代理模型、工具、skills、权限和提示词由应用通过公开配置明确提供，插件不复制 SDK 默认栈或读取 profile 私有函数。fork 允许按 SDK 规则继承，但验收仍证明实际工具被检查。

每次原生调用传入：

```python
context={"moss_info": {"session_id": session_id, "user_prompt": user_text}}
config={"configurable": {"thread_id": session_id}}
```

moss_info 由可信应用构造，不来自模型参数或未经校验的请求字典。session_id 是非空稳定字符串；thread_id 存在时必须与其一致；Gate session_id=deepagents:<session_id>。user_prompt 是根用户文本，不用子任务替换。上下文可为 dict 或应用自有 context_schema 对象，其 moss_info 字段为可变字典；其他应用字段不受影响。自定义工具须使用与 context_schema 相匹配的 ToolRuntime[dict]/ToolRuntime[AppContext]，避免 SDK 默认 None 上下文的序列化错误。每次根调用使用新的 moss_info 字典，SDK 向子调用传递同一上下文。

SDK 原生 context 只存在于本次运行，不进 checkpoint。中间件在 moss_info 中绑定一个内部临时运行对象（保留键 _agentmoss_run，应用不设置/持久化这个键），其中包含一次运行的历史共享视图及叶子锁；没有全局会话表。该对象不会进入用户消息、Gate payload 或图状态。使用前校验类型与根会话，拒绝冲突的外部值。

## Middleware 生命周期与身份

before_agent/abefore_agent 为根运行从 state.agentgate_history 初始化 RunHistory，生成 turn_id，记录原始用户输入。子调用合并继承状态，不重新创建根视图或轮次。after_agent/aafter_agent 将最终可序列化历史快照写回状态；没有工具调用的用户轮次也保留。

wrap_tool_call/awrap_tool_call 从 request.runtime.context 读取 moss_info，使用原始 request.tool_call 和 state，检查后调用 SDK handler 一次或返回错误 ToolMessage。返回 Command 合并自定义历史时保留应用状态与 graph/goto，使用公开 Command.update/graph/goto 属性，不调用 _update_as_tuples 私有方法。原生 DeepAgents 文件中间件期望工具 Command.update 为字典，应用使用字典更新；插件对序列字段的无损合并单独做单元回归，不承诺 SDK 原图支持序列工具返回。

委派 lineage 只由 task 中间件在调用原 handler 的动态范围内以 Python ContextVar 绑定，finally 重置；主身份始终从官方 context 获取，不靠自定义根入口设置 ContextVar。scope=main/<task_call_id>:<subagent_type>，parent_call_id 为父 task id。同一次运行的多个 task 分支共享运行视图，分支 lineage 相互隔离。显式不支持绕过 task 的自定义委派，除非其应用自行传递相容上下文并完成单独验证。

## 历史与并行

沿用 agentgate_history/schema=3、事件去重键 root/scope/call_id、seq 更新顺序、调用 order、prompt 分组、状态字段以及 200 条/20 轮/32KiB 窗口。保留 v3 数据含义，普通 output 不复制；旧 v2 通道不导入完整调用历史。

当前调用先取历史快照再 begin，不进入自己的历史。所有工具类型、完整参数和 denied/failed/attempted/unknown 状态都按 tool_calls 语义保留，不构造可信读取证据。

中间件只对本轮叶子调用的“取快照→分析→原 handler→状态记录”串行化，保障完成的前序调用能被后续检查看到；task 不持该锁等待子任务，防止死锁。共享运行视图解决 SDK 并行分支持有旧状态快照的问题；持久化只有官方状态 reducer/checkpointer。

## 请求适配与公开接口

保留 execute→bash、read_file→read、write_file→write、edit_file→edit 等词汇映射，文件参数正文用带键 JSON；当前参数完整送检（256KiB 上限），历史整条裁减。

不调用 SDK backend._resolve_path：复用官方公开 validate_path 作同一文件工具规范化（包含反斜杠和前导斜杠），再按公开 LocalShellBackend 的 cwd/virtual_mode 转换实际路径；pathless grep/glob 和 glob("/") 按 backend 搜索 cwd 的公开语义处理。不添加路径授权/文件证据规则。Backend 自身校验保留，非法路径的原工具仍不会被插件绕过；该行为用实际 SDK 工具验证，而不是复制私有校验函数。

metadata 包含真实 hostname/os、原工具名、原参数、scope/parent_call_id、history_semantics/statuses/arguments/truncated。Gate 按其现有机制生成 agent_id。Gate 网络/HTTP/响应故障按 fail_closed（默认 false）记录 Deny 或 Bypass，明确 Deny 始终不执行。接入错误返回对应 tool_call_id 的错误。执行参数不因脱敏改变。

## 应用职责与边界

应用验证外部输入，只构造文本用户消息与可信 session/context；原图和 checkpointer 只给可信应用代码访问，不接收外部 Command、保留状态写入或任意 checkpoint_id/ns。插件不再代替应用建立输入网关。

应用对同一 thread_id 的根请求串行化；不同线程/进程的调度由应用承担。本插件不提供全局根运行锁、不排队、不替换调用接口。

流式应用使用原生 generator，并通过 contextlib.closing/aclosing 或 finally 显式 close/aclose。SSE 断开和取消后，应用先结束/等待原生图执行及底层工具，再释放其会话锁；不要把“消费者 break”当作后台工具已经停止。中间件的异步叶子在取消时等待已开始的 handler 完成再释放叶子锁，不承诺强行终止线程或 Shell，也不把未知执行结果标成功。根调用取消后原生 checkpoint 可能仍 pending，由应用检查，不盲目重放副作用。

应用负责收尾后再关闭 GateRuntime。工具改参中间件应在 Gate 前完成；检查之后再改参数的 tail hooks、可编辑 HITL/resume 不在本轮保证范围内。不把原生图 API 可调用等同于所有工作流均已验证。

## 验收

1. 只用官方 create_deep_agent 与原生 invoke/ainvoke/stream/astream，返回真实 SDK 图；不依赖替代入口、profile 私有函数或 backend 私有路径函数。
2. Gate Allow 的普通 Shell、既有文件改写、自定义工具可实际执行；Deny 时物理目标不变，结果含正确 tool_call_id。
3. dict/应用 context_schema 的 moss_info 正确传递；根 thread mismatch、缺字段、跨会话状态拒绝；工具参数不能替换身份。
4. 显式 general-purpose、isolated/fork、同批并行 task、叶子并行及多会话；子动作回传、历史顺序、当前调用不自包含、同调用只执行一次。
5. 应用已有 checkpointer 多轮恢复、无工具轮次、v3 通道兼容；无插件 SQLite、无原始 output 泄露、请求/历史有界、凭据脱敏。
6. 原生流提前关闭、异常/取消和实际线程/短时 Shell；应用在 cleanup 后复用会话时无旧执行重叠。测试应用负责输入过滤/串行/关闭，证明职责边界不是文字承诺。
7. 本机临时 Gate/Moss API（隔离端口和数据、L1/L2）+实际 SDK 图：读取前改写被 Moss 拒绝，真实读取后允许；普通 Shell/新文件/子代理敏感读及身份和数据库调用记录；危险命令仅做分析文本不执行。使用固定测试模型，不产生模型 API 请求。

实现前做独立 builder check；实现后测试与独立代码评审，真实接口证据保存在插件交付目录之外。新实现只同步到用户新的 boostkit/AgentMoss 项目，保留备份；不改旧原始仓库、不接触 devbox/VM、不重新引入部署目录。
