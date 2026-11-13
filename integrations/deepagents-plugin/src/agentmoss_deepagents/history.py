"""Bounded session history; persistence belongs to the application's checkpointer."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from functools import wraps
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID, uuid5

from langchain.agents.middleware import AgentState

from .actions import Action
from .config import GateConfig

if TYPE_CHECKING:
    from .context import RunContext

KEY = "agentgate_history"
_CALL_FIELDS = {
    "tool_call_id",
    "turn_id",
    "original_name",
    "action_type",
    "arguments",
    "action_detail",
    "cwd",
    "status",
    "source",
    "decision",
    "seq",
    "order",
}
_LEGACY_FIELDS = _CALL_FIELDS | {"root_session_id", "scope", "parent_call_id"}
EVENT_FIELDS = _CALL_FIELDS | {"event_id"}
STATUSES = {"attempted", "denied", "executed_success", "executed_error", "unknown"}
_EVENT_NAMESPACE = UUID("b4ae6ea9-52c4-5b0d-84e8-9ad8a250a8bd")


def _event_id(parts: list[str]) -> str:
    return str(uuid5(_EVENT_NAMESPACE, json.dumps(parts, ensure_ascii=False, separators=(",", ":"))))


def make_event_id(session_id: str, checkpoint_ns: str, tool_call_id: str) -> str:
    """Identify SDK execution without assuming model call IDs are globally unique."""
    parts = [session_id, checkpoint_ns, tool_call_id]
    if any(not isinstance(part, str) or not part.strip() for part in parts):
        raise ValueError("Session, SDK execution namespace and tool call ID are required")
    return _event_id(["sdk-v4", *parts])


def event_key(event: dict) -> str:
    return event["event_id"]


def empty_history(root: str = "") -> dict:
    return {"schema": 4, "root": root, "events": [], "turns": [], "truncated": False}


def _validate_structure(value: dict, root: str) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "root", "events", "turns", "truncated"}
        or type(value["schema"]) is not int
        or value["schema"] not in {3, 4}
        or value["root"] != root
        or not isinstance(value["truncated"], bool)
        or not isinstance(value["events"], list)
        or not isinstance(value["turns"], list)
    ):
        raise ValueError("Incompatible or cross-root plugin context")
    fields = _LEGACY_FIELDS if value["schema"] == 3 else EVENT_FIELDS
    for event in value["events"]:
        if not isinstance(event, dict) or set(event) != fields:
            raise ValueError("Invalid tool-call event")
        if not isinstance(event["arguments"], dict):
            raise ValueError("Invalid structured arguments")
        if any(not isinstance(event[name], str) for name in fields - {"seq", "order", "arguments"}):
            raise ValueError("Invalid event text")
        if event["status"] not in STATUSES or any(type(event[name]) is not int for name in ("seq", "order")):
            raise ValueError("Invalid event status or sequence")
        if value["schema"] == 3 and event["root_session_id"] != root:
            raise ValueError("Cross-root legacy event")
        if value["schema"] == 4:
            try:
                UUID(event["event_id"])
            except ValueError as error:
                raise ValueError("Invalid event ID") from error
    for turn in value["turns"]:
        if (
            not isinstance(turn, dict)
            or set(turn) != {"id", "text"}
            or not all(isinstance(item, str) for item in turn.values())
        ):
            raise ValueError("Invalid user turn")


def migrate_history(value: dict, root: str) -> dict:
    """Validate before converting; never change the supplied checkpoint in place."""
    if isinstance(value, dict) and not value:
        return empty_history(root)
    _validate_structure(value, root)
    result = deepcopy(value)
    if result["schema"] == 3:
        for event in result["events"]:
            if event["status"] in {"attempted", "unknown"}:
                raise ValueError("Cannot upgrade unfinished legacy calls; inspect the old checkpoint first")
            event["event_id"] = _event_id(["legacy-v3", root, event.pop("scope"), event["tool_call_id"]])
            del event["parent_call_id"], event["root_session_id"]
        result["schema"] = 4
    return result


def validate_history(value: dict, root: str) -> dict:
    return migrate_history(value, root)


def prune(value: dict, actions: int = 200, turns: int = 20, budget: int = 32768) -> dict:
    result = {**value, "events": list(value["events"]), "turns": list(value["turns"])}
    if len(result["events"]) > actions or len(result["turns"]) > turns:
        result["truncated"] = True
    result["events"] = result["events"][-actions:]
    result["turns"] = result["turns"][-turns:]
    valid = {turn["id"] for turn in result["turns"]}
    retained = [event for event in result["events"] if event["turn_id"] in valid]
    if len(retained) != len(result["events"]):
        result["truncated"] = True
    result["events"] = retained
    while len(json.dumps(result, ensure_ascii=False).encode()) > budget and (result["events"] or result["turns"]):
        result["truncated"] = True
        (result["events"] if result["events"] else result["turns"]).pop(0)
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
    return prune(
        {
            "schema": 4,
            "root": root,
            "events": sorted(events.values(), key=lambda event: (event["order"], event["event_id"])),
            "turns": list(turns.values()),
            "truncated": old["truncated"] or delta["truncated"],
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
        self.seq = max((event["seq"] for event in self.value["events"]), default=0)
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
        self.seq = max(self.seq, max((event["seq"] for event in self.value["events"]), default=0))

    @synchronized
    def record_prompt(self, turn_id: str, user_prompt: str) -> None:
        self.merge(empty_history(self.value["root"]) | {"turns": [{"id": turn_id, "text": user_prompt}]})

    @synchronized
    def begin(self, run: RunContext, call_id: str, action: Action, original_name: str, *, event_id: str) -> dict:
        if run.session_id != self.value["root"]:
            raise ValueError("Cross-root tool call")
        if not call_id or event_id in self._seen_event_ids:
            raise ValueError("Recorded tool call cannot be replayed")
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
        self.merge(empty_history(run.session_id) | {"events": [event]})
        return event

    @synchronized
    def finish(self, event: dict, status: str, *, decision: str = "", source: str = "") -> None:
        self.seq += 1
        updated = {**event, "status": status, "decision": decision, "source": str(source), "seq": self.seq}
        self.merge(empty_history(self.value["root"]) | {"events": [updated]})

    @synchronized
    def snapshot(self) -> dict:
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
        return result

    @contextmanager
    def leaf_lock(self) -> Generator[None, None, None]:
        with self.lock:
            yield

    @asynccontextmanager
    async def async_leaf_lock(self) -> AsyncGenerator[None, None]:
        acquired = False
        try:
            while not acquired:
                acquired = self.lock.acquire(blocking=False)
                if not acquired:
                    await asyncio.sleep(0.005)
            yield
        finally:
            if acquired:
                self.lock.release()
