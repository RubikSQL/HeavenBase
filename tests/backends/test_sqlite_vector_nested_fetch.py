"""A SQLite UDF sees original DBAPI results during typed vector fetches."""

import sqlite3

import pytest
import sqlalchemy as sa

from heavenbase.database.types import DatabaseVectorType

pytestmark = pytest.mark.fast


class InventoryCursor(sqlite3.Cursor):
    def execute(self, *args, **kwargs):
        self.connection.cursor_calls.append("execute")
        return super().execute(*args, **kwargs)

    def fetchone(self):
        self.connection.cursor_calls.append("fetchone")
        return super().fetchone()

    def fetchmany(self, *args):
        self.connection.cursor_calls.append("fetchmany")
        return super().fetchmany(*args)

    def fetchall(self):
        self.connection.cursor_calls.append("fetchall")
        return super().fetchall()


class InventoryConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cursor_calls = []

    def cursor(self):
        return super().cursor(factory=InventoryCursor)


@pytest.mark.parametrize("fetch", ["fetchall", "fetchmany", "fetchone"])
@pytest.mark.parametrize("custom_cursor", [False, True])
def test_typed_sqlite_vector_fetch_keeps_nested_query_values(fetch, custom_cursor):
    creator = {"creator": lambda: sqlite3.connect(":memory:", factory=InventoryConnection)} if custom_cursor else {}
    engine = sa.create_engine("sqlite://", **creator)
    nested_types = []

    try:
        with engine.connect() as conn:
            raw = conn.connection.driver_connection
            raw.text_factory = bytes

            def nested_vector(value):
                label = raw.execute("SELECT 'label'").fetchone()[0]
                nested_types.append(type(label))
                return f"[{value if label.decode() == 'label' else 0}]"

            raw.create_function("nested_vector", 1, nested_vector)
            statement = sa.text("SELECT nested_vector(1) AS v UNION ALL SELECT nested_vector(2) UNION ALL SELECT nested_vector(3)")
            if custom_cursor:
                raw.cursor_calls.clear()
            result = conn.execute(statement.columns(v=DatabaseVectorType()))
            if fetch == "fetchmany":
                assert result.fetchmany(2) + result.fetchmany(2) == [([1.0],), ([2.0],), ([3.0],)]
            elif fetch == "fetchone":
                assert [result.fetchone() for _ in range(3)] == [([1.0],), ([2.0],), ([3.0],)]
            else:
                assert result.fetchall() == [([1.0],), ([2.0],), ([3.0],)]
            assert nested_types == [bytes, bytes, bytes]
            assert raw.text_factory is bytes
            if custom_cursor:
                assert raw.cursor_calls[0] == "execute"
                assert fetch in raw.cursor_calls
    finally:
        engine.dispose()
