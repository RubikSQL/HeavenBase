"""A registration whose SQL column or side table cannot be added leaves the entity readable and writable."""

import os
import warnings
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import heavenbase as hb
from heavenbase.database.execution import DatabaseError
from heavenbase.placement.binding import StorageBinding


class Note(hb.Entity):
    identifier = "failed-column-note"
    title = hb.field(hb.ShortText).store(to="main")
    body = hb.field(hb.ShortText).store(to="main")


class NoteWithOwner(hb.Entity):
    identifier = "failed-column-note"
    title = hb.field(hb.ShortText).store(to="main")
    body = hb.field(hb.ShortText).store(to="main")
    owner = hb.field(hb.ShortText).required().store(to="main")


class SplitNote(hb.Entity):
    identifier = "failed-column-split-note"
    title = hb.field(hb.ShortText).store(to="left")
    body = hb.field(hb.ShortText).store(to="right")


class SplitNoteWithOwner(hb.Entity):
    identifier = "failed-column-split-note"
    title = hb.field(hb.ShortText).store(to="left")
    body = hb.field(hb.ShortText).store(to="right")
    owner = hb.field(hb.ShortText).required().store(to="right")


class TaggedNote(hb.Entity):
    identifier = "failed-side-table-note"
    title = hb.field(hb.ShortText).store(to="main")


class TaggedNoteWithTags(hb.Entity):
    identifier = "failed-side-table-note"
    title = hb.field(hb.ShortText).store(to="main")
    tags = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SideTable)


def _sqlite(path):
    return {"type": "sqlite", "database": hb.utils.path_to_file_uri(path)}


def _open(tmp_path, layout):
    if layout == "one route":
        backends = {"main": _sqlite(tmp_path / "notes.db")}
    else:
        backends = {
            "main": _sqlite(tmp_path / "notes.db"),
            "left": _sqlite(tmp_path / "notes.db"),
            "right": _sqlite(tmp_path / ("notes.db" if layout == "split on one file" else "right.db")),
        }
    return hb.HeavenBase("failed-column", detached=True, backends=backends)


def _register_failing(ws, entity):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(DatabaseError, match="Cannot add a NOT NULL column"):
            ws.register(entity, allow_replace=True)
    return [str(item.message) for item in caught if issubclass(item.category, RuntimeWarning)]


@pytest.mark.slow
@pytest.mark.parametrize("layout", ["one route", "split on one file", "split on two files"])
def test_a_required_column_the_database_rejects_leaves_the_entity_usable(tmp_path, layout):
    plain, failing = (Note, NoteWithOwner) if layout == "one route" else (SplitNote, SplitNoteWithOwner)
    ws = _open(tmp_path, layout)
    try:
        ws.register(plain)
        ws.upsert(plain, {"object_id": "n1", "title": "Plan", "body": "Draft the plan."})
        # SQLite cannot add a NOT NULL column without a default to a table that holds rows.
        assert _register_failing(ws, failing) == []
        assert {key: ws.get("n1", entity=plain)[key] for key in ("title", "body")} == {"title": "Plan", "body": "Draft the plan."}
        ws.upsert(plain, {"object_id": "n2", "title": "Review", "body": "Review the plan."})
        assert ws.query(plain).execute().ids == ["n1", "n2"]
        ws.register(plain, allow_replace=True)
        assert ws.count(plain) == 2
    finally:
        ws.release()


@pytest.mark.slow
def test_a_new_facade_whose_replacement_fails_registers_the_stored_definition(tmp_path):
    ws = _open(tmp_path, "one route")
    try:
        ws.register(Note)
        ws.upsert(Note, {"object_id": "n1", "title": "Plan", "body": "Draft the plan."})
    finally:
        ws.release()
    ws = _open(tmp_path, "one route")
    try:
        assert _register_failing(ws, NoteWithOwner) == []
        ws.register(Note)
        assert ws.get("n1", entity=Note)["body"] == "Draft the plan."
        ws.upsert(Note, {"object_id": "n2", "title": "Review", "body": "Review the plan."})
        assert ws.count(Note) == 2
    finally:
        ws.release()


@pytest.mark.fast
def test_a_side_table_that_cannot_be_created_leaves_the_entity_usable(tmp_path, monkeypatch):
    ws = _open(tmp_path, "one route")
    try:
        ws.register(TaggedNote)
        ws.upsert(TaggedNote, {"object_id": "n1", "title": "Plan"})
        db = ws.backends.get("main").db

        def unavailable(tables, checkfirst=True):
            raise OperationalError("CREATE TABLE", {}, ConnectionError("database is locked"))

        monkeypatch.setattr(db, "create_tables", unavailable)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with pytest.raises(OperationalError, match="database is locked"):
                ws.register(TaggedNoteWithTags, allow_replace=True)
        monkeypatch.undo()
        assert ws.get("n1", entity=TaggedNote)["title"] == "Plan"
        ws.upsert(TaggedNote, {"object_id": "n2", "title": "Review"})
        ws.register(TaggedNoteWithTags, allow_replace=True)
        ws.upsert(TaggedNoteWithTags, {"object_id": "n3", "title": "Ship", "tags": ["release", "q3"]})
        assert ws.get("n3", entity=TaggedNoteWithTags)["tags"] == ["release", "q3"]
        assert ws.get("n1", entity=TaggedNoteWithTags)["tags"] is None
    finally:
        ws.release()


@pytest.mark.fast
def test_starrocks_registers_a_table_whose_creation_failed_again(monkeypatch):
    monkeypatch.setattr(hb.backends.StarRocksBackend, "_ensure_database", lambda *args: None)
    backend = hb.backends.StarRocksBackend("starrocks")
    statements = []

    def execute(sql, **kwargs):
        statements.append(sql)
        if len(statements) == 1:
            raise OperationalError(sql, {}, ConnectionError("Lost connection to the server during query"))

    monkeypatch.setattr(backend.db, "execute", execute)
    # the stored table once the retried CREATE TABLE has run
    monkeypatch.setattr(backend.db, "columns", lambda table, schema=None, full=False: ["object_id", "title", "body"])
    schema = Note.schema()
    bindings = [StorageBinding(schema.entity_id, name, "starrocks", "inline", "test") for name in ("title", "body")]
    with pytest.raises(OperationalError):
        backend.ensure(schema, bindings)
    assert Note.identifier not in backend.tables
    backend.ensure(schema, bindings)
    assert len(statements) == 2 and "CREATE TABLE" in statements[1].upper()
    assert {column.name for column in backend.tables[Note.identifier].c} == {"object_id", "title", "body"}


_DEFAULT_POSTGRES_URL = "postgresql+psycopg2://rubik:password@localhost:5432/postgres?sslmode=prefer"


@pytest.fixture
def postgres_url():
    pytest.importorskip("psycopg2")
    root = make_url(os.environ.get("HVNB_POSTGRES_URL", _DEFAULT_POSTGRES_URL)).set(database="postgres")
    database = f"failed_column_{uuid4().hex}"
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


class ServerNote(hb.Entity):
    identifier = "failed-column-server-note"
    title = hb.field(hb.ShortText).store(to="notes")
    body = hb.field(hb.ShortText).store(to="notes")


class ServerNoteWithOwner(hb.Entity):
    identifier = "failed-column-server-note"
    title = hb.field(hb.ShortText).store(to="notes")
    body = hb.field(hb.ShortText).store(to="notes")
    owner = hb.field(hb.ShortText).required().store(to="notes")


class ServerNoteWithTopic(hb.Entity):
    identifier = "failed-column-server-note"
    title = hb.field(hb.ShortText).store(to="notes")
    body = hb.field(hb.ShortText).store(to="notes")
    topic = hb.field(hb.ShortText).store(to="notes")


def _open_server(tmp_path, url, backend_type):
    if backend_type == "pgvector":
        pytest.importorskip("pgvector")
    return hb.HeavenBase(
        "failed-column-server",
        detached=True,
        backends={
            "main": _sqlite(tmp_path / "system.db"),
            "notes": {"type": backend_type, "url": url.render_as_string(hide_password=False)},
        },
    )


def _register_failing_on_server(ws):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # PostgreSQL cannot add a NOT NULL column without a default to a table that holds rows.
        with pytest.raises(DatabaseError, match="contains null values"):
            ws.register(ServerNoteWithOwner, allow_replace=True)
    return [str(item.message) for item in caught if issubclass(item.category, RuntimeWarning)]


@pytest.mark.full
@pytest.mark.parametrize("backend_type", ["postgres", "pgvector"])
def test_a_required_column_postgres_rejects_leaves_the_entity_usable(tmp_path, postgres_url, backend_type):
    ws = _open_server(tmp_path, postgres_url, backend_type)
    try:
        ws.register(ServerNote)
        ws.upsert(ServerNote, {"object_id": "n1", "title": "Plan", "body": "Draft the plan."})
        assert _register_failing_on_server(ws) == []
        assert ws.get("n1", entity=ServerNote)["body"] == "Draft the plan."
        ws.upsert(ServerNote, {"object_id": "n2", "title": "Review", "body": "Review the plan."})
        ws.register(ServerNote, allow_replace=True)
        assert ws.count(ServerNote) == 2
    finally:
        ws.release()


@pytest.mark.full
@pytest.mark.parametrize("backend_type", ["postgres", "pgvector"])
def test_a_new_facade_whose_postgres_replacement_fails_registers_the_stored_definition(tmp_path, postgres_url, backend_type):
    ws = _open_server(tmp_path, postgres_url, backend_type)
    try:
        ws.register(ServerNote)
        ws.upsert(ServerNote, {"object_id": "n1", "title": "Plan", "body": "Draft the plan."})
    finally:
        ws.release()
    ws = _open_server(tmp_path, postgres_url, backend_type)
    try:
        assert _register_failing_on_server(ws) == []
        ws.register(ServerNote)
        assert ws.get("n1", entity=ServerNote)["body"] == "Draft the plan."
        ws.upsert(ServerNote, {"object_id": "n2", "title": "Review", "body": "Review the plan."})
        assert ws.count(ServerNote) == 2
    finally:
        ws.release()


@pytest.mark.full
@pytest.mark.parametrize("backend_type", ["postgres", "pgvector"])
def test_a_replacement_adds_a_nullable_postgres_column(tmp_path, postgres_url, backend_type):
    ws = _open_server(tmp_path, postgres_url, backend_type)
    try:
        ws.register(ServerNote)
        ws.upsert(ServerNote, {"object_id": "n1", "title": "Plan", "body": "Draft the plan."})
        ws.register(ServerNoteWithTopic, allow_replace=True)
        ws.upsert(ServerNoteWithTopic, {"object_id": "n2", "title": "Budget", "body": "Q3 budget.", "topic": "finance"})
        assert ws.get("n2", entity=ServerNoteWithTopic)["topic"] == "finance"
        assert ws.get("n1", entity=ServerNoteWithTopic)["topic"] is None
    finally:
        ws.release()
