"""Vector stores that keep float32 coordinates rank cosine queries of any finite magnitude by direction."""

import math
import sys
from importlib.util import find_spec
from types import SimpleNamespace

import pytest

import heavenbase as hb
from heavenbase.execution import QueryFragment
from heavenbase.types import cosine_score
from heavenbase.execution.handlers.near import _scale_cosine_query

pytestmark = pytest.mark.fast

# 1e20 and 1e-25 square outside float32; 1e50 and 1e-60 do not fit float32 at all.
SCALES = [1.0, 1e20, 1e50, 1e-25, 1e-60]
ROWS = [
    {"object_id": "b", "embedding": [-1.0, 0.0]},
    {"object_id": "c", "embedding": [0.0, 1.0]},
    {"object_id": "a", "embedding": [1.0, 0.01]},
]


class CosineRangeDoc(hb.Entity):
    identifier = "vector-cosine-query-range-doc"

    embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).required()


def _workspace(tmp_path, kind):
    if find_spec("chromadb" if kind == "chroma" else "lancedb") is None:
        pytest.skip(f"{kind} is not installed")
    vec = {"type": kind, ("path" if kind == "chroma" else "uri"): hb.utils.path_to_file_uri(tmp_path / kind)}
    return hb.HeavenBase(f"cosine-query-range-{kind}", detached=True, backends={"main": {"type": "inmem"}, "vec": vec})


def _assert_float32_safe(sent, query):
    """The vector a provider received is the scaled query (to float32 precision), with float32-sized squares."""
    import numpy as np

    sent = [float(value) for value in sent]
    assert sent == pytest.approx(_scale_cosine_query(query), rel=1e-6, abs=0.0), sent
    with np.errstate(all="ignore"):
        coordinates = np.asarray(sent, dtype=np.float32)
        squares = float(np.sum(coordinates * coordinates))
    assert np.isfinite(coordinates).all() and math.isfinite(squares) and squares > 0.0, sent


def test_scale_cosine_query_is_an_exact_power_of_two():
    for query in ([0.3, -0.2], [3e50, -1e48], [-7e-60, 2e-61], [1e20, 0.0], [0.75, 0.5]):
        scaled = _scale_cosine_query(query)
        ratio = scaled[0] / query[0]
        assert 0.5 <= max(map(abs, scaled)) < 1.0
        assert math.frexp(ratio)[0] == 0.5
        assert all(value * ratio == item for value, item in zip(query, scaled))
    in_range = [0.75, 0.5]
    assert _scale_cosine_query(in_range) is in_range
    for query in ([0.0, 0.0], [math.inf, 1.0]):
        assert _scale_cosine_query(query) is query


def test_cosine_score_ranks_operands_of_any_finite_magnitude():
    for scale in (1e200, 1e-200, 1e308, 5e-324):
        assert cosine_score([1.0, 0.0], [scale, 0.0]) == pytest.approx(1.0)
        assert cosine_score([scale, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
        assert cosine_score([0.0, 1.0], [scale, 0.0]) == 0.0
    assert cosine_score([1.0, 0.01], [1e200, 1e198]) > cosine_score([0.0, 1.0], [1e200, 1e198])
    assert cosine_score([0.0, 0.0], [1e200, 0.0]) == 0.0
    ordinary = ([0.3, -0.2, 0.9], [0.1, 0.4, -0.7])
    dot = sum(x * y for x, y in zip(*ordinary))
    assert cosine_score(*ordinary) == dot / (sum(x * x for x in ordinary[0]) ** 0.5 * sum(y * y for y in ordinary[1]) ** 0.5)


def test_portable_scan_cosine_near_returns_the_nearest_row_for_extreme_queries():
    """The in-memory near scores in binary64; a query whose squares leave binary64 still ranks by direction."""
    ws = hb.HeavenBase("cosine-query-range-inmem", detached=True, backends={"main": {"type": "inmem"}, "vec": {"type": "inmem"}})
    try:
        ws.register(CosineRangeDoc)
        ws.upsert_many(CosineRangeDoc, ROWS)
        for scale in (1e200, 1e-200):
            rows = ws.query(CosineRangeDoc).near("embedding", [scale, scale / 100], metric="cosine", top_k=1).execute().rows()
            assert [row["object_id"] for row in rows] == ["a"], scale
            assert rows[0]["score"] == pytest.approx(1.0), scale
    finally:
        ws.release()


def test_chroma_scan_cosine_near_returns_the_nearest_row_for_extreme_queries(tmp_path):
    """A cosine near on an l2 collection ranks through the scan scorer."""
    ws = _workspace(tmp_path, "chroma")
    setting = "heavenbase.backend_defaults.chroma.metric"
    try:
        ws.context.config.set(setting, "l2")
        ws.register(CosineRangeDoc)
        ws.upsert_many(CosineRangeDoc, ROWS)
        for scale in (1e200, 1e-200):
            near = ws.query(CosineRangeDoc).near("embedding", [scale, scale / 100], metric="cosine", top_k=1)
            assert near.explain()["steps"][-1]["handler_mode"] == "py_scan"
            rows = near.execute().rows()
            assert [row["object_id"] for row in rows] == ["a"], scale
            assert rows[0]["score"] == pytest.approx(1.0, rel=1e-6), scale
    finally:
        ws.context.config.unset(setting)
        ws.release()


@pytest.mark.parametrize("kind", ["chroma", "lance"])
def test_cosine_near_returns_the_nearest_row_for_any_finite_query_scale(tmp_path, kind):
    ws = _workspace(tmp_path, kind)
    try:
        ws.register(CosineRangeDoc)
        ws.upsert_many(CosineRangeDoc, ROWS)
        for scale in SCALES:
            rows = ws.query(CosineRangeDoc).near("embedding", [scale, scale / 100], metric="cosine", top_k=1).execute().rows()
            assert [row["object_id"] for row in rows] == ["a"], scale
            assert rows[0]["score"] == pytest.approx(1.0, rel=1e-5), scale
    finally:
        ws.release()


def test_chroma_rescored_cosine_near_keeps_the_true_candidates(tmp_path, monkeypatch):
    """A near larger than one id read ranks Chroma's candidates again; the candidates must be the nearest."""
    from heavenbase.backends.vector import chroma

    monkeypatch.setattr(chroma, "CHROMA_MAX_GET_IDS", 2)
    ws = _workspace(tmp_path, "chroma")
    try:
        ws.register(CosineRangeDoc)
        ws.upsert_many(
            CosineRangeDoc,
            [{"object_id": f"r{angle:03d}", "embedding": [math.cos(math.radians(angle)), math.sin(math.radians(angle))]} for angle in range(0, 360, 45)],
        )
        for scale in SCALES:
            ids = ws.query(CosineRangeDoc).near("embedding", [scale, 0.0], metric="cosine", top_k=3).execute().ids
            assert ids[0] == "r000", scale
            assert set(ids) == {"r000", "r045", "r315"}, scale
    finally:
        ws.release()


@pytest.mark.skipif(find_spec("milvus_lite") is None, reason="milvus-lite is not installed")
def test_milvus_server_native_cosine_near_scales_the_query(tmp_path):
    """The server adapter searches natively; a real Milvus Lite file stands in for the float32 server."""
    backend = hb.backends.MilvusBackend("vec", str(tmp_path / "server-path.db"), index_type="FLAT")
    ws = hb.HeavenBase("cosine-query-range-milvus", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    try:
        ws.register(CosineRangeDoc)
        ws.upsert_many(CosineRangeDoc, ROWS)
        for scale in SCALES:
            near = ws.query(CosineRangeDoc).near("embedding", [scale, scale / 100], metric="cosine", top_k=1)
            assert near.explain()["steps"][-1]["handler_mode"] == "native"
            rows = near.execute().rows()
            assert [row["object_id"] for row in rows] == ["a"], scale
            assert rows[0]["score"] == pytest.approx(1.0, rel=1e-5), scale
    finally:
        ws.release()


@pytest.mark.skipif(find_spec("redis") is None, reason="redis client is not installed")
@pytest.mark.parametrize("scale", SCALES)
def test_redisearch_cosine_knn_packs_a_float32_query(monkeypatch, scale):
    import numpy as np

    from heavenbase.backends.vector.handler.redisearch import redisearch_near_handler
    from heavenbase.backends.vector.redisearch import RediSearchBackend
    from heavenbase.execution import HandlerContext
    from heavenbase.strategies import VectorIndex

    class RsDoc(hb.Entity):
        identifier = "unit-rs-cosine-range-doc"

        embedding = hb.field(hb.Vector[2])

    schema = RsDoc.schema()
    backend = RediSearchBackend("unit-rs-cosine-range", url="redis://127.0.0.1:1")
    backend._schemas[schema.entity_id] = schema
    monkeypatch.setattr(backend, "client", object())  # stands in for a connected client
    monkeypatch.setattr(backend, "_need_client", lambda: None)
    monkeypatch.setattr(backend, "_has_index", lambda entity_id: True)
    monkeypatch.setattr(backend, "_index_field_specs", lambda index: {"embedding": {"type": "VECTOR", "dim": 2, "metric": "COSINE"}})
    monkeypatch.setattr(backend, "_fetch_rows", lambda entity_id, ids: [])
    sent = []

    class Search:
        def search(self, query, query_params=None):
            sent.append(np.frombuffer(query_params["vec"], dtype="<f4").tolist())
            return SimpleNamespace(docs=[], total=0)

    monkeypatch.setattr(backend, "text_client", SimpleNamespace(ft=lambda index: Search()))
    ctx = HandlerContext(
        "unit-rs-cosine-range",
        "redisearch",
        VectorIndex.identifier,
        top_k=1,
        metric="cosine",
        resolver=hb.DEFAULT_CONTEXT.modules(),
        config=backend.config,
        backend_instance=backend,
        entity_id=schema.entity_id,
    )
    query = [scale, scale / 100]
    near = redisearch_near_handler(schema.fields["embedding"], query, ctx)
    assert near.mode() == "native"
    backend.execute(near.bind(schema.entity_id))
    _assert_float32_safe(sent[0], query)


@pytest.mark.skipif(find_spec("pgvector") is None, reason="pgvector is not installed")
@pytest.mark.parametrize("scale", SCALES)
def test_pgvector_cosine_distance_binds_a_float32_query(scale):
    import sqlalchemy as sa
    from pgvector.sqlalchemy import Vector
    from sqlalchemy.dialects import postgresql

    from heavenbase.backends.vector.pgvector import PgVectorBackend

    column = sa.Column("embedding", Vector(2))
    query = [scale, scale / 100]
    distance = PgVectorBackend._distance(None, column, query, "cosine")
    (bound,) = distance.compile(dialect=postgresql.dialect()).params.values()
    sent = [float(item) for item in Vector(2).bind_processor(postgresql.dialect())(bound).strip("[]").split(",")]
    _assert_float32_safe(sent, query)


def _capture_pinecone(monkeypatch, query):
    sent = []

    class Index:
        def query(self, *, vector, top_k, include_metadata=True, filter=None):
            sent.append(vector)
            return {"matches": []}

    class FakePinecone:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setitem(sys.modules, "pinecone", SimpleNamespace(Pinecone=FakePinecone, ServerlessSpec=lambda **kwargs: kwargs))
    backend = hb.backends.PineconeBackend("vec", host="localhost:5080")
    monkeypatch.setattr(backend, "client", object())
    monkeypatch.setattr(backend, "_has_index", lambda name: True)
    monkeypatch.setattr(backend, "_index", lambda name: Index())
    payload = {"vector": query, "top_k": 1, "metric": "cosine"}
    backend.execute(QueryFragment("vec", "near", "embedding", payload, notes={"kind": "pinecone_search", "mode": "native", "entity_id": "doc"}))
    return sent


def _capture_qdrant(monkeypatch, query):
    sent = []

    class Client:
        def collection_exists(self, name):
            return True

        def query_points(self, *, query, **kwargs):
            sent.append(query)
            return SimpleNamespace(points=[])

    backend = hb.backends.QdrantBackend("vec", url="http://127.0.0.1:1")
    monkeypatch.setattr(backend, "client", Client())
    monkeypatch.setattr(backend, "_connect_pending", False)
    payload = {"vector": query, "top_k": 1, "metric": "cosine"}
    backend.execute(QueryFragment("vec", "near", "embedding", payload, notes={"kind": "qdrant_search", "mode": "native", "entity_id": "doc"}))
    return sent


def _capture_weaviate(monkeypatch, query):
    sent = []

    def near_vector(*, near_vector, **kwargs):
        sent.append(near_vector)
        return SimpleNamespace(objects=[])

    collections = SimpleNamespace(exists=lambda name: True, use=lambda name: SimpleNamespace(query=SimpleNamespace(near_vector=near_vector)))
    backend = hb.backends.WeaviateBackend("vec", url="http://127.0.0.1:1")
    monkeypatch.setattr(backend, "client", SimpleNamespace(collections=collections))
    monkeypatch.setattr(backend, "_connect_pending", False)
    monkeypatch.setattr(backend, "_wvc", SimpleNamespace(query=SimpleNamespace(MetadataQuery=lambda **kwargs: kwargs)))
    payload = {"vector": query, "top_k": 1, "metric": "cosine"}
    backend.execute(QueryFragment("vec", "near", "embedding", payload, notes={"kind": "weaviate_search", "mode": "native", "entity_id": "doc"}))
    return sent


@pytest.mark.slow
@pytest.mark.parametrize("capture", [_capture_pinecone, _capture_qdrant, _capture_weaviate], ids=["pinecone", "qdrant", "weaviate"])
@pytest.mark.parametrize("scale", SCALES)
def test_remote_cosine_near_sends_a_float32_query(monkeypatch, capture, scale):
    """Pinecone, Qdrant and Weaviate keep float32 vectors, so the query each receives must square inside float32."""
    query = [scale, scale / 100]
    sent = capture(monkeypatch, query)
    assert len(sent) == 1
    _assert_float32_safe(sent[0], query)


@pytest.mark.parametrize("stored_scale", [1e-200, 1e200, 1e-300, 1e300, 1e308])
def test_exact_batch_cosine_scores_stored_vectors_of_any_finite_magnitude(stored_scale):
    """Exact near scores batches with NumPy; a stored row whose squares leave binary64 keeps its cosine."""
    pytest.importorskip("numpy")
    from heavenbase.execution.handlers.near import _PreparedVectors, _batch_scores

    rows = [{"object_id": "a", "embedding": [stored_scale, stored_scale / 100]}, *[dict(row) for row in ROWS[:2]]]
    for prepared in (None, _PreparedVectors(rows, "embedding")):
        scores = _batch_scores(rows, "embedding", [1.0, 0.01], "cosine", None, prepared=prepared)
        assert scores[0] == pytest.approx(1.0), (stored_scale, prepared)
        assert scores[1] == pytest.approx(-1.0 / math.hypot(1.0, 0.01)), (stored_scale, prepared)
        assert scores[2] == pytest.approx(0.01 / math.hypot(1.0, 0.01)), (stored_scale, prepared)


@pytest.mark.parametrize("use_prepared", [False, True])
def test_exact_batch_cosine_scores_zero_dimension_vectors_as_zero(use_prepared):
    """A zero-dimension row has no magnitude; exact cosine keeps scoring it 0.0, as before."""
    pytest.importorskip("numpy")
    from heavenbase.execution.handlers.near import _PreparedVectors, _batch_scores

    rows = [{"object_id": "first", "v": []}, {"object_id": "second", "v": []}, {"object_id": "empty"}]
    prepared = _PreparedVectors(rows, "v") if use_prepared else None
    assert _batch_scores(rows, "v", [], "cosine", None, prepared=prepared) == [0.0, 0.0, None]


@pytest.mark.slow
@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("stored_scale", [1e-200, 1e200])
def test_portable_cosine_near_ranks_stored_vectors_of_any_finite_magnitude(exact, stored_scale):
    ws = hb.HeavenBase("cosine-stored-range-inmem", detached=True, backends={"main": {"type": "inmem"}, "vec": {"type": "inmem"}})
    try:
        ws.register(CosineRangeDoc)
        ws.upsert_many(CosineRangeDoc, [{"object_id": "a", "embedding": [stored_scale, stored_scale / 100]}, *ROWS[:2]])
        for _ in range(2):  # the second run may score from the prepared generation
            rows = ws.query(CosineRangeDoc).near("embedding", [1.0, 0.01], metric="cosine", top_k=3, exact=exact).execute().rows()
            assert [row["object_id"] for row in rows] == ["a", "c", "b"], (exact, stored_scale)
            assert rows[0]["score"] == pytest.approx(1.0), (exact, stored_scale)
    finally:
        ws.release()


@pytest.mark.parametrize("scale", SCALES)
def test_surreal_native_cosine_near_binds_a_float32_query(scale):
    from heavenbase.backends.surreal.backend import SurrealBackend

    backend = SurrealBackend("surreal", url="ws://127.0.0.1:1/rpc")
    schema = CosineRangeDoc.schema()
    backend._schemas[schema.entity_id] = schema
    sent = []

    def query(surql, variables=None):
        sent.append((variables or {}).get("vec"))
        return []

    backend._query = query
    query_vector = [scale, scale / 100]
    payload = {"vector": query_vector, "top_k": 1, "metric": "cosine"}
    backend._run_near(QueryFragment("surreal", "near", "embedding", payload, notes={"entity_id": schema.entity_id}))
    assert len(sent) == 1
    _assert_float32_safe(sent[0], query_vector)


@pytest.mark.parametrize("scale", SCALES)
def test_neo4j_native_cosine_near_sends_a_float32_query(scale):
    from heavenbase.backends.neo4j.backend import Neo4jBackend

    backend = Neo4jBackend("neo4j", url="bolt://127.0.0.1:1")
    schema = CosineRangeDoc.schema()
    backend._schemas[schema.entity_id] = schema
    index = f"vec_{backend._label(schema.entity_id)}_embedding"
    sent = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def run(self, query, **params):
            if query.startswith("SHOW INDEXES"):
                return [{"name": index, "state": "ONLINE"}]
            sent.append(params["vector"])
            return []

    backend.driver = SimpleNamespace(session=lambda: Session())
    query_vector = [scale, scale / 100]
    payload = {"vector": query_vector, "top_k": 1, "metric": "cosine"}
    backend._run_near(QueryFragment("neo4j", "near", "embedding", payload, notes={"entity_id": schema.entity_id}))
    assert len(sent) == 1
    _assert_float32_safe(sent[0], query_vector)
