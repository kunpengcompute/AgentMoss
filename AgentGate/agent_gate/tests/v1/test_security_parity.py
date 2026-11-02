"""安全判定金标准用例。

``tests/fixtures/security_cases.json`` 是唯一事实源，覆盖来源校验 / 鉴权 /
身份绑定三套判定。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_gate.v1.server import security as sp

ROOT = Path(__file__).resolve().parents[3]
CASES = json.loads((ROOT / "tests" / "fixtures" / "security_cases.json").read_text(encoding="utf-8"))

GLOBAL_TOKEN = "good"
AGENT_TOKENS = "opendesk=tok-open,qwenpaw=tok-qwen"


@pytest.fixture(autouse=True)
def _fixed_tokens(monkeypatch):
    monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "auto")
    monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN", GLOBAL_TOKEN)
    monkeypatch.setenv("AGENT_GATE_AGENT_TOKENS", AGENT_TOKENS)
    monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN_FILE", raising=False)
    monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN_FD", raising=False)
    sp.reset_counters()
    yield
    sp.reset_counters()


@pytest.mark.parametrize("case", CASES["origin"], ids=lambda c: f"{c['origin'] or '<none>'}@{c['host'] or '<none>'}")
def test_origin_allowed(case):
    assert sp.origin_allowed(case["origin"], case["host"], case["extra"]) is case["allowed"]


@pytest.mark.parametrize("case", CASES["auth"], ids=lambda c: c["name"])
def test_request_auth(case):
    headers = {}
    if case["bearer"]:
        headers["authorization"] = f"Bearer {case['bearer']}"
    ok, principal = sp.check_request_auth(headers, case["path"], case["cookie"])
    assert ok is case["ok"], f"{case['name']}: ok={ok} 期望 {case['ok']}"
    assert principal.startswith(case["principalPrefix"]), (
        f"{case['name']}: principal={principal!r} 不以 {case['principalPrefix']!r} 开头"
    )
    if case["principalPrefix"] == "":
        assert principal == ""


@pytest.mark.parametrize("case", CASES["binding"], ids=lambda c: c["name"])
def test_bind_agent(case):
    ok, _reason = sp.bind_agent(case["principal"], case["agentType"])
    assert ok is case["ok"], f"{case['name']}: ok={ok} 期望 {case['ok']}"


@pytest.mark.parametrize("case", CASES["cookie_paths"], ids=lambda c: c["path"])
def test_cookie_path_allowed(case):
    assert sp.cookie_path_allowed(case["path"]) is case["cookieAllowed"]
