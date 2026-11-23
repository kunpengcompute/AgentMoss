"""Agent / session identity recorded by ``POST /api/v1/analyze``.

AgentGate builds its agent id as ``f"{agent_type}:{hostname}"``.  The hostname
used to come from ``socket.gethostname()`` — the *gateway's own* machine — so
every agent of a given type behind one gateway collapsed into a single
``agent_id``, and a record could never say which machine it came from.

Measured in the field: a QwenPaw running on Windows, talking to a gateway in
WSL, was filed as ``qwenpaw:kenhkl`` — the WSL host.  Two same-type agents
behind that gateway were therefore indistinguishable.

Rule: what the caller reports about itself wins; the gateway's own view is the
fallback, so callers that report nothing behave exactly as before.
"""

import os
import socket
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

TEST_DB_PATH = "/tmp/agent_gate_test_identity.db"
MOSS_PATCH = "agent_gate.shared.client.moss_client.MossClient"

MOSS_ALLOW = {
    "decision": "Allow",
    "reason": "Safe operation",
    "risk_level": "low",
    "risk_type": "safe",
    "violated_layers": [],
    "policy": "",
    "confidence": 95,
    "analysis_duration_ms": 1.0,
}


@pytest.fixture(autouse=True)
def clean_db():
    """Fresh database per test (module-level singleton)."""
    import agent_gate.shared.storage.db as db_module

    def _remove():
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(TEST_DB_PATH + suffix)
            except OSError:
                pass

    def _reset():
        _remove()
        db_module._db = None

    _reset()
    yield
    _reset()


@pytest.fixture
def client():
    from agent_gate.shared.config import reload_config
    from agent_gate.shared.server.app import create_app
    from agent_gate.shared.storage.db import get_db

    os.environ["AGENT_GATE_STORAGE_DB_PATH"] = TEST_DB_PATH
    reload_config()
    get_db(TEST_DB_PATH).init_schema()
    return TestClient(create_app())


def _analyze(client, **overrides) -> None:
    """Post one analyze request."""
    payload = {
        "agent_type": "qwenpaw",
        "session_id": "sess-1",
        "tool_name": "bash",
        "command": "ls -la",
        "cwd": "/tmp",
    }
    payload.update(overrides)
    with patch(MOSS_PATCH) as mock_cls:
        mock_cls.return_value.analyze = AsyncMock(return_value=dict(MOSS_ALLOW))
        response = client.post("/api/v1/analyze", json=payload)
    assert response.status_code == 200, response.text


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    connection = sqlite3.connect(TEST_DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def test_reported_hostname_names_the_agent(client):
    _analyze(client, metadata={"hostname": "WIN-BOX", "os_type": "windows"})

    agents = _rows("SELECT id, hostname FROM agents")

    assert [a["id"] for a in agents] == ["qwenpaw:WIN-BOX"]
    assert agents[0]["hostname"] == "WIN-BOX"


def test_same_type_agents_on_different_hosts_stay_apart(client):
    """Two QwenPaw agents behind one gateway must not merge into one record."""
    _analyze(client, session_id="win", command="dir",
             metadata={"hostname": "WIN-BOX"})
    _analyze(client, session_id="wsl", command="ls",
             metadata={"hostname": "wsl-host"})

    assert {a["id"] for a in _rows("SELECT id FROM agents")} == {
        "qwenpaw:WIN-BOX",
        "qwenpaw:wsl-host",
    }


def test_reported_os_type_is_stored_on_the_session(client):
    """sessions.os_type used to be permanently empty — never passed through."""
    _analyze(client, metadata={"hostname": "WIN-BOX", "os_type": "windows"})

    sessions = _rows("SELECT id, agent_id, os_type FROM sessions")

    assert len(sessions) == 1
    assert sessions[0]["os_type"] == "windows"
    assert sessions[0]["agent_id"] == "qwenpaw:WIN-BOX"


def test_falls_back_to_gateway_host_when_caller_reports_nothing(client):
    """Callers that predate this change keep the old behaviour."""
    _analyze(client)

    assert _rows("SELECT id FROM agents")[0]["id"] == (
        f"qwenpaw:{socket.gethostname()}"
    )
