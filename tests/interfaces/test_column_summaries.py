"""Unit contracts for the column-summary folds.

These run through the registered ``column_summary`` reducer, so the engine hands
each one every non-null value of a column. Testing them directly is the point of
keeping them as ordinary functions rather than caller-authored expressions.
"""

from __future__ import annotations

import pytest

from heavenbase.query.summary import (
    STAT_LIST_LIMIT,
    bool_summary,
    elided_value,
    json_summary,
    numeric_summary,
    stat_kind,
    text_summary,
    time_summary,
    vector_summary,
)

pytestmark = [pytest.mark.fast, pytest.mark.unit]


def test_stat_kind_maps_every_dtype_family():
    assert stat_kind("integer") == "numeric" and stat_kind("float") == "numeric"
    assert stat_kind("boolean") == "bool" and stat_kind("json") == "json"
    assert stat_kind("vector[8]") == "vector" and stat_kind("timestamp") == "time"
    assert stat_kind("array[short-text]") == "list" and stat_kind("short-text") == "text"
    assert stat_kind("") == "text"  # an unknown dtype gets the general treatment, not a crash


def test_numeric_summary_excludes_booleans_from_every_statistic():
    """Folding True as 1 would report a mean over values that were never numbers."""
    summary = numeric_summary([1, 2, 3, True, False])
    assert summary["min"] == 1.0 and summary["max"] == 3.0 and summary["mean"] == 2.0
    assert summary["distinct"] == 3  # the two booleans are not distinct numeric values
    assert sum(bucket["count"] for bucket in summary["histogram"]) == 3


def test_numeric_summary_is_exact_over_the_values_it_is_given():
    summary = numeric_summary(list(range(1, 101)))
    assert summary["min"] == 1.0 and summary["max"] == 100.0 and summary["mean"] == 50.5
    assert summary["percentiles"]["p50"] == 50.5
    assert sum(bucket["count"] for bucket in summary["histogram"]) == 100
    assert "top" not in summary  # 100 distinct values is not a most-common list


def test_numeric_summary_publishes_top_only_for_a_low_cardinality_column():
    summary = numeric_summary([1, 1, 1, 2, 2, 3])
    assert summary["top"][0] == {"value": "1", "count": 3}
    assert "top_truncated" not in summary  # three entries under the limit of five


def test_summaries_survive_an_all_null_column():
    """The engine drops nulls, so an all-null column reaches the fold as no values at all."""
    for summary in (numeric_summary([]), bool_summary([]), time_summary([]), vector_summary([]), json_summary([]), text_summary([])):
        assert summary == {}


def test_time_summary_reports_mixed_rather_than_one_of_the_formats():
    summary = time_summary(["2024-01-01", "2024-06-01T12:00:00Z"])
    assert summary["format"] == "mixed"
    assert summary["earliest"].startswith("2024-01-01") and summary["latest"].startswith("2024-06-01")
    assert summary["span_days"] > 150


def test_time_summary_counts_what_it_could_not_parse():
    summary = time_summary(["2024-01-01", "not a date", "also not"])
    assert summary["unparsed"] == 2
    assert summary["format"] == "ISO date" and summary["earliest"] == "2024-01-01"


def test_vector_summary_reports_a_range_when_widths_differ():
    assert vector_summary([[3.0, 4.0], [3.0, 4.0]])["dims"] == "2"
    assert vector_summary([[1.0], [1.0, 1.0]])["dims"] == "1–2"
    assert vector_summary([[3.0, 4.0]])["norm"] == {"min": 5.0, "mean": 5.0, "max": 5.0}


def test_json_summary_counts_objects_and_lists_sharing_one_column():
    summary = json_summary([{"a": 1, "b": 2}, {"a": 1}, ["x", "y"], 5])
    assert {row["key"]: row["count"] for row in summary["keys"]} == {"a": 2, "b": 1}
    assert summary["distinct_keys"] == 2 and summary["key_count"] == {"min": 1, "mean": 1.5, "max": 2}
    assert {row["value"]: row["count"] for row in summary["elements"]} == {"x": 1, "y": 1}
    assert summary["items"] == {"min": 2, "mean": 2.0, "max": 2}


def test_json_summary_describes_the_rows_that_are_neither_object_nor_list():
    """A Json column may hold bare scalars; they yield no keys and no elements."""
    summary = json_summary([{"a": 1}] * 10 + [["x", "y"]] * 10 + [7] * 80)
    assert summary["scalars"] == 80
    assert summary["scalar_values"] == [{"value": "7", "count": 80}]
    # every other number describes only the 20 shaped rows, which is why the count must be published
    assert summary["key_count"] == {"min": 1, "mean": 1.0, "max": 1}
    assert summary["items"] == {"min": 2, "mean": 2.0, "max": 2}

    # a column of nothing but scalars still describes itself rather than
    # returning a body with no kind-specific key at all
    only = json_summary([1, 2, 2, 3, "x"])
    assert only["scalars"] == 5
    assert only["scalar_values"][0] == {"value": "2", "count": 2}
    assert [row["value"] for row in only["scalar_values"]] == ["2", "1", "3", "x"]  # ties break on the name

    # a column with no scalars must not carry either key
    assert "scalars" not in json_summary([{"a": 1}, ["x"]])
    assert "scalar_values" not in json_summary([{"a": 1}, ["x"]])


def test_json_summary_never_caps_the_key_list_silently():
    """A rare key must be counted, and a list long enough to cut must say it was cut."""
    summary = json_summary([{f"tag{index:02d}": 1, "shared": 1} for index in range(40)])
    assert summary["distinct_keys"] == 41 and len(summary["keys"]) == 41
    assert summary["keys"][0] == {"key": "shared", "count": 40}
    assert "keys_truncated" not in summary  # well under the limit, so nothing was cut

    # the fold sees every row, so a varied column would otherwise publish an
    # unbounded list; the cut is declared and the exact count survives it
    wide = json_summary([{f"k{index:04d}": 1} for index in range(STAT_LIST_LIMIT + 300)])
    assert len(wide["keys"]) == STAT_LIST_LIMIT
    assert wide["keys_truncated"] is True
    assert wide["distinct_keys"] == STAT_LIST_LIMIT + 300


def test_text_summary_gives_examples_only_when_nothing_repeats():
    repeated = text_summary(["same", "same", "other"])
    assert repeated["top"][0] == {"value": "same", "count": 2} and "examples" not in repeated

    unique = text_summary(["one", "two", "three", "four"])
    assert [example["value"] for example in unique["examples"]] == ["one", "two", "three"]
    assert unique["distinct"] == 4 and unique["length"] == {"min": 3, "mean": 3.8, "max": 5}


def test_text_summary_keeps_two_long_values_that_share_a_prefix():
    shared = "x" * 200
    summary = text_summary([f"{shared}alpha", f"{shared}beta"])
    assert len(summary["examples"]) == 2  # dedup keys on the whole value, not the shown prefix
    assert {example["value_len"] for example in summary["examples"]} == {205, 204}
    assert all(example["value_truncated"] is True for example in summary["examples"])


def test_tied_counts_break_on_the_name_not_on_row_order():
    """A published list that reorders with row order is not a fact about the data."""
    forward = json_summary([{"zebra": 1, "apple": 1}, ["zebra", "apple"]])
    reverse = json_summary([{"apple": 1, "zebra": 1}, ["apple", "zebra"]])
    assert [row["key"] for row in forward["keys"]] == [row["key"] for row in reverse["keys"]] == ["apple", "zebra"]
    assert [row["value"] for row in forward["elements"]] == [row["value"] for row in reverse["elements"]] == ["apple", "zebra"]

    # six distinct values into a list of five: the cut must drop the SAME entry
    # either way, not whichever one happened to arrive last
    a = text_summary(["f", "e", "d", "c", "b", "a", "a"])
    b = text_summary(["a", "b", "c", "d", "e", "f", "a"])
    assert a["top"] == b["top"] == [{"value": "a", "count": 2}] + [{"value": name, "count": 1} for name in "bcde"]
    assert a["top_truncated"] is True and b["top_truncated"] is True  # "f" is the one dropped, both ways


def test_elided_value_marks_only_what_it_cut():
    assert elided_value("short") == {"value": "short"}
    cell = elided_value("y" * 300)
    assert cell["value_truncated"] is True and cell["value_len"] == 300 and len(cell["value"]) == 120
