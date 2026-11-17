"""An absent inherited token FD must not be replaced by the SQLite file FD."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("empty_fd", [False, True])
def test_cli_rejects_missing_or_empty_auth_fd_before_opening_database(tmp_path, empty_fd):
    env = dict(os.environ)
    for name in ("AGENT_GATE_AUTH_TOKEN", "AGENT_GATE_AUTH_TOKEN_FILE", "AGENT_GATE_AGENT_TOKENS"):
        env.pop(name, None)
    env.update({
        "AGENT_GATE_AUTH_MODE": "auto",
        "AGENT_GATE_AUTH_TOKEN_FD": "3",
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "AGENT_GATE_STORAGE_DB_PATH": str(tmp_path / "gate.db"),
    })
    # Ensure FD 3 is absent (or empty) at the real CLI entry point, regardless
    # of descriptors inherited by the pytest runner.
    script = """
import os
from agent_gate.v1.cli import main
try:
    os.close(3)
except OSError:
    pass
"""
    if empty_fd:
        script += "fd = os.open(os.devnull, os.O_RDONLY)\nos.dup2(fd, 3)\n"
    script += "main(['server', '--host', '127.0.0.1', '--port', '0'])\n"
    proc = subprocess.Popen(
        [sys.executable, "-c", script], env=env, cwd=tmp_path,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=4)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.communicate(timeout=5)
        pytest.fail("CLI started with an absent auth FD instead of rejecting the configuration")
    assert proc.returncode != 0
    assert "AGENT_GATE_AUTH_TOKEN_FD" in stdout + stderr
    assert not (tmp_path / "gate.db").exists()
    assert not (tmp_path / "config" / "agentgate" / "auth_token").exists()
