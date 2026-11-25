"""A dropping reset of the LLM cache or the Capsule registry removes the storage its configuration names.

The rows a closed Context wrote stay on disk. A later Context over the same root that has not opened that
storage yet, as at the start of a new process, must still drop them.
"""

from contextlib import closing
import gc
import os
from pathlib import Path
import subprocess
import sys
import threading
import weakref
from weakref import WeakSet, WeakValueDictionary

import pytest

import heavenbase as hb
from heavenbase.capsule import registry as capsule_registry
from heavenbase.context import runtime
from heavenbase.llm.cache import LLMCache
from heavenbase.utils.config import ConfigManager

_KEY = {"object_id": "text-row", "spec_key": "spec", "request_key": "request", "args": {}}
_ANSWER = {"text": "summary of the quarterly report"}
_CHILD = Path(__file__).with_name("reset_drop_child.py")


def add(left, right):
    return left + right


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    """Keep the process-wide resets of this module to the Contexts each test builds.

    Without it a process-wide drop also reaches the worker's default Context and
    removes Capsules that rows registered by other tests still name.
    """
    process_roots = runtime._ROOT_CONTEXTS
    test_roots: WeakSet = WeakSet()
    monkeypatch.setattr(capsule_registry, "_AUTHORITY_REGISTRIES", WeakValueDictionary())
    monkeypatch.setattr(capsule_registry, "_DEFAULT_REGISTRY", None)
    monkeypatch.setattr(runtime, "_ROOT_CONTEXTS", test_roots)
    yield
    # A root Context that outlives the test, such as a default Context first built here, stays known to the process.
    with runtime._ROOT_CONTEXTS_LOCK:
        for context in list(test_roots):
            if context is runtime._DEFAULT_CONTEXT:
                process_roots.add(context)


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


def _write_cache_row(root):
    with closing(_context(root)) as first:
        cache = LLMCache.current(config_manager=first.config)
        cache.set_text(_KEY, [], _ANSWER, [], policy="deterministic")
        assert cache.get_text(_KEY)["response"] == _ANSWER


def _register_capsule(root):
    with closing(_context(root)) as first:
        registry = capsule_registry.capsule_registry(resolver=first.modules(), config=first.config)
        registry.register(hb.Capsule(add, name="sum_two", namespace="demo", version="1", include_cloudpickle=False))
        assert [row["name"] for row in registry.list()] == ["sum_two"]


def _capsules(context):
    return capsule_registry.capsule_registry(resolver=context.modules(), config=context.config).list()


@pytest.mark.slow
def test_cache_drop_in_a_new_context_removes_the_rows_of_an_earlier_one(tmp_path):
    root = tmp_path / "root"
    _write_cache_row(root)
    with closing(_context(root)) as later:
        LLMCache.reset(drop=True, config_manager=later.config)
        assert LLMCache.current(config_manager=later.config).get_text(_KEY) is None


@pytest.mark.slow
def test_process_wide_cache_drop_reaches_a_cache_service_that_has_not_opened_its_storage(tmp_path):
    root = tmp_path / "root"
    _write_cache_row(root)
    with closing(_context(root)) as later:
        cache = LLMCache.current(config_manager=later.config)
        LLMCache.reset(drop=True)
        assert cache.get_text(_KEY) is None


@pytest.mark.slow
def test_cache_reset_without_drop_keeps_the_rows_of_an_earlier_context(tmp_path):
    root = tmp_path / "root"
    _write_cache_row(root)
    with closing(_context(root)) as later:
        LLMCache.reset(config_manager=later.config)
        assert LLMCache.current(config_manager=later.config).get_text(_KEY)["response"] == _ANSWER


@pytest.mark.slow
def test_capsule_registry_drop_in_a_new_context_removes_the_capsules_of_an_earlier_one(tmp_path):
    root = tmp_path / "root"
    _register_capsule(root)
    with closing(_context(root)) as later:
        capsule_registry.reset_capsule_registry(drop=True, resolver=later.modules(), config=later.config)
        assert _capsules(later) == []


@pytest.mark.slow
def test_process_wide_capsule_registry_drop_reaches_a_registry_that_has_not_read_its_storage(tmp_path):
    root = tmp_path / "root"
    _register_capsule(root)
    with closing(_context(root)) as later:
        capsule_registry.capsule_registry(resolver=later.modules(), config=later.config)
        capsule_registry.reset_capsule_registry(drop=True)
        assert _capsules(later) == []


@pytest.mark.slow
def test_capsule_registry_drop_after_a_close_keeps_the_closed_rows(tmp_path):
    root = tmp_path / "root"
    _register_capsule(root)
    closed = _context(root)
    capsule_registry.capsule_registry(resolver=closed.modules(), config=closed.config)
    closed.close()
    capsule_registry.reset_capsule_registry(drop=True)
    with closing(_context(root)) as later:
        assert [row["name"] for row in _capsules(later)] == ["sum_two"]


@pytest.mark.slow
def test_cache_drop_for_a_context_closed_before_any_use_returns_and_keeps_its_rows(tmp_path):
    root = tmp_path / "root"
    _write_cache_row(root)
    closed = _context(root)
    closed.close()
    LLMCache.reset(drop=True, config_manager=closed.config)
    with closing(_context(root)) as later:
        assert LLMCache.current(config_manager=later.config).get_text(_KEY)["response"] == _ANSWER


def _child(env, mode):
    done = subprocess.run([sys.executable, str(_CHILD), mode], env=env, capture_output=True, text=True, timeout=300)
    lines = done.stdout.strip().splitlines()
    return lines[-1] if lines else "no output: " + done.stderr[-400:]


@pytest.fixture
def env(tmp_path, home_env):
    home = tmp_path / "home"
    home.mkdir()
    return {**home_env(home), "PYTHON_DOTENV_DISABLED": "1"}


@pytest.mark.slow
def test_cache_drop_with_cm_hvnb_in_a_new_process_removes_the_default_rows(env):
    assert _child(env, "write") == "written"
    assert _child(env, "drop-cm") == "dropped None"


@pytest.mark.fast
def test_cache_drop_with_cm_hvnb_in_explicit_mode_returns(env):
    assert _child(env, "explicit-cm") == "returned"


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["closed-default-pw", "closed-default-cm"])
def test_cache_drop_after_the_default_context_closed_returns(env, mode):
    assert _child(env, "write") == "written"
    assert _child(env, mode) == "returned"


@pytest.mark.slow
def test_process_wide_cache_drop_with_a_closed_default_still_drops_an_open_context(env):
    assert _child(env, "write") == "written"
    assert _child(env, "closed-default-other") == "returned other-rows dropped"


@pytest.mark.full
def test_process_wide_cache_drop_reaches_an_open_context_that_never_used_the_cache(tmp_path):
    root = tmp_path / "root"
    _write_cache_row(root)
    with closing(_context(root)) as later:
        LLMCache.reset(drop=True)
        assert LLMCache.current(config_manager=later.config).get_text(_KEY) is None


@pytest.mark.full
def test_process_wide_capsule_registry_drop_reaches_an_open_context_that_never_used_the_registry(tmp_path):
    root = tmp_path / "root"
    _register_capsule(root)
    with closing(_context(root)) as later:
        capsule_registry.reset_capsule_registry(drop=True)
        assert _capsules(later) == []


@pytest.mark.full
def test_process_wide_cache_drop_in_a_new_process_does_not_build_the_default_context(env):
    assert _child(env, "write") == "written"
    assert _child(env, "drop-pw") == "default-built=False kept"


@pytest.mark.fast
def test_the_process_holds_its_root_contexts_weakly_and_lists_only_open_ones(tmp_path):
    open_context = _context(tmp_path / "open")
    closed_context = _context(tmp_path / "closed")
    closed_context.close()
    try:
        listed = runtime._root_contexts()
        assert any(context is open_context for context in listed)
        assert not any(context is closed_context for context in listed)
        reference = weakref.ref(closed_context)
        del closed_context, listed
        gc.collect()
        assert reference() is None
    finally:
        open_context.close()


@pytest.mark.fast
@pytest.mark.skipif(not hasattr(os, "fork"), reason="the platform has no fork")
def test_a_forked_child_can_register_root_contexts_while_a_parent_thread_holds_the_registry_lock():
    held, release = threading.Event(), threading.Event()

    def hold():
        with runtime._ROOT_CONTEXTS_LOCK:
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(30)
    try:
        pid = os.fork()
        if pid == 0:
            acquired = runtime._ROOT_CONTEXTS_LOCK.acquire(timeout=5)
            os._exit(0 if acquired else 1)
        _, status = os.waitpid(pid, 0)
    finally:
        release.set()
        holder.join(30)
    assert os.waitstatus_to_exitcode(status) == 0


@pytest.mark.fast
def test_a_non_dropping_process_wide_cache_reset_does_not_wait_for_a_context_without_a_cache_store(tmp_path):
    with closing(_context(tmp_path / "busy")) as busy:
        held, release, done = threading.Event(), threading.Event(), threading.Event()

        def hold():
            with busy._lock:
                held.set()
                release.wait(30)

        def reset():
            LLMCache.reset()
            done.set()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(30)
        resetter = threading.Thread(target=reset, daemon=True)
        resetter.start()
        try:
            assert done.wait(10)
        finally:
            release.set()
            holder.join(30)
            resetter.join(30)


def _files(root):
    return sorted(os.path.relpath(os.path.join(base, name), root) for base, _, names in os.walk(root) for name in names)


@pytest.mark.fast
def test_process_wide_drops_leave_an_open_context_without_storage_uninitialized(tmp_path):
    root = tmp_path / "unrelated"
    with closing(_context(root)) as unrelated:
        before = _files(root)
        LLMCache.reset(drop=True)
        capsule_registry.reset_capsule_registry(drop=True)
        assert _files(root) == before
        assert unrelated._modules is None
