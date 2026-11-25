"""Fresh-process checks for config authority, selection, and recovery."""

import io
from pathlib import Path
import sys

import heavenbase as hb
from heavenbase.context import default_context, set_default_context
from heavenbase.utils import get_logger, redirect_logs, restore_logs, set_log_level

_KEY = "notes.display"
_INMEM = """version: 1
root: ~/.heavenbase
workspace: default
backend:
  type: inmem
  module: heavenbase.backends.inmem.backend
  name: system
"""


def bootstrap(text: str) -> Path:
    path = Path.home() / ".heavenbase" / "bootstrap.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def explicit_after_fallback() -> None:
    bootstrap("version: 2\n")
    assert hb.CM_HVNB.set(_KEY, "compact")
    assert hb.CM_HVNB.get(_KEY) == "compact"
    hb.configure_explicit_context()
    for read in (lambda: hb.CM_HVNB.get(_KEY), hb.CM_HVNB.load, hb.CM_HVNB.snapshot):
        try:
            read()
        except RuntimeError as error:
            assert "explicit context mode" in str(error).lower()
        else:
            raise AssertionError("explicit Context mode accepted a cached default value")


def bound_fallback() -> None:
    invalid = bootstrap("version: 2\n")
    assert hb.CM_HVNB.set(_KEY, "compact")
    assert hb.CM_HVNB.get(_KEY) == "compact"
    invalid.unlink()
    set_default_context(hb.Context.load(config=hb.CM_HVNB))
    assert hb.CM_HVNB.get(_KEY) is None
    assert hb.CM_HVNB.set(_KEY, "wide")
    assert hb.CM_HVNB.last_write_persisted is True


def reset_fallback() -> None:
    from heavenbase.context.reset import reset_default_local_state

    home = Path.home()
    home.chmod(0o555)
    try:
        assert hb.CM_HVNB.set(_KEY, "compact")
        assert hb.CM_HVNB.get(_KEY) == "compact"
        assert hb.CM_HVNB.last_write_persisted is False
    finally:
        home.chmod(0o755)
    reset_default_local_state(default_context())
    assert hb.CM_HVNB.get(_KEY) is None
    assert hb.CM_HVNB.set(_KEY, "wide")
    assert hb.CM_HVNB.last_write_persisted is True


def bad_registry_policy() -> None:
    import warnings

    bootstrap(_INMEM + "registry:\n  hash: sha512\n")
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        assert hb.CM_HVNB.get("heavenbase.entity.id.hash_length") == 42
        assert hb.CM_HVNB.set(_KEY, "compact")
        assert hb.CM_HVNB.last_write_persisted is False
    assert sum("packaged defaults" in str(item.message) for item in captured) == 1


def separate_registry() -> None:
    from heavenbase.utils.config import ConfigManager
    from heavenbase.utils.registry import Registry
    from heavenbase.utils.registry.store import SQLiteRegistryStore

    name = "independent-config"
    path = Path.home() / "owned-config.sqlite"
    manager = ConfigManager(registry=Registry.load(name, store=SQLiteRegistryStore(path)))
    assert manager.set(_KEY, "compact")
    context = hb.Context.load(home_path=bootstrap(_INMEM), config=manager)
    context.modules()
    assert manager.set(_KEY, "wide")
    mirror = ConfigManager(registry=Registry.load(name, store=SQLiteRegistryStore(path)))
    assert mirror.get(_KEY) == "wide"
    assert manager.last_write_persisted is True


def owned_sql() -> None:
    context = default_context()
    context._setup_modules()
    modules = context.modules()
    assert context.config is hb.CM_HVNB
    backend = hb.backends.SQLiteBackend("notes", ":memory:", config=hb.CM_HVNB)
    assert backend.config is hb.CM_HVNB and backend.module_resolver is modules
    assert backend.db._module_resolver is modules


def custom_default_log() -> None:
    context = set_default_context(hb.Context.load())
    assert context.config is not hb.CM_HVNB
    assert context.config.set("heavenbase.log.level", "WARNING")
    stream = io.StringIO()
    redirect_logs(stream)
    logger = get_logger("heavenbase.tests.saved-log")
    try:
        set_log_level("ERROR", persist=True)
        assert context.config.get("heavenbase.log.level") == "ERROR"
        assert context.config.last_write_persisted is True
        logger.warning("filtered")
        logger.error("visible")
        assert "visible" in stream.getvalue() and "filtered" not in stream.getvalue()
        set_log_level("DEBUG")
        assert context.config.get("heavenbase.log.level") == "ERROR"
        logger.debug("local-debug")
        assert "local-debug" in stream.getvalue()
    finally:
        restore_logs()


def recover_to_inmem() -> None:
    bootstrap("version: 2\n")
    assert hb.CM_HVNB.set(_KEY, "compact")
    bootstrap(_INMEM)
    assert hb.CM_HVNB.refresh(discard_process_changes=True) is False
    assert hb.CM_HVNB.get(_KEY) == "compact"
    assert hb.CM_HVNB.set(_KEY, "wide")
    assert hb.CM_HVNB.last_write_persisted is False


_CASES = {
    "explicit": explicit_after_fallback,
    "bind": bound_fallback,
    "reset": reset_fallback,
    "hash-policy": bad_registry_policy,
    "independent-store": separate_registry,
    "owned-sql": owned_sql,
    "log-store": custom_default_log,
    "refresh-to-inmem": recover_to_inmem,
}

if __name__ == "__main__":
    _CASES[sys.argv[1]]()
