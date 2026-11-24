"""Dedicated Gate authentication, response correlation, and audit records."""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from agent_gate.shared.client.moss_client import MossClient
from agent_gate.shared.normalizer import Normalizer
from agent_gate.shared.server import routes
from agent_gate.shared.server.models import SystemContext
from agent_gate.shared.storage.db import Database
from agent_gate.shared.storage.repository import Repository


@pytest.fixture
def dedicated_gate(monkeypatch):
    monkeypatch.setenv("AGENT_GATE_MOSS_GATE_TOKEN", "gate-test-token")
    monkeypatch.setenv("AGENT_GATE_MOSS_TOKEN", "global-test-token")


@pytest.mark.parametrize(
    ("outcome", "expected_decision", "expected_numbered"),
    [
        ("allow", "Allow", True),
        ("deny", "Deny", True),
        ("foreign-deny", "Deny", False),
        ("missing-echo", "Deny", False),
        ("trusted-rate-limit", "Deny", True),
        ("plain-rate-limit", "Deny", False),
        ("foreign-rate-limit", "Deny", False),
        ("identity-rejected", "Deny", False),
        ("unavailable", "Deny", True),
        ("refreshed-busy", "Deny", True),
        ("refreshed-unverified-busy", "Deny", False),
    ],
)
async def test_gate_protocol_response_reaches_audit_safely(
    outcome, expected_decision, expected_numbered, dedicated_gate, tmp_path, monkeypatch,
):
    foreign_id = "f" * 32
    foreign_marker = "other-session-confirmed-evidence"
    sent = []
    refresh = outcome.startswith("refreshed-")

    def upstream(request):
        payload = json.loads(request.content)
        sent.append(payload)
        call_id = payload["gate_call_id"]
        expected_token = "refreshed-test-token" if refresh and len(sent) == 2 else "gate-test-token"
        assert request.headers["Authorization"] == f"Bearer {expected_token}"
        assert request.headers["X-AgentGate-Require-Identity"] == "1"
        assert request.headers["X-AgentGate-Call-Id"] == call_id
        assert len(call_id) == 32 and all(char in "0123456789abcdef" for char in call_id)
        assert payload["gate_call_time_us"] > 0
        assert payload["gate_delivery_uncertain"] is False
        assert "gate_call_id" not in payload["metadata"]
        assert "gate_delivery_uncertain" not in payload["metadata"]
        if refresh:
            if len(sent) == 1:
                monkeypatch.setenv("AGENT_GATE_MOSS_GATE_TOKEN", "refreshed-test-token")
                return httpx.Response(401)
            echoed_id = call_id if outcome == "refreshed-busy" else foreign_id
            return httpx.Response(503, headers={"X-AgentGate-Busy-Call-Id": echoed_id})
        if outcome == "unavailable":
            raise httpx.ReadTimeout("no analysis response", request=request)
        if outcome.endswith("rate-limit"):
            headers = {"Retry-After": "1"}
            if outcome != "plain-rate-limit":
                headers["X-AgentGate-Rate-Limited-Call-Id"] = (
                    call_id if outcome == "trusted-rate-limit" else foreign_id
                )
            return httpx.Response(429, headers=headers)
        if outcome == "identity-rejected":
            return httpx.Response(403)
        if outcome == "foreign-deny":
            return httpx.Response(200, json={
                "gate_call_id": foreign_id,
                "decision": "Deny",
                "reason": foreign_marker,
                "violation_graph": {"detail": foreign_marker},
                "_gate_call_id": foreign_id,
            })
        result = {"decision": "Deny" if outcome == "deny" else "Allow", "reason": "checked"}
        if outcome != "missing-echo":
            result["gate_call_id"] = call_id
        result["_gate_call_id"] = foreign_id
        result["_gate_protocol_rejected"] = True
        return httpx.Response(200, json=result)

    monkeypatch.setenv("AGENT_GATE_INSTANCE_ID", "protocol-test")
    db = Database(str(tmp_path / "audit.db"))
    db.init_schema()
    repo = Repository(db)
    moss = MossClient(base_url="http://moss.test", max_retries=0)
    await moss.close()
    moss._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_repo] = lambda: repo
    app.dependency_overrides[routes.get_moss_client] = lambda: moss
    app.dependency_overrides[routes.get_normalizer] = lambda: Normalizer(repo)
    app.dependency_overrides[routes.get_orchestrator] = lambda: SimpleNamespace(collect=SystemContext)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gate.test") as gate:
            response = await gate.post("/api/v1/analyze", json={
                "session_id": "protocol-audit",
                "agent_type": "test",
                "tool_name": "bash",
                "command": "echo ok",
                "metadata": {"gate_call_id": foreign_id, "gate_delivery_uncertain": True},
            })
        assert response.status_code == 200
        assert response.json()["decision"] == expected_decision
        assert "gate_call_id" not in response.json()
        assert len(sent) == (2 if refresh else 1)
        assert all(entry["gate_call_id"] == sent[0]["gate_call_id"] for entry in sent)
        assert all(entry["gate_call_time_us"] == sent[0]["gate_call_time_us"] for entry in sent)
        [row] = repo.get_session_traces("protocol-audit")
        stored = json.loads(row["moss_response"])
        assert row["decision"] == expected_decision
        assert stored["decision"] == expected_decision
        assert foreign_id not in response.text and foreign_marker not in response.text
        assert foreign_id not in row["moss_response"] and foreign_marker not in row["moss_response"]
        assert foreign_marker not in row["reason"]
        if expected_numbered:
            assert stored["_gate_call_id"] == sent[0]["gate_call_id"]
            assert stored["_gate_call_time_us"] == sent[0]["gate_call_time_us"]
            assert "_gate_protocol_rejected" not in stored
        else:
            assert stored["_gate_protocol_rejected"] is True
            assert "_gate_call_id" not in stored and "_gate_call_time_us" not in stored
            assert stored["risk_type"] == "service_unavailable"
    finally:
        await moss.close()
        db.close()


async def test_dedicated_retry_preserves_identity_and_uncertainty(dedicated_gate):
    sent = []

    def upstream(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if len(sent) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        if len(sent) == 2:
            raise httpx.ConnectTimeout("not sent", request=request)
        if len(sent) == 3:
            return httpx.Response(401)
        return httpx.Response(200, json={"gate_call_id": payload["gate_call_id"], "decision": "Allow"})

    moss = MossClient(base_url="http://moss.test", max_retries=2)
    await moss.close()
    moss._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    moss._retry_delay = 0
    try:
        request = {"session_id": "retry", "a_next": {"called_at": "2026-01-01T00:00:00Z"}}
        first = await moss.analyze(request, accepted_at_us=123456)
        second = await moss.analyze(request, accepted_at_us=123456)
    finally:
        await moss.close()
    assert first["decision"] == second["decision"] == "Allow"
    assert [entry["gate_delivery_uncertain"] for entry in sent] == [False, True, True, True, False]
    assert all(entry["gate_call_id"] == first["_gate_call_id"] for entry in sent[:4])
    assert second["_gate_call_id"] != first["_gate_call_id"]
    assert all(entry["gate_call_time_us"] == 123456 for entry in sent)
    assert all(entry["a_next"] == request["a_next"] for entry in sent)
    assert "gate_call_id" not in request


@pytest.mark.parametrize(
    ("prior_timeout", "status_code", "busy_header", "expected_uncertain"),
    [
        (False, 503, "match", False),
        (True, 503, "match", True),
        (False, 503, "missing", True),
        (False, 503, "foreign", True),
        (False, 500, "match", True),
    ],
)
async def test_only_correlated_admission_503_preserves_delivery_state(
    dedicated_gate, prior_timeout, status_code, busy_header, expected_uncertain,
):
    sent = []

    def upstream(request):
        payload = json.loads(request.content)
        sent.append(payload)
        call_id = payload["gate_call_id"]
        if prior_timeout and len(sent) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        if len(sent) == 1 + prior_timeout:
            headers = {}
            if busy_header != "missing":
                headers["X-AgentGate-Busy-Call-Id"] = call_id if busy_header == "match" else "f" * 32
            return httpx.Response(status_code, headers=headers)
        return httpx.Response(200, json={
            "gate_call_id": call_id,
            "decision": "Deny" if payload["gate_delivery_uncertain"] else "Allow",
        })

    moss = MossClient(base_url="http://moss.test", max_retries=1 + prior_timeout)
    await moss.close()
    moss._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    moss._retry_delay = 0
    try:
        result = await moss.analyze({"session_id": "admission-retry"}, accepted_at_us=123456)
    finally:
        await moss.close()

    assert result["decision"] == ("Deny" if expected_uncertain else "Allow")
    expected_statuses = [False, True, expected_uncertain] if prior_timeout else [False, expected_uncertain]
    assert [entry["gate_delivery_uncertain"] for entry in sent] == expected_statuses
    assert all(entry["gate_call_id"] == result["_gate_call_id"] for entry in sent)
    assert all(entry["gate_call_time_us"] == 123456 for entry in sent)


async def test_dedicated_retry_after_auth_failure_cannot_authorize_audit(dedicated_gate):
    count = 0

    def upstream(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(401)
        raise httpx.ReadTimeout("no verified identity", request=request)

    moss = MossClient(base_url="http://moss.test", max_retries=0)
    await moss.close()
    moss._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    try:
        result = await moss.analyze({"session_id": "unauthorized"})
    finally:
        await moss.close()
    assert count == 2
    assert result["decision"] == "Deny"
    assert result["_gate_protocol_rejected"] is True
    assert "_gate_call_id" not in result


async def test_dedicated_review_uses_same_identity_without_analyze_fields(dedicated_gate):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={})

    moss = MossClient(base_url="http://moss.test", max_retries=0)
    await moss.close()
    moss._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    try:
        await moss.review(agent_id="test", db_path="/tmp/gate-test.db")
    finally:
        await moss.close()
    [request] = seen
    assert request.url.path == "/api/v1/review"
    assert request.headers["Authorization"] == "Bearer gate-test-token"
    assert request.headers["X-AgentGate-Require-Identity"] == "1"
    assert "X-AgentGate-Call-Id" not in request.headers
    assert json.loads(request.content) == {"agent_id": "test", "db_path": "/tmp/gate-test.db"}


async def test_legacy_response_does_not_require_echo_or_supply_audit_identity(monkeypatch):
    monkeypatch.delenv("AGENT_GATE_MOSS_GATE_TOKEN", raising=False)
    monkeypatch.setattr("agent_gate.shared.client.moss_client.resolve_moss_token", lambda: "legacy-test")

    def upstream(request):
        assert request.headers["Authorization"] == "Bearer legacy-test"
        assert "X-AgentGate-Require-Identity" not in request.headers
        assert json.loads(request.content) == {"session_id": "legacy"}
        return httpx.Response(200, json={"decision": "Allow", "_gate_call_id": "untrusted"})

    moss = MossClient(base_url="http://moss.test", max_retries=0)
    await moss.close()
    moss._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    try:
        result = await moss.analyze({"session_id": "legacy"})
    finally:
        await moss.close()
    assert result == {"decision": "Allow"}
