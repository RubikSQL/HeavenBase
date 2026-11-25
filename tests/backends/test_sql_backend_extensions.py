"""Focused contracts for the extended SQL backend matrix."""

from collections.abc import Iterator
import os
from importlib.util import find_spec
from uuid import uuid4

import pytest
import sqlalchemy as sa

import heavenbase as hb
from heavenbase.backends.sql.handler.base import sql_sparse_gram_contained_in_handler
from heavenbase.placement import StorageBinding
from heavenbase.schema import EntitySchema, FieldSchema
from heavenbase.utils import Any

_EXTERNAL_SQL_ROUTES = [
    pytest.param("supabase", hb.backends.SupabaseBackend, "pg", True, id="supabase-postgresql-wire"),
    pytest.param("seekdb", hb.backends.SeekDBBackend, "mysql", False, id="seekdb"),
    pytest.param("dolt", hb.backends.DoltBackend, "mysql", False, id="dolt"),
    pytest.param("clickhouse", hb.backends.ClickHouseBackend, "clickhouse", True, id="clickhouse"),
    pytest.param("oceanbase", hb.backends.OceanBaseBackend, "oceanbase", False, id="oceanbase"),
]


def _external_sql_connection(route: str) -> tuple[str, dict[str, Any]]:
    """Resolve one live-test connection exclusively from explicit environment input."""
    prefix = f"HVNB_{route.upper()}"
    names = {field.lower(): f"{prefix}_{field}" for field in ("HOST", "PORT", "USER", "PASSWORD", "DATABASE")}
    missing = [name for key, name in names.items() if name not in os.environ or (key != "password" and not os.environ[name].strip())]
    if missing:
        pytest.skip(f"{route} external SQL contract requires: {', '.join(missing)}")
    try:
        port = int(os.environ[names["port"]])
    except ValueError:
        pytest.fail(f"{names['port']} must be an integer")
    return os.environ[names["database"]], {
        "host": os.environ[names["host"]],
        "port": port,
        "username": os.environ[names["user"]],
        "password": os.environ[names["password"]],
    }


def _require_external_sql(route: str, provider: str, database: str, connection: dict[str, Any]) -> None:
    """Fail when an explicitly configured external SQL service is unreachable."""
    probe = hb.Database(provider=provider, database=database, autocreate=False, **connection)
    reachable = False
    failures: list[str] = []
    try:
        reachable = bool(probe.available(check=True))
    except Exception as exc:
        failures.append(f"availability probe raised {type(exc).__name__}: {exc}")
    try:
        probe.close()
    except Exception as exc:
        failures.append(f"probe cleanup raised {type(exc).__name__}: {exc}")
    if not reachable:
        failures.insert(0, "availability probe returned false")
    if failures:
        pytest.fail(f"configured {route} external SQL service is unreachable: {'; '.join(failures)}", pytrace=False)


def _external_backend(
    backend_type: type[Any],
    *,
    scope: str,
    database: str,
    shared_database: bool,
    connection: dict[str, Any],
) -> Any:
    """Build one backend with either a fixed shared database or a unique database namespace."""
    target_database = database if shared_database else scope
    return backend_type("sql", target_database, ws_id=scope, **connection)


def _external_workspace(scope: str, backend: Any) -> hb.HeavenBase:
    """Bootstrap fixed entities in memory before attaching the backend under test."""
    workspace = hb.HeavenBase(scope, detached=True, backends={"main": hb.backends.InMemBackend("main")})
    workspace.backends.register(backend)
    return workspace


def _assert_external_sql_cleanup(backends: list[Any]) -> None:
    """Drop every owned live-test namespace and surface all cleanup failures."""
    failures: list[str] = []
    for backend in reversed(backends):
        try:
            backend.drop()
        except Exception as exc:
            failures.append(f"{backend.name!r} ({backend.type}): {type(exc).__name__}: {exc}")
    if failures:
        pytest.fail("external SQL cleanup failed:\n" + "\n".join(f"- {failure}" for failure in failures), pytrace=False)


@pytest.fixture
def external_sql_backends() -> Iterator[list[Any]]:
    """Track live backends so cleanup is reported as a distinct teardown result."""
    backends: list[Any] = []
    yield backends
    _assert_external_sql_cleanup(backends)


@pytest.mark.fast
def test_external_sql_without_explicit_environment_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent live-test configuration remains an intentional skip."""
    for field in ("HOST", "PORT", "USER", "PASSWORD", "DATABASE"):
        monkeypatch.delenv(f"HVNB_UNCONFIGURED_{field}", raising=False)

    with pytest.raises(pytest.skip.Exception, match="external SQL contract requires"):
        _external_sql_connection("unconfigured")


@pytest.mark.fast
def test_configured_external_sql_unavailability_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit live-test configuration cannot silently turn into a skip."""

    class UnavailableProbe:
        def available(self, *, check: bool) -> bool:
            assert check is True
            return False

        def close(self) -> None:
            return None

    monkeypatch.setattr(hb, "Database", lambda **_kwargs: UnavailableProbe())

    with pytest.raises(pytest.fail.Exception, match="configured seekdb external SQL service is unreachable"):
        _require_external_sql("seekdb", "mysql", "heavenbase", {})


@pytest.mark.fast
def test_external_sql_cleanup_attempts_every_backend_and_reports_failures() -> None:
    """Cleanup keeps going after one failure and reports the complete teardown state."""
    calls: list[str] = []

    class CleanupBackend:
        type = "test"

        def __init__(self, name: str, *, fail: bool) -> None:
            self.name = name
            self.fail = fail

        def drop(self) -> None:
            calls.append(self.name)
            if self.fail:
                raise RuntimeError(f"cannot drop {self.name}")

    backends = [CleanupBackend("first", fail=True), CleanupBackend("second", fail=False)]

    with pytest.raises(pytest.fail.Exception, match="cannot drop first"):
        _assert_external_sql_cleanup(backends)

    assert calls == ["second", "first"]


@pytest.mark.fast
def test_protocol_backends_keep_logical_identity_and_physical_provider() -> None:
    """Protocol-compatible products retain canonical construction-time records."""
    supabase = hb.BackendType.build("main", {"type": "supabase"}, ws_id="team-one")
    seekdb = hb.BackendType.build("seek", {"type": "seekdb"}, ws_id="team-one")
    dolt = hb.BackendType.build("history", {"type": "dolt"}, ws_id="team-one")

    assert (supabase.type, supabase.provider, supabase.database) == ("supabase", "pg", "postgres")
    assert supabase.tag("workspace-database-namespace").value is False
    assert supabase.db.spec.provider == "postgres"
    assert supabase.db.spec.dialect == "postgresql"

    assert (seekdb.type, seekdb.provider, seekdb.database) == ("seekdb", "mysql", "team_one")
    assert seekdb.tag("workspace-database-namespace").value is True
    assert seekdb.db.spec.provider == "mysql"
    assert seekdb.db.spec.port == 2881

    assert (dolt.type, dolt.provider, dolt.database) == ("dolt", "mysql", "team_one")
    assert dolt.tag("workspace-database-namespace").value is True
    assert dolt.db.spec.provider == "mysql"
    assert dolt.db.spec.port == 3306


@pytest.mark.fast
def test_supabase_scopes_tables_and_drop_to_one_workspace() -> None:
    """Managed Supabase workspaces cannot collide or drop another workspace's tables."""
    first = hb.backends.SupabaseBackend("main", ws_id="workspace-one")
    second = hb.backends.SupabaseBackend("main", ws_id="workspace-two")
    first_main = first._table_name("catalog-entry")
    first_side = first._side_table_name("catalog-entry", "labels")
    second_main = second._table_name("catalog-entry")

    assert first_main != second_main
    assert len(first_main) <= 63
    assert len(first_side) <= 63

    class FakeDatabase:
        def __init__(self) -> None:
            self.dropped: list[str] = []
            self.closed = False

        def tables(self) -> list[str]:
            return [first_main, first_side, second_main, "unrelated"]

        def drop_table(self, name: str) -> None:
            self.dropped.append(name)

        def close(self) -> None:
            self.closed = True

    fake = FakeDatabase()
    first.db = fake
    first.drop()

    assert set(fake.dropped) == {first_main, first_side}
    assert second_main not in fake.dropped
    assert "unrelated" not in fake.dropped
    assert fake.closed is True
    assert first.db is None


@pytest.mark.fast
@pytest.mark.parametrize("backend_type", ["supabase", "clickhouse"])
def test_shared_database_scope_rebind_clears_cached_tables(backend_type: str) -> None:
    """Shared-database adapters reset table state across A to B to A rebinding."""
    backend = hb.BackendType.build("main", {"type": backend_type}, ws_id="scope-a")
    database = backend.db
    first_name = backend._table_name("entry")
    backend.tables["entry"] = sa.Table(first_name, backend.metadata, sa.Column("object_id", sa.String()))

    backend.bind_workspace("scope-b")

    assert backend.db is database
    assert backend.tables == {}
    second_name = backend._table_name("entry")
    assert second_name != first_name
    backend.tables["entry"] = sa.Table(second_name, backend.metadata, sa.Column("object_id", sa.String()))

    backend.bind_workspace("scope-a")

    assert backend.db is database
    assert backend.tables == {}
    assert backend._table_name("entry") == first_name


@pytest.mark.parametrize(
    ("backend_type", "kwargs"),
    [
        pytest.param(hb.backends.SupabaseBackend, {}, id="supabase"),
        pytest.param(hb.backends.PgVectorBackend, {"shared_database": True}, id="pgvector-shared"),
    ],
)
def test_shared_sql_without_sparse_gram_adapter_reports_scan_fallback(backend_type: type[Any], kwargs: dict[str, Any]) -> None:
    """Instance-local adapter absence must remain truthful in fragments and explain."""
    backend = backend_type("main", ws_id="shared-sql-scan", **kwargs)
    field = FieldSchema("keywords", hb.Array[hb.ShortText])
    context = hb.ext.HandlerContext(
        backend.name,
        backend.identifier,
        hb.SparseGramIndex.identifier,
        backend_instance=backend,
        entity_id="shared-sql-document",
    )

    fragment = sql_sparse_gram_contained_in_handler(field, "blue whale", context)

    assert fragment.mode() == "py_scan"
    assert fragment.notes.get("strategy_adapter") is None
    assert "no SparseGramIndex strategy adapter" in fragment.notes["fallback_reason"]


@pytest.mark.skipif(find_spec("pgvector") is None, reason="pgvector is not installed")
@pytest.mark.parametrize("object_id_chars", [None, 200])
def test_pgvector_ensure_creates_only_fields_routed_to_split_backend(object_id_chars: int | None) -> None:
    """Required scalars owned by Supabase cannot leak into pgvector DDL; object_id takes the schema's own domain."""
    backend = hb.backends.PgVectorBackend("vec", database="postgres", ws_id="split-placement", shared_database=True)
    dialect_spec = backend.db.dialect_spec

    class FakeDatabase:
        def __init__(self) -> None:
            self.dialect_spec = dialect_spec
            self.created: Any = None

        def execute(self, statement: str, *, readonly: bool) -> None:
            assert statement == "CREATE EXTENSION IF NOT EXISTS vector"
            assert readonly is False

        def create_table(self, table: Any) -> None:
            self.created = table

        def columns(self, name: str, *, schema: str | None = None) -> list[str]:
            assert self.created is not None
            assert (name, schema) == (self.created.name, self.created.schema)
            return list(self.created.c.keys())

        def add_table_col(self, table: Any, column: Any) -> None:
            raise AssertionError(f"unexpected missing column {table.name}.{column.name}")

    schema = EntitySchema(
        "split-placement-doc",
        "SplitPlacementDoc",
        fields={
            "object_id": FieldSchema("object_id", hb.Identifier(max_chars=object_id_chars), pk=True, required=True),
            "title": FieldSchema("title", hb.ShortText(), required=True),
            "embedding": FieldSchema("embedding", hb.Vector[2], required=True),
        },
    )
    bindings = [
        StorageBinding(schema.entity_id, "object_id", "main", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "title", "main", hb.InlineColumn.identifier, "test"),
        StorageBinding(schema.entity_id, "embedding", "vec", hb.VectorIndex.identifier, "test"),
    ]
    fake = FakeDatabase()
    backend.db = fake

    backend.ensure(schema, bindings)

    assert list(fake.created.c.keys()) == ["object_id", "embedding"]
    assert fake.created.c.embedding.nullable is False
    assert fake.created.c.object_id.type.length == (object_id_chars or 63)


@pytest.mark.fast
def test_oceanbase_defaults_and_sqlserver_aliases_resolve() -> None:
    """OceanBase constructs from defaults and common SQL Server names stay canonical."""
    oceanbase = hb.backends.OceanBaseBackend("ocean", database="heavenbase")

    assert oceanbase.type == "oceanbase"
    assert oceanbase.provider == "oceanbase"
    assert oceanbase.db.spec.provider == "oceanbase"
    assert oceanbase.db.spec.dialect == "mysql"
    assert oceanbase.db.spec.port == 2881
    assert hb.BackendType.identifier_of("sqlserver") == "mssql"
    assert hb.BackendType.identifier_of("sql-server") == "mssql"
    assert hb.ext.sql_dialect_spec("sqlserver").identifier == "mssql"
    assert hb.ext.database_dialects.get("sqlserver").identifier == "mssql"


@pytest.mark.fast
def test_mysql_compatible_duplicate_index_names_are_idempotent() -> None:
    """MySQL-wire providers may report a duplicate when index reflection is incomplete."""
    backend = hb.backends.SeekDBBackend("seek", database="heavenbase")
    table = sa.Table("entry", sa.MetaData(), sa.Column("rank", sa.Integer()))
    sa.Index("ix_entry_rank", table.c.rank)

    class FakeDatabase:
        def __init__(self) -> None:
            self.calls = 0

        def index_column_sets(self, _table: Any) -> dict[tuple[str, ...], bool]:
            return {}

        def create_index(self, _index: Any, *, checkfirst: bool) -> None:
            self.calls += 1
            raise sa.exc.OperationalError("CREATE INDEX", {}, RuntimeError("(1061, \"Duplicate key name 'ix_entry_rank'\")"))

    fake = FakeDatabase()
    backend.db = fake
    backend._create_indexes(table)

    assert fake.calls == 1


@pytest.mark.fast
def test_clickhouse_declares_shared_database_and_nontransactional_writes() -> None:
    """ClickHouse uses a shared database safely and never enters the SQL transaction path."""
    backend = hb.BackendType.build("warehouse", {"type": "clickhouse"}, ws_id="analytics")

    assert backend.type == "clickhouse"
    assert backend.provider == "clickhouse"
    assert backend.database == "default"
    assert backend.tag("workspace-database-namespace").value is False
    assert backend.tag("binary-storage").value == "base64"
    assert hb.DEFAULT_CONTEXT.modules().tag("backend_type", "clickhouse", "binary-storage").value == "base64"
    assert backend.scalar_index().value is False
    assert backend.tag("unsupported-storage-types").value == ("array", "hyper-g")
    assert backend.db.spec.provider == "clickhouse"
    assert backend.db.spec.dialect == "clickhousedb"
    assert backend.db.dialect_spec.column_reflection_sql == "DESCRIBE TABLE {table_name}"
    assert backend._table_name("event") != hb.backends.ClickHouseBackend("warehouse", ws_id="other")._table_name("event")

    table = sa.Table(
        backend._table_name("event"),
        sa.MetaData(),
        sa.Column("object_id", sa.String(), primary_key=True),
        sa.Column("value", sa.Integer()),
    )
    backend.tables["event"] = table

    class FakeDatabase:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, Any, bool]] = []

        def tx(self) -> None:
            raise AssertionError("ClickHouse upsert must not open an OLTP transaction")

        def orm_execute(self, statement: Any, params: Any = None, *, readonly: bool = False) -> None:
            self.calls.append((statement, params, readonly))

    fake = FakeDatabase()
    backend.db = fake
    backend.upsert([hb.ext.RowOp("event", "e1", {"value": 7})])

    assert len(fake.calls) == 2
    delete_stmt, delete_params, delete_readonly = fake.calls[0]
    insert_stmt, insert_params, insert_readonly = fake.calls[1]
    assert isinstance(delete_stmt, sa.sql.dml.Delete)
    assert delete_stmt.get_execution_options()["settings"] == {"lightweight_deletes_sync": 1}
    assert delete_params is None
    assert delete_readonly is False
    assert isinstance(insert_stmt, sa.sql.dml.Insert)
    assert insert_params == [{"object_id": "e1", "value": 7}]
    assert insert_readonly is False


@pytest.mark.fast
def test_clickhouse_preserves_per_row_order_when_a_later_insert_fails() -> None:
    """A failed replacement cannot reorder the delete for a later ClickHouse row."""
    backend = hb.backends.ClickHouseBackend("warehouse", ws_id="analytics")
    table = sa.Table(
        backend._table_name("event"),
        sa.MetaData(),
        sa.Column("object_id", sa.String(), primary_key=True),
        sa.Column("value", sa.Integer()),
    )
    backend.tables["event"] = table

    class FailingDatabase:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []
            self.inserts = 0

        def orm_execute(self, statement: Any, params: Any = None, *, readonly: bool = False) -> None:
            assert readonly is False
            if isinstance(statement, sa.sql.dml.Delete):
                compiled = statement.compile()
                object_ids = next(value for value in compiled.params.values() if isinstance(value, list))
                self.calls.append(("delete", object_ids))
                return
            assert isinstance(statement, sa.sql.dml.Insert)
            self.inserts += 1
            self.calls.append(("insert", params))
            if self.inserts == 2:
                raise RuntimeError("second insert failed")

    fake = FailingDatabase()
    backend.db = fake

    with pytest.raises(RuntimeError, match="second insert failed"):
        backend.upsert(
            [
                hb.ext.RowOp("event", "e1", {"value": 1}),
                hb.ext.RowOp("event", "e2", {"value": 2}),
                hb.ext.RowOp("event", "e3", {"value": 3}),
            ]
        )

    assert fake.calls == [
        ("delete", ["e1"]),
        ("insert", [{"object_id": "e1", "value": 1}]),
        ("delete", ["e2"]),
        ("insert", [{"object_id": "e2", "value": 2}]),
    ]


@pytest.mark.fast
@pytest.mark.skipif(find_spec("clickhouse_connect") is None, reason="clickhouse-connect is not installed")
def test_clickhouse_artifact_type_roundtrips_canonical_base64() -> None:
    """The ClickHouse Artifact processor persists text and restores logical bytes."""
    from heavenbase.backends.sql.clickhouse import _clickhouse_col
    from heavenbase.utils.serialize import dumps_b64

    payload = b"\x00clickhouse\xff"
    column_type = _clickhouse_col(hb.Artifact())
    bind = column_type.bind_processor(None)
    result = column_type.result_processor(None, None)

    stored = bind(payload)
    assert stored == dumps_b64(payload)
    assert isinstance(stored, str)
    assert hb.Artifact().decode(result(stored)) == payload


@pytest.mark.fast
@pytest.mark.skipif(find_spec("clickhouse_connect") is None, reason="clickhouse-connect is not installed")
def test_clickhouse_tables_use_mergetree_engines() -> None:
    """ClickHouse main and side tables declare MergeTree ordering explicitly."""
    backend = hb.backends.ClickHouseBackend("warehouse", ws_id="analytics")
    main_engine = backend._table_args(order_by=("object_id",))[0]
    side_engine = backend._table_args(order_by=("object_id", "idx"))[0]

    assert " ".join(str(main_engine.compile()).split()) == "Engine MergeTree ORDER BY object_id"
    assert " ".join(str(side_engine.compile()).split()) == "Engine MergeTree ORDER BY (object_id,idx)"


@pytest.mark.external
@pytest.mark.parametrize("route,backend_type,provider,shared_database", _EXTERNAL_SQL_ROUTES)
def test_extended_sql_backends_live_contract(
    route: str,
    backend_type: type[Any],
    provider: str,
    shared_database: bool,
    external_sql_backends: list[Any],
) -> None:
    """Live extended SQL routes preserve ordered writes, native reads, and drop isolation."""
    database, connection = _external_sql_connection(route)
    _require_external_sql(route, provider, database, connection)
    token = uuid4().hex[:10]
    primary_scope = f"ext-{route}-{token}-a"
    survivor_scope = f"ext-{route}-{token}-b"
    entity_id = f"ext-{route}-{token}-record"

    class ExternalSQLRecord(hb.Entity):
        identifier = entity_id

        label = hb.field(hb.ShortText).store(to="sql", strategy=hb.InlineColumn)
        rank = hb.field(hb.Integer).store(to="sql", strategy=hb.InlineColumn)

    primary = _external_backend(
        backend_type,
        scope=primary_scope,
        database=database,
        shared_database=shared_database,
        connection=connection,
    )
    external_sql_backends.append(primary)
    survivor = _external_backend(
        backend_type,
        scope=survivor_scope,
        database=database,
        shared_database=shared_database,
        connection=connection,
    )
    external_sql_backends.append(survivor)
    primary_ws = _external_workspace(primary_scope, primary)
    survivor_ws = _external_workspace(survivor_scope, survivor)
    primary_ws.register(ExternalSQLRecord)
    survivor_ws.register(ExternalSQLRecord)

    primary.upsert(
        [
            hb.ext.RowOp(entity_id, "alpha", {"label": "old", "rank": 1}),
            hb.ext.RowOp(entity_id, "beta", {"label": "remove", "rank": 2}),
            hb.ext.RowOp(entity_id, "alpha", {"label": "replacement", "rank": 7}),
            hb.ext.RowOp(entity_id, "beta", op="delete"),
            hb.ext.RowOp(entity_id, "gamma", {"label": "kept", "rank": 3}),
        ]
    )

    rows = primary.get_many(entity_id, ["gamma", "missing", "alpha", "beta"])
    assert [None if row is None else row["object_id"] for row in rows] == ["gamma", None, "alpha", None]
    assert rows[2] == {"object_id": "alpha", "label": "replacement", "rank": 7}
    assert primary.exists_many(entity_id, ["alpha", "beta", "gamma"]) == [True, False, True]
    assert primary.count(entity_id) == 2
    assert primary.scan(entity_id) == [
        {"object_id": "alpha", "label": "replacement", "rank": 7},
        {"object_id": "gamma", "label": "kept", "rank": 3},
    ]

    query = primary_ws.query(ExternalSQLRecord).where(ExternalSQLRecord.rank >= 3)
    assert set(query.execute().ids) == {"alpha", "gamma"}
    filter_step = next(step for step in query.explain()["steps"] if step.get("node") == "filter")
    assert filter_step["backend"] == "sql"
    assert filter_step["type"] == route
    assert filter_step["handler_mode"] == "native"

    survivor.upsert([hb.ext.RowOp(entity_id, "sentinel", {"label": "isolated", "rank": 11})])
    primary.drop()
    assert survivor.get_many(entity_id, ["sentinel"]) == [{"object_id": "sentinel", "label": "isolated", "rank": 11}]
    assert survivor.exists_many(entity_id, ["sentinel"]) == [True]
    assert survivor.count(entity_id) == 1
