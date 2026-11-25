"""SQLite vector reads should preserve text-factory and row-factory output."""

import sqlite3
from contextlib import closing

import pytest
import sqlalchemy as sa

from heavenbase.database.dialect.spec import DatabaseDialectSpec
from heavenbase.database.types import DatabaseVectorType

pytestmark = [pytest.mark.fast, pytest.mark.unit]


@pytest.mark.parametrize("packed", [None, "float64_blob"])
@pytest.mark.parametrize("form", ["core", "typed-text"])
@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-16-be", "utf-16-le", "utf-32", "utf-32-be", "utf-32-le"])
def test_sqlite_vector_reads_encoded_json_from_a_text_factory(packed, form, encoding):
    with closing(sqlite3.connect(":memory:")) as raw:
        raw.execute("CREATE TABLE vectors (id INTEGER PRIMARY KEY, embedding TEXT)")
        raw.execute("INSERT INTO vectors VALUES (?, ?)", (1, "[1.25,2.5]"))
        raw.commit()
        raw.text_factory = lambda value: value.decode("utf-8").encode(encoding)
        engine = sa.create_engine("sqlite://", creator=lambda: raw)
        try:
            spec = DatabaseDialectSpec(identifier="sqlite", tags={"database-dynamic-typing": True}, vector_packed_type=packed)
            dtype = DatabaseVectorType(dialect_spec=spec)
            vectors = sa.table("vectors", sa.column("id", sa.Integer), sa.column("embedding", dtype))
            if form == "core":
                statement = sa.select(vectors.c.embedding).order_by(vectors.c.id)
            else:
                statement = sa.text("SELECT embedding FROM vectors ORDER BY id").columns(embedding=dtype)
            with engine.begin() as conn:
                conn.execute(vectors.insert().values(id=2, embedding=[3.5, 4.75]))
                assert conn.execute(statement).scalars().all() == [[1.25, 2.5], [3.5, 4.75]]
        finally:
            engine.dispose()


def test_sqlite_typed_vector_preserves_the_row_factory_result():
    def decode_byte_text(cursor, row):
        if cursor.connection.text_factory is bytes:
            return tuple(value.decode("utf-8") if isinstance(value, bytes) else value for value in row)
        return row

    with closing(sqlite3.connect(":memory:")) as raw:
        raw.text_factory = bytes
        raw.row_factory = decode_byte_text
        engine = sa.create_engine("sqlite://", creator=lambda: raw)
        try:
            dtype = DatabaseVectorType(dialect_spec=DatabaseDialectSpec(identifier="sqlite"))
            statement = sa.text("SELECT :value AS vec, :label AS label").columns(vec=dtype)
            with engine.connect() as conn:
                assert conn.execute(statement, {"value": "[1234.0]", "label": "ready"}).one() == ([1234.0], "ready")
        finally:
            engine.dispose()
