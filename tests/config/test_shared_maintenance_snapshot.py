from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from heavenbase.utils.config import ConfigManager
from heavenbase.utils.registry import Registry

pytestmark = pytest.mark.fast


def _managers():
    options = {
        "package": "heavenbase-test",
        "scope": "shared",
        "default": {"value": "default"},
        "registry": Registry("shared-config"),
        "setup": False,
        "refresh_interval": 600.0,
    }
    return ConfigManager(**options), ConfigManager(**options)


@pytest.mark.parametrize("operation", ["compact", "remove"])
def test_noop_maintenance_observes_a_shared_registry_publication(operation):
    reader, writer = _managers()
    assert reader.set("value", "first")
    assert reader.get("value") == "first"
    assert writer.set("value", "second")
    assert reader.get("value") == "first"

    if operation == "compact":
        assert reader.compact(keep_last_k=100) == 0
    else:
        assert reader.remove(version=999) is False
    assert reader.get("value") == "second"


def test_noop_compaction_rechecks_ancestors_of_the_target_scope():
    reader, writer = _managers()
    assert writer.set("value", "first")
    with reader.scoped("reports"):
        assert reader.set("format", "table")
        assert reader.get("value") == "first"
        assert writer.set("value", "second", scope="shared")
        assert reader.compact(keep_last_k=100) == 0
        assert reader.get("value") == "second"
        assert reader.get("format") == "table"


def test_noop_compaction_invalidates_an_in_flight_generation_read(monkeypatch):
    reader, writer = _managers()
    assert writer.set("value", "first")
    entered, resume = Event(), Event()
    read_chain = reader._chain_state

    def pause_first_read(*args, **kwargs):
        state = read_chain(*args, **kwargs)
        if not entered.is_set():
            entered.set()
            assert resume.wait(15)
        return state

    monkeypatch.setattr(reader, "_chain_state", pause_first_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        read = pool.submit(reader.get, "value")
        try:
            assert entered.wait(10)
            assert writer.set("value", "second")
            assert reader.compact(keep_last_k=100) == 0
        finally:
            resume.set()
        assert read.result(timeout=10) == "second"
    assert reader.get("value") == "second"
