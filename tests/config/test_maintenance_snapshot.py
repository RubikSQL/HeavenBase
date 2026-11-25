import pytest

from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("operation", ["compact", "remove"])
def test_noop_config_maintenance_preserves_snapshot(operation):
    cfg = ConfigManager(package="heavenbase-test", default={"value": "original"})
    assert cfg.set("value", "current")
    first = cfg.snapshot()
    if operation == "compact":
        assert cfg.compact(keep_last_k=100) == 0
    else:
        assert cfg.remove(version=999) is False

    assert cfg.snapshot() is first
    assert cfg.remove()
    assert cfg.snapshot().get("value") == "original"
