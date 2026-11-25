from pathlib import PureWindowsPath

import pytest

from heavenbase.utils import log
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast


def test_snapshot_tracks_hyphenated_environment_name(monkeypatch):
    name = "HB_R8LLM_REPAIR-HOST"
    monkeypatch.setenv(name, "first.test")
    config = ConfigManager(package="heavenbase-test", default={"url": "https://${env:HB_R8LLM_REPAIR-HOST}/v1"})
    first = config.snapshot()
    assert first.get("url") == "https://first.test/v1"
    assert config.snapshot() is first
    monkeypatch.setenv(name, "second.test")
    assert config.snapshot().get("url") == "https://second.test/v1"


def test_saved_log_path_keeps_expanded_trailing_whitespace(tmp_path, monkeypatch):
    if log.os.name == "nt":
        pytest.skip("Windows only preserves trailing spaces in extended paths")
    expected = tmp_path / "trailing.log "
    monkeypatch.setenv("HB_R8LLM_REPAIR_LOG", str(expected))
    try:
        log.redirect_logs("$HB_R8LLM_REPAIR_LOG")
        log.get_logger("heavenbase.tests.log.literal").warning("message")
        assert expected.is_file()
    finally:
        log.restore_logs()


def test_saved_log_path_decodes_extended_drive_authority(monkeypatch):
    class WindowsPath(PureWindowsPath):
        def resolve(self):
            return self

    monkeypatch.setattr(log, "Path", WindowsPath)
    expected = WindowsPath(r"\\?\C:\logs\$HB_LITERAL\output.log")
    assert WindowsPath(log._log_path(expected.as_uri())) == expected


def test_noop_config_write_does_not_suppress_generation_refresh(monkeypatch):
    config = ConfigManager(package="heavenbase-test", default={"value": 7}, refresh_interval=0)
    chain_state = config._chain_state
    reloads = []

    def observe(*args, **kwargs):
        reloads.append(kwargs.get("reload"))
        return chain_state(*args, **kwargs)

    monkeypatch.setattr(config, "_chain_state", observe)
    snapshot = config.snapshot()
    assert config.compact() == 0
    reloads.clear()
    assert config.snapshot() is snapshot
    assert reloads == [True]
