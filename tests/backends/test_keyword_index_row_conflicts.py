"""Row conflicts between writers are reported as the database reports them, not as a change of the keyword index.

The cases run against the servers that ``HVNB_MYSQL_URL`` and ``HVNB_POSTGRES_URL`` name, accounts that may create databases.
"""

import os
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import heavenbase as hb
from heavenbase.database.execution import DatabaseError
from heavenbase.execution.contracts import RowOp

pytestmark = pytest.mark.full


class Indexed(hb.Entity):
    identifier = "row-conflict-indexed"
    tags = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


class Note(hb.Entity):
    """An entity without a keyword index, so two writers meet only at its own rows."""

    identifier = "row-conflict-note"
    title = hb.field(hb.ShortText).store(to="notes")


@pytest.fixture
def writers(tmp_path, request):
    """Return two workspaces on one database, each on an engine of its own, with rows ``n1`` and ``n2`` stored."""
    kind, options = request.param
    variable = {"postgres": "HVNB_POSTGRES_URL", "mysql": "HVNB_MYSQL_URL"}[kind]
    url = os.environ.get(variable)
    if not url:
        pytest.skip(f"requires a test server in {variable}")
    database = f"row_conflicts_{uuid4().hex[:16]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.exec_driver_sql(f"CREATE DATABASE {database}")
    route = {"type": kind, "url": make_url(url).set(database=database).render_as_string(hide_password=False), **options}
    opened = []
    try:
        for name in ("one", "two"):
            main = {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / f"{name}-system.db")}
            opened.append(hb.HeavenBase(database.replace("_", "-"), detached=True, backends={"main": main, "notes": dict(route)}))
            opened[-1].register(Indexed)  # the index tables exist, so every write carries the keyword-index check
            opened[-1].register(Note)
        opened[0].upsert_many(Note, [{"object_id": name, "title": "invoice"} for name in ("n1", "n2")])
        yield [ws.backends.get("notes") for ws in opened]
    finally:
        for ws in opened:
            ws.release()
        with admin.connect() as connection:
            connection.exec_driver_sql(f"DROP DATABASE {database}" + (" WITH (FORCE)" if kind == "postgres" else ""))
        admin.dispose()


def _write(backend, object_id, title):
    backend.upsert([RowOp(Note.identifier, object_id, {"title": title})])


def _causes(exc):
    chain = []
    while exc is not None:
        chain.append(exc)
        exc = exc.__cause__
    return chain


@pytest.mark.parametrize("writers", [("mysql", {})], indirect=True, ids=["mysql"])
def test_a_deadlock_between_two_writers_is_a_database_error(writers):
    """Each transaction holds the row the other one writes next, so MySQL ends one with error 1213."""
    barrier, failures = threading.Barrier(2, timeout=60), []

    def write(backend, first, second):
        try:
            with backend.db.tx():
                _write(backend, first, "receipt")
                barrier.wait()
                _write(backend, second, "receipt")
        except Exception as exc:  # reported below
            failures.append(exc)

    threads = [threading.Thread(target=write, args=(backend, *ids), daemon=True) for backend, ids in zip(writers, (("n1", "n2"), ("n2", "n1")))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(90)
    assert not any(thread.is_alive() for thread in threads)
    assert len(failures) == 1, failures
    assert type(failures[0]) is DatabaseError, failures
    assert getattr(_causes(failures[0])[1], "orig").args[0] == 1213


@pytest.mark.parametrize("writers", [("postgres", {"isolation_level": "REPEATABLE READ"})], indirect=True, ids=["postgres-repeatable-read"])
def test_a_row_written_since_the_snapshot_is_a_database_error(writers):
    """PostgreSQL refuses a write of a row that another transaction wrote after the snapshot, whatever the keyword index holds."""
    first, second = writers
    with pytest.raises(DatabaseError) as caught:
        with first.db.tx():
            first.db.orm_execute(text("SELECT COUNT(*) FROM row_conflict_note")).scalar()
            _write(second, "n1", "receipt")
            _write(first, "n1", "credit note")
    assert type(caught.value) is DatabaseError
    assert getattr(_causes(caught.value)[1], "orig").pgcode == "40001"
