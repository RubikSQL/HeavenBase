"""Streamed Anthropic Messages calls report the model that served them, as non-streamed calls do."""

from __future__ import annotations

import asyncio

import pytest

import heavenbase as hb
from heavenbase.llm.config import LLM_ENGINE
from heavenbase.llm.gateways._protocols import _anthropic_astream, _anthropic_stream
from heavenbase.llm.gateways.anthropic import runtime as anthropic_gateway

pytestmark = pytest.mark.fast

SERVED = "claude-haiku-4-5-20251001"

# Event bodies as the Anthropic streaming documentation shows them; only message_start names the model.
EVENTS = [
    {
        "type": "message_start",
        "message": {
            "id": "msg_served",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": SERVED,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 25, "output_tokens": 1},
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 15}},
    {"type": "message_stop"},
]
MESSAGE = {
    "id": "msg_served",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "Hello"}],
    "model": SERVED,
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 25, "output_tokens": 15},
}


def _runtime(spec):
    return LLM_ENGINE.apply(spec, profile=spec.gateway)


def _use_client(monkeypatch):
    class FakeMessages:
        def create(self, **payload):
            return list(EVENTS) if payload.get("stream") else dict(MESSAGE)

    class FakeAsyncMessages:
        async def create(self, **payload):
            return list(EVENTS) if payload.get("stream") else dict(MESSAGE)

    class FakeClient:
        messages = FakeMessages()

    class FakeAsyncClient:
        messages = FakeAsyncMessages()

    monkeypatch.setattr(anthropic_gateway, "_anthropic_client", lambda spec, mode="anthropic": (FakeClient(), _runtime(spec)))
    monkeypatch.setattr(anthropic_gateway, "_anthropic_async_client", lambda spec, mode="anthropic": (FakeAsyncClient(), _runtime(spec)))


def _llm(**kwargs):
    return hb.LLM(model="haiku", provider="anthropic", gateway="anthropic", max_tokens=64, **kwargs)


def _served(events):
    responses = [event for event in events if event.kind == "response"]
    assert len(responses) == 1
    return responses[0].payload["served_model"]


def _last_chunk(raw):
    while isinstance(raw, list) and raw:
        raw = raw[-1]
    return raw


@pytest.mark.parametrize("stream_chat", [True, False])
def test_chat_trace_reports_the_served_model(monkeypatch, stream_chat):
    _use_client(monkeypatch)
    events = []

    result = _llm(stream_chat=stream_chat).chat("say hello", include=["text", "raw"], reduce=False, trace=events.append)

    assert result["text"] == "Hello"
    assert _served(events) == SERVED
    # The dashboard assistant reads the served model from the last raw response the same way.
    assert _last_chunk(result["raw"])["model"] == SERVED


def test_async_chat_trace_reports_the_served_model(monkeypatch):
    _use_client(monkeypatch)
    events = []

    result = asyncio.run(_llm().achat("say hello", include=["text", "raw"], reduce=False, trace=events.append))

    assert result["text"] == "Hello"
    assert _served(events) == SERVED
    assert _last_chunk(result["raw"])["model"] == SERVED


def test_every_converted_chunk_carries_the_message_id_and_model():
    # Shared by the anthropic, portkey and bifrost gateways' Anthropic Messages routes.
    chunks = list(_anthropic_stream(EVENTS))

    assert sum(chunk.get("usage", {}).get("completion_tokens", 0) for chunk in chunks) == 15
    assert all(chunk["model"] == SERVED and chunk["id"] == "msg_served" for chunk in chunks)


def test_async_converted_chunks_carry_the_message_id_and_model():
    async def collect():
        return [chunk async for chunk in _anthropic_astream(EVENTS)]

    chunks = asyncio.run(collect())

    assert sum(chunk.get("usage", {}).get("completion_tokens", 0) for chunk in chunks) == 15
    assert all(chunk["model"] == SERVED and chunk["id"] == "msg_served" for chunk in chunks)


def test_stream_without_usage_still_ends_on_a_chunk_with_the_model():
    events = [{**event, "message": {**event["message"], "usage": {}}} if event["type"] == "message_start" else event for event in EVENTS]
    events = [event for event in events if event["type"] != "message_delta"]

    chunks = list(_anthropic_stream(events))

    assert "usage" not in chunks[-1]
    assert chunks[-1]["model"] == SERVED
