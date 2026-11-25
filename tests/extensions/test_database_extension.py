from __future__ import annotations

import sys

import pytest
from contextlib import contextmanager
from importlib import import_module
from types import SimpleNamespace

import heavenbase as hb
import heavenbase.ext as hb_ext
from heavenbase.extensions.database.toolkit import database_allow_write
from heavenbase.utils.config import ConfigManager


def _ws(name: str):
    return hb.HeavenBase(name, detached=True, backends={"main": {"type": "inmem"}})


def _warehouse_db(tmp_path) -> str:
    path = tmp_path / "warehouse.db"
    db = hb.Database(provider="sqlite", database=hb.utils.path_to_file_uri(path))
    db.execute("CREATE TABLE customers (customer_id TEXT PRIMARY KEY, name TEXT, tier TEXT)", readonly=False)
    db.execute("CREATE TABLE orders (order_id TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers(customer_id), total REAL)", readonly=False)
    db.execute("CREATE TABLE customer_labels (customer_id TEXT, label TEXT)", readonly=False)
    db.execute("CREATE VIEW customer_names AS SELECT customer_id, name FROM customers", readonly=False)
    db.execute("INSERT INTO customers VALUES ('c1', 'Ada', 'enterprise')", readonly=False)
    db.execute("INSERT INTO orders VALUES ('o1', 'c1', 42.0)", readonly=False)
    db.execute("INSERT INTO customer_labels VALUES ('c1', 'priority')", readonly=False)
    return str(path)


@contextmanager
def _default_extensions(value):
    old_value = hb.CM_HVNB.get("heavenbase.extensions.default", default=None)
    try:
        hb.CM_HVNB.set("heavenbase.extensions.default", value)
        yield
    finally:
        if old_value is None:
            hb.CM_HVNB.unset("heavenbase.extensions.default")
        else:
            hb.CM_HVNB.set("heavenbase.extensions.default", old_value)


@contextmanager
def _allow_write(value):
    old_value = hb.CM_HVNB.get("heavenbase.database.allow_write", default=None)
    try:
        hb.CM_HVNB.set("heavenbase.database.allow_write", value)
        yield
    finally:
        if old_value is None:
            hb.CM_HVNB.unset("heavenbase.database.allow_write")
        else:
            hb.CM_HVNB.set("heavenbase.database.allow_write", old_value)


def _extension_rows(ws):
    return [row for row in ws.query(hb.MetaSchema).where({"kind": "extension"}).execute().rows() if not row["object_id"].startswith("registration:")]


def test_database_extension_registers_optional_entities():
    with _default_extensions([]):
        ws = _ws("database-extension-test")

        assert "database" in hb_ext.Extension.identifiers()
        assert "database" not in ws.extensions()
        assert "db-database" not in ws.entities
        assert not hasattr(ws, "database")

        spec = ws.enable_extension("database")

        assert spec.identifier == "database"
        assert hasattr(ws, "database")
        assert callable(ws.database.ingest)
        assert callable(ws.database.sources)
        assert {"db-database", "db-table", "db-column", "db-enum", "db-snippet", "db-predicate", "db-rule"} <= set(ws.entities)
        row = next(row for row in _extension_rows(ws) if row["subject_id"] == "database")
        assert row["name"] == "Database"
        assert row["meta"]["required"] is False
        assert "db-database" in row["meta"]["entities"]


def test_database_entity_additions_keep_released_rows_compatible():
    from heavenbase.extensions.database import DbColumn, DbDatabase, DbEnum, DbPredicate, DbSnippet, DbTable

    fields = {
        DbDatabase: ("desc", "synonyms", "schemas", "selected_tables", "statistics", "semantic_text"),
        DbTable: ("database_name", "schema_name", "qualified_name", "desc", "synonyms", "statistics", "semantic_text"),
        DbColumn: ("database_id", "qualified_name", "semantic_type", "desc", "synonyms", "profile", "statistics", "semantic_text"),
        DbEnum: ("database_id", "database_name", "desc", "synonyms", "statistics", "semantic_text"),
        DbSnippet: ("database_name", "dialect", "tables", "synonyms", "verified", "last_succeeded_at", "semantic_text"),
        DbPredicate: ("database_id", "database_name", "title", "synonyms", "semantic_text"),
    }

    for entity, names in fields.items():
        schema = entity.schema()
        assert all(schema.fields[name].required is False and schema.fields[name].has_default is False for name in names)


def test_ingest_database_sqlite_warehouse_shapes_and_search(tmp_path):
    ws = _ws("database-ingest-test")
    ws.enable_extension("database")

    result = ws.database.ingest(_warehouse_db(tmp_path), name="warehouse")

    assert result["database"]
    assert len(result["tables"]) >= 3
    assert len(result["columns"]) >= 3

    database_id = result["database"][0]
    db_row = ws.get(database_id, entity="db-database")
    assert db_row["name"] == "warehouse"
    assert db_row["dialect"] == "sqlite"
    assert "password" not in str(db_row["dsn_summary"]).lower() or "<redacted>" in str(db_row["dsn_summary"])
    assert db_row["source"].get("password") in (None, "", "***")

    table_rows = ws.query("db-table").where({"database_id": database_id}).execute().rows()
    table_names = {row["table_name"] for row in table_rows}
    assert {"customers", "orders", "customer_labels", "customer_names"} <= table_names
    assert next(row for row in table_rows if row["table_name"] == "customer_names")["relation_kind"] == "view"

    customers = next(row for row in table_rows if row["table_name"] == "customers")
    column_rows = ws.query("db-column").where({"table_id": customers["object_id"]}).execute().rows()
    column_names = {row["column_name"] for row in column_rows}
    assert "customer_id" in column_names
    assert all("dtype" in row and "nullable" in row for row in column_rows)

    discovered = ws.query("db-table").where({"table_name": {"$match": "customer"}}).execute().rows()
    assert discovered


def test_ingest_database_reports_duckdb_catalog_indexes_and_primary_key(tmp_path):
    pytest.importorskip("duckdb_engine")
    uri = hb.utils.path_to_file_uri(tmp_path / "warehouse.duckdb")
    db = hb.Database(provider="duckdb", database=uri)
    db.execute("CREATE TABLE customers (customer_id VARCHAR PRIMARY KEY, email VARCHAR, tier VARCHAR)", readonly=False)
    db.execute("CREATE UNIQUE INDEX customers_email ON customers (email)", readonly=False)
    db.execute("CREATE INDEX customers_tier ON customers (tier)", readonly=False)
    db.execute("INSERT INTO customers VALUES ('c1', 'ada@example.com', 'enterprise')", readonly=False)
    ws = _ws("database-ingest-duckdb-indexes")
    ws.enable_extension("database")

    result = ws.database.ingest({"provider": "duckdb", "database": uri}, name="warehouse", profile=False)

    (table_id,) = result["tables"]
    table = ws.get(table_id, entity="db-table")
    # SQLAlchemy's DuckDB inspector reports no indexes and no primary key, so both were once ingested empty
    indexes = {index["name"]: (index["unique"], index["column_names"]) for index in table["indexes"]}
    assert indexes == {"customers_email": (True, ["email"]), "customers_tier": (False, ["tier"])}
    assert table["primary_key"] == ["customer_id"]
    columns = {row["column_name"]: row for row in ws.query("db-column").where({"table_id": table_id}).execute().rows()}
    assert columns["customer_id"]["is_primary_key"] is True and columns["email"]["is_primary_key"] is False


def test_duckdb_reflection_names_each_schema_once_and_accepts_its_names(tmp_path):
    pytest.importorskip("duckdb_engine")
    db = hb.Database(provider="duckdb", database=hb.utils.path_to_file_uri(tmp_path / "shop.duckdb"))
    db.execute("CREATE TABLE items (sku VARCHAR PRIMARY KEY, label VARCHAR)", readonly=False)
    db.execute("CREATE INDEX items_label ON items (label)", readonly=False)

    schemas = db.schemas()
    assert len(schemas) == len(set(schemas))
    (main,) = [schema for schema in schemas if "items" in db.tables(schema=schema)]
    # duckdb-engine names a schema "<database>.<schema>"; the catalog reflections accept that name
    assert db._reflected_indexes("items", main) == [{"name": "items_label", "unique": False, "column_names": ["label"]}]
    assert db.pks("items", schema=main) == db.pks("items") == ["sku"]


def test_duckdb_composite_primary_key_keeps_its_declared_order(tmp_path):
    pytest.importorskip("duckdb_engine")
    uri = hb.utils.path_to_file_uri(tmp_path / "orders.duckdb")
    db = hb.Database(provider="duckdb", database=uri)
    # the key lists its columns in an order other than the table's column order
    db.execute("CREATE TABLE order_lines (line_no INTEGER, sku VARCHAR, order_id VARCHAR, PRIMARY KEY (order_id, line_no))", readonly=False)
    db.execute("INSERT INTO order_lines VALUES (1, 'a', 'o1')", readonly=False)
    (main,) = [schema for schema in db.schemas() if "order_lines" in db.tables(schema=schema)]
    assert db.pks("order_lines") == db.pks("order_lines", schema=main) == ["order_id", "line_no"]

    ws = _ws("database-ingest-duckdb-key-order")
    ws.enable_extension("database")
    result = ws.database.ingest({"provider": "duckdb", "database": uri}, name="orders", profile=False)
    assert ws.get(result["tables"][0], entity="db-table")["primary_key"] == ["order_id", "line_no"]


def test_ingest_database_redacts_dsn_credentials():
    from heavenbase.extensions.database.source import redact_dsn_summary, redacted_source
    from heavenbase.database import Database

    spec = Database(
        provider="postgres",
        username="analyst",
        password="super-secret",
        host="localhost",
        port=5432,
        database="warehouse",
    ).spec
    summary = redact_dsn_summary(spec)
    source = redacted_source(spec)

    assert "super-secret" not in summary
    assert ":***@" in summary
    assert source.get("password") == "***"


def test_database_source_delegates_url_driver_and_query_to_db_engine():
    from heavenbase.extensions.database.source import resolve_database

    workspace = object()
    resolved = resolve_database(
        workspace,
        "postgresql+psycopg2://analyst:secret@localhost:5432/warehouse?sslmode=require&application_name=ingest",
    )

    assert resolved.db.spec.provider == "postgres"
    assert resolved.db.spec.driver == "psycopg2"
    assert resolved.db.spec.params == {"sslmode": "require", "application_name": "ingest"}


def test_database_profile_requires_explicit_extension_activation_and_lists_tools():
    ws = _ws("database-mcp-profile")
    assert "database" not in ws.extensions()
    assert not hasattr(ws, "database")
    ws.enable_extension("toolkit")

    with pytest.raises(ValueError, match="Unsupported MCP workspace profile 'database'"):
        ws.to_mcp(profile="database")

    ws.enable_extension("database")
    toolkit = ws.to_mcp(profile="database")
    tool_names = toolkit.list_tools()
    toolkit.run("suggest_sql", question="customers")
    empty_tables = toolkit.run("query", entity="db-table", spec={})

    assert "database" in ws.extensions()
    assert hasattr(ws, "database")
    assert empty_tables["n"] == 0
    assert "ingest_database" in tool_names
    assert "list_sources" in tool_names
    assert "run_snippet" in tool_names
    assert "run_sql" in tool_names
    assert "save_snippet" in tool_names
    assert "suggest_sql" in tool_names
    assert "get" in tool_names
    assert "query" in tool_names
    assert "upsert" in tool_names
    assert "set" in tool_names
    assert "delete" not in tool_names


@pytest.mark.parametrize("main_type", ["sqlite", "inmem"])
def test_database_mcp_ingests_only_named_workspace_sql_sources(tmp_path, main_type):
    """A source card reports whether default placement also stores context on that source.

    ``Array[ShortText]`` fields such as ``synonyms`` go to the first SQL row
    store, so a SQL ``main`` keeps every ``db-*`` row off the warehouse, while
    an in-memory ``main`` places those fields on the warehouse.
    """
    source = _warehouse_db(tmp_path)
    main = {"type": "inmem"} if main_type == "inmem" else {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "main.db")}
    ws = hb.HeavenBase(
        "database-named-source",
        detached=True,
        backends={
            "main": main,
            "warehouse": {"type": "sqlite", "database": hb.utils.path_to_file_uri(source)},
        },
    )
    ws.enable_extension("database")
    toolkit = ws.to_mcp(profile="database")

    sources = toolkit.run("list_sources")
    card = next(row for row in sources["sources"] if row["backend_ref"] == "warehouse")
    result = toolkit.run(
        "ingest_database",
        backend_ref="warehouse",
        name="warehouse",
        tables=["customers", "orders"],
        columns={"customers": ["customer_id", "tier"], "orders": ["order_id", "total"]},
        profile=False,
    )
    database = ws.get(result["database"][0], entity="db-database")

    context = {row["backend_ref"]: row["context_backend"] for row in sources["sources"]}
    assert context == ({"main": True, "warehouse": False} if main_type == "sqlite" else {"warehouse": True})
    assert sources["n"] == len(context)
    assert card["status"] == "connected"
    assert "password" not in repr(card).lower()
    assert database["backend_ref"] == "warehouse"
    assert {row["table_name"] for row in ws.query("db-table").execute().rows()} == {"customers", "orders"}
    with pytest.raises(RuntimeError, match="Invalid backend instance name"):
        toolkit.run("ingest_database", backend_ref="sqlite:///not-allowed.db", profile=False)
    with pytest.raises(RuntimeError, match="Unknown backend"):
        toolkit.run("ingest_database", backend_ref="not_configured", profile=False)


def test_selective_ingest_builds_rich_standalone_context(tmp_path):
    ws = _ws("database-selective-ingest")
    ws.enable_extension("database")

    result = ws.database.ingest(
        _warehouse_db(tmp_path),
        name="warehouse",
        tables=["customers", "orders"],
        columns={"customers": ["customer_id", "tier"], "orders": ["order_id", "customer_id", "total"]},
    )

    database_id = result["database"][0]
    database = ws.get(database_id, entity="db-database")
    tables = ws.query("db-table").where({"database_id": database_id}).execute().rows()
    columns = ws.query("db-column").where({"database_id": database_id}).execute().rows()

    assert {row["table_name"] for row in tables} == {"customers", "orders"}
    assert all(row["database_name"] == "warehouse" and row["qualified_name"] for row in tables)
    assert database["statistics"]["relation_count"] == 2
    assert database["statistics"]["column_count"] == 5
    assert all("customer_labels" not in name for name in database["selected_tables"])

    customer_columns = {row["column_name"]: row for row in columns if row["table_name"] == "customers"}
    assert set(customer_columns) == {"customer_id", "tier"}
    assert customer_columns["customer_id"]["is_primary_key"] is True
    assert customer_columns["customer_id"]["semantic_type"] == "identifier"
    assert customer_columns["tier"]["semantic_type"] == "categorical"
    assert customer_columns["tier"]["statistics"]["distinct_count"] == 1
    assert customer_columns["tier"]["profile"]
    assert "tier" in customer_columns["tier"]["semantic_text"]

    order_customer = next(row for row in columns if row["table_name"] == "orders" and row["column_name"] == "customer_id")
    assert order_customer["foreign_key"]["table"] == "customers"
    assert order_customer["semantic_type"] == "reference"
    enums = ws.query("db-enum").where({"database_id": database_id}).execute().rows()
    assert any(row["column_name"] == "tier" and row["values"] == ["enterprise"] for row in enums)


def test_ingest_preserves_agent_authored_context_on_refresh(tmp_path):
    ws = _ws("database-refresh-context")
    ws.enable_extension("database")
    source = _warehouse_db(tmp_path)
    result = ws.database.ingest(source, name="warehouse", tables="customers", columns={"customers": ["tier"]})
    column_id = result["columns"][0]

    ws.set(
        column_id,
        {"desc": "Customer commercial segment used for account planning.", "synonyms": ["account segment", "service tier"], "semantic_type": "dimension"},
        entity="db-column",
    )
    ws.database.ingest(source, name="warehouse", tables="customers", columns={"customers": ["tier"]})
    refreshed = ws.get(column_id, entity="db-column")

    assert refreshed["desc"] == "Customer commercial segment used for account planning."
    assert refreshed["synonyms"] == ["account segment", "service tier"]
    assert refreshed["semantic_type"] == "dimension"
    assert "account segment" in refreshed["semantic_text"]
    assert any(row["target_id"] == column_id for row in ws.search("account segment").rows())


def test_ingest_validates_selection_before_writing(tmp_path):
    ws = _ws("database-selection-validation")
    ws.enable_extension("database")
    source = _warehouse_db(tmp_path)

    with pytest.raises(ValueError, match="Unknown table"):
        ws.database.ingest(source, name="warehouse", tables=["missing_table"])
    assert ws.count("db-database") == 0

    with pytest.raises(ValueError, match="Unknown columns"):
        ws.database.ingest(source, name="warehouse", tables=["customers"], columns={"customers": ["missing_column"]})
    assert ws.count("db-database") == 0


def test_run_snippet_executes_readonly_sql(tmp_path):
    ws = _ws("database-run-snippet")
    ws.enable_extension("database")
    result = ws.database.ingest(_warehouse_db(tmp_path), name="warehouse")
    database_id = result["database"][0]

    snippet_id = "snippet-customers-count"
    ws.upsert(
        "db-snippet",
        {
            "object_id": snippet_id,
            "database_id": database_id,
            "title": "Count customers",
            "sql": "SELECT COUNT(*) AS n FROM customers",
            "readonly": True,
            "card_text": "count all customers in warehouse",
        },
    )

    frame = ws.database.run_snippet(snippet_id)
    assert frame["readonly"] is True
    assert frame["n"] == 1
    assert frame["rows"][0]["n"] >= 1

    matches = ws.database.suggest_sql("count customers")
    assert any(row["object_id"] == snippet_id for row in matches["snippets"])


def test_run_sql_is_readonly_and_bounds_results(monkeypatch, tmp_path):
    ws = _ws("database-run-sql")
    ws.enable_extension("database")
    database_id = ws.database.ingest(_warehouse_db(tmp_path), name="warehouse", profile=False)["database"][0]

    frame = ws.database.run_sql(database_id, "SELECT customer_id FROM customers UNION ALL SELECT customer_id FROM customers", limit=1)

    assert frame["n"] == 1
    assert frame["total"] == 2
    assert frame["limit"] == 1
    assert frame["truncated"] is True
    with pytest.raises(RuntimeError, match="configured maximum"):
        ws.database.run_sql(database_id, "SELECT customer_id FROM customers", limit=201)
    with pytest.raises(RuntimeError, match="read-only"):
        ws.database.run_sql(database_id, "DELETE FROM customers")
    # without SQLGlot, a read followed by a second statement is still not read-only
    monkeypatch.setitem(sys.modules, "sqlglot", None)
    with pytest.raises(RuntimeError, match="read-only"):
        ws.database.run_sql(database_id, "SELECT customer_id FROM customers; DELETE FROM customers")
    assert ws.database.run_sql(database_id, "SELECT customer_id FROM customers;")["total"] == 1


def test_save_snippet_persists_only_successful_sql(tmp_path):
    ws = _ws("database-save-snippet")
    ws.enable_extension("database")
    database_id = ws.database.ingest(_warehouse_db(tmp_path), name="warehouse", profile=False)["database"][0]

    saved = ws.database.save_snippet(
        database_id,
        "Customer count",
        "SELECT COUNT(*) AS n FROM customers",
        card_text="How many customer accounts exist?",
        tables=["main.customers"],
        synonyms=["number of customers"],
    )
    snippet = ws.get(saved["snippet_id"], entity="db-snippet")

    assert saved["verified"] is True
    assert saved["result"]["rows"][0]["n"] == 1
    assert snippet["verified"] is True
    assert snippet["database_name"] == "warehouse"
    assert snippet["last_succeeded_at"] > 0
    assert "number of customers" in snippet["semantic_text"]

    before = ws.count("db-snippet")
    with pytest.raises(RuntimeError):
        ws.database.save_snippet(database_id, "Broken query", "SELECT missing FROM customers")
    assert ws.count("db-snippet") == before


def test_database_mcp_can_edit_context_and_return_relevant_rules(tmp_path):
    ws = _ws("database-agent-edit")
    ws.enable_extension("database")
    ws.enable_extension("toolkit")
    database_id = ws.database.ingest(_warehouse_db(tmp_path), name="warehouse", tables="orders", profile=False)["database"][0]
    column = next(row for row in ws.query("db-column").execute().rows() if row["column_name"] == "total")
    toolkit = ws.to_mcp(profile="database")

    toolkit.run(
        "set",
        entity="db-column",
        row={"object_id": column["object_id"], "desc": "Order value before tax, in reporting currency.", "synonyms": ["revenue", "sales value"]},
    )
    toolkit.run(
        "upsert",
        entity="db-rule",
        row={
            "object_id": "rule-positive-order-value",
            "database_id": database_id,
            "database_name": "warehouse",
            "title": "Exclude non-positive order value",
            "desc": "Revenue analyses must include only orders whose total is greater than zero.",
            "tables": ["main.orders"],
            "triggers": ["revenue", "sales", "同比"],
            "synonyms": ["positive order amount"],
        },
    )

    edited = ws.get(column["object_id"], entity="db-column")
    suggestions = toolkit.run("suggest_sql", question="revenue")
    long_suggestions = toolkit.run(
        "suggest_sql",
        question="net revenue by customer region and product category, compare 2024 with the prior year",
    )
    chinese_suggestions = toolkit.run("suggest_sql", question="某公司2024年8月到2026年8月净重同比增长率")
    frame = toolkit.run("run_sql", database_id=database_id, sql="SELECT SUM(total) AS revenue FROM orders")
    saved = toolkit.run(
        "save_snippet",
        database_id=database_id,
        title="Order revenue",
        sql="SELECT SUM(total) AS revenue FROM orders",
        card_text="Total order revenue",
        tables=["main.orders"],
    )
    assert edited["desc"] == "Order value before tax, in reporting currency."
    assert "revenue" in edited["semantic_text"]
    assert any(rule["object_id"] == "rule-positive-order-value" for rule in suggestions["rules"])
    assert any(rule["object_id"] == "rule-positive-order-value" for rule in long_suggestions["rules"])
    assert any(rule["object_id"] == "rule-positive-order-value" for rule in chinese_suggestions["rules"])
    assert frame["rows"][0]["revenue"] == 42.0
    assert ws.get(saved["snippet_id"], entity="db-snippet")["verified"] is True


def test_run_snippet_refuses_write_by_default(tmp_path):
    ws = _ws("database-write-refusal")
    ws.enable_extension("database")
    database_id = ws.database.ingest(_warehouse_db(tmp_path), name="warehouse")["database"][0]

    ws.upsert(
        "db-snippet",
        {
            "object_id": "snippet-write",
            "database_id": database_id,
            "title": "Delete customers",
            "sql": "DELETE FROM customers",
            "readonly": False,
            "card_text": "destructive delete",
        },
    )

    assert database_allow_write() is False
    with pytest.raises(RuntimeError) as err:
        ws.database.run_snippet("snippet-write")
    assert "allow_write" in str(err.value).lower()


@pytest.mark.parametrize("value", ["false", "0", "no", "off", ""])
def test_database_allow_write_parses_false_strings(value):
    with _allow_write(value):
        assert database_allow_write() is False


def test_database_extension_uses_workspace_config_and_resolver(monkeypatch, tmp_path):
    from heavenbase.extensions.database import source

    config = ConfigManager(root=str(tmp_path), default={}, setup=False)
    config.set("heavenbase.database.allow_write", True)
    resolver = object()
    workspace = SimpleNamespace(context=SimpleNamespace(config=config), module_resolver=resolver)
    captured = {}

    class FakeDatabase:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(source, "Database", FakeDatabase)

    resolved = source.resolve_database(workspace, "warehouse.db")

    assert isinstance(resolved.db, FakeDatabase)
    assert captured["resolver"] is resolver
    assert captured["config"] is config
    assert str(tmp_path) in captured["database"]
    assert database_allow_write(config=config) is True


def test_run_snippet_redacts_execution_errors(monkeypatch, tmp_path):
    ws = _ws("database-error-redact")
    ws.enable_extension("database")
    database_id = ws.database.ingest(_warehouse_db(tmp_path), name="warehouse")["database"][0]
    ws.upsert(
        "db-snippet",
        {
            "object_id": "snippet-bad",
            "database_id": database_id,
            "title": "Bad SQL",
            "sql": "SELECT 1",
            "readonly": True,
            "card_text": "test",
        },
    )

    def fail_execute(sql, params=None, readonly=True):
        raise RuntimeError("password:open-sesame postgresql://alice:secret@localhost/db")

    db_row = ws.get(database_id, entity="db-database")
    live_db = hb.Database(**db_row["source"])

    monkeypatch.setattr(live_db, "execute", fail_execute)
    artifact_package = type(ws.database).__module__.split(".", 1)[0]
    run_snippet_module = import_module(f"{artifact_package}.toolkit.tools.run_snippet")
    monkeypatch.setattr(run_snippet_module, "database_from_row", lambda workspace, row: live_db)

    with pytest.raises(RuntimeError) as err:
        ws.database.run_snippet("snippet-bad")

    text = str(err.value)
    assert "open-sesame" not in text
    assert ":secret@" not in text
    assert "<redacted>" in text
