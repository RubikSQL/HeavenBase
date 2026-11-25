"""Regression coverage for what ``reset_capsule_registry(drop=True)`` drops."""

from contextlib import closing
import gc
import threading
from weakref import WeakSet, WeakValueDictionary

import pytest

import heavenbase as hb
from heavenbase.capsule import registry as capsule_registry
from heavenbase.context import runtime
from heavenbase.context.reset import reset_default_local_state
from heavenbase.llm.cache import LLMCache
from heavenbase.workspace.hidden_store import HiddenWorkspaceStore
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast

_KEY = {"object_id": "text-row", "spec_key": "spec", "request_key": "request", "args": {}}
_ANSWER = {"text": "summary of the quarterly report"}


def add(left, right):
    return left + right


@pytest.fixture
def isolated_registry(monkeypatch):
    """Keep process-wide resets to the Contexts each test builds."""
    monkeypatch.setattr(capsule_registry, "_AUTHORITY_REGISTRIES", WeakValueDictionary())
    monkeypatch.setattr(capsule_registry, "_DEFAULT_REGISTRY", None)
    monkeypatch.setattr(runtime, "_ROOT_CONTEXTS", WeakSet())


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


def _cached_answer(context, path):
    cache = LLMCache.current(config_manager=context.config)
    cache.config["backend"] = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(path)}}
    cache.set_text(_KEY, [], _ANSWER, [], policy="deterministic")
    assert cache.get_text(_KEY)["response"] == _ANSWER
    return cache


@pytest.mark.slow
def test_scoped_capsule_drop_keeps_the_llm_cache_rows(tmp_path, isolated_registry):
    with closing(_context(tmp_path / "ctx")) as context:
        cache = _cached_answer(context, tmp_path / "cache.db")
        registry = capsule_registry.capsule_registry(resolver=context.modules(), config=context.config)
        capsule_id = registry.register(hb.Capsule(add, name="sum_two", namespace="demo", version="1", include_cloudpickle=False))
        owner = registry.store.owner

        capsule_registry.reset_capsule_registry(drop=True, resolver=context.modules(), config=context.config)

        assert owner.retired
        again = capsule_registry.capsule_registry(resolver=context.modules(), config=context.config)
        assert again.store.get(capsule_id) is None
        hit = cache.get_text(_KEY)
        assert hit is not None and hit["response"] == _ANSWER


@pytest.mark.slow
def test_process_wide_capsule_drop_keeps_the_llm_cache_rows(tmp_path, isolated_registry):
    with closing(_context(tmp_path / "ctx")) as context:
        cache = _cached_answer(context, tmp_path / "cache.db")
        registry = capsule_registry.capsule_registry(resolver=context.modules(), config=context.config)
        registry.store.rows()

        capsule_registry.reset_capsule_registry(drop=True)

        assert registry.store.owner.retired
        hit = cache.get_text(_KEY)
        assert hit is not None and hit["response"] == _ANSWER


@pytest.mark.slow
def test_process_wide_capsule_drop_reaches_a_context_whose_facade_was_collected(tmp_path, isolated_registry):
    with closing(_context(tmp_path / "ctx")) as context:
        registry = capsule_registry.capsule_registry(resolver=context.modules(), config=context.config)
        capsule_id = registry.register(hb.Capsule(add, name="sum_two", namespace="demo", version="1", include_cloudpickle=False))
        owner = registry.store.owner
        del registry
        gc.collect()

        capsule_registry.reset_capsule_registry(drop=True)

        assert owner.retired
        again = capsule_registry.capsule_registry(resolver=context.modules(), config=context.config)
        assert again.store.owner is not owner
        assert again.store.get(capsule_id) is None


@pytest.mark.slow
def test_capsule_drop_reopens_an_llm_cache_on_the_same_database_file(tmp_path, isolated_registry):
    shared = tmp_path / "shared.db"
    backend = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(shared)}}
    later = dict(_KEY, request_key="request-2")
    with closing(_context(tmp_path / "ctx")) as context:
        cache = _cached_answer(context, shared)
        registry = capsule_registry.capsule_registry({"backend": backend}, resolver=context.modules(), config=context.config)
        registry.register(hb.Capsule(add, name="sum_two", namespace="demo", version="1", include_cloudpickle=False))

        capsule_registry.reset_capsule_registry(drop=True, resolver=context.modules(), config=context.config)

        # The drop deleted the shared file, so the cache reopens on fresh storage.
        assert cache.get_text(_KEY) is None
        cache.set_text(later, [], _ANSWER, [], policy="deterministic")
        assert cache.get_text(later)["response"] == _ANSWER
    with closing(_context(tmp_path / "reader")) as reader:
        cache = LLMCache.current(config_manager=reader.config)
        cache.config["backend"] = backend
        assert cache.get_text(later)["response"] == _ANSWER


def _routed_cache(context, **paths):
    cache = LLMCache.current(config_manager=context.config)
    cache.config["backend"] = {route: {"type": "sqlite", "database": hb.utils.path_to_file_uri(path)} for route, path in paths.items()}
    return cache


def _capsule_drop_on(context, path):
    backend = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(path)}}
    registry = capsule_registry.capsule_registry({"backend": backend}, resolver=context.modules(), config=context.config)
    registry.register(hb.Capsule(add, name="sum_two", namespace="demo", version="1", include_cloudpickle=False))
    capsule_registry.reset_capsule_registry(drop=True, resolver=context.modules(), config=context.config)


@pytest.mark.slow
@pytest.mark.parametrize("later_reset", ["cache_drop", "context_reset"])
def test_a_later_drop_reaches_every_cache_route_after_a_capsule_drop_on_one_of_them(tmp_path, isolated_registry, later_reset):
    shared, independent = tmp_path / "shared.db", tmp_path / "independent.db"
    with closing(_context(tmp_path / "ctx")) as context:
        cache = _routed_cache(context, main=independent, archive=shared)
        cache.set_text(_KEY, [], _ANSWER, [], policy="deterministic")
        _capsule_drop_on(context, shared)
        # The Capsule drop removes only its own storage; the cache's other route stays.
        assert independent.exists() and not shared.exists()

        if later_reset == "cache_drop":
            LLMCache.reset(drop=True, config_manager=context.config)
        else:
            reset_default_local_state(context)

        assert _routed_cache(context, main=independent, archive=shared).get_text(_KEY) is None


@pytest.mark.slow
def test_a_cached_call_during_a_capsule_drop_on_the_cache_file_waits_for_the_drop(tmp_path, isolated_registry, monkeypatch):
    shared = tmp_path / "shared.db"
    later = dict(_KEY, request_key="request-2")
    with closing(_context(tmp_path / "ctx")) as context:
        cache = _cached_answer(context, shared)
        calls: list[threading.Thread] = []
        results: list[object] = []
        drop = HiddenWorkspaceStore.drop

        def drop_with_a_concurrent_call(store, **kwargs):
            if not calls:
                worker = threading.Thread(target=lambda: results.append(cache.get_text(_KEY)))
                calls.append(worker)
                worker.start()
                # Give the call time to reach the cache before the Capsule store drops.
                worker.join(timeout=1.0)
            drop(store, **kwargs)

        monkeypatch.setattr(HiddenWorkspaceStore, "drop", drop_with_a_concurrent_call)
        _capsule_drop_on(context, shared)
        monkeypatch.undo()
        calls[0].join(timeout=30)

        assert not calls[0].is_alive()
        # The call ran after the drop, on fresh storage.
        assert results == [None]
        cache.set_text(later, [], _ANSWER, [], policy="deterministic")
        assert cache.get_text(later)["response"] == _ANSWER


@pytest.mark.slow
def test_a_capsule_drop_detaches_a_cache_of_another_context_on_the_same_file(tmp_path, isolated_registry):
    shared = tmp_path / "shared.db"
    later = dict(_KEY, request_key="request-2")
    with closing(_context(tmp_path / "ctx")) as context, closing(_context(tmp_path / "other")) as other:
        cache = _cached_answer(other, shared)
        _capsule_drop_on(context, shared)

        # The drop deleted the file under the other Context's cache, which reopens on fresh storage.
        assert cache.get_text(_KEY) is None
        cache.set_text(later, [], _ANSWER, [], policy="deterministic")
    with closing(_context(tmp_path / "reader")) as reader:
        assert _routed_cache(reader, main=shared).get_text(later)["response"] == _ANSWER


def _reader_hit(root, backend, key):
    with closing(_context(root)) as reader:
        cache = LLMCache.current(config_manager=reader.config)
        cache.config["backend"] = backend
        hit = cache.get_text(key)
        return None if hit is None else hit["response"]


@pytest.mark.slow
def test_a_capsule_drop_reaches_the_store_of_a_collected_cache_of_another_context(tmp_path, isolated_registry):
    shared = tmp_path / "shared.db"
    backend = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(shared)}}
    later = dict(_KEY, request_key="request-2")
    with closing(_context(tmp_path / "ctx")) as context, closing(_context(tmp_path / "other")) as other:
        transient = LLMCache(config_manager=other.config)
        transient.config["backend"] = backend
        transient.set_text(_KEY, [], _ANSWER, [], policy="deterministic")
        del transient
        gc.collect()
        _capsule_drop_on(context, shared)

        # The collected cache's store and pooled engine stay in the other Context; a new cache there must not use them.
        cache = _routed_cache(other, main=shared)
        assert cache.get_text(_KEY) is None
        cache.set_text(later, [], _ANSWER, [], policy="deterministic")
    assert _reader_hit(tmp_path / "reader", backend, later) == _ANSWER


@pytest.mark.slow
def test_a_drop_from_another_context_reaches_a_cache_detached_by_an_earlier_drop(tmp_path, isolated_registry):
    shared, independent = tmp_path / "shared.db", tmp_path / "independent.db"
    backend = {route: {"type": "sqlite", "database": hb.utils.path_to_file_uri(path)} for route, path in (("main", independent), ("archive", shared))}
    later = dict(_KEY, request_key="request-2")
    with closing(_context(tmp_path / "ctx")) as context, closing(_context(tmp_path / "other")) as other:
        cache = _routed_cache(context, main=independent, archive=shared)
        cache.set_text(_KEY, [], _ANSWER, [], policy="deterministic")
        _capsule_drop_on(context, shared)
        # The cache is detached and not called again before another Context drops its other file.
        _capsule_drop_on(other, independent)

        again = _routed_cache(context, main=independent, archive=shared)
        assert again.get_text(_KEY) is None
        again.set_text(later, [], _ANSWER, [], policy="deterministic")
    assert _reader_hit(tmp_path / "reader", backend, later) == _ANSWER
