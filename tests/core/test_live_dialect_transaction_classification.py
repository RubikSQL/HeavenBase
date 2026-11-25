"""Transaction classification follows the dialect that will execute the statement."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.compiler import compiles

import heavenbase as hb
from heavenbase.database import DatabaseError

pytestmark = [pytest.mark.fast, pytest.mark.unit]


class _VersionedSelect(sa.sql.Select):
    inherit_cache = False


@compiles(_VersionedSelect)
@compiles(_VersionedSelect, "sqlite")
def _compile_versioned_select(statement, compiler, **kwargs):
    if compiler.dialect.name != "sqlite" or compiler.dialect.server_version_info is not None:
        return "INSERT INTO items (id) VALUES (42) RETURNING id"
    return compiler.visit_select(statement, **kwargs)


class _DDLSelect(sa.sql.Select):
    inherit_cache = False


@compiles(_DDLSelect, "sqlite")
def _compile_ddl_select(statement, compiler, **kwargs):
    if compiler.dialect.server_version_info is not None:
        return "CREATE TABLE items (id INTEGER)"
    return compiler.visit_select(statement, **kwargs)


@pytest.mark.parametrize("transaction", [False, True])
def test_auto_readonly_commits_a_write_rendered_by_the_live_dialect(tmp_path, transaction):
    path = tmp_path / "classified.db"
    db = hb.Database(provider="sqlite", database=hb.utils.path_to_file_uri(path))
    db.execute("CREATE TABLE items (id INTEGER)")
    statement = _VersionedSelect(sa.literal(42).label("id"))
    try:
        if transaction:
            with db.tx():
                response = db.execute(statement, readonly=None)
        else:
            response = db.execute(statement, readonly=None)
        assert response.to_list() == [{"id": 42}]
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT id FROM items").fetchall() == [(42,)]
    finally:
        db.dispose()


def test_explicit_readonly_rejects_writes_rendered_by_the_live_dialect(tmp_path):
    db = hb.Database(provider="sqlite", database=hb.utils.path_to_file_uri(tmp_path / "guard.db"))
    db.execute("CREATE TABLE items (id INTEGER)")
    try:
        with pytest.raises(DatabaseError, match="readonly=True rejects mutating SQL"):
            db.execute(_VersionedSelect(sa.literal(42)), readonly=True).close()
    finally:
        db.dispose()


def test_schema_invalidation_uses_executed_sql_for_a_custom_construct(tmp_path):
    db = hb.Database(provider="sqlite", database=hb.utils.path_to_file_uri(tmp_path / "schema.db"))
    try:
        assert db.schema_index() == {}
        db.execute(_DDLSelect(sa.literal(1)), readonly=None)
        assert db.tables() == ["items"]
        assert db.schema_index() == {"items": ["id"]}
    finally:
        db.dispose()
