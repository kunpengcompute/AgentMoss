"""Serialize sibling tools while allowing an awaited, instrumented child graph."""

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from .runtime import drain


@dataclass
class _Scope:
    root: Any
    children: Any = field(default_factory=threading.Lock)
    closed: bool = False


_scopes: ContextVar[tuple[_Scope, ...]] = ContextVar("agentmoss_tool_scopes", default=())


def _parent(root):
    return next((scope for scope in reversed(_scopes.get()) if scope.root is root), None)


def _admit(parent):
    if parent is not None and parent.closed:
        raise ValueError("Parent tool call has finished; delayed child tool was not admitted")


async def _acquire(lock):
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.005)


async def _join(scope):
    await _acquire(scope.children)
    scope.children.release()


@contextmanager
def tool_lock(root):
    parent = _parent(root)
    _admit(parent)
    with parent.children if parent is not None else root:
        _admit(parent)
        scope = _Scope(root)
        token = _scopes.set((*_scopes.get(), scope))
        try:
            yield
        finally:
            scope.closed = True
            try:
                # A copied context may already have admitted a child worker.
                # Drain it before making the root lock available to other tools.
                with scope.children:
                    pass
            finally:
                _scopes.reset(token)


@asynccontextmanager
async def async_tool_lock(root):
    parent = _parent(root)
    _admit(parent)
    lock = parent.children if parent is not None else root
    await _acquire(lock)
    try:
        _admit(parent)
        scope = _Scope(root)
        token = _scopes.set((*_scopes.get(), scope))
        try:
            yield
        finally:
            scope.closed = True
            try:
                await drain(_join(scope), propagate_cancel=True)
            finally:
                _scopes.reset(token)
    finally:
        lock.release()
