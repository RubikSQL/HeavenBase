"""Workspace batch writes of more rows than SQLite binds in one statement succeed on the SQLite backend."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

import heavenbase as hb

pytestmark = [pytest.mark.full, pytest.mark.integration]

# Impose the modern SQLite default even when the installed build allows more.
VARIABLE_LIMIT = 32_766


class LargeBatchDoc(hb.Entity):
    identifier = "large-batch-doc"

    title = hb.field(hb.ShortText)


def _enforce_variable_limit(engine, limit):
    rows = limit + 1
    if hasattr(sqlite3.Connection, "setlimit"):

        def set_variable_limit(connection, record, proxy):
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, limit)

        event.listen(engine, "checkout", set_variable_limit)
    else:
        # Python 3.10 does not expose SQLite's per-connection limit API.
        def check_variable_limit(connection, cursor, statement, parameters, context, executemany):
            if not executemany and len(parameters) > limit:
                raise sqlite3.OperationalError("too many SQL variables")

        event.listen(engine, "before_cursor_execute", check_variable_limit)

    with engine.connect() as connection:
        placeholders = ",".join("?" for _ in range(rows))
        with pytest.raises((OperationalError, sqlite3.OperationalError), match="too many SQL variables"):
            connection.exec_driver_sql(f"SELECT 1 WHERE 1 IN ({placeholders})", (1,) * rows)


@pytest.mark.parametrize("catalog_mode", ["merge", "fresh"])
@pytest.mark.parametrize(
    "limit",
    [
        VARIABLE_LIMIT,
        pytest.param(
            1024,
            marks=pytest.mark.skipif(not hasattr(sqlite3.Connection, "setlimit"), reason="sqlite3 lacks connection limit APIs"),
        ),
    ],
)
def test_sqlite_upsert_many_and_delete_many_beyond_the_variable_limit(tmp_path, catalog_mode, limit):
    """Once ``too many SQL variables``: each batch deleted all its ids in one ``DELETE ... IN``."""
    database = hb.utils.path_to_file_uri(tmp_path / "large.db")
    ws = hb.HeavenBase(f"sql-large-{catalog_mode}", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": database}})
    try:
        ws.register(LargeBatchDoc)
        backend, entity_id = ws.backends.get("main"), LargeBatchDoc.schema().entity_id
        _enforce_variable_limit(backend.db.engine, limit)
        count = limit + 1
        rows = [{"object_id": f"r{index}", "title": f"t{index}"} for index in range(count)]
        ws.upsert_many(LargeBatchDoc, rows, catalog_mode=catalog_mode)
        assert backend.count(entity_id) == count
        ws.upsert_many(LargeBatchDoc, [{**row, "title": "again"} for row in rows], catalog_mode=catalog_mode)
        assert backend.count(entity_id) == count
        assert ws.get(f"r{count - 1}", entity=LargeBatchDoc)["title"] == "again"
        assert all(ws.delete_many([(LargeBatchDoc, row["object_id"]) for row in rows]))
        assert backend.count(entity_id) == 0
    finally:
        ws.release()
