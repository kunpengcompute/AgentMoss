"""Applications own admission/input/stream cleanup; use unwrapped SDK graphs."""

import asyncio
import threading
from contextlib import aclosing, asynccontextmanager

import pytest
from deepagents.backends.filesystem import FilesystemBackend
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from models import call
from test_native_sdk import make_agent, run_args, setup

from agentmoss_deepagents.history import KEY, AgentMossState, empty_history
from agentmoss_deepagents.runtime import drain


class TestApplication:
    """Minimal trusted application contract, not part of the shipped plugin API."""

    __test__ = False

    def __init__(self):
        self.locks = {}

    @asynccontextmanager
    async def session(self, session_id):
        async with self.locks.setdefault(session_id, asyncio.Lock()):
            yield

    @staticmethod
    def user_input(body):
        if set(body) - {"text", "session_id"} or not isinstance(body.get("text"), str):
            raise ValueError("Only user text and application session are accepted")
        return {"messages": [("user", body["text"])]}


@pytest.mark.parametrize("streaming", [False, True])
async def test_cancelled_thread_leaf_finishes_before_application_reuses_session(tmp_path, monkeypatch, streaming):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = FilesystemBackend.read

    def slow(self, *args, **kwargs):
        started.set()
        assert release.wait(5)
        try:
            return original(self, *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(FilesystemBackend, "read", slow)
    (tmp_path / "a").write_text("before")
    runtime, _ = setup(tmp_path)
    graph = make_agent(runtime, tmp_path, [call("read_file", {"file_path": "/a"}, "read"), AIMessage(content="done")])
    inputs, cfg, ctx = run_args()
    app = TestApplication()
    session = ctx["moss_info"]["session_id"]
    next_admitted = asyncio.Event()

    async def consume():
        async with app.session(session):
            if streaming:
                async with aclosing(graph.astream(inputs, cfg, context=ctx, stream_mode="updates")) as stream:
                    async for _ in stream:
                        pass
            else:
                await graph.ainvoke(inputs, cfg, context=ctx)

    async def next_request():
        async with app.session(session):
            assert finished.is_set()
            next_admitted.set()
            next_graph = make_agent(runtime, tmp_path, [AIMessage(content="next")])
            i, c, x = run_args(session, "next")
            await next_graph.ainvoke(i, c, context=x)

    first = asyncio.create_task(consume())
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        first.cancel()  # Same cleanup path as a disconnected HTTP/SSE client.
        await asyncio.sleep(0)
        first.cancel()  # Repeated cancellation must not abandon the worker.
        second = asyncio.create_task(next_request())
        await asyncio.sleep(0.02)
        assert not first.done() and not next_admitted.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        assert finished.is_set() and next_admitted.is_set()
    finally:
        release.set()
        await drain(asyncio.gather(first, *([second] if second else []), return_exceptions=True))
        await runtime.aclose()


async def test_cancelled_actual_shell_finishes_before_request_cleanup(tmp_path):
    runtime, _ = setup(tmp_path)
    command = "printf started > started; sleep 0.15; printf finished > finished"
    graph = make_agent(runtime, tmp_path, [call("execute", {"command": command}, "shell"), AIMessage(content="done")])
    inputs, cfg, ctx = run_args()
    task = asyncio.create_task(graph.ainvoke(inputs, cfg, context=ctx))
    try:
        for _ in range(300):
            if (tmp_path / "started").exists():
                break
            await asyncio.sleep(0.005)
        assert (tmp_path / "started").exists()
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (tmp_path / "finished").read_text() == "finished"
    finally:
        await drain(asyncio.gather(task, return_exceptions=True))
        await runtime.aclose()


async def test_native_stream_early_break_explicit_close_then_next_request(tmp_path):
    runtime, _ = setup(tmp_path)
    graph = make_agent(runtime, tmp_path, [AIMessage(content="done")], checkpointer=InMemorySaver())
    inputs, cfg, ctx = run_args()
    app = TestApplication()
    try:
        async with app.session(ctx["moss_info"]["session_id"]):
            async with aclosing(graph.astream(inputs, cfg, context=ctx, stream_mode="updates")) as stream:
                async for _ in stream:
                    break
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
        i, c, x = run_args(ctx["moss_info"]["session_id"], "next")
        async with app.session(x["moss_info"]["session_id"]):
            result = await graph.ainvoke(i, c, context=x)
        assert result[KEY]["root"] == x["moss_info"]["session_id"]
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    "body",
    [
        {"text": "x", KEY: {}},
        {"text": "x", "moss_info": {}},
        {"text": "x", "update": {KEY: {}}},
        {"text": "x", "messages": []},
    ],
)
def test_application_whitelist_rejects_external_state_and_command_fields(body):
    with pytest.raises(ValueError):
        TestApplication.user_input(body)


async def test_wrong_root_checkpoint_is_rejected(tmp_path):
    from langgraph.graph import START, StateGraph

    saver = InMemorySaver()
    inputs, cfg, ctx = run_args()
    raw = StateGraph(AgentMossState).add_node("node", lambda s: {}).add_edge(START, "node").compile(checkpointer=saver)
    await raw.aupdate_state(cfg, {KEY: empty_history("other")})
    runtime, seen = setup(tmp_path)
    graph = make_agent(runtime, tmp_path, [AIMessage(content="done")], checkpointer=saver)
    try:
        with pytest.raises(ValueError, match="cross-root"):
            await graph.ainvoke(inputs, cfg, context=ctx)
        assert not seen
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("path", ["/../outside", "/link/outside", "~/outside"])
async def test_original_sdk_still_rejects_unsafe_paths(tmp_path, path):
    outside = tmp_path.parent / "outside"
    outside.write_text("original")
    (tmp_path / "link").symlink_to(tmp_path.parent)
    runtime, _ = setup(tmp_path)
    graph = make_agent(
        runtime,
        tmp_path,
        [
            call("edit_file", {"file_path": path, "old_string": "original", "new_string": "changed"}, "edit"),
            AIMessage(content="done"),
        ],
    )
    inputs, cfg, ctx = run_args()
    try:
        result = await graph.ainvoke(inputs, cfg, context=ctx)
        assert outside.read_text() == "original"
        assert all(e["status"] in {"executed_error", "unknown"} for e in result[KEY]["events"])
        assert any(getattr(m, "status", None) == "error" for m in result["messages"])
    finally:
        await runtime.aclose()
