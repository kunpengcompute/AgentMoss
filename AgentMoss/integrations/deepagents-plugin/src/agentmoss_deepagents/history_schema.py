"""Validate persisted audit history and migrate completed older checkpoints."""

import json
from copy import deepcopy
from uuid import UUID, uuid5

DEFAULT_LIMITS = {"actions": 200, "turns": 20, "bytes": 32768}
STATUSES = {"attempted", "denied", "executed_success", "executed_error", "unknown", "interrupted"}
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
_HISTORY_FIELDS = {"schema", "root", "events", "turns", "truncated"}
_V5_FIELDS = _HISTORY_FIELDS | {"limits", "clock", "pruned", "current_turn"}
_EVENT_NAMESPACE = UUID("b4ae6ea9-52c4-5b0d-84e8-9ad8a250a8bd")


def _event_id(parts: list[str]) -> str:
    return str(uuid5(_EVENT_NAMESPACE, json.dumps(parts, ensure_ascii=False, separators=(",", ":"))))


def make_event_id(session_id: str, checkpoint_ns: str, tool_call_id: str) -> str:
    """Keep execution IDs stable across the history schema upgrade."""
    parts = [session_id, checkpoint_ns, tool_call_id]
    if any(not isinstance(part, str) or not part.strip() for part in parts):
        raise ValueError("Session, SDK execution namespace and tool call ID are required")
    return _event_id(["sdk-v4", *parts])


def empty_history(root: str = "") -> dict:
    return {
        "schema": 5,
        "root": root,
        "events": [],
        "turns": [],
        "truncated": False,
        "limits": dict(DEFAULT_LIMITS),
        "clock": 0,
        "pruned": {"events": -1, "turns": -1},
        "current_turn": None,
    }


def _validate_structure(value: dict, root: str) -> None:
    if not isinstance(value, dict):
        raise ValueError("Incompatible or cross-root plugin context")
    version = value.get("schema")
    if (
        type(version) is not int
        or version not in {3, 4, 5}
        or set(value) != (_V5_FIELDS if version == 5 else _HISTORY_FIELDS)
        or value["root"] != root
        or not isinstance(value["truncated"], bool)
        or not isinstance(value["events"], list)
        or not isinstance(value["turns"], list)
    ):
        raise ValueError("Incompatible or cross-root plugin context")
    fields = _LEGACY_FIELDS if version == 3 else EVENT_FIELDS
    for event in value["events"]:
        if not isinstance(event, dict) or set(event) != fields:
            raise ValueError("Invalid tool-call event")
        if not isinstance(event["arguments"], dict):
            raise ValueError("Invalid structured arguments")
        if any(not isinstance(event[name], str) for name in fields - {"seq", "order", "arguments"}):
            raise ValueError("Invalid event text")
        if event["status"] not in STATUSES or any(
            type(event[name]) is not int or event[name] < 0 for name in ("seq", "order")
        ):
            raise ValueError("Invalid event status or sequence")
        if version == 3 and event["root_session_id"] != root:
            raise ValueError("Cross-root legacy event")
        if version >= 4:
            try:
                UUID(event["event_id"])
            except ValueError as error:
                raise ValueError("Invalid event ID") from error
    for turn in value["turns"]:
        if (
            not isinstance(turn, dict)
            or set(turn) != ({"id", "text", "order"} if version == 5 else {"id", "text"})
            or not all(isinstance(turn[name], str) for name in ("id", "text"))
            or (version == 5 and (type(turn["order"]) is not int or turn["order"] < 0))
        ):
            raise ValueError("Invalid user turn")
    if version == 5:
        for key, expected in (("limits", DEFAULT_LIMITS), ("pruned", {"events": 0, "turns": 0})):
            if not isinstance(value[key], dict) or value[key].keys() != expected.keys():
                raise ValueError("Invalid history bounds")
            for name, number in value[key].items():
                if type(number) is not int or number < (1 if key == "limits" else -1):
                    raise ValueError("Invalid history bounds")
                if key == "limits" and number > DEFAULT_LIMITS[name]:
                    raise ValueError("History limits exceed supported bounds")
        current = value["current_turn"]
        if current is not None and (
            not isinstance(current, dict)
            or set(current) != {"id", "order"}
            or not isinstance(current["id"], str)
            or not current["id"]
            or type(current["order"]) is not int
            or current["order"] < 0
        ):
            raise ValueError("Invalid current turn")
        if type(value["clock"]) is not int or value["clock"] < 0:
            raise ValueError("Invalid history clock")


def migrate_history(value: dict, root: str) -> dict:
    if isinstance(value, dict) and not value:
        return empty_history(root)
    _validate_structure(value, root)
    result = deepcopy(value)
    if result["schema"] < 5:
        if any(event["status"] in {"attempted", "unknown", "interrupted"} for event in result["events"]):
            raise ValueError("Cannot upgrade unfinished legacy calls; inspect the old checkpoint first")
        if result["schema"] == 3:
            for event in result["events"]:
                event["event_id"] = _event_id(["legacy-v3", root, event.pop("scope"), event["tool_call_id"]])
                del event["parent_call_id"], event["root_session_id"]
        result["turns"] = [{**turn, "order": index} for index, turn in enumerate(result["turns"], 1)]
        result = empty_history(root) | result | {"schema": 5}
        if result["turns"]:
            last = result["turns"][-1]
            result["current_turn"] = {"id": last["id"], "order": last["order"]}
        result["clock"] = max(
            [turn["order"] for turn in result["turns"]]
            + [event[name] for event in result["events"] for name in ("seq", "order")],
            default=0,
        )
    return result
