import heavenbase as hb
import pytest

from heavenbase.backends.sql.sqlite import SQLiteBackend
from heavenbase.database.execution import DatabaseError

pytestmark = pytest.mark.fast


class RecordingNote(hb.Entity):
    identifier = "review-note"
    title = hb.field(hb.ShortText)
    region = hb.field(hb.ShortText)
    __constraints__ = [hb.unique("title", "region")]


class RevisedNote(hb.Entity):
    identifier = "review-note"
    title = hb.field(hb.ShortText)
    region = hb.field(hb.ShortText)


class InterruptibleSQLite(SQLiteBackend):
    def __init__(self, database):
        super().__init__("main", database)
        self.after_note_projection = None
        self.reject_catalog = False

    def upsert(self, rows):
        result = super().upsert(rows)
        if self.after_note_projection is not None and any(row.entity_id == hb.MetaSchema.identifier and row.object_id == "entity:review-note" for row in rows):
            callback = self.after_note_projection
            self.after_note_projection = None
            callback()
            raise OSError("Schema publication interrupted")
        return result

    def upsert_owned(self, rows, *, owner_field, owner):
        if self.reject_catalog and any(row.entity_id == hb.Catalog.identifier for row in rows):
            raise OSError("Catalog write interrupted")
        return super().upsert_owned(rows, owner_field=owner_field, owner=owner)


@pytest.mark.slow
def test_rollback_preserves_a_later_metaschema_write(tmp_path):
    database = hb.utils.path_to_file_uri(tmp_path / "notes.sqlite")
    backend = InterruptibleSQLite(database)
    backends = {"main": {"type": "sqlite", "database": database}}
    first = hb.HeavenBase("review-notes", detached=True, backends={"main": backend})
    second = hb.HeavenBase("review-notes", detached=True, backends=backends)
    try:
        first.register(RecordingNote)
        constraint_id = "constraint:review-note.title-region.main.unique"
        assert second.get(constraint_id, entity=hb.MetaSchema) is not None

        def publish_description():
            row = second.get("field:review-note.title", entity=hb.MetaSchema)
            second.upsert(hb.MetaSchema, dict(row, desc="Description saved by the other facade"))
            assert second.get("field:review-note.title", entity=hb.MetaSchema)["desc"] == "Description saved by the other facade"
            assert second.delete((hb.MetaSchema, constraint_id))

        backend.after_note_projection = publish_description
        with pytest.raises(OSError, match="Schema publication interrupted"):
            first.register(RevisedNote, gram=False, allow_replace=True)
        row = second.get("field:review-note.title", entity=hb.MetaSchema)
        assert row["desc"] == "Description saved by the other facade"
        assert second.get(constraint_id, entity=hb.MetaSchema) is None
    finally:
        second.release()
        first.release()


@pytest.mark.slow
def test_failed_catalog_rewrite_does_not_drop_stored_unique_index(tmp_path):
    database = hb.utils.path_to_file_uri(tmp_path / "notes.sqlite")
    backends = {"main": {"type": "sqlite", "database": database}}
    original = hb.HeavenBase("review-notes", detached=True, backends=backends)
    row = {"object_id": "note-1", "title": "Field notes", "region": "west"}
    try:
        original.register(RecordingNote)
        original.upsert(RecordingNote, row)
    finally:
        original.release()

    backend = InterruptibleSQLite(database)
    reopened = hb.HeavenBase("review-notes", detached=True, backends={"main": backend})
    try:
        backend.reject_catalog = True
        with pytest.raises(RuntimeError, match="keeps catalog='full'"):
            reopened.register(RevisedNote, catalog="identity", allow_replace=True)
        backend.reject_catalog = False
        assert RecordingNote.identifier not in reopened.entities
        indexes = backend.db.table_indexes(backend.tables[RecordingNote.identifier])
        assert any(index["unique"] and index["column_names"] == ["title", "region"] for index in indexes)
        reopened.register(RecordingNote)
        with pytest.raises(DatabaseError, match="UNIQUE constraint failed"):
            reopened.upsert("review-note", dict(row, object_id="note-2"))
    finally:
        reopened.release()
