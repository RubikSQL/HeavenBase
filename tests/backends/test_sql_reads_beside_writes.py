"""SQL backend reads beside a drop, a row write of the same backend, and their own batches."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import heavenbase as hb

ENTITY = "reads-beside-writes-doc"
ROWS = 6
TIMEOUT = 20.0
_DEFAULT_POSTGRES_URL = "postgresql+psycopg2://rubik:password@localhost:5432/postgres?sslmode=prefer"


class Doc(hb.Entity):
    identifier = ENTITY

    title = hb.field(hb.ShortText)
    tags = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)
    labels = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SparseGramIndex(gram_width=2), optional=True)


def _row(index: int) -> dict:
    return {"object_id": f"d{index:02}", "title": f"title {index}", "tags": ["shared", str(index)], "labels": [f"INV-{index}"]}


def _workspace(tmp_path, name: str):
    backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / f"{name}.db"))
    ws = hb.HeavenBase(name, detached=True, backends={"main": backend})
    ws.register(Doc)
    ws.upsert_many(Doc, [_row(index) for index in range(ROWS)])
    return ws, backend


def _gate_thread(monkeypatch, backend, thread_name: str, *, call: int = 1, matching: str = ""):
    """Hold the ``call``-th statement (counting those whose SQL contains ``matching``) that ``thread_name`` runs on the backend's database.

    Returns:
        tuple[threading.Event, threading.Event]: ``held`` is set once that statement waits, ``release`` lets it run.
    """
    real_execute = backend.db.orm_execute
    held, release = threading.Event(), threading.Event()
    seen = [0]

    def gated_execute(stmt, params=None, **kwargs):
        if threading.current_thread().name.startswith(thread_name) and matching in str(stmt):
            seen[0] += 1
            if seen[0] == call:
                held.set()
                assert release.wait(TIMEOUT)
        return real_execute(stmt, params, **kwargs)

    monkeypatch.setattr(backend.db, "orm_execute", gated_execute)
    return held, release


def _reads(ws, backend) -> dict:
    ids = [f"d{index:02}" for index in range(ROWS)]
    return {
        "get_many": lambda: backend.get_many(ENTITY, ids),
        "exists_many": lambda: backend.exists_many(ENTITY, ids),
        "scan": lambda: backend.scan(ENTITY),
        "scan_page": lambda: backend.scan_page(ENTITY, after=None, batch_size=ROWS + 5),
        "count": lambda: backend.count(ENTITY),
        "filter": lambda: sorted(ws.query(Doc).where(Doc.title == "title 1").execute().ids),
        "contained_in": lambda: sorted(ws.query(Doc).where(Doc.labels.contained_in("invoice INV-3 is open")).execute().ids),
    }


def test_sql_drop_waits_for_a_read_running_outside_the_backend_lock(tmp_path, monkeypatch):
    ws, backend = _workspace(tmp_path, "drop-waits-for-read")
    held, release = _gate_thread(monkeypatch, backend, "reader")
    dropped = threading.Event()

    def drop() -> None:
        backend.drop()
        dropped.set()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="reader") as pool:
        future = pool.submit(backend.scan, ENTITY)
        assert held.wait(TIMEOUT)  # the read planned and its statement is about to run outside the lock
        dropper = threading.Thread(target=drop, name="dropper", daemon=True)
        dropper.start()
        drop_waited = not dropped.wait(0.5)
        release.set()
        rows = future.result(timeout=TIMEOUT)
        dropper.join(TIMEOUT)
    assert drop_waited, "drop removed the database while a read of it was running"
    assert rows == [_row(index) for index in range(ROWS)]
    assert dropped.is_set() and backend.db is None
    with pytest.raises(RuntimeError, match="unavailable after drop"):
        backend.scan(ENTITY)


@pytest.mark.parametrize("read", ["get_many", "exists_many", "scan", "scan_page", "count", "filter"])
def test_sql_read_planned_before_a_drop_raises_runtime_error(tmp_path, monkeypatch, read):
    """A read whose statement would run after a drop removed its database fails as a read after the drop does."""
    ws, backend = _workspace(tmp_path, f"read-after-drop-{read}")
    real_scope = backend._read_scope

    def drop_first(*args, **kwargs):
        backend.drop()  # another thread's drop lands after this read planned
        return real_scope(*args, **kwargs)

    monkeypatch.setattr(backend, "_read_scope", drop_first)
    with pytest.raises(RuntimeError, match="unavailable after drop"):
        _reads(ws, backend)[read]()


@pytest.mark.parametrize("read", ["get_many", "exists_many"])
def test_sql_batched_lookup_never_sees_part_of_a_write(tmp_path, monkeypatch, read):
    """A lookup over several id batches sees a write of the same backend completely or not at all, as in 0.1.2.2."""
    ws, backend = _workspace(tmp_path, f"batched-lookup-{read}")
    monkeypatch.setattr(backend, "_sql_id_lookup_batch_size", 2)
    ids = ["d00", "d01", "d02", "d03", "d04", "d09"]  # d00 is in the first batch, d09 in the last
    held, release = _gate_thread(monkeypatch, backend, "reader", call=2)
    move = [hb.ext.RowOp(ENTITY, "d00", op="delete"), hb.ext.RowOp(ENTITY, "d09", _row(9))]
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="reader") as pool:
        future = pool.submit(getattr(backend, read), ENTITY, ids)
        assert held.wait(TIMEOUT)  # the first batch has been read
        writer = threading.Thread(target=backend.upsert, args=(move,), name="writer", daemon=True)
        writer.start()
        writer.join(0.5)  # a write landing between two batches has done so by now
        release.set()
        result = future.result(timeout=TIMEOUT)
        writer.join(TIMEOUT)
    present = [bool(item) for item in result]
    assert present[0] != present[-1], f"the lookup saw d00 {'present' if present[0] else 'absent'} and d09 {'present' if present[-1] else 'absent'}"
    assert backend.exists_many(ENTITY, ["d00", "d09"]) == [False, True]
    ws.release()


def test_sql_drop_stops_waiting_for_a_read_after_its_bound_and_the_read_raises_runtime_error(tmp_path, monkeypatch):
    from heavenbase.backends.sql import base as sql_base

    monkeypatch.setattr(sql_base, "_DROP_READ_WAIT_SECONDS", 0.3)
    ws, backend = _workspace(tmp_path, "drop-wait-bound")
    held, release = _gate_thread(monkeypatch, backend, "reader")
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="reader") as pool:
        future = pool.submit(backend.scan, ENTITY)
        assert held.wait(TIMEOUT)
        backend.drop()  # gives up waiting after 0.3 s and removes the database under the running read
        release.set()
        with pytest.raises(RuntimeError, match="unavailable after drop"):
            future.result(timeout=TIMEOUT)


@pytest.mark.slow
def test_sql_reads_and_row_writes_of_one_backend_run_together_without_stalling(tmp_path):
    """Readers of every kind and writers on one backend finish together and every read returns stored rows."""
    ws, backend = _workspace(tmp_path, "reads-and-writes-together")
    stop = time.monotonic() + 1.5
    errors: list[BaseException] = []

    def write(writer: int) -> int:
        count = 0
        while count == 0 or time.monotonic() < stop:  # at least once, however late the thread starts
            index = 10 + writer * 1000 + count
            backend.upsert([hb.ext.RowOp(ENTITY, f"d{index:02}", _row(index))])
            count += 1
        return count

    def read(name: str) -> int:
        count = 0
        reads = _reads(ws, backend)
        while count == 0 or time.monotonic() < stop:  # at least once, however late the thread starts
            result = reads[name]()
            if name in ("scan", "scan_page"):
                assert all(row == _row(int(row["object_id"][1:])) for row in result)
            count += 1
        return count

    with ThreadPoolExecutor(max_workers=11) as pool:
        futures = [pool.submit(write, writer) for writer in range(4)]
        futures += [pool.submit(read, name) for name in ("get_many", "exists_many", "scan", "scan_page", "count", "filter", "contained_in")]
        for future in futures:
            try:
                assert future.result(timeout=TIMEOUT) > 0
            except BaseException as error:  # collect every thread's failure
                errors.append(error)
    assert not errors
    ws.release()


def test_sparse_gram_query_sees_a_move_by_the_same_backend_whole(tmp_path, monkeypatch):
    """One upsert replaces the only matching row by another; a sparse-GRAM query of the same backend finds one of them, as in 0.1.2.2."""
    ws, backend = _workspace(tmp_path, "gram-query-beside-a-move")
    real_execute = backend.db.orm_execute
    held, release = threading.Event(), threading.Event()

    def gated_execute(stmt, params=None, **kwargs):
        text = str(stmt)
        # the reader's row read after its keyword lookup found the candidates
        if threading.current_thread().name.startswith("reader") and "POSTCOMPILE_object_ids" in text and "hb_gram" not in text and not held.is_set():
            held.set()
            assert release.wait(TIMEOUT)
        return real_execute(stmt, params, **kwargs)

    monkeypatch.setattr(backend.db, "orm_execute", gated_execute)
    move = [hb.ext.RowOp(ENTITY, "d03", op="delete"), hb.ext.RowOp(ENTITY, "d33", {**_row(33), "labels": ["INV-3"]})]
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="reader") as pool:
        future = pool.submit(lambda: sorted(ws.query(Doc).where(Doc.labels.contained_in("invoice INV-3 is open")).execute().ids))
        assert held.wait(TIMEOUT)
        writer = threading.Thread(target=backend.upsert, args=(move,), name="writer", daemon=True)
        writer.start()
        writer.join(0.5)  # a write landing between the lookup and the row read has done so by now
        release.set()
        result = future.result(timeout=TIMEOUT)
        writer.join(TIMEOUT)
    assert backend.exists_many(ENTITY, ["d03", "d33"]) == [False, True]
    assert result in (["d03"], ["d33"])
    ws.release()


@pytest.fixture
def postgres_url():
    pytest.importorskip("psycopg2")
    pytest.importorskip("pgvector")
    root = make_url(os.environ.get("HVNB_POSTGRES_URL", _DEFAULT_POSTGRES_URL)).set(database="postgres")
    database = f"reads_beside_drop_{uuid4().hex}"
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
                connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
    finally:
        admin.dispose()


class Note(hb.Entity):
    identifier = "reads-beside-drop-note"

    title = hb.field(hb.ShortText).store(to="notes")


@pytest.mark.parametrize("route", ["postgres", "pgvector", "pgvector shared_database"])
def test_sql_drop_waits_for_a_running_read_on_every_postgres_route(tmp_path, monkeypatch, postgres_url, route):
    notes = {"type": route.split()[0], "url": postgres_url.render_as_string(hide_password=False)}
    if route.endswith("shared_database"):
        notes["shared_database"] = True
    ws = hb.HeavenBase(
        f"drop-waits-{route.replace(' ', '-').replace('_', '-')}",
        detached=True,
        backends={"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "system.db")}, "notes": notes},
    )
    ws.register(Note)
    ws.upsert_many(Note, [{"object_id": f"n{index}", "title": f"title {index}"} for index in range(5)])
    backend = ws.backends.get("notes")
    held, release = _gate_thread(monkeypatch, backend, "reader")
    dropped = threading.Event()

    def drop() -> None:
        backend.drop()
        dropped.set()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="reader") as pool:
        future = pool.submit(backend.scan, Note.identifier)
        assert held.wait(TIMEOUT)
        dropper = threading.Thread(target=drop, name="dropper", daemon=True)
        dropper.start()
        drop_waited = not dropped.wait(0.5)
        release.set()
        rows = future.result(timeout=TIMEOUT)
        dropper.join(TIMEOUT)
    assert drop_waited, f"drop removed the {route} tables while a read of them was running"
    assert [row["object_id"] for row in rows] == [f"n{index}" for index in range(5)]
    with pytest.raises(RuntimeError, match="unavailable after drop"):
        backend.scan(Note.identifier)
