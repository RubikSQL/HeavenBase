"""SQL reads plan under the backend lock and run their statements outside it; writes keep the lock."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import Column, String, Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.pool import QueuePool, SingletonThreadPool, StaticPool

import heavenbase as hb
from heavenbase.execution.contracts import QueryFragment

pytestmark = [pytest.mark.fast, pytest.mark.unit]

ENTITY = "read-lock-doc"
PGVECTOR_ENTITY = "read-lock-pgvector-doc"
ROWS = 12
READERS = 4
TIMEOUT = 20.0


class ReadLockDoc(hb.Entity):
    identifier = ENTITY

    title = hb.field(hb.ShortText)
    tags = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)


def _rows(count: int) -> list[dict]:
    return [{"object_id": f"r{index:02}", "title": f"title {index}", "tags": ["shared", str(index)]} for index in range(count)]


def _workspace(tmp_path, name: str):
    backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / f"{name}.db"))
    ws = hb.HeavenBase(name, detached=True, backends={"main": backend})
    ws.register(ReadLockDoc)
    ws.upsert_many(ReadLockDoc, _rows(ROWS))
    return ws, backend


def _serialized_workspace(tmp_path, name: str, storage: str):
    """A workspace whose engine cannot serve two threads: ``:memory:`` with its default static pool or an explicit queue pool, or a file with ``pool_class: static`` or ``singleton``."""
    if storage == "memory":
        config = {"type": "sqlite", "name": "main", "database": ":memory:"}
    elif storage == "memory-queue":
        config = {"type": "sqlite", "name": "main", "database": ":memory:", "pool": {"pool_class": "queue"}}
    else:
        pool_class = "singleton" if storage == "file-singleton" else "static"
        config = {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / f"{name}.db"), "pool": {"pool_class": pool_class}}
    ws = hb.HeavenBase(name, detached=True, backends={"main": config})
    ws.register(ReadLockDoc)
    ws.upsert_many(ReadLockDoc, _rows(ROWS))
    return ws, ws.backends.get("main")


class _Rows:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def to_dicts(self) -> list[dict]:
        return self._rows


def _pages(backend, *, batch_size: int, columns=None) -> list[dict]:
    rows, after = [], None
    while page := backend.scan_page(ENTITY, after=after, batch_size=batch_size, columns=columns):
        rows.extend(page)
        after = page[-1]["object_id"]
    return rows


def _read_holds_lock(backend) -> bool:
    """Return whether a read of the backend's database holds the backend lock while its statement runs."""
    with backend._read_scope(backend.db):
        return backend._lock._is_owned()


class ProbedLock:
    """Delegate to the backend's RLock and record that a thread asked for it before it may block."""

    def __init__(self, inner: threading.RLock):
        self.inner = inner
        self.entered = threading.Event()

    def __enter__(self):
        self.entered.set()
        return self.inner.__enter__()

    def __exit__(self, *exc):
        return self.inner.__exit__(*exc)


def test_sql_reads_run_their_statements_concurrently_while_another_thread_holds_the_backend_lock(tmp_path, monkeypatch):
    ws, backend = _workspace(tmp_path, "sql-read-lock-concurrent")
    expected = _rows(ROWS)
    ids = [row["object_id"] for row in expected]
    real_execute = backend.db.orm_execute
    # every reader plus the test thread meet while the readers' statements are in flight; the barrier
    # can only fill if no reader holds the backend lock during execution
    in_flight = threading.Barrier(READERS + 1)
    resume = threading.Event()

    def gated_execute(stmt, params=None, **kwargs):
        in_flight.wait(TIMEOUT)
        assert resume.wait(TIMEOUT)
        return real_execute(stmt, params, **kwargs)

    monkeypatch.setattr(backend.db, "orm_execute", gated_execute)
    reads = (
        lambda: backend.get_many(ENTITY, ids),
        lambda: backend.scan_page(ENTITY, after=None, batch_size=ROWS + 1),
        lambda: backend.exists_many(ENTITY, ids),
        lambda: backend.count(ENTITY),
    )
    assert len(reads) == READERS  # one statement per reader, so each passes the barrier exactly once

    with ThreadPoolExecutor(max_workers=READERS) as pool:
        futures = [pool.submit(read) for read in reads]
        in_flight.wait(TIMEOUT)
        # the backend lock is free while four statements execute ...
        assert backend._lock.acquire(timeout=TIMEOUT)
        try:
            resume.set()
            # ... and the reads finish, decode included, while this thread still holds it
            results = [future.result(timeout=TIMEOUT) for future in futures]
        finally:
            backend._lock.release()

    assert results == [expected, expected, [True] * ROWS, ROWS]
    ws.release()


def test_sql_upsert_waits_for_a_held_backend_lock(tmp_path):
    ws, backend = _workspace(tmp_path, "sql-read-lock-write")
    inner = backend._lock
    probe = backend._lock = ProbedLock(inner)
    row = {"object_id": "r99", "title": "title 99", "tags": ["shared", "99"]}
    order: list[str] = []
    written = threading.Event()

    def write():
        backend.upsert([hb.ext.RowOp(ENTITY, "r99", row)])
        order.append("written")
        written.set()

    inner.acquire()
    try:
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        assert probe.entered.wait(TIMEOUT)  # the writer asked for the backend lock ...
        assert not written.is_set()  # ... and cannot proceed while this thread holds it
        order.append("released")
    finally:
        inner.release()
    writer.join(TIMEOUT)

    assert order == ["released", "written"]
    backend._lock = inner
    assert backend.get_many(ENTITY, ["r99"]) == [row]
    ws.release()


def test_sql_concurrent_reads_equal_sequential_reads(tmp_path):
    ws, backend = _workspace(tmp_path, "sql-read-lock-parity")
    ids = [f"r{index:02}" for index in range(ROWS + 2)]  # the last two ids are absent

    def read_all():
        return (
            backend.get_many(ENTITY, ids),
            backend.get_many(ENTITY, ids, columns=["title"]),
            _pages(backend, batch_size=5, columns=["tags"]),
            backend.exists_many(ENTITY, ids),
            backend.count(ENTITY),
            backend.scan(ENTITY),
        )

    sequential = read_all()
    start = threading.Barrier(READERS * 2)

    def read_together(_index: int):
        start.wait(TIMEOUT)
        return read_all()

    with ThreadPoolExecutor(max_workers=READERS * 2) as pool:
        concurrent = list(pool.map(read_together, range(READERS * 2)))

    assert all(result == sequential for result in concurrent)
    assert sequential[0][ROWS:] == [None, None] and sequential[3][ROWS:] == [False, False] and sequential[4] == ROWS
    assert sequential[1][0] == {"object_id": "r00", "title": "title 0"} and sequential[2][0] == {"object_id": "r00", "tags": ["shared", "0"]}
    ws.release()


@pytest.mark.slow
def test_sqlite_backend_pool_mapping_sizes_the_engine_pool(tmp_path):
    def sqlite(name: str, **extra):
        return {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / f"{name}.db"), **extra}

    sized = hb.HeavenBase(
        "sql-read-lock-pool", detached=True, backends={"main": sqlite("sized", pool={"pool_class": "queue", "pool_size": 3, "max_overflow": 4})}
    )
    default = hb.HeavenBase("sql-read-lock-pool-default", detached=True, backends={"main": sqlite("default")})

    sized_pool = sized.backends.get("main").db.engine.pool
    default_pool = default.backends.get("main").db.engine.pool
    assert isinstance(sized_pool, QueuePool) and (sized_pool.size(), sized_pool._max_overflow) == (3, 4)
    assert isinstance(default_pool, QueuePool) and (default_pool.size(), default_pool._max_overflow) == (5, 10)
    # keys left out of the mapping keep the provider defaults
    spec = sized.backends.get("main").db.spec.pool
    assert spec["pool_timeout"] == 30 and spec["pool_pre_ping"] is True and spec["connect_args"]["check_same_thread"] is False
    sized.release()
    default.release()


@pytest.mark.slow
@pytest.mark.parametrize(
    ("storage", "pool_type"), [("memory", StaticPool), ("memory-queue", QueuePool), ("file", StaticPool), ("file-singleton", SingletonThreadPool)]
)
def test_sql_reads_keep_the_backend_lock_when_the_engine_cannot_serve_two_threads(tmp_path, monkeypatch, storage, pool_type):
    ws, backend = _serialized_workspace(tmp_path, f"sql-read-lock-serialized-{storage}", storage)
    assert isinstance(backend.db.engine.pool, pool_type) and backend._serialized_reads(backend.db)
    real_execute = backend.db.orm_execute
    in_flight = threading.Event()
    resume = threading.Event()
    owned: list[bool] = []

    def gated_execute(stmt, params=None, **kwargs):
        if threading.current_thread().name == "reader":
            owned.append(backend._lock._is_owned())
            in_flight.set()
            assert resume.wait(TIMEOUT)
        return real_execute(stmt, params, **kwargs)

    monkeypatch.setattr(backend.db, "orm_execute", gated_execute)
    counted: list[int] = []
    reader = threading.Thread(target=lambda: counted.append(backend.count(ENTITY)), name="reader", daemon=True)
    reader.start()
    assert in_flight.wait(TIMEOUT)
    assert owned == [True]  # the reader still owns the backend lock while its statement runs on the one usable connection ...
    row = {"object_id": "r99", "title": "title 99", "tags": ["shared", "99"]}
    writer = threading.Thread(target=lambda: backend.upsert([hb.ext.RowOp(ENTITY, "r99", row)]), daemon=True)
    writer.start()  # ... so this write neither shares that connection's transaction nor opens a second, empty, in-memory database
    resume.set()
    reader.join(TIMEOUT)
    writer.join(TIMEOUT)
    assert not reader.is_alive() and not writer.is_alive()
    assert counted == [ROWS]  # the read never saw the writer's rows, and its rollback did not discard them:
    assert backend.count(ENTITY) == ROWS + 1 and backend.get_many(ENTITY, ["r99"]) == [row]
    ws.release()


@pytest.mark.slow
def test_sql_memory_reads_never_roll_back_a_concurrent_writer():
    ws = hb.HeavenBase("sql-read-lock-memory-stress", detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": ":memory:"}})
    backend = ws.backends.get("main")
    ws.register(ReadLockDoc)
    writes = 150
    stop = threading.Event()
    errors: list[str] = []

    def read_until_stopped():
        while not stop.is_set():
            try:
                backend.count(ENTITY)
                backend.scan(ENTITY)
            except Exception as error:
                errors.append(repr(error))
                return

    def write_all():
        for index in range(writes):
            backend.upsert([hb.ext.RowOp(ENTITY, f"w{index:03}", {"object_id": f"w{index:03}", "title": f"title {index}", "tags": []})])
        stop.set()

    readers = [threading.Thread(target=read_until_stopped, daemon=True) for _ in range(READERS)]
    writer = threading.Thread(target=write_all, daemon=True)
    for thread in (*readers, writer):
        thread.start()
    for thread in (writer, *readers):
        thread.join(TIMEOUT)
    stop.set()
    assert not any(thread.is_alive() for thread in (writer, *readers))
    assert errors == [] and backend.count(ENTITY) == writes
    ws.release()


@pytest.mark.slow
def test_sql_singleton_pool_reads_survive_more_threads_than_its_pool_size(tmp_path):
    # SingletonThreadPool closes other threads' connections, even one running a statement, once
    # more threads than its pool_size (5) have checked out; unserialized reads crashed the process.
    ws, backend = _serialized_workspace(tmp_path, "sql-read-lock-singleton-stress", "file-singleton")
    assert isinstance(backend.db.engine.pool, SingletonThreadPool) and backend._serialized_reads(backend.db)
    writes = 60
    stop = threading.Event()
    errors: list[str] = []
    ids = [f"r{index:02}" for index in range(ROWS)]

    def read_until_stopped():
        while not stop.is_set():
            try:
                backend.count(ENTITY)
                backend.get_many(ENTITY, ids)
                backend.exists_many(ENTITY, ids)
                backend.scan_page(ENTITY, after=None, batch_size=5)
            except Exception as error:
                errors.append(repr(error))
                return

    def write_all():
        for index in range(writes):
            backend.upsert([hb.ext.RowOp(ENTITY, f"w{index:03}", {"object_id": f"w{index:03}", "title": f"title {index}", "tags": ["w"]})])
        stop.set()

    readers = [threading.Thread(target=read_until_stopped, daemon=True) for _ in range(12)]
    writer = threading.Thread(target=write_all, daemon=True)
    for thread in (*readers, writer):
        thread.start()
    for thread in (writer, *readers):
        thread.join(TIMEOUT)
    stop.set()
    assert not any(thread.is_alive() for thread in (writer, *readers))
    assert errors == [] and backend.count(ENTITY) == ROWS + writes
    ws.release()


@pytest.mark.slow
def test_sql_singleton_pool_reads_survive_two_workspaces_on_one_database(tmp_path):
    # Each backend serializes only under its own lock. When two workspaces on one file shared a singleton engine,
    # the other backend's threads pushed the pool past its size and closed connections mid-statement.
    config = {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "shared.db"), "pool": {"pool_class": "singleton"}}
    spaces = [hb.HeavenBase(f"sql-read-lock-singleton-{name}", detached=True, backends={"main": dict(config)}) for name in ("a", "b")]
    for ws in spaces:
        ws.register(ReadLockDoc)
    spaces[0].upsert_many(ReadLockDoc, _rows(ROWS))
    backends = [ws.backends.get("main") for ws in spaces]
    assert backends[0].db.engine is not backends[1].db.engine
    assert all(isinstance(backend.db.engine.pool, SingletonThreadPool) and backend._serialized_reads(backend.db) for backend in backends)
    writes = 40
    stop = threading.Event()
    errors: list[str] = []
    ids = [f"r{index:02}" for index in range(ROWS)]

    def read_until_stopped(backend):
        while not stop.is_set():
            try:
                backend.count(ENTITY)
                backend.get_many(ENTITY, ids)
                backend.exists_many(ENTITY, ids)
                backend.scan_page(ENTITY, after=None, batch_size=5)
            except Exception as error:
                errors.append(repr(error))
                return

    def write_all(backend, prefix):
        try:
            for index in range(writes):
                object_id = f"{prefix}{index:03}"
                backend.upsert([hb.ext.RowOp(ENTITY, object_id, {"object_id": object_id, "title": f"title {index}", "tags": ["w"]})])
        except Exception as error:
            errors.append(repr(error))
        finally:
            stop.set()

    readers = [threading.Thread(target=read_until_stopped, args=(backends[index % 2],), daemon=True) for index in range(12)]
    writers = [threading.Thread(target=write_all, args=(backend, prefix), daemon=True) for backend, prefix in zip(backends, ("a", "b"))]
    for thread in (*readers, *writers):
        thread.start()
    for thread in (*writers, *readers):
        thread.join(TIMEOUT)
    stop.set()
    assert not any(thread.is_alive() for thread in (*writers, *readers))
    assert errors == [] and all(backend.count(ENTITY) == ROWS + 2 * writes for backend in backends)
    for ws in spaces:
        ws.release()


def test_sql_concurrent_readers_share_one_proxy_scope_and_restore_the_environment(tmp_path, monkeypatch):
    from heavenbase.database import DatabaseEngineRegistry

    monkeypatch.setenv("HTTP_PROXY", "http://before")
    if os.name != "nt":  # on Windows the lowercase spelling is the same variable
        monkeypatch.delenv("http_proxy", raising=False)
    proxy = "http://proxy.invalid:3128"
    config = {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "proxied.db"), "http_proxy": proxy}
    ws = hb.HeavenBase("sql-read-lock-proxy", detached=True, backends={"main": config})
    ws.register(ReadLockDoc)
    ws.upsert_many(ReadLockDoc, _rows(ROWS))
    backend = ws.backends.get("main")
    assert backend.db.spec.args["http_proxy"] == proxy and backend.db.proxy.active
    real_get = DatabaseEngineRegistry.get
    # every unlocked read enters ``Database.engine`` and therefore the database's one ``NetworkProxy``;
    # both readers are gated inside that scope, then released one at a time
    inside = threading.Barrier(3)
    resume = {"reader-1": threading.Event(), "reader-2": threading.Event()}
    gated: set[str] = set()

    def gated_get(*args, **kwargs):
        name = threading.current_thread().name
        if name in resume and name not in gated:
            gated.add(name)
            inside.wait(TIMEOUT)
            assert resume[name].wait(TIMEOUT)
        return real_get(*args, **kwargs)

    monkeypatch.setattr(DatabaseEngineRegistry, "get", gated_get)
    results: dict[str, list] = {}
    readers = [threading.Thread(target=lambda n=name: results.setdefault(n, backend.scan(ENTITY)), name=name, daemon=True) for name in resume]
    for reader in readers:
        reader.start()
    inside.wait(TIMEOUT)
    assert os.environ["HTTP_PROXY"] == proxy  # both readers hold the proxy scope at once
    resume["reader-1"].set()
    readers[0].join(TIMEOUT)
    assert not readers[0].is_alive() and os.environ["HTTP_PROXY"] == proxy  # the first exit leaves the scope to the second reader
    resume["reader-2"].set()
    readers[1].join(TIMEOUT)
    assert not readers[1].is_alive() and os.environ["HTTP_PROXY"] == "http://before"  # the last exit restores the original
    assert (os.name == "nt" or "http_proxy" not in os.environ) and results == {"reader-1": _rows(ROWS), "reader-2": _rows(ROWS)}
    ws.release()


def test_pgvector_near_plans_under_the_backend_lock_and_runs_outside_it(monkeypatch):
    Vector = pytest.importorskip("pgvector.sqlalchemy").Vector

    backend = hb.backends.PgVectorBackend("pgvector", database="rubik", host="localhost", port=5432, username="rubik", password="password")
    table = Table("pgv_doc", backend.metadata, Column("object_id", String, primary_key=True), Column("title", String), Column("embedding", Vector(3)))
    backend.tables[PGVECTOR_ENTITY] = table
    planning_lock: list[bool] = []
    read_storage = backend._read_storage

    def record_planning(entity_id):
        planning_lock.append(backend._lock._is_owned())
        return read_storage(entity_id)

    monkeypatch.setattr(backend, "_read_storage", record_planning)
    owned: list[bool] = []
    statements: list[str] = []

    def fake_execute(stmt, params=None, **kwargs):
        owned.append(backend._lock._is_owned())
        statements.append(str(stmt.compile(dialect=postgresql.dialect())).replace("\n", " "))
        assert params is None and kwargs == {"readonly": True}
        return _Rows([{"object_id": "a", "title": "A", "embedding": [1.0, 0.0, 0.0], "_distance": 0.25}])

    monkeypatch.setattr(backend.db, "orm_execute", fake_execute)
    payload = {"vector": [1.0, 0.0, 0.0], "top_k": 2, "metric": "cosine"}
    fragment = QueryFragment("pgvector", "near", "embedding", payload, returns=["score"], notes={"kind": "pgvector_search", "mode": "native"})
    frame = backend.execute(fragment.bind(PGVECTOR_ENTITY, backend="pgvector"))

    assert frame.rows() == [{"object_id": "a", "title": "A", "embedding": [1.0, 0.0, 0.0], "score": 0.75}]
    assert planning_lock == [True]
    assert owned == [False]  # the statement ran outside the backend lock
    assert "<=>" in statements[0] and "ORDER BY" in statements[0] and "LIMIT" in statements[0]
    assert backend.execute(QueryFragment("pgvector", "near", "embedding", payload, notes={"kind": "pgvector_search"}).bind("absent")).rows() == []


class AtomicityDoc(hb.Entity):
    identifier = "read-lock-atomicity-doc"

    title = hb.field(hb.ShortText)


class _NonAtomicUpsertSQLite(hb.backends.SQLiteBackend):
    """A SQLite stand-in for a backend type that does not claim ``atomic-upsert``.

    It writes through ClickHouse's own upsert (a delete and an insert per row, no
    transaction) and answers the ``atomic-upsert`` claim of ``claimed_type``.
    """

    upsert = hb.backends.ClickHouseBackend.upsert

    def __init__(self, *args, claimed_type: str, **kwargs):
        self.claimed_type = claimed_type
        super().__init__(*args, **kwargs)
        self.armed = False
        self.deleted = threading.Event()
        self.read_done = threading.Event()

    def tag(self, identifier):
        if identifier == "atomic-upsert":
            from heavenbase.modules.tags import tag

            return tag(self.module_resolver, "backend_type", self.claimed_type, identifier)
        return super().tag(identifier)

    def _delete_rows(self, table, object_ids):
        from sqlalchemy import delete

        self.db.orm_execute(delete(table).where(table.c.object_id.in_(object_ids)), readonly=False)
        if self.armed:
            self.deleted.set()
            self.read_done.wait(1.0)  # a read running outside the lock would finish inside this gap


@pytest.mark.slow
@pytest.mark.parametrize("claimed_type", ["clickhouse", "starrocks", "trino", "sql"])
def test_sql_reads_never_see_a_non_transactional_upsert_half_applied(tmp_path, monkeypatch, claimed_type):
    """Every backend type without ``atomic-upsert`` keeps its reads under the lock, not ClickHouse alone."""
    backend = _NonAtomicUpsertSQLite("main", hb.utils.path_to_file_uri(tmp_path / "atomicity.db"), claimed_type=claimed_type)
    ws = hb.HeavenBase("sql-read-lock-non-atomic-upsert", detached=True, backends={"main": backend})
    ws.register(AtomicityDoc)
    ws.upsert(AtomicityDoc, {"object_id": "a", "title": "old"})
    backend.armed = True
    assert not backend._upserts_are_atomic() and backend._serialized_reads(backend.db)
    real_scope = backend._read_scope
    planned = threading.Event()

    def gated_scope(db):
        if threading.current_thread().name == "reader":
            planned.set()  # the read has planned under the lock and released it ...
            assert backend.deleted.wait(TIMEOUT)  # ... and its statement starts once the upsert has deleted the row
        return real_scope(db)

    monkeypatch.setattr(backend, "_read_scope", gated_scope)
    seen: list = []

    def read():
        seen.extend(backend.get_many(AtomicityDoc.identifier, ["a"], columns=["title"]))
        backend.read_done.set()

    reader = threading.Thread(target=read, name="reader", daemon=True)
    reader.start()
    assert planned.wait(TIMEOUT)
    backend.upsert([hb.ext.RowOp(AtomicityDoc.identifier, "a", {"object_id": "a", "title": "new"})])
    reader.join(TIMEOUT)
    assert not reader.is_alive()
    # the read waited for the whole upsert instead of reporting the existing row as missing
    assert seen == [{"object_id": "a", "title": "new"}]
    ws.release()


@pytest.mark.parametrize(
    ("identifier", "atomic"),
    [(None, True), ("sqlite", True), ("sql", False), ("clickhouse", False), ("starrocks", False), ("trino", False), ("not-a-registered-sql-type", False)],
)
def test_sql_reads_leave_the_lock_only_for_a_type_that_claims_atomic_upsert(tmp_path, identifier, atomic):
    """The read rule follows the backend type's ``atomic-upsert`` claim; an unknown type counts as not claiming it."""
    backend = hb.backends.SQLBackend("main", provider="sqlite", database=hb.utils.path_to_file_uri(tmp_path / "claim.db"), identifier=identifier)
    assert backend._upserts_are_atomic() is atomic
    assert backend._serialized_reads(backend.db) is not atomic
    assert _read_holds_lock(backend) is not atomic


@pytest.mark.slow
@pytest.mark.parametrize("read", ["scan", "scan_page", "get_many", "exists_many", "count"])
def test_sql_read_planned_before_a_drop_fails_as_a_read_of_the_dropped_database(tmp_path, monkeypatch, read):
    """A drop that lands between planning and running a read leaves the read its own ``Database``, not ``self.db``, so it never raises ``AttributeError``."""
    ws, backend = _workspace(tmp_path, f"drop-race-{read}")
    real_scope = backend._read_scope

    def drop_first(*args):
        backend.drop()  # another thread's drop lands after this read planned under the lock
        assert backend.db is None
        return real_scope(*args)

    monkeypatch.setattr(backend, "_read_scope", drop_first)
    calls = {
        "scan": lambda: backend.scan(ENTITY),
        "get_many": lambda: backend.get_many(ENTITY, ["r00"]),
        "scan_page": lambda: backend.scan_page(ENTITY, batch_size=4),
        "exists_many": lambda: backend.exists_many(ENTITY, ["r00"]),
        "count": lambda: backend.count(ENTITY),
    }
    try:
        calls[read]()
    except AttributeError as error:  # pragma: no cover - the defect this test guards against
        pytest.fail(f"a read racing a drop raised AttributeError: {error}")
    except Exception:
        pass  # the dropped database's own error


POOL = {"pool_class": "queue", "pool_size": 2, "max_overflow": 1, "pool_timeout": 0.5}
HOLD = 0.8  # longer than the pool timeout
READS = 8


def _hold_reader_connections(monkeypatch, backend, in_flight: threading.Semaphore | None = None) -> None:
    """Keep each reader thread's pooled connection checked out for longer than ``pool_timeout``.

    A read's response streams its rows, so its connection stays checked out until they are consumed.
    """
    real_execute = backend.db.orm_execute

    def held_execute(stmt, params=None, **kwargs):
        response = real_execute(stmt, params, **kwargs)
        if threading.current_thread().name.startswith("reader"):
            if in_flight is not None:
                in_flight.release()
            time.sleep(HOLD)
        return response

    monkeypatch.setattr(backend.db, "orm_execute", held_execute)


def _write(backend, object_id: str) -> None:
    backend.upsert([hb.ext.RowOp(ENTITY, object_id, {"object_id": object_id, "title": object_id, "tags": ["w"]})])


@pytest.mark.slow
def test_sql_reads_beyond_the_pool_wait_for_a_connection_and_a_write_never_times_out(tmp_path, monkeypatch):
    """More slow reads than the queue pool holds all finish, and a write among them gets a connection at once.

    Reads outside the backend lock each held a pooled connection, so the reads past ``pool_size + max_overflow``
    and a write behind them failed after ``pool_timeout``; 0.1.2.2 ran them one at a time and completed them all.
    """
    config = {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "pool.db"), "pool": dict(POOL)}
    ws = hb.HeavenBase("sql-read-lock-pool-wait", detached=True, backends={"main": config})
    ws.register(ReadLockDoc)
    ws.upsert_many(ReadLockDoc, _rows(ROWS))
    backend = ws.backends.get("main")
    expected = _rows(ROWS)
    in_flight = threading.Semaphore(0)
    _hold_reader_connections(monkeypatch, backend, in_flight)

    with ThreadPoolExecutor(max_workers=READS, thread_name_prefix="reader") as pool:
        futures = [pool.submit(backend.scan, ENTITY) for _ in range(READS)]
        assert in_flight.acquire(timeout=TIMEOUT)  # reads hold connections while the write starts
        started = time.monotonic()
        _write(backend, "r99")
        write_seconds = time.monotonic() - started
        results = [future.result(timeout=TIMEOUT) for future in futures]

    assert write_seconds < POOL["pool_timeout"]  # the backend kept a connection for its lock holder
    assert all(result in (expected, [*expected, {"object_id": "r99", "title": "r99", "tags": ["w"]}]) for result in results)
    assert backend.count(ENTITY) == ROWS + 1
    ws.release()


@pytest.mark.slow
def test_sql_backends_sharing_an_engine_each_keep_a_connection_and_read_under_the_lock_once_they_keep_all(tmp_path, monkeypatch):
    database = hb.utils.path_to_file_uri(tmp_path / "shared-pool.db")
    backends = [hb.backends.SQLiteBackend(name, database, pool=dict(POOL)) for name in ("a", "b")]
    spaces = [hb.HeavenBase(f"sql-read-lock-shared-pool-{backend.name}", detached=True, backends={"main": backend}) for backend in backends]
    for ws in spaces:
        ws.register(ReadLockDoc)
    spaces[0].upsert_many(ReadLockDoc, _rows(ROWS))
    assert backends[0].db.engine is backends[1].db.engine
    for backend in backends:
        _write(backend, f"w-{backend.name}")  # both have written: each keeps one of the three connections
        _hold_reader_connections(monkeypatch, backend)
    errors: list[str] = []

    def run(call) -> None:
        try:
            call()
        except Exception as error:
            errors.append(repr(error))

    readers = [threading.Thread(target=run, args=(lambda b=backends[index % 2]: b.scan(ENTITY),), name=f"reader-{index}") for index in range(6)]
    writers = [threading.Thread(target=run, args=(lambda b=backend: _write(b, f"x-{b.name}"),)) for backend in backends]
    for thread in readers:
        thread.start()
    time.sleep(HOLD / 4)
    for thread in writers:
        thread.start()
    for thread in (*writers, *readers):
        thread.join(TIMEOUT)
    assert not any(thread.is_alive() for thread in (*writers, *readers))
    assert errors == [] and backends[0].count(ENTITY) == ROWS + 4

    # a third backend on the engine keeps the last connection: reads then keep their backend lock, as in 0.1.2.2
    third = hb.backends.SQLiteBackend("c", database, pool=dict(POOL))
    spaces.append(hb.HeavenBase("sql-read-lock-shared-pool-c", detached=True, backends={"main": third}))
    spaces[-1].register(ReadLockDoc)
    _write(third, "w-c")
    owned: list[bool] = []
    real_execute = third.db.orm_execute
    monkeypatch.setattr(third.db, "orm_execute", lambda stmt, params=None, **kw: owned.append(third._lock._is_owned()) or real_execute(stmt, params, **kw))
    assert third.count(ENTITY) == ROWS + 5 and owned == [True]
    for ws in spaces:
        ws.release()


def test_sql_pool_slots_admit_reads_only_beyond_the_connections_their_backends_keep():
    from heavenbase.backends.sql.base import _PoolSlots

    class Owner:
        pass

    slots, owners = _PoolSlots(3), [Owner(), Owner()]
    for owner in owners:
        slots.join(owner)
    assert slots.acquire()  # one connection is left for reads
    waiting: list[bool] = []
    waiter = threading.Thread(target=lambda: waiting.append(slots.acquire()), daemon=True)
    waiter.start()
    waiter.join(0.2)
    assert waiter.is_alive()  # the second read waits for the first, without a time limit
    slots.release()
    waiter.join(TIMEOUT)
    assert waiting == [True]
    blocked = threading.Thread(target=lambda: waiting.append(slots.acquire()), daemon=True)
    blocked.start()
    blocked.join(0.2)
    assert blocked.is_alive()
    third = Owner()
    slots.join(third)
    slots.release()  # the waiter wakes to find no connection left for reads and keeps its lock instead
    blocked.join(TIMEOUT)
    assert waiting == [True, False]
    assert not slots.acquire()  # and so does every later read
    slots.leave(third)
    del owners[0]  # a collected backend keeps nothing
    assert slots.acquire() and slots.acquire()


class _Answer:
    def __init__(self, ok: bool, rows: list):
        self.ok, self._rows = ok, rows

    def to_lists(self) -> list:
        return self._rows


@pytest.mark.parametrize(
    ("isolation_level", "session_level", "answer", "serialized"),
    [
        (None, "READ COMMITTED", _Answer(True, [[0]]), True),  # locking READ COMMITTED, the default: a scan may mix an upsert's old and new rows
        (None, "READ COMMITTED", _Answer(True, [[1]]), False),  # READ_COMMITTED_SNAPSHOT ON: each statement reads committed row versions
        ("READ COMMITTED", "READ COMMITTED", _Answer(True, [[1]]), False),
        (None, "READ COMMITTED", _Answer(False, []), True),  # the database could not be asked
        ("SNAPSHOT", "SNAPSHOT", None, False),
        ("REPEATABLE READ", "REPEATABLE READ", _Answer(True, [[1]]), True),  # RCSI changes READ COMMITTED alone
        ("SERIALIZABLE", "SERIALIZABLE", _Answer(True, [[1]]), True),
        ("READ UNCOMMITTED", "READ UNCOMMITTED", _Answer(True, [[1]]), True),
        (None, "SNAPSHOT", None, False),  # a level set as each connection opens counts, not only the engine's
        (None, "READ UNCOMMITTED", _Answer(True, [[1]]), True),
        (None, None, _Answer(True, [[1]]), True),  # the session level could not be read
        ("READ COMMITTED", None, _Answer(True, [[1]]), False),  # the engine's own level stands in for it
    ],
    ids=[
        "rcsi-off",
        "rcsi-on",
        "read-committed-rcsi-on",
        "unknown",
        "snapshot",
        "repeatable-read",
        "serializable",
        "read-uncommitted",
        "session-snapshot",
        "session-read-uncommitted",
        "session-unknown",
        "session-unknown-engine-level",
    ],
)
def test_sql_server_reads_leave_the_lock_only_when_each_statement_reads_a_committed_snapshot(monkeypatch, isolation_level, session_level, answer, serialized):
    """SQL Server's default READ COMMITTED takes and releases row locks as a scan moves, so the scan is no snapshot."""
    options = {} if isolation_level is None else {"isolation_level": isolation_level}
    backend = hb.backends.MsSQLBackend("main", database="hb_read_lock", host="127.0.0.1", **options)
    asked: list[str] = []
    sessions: list[bool] = []

    def execute(query, params=None, **kwargs):
        asked.append(str(query))
        assert answer is not None and kwargs.get("readonly") is True
        return answer

    monkeypatch.setattr(backend.db, "execute", execute)
    monkeypatch.setattr(backend, "_session_isolation_level", lambda db: sessions.append(True) or session_level)
    monkeypatch.setattr(backend, "_pool_slots", lambda db: None)  # no server to build an engine for
    assert backend._upserts_are_atomic()
    assert backend._serialized_reads(backend.db) is serialized
    assert backend._serialized_reads(backend.db) is serialized
    assert sessions == [True]  # asked once per connection
    assert len(asked) == ((session_level or isolation_level) == "READ COMMITTED")
    if serialized:
        assert _read_holds_lock(backend)


@pytest.mark.parametrize(
    ("isolation_level", "session_level", "serialized"),
    [
        (None, "REPEATABLE READ", False),  # InnoDB's default reads each statement from a snapshot
        (None, "READ UNCOMMITTED", True),  # a server whose transaction_isolation is READ-UNCOMMITTED
        ("READ COMMITTED", "READ UNCOMMITTED", True),  # an init command set it after the engine's level
        (None, None, True),  # the server could not be asked
        ("REPEATABLE READ", None, False),
    ],
    ids=["default", "server-read-uncommitted", "init-read-uncommitted", "unknown", "unknown-engine-level"],
)
def test_mysql_reads_keep_the_lock_when_the_server_reads_uncommitted_rows(monkeypatch, isolation_level, session_level, serialized):
    """The level a MySQL connection reads at may come from the server or the connection's init command, not only the engine."""
    options = {} if isolation_level is None else {"isolation_level": isolation_level}
    backend = hb.backends.MySQLBackend("main", database="hb_read_lock", host="127.0.0.1", **options)
    sessions: list[bool] = []
    monkeypatch.setattr(backend, "_session_isolation_level", lambda db: sessions.append(True) or session_level)
    monkeypatch.setattr(backend, "_pool_slots", lambda db: None)  # no server to build an engine for
    assert backend._upserts_are_atomic()
    assert backend._serialized_reads(backend.db) is serialized
    assert backend._serialized_reads(backend.db) is serialized and sessions == [True]


def test_sqlite_reads_keep_the_lock_when_a_connection_reads_uncommitted_rows(tmp_path):
    """A level set as each connection opens, outside the engine's ``isolation_level``, is read back from the connection."""
    from sqlalchemy import event

    backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / "pragma.db"))
    engine = backend.db.engine
    event.listen(engine, "connect", lambda dbapi_connection, record: dbapi_connection.execute("PRAGMA read_uncommitted = 1"))
    engine.dispose()  # every connection from here on runs the listener
    assert backend._session_isolation_level(backend.db) == "READ UNCOMMITTED"
    assert backend._upserts_are_atomic() and backend._serialized_reads(backend.db)
    plain = hb.backends.SQLiteBackend("plain", hb.utils.path_to_file_uri(tmp_path / "plain.db"))
    assert plain._session_isolation_level(plain.db) == "SERIALIZABLE" and not plain._serialized_reads(plain.db)


@pytest.mark.slow
@pytest.mark.parametrize(("engine_options", "serialized"), [({}, False), ({"isolation_level": "READ UNCOMMITTED"}, True)])
def test_sql_reads_keep_the_lock_on_an_engine_that_reads_uncommitted_rows(tmp_path, engine_options, serialized):
    config = {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "isolation.db"), **engine_options}
    ws = hb.HeavenBase("sql-read-lock-isolation", detached=True, backends={"main": config})
    backend = ws.backends.get("main")
    assert backend._upserts_are_atomic() and backend._serialized_reads(backend.db) is serialized
    ws.release()


@pytest.mark.slow
@pytest.mark.parametrize(
    "engine_options",
    [{"isolation_level": "AUTOCOMMIT"}, {"args": {"isolation_level": "AUTOCOMMIT"}}, {"execution_options": {"isolation_level": "AUTOCOMMIT"}}],
)
def test_sql_reads_keep_the_lock_on_an_engine_that_commits_every_statement(tmp_path, engine_options):
    """An engine that commits every statement (Trino's default) withdraws ``atomic-upsert`` from any type, so reads keep the lock."""
    config = {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "autocommit.db"), **engine_options}
    ws = hb.HeavenBase("sql-read-lock-autocommit", detached=True, backends={"main": config})
    backend = ws.backends.get("main")
    assert not backend._upserts_are_atomic() and backend._serialized_reads(backend.db)
    assert _read_holds_lock(backend)
    ws.release()


@pytest.mark.slow
def test_sql_read_by_the_lock_holder_runs_under_the_lock_without_waiting_for_a_slot(tmp_path, monkeypatch):
    """The sparse-GRAM adapter reads rows while it holds the backend lock; such a read must not wait behind the reads outside it."""
    config = {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "holder.db"), "pool": dict(POOL)}
    ws = hb.HeavenBase("sql-read-lock-holder-read", detached=True, backends={"main": config})
    ws.register(ReadLockDoc)
    ws.upsert_many(ReadLockDoc, _rows(ROWS))
    backend = ws.backends.get("main")
    in_flight = threading.Semaphore(0)
    _hold_reader_connections(monkeypatch, backend, in_flight)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="reader") as pool:
        futures = [pool.submit(backend.scan, ENTITY) for _ in range(2)]  # the two connections reads may use
        assert in_flight.acquire(timeout=TIMEOUT) and in_flight.acquire(timeout=TIMEOUT)
        with backend._lock:
            started = time.monotonic()
            assert backend.count(ENTITY) == ROWS
            assert backend.get_many(ENTITY, ["r00"])[0]["object_id"] == "r00"
            seconds = time.monotonic() - started
        assert all(len(future.result(timeout=TIMEOUT)) == ROWS for future in futures)

    assert seconds < HOLD / 2  # it ran on the connection kept for the lock holder, not after a read's slot
    ws.release()


def test_sql_pool_slots_leave_out_connections_other_users_of_the_engine_hold(tmp_path):
    from sqlalchemy import create_engine, text

    from heavenbase.backends.sql.base import _PoolSlots

    engine = create_engine(f"sqlite:///{tmp_path / 'others.db'}", poolclass=QueuePool, pool_size=2, max_overflow=1, pool_timeout=0.5)

    class Owner:
        def __init__(self):
            self._lock = threading.RLock()

    slots, owner = _PoolSlots.for_engine(engine), Owner()
    slots.join(owner)
    direct = engine.connect()
    direct.execute(text("SELECT 1"))  # a statement outside every backend holds a connection
    with owner._lock:
        with engine.connect() as held:  # the lock holder's own connection is the one kept for it
            held.execute(text("SELECT 1"))
            assert slots.acquire()  # 3 connections - 1 kept - 1 held elsewhere: one for reads
            with engine.connect() as read:  # a slot read's connection is its slot
                read.execute(text("SELECT 1"))
    waiting: list[bool] = []

    def read() -> None:
        waiting.append(slots.acquire())
        slots.release()

    waiter = threading.Thread(target=read, daemon=True)
    waiter.start()
    waiter.join(0.3)
    assert waiter.is_alive()  # the second read waits: the direct user still holds its connection
    direct.close()
    waiter.join(TIMEOUT)
    assert waiting == [True]  # the checkin freed a slot
    slots.release()
    direct = engine.connect()
    second = engine.connect()
    direct.execute(text("SELECT 1"))
    second.execute(text("SELECT 1"))
    assert not slots.acquire()  # the other users and the kept connection leave nothing: reads keep their lock
    direct.close()
    second.close()
    assert slots.acquire() and slots.acquire()
    slots.release()
    slots.release()
    engine.dispose()


@pytest.mark.slow
def test_sql_lock_holder_keeps_its_connection_from_a_direct_database_user_who_came_first(tmp_path, monkeypatch):
    """A statement held open directly on the backend's Database leaves reads fewer connections, not the lock holder none."""
    config = {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "direct.db"), "pool": dict(POOL)}
    ws = hb.HeavenBase("sql-read-lock-direct-user", detached=True, backends={"main": config})
    ws.register(ReadLockDoc)
    ws.upsert_many(ReadLockDoc, _rows(ROWS))
    backend = ws.backends.get("main")
    direct = backend.db.engine.connect()
    direct.exec_driver_sql("SELECT 1")
    in_flight = threading.Semaphore(0)
    _hold_reader_connections(monkeypatch, backend, in_flight)

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="reader") as pool:
        futures = [pool.submit(backend.scan, ENTITY) for _ in range(4)]
        assert in_flight.acquire(timeout=TIMEOUT)
        started = time.monotonic()
        _write(backend, "r99")
        write_seconds = time.monotonic() - started
        assert all(len(future.result(timeout=TIMEOUT)) in (ROWS, ROWS + 1) for future in futures)
    direct.close()

    assert write_seconds < POOL["pool_timeout"]
    assert backend.count(ENTITY) == ROWS + 1
    ws.release()
