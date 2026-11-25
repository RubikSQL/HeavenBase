"""An unrelated patch when Pinecone misses a read of required fields."""

import pytest

import heavenbase as hb
from test_pinecone_records import _workspace


@pytest.mark.slow
def test_title_patch_can_be_retried_after_a_required_pinecone_record_is_missed(monkeypatch):
    class Doc(hb.Entity):
        identifier = "pinecone-required-fields"

        title = hb.field(hb.ShortText)
        status = hb.field(hb.ShortText).store(to="vec").required()
        emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier).required()

    ws, indexes = _workspace(monkeypatch, "pinecone-required-fields")
    try:
        ws.register(Doc)
        original = {"object_id": "a", "title": "original", "status": "ready", "emb": [1.0, 0.0]}
        ws.upsert(Doc, original)
        [index] = indexes.values()
        index.stale = {"a"}

        with pytest.raises(ValueError) as failure:
            ws.set("a", {"title": "updated"}, entity=Doc)
        assert ws.get("a", entity=Doc) == original
        assert str(failure.value) == (
            "Pinecone backend 'vec' returned no record for entity 'pinecone-required-fields' object_id 'a'; "
            "retry it once the record can be read, or use a full upsert if the record is absent"
        )
        assert ws.set("a", {"title": "updated"}, entity=Doc) == "a"
        assert ws.get("a", entity=Doc) == {**original, "title": "updated"}
    finally:
        ws.release()
