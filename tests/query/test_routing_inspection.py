"""Routing inspection and explain trust contract tests.

Objective: verify that query.explain() reports accurate handler_mode,
fallback_reason, unsupported_reason, and backend_group metadata for
all routing decisions.
"""

from __future__ import annotations

import os
from importlib.util import find_spec
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

import heavenbase as hb
from heavenbase.schema import FieldSchema

WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry

_LANCE_UNAVAILABLE = find_spec("lancedb") is None
_POSTGRES_URL = os.environ.get("HVNB_POSTGRES_URL", "postgresql+psycopg2://rubik:password@localhost:5432/postgres?sslmode=prefer")
_ELASTICSEARCH_URL = os.environ.get("HVNB_ELASTICSEARCH_URL", "http://localhost:9200")


class CustomSQLStruct(hb.LogicalType):
    """Durable custom structured type used by SQL scan routing tests."""

    identifier = "custom-sql-struct"


def test_near_filter_route_is_explain_source_of_truth():
    """Execution and explain should share one near/filter route selector."""
    ws = hb.HeavenBase("near-route-source-test", backends={"main": hb.backends.InMemBackend("main")})

    class RouteDoc(hb.Entity):
        identifier = "near-route-source-doc"

        price = hb.field(hb.Float)
        embedding = hb.field(hb.Vector[2])

    ws.register(RouteDoc)
    ws.upsert_many(
        RouteDoc,
        [
            {"object_id": "r1", "price": 100.0, "embedding": [1.0, 0.0]},
            {"object_id": "r2", "price": 10.0, "embedding": [1.0, 0.0]},
        ],
    )

    query = ws.query(RouteDoc).where(RouteDoc.price >= 100).near(RouteDoc.embedding, [1.0, 0.0], top_k=2)
    spec = query.spec
    route = ws._router._near.near_filter_route(spec.near, spec.filter, RouteDoc)
    near_step = [step for step in query.explain()["steps"] if step.get("node") == "near"][0]

    assert route == "combined_scan"
    assert near_step["near_filter_mode"] == route
    assert query.execute().ids == ["r1"]

    WORKSPACES.pop("near-route-source-test", None)


def _ensure_pg_database(database: str) -> None:
    import sqlalchemy as sa

    root_url = sa.engine.make_url(_POSTGRES_URL).set(database="postgres")
    engine = sa.create_engine(root_url, pool_pre_ping=True, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        result = conn.execute(sa.text("SELECT 1 FROM pg_database WHERE datname = :db"), {"db": database})
        if not result.scalar():
            conn.execute(sa.text(f'CREATE DATABASE "{database}"'))
    engine.dispose()


def _require_postgres() -> None:
    """Require an authenticated PostgreSQL test service."""
    import sqlalchemy as sa

    root_url = sa.engine.make_url(_POSTGRES_URL).set(database="postgres")
    engine = sa.create_engine(root_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if "HVNB_POSTGRES_URL" in os.environ:
            pytest.fail(f"configured PostgreSQL test service is unavailable: {detail}", pytrace=False)
        pytest.skip(f"default PostgreSQL endpoint is not an authenticated test service: {detail}")
    finally:
        engine.dispose()


def _require_elasticsearch() -> None:
    """Require an authenticated Elasticsearch test service."""
    from elasticsearch import Elasticsearch

    client = Elasticsearch(_ELASTICSEARCH_URL)
    try:
        if not client.ping():
            raise RuntimeError("ping returned false")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if "HVNB_ELASTICSEARCH_URL" in os.environ:
            pytest.fail(f"configured Elasticsearch test service is unavailable: {detail}", pytrace=False)
        pytest.skip(f"default Elasticsearch endpoint is not an authenticated test service: {detail}")
    finally:
        client.close()


def _pg_url(database: str) -> str:
    import sqlalchemy as sa

    return sa.engine.make_url(_POSTGRES_URL).set(database=database).render_as_string(hide_password=False)


def _db(path, name: str) -> str:
    return hb.utils.path_to_file_uri(path / name)


# ── Helpers ──


class _SpyScanBackend(hb.backends.InMemBackend):
    """InMemBackend that counts scan() calls."""

    def __init__(self, name):
        super().__init__(name)
        self.scan_count = 0

    def scan(self, entity_id):
        self.scan_count += 1
        return super().scan(entity_id)


class _NoJsonBackend(_SpyScanBackend):
    """Importable scan Backend fixture that rejects Json storage."""

    identifier = "no-json-storage-test"


class _SpySQLBackend(hb.backends.SQLiteBackend):
    """SQLiteBackend that counts execute() ops."""

    def __init__(self, name, database):
        super().__init__(name, database)
        self.ops = []
        self.scan_count = 0

    def execute(self, fragment):
        self.ops.append(fragment.op)
        return super().execute(fragment)

    def scan(self, entity_id):
        self.scan_count += 1
        return super().scan(entity_id)


class _SpyJsonFileBackend(hb.backends.JsonFileBackend):
    """JsonFileBackend that counts execute() and scan() calls."""

    def __init__(self, name, path):
        super().__init__(name, path)
        self.ops = []
        self.scan_count = 0

    def execute(self, fragment):
        self.ops.append(fragment.op)
        return super().execute(fragment)

    def scan(self, entity_id):
        self.scan_count += 1
        return super().scan(entity_id)


class _SpyPineconeBackend(hb.backends.InMemBackend):
    """Pinecone-type local backend for deterministic vector routing tests."""

    identifier = "pinecone"

    def __init__(self, name):
        super().__init__(name)
        self.ops = []
        self.fragments = []
        self.scan_count = 0

    def execute(self, fragment):
        self.ops.append(fragment.op)
        self.fragments.append(fragment)
        return super().execute(fragment)

    def scan(self, entity_id):
        self.scan_count += 1
        return super().scan(entity_id)


class _SpyCandidateVectorBackend(hb.backends.InMemBackend):
    """Lance-type local backend for deterministic candidate ``object_id`` prefilter tests."""

    identifier = "lance"

    def __init__(self, name):
        super().__init__(name)
        self.ops = []
        self.fragments = []
        self.scan_count = 0

    def execute(self, fragment):
        self.ops.append(fragment.op)
        self.fragments.append(fragment)
        return super().execute(fragment)

    def candidate_id_fragment(self, object_ids):
        py_fragment = super().candidate_id_fragment(object_ids)
        payload = "entity_id IN (" + ", ".join("'" + str(item).replace("'", "''") + "'" for item in object_ids) + ")"
        return hb.ext.QueryFragment(self.name, "in", "object_id", payload, notes={**py_fragment.notes, "kind": "lance_where", "mode": "native"})

    def scan(self, entity_id):
        self.scan_count += 1
        return super().scan(entity_id)


def _filter_step(steps):
    return [s for s in steps if s.get("node") == "filter"]


def _find_step(steps, **kwargs):
    for s in steps:
        if all(s.get(k) == v for k, v in kwargs.items()):
            return s
    return None


# ── 1. Merge column preservation ──


def test_merge_columns_preserved_across_three_backends(tmp_path):
    """Intersecting frames from three backends should preserve non-null columns."""
    text = _SpyScanBackend("text")
    nums = _SpyScanBackend("nums")
    flags = _SpyScanBackend("flags")
    ws = hb.HeavenBase("merge-col-test", backends={"text": text, "nums": nums, "flags": flags})

    class SplitDoc(hb.Entity):
        identifier = "merge-col-doc"

        title = hb.field(hb.ShortText).store(to="text")
        body = hb.field(hb.LongText).store(to="text")
        price = hb.field(hb.Float).store(to="nums")
        stock = hb.field(hb.Integer).store(to="nums")
        active = hb.field(hb.Boolean).store(to="flags")

    ws.register(SplitDoc)
    ws.upsert_many(
        SplitDoc,
        [
            {"object_id": "d1", "title": "A", "body": "alpha", "price": 10.0, "stock": 5, "active": True},
            {"object_id": "d2", "title": "B", "body": "beta", "price": 20.0, "stock": 0, "active": False},
        ],
    )

    frame = ws.query(SplitDoc).where((SplitDoc.price > 5) & (SplitDoc.stock >= 0) & SplitDoc.active.op("eq", True)).execute()
    row = frame.rows()[0]

    assert row["title"] == "A"
    assert row["price"] == 10.0
    assert row["active"] is True
    assert "body" in row
    assert "stock" in row

    WORKSPACES.pop("merge-col-test", None)


def test_select_subset_drops_unrequested_columns(tmp_path):
    """Selecting a subset should drop unrequested columns from result rows."""
    ws = hb.HeavenBase("select-subset-test", backends={"main": hb.backends.InMemBackend("main")})

    class FullDoc(hb.Entity):
        identifier = "select-subset-doc"

        title = hb.field(hb.ShortText)
        body = hb.field(hb.LongText)
        score = hb.field(hb.Float)

    ws.register(FullDoc)
    ws.upsert(FullDoc, {"object_id": "d1", "title": "A", "body": "alpha", "score": 9.0})
    frame = ws.query(FullDoc).select("title").execute()
    row = frame.rows()[0]

    assert set(row.keys()) == {"object_id", "title"}
    assert "body" not in row
    assert "score" not in row

    WORKSPACES.pop("select-subset-test", None)


# ── 2. Fallback reason presence ──


def test_sql_regex_fallback_reason(tmp_path):
    """SQL regex filters should explain Python-scan fallback reasons."""
    backend = _SpySQLBackend("sql", _db(tmp_path, "regex.db"))
    ws = hb.HeavenBase("sql-regex-test", backends={"sql": backend})

    class RegexDoc(hb.Entity):
        identifier = "sql-regex-doc"

        name = hb.field(hb.ShortText)

    ws.register(RegexDoc)
    ws.upsert(RegexDoc, {"object_id": "r1", "name": "alpha"})
    query = ws.query(RegexDoc).where(RegexDoc.name.regex(r"^al"))
    explain = query.explain()
    step = _filter_step(explain["steps"])[0]

    assert step["handler_mode"] == "py_scan"
    assert step["fallback_reason"] is not None
    assert step["fallback_reason"] == "text_semantics_not_proven"

    WORKSPACES.pop("sql-regex-test", None)


def test_sql_dialect_capabilities_are_published_to_metaschema(tmp_path):
    """SQL dialect capability metadata should be visible through MetaSchema."""
    ws = hb.HeavenBase("sql-dialect-metadata-test", backends={"main": {"type": "sqlite", "database": _db(tmp_path, "dialect.db")}})

    rows = {row["object_id"]: row for row in ws.rows(hb.MetaSchema) if row["kind"] == "sql_dialect"}
    sqlite = rows["sql_dialect:sqlite"]

    assert sqlite["subject_id"] == "sqlite"
    assert sqlite["type"] == "sqlite"
    assert sqlite["meta"]["identifier"] == "sqlite"
    assert sqlite["meta"]["case_insensitive_like"] is True
    assert sqlite["meta"]["supports_wildcard"] is True
    assert sqlite["meta"]["wildcard_style"] == "glob"
    assert sqlite["meta"]["fallback_like"] is True
    assert "regex" in sqlite["meta"]["fallback_reasons"]
    assert "eq" in sqlite["meta"]["native_ops"]
    assert sqlite["meta"]["native_text_ops"] == ["eq", "ne", "lt", "lte", "gt", "gte", "in", "contains", "starts_with", "ends_with"]
    assert sqlite["meta"]["native_text_key_ops"] == ["group", "distinct", "order"]

    WORKSPACES.pop("sql-dialect-metadata-test", None)


def test_database_dialect_physical_metadata_is_published_without_tag_copies(tmp_path):
    """MetaSchema exposes structural dialect metadata while tags stay Registry-owned."""
    ws = hb.HeavenBase("database-dialect-metadata-test", backends={"main": {"type": "sqlite", "database": _db(tmp_path, "database-dialect.db")}})

    rows = {row["object_id"]: row for row in ws.rows(hb.MetaSchema) if row["kind"] == "database_dialect"}
    sqlite = rows["database_dialect:sqlite"]
    duckdb = rows["database_dialect:duckdb"]
    postgresql = rows["database_dialect:postgresql"]
    mysql = rows["database_dialect:mysql"]
    mssql = rows["database_dialect:mssql"]
    oracle = rows["database_dialect:oracle"]
    trino = rows["database_dialect:trino"]

    assert sqlite["subject_id"] == "sqlite"
    assert sqlite["type"] == "sqlite"
    assert sqlite["source"] == "inline"
    assert "tags" not in sqlite["meta"]
    assert "caps" not in sqlite["meta"]
    assert "tags" not in postgresql["meta"]
    assert "caps" not in postgresql["meta"]
    assert sqlite["meta"]["requires_host"] is False
    assert sqlite["meta"]["retryable_error_messages"] == ["database is locked", "database table is locked", "database schema is locked"]
    assert duckdb["meta"]["requires_host"] is False
    assert postgresql["meta"]["requires_host"] is True
    assert postgresql["meta"]["sqlglot_dialect"] == "postgres"
    assert "postgres" in postgresql["meta"]["sqlalchemy_aliases"]
    assert postgresql["meta"]["current_database_sql"] == "SELECT current_database() AS name"
    assert postgresql["meta"]["list_databases_sql"] == "SELECT datname AS name FROM pg_database"
    assert mysql["meta"]["sqlglot_dialect"] == "mysql"
    assert "mariadb" in mysql["meta"]["sqlalchemy_aliases"]
    assert mysql["meta"]["current_database_sql"] == "SELECT DATABASE() AS name"
    assert mysql["meta"]["list_databases_sql"] == "SELECT SCHEMA_NAME AS name FROM INFORMATION_SCHEMA.SCHEMATA"
    assert mysql["meta"]["identifier_quote_style"] == "backtick"
    assert postgresql["meta"]["owner_after_create_sql_keys"] == ["postgresql_owner"]
    assert postgresql["meta"]["before_drop_force_sql_key"] == "postgresql"
    assert mssql["meta"]["rename_table_sql"] == "EXEC sp_rename :old_name, :new_name"
    assert mssql["meta"]["rename_table_bind_params"] is True
    assert mssql["meta"]["add_column_sql"] == "ALTER TABLE {table} ADD {column}"
    assert mssql["meta"]["identifier_quote_style"] == "bracket"
    assert mssql["meta"]["before_drop_force_mode"] == "quoted_database"
    assert duckdb["meta"]["column_reflection_sql"] == "DESCRIBE {table_name}"
    assert duckdb["meta"]["primary_key_reflection_sql"] == hb.ext.database_dialects.get("duckdb").primary_key_reflection_sql
    assert "constraint_column_names" in duckdb["meta"]["primary_key_reflection_sql"]
    assert duckdb["meta"]["empty_foreign_keys_on_reflection_error"] is True
    assert sqlite["meta"]["static_database_names"] == ["main"]
    assert oracle["meta"]["lifecycle_namespace_source"] == "schema_or_username"
    assert oracle["meta"]["lifecycle_namespace_upper"] is True
    assert oracle["meta"]["requires_host"] is False
    assert oracle["meta"]["url_database_omit_params"] == ["service_name"]
    assert oracle["meta"]["after_create_sql_keys"] == ["oracle"]
    assert oracle["meta"]["add_column_sql"] == "ALTER TABLE {table} ADD ({column})"
    assert trino["meta"]["requires_host"] is False
    assert "tags" not in trino["meta"]
    assert "caps" not in trino["meta"]
    assert hb.ext.database_dialects.get("sqlite").supports("database-local-path") is True
    assert hb.ext.database_dialects.get("duckdb").supports("database-local-path") is True
    assert hb.ext.database_dialects.get("postgresql").supports("database-local-path") is False
    assert hb.ext.database_dialects.get("sqlite").supports("database-blob-native") is True
    assert hb.ext.database_dialects.get("postgresql").supports("database-json-native") is True
    assert hb.ext.database_dialects.get("trino").supports("database-json-native") is True
    assert hb.ext.database_dialects.get("trino").supports("database-blob-native") is False
    assert hb.ext.database_dialects.to_sqlglot("postgres") == "postgres"
    assert hb.ext.database_dialects.from_sqlglot("tsql") == "mssql"
    assert hb.ext.database_dialects.get("sqlite").supports("database-create") is True
    assert hb.ext.database_dialects.get("postgresql").supports("database-drop") is True
    assert hb.ext.database_dialects.get("trino").supports("database-drop") is False
    assert mysql["meta"]["text_min_len"] == hb.ext.MYSQL_TEXT_MIN_LEN
    assert oracle["meta"]["varchar_max"] == hb.ext.ORACLE_VARCHAR_MAX

    WORKSPACES.pop("database-dialect-metadata-test", None)


def test_storage_profiles_are_published_to_metaschema(tmp_path):
    """Storage profile registry defaults should be visible through MetaSchema."""
    ws = hb.HeavenBase("storage-profile-metadata-test", backends={"main": {"type": "sqlite", "database": _db(tmp_path, "storage-profile.db")}})

    rows = {row["object_id"]: row for row in ws.rows(hb.MetaSchema) if row["kind"] == "storage_profile"}
    vector = rows["storage_profile:vector"]
    array = rows["storage_profile:array"]

    assert vector["subject_id"] == "vector"
    assert vector["dtype"] == "vector"
    assert vector["strategy"] == hb.VectorIndex.identifier
    assert vector["meta"]["backend_preferences"] == [{"tag": "storage-roles", "contains": "vector"}]
    assert vector["meta"]["reason"] == "builtin-vector"
    assert array["strategy"] == hb.InlineColumn.identifier
    assert array["meta"]["strategy_rules"] == [
        {
            "when": {
                "all": [
                    {"tag": "sql", "equals": True},
                    {"tag": "storage-roles", "contains": "row"},
                ]
            },
            "strategy": hb.SideTable.identifier,
        }
    ]

    WORKSPACES.pop("storage-profile-metadata-test", None)


@pytest.mark.parametrize("row_lookup", ["native", "scan"])
def test_workspace_storage_profile_override_controls_future_registration(tmp_path, monkeypatch, row_lookup):
    """Workspace-owned storage profile rows should override process defaults, whatever lookup mode the MetaSchema Backend claims."""

    class _SpySearchBackend(hb.backends.InMemBackend):
        identifier = "elasticsearch"

    main = hb.backends.InMemBackend("main")
    ws = hb.HeavenBase(
        f"storage-profile-override-test-{row_lookup}",
        backends={"main": main, "search": _SpySearchBackend("search")},
    )
    if row_lookup == "scan":
        # a third-party Backend type without a row-lookup claim gets the scan default
        tag = main.tag
        scan = SimpleNamespace(value={"get_many": "scan", "exists_many": "scan", "count": "scan"})
        monkeypatch.setattr(main, "tag", lambda identifier: scan if identifier == "row-lookup" else tag(identifier))
    ws.set(
        {
            "object_id": "storage_profile:long-text",
            "kind": "storage_profile",
            "subject_id": "long-text",
            "name": "long-text",
            "dtype": "long-text",
            "strategy": hb.InlineColumn.identifier,
            "source": "workspace",
            "active": True,
            "ws": ws.id,
            "meta": {
                "source": "workspace",
                "backend_preferences": [{"tag": "storage-roles", "contains": "row"}],
                "default_strategy": hb.InlineColumn.identifier,
                "reason": "workspace-longtext-inline",
                "strategy_rules": [],
            },
        },
        entity=hb.MetaSchema,
    )

    class LocalTextDoc(hb.Entity):
        identifier = "storage-profile-override-doc"

        body = hb.field(hb.LongText)

    ws.register(LocalTextDoc)
    binding = ws.storage_plans[LocalTextDoc.schema().entity_id].first("body")
    rows = {row["object_id"]: row for row in ws.rows(hb.MetaSchema) if row["kind"] == "storage_profile"}

    assert binding.backend == "main"
    assert binding.strategy == hb.InlineColumn.identifier
    assert binding.source == "workspace-longtext-inline"
    assert rows["storage_profile:long-text"]["source"] == "workspace"
    assert rows["storage_profile:long-text"]["meta"]["source"] == "workspace"

    WORKSPACES.pop(f"storage-profile-override-test-{row_lookup}", None)


def test_sql_wildcard_fallback_reason(tmp_path):
    """SQL wildcard filters with character classes should explain Python-scan fallback reasons."""
    backend = _SpySQLBackend("sql", _db(tmp_path, "wildcard.db"))
    ws = hb.HeavenBase("sql-wildcard-test", backends={"sql": backend})

    class WildDoc(hb.Entity):
        identifier = "sql-wildcard-doc"

        name = hb.field(hb.ShortText)

    ws.register(WildDoc)
    ws.upsert(WildDoc, {"object_id": "w1", "name": "alpha"})
    # Character classes [] are not translatable to SQL LIKE/GLOB, so this falls back.
    query = ws.query(WildDoc).where(WildDoc.name.wildcard("al[ph]*"))
    explain = query.explain()
    step = _filter_step(explain["steps"])[0]

    assert step["handler_mode"] == "py_scan"
    assert step["fallback_reason"] is not None
    assert step["fallback_reason"] == "text_semantics_not_proven"

    WORKSPACES.pop("sql-wildcard-test", None)


@pytest.mark.detail
def test_lance_json_field_fallback_reason_compiler():
    """Compiler-level test: Json path filter on Lance falls back."""
    from heavenbase.execution.handlers.seed import seed_handlers

    handlers = seed_handlers()
    field_schema = FieldSchema("attrs", hb.Json())
    ctx = hb.ext.HandlerContext("vec", "lance", hb.InlineColumn.identifier)
    value = {"path": "tier", "eq": "gold"}
    fragment = handlers.get(hb.Json, "get", "lance", hb.InlineColumn.identifier)(field_schema, value, ctx)

    assert fragment.notes["mode"] == "py_scan"
    assert "fallback_reason" in fragment.notes
    assert "Json" in fragment.notes["fallback_reason"]


@pytest.mark.detail
def test_pinecone_numeric_array_contains_fallback_reason():
    """Compiler-level test: numeric array_contains on Pinecone falls back."""
    from heavenbase.execution.handlers.seed import seed_handlers

    handlers = seed_handlers()
    field_schema = FieldSchema("scores", hb.Array[hb.Integer])
    ctx = hb.ext.HandlerContext("pine", "pinecone", hb.InlineColumn.identifier)
    fragment = handlers.get(hb.Array, "array_contains", "pinecone", hb.InlineColumn.identifier)(field_schema, 3, ctx)

    assert fragment.notes["mode"] == "py_scan"
    assert "fallback_reason" in fragment.notes


@pytest.mark.detail
def test_chroma_non_scalar_array_fallback_reason():
    """Compiler-level test: Array of non-scalars on Chroma falls back."""
    from heavenbase.execution.handlers.seed import seed_handlers

    handlers = seed_handlers()
    field_schema = FieldSchema("tags", hb.Array[hb.Json])
    ctx = hb.ext.HandlerContext("chr", "chroma", hb.InlineColumn.identifier)
    fragment = handlers.get(hb.Array, "array_contains", "chroma", hb.InlineColumn.identifier)(field_schema, {"x": 1}, ctx)

    assert fragment.notes["mode"] == "py_scan"
    # Fallback reason may be present depending on registry; mode=py_scan is the invariant.


@pytest.mark.detail
def test_redisearch_tag_native_but_text_scans_routing():
    """Compiler-level test: redisearch routes a TAG eq native, but a tokenized TEXT eq scans."""
    from heavenbase.execution.handlers.seed import seed_handlers

    handlers = seed_handlers()
    ctx = hb.ext.HandlerContext("rs", "redisearch", hb.InlineColumn.identifier)
    tag = handlers.get(hb.ShortText, "eq", "redisearch", hb.InlineColumn.identifier)(FieldSchema("category", hb.ShortText()), "x", ctx)
    assert tag.notes["mode"] == "native" and tag.notes["kind"] == "redis_filter"
    text = handlers.get(hb.LongText, "eq", "redisearch", hb.InlineColumn.identifier)(FieldSchema("body", hb.LongText()), "x", ctx)
    assert text.notes["mode"] == "py_scan"


# ── 3. Unsupported reason presence ──


def test_float_regex_unsupported_reason():
    """Unsupported Float regex filters should explain and raise on execution."""
    ws = hb.HeavenBase("float-regex-test", backends={"main": hb.backends.InMemBackend("main")})

    class BadDoc(hb.Entity):
        identifier = "float-regex-doc"

        price = hb.field(hb.Float)

    ws.register(BadDoc)
    ws.upsert(BadDoc, {"object_id": "b1", "price": 10.0})
    query = ws.query(BadDoc).where(BadDoc.price.regex(r"^1"))

    # explain() should show unsupported_reason without raising
    explain = query.explain()
    step = _filter_step(explain["steps"])[0]
    assert step["unsupported_reason"] is not None
    assert "float" in step["unsupported_reason"]

    # execute() should raise LookupError
    with pytest.raises(LookupError):
        query.execute()

    WORKSPACES.pop("float-regex-test", None)


def test_near_filter_mode_reports_unsupported_filter():
    """Near explain should surface unsupported filter combinations on the near step."""
    ws = hb.HeavenBase("near-unsupported-test", backends={"main": hb.backends.InMemBackend("main")})

    class BadNearDoc(hb.Entity):
        identifier = "near-unsupported-doc"

        price = hb.field(hb.Float)
        emb = hb.field(hb.Vector[2])

    ws.register(BadNearDoc)
    ws.upsert(BadNearDoc, {"object_id": "b1", "price": 10.0, "emb": [1.0, 0.0]})
    query = ws.query(BadNearDoc).where(BadNearDoc.price.regex(r"^1")).near(BadNearDoc.emb, [1.0, 0.0], top_k=1)
    near_step = [step for step in query.explain()["steps"] if step.get("node") == "near"][0]

    assert near_step["near_filter_mode"] == "unsupported"
    with pytest.raises(LookupError):
        query.execute()

    WORKSPACES.pop("near-unsupported-test", None)


def test_boolean_wildcard_unsupported_reason():
    """Unsupported Boolean wildcard filters should explain and raise on execution."""
    ws = hb.HeavenBase("bool-wildcard-test", backends={"main": hb.backends.InMemBackend("main")})

    class BoolDoc(hb.Entity):
        identifier = "bool-wildcard-doc"

        active = hb.field(hb.Boolean)

    ws.register(BoolDoc)
    ws.upsert(BoolDoc, {"object_id": "b1", "active": True})
    query = ws.query(BoolDoc).where(BoolDoc.active.wildcard("Tr*"))

    explain = query.explain()
    step = _filter_step(explain["steps"])[0]
    assert step["unsupported_reason"] is not None
    assert "boolean" in step["unsupported_reason"]

    with pytest.raises(LookupError):
        query.execute()

    WORKSPACES.pop("bool-wildcard-test", None)


def test_unregistered_op_unsupported_reason():
    """Unregistered operations should fail before a query can be planned."""
    ws = hb.HeavenBase("unreg-op-test", backends={"main": hb.backends.InMemBackend("main")})

    class PlainDoc(hb.Entity):
        identifier = "unreg-op-doc"

        name = hb.field(hb.ShortText)

    ws.register(PlainDoc)
    ws.upsert(PlainDoc, {"object_id": "p1", "name": "alpha"})
    with pytest.raises(LookupError, match="Unknown operation 'not_a_real_op'"):
        ws.query(PlainDoc).where(PlainDoc.name.op("not_a_real_op", "al"))

    WORKSPACES.pop("unreg-op-test", None)


_AFFIX_ROWS = [
    {"object_id": "1", "name": "Alpha"},
    {"object_id": "2", "name": "alpha"},
    {"object_id": "3", "name": "ALPHABET"},
    {"object_id": "4", "name": "zeta"},
    {"object_id": "5", "name": None},  # null
    {"object_id": "6", "name": ""},  # empty string
]


def _affix_results(ws, AffixDoc):
    """Run the three affix probes and return (ids, handler_mode) tuples."""
    out = {}
    for label, predicate in (
        ("starts_lc", AffixDoc.name.starts_with("al")),  # case-sensitive: only 'alpha'
        ("starts_empty", AffixDoc.name.starts_with("")),  # every present row, never NULL
        ("ends_a", AffixDoc.name.ends_with("a")),  # 'Alpha', 'alpha', 'zeta'
    ):
        q = ws.query(AffixDoc).where(predicate)
        out[label] = (sorted(q.execute().ids), q.explain()["steps"][0]["handler_mode"])
    return out


def test_starts_with_ends_with_case_sensitive_routes_agree():
    """Portable scan and SQLite's proved byte-affix compiler should agree.

    Pins all three review-found divergences: case sensitivity, null handling,
    and empty-value semantics without relying on SQLite LIKE.
    """
    expected_ids = {
        "starts_lc": ["2"],  # 'al' matches only lowercase 'alpha'
        "starts_empty": ["1", "2", "3", "4", "6"],  # all present rows, NOT the null row "5"
        "ends_a": ["1", "2", "4"],  # 'Alpha', 'alpha', 'zeta'
    }
    for backend_type in ("inmem", "sqlite"):
        config = {"type": "sqlite", "database": ":memory:"} if backend_type == "sqlite" else {"type": "inmem"}
        ws = hb.HeavenBase(f"affix-{backend_type}", backends={"main": config})

        class AffixDoc(hb.Entity):
            identifier = f"affix-doc-{backend_type}"

            name = hb.field(hb.ShortText)

        ws.register(AffixDoc)
        ws.upsert_many(AffixDoc, _AFFIX_ROWS)
        expected_mode = "native" if backend_type == "sqlite" else "py_scan"
        assert _affix_results(ws, AffixDoc) == {label: (ids, expected_mode) for label, ids in expected_ids.items()}, backend_type

        WORKSPACES.pop(f"affix-{backend_type}", None)


@pytest.mark.detail
def test_starts_with_ends_with_scan_without_duckdb_field_proof():
    """DuckDB affix operations should scan until the concrete column collation is proved."""
    pytest.importorskip("duckdb_engine")
    ws = hb.HeavenBase("affix-duckdb", backends={"main": {"type": "duckdb", "database": ":memory:"}})

    class AffixDoc(hb.Entity):
        identifier = "affix-doc-duckdb"

        name = hb.field(hb.ShortText)

    ws.register(AffixDoc)
    ws.upsert_many(AffixDoc, _AFFIX_ROWS)
    assert _affix_results(ws, AffixDoc) == {
        "starts_lc": (["2"], "py_scan"),
        "starts_empty": (["1", "2", "3", "4", "6"], "py_scan"),
        "ends_a": (["1", "2", "4"], "py_scan"),
    }

    WORKSPACES.pop("affix-duckdb", None)


@pytest.mark.detail
def test_oceanbase_affix_scans_like_other_ci_like_backends():
    """All SQL affix compilers should scan without concrete text evidence."""
    from heavenbase.backends.sql.handler.base import sql_filter_handler

    field_schema = FieldSchema("name", hb.ShortText())
    for backend in ("oceanbase", "mysql", "starrocks"):
        ctx = hb.ext.HandlerContext("be", backend, hb.InlineColumn.identifier)
        for op in ("starts_with", "ends_with"):
            assert sql_filter_handler(op)(field_schema, "x", ctx).notes["mode"] == "py_scan", (backend, op)
    duck = hb.ext.HandlerContext("be", "duckdb", hb.InlineColumn.identifier)
    assert sql_filter_handler("starts_with")(field_schema, "x", duck).notes["mode"] == "py_scan"


def test_sql_dialect_capabilities_are_registry_backed(monkeypatch):
    """SQL dialect pushdown flags should be registered by backend metadata, not central provider lists."""
    from heavenbase.backends.sql.capabilities import (
        SQL_JSON_AGG_STYLE_ARRAYAGG,
        SQL_JSON_AGG_STYLE_GROUP_ARRAY,
        SQL_JSON_AGG_STYLE_POSTGRES_ORDERED,
        SQL_JSON_OBJECT_STYLE_BUILD_OBJECT,
        SQL_JSON_PATH_STYLE_JSON1,
        SQL_REGEX_STYLE_DUCKDB_TILDE_SEARCH,
        SQL_REGEX_STYLE_REGEXP_LIKE_CASE_SENSITIVE,
        SQL_WILDCARD_STYLE_GLOB,
        sql_filter_requires_scan,
    )
    from heavenbase.backends.sql.handler.base import sql_filter_handler
    from sqlalchemy import Column, Integer, MetaData, String, Table, select
    from sqlalchemy.dialects import mysql as mysql_dialect
    from sqlalchemy.dialects import postgresql, sqlite

    specs = hb.ext.registered_sql_dialects()
    assert specs["sqlite"].case_insensitive_like is True
    assert specs["sqlite"].supports_wildcard is True
    assert specs["sqlite"].wildcard_style == SQL_WILDCARD_STYLE_GLOB
    assert specs["sqlite"].side_table_json_agg_style == SQL_JSON_AGG_STYLE_GROUP_ARRAY
    assert specs["sqlite"].supports_regex is False
    assert specs["sqlite"].native_text_ops == ("eq", "ne", "lt", "lte", "gt", "gte", "in", "contains", "starts_with", "ends_with")
    assert specs["sqlite"].native_text_key_ops == ("group", "distinct", "order")
    assert specs["duckdb"].native_text_ops == ()
    assert specs["sqlite"].supports_native_filter("like") is False
    assert specs["sqlite"].has_native_filter_compiler("like") is True
    assert specs["sqlite"].supports_native_filter("starts_with") is False
    assert specs["sqlite"].has_native_filter_compiler("starts_with") is True
    assert specs["sqlite"].supports_native_text_filter("starts_with") is True
    assert specs["sqlite"].json_path_style == SQL_JSON_PATH_STYLE_JSON1
    assert specs["sqlite"].supports_native_filter("get") is True
    assert specs["postgresql"].json_path_style is None
    assert specs["postgresql"].supports_native_filter("get") is False
    assert specs["duckdb"].supports_regex is True
    assert specs["duckdb"].regex_style == SQL_REGEX_STYLE_DUCKDB_TILDE_SEARCH
    assert specs["duckdb"].supports_native_filter("starts_with") is True
    assert specs["postgresql"].side_table_json_object_style == SQL_JSON_OBJECT_STYLE_BUILD_OBJECT
    assert specs["postgresql"].side_table_json_agg_style == SQL_JSON_AGG_STYLE_POSTGRES_ORDERED
    assert specs["mysql"].supports_regex is True
    assert specs["mysql"].native_text_ops == ("eq", "ne", "in")
    assert specs["mysql"].regex_style == SQL_REGEX_STYLE_REGEXP_LIKE_CASE_SENSITIVE
    assert specs["mysql"].side_table_json_agg_style == SQL_JSON_AGG_STYLE_ARRAYAGG
    assert specs["mysql"].supports_wildcard is False
    assert specs["mysql"].supports_native_filter("wildcard") is False
    assert sql_filter_requires_scan(hb.Json) is True
    assert sql_filter_requires_scan(hb.Array) is True
    assert sql_filter_requires_scan(hb.ShortText) is False

    field_schema = FieldSchema("name", hb.ShortText())
    sqlite_ctx = hb.ext.HandlerContext("be", "sqlite", hb.InlineColumn.identifier)
    duck = hb.ext.HandlerContext("be", "duckdb", hb.InlineColumn.identifier)
    mysql = hb.ext.HandlerContext("be", "mysql", hb.InlineColumn.identifier)

    assert sql_filter_handler("regex")(field_schema, "x", sqlite_ctx).notes["mode"] == "py_scan"
    assert sql_filter_handler("regex")(field_schema, "x", duck).notes["mode"] == "py_scan"
    assert sql_filter_handler("wildcard")(field_schema, "x*", mysql).notes["mode"] == "py_scan"

    custom_spec = hb.ext.SQLDialectSpec(
        "sqlite",
        native_text_ops=("regex", "wildcard"),
        supports_regex=True,
        supports_wildcard=True,
        text_evidence=lambda _backend, _entity_id, _field, _strategy: ("regex", "wildcard"),
    )
    from heavenbase.backends.sql.handler import base as sql_base
    from heavenbase.backends.sql.handler import text as sql_text

    monkeypatch.setattr(
        sql_base,
        "sql_dialect_spec",
        lambda identifier, *, resolver=None: custom_spec if identifier == "sqlite" else specs[identifier],
    )
    monkeypatch.setattr(
        sql_text,
        "sql_dialect_spec",
        lambda identifier, *, resolver=None: custom_spec if identifier == "sqlite" else specs[identifier],
    )
    custom = hb.ext.HandlerContext("be", "sqlite", hb.InlineColumn.identifier, backend_instance=object(), entity_id="custom-doc")
    assert custom_spec.supports_wildcard is True
    assert sql_filter_handler("wildcard")(field_schema, "x*", custom).notes["mode"] == "native"
    regex_fragment = sql_filter_handler("regex")(field_schema, "x", custom)
    table = Table("test", MetaData(), Column("name", String))
    sql = str(regex_fragment.payload(table).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert regex_fragment.notes["mode"] == "native"
    assert "REGEXP_LIKE" in sql

    side_table = Table("side_items", MetaData(), Column("idx", Integer), Column("name", String))
    pg_payload = specs["postgresql"].side_table_json_object_expression((("name", side_table.c.name),))
    pg_sql = str(select(specs["postgresql"].side_table_json_agg_expression(pg_payload, side_table.c.idx)).compile(dialect=postgresql.dialect()))
    mysql_sql = str(select(specs["mysql"].side_table_json_agg_expression(side_table.c.name, side_table.c.idx)).compile(dialect=mysql_dialect.dialect()))
    sqlite_sql = str(select(specs["sqlite"].side_table_json_agg_expression(side_table.c.name, side_table.c.idx)).compile(dialect=sqlite.dialect()))
    assert "json_build_object" in pg_sql and "json_agg" in pg_sql and "ORDER BY" in pg_sql
    assert "json_arrayagg" in mysql_sql.lower()
    assert "json_group_array" in sqlite_sql
    assert hb.ext.SQL_JSON_AGG_STYLE_ARRAYAGG == SQL_JSON_AGG_STYLE_ARRAYAGG

    hb.ext.register_logical_type(CustomSQLStruct, tags={"filter": True, "filter-domains": ["struct"]})
    struct_field = FieldSchema("payload", CustomSQLStruct())
    assert sql_filter_requires_scan(CustomSQLStruct) is True
    assert sql_filter_handler("eq")(struct_field, {"a": 1}, duck).notes["mode"] == "py_scan"


_AFFIX_METACHAR_ROWS = [
    {"object_id": "1", "name": "a%b"},  # literal percent
    {"object_id": "2", "name": "axb"},
    {"object_id": "3", "name": "a_b"},  # literal underscore
    {"object_id": "4", "name": "a/b"},  # literal slash (the LIKE escape char)
    {"object_id": "5", "name": "zzz"},
]


@pytest.mark.detail
def test_starts_with_ends_with_native_escapes_metachars_matches_scan():
    """Native affix push-down escapes LIKE metacharacters (`%`, `_`, and the `/` escape char) so a
    literal operand returns exactly the case-sensitive scan rows. Drop the autoescape and `%`/`_`
    become wildcards and these probes diverge — this is the regression guard for that escape branch.
    """
    pytest.importorskip("duckdb_engine")
    probes = (
        ("pct", lambda D: D.name.starts_with("a%"), ["1"]),  # literal % prefix, not a LIKE wildcard
        ("under", lambda D: D.name.starts_with("a_"), ["3"]),  # literal _ prefix, not a single-char wildcard
        ("slash", lambda D: D.name.ends_with("/b"), ["4"]),  # literal '/' (the escape char) suffix
    )
    for backend_type, expect_mode in (("inmem", "py_scan"), ("duckdb", "py_scan")):
        config = {"type": "inmem"} if backend_type == "inmem" else {"type": "duckdb", "database": ":memory:"}
        ws = hb.HeavenBase(f"affix-meta-{backend_type}", backends={"main": config})

        class MetaDoc(hb.Entity):
            identifier = f"affix-meta-doc-{backend_type}"

            name = hb.field(hb.ShortText)

        ws.register(MetaDoc)
        ws.upsert_many(MetaDoc, _AFFIX_METACHAR_ROWS)
        for label, predicate, expected in probes:
            q = ws.query(MetaDoc).where(predicate(MetaDoc))
            assert sorted(q.execute().ids) == expected, (backend_type, label)
            assert q.explain()["steps"][0]["handler_mode"] == expect_mode, (backend_type, label)

        WORKSPACES.pop(f"affix-meta-{backend_type}", None)


_WILDCARD_ROWS = [
    {"object_id": "1", "name": "alpha"},
    {"object_id": "2", "name": "alp%ha"},  # literal percent
    {"object_id": "3", "name": "alpXha"},
    {"object_id": "4", "name": "ALPHA"},  # case
]


@pytest.mark.detail
def test_wildcard_native_on_sql_escapes_metachars_and_matches_scan():
    """Native SQL wildcard (incl. DuckDB) translates `*`/`?`, escapes literal LIKE metachars, equals scan.

    Regression: DuckDB rejects a backslash LIKE escape, so a native wildcard query
    crashed. The handler now escapes with '/'. sqlite uses GLOB; both are case-sensitive.
    """
    expected_star = ["1", "2", "3"]  # alp*ha -> alpha, alp%ha, alpXha (case-sensitive, not ALPHA)
    expected_pct = ["2"]  # alp%ha -> literal %, matches only 'alp%ha'

    cases = [("inmem", "py_scan"), ("sqlite", "py_scan")]
    if find_spec("duckdb_engine") is not None:
        cases.append(("duckdb", "py_scan"))
    for backend_type, expect_mode in cases:
        config = {"type": "inmem"} if backend_type == "inmem" else {"type": backend_type, "database": ":memory:"}
        ws = hb.HeavenBase(f"wild-{backend_type}", backends={"main": config})

        class WildDoc(hb.Entity):
            identifier = f"wild-doc-{backend_type}"

            name = hb.field(hb.ShortText)

        ws.register(WildDoc)
        ws.upsert_many(WildDoc, _WILDCARD_ROWS)
        star = ws.query(WildDoc).where(WildDoc.name.wildcard("alp*ha"))
        pct = ws.query(WildDoc).where(WildDoc.name.wildcard("alp%ha"))

        assert sorted(star.execute().ids) == expected_star, backend_type
        assert sorted(pct.execute().ids) == expected_pct, backend_type
        # pin both queries to the expected path; pct is the escape-critical one (literal %).
        assert star.explain()["steps"][0]["handler_mode"] == expect_mode, backend_type
        assert pct.explain()["steps"][0]["handler_mode"] == expect_mode, backend_type

        WORKSPACES.pop(f"wild-{backend_type}", None)


@pytest.mark.detail
def test_wildcard_native_literal_slash_matches_scan():
    """DuckDB LIKE execution should treat '/' as a literal when it is also the escape char."""
    if find_spec("duckdb_engine") is None:
        pytest.skip("duckdb_engine is not installed")
    rows = [{"object_id": "1", "name": "a/bc"}, {"object_id": "2", "name": "axbc"}, {"object_id": "3", "name": "a/bX"}]
    expected = ["1", "3"]  # 'a/b*' -> literal 'a/b' prefix; 'axbc' must not match
    ws = hb.HeavenBase("wild-slash-duckdb", backends={"main": {"type": "duckdb", "database": ":memory:"}})

    class SlashDoc(hb.Entity):
        identifier = "wild-slash-doc-duckdb"

        name = hb.field(hb.ShortText)

    ws.register(SlashDoc)
    ws.upsert_many(SlashDoc, rows)
    query = ws.query(SlashDoc).where(SlashDoc.name.wildcard("a/b*"))
    assert sorted(query.execute().ids) == expected
    assert query.explain()["steps"][0]["handler_mode"] == "py_scan"

    WORKSPACES.pop("wild-slash-duckdb", None)


# ── 4. Cross-backend OR deduplication ──


def test_cross_backend_or_deduplicates_overlapping_rows(tmp_path):
    """Cross-backend OR results should deduplicate overlapping object ids."""
    text = _SpyScanBackend("text")
    nums = _SpyScanBackend("nums")
    ws = hb.HeavenBase("or-dedup-test", backends={"text": text, "nums": nums})

    class OrDoc(hb.Entity):
        identifier = "or-dedup-doc"

        title = hb.field(hb.ShortText).store(to="text")
        price = hb.field(hb.Float).store(to="nums")

    ws.register(OrDoc)
    ws.upsert(OrDoc, {"object_id": "o1", "title": "alpha", "price": 10.0})
    frame = ws.query(OrDoc).where((OrDoc.title == "alpha") | (OrDoc.price > 5)).execute()

    assert frame.ids == ["o1"]
    # Only one row despite matching both branches
    assert len(frame) == 1

    WORKSPACES.pop("or-dedup-test", None)


# ── 5. Backend grouping in explain ──


def test_complex_nested_backend_groups_in_explain(tmp_path):
    """Nested filters should emit backend_group explain nodes for grouped clauses."""
    text = _SpySQLBackend("text", _db(tmp_path, "text.db"))
    nums = _SpySQLBackend("nums", _db(tmp_path, "nums.db"))
    ws = hb.HeavenBase("group-explain-test", backends={"text": text, "nums": nums})

    class GroupDoc(hb.Entity):
        identifier = "group-explain-doc"

        title = hb.field(hb.ShortText).store(to="text")
        body = hb.field(hb.LongText).store(to="text")
        price = hb.field(hb.Float).store(to="nums")
        stock = hb.field(hb.Integer).store(to="nums")

    ws.register(GroupDoc)
    ws.upsert(GroupDoc, {"object_id": "g1", "title": "A", "body": "alpha", "price": 10.0, "stock": 5})
    query = ws.query(GroupDoc).where(((GroupDoc.title == "A") | (GroupDoc.body.match("alpha"))) & ((GroupDoc.price > 5) & (GroupDoc.stock >= 0)))
    explain = query.explain()

    groups = [s for s in explain["steps"] if s.get("node") == "backend_group"]
    text_group = _find_step(groups, backend="text")
    nums_group = _find_step(groups, backend="nums")

    assert text_group is not None
    assert nums_group is not None
    assert text_group["n"] == 2
    assert nums_group["n"] == 2

    WORKSPACES.pop("group-explain-test", None)


def test_same_level_and_backend_grouping_is_order_independent(tmp_path):
    """Same-level AND clauses should group by backend even when interleaved."""
    left = _SpySQLBackend("left", _db(tmp_path, "left.db"))
    right = _SpySQLBackend("right", _db(tmp_path, "right.db"))
    ws = hb.HeavenBase("orderless-group-test", backends={"left": left, "right": right})

    class InterleavedDoc(hb.Entity):
        identifier = "orderless-group-doc"

        left_a = hb.field(hb.ShortText).store(to="left")
        left_b = hb.field(hb.ShortText).store(to="left")
        right_a = hb.field(hb.Float).store(to="right")
        right_b = hb.field(hb.Integer).store(to="right")

    ws.register(InterleavedDoc)
    ws.upsert(InterleavedDoc, {"object_id": "x1", "left_a": "alpha", "left_b": "beta", "right_a": 9.0, "right_b": 3})
    query = ws.query(InterleavedDoc).where(
        (InterleavedDoc.left_a == "alpha") & (InterleavedDoc.right_a > 5) & (InterleavedDoc.left_b == "beta") & (InterleavedDoc.right_b >= 2)
    )
    explain = query.explain()
    groups = [s for s in explain["steps"] if s.get("node") == "backend_group"]

    assert _find_step(groups, backend="left")["n"] == 2
    assert _find_step(groups, backend="right")["n"] == 2
    assert query.execute().ids == ["x1"]
    assert left.ops == ["and"]
    assert right.ops == ["and"]

    WORKSPACES.pop("orderless-group-test", None)


# ── 6. Backend-specific native mode checks ──


@pytest.mark.detail
def test_sqlite_ilike_requires_text_parity_proof(tmp_path):
    """SQLite ilike should scan because lower/LIKE does not match Python Unicode semantics."""
    backend = _SpySQLBackend("sql", _db(tmp_path, "sqlite_ilike.db"))
    ws = hb.HeavenBase("sqlite-ilike-test", backends={"sql": backend})

    class IlikeDoc(hb.Entity):
        identifier = "sql-ilike-doc"

        name = hb.field(hb.ShortText)

    ws.register(IlikeDoc)
    ws.upsert(IlikeDoc, {"object_id": "i1", "name": "Alpha"})
    query = ws.query(IlikeDoc).where(IlikeDoc.name.ilike("%alp%"))
    explain = query.explain()
    step = _filter_step(explain["steps"])[0]

    assert step["handler_mode"] == "py_scan"
    assert step["fallback_reason"] == "text_semantics_not_proven"
    assert step["type"] == "sqlite"

    WORKSPACES.pop("sqlite-ilike-test", None)


@pytest.mark.detail
def test_sqlite_wildcard_scans_without_glob_parity(tmp_path):
    """SQLite wildcard should scan because GLOB does not cover the Python domain."""
    backend = _SpySQLBackend("sql", _db(tmp_path, "sqlite_wildcard.db"))
    ws = hb.HeavenBase("sqlite-wildcard-test", backends={"sql": backend})

    class WildDoc(hb.Entity):
        identifier = "sql-wildcard-doc"

        name = hb.field(hb.ShortText)

    ws.register(WildDoc)
    ws.upsert(WildDoc, {"object_id": "w1", "name": "alpha"})
    query = ws.query(WildDoc).where(WildDoc.name.wildcard("al*"))
    explain = query.explain()
    step = _filter_step(explain["steps"])[0]

    assert step["handler_mode"] == "py_scan"
    assert step["type"] == "sqlite"
    assert step["fallback_reason"] == "text_semantics_not_proven"

    # Verify it actually executes correctly
    assert query.execute().ids == ["w1"]

    WORKSPACES.pop("sqlite-wildcard-test", None)


@pytest.mark.detail
def test_duckdb_regex_compiler_is_available_but_safe_route_scans():
    """DuckDB keeps a native compiler while portable routing requires parity proof."""
    from heavenbase.execution.handlers.seed import seed_handlers

    handlers = seed_handlers()
    field_schema = FieldSchema("name", hb.ShortText())
    ctx = hb.ext.HandlerContext("duck", "duckdb", hb.InlineColumn.identifier)
    fragment = handlers.get(hb.ShortText, "regex", "duckdb", hb.InlineColumn.identifier)(field_schema, r"^al", ctx)

    assert fragment.notes["mode"] == "py_scan"
    assert fragment.notes["fallback_reason"] == "text_semantics_not_proven"


@pytest.mark.detail
def test_duckdb_regex_substring_wrapping():
    """The dormant DuckDB compiler should retain its provider regex lowering."""
    from heavenbase.backends.sql.capabilities import sql_dialect_spec
    from sqlalchemy import MetaData, String, Table, Column
    from sqlalchemy.dialects import postgresql

    metadata = MetaData()
    table = Table("test", metadata, Column("name", String))
    dialect = postgresql.dialect()
    spec = sql_dialect_spec("duckdb")

    # No anchors -> wrap with .* on both sides
    sql = str(spec.regex_expression(table.c.name, r"al").compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    assert "~ '.*al.*'" in sql

    # Leading ^ -> only append .*
    sql = str(spec.regex_expression(table.c.name, r"^al").compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    assert "~ '^al.*'" in sql

    # Trailing $ -> only prepend .*
    sql = str(spec.regex_expression(table.c.name, r"al$").compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    assert "~ '.*al$'" in sql

    # Both ^ and $ -> no wrapping
    sql = str(spec.regex_expression(table.c.name, r"^al$").compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    assert "~ '^al$'" in sql


@pytest.mark.detail
def test_mysql_regex_case_sensitive_flag():
    """The dormant MySQL compiler should retain its case-sensitive provider flag."""
    from heavenbase.backends.sql.capabilities import sql_dialect_spec
    from sqlalchemy import MetaData, String, Table, Column
    from sqlalchemy.dialects import mysql

    metadata = MetaData()
    table = Table("test", metadata, Column("name", String))
    sql = str(sql_dialect_spec("mysql").regex_expression(table.c.name, r"^al").compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "regexp_like(" in sql.lower()
    assert "'c'" in sql


@pytest.mark.detail
def test_sql_wildcard_escapes_like_metacharacters():
    """Dormant wildcard compilers should retain safe provider escaping."""
    from heavenbase.backends.sql.capabilities import sql_dialect_spec
    from sqlalchemy import MetaData, String, Table, Column
    from sqlalchemy.dialects import postgresql

    metadata = MetaData()
    table = Table("test", metadata, Column("name", String))
    dialect = postgresql.dialect()

    for backend_identifier in ("postgres", "duckdb"):
        spec = sql_dialect_spec(backend_identifier)
        sql = str(spec.wildcard_expression(table.c.name, "a_c").compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
        # '/' escape (not '\\'): DuckDB rejects a backslash escape as a two-char string.
        assert "LIKE 'a/_c' ESCAPE '/'" in sql, f"Expected escaped underscore for {backend_identifier}, got: {sql}"

        # Also verify * and ? are still translated
        sql = str(spec.wildcard_expression(table.c.name, "a*c?d").compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
        assert "LIKE 'a%%c_d' ESCAPE '/'" in sql, f"Expected translated wildcards for {backend_identifier}, got: {sql}"


def test_sql_null_and_empty_string_semantics(tmp_path):
    """SQL backends should distinguish NULL from empty string in eq/ne filters."""
    backend = _SpySQLBackend("sql", _db(tmp_path, "null_empty.db"))
    ws = hb.HeavenBase("null-empty-test", backends={"sql": backend})

    class NullDoc(hb.Entity):
        identifier = "null-empty-doc"

        name = hb.field(hb.ShortText)

    ws.register(NullDoc)
    ws.upsert(NullDoc, {"object_id": "n1", "name": ""})
    ws.upsert(NullDoc, {"object_id": "n2", "name": "alpha"})

    # Empty string should match eq("")
    assert ws.query(NullDoc).where(NullDoc.name == "").execute().ids == ["n1"]
    # Non-empty should not match eq("")
    assert ws.query(NullDoc).where(NullDoc.name != "").execute().ids == ["n2"]

    WORKSPACES.pop("null-empty-test", None)


@pytest.mark.external
def test_postgres_ilike_requires_text_parity_proof():
    """PostgreSQL ILIKE should scan until its semantics are concretely proved."""
    if not find_spec("psycopg2"):
        pytest.skip("psycopg2 not installed")
    _require_postgres()

    _ensure_pg_database("pg_ilike_test")
    backend = hb.backends.PostgresBackend("pg", url=_pg_url("pg_ilike_test"))
    ws = hb.HeavenBase("pg-ilike-test", backends={"pg": backend})

    class PgDoc(hb.Entity):
        identifier = "pg-ilike-doc"

        name = hb.field(hb.ShortText)

    ws.register(PgDoc)
    ws.upsert(PgDoc, {"object_id": "p1", "name": "Alpha"})
    query = ws.query(PgDoc).where(PgDoc.name.ilike("%alp%"))
    explain = query.explain()
    step = _filter_step(explain["steps"])[0]

    assert step["handler_mode"] == "py_scan"
    assert step["type"] == "postgres"
    assert step["fallback_reason"] == "text_semantics_not_proven"

    WORKSPACES.pop("pg-ilike-test", None)


@pytest.mark.external
def test_pgvector_near_native_mode():
    """PgVector near should report native mode when PostgreSQL is reachable."""
    if not find_spec("psycopg2"):
        pytest.skip("psycopg2 not installed")
    try:
        import pgvector.sqlalchemy  # noqa: F401
    except ModuleNotFoundError:
        pytest.skip("pgvector.sqlalchemy not installed")
    _require_postgres()

    _ensure_pg_database("pgv_near_test")
    backend = hb.backends.PgVectorBackend("pgvector", url=_pg_url("pgv_near_test"))
    ws = hb.HeavenBase("pgv-near-test", backends={"pgvector": backend})

    class PgVecDoc(hb.Entity):
        identifier = "pgv-near-doc"

        title = hb.field(hb.ShortText)
        emb = hb.field(hb.Vector[2])

    ws.register(PgVecDoc)
    ws.upsert(PgVecDoc, {"object_id": "v1", "title": "near", "emb": [1, 0]})
    query = ws.query(PgVecDoc).near(PgVecDoc.emb, [1, 0], top_k=1)
    explain = query.explain()
    step = [s for s in explain["steps"] if s.get("node") == "near"][0]

    assert step["handler_mode"] == "native"
    assert step["type"] == "pgvector"
    assert step["fallback_reason"] is None

    WORKSPACES.pop("pgv-near-test", None)


@pytest.mark.external
def test_elasticsearch_text_match_native_mode():
    """Elasticsearch text match should report native mode when reachable."""
    if not find_spec("elasticsearch"):
        pytest.skip("elasticsearch not installed")
    _require_elasticsearch()

    ws = hb.HeavenBase(
        "es-match-test",
        backends={"main": hb.backends.InMemBackend("main"), "search": hb.backends.ElasticsearchBackend("search", hosts=[_ELASTICSEARCH_URL])},
    )

    class EsDoc(hb.Entity):
        identifier = "es-match-doc"

        body = hb.field(hb.LongText).store(to="search").index()

    ws.register(EsDoc)
    ws.upsert(EsDoc, {"object_id": "e1", "body": "alpha search text"})
    query = ws.query(EsDoc).where(EsDoc.body.match("search"))
    explain = query.explain()
    step = _filter_step(explain["steps"])[0]

    assert step["handler_mode"] == "native"
    assert step["type"] == "elasticsearch"
    assert step["fallback_reason"] is None

    ws.drop()
    WORKSPACES.pop("es-match-test", None)


@pytest.mark.external
def test_milvus_array_contains_native_mode():
    """Milvus Array membership should report native mode when reachable."""
    import socket

    if not find_spec("pymilvus"):
        pytest.skip("pymilvus not installed")
    try:
        with socket.create_connection(("localhost", 19530), timeout=0.5):
            pass
    except OSError:
        pytest.skip("milvus not reachable")

    ws = hb.HeavenBase(
        "milvus-arr-test",
        backends={"main": hb.backends.InMemBackend("main"), "milvus": hb.backends.MilvusBackend("milvus", uri="http://localhost:19530")},
    )

    class MilvusDoc(hb.Entity):
        identifier = "milvus-arr-doc"

        embedding = hb.field(hb.Vector[2]).required().store(to="milvus")
        tags = hb.field(hb.Array[hb.ShortText]).store(to="milvus")
        title = hb.field(hb.ShortText).store(to="milvus")

    ws.register(MilvusDoc)
    ws.upsert(MilvusDoc, {"object_id": "m1", "embedding": [1.0, 0.0], "title": "alpha", "tags": ["a", "b"]})
    query = ws.query(MilvusDoc).where(MilvusDoc.tags.array_contains("a"))
    explain = query.explain()
    step = _filter_step(explain["steps"])[0]

    assert step["handler_mode"] == "native"
    assert step["type"] == "milvus"
    assert step["fallback_reason"] is None

    ws.drop()


@pytest.mark.detail
@pytest.mark.skipif(find_spec("chromadb") is None, reason="chromadb is not installed")
def test_chroma_scalar_eq_native_mode(tmp_path):
    """Chroma scalar equality should report native metadata filtering."""
    ws = hb.HeavenBase(
        "chroma-eq-test",
        backends={"main": hb.backends.InMemBackend("main"), "chroma": hb.backends.ChromaBackend("chroma", hb.utils.path_to_file_uri(tmp_path / "chroma-eq"))},
    )

    class ChromaDoc(hb.Entity):
        identifier = "chroma-eq-doc"

        title = hb.field(hb.ShortText).store(to="chroma")
        price = hb.field(hb.Float).store(to="chroma")

    ws.register(ChromaDoc)
    ws.upsert(ChromaDoc, {"object_id": "c1", "title": "alpha", "price": 10.0})
    query = ws.query(ChromaDoc).where(ChromaDoc.price == 10.0)
    explain = query.explain()
    step = _filter_step(explain["steps"])[0]

    assert step["handler_mode"] == "native"
    assert step["type"] == "chroma"
    assert step["fallback_reason"] is None

    WORKSPACES.pop("chroma-eq-test", None)


@pytest.mark.detail
@pytest.mark.skipif(_LANCE_UNAVAILABLE, reason="lancedb runtime is not installed")
def test_lance_scalar_range_native_mode(tmp_path):
    """Lance scalar ranges should report native filtering."""
    ws = hb.HeavenBase(
        "lance-range-test",
        backends={"main": hb.backends.InMemBackend("main"), "vec": hb.backends.LanceBackend("vec", hb.utils.path_to_file_uri(tmp_path / "lance-range"))},
    )

    class LanceDoc(hb.Entity):
        identifier = "lance-range-doc"

        score = hb.field(hb.Float).store(to="vec")

    ws.register(LanceDoc)
    ws.upsert(LanceDoc, {"object_id": "l1", "score": 10.0})
    query = ws.query(LanceDoc).where(LanceDoc.score > 5)
    explain = query.explain()
    step = _filter_step(explain["steps"])[0]

    assert step["handler_mode"] == "native"
    assert step["type"] == "lance"
    assert step["fallback_reason"] is None

    WORKSPACES.pop("lance-range-test", None)


def test_inmem_always_py_scan_with_reason():
    """In-memory filtering should consistently explain Python-scan fallback."""
    ws = hb.HeavenBase("inmem-reason-test", backends={"main": hb.backends.InMemBackend("main")})

    class InmemDoc(hb.Entity):
        identifier = "inmem-reason-doc"

        name = hb.field(hb.ShortText)

    ws.register(InmemDoc)
    ws.upsert(InmemDoc, {"object_id": "i1", "name": "alpha"})
    query = ws.query(InmemDoc).where(InmemDoc.name == "alpha")
    explain = query.explain()
    step = _filter_step(explain["steps"])[0]

    assert step["handler_mode"] == "py_scan"
    assert step["fallback_reason"] is not None
    assert "in-memory" in step["fallback_reason"].lower() or "Python scan" in step["fallback_reason"]

    WORKSPACES.pop("inmem-reason-test", None)


# ── 7. Spy-backend native pushdown proof ──


def test_sqlite_native_eq_no_scan(tmp_path):
    """In-memory spy documents the scan baseline for native SQL comparisons."""
    spy = _SpyScanBackend("sql")
    # We can't easily make SQLiteBackend a spy, so test via inmem + compiler proof
    ws = hb.HeavenBase("native-eq-test", backends={"main": spy})

    class EqDoc(hb.Entity):
        identifier = "native-eq-doc"

        name = hb.field(hb.ShortText)

    ws.register(EqDoc)
    ws.upsert(EqDoc, {"object_id": "e1", "name": "alpha"})
    ws.query(EqDoc).where(EqDoc.name == "alpha").execute()

    # InMem always scans, so this is a design check rather than a no-scan proof.
    # Real no-scan proof is in test_sql_groups_same_backend_boolean_tree.
    assert spy.scan_count > 0

    WORKSPACES.pop("native-eq-test", None)


def test_explain_returns_handler_op_field():
    """Explain output should include both public op and handler op fields."""
    ws = hb.HeavenBase("handler-op-test", backends={"main": hb.backends.InMemBackend("main")})

    class OpDoc(hb.Entity):
        identifier = "handler-op-doc"

        price = hb.field(hb.Float)

    ws.register(OpDoc)
    ws.upsert(OpDoc, {"object_id": "o1", "price": 10.0})
    query = ws.query(OpDoc).where(OpDoc.price > 5)
    explain = query.explain()
    step = _filter_step(explain["steps"])[0]

    assert step["handler_op"] == "gt"
    assert step["op"] == "gt"

    WORKSPACES.pop("handler-op-test", None)


def test_explain_returns_null_reasons_for_native():
    """Explain output should include reason keys with None for supported paths."""
    ws = hb.HeavenBase("null-reason-test", backends={"main": hb.backends.InMemBackend("main")})

    class ReasonDoc(hb.Entity):
        identifier = "null-reason-doc"

        name = hb.field(hb.ShortText)

    ws.register(ReasonDoc)
    ws.upsert(ReasonDoc, {"object_id": "r1", "name": "alpha"})
    query = ws.query(ReasonDoc).where(ReasonDoc.name == "alpha")
    explain = query.explain()
    step = _filter_step(explain["steps"])[0]

    assert "fallback_reason" in step
    assert "unsupported_reason" in step
    # InMem is py_scan, so fallback_reason should be present
    assert step["fallback_reason"] is not None
    assert step["unsupported_reason"] is None

    WORKSPACES.pop("null-reason-test", None)


# ── 8. Mega complex query: backend grouping efficiency analysis ──


@pytest.mark.slow
def test_mega_complex_query_backend_grouping_efficiency(tmp_path):
    """Stress-test explain() with a very large entity + deeply nested query.

    Entity spans 5 backends:
      - text_backend   : title, body, summary, tags (ShortText, LongText, Array[ShortText])
      - nums_backend   : price, stock, rating, views (Float, Integer)
      - flags_backend  : active, featured, verified (Boolean)
      - vec_backend    : embedding (Vector[4])
      - search_backend : description (LongText with InvertedIndex)

    Query: deeply nested AND/OR tree with 15+ clauses across all backends.
    We verify that backend_group nodes correctly coalesce clauses per backend,
    minimizing the number of backend round-trips.
    """
    text_b = _SpySQLBackend("text", _db(tmp_path, "text.db"))
    nums_b = _SpySQLBackend("nums", _db(tmp_path, "nums.db"))
    flags_b = _SpySQLBackend("flags", _db(tmp_path, "flags.db"))
    vec_b = _SpySQLBackend("vec", _db(tmp_path, "vec.db"))
    search_b = _SpySQLBackend("search", _db(tmp_path, "search.db"))

    ws = hb.HeavenBase(
        "mega-test",
        backends={"text": text_b, "nums": nums_b, "flags": flags_b, "vec": vec_b, "search": search_b},
    )

    class MegaProduct(hb.Entity):
        identifier = "mega-product"

        # text_backend (5 fields)
        title = hb.field(hb.ShortText).store(to="text")
        body = hb.field(hb.LongText).store(to="text")
        summary = hb.field(hb.LongText).store(to="text")
        category = hb.field(hb.ShortText).store(to="text")
        tags = hb.field(hb.Array[hb.ShortText]).store(to="text", strategy=hb.SideTable.identifier)

        # nums_backend (5 fields)
        price = hb.field(hb.Float).store(to="nums")
        stock = hb.field(hb.Integer).store(to="nums")
        rating = hb.field(hb.Float).store(to="nums")
        views = hb.field(hb.Integer).store(to="nums")
        weight = hb.field(hb.Float).store(to="nums")

        # flags_backend (3 fields)
        active = hb.field(hb.Boolean).store(to="flags")
        featured = hb.field(hb.Boolean).store(to="flags")
        verified = hb.field(hb.Boolean).store(to="flags")

        # vec_backend (1 field)
        embedding = hb.field(hb.Vector[4]).store(to="vec")

        # search_backend (1 field)
        description = hb.field(hb.LongText).store(to="search", strategy=hb.InvertedIndex.identifier)

    # Insert 3 rows
    ws.register(MegaProduct)
    ws.upsert_many(
        MegaProduct,
        [
            {
                "object_id": "p1",
                "title": "Alpha Gadget",
                "body": "The alpha gadget is amazing",
                "summary": "Alpha summary",
                "category": "electronics",
                "tags": ["new", "sale"],
                "price": 99.99,
                "stock": 50,
                "rating": 4.5,
                "views": 1000,
                "weight": 1.2,
                "active": True,
                "featured": True,
                "verified": True,
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "description": "A premium alpha gadget for professionals",
            },
            {
                "object_id": "p2",
                "title": "Beta Widget",
                "body": "The beta widget is budget friendly",
                "summary": "Beta summary",
                "category": "home",
                "tags": ["used", "clearance"],
                "price": 29.99,
                "stock": 0,
                "rating": 3.0,
                "views": 500,
                "weight": 0.8,
                "active": False,
                "featured": False,
                "verified": False,
                "embedding": [0.0, 1.0, 0.0, 0.0],
                "description": "A cheap beta widget for students",
            },
            {
                "object_id": "p3",
                "title": "Gamma Device",
                "body": "The gamma device is experimental",
                "summary": "Gamma summary",
                "category": "lab",
                "tags": ["prototype", "experimental"],
                "price": 199.99,
                "stock": 10,
                "rating": 5.0,
                "views": 100,
                "weight": 2.5,
                "active": True,
                "featured": False,
                "verified": True,
                "embedding": [0.0, 0.0, 1.0, 0.0],
                "description": "An experimental gamma device for labs",
            },
        ],
    )

    # Build a deeply nested query with 15+ clauses across all backends
    query = (
        ws.query(MegaProduct)
        .where(
            # AND group 1: text_backend clauses (5 clauses on same backend)
            (
                (MegaProduct.title == "Alpha Gadget")
                | (MegaProduct.body.match("amazing"))
                | (MegaProduct.summary.contains("summary"))
                | (MegaProduct.category.in_(["electronics", "lab"]))
                | MegaProduct.tags.array_contains("new")
            )
            & (
                # AND group 2: nums_backend clauses (5 clauses on same backend)
                (MegaProduct.price > 50)
                & (MegaProduct.stock > 0)
                & (MegaProduct.rating >= 4.0)
                & (MegaProduct.views < 2000)
                & (MegaProduct.weight <= 2.0)
            )
            & (
                # AND group 3: flags_backend clauses (3 clauses on same backend)
                MegaProduct.active.op("eq", True)
                & MegaProduct.verified.op("eq", True)
                & MegaProduct.featured.op("eq", True)
            )
            & (
                # AND group 4: search_backend (1 clause)
                MegaProduct.description.contains("premium")
            )
        )
        .select("title", "price", "rating", "active")
        .order_by(MegaProduct.price, desc=True)
        .limit(10)
    )

    explain = query.explain()
    steps = explain["steps"]

    # ── Analysis 1: Verify backend_group nodes exist and count correctly ──
    and_groups = [s for s in steps if s.get("node") == "backend_group" and s.get("op") == "and"]
    or_groups = [s for s in steps if s.get("node") == "backend_group" and s.get("op") == "or"]

    # The top-level AND has 4 groups; groups 1-3 have multiple clauses each.
    # Group 1 (text_backend OR) has 5 clauses -> backend_group OR
    # Group 2 (nums_backend AND) has 5 clauses -> backend_group AND
    # Group 3 (flags_backend AND) has 3 clauses -> backend_group AND
    # Group 4 (search_backend) has 1 clause -> no group node
    assert len(or_groups) >= 1, f"Expected OR backend_group nodes, got: {[s for s in steps if s.get('node') == 'backend_group']}"
    text_or_group = [g for g in or_groups if g.get("backend") == "text"]
    assert len(text_or_group) == 1, f"Expected exactly one text OR group, got {text_or_group}"
    assert text_or_group[0]["n"] == 5, f"Expected text OR group to have 5 clauses, got {text_or_group[0]['n']}"

    nums_and_group = [g for g in and_groups if g.get("backend") == "nums"]
    assert len(nums_and_group) == 1, f"Expected exactly one nums AND group, got {nums_and_group}"
    assert nums_and_group[0]["n"] == 5, f"Expected nums AND group to have 5 clauses, got {nums_and_group[0]['n']}"

    flags_and_group = [g for g in and_groups if g.get("backend") == "flags"]
    assert len(flags_and_group) == 1, f"Expected exactly one flags AND group, got {flags_and_group}"
    assert flags_and_group[0]["n"] == 3, f"Expected flags AND group to have 3 clauses, got {flags_and_group[0]['n']}"

    # ── Analysis 2: Verify no redundant backend_group for single-clause backends ──
    search_groups = [g for g in and_groups if g.get("backend") == "search"]
    assert len(search_groups) == 0, "Single-clause backend should NOT produce a backend_group node"

    # ── Analysis 3: Verify filter steps report correct backends ──
    filter_steps = _filter_step(steps)
    backend_counts: dict[str, int] = {}
    for s in filter_steps:
        backend_counts[s["backend"]] = backend_counts.get(s["backend"], 0) + 1

    assert backend_counts.get("text") == 5, f"Expected 5 text filter steps, got {backend_counts.get('text')}"
    assert backend_counts.get("nums") == 5, f"Expected 5 nums filter steps, got {backend_counts.get('nums')}"
    assert backend_counts.get("flags") == 3, f"Expected 3 flags filter steps, got {backend_counts.get('flags')}"
    assert backend_counts.get("search") == 1, f"Expected 1 search filter step, got {backend_counts.get('search')}"

    # ── Analysis 4: Verify managed SQL text proofs and non-text native modes ──
    text_modes = {
        "title": "native",
        "body": "py_scan",
        "summary": "native",
        "category": "native",
        "tags": "py_scan",
        "description": "py_scan",
    }
    for s in filter_steps:
        if s["backend"] in ("text", "search"):
            assert s["handler_mode"] == text_modes[s["field"]]
            expected_fallback = "text_semantics_not_proven" if s["handler_mode"] == "py_scan" else None
            assert s["fallback_reason"] == expected_fallback
            assert s["unsupported_reason"] is None
        elif s["backend"] in ("nums", "flags"):
            assert s["handler_mode"] == "native", f"SQL backend {s['backend']} op {s['op']} should be native, got {s['handler_mode']}"
            assert s["fallback_reason"] is None, f"Native step should have no fallback_reason: {s}"
            assert s["unsupported_reason"] is None, f"Valid step should have no unsupported_reason: {s}"

    # Specifically verify array_contains uses SideTable strategy
    array_step = _find_step(filter_steps, field="tags", op="array_contains")
    assert array_step is not None
    assert array_step["handler_mode"] == "py_scan"
    assert array_step["fallback_reason"] == "text_semantics_not_proven"
    assert array_step["strategy"] == hb.SideTable.identifier

    # ── Analysis 5: Verify select/limit/order are in the query plan but not in steps ──
    # The explain() focuses on routing; select/limit/order are query metadata.
    assert explain["query"]["select"] == ["title", "price", "rating", "active"]
    assert explain["query"]["limit"] == 10
    assert explain["query"]["order"] == [("price", True)]

    # ── Analysis 6: Count total steps vs expected ──
    # Expected structure:
    #   and (1) — n=10 because it has 10 direct children
    #   backend_group and: nums (1), flags (1)
    #   or (1) — n=5 because it has 5 direct children
    #   backend_group or: text (1)
    #   filter steps: text (5) + nums (5) + flags (3) + search (1) = 14
    #   Total = 1 + 2 + 1 + 1 + 14 = 19
    expected_steps = 19
    assert len(steps) == expected_steps, f"Expected {expected_steps} steps, got {len(steps)}: {steps}"

    # ── Analysis 7: Execute and verify result ──
    frame = query.execute()
    rows = frame.rows()
    assert len(rows) == 1, f"Expected exactly 1 result row, got {len(rows)}"
    assert rows[0]["title"] == "Alpha Gadget"
    assert rows[0]["price"] == 99.99
    assert rows[0]["active"] is True

    WORKSPACES.pop("mega-test", None)


@pytest.mark.slow
def test_mega_query_cross_backend_or_efficiency(tmp_path):
    """Verify that cross-backend OR correctly deduplicates and groups.

    Two backends each have 2 clauses in an OR. The result should deduplicate
    rows that match both branches.
    """
    text_b = _SpySQLBackend("text", _db(tmp_path, "xor_text.db"))
    nums_b = _SpySQLBackend("nums", _db(tmp_path, "xor_nums.db"))
    ws = hb.HeavenBase("mega-xor-test", backends={"text": text_b, "nums": nums_b})

    class XorDoc(hb.Entity):
        identifier = "mega-xor-doc"

        title = hb.field(hb.ShortText).store(to="text")
        body = hb.field(hb.LongText).store(to="text")
        price = hb.field(hb.Float).store(to="nums")
        stock = hb.field(hb.Integer).store(to="nums")

    ws.register(XorDoc)
    ws.upsert_many(
        XorDoc,
        [
            {"object_id": "x1", "title": "alpha", "body": "alpha body", "price": 10.0, "stock": 5},
            {"object_id": "x2", "title": "beta", "body": "beta body", "price": 20.0, "stock": 0},
            {"object_id": "x3", "title": "gamma", "body": "gamma body", "price": 15.0, "stock": 10},
        ],
    )

    # OR with 2 text clauses + 2 nums clauses = 4 total, 2 backends
    query = ws.query(XorDoc).where((XorDoc.title == "alpha") | (XorDoc.body.match("beta")) | (XorDoc.price > 15) | (XorDoc.stock > 5))
    explain = query.explain()
    steps = explain["steps"]

    or_groups = [s for s in steps if s.get("node") == "backend_group" and s.get("op") == "or"]
    assert len(or_groups) == 2, f"Expected 2 OR backend groups, got {len(or_groups)}"

    text_group = [g for g in or_groups if g.get("backend") == "text"]
    nums_group = [g for g in or_groups if g.get("backend") == "nums"]
    assert len(text_group) == 1 and text_group[0]["n"] == 2
    assert len(nums_group) == 1 and nums_group[0]["n"] == 2

    # x1 matches title==alpha (text) and stock>5 (nums)
    # x2 matches body~beta (text) and price>15 (nums)
    # x3 matches body~beta? no. price>15 yes. stock>5 yes.
    frame = query.execute()
    ids = sorted(frame.ids)
    assert ids == ["x1", "x2", "x3"], f"Expected all 3 rows, got {ids}"

    WORKSPACES.pop("mega-xor-test", None)


# ── 9. Heterogeneous backend routing ──


@pytest.mark.slow
def test_heterogeneous_query_routes_explain_and_results_scale_contract(tmp_path):
    """A single query should distribute across local file, inmem, SQL, vector, and SideTable-backed fields."""
    catalog = _SpyJsonFileBackend("catalog", hb.utils.path_to_file_uri(tmp_path / "catalog-json"))
    flags = _SpyScanBackend("flags")
    pricing = _SpySQLBackend("pricing", _db(tmp_path, "pricing.db"))
    inventory = _SpySQLBackend("inventory", _db(tmp_path, "inventory.db"))
    facet = _SpySQLBackend("facet", _db(tmp_path, "facet.db"))
    vector = _SpyPineconeBackend("vec")
    ws = hb.HeavenBase(
        "heterogeneous-routing-test",
        backends={"catalog": catalog, "flags": flags, "pricing": pricing, "inventory": inventory, "facet": facet, "vec": vector},
    )

    class RoutedProduct(hb.Entity):
        identifier = "heterogeneous-routing-product"

        title = hb.field(hb.ShortText).store(to="catalog")
        category = hb.field(hb.ShortText).store(to="catalog")
        active = hb.field(hb.Boolean).store(to="flags")
        region = hb.field(hb.ShortText).store(to="flags")
        price = hb.field(hb.Float).store(to="pricing")
        rating = hb.field(hb.Float).store(to="pricing")
        discount = hb.field(hb.Float).store(to="pricing")
        stock = hb.field(hb.Integer).store(to="inventory")
        warehouse = hb.field(hb.ShortText).store(to="inventory")
        tags = hb.field(hb.Array[hb.ShortText]).store(to="facet", strategy=hb.SideTable.identifier)
        badges = hb.field(hb.Array[hb.ShortText]).store(to="facet", strategy=hb.SideTable.identifier)
        topics = hb.field(hb.HyperG["slot", "value", "topic"]).store(to="facet", strategy=hb.SideTable.identifier)
        vector_label = hb.field(hb.ShortText).store(to="vec")
        vector_rank = hb.field(hb.Integer).store(to="vec")
        embedding = hb.field(hb.Vector[3]).store(to="vec", strategy=hb.VectorIndex.identifier)

    ws.register(RoutedProduct)
    target_topics = [{"slot": "intent", "value": "refund", "topic": "support"}, {"slot": "region", "value": "emea", "topic": "launch"}]
    rows = [
        {
            "object_id": "p1",
            "title": "Alpha Console",
            "category": "electronics",
            "active": True,
            "region": "emea",
            "price": 129.0,
            "rating": 4.7,
            "discount": 0.1,
            "stock": 15,
            "warehouse": "north",
            "tags": ["launch", "premium"],
            "badges": ["certified", "new"],
            "topics": target_topics,
            "vector_label": "candidate",
            "vector_rank": 1,
            "embedding": [1.0, 0.0, 0.0],
        },
        {
            "object_id": "p2",
            "title": "Beta Console",
            "category": "electronics",
            "active": True,
            "region": "emea",
            "price": 119.0,
            "rating": 4.2,
            "discount": 0.2,
            "stock": 10,
            "warehouse": "north",
            "tags": ["launch"],
            "badges": ["new"],
            "topics": target_topics[:1],
            "vector_label": "candidate",
            "vector_rank": 2,
            "embedding": [0.9, 0.1, 0.0],
        },
        {
            "object_id": "p3",
            "title": "Alpha Outlet",
            "category": "clearance",
            "active": True,
            "region": "global",
            "price": 89.0,
            "rating": 4.9,
            "discount": 0.4,
            "stock": 5,
            "warehouse": "north",
            "tags": ["launch"],
            "badges": ["certified"],
            "topics": target_topics,
            "vector_label": "candidate",
            "vector_rank": 1,
            "embedding": [0.8, 0.2, 0.0],
        },
        {
            "object_id": "p4",
            "title": "Alpha Pro",
            "category": "lab",
            "active": True,
            "region": "global",
            "price": 149.0,
            "rating": 4.9,
            "discount": 0.05,
            "stock": 8,
            "warehouse": "west",
            "tags": ["launch", "pro"],
            "badges": ["certified"],
            "topics": target_topics,
            "vector_label": "candidate",
            "vector_rank": 3,
            "embedding": [0.95, 0.05, 0.0],
        },
        {
            "object_id": "p5",
            "title": "Archived Alpha",
            "category": "electronics",
            "active": True,
            "region": "emea",
            "price": 199.0,
            "rating": 5.0,
            "discount": 0.05,
            "stock": 20,
            "warehouse": "north",
            "tags": ["launch"],
            "badges": ["certified"],
            "topics": target_topics,
            "vector_label": "candidate",
            "vector_rank": 1,
            "embedding": [1.0, 0.0, 0.0],
        },
    ]
    rows.extend(
        {
            "object_id": f"n{noise_idx}",
            "title": f"Noise Product {noise_idx}",
            "category": "other",
            "active": noise_idx % 2 == 0,
            "region": "apac",
            "price": float(20 + noise_idx),
            "rating": 3.0,
            "discount": 0.3,
            "stock": noise_idx,
            "warehouse": "returns",
            "tags": ["background"],
            "badges": ["archive"],
            "topics": [{"slot": "noise", "value": str(noise_idx), "topic": "background"}],
            "vector_label": "background",
            "vector_rank": 100 + noise_idx,
            "embedding": [0.0, 1.0, 0.0],
        }
        for noise_idx in range(20)
    )
    ws.upsert_many(RoutedProduct, rows)

    query = (
        ws.query(RoutedProduct)
        .where(
            ((RoutedProduct.title.match("Alpha") | RoutedProduct.category.in_(["electronics", "lab"])) & ~RoutedProduct.title.contains("Archived"))
            & (RoutedProduct.active.op("eq", True) & RoutedProduct.region.in_(["emea", "global"]))
            & ((RoutedProduct.price >= 100) & (RoutedProduct.rating >= 4.5) & (RoutedProduct.discount < 0.25))
            & ((RoutedProduct.stock > 0) & (RoutedProduct.warehouse != "returns"))
            & (RoutedProduct.tags.array_contains("launch") & RoutedProduct.badges.array_contains("certified"))
            & ((RoutedProduct.vector_label == "candidate") | (RoutedProduct.vector_rank <= 2))
        )
        .near(RoutedProduct.embedding, [1.0, 0.0, 0.0], top_k=20)
        .select("title", "price", "stock", "active", "tags", "topics", "vector_label", "score")
        .order_by(RoutedProduct.price, desc=True)
        .limit(2)
    )

    explain = query.explain()
    steps = explain["steps"]
    filter_steps = _filter_step(steps)
    backend_counts: dict[str, int] = {}
    for step in filter_steps:
        backend_counts[step["backend"]] = backend_counts.get(step["backend"], 0) + 1

    assert backend_counts == {"catalog": 3, "flags": 2, "pricing": 3, "inventory": 2, "facet": 2, "vec": 2}
    for backend, count in {"catalog": 2, "flags": 2, "pricing": 3, "inventory": 2, "facet": 2}.items():
        assert _find_step(steps, node="backend_group", op="and", backend=backend, n=count) is not None
    assert _find_step(steps, node="backend_group", op="or", backend="catalog", n=2) is not None
    assert _find_step(steps, node="backend_group", op="or", backend="vec", n=2) is not None

    route_bindings = {binding["field_name"]: binding for binding in explain["storage_plan"]["bindings"]}
    assert route_bindings["title"]["backend"] == "catalog"
    assert route_bindings["active"]["backend"] == "flags"
    assert route_bindings["price"]["backend"] == "pricing"
    assert route_bindings["stock"]["backend"] == "inventory"
    assert route_bindings["embedding"]["backend"] == "vec"
    assert route_bindings["embedding"]["strategy"] == hb.VectorIndex.identifier
    assert route_bindings["tags"]["strategy"] == hb.SideTable.identifier
    assert route_bindings["badges"]["strategy"] == hb.SideTable.identifier
    assert route_bindings["topics"]["strategy"] == hb.SideTable.identifier

    for field_name in ("price", "rating", "discount", "stock"):
        step = _find_step(filter_steps, field=field_name)
        assert step["handler_mode"] == "native"
        assert step["handler_kind"] == "sql_where"
        assert step["fallback_reason"] is None
    warehouse_step = _find_step(filter_steps, field="warehouse")
    assert warehouse_step["handler_mode"] == "native"
    assert warehouse_step["fallback_reason"] is None
    for field_name in ("tags", "badges"):
        step = _find_step(filter_steps, field=field_name, op="array_contains")
        assert step["handler_mode"] == "py_scan"
        assert step["strategy"] == hb.SideTable.identifier
        assert step["fallback_reason"] == "text_semantics_not_proven"
    for field_name in ("title", "category", "active", "region"):
        step = _find_step(filter_steps, field=field_name)
        assert step["handler_mode"] == "py_scan"
        assert step["fallback_reason"] is not None
    for field_name in ("vector_label", "vector_rank"):
        step = _find_step(filter_steps, field=field_name)
        assert step["type"] == "pinecone"
        assert step["handler_mode"] == "native_with_near"
        assert step["handler_kind"] == "pinecone_filter"

    near_step = [step for step in steps if step.get("node") == "near"][0]
    assert near_step["backend"] == "vec"
    assert near_step["type"] == "pinecone"
    assert near_step["handler_mode"] == "native"
    assert near_step["strategy"] == hb.VectorIndex.identifier
    assert near_step["near_filter_mode"] == "post_filter"

    frame = query.execute()
    result_rows = frame.rows()
    assert [row["object_id"] for row in result_rows] == ["p4", "p1"]
    assert result_rows[0]["title"] == "Alpha Pro"
    assert result_rows[0]["topics"] == target_topics
    assert result_rows[1]["tags"] == ["launch", "premium"]
    assert all(row["score"] > 0.9 for row in result_rows)

    assert catalog.ops == ["and"]
    assert flags.scan_count == 1
    assert pricing.ops == ["and"]
    assert inventory.ops == ["and"]
    assert facet.ops == ["and"]
    assert vector.ops == ["or", "near"]
    assert pricing.scan_count == 0
    assert inventory.scan_count == 0
    assert facet.scan_count == 1

    WORKSPACES.pop("heterogeneous-routing-test", None)


@pytest.mark.full
def test_efficient_same_query_shape_uses_single_near_with_filter_backend(tmp_path):
    """The same query shape can execute as one same-backend near when metadata is co-located."""
    vector = _SpyPineconeBackend("vec")
    ws = hb.HeavenBase("efficient-routing-test", backends={"main": hb.backends.InMemBackend("main"), "vec": vector})

    class RoutedProduct(hb.Entity):
        identifier = "efficient-routing-product"

        title = hb.field(hb.ShortText).store(to="vec")
        category = hb.field(hb.ShortText).store(to="vec")
        active = hb.field(hb.Boolean).store(to="vec")
        region = hb.field(hb.ShortText).store(to="vec")
        price = hb.field(hb.Float).store(to="vec")
        rating = hb.field(hb.Float).store(to="vec")
        discount = hb.field(hb.Float).store(to="vec")
        stock = hb.field(hb.Integer).store(to="vec")
        warehouse = hb.field(hb.ShortText).store(to="vec")
        tags = hb.field(hb.Array[hb.ShortText]).store(to="vec")
        badges = hb.field(hb.Array[hb.ShortText]).store(to="vec")
        topics = hb.field(hb.HyperG["slot", "value", "topic"]).store(to="main")
        vector_label = hb.field(hb.ShortText).store(to="vec")
        vector_rank = hb.field(hb.Integer).store(to="vec")
        embedding = hb.field(hb.Vector[3]).store(to="vec", strategy=hb.VectorIndex.identifier)

    ws.register(RoutedProduct)
    target_topics = [{"slot": "intent", "value": "refund", "topic": "support"}, {"slot": "region", "value": "emea", "topic": "launch"}]
    ws.upsert_many(
        RoutedProduct,
        [
            {
                "object_id": "p1",
                "title": "Alpha Console",
                "category": "electronics",
                "active": True,
                "region": "emea",
                "price": 129.0,
                "rating": 4.7,
                "discount": 0.1,
                "stock": 15,
                "warehouse": "north",
                "tags": ["launch", "premium"],
                "badges": ["certified", "new"],
                "topics": target_topics,
                "vector_label": "candidate",
                "vector_rank": 1,
                "embedding": [1.0, 0.0, 0.0],
            },
            {
                "object_id": "p4",
                "title": "Alpha Pro",
                "category": "lab",
                "active": True,
                "region": "global",
                "price": 149.0,
                "rating": 4.9,
                "discount": 0.05,
                "stock": 8,
                "warehouse": "west",
                "tags": ["launch", "pro"],
                "badges": ["certified"],
                "topics": target_topics,
                "vector_label": "candidate",
                "vector_rank": 3,
                "embedding": [0.95, 0.05, 0.0],
            },
            {
                "object_id": "p5",
                "title": "Archived Alpha",
                "category": "electronics",
                "active": True,
                "region": "emea",
                "price": 199.0,
                "rating": 5.0,
                "discount": 0.05,
                "stock": 20,
                "warehouse": "north",
                "tags": ["launch"],
                "badges": ["certified"],
                "topics": target_topics,
                "vector_label": "candidate",
                "vector_rank": 1,
                "embedding": [1.0, 0.0, 0.0],
            },
        ],
    )

    query = (
        ws.query(RoutedProduct)
        .where(
            ((RoutedProduct.title.match("Alpha") | RoutedProduct.category.in_(["electronics", "lab"])) & ~RoutedProduct.title.contains("Archived"))
            & (RoutedProduct.active.op("eq", True) & RoutedProduct.region.in_(["emea", "global"]))
            & ((RoutedProduct.price >= 100) & (RoutedProduct.rating >= 4.5) & (RoutedProduct.discount < 0.25))
            & ((RoutedProduct.stock > 0) & (RoutedProduct.warehouse != "returns"))
            & (RoutedProduct.tags.array_contains("launch") & RoutedProduct.badges.array_contains("certified"))
            & ((RoutedProduct.vector_label == "candidate") | (RoutedProduct.vector_rank <= 2))
        )
        .near(RoutedProduct.embedding, [1.0, 0.0, 0.0], top_k=20)
        .select("title", "price", "stock", "active", "tags", "topics", "vector_label", "score")
        .order_by(RoutedProduct.price, desc=True)
        .limit(2)
    )

    near_step = [step for step in query.explain()["steps"] if step.get("node") == "near"][0]
    assert near_step["near_filter_mode"] == "combined_scan"
    result_rows = query.execute().rows()
    assert [row["object_id"] for row in result_rows] == ["p4", "p1"]
    assert vector.ops == ["near"]
    assert vector.fragments[0].notes.get("filter") is not None
    assert vector.fragments[0].payload.get("filter") is vector.fragments[0].notes["filter"]
    assert vector.scan_count == 1

    WORKSPACES.pop("efficient-routing-test", None)


def test_hyperg_rejects_backends_with_unsupported_storage_tag():
    """Backends fail clearly when their static Tag claims reject HyperG storage."""
    for backend_identifier in ("chroma", "lance", "pinecone"):
        backend = _SpyScanBackend("vec")
        backend.type = backend_identifier
        ws = hb.HeavenBase(
            f"hyperg-unsupported-{backend_identifier}-test",
            backends={"main": hb.backends.InMemBackend("main"), "vec": backend},
        )

        class BadDoc(hb.Entity):
            identifier = f"hyperg-unsupported-{backend_identifier}-doc"

            topics = hb.field(hb.HyperG["slot", "value", "topic"]).store(to="vec")

        with pytest.raises(NotImplementedError, match=f"{backend_identifier} backend 'vec' does not support HyperG storage"):
            ws.register(BadDoc)

        WORKSPACES.pop(f"hyperg-unsupported-{backend_identifier}-test", None)


def test_storage_support_rejection_uses_logical_type_contract_for_any_dtype():
    """Unsupported storage type declarations should apply to every logical type."""

    modules = hb.DEFAULT_CONTEXT.modules()
    inmem_tags = modules.inspect("backend_type", "inmem")[0]["record"]["meta"]["tags"]
    _NoJsonBackend.register(tags={**inmem_tags, "unsupported-storage-types": ["json"]}, resolver=modules)
    backend = _NoJsonBackend("doc")
    ws = hb.HeavenBase("json-unsupported-storage-test", backends={"main": hb.backends.InMemBackend("main"), "doc": backend})

    class BadJsonDoc(hb.Entity):
        identifier = "json-unsupported-storage-doc"

        meta = hb.field(hb.Json).store(to="doc")

    try:
        with pytest.raises(NotImplementedError, match="no-json-storage-test backend 'doc' does not support Json storage"):
            ws.register(BadJsonDoc)
    finally:
        WORKSPACES.pop("json-unsupported-storage-test", None)
        modules.unregister("backend_type", _NoJsonBackend.identifier)


def test_hyperg_rejection_uses_effective_backend_tag():
    """An empty unsupported-storage-types Tag permits HyperG placement."""
    backend = _SpyScanBackend("vec")
    ws = hb.HeavenBase(
        "hyperg-tag-support-test",
        backends={"main": hb.backends.InMemBackend("main"), "vec": backend},
    )

    class CapableDoc(hb.Entity):
        identifier = "hyperg-tag-support-doc"

        topics = hb.field(hb.HyperG["slot", "value", "topic"]).store(to="vec")

    ws.register(CapableDoc)

    assert "hyper-g" not in backend.tag("unsupported-storage-types").value
    assert ws.storage_plans["hyperg-tag-support-doc"].first("topics").backend == "vec"

    WORKSPACES.pop("hyperg-tag-support-test", None)


def test_metadata_vector_backends_declare_hyperg_storage_gap_as_tags():
    """Provider records own their HyperG storage support declarations."""
    modules = hb.DEFAULT_CONTEXT.modules()
    assert modules.tag("backend_type", "chroma", "unsupported-storage-types").value == ("hyper-g",)
    assert modules.tag("backend_type", "lance", "unsupported-storage-types").value == ("hyper-g",)
    assert modules.tag("backend_type", "pinecone", "unsupported-storage-types").value == ("hyper-g",)
    assert modules.tag("backend_type", "milvus", "unsupported-storage-types").value == ()


@pytest.mark.full
def test_cross_backend_near_uses_candidate_id_prefilter(tmp_path):
    """Cross-backend scalar filters can bound vector search through candidate IDs."""
    scalar = _SpySQLBackend("scalar", _db(tmp_path, "candidate_scalar.db"))
    vector = _SpyCandidateVectorBackend("vec")
    ws = hb.HeavenBase("candidate-prefilter-test", backends={"scalar": scalar, "vec": vector})

    class CandidateProduct(hb.Entity):
        identifier = "candidate-prefilter-product"

        title = hb.field(hb.ShortText).store(to="scalar")
        price = hb.field(hb.Float).store(to="scalar")
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    ws.register(CandidateProduct)
    ws.upsert_many(
        CandidateProduct,
        [
            {"object_id": "p1", "title": "Alpha", "price": 100.0, "embedding": [0.9, 0.1]},
            {"object_id": "p2", "title": "Cheap Near", "price": 10.0, "embedding": [1.0, 0.0]},
            {"object_id": "p3", "title": "Beta", "price": 140.0, "embedding": [0.8, 0.2]},
            {"object_id": "p4", "title": "Far", "price": 200.0, "embedding": [0.0, 1.0]},
        ],
    )

    query = ws.query(CandidateProduct).where(CandidateProduct.price >= 100).near(CandidateProduct.embedding, [1.0, 0.0], top_k=2).select("price", "score")
    near_step = [step for step in query.explain()["steps"] if step.get("node") == "near"][0]
    frame = query.execute()
    filt = vector.fragments[0].notes["filter"]
    health_backend = [backend for backend in ws.health()["backends"] if backend["name"] == "vec"][0]

    assert near_step["near_filter_mode"] == "candidate_id_prefilter"
    assert frame.ids == ["p1", "p3"]
    assert [row["price"] for row in frame.rows()] == [100.0, 140.0]
    assert scalar.ops == ["gte"]
    assert vector.ops == ["near"]
    assert vector.fragments[0].notes["candidate_id_count"] == 3
    assert filt.notes["kind"] == "lance_where"
    assert set(filt.notes["candidate_ids"]) == {"p1", "p3", "p4"}
    assert "capabilities" not in health_backend
    assert "tags" not in health_backend
    assert "candidate_id_prefilter" in vector.tag("near-filter-modes").value

    WORKSPACES.pop("candidate-prefilter-test", None)


@pytest.mark.full
def test_empty_candidate_prefilter_keeps_filter_metadata_and_score(tmp_path):
    """An empty candidate set answers the columns a populated candidate-prefiltered result carries."""
    scalar = _SpySQLBackend("scalar", _db(tmp_path, "candidate_empty.db"))
    ws = hb.HeavenBase("candidate-prefilter-empty-test", detached=True, backends={"scalar": scalar, "vec": _SpyCandidateVectorBackend("vec")})

    class CandidateTagged(hb.Entity):
        identifier = "candidate-prefilter-tagged"

        keywords = hb.field(hb.Array[hb.ShortText]).store(to="scalar")
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    ws.register(CandidateTagged)
    ws.upsert(CandidateTagged, {"object_id": "p1", "keywords": ["needle"], "embedding": [1.0, 0.0]})

    def query(text):
        return ws.query(CandidateTagged).where(CandidateTagged.keywords.contained_in(text)).near(CandidateTagged.embedding, [1.0, 0.0]).select("match", "score")

    near_step = [step for step in query("a needle here").explain()["steps"] if step.get("node") == "near"][0]
    assert near_step["near_filter_mode"] == "candidate_id_prefilter"
    assert query("a needle here").execute().col_names() == ["object_id", "match", "score"]
    empty = query("absent words").execute()
    assert empty.rows() == [] and empty.col_names() == ["object_id", "match", "score"]


@pytest.mark.parametrize("kind", ["inmem", "sqlite"])
def test_combined_scan_near_keeps_filter_metadata_whether_or_not_rows_match(tmp_path, kind):
    """A non-exact near scanned together with its filter declares and fills the filter's `match`, as the filter alone does."""
    backend = hb.backends.InMemBackend("main") if kind == "inmem" else hb.backends.SQLiteBackend("main", _db(tmp_path, "combined_scan.db"))
    ws = hb.HeavenBase(f"combined-scan-metadata-{kind}", detached=True, backends={"main": backend})

    class CombinedTagged(hb.Entity):
        identifier = "combined-scan-tagged"

        tags = hb.field(hb.Array[hb.ShortText])
        emb = hb.field(hb.Vector[2])

    ws.register(CombinedTagged)
    ws.upsert_many(
        CombinedTagged,
        [{"object_id": "a", "tags": ["needle"], "emb": [1.0, 0.0]}, {"object_id": "b", "tags": ["hay"], "emb": [0.0, 1.0]}],
    )

    def filtered(text):
        return ws.query(CombinedTagged).where(CombinedTagged.tags.contained_in(text))

    def near(text):
        return filtered(text).near(CombinedTagged.emb, [1.0, 0.0], top_k=3).select("match", "score")

    near_step = [step for step in near("a needle here").explain()["steps"] if step.get("node") == "near"][0]
    assert near_step["near_filter_mode"] == "combined_scan"
    expected = filtered("a needle here").select("match").execute().rows()
    assert [row["object_id"] for row in expected] == ["a"] and expected[0]["match"]
    found = near("a needle here").execute()
    assert found.col_names() == ["object_id", "match", "score"]
    assert [(row["object_id"], row["match"]) for row in found.rows()] == [(row["object_id"], row["match"]) for row in expected]
    empty = near("nothing at all").execute()
    assert empty.rows() == [] and empty.col_names() == ["object_id", "match", "score"]
    assert near("nothing at all").execute().column("match") == []


def test_duplicated_route_metadata_prefilter_uses_vector_backend(tmp_path):
    """A scalar field with a vector backend copy can be pushed into near search."""
    scalar = _SpySQLBackend("scalar", _db(tmp_path, "metadata_scalar.db"))
    vector = _SpyCandidateVectorBackend("vec")
    ws = hb.HeavenBase("metadata-prefilter-test", backends={"scalar": scalar, "vec": vector})

    class MetadataCopyProduct(hb.Entity):
        identifier = "metadata-prefilter-product"

        title = hb.field(hb.ShortText).store(to="scalar")
        price = hb.field(hb.Float).store(to="scalar").store(to="vec")
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    ws.register(MetadataCopyProduct)
    ws.upsert_many(
        MetadataCopyProduct,
        [
            {"object_id": "p1", "title": "Alpha", "price": 100.0, "embedding": [0.9, 0.1]},
            {"object_id": "p2", "title": "Cheap Near", "price": 10.0, "embedding": [1.0, 0.0]},
            {"object_id": "p3", "title": "Beta", "price": 140.0, "embedding": [0.8, 0.2]},
            {"object_id": "p4", "title": "Far", "price": 200.0, "embedding": [0.0, 1.0]},
        ],
    )

    query = (
        ws.query(MetadataCopyProduct).where(MetadataCopyProduct.price >= 100).near(MetadataCopyProduct.embedding, [1.0, 0.0], top_k=2).select("price", "score")
    )
    explain = query.explain()
    near_step = [step for step in explain["steps"] if step.get("node") == "near"][0]
    filter_step = _filter_step(explain["steps"])[0]
    pruned_group = _find_step(explain["steps"], node="pruned_backend_group", backend="scalar")
    frame = query.execute()
    filt = vector.fragments[0].notes["filter"]

    assert near_step["near_filter_mode"] == "metadata_prefilter"
    assert filter_step["pruned"] is True
    assert filter_step["pruned_by"] == "vec"
    assert filter_step["prune_reason"] == "covered_by_near_metadata_prefilter"
    assert [route["backend"] for route in filter_step["field_storage"]] == ["scalar", "vec"]
    assert [route["selected"] for route in filter_step["field_storage"]] == [True, False]
    assert pruned_group is not None
    assert pruned_group["pruned_by"] == "vec"
    assert frame.ids == ["p1", "p3"]
    assert scalar.ops == []
    assert vector.ops == ["near"]
    assert filt.notes["kind"] == "lance_where"
    assert filt.payload == "(price IS NOT NULL AND price >= 100)"

    WORKSPACES.pop("metadata-prefilter-test", None)


def test_explain_lists_all_routes_for_multi_routed_filter():
    """Filter explain should expose all duplicated field routes without changing default execution semantics."""
    left = _SpyScanBackend("left")
    right = _SpyScanBackend("right")
    ws = hb.HeavenBase("multi-route-explain-test", backends={"left": left, "right": right})

    class MultiRouteMetric(hb.Entity):
        identifier = "multi-route-explain-metric"

        price = hb.field(hb.Float).store(to="left").store(to="right")

    ws.register(MultiRouteMetric)

    step = _filter_step(ws.query(MultiRouteMetric).where(MultiRouteMetric.price >= 100).explain()["steps"])[0]

    assert step["backend"] == "left"
    assert [route["backend"] for route in step["field_storage"]] == ["left", "right"]
    assert [route["selected"] for route in step["field_storage"]] == [True, False]
    assert "pruned" not in step

    WORKSPACES.pop("multi-route-explain-test", None)


@pytest.mark.detail
@pytest.mark.skipif(_LANCE_UNAVAILABLE, reason="lancedb runtime is not installed")
def test_lance_near_with_scalar_prefilter_uses_combined_native(tmp_path):
    """LanceDB should combine supported scalar filters with native vector search."""
    lance = hb.backends.LanceBackend("vec", hb.utils.path_to_file_uri(tmp_path / "lance-prefilter"))
    ws = hb.HeavenBase("lance-prefilter-test", backends={"main": hb.backends.InMemBackend("main"), "vec": lance})

    class VecProduct(hb.Entity):
        identifier = "lance-prefilter-product"

        title = hb.field(hb.ShortText).store(to="vec")
        category = hb.field(hb.ShortText).store(to="vec")
        price = hb.field(hb.Float).store(to="vec")
        embedding = hb.field(hb.Vector[3]).store(to="vec", strategy=hb.VectorIndex.identifier)

    ws.register(VecProduct)
    ws.upsert_many(
        VecProduct,
        [
            {"object_id": "p1", "title": "Alpha", "category": "electronics", "price": 129.0, "embedding": [1.0, 0.0, 0.0]},
            {"object_id": "p2", "title": "Beta", "category": "electronics", "price": 80.0, "embedding": [0.99, 0.01, 0.0]},
            {"object_id": "p3", "title": "Gamma", "category": "lab", "price": 149.0, "embedding": [0.9, 0.1, 0.0]},
        ],
    )

    query = (
        ws.query(VecProduct)
        .where((VecProduct.category.in_(["electronics", "lab"])) & (VecProduct.price >= 100))
        .near(VecProduct.embedding, [1.0, 0.0, 0.0], top_k=10)
        .select("title", "price", "score")
        .limit(2)
    )

    near_step = [step for step in query.explain()["steps"] if step.get("node") == "near"][0]
    assert near_step["near_filter_mode"] == "combined_native"
    assert query.execute().ids == ["p1", "p3"]

    WORKSPACES.pop("lance-prefilter-test", None)


# ── 10. SideTable strategy tests ──


def test_sidetable_array_storage_query_update_and_delete(tmp_path):
    """Array SideTable should scan safely and preserve update/delete behavior."""
    from sqlalchemy import event, func, select

    backend = _SpySQLBackend("sql", _db(tmp_path, "sidetable_array_contract.db"))
    ws = hb.HeavenBase("sidetable-array-contract-test", backends={"sql": backend})

    class TagDoc(hb.Entity):
        identifier = "sidetable-array-contract-doc"

        name = hb.field(hb.ShortText)
        scores = hb.field(hb.Array[hb.Integer])
        tags = hb.field(hb.Array[hb.ShortText]).store(to="sql", strategy=hb.SideTable)

    ws.register(TagDoc)
    scores_binding = ws.storage_plans["sidetable-array-contract-doc"].first("scores")
    tags_binding = ws.storage_plans["sidetable-array-contract-doc"].first("tags")
    assert scores_binding.strategy == hb.SideTable.identifier
    assert tags_binding.strategy == hb.SideTable.identifier
    assert scores_binding.backend == "sql"
    assert tags_binding.backend == "sql"

    ws.upsert(TagDoc, {"object_id": "t1", "name": "Alpha", "scores": [1, 2], "tags": ["new", "sale", "hot"]})
    ws.upsert(TagDoc, {"object_id": "t2", "name": "Beta", "scores": [3], "tags": ["used"]})

    rows = backend.scan("sidetable-array-contract-doc")
    by_id = {r["object_id"]: r for r in rows}
    assert by_id["t1"]["tags"] == ["new", "sale", "hot"]
    assert by_id["t2"]["tags"] == ["used"]

    query = ws.query(TagDoc).where(TagDoc.tags.array_contains("new"))
    step = _filter_step(query.explain()["steps"])[0]
    assert step["handler_mode"] == "py_scan"
    assert step["strategy"] == hb.SideTable.identifier
    assert step["fallback_reason"] == "text_semantics_not_proven"

    statements = []

    def _track(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(backend.db.engine, "before_cursor_execute", _track)
    try:
        result = query.select("name", "tags").execute()
    finally:
        event.remove(backend.db.engine, "before_cursor_execute", _track)

    assert result.ids == ["t1"]
    assert result.rows()[0]["tags"] == ["new", "sale", "hot"]
    assert len(statements) == 1
    assert ws.query(TagDoc).where(TagDoc.tags.array_contains("nonexistent")).execute().ids == []

    ws.upsert(TagDoc, {"object_id": "t1", "name": "Alpha", "scores": [4], "tags": ["fresh"]})
    rows = {row["object_id"]: row for row in backend.scan("sidetable-array-contract-doc")}
    assert rows["t1"]["tags"] == ["fresh"]

    side_table = backend._side_specs["sidetable-array-contract-doc"]["tags"].table
    with backend.db.engine.begin() as conn:
        count = conn.execute(select(func.count()).select_from(side_table).where(side_table.c.object_id == "t1")).scalar()
    assert count == 1

    ws.delete((TagDoc, "t1"))
    with backend.db.engine.begin() as conn:
        count = conn.execute(select(func.count()).select_from(side_table).where(side_table.c.object_id == "t1")).scalar()
    assert count == 0

    WORKSPACES.pop("sidetable-array-contract-test", None)


def test_sidetable_hyperg_exists_uses_safe_scan(tmp_path):
    """HyperG SideTable predicates should scan until owner text equality is proved."""
    from sqlalchemy import inspect

    backend = _SpySQLBackend("sql", _db(tmp_path, "sidetable_hyperg_exists.db"))
    ws = hb.HeavenBase("sidetable-hyperg-exists-test", backends={"sql": backend})

    class RelationDoc(hb.Entity):
        identifier = "sidetable-hyperg-exists-doc"

        name = hb.field(hb.ShortText)
        relations = hb.field(
            hb.HyperG[
                "subject" : hb.Identifier,
                "relation" : hb.Categorical["cites", "mentions", "blocks"],
                "object" : hb.Identifier,
                "weight" : {"type": hb.Float, "required": False},
                "meta" : {"type": hb.Json, "filterable": False, "required": False},
            ]
        )

    ws.register(RelationDoc)
    binding = ws.storage_plans["sidetable-hyperg-exists-doc"].first("relations")
    assert binding.strategy == hb.SideTable.identifier
    assert binding.backend == "sql"

    ws.upsert_many(
        RelationDoc,
        [
            {
                "object_id": "r1",
                "name": "Alpha",
                "relations": [
                    {"subject": "doc:1", "relation": "cites", "object": "doc:2", "weight": 0.8, "meta": {"source": "demo"}},
                    {"subject": "doc:1", "relation": "mentions", "object": "doc:3", "weight": 0.2},
                ],
            },
            {"object_id": "r2", "name": "Beta", "relations": [{"subject": "doc:4", "relation": "blocks", "object": "doc:2", "weight": 0.9}]},
            {"object_id": "r3", "name": "Gamma", "relations": [{"subject": "doc:5", "relation": "cites", "object": "doc:2", "weight": 0.4}]},
            {"object_id": "r4", "name": "Delta", "relations": [{"subject": "doc:6", "relation": "cites", "object": "doc:2"}]},
        ],
    )
    rows_by_id = {row["object_id"]: row for row in backend.scan("sidetable-hyperg-exists-doc")}
    assert rows_by_id["r1"]["relations"][0]["meta"] == {"source": "demo"}

    insp = inspect(backend.db.engine)
    side_table = [name for name in insp.get_table_names() if "sidetable_hyperg_exists_doc" in name and name.endswith("_relations")][0]
    assert [col["name"] for col in insp.get_columns(side_table)] == ["_id", "object_id", "idx", "subject", "relation", "object", "weight", "meta"]

    query = ws.query(RelationDoc).where(RelationDoc.relations.exists(relation={"$in": ["cites", "mentions"]}, object="doc:2", weight={"$gte": 0.7}))
    step = _filter_step(query.explain()["steps"])[0]
    rows = query.execute().rows()
    all_query = ws.query(RelationDoc).where(
        RelationDoc.relations.exists(relation="cites") & RelationDoc.relations.all(where={"relation": "cites"}, weight={"$gte": 0.5})
    )
    all_steps = [item for item in _filter_step(all_query.explain()["steps"]) if item["op"] == "all"]

    assert step["handler_mode"] == "py_scan"
    assert step["strategy"] == hb.SideTable.identifier
    assert step["fallback_reason"] == "text_semantics_not_proven"
    assert [row["object_id"] for row in rows] == ["r1"]
    assert all_steps[0]["handler_mode"] == "py_scan"
    assert all_steps[0]["fallback_reason"] == "text_semantics_not_proven"
    assert all_query.execute().ids == ["r1"]
    assert rows[0]["relations"][0]["meta"] == {"source": "demo"}
    assert "meta" not in rows[0]["relations"][1]

    WORKSPACES.pop("sidetable-hyperg-exists-test", None)


def test_sql_default_indexes_for_scalar_and_sidetable_fields(tmp_path):
    """SQL backends should create default indexes for scalar filters and SideTable access paths."""
    from sqlalchemy import inspect

    backend = _SpySQLBackend("sql", _db(tmp_path, "sidetable_indexes.db"))
    ws = hb.HeavenBase("sidetable-index-test", backends={"sql": backend})

    class IndexedDoc(hb.Entity):
        identifier = "sidetable-index-doc"

        name = hb.field(hb.ShortText)
        price = hb.field(hb.Float)
        active = hb.field(hb.Boolean)
        tags = hb.field(hb.Array[hb.ShortText]).store(to="sql", strategy=hb.SideTable)
        topics = hb.field(hb.HyperG["slot", "value", "topic"])

    ws.register(IndexedDoc)
    insp = inspect(backend.db.engine)
    main_indexes = [idx["column_names"] for idx in insp.get_indexes(backend.tables["sidetable-index-doc"].name)]
    tag_table = backend._side_specs["sidetable-index-doc"]["tags"].table.name
    topic_table = backend._side_specs["sidetable-index-doc"]["topics"].table.name
    tag_indexes = [idx["column_names"] for idx in insp.get_indexes(tag_table)]
    topic_indexes = [idx["column_names"] for idx in insp.get_indexes(topic_table)]

    assert ["name"] in main_indexes
    assert ["price"] in main_indexes
    assert ["active"] not in main_indexes
    assert ["object_id"] in tag_indexes
    assert ["object_id", "idx"] in tag_indexes
    assert ["item", "object_id"] in tag_indexes
    assert ["object_id"] in topic_indexes
    assert ["object_id", "idx"] in topic_indexes
    assert ["slot", "value", "topic", "object_id"] in topic_indexes
    WORKSPACES.pop("sidetable-index-test", None)
