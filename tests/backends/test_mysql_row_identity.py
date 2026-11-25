"""MySQL row identity is independent of the database default collation."""

import os
from uuid import uuid4

import pytest
import sqlalchemy as sa

import heavenbase as hb
from heavenbase.database import DatabaseError
from heavenbase.ext import RowOp


class MySQLNote(hb.Entity):
    identifier = "r9sqlx-mysql-note"
    title = hb.field(hb.ShortText)


class MySQLTaggedNote(hb.Entity):
    identifier = "r9sqlx-mysql-tagged-note"
    tags = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SideTable)


class MySQLCatalogItem(hb.Entity):
    identifier = "r10sql-mysql-catalog-item"
    name = hb.field(hb.ShortText)
    tags = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SideTable)
    specs = hb.field(hb.HyperG["slot", "value"]).store(to="main", strategy=hb.SideTable)


@pytest.fixture
def mysql_workspace(request):
    backend_type = getattr(request, "param", "mysql")
    url = os.environ.get("HVNB_MYSQL_URL")
    if not url:
        pytest.skip("requires a dedicated MySQL test database in HVNB_MYSQL_URL")
    engine = sa.create_engine(url)
    database = f"r9sqlx_ids_{uuid4().hex}"
    try:
        with engine.begin() as conn:
            no_pad = conn.exec_driver_sql("SHOW COLLATION WHERE Collation = 'utf8mb4_0900_ai_ci'").first() is not None
            collation = "utf8mb4_0900_ai_ci" if no_pad else "utf8mb4_general_ci"
            conn.exec_driver_sql(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE {collation}")
        try:
            test_url = engine.url.set(database=database)
            config = {"type": backend_type, "url": test_url.render_as_string(hide_password=False)}
            if backend_type == "seekdb":
                # seekdb fills in its own default server address, so name every part of it.
                config = {"type": backend_type, "host": test_url.host, "port": test_url.port, "username": test_url.username}
                config.update(password=test_url.password or "", database=database)
            ws = hb.HeavenBase(database, detached=True, backends={"main": config})
            try:
                assert ws.backends.get("main").db.spec.database == test_url.database
                yield ws, no_pad
            finally:
                ws.release()
        finally:
            with engine.begin() as conn:
                conn.exec_driver_sql(f"DROP DATABASE `{database}`")
    finally:
        engine.dispose()


@pytest.fixture
def mysql_notes(mysql_workspace):
    ws, _ = mysql_workspace
    ws.register(MySQLNote)
    return ws.backends.get("main"), MySQLNote.identifier


@pytest.mark.full
def test_mysql_upserts_preserve_case_distinct_ids(mysql_notes):
    backend, entity = mysql_notes
    backend.upsert([RowOp(entity, "ProductA", {"title": "first"})])
    backend.upsert([RowOp(entity, "producta", {"title": "second"})])
    expected = [{"object_id": "ProductA", "title": "first"}, {"object_id": "producta", "title": "second"}]
    assert backend.scan(entity) == expected
    assert backend.get_many(entity, ["ProductA", "producta", "PRODUCTA"]) == [*expected, None]
    assert list(backend.iter_scan(entity, batch_size=1)) == expected


@pytest.mark.full
@pytest.mark.parametrize("mysql_workspace", ["dolt", "seekdb", "oceanbase"], indirect=True)
def test_mysql_family_upserts_preserve_case_distinct_ids(mysql_workspace):
    ws, _ = mysql_workspace
    ws.register(MySQLNote)
    backend = ws.backends.get("main")
    entity = MySQLNote.identifier
    backend.upsert([RowOp(entity, "ProductA", {"title": "first"})])
    backend.upsert([RowOp(entity, "producta", {"title": "second"})])
    assert backend.scan(entity) == [{"object_id": "ProductA", "title": "first"}, {"object_id": "producta", "title": "second"}]
    backend.upsert([RowOp(entity, "producta", {}, op="delete")])
    assert backend.scan(entity) == [{"object_id": "ProductA", "title": "first"}]


@pytest.mark.full
def test_mysql_upsert_reports_case_conflict_in_existing_case_insensitive_table(mysql_notes):
    backend, entity = mysql_notes
    table = backend.tables[entity].name
    backend.db.execute(f"ALTER TABLE `{table}` MODIFY object_id VARCHAR(63) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL").close()
    backend.upsert([RowOp(entity, "Élan", {"title": "first"})])

    with pytest.raises(DatabaseError, match="Duplicate entry"):
        backend.upsert([RowOp(entity, "élan", {"title": "second"})])

    assert backend.scan(entity) == [{"object_id": "Élan", "title": "first"}]
    backend.upsert([RowOp(entity, "Élan", {"title": "updated"})])
    assert backend.scan(entity) == [{"object_id": "Élan", "title": "updated"}]


@pytest.mark.full
def test_mysql_new_tables_preserve_trailing_spaces_in_ids(mysql_workspace):
    ws, no_pad = mysql_workspace
    if not no_pad:
        pytest.skip("requires the MySQL 8.0 no-pad database collation")
    ws.register(MySQLTaggedNote)
    backend = ws.backends.get("main")
    entity = MySQLTaggedNote.identifier
    backend.upsert([RowOp(entity, "ProductA", {"tags": ["standard"]})])
    backend.upsert([RowOp(entity, "ProductA ", {"tags": ["imported"]})])
    expected = [{"object_id": "ProductA", "tags": ["standard"]}, {"object_id": "ProductA ", "tags": ["imported"]}]
    assert backend.scan(entity) == expected
    assert backend.get_many(entity, ["ProductA", "ProductA "]) == expected
    assert list(backend.iter_scan(entity, batch_size=1)) == expected
    backend.upsert([RowOp(entity, "ProductA ", {}, op="delete")])
    assert backend.scan(entity) == expected[:1]


@pytest.mark.full
def test_mysql_side_projection_handles_older_main_table_collation(mysql_workspace):
    ws, no_pad = mysql_workspace
    if not no_pad:
        pytest.skip("requires the MySQL 8.0 no-pad database collation")
    ws.register(MySQLTaggedNote)
    backend = ws.backends.get("main")
    entity = MySQLTaggedNote.identifier
    table = backend.tables[entity]
    backend.db.execute(f"ALTER TABLE `{table.name}` MODIFY object_id VARCHAR(63) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL").close()
    backend.upsert([RowOp(entity, "ProductA", {"tags": ["standard"]})])
    assert backend.get_many(entity, ["ProductA"]) == [{"object_id": "ProductA", "tags": ["standard"]}]


def _catalog_rows(count):
    colours = ["red", "green", "blue", "black", "white", "grey"]
    return [
        {
            "object_id": f"sku-{index:04d}",
            "name": f"Desk lamp {index}",
            "tags": ["lighting", colours[index % 6], f"batch-{index % 7}", "home"],
            "specs": [{"slot": "wattage", "value": f"{40 + index % 5}W"}, {"slot": "finish", "value": "brushed"}, {"slot": "base", "value": "oak"}],
        }
        for index in range(count)
    ]


@pytest.mark.full
def test_mysql_side_table_items_keep_their_written_order(mysql_workspace):
    ws, _ = mysql_workspace
    ws.register(MySQLCatalogItem)
    backend = ws.backends.get("main")
    entity = MySQLCatalogItem.identifier
    rows = _catalog_rows(120)
    ws.upsert_many(MySQLCatalogItem, rows)
    ws.set("sku-0003", {"tags": ["outdoor", "lighting", "garden"]}, entity=MySQLCatalogItem)
    rows[3]["tags"] = ["outdoor", "lighting", "garden"]

    def side_values(row):
        return {"object_id": row["object_id"], "tags": row["tags"], "specs": row["specs"]}

    expected = [side_values(row) for row in rows]
    assert [side_values(row) for row in backend.scan(entity)] == expected
    assert [side_values(row) for row in backend.iter_scan(entity, batch_size=50)] == expected
    assert [side_values(row) for row in backend.get_many(entity, [row["object_id"] for row in rows[:40]])] == expected[:40]
    assert ws.get("sku-0003", entity=MySQLCatalogItem)["tags"] == ["outdoor", "lighting", "garden"]
    assert sorted(ws.query(MySQLCatalogItem).where(MySQLCatalogItem.tags.array_contains("garden")).execute().ids) == ["sku-0003"]


def _side_table_lookups(backend, entity):
    table = backend.tables[entity]
    with backend._lock:
        statement = sa.select(*backend._select_cols(entity, table)).order_by(table.c.object_id)
    sql = str(statement.compile(dialect=backend.db.engine.dialect, compile_kwargs={"literal_binds": True}))
    side_tables = {spec.table.name for spec in backend._side_specs[entity].values()}
    with backend.db.engine.connect() as connection:
        plan = connection.exec_driver_sql("EXPLAIN " + sql).mappings().all()
    return {row["table"]: row["key"] for row in plan if row["table"] in side_tables}


@pytest.mark.full
@pytest.mark.parametrize("main_collation", [None, "utf8mb4_bin"])
def test_mysql_side_table_reads_use_the_object_id_index(mysql_workspace, main_collation):
    ws, _ = mysql_workspace
    ws.register(MySQLCatalogItem)
    backend = ws.backends.get("main")
    entity = MySQLCatalogItem.identifier
    if main_collation is not None:
        # A main table created by an earlier release keeps its own collation beside new side tables.
        backend.db.execute(
            f"ALTER TABLE `{backend.tables[entity].name}` MODIFY object_id VARCHAR(63) CHARACTER SET utf8mb4 COLLATE {main_collation} NOT NULL"
        ).close()
    ws.upsert_many(MySQLCatalogItem, _catalog_rows(60))
    lookups = _side_table_lookups(backend, entity)
    assert len(lookups) == 2
    assert all(key is not None for key in lookups.values()), lookups
    assert ws.get("sku-0005", entity=MySQLCatalogItem)["tags"] == ["lighting", "grey", "batch-5", "home"]


@pytest.fixture(params=["same-url", "separate-pool"])
def mysql_split_workspace(request):
    url = os.environ.get("HVNB_MYSQL_URL")
    if not url:
        pytest.skip("requires a dedicated MySQL test database in HVNB_MYSQL_URL")
    engine = sa.create_engine(url)
    database = f"r10sql_split_{uuid4().hex}"
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
        try:
            main = engine.url.set(database=database)
            secondary = main if request.param == "same-url" else main.update_query_dict({"connect_timeout": "20"})
            backends = {
                name: {"type": "mysql", "url": route.render_as_string(hide_password=False)} for name, route in (("main", main), ("secondary", secondary))
            }
            ws = hb.HeavenBase(database, detached=True, backends=backends)
            try:
                engines = {ws.backends.get(name).db.engine for name in backends}
                assert len(engines) == (1 if request.param == "same-url" else 2)
                yield ws
            finally:
                ws.release()
        finally:
            with engine.begin() as conn:
                conn.exec_driver_sql(f"DROP DATABASE `{database}`")
    finally:
        engine.dispose()


@pytest.mark.full
def test_mysql_routes_on_one_database_write_every_row_fragment(mysql_split_workspace):
    ws = mysql_split_workspace

    class Note(hb.Entity):
        identifier = "r10sql-mysql-split-note"
        title = hb.field(hb.ShortText).store(to="main")
        body = hb.field(hb.ShortText).required().store(to="secondary")
        tags = hb.field(hb.Array[hb.ShortText]).store(to="secondary", strategy=hb.SideTable)

    ws.register(Note)
    ws.upsert(Note, {"object_id": "n1", "title": "Quarterly report", "body": "Revenue grew in the third quarter.", "tags": ["finance", "q3"]})
    ws.set("n1", {"title": "Quarterly report (final)"}, entity=Note)
    assert {key: ws.get("n1", entity=Note)[key] for key in ("title", "body", "tags")} == {
        "title": "Quarterly report (final)",
        "body": "Revenue grew in the third quarter.",
        "tags": ["finance", "q3"],
    }
    assert ws.delete((Note, "n1"))
    assert ws.get("n1", entity=Note) is None


@pytest.mark.full
def test_mysql_routes_on_one_database_share_sparse_gram_fields(mysql_split_workspace):
    ws = mysql_split_workspace

    class Doc(hb.Entity):
        identifier = "r10sql-mysql-split-gram"
        primary_tags = hb.field(hb.Array[hb.ShortText]).required().store(to="main", strategy=hb.SparseGramIndex(gram_width=2))
        secondary_tags = hb.field(hb.Array[hb.ShortText]).required().store(to="secondary", strategy=hb.SparseGramIndex(gram_width=3))

    ws.register(Doc)

    def matches(field, query):
        return ws.query(Doc).where(field.contained_in(query)).execute().ids

    ws.upsert(Doc, {"object_id": "invoice-104", "primary_tags": ["invoice"], "secondary_tags": ["finance"]})
    assert matches(Doc.primary_tags, "open invoice") == ["invoice-104"]
    assert matches(Doc.secondary_tags, "contact finance") == ["invoice-104"]
    ws.set("invoice-104", {"primary_tags": ["receipt"]}, entity=Doc)
    assert matches(Doc.primary_tags, "open invoice") == []
    assert matches(Doc.primary_tags, "open receipt") == ["invoice-104"]
    assert matches(Doc.secondary_tags, "contact finance") == ["invoice-104"]
    assert ws.delete((Doc, "invoice-104"))
    assert matches(Doc.secondary_tags, "contact finance") == []
