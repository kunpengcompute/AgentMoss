"""Gate must start on an available port when the requested one is occupied."""

import asyncio
import errno
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_gate.v1.server import app as app_module
from agent_gate.v1.storage.db import Database


@pytest.fixture
def ipv6_hostname():
    """Use an existing local alias; never change the machine's resolver config."""
    for host in ("ip6-localhost", "ip6-loopback", "localhost"):
        try:
            addresses = socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM)
            if {address[0] for address in addresses} != {socket.AF_INET6}:
                continue
            with socket.socket(socket.AF_INET6) as probe:
                probe.bind((host, 0))
            return host
        except OSError:
            continue
    pytest.skip("No local IPv6-only hostname is available")


def _verify_uvicorn_bind(host, port, loop_backend="asyncio", workers=1):
    """Exercise Uvicorn's real startup/bind path, without serving requests."""
    config = app_module.uvicorn.Config(
        app_module.create_app(), host=host, port=port, workers=workers,
        lifespan="off", ws="none", log_config=None,
    )
    if workers > 1:
        with config.bind_socket() as listener:
            listener.listen()
        return

    if loop_backend == "uvloop":
        loop = pytest.importorskip("uvloop").new_event_loop()
    else:
        loop = asyncio.new_event_loop()
    config.load()
    server = app_module.uvicorn.Server(config)
    server.lifespan = config.lifespan_class(config)

    async def startup_and_close():
        try:
            await server.startup()
            assert server.started
        finally:
            for listener in getattr(server, "servers", []):
                listener.close()
                await listener.wait_closed()

    try:
        loop.run_until_complete(startup_and_close())
    finally:
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


@pytest.mark.parametrize("loop_backend", ["asyncio", "uvloop"])
def test_ipv6_only_hostname_migrates_to_a_port_uvicorn_can_bind(ipv6_hostname, loop_backend):
    with socket.socket(socket.AF_INET6) as listener:
        listener.bind(("::1", 0))
        listener.listen()
        requested = listener.getsockname()[1]
        actual = app_module._find_free_port(requested, ipv6_hostname)
        assert requested < actual < min(requested + 100, 65536)
        _verify_uvicorn_bind(ipv6_hostname, actual, loop_backend)


@pytest.mark.parametrize("loop_backend", ["asyncio", "uvloop"])
def test_ipv6_wildcard_keeps_port_occupied_only_on_ipv4(loop_backend):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        requested = listener.getsockname()[1]
        # Uvicorn's single-worker listener is IPv6-only and can use this port.
        _verify_uvicorn_bind("::", requested, loop_backend)
        assert app_module._find_free_port(requested, "::") == requested


@pytest.mark.parametrize("occupied", [False, True])
@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0", "::1", "localhost"])
@pytest.mark.parametrize("workers", [1, 2])
def test_run_server_selects_bindable_port(tmp_path, monkeypatch, occupied, host, workers):
    db = Database(str(tmp_path / "gate.db"))
    db.init_schema()
    import agent_gate.v1.storage.db as db_module
    import agent_gate.v1.storage.maintenance as maintenance
    monkeypatch.setattr(db_module, "get_db", lambda *args: db)
    monkeypatch.setattr(maintenance, "start_storage_maintenance", lambda: None)
    seen = {}

    def start_uvicorn(app, **kwargs):
        _verify_uvicorn_bind(kwargs["host"], kwargs["port"], workers=kwargs["workers"])
        seen.update(kwargs)

    monkeypatch.setattr(app_module.uvicorn, "run", start_uvicorn)
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    with socket.socket(family) as listener:
        listener.bind((host, 0))
        requested = listener.getsockname()[1]
        if occupied:
            listener.listen()
        else:
            listener.close()
        app_module.run_server(host=host, port=requested, workers=workers, db_path=db.db_path)
    assert seen["host"] == host
    if occupied:
        assert requested < seen["port"] < requested + 100
    else:
        assert seen["port"] == requested
    db.close()


def test_multiple_workers_keep_uvicorn_default_ipv6_dual_stack_behavior():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        requested = listener.getsockname()[1]
        with socket.socket(socket.AF_INET6) as probe:
            if probe.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY):
                pytest.skip("System sockets are already IPv6-only")
        actual = app_module._find_free_port(requested, "::", workers=2)
        assert requested < actual < min(requested + 100, 65536)
        _verify_uvicorn_bind("::", actual, workers=2)


@pytest.mark.parametrize("occupied_host", ["127.0.0.1", "::1"])
def test_hostname_requires_a_free_port_on_every_resolved_address(monkeypatch, occupied_host):
    real_getaddrinfo = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if host == "dual-stack.test":
            return (
                real_getaddrinfo("127.0.0.1", port, *args, **kwargs)
                + real_getaddrinfo("::1", port, *args, **kwargs)
            )
        return real_getaddrinfo(host, port, *args, **kwargs)

    # Control only DNS; the occupied listener and all probes remain real sockets.
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    family = socket.AF_INET6 if occupied_host == "::1" else socket.AF_INET
    with socket.socket(family) as listener:
        listener.bind((occupied_host, 0))
        listener.listen()
        requested = listener.getsockname()[1]
        actual = app_module._find_free_port(requested, "dual-stack.test")
        assert requested < actual < min(requested + 100, 65536)
        _verify_uvicorn_bind("dual-stack.test", actual)


def test_unsupported_socket_family_does_not_hide_available_ipv4_port(monkeypatch):
    real_socket = socket.socket
    real_getaddrinfo = socket.getaddrinfo

    def ipv4_only_socket(family=socket.AF_INET, *args, **kwargs):
        if family == socket.AF_INET6:
            raise OSError(errno.EAFNOSUPPORT, "IPv6 is unavailable")
        return real_socket(family, *args, **kwargs)

    def resolve(host, port, *args, **kwargs):
        if host == "dual-stack.test":
            return (
                real_getaddrinfo("127.0.0.1", port, *args, **kwargs)
                + real_getaddrinfo("::1", port, *args, **kwargs)
            )
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", ipv4_only_socket)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        requested = listener.getsockname()[1]
        actual = app_module._find_free_port(requested, "dual-stack.test")
        assert requested < actual < min(requested + 100, 65536)
        _verify_uvicorn_bind("dual-stack.test", actual)


@pytest.mark.parametrize("loop_backend", ["asyncio", "uvloop"])
def test_nonlocal_address_follows_uvicorn_auto_loop_behavior(monkeypatch, loop_backend):
    if loop_backend == "asyncio":
        monkeypatch.setitem(sys.modules, "uvloop", None)
    else:
        pytest.importorskip("uvloop")
    with socket.socket() as probe:
        try:
            probe.bind(("192.0.2.1", 0))
        except OSError as exc:
            assert exc.errno == errno.EADDRNOTAVAIL
        else:
            pytest.skip("Test documentation address is configured locally")
    real_getaddrinfo = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if host == "partly-local.test":
            return (
                real_getaddrinfo("127.0.0.1", port, *args, **kwargs)
                + real_getaddrinfo("192.0.2.1", port, *args, **kwargs)
            )
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        requested = listener.getsockname()[1]
        actual = app_module._find_free_port(requested, "partly-local.test", scan=3)
        if loop_backend == "asyncio":
            assert requested < actual < requested + 3
            _verify_uvicorn_bind("partly-local.test", actual)
        else:
            # uvloop rejects EADDRNOTAVAIL instead of skipping that address.
            assert actual == requested
            with pytest.raises(SystemExit):
                _verify_uvicorn_bind(["127.0.0.1", "192.0.2.1"], 0, "uvloop")


def test_all_unavailable_addresses_leave_original_port_for_uvicorn(monkeypatch):
    monkeypatch.setitem(sys.modules, "uvloop", None)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        requested = probe.getsockname()[1]
    assert app_module._find_free_port(requested, "192.0.2.1", scan=3) == requested


def test_scan_exhaustion_returns_original_port():
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        first.listen()
        requested = first.getsockname()[1]
        try:
            second.bind(("127.0.0.1", requested + 1))
        except OSError:
            pytest.skip("Adjacent test port is unavailable")
        second.listen()
        assert app_module._find_free_port(requested, "127.0.0.1", scan=2) == requested


def test_scan_does_not_exceed_highest_tcp_port():
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 65535))
        except OSError:
            pytest.skip("Port 65535 is already in use")
        listener.listen()
        assert app_module._find_free_port(65535, "127.0.0.1") == 65535


def test_port_zero_is_left_for_uvicorn_to_allocate():
    assert app_module._find_free_port(0, "127.0.0.1") == 0


@pytest.mark.parametrize("host_kind", ["ipv6_hostname", "ipv6_wildcard"])
def test_real_run_server_serves_http_with_ipv6_port_selection(tmp_path, ipv6_hostname, host_kind):
    host = ipv6_hostname if host_kind == "ipv6_hostname" else "::"
    family = socket.AF_INET6 if host_kind == "ipv6_hostname" else socket.AF_INET
    blocking_host = "::1" if family == socket.AF_INET6 else "127.0.0.1"
    env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_GATE_")}
    env.update({
        "AGENT_GATE_AUTH_MODE": "off",
        "AGENT_GATE_ARCHIVE_THRESHOLD": "0",
        "AGENT_GATE_DATA_DIR": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
    })
    # A local empty config takes precedence over personal/system config files.
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agent_gate.yaml").write_text("{}\n")
    log_path = tmp_path / "server.log"
    script = (
        "import sys; from agent_gate.v1.server.app import run_server; "
        "run_server(host=sys.argv[1], port=int(sys.argv[2]), db_path=sys.argv[3])"
    )
    with socket.socket(family) as listener, log_path.open("w") as output:
        listener.bind((blocking_host, 0))
        listener.listen()
        requested = listener.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", script, host, str(requested), str(tmp_path / "gate.db")],
            cwd=tmp_path, env=env, stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                logs = log_path.read_text()
                assert proc.poll() is None, logs
                match = re.search(r"Starting AgentGate in HTTP mode: " + re.escape(host) + r":(\d+)", logs)
                if match:
                    actual = int(match.group(1))
                    connection = http.client.HTTPConnection("::1", actual, timeout=0.2)
                    try:
                        connection.request("GET", "/openapi.json")
                        response = connection.getresponse()
                        assert response.status == 200
                        assert json.loads(response.read())["info"]["title"] == "AgentGate"
                        break
                    except (OSError, http.client.HTTPException):
                        pass
                    finally:
                        connection.close()
                time.sleep(0.05)
            else:
                pytest.fail("AgentGate did not serve HTTP within 8 seconds:\n" + log_path.read_text())

            if host_kind == "ipv6_hostname":
                assert requested < actual < min(requested + 100, 65536)
            else:
                assert actual == requested
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
