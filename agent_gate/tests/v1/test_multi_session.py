"""跨会话分析开关（``AGENT_GATE_MULTI_SESSION``）。

默认开：未设置时组装 ``session_history``；显式 ``0 / false / no / off``
（大小写不敏感）时跳过跨会话查询。
"""

from unittest.mock import MagicMock

import pytest

from agent_gate.v1.normalizer import Normalizer
from agent_gate.v1.multi_session import (
    multi_session_enabled,
    stateful_incremental_enabled,
)
from agent_gate.v1.server.models import AgentGateInput


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("AGENT_GATE_MULTI_SESSION", raising=False)


def _make_input() -> AgentGateInput:
    return AgentGateInput(
        agent_type="opendesk",
        session_id="sess-current",
        tool_name="bash",
        command="ls",
        cwd="/tmp",
    )


def _make_context():
    ctx = MagicMock()
    ctx.os.hostname = "host-1"
    ctx.os.system = "linux"
    return ctx


# ----------------------------------------------------------------------
# 开关本身
# ----------------------------------------------------------------------


def test_default_is_on():
    assert multi_session_enabled() is True
    assert stateful_incremental_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " false ", "Off"])
def test_explicit_off_values(monkeypatch, value):
    monkeypatch.setenv("AGENT_GATE_MULTI_SESSION", value)
    assert multi_session_enabled() is False
    assert stateful_incremental_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "anything"])
def test_explicit_on_values(monkeypatch, value):
    monkeypatch.setenv("AGENT_GATE_MULTI_SESSION", value)
    assert multi_session_enabled() is True


# ----------------------------------------------------------------------
# Normalizer：默认查、关闭不查
# ----------------------------------------------------------------------


async def test_normalizer_queries_history_by_default():
    repo = MagicMock()
    repo.get_action_history = MagicMock(return_value=[])
    repo.get_recent_session_histories = MagicMock(return_value=[])

    await Normalizer(repo=repo).normalize(_make_input(), _make_context())

    assert repo.get_recent_session_histories.called


async def test_normalizer_skips_history_when_disabled(monkeypatch):
    monkeypatch.setenv("AGENT_GATE_MULTI_SESSION", "0")
    repo = MagicMock()
    repo.get_action_history = MagicMock(return_value=[])
    repo.get_recent_session_histories = MagicMock(return_value=[])

    req = await Normalizer(repo=repo).normalize(_make_input(), _make_context())

    assert not repo.get_recent_session_histories.called
    assert req["session_history"] == []


# ----------------------------------------------------------------------
# Normalizer：stateful_incremental 注入
# ----------------------------------------------------------------------


async def test_normalizer_injects_stateful_incremental_by_default():
    """默认开：请求必须带 metadata.stateful_incremental=true。

    2026-10 修：Python 侧此前定义了 stateful_incremental_enabled() 但从没
    往请求里注入 —— 同一份配置下 TS 链路走 AgentMoss 增量引擎、Python 不走。
    """
    repo = MagicMock()
    repo.get_action_history = MagicMock(return_value=[])
    repo.get_recent_session_histories = MagicMock(return_value=[])

    req = await Normalizer(repo=repo).normalize(_make_input(), _make_context())

    assert req["metadata"].get("stateful_incremental") is True


async def test_normalizer_skips_stateful_incremental_when_disabled(monkeypatch):
    monkeypatch.setenv("AGENT_GATE_MULTI_SESSION", "0")
    repo = MagicMock()
    repo.get_action_history = MagicMock(return_value=[])
    repo.get_recent_session_histories = MagicMock(return_value=[])

    req = await Normalizer(repo=repo).normalize(_make_input(), _make_context())

    assert "stateful_incremental" not in req["metadata"]


# ----------------------------------------------------------------------
# 跨会话窗口可配（默认不变，保证性能）
# ----------------------------------------------------------------------


def test_session_history_limit_default_and_bounds(monkeypatch):
    from agent_gate.v1.multi_session import session_history_limit

    monkeypatch.delenv("AGENT_GATE_SESSION_HISTORY_LIMIT", raising=False)
    assert session_history_limit() == 5          # 默认保持现有性能

    monkeypatch.setenv("AGENT_GATE_SESSION_HISTORY_LIMIT", "20")
    assert session_history_limit() == 20

    monkeypatch.setenv("AGENT_GATE_SESSION_HISTORY_LIMIT", "999")
    assert session_history_limit() == 50         # 上限防误配

    monkeypatch.setenv("AGENT_GATE_SESSION_HISTORY_LIMIT", "abc")
    assert session_history_limit() == 5
