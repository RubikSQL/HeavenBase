"""SQL routes that split an entity on one database agree that they share it, even when the database is new.

The routes decide which of them share a database once every route's storage
DDL has run, so a database file or sparse-GRAM table that the first route
creates does not change the answer of a later route.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import heavenbase as hb


def _symlink(target, link):
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links are unavailable here: {exc}")


def _file_routes(tmp_path, backend_type, reach):
    """Return route configs for one new database file reached through two separately configured routes."""
    if backend_type == "duckdb":
        pytest.importorskip("duckdb_engine")
    (tmp_path / "data").mkdir()
    shared = tmp_path / "data" / "shared.db"
    second = {"type": backend_type, "database": hb.utils.path_to_file_uri(shared)}
    if reach == "file-link":
        _symlink(shared, tmp_path / "link.db")
        second["database"] = hb.utils.path_to_file_uri(tmp_path / "link.db")
    elif reach == "directory-link":
        _symlink(tmp_path / "data", tmp_path / "linked-data")
        second["database"] = hb.utils.path_to_file_uri(tmp_path / "linked-data" / "shared.db")
    else:
        second["pool"] = {"pool_size": 2}
    assert not shared.exists()
    return {
        "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "system.db")},
        "left": {"type": backend_type, "database": hb.utils.path_to_file_uri(shared)},
        "right": second,
    }


def _assert_routes_agree(ws, entity_id):
    routes = {name: ws.backends.get(name)._row_write_routes(entity_id) for name in ("left", "right")}
    assert routes == {"left": ("left", "right"), "right": ("left", "right")}


@pytest.mark.slow
@pytest.mark.parametrize("backend_type", ["sqlite", "duckdb"])
@pytest.mark.parametrize("reach", ["file-link", "directory-link", "pool-setting"])
def test_split_routes_on_a_new_file_keep_every_fragment(tmp_path, backend_type, reach):
    # DuckDB cannot add a NOT NULL column to the table the other route created, so only SQLite requires the fields.
    def text_field(route):
        field = hb.field(hb.ShortText)
        return (field.required() if backend_type == "sqlite" else field).store(to=route)

    class Note(hb.Entity):
        identifier = "split-new-file-note"
        title = text_field("left")
        body = text_field("right")

    ws = hb.HeavenBase(f"split-new-file-{backend_type}-{reach}", detached=True, backends=_file_routes(tmp_path, backend_type, reach))
    try:
        ws.register(Note)
        _assert_routes_agree(ws, Note.identifier)
        ws.upsert(Note, {"object_id": "n1", "title": "Quarterly report", "body": "Revenue grew in the third quarter."})
        ws.set("n1", {"title": "Quarterly report (final)"}, entity=Note)
        assert {key: ws.get("n1", entity=Note)[key] for key in ("title", "body")} == {
            "title": "Quarterly report (final)",
            "body": "Revenue grew in the third quarter.",
        }
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("backend_type", ["sqlite", "duckdb"])
def test_split_sparse_gram_routes_on_a_new_file_keep_both_indexes(tmp_path, backend_type):
    class Doc(hb.Entity):
        identifier = "split-new-file-gram-doc"
        title = hb.field(hb.ShortText).store(to="left")
        primary_tags = hb.field(hb.Array[hb.ShortText]).store(to="left", strategy=hb.SparseGramIndex(gram_width=2))
        secondary_tags = hb.field(hb.Array[hb.ShortText]).store(to="right", strategy=hb.SparseGramIndex(gram_width=2))

    ws = hb.HeavenBase(f"split-new-file-gram-{backend_type}", detached=True, backends=_file_routes(tmp_path, backend_type, "file-link"))
    try:
        ws.register(Doc)
        _assert_routes_agree(ws, Doc.identifier)
        ws.upsert(Doc, {"object_id": "invoice-104", "title": "Invoice 104", "primary_tags": ["invoice"], "secondary_tags": ["finance"]})
        assert ws.get("invoice-104", entity=Doc)["title"] == "Invoice 104"
        assert ws.query(Doc).where(Doc.primary_tags.contained_in("open invoice")).execute().ids == ["invoice-104"]
        assert ws.query(Doc).where(Doc.secondary_tags.contained_in("contact finance")).execute().ids == ["invoice-104"]
    finally:
        ws.release()


_DEFAULT_POSTGRES_URL = "postgresql+psycopg2://rubik:password@localhost:5432/postgres?sslmode=prefer"


@pytest.fixture
def fresh_postgres():
    """Yield the URL of a new, empty PostgreSQL database."""
    pytest.importorskip("psycopg2")
    root = make_url(os.environ.get("HVNB_POSTGRES_URL", _DEFAULT_POSTGRES_URL)).set(database="postgres")
    database = f"split_new_{uuid4().hex}"
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


def _postgres_workspace(tmp_path, url, *, rows_on):
    """Open two PostgreSQL routes on one database through separate engines; workspace rows live on ``rows_on``."""
    backends = {
        "left": {"type": "postgres", "url": url.render_as_string(hide_password=False)},
        "right": {"type": "postgres", "url": url.update_query_dict({"application_name": "split-right"}).render_as_string(hide_password=False)},
    }
    if rows_on == "sqlite":
        backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "system.db")}, **backends}
    return hb.HeavenBase(url.database.replace("_", "-"), detached=True, backends=backends)


@pytest.mark.full
@pytest.mark.parametrize("rows_on", ["sqlite", "left"])
def test_split_postgres_routes_on_a_new_database_keep_every_fragment(tmp_path, fresh_postgres, rows_on):
    class Ticket(hb.Entity):
        identifier = "split-new-postgres-ticket"
        subject = hb.field(hb.ShortText).required().store(to="left")
        queue = hb.field(hb.ShortText).required().store(to="right")

    ws = _postgres_workspace(tmp_path, fresh_postgres, rows_on=rows_on)
    try:
        ws.register(Ticket)
        _assert_routes_agree(ws, Ticket.identifier)
        ws.upsert(Ticket, {"object_id": "t1", "subject": "Printer offline", "queue": "facilities"})
        ws.set("t1", {"subject": "Printer offline again"}, entity=Ticket)
        assert {key: ws.get("t1", entity=Ticket)[key] for key in ("subject", "queue")} == {"subject": "Printer offline again", "queue": "facilities"}
    finally:
        ws.release()


@pytest.mark.full
@pytest.mark.parametrize("rows_on", ["sqlite", "left"])
@pytest.mark.parametrize("gram_routes", ["left", "both"])
def test_split_sparse_gram_postgres_routes_on_a_new_database_keep_both_indexes(tmp_path, fresh_postgres, rows_on, gram_routes):
    class Doc(hb.Entity):
        identifier = "split-new-postgres-gram-doc"
        title = hb.field(hb.ShortText).store(to="left")
        primary_tags = hb.field(hb.Array[hb.ShortText]).store(to="left", strategy=hb.SparseGramIndex(gram_width=2))
        if gram_routes == "both":
            secondary_tags = hb.field(hb.Array[hb.ShortText]).store(to="right", strategy=hb.SparseGramIndex(gram_width=2))
        else:
            secondary_tags = hb.field(hb.Array[hb.ShortText]).store(to="right")

    ws = _postgres_workspace(tmp_path, fresh_postgres, rows_on=rows_on)
    try:
        ws.register(Doc)
        _assert_routes_agree(ws, Doc.identifier)
        ws.upsert(Doc, {"object_id": "invoice-104", "title": "Invoice 104", "primary_tags": ["invoice"], "secondary_tags": ["finance"]})
        row = ws.get("invoice-104", entity=Doc)
        assert (row["title"], row["primary_tags"], row["secondary_tags"]) == ("Invoice 104", ["invoice"], ["finance"])
        assert ws.query(Doc).where(Doc.primary_tags.contained_in("open invoice")).execute().ids == ["invoice-104"]
        if gram_routes == "both":
            assert ws.query(Doc).where(Doc.secondary_tags.contained_in("contact finance")).execute().ids == ["invoice-104"]
    finally:
        ws.release()


_POOLS = {
    "default": None,
    "queue1": {"pool_class": "queue", "pool_size": 1, "max_overflow": 0, "pool_timeout": 5},
    "static": {"pool_class": "static"},
    "singleton": {"pool_class": "singleton"},
    "assertion": {"pool_class": "assertion"},
}


def _gram_doc(name):
    class Doc(hb.Entity):
        identifier = name
        title = hb.field(hb.ShortText).store(to="left")
        tags = hb.field(hb.Array[hb.ShortText]).store(to="left", strategy=hb.SparseGramIndex(gram_width=2))
        body = hb.field(hb.ShortText).store(to="right")

    return Doc


def _register_and_check_gram_doc(ws, Doc, *, seeded):
    if seeded:

        class Seed(hb.Entity):
            identifier = "split-pool-seed"
            labels = hb.field(hb.Array[hb.ShortText]).store(to="left", strategy=hb.SparseGramIndex(gram_width=2))

        ws.register(Seed)
        ws.upsert(Seed, {"object_id": "s1", "labels": ["archive"]})
    ws.register(Doc)
    _assert_routes_agree(ws, Doc.identifier)
    ws.upsert(Doc, {"object_id": "invoice-104", "title": "Invoice 104", "tags": ["invoice"], "body": "Paid in full."})
    row = ws.get("invoice-104", entity=Doc)
    assert (row["title"], row["tags"], row["body"]) == ("Invoice 104", ["invoice"], "Paid in full.")
    assert ws.query(Doc).where(Doc.tags.contained_in("open invoice")).execute().ids == ["invoice-104"]


@pytest.mark.full
@pytest.mark.parametrize("seeded", [False, True], ids=["new-tables", "existing-tables"])
@pytest.mark.parametrize("pool", list(_POOLS))
def test_split_postgres_routes_register_with_a_one_connection_pool_on_the_publishing_route(tmp_path, fresh_postgres, pool, seeded):
    """The shared-database check needs no second connection while the first route's publication transaction is open."""
    url = fresh_postgres
    left = {"type": "postgres", "url": url.render_as_string(hide_password=False)}
    if _POOLS[pool] is not None:
        left["pool"] = dict(_POOLS[pool])
    backends = {
        "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "system.db")},
        "left": left,
        "right": {"type": "postgres", "url": url.update_query_dict({"application_name": "split-right"}).render_as_string(hide_password=False)},
    }
    ws = hb.HeavenBase(url.database.replace("_", "-"), detached=True, backends=backends)
    try:
        _register_and_check_gram_doc(ws, _gram_doc("split-pool-postgres-doc"), seeded=seeded)
    finally:
        ws.release()


@pytest.fixture
def fresh_mysql():
    """Yield the URL of a new, empty MySQL database."""
    url = os.environ.get("HVNB_MYSQL_URL")
    if not url:
        pytest.skip("requires a dedicated MySQL test database in HVNB_MYSQL_URL")
    engine = create_engine(url)
    database = f"split_new_{uuid4().hex}"
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci")
        try:
            yield engine.url.set(database=database)
        finally:
            with engine.begin() as connection:
                connection.exec_driver_sql(f"DROP DATABASE `{database}`")
    finally:
        engine.dispose()


@pytest.mark.full
@pytest.mark.parametrize("pool", ["default", "queue1"])
def test_split_mysql_routes_on_a_new_database_keep_both_fragments(tmp_path, fresh_mysql, pool):
    url = fresh_mysql
    left = {"type": "mysql", "url": url.render_as_string(hide_password=False)}
    if _POOLS[pool] is not None:
        left["pool"] = dict(_POOLS[pool])
    backends = {
        "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "system.db")},
        "left": left,
        "right": {"type": "mysql", "url": url.update_query_dict({"charset": "utf8mb4"}).render_as_string(hide_password=False)},
    }
    ws = hb.HeavenBase(url.database.replace("_", "-"), detached=True, backends=backends)
    try:
        _register_and_check_gram_doc(ws, _gram_doc("split-pool-mysql-doc"), seeded=False)
    finally:
        ws.release()
