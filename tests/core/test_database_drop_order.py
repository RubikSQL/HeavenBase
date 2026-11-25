"""Drops of one database run one after another, from any Context; the dropping Context's other calls wait for the drop when that is safe."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import os
import threading
from time import monotonic

import pytest

import heavenbase as hb
from heavenbase.database import database as database_module
from heavenbase.database.registry import DatabaseEngineRegistry
from heavenbase.llm.cache import LLMCache
from heavenbase.utils.config import ConfigManager
from heavenbase.workspace import hidden_store
from heavenbase.workspace.hidden_store import HiddenWorkspaceStore
from heavenbase.workspace.runtime import HeavenBase

pytestmark = pytest.mark.fast

_KEY = {"object_id": "text-row", "spec_key": "spec", "request_key": "request", "args": {}}
_ANSWER = {"text": "summary of the quarterly report"}
# Seconds. Only stops a deadlocked test; no passing run depends on it.
_HANG_GUARD = 600


class TeamNote(hb.Entity):
    identifier = "drop-order-team-note"
    title = hb.field(hb.ShortText)


def _context(root):
    return hb.Context.load(
        {
            "version": 1,
            "root": str(root),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=ConfigManager(root=str(root), setup=False),
    )


def _backend(path):
    return {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(path)}}


def _cache(context, path):
    cache = LLMCache.current(config_manager=context.config)
    cache.config["backend"] = _backend(path)
    return cache


def _facade(context, path):
    return hb.Database(database=hb.utils.path_to_file_uri(path), provider="sqlite", resolver=context.modules(), config=context.config)


def _ready(context, root):
    # The Context's own cache use registers the SQLite dialect that a Database facade resolves.
    _cache(context, root / f"own-{id(context):x}.db").get_text(_KEY)


def _reached(event, future):
    """Wait until ``event`` is set; fail at once if ``future`` ends first. The guard only stops a deadlocked test."""
    deadline = monotonic() + _HANG_GUARD
    while not event.wait(0.05):
        if future.done():
            future.result()
            pytest.fail("the worker finished before it reached its pause point")
        assert monotonic() < deadline, "the worker did not reach its pause point"


class _HeldFirstDrop:
    """Hold the first database drop inside the drop listeners, after the hidden stores' listener, until released."""

    def __init__(self, monkeypatch):
        self.entered, self.release = threading.Event(), threading.Event()
        self.reached = 0

        @contextmanager
        def hold(spec, db, paths):
            self.reached += 1
            if self.reached == 1:
                self.entered.set()
                assert self.release.wait(_HANG_GUARD)
            yield

        monkeypatch.setattr(DatabaseEngineRegistry, "_drop_listeners", [*DatabaseEngineRegistry._drop_listeners, hold])


class _HeldFileRemoval:
    """Hold the first physical removal of a database file, after the drop took the engines on it, until released."""

    def __init__(self, monkeypatch):
        self.entered, self.release = threading.Event(), threading.Event()
        self.first = True
        drop_database = database_module.drop_database

        def slow(spec, *args, **kwargs):
            # Stands for a slow file system or database server.
            if self.first:
                self.first = False
                self.entered.set()
                assert self.release.wait(_HANG_GUARD)
            return drop_database(spec, *args, **kwargs)

        monkeypatch.setattr(database_module, "drop_database", slow)


@pytest.mark.slow
def test_two_drops_of_one_database_from_one_context_run_one_after_another(tmp_path, monkeypatch):
    shared = tmp_path / "orders.db"
    with closing(_context(tmp_path / "ctx")) as context:
        _ready(context, tmp_path)
        first, second = _facade(context, shared), _facade(context, shared)
        first.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)", readonly=False)
        held = _HeldFirstDrop(monkeypatch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(first.drop_database)
            try:
                _reached(held.entered, running)
                waiting = pool.submit(second.drop_database)
                threading.Event().wait(1.0)
                # The second drop waits for the first instead of failing.
                assert not waiting.done()
                assert held.reached == 1
            finally:
                held.release.set()
            running.result(timeout=_HANG_GUARD)
            waiting.result(timeout=_HANG_GUARD)
        assert held.reached == 2
        assert not shared.exists()


@pytest.mark.slow
def test_a_workspace_the_dropping_context_opens_while_the_drop_removes_the_database_fails_at_once(tmp_path, monkeypatch):
    shared = tmp_path / "notes.db"
    with closing(_context(tmp_path / "ctx")) as context:
        _ready(context, tmp_path)
        facade = _facade(context, shared)
        facade.execute("CREATE TABLE drafts (id INTEGER PRIMARY KEY, body TEXT)", readonly=False)
        held = _HeldFileRemoval(monkeypatch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(facade.drop_database)
            try:
                _reached(held.entered, running)
                opening = pool.submit(HeavenBase, "team-notes", context=context, backends=_backend(shared), detached=True)
                # The workspace creates its facade under the Context lock, which a thread holding a transaction on
                # the database may wait for while the drop waits for that transaction, so it fails instead of waiting.
                with pytest.raises(RuntimeError, match="Database is being dropped by this Context"):
                    opening.result(timeout=60)
            finally:
                held.release.set()
            running.result(timeout=_HANG_GUARD)
        # After the drop, the workspace opens the database as the drop left it.
        notes = HeavenBase("team-notes", context=context, backends=_backend(shared), detached=True)
        notes.register(TeamNote)
        notes.upsert(TeamNote, {"object_id": "n1", "title": "written after the drop"})
        assert notes.get("n1", TeamNote)["title"] == "written after the drop"
        assert "drafts" not in _facade(context, shared).tables()


@pytest.mark.slow
def test_a_facade_the_dropping_context_creates_during_the_drop_waits_and_opens_the_new_database(tmp_path, monkeypatch):
    shared = tmp_path / "orders.db"
    with closing(_context(tmp_path / "ctx")) as context:
        _ready(context, tmp_path)
        dropping = _facade(context, shared)
        dropping.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)", readonly=False)
        held = _HeldFileRemoval(monkeypatch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(dropping.drop_database)
            try:
                _reached(held.entered, running)
                # A thread that holds no HeavenBase lock and no connection to the database waits for the drop.
                making = pool.submit(lambda: _facade(context, shared).tables())
                threading.Event().wait(1.0)
                assert not making.done()
            finally:
                held.release.set()
            running.result(timeout=_HANG_GUARD)
            assert making.result(timeout=_HANG_GUARD) == []
        assert not shared.exists() or _facade(context, shared).tables() == []


@pytest.mark.slow
@pytest.mark.parametrize("hold", ["listeners", "file removal"])
def test_an_engine_use_of_the_dropping_context_runs_or_waits_for_the_removal(tmp_path, monkeypatch, hold):
    shared = tmp_path / "orders.db"
    with closing(_context(tmp_path / "ctx")) as context:
        _ready(context, tmp_path)
        dropping, reading = _facade(context, shared), _facade(context, shared)
        dropping.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)", readonly=False)
        assert reading.tables() == ["orders"]
        # Held while the listeners detach the hidden stores, before the drop takes the cached engines, or after it
        # took them, while it removes the file.
        held = _HeldFirstDrop(monkeypatch) if hold == "listeners" else _HeldFileRemoval(monkeypatch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(dropping.drop_database)
            try:
                _reached(held.entered, running)
                using = pool.submit(lambda: reading.execute("SELECT COUNT(*) FROM orders").to_lists())
                if hold == "listeners":
                    # The drop has not closed the scope yet: the facade still reads the database.
                    assert using.result(timeout=60) == [[0]]
                else:
                    # The facade waits for the removal, also through the engine it cached before the drop.
                    threading.Event().wait(1.0)
                    assert not using.done()
            finally:
                held.release.set()
            running.result(timeout=_HANG_GUARD)
            if hold == "file removal":
                # Then it fails as the facades the drop retired do.
                with pytest.raises(RuntimeError, match="Engine has been disposed"):
                    using.result(timeout=_HANG_GUARD)
        # The drop retired the facades this Context had on the database, as any drop does.
        with pytest.raises(RuntimeError, match="Engine has been disposed"):
            reading.execute("SELECT 1")
        assert _facade(context, shared).tables() == []


@pytest.mark.slow
@pytest.mark.skipif(os.name == "nt", reason="Windows does not remove a SQLite file that another thread's connection holds open, also in 0.1.2.2")
def test_a_call_of_the_dropping_context_from_a_transaction_on_the_database_fails_at_once(tmp_path, monkeypatch):
    shared = tmp_path / "orders.db"
    with closing(_context(tmp_path / "ctx")) as context:
        _ready(context, tmp_path)
        dropping, ordering = _facade(context, shared), _facade(context, shared)
        dropping.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)", readonly=False)
        held = _HeldFileRemoval(monkeypatch)
        in_transaction, go = threading.Event(), threading.Event()

        def read_then_clone():
            with ordering.tx():
                ordering.execute("SELECT COUNT(*) FROM orders")
                in_transaction.set()
                assert go.wait(_HANG_GUARD)
                # The drop's statement may wait for this transaction (as MySQL's does), so this cannot wait for it.
                ordering.clone()

        with ThreadPoolExecutor(max_workers=2) as pool:
            calling = pool.submit(read_then_clone)
            _reached(in_transaction, calling)
            running = pool.submit(dropping.drop_database)
            try:
                _reached(held.entered, running)
                go.set()
                with pytest.raises(RuntimeError, match="cannot wait for the drop"):
                    calling.result(timeout=60)
            finally:
                go.set()
                held.release.set()
            running.result(timeout=_HANG_GUARD)
        assert not shared.exists()


class _PausedHit:
    """Pause the flagged thread's LLM cache hit bookkeeping after its admission, until released."""

    def __init__(self, monkeypatch):
        self.thread = None
        self.admitted, self.go = threading.Event(), threading.Event()
        touch_rows = LLMCache._touch_rows

        def paused(cache, workspace, entity, hits):
            # Stands for bookkeeping that is slow to reach its engine, such as one queued on the backend lock.
            if threading.get_ident() == self.thread:
                self.admitted.set()
                assert self.go.wait(_HANG_GUARD)
            return touch_rows(cache, workspace, entity, hits)

        monkeypatch.setattr(LLMCache, "_touch_rows", paused)

    def hit(self, cache):
        self.thread = threading.get_ident()
        return cache.get_text(_KEY)


@pytest.mark.slow
def test_a_cache_hit_of_the_dropping_context_and_the_drop_both_end(tmp_path, monkeypatch):
    shared = tmp_path / "cache.db"
    with closing(_context(tmp_path / "ctx")) as context:
        cache = _cache(context, shared)
        cache.set_text(_KEY, [], _ANSWER, [], policy="deterministic")
        assert cache.get_text(_KEY)["response"] == _ANSWER
        store = cache._store
        notes = HeavenBase("team-notes", context=context, backends=_backend(tmp_path / "notes.db"), detached=True)
        notes.register(TeamNote)
        facade = _facade(context, shared)
        paused = _PausedHit(monkeypatch)
        held = _HeldFileRemoval(monkeypatch)
        draining = threading.Event()
        detach = HiddenWorkspaceStore._detach

        def watched_detach(detached, workspace, *, wait=True):
            if detached is store and wait:
                draining.set()
            return detach(detached, workspace, wait=wait)

        monkeypatch.setattr(HiddenWorkspaceStore, "_detach", watched_detach)
        pool = ThreadPoolExecutor(max_workers=3)
        try:
            hitting = pool.submit(paused.hit, cache)
            _reached(paused.admitted, hitting)
            dropping = pool.submit(facade.drop_database)
            # Before it removes anything, the drop waits for the bookkeeping admitted on its own Context's store.
            assert draining.wait(60), "the drop did not wait for the admitted bookkeeping"
            threading.Event().wait(0.5)
            assert not held.entered.is_set()
            paused.go.set()
            # The drop has not closed its scope while it waits for the bookkeeping, so the hit ends normally.
            assert hitting.result(timeout=60)["response"] == _ANSWER
            # Then the drop goes on, removes the file and ends, and the Context writes again.
            _reached(held.entered, dropping)
            held.release.set()
            dropping.result(timeout=60)
            pool.submit(notes.upsert, TeamNote, {"object_id": "n1", "title": "written after the drop"}).result(timeout=60)
        finally:
            paused.go.set()
            held.release.set()
            # A failed run would leave the drop waiting for the admitted bookkeeping: end that wait.
            for _, done in list(store._admitted):
                done.set()
            pool.shutdown(wait=True)
        assert notes.get("n1", TeamNote)["title"] == "written after the drop"
        assert cache.get_text(_KEY) is None


@pytest.mark.slow
def test_a_cache_drop_of_the_dropping_context_waits_for_its_running_drop(tmp_path, monkeypatch):
    shared = tmp_path / "cache.db"
    with closing(_context(tmp_path / "ctx")) as context:
        cache = _cache(context, shared)
        cache.set_text(_KEY, [], _ANSWER, [], policy="deterministic")
        held = _HeldFirstDrop(monkeypatch)
        waiting = threading.Event()
        resetter = {}
        drops_during = hidden_store._drops_during

        def watched(mark, workspace=None):
            overlapping = drops_during(mark, workspace)
            # The cache's store, which the running drop detached, has built a new workspace on the file meanwhile and
            # now waits for the running drop to end; its build no longer uses the file.
            if overlapping and threading.get_ident() == resetter.get("thread"):
                waiting.set()
            return overlapping

        monkeypatch.setattr(hidden_store, "_drops_during", watched)

        def reset():
            resetter["thread"] = threading.get_ident()
            LLMCache.reset(drop=True, config_manager=context.config)

        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(_facade(context, shared).drop_database)
            try:
                _reached(held.entered, running)
                resetting = pool.submit(reset)
                # The cache's drop reopens its store first, which builds on the file, and then waits for the running
                # drop. The running drop goes on only then, so it removes the file while no build has it open (Windows
                # does not remove a file that a connection holds open).
                _reached(waiting, resetting)
                assert not resetting.done()
            finally:
                held.release.set()
            running.result(timeout=_HANG_GUARD)
            # The cache's own drop ran after the other one instead of failing.
            resetting.result(timeout=_HANG_GUARD)
        assert LLMCache.current(config_manager=context.config).get_text(_KEY) is None


@pytest.mark.slow
def test_the_dropping_contexts_cache_answers_until_its_drop_detaches_it(tmp_path, monkeypatch):
    shared = tmp_path / "cache.db"
    with closing(_context(tmp_path / "dropper")) as dropper, closing(_context(tmp_path / "reader")) as reader:
        mine, theirs = _cache(dropper, shared), _cache(reader, shared)
        mine.set_text(_KEY, [], _ANSWER, [], policy="deterministic")
        assert theirs.get_text(_KEY)["response"] == _ANSWER
        facade = _facade(dropper, shared)
        entered, release = threading.Event(), threading.Event()
        same_paths = DatabaseEngineRegistry._same_paths.__func__
        dropping = {}

        def slow_comparison(cls, spec):
            # Stands for comparing many recorded paths with the dropped file, at the start of the drop.
            if threading.get_ident() == dropping.get("thread"):
                entered.set()
                assert release.wait(_HANG_GUARD)
            return same_paths(cls, spec)

        monkeypatch.setattr(DatabaseEngineRegistry, "_same_paths", classmethod(slow_comparison))

        def drop():
            dropping["thread"] = threading.get_ident()
            facade.drop_database()

        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(drop)
            try:
                _reached(entered, running)
                # The drop has not detached the stores or closed its scope yet: both Contexts' caches still answer.
                assert mine.get_text(_KEY)["response"] == _ANSWER
                assert theirs.get_text(_KEY)["response"] == _ANSWER
            finally:
                release.set()
            running.result(timeout=_HANG_GUARD)
        assert [mine.get_text(_KEY), theirs.get_text(_KEY)] == [None, None]


@pytest.mark.slow
@pytest.mark.skipif(not hasattr(os, "link"), reason="needs hard links")
@pytest.mark.parametrize("second", ["drop_database", "drop"])
def test_drops_of_one_file_through_two_of_its_names_run_one_after_another(tmp_path, monkeypatch, second):
    orders = tmp_path / "orders.db"
    with closing(_context(tmp_path / "first")) as first, closing(_context(tmp_path / "other")) as other:
        _ready(first, tmp_path)
        _ready(other, tmp_path)
        creating = _facade(first, orders)
        creating.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)", readonly=False)
        creating.dispose()
        # Another name of the same file, as a hard link or another letter case on a case-insensitive file system is.
        alias = tmp_path / "orders-link.db"
        os.link(orders, alias)
        through_alias = _facade(other, alias)
        assert through_alias.tables() == ["orders"]
        held = _HeldFirstDrop(monkeypatch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(_facade(first, orders).drop_database)
            try:
                _reached(held.entered, running)
                waiting = pool.submit(getattr(through_alias, second))
                threading.Event().wait(1.0)
                # The drop through the other name has not started: it waits for the running drop of the file.
                assert held.reached == 1
                assert not waiting.done()
            finally:
                held.release.set()
            running.result(timeout=_HANG_GUARD)
            waiting.result(timeout=_HANG_GUARD)
        assert held.reached == 2


@pytest.mark.slow
@pytest.mark.skipif(not hasattr(os, "link"), reason="needs hard links")
def test_a_drop_through_another_name_waits_also_after_that_name_is_gone(tmp_path, monkeypatch):
    orders = tmp_path / "orders.db"
    with closing(_context(tmp_path / "first")) as first, closing(_context(tmp_path / "other")) as other:
        _ready(first, tmp_path)
        _ready(other, tmp_path)
        creating = _facade(first, orders)
        creating.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)", readonly=False)
        creating.dispose()
        alias = tmp_path / "orders-link.db"
        os.link(orders, alias)
        through_alias = _facade(other, alias)
        assert through_alias.tables() == ["orders"]
        held = _HeldFirstDrop(monkeypatch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(_facade(first, orders).drop_database)
            try:
                _reached(held.entered, running)
                # The other name is removed meanwhile, as another letter case of a name on a case-insensitive file
                # system is when the drop removes the file: it no longer tells which file it named. Its idle
                # connection is closed first, since Windows does not remove a file that a connection holds open.
                through_alias.dispose()
                alias.unlink()
                waiting = pool.submit(_facade(other, alias).drop_database)
                threading.Event().wait(1.0)
                # The drop through the other name still waits for the running drop of the file it named.
                assert held.reached == 1
                assert not waiting.done()
            finally:
                held.release.set()
            running.result(timeout=_HANG_GUARD)
            waiting.result(timeout=_HANG_GUARD)
        assert held.reached == 2
        assert not orders.exists()
