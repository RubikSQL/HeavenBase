"""SQLite transaction cleanup preserves the failure sent by the driver."""

import sys

import pytest

import heavenbase as hb
from heavenbase.database import DatabaseError
from heavenbase.ext import RowOp


class RollbackNote(hb.Entity):
    identifier = "r9sqlx-rollback-note"
    title = hb.field(hb.ShortText)


@pytest.mark.fast
@pytest.mark.skipif(sys.version_info < (3, 12), reason="sqlite3 gained the autocommit argument in Python 3.12")
def test_sqlite_top_level_upsert_preserves_rollback_trigger_error(tmp_path):
    config = {
        "type": "sqlite",
        "database": (tmp_path / "rollback.db").as_uri(),
        "pool": {"connect_args": {"autocommit": False}},
        "pragmas": [],
    }
    ws = hb.HeavenBase("r9sqlx-rollback", detached=True, backends={"main": config})
    try:
        ws.register(RollbackNote)
        backend = ws.backends.get("main")
        entity = RollbackNote.identifier
        table = backend.tables[entity].name
        backend.db.execute(f"""CREATE TRIGGER reject_note BEFORE INSERT ON "{table}"
            WHEN NEW.title = 'reject' BEGIN SELECT RAISE(ROLLBACK, 'rejected title'); END""").close()
        backend.upsert([RowOp(entity, "original", {"title": "before"})])
        before = backend.scan(entity)

        with pytest.raises(DatabaseError, match="rejected title"):
            backend.upsert([RowOp(entity, "new", {"title": "ok"}), RowOp(entity, "original", {"title": "reject"})])

        assert backend.scan(entity) == before
        backend.upsert([RowOp(entity, "new", {"title": "retry"})])
        assert backend.get_many(entity, ["new"])[0]["title"] == "retry"
    finally:
        ws.release()
