"""None in an exact or membership filter tests for a missing value on every optional logical type."""

from importlib.util import find_spec

import pytest

import heavenbase as hb

pytestmark = [pytest.mark.fast, pytest.mark.unit]

_VALUES = {
    "count": 7,
    "ratio": 1.5,
    "flag": False,
    "day": "2026-05-14",
    "ttl": 5,
    "rank": 1,
    "status": "todo",
    "stamp": 1000,
    "tag": "x",
    "meta": {"k": 1},
}


class _Sparse(hb.Entity):
    identifier = "none-filter-sparse"

    count = hb.field(hb.Integer).optional()
    ratio = hb.field(hb.Float).optional()
    flag = hb.field(hb.Boolean).optional()
    day = hb.field(hb.Date).optional()
    ttl = hb.field(hb.Duration).optional()
    rank = hb.field(hb.Categorical([1, 2], base="integer")).optional()
    status = hb.field(hb.Categorical(["todo", "done"])).optional()
    stamp = hb.field(hb.Timestamp).optional()
    tag = hb.field(hb.ShortText).optional()
    meta = hb.field(hb.Json).optional()


def _logic_cases(fields, text, prefix):
    """Yield `(where, expected ids)` that combine None, NOT and empty-`in` filters over rows `full` and `empty`.

    `fields` maps a field name to `(stored value, another value)`; `text` is a text field whose stored value starts with
    `prefix`, so a `$regex` on it goes through the portable scan on every backend.
    """
    for name, (value, other) in fields.items():
        yield {"$or": [{name: None}, {text: {"$regex": "^Q"}}]}, ["empty"]
        yield {"$or": [{name: None}, {text: {"$regex": f"^{prefix}"}}]}, ["empty", "full"]
        yield {"$and": [{name: None}, {"$not": {text: {"$regex": "^Q"}}}]}, ["empty"]
        yield {"$not": {name: other}}, ["empty", "full"]
        yield {"$not": {name: value}}, ["empty"]
        yield {"$not": {name: {"$in": [other]}}}, ["empty", "full"]
        yield {"$not": {name: None}}, ["full"]
        yield {name: {"$in": []}}, []
        yield {"$not": {name: {"$in": []}}}, ["empty", "full"]


def _config(backend, tmp_path):
    if backend in ("json", "pickle"):
        return {"type": backend, "path": hb.utils.path_to_file_uri(tmp_path / backend)}
    if backend == "duckdb":
        pytest.importorskip("duckdb_engine")
        return {"type": "duckdb", "database": ":memory:"}
    return {"type": backend}


@pytest.mark.slow
@pytest.mark.parametrize("backend", ["inmem", "sqlite", "duckdb", "json", "pickle"])
def test_none_filters_test_for_a_missing_value_on_every_type(backend, tmp_path):
    """`== None`, `{f: None}`, `$in [None]` match the missing row; `!= None`, `$ne None` the stored one."""
    ws = hb.HeavenBase(f"none-filters-{backend}", detached=True, backends={"main": _config(backend, tmp_path)})
    try:
        ws.register(_Sparse)
        ws.upsert(_Sparse, {"object_id": "full", **_VALUES})
        ws.upsert(_Sparse, {"object_id": "empty"})

        def ids(where):
            return sorted(ws.query(_Sparse).where(where).execute().ids)

        for name, value in _VALUES.items():
            column = getattr(_Sparse, name)
            missing = [ids(column == None), ids({name: None}), ids({name: {"$in": [None]}})]  # noqa: E711
            present = [ids(column != None), ids({name: {"$ne": None}})]  # noqa: E711
            assert missing == [["empty"]] * 3, name
            assert present == [["full"]] * 2, name
            assert ids({name: {"$in": [None, value]}}) == ["empty", "full"], name
        for name in ("count", "ratio", "day", "ttl", "rank", "stamp", "tag"):
            with pytest.raises(ValueError, match="'gt' filter needs a value; None only matches a missing"):
                ids({name: {"$gt": None}})
        fields = {"count": (7, 99), "ratio": (1.5, 9.5), "flag": (False, True), "rank": (1, 2), "tag": ("x", "zz")}
        for where, expected in _logic_cases(fields, "tag", "x"):
            assert ids(where) == expected, where
        assert ids({"$not": {"count": {"$lt": 9}}}) == ["empty"]
    finally:
        ws.release()


def _provider_filter_handler(provider):
    if provider == "elasticsearch":
        from heavenbase.backends.elasticsearch.handler import elasticsearch_filter_handler

        return elasticsearch_filter_handler
    if provider == "chroma":
        from heavenbase.backends.vector.handler.chroma import chroma_filter_handler

        return chroma_filter_handler
    from heavenbase.backends.vector.handler.pinecone import pinecone_filter_handler

    return pinecone_filter_handler


@pytest.mark.parametrize("provider", ["elasticsearch", "chroma", "pinecone"])
def test_provider_filters_scan_none_operands(provider):
    """A provider whose native filter cannot carry null scans a None operand, which then tests for a missing value."""
    handler = _provider_filter_handler(provider)
    schema = _Sparse.schema()
    ctx = hb.ext.HandlerContext(provider, provider, hb.InlineColumn.identifier, scalar_indexed=True)
    for name, value in (("count", 7), ("tag", "x"), ("flag", True)):
        field = schema.get(name)
        assert handler("eq")(field, value, ctx).mode() != "py_scan", (name, "eq", value)
        cases = (("eq", None, True, False), ("ne", None, False, True), ("in", [None], True, False), ("in", [None, value], True, True))
        for op, operand, matches_missing, matches_stored in cases:
            fragment = handler(op)(field, operand, ctx)
            assert fragment.mode() == "py_scan", (name, op, operand)
            assert fragment.payload({"object_id": "empty"}) is matches_missing, (name, op, operand)
            assert fragment.payload({"object_id": "full", name: value}) is matches_stored, (name, op, operand)
        if provider != "elasticsearch":
            # Chroma and Pinecone reject an empty $in list natively; the scan matches no row.
            fragment = handler("in")(field, [], ctx)
            assert fragment.mode() == "py_scan", (name, "in", [])
            assert fragment.payload({"object_id": "full", name: value}) is False, (name, "in", [])


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_none_filters_test_for_a_missing_value(tmp_path):
    """Chroma rejects null in a metadata filter, so None filters scan and match the row without the field."""
    path = hb.utils.path_to_file_uri(tmp_path / "chroma")
    backends = {"main": hb.backends.InMemBackend("main"), "chroma": hb.backends.ChromaBackend("chroma", path)}
    ws = hb.HeavenBase("none-filters-chroma", detached=True, backends=backends)

    class ChromaSparse(hb.Entity):
        identifier = "none-filter-chroma-sparse"

        title = hb.field(hb.ShortText).store(to="chroma").optional()
        score = hb.field(hb.Float).store(to="chroma").optional()
        count = hb.field(hb.Integer).store(to="chroma").optional()
        flag = hb.field(hb.Boolean).store(to="chroma").optional()
        emb = hb.field(hb.Vector[2]).store(to="chroma", strategy=hb.VectorIndex.identifier)

    try:
        ws.register(ChromaSparse)
        full = {"title": "One", "score": 1.0, "count": 3, "flag": True}
        ws.upsert_many(ChromaSparse, [{"object_id": "full", **full, "emb": [1.0, 0.0]}, {"object_id": "empty", "emb": [0.0, 1.0]}])

        def ids(where):
            return sorted(ws.query(ChromaSparse).where(where).execute().ids)

        for name, value in (("title", "One"), ("score", 1.0)):
            assert ids({name: None}) == ids({name: {"$in": [None]}}) == ["empty"], name
            assert ids({name: {"$ne": None}}) == ["full"], name
            assert ids({name: {"$in": [None, value]}}) == ["empty", "full"], name
            assert ws.query(ChromaSparse).where({name: None}).near(ChromaSparse.emb, [1.0, 0.0], top_k=2).execute().ids == ["empty"], name
        fields = {"title": ("One", "zz"), "score": (1.0, 9.5), "count": (3, 99), "flag": (True, False)}
        for where, expected in _logic_cases(fields, "title", "O"):
            assert ids(where) == expected, where
    finally:
        ws.release()


@pytest.mark.skipif(find_spec("lancedb") is None, reason="lancedb is not installed")
def test_lance_none_and_ne_filters_test_for_a_missing_value(tmp_path):
    """LanceDB compares NULL as unknown; None, `!=`, NOT and scanned filters still treat a row without the field as holding None."""
    path = hb.utils.path_to_file_uri(tmp_path / "lance")
    backends = {"main": hb.backends.InMemBackend("main"), "vec": hb.backends.LanceBackend("vec", path)}
    ws = hb.HeavenBase("none-filters-lance", detached=True, backends=backends)

    class LanceSparse(hb.Entity):
        identifier = "none-filter-lance-sparse"

        title = hb.field(hb.ShortText).store(to="vec").optional()
        count = hb.field(hb.Integer).store(to="vec").optional()
        score = hb.field(hb.Float).store(to="vec").optional()
        flag = hb.field(hb.Boolean).store(to="vec").optional()
        rank = hb.field(hb.Categorical([1, 2], base="integer")).store(to="vec").optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    try:
        ws.register(LanceSparse)
        full = {"title": "One", "count": 3, "score": 1.5, "flag": True, "rank": 1}
        ws.upsert_many(LanceSparse, [{"object_id": "full", **full, "emb": [1.0, 0.0]}, {"object_id": "empty", "emb": [0.0, 1.0]}])

        def ids(where):
            return sorted(ws.query(LanceSparse).where(where).execute().ids)

        for name, value, other in (("title", "One", "zz"), ("count", 3, 99)):
            assert ids({name: None}) == ids({name: {"$in": [None]}}) == ["empty"], name
            assert ids({name: {"$ne": None}}) == ["full"], name
            assert ids({name: {"$in": [None, value]}}) == ["empty", "full"], name
            assert ids({name: {"$ne": other}}) == ["empty", "full"], name
            assert ids({name: {"$ne": value}}) == ["empty"], name
            assert ids({name: value}) == ["full"], name
            assert ws.query(LanceSparse).where({name: None}).explain()["steps"][0]["handler_mode"] == "native", name
        rows = {row["object_id"]: row for row in ws.query(LanceSparse).execute().rows()}
        assert {name: rows["full"][name] for name in full} == full
        assert type(rows["full"]["count"]) is int and type(rows["full"]["rank"]) is int
        assert all(rows["empty"][name] is None for name in full)
        fields = {"title": ("One", "zz"), "count": (3, 99), "score": (1.5, 9.5), "flag": (True, False), "rank": (1, 2)}
        for where, expected in _logic_cases(fields, "title", "O"):
            assert ids(where) == expected, where
        assert ids({"$not": {"count": {"$lt": 9}}}) == ids({"$not": {"score": {"$gt": 0.5}}}) == ["empty"]
        steps = ws.query(LanceSparse).where({"$not": {"count": 99}}).explain()["steps"]
        assert [step["node"] for step in steps] == ["not", "filter"] and steps[1]["handler_mode"] == "native"
        near = ws.query(LanceSparse).where({"$or": [{"count": None}, {"title": {"$regex": "^Q"}}]}).near(LanceSparse.emb, [1.0, 0.0], top_k=2)
        assert near.execute().ids == ["empty"]
    finally:
        ws.release()
