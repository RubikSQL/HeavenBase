"""Read-only SQLite inspection supports quoted schema names."""

import sys

import pytest

import heavenbase as hb
from heavenbase.database import DatabaseError, is_sql_readonly

pytestmark = [pytest.mark.fast, pytest.mark.unit]


@pytest.mark.parametrize("parser", [False, True])
@pytest.mark.parametrize("schema", ['"aux"', "`aux`", "[aux]", "'aux'", "[team's]", r'"team\ops"'])
def test_readonly_sqlite_inspection_accepts_quoted_schema(monkeypatch, parser, schema):
    if parser:
        pytest.importorskip("sqlglot")
    else:
        monkeypatch.setitem(sys.modules, "sqlglot", None)
    db = hb.Database(provider="sqlite", database=":memory:")
    try:
        with db.tx() as tx:
            tx.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
            tx.execute(f"CREATE TABLE {schema}.items (id INTEGER)")
            query = f"PRAGMA {schema}.table_info('items')"

            assert tx.execute(query, readonly=True).column("name") == ["id"]
            assert is_sql_readonly(query, dialect="sqlite")

            with pytest.raises(DatabaseError, match="readonly=True"):
                tx.execute(f"PRAGMA {schema}.user_version = 5", readonly=True)
    finally:
        db.dispose()
