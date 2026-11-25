"""A model server on this machine is reached directly, whatever proxy the system configures.

The SDK clients take their proxy from ``urllib.request.getproxies()``, which on Windows and macOS reads the
system proxy settings but not their bypass list. A system proxy then also caught ``http://127.0.0.1``.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
from importlib.util import find_spec
import json
import socket
import threading
import urllib.request

import pytest

import heavenbase as hb
from heavenbase.llm.gateways import _protocols
from heavenbase.utils import NetworkProxy

pytestmark = pytest.mark.fast

_OPENAI_REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 0,
    "model": "m1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_ANTHROPIC_REPLY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "m1",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


class _Handler(BaseHTTPRequestHandler):
    reply_text = "ok"

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        reply = _ANTHROPIC_REPLY if self.path.rstrip("/").endswith("/messages") else _OPENAI_REPLY
        body = json.dumps(reply).replace('"ok"', json.dumps(self.reply_text)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _ProxyHandler(_Handler):
    """Answer every proxied request itself with the text ``proxy``."""

    reply_text = "proxy"


@contextmanager
def _serve(handler=_Handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _clear_proxy_environment(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_proxy_variables(monkeypatch):
    """Start without proxy variables or cached clients, so that a client reads only the proxies a test sets."""
    _clear_proxy_environment(monkeypatch)
    _protocols.dispose_llm_clients()
    yield
    _protocols.dispose_llm_clients()


@pytest.fixture
def system_proxy(monkeypatch):
    """Configure a system proxy that refuses every connection, as an unreachable corporate or VPN proxy would."""
    proxy = f"http://127.0.0.1:{_closed_port()}"
    proxies = {"http": proxy, "https": proxy}
    _clear_proxy_environment(monkeypatch)
    # httpx and the anthropic SDK bind getproxies when they are imported, so import them before urllib's is replaced.
    readers = [pytest.importorskip("httpx._utils"), *(importlib.import_module(name) for name in ["anthropic._utils._httpx"] if find_spec("anthropic"))]
    for reader in (urllib.request, *readers):
        monkeypatch.setattr(reader, "getproxies", lambda: dict(proxies), raising=False)
    _protocols.dispose_llm_clients()
    yield proxy
    _protocols.dispose_llm_clients()


def _llm(gateway: str, host: str, port: int, **extra) -> hb.LLM:
    pytest.importorskip("openai" if gateway == "openai" else "anthropic")
    if gateway == "openai":
        return hb.LLM(
            preset="custom", model="m1", base_url=f"http://{host}:{port}/v1", api_key="sk-test", cache=False, stream_chat=False, max_retries=0, **extra
        )
    return hb.LLM(
        model="haiku",
        provider="anthropic",
        gateway="anthropic",
        base_url=f"http://{host}:{port}",
        api_key="sk-test",
        cache=False,
        stream_chat=False,
        max_retries=0,
        **extra,
    )


@pytest.mark.slow
@pytest.mark.parametrize("gateway", ["openai", "anthropic"])
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_a_loopback_model_server_is_reached_without_the_system_proxy(system_proxy, gateway, host):
    with _serve() as port:
        llm = _llm(gateway, host, port)
        assert llm.chat("hi") == "ok"
        assert asyncio.run(llm.achat("hi")) == "ok"


def test_an_explicit_proxy_of_the_llm_still_applies_to_a_loopback_server(system_proxy):
    httpx = pytest.importorskip("httpx")
    llm = _llm("openai", "127.0.0.1", 9, http_proxy=system_proxy, https_proxy=system_proxy)
    inner = llm.to_client()._client
    transport = inner._transport_for_url(httpx.URL("http://127.0.0.1:9/v1/chat/completions"))
    assert "Proxy" in type(transport._pool).__name__


def test_a_remote_model_server_keeps_the_system_proxy(system_proxy):
    httpx = pytest.importorskip("httpx")
    llm = _llm("openai", "models.example.com", 443)
    inner = llm.to_client()._client
    transport = inner._transport_for_url(httpx.URL("http://models.example.com:443/v1/chat/completions"))
    assert "Proxy" in type(transport._pool).__name__


@pytest.mark.slow
@pytest.mark.parametrize("gateway", ["openai", "anthropic"])
def test_a_proxy_passed_to_one_call_applies_to_that_call_only(no_proxy_variables, gateway):
    with _serve() as port, _serve(_ProxyHandler) as proxy_port:
        llm, proxy = _llm(gateway, "127.0.0.1", port), f"http://127.0.0.1:{proxy_port}"
        proxied = {"http_proxy": proxy, "no_proxy": ""}

        async def chats():
            return [await llm.achat("hi", **proxied), await llm.achat("hi"), await llm.achat("hi", **proxied)]

        assert [llm.chat("hi", **proxied), llm.chat("hi"), llm.chat("hi", **proxied)] == ["proxy", "ok", "proxy"]
        assert asyncio.run(chats()) == ["proxy", "ok", "proxy"]
        # An empty option clears the LLM's own proxy for that call.
        own = _llm(gateway, "127.0.0.1", port, **proxied)
        assert [own.chat("hi"), own.chat("hi", http_proxy=""), own.chat("hi")] == ["proxy", "ok", "proxy"]


def test_a_network_proxy_block_applies_to_the_calls_inside_it_only(no_proxy_variables):
    with _serve() as port, _serve(_ProxyHandler) as proxy_port:
        llm, block = _llm("openai", "127.0.0.1", port), NetworkProxy(http_proxy=f"http://127.0.0.1:{proxy_port}", no_proxy="")

        async def chats():
            with block:
                inside = await asyncio.gather(llm.achat("hi"), llm.achat("hi"))
            return [*inside, await llm.achat("hi")]

        before = llm.chat("hi")
        with block:
            inside, cleared = llm.chat("hi"), llm.chat("hi", http_proxy="")
        assert [before, inside, cleared, llm.chat("hi")] == ["ok", "proxy", "ok", "ok"]
        assert asyncio.run(chats()) == ["proxy", "proxy", "ok"]


def test_a_proxy_of_another_thread_or_task_does_not_reach_a_loopback_server(no_proxy_variables):
    with _serve() as port, _serve(_ProxyHandler) as proxy_port:
        llm, block = _llm("openai", "127.0.0.1", port), NetworkProxy(http_proxy=f"http://127.0.0.1:{proxy_port}", no_proxy="")
        entered, leave = threading.Event(), threading.Event()

        def hold():
            with block:
                entered.set()
                assert leave.wait(60)

        async def chats():
            inside, done = asyncio.Event(), asyncio.Event()

            async def hold_in_a_task():
                with block:
                    inside.set()
                    await done.wait()

            holder = asyncio.create_task(hold_in_a_task())
            await inside.wait()
            answer = await llm.achat("hi")
            done.set()
            await holder
            return answer

        thread = threading.Thread(target=hold)
        thread.start()
        try:
            assert entered.wait(60)
            with block:  # the same instance, entered and left on this thread while the other one is inside it
                pass
            assert llm.chat("hi") == "ok"
        finally:
            leave.set()
            thread.join(60)
        assert asyncio.run(chats()) == "ok"


def test_the_environments_proxy_still_does_not_reach_a_loopback_server(no_proxy_variables, monkeypatch):
    with _serve() as port, _serve(_ProxyHandler) as proxy_port:
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
        assert _llm("openai", "127.0.0.1", port).chat("hi") == "ok"
