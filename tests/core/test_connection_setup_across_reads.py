"""Connection initialization survives the end of a read-only transaction."""

import os
from uuid import uuid4

import pytest
import sqlalchemy as sa

import heavenbase as hb


@pytest.mark.fast
@pytest.mark.parametrize("pool_class", ["null", "queue"])
def test_sqlite_connection_setup_persists_across_reads(tmp_path, pool_class):
    db = hb.Database(
        provider="sqlite",
        database=(tmp_path / "setup.db").as_uri(),
        pool={"pool_class": pool_class},
        pragmas=["CREATE TEMP TABLE defaults (value TEXT)", "INSERT INTO defaults VALUES ('standard')"],
    )
    try:
        assert [db.execute("SELECT value FROM defaults", readonly=True).to_lists() for _ in range(2)] == [[["standard"]], [["standard"]]]
    finally:
        db.dispose()


@pytest.mark.full
@pytest.mark.parametrize("pool_class", ["null", "queue"])
@pytest.mark.parametrize("scope", ["session", "local"])
def test_postgres_connection_setup_persists_across_reads(pool_class, scope):
    url = os.environ.get("HVNB_POSTGRES_URL")
    if not url:
        pytest.skip("requires a dedicated PostgreSQL test database in HVNB_POSTGRES_URL")
    suffix = uuid4().hex
    schema, table = f"r9sqlx_setup_{suffix}", f"r9sqlx_notes_{suffix}"
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f"CREATE SCHEMA {schema}")
            conn.exec_driver_sql(f"CREATE TABLE {schema}.{table} (title TEXT)")
            conn.exec_driver_sql(f"INSERT INTO {schema}.{table} VALUES ('configured')")
            conn.exec_driver_sql(f"CREATE TABLE public.{table} (title TEXT)")
            conn.exec_driver_sql(f"INSERT INTO public.{table} VALUES ('public')")
        try:
            pragmas = (
                ["SET search_path TO public", f"/* catalog override */ SET LOCAL search_path TO {schema}, public"]
                if scope == "local"
                else [f"SET search_path TO {schema}, public"]
            )
            db = hb.Database(provider="postgres", url=url, pragmas=pragmas, pool={"pool_class": pool_class})
            try:
                query = f"SELECT title FROM {table}"
                for _ in range(2):
                    assert db.execute(query, readonly=True).to_lists() == [["configured"]]
                with db.tx():
                    assert db.execute(query, readonly=True).to_lists() == [["configured"]]
                    db.commit()
                    assert db.execute(query, readonly=True).to_lists() == [["configured"]]
                    db.rollback()
                    assert db.execute(query, readonly=True).to_lists() == [["configured"]]
            finally:
                db.dispose()
        finally:
            with engine.begin() as conn:
                conn.exec_driver_sql(f"DROP TABLE public.{table}")
                conn.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
    finally:
        engine.dispose()
