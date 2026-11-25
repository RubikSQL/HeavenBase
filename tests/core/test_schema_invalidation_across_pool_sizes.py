"""Schema invalidation across pools with identical SQL setup."""

import pytest

import heavenbase as hb


@pytest.mark.fast
def test_committed_ddl_invalidates_schema_index_across_pool_sizes(tmp_path):
    uri = (tmp_path / "ddl.db").as_uri()
    writer = hb.Database(provider="sqlite", database=uri, pool={"pool_size": 1})
    reader = hb.Database(provider="sqlite", database=uri, pool={"pool_size": 2})
    try:
        writer.execute("CREATE TABLE notes (id INTEGER)").close()
        assert reader.schema_index() == {"notes": ["id"]}
        with writer.tx() as tx:
            tx.execute("INSERT INTO notes VALUES (1)").close()
            tx.execute("ALTER TABLE notes ADD COLUMN title TEXT").close()
        cached = reader.schema_index()
        assert reader.schema_index(refresh=True) == {"notes": ["id", "title"]}
        assert cached == {"notes": ["id", "title"]}
    finally:
        writer.dispose()
        reader.dispose()
