"""Regression coverage for Context reset and temporary config ownership."""

from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from weakref import WeakSet

import pytest

import heavenbase as hb
from heavenbase.context import load_bootstrap, runtime
from heavenbase.context.reset import reset_default_local_state
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast

_SORT = "preferences.note_sort"


def _sqlite_context(root, *, packaged_config=False):
    backend = load_bootstrap(home_path=root / "missing.yaml").backend
    config = ConfigManager(root=str(root), setup=True) if packaged_config else ConfigManager(root=str(root), default={})
    return hb.Context.load({"version": 1, "root": str(root), "workspace": "default", "backend": backend}, config=config)


def _hidden_backend(context):
    store = context._hidden_store({"workspace": "team-notes", "backends": {"main": {"type": "inmem"}}})
    return store.workspace.backends.get("main")


@pytest.mark.slow
def test_failed_hidden_drop_preserves_the_sqlite_control_database(tmp_path, monkeypatch):
    context = _sqlite_context(tmp_path)
    assert context.config.set(_SORT, "updated")
    path = tmp_path / "system.db"
    assert path.is_file()
    backend = _hidden_backend(context)

    def fail_drop():
        raise OSError("note storage is unavailable")

    monkeypatch.setattr(backend, "drop", fail_drop)
    try:
        with pytest.raises(RuntimeError, match="note storage is unavailable"):
            reset_default_local_state(context)
        assert path.is_file()
        assert context.config.get(_SORT) == "updated"
    finally:
        context.close()

    reopened = _sqlite_context(tmp_path)
    try:
        assert reopened.config.get(_SORT) == "updated"
    finally:
        reopened.close()


def test_hidden_drop_resolves_settings_in_its_workspace_scope(tmp_path, monkeypatch):
    context = _sqlite_context(tmp_path)
    manager = context.config
    notes_scope = f"{manager.base_scope}.team-notes"
    maintenance_scope = f"{manager.base_scope}.maintenance"
    with manager.scoped(notes_scope):
        assert manager.set("notes.collection", "team-notes")
    with manager.scoped(maintenance_scope):
        assert manager.set("notes.collection", "maintenance-notes")
    backend = _hidden_backend(context)
    original_drop = backend.drop
    observed = []

    def drop():
        observed.append((manager.scope, manager.get("notes.collection")))
        original_drop()

    monkeypatch.setattr(backend, "drop", drop)
    try:
        with manager.scoped(maintenance_scope):
            reset_default_local_state(context)
            assert manager.scope == maintenance_scope
        assert observed == [(notes_scope, "team-notes")]
    finally:
        context.close()


def test_config_owner_created_during_temporary_default_persists_later_writes(tmp_path, home_env):
    home = tmp_path / "home"
    home.mkdir()
    command = [sys.executable, str(Path(__file__).resolve())]
    kwargs = dict(capture_output=True, text=True, timeout=30, env=home_env(home))
    for args in (["write", str(tmp_path / "temporary")], ["read"]):
        result = subprocess.run(command + args, **kwargs)
        assert result.returncode == 0, result.stderr


def _write_config(temporary_root):
    temporary = hb.Context.load(
        {
            "version": 1,
            "root": str(temporary_root),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
        }
    )
    assert runtime._default_context_instance() is None
    with patch.object(runtime, "DEFAULT_CONTEXT", temporary):
        assert hb.CM_HVNB.set("preferences.note_view", "compact")
        owner = hb.Context.load(config=hb.CM_HVNB)
        assert runtime.default_context() is temporary
    assert runtime.default_context() is owner
    assert hb.CM_HVNB.get("preferences.note_view") is None
    assert hb.CM_HVNB.set(_SORT, "updated")
    assert hb.CM_HVNB.get(_SORT) == "updated"
    temporary.close()
    owner.close()


if __name__ == "__main__":
    if sys.argv[1] == "write":
        _write_config(Path(sys.argv[2]))
    else:
        config = runtime.default_context().config
        value = config.get(_SORT)
        assert value == "updated", value


def _failing_note_store(context, monkeypatch):
    backend = _hidden_backend(context)

    def fail_drop():
        raise OSError("note storage is unavailable")

    monkeypatch.setattr(backend, "drop", fail_drop)


def _recorded_store(context, workspace):
    store = context._hidden_store({"workspace": workspace, "backends": {"main": {"type": "inmem"}}})
    backend = store.workspace.backends.get("main")
    original_drop = backend.drop
    dropped = []

    def drop():
        dropped.append(workspace)
        original_drop()

    backend.drop = drop
    return store, dropped


@pytest.mark.slow
def test_cached_llm_calls_work_after_a_failed_hidden_drop(tmp_path, monkeypatch):
    from heavenbase.llm.cache import LLMCache

    context = _sqlite_context(tmp_path, packaged_config=True)
    try:
        _failing_note_store(context, monkeypatch)
        # The first client keeps the Context's shared LLM cache alive.
        llm = hb.LLM(preset="mock", context=context)
        assert llm.chat("hello") == "mock response"
        store = llm._cache._store
        with pytest.raises(RuntimeError, match="note storage is unavailable"):
            reset_default_local_state(context)

        assert store.retired
        assert hb.LLM(preset="mock", context=context).chat("hello") == "mock response"
    finally:
        LLMCache.reset(drop=True, config_manager=context.config)
        context.close()


@pytest.mark.slow
def test_reset_drops_and_retires_every_hidden_store_after_a_failed_drop(tmp_path, monkeypatch):
    context = _sqlite_context(tmp_path)
    try:
        _failing_note_store(context, monkeypatch)
        store, dropped = _recorded_store(context, "reading-list")

        with pytest.raises(RuntimeError, match="note storage is unavailable"):
            reset_default_local_state(context)

        assert dropped == ["reading-list"]
        assert store.retired
    finally:
        context.close()


@pytest.mark.slow
def test_capsule_reset_drops_and_retires_every_hidden_store_after_a_failed_drop(tmp_path, monkeypatch):
    from heavenbase.capsule.registry import reset_capsule_registry

    context = _sqlite_context(tmp_path)
    try:
        _failing_note_store(context, monkeypatch)
        store, dropped = _recorded_store(context, "reading-list")

        with pytest.raises(RuntimeError, match="note storage is unavailable"):
            reset_capsule_registry(drop=True, resolver=context.modules(), config=context.config)

        assert dropped == ["reading-list"]
        assert store.retired
        replacement = context._hidden_store({"workspace": "reading-list", "backends": {"main": {"type": "inmem"}}})
        assert replacement is not store
    finally:
        context.close()


@pytest.mark.slow
def test_several_failed_hidden_drops_raise_one_combined_error(tmp_path, monkeypatch):
    context = _sqlite_context(tmp_path)
    try:
        _failing_note_store(context, monkeypatch)
        reading = context._hidden_store({"workspace": "reading-list", "backends": {"main": {"type": "inmem"}}})

        def fail_reading_drop():
            raise OSError("reading storage is unavailable")

        monkeypatch.setattr(reading.workspace.backends.get("main"), "drop", fail_reading_drop)
        store, dropped = _recorded_store(context, "recipes")

        with pytest.raises(RuntimeError, match="note storage is unavailable.*reading storage is unavailable") as raised:
            reset_default_local_state(context)

        assert "note storage is unavailable" in str(raised.value.__cause__)
        assert dropped == ["recipes"]
        assert reading.retired and store.retired
    finally:
        context.close()


@pytest.mark.slow
def test_process_wide_capsule_reset_drops_every_context_after_a_failed_drop(tmp_path, monkeypatch):
    from heavenbase.capsule import registry as capsule_registry

    # Keep the process-wide reset to the two Contexts built here.
    monkeypatch.setattr(capsule_registry, "_AUTHORITY_REGISTRIES", {})
    monkeypatch.setattr(capsule_registry, "_DEFAULT_REGISTRY", None)
    monkeypatch.setattr(runtime, "_ROOT_CONTEXTS", WeakSet())
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first = _sqlite_context(tmp_path / "first")
    second = _sqlite_context(tmp_path / "second")
    try:
        _failing_note_store(first, monkeypatch)
        store, dropped = _recorded_store(second, "reading-list")
        for context in (first, second):
            capsule_registry.capsule_registry(resolver=context.modules(), config=context.config)

        with pytest.raises(RuntimeError, match="note storage is unavailable"):
            capsule_registry.reset_capsule_registry(drop=True)

        assert dropped == ["reading-list"]
        assert store.retired
    finally:
        first.close()
        second.close()
