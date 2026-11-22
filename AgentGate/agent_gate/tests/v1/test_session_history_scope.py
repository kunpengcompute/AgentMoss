"""session_history 的来源与多 agent 覆盖（镜像 TS multi-session 测试）。

组合攻击针对整台服务器：history 必须覆盖**所有 agent**，不能只取「全局最近
N 个会话」——否则高频 agent 会把别人的会话全挤出去。每个会话还要带上来源
agent_id，AgentMoss 会把它记在链路径里供取证/展示。
"""
from __future__ import annotations

from agent_gate.shared.storage.db import Database
from agent_gate.shared.storage.repository import Repository


def _record(n: int = 0):
    return {
        "tool_name": "bash",
        "command": f"cmd-{n}",
        "description": "",
        "cwd": "/tmp",
        "action_type": "bash",
        "action_detail": f"cmd-{n}",
    }


def _moss():
    return {
        "decision": "Allow",
        "risk_level": "low",
        "risk_type": "",
        "violated_layers": [],
        "reason": "",
        "confidence": 0,
        "analysis_duration_ms": 0,
    }


def _repo(tmp_path):
    db = Database(str(tmp_path / "agent_gate.db"))
    db.init_schema()
    return db, Repository(db)


def test_history_includes_source_agent(tmp_path):
    db, repo = _repo(tmp_path)
    a1 = repo.get_or_create_agent("opendesk", "host", "0.1.0")
    repo.get_or_create_session("s1", a1)
    repo.insert_tool_call("s1", _record(1), _moss())

    hists = repo.get_recent_session_histories(exclude_session_id="other", limit=5)
    assert len(hists) == 1
    assert hists[0]["session_id"] == "s1"
    assert hists[0]["agent_id"] == "opendesk"
    assert hists[0]["action_history"]
    db.close()


def test_multi_agent_histories_all_covered(tmp_path):
    """两个 agent 各有会话时，limit 内必须都能出现（不被单一大户挤占）。"""
    db, repo = _repo(tmp_path)
    a1 = repo.get_or_create_agent("opendesk", "host", "0.1.0")
    a2 = repo.get_or_create_agent("xiaoo", "host", "0.1.0")

    # opendesk 造 3 个会话（活跃时间靠后）
    for i in range(3):
        sid = f"od-{i}"
        repo.get_or_create_session(sid, a1)
        repo.insert_tool_call(sid, _record(i), _moss())
    # xiaoo 造 1 个会话
    repo.get_or_create_session("xo-0", a2)
    repo.insert_tool_call("xo-0", _record(9), _moss())

    hists = repo.get_recent_session_histories(exclude_session_id="current", limit=2)
    agents = {h["agent_id"] for h in hists}
    assert agents == {"opendesk", "xiaoo"}, f"应覆盖两个 agent，实际 {agents}"
    db.close()


def test_single_agent_behavior_unchanged(tmp_path):
    """单 agent 时行为不变：按最近活跃取前 limit 个会话。"""
    db, repo = _repo(tmp_path)
    a1 = repo.get_or_create_agent("opendesk", "host", "0.1.0")
    for i in range(5):
        sid = f"s{i}"
        repo.get_or_create_session(sid, a1)
        repo.insert_tool_call(sid, _record(i), _moss())

    hists = repo.get_recent_session_histories(exclude_session_id="none", limit=3)
    assert len(hists) == 3
    assert all(h["agent_id"] == "opendesk" for h in hists)
    db.close()
