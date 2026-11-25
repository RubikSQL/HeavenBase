"""The File Backend's row calls resolve their paths no more often than in 0.1.2.2, and follow links as they did."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import heavenbase as hb

pytestmark = pytest.mark.fast

ENTITY = "path-note"
FACTORIES = [hb.backends.JsonFileBackend, hb.backends.PickleFileBackend]

# Paths that 0.1.2.2 (c8976704) resolved for the calls below.
IN_0122 = {"upsert": 12, "get": 5, "get 5": 25, "exists": 3, "count 5": 13, "delete": 4}


def _row(object_id, title="draft"):
    return hb.ext.RowOp(ENTITY, object_id, {"object_id": object_id, "title": title})


def _backend(factory, tmp_path):
    return factory("store", hb.utils.path_to_file_uri(tmp_path / "store"), ws_id="ws")


@pytest.mark.slow
@pytest.mark.parametrize("factory", FACTORIES, ids=["json", "pickle"])
def test_a_call_resolves_its_paths_no_more_often_than_in_0122(tmp_path, monkeypatch, factory):
    """Resolving a path asks the file system about each of its folders, so it is most of the time of a call."""
    backend = _backend(factory, tmp_path)
    backend.upsert([_row(f"note-{index}") for index in range(5)])
    resolved = []
    resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *args, **kwargs: resolved.append(self) or resolve(self, *args, **kwargs))

    def resolutions(call):
        resolved.clear()
        call()
        return len(resolved)

    counts = {
        "upsert": resolutions(lambda: backend.upsert([_row("note-1", "final")])),
        "get": resolutions(lambda: backend.get_many(ENTITY, ["note-1"])),
        "get 5": resolutions(lambda: backend.get_many(ENTITY, [f"note-{index}" for index in range(5)])),
        "exists": resolutions(lambda: backend.exists_many(ENTITY, ["note-1"])),
        "count 5": resolutions(lambda: backend.count(ENTITY)),
        "delete": resolutions(lambda: backend.upsert([hb.ext.RowOp(ENTITY, "note-1", op="delete")])),
    }
    assert all(counts[name] <= most for name, most in IN_0122.items()), counts


@pytest.mark.skipif(os.name == "nt", reason="creating links needs a privilege on Windows")
@pytest.mark.parametrize("factory", FACTORIES, ids=["json", "pickle"])
def test_a_linked_entity_folder_and_a_linked_record_are_followed_at_each_call(tmp_path, factory):
    """The folder a link names now is the one a call uses, and a write goes through a linked record file."""
    backend = _backend(factory, tmp_path)
    backend.upsert([_row("note-1", "first")])
    folder = Path(backend._entity_dir(ENTITY))
    (record,) = folder.iterdir()
    one, two = tmp_path / "one", tmp_path / "two"
    folder.rename(one)
    two.mkdir()
    folder.symlink_to(one)
    assert backend.get_many(ENTITY, ["note-1"])[0]["title"] == "first"

    folder.unlink()
    folder.symlink_to(two)
    assert backend.get_many(ENTITY, ["note-1"]) == [None]
    backend.upsert([_row("note-1", "second")])
    assert (one / record.name).exists() and (two / record.name).exists()
    assert backend.get_many(ENTITY, ["note-1"])[0]["title"] == "second"

    (two / record.name).unlink()
    (two / record.name).symlink_to(one / record.name)
    assert backend.get_many(ENTITY, ["note-1"])[0]["title"] == "first"
    backend.upsert([_row("note-1", "third")])
    assert (two / record.name).is_symlink()
    assert _backend(factory, tmp_path).get_many(ENTITY, ["note-1"])[0]["title"] == "third"
    assert backend.count(ENTITY) == 1 and backend.exists_many(ENTITY, ["note-1"]) == [True]
    backend.upsert([hb.ext.RowOp(ENTITY, "note-1", op="delete")])
    assert not (one / record.name).exists()
    assert backend.exists_many(ENTITY, ["note-1"]) == [False]


@pytest.mark.skipif(os.name == "nt", reason="creating links needs a privilege on Windows")
@pytest.mark.parametrize("factory", FACTORIES, ids=["json", "pickle"])
def test_an_entity_folder_linked_to_a_folder_named_like_a_variable_keeps_its_rows(tmp_path, monkeypatch, factory):
    """The file helpers expand the variable in the resolved path, so a lookup must read where they write."""
    monkeypatch.setenv("HB_NOTE_FOLDER", "notes")
    backend = _backend(factory, tmp_path)
    (tmp_path / "$HB_NOTE_FOLDER").mkdir()
    Path(backend.path, backend._physical_name(ENTITY)).symlink_to(tmp_path / "$HB_NOTE_FOLDER")

    backend.upsert([_row("note-1"), _row("note-2")])
    assert [row["object_id"] for row in backend.get_many(ENTITY, ["note-1", "note-2"])] == ["note-1", "note-2"]
    assert backend.exists_many(ENTITY, ["note-1", "missing"]) == [True, False]
    assert backend.count(ENTITY) == 2
    reopened = _backend(factory, tmp_path)
    assert sorted(row["object_id"] for row in reopened.scan(ENTITY)) == ["note-1", "note-2"]
    reopened.upsert([hb.ext.RowOp(ENTITY, "note-1", op="delete")])
    assert reopened.exists_many(ENTITY, ["note-1", "note-2"]) == [False, True]
