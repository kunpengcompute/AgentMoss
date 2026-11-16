"""Nested graphs must preserve Gate checks without holding each other off forever."""

import asyncio
import json
import subprocess
import sys
import threading
from contextvars import copy_context
from pathlib import Path

import httpx
import pytest
from deepagents import create_deep_agent
from deepagents.backends.local_shell import LocalShellBackend
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage
from models import ScriptedModel, call
from test_native_sdk import run_args, setup

from agentmoss_deepagents import AgentMossMiddleware
from agentmoss_deepagents.history import KEY


async def delegated_graph(root, sync, deny, depth, cancel):
    started, release = threading.Event(), threading.Event()

    def verdict(request):
        payload = json.loads(request.content)
        blocked = deny and payload["metadata"]["tool_call_id"] == "second"
        return httpx.Response(200, json={"decision": "Deny" if blocked else "Allow"})

    runtime, seen = setup(root, verdict)
    backend = LocalShellBackend(root_dir=root)
    middleware = AgentMossMiddleware(runtime, backend)

    @tool
    def write_marker(letter: str) -> str:
        """Append a marker to verify ordered effects."""
        if cancel:
            started.set()
            assert release.wait(5)
        with (root / "marker").open("a") as stream:
            stream.write(letter)
        return "written"

    def agent(responses, tools):
        return create_deep_agent(
            model=ScriptedModel(responses=responses),
            backend=backend,
            context_schema=dict,
            middleware=[middleware],
            tools=tools,
            subagents=[{"name": "general-purpose", "description": "worker", "middleware": [middleware]}],
        )

    calls = call("write_marker", {"letter": "A"}, "first")
    if not cancel:
        calls.tool_calls.append(call("write_marker", {"letter": "B"}, "second").tool_calls[0])
    child = agent([calls, AIMessage(content="child done")], [write_marker])

    def wrap(target, index):
        @tool
        def delegate(runtime: ToolRuntime[dict]) -> str:
            """Delegate to a separately compiled and instrumented graph."""
            result = target.invoke({"messages": [("user", "perform step")]}, context=runtime.context)
            return result["messages"][-1].text

        async def adelegate(runtime: ToolRuntime[dict]) -> str:
            result = await target.ainvoke({"messages": [("user", "perform step")]}, context=runtime.context)
            return result["messages"][-1].text

        delegate.coroutine = adelegate
        return agent([call("delegate", {}, f"delegate-{index}"), AIMessage(content="done")], [delegate])

    for index in range(depth):
        child = wrap(child, index)
    inputs, config, context = run_args("nested-test", "perform step")
    task = asyncio.create_task(
        asyncio.to_thread(child.invoke, inputs, config, context=context)
        if sync
        else child.ainvoke(inputs, config, context=context)
    )
    try:
        if cancel:
            assert await asyncio.to_thread(started.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "Cancellation must drain the admitted nested effect"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not context["moss_info"]["_agentmoss_run"].history.lock.locked()
            assert (root / "marker").read_text() == "A"
        else:
            result = await task
            assert (root / "marker").read_text() == ("A" if deny else "AB")
            assert len(seen) == depth + 2
            second = next(row for row in seen if row["metadata"]["tool_call_id"] == "second")
            assert next(row for row in second["metadata"]["history_statuses"]
                        if row["tool_call_id"] == "first")["status"] == "executed_success"
            assert next(row for row in result[KEY]["events"]
                        if row["tool_call_id"] == "second")["status"] == ("denied" if deny else "executed_success")
    finally:
        release.set()
        await runtime.aclose()


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("deny", [False, True])
@pytest.mark.parametrize("depth", [1, 2])
def test_nested_graph_keeps_checks_and_sibling_order(tmp_path, sync, deny, depth):
    # Isolate the pre-fix deadlock so even asyncio.run() cleanup cannot hang pytest.
    try:
        result = subprocess.run(
            [sys.executable, __file__, str(tmp_path), str(int(sync)), str(int(deny)), str(depth), "0"],
            capture_output=True, text=True, timeout=8,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("Nested instrumented tool calls deadlocked")
    assert result.returncode == 0, result.stdout + result.stderr


def test_nested_cancellation_drains_then_releases_locks(tmp_path):
    try:
        result = subprocess.run(
            [sys.executable, __file__, str(tmp_path), "0", "0", "2", "1"],
            capture_output=True, text=True, timeout=8,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("Nested tool cancellation deadlocked")
    assert result.returncode == 0, result.stdout + result.stderr


async def test_finished_parent_cannot_admit_a_delayed_child(tmp_path):
    from test_native_sdk import make_agent

    saved = []

    @tool
    async def save_context() -> str:
        """Capture the context a delayed background child would inherit."""
        saved.append(copy_context())
        return "saved"

    runtime, seen = setup(tmp_path)
    parent = make_agent(runtime, tmp_path, [call("save_context", {}, "parent"), AIMessage(content="done")],
                        tools=[save_context])
    child = make_agent(runtime, tmp_path, [call("execute", {"command": "touch late"}, "late"),
                                        AIMessage(content="done")])
    inputs, config, context = run_args("late-child")
    try:
        await parent.ainvoke(inputs, config, context=context)
        result = await asyncio.create_task(child.ainvoke(inputs, config, context=context), context=saved[0])
        assert not (tmp_path / "late").exists(), "A completed parent admitted a delayed child"
        assert len(seen) == 1
        assert any("finished" in str(m.content).lower() for m in result["messages"])
    finally:
        await runtime.aclose()


if __name__ == "__main__":
    asyncio.run(delegated_graph(Path(sys.argv[1]), bool(int(sys.argv[2])), bool(int(sys.argv[3])),
                               int(sys.argv[4]), bool(int(sys.argv[5]))))
