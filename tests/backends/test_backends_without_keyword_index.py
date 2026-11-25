"""Keyword queries on SQL routes that keep no keyword index: contained_in scans with the field's options and finds every stored row."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import heavenbase as hb
from heavenbase.execution.contracts import RowOp

_DEFAULT_POSTGRES_URL = "postgresql+psycopg2://rubik:password@localhost:5432/postgres?sslmode=prefer"


@pytest.fixture
def postgres_url():
    pytest.importorskip("psycopg2")
    pytest.importorskip("pgvector")
    root = make_url(os.environ.get("HVNB_POSTGRES_URL", _DEFAULT_POSTGRES_URL)).set(database="postgres")
    database = f"pgvector_keywords_{uuid4().hex}"
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


class Doc(hb.Entity):
    identifier = "pgvector-keyword-doc"
    title = hb.field(hb.ShortText).store(to="notes")
    tags = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


def _open(tmp_path, url):
    return hb.HeavenBase(
        url.database.replace("_", "-"),
        detached=True,
        backends={
            "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "system.db")},
            "notes": {"type": "pgvector", "url": url.render_as_string(hide_password=False)},
        },
    )


def _matches(ws, query):
    return sorted(ws.query(Doc).where(Doc.tags.contained_in(query)).execute().ids)


@pytest.mark.full
def test_contained_in_on_pgvector_finds_rows_that_no_keyword_index_saw(tmp_path, postgres_url):
    ws = _open(tmp_path, postgres_url)
    try:
        ws.register(Doc)
        ws.upsert(Doc, {"object_id": "d1", "title": "Invoice", "tags": ["invoice"]})
        assert _matches(ws, "please open the invoice and the memo") == ["d1"]
        # A writer that maintains no keyword index, as HeavenBase 0.1.2.2 on this route, stores a second row.
        ws.backends.get("notes").upsert([RowOp(Doc.identifier, "d2", {"title": "Memo", "tags": ["memo"]}, strategies=False)])
    finally:
        ws.release()
    ws = _open(tmp_path, postgres_url)
    try:
        ws.register(Doc)
        assert _matches(ws, "please open the invoice and the memo") == ["d1", "d2"]
        ws.register(Doc, allow_replace=True)
        assert _matches(ws, "open the memo") == ["d2"]
    finally:
        ws.release()


@pytest.mark.fast
@pytest.mark.parametrize("backend_cls", [hb.backends.StarRocksBackend, hb.backends.PgVectorBackend])
def test_starrocks_and_pgvector_keep_no_keyword_index(monkeypatch, backend_cls):
    if backend_cls is hb.backends.StarRocksBackend:
        monkeypatch.setattr(backend_cls, "_ensure_database", lambda *args: None)
    else:
        pytest.importorskip("pgvector")
    adapter = backend_cls("route").strategy_adapter(hb.SparseGramIndex)
    assert adapter is not None and adapter.maintains_index is False


class Ticket(hb.Entity):
    identifier = "keyword-options-ticket"
    title = hb.field(hb.ShortText).store(to="notes")
    codes = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2, casefold=False))


@pytest.mark.full
@pytest.mark.parametrize("route", ["postgres", "pgvector", "pgvector shared_database"])
def test_contained_in_matches_with_the_field_options_on_every_postgres_route(tmp_path, postgres_url, route):
    notes = {"type": route.split()[0], "url": postgres_url.render_as_string(hide_password=False)}
    if route.endswith("shared_database"):
        notes["shared_database"] = True
    ws = hb.HeavenBase(
        postgres_url.database.replace("_", "-"),
        detached=True,
        backends={"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "system.db")}, "notes": notes},
    )
    try:
        ws.register(Ticket)
        ws.upsert_many(Ticket, [{"object_id": "t1", "title": "Upper", "codes": ["INV"]}, {"object_id": "t2", "title": "Lower", "codes": ["inv"]}])
        # casefold=False: only the stored keyword written as in the query matches
        assert sorted(ws.query(Ticket).where(Ticket.codes.contained_in("ticket INV-7 is open")).execute().ids) == ["t1"]
    finally:
        ws.release()


def _sqlite_tickets(tmp_path, name="keyword-options-sqlite"):
    return hb.HeavenBase(
        name,
        detached=True,
        backends={
            "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "system.db")},
            "notes": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "notes.db")},
        },
    )


@pytest.mark.slow
@pytest.mark.parametrize("adapter", ["installed", "none"])
def test_contained_in_matches_with_the_field_options_on_a_route_with_or_without_the_keyword_adapter(tmp_path, monkeypatch, adapter):
    if adapter == "none":
        # a route that installs no keyword adapter, as a shared-database pgvector, Supabase or ClickHouse route does
        monkeypatch.setattr(hb.backends.SQLiteBackend, "_install_strategy_adapters", lambda self: self._strategy_adapters.clear())
    ws = _sqlite_tickets(tmp_path)
    try:
        ws.register(Ticket)
        assert (ws.backends.get("notes").strategy_adapter(hb.SparseGramIndex) is None) is (adapter == "none")
        ws.upsert_many(Ticket, [{"object_id": "t1", "title": "Upper", "codes": ["INV"]}, {"object_id": "t2", "title": "Lower", "codes": ["inv"]}])
        assert sorted(ws.query(Ticket).where(Ticket.codes.contained_in("ticket INV-7 is open")).execute().ids) == ["t1"]
    finally:
        ws.release()
