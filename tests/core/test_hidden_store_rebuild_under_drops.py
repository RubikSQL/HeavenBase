"""Drops that keep coming do not postpone a hidden store's rebuild indefinitely.

A drop of a database that a hidden store's build used makes the store discard
that build and build again. The next drop of that database waits until the
store has built again, so a store whose builds take longer than the gap
between drops still opens, instead of building again after every drop.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import threading
from time import monotonic

import pytest

import heavenbase as hb
from heavenbase.llm.cache import LLMCache
from heavenbase.utils.config import ConfigManager
from heavenbase.workspace import hidden_store
from heavenbase.workspace.hidden_store import HiddenWorkspaceStore

# Each case builds several workspaces (10 to 22 s), so they run in the full tier.
pytestmark = [pytest.mark.full, pytest.mark.slow]

_KEY = {"object_id": "text-row", "spec_key": "spec", "request_key": "request", "args": {}}
_LATER = dict(_KEY, request_key="request-2", object_id="text-row-2")
_ANSWER = {"text": "summary of the quarterly report"}
# Seconds. Only stops a deadlocked test; no passing run depends on it.
_HANG_GUARD = 600
# Seconds the rebuild gives the next drop to run during it; the next drop now waits for the rebuild instead.
_DROP_WINDOW = 3.0
# The drop's wait for a postponed rebuild, raised so a slow build on a loaded host never outlasts it.
_GRACE = float(_HANG_GUARD)


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


def _cache(context, path):
    cache = LLMCache.current(config_manager=context.config)
    cache.config["backend"] = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(path)}}
    return cache


def _facade(context, path):
    return hb.Database(database=hb.utils.path_to_file_uri(path), provider="sqlite", resolver=context.modules(), config=context.config)


def _reached(event, future):
    """Wait until ``event`` is set, failing at once if ``future`` ended first."""
    deadline = monotonic() + _HANG_GUARD
    while not event.wait(0.05):
        if future.done():
            future.result()
            raise AssertionError("the worker ended before its pause point")
        assert monotonic() < deadline, "the worker did not reach its pause point"


def test_the_next_drop_lets_a_postponed_rebuild_finish(tmp_path, monkeypatch):
    shared = tmp_path / "shared.db"
    with closing(_context(tmp_path / "dropper")) as dropper, closing(_context(tmp_path / "reader")) as reader:
        _cache(dropper, shared).set_text(_KEY, [], _ANSWER, [], policy="deterministic")
        theirs = _cache(reader, shared)
        facade = _facade(dropper, shared)
        builds: list[int] = []
        first_paused, resume_first, rebuilding, second_dropped = (threading.Event() for _ in range(4))
        build = HiddenWorkspaceStore._build

        def paused_build(store):
            if store.context is reader:
                builds.append(len(builds) + 1)
                if len(builds) == 1:
                    first_paused.set()
                    assert resume_first.wait(_HANG_GUARD)
                elif len(builds) == 2:
                    rebuilding.set()
                    second_dropped.wait(_DROP_WINDOW)
            return build(store)

        def open_cache():
            try:
                return theirs.get_text(_KEY)
            except Exception as exc:  # a call that overlaps a drop may fail once
                return type(exc).__name__

        def drop_again():
            facade.drop_database()
            second_dropped.set()

        monkeypatch.setattr(HiddenWorkspaceStore, "_build", paused_build)
        monkeypatch.setattr(hidden_store, "_REBUILD_GRACE", _GRACE)
        with ThreadPoolExecutor(max_workers=2) as pool:
            opening = pool.submit(open_cache)
            try:
                _reached(first_paused, opening)
                facade.drop_database()  # runs during the first build, so the store builds again
                resume_first.set()
                _reached(rebuilding, opening)
                second = pool.submit(drop_again)
                outcome = opening.result(timeout=_HANG_GUARD)
                second.result(timeout=_HANG_GUARD)
            finally:
                resume_first.set()
                second_dropped.set()
        # Before the fix the second drop ran during the rebuild, which then built a third time.
        assert builds == [1, 2]
        assert outcome is None or outcome in {"DatabaseError", "OperationalError", "RuntimeError"}
        theirs.set_text(_LATER, [], _ANSWER, [], policy="deterministic")
        assert theirs.get_text(_LATER)["response"] == _ANSWER
