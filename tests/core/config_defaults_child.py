"""Fresh-process exercises for package config and log defaults."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch
import warnings

import heavenbase as hb
from heavenbase.utils import get_logger, pj, redirect_logs, restore_logs, set_log_level, suppress_logs
from heavenbase.utils.log import HbLogHandler


def emit() -> None:
    logger = get_logger("heavenbase.tests.config-defaults")
    logger.info("ordinary note")
    logger.warning("important note")


def log_isolation(work: Path) -> None:
    def child():
        return subprocess.run(
            [sys.executable, __file__, "emit", str(work)],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )

    log = work / "log.txt"
    try:
        suppress_logs()
        assert "important note" in child().stderr, "global suppression escaped the current process"
        restore_logs()
        set_log_level("ERROR")
        assert "ordinary note" in child().stderr, "global level escaped the current process"
        redirect_logs(log, level="INFO")
        emit()
        assert "important note" in log.read_text(encoding="utf-8")
        logger = get_logger("heavenbase.tests.config-defaults")
        handler = next(handler for handler in logger.handlers if isinstance(handler, HbLogHandler))
        restore_logs()
        assert handler.file_stream is None, "The test must close the cached handle before unlinking on Windows"
        log.unlink()
        assert "important note" in child().stderr, "global file routing escaped the current process"
        assert not log.exists()
    finally:
        restore_logs()


def config_recovery(case: str) -> None:
    home = Path.home()
    config_home = home / ".heavenbase"
    if case == "invalid":
        config_home.mkdir()
        bootstrap = config_home / "bootstrap.yaml"
        bootstrap.write_text("version: 2\n", encoding="utf-8")
    else:
        home.chmod(0o555)
    key = "heavenbase.entity.id.hash_length"
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert hb.CM_HVNB.get(key) == 42
            assert hb.CM_HVNB.load()["heavenbase"]["entity"]["id"]["hash_length"] == 42
            assert hb.CM_HVNB.refresh() is False
            assert hb.CM_HVNB.set(key, 31)
            assert hb.CM_HVNB.last_write_persisted is False
            assert hb.CM_HVNB.get(key) == 31
            assert hb.CM_HVNB.setdef("notes.labels", [])
            assert hb.CM_HVNB.last_write_persisted is False
            assert hb.CM_HVNB.set("notes.labels[]", "work")
            assert hb.CM_HVNB.get("notes.labels") == ("work",)
            assert hb.CM_HVNB.unset("notes.labels[0]")
            assert hb.CM_HVNB.get("notes.labels") == ()
            assert hb.CM_HVNB.init("heavenbase.team", data={"display": "compact"})
            assert hb.CM_HVNB.last_write_persisted is False
            assert hb.CM_HVNB.remove("heavenbase.team")
            assert hb.CM_HVNB.last_write_persisted is False
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(lambda: hb.CM_HVNB.last_write_persisted).result(timeout=5) is None
        assert sum("packaged" in str(item.message) for item in caught) == 1
    finally:
        if case == "invalid":
            bootstrap.unlink()
        else:
            home.chmod(0o755)
    assert hb.CM_HVNB.refresh() is False
    assert hb.CM_HVNB.get(key) == 31
    entered, resumed = threading.Event(), threading.Event()
    commit = hb.CM_HVNB._commit

    def wait_before_commit(change, *, registry=None):
        entered.set()
        assert resumed.wait(5)
        return commit(change, registry=registry)

    with patch.object(hb.CM_HVNB, "_commit", side_effect=wait_before_commit), ThreadPoolExecutor(max_workers=1) as pool:
        write = pool.submit(hb.CM_HVNB.set, "notes.display", "compact")
        try:
            assert entered.wait(5)
            assert hb.CM_HVNB.refresh(discard_process_changes=True) is False
        finally:
            resumed.set()
        assert write.result(timeout=5)
    assert hb.CM_HVNB.get("notes.display") == "compact"
    assert hb.CM_HVNB.refresh(discard_process_changes=True) is True
    assert hb.CM_HVNB.get(key) == 42
    assert hb.CM_HVNB._scope_registry.persistent
    assert hb.CM_HVNB.set(key, 32)
    assert hb.CM_HVNB.last_write_persisted is True
    assert hb.CM_HVNB.get(key) == 32


def home_spelling(operation: str) -> None:
    assert Path(pj("~", abs=True)) == Path.home().resolve()
    assert hb.CM_HVNB.pj("%", abs=True) == str(Path.home().resolve() / ".heavenbase")
    if operation == "write-home":
        assert hb.CM_HVNB.set("notes.display", "compact")
    else:
        assert hb.CM_HVNB.get("notes.display") == "compact"


if __name__ == "__main__":
    operation, root = sys.argv[1:3]
    if operation == "emit":
        emit()
    elif operation == "log":
        log_isolation(Path(root))
    elif operation in {"write-home", "read-home"}:
        home_spelling(operation)
    else:
        config_recovery(operation)
