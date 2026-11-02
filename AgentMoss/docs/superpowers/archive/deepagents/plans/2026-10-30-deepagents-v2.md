# DeepAgents v2 Implementation Plan

> 历史 v2 文档：用户已明确插件只做对接。最新方案为 [v3 对接方案](../../../specs/2026-10-30-deepagents-integration-only-design.md)，下文的本地文件证据/路径策略和 pwd-only 限制已在 v3 撤销。

> Use executing-plans inline, test-driven-development and one final fresh review.

**Goal:** Implement the approved session-state plugin and verify locally and in the existing VM.
**Spec:** ../specs/2026-10-29-deepagents-agentgate-agentmoss-design.md
**Architecture:** Bounded reducer-backed agent state; per-run shared view; exact local file guard before Gate; controlled root facade and managed streams.
**Tech Stack:** Existing pinned DeepAgents/LangGraph, Python, httpx. No new persistence.

## Global Constraints

- No plugin SQLite or output storage, no model/engine changes, one process/worker.
- UUID root identity; main and synchronous declarative children; exact path and file version checks.
- Unknown side effects denied, only exact `pwd` for execute; Gate Allow/fail-open cannot override local denial.
- Root input only user messages; external Command/config checkpoint injection rejected.
- Close graph streams and drain leaves before releasing root context/locks.
- Existing checkpointer owns persistence; no automatic pending side effect replay.

## Review Focus

- Command merges preserve SDK semantics and child state; no synthesized output evidence.
- Symlinks, hard links, unusual paths, changed files and Gate-time replacement cannot enlarge read authority.
- Parallel leaves see latest completed events; pruning bounds persisted state as well as requests.
- Double cancellation and SSE disconnect retain locks through physical completion.
- Migration preserves user model/settings/chat, has verified backup and rollback.

### Task 1: State and exact file guard
**Files:** history.py, guard.py, config.py; tests/test_state_v2.py, tests/test_guard_v2.py.
**Interfaces:** merge_history(old, delta), HistoryStore (in-memory per-run only); FileGuard.prepare/recheck/proof.
- [x] Write tests for bounded idempotent state, no output, exact three negative paths, fingerprint invalidation, unknown tools/Shell, new files.
- [x] Run targeted pytest: expected failures before implementation.
- [x] Implement state schema/validation/projection and conservative path guard; remove output_bytes.
- [x] Run targeted pytest: expected all pass; commit.

### Task 2: Controlled root and lifecycle
**Files:** entrypoint.py, runtime.py, context.py; tests/test_entrypoint_v2.py.
**Interfaces:** GuardedAgent.invoke/ainvoke and context-managed stream/astream; GateRuntime.load(config_path).
- [x] Write injection, per-root busy, managed stream close/double cancel/drain tests; run RED.
- [x] Implement facade validation, checkpoint validation/pending refusal, run views and shutdown.
- [x] Run targeted tests GREEN; commit.

### Task 3: Real graph and child integration
**Files:** middleware.py, assembly.py, actions.py; tests/test_graph_v2.py.
**Interfaces:** AgentMossState; Command state deltas merged with tool results; root facade wraps raw graph.
- [x] Write real graph read/edit, child/fork/parallel, checkpoint restart, errors and projection tests; run RED.
- [x] Implement local guard/Gate/recheck/tool completion pipeline and trusted built-in identity checks.
- [x] Run graph suite plus all applicable existing tests GREEN; commit.

### Task 4: Web migration and verification
**Files:** deploy scripts, README, VM Web wiring; tests/test_deploy_v2.py.
- [x] Write current-v1 wiring/migration compatibility tests; run RED.
- [x] Implement versioned backup/rollback, managed SSE, configuration migration, no plugin SQLite accesses.
- [x] Full local suite/lint then fresh review and TDD fixes.
- [x] Read SERVER, inspect actual VM, back up configuration/plugin/Web/checkpoint schema, update only DeepAgents service.
- [x] Run actual Gate/Moss/tool tests and SSE disconnect, check physical file effects and existing app persistence; record evidence/rollback.
- [x] Copy reviewed package to main requested directory; final requirement audit.

Completion evidence: /home/zhouq/code/deepagents-gate-integration-20261029/v2-evidence/REPORT.md. Local 116 pass; VM 107 pass/9 explicitly deselected; live model/Gate/Moss/checkpoint/rollback verified.
