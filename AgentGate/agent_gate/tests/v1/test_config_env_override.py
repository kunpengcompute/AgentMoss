"""``AGENT_GATE_<SECTION>_<KEY>`` 环境变量覆盖 + AgentMoss 地址解析。

看护两件事（2026-10 修）：

1. ``_apply_env_overrides`` 对**带下划线的 section 名**（``agent_moss``）此前
   失效：旧实现按第一个下划线切分，得到 ``["agent", "moss_url"]``，而 ``_data``
   里没有 ``agent`` 这一节 —— 变量设了却**静默不生效**。
2. ``MossClient`` 此前只读配置文件，完全不看 ``AGENT_MOSS_URL`` / ``MOSS_URL``，
   于是 V1 的 ``server`` 子命令在分离部署里会一直连默认的 127.0.0.1:9090。
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_gate.shared import config as config_mod
from agent_gate.shared.client.moss_client import MossClient
from agent_gate.shared.config import Config


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """不读本机 ~/.config/agent_gate/config.yaml。"""
    monkeypatch.setattr(
        config_mod, "DEFAULT_CONFIG_PATHS", [tmp_path / "missing.yaml"]
    )


# ----------------------------------------------------------------------
# 配置项覆盖
# ----------------------------------------------------------------------


def test_simple_section_override(monkeypatch):
    monkeypatch.setenv("AGENT_GATE_SERVER_PORT", "9200")
    cfg = Config()
    assert cfg.server["port"] == 9200
    assert isinstance(cfg.server["port"], int), "类型应跟随默认值（int）"


def test_underscore_section_override(monkeypatch):
    """修复项：agent_moss 带下划线，旧实现拆成 agent + moss_url 后失效。"""
    monkeypatch.setenv("AGENT_GATE_AGENT_MOSS_URL", "http://10.0.0.5:9090")
    cfg = Config()
    assert cfg.agent_moss["url"] == "http://10.0.0.5:9090"


def test_float_override(monkeypatch):
    monkeypatch.setenv("AGENT_GATE_AGENT_MOSS_TIMEOUT", "45.5")
    cfg = Config()
    assert cfg.agent_moss["timeout"] == 45.5
    assert isinstance(cfg.agent_moss["timeout"], float)


def test_unknown_key_is_ignored(monkeypatch):
    """security.py 直接读的变量（如 AUTH_TOKEN）不该被这里当成配置项报错。"""
    monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN", "tok-1")
    monkeypatch.setenv("AGENT_GATE_AGENT_MOSS_NOPE", "x")
    cfg = Config()  # 不应抛异常
    assert cfg.agent_moss.get("nope") is None


# ----------------------------------------------------------------------
# MossClient 地址解析
# ----------------------------------------------------------------------


async def _base_url_of(monkeypatch, **env):
    for key in ("AGENT_MOSS_URL", "MOSS_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    client = MossClient()
    try:
        return client._base_url
    finally:
        await client.close()


async def test_explicit_base_url_wins(monkeypatch):
    client = MossClient(base_url="http://explicit:1234")
    try:
        assert client._base_url == "http://explicit:1234"
    finally:
        await client.close()


async def test_agent_moss_url_beats_moss_url(monkeypatch):
    url = await _base_url_of(
        monkeypatch,
        AGENT_MOSS_URL="http://from-agent-moss:9090",
        MOSS_URL="http://from-moss:9090",
    )
    assert url == "http://from-agent-moss:9090"


async def test_moss_url_fallback(monkeypatch):
    url = await _base_url_of(monkeypatch, MOSS_URL="http://from-moss:9090")
    assert url == "http://from-moss:9090"


async def test_default_when_nothing_set(monkeypatch, tmp_path):
    # 隔离真实端口文件（本机可能正跑 AgentMoss 并写了别的端口）
    monkeypatch.setenv("AGENT_MOSS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AGENT_MOSS_INSTANCE", raising=False)
    url = await _base_url_of(monkeypatch)
    assert url == "http://127.0.0.1:9090"


async def test_default_timeout_covers_moss_budget(monkeypatch):
    monkeypatch.delenv("AGENT_GATE_MOSS_TIMEOUT", raising=False)
    client = MossClient()
    try:
        assert client._timeout == 330.0
        assert client._max_retries == 0
    finally:
        await client.close()


def test_shipped_config_matches_moss_budget():
    config_path = Path(__file__).resolve().parents[3] / "config" / "agent_gate.yaml"
    cfg = Config(config_path)
    assert cfg.agent_moss["timeout"] == 330.0
    assert cfg.agent_moss["max_retries"] == 0
