"""Pinecone keeps every row it is given, or refuses it before writing anything."""

from __future__ import annotations

import dataclasses
import math
import sys

import pytest

import heavenbase as hb
from heavenbase.execution.contracts import RowOp

pytestmark = pytest.mark.fast


class _FakeIndex:
    """In-process Pinecone index: a record is a dense vector of the index dimension, with metadata.

    Every upsert and delete call is one entry of a write log with an increasing
    log sequence number (LSN). With ``report_lsn``, reads report the LSN of the
    view they read in ``x-pinecone-max-indexed-lsn``, as Pinecone serverless does.
    ``lag_fetches(n, view=lsn)`` makes the next ``n`` fetches read the view at
    ``lsn`` (by default the current one), so they miss records written after it,
    as a lagging Pinecone fetch can.
    """

    def __init__(self, dimension: int):
        self.dimension = dimension
        self.records: dict[str, dict] = {}
        # Ids the next fetch misses once, as an eventually consistent Pinecone read can; such a read reports no view.
        self.stale: set[str] = set()
        self.report_lsn = False
        self.lsn = 0
        self.fetches = 0
        self._snapshots: dict[int, dict[str, dict]] = {0: {}}
        self._lagging_fetches = 0
        self._lagging_view = 0

    def _write(self):
        self.lsn += 1
        self._snapshots[self.lsn] = {record_id: dict(record) for record_id, record in self.records.items()}

    def _headers(self, view):
        return {"response_info": {"raw_headers": {"x-pinecone-max-indexed-lsn": str(view)}}} if self.report_lsn else {}

    def lag_fetches(self, count: int, *, view: int | None = None) -> None:
        """Make the next ``count`` fetches read the index as it was at LSN ``view`` (default: now)."""
        self._lagging_fetches = count
        self._lagging_view = self.lsn if view is None else view

    def upsert(self, *, vectors):
        for item in vectors:
            values = item.get("values")
            if values is None or len(values) != self.dimension:
                raise ValueError(f"Pinecone record {item['id']!r} needs {self.dimension} dense values, got {values!r}")
        for item in vectors:
            self.records[item["id"]] = {"values": list(item["values"]), "metadata": dict(item.get("metadata") or {})}
        self._write()

    def delete(self, *, ids):
        for record_id in ids:
            self.records.pop(record_id, None)
        self._write()

    def fetch(self, ids):
        self.fetches += 1
        view, records = self.lsn, self.records
        if self._lagging_fetches:
            self._lagging_fetches -= 1
            view, records = self._lagging_view, self._snapshots[self._lagging_view]
        missed = self.stale & set(ids)
        self.stale -= missed
        found = {record_id: records[record_id] for record_id in ids if record_id in records and record_id not in missed}
        return {"vectors": found, **(self._headers(view) if not missed else {})}

    def describe_index_stats(self):
        return {"total_vector_count": len(self.records)}

    def query(self, *, vector, top_k, include_metadata=True, filter=None):
        def cosine(values):
            norm = math.sqrt(sum(item * item for item in values)) * math.sqrt(sum(item * item for item in vector))
            return sum(left * right for left, right in zip(values, vector)) / norm if norm else 0.0

        ranked = sorted(self.records.items(), key=lambda item: -cosine(item[1]["values"]))[:top_k]
        matches = [{"id": record_id, "score": cosine(record["values"]), "metadata": record["metadata"]} for record_id, record in ranked]
        return {"matches": matches, **self._headers(self.lsn)}


def _install_fake_pinecone(monkeypatch) -> dict[str, dict[str, _FakeIndex]]:
    """Install a fake ``pinecone`` module and return its indexes by name, per Pinecone host."""
    stores: dict[str, dict[str, _FakeIndex]] = {}

    class FakePinecone:
        def __init__(self, *, host, **kwargs):
            self.indexes = stores.setdefault(host, {})

        def list_indexes(self):
            return [{"name": name} for name in self.indexes]

        def has_index(self, name):
            return name in self.indexes

        def create_index(self, *, name, dimension, metric, spec):
            self.indexes[name] = _FakeIndex(dimension)

        def describe_index(self, name):
            return type("Description", (), {"host": f"http://localhost:5080/{name}", "dimension": self.indexes[name].dimension})()

        def Index(self, *, host, **kwargs):
            return self.indexes[host.rsplit("/", 1)[-1]]

        def delete_index(self, name):
            self.indexes.pop(name, None)

    module = type("FakePineconeModule", (), {"Pinecone": FakePinecone, "ServerlessSpec": lambda **kwargs: kwargs})
    monkeypatch.setitem(sys.modules, "pinecone", module)
    return stores


def _workspace(monkeypatch, name: str):
    stores = _install_fake_pinecone(monkeypatch)
    backends = {"main": hb.backends.InMemBackend("main"), "vec": hb.backends.PineconeBackend("vec", host="localhost:5080")}
    return hb.HeavenBase(name, detached=True, backends=backends), stores.setdefault("localhost:5080", {})


def _two_pinecone_workspace(monkeypatch, name: str):
    """Return a workspace with Pinecone backends ``pa`` and ``pb`` on separate hosts, and their indexes."""
    stores = _install_fake_pinecone(monkeypatch)
    backends = {
        "main": hb.backends.InMemBackend("main"),
        "pa": hb.backends.PineconeBackend("pa", host="localhost:5081"),
        "pb": hb.backends.PineconeBackend("pb", host="localhost:5082"),
    }
    ws = hb.HeavenBase(name, detached=True, backends=backends)
    return ws, stores.setdefault("localhost:5081", {}), stores.setdefault("localhost:5082", {})


def test_pinecone_refuses_an_entity_without_a_routed_vector(monkeypatch):
    """An entity none of whose fields routed to Pinecone is a Vector is refused at registration instead of losing every row."""
    ws, indexes = _workspace(monkeypatch, "pinecone-no-vector")

    class Plain(hb.Entity):
        identifier = "pinecone-no-vector-doc"

        title = hb.field(hb.ShortText).store(to="vec").optional()

    try:
        with pytest.raises(ValueError, match="without a routed Vector field"):
            ws.register(Plain)
        assert indexes == {}
    finally:
        ws.release()


def test_pinecone_refuses_two_routed_vectors(monkeypatch):
    """A Pinecone record holds one dense vector, so a second routed Vector field is refused rather than dropped."""
    ws, _ = _workspace(monkeypatch, "pinecone-two-vectors")

    class Two(hb.Entity):
        identifier = "pinecone-two-vectors-doc"

        first = hb.field(hb.Vector[2]).store(to="vec")
        second = hb.field(hb.Vector[2]).store(to="vec")

    try:
        with pytest.raises(ValueError, match="one dense vector per record"):
            ws.register(Two)
    finally:
        ws.release()


def test_pinecone_refuses_a_row_without_its_vector_before_writing(monkeypatch):
    """When Pinecone alone holds the row, a row without its vector fails the upsert and nothing of the call is written."""
    ws, indexes = _workspace(monkeypatch, "pinecone-sole-route")

    class Doc(hb.Entity):
        identifier = "pinecone-sole-route-doc"

        title = hb.field(hb.ShortText).store(to="vec").optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).optional()

    try:
        ws.register(Doc)
        [index] = indexes.values()
        with pytest.raises(ValueError, match=r"rows \['b'\] without their 'emb' vector"):
            ws.upsert_many(Doc, [{"object_id": "a", "title": "A", "emb": [1.0, 0.0]}, {"object_id": "b", "title": "B"}])
        assert index.records == {}
        ws.upsert(Doc, {"object_id": "a", "title": "A", "emb": [1.0, 0.0]})
        assert ws.get("a", entity=Doc)["title"] == "A"
    finally:
        ws.release()


def test_pinecone_row_without_a_vector_removes_its_record_when_another_backend_holds_the_row(monkeypatch):
    """A cleared vector deletes the Pinecone record: near search stops returning the row, which other backends still hold."""
    ws, indexes = _workspace(monkeypatch, "pinecone-shared-route")

    class Doc(hb.Entity):
        identifier = "pinecone-shared-route-doc"

        title = hb.field(hb.ShortText).optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).optional()

    try:
        ws.register(Doc)
        [index] = indexes.values()
        ws.upsert_many(Doc, [{"object_id": "a", "title": "A", "emb": [1.0, 0.0]}, {"object_id": "b", "title": "B", "emb": [0.0, 1.0]}])
        assert sorted(index.records) == ["a", "b"]
        ws.upsert(Doc, {"object_id": "a", "title": "A2"})
        assert sorted(index.records) == ["b"]
        assert ws.get("a", entity=Doc)["title"] == "A2"
        assert ws.get("a", entity=Doc).get("emb") is None
        assert ws.query(Doc).near(Doc.emb, [1.0, 0.0], top_k=5).execute().ids == ["b"]
        ws.upsert(Doc, {"object_id": "c", "title": "C"})
        assert sorted(index.records) == ["b"]
        assert ws.get("c", entity=Doc)["title"] == "C"
    finally:
        ws.release()


@pytest.mark.slow
def test_a_set_whose_read_misses_the_pinecone_record_writes_nothing(monkeypatch):
    """A missed source blocks even a patch that names only fields on another route."""
    ws, indexes = _workspace(monkeypatch, "pinecone-stale-set")

    class Doc(hb.Entity):
        identifier = "pinecone-stale-set-doc"

        title = hb.field(hb.ShortText).optional()
        label = hb.field(hb.ShortText).store(to="vec").optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).optional()

    try:
        ws.register(Doc)
        [index] = indexes.values()
        ws.upsert_many(Doc, [{"object_id": "a", "title": "A", "label": "L", "emb": [1.0, 0.0]}, {"object_id": "b", "title": "B", "emb": [0.0, 1.0]}])
        index.stale = {"a"}
        with pytest.raises(ValueError, match="retry it once the record can be read"):
            ws.set("a", {"title": "A2"}, entity=Doc)
        assert index.records["a"] == {"values": [1.0, 0.0], "metadata": {"label": "L"}}
        assert ws.get("a", entity=Doc) == {"object_id": "a", "title": "A", "label": "L", "emb": [1.0, 0.0]}
        assert ws.set("a", {"title": "A2"}, entity=Doc) == "a"
        assert ws.get("a", entity=Doc) == {"object_id": "a", "title": "A2", "label": "L", "emb": [1.0, 0.0]}
        assert ws.query(Doc).near(Doc.emb, [1.0, 0.0], top_k=1).execute().ids == ["a"]
        index.stale = {"a"}
        with pytest.raises(ValueError, match="retry it once the record can be read"):
            ws.set("a", {"label": "M"}, entity=Doc)
        assert index.records["a"] == {"values": [1.0, 0.0], "metadata": {"label": "L"}}
        ws.set("a", {"emb": None, "label": None}, entity=Doc)
        assert sorted(index.records) == ["b"]
        assert ws.get("a", entity=Doc) == {"object_id": "a", "title": "A2"}
        with pytest.raises(ValueError, match="use a full upsert if the record is absent"):
            ws.set("a", {"title": "A3"}, entity=Doc)
        ws.upsert(Doc, {"object_id": "a", "title": "A3"})
        assert sorted(index.records) == ["b"]
    finally:
        ws.release()


def test_pinecone_metadata_clear_after_a_stale_read_is_retryable(monkeypatch):
    class Doc(hb.Entity):
        identifier = "pinecone-stale-clear-doc"

        title = hb.field(hb.ShortText)
        label = hb.field(hb.ShortText).store(to="vec").optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).optional()

    ws, indexes = _workspace(monkeypatch, "pinecone-stale-clear")
    try:
        ws.register(Doc)
        [index] = indexes.values()
        ws.upsert(Doc, {"object_id": "a", "title": "A", "label": "L", "emb": [1.0, 0.0]})
        index.stale = {"a"}
        with pytest.raises(ValueError, match="retry it once the record can be read"):
            ws.set("a", {"label": None}, entity=Doc)
        assert ws.get("a", entity=Doc)["label"] == "L"
        assert ws.set("a", {"label": None}, entity=Doc) == "a"
        assert ws.get("a", entity=Doc) == {"object_id": "a", "title": "A", "emb": [1.0, 0.0]}
    finally:
        ws.release()


def test_a_graph_edge_rewrite_whose_read_misses_the_pinecone_record_writes_nothing(monkeypatch):
    """Detach requires a readable source before rewriting the edge or deleting the target."""
    ws, indexes = _workspace(monkeypatch, "pinecone-stale-edge")

    class Doc(hb.Entity):
        identifier = "pinecone-stale-edge-doc"

        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["pinecone-stale-edge-doc"]]).store(strategy=hb.GraphEdge(on_delete="detach")).optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).optional()

    try:
        ws.register(Doc)
        [index] = indexes.values()
        ws.upsert_many(
            Doc,
            [
                {"object_id": "d1", "title": "one", "cites": [{"cited": "d2"}], "emb": [1.0, 0.0]},
                {"object_id": "d2", "title": "two", "cites": [], "emb": [0.0, 1.0]},
            ],
        )
        index.stale = {"d1"}
        with pytest.raises(ValueError, match="retry it once the record can be read"):
            ws.delete((Doc, "d2"))
        assert sorted(index.records) == ["d1", "d2"]
        assert ws.exists("d2", entity=Doc)
        assert ws.get("d1", entity=Doc)["cites"] == [{"cited": "d2"}]
        assert ws.delete((Doc, "d2")) is True
        assert sorted(index.records) == ["d1"]
        assert ws.get("d1", entity=Doc) == {"object_id": "d1", "title": "one", "cites": [], "emb": [1.0, 0.0]}
    finally:
        ws.release()


def test_an_unhashable_pinecone_backend_subclass_constructs(monkeypatch):
    """A dataclass subclass sets ``__hash__ = None``; as in 0.1.2.2 it constructs, and still counts as a Pinecone backend."""
    _install_fake_pinecone(monkeypatch)

    @dataclasses.dataclass(init=False)
    class UnhashablePinecone(hb.backends.PineconeBackend):
        pass

    backends = {
        "main": hb.backends.InMemBackend("main"),
        "pa": hb.backends.PineconeBackend("pa", host="localhost:5081"),
        "pb": UnhashablePinecone("pb", host="localhost:5082"),
    }
    with pytest.raises(TypeError):
        hash(backends["pb"])
    ws = hb.HeavenBase("pinecone-unhashable", detached=True, backends=backends)

    class Doc(hb.Entity):
        identifier = "pinecone-unhashable-doc"

        e1 = hb.field(hb.Vector[2]).store(to="pa", strategy=hb.VectorIndex.identifier).optional()
        e2 = hb.field(hb.Vector[2]).store(to="pb", strategy=hb.VectorIndex.identifier).optional()

    try:
        ws.register(Doc)
        with pytest.raises(ValueError, match="which no other backend keeps"):
            ws.upsert(Doc, {"object_id": "y", "e2": [0.0, 1.0]})
    finally:
        ws.release()


def test_pinecone_stores_the_vector_routed_to_it_not_the_first_one_declared(monkeypatch):
    """The record's values are the Vector field routed to Pinecone, even when another Vector field is declared first."""
    ws, indexes = _workspace(monkeypatch, "pinecone-second-vector")

    class Doc(hb.Entity):
        identifier = "pinecone-second-vector-doc"

        local = hb.field(hb.Vector[3]).store(to="main")
        remote = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    try:
        ws.register(Doc)
        [index] = indexes.values()
        assert index.dimension == 2
        ws.upsert(Doc, {"object_id": "a", "local": [1.0, 0.0, 0.0], "remote": [0.0, 1.0]})
        assert index.records["a"]["values"] == [0.0, 1.0]
        assert ws.get("a", entity=Doc)["remote"] == [0.0, 1.0]
        assert ws.query(Doc).near(Doc.remote, [0.0, 1.0], top_k=1).execute().ids == ["a"]
    finally:
        ws.release()


def test_another_pinecone_backend_does_not_keep_a_row_without_its_vector(monkeypatch):
    """Two Pinecone backends do not count on each other for a row without its vectors, so the row fails instead of vanishing."""
    ws, a_indexes, b_indexes = _two_pinecone_workspace(monkeypatch, "pinecone-peers")

    class Doc(hb.Entity):
        identifier = "pinecone-peers-doc"

        e1 = hb.field(hb.Vector[2]).store(to="pa", strategy=hb.VectorIndex.identifier).optional()
        e2 = hb.field(hb.Vector[2]).store(to="pb", strategy=hb.VectorIndex.identifier).optional()

    try:
        ws.register(Doc)
        [a_index], [b_index] = a_indexes.values(), b_indexes.values()
        ws.upsert(Doc, {"object_id": "x", "e1": [1.0, 0.0], "e2": [0.0, 1.0]})
        with pytest.raises(ValueError, match="which no other backend keeps"):
            ws.upsert(Doc, {"object_id": "y"})
        with pytest.raises(ValueError, match="which no other backend keeps"):
            ws.upsert(Doc, {"object_id": "x"})
        assert sorted(a_index.records) == ["x"] and sorted(b_index.records) == ["x"]
        assert ws.get("x", entity=Doc) == {"object_id": "x", "e1": [1.0, 0.0], "e2": [0.0, 1.0]}
        assert ws.get("y", entity=Doc) is None
    finally:
        ws.release()


def test_a_pinecone_backend_with_a_required_vector_keeps_every_row(monkeypatch):
    """A Pinecone backend whose routed vector is required holds every row, so another Pinecone backend may leave its vector out."""
    ws, a_indexes, b_indexes = _two_pinecone_workspace(monkeypatch, "pinecone-required-peer")

    class Doc(hb.Entity):
        identifier = "pinecone-required-peer-doc"

        e1 = hb.field(hb.Vector[2]).store(to="pa", strategy=hb.VectorIndex.identifier).optional()
        e2 = hb.field(hb.Vector[2]).store(to="pb", strategy=hb.VectorIndex.identifier).required()

    try:
        ws.register(Doc)
        [a_index], [b_index] = a_indexes.values(), b_indexes.values()
        ws.upsert(Doc, {"object_id": "z", "e1": [1.0, 0.0], "e2": [0.0, 1.0]})
        ws.upsert(Doc, {"object_id": "z", "e2": [0.0, 1.0]})
        assert a_index.records == {} and sorted(b_index.records) == ["z"]
        assert ws.get("z", entity=Doc) == {"object_id": "z", "e2": [0.0, 1.0]}
    finally:
        ws.release()


def test_pinecone_refuses_an_existing_index_of_another_dimension(monkeypatch):
    """An index whose dimension is not the routed vector's (as 0.1.2.2 could create) is refused by name, and other entities still work."""
    ws, indexes = _workspace(monkeypatch, "pinecone-index-dimension")

    class Doc(hb.Entity):
        identifier = "pinecone-index-dimension-doc"

        local = hb.field(hb.Vector[3]).store(to="main")
        remote = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    class Other(hb.Entity):
        identifier = "pinecone-index-dimension-other"

        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    try:
        name = ws.backends.get("vec")._index_name(Doc.identifier)
        indexes[name] = _FakeIndex(3)
        with pytest.raises(ValueError, match=rf"Pinecone index {name!r} for {Doc.identifier!r} has dimension 3.*Delete the index"):
            ws.register(Doc)
        assert indexes[name].dimension == 3
        ws.register(Other)
        ws.upsert(Other, {"object_id": "o", "emb": [0.0, 1.0]})
        assert ws.get("o", entity=Other)["emb"] == [0.0, 1.0]
        del indexes[name]
        ws.register(Doc)
        ws.upsert(Doc, {"object_id": "d", "local": [1.0, 0.0, 0.0], "remote": [0.0, 1.0]})
        assert indexes[name].records["d"]["values"] == [0.0, 1.0]
    finally:
        ws.release()


def test_pinecone_backend_refuses_upserts_it_cannot_store(monkeypatch):
    """A direct upsert into a missing index, or of an entity never ensured, raises instead of returning as if written."""
    indexes = _install_fake_pinecone(monkeypatch).setdefault("localhost:5080", {})
    backend = hb.backends.PineconeBackend("vec", host="localhost:5080")

    class Doc(hb.Entity):
        identifier = "pinecone-direct-doc"

        emb = hb.field(hb.Vector[2])

    schema = Doc.schema()
    backend.ensure(schema, [])
    [name] = indexes
    row = RowOp(Doc.identifier, "a", {"emb": [1.0, 0.0]})
    with pytest.raises(ValueError, match="no Vector field registered"):
        backend.upsert([RowOp("pinecone-never-ensured", "a", {"emb": [1.0, 0.0]})])
    del indexes[name]
    with pytest.raises(RuntimeError, match="does not exist; refusing to discard 1 upsert"):
        backend.upsert([row])
    backend.upsert([RowOp(Doc.identifier, "a", op="delete")])
    assert backend.client is not None


def test_replicated_vector_cannot_hide_a_stale_metadata_read(monkeypatch):
    ws, indexes = _workspace(monkeypatch, "pinecone-stale-replicated-vector")

    class Doc(hb.Entity):
        identifier = "pinecone-stale-replicated-vector-doc"

        title = hb.field(hb.ShortText)
        label = hb.field(hb.ShortText).store(to="vec").optional()
        note = hb.field(hb.ShortText).store(to="vec").optional()
        emb = hb.field(hb.Vector[2]).store(to="main").store(to="vec", strategy=hb.VectorIndex.identifier).optional()

    try:
        ws.register(Doc)
        [index] = indexes.values()
        original = {"object_id": "a", "title": "A", "label": "L", "note": "N", "emb": [1.0, 0.0]}
        ws.upsert(Doc, original)
        index.stale = {"a"}
        with pytest.raises(ValueError, match="retry it once the record can be read"):
            ws.set("a", {"label": None}, entity=Doc)
        assert ws.get("a", entity=Doc) == original

        index.stale = {"a"}
        with pytest.raises(ValueError, match="retry it once the record can be read"):
            ws.set("a", {"title": "Updated"}, entity=Doc)
        assert ws.get("a", entity=Doc) == original
        assert ws.set("a", {"title": "Updated"}, entity=Doc) == "a"
        assert index.records["a"] == {"values": [1.0, 0.0], "metadata": {"label": "L", "note": "N"}}

        assert ws.set("a", {"label": None}, entity=Doc) == "a"
        assert ws.get("a", entity=Doc) == {"object_id": "a", "title": "Updated", "note": "N", "emb": [1.0, 0.0]}
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("batch", [False, True], ids=["set", "set-many"])
def test_rejected_clear_preserves_replicas_for_retry(monkeypatch, batch):
    stores = _install_fake_pinecone(monkeypatch)
    backends = {
        "main": hb.backends.InMemBackend("main"),
        "vec": hb.backends.PineconeBackend("vec", host="localhost:5080"),
        "zrep": hb.backends.InMemBackend("zrep"),
    }
    ws = hb.HeavenBase("pinecone-review-retry", detached=True, backends=backends)

    class Doc(hb.Entity):
        identifier = "pinecone-review-retry-doc"

        title = hb.field(hb.ShortText).store(to="main").store(to="zrep")
        label = hb.field(hb.ShortText).store(to="vec").optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).optional()

    try:
        ws.register(Doc)
        [index] = stores["localhost:5080"].values()
        original = {"object_id": "a", "title": "Original", "label": "L", "emb": [1.0, 0.0]}
        second = {"object_id": "b", "title": "B", "emb": [0.0, 1.0]}
        ws.upsert_many(Doc, [original, second])
        index.stale = {"a"}
        patch = {"title": "Updated", "label": None}
        patches = [{"object_id": "b", "title": "B2"}, {"object_id": "a", **patch}]
        with pytest.raises(ValueError, match="retry it once the record can be read"):
            if batch:
                ws.set_many(patches, entity=Doc)
            else:
                ws.set("a", patch, entity=Doc)
        assert ws.get("b", entity=Doc) == second
        assert ws.get("a", entity=Doc) == original
        if batch:
            assert ws.set_many(patches, entity=Doc) == ["b", "a"]
            assert ws.get("b", entity=Doc) == {**second, "title": "B2"}
        else:
            assert ws.set("a", patch, entity=Doc) == "a"
        assert ws.get("a", entity=Doc) == {"object_id": "a", "title": "Updated", "emb": [1.0, 0.0]}
    finally:
        ws.release()


def test_a_full_upsert_can_write_a_vectorless_row(monkeypatch):
    ws, indexes = _workspace(monkeypatch, "pinecone-vectorless-replica")

    class Doc(hb.Entity):
        identifier = "pinecone-vectorless-replica-doc"

        title = hb.field(hb.ShortText)
        label = hb.field(hb.ShortText).store(to="main").store(to="vec").optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).optional()

    try:
        ws.register(Doc)
        [index] = indexes.values()
        ws.upsert(Doc, {"object_id": "a", "title": "Original", "label": None})
        with pytest.raises(ValueError, match="use a full upsert if the record is absent"):
            ws.set("a", {"title": "Updated"}, entity=Doc)
        assert ws.upsert(Doc, {"object_id": "a", "title": "Updated", "label": None}) == "a"
        assert index.records == {}
        assert ws.get("a", entity=Doc)["title"] == "Updated"
        assert ws.upsert(Doc, {"object_id": "a", "title": "Updated", "label": None, "emb": [1.0, 0.0]}) == "a"
        assert index.records["a"] == {"values": [1.0, 0.0], "metadata": {}}
        index.stale = {"a"}
        with pytest.raises(ValueError, match="retry it once the record can be read"):
            ws.set("a", {"label": None, "emb": None}, entity=Doc)
        assert ws.set("a", {"label": None, "emb": None}, entity=Doc) == "a"
        assert index.records == {}
    finally:
        ws.release()
