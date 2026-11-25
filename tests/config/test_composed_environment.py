import pytest

from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast


def test_composed_environment_references_keep_scope_snapshots_cached(monkeypatch):
    monkeypatch.setenv("HB_R8LLM_HOST", "localhost")
    monkeypatch.setenv("HB_R8LLM_PORT", "8000")
    cfg = ConfigManager(package="heavenbase-test", default={"value": "kept"})
    with cfg.scoped("composed"):
        assert cfg.set("url", "http://${env:HB_R8LLM_HOST}:${env:HB_R8LLM_PORT,80}/service")
        first = cfg.snapshot()
        assert first.get("url") == "http://localhost:8000/service"
        assert cfg.snapshot() is first

        monkeypatch.setenv("HB_R8LLM_PORT", "9000")
        second = cfg.snapshot()
        assert second.get("url") == "http://localhost:9000/service"
        assert cfg.snapshot() is second
