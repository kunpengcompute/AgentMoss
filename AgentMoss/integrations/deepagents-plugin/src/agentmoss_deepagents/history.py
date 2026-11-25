"""Bounded session history; persistence belongs to the application's checkpointer."""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from functools import wraps
from typing import TYPE_CHECKING, Annotated, Any

from langchain.agents.middleware import AgentState

from .actions import Action
from .config import GateConfig
from .history_schema import DEFAULT_LIMITS, empty_history, migrate_history
from .history_schema import make_event_id as make_event_id
from .tool_lock import async_tool_lock, tool_lock

if TYPE_CHECKING:
    from .context import RunContext

KEY = "agentgate_history"


def event_key(event: dict) -> str:
    return event["event_id"]


def validate_history(value: dict, root: str) -> dict:
    return migrate_history(value, root)


def _turn_rank(value: dict) -> tuple[int, str]:
    turn = value["current_turn"]
    return (turn["order"], turn["id"]) if turn else (-1, "")


def prune(value: dict, actions: int | None = None, turns: int | None = None, budget: int | None = None) -> dict:
    result = deepcopy(value)
    for name, configured in (("actions", actions), ("turns", turns), ("bytes", budget)):
        if configured is not None:
            result["limits"][name] = min(DEFAULT_LIMITS[name], configured)
    limits, floor = result["limits"], result["pruned"]
    result["events"] = sorted(result["events"], key=lambda event: (event["order"], event["event_id"]))
    result["turns"] = sorted(result["turns"], key=lambda turn: (turn["order"], turn["id"]))

    def discard(kind, rows):
        if rows:
            floor[kind] = max(floor[kind], max(row["order"] for row in rows))
            result["truncated"] = True

    def enforce_cursors():
        result["turns"] = [turn for turn in result["turns"] if turn["order"] > floor["turns"]]
        valid = {turn["id"] for turn in result["turns"]}
        discard("events", [event for event in result["events"] if event["turn_id"] not in valid])
        result["events"] = [event for event in result["events"] if event["order"] > floor["events"]]

    enforce_cursors()
    for kind, maximum in (("events", limits["actions"]), ("turns", limits["turns"])):
        discard(kind, result[kind][:-maximum])
    enforce_cursors()
    while len(json.dumps(result, ensure_ascii=False).encode()) > limits["bytes"] and (
        result["events"] or result["turns"]
    ):
        kind = "events" if result["events"] else "turns"
        discard(kind, result[kind][:1])
        enforce_cursors()
    if len(json.dumps(result, ensure_ascii=False).encode()) > limits["bytes"]:
        raise ValueError("history_bytes is too small to retain session and pruning metadata")
    return result


def merge_history(old: dict, delta: dict) -> dict:
    if not old and not delta:
        return {}
    root = (old or delta)["root"]
    old, delta = validate_history(old, root), validate_history(delta, root)
    events = {}
    for event in old["events"] + delta["events"]:
        key = event_key(event)
        if key not in events or event["seq"] >= events[key]["seq"]:
            events[key] = event
    turns = {turn["id"]: turn for turn in old["turns"] + delta["turns"]}
    latest = max((old, delta), key=_turn_rank)
    # The latest root invocation supplies policy. At the same turn, apply the
    # stricter bounds so a stale graph snapshot cannot loosen an active policy.
    limits = dict(latest["limits"])
    if _turn_rank(old) == _turn_rank(delta):
        limits = {name: min(old["limits"][name], delta["limits"][name]) for name in DEFAULT_LIMITS}
    return prune(
        {
            "schema": 5,
            "root": root,
            "events": list(events.values()),
            "turns": list(turns.values()),
            "truncated": old["truncated"] or delta["truncated"],
            "limits": limits,
            "current_turn": latest["current_turn"],
            "clock": max(
                [old["clock"], delta["clock"]]
                + [turn["order"] for turn in turns.values()]
                + [event[name] for event in events.values() for name in ("seq", "order")]
            ),
            "pruned": {name: max(old["pruned"][name], delta["pruned"][name]) for name in ("events", "turns")},
        }
    )


class AgentMossState(AgentState):
    agentgate_history: Annotated[dict, merge_history]


def synchronized(method: Callable) -> Callable:
    @wraps(method)
    def wrapped(self, *args, **kwargs) -> Any:
        with self._state_guard:
            return method(self, *args, **kwargs)

    return wrapped


class RunHistory:
    """A shared in-memory view for this invocation; graph state owns persistence."""

    def __init__(self, root: str, value: dict, config: GateConfig) -> None:
        self.value = validate_history(value, root)
        self.config = config
        self.seq = max(self.value["clock"], max((event["seq"] for event in self.value["events"]), default=0))
        # Replay protection lasts for this invocation even when its audit window is pruned.
        self._seen_event_ids = {event_key(event) for event in self.value["events"]}
        self._state_guard = threading.RLock()
        self.lock = threading.Lock()

    def trim(self, value: dict) -> dict:
        return prune(
            value,
            min(200, self.config.history_actions),
            min(20, self.config.history_turns),
            min(32768, self.config.history_bytes),
        )

    @synchronized
    def merge(self, value: dict) -> None:
        merged = merge_history(self.value, value)
        self._seen_event_ids.update(event_key(event) for event in merged["events"])
        self.value = self.trim(merged)
        self.seq = max(self.seq, self.value["clock"])

    @synchronized
    def record_prompt(self, turn_id: str, user_prompt: str) -> None:
        self.seq += 1
        self.merge(
            empty_history(self.value["root"])
            | {
                "turns": [{"id": turn_id, "text": user_prompt, "order": self.seq}],
                "current_turn": {"id": turn_id, "order": self.seq},
                "clock": self.seq,
                "limits": {
                    "actions": min(200, self.config.history_actions),
                    "turns": min(20, self.config.history_turns),
                    "bytes": min(32768, self.config.history_bytes),
                },
            }
        )

    @synchronized
    def begin(self, run: RunContext, call_id: str, action: Action, original_name: str, *, event_id: str) -> dict:
        if run.session_id != self.value["root"]:
            raise ValueError("Cross-root tool call")
        if not call_id:
            raise ValueError("Recorded tool call cannot be replayed")
        if event_id in self._seen_event_ids:
            previous = next((row for row in self.value["events"] if event_key(row) == event_id), None)
            if original_name != "task" or previous is None or previous["status"] != "interrupted":
                raise ValueError("Recorded tool call cannot be replayed")
            # Only orchestration suspended by a native interrupt can re-enter.
            # The SDK resumes its child checkpoint; physical tools remain protected.
            self.seq += 1
            event = {**previous, "seq": self.seq, "status": "attempted", "decision": "", "source": ""}
            self.merge(empty_history(run.session_id) | {"events": [event], "clock": self.seq})
            return event
        self._seen_event_ids.add(event_id)
        self.seq += 1
        event = {
            "event_id": event_id,
            "tool_call_id": call_id,
            "turn_id": run.turn_id,
            "original_name": original_name,
            "action_type": action.tool_name,
            "action_detail": action.command,
            "arguments": action.metadata.get("original_arguments", {}),
            "cwd": action.cwd,
            "status": "attempted",
            "source": "",
            "decision": "",
            "seq": self.seq,
            "order": self.seq,
        }
        self.merge(empty_history(run.session_id) | {"events": [event], "clock": self.seq})
        return event

    @synchronized
    def finish(self, event: dict, status: str, *, decision: str = "", source: str = "") -> None:
        self.seq += 1
        updated = {**event, "status": status, "decision": decision, "source": str(source), "seq": self.seq}
        self.merge(empty_history(self.value["root"]) | {"events": [updated], "clock": self.seq})

    @synchronized
    def snapshot(self, *, omit_prompt_turn_id: str | None = None) -> dict:
        rows = list(self.value["events"])
        turns = list(self.value["turns"])
        truncated = self.value["truncated"]

        def build() -> dict:
            actions = [{"action_type": event["action_type"], "action_detail": event["action_detail"]} for event in rows]
            return {
                "action_history": actions,
                "prompt_history": [
                    {
                        "text": turn["text"],
                        "actions": [
                            action
                            for event, action in zip(rows, actions, strict=True)
                            if event["turn_id"] == turn["id"]
                        ],
                    }
                    for turn in turns
                ],
                "history_statuses": [
                    {"event_id": event["event_id"], "tool_call_id": event["tool_call_id"], "status": event["status"]}
                    for event in rows
                ],
                "history_arguments": [
                    {
                        "event_id": event["event_id"],
                        "tool_call_id": event["tool_call_id"],
                        "arguments": event["arguments"],
                    }
                    for event in rows
                ],
                "history_truncated": truncated,
            }

        result = build()
        while len(json.dumps(result, ensure_ascii=False).encode()) > self.config.history_bytes and (rows or turns):
            truncated = True
            if rows:
                rows.pop(0)
            else:
                turns.pop(0)
            result = build()
        if omit_prompt_turn_id is not None:
            # Keep the original history limits and omit the duplicate only in the wire copy.
            for turn, prompt in zip(turns, result["prompt_history"], strict=True):
                if turn["id"] == omit_prompt_turn_id:
                    prompt["text"] = ""
                    break
        return result

    @contextmanager
    def leaf_lock(self) -> Generator[None, None, None]:
        with tool_lock(self.lock):
            yield

    @asynccontextmanager
    async def async_leaf_lock(self) -> AsyncGenerator[None, None]:
        async with async_tool_lock(self.lock):
            yield
