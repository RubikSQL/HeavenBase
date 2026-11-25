"""A replacement that adds a route deletes nothing the route already stores."""

import pytest

import heavenbase as hb

pytestmark = pytest.mark.slow


def _memo(with_note):
    class Memo(hb.Entity):
        identifier = "added-route-memo"

        title = hb.field(hb.ShortText).store(to="main").optional()
        if with_note:
            note = hb.field(hb.ShortText).store(to="aux").optional()

    return Memo


def _sqlite(name, path):
    return hb.backends.SQLiteBackend(name, database=hb.utils.path_to_file_uri(path))


def _workspace(name, main, aux):
    return hb.HeavenBase(name, detached=True, backends={"main": main, "aux": aux})


def test_another_workspaces_rows_on_the_added_routes_storage_are_kept(tmp_path):
    other = _workspace("added-route-other", _sqlite("main", tmp_path / "m2.db"), _sqlite("aux", tmp_path / "aux.db"))
    mine = _workspace("added-route-mine", _sqlite("main", tmp_path / "m1.db"), _sqlite("aux", tmp_path / "aux.db"))
    try:
        Other = other.register(_memo(True))
        other.upsert(Other, {"object_id": "o1", "title": "Other", "note": "other note"})
        mine.register(_memo(False))
        mine.upsert(_memo(False).identifier, {"object_id": "a1", "title": "Mine"})
        mine.register(_memo(True), allow_replace=True)
        assert other.get("o1", entity=Other)["note"] == "other note"
    finally:
        mine.release()
        other.release()


def test_durable_rows_are_kept_when_the_older_route_is_an_empty_volatile_backend(tmp_path):
    def open_workspace():
        return _workspace("added-route-volatile", hb.backends.InMemBackend("main"), _sqlite("aux", tmp_path / "aux.db"))

    first = open_workspace()
    try:
        Memo = first.register(_memo(True))
        first.upsert(Memo, {"object_id": "d1", "title": "T", "note": "durable note"})
    finally:
        first.release()
    second = open_workspace()
    try:
        second.register(_memo(False))
        second.register(_memo(True), allow_replace=True)
        assert second.backends.get("aux").get_many("added-route-memo", ["d1"])[0]["note"] == "durable note"
    finally:
        second.release()


def test_a_row_deleted_while_its_route_was_dropped_can_be_deleted_again(tmp_path):
    ws = _workspace("added-route-back", _sqlite("main", tmp_path / "main.db"), _sqlite("aux", tmp_path / "aux.db"))
    try:
        ws.register(_memo(True))
        ws.upsert(_memo(True).identifier, {"object_id": "m1", "title": "First", "note": "old note"})
        ws.register(_memo(False), allow_replace=True)
        assert ws.delete("m1") is True
        Memo = ws.register(_memo(True), allow_replace=True)
        assert ws.get("m1", entity=Memo)["note"] == "old note"
        with pytest.raises(ValueError, match="already exists"):
            ws.upsert(Memo, {"object_id": "m1", "title": "Recreated"}, overwrite=False)
        assert ws.delete((Memo, "m1")) is True
        assert ws.get("m1", entity=Memo) is None
        assert ws.upsert(Memo, {"object_id": "m1", "title": "Recreated"}, overwrite=False) == "m1"
    finally:
        ws.release()
