"""Contract tests for Backend compare-and-set row replacement."""

from concurrent.futures import ThreadPoolExecutor

import pytest

import heavenbase as hb
from heavenbase.execution.contracts import RowOp
from heavenbase.schema.model import EntitySchema, FieldSchema
from heavenbase.placement.binding import StorageBinding

ENTITY_ID = "sys-cas-contract"


def _schema() -> EntitySchema:
    return EntitySchema(
        ENTITY_ID,
        "CasContract",
        tags=["system"],
        fields={
            "object_id": FieldSchema("object_id", hb.Identifier(), pk=True, required=True),
            "revision": FieldSchema("revision", hb.Integer(), required=True),
            "value": FieldSchema("value", hb.ShortText(), required=True),
        },
    )


def _ensure(backend) -> None:
    schema = _schema()
    bindings = [StorageBinding(ENTITY_ID, name, backend.name, hb.InlineColumn.identifier, "test") for name in schema.fields]
    backend.ensure(schema, bindings)


@pytest.fixture(params=("inmem", "file", "sqlite"))
def cas_backends(request, tmp_path):
    if request.param == "inmem":
        backend = hb.backends.InMemBackend("main", ws_id="cas")
        pair = (backend, backend)
    elif request.param == "file":
        # Two instances over one folder: the record file, not either cache, is compared.
        folder = hb.utils.path_to_file_uri(tmp_path / "cas")
        pair = (
            hb.backends.FileBackend("main", folder, ws_id="cas"),
            hb.backends.FileBackend("main", folder, ws_id="cas"),
        )
    else:
        database = hb.utils.path_to_file_uri(tmp_path / "cas.db")
        pair = (
            hb.backends.SQLiteBackend("main", database, ws_id="cas"),
            hb.backends.SQLiteBackend("main", database, ws_id="cas"),
        )
    for backend in pair:
        _ensure(backend)
    return pair


def _row(revision: int, value: str, object_id: str = "state") -> RowOp:
    return RowOp(ENTITY_ID, object_id, {"revision": revision, "value": value})


def test_compare_upsert_create_replace_and_failed_comparison(cas_backends):
    first, second = cas_backends

    assert first.compare_upsert(_row(0, "created"), revision_field="revision", expected_revision=None)
    assert not second.compare_upsert(_row(0, "duplicate"), revision_field="revision", expected_revision=None)
    assert not second.compare_upsert(_row(2, "wrong"), revision_field="revision", expected_revision=1)
    assert second.get_many(ENTITY_ID, ["state"])[0] == {"object_id": "state", "revision": 0, "value": "created"}
    assert second.compare_upsert(_row(1, "replaced"), revision_field="revision", expected_revision=0)
    assert first.get_many(ENTITY_ID, ["state"])[0] == {"object_id": "state", "revision": 1, "value": "replaced"}


def test_compare_upsert_races_have_exactly_one_winner(cas_backends):
    first, second = cas_backends
    assert first.compare_upsert(_row(0, "seed"), revision_field="revision", expected_revision=None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: args[0].compare_upsert(_row(1, args[1]), revision_field="revision", expected_revision=0),
                ((first, "first"), (second, "second")),
            )
        )

    assert sorted(results) == [False, True]
    stored = first.get_many(ENTITY_ID, ["state"])[0]
    assert stored["revision"] == 1
    assert stored["value"] in {"first", "second"}


def test_compare_upsert_create_race_has_exactly_one_winner(cas_backends):
    first, second = cas_backends

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: args[0].compare_upsert(_row(0, args[1], "new"), revision_field="revision", expected_revision=None),
                ((first, "first"), (second, "second")),
            )
        )

    assert sorted(results) == [False, True]
    assert first.get_many(ENTITY_ID, ["new"])[0]["value"] in {"first", "second"}


def test_compare_upsert_fails_loudly_when_backend_is_unqualified():
    backend = hb.backends.Backend("plain", ws_id="cas")

    with pytest.raises(NotImplementedError, match="atomic compare_upsert"):
        backend.compare_upsert(_row(0, "value"), revision_field="revision", expected_revision=None)


def test_compare_upsert_rejects_delete_rows(cas_backends):
    first, _ = cas_backends

    with pytest.raises(ValueError, match="upsert RowOp"):
        first.compare_upsert(RowOp(ENTITY_ID, "state", op="delete"), revision_field="revision", expected_revision=None)


def test_compare_upsert_scope_is_exposed_as_an_effective_backend_tag(cas_backends):
    first, _ = cas_backends

    expected = "provider" if first.type == "sqlite" else "process"
    result = first.tag("compare-upsert")
    assert result is not None
    assert result.value == expected
    assert result.source == "record"


def test_file_compare_upsert_writes_the_record_file(tmp_path):
    """A successful file compare_upsert must persist the row, not only cache it."""
    folder = hb.utils.path_to_file_uri(tmp_path / "cas")
    writer = hb.backends.FileBackend("main", folder, ws_id="cas")
    _ensure(writer)

    assert writer.compare_upsert(_row(0, "created"), revision_field="revision", expected_revision=None)
    assert writer.compare_upsert(_row(1, "replaced"), revision_field="revision", expected_revision=0)

    reader = hb.backends.FileBackend("main", folder, ws_id="cas")
    assert writer.get_many(ENTITY_ID, ["state"])[0] == {"object_id": "state", "revision": 1, "value": "replaced"}
    assert reader.get_many(ENTITY_ID, ["state"])[0] == {"object_id": "state", "revision": 1, "value": "replaced"}
    assert reader.scan(ENTITY_ID) == [{"object_id": "state", "revision": 1, "value": "replaced"}]
