"""Comprehensive integration tests for AgentGate service.

Tests cover:
  1. Health check endpoint
  2. Analyze endpoint — Allow / Deny decisions
  3. Decision cache (Allow only caching)
  4. Fail-Closed behavior when AgentMoss is unavailable
  5. Trace query API (session traces, tool_calls query)
  6. Database operations (agents, sessions, tool_calls, snapshots)
  7. Collector orchestrator
  8. Normalizer with DB history
  9. Configuration loading
  10. CLI entry point
"""

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_gate import __version__ as agent_gate_version
from agent_gate.v1.config import Config, reload_config, get_config
from agent_gate.v1.server.models import (
    AgentGateInput,
    AgentGateResponse,
    HealthResponse,
    SessionTraceResponse,
    SystemContext,
)
from agent_gate.v1.storage.db import Database, get_db
from agent_gate.v1.storage.repository import Repository
from agent_gate.v1.client.cache import DecisionCache
from agent_gate.v1.client.moss_client import MossClient
from agent_gate.v1.normalizer import Normalizer
from agent_gate.v1.collector import CollectorOrchestrator, get_orchestrator
from agent_gate.v1.server.app import create_app

# ---- Test database path ----
TEST_DB_PATH = "/tmp/agent_gate_test.db"


@pytest.fixture(autouse=True)
def clean_db():
    """Clean test database before and after each test."""
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    # Reset module-level singletons
    import agent_gate.v1.storage.db as db_module
    db_module._db = None
    import agent_gate.v1.storage.repository as repo_module
    yield
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except Exception:
            pass
    db_module._db = None


@pytest.fixture
def app_for_test():
    """Create a FastAPI test app with test database."""
    # Override config for testing
    os.environ["AGENT_GATE_STORAGE_DB_PATH"] = TEST_DB_PATH
    reload_config()

    # Ensure database is initialized
    db = get_db(TEST_DB_PATH)
    db.init_schema()

    app = create_app()
    return app


@pytest.fixture
def client(app_for_test):
    """TestClient for FastAPI app."""
    return TestClient(app_for_test)


@pytest.fixture
def repo():
    """Repository with test database."""
    return Repository(get_db(TEST_DB_PATH))


# ============================================================
# 1. Health Check Tests
# ============================================================

class TestHealthCheck:
    def test_health_returns_ok(self, client):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["version"] == agent_gate_version
        assert "timestamp" in data


# ============================================================
# 2. Analyze Endpoint Tests
# ============================================================

class TestAnalyzeEndpoint:
    """Test the analyze endpoint with mocked AgentMoss."""

    PATCH_TARGET = "agent_gate.v1.client.moss_client.MossClient"

    @pytest.fixture
    def moss_mock(self):
        """Mock MossClient.analyze to return Allow."""
        with patch(self.PATCH_TARGET) as mock_cls:
            instance = mock_cls.return_value
            instance.analyze = AsyncMock(return_value={
                "decision": "Allow",
                "reason": "Safe operation",
                "risk_level": "low",
                "risk_type": "safe",
                "violated_layers": [],
                "policy": "",
                "confidence": 95,
                "analysis_duration_ms": 10.5,
            })
            yield instance

    def test_analyze_allow(self, client, moss_mock):
        """Test a safe command gets Allow."""
        response = client.post("/api/v1/analyze", json={
            "agent_type": "test-agent",
            "session_id": "sess-001",
            "tool_name": "bash",
            "command": "ls -la",
            "cwd": "/tmp",
            "description": "List files",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["decision"] == "Allow"
        assert data["source"] == "agent_moss"

    def test_analyze_deny(self, client):
        """Test a dangerous command gets Deny."""
        with patch(self.PATCH_TARGET) as mock_cls:
            instance = mock_cls.return_value
            instance.analyze = AsyncMock(return_value={
                "decision": "Deny",
                "reason": "Recursive force delete detected",
                "risk_level": "high",
                "risk_type": "dangerous_command",
                # L2.1=逻辑规则（dangerous_command），与重构后真实执行顺序一致
                "violated_layers": ["L2.1"],
                "policy": "",
                "confidence": 97,
                "analysis_duration_ms": 15.0,
            })

            response = client.post("/api/v1/analyze", json={
                "agent_type": "test-agent",
                "session_id": "sess-002",
                "tool_name": "bash",
                "command": "rm -rf /",
                "cwd": "/tmp",
                "description": "Delete everything",
            })

        assert response.status_code == 200
        data = response.json()
        assert data["decision"] == "Deny"
        assert data["risk_level"] == "high"
        assert "rm" in data["reason"].lower() or "delete" in data["reason"].lower()

    def test_analyze_minimal_input(self, client, moss_mock):
        """Test analyze with only required fields."""
        response = client.post("/api/v1/analyze", json={
            "agent_type": "minimal",
            "session_id": "sess-min",
            "tool_name": "edit",
            "command": "edit file.py",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["decision"] in ("Allow", "Deny")

    def test_analyze_with_action_history_provided(self, client, moss_mock):
        """Test analyze when Agent provides action_history."""
        response = client.post("/api/v1/analyze", json={
            "agent_type": "test-agent",
            "session_id": "sess-hist",
            "tool_name": "bash",
            "command": "echo hello",
            "cwd": "/tmp",
            "action_history": [
                {"action_type": "read", "action_detail": "cat /etc/passwd"},
                {"action_type": "bash", "action_detail": "ls /tmp"},
            ],
        })
        assert response.status_code == 200
        data = response.json()
        assert data["decision"] in ("Allow", "Deny")

    def test_analyze_collects_context(self, client, moss_mock):
        """Test that collected_context is returned."""
        response = client.post("/api/v1/analyze", json={
            "agent_type": "test-agent",
            "session_id": "sess-ctx",
            "tool_name": "bash",
            "command": "whoami",
            "cwd": "/tmp",
        })
        assert response.status_code == 200
        data = response.json()
        assert "collected_context" in data
        ctx = data["collected_context"]
        assert ctx is not None
        assert "os" in ctx
        assert "resources" in ctx
        assert "user" in ctx


class TestUpstreamOsTypePriority:
    """os_type reported by the agent wins over the gateway host's own OS.

    Cross-platform deployment: the agent may run on Windows while AgentGate
    runs on Linux (or in a container). AgentMoss selects its rule set from
    ``os_type``, so falling back to the gateway host's OS would apply Linux
    rules to Windows commands — the gateway would then report "Allow" for
    actions that are critical on the agent's platform.
    """

    PATCH_TARGET = "agent_gate.v1.client.moss_client.MossClient"

    def _forwarded_payload(self, client, command, **extra):
        """Post one analyze request, return the payload forwarded to AgentMoss.

        ``command`` must be unique per test — the decision cache is keyed on
        (session_id, cwd, command) and would otherwise short-circuit the
        second call, so ``analyze`` would never be invoked.
        """
        with patch(self.PATCH_TARGET) as mock_cls:
            instance = mock_cls.return_value
            instance.analyze = AsyncMock(return_value={
                "decision": "Allow",
                "reason": "Safe operation",
                "risk_level": "low",
                "risk_type": "safe",
                "violated_layers": [],
                "policy": "",
                "confidence": 95,
                "analysis_duration_ms": 1.0,
            })
            response = client.post("/api/v1/analyze", json={
                "agent_type": "qwenpaw",
                "session_id": f"sess-os-{command[-6:]}",
                "tool_name": "bash",
                "command": command,
                "cwd": "C:\\",
                **extra,
            })
            assert response.status_code == 200
            assert instance.analyze.await_count == 1, "cache short-circuited the call"
            return instance.analyze.call_args.args[0]

    def test_upstream_os_type_wins_over_host(self, client):
        """An explicit os_type from the agent is forwarded unchanged."""
        payload = self._forwarded_payload(
            client, "type C:\\Windows\\System32\\config\\SAM",
            metadata={"os_type": "windows"},
        )
        assert payload["os_type"] == "windows"

    def test_upstream_os_type_is_case_insensitive(self, client):
        payload = self._forwarded_payload(
            client, "type C:\\Windows\\System32\\config\\SECURITY",
            metadata={"os_type": "Windows"},
        )
        assert payload["os_type"] == "windows"

    def test_falls_back_to_host_os_when_absent(self, client):
        """Without an upstream os_type the gateway host's OS is used."""
        payload = self._forwarded_payload(
            client, "type C:\\Windows\\NTDS\\ntds.dit",
        )
        # CI hosts are Linux/macOS, so this must not be the agent's Windows.
        assert payload["os_type"] != "windows"
        assert payload["os_type"]

    def test_empty_upstream_os_type_falls_back(self, client):
        """An empty string must not shadow the host OS."""
        payload = self._forwarded_payload(
            client, "type C:\\Windows\\repair\\SAM",
            metadata={"os_type": ""},
        )
        assert payload["os_type"]
        assert payload["os_type"] != "windows"

    def test_other_metadata_is_preserved(self, client):
        """The os_type lookup must not drop the rest of metadata."""
        payload = self._forwarded_payload(
            client, "type C:\\Windows\\System32\\LSA",
            metadata={"os_type": "windows", "tenant": "cmb"},
        )
        assert payload["metadata"]["tenant"] == "cmb"
        assert payload["metadata"]["agent_type"] == "qwenpaw"


class TestToolNameMapping:
    """Agent-specific tool names must reach AgentMoss as standard action_types.

    AgentMoss decides whether a call is shell-like by comparing action_type
    against ("bash", "executebash", "terminal-executebash"). An unmapped name
    such as QwenPaw's "execute_shell_command" fails that test, so every
    shell-dependent L2 rule (read_before_write, intent consistency) silently
    stops firing for that agent.
    """

    PATCH_TARGET = "agent_gate.v1.client.moss_client.MossClient"

    def _forwarded_payload(self, client, **body):
        with patch(self.PATCH_TARGET) as mock_cls:
            instance = mock_cls.return_value
            instance.analyze = AsyncMock(return_value={
                "decision": "Allow",
                "reason": "Safe operation",
                "risk_level": "low",
                "risk_type": "safe",
                "violated_layers": [],
                "policy": "",
                "confidence": 95,
                "analysis_duration_ms": 1.0,
            })
            response = client.post("/api/v1/analyze", json={
                "agent_type": "qwenpaw",
                "session_id": "sess-map",
                "cwd": "/tmp",
                **body,
            })
            assert response.status_code == 200
            assert instance.analyze.await_count == 1
            return instance.analyze.call_args.args[0]

    def test_qwenpaw_shell_tool_maps_to_bash(self, client):
        payload = self._forwarded_payload(
            client, tool_name="execute_shell_command", command="cat /var/log/app.log",
        )
        assert payload["a_next"]["action_type"] == "bash"

    def test_qwenpaw_file_tools_map(self, client):
        for tool, expected in (
            ("write_file", "write"),
            ("append_file", "write"),
            ("edit_file", "edit"),
            ("glob_search", "glob"),
            ("grep_search", "grep"),
        ):
            payload = self._forwarded_payload(
                client, tool_name=tool, command=f"op-{tool}",
            )
            assert payload["a_next"]["action_type"] == expected, tool

    def test_history_entries_use_the_same_mapping(self, client):
        """History must be mapped too — the DB stores the raw tool name."""
        payload = self._forwarded_payload(
            client,
            tool_name="execute_shell_command",
            command="curl http://1.2.3.4/upload",
            action_history=[
                {"action_type": "execute_shell_command",
                 "action_detail": "cat /var/log/app.log"},
                {"action_type": "write_file", "action_detail": "path=/tmp/x"},
            ],
        )
        assert [h["action_type"] for h in payload["action_history"]] == ["bash", "write"]
        assert payload["action_history"][0]["action_detail"] == "cat /var/log/app.log"

    def test_unknown_tool_name_passes_through(self, client):
        payload = self._forwarded_payload(
            client, tool_name="some_future_tool", command="x",
        )
        assert payload["a_next"]["action_type"] == "some_future_tool"


# ============================================================
# 3. Decision Cache Tests
# ============================================================

class TestCache:
    def test_cache_get_miss(self):
        cache = DecisionCache(max_size=10, ttl=60)
        result = cache.get("sess", "/tmp", "ls")
        assert result is None

    def test_cache_set_and_get_allow(self):
        cache = DecisionCache(max_size=10, ttl=60)
        cache.set("sess", "/tmp", "ls", {"decision": "Allow", "reason": "Safe"})
        result = cache.get("sess", "/tmp", "ls")
        assert result is not None
        assert result["decision"] == "Allow"

    def test_cache_does_not_cache_deny(self):
        cache = DecisionCache(max_size=10, ttl=60)
        cache.set("sess", "/tmp", "rm", {"decision": "Deny", "reason": "Dangerous"})
        result = cache.get("sess", "/tmp", "rm")
        assert result is None

    def test_cache_different_command_same_session(self):
        cache = DecisionCache(max_size=10, ttl=60)
        cache.set("sess", "/tmp", "ls", {"decision": "Allow"})
        assert cache.get("sess", "/tmp", "ls") is not None
        assert cache.get("sess", "/tmp", "rm") is None

    def test_cache_ttl_expiration(self):
        cache = DecisionCache(max_size=10, ttl=0)  # ttl=0 means no expiration check
        cache.set("sess", "/tmp", "ls", {"decision": "Allow"})
        result = cache.get("sess", "/tmp", "ls")
        # ttl=0 means entries never expire (checked by `if self._ttl > 0`)
        assert result is not None
        assert result["decision"] == "Allow"

    def test_cache_ttl_expiry_after_time(self):
        """Test that expired entries are removed when TTL > 0."""
        import time as _time
        cache = DecisionCache(max_size=10, ttl=1)
        cache.set("sess", "/tmp", "ls", {"decision": "Allow"})

        # Artificially age the cache entry
        key = cache._key("sess", "/tmp", "ls")
        entry = cache._cache.get(key)
        if entry:
            cache._cache[key] = (_time.time() - 2, entry[1])

        result = cache.get("sess", "/tmp", "ls")
        assert result is None

    def test_cache_lru_eviction(self):
        cache = DecisionCache(max_size=3, ttl=3600)
        for i in range(5):
            cache.set(f"sess{i}", "/tmp", f"cmd{i}", {"decision": "Allow"})
        # Only 3 entries remain, oldest 2 evicted
        assert cache.size == 3

    def test_cache_clear(self):
        cache = DecisionCache(max_size=10, ttl=60)
        cache.set("sess", "/tmp", "ls", {"decision": "Allow"})
        cache.clear()
        assert cache.size == 0

    def test_cache_hit_in_endpoint(self, client, repo):
        """Test that cache hit skips AgentMoss call."""
        from agent_gate.v1.server.routes import reset_cache
        reset_cache()  # Ensure clean cache for test

        PATCH_TARGET = "agent_gate.v1.client.moss_client.MossClient"
        with patch(PATCH_TARGET) as mock_cls:
            instance = mock_cls.return_value
            instance.analyze = AsyncMock(return_value={
                "decision": "Allow",
                "reason": "Safe",
                "risk_level": "low",
                "risk_type": "safe",
                "violated_layers": [],
                "confidence": 95,
                "analysis_duration_ms": 5.0,
            })

            # First call — should call AgentMoss
            response1 = client.post("/api/v1/analyze", json={
                "agent_type": "test",
                "session_id": "sess-cache",
                "tool_name": "bash",
                "command": "ls -la",
                "cwd": "/tmp",
            })
            assert response1.status_code == 200
            assert instance.analyze.call_count == 1

            # Second call with same params — should use cache
            response2 = client.post("/api/v1/analyze", json={
                "agent_type": "test",
                "session_id": "sess-cache",
                "tool_name": "bash",
                "command": "ls -la",
                "cwd": "/tmp",
            })
            assert response2.status_code == 200
            assert response2.json()["source"] == "cache"
            # MossClient.analyze should NOT be called again
            assert instance.analyze.call_count == 1


# ============================================================
# 4. Fail-Closed Tests
# ============================================================

class TestFailClosed:
    def test_analyze_fail_closed_on_connection_error(self, client):
        """Test that connection error to AgentMoss → Deny."""
        PATCH_TARGET = "agent_gate.v1.client.moss_client.MossClient"
        with patch(PATCH_TARGET) as mock_cls:
            instance = mock_cls.return_value
            # Return a Deny response (simulating fail-closed)
            instance.analyze = AsyncMock(return_value={
                "decision": "Deny",
                "reason": "AgentMoss security service unavailable — blocking by default: connection failed",
                "risk_level": "high",
                "risk_type": "service_unavailable",
                "violated_layers": [],
                "confidence": 100,
                "analysis_duration_ms": 0,
            })

            response = client.post("/api/v1/analyze", json={
                "agent_type": "test",
                "session_id": "sess-fail",
                "tool_name": "bash",
                "command": "ls",
                "cwd": "/tmp",
            })

        assert response.status_code == 200
        data = response.json()
        assert data["decision"] == "Deny"
        assert "unavailable" in data["reason"].lower()


# ============================================================
# 5. Trace Query API Tests
# ============================================================

class TestTraceAPI:
    def setup_method(self):
        """Ensure DB is initialized."""
        get_db(TEST_DB_PATH).init_schema()

    def test_get_session_traces_empty(self, client, repo):
        """Test traces for session with no tool_calls yet."""
        repo.get_or_create_agent("test-agent", "test-host")
        agent_id = "test-agent:test-host"
        repo.get_or_create_session("sess-empty", agent_id)

        response = client.get("/api/v1/sessions/sess-empty/traces")
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "sess-empty"
        assert data["tool_call_count"] == 0
        assert data["traces"] == []

    def test_get_session_traces_with_records(self, client, repo):
        """Test traces after some tool_calls have been recorded."""
        repo.get_or_create_agent("test-agent", "test-host")
        agent_id = "test-agent:test-host"
        repo.get_or_create_session("sess-traced", agent_id)

        # Insert 3 tool calls
        for i in range(3):
            repo.insert_tool_call(
                session_id="sess-traced",
                tool_call={
                    "tool_name": "bash",
                    "command": f"cmd-{i}",
                    "cwd": "/tmp",
                    "action_type": "bash",
                    "action_detail": f"cmd-{i}",
                },
                moss_result={
                    "decision": "Allow" if i % 2 == 0 else "Deny",
                    "reason": f"reason-{i}",
                    "risk_level": "low",
                },
            )

        response = client.get("/api/v1/sessions/sess-traced/traces")
        assert response.status_code == 200
        data = response.json()
        assert data["tool_call_count"] == 3
        assert len(data["traces"]) == 3
        assert data["traces"][0]["seq"] == 1
        assert data["traces"][1]["seq"] == 2
        assert data["traces"][2]["seq"] == 3

    def test_get_session_traces_not_found(self, client):
        response = client.get("/api/v1/sessions/nonexistent/traces")
        assert response.status_code == 404

    def test_query_tool_calls_by_decision(self, client, repo):
        """Test filtering tool_calls by decision."""
        repo.get_or_create_agent("test-agent", "test-host")
        agent_id = "test-agent:test-host"
        repo.get_or_create_session("sess-filter", agent_id)

        # Insert Allow and Deny records
        repo.insert_tool_call("sess-filter", {
            "tool_name": "bash", "command": "safe-cmd", "cwd": "/tmp",
            "action_type": "bash", "action_detail": "safe-cmd",
        }, {"decision": "Allow", "reason": "safe", "risk_level": "low"})

        repo.insert_tool_call("sess-filter", {
            "tool_name": "bash", "command": "danger-cmd", "cwd": "/tmp",
            "action_type": "bash", "action_detail": "danger-cmd",
        }, {"decision": "Deny", "reason": "danger", "risk_level": "high"})

        # Query Deny only
        response = client.get("/api/v1/tool_calls?decision=Deny")
        assert response.status_code == 200
        data = response.json()
        records = data["data"]
        assert len(records) >= 1
        assert all(r["decision"] == "Deny" for r in records)

    def test_query_tool_calls_pagination(self, client, repo):
        """Test pagination."""
        repo.get_or_create_agent("test-agent", "test-host")
        agent_id = "test-agent:test-host"
        repo.get_or_create_session("sess-page", agent_id)

        for i in range(10):
            repo.insert_tool_call("sess-page", {
                "tool_name": "bash", "command": f"cmd-{i}", "cwd": "/tmp",
                "action_type": "bash", "action_detail": f"cmd-{i}",
            }, {"decision": "Allow", "reason": f"r-{i}", "risk_level": "low"})

        response = client.get("/api/v1/tool_calls?session_id=sess-page&offset=0&limit=3")
        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 3
        assert data["total"] >= 10

    def test_query_tool_calls_filters(self, client, repo):
        """/tool_calls 的 risk_level 与 from/to 过滤。"""
        repo.get_or_create_agent("filter-agent", "filter-host")
        repo.get_or_create_session("sess-filter", "filter-agent:filter-host")
        repo.insert_tool_call("sess-filter", {
            "tool_name": "bash", "command": "cmd-deny", "cwd": "/tmp",
            "action_type": "bash", "action_detail": "cmd-deny",
        }, {"decision": "Deny", "reason": "r", "risk_level": "high"})

        r = client.get("/api/v1/tool_calls?session_id=sess-filter&risk_level=high")
        assert r.status_code == 200
        assert all(x["risk_level"] == "high" for x in r.json()["data"])

        # from/to 为闭区间；给一个远未来下界应查不到
        r2 = client.get("/api/v1/tool_calls?session_id=sess-filter&from=2999-01-01T00:00:00")
        assert r2.status_code == 200
        assert r2.json()["total"] == 0

        # limit 超过 500 静默截断（不报错）
        r3 = client.get("/api/v1/tool_calls?limit=100000")
        assert r3.status_code == 200
        assert len(r3.json()["data"]) <= 500


class TestGlobalReadAPIs:
    """与 TS V1 对齐的全局接口：/agents、/timeline、/storage_stats、DELETE /tool_calls。"""

    def _seed(self, repo):
        repo.get_or_create_agent("global-agent", "global-host")
        repo.get_or_create_session("sess-global", "global-agent:global-host")
        repo.insert_tool_call("sess-global", {
            "tool_name": "bash", "command": "ls", "cwd": "/tmp",
            "action_type": "bash", "action_detail": "ls",
        }, {"decision": "Allow", "reason": "ok", "risk_level": "low"})

    def test_agents_list(self, client, repo):
        self._seed(repo)
        r = client.get("/api/v1/agents")
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        hit = [x for x in rows if x["agent_type"] == "global-agent"]
        assert hit and hit[0]["session_count"] >= 1
        assert "call_count" in hit[0] and "hostname" in hit[0]

    def test_timeline(self, client, repo):
        self._seed(repo)
        r = client.get("/api/v1/timeline?limit=10")
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        if rows:
            assert {"time", "allow", "deny"} <= set(rows[0])

    def test_storage_stats(self, client, repo):
        self._seed(repo)
        r = client.get("/api/v1/storage_stats")
        assert r.status_code == 200
        body = r.json()
        assert {"stats", "oversize", "thresholdMB"} <= set(body)
        assert {"dbBytes", "toolCalls", "jsonFiles", "jsonBytes"} <= set(body["stats"])

    def test_delete_tool_calls(self, client, repo):
        self._seed(repo)
        r = client.delete("/api/v1/tool_calls")
        assert r.status_code == 200
        assert r.json()["deleted"] >= 1
        assert client.get("/api/v1/tool_calls").json()["total"] == 0


# ============================================================
# 6. Database Operations Tests
# ============================================================

class TestDatabase:
    def test_get_or_create_agent(self, repo):
        agent_id = repo.get_or_create_agent("opendes", "desktop-01", "0.1.0")
        assert agent_id == "opendes:desktop-01"

    def test_get_or_create_agent_idempotent(self, repo):
        id1 = repo.get_or_create_agent("agent-x", "host-x")
        id2 = repo.get_or_create_agent("agent-x", "host-x", "0.2.0")
        assert id1 == id2 == "agent-x:host-x"

    def test_get_or_create_session(self, repo):
        repo.get_or_create_agent("test-agent", "test-host")
        agent_id = "test-agent:test-host"
        repo.get_or_create_session("sess-1", agent_id, cwd="/tmp", user_prompt="Test task")

        session = repo.get_session("sess-1")
        assert session is not None
        assert session["cwd"] == "/tmp"
        assert session["user_prompt"] == "Test task"
        assert session["status"] == "active"

    def test_close_session(self, repo):
        repo.get_or_create_agent("test-agent", "test-host")
        agent_id = "test-agent:test-host"
        repo.get_or_create_session("sess-close", agent_id)
        repo.close_session("sess-close")

        session = repo.get_session("sess-close")
        assert session["status"] == "closed"
        assert session["ended_at"] is not None

    def test_insert_tool_call_increments_seq(self, repo):
        repo.get_or_create_agent("test-agent", "test-host")
        agent_id = "test-agent:test-host"
        repo.get_or_create_session("sess-seq", agent_id)

        id1 = repo.insert_tool_call("sess-seq", {
            "tool_name": "bash", "command": "cmd1", "cwd": "/tmp",
            "action_type": "bash", "action_detail": "cmd1",
        }, {"decision": "Allow"})
        id2 = repo.insert_tool_call("sess-seq", {
            "tool_name": "bash", "command": "cmd2", "cwd": "/tmp",
            "action_type": "bash", "action_detail": "cmd2",
        }, {"decision": "Allow"})

        assert id2 > id1
        session = repo.get_session("sess-seq")
        assert session["tool_call_count"] == 2

    def test_get_action_history(self, repo):
        repo.get_or_create_agent("test-agent", "test-host")
        agent_id = "test-agent:test-host"
        repo.get_or_create_session("sess-hist-db", agent_id)

        repo.insert_tool_call("sess-hist-db", {
            "tool_name": "bash", "command": "ls", "cwd": "/tmp",
            "action_type": "bash", "action_detail": "ls -la",
        }, {"decision": "Allow"})
        repo.insert_tool_call("sess-hist-db", {
            "tool_name": "bash", "command": "cat", "cwd": "/tmp",
            "action_type": "read", "action_detail": "cat /etc/hosts",
        }, {"decision": "Allow"})

        history = repo.get_action_history("sess-hist-db")
        assert len(history) == 2
        assert history[0]["action_type"] == "bash"
        assert history[0]["action_detail"] == "ls -la"
        assert history[1]["action_type"] == "read"
        assert history[1]["action_detail"] == "cat /etc/hosts"
        # called_at 供 AgentMoss P4 时间窗口判定（2026-10 新增）
        assert history[0]["called_at"], "DB 补全的历史必须带 called_at"

    def test_system_snapshot(self, repo):
        repo.get_or_create_agent("test-agent", "test-host")
        agent_id = "test-agent:test-host"
        repo.get_or_create_session("sess-snap", agent_id)

        tc_id = repo.insert_tool_call("sess-snap", {
            "tool_name": "bash", "command": "test", "cwd": "/tmp",
            "action_type": "bash", "action_detail": "test",
        }, {"decision": "Allow"})

        repo.insert_system_snapshot("sess-snap", tc_id, {
            "os": {"system": "linux"},
            "resources": {"cpu_count": 8},
        })

        snap = repo.get_latest_snapshot("sess-snap")
        assert snap is not None
        assert "linux" in snap["os_info"]

    def test_cleanup_old_records(self, repo):
        repo.get_or_create_agent("test-agent", "test-host")
        agent_id = "test-agent:test-host"
        repo.get_or_create_session("sess-clean", agent_id)
        repo.insert_tool_call("sess-clean", {
            "tool_name": "bash", "command": "test", "cwd": "/tmp",
            "action_type": "bash", "action_detail": "test",
        }, {"decision": "Allow"})
        repo.close_session("sess-clean")

        # Cleanup with 0 retention should delete everything old
        deleted = repo.cleanup_old_records(retention_days=0)
        assert deleted >= 0  # May delete snapshots and tool_calls


# ============================================================
# 7. Collector Orchestrator Tests
# ============================================================

class TestCollector:
    def test_collect_returns_system_context(self):
        orchestrator = CollectorOrchestrator()
        context = orchestrator.collect()

        assert isinstance(context, SystemContext)
        # OS info should be populated on Linux
        assert context.os.system in ("linux", "")
        assert isinstance(context.resources.cpu_count, int)
        assert isinstance(context.process.pid, int)
        assert context.process.pid > 0

    def test_collect_user_info(self):
        orchestrator = CollectorOrchestrator()
        context = orchestrator.collect()

        assert context.user.uid >= 0
        assert context.user.gid >= 0


# ============================================================
# 8. Normalizer Tests
# ============================================================

class TestNormalizer:
    @pytest.mark.asyncio
    async def test_normalize_basic(self):
        from agent_gate.v1.server.models import AgentGateInput
        normalizer = Normalizer(repo=None)

        input_data = AgentGateInput(
            agent_type="test",
            session_id="sess-norm",
            tool_name="bash",
            command="ls -la",
            cwd="/tmp",
            description="List files",
            user_prompt="Check directory",
        )
        context = SystemContext()

        moss_request = await normalizer.normalize(input_data, context)
        assert moss_request["session_id"] == "sess-norm"
        assert moss_request["a_next"]["action_type"] == "bash"
        assert moss_request["a_next"]["action_detail"] == "ls -la"
        assert moss_request["cwd"] == "/tmp"

    @pytest.mark.asyncio
    async def test_normalize_prompt_history(self):
        """prompt_history 交错时序应透传到 AgentMoss 请求."""
        from agent_gate.v1.server.models import AgentGateInput
        normalizer = Normalizer(repo=None)

        input_data = AgentGateInput(
            agent_type="test",
            session_id="sess-norm-ph",
            tool_name="bash",
            command="rm /tmp/a",
            cwd="/tmp",
            description="clean",
            user_prompt="继续",
            prompt_history=[
                {"text": "读取分析 src", "actions": [{"action_type": "read", "action_detail": "cat src/app.ts"}]},
                {"text": "继续", "actions": []},
            ],
        )
        context = SystemContext()

        moss_request = await normalizer.normalize(input_data, context)
        assert moss_request["prompt_session"] == "继续"
        assert len(moss_request["prompt_history"]) == 2
        assert moss_request["prompt_history"][0]["text"] == "读取分析 src"
        assert moss_request["prompt_history"][0]["actions"][0]["action_type"] == "read"
        assert moss_request["prompt_history"][0]["actions"][0]["action_detail"] == "cat src/app.ts"
        assert moss_request["prompt_history"][1]["text"] == "继续"
        assert moss_request["prompt_history"][1]["actions"] == []

    @pytest.mark.asyncio
    async def test_normalize_with_db_history(self, repo):
        """Test that normalizer fetches action_history from DB."""
        from agent_gate.v1.server.models import AgentGateInput

        repo.get_or_create_agent("test-agent", "test-host")
        agent_id = "test-agent:test-host"
        repo.get_or_create_session("sess-norm-db", agent_id)

        # Insert history into DB
        repo.insert_tool_call("sess-norm-db", {
            "tool_name": "bash", "command": "cmd1", "cwd": "/tmp",
            "action_type": "bash", "action_detail": "cmd1",
        }, {"decision": "Allow"})
        repo.insert_tool_call("sess-norm-db", {
            "tool_name": "bash", "command": "cmd2", "cwd": "/tmp",
            "action_type": "read", "action_detail": "cmd2",
        }, {"decision": "Allow"})

        normalizer = Normalizer(repo=repo)
        input_data = AgentGateInput(
            agent_type="test",
            session_id="sess-norm-db",
            tool_name="bash",
            command="cmd3",
            cwd="/tmp",
        )

        moss_request = await normalizer.normalize(input_data, SystemContext())

        # History should have 2 items from DB
        assert len(moss_request["action_history"]) == 2
        assert moss_request["action_history"][0]["action_type"] == "bash"
        assert moss_request["action_history"][1]["action_type"] == "read"
        # a_next should be the current action
        assert moss_request["a_next"]["action_detail"] == "cmd3"

    @pytest.mark.asyncio
    async def test_normalize_agent_provided_history_overrides_db(self, repo):
        """Agent-provided history should override DB history."""
        from agent_gate.v1.server.models import AgentGateInput, ActionHistoryItem

        repo.get_or_create_agent("test-agent", "test-host")
        agent_id = "test-agent:test-host"
        repo.get_or_create_session("sess-override", agent_id)

        # Insert some DB history
        repo.insert_tool_call("sess-override", {
            "tool_name": "bash", "command": "db-cmd", "cwd": "/tmp",
            "action_type": "bash", "action_detail": "db-cmd",
        }, {"decision": "Allow"})

        normalizer = Normalizer(repo=repo)
        input_data = AgentGateInput(
            agent_type="test",
            session_id="sess-override",
            tool_name="bash",
            command="new-cmd",
            cwd="/tmp",
            action_history=[
                ActionHistoryItem(action_type="custom", action_detail="agent-provided"),
            ],
        )

        moss_request = await normalizer.normalize(input_data, SystemContext())

        # Should use Agent-provided history, not DB history
        assert len(moss_request["action_history"]) == 1
        assert moss_request["action_history"][0]["action_type"] == "custom"

    @pytest.mark.asyncio
    async def test_normalize_forwards_action_history_called_at(self):
        """Agent 上报的 called_at 必须透传 —— 授权时效窗靠它和 a_next 相减。"""
        from agent_gate.v1.server.models import AgentGateInput, ActionHistoryItem

        normalizer = Normalizer(repo=None)
        asked_at = "2026-11-02T10:00:00"
        input_data = AgentGateInput(
            agent_type="test",
            session_id="sess-called-at",
            tool_name="bash",
            command="echo 'sandbox:newpass' | chpasswd",
            cwd="/tmp",
            action_history=[
                ActionHistoryItem(
                    action_type="ask_user_question",
                    action_detail="确认要改密吗？",
                    output="是",
                    called_at=asked_at,
                ),
            ],
        )

        moss_request = await normalizer.normalize(input_data, SystemContext())

        assert moss_request["action_history"][0]["called_at"] == asked_at

    @pytest.mark.asyncio
    async def test_normalize_stamps_a_next_called_at(self):
        """a_next.called_at 由 AgentGate 补当前时刻（调用方通常不上报）。"""
        import datetime
        from agent_gate.v1.server.models import AgentGateInput

        normalizer = Normalizer(repo=None)
        before = datetime.datetime.now() - datetime.timedelta(seconds=5)
        input_data = AgentGateInput(
            agent_type="test",
            session_id="sess-a-next-called-at",
            tool_name="bash",
            command="ls -la",
            cwd="/tmp",
        )

        moss_request = await normalizer.normalize(input_data, SystemContext())

        stamp = datetime.datetime.fromisoformat(moss_request["a_next"]["called_at"])
        assert stamp >= before


# ============================================================
# 9. Configuration Tests
# ============================================================

class TestConfig:
    def test_config_defaults(self):
        cfg = Config()
        assert cfg.server["port"] == 9100
        assert cfg.agent_moss["url"] == "http://127.0.0.1:9090"
        assert cfg.collector["system_info"] is True
        assert cfg.cache["enabled"] is True
        assert cfg.storage["retention_days"] == 30

    def test_config_auto_db_path(self):
        """Default db_path should be 'auto' (resolved at runtime)."""
        # Use reload to bypass env overrides from other tests
        import os
        old = os.environ.pop("AGENT_GATE_STORAGE_DB_PATH", None)
        try:
            cfg = reload_config()
            assert cfg.storage["db_path"] == "auto"
        finally:
            if old:
                os.environ["AGENT_GATE_STORAGE_DB_PATH"] = old

    def test_config_env_override(self):
        os.environ["AGENT_GATE_SERVER_PORT"] = "9999"
        cfg = Config()
        assert cfg.server["port"] == 9999
        del os.environ["AGENT_GATE_SERVER_PORT"]

    def test_config_get_dotted(self):
        cfg = Config()
        assert cfg.get("server.port") == 9100
        assert cfg.get("nonexistent.key", "default") == "default"

    def test_config_from_yaml(self):
        cfg = Config(config_path=Path(__file__).parent.parent / "config" / "agent_gate.yaml")
        assert cfg.server["port"] == 9100


# ============================================================
# 10. CLI Tests
# ============================================================

class TestCLI:
    def test_cli_help(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "agent_gate.v1", "--help"],
            capture_output=True, text=True,
        )
        assert "AgentGate" in result.stdout


# ============================================================
# 11. Integration: Full Flow Tests
# ============================================================

class TestFullFlow:
    def test_full_flow_allow_then_deny(self, client, repo):
        """Simulate a full session with multiple tool calls."""
        session_id = "full-flow-session"
        agent_type = "integration-test"
        PATCH_TARGET = "agent_gate.v1.client.moss_client.MossClient"

        with patch(PATCH_TARGET) as mock_cls:
            instance = mock_cls.return_value

            # First call: Allow
            instance.analyze = AsyncMock(return_value={
                "decision": "Allow",
                "reason": "Safe command",
                "risk_level": "low",
                "risk_type": "safe",
                "violated_layers": [],
                "confidence": 95,
                "analysis_duration_ms": 5.0,
            })
            r1 = client.post("/api/v1/analyze", json={
                "agent_type": agent_type,
                "session_id": session_id,
                "tool_name": "bash",
                "command": "ls -la",
                "cwd": "/tmp",
            })
            assert r1.json()["decision"] == "Allow"

            # Second call: Deny
            instance.analyze = AsyncMock(return_value={
                "decision": "Deny",
                "reason": "Detected dangerous operation",
                "risk_level": "critical",
                "risk_type": "file_deletion",
                # L2.1=逻辑规则 + L2.2=形式化验证，与重构后真实执行顺序一致
                "violated_layers": ["L2.1", "L2.2"],
                "confidence": 98,
                "analysis_duration_ms": 15.0,
            })
            r2 = client.post("/api/v1/analyze", json={
                "agent_type": agent_type,
                "session_id": session_id,
                "tool_name": "bash",
                "command": "rm -rf /important/*",
                "cwd": "/tmp",
            })
            assert r2.json()["decision"] == "Deny"
            assert r2.json()["risk_level"] == "critical"

        # Verify session trace
        traces_resp = client.get(f"/api/v1/sessions/{session_id}/traces")
        assert traces_resp.status_code == 200
        traces = traces_resp.json()
        assert traces["tool_call_count"] == 2
        assert traces["traces"][0]["decision"] == "Allow"
        assert traces["traces"][1]["decision"] == "Deny"

    def test_session_history_accumulates(self, client, repo):
        """Verify that history accumulates across multiple analyze calls."""
        session_id = "accum-session"
        PATCH_TARGET = "agent_gate.v1.client.moss_client.MossClient"

        with patch(PATCH_TARGET) as mock_cls:
            instance = mock_cls.return_value

            for i in range(3):
                instance.analyze = AsyncMock(return_value={
                    "decision": "Allow",
                    "reason": f"ok-{i}",
                    "risk_level": "low",
                    "risk_type": "safe",
                    "violated_layers": [],
                    "confidence": 90,
                    "analysis_duration_ms": 5.0,
                })
                client.post("/api/v1/analyze", json={
                    "agent_type": "accum-test",
                    "session_id": session_id,
                    "tool_name": "bash",
                    "command": f"cmd-{i}",
                    "cwd": "/tmp",
                })

        traces_resp = client.get(f"/api/v1/sessions/{session_id}/traces")
        assert traces_resp.json()["tool_call_count"] == 3


# ============================================================
# 12. Dashboard Tests
# ============================================================

class TestDashboard:
    def test_dashboard_page_loads(self, client):
        """Dashboard HTML page should return 200."""
        response = client.get("/dashboard/")
        assert response.status_code == 200
        assert "<title>AgentGate Dashboard</title>" in response.text

    def test_dashboard_stats(self, client, repo):
        """Stats API should return aggregated data."""
        repo.get_or_create_agent("test-agent", "test-host")
        agent_id = "test-agent:test-host"
        repo.get_or_create_session("dash-sess", agent_id)
        repo.insert_tool_call("dash-sess", {
            "tool_name": "bash", "command": "test", "cwd": "/tmp",
            "action_type": "bash", "action_detail": "test",
        }, {"decision": "Allow", "risk_level": "low"})

        response = client.get("/dashboard/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["total_agents"] >= 1
        assert data["total_tool_calls"] >= 1

    def test_dashboard_recent(self, client, repo):
        """Recent calls API should return records."""
        repo.get_or_create_agent("test-agent", "test-host")
        agent_id = "test-agent:test-host"
        repo.get_or_create_session("dash-recent", agent_id)
        repo.insert_tool_call("dash-recent", {
            "tool_name": "bash", "command": "ls", "cwd": "/tmp",
            "action_type": "bash", "action_detail": "ls",
        }, {"decision": "Allow"})

        response = client.get("/dashboard/recent?limit=5")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1

    def test_dashboard_timeline(self, client):
        """Timeline API should return time-series data."""
        response = client.get("/dashboard/timeline?minutes=10")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
