"""Transactions on MySQL-protocol servers: a rollback undoes the writes of a route and of a plain Database facade.

The cases run against the servers that ``HVNB_MYSQL_URL``, ``HVNB_MARIADB_URL``, ``HVNB_DOLT_URL``, ``HVNB_SEEKDB_URL`` and
``HVNB_OCEANBASE_URL`` name, each an account that may create databases.
"""

import os
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import heavenbase as hb
from heavenbase.execution.contracts import RowOp

pytest.importorskip("pymysql")

pytestmark = pytest.mark.full

# (backend type, environment variable)
_SERVERS = [
    ("mysql", "HVNB_MYSQL_URL"),
    ("mariadb", "HVNB_MARIADB_URL"),
    ("dolt", "HVNB_DOLT_URL"),
    ("seekdb", "HVNB_SEEKDB_URL"),
    ("oceanbase", "HVNB_OCEANBASE_URL"),
]


@pytest.fixture(params=_SERVERS, ids=[name for name, _ in _SERVERS])
def server(request):
    name, variable = request.param
    url = os.environ.get(variable)
    if not url:
        pytest.skip(f"{variable} not set")
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    database = f"mysql_tx_{uuid4().hex[:20]}"
    with admin.connect() as connection:
        connection.exec_driver_sql(f"CREATE DATABASE `{database}`")
    try:
        yield ("mysql" if name == "mariadb" else name), make_url(url).set(database=database).render_as_string(hide_password=False), database
    finally:
        with admin.connect() as connection:
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database}`")
        admin.dispose()


class Doc(hb.Entity):
    identifier = "mysql-tx-doc"
    labels = hb.field(hb.Array[hb.ShortText]).store(to="notes")


class TitledDoc(hb.Entity):
    identifier = "mysql-tx-titled"
    title = hb.field(hb.ShortText).store(to="notes").index()


def _ws(tmp_path, name, route, ws_id):
    return hb.HeavenBase(
        ws_id,
        detached=True,
        backends={"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / f"{name}-system.db")}, "notes": route},
    )


def _autocommit(connection):
    return int(connection.exec_driver_sql("SELECT @@autocommit").scalar())


def test_pooled_connections_run_transactions_after_reuse_and_a_mid_transaction_return(tmp_path, server):
    backend_type, url, database = server
    ws = _ws(tmp_path, "mysql-tx-pool", {"type": backend_type, "url": url, "pool": {"pool_size": 1, "max_overflow": 0}}, database.replace("_", "-"))
    try:
        ws.register(Doc)
        backend = ws.backends.get("notes")
        for attempt in range(3):
            with pytest.raises(RuntimeError, match="roll back"):
                with backend.db.tx():
                    backend.upsert([RowOp(Doc.identifier, f"d{attempt}", {"labels": ["alpha"]})])
                    raise RuntimeError("roll back")
            assert list(backend.scan(Doc.identifier)) == [], attempt
        engine = backend.db.engine
        table = backend.tables[Doc.identifier].name
        # a connection returned to the pool in the middle of a transaction: the pool rolls it back
        connection = engine.connect()
        connection.exec_driver_sql(f"INSERT INTO `{table}` (object_id) VALUES ('raw1')")
        connection.close()
        with engine.connect() as again:
            assert _autocommit(again) == 0
            assert again.exec_driver_sql(f"SELECT COUNT(*) FROM `{table}` WHERE object_id = 'raw1'").scalar() == 0
        with pytest.raises(RuntimeError, match="roll back"):
            with backend.db.tx():
                backend.upsert([RowOp(Doc.identifier, "d9", {"labels": ["alpha"]})])
                raise RuntimeError("roll back")
        assert list(backend.scan(Doc.identifier)) == []
        ws.upsert(Doc, {"object_id": "kept", "labels": ["alpha"]})
        assert [row["object_id"] for row in backend.scan(Doc.identifier)] == ["kept"]
    finally:
        ws.release()


def test_a_route_configured_for_autocommit_keeps_autocommit(tmp_path, server):
    backend_type, url, database = server
    ws = _ws(tmp_path, "mysql-tx-auto", {"type": backend_type, "url": url, "isolation_level": "AUTOCOMMIT"}, database.replace("_", "-"))
    try:
        ws.register(Doc)
        ws.upsert(Doc, {"object_id": "d1", "labels": ["alpha"]})
        backend = ws.backends.get("notes")
        with backend.db.engine.connect() as connection:
            assert _autocommit(connection) == 1
        assert [row["object_id"] for row in backend.scan(Doc.identifier)] == ["d1"]
    finally:
        ws.release()


def test_a_plain_database_facade_rolls_back(tmp_path, server):
    """dolt and OceanBase report no autocommit flag in their handshake, so PyMySQL left their sessions committing every statement."""
    backend_type, url, database = server
    parsed = make_url(url)
    db = hb.Database(
        provider=backend_type,
        database=database,
        host=parsed.host,
        port=parsed.port,
        username=parsed.username,
        password=parsed.password or "",
    )
    try:
        with db.tx():
            db.execute("CREATE TABLE IF NOT EXISTS hb_facade_rollback (k VARCHAR(10) PRIMARY KEY)")
        with pytest.raises(RuntimeError, match="roll back"):
            with db.tx():
                db.execute("INSERT INTO hb_facade_rollback (k) VALUES ('a')")
                raise RuntimeError("roll back")
        with db.engine.connect() as connection:
            count = connection.exec_driver_sql("SELECT COUNT(*) FROM hb_facade_rollback").scalar()
            mode = _autocommit(connection)
        assert count == 0, f"autocommit={mode}: a rolled-back facade INSERT persisted"
    finally:
        db.engine.dispose()


def test_an_entity_with_a_scalar_index_registers_in_a_second_workspace_and_after_a_reopen(tmp_path, server):
    """SQLAlchemy reads no index from seekdb's and OceanBase's SHOW CREATE TABLE; an entity's own scalar index still registers again."""
    backend_type, url, database = server
    route = {"type": backend_type, "url": url}
    first = _ws(tmp_path, "mysql-tx-idx1", dict(route), database.replace("_", "-"))
    try:
        first.register(TitledDoc)
        first.upsert(TitledDoc, {"object_id": "t1", "title": "alpha"})
    finally:
        first.release()
    again = _ws(tmp_path, "mysql-tx-idx1", dict(route), database.replace("_", "-"))
    second = _ws(tmp_path, "mysql-tx-idx2", dict(route), database.replace("_", "-"))
    try:
        again.register(TitledDoc)
        second.register(TitledDoc)
        assert again.get("t1", entity=TitledDoc)["title"] == "alpha"
    finally:
        second.release()
        again.release()
