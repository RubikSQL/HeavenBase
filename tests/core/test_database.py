import sys
from importlib.util import find_spec

import pytest
from sqlalchemy import text

import heavenbase as hb
from heavenbase.database import table_display
from heavenbase.utils import cmd, loads_json
from heavenbase.database.dialect.registry import BootstrapDialectCatalog, database_dialects


def _db(tmp_path, name: str) -> str:
    return hb.utils.path_to_file_uri(tmp_path / name)


def _db_spec(*, provider: str, dialect: str | None = None, superuser=None, **data):
    """Build a minimal executable DBSpec for dialect-only unit tests."""
    selected_dialect = dialect or provider
    return hb.DBSpec(
        "db",
        {"provider": provider, "dialect": selected_dialect, **data},
        bindings={
            "contract": "db-route/v1",
            "provider_dialect": selected_dialect,
            "dialect": database_dialects.get(selected_dialect).to_dict(),
            "superuser": superuser or {},
        },
    )


def _bind_test_dialect(db, dialect: str) -> None:
    """Replace a database's captured dialect contract for a focused unit test."""
    db.dialect = dialect
    db.dialect_spec = database_dialects.get(dialect)


def test_database_exposes_engine_and_short_feature_api(tmp_path):
    db = hb.Database(provider="sqlite", database=_db(tmp_path, "feature.db"))

    assert db.engine.dialect.name == "sqlite"
    db.execute("CREATE TABLE items (id TEXT PRIMARY KEY, name TEXT, price FLOAT)")
    db.execute(
        "INSERT INTO items (id, name, price) VALUES (:id, :name, :price)",
        [{"id": "a", "name": "alpha", "price": 1.5}, {"id": "b", "name": "beta", "price": 2.5}],
    )

    assert db.current_database() == "main"
    assert db.tables() == ["items"]
    assert db.columns("items") == ["id", "name", "price"]
    assert db.pks("items") == ["id"]
    assert db.n_rows("items") == 2
    assert db.table_size("items") == 2
    assert db.col_type("items", "name")
    assert db.col_agg("items", "price", "count") == 2
    assert db.col_distincts("items", "name") == ["alpha", "beta"]
    assert db.col_freqs("items", "name", topk=1)[0] == {"value": "beta", "n": 1}
    assert db.browse("items", orderby="id").to_dicts() == [
        {"id": "a", "name": "alpha", "price": 1.5},
        {"id": "b", "name": "beta", "price": 2.5},
    ]
    assert db.describe("items")["n_rows"] == 2
    for name in (
        "add_tab_col",
        "clear_tab",
        "col_freqk",
        "cols",
        "create_tab",
        "create_tabs",
        "db_name",
        "db_tabs",
        "db_views",
        "drop_tab",
        "drop_tab_comment",
        "init",
        "list_tabs",
        "row_count",
        "row_sample",
        "set_tab_comment",
        "sort_tab_col",
        "tab_cols",
        "tab_comment",
        "tab_comments",
        "tab_fks",
        "tab_pks",
        "tab_size",
    ):
        assert not hasattr(db, name)


def test_database_instances_with_same_spec_share_engine_pool(tmp_path):
    db1 = hb.Database(provider="sqlite", database=_db(tmp_path, "shared.db"))
    db2 = hb.Database(provider="sqlite", database=_db(tmp_path, "shared.db"))

    assert db1.engine is db2.engine
    assert db1.engine.pool is db2.engine.pool
    db1.execute("CREATE TABLE shared_items (id INTEGER PRIMARY KEY)")
    db1.execute("INSERT INTO shared_items (id) VALUES (1)")
    assert db2.execute("SELECT count(*) AS n FROM shared_items", readonly=True).scalar() == 1


@pytest.mark.parametrize("pool_class", ["assertion", "singleton", "static"])
def test_database_keeps_an_engine_that_serves_one_thread_at_a_time_private_to_its_facade(tmp_path, pool_class):
    # Each facade's owner serializes only its own statements, so a shared singleton engine let one owner's threads
    # close connections another owner was still running statements on.
    db1 = hb.Database(provider="sqlite", database=_db(tmp_path, "private.db"), pool={"pool_class": pool_class})
    db2 = hb.Database(provider="sqlite", database=_db(tmp_path, "private.db"), pool={"pool_class": pool_class})

    assert db1.spec.serialized and db1.engine is not db2.engine
    db1.execute("CREATE TABLE private_items (id INTEGER PRIMARY KEY)")
    db1.execute("INSERT INTO private_items (id) VALUES (1)")
    assert db2.execute("SELECT count(*) AS n FROM private_items", readonly=True).scalar() == 1


def test_database_accepts_one_public_keyword_only_captured_spec(tmp_path):
    from inspect import Parameter, signature

    engine = hb.DBEngine(config={"local_root": hb.utils.path_to_file_uri(tmp_path)})
    spec = engine.resolve({"provider": "sqlite", "database": "captured.db"})

    database = hb.Database(spec=spec)

    assert signature(hb.Database).parameters["spec"].kind is Parameter.KEYWORD_ONLY
    assert database.spec is spec
    assert database.clone().spec is spec
    with pytest.raises(TypeError, match="must be a DBSpec"):
        hb.Database(spec={})
    with pytest.raises(ValueError, match="cannot be combined with: provider"):
        hb.Database(provider="sqlite", spec=spec)
    with pytest.raises(ValueError, match="cannot be combined with: config_engine"):
        hb.Database(spec=spec, config_engine=engine)


def test_database_normalizes_file_alias_database_path(tmp_path):
    engine = hb.DBEngine(config={"local_root": hb.utils.path_to_file_uri(tmp_path)})
    spec = engine.resolve({"provider": "sqlite", "database": "resolved.db"})

    assert spec.database == hb.utils.path_to_file_uri(tmp_path / "resolved.db")


def test_sqlite_memory_database_shares_one_static_connection_pool():
    db = hb.Database(provider="sqlite", database=":memory:")

    conn1 = db.engine.connect()
    conn2 = db.engine.connect()
    try:
        with conn1.begin():
            conn1.execute(text("CREATE TABLE shared_items (id INTEGER PRIMARY KEY)"))
            conn1.execute(text("INSERT INTO shared_items (id) VALUES (1)"))
        assert conn2.execute(text("SELECT count(*) FROM shared_items")).scalar() == 1
    finally:
        conn2.close()
        conn1.close()


def test_database_default_and_logical_name_follow_local_root(tmp_path):
    engine = hb.DBEngine(config={"local_root": hb.utils.path_to_file_uri(tmp_path)})

    default = engine.resolve()
    logical = engine.resolve({"database": "my-database"})
    facade = hb.Database("my-facade", preset="default", config_engine=engine)

    assert default.database == hb.utils.path_to_file_uri(tmp_path / "default.db")
    assert logical.database == hb.utils.path_to_file_uri(tmp_path / "my-database")
    assert default.preset == "default"
    assert default.provider == "sqlite"
    assert facade.spec.preset == "default"
    assert facade.spec.provider == "sqlite"
    assert facade.spec.database == hb.utils.path_to_file_uri(tmp_path / "my-facade")
    assert not default.in_memory


def test_database_preset_preloads_its_dialect_from_a_cold_catalog(monkeypatch):
    from heavenbase.database.dialect.spec import DatabaseDialectSpec

    dialect = "preset-cold-test"
    loaded = []
    original_get = database_dialects.get

    def load_dialect(name, *, resolver=None):
        loaded.append(name)
        if name == dialect:
            database_dialects.register(DatabaseDialectSpec(dialect, requires_host=False), replace=True)
        return original_get(name, resolver=resolver)

    monkeypatch.setattr(database_dialects, "get", load_dialect)
    engine = hb.DBEngine(
        config={
            "default_provider": "sqlite",
            "providers": {
                "sqlite": {"dialect": "sqlite"},
                "preset-provider": {"dialect": dialect},
            },
            "presets": {"cold": {"provider": "preset-provider", "database": "app"}},
        },
        manager=None,
    )

    spec = engine.resolve({"preset": "cold"})

    assert (spec.provider, spec.dialect) == ("preset-provider", dialect)
    assert loaded == [dialect]


def test_default_database_providers_cover_every_concrete_sql_backend():
    providers = hb.CM_HVNB.get("heavenbase.db.providers")
    assert {
        "sqlite",
        "duckdb",
        "postgres",
        "supabase",
        "mysql",
        "seekdb",
        "dolt",
        "oceanbase",
        "mssql",
        "oracle",
        "starrocks",
        "trino",
        "clickhouse",
    } <= set(providers)


def test_database_url_normalization_precedence_and_round_trip(tmp_path, monkeypatch):
    engine = hb.DBEngine(config={"local_root": hb.utils.path_to_file_uri(tmp_path)})
    path = tmp_path / "space # percent% query? 雪.db"

    spec = engine.resolve({"provider": "sqlite", "database": hb.utils.path_to_file_uri(path), "params": {"mode": "rw", "tag": ["a", "b"]}})
    url = engine.apply(spec, profile="url")
    uri = engine.apply(spec, profile="uri")
    restored = engine.resolve({"url": uri})

    assert url.database == str(path)
    assert restored == spec
    assert restored.params["tag"] == ("a", "b")
    assert engine.assert_idempotent({"url": uri}) == restored

    executable = hb.DBSpec.from_dict(spec.to_dict())
    with monkeypatch.context() as isolated:
        isolated.setattr(database_dialects, "get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError(args[0])))
        assert engine.apply(executable, profile="url") == url

    relative = engine.resolve({"url": "sqlite:///nested/app.db", "database": "explicit.db"})
    assert relative.database == hb.utils.path_to_file_uri(tmp_path / "explicit.db")


def test_database_strict_local_input_and_memory_resource_identity(tmp_path):
    engine = hb.DBEngine(config={"local_root": hb.utils.path_to_file_uri(tmp_path)})

    for token in (str(tmp_path / "bare.db"), "nested/bare.db", "file:/tmp/legacy.db", "file://&/readonly.db"):
        with pytest.raises(ValueError):
            engine.resolve({"database": token})
    with pytest.raises(ValueError, match="Unknown database pool keys"):
        engine.resolve({"database": "pool.db", "pool": {"engine_cache_key": "legacy"}})
    with pytest.raises(ValueError, match="does not accept queue sizing"):
        engine.resolve({"database": "pool.db", "pool": {"pool_class": "static", "pool_size": 2}})

    first = hb.Database(database=":memory:")
    second = hb.Database(database=":memory:")
    assert first.spec == second.spec
    assert first.engine is not second.engine


def test_memory_database_never_inherits_a_collected_facade_engine(monkeypatch):
    import gc

    import heavenbase.database.database as database_module
    from heavenbase.database.registry import DatabaseEngineRegistry

    # CPython reuses a collected object's address; pin every facade to one address so the reuse is certain.
    monkeypatch.setattr(database_module, "id", lambda obj: 0x1, raising=False)
    first = hb.Database(provider="sqlite", database=":memory:")
    first.execute("CREATE TABLE leaked (id INTEGER PRIMARY KEY)")
    first.execute("INSERT INTO leaked (id) VALUES (1)")
    held, resource = first.engine, first._resource_id
    del first
    gc.collect()

    second = hb.Database(provider="sqlite", database=":memory:")
    assert second.execute("SELECT name FROM sqlite_master WHERE name = 'leaked'", readonly=True).column(0) == []
    assert not any(key.endswith(f":{resource}") for key in DatabaseEngineRegistry._engines)
    with held.connect() as conn:  # the registry forgets the engine without disposing it under a holder
        assert conn.execute(text("SELECT count(*) FROM leaked")).scalar() == 1


def test_database_scope_normalization_and_provider_inference():
    engine = hb.DBEngine()

    postgres = engine.resolve({"provider": "postgres", "database": "app", "schema": "tenant"})
    trino = engine.resolve({"provider": "trino", "database": "hive/analytics"})
    oracle = engine.resolve({"provider": "oracle", "database": "FREEPDB1", "username": "app", "schema": "tenant"})
    oracle_sid = engine.resolve({"url": "oracle+oracledb://system@localhost:1521/ORCL"})
    mysql = engine.resolve({"provider": "mysql", "schema": "app"})

    assert (postgres.database, postgres.schema) == ("app", "tenant")
    assert (trino.catalog, trino.schema, trino.database) == ("hive", "analytics", None)
    assert (oracle.service, oracle.sid, oracle.schema, oracle.username) == ("FREEPDB1", None, "tenant", "app")
    assert (oracle_sid.service, oracle_sid.sid) == (None, "ORCL")
    assert (mysql.database, mysql.schema) == ("app", None)
    with pytest.raises(ValueError, match="ambiguous"):
        engine.resolve({"url": "mysql+pymysql://root@localhost/app"})
    # supabase only changes PostgreSQL defaults, so a PostgreSQL URL names postgres unless supabase is explicit
    assert engine.resolve({"url": "postgresql+psycopg2://app@localhost/app"}).provider == "postgres"
    assert engine.resolve({"url": "postgresql://app@localhost/app"}).provider == "postgres"
    assert engine.resolve({"url": "postgresql://app@localhost/app", "provider": "supabase"}).provider == "supabase"


def test_database_print_profile_redacts_credentials_and_query_tokens():
    engine = hb.DBEngine()
    spec = engine.resolve(
        {
            "provider": "postgres",
            "database": "app",
            "username": "user",
            "password": "password-secret",
            "params": {"token": "query-secret", "sslmode": "require"},
        }
    )

    rendered = engine.apply(spec, profile="print")
    text = repr(rendered)
    assert "password-secret" not in text
    assert "query-secret" not in text
    assert "sslmode=require" in rendered["uri"]


def test_database_execute_many_args_and_orm_statements(tmp_path):
    from sqlalchemy import Column, Float, MetaData, String, Table, insert, select
    from sqlalchemy.schema import CreateTable

    db = hb.Database(provider="sqlite", database=_db(tmp_path, "orm.db"))
    meta = MetaData()
    products = Table("products", meta, Column("sku", String, primary_key=True), Column("name", String), Column("price", Float))

    db.orm_execute_many(
        [CreateTable(products), insert(products)], [None, [{"sku": "p1", "name": "Desk", "price": 10.0}, {"sku": "p2", "name": "Lamp", "price": 5.0}]]
    )
    db.execute_many(
        ["UPDATE products SET price = :price WHERE sku = :sku", "INSERT INTO products (sku, name, price) VALUES (:sku, :name, :price)"],
        [{"sku": "p2", "price": 7.0}, {"sku": "p3", "name": "Chair", "price": 12.0}],
    )

    result = db.orm_execute(select(products.c.sku, products.c.price).order_by(products.c.sku), readonly=True)

    assert result.to_lists() == [["p1", 10.0], ["p2", 7.0], ["p3", 12.0]]
    assert result.to_list("tuple", columns=["sku"]) == [("p1",), ("p2",), ("p3",)]
    assert result.scalar() == "p1"
    assert result.column("price") == [10.0, 7.0, 12.0]
    assert result[1, "sku"] == "p2"


def test_database_table_mutations_stats_schema_index_and_canonical_api(tmp_path):
    from sqlalchemy import Column, Integer, MetaData, String, Table

    db = hb.Database(provider="sqlite", database=_db(tmp_path, "features.db"))
    meta = MetaData()
    items = Table("items", meta, Column("sku", String, primary_key=True), Column("name", String), Column("score", Integer))

    db.create_table(items)
    db.execute(
        "INSERT INTO items (sku, name, score) VALUES (:sku, :name, :score)",
        [
            {"sku": "a", "name": "alpha", "score": 10},
            {"sku": "b", "name": "beta", "score": 20},
            {"sku": "c", "name": "gamma", "score": 30},
        ],
    )
    items.append_column(Column("note", String))
    db.add_table_col(items, items.c.note)
    db.execute("UPDATE items SET note = name")

    assert db.tables() == ["items"]
    assert db.columns("items") == ["sku", "name", "score", "note"]
    assert db.pks("items") == ["sku"]
    assert ("sku",) in db.index_column_sets(items)
    assert db.n_rows("items") == 3
    assert db.table_size("items") == 3
    assert db.col_percentile("items", "score", [0, 50, 100]) == {"p0": 10.0, "p50": 20.0, "p100": 30.0}
    assert db.col_lengths("items", "name")["max_len"] == 5
    assert db.sample("items", n=2, seed=7).shape == (2, 4)
    assert db.col_comments("items") == {"sku": None, "name": None, "score": None, "note": None}
    assert db.schema_index(refresh=True) == {"items": ["sku", "name", "score", "note"]}

    db.clear_table("items")
    assert db.n_rows("items") == 0
    db.drop_table("items")
    assert db.tables() == []


def test_database_add_table_col_uses_dialect_metadata(tmp_path, monkeypatch):
    from sqlalchemy import Column, Integer, MetaData, Table

    db = hb.Database(provider="sqlite", database=_db(tmp_path, "add-column.db"))
    items = Table("items", MetaData(), Column("enabled", Integer, nullable=False, server_default=text("0")))
    calls = []
    monkeypatch.setattr(db, "columns", lambda _table, schema=None: [])
    monkeypatch.setattr(db, "execute", lambda sql, params=None, readonly=False: calls.append((sql, params, readonly)))
    monkeypatch.setattr(db, "invalidate_schema_index", lambda: None)

    for dialect in ("sqlite", "mssql", "oracle"):
        _bind_test_dialect(db, dialect)
        db.add_table_col(items, items.c.enabled)

    assert calls == [
        ("ALTER TABLE items ADD COLUMN enabled INTEGER DEFAULT 0 NOT NULL", None, False),
        ("ALTER TABLE items ADD enabled INTEGER DEFAULT 0 NOT NULL", None, False),
        ("ALTER TABLE items ADD (enabled INTEGER DEFAULT 0 NOT NULL)", None, False),
    ]


def test_database_default_readonly_false_and_explicit_guard(tmp_path):
    db = hb.Database(provider="sqlite", database=_db(tmp_path, "readonly.db"))
    db.execute("CREATE TABLE items (id INTEGER)")

    db.execute("WITH input(id) AS (VALUES (1)) INSERT INTO items SELECT id FROM input")

    assert db.n_rows("items") == 1
    assert db.execute("WITH input(id) AS (SELECT 2) SELECT id FROM input", readonly=True).scalar() == 2
    assert db.execute("WITH input(id) AS (SELECT 3) SELECT id FROM input", readonly=None).scalar() == 3
    with pytest.raises(Exception, match="readonly=True"):
        db.execute("WITH input(id) AS (VALUES (2)) INSERT INTO items SELECT id FROM input", readonly=True)


def test_database_execute_normalizes_common_placeholder_styles():
    db = hb.Database(provider="sqlite", database=":memory:")

    assert db.execute("SELECT ? AS x", (1,), readonly=True).scalar() == 1
    assert db.execute("SELECT %s AS x", (2,), readonly=True).scalar() == 2
    assert db.execute("SELECT %(x)s AS x", {"x": 3}, readonly=True).scalar() == 3
    assert db.execute("SELECT $x AS x", {"x": 4}, readonly=True).scalar() == 4
    assert db.execute("SELECT $1 AS x", (5,), readonly=True).scalar() == 5

    db.execute("CREATE TABLE items (id INTEGER, name TEXT)")
    db.execute("INSERT INTO items (id, name) VALUES (?, ?)", [(1, "alpha"), (2, "beta")])

    assert db.execute("SELECT name FROM items WHERE id = ?", (2,), readonly=True).scalar() == "beta"


def test_sql_processor_preserves_postgres_qmark_operators_and_casts():
    from heavenbase.database import SQLProcessor

    processor = SQLProcessor("postgresql")

    assert processor.normalize_params("SELECT ?::int AS x", (1,)) == ("SELECT :param_0::int AS x", {"param_0": 1})
    assert processor.normalize_params("SELECT :x::int AS x", {"x": 1}) == ("SELECT :x::int AS x", {"x": 1})
    assert processor.normalize_params("SELECT data ? 'key' AS has_key")[0] == "SELECT data ? 'key' AS has_key"
    assert processor.normalize_params("SELECT data ?| array['a', 'b'] AS has_any")[0] == "SELECT data ?| array['a', 'b'] AS has_any"
    assert processor.normalize_params("SELECT data ? 'key' FROM items WHERE id = ?", (7,)) == (
        "SELECT data ? 'key' FROM items WHERE id = :param_0",
        {"param_0": 7},
    )


def test_sql_text_helpers_route_through_public_facade():
    import heavenbase.database.sql_text as sql_text_api
    from heavenbase.database import compare_sqls, escape_sql_binds, strip_sql_comments, validate_sql

    assert strip_sql_comments("SELECT 1 -- trailing\n/* hidden */\n") == "SELECT 1"
    assert escape_sql_binds("SELECT :id::int") == r"SELECT \:id::int"
    assert compare_sqls("select 1", "SELECT 1")
    assert validate_sql("select 1")
    assert strip_sql_comments is sql_text_api.strip_sql_comments


def test_database_safe_failure_rolls_back_transaction_context(tmp_path):
    db = hb.Database(provider="sqlite", database=_db(tmp_path, "safe_tx.db"))
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")

    with db.tx() as tx:
        tx.execute("INSERT INTO items (id) VALUES (1)")
        resp = tx.execute("INSERT INTO missing_items (id) VALUES (2)", safe=True)
        assert resp.ok is False

    assert db.n_rows("items") == 0


def test_database_safe_failure_can_be_handled_with_explicit_rollback(tmp_path):
    db = hb.Database(provider="sqlite", database=_db(tmp_path, "safe_recover.db"))
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")

    with db.tx() as tx:
        tx.execute("INSERT INTO items (id) VALUES (1)")
        resp = tx.execute("INSERT INTO missing_items (id) VALUES (2)", safe=True)
        assert resp.ok is False
        tx.rollback()
        tx.execute("INSERT INTO items (id) VALUES (3)")

    assert db.execute("SELECT id FROM items", readonly=True).to_lists() == [[3]]


def test_database_execute_many_safe_rolls_back_failed_batch(tmp_path):
    db = hb.Database(provider="sqlite", database=_db(tmp_path, "safe_many.db"))
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")

    res = db.execute_many(["INSERT INTO items (id) VALUES (1)", "INSERT INTO missing_items (id) VALUES (2)"], safe=True)

    assert [resp.ok for resp in res] == [True, False]
    assert db.n_rows("items") == 0


def test_database_execute_many_safe_autocommit_is_best_effort(tmp_path):
    db = hb.Database(provider="sqlite", database=_db(tmp_path, "safe_autocommit.db"))
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")

    res = db.execute_many(
        ["INSERT INTO items (id) VALUES (1)", "INSERT INTO missing_items (id) VALUES (2)", "INSERT INTO items (id) VALUES (3)"],
        safe=True,
        autocommit=True,
    )

    assert [resp.ok for resp in res] == [True, False, True]
    assert db.execute("SELECT id FROM items ORDER BY id", readonly=True).to_lists() == [[1], [3]]


def test_database_tx_readonly_none_autodetects(tmp_path):
    db = hb.Database(provider="sqlite", database=_db(tmp_path, "tx_readonly.db"))
    db.execute("CREATE TABLE items (id INTEGER)")

    with db.tx(readonly=None) as tx:
        assert tx.in_transaction() is True
        assert tx.execute("SELECT count(*) AS n FROM items").scalar() == 0
        tx.execute("INSERT INTO items (id) VALUES (1)")

    assert db.in_transaction() is False
    assert db.n_rows("items") == 1


def test_schema_index_normalization_accepts_cache_shapes():
    from heavenbase.database.schema_index import normalize_schema_index

    assert normalize_schema_index(
        {
            "tables": {
                "items": {"sku": {}, "name": {}},
                "events": ["id", "id", "ts", ""],
                "single": "id",
                "empty": None,
                " ": ["ignored"],
            }
        }
    ) == {"items": ["sku", "name"], "events": ["id", "ts"], "single": ["id"], "empty": []}

    with pytest.raises(ValueError, match="schema_index"):
        normalize_schema_index(["items"])


def test_engine_registry_dispose_during_create_is_not_cached(tmp_path, monkeypatch):
    import heavenbase.database.engine as engine_api
    from heavenbase.database.registry import DatabaseEngineRegistry

    db = hb.Database(provider="sqlite", database=_db(tmp_path, "race.db"))
    spec = db.spec
    original = engine_api.create_database_engine

    def create_then_dispose(spec, autocreate=True):
        engine = original(spec, autocreate=autocreate)
        DatabaseEngineRegistry.dispose(spec)
        return engine

    monkeypatch.setattr(engine_api, "create_database_engine", create_then_dispose)

    with pytest.raises(RuntimeError, match="disposed"):
        DatabaseEngineRegistry.get(spec)

    assert not DatabaseEngineRegistry.has_engine(spec)


def test_builtin_sql_backends_register_database_dialect_metadata():
    from heavenbase.database.dialect.registry import (
        MYSQL_TEXT_MIN_LEN,
        ORACLE_VARCHAR_MAX,
    )

    assert database_dialects.get("sqlite").supports("database-blob-native") is True
    assert database_dialects.get("postgresql").supports("database-json-native") is True
    assert database_dialects.get("postgresql").supports("database-blob-native") is True
    assert database_dialects.get("mysql").text_min_len == MYSQL_TEXT_MIN_LEN
    assert database_dialects.get("oracle").varchar_max == ORACLE_VARCHAR_MAX
    assert database_dialects.get("trino").supports("database-json-native") is True
    assert database_dialects.get("trino").supports("database-blob-native") is False
    assert database_dialects.get("sqlite").supports("database-create") is True
    assert database_dialects.get("sqlite").supports("database-drop") is True
    assert database_dialects.get("postgresql").supports("database-create") is True
    assert database_dialects.get("postgresql").supports("database-drop") is True
    assert database_dialects.get("postgres").supports("database-create") is True
    assert database_dialects.get("postgres").supports("database-drop") is True
    assert database_dialects.get("hive").supports("database-create") is True
    assert database_dialects.get("trino").supports("database-drop") is False
    assert database_dialects.get("postgresql").requires_host is True
    assert database_dialects.get("sqlite").requires_host is False
    assert database_dialects.get("duckdb").requires_host is False
    assert database_dialects.get("trino").requires_host is False
    assert database_dialects.superuser_database("postgresql", "shop") == "postgres"
    assert database_dialects.superuser_database("mysql", "shop") == "mysql"
    assert database_dialects.superuser_database("mssql", "shop") == "master"
    assert database_dialects.superuser_database("hive", "shop") == "default"
    assert database_dialects.superuser_database("oracle", "shop") == "shop"
    assert database_dialects.superuser_database("starrocks", "shop") is None
    assert database_dialects.to_sqlglot("postgresql") == "postgres"
    assert database_dialects.to_sqlglot("postgres") == "postgres"
    assert database_dialects.to_sqlglot("mariadb") == "mysql"
    assert database_dialects.to_sqlglot("mssql") == "tsql"
    assert database_dialects.to_sqlglot("starrocks") == "mysql"
    assert database_dialects.from_sqlglot("postgres") == "postgresql"
    assert database_dialects.from_sqlglot("mysql") == "mysql"
    assert database_dialects.from_sqlglot("tsql") == "mssql"
    assert database_dialects.get("sqlite").current_database_sql == "PRAGMA database_list"
    assert database_dialects.get("postgres").current_database_sql == "SELECT current_database() AS name"
    assert database_dialects.get("mariadb").current_database_sql == "SELECT DATABASE() AS name"
    assert database_dialects.get("postgresql").list_databases_sql == "SELECT datname AS name FROM pg_database"
    assert database_dialects.get("mysql").list_databases_sql == "SELECT SCHEMA_NAME AS name FROM INFORMATION_SCHEMA.SCHEMATA"
    assert database_dialects.get("sqlite").list_databases_sql is None
    assert database_dialects.get("sqlite").static_database_names == ("main",)
    sqlite = database_dialects.get("sqlite")
    assert sqlite.retryable_error_messages == ("database is locked", "database table is locked", "database schema is locked")
    assert sqlite.is_retryable_error(RuntimeError("database table is locked during write")) is True
    assert sqlite.is_retryable_error(RuntimeError("permission denied")) is False
    duckdb = database_dialects.get("duckdb")
    assert duckdb.column_reflection_sql == "DESCRIBE {table_name}"
    # the key columns in declared order come from duckdb_constraints(); PRAGMA table_info lists them in column order
    assert "constraint_column_names" in duckdb.primary_key_reflection_sql and "ORDER BY ordinal" in duckdb.primary_key_reflection_sql
    assert duckdb.empty_foreign_keys_on_reflection_error is True
    postgresql = database_dialects.get("postgresql")
    assert postgresql.database_exists_sql() == "SELECT 1 FROM pg_database WHERE datname = :name"
    assert postgresql.create_database_statement(_db_spec(provider="postgresql", database="shop"), "shop") == 'CREATE DATABASE "shop"'
    assert postgresql.before_drop_force_statements("shop", force=True) == [
        ("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :name AND pid <> pg_backend_pid()", {"name": "shop"})
    ]
    assert postgresql.after_create_statements(_db_spec(provider="postgresql", database="shop", username="app", superuser={"username": "postgres"}), "shop") == [
        'ALTER DATABASE "shop" OWNER TO "app"',
        'GRANT ALL PRIVILEGES ON DATABASE "shop" TO "app"',
    ]
    mysql = database_dialects.get("mysql")
    assert mysql.create_database_statement(_db_spec(provider="mysql", database="shop"), "shop") == "CREATE DATABASE `shop`"
    assert database_dialects.get("mssql").before_drop_force_statements("shop", force=True) == [
        ("ALTER DATABASE [shop] SET SINGLE_USER WITH ROLLBACK IMMEDIATE", None)
    ]
    oracle = database_dialects.get("oracle")
    assert oracle.requires_host is False
    assert oracle.url_database_omit_params == ("service_name",)
    assert oracle.url_database({"database": "freepdb1", "params": {"service_name": "freepdb1"}}) is None
    assert oracle.url_database({"database": "freepdb1", "params": {}}) == "freepdb1"
    oracle_spec = _db_spec(provider="oracle", database="freepdb1", username="app")
    assert oracle.lifecycle_namespace(oracle_spec) == "app"
    assert oracle.create_database_statement(oracle_spec, "app") == 'CREATE USER "APP" IDENTIFIED BY "app_pw"'
    quoted_password_spec = _db_spec(provider="oracle", database="freepdb1", username="app", password='pa"ss')
    assert oracle.create_database_statement(quoted_password_spec, "app") == 'CREATE USER "APP" IDENTIFIED BY "pa""ss"'
    assert oracle.after_create_statements(oracle_spec, "app") == [
        'GRANT CREATE SESSION TO "APP"',
        'GRANT CREATE TABLE TO "APP"',
        'GRANT CREATE SEQUENCE TO "APP"',
        'GRANT CREATE VIEW TO "APP"',
        'GRANT UNLIMITED TABLESPACE TO "APP"',
    ]
    sql, params = database_dialects.get("sqlite").rename_table_statement("old_table", "new_table", lambda name: f"`{name}`")
    assert sql == "ALTER TABLE `old_table` RENAME TO `new_table`"
    assert params is None
    sql, params = database_dialects.get("mssql").rename_table_statement("old_table", "new_table", lambda name: f"[{name}]")
    assert sql == "EXEC sp_rename :old_name, :new_name"
    assert params == {"old_name": "old_table", "new_name": "new_table"}
    assert database_dialects.get("sqlite").add_column_statement("items", "note TEXT") == "ALTER TABLE items ADD COLUMN note TEXT"
    assert database_dialects.get("mssql").add_column_statement("[items]", "[note] TEXT") == "ALTER TABLE [items] ADD [note] TEXT"
    assert database_dialects.get("oracle").add_column_statement('"ITEMS"', '"NOTE" VARCHAR2(10)') == 'ALTER TABLE "ITEMS" ADD ("NOTE" VARCHAR2(10))'


@pytest.mark.fast
def test_bootstrap_database_dialect_catalog_is_explicit_and_resolver_independent(monkeypatch):
    from importlib import import_module

    registry = import_module("heavenbase.database.dialect.registry")
    catalog = BootstrapDialectCatalog(
        {
            "bootstrap": {
                "requires_host": False,
                "name_warning_max_identifier_length": 17,
                "name_warning_reserved_words": ["outer-word"],
            }
        }
    )

    def fail_if_selected(resolver=None):
        del resolver
        raise AssertionError("bootstrap catalog must not select a module resolver")

    monkeypatch.setattr(registry, "module_resolver", fail_if_selected)

    dialect = catalog.get("bootstrap")
    assert dialect.identifier == "bootstrap"
    assert dialect.name_warning_rules() == (17, {"outer-word"})
    with pytest.raises(LookupError, match="absent from the bootstrap catalog"):
        catalog.get("missing")


def test_builtin_database_dialect_metadata_does_not_import_provider_modules():
    script = """
import json
import sys
from tempfile import TemporaryDirectory

import heavenbase as hb
import heavenbase.context.runtime as context_runtime
from heavenbase.utils.config import ConfigManager

temporary_root = TemporaryDirectory()
context = hb.Context.load(
    {
        "version": 1,
        "root": temporary_root.name,
        "workspace": "default",
        "backend": {
            "type": "inmem",
            "module": "heavenbase.backends.inmem.backend",
            "name": "system",
        },
        "registry": {},
    },
    config=ConfigManager(default={}, setup=False),
)
context_runtime.set_default_context(context)
context._setup_modules(force=True)

ext = hb.ext
provider_modules = {
    "heavenbase.backends.sql.duckdb",
    "heavenbase.backends.sql.mssql",
    "heavenbase.backends.sql.mysql",
    "heavenbase.backends.sql.oracle",
    "heavenbase.backends.sql.postgres",
    "heavenbase.backends.sql.sqlite",
    "heavenbase.backends.sql.starrocks",
    "heavenbase.backends.sql.trino",
    "heavenbase.backends.vector.pgvector",
}
loaded_provider_modules_before = sorted(name for name in sys.modules if name in provider_modules)
data = {
    "loaded_provider_modules_before": loaded_provider_modules_before,
    "mysql_text_min_len": ext.database_dialects.get("mysql").text_min_len,
    "oracle_varchar_max": ext.database_dialects.get("oracle").varchar_max,
    "postgresql_create": ext.database_dialects.get("postgresql").supports("database-create"),
    "postgresql_drop": ext.database_dialects.get("postgresql").supports("database-drop"),
    "postgresql_blob": ext.database_dialects.get("postgresql").supports("database-blob-native"),
    "postgresql_json": ext.database_dialects.get("postgresql").supports("database-json-native"),
    "sqlite_create": ext.database_dialects.get("sqlite").supports("database-create"),
    "sqlite_drop": ext.database_dialects.get("sqlite").supports("database-drop"),
    "sqlite_blob": ext.database_dialects.get("sqlite").supports("database-blob-native"),
    "sqlite_requires_host": ext.database_dialects.get("sqlite").requires_host,
    "sqlite_retryable_error_messages": ext.database_dialects.get("sqlite").retryable_error_messages,
    "duckdb_requires_host": ext.database_dialects.get("duckdb").requires_host,
    "trino_drop": ext.database_dialects.get("trino").supports("database-drop"),
    "trino_blob": ext.database_dialects.get("trino").supports("database-blob-native"),
    "trino_json": ext.database_dialects.get("trino").supports("database-json-native"),
    "trino_requires_host": ext.database_dialects.get("trino").requires_host,
    "postgresql_superuser_database": ext.database_dialects.superuser_database("postgresql", "shop"),
    "postgresql_requires_host": ext.database_dialects.get("postgresql").requires_host,
    "postgresql_sqlglot": ext.database_dialects.to_sqlglot("postgresql"),
    "postgres_alias_sqlglot": ext.database_dialects.to_sqlglot("postgres"),
    "postgresql_current_database_sql": ext.database_dialects.get("postgresql").current_database_sql,
    "postgresql_list_databases_sql": ext.database_dialects.get("postgresql").list_databases_sql,
    "mysql_superuser_database": ext.database_dialects.superuser_database("mysql", "shop"),
    "mariadb_sqlglot": ext.database_dialects.to_sqlglot("mariadb"),
    "mysql_current_database_sql": ext.database_dialects.get("mysql").current_database_sql,
    "mysql_list_databases_sql": ext.database_dialects.get("mysql").list_databases_sql,
    "oracle_superuser_database": ext.database_dialects.superuser_database("oracle", "shop"),
    "oracle_requires_host": ext.database_dialects.get("oracle").requires_host,
    "oracle_url_database_omit_params": ext.database_dialects.get("oracle").url_database_omit_params,
    "starrocks_superuser_database": ext.database_dialects.superuser_database("starrocks", "shop"),
    "starrocks_sqlglot": ext.database_dialects.to_sqlglot("starrocks"),
    "sqlite_static_database_names": ext.database_dialects.get("sqlite").static_database_names,
    "duckdb_column_reflection_sql": ext.database_dialects.get("duckdb").column_reflection_sql,
    "duckdb_primary_key_reflection_sql": "constraint_column_names" in ext.database_dialects.get("duckdb").primary_key_reflection_sql,
    "duckdb_empty_foreign_keys_on_reflection_error": ext.database_dialects.get("duckdb").empty_foreign_keys_on_reflection_error,
    "mssql_rename_table_sql": ext.database_dialects.get("mssql").rename_table_sql,
    "mssql_rename_table_bind_params": ext.database_dialects.get("mssql").rename_table_bind_params,
    "mssql_add_column_sql": ext.database_dialects.get("mssql").add_column_sql,
    "oracle_add_column_sql": ext.database_dialects.get("oracle").add_column_sql,
    "mssql_force_drop": ext.database_dialects.get("mssql").before_drop_force_statements("shop", force=True),
    "oracle_namespace_source": ext.database_dialects.get("oracle").lifecycle_namespace_source,
    "postgresql_force_drop_key": ext.database_dialects.get("postgresql").before_drop_force_sql_key,
    "tsql_sa": ext.database_dialects.from_sqlglot("tsql"),
}
data["loaded_provider_modules_after"] = sorted(name for name in sys.modules if name in provider_modules)
print(json.dumps(data, sort_keys=True))
"""
    result = cmd([sys.executable, "-c", script], include="out", check=True)
    assert loads_json(result) == {
        "loaded_provider_modules_before": [],
        "loaded_provider_modules_after": [],
        "mysql_text_min_len": hb.ext.MYSQL_TEXT_MIN_LEN,
        "oracle_varchar_max": hb.ext.ORACLE_VARCHAR_MAX,
        "postgresql_create": True,
        "postgresql_drop": True,
        "postgresql_blob": True,
        "postgresql_json": True,
        "sqlite_create": True,
        "sqlite_drop": True,
        "sqlite_blob": True,
        "sqlite_requires_host": False,
        "sqlite_retryable_error_messages": ["database is locked", "database table is locked", "database schema is locked"],
        "duckdb_requires_host": False,
        "trino_drop": False,
        "trino_blob": False,
        "trino_json": True,
        "trino_requires_host": False,
        "postgresql_superuser_database": "postgres",
        "postgresql_requires_host": True,
        "postgresql_sqlglot": "postgres",
        "postgres_alias_sqlglot": "postgres",
        "postgresql_current_database_sql": "SELECT current_database() AS name",
        "postgresql_list_databases_sql": "SELECT datname AS name FROM pg_database",
        "mysql_superuser_database": "mysql",
        "mariadb_sqlglot": "mysql",
        "mysql_current_database_sql": "SELECT DATABASE() AS name",
        "mysql_list_databases_sql": "SELECT SCHEMA_NAME AS name FROM INFORMATION_SCHEMA.SCHEMATA",
        "oracle_superuser_database": "shop",
        "oracle_requires_host": False,
        "oracle_url_database_omit_params": ["service_name"],
        "starrocks_superuser_database": None,
        "starrocks_sqlglot": "mysql",
        "sqlite_static_database_names": ["main"],
        "duckdb_column_reflection_sql": "DESCRIBE {table_name}",
        "duckdb_primary_key_reflection_sql": True,
        "duckdb_empty_foreign_keys_on_reflection_error": True,
        "mssql_rename_table_sql": "EXEC sp_rename :old_name, :new_name",
        "mssql_rename_table_bind_params": True,
        "mssql_add_column_sql": "ALTER TABLE {table} ADD {column}",
        "oracle_add_column_sql": "ALTER TABLE {table} ADD ({column})",
        "mssql_force_drop": [["ALTER DATABASE [shop] SET SINGLE_USER WITH ROLLBACK IMMEDIATE", None]],
        "oracle_namespace_source": "schema_or_username",
        "postgresql_force_drop_key": "postgresql",
        "tsql_sa": "mssql",
    }


def test_database_text_type_uses_oracle_clob_above_varchar_limit():
    import sqlalchemy as sa
    from sqlalchemy.dialects import oracle

    from heavenbase.database.dialect.registry import ORACLE_VARCHAR_MAX
    from heavenbase.database.types import DatabaseTextType

    assert database_dialects.get("oracle").varchar_max == ORACLE_VARCHAR_MAX
    dialect = oracle.dialect()
    table = sa.Table("items", sa.MetaData(), sa.Column("body", DatabaseTextType(length=4095)))

    ddl = str(sa.schema.CreateTable(table).compile(dialect=dialect))

    assert "body CLOB" in ddl


def test_database_text_type_uses_mssql_max_above_varchar_limit():
    import sqlalchemy as sa
    from sqlalchemy.dialects import mssql

    from heavenbase.database.types import DatabaseTextType

    table = sa.Table("items", sa.MetaData(), sa.Column("body", DatabaseTextType(length=65535)))
    ddl = str(sa.schema.CreateTable(table).compile(dialect=mssql.dialect()))

    assert database_dialects.get("mssql").varchar_max == 8000
    assert "body VARCHAR(max)" in ddl


def test_database_sqlalchemy_type_shapes_are_registry_backed():
    import sqlalchemy.types as sa_types
    import sqlalchemy.dialects.mysql as sa_mysql

    from heavenbase.database.dialect.registry import (
        DatabaseDialectSpec,
        SQL_TYPE_FLOAT_ARRAY,
        SQL_TYPE_JSON,
        SQL_TYPE_MYSQL_TEXT,
    )
    from heavenbase.database.types import DatabaseBlobType, DatabaseJsonType, DatabaseVectorType

    class Dialect:
        name = "customtypes"

        def type_descriptor(self, value):
            return value

    database_dialects.register(
        DatabaseDialectSpec(
            "customtypes",
            tags={"database-json-native": True, "database-blob-native": False},
            blob_text_type=SQL_TYPE_MYSQL_TEXT,
            json_native_type=SQL_TYPE_JSON,
            vector_native_type=SQL_TYPE_FLOAT_ARRAY,
        ),
        replace=True,
    )
    dialect = Dialect()

    assert isinstance(DatabaseBlobType().load_dialect_impl(dialect), sa_mysql.TEXT)
    assert isinstance(DatabaseJsonType().load_dialect_impl(dialect), sa_types.JSON)
    vector = DatabaseVectorType(dialect_spec=database_dialects.get("customtypes"))
    assert isinstance(vector.load_dialect_impl(dialect), sa_types.ARRAY)
    assert vector.process_bind_param([1, 2], dialect) == [1.0, 2.0]


def test_database_json_text_fallback_is_compact_and_config_independent(monkeypatch):
    """Physical JSON text should not inherit presentation indentation or resolve config per cell."""
    from sqlalchemy.dialects import sqlite

    from heavenbase.database.types import DatabaseJsonType
    from heavenbase.utils.serialize import CM_HVNB

    def reject_config_lookup(*_args, **_kwargs):
        raise AssertionError("database JSON serialization resolved presentation config")

    monkeypatch.setitem(vars(CM_HVNB), "get", reject_config_lookup)

    value = {"items": [1, 2], "nested": {"enabled": True}}
    assert DatabaseJsonType().process_bind_param(value, sqlite.dialect()) == '{"items": [1, 2], "nested": {"enabled": true}}'


def test_database_dialect_physical_tags_are_registry_backed():
    from heavenbase.database.dialect.registry import (
        DatabaseDialectSpec,
        SQL_TYPE_CLOB,
        SQL_TYPE_MYSQL_TEXT,
        SQL_TYPE_TEXT,
    )
    from heavenbase.database import sa_dialect_to_sqlglot, sqlglot_dialect_to_sa

    spec = database_dialects.register(
        DatabaseDialectSpec(
            "customdialect",
            tags={"database-json-native": True, "database-blob-native": False, "database-create": True, "database-drop": False},
            varchar_max=128,
            text_min_len=512,
            superuser_database="admin",
            sqlglot_dialect="spark",
            sqlalchemy_aliases=("customalias",),
            current_database_sql="SELECT active_database AS name FROM sys_context",
            list_databases_sql="SELECT name FROM sys_databases",
            static_database_names=("local",),
            ddl_resource_key="mysql",
            identifier_quote_style="backtick",
            requires_host=False,
            url_database_omit_params=("catalog",),
            retryable_error_messages=("busy now",),
            rename_table_sql="ALTER TABLE {old_name} RENAME TO {new_name}",
            add_column_sql="ALTER TABLE {table} ADD CUSTOM {column}",
            before_drop_force_sql_key="mssql",
            before_drop_force_mode="quoted_database",
            column_reflection_sql="PRAGMA table_info({table_name_literal})",
            primary_key_reflection_sql="PRAGMA table_info({table_name_literal})",
            empty_foreign_keys_on_reflection_error=True,
            varchar_overflow_type=SQL_TYPE_CLOB,
            text_threshold_type=SQL_TYPE_MYSQL_TEXT,
            blob_text_type=SQL_TYPE_TEXT,
            json_text_type=SQL_TYPE_TEXT,
            name_warning_dialect_factory=("sqlalchemy.dialects.sqlite", "dialect"),
            name_warning_reserved_words=("select", "customword"),
            name_warning_max_identifier_length=32,
        ),
        replace=True,
    )

    assert database_dialects.get("customdialect") is spec
    assert database_dialects.get("customalias") is spec
    assert database_dialects.get("customdialect").supports("database-json-native") is True
    assert database_dialects.get("customdialect").supports("database-blob-native") is False
    assert database_dialects.get("customdialect").supports("database-create") is True
    assert database_dialects.get("customdialect").supports("database-drop") is False
    assert database_dialects.get("customdialect").varchar_max == 128
    assert database_dialects.get("customdialect").text_min_len == 512
    assert database_dialects.superuser_database("customdialect", "shop") == "admin"
    assert database_dialects.to_sqlglot("customdialect") == "spark"
    assert database_dialects.to_sqlglot("customalias") == "spark"
    assert database_dialects.from_sqlglot("spark") == "customdialect"
    assert database_dialects.get("customalias").current_database_sql == "SELECT active_database AS name FROM sys_context"
    assert database_dialects.get("customdialect").list_databases_sql == "SELECT name FROM sys_databases"
    assert database_dialects.get("customdialect").static_database_names == ("local",)
    assert spec.requires_host is False
    assert spec.requires_host_for_url() is False
    assert spec.url_database({"database": "shop", "params": {"catalog": "iceberg"}}) is None
    assert spec.url_database({"database": "shop", "params": {}}) == "shop"
    assert spec.is_retryable_error(RuntimeError("server is busy now")) is True
    assert spec.is_retryable_error(RuntimeError("permission denied")) is False
    assert spec.create_database_statement(_db_spec(provider="customdialect", database="customdb"), "customdb") == "CREATE DATABASE `customdb`"
    assert spec.before_drop_force_statements("customdb", force=True) == [("ALTER DATABASE `customdb` SET SINGLE_USER WITH ROLLBACK IMMEDIATE", None)]
    sql, params = spec.rename_table_statement("old", "new", lambda name: f"`{name}`")
    assert sql == "ALTER TABLE `old` RENAME TO `new`"
    assert params is None
    assert spec.add_column_statement("`items`", "`note` TEXT") == "ALTER TABLE `items` ADD CUSTOM `note` TEXT"
    assert spec.column_reflection_statement("items", None, lambda name: f"`{name}`", lambda name: f"`{name}`") == "PRAGMA table_info('items')"
    assert spec.primary_key_reflection_statement("items", "main", lambda name: f"`{name}`", lambda name: f"`{name}`") == "PRAGMA table_info('main.items')"
    assert spec.empty_foreign_keys_on_reflection_error is True
    assert spec.varchar_overflow_type == SQL_TYPE_CLOB
    assert spec.text_threshold_type == SQL_TYPE_MYSQL_TEXT
    assert spec.blob_text_type == SQL_TYPE_TEXT
    assert spec.json_text_type == SQL_TYPE_TEXT
    max_length, reserved = database_dialects.warning_rules("customalias")
    assert max_length == 32
    assert {"customword", "select"}.issubset(reserved)
    assert sa_dialect_to_sqlglot("customalias") == "spark"
    assert sqlglot_dialect_to_sa("spark") == "customdialect"
    assert database_dialects.sqlalchemy_map()["customalias"] == "spark"
    assert database_dialects.sqlglot_map()["spark"] == "customdialect"
    assert "customdialect" in database_dialects.snapshot()
    with pytest.raises(LookupError, match="Unknown database dialect 'unknownsql'"):
        database_dialects.get("unknownsql")


@pytest.mark.parametrize(
    "template",
    (
        "",
        "ALTER TABLE {table} ADD",
        "ALTER TABLE {table} ADD {column!r}",
        "ALTER TABLE {table} ADD {column",
    ),
)
def test_database_dialect_rejects_invalid_add_column_templates(template):
    from heavenbase.database.dialect.registry import DatabaseDialectSpec

    with pytest.raises(ValueError, match="add_column_sql"):
        DatabaseDialectSpec("invalid-add-column", add_column_sql=template)


def test_database_introspection_uses_dialect_metadata(tmp_path):
    from heavenbase.database.dialect.registry import DatabaseDialectSpec

    db = hb.Database(provider="sqlite", database=_db(tmp_path, "introspection.db"))

    assert db.current_database() == "main"
    assert db.dbs() == ["main"]

    database_dialects.register(
        DatabaseDialectSpec(
            "custom-introspection",
            current_database_sql="SELECT 'active' AS name",
            list_databases_sql="SELECT 'alpha' AS name UNION ALL SELECT 'beta' AS name",
        ),
        replace=True,
    )
    _bind_test_dialect(db, "custom-introspection")

    assert db.current_database() == "active"
    assert db.dbs() == ["alpha", "beta"]


def test_database_rename_table_uses_dialect_metadata(tmp_path, monkeypatch):
    db = hb.Database(provider="sqlite", database=_db(tmp_path, "rename.db"))
    db.execute("CREATE TABLE old_table (id TEXT)")

    db.rename_table("old_table", "new_table")

    assert "new_table" in db.tables()
    assert "old_table" not in db.tables()

    calls = []
    monkeypatch.setattr(db, "execute", lambda sql, params=None, readonly=False: calls.append((sql, params, readonly)))
    monkeypatch.setattr(db, "invalidate_schema_index", lambda: None)
    _bind_test_dialect(db, "mssql")

    db.rename_table("old_table", "new_table")

    assert calls == [("EXEC sp_rename :old_name, :new_name", {"old_name": "old_table", "new_name": "new_table"}, False)]


def test_database_reflection_fallback_uses_dialect_metadata(tmp_path, monkeypatch):
    import heavenbase.database.database as database_module

    from heavenbase.database.dialect.registry import DatabaseDialectSpec

    class BrokenInspector:
        def get_columns(self, *args, **kwargs):
            raise RuntimeError("reflection unavailable")

        def get_pk_constraint(self, *args, **kwargs):
            raise RuntimeError("reflection unavailable")

        def get_foreign_keys(self, *args, **kwargs):
            raise RuntimeError("reflection unavailable")

    db = hb.Database(provider="sqlite", database=_db(tmp_path, "reflection.db"))
    db.execute("CREATE TABLE items (id TEXT PRIMARY KEY, body TEXT)")
    database_dialects.register(
        DatabaseDialectSpec(
            "reflection-test",
            column_reflection_sql="PRAGMA table_info({table_name_literal})",
            primary_key_reflection_sql="PRAGMA table_info({table_name_literal})",
            empty_foreign_keys_on_reflection_error=True,
        ),
        replace=True,
    )
    _bind_test_dialect(db, "reflection-test")
    monkeypatch.setattr(database_module.sa, "inspect", lambda engine: BrokenInspector())

    assert db.columns("items") == ["id", "body"]
    assert db.pks("items") == ["id"]
    assert db.fks("items") == []


def test_database_superuser_url_uses_dialect_metadata_and_explicit_config_override():
    from heavenbase.database import DB_ENGINE

    postgres = DB_ENGINE.resolve({"provider": "postgres", "host": "localhost", "database": "shop", "username": "app", "superuser": {"username": "root"}})
    starrocks = DB_ENGINE.resolve({"provider": "starrocks", "host": "localhost", "database": "shop", "username": "root", "superuser": {"database": None}})
    oracle = DB_ENGINE.resolve({"provider": "oracle", "host": "localhost", "database": "FREEPDB1", "username": "system", "superuser": {}})

    assert DB_ENGINE.apply(postgres, profile="superuser")["url"].database == "postgres"
    assert DB_ENGINE.apply(starrocks, profile="superuser")["url"].database is None
    assert DB_ENGINE.apply(oracle, profile="superuser")["url"].query["service_name"] == "FREEPDB1"


def test_engine_args_exclude_backend_instance_vocabulary():
    from heavenbase.database import DB_ENGINE

    # Workspace backend rows carry type/name/backend_type/desc; a polluted or
    # hand-edited config must not leak them into create_engine kwargs.
    config = {"provider": "sqlite", "database": ":memory:", "type": "sqlite", "name": "main", "backend_type": "sqlite", "desc": "row store"}
    engine_args = DB_ENGINE.apply(config, profile="engine")
    assert not {"type", "name", "backend_type", "desc"} & set(engine_args)


def test_database_url_host_policy_uses_dialect_metadata():
    from heavenbase.database import DB_ENGINE

    with pytest.raises(ValueError, match="requires host"):
        DB_ENGINE.resolve({"provider": "postgres", "host": None, "database": "shop"})

    DB_ENGINE.resolve({"provider": "trino", "database": "hive"})
    DB_ENGINE.resolve({"provider": "oracle", "database": "FREEPDB1", "params": {"service_name": "FREEPDB1"}})

    oracle_url = DB_ENGINE.apply(
        {"provider": "oracle", "database": "FREEPDB1", "params": {"service_name": "FREEPDB1"}},
        profile="url",
    )

    assert oracle_url.query["service_name"] == "FREEPDB1"
    assert oracle_url.database is None


def test_database_server_ddl_uses_dialect_resource_contract():
    spec = _db_spec(provider="postgres", dialect="postgresql", database="shop", username="app", superuser={"username": "root"})
    dialect = database_dialects.get("postgresql")

    assert dialect.database_exists_sql() == "SELECT 1 FROM pg_database WHERE datname = :name"
    assert dialect.create_database_statement(spec, "shop") == 'CREATE DATABASE "shop"'
    assert dialect.drop_database_statement("shop") == 'DROP DATABASE "shop"'
    assert dialect.before_drop_force_statements("shop", force=True)[0][1] == {"name": "shop"}
    assert dialect.after_create_statements(spec, "shop") == ['ALTER DATABASE "shop" OWNER TO "app"', 'GRANT ALL PRIVILEGES ON DATABASE "shop" TO "app"']


def test_database_ddl_uses_lifecycle_tags(monkeypatch):
    from heavenbase.database import ddl
    from heavenbase.database.dialect.registry import DatabaseDialectSpec

    calls = []
    monkeypatch.setattr(ddl, "_server_ddl", lambda spec, drop=False, force=False: calls.append((spec.dialect, drop, force)))
    # The tagged dialect borrows PostgreSQL's templates: a drop with no registered statement removes nothing.
    tagged = DatabaseDialectSpec("ddl-tag-test", tags={"database-create": True, "database-drop": True}, ddl_resource_key="postgresql")
    database_dialects.register(tagged, replace=True)
    database_dialects.register(DatabaseDialectSpec("ddl-no-tag-test", ddl_resource_key="postgresql"), replace=True)

    ddl.create_database(_db_spec(provider="ddl-tag-test", database="shop"))
    ddl.drop_database(_db_spec(provider="ddl-tag-test", database="shop"), force=True)
    ddl.create_database(_db_spec(provider="ddl-no-tag-test", database="shop"))
    ddl.drop_database(_db_spec(provider="ddl-no-tag-test", database="shop"), force=True)

    assert calls == [("ddl-tag-test", False, False), ("ddl-tag-test", True, True)]


def test_sqlresponse_iterator_close_caches_remaining_rows(tmp_path):
    db = hb.Database(provider="sqlite", database=_db(tmp_path, "lazy.db"))
    db.execute("CREATE TABLE nums (n INTEGER)")
    db.execute("INSERT INTO nums (n) VALUES (:n)", [{"n": 1}, {"n": 2}, {"n": 3}])

    result = db.execute("SELECT n FROM nums ORDER BY n", readonly=True)
    iterator = iter(result)

    assert next(iterator) == {"n": 1}
    iterator.close()
    assert result.to_lists() == [[1], [2], [3]]
    assert result.clone().to_dicts() == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_sqlresponse_does_not_read_lastrowid_from_row_cursor():
    """Row-returning drivers may make lastrowid access destructive."""

    class FragileResult:
        returns_rows = True
        rowcount = -1

        def __init__(self):
            self.closed = False

        @property
        def lastrowid(self):
            self.closed = True
            raise AttributeError("row cursors do not expose lastrowid")

        def keys(self):
            return ["id"]

        def mappings(self):
            if self.closed:
                raise RuntimeError("cursor was closed by lastrowid access")
            return iter([{"id": "a"}])

    result = FragileResult()
    response = hb.SQLResponse.from_cursor(result)

    assert response.to_dicts() == [{"id": "a"}]
    assert result.closed is False
    assert response.lastrowid is None


def test_sqlresponse_column_checks_and_table_display(tmp_path):
    db = hb.Database(provider="sqlite", database=_db(tmp_path, "display.db"))
    db.execute("CREATE TABLE items (id TEXT, name TEXT)")
    db.execute("INSERT INTO items (id, name) VALUES (:id, :name)", [{"id": "a", "name": "Alpha"}, {"id": "b", "name": "Beta"}])

    result = db.execute("SELECT id, name FROM items ORDER BY id", readonly=True)

    assert result.to_dicts(columns=["id"]) == [{"id": "a"}, {"id": "b"}]
    with pytest.raises(ValueError, match="Unknown column"):
        result.to_dicts(columns=["missing"])
    assert result.to_dicts(columns=["missing"], check=False) == [{"missing": None}, {"missing": None}]
    rendered = result.table_display(max_rows=1)
    assert "| id " in rendered
    assert "2 rows" in rendered
    assert "| name" in table_display(result, columns=["name"])


def test_sqlresponse_owns_row_projection_protocol():
    detached = hb.SQLResponse.from_rows([{"id": "v", "embedding": [1.0, 2.0], "meta": {"k": ["x"]}}], columns=["id", "embedding", "meta"])
    exported = detached.to_list()
    exported[0]["embedding"].append(3.0)
    exported[0]["meta"]["k"].append("y")
    assert detached.to_list() == [{"id": "v", "embedding": [1.0, 2.0], "meta": {"k": ["x"]}}]

    result = hb.SQLResponse.from_rows([{"id": "a", "price": 1.5}, {"id": "b", "price": 2.0}], columns=["id", "price"])

    assert result.shape == (2, 2)
    assert result.to_lists(columns=[1, 0]) == [[1.5, "a"], [2.0, "b"]]
    assert result.to_list("tuple", columns=["missing"], check=False) == [(None,), (None,)]
    assert result.column(1) == [1.5, 2.0]
    assert result[0, 1] == 1.5
    assert result.scalar() == "a"
    assert not hasattr(result, "rows")
    assert not hasattr(result, "size")
    with pytest.raises(ValueError, match="SQL response row format"):
        result.to_list("records")


@pytest.mark.skipif(find_spec("pandas") is None, reason="pandas is not installed")
def test_sqlresponse_exports_to_pandas(tmp_path):
    db = hb.Database(provider="sqlite", database=_db(tmp_path, "pandas.db"))
    db.execute("CREATE TABLE items (id TEXT, price FLOAT)")
    db.execute("INSERT INTO items (id, price) VALUES (:id, :price)", [{"id": "a", "price": 1.0}])

    frame = db.execute("SELECT * FROM items", readonly=True).to_pandas()

    assert list(frame["id"]) == ["a"]


@pytest.mark.skipif(find_spec("numpy") is None, reason="numpy is not installed")
def test_sqlresponse_exports_to_numpy(tmp_path):
    db = hb.Database(provider="sqlite", database=_db(tmp_path, "numpy.db"))
    db.execute("CREATE TABLE items (id TEXT, price FLOAT)")
    db.execute("INSERT INTO items (id, price) VALUES (:id, :price)", [{"id": "a", "price": 1.0}])

    arr = db.execute("SELECT id, price FROM items", readonly=True).to_numpy("id", "price")

    assert arr.shape == (1, 2)
    assert arr[0, 0] == "a"


@pytest.mark.skipif(find_spec("pyarrow") is None, reason="pyarrow is not installed")
def test_sqlresponse_exports_to_pyarrow(tmp_path):
    db = hb.Database(provider="sqlite", database=_db(tmp_path, "arrow.db"))
    db.execute("CREATE TABLE items (id TEXT, price FLOAT)")
    db.execute("INSERT INTO items (id, price) VALUES (:id, :price)", [{"id": "a", "price": 1.0}])

    table = db.execute("SELECT id, price FROM items", readonly=True).to_pyarrow()

    assert table.num_rows == 1
    assert table.column("id").to_pylist() == ["a"]


@pytest.mark.fast
def test_database_dialect_packed_vector_storage_is_a_validated_physical_type():
    """Packed float64 vectors are an opt-in: no packaged dialect declares them, and unknown tokens are rejected."""
    from sqlalchemy import LargeBinary
    from sqlalchemy.dialects import sqlite as sqlite_dialect

    from heavenbase.database.dialect.registry import SQL_TYPE_FLOAT64_BLOB, SQL_TYPE_FLOAT_ARRAY, DatabaseDialectSpec
    from heavenbase.database.types import DatabaseVectorType

    assert SQL_TYPE_FLOAT64_BLOB == "float64_blob"
    assert hb.ext.SQL_TYPE_FLOAT64_BLOB == SQL_TYPE_FLOAT64_BLOB
    assert database_dialects.get("sqlite").to_dict()["vector_packed_type"] is None
    for identifier in ("sqlite", "postgresql", "mysql", "duckdb", "default"):
        assert database_dialects.get(identifier).vector_packed_type is None

    packed = DatabaseDialectSpec("packedtypes", tags={"database-dynamic-typing": True}, vector_packed_type="FLOAT64-BLOB")
    assert packed.vector_packed_type == SQL_TYPE_FLOAT64_BLOB
    # packed bytes need columns that hold a BLOB whatever their declared type, so a statically typed dialect refuses them
    with pytest.raises(ValueError, match="'statictypes' cannot use vector_packed_type 'float64_blob'"):
        DatabaseDialectSpec("statictypes", vector_packed_type="float64_blob")
    assert DatabaseDialectSpec("texttypes", vector_packed_type="json_text").vector_packed_type == hb.ext.SQL_TYPE_JSON_TEXT == "json_text"
    with pytest.raises(ValueError, match="vector_packed_type"):
        DatabaseDialectSpec("badpacked", vector_packed_type="float32_blob")

    class Dialect:
        def __init__(self, name):
            self.name = name

        def type_descriptor(self, value):
            return value

    # a native array type always wins over packed storage
    both = DatabaseDialectSpec("bothtypes", vector_native_type=SQL_TYPE_FLOAT_ARRAY, vector_packed_type=SQL_TYPE_FLOAT64_BLOB)
    assert not isinstance(DatabaseVectorType(dialect_spec=both).load_dialect_impl(Dialect("bothtypes")), LargeBinary)
    assert DatabaseVectorType(dialect_spec=both).process_bind_param([1, 2], Dialect("bothtypes")) == [1.0, 2.0]
    assert isinstance(DatabaseVectorType(dialect_spec=packed).load_dialect_impl(Dialect("packedtypes")), LargeBinary)
    assert not isinstance(DatabaseVectorType(dialect_spec=database_dialects.get("sqlite")).load_dialect_impl(sqlite_dialect.dialect()), LargeBinary)
