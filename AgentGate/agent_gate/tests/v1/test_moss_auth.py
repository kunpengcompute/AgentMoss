"""所有出站调 AgentMoss / AgentGate 的客户端都必须能带 Bearer。

背景：加固版 AgentMoss 默认开鉴权（``AGENT_MOSS_AUTH_MODE=auto``），加固版
AgentGate 默认开鉴权（``AGENT_GATE_AUTH_MODE=auto``）。此前只有主客户端
``MossClient`` 带了凭证，遗漏了以下路径：

- ``GateClient``（第三方 Agent 用的 SDK）—— 调加固版 AgentGate 会 401。

本文件看护这三条路径都能正确携带凭证，且未配置 token 时行为与加固前一致。
"""

from unittest.mock import MagicMock, patch

from agent_gate.shared.sdk import GateClient


def _fake_urlopen(captured: dict):
    def _open(req, timeout=None):  # noqa: ANN001
        captured["auth"] = req.get_header("Authorization")
        resp = MagicMock()
        resp.read.return_value = b'{"decision":"Allow","risk_level":"low"}'
        resp.__enter__ = lambda s: resp
        resp.__exit__ = lambda *a: None
        return resp

    return _open


# ----------------------------------------------------------------------
# GateClient（第三方 SDK）
# ----------------------------------------------------------------------


def _fake_response() -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"decision": "Allow"})
    return resp


def test_gate_client_analyze_sends_bearer():
    client = GateClient(base_url="http://127.0.0.1:9100", api_token="tok-sdk")
    with patch.object(client._client, "post", return_value=_fake_response()) as post:
        client.analyze(agent_type="x", session_id="s", tool_name="bash", command="ls")

    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer tok-sdk"
    client.close()


def test_gate_client_omits_header_without_token(monkeypatch):
    monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN", raising=False)
    client = GateClient(base_url="http://127.0.0.1:9100")
    with patch.object(client._client, "post", return_value=_fake_response()) as post:
        client.analyze(agent_type="x", session_id="s", tool_name="bash", command="ls")

    assert post.call_args.kwargs["headers"] == {}
    client.close()


def test_gate_client_reads_env_token(monkeypatch):
    monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN", "tok-env")
    client = GateClient(base_url="http://127.0.0.1:9100")
    assert client._headers()["Authorization"] == "Bearer tok-env"
    client.close()
