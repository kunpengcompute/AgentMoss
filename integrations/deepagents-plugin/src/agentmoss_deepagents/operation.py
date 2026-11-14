"""Track one tool's AIO requests and certainty across SDK wrappers and threads."""

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from .runtime import drain


@dataclass
class ToolOperation:
    uncertain: bool = False
    _active: int = 0
    _closed: bool = False
    _condition: threading.Condition = field(default_factory=threading.Condition, repr=False)

    @contextmanager
    def track(self):
        with self._condition:
            if self._closed:
                raise RuntimeError("Tool call has finished; queued AIO request was not submitted")
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.wait_for(lambda: self._active == 0)

    async def aclose(self):
        # Seal before yielding so a delayed SDK worker cannot start after the
        # handler returns. Already-admitted requests still update certainty.
        with self._condition:
            self._closed = True
            pending = self._active > 0
        if pending:
            await drain(asyncio.to_thread(self.close), propagate_cancel=True)


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
        try:
            outcome.close()
        finally:
            _current.reset(token)


@asynccontextmanager
async def async_tool_operation():
    outcome = ToolOperation()
    token = _current.set(outcome)
    try:
        yield outcome
    finally:
        try:
            await outcome.aclose()
        finally:
            _current.reset(token)


@contextmanager
def track_operation():
    outcome = _current.get()
    if outcome is None:
        # Direct backend calls retain the upstream cancellation behavior.
        yield
    else:
        with outcome.track():
            yield


def mark_uncertain() -> None:
    if outcome := _current.get():
        outcome.uncertain = True
