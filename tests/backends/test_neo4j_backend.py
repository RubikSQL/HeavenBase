"""Offline tests for the Neo4j entity/row backend.

These cover the parts that need no server or ``neo4j`` driver: registry wiring, the
per-logical-type property codec, workspace+instance label namespacing, and the
provider-unavailable degrade path (the driver is imported lazily inside ``_connect``,
so an absent driver or unreachable server degrades to ``driver=None`` rather than
raising at construction). Live behavior (MERGE/scan/constraints against a real Neo4j)
belongs in a separate ``@pytest.mark.external`` suite.
"""

import os
import socket
import sys
from importlib.util import find_spec
from urllib.parse import urlparse

import pytest

sys.path.insert(0, "src")

import heavenbase as hb
from heavenbase.backends import Neo4jBackend
from heavenbase.execution import RowOp

WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry

_OFFLINE_URL = "bolt://127.0.0.1:1"  # nothing listens here -> connect refused fast; driver also absent -> degrade
_NEO4J_URL = os.environ.get("HVNB_NEO4J_URL", "bolt://localhost:7687")  # credentials come from CM_HVNB / HVNB_NEO4J_* env


def test_neo4j_backend_is_registered():
    """neo4j publishes its Backend tags and structural handler record."""
    modules = hb.DEFAULT_CONTEXT.modules()
    ids = hb.BackendType.identifiers()
    assert "neo4j" in ids
    assert "row" in modules.tag("backend_type", "neo4j", "storage-roles").value
    assert modules.tag("backend_type", "neo4j", "graph").value is True
    assert modules.inspect("handler", "neo4j")


def test_neo4j_degrades_when_unreachable():
    """An absent driver or unreachable server degrades to driver=None; ops raise lazily, ensure is a no-op."""
    backend = Neo4jBackend("neo4j", url=_OFFLINE_URL)
    assert backend.driver is None
    assert backend.client_error  # captured connect/import error
    assert backend.to_dict()["connected"] is False
    assert backend.tag("row-lookup").value == {"get_many": "native", "exists_many": "native", "count": "native"}
    assert backend.tag("backend-availability").value == "unavailable"
    with pytest.raises(RuntimeError):
        backend.scan("ent")
    with pytest.raises(RuntimeError):
        backend.upsert([RowOp("ent", "o1", {"x": 1})])
    backend.ensure(_CodecDoc.schema(), [])  # ensure records schema and returns without a client; must not raise
    backend.drop()  # drop is a no-op while down


class _CodecDoc(hb.Entity):
    identifier = "unit-neo4j-codec-doc"

    name = hb.field(hb.ShortText)
    score = hb.field(hb.Float)
    count = hb.field(hb.Integer)
    flag = hb.field(hb.Boolean)
    vec = hb.field(hb.Vector[3])
    meta = hb.field(hb.Json)


def test_neo4j_codec_roundtrips_logical_types():
    """_encode_props/_decode_node round-trip each logical type to/from Neo4j property shapes."""
    backend = Neo4jBackend("neo4j", url=_OFFLINE_URL)  # offline: codec needs no driver
    schema = _CodecDoc.schema()
    values = {"name": "hello", "score": 3.5, "count": 7, "flag": True, "vec": [1.0, 2.0, 3.0], "meta": {"a": 1, "b": [2, 3]}}
    enc = backend._encode_props(schema, "o1", values)

    assert enc["object_id"] == "o1"
    assert enc["vec"] == [1.0, 2.0, 3.0]  # native float array, not a packed blob
    assert enc["flag"] is True
    assert isinstance(enc["meta"], str)  # json/array/hyper-g collapse to a JSON string property

    dec = backend._decode_node(schema, enc)
    assert dec["object_id"] == "o1"
    assert dec["name"] == "hello"
    assert dec["score"] == 3.5
    assert dec["count"] == 7
    assert dec["flag"] is True
    assert dec["vec"] == [1.0, 2.0, 3.0]
    assert dec["meta"] == {"a": 1, "b": [2, 3]}

    # The production write path serializes Json first (row store dtype.encode -> JSON string), so also
    # drive the codec with that already-encoded form and confirm it is stored once (not double-encoded)
    # and still decodes back to the canonical value.
    meta_encoded = schema.fields["meta"].dtype.encode({"a": 1, "b": [2, 3]})
    assert isinstance(meta_encoded, str)
    enc2 = backend._encode_props(schema, "o3", {"meta": meta_encoded})
    assert enc2["meta"] == meta_encoded  # stored as-is, no second dumps_json layer
    assert backend._decode_node(schema, enc2)["meta"] == {"a": 1, "b": [2, 3]}


def test_neo4j_codec_skips_none_and_unknown_fields():
    """None values are skipped on encode; properties without a schema field decode untouched."""
    backend = Neo4jBackend("neo4j", url=_OFFLINE_URL)
    schema = _CodecDoc.schema()
    enc = backend._encode_props(schema, "o2", {"name": "x", "score": None})
    assert "score" not in enc and enc["name"] == "x"
    dec = backend._decode_node(schema, {"object_id": "o2", "name": "x", "__stray": "keep"})
    assert dec["name"] == "x" and dec["__stray"] == "keep"


def test_neo4j_json_workspace_encode_round_trips_for_scan_filters():
    """Json written through the row-store encode boundary scans back as the canonical value, not a JSON string.

    Regression: the workspace row store already serializes Json via ``dtype.encode`` (-> a JSON string)
    before the backend sees it. If ``_encode_props`` calls ``dumps_json`` again, ``_decode_node``'s single
    ``loads_json`` only undoes one layer and ``scan()`` hands a JSON *string* to the Python predicate; a
    scan-fallback Json filter (json is never index-pushable) then compares ``str == dict`` and silently
    drops matching rows. This drives ``_encode_props``/``_decode_node`` with the row-store-encoded input
    (offline; no driver needed).
    """
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.neo4j.handler import neo4j_filter_handler

    class _JsonScanDoc(hb.Entity):
        identifier = "unit-neo4j-json-scan-doc"

        meta = hb.field(hb.Json)

    schema = _JsonScanDoc.schema()
    field = schema.fields["meta"]
    backend = Neo4jBackend("neo4j", url=_OFFLINE_URL)
    canonical = {"a": 1, "b": [2, 3]}

    encoded = field.dtype.encode(canonical)  # what RowOp.values carries through the workspace write path
    assert isinstance(encoded, str)
    scanned = backend._decode_node(schema, backend._encode_props(schema, "o1", {"meta": encoded}))
    assert scanned["meta"] == canonical  # canonical dict for the scan predicate, NOT a JSON string

    eq = neo4j_filter_handler("eq")(field, canonical, HandlerContext("n", "neo4j", "inline"))
    predicate = eq.notes.get("row_payload") or eq.payload  # json -> py_scan fallback predicate
    assert predicate(scanned) is True  # the matching row is found
    assert predicate({"meta": {"a": 999}}) is False


def test_neo4j_label_namespacing_and_transparent_encoding():
    """Labels are scoped by (workspace, instance name, entity) with transparent separator normalization."""
    a = Neo4jBackend("na", url=_OFFLINE_URL, ws_id="shop")
    b = Neo4jBackend("nb", url=_OFFLINE_URL, ws_id="shop")
    assert a._label("doc") == "E_shop_na_doc"
    assert a._label("doc") != b._label("doc")  # different instance name -> different label, no collision
    assert a._label_prefix().startswith("E_shop_na_")
    assert a._label("doc").startswith(a._label_prefix())
    # '-' and '_' are both legal in ids and intentionally normalize to the same provider label.
    assert a._label("a-b") == a._label("a_b")
    a_dash = Neo4jBackend("n", url=_OFFLINE_URL, ws_id="a-b")
    a_under = Neo4jBackend("n", url=_OFFLINE_URL, ws_id="a_b")
    assert a_dash._label("x") == a_under._label("x")
    # the trailing "_" keeps the prefix a true segment boundary: "na" is never a prefix of "na2"'s labels
    assert not Neo4jBackend("na2", url=_OFFLINE_URL, ws_id="shop")._label("doc").startswith(a._label_prefix())


def test_neo4j_filter_handler_native_vs_scan_routing():
    """The handler pushes the parity-safe subset to a cypher_where fragment and scans the rest (offline)."""
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.neo4j.handler import neo4j_filter_handler

    class _FilterDoc(hb.Entity):
        identifier = "unit-neo4j-filter-doc"

        name = hb.field(hb.ShortText)
        price = hb.field(hb.Float)
        qty = hb.field(hb.Integer)
        flag = hb.field(hb.Boolean)
        meta = hb.field(hb.Json)

    fields = _FilterDoc.schema().fields
    ctx = HandlerContext("n", "neo4j", "inline")

    # eq / in on a scalar -> native, with the operand encoded into the structured payload
    eq = neo4j_filter_handler("eq")(fields["name"], "x", ctx)
    assert eq.notes["kind"] == "cypher_where" and eq.notes["mode"] == "native"
    assert eq.payload == {"op": "eq", "field": "name", "value": "x"}
    in_ = neo4j_filter_handler("in")(fields["qty"], [1, 2], ctx)
    assert in_.notes["kind"] == "cypher_where" and in_.payload["value"] == [1, 2]
    # range on a numeric field -> native; range on text -> scan (no faithful lexical order pushdown)
    assert neo4j_filter_handler("lt")(fields["price"], 5.0, ctx).notes["kind"] == "cypher_where"
    assert neo4j_filter_handler("gte")(fields["qty"], 3, ctx).notes["kind"] == "cypher_where"
    assert neo4j_filter_handler("lt")(fields["name"], "m", ctx).notes["mode"] == "py_scan"
    # documented case-sensitive Cypher string operators are parity-safe for non-empty text operands
    contains = neo4j_filter_handler("contains")(fields["name"], "e", ctx)
    assert contains.notes["kind"] == "cypher_where" and contains.payload == {"op": "contains", "field": "name", "value": "e"}
    assert neo4j_filter_handler("starts_with")(fields["name"], "d", ctx).notes["kind"] == "cypher_where"
    assert neo4j_filter_handler("ends_with")(fields["name"], "k", ctx).notes["kind"] == "cypher_where"
    assert neo4j_filter_handler("contains")(fields["name"], "", ctx).notes["mode"] == "py_scan"
    assert neo4j_filter_handler("match")(fields["name"], "x", ctx).notes["mode"] == "py_scan"
    # None operand (boolean validates None -> None) -> scan; a real boolean eq -> native
    assert neo4j_filter_handler("eq")(fields["flag"], None, ctx).notes["mode"] == "py_scan"
    assert neo4j_filter_handler("eq")(fields["flag"], True, ctx).notes["kind"] == "cypher_where"
    # compound JSON-string type and ne -> scan
    assert neo4j_filter_handler("eq")(fields["meta"], {"a": 1}, ctx).notes["mode"] == "py_scan"
    assert neo4j_filter_handler("ne")(fields["name"], "x", ctx).notes["mode"] == "py_scan"


def test_neo4j_cypher_where_compiler_and_pushdown_gate_offline():
    """_cypher_where renders a parameterized WHERE and _is_cypher_fragment gates whole-tree pushdown (offline).

    These are pure, server-independent functions; the live parity test (@external) is excluded by CI, so
    pin the rendering + gate contract here so a regression (wrong operator, wrong AND/OR joiner, duplicated
    param key, `in` rendered as `=`, or a scan/`not` leaf slipping through) cannot ship green.
    """
    from heavenbase.execution import HandlerContext, QueryFragment
    from heavenbase.backends.neo4j.handler import neo4j_filter_handler

    class _WhereDoc(hb.Entity):
        identifier = "unit-neo4j-where-doc"

        name = hb.field(hb.ShortText)
        price = hb.field(hb.Float)
        qty = hb.field(hb.Integer)
        meta = hb.field(hb.Json)

    f = _WhereDoc.schema().fields
    ctx = HandlerContext("n", "neo4j", "inline")
    backend = Neo4jBackend("neo4j", url=_OFFLINE_URL)
    eq = neo4j_filter_handler("eq")(f["name"], "x", ctx)
    in_ = neo4j_filter_handler("in")(f["qty"], [1, 2], ctx)
    lt = neo4j_filter_handler("lt")(f["price"], 5.0, ctx)
    contains = neo4j_filter_handler("contains")(f["name"], "es", ctx)
    starts = neo4j_filter_handler("starts_with")(f["name"], "de", ctx)
    ends = neo4j_filter_handler("ends_with")(f["name"], "sk", ctx)

    # single leaves: correct operator, `in` keyword, and a fresh sequential param key bound to the operand
    params: dict = {}
    assert backend._cypher_where(eq, params) == "n.`name` = $p0" and params == {"p0": "x"}
    params = {}
    assert backend._cypher_where(in_, params) == "n.`qty` IN $p0" and params == {"p0": [1, 2]}
    params = {}
    assert backend._cypher_where(lt, params) == "n.`price` < $p0" and params == {"p0": 5.0}
    params = {}
    assert backend._cypher_where(contains, params) == "(n.`name` IS NOT NULL AND n.`name` CONTAINS $p0)" and params == {"p0": "es"}
    params = {}
    assert backend._cypher_where(starts, params) == "(n.`name` IS NOT NULL AND n.`name` STARTS WITH $p0)" and params == {"p0": "de"}
    params = {}
    assert backend._cypher_where(ends, params) == "(n.`name` IS NOT NULL AND n.`name` ENDS WITH $p0)" and params == {"p0": "sk"}

    # AND / OR join with DISTINCT, sequential param keys (no collision between leaves)
    and_frag = QueryFragment("n", "and", "", [eq, lt])
    params = {}
    assert backend._cypher_where(and_frag, params) == "(n.`name` = $p0 AND n.`price` < $p1)"
    assert params == {"p0": "x", "p1": 5.0}
    or_frag = QueryFragment("n", "or", "", [eq, in_])
    params = {}
    assert backend._cypher_where(or_frag, params) == "(n.`name` = $p0 OR n.`qty` IN $p1)"
    assert params == {"p0": "x", "p1": [1, 2]}

    # pushdown gate: all-native trees push down; any py_scan leaf or a `not` node keeps the whole tree on scan
    assert backend._is_cypher_fragment(eq) is True
    assert backend._is_cypher_fragment(and_frag) is True
    scan_leaf = neo4j_filter_handler("ne")(f["name"], "x", ctx)  # ne -> py_scan
    json_leaf = neo4j_filter_handler("eq")(f["meta"], {"a": 1}, ctx)  # json compound -> py_scan
    assert backend._is_cypher_fragment(scan_leaf) is False
    assert backend._is_cypher_fragment(QueryFragment("n", "and", "", [eq, scan_leaf])) is False
    assert backend._is_cypher_fragment(QueryFragment("n", "or", "", [eq, json_leaf])) is False
    assert backend._is_cypher_fragment(QueryFragment("n", "not", "", [eq])) is False


def test_neo4j_mixed_cypher_and_scan_filters_compose_offline(monkeypatch):
    """Mixed logical trees execute native leaves natively and scan-only leaves portably, then compose frames."""
    from heavenbase.execution import HandlerContext, QueryFragment
    from heavenbase.backends.neo4j.handler import neo4j_filter_handler

    class _MixedDoc(hb.Entity):
        identifier = "unit-neo4j-mixed-doc"

        name = hb.field(hb.ShortText)
        meta = hb.field(hb.Json)

    class _FakeSession:
        def __init__(self, driver):
            self.driver = driver

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def run(self, query, **params):
            self.driver.runs.append((query, params))
            return [{"n": {"object_id": "a", "name": "alpha"}}]

    class _FakeDriver:
        def __init__(self):
            self.runs = []

        def session(self):
            return _FakeSession(self)

    schema = _MixedDoc.schema()
    fields = schema.fields
    Neo4jBackend.register()
    ctx = HandlerContext("neo4j", "neo4j", "inline")
    native = neo4j_filter_handler("eq")(fields["name"], "alpha", ctx)
    scan_leaf = neo4j_filter_handler("eq")(fields["meta"], {"flag": True}, ctx)
    root = QueryFragment("neo4j", "and", "", [native, scan_leaf], notes={"entity_id": schema.entity_id})

    backend = Neo4jBackend("neo4j", url=_OFFLINE_URL)
    backend.driver = _FakeDriver()
    backend.client_error = ""
    backend._schemas[schema.entity_id] = schema
    scan_calls = []

    def _scan(entity_id):
        scan_calls.append(entity_id)
        return [
            {"object_id": "a", "name": "alpha", "meta": {"flag": True}},
            {"object_id": "b", "name": "alpha", "meta": {"flag": False}},
        ]

    monkeypatch.setattr(backend, "scan", _scan)

    assert backend.execute(root).ids == ["a"]
    assert scan_calls == [schema.entity_id]
    assert len(backend.driver.runs) == 1
    query, params = backend.driver.runs[0]
    assert "WHERE n.`name` = $p0" in query
    assert params == {"p0": "alpha"}


def test_neo4j_near_handler_produces_native_vector_fragment():
    """The near handler emits a native neo4j_vector_search fragment carrying the query + a scan scorer (offline)."""
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.neo4j.handler import neo4j_near_handler

    class _NearDoc(hb.Entity):
        identifier = "unit-neo4j-near-doc"

        emb = hb.field(hb.Vector[3])

    field = _NearDoc.schema().fields["emb"]
    ctx = HandlerContext("n", "neo4j", "vector", top_k=5, metric="cosine")
    frag = neo4j_near_handler(field, [1.0, 0.0, 0.0], ctx)
    assert frag.op == "near" and frag.notes["kind"] == "neo4j_vector_search" and frag.notes["mode"] == "native"
    assert frag.payload == {"vector": [1.0, 0.0, 0.0], "top_k": 5, "metric": "cosine"}
    assert callable(frag.notes["row_payload"])  # cosine scorer kept for the scan fallback / combined cases
    # native KNN needs a cosine, fixed-dim vector index: a non-cosine metric or a dimensionless field reports py_scan honestly
    l2 = neo4j_near_handler(field, [1.0, 0.0, 0.0], HandlerContext("n", "neo4j", "vector", top_k=5, metric="l2"))
    assert l2.notes["mode"] == "py_scan" and l2.notes.get("fallback_reason")

    class _DimlessDoc(hb.Entity):
        identifier = "unit-neo4j-near-dimless"

        emb = hb.field(hb.Vector)

    dimless = neo4j_near_handler(
        _DimlessDoc.schema().fields["emb"],
        [1.0, 0.0, 0.0],
        HandlerContext("n", "neo4j", "vector", top_k=5, metric="cosine"),
    )
    assert dimless.notes["mode"] == "py_scan" and dimless.notes.get("fallback_reason")
    with pytest.raises(ValueError):  # query vector dimension is validated against Vector[3]
        neo4j_near_handler(field, [1.0, 0.0], ctx)


def test_neo4j_near_handler_metric_selects_scan_scorer():
    """The near handler carries a scorer matching the requested metric, and rejects unknown metrics (offline)."""
    from heavenbase.execution import HandlerContext
    from heavenbase.backends.neo4j.handler import neo4j_near_handler
    from heavenbase.types import cosine_score, euclidean_score, inner_product_score

    class _MetricDoc(hb.Entity):
        identifier = "unit-neo4j-metric-doc"

        emb = hb.field(hb.Vector[3])

    field = _MetricDoc.schema().fields["emb"]
    q = [1.0, 0.0, 0.0]
    row = {"emb": [0.5, 0.5, 0.0]}

    def scorer(metric):
        return neo4j_near_handler(field, q, HandlerContext("n", "neo4j", "vector", top_k=5, metric=metric)).notes["row_payload"]

    # a non-cosine metric falls back to the scan, which must rank by THAT metric (not silently cosine)
    assert scorer("cosine")(row) == cosine_score(row["emb"], q)
    assert scorer("l2")(row) == euclidean_score(row["emb"], q)
    assert scorer("ip")(row) == inner_product_score(row["emb"], q)
    # an empty vector keeps the neutral cosine score but never outranks real rows under l2/ip
    assert scorer("cosine")({}) is None  # the vector index never returns a node without the vector
    assert scorer("l2")({}) is None and scorer("ip")({}) is None
    with pytest.raises(ValueError):  # an unsupported metric is rejected, not silently mis-ranked
        neo4j_near_handler(field, q, HandlerContext("n", "neo4j", "vector", top_k=5, metric="bogus"))


def test_neo4j_near_plus_filter_explains_combined_scan():
    """A same-backend native filter + native near must explain as combined_scan: neo4j has no native combined path (offline).

    Both fragments are individually native (cypher_where + neo4j_vector_search), but _run_near always
    degrades to the Python scan when a filter is attached, and neo4j advertises only ``combined_scan``.
    explain() must report what actually runs, not ``combined_native``.
    """
    # inmem owns the catalog/system rows (neo4j is offline here); both queried fields live on neo4j so
    # the near + scalar filter take the same-backend combined path.
    ws = hb.HeavenBase("unit-neo4j-explain", backends={"main": hb.backends.InMemBackend("main"), "n": Neo4jBackend("n", url=_OFFLINE_URL)})

    class _ExplainDoc(hb.Entity):
        identifier = "unit-neo4j-explain-doc"

        price = hb.field(hb.Float).store(to="n")
        emb = hb.field(hb.Vector[3]).store(to="n", strategy=hb.VectorIndex.identifier)

    try:
        ws.register(_ExplainDoc)
        query = ws.query(_ExplainDoc).where(_ExplainDoc.price >= 1.0).near(_ExplainDoc.emb, [1.0, 0.0, 0.0], top_k=5)
        near_step = [step for step in query.explain()["steps"] if step.get("node") == "near"][0]
        assert near_step["near_filter_mode"] == "combined_scan"
    finally:
        WORKSPACES.pop("unit-neo4j-explain", None)


# --------------------------------------------------------------- live (docker-gated)
# Bring a server up with, e.g.:
#   docker run -p 7687:7687 -p 7474:7474 -e NEO4J_AUTH=neo4j/testpassword neo4j:5
# and set HVNB_NEO4J_PASSWORD (+ optional HVNB_NEO4J_URL/USER). These skip when the
# neo4j driver is missing, the server is unreachable, or auth/connect fails.


def _reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _live_backend(name: str, *, ws_id: str | None = None) -> Neo4jBackend:
    """Return a connected Neo4jBackend or skip when the driver/server/auth is unavailable."""
    if find_spec("neo4j") is None:
        pytest.skip("neo4j driver is not installed")
    parsed = urlparse(_NEO4J_URL)  # probe the CONFIGURED host/port (HVNB_NEO4J_URL), not a hard-coded localhost
    host, port = parsed.hostname or "localhost", parsed.port or 7687
    if not _reachable(host, port):
        pytest.skip(f"neo4j server is not reachable on {host}:{port}")
    backend = Neo4jBackend(name, url=_NEO4J_URL, ws_id=ws_id)
    if backend.driver is None:
        pytest.skip(f"neo4j connect/auth failed: {backend.client_error}")
    return backend


class _RtThing(hb.Entity):
    identifier = "docker-neo4j-rt-thing"

    name = hb.field(hb.ShortText)
    score = hb.field(hb.Float)


@pytest.mark.external
def test_neo4j_workspace_query_pipeline():
    """End-to-end through the workspace: upsert, then scalar filters via the scan pipeline."""
    backend = _live_backend("nq")
    ws = hb.HeavenBase("docker-neo4j-q", backends={"n": backend})
    try:
        ws.drop()  # clean slate
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
        assert ws.query(_RtThing).where(_RtThing.name == "alpha").execute().ids == ["a"]  # router -> handler -> scan -> decode -> predicate
    finally:
        ws.drop()
        WORKSPACES.pop("docker-neo4j-q", None)


@pytest.mark.external
def test_neo4j_codec_roundtrips_live():
    """A live MERGE upsert -> get_many round-trips every logical-type property shape."""
    backend = _live_backend("ncodec")

    class _LiveCodecDoc(hb.Entity):
        identifier = "docker-neo4j-codec-doc"

        name = hb.field(hb.ShortText)
        count = hb.field(hb.Integer)
        score = hb.field(hb.Float)
        flag = hb.field(hb.Boolean)
        vec = hb.field(hb.Vector[2])
        meta = hb.field(hb.Json)
        edges = hb.field(hb.HyperG["src" : hb.ShortText, "rel" : hb.ShortText, "weight" : hb.Float])

    schema = _LiveCodecDoc.schema()
    backend.ensure(schema, [])
    backend.drop()
    backend.ensure(schema, [])
    values = {
        "name": "x",
        "count": 7,
        "score": 3.5,
        "flag": True,
        "vec": [1.0, 2.0],
        "meta": {"a": 1, "b": [2, 3]},
        "edges": [{"src": "a", "rel": "cites", "weight": 0.5}],  # hyper-g goes through the same JSON-string path as json
    }
    backend.upsert([RowOp(schema.entity_id, "o1", values)])
    (row,) = backend.get_many(schema.entity_id, ["o1"])
    assert row["object_id"] == "o1"
    assert row["name"] == "x" and row["count"] == 7 and row["score"] == 3.5 and row["flag"] is True
    assert row["vec"] == [1.0, 2.0]  # native float array round-trip
    assert row["meta"] == {"a": 1, "b": [2, 3]}
    assert row["edges"] == [{"src": "a", "rel": "cites", "weight": 0.5}]
    backend.drop()


@pytest.mark.external
def test_neo4j_full_row_replace_and_delete_live():
    """MERGE + SET n = $props replaces the whole node (drops stale fields); delete removes it."""
    backend = _live_backend("nrep")

    class _RepDoc(hb.Entity):
        identifier = "docker-neo4j-rep-doc"

        a = hb.field(hb.ShortText)
        b = hb.field(hb.ShortText)

    schema = _RepDoc.schema()
    backend.ensure(schema, [])
    backend.drop()
    backend.ensure(schema, [])
    backend.upsert([RowOp(schema.entity_id, "o1", {"a": "x", "b": "y"})])
    (row,) = backend.get_many(schema.entity_id, ["o1"])
    assert row["a"] == "x" and row["b"] == "y"
    backend.upsert([RowOp(schema.entity_id, "o1", {"a": "x2"})])  # full-row replace drops stale b
    (row,) = backend.get_many(schema.entity_id, ["o1"])
    assert row["a"] == "x2" and row.get("b") is None
    backend.upsert([RowOp(schema.entity_id, "o1", {}, op="delete")])
    assert backend.count(schema.entity_id) == 0
    backend.drop()


@pytest.mark.external
def test_neo4j_namespace_isolation_live():
    """Two backends with the same instance name but different workspaces never see each other's rows."""
    a = _live_backend("iso", ws_id="nsa")
    b = _live_backend("iso", ws_id="nsb")

    class _IsoDoc(hb.Entity):
        identifier = "docker-neo4j-iso-doc"

        v = hb.field(hb.ShortText)

    schema = _IsoDoc.schema()
    for backend in (a, b):
        backend.ensure(schema, [])
        backend.drop()
        backend.ensure(schema, [])
    a.upsert([RowOp(schema.entity_id, "o1", {"v": "in-a"})])
    assert a.count(schema.entity_id) == 1
    assert b.count(schema.entity_id) == 0  # different workspace label -> isolated keyspace
    a.drop()
    b.drop()


class _PushdownDoc(hb.Entity):
    identifier = "docker-neo4j-pushdown-doc"

    name = hb.field(hb.ShortText)
    price = hb.field(hb.Float)
    qty = hb.field(hb.Integer)
    tag = hb.field(hb.ShortText).optional()


@pytest.mark.external
def test_neo4j_native_pushdown_parity_live():
    """Native Cypher eq/in/range pushdown returns exactly what the scan predicate would, live.

    Each pushed-down filter reports handler_mode == "native" via explain(); the result set is
    asserted against the independently-known expectation. A nullable field excludes its None rows
    (Cypher 3-valued logic, matching the scan's False-on-None for eq), and non-empty text affix /
    substring ops push down through Cypher's documented case-sensitive string operators.
    """
    backend = _live_backend("npd")
    ws = hb.HeavenBase("docker-neo4j-pd", backends={"n": backend})
    try:
        ws.drop()
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
        # logical AND/OR of native leaves still pushes down to one Cypher WHERE
        assert sorted(ws.query(_PushdownDoc).where((_PushdownDoc.price > 50) & (_PushdownDoc.price < 200)).execute().ids) == ["p1", "p4"]
        assert sorted(ws.query(_PushdownDoc).where((_PushdownDoc.name == "lamp") | (_PushdownDoc.qty == 3)).execute().ids) == ["p2", "p3"]
        # documented case-sensitive string ops push down with a null guard, still matching scan semantics
        contains = ws.query(_PushdownDoc).where(_PushdownDoc.name.contains("es"))
        assert contains.explain()["steps"][0].get("handler_mode") == "native"
        assert sorted(contains.execute().ids) == ["p1", "p4"]
        assert native(ws.query(_PushdownDoc).where(_PushdownDoc.name.starts_with("la"))) == ["p2"]
        assert native(ws.query(_PushdownDoc).where(_PushdownDoc.name.ends_with("fa"))) == ["p3"]
    finally:
        ws.drop()
        WORKSPACES.pop("docker-neo4j-pd", None)


class _NearDocLive(hb.Entity):
    identifier = "docker-neo4j-near-doc"

    cat = hb.field(hb.ShortText)
    emb = hb.field(hb.Vector[3])


@pytest.mark.external
def test_neo4j_native_vector_near_parity_live():
    """Native KNN via the Neo4j vector index returns the same top-k as the cosine scan scorer, live.

    The pure near reports handler_mode == "native"; its ranking equals the order cosine_score would
    produce, and each .score is the raw cosine (Neo4j's (1+cos)/2 index score -> 2*s-1, within
    float32/HNSW noise). A near co-located with a filter, or a non-cosine metric, degrades to the
    cosine scan — the only metric the portable scorer computes — so native and scan agree.
    """
    from heavenbase.types import cosine_score

    backend = _live_backend("nnear")
    ws = hb.HeavenBase("docker-neo4j-near", backends={"n": backend})
    q = [1.0, 0.0, 0.0]
    data = [("a", "x", [1.0, 0.0, 0.0]), ("b", "x", [0.9, 0.1, 0.0]), ("c", "y", [0.5, 0.5, 0.0]), ("d", "y", [0.1, 0.9, 0.0])]
    emap = {oid: vec for oid, _, vec in data}
    try:
        ws.drop()
        ws.upsert_many(_NearDocLive, [{"object_id": oid, "cat": cat, "emb": vec} for oid, cat, vec in data])
        expected = [oid for oid, _, _ in sorted(data, key=lambda r: -cosine_score(r[2], q))]
        near = ws.query(_NearDocLive).near(_NearDocLive.emb, q, top_k=3)
        frame = near.execute()
        assert near.explain()["steps"][0].get("handler_mode") == "native"  # native KNN, not scan
        assert frame.ids == expected[:3]  # ranking parity with the cosine scan scorer
        for row in frame.rows():
            assert abs(row["score"] - cosine_score(emap[row["object_id"]], q)) < 0.02  # ~ raw cosine (float32/ANN noise)
        assert len(ws.query(_NearDocLive).near(_NearDocLive.emb, q, top_k=2).execute().ids) == 2  # top_k honored
        # near co-located with a filter degrades to the (cosine) scan and stays correct
        assert sorted(ws.query(_NearDocLive).where(_NearDocLive.cat == "x").near(_NearDocLive.emb, q, top_k=3).execute().ids) == ["a", "b"]
    finally:
        ws.drop()
        WORKSPACES.pop("docker-neo4j-near", None)


class _DimlessNearDoc(hb.Entity):
    identifier = "docker-neo4j-dimless-doc"

    emb = hb.field(hb.Vector)  # no declared dimension -> ensure() builds no native vector index


@pytest.mark.external
def test_neo4j_dimensionless_near_falls_back_to_scan_live():
    """A near on a dimensionless Vector field has no native index and must scan, not crash, live.

    ``ensure`` only builds a vector index for a fixed-dim field, but the query still emits a native
    near fragment; ``_run_near`` must detect the absent dimension/index and degrade to the cosine
    scan instead of calling ``db.index.vector.queryNodes`` against a missing index.
    """
    backend = _live_backend("ndimless")
    ws = hb.HeavenBase("docker-neo4j-dimless", backends={"n": backend})
    q = [1.0, 0.0, 0.0]
    try:
        ws.drop()
        ws.upsert_many(
            _DimlessNearDoc,
            [{"object_id": "a", "emb": [1.0, 0.0, 0.0]}, {"object_id": "b", "emb": [0.0, 1.0, 0.0]}],
        )
        ids = ws.query(_DimlessNearDoc).near(_DimlessNearDoc.emb, q, top_k=2).execute().ids  # no crash
        assert ids[0] == "a"  # cosine scan fallback still ranks correctly
    finally:
        ws.drop()
        WORKSPACES.pop("docker-neo4j-dimless", None)


class _ArtifactDoc(hb.Entity):
    identifier = "docker-neo4j-artifact-doc"

    name = hb.field(hb.ShortText)
    emb = hb.field(hb.Vector[3])


@pytest.mark.external
def test_neo4j_drop_clears_schema_artifacts_live():
    """drop() removes the namespace's constraints and property/vector indexes, not just nodes, live.

    A leftover vector index would pin a stale dimension/config (``ensure`` only issues
    ``CREATE ... IF NOT EXISTS``), so a later recreate of the same field with a different dimension
    would silently keep the old index. After drop() no schema artifact for the prefix may remain.
    """
    backend = _live_backend("nartifacts")
    schema = _ArtifactDoc.schema()
    prefix = backend._label_prefix()

    def artifacts():
        with backend.driver.session() as session:
            cons = [
                r["name"]
                for r in session.run("SHOW CONSTRAINTS YIELD name, labelsOrTypes RETURN name, labelsOrTypes")
                if backend._matches_prefix(r["labelsOrTypes"], prefix)
            ]
            idx = [
                r["name"]
                for r in session.run("SHOW INDEXES YIELD name, labelsOrTypes RETURN name, labelsOrTypes")
                if backend._matches_prefix(r["labelsOrTypes"], prefix)
            ]
        return cons, idx

    try:
        backend.drop()
        backend.ensure(schema, [])  # object_id constraint + name property index + dim-3 vector index
        cons, idx = artifacts()
        assert cons and idx  # ensure created the schema artifacts
        backend.drop()
        cons, idx = artifacts()
        assert cons == [] and idx == []  # drop cleared every constraint and index for this namespace
    finally:
        backend.drop()
