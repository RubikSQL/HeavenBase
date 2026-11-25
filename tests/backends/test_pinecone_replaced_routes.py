"""A route that a replacement registration adds settles absence only once no older route of the entity is left."""

import pytest

import heavenbase as hb
from test_pinecone_records import _install_fake_pinecone, _workspace

pytestmark = pytest.mark.fast


def _note(*, summary_on=None, embedding_on="vec"):
    """Return a note entity with its title and embedding on ``embedding_on``, and optionally a summary on ``summary_on``."""

    class Note(hb.Entity):
        identifier = "replaced-route-note"

        title = hb.field(hb.ShortText).store(to=embedding_on)
        embedding = hb.field(hb.Vector[2]).store(to=embedding_on, strategy=hb.VectorIndex.identifier)
        if summary_on is not None:
            summary = hb.field(hb.ShortText).store(to=summary_on).optional()

    return Note


class Draft(hb.Entity):
    identifier = "replaced-route-draft"

    title = hb.field(hb.ShortText)


def _stored_note(ws):
    """Store one note on Pinecone only, then add an InMem or SQL route through a replacement registration."""
    ws.register(_note())
    ws.register(Draft)
    ws.upsert(_note().identifier, {"object_id": "meeting-notes", "title": "Original notes", "embedding": [1, 0]})
    ws.upsert(Draft, {"object_id": "meeting-notes", "title": "Draft notes"})
    return ws.register(_note(summary_on="main"), allow_replace=True)


def _refuses_absence(ws, index, Note):
    """Check that a fetch miss on Pinecone is not settled by the added route, which holds none of the stored rows."""
    index.stale.add("meeting-notes")
    with pytest.raises(ValueError, match="Pinecone backend 'vec'.*overwrite=False cannot confirm"):
        ws.upsert(Note, {"object_id": "meeting-notes", "title": "Replacement", "embedding": [0, 1]}, overwrite=False)
    index.stale.add("meeting-notes")
    with pytest.raises((ValueError, RuntimeError), match="Pinecone backend 'vec'.*specify the target entity explicitly"):
        ws.set({"object_id": "meeting-notes", "title": "Revised"})
    assert ws.get("meeting-notes", entity=Note)["title"] == "Original notes"
    assert ws.get("meeting-notes", entity=Draft)["title"] == "Draft notes"


@pytest.mark.slow
def test_a_route_added_by_a_replacement_does_not_settle_absence_of_a_row_stored_before(monkeypatch):
    ws, indexes = _workspace(monkeypatch, "replaced-route-live")
    try:
        Note = _stored_note(ws)
        [index] = indexes.values()
        _refuses_absence(ws, index, Note)
        # The older route stays the only one that can settle absence, so the entity keeps Pinecone's rule, as before the change.
        with pytest.raises(ValueError, match="overwrite=False cannot confirm"):
            ws.upsert(Note, {"object_id": "new-notes", "title": "New", "embedding": [0, 1]}, overwrite=False)
        assert ws.upsert(Note, {"object_id": "new-notes", "title": "New", "embedding": [0, 1]}) == "new-notes"
    finally:
        ws.release()


@pytest.mark.slow
def test_an_added_route_settles_absence_once_the_older_route_is_gone(monkeypatch):
    """Rows the added route lacks are held only on routes the entity no longer reads, so no read returns them."""
    ws, indexes = _workspace(monkeypatch, "replaced-route-dropped")
    try:
        _stored_note(ws)
        Note = ws.register(_note(summary_on="main", embedding_on="main"), allow_replace=True)
        assert ws.get("meeting-notes", entity=Note) is None
        assert ws.upsert(Note, {"object_id": "meeting-notes", "title": "Moved", "embedding": [0, 1]}, overwrite=False) == "meeting-notes"
        with pytest.raises(ValueError, match="already exists"):
            ws.upsert(Note, {"object_id": "meeting-notes", "title": "Again", "embedding": [0, 1]}, overwrite=False)
        assert ws.upsert(Note, {"object_id": "other-notes", "title": "Other", "embedding": [1, 0]}, overwrite=False) == "other-notes"
    finally:
        ws.release()


def test_an_older_route_other_than_pinecone_still_settles_absence_after_a_route_is_added(monkeypatch):
    ws = hb.HeavenBase("replaced-route-kept", detached=True, backends={"main": hb.backends.InMemBackend("main"), "aux": hb.backends.InMemBackend("aux")})

    def memo(with_note):
        class Memo(hb.Entity):
            identifier = "replaced-route-memo"

            title = hb.field(hb.ShortText).store(to="main")
            if with_note:
                note = hb.field(hb.ShortText).store(to="aux").optional()

        return Memo

    try:
        ws.register(memo(False))
        ws.upsert(memo(False).identifier, {"object_id": "m1", "title": "First"})
        Memo = ws.register(memo(True), allow_replace=True)
        assert ws.upsert(Memo, {"object_id": "m2", "title": "Second"}, overwrite=False) == "m2"
        with pytest.raises(ValueError, match="already exists"):
            ws.upsert(Memo, {"object_id": "m1", "title": "Again"}, overwrite=False)
        assert ws.delete("never-written") is False
    finally:
        ws.release()


def _sqlite_pinecone(tmp_path, name):
    database = hb.utils.path_to_file_uri(tmp_path / "replaced-route.db")
    backends = {"main": hb.backends.SQLiteBackend("main", database=database), "vec": hb.backends.PineconeBackend("vec", host="localhost:5080")}
    return hb.HeavenBase(name, detached=True, backends=backends)


@pytest.mark.slow
def test_a_later_facade_reads_which_route_a_replacement_added(monkeypatch, tmp_path):
    """The order in which routes joined is stored with the entity's MetaSchema row, so a new facade keeps the rule."""
    stores = _install_fake_pinecone(monkeypatch)
    first = _sqlite_pinecone(tmp_path, "replaced-route-stored")
    try:
        _stored_note(first)
    finally:
        first.release()
    second = _sqlite_pinecone(tmp_path, "replaced-route-stored")
    try:
        Note = second.register(_note(summary_on="main"))
        second.register(Draft)
        [index] = stores["localhost:5080"].values()
        _refuses_absence(second, index, Note)
    finally:
        second.release()


@pytest.mark.slow
def test_a_replacement_in_a_new_facade_compares_routes_with_the_stored_registration(monkeypatch, tmp_path):
    stores = _install_fake_pinecone(monkeypatch)
    first = _sqlite_pinecone(tmp_path, "replaced-route-cold")
    try:
        first.register(_note())
        first.register(Draft)
        first.upsert(_note().identifier, {"object_id": "meeting-notes", "title": "Original notes", "embedding": [1, 0]})
        first.upsert(Draft, {"object_id": "meeting-notes", "title": "Draft notes"})
    finally:
        first.release()
    second = _sqlite_pinecone(tmp_path, "replaced-route-cold")
    try:
        Note = second.register(_note(summary_on="main"), allow_replace=True)
        second.register(Draft)
        [index] = stores["localhost:5080"].values()
        _refuses_absence(second, index, Note)
    finally:
        second.release()
