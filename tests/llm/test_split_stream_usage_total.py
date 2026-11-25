"""A streamed response whose counts arrive in several usage chunks gets the total of their sum."""

from __future__ import annotations

import pytest

import heavenbase as hb
from heavenbase.llm.base import _merge_usage

pytestmark = pytest.mark.fast

# The first chunk reports both counts (completion still 0); a later chunk adds the completion.
CHUNK_USAGE = [{"prompt_tokens": 10, "completion_tokens": 0}, {"completion_tokens": 5}]
EXPECTED = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_split_usage_chunks_merge_to_the_total_of_their_sum():
    assert _merge_usage(CHUNK_USAGE) == EXPECTED


def test_reported_totals_and_rounds_without_one_both_count():
    rounds = [{"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, {"prompt_tokens": 30, "completion_tokens": 4}]

    assert _merge_usage(rounds) == {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49}
    assert _merge_usage([{"total_tokens": 7}, {"prompt_tokens": 2, "completion_tokens": 1}])["total_tokens"] == 10


@pytest.mark.parametrize("stream_chat", [True, False])
def test_chat_usage_of_a_split_usage_stream_matches_the_unstreamed_call(stream_chat):
    def mock_chat(spec, messages, stream=False, **kwargs):
        if not stream:
            return {"choices": [{"message": {"role": "assistant", "content": "hello"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        return iter(
            [
                {"choices": [{"delta": {"role": "assistant", "content": "hel"}}], "usage": dict(CHUNK_USAGE[0])},
                {"choices": [{"delta": {"content": "lo"}}], "usage": dict(CHUNK_USAGE[1])},
            ]
        )

    llm = hb.LLM(preset="mock", mock_chat=mock_chat, mock_pause=(0, 0), cache=False, stream_chat=stream_chat)
    result = llm.chat("say hello", include=["text", "usage"], reduce=False)

    assert result["text"] == "hello"
    assert result["usage"] == EXPECTED
