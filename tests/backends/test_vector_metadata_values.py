"""Field values that Chroma and Pinecone keep in scalar-only metadata survive writes, updates and reads."""

from __future__ import annotations

import sys
from importlib.util import find_spec

import pytest

import heavenbase as hb

pytestmark = pytest.mark.fast

needs_chromadb = pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
CHROMA_CLIENTS = ["persistent", "ephemeral"]

FULL_ROW = {
    "object_id": "a",
    "title": "t",
    "note": "old",
    "js": {"k": [1, 2], "nested": {"d": "x"}},
    "js_scalar": 5,
    "art": b"\x00\x01payload\xff",
    "tags": ["x", "y"],
    "nums": [1, 2],
    "objs": [{"a": 1}, {"b": [2]}],
    "mixed": [1, "a"],
    "flags": [True, None],
    "empty": [],
    "flag": True,
    "emb": [1.0, 0.0],
}


def _chroma_workspace(tmp_path, client: str, name: str):
    """Return a workspace whose ``vec`` backend is Chroma with the given client, and its entity."""
    path = None if client == "ephemeral" else hb.utils.path_to_file_uri(tmp_path / "chroma")
    backends = {"main": hb.backends.InMemBackend("main"), "vec": hb.backends.ChromaBackend("vec", path)}
    ws = hb.HeavenBase(f"{name}-{client}", detached=True, backends=backends)
    return ws, _register(ws, f"{name}-{client}-doc")


def _register(ws, entity_identifier: str):
    """Register an entity with a field of every kind Chroma and Pinecone metadata must keep."""

    class Doc(hb.Entity):
        identifier = entity_identifier

        title = hb.field(hb.ShortText).store(to="vec").optional()
        note = hb.field(hb.ShortText).store(to="vec").optional()
        js = hb.field(hb.Json).store(to="vec").optional()
        js_scalar = hb.field(hb.Json).store(to="vec").optional()
        art = hb.field(hb.Artifact).store(to="vec").optional()
        tags = hb.field(hb.Array[hb.ShortText]).store(to="vec").optional()
        nums = hb.field(hb.Array[hb.Integer]).store(to="vec").optional()
        objs = hb.field(hb.Array[hb.Json]).store(to="vec").optional()
        mixed = hb.field(hb.Array).store(to="vec").optional()
        flags = hb.field(hb.Array[hb.Boolean]).store(to="vec").optional()
        empty = hb.field(hb.Array[hb.ShortText]).store(to="vec").optional()
        flag = hb.field(hb.Boolean).store(to="vec").optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    ws.register(Doc)
    return Doc


def _stored(ws, Doc, object_id: str = "a") -> dict:
    """Return the decoded stored row without the identity."""
    row = dict(ws.get(object_id, entity=Doc))
    row.pop("object_id", None)
    return row


@needs_chromadb
@pytest.mark.parametrize("client", CHROMA_CLIENTS)
def test_chroma_keeps_every_field_value(tmp_path, client):
    """Json, Artifact and Arrays Chroma metadata cannot hold as they are round-trip, and filters see them."""
    ws, Doc = _chroma_workspace(tmp_path, client, "chroma-values")
    try:
        ws.upsert(Doc, FULL_ROW)
        expected = {key: value for key, value in FULL_ROW.items() if key != "object_id"}
        assert _stored(ws, Doc) == expected
        [row] = ws.query(Doc).execute().rows()
        assert {key: row[key] for key in expected} == expected
        assert ws.query(Doc).where(Doc.js == {"k": [1, 2], "nested": {"d": "x"}}).execute().ids == ["a"]
        assert ws.query(Doc).where(Doc.mixed.array_contains(1)).execute().ids == ["a"]
        assert ws.query(Doc).where(Doc.flags.array_contains(True)).execute().ids == ["a"]
        assert ws.query(Doc).where(Doc.tags.array_contains("y")).execute().ids == ["a"]
        assert ws.query(Doc).where(Doc.nums.array_contains(2)).near(Doc.emb, [1.0, 0.0], top_k=1).execute().ids == ["a"]
        assert ws.query(Doc).where(Doc.empty.array_contains("x")).execute().ids == []
    finally:
        ws.release()


class _FakePineconeIndex:
    """In-process Pinecone index: an upsert replaces the record, metadata is checked as Pinecone does, and numbers are 64-bit floats."""

    def __init__(self):
        self.records: dict[str, dict] = {}

    @staticmethod
    def _check(metadata: dict) -> None:
        for key, value in metadata.items():
            ok = isinstance(value, (str, int, float, bool)) or (isinstance(value, list) and all(isinstance(item, str) for item in value))
            if not ok:
                raise ValueError(f"Pinecone metadata value for {key!r} must be a string, number, boolean or list of strings, got {value!r}")

    def upsert(self, vectors):
        for vector in vectors:
            metadata = dict(vector.get("metadata") or {})
            self._check(metadata)
            metadata = {key: float(value) if isinstance(value, int) and not isinstance(value, bool) else value for key, value in metadata.items()}
            self.records[vector["id"]] = {"id": vector["id"], "values": list(vector["values"]), "metadata": metadata}

    def fetch(self, ids):
        return {"vectors": {row_id: self.records[row_id] for row_id in ids if row_id in self.records}}

    def delete(self, ids):
        for row_id in ids:
            self.records.pop(row_id, None)

    def describe_index_stats(self):
        return {"total_vector_count": len(self.records)}

    def query(self, vector, top_k, include_metadata=True, **kwargs):
        def score(record):
            return sum(left * right for left, right in zip(record["values"], vector))

        best = sorted(self.records.values(), key=score, reverse=True)[:top_k]
        return {"matches": [{"id": record["id"], "score": score(record), "metadata": record["metadata"]} for record in best]}


def _install_fake_pinecone(monkeypatch) -> dict[str, _FakePineconeIndex]:
    """Install a fake ``pinecone`` module and return its indexes by name."""
    indexes: dict[str, _FakePineconeIndex] = {}

    class FakePinecone:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.current = None

        def list_indexes(self):
            return [{"name": name} for name in indexes]

        def has_index(self, name):
            return name in indexes

        def create_index(self, name, **kwargs):
            indexes[name] = _FakePineconeIndex()

        def describe_index(self, name):
            self.current = name
            return type("Description", (), {"host": "localhost:5080"})()

        def Index(self, **kwargs):
            return indexes[self.current]

        def delete_index(self, name):
            indexes.pop(name, None)

    module = type("FakePineconeModule", (), {"Pinecone": FakePinecone, "ServerlessSpec": lambda **kwargs: kwargs})
    monkeypatch.setitem(sys.modules, "pinecone", module)
    return indexes


def _pinecone_workspace(monkeypatch, name: str):
    indexes = _install_fake_pinecone(monkeypatch)
    backends = {"main": hb.backends.InMemBackend("main"), "vec": hb.backends.PineconeBackend("vec", host="localhost:5080")}
    ws = hb.HeavenBase(name, detached=True, backends=backends)
    return ws, _register(ws, f"{name}-doc"), indexes


def test_pinecone_keeps_every_field_value(monkeypatch):
    """Json, Artifact and non-text Arrays round-trip on Pinecone instead of being left out of the metadata."""
    ws, Doc, _ = _pinecone_workspace(monkeypatch, "pinecone-values")
    try:
        ws.upsert(Doc, FULL_ROW)
        expected = {key: value for key, value in FULL_ROW.items() if key != "object_id"}
        assert _stored(ws, Doc) == expected
    finally:
        ws.release()


@needs_chromadb
@pytest.mark.parametrize("client", CHROMA_CLIENTS)
def test_chroma_upsert_replaces_the_stored_fields(tmp_path, client):
    """A field set to None, an empty Array or an omitted field is cleared on Chroma, and filters agree."""
    ws, Doc = _chroma_workspace(tmp_path, client, "chroma-clear")
    try:
        ws.upsert(Doc, FULL_ROW)
        ws.upsert(Doc, {"object_id": "a", "title": "t", "note": None, "tags": [], "js": None, "emb": [1.0, 0.0]})
        stored = _stored(ws, Doc)
        assert stored.get("tags") == []
        assert all(stored.get(name) is None for name in ("note", "js", "js_scalar", "art", "nums", "objs", "mixed", "flags", "flag"))
        assert stored["title"] == "t" and stored["emb"] == [1.0, 0.0]
        assert ws.query(Doc).where(Doc.note == "old").execute().ids == []
        assert ws.query(Doc).where(Doc.flag == True).execute().ids == []  # noqa: E712
        assert ws.query(Doc).where(Doc.tags.array_contains("x")).execute().ids == []
        assert ws.query(Doc).where(Doc.note == "old").near(Doc.emb, [1.0, 0.0], top_k=1).execute().ids == []
        assert ws.query(Doc).where(Doc.note == None).execute().ids == ["a"]  # noqa: E711
        ws.upsert(Doc, FULL_ROW)
        assert _stored(ws, Doc) == {key: value for key, value in FULL_ROW.items() if key != "object_id"}
        assert ws.query(Doc).where(Doc.tags.array_contains("x")).execute().ids == ["a"]
    finally:
        ws.release()


@needs_chromadb
@pytest.mark.parametrize("client", CHROMA_CLIENTS)
def test_chroma_rows_never_carry_the_placeholder(tmp_path, client):
    """The ``_hb`` placeholder of a row without a field value is no column, including on rows 0.1.2.2 wrote."""
    ws, Doc = _chroma_workspace(tmp_path, client, "chroma-placeholder")
    try:
        entity_id = Doc.schema().entity_id
        backend = ws.backends.get("vec")
        ws.upsert(Doc, {"object_id": "z", "emb": [0.0, 1.0]})
        backend._collection(entity_id).upsert(ids=["legacy"], embeddings=[[0.0, 1.0]], metadatas=[{"_hb": True}], documents=["legacy"])
        for row in ws.query(Doc).near(Doc.emb, [0.0, 1.0], top_k=2).execute().rows():
            assert "_hb" not in row, row
        assert [sorted(row) for row in backend.get_many(entity_id, ["z", "legacy"])] == [["emb", "object_id"], ["emb", "object_id"]]
        assert all("_hb" not in row for row in backend.scan(entity_id))
        ws.upsert(Doc, {"object_id": "legacy", "title": "t", "emb": [0.0, 1.0]})
        assert backend._collection(entity_id).get(ids=["legacy"], include=["metadatas"])["metadatas"] == [{"title": "t"}]
    finally:
        ws.release()


def test_pinecone_rows_never_carry_the_placeholder(monkeypatch):
    """Pinecone writes no ``_hb`` placeholder, and rows 0.1.2.2 wrote with it do not show it."""
    ws, Doc, indexes = _pinecone_workspace(monkeypatch, "pinecone-placeholder")
    try:
        entity_id = Doc.schema().entity_id
        backend = ws.backends.get("vec")
        ws.upsert(Doc, {"object_id": "z", "emb": [0.0, 1.0]})
        [index] = indexes.values()
        assert index.records["z"]["metadata"] == {}
        index.upsert([{"id": "legacy", "values": [0.0, 1.0], "metadata": {"_hb": True}}])
        assert [sorted(row) for row in backend.get_many(entity_id, ["z", "legacy"])] == [["emb", "object_id"], ["emb", "object_id"]]
        for row in ws.query(Doc).near(Doc.emb, [0.0, 1.0], top_k=2).execute().rows():
            assert "_hb" not in row, row
    finally:
        ws.release()


def _register_big(ws, entity_identifier: str):
    """Register an entity whose Integer fields may hold integers beyond what metadata numbers hold exactly."""

    class Big(hb.Entity):
        identifier = entity_identifier

        big = hb.field(hb.Integer).store(to="vec").optional()
        bigs = hb.field(hb.Array[hb.Integer]).store(to="vec").optional()
        un = hb.field(hb.Array).store(to="vec").optional()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    ws.register(Big)
    return Big


@needs_chromadb
@pytest.mark.parametrize("client", CHROMA_CLIENTS)
def test_chroma_keeps_integers_beyond_int64(tmp_path, client):
    """An integer outside the signed 64-bit range round-trips exactly on Chroma, and filters on it agree with the scan."""
    path = None if client == "ephemeral" else hb.utils.path_to_file_uri(tmp_path / "chroma")
    backends = {"main": hb.backends.InMemBackend("main"), "vec": hb.backends.ChromaBackend("vec", path)}
    ws = hb.HeavenBase(f"chroma-bigint-{client}", detached=True, backends=backends)
    Big = _register_big(ws, f"chroma-bigint-{client}-doc")
    try:
        ws.upsert_many(
            Big,
            [
                {"object_id": "a", "big": 2**70, "bigs": [2**70, 1], "un": [2**63, 3], "emb": [1.0, 0.0]},
                {"object_id": "b", "big": 5, "bigs": [2], "un": [1], "emb": [0.0, 1.0]},
            ],
        )
        assert _stored(ws, Big) == {"big": 2**70, "bigs": [2**70, 1], "un": [2**63, 3], "emb": [1.0, 0.0]}

        def ids(where):
            return sorted(ws.query(Big).where(where).execute().ids)

        assert ids(Big.big == 2**70) == ["a"]
        assert ids(Big.big == 2**63 - 1) == []
        assert ids(Big.big.in_([2**70, 7])) == ["a"]
        assert ids(Big.big > 5) == ["a"]
        assert ids(Big.bigs.array_contains(1)) == ["a"]
        assert ids(Big.bigs.array_contains(2**70)) == ["a"]
        assert ids(Big.un.array_contains(2**63)) == ["a"]
        assert ws.query(Big).where(Big.bigs.array_contains(1)).near(Big.emb, [0.0, 1.0], top_k=1).execute().ids == ["a"]
    finally:
        ws.release()


@needs_chromadb
@pytest.mark.parametrize("client", CHROMA_CLIENTS)
def test_chroma_filters_near_the_int64_bounds_agree_with_the_scan(tmp_path, client):
    """Chroma compares an in-range operand near 2**63 with a stored out-of-range integer as a float, so such a filter scans."""
    from heavenbase.backends.vector.handler.chroma import chroma_filter_handler

    path = None if client == "ephemeral" else hb.utils.path_to_file_uri(tmp_path / "chroma")
    backends = {"main": hb.backends.InMemBackend("main"), "vec": hb.backends.ChromaBackend("vec", path)}
    ws = hb.HeavenBase(f"chroma-int64-bounds-{client}", detached=True, backends=backends)
    Big = _register_big(ws, f"chroma-int64-bounds-{client}-doc")
    try:
        values = {"hi": 2**63, "lo": -(2**63) - 1, "mid": 2**63 - 1, "edge": 2**63 - 2**10, "none": None}
        ws.upsert_many(Big, [{"object_id": key, "big": value, "emb": [1.0, 0.0]} for key, value in values.items()])

        def ids(where):
            return sorted(ws.query(Big).where(where).execute().ids)

        assert ids(Big.big == 2**63 - 1) == ["mid"]
        assert ids(Big.big > 2**63 - 1) == ["hi"]
        assert ids(Big.big != 2**63 - 1) == ["edge", "hi", "lo", "none"]
        assert ids(Big.big == -(2**63)) == []
        assert ids(Big.big < -(2**63)) == ["lo"]
        assert ids(Big.big.in_([2**63 - 1])) == ["mid"]
        assert ids(Big.big >= 2**63 - 2**10) == ["edge", "hi", "mid"]
        # Below the bound Chroma compares exactly, so the filter stays native.
        assert ids(Big.big == 2**63 - 2**10 - 1) == []
        assert ids(Big.big < 2**63 - 2**10 - 1) == ["lo"]
        field = Big.schema().get("big")
        ctx = hb.ext.HandlerContext("vec", "chroma", hb.InlineColumn.identifier)
        assert chroma_filter_handler("eq")(field, 2**63 - 2**10 - 1, ctx).mode() != "py_scan"
        assert chroma_filter_handler("eq")(field, -(2**63) + 2**10 + 1, ctx).mode() != "py_scan"
        for op, operand in (("eq", 2**63 - 2**10), ("lt", -(2**63) + 2**10), ("in", [1, 2**63 - 1])):
            assert chroma_filter_handler(op)(field, operand, ctx).mode() == "py_scan", (op, operand)
    finally:
        ws.release()


def test_pinecone_keeps_integers_beyond_float64_precision(monkeypatch):
    """Pinecone numbers are 64-bit floats; an integer beyond 2**53 keeps its exact value, and a filter on one scans."""
    from heavenbase.backends.vector.handler.pinecone import pinecone_filter_handler

    indexes = _install_fake_pinecone(monkeypatch)
    backends = {"main": hb.backends.InMemBackend("main"), "vec": hb.backends.PineconeBackend("vec", host="localhost:5080")}
    ws = hb.HeavenBase("pinecone-bigint", detached=True, backends=backends)
    Big = _register_big(ws, "pinecone-bigint-doc")
    try:
        ws.upsert(Big, {"object_id": "a", "big": 2**60 + 1, "bigs": [2**70, 1], "un": [2**63, 3], "emb": [1.0, 0.0]})
        assert _stored(ws, Big) == {"big": 2**60 + 1, "bigs": [2**70, 1], "un": [2**63, 3], "emb": [1.0, 0.0]}
        [index] = indexes.values()
        assert index.records["a"]["metadata"]["big"] == float(2**60)
        field = Big.schema().get("big")
        ctx = hb.ext.HandlerContext("vec", "pinecone", hb.InlineColumn.identifier)
        # A stored 2**53 + 1 keeps 2**53 as its metadata number, so only operands below 2**53 compare exactly.
        ws.upsert(Big, {"object_id": "b", "big": 2**53 + 1, "emb": [0.0, 1.0]})
        assert _stored(ws, Big, "b")["big"] == 2**53 + 1
        assert index.records["b"]["metadata"]["big"] == float(2**53)
        assert pinecone_filter_handler("eq")(field, 2**53 - 1, ctx).mode() != "py_scan"
        assert pinecone_filter_handler("gt")(field, -(2**53) + 1, ctx).mode() != "py_scan"
        for op, operand in (("eq", 2**53), ("ne", -(2**53)), ("gt", 2**53 + 1), ("in", [1, 2**60 + 1])):
            assert pinecone_filter_handler(op)(field, operand, ctx).mode() == "py_scan", (op, operand)
    finally:
        ws.release()


def test_chroma_refuses_a_chromadb_without_list_metadata(monkeypatch):
    """chromadb before 1.5.0 cannot hold list metadata or remove a key on upsert, so the backend names the floor."""

    class OldChroma:
        __version__ = "0.6.3"

        @staticmethod
        def EphemeralClient(**kwargs):
            raise AssertionError("an unsupported chromadb must not be used")

        PersistentClient = EphemeralClient

    monkeypatch.setitem(sys.modules, "chromadb", OldChroma)
    backend = hb.backends.ChromaBackend("vec", database="db")
    assert backend.client is None
    with pytest.raises(RuntimeError, match=r"needs chromadb>=1\.5\.0, found chromadb 0\.6\.3"):
        backend.upsert([])
    OldChroma.__version__ = "1.4.1"
    with pytest.raises(RuntimeError, match=r"found chromadb 1\.4\.1"):
        hb.backends.ChromaBackend("vec", database="db").upsert([])


def test_pinecone_query_recovers_after_explain_metric_lookup_times_out(monkeypatch):
    ws, doc, _ = _pinecone_workspace(monkeypatch, "pinecone-explain-timeout")
    try:
        ws.upsert(doc, FULL_ROW)
        backend = ws.backends.get("vec")
        client = backend.client
        describe_index = client.describe_index
        failed = False

        def describe_once_with_timeout(name):
            nonlocal failed
            if not failed:
                failed = True
                raise TimeoutError("index metadata timed out")
            return describe_index(name)

        monkeypatch.setattr(client, "describe_index", describe_once_with_timeout)
        query = ws.query(doc).near(doc.emb, [1.0, 0.0], top_k=1)
        with pytest.raises(RuntimeError, match="index metadata timed out") as error:
            query.explain()
        assert isinstance(error.value.__cause__, TimeoutError)

        assert query.execute().ids == ["a"]
        assert backend.client is client
        assert backend.to_dict()["connected"]
    finally:
        ws.release()
