import asyncio

import pytest

from heavenbase import LLM

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("wrap_tasks", [False, True])
@pytest.mark.parametrize("loop_type", ["asyncio", "uvloop"])
def test_sdk_client_survives_already_scheduled_generator_cleanup(monkeypatch, wrap_tasks, loop_type):
    new_loop = asyncio.new_event_loop if loop_type == "asyncio" else pytest.importorskip("uvloop").new_event_loop
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    default_http_client = openai.DefaultAsyncHttpxClient

    def http_client(**kwargs):
        def models(request):
            assert request.url.path == "/v1/models"
            return httpx.Response(200, json={"object": "list", "data": []})

        return default_http_client(transport=httpx.MockTransport(models), **kwargs)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", http_client)

    def task_factory(loop, coroutine, **kwargs):
        async def run():
            return await coroutine

        return asyncio.Task(run(), loop=loop, **kwargs)

    async def main():
        client = LLM(preset="custom", model="m1", base_url="http://127.0.0.1:9/v1", api_key="sk-test", cache=False).to_aclient()
        entered, resume, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        still_open = []

        async def events():
            try:
                yield
            finally:
                entered.set()
                await resume.wait()
                still_open.append(not client.is_closed())
                if still_open[-1]:
                    await client.models.list()
                done.set()

        generator = events()
        await anext(generator)
        del generator
        await asyncio.wait_for(entered.wait(), 10)
        shutdown = asyncio.create_task(asyncio.get_running_loop().shutdown_asyncgens())
        await asyncio.sleep(0)
        resume.set()
        await asyncio.wait_for(done.wait(), 10)
        await asyncio.wait_for(shutdown, 10)
        assert still_open == [True]
        assert client.is_closed()

    loop = new_loop()
    try:
        if wrap_tasks:
            loop.set_task_factory(task_factory)
        loop.run_until_complete(main())
    finally:
        loop.close()
