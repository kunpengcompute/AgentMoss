"""AIO contract, Gate integration and lifecycle regression tests; no external models."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from agent_sandbox.core.api_error import ApiError
from langchain_core.messages import AIMessage
from models import call
from test_native_sdk import make_agent, run_args, setup

from agentmoss_deepagents import DeepAgentsIntegration
from agentmoss_deepagents.actions import create_action_adapter
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
    action = create_action_adapter(backend).adapt(tool, args)
    assert action.metadata["real_path"] == backend.normalize_path(validate_path(path))
    assert action.metadata["os_type"] == "linux"
    assert args == original


def test_missing_search_path_uses_working_dir(backend):
    assert create_action_adapter(backend).adapt("glob", {"pattern": "*.txt"}).metadata["real_path"] == "/home/gem"
    assert backend.normalize_path("foo.txt") == "/home/gem/foo.txt"


@pytest.mark.parametrize("code", [0, 7, 126])
def test_execute_keeps_real_exit_and_disables_retry(backend, client, code):
    client.shell.answer = response(status="completed", exit_code=code, output="exact")
    result = backend.execute("printf exact", timeout=5)
    assert result.exit_code == code and result.output == "exact"
    sent = client.shell.calls[0]
    assert sent["command"] == "printf exact"
    assert sent["exec_dir"] == "/home/gem" and sent["strict"] is True
    assert sent["async_mode"] is False
    assert sent["request_options"]["max_retries"] == 0
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


def test_grep_uses_structured_api_not_pty_null_delimiters(backend, client):
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
    # PTY strips the NUL filename separator required by BaseSandbox's shell parser.
    client.shell.answer = response(status="completed", exit_code=0, output="/home/gem/file.txt2:literal.*")
    result = backend.grep("literal.*", "/home/gem", "*.txt", max_count=1)
    assert result.error is None
    assert result.matches == [{"path": "/home/gem/file.txt", "line": 2, "text": "literal.*"}]
    assert result.truncated is True
    assert seen[0]["fixed_strings"] is True
    assert seen[0]["include"] == ["*.txt"]
    assert seen[0]["request_options"]["max_retries"] == 0
    assert client.shell.calls == []
