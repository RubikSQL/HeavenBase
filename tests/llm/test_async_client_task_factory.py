import asyncio

import pytest

from heavenbase import LLM
from heavenbase.llm.gateways import _protocols

pytestmark = pytest.mark.fast


def test_wrapping_task_factory_leaves_async_client_open_until_generator_cleanup(monkeypatch):
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    spec = LLM(preset="custom", model="m1", base_url="http://127.0.0.1:9/v1", api_key="sk-test", cache=False).spec
    original_http_client = openai.DefaultAsyncHttpxClient
    completed = []
    clients = []
    suspended = []

    def models(request):
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"object": "list", "data": [{"id": "m1", "object": "model", "created": 0, "owned_by": "local"}]})

    def http_client(**kwargs):
        return original_http_client(transport=httpx.MockTransport(models), **kwargs)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", http_client)

    def task_factory(loop, coroutine, **kwargs):
        async def run():
            await asyncio.sleep(0)
            return await coroutine

        return asyncio.Task(run(), loop=loop, **kwargs)

    async def generator(client):
        try:
            yield
        finally:
            await asyncio.sleep(0)
            completed.extend(model.id for model in (await client.models.list()).data)

    async def main():
        asyncio.get_running_loop().set_task_factory(task_factory)
        client, _ = _protocols._openai_async_client(spec, spec.gateway)
        clients.append(client)
        gen = generator(client)
        await gen.__anext__()
        suspended.append(gen)

    asyncio.run(main())

    assert completed == ["m1"]
    assert clients[0].is_closed()
