from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event
from time import monotonic

import pytest

import heavenbase as hb
from heavenbase.llm import cache as cache_module
from heavenbase.llm.cache import LLMCache, LLMTextEntry
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast
# Seconds. Only stops a deadlocked test; no passing run depends on it.
_HANG_GUARD = 600
_KEY = {"object_id": "text-row", "spec_key": "spec", "request_key": "request", "args": {}}


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


def _cache(context, *, pragmas=None, **paths):
    cache = LLMCache.current(config_manager=context.config)
    cache.config["backend"] = {route: {"type": "sqlite", "database": hb.utils.path_to_file_uri(path)} for route, path in paths.items()}
    if pragmas is not None:
        for backend in cache.config["backend"].values():
            backend["pragmas"] = pragmas
    return cache


def _write(cache, value):
    cache.set_text(_KEY, [], {"text": value}, [], policy="deterministic")


def _reached(event, worker):
    """Wait until a worker thread reaches its pause point; fail at once if it ends first.

    Opening a SQLite cache workspace can take many seconds on a loaded test host,
    so no step waits for a fixed time. The guard only stops a deadlocked test.
    """
    deadline = monotonic() + _HANG_GUARD
    while not event.wait(0.05):
        if worker.done():
            worker.result()
            pytest.fail("the worker finished before it reached its pause point")
        assert monotonic() < deadline, "the worker did not reach its pause point"


@pytest.mark.slow
def test_drop_leaves_fresh_row_intact_during_an_older_hit(tmp_path, monkeypatch):
    shared = tmp_path / "Cache.db"
    with closing(_context(tmp_path / "first")) as first, closing(_context(tmp_path / "second")) as second:
        primary = _cache(first, main=shared)
        peer = _cache(second, main=shared)
        _write(primary, "old")
        workspace = peer.workspace
        ready, resume = Event(), Event()
        get = workspace.get

        def paused_get(*args, **kwargs):
            old = get(*args, **kwargs)
            ready.set()
            # The main thread always releases this wait in its finally block.
            assert resume.wait(_HANG_GUARD)
            return old

        monkeypatch.setattr(workspace, "get", paused_get)
        with ThreadPoolExecutor(max_workers=1) as executor:
            hit = executor.submit(peer.get_text, _KEY)
            try:
                _reached(ready, hit)
                LLMCache.reset(drop=True, config_manager=first.config)
                _write(primary, "fresh")
                assert primary.get_text(_KEY)["response"] == {"text": "fresh"}
            finally:
                resume.set()
            assert hit.result(timeout=_HANG_GUARD)["response"] == {"text": "old"}
        assert primary.get_text(_KEY)["response"] == {"text": "fresh"}


@pytest.mark.slow
@pytest.mark.parametrize("clock", ["real", "same-tick"])
def test_drop_waits_for_an_admitted_hit_write_on_a_peer(tmp_path, monkeypatch, clock):
    if clock == "same-tick":
        # Both writes share one creation time, as with a coarse clock or a column that rounds it.
        monkeypatch.setattr(cache_module, "time", lambda: 1_763_700_000.25)
    shared = tmp_path / "shared.db"
    with closing(_context(tmp_path / "first")) as first, closing(_context(tmp_path / "second")) as second:
        primary = _cache(first, main=shared)
        peer = _cache(second, main=shared)
        _write(primary, "old")
        assert peer.workspace is not None
        entered, resume, draining = Event(), Event(), Event()
        write = peer._touch_rows

        class AdmittedWrites(set):
            # The drop lists the peer's admitted hit writes right before it waits for them.
            def __iter__(self):
                if self:
                    draining.set()
                return super().__iter__()

        def paused_write(*args, **kwargs):
            entered.set()
            assert resume.wait(_HANG_GUARD)
            return write(*args, **kwargs)

        def reset_and_replace():
            LLMCache.reset(drop=True, config_manager=first.config)
            _write(primary, "fresh")

        monkeypatch.setattr(peer._store, "_admitted", AdmittedWrites(getattr(peer._store, "_admitted", ())), raising=False)
        monkeypatch.setattr(peer, "_touch_rows", paused_write)
        with ThreadPoolExecutor(max_workers=2) as pool:
            hit = pool.submit(peer.get_text, _KEY)
            try:
                _reached(entered, hit)
                reset = pool.submit(reset_and_replace)
                _reached(draining, reset)
                # The admitted write cannot finish before resume, so the drop is still waiting for it.
                assert not reset.done()
            finally:
                resume.set()
            assert hit.result(timeout=_HANG_GUARD)["response"] == {"text": "old"}
            reset.result(timeout=_HANG_GUARD)
        row = primary.workspace.get(_KEY["object_id"], entity=LLMTextEntry)
        assert row["response"] == {"text": "fresh"}
        assert row["n_hits"] == 0


@pytest.mark.slow
@pytest.mark.parametrize("alias_mode", ["same", "case", "hardlink"])
def test_cache_drop_identifies_peer_file_aliases(tmp_path, alias_mode):
    shared = tmp_path / "Cache.db"
    alias = tmp_path / "cache.db" if alias_mode == "case" else shared
    # Hardlinks have distinct WAL sidecars. Use rollback journals for this probe.
    pragmas = ["PRAGMA journal_mode = DELETE;"] if alias_mode == "hardlink" else None
    with closing(_context(tmp_path / "first")) as first, closing(_context(tmp_path / "second")) as second:
        primary = _cache(first, main=shared, pragmas=pragmas)
        _write(primary, "old")
        if alias_mode == "hardlink":
            alias = tmp_path / "alias.db"
            try:
                alias.hardlink_to(shared)
            except OSError as error:
                pytest.skip(f"Filesystem does not support hardlinks: {error}")
        elif not alias.exists():
            pytest.skip("Filesystem distinguishes differently cased paths")
        peer = _cache(second, main=alias, pragmas=pragmas)
        old_workspace = peer.workspace
        LLMCache.reset(drop=True, config_manager=first.config)
        assert not shared.exists()
        if alias_mode == "hardlink":
            assert alias.is_file()
        else:
            _write(primary, "fresh")
            assert peer.get_text(_KEY)["response"] == {"text": "fresh"}
        assert peer.workspace is not old_workspace


@pytest.mark.slow
def test_peer_reopens_after_non_dropping_reset_and_later_drop(tmp_path):
    shared = tmp_path / "shared.db"
    with closing(_context(tmp_path / "first")) as first, closing(_context(tmp_path / "second")) as second:
        primary = _cache(first, main=shared)
        peer = _cache(second, main=shared)
        _write(primary, "old")
        assert peer.get_text(_KEY)["response"] == {"text": "old"}
        LLMCache.reset(config_manager=second.config)
        LLMCache.reset(drop=True, config_manager=first.config)
        _write(primary, "fresh")
        assert peer.get_text(_KEY)["response"] == {"text": "fresh"}


@pytest.mark.slow
def test_peer_drop_still_reaches_its_independent_route(tmp_path):
    shared, independent = tmp_path / "shared.db", tmp_path / "independent.db"
    with closing(_context(tmp_path / "first")) as first, closing(_context(tmp_path / "second")) as second:
        primary = _cache(first, main=shared)
        peer = _cache(second, main=shared, archive=independent)
        _write(primary, "old")
        peer.workspace.backends.get("archive").db.execute("CREATE TABLE archive_marker (name TEXT)")
        assert independent.exists()
        LLMCache.reset(drop=True, config_manager=first.config)
        assert independent.exists()
        LLMCache.reset(drop=True, config_manager=second.config)
        assert not independent.exists()
