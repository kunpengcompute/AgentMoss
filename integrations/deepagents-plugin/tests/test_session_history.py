"""Session ledger identity and migration contracts, independent of graph assembly."""

from copy import deepcopy
from uuid import UUID

import pytest

from agentmoss_deepagents import history


def legacy_event(call_id="same", scope="main", **updates):
    return {
        "root_session_id": "chat",
        "scope": scope,
        "parent_call_id": "",
        "tool_call_id": call_id,
        "turn_id": "turn",
        "original_name": "execute",
        "action_type": "bash",
        "action_detail": "printf test",
        "arguments": {"command": "printf test"},
        "cwd": "/tmp",
        "status": "executed_success",
        "source": "agent_moss",
        "decision": "Allow",
        "seq": 2,
        "order": 1,
        **updates,
    }


def legacy_history(*events):
    return {
        "schema": 3,
        "root": "chat",
        "events": list(events),
        "turns": [{"id": "turn", "text": "original user"}],
        "truncated": False,
    }


def test_event_id_is_stable_and_separates_sdk_execution_positions():
    first = history.make_event_id("chat", "tools:node-a", "same")
    assert first == "b991f002-0175-51c8-b8da-703d4dbb9093"
    assert first == history.make_event_id("chat", "tools:node-a", "same")
    assert UUID(first).version == 5
    assert (
        len(
            {
                first,
                history.make_event_id("other", "tools:node-a", "same"),
                history.make_event_id("chat", "tools:node-b", "same"),
                history.make_event_id("chat", "tools:node-a", "other"),
            }
        )
        == 4
    )


@pytest.mark.parametrize(
    "parts", [("", "node", "call"), ("chat", "", "call"), ("chat", "node", ""), ("chat", None, "call")]
)
def test_event_id_rejects_missing_identity_instead_of_random_fallback(parts):
    with pytest.raises(ValueError):
        history.make_event_id(*parts)


def test_legacy_migration_preserves_data_and_distinguishes_same_child_call_ids():
    original = legacy_history(
        legacy_event(scope="main/task-a:worker"),
        legacy_event(scope="main/task-b:worker", seq=4, order=3, status="denied", decision="Deny"),
    )
    before = deepcopy(original)
    result = history.migrate_history(original, "chat")
    assert result["schema"] == 5
    assert result["root"] == "chat"
    assert [{key: turn[key] for key in ("id", "text")} for turn in result["turns"]] == before["turns"]
    assert result["events"][0]["event_id"] == "dad29b69-d57b-55b6-a813-c3814632ea80"
    assert len({event["event_id"] for event in result["events"]}) == 2
    assert [event["status"] for event in result["events"]] == ["executed_success", "denied"]
    assert [event["tool_call_id"] for event in result["events"]] == ["same", "same"]
    for old, new in zip(before["events"], result["events"], strict=True):
        assert new == {
            key: value for key, value in old.items() if key not in {"scope", "parent_call_id", "root_session_id"}
        } | {"event_id": new["event_id"]}
    assert original == before
    assert history.migrate_history(original, "chat") == result
    assert history.migrate_history(result, "chat") == result


@pytest.mark.parametrize("status", ["unknown", "attempted"])
def test_legacy_unfinished_actions_are_never_guessed_or_modified(status):
    value = legacy_history(legacy_event(status=status))
    before = deepcopy(value)
    with pytest.raises(ValueError, match="unfinished"):
        history.migrate_history(value, "chat")
    assert value == before


@pytest.mark.parametrize(
    "mutation",
    [
        {"root": "other"},
        {"schema": 2},
        {"events": [legacy_event(root_session_id="other")]},
        {"events": [legacy_event(arguments=[])]},
        {"events": [legacy_event(seq=True)]},
        {"events": [legacy_event(output="should not be imported")]},
        {"turns": [{"id": "turn", "text": 1}]},
        {"truncated": 0},
    ],
)
def test_legacy_corrupt_or_cross_session_data_is_not_silently_normalized(mutation):
    with pytest.raises(ValueError):
        history.migrate_history(legacy_history(legacy_event()) | mutation, "chat")


def test_empty_history_and_invalid_current_history_are_checked():
    assert history.migrate_history({}, "chat") == {
        "schema": 5,
        "root": "chat",
        "events": [],
        "turns": [],
        "truncated": False,
        "limits": {"actions": 200, "turns": 20, "bytes": 32768},
        "clock": 0,
        "pruned": {"events": -1, "turns": -1},
        "current_turn": None,
    }
    value = history.migrate_history(legacy_history(legacy_event()), "chat")
    for field in ["scope", "output"]:
        broken = deepcopy(value)
        broken["events"][0][field] = "invalid"
        with pytest.raises(ValueError):
            history.migrate_history(broken, "chat")


def test_repeated_or_stale_graph_updates_cannot_revert_a_completed_event():
    completed = history.migrate_history(legacy_history(legacy_event()), "chat")
    stale = deepcopy(completed)
    stale["events"][0].update(status="attempted", seq=1, decision="")
    result = history.merge_history(completed, stale)
    assert result["events"][0]["status"] == "executed_success"
    assert result["events"][0]["seq"] == 2
    assert history.merge_history(result, result) == result


def test_turn_window_keeps_last_twenty_complete_prompts():
    value = history.empty_history("chat") | {
        "turns": [{"id": str(i), "text": f"user-{i}", "order": i + 1} for i in range(21)],
        "clock": 21,
        "current_turn": {"id": "20", "order": 21},
    }
    result = history.merge_history({}, value)
    assert len(result["turns"]) == 20
    assert result["turns"][0]["text"] == "user-1"
    assert result["turns"][-1]["text"] == "user-20"
    assert result["truncated"] is True
