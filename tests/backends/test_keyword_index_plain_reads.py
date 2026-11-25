"""PostgreSQL: which keyword-index metadata reads lock rows, for read-only transactions and logins without UPDATE.

The cases run against the server that ``HVNB_POSTGRES_URL`` names, an account that may create databases and logins.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url

import heavenbase as hb
from heavenbase.backends.sql.strategies.sparse_gram import SparseGramIndexConflictError

pytestmark = pytest.mark.full


class Note(hb.Entity):
    identifier = "plain-read-note"
    tags = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


class WiderNote(hb.Entity):
    identifier = "plain-read-note"
    tags = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=3))


@pytest.fixture
def server():
    """Yield the URL of a fresh database's owner and of a login that may SELECT, INSERT and DELETE in it."""
    url = os.environ.get("HVNB_POSTGRES_URL")
    if not url:
        pytest.skip("requires a test server in HVNB_POSTGRES_URL")
    database, login = f"plain_reads_{uuid4().hex[:16]}", f"hb_plain_{uuid4().hex[:10]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    owner = make_url(url).set(database=database)
    with admin.connect() as connection:
        connection.exec_driver_sql(f"CREATE DATABASE {database}")
        connection.exec_driver_sql(f"CREATE USER {login} PASSWORD 'plain'")
    grants = create_engine(owner, isolation_level="AUTOCOMMIT")
    try:
        with grants.connect() as connection:
            connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {login}")
            connection.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, DELETE ON TABLES TO {login}")
        limited = owner.set(username=login, password="plain")
        yield owner.render_as_string(hide_password=False), limited.render_as_string(hide_password=False), database.replace("_", "-")
    finally:
        with grants.connect() as connection:
            connection.exec_driver_sql(f"DROP OWNED BY {login}")
        grants.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f"DROP DATABASE {database} WITH (FORCE)")
            connection.exec_driver_sql(f"DROP USER {login}")
        admin.dispose()


def _ws(tmp_path, name, url, ws_id):
    main = {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / f"{name}-system.db")}
    return hb.HeavenBase(ws_id, detached=True, backends={"main": main, "notes": {"type": "postgres", "url": url}})


def _found(ws, query, entity=Note):
    return sorted(ws.query(entity).where(entity.tags.contained_in(query)).execute().ids)


def test_a_keyword_query_runs_in_a_read_only_transaction(tmp_path, server):
    owner, _, ws_id = server
    ws = _ws(tmp_path, "read-only", owner, ws_id)
    try:
        ws.register(Note)
        ws.upsert(Note, {"object_id": "n1", "tags": ["invoice"]})
        db = ws.backends.get("notes").db
        with db.tx():
            db.execute("SET TRANSACTION READ ONLY", readonly=False)
            assert _found(ws, "an invoice") == ["n1"]
    finally:
        ws.release()


def test_lookups_and_unchanged_registrations_lock_no_metadata_row(tmp_path, server):
    owner, _, ws_id = server
    first = _ws(tmp_path, "first", owner, ws_id)
    second = _ws(tmp_path, "second", owner, ws_id)
    try:
        first.register(Note)
        first.upsert(Note, {"object_id": "n1", "tags": ["invoice"]})
        backend, sent = second.backends.get("notes"), []
        event.listen(backend.db.engine, "before_cursor_execute", lambda conn, cursor, statement, *args: sent.append(statement))
        second.register(Note)
        with backend.db.tx():
            assert _found(second, "an invoice") == ["n1"]
        assert [statement for statement in sent if "FOR SHARE" in statement] == []
        second.upsert(Note, {"object_id": "n2", "tags": ["receipt"]})
        assert any("FOR SHARE" in statement for statement in sent)
        del sent[:]
        second.register(WiderNote, allow_replace=True)
        assert any(statement.startswith("SELECT") and "hb_gram_meta" in statement and "FOR SHARE" in statement for statement in sent)
    finally:
        second.release()
        first.release()


def test_a_login_without_update_opens_writes_and_is_still_refused_after_a_rebuild(tmp_path, server):
    owner, limited, ws_id = server
    owning = _ws(tmp_path, "owner", owner, ws_id)
    login = _ws(tmp_path, "login", limited, ws_id)
    try:
        owning.register(Note)
        owning.upsert(Note, {"object_id": "n1", "tags": ["invoice"]})
        login.register(Note)
        login.upsert(Note, {"object_id": "n2", "tags": ["receipt"]})
        login.upsert(Note, {"object_id": "n1", "tags": ["credit note"]})
        assert _found(login, "a receipt and a credit note") == ["n1", "n2"]
        assert login.delete((Note, "n2"))
        owning.register(WiderNote, allow_replace=True)
        with pytest.raises(SparseGramIndexConflictError, match="rebuilt it with other SparseGramIndex options"):
            login.upsert(Note, {"object_id": "n3", "tags": ["invoice"]})
        with pytest.raises(SparseGramIndexConflictError, match="rebuilt it with other SparseGramIndex options"):
            _found(login, "a credit note")
        login.register(WiderNote, allow_replace=True)
        login.upsert(WiderNote, {"object_id": "n3", "tags": ["invoice"]})
        assert _found(owning, "an invoice and a credit note", WiderNote) == ["n1", "n3"]
    finally:
        login.release()
        owning.release()
