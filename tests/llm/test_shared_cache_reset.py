from contextlib import closing

import pytest

import heavenbase as hb
from heavenbase.llm.cache import LLMCache
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast


def _context(root):
    manager = ConfigManager(root=str(root), setup=False)
    bootstrap = {
        "version": 1,
        "root": str(root),
        "workspace": "default",
        "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
        "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
    }
    return hb.Context.load(bootstrap, config=manager)


@pytest.mark.slow
def test_dropping_a_shared_sqlite_cache_reopens_a_peer_in_another_context(tmp_path):
    path = tmp_path / "shared-cache.db"
    backend = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(path)}}
    key = {"object_id": "seed-row", "spec_key": "spec", "request_key": "request", "args": {}}

    with closing(_context(tmp_path / "first")) as first, closing(_context(tmp_path / "second")) as second:
        registered = LLMCache.current(config_manager=first.config)
        registered.config["backend"] = backend
        peer = LLMCache.from_dict({"backend": backend}, config_manager=second.config)
        registered.set_text(key, [], {"text": "old"}, [], policy="deterministic")
        assert peer.get_text(key)["response"] == {"text": "old"}

        LLMCache.reset(drop=True, config_manager=first.config)
        assert not path.exists()
        registered.set_text(key, [], {"text": "fresh"}, [], policy="deterministic")

        assert peer.get_text(key)["response"] == {"text": "fresh"}
