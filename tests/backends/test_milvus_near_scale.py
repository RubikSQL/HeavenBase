"""Milvus Lite must preserve cosine ranking across positive query scales."""

from importlib.util import find_spec

import pytest

import heavenbase as hb

pytestmark = [
    pytest.mark.fast,
    pytest.mark.skipif(find_spec("pymilvus") is None or find_spec("milvus_lite") is None, reason="pymilvus and milvus-lite are required"),
]


class MilvusNearDoc(hb.Entity):
    identifier = "milvus-near-scale-doc"

    embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).required()


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("stored_scale", [1.0, 1e20, 1e-24])
def test_milvus_cosine_near_is_invariant_to_vector_scale(tmp_path, exact, stored_scale):
    uri = hb.utils.path_to_file_uri(tmp_path / "milvus-near-scale.db")
    backends = {"main": hb.backends.InMemBackend("main"), "vec": hb.backends.MilvusLiteBackend("vec", uri)}
    ws = hb.HeavenBase("milvus-near-scale", detached=True, backends=backends)
    try:
        ws.register(MilvusNearDoc)
        ws.upsert_many(
            MilvusNearDoc,
            [{"object_id": "a", "embedding": [-stored_scale, 0.0]}, {"object_id": "z", "embedding": [stored_scale, 0.0]}],
        )
        for scale in (1.0, 1e20, 1e-24, 1e308, 1e-308):
            near = ws.query(MilvusNearDoc).near("embedding", [scale, 0.0], metric="cosine", top_k=1, exact=exact)
            assert near.explain()["steps"][-1]["handler_mode"] == "py_scan"
            row = near.select("object_id", "embedding", "score").execute().rows()[0]
            assert row["object_id"] == "z"
            assert row["embedding"] == pytest.approx([stored_scale, 0.0], rel=1e-6, abs=0.0)
            assert row["score"] == pytest.approx(1.0)
    finally:
        ws.release()
