"""A LiteLLM-routed Anthropic thinking tool loop sends the signed thinking block back, streamed or not.

The streamed chunks come from LiteLLM's own Anthropic stream parser fed ordinary documented events.
LiteLLM splits each block into per-delta thinking_blocks fragments: text fragments with an empty
signature, then a fragment with only the signature.
"""

from __future__ import annotations

import pytest

import heavenbase as hb
from heavenbase.llm.cache import LLMCache

litellm = pytest.importorskip("litellm")
handler = pytest.importorskip("litellm.llms.anthropic.chat.handler")

pytestmark = pytest.mark.fast

SIGNATURE = "EqQBCgIYAhIM1gbcDa9GJwZA2b3hGgxBdjrkzLoky3dl1pkiMOYds"
THINKING = {"type": "thinking", "thinking": "plan", "signature": SIGNATURE}


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    cache = LLMCache.from_dict({"enabled": False, "backend": {"main": {"type": "inmem"}}})
    monkeypatch.setattr(LLMCache, "current", classmethod(lambda cls, config_manager=None: cache))


def add(left: int, right: int) -> int:
    return left + right


def _events(tool):
    start = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": [],
        "usage": {"input_tokens": 4, "output_tokens": 1},
    }
    events = [{"type": "message_start", "message": start}]
    if tool:
        events += [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "pl"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "an"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": SIGNATURE}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "add", "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"left":2,"right":3}'}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
        ]
    else:
        events += [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "sum=5"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
        ]
    events.append({"type": "message_stop"})
    return events


def _chunks(tool):
    parser = handler.ModelResponseIterator(streaming_response=iter([]), sync_stream=True, json_mode=False)
    return [parser.chunk_parser(event) for event in _events(tool)]


def _has_tool_message(messages):
    return any(m.get("role") == "tool" for m in messages)


def _round_two_assistant(calls):
    assert len(calls) == 2
    return [m for m in calls[1]["messages"] if m.get("role") == "assistant"][-1]


def test_streamed_tool_loop_sends_the_joined_thinking_block_back(monkeypatch):
    calls = []

    def completion(**payload):
        calls.append(payload)
        return iter(_chunks(tool=not _has_tool_message(payload["messages"])))

    monkeypatch.setattr(litellm, "completion", completion)
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="litellm", max_tokens=2048, stream_chat=True)

    result = llm.chat("add 2 and 3", think=True, tools=[add], include=["text", "think"], reduce=False)

    assert result == {"text": "sum=5", "think": "plan"}
    assert _round_two_assistant(calls).get("thinking_blocks") == [THINKING]


def test_non_streamed_tool_loop_sends_the_thinking_block_back(monkeypatch):
    from litellm.types.utils import ChatCompletionMessageToolCall, Choices, Function, Message, ModelResponse

    calls = []

    def completion(**payload):
        calls.append(payload)
        if _has_tool_message(payload["messages"]):
            message = Message(content="sum=5", role="assistant")
            return ModelResponse(model="claude-haiku-4-5", choices=[Choices(index=0, message=message, finish_reason="stop")])
        call = ChatCompletionMessageToolCall(id="toolu_1", type="function", function=Function(name="add", arguments='{"left":2,"right":3}'))
        message = Message(content=None, role="assistant", tool_calls=[call], reasoning_content="plan", thinking_blocks=[dict(THINKING)])
        return ModelResponse(model="claude-haiku-4-5", choices=[Choices(index=0, message=message, finish_reason="tool_calls")])

    monkeypatch.setattr(litellm, "completion", completion)
    llm = hb.LLM(model="haiku", provider="anthropic", gateway="litellm", max_tokens=2048, stream_chat=False)

    result = llm.chat("add 2 and 3", think=True, tools=[add], include="text")

    assert result == "sum=5"
    assert _round_two_assistant(calls).get("thinking_blocks") == [THINKING]
