"""Session log — event-sourcing interaction log (inspired by dsh session-log).

A typed append-only log of `SessionEvent`s with contiguous seq. Event
vocabulary uses AgentGate collector semantics (llm / tool / process / mcp /
syscall) instead of dsh's turn/step wording; message history is DERIVED
from the log; JSONL persistence is lossless. The trajectory module assembles
the log into a user-facing data-flow record table for the dashboard.
"""

from .events import (
    SessionEvent,
    SessionEventType,
    llm_chunk,
    llm_request,
    llm_response,
    process_exec,
    process_result,
    session_end,
    session_start,
    syscall_event,
    system_event,
    tool_call,
    tool_result,
    user_prompt,
)
from .log import SessionLog
from .persistence import load, save_append, save_snapshot
from .trajectory import build_trajectory, summarize

__all__ = [
    "SessionEvent",
    "SessionEventType",
    "SessionLog",
    "build_trajectory",
    "llm_chunk",
    "llm_request",
    "llm_response",
    "load",
    "process_exec",
    "process_result",
    "save_append",
    "save_snapshot",
    "session_end",
    "session_start",
    "summarize",
    "syscall_event",
    "system_event",
    "tool_call",
    "tool_result",
    "user_prompt",
]
