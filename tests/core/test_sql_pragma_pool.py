"""SQL pool identity includes ordered connection initialization settings."""

import pytest

import heavenbase as hb


@pytest.mark.fast
def test_sqlite_pools_share_only_with_identical_connection_setup(tmp_path):
    uri = hb.utils.path_to_file_uri(tmp_path / "pragma-pool.db")
    disable, enable = "PRAGMA foreign_keys=OFF", "PRAGMA foreign_keys=ON"

    def database(*pragmas):
        return hb.Database(provider="sqlite", database=uri, pragmas=list(pragmas))

    first = database(disable, enable)
    same = database(disable, enable)
    reverse = database(enable, disable)
    try:
        assert first.execute("PRAGMA foreign_keys").scalar() == 1
        assert reverse.execute("PRAGMA foreign_keys").scalar() == 0
        assert first.engine is same.engine
        assert first.engine is not reverse.engine
    finally:
        first.dispose()
        reverse.dispose()


@pytest.mark.fast
def test_sqlite_drop_disposes_pools_with_distinct_connection_setup(tmp_path):
    path = tmp_path / "pragma-pool-drop.db"
    uri = hb.utils.path_to_file_uri(path)
    first = hb.Database(provider="sqlite", database=uri, pragmas=["PRAGMA foreign_keys=ON"])
    peer = hb.Database(provider="sqlite", database=uri, pragmas=["PRAGMA foreign_keys=OFF"])
    try:
        assert peer.schema_index() == {}
        first.execute("CREATE TABLE notes (title TEXT)").close()
        assert peer.schema_index() == {"notes": ["title"]}
        assert peer.execute("SELECT count(*) FROM notes").scalar() == 0
        first.drop_database()
        assert not path.exists()
        with pytest.raises(RuntimeError, match="disposed"):
            _ = peer.engine
    finally:
        first.dispose()
        peer.dispose()
