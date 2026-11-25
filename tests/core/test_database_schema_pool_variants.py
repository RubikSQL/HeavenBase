"""SQLite pool tuning does not isolate schema invalidation."""

import pytest

import heavenbase as hb
from heavenbase.database.config import DB_ENGINE

pytestmark = pytest.mark.fast


def _database_path(tmp_path, path, symlink):
    if not symlink:
        return path
    alias = tmp_path / "inventory-link.db"
    try:
        alias.symlink_to(path)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Cannot create SQLite file symlink: {exc}")
    return alias


@pytest.mark.parametrize("symlink", [False, True])
def test_sqlite_schema_index_is_invalidated_across_pool_sizes(tmp_path, symlink):
    path = tmp_path / "pool-schema.db"
    writer = hb.Database(provider="sqlite", database=path.as_uri(), pool={"pool_size": 1})
    try:
        writer.execute("CREATE TABLE items (id INTEGER)")
        reader_path = _database_path(tmp_path, path, symlink)
        reader = hb.Database(provider="sqlite", database=reader_path.as_uri(), pool={"pool_size": 2})
        try:
            assert reader.schema_index() == {"items": ["id"]}
            with writer.tx() as tx:
                tx.execute("ALTER TABLE items ADD COLUMN detail TEXT")
            assert reader.schema_index() == {"items": ["id", "detail"]}
            assert writer.engine is not reader.engine
        finally:
            reader.dispose()
    finally:
        writer.dispose()


@pytest.mark.parametrize("symlink", [False, True])
def test_sqlite_drop_retires_pools_with_different_sizes(tmp_path, symlink):
    path = tmp_path / "pool-drop.db"
    writer = hb.Database(provider="sqlite", database=path.as_uri(), pool={"pool_size": 1})
    try:
        writer.execute("CREATE TABLE items (id INTEGER)")
        reader_path = _database_path(tmp_path, path, symlink)
        reader = hb.Database(provider="sqlite", database=reader_path.as_uri(), pool={"pool_size": 2})
        try:
            assert reader.schema_index() == {"items": ["id"]}

            writer.drop_database()

            assert not path.exists()
            with pytest.raises(RuntimeError, match="disposed"):
                _ = reader.engine
        finally:
            reader.dispose()
    finally:
        writer.dispose()


def test_sqlite_drop_through_symlink_retires_pools_after_config_cache_refresh(tmp_path):
    path = tmp_path / "inventory.db"
    direct = hb.Database(provider="sqlite", database=path.as_uri())
    try:
        direct.execute("CREATE TABLE items (id INTEGER)")
        alias = _database_path(tmp_path, path, symlink=True)
        linked = hb.Database(provider="sqlite", database=alias.as_uri())
        try:
            assert linked.schema_index() == {"items": ["id"]}
            linked.drop_database()

            assert alias.is_symlink()
            assert not path.exists()
            assert linked.disposed and direct.disposed
            alias.unlink()
            DB_ENGINE.clear_cache()
            assert linked.disposed and direct.disposed
        finally:
            linked.dispose()
    finally:
        direct.dispose()
