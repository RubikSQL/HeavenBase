"""Collection callbacks and finalizers take no lock and do no database I/O.

A garbage collection can run a weak-reference callback or a finalizer inside
any critical section of whichever thread allocates. Each test parks a
collection at one such point and checks that the interrupted operation
completes unchanged.
"""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

import pytest
from sqlalchemy import create_engine, event, text

import heavenbase as hb
from heavenbase.database import response as response_module
from heavenbase.database.registry import DatabaseEngineRegistry, _thread_token
from heavenbase.database.response import SQLResponse
from heavenbase.modules import artifacts
from heavenbase.modules.artifacts import ModuleArtifactStore
from heavenbase.modules.base import ResolverCache

T = TypeVar("T")

ROW = {"kind": "kind", "identifier": "identifier", "publication": 0, "revision": 0, "fingerprint": "fingerprint"}


class _Catalog:
    publication = 0


class _Resolver:
    """A minimal ``ModuleResolver``; ``cycle`` lets only a collection free it."""

    def __init__(self) -> None:
        self.catalog = _Catalog()
        self.cycle: object = None

    def inspect(self, kind: str | None = None, identifier: str | None = None, *, include_inactive: bool = False) -> tuple[dict[str, Any], ...]:
        return ()

    def resolve(self, kind: str, identifier: str) -> Any:
        raise KeyError(kind)

    def resolve_executable(self, reference: Any, *, owner: Any = None) -> Any:
        raise KeyError(reference)


class _Value:
    """A weakly referenceable materialization."""


class _Owner:
    """A weakly referenceable artifact lifecycle owner."""

    cycle: object = None


class _CollectOnce(Generic[T]):
    """Run one full collection the first time it is called, then return ``value()``."""

    def __init__(self, value: Callable[[], T]) -> None:
        self.value = value
        self.collected = False

    def __call__(self) -> T:
        result = self.value()
        if not self.collected:
            self.collected = True
            gc.collect()
        return result


class _CollectingDict(dict[str, Any]):
    """A dict whose next ``get`` runs a collection after reading its value."""

    def __init__(self, items: dict[str, Any]) -> None:
        super().__init__(items)
        self.collect = _CollectOnce[None](lambda: None)

    def get(self, key: str, default: Any = None, /) -> Any:
        value = super().get(key, default)
        self.collect()
        return value


def test_resolver_cache_count_survives_a_resolver_collected_inside_it() -> None:
    """The callback of a collected resolver used to pop its state mid-iteration."""
    cache = ResolverCache[_Value]()
    live, doomed = _Resolver(), _Resolver()
    values = [cache.get(cast(Any, resolver), ROW, lambda _row: _Value()) for resolver in (live, doomed)]
    state = cast(Any, cache)._states[id(live)]
    weak = next(iter(state.values.values()))
    gc.disable()
    try:
        doomed.cycle = doomed
        del doomed
        weak.reference = _CollectOnce(weak.reference)
        counted = len(cache)
    finally:
        gc.enable()
    assert weak.reference.collected and counted == 2
    assert len(cache) == 1 and values


def test_artifact_owner_collected_while_another_registers_keeps_the_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The callback of a collected owner used to retire the generation being registered."""
    finder = cast(Any, artifacts)._ArtifactFinder()
    monkeypatch.setattr(artifacts, "_FINDER", finder)
    source = tmp_path / "package"
    source.mkdir()
    (source / "__init__.py").write_text("VALUE = 7\n", encoding="utf-8")
    (source / "meta.yaml").write_bytes(b"manifest: [intentionally invalid")
    store = ModuleArtifactStore(tmp_path / "store")
    artifact = store.capture(source)
    owner = _Owner()
    try:
        gc.disable()
        try:
            doomed = _Owner()
            store.load(artifact, coordinate="demo/collected", generation=1, lifecycle_owner=doomed)
            doomed.cycle = doomed
            del doomed
            finder._generations = _CollectingDict(finder._generations)
            loaded = store.load(artifact, coordinate="demo/collected", generation=1, lifecycle_owner=owner)
        finally:
            gc.enable()
        assert finder._generations.collect.collected and loaded.VALUE == 7
        assert store.release_all(lifecycle_owner=owner) == (loaded.__name__,)
        assert loaded.__name__ not in sys.modules
    finally:
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)


def _lazy_response(tmp_path: Path) -> tuple[SQLResponse, list[str]]:
    """A lazy response over five rows, as ``Database`` returns one outside a transaction."""
    engine = create_engine(f"sqlite:///{tmp_path / 'rows.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE nums (n INTEGER)"))
        connection.execute(text("INSERT INTO nums (n) VALUES (1), (2), (3), (4), (5)"))
    connection = engine.connect()
    closes: list[str] = []

    def close() -> None:
        closes.append(threading.current_thread().name)
        connection.close()

    result = connection.execute(text("SELECT n FROM nums ORDER BY n"))
    return SQLResponse.from_cursor(result, close=close), closes


class _CollectAfterFetch:
    """Wrap a row iterator so its first fetch is followed by one collection."""

    def __init__(self, rows: Iterator[Any]) -> None:
        self.rows = rows
        self.collect = _CollectOnce[None](lambda: None)

    def __iter__(self) -> _CollectAfterFetch:
        return self

    def __next__(self) -> Any:
        row = next(self.rows)
        self.collect()
        return row


def test_a_collected_response_iterator_never_drains_inside_the_collection(tmp_path: Path) -> None:
    """The finalizer of an abandoned iterator used to drain the response mid-fetch."""
    response, closes = _lazy_response(tmp_path)
    abandoned = iter(response)
    assert next(abandoned) == {"n": 1}
    gc.disable()
    try:
        box: list[Any] = [abandoned]
        box.append(box)
        del abandoned, box
        rows = cast(Any, response)._row_iter = _CollectAfterFetch(cast(Any, response)._row_iter)
        drained = response.to_lists()
    finally:
        gc.enable()
    assert rows.collect.collected
    assert drained == [[1], [2], [3], [4], [5]]
    assert closes == [threading.current_thread().name]


def test_a_response_iterator_dropped_on_another_thread_drains_and_closes_there(tmp_path: Path) -> None:
    response, closes = _lazy_response(tmp_path)
    abandoned = iter(response)
    assert next(abandoned) == {"n": 1}
    holder = [abandoned]
    del abandoned
    worker = threading.Thread(target=holder.clear, name="foreign")
    worker.start()
    worker.join(5)
    assert not worker.is_alive() and closes == ["foreign"]
    assert response.to_lists() == [[1], [2], [3], [4], [5]]
    assert closes == ["foreign"]


class _ParkFetch:
    """Wrap a row iterator so its next fetch, once armed, runs ``during`` before returning the row."""

    def __init__(self, rows: Iterator[Any], during: Callable[[], None]) -> None:
        self.rows = rows
        self.during = during
        self.armed = True

    def __iter__(self) -> _ParkFetch:
        return self

    def __next__(self) -> Any:
        row = next(self.rows)
        if self.armed:
            self.armed = False
            self.during()
        return row


def test_a_response_iterator_dropped_on_another_thread_waits_for_a_read_in_progress(tmp_path: Path) -> None:
    """A foreign drop used to drain the cursor in the middle of the owner's fetch, mixing the two reads."""
    response, closes = _lazy_response(tmp_path)
    reading = iter(response)
    assert next(reading) == {"n": 1}
    holder = [iter(response)]
    fetching, go = threading.Event(), threading.Event()

    def park() -> None:
        fetching.set()
        assert go.wait(5)

    cast(Any, response)._row_iter = _ParkFetch(cast(Any, response)._row_iter, park)
    got: list[Any] = []
    reader = threading.Thread(target=lambda: got.append(next(reading)), name="reader")
    reader.start()
    assert fetching.wait(5)
    dropper = threading.Thread(target=holder.clear, name="foreign")
    dropper.start()
    dropper.join(0.3)
    assert dropper.is_alive() and closes == []
    go.set()
    reader.join(5)
    dropper.join(5)
    assert not reader.is_alive() and not dropper.is_alive()
    assert got == [{"n": 2}] and closes == ["foreign"]
    assert list(reading) == [{"n": 3}, {"n": 4}, {"n": 5}]
    assert response.to_lists() == [[1], [2], [3], [4], [5]]


def _eventually(check: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


SETTLER = "heavenbase-sqlresponse-settler"


def test_a_response_iterator_dropped_during_a_read_of_its_response_is_drained_after_the_read(tmp_path: Path) -> None:
    """An iterator freed by its own thread mid-fetch used to drain the cursor inside that fetch, and later left it lazy."""
    response, closes = _lazy_response(tmp_path)
    reading = iter(response)
    assert next(reading) == {"n": 1}
    holder = [iter(response)]
    cast(Any, response)._row_iter = _ParkFetch(cast(Any, response)._row_iter, holder.clear)
    assert next(reading) == {"n": 2}
    assert not holder
    assert _eventually(lambda: closes == [SETTLER]), closes
    assert list(reading) == [{"n": 3}, {"n": 4}, {"n": 5}]


def _insert_returning_on(tmp_path: Path, thread_name: str) -> tuple[Any, Any]:
    """A table plus an ``INSERT ... RETURNING`` response, and its iterator, built on another thread."""
    database = hb.utils.path_to_file_uri(tmp_path / "returning.db")
    db = hb.Database(provider="sqlite", database=database)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    built: list[Any] = []

    def build() -> None:
        response = db.execute("INSERT INTO t (id, v) VALUES (1, 'x') RETURNING id")
        built.append((response, iter(response)))

    worker = threading.Thread(target=build, name=thread_name)
    worker.start()
    worker.join(5)
    assert not worker.is_alive() and len(built) == 1
    return database, built.pop()


def _committed_rows(database: str) -> list[Any]:
    return hb.Database(provider="sqlite", database=database).execute("SELECT id, v FROM t").to_lists()


@pytest.mark.parametrize("consume", [True, False], ids=["partly-read", "unread"])
@pytest.mark.parametrize("keep_response", [True, False], ids=["response-held", "response-dropped"])
def test_an_insert_returning_iterator_dropped_on_another_thread_keeps_its_committed_row(tmp_path: Path, consume: bool, keep_response: bool) -> None:
    """A standalone ``INSERT ... RETURNING`` has committed when ``execute`` returns, so no iterator drop can lose it.

    In 0.1.2.2 its response was lazy and committed only once its iterator was dropped or consumed.
    """
    database, (response, rows) = _insert_returning_on(tmp_path, "builder")
    assert _committed_rows(database) == [[1, "x"]]  # committed before anything reads or drops the response
    if consume:
        assert next(rows) == {"id": 1}
    if not keep_response:
        del response
    del rows
    assert _committed_rows(database) == [[1, "x"]]


def _on_own_thread(body: Callable[[], Any]) -> Any:
    """Run ``body`` on a worker thread and fail instead of hanging the suite when it deadlocks."""
    out: list[Any] = []
    worker = threading.Thread(target=lambda: out.append(body()), name="body", daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "deadlocked"
    assert len(out) == 1
    return out[0]


@pytest.mark.parametrize(("readonly", "ending"), [(False, "commit"), (True, "rollback")])
def test_a_listener_on_the_end_of_a_streamed_reads_transaction_may_use_that_response(tmp_path: Path, readonly: bool, ending: str) -> None:
    """Ending a streamed read's transaction fires an engine event; a listener that read the same response used to deadlock on its lock.

    ``Database.execute`` streams only reads, so only a read's transaction ends when its response is consumed: with a
    commit by default, with a rollback when ``readonly`` accepts the statement.
    """
    database = hb.utils.path_to_file_uri(tmp_path / "listener.db")
    db = hb.Database(provider="sqlite", database=database)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t (id, v) VALUES (1, 'x')")
    responses: list[Any] = []
    seen: list[Any] = []

    def listener(_connection: Any) -> None:
        if responses:
            response = responses[0]
            seen.append((len(response), response.to_lists(), str(response) != ""))
            response.close()

    event.listen(db.engine, ending, listener)
    try:

        def body() -> Any:
            responses.append(db.execute("SELECT id FROM t", readonly=readonly))
            return responses[0].to_lists()

        assert _on_own_thread(body) == [[1]]
    finally:
        event.remove(db.engine, ending, listener)
    assert seen == [(1, [[1]], True)]
    assert _committed_rows(database) == [[1, "x"]]


def test_a_call_on_another_thread_waits_for_the_end_of_a_finished_cursors_transaction(tmp_path: Path) -> None:
    """Such a call used to return while the transaction was still ending, before its commit was visible."""
    response, closes = _lazy_response(tmp_path)
    ending = threading.Event()
    close = cast(Any, response)._close

    def slow_close() -> None:
        ending.set()
        time.sleep(0.5)
        close()

    cast(Any, response)._close = slow_close
    drainer = threading.Thread(target=response.cache, name="drainer")
    drainer.start()
    assert ending.wait(5)
    for call in (response.close, response.to_lists, lambda: list(iter(response))):
        assert _on_own_thread(lambda: (call(), list(closes))[1]) == ["drainer"]
    drainer.join(5)
    assert not drainer.is_alive() and response.to_lists() == [[1], [2], [3], [4], [5]]


def test_a_response_stays_readable_on_other_threads_while_its_transaction_ends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The close callback ran under the read lock, so a reader on another thread waited on whatever ending it waits on.

    A call on another thread waits for the end, but only for a bounded time: code run by the end may wait on that call.
    """
    monkeypatch.setattr(response_module, "_END_WAIT", 0.2)
    response, closes = _lazy_response(tmp_path)
    ending, done = threading.Event(), threading.Event()
    close = cast(Any, response)._close

    def slow_close() -> None:
        ending.set()
        assert done.wait(10)
        close()

    cast(Any, response)._close = slow_close
    drainer = threading.Thread(target=response.cache, name="drainer")
    drainer.start()
    try:
        assert ending.wait(5)
        assert _on_own_thread(lambda: (response.to_lists(), response.close(), len(response))) == ([[1], [2], [3], [4], [5]], None, 5)
        assert closes == []
    finally:
        done.set()
        drainer.join(5)
    assert not drainer.is_alive() and closes == ["drainer"]


def test_a_response_closed_inside_its_own_fetch_ends_after_the_fetch(tmp_path: Path) -> None:
    """Code inside a fetch may call back into the response; the lock is reentrant and the transaction ends after the fetch."""
    response, closes = _lazy_response(tmp_path)
    reading = iter(response)
    assert next(reading) == {"n": 1}
    inside: list[Any] = []

    def close_inside() -> None:
        response.close()
        inside.append(list(closes))

    cast(Any, response)._row_iter = _ParkFetch(cast(Any, response)._row_iter, close_inside)
    assert _on_own_thread(lambda: (next(reading), list(reading))) == ({"n": 2}, [])
    assert inside == [[]] and closes == ["body"]
    assert response.to_lists() == [[1], [2]]


def test_a_response_iterator_dropped_by_its_owner_still_drains_and_closes(tmp_path: Path) -> None:
    response, closes = _lazy_response(tmp_path)
    abandoned = iter(response)
    assert next(abandoned) == {"n": 1}
    del abandoned
    assert closes == [threading.current_thread().name]
    assert response.to_lists() == [[1], [2], [3], [4], [5]]


def _writing_read(tmp_path: Path) -> tuple[Any, str]:
    """A database whose ``bump(x)`` logs ``x`` through the connection running it, so a streamed read writes."""
    database = hb.utils.path_to_file_uri(tmp_path / "writing.db")
    db = hb.Database(provider="sqlite", database=database)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE log (n INTEGER)")
    db.execute("INSERT INTO t (id) VALUES (1), (2), (3)")

    def bump_on(dbapi_connection: Any, _record: Any) -> None:
        def bump(x: int) -> int:
            dbapi_connection.execute("INSERT INTO log (n) VALUES (?)", (x,))
            return x

        dbapi_connection.create_function("bump", 1, bump)

    event.listen(db.engine, "connect", bump_on)
    db.engine.dispose()
    return db, database


def _logged(database: str) -> list[Any]:
    return hb.Database(provider="sqlite", database=database).execute("SELECT n FROM log ORDER BY n").to_lists()


@pytest.mark.parametrize("keep_response", [True, False], ids=["response-held", "response-dropped-after"])
def test_a_collected_iterator_of_a_held_response_still_commits_the_read(tmp_path: Path, keep_response: bool) -> None:
    """A streamed read that writes, abandoned by an iterator a collection frees, committed only if the response was later drained.

    0.1.2.2 committed it inside the collection; dropping the response afterwards then rolled it back.
    """
    db, database = _writing_read(tmp_path)
    response = db.execute("SELECT bump(id) AS b FROM t ORDER BY id")
    rows = iter(response)
    assert next(rows) == {"b": 1}
    box: list[Any] = [rows]
    box.append(box)
    del rows, box
    gc.collect()
    if not keep_response:
        del response
    assert _eventually(lambda: _logged(database) == [[1], [2], [3]]), _logged(database)


def test_an_iterator_collected_together_with_its_response_still_commits_the_read(tmp_path: Path) -> None:
    """The collection reset the connection before any finalizer ran, so the read's writes were rolled back (also in 0.1.2.2)."""
    db, database = _writing_read(tmp_path)
    response = db.execute("SELECT bump(id) AS b FROM t ORDER BY id")
    rows = iter(response)
    assert next(rows) == {"b": 1}
    box: list[Any] = [rows, response]
    box.append(box)
    key = id(response)
    del rows, box, response
    gc.collect()
    assert _eventually(lambda: _logged(database) == [[1], [2], [3]]), _logged(database)
    assert _eventually(lambda: key not in response_module._pins)


def test_a_collected_response_without_an_iterator_still_rolls_back(tmp_path: Path) -> None:
    db, database = _writing_read(tmp_path)
    response = db.execute("SELECT bump(id) AS b FROM t ORDER BY id")
    box: list[Any] = [response]
    box.append(box)
    del box, response
    gc.collect()
    time.sleep(0.2)
    assert _logged(database) == []


@pytest.mark.slow
@pytest.mark.parametrize("collect", ["gc.collect()", "pass"], ids=["collected", "left-for-exit"])
def test_an_iterator_abandoned_just_before_exit_still_commits_the_read(tmp_path: Path, collect: str) -> None:
    """The process may exit right after the abandonment, with or without a collection; the exit hook settles it."""
    path = tmp_path / "exit.db"
    script = textwrap.dedent(f"""
        import gc
        from sqlalchemy import create_engine, event, text
        from heavenbase.database.response import SQLResponse


        def abandon():
            engine = create_engine("sqlite:///{path.as_posix()}")
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
                connection.execute(text("CREATE TABLE log (n INTEGER)"))
                connection.execute(text("INSERT INTO t (id) VALUES (1), (2), (3)"))

            def bump_on(dbapi_connection, _record):
                def bump(x):
                    dbapi_connection.execute("INSERT INTO log (n) VALUES (?)", (x,))
                    return x

                dbapi_connection.create_function("bump", 1, bump)

            event.listen(engine, "connect", bump_on)
            engine.dispose()
            connection = engine.connect()
            transaction = connection.begin()

            def close():
                transaction.commit()
                connection.close()

            response = SQLResponse.from_cursor(connection.execute(text("SELECT bump(id) AS b FROM t ORDER BY id")), close=close)
            rows = iter(response)
            assert next(rows) == {{"b": 1}}
            box = [rows, response]
            box.append(box)


        abandon()
        {collect}
        """)
    env = dict(os.environ)
    src = str(Path(response_module.__file__).resolve().parents[2])  # the source tree this process imported
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    result = subprocess.run([sys.executable, "-c", script], env=env, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT n FROM log ORDER BY n")).all() == [(1,), (2,), (3,)]
    engine.dispose()


def _abandoned_read(db: Any, sql: str, **kwargs: Any) -> None:
    """Start a streamed read, take one row, and leave the response and its iterator to the next collection."""
    response = db.execute(sql, **kwargs)
    rows = iter(response)
    next(rows)
    box: list[Any] = [rows, response]
    box.append(box)


@contextmanager
def _settler_held() -> Iterator[None]:
    """Keep the settler thread from taking queued responses, so the thread that queued them meets them first."""
    with response_module._settling:
        yield


def _rows_db(tmp_path: Path, name: str, rows: int = 2000) -> Any:
    db = hb.Database(provider="sqlite", database=hb.utils.path_to_file_uri(tmp_path / name))
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute(f"INSERT INTO t (id, v) WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < {rows}) SELECT x, 'row' FROM c")
    return db


def test_a_drop_right_after_a_collection_settles_the_abandoned_read_first(tmp_path: Path) -> None:
    """The pinned connection of a read abandoned in a reference cycle counted as this thread's until the settler ended it.

    So a drop right after the collection raised ``DropRefusedError``, where the collection itself had released the
    connection before. The drop now settles the response on its own thread first.
    """
    db = _rows_db(tmp_path, "drop.db")
    registry = DatabaseEngineRegistry
    with _settler_held():
        _abandoned_read(db, "SELECT id, v FROM t ORDER BY id")
        gc.collect()
        assert registry._holds_connection(db.spec._database_key, _thread_token())
        assert registry._may_wait(db.spec) and not registry._holds_any_connection()
        db.drop_database()
    assert not (tmp_path / "drop.db").exists()
    db.dispose()


def test_a_drop_right_after_a_collection_commits_the_abandoned_reads_writes(tmp_path: Path) -> None:
    db, database = _writing_read(tmp_path)
    with _settler_held():
        _abandoned_read(db, "SELECT bump(id) AS b FROM t ORDER BY id")
        gc.collect()
        assert _logged(database) == []
        DatabaseEngineRegistry._need_no_connection(db.spec)
    assert _logged(database) == [[1], [2], [3]]


def test_a_response_freed_with_its_iterator_is_read_without_keeping_its_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The settler cached every remaining row of a response that nothing could read anymore."""
    db = _rows_db(tmp_path, "orphan.db")
    kept: list[int] = []
    pull = SQLResponse._pull

    def counting_pull(self: SQLResponse) -> Any:
        kept.append(1)
        return pull(self)

    monkeypatch.setattr(SQLResponse, "_pull", counting_pull)
    _abandoned_read(db, "SELECT id, v FROM t ORDER BY id")
    gc.collect()
    assert _eventually(lambda: not response_module._pins and db.engine.pool.checkedout() == 0)
    assert len(kept) < 10


def test_a_read_only_response_freed_with_its_iterator_is_rolled_back_without_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A statement that ``readonly`` accepted ends with a rollback, so reading its remaining rows first was wasted."""
    db = _rows_db(tmp_path, "rollback.db")
    ways: list[str] = []
    for name in ("_exhaust", "_release_connection", "_drain"):
        method = getattr(SQLResponse, name)
        monkeypatch.setattr(SQLResponse, name, (lambda method, name: lambda self, **kw: (ways.append(name), method(self, **kw))[1])(method, name))
    _abandoned_read(db, "SELECT id, v FROM t ORDER BY id", readonly=True)
    gc.collect()
    assert _eventually(lambda: not response_module._pins and db.engine.pool.checkedout() == 0)
    assert "_release_connection" in ways and "_exhaust" not in ways and "_drain" not in ways
