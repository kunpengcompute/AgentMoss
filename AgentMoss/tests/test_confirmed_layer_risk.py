"""仅内联代码中的文件线索可以暂缓，独立命中的 L1 拒绝必须保留。"""

from unittest.mock import Mock

import pytest

from agent_moss import agents
from agent_moss.agents.base import AgentExtensions
from agent_moss.engine.analyzer import analyze
from agent_moss.engine.llm_analyzer import LLMAnalyzer
from agent_moss.engine.logic_rules import LogicRulesChecker
from agent_moss.profiles import get_profile
from agent_moss.profiles.base import SensitivePath


_GUIDE = "/tmp/example/guide.txt"
_PUBLISHED = "/tmp/example/published.txt"
_LOG = 'python3 -c "print(42)"'


@pytest.fixture
def check(monkeypatch, default_config):
    monkeypatch.setenv("AGENT_MOSS_DISABLE_HEURISTIC", "0")
    monkeypatch.setenv("AGENT_MOSS_DISABLE_LOGIC_RULES", "1")
    monkeypatch.setenv("AGENT_MOSS_DISABLE_LLM", "1")
    policy = AgentExtensions(agent_id="fixture", extra_sensitive_paths=[
        SensitivePath(path=_GUIDE, deny_mode="deny_read", risk_level="high"),
        SensitivePath(path=_PUBLISHED, deny_mode="deny_write", risk_level="medium"),
    ])
    monkeypatch.setattr(agents, "get_agent_extensions", lambda agent_id: policy)

    def run(command, tool="bash"):
        return analyze(
            session_id="deferred-l1", prompt_session="", action_history=[], reason="",
            a_next={"action_type": tool, "action_detail": command},
            config=default_config, agent_id="fixture", profile=get_profile("linux"),
        )

    return run


@pytest.mark.parametrize("tool", ["bash", "executeBash", "terminal-executeBash"])
@pytest.mark.parametrize("command", [
    f"cat {_GUIDE} && {_LOG}",
    f"{_LOG}; cat '{_GUIDE}'",
    f"{_LOG} | cat {_GUIDE}",
])
def test_independent_file_denial_survives_inline_command(check, tool, command):
    direct = check(f"cat {_GUIDE}", tool)
    combined = check(command, tool)
    assert (direct["decision"], direct["violated_layers"]) == ("Deny", ["L1.1"])
    for field in ("decision", "reason", "risk_type", "risk_level", "source", "violated_layers"):
        assert combined[field] == direct[field]


def test_confirmed_l1_denial_precedes_l2_and_l3(check, monkeypatch):
    monkeypatch.setenv("AGENT_MOSS_DISABLE_LOGIC_RULES", "0")
    monkeypatch.setenv("AGENT_MOSS_DISABLE_LLM", "0")
    l2, l3 = Mock(), Mock()
    monkeypatch.setattr(LogicRulesChecker, "check", l2)
    monkeypatch.setattr(LLMAnalyzer, "analyze", l3)
    result = check(f"{_LOG} && cat {_GUIDE}")
    assert (result["decision"], result["violated_layers"]) == ("Deny", ["L1.1"])
    l2.assert_not_called()
    l3.assert_not_called()


def test_independent_write_keeps_its_original_policy(check):
    result = check(f"{_LOG} && rm {_PUBLISHED}")
    assert (result["decision"], result["risk_level"], result["violated_layers"]) == (
        "Deny", "medium", ["L1.1"],
    )


def test_disabled_l1_leaves_file_decision_to_later_layers(check, monkeypatch):
    monkeypatch.setenv("AGENT_MOSS_DISABLE_HEURISTIC", "1")
    result = check(f"{_LOG} && cat {_GUIDE}")
    assert (result["decision"], result["violated_layers"]) == ("Allow", [])


@pytest.mark.parametrize("command", [
    f'''python3 -c "print('cat {_GUIDE}; done')"''',
    f"""python3 -c "print('{_GUIDE}')" && cat /tmp/notes.txt""",
    f'''cat /tmp/notes.txt && python3 -c "print('{_GUIDE}')"''',
    f"""{_LOG} && echo 'cat {_GUIDE}'""",
    f"""{_LOG} # comment; cat {_GUIDE}""",
    f"""{_LOG}; NOTES='{_GUIDE}'""",
    f"""{_LOG}; if false; then cat {_GUIDE}; fi""",
    f"""{_LOG}; grep -F '{_GUIDE}' /tmp/notes.txt""",
    f"""{_LOG}; cat <(echo '{_GUIDE}')""",
    f"""cat {_PUBLISHED}; {_LOG}; cp /tmp/a /tmp/b""",
    f"""{_LOG}; cat <<END
cat {_GUIDE}
END""",
])
def test_unconfirmed_file_reference_continues_through_original_flow(check, command):
    assert check(command)["decision"] == "Allow"
