"""Large Chroma near queries must pair each embedding with its own score."""

from importlib.util import find_spec

import pytest

import heavenbase as hb
from heavenbase.execution.contracts import QueryFragment, RowOp
from heavenbase.placement import StorageBinding
from heavenbase.schema import EntitySchema, FieldSchema

pytestmark = [pytest.mark.fast, pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")]


def test_chroma_large_near_scores_the_returned_embeddings(tmp_path, monkeypatch):
    uri = hb.utils.path_to_file_uri(tmp_path / "chroma-near-write")
    backend = hb.backends.ChromaBackend("vec", uri, ws_id="chroma-near-write")
    writer = hb.backends.ChromaBackend("vec", uri, ws_id="chroma-near-write")
    schema = EntitySchema(
        "near-doc",
        "NearDoc",
        fields={
            "object_id": FieldSchema("object_id", hb.Identifier(), pk=True, required=True),
            "embedding": FieldSchema("embedding", hb.Vector[2], required=True),
        },
    )
    bindings = [StorageBinding(schema.entity_id, "embedding", "vec", hb.VectorIndex.identifier, "test")]
    backend.ensure(schema, bindings)
    writer.ensure(schema, bindings)
    writer.upsert([RowOp(schema.entity_id, "a", {"embedding": [1.0, 0.0]}), RowOp(schema.entity_id, "b", {"embedding": [0.0, 1.0]})])

    collection = backend._collection(schema.entity_id)
    query = collection.query

    def query_then_write(**kwargs):
        result = query(**kwargs)
        writer.upsert([RowOp(schema.entity_id, "a", {"embedding": [-1.0, 0.0]})])
        return result

    monkeypatch.setattr(collection, "query", query_then_write)
    monkeypatch.setattr(backend, "_collection", lambda entity_id: collection)
    fragment = QueryFragment(
        "vec",
        "near",
        "embedding",
        {"vector": [1.0, 0.0], "top_k": 4097, "metric": "cosine"},
        returns=["score"],
        notes={"kind": "chroma_search", "mode": "native", "entity_id": schema.entity_id},
    )
    rows = backend.execute(fragment).rows()
    assert {row["object_id"] for row in rows} == {"a", "b"}
    for row in rows:
        assert row["score"] == pytest.approx(row["embedding"][0])
    assert [row["score"] for row in rows] == sorted((row["score"] for row in rows), reverse=True)
