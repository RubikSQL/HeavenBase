"""A database drop reads the file system outside the engine registry lock and visits each cache once."""

from collections import OrderedDict

import pytest

import heavenbase as hb
from heavenbase.database import registry as registry_module
from heavenbase.database.registry import DatabaseEngineRegistry

pytestmark = pytest.mark.fast


class _CountingEngines(OrderedDict):
    """Engine cache that counts full iterations over its keys."""

    def __init__(self) -> None:
        super().__init__()
        self.scans = 0

    def __iter__(self):
        self.scans += 1
        return super().__iter__()


@pytest.fixture
def counted_registry(monkeypatch):
    engines = _CountingEngines()
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


def _spec(path):
    return hb.Database(provider="sqlite", database=path.as_uri()).spec


def test_a_drop_compares_files_outside_the_registry_lock(tmp_path, counted_registry, monkeypatch):
    dropped, other = _spec(tmp_path / "orders.db"), _spec(tmp_path / "invoices.db")
    for spec in (dropped, other):
        DatabaseEngineRegistry.get(spec, authority_id="tenant")
    held: list[bool] = []
    same_file = registry_module._same_file

    def compare(left, right):
        held.append(DatabaseEngineRegistry._lock.locked())
        return same_file(left, right)

    monkeypatch.setattr(registry_module, "_same_file", compare)
    with DatabaseEngineRegistry._dropping_database(dropped, authority_id="tenant", resource_id=None):
        pass

    # The other recorded file was compared with the dropped one, and never under the lock.
    assert held and not any(held)


def test_caching_a_new_file_engine_resolves_its_path_outside_the_registry_lock(tmp_path, counted_registry, monkeypatch):
    held: list[bool] = []
    realpath = registry_module.realpath

    def resolve(path):
        held.append(DatabaseEngineRegistry._lock.locked())
        return realpath(path)

    monkeypatch.setattr(registry_module, "realpath", resolve)
    DatabaseEngineRegistry.get(_spec(tmp_path / "orders.db"), authority_id="tenant")

    # The new engine's file was resolved for the path record, and never under the lock.
    assert held and not any(held)
    assert len(DatabaseEngineRegistry._paths) == 1


def test_a_drop_visits_the_engine_cache_a_fixed_number_of_times_whatever_the_number_of_contexts(tmp_path, counted_registry):
    spec = _spec(tmp_path / "orders.db")
    for index in range(8):
        DatabaseEngineRegistry.get(spec, authority_id=f"tenant-{index}")
    counted_registry.scans = 0

    with DatabaseEngineRegistry._dropping_database(spec, authority_id="tenant-0", resource_id=None):
        pass

    # Two visits before the drop and two after it, not one per Context holding an engine on the database.
    assert counted_registry.scans <= 4
    assert not counted_registry
