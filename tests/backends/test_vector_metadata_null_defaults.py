"""Round-trip explicit nulls on scalar-only vector metadata providers."""

import pytest

import heavenbase as hb
from heavenbase.execution.contracts import RowOp
from heavenbase.workspace.rows.errors import SchemaVersionMismatchError
from test_pinecone_records import _workspace

pytestmark = pytest.mark.fast


@pytest.mark.slow
@pytest.mark.parametrize("provider", ["pinecone", "chroma"])
def test_nullable_vector_metadata_with_a_default_can_be_cleared(monkeypatch, tmp_path, provider):
    if provider == "pinecone":
        ws, _ = _workspace(monkeypatch, "pinecone-null-default")
    else:
        pytest.importorskip("chromadb")
        backends = {
            "main": hb.backends.InMemBackend("main"),
            "vec": hb.backends.ChromaBackend("vec", hb.utils.path_to_file_uri(tmp_path / "chroma")),
        }
        ws = hb.HeavenBase("chroma-null-default", detached=True, backends=backends)

    class Doc(hb.Entity):
        identifier = f"{provider}-null-default-doc"

        note = hb.field(hb.ShortText).store(to="vec").optional().default("default")
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    try:
        ws.register(Doc)
        ws.upsert(Doc, {"object_id": "a", "emb": [1.0, 0.0]})
        assert ws.get("a", entity=Doc)["note"] == "default"
        assert ws.set("a", {"note": None}, entity=Doc) == "a"
        assert ws.exists("a", entity=Doc)
        assert ws.get("a", entity=Doc) == {"object_id": "a", "note": None, "emb": [1.0, 0.0]}
        assert ws.get("a", entity=Doc, columns=["note"]) == {"object_id": "a", "note": None}
        assert ws.set("a", {"emb": [0.0, 1.0]}, entity=Doc) == "a"
        assert ws.get("a", entity=Doc)["note"] is None

        ws.backends.get("vec").upsert([RowOp(Doc.identifier, "legacy", {"emb": [1.0, 0.0]})])
        with pytest.raises(SchemaVersionMismatchError, match="predates current schema fields.*note"):
            ws.get("legacy", entity=Doc)
    finally:
        ws.release()
