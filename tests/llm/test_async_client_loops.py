"""Async SDK clients belong to the event loop that uses them."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import gc
import json
import sys
import threading
import time
import types
import warnings
import weakref

import pytest

import heavenbase as hb
from heavenbase.llm.gateways import _protocols

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


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.delay = 0.0
        self.open_connections = 0
        self.connections = 0


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, so an SDK pool reuses its connections
    server: _Server

    def setup(self) -> None:
        super().setup()
        with self.server.lock:
            self.server.open_connections += 1
            self.server.connections += 1

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            with self.server.lock:
                self.server.open_connections -= 1

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(self.server.delay)
        reply = _ANTHROPIC_REPLY if self.path.rstrip("/").endswith("/messages") else _OPENAI_REPLY
        body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _serve():
    server = _Server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _openai_llm(port: int) -> hb.LLM:
    return hb.LLM(preset="custom", model="m1", base_url=f"http://127.0.0.1:{port}/v1", api_key="sk-test", cache=False, stream_chat=False)


def _anthropic_llm(port: int) -> hb.LLM:
    return hb.LLM(
        model="haiku", provider="anthropic", gateway="anthropic", base_url=f"http://127.0.0.1:{port}", api_key="sk-test", cache=False, stream_chat=False
    )


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(autouse=True)
def _fresh_clients():
    _protocols.dispose_llm_clients()
    yield
    _protocols.dispose_llm_clients()


@pytest.fixture
def loops_before():
    """Return the loop records other tests left behind."""
    return set(_protocols._LOOP_RECORDS)


@pytest.mark.parametrize("build", [_openai_llm, _anthropic_llm], ids=["openai", "anthropic"])
def test_achat_works_in_every_new_event_loop_and_closes_its_connections(build):
    pytest.importorskip("openai" if build is _openai_llm else "anthropic")
    with _serve() as server:
        llm = build(server.server_address[1])
        outcomes, left_open = [], []
        for _ in range(3):
            try:
                outcomes.append(asyncio.run(llm.achat("hi")))
            except Exception as error:  # a client shared across loops raises RuntimeError('Event loop is closed')
                outcomes.append(repr(error))
            # The loop's client closed with the loop: no pooled connection outlives it.
            _wait_until(lambda: server.open_connections == 0)
            left_open.append(server.open_connections)

        assert outcomes == ["ok"] * 3
        assert left_open == [0] * 3


@pytest.mark.parametrize("build", [_openai_llm, _anthropic_llm], ids=["openai", "anthropic"])
def test_achat_works_in_concurrent_threads_that_each_run_their_own_loop(build):
    pytest.importorskip("openai" if build is _openai_llm else "anthropic")
    with _serve() as server:
        llm = build(server.server_address[1])

        async def burst() -> list:
            return await asyncio.gather(*(llm.achat("hi") for _ in range(4)))

        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: asyncio.run(burst()), range(32)))

        assert results == [["ok"] * 4] * 32
        assert _wait_until(lambda: server.open_connections == 0), server.open_connections


@pytest.mark.slow
def test_more_live_event_loops_than_the_cache_size_all_keep_working():
    pytest.importorskip("openai")
    threads = _protocols._CLIENT_CACHE_MAX + 8
    with _serve() as server:
        server.delay = 0.5  # every loop's request is still in flight when the last loop builds its client
        llm = _openai_llm(server.server_address[1])
        barrier = threading.Barrier(threads)

        def call() -> str:
            barrier.wait(10)
            try:
                return asyncio.run(llm.achat("hi"))
            except Exception as error:  # a cross-loop eviction closes a client that is still in use
                return repr(error)

        with ThreadPoolExecutor(threads) as pool:
            results = list(pool.map(lambda _: call(), range(threads)))

        assert results == ["ok"] * threads


@pytest.mark.parametrize("build", [_openai_llm, _anthropic_llm], ids=["openai", "anthropic"])
def test_a_loop_dropped_without_closing_is_collected_with_its_connections(build):
    pytest.importorskip("openai" if build is _openai_llm else "anthropic")
    with _serve() as server:
        llm = build(server.server_address[1])
        refs = []
        for _ in range(5):
            loop = asyncio.new_event_loop()
            assert loop.run_until_complete(llm.achat("hi")) == "ok"
            refs.append(weakref.ref(loop))
            del loop

        gc.collect()

        assert [ref() for ref in refs] == [None] * 5
        assert _wait_until(lambda: server.open_connections == 0), server.open_connections


@pytest.mark.parametrize("build", [_openai_llm, _anthropic_llm], ids=["openai", "anthropic"])
def test_clients_of_a_loop_closed_without_shutting_down_are_dropped_quietly(build):
    pytest.importorskip("openai" if build is _openai_llm else "anthropic")
    errors: list = []
    with _serve() as server:
        llm = build(server.server_address[1])
        for _ in range(3):
            loop = asyncio.new_event_loop()
            assert loop.run_until_complete(llm.achat("hi")) == "ok"
            loop.close()  # no shutdown_asyncgens: the clients cannot be closed on this loop

        async def next_loop() -> str:
            asyncio.get_running_loop().set_exception_handler(lambda _loop, context: errors.append(context.get("message")))
            result = await llm.achat("hi")  # drops the closed loops' clients
            gc.collect()
            await asyncio.sleep(0.05)
            gc.collect()
            return result

        assert asyncio.run(next_loop()) == "ok"
        assert errors == []  # nothing tried to close those clients on the running loop
        del loop
        gc.collect()
        assert _wait_until(lambda: server.open_connections == 0), server.open_connections


@pytest.mark.parametrize("keeper_first", [True, False], ids=["keeper-first", "generator-first"])
@pytest.mark.parametrize("build", [_openai_llm, _anthropic_llm], ids=["openai", "anthropic"])
def test_a_call_from_a_generator_finalized_at_shutdown_succeeds_and_leaves_nothing_open(build, keeper_first):
    pytest.importorskip("openai" if build is _openai_llm else "anthropic")
    with _serve() as server:
        server.delay = 0.05
        llm = build(server.server_address[1])
        suspended: list = []
        late: list = []

        async def generator():
            try:
                yield
            finally:  # runs inside asyncio.run's shutdown_asyncgens
                try:
                    late.append(await llm.achat("late"))
                except Exception as error:
                    late.append(repr(error))

        async def main() -> None:
            if keeper_first:
                await llm.achat("warm")
            for _ in range(3):
                agen = generator()
                await agen.__anext__()
                suspended.append(agen)
            if not keeper_first:
                await llm.achat("warm")

        for _ in range(3):
            asyncio.run(main())
            assert _wait_until(lambda: server.open_connections == 0), server.open_connections
            suspended.clear()

        assert late == ["ok"] * 9


@pytest.mark.parametrize("build", [_openai_llm, _anthropic_llm], ids=["openai", "anthropic"])
def test_a_loops_first_call_from_a_generator_finalized_at_shutdown_leaves_nothing_open(build):
    pytest.importorskip("openai" if build is _openai_llm else "anthropic")
    with _serve() as server:
        llm = build(server.server_address[1])
        suspended: list = []
        late: list = []
        left_open: list = []

        async def generator():
            try:
                yield
            finally:  # the loop's first model call, made inside asyncio.run's shutdown_asyncgens
                try:
                    late.append(await llm.achat("late"))
                except Exception as error:
                    late.append(repr(error))

        async def main() -> None:
            agen = generator()
            await agen.__anext__()
            suspended.append(agen)

        for _ in range(3):
            asyncio.run(main())
            _wait_until(lambda: server.open_connections == 0)
            left_open.append(server.open_connections)
            suspended.clear()

        assert late == ["ok"] * 3
        assert left_open == [0] * 3


class _FakeAsyncClient:
    instances: list = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.closed_on = None
        _FakeAsyncClient.instances.append(self)

    async def close(self) -> None:
        self.closed_on = asyncio.get_running_loop()


@pytest.fixture
def fake_sdk(monkeypatch):
    _FakeAsyncClient.instances = []
    sdk = types.SimpleNamespace(OpenAI=_FakeAsyncClient, AsyncOpenAI=_FakeAsyncClient, Anthropic=_FakeAsyncClient, AsyncAnthropic=_FakeAsyncClient)
    monkeypatch.setitem(sys.modules, "openai", sdk)
    monkeypatch.setitem(sys.modules, "anthropic", sdk)
    return _FakeAsyncClient.instances


def _fake_spec(api_key: str = "sk-test"):
    return hb.LLM(preset="custom", model="m1", base_url="http://127.0.0.1:9/v1", api_key=api_key).spec


def _record(loop: asyncio.AbstractEventLoop):
    return getattr(loop, _protocols._LOOP_ATTRIBUTE)


def _cached(loop: asyncio.AbstractEventLoop) -> list:
    return [client for cache in _record(loop).caches.values() for client in cache.values()]


def test_one_loop_reuses_its_client_and_closes_it_on_that_loop(fake_sdk):
    spec = _fake_spec()

    async def use() -> tuple:
        first, _ = _protocols._openai_async_client(spec, spec.gateway)
        second, _ = _protocols._openai_async_client(spec, spec.gateway)
        return first, second, asyncio.get_running_loop()

    loop = asyncio.new_event_loop()
    try:
        first, second, owner = loop.run_until_complete(use())
        assert first is second
        assert first.closed_on is None
        loop.run_until_complete(loop.shutdown_asyncgens())
        assert first.closed_on is owner
        assert _cached(loop) == []
    finally:
        loop.close()
    other, _, _ = asyncio.run(use())
    assert other is not first
    assert len(fake_sdk) == 2
    assert _record(loop) not in _protocols._LOOP_RECORDS  # the next loop forgot the closed one


def test_an_async_client_requested_outside_a_loop_is_the_callers_own(fake_sdk, loops_before):
    llm = hb.LLM(preset="custom", model="m1", base_url="http://127.0.0.1:9/v1", api_key="sk-test")

    first = llm.to_aclient()
    second = llm.to_aclient()

    assert first is not second
    assert set(_protocols._LOOP_RECORDS) <= loops_before


def test_disposing_closes_an_async_client_on_the_loop_that_owns_it(fake_sdk):
    spec = _fake_spec()
    built = threading.Event()
    release = threading.Event()
    seen: dict = {}

    async def hold() -> None:
        seen["client"], _ = _protocols._openai_async_client(spec, spec.gateway)
        seen["loop"] = asyncio.get_running_loop()
        built.set()
        while not release.is_set():
            await asyncio.sleep(0.01)

    worker = threading.Thread(target=asyncio.run, args=(hold(),))
    worker.start()
    try:
        assert built.wait(5)
        _protocols.dispose_llm_clients()
        assert _wait_until(lambda: seen["client"].closed_on is not None)
        assert seen["client"].closed_on is seen["loop"]
    finally:
        release.set()
        worker.join(5)


def test_a_loop_closed_without_shutting_down_drops_its_clients(fake_sdk):
    spec = _fake_spec()

    async def use():
        return _protocols._openai_async_client(spec, spec.gateway)[0]

    loop = asyncio.new_event_loop()
    stale = loop.run_until_complete(use())
    loop.close()  # no shutdown_asyncgens: nothing ran on this loop to close the client

    fresh = asyncio.run(use())

    assert fresh is not stale
    assert stale.closed_on is None
    assert _cached(loop) == []
    assert _record(loop) not in _protocols._LOOP_RECORDS


def test_every_live_loop_keeps_its_client_beyond_the_cache_size(fake_sdk):
    spec = _fake_spec()
    threads = _protocols._CLIENT_CACHE_MAX + 8
    barrier = threading.Barrier(threads)

    async def hold() -> bool:
        first, _ = _protocols._openai_async_client(spec, spec.gateway)
        barrier.wait(10)  # every loop is alive and holds its client at once
        await asyncio.sleep(0.01)
        second, _ = _protocols._openai_async_client(spec, spec.gateway)
        return first is second and first.closed_on is None

    with ThreadPoolExecutor(threads) as pool:
        kept = list(pool.map(lambda _: asyncio.run(hold()), range(threads)))

    assert kept == [True] * threads
    assert len(fake_sdk) == threads


def test_a_loop_dropped_without_closing_is_not_kept_alive(fake_sdk):
    spec = _fake_spec()

    async def use():
        return _protocols._openai_async_client(spec, spec.gateway)[0]

    loop = asyncio.new_event_loop()
    loop.run_until_complete(use())
    ref = weakref.ref(loop)
    del loop
    gc.collect()

    assert ref() is None


def test_a_loop_dropped_without_closing_schedules_no_close_on_itself(fake_sdk):
    spec = _fake_spec()
    scheduled: list = []

    class _Loop(asyncio.SelectorEventLoop):
        def _asyncgen_finalizer_hook(self, agen) -> None:
            scheduled.append(agen.__qualname__)
            super()._asyncgen_finalizer_hook(agen)

    async def use():
        return _protocols._openai_async_client(spec, spec.gateway)[0]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(3):
            loop = _Loop()
            loop.run_until_complete(use())
            del loop
            gc.collect()

    # Collected with its loop, the keeper closes directly instead of scheduling a
    # close that the dropped loop never runs (Python 3.13 warns about that).
    assert scheduled == []
    assert [str(caught_warning.message) for caught_warning in caught if issubclass(caught_warning.category, RuntimeWarning)] == []


@pytest.mark.slow
def test_one_loop_evicts_its_least_recently_used_client_on_that_loop(fake_sdk):
    specs = [_fake_spec(f"sk-{index}") for index in range(_protocols._CLIENT_CACHE_MAX + 1)]

    async def use() -> tuple:
        clients = [_protocols._openai_async_client(spec, spec.gateway)[0] for spec in specs]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        return clients[0].closed_on is loop, [client.closed_on for client in clients[1:]], clients, loop

    evicted_here, others_before, clients, loop = asyncio.run(use())

    assert evicted_here
    assert others_before == [None] * _protocols._CLIENT_CACHE_MAX
    assert [client.closed_on for client in clients[1:]] == [loop] * _protocols._CLIENT_CACHE_MAX  # closed at shutdown


def test_sync_clients_stay_shared_across_threads(fake_sdk):
    spec = _fake_spec()

    with ThreadPoolExecutor(4) as pool:
        clients = list(pool.map(lambda _: _protocols._openai_client(spec, spec.gateway)[0], range(8)))

    assert len({id(client) for client in clients}) == 1
