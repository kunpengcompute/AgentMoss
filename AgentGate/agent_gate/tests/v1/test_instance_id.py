"""安装级实例 ID（instance_id）—— 展示层脱敏「同实例不隐藏」的载体。

与 TS 版 ``ts/tests/shared/agent-identity.test.ts`` 对称看护：
- get_instance_id：env 覆盖 > 持久化文件 > 生成并落盘（0600），进程内缓存；
- Normalizer 透传：调用方显式带的 instance_id 原样透传，缺省时用网关自己的
  持久化实例 ID（AgentMoss 据此判断攻击链图节点是否同实例）。
"""

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock

from agent_gate.shared import instance_id as iid_mod
from agent_gate.shared.instance_id import get_instance_id
from agent_gate.shared.normalizer import Normalizer
from agent_gate.shared.server.models import AgentGateInput


def test_get_instance_id_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_GATE_INSTANCE_ID", "env-instance")
    iid_mod._cached = ""
    assert get_instance_id() == "env-instance"


def test_get_instance_id_persists_and_reuses(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_GATE_INSTANCE_ID", raising=False)
    monkeypatch.setenv("AGENT_GATE_INSTANCE_ID_PATH", str(tmp_path / "instance_id"))
    iid_mod._cached = ""
    first = get_instance_id()
    iid_mod._cached = ""  # 模拟新进程：清缓存后必须从文件读到同一个 ID
    second = get_instance_id()
    assert first == second
    stored = (tmp_path / "instance_id").read_text().strip()
    assert stored == first


def test_normalizer_passes_explicit_instance_id():
    """调用方显式带 instance_id → 原样透传给 AgentMoss。"""
    inp = AgentGateInput(
        agent_type="xiaoo",
        session_id="s1",
        tool_name="bash",
        command="ls",
        instance_id="caller-iid",
    )
    norm = Normalizer(repo=None)
    req = asyncio.run(norm.normalize(inp, _make_context()))
    assert req["instance_id"] == "caller-iid"


def test_normalizer_falls_back_to_gate_instance_id(monkeypatch, tmp_path):
    """调用方不带 → 用网关自己的持久化实例 ID（同实例多会话不隐藏）。"""
    monkeypatch.delenv("AGENT_GATE_INSTANCE_ID", raising=False)
    monkeypatch.setenv("AGENT_GATE_INSTANCE_ID_PATH", str(tmp_path / "instance_id"))
    iid_mod._cached = ""
    inp = AgentGateInput(agent_type="xiaoo", session_id="s1", tool_name="bash", command="ls")
    norm = Normalizer(repo=None)
    req = asyncio.run(norm.normalize(inp, _make_context()))
    assert req["instance_id"]
    assert req["instance_id"] == (tmp_path / "instance_id").read_text().strip()


def _make_context():
    ctx = MagicMock()
    ctx.os.hostname = "host-1"
    ctx.os.system = "linux"
    ctx.os.user = "u"
    ctx.process.pid = 1
    ctx.process.executable = "/bin/bash"
    return ctx
