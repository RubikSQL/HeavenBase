"""SQLite permits quoted names for inspection pragmas."""

import pytest

import heavenbase as hb
from heavenbase.database import DatabaseError

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("name", ['"table_info"', "`table_info`", "[TABLE_INFO]", "'table_info'"])
@pytest.mark.parametrize("prefix", [" ", "", '/* inspect */ "main".', '"main".'])
def test_sqlite_readonly_accepts_quoted_pragma_name(name, prefix):
    db = hb.Database(provider="sqlite", database=":memory:")
    try:
        db.execute("CREATE TABLE items (id INTEGER)")
        query = f"PRAGMA{prefix}{name}('items')"
        assert db.execute(query, readonly=True).column("name") == ["id"]
    finally:
        db.dispose()


@pytest.mark.parametrize("assignment", [' "user_version" = 5', '"user_version"(5)', '"optimize"'])
def test_sqlite_readonly_rejects_quoted_mutating_pragma(assignment):
    db = hb.Database(provider="sqlite", database=":memory:")
    try:
        with pytest.raises(DatabaseError, match="readonly=True"):
            db.execute(f"PRAGMA{assignment}", readonly=True)
    finally:
        db.dispose()
