"""Near routing and scores follow changes to provider-side index metrics."""

from importlib.util import find_spec
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import heavenbase as hb
from heavenbase.backends.vector.chroma import ChromaBackend
from heavenbase.backends.vector.handler.redisearch import redisearch_near_handler
from heavenbase.backends.vector.pinecone import PineconeBackend
from heavenbase.backends.vector.redisearch import RediSearchBackend
from heavenbase.execution import HandlerContext

pytestmark = pytest.mark.fast


class SearchDocument(hb.Entity):
    identifier = "search-document"

    embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)


@pytest.mark.slow
def test_pinecone_near_follows_recreated_index(monkeypatch):
    client = Mock()
    client.has_index.return_value = True
    client.describe_index.return_value = SimpleNamespace(host="old.example.com", dimension=2, metric="cosine")
    monkeypatch.setattr(PineconeBackend, "_connect", lambda backend: setattr(backend, "client", client))
    backend = PineconeBackend("vec", host="http://localhost:5080")
    ws = hb.HeavenBase("pinecone-live-metric", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    try:
        ws.register(SearchDocument)
        client.describe_index.return_value = SimpleNamespace(host="new.example.com", dimension=2, metric="euclidean")
        client.Index.return_value.query.return_value = {"matches": [{"id": "paper", "score": 4.0, "metadata": {}}]}
        client.Index.return_value.fetch.return_value = {"vectors": {"paper": {"id": "paper", "values": [3.0, 0.0], "metadata": {}}}}

        cosine = ws.query(SearchDocument).near(SearchDocument.embedding, [1, 0], top_k=1)
        with pytest.raises(ValueError, match="index ranks by 'euclidean'"):
            cosine.execute()
        l2 = ws.query(SearchDocument).near(SearchDocument.embedding, [1, 0], top_k=1, metric="l2")
        [row] = l2.execute().rows()
        assert (row["object_id"], row["score"]) == ("paper", -4.0)
        assert client.Index.call_args.kwargs["host"].endswith("new.example.com")
    finally:
        ws.release()


@pytest.mark.skipif(find_spec("redis") is None, reason="redis is not installed")
def test_redisearch_near_follows_index_rebuilt_by_another_instance(monkeypatch):
    docs = [{"object_id": "aligned", "embedding": [4.0, 0.0]}, {"object_id": "nearby", "embedding": [1.0, 1.0]}]
    raw_client = Mock()
    raw_client.scan_iter.return_value = iter(())
    ft = Mock()
    ft.info.return_value = {
        "attributes": [
            ["identifier", "object_id", "type", "TAG", "SEPARATOR", "\x1f"],
            ["identifier", "embedding", "type", "VECTOR", "dim", 2, "distance_metric", "COSINE"],
        ]
    }
    text_client = Mock()
    text_client.ft.return_value = ft
    monkeypatch.setattr(RediSearchBackend, "_connect", lambda backend: vars(backend).update(client=raw_client, text_client=text_client))
    first = RediSearchBackend("vec")
    other = RediSearchBackend("vec")
    schema = SearchDocument.schema()
    first.ensure(schema, [])

    def create_l2(fields, *, definition):
        ft.info.return_value["attributes"][1][-1] = "L2"

    ft.create_index.side_effect = create_l2
    monkeypatch.setattr(other, "_index_metric", lambda: "L2")
    other.ensure(schema, [])
    ft.dropindex.assert_called_once_with(delete_documents=False)
    monkeypatch.setattr(first, "scan", lambda entity_id: docs)
    monkeypatch.setattr(first, "_fetch_rows", lambda entity_id, ids: [row for row in docs if row["object_id"] in ids])
    ft.search.return_value = SimpleNamespace(docs=[SimpleNamespace(id="nearby", score=1.0)])

    def near(metric):
        context = HandlerContext(
            first.name,
            first.type,
            hb.VectorIndex.identifier,
            top_k=1,
            metric=metric,
            resolver=hb.DEFAULT_CONTEXT.modules(),
            config=first.config,
            backend_instance=first,
            entity_id=schema.entity_id,
        )
        fragment = redisearch_near_handler(schema.fields["embedding"], [1.0, 0.0], context)
        return first.execute(fragment.bind(schema.entity_id)).rows()

    [cosine] = near("cosine")
    assert (cosine["object_id"], cosine["score"]) == ("aligned", 1.0)
    ft.search.assert_not_called()
    [l2] = near("l2")
    assert (l2["object_id"], l2["score"]) == ("nearby", -1.0)
    ft.search.assert_called_once()


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_near_follows_recreated_collection(tmp_path):
    backend = ChromaBackend("vec", hb.utils.path_to_file_uri(tmp_path / "chroma"))
    ws = hb.HeavenBase("chroma-live-metric", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    try:
        name = backend._collection_name(SearchDocument.schema().entity_id)
        backend.client.create_collection(name=name, configuration={"hnsw": {"space": "ip"}})
        ws.register(SearchDocument)
        ws.upsert_many(SearchDocument, [{"object_id": "paper", "embedding": [3.0, 0.0]}])
        backend.client.delete_collection(name)
        collection = backend.client.create_collection(name=name, configuration={"hnsw": {"space": "l2"}})
        collection.upsert(ids=["paper"], embeddings=[[3.0, 0.0]])

        query = ws.query(SearchDocument).near(SearchDocument.embedding, [1, 0], top_k=1, metric="l2")
        assert query.explain()["steps"][-1]["handler_mode"] == "native"
        [row] = query.execute().rows()
        assert (row["object_id"], row["score"]) == ("paper", -3.0)
    finally:
        ws.release()
