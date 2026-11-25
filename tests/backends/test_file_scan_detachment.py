"""File backend reads isolate nested values from cached records."""

from functools import partial
from pathlib import Path

import heavenbase as hb
import pytest
from heavenbase.utils.serialize import save_pkl

ENTITY = "file-scan-detached"


class _UncopyableAfterLoad:
    def __init__(self):
        self.values = ["stored"]
        self.restored = False

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.restored = True

    def __deepcopy__(self, memo):
        if self.restored:
            raise TypeError("cannot copy a restored value")
        return type(self)()

    def __reduce_ex__(self, protocol):
        if self.restored:
            raise TypeError("cannot pickle a restored value")
        return super().__reduce_ex__(protocol)


class _SelfCopyingPayload:
    def __init__(self):
        self.values = ["stored"]

    def __deepcopy__(self, memo):
        return self


@pytest.mark.fast
@pytest.mark.parametrize("factory", [hb.backends.JsonFileBackend, hb.backends.PickleFileBackend], ids=["json", "pickle"])
@pytest.mark.parametrize("method", ["scan", "iter_scan", "scan_page"])
def test_file_scan_returns_detached_values(tmp_path, factory, method):
    backend = factory("store", hb.utils.path_to_file_uri(tmp_path / "store"), ws_id="file-scan-detached")
    stored = {"object_id": "x", "tags": ["original"], "details": {"nested": [1]}}
    backend.upsert([hb.ext.RowOp(ENTITY, "x", stored)])
    read = getattr(backend, method)
    if method == "iter_scan":
        read = partial(read, batch_size=1, columns=["tags", "details"])
    elif method == "scan_page":
        read = partial(read, after=None, batch_size=1, columns=["tags", "details"])
    (result,) = read(ENTITY)
    result["tags"].append("local")
    result["details"]["nested"].append(2)

    assert backend.get_many(ENTITY, ["x"]) == [stored]
    (again,) = read(ENTITY)
    assert again["tags"] == ["original"]
    assert again["details"] == {"nested": [1]}


@pytest.mark.fast
@pytest.mark.parametrize("factory", [hb.backends.JsonFileBackend, hb.backends.PickleFileBackend], ids=["json", "pickle"])
def test_file_lookup_does_not_share_values_with_scan_continuation(tmp_path, factory):
    backend = factory("store", hb.utils.path_to_file_uri(tmp_path / "store"), ws_id="file-scan-detached")
    backend.upsert([hb.ext.RowOp(ENTITY, object_id, {"tags": [object_id]}) for object_id in ("a", "b")])
    (first,) = backend.scan_page(ENTITY, after=None, batch_size=1)
    (lookup,) = backend.get_many(ENTITY, ["b"], columns=["tags"])
    lookup["tags"].append("local")

    assert backend.scan_page(ENTITY, after=first["object_id"], batch_size=1) == [{"object_id": "b", "tags": ["b"]}]


@pytest.mark.fast
def test_pickle_scan_preserves_nested_cycles_in_existing_records(tmp_path):
    backend = hb.backends.PickleFileBackend("store", hb.utils.path_to_file_uri(tmp_path / "store"), ws_id="file-scan-detached")
    folder = Path(backend._entity_dir(ENTITY))
    folder.mkdir(parents=True)
    value = []
    value.append(value)
    save_pkl({"object_id": "x", "value": value}, str(folder / "x.pkl"))

    (first,) = backend.scan(ENTITY)
    assert first["value"][0] is first["value"]
    first["value"].append("local")
    (again,) = backend.scan(ENTITY)
    assert len(again["value"]) == 1
    assert again["value"][0] is again["value"]


@pytest.mark.fast
@pytest.mark.parametrize("payload_type", [_UncopyableAfterLoad, _SelfCopyingPayload])
@pytest.mark.parametrize("method", ["scan", "get_many", "iter_scan", "scan_page"])
def test_pickle_reads_reconstruct_the_persisted_value(tmp_path, payload_type, method):
    backend = hb.backends.PickleFileBackend("store", hb.utils.path_to_file_uri(tmp_path), ws_id="file-scan-detached")
    backend.upsert([hb.ext.RowOp(ENTITY, "x", {"payload": payload_type()})])
    readers = {
        "scan": partial(backend.scan, ENTITY),
        "get_many": partial(backend.get_many, ENTITY, ["x"], columns=["payload"]),
        "iter_scan": partial(backend.iter_scan, ENTITY, batch_size=1, columns=["payload"]),
        "scan_page": partial(backend.scan_page, ENTITY, after="", batch_size=1, columns=["payload"]),
    }
    read = readers[method]

    (first,) = read()
    assert first["payload"].values == ["stored"]
    first["payload"].values.append("local")
    (again,) = read()
    assert again["payload"].values == ["stored"]


class _CountedRestore(_UncopyableAfterLoad):
    restores = 0

    def __setstate__(self, state):
        type(self).restores += 1
        super().__setstate__(state)


@pytest.mark.fast
def test_pickle_point_lookup_restores_record_once(tmp_path):
    backend = hb.backends.PickleFileBackend("store", hb.utils.path_to_file_uri(tmp_path), ws_id="file-scan-detached")
    backend.upsert([hb.ext.RowOp(ENTITY, "x", {"payload": _CountedRestore()})])
    _CountedRestore.restores = 0

    found, missing, repeated = backend.get_many(ENTITY, ["x", "missing", "x"], columns=["payload"])
    assert missing is None
    assert _CountedRestore.restores == 2
    found["payload"].values.append("local")
    assert repeated["payload"].values == ["stored"]
    assert backend.get_many(ENTITY, ["x"])[0]["payload"].values == ["stored"]
