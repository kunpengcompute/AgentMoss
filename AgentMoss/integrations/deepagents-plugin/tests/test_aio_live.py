"""Opt-in real Gate/Moss/AIO validation; scripted models never call an LLM API."""

import asyncio
import os
from uuid import uuid4

import pytest
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, ToolMessage
from models import ScriptedModel, call
from test_native_sdk import run_args

from agentmoss_deepagents import DeepAgentsIntegration
from agentmoss_deepagents.history import KEY

pytestmark = pytest.mark.skipif(not os.getenv("AGENTMOSS_AIO_LIVE_URL"), reason="real AIO endpoint not configured")


@pytest.fixture
async def integration():
    value = await asyncio.to_thread(
        DeepAgentsIntegration.load,
        {
            "backend": "aio",
            "gate": {
                "gate_url": os.environ["AGENTMOSS_LIVE_GATE_URL"],
                "api_token_file": os.environ["AGENTMOSS_LIVE_GATE_TOKEN_FILE"],
                "fail_closed": True,
            },
            "aio": {
                "proxy_url": os.environ["AGENTMOSS_AIO_LIVE_URL"],
                "sandbox_id": os.environ["AGENTMOSS_AIO_LIVE_SANDBOX_ID"],
                "working_dir": "/home/gem",
                "default_timeout": 8,
                "hard_timeout": 8,
            },
        },
    )
    try:
        yield value
    finally:
        await value.aclose()


def graph(integration, messages, subagents=None):
    children = subagents or [{"name": "general-purpose", "description": "worker"}]
    children = [{**spec, "middleware": [integration.middleware]} for spec in children]
    return create_deep_agent(
        model=ScriptedModel(responses=messages),
        backend=integration.backend,
        context_schema=dict,
        middleware=[integration.middleware],
        subagents=children,
    )


async def invoke(agent, prompt="Run the requested sandbox validation command", *, sync=False):
    inputs, config, context = run_args(prompt=prompt)
    if sync:
        return await asyncio.to_thread(agent.invoke, inputs, config, context=context)
    return await agent.ainvoke(inputs, config, context=context)


async def test_live_aio_file_contract(integration):
    backend = integration.backend
    directory = "/home/gem/aio-plugin-" + uuid4().hex
    assert (await backend.aexecute("mkdir -p " + directory)).exit_code == 0
    path = directory + "/file.txt"
    assert (await backend.awrite(path, "first\nsecond\n")).error is None
    assert (await backend.awrite(path, "overwrite")).error is not None
    assert (await backend.aread(path)).file_data["content"].splitlines() == ["first", "second"]
    assert (await backend.adownload_files([path]))[0].content == b"first\nsecond\n"
    assert (await backend.aedit(path, "second", "updated")).error is None
    assert "updated" in (await backend.aread(path)).file_data["content"]
    assert any(item["path"] == path for item in (await backend.als(directory)).entries)
    assert any(item["path"] == path for item in (await backend.aglob("*.txt", directory)).matches)
    grep = await backend.agrep("updated", directory)
    assert grep.error is None and grep.matches
    binary = directory + "/binary.bin"
    payload = b"\x00\xff\x01binary"
    assert (await backend.aupload_files([(binary, payload)]))[0].error is None
    assert (await backend.adownload_files([binary]))[0].content == payload
    assert (await backend.adelete(directory)).error is None


@pytest.mark.parametrize("sync,command,code", [(False, "printf live-ok", 0), (True, 'sh -c "exit 7"', 7)])
async def test_live_graph_real_exit_codes(integration, sync, command, code):
    result = await invoke(
        graph(integration, [call("execute", {"command": command}, "live"), AIMessage(content="done")]), sync=sync
    )
    event = next(e for e in result[KEY]["events"] if e["tool_call_id"] == "live")
    assert event["decision"] == "Allow"
    assert event["status"] == ("executed_success" if code == 0 else "executed_error")
    message = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "live")
    assert message.artifact["exit_code"] == code


async def test_live_external_gate_denies_before_aio(integration, monkeypatch):
    invoked = []
    original = integration.backend.execute

    def track(*args, **kwargs):
        invoked.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(integration.backend, "execute", track)
    # Unreachable sensitive access remains harmless even if an assertion fails.
    command = "if false; then cat /etc/shadow; fi; printf should-not-submit"
    result = await invoke(
        graph(integration, [call("execute", {"command": command}, "outer"), AIMessage(content="done")])
    )
    event = next(e for e in result[KEY]["events"] if e["tool_call_id"] == "outer")
    assert event["decision"] == "Deny" and event["status"] == "denied"
    assert invoked == []


async def test_live_inner_moss_denies_script_after_external_allow(integration):
    path = "/home/gem/aio-inner-" + uuid4().hex + ".py"
    # Read-only probe discards all output even if the inner guard unexpectedly permits execution.
    content = b'import os\nos.system("cat /etc/shadow > /dev/null 2>&1")\nprint("inner-marker")\n'
    assert (await integration.backend.aupload_files([(path, content)]))[0].error is None
    result = await invoke(
        graph(integration, [call("execute", {"command": "python3 " + path}, "inner"), AIMessage(content="done")])
    )
    event = next(e for e in result[KEY]["events"] if e["tool_call_id"] == "inner")
    assert event["decision"] == "Allow" and event["status"] == "executed_error"
    message = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "inner")
    assert message.artifact["exit_code"] == 126
    assert "AGENTMOSS_DENIED" in message.content


async def test_live_subagent_keeps_shared_gate_history(integration):
    worker = ScriptedModel(
        responses=[call("execute", {"command": "printf child-ok"}, "child"), AIMessage(content="child done")]
    )
    delegate = call("task", {"description": "Print child-ok in the sandbox", "subagent_type": "worker"}, "delegate")
    agent = graph(
        integration,
        [delegate, call("execute", {"command": "printf parent-ok"}, "parent"), AIMessage(content="done")],
        subagents=[{"name": "worker", "description": "worker", "model": worker}],
    )
    result = await invoke(agent)
    events = {e["tool_call_id"]: e for e in result[KEY]["events"]}
    for name in ["child", "parent"]:
        assert events[name]["decision"] == "Allow"
        assert events[name]["status"] == "executed_success"

    import httpx

    session_id = "deepagents:" + result[KEY]["root"]
    async with httpx.AsyncClient(trust_env=False) as http:
        audit = await http.get(
            os.environ["AGENTMOSS_LIVE_GATE_URL"] + "/api/v1/sessions/" + session_id + "/traces",
            headers={"Authorization": "Bearer " + integration.runtime.client.token},
        )
    audit.raise_for_status()
    assert len(audit.json()["tool_calls"]) >= 3


async def test_live_hard_timeout_stops_session(integration):
    from agentmoss_deepagents.aio_errors import AIOExecutionUncertain, AIOUnavailable

    with pytest.raises(AIOExecutionUncertain):
        await integration.backend.aexecute("sleep 4", timeout=1)
    with pytest.raises(AIOUnavailable):
        await integration.backend.aexecute("printf must-not-submit")


async def test_live_invalid_gate_token_fails_closed(integration, monkeypatch):
    from dataclasses import replace

    from agentmoss_deepagents import AgentMossMiddleware, GateRuntime

    gate = replace(integration.config.gate, api_token="invalid-test-token", api_token_file=None)
    runtime = GateRuntime.load(gate)
    original_runtime, original_middleware = integration.runtime, integration.middleware
    calls = []
    monkeypatch.setattr(integration.backend, "execute", lambda *args, **kwargs: calls.append(args))
    integration.runtime, integration.middleware = runtime, AgentMossMiddleware(runtime, integration.backend)
    try:
        result = await invoke(
            graph(
                integration,
                [call("execute", {"command": "printf auth-bypass"}, "bad-token"), AIMessage(content="done")],
            )
        )
        event = next(e for e in result[KEY]["events"] if e["tool_call_id"] == "bad-token")
        assert event["decision"] == "Deny" and event["source"] == "fail_closed"
        assert event["status"] == "denied" and calls == []
    finally:
        await runtime.aclose()
        integration.runtime, integration.middleware = original_runtime, original_middleware


async def test_live_close_removes_shell_session(integration):
    import httpx
    from agent_sandbox import Sandbox

    session = integration.backend.session_id
    await integration.aclose()
    with httpx.Client(timeout=20, trust_env=False) as http:
        client = Sandbox(
            base_url=os.environ["AGENTMOSS_AIO_LIVE_URL"],
            headers={"Host": integration.config.aio.host},
            httpx_client=http,
        )
        result = client.shell.list_sessions(request_options={"max_retries": 0})
        assert result.success and session not in result.data.sessions
