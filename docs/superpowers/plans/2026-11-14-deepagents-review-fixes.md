# DeepAgents Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair and verify all five reproduced PR #8 findings.

**Architecture:** Keep Gate/backend public APIs. Share deletion semantics, preserve
per-tool uncertain outcomes, and supply correct read windows. Version bounded
history metadata and restore invocation context from trusted checkpoints.

**Tech Stack:** Python, DeepAgents, LangGraph, agent-sandbox, pytest, httpx.

**Spec:** `docs/superpowers/specs/2026-11-14-deepagents-review-fixes-design.md`

## Global Constraints

- Moss base installation supports Python >=3.10; plugin supports Python >=3.11.
- Preserve existing dependency ranges, AIO zero retries, and application-owned lifecycle.
- Preserve Gate Deny, bounded history, child sharing, and execution-ID replay prevention.
- Existing linked worktree and session model; implement inline, then one independent reviewer.

## Review Focus

- Quoted/unicode directory deletion and Gate Deny before side effects (Task 1).
- Blank pages, EOF, zero limits, and absent total-line metadata (Task 1).
- An uncertain operation followed by a rejected operation must not inherit unknown (Task 1).
- Stale parallel snapshots, byte pruning to zero events, and lower/increased limits (Task 2).
- Approval edits/rejection, child interruptions, repeated resumes, and old checkpoints (Task 2).

### Task 1: Correct file operations and outcome propagation

**Files:** `actions.py`, `aio_backend.py`, new `operation.py` in the plugin package;
`tests/test_review_fixes.py`, `tests/test_actions.py`.

**Interfaces:** `delete_command(path: str) -> str`; `tool_operation()` yields a
mutable per-call outcome with `uncertain: bool`; `mark_uncertain()` records only
the current operation. Middleware reads this after the SDK handler returns.

- [ ] Add graph regressions for recursive-delete Deny, blank read windows, and
  glob running/transport loss (`unknown`, no further submission).
- [ ] Run new tests on baseline; require the three relevant defects to fail.
- [ ] Implement shared command builder, read window metadata, operation context,
  and middleware classification; cover EOF/empty/zero and ordinary errors.
- [ ] Run `python -m pytest -c integrations/deepagents-plugin/pyproject.toml integrations/deepagents-plugin/tests -q`.
  Expected: all offline tests pass; only unconfigured online/artifact cases skip.
- [ ] Commit the tested task.

### Task 2: Bounded history and native approval recovery

**Files:** plugin `history.py`, `context.py`, `middleware.py`;
`tests/test_review_fixes.py`, history/context/session SDK tests; plugin README.

**Interfaces:** schema 5 preserves existing events and adds stable ordered turns,
limits, a monotonic clock, prune cursors, and current turn identity. `ensure_run`
creates the normal invocation or restores its existing turn from checkpoint state.

- [ ] Add failing graph tests for a one-turn/one-action checkpoint across turns,
  and native approve/resume with fresh context.
- [ ] Add merge tests for stale snapshot reintroduction, byte eviction, policy
  changes, and completed/unfinished v4 migration; verify failures.
- [ ] Implement schema migration and one merge/prune policy, keeping execution-ID
  replay protection independent of retained events.
- [ ] Implement normal/resumed run context initialization. Test approve/edit/reject,
  repeated checkpoints, child interruption, and no duplicate completed effects.
- [ ] Update compatibility documentation and run the complete plugin suite.
  Expected: all offline tests pass without dropping cancellation/replay coverage.
- [ ] Commit the tested task.

### Task 3: Compatibility, packaging, and independent review

**Files:** documentation and any regression-driven corrections only.

**Interfaces:** consume unchanged public integration APIs and new schema 5.

- [ ] Run plugin suites in Python 3.11 minimum and Python 3.12 newer dependencies;
  run root `AGENT_MOSS_ROOT="$PWD" AGENT_MOSS_DISABLE_LLM=1 .venv/bin/python -m pytest -q`.
- [ ] Run Ruff; build root/plugin wheel and sdist from the final tree; run artifact
  tests, installed plugin tests, and Python 3.10 base import checks.
- [ ] Dispatch one clean-context independent reviewer over baseline..HEAD; verify
  any actionable finding with a failing test, fix it, and rerun affected suites.
- [ ] Record final results and commit remaining tested corrections/documentation.
