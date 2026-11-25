"""Typed textual SQLite statements must distinguish identical TEXT and BLOB bytes."""

import sqlite3
import struct
from contextlib import closing
from dataclasses import replace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, registry

from heavenbase.database.dialect.registry import database_dialects
from heavenbase.database.types import DatabaseVectorType

pytestmark = [pytest.mark.fast, pytest.mark.unit]


@pytest.mark.parametrize("storage", [None, "float64_blob"])
@pytest.mark.parametrize("positional", [False, True])
@pytest.mark.parametrize("compiled", [False, True])
@pytest.mark.parametrize("callable_factory", [False, True])
def test_typed_text_reads_sqlite_vector_storage_class(storage, positional, compiled, callable_factory):
    payload = b"[1234.0]"
    spec = replace(database_dialects.get("sqlite"), vector_packed_type=storage)

    def text_factory(value):
        return bytes(value)

    def row_factory(cursor, row):
        if len(row) == 2 and row[0] == b"untyped":
            assert row[1] is None or type(row[1]) is bytes
            ordinary = cursor.connection.execute("SELECT 'nested'").fetchone()[0]
            assert type(ordinary) is bytes
        return sqlite3.Row(cursor, row)

    with closing(sqlite3.connect(":memory:")) as raw:
        factory = text_factory if callable_factory else bytes
        raw.text_factory = factory
        raw.row_factory = row_factory
        engine = sa.create_engine("sqlite://", creator=lambda: raw)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE vectors (id INTEGER, embedding TEXT)")
                conn.exec_driver_sql("INSERT INTO vectors VALUES (1, '[1234.0]'), (2, ?), (3, NULL)", (payload,))
                assert conn.exec_driver_sql("SELECT typeof(embedding) FROM vectors ORDER BY id").scalars().all() == [b"text", b"blob", b"null"]
                dtype = DatabaseVectorType(dialect_spec=spec)
                statement = sa.text("SELECT 'untyped', embedding FROM vectors WHERE id = :row_id")
                read = statement.columns(sa.column("description"), sa.column("vec", dtype)) if positional else statement.columns(embedding=dtype)
                if compiled:
                    read = read.compile(engine)
                row = conn.execute(read, {"row_id": 1}).one()
                assert row == (b"untyped", [1234.0])
                assert type(row[0]) is bytes
                assert raw.text_factory is factory
                assert conn.execute(read, {"row_id": 3}).fetchmany(1) == [(b"untyped", None)]
                if storage:
                    assert conn.execute(read, {"row_id": 2}).all() == [(b"untyped", list(struct.unpack("<d", payload)))]
                else:
                    with pytest.raises(ValueError, match="SQLite BLOB of 8 bytes"):
                        conn.execute(read, {"row_id": 2}).one()
                assert type(conn.exec_driver_sql("SELECT 'ordinary'").scalar_one()) is bytes
        finally:
            engine.dispose()


@pytest.mark.parametrize("storage", [None, "float64_blob"])
@pytest.mark.parametrize("typed", [False, True])
def test_orm_from_statement_keeps_sqlite_vector_storage_class(storage, typed):
    spec = replace(database_dialects.get("sqlite"), vector_packed_type=storage)
    dtype = DatabaseVectorType(dialect_spec=spec)
    mappings = registry()
    table = sa.Table("vectors", mappings.metadata, sa.Column("id", sa.Integer(), primary_key=True), sa.Column("embedding", dtype))

    class VectorRow:
        pass

    mappings.map_imperatively(VectorRow, table)
    with closing(sqlite3.connect(":memory:")) as raw:
        raw.text_factory = bytes
        engine = sa.create_engine("sqlite://", creator=lambda: raw)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE vectors (id INTEGER PRIMARY KEY, embedding TEXT)")
                conn.exec_driver_sql("INSERT INTO vectors VALUES (1, '[1234.0]'), (2, ?)", (b"[1234.0]",))
            text = sa.text("SELECT embedding, id FROM vectors WHERE id = :row_id")
            statement = sa.select(VectorRow).from_statement(text.columns(embedding=dtype, id=sa.Integer()) if typed else text)
            with Session(engine) as session:
                assert session.scalars(statement, {"row_id": 1}).one().embedding == [1234.0]
                if storage:
                    assert session.scalars(statement, {"row_id": 2}).one().embedding == list(struct.unpack("<d", b"[1234.0]"))
                else:
                    with pytest.raises(ValueError, match="SQLite BLOB of 8 bytes"):
                        session.scalars(statement, {"row_id": 2}).one()
        finally:
            engine.dispose()
            mappings.dispose()


@pytest.mark.parametrize("no_parameters", [False, True])
def test_typed_text_preserves_prefetched_sqlite_rows(no_parameters):
    spec = replace(database_dialects.get("sqlite"), vector_packed_type="float64_blob")
    dtype = DatabaseVectorType(dialect_spec=spec)
    statement = sa.text(
        "SELECT 'first' AS label, '[1234.0]' AS embedding " "UNION ALL SELECT 'second', '[4321.0]' " "UNION ALL SELECT 'third', X'5B313233342E305D'"
    ).columns(embedding=dtype)
    with closing(sqlite3.connect(":memory:")) as raw:
        raw.text_factory = bytes
        engine = sa.create_engine("sqlite://", creator=lambda: raw)
        try:
            with engine.connect().execution_options(no_parameters=no_parameters) as conn:
                with conn.execute(statement) as result:
                    assert raw.text_factory is bytes
                    assert result.fetchone() == (b"first", [1234.0])
                    assert raw.text_factory is bytes
                    assert conn.exec_driver_sql("SELECT 'ordinary'").scalar_one() == b"ordinary"
                    assert result.fetchmany(1) == [(b"second", [4321.0])]
                    assert result.fetchall() == [(b"third", list(struct.unpack("<d", b"[1234.0]")))]
                unmatched = sa.text("SELECT 'ordinary' AS label, 1 AS id").columns(embedding=dtype)
                assert type(conn.execute(unmatched).scalar_one()) is bytes
                with pytest.raises(sa.exc.OperationalError, match="no such table"):
                    conn.execute(sa.text("SELECT embedding FROM missing_vectors").columns(embedding=dtype))
                assert raw.text_factory is bytes
        finally:
            engine.dispose()


@pytest.mark.parametrize("no_parameters", [False, True])
@pytest.mark.parametrize("listener_executes", [False, True])
def test_typed_text_preserves_sqlite_dialect_listeners(no_parameters, listener_executes):
    spec = replace(database_dialects.get("sqlite"), vector_packed_type="float64_blob")
    observed = []
    event = "do_execute_no_params" if no_parameters else "do_execute"
    sql = "SELECT '[1.25]' AS embedding"

    def listen(cursor, statement, *args):
        if statement != sql:
            return None
        observed.append(statement)
        if listener_executes:
            assert cursor.execute(statement, *args[:-1]) is cursor
            return True
        return None

    with closing(sqlite3.connect(":memory:")) as raw:
        raw.text_factory = bytes
        engine = sa.create_engine("sqlite://", creator=lambda: raw)
        sa.event.listen(engine, event, listen)
        try:
            with engine.connect().execution_options(no_parameters=no_parameters) as conn:
                result = conn.execute(sa.text(sql).columns(embedding=DatabaseVectorType(dialect_spec=spec)))
                assert observed == [sql]
                assert result.scalar_one() == [1.25]
        finally:
            engine.dispose()


def test_typed_text_keeps_nested_sqlite_function_result_types():
    spec = replace(database_dialects.get("sqlite"), vector_packed_type=None)
    categories = []
    with closing(sqlite3.connect(":memory:")) as raw:
        raw.text_factory = bytes
        engine = sa.create_engine("sqlite://", creator=lambda: raw)

        def lookup_embedding():
            category = raw.execute("SELECT 'books'").fetchone()[0]
            categories.append(category)
            return "[1.25]"

        raw.create_function("lookup_embedding", 0, lookup_embedding)
        try:
            sql = "WITH rows(id) AS (VALUES (1), (2)) SELECT lookup_embedding() AS embedding FROM rows"
            with engine.connect() as conn:
                result = conn.execute(sa.text(sql).columns(embedding=DatabaseVectorType(dialect_spec=spec)))
                assert result.scalars().all() == [[1.25], [1.25]]
                assert categories == [b"books", b"books"]
                assert all(type(category) is bytes for category in categories)
        finally:
            engine.dispose()
