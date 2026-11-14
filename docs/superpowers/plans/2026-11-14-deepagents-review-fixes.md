# DeepAgents Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

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

- [x] Add graph regressions for recursive-delete Deny, blank read windows, and
  glob running/transport loss (`unknown`, no further submission).
- [x] Run new tests on baseline; require the three relevant defects to fail.
- [x] Implement shared command builder, read window metadata, operation context,
  and middleware classification; cover EOF/empty/zero and ordinary errors.
- [x] Run `python -m pytest -c integrations/deepagents-plugin/pyproject.toml integrations/deepagents-plugin/tests -q`.
  Expected: all offline tests pass; only unconfigured online/artifact cases skip.
- [x] Commit the tested task.

### Task 2: Bounded history and native approval recovery

**Files:** plugin `history.py`, new `history_schema.py`, `context.py`, `middleware.py`;
new `tests/test_history_limits.py`, `tests/test_resume.py`, history/context/session SDK tests; plugin README.

**Interfaces:** schema 5 preserves existing events and adds stable ordered turns,
limits, a monotonic clock, prune cursors, and current turn identity. `ensure_run`
creates the normal invocation or restores its existing turn from checkpoint state.

- [x] Add failing graph tests for a one-turn/one-action checkpoint across turns,
  and native approve/resume with fresh context.
- [x] Add merge tests for stale snapshot reintroduction, byte eviction, policy
  changes, and completed/unfinished v4 migration; verify failures.
- [x] Implement schema migration and one merge/prune policy, keeping execution-ID
  replay protection independent of retained events.
- [x] Implement normal/resumed run context initialization. Test approve/edit/reject,
  repeated checkpoints, child interruption, and no duplicate completed effects.
- [x] Update compatibility documentation and run the complete plugin suite.
  Expected: all offline tests pass without dropping cancellation/replay coverage.
- [x] Commit the tested task.

### Task 3: Compatibility, packaging, and independent review

**Files:** documentation and any regression-driven corrections only.

**Interfaces:** consume unchanged public integration APIs and new schema 5.

- [x] Run plugin suites in Python 3.11 minimum and Python 3.12 newer dependencies;
  run root `AGENT_MOSS_ROOT="$PWD" AGENT_MOSS_DISABLE_LLM=1 .venv/bin/python -m pytest -q`.
- [x] Run Ruff; build root/plugin wheel and sdist from the final tree; run artifact
  tests, installed plugin tests, and Python 3.10 base import checks.
- [x] Dispatch one clean-context independent reviewer over baseline..HEAD; verify
  any actionable finding with a failing test, fix it, and rerun affected suites.
- [x] Record final results and commit remaining tested corrections/documentation.


## Verification result (2026-11-14)

Implementation commits: `4e6be16`, `f562a21`, `3470172`.

| Check | Result |
| --- | --- |
| Plugin, Python 3.12 / DeepAgents 0.7.32 | 311 passed, 11 skipped |
| Plugin, Python 3.11 minimum dependencies | 311 passed, 11 skipped |
| Plugin, Python 3.12 / DeepAgents 0.7.37 | 311 passed, 11 skipped |
| Root project regression | 3164 passed, 140 skipped; 97 baseline deprecation warnings |
| Installed unified wheel, outside source checkout | 313 passed, 9 skipped |
| Wheel metadata, licensing, optional-SDK local mode | 4 passed |
| Root/plugin wheel and sdist | All four built; plugin modules match source bytes |
| Python 3.10 base Moss | Installed wheel engine import passed |
| Ruff and git diff checks | Passed |

The independent reviewer found a synchronous approval-resume initialization race.
Two deterministic thread-interleaving regressions failed before atomic context
initialization and passed afterward, together with the full plugin suite. No
confirmed Critical or Minor findings remain deferred. Real Gate/Moss/AIO services
were not configured; nine online plugin checks remain unexecuted.

### Implementation decisions and retained contracts

- Schema validation/migration is isolated in `history_schema.py`; public integration
  APIs retain their signatures. The checkpoint format intentionally advances to 5.
- Batched native HITL's persisted edited actions are checked before the SDK applies
  them. This dependency contract is tested on minimum/newer versions; future SDK
  upgrades must preserve it or update the adapter.
- A byte budget too small for session/pruning metadata now fails before execution;
  applications using such a budget must increase it.
- Applications supply a new `moss_info` dictionary for each invocation/resume as
  documented; reusing it can still fail after pruning.
- Cross-process side effects whose execution IDs have left the retained history
  still require application reconciliation; no exactly-once claim is added.
- Repair/validation work is committed on the existing PR branch locally. This
  task does not publish packages, merge the PR, or update the remote branch.
