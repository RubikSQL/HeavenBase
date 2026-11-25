"""Persistent SQLite facades see a single schema and database lifecycle."""

from __future__ import annotations

import pytest

import heavenbase as hb
from heavenbase.database import DatabaseError

pytestmark = [pytest.mark.fast, pytest.mark.unit]


@pytest.fixture(params=["database", "backend"])
def static_sqlite(request, tmp_path):
    path = tmp_path / "cloned.db"
    uri = hb.utils.path_to_file_uri(path)
    pool = {"pool_class": "static"}
    if request.param == "backend":
        backend = hb.backends.SQLiteBackend("main", uri, pool=pool)
        db = backend.db
        drop = backend.drop
    else:
        db = hb.Database(provider="sqlite", database=uri, pool=pool)
        drop = db.drop_database
    db.execute("CREATE TABLE items (id INTEGER)")
    db.execute("INSERT INTO items VALUES (1)")
    clone = db.clone()
    try:
        yield db, clone, path, drop
    finally:
        clone.dispose()
        db.dispose()


def test_persistent_sqlite_clone_refreshes_schema_index_after_ddl(static_sqlite):
    db, clone, _, _ = static_sqlite
    assert clone.schema_index() == {"items": ["id"]}

    db.execute("ALTER TABLE items ADD COLUMN detail TEXT")

    assert clone.schema_index() == {"items": ["id", "detail"]}


def test_persistent_sqlite_drop_disposes_the_clone(static_sqlite):
    _, clone, path, drop = static_sqlite
    assert clone.execute("SELECT id FROM items").column(0) == [1]

    drop()

    assert not path.exists()
    with pytest.raises(DatabaseError, match="disposed"):
        clone.execute("SELECT id FROM items").column(0)
