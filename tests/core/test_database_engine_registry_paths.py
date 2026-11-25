"""The engine registry's local-file path bookkeeping stays cheap and keeps drops reaching path aliases."""

import os
import sqlite3
from collections import OrderedDict

import pytest

import heavenbase as hb
from heavenbase.database.registry import DatabaseEngineRegistry

pytestmark = pytest.mark.fast

_CAP = 16


class _CountingEngines(OrderedDict):
    """Engine cache that counts full iterations over its keys."""

    def __init__(self) -> None:
        super().__init__()
        self.scans = 0

    def __iter__(self):
        self.scans += 1
        return super().__iter__()


@pytest.fixture
def small_registry(monkeypatch):
    engines = _CountingEngines()
    monkeypatch.setattr(DatabaseEngineRegistry, "_CACHE_MAX", _CAP)
    monkeypatch.setattr(DatabaseEngineRegistry, "_engines", engines)
    monkeypatch.setattr(DatabaseEngineRegistry, "_schema_indices", OrderedDict())
    monkeypatch.setattr(DatabaseEngineRegistry, "_schema_builds", {})
    monkeypatch.setattr(DatabaseEngineRegistry, "_disposed", set())
    monkeypatch.setattr(DatabaseEngineRegistry, "_dropping", set())
    monkeypatch.setattr(DatabaseEngineRegistry, "_paths", {})
    monkeypatch.setattr(DatabaseEngineRegistry, "_paths_limit", 0, raising=False)
    yield engines
    for engine in list(engines.values()):
        engine.dispose()


def _open(path):
    database = hb.Database(provider="sqlite", database=path.as_uri())
    _ = database.engine
    return database


@pytest.mark.slow
def test_opening_new_files_with_a_full_cache_does_not_scan_the_cache_per_recorded_path(tmp_path, small_registry):
    opened = [_open(tmp_path / f"tenant-{index}.db") for index in range(_CAP)]
    small_registry.scans = 0

    later = 8 * _CAP
    opened += [_open(tmp_path / f"tenant-{_CAP + index}.db") for index in range(later)]

    # Each new file evicts one engine. Bookkeeping that rescans every cached engine
    # for every recorded path walks the cache about _CAP times per new file.
    assert small_registry.scans <= later // 4
    assert len(DatabaseEngineRegistry._paths) <= 2 * _CAP
    assert len(small_registry) == _CAP


@pytest.mark.slow
def test_a_drop_reaches_an_engine_opened_through_a_hard_link_while_other_files_cycle_the_cache(tmp_path, small_registry):
    path = tmp_path / "orders.db"
    sqlite3.connect(path).close()
    alias = tmp_path / "orders-alias.db"
    try:
        os.link(path, alias)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Cannot create a hard link: {exc}")
    clerk = _open(alias)
    stale = clerk.engine
    others = []
    for index in range(8 * _CAP):
        others.append(_open(tmp_path / f"other-{index}.db"))
        # Keep the clerk's engine the most recently used, so only the other files are evicted.
        assert clerk.engine is stale

    owner = _open(path)
    owner.drop_database()

    assert not path.exists()
    assert stale not in small_registry.values()
    assert clerk.engine is not stale


def test_dispose_all_and_dispose_authority_forget_recorded_paths(tmp_path, small_registry):
    first = _open(tmp_path / "first.db")
    assert DatabaseEngineRegistry._paths
    DatabaseEngineRegistry.dispose_authority(first._authority_id)
    assert not DatabaseEngineRegistry._paths

    _open(tmp_path / "second.db")
    assert DatabaseEngineRegistry._paths
    DatabaseEngineRegistry.dispose_all()
    assert not DatabaseEngineRegistry._paths
