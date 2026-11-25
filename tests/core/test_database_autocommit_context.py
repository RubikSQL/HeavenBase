"""Standalone writes require a transaction when autocommit is disabled."""

import pytest
import sqlalchemy as sa

import heavenbase as hb
from heavenbase.database import DatabaseError

pytestmark = [pytest.mark.fast, pytest.mark.unit]


@pytest.mark.parametrize("construct", [False, True])
@pytest.mark.parametrize("safe", [False, True])
def test_autocommit_false_requires_a_transaction(construct, safe):
    db = hb.Database(provider="sqlite", database=":memory:")
    db.execute("CREATE TABLE items (id INTEGER)")
    query = "INSERT INTO items VALUES (42)"
    if construct:
        query = sa.text(query)

    if safe:
        response = db.execute(query, autocommit=False, safe=True)
        assert not response.ok
        assert "autocommit=False requires a transaction context" in response.error_message
    else:
        with pytest.raises(DatabaseError, match="autocommit=False requires a transaction context"):
            db.execute(query, autocommit=False)
    assert db.execute("SELECT id FROM items").column(0) == []

    with db.tx() as tx:
        tx.execute(query, autocommit=False)
    assert db.execute("SELECT id FROM items").column(0) == [42]
