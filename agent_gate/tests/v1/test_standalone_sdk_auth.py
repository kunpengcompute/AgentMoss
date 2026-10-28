"""独立 SDK（sdk/python/gate_client.py）的鉴权回归。

背景：AgentGate 0.7.6 起默认开启 Bearer 鉴权。包内 SDK（``agent_gate.v1.sdk``）
当时已补上 token，但 **sdk/ 目录下那份独立客户端没有** —— 而它是随 sdist 发布、
且被文档明确推荐给集成方单独拷贝使用的。默认配置下不带 token 会 401，等于官方
SDK 不可用。

这里锁住四条：显式 token、环境变量回退、无 token 时不带 Authorization、
显式 token 优先于环境变量。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_SDK_PATH = Path(__file__).resolve().parents[3] / "sdk" / "python" / "gate_client.py"


def _load_sdk():
    """按文件路径加载独立 SDK（它不在包内，不能普通 import）。"""
    spec = importlib.util.spec_from_file_location("agent_gate_standalone_sdk", _SDK_PATH)
    assert spec and spec.loader, f"找不到独立 SDK: {_SDK_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fake_response() -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"decision": "Allow"})
    return resp


def test_explicit_token_sends_bearer():
    GateClient = _load_sdk().GateClient
    client = GateClient(base_url="http://127.0.0.1:9100", api_token="tok-sdk")
    with patch.object(client._client, "post", return_value=_fake_response()) as post:
        client.analyze(agent_type="x", session_id="s", tool_name="bash", command="ls")
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer tok-sdk"
    client.close()


def test_env_token_fallback(monkeypatch):
    monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN", "tok-env")
    GateClient = _load_sdk().GateClient
    client = GateClient(base_url="http://127.0.0.1:9100")
    assert client._headers()["Authorization"] == "Bearer tok-env"
    client.close()


def test_no_token_sends_no_authorization(monkeypatch):
    monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN", raising=False)
    GateClient = _load_sdk().GateClient
    client = GateClient(base_url="http://127.0.0.1:9100")
    with patch.object(client._client, "post", return_value=_fake_response()) as post:
        client.analyze(agent_type="x", session_id="s", tool_name="bash", command="ls")
    assert post.call_args.kwargs["headers"] == {}
    client.close()


def test_explicit_token_overrides_env(monkeypatch):
    monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN", "tok-env")
    GateClient = _load_sdk().GateClient
    client = GateClient(base_url="http://127.0.0.1:9100", api_token="tok-explicit")
    assert client._headers()["Authorization"] == "Bearer tok-explicit"
    client.close()
