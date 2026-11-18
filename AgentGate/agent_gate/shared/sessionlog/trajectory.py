"""Trajectory view — assemble the session log into a user-facing data flow.

Inspired by dsh's ui-trajectory, but AgentGate-flavored: the record table
covers every collector dimension (llm / tool / process / syscall) and keeps
only the INFORMATION a user actually needs — who did what, when, how long it
took, tokens spent, and whether it succeeded. Raw payload (input/output) is
attached but presentation stays digestible.

Record shapes:
- {kind: "user",    seq, time, content}
- {kind: "llm",     seq, time, turn, step, model, content, usage, duration_ms}
- {kind: "tool",    seq, time, turn, step, call_id, name, arguments, result,
   error, duration_ms, source}
- {kind: "process", seq, time, command, argv, pid, cwd, output, exit_code}
- {kind: "syscall", seq, time, syscall, argv, pid}
- {kind: "system",  seq, time, detail}
- {kind: "turn",    turn, start_time, end_time}  (section divider)
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .events import SessionEvent, SessionEventType
from .log import SessionLog


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def build_trajectory(log: SessionLog) -> list[dict]:
    """Assemble the log into an ordered list of user-facing flow records."""
    records: list[dict] = []
    turns: dict[int, dict] = {}
    step_marks: dict[tuple[int, int], dict] = {}
    tool_times: dict[str, int] = {}
    proc_times: dict[str, int] = {}

    for e in log.events:
        if e.type == SessionEventType.SESSION_START:
            records.append({"kind": "system", "seq": e.seq, "time": e.time,
                            "detail": "session/start"})
        elif e.type == SessionEventType.SESSION_END:
            records.append({"kind": "system", "seq": e.seq, "time": e.time,
                            "detail": "session/end"})
        elif e.type == SessionEventType.USER_PROMPT:
            records.append({
                "kind": "user", "seq": e.seq, "time": e.time, "turn": e.turn,
                "content": _as_text(e.data.get("content", "")),
            })
        elif e.type == SessionEventType.LLM_RESPONSE:
            usage = e.data.get("usage") or {}
            records.append({
                "kind": "llm", "seq": e.seq, "time": e.time, "turn": e.turn, "step": e.step,
                "model": str(e.data.get("model", "")),
                "content": _as_text(e.data.get("content", "")),
                "usage": usage,
                "duration_ms": _step_duration(step_marks, e.turn, e.step),
            })
        elif e.type == SessionEventType.TOOL_CALL:
            tool_times[str(e.data.get("callId", ""))] = e.time
            records.append({
                "kind": "tool", "seq": e.seq, "time": e.time, "turn": e.turn, "step": e.step,
                "call_id": str(e.data.get("callId", "")),
                "name": str(e.data.get("name", "")),
                "arguments": str(e.data.get("arguments", "")),
                "result": None, "error": None, "duration_ms": None,
                "source": e.source,
            })
        elif e.type == SessionEventType.TOOL_RESULT:
            call_id = str(e.data.get("callId", ""))
            start = tool_times.get(call_id)
            duration = None
            if start is not None:
                duration = e.time - start
            tool_records = [r for r in records if r["kind"] == "tool" and r["call_id"] == call_id]
            if tool_records:
                r = tool_records[-1]
                r["result"] = e.data.get("content")
                r["error"] = e.data.get("error") or None
                r["duration_ms"] = duration
            else:
                records.append({
                    "kind": "tool", "seq": e.seq, "time": e.time, "turn": e.turn, "step": e.step,
                    "call_id": call_id, "name": "", "arguments": "",
                    "result": e.data.get("content"), "error": e.data.get("error") or None,
                    "duration_ms": duration, "source": e.source,
                })
        elif e.type == SessionEventType.PROCESS_EXEC:
            key = str(e.data.get("pid") or e.data.get("command"))
            proc_times[key] = e.time
            records.append({
                "kind": "process", "seq": e.seq, "time": e.time, "turn": e.turn,
                "command": str(e.data.get("command", "")),
                "argv": e.data.get("argv") or [],
                "pid": e.data.get("pid"),
                "cwd": str(e.data.get("cwd", "")),
                "output": None, "exit_code": None, "duration_ms": None,
            })
        elif e.type == SessionEventType.PROCESS_RESULT:
            key = str(e.data.get("pid") or e.data.get("command"))
            start = proc_times.get(key)
            duration = None
            if start is not None:
                duration = e.time - start
            matches = [r for r in records if r["kind"] == "process" and r["pid"] == e.data.get("pid")]
            if matches:
                r = matches[-1]
                r["output"] = _as_text(e.data.get("output", ""))
                r["exit_code"] = e.data.get("exit_code")
                r["duration_ms"] = duration
            else:
                records.append({
                    "kind": "process", "seq": e.seq, "time": e.time, "turn": e.turn,
                    "command": str(e.data.get("command", "")), "argv": [],
                    "pid": e.data.get("pid"), "cwd": "",
                    "output": _as_text(e.data.get("output", "")),
                    "exit_code": e.data.get("exit_code"), "duration_ms": duration,
                })
        elif e.type == SessionEventType.SYSCALL:
            records.append({
                "kind": "syscall", "seq": e.seq, "time": e.time,
                "syscall": str(e.data.get("syscall", "")),
                "argv": e.data.get("argv") or [],
                "pid": e.data.get("pid"),
            })
        elif e.type == SessionEventType.SYSTEM:
            records.append({
                "kind": "system", "seq": e.seq, "time": e.time,
                "detail": str(e.data.get("kind", "")),
                "content": e.data.get("detail"),
            })
        # llm/chunk + llm/request are replay-fidelity data, not separate records.

    records.sort(key=lambda r: r.get("seq", 0))
    return records


def _step_duration(marks: dict, turn: int, step: int) -> Optional[int]:
    m = marks.get((turn, step))
    if not m or m.get("start") is None or m.get("end") is None:
        return None
    return m["end"] - m["start"]


def summarize(log: SessionLog) -> dict:
    """Aggregate summary: turn count, tool count, token usage, duration."""
    events = list(log.events)
    if not events:
        return {"turns": 0, "llm_calls": 0, "tool_calls": 0, "tool_errors": 0,
                "process_execs": 0, "syscalls": 0,
                "input_tokens": 0, "output_tokens": 0, "start_time": None, "end_time": None}
    start = min(e.time for e in events)
    end = max(e.time for e in events)
    usage_in = usage_out = 0
    for e in events:
        if e.type == SessionEventType.LLM_RESPONSE:
            u = e.data.get("usage") or {}
            usage_in += int(u.get("input_tokens", u.get("prompt_tokens", 0)) or 0)
            usage_out += int(u.get("output_tokens", u.get("completion_tokens", 0)) or 0)
    return {
        "turns": len({e.turn for e in events if e.turn}),
        "llm_calls": len([e for e in events if e.type == SessionEventType.LLM_RESPONSE]),
        "tool_calls": len([e for e in events if e.type == SessionEventType.TOOL_CALL]),
        "tool_errors": len([e for e in events if e.type == SessionEventType.TOOL_RESULT and e.data.get("error")]),
        "process_execs": len([e for e in events if e.type == SessionEventType.PROCESS_EXEC]),
        "syscalls": len([e for e in events if e.type == SessionEventType.SYSCALL]),
        "input_tokens": usage_in,
        "output_tokens": usage_out,
        "start_time": start,
        "end_time": end,
        "duration_ms": end - start,
    }
