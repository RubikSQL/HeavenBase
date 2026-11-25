"""Regression coverage for the dashboard opener and its HTTP listener."""

import asyncio
import contextlib
import socket
import subprocess
import sys
from http.client import HTTPConnection
from threading import Event

import pytest
import uvicorn
from starlette.applications import Starlette
from uvicorn.main import STARTUP_FAILURE

from heavenbase.api import http
from heavenbase.dashboard import launch, macos

pytestmark = pytest.mark.fast


def _bind_test_port(listener, start):
    for port in range(start, start + 100):
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            continue
        return port
    pytest.fail("no free dashboard test port")


def test_dashboard_does_not_open_on_failed_bind(monkeypatch):
    opened = Event()
    monkeypatch.setattr(launch, "open_dashboard", lambda *args: opened.set())
    with socket.socket() as occupied:
        port = _bind_test_port(occupied, 24630)
        occupied.listen()

        with pytest.raises(SystemExit) as error:
            launch.serve(host="127.0.0.1", port=port, open_window=True)
        assert error.value.code == 1
        assert not opened.wait(0.5)


@pytest.mark.slow
def test_dashboard_command_exits_with_status_1_on_a_busy_port(tmp_path, home_env):
    """Uvicorn exits with status 1 when it cannot bind, as ``uvicorn.run`` did in 0.1.2.2."""
    with socket.socket() as occupied:
        port = _bind_test_port(occupied, 24660)
        occupied.listen()
        result = subprocess.run(
            [sys.executable, "-m", "heavenbase.cli.app", "dashboard", "--port", str(port), "--no-open"],
            cwd=tmp_path,
            env=home_env(tmp_path / "home"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
        )

    assert result.returncode == 1, (result.stdout[-2000:], result.stderr[-3000:])
    assert "error while attempting to bind" in result.stderr  # Uvicorn's own message; the OS reason text differs per platform


@pytest.mark.parametrize("entry", ["serve", "run_server"])
def test_dashboard_whose_application_startup_fails_exits_with_startup_failure_status(monkeypatch, entry):
    """A server that binds nothing because its application startup failed exits with status 3."""

    @contextlib.asynccontextmanager
    async def failing_lifespan(app):
        raise RuntimeError("application startup failed")
        yield

    monkeypatch.setattr(http, "build_app", lambda *args, **kwargs: Starlette(lifespan=failing_lifespan))
    with socket.socket() as probe:
        port = _bind_test_port(probe, 24680)
    run = launch.serve if entry == "serve" else http.run_server
    with pytest.raises(SystemExit) as error:
        run(host="127.0.0.1", port=port, **({"open_window": False} if entry == "serve" else {}))
    assert error.value.code == STARTUP_FAILURE == 3


def test_dashboard_opener_can_reach_the_started_http_service(monkeypatch):
    with socket.socket() as listener:
        port = _bind_test_port(listener, 24730)
        _run_on_reserved_socket(monkeypatch, listener)
        _assert_dashboard_opener(monkeypatch, port)


def _run_on_reserved_socket(monkeypatch, listener):
    run = uvicorn.Server.run
    monkeypatch.setattr(uvicorn.Server, "run", lambda server: run(server, sockets=[listener]))


def _assert_dashboard_opener(monkeypatch, port):
    opened = Event()
    seen = []

    def open_dashboard(url, mode):
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/openapi.json")
            response = connection.getresponse()
            response.read()
            seen.append((url, mode, response.status))
        finally:
            connection.close()
            opened.set()

    async def wait_for_browser(server):
        assert await asyncio.to_thread(opened.wait, 10)

    monkeypatch.setattr(launch, "open_dashboard", open_dashboard)
    monkeypatch.setattr(uvicorn.Server, "main_loop", wait_for_browser)

    launch.serve(host="127.0.0.1", port=port, open_window=True, host_mode="browser")

    assert seen == [(f"http://127.0.0.1:{port}", "browser", 200)]


def test_finder_child_does_not_signal_readiness_on_failed_bind(tmp_path):
    ready_file = tmp_path / "ready"
    with socket.socket() as occupied:
        port = _bind_test_port(occupied, 24830)
        occupied.listen()
        with pytest.raises(SystemExit) as error:
            macos.main(["--serve", "--host", "127.0.0.1", "--port", str(port), "--ready-file", str(ready_file)])

    assert error.value.code == 1
    assert not ready_file.exists()


def test_finder_child_signals_readiness_at_server_startup(monkeypatch, tmp_path):
    ready_file = tmp_path / "ready"

    async def verify_readiness(server):
        assert server.started
        assert ready_file.is_file()

    monkeypatch.setattr(uvicorn.Server, "main_loop", verify_readiness)
    with socket.socket() as listener:
        port = _bind_test_port(listener, 24930)
        _run_on_reserved_socket(monkeypatch, listener)
        assert macos.main(["--serve", "--host", "127.0.0.1", "--port", str(port), "--ready-file", str(ready_file)]) == 0
