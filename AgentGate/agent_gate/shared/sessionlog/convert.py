"""Convert AgentGate NormalizedEvents into SessionEvents (default-on hook).

`from_normalized()` maps one existing collector event (source_collector:
llm_api_proxy | mcp | pty | ebpf) into one or more typed SessionEvents with
AgentGate semantics — so the sessionlog collects WITHOUT changing any
collector. `SessionLogRecorder` wraps a SessionLog + JSONL persistence and
is the default-on write hook used by the coordinator.

Mapping (effective information only — raw payloads stay attached, nothing
else is invented):
- llm_api_proxy: user/prompt + tool/call + tool/result (source=llm)
- mcp:          tool/call + tool/result (source=mcp)
- pty:          process/exec + process/result
- ebpf:         syscall/event
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from .events import (
    SessionEvent,
    process_exec,
    process_result,
    syscall_event,
    tool_call,
    tool_result,
    user_prompt,
)
from .log import SessionLog
from .persistence import save_append

logger = logging.getLogger(__name__)

DEFAULT_LOG_DIR = os.path.join(os.path.expanduser("~"), ".agentgate", "sessionlogs")


def _now_ms() -> int:
    return int(time.time() * 1000)


def from_normalized(
    event: dict,
    *,
    session_id: str = "",
    turn: int = 0,
    step: int = 0,
    time_ms: Optional[int] = None,
) -> list[SessionEvent]:
    """Convert one NormalizedEvent into SessionEvents (empty list if unknown).

    Preserves the collector's own session id when the event carries one.
    """
    collector = str(event.get("source_collector", ""))
    sid = session_id or str(event.get("session_id", ""))
    ts = time_ms or _now_ms()
    action = event.get("action", {}) or {}
    tool_name = str(action.get("tool_name", ""))
    call_id = str(action.get("tool_call_id", ""))
    arguments_raw = str(action.get("arguments_raw", "{}"))
    payload = event.get("payload", {}) or {}

    if collector == "llm_api_proxy":
        out: list[SessionEvent] = []
        user_text = payload.get("user_prompt", "")
        if user_text:
            out.append(user_prompt(len(out), str(user_text), turn=turn, time_ms=ts, source="llm"))
        if tool_name:
            out.append(tool_call(len(out), tool_name, arguments_raw, call_id,
                                 turn=turn, step=step, time_ms=ts, source="llm"))
        response = payload.get("tool_response_content")
        if response is not None:
            out.append(tool_result(len(out), call_id, response,
                                   turn=turn, step=step, time_ms=ts, source="llm"))
        return out

    if collector == "mcp":
        out = []
        if tool_name:
            out.append(tool_call(len(out), tool_name, arguments_raw, call_id,
                                 turn=turn, step=step, time_ms=ts, source="mcp"))
        raw_resp = payload.get("mcp_response") or event.get("response")
        if raw_resp is not None:
            out.append(tool_result(len(out), call_id, raw_resp,
                                   turn=turn, step=step, time_ms=ts, source="mcp"))
        return out

    if collector == "pty":
        command = str(action.get("command", "") or event.get("command", ""))
        pid = event.get("process", {}).get("pid") if isinstance(event.get("process"), dict) else None
        if command:
            return [process_exec(0, command, pid=pid, cwd=str(event.get("cwd", "")),
                                 turn=turn, time_ms=ts, source="pty")]
        return []

    if collector == "ebpf":
        syscall = str(action.get("syscall", "") or action.get("syscall_type", "") or tool_name)
        pid = event.get("process", {}).get("pid") if isinstance(event.get("process"), dict) else None
        if syscall:
            return [syscall_event(0, syscall, pid=pid, time_ms=ts, source="ebpf")]
        return []

    logger.debug("sessionlog: no mapping for collector=%s", collector)
    return []


class SessionLogRecorder:
    """Default-on recorder: appends normalized events to a per-session log.

    One SessionLog per session_id; persisted as JSONL under
    `~/.agentgate/sessionlogs/<session_id>.jsonl` (override with log_dir).
    Safe on the hot path: conversion + in-memory append are cheap, disk
    write is fire-and-forget (errors logged, never raised).
    """

    def __init__(self, log_dir: str = DEFAULT_LOG_DIR, enabled: bool = True):
        self.log_dir = log_dir
        self.enabled = enabled
        self._logs: dict[str, SessionLog] = {}
        self._turns: dict[str, int] = {}
        self._steps: dict[str, int] = {}

    def _log_for(self, session_id: str) -> SessionLog:
        if session_id not in self._logs:
            self._logs[session_id] = SessionLog(session_id=session_id)
            self._turns[session_id] = 0
            self._steps[session_id] = 0
        return self._logs[session_id]

    def record(self, event: dict, session_id: str = "") -> None:
        """Record one NormalizedEvent into its session log (default-on)."""
        if not self.enabled:
            return
        sid = session_id or str(event.get("session_id", ""))
        if not sid:
            return
        try:
            log = self._log_for(sid)
            turn = self._turns[sid]
            step = self._steps[sid]
            events = from_normalized(event, session_id=sid, turn=turn, step=step)
            if not events:
                return
            # Advance turn per user prompt; step per tool call.
            for e in events:
                log.append(e)
            if any(e.type.value == "user/prompt" for e in events):
                self._turns[sid] += 1
            if any(e.type.value == "tool/call" for e in events):
                self._steps[sid] += 1
            path = os.path.join(self.log_dir, f"{sid}.jsonl")
            save_append(log, path)
        except Exception as e:  # never break the collector hot path
            logger.warning("sessionlog record failed: %s", e)

    def session_log(self, session_id: str) -> Optional[SessionLog]:
        return self._logs.get(session_id)

    def persist_all(self) -> None:
        for sid, log in self._logs.items():
            save_append(log, os.path.join(self.log_dir, f"{sid}.jsonl"))


# Module-level default recorder (importable by the coordinator).
default_recorder = SessionLogRecorder()
