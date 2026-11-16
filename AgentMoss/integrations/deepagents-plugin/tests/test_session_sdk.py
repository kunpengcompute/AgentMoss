"""Check execution identity against actual pinned SDK graphs, including retries."""

from contextlib import nullcontext

import pytest
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.tools import tool
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import RetryPolicy
from models import ScriptedModel, call
from test_native_sdk import make_agent, run_args, setup

from agentmoss_deepagents.history import KEY, make_event_id


class CaptureExecution(AgentMiddleware):
    def __init__(self):
        self.calls = []

    def capture(self, request):
        info = request.runtime.execution_info
        assert info is not None and info.checkpoint_ns
        self.calls.append((info.checkpoint_ns, request.tool_call["id"]))

    def wrap_tool_call(self, request, handler):
        self.capture(request)
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        self.capture(request)
        return await handler(request)


def graph_with_capture(runtime, tmp_path, capture, saver, mode="main"):
    action = call("execute", {"command": "printf sdk"}, "same")
    children = []
    if mode != "main":
        for name in ("one", "two"):
            children.append(
                {
                    "name": name,
                    "description": name,
                    "middleware": [capture],
                    "model": ScriptedModel(responses=[action.model_copy(deep=True), AIMessage(content="done")]),
                    **({"mode": "fork"} if mode == "fork" else {}),
                }
            )
        action = call("task", {"description": "first", "subagent_type": "one"}, "task-one")
        action.tool_calls.append(
            {
                "name": "task",
                "args": {"description": "second", "subagent_type": "two"},
                "id": "task-two",
                "type": "tool_call",
            }
        )
    with pytest.warns(Warning) if mode == "fork" else nullcontext():
        return make_agent(
            runtime,
            tmp_path,
            [action, AIMessage(content="done")],
            middleware=[capture],
            subagents=children,
            checkpointer=saver,
        )


@pytest.mark.parametrize("checkpointed", [False, True])
def test_sync_sdk_exposes_stable_execution_identity(tmp_path, checkpointed):
    import asyncio

    runtime, _ = setup(tmp_path)
    capture = CaptureExecution()
    graph = graph_with_capture(runtime, tmp_path, capture, InMemorySaver() if checkpointed else None)
    inputs, config, context = run_args()
    try:
        graph.invoke(inputs, config, context=context)
        assert len(capture.calls) == 1
        namespace, identifier = capture.calls[0]
        assert make_event_id("chat", namespace, identifier) == make_event_id("chat", namespace, identifier)
    finally:
        asyncio.run(runtime.aclose())


@pytest.mark.parametrize("checkpointed", [False, True])
@pytest.mark.parametrize("mode", ["main", "isolated", "fork"])
async def test_async_sdk_distinguishes_same_ids_in_parallel_graphs(tmp_path, checkpointed, mode):
    runtime, _ = setup(tmp_path)
    capture = CaptureExecution()
    graph = graph_with_capture(runtime, tmp_path, capture, InMemorySaver() if checkpointed else None, mode)
    inputs, config, context = run_args()
    try:
        result = await graph.ainvoke(inputs, config, context=context)
        assert result[KEY]["schema"] == 5
        entries = [e for e in result[KEY]["events"] if e["tool_call_id"] == "same"]
        assert len(entries) == (1 if mode == "main" else 2)
        assert len({e["event_id"] for e in entries}) == len(entries)
        assert all(e["status"] == "executed_success" for e in entries)
        assert len(result[KEY]["turns"]) == 1
        calls = [(namespace, identifier) for namespace, identifier in capture.calls if identifier == "same"]
        assert len(calls) == (1 if mode == "main" else 2)
        assert len({namespace for namespace, _ in calls}) == len(calls)
        assert len({make_event_id("chat", namespace, identifier) for namespace, identifier in calls}) == len(calls)
    finally:
        await runtime.aclose()


async def test_actual_sdk_node_retry_keeps_execution_identity():
    positions = []

    @tool
    def counter() -> str:
        """Return a small result without external effects."""
        return "ok"

    async def capture(request, handler):
        positions.append((request.runtime.execution_info.checkpoint_ns, request.tool_call["id"]))
        return await handler(request)

    tools = ToolNode([counter], awrap_tool_call=capture)

    async def flaky_node(state, config):
        result = await tools.ainvoke(state, config)
        if len(positions) == 1:
            raise RuntimeError("retry the SDK node")
        return result

    builder = StateGraph(AgentState)
    builder.add_node(
        "tools",
        flaky_node,
        retry_policy=RetryPolicy(max_attempts=2, initial_interval=0.001, jitter=False, retry_on=RuntimeError),
    )
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    result = await builder.compile().ainvoke({"messages": [call("counter", {}, "same")]})
    assert result["messages"][-1].content == "ok"
    assert len(positions) == 2 and positions[0] == positions[1]
    assert make_event_id("chat", *positions[0]) == make_event_id("chat", *positions[1])


@pytest.mark.parametrize("outcome", ["success", "unknown"])
@pytest.mark.parametrize("padding_bytes", [0, 20_000])
async def test_plugin_does_not_repeat_a_physical_tool_when_the_sdk_node_retries(tmp_path, outcome, padding_bytes):
    from deepagents.backends.local_shell import LocalShellBackend
    from langgraph.runtime import Runtime

    from agentmoss_deepagents import AgentMossMiddleware
    from agentmoss_deepagents.history import AgentMossState

    counter_file = tmp_path / "count"

    @tool
    def physical_counter(padding: str) -> str:
        """Increment a disposable file."""
        count = int(counter_file.read_text()) if counter_file.exists() else 0
        counter_file.write_text(str(count + 1))
        if outcome == "unknown":
            raise RuntimeError("partial tool failure")
        return "incremented"

    runtime, seen = setup(tmp_path)
    middleware = AgentMossMiddleware(runtime, LocalShellBackend(root_dir=tmp_path))
    tools = ToolNode([physical_counter], awrap_tool_call=middleware.awrap_tool_call)
    attempts = 0

    def initialize(state, runtime: Runtime[dict]):
        return middleware.before_agent(state, runtime)

    async def flaky_node(state, config):
        nonlocal attempts
        attempts += 1
        result = await tools.ainvoke(state, config)
        if attempts == 1:
            raise RuntimeError("retry after the physical side effect")
        return result

    builder = StateGraph(AgentMossState, context_schema=dict)
    builder.add_node("initialize", initialize)
    builder.add_node(
        "tools",
        flaky_node,
        retry_policy=RetryPolicy(max_attempts=2, initial_interval=0.001, jitter=False, retry_on=RuntimeError),
    )
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "tools")
    builder.add_edge("tools", END)
    _, config, context = run_args()
    try:
        result = await builder.compile().ainvoke(
            {"messages": [call("physical_counter", {"padding": "x" * padding_bytes}, "same")]}, config, context=context
        )
        assert attempts == 2 and counter_file.read_text() == "1" and len(seen) == 1
        if padding_bytes:
            # The bounded checkpoint may drop a whole event; retries still must not replay it.
            assert result[KEY]["events"] == [] and result[KEY]["truncated"] is True
        else:
            assert len(result[KEY]["events"]) == 1
            assert result[KEY]["events"][0]["status"] == ("executed_success" if outcome == "success" else "unknown")
            assert result[KEY]["events"][0]["event_id"]
        assert result["messages"][-1].status == "error"
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("missing", ["execution_info", "checkpoint_ns", "tool_call_id"])
async def test_missing_sdk_identity_never_bypasses_gate_or_executes(tmp_path, missing):
    from dataclasses import replace

    class WithoutIdentity(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            if missing == "tool_call_id":
                return await handler(replace(request, tool_call={**request.tool_call, "id": ""}))
            execution_info = (
                None if missing == "execution_info" else replace(request.runtime.execution_info, checkpoint_ns="")
            )
            return await handler(replace(request, runtime=replace(request.runtime, execution_info=execution_info)))

    runtime, seen = setup(tmp_path)
    graph = make_agent(
        runtime,
        tmp_path,
        [call("execute", {"command": "printf wrong > should-not-exist"}, "same"), AIMessage(content="done")],
        middleware=[WithoutIdentity()],
    )
    inputs, config, context = run_args()
    try:
        result = await graph.ainvoke(inputs, config, context=context)
        assert seen == [] and not (tmp_path / "should-not-exist").exists()
        assert any(getattr(message, "status", None) == "error" for message in result["messages"])
    finally:
        await runtime.aclose()


async def test_instrumented_nested_task_shares_one_root_turn_and_history(tmp_path):
    from deepagents import create_deep_agent
    from deepagents.backends.local_shell import LocalShellBackend

    from agentmoss_deepagents import AgentMossMiddleware

    runtime, seen = setup(tmp_path)
    backend = LocalShellBackend(root_dir=tmp_path)
    middleware = AgentMossMiddleware(runtime, backend)
    child = create_deep_agent(
        model=ScriptedModel(
            responses=[
                call("task", {"description": "nested work", "subagent_type": "grandchild"}, "nested-task"),
                call("execute", {"command": "printf child > child-marker"}, "child-after"),
                AIMessage(content="child done"),
            ]
        ),
        backend=backend,
        context_schema=dict,
        middleware=[middleware],
        subagents=[
            {
                "name": "grandchild",
                "description": "grandchild",
                "middleware": [middleware],
                "model": ScriptedModel(
                    responses=[
                        call("execute", {"command": "printf grandchild > grandchild-marker"}, "grandchild-tool"),
                        AIMessage(content="grandchild done"),
                    ]
                ),
            },
            {"name": "general-purpose", "description": "general", "middleware": [middleware]},
        ],
    )
    graph = make_agent(
        runtime,
        tmp_path,
        [
            call("task", {"description": "root work", "subagent_type": "child"}, "root-task"),
            call("execute", {"command": "printf parent > parent-marker"}, "parent-after"),
            AIMessage(content="done"),
        ],
        subagents=[{"name": "child", "description": "child", "runnable": child}],
    )
    inputs, config, context = run_args(prompt="original user")
    try:
        result = await graph.ainvoke(inputs, config, context=context)
        assert (tmp_path / "grandchild-marker").read_text() == "grandchild"
        assert (tmp_path / "child-marker").read_text() == "child"
        assert (tmp_path / "parent-marker").read_text() == "parent"
        assert len(result[KEY]["turns"]) == 1
        assert len(result[KEY]["events"]) == 5
        assert len({event["turn_id"] for event in result[KEY]["events"]}) == 1
        assert all(payload["user_prompt"] == "original user" for payload in seen)
        parent_check = next(payload for payload in seen if payload["metadata"]["tool_call_id"] == "parent-after")
        assert any(
            action["action_detail"] == "printf grandchild > grandchild-marker"
            for action in parent_check["action_history"]
        )
        assert all(
            not ({"scope", "parent_call_id", "agent_role", "delegated_task"} & payload["metadata"].keys())
            for payload in seen
        )
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("legacy_status", ["executed_success", "attempted", "unknown"])
async def test_real_checkpoint_upgrade_preserves_finished_calls_and_rejects_unfinished(tmp_path, legacy_status):
    from test_session_history import legacy_event, legacy_history

    class OldState(AgentState):
        agentgate_history: dict

    saver = InMemorySaver()
    _, config, context = run_args(session="chat", prompt="next input")
    old = (
        StateGraph(OldState)
        .add_node("old_end", lambda state: {})
        .add_edge(START, "old_end")
        .compile(checkpointer=saver)
    )
    legacy = legacy_history(legacy_event(status=legacy_status))
    await old.aupdate_state(config, {KEY: legacy}, as_node="old_end")
    assert (await old.aget_state(config)).values[KEY]["schema"] == 3
    assert (await old.aget_state(config)).next == ()
    runtime, seen = setup(tmp_path)
    graph = make_agent(
        runtime,
        tmp_path,
        [call("execute", {"command": "printf upgraded > upgraded"}, "same"), AIMessage(content="done")],
        checkpointer=saver,
    )
    try:
        if legacy_status != "executed_success":
            with pytest.raises(ValueError, match="unfinished"):
                await graph.ainvoke({"messages": [("user", "next input")]}, config, context=context)
            assert seen == [] and not (tmp_path / "upgraded").exists()
            assert (await old.aget_state(config)).values[KEY] == legacy
        else:
            result = await graph.ainvoke({"messages": [("user", "next input")]}, config, context=context)
            assert (tmp_path / "upgraded").read_text() == "upgraded"
            assert result[KEY]["schema"] == 5 and len(result[KEY]["events"]) == 2
            assert len({event["event_id"] for event in result[KEY]["events"]}) == 2
            assert [turn["text"] for turn in result[KEY]["turns"]] == ["original user", "next input"]
            assert seen[0]["action_history"] == [{"action_type": "bash", "action_detail": "printf test"}]
            await graph.aupdate_state(config, {KEY: legacy})
            assert len((await graph.aget_state(config)).values[KEY]["events"]) == 2
            next_inputs, next_config, next_context = run_args(session="chat", prompt="last input")
            result = await graph.ainvoke(next_inputs, next_config, context=next_context)
            assert len(result[KEY]["events"]) == 2 and len(result[KEY]["turns"]) == 3
    finally:
        await runtime.aclose()


async def test_oversized_analysis_blocks_tool_without_sending_truncated_arguments(tmp_path):
    @tool
    def save_payload(text: str) -> str:
        """Create a marker when called."""
        (tmp_path / "oversized-effect").write_text(text)
        return "saved"

    runtime, seen = setup(tmp_path)
    graph = make_agent(
        runtime,
        tmp_path,
        [call("save_payload", {"text": "x" * 270000}, "large"), AIMessage(content="done")],
        tools=[save_payload],
    )
    inputs, config, context = run_args()
    try:
        result = await graph.ainvoke(inputs, config, context=context)
        assert seen == [] and not (tmp_path / "oversized-effect").exists()
        assert any(
            "exceeds configured limit" in str(message.content)
            for message in result["messages"]
            if getattr(message, "status", None) == "error"
        )
        assert result[KEY]["truncated"] is True
    finally:
        await runtime.aclose()
