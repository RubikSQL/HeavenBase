"""Explicit candidate reuse has freshness, isolation, capacity, and streaming contracts."""

from dataclasses import FrozenInstanceError
import threading

import heavenbase as hb
import pytest

from heavenbase.backends.vector.handler.milvus import milvus_near_handler
from heavenbase.execution.contracts import RowOp
from heavenbase.execution.frame.result import ResultFrame
from heavenbase.execution.handlers.base import Handler
from heavenbase.execution.handlers.near import _PreparedVectors, _batch_scores
from heavenbase.workspace.query.cache import _CandidateCache

pytestmark = [pytest.mark.fast, pytest.mark.unit]


class CachedDoc(hb.Entity):
    scope = hb.field(hb.ShortText)
    extra = hb.field(hb.Json)
    embedding = hb.field(hb.Vector[2]).store(to="main")


@pytest.fixture
def workspace():
    ws = hb.HeavenBase("near-cache-contract", detached=True, backends={"main": {"type": "inmem"}})
    ws.register(CachedDoc)
    ws.upsert_many(
        CachedDoc,
        [
            {"object_id": "a", "scope": "own", "extra": {"nested": [1]}, "embedding": [1, 0]},
            {"object_id": "b", "scope": "own", "extra": {"nested": [2]}, "embedding": [0, 1]},
            {"object_id": "c", "scope": "foreign", "extra": {}, "embedding": [1, 0]},
        ],
    )
    try:
        yield ws
    finally:
        ws.release()


def query(ws, vector=(1, 0), *, version="v1"):
    return ws.query(CachedDoc).where({"scope": "own"}).near("embedding", list(vector), top_k=1, exact=True, cache_version=version)


def test_cache_reuses_candidates_not_results_and_is_detached(workspace, monkeypatch):
    ws = workspace
    rows = query(ws).execute().rows()
    rows[0]["embedding"][0] = -999
    rows[0]["extra"]["nested"].append(999)

    def forbid(*args, **kwargs):
        pytest.fail("Warm versioned candidates must not reread storage")

    monkeypatch.setattr(ws._row_store, "iter_rows", forbid)
    assert query(ws).execute().rows()[0]["extra"] == {"nested": [1]}
    assert query(ws, (0, 1)).execute().ids == ["b"]
    assert ws.query(CachedDoc).where({"scope": "foreign"}).near("embedding", [1, 0], top_k=1, exact=True, cache_version="v1").execute().ids == ["c"]


def test_live_reads_external_changes_and_refresh_invalidates(workspace):
    ws = workspace
    assert query(ws).execute().ids == ["a"]
    backend = ws.backends.get("main")
    backend.upsert([RowOp(CachedDoc.schema().entity_id, "a", {"scope": "own", "extra": {}, "embedding": [-1, 0]})])
    assert query(ws).execute().ids == ["a"]  # explicit reuse, not automatic freshness
    assert query(ws, version=None).execute().ids == ["b"]
    ws.refresh(entity=CachedDoc)
    assert query(ws).execute().ids == ["b"]
    backend.upsert([RowOp(CachedDoc.schema().entity_id, "a", {"scope": "own", "extra": {}, "embedding": [1, 0]})])
    assert query(ws, version="v2").execute().ids == ["a"]
    assert len(ws._router._near.cache.entries) == 1


def test_owned_same_count_update_delete_and_failure_invalidate(workspace, monkeypatch):
    ws = workspace
    query(ws).execute()
    ws.set("a", {"embedding": [-1, 0]}, entity=CachedDoc)
    assert not ws._router._near.cache.entries
    assert query(ws).execute().ids == ["b"]
    backend = ws.backends.get("main")
    original = backend.upsert

    def failed(ops):
        original(ops)
        raise RuntimeError("partial write")

    with monkeypatch.context() as patch:
        patch.setattr(backend, "upsert", failed)
        with pytest.raises(RuntimeError, match="partial write"):
            ws.set("a", {"embedding": [1, 0]}, entity=CachedDoc)
    assert not ws._router._near.cache.entries
    assert query(ws).execute().ids == ["a"]
    ws.delete((CachedDoc, "a"))
    assert query(ws).execute().ids == ["b"]
    ws.release()
    assert ws._router._near.cache.bytes == 0


@pytest.mark.slow
@pytest.mark.parametrize("bad", ["", " ", 1, False, []])
def test_version_validation(workspace, bad):
    with pytest.raises((TypeError, ValueError)):
        query(workspace, version=bad)


def test_version_is_immutable_and_survives_json(workspace):
    q = query(workspace)
    assert q.to_dict()["near"]["cache_version"] == "v1"
    with pytest.raises(FrozenInstanceError):
        q.spec.near.cache_version = "v2"
    loaded = workspace.query_json(
        CachedDoc, {"filter": {"scope": "own"}, "near": {"field": "embedding", "query": [1, 0], "exact": True, "cache_version": "v1"}}
    )
    assert loaded.spec.near.cache_version == "v1"
    assert loaded.explain()["steps"][-1]["cache"]["mode"] == "versioned_candidates"
    with pytest.raises(ValueError, match="requires exact"):
        workspace.query(CachedDoc).near("embedding", [1, 0], cache_version="v1")


@pytest.mark.slow
@pytest.mark.parametrize("metric", ["cosine", "l2", "ip"])
def test_prepared_vectors_preserve_live_scores_and_projection(workspace, metric):
    ws = workspace

    def run(version, vector):
        return (
            ws.query(CachedDoc)
            .near("embedding", vector, top_k=3, metric=metric, exact=True, cache_version=version)
            .select("object_id", "score")
            .execute()
            .rows()
        )

    for vector in ([1.0, 0.0], [0.0, 1.0], [0.0, 0.0]):
        assert run("v1", vector) == run(None, vector)
    entry = ws._router._near.cache.entries[CachedDoc.schema().entity_id]
    assert entry.vectors and entry.size <= 67108864
    assert not entry.vectors["embedding"].values.flags.writeable


def test_disabling_cache_cannot_reuse_old_prepared_vectors(workspace):
    ws = workspace
    query(ws).execute()
    query(ws).execute()
    ws.backends.get("main").upsert([RowOp(CachedDoc.schema().entity_id, "a", {"scope": "own", "extra": {}, "embedding": [-1, 0]})])
    ws.context.config.set("heavenbase.query.near.cache.max_entries", 0)
    try:
        assert query(ws).execute().ids == ["b"]
        assert ws._router._near.cache.bytes == 0
    finally:
        ws.context.config.unset("heavenbase.query.near.cache.max_entries")


@pytest.mark.slow
@pytest.mark.parametrize("bound", ["max_entries", "max_bytes"])
def test_explain_reports_a_live_read_when_a_zero_bound_disables_reuse(workspace, bound):
    """explain() reports the cache mode execution uses: a zero bound reads live, so no versioned_candidates."""
    ws = workspace
    cache = ws.query(CachedDoc).near("embedding", [1, 0], top_k=1, exact=True, cache_version="v1").explain()["steps"][-1]["cache"]
    assert (cache["mode"], cache["max_bytes"], cache["max_entries"]) == ("versioned_candidates", 67108864, 16)
    ws.context.config.set(f"heavenbase.query.near.cache.{bound}", 0)
    try:
        cache = query(ws).explain()["steps"][-1]["cache"]
        query(ws).execute()
        query(ws).execute()
        assert not ws._router._near.cache.entries  # execution retained nothing ...
        assert cache["mode"] == "live" and cache[bound] == 0  # ... and explain says it reads live
    finally:
        ws.context.config.unset(f"heavenbase.query.near.cache.{bound}")


@pytest.mark.slow
@pytest.mark.parametrize("bound", ["max_entries", "max_bytes"])
def test_zero_bound_keeps_proven_exact_native_near(workspace, monkeypatch, bound):
    """A proven Milvus FLAT route dispatches to the provider even with candidate reuse disabled."""
    ws = workspace
    binding = ws.storage_plans[CachedDoc.identifier].first("embedding")
    backend = ws.backends.get(binding.backend)
    monkeypatch.setattr(backend, "index_type", "FLAT", raising=False)
    ws.handlers.register(Handler(hb.Vector, "near", backend.type, binding.strategy, milvus_near_handler, resolver=ws.module_resolver), replace=True)

    def execute_native(fragment):
        assert fragment.kind("") == "milvus_search"
        assert fragment.notes["exact"] is True
        return ResultFrame.from_rows([{"object_id": "a", "score": 1.0}])

    monkeypatch.setattr(backend, "execute", execute_native)
    key = f"heavenbase.query.near.cache.{bound}"
    ws.context.config.set(key, 0)
    try:
        near = ws.query(CachedDoc).near("embedding", [1, 0], top_k=1, exact=True, cache_version="v1")
        step = near.explain()["steps"][-1]
        assert step["handler_mode"] == "native" and step["cache"]["mode"] == "live"
        assert near.execute().ids == ["a"]
        assert not ws._router._near.cache.entries
    finally:
        ws.context.config.unset(key)


@pytest.mark.slow
@pytest.mark.parametrize("bound", ["max_entries", "max_bytes"])
def test_explain_rejects_a_negative_bound_as_execution_does(workspace, bound):
    """A negative bound fails the scan's execution, so explain() must fail the same way instead of reporting a live read."""
    ws = workspace
    ws.context.config.set(f"heavenbase.query.near.cache.{bound}", -1)
    try:
        for cache_version in (None, "v1"):
            near = ws.query(CachedDoc).near("embedding", [1, 0], top_k=1, exact=True, cache_version=cache_version)
            with pytest.raises(ValueError, match="near cache bounds must be non-negative"):
                near.execute()
            with pytest.raises(ValueError, match="near cache bounds must be non-negative"):
                near.explain()
    finally:
        ws.context.config.unset(f"heavenbase.query.near.cache.{bound}")


@pytest.mark.slow
@pytest.mark.parametrize("bound", ["max_entries", "max_bytes"])
def test_non_exact_scan_explain_accepts_unused_negative_bounds(workspace, bound):
    class LiveDoc(hb.Entity):
        embedding = hb.field(hb.Vector[2]).store(to="main", strategy=hb.VectorIndex)

    workspace.register(LiveDoc)
    workspace.upsert(LiveDoc, {"object_id": "live", "embedding": [0, 1]})
    key = f"heavenbase.query.near.cache.{bound}"
    workspace.context.config.set(key, -1)
    try:
        near = workspace.query(LiveDoc).near("embedding", [0, 1], top_k=1)
        assert near.execute().ids == ["live"]
        assert near.explain()["steps"][-1]["cache"]["mode"] == "live"
    finally:
        workspace.context.config.unset(key)


@pytest.mark.slow
@pytest.mark.parametrize("vector", [[float("nan"), 0.0], [1.0], 3.0])
def test_preparation_does_not_validate_excluded_vectors(workspace, vector):
    ws = workspace
    ws.backends.get("main").upsert([RowOp(CachedDoc.schema().entity_id, "c", {"scope": "foreign", "extra": {}, "embedding": vector})])
    assert query(ws).execute().ids == ["a"]
    assert query(ws).execute().ids == ["a"]
    with pytest.raises(ValueError):
        ws.query(CachedDoc).where({"scope": "foreign"}).near("embedding", [1, 0], exact=True, cache_version="v1").execute()


@pytest.mark.slow
def test_empty_versioned_candidates_remain_empty(workspace):
    ws = workspace
    for object_id in ("a", "b", "c"):
        ws.delete((CachedDoc, object_id))
    assert query(ws).execute().ids == []
    assert query(ws).execute().ids == []


def test_bounded_cache_overflow_eviction_errors_and_cancellation():
    cache = _CandidateCache()

    def run(entity="one", version="v1", source=lambda: iter([{"object_id": "x", "v": [1.0, 0.0]}]), budget=4096, entries=1):
        return cache.rows(entity, version, source, max_bytes=budget, max_entries=entries)

    expected = list(run())
    assert cache.entries and cache.bytes <= 4096
    assert list(run("two")) == expected
    assert list(cache.entries) == ["two"]
    assert list(run(budget=1)) == expected
    assert not cache.entries and cache.bytes == 0

    def broken():
        yield {"object_id": "partial"}
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        list(run(source=broken))
    assert not cache.entries
    pending = run()
    next(pending)
    pending.close()
    assert not cache.entries
    assert list(run(source=lambda: iter([]), budget=1)) == []
    assert not cache.entries


@pytest.mark.slow
def test_sql_streams_before_hydration_and_never_calls_full_scan(tmp_path, monkeypatch):
    ws = hb.HeavenBase("near-stream-sql", detached=True, backends={"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "stream.db")}})
    try:
        ws.register(CachedDoc)
        ws.upsert_many(CachedDoc, [{"object_id": f"r{i:04}", "scope": "own", "extra": {}, "embedding": [float(i), 1.0]} for i in range(620)])
        backend = ws.backends.get("main")
        sizes = []
        original = backend.db.orm_execute

        def tracked(stmt, *args, **kwargs):
            result = original(stmt, *args, **kwargs)
            limit = getattr(stmt, "_limit_clause", None)
            if getattr(stmt, "is_select", False) and limit is not None:
                # A reusable page construct binds its LIMIT as a parameter; the
                # page size then travels with the execution parameters.
                value = limit.value
                if value is None:
                    params = args[0] if args and isinstance(args[0], dict) else kwargs.get("params") or {}
                    value = params.get(getattr(limit, "key", "page_size"))
                sizes.append(int(value))
            return result

        def forbidden(*args, **kwargs):
            pytest.fail("Exact fallback must not materialize a full scan or universe")

        monkeypatch.setattr(backend.db, "orm_execute", tracked)
        monkeypatch.setattr(backend, "scan", forbidden)
        monkeypatch.setattr(ws._row_store, "universe", forbidden)
        monkeypatch.setattr(ws._row_store, "hydrate_query_frame", forbidden)
        assert query(ws, version=None).execute().ids == ["r0619"]
        assert sizes and max(sizes) == 256 and len(sizes) >= 3
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("change", ["added", "changed"])
def test_straggler_scores_the_generation_it_serves_when_a_reader_switches_versions(workspace, monkeypatch, change):
    ws = workspace
    entity_id = CachedDoc.schema().entity_id

    def ranked(version):
        near = ws.query(CachedDoc).where({"scope": "own"}).near("embedding", [1, 0], top_k=3, exact=True, cache_version=version)
        return {row["object_id"]: row["score"] for row in near.select("object_id", "score").execute().rows()}

    ranked("v1")
    ranked("v1")
    assert ws._router._near.cache.entries[entity_id].vectors  # the v1 generation retains a prepared matrix
    ops = [RowOp(entity_id, "a", {"scope": "own", "extra": {}, "embedding": [-1, 0]})]
    if change == "added":
        ops.append(RowOp(entity_id, "new", {"scope": "own", "extra": {}, "embedding": [1, 0]}))
    ws.backends.get("main").upsert(ops)  # an external write, announced by bumping the version
    original = _CandidateCache.prepare
    newer = []

    def interleave(self, *args, **kwargs):
        prepared = original(self, *args, **kwargs)
        if args[1] == "v1" and not newer:
            # a concurrent reader with the bumped version replaces the generation this straggler prepared from
            reader = threading.Thread(target=lambda: newer.append(ranked("v2")))
            reader.start()
            reader.join(60)
            assert newer, "readers share the workspace lock"
        return prepared

    monkeypatch.setattr(_CandidateCache, "prepare", interleave)
    straggler = ranked("v1")

    assert newer[0] == straggler == ranked(None)
    assert straggler["a"] == pytest.approx(-1.0)


def test_a_warm_generation_scores_its_own_rows_with_the_prepared_matrix(workspace, monkeypatch):
    pytest.importorskip("numpy")
    from heavenbase.execution.handlers import near as handlers

    original = handlers._prepared_positions
    mapped = []
    monkeypatch.setattr(handlers, "_prepared_positions", lambda *args: mapped.append(original(*args)) or mapped[-1])
    for _ in range(3):
        query(workspace).execute()

    # the scan serves the retained rows themselves, so their generation's matrix scores every warm batch
    assert mapped and all(positions is not None for positions in mapped)


def test_prepared_vectors_score_only_the_row_objects_they_were_built_from():
    pytest.importorskip("numpy")
    rows = [{"object_id": "a", "v": [1.0, 0.0]}, {"object_id": "b", "v": [0.0, 1.0]}]
    prepared = _PreparedVectors(rows, "v")

    def scores(batch):
        return _batch_scores(batch, "v", [1.0, 0.0], "cosine", lambda row: pytest.fail("NumPy scores the batch"), prepared=prepared)

    assert scores(rows) == [1.0, 0.0]
    live = [{"object_id": "a", "v": [-1.0, 0.0]}, {"object_id": "b", "v": [0.0, 1.0]}, {"object_id": "new", "v": [1.0, 0.0]}]
    assert scores(live) == [-1.0, 0.0, 1.0]
    assert scores([rows[1], live[0]]) == [0.0, -1.0]
