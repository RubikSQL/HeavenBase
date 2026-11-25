"""A SQL registration keeps the scalar indexes of the entity's other routes and of other workspaces on the same table."""

import os
import subprocess
import sys
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url

import heavenbase as hb


def _account():
    class Account(hb.Entity):
        identifier = "index-routes-account"
        email = hb.field(hb.ShortText).required().store(to="main").unique()
        city = hb.field(hb.ShortText).optional().store(to="main")
        plan = hb.field(hb.ShortText).optional().store(to="second")
        seats = hb.field(hb.Integer).optional().store(to="second")

    return Account


def _member(*, full):
    class Member(hb.Entity):
        identifier = "index-routes-member"
        name = hb.field(hb.ShortText).required()
        if full:
            email = hb.field(hb.ShortText).optional().unique()
            rank = hb.field(hb.Integer).default(0)

    return Member


def _altered_member():
    class Member(hb.Entity):
        identifier = "index-routes-member"
        name = hb.field(hb.ShortText).required()
        city = hb.field(hb.ShortText).optional()

    return Member


@contextmanager
def _database(kind, tmp_path):
    """Yield a workspace id, a backend config and, on a server, the URL of the fresh database that workspace is kept in."""
    if kind in ("sqlite", "duckdb"):
        if kind == "duckdb":
            pytest.importorskip("duckdb_engine")
        yield "index-routes", {"type": kind, "database": hb.utils.path_to_file_uri(tmp_path / f"shared.{kind}")}, None
        return
    variable = {"postgres": "HVNB_POSTGRES_URL", "mysql": "HVNB_MYSQL_URL", "mariadb": "HVNB_MARIADB_URL"}[kind]
    if variable not in os.environ:
        pytest.skip(f"requires a dedicated test server in {variable}")
    pytest.importorskip("psycopg2" if kind == "postgres" else "pymysql")
    root = make_url(os.environ[variable])
    admin = create_engine(root.set(database="postgres") if kind == "postgres" else root, isolation_level="AUTOCOMMIT")
    name = f"r18ix_{uuid4().hex[:12]}"
    with admin.connect() as connection:
        connection.exec_driver_sql(f"CREATE DATABASE {name}")
    try:
        url = admin.url.set(database=name)
        yield name.replace("_", "-"), {"type": "postgres" if kind == "postgres" else "mysql", "url": url.render_as_string(hide_password=False)}, url
    finally:
        with admin.connect() as connection:
            connection.exec_driver_sql(f"DROP DATABASE {name}" + (" WITH (FORCE)" if kind == "postgres" else ""))
        admin.dispose()


@contextmanager
def _index_ddl():
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith(("CREATE INDEX", "CREATE UNIQUE INDEX", "DROP INDEX")):
            statements.append(statement.strip())

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", record)


def _indexes(ws, entity, route="main"):
    backend = ws.backends.get(route)
    return sorted((",".join(item["column_names"]), bool(item["unique"])) for item in backend.db.table_indexes(backend.tables[entity.identifier]))


_KINDS = pytest.mark.parametrize(
    "kind",
    [pytest.param("sqlite", marks=pytest.mark.fast), *(pytest.param(kind, marks=pytest.mark.slow) for kind in ("duckdb", "postgres", "mysql", "mariadb"))],
)


@pytest.mark.slow
@_KINDS
def test_split_routes_on_one_database_keep_each_others_indexes_and_constraints(tmp_path, kind):
    with _database(kind, tmp_path) as (name, config, _):
        for attempt in range(2):
            ws = hb.HeavenBase(name, detached=True, backends={"main": dict(config), "second": dict(config)})
            try:
                Account = _account()
                with _index_ddl() as ddl:
                    ws.register(Account)
                assert _indexes(ws, Account) == [("city", False), ("email", True), ("plan", False), ("seats", False)]
                if attempt:
                    assert [statement for statement in ddl if "index_routes_account" in statement] == []
                else:
                    ws.upsert(Account, {"object_id": "1", "email": "a@x.org", "plan": "pro", "city": "Rome", "seats": 2})
                with pytest.raises(Exception, match="(?i)unique|duplicate"):
                    ws.upsert(Account, {"object_id": f"{attempt + 2}", "email": "a@x.org", "plan": "free"})
                assert [(row["object_id"], row["email"], row["plan"]) for row in ws.rows(Account)] == [("1", "a@x.org", "pro")]
            finally:
                ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("kind", [pytest.param("sqlite", marks=pytest.mark.fast), pytest.param("duckdb", marks=pytest.mark.slow)])
def test_a_workspace_whose_definition_lacks_a_field_keeps_another_workspaces_indexes_on_it(tmp_path, kind):
    with _database(kind, tmp_path) as (_, config, _):
        newer = hb.HeavenBase("index-routes-new", detached=True, backends={"main": dict(config)})
        older = hb.HeavenBase("index-routes-old", detached=True, backends={"main": dict(config)})
        New, Old = _member(full=True), _member(full=False)
        try:
            newer.register(New)
            before = _indexes(newer, New)
            assert before == [("email", True), ("name", False), ("rank", False)]
            newer.upsert(New, {"object_id": "1", "name": "Ada", "email": "a@x.org", "rank": 4})
            older.register(Old)
            older.register(Old, allow_replace=True)
            assert older.get("1", entity=Old)["name"] == "Ada"
            assert _indexes(newer, New) == before
            with pytest.raises(Exception, match="(?i)unique|duplicate"):
                newer.upsert(New, {"object_id": "2", "name": "Bob", "email": "a@x.org"})
        finally:
            older.release()
            newer.release()
        # the older workspace opens again: what it stored before never named the field
        older = hb.HeavenBase("index-routes-old", detached=True, backends={"main": dict(config)})
        try:
            older.register(Old)
            assert _indexes(older, Old) == before
        finally:
            older.release()


@pytest.mark.slow
@_KINDS
def test_a_field_removed_in_a_new_process_keeps_its_indexes_and_one_removed_live_loses_them(tmp_path, kind):
    """As in 0.1.2.2: only an index on columns the cached table declares is dropped, and none on DuckDB."""
    full, less = [("email", True), ("name", False), ("rank", False)], [("name", False)]
    with _database(kind, tmp_path) as (name, config, _):
        steps = [(True, {}, full), (False, {}, full), (False, {"allow_replace": True}, full), (True, {}, full)]
        for position, (definition, options, expected) in enumerate(steps):
            ws = hb.HeavenBase(name, detached=True, backends={"main": dict(config)})
            try:
                Member = _member(full=definition)
                ws.register(Member, **options)
                assert _indexes(ws, Member) == expected
                ws.upsert(Member, {"object_id": str(position), "name": "Ada"})
                if position == len(steps) - 1:
                    Less = _member(full=False)
                    ws.register(Less, allow_replace=True)
                    assert _indexes(ws, Less) == (full if kind == "duckdb" else less)
            finally:
                ws.release()


@_KINDS
def test_an_older_definition_of_the_same_workspace_keeps_the_newer_ones_constraint(tmp_path, kind):
    """A second process of one workspace registers the definition of an older release; the first one's constraint still holds."""
    with _database(kind, tmp_path) as (name, config, _):
        newer = hb.HeavenBase(name, detached=True, backends={"main": dict(config)})
        older = hb.HeavenBase(name, detached=True, backends={"main": dict(config)})
        New, Old = _member(full=True), _member(full=False)
        try:
            newer.register(New)
            before = _indexes(newer, New)
            newer.upsert(New, {"object_id": "1", "name": "Ada", "email": "a@x.org"})
            older.register(Old)
            older.upsert(Old, {"object_id": "9", "name": "Eve"})
            older.alter_entity(_altered_member(), confirm=Old.identifier)
            older.register(_altered_member(), allow_replace=True)
            assert set(before) <= set(_indexes(newer, New))
            with pytest.raises(Exception, match="(?i)unique|duplicate"):
                newer.upsert(New, {"object_id": "2", "name": "Bob", "email": "a@x.org"})
        finally:
            older.release()
            newer.release()
        newer = hb.HeavenBase(name, detached=True, backends={"main": dict(config)})
        try:
            newer.register(New)
            assert sorted(newer.ids(New)) == ["1", "9"]
        finally:
            newer.release()


@pytest.mark.slow
@pytest.mark.parametrize("kind", [pytest.param("sqlite", marks=pytest.mark.fast), pytest.param("duckdb", marks=pytest.mark.slow)])
def test_a_workspace_that_registered_a_field_last_keeps_it_indexed_for_the_other_one(tmp_path, kind):
    with _database(kind, tmp_path) as (_, config, _):
        New, Old = _member(full=True), _member(full=False)
        first = hb.HeavenBase("index-routes-a", detached=True, backends={"main": dict(config)})
        try:
            first.register(New)
            first.upsert(New, {"object_id": "1", "name": "Ada", "email": "a@x.org"})
            for definition, options in ((New, {}), (Old, {}), (Old, {"allow_replace": True})):
                other = hb.HeavenBase("index-routes-b", detached=True, backends={"main": dict(config)})
                try:
                    other.register(definition, **options)
                finally:
                    other.release()
            with pytest.raises(Exception, match="(?i)unique|duplicate"):
                first.upsert(New, {"object_id": "2", "name": "Bob", "email": "a@x.org"})
            assert sorted(first.ids(New)) == ["1"]
        finally:
            first.release()


_DUCKDB_CHILD = """
import os, sys, warnings
from pathlib import Path
warnings.simplefilter("ignore")
import heavenbase as hb

step, path, how = sys.argv[1:4]

def customer(full):
    class Customer(hb.Entity):
        identifier = "index-routes-customer"
        name = hb.field(hb.ShortText).required()
        if full:
            email = hb.field(hb.ShortText).optional().unique()
            rank = hb.field(hb.Integer).default(0)
    return Customer

ws = hb.HeavenBase("shop", detached=True, backends={"main": {"type": "duckdb", "database": hb.utils.path_to_file_uri(Path(path))}})
if step == "write":
    Full, Less = customer(True), customer(False)
    ws.register(Full)
    ws.upsert_many(Full, [{"object_id": f"c{n}", "name": f"N{n}", "email": f"e{n}@x.org"} for n in range(20)])
    ws.register(Less, allow_replace=True)
    ws.upsert(Less, {"object_id": "c20", "name": "later"})
elif step == "less":
    Less = customer(False)
    ws.register(Less, allow_replace=True)
    ws.upsert(Less, {"object_id": "c21", "name": "cold"})
else:
    Less = customer(False)
    ws.register(Less)
    print("rows", len(ws.ids(Less)))
if how == "kill":
    os._exit(0)
ws.release()
"""


@pytest.mark.slow
@pytest.mark.parametrize("how", ["release", "kill"])
def test_a_duckdb_file_opens_after_a_field_was_removed_and_the_process_ended(tmp_path, home_env, how):
    """DuckDB cannot replay a log that drops indexes and then writes rows, so a registration drops none there."""
    pytest.importorskip("duckdb_engine")
    env = home_env(tmp_path / "home")

    def run(step, end):
        command = [sys.executable, "-c", _DUCKDB_CHILD, step, str(tmp_path / "shop.duckdb"), end]
        return subprocess.run(command, env=env, cwd=tmp_path, text=True, capture_output=True, timeout=180)

    for step, end in (("write", how), ("less", how)):
        result = run(step, end)
        assert result.returncode == 0, result.stdout + result.stderr
    result = run("read", "release")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "rows 22" in result.stdout


def _limited_login(admin_url, kind, user):
    """Create a login that may read and write the rows of the database and nothing else; return its URL."""
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    rights = "SELECT, INSERT, UPDATE, DELETE"
    with admin.connect() as connection:
        if kind == "postgres":
            connection.exec_driver_sql(f"CREATE ROLE {user} LOGIN PASSWORD 'R18-test-pw'")
            connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {user}")
            connection.exec_driver_sql(f"GRANT {rights} ON ALL TABLES IN SCHEMA public TO {user}")
        else:
            connection.exec_driver_sql(f"CREATE USER '{user}'@'%%' IDENTIFIED BY 'R18-test-pw'")
            connection.exec_driver_sql(f"GRANT {rights} ON `{admin_url.database}`.* TO '{user}'@'%%'")
    admin.dispose()
    return admin_url.set(username=user, password="R18-test-pw")


def _drop_login(admin_url, kind, user):
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        if kind == "postgres":
            connection.exec_driver_sql(f"DROP OWNED BY {user}")
            connection.exec_driver_sql(f"DROP ROLE {user}")
        else:
            connection.exec_driver_sql(f"DROP USER '{user}'@'%%'")
    admin.dispose()


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["postgres", "mysql", "mariadb"])
@pytest.mark.parametrize("shape", ["split", "older definition"])
def test_a_login_that_may_only_write_rows_opens_the_entity(tmp_path, kind, shape):
    with _database(kind, tmp_path) as (name, config, url):
        stored, opened = (_account(), _account()) if shape == "split" else (_member(full=True), _member(full=False))
        routes = ("main", "second") if shape == "split" else ("main",)
        ws = hb.HeavenBase(name, detached=True, backends={route: dict(config) for route in routes})
        try:
            ws.register(stored)
        finally:
            ws.release()
        user = f"r18{uuid4().hex[:8]}"
        login = {**config, "url": _limited_login(url, kind, user).render_as_string(hide_password=False)}
        try:
            ws = hb.HeavenBase(name, detached=True, backends={route: dict(login) for route in routes})
            try:
                ws.register(opened)
                row = {"email": "a@x.org", "plan": "pro"} if shape == "split" else {"name": "Ada"}
                ws.upsert(opened, {"object_id": "1", **row})
                assert {key: ws.get("1", entity=opened)[key] for key in row} == row
            finally:
                ws.release()
        finally:
            _drop_login(url, kind, user)
