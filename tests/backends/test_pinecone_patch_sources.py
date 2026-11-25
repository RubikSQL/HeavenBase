"""Reject Pinecone write-backs when the source record was missed."""

import pytest

import heavenbase as hb
from heavenbase.execution.contracts import RowOp
from test_pinecone_records import _workspace

pytestmark = pytest.mark.fast


def _axis(revision):
    return revision % 2


def _vector(axis):
    return [0.0, 1e-7] if axis else [1e-7, 0.0]


def test_missed_pinecone_source_cannot_leave_a_transitively_derived_vector_stale(monkeypatch):
    ws, indexes = _workspace(monkeypatch, "pinecone-transitive")

    class Doc(hb.Entity):
        identifier = "pinecone-transitive-doc"

        revision = hb.field(hb.Integer)
        axis = hb.field(hb.Integer).compute(_axis, inputs=["revision"])
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).store(to="main").compute(_vector, inputs=["axis"])

    try:
        ws.register(Doc)
        [index] = indexes.values()
        ws.upsert_many(Doc, [{"object_id": "a", "revision": 0}, {"object_id": "b", "revision": 0, "emb": [1.0, 1.0]}])
        index.stale = {"a"}
        with pytest.raises(ValueError, match="retry it once the record can be read"):
            ws.set("a", {"revision": 1}, entity=Doc)
        assert ws.get("a", entity=Doc)["revision"] == 0
        assert index.records["a"]["values"] == [1e-7, 0.0]
        assert ws.set("a", {"revision": 1}, entity=Doc) == "a"
        assert index.records["a"]["values"] == [0.0, 1e-7]
        assert ws.query(Doc).near(Doc.emb, [0, 1], top_k=1).execute().ids == ["a"]
    finally:
        ws.release()


def test_missed_pinecone_only_source_cannot_erase_metadata(monkeypatch):
    ws, indexes = _workspace(monkeypatch, "pinecone-only-source")

    class Doc(hb.Entity):
        identifier = "pinecone-only-source-doc"

        note = hb.field(hb.ShortText).store(to="vec").optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    try:
        ws.register(Doc)
        [index] = indexes.values()
        ws.upsert(Doc, {"object_id": "a", "note": "keep", "emb": [1.0, 0.0]})
        index.stale = {"a"}
        with pytest.raises(ValueError, match="retry it once the record can be read"):
            ws.set("a", {"emb": [0.0, 1.0]}, entity=Doc)
        assert ws.get("a", entity=Doc) == {"object_id": "a", "note": "keep", "emb": [1.0, 0.0]}
        assert ws.set("a", {"emb": [0.0, 1.0]}, entity=Doc) == "a"
        assert ws.get("a", entity=Doc) == {"object_id": "a", "note": "keep", "emb": [0.0, 1.0]}
    finally:
        ws.release()


@pytest.mark.slow
def test_graph_repair_rejects_a_missed_pinecone_source_before_writing(monkeypatch):
    ws, indexes = _workspace(monkeypatch, "pinecone-repair-source")

    class Target(hb.Entity):
        identifier = "pinecone-repair-target"

        title = hb.field(hb.ShortText)

    class Source(hb.Entity):
        identifier = "pinecone-repair-source-doc"

        cites = hb.field(hb.HyperG["cited" : hb.Identifier["pinecone-repair-target"]]).store(strategy=hb.GraphEdge)
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).optional()

    try:
        ws.register(Target)
        ws.register(Source)
        [index] = indexes.values()
        ws.upsert(Target, {"object_id": "target", "title": "Target"})
        source = {"object_id": "source", "cites": [{"cited": "target"}], "emb": [1.0, 0.0]}
        ws.upsert(Source, source)
        ws.backends.get("main").upsert([RowOp(Target.identifier, "target", op="delete")])
        index.stale = {"source"}
        with pytest.raises(ValueError, match="retry it once the record can be read"):
            ws.scrub_dangling(confirm=True)
        assert ws.get("source", entity=Source) == source
        assert ws.scrub_dangling(confirm=True)["removed"] == 1
        assert ws.get("source", entity=Source) == {**source, "cites": []}
    finally:
        ws.release()
