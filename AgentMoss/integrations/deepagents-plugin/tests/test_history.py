import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_integration import setup

from agentmoss_deepagents.actions import Action
from agentmoss_deepagents.context import RunContext
from agentmoss_deepagents.history import RunHistory, empty_history, make_event_id, merge_history, validate_history


def event(i, root="root"):
    return {
        "event_id": make_event_id(root, "fixture-node", str(i)),
        "tool_call_id": str(i),
        "turn_id": "turn",
        "seq": i,
        "order": i,
        "status": "executed_success",
        "action_detail": "command-" + str(i),
        "arguments": {},
        "original_name": "execute",
        "action_type": "bash",
        "cwd": "/",
        "source": "gate",
        "decision": "Allow",
    }


def state(events):
    return empty_history("root") | {
        "events": events,
        "turns": [{"id": "turn", "text": "user", "order": 1}],
        "current_turn": {"id": "turn", "order": 1},
        "clock": max([1, *(event["seq"] for event in events)]),
    }


def test_idempotent_merge_keeps_latest_status_in_original_call_order():
    first = state([event(1)])
    result = merge_history(first, state([event(1) | {"seq": 3, "status": "denied"}, event(2)]))
    assert [e["tool_call_id"] for e in result["events"]] == ["1", "2"]
    assert result["events"][0]["status"] == "denied"
    assert merge_history(result, result) == result


def test_bounds_apply_to_checkpoint_without_clipping_commands():
    result = merge_history({}, state([event(i) | {"action_detail": "x" * 1000} for i in range(300)]))
    assert len(result["events"]) <= 200 and len(json.dumps(result).encode()) <= 32768
    assert result["truncated"] and all(e["action_detail"] == "x" * 1000 for e in result["events"])


def test_cross_root_schema_and_output_rejected():
    for invalid in [
        state([event(1)]) | {"root": "other"},
        state([event(1) | {"output": "secret"}]),
        state([]) | {"schema": 2},
    ]:
        with pytest.raises(ValueError):
            validate_history(invalid, "root")
    with pytest.raises(ValueError):
        merge_history(state([]), state([]) | {"root": "other"})


def test_full_command_and_status_metadata_are_bounded_together(tmp_path):
    runtime, _ = setup(tmp_path)
    session = str(uuid4())
    view = RunHistory(session, {}, runtime.config)
    ctx = RunContext({}, session, "prompt", "turn", view)
    view.record_prompt(ctx.turn_id, ctx.user_prompt)
    command = "printf " + "x" * 6000
    ev = view.begin(ctx, "a", Action("bash", command, "/"), "execute", event_id=make_event_id(session, "node", "a"))
    view.finish(ev, "executed_error")
    snap = view.snapshot()
    assert snap["action_history"][0]["action_detail"] == command
    assert snap["history_statuses"][0]["status"] == "executed_error"
    huge = view.begin(
        ctx, "huge", Action("bash", "z" * 40000, "/"), "execute", event_id=make_event_id(session, "node", "huge")
    )
    view.finish(huge, "executed_success")
    snap = view.snapshot()
    assert len(json.dumps(snap).encode()) <= runtime.config.history_bytes
    assert snap["history_truncated"]
    assert not any(a["action_detail"].startswith("z") for a in snap["action_history"])


@pytest.mark.parametrize("limit", ["bytes", "actions", "turns"])
def test_pruning_history_never_allows_a_started_execution_to_replay(tmp_path, limit):
    runtime, _ = setup(tmp_path)
    config = replace(runtime.config, history_actions=1, history_turns=1)
    view = RunHistory("root", {}, config)
    ctx = RunContext({}, "root", "prompt", "turn", view)
    view.record_prompt(ctx.turn_id, ctx.user_prompt)
    command = "x" * 20_000 if limit == "bytes" else "printf first"
    action = Action("bash", command, "/", {"original_arguments": {"command": command}})
    event_id = make_event_id("root", "tools:node", "first")
    first = view.begin(ctx, "first", action, "execute", event_id=event_id)
    view.finish(first, "executed_success", decision="Allow")
    if limit == "actions":
        second = view.begin(
            ctx,
            "second",
            Action("bash", "printf second", "/"),
            "execute",
            event_id=make_event_id("root", "tools:node", "second"),
        )
        view.finish(second, "executed_success", decision="Allow")
    elif limit == "turns":
        view.record_prompt("next-turn", "next prompt")
    assert all(event["event_id"] != event_id for event in view.value["events"])
    assert view.value["truncated"] is True
    with pytest.raises(ValueError, match="cannot be replayed"):
        view.begin(ctx, "first", action, "execute", event_id=event_id)
