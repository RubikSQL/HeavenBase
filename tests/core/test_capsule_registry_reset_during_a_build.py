"""A Capsule registry facade whose build started before reset_capsule_registry() is not cached after it.

capsule_registry() builds a missing facade outside the registry lock, because the build takes the Context lock.
A reset that runs during the build retires that facade all the same: the call that built it gets it, as if it
had ended before the reset, and the next call builds a new one from the configuration the reset left.
"""

from contextlib import closing
import threading
import time
from weakref import WeakSet, WeakValueDictionary

import pytest

import heavenbase as hb
from heavenbase.capsule import registry as capsule_mod
from heavenbase.utils.config import ConfigManager

# Each case builds two workspaces and waits for a reset (about 10 s), so they run in the full tier.
pytestmark = [pytest.mark.full, pytest.mark.slow]

_HOOK: dict = {}


def add(left, right):
    return left + right


def _held_label(name):
    """Compute hook: runs inside the workspace write, under the Context lock, until the test releases it."""
    _HOOK["inside"].set()
    assert _HOOK["release"].wait(60)
    return name.upper()


class SlowNote(hb.Entity):
    identifier = "registry-reset-slow-note"

    name = hb.field(hb.ShortText)
    label = hb.field(hb.ShortText).compute(_held_label, inputs=["name"])


@pytest.fixture
def isolated_registry(monkeypatch):
    monkeypatch.setattr(capsule_mod, "_AUTHORITY_REGISTRIES", WeakValueDictionary())
    monkeypatch.setattr(capsule_mod, "_DEFAULT_REGISTRY", None)
    monkeypatch.setattr(capsule_mod, "_REGISTRY_CONTEXTS", WeakSet(), raising=False)


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


def test_a_facade_whose_build_waited_for_the_context_lock_is_not_served_after_a_reset(tmp_path, isolated_registry):
    with closing(_context(tmp_path / "ctx")) as context:
        resolver, config = context.modules(), context.config
        ws = hb.HeavenBase("registry-reset-slow", context=context, detached=True, backends={"main": {"type": "inmem"}})
        ws.register(SlowNote)
        _HOOK.update(inside=threading.Event(), release=threading.Event())
        built: dict = {}
        writer = threading.Thread(target=lambda: ws.upsert(SlowNote, {"object_id": "n1", "name": "first"}), daemon=True)
        builder = threading.Thread(target=lambda: built.update(registry=capsule_mod.capsule_registry(resolver=resolver, config=config)), daemon=True)
        resetting = threading.Thread(target=lambda: capsule_mod.reset_capsule_registry(resolver=resolver, config=config), daemon=True)
        writer.start()
        assert _HOOK["inside"].wait(60)  # the writer holds the Context lock in its compute hook
        builder.start()
        time.sleep(1.0)  # the builder's first registry call now waits for the Context lock inside its facade build
        resetting.start()
        resetting.join(5)
        _HOOK["release"].set()
        for thread in (writer, builder, resetting):
            thread.join(60)
            assert not thread.is_alive()
        after = capsule_mod.capsule_registry(resolver=resolver, config=config)
        # The reset retired every cached facade; one whose build began before it is not served after it.
        assert after is not built["registry"]


def test_a_registry_configuration_changed_before_a_reset_is_used_after_it(tmp_path, isolated_registry, monkeypatch):
    with closing(_context(tmp_path / "ctx")) as context:
        resolver, config = context.modules(), context.config
        paused, resume = threading.Event(), threading.Event()
        workspace_store = capsule_mod._workspace_store

        def paused_store(authority, registry_config):
            if threading.current_thread().name == "registry-builder":
                paused.set()
                assert resume.wait(60)
            return workspace_store(authority, registry_config)

        monkeypatch.setattr(capsule_mod, "_workspace_store", paused_store)
        built: dict = {}
        builder = threading.Thread(
            target=lambda: built.update(registry=capsule_mod.capsule_registry(resolver=resolver, config=config)), name="registry-builder", daemon=True
        )
        builder.start()
        assert paused.wait(60)  # the first registry call has read the configuration and is opening its store
        moved = hb.utils.path_to_file_uri(tmp_path / "registry-two.db")
        config.set("heavenbase.capsule.registry.backend", {"main": {"type": "sqlite", "name": "main", "database": moved}})
        resetting = threading.Thread(target=lambda: capsule_mod.reset_capsule_registry(resolver=resolver, config=config), daemon=True)
        resetting.start()
        resetting.join(5)
        resume.set()
        for thread in (builder, resetting):
            thread.join(60)
            assert not thread.is_alive()
        after = capsule_mod.capsule_registry(resolver=resolver, config=config)
        capsule_id = after.register(hb.Capsule(add, name="sum_two", namespace="demo", version="1", include_cloudpickle=False))
        capsule_mod.reset_capsule_registry(resolver=resolver, config=config)
        fresh = capsule_mod.capsule_registry(resolver=resolver, config=config)
        assert after.config["backend"]["main"]["database"] == moved
        assert fresh.store.get(capsule_id) is not None
