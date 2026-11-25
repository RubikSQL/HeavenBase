"""Neutral execution-frame composition contracts."""

import heavenbase as hb
import pytest
from heavenbase.execution.frame import difference, intersect, union
from heavenbase.execution.frame.aggregate import aggregate_frame, filter_frame
from heavenbase.execution.frame.json_path import derive_json_path_columns


def test_frame_merge_helpers_preserve_metadata_and_non_null_values():
    left = hb.ResultFrame.from_rows(
        [
            {"object_id": "a", "title": None, "evidence": [{"source": "left"}]},
            {"object_id": "b", "title": "Beta", "evidence": [{"source": "left-b"}]},
        ],
        metadata_columns={"evidence"},
    )
    right = hb.ResultFrame.from_rows(
        [
            {"object_id": "a", "title": "Alpha", "evidence": [{"source": "right"}]},
            {"object_id": "c", "title": "Gamma", "evidence": [{"source": "right-c"}]},
        ],
        metadata_columns={"evidence"},
    )

    merged = union([left, right])
    common = intersect([left, right])
    remaining = difference(left, right)

    assert merged.rows()[0] == {
        "object_id": "a",
        "title": "Alpha",
        "evidence": [{"source": "left"}, {"source": "right"}],
    }
    assert common.rows() == [
        {
            "object_id": "a",
            "title": "Alpha",
            "evidence": [{"source": "left"}, {"source": "right"}],
        }
    ]
    assert remaining.ids == ["b"]


def test_identityless_table_frame_keeps_table_shape_and_rejects_identity_ops():
    frame = hb.ResultFrame.from_table(
        [
            {"category": "chair", "n": 2},
            {"category": "desk", "n": 1},
        ],
        cols=("category", "n"),
    )

    assert frame.identity_column is None
    assert frame.col_names() == ["category", "n"]
    assert len(frame) == 2
    assert frame.sort("n", desc=True).limit(1).rows() == [{"category": "chair", "n": 2}]
    assert frame.project(["n"]).rows() == [{"n": 2}, {"n": 1}]
    assert frame.to_dict() == {"columns": {"category": ["chair", "desk"], "n": [2, 1]}, "identity_column": None}

    with pytest.raises(ValueError, match="no identity column"):
        frame.ids
    with pytest.raises(ValueError, match="no identity column"):
        frame.filter_ids(["chair"])
    with pytest.raises(ValueError, match="no identity column"):
        union([frame])


def test_frame_merge_rejects_different_identity_columns():
    left = hb.ResultFrame.from_rows([{"object_id": "a", "title": "Alpha"}])
    right = hb.ResultFrame.from_rows([{"row_id": "a", "score": 1}], identity_column="row_id")

    with pytest.raises(ValueError, match="different identity columns"):
        intersect([left, right])


def test_result_frame_validates_entity_identity_at_construction():
    with pytest.raises(ValueError, match="must contain identity column"):
        hb.ResultFrame.from_rows([{"title": "Alpha"}])
    with pytest.raises(ValueError, match="empty value"):
        hb.ResultFrame.from_rows([{"object_id": None, "title": "Alpha"}])
    with pytest.raises(ValueError, match="duplicate values"):
        hb.ResultFrame.from_rows([{"object_id": "a"}, {"object_id": "a"}])
    with pytest.raises(ValueError, match="same length"):
        hb.ResultFrame({"object_id": ["a"], "title": ["Alpha", "Beta"]})
    with pytest.raises(ValueError, match="collision after normalization"):
        hb.ResultFrame.from_table([{1: "numeric", "1": "text"}])


def test_result_frame_accessors_do_not_mutate_frame_state():
    frame = hb.ResultFrame.from_rows(
        [{"object_id": "a", "title": "Alpha", "evidence": [{"source": "query"}]}],
        metadata_columns={"evidence"},
    )

    columns = frame.columns
    columns["title"][0] = "Changed"
    columns["evidence"][0].append({"source": "changed"})
    rows = frame.rows()
    rows[0]["evidence"].clear()
    ids = frame.ids
    ids.append("b")
    metadata = frame.metadata_columns
    metadata.clear()

    assert frame.rows() == [{"object_id": "a", "evidence": [{"source": "query"}], "title": "Alpha"}]
    assert frame.ids == ["a"]
    assert frame.metadata_columns == {"evidence"}
    assert frame.to_dict()["metadata_columns"] == ["evidence"]
    assert frame == hb.ResultFrame.from_rows(frame.rows(), metadata_columns={"evidence"})


def test_result_frame_transformations_validate_requests_and_keep_nulls_last():
    frame = hb.ResultFrame.from_rows(
        [
            {"object_id": "missing", "rank": None},
            {"object_id": "high", "rank": 2},
            {"object_id": "low", "rank": 1},
        ]
    )

    assert frame.sort("rank").ids == ["low", "high", "missing"]
    assert frame.sort("rank", desc=True).ids == ["high", "low", "missing"]
    assert frame.limit(1, offset=1).ids == ["high"]
    with pytest.raises(ValueError, match="result column"):
        frame.project(["unknown"])
    with pytest.raises(ValueError, match="result column"):
        frame.sort("unknown")
    with pytest.raises(ValueError, match="result column"):
        frame.to_numpy("unknown")
    with pytest.raises(ValueError, match="non-negative"):
        frame.limit(-1)


def test_empty_frame_keeps_its_schema_for_sort_and_project():
    """An empty frame sorts and projects its own columns and rejects unknown names like a populated one."""
    empty = hb.ResultFrame.empty(["object_id", "rank", "title"])

    ordered = empty.sort("rank", desc=True).sort("title").limit(5, offset=1)
    assert ordered.rows() == [] and ordered.col_names() == ["object_id", "rank", "title"]
    assert ordered.project(["title"]).col_names() == ["object_id", "title"]
    for transform in (lambda frame: frame.sort("unknown"), lambda frame: frame.project(["unknown"])):
        with pytest.raises(ValueError, match="Unsupported result column 'unknown'"):
            transform(empty)
    with pytest.raises(ValueError, match="Unsupported result column 'rank'"):
        hb.ResultFrame.empty().project(["rank"])  # an empty frame never invents a column it was not built with


def test_frame_aggregation_uses_caller_bound_reducers():
    frame = hb.ResultFrame.from_rows(
        [
            {"object_id": "b", "kind": {"name": "desk"}, "price": 2},
            {"object_id": "a", "kind": {"name": "desk"}, "price": 1},
            {"object_id": "c", "kind": {"name": "lamp"}, "price": None},
        ]
    )

    result = aggregate_frame(
        frame,
        group_by=("kind",),
        metrics={"total": ("price", sum)},
    )

    assert result.rows() == [
        {"kind": {"name": "desk"}, "total": 3},
        {"kind": {"name": "lamp"}, "total": 0},
    ]
    assert filter_frame(result, lambda row: row["total"] > 0).rows() == [{"kind": {"name": "desk"}, "total": 3}]
    source_filtered = filter_frame(frame, lambda row: row["price"] == 1)
    assert source_filtered.identity_column == "object_id"
    assert source_filtered.ids == ["a"]


def test_json_path_derivation_is_an_execution_frame_transform():
    class Field:
        dtype = hb.Json

    class Schema:
        def get(self, name):
            assert name == "meta"
            return Field()

    frame = hb.ResultFrame.from_rows([{"object_id": "a", "meta": {"venue": {"city": "Paris"}}}])

    result = derive_json_path_columns(Schema(), frame, {"meta.venue.city": ("meta", ["venue", "city"])})

    assert result.rows() == [{"object_id": "a", "meta": {"venue": {"city": "Paris"}}, "meta.venue.city": "Paris"}]


def test_limit_records_the_complete_row_count_it_sliced_from():
    """Pagination runs last on a complete result, so a slice knows its exact total."""
    frame = hb.ResultFrame.from_rows([{"object_id": f"r{index}", "n": index} for index in range(10)])
    assert frame.total is None and frame.truncated is False  # an unsliced frame is already the whole result

    page = frame.limit(3)
    assert len(page) == 3 and page.total == 10 and page.truncated is True

    offset_page = frame.limit(3, offset=8)
    assert len(offset_page) == 2 and offset_page.total == 10 and offset_page.truncated is True

    whole = frame.limit(50)
    assert len(whole) == 10 and whole.total == 10 and whole.truncated is False  # a limit wider than the result cuts nothing
    assert whole.to_dict()["truncated"] is False and whole.to_dict()["total"] == 10

    nested = page.limit(2)
    assert nested.total == 10 and nested.truncated is True
    assert nested != hb.ResultFrame.from_rows(nested.rows())

    with pytest.raises(ValueError, match="cannot be smaller"):
        hb.ResultFrame({"object_id": ["a", "b"]}, total=1)

    # row-preserving derivations carry the total; anything that drops rows must not inherit it
    assert page.sort("n").total == 10
    assert page.project(["n"]).total == 10
    assert page.filter_ids(["r0"]).total is None


def test_frame_derivations_do_not_recopy_owned_vectors_but_exports_stay_isolated(monkeypatch):
    from heavenbase.execution.frame import result as result_module

    source = [{"object_id": "a", "vector": [1.0, 2.0], "meta": {"labels": ["original"]}}]
    frame = hb.ResultFrame.from_rows(source, metadata_columns={"meta"})
    source[0]["vector"][0] = 99.0

    def forbidden_copy(_value):
        raise AssertionError("projection/pagination must not deep-copy owned cells")

    with monkeypatch.context() as patch:
        patch.setattr(result_module, "deepcopy", forbidden_copy)
        projected = frame.project(["vector", "meta"])
        page = projected.limit(1)
        whole = frame.limit()
    assert projected is not frame and page is not projected and whole is not frame
    for derived in (frame, projected, page, whole):
        exported = derived.rows()
        exported[0]["vector"][0] = 77.0
        exported[0]["meta"]["labels"].clear()
        columns = derived.columns
        columns["vector"][0].clear()
        derived.column("meta")[0]["labels"].append("changed")
    for derived in (frame, projected, page, whole):
        assert derived.rows()[0]["vector"] == [1.0, 2.0]
        assert derived.rows()[0]["meta"] == {"labels": ["original"]}
