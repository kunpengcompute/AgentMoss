"""Long sessions keep recent analysis context and complete audit traces."""

import asyncio

import pytest

from agent_gate.shared.normalizer import Normalizer
from agent_gate.shared.server.models import AgentGateInput, SystemContext
from agent_gate.shared.storage.db import Database
from agent_gate.shared.storage.repository import Repository


@pytest.fixture
def long_session_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    db = Database(str(tmp_path / "gate.db"))
    db.init_schema()
    repo = Repository(db)
    agent = repo.get_or_create_agent("long-session", "reported-host", "1.0")
    repo.get_or_create_session("long-history", agent)
    for i in range(1, 106):
        command = "cat /tmp/recent.conf" if i == 105 else f"echo {i}"
        repo.insert_tool_call("long-history", {
            "tool_name": "bash", "command": command,
            "action_type": "bash", "action_detail": command,
        }, {"decision": "Allow", "risk_level": "low"})
    yield repo
    db.close()


def test_normalizer_keeps_latest_read_in_bounded_chronological_history(long_session_repo):
    payload = asyncio.run(Normalizer(long_session_repo).normalize(
        AgentGateInput(
            agent_type="long-session", session_id="long-history", instance_id="test-instance",
            tool_name="bash", command="echo updated > /tmp/recent.conf",
        ), SystemContext(),
    ))
    history = payload["action_history"]
    assert len(history) == 100
    assert [h["action_detail"] for h in history] == [
        *(f"echo {i}" for i in range(6, 105)), "cat /tmp/recent.conf",
    ]
    assert all(h["called_at"] for h in history)


def test_cross_session_history_uses_recent_window_without_truncating_audit(long_session_repo):
    histories = long_session_repo.get_recent_session_histories(
        exclude_session_id="current", limit=1, per_session_limit=3,
    )
    assert len(histories) == 1
    assert [h["action_detail"] for h in histories[0]["action_history"]] == [
        "echo 103", "echo 104", "cat /tmp/recent.conf",
    ]
    traces = long_session_repo.get_session_traces("long-history")
    assert len(traces) == 105
    assert [call["seq"] for call in traces] == list(range(1, 106))
    assert long_session_repo.get_session("long-history")["tool_call_count"] == 105
