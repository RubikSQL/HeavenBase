"""A file-backed SQLite database is opened by the path its symbolic links resolve to as each connection opens.

SQLite names a database's ``-wal`` and ``-shm`` files after the path it opens. Its Windows file layer does not
resolve symbolic links, so a database opened through a file link kept a second write-ahead log beside the link.
The configured name stays what users see: the engine URL and the printed config. A link that changes target is
followed by the next connection, as before.
"""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import event

import heavenbase as hb
from heavenbase.database.config import DB_ENGINE

pytestmark = pytest.mark.fast


def _link(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Cannot create a symbolic link here: {exc}")
    return link


def _opened_names(db):
    """Return the file names the driver receives for the engine's new connections."""
    names = []
    db.engine.dispose()
    event.listen(db.engine, "do_connect", lambda dialect, record, cargs, cparams: names.append(cargs[0]))
    return names


def test_a_database_reached_through_a_file_link_opens_the_linked_file(tmp_path):
    path = tmp_path / "ledger.db"
    alias = _link(tmp_path / "ledger-link.db", path)
    db = hb.Database(provider="sqlite", database=alias.as_uri())
    try:
        names = _opened_names(db)
        with db.engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            conn.exec_driver_sql("CREATE TABLE entries (id INTEGER)")
            conn.commit()
            assert (tmp_path / "ledger.db-wal").exists()
            assert not (tmp_path / "ledger-link.db-wal").exists()
        assert names and set(names) == {str(path.resolve())}
        assert Path(db.engine.url.database) == alias
        assert "ledger-link.db" in DB_ENGINE.apply(db.spec, profile="print")["uri"]
    finally:
        db.dispose()


def test_a_reader_through_a_folder_link_sees_what_a_writer_committed_by_the_real_path(tmp_path):
    (tmp_path / "data").mkdir()
    path = tmp_path / "data" / "ledger.db"
    linked = _link(tmp_path / "linked-data", tmp_path / "data", directory=True) / "ledger.db"
    writer = hb.Database(provider="sqlite", database=path.as_uri())
    try:
        with writer.engine.connect() as conn:
            conn.exec_driver_sql("CREATE TABLE entries (id INTEGER)")
            conn.exec_driver_sql("INSERT INTO entries VALUES (7)")
            conn.commit()
            reader = hb.Database(provider="sqlite", database=linked.as_uri())
            try:
                names = _opened_names(reader)
                with reader.engine.connect() as other:
                    assert other.exec_driver_sql("SELECT id FROM entries").scalars().all() == [7]
                assert names and set(names) == {str(path.resolve())}
            finally:
                reader.dispose()
    finally:
        writer.dispose()


def test_a_database_opened_by_its_own_path_keeps_the_driver_name(tmp_path):
    path = (tmp_path / "ledger.db").resolve()
    db = hb.Database(provider="sqlite", database=path.as_uri())
    try:
        names = _opened_names(db)
        db.execute("CREATE TABLE entries (id INTEGER)")
        assert names and set(names) == {str(path)}
    finally:
        db.dispose()


def _tables(path):
    with sqlite3.connect(str(path)) as conn:
        return sorted(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))


def test_a_new_connection_follows_a_link_that_changed_target(tmp_path):
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    for path in (first, second):
        sqlite3.connect(str(path)).close()
    alias = _link(tmp_path / "current.db", first)
    db = hb.Database(provider="sqlite", database=alias.as_uri())
    try:
        db.execute("CREATE TABLE ta (id INTEGER)")
        db.engine.dispose()
        alias.unlink()
        _link(alias, second)
        db.execute("CREATE TABLE tb (id INTEGER)")
    finally:
        db.dispose()
    assert (_tables(first), _tables(second)) == (["ta"], ["tb"])
