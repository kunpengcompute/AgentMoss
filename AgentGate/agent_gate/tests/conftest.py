"""pytest 配置（agent_gate/tests 作用域）。"""

import pytest


@pytest.fixture(autouse=True)
def _auth_off_by_default(monkeypatch):
    """存量用例默认关闭鉴权；鉴权/来源校验由 tests/v1/test_security.py 覆盖。"""
    monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "off")
    monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_GATE_AGENT_TOKENS", raising=False)
    try:
        from agent_gate.shared.server import security as sp
        sp.reset_counters()
    except Exception:
        pass
    yield
    try:
        from agent_gate.shared.server import security as sp
        sp.reset_counters()
    except Exception:
        pass
