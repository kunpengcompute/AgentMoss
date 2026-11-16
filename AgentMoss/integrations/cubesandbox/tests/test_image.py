"""Exercise the in-image guard and the pinned AIO patch without running commands."""

import ast
import asyncio
import importlib.util
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

IMAGE = Path(__file__).resolve().parents[1] / "image"


def load_module(name, path):
    assert path.is_file(), f"Missing integration implementation: {path.name}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def judgment(allowed=True):
    return SimpleNamespace(allowed=allowed, reason="test rule", source="L1", violated_layers=[])


def load_guard(monkeypatch):
    # Replace only the rule engine boundary; locks, checks, history and submission are real.
    from agent_moss.engine import coordinator

    monkeypatch.setattr(coordinator, "judge_security", lambda **kwargs: judgment())
    monkeypatch.setenv("AGENT_MOSS_DISABLE_LLM", "1")
    monkeypatch.setenv("AGENT_MOSS_RULES_MODE", "builtin")
    return load_module("cubesandbox_moss_guard", IMAGE / "moss_guard.py")


def session():
    return SimpleNamespace(id="test-session", working_dir="/home/gem", has_active_command=False)


def test_allowed_command_is_checked_with_history_before_submission(monkeypatch):
    guard = load_guard(monkeypatch)
    current = session()
    current._moss_history = [{"action_type": "bash", "action_detail": "printf previous"}]
    checked = []

    def judge(**kwargs):
        checked.append(kwargs)
        return judgment()

    monkeypatch.setattr(guard, "judge_security", judge)

    async def submit(command, *, marker):
        assert checked[0]["cwd"] == "/home/gem/work"
        assert checked[0]["a_next"] == {"action_type": "bash", "action_detail": command}
        assert checked[0]["action_history"] == [{"action_type": "bash", "action_detail": "printf previous"}]
        return marker

    result = asyncio.run(
        guard.execute_checked(current, "printf next", "/home/gem/work", submit, "printf next", marker="submitted")
    )
    assert result == "submitted"
    assert current._moss_history[-1] == {"action_type": "bash", "action_detail": "printf next"}


@pytest.mark.parametrize(
    "decision,status,code",
    [(False, 403, "AGENTMOSS_DENIED"), (None, 403, "AGENTMOSS_DENIED"), ("error", 503, "AGENTMOSS_UNAVAILABLE")],
)
def test_denial_or_engine_failure_never_submits(monkeypatch, decision, status, code):
    guard = load_guard(monkeypatch)
    current = session()

    def judge(**kwargs):
        if decision == "error":
            raise RuntimeError("engine failed")
        return judgment(decision)

    monkeypatch.setattr(guard, "judge_security", judge)

    async def submit():
        pytest.fail("Rejected command reached the shell manager")

    with pytest.raises(HTTPException) as error:
        asyncio.run(guard.execute_checked(current, "blocked", None, submit))
    assert error.value.status_code == status
    assert error.value.detail["code"] == code
    assert not getattr(current, "_moss_history", [])


@pytest.mark.parametrize("attribute,value", [("has_active_command", True), ("active_command_count", 1)])
def test_busy_session_never_checks_or_submits(monkeypatch, attribute, value):
    guard = load_guard(monkeypatch)
    current = session()
    setattr(current, attribute, value)
    monkeypatch.setattr(guard, "judge_security", lambda **kwargs: pytest.fail("Busy session reached the engine"))

    async def submit():
        pytest.fail("Busy session reached the shell manager")

    with pytest.raises(HTTPException) as error:
        asyncio.run(guard.execute_checked(current, "printf test", None, submit))
    assert error.value.status_code == 409


def test_concurrent_request_cannot_check_during_pending_submission(monkeypatch):
    guard = load_guard(monkeypatch)
    current = session()
    checks = []

    def judge(**kwargs):
        checks.append(kwargs["a_next"]["action_detail"])
        return judgment()

    monkeypatch.setattr(guard, "judge_security", judge)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def first_submit():
            entered.set()
            await release.wait()
            current.has_active_command = True
            return "queued"

        async def second_submit():
            pytest.fail("Second request entered an active session")

        first = asyncio.create_task(guard.execute_checked(current, "first", None, first_submit))
        await entered.wait()
        second = asyncio.create_task(guard.execute_checked(current, "second", None, second_submit))
        await asyncio.sleep(0.02)
        assert checks == ["first"]
        release.set()
        assert await first == "queued"
        with pytest.raises(HTTPException) as error:
            await second
        assert error.value.status_code == 409

    asyncio.run(scenario())


@pytest.mark.parametrize("optimize", [False, True])
def test_unknown_shell_version_is_rejected_without_changing_file(tmp_path, optimize):
    patch = IMAGE / "patch_shell.py"
    assert patch.is_file(), "Missing version-checked AIO patch"
    target = tmp_path / "shell.py"
    target.write_bytes(b"# unreviewed AIO version\n")
    result = subprocess.run(
        [sys.executable, *(["-O"] if optimize else []), str(patch), str(target)], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "SHA-256" in result.stderr
    assert target.read_bytes() == b"# unreviewed AIO version\n"


def patched_routes(tmp_path, monkeypatch):
    original = os.environ.get("CUBESANDBOX_AIO_SHELL_SOURCE")
    if not original:
        pytest.skip("Set CUBESANDBOX_AIO_SHELL_SOURCE to the inspected AIO 1.11.0 shell.py")
    target = tmp_path / "shell.py"
    target.write_bytes(Path(original).read_bytes())
    patch = IMAGE / "patch_shell.py"
    assert patch.is_file(), "Missing version-checked AIO patch"
    result = subprocess.run([sys.executable, "-O", str(patch), str(target)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    guard = load_guard(monkeypatch)
    tree = ast.parse(target.read_text())
    routes = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name in {"exec_command", "write_to_process"}
    ]
    for route in routes:
        route.decorator_list = []
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *routes],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {
        "asyncio": asyncio,
        "HTTPException": HTTPException,
        "check_command": guard.check_command,
        "execute_checked": guard.execute_checked,
        "validate_cwd": lambda path: path,
        "sanitize_for_logging": lambda value, **kwargs: value,
        "logger": logging.getLogger(__name__),
    }
    exec(compile(module, str(target), "exec"), namespace)
    return guard, namespace


@pytest.mark.parametrize(
    "route,async_mode,sse",
    [
        ("exec_command", False, False),
        ("exec_command", True, False),
        ("exec_command", False, True),
        ("exec_command", True, True),
        ("write_to_process", True, False),
    ],
)
def test_patched_routes_reject_before_calling_manager(tmp_path, monkeypatch, route, async_mode, sse):
    guard, namespace = patched_routes(tmp_path, monkeypatch)
    monkeypatch.setattr(guard, "judge_security", lambda **kwargs: judgment(False))
    current = session()
    current.active = True

    async def submit(*args, **kwargs):
        pytest.fail("A rejected route reached the shell manager")

    manager = SimpleNamespace(get_session=lambda ident: current, execute_command=submit)
    namespace["services"] = SimpleNamespace(require=lambda name: manager)
    request = SimpleNamespace(
        id=current.id,
        command="blocked",
        input="blocked",
        exec_dir="/home/gem",
        strict=True,
        async_mode=async_mode,
        timeout=None,
        hard_timeout=30,
        no_change_timeout=None,
        truncate=True,
        model_dump=lambda **kwargs: {},
    )
    args = [request]
    if route == "exec_command":
        args.append(SimpleNamespace(headers={"accept": "text/event-stream" if sse else ""}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(namespace[route](*args))
    assert error.value.status_code == 403


def test_sse_generator_rechecks_after_headers_before_submission(tmp_path, monkeypatch):
    guard, namespace = patched_routes(tmp_path, monkeypatch)
    decisions = iter([True, False])
    monkeypatch.setattr(guard, "judge_security", lambda **kwargs: judgment(next(decisions)))
    current = session()

    async def submit(*args, **kwargs):
        pytest.fail("Deferred SSE execution skipped its second check")

    async def subscribe(*args):
        return asyncio.Queue()

    async def unsubscribe(*args):
        return None

    manager = SimpleNamespace(
        get_session=lambda ident: current,
        execute_command=submit,
        subscribe_char=subscribe,
        unsubscribe_char=unsubscribe,
    )
    namespace.update(
        {
            "services": SimpleNamespace(require=lambda name: manager),
            "StreamingResponse": lambda content, **kwargs: content,
            "SSE_HEADERS": {},
            "_sse_comment": lambda message: message,
            "_sse_data": lambda data: data,
        }
    )
    request = SimpleNamespace(
        id=current.id,
        command="blocked",
        exec_dir="/home/gem",
        strict=True,
        async_mode=True,
        truncate=True,
        model_dump=lambda **kwargs: {},
    )

    async def scenario():
        stream = await namespace["exec_command"](request, SimpleNamespace(headers={"accept": "text/event-stream"}))
        return [item async for item in stream]

    output = asyncio.run(scenario())
    assert output[0] == "connected"
    assert output[1]["status_code"] == 403
    assert output[1]["error"]["code"] == "AGENTMOSS_DENIED"
    assert not getattr(current, "_moss_history", [])
