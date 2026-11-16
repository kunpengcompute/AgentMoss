"""User-visible regressions from the PR #8 documentation/functionality review."""

import asyncio
import json
import shlex

import httpx
import pytest
from deepagents.backends.local_shell import LocalShellBackend
from langchain_core.messages import AIMessage, ToolMessage
from models import call
from test_aio_backend import backend as backend
from test_aio_backend import client as client
from test_aio_backend import disk_backend as disk_backend
from test_aio_backend import response
from test_aio_backend import sdk_backend as sdk_backend
from test_native_sdk import make_agent, run_args, setup

from agentmoss_deepagents.history import KEY


async def invoke(graph, *, sync=False):
    inputs, config, context = run_args()
    if sync:
        return await asyncio.to_thread(graph.invoke, inputs, config, context=context)
    return await graph.ainvoke(inputs, config, context=context)


@pytest.mark.parametrize("mode", ["local", "aio"])
@pytest.mark.parametrize("sync", [False, True])
async def test_recursive_delete_policy_preserves_nonempty_directory(tmp_path, disk_backend, mode, sync):
    target = tmp_path / "目录 dir'one"
    target.mkdir()
    child = target / "keep.txt"
    child.write_text("keep")

    def deny_recursive(request):
        command = json.loads(request.content)["command"]
        return httpx.Response(200, json={"decision": "Deny" if "-r" in shlex.split(command) else "Allow"})

    runtime, seen = setup(tmp_path, deny_recursive)
    execution_backend = disk_backend if mode == "aio" else LocalShellBackend(root_dir=tmp_path, virtual_mode=False)
    graph = make_agent(
        runtime,
        tmp_path,
        [call("delete", {"file_path": str(target)}, "delete"), AIMessage(content="done")],
        backend=execution_backend,
    )
    try:
        result = await invoke(graph, sync=sync)
        assert child.exists(), "Gate did not see that delete recursively removes directory contents"
        assert child.read_text() == "keep"
        assert shlex.split(seen[0]["command"]) == ["rm", "-r", "--", str(target)]
        assert result[KEY]["events"][0]["status"] == "denied"
        assert disk_backend.client.shell.calls == []
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("offset,content,span", [(0, "\n\n", "1-2"), (8, "\n\n", "9-10"), (1, "\n", "2-2")])
async def test_blank_read_page_is_a_source_window_not_an_empty_file(tmp_path, sdk_backend, offset, content, span):
    execution_backend, _, replies = sdk_backend
    replies.append(httpx.Response(200, json={"success": True, "data": {"file": "/file.txt", "content": content}}))
    runtime, _ = setup(tmp_path)
    graph = make_agent(
        runtime,
        tmp_path,
        [
            call("read_file", {"file_path": "/file.txt", "offset": offset, "limit": 2}, "read"),
            AIMessage(content="done"),
        ],
        backend=execution_backend,
    )
    try:
        result = await invoke(graph)
        message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert "empty contents" not in message.content
        assert f"lines {span}" in message.content
        assert message.status == "success"
    finally:
        await runtime.aclose()


def test_read_past_eof_does_not_claim_the_whole_file_is_empty(sdk_backend):
    execution_backend, _, replies = sdk_backend
    replies.append(httpx.Response(200, json={"success": True, "data": {"file": "/file.txt", "content": ""}}))
    result = execution_backend.read("/file.txt", offset=9, limit=2)
    assert result.error and "offset" in result.error.lower()
    assert execution_backend.status()["state"] == "ready"


@pytest.mark.parametrize("fault", ["running", "timeout"])
@pytest.mark.parametrize("sync", [False, True])
async def test_glob_unknown_is_preserved_without_contaminating_next_rejection(tmp_path, backend, client, fault, sync):
    client.shell.answer = (
        response(status="running", exit_code=None, output="") if fault == "running" else httpx.ReadTimeout("lost")
    )
    runtime, _ = setup(tmp_path)
    graph = make_agent(
        runtime,
        tmp_path,
        [
            call("glob", {"pattern": "*.txt"}, "unknown"),
            call("glob", {"pattern": "*.py"}, "rejected"),
            AIMessage(content="done"),
        ],
        backend=backend,
    )
    try:
        result = await invoke(graph, sync=sync)
        events = {event["tool_call_id"]: event for event in result[KEY]["events"]}
        assert events["unknown"]["status"] == "unknown"
        assert events["rejected"]["status"] == "executed_error"
        assert len(client.shell.calls) == 1
        assert backend.status()["state"] == "uncertain"
    finally:
        await runtime.aclose()
