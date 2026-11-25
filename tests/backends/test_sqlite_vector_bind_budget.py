"""SQLite vector projections leave the bind budget available for IDs."""

import sqlite3

import pytest
import sqlalchemy as sa

import heavenbase as hb

pytestmark = [pytest.mark.fast, pytest.mark.unit]


class QueryVectorDoc(hb.Entity):
    identifier = "query-vector-doc"

    embedding = hb.field(hb.Vector[1])


@pytest.mark.slow
@pytest.mark.parametrize("storage", [None, "float64_blob"])
def test_sqlite_bulk_vector_lookup_at_bind_limit(tmp_path, storage):
    bind_limit = 32766 if sqlite3.sqlite_version_info >= (3, 32, 0) else 999
    backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / "vectors.db"), vector_packed_type=storage)
    backend._sql_id_lookup_batch_size = bind_limit
    ws = hb.HeavenBase("query-vector-bulk", detached=True, backends={"main": backend})
    try:
        ws.register(QueryVectorDoc)
        item = {"object_id": "present", "embedding": [42.0]}
        ws.upsert(QueryVectorDoc, item)

        engine = backend.db.engine
        bind_counts = []

        def count_binds(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                bind_counts.append(len(parameters))

        sa.event.listen(engine, "before_cursor_execute", count_binds)
        try:
            ids = ["present", *(f"missing-{i}" for i in range(bind_limit - 1))]
            assert backend.get_many(QueryVectorDoc.identifier, ids) == [item] + [None] * (bind_limit - 1)
            assert max(bind_counts) <= bind_limit
        finally:
            sa.event.remove(engine, "before_cursor_execute", count_binds)
    finally:
        ws.release()
