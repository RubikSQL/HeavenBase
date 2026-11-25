"""Schema cache invalidation across different pools for one SQLite file."""

import pytest

from heavenbase import Database


@pytest.mark.fast
def test_sqlite_schema_index_tracks_ddl_across_pool_sizes(tmp_path):
    uri = (tmp_path / "items.db").as_uri()
    writer = Database(provider="sqlite", database=uri, pool={"pool_size": 2})
    reader = Database(provider="sqlite", database=uri, pool={"pool_size": 3})
    try:
        writer.execute("CREATE TABLE items (id INTEGER)")
        before = {"items": ["id"]}
        expected = {"items": ["id", "description"]}
        assert reader.schema_index() == before

        writer.execute("ALTER TABLE items ADD COLUMN description TEXT")
        assert reader.schema_index() == expected
    finally:
        reader.close()
        writer.close()
