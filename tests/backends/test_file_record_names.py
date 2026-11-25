"""Record-file names of the JSON and pickle file backends.

A record file is named by the percent-encoded object ID when that name is usable, and by a bounded
hashed name otherwise. These tests pin both layouts, the 0.1.2.2 compatibility of the encoded one, and
the cases the encoded name cannot serve: ids that differ only by letter case on a case-insensitive file
system (the default on macOS and Windows), ids too long for a file name, and Windows device names.
"""

import os
from urllib.parse import quote

import pytest

import heavenbase as hb
from heavenbase.backends.file import base as file_base
from heavenbase.utils.serialize import dump_json, save_pkl

pytestmark = [pytest.mark.fast, pytest.mark.unit]

ENTITY = "record-name-thing"
FORMATS = [("json", hb.backends.JsonFileBackend, ".json"), ("pickle", hb.backends.PickleFileBackend, ".pkl")]


def _fold(path):
    """Return the path a case-insensitive file system opens: record names compare without case."""
    text = str(path)
    folder, name = os.path.split(text)
    if name.endswith((".json", ".pkl")):
        return os.path.join(folder, name.lower())
    return text


@pytest.fixture
def case_insensitive(monkeypatch):
    """Make the file backend see a case-insensitive file system, as on default macOS (APFS) and Windows (NTFS).

    Linux file systems are case-sensitive, so every record-file access the backend makes is routed to the
    name folded to lower case: two names that differ only by case then open one file, the way APFS and NTFS
    resolve them. Temporary names end in ``.tmp`` and stay as they are.
    """
    for helper in ("_exists", "delete_file", "load_json", "load_bin"):
        original = getattr(file_base, helper)
        monkeypatch.setattr(file_base, helper, lambda path, *args, _original=original, **kwargs: _original(_fold(path), *args, **kwargs))
    replace = file_base.replace_file
    monkeypatch.setattr(file_base, "replace_file", lambda src, dst: replace(src, _fold(dst)))


def _backend(factory, tmp_path, name="store"):
    return factory(name, hb.utils.path_to_file_uri(tmp_path / name), ws_id="ws")


def _rows(title_by_id):
    return [hb.ext.RowOp(ENTITY, object_id, {"object_id": object_id, "title": title}) for object_id, title in title_by_id.items()]


def _entity_dir(backend):
    return os.path.join(backend.path, backend._physical_name(ENTITY))


def _record_names(backend):
    return sorted(name for name in os.listdir(_entity_dir(backend)) if not name.endswith(".tmp"))


@pytest.mark.parametrize(("fmt", "factory", "suffix"), FORMATS, ids=[item[0] for item in FORMATS])
def test_ids_that_differ_only_by_case_keep_separate_records_on_a_case_insensitive_file_system(case_insensitive, tmp_path, fmt, factory, suffix):
    """``Doc1`` and ``doc1`` never share a record file, so neither overwrites, deletes or returns the other."""
    backend = _backend(factory, tmp_path)
    backend.upsert(_rows({"Doc1": "upper"}))
    backend.upsert(_rows({"doc1": "lower"}))

    assert [row["title"] for row in backend.get_many(ENTITY, ["Doc1", "doc1"])] == ["upper", "lower"]
    assert backend.count(ENTITY) == 2
    reopened = _backend(factory, tmp_path)
    assert {row["object_id"]: row["title"] for row in reopened.scan(ENTITY)} == {"Doc1": "upper", "doc1": "lower"}
    assert reopened.exists_many(ENTITY, ["Doc1", "doc1", "DOC1"]) == [True, True, False]
    assert reopened.get_many(ENTITY, ["DOC1"]) == [None]

    reopened.upsert([hb.ext.RowOp(ENTITY, "doc1", op="delete")])
    again = _backend(factory, tmp_path)
    assert again.exists_many(ENTITY, ["Doc1", "doc1"]) == [True, False]
    assert [row["object_id"] for row in again.scan(ENTITY)] == ["Doc1"]
    assert again.get_many(ENTITY, ["Doc1"])[0]["title"] == "upper"

    again.upsert([hb.ext.RowOp(ENTITY, "Doc1", op="delete")])
    again.upsert(_rows({"doc1": "lower again"}))
    final = _backend(factory, tmp_path)
    assert {row["object_id"]: row["title"] for row in final.scan(ENTITY)} == {"doc1": "lower again"}
    assert final.count(ENTITY) == 1


@pytest.mark.parametrize(("fmt", "factory", "suffix"), FORMATS, ids=[item[0] for item in FORMATS])
def test_a_0122_record_of_a_case_variant_id_is_neither_returned_nor_deleted_for_another_id(case_insensitive, tmp_path, fmt, factory, suffix):
    """A store written by 0.1.2.2 holds ``Doc1`` under its encoded name; ``doc1`` does not read or remove it."""
    backend = _backend(factory, tmp_path)
    os.makedirs(_entity_dir(backend))
    legacy = os.path.join(_entity_dir(backend), f"Doc1{suffix}")
    if fmt == "json":
        dump_json({"object_id": "Doc1", "title": "upper"}, _fold(legacy))
    else:
        save_pkl({"object_id": "Doc1", "title": "upper"}, _fold(legacy))

    assert backend.get_many(ENTITY, ["doc1", "Doc1"]) == [None, {"object_id": "Doc1", "title": "upper"}]
    assert backend.exists_many(ENTITY, ["doc1", "Doc1"]) == [False, True]
    backend.upsert([hb.ext.RowOp(ENTITY, "doc1", op="delete")])
    assert backend.exists_many(ENTITY, ["Doc1"]) == [True]

    backend.upsert(_rows({"doc1": "lower"}))
    reopened = _backend(factory, tmp_path)
    assert {row["object_id"]: row["title"] for row in reopened.scan(ENTITY)} == {"Doc1": "upper", "doc1": "lower"}


@pytest.mark.parametrize(("fmt", "factory", "suffix"), FORMATS, ids=[item[0] for item in FORMATS])
def test_short_ids_keep_the_0122_encoded_record_name(tmp_path, fmt, factory, suffix):
    """Every id whose encoded name is usable and free keeps the 0.1.2.2 record name."""
    backend = _backend(factory, tmp_path)
    ids = ["Doc1", "a b/c?", "中文", "catalog:thing:Doc1", "%41", "x" * 100]
    backend.upsert(_rows({object_id: object_id for object_id in ids}))

    assert _record_names(backend) == sorted(f"{quote(object_id, safe='')}{suffix}" for object_id in ids)
    reopened = _backend(factory, tmp_path)
    assert [row["title"] for row in reopened.get_many(ENTITY, ids)] == ids


@pytest.mark.parametrize(("fmt", "factory", "suffix"), FORMATS, ids=[item[0] for item in FORMATS])
@pytest.mark.parametrize("object_id", ["中" * 27, "知识库" * 21, "😀" * 63, "x" * 300], ids=["cjk27", "cjk63", "emoji63", "ascii300"])
def test_an_id_too_long_for_its_encoded_file_name_round_trips(tmp_path, fmt, factory, suffix, object_id):
    """An id whose encoded name exceeds the file-name limit is stored under a bounded hashed name."""
    backend = _backend(factory, tmp_path)
    backend.upsert(_rows({"ok1": "one", object_id: "long", "ok3": "three"}))

    (name,) = [name for name in _record_names(backend) if name not in (f"ok1{suffix}", f"ok3{suffix}")]
    assert "=" in name and len(name) + file_base._TEMP_NAME_OVERHEAD <= 255
    reopened = _backend(factory, tmp_path)
    assert reopened.get_many(ENTITY, [object_id])[0]["title"] == "long"
    assert reopened.exists_many(ENTITY, [object_id]) == [True]
    assert {row["object_id"] for row in reopened.scan(ENTITY)} == {"ok1", object_id, "ok3"}
    reopened.upsert([hb.ext.RowOp(ENTITY, object_id, op="delete")])
    assert _backend(factory, tmp_path).exists_many(ENTITY, [object_id, "ok1"]) == [False, True]


class LongIdNote(hb.Entity):
    identifier = "record-name-long-id-note"

    title = hb.field(hb.ShortText)


@pytest.mark.slow
def test_a_workspace_stores_a_27_character_cjk_id_on_the_json_and_pickle_backends(tmp_path):
    """A valid 27-character CJK object_id is stored and read through the workspace, Catalog row included."""
    object_id = "知识库" * 9
    for kind in ("json", "pickle"):
        path = hb.utils.path_to_file_uri(tmp_path / kind)
        workspace = hb.HeavenBase(f"record-name-long-{kind}", detached=True, backends={"main": {"type": kind, "path": path}})
        workspace.register(LongIdNote)
        workspace.upsert(LongIdNote, {"object_id": object_id, "title": "long"})
        assert workspace.get(object_id, entity=LongIdNote)["title"] == "long"
        assert workspace.exists(object_id, entity=LongIdNote)


@pytest.mark.parametrize(("fmt", "factory", "suffix"), FORMATS, ids=[item[0] for item in FORMATS])
def test_a_failed_record_write_leaves_memory_as_it_is_on_disk(tmp_path, monkeypatch, fmt, factory, suffix):
    """A row whose file write fails is not kept in memory; rows before it stay written, rows after it are not."""
    backend = _backend(factory, tmp_path)
    replace = file_base.replace_file

    def failing_replace(src, dst):
        if os.path.basename(str(dst)).startswith("bad"):
            raise OSError("disk full")
        return replace(src, dst)

    monkeypatch.setattr(file_base, "replace_file", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        backend.upsert(_rows({"ok1": "one", "bad": "two", "ok3": "three"}))

    assert sorted(backend.data[backend.ws_id][ENTITY]) == ["ok1"]
    assert [row["object_id"] for row in backend.scan(ENTITY)] == ["ok1"]


@pytest.mark.parametrize(("fmt", "factory", "suffix"), FORMATS, ids=[item[0] for item in FORMATS])
def test_a_windows_device_name_id_never_names_a_record_file(tmp_path, fmt, factory, suffix):
    """``con``, ``NUL`` or ``com1.x`` would open a device on Windows, so their records use the hashed name."""
    backend = _backend(factory, tmp_path)
    ids = ["con", "NUL", "com1.x", "lpt9"]
    backend.upsert(_rows({object_id: object_id for object_id in ids}))

    names = _record_names(backend)
    assert len(names) == len(ids)
    assert not any(file_base._is_device_name(name) for name in names)
    assert [row["title"] for row in _backend(factory, tmp_path).get_many(ENTITY, ids)] == ids


@pytest.mark.skipif(os.name == "nt", reason="a POSIX store may hold a 0.1.2.2 record named after a Windows device")
@pytest.mark.parametrize(("fmt", "factory", "suffix"), FORMATS, ids=[item[0] for item in FORMATS])
def test_a_0122_device_name_record_is_read_and_moved_to_the_portable_name_on_its_next_write(tmp_path, fmt, factory, suffix):
    """A 0.1.2.2 POSIX store's ``con`` record stays readable and leaves its device name when rewritten."""
    backend = _backend(factory, tmp_path)
    backend.upsert(_rows({"keep": "keep"}))
    legacy = os.path.join(_entity_dir(backend), f"con{suffix}")
    if fmt == "json":
        dump_json({"object_id": "con", "title": "old"}, legacy)
    else:
        save_pkl({"object_id": "con", "title": "old"}, legacy)

    reopened = _backend(factory, tmp_path)
    assert reopened.get_many(ENTITY, ["con"])[0]["title"] == "old"
    reopened.upsert(_rows({"con": "new"}))

    assert f"con{suffix}" not in _record_names(reopened)
    assert reopened.count(ENTITY) == 2
    assert {row["object_id"]: row["title"] for row in _backend(factory, tmp_path).scan(ENTITY)} == {"keep": "keep", "con": "new"}


def test_hashed_record_names_are_bounded_and_never_end_on_a_split_escape(tmp_path):
    """The readable tail of a hashed name keeps whole ``%XX`` escapes and the name stays short."""
    backend = _backend(hb.backends.JsonFileBackend, tmp_path)
    for object_id in ["中" * 27, "a" + "中" * 27, "ab" + "中" * 27, "Doc1"]:
        name = backend._hashed_name(object_id)
        digest, readable = name[: -len(".json")].split("=", 1)
        assert len(digest) == 32 and len(readable) <= 32
        assert quote(object_id, safe="").startswith(readable)
        assert "%" not in readable[-2:]


@pytest.mark.parametrize(("fmt", "factory", "suffix"), FORMATS, ids=[item[0] for item in FORMATS])
def test_case_variant_ids_keep_their_0122_names_on_a_case_sensitive_file_system(tmp_path, fmt, factory, suffix):
    """Where the file system tells ``Doc1`` from ``doc1``, both keep the encoded name 0.1.2.2 gave them."""
    probe = tmp_path / "Probe"
    probe.write_text("")
    if (tmp_path / "probe").exists():
        pytest.skip("this temporary folder is on a case-insensitive file system")
    backend = _backend(factory, tmp_path)
    backend.upsert(_rows({"Doc1": "upper", "doc1": "lower"}))

    assert _record_names(backend) == sorted([f"Doc1{suffix}", f"doc1{suffix}"])
    assert [row["title"] for row in _backend(factory, tmp_path).get_many(ENTITY, ["Doc1", "doc1"])] == ["upper", "lower"]


def _write_record(path, fmt, row):
    """Write a record file as another file system would, also where its path exceeds Windows' ``MAX_PATH``."""
    if os.name == "nt":
        path = "\\\\?\\" + os.path.abspath(path)  # the extended-length form Windows opens past MAX_PATH
    if fmt == "json":
        dump_json(row, path)
    else:
        save_pkl(row, path)


@pytest.mark.parametrize(("fmt", "factory", "suffix"), FORMATS, ids=[item[0] for item in FORMATS])
def test_a_record_under_an_encoded_name_too_long_to_write_is_still_found_and_moved(tmp_path, fmt, factory, suffix):
    """A record another file system wrote under a long encoded name is read, deleted and moved like any other."""
    object_id = "x" * 240
    backend = _backend(factory, tmp_path)
    backend.upsert(_rows({"keep": "keep"}))
    name = f"{object_id}{suffix}"
    if len(name) > backend._name_max:
        pytest.skip("this temporary folder cannot hold the long encoded name")
    assert len(name) + file_base._TEMP_NAME_OVERHEAD > backend._name_max
    _write_record(os.path.join(_entity_dir(backend), name), fmt, {"object_id": object_id, "title": "old"})

    reopened = _backend(factory, tmp_path)
    assert reopened.get_many(ENTITY, [object_id])[0]["title"] == "old"
    assert reopened.exists_many(ENTITY, [object_id]) == [True]
    reopened.upsert(_rows({object_id: "new"}))
    assert name not in _record_names(reopened)
    assert {row["object_id"]: row["title"] for row in _backend(factory, tmp_path).scan(ENTITY)} == {"keep": "keep", object_id: "new"}

    _write_record(os.path.join(_entity_dir(backend), name), fmt, {"object_id": object_id, "title": "old"})
    deleting = _backend(factory, tmp_path)
    deleting.upsert([hb.ext.RowOp(ENTITY, object_id, op="delete")])
    assert [row["object_id"] for row in _backend(factory, tmp_path).scan(ENTITY)] == ["keep"]


@pytest.mark.parametrize(("fmt", "factory", "suffix"), FORMATS, ids=[item[0] for item in FORMATS])
def test_count_leaves_out_the_original_of_an_interrupted_move_as_scan_does(tmp_path, fmt, factory, suffix):
    """A move to the hashed name interrupted before the original is removed counts the row once."""
    object_id = "x" * 240
    backend = _backend(factory, tmp_path)
    backend.upsert(_rows({"keep": "keep", object_id: "new"}))
    original = f"{object_id}{suffix}"
    if len(original) > backend._name_max:
        pytest.skip("this temporary folder cannot hold the long encoded name")
    _write_record(os.path.join(_entity_dir(backend), original), fmt, {"object_id": object_id, "title": "old"})

    reopened = _backend(factory, tmp_path)
    assert len(_record_names(reopened)) == 3
    assert {row["object_id"]: row["title"] for row in reopened.scan(ENTITY)} == {"keep": "keep", object_id: "new"}
    assert reopened.count(ENTITY) == 2


@pytest.fixture
def short_names(monkeypatch):
    """Make ``123456~1`` resolve to the record of ``1234567890``, as an NTFS 8.3 short name does."""

    def alias(path):
        folder, name = os.path.split(str(path))
        stem, dot, suffix = name.partition(".")
        return os.path.join(folder, f"1234567890.{suffix}") if dot and stem.upper() == "123456~1" else str(path)

    for helper in ("_exists", "delete_file", "load_json", "load_bin"):
        original = getattr(file_base, helper)
        monkeypatch.setattr(file_base, helper, lambda path, *args, _original=original, **kwargs: _original(alias(path), *args, **kwargs))
    replace = file_base.replace_file
    monkeypatch.setattr(file_base, "replace_file", lambda src, dst: replace(src, alias(dst)))


@pytest.mark.parametrize(("fmt", "factory", "suffix"), FORMATS, ids=[item[0] for item in FORMATS])
def test_an_id_spelled_like_a_windows_short_name_never_opens_another_record(short_names, tmp_path, fmt, factory, suffix):
    """``123456~1`` does not report, overwrite or delete the record of ``1234567890`` that its name aliases."""
    backend = _backend(factory, tmp_path)
    backend.upsert(_rows({"1234567890": "long"}))

    assert backend.exists_many(ENTITY, ["123456~1"]) == [False]
    assert backend.get_many(ENTITY, ["123456~1"]) == [None]
    backend.upsert([hb.ext.RowOp(ENTITY, "123456~1", op="delete")])
    backend.upsert(_rows({"123456~1": "alias"}))

    reopened = _backend(factory, tmp_path)
    assert {row["object_id"]: row["title"] for row in reopened.scan(ENTITY)} == {"1234567890": "long", "123456~1": "alias"}
    assert reopened.count(ENTITY) == 2


@pytest.mark.parametrize(("fmt", "factory", "suffix"), FORMATS, ids=[item[0] for item in FORMATS])
@pytest.mark.parametrize("object_id", ["Doc1", "123"])
def test_an_unreadable_record_exists_as_in_0122(tmp_path, fmt, factory, suffix, object_id):
    """``exists`` reports a corrupt record file present whatever the id's letters, as 0.1.2.2 did."""
    backend = _backend(factory, tmp_path)
    backend.upsert(_rows({object_id: "ok"}))
    with open(os.path.join(_entity_dir(backend), f"{object_id}{suffix}"), "w") as handle:
        handle.write("{not json")

    reopened = _backend(factory, tmp_path)
    assert reopened.exists_many(ENTITY, [object_id]) == [True]


@pytest.mark.parametrize(("fmt", "factory", "suffix"), FORMATS, ids=[item[0] for item in FORMATS])
def test_an_id_whose_temporary_path_exceeds_the_windows_path_limit_uses_the_hashed_name(tmp_path, monkeypatch, fmt, factory, suffix):
    """Without long-path support Windows opens at most 259 characters, so a record whose temporary path would not fit is hashed."""
    backend = _backend(factory, tmp_path)
    entity_dir = _entity_dir(backend)
    os.makedirs(entity_dir)
    long_id = "y" * 90
    hashed = backend._hashed_name(long_id)
    room = len(os.path.abspath(os.path.join(entity_dir, hashed))) + file_base._TEMP_NAME_OVERHEAD
    ordinary = f"{long_id}{suffix}"
    assert len(ordinary) + file_base._TEMP_NAME_OVERHEAD <= backend._name_max
    assert len(os.path.abspath(os.path.join(entity_dir, ordinary))) + file_base._TEMP_NAME_OVERHEAD > room

    temp_file = file_base.temp_file

    def bounded_temp_file(path):
        actual = temp_file(path)
        assert len(os.path.abspath(actual)) <= room
        return actual

    monkeypatch.setattr(file_base, "_PATH_MAX", room, raising=False)
    monkeypatch.setattr(file_base, "temp_file", bounded_temp_file)
    backend.upsert(_rows({"ok": "short", long_id: "long"}))

    assert set(_record_names(backend)) == {f"ok{suffix}", hashed}
    reopened = _backend(factory, tmp_path)
    assert [row["title"] for row in reopened.get_many(ENTITY, ["ok", long_id])] == ["short", "long"]
    assert reopened.exists_many(ENTITY, [long_id]) == [True]
    assert reopened.count(ENTITY) == 2
