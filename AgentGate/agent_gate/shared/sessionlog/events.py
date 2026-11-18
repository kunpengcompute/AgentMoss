"""Session log events — typed, append-only event vocabulary.

Inspired by DeepSeek Harness' session-log event-sourcing model (append-only
typed log + contiguous seq + message history DERIVED from the log), but named
with AgentGate's own collector semantics (llm / tool / process / mcp /
syscall) instead of dsh's turn/step vocabulary.

Event types map 1:1 onto AgentGate's source_collector dimensions:
- session/start, session/end           — session lifecycle
- user/prompt                          — L1 payload.user_prompt / transcript
- llm/request, llm/chunk, llm/response — LLM API traffic (chunk = raw stream,
                                          response carries token usage)
- tool/call, tool/result               — tool invocation (source: llm|mcp)
- process/exec, process/result         — L2 PTY command execution
- syscall/event                        — L3 eBPF system call
- system                               — collector metadata / errors

Every event carries `turn`/`step` as positional facts (a turn is one user
input round; a step is one model call + its tool executions) and `source`
marking which collector produced it.
"""

from __future__ import annotations

import dataclasses
import json
import time
from enum import Enum
from typing import Any, Optional


class SessionEventType(str, Enum):
    SESSION_START = "session/start"
    SESSION_END = "session/end"
    USER_PROMPT = "user/prompt"
    LLM_REQUEST = "llm/request"
    LLM_CHUNK = "llm/chunk"
    LLM_RESPONSE = "llm/response"
    TOOL_CALL = "tool/call"
    TOOL_RESULT = "tool/result"
    PROCESS_EXEC = "process/exec"
    PROCESS_RESULT = "process/result"
    SYSCALL = "syscall/event"
    SYSTEM = "system"


@dataclasses.dataclass
class SessionEvent:
    """One append-only log entry.

    A discriminated union keyed by `type` with contiguous `seq` and
    epoch-ms `time`. `data` is the type-specific payload (JSON-serializable).
    """

    type: SessionEventType
    seq: int
    time: int
    data: dict  # type-specific payload, must be JSON-serializable

    # Positional facts (not derived).
    turn: int = 0
    step: int = 0

    # Source collector annotation: llm_api_proxy | pty | ebpf | mcp | transcript
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "seq": self.seq,
            "time": self.time,
            "turn": self.turn,
            "step": self.step,
            "source": self.source,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SessionEvent":
        return cls(
            type=SessionEventType(raw["type"]),
            seq=int(raw["seq"]),
            time=int(raw["time"]),
            turn=int(raw.get("turn", 0)),
            step=int(raw.get("step", 0)),
            source=str(raw.get("source", "")),
            data=raw.get("data", {}),
        )


def _is_jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Factory helpers — AgentGate-semantic typed append vocabulary
# ---------------------------------------------------------------------------

def _ev(type_: SessionEventType, seq: int, data: dict, *, turn: int = 0, step: int = 0,
        time_ms: Optional[int] = None, source: str = "") -> SessionEvent:
    return SessionEvent(type_, seq, time_ms or _now_ms(), data, turn=turn, step=step, source=source)


def session_start(seq: int, *, session_meta: Optional[dict] = None, time_ms: Optional[int] = None,
                  source: str = "") -> SessionEvent:
    return _ev(SessionEventType.SESSION_START, seq, {"session": session_meta or {}},
               time_ms=time_ms, source=source)


def session_end(seq: int, reason: str = "", *, time_ms: Optional[int] = None, source: str = "") -> SessionEvent:
    return _ev(SessionEventType.SESSION_END, seq, {"reason": reason}, time_ms=time_ms, source=source)


def user_prompt(seq: int, content: str, *, turn: int = 0, time_ms: Optional[int] = None,
                source: str = "") -> SessionEvent:
    return _ev(SessionEventType.USER_PROMPT, seq, {"content": content, "role": "user"},
               turn=turn, time_ms=time_ms, source=source)


def llm_request(seq: int, model: str, *, messages_count: int = 0, provider: str = "",
                request_meta: Optional[dict] = None, turn: int = 0, time_ms: Optional[int] = None,
                source: str = "") -> SessionEvent:
    """One LLM API request (L1). `request_meta` keeps model/headers without raw body."""
    return _ev(SessionEventType.LLM_REQUEST, seq,
               {"model": model, "messages_count": messages_count, "provider": provider,
                "request_meta": request_meta or {}},
               turn=turn, time_ms=time_ms, source=source)


def llm_chunk(seq: int, chunk: str, *, turn: int = 0, step: int = 0, time_ms: Optional[int] = None,
              source: str = "") -> SessionEvent:
    """Raw stream chunk — token-level replay fidelity (mirrors dsh assistant/chunk)."""
    return _ev(SessionEventType.LLM_CHUNK, seq, {"chunk": chunk}, turn=turn, step=step,
               time_ms=time_ms, source=source)


def llm_response(seq: int, content: str, *, model: str = "", turn: int = 0, step: int = 0,
                 usage: Optional[dict] = None, time_ms: Optional[int] = None,
                 source: str = "") -> SessionEvent:
    """Assembled LLM response; carries token usage when the adapter reported it."""
    return _ev(SessionEventType.LLM_RESPONSE, seq,
               {"content": content, "model": model, "usage": usage or {}},
               turn=turn, step=step, time_ms=time_ms, source=source)


def tool_call(seq: int, name: str, arguments: str, call_id: str, *, turn: int = 0, step: int = 0,
              time_ms: Optional[int] = None, source: str = "llm") -> SessionEvent:
    """One tool invocation — raw arguments JSON string unparsed (dsh tool/call)."""
    return _ev(SessionEventType.TOOL_CALL, seq,
               {"name": name, "arguments": arguments, "callId": call_id},
               turn=turn, step=step, time_ms=time_ms, source=source)


def tool_result(seq: int, call_id: str, content: Any, *, turn: int = 0, step: int = 0,
                error: Optional[dict] = None, time_ms: Optional[int] = None,
                source: str = "llm") -> SessionEvent:
    """Completed tool call's result, optional error identity (dsh tool/result)."""
    return _ev(SessionEventType.TOOL_RESULT, seq,
               {"callId": call_id, "content": content, "error": error or {}},
               turn=turn, step=step, time_ms=time_ms, source=source)


def process_exec(seq: int, command: str, *, argv: Optional[list] = None, pid: Optional[int] = None,
                 cwd: str = "", turn: int = 0, time_ms: Optional[int] = None,
                 source: str = "pty") -> SessionEvent:
    """L2 PTY command execution (agent subprocess)."""
    return _ev(SessionEventType.PROCESS_EXEC, seq,
               {"command": command, "argv": argv or [], "pid": pid, "cwd": cwd},
               turn=turn, time_ms=time_ms, source=source)


def process_result(seq: int, command: str, output: str, *, exit_code: Optional[int] = None,
                   pid: Optional[int] = None, time_ms: Optional[int] = None,
                   source: str = "pty") -> SessionEvent:
    """L2 PTY command output."""
    return _ev(SessionEventType.PROCESS_RESULT, seq,
               {"command": command, "output": output, "exit_code": exit_code, "pid": pid},
               time_ms=time_ms, source=source)


def syscall_event(seq: int, syscall: str, *, argv: Optional[list] = None, pid: Optional[int] = None,
                  time_ms: Optional[int] = None, source: str = "ebpf") -> SessionEvent:
    """L3 eBPF system call event."""
    return _ev(SessionEventType.SYSCALL, seq,
               {"syscall": syscall, "argv": argv or [], "pid": pid},
               time_ms=time_ms, source=source)


def system_event(seq: int, kind: str, detail: Any = None, *, time_ms: Optional[int] = None,
                 source: str = "") -> SessionEvent:
    """Collector metadata / errors / phase boundaries."""
    return _ev(SessionEventType.SYSTEM, seq, {"kind": kind, "detail": detail},
               time_ms=time_ms, source=source)
