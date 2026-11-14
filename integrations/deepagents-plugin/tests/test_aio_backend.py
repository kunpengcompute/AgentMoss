"""AIO contract, Gate integration and lifecycle regression tests; no external models."""

import asyncio
import base64
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from agent_sandbox.core.api_error import ApiError
from langchain_core.messages import AIMessage
from models import call
from test_native_sdk import make_agent, run_args, setup

from agentmoss_deepagents import DeepAgentsIntegration
from agentmoss_deepagents.actions import ActionAdapter
from agentmoss_deepagents.aio_backend import AIOSandboxBackend
from agentmoss_deepagents.aio_errors import AIOExecutionUncertain, AIOStartupError, AIOUnavailable
from agentmoss_deepagents.backend_config import AIOBackendConfig, IntegrationConfig
from agentmoss_deepagents.history import KEY


def response(**fields):
    return SimpleNamespace(success=True, data=SimpleNamespace(**fields))


class FakeShell:
    def __init__(self):
        self.calls = []
        self.cleaned = []
        self.answer = response(status="completed", exit_code=0, output="sandbox-output")

    def create_session(self, **kwargs):
        self.created = kwargs
        return response(session_id=kwargs["id"])

    def exec_command(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer() if callable(self.answer) else self.answer

    def cleanup_session(self, **kwargs):
        self.cleaned.append(kwargs)
        return SimpleNamespace(success=True)


class FakeFiles:
    def __init__(self):
        self.paths = []
        self.files = {"/foo.txt": "remote-root-file", "/home/gem/foo.txt": "different-file"}

    def read_file(self, **kwargs):
        self.paths.append(kwargs["file"])
        return response(content=self.files[kwargs["file"]])


@pytest.fixture
def client():
    return SimpleNamespace(shell=FakeShell(), file=FakeFiles())


@pytest.fixture
def backend(client):
    value = AIOSandboxBackend(client, config=AIOBackendConfig("http://proxy.test:8080", "sandbox-1"))
    yield value
    value.close()


@pytest.mark.parametrize(
    "key,value",
    [
        ("proxy_url", "http://user:secret@proxy.test"),
        ("sandbox_id", "bad\r\nhost"),
        ("working_dir", "relative"),
        ("working_dir", "C:\\workspace"),
        ("hard_timeout", 0),
        ("default_timeout", float("inf")),
        ("traffic_token", "a\nb"),
        ("route_host", "bad host"),
    ],
)
def test_invalid_aio_configuration(key, value):
    raw = {"proxy_url": "http://proxy.test", "sandbox_id": "sandbox-1", key: value}
    with pytest.raises(ValueError):
        AIOBackendConfig(**raw)


def test_explicit_config_keeps_gate_independent(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_GATE_API_TOKEN", "environment-secret")
    config = IntegrationConfig.from_mapping(
        {
            "backend": "aio",
            "gate": {"api_token": "explicit-secret"},
            "aio": {"proxy_url": "http://proxy.test", "sandbox_id": "sandbox-1", "traffic_token": "traffic-secret"},
        }
    )
    assert config.gate.read_token() == "explicit-secret"
    assert config.aio.read_token() == "traffic-secret"
    assert "explicit-secret" not in repr(config)
    assert "traffic-secret" not in repr(config)
    with pytest.raises(ValueError):
        IntegrationConfig.from_mapping({"backend": "aio", "local": {}, "aio": {}})
    empty = tmp_path / "empty"
    empty.write_text("")
    with pytest.raises(ValueError):
        AIOBackendConfig("http://proxy.test", "sandbox-1", traffic_token_file=empty).read_token()


@pytest.mark.parametrize(
    "tool,key",
    [
        ("read_file", "file_path"),
        ("write_file", "file_path"),
        ("edit_file", "file_path"),
        ("delete", "file_path"),
        ("ls", "path"),
        ("glob", "path"),
        ("grep", "path"),
    ],
)
@pytest.mark.parametrize("path", ["foo.txt", "/home/gem/foo.txt", "/"])
def test_adapter_matches_upstream_path_normalization(backend, tool, key, path):
    from deepagents.backends.utils import validate_path

    args = {key: path, "pattern": "*.txt"}
    original = deepcopy(args)
    action = ActionAdapter(backend).adapt(tool, args)
    assert action.metadata["real_path"] == backend.normalize_path(validate_path(path))
    assert action.metadata["os_type"] == "linux"
    assert args == original


def test_missing_search_path_uses_working_dir(backend):
    assert ActionAdapter(backend).adapt("glob", {"pattern": "*.txt"}).metadata["real_path"] == "/home/gem"
    assert backend.normalize_path("foo.txt") == "/home/gem/foo.txt"


@pytest.mark.parametrize("code", [0, 7, 126])
def test_execute_keeps_real_exit_and_timeout(backend, client, code):
    client.shell.answer = response(status="completed", exit_code=code, output="exact")
    result = backend.execute("printf exact", timeout=5)
    assert result.exit_code == code and result.output == "exact"
    sent = client.shell.calls[0]
    assert sent["command"] == "printf exact"
    assert sent["exec_dir"] == "/home/gem" and sent["strict"] is True
    assert sent["async_mode"] is False
    assert sent["request_options"]["timeout_in_seconds"] > sent["hard_timeout"]


@pytest.mark.parametrize("status", ["running", "hard_timeout", "no_change_timeout"])
def test_incomplete_execution_blocks_following_calls(backend, client, status):
    client.shell.answer = response(status=status, exit_code=None, output="partial")
    with pytest.raises(AIOExecutionUncertain):
        backend.execute("long-command")
    with pytest.raises(AIOUnavailable):
        backend.execute("second-command")
    assert len(client.shell.calls) == 1
    assert backend.status()["state"] == "uncertain"


@pytest.mark.parametrize("code", [403, 409, 503])
def test_aio_rejection_is_not_success(backend, client, code):
    backend._secrets = ("gate-secret", "traffic-secret")
    client.shell.answer = ApiError(status_code=code, body={"reason": "gate-secret traffic-secret"})
    result = backend.execute("command")
    assert result.exit_code == 126
    assert "gate-secret" not in result.output and "traffic-secret" not in result.output
    if code == 409:
        with pytest.raises(AIOUnavailable):
            backend.execute("retry")
        assert len(client.shell.calls) == 1


def test_network_loss_is_unknown_without_retry(backend, client):
    client.shell.answer = httpx.ReadTimeout("secret must not leak")
    with pytest.raises(AIOExecutionUncertain, match="unknown") as error:
        backend.execute("command")
    assert "secret" not in str(error.value)
    assert len(client.shell.calls) == 1


def test_startup_error_preserves_recovery_ids(client):
    def fail(**kwargs):
        raise httpx.ReadTimeout("private-token")

    client.shell.create_session = fail
    client.shell.cleanup_session = fail
    with pytest.raises(AIOStartupError) as error:
        AIOSandboxBackend(client, config=AIOBackendConfig("http://proxy.test", "sandbox-1"))
    assert error.value.sandbox_id == "sandbox-1"
    assert error.value.session_id.startswith("moss-")
    assert error.value.cleanup_confirmed is False
    assert "private-token" not in str(error.value)


def test_close_stops_admission_before_waiting(backend, client):
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        assert release.wait(5)
        return response(status="completed", exit_code=0, output="done")

    client.shell.answer = slow
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(backend.execute, "first")
        assert started.wait(5)
        close = pool.submit(backend.close)
        assert backend._closing.wait(5)
        second = pool.submit(backend.execute, "second")
        release.set()
        assert first.result(timeout=5).exit_code == 0
        close.result(timeout=5)
        with pytest.raises(AIOUnavailable):
            second.result(timeout=5)
    assert len(client.shell.calls) == 1
    backend.close()
    assert len(client.shell.cleaned) == 1


@pytest.mark.parametrize(
    "gate_allow,aio_status,expected",
    [
        (False, "completed", "denied"),
        (True, "completed", "executed_success"),
        (True, "reject", "executed_error"),
        (True, "running", "unknown"),
    ],
)
async def test_real_deepagents_graph_retains_gate_and_history(
    tmp_path, backend, client, gate_allow, aio_status, expected
):
    runtime, seen = setup(tmp_path, httpx.Response(200, json={"decision": "Allow" if gate_allow else "Deny"}))
    if aio_status == "reject":
        client.shell.answer = ApiError(status_code=403, body={"reason": "inner Moss denied"})
    elif aio_status == "running":
        client.shell.answer = response(status="running", exit_code=None, output="")
    agent = make_agent(
        runtime,
        tmp_path,
        [call("execute", {"command": "printf safe"}, "tool"), AIMessage(content="done")],
        backend=backend,
    )
    inputs, config, context = run_args()
    try:
        result = await agent.ainvoke(inputs, config, context=context)
        assert len(seen) == 1
        assert seen[0]["metadata"]["os_type"] == "linux"
        assert seen[0]["metadata"]["sandbox_id"] == "sandbox-1"
        event = result[KEY]["events"][0]
        assert event["status"] == expected
        assert event["decision"] == ("Allow" if gate_allow else "Deny")
        assert len(client.shell.calls) == int(gate_allow)
    finally:
        await runtime.aclose()


async def test_real_file_tool_inspects_and_reads_same_remote_path(tmp_path, backend, client):
    runtime, seen = setup(tmp_path)
    agent = make_agent(
        runtime,
        tmp_path,
        [call("read_file", {"file_path": "foo.txt"}, "file"), AIMessage(content="done")],
        backend=backend,
    )
    inputs, config, context = run_args()
    try:
        result = await agent.ainvoke(inputs, config, context=context)
        assert client.file.paths == ["/foo.txt"]
        assert seen[0]["metadata"]["real_path"] == "/foo.txt"
        assert result[KEY]["events"][0]["status"] == "executed_success"
    finally:
        await runtime.aclose()


async def test_factory_routes_and_closes_owned_resources(monkeypatch, tmp_path, client):
    import agent_sandbox

    seen = {}

    def sandbox(**kwargs):
        seen.update(kwargs)
        return client

    monkeypatch.setattr(agent_sandbox, "Sandbox", sandbox)
    integration = DeepAgentsIntegration.load(
        {
            "backend": "aio",
            "gate": {"api_token": "gate-secret"},
            "aio": {
                "proxy_url": "http://proxy.test:8080",
                "sandbox_id": "sandbox-1",
                "traffic_token": "traffic-secret",
            },
        }
    )
    assert seen["headers"]["Host"] == "8080-sandbox-1.cube.app"
    assert seen["headers"]["e2b-traffic-access-token"] == "traffic-secret"
    assert "secret" not in json.dumps(integration.status())
    await integration.aclose()
    await integration.aclose()
    assert integration._http.is_closed
    assert len(client.shell.cleaned) == 1
    assert integration.status()["closed"] is True


def test_grep_literal_pattern_with_filter_and_total_limit(backend, client):
    seen = []

    def grep_files(**kwargs):
        seen.append(kwargs)
        return response(
            matches=[
                SimpleNamespace(file="/home/gem/file.txt", line_number=2, line_content="literal.*"),
                SimpleNamespace(file="/home/gem/other.txt", line_number=3, line_content="literal.*"),
            ],
            truncated=False,
        )

    client.file.grep_files = grep_files
    result = backend.grep("literal.*", "/home/gem", "*.txt", max_count=1)
    assert result.error is None
    assert result.matches == [{"path": "/home/gem/file.txt", "line": 2, "text": "literal.*"}]
    assert result.truncated is True
    assert seen[0]["fixed_strings"] is True
    assert seen[0]["include"] == ["*.txt"]
    assert client.shell.calls == []


@pytest.mark.parametrize("offset,limit,expected", [(1, 1, "second\n"), (-1, 1, "first\n"), (9, 2, ""), (0, 0, "")])
def test_read_keeps_line_bounds(backend, client, offset, limit, expected):
    def read_file(**kwargs):
        lines = ["first\n", "second\n", "third\n"]
        return response(content="".join(lines[kwargs["start_line"] : kwargs["end_line"]]))

    client.file.read_file = read_file
    result = backend.read("foo.txt", offset, limit)
    assert result.error is None and result.file_data["content"] == expected
    assert bool(result.no_lines_requested) == (limit == 0)


def test_listing_preserves_remote_metadata(backend, client):
    client.file.list_path = lambda **kwargs: response(
        files=[SimpleNamespace(path="目录/a b.txt", is_directory=False, size=7, modified_time="2026-01-01T00:00:00Z")]
    )
    assert backend.ls(".").entries == [
        {"path": "/home/gem/目录/a b.txt", "is_dir": False, "size": 7, "modified_at": "2026-01-01T00:00:00Z"}
    ]


def test_execute_preserves_transport_truncation(backend, client):
    client.shell.answer = response(status="completed", exit_code=7, output="partial", full_output_file_path="/tmp/out")
    result = backend.execute("command")
    assert (result.output, result.exit_code, result.truncated) == ("partial", 7, True)


@pytest.fixture
def disk_backend(tmp_path, client):
    """Exercise file effects and shell deletion on disk; not a live AIO server."""

    def execute(**kwargs):
        client.shell.calls.append(kwargs)
        result = subprocess.run(
            kwargs["command"], shell=True, cwd=kwargs["exec_dir"], capture_output=True, text=True, timeout=10
        )
        # Exercise JSON output with the line endings of a POSIX terminal.
        return response(
            status="completed",
            exit_code=result.returncode,
            output=(result.stdout + result.stderr).replace("\n", "\r\n"),
        )

    def write_file(**kwargs):
        target = Path(kwargs["file"])
        target.parent.mkdir(parents=True, exist_ok=True)
        content = kwargs["content"]
        target.write_bytes(base64.b64decode(content) if kwargs.get("encoding") == "base64" else content.encode())
        return response()

    def replace(**kwargs):
        target = Path(kwargs["path"])
        old = target.read_text()
        count = old.count(kwargs["old_str"])
        if not count or (count > 1 and kwargs.get("replace_mode") != "ALL"):
            raise ApiError(status_code=400, body="replacement mismatch")
        target.write_text(old.replace(kwargs["old_str"], kwargs["new_str"]))
        return response(old_content=old)

    client.shell.exec_command = execute
    client.file.write_file = write_file
    client.file.download_file = lambda **kwargs: iter([Path(kwargs["path"]).read_bytes()])
    client.file.find_files = lambda **kwargs: response(
        files=[str(p) for p in Path(kwargs["path"]).glob(kwargs["glob"])]
    )
    client.file.str_replace_editor = replace
    value = AIOSandboxBackend(
        client, config=AIOBackendConfig("http://proxy.test", "sandbox-1", working_dir=str(tmp_path))
    )
    try:
        yield value
    finally:
        value.close()


def test_create_only_write_and_binary_transfer(disk_backend, tmp_path):
    target = tmp_path / "目录 ' [x].txt"
    assert disk_backend.write(target.name, "first").error is None
    assert disk_backend.write(target.name, "overwrite").error is not None
    assert target.read_text() == "first"
    data = b"\x00\xff\x01binary\r\n"
    result = disk_backend.upload_files([("binary.bin", data), ("bad\x00path", b"no")])
    assert result[0].error is None and result[1].error == "invalid_path"
    assert disk_backend.download_files(["binary.bin"])[0].content == data


@pytest.mark.parametrize("size", [1, 60000])
def test_edit_preserves_unique_and_replace_all_contract(disk_backend, tmp_path, size):
    target = tmp_path / "目录 ' [x].txt"
    old = "a" * size
    target.write_text(old + "\n" + old)
    rejected = disk_backend.edit(target.name, old, "new")
    assert rejected.error and target.read_text() == old + "\n" + old
    accepted = disk_backend.edit(target.name, old, "new", replace_all=True)
    assert accepted.error is None and accepted.occurrences == 2
    assert accepted.path == str(target) and target.read_text() == "new\nnew"
    assert disk_backend.edit(target.name, "absent", "new").error


@pytest.mark.parametrize("pattern", ["*.txt", "*.{txt,md}"])
def test_glob_and_delete_operate_on_remote_working_directory(disk_backend, tmp_path, pattern):
    directory = tmp_path / "目录 '"
    directory.mkdir()
    target = directory / "a b.txt"
    target.write_text("keep")
    nested = directory / "child" / "nested.txt"
    nested.parent.mkdir()
    nested.write_text("nested")
    result = disk_backend.glob(pattern, directory.name)
    assert result.error is None and {m["path"] for m in result.matches} == {str(target), str(nested)}
    assert disk_backend.glob("*.absent", directory.name).matches == []
    assert disk_backend.status()["state"] == "ready"
    assert disk_backend.glob("*.txt").error is None
    assert disk_backend.delete(directory.name).error is None and not directory.exists()
    assert disk_backend.delete(directory.name).error


def test_empty_edit_never_modifies_file(disk_backend, tmp_path):
    target = tmp_path / "empty.txt"
    target.write_text("")
    result = disk_backend.edit(target.name, "", "inserted")
    assert result.error and target.read_text() == ""


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("traffic-secret"), ApiError(status_code=500, body="private")])
def test_lost_upload_response_blocks_remaining_submissions(disk_backend, tmp_path, failure):
    original = disk_backend.client.file.write_file

    def lost(**kwargs):
        original(**kwargs)
        raise failure

    disk_backend.client.file.write_file = lost
    result = disk_backend.upload_files([("first", b"written"), ("second", b"must not run")])
    assert all(item.error for item in result)
    assert (tmp_path / "first").read_bytes() == b"written"
    assert not (tmp_path / "second").exists()
    assert disk_backend.status()["state"] == "uncertain"
    with pytest.raises(AIOUnavailable):
        disk_backend.execute("printf must-not-submit")


@pytest.mark.parametrize("graph_call", [False, True])
async def test_cancelled_write_worker_finishes_before_close(backend, client, tmp_path, graph_call):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def write_file(**kwargs):
        started.set()
        assert release.wait(5)
        finished.set()
        return response()

    client.file.write_file = write_file
    runtime, _ = setup(tmp_path)
    client.file.find_files = lambda **kwargs: response(files=[])
    agent = make_agent(
        runtime,
        tmp_path,
        [call("write_file", {"file_path": "/a", "content": "x"}, "write"), AIMessage(content="done")],
        backend=backend,
    )
    inputs, config, context = run_args()
    operation = asyncio.create_task(
        agent.ainvoke(inputs, config, context=context) if graph_call else backend.aupload_files([("a", b"x")])
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        close = asyncio.create_task(asyncio.to_thread(backend.close))
        assert await asyncio.to_thread(backend._closing.wait, 5)
        assert not close.done() and not finished.is_set()
        if graph_call:
            assert not operation.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    await close
    await runtime.aclose()
    assert finished.is_set() and backend.status()["state"] == "closed"


@pytest.mark.parametrize(
    "method,args", [("edit", ("", "x", "y")), ("glob", ("*", "~bad")), ("delete", ("bad\x00path",))]
)
def test_invalid_file_arguments_do_not_poison_session(backend, client, method, args):
    with pytest.raises(ValueError):
        getattr(backend, method)(*args)
    assert backend.status()["state"] == "ready" and client.shell.calls == []


@pytest.mark.parametrize("method", ["read", "ls", "write", "edit", "grep", "upload_files"])
@pytest.mark.parametrize(
    "kind,status,want", [("not_found", 404, "file_not_found"), ("permission_denied", 403, "permission_denied")]
)
@pytest.mark.parametrize("http_error", [False, True])
def test_sdk_file_rejection_is_recoverable(sdk_backend, method, kind, status, want, http_error):
    backend, submitted, replies = sdk_backend
    replies.append(
        httpx.Response(
            status if http_error else 200,
            json={
                "success": False,
                "message": "private-token",
                "data": {"path": "/missing", "operation": method, "error_type": kind, "message": "private-token"},
            },
        )
    )
    args = {
        "read": ("missing",),
        "ls": ("missing",),
        "write": ("missing", "x"),
        "edit": ("missing", "old", "new"),
        "grep": ("text", "missing"),
        "upload_files": ([("missing", b"x")],),
    }
    result = getattr(backend, method)(*args[method])
    assert (result[0] if isinstance(result, list) else result).error == want
    assert backend.status()["state"] == "ready" and len(submitted) == 1


@pytest.mark.parametrize("output,code,truncated", [("crashed", 1, False), ("not-json", 0, False), ("", 0, True)])
def test_glob_does_not_hide_errors_or_truncation(backend, client, output, code, truncated):
    client.shell.answer = response(
        status="completed", output=output, exit_code=code, full_output_file_path="/tmp/out" if truncated else None
    )
    result = backend.glob("*")
    assert result.truncated if truncated else result.error


@pytest.mark.parametrize(
    "method,fields",
    [
        ("ls", {"total_count": 1}),
        ("ls", {"files": [{"error_type": "not_found"}]}),
        ("read", {"error_type": ["not_found"]}),
        ("read", {"error_type": "private-token"}),
    ],
)
def test_sdk_malformed_file_result_is_not_a_confirmed_rejection(sdk_backend, method, fields):
    backend, _, replies = sdk_backend
    replies.append(
        httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "path": "/home/gem",
                    "pattern": "*.txt",
                    **fields,
                },
            },
        )
    )
    with pytest.raises(AIOExecutionUncertain) as caught:
        getattr(backend, method)("/home/gem")
    assert "private-token" not in str(caught.value)
    assert backend.status()["state"] == "uncertain"


@pytest.mark.parametrize("cleanup", [True, False, 404, 500])
def test_failed_startup_reports_cleanup_outcome_and_session(client, cleanup):
    def create_session(**kwargs):
        raise httpx.ReadTimeout("private-token")

    def cleanup_session(**kwargs):
        client.shell.cleaned.append(kwargs)
        if type(cleanup) is int:
            raise ApiError(status_code=cleanup, body="private-token")
        return SimpleNamespace(success=cleanup)

    client.shell.create_session, client.shell.cleanup_session = create_session, cleanup_session
    with pytest.raises(AIOStartupError) as caught:
        AIOSandboxBackend(client, config=AIOBackendConfig("http://proxy.test", "sandbox-1"))
    assert caught.value.cleanup_confirmed == (cleanup is True or cleanup == 404)
    assert client.shell.cleaned[0]["session_id"] == caught.value.session_id
    assert "private-token" not in str(caught.value)


@pytest.fixture
def sdk_backend():
    from agent_sandbox import Sandbox

    submitted, replies = [], []

    def transport(request):
        if request.url.path.endswith("/create"):
            body = json.loads(request.content)
            return httpx.Response(
                200, json={"success": True, "data": {"session_id": body["id"], "working_dir": body["exec_dir"]}}
            )
        if request.method == "DELETE":
            return httpx.Response(200, json={"success": True})
        submitted.append(request)
        return replies.pop(0)

    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        backend = AIOSandboxBackend(
            Sandbox(base_url="http://proxy.test", httpx_client=http),
            config=AIOBackendConfig("http://proxy.test", "sandbox-1", traffic_token="traffic-secret"),
        )
        try:
            yield backend, submitted, replies
        finally:
            backend.close()


@pytest.mark.parametrize("operation", ["execute", "upload_files", "glob"])
def test_official_sdk_never_retries_uncertain_mutations(sdk_backend, operation):
    backend, submitted, replies = sdk_backend
    replies.append(httpx.Response(500, json={"error": "traffic-secret"}))
    if operation != "upload_files":
        with pytest.raises(AIOExecutionUncertain):
            getattr(backend, operation)("mutate-once")
    else:
        assert backend.upload_files([("a", b"x")])[0].error
    assert len(submitted) == 1 and backend.status()["state"] == "uncertain"
    with pytest.raises(AIOUnavailable):
        backend.execute("must-not-submit")
    assert len(submitted) == 1
    assert "traffic-secret" not in json.dumps(backend.status())


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("tool", ["execute", "edit_file", "delete"])
async def test_gate_deny_protects_aio_files_in_parent_and_child(tmp_path, disk_backend, child, tool):
    from models import ScriptedModel

    target = tmp_path / "protected.txt"
    target.write_text("original")
    args = {
        "execute": {"command": f"printf changed > {target}"},
        "edit_file": {"file_path": str(target), "old_string": "original", "new_string": "changed"},
        "delete": {"file_path": str(target)},
    }

    def verdict(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json={"decision": "Allow" if payload["metadata"].get("orchestration") else "Deny"})

    runtime, seen = setup(tmp_path, verdict)
    calls = [call(tool, args[tool], "protected"), AIMessage(content="done")]
    options = {}
    if child:
        options["subagents"] = [{"name": "worker", "description": "worker", "model": ScriptedModel(responses=calls)}]
        calls = [
            call("task", {"description": "work", "subagent_type": "worker"}, "delegate"),
            AIMessage(content="done"),
        ]
    agent = make_agent(runtime, tmp_path, calls, backend=disk_backend, **options)
    inputs, config, context = run_args()
    try:
        result = await agent.ainvoke(inputs, config, context=context)
        event = next(e for e in result[KEY]["events"] if e["tool_call_id"] == "protected")
        assert event["decision"] == "Deny" and event["status"] == "denied"
        assert seen[-1]["metadata"]["sandbox_id"] == "sandbox-1"
        assert target.read_text() == "original" and disk_backend.client.shell.calls == []
    finally:
        await runtime.aclose()


def test_edit_lost_result_stops_future_submissions(disk_backend, tmp_path):
    target = tmp_path / "edit.txt"
    target.write_text("before")
    editor = disk_backend.client.file.str_replace_editor

    def lost_editor(**kwargs):
        editor(**kwargs)
        raise httpx.ReadTimeout("private-token")

    disk_backend.client.file.str_replace_editor = lost_editor
    with pytest.raises(AIOExecutionUncertain):
        disk_backend.edit(target.name, "before", "after")
    assert target.read_text() == "after" and disk_backend.status()["state"] == "uncertain"
    with pytest.raises(AIOUnavailable):
        disk_backend.execute("must-not-submit")


def test_large_edit_does_not_leave_sensitive_partial_uploads(disk_backend, tmp_path):
    target = tmp_path / "large.txt"
    old = "a" * 60000
    target.write_text(old)
    partial_uploads = {}

    def upload(**kwargs):
        if partial_uploads:
            raise ApiError(status_code=403, body="denied")
        partial_uploads[kwargs["file"]] = kwargs["content"]
        return response()

    disk_backend.client.file.write_file = upload
    result = disk_backend.edit(target.name, old, "after")
    assert not partial_uploads
    assert result.error is None and target.read_text() == "after"


@pytest.mark.parametrize("code", [403, 503])
def test_delete_preserves_aio_rejection_reason(backend, client, code):
    client.shell.answer = ApiError(status_code=code, body="service rejected deletion")
    result = backend.delete("file.txt")
    assert result.error and f"AIO HTTP {code}" in result.error


def test_editor_error_field_is_not_success(backend, client):
    from agent_sandbox.types import StrReplaceEditorResult

    client.file.str_replace_editor = lambda **kwargs: SimpleNamespace(
        success=True,
        data=StrReplaceEditorResult(
            output="", error="cannot replace gate-secret", path="/home/gem/file", prev_exist=True
        ),
    )
    backend._secrets = ("gate-secret",)
    result = backend.edit("file", "before", "after")
    assert result.error and "cannot replace" in result.error
    assert "gate-secret" not in result.error and backend.status()["state"] == "ready"
