"""A SQL batch upsert stores every row as a single-row upsert would, whatever fields each row names or how many rows it holds."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import heavenbase as hb

pytestmark = [pytest.mark.fast, pytest.mark.unit]


class SparseFieldDoc(hb.Entity):
    identifier = "sparse-field-doc"

    name = hb.field(hb.ShortText)
    n = hb.field(hb.Integer).store(optional=True)
    extra = hb.field(hb.Json).store(optional=True)
    embedding = hb.field(hb.Vector[3]).store(optional=True)
    tags = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)


BARE = {"object_id": "d0", "name": "a"}
FULL = {"object_id": "d1", "name": "b", "n": 5, "extra": {"k": 1}, "embedding": [1.0, 0.0, 0.0], "tags": ["x", "y"]}
PARTIAL = {"object_id": "d2", "name": "c", "extra": {"k": 2}}


def _stored(backends, rows, *, batched):
    ws = hb.HeavenBase("sql-upsert-batches", detached=True, backends=backends)
    try:
        ws.register(SparseFieldDoc)
        if batched:
            ws.upsert_many(SparseFieldDoc, rows)
        else:
            for row in rows:
                ws.upsert(SparseFieldDoc, row)
        stored = {row_id: ws.get(row_id, entity=SparseFieldDoc) for row_id in sorted(row["object_id"] for row in rows)}
        # an unset optional field reads back as None on SQL and is absent in memory
        return {row_id: {name: value for name, value in row.items() if value is not None} for row_id, row in stored.items()}
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("order", [(BARE, FULL, PARTIAL), (FULL, BARE, PARTIAL), (PARTIAL, FULL, BARE)])
def test_sql_upsert_many_keeps_fields_that_only_some_rows_set(tmp_path, order):
    sqlite = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "batches.db")}}
    rows = [dict(row) for row in order]

    batched = _stored(sqlite, rows, batched=True)

    # once dropped silently or refused with "A value is required for bind parameter", depending on row order
    assert batched == _stored({"main": {"type": "inmem"}}, rows, batched=True)
    assert batched == _stored({"main": {"type": "sqlite", "name": "main", "database": ":memory:"}}, rows, batched=False)
    assert batched["d1"]["n"] == 5 and batched["d1"]["extra"] == {"k": 1} and batched["d1"]["tags"] == ["x", "y"]
    assert batched["d1"]["embedding"] == pytest.approx([1.0, 0.0, 0.0]) and batched["d2"]["extra"] == {"k": 2}


class WideBatchDoc(hb.Entity):
    identifier = "wide-batch-doc"

    title = hb.field(hb.ShortText)


def _wide_workspace(tmp_path, name):
    database = hb.utils.path_to_file_uri(tmp_path / f"{name}.db")
    ws = hb.HeavenBase(name, detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": database}})
    ws.register(WideBatchDoc)
    return ws, ws.backends.get("main"), WideBatchDoc.schema().entity_id


def _record_id_deletes(backend, monkeypatch, *, fail_at=None):
    """Record how many object ids each ``DELETE ... IN`` binds; raise on the ``fail_at``-th one."""
    bound: list[int] = []
    real = backend.db.orm_execute

    def orm_execute(statement, params=None, **kwargs):
        if getattr(statement, "is_delete", False) and isinstance(params, dict) and "object_ids" in params:
            bound.append(len(params["object_ids"]))
            if fail_at is not None and len(bound) == fail_at:
                raise RuntimeError("delete failed")
        return real(statement, params, **kwargs)

    monkeypatch.setattr(backend.db, "orm_execute", orm_execute)
    return bound


def test_sql_upsert_and_delete_bind_at_most_one_id_batch_per_statement(tmp_path, monkeypatch):
    """A batch of any size deletes its ids in statements of at most one id batch, as ``get_many`` reads them."""
    ws, backend, entity_id = _wide_workspace(tmp_path, "sql-wide-batches")
    backend._sql_id_lookup_batch_size = 300
    bound = _record_id_deletes(backend, monkeypatch)
    rows = [{"object_id": f"r{index:04d}", "title": f"t{index}"} for index in range(1000)]
    try:
        backend.upsert([hb.ext.RowOp(entity_id, row["object_id"], dict(row)) for row in rows])
        # once one DELETE bound all 1,000 ids, past SQLite's variable limit and Oracle's IN-list limit at scale
        assert bound == [300, 300, 300, 100]
        assert backend.count(entity_id) == 1000
        del bound[:]
        backend.upsert([hb.ext.RowOp(entity_id, row["object_id"], op="delete") for row in rows])
        assert bound == [300, 300, 300, 100] and backend.count(entity_id) == 0
    finally:
        ws.release()


def test_sql_upsert_deletes_every_id_batch_inside_its_one_transaction(tmp_path, monkeypatch):
    """A failure in a later id batch rolls back the earlier ones: the upsert stays one transaction."""
    ws, backend, entity_id = _wide_workspace(tmp_path, "sql-wide-rollback")
    backend._sql_id_lookup_batch_size = 300
    rows = [{"object_id": f"r{index:04d}", "title": "old"} for index in range(700)]
    backend.upsert([hb.ext.RowOp(entity_id, row["object_id"], dict(row)) for row in rows])
    bound = _record_id_deletes(backend, monkeypatch, fail_at=3)
    try:
        with pytest.raises(Exception, match="delete failed"):
            backend.upsert([hb.ext.RowOp(entity_id, row["object_id"], {**row, "title": "new"}) for row in rows])
        assert bound == [300, 300, 100]
        stored = backend.get_many(entity_id, [row["object_id"] for row in rows], columns=["title"])
        assert [row["title"] for row in stored] == ["old"] * 700
    finally:
        ws.release()


@pytest.mark.parametrize(
    ("dialect", "configured", "expected"),
    [
        ("oracle", 5000, 1000),  # ORA-01795: an IN list holds at most 1,000 expressions
        ("mssql", 5000, 2000),  # SQL Server: at most 2,100 parameters per statement
        ("sqlite", 5000, 999),  # a driver that does not report its SQLite version: at most 999 variables
        ("postgresql", 50000, 32766),
        ("oracle", 500, 500),
        ("postgresql", 5000, 5000),
    ],
)
def test_sql_id_batches_never_exceed_the_dialect_limit(tmp_path, dialect, configured, expected):
    """The configured id batch size is capped by what one ``IN`` list may bind on the dialect."""
    backend = hb.backends.SQLBackend("main", provider="sqlite", database=hb.utils.path_to_file_uri(tmp_path / "limits.db"))
    backend._sql_id_lookup_batch_size = configured
    assert backend._id_batch_size(SimpleNamespace(spec=SimpleNamespace(dialect=dialect))) == expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [((3, 31, 1), 999), ((3, 32, 0), 32766), ((3, 50, 4), 32766)],
)
def test_sql_id_batches_follow_the_sqlite_librarys_variable_limit(tmp_path, version, expected):
    """The batching policy uses the default bind limit for the linked SQLite version."""
    backend = hb.backends.SQLBackend("main", provider="sqlite", database=hb.utils.path_to_file_uri(tmp_path / "limits.db"))
    backend._sql_id_lookup_batch_size = 50000
    engine = SimpleNamespace(dialect=SimpleNamespace(dbapi=SimpleNamespace(sqlite_version_info=version)))
    db = SimpleNamespace(spec=SimpleNamespace(dialect="sqlite"), engine=engine)
    assert backend._id_list_limit(db) == expected
    assert backend._id_batch_size(db) == expected
    # a statement binding other values besides its id list leaves room for them
    assert backend._id_batch_size(db, 7) == expected - 7


class GramBatchDoc(hb.Entity):
    identifier = "gram-batch-doc"

    title = hb.field(hb.ShortText)
    keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))


@pytest.mark.slow
@pytest.mark.parametrize("limit", [None, 1000, 2000], ids=["sqlite-999", "oracle-1000", "mssql-2000"])
def test_every_statement_of_a_large_sql_write_binds_within_the_dialect_limit(tmp_path, monkeypatch, limit):
    """Every statement an upsert_many, re-upsert, sparse-GRAM query or delete_many runs binds within the dialect's limit.

    The sparse-GRAM adapter runs in the same transaction as the row DELETE. It once bound up to
    ``posting_page_size`` (10,000) object ids or keyword ids in one ``IN`` list, so these writes failed on Oracle
    above 1,000 rows, on SQL Server above about 2,100 and on SQLite builds before 3.32 above 999, even for an
    entity with no sparse-GRAM field, whose catalog keywords go through the same adapter.
    """
    from sqlalchemy import event

    from heavenbase.backends.sql import base as sql_base

    # SQLite given the limit of SQLite before 3.32, of Oracle or of SQL Server
    cap = limit or 999
    monkeypatch.setattr(sql_base, "_sqlite_variable_limit", lambda db: cap)
    rows_count = cap + 50
    database = hb.utils.path_to_file_uri(tmp_path / "gram-batches.db")
    ws = hb.HeavenBase("sql-gram-batches", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": database}})
    bound: list[tuple[int, str]] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if not executemany:
            bound.append((len(parameters or ()), " ".join(statement.split())[:120]))

    backend, entity_id = ws.backends.get("main"), GramBatchDoc.schema().entity_id
    event.listen(backend.db.engine, "before_cursor_execute", record)
    try:
        ws.register(GramBatchDoc)
        rows = [{"object_id": f"r{index:05d}", "title": f"title {index}", "keywords": [f"alpha{index} beta{index}"]} for index in range(rows_count)]
        ws.upsert_many(GramBatchDoc, rows)
        ws.upsert_many(GramBatchDoc, [{**row, "keywords": [f"gamma{index} delta{index}"]} for index, row in enumerate(rows)])
        assert backend.count(entity_id) == rows_count
        text = " ".join(f"gamma{index} delta{index}" for index in range(rows_count))
        assert len(ws.query(GramBatchDoc).where(GramBatchDoc.keywords.contained_in(text)).execute().ids) == rows_count
        assert all(ws.delete_many([(GramBatchDoc, row["object_id"]) for row in rows]))
        assert backend.count(entity_id) == 0
        assert backend.strategy_adapter(hb.SparseGramIndex).posting_count(entity_id, "keywords") == 0
        # an entity with no sparse-GRAM field still writes its catalog keywords through the adapter
        ws.register(WideBatchDoc)
        plain = [{"object_id": f"p{index:05d}", "title": f"plain {index}"} for index in range(rows_count)]
        ws.upsert_many(WideBatchDoc, plain)
        ws.upsert_many(WideBatchDoc, [{**row, "title": "again"} for row in plain])
        assert all(ws.delete_many([(WideBatchDoc, row["object_id"]) for row in plain]))
        assert backend.count(WideBatchDoc.schema().entity_id) == 0
    finally:
        event.remove(backend.db.engine, "before_cursor_execute", record)
        ws.release()
    assert any("hb_gram" in statement for _, statement in bound)
    assert [entry for entry in bound if entry[0] > cap] == []


class SideProjectionDoc(hb.Entity):
    identifier = "side-projection-doc"

    title = hb.field(hb.ShortText)
    topics = hb.field(hb.HyperG["slot", "value", "topic"]).store(strategy=hb.SideTable, optional=True)
    tags = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)


@pytest.mark.slow
@pytest.mark.parametrize("limit", [999, 2000], ids=["sqlite-999", "mssql-2000"])
def test_get_many_projecting_side_tables_binds_within_the_dialect_limit(tmp_path, monkeypatch, limit):
    """A ``get_many`` id batch leaves room for the JSON key names a side-table projection binds.

    With ``id_lookup.batch_size`` configured at the dialect's limit, the id list once took the whole limit and the
    projection's key names went over it (``too many SQL variables`` on SQLite before 3.32).
    """
    from sqlalchemy import event

    from heavenbase.backends.sql import base as sql_base

    monkeypatch.setattr(sql_base, "_sqlite_variable_limit", lambda db: limit)
    database = hb.utils.path_to_file_uri(tmp_path / "side-projection.db")
    ws = hb.HeavenBase("sql-side-projection", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": database}})
    bound: list[tuple[int, str]] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if not executemany:
            bound.append((len(parameters or ()), " ".join(statement.split())[:120]))

    backend, entity_id = ws.backends.get("main"), SideProjectionDoc.schema().entity_id
    backend._sql_id_lookup_batch_size = limit
    try:
        ws.register(SideProjectionDoc)
        rows = [
            {"object_id": f"s{index:05d}", "title": f"t{index}", "topics": [{"slot": "a", "value": f"v{index}", "topic": "x"}], "tags": [f"g{index}"]}
            for index in range(limit + 50)
        ]
        ws.upsert_many(SideProjectionDoc, rows)
        event.listen(backend.db.engine, "before_cursor_execute", record)
        try:
            got = backend.get_many(entity_id, [row["object_id"] for row in rows])
            narrowed = backend.get_many(entity_id, [row["object_id"] for row in rows], columns=["object_id", "topics"])
        finally:
            event.remove(backend.db.engine, "before_cursor_execute", record)
    finally:
        ws.release()
    assert [row["tags"] for row in got] == [row["tags"] for row in rows]
    assert [row["topics"][0]["value"] for row in got] == [f"v{index}" for index in range(limit + 50)]
    assert [row["topics"][0]["value"] for row in narrowed] == [f"v{index}" for index in range(limit + 50)]
    assert len(bound) >= 4
    assert [entry for entry in bound if entry[0] > limit] == []


def test_sql_upsert_preserves_insert_order_across_optional_columns(tmp_path):
    ws, backend, entity_id = _wide_workspace(tmp_path, "sql-insert-order")
    try:
        table = backend.tables[entity_id].name
        backend.db.execute(f"""
            CREATE TRIGGER check_insert_order
            BEFORE INSERT ON "{table}"
            WHEN NEW.object_id = 'c'
                 AND NOT EXISTS (SELECT 1 FROM "{table}" WHERE object_id = 'b')
            BEGIN
                SELECT RAISE(ABORT, 'insert b before c');
            END
            """)
        backend.upsert([hb.ext.RowOp(entity_id, "a"), hb.ext.RowOp(entity_id, "b", {"title": "middle"}), hb.ext.RowOp(entity_id, "c")])
        assert [row["object_id"] for row in backend.scan(entity_id)] == ["a", "b", "c"]
    finally:
        ws.release()
