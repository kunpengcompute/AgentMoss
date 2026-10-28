"""独立 SDK（``sdk/python/gate_client.py``）鉴权头回归。

AgentGate 默认 ``AGENT_GATE_AUTH_MODE=auto``，非 health 请求都要带 Bearer。
独立 SDK 可能被拷贝到任意 Agent 项目里使用，必须能用 ``api_token`` 参数或
``AGENT_GATE_AUTH_TOKEN`` 环境变量提供凭证。
"""

import sys
from pathlib import Path

# parents: [0]=v1, [1]=tests, [2]=agent_gate, [3]=仓库根
SDK_DIR = Path(__file__).resolve().parents[3] / "sdk" / "python"
sys.path.insert(0, str(SDK_DIR))

from gate_client import GateClient  # noqa: E402


def test_api_token_becomes_bearer():
    client = GateClient(api_token="tok-123")
    assert client._headers() == {"Authorization": "Bearer tok-123"}


def test_env_token_fallback(monkeypatch):
    monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN", "env-tok")
    client = GateClient()
    assert client._headers() == {"Authorization": "Bearer env-tok"}


def test_no_token_no_header(monkeypatch):
    monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN", raising=False)
    client = GateClient()
    assert client._headers() == {}
