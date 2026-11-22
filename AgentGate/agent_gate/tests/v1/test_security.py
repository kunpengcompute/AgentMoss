"""AgentGate 自身安全的回归测试。

对应审计发现：
  G1 默认绑 0.0.0.0        → 默认回环
  G2 全接口无鉴权          → Bearer / auto token
  G3 traces 暴露 prompt    → 需鉴权；跨会话扫描有信号
  G4 CORS * + credentials  → 白名单回显
  G5 agent_id 自报         → 每 agent token 绑定 agent_type
  G6 WebSocket 无鉴权      → 握手校验
  G8 SQLite 权限           → 0600 / 0700
  G9 无体积/限流上限       → 413 / 429 / 503
  G10 moss_client 不带凭证 → Authorization + 401 自愈

存量用例统一在 conftest 里关掉鉴权（AGENT_GATE_AUTH_MODE=off），这里显式打开。
"""

import os
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_gate.shared.server import security as sp
from agent_gate.shared.server.app import create_app

TEST_DB_PATH = "/tmp/agent_gate_security_test.db"
GOOD_TOKEN = "test-token-0123456789abcdef"
AGENT_TOKENS = "opendesk=tok-opendesk,qwenpaw=tok-qwenpaw"


@pytest.fixture
def auth_on(monkeypatch):
    """打开鉴权，用固定的全局 token（不落盘）。"""
    monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "auto")
    monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN", GOOD_TOKEN)
    monkeypatch.delenv("AGENT_GATE_AGENT_TOKENS", raising=False)
    monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN_FD", raising=False)
    monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN_FILE", raising=False)
    sp.reset_counters()
    yield
    sp.reset_counters()


@pytest.fixture
def client(monkeypatch, auth_on):
    monkeypatch.setenv("AGENT_GATE_STORAGE_DB_PATH", TEST_DB_PATH)
    from agent_gate.shared.config import reload_config
    reload_config()
    from agent_gate.shared.storage.db import get_db
    db = get_db(TEST_DB_PATH)
    db.init_schema()
    return TestClient(create_app())


def _h(tok=GOOD_TOKEN):
    return {"Authorization": f"Bearer {tok}"}


# ============================================================
# 1. 默认值
# ============================================================

class TestDefaults:
    def test_auth_mode_defaults_to_auto(self, monkeypatch):
        monkeypatch.delenv("AGENT_GATE_AUTH_MODE", raising=False)
        assert sp.auth_mode() == "auto"

    def test_origin_check_defaults_on(self, monkeypatch):
        monkeypatch.delenv("AGENT_GATE_ORIGIN_CHECK", raising=False)
        assert sp.origin_check_enabled() is True

    def test_server_bind_default_is_unchanged(self):
        """监听地址**故意不改**：改成回环会破坏跨机采集，那是功能变化。

        安全靠鉴权兜（监听 0.0.0.0 + 无凭证 401），不靠改监听地址。
        """
        from agent_gate.shared.config import Config
        assert Config().server["host"] == "0.0.0.0"

    def test_cli_host_default_is_unchanged(self):
        import subprocess
        r = subprocess.run(
            [sys.executable, "-m", "agent_gate.v1", "server", "--help"],
            capture_output=True, text=True,
        )
        assert "0.0.0.0" in r.stdout


# ============================================================
# 2. 自动 token
# ============================================================

class TestAutoToken:
    def test_generated_with_0600(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "auto")
        monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN_FILE", raising=False)
        monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN_FD", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        sp.reset_counters()

        tok = sp.auth_token()
        assert tok
        path = sp.auth_token_path()
        # 与 AgentMoss 同一约定：~/.config/<产品>/auth_token
        assert path == tmp_path / "agentgate" / "auth_token"
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        # 第二次读同一份（多 worker 场景）
        sp.reset_counters()
        assert sp.auth_token() == tok

    def test_token_survives_restart(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "auto")
        monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        sp.reset_counters()
        first = sp.auth_token()
        sp.reset_counters()
        assert sp.auth_token() == first

    def test_token_is_not_under_data_dir(self, monkeypatch, tmp_path):
        """凭据在 .config，数据库仍在 .local/share —— 两者不要混。"""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        sp.reset_counters()
        assert str(sp.auth_token_path()).startswith(str(tmp_path / "cfg"))


class TestFdMisconfigured:
    """显式给了 fd 却读不到 → 拒绝启动 + 运行期 fail-closed，绝不降级到磁盘 token。"""

    def _closed_fd(self):
        import os
        r, w = os.pipe()
        os.close(r)
        os.close(w)
        return r

    def test_auth_config_error_reported(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "auto")
        monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN_FD", str(self._closed_fd()))
        sp.reset_counters()
        assert sp.auth_config_error()

    def test_no_disk_token_written(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "auto")
        monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN_FD", str(self._closed_fd()))
        sp.reset_counters()
        sp.auth_token()  # 触发解析
        assert not (tmp_path / "agentgate" / "auth_token").exists()

    def test_requests_fail_closed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "auto")
        monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN_FD", str(self._closed_fd()))
        sp.reset_counters()
        ok, _ = sp.check_request_auth({}, "/api/v1/tool_calls")
        assert ok is False
        # 探活仍然放行，方便运维发现问题
        ok_health, _ = sp.check_request_auth({}, "/api/v1/health")
        assert ok_health is True

    def test_startup_exits(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "auto")
        monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN_FD", str(self._closed_fd()))
        sp.reset_counters()
        with pytest.raises(SystemExit):
            sp.fail_fast_on_auth_error()


# ============================================================
# 3. 鉴权
# ============================================================

class TestAuth:
    def test_health_is_public(self, client):
        assert client.get("/api/v1/health").status_code == 200

    def test_analyze_requires_token(self, client):
        r = client.post("/api/v1/analyze", json={
            "agent_type": "test", "session_id": "s", "tool_name": "bash", "command": "ls",
        })
        assert r.status_code == 401
        assert r.headers.get("www-authenticate") == "Bearer"

    def test_analyze_rejects_wrong_token(self, client):
        r = client.post("/api/v1/analyze", json={
            "agent_type": "test", "session_id": "s", "tool_name": "bash", "command": "ls",
        }, headers=_h("nope"))
        assert r.status_code == 401

    def test_traces_require_token(self, client):
        assert client.get("/api/v1/sessions/whatever/traces").status_code == 401

    def test_tool_calls_require_token(self, client):
        assert client.get("/api/v1/tool_calls").status_code == 401

    def test_auth_off_restores_old_behavior(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "off")
        sp.reset_counters()
        # 没有 token 也能过鉴权（默认无鉴权 → 走到业务层，404 说明已经过了）
        assert client.get("/api/v1/sessions/nonexistent/traces").status_code == 404


class TestAgentBinding:
    def test_mismatched_agent_type_rejected(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_GATE_AGENT_TOKENS", AGENT_TOKENS)
        sp.reset_counters()
        r = client.post("/api/v1/analyze", json={
            "agent_type": "opendesk", "session_id": "s1",
            "tool_name": "bash", "command": "ls",
        }, headers=_h("tok-qwenpaw"))
        assert r.status_code == 403
        assert "opendesk" in r.json()["detail"]

    def test_binding_enforced_when_inflight_gate_enabled(self, monkeypatch):
        """有界并发开启时，逐 agent 身份绑定不能失效。

        2026-10 回归：`_InflightGate` 分支原来直接 `return await call_next`，
        跳过了函数末尾的 `request.state.principal` 赋值 —— 并发一开，
        analyze 拿到空 principal，`bind_agent("")` 恒放行，仿冒防护静默失效。
        这里显式把 MAX_INFLIGHT 设为 2（不依赖默认值）守住这条路径。
        """
        monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "auto")
        monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN", GOOD_TOKEN)
        monkeypatch.setenv("AGENT_GATE_AGENT_TOKENS", AGENT_TOKENS)
        monkeypatch.setenv("AGENT_GATE_MAX_INFLIGHT", "2")
        monkeypatch.setenv("AGENT_GATE_STORAGE_DB_PATH", TEST_DB_PATH)
        monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN_FILE", raising=False)
        monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN_FD", raising=False)
        sp.reset_counters()
        from agent_gate.shared.config import reload_config
        reload_config()
        from agent_gate.shared.storage.db import get_db
        get_db(TEST_DB_PATH).init_schema()

        client = TestClient(create_app())
        r = client.post("/api/v1/analyze", json={
            "agent_type": "opendesk", "session_id": "s-gate",
            "tool_name": "bash", "command": "ls",
        }, headers=_h("tok-qwenpaw"))
        assert r.status_code == 403

    def test_matched_agent_type_passes(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_GATE_AGENT_TOKENS", AGENT_TOKENS)
        sp.reset_counters()
        with patch("agent_gate.shared.client.moss_client.MossClient") as mock_cls:
            mock_cls.return_value.analyze = AsyncMock(return_value={
                "decision": "Allow", "reason": "ok", "risk_level": "low",
                "risk_type": "safe", "violated_layers": [], "confidence": 90,
                "analysis_duration_ms": 1.0,
            })
            r = client.post("/api/v1/analyze", json={
                "agent_type": "opendesk", "session_id": "s2",
                "tool_name": "bash", "command": "ls",
            }, headers=_h("tok-opendesk"))
        assert r.status_code == 200
        assert r.json()["decision"] == "Allow"

    def test_global_token_may_claim_any_agent(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_GATE_AGENT_TOKENS", AGENT_TOKENS)
        sp.reset_counters()
        with patch("agent_gate.shared.client.moss_client.MossClient") as mock_cls:
            mock_cls.return_value.analyze = AsyncMock(return_value={
                "decision": "Allow", "reason": "ok", "risk_level": "low",
                "risk_type": "safe", "violated_layers": [], "confidence": 90,
                "analysis_duration_ms": 1.0,
            })
            r = client.post("/api/v1/analyze", json={
                "agent_type": "qwenpaw", "session_id": "s3",
                "tool_name": "bash", "command": "ls",
            }, headers=_h(GOOD_TOKEN))
        assert r.status_code == 200


class TestAgentDataScope:
    """逐 agent token 的数据隔离：agent 只能看自己的，看板只给全局 token。

    2026-10 修复：此前 check_token 对全局和 agent token 一视同仁 ——
    agent A 拿自己的 token 就能读 agent B 的全部命令原文和用户 prompt，
    也能打开看板看全站数据。
    """

    def _seed(self):
        from agent_gate.shared.storage.repository import Repository
        repo = Repository()
        a1 = repo.get_or_create_agent("opendesk", "host-a", "1.0")
        a2 = repo.get_or_create_agent("qwenpaw", "host-b", "1.0")
        repo.get_or_create_session(
            session_id="s-scope-open", agent_id=a1, cwd="/tmp",
            user_prompt="open prompt", os_type="linux",
        )
        repo.get_or_create_session(
            session_id="s-scope-qwen", agent_id=a2, cwd="/tmp",
            user_prompt="qwen prompt", os_type="linux",
        )
        repo.insert_tool_call(
            "s-scope-open",
            {"tool_name": "bash", "command": "ls", "action_type": "bash", "action_detail": "ls"},
            {"decision": "Allow", "risk_level": "low"},
        )
        repo.insert_tool_call(
            "s-scope-qwen",
            {"tool_name": "bash", "command": "pwd", "action_type": "bash", "action_detail": "pwd"},
            {"decision": "Allow", "risk_level": "low"},
        )

    def test_agent_token_cannot_open_dashboard(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_GATE_AGENT_TOKENS", AGENT_TOKENS)
        sp.reset_counters()
        r = client.get("/dashboard/?token=tok-qwenpaw", follow_redirects=False)
        assert r.status_code == 403

    def test_agent_token_cannot_read_dashboard_stats(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_GATE_AGENT_TOKENS", AGENT_TOKENS)
        sp.reset_counters()
        assert client.get("/dashboard/stats", headers=_h("tok-qwenpaw")).status_code == 403

    def test_agent_token_reads_own_session(self, client, monkeypatch):
        self._seed()
        monkeypatch.setenv("AGENT_GATE_AGENT_TOKENS", AGENT_TOKENS)
        sp.reset_counters()
        r = client.get("/api/v1/sessions/s-scope-open/traces", headers=_h("tok-opendesk"))
        assert r.status_code == 200
        assert r.json()["user_prompt"] == "open prompt"

    def test_agent_token_cannot_read_other_session(self, client, monkeypatch):
        self._seed()
        monkeypatch.setenv("AGENT_GATE_AGENT_TOKENS", AGENT_TOKENS)
        sp.reset_counters()
        r = client.get("/api/v1/sessions/s-scope-qwen/traces", headers=_h("tok-opendesk"))
        assert r.status_code == 403

    def test_agent_token_tool_calls_only_own(self, client, monkeypatch):
        self._seed()
        monkeypatch.setenv("AGENT_GATE_AGENT_TOKENS", AGENT_TOKENS)
        sp.reset_counters()
        r = client.get("/api/v1/tool_calls", headers=_h("tok-opendesk"))
        assert r.status_code == 200
        sids = {x["session_id"] for x in r.json()["data"]}
        # 自己的记录在；别人（qwenpaw）的一条都不能出现。
        # 不断言"只有 s-scope-open"——TEST_DB_PATH 是共享库，其他测试也写过 opendesk 数据。
        assert "s-scope-open" in sids
        assert "s-scope-qwen" not in sids

    def test_global_token_reads_everything(self, client, monkeypatch):
        self._seed()
        monkeypatch.setenv("AGENT_GATE_AGENT_TOKENS", AGENT_TOKENS)
        sp.reset_counters()
        assert client.get(
            "/api/v1/sessions/s-scope-qwen/traces", headers=_h()
        ).status_code == 200
        assert client.get("/dashboard/stats", headers=_h()).status_code == 200

    def test_agent_token_cannot_read_global_endpoints(self, client, monkeypatch):
        """timeline / storage_stats / DELETE tool_calls 是全局聚合或破坏性操作。"""
        self._seed()
        monkeypatch.setenv("AGENT_GATE_AGENT_TOKENS", AGENT_TOKENS)
        sp.reset_counters()
        h = _h("tok-opendesk")
        assert client.get("/api/v1/timeline", headers=h).status_code == 403
        assert client.get("/api/v1/storage_stats", headers=h).status_code == 403
        assert client.delete("/api/v1/tool_calls", headers=h).status_code == 403
        # /agents 不 403，但只能看到自己 agent 的那一条（与 TS 同构）
        rows = client.get("/api/v1/agents", headers=h).json()
        assert all(x["agent_type"] == "opendesk" for x in rows)


# ============================================================
# 4. 跨源
# ============================================================

class TestOrigin:
    def test_cross_origin_rejected(self):
        assert sp.origin_allowed("http://evil.example", "127.0.0.1:9100") is False

    def test_same_origin_allowed(self):
        assert sp.origin_allowed("http://127.0.0.1:9100", "127.0.0.1:9100") is True

    def test_no_origin_allowed(self):
        """原生客户端（curl / hooks）不发 Origin。"""
        assert sp.origin_allowed("", "127.0.0.1:9100") is True

    def test_allowlist(self, monkeypatch):
        monkeypatch.setenv("AGENT_GATE_CORS_ORIGINS", "http://ui.internal:3000")
        assert sp.origin_allowed("http://ui.internal:3000", "127.0.0.1:9100") is True

    def test_middleware_blocks_cross_origin(self, client):
        r = client.get("/api/v1/tool_calls", headers={
            **_h(), "Origin": "http://evil.example",
        })
        assert r.status_code == 403


# ============================================================
# 5. Dashboard
# ============================================================

class TestDashboard:
    def test_page_without_token_is_401(self, client):
        r = client.get("/dashboard/")
        assert r.status_code == 401
        assert "token" in r.text

    def test_token_query_sets_cookie_and_redirects(self, client):
        r = client.get(f"/dashboard/?token={GOOD_TOKEN}", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/dashboard/"
        assert sp.DASHBOARD_COOKIE in r.headers.get("set-cookie", "")
        assert "httponly" in r.headers.get("set-cookie", "").lower()
        assert r.headers.get("referrer-policy") == "no-referrer"

    def test_cookie_grants_access(self, client):
        client.get(f"/dashboard/?token={GOOD_TOKEN}", follow_redirects=False)
        r = client.get("/dashboard/")
        assert r.status_code == 200
        assert "<title>AgentGate Dashboard</title>" in r.text

    def test_wrong_token_query_is_401(self, client):
        r = client.get("/dashboard/?token=bad", follow_redirects=False)
        assert r.status_code == 401

    def test_stats_requires_auth(self, client):
        assert client.get("/dashboard/stats").status_code == 401

    def test_stats_with_cookie(self, client):
        client.get(f"/dashboard/?token={GOOD_TOKEN}", follow_redirects=False)
        assert client.get("/dashboard/stats").status_code == 200

    def test_websocket_not_registered_by_default(self, client):
        """V1 看板是轮询，不需要 WS —— 默认不注册这个路由。

        注册了就是一个白送的攻击面：WS 不走 HTTP 中间件，不受连接层上限约束。
        """
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/events"):
                pass


@pytest.fixture
def ws_client(monkeypatch):
    """显式开启 WS 的客户端（AGENT_GATE_ENABLE_WS=1）。"""
    monkeypatch.setenv("AGENT_GATE_ENABLE_WS", "1")
    monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "auto")
    monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN", GOOD_TOKEN)
    monkeypatch.setenv("AGENT_GATE_STORAGE_DB_PATH", TEST_DB_PATH)
    monkeypatch.delenv("AGENT_GATE_AGENT_TOKENS", raising=False)
    monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN_FILE", raising=False)
    monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN_FD", raising=False)
    sp.reset_counters()
    from agent_gate.shared.config import reload_config
    reload_config()
    from agent_gate.shared.storage.db import get_db
    get_db(TEST_DB_PATH).init_schema()
    return TestClient(create_app())


class TestWebSocketGuards:
    """开启 WS 后，三道闸必须自己做（它不走 HTTP 中间件）。

    uvicorn 的 ``limit_concurrency`` 只在 HTTP 协议实现里检查，WS 不受约束；
    不做连接上限就可以无限开连接打满 task/内存。
    """

    def test_requires_token(self, ws_client):
        with pytest.raises(Exception):
            with ws_client.websocket_connect("/ws/events"):
                pass

    def test_cross_origin_rejected(self, ws_client, monkeypatch):
        monkeypatch.setenv("AGENT_GATE_ORIGIN_CHECK", "1")
        with pytest.raises(Exception):
            with ws_client.websocket_connect(
                "/ws/events",
                headers={
                    "Authorization": f"Bearer {GOOD_TOKEN}",
                    "Origin": "http://evil.example",
                },
            ):
                pass

    def test_same_origin_allowed(self, ws_client, monkeypatch):
        monkeypatch.setenv("AGENT_GATE_ORIGIN_CHECK", "1")
        with ws_client.websocket_connect(
            "/ws/events",
            headers={
                "Authorization": f"Bearer {GOOD_TOKEN}",
                "Origin": "http://testserver",
            },
        ) as ws:
            ws.send_text("ping")

    def test_connection_limit(self, ws_client, monkeypatch):
        monkeypatch.setenv("AGENT_GATE_MAX_WS_CONNECTIONS", "1")
        headers = {"Authorization": f"Bearer {GOOD_TOKEN}"}
        with ws_client.websocket_connect("/ws/events", headers=headers):
            # 第一条在线时，第二条必须被拒（close 1013）
            with pytest.raises(Exception):
                with ws_client.websocket_connect("/ws/events", headers=headers):
                    pass


# ============================================================
# 6. 资源上限
# ============================================================

class TestLimits:
    def test_body_too_large_413(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_GATE_MAX_BODY_BYTES", "1024")
        big = "x" * 4096
        r = client.post("/api/v1/analyze", json={
            "agent_type": "t", "session_id": "s", "tool_name": "bash", "command": big,
        }, headers=_h())
        assert r.status_code == 413

    def test_rate_limit_429(self, client, monkeypatch):
        from agent_gate.shared.server import app as app_module
        monkeypatch.setattr(app_module, "_limiter", app_module._RateLimiter(qps=1))
        codes = [client.get("/api/v1/tool_calls", headers=_h()).status_code for _ in range(6)]
        assert 429 in codes

    def test_socket_mode_default_and_override(self, monkeypatch):
        """UDS 没有网络面，socket 权限就是唯一的内核级访问控制。"""
        monkeypatch.delenv("AGENT_GATE_SOCKET_MODE", raising=False)
        assert sp.socket_mode() == 0o600
        monkeypatch.setenv("AGENT_GATE_SOCKET_MODE", "660")
        assert sp.socket_mode() == 0o660
        for bad in ("zzz", "9999", "-1"):
            monkeypatch.setenv("AGENT_GATE_SOCKET_MODE", bad)
            assert sp.socket_mode() == 0o600, bad

    def test_pool_of_classifies_paths(self):
        """路径分池：Dashboard 打满只占 ops，不挤占 analyze 的 biz。"""
        assert sp.pool_of("/api/v1/analyze") == "biz"
        assert sp.pool_of("/api/v1/sessions/s1/traces") == "read"
        assert sp.pool_of("/api/v1/tool_calls") == "read"
        assert sp.pool_of("/dashboard/stats") == "ops"
        assert sp.pool_of("/api/v1/health") == "health"
        assert sp.pool_of("/anything-else") == "other"

    def test_pool_limits_defaults(self, monkeypatch):
        for k in ("AGENT_GATE_POOL_BIZ", "AGENT_GATE_POOL_READ",
                  "AGENT_GATE_POOL_OPS", "AGENT_GATE_POOL_HEALTH",
                  "AGENT_GATE_POOL_OTHER", "AGENT_GATE_MAX_INFLIGHT"):
            monkeypatch.delenv(k, raising=False)
        assert sp.pool_limit("biz") == 16
        assert sp.pool_limit("read") == 8
        assert sp.pool_limit("ops") == 4
        assert sp.pool_limit("health") == 4
        # biz 池兼容旧的 AGENT_GATE_MAX_INFLIGHT
        monkeypatch.setenv("AGENT_GATE_MAX_INFLIGHT", "32")
        assert sp.pool_limit("biz") == 32

    def test_unauth_and_agent_limits_defaults(self, monkeypatch):
        for k in ("AGENT_GATE_MAX_UNAUTH_INFLIGHT", "AGENT_GATE_MAX_UNAUTH_PER_IP",
                  "AGENT_GATE_MAX_AGENT_INFLIGHT"):
            monkeypatch.delenv(k, raising=False)
        assert sp.unauth_inflight() == 4
        assert sp.unauth_per_ip() == 2
        assert sp.agent_inflight() == 8

    def test_pooled_limiter_counts_and_releases(self):
        """计数闸门：拿到就能进，满了一律拒绝，release 后恢复。"""
        import asyncio
        from agent_gate.shared.server.app import _PooledLimiter
        lim = _PooledLimiter()

        async def scenario():
            assert await lim.try_acquire("k", 1) is True
            assert await lim.try_acquire("k", 1) is False
            await lim.release("k")
            assert await lim.try_acquire("k", 1) is True
            await lim.release("k")
            # 0 = 不限
            assert await lim.try_acquire("x", 0) is True

        asyncio.run(scenario())


# ============================================================
# 7. 落盘权限
# ============================================================

class TestFilePermissions:
    def test_db_hardened_to_0600(self, tmp_path):
        db = tmp_path / "agent_gate" / "agent_gate.db"
        db.parent.mkdir(parents=True)
        db.write_text("x")
        os.chmod(db, 0o644)
        sp.harden_db_files(db)
        assert stat.S_IMODE(db.stat().st_mode) == 0o600
        assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700

    def test_does_not_touch_shared_dir(self, tmp_path):
        """db 放在 /tmp 这类共享目录时，绝不能去 chmod 父目录。"""
        db = tmp_path / "agent_gate_test.db"
        db.write_text("x")
        os.chmod(db, 0o644)
        before = stat.S_IMODE(tmp_path.stat().st_mode)
        sp.harden_db_files(db)
        assert stat.S_IMODE(db.stat().st_mode) == 0o600
        assert stat.S_IMODE(tmp_path.stat().st_mode) == before


# ============================================================
# 8. AgentMoss 凭证（G10）
# ============================================================

class TestMossClientAuth:
    def test_reads_token_from_env(self, monkeypatch):
        from agent_gate.shared.client import moss_client
        monkeypatch.setenv("AGENT_MOSS_AUTH_TOKEN", "moss-tok")
        assert moss_client.resolve_moss_token(force=True) == "moss-tok"

    def test_reads_token_from_file(self, monkeypatch, tmp_path):
        from agent_gate.shared.client import moss_client
        monkeypatch.delenv("AGENT_MOSS_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("AGENT_GATE_MOSS_TOKEN", raising=False)
        p = tmp_path / "auth_token"
        p.write_text("file-tok\n")
        monkeypatch.setenv("AGENT_MOSS_AUTH_TOKEN_FILE", str(p))
        assert moss_client.resolve_moss_token(force=True) == "file-tok"

    def test_reads_token_from_data_dir(self, monkeypatch, tmp_path):
        from agent_gate.shared.client import moss_client
        monkeypatch.delenv("AGENT_MOSS_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("AGENT_MOSS_AUTH_TOKEN_FILE", raising=False)
        monkeypatch.delenv("AGENT_GATE_MOSS_TOKEN", raising=False)
        (tmp_path / "auth_token").write_text("dir-tok")
        monkeypatch.setenv("AGENT_MOSS_DATA_DIR", str(tmp_path))
        assert moss_client.resolve_moss_token(force=True) == "dir-tok"

    def test_analyze_sends_bearer(self, monkeypatch):
        """回归：0.7.6 及以前不带任何凭证，AgentMoss 默认鉴权后会全部 401。"""
        import httpx
        from agent_gate.shared.client import moss_client

        monkeypatch.setenv("AGENT_MOSS_AUTH_TOKEN", "moss-tok")
        moss_client.resolve_moss_token(force=True)

        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"decision": "Allow"})

        c = moss_client.MossClient()
        import asyncio
        c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        asyncio.run(c.analyze({"session_id": "s"}))
        assert seen["auth"] == "Bearer moss-tok"


# ============================================================
# 9. 跨会话扫描信号
# ============================================================

class TestSessionSweep:
    def test_counts_distinct_sessions(self):
        sp.reset_counters()
        n = 0
        for i in range(5):
            n, hit = sp.note_session_access("agent:opendesk", f"sess-{i}")
        assert n == 5
        assert hit is False

    def test_threshold_triggers(self, monkeypatch):
        sp.reset_counters()
        monkeypatch.setenv("AGENT_GATE_SESSION_SWEEP_THRESHOLD", "3")
        for i in range(4):
            _, hit = sp.note_session_access("agent:x", f"s{i}")
        assert hit is True

    def test_zero_disables(self, monkeypatch):
        sp.reset_counters()
        monkeypatch.setenv("AGENT_GATE_SESSION_SWEEP_THRESHOLD", "0")
        assert sp.note_session_access("k", "s1") == (0, False)

    def test_same_session_not_counted_twice(self, monkeypatch):
        sp.reset_counters()
        monkeypatch.setenv("AGENT_GATE_SESSION_SWEEP_THRESHOLD", "3")
        for _ in range(10):
            n, hit = sp.note_session_access("k", "same")
        assert n == 1
        assert hit is False
