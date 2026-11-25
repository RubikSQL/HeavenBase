"""Exact intent uses the ordinary query pipeline, without a parallel pool API."""

from dataclasses import FrozenInstanceError, replace

import heavenbase as hb
import pytest
from heavenbase.backends.base import Backend

pytestmark = [pytest.mark.fast, pytest.mark.unit]


class ExactDoc(hb.Entity):
    scope = hb.field(hb.ShortText)
    embedding = hb.field(hb.Vector[2]).store(to="main")


@pytest.fixture
def workspace():
    ws = hb.HeavenBase("exact-near-contract", detached=True, backends={"main": {"type": "inmem"}})
    ws.register(ExactDoc)
    ws.upsert_many(
        ExactDoc,
        [
            {"object_id": "b", "scope": "one", "embedding": [1, 0]},
            {"object_id": "a", "scope": "one", "embedding": [1, 0]},
            {"object_id": "z", "scope": "one", "embedding": [0, 0]},
            {"object_id": "n", "scope": "one", "embedding": [-1, 0]},
            {"object_id": "other", "scope": "two", "embedding": [2, 1]},
            {"object_id": "missing", "scope": "one"},
        ],
    )
    yield ws
    ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("bad", [None, 0, 1, "true"])
def test_exact_requires_boolean(workspace, bad):
    with pytest.raises(TypeError, match="exact"):
        workspace.query(ExactDoc).near("embedding", [1, 0], exact=bad)
    with pytest.raises(TypeError, match="exact"):
        workspace.query_json(ExactDoc, {"near": {"field": "embedding", "query": [1, 0], "exact": bad}})


def test_exact_intent_is_frozen_and_survives_json_binding(workspace):
    query = workspace.query_json(ExactDoc, {"near": {"field": "embedding", "query": [1, 0], "exact": True}})
    assert query.spec.near.exact is True
    assert query.spec.near.to_dict()["exact"] is True
    assert query.limit(2).spec.near.exact is True
    with pytest.raises(FrozenInstanceError):
        query.spec.near.exact = False
    assert "exact" not in workspace.query(ExactDoc).near("embedding", [1, 0]).spec.near.to_dict()


def test_exact_full_ranking_filters_ties_and_missing_vectors(workspace):
    query = workspace.query(ExactDoc).where({"scope": "one"}).near("embedding", [1, 0], top_k=10, exact=True)
    rows = query.execute().rows()
    assert [(row["object_id"], row["score"]) for row in rows] == [("a", 1), ("b", 1), ("z", 0), ("n", -1)]
    step = query.explain()["steps"][-1]
    assert step["near_filter_mode"] == "exact_scan"
    assert step["handler_mode"] == "py_scan"
    assert step["exact_required"] and step["exact_guaranteed"]
    assert step["fallback_reason"]
    # Filtering must precede Top-K, even if the global nearest row is excluded.
    filtered = workspace.query(ExactDoc).where({"object_id": "n"}).near("embedding", [1, 0], top_k=1, exact=True)
    assert filtered.execute().ids == ["n"]
    assert filtered.count().execute().scalar() == 1
    assert workspace.query(ExactDoc).where({"scope": "absent"}).near("embedding", [1, 0], exact=True).execute().ids == []


@pytest.mark.slow
@pytest.mark.parametrize(("metric", "expected"), [("cosine", "a"), ("l2", "a"), ("ip", "other")])
def test_exact_uses_the_requested_metric(workspace, metric, expected):
    query = workspace.query(ExactDoc).near("embedding", [1, 0], top_k=1, metric=metric, exact=True)
    assert query.execute().ids == [expected]


class IndexedNearDoc(hb.Entity):
    embedding = hb.field(hb.Vector[2]).store(to="main", strategy=hb.VectorIndex)


def test_portable_near_drops_missing_vectors_for_non_cosine_metrics(workspace):
    # Euclidean scores are negative distances and inner products are unbounded, so a constant
    # score for an empty vector would outrank real neighbours on the default, non-exact path.
    workspace.register(IndexedNearDoc)
    workspace.upsert_many(
        IndexedNearDoc,
        [
            {"object_id": "b", "embedding": [1, 0]},
            {"object_id": "a", "embedding": [1, 0]},
            {"object_id": "z", "embedding": [0, 0]},
            {"object_id": "n", "embedding": [-1, 0]},
            {"object_id": "other", "embedding": [2, 1]},
            {"object_id": "missing"},
        ],
    )
    l2 = workspace.query(IndexedNearDoc).near("embedding", [1, 0], top_k=10, metric="l2")
    assert l2.explain()["steps"][-1]["handler_mode"] == "py_scan"
    rows = l2.execute().rows()
    assert sorted(row["object_id"] for row in rows[:2]) == ["a", "b"]
    assert [row["object_id"] for row in rows[2:]] == ["z", "other", "n"]
    ip = workspace.query(IndexedNearDoc).near("embedding", [1, 0], top_k=10, metric="ip").execute().ids
    assert ip[0] == "other" and ip[-1] == "n" and "missing" not in ip
    # Cosine keeps the 0.1.2.2 neutral score for an empty vector.
    cosine = workspace.query(IndexedNearDoc).near("embedding", [1, 0], top_k=10).execute().rows()
    assert {row["object_id"]: row["score"] for row in cosine}["missing"] == 0.0


def test_exact_zero_query_and_only_missing_vectors(workspace):
    assert workspace.query(ExactDoc).near("embedding", [0, 0], top_k=2, exact=True).execute().ids == ["a", "b"]
    assert workspace.query(ExactDoc).where({"object_id": "missing"}).near("embedding", [1, 0], exact=True).execute().ids == []
    workspace.delete_many([(ExactDoc, oid) for oid in ["a", "b", "z", "n", "other"]])
    assert workspace.query(ExactDoc).near("embedding", [1, 0], exact=True).execute().ids == []


def test_exact_rejects_nonfinite_and_explain_cannot_be_spoofed(workspace):
    with pytest.raises(ValueError, match="finite"):
        workspace.query(ExactDoc).near("embedding", [float("nan"), 0], exact=True).execute()
    query = workspace.query(ExactDoc).near("embedding", [1, 0], exact=True)
    spec = replace(query.spec, near=replace(query.spec.near, notes={"handler_mode": "native", "exact_guaranteed": False}))
    # The ordinary public immutable query value retains caller metadata, not authority.
    step = hb.Query(workspace, ExactDoc, spec).explain()["steps"][-1]
    assert step["handler_mode"] == "py_scan" and step["exact_guaranteed"] is True


class NativeExactDoc(hb.Entity):
    category = hb.field(hb.ShortText).required().store(to="vec")
    embedding = hb.field(hb.Vector[2]).required().store(to="vec", strategy=hb.VectorIndex)
    # Deliberately absent from vec, so filters here require a complete cross-backend read.
    scope = hb.field(hb.ShortText)


def test_real_milvus_lite_exact_scans_with_local_and_cross_backend_filters(tmp_path, monkeypatch):
    """Milvus Lite exhaustively scores eligible rows for both filter placements."""
    pytest.importorskip("pymilvus")
    pytest.importorskip("milvus_lite")
    ws = hb.HeavenBase(
        "exact-flat-live",
        detached=True,
        backends={
            "main": {"type": "inmem"},
            "vec": {"type": "milvus-lite", "uri": hb.utils.path_to_file_uri(tmp_path / "exact.db")},
        },
    )
    try:
        ws.context.config.set("heavenbase.backend_defaults.milvus.metric", "L2")
        ws.register(NativeExactDoc)
        ws.upsert_many(
            NativeExactDoc,
            [
                {"object_id": "near", "category": "one", "scope": "excluded", "embedding": [1, 0]},
                {"object_id": "middle", "category": "two", "scope": "included", "embedding": [1, 1]},
                {"object_id": "far", "category": "two", "scope": "included", "embedding": [0, 1]},
            ],
        )
        backend = ws.backends.get("vec")
        query = ws.query(NativeExactDoc).where({"category": "two"}).near("embedding", [1, 0], metric="l2", top_k=1, exact=True)
        step = query.explain()["steps"][-1]
        assert step["handler_mode"] == "py_scan" and step["exact_guaranteed"] is True, step
        assert step["near_filter_mode"] == "exact_scan"

        def forbid_search(**kwargs):
            pytest.fail("Milvus Lite native scores can lose precision")

        with monkeypatch.context() as native_patch:
            native_patch.setattr(backend.client, "search", forbid_search)
            assert query.execute().ids == ["middle"]
            assert ws.query(NativeExactDoc).near("embedding", [1, 0], metric="l2", top_k=1, exact=True).execute().ids == ["near"]
        ordinary = ws.query(NativeExactDoc).where({"category": "two"}).near("embedding", [1, 0], metric="l2", top_k=1)
        assert ordinary.explain()["steps"][-1]["near_filter_mode"] == "combined_native"
        assert ordinary.execute().ids == ["middle"]

        cross = ws.query(NativeExactDoc).where({"scope": "included"}).near("embedding", [1, 0], metric="l2", top_k=2, exact=True)
        assert cross.explain()["steps"][-1]["near_filter_mode"] == "exact_scan"
        assert cross.execute().ids == ["middle", "far"]
        # An exact scan also works when the requested metric differs from the index metric.
        ip = ws.query(NativeExactDoc).near("embedding", [1, 0], metric="ip", top_k=2, exact=True)
        assert ip.explain()["steps"][-1]["handler_mode"] == "py_scan"
        assert ip.execute().ids == ["middle", "near"]
    finally:
        ws.context.config.unset("heavenbase.backend_defaults.milvus.metric")
        ws.release()


def test_real_milvus_near_above_the_server_search_limit_returns_every_requested_neighbour(tmp_path, monkeypatch):
    """A server-limited near returns the requested neighbours through the scan, including after filtering."""
    pytest.importorskip("pymilvus")
    pytest.importorskip("milvus_lite")
    from heavenbase.backends.vector.milvus import MILVUS_MAX_TOP_K, MilvusBackend

    ws = hb.HeavenBase(
        "near-top-k-limit",
        detached=True,
        backends={
            "main": {"type": "inmem"},
            "vec": {"type": "milvus-lite", "uri": hb.utils.path_to_file_uri(tmp_path / "top-k.db")},
        },
    )
    try:
        ws.context.config.set("heavenbase.backend_defaults.milvus.metric", "L2")
        ws.register(NativeExactDoc)
        ws.upsert_many(
            NativeExactDoc,
            [
                {"object_id": "near", "category": "one", "embedding": [1, 0]},
                {"object_id": "middle", "category": "two", "embedding": [1, 1]},
                {"object_id": "far", "category": "two", "embedding": [0, 1]},
            ],
        )
        backend = ws.backends.get("vec")
        # The engine-wide maximum (1000 by default) must allow such a top_k first.
        top_k = MILVUS_MAX_TOP_K + 1
        ws.context.config.set("heavenbase.query.near.max_top_k", top_k)
        lite = ws.query(NativeExactDoc).near("embedding", [1, 0], metric="l2", top_k=top_k)
        assert lite.explain()["steps"][-1]["handler_mode"] == "native"
        assert lite.execute().ids == ["near", "middle", "far"]

        monkeypatch.setattr(backend, "provider_max_top_k", MilvusBackend.provider_max_top_k)
        at_limit = ws.query(NativeExactDoc).near("embedding", [1, 0], metric="l2", top_k=MILVUS_MAX_TOP_K)
        assert at_limit.explain()["steps"][-1]["handler_mode"] == "native"
        assert at_limit.execute().ids == ["near", "middle", "far"]

        # Even after excluding the best global match, the remaining rows exceed the server's cap.
        filler_ids = [f"filler-{index:05d}" for index in range(top_k - 2)]
        ws.upsert_many(NativeExactDoc, [{"object_id": oid, "category": "two", "embedding": [1, 1.2]} for oid in filler_ids])

        def forbid_search(**kwargs):
            pytest.fail(f"A Milvus server rejects a search limit above {MILVUS_MAX_TOP_K}")

        monkeypatch.setattr(backend.client, "search", forbid_search)
        for exact in (False, True):
            above = ws.query(NativeExactDoc).near("embedding", [1, 0], metric="l2", top_k=top_k, exact=exact)
            step = above.explain()["steps"][-1]
            assert step["handler_mode"] == "py_scan", step
            ids = above.execute().ids
            assert len(ids) == top_k
            assert ids[:2] == ["near", "middle"]
            assert set(ids) == {"near", "middle", *filler_ids}

            filtered = ws.query(NativeExactDoc).where({"category": "two"}).near("embedding", [1, 0], metric="l2", top_k=top_k, exact=exact)
            filtered_ids = filtered.execute().ids
            assert len(filtered_ids) == top_k
            assert filtered_ids[0] == "middle" and filtered_ids[-1] == "far"
            assert set(filtered_ids) == {"middle", "far", *filler_ids}
    finally:
        ws.context.config.unset("heavenbase.backend_defaults.milvus.metric")
        ws.context.config.unset("heavenbase.query.near.max_top_k")
        ws.release()


def test_exact_survives_query_compute_and_reexecutes_without_cache(workspace):
    calls = []

    def embed(text):
        calls.append(text)
        return [1, 0]

    class ComputedExactDoc(hb.Entity):
        embedding = hb.field(hb.Vector[2]).store(to="main").query_compute(embed)

    workspace.register(ComputedExactDoc)
    workspace.upsert_many(ComputedExactDoc, [{"object_id": "a", "embedding": [1, 0]}, {"object_id": "b", "embedding": [0, 1]}])
    query = workspace.query(ComputedExactDoc).near("embedding", "find it", top_k=1, exact=True)
    step = query.explain()["steps"][-1]
    assert step["query_compute"] is True and step["exact_required"] is True
    assert query.execute().ids == ["a"]
    workspace.set("a", {"embedding": [-1, 0]}, entity=ComputedExactDoc)
    assert query.execute().ids == ["b"]
    assert calls == ["find it"] * 3


def test_server_flat_configuration_is_explicit_and_not_a_client_kwarg(monkeypatch):
    """Selecting FLAT changes the owned index contract, not client connection options."""
    from heavenbase.backends.vector.milvus import MilvusBackend

    monkeypatch.setattr(MilvusBackend, "_connect", lambda self: None)
    default = MilvusBackend("vec")
    flat = MilvusBackend.from_config("vec", {"index_type": "flat"})
    assert default.index_type == "AUTOINDEX"
    assert flat.index_type == "FLAT"
    assert "index_type" not in flat._client_kwargs
    for invalid in ["HNSW", 1, ""]:
        with pytest.raises(ValueError, match="index_type"):
            MilvusBackend("vec", index_type=invalid)


class PortablePagingBackend(hb.backends.InMemBackend):
    """Provider without a native keyset page whose iter_scan holds its lock across yields, like Chroma or a client iterator."""

    scan_page = Backend.scan_page

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.iter_scans = 0

    def iter_scan(self, entity_id, *, batch_size, columns=None):
        self.iter_scans += 1
        yield from super().iter_scan(entity_id, batch_size=batch_size, columns=columns)


class SplitExactDoc(hb.Entity):
    scope = hb.field(hb.ShortText)
    embedding = hb.field(hb.Vector[2]).store(to="vec")


def _paged_workspace(name, **backends):
    ws = hb.HeavenBase(name, detached=True, backends=backends)
    ws.register(ExactDoc)
    ws.register(SplitExactDoc)
    rows = [{"object_id": f"r{index:02}", "scope": "one", "embedding": [1, index]} for index in range(9)]
    ws.upsert_many(ExactDoc, rows)
    ws.upsert_many(SplitExactDoc, rows)
    ws.context.config.set("heavenbase.query.near.scan_batch_size", 2)
    return ws


def _top3(ws, entity):
    return ws.query(entity).where({"scope": "one"}).near("embedding", [1, 0], top_k=3, exact=True).execute().ids


def test_exact_scan_reads_a_route_without_native_pages_in_one_pass():
    main, vec = PortablePagingBackend("main"), hb.backends.InMemBackend("vec")
    ws = _paged_workspace("exact-near-portable-pages", main=main, vec=vec)
    try:
        main.iter_scans = 0
        # Nine rows at batch size 2 are five pages; the portable page would enumerate the route six times.
        assert _top3(ws, ExactDoc) == ["r00", "r01", "r02"]
        assert main.iter_scans == 1
        main.iter_scans = 0
        assert _top3(ws, SplitExactDoc) == ["r00", "r01", "r02"]  # the other route pages natively
        assert main.iter_scans == 1
        # Public paging releases the workspace lock between pages, so it keeps the keyset page.
        main.iter_scans = 0
        assert len(list(ws.iter_rows(ExactDoc, batch_size=2))) == 9
        assert main.iter_scans == 6
    finally:
        ws.context.config.unset("heavenbase.query.near.scan_batch_size")
        ws.release()


def test_exact_scan_keeps_keyset_pages_when_two_routes_lack_native_pages():
    # Two routes that both held their locks across the merge's lookups into each other could
    # deadlock concurrent scans, so neither is streamed; the answer is unchanged.
    main, vec = PortablePagingBackend("main"), PortablePagingBackend("vec")
    ws = _paged_workspace("exact-near-two-portable-routes", main=main, vec=vec)
    try:
        main.iter_scans = vec.iter_scans = 0
        assert _top3(ws, SplitExactDoc) == ["r00", "r01", "r02"]
        assert main.iter_scans == 6 and vec.iter_scans == 6
        assert _top3(ws, ExactDoc) == ["r00", "r01", "r02"]  # a single-route entity is unaffected
        assert main.iter_scans == 7
    finally:
        ws.context.config.unset("heavenbase.query.near.scan_batch_size")
        ws.release()


class ExactTaggedDoc(hb.Entity):
    identifier = "exact-tagged-doc"

    keywords = hb.field(hb.Array[hb.ShortText]).default([])
    embedding = hb.field(hb.Vector[2]).store(to="main")


@pytest.mark.slow
@pytest.mark.parametrize(
    ("backend", "where"),
    [
        ("inmem", {"keywords": {"exists": True}}),
        ("sqlite", {"keywords": {"exists": True}}),
        ("inmem", {"keywords": {"contained_in": "x y"}}),
        ("sqlite", {"keywords": {"contained_in": "x y"}}),
    ],
)
def test_exact_near_with_an_unsupported_filter_is_explained_and_refused_on_execute(tmp_path, backend, where):
    """explain() reports an unsupported filter under exact near as it does without near; execute() still refuses it."""
    config = {"type": "inmem"} if backend == "inmem" else {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "exact.db")}
    ws = hb.HeavenBase(f"exact-near-unsupported-{backend}", detached=True, backends={"main": config})
    try:
        ws.register(ExactTaggedDoc, gram=False)
        ws.upsert(ExactTaggedDoc, {"object_id": "a", "keywords": ["x"], "embedding": [1, 0]})
        reason = ws.query(ExactTaggedDoc).where(where).explain()["steps"][-1]["unsupported_reason"]
        assert reason

        query = ws.query(ExactTaggedDoc).where(where).near("embedding", [1, 0], exact=True)
        steps = query.explain()["steps"]
        assert [step["unsupported_reason"] for step in steps if step["node"] == "filter"] == [reason]
        assert steps[-1]["node"] == "near" and steps[-1]["near_filter_mode"] == "unsupported"
        with pytest.raises(LookupError):
            query.execute()
    finally:
        ws.release()


def test_exact_near_refuses_an_unsupported_filter_on_a_native_near_route(workspace, monkeypatch):
    """A near route that compiles natively reaches the shared route selector, which refuses the exact unsupported pair."""
    router = workspace._router
    near = router._near
    monkeypatch.setattr(near, "uses_exact_scan", lambda *args, **kwargs: False)
    monkeypatch.setattr(near, "expr_has_unsupported", lambda *args, **kwargs: True)
    query = workspace.query(ExactDoc).where({"scope": "one"}).near("embedding", [1, 0], exact=True)
    assert query.explain()["steps"][-1]["near_filter_mode"] == "unsupported"
    with pytest.raises(LookupError, match="Exact near cannot execute an unsupported candidate filter"):
        query.execute()


def test_scan_batch_size_applies_to_portable_exact_near(workspace):
    """A generic combined scan can run independently of the exact scan batch size."""

    class ScanNearDoc(hb.Entity):
        identifier = "scan-batch-near-doc"

        scope = hb.field(hb.ShortText)
        embedding = hb.field(hb.Vector[2])

    workspace.register(ScanNearDoc)
    workspace.upsert(ScanNearDoc, {"object_id": "near", "scope": "one", "embedding": [1, 0]})
    workspace.upsert(ScanNearDoc, {"object_id": "far", "scope": "one", "embedding": [-1, 0]})
    workspace.context.config.set("heavenbase.query.near.scan_batch_size", 0)
    try:
        query = workspace.query(ScanNearDoc).where({"scope": "one"}).near("embedding", [1, 0], top_k=1)
        assert [step["near_filter_mode"] for step in query.explain()["steps"] if step.get("node") == "near"] == ["combined_scan"]
        assert query.execute().ids == ["near"]
        with pytest.raises(ValueError, match="scan_batch_size must be positive"):
            workspace.query(ExactDoc).where({"scope": "one"}).near("embedding", [1, 0], top_k=2, exact=True).execute()
    finally:
        workspace.context.config.unset("heavenbase.query.near.scan_batch_size")
