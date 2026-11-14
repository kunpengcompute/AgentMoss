"""Exercise native approvals with a fresh application context on every invocation."""

import asyncio

import httpx
import pytest
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from models import ScriptedModel, call
from test_native_sdk import make_agent, run_args, setup

from agentmoss_deepagents.history import KEY


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("decision", ["approve", "edit", "reject", "gate_deny", "edit_deny"])
async def test_native_approval_restores_context_and_checks_final_arguments(tmp_path, sync, decision):
    @tool
    def count_once() -> str:
        """Record a completed side effect preceding the approval."""
        with (tmp_path / "count").open("a") as stream:
            stream.write("once\n")
        return "counted"

    def gate_response(request):
        import json

        payload = json.loads(request.content)
        denied = (decision == "gate_deny" and payload["tool_name"] == "bash") or (
            decision == "edit_deny" and "edited" in payload["command"]
        )
        return httpx.Response(200, json={"decision": "Deny" if denied else "Allow"})

    runtime, seen = setup(tmp_path, gate_response)
    graph = make_agent(
        runtime,
        tmp_path,
        [
            call("count_once", {}, "completed"),
            call("execute", {"command": "printf approved >> marker"}, "approval"),
            AIMessage(content="done"),
        ],
        tools=[count_once],
        checkpointer=InMemorySaver(),
        interrupt_on={"execute": True},
    )
    inputs, config, context = run_args("approval", "create marker")

    async def invoke(value, ctx):
        if sync:
            return await asyncio.to_thread(graph.invoke, value, config, context=ctx)
        return await graph.ainvoke(value, config, context=ctx)

    try:
        first = await invoke(inputs, context)
        assert first["__interrupt__"] and not (tmp_path / "marker").exists()
        approved = {"type": "approve" if decision == "gate_deny" else "edit" if decision == "edit_deny" else decision}
        if decision in {"edit", "edit_deny"}:
            approved["edited_action"] = {"name": "execute", "args": {"command": "printf edited >> marker"}}
        fresh = {"moss_info": {"session_id": "approval", "user_prompt": "create marker"}}
        second = await invoke(Command(resume={"decisions": [approved]}), fresh)
        assert (tmp_path / "count").read_text() == "once\n"
        assert len(second[KEY]["turns"]) == 1
        if decision in {"reject", "gate_deny", "edit_deny"}:
            assert not (tmp_path / "marker").exists()
        else:
            assert (tmp_path / "marker").read_text() == ("edited" if decision == "edit" else "approved")
        shell_checks = [payload for payload in seen if payload["tool_name"] == "bash"]
        assert len(shell_checks) == (0 if decision == "reject" else 1)
        if decision in {"edit", "edit_deny"}:
            assert shell_checks[0]["command"] == "printf edited >> marker"
        # A second, empty continuation must not rerun either completed effect.
        await invoke(None, {"moss_info": {"session_id": "approval", "user_prompt": "create marker"}})
        assert (tmp_path / "count").read_text() == "once\n"
    finally:
        await runtime.aclose()


async def test_child_approval_resumes_delegation_without_repeating_completed_tools(tmp_path):
    @tool
    def count_child() -> str:
        """Count completed child effects preceding its approval."""
        with (tmp_path / "child-count").open("a") as stream:
            stream.write("once\n")
        return "counted"

    runtime, seen = setup(tmp_path)
    worker = ScriptedModel(
        responses=[
            call("count_child", {}, "child-completed"),
            call("execute", {"command": "printf child >> marker"}, "child-approved"),
            AIMessage(content="child done"),
        ]
    )
    graph = make_agent(
        runtime,
        tmp_path,
        [
            call("task", {"description": "create child marker", "subagent_type": "worker"}, "delegate"),
            AIMessage(content="done"),
        ],
        checkpointer=InMemorySaver(),
        subagents=[
            {
                "name": "worker",
                "description": "worker",
                "model": worker,
                "interrupt_on": {"execute": True},
                "tools": [count_child],
            }
        ],
    )
    inputs, config, context = run_args("child-approval", "create marker")
    try:
        first = await graph.ainvoke(inputs, config, context=context)
        assert first["__interrupt__"] and not (tmp_path / "marker").exists()
        result = await graph.ainvoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config,
            context={"moss_info": {"session_id": "child-approval", "user_prompt": "create marker"}},
        )
        assert (tmp_path / "marker").read_text() == "child"
        assert (tmp_path / "child-count").read_text() == "once\n"
        assert len(result[KEY]["turns"]) == 1
        assert len([payload for payload in seen if payload["tool_name"] == "bash"]) == 1
        assert {event["status"] for event in result[KEY]["events"]} == {"executed_success"}
    finally:
        await runtime.aclose()


async def test_per_call_approval_before_gate_restores_and_checks_edited_arguments(tmp_path):
    runtime, seen = setup(tmp_path)
    graph = make_agent(
        runtime,
        tmp_path,
        [
            call("execute", {"command": "printf original >> marker"}, "approval"),
            AIMessage(content="done"),
        ],
        checkpointer=InMemorySaver(),
        middleware=[
            HumanInTheLoopMiddleware(interrupt_on={"execute": True}, interrupt_mode="per_call"),
        ],
    )
    inputs, config, context = run_args("per-call", "create marker")
    try:
        first = await graph.ainvoke(inputs, config, context=context)
        assert first["__interrupt__"] and seen == []
        await graph.ainvoke(
            Command(
                resume={
                    first["__interrupt__"][0].id: {
                        "type": "edit",
                        "edited_action": {"name": "execute", "args": {"command": "printf edited >> marker"}},
                    }
                }
            ),
            config,
            context={"moss_info": {"session_id": "per-call", "user_prompt": "create marker"}},
        )
        assert (tmp_path / "marker").read_text() == "edited"
        assert [payload["command"] for payload in seen] == ["printf edited >> marker"]
    finally:
        await runtime.aclose()
