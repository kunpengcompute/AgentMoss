"""Incomplete security checks must not cause physical tool side effects by default."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from models import call
from test_native_sdk import make_agent, run_args, setup

from agentmoss_deepagents.config import GateConfig
from agentmoss_deepagents.history import KEY


async def invoke(graph, sync, *args):
    inputs, config, context = run_args(*args)
    if sync:
        return await asyncio.to_thread(graph.invoke, inputs, config, context=context)
    return await graph.ainvoke(inputs, config, context=context)


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("source", ["constructor", "mapping", "environment", "legacy_file"])
async def test_default_gate_failure_prevents_physical_write(tmp_path, monkeypatch, sync, source):
    import os

    for key in list(os.environ):
        if key.startswith("DEEPAGENTS_GATE_"):
            monkeypatch.delenv(key)
    runtime, seen = setup(tmp_path, httpx.ConnectError("offline"))
    values = {"api_token": "secret-token"}
    if source == "mapping":
        runtime.config = GateConfig.from_mapping(values)
    elif source == "environment":
        monkeypatch.setenv("DEEPAGENTS_GATE_API_TOKEN", "secret-token")
        runtime.config = GateConfig.from_env()
    elif source == "legacy_file":
        path = tmp_path / "gate.json"
        path.write_text(json.dumps(values))
        runtime.config = GateConfig.load(path)
    graph = make_agent(
        runtime, tmp_path, [call("execute", {"command": "printf unsafe > marker"}, "write"), AIMessage(content="done")]
    )
    try:
        result = await invoke(graph, sync)
        assert not (tmp_path / "marker").exists()
        assert len(seen) == 1
        event = result[KEY]["events"][-1]
        assert (event["status"], event["decision"], event["source"]) == ("denied", "Deny", "fail_closed")
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("limit", ["actions", "turns", "bytes", "wire_bytes"])
@pytest.mark.parametrize("policy", ["default", "fail_open", "gate_deny"])
async def test_truncated_checkpoint_never_silently_loses_security_context(tmp_path, sync, limit, policy):
    deny = False

    def gate(request):
        return httpx.Response(200, json={"decision": "Deny" if deny else "Allow"})

    runtime, seen = setup(tmp_path, gate)
    overrides = {
        "actions": {"history_actions": 1},
        "turns": {"history_turns": 1},
        "bytes": {"history_bytes": 1500},
        "wire_bytes": {"history_bytes": 15000},
    }[limit]
    if policy != "default":
        overrides["fail_closed"] = False
    runtime.config = replace(runtime.config, **overrides)
    padding = "x" * 6000 if limit in {"bytes", "wire_bytes"} else "first"
    first_calls = [call("execute", {"command": f"printf '%s' '{padding}' > first"}, "first")]
    if limit == "actions":
        first_calls.append(call("execute", {"command": "printf second > second"}, "second"))
    saver = InMemorySaver()
    try:
        first_graph = make_agent(runtime, tmp_path, [*first_calls, AIMessage(content="done")], checkpointer=saver)
        first_result = await invoke(first_graph, sync, "bounded", "initial request")
        assert (tmp_path / "first").read_text() == padding
        assert len(seen) == len(first_calls)
        assert first_result[KEY]["truncated"] == (limit in {"actions", "bytes"})
        deny = policy == "gate_deny"
        # Build a fresh graph and context to force recovery from the saved checkpoint.
        graph = make_agent(
            runtime,
            tmp_path,
            [call("execute", {"command": "printf final > marker"}, "last"), AIMessage(content="done")],
            checkpointer=saver,
        )
        result = await invoke(graph, sync, "bounded", "next request")
        assert (tmp_path / "marker").exists() == (policy == "fail_open")
        event = result[KEY]["events"][-1]
        if policy == "default":
            assert len(seen) == len(first_calls)
            assert (event["status"], event["decision"], event["source"]) == ("denied", "Deny", "history_truncated")
        else:
            assert len(seen) == len(first_calls) + 1
            assert seen[-1]["metadata"]["history_truncated"] is True
            assert event["decision"] == ("Deny" if deny else "Allow")
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("sync", [False, True])
async def test_history_truncated_in_current_invocation_prevents_next_write(tmp_path, sync):
    runtime, seen = setup(tmp_path)
    runtime.config = replace(runtime.config, history_actions=1)
    graph = make_agent(
        runtime,
        tmp_path,
        [
            call("execute", {"command": "printf A >> marker"}, "first"),
            call("execute", {"command": "printf B >> marker"}, "second"),
            call("execute", {"command": "printf C >> marker"}, "third"),
            AIMessage(content="done"),
        ],
    )
    try:
        result = await invoke(graph, sync)
        assert (tmp_path / "marker").read_text() == "AB"
        assert len(seen) == 2
        assert result[KEY]["events"][-1]["source"] == "history_truncated"
    finally:
        await runtime.aclose()
