"""Persistent Registry contract tests over qualified Backends."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Barrier, Event

import pytest

import heavenbase as hb
from heavenbase.backends.base import Backend
from heavenbase.execution.contracts import RowOp
from heavenbase.backends.inmem.backend import InMemBackend
from heavenbase.context.registry import HeavenBaseRegistryStore, _schema
from heavenbase.utils.serialize import dumps_json, loads_json

ENTITY_ID = "sys-registry-state"


def _store(backend):
    return HeavenBaseRegistryStore(backend)


def test_registry_control_schema_retains_fixed_private_text_widths():
    """Control-plane DDL should stay fixed while public defaults remain 63."""
    schema = _schema()

    assert hb.Identifier().max_chars == 63
    assert schema.get("object_id").dtype.max_chars == 255
    assert schema.get("digest").dtype.max_chars == 255
    assert schema.get("updated_at").dtype.max_chars == 255


@pytest.fixture(params=("inmem", "sqlite"))
def registry_backend(request, tmp_path):
    if request.param == "inmem":
        return hb.backends.InMemBackend("registry", ws_id="default")
    return hb.backends.SQLiteBackend("registry", hb.utils.path_to_file_uri(tmp_path / "registry.db"), ws_id="default")


def test_absent_registry_load_is_read_only(registry_backend):
    registry = hb.Registry.load("test-empty", store=_store(registry_backend))

    assert registry.persistent
    assert registry.revision == 0
    assert registry.list() == ()
    assert registry_backend.get_many(ENTITY_ID, ["test-empty"]) == [None]


def test_persistent_registry_round_trips_crud_aliases_groups_and_none(registry_backend):
    registry = hb.Registry.load("test-roundtrip", store=_store(registry_backend), normalize=str.lower, normalize_group=str.lower)

    registry.create("Alpha", {"count": 1}, aliases=("A",), groups=("Builtin", "All"))
    registry.create("none", None, groups=("All",))
    registry.update("a", derive=lambda value: {"count": value["count"] + 1})

    loaded = hb.Registry.load("test-roundtrip", store=_store(registry_backend), normalize=str.lower, normalize_group=str.lower)
    assert loaded.revision == 3
    assert loaded.get("A") == {"count": 2}
    assert loaded["none"] is None
    assert "none" in loaded
    assert loaded.aliases("alpha") == ("A",)
    assert loaded.groups("alpha") == ("Builtin", "All")
    assert loaded.list("builtin") == ("alpha",)

    assert loaded.delete("A") == {"count": 2}
    reloaded = hb.Registry.load("test-roundtrip", store=_store(registry_backend), normalize=str.lower, normalize_group=str.lower)
    assert reloaded.list() == ("none",)
    assert reloaded.list("all") == ("none",)


def test_default_codec_round_trips_recursive_tuple_keys(registry_backend):
    key = ("handler", ("sql", "select"))
    alias = ("handler", ("db", "select"))
    group = ("family", "sql")
    registry = hb.Registry.load("test-tuples", store=_store(registry_backend))

    registry.create(key, {"enabled": True}, aliases=(alias,), groups=(group,))
    loaded = hb.Registry.load("test-tuples", store=_store(registry_backend))

    assert loaded.resolve(alias) == key
    assert loaded.get(alias) == {"enabled": True}
    assert loaded.list(group) == (key,)
    assert loaded.groups(key) == (group,)


def test_registry_snapshot_canonical_bytes_are_stable(registry_backend):
    registry = hb.Registry.load("contract", store=_store(registry_backend))
    registry.create(
        ("kind", "item"),
        {"active": True},
        aliases=(("kind", "alias"),),
        groups=(("family", "demo"),),
    )

    row = registry_backend.get_many(ENTITY_ID, ["contract"])[0]

    assert row["revision"] == 1
    assert row["digest"] == "4136a2fd546ab93c32727346e66c09da7d554dd2dfc2aee46dd58f5091b58a8d"
    assert loads_json(row["payload"])["codec"] == 1


def test_custom_value_codec_round_trips_typed_values(registry_backend):
    @dataclass(frozen=True)
    class Value:
        count: int

    registry = hb.Registry.load(
        "test-values",
        store=_store(registry_backend),
        encode_value=lambda value: {"count": value.count},
        decode_value=lambda value: Value(value["count"]),
    )
    registry.create("item", Value(3))

    loaded = hb.Registry.load(
        "test-values",
        store=_store(registry_backend),
        encode_value=lambda value: {"count": value.count},
        decode_value=lambda value: Value(value["count"]),
    )
    assert loaded["item"] == Value(3)


def test_codec_functions_must_be_paired(registry_backend):
    with pytest.raises(TypeError, match="key encoder and decoder"):
        hb.Registry.load("test-pair", store=_store(registry_backend), encode_key=str)
    with pytest.raises(TypeError, match="value encoder and decoder"):
        hb.Registry.load("test-pair", store=_store(registry_backend), decode_value=lambda value: value)


def test_unhandled_key_types_fail_before_backend_mutation(registry_backend):
    registry = hb.Registry.load("test-key-type", store=_store(registry_backend))

    with pytest.raises(TypeError, match="strings or recursive tuples"):
        registry.create(42, "value")

    assert len(registry) == 0
    assert registry.revision == 0
    assert registry_backend.get_many(ENTITY_ID, ["test-key-type"]) == [None]


def test_cas_conflict_preserves_local_state_until_explicit_refresh(registry_backend):
    first = hb.Registry.load("test-conflict", store=_store(registry_backend))
    second = hb.Registry.load("test-conflict", store=_store(registry_backend))

    first.create("value", 1)
    with pytest.raises(hb.RegistryConflictError, match="revision 0"):
        second.create("other", 2)

    assert second.revision == 0
    assert second.list() == ()
    assert second.refresh()
    assert second.revision == 1
    assert second.items() == (("value", 1),)
    second.update("value", derive=lambda value: value + 1)
    assert first.refresh()
    assert first["value"] == 2


def test_equal_or_missing_refresh_is_a_noop(registry_backend):
    registry = hb.Registry.load("test-refresh", store=_store(registry_backend))

    assert not registry.refresh()
    registry.create("value", 1)
    assert not registry.refresh()


def test_refresh_decodes_only_a_snapshot_newer_than_the_published_one(registry_backend, monkeypatch):
    # A config manager refreshes its Registry every refresh interval; decoding the
    # unchanged snapshot each time cost a full payload decode and copy per refresh.
    registry = hb.Registry.load("test-refresh-decode", store=_store(registry_backend))
    registry.create("value", {"items": [1, 2]})
    decoded = []
    real_decode = registry._codec.decode
    monkeypatch.setattr(registry._codec, "decode", lambda payload: decoded.append(payload) or real_decode(payload))

    publication = registry.publication
    assert not registry.refresh()
    assert decoded == [] and registry.publication == publication

    peer = hb.Registry.load("test-refresh-decode", store=_store(registry_backend))
    peer.update("value", {"items": [3]})
    assert registry.refresh()
    assert len(decoded) == 1
    assert registry["value"] == {"items": [3]} and registry.publication == publication + 1


def _store_snapshot(backend, identifier, revision, payload, digest):
    row = _stored_row(backend, identifier)
    backend.upsert([RowOp(ENTITY_ID, identifier, {"revision": revision, "payload": payload, "digest": digest, "updated_at": row["updated_at"]})])


def test_refresh_verifies_the_digest_of_the_published_snapshot(registry_backend):
    # Skipping the decode of an unchanged snapshot also skipped its digest check,
    # so a payload replaced under the published revision and digest was served on.
    registry = hb.Registry.load("test-refresh-verify", store=_store(registry_backend))
    registry.create("value", {"items": [1, 2]})
    old_payload = _stored_row(registry_backend, registry.identifier)["payload"]
    registry.update("value", {"items": [3]})
    published = _stored_row(registry_backend, registry.identifier)
    _store_snapshot(registry_backend, registry.identifier, published["revision"], old_payload, published["digest"])
    publication = registry.publication

    with pytest.raises(hb.RegistryIntegrityError, match="digest does not match"):
        registry.refresh()
    assert registry["value"] == {"items": [3]} and registry.publication == publication


def test_refresh_decodes_a_snapshot_at_the_published_revision_with_another_digest(registry_backend, monkeypatch):
    registry = hb.Registry.load("test-refresh-digest", store=_store(registry_backend))
    registry.create("value", {"items": [1, 2]})
    old_payload = _stored_row(registry_backend, registry.identifier)["payload"]
    registry.update("value", {"items": [3]})
    revision = registry.revision
    decoded = []
    real_decode = registry._codec.decode
    monkeypatch.setattr(registry._codec, "decode", lambda payload: decoded.append(payload) or real_decode(payload))

    # A consistent snapshot at the published revision is decoded and validated,
    # and the equal revision keeps the published state.
    _store_snapshot(registry_backend, registry.identifier, revision, old_payload, registry._codec.digest(revision, old_payload))
    assert not registry.refresh()
    assert len(decoded) == 1 and registry["value"] == {"items": [3]}

    _store_snapshot(registry_backend, registry.identifier, revision, old_payload, "0" * 64)
    with pytest.raises(hb.RegistryIntegrityError, match="digest does not match"):
        registry.refresh()
    assert len(decoded) == 1


def test_refresh_without_hashing_decodes_and_validates_the_stored_snapshot(registry_backend, monkeypatch):
    registry = hb.Registry.load("test-refresh-nohash", store=_store(registry_backend), hash_fn=None)
    registry.create("value", 1)
    decoded = []
    real_decode = registry._codec.decode
    monkeypatch.setattr(registry._codec, "decode", lambda payload: decoded.append(payload) or real_decode(payload))

    assert not registry.refresh()
    assert len(decoded) == 1

    _replace_payload(registry_backend, registry.identifier, lambda payload: payload.update(codec=99))
    with pytest.raises(hb.RegistryIntegrityError, match="codec version 99"):
        registry.refresh()
    assert registry["value"] == 1


class _ReadOnlyInMemBackend(InMemBackend):
    compare_upsert = Backend.compare_upsert


def test_unsupported_backend_write_preserves_local_state():
    backend = _ReadOnlyInMemBackend("readonly", ws_id="default")
    registry = hb.Registry.load("test-readonly", store=_store(backend))

    with pytest.raises(NotImplementedError, match="atomic compare_upsert"):
        registry.create("value", 1)

    assert registry.revision == 0
    assert registry.list() == ()


class _CountingInMemBackend(InMemBackend):
    def __init__(self) -> None:
        super().__init__("counting", ws_id="default")
        self.reads = 0
        self.compares = 0

    def get_many(self, entity_id, object_ids):
        self.reads += 1
        return super().get_many(entity_id, object_ids)

    def compare_upsert(self, row, *, revision_field, expected_revision):
        self.compares += 1
        return super().compare_upsert(row, revision_field=revision_field, expected_revision=expected_revision)


def test_hot_reads_do_no_backend_io_serialization_or_digest_work():
    backend = _CountingInMemBackend()
    registry = hb.Registry.load("test-hot-read", store=_store(backend))
    registry.create("item", {"count": 1}, aliases=("alias",), groups=("group",))
    reads = backend.reads
    compares = backend.compares

    for _ in range(1_000):
        assert registry.get("alias") == {"count": 1}
        assert registry.list("group") == ("item",)
        assert registry.snapshot()["item"] == {"count": 1}

    assert backend.reads == reads
    assert backend.compares == compares


def test_max_bytes_fails_before_backend_io_and_local_publish():
    backend = _CountingInMemBackend()
    registry = hb.Registry.load("test-max-bytes", store=_store(backend), max_bytes=100)
    compares = backend.compares

    with pytest.raises(OverflowError, match="encoded snapshot"):
        registry.create("large", "x" * 1_000)

    assert backend.compares == compares
    assert registry.revision == 0
    assert registry.list() == ()


def test_encoding_failure_preserves_backend_and_local_state():
    backend = _CountingInMemBackend()
    registry = hb.Registry.load(
        "test-encode-failure",
        store=_store(backend),
        encode_value=lambda value: {"not-json": object()},
        decode_value=lambda value: value,
    )
    compares = backend.compares

    with pytest.raises(TypeError, match="not JSON encodable"):
        registry.create("value", 1)

    assert backend.compares == compares
    assert registry.revision == 0
    assert registry.list() == ()


class _FailingCasBackend(InMemBackend):
    def compare_upsert(self, row, *, revision_field, expected_revision):
        raise OSError("simulated persistence failure")


def test_backend_failure_preserves_local_state():
    backend = _FailingCasBackend("failing", ws_id="default")
    registry = hb.Registry.load("test-backend-failure", store=_store(backend))

    with pytest.raises(OSError, match="simulated persistence failure"):
        registry.create("value", 1)

    assert registry.revision == 0
    assert registry.list() == ()
    assert backend.get_many(ENTITY_ID, [registry.identifier]) == [None]


def test_invalid_capacity_and_hash_options_fail_during_load(registry_backend):
    with pytest.raises(ValueError, match="max_bytes"):
        hb.Registry.load("test-options", store=_store(registry_backend), max_bytes=0)
    with pytest.raises(ValueError, match="Unsupported Registry hash"):
        hb.Registry.load("test-options", store=_store(registry_backend), hash_fn="md5")


@pytest.mark.parametrize("value", (True, "1", 1.0, 1.5, -1))
def test_persistent_registry_rejects_non_integer_byte_limits(registry_backend, value):
    with pytest.raises(ValueError, match="positive integer or None"):
        hb.Registry.load("test-byte-limit", store=_store(registry_backend), max_bytes=value)


def _stored_row(backend, identifier):
    row = backend.get_many(ENTITY_ID, [identifier])[0]
    assert row is not None
    return row


def _replace_payload(backend, identifier, mutate):
    row = _stored_row(backend, identifier)
    payload = loads_json(row["payload"].decode("utf-8"))
    mutate(payload)
    encoded = dumps_json(payload, sort_keys=True, compact=True).encode("utf-8")
    backend.upsert(
        [
            RowOp(
                ENTITY_ID,
                identifier,
                {
                    "revision": row["revision"],
                    "payload": encoded,
                    "digest": "",
                    "updated_at": row["updated_at"],
                },
            )
        ]
    )


def test_corrupt_digest_fails_loudly(registry_backend):
    registry = hb.Registry.load("test-digest", store=_store(registry_backend))
    registry.create("value", 1)
    row = _stored_row(registry_backend, registry.identifier)
    registry_backend.upsert(
        [
            RowOp(
                ENTITY_ID,
                registry.identifier,
                {
                    "revision": row["revision"],
                    "payload": row["payload"],
                    "digest": "corrupt",
                    "updated_at": row["updated_at"],
                },
            )
        ]
    )

    with pytest.raises(hb.RegistryIntegrityError, match="digest"):
        hb.Registry.load("test-digest", store=_store(registry_backend))


def test_unknown_codec_version_fails_loudly():
    backend = hb.backends.InMemBackend("registry", ws_id="default")
    registry = hb.Registry.load("test-codec", store=_store(backend), hash_fn=None)
    registry.create("value", 1)
    row = _stored_row(backend, registry.identifier)
    payload = loads_json(row["payload"].decode("utf-8"))
    payload["codec"] = 99
    corrupted = dumps_json(payload, sort_keys=True, compact=True).encode("utf-8")
    backend.upsert(
        [
            RowOp(
                ENTITY_ID,
                registry.identifier,
                {
                    "revision": row["revision"],
                    "payload": corrupted,
                    "digest": "",
                    "updated_at": row["updated_at"],
                },
            )
        ]
    )

    with pytest.raises(hb.RegistryIntegrityError, match="codec version 99"):
        hb.Registry.load("test-codec", store=_store(backend), hash_fn=None)


def test_duplicate_persisted_identities_fail_loudly():
    backend = hb.backends.InMemBackend("registry", ws_id="default")
    registry = hb.Registry.load("test-duplicates", store=_store(backend), hash_fn=None)
    registry.create("value", 1)
    _replace_payload(backend, registry.identifier, lambda payload: payload["items"].append(payload["items"][0]))

    with pytest.raises(hb.RegistryIntegrityError, match="duplicate identities"):
        hb.Registry.load("test-duplicates", store=_store(backend), hash_fn=None)


def test_inconsistent_persisted_alias_metadata_fails_loudly():
    backend = hb.backends.InMemBackend("registry", ws_id="default")
    registry = hb.Registry.load("test-alias-metadata", store=_store(backend), hash_fn=None)
    registry.create("value", 1, aliases=("alias",))
    _replace_payload(backend, registry.identifier, lambda payload: payload["alias_names"].clear())

    with pytest.raises(hb.RegistryIntegrityError, match="alias metadata"):
        hb.Registry.load("test-alias-metadata", store=_store(backend), hash_fn=None)


def test_cross_key_duplicate_persisted_aliases_fail_loudly():
    backend = hb.backends.InMemBackend("registry", ws_id="default")
    registry = hb.Registry.load("test-alias-owner", store=_store(backend), hash_fn=None)
    registry.create("first", 1, aliases=("shared",))
    registry.create("second", 2)

    def duplicate_alias(payload):
        alias = payload["alias_names"][0][1][0]
        second_key = payload["alias_names"][1][0]
        payload["alias_names"][1][1].append(alias)
        payload["aliases"][0][1] = second_key

    _replace_payload(backend, registry.identifier, duplicate_alias)

    with pytest.raises(hb.RegistryIntegrityError, match="duplicate identities"):
        hb.Registry.load("test-alias-owner", store=_store(backend), hash_fn=None)


def test_persisted_identities_must_match_the_requested_normalizers():
    backend = hb.backends.InMemBackend("registry", ws_id="default")
    registry = hb.Registry.load("test-normalizers", store=_store(backend))
    registry.create("MixedCase", 1)

    with pytest.raises(hb.RegistryIntegrityError, match="violate its normalizers"):
        hb.Registry.load("test-normalizers", store=_store(backend), normalize=str.lower)


def test_sqlite_registry_first_load_recovers_from_a_concurrent_table_creator(tmp_path, monkeypatch):
    from heavenbase.database.database import Database

    database = hb.utils.path_to_file_uri(tmp_path / "first-load.db")
    first = hb.backends.SQLiteBackend("first", database, ws_id="default")
    second = hb.backends.SQLiteBackend("second", database, ws_id="default")
    first_db = first.db
    barrier = Barrier(2)
    created = Event()
    original = Database.create_table

    raced: set[int] = set()

    def raced_create(self, table, checkfirst=True):
        # Only each backend's first CREATE races; the loser's retry checks first, as it would without the race.
        if table.name != ENTITY_ID.replace("-", "_") or id(self) in raced:
            return original(self, table, checkfirst=checkfirst)
        raced.add(id(self))
        barrier.wait()
        if self is first_db:
            try:
                return original(self, table, checkfirst=False)
            finally:
                created.set()
        assert created.wait(timeout=5)
        return original(self, table, checkfirst=False)

    monkeypatch.setattr(Database, "create_table", raced_create)
    with ThreadPoolExecutor(max_workers=2) as pool:
        loaded = list(pool.map(lambda backend: hb.Registry.load("load-race", store=_store(backend)), (first, second)))

    assert all(registry.revision == 0 for registry in loaded)
    assert all(registry.persistent for registry in loaded)


def test_refresh_rejects_persisted_revision_rollback():
    backend = hb.backends.InMemBackend("registry", ws_id="default")
    registry = hb.Registry.load("test-rollback", store=_store(backend))
    registry.create("value", 1)
    first_row = _stored_row(backend, registry.identifier)
    registry.update("value", 2)
    backend.upsert(
        [
            RowOp(
                ENTITY_ID,
                registry.identifier,
                {name: first_row[name] for name in ("revision", "payload", "digest", "updated_at")},
            )
        ]
    )

    with pytest.raises(hb.RegistryIntegrityError, match="older than local"):
        registry.refresh()
    assert registry.revision == 2
    assert registry["value"] == 2


def test_parallel_registry_writers_have_one_cas_winner(registry_backend):
    first = hb.Registry.load("test-parallel-cas", store=_store(registry_backend))
    second = hb.Registry.load("test-parallel-cas", store=_store(registry_backend))

    def create(registry, key):
        try:
            registry.create(key, 1)
            return "won"
        except hb.RegistryConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda args: create(*args), ((first, "first"), (second, "second"))))

    assert sorted(outcomes) == ["conflict", "won"]
    loaded = hb.Registry.load("test-parallel-cas", store=_store(registry_backend))
    assert loaded.list() in (("first",), ("second",))


@pytest.mark.anyio
async def test_async_persistent_operations_share_the_sync_contract(registry_backend):
    registry = await hb.Registry.aload("test-async", store=_store(registry_backend))

    await registry.acreate("counter", 0)
    await asyncio.gather(*(registry.aupdate("counter", derive=lambda value: value + 1) for _ in range(20)))
    loaded = await hb.Registry.aload("test-async", store=_store(registry_backend))

    assert loaded["counter"] == 20
    assert loaded.revision == 21
