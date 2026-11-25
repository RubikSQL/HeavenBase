"""Milvus Lite exact near should rank the stored coordinates in binary64."""

from math import dist

import pytest

import heavenbase as hb

pytestmark = pytest.mark.fast


class MilvusExactDoc(hb.Entity):
    identifier = "r9vec-milvus-exact-doc"

    embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).required()


@pytest.mark.parametrize("metric", ["l2", "ip"])
def test_milvus_lite_exact_near_distinguishes_tiny_vectors(tmp_path, metric):
    pytest.importorskip("pymilvus")
    pytest.importorskip("milvus_lite")
    ws = hb.HeavenBase(
        "milvus-exact-precision",
        detached=True,
        backends={
            "main": {"type": "inmem"},
            "vec": {"type": "milvus-lite", "uri": hb.utils.path_to_file_uri(tmp_path / "exact.db")},
        },
    )
    setting = "heavenbase.backend_defaults.milvus.metric"
    try:
        ws.context.config.set(setting, metric.upper())
        ws.register(MilvusExactDoc)
        ws.upsert_many(
            MilvusExactDoc,
            [{"object_id": "a", "embedding": [-1e-24, 0.0]}, {"object_id": "z", "embedding": [1e-24, 0.0]}],
        )
        query_vector = [1e-24, 0.0]
        stored = ws.query(MilvusExactDoc).execute().rows()
        scores = {
            row["object_id"]: (
                -dist(row["embedding"], query_vector) if metric == "l2" else sum(left * right for left, right in zip(row["embedding"], query_vector))
            )
            for row in stored
        }
        assert scores["z"] > scores["a"]
        query = ws.query(MilvusExactDoc).near("embedding", query_vector, top_k=1, metric=metric, exact=True)
        result = query.execute()
        assert result.ids == ["z"], result.rows()
        assert result.rows()[0]["score"] == pytest.approx(scores["z"], rel=1e-6, abs=0.0)
        assert query.explain()["steps"][-1]["exact_guaranteed"] is True
    finally:
        ws.context.config.unset(setting)
        ws.release()


def test_milvus_lite_exact_l2_handles_large_finite_distances(tmp_path):
    pytest.importorskip("pymilvus")
    pytest.importorskip("milvus_lite")
    ws = hb.HeavenBase(
        "milvus-exact-distance",
        detached=True,
        backends={
            "main": {"type": "inmem"},
            "vec": {"type": "milvus-lite", "uri": hb.utils.path_to_file_uri(tmp_path / "distance.db")},
        },
    )
    try:
        ws.register(MilvusExactDoc)
        ws.upsert(MilvusExactDoc, {"object_id": "document", "embedding": [1.0, 0.0]})
        query = ws.query(MilvusExactDoc).near("embedding", [1e155, 0.0], top_k=1, metric="l2", exact=True)
        for _ in range(2):  # Exercise both the initial scan and its retained generation.
            result = query.execute()
            assert result.ids == ["document"]
            assert result.rows()[0]["score"] == pytest.approx(-1e155)
    finally:
        ws.release()


def test_milvus_lite_ordinary_cosine_uses_scan_batch_size(tmp_path):
    pytest.importorskip("pymilvus")
    pytest.importorskip("milvus_lite")
    ws = hb.HeavenBase(
        "milvus-cosine-batch-size",
        detached=True,
        backends={
            "main": {"type": "inmem"},
            "vec": {"type": "milvus-lite", "uri": hb.utils.path_to_file_uri(tmp_path / "cosine.db")},
        },
    )
    setting = "heavenbase.query.near.scan_batch_size"
    try:
        ws.register(MilvusExactDoc)
        ws.upsert(MilvusExactDoc, {"object_id": "a", "embedding": [1.0, 0.0]})
        query = ws.query(MilvusExactDoc).near("embedding", [1.0, 0.0], top_k=1)
        assert query.execute().ids == ["a"]
        ws.context.config.set(setting, 0)
        with pytest.raises(ValueError, match="batch_size"):
            query.execute()
    finally:
        ws.context.config.unset(setting)
        ws.release()
