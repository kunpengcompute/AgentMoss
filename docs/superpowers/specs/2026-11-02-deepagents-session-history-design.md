# DeepAgents 按会话共享历史的简化方案

日期：2026-11-02。状态：方案②已确认并完成实施、独立评审和本机 API 验证；交付插件 0.5.0。

## 目标与来源

同一个 `session_id` 的工具调用进入同一份历史，不再追踪调用来自主agent还是哪个 subagent。工具执行前仍请求 Gate，明确 Deny 不执行，Allow 才继续 SDK 原工具流程。

来源有三项：QwenPaw 的执行前检查与会话历史上报；DeepAgents 官方 Middleware、运行 context、状态 reducer 和 checkpointer；用户本次选择的“精简结构”方案。上次设计中的主agent/subagent 身份追踪由本文替代，其余接入约定继续适用。

## 方案比较与选择

1. 最小修改：删除身份字段，保留 `CallContext`、`RunBinding` 两层对象。改动少，但保留了用户阅读时遇到的对象层次。
2. 精简结构：把上述两类合并成一个 `RunContext`，所有已安装中间件的 agent 在一轮运行中共用它。用户已选择此方案。

仅使用图状态、不保留本轮共享历史对象，没有纳入可选方案：并行 subagent 持有各自的状态快照，返回后的 reducer 合并不能让执行中的下一次检查立即看到其他分支已完成的调用。

## 保留的对外接口和范围

应用继续使用 `GateRuntime`、`AgentMossMiddleware(runtime, backend)`、官方 `create_deep_agent()` 和原生调用接口，每个隔离 subagent 仍显式安装中间件，包括 general-purpose。

每轮由应用新建 `context.moss_info`，提供非空 `session_id` 和字符串 `user_prompt`；`thread_id` 存在时必须一致。Gate 会话编号继续为 `deepagents:<session_id>`。

本次不合并 GateRuntime，不调整配置、HTTP 客户端、backend 支持、工具词汇或路径转换，不修改 Gate/Moss 引擎，不新增数据库、根运行入口或全局会话注册表。普通工具输出不另存或上报。

覆盖范围沿用当前版本：主agent、声明式同步 subagent、general-purpose、fork、同步/异步及原生流式。远端/异步 subagent、未经中间件接入的编译图、可编辑 HITL/resume 仍不属于保证范围。

## 一个运行上下文

删除 `CallContext`、`RunBinding`、`_DELEGATION`、`delegation()` 和中间件 `_delegation()`。内部保留一个明确命名的对象：

```python
@dataclass(frozen=True)
class RunContext:
    owner: dict
    session_id: str
    user_prompt: str
    turn_id: str
    history: RunHistory
```

`owner` 是应用本轮传入的那份 moss_info 引用，用 `is` 校验归属，不复制或序列化；其余字段分别是会话、本轮用户输入、本轮 UUID 和历史操作对象。保留 gate_session_id 字符串属性，供构造请求使用。

保留键 `_agentmoss_run` 不变，只存放 RunContext。它与锁只在本轮 context 中存在，不进入 checkpoint、模型消息或 Gate payload。应用不设置这个键、不跨根调用复用 moss_info。冻结对象只防止字段重新赋值，history 本身仍可更新。

`begin_run() -> RunContext`：

1. 验证 moss_info、thread_id 和历史会话归属。
2. 若保留键已有 RunContext，验证 owner、session_id 及本轮用户输入未变，合并当前图状态并返回同一对象；不创建新 turn，不重复记录根用户输入。
3. 若没有对象，从图状态恢复历史，生成 turn_id，记录根用户输入后绑定到 moss_info。RunContext.user_prompt 在本轮内保留原文用于精确校验；写入历史和送检时对副本脱敏，不把原文额外持久化。
4. 保留键类型或归属不符时明确报错，不覆盖可疑值。

`current_run() -> RunContext` 替代 binding_for()：只校验并返回本轮对象，不再查询委派分支。工具、after_agent 和所有 subagent 都取得相同对象。同一会话跨轮通过 checkpointer 恢复字典；不同会话及不同轮次不共享运行对象或锁。

## 调用记录的唯一标识

不能只用原始 tool_call_id 去重，也不能每次进入包装器都随机生成一个新编号：前者可能把不同 subagent 的同名调用合并，后者会让重试变成新调用。

采用 SDK 执行位置加原始工具调用编号，确定性生成 `event_id`：

```text
event_id = UUIDv5(
    固定的插件命名空间,
    JSON(["sdk-v4", session_id, execution_info.checkpoint_ns, tool_call_id])
)
```

固定命名空间字面值在实施时选定并固定，不随进程、轮次、重启改变；JSON 必须使用固定序列化设置。SDK 的 checkpoint_ns 作为不透明执行标识使用，不解析其中的名称、不映射主agent/subagent、不用于权限规则。仅持久化生成的 event_id，保留原 tool_call_id 以关联 SDK 消息，不更改实际工具请求或 ToolMessage 的编号。

信息来自固定依赖版本公开的 `request.runtime.execution_info`。本地 SDK 源码已确认 ToolNode 同步、异步均向 ToolRuntime 转交 execution_info，LangGraph 为执行节点生成含任务标识的 checkpoint_ns；实施必须用实际 SDK 验证无 checkpointer、有 checkpointer、并行 subagent、fork 和执行重试，不能只测试伪造请求。

缺少 execution_info、可用 checkpoint_ns 或非空 tool_call_id 时，在送检和执行前返回接入错误，不退化到随机编号或单独 tool_call_id；这不是可按 fail_open 放行的 Gate 故障。若实际 SDK 验证不能成立，应先修订此设计，不引入新的业务身份追踪兜底。

历史 reducer 按 `(root, event_id)` 合并，同条事件用较新 seq 更新状态，并按 `(order, event_id)` 稳定排序。已记录的同一 event_id 再进入执行包装器时拒绝重放，包含 attempted/unknown；不假装成功，不自动再执行，不缓存或重传普通工具结果。保证只覆盖保留的历史窗口，不能当作无限期的 exactly-once 机制。

## 历史格式与旧状态

状态字段仍为 `agentgate_history`，格式升为 schema=4。顶层保留 `root/events/turns/truncated`；每条事件保留 event_id、tool_call_id、turn_id、原工具名、送检类型和详情、原参数副本、cwd、状态、判定来源、decision、seq 和 order。

删除事件中的 scope、parent_call_id 和重复的 root_session_id。会话归属由顶层 root 校验。送检 metadata 删除 scope、parent_call_id、agent_role、delegated_task；history_statuses/history_arguments 改用 event_id 关联，并保留 tool_call_id。

原有五种状态、无工具轮次、完整参数、200 条/20 轮/32 KiB 历史限制和 256 KiB 请求上限继续适用。历史整条裁减，当前参数不静默截断，脱敏不改变工具实际参数。

schema=3 的已完成历史通过纯函数转换为 schema=4：先完整验证旧格式和会话，再按 `UUIDv5(固定命名空间, JSON(["legacy-v3", root, old_scope, tool_call_id]))` 生成确定性 event_id，删除身份字段，保留状态、顺序、轮次和调用参数。相同旧记录重复迁移结果一致；归并前统一转换旧值和增量，不混用两种 schema。旧 scope 仅用于一次性生成编号，不继续存储或送检。

升级须在相关会话空闲、checkpointer 没有待执行任务时进行，由应用检查。v3 的 attempted/unknown 历史拒绝自动转换并给出明确错误，保留旧数据供人工核对，不猜测是否已执行。新格式不支持旧插件直接读取；回退使用升级前的 checkpoint 备份或新会话，不把新状态原地降级。v2 的诊断通道继续不作为完整调用历史导入。

## 执行链路

1. before_agent 初始化或取得同一 RunContext，并返回历史字典更新。
2. 工具包装器取得 RunContext，合并当前图状态，计算 event_id，检查重复；取此前历史快照，再登记 attempted。
3. 沿用 ActionAdapter 和 GateClient 发送当前动作、根用户输入与整份会话历史。当前调用不出现在自己的此前历史中。
4. 明确 Deny 时不调用 SDK handler。Allow 或现有 fail_open Bypass 时继续原 handler，结果只交回 SDK。
5. 按原逻辑更新 executed_success/executed_error/unknown，返回 Command 合并历史，保留原工具的应用状态更新和 graph/goto。
6. after_agent 合并共享对象与图状态；subagent 的收尾也只同步数据，不清空上下文、不另建一轮。根运行收尾后由官方状态和应用 checkpointer 保存。

具体工具仍串行执行“取快照—检查—执行—记录”，同批并行任务共享历史；不承诺按模型数组下标执行，而按实际获得锁的次序记录。task 仍送检，但不能持叶子锁等待 subagent，否则会死锁。取消后等待已开始工具收尾再释放锁的机制保留，不强行终止 Shell。

应用继续负责同一 thread_id 的根请求串行、输入白名单、流关闭及结束请求后关闭 GateRuntime。插件不区分主agent/subagent，不代表应用可以把外部字典或 Command 写入保留状态字段。

## 文件调整与返回类型

- context.py：合并上下文类，删除委派 ContextVar 和动态上下文管理器；提供 moss_info()、begin_run()、current_run()。
- history.py：实现 schema=4、event_id 去重和 v3 转换，保留 RunHistory 和 reducer；更新相应状态定义。
- middleware.py：用 run.history 取代 binding.view、用 run 的字段取代 binding.context；去掉身份 metadata 和委派包裹，使用原生 execution_info 计算事件标识。
- README.md：保留现有安装/接入方式，精简运行机制说明；不向用户增加事件编号配置。
- tests：按新格式更新现有测试，新增编号冲突、重复执行、旧状态迁移和上下文共享的回归。

新增和本次改动的方法写明参数/返回类型。生命周期钩子返回 dict | None；begin_run/current_run 返回 RunContext；历史生成/合并函数返回 dict；修改对象的 record_prompt/merge/finish 返回 None。构造函数 __init__ 返回 None，调用类的构造表达式返回实例。具体内部帮助函数签名由实施者确定，避免为了类型说明另建大量包装类。

## 验收

1. 原生 SDK 创建与调用方式、环境配置、backend 路径转换、Allow/Deny 和故障策略保持；明确 Deny 的物理目标不变。
2. 主agent、general-purpose、isolated、fork、嵌套/并行 task 都送检，共用 session、根用户输入和 turn；payload/新 checkpoint 中不再新增或保留插件的身份追踪字段。原始工具参数（包括 task 的 subagent_type/description）完整保留，不因删除身份 metadata 而删改。
3. 两个并行 subagent 使用相同原始 tool_call_id 时，保留两条事件；同一执行位置和调用编号重入时只执行一次，重复进入返回错误；attempted/unknown 不自动重放。
4. 同会话后续动作看到已完成的前序工具调用和状态；不同会话无串扰、无全局串行；task 等待不死锁。
5. 同步/异步及无/有 checkpointer 的实际 SDK execution_info 可用；缺失时阻止执行，不能偷偷换算法。
6. 多轮及无工具轮次、子历史回传、重复 reducer 更新、v3 确定性转换、跨会话拒绝、pending/unknown 升级拒绝、边界裁减均有验证。
7. 普通工具消息及 Command 的应用字段/路由保持，工具参数不被脱敏改写，无普通 output 上报。
8. 保留已有取消、真实短时 Shell、流提前关闭和随后同会话复用的验证。
9. 本机隔离 Gate/Moss 的接口验证：正常执行、未读先改拒绝、先读后改允许、subagent 拒绝、多轮历史及同名调用不丢失。当前阶段不发真实模型 API 请求；实施阶段若复用此前明确授权的真实模型联调，单独记录证据与使用情况。

实现方式、升级拒绝策略和验收细节由设计者提出，用户于 2026-11-02 审阅后确认继续。固定命名空间字面值等已明确留给实施者的细节在实施计划中落实。
