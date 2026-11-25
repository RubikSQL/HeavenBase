"""A drop_database() that removes nothing leaves the other facades of its database usable."""

import pytest

import heavenbase as hb
from heavenbase.database.registry import DatabaseEngineRegistry

pytestmark = pytest.mark.fast

TARGETS = {
    "clickhouse": {"provider": "clickhouse", "host": "db.internal", "port": 8123, "database": "shop"},
    "trino": {"provider": "trino", "host": "db.internal", "port": 8080, "database": "hive/sales"},
    "mysql-without-database": {"provider": "mysql", "host": "db.internal", "port": 3306},
}


def _database_key(db):
    return DatabaseEngineRegistry._keys(db.spec, db._authority_id, db._resource_id)[2]


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_drop_that_removes_nothing_disposes_only_the_caller(name):
    target = TARGETS[name]
    owner = hb.Database(**target, username="app", password="one", pool={"pool_size": 1})
    reporter = hb.Database(**target, username="report", password="two", pool={"pool_size": 3})
    try:
        assert _database_key(owner) == _database_key(reporter)
        assert not reporter.disposed

        owner.drop_database()

        assert owner.disposed
        assert not reporter.disposed
    finally:
        reporter.dispose()
        owner.dispose()
