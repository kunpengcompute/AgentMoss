# DeepAgents AgentGate Plugin Implementation Plan

> **2026-10-30 状态变更：本文件为 v1 历史计划。** v1 插件已实现并部署，本文中的 SQLite 账本、普通 output 上报和独立持久化防重放要求已被 v2 方案替代，不得据此继续实施。本次用户要求调整方案，v2 还未编码或部署。
>
> v2 的会话状态设计、实施顺序与验收要求见 [修订设计](../specs/2026-10-29-deepagents-agentgate-agentmoss-design.md)。用户已选择复用 DeepAgents 状态：不新增插件数据库；成功动作与可信读取证据分离，Gate 历史采用保守读取投影；受控根入口阻止外部状态注入；流关闭、叶子结束后才释放根运行锁。恢复沿用既有 checkpointer，并明确跨崩溃副作用空窗及完整组合链历史覆盖缩小。下文保留以解释 v1 来源，不代表当前待办。
>
> 本轮 P1 补充：插件本地按结构化路径精确匹配并验证读取证据，Gate Allow 不能覆盖；目标未知一律拒绝。execute 首版仅保留固定 pwd，不按下文 v1 的任意 Shell 执行能力实施。规范路径引用本身不能解决 Moss 的大小写/前缀/额外路径提取问题。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在当前 VM 中交付可供用户浏览器验证的 DeepAgents → AgentGate → AgentMoss 工具执行前检查。

**Architecture:** 私仓独立 Python 包，通过同步/异步 AgentMiddleware 和显式子代理组装接入。插件持久化真实完成历史；Web 应用只做配置、生命周期和根会话接线，不修改 SDK/Gate/Moss 核心。

**Tech Stack:** Python 3.12 来宾，DeepAgents 0.7.32、LangChain 1.4.7、langchain-core 1.7.1、LangGraph 1.2.18、langgraph-prebuilt 1.1.1、httpx 0.28.1、sqlite3、pytest、uv。

**Spec:** `docs/superpowers/specs/2026-10-29-deepagents-agentgate-agentmoss-design.md`

## Global Constraints

- 插件仅维护在 AgentMoss 私仓 `integrations/deepagents-plugin/`，导入名 `agentmoss_deepagents`。
- agent_type="deepagents"；Gate session_id=`deepagents:<浏览器UUID>`；主/子代理共用根 ID，以 scope/parent_call_id 区分。
- Deny 不调用 handler；Allow 不放宽现有权限；有效 Moss 故障 Deny 始终拦截。
- 默认 Gate URL http://127.0.0.1:9100、超时 5 秒、fail_closed=false；配置/映射/账本错误本地拒绝。
- 单次请求 256 KiB；历史 200 成功动作/20 提示轮次/32 KiB，单输出 2 KiB；不可截断待执行动作后放行。
- 当前单进程 LocalShellBackend，同根会话叶子工具串行；task 不持锁等待子代理。
- 不启用 L3、不更改模型、不调整宿主/QEMU 网络、不公开推送、不升级现有运行依赖。
- 修改部署文件前备份，记录恢复命令；上传使用 devbox `/home/zqb`。

## Review Focus

- 取消/崩溃时执行状态未知：不得记成成功或重放副作用（Task 2/3）。
- Shell 非零退出/被拒读取：不能为后续写入提供成功读取证据（Task 2/3）。
- 子代理线程切换、并发 task：不能丢根身份或共享错 parent ID（Task 4）。
- Unicode/空格/symlink/大内容：送检目标与实际操作一致，超限本地拒绝（Task 1/3）。
- Gate HTTP 200 但无有效 decision，以及 Gate 超时晚返回：只能记 bypass，不能伪称 Allow（Task 3/6）。

---

### Task 1: Configuration and action adaptation

**Files:** Create `integrations/deepagents-plugin/{pyproject.toml,config.example.json}`, `src/agentmoss_deepagents/{__init__.py,config.py,actions.py}`, `tests/{conftest.py,test_actions.py}`.

**Interfaces:** Produces `GateConfig.load(path: Path) -> GateConfig`; frozen config with specified defaults, `api_token_file: Path`, limits and enabled; `ActionAdapter(backend: LocalShellBackend).adapt(name: str, args: dict) -> Action` with tool_name, command, cwd, metadata.

- [ ] Write failing tests: `test_execute_preserves_shell`, `test_file_paths_follow_virtual_root`, `test_delete_is_analysis_only`, `test_glob_pattern_not_resolved_as_path`, `test_invalid_traversal_and_external_symlink_rejected`, `test_unicode_space_content_preserved`, `test_configuration_rejects_missing_credentials_and_bad_values`.
- [ ] Run `pytest tests/test_actions.py -q` in plugin project; expect missing plugin API failures.
- [ ] Implement config validation and all spec mappings; use SDK path validation/current backend resolution without changing actual requests; keep secret fields out of repr/output.
- [ ] Run the test command; expect all pass. Commit `feat: add DeepAgents gate configuration and action adapter`.

### Task 2: Durable context and successful history

**Files:** Create `src/agentmoss_deepagents/{context.py,history.py}`, `tests/test_history.py`.

**Interfaces:** Produces `root_context(session_id: str, prompt: str)` context manager, `current_context() -> CallContext`, `child_context(call_id: str, role: str, task: str)`; `HistoryStore(path: Path)` with begin/finish, prompt recording, bounded `snapshot(session_id: str, config: GateConfig) -> dict` and per-root lock; closes resources.

- [ ] Write tests for restart persistence, denied/error/unknown exclusion, duplicate IDs, old AI calls not trusted, limits/truncation, prompt grouping, root UUID validation and child context reset, locks isolated per root.
- [ ] Run `pytest tests/test_history.py -q`; expect absent APIs.
- [ ] Implement sqlite event journal (0600), prompt turns and stable scope/call keys; mark in-flight rows unknown at startup. Single threading lock per root acquired from async via cancellation-safe polling, with task excluded from holding leaf lock. No shared sqlite connection across threads.
- [ ] Run suite; expect all pass. Commit `feat: persist DeepAgents execution context and trusted history`.

### Task 3: Tool enforcement and client lifecycle

**Files:** Create `src/agentmoss_deepagents/{client.py,middleware.py,runtime.py}`, `tests/test_middleware.py`.

**Interfaces:** Produces `GateClient(config: GateConfig)` with analyze/aanalyze/close/aclose; `GateRuntime(config: GateConfig, history: HistoryStore, client: GateClient)` shared for process; `AgentMossGateMiddleware(runtime: GateRuntime, adapter: ActionAdapter, role: str)` overriding wrap_tool_call/awrap_tool_call.

- [ ] Write behavior tests with real ToolCallRequest and controlled httpx transport: Allow preserves ToolMessage/Command and one physical side effect; Deny zero side effects and matching ID; network/HTTP/JSON/unknown decision both fault modes; valid Deny never bypassed; request >256 KiB refuses locally; no root context refuses locally; journal failure refuses; successful/failed execute exit_code distinctions; cancellations release locks and leave unknown; same-root serialized and different roots concurrent.
- [ ] Run `pytest tests/test_middleware.py -q`; expect missing implementation.
- [ ] Implement payload with actual host/OS, namespaced session, original name and lineage metadata, successful history/turns and assistant reason. Full current action included unchanged, history bounded. No retries; default bypass emits warning and explicit journal source. Result success uses tool artifacts including execute exit_code; errors/unknown never enter trusted history. Preserve returned Command.
- [ ] Run all plugin tests; expect green. Commit `feat: enforce AgentMoss decisions before DeepAgents tools`.

### Task 4: Real graph and subagent coverage

**Files:** Create `src/agentmoss_deepagents/assembly.py`, `tests/{models.py,test_graph.py}`; export public APIs in `__init__.py`.

**Interfaces:** Produces `create_guarded_agent(*, runtime: GateRuntime, model, backend: LocalShellBackend, **kwargs)` preserving SDK kwargs and checking supported version; runtime.root_context compatible with Web session entry.

- [ ] Write deterministic fake-chat-model real graph tests for main and general-purpose tool Allow/Deny, explicit declarative/fork injection once, nested task lineage, two parallel roots, restart/checkpointer identity and unsupported compiled/async agents/backend rejection; use physical temporary files as evidence, no real model requests.
- [ ] Run `pytest tests/test_graph.py -q`; expect missing assembly API.
- [ ] Assemble explicit general-purpose spec from pinned SDK defaults, append unique middleware on main/declarative subagents, reject unsupported scopes; task wrapper binds child context across actual handler, root context remains stable. Validate pinned installed versions without upgrading dependencies.
- [ ] Run full plugin suite; expect green. Commit `feat: cover DeepAgents main and delegated tool execution`.

### Task 5: Web wiring and reproducible deployment

**Files:** Create plugin `deploy/{wire_web.py,install_guest.sh,rollback.sh}`, `tests/test_web_wiring.py`, `README.md`; modify deployed Web app and project/lock only after backup; add local task `code/deepagents-gate-integration-20261029/` outside repo for evidence and backups.

**Interfaces:** `wire_web.py INPUT OUTPUT` applies one recognized prior Web revision or exits without overwriting; lifespan owns runtime and root_context encloses agent stream; /api/health includes safe plugin status. Install records file hashes, original unit/config/lock, independent deepagent-owned token copy and plugin config; rollback restores actual saved files and locked environment.

- [ ] Write test that runs wiring against original Web app and executes health/lifespan/stream setup with controlled model transport; unsafe unknown revision refuses; existing browser authentication remains effective.
- [ ] Run `pytest tests/test_web_wiring.py -q`; expect absent wiring artifact.
- [ ] Implement wiring/install/rollback; package install using uv, retain current dependency pins. Add plugin docs for install, identity mapping, limitations, commands and user examples.
- [ ] Run full suite and shell syntax checks; expect green. Capture current remote health, hashes, dependency versions, Gate cache and service state, then back up and deploy only deepagents-web changes. Commit `feat: wire and deploy guarded DeepAgents web service`.

### Task 6: Live acceptance and final review

**Files:** Create `deploy/verify_vm.py`, `tests/test_live_contract.py`; local evidence/README, update local SERVER.md and existing deployment README after backup.

**Interfaces:** verifier uses current web/Gate bearer tokens read from files, emits only scrubbed JSON/SSE evidence and explicit assertions, requires no model credentials on command line.

- [ ] Write contract tests for verifier's physical-effect and ID correspondence checks against controlled Gate/Web endpoints; expect fail before verifier exists.
- [ ] Implement verifier and run full plugin tests plus compile/shell checks; expect green. Verify actual VM real Gate/Moss L1/L2, successful temporary file read/write, safe denied marker operation, child tool denial, Gate DB session/agent mapping, retained IDs after service restart and fault injection using isolated endpoints. No destructive commands reach real tools.
- [ ] Verify one browser real-model safe task through existing Web service without changing its configured model; confirm Windows HTTP/browser entry and three services active. Record any engine limitations separately.
- [ ] Commit implementation/evidence-safe docs, dispatch one fresh whole-branch reviewer with spec/plan/ledger. Fix important findings through RED→GREEN and full suite, record rulings and any deferred minors.
- [ ] Finish branch by retaining isolated worktree/commits for review (no public push or automatic shared-branch merge); record exact user browser entry and test prompts, backups and rollback. Complete goal only after current-state audit proves deployment and all spec invariants.

## Execution decision

用户已明确授权按当前设计实现并部署供其验证，因此执行无需再次请求计划确认。采用 executing-plans 原生执行，最后一次独立代码审阅；子代理模型继承当前会话，不按技能建议换模型。使用 using-git-worktrees 隔离修改，所有授权内的可逆准备/测试/部署持续执行。
