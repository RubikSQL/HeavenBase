"""Exact cosine near scores stored vectors of extreme finite magnitude by direction, like the scan."""

import pytest

import heavenbase as hb
from heavenbase.execution.handlers import near as near_module
from heavenbase.types import cosine_score

pytestmark = [pytest.mark.fast, pytest.mark.unit]

# Each case: stored rows, a query, and the expected order. The first row is the extreme one.
CASES = {
    # Norm 1e308 is representable, but times |q| = 1.8 it is not.
    "huge-norm": ([[1e308, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [-1.0, 0.0, 0.0, 0.0]], [0.9, 0.9, 0.9, 0.9]),
    # Norm 1.41e308 is representable, but the raw dot product 1.8e308 is not.
    "huge-dot": ([[1e308, 1e308, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [-1.0, 0.0, 0.0, 0.0]], [0.9, 0.9, 0.9, 0.9]),
    # Finite coordinates whose norm 2.1e308 exceeds binary64: the direction is still well defined.
    "norm-beyond-binary64": ([[1.5e308, 1.5e308, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [-1.0, 0.0, 0.0, 0.0]], [0.9, 0.9, 0.9, 0.9]),
    # Subnormal coordinates: raw products round to a few units in the last place.
    "subnormal": ([[1e-323, 0.0], [1.0, 0.4], [-1.0, 0.0]], [3.0, 1.0]),
}


def _rows(vectors):
    return [{"object_id": oid, "v": vector} for oid, vector in zip("abc", vectors)]


@pytest.mark.parametrize("use_prepared", [False, True])
@pytest.mark.parametrize("case", sorted(CASES))
def test_batch_cosine_matches_the_scalar_scorer_for_extreme_stored_vectors(case, use_prepared):
    pytest.importorskip("numpy")
    vectors, query = CASES[case]
    rows = _rows(vectors)
    prepared = near_module._PreparedVectors(rows, "v") if use_prepared else None
    scores = near_module._batch_scores(rows, "v", query, "cosine", None, prepared=prepared)
    assert scores == pytest.approx([cosine_score(vector, query) for vector in vectors], rel=1e-12, abs=0.0)


def test_batch_cosine_keeps_ordinary_scores_bit_for_bit():
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(3121)
    rows = [{"object_id": f"r{i}", "v": vector} for i, vector in enumerate(rng.normal(size=(64, 32)).tolist())]
    query = near_module._scale_cosine_query(rng.normal(size=32).tolist())
    vectors = np.asarray([row["v"] for row in rows])
    q = np.asarray(query)
    expected = np.sum(vectors * q, axis=1) / (np.sqrt(np.sum(vectors * vectors, axis=1)) * np.sqrt(np.sum(q * q)))
    for prepared in (None, near_module._PreparedVectors(rows, "v")):
        assert near_module._batch_scores(rows, "v", query, "cosine", None, prepared=prepared) == expected.tolist()


@pytest.mark.slow
@pytest.mark.parametrize("case", sorted(CASES))
def test_exact_cosine_near_ranks_extreme_stored_vectors_like_the_scan(case):
    pytest.importorskip("numpy")
    vectors, query = CASES[case]
    dimension = len(query)

    class Doc(hb.Entity):
        identifier = f"exact-cosine-extreme-{case}"
        v = hb.field(hb.Vector[dimension]).store(to="vec", strategy=hb.VectorIndex.identifier).required()

    backends = {"main": {"type": "inmem"}, "vec": {"type": "inmem"}}
    ws = hb.HeavenBase(f"exact-cosine-extreme-{case}", detached=True, backends=backends)
    try:
        ws.register(Doc)
        ws.upsert_many(Doc, _rows(vectors))
        scan = ws.query(Doc).near("v", query, metric="cosine", top_k=3, exact=False).execute().rows()
        for _ in range(2):  # the second run may score from the prepared generation
            exact = ws.query(Doc).near("v", query, metric="cosine", top_k=3, exact=True).execute().rows()
            assert [row["object_id"] for row in exact] == [row["object_id"] for row in scan]
            assert [row["score"] for row in exact] == pytest.approx([row["score"] for row in scan], rel=1e-12, abs=0.0)
    finally:
        ws.release()
