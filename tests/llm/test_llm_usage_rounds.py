"""Usage of a multi-round tool call is the same whether the rounds were streamed or not."""

from __future__ import annotations

import asyncio

import pytest

import heavenbase as hb

pytestmark = pytest.mark.fast

# Round one reports a total; round two reports only the prompt and completion counts.
ROUND_USAGE = [
    {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    {"prompt_tokens": 30, "completion_tokens": 4},
]
EXPECTED = {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49}


def add(left: int, right: int) -> int:
    return left + right


def mock_chat(spec, messages, stream=False, **kwargs):
    second = any(message.get("role") == "tool" for message in messages)
    usage = dict(ROUND_USAGE[1 if second else 0])
    if second:
        message = {"role": "assistant", "content": "sum=5"}
    else:
        call = {"id": "c1", "type": "function", "function": {"name": "add", "arguments": '{"left":2,"right":3}'}}
        message = {"role": "assistant", "content": "", "tool_calls": [call]}
    if not stream:
        return {"choices": [{"message": message}], "usage": usage}
    delta = dict(message)
    if "tool_calls" in delta:
        delta["tool_calls"] = [{"index": 0, **delta["tool_calls"][0]}]
    return iter([{"choices": [{"delta": delta}]}, {"choices": [{"delta": {}}], "usage": usage}])


def _llm(stream_chat):
    return hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False, stream_chat=stream_chat)


@pytest.mark.parametrize("stream_chat", [True, False])
def test_chat_usage_sums_each_rounds_total(stream_chat):
    result = _llm(stream_chat).chat("add 2 and 3", include=["text", "usage"], reduce=False, tools=[add])

    assert result["text"] == "sum=5"
    assert result["usage"] == EXPECTED


@pytest.mark.parametrize("stream_chat", [True, False])
def test_async_chat_usage_sums_each_rounds_total(stream_chat):
    result = asyncio.run(_llm(stream_chat).achat("add 2 and 3", include=["text", "usage"], reduce=False, tools=[add]))

    assert result["text"] == "sum=5"
    assert result["usage"] == EXPECTED


@pytest.mark.parametrize("stream_chat", [True, False])
def test_stream_rounds_report_the_same_usage_as_chat(stream_chat):
    rounds = []
    chunks = list(_llm(stream_chat).stream("add 2 and 3", include=["text", "usage"], reduce=False, tools=[add], on_round=rounds.append))

    assert "".join(chunk["text"] for chunk in chunks) == "sum=5"
    assert [response.usage.get("total_tokens") for response in rounds] == [15, 34]


@pytest.mark.parametrize("stream_chat", [True, False])
def test_single_round_usage_is_the_same_streamed_or_not(stream_chat):
    def plain_chat(spec, messages, stream=False, **kwargs):
        usage = dict(ROUND_USAGE[1])
        if not stream:
            return {"choices": [{"message": {"role": "assistant", "content": "hello"}}], "usage": usage}
        return iter([{"choices": [{"delta": {"role": "assistant", "content": "hello"}}]}, {"choices": [{"delta": {}}], "usage": usage}])

    llm = hb.LLM(preset="mock", mock_chat=plain_chat, mock_pause=(0, 0), cache=False, stream_chat=stream_chat)
    result = llm.chat("say hello", include=["text", "usage"], reduce=False)

    assert result["text"] == "hello"
    assert result["usage"] == {"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34}
