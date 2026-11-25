"""Standalone SQLite rollback cleanup preserves a safe execution response."""

import sys

import pytest

import heavenbase as hb


@pytest.mark.fast
@pytest.mark.skipif(sys.version_info < (3, 12), reason="sqlite3 gained the autocommit argument in Python 3.12")
def test_sqlite_standalone_write_preserves_rollback_trigger_error(tmp_path):
    db = hb.Database(
        provider="sqlite",
        database=(tmp_path / "standalone.db").as_uri(),
        pool={"connect_args": {"autocommit": False}},
        pragmas=[],
    )
    try:
        db.execute("CREATE TABLE notes (title TEXT)").close()
        db.execute("""CREATE TRIGGER reject_note BEFORE INSERT ON notes
            WHEN NEW.title = 'reject' BEGIN SELECT RAISE(ROLLBACK, 'rejected title'); END""").close()
        response = db.execute("INSERT INTO notes VALUES (:title)", [{"title": "temporary"}, {"title": "reject"}], safe=True)
        assert not response.ok
        assert "rejected title" in response.error_message
        assert db.execute("SELECT title FROM notes", readonly=True).to_lists() == []
        db.execute("INSERT INTO notes VALUES ('retry')").close()
        assert db.execute("SELECT title FROM notes", readonly=True).to_lists() == [["retry"]]
    finally:
        db.dispose()
