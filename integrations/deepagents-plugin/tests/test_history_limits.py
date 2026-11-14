"""Bounded checkpoints must not restore pruned records or lose the current turn."""

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from models import call
from test_native_sdk import make_agent, run_args, setup
from test_session_history import legacy_event, legacy_history

from agentmoss_deepagents.actions import Action
from agentmoss_deepagents.config import GateConfig
from agentmoss_deepagents.context import RunContext
from agentmoss_deepagents.history import KEY, RunHistory, make_event_id, merge_history, migrate_history


@pytest.mark.parametrize("sync", [False, True])
async def test_small_window_keeps_current_actions_in_gate_and_checkpoint(tmp_path, sync):
    runtime, seen = setup(tmp_path)
    runtime.config = replace(runtime.config, history_actions=1, history_turns=1)
    responses = []
    for turn in range(3):
        responses.extend(
            [
                call("execute", {"command": f"printf turn{turn}-first"}, f"first-{turn}"),
                call("execute", {"command": f"printf turn{turn}-second"}, f"second-{turn}"),
                AIMessage(content="done"),
            ]
        )
    graph = make_agent(runtime, tmp_path, responses, checkpointer=InMemorySaver())
    try:
        for turn in range(3):
            inputs, config, context = run_args("small-window", f"turn-{turn}")
            if sync:
                await asyncio.to_thread(graph.invoke, inputs, config, context=context)
            else:
                await graph.ainvoke(inputs, config, context=context)
            saved = (await graph.aget_state(config)).values[KEY]
            assert [event["tool_call_id"] for event in saved["events"]] == [f"second-{turn}"]
            assert [prompt["text"] for prompt in saved["turns"]] == [f"turn-{turn}"]
            assert seen[-1]["action_history"] == [{"action_type": "bash", "action_detail": f"printf turn{turn}-first"}]
    finally:
        await runtime.aclose()


def record(view, turn, call_id, command="printf test"):
    context = RunContext({}, "chat", "prompt", turn, view)
    event = view.begin(
        context,
        call_id,
        Action("bash", command, "/"),
        "execute",
        event_id=make_event_id("chat", "node", call_id),
    )
    view.finish(event, "executed_success", decision="Allow")
    return event


@pytest.mark.parametrize("limit", ["actions", "turns", "bytes"])
def test_stale_snapshot_cannot_resurrect_pruned_history(limit):
    view = RunHistory("chat", {}, GateConfig(enabled=False, history_actions=1, history_turns=1, history_bytes=2048))
    view.record_prompt("old-turn", "old prompt")
    record(view, "old-turn", "old")
    stale = deepcopy(view.value)
    turn = "new-turn" if limit == "turns" else "old-turn"
    if limit == "turns":
        view.record_prompt(turn, "new prompt")
    evicted = record(view, turn, "new", "x" * 6000 if limit == "bytes" else "printf new")
    recent = deepcopy(view.value)
    merged = merge_history(stale, recent)
    assert [event["tool_call_id"] for event in merged["events"]] == ([] if limit == "bytes" else ["new"])
    assert merge_history(recent, stale) == merged
    assert merge_history(merged, stale) == merged
    view.merge(stale)
    assert view.value == merged
    next_event = record(view, turn, "after")
    assert next_event["order"] > evicted["seq"]


async def test_new_invocation_can_change_limits_without_restoring_evicted_records(tmp_path):
    runtime, _ = setup(tmp_path)
    saver = InMemorySaver()
    try:
        for turn, limit in enumerate([1, 3, 1]):
            runtime.config = replace(runtime.config, history_actions=limit, history_turns=limit)
            graph = make_agent(
                runtime,
                tmp_path,
                [
                    call("execute", {"command": "printf first"}, f"first-{turn}"),
                    call("execute", {"command": "printf second"}, f"second-{turn}"),
                    AIMessage(content="done"),
                ],
                checkpointer=saver,
            )
            inputs, config, context = run_args("changing-limits", f"turn-{turn}")
            result = await graph.ainvoke(inputs, config, context=context)
            expected = ["second-0", "first-1", "second-1"] if limit == 3 else [f"second-{turn}"]
            assert [event["tool_call_id"] for event in result[KEY]["events"]] == expected
    finally:
        await runtime.aclose()


def v4_history(status="executed_success"):
    event = legacy_event(status=status)
    for field in ("root_session_id", "scope", "parent_call_id"):
        del event[field]
    event["event_id"] = make_event_id("chat", "old-node", "same")
    return legacy_history(event) | {"schema": 4}


def test_completed_v4_history_migrates_without_changing_recorded_actions():
    original = v4_history()
    before = deepcopy(original)
    upgraded = migrate_history(original, "chat")
    assert upgraded["schema"] == 5
    assert upgraded["events"] == original["events"]
    assert upgraded["turns"][0]["text"] == "original user"
    assert migrate_history(upgraded, "chat") == upgraded
    assert original == before


@pytest.mark.parametrize("status", ["attempted", "unknown"])
def test_unfinished_v4_history_requires_reconciliation_before_upgrade(status):
    with pytest.raises(ValueError, match="unfinished"):
        migrate_history(v4_history(status), "chat")


def test_history_budget_that_cannot_hold_metadata_is_rejected_before_execution():
    view = RunHistory("chat", {}, GateConfig(enabled=False, history_bytes=64))
    with pytest.raises(ValueError, match="history_bytes"):
        view.record_prompt("turn", "prompt")


def test_late_completion_cannot_restore_an_evicted_parent_action():
    view = RunHistory("chat", {}, GateConfig(enabled=False, history_actions=1))
    view.record_prompt("turn", "prompt")
    context = RunContext({}, "chat", "prompt", "turn", view)
    parent = view.begin(
        context,
        "parent",
        Action("task", "delegate", "/"),
        "task",
        event_id=make_event_id("chat", "node", "parent"),
    )
    record(view, "turn", "child")
    view.finish(parent, "executed_success", decision="Allow")
    assert [event["tool_call_id"] for event in view.value["events"]] == ["child"]
