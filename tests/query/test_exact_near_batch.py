"""Portable exact acceleration belongs to HB execution, not an application pool."""

import builtins

import heavenbase as hb
import pytest

from heavenbase.execution.handlers import near as near_module
from heavenbase.types import cosine_score, euclidean_score, inner_product_score

# The workspace query module binds ``_batch_scores`` when it is imported. Import it before any
# test patches the handler, so it never keeps a test's wrapper for the rest of the process.
import heavenbase.workspace.query.near

pytestmark = [pytest.mark.fast, pytest.mark.unit]


@pytest.mark.slow
@pytest.mark.parametrize("metric,scorer", [("cosine", cosine_score), ("l2", euclidean_score), ("ip", inner_product_score)])
@pytest.mark.parametrize("dimension", [3, 17, 1024])
def test_batch_metrics_match_hb_and_ties_survive_batch_boundaries(monkeypatch, metric, scorer, dimension):
    np = pytest.importorskip("numpy")

    class Doc(hb.Entity):
        scope = hb.field(hb.ShortText)
        embedding = hb.field(hb.Vector[dimension]).store(to="main")

    rng = np.random.default_rng(829)
    vectors = rng.normal(size=(600, dimension)).tolist()
    vectors[599] = vectors[0]
    vectors[256] = [0.0] * dimension
    query_vector = rng.normal(size=dimension).tolist()
    sizes = []
    original = near_module._batch_scores

    def tracked(rows, *args, **kwargs):
        sizes.append(len(rows))
        return original(rows, *args, **kwargs)

    monkeypatch.setattr(near_module, "_batch_scores", tracked)
    ws = hb.HeavenBase("exact-batch", detached=True, backends={"main": {"type": "inmem"}})
    try:
        ws.register(Doc)
        ws.upsert_many(Doc, [{"object_id": f"r{i:04}", "scope": "own", "embedding": v} for i, v in enumerate(vectors)])
        ws.upsert(Doc, {"object_id": "foreign", "scope": "other", "embedding": query_vector})
        query = ws.query(Doc).where({"scope": "own"}).near("embedding", query_vector, top_k=700, metric=metric, exact=True)
        rows = query.execute().rows()
        reference = {f"r{i:04}": scorer(v, query_vector) for i, v in enumerate(vectors)}
        actual = {row["object_id"]: row["score"] for row in rows}
        assert actual == pytest.approx(reference, rel=1e-12, abs=1e-12)
        assert [row["object_id"] for row in rows] == sorted(reference, key=lambda oid: (-reference[oid], oid))
        assert actual["r0000"] == actual["r0599"]
        assert sizes == [256, 256, 88]
        ws.set("r0000", {"embedding": [0.0] * dimension}, entity=Doc)
        updated = {row["object_id"]: row["score"] for row in query.execute().rows()}
        assert updated["r0000"] == pytest.approx(scorer([0.0] * dimension, query_vector))
    finally:
        ws.release()


def test_batch_without_numpy_uses_existing_scalar_function(monkeypatch):
    original = builtins.__import__

    def without_numpy(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("optional acceleration unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_numpy)
    rows = [{"v": [1.0, 2.0]}, {"v": [0.0, 0.0]}, {}]

    def scalar(row):
        return cosine_score(row["v"], [1.0, 1.0]) if "v" in row else None

    assert near_module._batch_scores(rows, "v", [1.0, 1.0], "cosine", scalar) == [scalar(row) for row in rows]


def test_scalar_euclidean_score_handles_large_finite_distances():
    assert euclidean_score([1.0, 0.0], [1e155, 0.0]) == pytest.approx(-1e155)


@pytest.mark.parametrize("vector", [[1.0], [float("nan"), 1.0], [float("inf"), 0.0]])
def test_batch_invalid_vectors_do_not_become_plausible_scores(vector):
    pytest.importorskip("numpy")
    with pytest.raises(ValueError):
        near_module._batch_scores([{"v": vector}], "v", [1.0, 0.0], "cosine", None)


@pytest.mark.parametrize("use_prepared", [False, True])
def test_batch_l2_matches_math_dist_to_rounding(use_prepared):
    np = pytest.importorskip("numpy")
    from math import dist

    rng = np.random.default_rng(4170)
    rows = [{"object_id": f"doc-{i}", "v": vector} for i, vector in enumerate(rng.normal(size=(2000, 768)).tolist())]
    query = rng.normal(size=768).tolist()
    prepared = near_module._PreparedVectors(rows, "v") if use_prepared else None
    scores = near_module._batch_scores(rows, "v", query, "l2", None, prepared=prepared)
    worst = max(abs(score + dist(row["v"], query)) / dist(row["v"], query) for row, score in zip(rows, scores))
    # A sum of squares stays within a few units in the last place; a per-coordinate hypot chain drifts.
    assert worst <= 1e-15


def test_batch_l2_keeps_extreme_and_zero_distances():
    pytest.importorskip("numpy")
    from math import dist

    query = [1e-170, 0.0]
    vectors = [[1e-170, 0.0], [-1e-170, 3e-170], [5e-160, -2e-155], [3.0, -4.0], [1e155, 2e155], [-1e300, 1e300]]
    rows = [{"object_id": f"doc-{i}", "v": vector} for i, vector in enumerate(vectors)]
    scores = near_module._batch_scores(rows, "v", query, "euclidean", None)
    assert scores[0] == 0.0
    for vector, score in zip(vectors[1:], scores[1:]):
        assert score == pytest.approx(-dist(vector, query), rel=1e-15, abs=0.0)
    with pytest.raises(ValueError, match="non-finite"):
        near_module._batch_scores([{"object_id": "far", "v": [1e308, 0.0]}], "v", [-1e308, 0.0], "l2", None)


@pytest.mark.parametrize("use_prepared", [False, True])
def test_batch_l2_scores_zero_dimension_vectors_as_zero_distance(use_prepared):
    pytest.importorskip("numpy")
    rows = [{"object_id": "first", "v": []}, {"object_id": "second", "v": []}, {"object_id": "empty"}]
    prepared = near_module._PreparedVectors(rows, "v") if use_prepared else None
    scores = near_module._batch_scores(rows, "v", [], "l2", None, prepared=prepared)
    assert scores == [euclidean_score([], []), euclidean_score([], []), None]
    assert scores[0] == 0.0
