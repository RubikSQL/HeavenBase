"""Near ranking is identical whether SQL vectors are stored packed or as legacy JSON text."""

from __future__ import annotations

import pytest

import heavenbase as hb
from heavenbase.utils.serialize import dumps_json

pytestmark = [pytest.mark.fast, pytest.mark.unit]

ENTITY = "near-storage-doc"
ROWS = [
    {"object_id": "b", "scope": "one", "embedding": [0.1, 0.2]},
    {"object_id": "a", "scope": "one", "embedding": [0.1, 0.2]},  # exact tie with b, broken by object_id
    {"object_id": "c", "scope": "one", "embedding": [0.3, 0.6]},  # collinear: cosine tie, distinct l2 and ip
    {"object_id": "z", "scope": "one", "embedding": [0.0, 0.0]},
    {"object_id": "n", "scope": "one", "embedding": [-0.1, -0.2]},
    {"object_id": "other", "scope": "two", "embedding": [1 / 3, 2 / 3]},
    {"object_id": "missing", "scope": "one", "embedding": None},
]
QUERY = [0.1, 0.2]


class NearDoc(hb.Entity):
    identifier = ENTITY

    scope = hb.field(hb.ShortText)
    embedding = hb.field(hb.Vector[2]).store(to="main").optional()


def _workspace(tmp_path, name: str, *, legacy_json: bool):
    # both stores opt in to packed vectors; the legacy one rewrites its rows as the JSON text a packed store still reads
    backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / f"{name}.db"), vector_packed_type="float64_blob")
    ws = hb.HeavenBase(name, detached=True, backends={"main": backend})
    ws.register(NearDoc)
    ws.upsert_many(NearDoc, [dict(row) for row in ROWS])
    table = backend.tables[ENTITY].name
    if legacy_json:
        for row in ROWS:
            if row.get("embedding") is not None:  # SQL NULL was never stored as JSON text
                backend.db.execute(
                    f'UPDATE "{table}" SET embedding = :payload WHERE object_id = :object_id',
                    {"payload": dumps_json(row["embedding"], compact=True), "object_id": row["object_id"]},
                    readonly=False,
                )
    kinds = {r["object_id"]: r["kind"] for r in backend.db.execute(f'SELECT object_id, typeof(embedding) AS kind FROM "{table}"', readonly=True).to_dicts()}
    assert kinds["a"] == ("text" if legacy_json else "blob")
    return ws


@pytest.fixture
def workspaces(tmp_path):
    packed = _workspace(tmp_path, "near-packed", legacy_json=False)
    legacy = _workspace(tmp_path, "near-legacy", legacy_json=True)
    yield packed, legacy
    packed.release()
    legacy.release()


def _ranking(ws, metric: str):
    """Rank through the exact scan route, the only near route an inline SQLite vector column has."""
    query = ws.query(NearDoc).where({"scope": "one"}).near("embedding", QUERY, top_k=10, metric=metric, exact=True)
    return [(row["object_id"], row["score"]) for row in query.execute().rows()]


@pytest.mark.slow
@pytest.mark.parametrize("metric", ["cosine", "l2", "ip"])
def test_packed_and_json_vectors_rank_identically_with_identical_scores(workspaces, metric):
    packed, legacy = workspaces

    packed_rows = _ranking(packed, metric)
    legacy_rows = _ranking(legacy, metric)

    assert packed_rows == legacy_rows
    assert [object_id for object_id, _score in packed_rows] == {
        "cosine": ["a", "b", "c", "z", "n"],
        "l2": ["a", "b", "z", "c", "n"],
        "ip": ["c", "a", "b", "z", "n"],
    }[metric]
    # the identical vectors tie exactly and keep object_id order under both storage forms
    tied = packed_rows[:2] if metric != "ip" else packed_rows[1:3]
    assert tied[0][1] == tied[1][1]


@pytest.mark.slow
def test_stored_vectors_compare_equal_across_both_storage_forms(workspaces):
    packed, legacy = workspaces
    ids = [row["object_id"] for row in ROWS]

    packed_rows = packed.get_many(ids, entity=NearDoc)
    legacy_rows = legacy.get_many(ids, entity=NearDoc)

    assert [row["embedding"] for row in packed_rows] == [row["embedding"] for row in legacy_rows]
    assert packed_rows[-1]["embedding"] is None and legacy_rows[-1]["embedding"] is None
