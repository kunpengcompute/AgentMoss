"""AIO requests must finish before an SDK search timeout finalizes tool history."""

import asyncio
import json
import threading

import deepagents.backends.protocol as protocol
import deepagents.middleware.filesystem as filesystem
import httpx
import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from models import ScriptedModel, call
from test_aio_backend import sdk_backend as sdk_backend
from test_native_sdk import run_args, setup

from agentmoss_deepagents import AgentMossMiddleware
from agentmoss_deepagents.history import KEY


class ObserveToolReturn(AgentMiddleware):
    """Observe the real SDK handler returning, inside the Gate middleware."""

    def __init__(self, returned):
        self.returned = returned

    def wrap_tool_call(self, request, handler):
        try:
            return handler(request)
        finally:
            self.returned.set()

    async def awrap_tool_call(self, request, handler):
        try:
            return await handler(request)
        finally:
            self.returned.set()


def search_graph(runtime, backend, tool, returned):
    middleware = AgentMossMiddleware(runtime, backend)
    return create_deep_agent(
        model=ScriptedModel(responses=[call(tool, {"pattern": "needle"}, "search"), AIMessage(content="done")]),
        backend=backend,
        context_schema=dict,
        middleware=[middleware, ObserveToolReturn(returned)],
        subagents=[{"name": "general-purpose", "description": "worker", "middleware": [middleware]}],
        checkpointer=InMemorySaver(),
    )


def search_reply(request):
    body = json.loads(request.content)
    if request.url.path.endswith("/grep"):
        data = {"path": body["path"], "pattern": body["pattern"], "matches": [], "match_count": 0}
    else:
        data = {
            "session_id": body["id"], "command": body["command"],
            "status": "completed", "exit_code": 0, "output": "",
        }
    return httpx.Response(200, json={"success": True, "data": data})


@pytest.fixture(autouse=True)
def short_search_deadlines(monkeypatch):
    monkeypatch.setattr(filesystem, "GLOB_TIMEOUT", 0.05)
    monkeypatch.setattr(protocol, "ASYNC_GREP_TIMEOUT", 0.05)


@pytest.mark.parametrize("tool,sync", [("glob", True), ("glob", False), ("grep", False)])
@pytest.mark.parametrize("lost_response", [False, True])
async def test_search_timeout_waits_for_request_and_checkpoints_final_certainty(
    tmp_path, sdk_backend, tool, sync, lost_response
):
    backend, submitted, replies = sdk_backend
    started, release, returned = threading.Event(), threading.Event(), threading.Event()

    def delayed(request):
        started.set()
        assert release.wait(5)
        if lost_response:
            raise httpx.ReadTimeout("response lost", request=request)
        return search_reply(request)

    replies.append(delayed)
    runtime, _ = setup(tmp_path)
    graph = search_graph(runtime, backend, tool, returned)
    inputs, config, context = run_args()
    running = asyncio.create_task(
        asyncio.to_thread(graph.invoke, inputs, config, context=context)
        if sync else graph.ainvoke(inputs, config, context=context)
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert await asyncio.to_thread(returned.wait, 2)
        done, _ = await asyncio.wait({running}, timeout=0.1)
        assert not done, "Graph finished while its AIO search request was still running"
        release.set()
        result = await asyncio.wait_for(running, 2)
        expected = "unknown" if lost_response else "executed_error"
        assert result[KEY]["events"][0]["status"] == expected
        saved = await graph.aget_state(config)
        assert saved.values[KEY]["events"][0]["status"] == expected
        assert backend.status()["state"] == ("uncertain" if lost_response else "ready")
        assert len(submitted) == 1
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        await runtime.aclose()


@pytest.mark.parametrize("tool", ["glob", "grep"])
async def test_cancellation_while_draining_search_still_waits_for_aio(tmp_path, sdk_backend, tool):
    backend, _, replies = sdk_backend
    started, release, returned = threading.Event(), threading.Event(), threading.Event()

    def delayed(request):
        started.set()
        assert release.wait(5)
        raise httpx.ReadTimeout("response lost", request=request)

    replies.append(delayed)
    runtime, _ = setup(tmp_path)
    graph = search_graph(runtime, backend, tool, returned)
    inputs, config, context = run_args()
    running = asyncio.create_task(graph.ainvoke(inputs, config, context=context))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert await asyncio.to_thread(returned.wait, 2)
        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
        done, _ = await asyncio.wait({running}, timeout=0.1)
        assert not done, "Cancellation abandoned the outstanding AIO request"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 2)
        assert backend.status()["state"] == "uncertain"
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        await runtime.aclose()


async def test_glob_worker_cannot_submit_after_its_tool_scope_finishes(tmp_path, sdk_backend, monkeypatch):
    backend, submitted, replies = sdk_backend
    started, release, finished, returned = (threading.Event() for _ in range(4))
    original = backend.glob

    def delayed_start(*args, **kwargs):
        started.set()
        assert release.wait(5)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(backend, "glob", delayed_start)
    replies.append(search_reply)
    runtime, _ = setup(tmp_path)
    graph = search_graph(runtime, backend, "glob", returned)
    inputs, config, context = run_args()
    running = asyncio.create_task(asyncio.to_thread(graph.invoke, inputs, config, context=context))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.wait_for(running, 2)
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert submitted == [], "A timed-out worker submitted an AIO request after the graph returned"
        assert backend.status()["state"] == "ready"
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        await asyncio.to_thread(finished.wait, 2)
        await runtime.aclose()
