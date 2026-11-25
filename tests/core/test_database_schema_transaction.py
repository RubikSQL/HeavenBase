"""Schema caches follow the outcome of transactional DDL."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

import heavenbase as hb

pytestmark = [pytest.mark.fast, pytest.mark.unit]


@pytest.mark.slow
@pytest.mark.parametrize("finish", ["exit", "commit", "autocommit"])
@pytest.mark.parametrize("prefix", ["", "; /* empty statement */ ", "\ufeff"], ids=["plain", "empty-statement", "bom"])
def test_schema_index_reflected_before_ddl_commit_is_invalidated(tmp_path, finish, prefix):
    db = hb.Database(provider="sqlite", database=(tmp_path / "schema.db").as_uri())
    db.execute("CREATE TABLE items (id INTEGER)")
    reader = db.clone()
    with db.tx() as tx:
        tx.execute("INSERT INTO items VALUES (1)")
        tx.execute(prefix + "ALTER TABLE items ADD COLUMN detail TEXT")
        assert reader.schema_index() == {"items": ["id"]}
        if finish == "commit":
            tx.commit()
        elif finish == "autocommit":
            tx.execute("INSERT INTO items VALUES (2, NULL)", autocommit=True)
        if finish != "exit":
            assert reader.schema_index() == {"items": ["id", "detail"]}

    assert reader.schema_index() == {"items": ["id", "detail"]}


@pytest.mark.parametrize("pragmas", [None, ["PRAGMA foreign_keys = ON"]], ids=["shared-pool", "separate-pool"])
@pytest.mark.parametrize("prefix", ["", "/* schema update */ "])
def test_schema_reflection_in_flight_cannot_cache_pre_commit_columns(tmp_path, monkeypatch, prefix, pragmas):
    db = hb.Database(provider="sqlite", database=(tmp_path / "schema-race.db").as_uri())
    db.execute("CREATE TABLE items (id INTEGER)")
    reader = db.clone() if pragmas is None else hb.Database(provider="sqlite", database=(tmp_path / "schema-race.db").as_uri(), pragmas=pragmas)
    reflected, publish = Event(), Event()
    columns = reader.columns

    def pause_after_reflection(*args, **kwargs):
        result = columns(*args, **kwargs)
        reflected.set()
        assert publish.wait(timeout=10)
        return result

    monkeypatch.setattr(reader, "columns", pause_after_reflection)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with db.tx() as tx:
                tx.execute("INSERT INTO items VALUES (1)")
                tx.execute(prefix + "ALTER TABLE items ADD COLUMN detail TEXT")
                pending = pool.submit(reader.schema_index)
                assert reflected.wait(timeout=10)
        finally:
            publish.set()
        assert pending.result(timeout=10) == {"items": ["id"]}

    assert reader.schema_index() == {"items": ["id", "detail"]}
