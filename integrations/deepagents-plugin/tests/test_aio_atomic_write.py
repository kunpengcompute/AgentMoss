"""Exercise exclusive creation with the official SDK and real filesystem effects."""

import asyncio
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from agent_sandbox import Sandbox
from langchain_core.messages import AIMessage
from models import call
from test_aio_backend import sdk_backend as sdk_backend
from test_native_sdk import make_agent, run_args, setup

from agentmoss_deepagents.aio_backend import AIOSandboxBackend
from agentmoss_deepagents.aio_errors import AIOExecutionUncertain, AIOUnavailable
from agentmoss_deepagents.backend_config import AIOBackendConfig
from agentmoss_deepagents.history import KEY


@pytest.fixture
def atomic_service(tmp_path):
    service = SimpleNamespace(seen=[], barrier=None, transform=None)

    def transport(request):
        if request.method == "DELETE":
            return httpx.Response(200, json={"success": True})
        body = json.loads(request.content)
        endpoint = request.url.path
        if endpoint.endswith("/create"):
            return httpx.Response(200, json={"success": True, "data": {
                "session_id": body["id"], "working_dir": body["exec_dir"],
            }})
        service.seen.append((endpoint, body))
        if endpoint == "/v1/file/find":
            files = [str(p) for p in Path(body["path"]).glob(body["glob"])]
            if service.barrier:
                service.barrier.wait(5)
            return httpx.Response(200, json={"success": True, "data": {"path": body["path"], "files": files}})
        if endpoint == "/v1/file/write":
            target = Path(body["file"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body["content"])
            return httpx.Response(200, json={"success": True, "data": {"file": str(target)}})
        assert endpoint == "/v1/code/execute"
        assert body["language"] == "python" and body["stateful"] is False
        assert 1 <= body["timeout"] <= 300
        if service.barrier:
            service.barrier.wait(5)
        process = subprocess.run(
            [sys.executable, "-"], input=body["code"], cwd=tmp_path,
            capture_output=True, text=True, timeout=5,
        )
        data = {
            "language": "python", "code": body["code"],
            "status": "ok" if process.returncode == 0 else "error", "exit_code": process.returncode,
            "stdout": process.stdout, "stderr": process.stderr, "outputs": [],
        }
        if service.transform:
            return service.transform(data)
        return httpx.Response(200, json={"success": True, "data": data})

    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        config = AIOBackendConfig("http://proxy.test", "same-sandbox", working_dir=str(tmp_path))
        backends = [AIOSandboxBackend(Sandbox(base_url="http://proxy.test", httpx_client=http), config=config)
                    for _ in range(2)]
        try:
            yield backends, service
        finally:
            for backend in backends:
                backend.close()


def test_concurrent_writers_have_one_winner(atomic_service, tmp_path):
    backends, service = atomic_service
    service.barrier = threading.Barrier(2)
    target = tmp_path / "shared.txt"
    contents = ["first", "second"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(backend.write, str(target), content)
                for backend, content in zip(backends, contents, strict=True)]
        results = [job.result(10) for job in jobs]
    winners = [i for i, result in enumerate(results) if result.error is None]
    assert len(winners) == 1, "Both writes succeeded; the later writer overwrote the first"
    assert target.read_text() == contents[winners[0]]
    assert all(backend.status()["state"] == "ready" for backend in backends)


@pytest.mark.parametrize(
    "content", ["", "new\r\ntext\x00尾部", "'\n__import__('os').remove('unrelated')\n", "x" * 300000]
)
def test_creation_preserves_content_and_existing_target(atomic_service, tmp_path, content):
    (backend, _), service = atomic_service
    target = tmp_path / "new parent" / "目录 ' [x].txt"
    (tmp_path / "unrelated").write_text("keep")
    assert backend.write(str(target), content).error is None
    assert backend.write(str(target), "overwrite").error
    assert target.read_bytes() == content.encode("utf-8")
    assert (tmp_path / "unrelated").read_text() == "keep"
    assert [path for path, _ in service.seen] == ["/v1/code/execute", "/v1/code/execute"]


def test_invalid_parent_is_recoverable(atomic_service, tmp_path):
    (backend, _), _ = atomic_service
    parent = tmp_path / "not-a-directory"
    parent.write_text("keep")
    result = backend.write(str(parent / "file"), "new")
    assert result.error and backend.status()["state"] == "ready"
    assert backend.write(str(tmp_path / "valid"), "ok").error is None
    assert parent.read_text() == "keep"


@pytest.mark.parametrize("kind", ["lost", "bad_json", "wrong_path", "partial", "running"])
def test_unconfirmed_creation_is_not_retried(atomic_service, tmp_path, kind):
    (backend, _), service = atomic_service
    target = tmp_path / "written"

    def transform(data):
        if kind == "lost":
            raise httpx.ReadTimeout("traffic-secret")
        if kind == "bad_json":
            data["stdout"] = "broken acknowledgement"
        elif kind == "wrong_path":
            data["stdout"] = json.dumps({"status": "created", "path": "/somewhere/else"})
        elif kind == "partial":
            data.update(status="error", exit_code=1, stdout="", stderr="no space left")
        else:
            data.update(status="running", exit_code=None)
        return httpx.Response(200, json={"success": True, "data": data})

    service.transform = transform
    with pytest.raises(AIOExecutionUncertain):
        backend.write(str(target), "once")
    assert target.read_text() == "once"
    with pytest.raises(AIOUnavailable):
        backend.write(str(tmp_path / "must-not-exist"), "no")
    assert len(service.seen) == 1 and not (tmp_path / "must-not-exist").exists()


def test_python_stream_output_acknowledgement(atomic_service, tmp_path):
    (backend, _), service = atomic_service

    def transform(data):
        data["outputs"] = [{"output_type": "stream", "name": "stdout", "text": data.pop("stdout")}]
        data.pop("exit_code")
        return httpx.Response(200, json={"success": True, "data": data})

    service.transform = transform
    assert backend.write(str(tmp_path / "stream-result"), "ok").error is None


@pytest.mark.parametrize("malformed", [False, True])
@pytest.mark.parametrize("error", ["permission_denied", "already_exists", "not_found", "read_only_filesystem"])
def test_file_error_body_cannot_confirm_code_api_rejection(atomic_service, tmp_path, malformed, error):
    (backend, _), service = atomic_service
    target = tmp_path / "written-before-broken-response"

    def broken_response(data):
        failure = {"path": str(target), "operation": "write", "error_type": error}
        return httpx.Response(200, json={"success": False, "data": failure if malformed else {**data, **failure}})

    service.transform = broken_response
    with pytest.raises(AIOExecutionUncertain):
        backend.write(str(target), "once")
    assert target.read_text() == "once"
    assert backend.status()["state"] == "uncertain"
    with pytest.raises(AIOUnavailable):
        backend.write(str(tmp_path / "second"), "must not run")
    assert len(service.seen) == 1 and not (tmp_path / "second").exists()


@pytest.mark.parametrize("code,error", [(403, "permission_denied"), (404, "file_not_found")])
def test_explicit_code_http_rejection_is_recoverable(sdk_backend, code, error):
    backend, submitted, replies = sdk_backend
    replies.extend([
        httpx.Response(code),
        httpx.Response(200, json={"success": True, "data": {"file": "/home/gem/ok", "content": "ok"}}),
    ])
    assert backend.write("new", "data").error == error
    assert backend.status()["state"] == "ready"
    assert backend.read("ok").file_data["content"] == "ok"
    assert [r.url.path for r in submitted] == ["/v1/code/execute", "/v1/file/read"]


@pytest.mark.parametrize("error", ["permission_denied", "read_only_filesystem"])
def test_verified_creation_receipt_is_a_recoverable_rejection(sdk_backend, error):
    backend, _, replies = sdk_backend

    def response(request):
        return httpx.Response(200, json={"success": True, "data": {
            "language": "python", "code": json.loads(request.content)["code"], "status": "ok", "exit_code": 0,
            "stdout": json.dumps({"status": "rejected", "error": error}),
        }})

    replies.append(response)
    assert backend.write("new", "data").error == error
    assert backend.status()["state"] == "ready"


@pytest.mark.parametrize("seconds,expected", [(5.9, 5), (900, 300), (0.5, None)])
def test_write_timeout_respects_code_api_and_configuration(atomic_service, tmp_path, seconds, expected):
    (backend, _), service = atomic_service
    backend.config = replace(backend.config, default_timeout=seconds, hard_timeout=seconds)
    result = backend.write(str(tmp_path / "timeout"), "ok")
    if expected is None:
        assert result.error and not service.seen and not (tmp_path / "timeout").exists()
    else:
        assert result.error is None
        assert len(service.seen) == 1 and service.seen[0][1]["timeout"] == expected


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("deny", [False, True])
async def test_file_tool_gate_checks_before_atomic_create(atomic_service, tmp_path, sync, deny):
    (backend, _), service = atomic_service
    runtime, seen = setup(tmp_path, httpx.Response(200, json={"decision": "Deny" if deny else "Allow"}))
    target = tmp_path / "gated.txt"
    graph = make_agent(runtime, tmp_path, [call("write_file", {"file_path": str(target), "content": "once"}, "write"),
                                          AIMessage(content="done")], backend=backend)
    inputs, config, context = run_args()
    try:
        result = (await asyncio.to_thread(graph.invoke, inputs, config, context=context) if sync
                  else await graph.ainvoke(inputs, config, context=context))
        assert target.exists() is (not deny)
        assert len(service.seen) == (0 if deny else 1)
        assert seen[0]["tool_name"] == "write"
        assert result[KEY]["events"][0]["status"] == ("denied" if deny else "executed_success")
    finally:
        await runtime.aclose()
