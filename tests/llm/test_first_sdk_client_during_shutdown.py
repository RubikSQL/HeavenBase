"""First SDK use during asynchronous generator cleanup."""

import asyncio
from contextlib import aclosing
from functools import partial

import pytest

from heavenbase import LLM

pytestmark = pytest.mark.fast


@pytest.fixture
def client_limits(monkeypatch):
    openai = pytest.importorskip("openai")
    http_client = openai.DefaultAsyncHttpxClient
    limits = []

    def record_http_client(**kwargs):
        limits.append(kwargs.get("limits"))
        return http_client(**kwargs)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", record_http_client)
    return limits


def _wrapping_task_factory(loop, coroutine, *, eager=False, **kwargs):
    async def run():
        return await coroutine

    if eager:
        return asyncio.eager_task_factory(loop, run(), **kwargs)
    return asyncio.Task(run(), loop=loop, **kwargs)


@pytest.mark.slow
@pytest.mark.parametrize("loop_type", ["asyncio", "uvloop"])
@pytest.mark.parametrize("task_mode", ["default", "wrapped", "eager"])
@pytest.mark.parametrize("handle_missing_description", [False, True])
def test_first_sdk_client_during_generator_shutdown_keeps_no_idle_connections(client_limits, loop_type, task_mode, handle_missing_description):
    if task_mode == "eager" and not hasattr(asyncio, "eager_task_factory"):
        pytest.skip("Eager asyncio tasks require Python 3.12")
    new_loop = asyncio.new_event_loop if loop_type == "asyncio" else pytest.importorskip("uvloop").new_event_loop
    clients = []

    async def events():
        try:
            yield
        finally:
            llm = LLM(preset="custom", model="m1", base_url="http://127.0.0.1:9/v1", api_key="sk-test", cache=False)
            if handle_missing_description:
                event = {"title": "Meeting"}
                try:
                    event["description"]
                except KeyError:
                    clients.append(llm.to_aclient())
            else:
                clients.append(llm.to_aclient())

    async def main():
        generator = events()
        await anext(generator)
        return generator

    loop = new_loop()
    try:
        if task_mode != "default":
            loop.set_task_factory(partial(_wrapping_task_factory, eager=task_mode == "eager"))
        suspended = loop.run_until_complete(main())
        loop.run_until_complete(loop.shutdown_asyncgens())
        assert suspended.ag_frame is None
        assert len(clients) == 1
        assert len(client_limits) == 1
        assert client_limits[0] is not None and client_limits[0].max_keepalive_connections == 0
    finally:
        for client in clients:
            loop.run_until_complete(client.close())
        loop.close()


@pytest.mark.parametrize("schedule_close", [False, True])
def test_first_sdk_client_during_explicit_uvloop_close_reuses_connections(client_limits, schedule_close):
    uvloop = pytest.importorskip("uvloop")
    llm = LLM(preset="custom", model="m1", base_url="http://127.0.0.1:9/v1", api_key="sk-test", cache=False)
    clients = []

    async def events():
        try:
            yield
        finally:
            clients.append(llm.to_aclient())

    async def main():
        generator = events()
        if schedule_close:
            await anext(generator)
            await asyncio.create_task(generator.aclose())
        else:
            async with aclosing(generator):
                await anext(generator)
        assert generator.ag_frame is None
        assert llm.to_aclient() is clients[0]

    loop = uvloop.new_event_loop()
    try:
        loop.set_task_factory(_wrapping_task_factory)
        loop.run_until_complete(main())
        assert client_limits == [None]
        assert not clients[0].is_closed()
        loop.run_until_complete(loop.shutdown_asyncgens())
        assert clients[0].is_closed()
    finally:
        for client in clients:
            loop.run_until_complete(client.close())
        loop.close()
