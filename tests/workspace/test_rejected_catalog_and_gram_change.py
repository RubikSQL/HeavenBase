import sqlite3
from contextlib import closing

import heavenbase as hb
import pytest

from heavenbase.database.execution import DatabaseError

pytestmark = pytest.mark.fast


class PreviousNote(hb.Entity):
    identifier = "r9ws-mixed"
    name = hb.field(hb.ShortText)
    region = hb.field(hb.ShortText)
    keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))
    __constraints__ = [hb.unique("name", "region")]


class IncomingNote(hb.Entity):
    identifier = PreviousNote.identifier
    name = hb.field(hb.ShortText)
    region = hb.field(hb.ShortText)
    keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))


@pytest.mark.slow
def test_rejected_catalog_and_gram_change_keeps_the_stored_constraint(tmp_path):
    path = tmp_path / "mixed.sqlite"
    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(path)}}
    row = {"object_id": "n1", "name": "Note", "region": "west", "keywords": ["search query"]}
    original = hb.HeavenBase("r9ws-mixed", detached=True, backends=backends)
    try:
        original.register(PreviousNote)
        original.upsert(PreviousNote, row)
        saved = original.get("entity:r9ws-mixed", entity=hb.MetaSchema)
        backend = original.backends.get("main")
        indexes = backend.db.table_indexes(backend.tables[PreviousNote.identifier])
        constraint = next(index["name"] for index in indexes if index["unique"] and index["column_names"] == ["name", "region"])
    finally:
        original.release()

    reopened = hb.HeavenBase("r9ws-mixed", detached=True, backends=backends)
    try:
        with pytest.warns(RuntimeWarning, match="differs from the stored schema"), pytest.raises(ValueError, match="gram=False conflicts"):
            reopened.register(IncomingNote, catalog="identity", gram=False, allow_replace=True)
        assert reopened.get("entity:r9ws-mixed", entity=hb.MetaSchema) == saved
        assert reopened.entities.get(PreviousNote.identifier) is not IncomingNote
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (constraint,)).fetchone() == (1,)
        reopened.register(PreviousNote)
        with pytest.raises(DatabaseError, match="UNIQUE constraint failed"):
            reopened.upsert(PreviousNote, dict(row, object_id="n2"))
    finally:
        reopened.release()
