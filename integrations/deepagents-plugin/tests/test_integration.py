"""Gate decides policy; the plugin carries context and runs the original tool."""

import json
from uuid import uuid4

import httpx
import pytest
from deepagents import create_deep_agent
from deepagents.backends.local_shell import LocalShellBackend
from langchain_core.messages import AIMessage, ToolMessage, convert_to_messages
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from models import ScriptedModel, call

from agentmoss_deepagents import AgentMossMiddleware, GateRuntime
from agentmoss_deepagents.client import GateClient
from agentmoss_deepagents.config import GateConfig
from agentmoss_deepagents.history import KEY


def setup(tmp_path, response=None):
    token = tmp_path / "token"
    token.write_text("secret-token")
    config = GateConfig(token)
    seen = []

    def handle(req):
        seen.append(json.loads(req.content))
        if isinstance(response, Exception):
            raise response
        return response or httpx.Response(200, json={"decision": "Allow"})

    return (GateRuntime(config, GateClient(config, transport=httpx.MockTransport(handle))), seen)


def config(session=None):
    return {"configurable": {"thread_id": session or str(uuid4())}, "recursion_limit": 60}


def done():
    return AIMessage(content="done")


def context_for(inputs, cfg):
    return {
        "moss_info": {
            "session_id": cfg["configurable"]["thread_id"],
            "user_prompt": "\n".join(str(m.content) for m in convert_to_messages(inputs["messages"])),
        }
    }


def build(runtime, tmp_path, responses, **kwargs):
    backend = LocalShellBackend(root_dir=tmp_path)
    middleware = AgentMossMiddleware(runtime, backend)
    children = list(kwargs.pop("subagents", []))
    if not any(child["name"] == "general-purpose" for child in children):
        children.append({"name": "general-purpose", "description": "General purpose worker"})
    children = [{**child, "middleware": [*child.get("middleware", []), middleware]} for child in children]
    kwargs.setdefault("context_schema", dict)
    return create_deep_agent(
        model=ScriptedModel(responses=responses),
        backend=backend,
        middleware=[*kwargs.pop("middleware", []), middleware],
        subagents=children,
        **kwargs,
    )


@pytest.mark.parametrize(
    "name,args,target",
    [
        ("execute", {"command": "printf shell-ok > shell-marker"}, "shell-marker"),
        ("write_file", {"file_path": "/existing", "content": "after"}, "existing"),
        ("edit_file", {"file_path": "/existing", "old_string": "before", "new_string": "after"}, "existing"),
    ],
)
@pytest.mark.parametrize("allow", [True, False])
async def test_gate_verdict_is_only_policy_gate(tmp_path, name, args, target, allow):
    (tmp_path / "existing").write_text("before")
    runtime, seen = setup(
        tmp_path, httpx.Response(200, json={"decision": "Allow" if allow else "Deny", "reason": "fixture"})
    )
    result = await build(runtime, tmp_path, [call(name, args, "call"), done()]).ainvoke(
        (_sdk_inputs := {"messages": [("user", "test")]}),
        (_sdk_config := config()),
        context=context_for(_sdk_inputs, _sdk_config),
    )
    assert len(seen) == 1
    if allow:
        assert (tmp_path / target).read_text() in {"shell-ok", "after"}
    elif target == "existing":
        assert (tmp_path / target).read_text() == "before"
    else:
        assert not (tmp_path / target).exists()
    assert result[KEY]["events"][0]["decision"] == ("Allow" if allow else "Deny")


async def test_custom_tool_is_sent_to_gate_and_full_history_is_forwarded(tmp_path):

    @tool
    def custom_marker(text: str) -> str:
        """Write a disposable custom marker."""
        (tmp_path / "custom").write_text(text)
        return "OUTPUT_MUST_NOT_BE_COPIED"

    runtime, seen = setup(tmp_path)
    responses = [
        call("write_file", {"file_path": "/script", "content": "cat /example/target"}, "write"),
        call("execute", {"command": "printf hello"}, "shell"),
        call("custom_marker", {"text": "custom-ok"}, "custom"),
        call("execute", {"command": "pwd"}, "last"),
        done(),
    ]
    result = await build(runtime, tmp_path, responses, tools=[custom_marker]).ainvoke(
        (_sdk_inputs := {"messages": [("user", "test")]}),
        (_sdk_config := config()),
        context=context_for(_sdk_inputs, _sdk_config),
    )
    assert (tmp_path / "custom").read_text() == "custom-ok"
    assert [a["action_type"] for a in seen[-1]["action_history"]] == ["write", "bash", "custom_marker"]
    assert "cat /example/target" in seen[-1]["action_history"][0]["action_detail"]
    assert seen[-1]["action_history"][1]["action_detail"] == "printf hello"
    assert seen[-1]["metadata"]["history_semantics"] == "tool_calls"
    assert "OUTPUT_MUST_NOT_BE_COPIED" not in json.dumps(result[KEY])
    assert "verified_reads" not in json.dumps(result[KEY])
    assert not list(tmp_path.glob("*.sqlite*"))


async def test_failed_and_denied_calls_are_context_not_read_evidence(tmp_path):
    runtime, seen = setup(tmp_path)

    def handle(req):
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(200, json={"decision": "Deny" if body["tool_name"] == "read" else "Allow"})

    runtime.client = GateClient(runtime.config, transport=httpx.MockTransport(handle))
    result = await build(
        runtime,
        tmp_path,
        [
            call("read_file", {"file_path": "/missing"}, "denied"),
            call("execute", {"command": "false"}, "failed"),
            call("execute", {"command": "pwd"}, "last"),
            done(),
        ],
    ).ainvoke(
        (_sdk_inputs := {"messages": [("user", "test")]}),
        (_sdk_config := config()),
        context=context_for(_sdk_inputs, _sdk_config),
    )
    assert [a["action_type"] for a in seen[-1]["action_history"]] == ["read", "bash"]
    assert [a["status"] for a in seen[-1]["metadata"]["history_statuses"]] == ["denied", "executed_error"]
    assert all("output" not in a for a in seen[-1]["action_history"])
    assert result[KEY]["events"][-1]["tool_call_id"] == "last"


@pytest.mark.parametrize("mode", ["child", "parallel", "fork", "restore"])
async def test_full_context_returns_from_children_and_checkpoints(tmp_path, mode):
    from contextlib import nullcontext

    runtime, seen = setup(tmp_path)
    saver = InMemorySaver()
    cfg = config()
    action = call("execute", {"command": "printf child"}, "child-shell")
    if mode == "restore":
        await build(runtime, tmp_path, [action, done()], checkpointer=saver).ainvoke(
            (_sdk_inputs := {"messages": [("user", "first")]}),
            (_sdk_config := cfg),
            context=context_for(_sdk_inputs, _sdk_config),
        )
        runtime, seen = setup(tmp_path)
        agent = build(runtime, tmp_path, [call("execute", {"command": "pwd"}, "last"), done()], checkpointer=saver)
    else:
        specs = [{"name": "worker", "description": "worker", "model": ScriptedModel(responses=[action, done()])}]
        if mode == "fork":
            specs[0]["mode"] = "fork"
        delegate = call("task", {"description": "child task", "subagent_type": "worker"}, "delegate")
        if mode == "parallel":
            specs.append(
                {
                    "name": "other",
                    "description": "other",
                    "model": ScriptedModel(
                        responses=[call("execute", {"command": "printf other"}, "other-shell"), done()]
                    ),
                }
            )
            delegate.tool_calls.append(
                {
                    "name": "task",
                    "args": {"description": "other", "subagent_type": "other"},
                    "id": "other-task",
                    "type": "tool_call",
                }
            )
        with pytest.warns(Warning) if mode == "fork" else nullcontext():
            agent = build(
                runtime,
                tmp_path,
                [delegate, call("execute", {"command": "pwd"}, "last"), done()],
                subagents=specs,
                checkpointer=saver,
            )
    result = await agent.ainvoke(
        (_sdk_inputs := {"messages": [("user", "next")]}),
        (_sdk_config := cfg),
        context=context_for(_sdk_inputs, _sdk_config),
    )
    last = next(b for b in seen if b["metadata"]["tool_call_id"] == "last")
    assert any(a["action_detail"] == "printf child" for a in last["action_history"])
    assert last["session_id"] == "deepagents:" + cfg["configurable"]["thread_id"]
    assert len({e["event_id"] for e in result[KEY]["events"]}) == len(result[KEY]["events"])


async def test_v2_diagnostic_channel_stays_separate_from_current_call_history(tmp_path, monkeypatch):
    import importlib
    import importlib.util
    import sys
    import tarfile
    from pathlib import Path

    legacy = tmp_path / "v2-source"
    legacy.mkdir()
    with tarfile.open(Path(__file__).parent / "fixtures/v2-source.tar.gz") as archive:
        archive.extractall(legacy, filter="data")
    prefix = "fixture_v2_" + uuid4().hex
    spec = importlib.util.spec_from_file_location(
        prefix, legacy / "__init__.py", submodule_search_locations=[str(legacy)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[prefix] = module
    spec.loader.exec_module(module)
    old_assembly = importlib.import_module(prefix + ".assembly")
    # The archived fixture pins its original environment; this test checks history isolation.
    monkeypatch.setattr(old_assembly, "validate_versions", lambda: None)
    old_create = old_assembly.create_guarded_agent
    old_runtime_class = importlib.import_module(prefix + ".runtime").GateRuntime
    saver, cfg = (InMemorySaver(), config())
    backend = LocalShellBackend(root_dir=tmp_path)
    (tmp_path / "old.txt").write_text("original")
    client_runtime, _ = setup(tmp_path)
    old = old_runtime_class(client_runtime.config, client_runtime.client)
    first = old_create(
        runtime=old,
        model=ScriptedModel(responses=[call("read_file", {"file_path": "/old.txt"}, "v2-read"), done()]),
        backend=backend,
        checkpointer=saver,
    )
    result = await first.ainvoke({"messages": [("user", "v2")]}, cfg)
    assert result["agentmoss_history"]["events"][0]["verified_reads"]
    await old.aclose()
    current, seen = setup(tmp_path)
    result = await build(
        current,
        tmp_path,
        [call("execute", {"command": "printf v3 > v3-marker"}, "v3-shell"), done()],
        checkpointer=saver,
    ).ainvoke(
        (_sdk_inputs := {"messages": [("user", "v3")]}),
        (_sdk_config := cfg),
        context=context_for(_sdk_inputs, _sdk_config),
    )
    assert result[KEY]["schema"] == 4 and seen[0]["action_history"] == []
    assert (tmp_path / "v3-marker").read_text() == "v3"
    await current.aclose()
    client_runtime, _ = setup(tmp_path)
    old = old_runtime_class(client_runtime.config, client_runtime.client)
    last = old_create(
        runtime=old,
        model=ScriptedModel(responses=[call("execute", {"command": "pwd"}, "v2-again"), done()]),
        backend=backend,
        checkpointer=saver,
    )
    result = await last.ainvoke({"messages": [("user", "v2 rollback")]}, cfg)
    assert any(
        isinstance(m, ToolMessage) and m.tool_call_id == "v2-again" and (m.status == "success")
        for m in result["messages"]
    )
    assert (tmp_path / "old.txt").read_text() == "original"
    await old.aclose()


def test_native_unsupported_permissions_are_not_silently_removed(tmp_path):
    from deepagents import create_deep_agent
    from deepagents.middleware.filesystem import FilesystemPermission

    rule = FilesystemPermission(operations=["write"], paths=["/protected"], mode="deny")
    backend = LocalShellBackend(root_dir=tmp_path)
    with pytest.raises(NotImplementedError, match="permissions"):
        create_deep_agent(model=ScriptedModel(responses=[done()]), backend=backend, permissions=[rule])
    runtime, seen = setup(tmp_path)
    with pytest.raises(NotImplementedError, match="permissions"):
        build(runtime, tmp_path, [done()], permissions=[rule])
    assert seen == []


async def test_original_application_permission_middleware_remains_in_control(tmp_path):
    from langchain.agents.middleware import AgentMiddleware

    class AppPermission(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            if request.tool_call["name"] == "write_file":
                return ToolMessage(
                    content="Application permission denied", tool_call_id=request.tool_call["id"], status="error"
                )
            return await handler(request)

    path = tmp_path / "protected"
    path.write_text("before")
    runtime, seen = setup(tmp_path)
    await build(
        runtime,
        tmp_path,
        [call("write_file", {"file_path": "/protected", "content": "BAD"}, "w"), done()],
        middleware=[AppPermission()],
    ).ainvoke(
        (_sdk_inputs := {"messages": [("user", "test")]}),
        (_sdk_config := config()),
        context=context_for(_sdk_inputs, _sdk_config),
    )
    assert path.read_text() == "before" and seen == []


async def test_internal_custom_command_keeps_application_state_and_message(tmp_path):
    from langchain.agents.middleware import AgentState
    from langchain.tools import ToolRuntime
    from langgraph.types import Command

    class AppState(AgentState):
        app_marker: str

    @tool
    def set_marker(text: str, runtime: ToolRuntime[dict]) -> Command:
        """Update an application state field."""
        return Command(
            update={
                "app_marker": text,
                "messages": [ToolMessage(content="tool-result", tool_call_id=runtime.tool_call_id)],
            }
        )

    runtime, seen = setup(tmp_path)
    result = await build(
        runtime,
        tmp_path,
        [call("set_marker", {"text": "kept"}, "custom"), done()],
        tools=[set_marker],
        state_schema=AppState,
    ).ainvoke(
        (_sdk_inputs := {"messages": [("user", "test")]}),
        (_sdk_config := config()),
        context=context_for(_sdk_inputs, _sdk_config),
    )
    assert result["app_marker"] == "kept"
    assert result[KEY]["events"][0]["original_name"] == "set_marker"
    assert any(isinstance(m, ToolMessage) and m.content == "tool-result" for m in result["messages"])


async def test_denied_custom_tool_never_runs(tmp_path):

    @tool
    def custom_effect() -> str:
        """Create a disposable marker."""
        (tmp_path / "denied-custom").touch()
        return "ok"

    runtime, seen = setup(tmp_path, httpx.Response(200, json={"decision": "Deny"}))
    result = await build(
        runtime, tmp_path, [call("custom_effect", {}, "custom"), done()], tools=[custom_effect]
    ).ainvoke(
        (_sdk_inputs := {"messages": [("user", "test")]}),
        (_sdk_config := config()),
        context=context_for(_sdk_inputs, _sdk_config),
    )
    assert len(seen) == 1 and (not (tmp_path / "denied-custom").exists())
    assert result[KEY]["events"][0]["status"] == "denied"


async def test_tool_exception_context_commits_without_copying_error_output(tmp_path):

    @tool
    def failing_tool() -> str:
        """Raise an ordinary tool failure."""
        raise RuntimeError("PRIVATE_TOOL_ERROR_BODY")

    runtime, seen = setup(tmp_path)
    result = await build(runtime, tmp_path, [call("failing_tool", {}, "failed"), done()], tools=[failing_tool]).ainvoke(
        (_sdk_inputs := {"messages": [("user", "test")]}),
        (_sdk_config := config()),
        context=context_for(_sdk_inputs, _sdk_config),
    )
    assert len(result[KEY]["events"]) == 1
    assert result[KEY]["events"][0]["status"] in {"executed_error", "unknown"}
    assert "PRIVATE_TOOL_ERROR_BODY" not in json.dumps(result[KEY])


async def test_internal_command_without_update_routes_to_parent_graph(tmp_path):
    from langgraph.types import Command

    @tool
    def return_to_parent() -> Command:
        """Route from a declarative child to its parent model."""
        return Command(graph=Command.PARENT, goto="model")

    runtime, seen = setup(tmp_path)
    child = ScriptedModel(responses=[call("return_to_parent", {}, "route"), AIMessage(content="CHILD_FALLBACK")])
    agent = build(
        runtime,
        tmp_path,
        [
            call("task", {"description": "route", "subagent_type": "worker"}, "delegate"),
            AIMessage(content="PARENT_DESTINATION"),
        ],
        subagents=[{"name": "worker", "description": "worker", "model": child, "tools": [return_to_parent]}],
    )
    result = await agent.ainvoke(
        (_sdk_inputs := {"messages": [("user", "route")]}),
        (_sdk_config := config()),
        context=context_for(_sdk_inputs, _sdk_config),
    )
    assert child._index == 1
    assert result["messages"][-1].content == "PARENT_DESTINATION"
    assert not any("local integration error" in str(m.content) for m in result["messages"])
    assert any(e["tool_call_id"] == "route" for e in result[KEY]["events"])


@pytest.mark.parametrize("representation", ["object", "dict"])
@pytest.mark.parametrize("current_status", ["success", "error"])
async def test_command_status_matches_current_call_and_normalizes_message_dicts(
    tmp_path, representation, current_status
):
    from langchain.agents.middleware import AgentState
    from langchain.tools import ToolRuntime
    from langgraph.types import Command

    class AppState(AgentState):
        marker: str

    @tool
    def report_result(runtime: ToolRuntime[dict]) -> Command:
        """Return the current result alongside a different call's message."""
        messages = [
            ToolMessage(content="current result", tool_call_id=runtime.tool_call_id, status=current_status),
            ToolMessage(
                content="another call result",
                tool_call_id="other-call",
                status="error" if current_status == "success" else "success",
            ),
        ]
        if representation == "dict":
            messages = [message.model_dump() for message in messages]
        return Command(update={"messages": messages, "marker": "kept"})

    runtime, seen = setup(tmp_path)
    inputs = {"messages": [("user", "report the tool result")]}
    cfg = config()
    try:
        result = await build(
            runtime,
            tmp_path,
            [call("report_result", {}, "current"), call("execute", {"command": "pwd"}, "next"), done()],
            tools=[report_result],
            state_schema=AppState,
        ).ainvoke(inputs, cfg, context=context_for(inputs, cfg))
        expected = "executed_success" if current_status == "success" else "executed_error"
        event = next(event for event in result[KEY]["events"] if event["tool_call_id"] == "current")
        assert event["status"] == expected
        forwarded = next(row for row in seen[1]["metadata"]["history_statuses"] if row["tool_call_id"] == "current")
        assert forwarded["status"] == expected
        actual = next(
            message
            for message in result["messages"]
            if isinstance(message, ToolMessage) and message.tool_call_id == "current"
        )
        assert actual.status == current_status and result["marker"] == "kept"
    finally:
        await runtime.aclose()
