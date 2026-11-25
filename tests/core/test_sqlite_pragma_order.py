"""SQLite connection setup preserves the order of ordinary SQL."""

import pytest

import heavenbase as hb


@pytest.mark.fast
@pytest.mark.parametrize(
    "timeout",
    [
        "PRAGMA busy_timeout = 4200",
        '/* set timeout */ PrAgMa main."busy_timeout"(4200)',
    ],
)
def test_sqlite_hoists_only_the_busy_timeout_setting(tmp_path, timeout):
    database = hb.Database(
        provider="sqlite",
        database=hb.utils.path_to_file_uri(tmp_path / "setting-order.db"),
        pragmas=[
            "CREATE TEMP TABLE settings (name TEXT, value INTEGER)",
            "INSERT INTO settings SELECT 'busy_timeout', timeout FROM pragma_busy_timeout",
            timeout,
        ],
    )
    try:
        rows = database.execute("SELECT name, value FROM settings").to_lists()
        assert rows == [["busy_timeout", 4200]]
    finally:
        database.dispose()


@pytest.mark.fast
def test_sqlite_keeps_a_qualified_timeout_setting_after_its_attachment(tmp_path):
    database = hb.Database(
        provider="sqlite",
        database=hb.utils.path_to_file_uri(tmp_path / "attached.db"),
        pragmas=["ATTACH DATABASE ':memory:' AS aux", "PRAGMA aux.busy_timeout = 4400"],
    )
    try:
        assert database.execute("PRAGMA busy_timeout").scalar() == 4400
    finally:
        database.dispose()


@pytest.mark.fast
def test_sqlite_does_not_promote_an_insert_after_a_block_comment(tmp_path):
    database = hb.Database(
        provider="sqlite",
        database=hb.utils.path_to_file_uri(tmp_path / "commented.db"),
        pragmas=[
            "CREATE TEMP TABLE settings (name TEXT)",
            "/* insert a setting */ INSERT INTO settings VALUES ('*/ PRAGMA busy_timeout = 7000')",
            "PRAGMA busy_timeout = 4200",
        ],
    )
    try:
        assert database.execute("SELECT name FROM settings").scalar() == "*/ PRAGMA busy_timeout = 7000"
    finally:
        database.dispose()


@pytest.mark.fast
@pytest.mark.parametrize("schema", ["aux", '"my.schema"', "'aux''s'"])
def test_sqlite_keeps_the_order_of_mixed_timeout_assignments(tmp_path, schema):
    database = hb.Database(
        provider="sqlite",
        database=hb.utils.path_to_file_uri(tmp_path / "mixed-timeouts.db"),
        pragmas=[
            f"ATTACH DATABASE ':memory:' AS {schema}",
            f"PRAGMA {schema}.busy_timeout = 4400",
            "PRAGMA main.busy_timeout = 4200",
        ],
    )
    try:
        assert database.execute("PRAGMA busy_timeout").scalar() == 4200
    finally:
        database.dispose()
