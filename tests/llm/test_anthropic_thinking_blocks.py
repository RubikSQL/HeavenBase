"""Anthropic thinking blocks are kept on the assistant message and sent back in tool loops.

The streamed events and message bodies follow the Anthropic extended-thinking documentation: a
thinking block ends with a signature_delta, and with thinking on, the assistant turn that holds
tool_use must be sent back starting with its thinking blocks, unmodified. The fakes record the
payloads; they do not enforce the API rule themselves.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

import heavenbase as hb
from heavenbase.llm.cache import LLMCache
from heavenbase.llm.config import LLM_ENGINE
from heavenbase.llm.gateways._protocols import _anthropic_payload, _anthropic_stream, _join_thinking_blocks
from heavenbase.llm.gateways.anthropic import runtime as anthropic_gateway

pytestmark = pytest.mark.fast

SIGNATURE = "EqQBCgIYAhIM1gbcDa9GJwZA2b3hGgxBdjrkzLoky3dl1pkiMOYds"
REDACTED = "EmwKAhgBEgy3va3pzix/LafPsn4aDFIT2Xlxh0L5L8rLVyIwxtE3rAFBa8cr3qpP"
THINKING = {"type": "thinking", "thinking": "plan", "signature": SIGNATURE}


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    cache = LLMCache.from_dict({"enabled": False, "backend": {"main": {"type": "inmem"}}})
    monkeypatch.setattr(LLMCache, "current", classmethod(lambda cls, config_manager=None: cache))


def add(left: int, right: int) -> int:
    return left + right


def _round(stream, text, tool=False, redacted=False):
    blocks = [dict(THINKING)]
    if redacted:
        blocks.append({"type": "redacted_thinking", "data": REDACTED})
    if text:
        blocks.append({"type": "text", "text": text})
    if tool:
        blocks.append({"type": "tool_use", "id": "toolu_1", "name": "add", "input": {"left": 2, "right": 3}})
    stop = "tool_use" if tool else "end_turn"
    if not stream:
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku",
            "content": blocks,
            "stop_reason": stop,
            "usage": {"input_tokens": 4, "output_tokens": 5},
        }
    events = [{"type": "message_start", "message": {"id": "msg_1", "model": "claude-haiku", "usage": {"input_tokens": 4}}}]
    events += [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "pl"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "an"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": SIGNATURE}},
        {"type": "content_block_stop", "index": 0},
    ]
    index = 1
    if redacted:
        events += [
            {"type": "content_block_start", "index": index, "content_block": {"type": "redacted_thinking", "data": REDACTED}},
            {"type": "content_block_stop", "index": index},
        ]
        index += 1
    if text:
        events += [
            {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": index},
        ]
        index += 1
    if tool:
        events += [
            {"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "add", "input": {}}},
            {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": '{"left":2,"right":3}'}},
            {"type": "content_block_stop", "index": index},
        ]
    events += [{"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 5}}, {"type": "message_stop"}]
    return events


def _has_tool_result(payload):
    return any(isinstance(m.get("content"), list) and any(b.get("type") == "tool_result" for b in m["content"]) for m in payload["messages"])


def _reply(payload, tool_loop, redacted):
    stream = bool(payload.get("stream"))
    if not tool_loop:
        return _round(stream, "answer", redacted=redacted)
    return _round(stream, "sum=5") if _has_tool_result(payload) else _round(stream, "", tool=True, redacted=redacted)


def _install(monkeypatch, payloads, tool_loop=False, redacted=False):
    class FakeMessages:
        def create(self, **payload):
            payloads.append(payload)
            return _reply(payload, tool_loop, redacted)

    class FakeAsyncMessages:
        async def create(self, **payload):
            payloads.append(payload)
            return _reply(payload, tool_loop, redacted)

    class FakeClient:
        messages = FakeMessages()

    class FakeAsyncClient:
        messages = FakeAsyncMessages()

    def runtime(spec):
        return LLM_ENGINE.apply(spec, profile=spec.gateway)

    monkeypatch.setattr(anthropic_gateway, "_anthropic_client", lambda spec, mode="anthropic": (FakeClient(), runtime(spec)))
    monkeypatch.setattr(anthropic_gateway, "_anthropic_async_client", lambda spec, mode="anthropic": (FakeAsyncClient(), runtime(spec)))


def _llm(stream_chat):
    return hb.LLM(model="sonnet-4.6", provider="anthropic", gateway="anthropic", max_tokens=64, stream_chat=stream_chat)


def _round_two_assistant(payloads):
    assert len(payloads) == 2
    return [m for m in payloads[1]["messages"] if m["role"] == "assistant"][-1]


@pytest.mark.parametrize("stream_chat", [True, False])
def test_message_keeps_the_signed_thinking_blocks(monkeypatch, stream_chat):
    payloads = []
    _install(monkeypatch, payloads, redacted=True)

    result = _llm(stream_chat).chat("hi", think=True, include=["text", "think", "message"], reduce=False)

    assert result["text"] == "answer"
    assert result["think"] == "plan"
    assert result["message"]["thinking_blocks"] == [THINKING, {"type": "redacted_thinking", "data": REDACTED}]


@pytest.mark.parametrize("stream_chat", [True, False])
def test_tool_loop_sends_the_thinking_blocks_back_first(monkeypatch, stream_chat):
    payloads = []
    _install(monkeypatch, payloads, tool_loop=True, redacted=True)

    result = _llm(stream_chat).chat("add 2 and 3", think=True, include=["text", "think"], reduce=False, tools=[add])

    assert result["text"] == "sum=5"
    assert payloads[1]["thinking"]["type"] == "adaptive"
    content = _round_two_assistant(payloads)["content"]
    assert content[:2] == [THINKING, {"type": "redacted_thinking", "data": REDACTED}]
    assert content[2] == {"type": "tool_use", "id": "toolu_1", "name": "add", "input": {"left": 2, "right": 3}}


@pytest.mark.parametrize("stream_chat", [True, False])
def test_async_tool_loop_sends_the_thinking_block_back_first(monkeypatch, stream_chat):
    payloads = []
    _install(monkeypatch, payloads, tool_loop=True)

    result = asyncio.run(_llm(stream_chat).achat("add 2 and 3", think=True, include="text", tools=[add]))

    assert result == "sum=5"
    assert _round_two_assistant(payloads)["content"][0] == THINKING


def test_streamed_tool_loop_sends_the_thinking_block_back_first(monkeypatch):
    payloads = []
    _install(monkeypatch, payloads, tool_loop=True)

    texts = [chunk for chunk in _llm(True).stream("add 2 and 3", think=True, include="text", tools=[add])]

    assert "".join(texts) == "sum=5"
    assert _round_two_assistant(payloads)["content"][0] == THINKING


def test_continued_conversation_sends_the_blocks_back_only_with_thinking_on():
    spec = hb.LLM(model="sonnet-4.6", provider="anthropic", gateway="anthropic", max_tokens=64).spec
    runtime = LLM_ENGINE.apply(spec, profile=spec.gateway)
    call = {"id": "toolu_1", "type": "function", "function": {"name": "add", "arguments": '{"left":2,"right":3}'}}
    history = [
        {"role": "user", "content": "add 2 and 3"},
        {"role": "assistant", "content": "", "reasoning_content": "plan", "thinking_blocks": [THINKING], "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "5"},
    ]
    use = {"type": "tool_use", "id": "toolu_1", "name": "add", "input": {"left": 2, "right": 3}}

    on = _anthropic_payload(runtime, spec, history, think=True)
    off = _anthropic_payload(runtime, spec, history, think=False)

    assert on["messages"][1]["content"] == [THINKING, use]
    # Without thinking the request stays exactly as before: no thinking blocks are sent.
    assert off["messages"][1]["content"] == [use]


def test_think_false_drops_the_thinking_blocks(monkeypatch):
    payloads = []
    _install(monkeypatch, payloads)

    message = _llm(True).chat("hi", think=False, include="message")

    assert "thinking_blocks" not in message and "reasoning_content" not in message


def test_converted_stream_fragments_join_to_the_original_blocks():
    # Shared by the anthropic, portkey and bifrost gateways' Anthropic Messages routes.
    events = _round(True, "answer", redacted=True)

    fragments = [block for chunk in _anthropic_stream(events) for block in chunk["choices"][0]["delta"].get("thinking_blocks", [])]

    assert _join_thinking_blocks(fragments) == [THINKING, {"type": "redacted_thinking", "data": REDACTED}]


def test_join_puts_fragments_back_together_in_order():
    fragments = [
        {"type": "thinking", "thinking": "pl", "signature": ""},
        {"type": "thinking", "thinking": "an", "signature": ""},
        {"type": "thinking", "thinking": "", "signature": SIGNATURE},
        {"type": "thinking", "thinking": "second", "signature": "sig-2"},
        {"type": "thinking", "thinking": "unsigned", "signature": ""},
    ]

    # The unsigned last block is incomplete and is never sent back.
    assert _join_thinking_blocks(fragments) == [THINKING, {"type": "thinking", "thinking": "second", "signature": "sig-2"}]


def test_a_signed_block_with_omitted_text_is_complete():
    # Claude returns thinking display "omitted" blocks with empty text and the signature.
    assert _join_thinking_blocks([{"type": "thinking", "thinking": "", "signature": SIGNATURE}]) == [
        {"type": "thinking", "thinking": "", "signature": SIGNATURE}
    ]


def test_stream_callers_never_see_fragment_only_chunks(monkeypatch):
    payloads = []
    _install(monkeypatch, payloads)

    fields = ["text", "think", "tool_calls", "usage"]
    chunks = list(_llm(True).stream("hi", think=True, include=fields, reduce=False))

    assert all(any(chunk[field] for field in fields) for chunk in chunks)
    assert "".join(chunk["think"] for chunk in chunks) == "plan"


def test_stream_conversion_leaves_the_events_unchanged():
    events = _round(True, "answer", redacted=True)
    before = deepcopy(events)

    list(_anthropic_stream(events))

    assert events == before
