"""Placement-aware backend row transport protocol contracts."""

import pytest

import heavenbase as hb
from heavenbase.workspace.rows.store import RowFragment


def test_row_op_validates_and_builds_isolated_replacement_rows():
    """RowOp should provide the complete replacement/delete protocol to backends."""
    values = {"title": "Alpha", "ignored": True, "meta": {"rank": 1}}
    op = hb.ext.RowOp("transport-doc", "d1", values)
    values["meta"]["rank"] = 2

    assert op.is_upsert() is True
    assert op.is_delete() is False
    assert op.values["meta"]["rank"] == 1
    assert op.to_row(fields=("title", "meta")) == {"title": "Alpha", "meta": {"rank": 1}, "object_id": "d1"}

    row = op.to_row()
    row["meta"]["rank"] = 3
    assert op.values["meta"]["rank"] == 1

    delete = hb.ext.RowOp("transport-doc", "d1", op="delete")
    assert delete.is_delete() is True
    assert delete.is_upsert() is False
    with pytest.raises(ValueError, match="no replacement row"):
        delete.to_row()
    with pytest.raises(ValueError, match="row mutation op"):
        hb.ext.RowOp("transport-doc", "d1", op="patch")
    with pytest.raises(ValueError, match="object_id is required"):
        hb.ext.RowOp("transport-doc", "")
    with pytest.raises(ValueError, match="entity_id"):
        hb.ext.RowOp("Bad Entity", "d1")
    with pytest.raises(TypeError, match="values must be a mapping"):
        hb.ext.RowOp("transport-doc", "d1", 7)


def test_backend_ordered_row_op_batches_preserve_phases_and_split_repeated_ids():
    """Backend batching keeps phase order while isolating repeated same-key replacements."""
    rows = [
        hb.ext.RowOp("transport-doc", "a", {"title": "first"}),
        hb.ext.RowOp("transport-doc", "b", {"title": "second"}),
        hb.ext.RowOp("transport-doc", "a", {"title": "replacement"}),
        hb.ext.RowOp("transport-doc", "c", op="delete"),
        hb.ext.RowOp("other-doc", "d", op="delete"),
    ]

    batches = list(hb.ext.Backend.ordered_row_op_batches(rows))

    assert [(entity_id, is_delete, [row.object_id for row in batch]) for entity_id, is_delete, batch in batches] == [
        ("transport-doc", False, ["a", "b"]),
        ("transport-doc", False, ["a"]),
        ("transport-doc", True, ["c"]),
        ("other-doc", True, ["d"]),
    ]


def test_row_fragment_isolates_backend_mapping_and_validates_source():
    """RowFragment should preserve backend provenance without tuple-position coupling."""
    row = {"object_id": "d1", "title": "Alpha"}
    fragment = RowFragment("main", row)
    row["title"] = "Changed"

    assert fragment.backend_name == "main"
    assert fragment.row == {"object_id": "d1", "title": "Alpha"}
    with pytest.raises(ValueError, match="backend_name is required"):
        RowFragment("", {})
    with pytest.raises(TypeError, match="row must be a backend field mapping"):
        RowFragment("main", 7)


def test_routed_row_store_wraps_every_backend_fragment():
    """Routed storage should wrap each physical result before logical merge."""
    workspace = hb.HeavenBase(
        "row-fragment-routing",
        detached=True,
        backends={"main": {"type": "inmem"}, "zaux": {"type": "inmem"}},
    )

    class RoutedNote(hb.Entity):
        identifier = "routed-transport-note"

        title = hb.field(hb.ShortText).store(to="main")
        body = hb.field(hb.LongText).store(to="zaux")

    workspace.register(RoutedNote)
    workspace.upsert(RoutedNote, {"object_id": "n1", "title": "Alpha", "body": "Split storage"})
    fragments = workspace._row_store.rows_for_ids(RoutedNote.schema().entity_id, ["n1"])["n1"]

    assert all(isinstance(fragment, RowFragment) for fragment in fragments)
    assert {fragment.backend_name for fragment in fragments} == {"main", "zaux"}
    assert workspace.get("n1", entity=RoutedNote) == {"object_id": "n1", "title": "Alpha", "body": "Split storage"}
