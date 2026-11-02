# DeepAgents 对接插件 v3 实施计划

> Use executing-plans inline with TDD and final review.

**Goal:** Remove engine policy compensation, keep accurate Gate integration and verify deployment.
**Spec:** ../specs/2026-10-30-deepagents-integration-only-design.md
**Architecture:** New bounded agentgate_history context, existing root/child middleware transport and lifecycle; no local file authorization.

## Global Constraints

No SQLite/history service; no Gate/Moss/model changes. Existing SDK pins, LocalShellBackend and synchronous declarative delegation. Current tool arguments stay intact; Gate owns security verdict. Preserve existing app permissions, root identity and stream cleanup. Full v2/v3 roundtrip must preserve chats.

## Review Focus

- Full command history, no read-only projection or hidden local policy remains.
- Failed/denied/parallel requests are identified as tool calls, not execution evidence.
- Commands are not silently clipped; budget drops whole old events.
- New state key keeps existing checkpoint and rollback compatible.
- Custom tools, native permissions, cancellation and routing remain intact.

### Task 1: Context and middleware
- [x] Write failing integration-only/history tests; run RED.
- [x] Remove guard and proof generation, use bounded full tool call context with explicit semantics; preserve control/transport code.
- [x] Run targeted tests GREEN, commit.

### Task 2: Regression, documentation and migration
- [x] Replace obsolete policy tests with direct Gate delegation assertions; retain API/identity/child/lifecycle regressions.
- [x] Test v2/v3 namespace roundtrip, sync, stream, native permissions and internal Command.
- [x] Update README, version 0.3.0, v2-aware installer/rollback/verifier; run full suite/lint/shell checks.
- [x] Fresh whole-branch review, TDD-fix material findings; commit.

### Task 3: Deploy and verify
- [x] Inspect current VM and hashes; back up current files/data before upgrade.
- [x] Upgrade only plugin/Web, verify real Gate/Moss/model browser and checkpoint history.
- [x] Actual v2 rollback then v3 reinstallation preserving conversations, model and other dependency versions.
- [x] Sync source to main directory, compare hashes, update SERVER and report, audit requirements.

Completed evidence: /home/zhouq/code/deepagents-gate-integration-20261029/v3-evidence/REPORT.md. Local and VM96 pass each; real Gate/Moss/model/checkpoint/rollback verified.
