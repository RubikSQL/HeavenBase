from __future__ import annotations

import asyncio

import pytest

import heavenbase as hb
from heavenbase.llm.config import LLM_ENGINE
from heavenbase.llm.gateways.anthropic import runtime as anthropic_gateway

pytestmark = pytest.mark.fast

# Event bodies as the Anthropic streaming documentation shows them: message_start carries the input
# count and an initial output count; message_delta carries the cumulative output count for the message.
MESSAGE_START = {
    "type": "message_start",
    "message": {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": "claude-haiku-4-5",
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 25, "output_tokens": 1},
    },
}
TEXT_EVENTS = [
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
    {"type": "content_block_stop", "index": 0},
]
MESSAGE_DELTA = {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 15}}
MESSAGE_STOP = {"type": "message_stop"}


def _documented_events(delta=MESSAGE_DELTA):
    return [MESSAGE_START, *TEXT_EVENTS, delta, MESSAGE_STOP]


def _runtime(spec, mode="anthropic"):
    return LLM_ENGINE.apply(spec, profile=spec.gateway)


def _use_events(monkeypatch, events):
    class FakeMessages:
        def create(self, **payload):
            assert payload["stream"] is True
            return list(events)

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(anthropic_gateway, "_anthropic_client", lambda spec, mode="anthropic": (FakeClient(), _runtime(spec, mode)))


def _usage(prompt, **kwargs):
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="anthropic", max_tokens=64)
    return llm.chat(prompt, include=["text", "usage"], reduce=False, **kwargs)


def _assert_counts(usage, prompt, completion):
    assert usage["input_tokens"] == prompt
    assert usage["output_tokens"] == completion
    assert usage["prompt_tokens"] == prompt
    assert usage["completion_tokens"] == completion
    assert usage["total_tokens"] == prompt + completion


def test_cumulative_output_count_is_not_added_to_the_start_count(monkeypatch):
    _use_events(monkeypatch, _documented_events())

    result = _usage("count the documented stream")

    assert result["text"] == "Hello"
    assert result["usage"] == {"input_tokens": 25, "output_tokens": 15, "prompt_tokens": 25, "completion_tokens": 15, "total_tokens": 40}


def test_cumulative_input_count_in_message_delta_is_not_added_twice(monkeypatch):
    delta = {**MESSAGE_DELTA, "usage": {"input_tokens": 25, "output_tokens": 15, "cache_read_input_tokens": 3}}
    _use_events(monkeypatch, _documented_events(delta))

    usage = _usage("count the stream with cumulative input")["usage"]

    _assert_counts(usage, 25, 15)
    assert usage["cache_read_input_tokens"] == 3


def test_streamed_usage_from_sdk_event_objects(monkeypatch):
    anthropic_types = pytest.importorskip("anthropic.types")
    from pydantic import TypeAdapter

    adapter = TypeAdapter(anthropic_types.RawMessageStreamEvent)
    _use_events(monkeypatch, [adapter.validate_python(event) for event in _documented_events()])

    result = _usage("count the sdk stream")

    assert result["text"] == "Hello"
    _assert_counts(result["usage"], 25, 15)


def test_non_streamed_sdk_usage_with_null_counts():
    anthropic_types = pytest.importorskip("anthropic.types")
    from heavenbase.llm.gateways._protocols import _anthropic_usage

    usage = _anthropic_usage(anthropic_types.MessageDeltaUsage.model_validate({"output_tokens": 15}).model_dump())

    assert usage["completion_tokens"] == 15
    assert "total_tokens" not in usage
    assert usage.get("prompt_tokens") is None


def test_async_stream_reports_the_final_usage_once(monkeypatch):
    class FakeMessages:
        async def create(self, **payload):
            assert payload["stream"] is True
            return _documented_events()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(anthropic_gateway, "_anthropic_async_client", lambda spec, mode="anthropic": (FakeClient(), _runtime(spec, mode)))
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="anthropic", max_tokens=64)

    result = asyncio.run(llm.achat("count the async stream", include=["text", "usage"], reduce=False))

    assert result["text"] == "Hello"
    _assert_counts(result["usage"], 25, 15)


@pytest.mark.parametrize("early_error", [False, True])
def test_usage_is_available_when_an_anthropic_stream_ends_early(monkeypatch, early_error):
    from anthropic import APIError
    from httpx import Request

    class FakeMessages:
        def create(self, **payload):
            yield MESSAGE_START
            yield from TEXT_EVENTS
            raise APIError("Overloaded", request=Request("POST", "https://api.anthropic.com/v1/messages"), body=None)

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(anthropic_gateway, "_anthropic_client", lambda spec, mode="anthropic": (FakeClient(), _runtime(spec, mode)))
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="anthropic", max_tokens=64, cache=False)
    stream = llm.stream("count the interrupted stream", include=["text", "usage"], reduce=False)

    chunks = []
    try:
        for chunk in stream:
            chunks.append(chunk)
            if chunk["text"] and not early_error:
                break
    except APIError:
        assert early_error
    else:
        assert not early_error
    finally:
        stream.close()

    assert "".join(chunk["text"] for chunk in chunks) == "Hello"
    _assert_counts(next((chunk["usage"] for chunk in chunks if chunk["usage"]), {}), 25, 1)


def test_text_only_stream_starts_with_text(monkeypatch):
    _use_events(monkeypatch, _documented_events())
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="anthropic", max_tokens=64, cache=False)
    stream = llm.stream("answer without usage")
    try:
        assert next(stream) == "Hello"
    finally:
        stream.close()


def test_async_usage_is_available_before_the_stream_finishes(monkeypatch):
    class FakeMessages:
        async def create(self, **payload):
            return _documented_events()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(anthropic_gateway, "_anthropic_async_client", lambda spec, mode="anthropic": (FakeClient(), _runtime(spec, mode)))
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="anthropic", max_tokens=64, cache=False)

    async def first_usage():
        stream = llm.astream("report usage as soon as possible", include="usage")
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    _assert_counts(asyncio.run(first_usage()), 25, 1)
