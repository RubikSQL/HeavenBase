from __future__ import annotations

import pytest

import heavenbase.utils as utils
from heavenbase.utils.parsers import parse_keys, parse_md


def test_parser_utilities_are_exported_from_utils():
    assert utils.parse_keys is parse_keys
    assert utils.parse_md is parse_md


def test_parse_keys_discovers_multiline_and_unicode_values():
    response = "ignored preamble\nname: Ada\nnotes: first line\n  second line\n评分: 十分"

    assert parse_keys(response) == {
        "name": "Ada",
        "notes": "first line\n  second line",
        "评分": "十分",
    }


def test_parse_keys_honors_requested_keys_missing_values_and_duplicates():
    response = "status: first\nignored: no\nstatus: final"

    assert parse_keys(response, keys=["status", "missing"]) == {
        "status": "final",
        "missing": None,
    }
    assert parse_keys(response, keys=["status"], mode="list") == [
        {"key": "status", "value": "first\nignored: no"},
        {"key": "status", "value": "final"},
    ]


def test_parse_keys_handles_empty_input_and_rejects_unknown_mode():
    assert parse_keys("") == {}
    assert parse_keys("", keys=["answer"]) == {"answer": None}
    assert parse_keys("", mode="list") == []

    with pytest.raises(ValueError, match="mode"):
        parse_keys("answer: yes", mode="tuple")  # type: ignore[arg-type]


def test_parse_md_parses_tags_fences_and_plain_text():
    response = '<think>Hello!</think>\nVisible text.\n```json\n{"rating": 5}\n```'

    assert parse_md(response) == {
        "think": "Hello!",
        "text": "Visible text.",
        "json": '{"rating": 5}',
    }
    assert parse_md(response, mode="list") == [
        {"key": "think", "value": "Hello!"},
        {"key": "text", "value": "Visible text."},
        {"key": "json", "value": '{"rating": 5}'},
    ]


def test_parse_md_recurses_and_flattens_nested_blocks():
    response = "<result><score>10</score><note>great</note></result>"

    assert parse_md(response, recurse=True) == {
        "result.score.text": "10",
        "result.note.text": "great",
    }
    assert parse_md(response, recurse=True, mode="list") == [
        {
            "key": "result",
            "value": [
                {"key": "score", "value": [{"key": "text", "value": "10"}]},
                {"key": "note", "value": [{"key": "text", "value": "great"}]},
            ],
        }
    ]


def test_parse_md_preserves_nested_equal_name_tags():
    response = "<item>outer <item>inner</item> tail</item>"

    assert parse_md(response) == {
        "item": "outer <item>inner</item> tail",
    }
    assert parse_md(response, recurse=True) == {
        "item.text": "tail",
        "item.item.text": "inner",
    }


def test_parse_md_returns_best_effort_for_incomplete_streams():
    assert parse_md("<score>10") == {"score": "10"}
    assert parse_md("```python\nprint('ok')") == {
        "python": "print('ok')",
    }


def test_parse_md_keeps_duplicates_in_list_mode_and_rejects_unknown_mode():
    response = "<score>1</score><score>2</score>"

    assert parse_md(response) == {"score": "2"}
    assert parse_md(response, mode="list") == [
        {"key": "score", "value": "1"},
        {"key": "score", "value": "2"},
    ]

    with pytest.raises(ValueError, match="mode"):
        parse_md(response, mode="tuple")  # type: ignore[arg-type]
