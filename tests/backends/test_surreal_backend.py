"""Tests for the SurrealDB multi-model backend (P0–P3: row + vector + full-text + graph).

Offline tests cover the parts that need no server or ``surrealdb`` driver: registry wiring,
database namespacing, native-vs-scan filter routing, the SurrealQL ``WHERE`` compiler, and the
provider-unavailable degrade path (the driver is imported lazily inside ``_connect``, so an absent
driver or unreachable server degrades to ``client=None`` rather than raising at construction).

Local integration tests use the **embedded** ``mem://`` engine in-process (no docker, no server).
Core row/query checks run in the fast suite; the heavier GraphEdge lifecycle/projection matrix uses
the ``full`` tier. Both skip when the ``surrealdb`` driver is not installed and cover row CRUD,
native scalar/text pushdown, cosine vector KNN, the compound-type codec, canonical inline GraphEdge
traversal / shortest-path, and direct checks of the derived ``RELATE`` shadow projection.

A separate ``@pytest.mark.external`` suite re-runs that same surface against a **real ``ws://``
server** (mirroring the neo4j external pattern): it parses ``HVNB_SURREAL_URL`` (default
``ws://localhost:8000/rpc``, creds from ``HVNB_SURREAL_USER``/``HVNB_SURREAL_PASSWORD``) and skips
when the driver is missing, the socket is unreachable, or signin fails — so the default suite stays
green with no server. Bring one up with, e.g.::

    docker run -p 8000:8000 surrealdb/surrealdb:latest start --user root --pass root
"""

import os
import socket
import sys
from importlib.util import find_spec
from urllib.parse import urlparse

import pytest

sys.path.insert(0, "src")

import heavenbase as hb
from heavenbase.backends import SurrealBackend
from heavenbase.execution import RowOp

WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry

_OFFLINE_URL = "ws://127.0.0.1:1/rpc"  # nothing listens here -> connect refused fast; driver also absent -> degrade
_SURREAL_URL = os.environ.get("HVNB_SURREAL_URL", "ws://localhost:8000/rpc")  # @external server (creds via HVNB_SURREAL_*)


class _RtThing(hb.Entity):
    identifier = "surreal-rt-thing"

    name = hb.field(hb.ShortText)
    score = hb.field(hb.Float)


# --------------------------------------------------------------------- offline
def test_surreal_backend_is_registered():
    """surreal publishes its Backend tags and structural handler record."""
    modules = hb.DEFAULT_CONTEXT.modules()
    ids = hb.BackendType.identifiers()
    assert "surreal" in ids
    assert "row" in modules.tag("backend_type", "surreal", "storage-roles").value
    assert modules.tag("backend_type", "surreal", "graph").value is True
    assert modules.inspect("handler", "surreal")


def test_surreal_degrades_when_unreachable():
    """An absent driver or unreachable server degrades to client=None; ops raise lazily, ensure records schema."""
    backend = SurrealBackend("surreal", url=_OFFLINE_URL)
    assert backend.client is None
    assert backend.client_error  # captured connect/import error
    assert backend.to_dict()["connected"] is False
    assert backend.tag("row-lookup").value == {"get_many": "native", "exists_many": "native", "count": "native"}
    assert backend.tag("backend-availability").value == "unavailable"
    with pytest.raises(RuntimeError):
        backend.scan("ent")
    with pytest.raises(RuntimeError):
        backend.upsert([RowOp("ent", "o1", {"x": 1})])
    backend.ensure(_RtThing.schema(), [])  # records schema and returns without a client; must not raise
    backend.drop()  # drop is a no-op while down


def test_surreal_database_namespacing():
    """Each (instance name, workspace) maps to its own database under a shared namespace."""
    a = SurrealBackend("sa", url=_OFFLINE_URL, ws_id="shop")
    b = SurrealBackend("sb", url=_OFFLINE_URL, ws_id="shop")
    a2 = SurrealBackend("sa", url=_OFFLINE_URL, ws_id="shop2")
    assert a._database() != b._database()  # different instance name -> different database
    assert a._database() != a2._database()  # different workspace -> different database
    assert a._namespace() == b._namespace()  # shared namespace
    assert a._database().startswith("sa")
    assert SurrealBackend("s", url=_OFFLINE_URL)._database() == "s"  # no workspace -> bare instance name


def test_surreal_ident_rejects_backtick():
    """Identifier quoting backtick-wraps validated names and rejects an embedded backtick."""
    backend = SurrealBackend("s", url=_OFFLINE_URL)
    assert backend._ident("name") == "`name`"
    with pytest.raises(ValueError):
        backend._ident("ev`il")


def test_surreal_filter_handler_native_vs_scan_routing():
    """The handler pushes the parity-safe subset to a surreal_where fragment and scans the rest (offline)."""
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.surreal.handler import surreal_filter_handler

    class _FilterDoc(hb.Entity):
        identifier = "unit-surreal-filter-doc"

        name = hb.field(hb.ShortText)
        price = hb.field(hb.Float)
        qty = hb.field(hb.Integer)
        flag = hb.field(hb.Boolean)
        meta = hb.field(hb.Json)

    fields = _FilterDoc.schema().fields
    ctx = HandlerContext("s", "surreal", "inline")

    eq = surreal_filter_handler("eq")(fields["name"], "x", ctx)
    assert eq.notes["kind"] == "surreal_where" and eq.notes["mode"] == "native"
    assert eq.payload == {"op": "eq", "field": "name", "value": "x"}
    in_ = surreal_filter_handler("in")(fields["qty"], [1, 2], ctx)
    assert in_.notes["kind"] == "surreal_where" and in_.payload["value"] == [1, 2]
    assert surreal_filter_handler("lt")(fields["price"], 5.0, ctx).notes["kind"] == "surreal_where"
    assert surreal_filter_handler("gte")(fields["qty"], 3, ctx).notes["kind"] == "surreal_where"
    assert surreal_filter_handler("lt")(fields["name"], "m", ctx).notes["mode"] == "py_scan"  # text range -> scan
    assert surreal_filter_handler("eq")(fields["flag"], None, ctx).notes["mode"] == "py_scan"  # None operand -> scan
    assert surreal_filter_handler("eq")(fields["flag"], True, ctx).notes["kind"] == "surreal_where"
    assert surreal_filter_handler("eq")(fields["meta"], {"a": 1}, ctx).notes["mode"] == "py_scan"  # compound -> scan
    assert surreal_filter_handler("ne")(fields["name"], "x", ctx).notes["mode"] == "py_scan"  # ne -> scan


def test_surreal_where_compiler_and_pushdown_gate_offline():
    """_compile_where renders a parameterized WHERE and _is_surreal_fragment gates whole-tree pushdown (offline)."""
    from heavenbase.execution import HandlerContext, QueryFragment
    from heavenbase.backends.surreal.handler import surreal_filter_handler

    class _WhereDoc(hb.Entity):
        identifier = "unit-surreal-where-doc"

        name = hb.field(hb.ShortText)
        price = hb.field(hb.Float)
        qty = hb.field(hb.Integer)

    f = _WhereDoc.schema().fields
    ctx = HandlerContext("s", "surreal", "inline")
    backend = SurrealBackend("s", url=_OFFLINE_URL)
    eq = surreal_filter_handler("eq")(f["name"], "x", ctx)
    in_ = surreal_filter_handler("in")(f["qty"], [1, 2], ctx)
    lt = surreal_filter_handler("lt")(f["price"], 5.0, ctx)

    variables: dict = {}
    assert backend._compile_where(eq, variables) == "`name` = $p0" and variables == {"p0": "x"}
    variables = {}
    assert backend._compile_where(in_, variables) == "`qty` IN $p0" and variables == {"p0": [1, 2]}
    variables = {}
    assert backend._compile_where(lt, variables) == "`price` < $p0" and variables == {"p0": 5.0}

    and_frag = QueryFragment("s", "and", "", [eq, lt])
    variables = {}
    assert backend._compile_where(and_frag, variables) == "(`name` = $p0 AND `price` < $p1)"
    assert variables == {"p0": "x", "p1": 5.0}
    or_frag = QueryFragment("s", "or", "", [eq, in_])
    variables = {}
    assert backend._compile_where(or_frag, variables) == "(`name` = $p0 OR `qty` IN $p1)"

    assert backend._is_surreal_fragment(eq) is True
    assert backend._is_surreal_fragment(and_frag) is True
    scan_leaf = surreal_filter_handler("ne")(f["name"], "x", ctx)
    assert backend._is_surreal_fragment(scan_leaf) is False
    assert backend._is_surreal_fragment(QueryFragment("s", "and", "", [eq, scan_leaf])) is False
    assert backend._is_surreal_fragment(QueryFragment("s", "not", "", [eq])) is False


# ----------------------------------------------------- live (embedded, in-process)
def _embedded(name: str, *, ws_id: str | None = None) -> SurrealBackend:
    """Return a connected embedded SurrealBackend, or skip when the driver is unavailable."""
    if find_spec("surrealdb") is None:
        pytest.skip("surrealdb driver is not installed")
    backend = SurrealBackend(name, url="mem://", ws_id=ws_id)
    if backend.client is None:
        pytest.skip(f"surrealdb embedded engine unavailable: {backend.client_error}")
    return backend


def test_surreal_embedded_backend_crud_and_namespacing():
    """Backend-level CRUD round-trips, full-row replace drops stale fields, and namespaces are isolated."""
    backend = _embedded("sc", ws_id="nsa")
    other = _embedded("sc", ws_id="nsb")  # same instance name, different workspace -> isolated database
    schema = _RtThing.schema()
    for instance in (backend, other):
        instance.ensure(schema, [])
    backend.upsert(
        [
            RowOp(schema.entity_id, "a", {"name": "alpha", "score": 1.0}),
            RowOp(schema.entity_id, "b", {"name": "beta", "score": 2.0}),
        ]
    )
    rows = {row["object_id"]: row for row in backend.scan(schema.entity_id)}
    assert set(rows) == {"a", "b"} and rows["a"]["name"] == "alpha" and rows["a"]["score"] == 1.0
    assert backend.count(schema.entity_id) == 2
    assert other.count(schema.entity_id) == 0  # different workspace database -> isolated

    got = backend.get_many(schema.entity_id, ["b", "missing", "a"])
    assert [row["object_id"] if row else None for row in got] == ["b", None, "a"]
    assert backend.exists_many(schema.entity_id, ["a", "missing"]) == [True, False]

    backend.upsert([RowOp(schema.entity_id, "a", {"name": "alpha2"})])  # full-row replace drops stale score
    (row,) = backend.get_many(schema.entity_id, ["a"])
    assert row["name"] == "alpha2" and row.get("score") is None

    backend.upsert([RowOp(schema.entity_id, "a", {}, op="delete")])
    assert backend.count(schema.entity_id) == 1
    assert backend.exists_many(schema.entity_id, ["a"]) == [False]


def test_surreal_embedded_workspace_query_pipeline():
    """End-to-end through the workspace: upsert, then scalar filters via the routed pipeline."""
    backend = _embedded("sq")
    ws = hb.HeavenBase("surreal-q", backends={"s": backend})
    try:
        ws.register(_RtThing)
        ws.upsert_many(
            _RtThing,
            [
                {"object_id": "a", "name": "alpha", "score": 1.0},
                {"object_id": "b", "name": "beta", "score": 2.0},
                {"object_id": "c", "name": "gamma", "score": 3.0},
            ],
        )
        assert sorted(ws.query(_RtThing).execute().ids) == ["a", "b", "c"]
        assert sorted(ws.query(_RtThing).where(_RtThing.score > 1.5).execute().ids) == ["b", "c"]
        assert ws.query(_RtThing).where(_RtThing.name == "alpha").execute().ids == ["a"]
    finally:
        ws.drop()
        WORKSPACES.pop("surreal-q", None)


class _PushdownDoc(hb.Entity):
    identifier = "surreal-pushdown-doc"

    name = hb.field(hb.ShortText)
    price = hb.field(hb.Float)
    qty = hb.field(hb.Integer)
    tag = hb.field(hb.ShortText).optional()


def test_surreal_embedded_native_pushdown_parity():
    """Native SurrealQL eq/in/range pushdown returns exactly what the scan predicate would (embedded)."""
    backend = _embedded("spd")
    ws = hb.HeavenBase("surreal-pd", backends={"s": backend})
    try:
        ws.register(_PushdownDoc)
        ws.upsert_many(
            _PushdownDoc,
            [
                {"object_id": "p1", "name": "desk", "price": 89.0, "qty": 5, "tag": "A"},
                {"object_id": "p2", "name": "lamp", "price": 19.0, "qty": 0, "tag": "A"},
                {"object_id": "p3", "name": "sofa", "price": 299.0, "qty": 3},  # tag absent (None)
                {"object_id": "p4", "name": "desk", "price": 150.0, "qty": 5, "tag": "B"},
            ],
        )

        def native(query):
            assert query.explain()["steps"][0].get("handler_mode") == "native"
            return sorted(query.execute().ids)

        assert native(ws.query(_PushdownDoc).where(_PushdownDoc.name == "desk")) == ["p1", "p4"]
        assert native(ws.query(_PushdownDoc).where(_PushdownDoc.qty.in_([0, 5]))) == ["p1", "p2", "p4"]
        assert native(ws.query(_PushdownDoc).where(_PushdownDoc.price < 100)) == ["p1", "p2"]
        assert native(ws.query(_PushdownDoc).where(_PushdownDoc.price >= 150)) == ["p3", "p4"]
        assert native(ws.query(_PushdownDoc).where(_PushdownDoc.tag == "A")) == ["p1", "p2"]  # p3 (None) excluded
        # logical AND/OR of native leaves still pushes down to one SurrealQL WHERE
        assert sorted(ws.query(_PushdownDoc).where((_PushdownDoc.price > 50) & (_PushdownDoc.price < 200)).execute().ids) == ["p1", "p4"]
        assert sorted(ws.query(_PushdownDoc).where((_PushdownDoc.name == "lamp") | (_PushdownDoc.qty == 3)).execute().ids) == ["p2", "p3"]
        # a regex op cannot push down -> scan fallback, still correct
        rx = ws.query(_PushdownDoc).where(_PushdownDoc.name.regex("^des"))
        assert rx.explain()["steps"][0].get("handler_mode") == "py_scan"
        assert sorted(rx.execute().ids) == ["p1", "p4"]
    finally:
        ws.drop()
        WORKSPACES.pop("surreal-pd", None)


# ----------------------------------------------------------------- P1: vector
def test_surreal_near_handler_produces_native_vector_fragment():
    """The near handler emits a native surreal_vector fragment carrying the query + a metric scorer (offline)."""
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.surreal.handler import surreal_near_handler
    from heavenbase.types import cosine_score, euclidean_score, inner_product_score

    class _NearDoc(hb.Entity):
        identifier = "unit-surreal-near-doc"

        emb = hb.field(hb.Vector[3])

    field = _NearDoc.schema().fields["emb"]
    q = [1.0, 0.0, 0.0]
    frag = surreal_near_handler(field, q, HandlerContext("s", "surreal", "vector", top_k=5, metric="cosine"))
    assert frag.op == "near" and frag.notes["kind"] == "surreal_vector" and frag.notes["mode"] == "native"
    assert frag.payload == {"vector": q, "top_k": 5, "metric": "cosine"}
    assert callable(frag.notes["row_payload"])
    # native KNN is cosine-only, so a non-cosine metric reports py_scan honestly (it scans at runtime)
    l2_frag = surreal_near_handler(field, q, HandlerContext("s", "surreal", "vector", top_k=5, metric="l2"))
    assert l2_frag.notes["mode"] == "py_scan" and l2_frag.notes.get("fallback_reason")

    row = {"emb": [0.5, 0.5, 0.0]}

    def scorer(metric):
        return surreal_near_handler(field, q, HandlerContext("s", "surreal", "vector", top_k=5, metric=metric)).notes["row_payload"]

    assert scorer("cosine")(row) == cosine_score(row["emb"], q)
    assert scorer("l2")(row) == euclidean_score(row["emb"], q)
    assert scorer("ip")(row) == inner_product_score(row["emb"], q)
    assert scorer("cosine")({}) is None  # KNN never returns a record without the vector
    assert scorer("l2")({}) is None and scorer("ip")({}) is None  # an empty vector never outranks real rows
    with pytest.raises(ValueError):  # query dimension validated against Vector[3]
        surreal_near_handler(field, [1.0, 0.0], HandlerContext("s", "surreal", "vector", top_k=5, metric="cosine"))
    with pytest.raises(ValueError):  # unsupported metric rejected, not silently mis-ranked
        surreal_near_handler(field, q, HandlerContext("s", "surreal", "vector", top_k=5, metric="bogus"))


class _NearLive(hb.Entity):
    identifier = "surreal-near-doc"

    cat = hb.field(hb.ShortText)
    emb = hb.field(hb.Vector[3])


def test_surreal_embedded_vector_near_parity():
    """Native cosine KNN returns the same top-k as the cosine scan scorer; near+filter degrades to scan (embedded)."""
    from heavenbase.types import cosine_score

    backend = _embedded("snear")
    ws = hb.HeavenBase("surreal-near", backends={"s": backend})
    q = [1.0, 0.0, 0.0]
    data = [("a", "x", [1.0, 0.0, 0.0]), ("b", "x", [0.9, 0.1, 0.0]), ("c", "y", [0.5, 0.5, 0.0]), ("d", "y", [0.1, 0.9, 0.0])]
    emap = {oid: vec for oid, _, vec in data}
    try:
        ws.register(_NearLive)
        ws.upsert_many(_NearLive, [{"object_id": oid, "cat": cat, "emb": vec} for oid, cat, vec in data])
        expected = [oid for oid, _, _ in sorted(data, key=lambda r: -cosine_score(r[2], q))]
        near = ws.query(_NearLive).near(_NearLive.emb, q, top_k=3)
        frame = near.execute()
        assert near.explain()["steps"][0].get("handler_mode") == "native"  # native KNN, not scan
        assert frame.ids == expected[:3]  # ranking parity with the cosine scan scorer
        for row in frame.rows():
            assert abs(row["score"] - cosine_score(emap[row["object_id"]], q)) < 0.02  # ~ raw cosine (float32/ANN noise)
        assert len(ws.query(_NearLive).near(_NearLive.emb, q, top_k=2).execute().ids) == 2  # top_k honored
        # near co-located with a filter degrades to the (cosine) scan and stays correct
        assert sorted(ws.query(_NearLive).where(_NearLive.cat == "x").near(_NearLive.emb, q, top_k=3).execute().ids) == ["a", "b"]
        # a non-cosine metric is cosine-only natively, so explain() honestly reports py_scan and it ranks via the l2 scorer
        from heavenbase.types import euclidean_score

        l2 = ws.query(_NearLive).near(_NearLive.emb, q, top_k=3, metric="l2")
        assert l2.explain()["steps"][0].get("handler_mode") == "py_scan"
        assert l2.execute().ids == [oid for oid, _, _ in sorted(data, key=lambda r: -euclidean_score(r[2], q))][:3]
    finally:
        ws.drop()
        WORKSPACES.pop("surreal-near", None)


# -------------------------------------------------------------- P2: text search
def test_surreal_text_handler_native_vs_scan_routing():
    """Substring/affix text ops push to string functions; like/wildcard/regex and non-text dtypes scan (offline)."""
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.surreal.handler import surreal_filter_handler

    class _TD(hb.Entity):
        identifier = "unit-surreal-text-doc"

        title = hb.field(hb.ShortText)
        qty = hb.field(hb.Integer)

    f = _TD.schema().fields
    ctx = HandlerContext("s", "surreal", "inline")
    for op in ("contains", "match", "starts_with", "ends_with"):
        assert surreal_filter_handler(op)(f["title"], "db", ctx).notes["kind"] == "surreal_where"
    assert surreal_filter_handler("match")(f["title"], "DB", ctx).payload["value"] == "db"  # operand lowercased
    assert surreal_filter_handler("contains")(f["title"], "", ctx).notes["mode"] == "py_scan"  # empty operand -> scan
    assert surreal_filter_handler("contains")(f["qty"], "1", ctx).notes["mode"] == "py_scan"  # non-text dtype -> scan
    assert surreal_filter_handler("like")(f["title"], "d%", ctx).notes["mode"] == "py_scan"  # like/wildcard/regex stay scan
    assert surreal_filter_handler("regex")(f["title"], "^d", ctx).notes["mode"] == "py_scan"


class _TextDoc(hb.Entity):
    identifier = "surreal-text-doc"

    title = hb.field(hb.ShortText)
    body = hb.field(hb.LongText).optional()


def test_surreal_embedded_text_pushdown_parity():
    """Native string::contains/starts_with/ends_with return exactly what the substring scan would (embedded)."""
    backend = _embedded("stext")
    ws = hb.HeavenBase("surreal-text", backends={"s": backend})
    try:
        ws.register(_TextDoc)
        ws.upsert_many(
            _TextDoc,
            [
                {"object_id": "t1", "title": "Database Systems", "body": "intro to relational databases"},
                {"object_id": "t2", "title": "a DESK lamp", "body": "home goods"},
                {"object_id": "t3", "title": "database design", "body": None},  # None body
            ],
        )

        def native(query):
            assert query.explain()["steps"][0].get("handler_mode") == "native"
            return sorted(query.execute().ids)

        assert native(ws.query(_TextDoc).where(_TextDoc.title.contains("base"))) == ["t1", "t3"]  # case-sensitive substring
        assert native(ws.query(_TextDoc).where(_TextDoc.title.match("DATABASE"))) == ["t1", "t3"]  # case-insensitive
        assert native(ws.query(_TextDoc).where(_TextDoc.title.starts_with("Database"))) == ["t1"]
        assert native(ws.query(_TextDoc).where(_TextDoc.title.ends_with("design"))) == ["t3"]
        assert native(ws.query(_TextDoc).where(_TextDoc.body.contains("goods"))) == ["t2"]  # None body excluded by the guard
        # a non-pushable text op (regex) stays on the scan, still correct
        rx = ws.query(_TextDoc).where(_TextDoc.title.regex("^[Dd]atabase"))
        assert rx.explain()["steps"][0].get("handler_mode") == "py_scan"
        assert sorted(rx.execute().ids) == ["t1", "t3"]
        # The text storage role is an explicit static Backend claim.
        assert "text" in backend.tag("storage-roles").value
    finally:
        ws.drop()
        WORKSPACES.pop("surreal-text", None)


# ------------------------------------------------------ P3: compound types + graph
class _Citer(hb.Entity):
    identifier = "surreal-citer"

    name = hb.field(hb.ShortText)
    meta = hb.field(hb.Json).optional()
    cites = hb.field(hb.HyperG["cited" : hb.Identifier]).store(strategy=hb.GraphEdge(target="cited")).optional()


def test_surreal_embedded_compound_types_and_self_ref_graph():
    """Json/Array/HyperG decode natively while public traversal follows the canonical inline GraphEdge."""
    backend = _embedded("sg")
    assert backend.tag("graph-traversal-native").value is False
    ws = hb.HeavenBase("surreal-graph", backends={"s": backend})
    try:
        ws.register(_Citer)
        ws.upsert_many(
            _Citer,
            [
                {"object_id": "a", "name": "A", "meta": {"k": 1, "v": [2, 3]}, "cites": [{"cited": "b"}, {"cited": "c"}]},
                {"object_id": "b", "name": "B", "cites": [{"cited": "c"}]},
                {"object_id": "c", "name": "C", "cites": []},
            ],
        )
        # Json round-trips natively: the scan-filter must see a dict, not the workspace-encoded JSON string
        assert ws.get("a", entity=_Citer)["meta"] == {"k": 1, "v": [2, 3]}
        assert ws.query(_Citer).where(_Citer.meta == {"k": 1, "v": [2, 3]}).execute().ids == ["a"]
        # Canonical traversal: 1-hop out, multi-hop, in-direction, shortest path, routed traverse op.
        assert sorted(row["object_id"] for row in ws.traverse(_Citer.cites, "a")) == ["b", "c"]
        assert sorted(row["object_id"] for row in ws.traverse(_Citer.cites, "a", min_hops=1, max_hops=2)) == ["b", "c"]
        assert sorted(row["object_id"] for row in ws.traverse(_Citer.cites, "c", direction="in")) == ["a", "b"]  # who cites c
        assert ws.shortest_path(_Citer.cites, "a", "c") == [("surreal-citer", "a"), ("surreal-citer", "c")]
        assert sorted(ws.query(_Citer).traverse(_Citer.cites, "a").execute().ids) == ["b", "c"]
        # RELATE remains a provider-owned shadow projection for future readiness-gated routing.
        assert backend.graph_traverse_refs(_Citer.identifier, "a", "cites") == [(_Citer.identifier, "b"), (_Citer.identifier, "c")]
        with pytest.raises(hb.GraphDeleteRestrictedError):
            ws.delete((_Citer, "c"))
        assert ws.delete((_Citer, "c"), edges="detach") is True
        assert ws.get("a", entity=_Citer)["cites"] == [{"cited": "b"}]
        assert ws.get("b", entity=_Citer)["cites"] == []
        ws.upsert(_Citer, {"object_id": "c", "name": "C", "cites": []})
        assert sorted(row["object_id"] for row in ws.traverse(_Citer.cites, "a")) == ["b"]
        # full-list replacement keeps the inline and native relationship projections synchronized
        ws.upsert_many(_Citer, [{"object_id": "a", "name": "A", "cites": [{"cited": "b"}]}])
        assert sorted(row["object_id"] for row in ws.traverse(_Citer.cites, "a")) == ["b"]
        # omitting an optional GraphEdge field on a full-row replace clears it inline AND natively (no desync)
        ws.upsert_many(_Citer, [{"object_id": "a", "name": "A3"}])
        assert ws.get("a", entity=_Citer).get("cites") in (None, [])
        assert [row["object_id"] for row in ws.traverse(_Citer.cites, "a")] == []
    finally:
        ws.drop()
        WORKSPACES.pop("surreal-graph", None)


class _GAuthor(hb.Entity):
    identifier = "surreal-g-author"

    name = hb.field(hb.ShortText)
    wrote = hb.field(hb.HyperG["paper" : hb.Identifier["surreal-g-paper"], "year" : hb.Integer]).store(strategy=hb.GraphEdge(target="paper")).optional()


class _GPaper(hb.Entity):
    identifier = "surreal-g-paper"

    title = hb.field(hb.ShortText)


@pytest.mark.full
def test_surreal_embedded_graph_lifecycle_cross_entity_and_parallel_reject():
    """Canonical cross-entity traversal, lifecycle, edge props, and parallel-edge rejection (embedded)."""
    backend = _embedded("sgx")
    ws = hb.HeavenBase("surreal-gx", backends={"s": backend})
    try:
        ws.register(_GPaper)
        ws.register(_GAuthor)
        ws.upsert_many(_GPaper, [{"object_id": "p1", "title": "P1"}, {"object_id": "p2", "title": "P2"}])
        ws.upsert_many(_GAuthor, [{"object_id": "a1", "name": "A1", "wrote": [{"paper": "p1", "year": 2020}, {"paper": "p2", "year": 2021}]}])
        assert ws.get("p1", entity=_GPaper)["title"] == "P1"  # merge-only identity anchoring preserves a co-located full row
        assert sorted(row["object_id"] for row in ws.traverse(_GAuthor.wrote, "a1")) == ["p1", "p2"]  # out: author -> papers
        assert sorted(row["object_id"] for row in ws.traverse(_GAuthor.wrote, "p1", direction="in")) == ["a1"]  # in: who wrote p1
        assert ws.shortest_path(_GAuthor.wrote, "a1", "p2") == [("surreal-g-author", "a1"), ("surreal-g-paper", "p2")]
        assert ws.get("a1", entity=_GAuthor)["wrote"] == [{"paper": "p1", "year": 2020}, {"paper": "p2", "year": 2021}]  # inline props round-trip
        with pytest.raises(hb.GraphDeleteRestrictedError):
            ws.delete((_GPaper, "p1"))
        assert ws.delete((_GPaper, "p1"), edges="detach") is True
        assert ws.get("a1", entity=_GAuthor)["wrote"] == [{"paper": "p2", "year": 2021}]
        ws.upsert(_GPaper, {"object_id": "p1", "title": "P1 recreated"})
        assert ws.get("a1", entity=_GAuthor)["wrote"] == [{"paper": "p2", "year": 2021}]
        # a GraphEdge models one edge per (source, target): two edges to the same paper are rejected loudly
        with pytest.raises(ValueError):
            ws.upsert_many(_GAuthor, [{"object_id": "a2", "name": "A2", "wrote": [{"paper": "p1", "year": 1}, {"paper": "p1", "year": 2}]}])
        # the rejected upsert is validated before any write, so it leaves no partial record and no dangling edge
        assert ws.get("a2", entity=_GAuthor) is None
        assert ws.traverse(_GAuthor.wrote, "p1", direction="in") == []
    finally:
        ws.drop()
        WORKSPACES.pop("surreal-gx", None)


class _SplitAnchorPaper(hb.Entity):
    identifier = "surreal-split-anchor-paper"

    title = hb.field(hb.ShortText).store(to="row")


class _SplitAnchorAuthor(hb.Entity):
    identifier = "surreal-split-anchor-author"

    name = hb.field(hb.ShortText).store(to="row")
    wrote = (
        hb.field(hb.HyperG["paper" : hb.Identifier[_SplitAnchorPaper], "year" : hb.Integer]).store(to="graph", strategy=hb.GraphEdge(target="paper")).optional()
    )


@pytest.mark.full
def test_surreal_embedded_split_routes_use_non_authoritative_endpoint_anchors():
    """The derived shadow stays coherent when target rows live only on a different logical route."""
    backend = _embedded("graph")
    ws = hb.HeavenBase(
        "surreal-split-anchor",
        backends={
            "row": hb.backends.InMemBackend("row"),
            "graph": backend,
        },
    )
    try:
        ws.register(_SplitAnchorPaper)
        ws.register(_SplitAnchorAuthor)
        ws.upsert_many(
            _SplitAnchorAuthor,
            [
                _SplitAnchorAuthor.from_dict(
                    object_id="a1",
                    name="A1",
                    wrote=[{"paper": "p1", "year": 2020}],
                ),
                _SplitAnchorPaper.from_dict(object_id="p1", title="P1"),
                _SplitAnchorPaper.from_dict(object_id="p2", title="P2"),
                _SplitAnchorAuthor.from_dict(
                    object_id="a2",
                    name="A2",
                    wrote=[{"paper": "p2", "year": 2021}],
                ),
            ],
        )

        assert [row["object_id"] for row in ws.traverse(_SplitAnchorAuthor.wrote, "a1")] == ["p1"]
        assert [row["object_id"] for row in ws.traverse(_SplitAnchorAuthor.wrote, "p1", direction="in")] == ["a1"]
        assert ws.shortest_path(_SplitAnchorAuthor.wrote, "a1", "p1") == [
            (_SplitAnchorAuthor.identifier, "a1"),
            (_SplitAnchorPaper.identifier, "p1"),
        ]
        anchor = backend.get_many(_SplitAnchorPaper.identifier, ["p1"])[0]
        assert anchor == {"object_id": "p1"}
        assert ws.get("p1", entity=_SplitAnchorPaper)["title"] == "P1"

        assert ws.delete((_SplitAnchorPaper, "p1"), edges="detach") is True
        assert backend.get_many(_SplitAnchorPaper.identifier, ["p1"])[0] is None
        assert ws.get("a1", entity=_SplitAnchorAuthor)["wrote"] == []
        assert [row["object_id"] for row in ws.traverse(_SplitAnchorAuthor.wrote, "a2")] == ["p2"]

        ws.upsert(_SplitAnchorPaper, {"object_id": "p1", "title": "P1 recreated"})
        assert ws.traverse(_SplitAnchorAuthor.wrote, "a1") == []
    finally:
        ws.drop()
        WORKSPACES.pop("surreal-split-anchor", None)


@pytest.mark.full
def test_surreal_legacy_projection_drift_cannot_change_public_graph_lifecycle():
    """Canonical inline edges govern traversal and deletion when a legacy RELATE projection is missing or stale."""
    backend = _embedded("graph")
    from surrealdb import RecordID

    ws_id = "surreal-legacy-projection"
    ws = hb.HeavenBase(
        ws_id,
        backends={
            "row": hb.backends.InMemBackend("row"),
            "graph": backend,
        },
    )
    try:
        ws.register(_SplitAnchorPaper)
        ws.register(_SplitAnchorAuthor)
        ws.upsert_many(
            _SplitAnchorAuthor,
            [
                _SplitAnchorAuthor.from_dict(object_id="a1", name="A1", wrote=[{"paper": "p1", "year": 2020}]),
                _SplitAnchorPaper.from_dict(object_id="p1", title="P1"),
            ],
        )
        backend.delete(_SplitAnchorPaper.identifier, ["p1"])

        assert backend.graph_traverse_refs(_SplitAnchorAuthor.identifier, "a1", "wrote") == []
        assert [row["object_id"] for row in ws.traverse(_SplitAnchorAuthor.wrote, "a1")] == ["p1"]
        assert ws.shortest_path(_SplitAnchorAuthor.wrote, "a1", "p1") == [
            (_SplitAnchorAuthor.identifier, "a1"),
            (_SplitAnchorPaper.identifier, "p1"),
        ]
        assert ws.check_graph() == []
        with pytest.raises(hb.GraphDeleteRestrictedError):
            ws.delete((_SplitAnchorPaper, "p1"))
        assert ws.exists("p1", entity=_SplitAnchorPaper) is True

        assert ws.delete((_SplitAnchorPaper, "p1"), edges="detach") is True
        assert ws.get("a1", entity=_SplitAnchorAuthor)["wrote"] == []
        ws.upsert(_SplitAnchorPaper, {"object_id": "p1", "title": "P1 recreated"})
        assert ws.traverse(_SplitAnchorAuthor.wrote, "a1") == []

        spec = backend._graph_specs[_SplitAnchorAuthor.identifier]["wrote"]
        source = RecordID(spec["source_table"], "a1")
        target = RecordID(spec["target_table"], "p1")
        backend._query(
            f"UPSERT $target MERGE $target_values; RELATE $source->{backend._ident(spec['edge_table'])}->$target CONTENT $edge_values;",
            {
                "source": source,
                "target": target,
                "target_values": {"object_id": "p1"},
                "edge_values": {"year": 1999},
            },
        )
        assert backend.graph_traverse_refs(_SplitAnchorAuthor.identifier, "a1", "wrote") == [(_SplitAnchorPaper.identifier, "p1")]
        assert ws.traverse(_SplitAnchorAuthor.wrote, "a1") == []
        assert ws.shortest_path(_SplitAnchorAuthor.wrote, "a1", "p1") is None
        assert ws.delete((_SplitAnchorPaper, "p1")) is True
    finally:
        ws.drop()
        WORKSPACES.pop(ws_id, None)


def test_surreal_upsert_apply_phase_is_atomic():
    """An arbitrary mid-batch DB error during the apply phase rolls the whole batch back (no partial writes).

    The record/edge writes run inside one ``BEGIN … COMMIT`` transaction, so a runtime failure on a later
    statement must leave none of the earlier rows' records or edges behind. We splice a ``THROW`` in just
    before ``COMMIT`` to stand in for an arbitrary DB error after the earlier rows' writes have run.
    """
    backend = _embedded("satomic")
    schema = _Citer.schema()
    backend.ensure(schema, [])  # schema-only ensure is enough for the inline-record writes this test makes
    # Seed one committed row so we can prove the failed batch neither wrote new rows nor touched this one.
    backend.upsert([RowOp(schema.entity_id, "keep", {"name": "KEEP"})])
    original_query = backend._query

    def throwing_query(surql, variables=None):  # simulate an arbitrary DB error mid-transaction
        if surql.startswith("BEGIN TRANSACTION"):
            surql = surql.replace("COMMIT TRANSACTION;", "THROW 'mid-batch boom';\nCOMMIT TRANSACTION;")
        return original_query(surql, variables)

    backend._query = throwing_query
    try:
        with pytest.raises(Exception):  # the failed transaction surfaces as an error
            backend.upsert([RowOp(schema.entity_id, "x", {"name": "X"}), RowOp(schema.entity_id, "y", {"name": "Y"})])
    finally:
        backend._query = original_query
    # No partial writes: the earlier rows of the rolled-back batch left nothing, and the seeded row is intact.
    assert backend.count(schema.entity_id) == 1
    assert backend.exists_many(schema.entity_id, ["x", "y"]) == [False, False]
    (kept,) = backend.get_many(schema.entity_id, ["keep"])
    assert kept["name"] == "KEEP"
    backend.drop()


@pytest.mark.full
def test_surreal_provider_batch_preserves_later_endpoint_delete():
    """A source upsert followed by endpoint deletion must not recreate the endpoint anchor."""
    from types import SimpleNamespace

    backend = _embedded("sordered")
    schema = _Citer.schema()
    backend.ensure(
        schema,
        [SimpleNamespace(backend=backend.name, field_name="cites", strategy=hb.GraphEdge.identifier, params={"target": "cited"})],
    )

    backend.upsert(
        [
            RowOp(schema.entity_id, "source", {"name": "Source", "cites": [{"cited": "target"}]}),
            RowOp(schema.entity_id, "target", op="delete"),
        ]
    )

    assert backend.get_many(schema.entity_id, ["target"])[0] is None
    assert backend.graph_traverse_refs(schema.entity_id, "source", "cites") == []
    backend.drop()


@pytest.mark.full
def test_surreal_traverse_endpoint_filter_explains_portable_postfilter():
    """Unversioned RELATE projections never make public explain claim native traversal."""
    backend = _embedded("sgef")
    ws = hb.HeavenBase("surreal-gef", backends={"s": backend})
    try:
        ws.register(_Citer)
        ws.upsert_many(
            _Citer,
            [
                {"object_id": "a", "name": "A", "cites": [{"cited": "b"}, {"cited": "c"}]},
                {"object_id": "b", "name": "B", "cites": []},
                {"object_id": "c", "name": "C", "cites": []},
            ],
        )
        q = ws.query(_Citer).traverse(_Citer.cites, "a").where(_Citer.name == "B")
        step = [s for s in q.explain()["steps"] if s.get("node") == "traverse"][0]
        assert step["mode"] == "py_traverse"
        assert step["endpoint_filter"] == "postfilter"
        assert sorted(q.execute().ids) == ["b"]
    finally:
        ws.drop()
        WORKSPACES.pop("surreal-gef", None)


def test_surreal_traverse_endpoint_pushdown_parity():
    """The internal RELATE projection keeps endpoint-pushdown parity for a future readiness gate.

    Pushed and unpushed queries must agree within the same coherent shadow projection; this does not
    assert parity with canonical inline state or public routing. The probe covers multi-hop, in-direction,
    ``in``/range ops, and a 2-hop walk whose interior node fails the predicate but still reaches a deeper
    passing node.
    """
    backend = _embedded("sgepp")
    entity_id = _Citer.schema().entity_id
    ws = hb.HeavenBase("surreal-gepp", backends={"s": backend})
    try:
        ws.register(_Citer)
        # a -> {b, c}; b -> d; c -> e. Names tag the endpoints; the 2-hop walk a..e crosses interior c ("C")
        # to reach e ("E"), so it exercises the interior-fails / endpoint-passes case.
        ws.upsert_many(
            _Citer,
            [
                {"object_id": "a", "name": "A", "cites": [{"cited": "b"}, {"cited": "c"}]},
                {"object_id": "b", "name": "B", "cites": [{"cited": "d"}]},
                {"object_id": "c", "name": "C", "cites": [{"cited": "e"}]},
                {"object_id": "d", "name": "D", "cites": []},
                {"object_id": "e", "name": "E", "cites": []},
            ],
        )
        # eq pushdown: 1-hop out from a, only "B"-named endpoint survives the native prune
        pushed = backend.graph_traverse_refs(entity_id, "a", "cites", endpoint_preds=[("name", "eq", "B")])
        unpushed = [ref for ref in backend.graph_traverse_refs(entity_id, "a", "cites") if ref[1] == "b"]
        assert pushed == unpushed == [(entity_id, "b")]
        # in-op pushdown over the full out-reach
        assert backend.graph_traverse_refs(entity_id, "a", "cites", endpoint_preds=[("name", "in", ["B", "C"])]) == sorted(
            ref for ref in backend.graph_traverse_refs(entity_id, "a", "cites") if ref[1] in ("b", "c")
        )
        # interior-fails / endpoint-passes: a -2hops-> e travels through c (name "C") to e (name "E").
        # pushing name == "E" must still reach e, because the frontier advances unpruned through c.
        two_hop = backend.graph_traverse_refs(entity_id, "a", "cites", min_hops=2, max_hops=2)
        pushed_e = backend.graph_traverse_refs(entity_id, "a", "cites", min_hops=2, max_hops=2, endpoint_preds=[("name", "eq", "E")])
        assert (entity_id, "e") in two_hop  # d and e are the 2-hop reach
        assert pushed_e == [(entity_id, "e")]  # native prune keeps e even though interior c fails the pred
        # in-direction pushdown (who cites b) with an endpoint pred on the source
        assert backend.graph_traverse_refs(entity_id, "b", "cites", direction="in", endpoint_preds=[("name", "eq", "A")]) == [(entity_id, "a")]
    finally:
        ws.drop()
        WORKSPACES.pop("surreal-gepp", None)


# ----------------------------------------------------- live (real ws:// server) — @external
# The embedded tests above cover the same surface in-process; these re-run it against a real
# SurrealDB server so the network/auth transport (signin -> use -> query) is exercised too. They
# skip when the driver is missing, the configured host/port is unreachable, or signin fails, so the
# default suite stays green with no server. Distinct ``docker-surreal-*`` entity ids keep these
# databases from colliding with the embedded tests on a shared server.


def _reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _live_backend(name: str, *, ws_id: str | None = None) -> SurrealBackend:
    """Return a connected ``ws://`` SurrealBackend, or skip when the driver/server/auth is unavailable."""
    if find_spec("surrealdb") is None:
        pytest.skip("surrealdb driver is not installed")
    parsed = urlparse(_SURREAL_URL)  # probe the CONFIGURED host/port (HVNB_SURREAL_URL), not a hard-coded localhost
    host, port = parsed.hostname or "localhost", parsed.port or 8000
    if not _reachable(host, port):
        pytest.skip(f"surrealdb server is not reachable on {host}:{port}")
    backend = SurrealBackend(
        name,
        url=_SURREAL_URL,
        ws_id=ws_id,
        user=os.environ.get("HVNB_SURREAL_USER", "root"),
        password=os.environ.get("HVNB_SURREAL_PASSWORD", "root"),
    )
    if backend.client is None:
        pytest.skip(f"surrealdb connect/auth failed: {backend.client_error}")
    return backend


@pytest.mark.external
def test_surreal_live_split_routes_use_non_authoritative_endpoint_anchors():
    """The split-route anchor contract also holds against the ws:// provider."""
    graph = _live_backend("graph")
    ws = hb.HeavenBase(
        "docker-surreal-split-anchor",
        backends={
            "row": hb.backends.InMemBackend("row"),
            "graph": graph,
        },
    )
    try:
        ws.drop()
        ws.register(_SplitAnchorPaper)
        ws.register(_SplitAnchorAuthor)
        ws.upsert_many(
            _SplitAnchorAuthor,
            [
                _SplitAnchorAuthor.from_dict(
                    object_id="a1",
                    name="A1",
                    wrote=[{"paper": "p1", "year": 2020}],
                ),
                _SplitAnchorPaper.from_dict(object_id="p1", title="P1"),
            ],
        )
        assert [row["object_id"] for row in ws.traverse(_SplitAnchorAuthor.wrote, "a1")] == ["p1"]
        assert graph.get_many(_SplitAnchorPaper.identifier, ["p1"])[0] == {"object_id": "p1"}

        assert ws.delete((_SplitAnchorPaper, "p1"), edges="detach") is True
        assert graph.get_many(_SplitAnchorPaper.identifier, ["p1"])[0] is None
        assert ws.get("a1", entity=_SplitAnchorAuthor)["wrote"] == []
    finally:
        ws.drop()
        WORKSPACES.pop("docker-surreal-split-anchor", None)


class _LiveThing(hb.Entity):
    identifier = "docker-surreal-thing"

    name = hb.field(hb.ShortText)
    score = hb.field(hb.Float)


@pytest.mark.external
def test_surreal_live_backend_crud_and_namespacing():
    """Backend-level CRUD round-trips over ws://, full-row replace drops stale fields, namespaces isolate."""
    backend = _live_backend("sc", ws_id="nsa")
    other = _live_backend("sc", ws_id="nsb")  # same instance name, different workspace -> isolated database
    schema = _LiveThing.schema()
    for instance in (backend, other):
        instance.ensure(schema, [])
        instance.drop()
        instance.ensure(schema, [])
    try:
        backend.upsert(
            [
                RowOp(schema.entity_id, "a", {"name": "alpha", "score": 1.0}),
                RowOp(schema.entity_id, "b", {"name": "beta", "score": 2.0}),
            ]
        )
        rows = {row["object_id"]: row for row in backend.scan(schema.entity_id)}
        assert set(rows) == {"a", "b"} and rows["a"]["name"] == "alpha" and rows["a"]["score"] == 1.0
        assert backend.count(schema.entity_id) == 2
        assert other.count(schema.entity_id) == 0  # different workspace database -> isolated

        got = backend.get_many(schema.entity_id, ["b", "missing", "a"])
        assert [row["object_id"] if row else None for row in got] == ["b", None, "a"]
        assert backend.exists_many(schema.entity_id, ["a", "missing"]) == [True, False]

        backend.upsert([RowOp(schema.entity_id, "a", {"name": "alpha2"})])  # full-row replace drops stale score
        (row,) = backend.get_many(schema.entity_id, ["a"])
        assert row["name"] == "alpha2" and row.get("score") is None

        backend.upsert([RowOp(schema.entity_id, "a", {}, op="delete")])
        assert backend.count(schema.entity_id) == 1
        assert backend.exists_many(schema.entity_id, ["a"]) == [False]
    finally:
        backend.drop()
        other.drop()


@pytest.mark.external
def test_surreal_live_workspace_query_pipeline():
    """End-to-end through the workspace over ws://: upsert, then scalar filters via the routed pipeline."""
    ws = hb.HeavenBase("docker-surreal-q", backends={"s": _live_backend("sq")})
    try:
        ws.drop()
        ws.register(_LiveThing)
        ws.upsert_many(
            _LiveThing,
            [
                {"object_id": "a", "name": "alpha", "score": 1.0},
                {"object_id": "b", "name": "beta", "score": 2.0},
                {"object_id": "c", "name": "gamma", "score": 3.0},
            ],
        )
        assert sorted(ws.query(_LiveThing).execute().ids) == ["a", "b", "c"]
        assert sorted(ws.query(_LiveThing).where(_LiveThing.score > 1.5).execute().ids) == ["b", "c"]
        assert ws.query(_LiveThing).where(_LiveThing.name == "alpha").execute().ids == ["a"]
    finally:
        ws.drop()
        WORKSPACES.pop("docker-surreal-q", None)


class _LivePushdownDoc(hb.Entity):
    identifier = "docker-surreal-pushdown-doc"

    name = hb.field(hb.ShortText)
    price = hb.field(hb.Float)
    qty = hb.field(hb.Integer)
    tag = hb.field(hb.ShortText).optional()


@pytest.mark.external
def test_surreal_live_native_pushdown_parity():
    """Native SurrealQL eq/in/range pushdown over ws:// returns exactly what the scan predicate would."""
    ws = hb.HeavenBase("docker-surreal-pd", backends={"s": _live_backend("spd")})
    try:
        ws.drop()
        ws.register(_LivePushdownDoc)
        ws.upsert_many(
            _LivePushdownDoc,
            [
                {"object_id": "p1", "name": "desk", "price": 89.0, "qty": 5, "tag": "A"},
                {"object_id": "p2", "name": "lamp", "price": 19.0, "qty": 0, "tag": "A"},
                {"object_id": "p3", "name": "sofa", "price": 299.0, "qty": 3},  # tag absent (None)
                {"object_id": "p4", "name": "desk", "price": 150.0, "qty": 5, "tag": "B"},
            ],
        )

        def native(query):
            assert query.explain()["steps"][0].get("handler_mode") == "native"
            return sorted(query.execute().ids)

        assert native(ws.query(_LivePushdownDoc).where(_LivePushdownDoc.name == "desk")) == ["p1", "p4"]
        assert native(ws.query(_LivePushdownDoc).where(_LivePushdownDoc.qty.in_([0, 5]))) == ["p1", "p2", "p4"]
        assert native(ws.query(_LivePushdownDoc).where(_LivePushdownDoc.price < 100)) == ["p1", "p2"]
        assert native(ws.query(_LivePushdownDoc).where(_LivePushdownDoc.price >= 150)) == ["p3", "p4"]
        assert native(ws.query(_LivePushdownDoc).where(_LivePushdownDoc.tag == "A")) == ["p1", "p2"]  # p3 (None) excluded
        assert sorted(ws.query(_LivePushdownDoc).where((_LivePushdownDoc.price > 50) & (_LivePushdownDoc.price < 200)).execute().ids) == ["p1", "p4"]
        assert sorted(ws.query(_LivePushdownDoc).where((_LivePushdownDoc.name == "lamp") | (_LivePushdownDoc.qty == 3)).execute().ids) == ["p2", "p3"]
        # a regex op cannot push down -> scan fallback, still correct
        rx = ws.query(_LivePushdownDoc).where(_LivePushdownDoc.name.regex("^des"))
        assert rx.explain()["steps"][0].get("handler_mode") == "py_scan"
        assert sorted(rx.execute().ids) == ["p1", "p4"]
    finally:
        ws.drop()
        WORKSPACES.pop("docker-surreal-pd", None)


class _LiveTextDoc(hb.Entity):
    identifier = "docker-surreal-text-doc"

    title = hb.field(hb.ShortText)
    body = hb.field(hb.LongText).optional()


@pytest.mark.external
def test_surreal_live_text_pushdown_parity():
    """Native string::contains/starts_with/ends_with over ws:// return exactly what the substring scan would."""
    ws = hb.HeavenBase("docker-surreal-text", backends={"s": _live_backend("stext")})
    try:
        ws.drop()
        ws.register(_LiveTextDoc)
        ws.upsert_many(
            _LiveTextDoc,
            [
                {"object_id": "t1", "title": "Database Systems", "body": "intro to relational databases"},
                {"object_id": "t2", "title": "a DESK lamp", "body": "home goods"},
                {"object_id": "t3", "title": "database design", "body": None},  # None body
            ],
        )

        def native(query):
            assert query.explain()["steps"][0].get("handler_mode") == "native"
            return sorted(query.execute().ids)

        assert native(ws.query(_LiveTextDoc).where(_LiveTextDoc.title.contains("base"))) == ["t1", "t3"]  # case-sensitive substring
        assert native(ws.query(_LiveTextDoc).where(_LiveTextDoc.title.match("DATABASE"))) == ["t1", "t3"]  # case-insensitive
        assert native(ws.query(_LiveTextDoc).where(_LiveTextDoc.title.starts_with("Database"))) == ["t1"]
        assert native(ws.query(_LiveTextDoc).where(_LiveTextDoc.title.ends_with("design"))) == ["t3"]
        assert native(ws.query(_LiveTextDoc).where(_LiveTextDoc.body.contains("goods"))) == ["t2"]  # None body excluded by the guard
        rx = ws.query(_LiveTextDoc).where(_LiveTextDoc.title.regex("^[Dd]atabase"))
        assert rx.explain()["steps"][0].get("handler_mode") == "py_scan"
        assert sorted(rx.execute().ids) == ["t1", "t3"]
    finally:
        ws.drop()
        WORKSPACES.pop("docker-surreal-text", None)


class _LiveNearDoc(hb.Entity):
    identifier = "docker-surreal-near-doc"

    cat = hb.field(hb.ShortText)
    emb = hb.field(hb.Vector[3])


@pytest.mark.external
def test_surreal_live_vector_near_parity():
    """Native cosine HNSW KNN over ws:// returns the same top-k as the cosine scan; near+filter degrades to scan."""
    from heavenbase.types import cosine_score, euclidean_score

    ws = hb.HeavenBase("docker-surreal-near", backends={"s": _live_backend("snear")})
    q = [1.0, 0.0, 0.0]
    data = [("a", "x", [1.0, 0.0, 0.0]), ("b", "x", [0.9, 0.1, 0.0]), ("c", "y", [0.5, 0.5, 0.0]), ("d", "y", [0.1, 0.9, 0.0])]
    emap = {oid: vec for oid, _, vec in data}
    try:
        ws.drop()
        ws.register(_LiveNearDoc)
        ws.upsert_many(_LiveNearDoc, [{"object_id": oid, "cat": cat, "emb": vec} for oid, cat, vec in data])
        expected = [oid for oid, _, _ in sorted(data, key=lambda r: -cosine_score(r[2], q))]
        near = ws.query(_LiveNearDoc).near(_LiveNearDoc.emb, q, top_k=3)
        frame = near.execute()
        assert near.explain()["steps"][0].get("handler_mode") == "native"  # native KNN, not scan
        assert frame.ids == expected[:3]  # ranking parity with the cosine scan scorer
        for row in frame.rows():
            assert abs(row["score"] - cosine_score(emap[row["object_id"]], q)) < 0.02  # ~ raw cosine (float32/ANN noise)
        assert len(ws.query(_LiveNearDoc).near(_LiveNearDoc.emb, q, top_k=2).execute().ids) == 2  # top_k honored
        # near co-located with a filter degrades to the (cosine) scan and stays correct
        assert sorted(ws.query(_LiveNearDoc).where(_LiveNearDoc.cat == "x").near(_LiveNearDoc.emb, q, top_k=3).execute().ids) == ["a", "b"]
        # a non-cosine metric is cosine-only natively, so explain() honestly reports py_scan and it ranks via the l2 scorer
        l2 = ws.query(_LiveNearDoc).near(_LiveNearDoc.emb, q, top_k=3, metric="l2")
        assert l2.explain()["steps"][0].get("handler_mode") == "py_scan"
        assert l2.execute().ids == [oid for oid, _, _ in sorted(data, key=lambda r: -euclidean_score(r[2], q))][:3]
    finally:
        ws.drop()
        WORKSPACES.pop("docker-surreal-near", None)


class _LiveCiter(hb.Entity):
    identifier = "docker-surreal-citer"

    name = hb.field(hb.ShortText)
    meta = hb.field(hb.Json).optional()
    cites = hb.field(hb.HyperG["cited" : hb.Identifier]).store(strategy=hb.GraphEdge(target="cited")).optional()


@pytest.mark.external
def test_surreal_live_compound_types_and_self_ref_graph():
    """Compound codec and canonical public traversal agree with the live RELATE shadow projection."""
    backend = _live_backend("sg")
    assert backend.tag("graph-traversal-native").value is False
    ws = hb.HeavenBase("docker-surreal-graph", backends={"s": backend})
    try:
        ws.drop()
        ws.register(_LiveCiter)
        ws.upsert_many(
            _LiveCiter,
            [
                {"object_id": "a", "name": "A", "meta": {"k": 1, "v": [2, 3]}, "cites": [{"cited": "b"}, {"cited": "c"}]},
                {"object_id": "b", "name": "B", "cites": [{"cited": "c"}]},
                {"object_id": "c", "name": "C", "cites": []},
            ],
        )
        # Json round-trips natively: the scan-filter must see a dict, not the workspace-encoded JSON string
        assert ws.get("a", entity=_LiveCiter)["meta"] == {"k": 1, "v": [2, 3]}
        assert ws.query(_LiveCiter).where(_LiveCiter.meta == {"k": 1, "v": [2, 3]}).execute().ids == ["a"]
        assert sorted(row["object_id"] for row in ws.traverse(_LiveCiter.cites, "a")) == ["b", "c"]
        assert backend.graph_traverse_refs(_LiveCiter.identifier, "a", "cites") == [(_LiveCiter.identifier, "b"), (_LiveCiter.identifier, "c")]
        assert sorted(row["object_id"] for row in ws.traverse(_LiveCiter.cites, "a", min_hops=1, max_hops=2)) == ["b", "c"]
        assert sorted(row["object_id"] for row in ws.traverse(_LiveCiter.cites, "c", direction="in")) == ["a", "b"]  # who cites c
        assert ws.shortest_path(_LiveCiter.cites, "a", "c") == [("docker-surreal-citer", "a"), ("docker-surreal-citer", "c")]
        assert sorted(ws.query(_LiveCiter).traverse(_LiveCiter.cites, "a").execute().ids) == ["b", "c"]
        with pytest.raises(hb.GraphDeleteRestrictedError):
            ws.delete((_LiveCiter, "c"))
        assert ws.delete((_LiveCiter, "c"), edges="detach") is True
        assert ws.get("a", entity=_LiveCiter)["cites"] == [{"cited": "b"}]
        assert ws.get("b", entity=_LiveCiter)["cites"] == []
        ws.upsert(_LiveCiter, {"object_id": "c", "name": "C", "cites": []})
        assert sorted(row["object_id"] for row in ws.traverse(_LiveCiter.cites, "a")) == ["b"]
        # omitting an optional GraphEdge field on a full-row replace clears it inline AND natively (no desync)
        ws.upsert_many(_LiveCiter, [{"object_id": "a", "name": "A3"}])
        assert ws.get("a", entity=_LiveCiter).get("cites") in (None, [])
        assert [row["object_id"] for row in ws.traverse(_LiveCiter.cites, "a")] == []
    finally:
        ws.drop()
        WORKSPACES.pop("docker-surreal-graph", None)


class _LiveGAuthor(hb.Entity):
    identifier = "docker-surreal-g-author"

    name = hb.field(hb.ShortText)
    wrote = hb.field(hb.HyperG["paper" : hb.Identifier["docker-surreal-g-paper"], "year" : hb.Integer]).store(strategy=hb.GraphEdge(target="paper")).optional()


class _LiveGPaper(hb.Entity):
    identifier = "docker-surreal-g-paper"

    title = hb.field(hb.ShortText)


@pytest.mark.external
def test_surreal_live_graph_lifecycle_cross_entity_and_parallel_reject():
    """Canonical cross-entity traversal, lifecycle, edge props, and parallel-edge rejection over ws://."""
    ws = hb.HeavenBase("docker-surreal-gx", backends={"s": _live_backend("sgx")})
    try:
        ws.drop()
        ws.register(_LiveGPaper)
        ws.register(_LiveGAuthor)
        ws.upsert_many(_LiveGPaper, [{"object_id": "p1", "title": "P1"}, {"object_id": "p2", "title": "P2"}])
        ws.upsert_many(_LiveGAuthor, [{"object_id": "a1", "name": "A1", "wrote": [{"paper": "p1", "year": 2020}, {"paper": "p2", "year": 2021}]}])
        assert ws.get("p1", entity=_LiveGPaper)["title"] == "P1"  # merge-only identity anchoring preserves a co-located full row
        assert sorted(row["object_id"] for row in ws.traverse(_LiveGAuthor.wrote, "a1")) == ["p1", "p2"]  # out: author -> papers
        assert sorted(row["object_id"] for row in ws.traverse(_LiveGAuthor.wrote, "p1", direction="in")) == ["a1"]  # in: who wrote p1
        assert ws.shortest_path(_LiveGAuthor.wrote, "a1", "p2") == [("docker-surreal-g-author", "a1"), ("docker-surreal-g-paper", "p2")]
        assert ws.get("a1", entity=_LiveGAuthor)["wrote"] == [{"paper": "p1", "year": 2020}, {"paper": "p2", "year": 2021}]  # inline props round-trip
        with pytest.raises(hb.GraphDeleteRestrictedError):
            ws.delete((_LiveGPaper, "p1"))
        assert ws.delete((_LiveGPaper, "p1"), edges="detach") is True
        assert ws.get("a1", entity=_LiveGAuthor)["wrote"] == [{"paper": "p2", "year": 2021}]
        ws.upsert(_LiveGPaper, {"object_id": "p1", "title": "P1 recreated"})
        assert ws.get("a1", entity=_LiveGAuthor)["wrote"] == [{"paper": "p2", "year": 2021}]
        # a GraphEdge models one edge per (source, target): two edges to the same paper are rejected loudly
        with pytest.raises(ValueError):
            ws.upsert_many(_LiveGAuthor, [{"object_id": "a2", "name": "A2", "wrote": [{"paper": "p1", "year": 1}, {"paper": "p1", "year": 2}]}])
        # the rejected upsert is validated before any write, so it leaves no partial record and no dangling edge
        assert ws.get("a2", entity=_LiveGAuthor) is None
        assert ws.traverse(_LiveGAuthor.wrote, "p1", direction="in") == []
    finally:
        ws.drop()
        WORKSPACES.pop("docker-surreal-gx", None)
