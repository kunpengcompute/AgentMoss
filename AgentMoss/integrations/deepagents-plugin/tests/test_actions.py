"""Catch wrong policy targets and lossy command serialization."""

import json

import pytest
from deepagents.backends.local_shell import LocalShellBackend


@pytest.fixture
def adapter(tmp_path):
    from agentmoss_deepagents.actions import ActionAdapter

    return ActionAdapter(LocalShellBackend(root_dir=tmp_path, virtual_mode=True))


def test_execute_preserves_shell(adapter, tmp_path):
    command = "printf '%s' '中文 $HOME'; cd /tmp && pwd"
    action = adapter.adapt("execute", {"command": command, "timeout": 12})
    assert action.tool_name == "bash"
    assert action.command == command
    assert action.cwd == str(tmp_path)


@pytest.mark.parametrize("name,want", [("read_file", "read"), ("write_file", "write"), ("edit_file", "edit")])
def test_file_paths_follow_virtual_root(adapter, tmp_path, name, want):
    action = adapter.adapt(
        name, {"file_path": "/目录/a b.txt", "content": "keep", "old_string": "a", "new_string": "b"}
    )
    assert action.tool_name == want
    assert action.command.splitlines()[0] == f"'{tmp_path}/目录/a b.txt'"
    assert action.metadata["virtual_path"] == "/目录/a b.txt"


def test_delete_is_analysis_only(adapter, tmp_path):
    target = tmp_path / "a b.txt"
    target.write_text("keep")
    args = {"file_path": "/a b.txt"}
    action = adapter.adapt("delete", args)
    assert action.tool_name == "bash"
    assert action.command == f"rm -r -- '{target}'"
    assert action.metadata["synthetic_action"] is True
    assert args == {"file_path": "/a b.txt"}
    assert target.read_text() == "keep"


def test_glob_pattern_not_resolved_as_path(adapter, tmp_path):
    action = adapter.adapt("glob", {"pattern": "**/*.py", "path": "/src"})
    assert "**/*.py" in action.command
    assert str(tmp_path / "src") in action.command
    assert str(tmp_path / "**") not in action.command
    assert adapter.adapt("ls", {"path": "/"}).tool_name == "list_dir"


def test_unicode_space_content_preserved(adapter):
    content = "hello\n中文\\path\n'quotes'\n" + "A" * 9000
    action = adapter.adapt("write_file", {"file_path": "/a.txt", "content": content})
    assert json.loads(action.command.split("\n", 1)[1])["content"] == content


def test_custom_tool_parameters_preserved(adapter):
    args = {"url": "https://example.test", "nested": {"value": "中文"}, "count": 3}
    action = adapter.adapt("custom", args)
    assert json.loads(action.command) == args


def test_configuration_rejects_missing_credentials_and_bad_values(tmp_path):
    from agentmoss_deepagents.config import GateConfig

    path = tmp_path / "config.json"
    path.write_text("{}")
    with pytest.raises(ValueError):
        GateConfig.load(path)
    token = tmp_path / "token"
    token.write_text("secret-test-value")
    for override in ({"timeout_seconds": -1}, {"fail_closed": "false"}, {"gate_url": "ftp://x"}, {"enabled": "false"}):
        path.write_text(json.dumps({"api_token_file": str(token), **override}))
        with pytest.raises(ValueError):
            GateConfig.load(path)
    path.write_text(json.dumps({"api_token_file": str(token)}))
    config = GateConfig.load(path)
    assert config.fail_closed is True
    assert config.timeout_seconds == 5
    assert "secret-test-value" not in repr(config)
    assert GateConfig.load(path).gate_url == "http://127.0.0.1:9100"


def test_missing_configuration_is_not_silent_disable(tmp_path):
    from agentmoss_deepagents.config import GateConfig

    with pytest.raises((ValueError, OSError)):
        GateConfig.load(tmp_path / "missing.json")


def test_edit_parameter_boundaries_and_original_arguments_are_preserved(adapter):
    first = {"file_path": "/x", "old_string": "a\nb", "new_string": "c"}
    second = {"file_path": "/x", "old_string": "a", "new_string": "b\nc"}
    a, b = adapter.adapt("edit_file", first), adapter.adapt("edit_file", second)
    assert a.command != b.command
    assert a.metadata["original_arguments"] == first
    assert b.metadata["original_arguments"] == second
