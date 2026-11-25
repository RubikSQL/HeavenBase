"""Regressions for SQLite TEXT and BLOB vectors with identical payloads."""

import sqlite3
import struct
from contextlib import closing
from dataclasses import replace

import pytest
import sqlalchemy as sa

import heavenbase as hb
from heavenbase.database.dialect.registry import database_dialects
from heavenbase.database.types import DatabaseVectorType

pytestmark = [pytest.mark.fast, pytest.mark.unit]


class CollisionVectorDoc(hb.Entity):
    identifier = "collision-vector-doc"

    embedding = hb.field(hb.Vector[1])


def _open(path, name, vector_packed_type, **options):
    backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(path), vector_packed_type=vector_packed_type, **options)
    ws = hb.HeavenBase(name, detached=True, backends={"main": backend})
    ws.register(CollisionVectorDoc)
    return ws, backend


@pytest.mark.slow
@pytest.mark.parametrize("text_mode", [None, "json_text"])
def test_sqlite_text_reader_refuses_a_packed_vector_that_looks_like_json(tmp_path, text_mode):
    path = tmp_path / "collision.db"
    payload = b"[12345] "
    vector = list(struct.unpack("<d", payload))
    packed, backend = _open(path, "packed-collision-vector", "float64_blob")
    try:
        packed.upsert(CollisionVectorDoc, {"object_id": "packed", "embedding": vector})
        assert packed.get("packed", entity=CollisionVectorDoc)["embedding"] == vector
        assert backend.db.execute("SELECT embedding FROM collision_vector_doc", readonly=True).scalar() == payload
    finally:
        packed.release()

    text, _ = _open(path, "text-collision-vector", text_mode)
    try:
        with pytest.raises(ValueError, match="SQLite BLOB"):
            text.get("packed", entity=CollisionVectorDoc)
        text.upsert(CollisionVectorDoc, {"object_id": "text", "embedding": [12345.0]})
        assert text.get("text", entity=CollisionVectorDoc)["embedding"] == [12345.0]
    finally:
        text.release()


@pytest.mark.slow
@pytest.mark.parametrize("storage", [None, "json_text", "float64_blob"])
def test_workspace_distinguishes_packed_rows_with_sqlite_text_converter(tmp_path, monkeypatch, storage):
    path = tmp_path / "legacy-text.db"
    payload = b"[123456]"
    vector = list(struct.unpack("<d", payload))
    text, _ = _open(path, "converter-text-writer", None)
    try:
        text.upsert(CollisionVectorDoc, {"object_id": "text", "embedding": [123456.0]})
    finally:
        text.release()

    packed, backend = _open(path, "converter-packed-writer", "float64_blob")
    try:
        packed.upsert(CollisionVectorDoc, {"object_id": "packed", "embedding": vector})
        assert backend.db.execute("SELECT embedding FROM collision_vector_doc WHERE object_id = 'packed'", readonly=True).scalar() == payload
    finally:
        packed.release()

    monkeypatch.setitem(sqlite3.converters, "TEXT", lambda raw: raw.decode("utf-8"))
    reader, _ = _open(path, "converter-reader", storage, pool={"connect_args": {"detect_types": sqlite3.PARSE_DECLTYPES}})
    try:
        assert reader.get("text", entity=CollisionVectorDoc)["embedding"] == [123456.0]
        if storage == "float64_blob":
            assert reader.get("packed", entity=CollisionVectorDoc)["embedding"] == vector
        else:
            with pytest.raises(ValueError, match="SQLite BLOB of 8 bytes"):
                reader.get("packed", entity=CollisionVectorDoc)
    finally:
        reader.release()


@pytest.mark.parametrize("text_factory", [str, bytes])
@pytest.mark.parametrize("storage", [None, "json_text", "float64_blob"])
def test_typed_sqlite_read_distinguishes_text_and_blob_with_the_same_bytes(text_factory, storage):
    payload = b"[12345] "
    vector = list(struct.unpack("<d", payload))
    spec = replace(database_dialects.get("sqlite"), vector_packed_type=storage)
    with closing(sqlite3.connect(":memory:")) as raw:
        raw.text_factory = text_factory
        engine = sa.create_engine("sqlite://", creator=lambda: raw)
        vectors = sa.Table(
            "vectors",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("embedding", DatabaseVectorType(dialect_spec=spec)),
        )
        try:
            with engine.begin() as conn:
                vectors.create(conn)
                conn.exec_driver_sql("INSERT INTO vectors VALUES (1, '[12345] '), (2, ?), (3, NULL), (4, X'010203')", (payload,))
                kinds = conn.exec_driver_sql("SELECT typeof(embedding) FROM vectors ORDER BY id").scalars().all()
                assert kinds == (["text", "blob", "null", "blob"] if text_factory is str else [b"text", b"blob", b"null", b"blob"])

                read = sa.select(vectors.c.embedding).where(vectors.c.id == sa.bindparam("row_id"))
                assert conn.execute(read, {"row_id": 1}).scalar_one() == [12345.0]
                assert conn.execute(read, {"row_id": 3}).scalar_one() is None
                if storage == "float64_blob":
                    assert conn.execute(read, {"row_id": 2}).scalar_one() == vector
                    with pytest.raises(ValueError, match="whole number of float64"):
                        conn.execute(read, {"row_id": 4}).scalar_one()
                else:
                    with pytest.raises(ValueError, match="SQLite BLOB of 8 bytes"):
                        conn.execute(read, {"row_id": 2}).scalar_one()
        finally:
            engine.dispose()


@pytest.mark.parametrize("first_storage", ["json_text", "float64_blob"])
def test_typed_sqlite_statement_cache_keeps_vector_storage_modes_distinct(first_storage):
    engine = sa.create_engine("sqlite://")
    payload = b"[123456]"
    vector = list(struct.unpack("<d", payload))
    spec = database_dialects.get("sqlite")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE vectors (embedding TEXT)")
            conn.exec_driver_sql("INSERT INTO vectors VALUES (?)", (payload,))
            second_storage = "float64_blob" if first_storage == "json_text" else "json_text"
            for storage in (first_storage, second_storage):
                vector_type = DatabaseVectorType(dialect_spec=replace(spec, vector_packed_type=storage))
                vectors = sa.table("vectors", sa.column("embedding", vector_type))
                if storage == "float64_blob":
                    assert conn.scalar(sa.select(vectors.c.embedding)) == vector
                else:
                    with pytest.raises(ValueError, match="SQLite BLOB of 8 bytes"):
                        conn.scalar(sa.select(vectors.c.embedding))
    finally:
        engine.dispose()
