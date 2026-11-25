"""Regression tests for queries run through the LanceDB native adapter."""

from importlib.util import find_spec
from types import SimpleNamespace
from uuid import uuid4

import pytest

import heavenbase as hb
from heavenbase.backends.vector.lance import _records

pytestmark = [
    pytest.mark.full,
    pytest.mark.skipif(find_spec("lancedb") is None, reason="lancedb runtime is not installed"),
]


@pytest.fixture
def lance_workspace(tmp_path):
    """Provide an isolated workspace whose rows live in local LanceDB."""
    backend = hb.backends.LanceBackend("vec", hb.utils.path_to_file_uri(tmp_path / "lance"))
    workspace = hb.HeavenBase(f"lance-native-{uuid4().hex[:8]}", backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    try:
        yield workspace
    finally:
        workspace.release()


def test_lance_native_scalar_filter_returns_every_match(lance_workspace):
    """A native scalar predicate returns all 10,001 matches when unbounded."""

    class LanceDoc(hb.Entity):
        identifier = "lance-native-scalar-doc"

        state = hb.field(hb.ShortText).store(to="vec")

    lance_workspace.register(LanceDoc)
    matching = {f"d{i:05d}" for i in range(10_001)}
    lance_workspace.upsert_many(
        LanceDoc,
        [{"object_id": object_id, "state": "ready"} for object_id in sorted(matching)] + [{"object_id": "pending", "state": "pending"}],
    )

    query = lance_workspace.query(LanceDoc).where(LanceDoc.state == "ready")
    ids = query.execute().ids
    assert len(ids) == len(matching)
    assert set(ids) == matching
    limited = query.limit(3).execute().ids
    assert len(limited) == 3
    assert set(limited) <= matching


def test_lance_native_near_searches_the_requested_vector_column(lance_workspace):
    """Each of two vector columns returns its own nearest neighbour."""

    class LanceDoc(hb.Entity):
        identifier = "lance-native-vector-doc"

        first = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)
        second = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    lance_workspace.register(LanceDoc)
    lance_workspace.upsert_many(
        LanceDoc,
        [
            {"object_id": "d1", "first": [1.0, 0.0], "second": [0.0, 1.0]},
            {"object_id": "d2", "first": [0.0, 1.0], "second": [1.0, 0.0]},
        ],
    )

    for field, expected in ((LanceDoc.first, "d1"), (LanceDoc.second, "d2")):
        assert lance_workspace.query(LanceDoc).near(field, [1, 0], top_k=1, exact=True).execute().ids == [expected]
        assert lance_workspace.query(LanceDoc).near(field, [1, 0], top_k=1).execute().ids == [expected]


def test_lance_arrow_records_decode_text_across_chunks_with_large_offsets():
    """The native result decoder reads a string column whose chunks exceed 2 GiB in total."""
    import pyarrow as pa

    # Put the padding behind a null so Arrow offsets span 2 GiB without
    # materializing 2 GiB of strings in Python or in unique Arrow buffers.
    chunk_size = 32 * 1024**2
    offsets = pa.array([0, 5, chunk_size], type=pa.int32())
    buffer = pa.py_buffer(b"ready".ljust(chunk_size, b" "))
    chunk = pa.Array.from_buffers(pa.string(), 2, [pa.py_buffer(b"\x01"), offsets.buffers()[1], buffer], null_count=1)
    body = pa.chunked_array([chunk] * 65)
    ids = [str(index) for index in range(len(body))]
    table = pa.table({"object_id": ids, "body": body})

    rows = _records(SimpleNamespace(to_arrow=lambda: table))

    assert rows == [{"object_id": object_id, "body": "ready" if index % 2 == 0 else None} for index, object_id in enumerate(ids)]


def test_lance_arrow_records_keep_vector_representation_consistent_across_chunks():
    """A null item selects list decoding for the entire affected vector column."""
    import numpy as np
    import pyarrow as pa

    vector_type = pa.list_(pa.float32(), 2)
    first = pa.array([[1.0, 2.0]], type=vector_type)
    mixed = pa.chunked_array([first, pa.array([[3.0, None]], type=vector_type)])
    complete = pa.chunked_array([first, pa.array([None], type=vector_type)])
    table = pa.table({"mixed": mixed, "complete": complete})

    rows = _records(SimpleNamespace(to_arrow=lambda: table))

    assert [row["mixed"] for row in rows] == [[1.0, 2.0], [3.0, None]]
    assert isinstance(rows[0]["complete"], np.ndarray)
    np.testing.assert_array_equal(rows[0]["complete"], [1.0, 2.0])
    assert rows[1]["complete"] is None
