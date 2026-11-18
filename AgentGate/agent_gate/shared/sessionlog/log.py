"""SessionLog — append-only event-sourcing log (borrowed from dsh session-log).

A `SessionLog` is a typed append-only log of `SessionEvent`s with contiguous
`seq`. Message history is DERIVED from the log (never stored separately), so
replay reproduces the identical interaction. Consumers (trajectory view,
evidence engine, AgentMoss report) never depend on a collector's raw format.

`append()` enforces JSON-serializability at the source (dsh invariant: events
must be lossless JSON); invalid events are rejected before they enter the log.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Iterator, Optional

from .events import (
    SessionEvent,
    SessionEventType,
    _is_jsonable,
)

logger = logging.getLogger(__name__)


class SessionLog:
    """In-memory append-only session event log with contiguous seq."""

    def __init__(self, session_id: str = "", events: Optional[list[SessionEvent]] = None):
        self.session_id = session_id
        self._events: list[SessionEvent] = list(events or [])
        # Reindex seq if a persisted log is loaded (must stay contiguous).
        self._renumber()

    # -- core ----------------------------------------------------------------

    def append(self, event: SessionEvent) -> SessionEvent:
        """Append one event, assigning the next contiguous seq.

        Raises ValueError if the event payload is not JSON-serializable
        (mirrors dsh: `Session.append` validates at the source).
        """
        if not _is_jsonable(event.data):
            raise ValueError(f"SessionLog: event data not JSON-serializable: {event.type}")
        event.seq = len(self._events)
        self._events.append(event)
        return event

    def append_many(self, events: list[SessionEvent]) -> list[SessionEvent]:
        out = []
        for e in events:
            out.append(self.append(e))
        return out

    def __len__(self) -> int:
        return len(self._events)

    @property
    def next_seq(self) -> int:
        return len(self._events)

    @property
    def events(self) -> list[SessionEvent]:
        return list(self._events)

    def __iter__(self) -> Iterator[SessionEvent]:
        return iter(self._events)

    def __getitem__(self, idx: int) -> SessionEvent:
        return self._events[idx]

    # -- queries -------------------------------------------------------------

    def by_type(self, etype: SessionEventType) -> list[SessionEvent]:
        return [e for e in self._events if e.type == etype]

    def tool_calls(self) -> list[SessionEvent]:
        return self.by_type(SessionEventType.TOOL_CALL)

    def tool_results(self) -> list[SessionEvent]:
        return self.by_type(SessionEventType.TOOL_RESULT)

    def pair_tool_calls(self) -> list[tuple[SessionEvent, Optional[SessionEvent]]]:
        """Pair each tool/call with its tool/result by callId (dsh callId pairing)."""
        results = {e.data.get("callId"): e for e in self.tool_results()}
        return [(c, results.get(c.data.get("callId"))) for c in self.tool_calls()]

    # -- derivation ----------------------------------------------------------

    def derive_messages(self) -> list[dict]:
        """Derive model-visible message history from the log.

        AgentGate semantics: user prompts (user/prompt) and assembled LLM
        responses (llm/response); tool calls are attached to the LLM response
        that requested them (same turn+step).
        """
        messages: list[dict] = []
        for e in self._events:
            if e.type == SessionEventType.USER_PROMPT:
                messages.append({"role": "user", "content": e.data.get("content", "")})
            elif e.type == SessionEventType.LLM_RESPONSE:
                content = e.data.get("content", "")
                calls = [c.data for c in self.tool_calls() if c.turn == e.turn and c.step == e.step]
                msg: dict = {"role": "assistant", "content": content}
                if calls:
                    msg["tool_calls"] = calls
                messages.append(msg)
        return messages

    def to_dicts(self) -> list[dict]:
        return [e.to_dict() for e in self._events]

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dicts(), ensure_ascii=False, indent=indent)

    # -- serialization -------------------------------------------------------

    @classmethod
    def from_dicts(cls, raw_list: list[dict], session_id: str = "") -> "SessionLog":
        events = [SessionEvent.from_dict(r) for r in raw_list]
        return cls(session_id=session_id, events=events)

    @classmethod
    def from_json(cls, text: str, session_id: str = "") -> "SessionLog":
        return cls.from_dicts(json.loads(text), session_id=session_id)

    def _renumber(self) -> None:
        for i, e in enumerate(self._events):
            e.seq = i

    # -- replay --------------------------------------------------------------

    def replay(self, visitor: Callable[[SessionEvent], None]) -> None:
        """Replay the whole log in order (dsh: replay = re-derive from events)."""
        for e in self._events:
            visitor(e)
