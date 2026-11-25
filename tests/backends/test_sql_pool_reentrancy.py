"""SQL connection admission preserves nested reads and backend extensions."""

from __future__ import annotations

from dataclasses import dataclass
from queue import Queue
from threading import Thread

import pytest
from sqlalchemy import event

import heavenbase as hb
from heavenbase.backends import SQLiteBackend

pytestmark = [pytest.mark.fast, pytest.mark.unit]


class PoolDoc(hb.Entity):
    identifier = "sql-pool-regression-doc"

    title = hb.field(hb.ShortText)


_ROW = {"object_id": "a", "title": "alpha"}


def test_sql_read_can_nest_on_the_spare_pool_connection(tmp_path):
    backend = SQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / "nested.db"), pool={"pool_size": 2, "max_overflow": 0, "pool_timeout": 1})
    ws = hb.HeavenBase("sql-pool-nested", detached=True, backends={"main": backend})
    ws.register(PoolDoc)
    ws.upsert(PoolDoc, _ROW)
    assert backend.count(PoolDoc.identifier) == 1  # resolve the isolation probe before installing the callback
    engine = backend.db.engine
    counts = []
    outcome = Queue()

    def read_nested(conn, cursor, statement, parameters, context, executemany):
        assert engine.pool.checkedout() == 1
        counts.append(backend.count(PoolDoc.identifier))

    def read():
        try:
            outcome.put(backend.scan(PoolDoc.identifier))
        except Exception as error:
            outcome.put(error)

    event.listen(engine, "before_cursor_execute", read_nested, once=True)
    reader = Thread(target=read, daemon=True)
    reader.start()
    reader.join(5)
    assert not reader.is_alive(), "nested read waited for its own outer read while a connection was free"
    try:
        result = outcome.get_nowait()
        if isinstance(result, Exception):
            raise result
        assert counts == [1]
        assert result == [_ROW]
    finally:
        event.remove(engine, "before_cursor_execute", read_nested)
        ws.release()


def test_sql_pool_accepts_an_unhashable_backend_subclass(tmp_path):
    @dataclass(init=False)
    class CustomSQLite(SQLiteBackend):
        pass

    backend = CustomSQLite("main", hb.utils.path_to_file_uri(tmp_path / "custom.db"))
    assert type(backend).__hash__ is None
    ws = hb.HeavenBase("sql-pool-unhashable", detached=True, backends={"main": backend})
    try:
        ws.register(PoolDoc)
        ws.upsert(PoolDoc, _ROW)
        assert backend.scan(PoolDoc.identifier) == [_ROW]
    finally:
        ws.release()
