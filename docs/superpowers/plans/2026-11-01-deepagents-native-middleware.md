# DeepAgents 原生 Middleware Implementation Plan

> **For agentic workers:** 使用 superpowers:executing-plans，在当前 worktree 逐项实施；用户已授权设计、开发和接口验证，按既定 SDK/moss_info 方向连续执行。

**Goal:** 使用官方图工厂与调用接口对接 Gate/Moss，保留工具检查和主子上下文。
**Architecture:** AgentMossMiddleware 使用 before/after agent 和工具 wrap hooks；moss_info 绑定本轮共享历史，官方状态/checkpointer 持久化。GateRuntime 只负责配置/HTTP，应用管理根请求和流关闭。
**Tech Stack:** Python >=3.12，deepagents 0.7.32、langchain 1.4.7、langgraph 1.2.18、httpx 0.28.1。
**Spec:** ../specs/2026-11-01-deepagents-native-middleware-design.md

## Global Constraints

- 仅修改新仓库的插件和相应文档；无插件数据库，无引擎补偿，不改 Gate/Moss 源码。
- 原生 create_deep_agent、invoke/ainvoke/stream/astream；字段名 moss_info。
- 不调用 SDK 私有函数，不拷贝默认子代理栈；主、general-purpose 和用户子代理通过官方配置挂中间件。
- 保留 schema=3 调用历史含义、完整当前动作、200条/20轮/32KiB历史、256KiB请求上限、fail_closed默认false。
- 接入应用提供可信输入与context，同会话根请求串行，流显式关闭；未知执行不盲目重放。

## Review Focus

- context 是每轮新建可变 moss_info；子代理不得重建根视图。
- 并行 task 不能持叶子锁等子代理，完成动作不能因旧分支快照而丢失。
- Command update=None、dict和tuple序列及PARENT路由均保留。
- 取消时已开始的线程/实际Shell可能继续，应用的根锁必须等cleanup完成。
- 路径转换不用backend私有方法，实际SDK仍负责其路径/权限限制。

## Task 1: 原生接入与基础判定

**Files:** src/agentmoss_deepagents/{context,runtime,middleware,__init__}.py；tests/test_native_sdk.py。
**Interfaces:** AgentMossMiddleware(runtime,backend)；原生 context={"moss_info":{"session_id":...,"user_prompt":...}}。
- [x] 先写原生 create_deep_agent Allow/Deny、缺失/冲突context和无工具轮次测试，运行见到旧版接口缺失失败。
- [x] before_agent 建立本轮视图并记录prompt；官方hooks分析/执行/拒绝；runtime移除根运行管理。
- [x] 运行基础测试和配置回归，提交。

## Task 2: 主子历史与公开接口

**Files:** history.py、actions.py、middleware.py、context.py；tests/test_native_sdk.py、test_actions.py、test_context_v3.py。
**Interfaces:** state.agentgate_history/schema3；task动态lineage；只用公开Command属性。
- [x] 增加 isolated/general-purpose/fork/parallel/checkpoint/旧v3数据、参数保留、幂等和应用Command测试并先见失败。
- [x] 实现共享视图与叶子串行、task上下文、脱敏、有界历史；路径只按公开语义映射。
- [x] 迁移原受支持行为测试；删除替代工厂、运行入口和只验证这些旧职责的测试；保留配置和兼容性覆盖。
- [x] 完整pytest与Ruff；提交。

## Task 3: 应用契约和取消

**Files:** tests/test_native_lifecycle.py、middleware.py；测试辅助只在tests中。
**Interfaces:** 原生流、contextlib.closing/aclosing；应用会话锁在finally收尾后释放。
- [x] 先写同步/异步/原生流、真实短时Shell取消、同会话复用、异常和网关故障、应用输入白名单与HTTP会话隔离测试。
- [x] 验证实际handler退出后才放叶子锁，测试应用负责根运行管理，不增加产品图包装类。
- [x] 全套回归、格式及diff检查，提交。

## Task 4: 文档与真实接口验收

**Files:** 插件README、pyproject.toml；临时接口验收与证据放工作区之外。
**Interfaces:** 0.4.0公开API、uv安装示例、显式子代理配置、context_schema示例、原生流关闭与服务生命周期。
- [x] 重写对接说明，说明迁移方式和应用责任；不新增deploy目录。
- [x] 通过隔离本机Gate/Moss与原生SDK实际工具验证Allow/Deny、先读后写、普通Shell、主子身份、历史与记录查询；不调用真实模型API。
- [x] 独立最终评审；修复重要问题并补失败回归；完整测试后同步新主目录，核对备份SHA并记录恢复方法。
