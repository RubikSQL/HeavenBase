"""Engine setup never splits the lifecycle group of one database."""

import threading

import pytest

import heavenbase as hb
from heavenbase.database import config as config_module
from heavenbase.database.config import DB_ENGINE
from heavenbase.database.registry import DatabaseEngineRegistry

pytestmark = pytest.mark.fast


def _keys(**config):
    engine_key, _, database_key = DatabaseEngineRegistry._keys(DB_ENGINE.resolve(config), None, None)
    return engine_key, database_key


def _linked(tmp_path, path):
    alias = tmp_path / "orders-link.duckdb"
    try:
        alias.symlink_to(path)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Cannot create file symlink: {exc}")
    return alias


@pytest.mark.parametrize("symlink", [False, True])
def test_duckdb_drop_retires_every_pool_variant_and_keeps_later_writes(tmp_path, symlink):
    pytest.importorskip("duckdb_engine")
    path = tmp_path / "orders.duckdb"
    owner = hb.Database(provider="duckdb", database=path.as_uri(), pool={"pool_size": 1})
    clerk_path = _linked(tmp_path, path) if symlink else path
    clerk = hb.Database(provider="duckdb", database=clerk_path.as_uri(), pool={"pool_size": 2, "max_overflow": 4})
    try:
        owner.execute("CREATE TABLE orders (id INTEGER, item TEXT)")
        owner.execute("INSERT INTO orders VALUES (1, 'lamp'), (2, 'desk')")
        assert clerk.execute("SELECT count(*) FROM orders", readonly=True).to_lists() == [[2]]

        owner.drop_database()

        assert not path.exists()
        assert clerk.disposed
        with pytest.raises(RuntimeError, match="disposed"):
            _ = clerk.engine
    finally:
        clerk.dispose()
        owner.dispose()

    fresh = hb.Database(provider="duckdb", database=path.as_uri())
    try:
        assert fresh.schema_index() == {}
        fresh.execute("CREATE TABLE orders (id INTEGER, item TEXT)")
        fresh.execute("INSERT INTO orders VALUES (3, 'chair')")
    finally:
        fresh.dispose()
    reopened = hb.Database(provider="duckdb", database=path.as_uri())
    try:
        assert reopened.execute("SELECT id, item FROM orders", readonly=True).to_lists() == [[3, "chair"]]
    finally:
        reopened.dispose()


def test_duckdb_ddl_invalidates_schema_index_across_pool_sizes(tmp_path):
    pytest.importorskip("duckdb_engine")
    path = tmp_path / "catalog.duckdb"
    writer = hb.Database(provider="duckdb", database=path.as_uri(), pool={"pool_size": 1})
    reader = hb.Database(provider="duckdb", database=path.as_uri(), pool={"pool_size": 2})
    try:
        writer.execute("CREATE TABLE items (id INTEGER)")
        assert reader.schema_index() == {"items": ["id"]}
        writer.execute("ALTER TABLE items ADD COLUMN title TEXT")
        assert reader.schema_index() == {"items": ["id", "title"]}
        assert writer.engine is not reader.engine
    finally:
        reader.dispose()
        writer.dispose()


@pytest.mark.parametrize(
    "provider,port",
    [("postgres", 5432), ("mysql", 3306), ("mssql", 1433), ("oracle", 1521)],
)
def test_server_setup_variants_share_one_database_group(provider, port):
    target = {"provider": provider, "host": "db.internal", "port": port, "database": "shop"}
    first_engine, first = _keys(**target, username="app", password="one", pool={"pool_size": 2})
    second_engine, second = _keys(**target, username="app", password="two", pool={"pool_size": 7, "pool_timeout": 5})

    assert first == second
    assert first_engine != second_engine
    assert first_engine.startswith(f"{first}:")


def test_server_database_groups_follow_the_dropped_namespace():
    base = {"host": "db.internal", "username": "app", "password": "secret"}
    _, shop = _keys(provider="postgres", port=5432, database="shop", **base)
    _, other_user = _keys(provider="postgres", port=5432, database="shop", host="db.internal", username="report", password="secret")
    assert other_user == shop
    assert _keys(provider="postgres", port=5432, database="billing", **base)[1] != shop
    assert _keys(provider="postgres", port=5433, database="shop", **base)[1] != shop
    assert _keys(provider="postgres", port=5432, database="shop", host="db.replica", username="app", password="secret")[1] != shop
    assert _keys(provider="mysql", port=5432, database="shop", **base)[1] != shop

    # Oracle drops the user schema, so each user on one service is its own group.
    _, scott = _keys(provider="oracle", host="db.internal", port=1521, database="FREEPDB1", username="scott", password="tiger")
    _, scott_pool = _keys(provider="oracle", host="db.internal", port=1521, database="FREEPDB1", username="scott", password="tiger", pool={"pool_size": 9})
    _, hr = _keys(provider="oracle", host="db.internal", port=1521, database="FREEPDB1", username="hr", password="tiger")
    assert scott == scott_pool
    assert scott != hr


def test_server_query_parameters_separate_groups_but_connect_args_do_not():
    target = {"provider": "postgres", "host": "db.internal", "port": 5432, "database": "shop", "username": "app", "password": "secret"}
    _, plain = _keys(**target)
    _, query_option = _keys(**target, params={"sslmode": "require"})
    _, connect_option = _keys(**target, pool={"connect_args": {"sslmode": "require"}})

    # A query parameter can select the target (a PostgreSQL socket directory), so every one counts.
    assert query_option != plain
    assert connect_option == plain


def test_a_slow_path_resolution_holds_up_no_other_database(tmp_path, monkeypatch):
    # As on a stalled network mount. Before Python 3.12 functools.cached_property computes under a lock that every spec shares.
    resolving, resume = threading.Event(), threading.Event()
    resolve = config_module.realpath

    def slow(path, *args, **kwargs):
        if "stalled" in str(path):
            resolving.set()
            assert resume.wait(60)
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(config_module, "realpath", slow)
    keys = {}

    def key(name):
        def run():
            keys[name] = DB_ENGINE.resolve({"provider": "sqlite", "database": (tmp_path / f"{name}.db").as_uri()})._database_key

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    stalled = key("stalled")
    try:
        assert resolving.wait(30)
        other = key("other")
        other.join(20)
        assert not other.is_alive()
        assert set(keys) == {"other"}
    finally:
        resume.set()
    stalled.join(30)
    assert len(set(keys.values())) == 2
