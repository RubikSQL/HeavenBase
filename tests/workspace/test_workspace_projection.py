"""Workspace reads with ``columns`` fetch only the requested fields from each routed backend."""

from __future__ import annotations

import pytest

import heavenbase as hb
from heavenbase.workspace.rows.errors import SchemaVersionMismatchError

pytestmark = [pytest.mark.fast, pytest.mark.unit]


class SplitDoc(hb.Entity):
    """Scalar fields on ``main`` and a vector replica on ``vec`` exercise per-route ownership."""

    identifier = "projection-split-doc"

    title = hb.field(hb.ShortText)
    status = hb.field(hb.ShortText).default("draft")
    embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex).optional()


@pytest.fixture
def workspace():
    ws = hb.HeavenBase("workspace-projection", detached=True, backends={"main": {"type": "inmem"}, "vec": {"type": "inmem"}})
    ws.register(SplitDoc)
    ws.upsert_many(
        SplitDoc,
        [
            {"object_id": "d1", "title": "one", "status": "live", "embedding": [1.0, 0.0]},
            {"object_id": "d2", "title": "two", "embedding": [0.0, 1.0]},
        ],
    )
    yield ws
    ws.release()


@pytest.fixture
def lookups(workspace, monkeypatch):
    """Record the selection every routed backend receives."""
    calls = []
    for name in ("main", "vec"):
        backend = workspace.backends.get(name)
        original = backend.get_many

        def record(entity_id, object_ids, *, columns=None, _name=name, _original=original):
            calls.append((_name, columns))
            return _original(entity_id, object_ids, columns=columns)

        monkeypatch.setattr(backend, "get_many", record)
    return calls


def test_get_and_get_many_return_only_requested_fields(workspace, lookups):
    assert workspace.get("d1", entity=SplitDoc, columns=["title"]) == {"object_id": "d1", "title": "one"}
    assert workspace.get_many(["d2", "absent", "d1"], entity=SplitDoc, columns=["status", "embedding"]) == [
        {"object_id": "d2", "status": "draft", "embedding": [0.0, 1.0]},
        None,
        {"object_id": "d1", "status": "live", "embedding": [1.0, 0.0]},
    ]
    # each route is asked only for the requested fields it stores; a route owning none still answers with identity
    assert lookups == [("main", ["title"]), ("vec", []), ("main", ["status"]), ("vec", ["embedding"])]
    # the complete read is unchanged
    assert workspace.get("d1", entity=SplitDoc) == {"object_id": "d1", "title": "one", "status": "live", "embedding": [1.0, 0.0]}
    assert lookups[-2:] == [("main", None), ("vec", None)]


@pytest.mark.slow
def test_projection_requires_an_entity_and_declared_fields(workspace):
    with pytest.raises(ValueError, match="requires entity"):
        workspace.get("d1", columns=["title"])
    with pytest.raises(ValueError, match="no field"):
        workspace.get_many(["d1"], entity=SplitDoc, columns=["title", "nope"])
    with pytest.raises(TypeError, match="field names"):
        workspace.get("d1", entity=SplitDoc, columns=["title", 3])
    # object_id may be named explicitly and duplicates collapse
    assert workspace.get("d1", entity=SplitDoc, columns=["object_id", "title", "title"]) == {"object_id": "d1", "title": "one"}
    assert workspace.get_many(["d1", "d2"], entity=SplitDoc, columns=[]) == [{"object_id": "d1"}, {"object_id": "d2"}]


def test_projection_is_validated_whatever_the_object_ids(workspace):
    """An empty ``get_many`` validates ``columns`` exactly as a non-empty one does."""
    with pytest.raises(ValueError, match=r"projection-split-doc has no field\(s\) \['nope'\]"):
        workspace.get_many([], entity=SplitDoc, columns=["nope"])
    with pytest.raises(TypeError, match="^columns must be field names$"):
        workspace.get_many([], entity=SplitDoc, columns=[3])
    with pytest.raises(ValueError, match="requires entity"):
        workspace.get_many([], columns=["title"])
    assert workspace.get_many([], entity=SplitDoc, columns=["title"]) == []


def test_projection_refuses_a_bare_string(workspace):
    """A ``str`` is a ``Sequence[str]`` of characters; it is refused instead of selecting one field per letter."""
    refusal = r"^columns must be a sequence of field names, not a single str; pass \['title'\] to select one field$"
    with pytest.raises(TypeError, match=refusal):
        workspace.get("d1", entity=SplitDoc, columns="title")
    with pytest.raises(TypeError, match=refusal):
        workspace.get_many([], entity=SplitDoc, columns="title")
    # bytes are refused without suggesting a bytes name, which would be refused too
    with pytest.raises(TypeError, match=r"^columns must be a sequence of str field names, not bytes$"):
        workspace.get("d1", entity=SplitDoc, columns=b"title")

    class Lettered(hb.Entity):
        identifier = "projection-lettered"

        a = hb.field(hb.ShortText)
        b = hb.field(hb.ShortText)

    workspace.register(Lettered)
    workspace.upsert(Lettered, {"object_id": "x", "a": "1", "b": "2"})
    with pytest.raises(TypeError, match="not a single str"):
        workspace.get("x", entity=Lettered, columns="ab")
    assert workspace.get("x", entity=Lettered, columns=["a", "b"]) == {"object_id": "x", "a": "1", "b": "2"}


def test_schema_version_check_covers_only_requested_fields(workspace):
    # a row written under an older schema lacks the defaulted ``status`` field
    workspace.backends.get("main").upsert([hb.ext.RowOp(SplitDoc.identifier, "old", {"object_id": "old", "title": "stale"})])

    with pytest.raises(SchemaVersionMismatchError):
        workspace.get("old", entity=SplitDoc)
    with pytest.raises(SchemaVersionMismatchError):
        workspace.get("old", entity=SplitDoc, columns=["status"])
    assert workspace.get("old", entity=SplitDoc, columns=["title"]) == {"object_id": "old", "title": "stale"}


def test_row_store_iteration_honours_the_selection(workspace):
    rows = list(workspace._row_store.iter_rows(SplitDoc.identifier, batch_size=1, columns=["title"]))

    assert sorted(rows, key=lambda row: row["object_id"]) == [{"object_id": "d1", "title": "one"}, {"object_id": "d2", "title": "two"}]
    assert sorted(row["object_id"] for row in workspace._row_store.iter_rows(SplitDoc.identifier, batch_size=1)) == ["d1", "d2"]


def test_portable_backend_projection_trims_fragments_in_python():
    backend = hb.backends.InMemBackend("solo", ws_id="projection")
    backend.upsert([hb.ext.RowOp("thing", "t1", {"object_id": "t1", "a": 1, "b": 2})])

    assert backend.get_many("thing", ["t1", "t2"], columns=["b"]) == [{"object_id": "t1", "b": 2}, None]
    assert list(backend.iter_scan("thing", batch_size=5, columns=["a"])) == [{"object_id": "t1", "a": 1}]
    assert backend.get_many("thing", ["t1"]) == [{"object_id": "t1", "a": 1, "b": 2}]
    with pytest.raises(TypeError, match="field names"):
        backend.get_many("thing", ["t1"], columns=[None])
    with pytest.raises(TypeError, match="^columns must be a sequence of field names, not a single str$"):
        backend.get_many("thing", ["t1"], columns="ab")
    with pytest.raises(TypeError, match="not a single str"):
        list(backend.iter_scan("thing", batch_size=5, columns="a"))
