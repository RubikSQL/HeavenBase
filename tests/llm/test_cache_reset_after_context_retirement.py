from contextlib import closing

import pytest

import heavenbase as hb
from heavenbase.context.reset import reset_default_local_state
from heavenbase.llm.cache import LLMCache
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast
_KEY = {"object_id": "text-row", "spec_key": "spec", "request_key": "request", "args": {}}


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


def _cache(context, path):
    cache = LLMCache.current(config_manager=context.config)
    cache.config["backend"] = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(path)}}
    return cache


def _routed_cache(context, **paths):
    cache = LLMCache.current(config_manager=context.config)
    cache.config["backend"] = {route: {"type": "sqlite", "database": hb.utils.path_to_file_uri(path)} for route, path in paths.items()}
    return cache


def _write(cache, value):
    cache.set_text(_KEY, [], {"text": value}, [], policy="deterministic")


@pytest.mark.slow
def test_cache_drop_after_a_context_reset_leaves_the_reset_outcome(tmp_path):
    with closing(_context(tmp_path / "ctx")) as context:
        cache = _cache(context, tmp_path / "cache.db")
        _write(cache, "old")
        assert cache.get_text(_KEY)["response"] == {"text": "old"}
        reset_default_local_state(context)
        # The Context reset already dropped the hidden cache storage.
        LLMCache.reset(drop=True, config_manager=context.config)
        fresh = _cache(context, tmp_path / "cache.db")
        assert fresh.get_text(_KEY) is None
        _write(fresh, "fresh")
        assert fresh.get_text(_KEY)["response"] == {"text": "fresh"}


@pytest.mark.slow
def test_cache_reset_after_a_context_reset_reopens_the_same_cache(tmp_path):
    with closing(_context(tmp_path / "ctx")) as context:
        cache = _cache(context, tmp_path / "cache.db")
        _write(cache, "old")
        reset_default_local_state(context)
        LLMCache.reset(config_manager=context.config)
        assert cache.get_text(_KEY) is None
        _write(cache, "fresh")
        assert cache.get_text(_KEY)["response"] == {"text": "fresh"}


@pytest.mark.slow
def test_cache_drop_after_a_context_close_keeps_the_closed_rows(tmp_path):
    path = tmp_path / "cache.db"
    context = _context(tmp_path / "ctx")
    cache = _cache(context, path)
    _write(cache, "old")
    context.close()
    # A closed Context owns no storage to drop; its rows stay on disk.
    LLMCache.reset(drop=True, config_manager=context.config)
    assert path.exists()
    with closing(_context(tmp_path / "other")) as other:
        assert _cache(other, path).get_text(_KEY)["response"] == {"text": "old"}


def _peer_after_a_shared_drop(tmp_path, first, second):
    shared, independent = tmp_path / "shared.db", tmp_path / "independent.db"
    primary = _routed_cache(first, main=shared)
    peer = _routed_cache(second, main=independent, archive=shared)
    _write(primary, "primary")
    _write(peer, "peer-old")
    assert peer.get_text(_KEY)["response"] == {"text": "peer-old"}
    # The primary's drop closes the peer, which shares the archive file.
    LLMCache.reset(drop=True, config_manager=first.config)
    assert independent.exists()
    return shared, independent, peer


@pytest.mark.slow
def test_context_reset_drops_the_routes_of_a_peer_closed_by_a_shared_drop(tmp_path):
    with closing(_context(tmp_path / "first")) as first, closing(_context(tmp_path / "second")) as second:
        shared, independent, peer = _peer_after_a_shared_drop(tmp_path, first, second)
        reset_default_local_state(second)
        assert _routed_cache(second, main=independent, archive=shared).get_text(_KEY) is None


@pytest.mark.slow
def test_cache_drop_after_a_context_reset_of_a_peer_closed_by_a_shared_drop(tmp_path):
    with closing(_context(tmp_path / "first")) as first, closing(_context(tmp_path / "second")) as second:
        shared, independent, peer = _peer_after_a_shared_drop(tmp_path, first, second)
        reset_default_local_state(second)
        LLMCache.reset(drop=True, config_manager=second.config)
        assert _routed_cache(second, main=independent, archive=shared).get_text(_KEY) is None


@pytest.mark.slow
def test_peer_closed_by_a_shared_drop_reopens_and_its_own_drop_reaches_every_route(tmp_path):
    with closing(_context(tmp_path / "first")) as first, closing(_context(tmp_path / "second")) as second:
        shared, independent, peer = _peer_after_a_shared_drop(tmp_path, first, second)
        assert peer.get_text(_KEY)["response"] == {"text": "peer-old"}
        LLMCache.reset(drop=True, config_manager=second.config)
        assert not independent.exists()
