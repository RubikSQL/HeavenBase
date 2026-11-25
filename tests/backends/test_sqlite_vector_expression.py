"""SQLite vector projections evaluate their input once."""

import sqlite3
from dataclasses import replace

import pytest
import sqlalchemy as sa

from heavenbase.database.dialect.registry import database_dialects
from heavenbase.database.types import DatabaseVectorType

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("storage", [None, "float64_blob"])
def test_computed_sqlite_vector_is_evaluated_once(storage):
    engine = sa.create_engine("sqlite://")
    calls = []

    def next_vector():
        calls.append(len(calls) + 1)
        return f"[{calls[-1]}]"

    try:
        with engine.connect() as conn:
            raw: sqlite3.Connection = conn.connection.driver_connection
            raw.create_function("next_vector", 0, next_vector)
            projected = []

            def row_factory(cursor, row):
                projected.append(row[0])
                return row

            raw.row_factory = row_factory
            spec = replace(database_dialects.get("sqlite"), vector_packed_type=storage) if storage else None
            value = sa.type_coerce(sa.func.next_vector(), DatabaseVectorType(dialect_spec=spec))
            assert conn.execute(sa.select(value)).scalar_one() == [1.0]
            assert calls == [1]
            assert projected == (["T[1]"] if storage else ["[1]"])
    finally:
        engine.dispose()
