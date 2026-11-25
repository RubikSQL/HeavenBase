"""File-backend record files whose full path does not fit Windows' ``MAX_PATH``.

Windows without long-path support (``LongPathsEnabled`` 0, its default) refuses a path longer than 259
characters unless the path is in its extended-length ``\\\\?\\`` form. The File Backends keep record names
that fit where they can and open the others through that form. Elsewhere these are ordinary round trips.
"""

import os

import pytest

import heavenbase as hb
from heavenbase.utils.serialize import dump_json, save_pkl

pytestmark = [pytest.mark.fast, pytest.mark.unit]

ENTITY = "record-path-note"
FORMATS = [("json", hb.backends.JsonFileBackend), ("pickle", hb.backends.PickleFileBackend)]


def _extended(path):
    """Return ``path`` in the form Windows opens past ``MAX_PATH``; elsewhere ``path`` itself."""
    return "\\\\?\\" + os.path.abspath(path) if os.name == "nt" else str(path)


def _store_folder(tmp_path, length):
    """Create and return a store folder whose path has about ``length`` characters, below ``tmp_path``."""
    folder = str(tmp_path)
    if len(folder) + 2 > length:
        pytest.skip("the temporary folder is already too deep for this layout")
    while len(folder) < length:
        folder = os.path.join(folder, "d" * max(1, min(40, length - len(folder) - 1)))
    os.makedirs(folder)
    return folder


def _rows(title_by_id):
    return [hb.ext.RowOp(ENTITY, object_id, {"object_id": object_id, "title": title}) for object_id, title in title_by_id.items()]


@pytest.mark.slow
@pytest.mark.parametrize(("fmt", "factory"), FORMATS, ids=[item[0] for item in FORMATS])
def test_rows_round_trip_in_a_folder_too_deep_for_their_hashed_record_names(tmp_path, fmt, factory):
    """A long id under a 200-character store folder goes to a hashed name whose path exceeds ``MAX_PATH``."""
    folder = _store_folder(tmp_path, 200)
    long_id = "y" * 90
    backend = factory("store", hb.utils.path_to_file_uri(folder), ws_id="ws")
    backend.upsert(_rows({"ok": "short", long_id: "long"}))

    reopened = factory("store", hb.utils.path_to_file_uri(folder), ws_id="ws")
    assert [row["title"] for row in reopened.get_many(ENTITY, ["ok", long_id])] == ["short", "long"]
    assert reopened.exists_many(ENTITY, [long_id, "missing"]) == [True, False]
    assert {row["object_id"]: row["title"] for row in reopened.scan(ENTITY)} == {"ok": "short", long_id: "long"}
    assert reopened.count(ENTITY) == 2

    reopened.upsert(_rows({long_id: "longer"}))
    assert factory("store", hb.utils.path_to_file_uri(folder), ws_id="ws").get_many(ENTITY, [long_id])[0]["title"] == "longer"
    reopened.upsert([hb.ext.RowOp(ENTITY, long_id, op="delete")])
    assert factory("store", hb.utils.path_to_file_uri(folder), ws_id="ws").exists_many(ENTITY, [long_id, "ok"]) == [False, True]


@pytest.mark.slow
@pytest.mark.parametrize(("fmt", "factory"), FORMATS, ids=[item[0] for item in FORMATS])
def test_drop_removes_a_record_whose_path_exceeds_the_windows_path_limit(tmp_path, fmt, factory):
    """A store holding a record under a 240-character encoded name, written by another file system, drops whole."""
    backend = factory("store", hb.utils.path_to_file_uri(tmp_path / "store"), ws_id="ws")
    backend.upsert(_rows({"keep": "keep"}))
    entity_dir = os.path.join(backend.path, backend._physical_name(ENTITY))
    object_id = "x" * 240
    record = _extended(os.path.join(entity_dir, f"{object_id}{backend.suffix}"))
    if fmt == "json":
        dump_json({"object_id": object_id, "title": "old"}, record)
    else:
        save_pkl({"object_id": object_id, "title": "old"}, record)

    assert {row["object_id"] for row in backend.scan(ENTITY)} == {"keep", object_id}
    backend.drop()

    assert not os.path.exists(backend.path)


@pytest.mark.slow
@pytest.mark.parametrize(("fmt", "factory"), FORMATS, ids=[item[0] for item in FORMATS])
def test_a_store_folder_longer_than_the_windows_folder_limit_works(tmp_path, fmt, factory):
    """Windows creates a folder of at most 247 characters without long-path support; a 252-character store works."""
    folder = str(tmp_path)
    while len(folder) < 252:
        folder = os.path.join(folder, "s" * max(1, min(40, 252 - len(folder) - 1)))
    backend = factory("store", hb.utils.path_to_file_uri(folder), ws_id="ws")
    assert not backend.path.startswith("\\\\?\\")
    backend.upsert(_rows({"a": "first", "y" * 90: "long"}))

    reopened = factory("store", hb.utils.path_to_file_uri(folder), ws_id="ws")
    assert [row["title"] for row in reopened.get_many(ENTITY, ["a", "y" * 90])] == ["first", "long"]
    assert reopened.count(ENTITY) == 2
    reopened.drop()
    assert not os.path.exists(_extended(reopened.path))
