"""A native filter that reads its matches again by id returns only rows that still match.

Milvus Lite and Chroma list the ids a native filter matches, then read those rows by id. Two
backend instances on the same storage have separate backend locks, so one may write between
the other's two reads; the by-id read must not return the changed row with its new values.
"""

from __future__ import annotations

from importlib.util import find_spec

import pytest

import heavenbase as hb

pytestmark = [pytest.mark.fast, pytest.mark.unit]

IDS = [f"k{index:03}" for index in range(6)]
CHANGED = "k003"


class RereadDoc(hb.Entity):
    identifier = "reread-doc"

    title = hb.field(hb.ShortText).required().store(to="vec")
    embedding = hb.field(hb.Vector[2]).required().store(to="vec", strategy=hb.VectorIndex)


def _backend(kind: str, tmp_path):
    if kind == "lite":
        return hb.backends.MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "shared.db"))
    return hb.backends.ChromaBackend("vec", hb.utils.path_to_file_uri(tmp_path / "chroma"))


@pytest.fixture
def shared(request, tmp_path):
    """Yield a reader backend, its workspace and a writer workspace over the same storage."""
    kind = request.param
    if kind == "lite" and (find_spec("pymilvus") is None or find_spec("milvus_lite") is None):
        pytest.skip("pymilvus with milvus-lite is not installed")
    if kind == "chroma" and find_spec("chromadb") is None:
        pytest.skip("chromadb is not installed")
    reader, writer = _backend(kind, tmp_path), _backend(kind, tmp_path)
    read_ws = hb.HeavenBase("reread-ws", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": reader})
    write_ws = hb.HeavenBase("reread-ws", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": writer})
    try:
        read_ws.register(RereadDoc)
        write_ws.register(RereadDoc)
        read_ws.upsert_many(RereadDoc, [{"object_id": object_id, "title": "a", "embedding": [1.0, index / 10]} for index, object_id in enumerate(IDS)])
        yield reader, read_ws, write_ws
    finally:
        write_ws.release()
        read_ws.release()


def _write_before_reread(monkeypatch, reader, method: str, write_ws) -> None:
    """Make the writer change one matched row just before the reader's first read by id."""
    original = getattr(reader, method)
    calls = []

    def reread(*args, **kwargs):
        if not calls:
            write_ws.upsert(RereadDoc, {"object_id": CHANGED, "title": "b", "embedding": [1.0, 0.3]})
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(reader, method, reread)


def _rows(frame) -> list[dict]:
    return frame.rows() if callable(frame.rows) else frame.rows


def _assert_still_matching(rows: list[dict]) -> None:
    assert [row["title"] for row in rows] == ["a"] * len(rows)
    assert sorted(row["object_id"] for row in rows) == [object_id for object_id in IDS if object_id != CHANGED]


@pytest.mark.parametrize("shared", ["lite"], indirect=True)
def test_milvus_lite_native_filter_drops_a_row_changed_after_its_listing(shared, monkeypatch):
    """The Lite key-ordered read applies the filter to each by-key fetch, not only to the key listing."""
    reader, read_ws, write_ws = shared
    query = read_ws.query(RereadDoc).where(RereadDoc.title == "a")
    assert "milvus-lite" in str(query.explain())
    _write_before_reread(monkeypatch, reader, "_rows_by_key", write_ws)

    _assert_still_matching(_rows(query.execute()))


@pytest.mark.parametrize("shared", ["chroma"], indirect=True)
@pytest.mark.parametrize("oversized", [False, True], ids=["one-statement", "split"])
def test_chroma_native_filter_drops_a_row_changed_after_its_listing(shared, monkeypatch, oversized):
    """Chroma's by-id read of the matches applies the ``where``, or the row predicate when the ``where`` is split."""
    from heavenbase.backends.vector import chroma

    reader, read_ws, write_ws = shared
    query = read_ws.query(RereadDoc).where(RereadDoc.title == "a")
    assert "chroma" in str(query.explain())
    if oversized:
        monkeypatch.setattr(chroma, "CHROMA_MAX_WHERE_VARIABLES", 1)
    _write_before_reread(monkeypatch, reader, "_rows_by_ids", write_ws)

    _assert_still_matching(_rows(query.execute()))


@pytest.mark.parametrize("shared", ["chroma"], indirect=True)
@pytest.mark.parametrize("path", ["large-top-k", "split"])
def test_chroma_filtered_near_drops_a_hit_changed_before_its_rows_are_read(shared, monkeypatch, path):
    """A filtered near that reads its hits by id (``top_k`` above the id cap, or a split ``where``) keeps only matching rows."""
    from heavenbase.backends.vector import chroma

    reader, read_ws, write_ws = shared
    if path == "large-top-k":
        monkeypatch.setattr(chroma, "CHROMA_MAX_GET_IDS", 2)
    else:
        monkeypatch.setattr(chroma, "CHROMA_MAX_WHERE_VARIABLES", 1)
    _write_before_reread(monkeypatch, reader, "_rows_by_ids", write_ws)

    rows = _rows(read_ws.query(RereadDoc).where(RereadDoc.title == "a").near(RereadDoc.embedding, [1.0, 0.0], top_k=len(IDS)).execute())

    _assert_still_matching(rows)
