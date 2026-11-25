"""The keyword-index check of a write whose transaction began before a rebuild committed, at each isolation level.

The cases run against the servers that ``HVNB_POSTGRES_URL``, ``HVNB_MYSQL_URL`` (accounts that may create databases)
and ``HVNB_ORACLE_URL`` (an account that may create users) name.
"""

import os
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import heavenbase as hb
from heavenbase.backends.sql.strategies.sparse_gram import SparseGramIndexConflictError
from heavenbase.database.execution import DatabaseError
from heavenbase.execution.contracts import RowOp

pytestmark = pytest.mark.full


class IndexedDoc(hb.Entity):
    identifier = "isolation-doc"
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


class WiderDoc(hb.Entity):
    identifier = "isolation-doc"
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=3))


class Plain(hb.Entity):
    identifier = "isolation-plain"
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes")


class IndexedPlain(hb.Entity):
    identifier = "isolation-plain"
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


def _server_database(kind):
    variable = {"postgres": "HVNB_POSTGRES_URL", "mysql": "HVNB_MYSQL_URL"}[kind]
    url = os.environ.get(variable)
    if not url:
        pytest.skip(f"requires a test server in {variable}")
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    database = f"isolation_{uuid4().hex[:16]}"
    with admin.connect() as connection:
        connection.exec_driver_sql(
            f"CREATE DATABASE {database}" if kind == "postgres" else f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"
        )
    try:
        url = make_url(url).set(database=database).render_as_string(hide_password=False)
        yield {"url": url}, database.replace("_", "-")
    finally:
        with admin.connect() as connection:
            connection.exec_driver_sql(f"DROP DATABASE {database} WITH (FORCE)" if kind == "postgres" else f"DROP DATABASE `{database}`")
        admin.dispose()


def _oracle_user():
    pytest.importorskip("oracledb")
    url = os.environ.get("HVNB_ORACLE_URL")
    if not url:
        pytest.skip("requires an Oracle test account that may create users in HVNB_ORACLE_URL")
    admin = create_engine(url)
    user, password = f"hbiso_{uuid4().hex[:12]}", uuid4().hex
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE USER {user} IDENTIFIED BY "{password}" QUOTA UNLIMITED ON USERS')
        connection.exec_driver_sql(f"GRANT CREATE SESSION, CREATE TABLE TO {user}")
    try:
        yield {
            "url": admin.url.set(username=user, password=password).render_as_string(hide_password=False),
            "database": admin.url.query["service_name"],
        }, "isolation"
    finally:
        with admin.begin() as connection:
            for _ in range(50):
                for sid, serial in connection.exec_driver_sql(f"SELECT sid, serial# FROM v$session WHERE username = '{user.upper()}'").all():
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


_CASES = [
    ("postgres", "READ COMMITTED"),
    ("postgres", "REPEATABLE READ"),
    ("postgres", "SERIALIZABLE"),
    ("mysql", "READ COMMITTED"),
    ("mysql", "REPEATABLE READ"),
    ("mysql", "SERIALIZABLE"),
    ("oracle", "READ COMMITTED"),
    ("oracle", "SERIALIZABLE"),
]


@pytest.fixture(params=_CASES, ids=[f"{kind}-{level.lower().replace(' ', '-')}" for kind, level in _CASES])
def route(request):
    kind, level = request.param
    found = _oracle_user() if kind == "oracle" else _server_database(kind)
    location, ws_id = next(found)
    try:
        yield kind, level, location, ws_id
    finally:
        next(found, None)


def _writer_route(kind, level, location):
    """Return the writer's route: an engine at ``level``, or on Oracle the default engine (see ``_oracle_level``)."""
    return {"type": kind, **location} if kind == "oracle" else {"type": kind, **location, "isolation_level": level}


def _oracle_level(kind, level):
    """An Oracle engine at SERIALIZABLE fails its own registrations with ORA-08177, so only the write transaction takes it."""
    return level if kind == "oracle" and level != "READ COMMITTED" else None


def _ws(tmp_path, name, notes, ws_id):
    return hb.HeavenBase(
        ws_id, detached=True, backends={"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / f"{name}-system.db")}, "notes": notes}
    )


def _write_after_a_registration_commits(writer, registrar, written, registered, object_id, *, oracle_level=None):
    """Open a write transaction that reads first, let another workspace register ``registered``, then write ``written``.

    ``oracle_level`` sets the level of that one Oracle transaction. Returns the outcome: ``"committed"`` or the refusal.
    """
    backend = writer.backends.get("notes")
    table = backend.tables[written.identifier].name
    failures = []

    def register():
        try:
            registrar.register(registered, allow_replace=True)
        except BaseException as exc:  # reported by the caller
            failures.append(exc)

    try:
        with backend.db.tx():
            if oracle_level:
                backend.db.orm_execute(text(f"SET TRANSACTION ISOLATION LEVEL {oracle_level}"), readonly=False)
            # the transaction's first read takes its snapshot at REPEATABLE READ and SERIALIZABLE
            backend.db.orm_execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            thread = threading.Thread(target=register, daemon=True)
            thread.start()
            thread.join(60)
            assert not thread.is_alive() and failures == [], failures
            backend.upsert([RowOp(written.identifier, object_id, {"labels": ["alpha"]})])
        return "committed"
    except SparseGramIndexConflictError as exc:
        return f"refused: {exc}"
    except DatabaseError as exc:
        # the serialization failure of the row statement itself: PostgreSQL's SQLSTATE 40001 or Oracle's ORA-08177
        if "SerializationFailure" not in str(exc) and "ORA-08177" not in str(exc):
            raise
        return f"refused: {exc}"


def test_a_write_whose_transaction_began_before_a_rebuild_never_leaves_the_index_stale(tmp_path, route):
    kind, level, location, ws_id = route
    writer = _ws(tmp_path, "isolation-writer", _writer_route(kind, level, location), ws_id)
    registrar = _ws(tmp_path, "isolation-registrar", {"type": kind, **location}, ws_id)
    try:
        writer.register(IndexedDoc)
        writer.upsert(IndexedDoc, {"object_id": "d1", "labels": ["alpha"]})
        registrar.register(IndexedDoc)
        outcome = _write_after_a_registration_commits(writer, registrar, IndexedDoc, WiderDoc, "d2", oracle_level=_oracle_level(kind, level))
        stored = sorted(row["object_id"] for row in registrar.backends.get("notes").scan(WiderDoc.identifier))
        found = sorted(registrar.query(WiderDoc).where(WiderDoc.labels.contained_in("alpha")).execute().ids)
        assert found == stored, f"{level}: {outcome}; stored {stored}, the index finds {found}"
    finally:
        registrar.release()
        writer.release()


def _xfail_on_oracle_serializable(route, request):
    if route[0] == "oracle" and route[1] == "SERIALIZABLE":
        request.applymarker(
            pytest.mark.xfail(
                raises=AssertionError,
                strict=True,
                reason="Oracle has no shared row lock, and a SERIALIZABLE transaction cannot see the epoch row a new index adds",
            )
        )


def test_a_write_whose_transaction_began_before_a_new_index_never_leaves_it_stale(tmp_path, route, request):
    """The entity had no keyword index; another workspace creates one while the write's transaction is open."""
    _xfail_on_oracle_serializable(route, request)
    kind, level, location, ws_id = route
    writer = _ws(tmp_path, "isolation-plain-writer", _writer_route(kind, level, location), ws_id)
    registrar = _ws(tmp_path, "isolation-plain-registrar", {"type": kind, **location}, ws_id)
    try:
        registrar.register(IndexedDoc)  # the index tables exist
        writer.register(Plain)
        writer.upsert(Plain, {"object_id": "p1", "labels": ["alpha"]})
        outcome = _write_after_a_registration_commits(writer, registrar, Plain, IndexedPlain, "p2", oracle_level=_oracle_level(kind, level))
        stored = sorted(row["object_id"] for row in registrar.backends.get("notes").scan(IndexedPlain.identifier))
        found = sorted(registrar.query(IndexedPlain).where(IndexedPlain.labels.contained_in("alpha")).execute().ids)
        assert found == stored, f"{level}: {outcome}; stored {stored}, the index finds {found}"
    finally:
        registrar.release()
        writer.release()


def test_a_write_whose_transaction_began_before_the_index_tables_existed_never_leaves_them_stale(tmp_path, route, request):
    """No keyword-index tables existed when the writer registered; another workspace creates them and indexes the entity."""
    _xfail_on_oracle_serializable(route, request)
    kind, level, location, ws_id = route
    writer = _ws(tmp_path, "isolation-tables-writer", _writer_route(kind, level, location), ws_id)
    registrar = _ws(tmp_path, "isolation-tables-registrar", {"type": kind, **location}, ws_id)
    try:
        writer.register(Plain)
        writer.upsert(Plain, {"object_id": "p1", "labels": ["alpha"]})
        outcome = _write_after_a_registration_commits(writer, registrar, Plain, IndexedPlain, "p2", oracle_level=_oracle_level(kind, level))
        stored = sorted(row["object_id"] for row in registrar.backends.get("notes").scan(IndexedPlain.identifier))
        found = sorted(registrar.query(IndexedPlain).where(IndexedPlain.labels.contained_in("alpha")).execute().ids)
        assert found == stored, f"{level}: {outcome}; stored {stored}, the index finds {found}"
    finally:
        registrar.release()
        writer.release()
