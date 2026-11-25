"""SQLite backend writes inside an existing Database transaction."""

import sys

import pytest

import heavenbase as hb
from heavenbase.database import DatabaseError
from heavenbase.ext import RowOp


class TransactionNote(hb.Entity):
    identifier = "nested-sql-upsert-note"
    title = hb.field(hb.ShortText)


@pytest.fixture
def backend(tmp_path, request):
    uri = hb.utils.path_to_file_uri(tmp_path / "notes.db")
    config = {"type": "sqlite", "database": uri}
    if getattr(request, "param", False):
        config["pool"] = {"connect_args": {"autocommit": False}}
        config["pragmas"] = ["PRAGMA busy_timeout = 6000"]
    workspace = hb.HeavenBase("nested-sql-upsert", detached=True, backends={"main": config})
    try:
        workspace.register(TransactionNote)
        yield workspace.backends.get("main")
    finally:
        workspace.release()


@pytest.mark.fast
def test_failed_nested_upsert_rolls_back_its_rows_and_keeps_the_outer_transaction(backend):
    entity = TransactionNote.identifier
    backend.upsert([RowOp(entity, "original", {"title": "before"})])

    with backend.db.tx():
        backend.upsert([RowOp(entity, "earlier", {"title": "keep"})])
        with pytest.raises(KeyError, match="unregistered"):
            backend.upsert([RowOp(entity, "original", {"title": "partial"}), RowOp("unregistered", "invalid", {"title": "invalid"})])
        assert backend.get_many(entity, ["original"])[0]["title"] == "before"
        backend.upsert([RowOp(entity, "later", {"title": "keep too"})])

    assert [row["title"] for row in backend.get_many(entity, ["original", "earlier", "later"])] == ["before", "keep", "keep too"]


@pytest.mark.fast
def test_successful_nested_upsert_rolls_back_with_the_outer_transaction(backend):
    entity = TransactionNote.identifier

    with pytest.raises(RuntimeError, match="cancel outer"):
        with backend.db.tx():
            backend.upsert([RowOp(entity, "temporary", {"title": "discard"})])
            assert backend.get_many(entity, ["temporary"])[0]["title"] == "discard"
            raise RuntimeError("cancel outer")

    assert backend.get_many(entity, ["temporary"]) == [None]


@pytest.mark.slow
@pytest.mark.parametrize(
    "backend",
    [
        pytest.param(False, id="legacy-transaction-control"),
        pytest.param(
            True,
            id="driver-autocommit-false",
            marks=pytest.mark.skipif(sys.version_info < (3, 12), reason="sqlite3 gained the autocommit argument in Python 3.12"),
        ),
    ],
    indirect=True,
)
def test_nested_upsert_preserves_error_when_sqlite_rolls_back_outer_transaction(backend):
    entity = TransactionNote.identifier
    table = backend.tables[entity].name
    backend.db.execute(f"""CREATE TRIGGER reject_invalid BEFORE INSERT ON "{table}"
        WHEN NEW.title = 'reject' BEGIN SELECT RAISE(ROLLBACK, 'invalid note'); END""").close()
    backend.upsert([RowOp(entity, "original", {"title": "before"})])

    with backend.db.tx():
        backend.upsert([RowOp(entity, "earlier", {"title": "discard"})])
        with pytest.raises(DatabaseError, match="invalid note"):
            backend.upsert([RowOp(entity, "original", {"title": "reject"})])
        with pytest.raises(DatabaseError, match=r"call rollback\(\) before continuing"):
            backend.db.commit()
        backend.db.rollback()
        backend.upsert([RowOp(entity, "later", {"title": "keep"})])

    original, discarded, later = backend.get_many(entity, ["original", "earlier", "later"])
    assert original["title"] == "before"
    assert discarded is None
    assert later["title"] == "keep"
