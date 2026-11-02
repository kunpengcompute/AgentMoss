# DeepAgents 会话历史简化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 合并 CallContext/RunBinding，取消主agent/subagent 身份追踪，保留按 session_id 共享的工具历史和 Gate 执行前检查。

**Architecture:** 本轮 moss_info 中只绑定一个 RunContext，内含共享 RunHistory；官方 reducer/checkpointer 保存历史字典。通过 SDK execution_info.checkpoint_ns 与原 tool_call_id 确定性生成 event_id；历史升为 schema=4，转换可确认已完成的 v3 记录。

**Tech Stack:** Python 3.12+；deepagents 0.7.32、langchain 1.4.7、langchain-core 1.7.1、langgraph 1.2.18、langgraph-prebuilt 1.1.1、httpx 0.28.1；pytest、ruff。

**Spec:** [2026-11-02-deepagents-session-history-design.md](../specs/2026-11-02-deepagents-session-history-design.md)

## Global Constraints

- 用户交付目录：`/home/zhouq/code/boostkit/AgentMoss/AgentMoss/integrations/deepagents-plugin`；旧原始仓库、VM、devbox 不改。
- 保留 GateRuntime、环境/应用配置、官方 create_deep_agent 和 invoke/ainvoke/stream/astream、各 subagent 显式安装中间件的接入方式。
- 不修改 Gate/Moss 引擎，不新增数据库、全局会话表、根入口或普通工具 output 上报。
- 同会话根请求串行、外部输入白名单、流关闭由应用负责；具体工具继续加叶子锁，task 不持锁等待 subagent。
- 保留五种执行状态、200 条调用/20 轮/32 KiB 历史上限、256 KiB 请求上限、凭据脱敏和原参数执行。
- 缺少 SDK 执行标识按接入错误拒绝执行，不能按 Gate fail_open 放行；已记录调用不自动重放。
- 本轮实施先使用固定测试模型和真实 Gate/Moss；不默认再次调用收费模型 API。
- 不修改用户 Git 作者配置、不借用其他人的身份提交。当前根仓库未配置作者且组件未跟踪：保留文件备份与 SHA 清单；提交条件满足时只提交本次明确文件。

## Review Focus

1. 两个并行 subagent 的原 tool_call_id 相同，仍须保留两条调用，见 Task 1/2。
2. 实际 SDK 节点重试的 execution_info 稳定；同一工具已执行后重入不能再执行，见 Task 1/2。
3. 子图返回旧快照或 reducer 重复收到更新，不能把最终状态回退到 attempted，见 Task 2。
4. 旧状态迁移必须校验整份数据，遇到 unknown/attempted 明确拒绝且不修改原数据，见 Task 1/3。
5. 一个 subagent 提前结束不能清理其他调用使用的共享对象；嵌套 task、异常和取消仍能收尾，见 Task 2/3。

## 执行准备

- [x] 记录当前插件文件及 SHA，备份整个插件与本次 spec/plan 到仓库外的任务目录；保护用户后续编辑，交付前再次对比原 SHA。
- [x] 确定实施工作目录。推荐在当前会话直接实施并做一次独立最终评审；若使用工作副本，必须从当前用户目录复制最新插件，不拿旧 worktree 的 README/源码覆盖用户文件。
- [x] 运行当前插件完整测试作为基线：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:/home/zhouq/code/boostkit/AgentMoss/AgentMoss /home/zhouq/code/AgentMoss/.worktrees/deepagents-plugin/.venv/bin/python -m pytest -q -p no:cacheprovider --tb=short`。预期当前 103 项通过；已有 LangSmith/Python 3.14 弃用警告单独记录。

以下源码/测试路径均相对插件目录；使用上述解释器借用依赖，不在旧仓库内安装或修改环境。测试先 RED 后实现；每个任务结束运行完整插件测试，不以单个新用例通过替代全套。

## Task 1：验证 SDK 编号并实现确定性编号、旧历史转换

**Files:** 修改 `src/agentmoss_deepagents/history.py`；新增 `tests/test_session_history.py`。

**Interfaces:**
- `make_event_id(session_id: str, checkpoint_ns: str, tool_call_id: str) -> str`。
- `migrate_history(value: dict, root: str) -> dict`：纯函数，v3 转 v4、v4 校验后返回；空值生成 v4 空历史；跨会话/错误格式/未完成 v3 历史抛 ValueError。
- 固定 UUID 命名空间：`b4ae6ea9-52c4-5b0d-84e8-9ad8a250a8bd`。序列化使用 `json.dumps(parts, ensure_ascii=False, separators=(",", ":"))`；新/旧编号域分别为 `sdk-v4`、`legacy-v3`，精确遵循 spec 的组成字段。

- [x] 新增编号与迁移测试并运行 RED：同一输入编号一致；任一组成项改变则编号不同；空编号拒绝；两个旧 scope 中相同 tool_call_id 转成不同 event_id；多次转换幂等；原字典不被修改；unknown/attempted、错误 root、错误字段或字段类型拒绝。
- [x] 增加实际 SDK 特征验证：通过公开 Middleware 钩子采集 execution_info，覆盖同步/异步、无/有 InMemorySaver、并行 subagent、fork；两个 subagent 用相同 tool_call_id，checkpoint_ns 不同。只采集必要标识，不输出凭据和原始用户内容。
- [x] 用公开 StateGraph/ToolNode/RetryPolicy 验证一次节点异常后的重试，断言同一工具执行位置的 checkpoint_ns 及计算出的 event_id 不变。不得通过改 SDK 私有字段制造通过结果。
- [x] 实现上述两个纯函数及必要的旧格式校验，暂不切换现有执行链路。旧格式先校验完整事件/轮次，再转换；不要用“删除未知字段”掩盖损坏数据。
- [x] 运行新用例及完整测试，确认 GREEN。SDK 标识验证若不成立，停止切换链路并报告具体条件，不能改用随机 ID 兜底。

## Task 2：一次切换上下文、历史和工具中间件

这些接口相互依赖，作为一个可独立验收的改动，避免临时兼容层留在成品。

**Files:** 修改 `context.py`、`history.py`、`middleware.py`；更新 `tests/test_context_v3.py`、`test_integration_v3.py`、`test_native_sdk.py`、`test_native_lifecycle.py`、`test_review_regressions.py`；补充 `tests/test_session_history.py`。

**Interfaces:**
- `RunContext(owner: dict, session_id: str, user_prompt: str, turn_id: str, history: RunHistory)`，只存本轮 context，保留 `gate_session_id -> str` 属性。
- `moss_info(context: object, thread_id: str | None = None) -> dict`；`begin_run(runtime_context: object, state: dict, gate_runtime: GateRuntime) -> RunContext`；`current_run(context: object, thread_id: str | None = None) -> RunContext`。
- `RunHistory.begin(run: RunContext, call_id: str, action: Action, original_name: str, *, event_id: str) -> dict`；`record_prompt(turn_id: str, user_prompt: str) -> None`；`finish(event: dict, status: str, *, decision: str = "", source: str = "") -> None`。历史模块仅在 TYPE_CHECKING 中导入 RunContext，避免与 context.py 的运行时循环导入。
- `event_key(event: dict) -> str` 返回 event_id；root 已由 reducer/RunHistory 校验。`empty_history(root: str = "") -> dict` 生成 v4；`validate_history(value: dict, root: str) -> dict` 使用 Task 1 的统一入口。
- `merge_history(old: dict, delta: dict) -> dict` 转换后按 event_id、较新 seq 合并，按 `(order, event_id)` 排序。保留 `AgentMossState.agentgate_history` reducer。

- [x] 新增行为测试并运行 RED：一轮多个 before_agent 返回同一 RunContext/RunHistory/turn_id；只记录一次根用户输入；新的 moss_info 恢复历史但新建轮次；跨 owner、会话、错误保留键或用户输入变化拒绝。
- [x] 为原始用户输入未变校验固定实现选择：RunContext.user_prompt 保存应用提供的原文；begin_run 调用 `history.record_prompt(run.turn_id, gate_runtime.redact(run.user_prompt))`，payload 构造时也调用现有脱敏函数。原文仅在已有应用 context 的本轮对象中存在，不进持久历史或送检副本。这样可直接比较原文，不新增摘要字段。RunHistory.begin 不再重复调用 record_prompt；本轮输入由初始化统一记录。
- [x] 新增 SDK 执行测试并运行 RED：并行 subagent 使用同一原工具编号仍执行两次且留下不同 event_id；同一 request 重入以及同一 SDK 节点重试时物理计数只增一次；缺 execution_info/ns/call_id 时 handler 和 Gate 均不被调用。
- [x] 实现 RunContext 和共享对象初始化/取得，删除旧类、delegation、ContextVar 和分支查找；按 Task 1 的标识切换 v4 ledger 与中间件，直接调用 handler，保留取消 drain 和 task 锁豁免。
- [x] history_statuses/history_arguments 通过 event_id 关联；删除插件 identity metadata。task 原始 subagent_type/description 等工具参数继续完整送检。
- [x] 调整原有用例到新格式与类名，保留原本允许/拒绝、状态更新、真实 SDK 文件路径和 Command 应用字段/路由断言，不因删除身份断言而删除功能验证。
- [x] 补充旧快照合并、重复 reducer 更新、并行 task 共享历史、嵌套 task、不清理共享对象和多会话测试，运行完整测试至 GREEN。

## Task 3：升级、生命周期、真实接口与用户文档

**Files:** 更新 `README.md`、`pyproject.toml`、`src/agentmoss_deepagents/__init__.py`；补充 `tests/test_session_history.py` 及已有生命周期测试。接口验证脚本、日志、服务数据保存到仓库外任务目录，不恢复 deploy/。

**Interfaces:** 保留全部公开接入 API；插件版本升至 0.5.0，说明 v4 checkpoint 不可交给旧插件读取。

- [x] 增加跨轮 checkpoint 测试：已有 v3 终态历史进入新图，下一次送检保留原调用/状态/轮次，落入 v4；再调用和重复合并不会重复迁移或丢记录。v3 unknown/attempted 在工具执行前报错；不编造执行结果。
- [x] 跑现有取消、短时真实 Shell、原生流提前关闭和同会话下一请求的用例；必要时只更新新状态断言，保留物理执行和收尾验证。
- [x] 保留并验证历史的 200/20/32768 上限、请求 262144 上限、当前调用不自包含、工具原参数不变和普通 output 不入历史。
- [x] README 用简短文字说明共享历史、新旧状态升级与回退：升级前应用确认 checkpoint 无 pending tasks 并备份；旧 unknown/attempted 需人工核对；回退使用升级前备份或新会话。保持 middleware 变量名和主agent/subagent 用词，不增加教程式长章节。
- [x] 真实 HTTP 联调：单独启动本机 Gate/Moss L1/L2，用独立端口、凭据和数据目录，通过官方 SDK 固定测试模型触发正常文件/Shell、未读先改、先读后改、subagent 拒绝及同名并行调用。核对真实 Gate API/数据库记录与物理目标；不操作用户现有服务。
- [x] 停止自建进程并验证端口释放，保留脱敏报告和原始断言结果；运行完整测试、ruff check、ruff format --check、README Python 示例解析和变更检查。

## Task 4：独立评审和交付

**Files:** 本次所有明确变更、仓库外验收报告；不包含组件的其他未跟踪文件。

- [x] 请一位新审阅者检查完整最终实现与测试，重点检查 spec 的九项验收、重复执行/迁移、并行历史和生命周期；继承当前会话模型，不切换模型或另发模型 API。
- [x] 对实际问题先补可复现的失败用例再修复，运行相应回归；修复完成后运行最终全套检查，不把评审意见本身当作验证证据。
- [x] 若在工作副本实施，对照最初 SHA 将本次文件同步回用户目录；用户期间修改的文件逐一合并，不覆盖；再从用户目录运行最终验收。
- [x] 更新 spec/plan 状态和任务清单，报告实际测试数、HTTP 验证结果、限制、备份位置和恢复方式。Git 无作者身份时明确说明未提交，不因此阻塞已授权的文件交付。

## 计划自检

本计划覆盖 spec 的上下文、编号、迁移、SDK 接入、并行、取消、真实接口及用户文档。Task 1/2 的输入输出签名一致；旧状态转换先于 reducer 合并。五项 Review Focus 均已有对应测试任务。SDK 未验证行为不作兼容承诺；应用 pending-checkpoint 检查不伪装成插件已实现的能力。

## 实施结果

2026-11-02 已实现并交付插件 0.5.0。用户目录 148 项测试通过，Ruff/示例/链接检查通过；最终 wheel 本机 API 验证 15 次工具送检，12 Allow、3 Deny，全部由 agent_moss 返回。独立评审无待修复问题。执行证据与恢复方式见 `/home/zhouq/code/deepagents-session-history-20261102-p9wgll7o/REPORT.md`。Git 作者未配置，保留快照而未提交；没有修改用户 Git 配置。
