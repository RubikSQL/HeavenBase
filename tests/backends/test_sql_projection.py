"""SQL lookups and keyset scans select only the requested columns, side tables, and vectors."""

from __future__ import annotations

import pytest
from sqlalchemy.sql.compiler import SQLCompiler

import heavenbase as hb

pytestmark = [pytest.mark.fast, pytest.mark.unit]

ENTITY = "projection-doc"


class ProjectionDoc(hb.Entity):
    identifier = ENTITY

    title = hb.field(hb.ShortText)
    revision = hb.field(hb.Integer).default(1)
    attrs = hb.field(hb.Json).optional()
    tags = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)
    embedding = hb.field(hb.Vector[3]).optional()


def _row(index: int) -> dict:
    return {
        "object_id": f"p{index:03}",
        "title": f"title {index}",
        "revision": index,
        "attrs": {"index": index},
        "tags": ["shared", str(index)],
        "embedding": [float(index), 0.5, -0.25],
    }


@pytest.fixture
def workspace(tmp_path):
    backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / "projection.db"))
    ws = hb.HeavenBase("sql-projection", detached=True, backends={"main": backend})
    ws.register(ProjectionDoc)
    ws.upsert_many(ProjectionDoc, [_row(index) for index in range(6)])
    yield ws, backend
    ws.release()


@pytest.fixture
def statements(workspace, monkeypatch):
    """Record every statement the backend executes so the selected columns can be inspected."""
    _ws, backend = workspace
    captured = []
    execute = backend.db.orm_execute

    def record(statement, *args, **kwargs):
        captured.append(statement)
        return execute(statement, *args, **kwargs)

    monkeypatch.setattr(backend.db, "orm_execute", record)
    return captured


def _selected(statement) -> set[str]:
    return {column.name for column in statement.selected_columns}


def test_get_many_selects_only_requested_columns_and_decodes_them(workspace, statements):
    ws, backend = workspace

    rows = backend.get_many(ENTITY, ["p001", "missing", "p004"], columns=["title", "attrs"])

    # a backend fragment carries the stored encoding; the workspace codec decodes the requested fields
    assert rows == [
        {"object_id": "p001", "title": "title 1", "attrs": '{"index": 1}'},
        None,
        {"object_id": "p004", "title": "title 4", "attrs": '{"index": 4}'},
    ]
    assert len(statements) == 1
    assert _selected(statements[0]) == {"object_id", "title", "attrs"}
    assert ws.get_many(["p001", "missing"], entity=ProjectionDoc, columns=["attrs"]) == [{"object_id": "p001", "attrs": {"index": 1}}, None]
    assert _selected(statements[-1]) == {"object_id", "attrs"}


def test_side_table_projection_carries_its_presence_marker(workspace, statements):
    ws, backend = workspace
    ws.upsert(ProjectionDoc, {**_row(7), "tags": None})
    ws.upsert(ProjectionDoc, {**_row(8), "tags": []})
    statements.clear()

    rows = backend.get_many(ENTITY, ["p002", "p007", "p008"], columns=["tags"])

    assert rows == [{"object_id": "p002", "tags": ["shared", "2"]}, {"object_id": "p007", "tags": None}, {"object_id": "p008", "tags": []}]
    assert _selected(statements[0]) == {"object_id", "_present_tags", "tags"}
    # an unrequested side table is neither joined nor decoded
    assert backend.get_many(ENTITY, ["p002"], columns=["title"]) == [{"object_id": "p002", "title": "title 2"}]
    assert _selected(statements[-1]) == {"object_id", "title"}


def test_empty_selection_reads_identity_only(workspace, statements):
    _ws, backend = workspace

    assert backend.get_many(ENTITY, ["p003", "missing"], columns=[]) == [{"object_id": "p003"}, None]
    assert _selected(statements[0]) == {"object_id"}


def test_unrequested_vector_is_never_selected(workspace, statements):
    _ws, backend = workspace

    rows = backend.get_many(ENTITY, ["p005"], columns=["title", "revision"])
    pages = list(backend.iter_scan(ENTITY, batch_size=4, columns=["revision"]))

    assert rows == [{"object_id": "p005", "title": "title 5", "revision": 5}]
    assert pages == [{"object_id": f"p{index:03}", "revision": index} for index in range(6)]
    assert all("embedding" not in _selected(statement) for statement in statements)
    assert [_selected(statement) for statement in statements[1:]] == [{"object_id", "revision"}] * len(statements[1:])


@pytest.mark.slow
def test_iter_scan_projection_pages_by_keyset_and_can_select_the_vector(workspace, statements):
    _ws, backend = workspace

    rows = list(backend.iter_scan(ENTITY, batch_size=2, columns=["embedding"]))

    assert rows == [{"object_id": f"p{index:03}", "embedding": [float(index), 0.5, -0.25]} for index in range(6)]
    assert len(statements) == 4  # three full pages plus the empty terminating page
    assert all(_selected(statement) == {"object_id", "embedding"} for statement in statements)


def test_projection_leaves_out_unstored_columns_and_rejects_non_string_names(workspace):
    _ws, backend = workspace

    # as the Backend contract states, a selection returns object_id plus the named fields this backend stores
    assert backend.get_many(ENTITY, ["p001"], columns=["title", "nope"]) == backend.get_many(ENTITY, ["p001"], columns=["title"])
    assert backend.get_many(ENTITY, ["p001"], columns=["title", "nope"])[0].keys() == {"object_id", "title"}
    # an internal presence marker is not a stored field
    assert list(backend.iter_scan(ENTITY, batch_size=2, columns=["_present_tags"]))[0] == {"object_id": "p000"}
    with pytest.raises(TypeError, match="field names"):
        backend.get_many(ENTITY, ["p001"], columns=[1])
    # an unknown entity keeps its miss shape without validating a selection it cannot know
    assert backend.get_many("absent-entity", ["p001"], columns=["title"]) == [None]


@pytest.mark.slow
def test_projected_shapes_are_cached_constructs_that_compile_once(workspace, monkeypatch):
    _ws, backend = workspace
    counts = {"compile": 0}
    compiler_init = SQLCompiler.__init__

    def count_compile(self, *args, **kwargs):
        counts["compile"] += 1
        return compiler_init(self, *args, **kwargs)

    monkeypatch.setattr(SQLCompiler, "__init__", count_compile)
    backend.get_many(ENTITY, ["p001"], columns=["title", "tags"])
    backend.get_many(ENTITY, ["p001"], columns=["tags", "title", "object_id"])  # same selection, different spelling
    list(backend.iter_scan(ENTITY, batch_size=3, columns=["title"]))
    counts["compile"] = 0

    for index in range(10):
        assert backend.get_many(ENTITY, [f"p{index % 6:03}"], columns=["tags", "title"])[0]["title"] == f"title {index % 6}"
        assert len(list(backend.iter_scan(ENTITY, batch_size=3, columns=["title"]))) == 6

    assert counts["compile"] == 0
    shapes = {shape for entity, shape in backend._constructs if entity == ENTITY}
    assert {"columns:object_id,tags,title", "get_many:object_id,tags,title", "scan_page:object_id,title", "scan_page_after:object_id,title"} <= shapes
    # the complete-row constructs from before projection keep their names
    assert backend.get_many(ENTITY, ["p001"])[0]["embedding"] == [1.0, 0.5, -0.25]
    assert {"columns", "get_many"} <= {shape for entity, shape in backend._constructs if entity == ENTITY}
