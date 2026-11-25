"""SQL routes that share one database write the fields they split of one row together."""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import heavenbase as hb
from heavenbase.backends.sql.base import SQLBackend


def _workspace(tmp_path, name, backend_type, *, secondary_file="shared.db"):
    if backend_type == "duckdb":
        pytest.importorskip("duckdb_engine")
    return hb.HeavenBase(
        name,
        detached=True,
        backends={
            "main": {"type": backend_type, "database": hb.utils.path_to_file_uri(tmp_path / "shared.db")},
            "secondary": {"type": backend_type, "database": hb.utils.path_to_file_uri(tmp_path / secondary_file)},
        },
    )


@pytest.mark.slow
@pytest.mark.parametrize("backend_type", ["sqlite", "duckdb"])
@pytest.mark.parametrize("secondary_file", ["shared.db", "separate.db"])
def test_split_fields_on_file_routes_keep_every_fragment(tmp_path, backend_type, secondary_file):
    class Note(hb.Entity):
        identifier = "r10sql-split-note"
        title = hb.field(hb.ShortText).store(to="main")
        body = hb.field(hb.ShortText).store(to="secondary")
        tags = hb.field(hb.Array[hb.ShortText]).store(to="secondary", strategy=hb.SideTable)

    ws = _workspace(tmp_path, "r10sql-split-plain", backend_type, secondary_file=secondary_file)
    try:
        ws.register(Note)
        ws.upsert(Note, {"object_id": "n1", "title": "Quarterly report", "body": "Revenue grew in the third quarter.", "tags": ["finance", "q3"]})
        ws.upsert(Note, {"object_id": "n2", "title": "Team offsite", "body": "The offsite moves to May."})
        ws.set("n1", {"title": "Quarterly report (final)"}, entity=Note)
        assert {key: ws.get("n1", entity=Note)[key] for key in ("title", "body", "tags")} == {
            "title": "Quarterly report (final)",
            "body": "Revenue grew in the third quarter.",
            "tags": ["finance", "q3"],
        }
        assert ws.get("n2", entity=Note)["body"] == "The offsite moves to May."
        assert ws.delete((Note, "n2"))
        assert ws.get("n2", entity=Note) is None
        assert ws.get("n1", entity=Note)["title"] == "Quarterly report (final)"
    finally:
        ws.release()


@pytest.mark.fast
def test_split_required_fields_on_one_sqlite_file(tmp_path):
    class Ticket(hb.Entity):
        identifier = "r10sql-split-required-ticket"
        subject = hb.field(hb.ShortText).required().store(to="main")
        queue = hb.field(hb.ShortText).required().store(to="secondary")

    ws = _workspace(tmp_path, "r10sql-split-required", "sqlite")
    try:
        ws.register(Ticket)
        ws.upsert(Ticket, {"object_id": "t1", "subject": "Printer offline", "queue": "facilities"})
        assert {key: ws.get("t1", entity=Ticket)[key] for key in ("subject", "queue")} == {"subject": "Printer offline", "queue": "facilities"}
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("backend_type", ["sqlite", "duckdb"])
def test_split_sparse_gram_fields_on_one_file(tmp_path, backend_type):
    class Doc(hb.Entity):
        identifier = "r10sql-split-gram-doc"
        primary_tags = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(gram_width=2))
        secondary_tags = hb.field(hb.Array[hb.ShortText]).store(to="secondary", strategy=hb.SparseGramIndex(gram_width=3))

    ws = _workspace(tmp_path, "r10sql-split-gram", backend_type)
    try:
        ws.register(Doc)

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
            probe = ws.backends.get(name).strategy_adapter(hb.SparseGramIndex).contained_in_probe(Doc.identifier, field, query)
            assert probe["candidates"] == probe["exact_matches"] == 0
    finally:
        ws.release()


class _InlineSideTableSQLite(hb.backends.SQLiteBackend):
    """Keeps ``SideTable`` fields in its main table, as the pgvector backend does."""

    def _side_table_fields(self, bindings):
        return []

    def _main_table_fields(self, bindings):
        return list(self._my_bindings(bindings))


@pytest.mark.fast
def test_split_row_keeps_a_side_table_field_that_its_route_stores_inline(tmp_path):
    class Note(hb.Entity):
        identifier = "r10sql-split-inline-side"
        title = hb.field(hb.ShortText).store(to="main")
        body = hb.field(hb.ShortText).store(to="secondary")
        tags = hb.field(hb.Array[hb.ShortText]).store(to="secondary", strategy=hb.SideTable)

    database = hb.utils.path_to_file_uri(tmp_path / "shared.db")
    backends = {"main": hb.backends.SQLiteBackend("main", database), "secondary": _InlineSideTableSQLite("secondary", database)}
    ws = hb.HeavenBase("r10sql-split-inline-side", detached=True, backends=backends)
    try:
        ws.register(Note)
        assert "tags" in ws.backends.get("secondary").tables[Note.identifier].c
        ws.upsert(Note, {"object_id": "n1", "title": "Quarterly report", "body": "Revenue grew.", "tags": ["finance", "q3"]})
        assert {key: ws.get("n1", entity=Note)[key] for key in ("title", "body", "tags")} == {
            "title": "Quarterly report",
            "body": "Revenue grew.",
            "tags": ["finance", "q3"],
        }
    finally:
        ws.release()


@pytest.mark.fast
def test_a_failed_shared_database_check_fails_the_registration(tmp_path, monkeypatch):
    class Note(hb.Entity):
        identifier = "r10sql-split-failed-check"
        title = hb.field(hb.ShortText).store(to="main")
        body = hb.field(hb.ShortText).store(to="secondary")

    ws = _workspace(tmp_path, "r10sql-split-failed-check", "sqlite")
    try:
        main = ws.backends.get("main")

        def unreachable(peers, **options):
            raise OperationalError("SELECT 1", {}, ConnectionError("server closed the connection"))

        monkeypatch.setattr(main, "_registration_aliases", unreachable)
        with pytest.raises(RuntimeError, match="Cannot tell whether SQL route 'main' shares its database with 'secondary'"):
            ws.register(Note)
        assert all(Note.identifier not in ws.backends.get(name)._shared_row_storage for name in ("main", "secondary"))
        monkeypatch.undo()
        ws.register(Note)
        ws.upsert(Note, {"object_id": "n1", "title": "Quarterly report", "body": "Revenue grew."})
        assert {key: ws.get("n1", entity=Note)[key] for key in ("title", "body")} == {"title": "Quarterly report", "body": "Revenue grew."}
    finally:
        ws.release()


class _ProbeRoute:
    """A MySQL route reduced to the parts its shared-database check uses."""

    def __init__(self, name, engine):
        self.name = name
        self.db = SimpleNamespace(engine=engine)

    def _mysql_registration_aliases(self, peers):
        return SQLBackend._mysql_registration_aliases(self, peers)

    def _alias_check_error(self, peers, exc):
        return SQLBackend._alias_check_error(self, peers, exc)

    def _probe_registration_aliases(self, peers, table):
        # the table check, which a real route runs against its server, answers for every peer it is asked about
        self.probed = list(peers)
        return list(peers)


class _FailingEngine:
    def __init__(self, error):
        self.error = error

    def connect(self):
        engine = self

        class _Connection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, *args, **kwargs):
                raise engine.error

        return _Connection()


@pytest.mark.fast
def test_mysql_shared_database_check_fails_instead_of_guessing():
    lost = OperationalError("SELECT GET_LOCK(%(name)s, 0)", {}, ConnectionError(2013, "Lost connection to MySQL server during query"))
    route = _ProbeRoute("main", _FailingEngine(lost))
    with pytest.raises(RuntimeError, match="Cannot tell whether SQL route 'main' shares its database with 'secondary'"):
        route._mysql_registration_aliases([_ProbeRoute("secondary", _FailingEngine(lost))])


@pytest.mark.fast
def test_mysql_server_without_named_locks_asks_the_other_engines_through_the_table_check():
    missing = OperationalError("SELECT GET_LOCK(%(name)s, 0)", {}, ConnectionError(1305, "FUNCTION GET_LOCK does not exist"))
    engine = _FailingEngine(missing)
    route = _ProbeRoute("main", engine)
    same_engine = _ProbeRoute("replica", engine)
    other_engine = _ProbeRoute("secondary", _FailingEngine(missing))
    assert route._mysql_registration_aliases([same_engine, other_engine]) == [same_engine, other_engine]
    assert route.probed == [other_engine]


_DEFAULT_POSTGRES_URL = "postgresql+psycopg2://rubik:password@localhost:5432/postgres?sslmode=prefer"


@pytest.fixture
def split_postgres():
    pytest.importorskip("psycopg2")
    root = make_url(os.environ.get("HVNB_POSTGRES_URL", _DEFAULT_POSTGRES_URL)).set(database="postgres")
    database = f"r10sql_split_{uuid4().hex}"
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


@pytest.mark.full
@pytest.mark.parametrize("secondary_type", ["postgres", "pgvector"])
@pytest.mark.parametrize("second_route", ["same-url", "separate-pool"])
def test_split_routes_on_one_postgres_database_keep_every_fragment(split_postgres, secondary_type, second_route):
    if secondary_type == "pgvector":
        pytest.importorskip("pgvector")

    class Note(hb.Entity):
        identifier = "r10sql-split-postgres-note"
        title = hb.field(hb.ShortText).store(to="main")
        body = hb.field(hb.ShortText).store(to="secondary")
        tags = hb.field(hb.Array[hb.ShortText]).store(to="secondary", strategy=hb.SideTable)

    url = split_postgres
    secondary = url if second_route == "same-url" else url.update_query_dict({"application_name": "r10sql-secondary"})
    backends = {
        "main": {"type": "postgres", "url": url.render_as_string(hide_password=False)},
        "secondary": {"type": secondary_type, "url": secondary.render_as_string(hide_password=False)},
    }
    ws = hb.HeavenBase(url.database.replace("_", "-"), detached=True, backends=backends)
    try:
        ws.register(Note)
        ws.upsert(Note, {"object_id": "n1", "title": "Quarterly report", "body": "Revenue grew.", "tags": ["finance", "q3"]})
        ws.set("n1", {"title": "Quarterly report (final)"}, entity=Note)
        assert {key: ws.get("n1", entity=Note)[key] for key in ("title", "body", "tags")} == {
            "title": "Quarterly report (final)",
            "body": "Revenue grew.",
            "tags": ["finance", "q3"],
        }
        assert ws.delete((Note, "n1"))
        assert ws.get("n1", entity=Note) is None
    finally:
        ws.release()


@pytest.mark.slow
def test_a_failed_publication_check_fails_the_registration_with_a_runtime_error(tmp_path, monkeypatch):
    class Doc(hb.Entity):
        identifier = "split-failed-publication-check"
        title = hb.field(hb.ShortText).store(to="main")
        tags = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(gram_width=2))
        body = hb.field(hb.ShortText).store(to="secondary")

    ws = _workspace(tmp_path, "split-failed-publication-check", "sqlite")
    try:
        main = ws.backends.get("main")
        check = main._registration_aliases

        def unreachable_for_publication(peers, *, registration=None, compatible=False, table=None):
            # The row-sharing check answers; the server stops answering before the sparse-GRAM publication check.
            if registration is not None:
                raise OperationalError("SELECT pg_advisory_xact_lock(1, 2)", {}, ConnectionError("server closed the connection"))
            return check(peers, registration=registration, compatible=compatible, table=table)

        monkeypatch.setattr(main, "_registration_aliases", unreachable_for_publication)
        with pytest.raises(RuntimeError, match="Cannot tell whether SQL route 'main' shares its database with 'secondary'") as raised:
            ws.register(Doc)
        assert isinstance(raised.value.__cause__, OperationalError)
        monkeypatch.undo()
        ws.register(Doc)
        ws.upsert(Doc, {"object_id": "d1", "title": "Invoice 104", "tags": ["invoice"], "body": "Paid in full."})
        assert ws.query(Doc).where(Doc.tags.contained_in("open invoice")).execute().ids == ["d1"]
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize(
    "pool",
    [
        {"pool_class": "queue", "pool_size": 1, "max_overflow": 0, "pool_timeout": 3},
        {"pool_class": "assertion"},
    ],
    ids=["queue of one", "assertion"],
)
@pytest.mark.parametrize("tags_on", ["left", "right"])
def test_split_keyword_field_on_a_later_route_writes_through_a_one_connection_first_route(tmp_path, pool, tags_on):
    class Doc(hb.Entity):
        identifier = "split-one-connection-gram-doc"
        title = hb.field(hb.ShortText).store(to="right" if tags_on == "left" else "left")
        tags = hb.field(hb.Array[hb.ShortText]).store(to=tags_on, strategy=hb.SparseGramIndex(gram_width=2))

    shared = hb.utils.path_to_file_uri(tmp_path / "shared.db")
    ws = hb.HeavenBase(
        "split-one-connection-gram",
        detached=True,
        backends={
            "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "system.db")},
            "left": {"type": "sqlite", "database": shared, "pool": dict(pool)},
            "right": {"type": "sqlite", "database": shared, "pool": {"pool_size": 2}},
        },
    )
    try:
        ws.register(Doc)
        assert ws.backends.get("left")._row_write_routes(Doc.identifier) == ("left", "right")
        ws.upsert(Doc, {"object_id": "invoice-104", "title": "Invoice 104", "tags": ["invoice"]})
        ws.upsert(Doc, {"object_id": "memo-7", "title": "Memo 7", "tags": ["memo"]})
        assert sorted(ws.query(Doc).where(Doc.tags.contained_in("open invoice memo")).execute().ids) == ["invoice-104", "memo-7"]
        assert ws.delete((Doc, "memo-7"))
        assert ws.query(Doc).where(Doc.tags.contained_in("open invoice memo")).execute().ids == ["invoice-104"]
    finally:
        ws.release()


@pytest.mark.fast
def test_a_one_off_failure_ensuring_the_first_routes_index_keeps_the_routes_writing_together(tmp_path, monkeypatch):
    class Doc(hb.Entity):
        identifier = "split-index-ensure-failure-doc"
        title = hb.field(hb.ShortText).store(to="left")
        tags = hb.field(hb.Array[hb.ShortText]).store(to="right", strategy=hb.SparseGramIndex(gram_width=2))

    shared = hb.utils.path_to_file_uri(tmp_path / "shared.db")
    ws = hb.HeavenBase(
        "split-index-ensure-failure",
        detached=True,
        backends={
            "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "system.db")},
            "left": {"type": "sqlite", "database": shared},
            "right": {"type": "sqlite", "database": shared},
        },
    )
    try:
        adapter = ws.backends.get("left").strategy_adapter(hb.SparseGramIndex)
        ensure_index = adapter._ensure_index
        calls = []

        def busy_once():
            calls.append(1)
            if len(calls) == 1:
                raise OperationalError("CREATE TABLE hb_gram_meta", {}, ConnectionError("database is locked"))
            return ensure_index()

        monkeypatch.setattr(adapter, "_ensure_index", busy_once)
        with pytest.warns(RuntimeWarning, match="database is locked"):
            ws.register(Doc)
        assert ws.backends.get("left")._row_write_routes(Doc.identifier) == ws.backends.get("right")._row_write_routes(Doc.identifier) == ("left", "right")
        ws.upsert(Doc, {"object_id": "invoice-104", "title": "Invoice 104", "tags": ["invoice"]})
        assert {key: ws.get("invoice-104", entity=Doc)[key] for key in ("title", "tags")} == {"title": "Invoice 104", "tags": ["invoice"]}
        assert ws.query(Doc).where(Doc.tags.contained_in("open invoice")).execute().ids == ["invoice-104"]
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize(
    "pool",
    [
        {"pool_class": "queue", "pool_size": 1, "max_overflow": 0, "pool_timeout": 3},
        {"pool_class": "assertion"},
    ],
    ids=["queue of one", "assertion"],
)
def test_the_next_write_ensures_a_keyword_index_registration_failed_to_ensure_on_a_one_connection_first_route(tmp_path, monkeypatch, pool):
    class Doc(hb.Entity):
        identifier = "split-index-ensure-failure-one-connection-doc"
        title = hb.field(hb.ShortText).store(to="left")
        tags = hb.field(hb.Array[hb.ShortText]).store(to="right", strategy=hb.SparseGramIndex(gram_width=2))

    shared = hb.utils.path_to_file_uri(tmp_path / "shared.db")
    ws = hb.HeavenBase(
        "split-index-ensure-failure-one-connection",
        detached=True,
        backends={
            "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "system.db")},
            "left": {"type": "sqlite", "database": shared, "pool": dict(pool)},
            "right": {"type": "sqlite", "database": shared, "pool": {"pool_size": 2}},
        },
    )
    try:
        adapter = ws.backends.get("left").strategy_adapter(hb.SparseGramIndex)
        ensure_index = adapter._ensure_index
        calls = []

        def busy_once():
            calls.append(1)
            if len(calls) == 1:
                raise OperationalError("CREATE TABLE hb_gram_meta", {}, ConnectionError("database is locked"))
            return ensure_index()

        monkeypatch.setattr(adapter, "_ensure_index", busy_once)
        with pytest.warns(RuntimeWarning, match="database is locked"):
            ws.register(Doc)
        ws.upsert(Doc, {"object_id": "invoice-104", "title": "Invoice 104", "tags": ["invoice"]})
        ws.upsert(Doc, {"object_id": "memo-7", "title": "Memo 7", "tags": ["memo"]})
        assert sorted(ws.query(Doc).where(Doc.tags.contained_in("open invoice memo")).execute().ids) == ["invoice-104", "memo-7"]
        assert ws.delete((Doc, "memo-7"))
        assert ws.query(Doc).where(Doc.tags.contained_in("open invoice memo")).execute().ids == ["invoice-104"]
    finally:
        ws.release()
