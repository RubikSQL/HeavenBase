"""Request budgets for live metadata used by native vector queries."""

from importlib.util import find_spec
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import heavenbase as hb
from heavenbase.backends.vector.chroma import ChromaBackend
from heavenbase.backends.vector.pinecone import PineconeBackend
from heavenbase.backends.vector.redisearch import RediSearchBackend

pytestmark = pytest.mark.fast


class SearchDocument(hb.Entity):
    identifier = "search-document"

    embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)
    title = hb.field(hb.ShortText).store(to="vec")
    notes = hb.field(hb.ShortText).optional().store(to="vec")


def _near(ws, metric, filtered):
    query = ws.query(SearchDocument).near(SearchDocument.embedding, [1.0, 0.0], top_k=2, metric=metric)
    return query.where(SearchDocument.title == "paper") if filtered else query


@pytest.mark.slow
@pytest.mark.parametrize("filtered", [False, True])
@pytest.mark.parametrize("reports_metric", [False, True])
def test_pinecone_near_and_explain_each_describe_index_once(monkeypatch, filtered, reports_metric):
    client = Mock()
    client.has_index.return_value = True
    metric = "ip" if reports_metric else "cosine"
    client.describe_index.return_value = SimpleNamespace(host="index.example.com", dimension=2)
    if reports_metric:
        client.describe_index.return_value.metric = "dotproduct"
    client.Index.return_value.query.return_value = {"matches": [{"id": "paper", "score": 1.0, "metadata": {"title": "paper"}}]}
    client.Index.return_value.fetch.return_value = {"vectors": {"paper": {"id": "paper", "values": [2.0, 0.0], "metadata": {"title": "paper"}}}}
    monkeypatch.setattr(PineconeBackend, "_connect", lambda backend: setattr(backend, "client", client))
    backend = PineconeBackend("vec", host="http://localhost:5080")
    ws = hb.HeavenBase("pinecone-metadata-budget", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    try:
        ws.register(SearchDocument)
        client.reset_mock()
        query = _near(ws, metric, filtered)
        assert query.explain()["steps"][-1]["handler_mode"] == "native"
        assert (client.has_index.call_count, client.describe_index.call_count) == (1, 1)
        client.Index.assert_not_called()
        assert query.execute().ids == ["paper"]
        assert (client.has_index.call_count, client.describe_index.call_count) == (2, 2)
        client.Index.return_value.query.assert_called_once()
        client.Index.return_value.fetch.assert_called_once_with(ids=["paper"])
    finally:
        ws.release()


@pytest.mark.skipif(find_spec("redis") is None, reason="redis is not installed")
@pytest.mark.parametrize("filtered", [False, True])
def test_redisearch_near_and_explain_each_read_index_info_once(monkeypatch, filtered):
    raw_client = Mock()
    raw_client.pipeline.return_value.execute.return_value = []
    raw_client.scan_iter.return_value = iter(())
    ft = Mock()
    ft.info.return_value = {
        "attributes": [
            ["identifier", "object_id", "type", "TAG", "SEPARATOR", "\x1f"],
            ["identifier", "embedding", "type", "VECTOR", "dim", 2, "distance_metric", "COSINE"],
            ["identifier", "title", "type", "TAG", "SEPARATOR", "\x1f"],
            ["identifier", "notes", "type", "TAG", "SEPARATOR", "\x1f"],
        ]
    }
    ft.search.return_value = SimpleNamespace(docs=[])
    text_client = Mock()
    text_client.ft.return_value = ft
    monkeypatch.setattr(RediSearchBackend, "_connect", lambda backend: vars(backend).update(client=raw_client, text_client=text_client, client_error=""))
    backend = RediSearchBackend("vec")
    ws = hb.HeavenBase("redisearch-metadata-budget", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    try:
        ws.register(SearchDocument)
        ft.reset_mock()
        backend._ensured.clear()
        query = _near(ws, "cosine", filtered)
        assert query.explain()["steps"][-1]["handler_mode"] == "native"
        ft.info.assert_called_once()
        ft.search.assert_not_called()
        backend._ensured.clear()
        assert query.execute().ids == []
        assert ft.info.call_count == 2
        ft.search.assert_called_once()
    finally:
        ws.release()


@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
@pytest.mark.parametrize("filtered", [False, True])
@pytest.mark.parametrize("metric", ["ip", "l2"])
def test_chroma_near_and_explain_each_open_collection_once(tmp_path, monkeypatch, filtered, metric):
    backend = ChromaBackend("vec", hb.utils.path_to_file_uri(tmp_path / "chroma"))
    ws = hb.HeavenBase("near-metadata", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    try:
        entity_id = SearchDocument.schema().entity_id
        backend.client.create_collection(name=backend._collection_name(entity_id), configuration={"hnsw": {"space": "ip"}})
        ws.register(SearchDocument)
        ws.upsert_many(SearchDocument, [{"object_id": "paper", "embedding": [2.0, 0.0], "title": "paper"}])
        open_collection = Mock(wraps=backend.client.get_or_create_collection)
        monkeypatch.setattr(backend.client, "get_or_create_collection", open_collection)

        query = _near(ws, metric, filtered)
        expected_mode = "native" if metric == "ip" else "py_scan"
        assert query.explain()["steps"][-1]["handler_mode"] == expected_mode
        open_collection.assert_called_once()
        assert query.execute().ids == ["paper"]
        assert open_collection.call_count == 2
    finally:
        ws.release()
