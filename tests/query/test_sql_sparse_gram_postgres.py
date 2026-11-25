"""Exercise overlapping shared sparse-GRAM index rebuilds on PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from threading import Event
from time import monotonic, sleep
from unittest.mock import Mock
from uuid import uuid4

import heavenbase as hb
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from heavenbase.backends.sql.strategies.sparse_gram import SQLSparseGramAdapter, _SQLGramIndex

_DEFAULT_POSTGRES_URL = "postgresql+psycopg2://rubik:password@localhost:5432/postgres?sslmode=prefer"


@pytest.fixture
def gram_postgres():
    pytest.importorskip("psycopg2")
    root = make_url(os.environ.get("HVNB_POSTGRES_URL", _DEFAULT_POSTGRES_URL)).set(database="postgres")
    database = f"hb_gram_registration_{uuid4().hex}"
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
            yield root.set(database=database), admin
        finally:
            with admin.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE {database} WITH (FORCE)")
    finally:
        admin.dispose()


@pytest.mark.fast
def test_sparse_gram_fixture_uses_standard_postgres_endpoint(monkeypatch, request):
    pytest.importorskip("psycopg2")
    monkeypatch.delenv("HVNB_POSTGRES_URL", raising=False)
    connection = Mock()
    admin = Mock()
    admin.connect.return_value = nullcontext(connection)
    mock_create_engine = Mock(return_value=admin)
    monkeypatch.setattr(__name__ + ".create_engine", mock_create_engine)

    try:
        database, monitor = request.getfixturevalue("gram_postgres")
    except pytest.skip.Exception:
        pytest.fail("The fixture skipped an available default PostgreSQL service", pytrace=False)

    [root] = mock_create_engine.call_args.args
    assert root == make_url("postgresql+psycopg2://rubik:password@localhost:5432/postgres?sslmode=prefer")
    assert database == root.set(database=database.database)
    assert monitor is admin
    connection.exec_driver_sql.assert_called_once_with(f"CREATE DATABASE {database.database}")


@pytest.mark.external
def test_shared_gram_registration_serializes_legacy_rebuilds(gram_postgres, monkeypatch, tmp_path):
    url, monitor = gram_postgres

    class Document(hb.Entity):
        identifier = "gram-concurrent-registration"
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="none"))

    def open_workspace(label, resources):
        context = hb.Context.load(
            {
                "version": 1,
                "root": str(tmp_path / label),
                "workspace": "default",
                "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
                "registry": {"hash": "sha256"},
            }
        )
        resources.callback(context.close)
        workspace = hb.HeavenBase(
            url.database.replace("_", "-"),
            context=context,
            detached=True,
            backends={"main": {"type": "postgres", "url": url.render_as_string(hide_password=False)}},
        )
        resources.callback(workspace.release)
        return workspace

    with ExitStack() as resources:
        # Each Context has its own workspace lock. PostgreSQL routes the same
        # workspace id in both Contexts to the same physical database.
        first = open_workspace("first", resources)
        second = open_workspace("second", resources)
        first.register(Document)
        first.upsert(Document, {"object_id": "x", "keywords": ["needle"]})
        backend_a = first.backends.get("main")
        backend_b = second.backends.get("main")
        assert backend_a.db.execute("SELECT current_database()", readonly=True).scalar() == url.database
        assert backend_b.db.execute("SELECT current_database()", readonly=True).scalar() == url.database
        adapter = backend_a.strategy_adapter(hb.SparseGramIndex)
        key = adapter._fingerprint_key(Document.identifier, "keywords")
        fingerprint = adapter._need_index().get_meta(key)
        for legacy in ("legacy-a", "legacy-b"):
            adapter._legacy_index(f"{legacy}:main:sql_sparse_gram").set_meta(key, fingerprint)

        deleted = Event()
        let_first_finish = Event()
        second_checked = Event()
        second_connections = []
        delete_postings = _SQLGramIndex._delete_postings
        ensure_fingerprint = SQLSparseGramAdapter._ensure_fingerprint

        def pause_after_delete(self, ids):
            delete_postings(self, ids)
            if self.backend is backend_a and self.namespace == "sql_sparse_gram":
                assert ids, "the first rebuild found no postings to delete"
                deleted.set()
                assert let_first_finish.wait(20)

        def record_second_connection(self, entity_id, field_name, config, **kwargs):
            if self.backend is backend_b and entity_id == Document.identifier:
                connection = self._need_index().db._conn
                second_connections.append(connection.connection.dbapi_connection.get_backend_pid())
                second_checked.set()
            return ensure_fingerprint(self, entity_id, field_name, config, **kwargs)

        monkeypatch.setattr(_SQLGramIndex, "_delete_postings", pause_after_delete)
        monkeypatch.setattr(SQLSparseGramAdapter, "_ensure_fingerprint", record_second_connection)

        with ThreadPoolExecutor(max_workers=2) as threads:
            registration_a = threads.submit(first.register, Document, allow_replace=True)
            try:
                assert deleted.wait(10), registration_a.exception() if registration_a.done() else "first rebuild did not delete postings"
                registration_b = threads.submit(second.register, Document)
                assert second_checked.wait(10), registration_b.exception() if registration_b.done() else "second registration did not reach GRAM"
                with monitor.connect() as connection:
                    deadline = monotonic() + 8
                    while not connection.execute(text("SELECT pg_blocking_pids(:pid)"), {"pid": second_connections[0]}).scalar_one():
                        if registration_b.done():
                            registration_b.result()
                            pytest.fail("the second registration finished before the first")
                        assert monotonic() < deadline, "the second registration never waited for the first"
                        sleep(0.02)
            finally:
                let_first_finish.set()
            registration_a.result(timeout=10)
            registration_b.result(timeout=10)

        for workspace in (first, second):
            assert workspace.query(Document).where(Document.keywords.contained_in("find the needle")).execute().ids == ["x"]


@pytest.mark.external
@pytest.mark.parametrize("second_route", ["same-pool", "separate-pool", "separate-schema"])
def test_split_gram_registration_and_writes_on_one_postgres_database(gram_postgres, second_route):
    url, _ = gram_postgres
    url = url.update_query_dict({"options": "-c lock_timeout=2500"})

    class Doc(hb.Entity):
        identifier = "gram-same-postgres-routes"
        primary_tags = hb.field(hb.Array[hb.ShortText]).required().store(to="main", strategy=hb.SparseGramIndex(gram_width=2))
        secondary_tags = hb.field(hb.Array[hb.ShortText]).required().store(to="secondary", strategy=hb.SparseGramIndex(gram_width=3))

    secondary = url
    if second_route == "separate-pool":
        secondary = url.update_query_dict({"application_name": "gram-secondary"})
    elif second_route == "separate-schema":
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("CREATE SCHEMA gram_secondary")
        finally:
            engine.dispose()
        secondary = url.update_query_dict({"options": "-c lock_timeout=2500 -c search_path=gram_secondary"})
    backends = {
        name: {"type": "postgres", "url": endpoint.render_as_string(hide_password=False)} for name, endpoint in (("main", url), ("secondary", secondary))
    }
    ws = hb.HeavenBase(url.database.replace("_", "-"), detached=True, backends=backends)
    try:
        main_db, secondary_db = (ws.backends.get(name).db for name in ("main", "secondary"))
        for db in (main_db, secondary_db):
            assert db.execute("SELECT current_database()", readonly=True).scalar() == url.database
        assert (main_db.engine is secondary_db.engine) == (second_route == "same-pool")
        ws.register(Doc)
        index_tables = [db.execute("SELECT to_regclass('hb_gram_meta')::oid", readonly=True).scalar() for db in (main_db, secondary_db)]
        assert all(index_tables)
        assert (index_tables[0] != index_tables[1]) == (second_route == "separate-schema")

        def matches(field, query):
            return ws.query(Doc).where(field.contained_in(query)).execute().ids

        ws.upsert(Doc, {"object_id": "invoice-104", "primary_tags": ["invoice"], "secondary_tags": ["finance"]})
        assert matches(Doc.primary_tags, "open invoice") == ["invoice-104"]
        assert matches(Doc.secondary_tags, "contact finance") == ["invoice-104"]
        ws.set("invoice-104", {"primary_tags": ["receipt"]}, entity=Doc)
        assert matches(Doc.primary_tags, "open invoice") == []
        assert matches(Doc.primary_tags, "open receipt") == ["invoice-104"]
        assert matches(Doc.secondary_tags, "contact finance") == ["invoice-104"]
        assert ws.delete((Doc, "invoice-104"))
        for name, field, query in (("main", "primary_tags", "open receipt"), ("secondary", "secondary_tags", "contact finance")):
            adapter = ws.backends.get(name).strategy_adapter(hb.SparseGramIndex)
            probe = adapter.contained_in_probe(Doc.identifier, field, query)
            assert probe["candidates"] == probe["exact_matches"] == 0
    finally:
        ws.release()
