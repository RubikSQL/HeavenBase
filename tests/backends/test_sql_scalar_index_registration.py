"""A SQL registration builds only the scalar indexes its plan asks for, and an index the database fails to build is not kept."""

import os
import warnings
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError, OperationalError

import heavenbase as hb
from heavenbase.workspace.schema.service import _SchemaService


class Order(hb.Entity):
    identifier = "scalar-index-order"
    customer = hb.field(hb.ShortText).store(to="orders")
    note = hb.field(hb.ShortText).index(enabled=False).store(to="orders")


class Product(hb.Entity):
    identifier = "scalar-index-product"
    sku = hb.field(hb.ShortText)
    name = hb.field(hb.ShortText)


class UniqueProduct(hb.Entity):
    identifier = "scalar-index-product"
    sku = hb.field(hb.ShortText).unique()
    name = hb.field(hb.ShortText)


@pytest.fixture
def index_ddl():
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith(("CREATE INDEX", "CREATE UNIQUE INDEX", "DROP INDEX")):
            statements.append(statement.strip())

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", record)


def _indexed_columns(ws, entity):
    backend = ws.backends.get("orders")
    return backend.db.index_column_sets(backend.tables[entity.identifier])


def _file_orders(tmp_path, backend_type="sqlite"):
    if backend_type == "duckdb":
        pytest.importorskip("duckdb_engine")
    return hb.HeavenBase(
        "scalar-index-orders",
        detached=True,
        backends={
            "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "system.db")},
            "orders": {"type": backend_type, "database": hb.utils.path_to_file_uri(tmp_path / f"orders.{backend_type}")},
        },
    )


_FILE_BACKENDS = pytest.mark.parametrize("backend_type", ["sqlite", "duckdb"])


def _check_no_opted_out_index(open_workspace, index_ddl):
    ws = open_workspace()
    try:
        ws.register(Order)
        ws.upsert_many(Order, [{"object_id": f"o{i}", "customer": f"customer {i % 7}", "note": f"Deliver before noon ({i})"} for i in range(50)])
        ws.register(Order, allow_replace=True)
        assert ("customer",) in _indexed_columns(ws, Order) and ("note",) not in _indexed_columns(ws, Order)
    finally:
        ws.release()
    ws = open_workspace()
    try:
        ws.register(Order)
        assert ("customer",) in _indexed_columns(ws, Order) and ("note",) not in _indexed_columns(ws, Order)
    finally:
        ws.release()
    assert [statement for statement in index_ddl if "(note)" in statement.replace('"', "")] == []


@pytest.mark.slow
@_FILE_BACKENDS
def test_a_field_that_opts_out_of_its_default_index_is_never_indexed_on_a_file_database(tmp_path, index_ddl, backend_type):
    _check_no_opted_out_index(lambda: _file_orders(tmp_path, backend_type), index_ddl)


class IndexedOrder(hb.Entity):
    identifier = "scalar-index-order"
    customer = hb.field(hb.ShortText).store(to="orders")
    note = hb.field(hb.ShortText).store(to="orders")


class StoredProduct(hb.Entity):
    identifier = "scalar-index-stored-product"
    sku = hb.field(hb.ShortText).unique().store(to="orders")
    name = hb.field(hb.ShortText).store(to="orders")


class StoredProductWithoutUnique(hb.Entity):
    identifier = "scalar-index-stored-product"
    sku = hb.field(hb.ShortText).store(to="orders")
    name = hb.field(hb.ShortText).store(to="orders")


@pytest.mark.slow
@_FILE_BACKENDS
def test_a_unique_constraint_a_registration_no_longer_declares_stops_applying(tmp_path, backend_type):
    ws = _file_orders(tmp_path, backend_type)
    try:
        ws.register(StoredProduct)
        ws.upsert_many(StoredProduct, [{"object_id": "p1", "sku": "A-100", "name": "Desk lamp"}, {"object_id": "p2", "sku": "B-200", "name": "Standing desk"}])
        with pytest.raises(Exception):
            ws.upsert(StoredProduct, {"object_id": "p3", "sku": "A-100", "name": "Desk lamp, second shipment"})
        assert _indexed_columns(ws, StoredProduct)[("sku",)] is True
        ws.register(StoredProductWithoutUnique, allow_replace=True)
        if backend_type == "duckdb":
            # no index is dropped on DuckDB, so the constraint keeps refusing duplicates, as in 0.1.2.2
            assert _indexed_columns(ws, StoredProduct)[("sku",)] is True
            with pytest.raises(Exception, match="(?i)duplicate"):
                ws.upsert(StoredProductWithoutUnique, {"object_id": "p3", "sku": "A-100", "name": "Desk lamp, second shipment"})
            return
        assert ("sku",) not in {columns for columns, unique in _indexed_columns(ws, StoredProduct).items() if unique}
        ws.upsert(StoredProductWithoutUnique, {"object_id": "p3", "sku": "A-100", "name": "Desk lamp, second shipment"})
    finally:
        ws.release()
    ws = _file_orders(tmp_path, backend_type)
    try:
        ws.register(StoredProductWithoutUnique)
        ws.upsert(StoredProductWithoutUnique, {"object_id": "p4", "sku": "A-100", "name": "Desk lamp, third shipment"})
        assert ws.count(StoredProductWithoutUnique) == 4
    finally:
        ws.release()


@pytest.mark.slow
@_FILE_BACKENDS
def test_an_index_a_field_later_opts_out_of_is_dropped_and_a_repeat_registration_sends_no_index_ddl(tmp_path, index_ddl, backend_type):
    """DuckDB keeps the index, as in 0.1.2.2: no registration drops an index there."""
    ws = _file_orders(tmp_path, backend_type)
    try:
        ws.register(IndexedOrder)
        ws.upsert(IndexedOrder, {"object_id": "o1", "customer": "Northwind", "note": "Deliver before noon"})
        assert {("customer",), ("note",)} <= set(_indexed_columns(ws, IndexedOrder))
        ws.register(Order, allow_replace=True)
        assert ("customer",) in _indexed_columns(ws, Order) and (("note",) in _indexed_columns(ws, Order)) is (backend_type == "duckdb")
    finally:
        ws.release()
    index_ddl.clear()
    ws = _file_orders(tmp_path, backend_type)
    try:
        ws.register(Order)
        ws.register(Order, allow_replace=True)
        assert ("customer",) in _indexed_columns(ws, Order) and (("note",) in _indexed_columns(ws, Order)) is (backend_type == "duckdb")
    finally:
        ws.release()
    assert index_ddl == []


_DEFAULT_POSTGRES_URL = "postgresql+psycopg2://rubik:password@localhost:5432/postgres?sslmode=prefer"


@pytest.fixture
def postgres_url():
    pytest.importorskip("psycopg2")
    root = make_url(os.environ.get("HVNB_POSTGRES_URL", _DEFAULT_POSTGRES_URL)).set(database="postgres")
    database = f"scalar_index_{uuid4().hex}"
    admin = create_engine(root, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
    try:
        try:
            connection = admin.connect()
        except OperationalError:
            if "HVNB_POSTGRES_URL" in os.environ:
                pytest.fail("Configured PostgreSQL test service is unavailable", pytrace=False)
            pytest.skip("Default PostgreSQL test service is unavailable")
        with connection as opened:
            opened.exec_driver_sql(f"CREATE DATABASE {database}")
        try:
            yield root.set(database=database)
        finally:
            with admin.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE {database} WITH (FORCE)")
    finally:
        admin.dispose()


@pytest.mark.full
@pytest.mark.parametrize("backend_type", ["postgres", "pgvector"])
def test_a_field_that_opts_out_of_its_default_index_is_never_indexed_on_postgres(tmp_path, postgres_url, backend_type, index_ddl):
    if backend_type == "pgvector":
        pytest.importorskip("pgvector")

    def open_workspace():
        return hb.HeavenBase(
            postgres_url.database.replace("_", "-"),
            detached=True,
            backends={
                "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "system.db")},
                "orders": {"type": backend_type, "url": postgres_url.render_as_string(hide_password=False)},
            },
        )

    _check_no_opted_out_index(open_workspace, index_ddl)


def _sqlite_products(path):
    return hb.HeavenBase("scalar-index-products", detached=True, backends={"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(path)}})


@pytest.mark.slow
def test_a_unique_index_the_database_cannot_build_leaves_the_previous_registration_in_place(tmp_path, monkeypatch):
    path = tmp_path / "products.db"
    ws = _sqlite_products(path)
    other = _sqlite_products(path)
    try:
        ws.register(Product)
        ws.upsert_many(Product, [{"object_id": "p1", "sku": "A-100", "name": "Desk lamp"}, {"object_id": "p2", "sku": "B-200", "name": "Standing desk"}])
        other.register(Product)
        check = _SchemaService.preflight_unique_constraints

        def check_then_another_writer(self, schema, storage_plan):
            check(self, schema, storage_plan)
            if self.workspace is ws:
                # Another process stores a duplicate after the check and before the index is built.
                other.upsert(Product, {"object_id": "p3", "sku": "A-100", "name": "Desk lamp, second shipment"})

        monkeypatch.setattr(_SchemaService, "preflight_unique_constraints", check_then_another_writer)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
                ws.register(UniqueProduct, allow_replace=True)
        monkeypatch.undo()
        assert [str(item.message) for item in caught if issubclass(item.category, RuntimeWarning)] == []
        table = ws.backends.get("main").tables[Product.identifier]
        assert [index.name for index in table.indexes if index.unique] == []
        assert ws.get("p3", entity=Product)["sku"] == "A-100"
        ws.upsert(Product, {"object_id": "p4", "sku": "C-300", "name": "Office chair"})
        ws.register(Product, allow_replace=True)
        assert ws.count(Product) == 4
        assert ws.delete((Product, "p3"))
        ws.register(UniqueProduct, allow_replace=True)
        assert [tuple(column.name for column in index.columns) for index in ws.backends.get("main").tables[Product.identifier].indexes if index.unique] == [
            ("sku",)
        ]
    finally:
        other.release()
        ws.release()


def _product_entities(column):
    """The same entity with and without a unique ``column``, and with the column opting out of its default index."""

    def entity(name, field):
        return type(
            name,
            (hb.Entity,),
            {"identifier": f"scalar-index-{column}-product", column: field.store(to="orders"), "note": hb.field(hb.ShortText).store(to="orders")},
        )

    return (
        entity(f"Unique{column.title()}Product", hb.field(hb.ShortText).unique()),
        entity(f"Plain{column.title()}Product", hb.field(hb.ShortText)),
        entity(f"Unindexed{column.title()}Product", hb.field(hb.ShortText).index(enabled=False)),
    )


@pytest.mark.slow
@_FILE_BACKENDS
@pytest.mark.parametrize("column", ["sku", "name", "order", "group", "user", "limit"])
def test_a_removed_unique_constraint_stops_applying_and_leaves_the_default_index_whatever_the_column_name(tmp_path, backend_type, column):
    unique, plain, unindexed = _product_entities(column)
    ws = _file_orders(tmp_path, backend_type)
    try:
        ws.register(unique)
        ws.upsert(unique, {"object_id": "p1", column: "K-1", "note": "first"})
        assert _indexed_columns(ws, unique)[(column,)] is True
        ws.register(plain, allow_replace=True)
        if backend_type == "duckdb":
            assert _indexed_columns(ws, plain).get((column,)) is True  # no index is dropped on DuckDB
            return
        # the plain default index exists right after the registration that removed the unique one
        assert _indexed_columns(ws, plain).get((column,)) is False
        ws.upsert(plain, {"object_id": "p2", column: "K-1", "note": "second"})
        ws.register(unindexed, allow_replace=True)
        assert (column,) not in _indexed_columns(ws, unindexed)
        assert ws.count(unindexed) == 2
    finally:
        ws.release()


@pytest.mark.fast
def test_duckdb_index_reflection_reads_quoted_multi_column_and_expression_indexes(tmp_path):
    import sqlalchemy as sa

    ws = _file_orders(tmp_path, "duckdb")
    try:
        ws.register(Order)
        db = ws.backends.get("orders").db
        columns = ["name", "order", "Mixed Case", "it's", 'a"b', "a, b", "sku", "Region"]
        quoted = ['"' + column.replace('"', '""') + '"' for column in columns]
        db.execute("CREATE TABLE awkward (object_id VARCHAR PRIMARY KEY, " + ", ".join(f"{name} VARCHAR" for name in quoted) + ")", readonly=False)
        for position, name in enumerate(quoted):
            db.execute(f"CREATE INDEX ix_awkward_{position} ON awkward ({name})", readonly=False)
        db.execute('CREATE UNIQUE INDEX ix_awkward_multi ON awkward ("name", "Mixed Case", sku)', readonly=False)
        db.execute('CREATE INDEX ix_awkward_expression ON awkward (lower("name"))', readonly=False)
        table = sa.Table("awkward", sa.MetaData(), sa.Column("object_id", sa.String, primary_key=True), *(sa.Column(column, sa.String) for column in columns))
        reflected = {item["name"]: (item["unique"], item["column_names"]) for item in db.table_indexes(table)}
        assert reflected == {
            **{f"ix_awkward_{position}": (False, [column]) for position, column in enumerate(columns)},
            "ix_awkward_multi": (True, ["name", "Mixed Case", "sku"]),
            "ix_awkward_expression": (False, []),  # an expression index names no column
        }
        assert db.index_column_sets(table)[("name", "mixed case", "sku")] is True
        db.drop_index(table, "ix_awkward_2", ["Mixed Case"])
        assert "ix_awkward_2" not in {item["name"] for item in db.table_indexes(table)}
    finally:
        ws.release()


@pytest.mark.fast
@pytest.mark.parametrize(
    ("value", "names"),
    [
        ("[sku]", ["sku"]),
        ("['\"name\"', '\"Mixed Case\"', sku]", ["name", "Mixed Case", "sku"]),
        ("['\"it\\'s\"']", ["it's"]),
        ('[\'"a""b"\']', ['a"b']),
        ("['\"a, b\"']", ["a, b"]),
        ("['(lower(\"name\"))']", []),
        (['"order"', "Region"], ["order", "Region"]),
        ([], []),
        # DuckDB 1.1 and 1.2 render a quoted column without the single quotes around it
        ('[Region, "名称", "order"]', ["Region", "名称", "order"]),
        ("[café, Region]", ["café", "Region"]),
    ],
)
def test_catalog_index_columns_parse_every_quoting_form(value, names):
    from heavenbase.database.tables import _catalog_column_names

    assert _catalog_column_names(value) == names


@pytest.mark.fast
@pytest.mark.parametrize(
    ("definition", "names"),
    [
        ("CREATE INDEX ix_a ON t(sku);", ["sku"]),
        ('CREATE UNIQUE INDEX ix_b ON t("name", "Mixed Case", sku);', ["name", "Mixed Case", "sku"]),
        ('CREATE INDEX ix_c ON "main"."we(ird"("it\'s", "a""b", "a, b");', ["it's", 'a"b', "a, b"]),
        ('CREATE INDEX ix_d ON t((lower("name")));', []),
        ("CREATE INDEX ix_e ON t(((sku || 'a, b')));", []),
        ("CREATE INDEX ix_f ON t", []),
    ],
)
def test_catalog_index_columns_come_from_the_create_index_statement_when_the_catalog_lists_none(definition, names):
    """DuckDB before 1.1 leaves ``duckdb_indexes().expressions`` empty, so the statement names the columns."""
    from heavenbase.database.tables import _catalog_column_names

    assert _catalog_column_names(None, definition) == names


class LegacyProduct(hb.Entity):
    identifier = "scalar-index-legacy-product"
    sku = hb.field(hb.ShortText).store(to="orders")
    legacy_sku = hb.field(hb.ShortText).unique().store(to="orders")


class CurrentProduct(hb.Entity):
    identifier = "scalar-index-legacy-product"
    sku = hb.field(hb.ShortText).store(to="orders")


@pytest.mark.slow
@_FILE_BACKENDS
def test_a_registration_keeps_foreign_indexes_and_one_on_a_field_its_table_does_not_declare(tmp_path, backend_type):
    from heavenbase.utils.hash import md5hash

    ws = _file_orders(tmp_path, backend_type)
    try:
        ws.register(LegacyProduct)
        ws.upsert(LegacyProduct, {"object_id": "p1", "sku": "A-100", "legacy_sku": "L-1"})
        db = ws.backends.get("orders").db
        # an index someone else made, named like one of HeavenBase's
        foreign = f"ix_{md5hash(['someone else', 'sku'])}"
        db.execute(f"CREATE INDEX {foreign} ON scalar_index_legacy_product (sku)", readonly=False)
    finally:
        ws.release()
    ws = _file_orders(tmp_path, backend_type)
    try:
        ws.register(CurrentProduct)
        backend = ws.backends.get("orders")

        def reflected():
            return {item["name"]: item["column_names"] for item in backend.db.table_indexes(backend.tables[CurrentProduct.identifier])}

        stored = reflected()
        assert stored.get(foreign) == ["sku"] and ["legacy_sku"] in stored.values()
        ws.register(CurrentProduct, allow_replace=True)
        assert reflected() == stored
    finally:
        ws.release()
