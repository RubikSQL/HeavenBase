"""The keyword-index check of SQL writes on SQLite, DuckDB, PostgreSQL, MySQL, dolt, seekdb, OceanBase and Oracle.

Every route checks its writes, also one that keeps no keyword index; the check rides on the row statements, so a write
whose index state is unchanged sends no query for it; and a change of the index waits for the writes in progress, on
every type whose engine has a lock that lets writers run together while it waits for them. The server cases run when
their ``HVNB_<TYPE>_URL`` names a test server: ``HVNB_DOLT_URL``, ``HVNB_SEEKDB_URL`` and ``HVNB_OCEANBASE_URL`` a
MySQL-protocol account that may create databases, and ``HVNB_ORACLE_URL`` an Oracle account that may create users.
"""

import os
import re
import threading
import time
from collections import Counter
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import heavenbase as hb
from heavenbase.backends.sql.strategies import sparse_gram
from heavenbase.backends.sql.strategies.sparse_gram import SparseGramIndexConflictError
from heavenbase.execution.contracts import RowOp

_DEFAULT_POSTGRES_URL = "postgresql+psycopg2://rubik:password@localhost:5432/postgres?sslmode=prefer"


@pytest.fixture
def postgres_database():
    pytest.importorskip("psycopg2")
    root = make_url(os.environ.get("HVNB_POSTGRES_URL", _DEFAULT_POSTGRES_URL)).set(database="postgres")
    database = f"write_guards_{uuid4().hex}"
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
            yield root.set(database=database).render_as_string(hide_password=False)
        finally:
            with admin.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE {database} WITH (FORCE)")
    finally:
        admin.dispose()


def _mysql_protocol_database(variable, server):
    """Create a database on the MySQL-protocol test server that ``variable`` names, yield its URL, and drop it."""
    pytest.importorskip("pymysql")
    url = os.environ.get(variable)
    if not url:
        pytest.skip(f"requires a dedicated {server} test server in {variable}")
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    database = f"write_guards_{uuid4().hex[:24]}"
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
        try:
            yield admin.url.set(database=database).render_as_string(hide_password=False)
        finally:
            with admin.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE `{database}`")
    finally:
        admin.dispose()


@pytest.fixture
def mysql_database():
    yield from _mysql_protocol_database("HVNB_MYSQL_URL", "MySQL")


@pytest.fixture
def dolt_database():
    yield from _mysql_protocol_database("HVNB_DOLT_URL", "dolt")


@pytest.fixture
def seekdb_database():
    yield from _mysql_protocol_database("HVNB_SEEKDB_URL", "seekdb")


@pytest.fixture
def oceanbase_database():
    yield from _mysql_protocol_database("HVNB_OCEANBASE_URL", "OceanBase")


@pytest.fixture
def oracle_database():
    """Create an Oracle user for one test, yield its URL and service name, and drop it."""
    pytest.importorskip("oracledb")
    url = os.environ.get("HVNB_ORACLE_URL")
    if not url:
        pytest.skip("requires an Oracle test account that may create users in HVNB_ORACLE_URL")
    admin = create_engine(url)
    user, password = f"hbwg_{uuid4().hex[:12]}", uuid4().hex
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE USER {user} IDENTIFIED BY "{password}" QUOTA UNLIMITED ON USERS')
        connection.exec_driver_sql(f"GRANT CREATE SESSION, CREATE TABLE TO {user}")
    try:
        yield admin.url.set(username=user, password=password).render_as_string(hide_password=False), admin.url.query["service_name"]
    finally:
        with admin.begin() as connection:
            for _ in range(50):
                sessions = connection.exec_driver_sql(f"SELECT sid, serial# FROM v$session WHERE username = '{user.upper()}'").all()
                for sid, serial in sessions:
                    try:
                        connection.exec_driver_sql(f"ALTER SYSTEM KILL SESSION '{sid},{serial}' IMMEDIATE")
                    except Exception:  # the session ended meanwhile (ORA-00030)
                        pass
                try:
                    connection.exec_driver_sql(f"DROP USER {user} CASCADE")
                    break
                except Exception:  # sessions still closing
                    time.sleep(0.2)
        admin.dispose()


_SERVERS = ("postgres", "mysql", "dolt", "seekdb", "oceanbase", "oracle")
# backend types whose registration holds off an entity's writes with a lock statement of its own; SQLite does it with its
# write lock and DuckDB with a gate in the process
_LOCK_STATEMENT_TYPES = {"postgres", "mysql", "oracle"}
# backend types whose engine has no lock that writers can share while a registration waits for them
_UNLOCKED_TYPES = {"dolt", "seekdb", "oceanbase"}


@pytest.fixture(params=["sqlite", pytest.param("duckdb", marks=pytest.mark.full), *(pytest.param(name, marks=pytest.mark.full) for name in _SERVERS)])
def database(request, tmp_path):
    """Return the shared database's backend type, a function giving a route config of a backend type on it, and the workspace ID to use.

    A PostgreSQL or MySQL-protocol route opens the database named after its workspace, so the workspaces that share one
    database here share its ID, each with its own system database.
    """
    if request.param in ("sqlite", "duckdb"):
        if request.param == "duckdb":
            pytest.importorskip("duckdb_engine")
        location = {"database": hb.utils.path_to_file_uri(tmp_path / f"shared.{request.param}")}
        return request.param, lambda backend_type=request.param, **options: {"type": backend_type, **location, **options}, None
    found = request.getfixturevalue(f"{request.param}_database")
    if request.param == "oracle":
        url, service = found
        location = {"url": url, "database": service}
    else:
        url = found
        location = {"url": url}
    ws_id = None if request.param == "oracle" else make_url(url).database.replace("_", "-")
    return request.param, lambda backend_type=request.param, **options: {"type": backend_type, **location, **options}, ws_id


def _indexed_ids(ws, definition, text_value):
    """Return the IDs the keyword index of ``definition.labels`` finds for ``text_value``."""
    return sorted(ws.query(definition).where(definition.labels.contained_in(text_value)).execute().ids)


def _xfail_on_unlocked(database, request):
    if database[0] in _UNLOCKED_TYPES:
        request.applymarker(
            pytest.mark.xfail(
                raises=AssertionError,
                strict=True,
                reason=f"{database[0]} has no lock that writers can share while a registration waits for them",
            )
        )


def _workspace(tmp_path, name, notes, ws_id=None):
    return hb.HeavenBase(
        ws_id or name,
        detached=True,
        backends={"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / f"{name}-system.db")}, "notes": notes},
    )


def _open(tmp_path, database, name, backend_type=None):
    dialect, route, ws_id = database
    return _workspace(tmp_path, name, route(backend_type) if backend_type else route(), ws_id)


class Customer(hb.Entity):
    identifier = "write-guard-customer"
    name = hb.field(hb.ShortText).store(to="notes")


class LabelledCustomer(hb.Entity):
    """The same entity as another workspace defines it, with a keyword field it indexes."""

    identifier = "write-guard-customer"
    name = hb.field(hb.ShortText).store(to="notes")
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


class Note(hb.Entity):
    identifier = "write-guard-note"
    tags = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


class _Statements:
    """Record the statements sent through one backend's engine."""

    def __init__(self, backend):
        self.engine = backend.db.engine
        self.sent = []
        self.epoch_writes = 0

    def record(self, conn, cursor, statement, parameters, context, executemany):
        self.sent.append(statement)
        if statement.lstrip().upper().startswith("INSERT") and "epoch:" in str(parameters):
            self.epoch_writes += 1

    @property
    def checks(self):
        """Statements sent only to look at the keyword index: its tables, fingerprints or epochs."""
        lookups = ("hb_gram_meta", "sqlite_master", "information_schema", "pg_class", "to_regclass", "all_tables", "duckdb_tables")
        return [statement for statement in self.sent if statement.lstrip().upper().startswith("SELECT") and any(name in statement.lower() for name in lookups)]

    @property
    def writes(self):
        """How many INSERT and DELETE statements went to each table (Oracle's guarded row is a conditional ``INSERT ALL``)."""
        found = (re.match(r"\s*(INSERT INTO|INSERT ALL WHEN hb_ok = 1 THEN INTO|DELETE FROM) (\S+)", statement) for statement in self.sent)
        return Counter((match.group(1).split()[0], match.group(2).strip('`"[]').lower()) for match in found if match)

    @property
    def entity_locks(self):
        """Statements that hold off an entity's writes while its keyword index changes."""
        return [statement for statement in self.sent if "LOCK TABLE" in statement or "hb_locked_rows" in statement]

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self.record)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self.record)


_ROWS = [{"object_id": f"c{i}", "name": f"Customer {i}"} for i in range(5)]


def _keep_no_index(ws):
    # a backend type that keeps no keyword index, as pgvector and StarRocks do
    ws.backends.get("notes")._keeps_keyword_index = False
    return ws


def _route_without_index(database, tmp_path, name):
    if database[0] == "postgres":
        pytest.importorskip("pgvector")
        return _open(tmp_path, database, name, "pgvector")
    return _keep_no_index(_open(tmp_path, database, name))


@pytest.mark.full
def test_a_route_that_keeps_no_keyword_index_refuses_writes_once_another_workspace_indexes_the_entity(tmp_path, database):
    """Registered before any keyword index existed, the route still notices one created later, without registering again."""
    plain = _route_without_index(database, tmp_path, "write-guard-plain")
    indexed = _open(tmp_path, database, "write-guard-indexed")
    try:
        plain.register(Customer)
        plain.upsert(Customer, {"object_id": "c0", "name": "Fabrikam"})
        indexed.register(LabelledCustomer)
        indexed.upsert(LabelledCustomer, {"object_id": "c1", "name": "Northwind", "labels": ["wholesale"]})
        with pytest.raises(SparseGramIndexConflictError, match="another one indexes the field"):
            plain.upsert(Customer, {"object_id": "c1", "name": "Northwind Traders"})
        with pytest.raises(SparseGramIndexConflictError, match="another one indexes the field"):
            plain.upsert(Customer, {"object_id": "c2", "name": "Contoso"})
        with pytest.raises(SparseGramIndexConflictError, match="another one indexes the field"):
            plain.delete((Customer, "c1"))
        assert indexed.get("c1", entity=LabelledCustomer) == {"object_id": "c1", "name": "Northwind", "labels": ["wholesale"]}
        assert indexed.get("c2", entity=LabelledCustomer) is None
        assert _indexed_ids(indexed, LabelledCustomer, "wholesale orders") == ["c1"]
    finally:
        indexed.release()
        plain.release()


@pytest.mark.full
def test_a_route_on_an_engine_that_commits_every_statement_refuses_writes_once_another_workspace_indexes_the_entity(tmp_path):
    shared = hb.utils.path_to_file_uri(tmp_path / "shared.db")
    plain = _workspace(tmp_path, "write-guard-autocommit", {"type": "sqlite", "database": shared, "isolation_level": "AUTOCOMMIT"})
    indexed = _workspace(tmp_path, "write-guard-autocommit-indexed", {"type": "sqlite", "database": shared})
    try:
        assert not plain.backends.get("notes")._upserts_are_atomic()
        plain.register(Customer)
        indexed.register(LabelledCustomer)
        indexed.upsert(LabelledCustomer, {"object_id": "c1", "name": "Northwind", "labels": ["wholesale"]})
        with pytest.raises(SparseGramIndexConflictError, match="another one indexes the field"):
            plain.upsert(Customer, {"object_id": "c1", "name": "Northwind Traders"})
        assert indexed.get("c1", entity=LabelledCustomer)["labels"] == ["wholesale"]
    finally:
        indexed.release()
        plain.release()


@pytest.mark.full
@pytest.mark.parametrize("index_tables", ["absent", "present"])
@pytest.mark.parametrize("keeps_index", [True, False], ids=["indexing route", "route keeping no index"])
def test_writes_send_only_their_row_statements(tmp_path, database, index_tables, keeps_index):
    ws = _open(tmp_path, database, "write-guard-count") if keeps_index else _route_without_index(database, tmp_path, "write-guard-count")
    try:
        if index_tables == "present":
            ws.register(Note)
            ws.upsert(Note, {"object_id": "n1", "tags": ["invoice"]})
        ws.register(Customer)
        with _Statements(ws.backends.get("notes")) as statements:
            for row in _ROWS:
                ws.upsert(Customer, row)
        # one DELETE and one INSERT per call, as in HeavenBase 0.1.2.2; the INSERT carries the check
        assert statements.checks == []
        assert len(statements.sent) == 2 * len(_ROWS)
        with _Statements(ws.backends.get("notes")) as statements:
            ws.upsert_many(Customer, [dict(row, name=row["name"] + " (updated)") for row in _ROWS])
            assert ws.delete((Customer, "c0"))
        # one DELETE and one INSERT for the call, and the read and DELETE of the workspace delete, as in 0.1.2.2
        assert statements.checks == []
        assert len(statements.sent) == 4
        assert sorted(row["object_id"] for row in ws.backends.get("notes").scan(Customer.identifier)) == ["c1", "c2", "c3", "c4"]
    finally:
        ws.release()


@pytest.mark.full
def test_a_workspace_upsert_many_of_new_rows_inserts_the_first_row_of_each_table_on_its_own(tmp_path, database):
    ws = _open(tmp_path, database, "write-guard-catalog-count")
    try:
        ws.register(Customer)
        with _Statements(ws.backends.get("notes")) as notes, _Statements(ws.backends.get("main")) as main:
            ws.upsert_many(Customer, _ROWS[:2])
        # each table's DELETE finds no stored row, so its first new row goes in on its own: one INSERT more than
        # HeavenBase 0.1.2.2 for the entity table and one for the Catalog table
        assert notes.checks == [] and main.checks == []
        assert notes.writes == Counter({("DELETE", "write_guard_customer"): 1, ("INSERT", "write_guard_customer"): 2})
        assert (main.writes[("DELETE", "sys_catalog")], main.writes[("INSERT", "sys_catalog")]) == (1, 2)
        with _Statements(ws.backends.get("notes")) as notes, _Statements(ws.backends.get("main")) as main:
            ws.upsert_many(Customer, [dict(row, name=row["name"] + " (updated)") for row in _ROWS[:2]])
        # stored rows: each DELETE removes every row it names, so the rows go in together, as in 0.1.2.2
        assert notes.checks == [] and main.checks == []
        assert notes.writes == Counter({("DELETE", "write_guard_customer"): 1, ("INSERT", "write_guard_customer"): 1})
        assert (main.writes[("DELETE", "sys_catalog")], main.writes[("INSERT", "sys_catalog")]) == (1, 1)
    finally:
        ws.release()


@pytest.mark.full
def test_only_a_registration_that_changes_a_stored_fingerprint_holds_off_the_entity_writes(tmp_path, database):
    first = _open(tmp_path, database, "write-guard-reopen-first")
    reopened = _open(tmp_path, database, "write-guard-reopen-second")
    try:
        first.register(IndexedDoc)
        first.upsert(IndexedDoc, {"object_id": "d1", "labels": ["alpha"]})
        with _Statements(reopened.backends.get("notes")) as unchanged:
            reopened.register(IndexedDoc)
        assert unchanged.entity_locks == [] and unchanged.epoch_writes == 0
        with _Statements(reopened.backends.get("notes")) as changed:
            reopened.register(WiderDoc, allow_replace=True)
        # SQLite holds them off with its write lock, which the epoch write takes first, and DuckDB with a gate in the process
        assert len(changed.entity_locks) == (1 if database[0] in _LOCK_STATEMENT_TYPES else 0) and changed.epoch_writes == 1
        assert _indexed_ids(reopened, WiderDoc, "alpha bravo") == ["d1"]
    finally:
        reopened.release()
        first.release()


@pytest.mark.full
def test_a_write_after_another_workspace_changed_the_index_checks_once_and_then_rides_on_its_statements_again(tmp_path, database):
    writer = _open(tmp_path, database, "write-guard-writer")
    other = _open(tmp_path, database, "write-guard-other")
    try:
        writer.register(Customer)
        writer.upsert_many(Customer, _ROWS)
        other.register(LabelledCustomer)
        with pytest.raises(SparseGramIndexConflictError, match="another one indexes the field"):
            writer.upsert(Customer, {"object_id": "c1", "name": "Northwind"})
        # the other workspace drops its keyword index again, which the writer's guard notices
        other.register(Customer, allow_replace=True)
        backend = writer.backends.get("notes")
        with _Statements(backend) as statements:
            writer.upsert(Customer, {"object_id": "c1", "name": "Northwind"})
        assert len(statements.checks) == 1
        with _Statements(backend) as statements:
            writer.upsert(Customer, {"object_id": "c2", "name": "Contoso"})
            assert writer.delete((Customer, "c3"))
        assert statements.checks == []
        assert len(statements.sent) == 4
        other.register(LabelledCustomer, allow_replace=True)
        other.register(Customer, allow_replace=True)
        with _Statements(backend) as statements:
            assert writer.delete((Customer, "c4"))
        assert len(statements.checks) == 1
        assert sorted(row["object_id"] for row in backend.scan(Customer.identifier)) == ["c0", "c1", "c2"]
        assert writer.get("c1", entity=Customer)["name"] == "Northwind"
    finally:
        other.release()
        writer.release()


class Doc(hb.Entity):
    identifier = "write-guard-doc"
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes")


class IndexedDoc(hb.Entity):
    identifier = "write-guard-doc"
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


class WiderDoc(hb.Entity):
    identifier = "write-guard-doc"
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=3))


def _register_while_a_write_is_open(writer, registrar, entity, monkeypatch, entity_id=None):
    """Register ``entity`` in another thread while ``writer`` holds an uncommitted row, then commit the row.

    The row commits once the registration has finished, or once it has started to wait for the entity's writes.
    """
    failures, waiting = [], threading.Event()
    # the registration waits in ``hold_entity_rows``; the release before it had only ``lock_entity_rows``
    name = next((name for name in ("hold_entity_rows", "lock_entity_rows") if hasattr(sparse_gram._SQLGramIndex, name)), None)
    hold = getattr(sparse_gram._SQLGramIndex, name) if name else None

    def hold_and_tell(index, *args):
        waiting.set()
        return hold(index, *args)

    if hold is not None:
        monkeypatch.setattr(sparse_gram._SQLGramIndex, name, hold_and_tell)

    def register():
        try:
            registrar.register(entity, allow_replace=True)
        except BaseException as exc:  # reported by the caller
            failures.append(exc)

    backend = writer.backends.get("notes")
    with backend.db.tx():
        backend.upsert([RowOp(entity_id or Doc.identifier, "d1", {"labels": ["alpha"]})])
        thread = threading.Thread(target=register, daemon=True)
        thread.start()
        deadline = time.monotonic() + 60
        while thread.is_alive() and not waiting.wait(0.02) and time.monotonic() < deadline:
            pass
        if waiting.is_set():
            time.sleep(0.2)  # the registration is about to wait on the database lock
    thread.join(60)
    assert not thread.is_alive()
    assert failures == []


@pytest.mark.full
@pytest.mark.parametrize(
    ("writer_definition", "registered"), [(Doc, IndexedDoc), (IndexedDoc, WiderDoc)], ids=["new index on stored rows", "rebuild with other options"]
)
def test_a_change_of_the_keyword_index_waits_for_the_writes_in_progress(tmp_path, database, writer_definition, registered, monkeypatch, request):
    _xfail_on_unlocked(database, request)
    writer = _open(tmp_path, database, "write-guard-race-writer")
    registrar = _open(tmp_path, database, "write-guard-race-registrar")
    try:
        # the index tables exist already, so the registration below changes only the keyword index
        writer.register(Note)
        writer.register(writer_definition)
        _register_while_a_write_is_open(writer, registrar, registered, monkeypatch)
        assert _indexed_ids(registrar, registered, "alpha bravo") == ["d1"]
    finally:
        registrar.release()
        writer.release()


@pytest.mark.full
def test_a_writer_that_maintains_the_old_options_cannot_write_after_a_rebuild(tmp_path, database):
    writer = _open(tmp_path, database, "write-guard-stale-writer")
    registrar = _open(tmp_path, database, "write-guard-stale-registrar")
    try:
        writer.register(IndexedDoc)
        writer.upsert(IndexedDoc, {"object_id": "d1", "labels": ["alpha"]})
        registrar.register(WiderDoc)
        with pytest.raises(SparseGramIndexConflictError, match="rebuilt it with other SparseGramIndex options"):
            writer.upsert(IndexedDoc, {"object_id": "d2", "labels": ["bravo"]})
        assert _indexed_ids(registrar, WiderDoc, "alpha bravo") == ["d1"]
        assert registrar.get("d2", entity=WiderDoc) is None
    finally:
        registrar.release()
        writer.release()


# Fast-tier cases on SQLite, run on DuckDB in the full tier. Per engine they share three workspaces on one database file,
# and each case uses entities of its own.


@pytest.fixture(scope="module", params=["sqlite", pytest.param("duckdb", marks=pytest.mark.full)])
def shared_file(tmp_path_factory, request):
    if request.param == "duckdb":
        pytest.importorskip("duckdb_engine")
    root = tmp_path_factory.mktemp(f"write-guard-shared-{request.param}")
    route = {"type": request.param, "database": hb.utils.path_to_file_uri(root / f"shared.{request.param}")}
    workspaces = {name: _workspace(root, f"write-guard-shared-{name}", dict(route)) for name in ("writer", "other", "plain")}
    _keep_no_index(workspaces["plain"])
    # registered while the database holds no keyword-index tables
    workspaces["plain"].register(_REFUSED[0])
    workspaces["writer"].register(Note)  # the index tables exist from here on
    try:
        yield workspaces
    finally:
        for ws in workspaces.values():
            ws.release()


def _definitions(tag):
    """Return the plain, keyword-indexed and wider-indexed definitions of one entity used by one case only."""

    class Plain(hb.Entity):
        identifier = f"write-guard-fast-{tag}"
        labels = hb.field(hb.Array[hb.ShortText]).store(to="notes")

    class Indexed(hb.Entity):
        identifier = f"write-guard-fast-{tag}"
        labels = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))

    class Wider(hb.Entity):
        identifier = f"write-guard-fast-{tag}"
        labels = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=3))

    return Plain, Indexed, Wider


_REFUSED = _definitions("refuse")


@pytest.mark.slow
def test_fast_a_route_that_keeps_no_keyword_index_refuses_a_write_over_an_index_created_after_it_registered(shared_file):
    plain_definition, indexed_definition, _ = _REFUSED
    plain, other = shared_file["plain"], shared_file["other"]
    other.register(indexed_definition)
    other.upsert(indexed_definition, {"object_id": "d1", "labels": ["alpha"]})
    with pytest.raises(SparseGramIndexConflictError, match="another one indexes the field"):
        plain.upsert(plain_definition, {"object_id": "d1", "labels": ["bravo"]})
    assert other.query(indexed_definition).where(indexed_definition.labels.contained_in("alpha bravo")).execute().ids == ["d1"]


@pytest.mark.slow
def test_fast_writes_send_only_their_row_statements(shared_file):
    _, indexed_definition, _ = _definitions("count")
    writer = shared_file["writer"]
    writer.register(indexed_definition)
    rows = [{"object_id": f"d{i}", "labels": ["alpha"]} for i in range(3)]
    with _Statements(writer.backends.get("notes")) as notes, _Statements(writer.backends.get("main")) as main:
        writer.upsert(indexed_definition, rows[0])
    assert notes.checks == [] and main.checks == []
    entity_table = indexed_definition.identifier.replace("-", "_")
    assert notes.writes[("DELETE", entity_table)] == 1 and notes.writes[("INSERT", entity_table)] == 1
    with _Statements(writer.backends.get("notes")) as notes, _Statements(writer.backends.get("main")) as main:
        writer.upsert_many(indexed_definition, rows[1:])
    # the first new row of each table goes in on its own: one INSERT more than 0.1.2.2 for the entity and the Catalog table
    assert notes.checks == [] and main.checks == []
    assert notes.writes[("INSERT", entity_table)] == 2 and main.writes[("INSERT", "sys_catalog")] == 2


@pytest.mark.slow
def test_fast_a_new_index_waits_for_the_write_in_progress_and_an_unchanged_reopen_takes_no_lock(shared_file, monkeypatch):
    plain_definition, indexed_definition, _ = _definitions("race")
    writer, other = shared_file["writer"], shared_file["other"]
    writer.register(plain_definition)
    _register_while_a_write_is_open(writer, other, indexed_definition, monkeypatch, entity_id=indexed_definition.identifier)
    assert other.query(indexed_definition).where(indexed_definition.labels.contained_in("alpha bravo")).execute().ids == ["d1"]
    with _Statements(writer.backends.get("notes")) as unchanged:
        writer.register(indexed_definition, allow_replace=True)
    assert unchanged.epoch_writes == 0


@pytest.mark.slow
def test_fast_an_upsert_of_stored_and_new_rows_inserts_them_together(shared_file):
    """A guarded DELETE that removes any row confirms the index state, so only a call of new rows alone needs an INSERT more."""
    _, indexed_definition, _ = _definitions("mixed")
    writer = shared_file["writer"]
    writer.register(indexed_definition)
    writer.upsert(indexed_definition, {"object_id": "d0", "labels": ["alpha"]})
    entity_table = indexed_definition.identifier.replace("-", "_")
    with _Statements(writer.backends.get("notes")) as notes:
        writer.upsert_many(indexed_definition, [{"object_id": "d0", "labels": ["bravo"]}, {"object_id": "d1", "labels": ["charlie"]}])
    assert notes.checks == []
    assert notes.writes[("DELETE", entity_table)] == 1 and notes.writes[("INSERT", entity_table)] == 1
    assert sorted(writer.query(indexed_definition).where(indexed_definition.labels.contained_in("bravo charlie")).execute().ids) == ["d0", "d1"]


@pytest.mark.full
def test_a_rolled_back_write_leaves_no_row(tmp_path, database):
    """dolt and OceanBase report no autocommit flag in their handshake, so PyMySQL left their sessions committing every statement."""
    ws = _open(tmp_path, database, "write-guard-rollback")
    try:
        ws.register(Doc)
        backend = ws.backends.get("notes")
        with pytest.raises(RuntimeError, match="roll back"):
            with backend.db.tx():
                backend.upsert([RowOp(Doc.identifier, "d1", {"labels": ["alpha"]})])
                raise RuntimeError("roll back")
        assert list(backend.scan(Doc.identifier)) == []
    finally:
        ws.release()


@pytest.mark.full
def test_a_route_whose_statements_commit_on_their_own_checks_a_stored_row_write_in_its_first_statement(tmp_path):
    shared = hb.utils.path_to_file_uri(tmp_path / "shared.db")
    plain = _workspace(tmp_path, "write-guard-autocommit-count", {"type": "sqlite", "database": shared, "isolation_level": "AUTOCOMMIT"})
    indexed = _workspace(tmp_path, "write-guard-autocommit-count-indexed", {"type": "sqlite", "database": shared})
    try:
        indexed.register(Note)  # the index tables exist
        plain.register(Customer)
        plain.upsert(Customer, _ROWS[0])
        backend = plain.backends.get("notes")
        with _Statements(backend) as statements:
            plain.upsert(Customer, dict(_ROWS[0], name="Renamed"))
        # the DELETE carries the check and removes the stored row, so no query of its own is sent
        assert statements.checks == [] and sum(statements.writes.values()) == 2
        indexed.register(LabelledCustomer)
        with pytest.raises(SparseGramIndexConflictError, match="another one indexes the field"):
            plain.upsert(Customer, dict(_ROWS[0], name="Renamed again"))
        assert indexed.get("c0", entity=LabelledCustomer)["name"] == "Renamed"
    finally:
        indexed.release()
        plain.release()


@pytest.mark.full
def test_a_duckdb_transaction_that_read_before_an_index_change_cannot_write_the_entity(tmp_path):
    """DuckDB reads from the snapshot of a transaction's first read, so the gate refuses a write that began reading earlier."""
    pytest.importorskip("duckdb_engine")
    route = {"type": "duckdb", "database": hb.utils.path_to_file_uri(tmp_path / "shared.duckdb")}
    writer = _workspace(tmp_path, "write-guard-duck-writer", dict(route))
    registrar = _workspace(tmp_path, "write-guard-duck-registrar", dict(route))
    try:
        writer.register(Note)
        writer.register(Doc)
        backend = writer.backends.get("notes")
        with pytest.raises(SparseGramIndexConflictError, match="may read from a snapshot"):
            with backend.db.tx():
                assert backend.db.orm_execute(text("SELECT COUNT(*) FROM write_guard_doc")).scalar() == 0
                registrar.register(IndexedDoc, allow_replace=True)
                backend.upsert([RowOp(Doc.identifier, "d1", {"labels": ["alpha"]})])
        writer.register(IndexedDoc, allow_replace=True)
        writer.upsert(IndexedDoc, {"object_id": "d1", "labels": ["alpha"]})
        assert registrar.query(IndexedDoc).where(IndexedDoc.labels.contained_in("alpha bravo")).execute().ids == ["d1"]
    finally:
        registrar.release()
        writer.release()
