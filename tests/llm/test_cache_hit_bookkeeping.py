import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from threading import Event

import pytest

import heavenbase as hb
from heavenbase.llm.cache import LLMCache, LLMImageEntry
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast
_KEY = {"object_id": "image-row", "spec_key": "spec", "image_inputs": [], "args": {}}
_IMAGES = [{"url": "https://example.invalid/picture.png", "format": "png", "media_type": "image/png"}]


@pytest.fixture
def image_cache(tmp_path):
    @contextmanager
    def open_cache(name, database=None):
        owner = hb.Context.load(
            {
                "version": 1,
                "root": str(tmp_path / name),
                "workspace": "default",
                "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
                "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
            },
            config=ConfigManager(root=str(tmp_path / name), setup=False),
        )
        with closing(owner):
            database = database or tmp_path / f"{name}.db"
            backend = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(database)}}
            cache = LLMCache({"backend": backend}, config_manager=owner.config)
            cache.set_image(_KEY, "picture", _IMAGES, {}, {})
            yield cache

    with ExitStack() as stack:
        yield lambda name, database=None: stack.enter_context(open_cache(name, database))


def _assert_hit(cache):
    row = cache.get_image(_KEY)
    assert row is not None and row["images"] == _IMAGES


@pytest.mark.slow
def test_hit_does_not_reinsert_an_evicted_image_row(image_cache, monkeypatch):
    cache = image_cache("eviction")
    touch = cache.touch

    def evict_before_touch(entity, row):
        assert cache.workspace.delete((LLMImageEntry, row["object_id"]))
        touch(entity, row)

    monkeypatch.setattr(cache, "touch", evict_before_touch)
    _assert_hit(cache)
    monkeypatch.setattr(cache, "touch", touch)
    assert cache.get_image(_KEY) is None


@pytest.mark.slow
@pytest.mark.parametrize("replace", [False, True])
def test_old_image_hit_respects_a_peer_delete_or_replacement(tmp_path, image_cache, monkeypatch, replace):
    database = tmp_path / "shared.db"
    reader = image_cache("reader", database)
    writer = image_cache("writer", database)
    db = reader.workspace.backends.get("main").db
    execute = db.orm_execute
    entered, resume = Event(), Event()
    replacement = [{**_IMAGES[0], "url": "https://example.invalid/new-picture.png"}]

    def pause_update(stmt, *args, **kwargs):
        if stmt.is_update:
            entered.set()
            assert resume.wait(15)
        return execute(stmt, *args, **kwargs)

    monkeypatch.setattr(db, "orm_execute", pause_update)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_hit = pool.submit(_assert_hit, reader)
        try:
            assert entered.wait(10)
            assert writer.workspace.delete((LLMImageEntry, _KEY["object_id"]))
            if replace:
                writer.set_image(_KEY, "picture", replacement, {}, {})
        finally:
            resume.set()
        old_hit.result(timeout=10)

    row = writer.workspace.get(_KEY["object_id"], entity=LLMImageEntry)
    if replace:
        assert row is not None and row["images"] == replacement and row["n_hits"] == 0
    else:
        assert row is None


@pytest.mark.slow
@pytest.mark.parametrize("worker", ["thread", "fork"])
def test_hit_completes_during_an_independent_cache_write(image_cache, monkeypatch, worker):
    if worker == "fork" and "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("multiprocessing fork is unavailable")
    slow = image_cache("slow")
    other = image_cache("other")
    _assert_hit(other)
    entered, resume = Event(), Event()
    write = slow._touch_rows

    def paused_write(*args, **kwargs):
        entered.set()
        assert resume.wait(15)
        return write(*args, **kwargs)

    monkeypatch.setattr(slow, "_touch_rows", paused_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow_hit = pool.submit(_assert_hit, slow)
        try:
            assert entered.wait(10)
            if worker == "thread":
                pool.submit(_assert_hit, other).result(timeout=5)
            else:
                child = multiprocessing.get_context("fork").Process(target=_assert_hit, args=(other,))
                child.start()
                try:
                    child.join(5)
                    assert child.exitcode == 0
                finally:
                    if child.is_alive():
                        child.terminate()
                    child.join()
                    child.close()
        finally:
            resume.set()
        slow_hit.result(timeout=10)
