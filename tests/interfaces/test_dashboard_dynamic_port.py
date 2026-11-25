"""Regressions for the dashboard's dynamic listener port."""

import asyncio
from http.client import HTTPConnection
import socket
from threading import Event
from urllib.parse import urlsplit

import pytest
import uvicorn

from heavenbase.cli.app import create_cli
from heavenbase.cli.backends.argparse import run_argparse
from heavenbase.dashboard import launch

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("configured", [True, False], ids=["config", "argument"])
def test_dashboard_opens_the_listener_port_after_dynamic_bind(monkeypatch, configured):
    api = launch.DashboardAPI()
    monkeypatch.setitem(vars(api.cm), "get", _dashboard_config(api.cm.get, configured))
    opened = Event()
    seen = []

    def open_dashboard(url, mode):
        seen.append((url, mode))
        opened.set()

    async def wait_for_opener(server):
        assert await asyncio.to_thread(opened.wait, 10)
        target = urlsplit(seen[0][0])
        assert target.hostname == "127.0.0.1"
        assert target.port == port

        def request_status():
            connection = HTTPConnection(target.hostname, target.port, timeout=5)
            try:
                connection.request("GET", "/openapi.json")
                return connection.getresponse().status
            finally:
                connection.close()

        assert await asyncio.to_thread(request_status) == 200
        assert seen[0][1] == "browser"

    monkeypatch.setattr(launch, "open_dashboard", open_dashboard)
    monkeypatch.setattr(uvicorn.Server, "main_loop", wait_for_opener)

    with socket.socket() as listener:
        for port in range(25030, 25130):
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                continue
            break
        else:
            pytest.fail("no free dashboard test port")

        run = uvicorn.Server.run

        def run_on_test_socket(server):
            assert server.config.port == 0
            run(server, sockets=[listener])

        monkeypatch.setattr(uvicorn.Server, "run", run_on_test_socket)
        launch.serve(host="127.0.0.1", port=None if configured else 0, api=api, open_window=True, host_mode="browser")


def _dashboard_config(get, configured):
    def dashboard_config(key, *args, **kwargs):
        if key == "heavenbase.dashboard.port":
            return 0 if configured else 42777
        return get(key, *args, **kwargs)

    return dashboard_config


def test_dashboard_cli_reports_dynamic_listener_without_opening_window(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(launch, "open_dashboard", lambda *args: opened.append(args))
    with socket.socket() as listener:
        for port in range(25700, 25750):
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                continue
            break
        else:
            pytest.fail("no free dashboard test port")

        async def stop(server):
            assert server.started

        run = uvicorn.Server.run

        def run_on_test_socket(server):
            assert server.config.port == 0
            run(server, sockets=[listener])

        monkeypatch.setattr(uvicorn.Server, "main_loop", stop)
        monkeypatch.setattr(uvicorn.Server, "run", run_on_test_socket)
        run_argparse(create_cli("argparse"), ["dashboard", "--port", "0", "--no-open"])

    assert f"dashboard at http://127.0.0.1:{port}" in capsys.readouterr().out
    assert opened == []
