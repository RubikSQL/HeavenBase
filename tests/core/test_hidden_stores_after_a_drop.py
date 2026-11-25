"""What a drop does to the LLM caches and Capsule registries of other Contexts on the dropped database."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
import sqlite3
import threading
from time import monotonic
from weakref import WeakSet, WeakValueDictionary

import pytest

import heavenbase as hb
from heavenbase.capsule import registry as capsule_registry
from heavenbase.context import runtime
from heavenbase.context.reset import reset_default_local_state
from heavenbase.database.registry import DatabaseEngineRegistry
from heavenbase.llm.cache import LLMCache, LLMImageEntry
from heavenbase.utils.config import ConfigManager
from heavenbase.workspace.hidden_store import HiddenWorkspaceStore
from heavenbase.workspace.runtime import HeavenBase

pytestmark = pytest.mark.fast

_KEY = {"object_id": "text-row", "spec_key": "spec", "request_key": "request", "args": {}}
_LATER = dict(_KEY, request_key="request-2", object_id="text-row-2")
_ANSWER = {"text": "summary of the quarterly report"}
# Seconds. Only stops a deadlocked test; no passing run depends on it.
_HANG_GUARD = 600


def add(left, right):
    return left + right


@pytest.fixture
def isolated_registry(monkeypatch):
    """Keep process-wide Capsule resets to the Contexts each test builds."""
    monkeypatch.setattr(capsule_registry, "_AUTHORITY_REGISTRIES", WeakValueDictionary())
    monkeypatch.setattr(capsule_registry, "_DEFAULT_REGISTRY", None)
    monkeypatch.setattr(runtime, "_ROOT_CONTEXTS", WeakSet())


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


def _cache(context, path=None):
    cache = LLMCache.current(config_manager=context.config)
    if path is not None:
        cache.config["backend"] = _backend(path)
    return cache


def _write(cache, key=_KEY):
    cache.set_text(key, [], _ANSWER, [], policy="deterministic")


def _registry(context, path):
    return capsule_registry.capsule_registry({"backend": _backend(path)}, resolver=context.modules(), config=context.config)


def _capsule(name="sum_two"):
    return hb.Capsule(add, name=name, namespace="demo", version="1", include_cloudpickle=False)


def _reader_hit(root, path, key):
    with closing(_context(root)) as reader:
        hit = _cache(reader, path).get_text(key)
        return None if hit is None else hit["response"]


@pytest.mark.slow
def test_a_context_reset_reopens_the_llm_cache_of_another_context_on_the_same_file(tmp_path, isolated_registry):
    shared = tmp_path / "cache.db"
    with closing(_context(tmp_path / "ctx")) as context, closing(_context(tmp_path / "other")) as other:
        theirs = _cache(other, shared)
        _write(theirs)
        mine = _cache(context, shared)
        _write(mine)

        reset_default_local_state(context)

        # The reset dropped the file: the other Context's cache reopens on fresh storage, with no stale hit and no error.
        assert [theirs.get_text(_KEY) for _ in range(3)] == [None, None, None]
        assert LLMCache.current(config_manager=other.config).get_text(_KEY) is None
        _write(theirs, _LATER)
        assert theirs.get_text(_LATER)["response"] == _ANSWER
    assert _reader_hit(tmp_path / "reader", shared, _LATER) == _ANSWER


@pytest.mark.slow
def test_a_context_reset_reopens_another_context_on_its_default_cache_file(tmp_path, isolated_registry):
    # Two Contexts over one root share the default cache file. The resetting Context keeps its own cache rows
    # elsewhere, so the reset deletes the default file without dropping a store on it; the other Context must
    # not keep serving the deleted rows.
    root = tmp_path / "shared-root"
    with closing(_context(root)) as context, closing(_context(root)) as other:
        theirs = _cache(other)
        _write(theirs)
        assert theirs.get_text(_KEY)["response"] == _ANSWER
        _write(_cache(context, tmp_path / "elsewhere.db"))

        reset_default_local_state(context)

        assert theirs.get_text(_KEY) is None
        _write(theirs, _LATER)
        assert theirs.get_text(_LATER)["response"] == _ANSWER


@pytest.mark.slow
def test_a_capsule_drop_reopens_the_capsule_registry_of_another_context_on_the_same_file(tmp_path, isolated_registry):
    shared = tmp_path / "registry.db"
    with closing(_context(tmp_path / "ctx")) as context, closing(_context(tmp_path / "other")) as other:
        theirs = _registry(other, shared)
        theirs.register(_capsule())
        _registry(context, shared).list()

        capsule_registry.reset_capsule_registry(drop=True, resolver=context.modules(), config=context.config)

        # The drop deleted the file: the other Context's registry reopens on fresh storage, without the dropped Capsule.
        assert [len(_registry(other, shared).list()) for _ in range(3)] == [0, 0, 0]
        assert len(theirs.list()) == 0
        capsule_id = theirs.register(_capsule("sum_again"))
        assert theirs.load(capsule_id) is not None
    with closing(_context(tmp_path / "reader")) as reader:
        assert [row["name"] for row in _registry(reader, shared).list()] == ["sum_again"]


@pytest.mark.slow
def test_a_database_drop_reopens_the_hidden_stores_of_every_context(tmp_path, isolated_registry):
    shared = tmp_path / "shared.db"
    with closing(_context(tmp_path / "ctx")) as context, closing(_context(tmp_path / "other")) as other:
        cache = _cache(other, shared)
        _write(cache)
        registry = _registry(other, shared)
        registry.register(_capsule())
        local = _cache(context, shared)
        assert local.get_text(_KEY)["response"] == _ANSWER

        hb.Database(database=hb.utils.path_to_file_uri(shared), provider="sqlite", resolver=context.modules(), config=context.config).drop_database()

        for each in (cache, local):
            assert each.get_text(_KEY) is None
        assert registry.list() == []
        _write(cache, _LATER)
        assert local.get_text(_LATER)["response"] == _ANSWER
    assert _reader_hit(tmp_path / "reader", shared, _LATER) == _ANSWER


@pytest.mark.slow
def test_a_table_drop_reopens_the_llm_cache_of_another_context(tmp_path, isolated_registry):
    shared = tmp_path / "shared.db"
    with closing(_context(tmp_path / "ctx")) as context, closing(_context(tmp_path / "other")) as other:
        cache = _cache(other, shared)
        _write(cache)
        _cache(context, shared).get_text(_KEY)

        hb.Database(database=hb.utils.path_to_file_uri(shared), provider="sqlite", resolver=context.modules(), config=context.config).drop()

        # The tables are gone, so the other Context's cache recreates them instead of failing on every call.
        assert cache.get_text(_KEY) is None
        _write(cache, _LATER)
        assert cache.get_text(_LATER)["response"] == _ANSWER


def _reached(event, future):
    """Wait until ``event`` is set; fail at once if ``future`` ends first. The guard only stops a deadlocked test."""
    deadline = monotonic() + _HANG_GUARD
    while not event.wait(0.05):
        if future.done():
            future.result()
            pytest.fail("the worker finished before it reached its pause point")
        assert monotonic() < deadline, "the worker did not reach its pause point"


class _PauseBeforeAttach:
    """Pause the flagged thread after its cache store created its tables, before the store attaches the workspace."""

    def __init__(self, monkeypatch):
        self.thread = None
        self.entered = threading.Event()
        self.resume = threading.Event()
        register = HeavenBase.register

        def paused(workspace, entity, *args, **kwargs):
            result = register(workspace, entity, *args, **kwargs)
            if threading.get_ident() == self.thread and entity is LLMImageEntry:
                self.thread = None
                self.entered.set()
                assert self.resume.wait(_HANG_GUARD)
            return result

        monkeypatch.setattr(HeavenBase, "register", paused)

    def first_call(self, cache):
        self.thread = threading.get_ident()
        return cache.get_text(_KEY)


@pytest.mark.slow
@pytest.mark.parametrize("drop", ["drop_database", "drop", "workspace"])
def test_a_cache_that_opens_during_a_drop_by_another_context_works_afterwards(tmp_path, monkeypatch, isolated_registry, drop):
    shared = tmp_path / "shared.db"
    with closing(_context(tmp_path / "dropper")) as dropper, closing(_context(tmp_path / "reader")) as reader:
        _write(_cache(dropper, shared))
        theirs = _cache(reader, shared)
        notes = HeavenBase("team-notes", context=dropper, backends=_backend(shared), detached=True) if drop == "workspace" else None
        pause = _PauseBeforeAttach(monkeypatch)
        with ThreadPoolExecutor(max_workers=1) as pool:
            opening = pool.submit(pause.first_call, theirs)
            try:
                _reached(pause.entered, opening)
                # None of these drops coordinates with hidden stores that are opening.
                if notes is not None:
                    notes.drop()
                else:
                    facade = hb.Database(database=hb.utils.path_to_file_uri(shared), provider="sqlite", resolver=dropper.modules(), config=dropper.config)
                    getattr(facade, drop)()
            finally:
                pause.resume.set()
            # The opening store saw the drop, so it opened fresh storage instead of the tables the drop removed.
            assert opening.result(timeout=_HANG_GUARD) is None
        assert [theirs.get_text(_KEY) for _ in range(3)] == [None, None, None]
        _write(theirs, _LATER)
        assert theirs.get_text(_LATER)["response"] == _ANSWER
        assert LLMCache.current(config_manager=reader.config).get_text(_LATER)["response"] == _ANSWER


class _HeldDrop:
    """Hold every database drop inside the drop listeners, after the hidden stores' listener recorded it."""

    def __init__(self, monkeypatch):
        self.entered, self.release = threading.Event(), threading.Event()

        @contextmanager
        def hold(spec, db, paths):
            self.entered.set()
            assert self.release.wait(_HANG_GUARD)
            yield

        monkeypatch.setattr(DatabaseEngineRegistry, "_drop_listeners", [*DatabaseEngineRegistry._drop_listeners, hold])


def _facade(context, path):
    return hb.Database(database=hb.utils.path_to_file_uri(path), provider="sqlite", resolver=context.modules(), config=context.config)


@pytest.mark.slow
def test_a_cache_built_while_a_drop_runs_waits_for_the_drop_and_builds_again(tmp_path, monkeypatch, isolated_registry):
    shared = tmp_path / "shared.db"
    with closing(_context(tmp_path / "dropper")) as dropper, closing(_context(tmp_path / "reader")) as reader:
        _write(_cache(dropper, shared))
        theirs = _cache(reader, shared)
        facade = _facade(dropper, shared)
        pause = _PauseBeforeAttach(monkeypatch)
        held = _HeldDrop(monkeypatch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            opening = pool.submit(pause.first_call, theirs)
            try:
                _reached(pause.entered, opening)
                drop = pool.submit(facade.drop)
                _reached(held.entered, drop)
                pause.resume.set()
                # The workspace was built before the drop removed its tables, so the store waits for the drop to end.
                threading.Event().wait(1.0)
                assert not opening.done()
            finally:
                pause.resume.set()
                held.release.set()
            drop.result(timeout=_HANG_GUARD)
            assert opening.result(timeout=_HANG_GUARD) is None
        _write(theirs, _LATER)
        assert theirs.get_text(_LATER)["response"] == _ANSWER


@pytest.mark.slow
def test_a_cache_whose_build_fails_during_a_drop_opens_once_the_drop_ends(tmp_path, monkeypatch, isolated_registry):
    shared = tmp_path / "shared.db"
    with closing(_context(tmp_path / "dropper")) as dropper, closing(_context(tmp_path / "reader")) as reader:
        _write(_cache(dropper, shared))
        theirs = _cache(reader, shared)
        facade = _facade(dropper, shared)
        held = _HeldDrop(monkeypatch)
        builds: list[str] = []
        failed = threading.Event()
        build = HiddenWorkspaceStore._build

        def failing_while_the_drop_runs(store):
            if store.context is reader:
                if not held.release.is_set():
                    builds.append("failed")
                    failed.set()
                    # What SQLite reports to a build whose file a drop removes meanwhile.
                    raise sqlite3.OperationalError("disk I/O error")
                builds.append("built")
            return build(store)

        monkeypatch.setattr(HiddenWorkspaceStore, "_build", failing_while_the_drop_runs)
        with ThreadPoolExecutor(max_workers=2) as pool:
            drop = pool.submit(facade.drop_database)
            try:
                _reached(held.entered, drop)
                opening = pool.submit(theirs.get_text, _KEY)
                _reached(failed, opening)
                # The build failed while the drop ran, so the store waits for the drop to end and builds again.
                threading.Event().wait(1.0)
                assert not opening.done()
            finally:
                held.release.set()
            drop.result(timeout=_HANG_GUARD)
            assert opening.result(timeout=_HANG_GUARD) is None
        assert builds == ["failed", "built"]
        _write(theirs, _LATER)
        assert theirs.get_text(_LATER)["response"] == _ANSWER


@pytest.mark.slow
def test_drops_of_one_database_from_two_contexts_run_one_after_another(tmp_path, monkeypatch, isolated_registry):
    shared = tmp_path / "shared.db"
    with closing(_context(tmp_path / "first")) as first, closing(_context(tmp_path / "second")) as second:
        for context in (first, second):
            _write(_cache(context, shared))
        held = _HeldDrop(monkeypatch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            removing = pool.submit(_facade(first, shared).drop_database)
            try:
                _reached(held.entered, removing)
                held.entered.clear()
                tables = pool.submit(_facade(second, shared).drop)
                threading.Event().wait(1.0)
                # The table drop waits for the file drop instead of connecting to the file it is removing.
                assert not held.entered.is_set()
                assert not tables.done()
            finally:
                held.release.set()
            removing.result(timeout=_HANG_GUARD)
            tables.result(timeout=_HANG_GUARD)
        for context in (first, second):
            cache = _cache(context, shared)
            assert cache.get_text(_KEY) is None
            _write(cache, _LATER)
            assert cache.get_text(_LATER)["response"] == _ANSWER


@pytest.mark.slow
def test_an_llm_cache_drop_waits_for_a_store_opening_on_its_storage_configuration(tmp_path, monkeypatch, isolated_registry):
    shared = tmp_path / "shared.db"
    with closing(_context(tmp_path / "dropper")) as dropper, closing(_context(tmp_path / "reader")) as reader:
        _write(_cache(dropper, shared))
        theirs = _cache(reader, shared)
        pause = _PauseBeforeAttach(monkeypatch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            opening = pool.submit(pause.first_call, theirs)
            try:
                _reached(pause.entered, opening)
                reset = pool.submit(LLMCache.reset, drop=True, config_manager=dropper.config)
                # The drop needs the open lock of the storage configuration the reader is opening.
                threading.Event().wait(1.0)
                assert not reset.done()
            finally:
                pause.resume.set()
            opening.result(timeout=_HANG_GUARD)
            reset.result(timeout=_HANG_GUARD)
        assert [theirs.get_text(_KEY) for _ in range(3)] == [None, None, None]
        _write(theirs, _LATER)
        assert theirs.get_text(_LATER)["response"] == _ANSWER


@pytest.mark.slow
def test_a_hidden_store_opens_while_another_context_opens_one_on_other_storage(tmp_path, monkeypatch, isolated_registry):
    with closing(_context(tmp_path / "slow")) as slow, closing(_context(tmp_path / "fast")) as fast:
        # Build both module catalogs first, so only the hidden-store openings remain.
        slow.modules(), fast.modules()
        entered, release = threading.Event(), threading.Event()
        construct = HeavenBase._construct

        def held(cls, *args, **kwargs):
            if kwargs.get("context") is slow:
                # Stands for a slow or unreachable backend.
                entered.set()
                assert release.wait(_HANG_GUARD)
            return construct(*args, **kwargs)

        monkeypatch.setattr(HeavenBase, "_construct", classmethod(held))
        cache = _cache(fast, tmp_path / "fast-cache.db")
        with ThreadPoolExecutor(max_workers=2) as pool:
            opening = pool.submit(lambda: _registry(slow, tmp_path / "slow-registry.db").list())
            try:
                _reached(entered, opening)
                first = pool.submit(lambda: (_write(cache), cache.get_text(_KEY))[1])
                # Raises TimeoutError if the first cache call waits for the other Context's opening.
                assert first.result(timeout=120)["response"] == _ANSWER
            finally:
                release.set()
            assert opening.result(timeout=_HANG_GUARD) == []


class OpenNote(hb.Entity):
    identifier = "hidden-store-open-note"
    title = hb.field(hb.ShortText)


@pytest.mark.slow
def test_a_store_waiting_for_a_drop_does_not_hold_up_the_writes_of_its_context(tmp_path, monkeypatch, isolated_registry):
    shared = tmp_path / "shared.db"
    with closing(_context(tmp_path / "dropper")) as dropper, closing(_context(tmp_path / "reader")) as reader:
        _write(_cache(dropper, shared))
        notes = HeavenBase("team-notes", context=reader, backends=_backend(tmp_path / "notes.db"), detached=True)
        notes.register(OpenNote)
        theirs = _cache(reader, shared)
        facade = _facade(dropper, shared)
        pause = _PauseBeforeAttach(monkeypatch)
        held = _HeldDrop(monkeypatch)
        with ThreadPoolExecutor(max_workers=3) as pool:
            opening = pool.submit(pause.first_call, theirs)
            try:
                _reached(pause.entered, opening)
                drop = pool.submit(facade.drop_database)
                _reached(held.entered, drop)
                pause.resume.set()
                # The store was built before the drop removed its tables, so it waits for the drop to end.
                threading.Event().wait(1.0)
                assert not opening.done()
                writing = pool.submit(notes.upsert, OpenNote, {"object_id": "n1", "title": "written while the store waits"})
                # Raises TimeoutError if the write waits for the store that waits for the drop.
                writing.result(timeout=120)
            finally:
                pause.resume.set()
                held.release.set()
            drop.result(timeout=_HANG_GUARD)
            assert opening.result(timeout=_HANG_GUARD) is None
        assert notes.get("n1", OpenNote)["title"] == "written while the store waits"


@pytest.mark.slow
def test_a_store_reopening_during_a_drop_of_its_own_context_does_not_hold_up_the_contexts_writes(tmp_path, monkeypatch, isolated_registry):
    shared = tmp_path / "shared.db"
    with closing(_context(tmp_path / "ctx")) as context:
        cache = _cache(context, shared)
        _write(cache)
        notes = HeavenBase("team-notes", context=context, backends=_backend(tmp_path / "notes.db"), detached=True)
        notes.register(OpenNote)
        facade = _facade(context, shared)
        held = _HeldDrop(monkeypatch)
        with ThreadPoolExecutor(max_workers=3) as pool:
            drop = pool.submit(facade.drop_database)
            try:
                _reached(held.entered, drop)
                # The drop detached the cache's store, so this call reopens it. The build fails on the drop's closed
                # scope, and the store waits for the drop to end outside the Context lock.
                opening = pool.submit(cache.get_text, _KEY)
                threading.Event().wait(1.0)
                assert not opening.done()
                writing = pool.submit(notes.upsert, OpenNote, {"object_id": "n1", "title": "written while the store reopens"})
                # Raises TimeoutError if the write waits for the store that waits for the drop.
                writing.result(timeout=120)
            finally:
                held.release.set()
            drop.result(timeout=_HANG_GUARD)
            assert opening.result(timeout=_HANG_GUARD) is None
        assert notes.get("n1", OpenNote)["title"] == "written while the store reopens"


class _CountingLock:
    """Reentrant lock that counts how often it is taken."""

    def __init__(self, counter):
        self._lock = threading.RLock()
        self._counter = counter

    def __enter__(self):
        self._counter[0] += 1
        return self._lock.__enter__()

    def __exit__(self, *exc):
        return self._lock.__exit__(*exc)

    def acquire(self, *args, **kwargs):
        self._counter[0] += 1
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        return self._lock.release()


@pytest.mark.slow
@pytest.mark.parametrize("drop", [False, True])
def test_a_process_wide_cache_reset_takes_each_cache_lock_once(tmp_path, drop):
    count = 6
    with ExitStack() as stack:
        caches = []
        for index in range(count):
            context = stack.enter_context(closing(_context(tmp_path / f"ctx-{index}")))
            cache = _cache(context, tmp_path / f"cache-{index}.db")
            _write(cache)
            caches.append(cache)
        taken = [0]
        for cache in caches:
            cache._lock = _CountingLock(taken)

        LLMCache.reset(drop=drop)

        # One Context's reset used to take the lock of every live cache: count squared in all.
        assert taken[0] <= 2 * count
        assert all(cache._store is None for cache in caches)


_DROP_KINDS = ("cache", "capsule", "database", "tables", "workspace")
_DROPS_PER_KIND = 6
# A round fails when another dropper removes the same file while it runs, as any call on a dropped file can.
_ROUND_ATTEMPTS = 20


# Minutes long (about 5 minutes alone), so it runs in the full tier.
@pytest.mark.full
def test_hidden_stores_of_several_contexts_recover_from_repeated_drops_under_traffic(tmp_path, isolated_registry):
    """Cache and Capsule traffic in several Contexts keeps running while other threads drop the shared files.

    The drops are LLM cache and Capsule registry drops, ``Database.drop_database()``, ``Database.drop()`` and an
    ordinary workspace drop, so stores also open while drops that HeavenBase does not coordinate run. A dropper's
    round, such as building the ordinary workspace it then drops, can fail while another dropper removes the same
    file; it is tried again, and every round must succeed.
    """
    cache_file, registry_file = tmp_path / "cache.db", tmp_path / "registry.db"
    contexts = [_context(tmp_path / f"ctx-{index}") for index in range(2 + len(_DROP_KINDS))]
    stop = threading.Event()
    errors: list[BaseException] = []
    drops = []
    collisions: list[str] = []

    def traffic(context, index):
        cache = _cache(context, cache_file)
        key = dict(_KEY, object_id=f"row-{index}", request_key=f"request-{index}")
        while not stop.is_set():
            try:
                _write(cache, key)
                cache.get_text(key)
                _registry(context, registry_file).list()
            except Exception:  # a call already running when a drop starts may fail; later calls must not
                pass

    def drop_round(context, kind, round_):
        target = registry_file if round_ % 2 else cache_file
        if kind == "cache":
            _cache(context, cache_file).get_text(_KEY)
            LLMCache.reset(drop=True, config_manager=context.config)
        elif kind == "capsule":
            _registry(context, registry_file).list()
            capsule_registry.reset_capsule_registry(drop=True, resolver=context.modules(), config=context.config)
        elif kind == "workspace":
            notes = hb.HeavenBase(f"team-notes-{round_}", context=context, backends=_backend(target), detached=True)
            notes.drop()
        else:
            facade = hb.Database(database=hb.utils.path_to_file_uri(target), provider="sqlite", resolver=context.modules(), config=context.config)
            facade.drop_database() if kind == "database" else facade.drop()

    def dropper(context, kind):
        try:
            # The Context's own cache use registers the SQLite dialect that a Database facade resolves.
            _cache(context, tmp_path / f"own-{kind}.db").get_text(_KEY)
            for round_ in range(_DROPS_PER_KIND):
                for _ in range(_ROUND_ATTEMPTS):
                    try:
                        drop_round(context, kind, round_)
                        break
                    except Exception as error:  # another dropper removed the file meanwhile
                        collisions.append(f"{kind} {round_}: {type(error).__name__}: {str(error).splitlines()[0][:120]}")
                        if isinstance(error, PermissionError):  # Windows removes no file that another thread has open
                            stop.wait(0.1)
                else:
                    raise AssertionError(f"{kind} round {round_} failed {_ROUND_ATTEMPTS} times: {collisions[-3:]}")
                drops.append(kind)
        except BaseException as error:  # reported below
            errors.append(error)

    workers = [threading.Thread(target=traffic, args=(context, index), daemon=True) for index, context in enumerate(contexts[:2])]
    droppers = [threading.Thread(target=dropper, args=(context, kind), daemon=True) for context, kind in zip(contexts[2:], _DROP_KINDS)]
    try:
        for thread in (*workers, *droppers):
            thread.start()
        for thread in droppers:
            thread.join(_HANG_GUARD)
            assert not thread.is_alive(), "a drop did not finish"
    finally:
        stop.set()
        for thread in workers:
            thread.join(_HANG_GUARD)
    try:
        assert not any(thread.is_alive() for thread in workers), "cache traffic did not finish"
        assert errors == []
        assert len(drops) == len(_DROP_KINDS) * _DROPS_PER_KIND
        # Every Context's cache and Capsule registry works after the drops.
        for index, context in enumerate(contexts):
            cache = _cache(context, cache_file)
            key = dict(_KEY, object_id=f"after-{index}", request_key=f"after-{index}")
            _write(cache, key)
            assert cache.get_text(key)["response"] == _ANSWER
            registry = _registry(context, registry_file)
            capsule_id = registry.register(_capsule(f"after_{index}"))
            assert registry.load(capsule_id) is not None
    finally:
        for context in contexts:
            context.close()
