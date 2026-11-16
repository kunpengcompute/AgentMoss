"""Only the current tool call's explanation may influence Gate policy."""

import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessage
from models import call
from test_native_sdk import make_agent, run_args, setup

from agentmoss_deepagents.history import KEY


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize(
    "content,expected",
    [
        ("Update the existing file", "Update the existing file"),
        ([{"type": "text", "text": "Update the existing file"}], "Update the existing file"),
        (["Update the ", {"type": "text", "text": "existing file"}], "Update the existing file"),
        ("", ""),
        ([], ""),
        ([{"type": "reasoning", "reasoning": "Create a new file"}], ""),
    ],
)
async def test_current_description_does_not_inherit_creation_exemption(tmp_path, sync, content, expected):
    from agent_moss.engine.logic_rules import LogicRulesChecker
    from agent_moss.profiles import get_profile

    target = tmp_path / "existing.txt"
    target.write_text("before")
    checker = LogicRulesChecker(get_profile("linux"))

    def check_write_before_read(request):
        payload = json.loads(request.content)
        action = {"action_type": payload["tool_name"], "action_detail": payload["command"]}
        verdict = checker._check_read_before_write(payload["action_history"], action, payload["description"])
        return httpx.Response(200, json={"decision": "Deny" if verdict.hit else "Allow"})

    runtime, seen = setup(tmp_path, check_write_before_read)
    previous = call("execute", {"command": "printf ready"}, "previous")
    previous.content = "Create a new file"
    current = call(
        "edit_file", {"file_path": "/existing.txt", "old_string": "before", "new_string": "after"}, "current"
    )
    current.content = content
    graph = make_agent(runtime, tmp_path, [previous, current, AIMessage(content="done")])
    inputs, config, context = run_args()
    try:
        result = (
            await asyncio.to_thread(graph.invoke, inputs, config, context=context)
            if sync
            else await graph.ainvoke(inputs, config, context=context)
        )
        assert target.read_text() == "before", "An old creation explanation exempted an unread existing-file edit"
        assert seen[-1]["description"] == expected
        event = next(event for event in result[KEY]["events"] if event["tool_call_id"] == "current")
        assert event["decision"] == "Deny" and event["status"] == "denied"
    finally:
        await runtime.aclose()
