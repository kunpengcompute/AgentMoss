"""The sample application's cancelled startup must not leak a sandbox session."""

import asyncio
import importlib.util
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("entry", ["example", "verifier"])
def test_cancelled_startup_waits_for_connection_and_closes_it(monkeypatch, tmp_path, entry):
    path = ROOT / ("examples/deepagents_aio.py" if entry == "example" else "verify.py")
    spec = importlib.util.spec_from_file_location("cubesandbox_sample_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    token_file = tmp_path / "gate.token"
    token_file.write_text("test-token")
    for key, value in {
        "MOSS_GATE_URL": "http://gate.invalid",
        "MOSS_GATE_TOKEN_FILE": str(token_file),
        "MOSS_AIO_URL": "http://aio.invalid",
        "MOSS_SANDBOX_ID": "test-sandbox",
    }.items():
        monkeypatch.setenv(key, value)
    if entry == "verifier":
        client = httpx.AsyncClient
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"status": "healthy", "moss_status": "healthy"},
            )
        )
        monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: client(transport=transport, **kwargs))

    async def scenario():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release, completed = threading.Event(), threading.Event()

        class Connection:
            closed = False

            async def aclose(self):
                self.closed = True

        connection = Connection()

        def load(config):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(timeout=5):
                raise TimeoutError("Test failed to release the startup worker")
            completed.set()
            return connection

        monkeypatch.setattr(module.DeepAgentsIntegration, "load", load)
        operation = (
            module.run_once(None, None, "test-session", "hello", config={}) if entry == "example" else module.main()
        )
        task = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            # Let cancellation reach the pending initialization before it completes.
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await asyncio.to_thread(completed.wait, 5)
            assert connection.closed, "Cancelled initialization leaked its completed sandbox connection"
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("response_mode", ["delayed", "still_present", "invalid"])
def test_cleanup_is_confirmed_by_session_query(monkeypatch, response_mode):
    spec = importlib.util.spec_from_file_location("cubesandbox_verify_under_test", ROOT / "verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen = []

    def handle(request):
        seen.append(request)
        assert request.url.path == "/v1/shell/sessions"
        assert request.headers["host"] == "8080-test-sandbox.cube.app"
        assert request.headers["e2b-traffic-access-token"] == "test-traffic-token"
        if response_mode == "invalid":
            return httpx.Response(200, json={"success": True})
        sessions = {} if response_mode == "delayed" and len(seen) >= 2 else {"test-session": {}}
        return httpx.Response(200, json={"success": True, "data": {"sessions": sessions}})

    client = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: client(transport=transport, **kwargs))
    config = SimpleNamespace(
        proxy_url="http://aio.invalid",
        host="8080-test-sandbox.cube.app",
        read_token=lambda: "test-traffic-token",
    )
    if response_mode == "delayed":
        asyncio.run(module.wait_for_session_cleanup(config, "test-session", timeout=2))
        assert len(seen) == 2
    else:
        with pytest.raises(RuntimeError, match="会话"):
            asyncio.run(module.wait_for_session_cleanup(config, "test-session", timeout=0.01))
