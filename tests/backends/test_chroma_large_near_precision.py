"""Chroma should rescore the embeddings returned for large finite queries."""

import pytest

import heavenbase as hb

pytestmark = pytest.mark.fast


class ChromaLargeNearDoc(hb.Entity):
    identifier = "r9vec-chroma-large-near-doc"

    embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).required()


@pytest.mark.parametrize(("metric", "score"), [("cosine", 1.0), ("l2", float("-inf"))])
def test_chroma_large_near_scores_a_finite_query(tmp_path, metric, score):
    pytest.importorskip("chromadb")
    ws = hb.HeavenBase(
        "chroma-large-finite-near",
        detached=True,
        backends={
            "main": {"type": "inmem"},
            "vec": {"type": "chroma", "path": hb.utils.path_to_file_uri(tmp_path / "chroma")},
        },
    )
    top_k_setting = "heavenbase.query.near.max_top_k"
    metric_setting = "heavenbase.backend_defaults.chroma.metric"
    try:
        ws.context.config.set(top_k_setting, 4097)
        ws.context.config.set(metric_setting, metric)
        ws.register(ChromaLargeNearDoc)
        ws.upsert(ChromaLargeNearDoc, {"object_id": "a", "embedding": [1.0, 0.0]})
        q = ws.query(ChromaLargeNearDoc)
        assert q.near("embedding", [1.0, 0.0], metric=metric, top_k=1).execute().ids == ["a"]
        result = q.near("embedding", [1e155, 0.0], metric=metric, top_k=4097).execute()
        assert result.ids == ["a"]
        assert result.rows()[0]["score"] == pytest.approx(score)
    finally:
        ws.context.config.unset(top_k_setting)
        ws.context.config.unset(metric_setting)
        ws.release()
