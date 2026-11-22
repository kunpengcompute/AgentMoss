"""Guard cached-call audit completeness and the documented trace response."""

import json

import pytest
from fastapi.testclient import TestClient

from agent_gate.shared.client.cache import DecisionCache
from agent_gate.shared.server.app import create_app
from agent_gate.shared.server import routes
from agent_gate.shared.storage.db import Database
from agent_gate.shared.storage.repository import Repository


@pytest.fixture
def audit_client(tmp_path):
    db = Database(str(tmp_path / "audit.db"))
    db.init_schema()
    repo = Repository(db)
    cache = DecisionCache()
    app = create_app()
    app.dependency_overrides[routes.get_repo] = lambda: repo
    app.dependency_overrides[routes.get_cache] = lambda: cache
    # Cache hits must not consult an upstream service. Use an object without
    # analyze so accidental upstream calls fail instead of returning fake data.
    app.dependency_overrides[routes.get_moss_client] = object
    with TestClient(app) as client:
        yield client, repo, cache
    db.close()


def test_cached_calls_keep_audit_snapshots(audit_client):
    client, repo, cache = audit_client
    result = {
        "decision": "Allow", "reason": "Read-only listing", "risk_level": "low",
        "risk_type": "", "violated_layers": [], "confidence": 100,
        "analysis_duration_ms": 12345, "policy": "read_only",
    }
    cache.set("cache-audit", "/tmp", "ls -la", result)
    request = {
        "agent_type": "acceptance", "session_id": "cache-audit",
        "tool_name": "bash", "command": "ls -la", "cwd": "/tmp",
    }
    for _ in range(2):
        response = client.post("/api/v1/analyze", json=request)
        assert response.status_code == 200
        assert response.json()["source"] == "cache"

    calls = repo.get_session_traces("cache-audit")
    assert len(calls) == 2
    assert response.json()["policy"] == "read_only"
    assert [call["seq"] for call in calls] == [1, 2]
    assert all(call["called_at"] for call in calls)
    assert all(call["analysis_duration_ms"] < 12345 for call in calls)
    assert all(json.loads(call["moss_response"])["source"] == "cache" for call in calls)
    assert repo.get_session("cache-audit")["tool_call_count"] == 2
    snapshots = repo._db.execute(
        "SELECT tool_call_id, os_info FROM system_snapshots ORDER BY id"
    ).fetchall()
    assert [row["tool_call_id"] for row in snapshots] == [call["id"] for call in calls]
    assert all(json.loads(row["os_info"])["system"] for row in snapshots)

@pytest.mark.parametrize("count", [0, 2])
def test_traces_response_has_compatible_ordered_alias(audit_client, count):
    client, repo, _ = audit_client
    aid = repo.get_or_create_agent("acceptance", "reported-host", "1.0")
    repo.get_or_create_session("trace-alias", aid)
    for index in range(count):
        repo.insert_tool_call("trace-alias", {
            "tool_name": "bash", "command": f"echo {index}",
            "action_type": "bash", "action_detail": f"echo {index}",
        }, {"decision": "Allow", "risk_level": "low"})
    response = client.get("/api/v1/sessions/trace-alias/traces")
    assert response.status_code == 200
    payload = response.json()
    assert payload["tool_call_count"] == count
    assert payload["traces"] == payload["tool_calls"]
    assert [call["seq"] for call in payload["traces"]] == list(range(1, count + 1))
