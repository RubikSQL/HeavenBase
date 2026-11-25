import asyncio

import pytest

from heavenbase import LLM

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("wrap_tasks", [False, True])
def test_retained_shutdown_method_closes_sdk_client(wrap_tasks):
    pytest.importorskip("openai")

    def task_factory(loop, coroutine, **kwargs):
        async def run():
            return await coroutine

        return asyncio.Task(run(), loop=loop, **kwargs)

    async def open_client():
        client = LLM(preset="custom", model="fixture", base_url="http://127.0.0.1:9/v1", api_key="fixture", cache=False).to_aclient()

        async def stream():
            try:
                yield
            finally:
                await asyncio.sleep(0)
                client_open_during_cleanup.append(not client.is_closed())

        generator = stream()
        await anext(generator)
        return client, generator

    client_open_during_cleanup = []
    loop = asyncio.new_event_loop()
    shutdown = loop.shutdown_asyncgens
    try:
        if wrap_tasks:
            loop.set_task_factory(task_factory)
        client, generator = loop.run_until_complete(open_client())
        try:
            loop.run_until_complete(asyncio.wait_for(shutdown(), timeout=10))
            assert client_open_during_cleanup == [True]
            assert client.is_closed()
        finally:
            loop.run_until_complete(generator.aclose())
            loop.run_until_complete(client.close())
    finally:
        loop.close()
