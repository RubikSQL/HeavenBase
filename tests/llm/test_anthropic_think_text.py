"""A streamed Anthropic thinking block's signature is not thinking text.

Adaptive and manual thinking end every streamed thinking block with a signature_delta event.
The signature is opaque verification data, so think, content and the gathered message carry
only the thinking text, exactly as the non-streamed response does.
"""

from __future__ import annotations

import pytest

import heavenbase as hb
from heavenbase.llm.cache import LLMCache
from heavenbase.llm.config import LLM_ENGINE
from heavenbase.llm.gateways._protocols import _anthropic_stream
from heavenbase.llm.gateways.anthropic import runtime as anthropic_gateway

pytestmark = pytest.mark.fast

SIGNATURE = "EqQBCgIYAhIM1gbcDa9GJwZA2b3hGgxBdjrkzLoky3dl1pkiMOYds"
MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku",
    "content": [{"type": "thinking", "thinking": "plan", "signature": SIGNATURE}, {"type": "text", "text": "answer"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 4, "output_tokens": 5},
}
EVENTS = [
    {"type": "message_start", "message": {"id": "msg_1", "model": "claude-haiku", "usage": {"input_tokens": 4}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "pl"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "an"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": SIGNATURE}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "answer"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
    {"type": "message_stop"},
]


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    cache = LLMCache.from_dict({"enabled": False, "backend": {"main": {"type": "inmem"}}})
    monkeypatch.setattr(LLMCache, "current", classmethod(lambda cls, config_manager=None: cache))


@pytest.fixture
def llm(monkeypatch):
    class FakeMessages:
        def create(self, **payload):
            return list(EVENTS) if payload.get("stream") else dict(MESSAGE)

    class FakeClient:
        messages = FakeMessages()

    def client(spec, mode="anthropic"):
        return FakeClient(), LLM_ENGINE.apply(spec, profile=spec.gateway)

    monkeypatch.setattr(anthropic_gateway, "_anthropic_client", client)
    return lambda stream_chat: hb.LLM(model="sonnet-4.6", provider="anthropic", gateway="anthropic", max_tokens=64, stream_chat=stream_chat)


@pytest.mark.parametrize("stream_chat", [True, False])
def test_think_is_only_the_thinking_text(llm, stream_chat):
    result = llm(stream_chat).chat("hi", think=True, include=["text", "think", "content", "message"], reduce=False)

    assert result["text"] == "answer"
    assert result["think"] == "plan"
    assert SIGNATURE not in result["content"]
    assert result["message"]["reasoning_content"] == "plan"


def test_streamed_think_chunks_never_carry_the_signature(llm):
    chunks = list(llm(True).stream("hi", think=True, include=["think"], reduce=False))

    assert "".join(chunk["think"] for chunk in chunks) == "plan"


def test_converted_stream_reasoning_is_the_thinking_text():
    # Shared by the anthropic, portkey and bifrost gateways' Anthropic Messages routes.
    chunks = list(_anthropic_stream(EVENTS))

    assert "".join(chunk["choices"][0]["delta"].get("reasoning", "") for chunk in chunks) == "plan"
