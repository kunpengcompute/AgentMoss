"""自动 token 生成失败 / 显式 token 文件不可读 → fail-closed。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent_gate.shared.server import security as sp


class TestAutoTokenFailure:
    def _set_auto(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "auto")
        monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN_FILE", raising=False)
        monkeypatch.delenv("AGENT_GATE_AUTH_TOKEN_FD", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def test_auto_token_unwritable_is_config_error(self, monkeypatch, tmp_path):
        bad_cfg = tmp_path / "cfg-as-file"
        bad_cfg.write_text("x", encoding="utf-8")
        self._set_auto(monkeypatch, tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(bad_cfg))
        sp.reset_counters()
        assert sp.auth_config_error()
        assert sp.auth_enabled() is True
        assert sp.check_request_auth({}, "/api/v1/tool_calls")[0] is False
        assert sp.check_request_auth({}, "/api/v1/health")[0] is True
        with pytest.raises(SystemExit):
            sp.fail_fast_on_auth_error()

    def test_explicit_token_file_unreadable_is_config_error(self, monkeypatch, tmp_path):
        self._set_auto(monkeypatch, tmp_path)
        monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN_FILE", str(tmp_path / "missing-token"))
        sp.reset_counters()
        assert sp.auth_config_error()
        assert sp.auth_enabled() is True
        assert not (tmp_path / "agentgate" / "auth_token").exists()
        assert sp.check_request_auth({}, "/api/v1/tool_calls")[0] is False
        assert sp.check_request_auth({}, "/api/v1/health")[0] is True
        with pytest.raises(SystemExit):
            sp.fail_fast_on_auth_error()

    def test_off_mode_ignores_bad_token_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "off")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_GATE_AUTH_TOKEN_FILE", str(tmp_path / "missing-token"))
        sp.reset_counters()
        assert sp.auth_config_error() == ""
        assert sp.auth_enabled() is False
        assert sp.check_request_auth({}, "/api/v1/tool_calls")[0] is True
