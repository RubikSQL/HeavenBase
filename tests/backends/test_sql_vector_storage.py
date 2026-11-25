"""SQLite keeps Vector columns as JSON text by default; packed float64 blobs are an opt-in that still reads JSON text."""

from __future__ import annotations

import json
import math
import sqlite3
import struct
from dataclasses import replace

import pytest
from sqlalchemy import LargeBinary
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect

import heavenbase as hb
from heavenbase.database.dialect.registry import database_dialects
from heavenbase.database.types import DatabaseVectorType
from heavenbase.utils.serialize import dumps_json

pytestmark = [pytest.mark.fast, pytest.mark.unit]

ENTITY = "vector-storage-doc"
EXACT_DOUBLES = [0.1, 1 / 3, -0.0, 5e-324, 1e-300, 1e300, 6.02214076e23, 2.0**53 + 2, float("inf"), -float("inf")]

# The table and row exactly as HeavenBase 0.1.2.2 creates them for VectorDoc on a SQLiteBackend: captured from that release
# (``register`` then ``upsert``), trailing spaces dropped. The vector column is plain TEXT, and each vector is the indented
# JSON text that release's ``dumps_json`` wrote.
LEGACY_0122_TABLE = "vector_storage_doc"
LEGACY_0122_DDL = """CREATE TABLE vector_storage_doc (
\tobject_id VARCHAR(63) COLLATE "BINARY" NOT NULL CONSTRAINT ck_hb_text_000232870690 CHECK (typeof("object_id") IN ('text', 'null')),
\ttitle VARCHAR(255) COLLATE "BINARY" CONSTRAINT ck_hb_text_000284225581 CHECK (typeof("title") IN ('text', 'null')),
\tembedding TEXT,
\tPRIMARY KEY (object_id)
)"""
LEGACY_0122_VECTOR = [0.1, 1 / 3, -2.5]
LEGACY_0122_TEXT = "[\n    0.1,\n    0.3333333333333333,\n    -2.5\n]"


class VectorDoc(hb.Entity):
    identifier = ENTITY

    title = hb.field(hb.ShortText)
    embedding = hb.field(hb.Vector[10]).optional()


class LegacyVectorDoc(hb.Entity):
    identifier = ENTITY

    title = hb.field(hb.ShortText)
    embedding = hb.field(hb.Vector[3]).optional()


def _open(path, ws_id: str, entity: type = VectorDoc, **backend_options):
    backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(path), **backend_options)
    ws = hb.HeavenBase(ws_id, detached=True, backends={"main": backend})
    ws.register(entity)
    return ws, backend


@pytest.fixture
def workspace(tmp_path):
    """A SQLite workspace that opted in to packed float64 vector storage."""
    ws, backend = _open(tmp_path / "vectors.db", "sql-vector-storage", vector_packed_type="float64_blob")
    yield ws, backend
    ws.release()


def _table(backend) -> str:
    return backend.tables[ENTITY].name


def _storage_classes(backend) -> dict[str, str]:
    rows = backend.db.execute(f'SELECT object_id, typeof(embedding) AS kind FROM "{_table(backend)}"', readonly=True).to_dicts()
    return {row["object_id"]: row["kind"] for row in rows}


def _store_as_json_text(backend, object_id: str, vector: list[float]) -> None:
    """Rewrite one row the way the JSON text form stores vectors."""
    backend.db.execute(
        f'UPDATE "{_table(backend)}" SET embedding = :payload WHERE object_id = :object_id',
        {"payload": dumps_json(vector, compact=True), "object_id": object_id},
        readonly=False,
    )


def _same_bits(left: list[float], right: list[float]) -> bool:
    return [struct.pack("<d", value) for value in left] == [struct.pack("<d", value) for value in right]


def _legacy_0122_database(path) -> None:
    """Create the vector table and its rows exactly as HeavenBase 0.1.2.2 left them."""
    with sqlite3.connect(path) as connection:
        connection.execute(LEGACY_0122_DDL)
        connection.execute(f"INSERT INTO {LEGACY_0122_TABLE} VALUES ('legacy', 'legacy', ?)", (LEGACY_0122_TEXT,))
        connection.execute(f"INSERT INTO {LEGACY_0122_TABLE} VALUES ('empty', 'empty', NULL)")
    connection.close()


def _raw_rows(path) -> dict[str, tuple[str, object]]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(f"SELECT object_id, typeof(embedding), embedding FROM {LEGACY_0122_TABLE}").fetchall()
        declared = {row[1]: row[2] for row in connection.execute(f"PRAGMA table_info({LEGACY_0122_TABLE})")}
    connection.close()
    assert declared["embedding"] == "TEXT"  # an existing column is never retyped
    return {object_id: (kind, value) for object_id, kind, value in rows}


@pytest.mark.slow
def test_sqlite_vectors_default_to_json_text_that_every_release_reads(tmp_path):
    ws, backend = _open(tmp_path / "default.db", "sql-vector-default")
    try:
        assert backend.db.dialect_spec.vector_packed_type is None
        assert database_dialects.get("sqlite").vector_packed_type is None
        ws.upsert(VectorDoc, {"object_id": "exact", "title": "exact", "embedding": EXACT_DOUBLES})
        ws.upsert(VectorDoc, {"object_id": "empty", "title": "empty", "embedding": None})

        assert _storage_classes(backend) == {"exact": "text", "empty": "null"}
        assert _same_bits(ws.get("exact", entity=VectorDoc)["embedding"], EXACT_DOUBLES)
        assert ws.get("empty", entity=VectorDoc)["embedding"] is None
    finally:
        ws.release()


def test_a_0122_text_vector_column_upgrades_in_place_under_the_default(tmp_path):
    """A store written by 0.1.2.2 keeps reading, and every write stays JSON text that 0.1.2.2 can read back."""
    path = tmp_path / "legacy-default.db"
    _legacy_0122_database(path)
    ws, backend = _open(path, "sql-vector-legacy-default", LegacyVectorDoc)
    try:
        assert _table(backend) == LEGACY_0122_TABLE
        assert ws.get("legacy", entity=LegacyVectorDoc)["embedding"] == LEGACY_0122_VECTOR
        assert ws.get("empty", entity=LegacyVectorDoc)["embedding"] is None
        ws.upsert(LegacyVectorDoc, {"object_id": "fresh", "title": "fresh", "embedding": [1.0, 2.0, 3.0]})
        ws.upsert(LegacyVectorDoc, {"object_id": "legacy", "title": "legacy", "embedding": LEGACY_0122_VECTOR})
        assert [row["embedding"] for row in ws.iter_rows(LegacyVectorDoc, batch_size=1)] == [None, [1.0, 2.0, 3.0], LEGACY_0122_VECTOR]
    finally:
        ws.release()

    raw = _raw_rows(path)
    assert {object_id: kind for object_id, (kind, _value) in raw.items()} == {"legacy": "text", "fresh": "text", "empty": "null"}
    # 0.1.2.2 decodes a vector cell with loads_json, which these text cells satisfy
    assert json.loads(raw["fresh"][1]) == [1.0, 2.0, 3.0]
    assert json.loads(raw["legacy"][1]) == LEGACY_0122_VECTOR


def test_a_0122_text_vector_column_reads_both_forms_after_opting_in_to_packed_storage(tmp_path):
    """Opting in on a 0.1.2.2 store packs new writes into the TEXT column and keeps reading the text rows next to them."""
    path = tmp_path / "legacy-packed.db"
    _legacy_0122_database(path)
    ws, backend = _open(path, "sql-vector-legacy-packed", LegacyVectorDoc, vector_packed_type="float64_blob")
    try:
        assert backend.db.dialect_spec.vector_packed_type == "float64_blob"
        ws.upsert(LegacyVectorDoc, {"object_id": "fresh", "title": "fresh", "embedding": [1.0, 2.0, 3.0]})
        assert _storage_classes(backend) == {"legacy": "text", "fresh": "blob", "empty": "null"}

        rows = ws.get_many(["legacy", "fresh", "empty"], entity=LegacyVectorDoc)
        assert [row["embedding"] for row in rows] == [LEGACY_0122_VECTOR, [1.0, 2.0, 3.0], None]
        assert sorted(row["embedding"] or [] for row in backend.scan(ENTITY)) == [[], LEGACY_0122_VECTOR, [1.0, 2.0, 3.0]]
        assert [row["embedding"] for row in ws.iter_rows(LegacyVectorDoc, batch_size=1)] == [None, [1.0, 2.0, 3.0], LEGACY_0122_VECTOR]
        # rewriting a legacy row moves it to the packed form
        ws.upsert(LegacyVectorDoc, {"object_id": "legacy", "title": "legacy", "embedding": LEGACY_0122_VECTOR})
        assert _storage_classes(backend) == {"legacy": "blob", "fresh": "blob", "empty": "null"}
        assert ws.get("legacy", entity=LegacyVectorDoc)["embedding"] == LEGACY_0122_VECTOR
    finally:
        ws.release()
    assert _raw_rows(path)["fresh"] == ("blob", struct.pack("<3d", 1.0, 2.0, 3.0))


def test_a_0122_text_vector_column_reads_after_opting_in_when_the_driver_does_not_declare_native_bytes(tmp_path, monkeypatch):
    """Before SQLAlchemy 2.0.11 pysqlite did not declare native bytes, so ``LargeBinary`` ran ``bytes()`` on every value.

    The packed column still holds the 0.1.2.2 JSON text; it must reach the vector decoder as text instead of failing
    with ``TypeError: string argument without an encoding``.
    """
    from sqlalchemy.dialects.sqlite.pysqlite import SQLiteDialect_pysqlite

    monkeypatch.setattr(SQLiteDialect_pysqlite, "returns_native_bytes", False, raising=False)
    path = tmp_path / "legacy-packed-old-driver.db"
    _legacy_0122_database(path)
    ws, backend = _open(path, "sql-vector-legacy-packed-old-driver", LegacyVectorDoc, vector_packed_type="float64_blob")
    try:
        ws.upsert(LegacyVectorDoc, {"object_id": "fresh", "title": "fresh", "embedding": [1.0, 2.0, 3.0]})
        assert _storage_classes(backend) == {"legacy": "text", "fresh": "blob", "empty": "null"}
        rows = ws.get_many(["legacy", "fresh", "empty"], entity=LegacyVectorDoc)
        assert [row["embedding"] for row in rows] == [LEGACY_0122_VECTOR, [1.0, 2.0, 3.0], None]
        assert ws.get("legacy", entity=LegacyVectorDoc)["embedding"] == LEGACY_0122_VECTOR
        assert [row["embedding"] for row in ws.iter_rows(LegacyVectorDoc, batch_size=1)] == [None, [1.0, 2.0, 3.0], LEGACY_0122_VECTOR]
    finally:
        ws.release()

    spec = replace(database_dialects.get("sqlite"), vector_packed_type="float64_blob")
    dialect = sqlite_dialect.dialect()
    process = DatabaseVectorType(dialect_spec=spec).dialect_impl(dialect).result_processor(dialect, None)
    assert process(LEGACY_0122_TEXT) == LEGACY_0122_VECTOR
    assert process(struct.pack("<2d", 1.0, 0.5)) == [1.0, 0.5]
    assert process(memoryview(struct.pack("<2d", 1.0, 0.5))) == [1.0, 0.5]
    assert process(None) is None


@pytest.mark.slow
def test_new_vector_writes_are_packed_float64_blobs_that_round_trip_bit_exactly(workspace):
    ws, backend = workspace
    ws.upsert(VectorDoc, {"object_id": "exact", "title": "exact", "embedding": EXACT_DOUBLES})
    ws.upsert(VectorDoc, {"object_id": "empty", "title": "empty", "embedding": None})

    assert _storage_classes(backend) == {"exact": "blob", "empty": "null"}
    stored = ws.get("exact", entity=VectorDoc)["embedding"]
    assert _same_bits(stored, EXACT_DOUBLES)
    assert math.copysign(1.0, stored[2]) == -1.0
    assert ws.get("empty", entity=VectorDoc)["embedding"] is None
    raw = backend.db.execute(f'SELECT embedding FROM "{_table(backend)}" WHERE object_id = :object_id', {"object_id": "exact"}, readonly=True).scalar()
    assert bytes(raw) == struct.pack("<10d", *EXACT_DOUBLES)


def test_nan_components_survive_packed_storage(workspace):
    ws, _backend = workspace
    vector = [float("nan")] + [1.0] * 9
    ws.upsert(VectorDoc, {"object_id": "nan", "title": "nan", "embedding": vector})

    stored = ws.get("nan", entity=VectorDoc)["embedding"]
    assert math.isnan(stored[0]) and stored[1:] == [1.0] * 9


def test_rows_stored_as_json_text_keep_reading_next_to_packed_rows(workspace):
    ws, backend = workspace
    ws.upsert_many(
        VectorDoc,
        [
            {"object_id": "legacy", "title": "legacy", "embedding": EXACT_DOUBLES},
            {"object_id": "packed", "title": "packed", "embedding": EXACT_DOUBLES},
        ],
    )
    _store_as_json_text(backend, "legacy", EXACT_DOUBLES)
    assert _storage_classes(backend) == {"legacy": "text", "packed": "blob"}

    rows = ws.get_many(["legacy", "packed"], entity=VectorDoc)
    assert _same_bits(rows[0]["embedding"], EXACT_DOUBLES) and _same_bits(rows[1]["embedding"], EXACT_DOUBLES)
    assert [row["embedding"] for row in backend.scan(ENTITY)] == [EXACT_DOUBLES, EXACT_DOUBLES]
    assert [row["embedding"] for row in backend.iter_scan(ENTITY, batch_size=1)] == [EXACT_DOUBLES, EXACT_DOUBLES]
    # rewriting a legacy row moves it to the packed form
    ws.upsert(VectorDoc, {"object_id": "legacy", "title": "legacy", "embedding": EXACT_DOUBLES})
    assert _storage_classes(backend) == {"legacy": "blob", "packed": "blob"}


def test_projection_without_the_vector_never_decodes_it(workspace, monkeypatch):
    ws, backend = workspace
    ws.upsert(VectorDoc, {"object_id": "v1", "title": "one", "embedding": EXACT_DOUBLES})
    decorator = DatabaseVectorType(dialect_spec=backend.db.dialect_spec)

    def refuse(*_args, **_kwargs):
        raise AssertionError("vector column was decoded for a projection that excluded it")

    monkeypatch.setattr(type(decorator), "process_result_value", refuse)
    assert ws.get("v1", entity=VectorDoc, columns=["title"]) == {"object_id": "v1", "title": "one"}


def test_database_vector_type_packs_for_an_opted_in_sqlite_dialect_and_accepts_every_stored_form():
    spec = replace(database_dialects.get("sqlite"), vector_packed_type="float64_blob")
    assert spec.vector_packed_type == "float64_blob"
    dialect = sqlite_dialect.dialect()
    decorator = DatabaseVectorType(dialect_spec=spec)

    assert isinstance(decorator.load_dialect_impl(dialect), LargeBinary)
    packed = decorator.process_bind_param([1, 0.5], dialect)
    assert packed == struct.pack("<2d", 1.0, 0.5)
    assert decorator.process_bind_param(None, dialect) is None
    assert decorator.process_result_value(packed, dialect) == [1.0, 0.5]
    assert decorator.process_result_value(memoryview(packed), dialect) == [1.0, 0.5]
    assert decorator.process_result_value(bytearray(packed), dialect) == [1.0, 0.5]
    assert decorator.process_result_value(b"", dialect) == []
    assert decorator.process_result_value("[1, 0.5]", dialect) == [1.0, 0.5]
    assert decorator.process_result_value(LEGACY_0122_TEXT, dialect) == LEGACY_0122_VECTOR
    assert decorator.process_result_value([1, 0.5], dialect) == [1.0, 0.5]
    assert decorator.process_result_value(None, dialect) is None
    with pytest.raises(ValueError, match="whole number of float64"):
        decorator.process_result_value(packed[:-1], dialect)


@pytest.mark.slow
def test_vector_packed_type_opts_in_per_backend_database_or_provider_and_json_text_refuses_packed_rows(tmp_path):
    """``vector_packed_type`` on a backend, a Database, or the provider config selects the form; text never decodes a packed row."""
    text_uri = hb.utils.path_to_file_uri(tmp_path / "text-vectors.db")
    ws = hb.HeavenBase(
        "sql-vector-text",
        detached=True,
        backends={"main": {"type": "sqlite", "name": "main", "database": text_uri, "vector_packed_type": "json_text"}},
    )
    backend = ws.backends.get("main")
    ws.register(VectorDoc)
    ws.upsert(VectorDoc, {"object_id": "t1", "title": "text", "embedding": EXACT_DOUBLES})

    assert backend.db.spec.vector_packed_type == backend.db.dialect_spec.vector_packed_type == "json_text"
    assert _storage_classes(backend) == {"t1": "text"}
    assert _same_bits(ws.get("t1", entity=VectorDoc)["embedding"], EXACT_DOUBLES)
    # a packed row written by an opted-in backend is refused through the text-form column, never decoded as garbage
    backend.db.execute(
        f'UPDATE "{_table(backend)}" SET embedding = :payload WHERE object_id = :object_id',
        {"payload": struct.pack("<10d", *EXACT_DOUBLES), "object_id": "t1"},
        readonly=False,
    )
    refusal = r"SQLite BLOB of 80 bytes\. Reading packed vectors requires vector_packed_type: float64_blob"
    with pytest.raises(ValueError, match=refusal):
        ws.get("t1", entity=VectorDoc)
    ws.release()

    # the default (no opt-in) is the same text form and refuses the packed row the same way
    default_ws, default_backend = _open(tmp_path / "text-vectors.db", "sql-vector-text-default")
    assert default_backend.db.dialect_spec.vector_packed_type is None
    with pytest.raises(ValueError, match=refusal):
        default_ws.get("t1", entity=VectorDoc)
    default_ws.release()
    packed_ws, _packed_backend = _open(tmp_path / "text-vectors.db", "sql-vector-text-packed", vector_packed_type="float64_blob")
    assert _same_bits(packed_ws.get("t1", entity=VectorDoc)["embedding"], EXACT_DOUBLES)
    packed_ws.release()

    with pytest.raises(ValueError, match="vector_packed_type 'float32_blob' is unsupported"):
        hb.backends.SQLiteBackend("main", text_uri, vector_packed_type="float32_blob")
    assert hb.Database(provider="sqlite", database=text_uri).dialect_spec.vector_packed_type is None
    assert hb.Database(provider="sqlite", database=text_uri, vector_packed_type="float64_blob").dialect_spec.vector_packed_type == "float64_blob"
    engine = hb.DBEngine(config={"local_root": hb.utils.path_to_file_uri(tmp_path), "providers": {"sqlite": {"vector_packed_type": "float64_blob"}}})
    assert engine.resolve({"provider": "sqlite", "database": "deployment"}).dialect_spec.vector_packed_type == "float64_blob"
    engine = hb.DBEngine(config={"local_root": hb.utils.path_to_file_uri(tmp_path), "providers": {"sqlite": {"vector_packed_type": "json_text"}}})
    assert engine.resolve({"provider": "sqlite", "database": "deployment"}).dialect_spec.vector_packed_type == "json_text"


def test_json_text_vectors_skip_the_tagged_value_walk_and_keep_their_text(monkeypatch):
    """A vector holds only floats, so its JSON text skips serialize's tagged-value walk both ways; the text is unchanged.

    The walk visits every component, and on a read-heavy JSON-text workload it was most of each vector decode.
    """
    from heavenbase.utils import serialize

    dialect = sqlite_dialect.dialect()
    decorator = DatabaseVectorType(dialect_spec=database_dialects.get("sqlite"))
    expected = dumps_json(EXACT_DOUBLES, compact=True)

    def refuse(*_args, **_kwargs):
        raise AssertionError("a vector went through the tagged-value walk")

    monkeypatch.setattr(serialize, "_json_ready", refuse)
    monkeypatch.setattr(serialize, "_json_restore", refuse)
    assert decorator.process_bind_param(EXACT_DOUBLES, dialect) == expected
    assert _same_bits(decorator.process_result_value(expected, dialect), EXACT_DOUBLES)
    assert decorator.process_result_value(LEGACY_0122_TEXT, dialect) == LEGACY_0122_VECTOR


def test_dialects_without_packed_storage_keep_the_json_text_form():
    dialect = mysql_dialect.dialect()
    text_only = database_dialects.get("mysql")
    decorator = DatabaseVectorType(dialect_spec=text_only)

    assert text_only.vector_packed_type is None
    assert not isinstance(decorator.load_dialect_impl(dialect), LargeBinary)
    assert decorator.process_bind_param([1, 0.5], dialect) == "[1.0, 0.5]"
    assert decorator.process_result_value("[1.0, 0.5]", dialect) == [1.0, 0.5]
    assert decorator.process_result_value(LEGACY_0122_TEXT, dialect) == LEGACY_0122_VECTOR
    # a driver that returns text columns as bytes is decoded as JSON text, packed bytes are refused rather than decoded as garbage
    assert decorator.process_result_value(b"[1.0, 0.5]", dialect) == [1.0, 0.5]
    assert decorator.process_result_value(memoryview(b"[2.0]"), dialect) == [2.0]
    with pytest.raises(ValueError, match="16 bytes that are not JSON text; dialect 'mysql' stores vectors as JSON text") as refused:
        decorator.process_result_value(struct.pack("<2d", 1.0, 0.5), dialect)
    assert isinstance(refused.value.__cause__, ValueError)
    with pytest.raises(ValueError, match="8 bytes that are not JSON text"):
        decorator.process_result_value(struct.pack("<d", 0.0), dialect)  # valid UTF-8, not JSON


@pytest.mark.slow
def test_a_registered_workspace_opts_in_through_the_provider_config_not_its_backend_definition(tmp_path):
    """A registered workspace's backend definition is its construction contract, so an existing one opts in by config."""
    from heavenbase.context import load_bootstrap
    from heavenbase.utils.config import ConfigManager

    path = tmp_path / "registered-vectors.db"
    _legacy_0122_database(path)
    backend = {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(path)}
    bootstrap = {
        "version": 1,
        "root": str(tmp_path / "home"),
        "workspace": "default",
        "backend": load_bootstrap(home_path=tmp_path / "missing-bootstrap.yaml").backend,
        "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
    }

    def open_context() -> hb.Context:
        return hb.Context.load(bootstrap, config=ConfigManager(root=str(tmp_path / "home"), setup=True))

    first = open_context()
    try:
        ws = hb.HeavenBase("registered-vectors", context=first, backends={"main": dict(backend)})
        ws.register(LegacyVectorDoc)
        ws.upsert(LegacyVectorDoc, {"object_id": "text", "title": "text", "embedding": [1.0, 2.0, 3.0]})
        assert _storage_classes(ws.backends.get("main")) == {"legacy": "text", "text": "text", "empty": "null"}
        ws.release()
        with pytest.raises(FileExistsError, match="different construction contract"):
            hb.HeavenBase("registered-vectors", context=first, backends={"main": {**backend, "vector_packed_type": "float64_blob"}})
    finally:
        first.close()

    second = open_context()
    try:
        second.config.set("heavenbase.db.providers.sqlite.vector_packed_type", "float64_blob")
        ws = hb.HeavenBase.load("registered-vectors", context=second)
        ws.register(LegacyVectorDoc)
        reopened = ws.backends.get("main")
        assert reopened.db.dialect_spec.vector_packed_type == "float64_blob"
        assert ws.get("legacy", entity=LegacyVectorDoc)["embedding"] == LEGACY_0122_VECTOR
        ws.upsert(LegacyVectorDoc, {"object_id": "packed", "title": "packed", "embedding": [4.0, 5.0, 6.0]})
        assert _storage_classes(reopened) == {"legacy": "text", "text": "text", "empty": "null", "packed": "blob"}
        rows = ws.get_many(["legacy", "text", "empty", "packed"], entity=LegacyVectorDoc)
        assert [row["embedding"] for row in rows] == [LEGACY_0122_VECTOR, [1.0, 2.0, 3.0], None, [4.0, 5.0, 6.0]]
        ws.release()
    finally:
        second.close()


PACKED_REFUSAL = "cannot use vector_packed_type 'float64_blob': packed vectors need dynamically typed columns"


def test_float64_blob_needs_dynamically_typed_columns_and_other_dialects_refuse_it():
    """A packed vector binds as bytes whatever the live column is, so only SQLite's dynamically typed columns accept it."""
    assert database_dialects.get("sqlite").supports("database-dynamic-typing") is True
    for name in ("duckdb", "mysql", "mssql", "oracle", "trino"):
        spec = database_dialects.get(name)
        assert spec.supports("database-dynamic-typing") is False and spec.vector_native_type is None
        with pytest.raises(ValueError, match=f"Database dialect '{name}' {PACKED_REFUSAL}"):
            replace(spec, vector_packed_type="float64_blob")
        assert replace(spec, vector_packed_type="json_text").vector_packed_type == "json_text"
    # a native vector type wins over the packed form, so naming it there changes nothing and is not refused
    assert replace(database_dialects.get("postgresql"), vector_packed_type="float64_blob").vector_native_type == "float_array"


def test_an_existing_duckdb_store_refuses_the_packed_opt_in_and_keeps_reading(tmp_path):
    """DuckDB's typed JSON Vector column would store packed bytes as escaped text, so every opt-in route is refused."""
    pytest.importorskip("duckdb_engine")
    uri = hb.utils.path_to_file_uri(tmp_path / "vectors.duckdb")
    ws = hb.HeavenBase("sql-vector-duckdb", detached=True, backends={"main": hb.backends.DuckDBBackend("main", uri)})
    ws.register(LegacyVectorDoc)
    ws.upsert(LegacyVectorDoc, {"object_id": "old", "title": "old", "embedding": LEGACY_0122_VECTOR})
    ws.release()

    with pytest.raises(ValueError, match=PACKED_REFUSAL):
        hb.backends.DuckDBBackend("main", uri, vector_packed_type="float64_blob")
    with pytest.raises(ValueError, match=PACKED_REFUSAL):
        hb.Database(provider="duckdb", database=uri, vector_packed_type="float64_blob")
    engine = hb.DBEngine(config={"local_root": hb.utils.path_to_file_uri(tmp_path), "providers": {"duckdb": {"vector_packed_type": "float64_blob"}}})
    with pytest.raises(ValueError, match=PACKED_REFUSAL):
        engine.resolve({"provider": "duckdb", "database": "deployment"})

    reopened = hb.HeavenBase("sql-vector-duckdb-reopen", detached=True, backends={"main": hb.backends.DuckDBBackend("main", uri)})
    try:
        reopened.register(LegacyVectorDoc)
        reopened.upsert(LegacyVectorDoc, {"object_id": "new", "title": "new", "embedding": [1.0, 0.5, -2.0]})
        rows = reopened.get_many(["old", "new"], entity=LegacyVectorDoc)
        assert [row["embedding"] for row in rows] == [LEGACY_0122_VECTOR, [1.0, 0.5, -2.0]]
    finally:
        reopened.release()
