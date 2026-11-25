"""How long an LLM cache's hidden store lives, and what a cache drop reaches after its cache object is gone."""

from contextlib import closing
import gc
import weakref

import pytest

import heavenbase as hb
from heavenbase.context.reset import reset_default_local_state
from heavenbase.llm.cache import LLMCache
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast

_KEY = {"object_id": "text-row", "spec_key": "spec", "request_key": "request", "args": {}}
_ANSWER = {"text": "summary of the quarterly report"}


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


def _backend(path):
    return {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(path)}}


def _current(context, path):
    cache = LLMCache.current(config_manager=context.config)
    cache.config["backend"] = _backend(path)
    return cache


def _key(index):
    return dict(_KEY, object_id=f"text-row-{index}", request_key=f"request-{index}")


def _live(references):
    return len({id(store) for reference in references if (store := reference()) is not None})


@pytest.mark.slow
def test_cache_drop_reaches_the_storage_of_a_collected_cache(tmp_path):
    path = tmp_path / "cache.db"
    with closing(_context(tmp_path / "ctx")) as context:
        cache = _current(context, path)
        cache.set_text(_KEY, [], _ANSWER, [], policy="deterministic")
        del cache
        gc.collect()

        LLMCache.reset(drop=True, config_manager=context.config)

        assert not path.exists()
        assert _current(context, path).get_text(_KEY) is None


@pytest.mark.slow
def test_caches_of_one_context_reuse_one_store_after_collection(tmp_path):
    path = tmp_path / "cache.db"
    with closing(_context(tmp_path / "ctx")) as context:
        stores = []
        for index in range(30):
            cache = _current(context, path)
            cache.set_text(_key(index), [], _ANSWER, [], policy="deterministic")
            assert cache.get_text(_key(index))["response"] == _ANSWER
            stores.append(weakref.ref(cache._store))
            del cache
            gc.collect()

        assert _live(stores) == 1
        # Every row stays reachable through the one store.
        cache = _current(context, path)
        assert all(cache.get_text(_key(index)) is not None for index in range(30))


@pytest.mark.slow
def test_a_collected_cache_built_from_a_config_releases_its_store(tmp_path):
    path = tmp_path / "batch.db"
    with closing(_context(tmp_path / "ctx")) as context:
        stores = []
        for index in range(30):
            cache = LLMCache.from_dict({"backend": _backend(path)}, config_manager=context.config)
            cache.set_text(_key(index), [], _ANSWER, [], policy="deterministic")
            stores.append(weakref.ref(cache._store))
            del cache
            gc.collect()

        assert _live(stores) == 0
        reader = LLMCache.from_dict({"backend": _backend(path)}, config_manager=context.config)
        assert reader.get_text(_key(29))["response"] == _ANSWER


@pytest.mark.slow
def test_a_context_reset_drops_the_storage_of_a_live_cache_built_from_a_config(tmp_path):
    path = tmp_path / "batch.db"
    with closing(_context(tmp_path / "ctx")) as context:
        cache = LLMCache.from_dict({"backend": _backend(path)}, config_manager=context.config)
        cache.set_text(_KEY, [], _ANSWER, [], policy="deterministic")

        reset_default_local_state(context)

        assert cache.get_text(_KEY) is None
