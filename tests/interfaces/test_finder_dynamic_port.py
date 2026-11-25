"""The Finder host receives the actual port bound by its child server."""

import socket

import pytest
import uvicorn

from heavenbase.dashboard import macos

pytestmark = pytest.mark.fast


def test_finder_readiness_reports_the_childs_dynamic_port(monkeypatch, tmp_path):
    ready_file = tmp_path / "ready"
    monkeypatch.setattr(macos, "resolve_app_bind", lambda: ("127.0.0.1", 0))

    with socket.socket() as listener:
        for port in range(25130, 25230):
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                continue
            break
        else:
            pytest.fail("no free Finder test port")

        run = uvicorn.Server.run
        monkeypatch.setattr(uvicorn.Server, "run", lambda server: run(server, sockets=[listener]))

        async def read_ready_port(server):
            assert server.started
            assert ready_file.read_text(encoding="ascii") == f"{port}\n"

        monkeypatch.setattr(uvicorn.Server, "main_loop", read_ready_port)
        assert macos.main(["--serve", "--ready-file", str(ready_file)]) == 0
