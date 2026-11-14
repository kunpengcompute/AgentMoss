# DeepAgents PR #8 review fixes

The user approved repairing and verifying the five reproduced PR #8 findings on
2026-11-14. The reviewed baseline is `2e2d46fbf338dd6e93348f8ceb67d3e638b19c61`.

## Requirements

1. A delete action sent to Gate must express the recursive deletion the backend
   performs. Share the AIO command builder, preserve shell quoting, and verify a
   recursive-delete Deny prevents all backend effects.
2. Custom history limits apply to both runtime and checkpoint state. Keep stable
   turn ordering and pruning boundaries, so merging stale snapshots cannot
   restore discarded data or discard a newer turn. Preserve the independent
   per-invocation execution-ID set after pruning.
3. Native DeepAgents approval/resume with a fresh `moss_info` reconstructs the
   current turn from trusted checkpoint state. Approved/edited arguments still
   pass Gate; rejected approvals never execute. Completed executions must not
   replay. Do not add a cross-process exactly-once guarantee.
4. AIO text reads return honest source-line windows, including blank pages.
   Distinguish a truly empty file, an empty page past EOF, and a zero-line request.
   Do not invent a total line count from a paginated SDK response.
5. Preserve an operation's uncertain outcome through SDK ToolMessage wrapping.
   Use a per-tool context marker shared with worker threads, not a global backend
   state or parsing an error message. Ordinary confirmed errors stay distinct.

## Approach

Keep the existing Gate and backend interfaces. Use one small SDK-independent
operation context for uncertainty propagation and a shared delete command helper.
Upgrade history to schema 5 with policy limits, monotonic ordering, prune cursors,
and the current turn identity. Centralize history merge/prune and run restoration.
Migrate completed schema 3/4 histories; refuse unfinished legacy histories.

Changing documentation alone would preserve the reproduced defects. Replacing
the history reducer with last-writer-wins snapshots would lose concurrent child
updates, so retain event-ID merging with explicit ordering and prune boundaries.

## Constraints and validation

- Moss base installation supports Python >=3.10; plugin supports Python >=3.11.
- Preserve the existing dependency ranges and zero-retry AIO transport.
- Preserve explicit child middleware setup, original arguments, Gate failure
  policy, session serialization, and cancellation/close behavior.
- Work in the existing linked PR worktree. No remote publishing is required by
  this repair-and-verification request.
- First reproduce each failure in regression tests using real DeepAgents graphs;
  replace only external Gate/AIO services with controlled transports/fixtures.
- Run the plugin suite on minimum and newer compatible dependencies, the root
  suite, Ruff, and wheel/sdist and installed-package checks.
- Real Gate/Moss/AIO service validation remains conditional on service availability.
