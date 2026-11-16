"""A root run and its SDK children reuse one context and one history view."""

from copy import copy

import pytest
from test_native_sdk import setup

from agentmoss_deepagents import context
from agentmoss_deepagents.history import KEY


@pytest.fixture
async def resources(tmp_path, monkeypatch):
    runtime, _ = setup(tmp_path)
    monkeypatch.setattr(context, "get_config", lambda: {"configurable": {"thread_id": "chat"}})
    try:
        yield runtime
    finally:
        await runtime.aclose()


def info(prompt="original"):
    return {"moss_info": {"session_id": "chat", "user_prompt": prompt}}


def test_repeated_initialization_reuses_object_and_records_one_root_turn(resources):
    ctx = info()
    first = context.begin_run(ctx, {}, resources)
    second = context.begin_run(ctx, {KEY: first.history.value}, resources)
    assert isinstance(first, context.RunContext)
    assert first is second is context.current_run(ctx, "chat")
    assert first.history is second.history
    assert first.owner is ctx["moss_info"]
    assert len(first.history.value["turns"]) == 1
    assert first.gate_session_id == "deepagents:chat"


def test_new_root_dictionary_restores_history_but_gets_a_new_turn(resources):
    first = context.begin_run(info(), {}, resources)
    second = context.begin_run(info("next"), {KEY: first.history.value}, resources)
    assert first is not second and first.history is not second.history
    assert first.turn_id != second.turn_id
    assert [t["text"] for t in second.history.value["turns"]] == ["original", "next"]


@pytest.mark.parametrize("mutation", [{"session_id": "other"}, {"user_prompt": "changed"}])
def test_context_cannot_change_identity_or_root_prompt_mid_run(resources, mutation):
    ctx = info()
    context.begin_run(ctx, {}, resources)
    ctx["moss_info"].update(mutation)
    with pytest.raises(ValueError):
        context.current_run(ctx)
    with pytest.raises(ValueError):
        context.begin_run(ctx, {}, resources)


def test_a_run_cannot_be_attached_to_a_copy_of_the_owner_dictionary(resources):
    ctx = info()
    context.begin_run(ctx, {}, resources)
    copied = {"moss_info": copy(ctx["moss_info"])}
    with pytest.raises(ValueError):
        context.current_run(copied)
    with pytest.raises(ValueError):
        context.begin_run(copied, {}, resources)


def test_foreign_reserved_value_is_not_silently_replaced(resources):
    ctx = info()
    ctx["moss_info"]["_agentmoss_run"] = {"forged": True}
    with pytest.raises(ValueError):
        context.begin_run(ctx, {}, resources)


def test_raw_prompt_mutation_is_detected_even_when_both_redact_to_same_text(resources):
    ctx = info("sk-first.synthetic")
    run = context.begin_run(ctx, {}, resources)
    assert run.user_prompt == "sk-first.synthetic"
    assert run.history.value["turns"][0]["text"] == "[redacted]"
    ctx["moss_info"]["user_prompt"] = "sk-second.synthetic"
    with pytest.raises(ValueError):
        context.current_run(ctx)
