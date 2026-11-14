"""Carry one tool's execution certainty through SDK wrappers and worker threads."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class ToolOperation:
    uncertain: bool = False


_current: ContextVar[ToolOperation | None] = ContextVar("agentmoss_tool_operation", default=None)


@contextmanager
def tool_operation():
    # Context copies in asyncio.to_thread share this object, not later assignments
    # to the ContextVar. Nested child tools receive their own outcome object.
    outcome = ToolOperation()
    token = _current.set(outcome)
    try:
        yield outcome
    finally:
        _current.reset(token)


def mark_uncertain() -> None:
    if outcome := _current.get():
        outcome.uncertain = True
