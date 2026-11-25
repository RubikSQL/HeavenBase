"""Which hidden-store calls with a connection in use wait for a drop, and which fail at once.

A drop's statement or file removal may wait for a connection that another
thread has in use on the dropped database, such as a MySQL transaction whose
metadata lock ``DROP DATABASE`` waits for. If that thread then waited for the
drop, both would wait for each other, so such a call raises ``RuntimeError``.
A connection on another database cannot hold the drop up, so a call with one
waits for the drop as any call does. Here the drop is held inside the drop
listeners, so a call that waits for it ends only once the test releases it.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import threading
from time import monotonic

import pytest

import heavenbase as hb
from heavenbase.database.registry import DatabaseEngineRegistry
from heavenbase.llm.cache import LLMCache
from heavenbase.utils.config import ConfigManager

# Each case builds several workspaces and holds a drop for seconds (10 to 20 s), so they run in the full tier.
pytestmark = [pytest.mark.full, pytest.mark.slow]

_KEY = {"object_id": "text-row", "spec_key": "spec", "request_key": "request", "args": {}}
_ANSWER = {"text": "summary of the quarterly report"}
# Seconds. Only stops a deadlocked test; no passing run depends on it.
_HANG_GUARD = 600
# Seconds the drop is held while the call runs.
_HOLD = 4.0


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


def _hold_drops(monkeypatch):
    """Hold every database drop inside the drop listeners, after the hidden stores' listener recorded it."""
    entered, release = threading.Event(), threading.Event()

    @contextmanager
    def hold(spec, db, paths):
        entered.set()
        assert release.wait(_HANG_GUARD)
        yield

    monkeypatch.setattr(DatabaseEngineRegistry, "_drop_listeners", [*DatabaseEngineRegistry._drop_listeners, hold])
    return entered, release


def _run(monkeypatch, tmp_path, drop, statement_on):
    """Make a cache call inside a transaction while a held drop of the cache's database runs."""
    shared = tmp_path / "shared.db"
    with closing(_context(tmp_path / "dropper")) as dropper, closing(_context(tmp_path / "reader")) as reader:
        _cache(dropper, shared).set_text(_KEY, [], _ANSWER, [], policy="deterministic")
        theirs = _cache(reader, shared)
        assert theirs.get_text(_KEY)["response"] == _ANSWER
        if statement_on == "other":
            target = _facade(reader, tmp_path / "orders.db")
            target.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)")
            target.execute("INSERT INTO orders VALUES (1, 'desk')")
            statement = "UPDATE orders SET item = 'chair' WHERE id = 1"
        else:
            # Only a read on the cache's own file, so the cache's writes are not blocked by it.
            target = _facade(reader, shared)
            statement = "SELECT COUNT(*) FROM sqlite_master"
        entered, release = _hold_drops(monkeypatch)
        outcome: dict[str, object] = {}

        def call_in_a_transaction():
            try:
                with target.tx():
                    target.execute(statement)
                    outcome["result"] = theirs.get_text(_KEY)
                outcome["call"] = "returned"
            except Exception as exc:  # reported below
                outcome["call"] = exc

        def run_drop():
            if drop == "drop_database":
                _facade(dropper, shared).drop_database()
            else:
                LLMCache.reset(drop=True, config_manager=dropper.config)

        with ThreadPoolExecutor(max_workers=2) as pool:
            dropping = pool.submit(run_drop)
            try:
                assert entered.wait(_HANG_GUARD)
                calling = pool.submit(call_in_a_transaction)
                deadline = monotonic() + _HOLD
                while not calling.done() and monotonic() < deadline:
                    threading.Event().wait(0.05)
                outcome["ended while the drop was held"] = calling.done()
            finally:
                release.set()
            dropping.result(timeout=_HANG_GUARD)
            calling.result(timeout=_HANG_GUARD)
        # The cache opens again once the drop has ended, on fresh storage.
        outcome["after"] = theirs.get_text(_KEY)
        return outcome


@pytest.mark.parametrize("drop", ["drop_database", "cache_drop"])
def test_a_cache_call_with_a_transaction_on_another_database_waits_for_the_drop(tmp_path, monkeypatch, drop):
    outcome = _run(monkeypatch, tmp_path, drop, "other")
    # The drop cannot wait for a connection on orders.db, so the call waits for the drop and then finds the row gone.
    assert outcome["call"] == "returned", outcome
    assert outcome["ended while the drop was held"] is False, outcome
    assert outcome["result"] is None
    assert outcome["after"] is None


def test_a_cache_call_with_a_transaction_on_the_dropped_database_fails_at_once(tmp_path, monkeypatch):
    outcome = _run(monkeypatch, tmp_path, "cache_drop", "dropped")
    assert outcome["ended while the drop was held"] is True, outcome
    assert isinstance(outcome["call"], RuntimeError), outcome
    assert "on a database that a running drop removes" in str(outcome["call"])
    assert outcome["after"] is None
