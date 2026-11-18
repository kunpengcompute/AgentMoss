"""SessionLog JSONL persistence — lossless, append-only, contiguous seq.

Mirrors dsh `session-persistence` conventions: the durable log preserves
every event verbatim (including raw chunks); `load` returns events exactly
as appended. One JSON object per line; seq must stay contiguous.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from .events import SessionEvent
from .log import SessionLog

logger = logging.getLogger(__name__)


def save_append(log: SessionLog, path: str) -> None:
    """Append the log's events to a JSONL file (one event per line)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for e in log.events:
            f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")


def load(path: str, session_id: str = "") -> SessionLog:
    """Load a session log from a JSONL file, renumbering seq contiguously."""
    events: list[SessionEvent] = []
    if not os.path.isfile(path):
        return SessionLog(session_id=session_id)
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(SessionEvent.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning("SessionLog: skip bad line in %s: %s", path, e)
    return SessionLog(session_id=session_id, events=events)


def save_snapshot(log: SessionLog, path: str) -> None:
    """Write a full snapshot (overwrites); useful for a completed session."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in log.events:
            f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
