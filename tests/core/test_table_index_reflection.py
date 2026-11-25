"""Index reflection finds what the server holds where SQLAlchemy's inspector reports nothing."""

import os
from uuid import uuid4

import pytest
import sqlalchemy as sa

import heavenbase as hb
from heavenbase.database import Database


@pytest.mark.fast
def test_duckdb_index_column_sets_count_the_primary_key(tmp_path):
    pytest.importorskip("duckdb_engine")
    db = Database(provider="duckdb", database=hb.utils.path_to_file_uri(tmp_path / "pk.duckdb"))
    try:
        table = sa.Table("orders", sa.MetaData(), sa.Column("object_id", sa.String(63), primary_key=True), sa.Column("status", sa.String(32)))
        db.create_table(table)
        assert db.index_column_sets(table)[("object_id",)] is True
    finally:
        db.dispose()


def _mysql_protocol_database(variable):
    url = os.environ.get(variable)
    if not url:
        pytest.skip(f"requires a dedicated test server in {variable}")
    pytest.importorskip("pymysql")
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    name = f"sql2_ix_{uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.exec_driver_sql(f"CREATE DATABASE `{name}`")
    return admin, admin.url.set(database=name), name


@pytest.mark.parametrize("variable", ["HVNB_SEEKDB_URL", "HVNB_OCEANBASE_URL", "HVNB_MYSQL_URL"])
def test_mysql_protocol_indexes_are_reflected_and_created_once(variable):
    admin, url, name = _mysql_protocol_database(variable)
    try:
        db = Database(provider="mysql", url=url.render_as_string(hide_password=False))
        try:
            table = sa.Table(
                "orders",
                sa.MetaData(),
                sa.Column("object_id", sa.String(63), primary_key=True),
                sa.Column("status", sa.String(32)),
                sa.Column("region", sa.String(32)),
            )
            sa.Index("ix_orders_status", table.c.status)
            sa.Index("ux_orders_region_status", table.c.region, table.c.status, unique=True)
            db.create_table(table)
            reflected = {item["name"]: (item["unique"], list(item["column_names"])) for item in db.table_indexes(table)}
            assert reflected == {"ix_orders_status": (False, ["status"]), "ux_orders_region_status": (True, ["region", "status"])}
            columns = db.index_column_sets(table)
            assert columns[("object_id",)] is True and columns[("status",)] is False and columns[("region", "status")] is True
            # a second ensure, as another workspace or a reopen runs it, creates nothing again
            db.create_missing_indexes(table)
            for index in table.indexes:
                db.create_index(index, checkfirst=True)
        finally:
            db.dispose()
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f"DROP DATABASE `{name}`")
        admin.dispose()
