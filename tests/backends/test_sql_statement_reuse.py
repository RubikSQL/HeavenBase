"""SQL backend constructs are reused so each statement shape compiles once and executes many times."""

from __future__ import annotations

import pytest
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import ClauseElement

import heavenbase as hb

pytestmark = [pytest.mark.fast, pytest.mark.unit]

ENTITY = "statement-reuse-doc"


class ReuseDoc(hb.Entity):
    identifier = ENTITY

    title = hb.field(hb.ShortText)
    tags = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)


@pytest.fixture
def counters(monkeypatch):
    """Count SQL compiler constructions and construct stringifications."""
    counts = {"compile": 0, "str": 0}
    compiler_init = SQLCompiler.__init__
    clause_str = ClauseElement.__str__

    def count_compile(self, *args, **kwargs):
        counts["compile"] += 1
        return compiler_init(self, *args, **kwargs)

    def count_str(self):
        counts["str"] += 1
        return clause_str(self)

    monkeypatch.setattr(SQLCompiler, "__init__", count_compile)
    monkeypatch.setattr(ClauseElement, "__str__", count_str)
    return counts


def _workspace(tmp_path, name: str):
    backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / f"{name}.db"))
    ws = hb.HeavenBase(name, detached=True, backends={"main": backend})
    ws.register(ReuseDoc)
    return ws, backend


def _row(index: int) -> dict:
    return {"object_id": f"r{index:03}", "title": f"title {index}", "tags": ["shared", str(index)]}


@pytest.mark.slow
def test_sql_lookups_compile_once_and_execute_many_times(tmp_path, counters):
    ws, backend = _workspace(tmp_path, "statement-reuse")
    ws.upsert_many(ReuseDoc, [_row(index) for index in range(12)])
    backend.get_many(ENTITY, ["r001"])
    backend.exists_many(ENTITY, ["r001"])
    backend.count(ENTITY)
    backend.scan(ENTITY)
    list(backend.iter_scan(ENTITY, batch_size=5))
    counters.update(compile=0, str=0)

    for index in range(20):
        rows = backend.get_many(ENTITY, [f"r{index % 12:03}", "missing", f"r{index % 12:03}"])
        assert [row and row["object_id"] for row in rows] == [f"r{index % 12:03}", None, f"r{index % 12:03}"]
        assert rows[0]["tags"] == ["shared", str(index % 12)] and rows[0]["title"] == f"title {index % 12}"
        assert backend.exists_many(ENTITY, [f"r{index % 12:03}", "missing"]) == [True, False]
        assert backend.count(ENTITY) == 12
        assert [row["object_id"] for row in backend.scan(ENTITY)] == [f"r{i:03}" for i in range(12)]
        pages = [row["object_id"] for row in backend.iter_scan(ENTITY, batch_size=5)]
        assert pages == [f"r{i:03}" for i in range(12)]

    # every shape was compiled during warm-up; the loop hit SQLAlchemy's compiled cache each time
    assert counters == {"compile": 0, "str": 0}
    assert {shape for entity, shape in backend._constructs if entity == ENTITY} >= {
        "columns",
        "get_many",
        "exists_many",
        "count",
        "scan",
        "scan_page",
        "scan_page_after",
    }


@pytest.mark.slow
def test_sql_writes_reuse_delete_and_insert_constructs(tmp_path, counters):
    ws, backend = _workspace(tmp_path, "statement-reuse-writes")
    ws.upsert_many(ReuseDoc, [_row(0), _row(1)])
    ws.upsert(ReuseDoc, {**_row(1), "title": "rewritten"})
    ws.upsert(ReuseDoc, _row(2))
    ws.delete((ReuseDoc, "r000"))
    counters.update(compile=0, str=0)

    for index in range(3, 9):
        ws.upsert(ReuseDoc, _row(index))
        ws.upsert(ReuseDoc, {**_row(index), "tags": None})
        ws.delete((ReuseDoc, f"r{index:03}"))

    assert counters == {"compile": 0, "str": 0}
    assert backend.get_many(ENTITY, ["r001", "r002", "r003"]) == [
        {"object_id": "r001", "title": "rewritten", "tags": ["shared", "1"]},
        {"object_id": "r002", "title": "title 2", "tags": ["shared", "2"]},
        None,
    ]


class WideDoc(hb.Entity):
    identifier = "statement-reuse-wide"

    c0 = hb.field(hb.ShortText).optional()
    c1 = hb.field(hb.ShortText).optional()
    c2 = hb.field(hb.ShortText).optional()
    c3 = hb.field(hb.ShortText).optional()
    c4 = hb.field(hb.ShortText).optional()
    c5 = hb.field(hb.ShortText).optional()
    c6 = hb.field(hb.ShortText).optional()
    c7 = hb.field(hb.ShortText).optional()
    c8 = hb.field(hb.ShortText).optional()


@pytest.mark.slow
def test_sql_constructs_stay_bounded_across_page_sizes_and_selections(tmp_path):
    """Page sizes bind as parameters and never widen the cache; distinct selections do, up to a fixed bound."""
    ws, backend = _workspace(tmp_path, "statement-reuse-bounded")
    ws.upsert_many(ReuseDoc, [_row(index) for index in range(12)])
    ws.register(WideDoc)
    ws.upsert(WideDoc, {"object_id": "w", **{f"c{index}": f"value {index}" for index in range(9)}})
    expected_ids = [f"r{index:03}" for index in range(12)]

    for batch_size in range(1, 201):
        assert [row["object_id"] for row in backend.iter_scan(ENTITY, batch_size=batch_size)] == expected_ids
    assert {shape for entity, shape in backend._constructs if entity == ENTITY and shape.startswith("scan_page")} == {"scan_page", "scan_page_after"}

    selections = [[f"c{index}" for index in range(9) if mask >> index & 1] for mask in range(1, 301)]
    for selection in selections:
        assert backend.get_many("statement-reuse-wide", ["w"], columns=selection)[0] == {"object_id": "w", **{name: f"value {name[1:]}" for name in selection}}
    assert len(backend._constructs) <= backend._construct_cache_size == 512
    first = "get_many:" + ",".join(sorted({"object_id", *selections[0]}))
    assert ("statement-reuse-wide", first) not in backend._constructs  # the oldest selection was evicted
    # evicted shapes rebuild on demand and read correctly; the untouched shapes still serve
    assert backend.get_many("statement-reuse-wide", ["w"], columns=selections[0])[0] == {"object_id": "w", "c0": "value 0"}
    assert ("statement-reuse-wide", first) in backend._constructs
    assert [row["object_id"] for row in backend.iter_scan(ENTITY, batch_size=7)] == expected_ids
    assert backend.get_many(ENTITY, ["r003"])[0] == {"object_id": "r003", "title": "title 3", "tags": ["shared", "3"]}


def test_sql_constructs_evict_the_least_recently_used_shape(tmp_path):
    """A hit moves a construct to the back of the eviction order, so shapes in steady use survive one-off selections."""
    ws, backend = _workspace(tmp_path, "statement-reuse-lru")
    backend._forget_constructs()
    backend._construct_cache_size = 3
    built = []

    def builder(shape):
        def build():
            built.append(shape)
            return shape

        return build

    for shape in ("a", "b", "c"):
        assert backend._construct(ENTITY, shape, builder(shape)) == shape
    assert backend._construct(ENTITY, "a", builder("a")) == "a"  # a hit, not a rebuild
    assert backend._construct(ENTITY, "d", builder("d")) == "d"  # evicts the least recently used shape, "b"

    assert built == ["a", "b", "c", "d"]
    assert [shape for _, shape in backend._constructs] == ["c", "a", "d"]
    assert backend._construct(ENTITY, "b", builder("b")) == "b" and built[-1] == "b"
    assert [shape for _, shape in backend._constructs] == ["a", "d", "b"]
    ws.release()


def test_sql_constructs_follow_schema_replacement_and_drop(tmp_path):
    ws, backend = _workspace(tmp_path, "statement-reuse-shape")
    ws.upsert_many(ReuseDoc, [_row(0)])
    assert backend.get_many(ENTITY, ["r000"])[0] == {"object_id": "r000", "title": "title 0", "tags": ["shared", "0"]}
    assert (ENTITY, "get_many") in backend._constructs

    class WidenedDoc(hb.Entity):
        identifier = ENTITY

        title = hb.field(hb.ShortText)
        note = hb.field(hb.ShortText).optional()
        tags = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)

    ws.register(WidenedDoc, allow_replace=True)
    assert (ENTITY, "get_many") not in backend._constructs
    ws.upsert(WidenedDoc, {**_row(1), "note": "added later"})

    # the rebuilt construct projects the appended column instead of the pre-replacement shape
    assert backend.get_many(ENTITY, ["r000", "r001"]) == [
        {"object_id": "r000", "title": "title 0", "note": None, "tags": ["shared", "0"]},
        {"object_id": "r001", "title": "title 1", "note": "added later", "tags": ["shared", "1"]},
    ]

    backend.drop()
    assert backend._constructs == {}
    with pytest.raises(RuntimeError, match="unavailable after drop"):
        backend.get_many(ENTITY, ["r000"])
