import heavenbase as hb
import pytest

from heavenbase.backends.base import Backend
from heavenbase.backends.sql.sqlite import SQLiteBackend

pytestmark = pytest.mark.fast


class ScanOnlySQLite(SQLiteBackend):
    get_many = Backend.get_many

    def __init__(self, database):
        super().__init__("main", database)
        self.metaschema_scans = 0

    def scan(self, entity_id):
        if entity_id == hb.MetaSchema.identifier:
            self.metaschema_scans += 1
        return super().scan(entity_id)


class Note(hb.Entity):
    identifier = "review-note"
    title = hb.field(hb.ShortText)


class Folder(hb.Entity):
    identifier = "review-folder"
    name = hb.field(hb.ShortText)


@pytest.mark.slow
def test_scan_route_observes_registration_published_after_first_lookup(tmp_path):
    database = hb.utils.path_to_file_uri(tmp_path / "notes.sqlite")
    reader = hb.HeavenBase("review-notes", detached=True, backends={"main": ScanOnlySQLite(database)})
    writer = None
    try:
        reader.register(Folder)
        writer = hb.HeavenBase("review-notes", detached=True, backends={"main": ScanOnlySQLite(database)})
        writer.register(Note, catalog="none")
        writer.upsert(Note, {"object_id": "note-1", "title": "Field notes"})

        with pytest.raises(ValueError, match="stored with catalog='none'"):
            reader.register(Note, catalog="full")
    finally:
        if writer is not None:
            writer.release()
        reader.release()


@pytest.mark.slow
def test_reopening_many_entities_uses_one_metaschema_scan(tmp_path):
    database = hb.utils.path_to_file_uri(tmp_path / "notes.sqlite")
    definitions = [{"entity_id": f"review-folder-{number}", "fields": {"name": {"type": "short-text"}}} for number in range(8)]
    writer = hb.HeavenBase("review-notes", detached=True, backends={"main": ScanOnlySQLite(database)})
    try:
        for definition in definitions:
            writer.register(definition)
    finally:
        writer.release()

    backend = ScanOnlySQLite(database)
    reader = hb.HeavenBase("review-notes", detached=True, backends={"main": backend})
    try:
        backend.metaschema_scans = 0
        for definition in definitions:
            reader.register(definition)
        assert backend.metaschema_scans <= 1
    finally:
        reader.release()
