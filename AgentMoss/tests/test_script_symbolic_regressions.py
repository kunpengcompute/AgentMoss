"""LiteSE 符号执行回归：通配符/正则/嵌套结构/变量预算。

对应上游反馈的 LS-30 / LS-31 / LS-25 补充 / LS-28 补充。前三个是漏判
（危险命令被剪枝），最后一个是变量表上限未覆盖启动参数。
"""
import pytest

from agent_moss.engine.script_engine.backends import shell_backend, symbolic


@pytest.mark.parametrize(
    ("labels", "arg", "selected"),
    [
        (("bak*", "*"), "bak20260101", 0),
        (("bak*", "*"), "deploy", 1),
        (("bak*", "bak20260101"), "bak20260101", 0),
        (("*", "bak20260101"), "bak20260101", 0),
        (("archive|bak[0-9]?", "*"), "bak42", 0),
        (('"bak*"', "*"), "bak42", 1),
        (('"a|*|b"', "*"), "bak42", 1),
    ],
)
@pytest.mark.parametrize("nested", [False, True])
def test_shell_backend_case_glob_keeps_first_matching_branch(labels, arg, selected, nested):
    first_body = "echo first_branch ;;"
    if nested:
        first_body = "echo first_branch\n    case $2 in\n      x) echo nested_branch ;;\n    esac\n    ;;"
    script = f"case $1 in\n  {labels[0]}) {first_body}\n  {labels[1]}) echo second_branch ;;\nesac"

    ir = shell_backend.ShellBackend().parse(script, [arg])

    assert ir.control_flow_resolved
    markers = ("echo first_branch", "echo second_branch")
    assert markers[selected] in ir.reachable_content
    assert markers[1 - selected] not in ir.reachable_content


@pytest.mark.parametrize("terminator", [";&", ";;&"])
def test_shell_backend_case_fallthrough_keeps_later_branches(terminator):
    script = f"""case $1 in
  bak*) echo first_branch {terminator}
  *) echo second_branch ;;
esac"""

    ir = shell_backend.ShellBackend().parse(script, ["bak42"])

    assert not ir.control_flow_resolved
    assert "echo first_branch" in ir.reachable_content
    assert "echo second_branch" in ir.reachable_content


@pytest.mark.parametrize("selector", ["$1", '"$1"'])
@pytest.mark.parametrize("arg,selected", [("bak42", "first"), ("other", "second")])
def test_case_alternatives_match_in_both_parsers(selector, arg, selected):
    script = f"""case {selector} in
  archive|bak*) echo first_branch ;;
  *) echo second_branch ;;
esac"""

    diagnostics = {}
    ir = shell_backend.ShellBackend().parse(script, [arg], diagnostics=diagnostics)

    assert ir.control_flow_resolved
    assert f"echo {selected}_branch" in ir.reachable_content
    assert ("echo first_branch" in ir.reachable_content) is (selected == "first")
    assert ("echo second_branch" in ir.reachable_content) is (selected == "second")
    assert diagnostics["parser"] == ("shell_p1" if selector == "$1" else "shell_litese")


@pytest.mark.parametrize("selector", ["$1", '"$1"'])
@pytest.mark.parametrize("literal", ["# ;& ;;&", 'echo "case ;& ;;&"', r"echo \;\&", 'echo prefix";&"'])
def test_case_text_does_not_trigger_fallthrough(selector, literal):
    script = f"""case {selector} in
  bak*)
    {literal}
    echo first_branch ;; # ;&
  *) echo second_branch ;;
esac"""

    ir = shell_backend.ShellBackend().parse(script, ["bak42"])

    assert ir.control_flow_resolved
    assert "echo first_branch" in ir.reachable_content
    assert "echo second_branch" not in ir.reachable_content


@pytest.mark.parametrize("selector", ["$1", '"$1"'])
@pytest.mark.parametrize("terminator", [";&", ";;&"])
def test_case_fallthrough_tracks_bound_child(tmp_path, selector, terminator):
    from agent_moss.engine.script_engine.tracker import RecursiveTracker

    parent = tmp_path / "parent.sh"
    child = tmp_path / "child script.sh"
    child.write_text("echo child_body", encoding="utf-8")
    script = f"""case {selector} in
  bak*) echo first_branch {terminator} # keep scanning
  *) bash "$2" ;;
esac"""
    parent.write_text(script, encoding="utf-8")
    args = ["bak42", child.name]

    ir = shell_backend.ShellBackend().parse(script, args)
    result = RecursiveTracker().track(str(parent), args)

    assert not ir.control_flow_resolved
    assert "echo first_branch" in ir.reachable_content
    assert [call.path for call in ir.calls] == [child.name]
    assert [node.script_path for node in result.nodes] == [str(parent), str(child)]


@pytest.mark.parametrize("selector", ["$1", '"$1"'])
@pytest.mark.parametrize("arg", ["a", "b"])
def test_case_caret_negation_keeps_all_branches(selector, arg):
    script = f"""case {selector} in
  [^a]) echo first_branch ;;
  *) echo second_branch ;;
esac"""
    ir = shell_backend.ShellBackend().parse(script, [arg])

    assert not ir.control_flow_resolved
    assert "echo first_branch" in ir.reachable_content
    assert "echo second_branch" in ir.reachable_content


@pytest.mark.parametrize("selector", ["$1", '"$1"'])
@pytest.mark.parametrize("boundary", [";", "&&", "||"])
def test_inline_nested_case_keeps_outer_and_inner_bodies(selector, boundary):
    script = f"""case {selector} in
  bak*) echo before_nested {boundary} case "$2" in
      on) echo inner_on ;;
      off) echo inner_off ;;
    esac
    echo after_nested
    ;;
  *) echo outer_default ;;
esac"""

    ir = shell_backend.ShellBackend().parse(script, ["bak42", "off"])

    assert ir.control_flow_resolved
    for marker in ("inner_on", "inner_off", "after_nested"):
        assert f"echo {marker}" in ir.reachable_content
    assert "echo outer_default" not in ir.reachable_content


def test_case_with_ambiguous_inline_closer_keeps_full_text():
    script = """case $1 in
  bak*) echo before; case $2 in
      off) echo inner_off ;; esac
    echo after
    ;;
  *) echo outer_default ;;
esac"""

    ir = shell_backend.ShellBackend().parse(script, ["bak42", "off"])

    assert not ir.control_flow_resolved
    assert "echo inner_off" in ir.reachable_content
    assert "echo after" in ir.reachable_content
    assert "echo outer_default" in ir.reachable_content


@pytest.mark.parametrize("selector", ["$1", '"$1"'])
@pytest.mark.parametrize("arg,selected", [("bak42", "first"), ("other", "second")])
def test_split_case_header_selects_branch(selector, arg, selected):
    script = f"""case {selector}
  # the keyword below completes the header
in
  bak*) echo first_branch ;;
  *) echo second_branch ;;
esac"""

    ir = shell_backend.ShellBackend().parse(script, [arg])

    assert ir.control_flow_resolved
    assert ("echo first_branch" in ir.reachable_content) is (selected == "first")
    assert ("echo second_branch" in ir.reachable_content) is (selected == "second")


def test_litese_child_arguments_preserve_shell_quoting(tmp_path):
    from agent_moss.engine.script_engine.tracker import RecursiveTracker

    parent = tmp_path / "parent.sh"
    child = tmp_path / "child.sh"
    script = r"""case "$1" in
  run) bash child.sh '$1' \$1 "\$1" "$1" "\${2}" "${2}" ;;
  *) echo other_branch ;;
esac"""
    parent.write_text(script, encoding="utf-8")
    child.write_text("""case "$1" in
  '$1') echo literal_branch ;;
  *) echo expanded_branch ;;
esac""", encoding="utf-8")
    args = ["run", 'two words; "$3"']

    diagnostics = {}
    ir = shell_backend.ShellBackend().parse(script, args, diagnostics=diagnostics)
    result = RecursiveTracker().track(str(parent), args)

    assert diagnostics["parser"] == "shell_litese"
    assert "'$1'" in ir.reachable_content
    assert r'"\$1"' in ir.reachable_content
    assert [(call.path, call.args) for call in ir.calls] == [
        ("child.sh", ["$1", "$1", "$1", "run", "${2}", args[1]])
    ]
    assert [node.script_path for node in result.nodes] == [str(parent), str(child)]
    assert "echo literal_branch" in result.nodes[1].preprocessed_content
    assert "echo expanded_branch" not in result.nodes[1].preprocessed_content


def test_glob_if_keeps_dangerous_command():
    script = '''if [[ "$1" == bak* ]]; then
  rm -rf /opt/data
else
  echo safe
fi
'''
    args = ["bak20260101"]
    assert "rm -rf /opt/data" in (symbolic.symbolic_process(script, args) or "")
    assert "rm -rf /opt/data" in (shell_backend.parse_if(script, args) or "")


def test_regex_if_keeps_dangerous_command():
    script = '''if [[ "$1" =~ ^ba.* ]]; then
  rm -rf /opt/data
else
  echo safe
fi
'''
    args = ["baz"]
    assert "rm -rf /opt/data" in (symbolic.symbolic_process(script, args) or "")
    assert "rm -rf /opt/data" in (shell_backend.parse_if(script, args) or "")


def test_nested_case_keeps_outer_command():
    script = '''MODE=x
case "$MODE" in
  x)
    rm -rf /opt/data
    case "$2" in
      x) echo inner ;;
    esac
    ;;
esac
'''
    out = symbolic.symbolic_process(script, ["run", "x"]) or ""
    assert "rm -rf /opt/data" in out


def test_startup_args_respect_var_budget():
    # 199 个启动参数 + $@/$* = 201 > 200 → 放弃精确分析、回退全文
    args = [f"a{i}" for i in range(199)]
    assert symbolic.symbolic_process("MODE=x\n", args) is None


def test_modify_existing_var_at_budget_continues():
    # 200 个变量填满预算后，修改已有变量不增加表项，不应触发回退
    script = "\n".join([f"V{i}=x" for i in range(200)] + ["V0=y"])
    assert symbolic.symbolic_process(script, None) is not None


def test_branch_selection_reaches_l1_scanner(tmp_path):
    """接口级回归：危险分支必须真正进入 CommandPatternScanner，而不只是内部函数输出。"""
    from agent_moss.engine.heuristic import CommandPatternScanner
    from agent_moss.engine.script_content_analyzer import analyze_script_content
    from agent_moss.profiles.linux import LinuxProfile

    script = '''if [[ "$1" == bak* ]]; then
  rm -rf /opt/data
else
  echo safe
fi
'''
    (tmp_path / "x.sh").write_text(script, encoding="utf-8")
    scanner = CommandPatternScanner(LinuxProfile())
    result = analyze_script_content(
        "bash", "bash x.sh bak20260101", command_scanner=scanner, cwd=str(tmp_path)
    )
    assert result.has_high or result.has_critical


def test_nested_case_inner_dangerous_command():
    """内层 case 的危险分支不能被外层分支切分吞掉（openEuler 回归）。"""
    script = '''case $1 in
  clean)
    case $2 in
      hard) rm -rf ~/.ssh/id_rsa ;;
      soft) echo safe ;;
    esac
    ;;
  backup) echo safe ;;
esac
'''
    out = symbolic.symbolic_process(script, ["clean", "hard"]) or ""
    assert "rm -rf ~/.ssh/id_rsa" in out


def test_nested_if_keeps_outer_command():
    """报告示例的嵌套 if 形态：外层危险命令不能被内层 if 覆盖。"""
    script = '''if [ "$1" = x ]; then
  rm -rf /opt/data
  if [ "$2" = x ]; then
    echo inner
  fi
fi
'''
    out = symbolic.symbolic_process(script, ["x", "x"]) or ""
    assert "rm -rf /opt/data" in out
