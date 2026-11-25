"""The keyword-index check of SQL writes: that it sends no query of its own while the index is unchanged, and that it still refuses writes that would leave an index stale."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import heavenbase as hb
from heavenbase.backends.sql.strategies.sparse_gram import SparseGramIndexConflictError
from heavenbase.execution.contracts import RowOp


class Customer(hb.Entity):
    identifier = "write-check-customer"
    name = hb.field(hb.ShortText).store(to="notes")


class LabelledCustomer(hb.Entity):
    """The same entity as another workspace defines it, with a keyword field it indexes."""

    identifier = "write-check-customer"
    name = hb.field(hb.ShortText).store(to="notes")
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


class Note(hb.Entity):
    identifier = "write-check-note"
    tags = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


def _workspace(tmp_path, name, *, keeps_index=True):
    ws = hb.HeavenBase(
        name,
        detached=True,
        backends={
            "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / f"{name}-system.db")},
            "notes": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "shared.db")},
        },
    )
    if not keeps_index:
        # a backend type that keeps no keyword index, as pgvector and StarRocks do
        ws.backends.get("notes")._keeps_keyword_index = False
    return ws


class _Lookups:
    """Count the keyword-index queries sent on their own through one backend's engine.

    A row statement that carries the check is not one of them.
    """

    def __init__(self, backend):
        self.engine = backend.db.engine
        self.statements = []

    def record(self, conn, cursor, statement, parameters, context, executemany):
        # PostgreSQL's table lookup binds the table name; SQLite's names it in a PRAGMA
        if statement.lstrip().upper().startswith(("SELECT", "PRAGMA")) and ("hb_gram" in statement or "hb_gram" in str(parameters)):
            self.statements.append(statement)

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self.record)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self.record)


_ROWS = [{"object_id": f"c{i}", "name": f"Customer {i}"} for i in range(5)]


@pytest.mark.slow
def test_writes_through_a_backend_that_keeps_no_keyword_index_send_no_lookup_of_their_own(tmp_path):
    ws = _workspace(tmp_path, "write-check-plain", keeps_index=False)
    try:
        ws.register(Customer)
        with _Lookups(ws.backends.get("notes")) as lookups:
            for row in _ROWS:
                ws.upsert(Customer, row)
            ws.upsert_many(Customer, [dict(row, name=row["name"] + " (updated)") for row in _ROWS])
            assert ws.delete((Customer, "c0"))
        assert lookups.statements == []
        assert ws.count(Customer) == 4
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("index_tables", ["absent", "present"])
def test_writes_through_an_indexing_backend_send_no_lookup_of_their_own(tmp_path, index_tables):
    ws = _workspace(tmp_path, f"write-check-indexing-{index_tables}")
    try:
        if index_tables == "present":
            ws.register(Note)
            ws.upsert(Note, {"object_id": "n1", "tags": ["invoice"]})
        ws.register(Customer)
        with _Lookups(ws.backends.get("notes")) as lookups:
            for row in _ROWS:
                ws.upsert(Customer, row)
            ws.upsert_many(Customer, [dict(row, name=row["name"] + " (updated)") for row in _ROWS])
        assert lookups.statements == []
    finally:
        ws.release()


@pytest.mark.slow
def test_write_calls_inside_one_transaction_send_no_lookup_of_their_own(tmp_path):
    ws = _workspace(tmp_path, "write-check-transaction")
    try:
        ws.register(Note)
        ws.register(Customer)
        backend = ws.backends.get("notes")
        with _Lookups(backend) as lookups:
            with backend.db.tx():
                for row in _ROWS:
                    backend.upsert([RowOp(Customer.identifier, row["object_id"], dict(row))])
        assert lookups.statements == []
        assert sorted(row["object_id"] for row in backend.scan(Customer.identifier)) == [row["object_id"] for row in _ROWS]
    finally:
        ws.release()


@pytest.mark.slow
def test_a_backend_that_keeps_no_keyword_index_checks_its_writes_once_it_registers_over_an_index(tmp_path):
    plain = _workspace(tmp_path, "write-check-unindexed", keeps_index=False)
    indexed = _workspace(tmp_path, "write-check-indexed")
    try:
        plain.register(Customer)
        indexed.register(LabelledCustomer)
        indexed.upsert(LabelledCustomer, {"object_id": "c1", "name": "Northwind", "labels": ["wholesale"]})
        # registered before any keyword index existed: the next registration notices the one created since
        plain.register(Customer, allow_replace=True)
        indexed.register(LabelledCustomer, allow_replace=True)
        with _Lookups(plain.backends.get("notes")) as lookups:
            with pytest.raises(SparseGramIndexConflictError, match="another one indexes the field"):
                plain.upsert(Customer, {"object_id": "c1", "name": "Northwind Traders"})
        assert len(lookups.statements) == 1
        assert indexed.get("c1", entity=LabelledCustomer)["labels"] == ["wholesale"]
    finally:
        indexed.release()
        plain.release()


class SplitCustomer(hb.Entity):
    identifier = "write-check-split-customer"
    name = hb.field(hb.ShortText).store(to="left")
    tags = hb.field(hb.Array[hb.ShortText]).store(to="right", strategy=hb.SparseGramIndex(gram_width=2))


class LabelledSplitCustomer(hb.Entity):
    """The split entity as another workspace defines it, on one route with one more keyword field."""

    identifier = "write-check-split-customer"
    name = hb.field(hb.ShortText).store(to="notes")
    tags = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


@pytest.mark.slow
@pytest.mark.parametrize("unindexed_route", ["left", "right"])
def test_split_routes_that_mix_a_backend_keeping_no_index_still_refuse_writes_that_would_leave_an_index_stale(tmp_path, unindexed_route):
    shared = hb.utils.path_to_file_uri(tmp_path / "shared.db")
    split = hb.HeavenBase(
        "write-check-split",
        detached=True,
        backends={
            "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "split-system.db")},
            "left": {"type": "sqlite", "database": shared},
            "right": {"type": "sqlite", "database": shared},
        },
    )
    indexed = _workspace(tmp_path, "write-check-split-indexed")
    try:
        split.backends.get(unindexed_route)._keeps_keyword_index = False
        split.register(SplitCustomer)
        assert split.backends.get("left")._row_write_routes(SplitCustomer.identifier) == ("left", "right")
        split.upsert(SplitCustomer, {"object_id": "c1", "name": "Northwind", "tags": ["wholesale"]})
        indexed.register(LabelledSplitCustomer)
        indexed.upsert(LabelledSplitCustomer, {"object_id": "c2", "name": "Contoso", "tags": ["retail"], "labels": ["priority"]})
        with pytest.raises(SparseGramIndexConflictError, match="another one indexes the field"):
            split.upsert(SplitCustomer, {"object_id": "c2", "name": "Contoso Ltd", "tags": ["retail"]})
        assert indexed.get("c2", entity=LabelledSplitCustomer)["labels"] == ["priority"]
    finally:
        indexed.release()
        split.release()


_DEFAULT_POSTGRES_URL = "postgresql+psycopg2://rubik:password@localhost:5432/postgres?sslmode=prefer"


@pytest.fixture
def postgres_url():
    pytest.importorskip("psycopg2")
    root = make_url(os.environ.get("HVNB_POSTGRES_URL", _DEFAULT_POSTGRES_URL)).set(database="postgres")
    database = f"write_checks_{uuid4().hex}"
    admin = create_engine(root, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
    try:
        try:
            connection = admin.connect()
        except OperationalError:
            if "HVNB_POSTGRES_URL" in os.environ:
                pytest.fail("Configured PostgreSQL test service is unavailable", pytrace=False)
            pytest.skip("Default PostgreSQL test service is unavailable")
        with connection as opened:
            opened.exec_driver_sql(f"CREATE DATABASE {database}")
        try:
            yield root.set(database=database)
        finally:
            with admin.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE {database} WITH (FORCE)")
    finally:
        admin.dispose()


class KeywordCustomer(hb.Entity):
    identifier = "write-check-keyword-customer"
    name = hb.field(hb.ShortText).store(to="notes")
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


@pytest.mark.full
@pytest.mark.parametrize(
    ("backend_type", "entity"),
    [("postgres", Customer), ("pgvector", Customer), ("pgvector", KeywordCustomer)],
    ids=["postgres", "pgvector", "pgvector with a keyword field"],
)
def test_postgres_write_calls_send_no_lookup_of_their_own(tmp_path, postgres_url, backend_type, entity):
    if backend_type == "pgvector":
        pytest.importorskip("pgvector")
    ws = hb.HeavenBase(
        postgres_url.database.replace("_", "-"),
        detached=True,
        backends={
            "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "system.db")},
            "notes": {"type": backend_type, "url": postgres_url.render_as_string(hide_password=False)},
        },
    )
    try:
        ws.register(entity)
        rows = [dict(row, labels=["wholesale"]) if entity is KeywordCustomer else row for row in _ROWS]
        with _Lookups(ws.backends.get("notes")) as lookups:
            for row in rows:
                ws.upsert(entity, row)
            ws.upsert_many(entity, rows)
        assert lookups.statements == []
    finally:
        ws.release()
