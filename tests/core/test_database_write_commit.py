"""A write run outside ``db.tx()`` commits before ``execute`` returns, even when its cursor reports rows."""

from __future__ import annotations

import gc

import pytest
import sqlalchemy as sa

import heavenbase as hb
from heavenbase.database import DatabaseError

pytestmark = [pytest.mark.fast, pytest.mark.unit]


def _sqlite(tmp_path, name: str) -> hb.Database:
    return hb.Database(provider="sqlite", database=hb.utils.path_to_file_uri(tmp_path / name))


def test_a_discarded_returning_write_is_committed(tmp_path):
    db = _sqlite(tmp_path, "returning.db")
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    items = sa.Table("items", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.Text))

    db.execute("INSERT INTO items (id, name) VALUES (1, 'alpha') RETURNING id")  # response dropped unread
    db.orm_execute(sa.insert(items).values(id=2, name="beta").returning(items.c.id))
    kept = db.execute("INSERT INTO items (id, name) VALUES (3, 'gamma') RETURNING id, name")

    # rows a write reports are already cached, and its transaction is over
    assert kept.to_dicts() == [{"id": 3, "name": "gamma"}]
    assert _sqlite(tmp_path, "returning.db").execute("SELECT id FROM items ORDER BY id", readonly=True).column(0) == [1, 2, 3]


@pytest.mark.slow
@pytest.mark.parametrize("safe", [False, True])
def test_a_failed_write_commits_none_of_its_parameter_sets(tmp_path, safe):
    db = _sqlite(tmp_path, "failed.db")
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    batch = [{"id": 1, "name": "alpha"}, {"id": 1, "name": "beta"}]  # sqlite3 applies the first set before the second fails

    if safe:
        assert db.execute("INSERT INTO items (id, name) VALUES (:id, :name)", batch, safe=True).ok is False
    else:
        with pytest.raises(DatabaseError):
            db.execute("INSERT INTO items (id, name) VALUES (:id, :name)", batch)

    assert _sqlite(tmp_path, "failed.db").execute("SELECT id FROM items", readonly=True).column(0) == []
    db.execute("INSERT INTO items (id, name) VALUES (:id, :name)", batch[:1])  # the connection is usable afterwards
    assert _sqlite(tmp_path, "failed.db").execute("SELECT name FROM items", readonly=True).column(0) == ["alpha"]


def test_a_read_still_streams_lazily_outside_a_transaction(tmp_path):
    db = _sqlite(tmp_path, "lazy.db")
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO items (id) VALUES (1), (2), (3)")

    response = db.execute("SELECT id FROM items ORDER BY id")  # readonly=False by default

    assert response._close is not None  # the connection stays open for the lazy cursor
    assert response.column(0) == [1, 2, 3]
    assert response._close is None
    # a verb inside a string literal does not make a read a write
    response = db.execute("SELECT replace('abc', 'a', 'z') AS n, 'delete' AS verb FROM items")
    assert response._close is not None
    assert response.column(1) == ["delete"] * 3


@pytest.mark.parametrize(
    ("statement", "read"),
    [
        ("SELECT id FROM items", True),
        ("/* insert */ select id, comment from items", True),
        ("WITH s AS (SELECT id FROM items) SELECT id FROM s", True),
        ("VALUES (1)", True),
        ("EXPLAIN SELECT 1", True),
        (sa.select(sa.column("id")).select_from(sa.table("items")), True),
        ("WITH s AS (SELECT 9 AS id) INSERT INTO items SELECT id FROM s", False),
        ("SELECT id INTO copy_items FROM items", False),
        ("EXPLAIN ANALYZE DELETE FROM items", False),
        ("COPY items FROM 'items.csv'", False),
        ("COMMENT ON TABLE items IS 'x'", False),
        ("PRAGMA user_version = 5", False),
        ("INSERT INTO items (id) VALUES (8) RETURNING id", False),
        (sa.insert(sa.table("items", sa.column("id"))).values(id=1), False),
        # quoted text and identifiers name no verb; insert( and replace( are string functions
        ("SELECT replace(name, 'a', 'b') AS n FROM items", True),
        ("SELECT id FROM items WHERE note = 'delete' OR note LIKE '%insert%'", True),
        ('SELECT "update", `set` FROM items -- drop\n', True),
        ("SELECT 'it''s', insert('abc', 1, 1, 'x');", True),
        # a second statement, or a write hidden from a quote-unaware reader
        ("SELECT 1; COMMENT ON TABLE items IS 'x'", False),
        ("SELECT 1; COPY items FROM 'f.csv'", False),
        ("WITH s AS (SELECT '--' AS c, 5 AS id) INSERT INTO items SELECT id FROM s", False),
        ("SELECT '/*' AS c; INSERT INTO items VALUES (1) -- */", False),
        ("SELECT 1 DELETE FROM items", False),
        ("EXPLAIN ANALYZE SELECT 1", False),
        # text that dialects split into statements differently
        ("SELECT 1 /* a /* b */ c */; DELETE FROM items", False),
        ("SELECT 1 # it's\n; DELETE FROM items; SELECT '", False),
        ("SELECT [it's]; DELETE FROM items; SELECT [x's]", False),
        ("SELECT 'it\\'; DELETE FROM items; SELECT '", False),
        ("SELECT 1 --x\n; DELETE FROM items", False),
        ("SELECT $$a$$; DELETE FROM items", False),
        ("SELECT 1 /*! ; DELETE FROM items */", False),
    ],
)
def test_only_a_statement_known_to_be_a_read_is_classified_as_one(statement, read):
    from heavenbase.database.readonly import _is_sql_read

    assert _is_sql_read(statement) is read


def test_a_write_led_by_with_is_cached_and_committed(tmp_path):
    db = _sqlite(tmp_path, "with.db")
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")

    response = db.execute("WITH s AS (SELECT 9 AS id) INSERT INTO items SELECT id FROM s RETURNING id")

    assert response._close is None  # no transaction is left waiting for the caller
    assert response.column(0) == [9]
    assert _sqlite(tmp_path, "with.db").execute("SELECT id FROM items", readonly=True).column(0) == [9]


@pytest.fixture
def duckdb_backend(tmp_path):
    pytest.importorskip("duckdb_engine")
    return hb.backends.DuckDBBackend("main", hb.utils.path_to_file_uri(tmp_path / "commit.duckdb"))


def test_duckdb_ddl_and_dml_run_outside_a_transaction_are_committed(duckdb_backend):
    db = duckdb_backend.db
    # DuckDB's driver reports rows for every statement, DDL included
    db.execute("CREATE TABLE items (id INTEGER)")
    db.execute("INSERT INTO items VALUES (1)")
    db.execute("ALTER TABLE items ADD COLUMN name VARCHAR")
    db.execute("UPDATE items SET name = 'alpha'")
    db.add_table_col(sa.Table("items", sa.MetaData(), sa.Column("id", sa.Integer)), sa.Column("extra", sa.Integer))

    assert list(db.columns("items")) == ["id", "name", "extra"]
    assert db.execute("SELECT id, name FROM items", readonly=True).to_dicts() == [{"id": 1, "name": "alpha"}]


class DuckSideDoc(hb.Entity):
    identifier = "duck-side-doc"

    title = hb.field(hb.ShortText)
    tags = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)


def test_duckdb_writes_and_reads_an_entity_with_a_side_table_field(duckdb_backend):
    ws = hb.HeavenBase("duckdb-side-table-commit", detached=True, backends={"main": duckdb_backend})
    try:
        ws.register(DuckSideDoc)
        ws.upsert_many(DuckSideDoc, [{"object_id": "a", "title": "t", "tags": ["x", "y"]}, {"object_id": "b", "title": "u", "tags": None}])

        assert "_present_tags" in duckdb_backend.db.columns(duckdb_backend.tables[DuckSideDoc.identifier].name)
        assert [(row["object_id"], row["tags"]) for row in ws.get_many(["a", "b"], entity=DuckSideDoc)] == [("a", ["x", "y"]), ("b", None)]
        assert [row["object_id"] for row in ws.iter_rows(DuckSideDoc, batch_size=1)] == ["a", "b"]
    finally:
        ws.release()


class DuckArrayDoc(hb.Entity):
    identifier = "duck-array-doc"

    title = hb.field(hb.ShortText).optional()
    count = hb.field(hb.Integer).optional()
    items = hb.field(hb.Array[hb.Integer]).optional()
    tags = hb.field(hb.Array[hb.ShortText]).optional()


@pytest.mark.parametrize("where", ["memory", "file"])
def test_duckdb_writes_an_entity_with_optional_array_fields_beside_indexed_scalars(tmp_path, where):
    """An Array[Integer] field, which SQL placement puts in a side table, adds a presence column beside indexed scalars.

    The Array[ShortText] field stays in the main table, so the entity mixes both placements.
    """
    pytest.importorskip("duckdb_engine")
    database = ":memory:" if where == "memory" else hb.utils.path_to_file_uri(tmp_path / "array.duckdb")
    rows = [
        {"object_id": "a", "title": "t", "count": 2, "items": [1, 2], "tags": ["x"]},
        {"object_id": "b", "title": None, "count": None, "items": None, "tags": []},
    ]
    expected = [
        {"object_id": "a", "title": "t", "count": 2, "items": [1, 2], "tags": ["x"]},
        {"object_id": "b", "title": None, "count": None, "items": None, "tags": []},
    ]
    ws = hb.HeavenBase(f"duckdb-array-{where}", detached=True, backends={"main": hb.backends.DuckDBBackend("main", database)})
    try:
        ws.register(DuckArrayDoc)
        ws.upsert_many(DuckArrayDoc, rows)
        assert ws.get_many(["a", "b"], entity=DuckArrayDoc) == expected
    finally:
        ws.release()
    if where == "memory":
        return
    reopened = hb.HeavenBase("duckdb-array-reopened", detached=True, backends={"main": hb.backends.DuckDBBackend("main", database)})
    try:
        reopened.register(DuckArrayDoc)
        reopened.upsert(DuckArrayDoc, {"object_id": "c", "items": [3]})
        assert reopened.get_many(["a", "b", "c"], entity=DuckArrayDoc) == [
            *expected,
            {"object_id": "c", "title": None, "count": None, "items": [3], "tags": None},
        ]
    finally:
        reopened.release()


def test_duckdb_writes_led_by_other_keywords_are_committed(duckdb_backend, tmp_path):
    db = duckdb_backend.db
    db.execute("CREATE TABLE items (id INTEGER)")
    db.execute("INSERT INTO items VALUES (1)")
    source = tmp_path / "items.csv"
    source.write_text("id\n7\n")
    # each response is dropped unread; DuckDB reports rows for every one of these statements
    db.execute("WITH s AS (SELECT 2 AS id) INSERT INTO items SELECT id FROM s")
    db.execute(f"COPY items FROM '{source.as_posix()}' (HEADER)")
    db.execute("COMMENT ON TABLE items IS 'kept'")
    db.execute("/* note */ INSERT INTO items VALUES (3)")
    db.execute("WITH s AS (SELECT 4 AS id) INSERT INTO items SELECT id FROM s RETURNING id")
    # a write behind a read lead: a second statement, or one a literal would hide from a comment stripper
    second = tmp_path / "more.csv"
    second.write_text("id\n8\n")
    db.execute("SELECT 1; INSERT INTO items VALUES (5)")
    db.execute("WITH s AS (SELECT '--' AS c, 6 AS id) INSERT INTO items SELECT id FROM s")
    db.execute(f"SELECT 1; COPY items FROM '{second.as_posix()}' (HEADER)")
    db.execute("SELECT 1; COMMENT ON TABLE items IS 'multi'")
    gc.collect()

    fresh = hb.backends.DuckDBBackend("check", hb.utils.path_to_file_uri(tmp_path / "commit.duckdb")).db
    assert fresh.execute("SELECT id FROM items ORDER BY id", readonly=True).column(0) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert fresh.execute("SELECT comment FROM duckdb_tables() WHERE table_name = 'items'", readonly=True).column(0) == ["multi"]
