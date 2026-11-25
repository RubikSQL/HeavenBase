"""SQL server semantics: storage engines and atomic upserts, exact IDs, routes that share a table."""

import os
from contextlib import contextmanager
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

import heavenbase as hb
from heavenbase.backends.sql.base import SQLBackend
from heavenbase.ext import RowOp


@contextmanager
def _mysql_database(collation="utf8mb4_0900_ai_ci"):
    url = os.environ.get("HVNB_MYSQL_URL")
    if not url:
        pytest.skip("requires a dedicated MySQL test server in HVNB_MYSQL_URL")
    pytest.importorskip("pymysql")
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    name = f"sql2_{uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.exec_driver_sql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE {collation}")
    try:
        yield admin, admin.url.set(database=name)
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f"DROP DATABASE `{name}`")
        admin.dispose()


@contextmanager
def _postgres_database():
    pytest.importorskip("psycopg2")
    url = os.environ.get("HVNB_POSTGRES_URL")
    if not url:
        pytest.skip("requires a PostgreSQL test server in HVNB_POSTGRES_URL")
    admin = sa.create_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = f"sql2_{uuid4().hex[:12]}"
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f"CREATE DATABASE \"{name}\" TEMPLATE template0 LC_COLLATE 'en_US.utf8' LC_CTYPE 'en_US.utf8'")
    except sa.exc.DBAPIError:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    try:
        yield admin, admin.url.set(database=name)
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.dispose()


class Note(hb.Entity):
    identifier = "sql2-note"
    title = hb.field(hb.ShortText)


def test_a_mysql_table_without_transactions_withdraws_the_atomic_upsert_claim():
    with _mysql_database("utf8mb4_0900_bin") as (admin, url):
        with admin.connect() as conn:
            conn.exec_driver_sql(f"CREATE TABLE `{url.database}`.`sql2_note` (object_id VARCHAR(63) NOT NULL PRIMARY KEY, title VARCHAR(100)) ENGINE=MyISAM")
        ws = hb.HeavenBase(url.database, detached=True, backends={"main": {"type": "mysql", "url": url.render_as_string(hide_password=False)}})
        try:
            backend = ws.backends.get("main")
            assert backend.tag("atomic-upsert").value is True
            ws.register(Note, catalog="none")
            assert backend.tag("atomic-upsert").value is False
            with pytest.raises(ValueError, match="atomic-upsert"):
                ws.upsert_many(Note, [{"object_id": "n1", "title": "first"}], atomic=True)
            assert ws.get("n1", entity=Note) is None
            ws.upsert(Note, {"object_id": "n1", "title": "first"})
            assert ws.get("n1", entity=Note)["title"] == "first"
            # maintenance of the Catalog, whose own table has transactions, still runs
            ws.register(Note, catalog="full", allow_replace=True)
            backend.delete(hb.Catalog.identifier, [ws._catalog.row_id(Note.schema(), "n1")])
            assert ws.repair()["catalog"]["missing_written"] == 1
            with admin.connect() as conn:
                conn.exec_driver_sql(f"ALTER TABLE `{url.database}`.`sql2_note` ENGINE=InnoDB")
            ws.register(Note, catalog="full", allow_replace=True)
            assert backend.tag("atomic-upsert").value is True
            ws.upsert_many(Note, [{"object_id": "n2", "title": "second"}], atomic=True)
        finally:
            ws.release()


def test_a_mysql_table_with_transactions_keeps_the_atomic_upsert_claim():
    with _mysql_database("utf8mb4_0900_bin") as (_, url):
        ws = hb.HeavenBase(url.database, detached=True, backends={"main": {"type": "mysql", "url": url.render_as_string(hide_password=False)}})
        try:
            ws.register(Note)
            assert ws.backends.get("main").tag("atomic-upsert").value is True
            ws.upsert_many(Note, [{"object_id": "n1", "title": "first"}], atomic=True)
            assert ws.get("n1", entity=Note)["title"] == "first"
        finally:
            ws.release()


def test_the_generic_sql_type_on_a_case_insensitive_table_never_replaces_another_spelling():
    with _mysql_database() as (_, url):
        backend = SQLBackend("main", provider="mysql", identifier="sql", url=url.render_as_string(hide_password=False))
        ws = hb.HeavenBase(url.database, detached=True, backends={"main": backend})
        try:
            ws.register(Note, catalog="none")
            ws.upsert(Note, {"object_id": "alpha", "title": "lower"})
            with pytest.raises(ValueError, match="collation"):
                ws.upsert(Note, {"object_id": "ALPHA", "title": "upper"})
            assert [(row["object_id"], row["title"]) for row in ws.iter_rows(Note)] == [("alpha", "lower")]
            assert ws.get("ALPHA", entity=Note) is None
            assert not ws.delete((Note, "ALPHA"))
            assert [(row["object_id"], row["title"]) for row in ws.iter_rows(Note)] == [("alpha", "lower")]
            ws.upsert(Note, {"object_id": "alpha", "title": "again"})
            assert ws.get("alpha", entity=Note)["title"] == "again"
            # a call that first deletes the stored spelling may write the other one
            backend.upsert([RowOp(Note.identifier, "alpha", op="delete"), RowOp(Note.identifier, "ALPHA", {"title": "renamed"})])
            assert [row["object_id"] for row in ws.iter_rows(Note)] == ["ALPHA"]
            assert ws.delete((Note, "ALPHA"))
            assert list(ws.iter_rows(Note)) == []
        finally:
            ws.release()


def test_split_routes_of_the_generic_sql_type_on_one_server_write_each_row_together():
    with _mysql_database("utf8mb4_0900_bin") as (_, url):

        class Split(hb.Entity):
            identifier = "sql2-generic-split"
            title = hb.field(hb.ShortText).store(to="main")
            body = hb.field(hb.ShortText).required().store(to="secondary")

        main = SQLBackend("main", provider="mysql", identifier="sql", url=url.render_as_string(hide_password=False))
        secondary = SQLBackend(
            "secondary", provider="mysql", identifier="sql", url=url.update_query_dict({"connect_timeout": "20"}).render_as_string(hide_password=False)
        )
        ws = hb.HeavenBase(url.database, detached=True, backends={"main": main, "secondary": secondary})
        try:
            assert main.db.engine is not secondary.db.engine
            ws.register(Split, catalog="none")
            ws.upsert(Split, {"object_id": "n1", "title": "Quarterly report", "body": "Revenue grew."})
            ws.set("n1", {"title": "Quarterly report (final)"}, entity=Split)
            assert {key: ws.get("n1", entity=Split)[key] for key in ("title", "body")} == {"title": "Quarterly report (final)", "body": "Revenue grew."}
            with main.db.engine.connect() as conn:
                names = conn.exec_driver_sql(f"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = '{url.database}'").scalars().all()
            assert not [name for name in names if name.startswith("hb_probe_")]
        finally:
            ws.release()


def test_split_generic_routes_on_separate_databases_stay_separate():
    with _mysql_database("utf8mb4_0900_bin") as (_, first), _mysql_database("utf8mb4_0900_bin") as (_, second):

        class Split(hb.Entity):
            identifier = "sql2-generic-apart"
            title = hb.field(hb.ShortText).store(to="main")
            body = hb.field(hb.ShortText).store(to="secondary")

        main = SQLBackend("main", database=first.database, provider="mysql", identifier="sql", url=first.render_as_string(hide_password=False))
        secondary = SQLBackend("secondary", database=second.database, provider="mysql", identifier="sql", url=second.render_as_string(hide_password=False))
        ws = hb.HeavenBase(first.database, detached=True, backends={"main": main, "secondary": secondary})
        statements = []
        sa.event.listen(main.db.engine, "before_cursor_execute", lambda conn, cursor, statement, *args: statements.append(statement))
        try:
            ws.register(Split, catalog="none")
            assert not [statement for statement in statements if "hb_probe_" in statement]
            assert main.db.execute("SELECT DATABASE()", readonly=True).scalar() != secondary.db.execute("SELECT DATABASE()", readonly=True).scalar()
            assert main._row_write_routes(Split.identifier) == ("main",)
            assert secondary._row_write_routes(Split.identifier) == ("secondary",)
            ws.upsert(Split, {"object_id": "n1", "title": "T", "body": "B"})
            assert {key: ws.get("n1", entity=Split)[key] for key in ("title", "body")} == {"title": "T", "body": "B"}
        finally:
            ws.release()


def test_postgres_routes_with_other_logins_that_write_one_table_write_each_row_together():
    with _postgres_database() as (admin, url):
        role, password = f"sql2_{uuid4().hex[:8]}", "Sql2-test-pw"
        with admin.connect() as conn:
            conn.exec_driver_sql(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
        try:
            owner = sa.create_engine(url, isolation_level="AUTOCOMMIT")
            with owner.connect() as conn:
                # a member of the owning role may change the tables it created, as a team's application roles do
                conn.exec_driver_sql(f"GRANT {url.username} TO {role}")
                conn.exec_driver_sql(f"GRANT ALL ON SCHEMA public TO {role}")
                conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {role}")
            owner.dispose()

            class Split(hb.Entity):
                identifier = "sql2-pg-roles"
                title = hb.field(hb.ShortText).store(to="main")
                body = hb.field(hb.ShortText).required().store(to="secondary")

            backends = {
                "main": {"type": "postgres", "url": url.render_as_string(hide_password=False)},
                "secondary": {"type": "postgres", "url": url.set(username=role, password=password).render_as_string(hide_password=False)},
            }
            ws = hb.HeavenBase(url.database.replace("_", "-"), detached=True, backends=backends)
            try:
                ws.register(Split, catalog="none")
                ws.upsert(Split, {"object_id": "n1", "title": "Quarterly report", "body": "Revenue grew."})
                ws.set("n1", {"title": "Quarterly report (final)"}, entity=Split)
                assert {key: ws.get("n1", entity=Split)[key] for key in ("title", "body")} == {"title": "Quarterly report (final)", "body": "Revenue grew."}
            finally:
                ws.release()
        finally:
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')
                conn.exec_driver_sql(f"DROP ROLE {role}")


def _gram_split():
    class Doc(hb.Entity):
        identifier = "sql2-gram-split"
        title = hb.field(hb.ShortText).store(to="main")
        first = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(gram_width=2))
        second = hb.field(hb.Array[hb.ShortText]).store(to="secondary", strategy=hb.SparseGramIndex(gram_width=3))

    return Doc


def _check_gram_split(ws, Doc):
    def matches(field, query):
        return ws.query(Doc).where(field.contained_in(query)).execute().ids

    ws.register(Doc, catalog="none")
    ws.upsert(Doc, {"object_id": "d1", "title": "Invoice", "first": ["invoice"], "second": ["finance"]})
    ws.set("d1", {"first": ["receipt"]}, entity=Doc)
    row = ws.get("d1", entity=Doc)
    assert (row["title"], row["first"], row["second"]) == ("Invoice", ["receipt"], ["finance"])
    assert matches(Doc.first, "open invoice") == [] and matches(Doc.first, "open receipt") == ["d1"]
    assert matches(Doc.second, "contact finance") == ["d1"]
    assert ws.delete((Doc, "d1")) and ws.get("d1", entity=Doc) is None


def test_split_keyword_fields_of_the_generic_sql_type_on_one_server_keep_both_indexes():
    with _mysql_database("utf8mb4_0900_bin") as (_, url):
        main = SQLBackend("main", provider="mysql", identifier="sql", url=url.render_as_string(hide_password=False))
        secondary = SQLBackend(
            "secondary", provider="mysql", identifier="sql", url=url.update_query_dict({"connect_timeout": "20"}).render_as_string(hide_password=False)
        )
        ws = hb.HeavenBase(url.database, detached=True, backends={"main": main, "secondary": secondary})
        try:
            _check_gram_split(ws, _gram_split())
        finally:
            ws.release()


@pytest.mark.parametrize("extra", ["", ", CREATE"], ids=["no CREATE", "CREATE without DROP"])
def test_generic_routes_register_through_a_login_that_may_not_create_or_drop_tables(extra):
    """The table made for the shared-table check never fails a registration that only needs data rights, as in 0.1.2.2."""
    with _mysql_database("utf8mb4_0900_bin") as (admin, first), _mysql_database("utf8mb4_0900_bin") as (_, second):

        class Both(hb.Entity):
            identifier = "sql2-data-rights"
            title = hb.field(hb.ShortText).store(to="left").store(to="right")  # right's columns are a subset of left's
            body = hb.field(hb.ShortText).store(to="left")

        def routes(**login):
            return {
                name: SQLBackend(name, database=url.database, provider="mysql", identifier="sql", url=url.set(**login).render_as_string(hide_password=False))
                for name, url in (("left", first), ("right", second))
            }

        user = f"sql2{uuid4().hex[:8]}"
        with admin.connect() as conn:
            conn.exec_driver_sql(f"CREATE USER '{user}'@'%%' IDENTIFIED BY 'Sql2-test-pw'")
            for url in (first, second):
                conn.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE, DELETE{extra} ON `{url.database}`.* TO '{user}'@'%%'")
        try:
            ws = hb.HeavenBase("sql2-data-rights", detached=True, backends=routes())
            try:
                ws.register(Both, catalog="none")
            finally:
                ws.release()
            ws = hb.HeavenBase("sql2-data-rights", detached=True, backends=routes(username=user, password="Sql2-test-pw"))
            try:
                if extra:
                    with pytest.warns(RuntimeWarning, match="hb_probe_"):
                        ws.register(Both, catalog="none")
                else:
                    ws.register(Both, catalog="none")
                ws.upsert(Both, {"object_id": "b1", "title": "T", "body": "B"})
                assert {key: ws.get("b1", entity=Both)[key] for key in ("title", "body")} == {"title": "T", "body": "B"}
                assert ws.backends.get("left")._row_write_routes(Both.identifier) == ("left",)
            finally:
                ws.release()
        finally:
            with admin.connect() as conn:
                conn.exec_driver_sql(f"DROP USER '{user}'@'%%'")
