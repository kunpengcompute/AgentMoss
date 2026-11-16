"""Behavioral regressions found by the independent branch reviewer."""

import pytest
from deepagents.backends.local_shell import LocalShellBackend


@pytest.mark.parametrize("filename", ["a b.txt", "目录 file.txt"])
def test_space_path_survives_actual_moss_path_parser(tmp_path, filename):
    from agent_moss.engine.logic_rules import LogicRulesChecker
    from agent_moss.profiles import get_profile

    from agentmoss_deepagents.actions import ActionAdapter

    target = tmp_path / filename
    target.write_text("before")
    action = ActionAdapter(LocalShellBackend(root_dir=tmp_path)).adapt(
        "edit_file", {"file_path": "/" + filename, "old_string": "before", "new_string": "after"}
    )
    checker = LogicRulesChecker(get_profile("linux"))
    result = checker._check_read_before_write([], {"action_type": action.tool_name, "action_detail": action.command})
    assert result.hit is True, "Moss lost the real target at the space and treated it as a new file"
