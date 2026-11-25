"""A SQLite workspace drop removes the database's journal files only inside the drop, never after it."""

import sqlite3

import pytest

import heavenbase as hb
from heavenbase.database.database import Database

pytestmark = pytest.mark.fast


class Invoice(hb.Entity):
    identifier = "drop-journal-invoice"
    customer = hb.field(hb.ShortText)


def test_a_client_that_creates_the_database_again_right_after_a_drop_keeps_its_writes(tmp_path, monkeypatch):
    path = tmp_path / "billing.db"
    ws = hb.HeavenBase("billing", detached=True, backends={"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(path)}})
    ws.register(Invoice)
    ws.upsert(Invoice, {"object_id": "inv-1", "customer": "Northwind"})
    clients = []
    drop_database = Database.drop_database

    def then_another_client_writes(self, *args, **kwargs):
        drop_database(self, *args, **kwargs)
        # Another program opens the database again as soon as the drop has removed it, and commits in WAL mode.
        client = sqlite3.connect(str(path))
        client.execute("PRAGMA journal_mode=WAL")
        client.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY, amount INTEGER)")
        client.executemany("INSERT INTO ledger VALUES (?, ?)", [(index, index * 10) for index in range(50)])
        client.commit()
        clients.append(client)

    monkeypatch.setattr(Database, "drop_database", then_another_client_writes)
    ws.drop()
    assert clients, "the workspace drop did not drop the database"
    fresh = sqlite3.connect(str(path))
    try:
        # The other client's committed rows are still in the database, journal included.
        assert fresh.execute("SELECT COUNT(*), SUM(amount) FROM ledger").fetchone() == (50, 12250)
    finally:
        fresh.close()
        clients[0].close()
