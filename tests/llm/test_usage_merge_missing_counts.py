import pytest

from heavenbase.extensions.agent.session import _merge_usage as merge_session_usage
from heavenbase.llm.base import _merge_usage

pytestmark = pytest.mark.fast


def test_a_count_after_a_missing_count_is_added():
    usage = _merge_usage([{"prompt_tokens": 3, "cached_tokens": None}, {"prompt_tokens": 2, "cached_tokens": 5}])
    assert usage == {"prompt_tokens": 5, "cached_tokens": 5}


def test_a_missing_count_keeps_the_count_before_it():
    assert _merge_usage([{"cached_tokens": 4}, {"cached_tokens": None}]) == {"cached_tokens": 4}


def test_total_tokens_needs_both_counts():
    assert _merge_usage([{"prompt_tokens": None, "completion_tokens": 2}]) == {"prompt_tokens": None, "completion_tokens": 2}
    assert _merge_usage([{"prompt_tokens": 1}, {"completion_tokens": 2}])["total_tokens"] == 3


def test_session_usage_adds_a_count_after_a_missing_count():
    assert merge_session_usage({"cached_tokens": None, "model": ""}, {"cached_tokens": 5, "model": "m"}) == {"cached_tokens": 5, "model": "m"}
    assert merge_session_usage({"cached_tokens": 4}, {"cached_tokens": None}) == {"cached_tokens": 4}
    assert merge_session_usage({"cached_tokens": 4}, {"cached_tokens": 1}) == {"cached_tokens": 5}
