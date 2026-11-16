"""Integration identity, transport and lifecycle regressions retained from v2."""

import json
from contextlib import closing

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from models import call
from test_integration import build, config, context_for, done, setup

from agentmoss_deepagents.history import KEY


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize(
    "response",
    [
        httpx.ConnectError("offline"),
        httpx.ReadTimeout("late"),
        httpx.Response(401),
        httpx.Response(500),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"decision": "Maybe"}),
        httpx.Response(200, text="bad json"),
    ],
)
async def test_gateway_faults_never_report_allow(tmp_path, closed, response):
    from dataclasses import replace

    runtime, _ = setup(tmp_path, response)
    runtime.config = replace(runtime.config, fail_closed=closed)
    result = await build(runtime, tmp_path, [call("execute", {"command": "pwd"}, "pwd"), done()]).ainvoke(
        (_inputs := {"messages": [("user", "pwd")]}), (_cfg := config()), context=context_for(_inputs, _cfg)
    )
    event = result[KEY]["events"][0]
    assert event["decision"] == ("Deny" if closed else "Bypass")
    assert event["source"] == ("fail_closed" if closed else "bypass")
    assert event["status"] == ("denied" if closed else "executed_success")


async def test_gate_valid_deny_always_prevents_new_file(tmp_path):
    runtime, seen = setup(tmp_path, httpx.Response(200, json={"decision": "Deny", "reason": "fixture"}))
    result = await build(
        runtime, tmp_path, [call("write_file", {"file_path": "/new", "content": "x"}, "write"), done()]
    ).ainvoke((_inputs := {"messages": [("user", "create")]}), (_cfg := config()), context=context_for(_inputs, _cfg))
    assert len(seen) == 1 and (not (tmp_path / "new").exists())
    assert result[KEY]["events"][0]["status"] == "denied"


@pytest.mark.parametrize("streaming", [False, True])
def test_real_synchronous_python_and_managed_stream(tmp_path, streaming):
    (tmp_path / "a").write_text("before")
    runtime, seen = setup(tmp_path)
    agent = build(
        runtime,
        tmp_path,
        [
            call("read_file", {"file_path": "/a"}, "r"),
            call("edit_file", {"file_path": "/a", "old_string": "before", "new_string": "after"}, "e"),
            done(),
        ],
        checkpointer=InMemorySaver(),
    )
    if streaming:
        with closing(
            agent.stream(
                {"messages": [("user", "read edit")]},
                (cfg := config()),
                context=context_for({"messages": [("user", "read edit")]}, cfg),
                stream_mode="updates",
            )
        ) as updates:
            assert list(updates)
    else:
        result = agent.invoke(
            (inputs := {"messages": [("user", "read edit")]}), (cfg := config()), context=context_for(inputs, cfg)
        )
        assert result[KEY]["events"][-1]["status"] == "executed_success"
    assert (tmp_path / "a").read_text() == "after"
    assert len(seen) == 2 and seen[1]["action_history"]
    runtime.client.close()


async def test_denied_shell_and_user_prompt_do_not_copy_known_credentials(tmp_path):
    runtime, _ = setup(tmp_path, httpx.Response(200, json={"decision": "Deny"}))
    secret = runtime.client.token
    model = [call("execute", {"command": "echo " + secret + " sk-model-test-credential"}, "bad"), done()]
    result = await build(runtime, tmp_path, model).ainvoke(
        (_inputs := {"messages": [("user", "test " + secret + " sk-model-test-credential")]}),
        (_cfg := config()),
        context=context_for(_inputs, _cfg),
    )
    state = json.dumps(result[KEY])
    assert secret not in state and "sk-model-test-credential" not in state
    assert result[KEY]["events"][0]["status"] == "denied"
