"""Configuration sources work without a dedicated plugin configuration file."""

import json

import httpx
import pytest

from agentmoss_deepagents import GateRuntime
from agentmoss_deepagents.client import GateClient
from agentmoss_deepagents.config import GateConfig


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    import os

    for key in os.environ:
        if key.startswith("DEEPAGENTS_GATE_"):
            monkeypatch.delenv(key)


async def test_environment_runtime_sends_token_and_applies_policy(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_GATE_API_TOKEN", "env-test-secret")
    monkeypatch.setenv("DEEPAGENTS_GATE_URL", "http://gate.test:9100")
    monkeypatch.setenv("DEEPAGENTS_GATE_FAIL_CLOSED", "true")
    monkeypatch.setenv("DEEPAGENTS_GATE_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("DEEPAGENTS_GATE_HISTORY_ACTIONS", "12")
    runtime = GateRuntime.load()
    assert runtime.config.fail_closed is True
    assert runtime.config.timeout_seconds == 2.5
    assert runtime.config.history_actions == 12
    assert "env-test-secret" not in repr(runtime.config)
    await runtime.client.aclose()

    def gate(request):
        assert str(request.url) == "http://gate.test:9100/api/v1/analyze"
        assert request.headers["authorization"] == "Bearer env-test-secret"
        assert json.loads(request.content) == {"command": "pwd"}
        return httpx.Response(200, json={"decision": "Deny"})

    runtime.client = GateClient(runtime.config, transport=httpx.MockTransport(gate))
    try:
        assert (await runtime.client.aanalyze({"command": "pwd"}))["decision"] == "Deny"
    finally:
        await runtime.aclose()


async def test_application_config_is_explicit_and_not_mixed_with_environment(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_GATE_API_TOKEN", "environment-secret")
    monkeypatch.setenv("DEEPAGENTS_GATE_FAIL_CLOSED", "true")
    source = {"api_token": "application-secret", "timeout_seconds": 3}
    runtime = GateRuntime.load(source)
    try:
        assert runtime.client.token == "application-secret"
        assert runtime.config.fail_closed is False
        assert source == {"api_token": "application-secret", "timeout_seconds": 3}
        assert runtime.redact("application-secret") == "[redacted]"
    finally:
        await runtime.aclose()


async def test_environment_token_file_and_existing_config_object(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("file-test-secret\n")
    monkeypatch.setenv("DEEPAGENTS_GATE_API_TOKEN_FILE", str(token))
    config = GateConfig.from_env()
    runtime = GateRuntime.load(config)
    try:
        assert runtime.client.token == "file-test-secret"
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    "key,value",
    [
        ("ENABLED", "maybe"),
        ("FAIL_CLOSED", ""),
        ("TIMEOUT_SECONDS", "nan"),
        ("TIMEOUT_SECONDS", "0"),
        ("HISTORY_ACTIONS", "0"),
        ("HISTORY_BYTES", "1.5"),
    ],
)
def test_invalid_environment_values_are_rejected(monkeypatch, key, value):
    monkeypatch.setenv("DEEPAGENTS_GATE_API_TOKEN", "test-secret")
    monkeypatch.setenv("DEEPAGENTS_GATE_" + key, value)
    with pytest.raises(ValueError):
        GateConfig.from_env()


def test_missing_and_conflicting_credentials_never_silently_disable(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        GateRuntime.load()
    token = tmp_path / "token"
    token.write_text("file-secret")
    monkeypatch.setenv("DEEPAGENTS_GATE_API_TOKEN", "direct-secret")
    monkeypatch.setenv("DEEPAGENTS_GATE_API_TOKEN_FILE", str(token))
    with pytest.raises(ValueError):
        GateRuntime.load()


async def test_explicit_disabled_runtime_needs_no_credentials(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_GATE_ENABLED", "false")
    runtime = GateRuntime.load()
    try:
        assert runtime.status()["enabled"] is False
        assert runtime.client.token == ""
    finally:
        await runtime.aclose()


async def test_legacy_keyword_path_remains_explicit_and_compatible(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("legacy-secret")
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"api_token_file": "token"}))
    monkeypatch.setenv("DEEPAGENTS_GATE_API_TOKEN", "different-env-secret")
    runtime = GateRuntime.load(config_path=path)
    try:
        assert runtime.client.token == "legacy-secret"
    finally:
        await runtime.aclose()
    with pytest.raises(ValueError):
        GateRuntime.load({"api_token": "explicit-secret"}, config_path=path)


def test_application_config_rejects_unknown_fields_and_non_boolean_flags():
    for values in ({"api_token": "secret", "fail_closed": "false"}, {"api_token": "secret", "typo": True}):
        with pytest.raises(ValueError):
            GateConfig.from_mapping(values)
