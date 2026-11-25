"""Exercise Gate through official SDK factories and invocation APIs."""

import json
from uuid import uuid4

import httpx
import pytest
from deepagents import create_deep_agent
from deepagents.backends.local_shell import LocalShellBackend
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from models import ScriptedModel, call

import agentmoss_deepagents as plugin
from agentmoss_deepagents.client import GateClient
from agentmoss_deepagents.config import GateConfig
from agentmoss_deepagents.history import KEY


def setup(tmp_path, response=None):
    token = tmp_path / "token"
    token.write_text("secret-token")
    config = GateConfig(token)
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        if callable(response):
            return response(request)
        if isinstance(response, Exception):
            raise response
        return response or httpx.Response(200, json={"decision": "Allow"})

    return plugin.GateRuntime(config, GateClient(config, transport=httpx.MockTransport(handle))), seen


def run_args(session=None, prompt="test"):
    session = session or str(uuid4())
    return (
        {"messages": [("user", prompt)]},
        {"configurable": {"thread_id": session}, "recursion_limit": 60},
        {"moss_info": {"session_id": session, "user_prompt": prompt}},
    )


def make_agent(runtime, tmp_path, responses, **options):
    """Test setup only; return the original graph with explicit child specs."""
    backend = options.pop("backend", None) or LocalShellBackend(root_dir=tmp_path)
    middleware = plugin.AgentMossMiddleware(runtime, backend)
    children = list(options.pop("subagents", []))
    if not any(spec["name"] == "general-purpose" for spec in children):
        children.append({"name": "general-purpose", "description": "General purpose worker"})
    children = [{**spec, "middleware": [*spec.get("middleware", []), middleware]} for spec in children]
    options.setdefault("context_schema", dict)
    return create_deep_agent(
        model=ScriptedModel(responses=responses),
        backend=backend,
        middleware=[*options.pop("middleware", []), middleware],
        subagents=children,
        **options,
    )


@pytest.mark.parametrize("allow", [True, False])
@pytest.mark.parametrize(
    "name,args,target,expected",
    [
        ("execute", {"command": "printf shell-ok > shell-marker"}, "shell-marker", "shell-ok"),
        ("write_file", {"file_path": "/existing", "content": "after"}, "existing", "after"),
        ("edit_file", {"file_path": "/existing", "old_string": "before", "new_string": "after"}, "existing", "after"),
    ],
)
async def test_native_graph_executes_only_gate_allowed_tools(tmp_path, allow, name, args, target, expected):
    (tmp_path / "existing").write_text("before")
    runtime, seen = setup(tmp_path, httpx.Response(200, json={"decision": "Allow" if allow else "Deny"}))
    graph = make_agent(runtime, tmp_path, [call(name, args, "tool"), AIMessage(content="done")])
    assert isinstance(graph, CompiledStateGraph)
    inputs, config, context = run_args()
    try:
        result = await graph.ainvoke(inputs, config, context=context)
        assert len(seen) == 1
        assert seen[0]["session_id"] == "deepagents:" + context["moss_info"]["session_id"]
        assert result[KEY]["events"][0]["decision"] == ("Allow" if allow else "Deny")
        if allow:
            assert (tmp_path / target).read_text() == expected
        elif target == "existing":
            assert (tmp_path / target).read_text() == "before"
        else:
            assert not (tmp_path / target).exists()
        if not allow:
            assert any(
                isinstance(m, ToolMessage) and m.tool_call_id == "tool" and m.status == "error"
                for m in result["messages"]
            )
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    "context",
    [
        None,
        {},
        {"moss_info": {}},
        {"moss_info": {"session_id": "", "user_prompt": "x"}},
        {"moss_info": {"session_id": "other", "user_prompt": "x"}},
    ],
)
async def test_invalid_moss_info_prevents_tools(tmp_path, context):
    runtime, seen = setup(tmp_path)
    graph = make_agent(runtime, tmp_path, [call("write_file", {"file_path": "/bad", "content": "bad"}, "bad")])
    inputs, config, _ = run_args()
    try:
        with pytest.raises(ValueError):
            await graph.ainvoke(inputs, config, context=context)
        assert seen == [] and not (tmp_path / "bad").exists()
    finally:
        await runtime.aclose()


async def test_no_tool_turn_is_saved_in_existing_checkpointer(tmp_path):
    runtime, _ = setup(tmp_path)
    graph = make_agent(runtime, tmp_path, [AIMessage(content="hello")], checkpointer=InMemorySaver())
    inputs, config, context = run_args(prompt="hello user")
    try:
        result = await graph.ainvoke(inputs, config, context=context)
        assert len(result[KEY]["turns"]) == 1 and result[KEY]["turns"][0]["text"] == "hello user"
        state = await graph.aget_state(config)
        assert state.values[KEY]["events"] == [] and state.values[KEY]["turns"][0]["text"] == "hello user"
    finally:
        await runtime.aclose()


async def test_gate_payload_omits_only_the_current_prompt_text(tmp_path):
    runtime, seen = setup(tmp_path)
    graph = make_agent(
        runtime,
        tmp_path,
        [
            call("execute", {"command": "printf previous"}, "previous"),
            AIMessage(content="done"),
            call("execute", {"command": "printf first"}, "first"),
            call("execute", {"command": "printf second"}, "second"),
            AIMessage(content="done"),
        ],
        checkpointer=InMemorySaver(),
    )
    prompt = "甲" * 194
    try:
        for _ in range(2):
            inputs, config, context = run_args("repeated-prompt", prompt)
            await graph.ainvoke(inputs, config, context=context)

        assert seen[-1]["metadata"]["tool_call_id"] == "second"
        assert seen[-1]["user_prompt"] == prompt
        previous = {"action_type": "bash", "action_detail": "printf previous"}
        first = {"action_type": "bash", "action_detail": "printf first"}
        assert seen[-1]["prompt_history"] == [
            {"text": prompt, "actions": [previous]},
            {"text": "", "actions": [first]},
        ]
        assert seen[-1]["action_history"] == [previous, first]
        saved = (await graph.aget_state(config)).values[KEY]
        assert [turn["text"] for turn in saved["turns"]] == [prompt, prompt]
    finally:
        await runtime.aclose()


async def test_parallel_leaf_calls_see_completed_predecessor(tmp_path):
    first = call("execute", {"command": "printf first"}, "first")
    first.tool_calls.append(
        {"name": "execute", "args": {"command": "printf second"}, "id": "second", "type": "tool_call"}
    )
    runtime, seen = setup(tmp_path)
    inputs, config, context = run_args()
    graph = make_agent(runtime, tmp_path, [first, AIMessage(content="done")])
    try:
        result = await graph.ainvoke(inputs, config, context=context)
        assert len(seen) == 2
        assert seen[0]["action_history"] == []
        assert len(seen[1]["action_history"]) == 1
        assert seen[1]["action_history"][0]["action_detail"] == seen[0]["command"]
        assert len(result[KEY]["events"]) == 2
    finally:
        await runtime.aclose()


async def test_application_context_schema_and_custom_tool_are_preserved(tmp_path):
    from dataclasses import dataclass

    from langchain.tools import ToolRuntime, tool

    @dataclass
    class AppContext:
        moss_info: dict
        tenant: str

    @tool
    def tenant_marker(runtime: ToolRuntime[AppContext]) -> str:
        """Use application context in the original tool."""
        (tmp_path / "tenant").write_text(runtime.context.tenant)
        return "PRIVATE_OUTPUT"

    runtime, seen = setup(tmp_path)
    inputs, config, context = run_args()
    graph = make_agent(
        runtime,
        tmp_path,
        [call("tenant_marker", {}, "tenant"), call("execute", {"command": "pwd"}, "last"), AIMessage(content="done")],
        tools=[tenant_marker],
        context_schema=AppContext,
    )
    try:
        result = await graph.ainvoke(inputs, config, context=AppContext(context["moss_info"], "tenant-a"))
        assert (tmp_path / "tenant").read_text() == "tenant-a"
        assert "PRIVATE_OUTPUT" not in json.dumps(result[KEY])
        assert "PRIVATE_OUTPUT" not in json.dumps(seen)
    finally:
        await runtime.aclose()


def test_command_public_fields_and_duplicate_updates_are_preserved(tmp_path):
    from types import SimpleNamespace

    from langgraph.types import Command

    from agentmoss_deepagents.context import RunContext
    from agentmoss_deepagents.history import RunHistory

    runtime, _ = setup(tmp_path)
    _, cfg, context = run_args()
    info = context["moss_info"]
    info["_agentmoss_run"] = RunContext(
        info, info["session_id"], info["user_prompt"], "turn", RunHistory(info["session_id"], {}, runtime.config)
    )
    request = SimpleNamespace(runtime=SimpleNamespace(context=context, config=cfg))
    middleware = plugin.AgentMossMiddleware(runtime, LocalShellBackend(root_dir=tmp_path))
    original = Command(update=(("app_values", ["one"]), ("app_values", ["two"])), graph=Command.PARENT, goto="next")
    result = middleware._result(request, original)
    assert list(result.update)[:2] == list(original.update)
    assert result.graph == Command.PARENT and result.goto == "next"
    assert result.update[-1][0] == KEY
    runtime.client.close()


def test_analysis_path_resolution_uses_no_backend_private_method(tmp_path, monkeypatch):
    from agentmoss_deepagents.actions import ActionAdapter

    backend = LocalShellBackend(root_dir=tmp_path)

    def unsupported(*args):
        raise AssertionError("SDK private path method is unavailable")

    monkeypatch.setattr(backend, "_resolve_path", unsupported)
    action = ActionAdapter(backend).adapt(
        "edit_file", {"file_path": "/file name", "old_string": "a", "new_string": "b"}
    )
    assert action.metadata["real_path"] == str(tmp_path / "file name")


@pytest.mark.parametrize("virtual", [True, False])
@pytest.mark.parametrize("name", ["read_file", "edit_file"])
async def test_policy_path_matches_actual_sdk_normalization(tmp_path, virtual, name):
    target = tmp_path / "nested/file.txt"
    target.parent.mkdir()
    target.write_text("before")
    # Official file tools replace backslashes and add a leading slash even
    # in native mode; the policy adapter must inspect that same target.
    raw = "/nested\\file.txt" if virtual else str(target).lstrip("/")
    args = {"file_path": raw}
    if name == "edit_file":
        args.update(old_string="before", new_string="after")
    runtime, seen = setup(tmp_path)
    backend = LocalShellBackend(root_dir=tmp_path, virtual_mode=virtual)
    graph = make_agent(runtime, tmp_path, [call(name, args, "normalized"), AIMessage(content="done")], backend=backend)
    inputs, config, context = run_args()
    try:
        result = await graph.ainvoke(inputs, config, context=context)
        assert seen[0]["metadata"]["real_path"] == str(target)
        assert result[KEY]["events"][0]["status"] == "executed_success"
        assert target.read_text() == ("after" if name == "edit_file" else "before")
        if name == "read_file":
            assert any(isinstance(m, ToolMessage) and "before" in str(m.content) for m in result["messages"])
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("name,path", [("glob", None), ("glob", "/"), ("grep", None)])
async def test_native_search_default_policy_target_is_backend_cwd(tmp_path, name, path):
    (tmp_path / "search.txt").write_text("needle")
    args = {"pattern": "*.txt" if name == "glob" else "needle"}
    if path is not None:
        args["path"] = path
    runtime, seen = setup(tmp_path)
    graph = make_agent(
        runtime,
        tmp_path,
        [call(name, args, "search"), AIMessage(content="done")],
        backend=LocalShellBackend(root_dir=tmp_path, virtual_mode=False),
    )
    inputs, config, context = run_args()
    try:
        result = await graph.ainvoke(inputs, config, context=context)
        assert seen[0]["metadata"]["real_path"] == str(tmp_path)
        assert result[KEY]["events"][0]["status"] == "executed_success"
        assert any(isinstance(m, ToolMessage) and "search.txt" in str(m.content) for m in result["messages"])
    finally:
        await runtime.aclose()


async def test_concurrent_root_sessions_keep_history_and_locks_separate(tmp_path):
    import asyncio

    runtime, _ = setup(tmp_path)
    seen, first_sessions = [], set()
    both_started = asyncio.Event()

    async def analyze(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if not payload["action_history"]:
            first_sessions.add(payload["session_id"])
            if len(first_sessions) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 2)
        return httpx.Response(200, json={"decision": "Allow"})

    await runtime.client.aclose()
    runtime.client = GateClient(runtime.config, transport=httpx.MockTransport(analyze))

    async def run(label):
        graph = make_agent(
            runtime,
            tmp_path,
            [
                call("execute", {"command": "printf " + label}, label),
                call("execute", {"command": "pwd"}, label + "2"),
                AIMessage(content="done"),
            ],
        )
        inputs, config, context = run_args(prompt=label)
        result = await graph.ainvoke(inputs, config, context=context)
        assert [e["tool_call_id"] for e in result[KEY]["events"]] == [label, label + "2"]

    try:
        await asyncio.wait_for(asyncio.gather(run("alpha"), run("beta")), 5)
        assert len(seen) == 4
        for payload in seen:
            if payload["action_history"]:
                assert payload["action_history"] == [
                    {"action_type": "bash", "action_detail": "printf " + payload["user_prompt"]}
                ]
    finally:
        await runtime.aclose()
