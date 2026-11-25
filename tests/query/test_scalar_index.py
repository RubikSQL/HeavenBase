"""Scalar index intent, resolution, and enforcement contracts."""

import os

import pytest

import heavenbase as hb
from heavenbase.backends.capabilities import ScalarIndexSpec
from heavenbase.backends.file.base import FileBackend
from heavenbase.execution.contracts import RowOp


class _NoScalarIndexInMemBackend(hb.backends.InMemBackend):
    """Importable InMem fixture with scalar-index support disabled."""

    identifier = "no-scalar-index-inmem-test"


@pytest.mark.parametrize("flag", ["value", "unique", "composite", "composite_unique", "opt_out"])
@pytest.mark.parametrize("invalid", ["false", 0, None])
def test_scalar_index_tag_flags_require_real_booleans(flag, invalid):
    """Scalar-index metadata must not reinterpret truthy strings or integer sentinels."""
    with pytest.raises(TypeError, match="must be bool"):
        ScalarIndexSpec(**{flag: invalid})


@pytest.mark.parametrize("field", ["supported_types", "default_types"])
def test_scalar_index_tag_type_sets_require_sequences(field):
    """Logical-type declarations must not split one string into characters."""
    with pytest.raises(TypeError, match="must be a sequence"):
        ScalarIndexSpec(**{field: "integer"})


def test_field_index_and_unique_are_authored_outside_logical_schema():
    """Index intent should resolve into StoragePlan without polluting FieldSchema."""

    class Account(hb.Entity):
        identifier = "index-account"

        email = hb.field(hb.ShortText).unique()
        active = hb.field(hb.Boolean).index()
        note = hb.field(hb.ShortText).index(enabled=False)

    ws = hb.HeavenBase("index-account-ws", detached=True, backends={"main": {"type": "sqlite", "database": ":memory:"}})
    ws.register(Account)
    plan = ws.storage_plans[Account.identifier]

    assert not hasattr(Account.schema().get("email"), "index")
    assert [(binding.fields, binding.backend, binding.source) for binding in plan.constraints] == [(("email",), "main", "field")]
    assert (("active",), "main", "field") in [(binding.fields, binding.backend, binding.source) for binding in plan.indexes]
    assert not plan.has_index("note", backend="main")


def test_sqlite_reconciles_default_opt_out_explicit_and_composite_unique_indexes():
    """SQLite should materialize exactly the resolved scalar index and constraint plan."""

    class Product(hb.Entity):
        identifier = "index-product"
        __indexes__ = [hb.index("tenant", "created_at")]
        __constraints__ = [hb.unique("tenant", "sku")]

        tenant = hb.field(hb.ShortText)
        sku = hb.field(hb.ShortText).index(enabled=False)
        created_at = hb.field(hb.Timestamp)
        active = hb.field(hb.Boolean).index()
        note = hb.field(hb.ShortText).index(enabled=False)

    ws = hb.HeavenBase("index-product-ws", detached=True, backends={"main": {"type": "sqlite", "database": ":memory:"}})
    ws.register(Product)
    backend = ws.backends.get("main")
    indexes = backend.db.index_column_sets(backend.tables[Product.identifier])

    assert indexes[("tenant",)] is False  # provider default
    assert indexes[("active",)] is False  # explicit value index outside the default type subset
    assert ("note",) not in indexes
    assert ("sku",) not in indexes
    assert indexes[("tenant", "created_at")] is False
    assert indexes[("tenant", "sku")] is True

    ws.upsert(Product, {"object_id": "p1", "tenant": "acme", "sku": "one", "created_at": 1, "active": True, "note": "a"})
    with pytest.raises(Exception, match="(?i)unique"):
        ws.upsert(Product, {"object_id": "p2", "tenant": "acme", "sku": "one", "created_at": 2, "active": False, "note": "b"})


def test_inmem_index_routes_queries_and_enforces_unique_batch_atomically():
    """In-memory declared indexes should accelerate equality and reject whole bad batches."""

    class User(hb.Entity):
        identifier = "index-user"

        email = hb.field(hb.ShortText).unique()
        team = hb.field(hb.ShortText).index()

    ws = hb.HeavenBase("index-user-ws", detached=True, backends={"main": {"type": "inmem"}})
    ws.register(User)
    ws.upsert_many(User, [{"object_id": "u1", "email": "a@x", "team": "red"}, {"object_id": "u2", "email": "b@x", "team": "blue"}])

    step = ws.query(User).where(User.team == "red").explain()["steps"][0]
    assert step["handler_mode"] == "native"
    assert ws.query(User).where(User.team == "red").execute().ids == ["u1"]

    with pytest.raises(ValueError, match="Unique constraint"):
        ws.upsert_many(User, [{"object_id": "u3", "email": "c@x", "team": "red"}, {"object_id": "u4", "email": "c@x", "team": "blue"}])
    assert ws.get("u3", entity=User) is None
    assert ws.get("u4", entity=User) is None


def test_inmem_unique_field_answers_native_equality_lookups():
    """A single-field unique constraint is a lookup path, so native eq/in must find its rows."""

    class Member(hb.Entity):
        identifier = "index-unique-lookup"

        email = hb.field(hb.ShortText).unique()
        team = hb.field(hb.ShortText)

    ws = hb.HeavenBase("index-unique-lookup-ws", detached=True, backends={"main": {"type": "inmem"}})
    try:
        ws.register(Member)
        ws.upsert_many(Member, [{"object_id": "u1", "email": "a@x", "team": "red"}, {"object_id": "u2", "email": "b@x", "team": "blue"}])

        assert ws.query(Member).where(Member.email == "b@x").explain()["steps"][0]["handler_mode"] == "native"
        assert ws.query(Member).where(Member.email == "b@x").execute().ids == ["u2"]
        assert sorted(ws.query(Member).where({"email": {"$in": ["a@x", "b@x"]}}).execute().ids) == ["u1", "u2"]
        ws.upsert(Member, {"object_id": "u2", "email": "c@x", "team": "blue"})
        assert ws.query(Member).where(Member.email == "b@x").execute().ids == []
        assert ws.query(Member).where(Member.email == "c@x").execute().ids == ["u2"]
    finally:
        ws.drop()


@pytest.mark.parametrize("backend_type", ["file", "json", "pickle"])
def test_file_backends_enforce_unique_constraints(tmp_path, backend_type):
    """A File Backend owns ``.unique()`` enforcement, so it must reject duplicates as inmem and SQL do."""

    class Account(hb.Entity):
        identifier = "index-file-unique"

        email = hb.field(hb.ShortText).optional().unique()
        tenant = hb.field(hb.ShortText)
        sku = hb.field(hb.ShortText)
        __constraints__ = [hb.unique("tenant", "sku")]

    def config():
        return {"type": backend_type, "path": hb.utils.path_to_file_uri(tmp_path / "rows")}

    ws = hb.HeavenBase(f"index-file-unique-{backend_type}-ws", detached=True, backends={"main": config()})
    try:
        ws.register(Account)
        ws.upsert(Account, {"object_id": "u1", "email": "a@x", "tenant": "t", "sku": "s1"})

        def ids():
            return sorted(ws.query(Account).execute().ids)

        with pytest.raises(ValueError, match="Unique constraint"):
            ws.upsert(Account, {"object_id": "u2", "email": "a@x", "tenant": "t", "sku": "s2"})
        with pytest.raises(ValueError, match="Unique constraint"):
            ws.upsert_many(
                Account,
                [
                    {"object_id": "u3", "email": "b@x", "tenant": "t", "sku": "s3"},
                    {"object_id": "u4", "email": "b@x", "tenant": "t", "sku": "s4"},
                ],
            )
        with pytest.raises(ValueError, match="Unique constraint"):
            ws.upsert(Account, {"object_id": "u5", "email": "e@x", "tenant": "t", "sku": "s1"})
        assert ids() == ["u1"]
        assert [ws.get(object_id, entity=Account) for object_id in ("u2", "u3", "u4", "u5")] == [None] * 4

        # Replacing a row keeps its own value; one batch may hand a value from one row to another.
        ws.upsert(Account, {"object_id": "u1", "email": "a@x", "tenant": "t", "sku": "s1"})
        ws.upsert_many(
            Account,
            [
                {"object_id": "u1", "email": "c@x", "tenant": "t", "sku": "s1"},
                {"object_id": "u6", "email": "a@x", "tenant": "t", "sku": "s6"},
            ],
        )
        # A missing value never conflicts.
        ws.upsert_many(
            Account,
            [
                {"object_id": "u7", "email": None, "tenant": "t", "sku": "s7"},
                {"object_id": "u8", "email": None, "tenant": "t", "sku": "s8"},
            ],
        )
        assert ids() == ["u1", "u6", "u7", "u8"]
        assert ws.query(Account).where({"email": "a@x"}).execute().ids == ["u6"]

        # Another instance of the same workspace over the same folder checks the record files, not its own stale cache.
        other = hb.HeavenBase(f"index-file-unique-{backend_type}-ws", detached=True, backends={"main": config()})
        other.register(Account)
        assert sorted(other.query(Account).execute().ids) == ["u1", "u6", "u7", "u8"]
        ws.upsert(Account, {"object_id": "u9", "email": "z@x", "tenant": "t", "sku": "s9"})
        with pytest.raises(ValueError, match="Unique constraint"):
            other.upsert(Account, {"object_id": "u10", "email": "z@x", "tenant": "t", "sku": "s10"})
        ws.delete((Account, "u9"))
        other.upsert(Account, {"object_id": "u10", "email": "z@x", "tenant": "t", "sku": "s10"})
        assert ids() == ["u1", "u10", "u6", "u7", "u8"]
    finally:
        ws.drop()


@pytest.mark.parametrize("backend_type", ["json", "pickle"])
def test_file_backend_unique_check_reads_only_changed_record_files(tmp_path, monkeypatch, backend_type):
    """A unique check rereads only changed record files, so loading rows one by one stays linear, and still sees other writers."""

    class Member(hb.Entity):
        identifier = "index-file-unique-reads"

        email = hb.field(hb.ShortText).unique()

    def config():
        return {"type": backend_type, "path": hb.utils.path_to_file_uri(tmp_path / "rows")}

    suffix = ".json" if backend_type == "json" else ".pkl"
    ws = hb.HeavenBase(f"index-file-unique-reads-{backend_type}-ws", detached=True, backends={"main": config()})
    try:
        ws.register(Member)
        for i in range(20):
            ws.upsert(Member, {"object_id": f"u{i:02d}", "email": f"{i}@x"})
        member_dir = next(path.parent.name for path in (tmp_path / "rows").rglob(f"u00{suffix}"))
        reads: list[str] = []
        load_record = FileBackend._load_record

        def counting_load_record(self, path):
            # Count only Member record files; the workspace also reads its catalog rows' own record files.
            if os.path.basename(os.path.dirname(path)) == member_dir:
                reads.append(os.path.basename(path))
            return load_record(self, path)

        monkeypatch.setattr(FileBackend, "_load_record", counting_load_record)
        ws.upsert(Member, {"object_id": "u20", "email": "20@x"})
        assert set(reads) <= {f"u19{suffix}", f"u20{suffix}"}
        reads.clear()
        ws.upsert(Member, {"object_id": "u20", "email": "20b@x"})
        assert set(reads) <= {f"u20{suffix}"}

        # Rows another instance of the same workspace changes, adds or deletes are read again.
        other = hb.HeavenBase(f"index-file-unique-reads-{backend_type}-ws", detached=True, backends={"main": config()})
        other.register(Member)
        other.upsert(Member, {"object_id": "u05", "email": "moved@x"})
        other.upsert(Member, {"object_id": "u30", "email": "new@x"})
        other.delete((Member, "u06"))
        reads.clear()
        with pytest.raises(ValueError, match="Unique constraint"):
            ws.upsert(Member, {"object_id": "u21", "email": "moved@x"})
        with pytest.raises(ValueError, match="Unique constraint"):
            ws.upsert(Member, {"object_id": "u21", "email": "new@x"})
        ws.upsert_many(Member, [{"object_id": "u21", "email": "5@x"}, {"object_id": "u22", "email": "6@x"}])
        # The changed and added files, and the file ws itself wrote last; never the 18 unchanged ones.
        assert set(reads) <= {f"u05{suffix}", f"u30{suffix}", f"u20{suffix}", f"u21{suffix}", f"u22{suffix}"}
        assert len(ws.query(Member).execute().ids) == 23
    finally:
        ws.drop()


def test_unique_owner_must_be_explicit_when_multiple_backends_can_enforce():
    """Replicated fields should not acquire an arbitrary uniqueness owner."""

    class Replicated(hb.Entity):
        identifier = "index-replicated"

        email = hb.field(hb.ShortText).store(to="left").store(to="right").unique()

    ws = hb.HeavenBase("index-replicated-ws", detached=True, backends={"left": {"type": "inmem"}, "right": {"type": "inmem"}})
    with pytest.raises(ValueError, match="multiple possible owners"):
        ws.register(Replicated)
    assert Replicated.identifier not in ws.entities
    assert Replicated.identifier not in ws.storage_plans


def test_replicated_entity_unique_constraints_share_one_enforcement_backend():
    """One entity should not require a distributed transaction to validate one row."""

    class SplitOwner(hb.Entity):
        identifier = "index-split-owner"

        email = hb.field(hb.ShortText).store(to="left").store(to="right").unique(to="left")
        handle = hb.field(hb.ShortText).store(to="left").store(to="right").unique(to="right")

    ws = hb.HeavenBase("index-split-owner-ws", detached=True, backends={"left": {"type": "inmem"}, "right": {"type": "inmem"}})
    with pytest.raises(ValueError, match="must share one enforcement backend"):
        ws.register(SplitOwner)
    assert SplitOwner.identifier not in ws.entities
    assert SplitOwner.identifier not in ws.storage_plans


def test_mixed_entity_batches_write_each_unique_owner_before_its_replicas():
    """Backend batching should not let one entity's replica outrun another entity's owner."""
    events = []

    class SpyBackend(hb.backends.InMemBackend):
        def upsert(self, rows):
            events.append((self.name, [row.entity_id for row in rows]))
            return super().upsert(rows)

    class LeftOwned(hb.Entity):
        identifier = "index-left-owned"

        email = hb.field(hb.ShortText).store(to="left").store(to="right").unique(to="left")

    class RightOwned(hb.Entity):
        identifier = "index-right-owned"

        email = hb.field(hb.ShortText).store(to="left").store(to="right").unique(to="right")

    ws = hb.HeavenBase("index-owner-order-ws", detached=True, backends={"left": SpyBackend("left"), "right": SpyBackend("right")})
    ws.register(LeftOwned)
    ws.register(RightOwned)
    events.clear()

    left = RowOp(LeftOwned.identifier, "l1", {"object_id": "l1", "email": "left@example.com"})
    right = RowOp(RightOwned.identifier, "r1", {"object_id": "r1", "email": "right@example.com"})
    ws._writer.write_ops({"left": [left, right], "right": [left, right]})

    assert events == [
        ("left", [LeftOwned.identifier]),
        ("right", [RightOwned.identifier]),
        ("left", [RightOwned.identifier]),
        ("right", [LeftOwned.identifier]),
    ]


def test_mssql_unique_null_policy_is_provider_owned():
    """The dialect spec should own SQL Server's filtered unique-index semantics."""
    from sqlalchemy import Column, MetaData, String, Table

    column = Table("accounts", MetaData(), Column("email", String())).c.email
    spec = hb.ext.sql_dialect_spec("mssql")
    options = spec.unique_index_options((column,))

    assert spec.unique_nulls_distinct is False
    assert list(options) == ["mssql_where"]
    assert "email IS NOT NULL" in str(options["mssql_where"])


def test_index_json_and_manifest_roundtrip_use_booleans_not_modes():
    """JSON boundaries should preserve intent without provider-centric mode strings."""
    entity = hb.Entity.from_schema(
        {
            "entity_id": "index-json",
            "fields": {
                "name": {"type": "short-text", "index": True},
                "note": {"type": "short-text", "index": False},
                "email": {"type": "short-text", "unique": True},
            },
            "indexes": [{"fields": ["name", "email"]}],
        }
    )
    ws = hb.HeavenBase("index-json-ws", detached=True, backends={"main": {"type": "sqlite", "database": ":memory:"}})
    ws.register(entity)
    manifest = ws.to_manifest()
    fields = manifest["entities"][0]["fields"]

    assert fields["name"]["index"] is True
    assert fields["note"]["index"] is False
    assert fields["email"]["unique"] is True
    assert manifest["entities"][0]["indexes"] == [{"fields": ["name", "email"]}]

    clone = hb.HeavenBase.from_manifest(hb.WorkspaceManifest.from_dict(manifest), detached=True)
    plan = clone.storage_plans[entity.identifier]
    assert plan.has_index("name", backend="main")
    assert not plan.has_index("note", backend="main")
    assert [(binding.fields, binding.backend) for binding in plan.constraints] == [(("email",), "main")]


def test_string_index_modes_and_unindexable_declarations_fail_early():
    """Legacy mode strings and compound logical values should not silently degrade."""
    with pytest.raises(TypeError, match="mode strings"):
        hb.Entity.from_schema({"entity_id": "index-mode-string", "fields": {"name": {"type": "short-text", "index": "unique"}}})

    class JsonIndex(hb.Entity):
        identifier = "index-json-value"

        payload = hb.field(hb.Json).index()

    ws = hb.HeavenBase("index-json-value-ws", detached=True, backends={"main": {"type": "sqlite", "database": ":memory:"}})
    with pytest.raises(NotImplementedError, match="cannot provide a scalar index"):
        ws.register(JsonIndex)
    assert JsonIndex.identifier not in ws.entities

    class UnsafePortableSqlTextIndex(hb.Entity):
        identifier = "index-long-text-sql"

        body = hb.field(hb.LongText).index()

    with pytest.raises(NotImplementedError, match="long-text"):
        ws.register(UnsafePortableSqlTextIndex)


def test_unique_has_one_logical_owner_and_provider_indexes_are_not_user_toggles():
    """Duplicate uniqueness and provider-required search structures should fail clearly."""
    with pytest.raises(ValueError, match="one enforcement owner"):

        class DuplicateUnique(hb.Entity):
            identifier = "index-duplicate-unique"
            __constraints__ = [hb.unique("email")]

            email = hb.field(hb.ShortText).unique()

    class SearchManaged(hb.Entity):
        identifier = "index-search-managed"

        title = hb.field(hb.ShortText).store(to="search").index(enabled=False, to="search")

    modules = hb.DEFAULT_CONTEXT.modules()
    inmem_tags = modules.inspect("backend_type", "inmem")[0]["record"]["meta"]["tags"]
    _NoScalarIndexInMemBackend.register(
        tags={**inmem_tags, "scalar-index": ScalarIndexSpec().to_dict()},
        resolver=modules,
    )
    backend = _NoScalarIndexInMemBackend("search")
    ws = hb.HeavenBase("index-search-managed-ws", detached=True, backends={"search": backend})
    try:
        with pytest.raises(NotImplementedError, match="provider-required search indexes"):
            ws.register(SearchManaged)
    finally:
        ws.drop()
        modules.unregister("backend_type", _NoScalarIndexInMemBackend.identifier)


def test_unique_preflight_cannot_be_bypassed_by_schema_replacement():
    """Replacement registration should preserve the old schema until stored duplicates are fixed."""

    class ContactV1(hb.Entity):
        identifier = "index-contact-replace"

        email = hb.field(hb.ShortText)

    ws = hb.HeavenBase("index-contact-replace-ws", detached=True, backends={"main": {"type": "sqlite", "database": ":memory:"}})
    ws.register(ContactV1)
    ws.upsert_many(ContactV1, [{"object_id": "c1", "email": "same@x"}, {"object_id": "c2", "email": "same@x"}])

    class ContactV2(hb.Entity):
        identifier = "index-contact-replace"

        email = hb.field(hb.ShortText).unique()

    with pytest.raises(ValueError, match="Cannot enforce unique constraint"):
        ws.register(ContactV2, allow_replace=True)
    assert ws.entities[ContactV1.identifier] is ContactV1

    ws.delete((ContactV1, "c2"))
    assert ws.register(ContactV2, allow_replace=True) is ContactV2


def test_registration_rolls_back_logical_state_when_metadata_publish_fails(monkeypatch):
    """A retryable metadata failure should not leave a half-registered logical entity."""

    class Retryable(hb.Entity):
        identifier = "index-register-retry"

        slug = hb.field(hb.ShortText).unique()

    ws = hb.HeavenBase("index-register-retry-ws", detached=True, backends={"main": {"type": "sqlite", "database": ":memory:"}})
    publish = ws._metaschema.publish_schema_rows

    def fail_once(entity):
        if entity.identifier == Retryable.identifier:
            raise RuntimeError("metadata unavailable")
        return publish(entity)

    monkeypatch.setattr(ws._metaschema, "publish_schema_rows", fail_once)
    with pytest.raises(RuntimeError, match="metadata unavailable"):
        ws.register(Retryable)
    assert Retryable.identifier not in ws.entities
    assert Retryable.identifier not in ws.storage_plans

    monkeypatch.setattr(ws._metaschema, "publish_schema_rows", publish)
    assert ws.register(Retryable) is Retryable


def test_redis_and_elasticsearch_opt_out_force_scan_without_provider_io():
    """Resolved scalar opt-out should control handler mode and Elasticsearch mapping policy."""

    class RedisOptOut(hb.Entity):
        identifier = "index-redis-opt-out"

        category = hb.field(hb.ShortText).store(to="cache").index(enabled=False, to="cache")

    redis = hb.backends.RedisBackend("cache", url="redis://127.0.0.1:1")
    redis_ws = hb.HeavenBase("index-redis-opt-out-ws", detached=True, backends={"main": hb.backends.InMemBackend("main"), "cache": redis})
    redis_ws.register(RedisOptOut)
    redis_step = redis_ws.query(RedisOptOut).where(RedisOptOut.category == "a").explain()["steps"][0]
    assert redis_step["handler_mode"] == "py_scan"
    assert not redis_ws.storage_plans[RedisOptOut.identifier].has_index("category", backend="cache")

    class ElasticsearchOptOut(hb.Entity):
        identifier = "index-elasticsearch-opt-out"

        body = hb.field(hb.LongText).store(to="search").index(enabled=False, to="search")

    search = hb.backends.ElasticsearchBackend("search", hosts=["http://127.0.0.1:1"])
    search.client = None
    search_ws = hb.HeavenBase("index-elasticsearch-opt-out-ws", detached=True, backends={"main": hb.backends.InMemBackend("main"), "search": search})
    search_ws.register(ElasticsearchOptOut)
    search_step = search_ws.query(ElasticsearchOptOut).where(ElasticsearchOptOut.body.match("term")).explain()["steps"][0]
    mapping = search._mapping(ElasticsearchOptOut.schema(), indexed_fields=set())["mappings"]["properties"]["body"]

    assert search_step["handler_mode"] == "py_scan"
    assert mapping["index"] is False
    assert not search_ws.storage_plans[ElasticsearchOptOut.identifier].has_index("body", backend="search")


_ARRAY_ITEM_CASES = [
    ("short-text", hb.ShortText, ["red", "blue"], ["green"], ["red"], "red"),
    ("identifier", hb.Identifier, ["i2", "i3"], ["i1"], ["i2"], "i2"),
    ("medium-text", hb.MediumText, ["red", "blue"], ["green"], ["red"], "red"),
    ("long-text", hb.LongText, ["red", "blue"], ["green"], ["red"], "red"),
    ("date", hb.Date, ["2026-01-01", "2026-01-02"], ["2026-01-03"], ["2026-01-01"], "2026-01-01"),
    ("categorical", hb.Categorical(["red", "green", "blue"]), ["red", "blue"], ["green"], ["red"], "red"),
    ("categorical-integer", hb.Categorical([1, 2, 3], base="integer"), [1, 3], [2], [1], 1),
    ("integer", hb.Integer, [1, 3], [2], [1], 1),
    ("float", hb.Float, [1.5, 3.5], [2.5], [1.5], 1.5),
    ("boolean", hb.Boolean, [True], [False], [True, False], True),
]


@pytest.mark.parametrize("backend_type", ["inmem", "file"])
@pytest.mark.parametrize(("label", "item_type", "a_items", "b_items", "c_items", "probe"), _ARRAY_ITEM_CASES, ids=[case[0] for case in _ARRAY_ITEM_CASES])
def test_array_contains_returns_member_rows_on_row_stores(tmp_path, backend_type, label, item_type, a_items, b_items, c_items, probe):
    """A filter-only ``$contains`` on an array must return the rows ``count()`` and scans see.

    The in-memory value index holds whole scalar values of declared indexes only,
    so a native ``array_contains`` lookup there would find no candidates.
    """

    class ArrayRow(hb.Entity):
        identifier = f"array-contains-{label}"

        title = hb.field(hb.ShortText)
        items = hb.field(hb.Array(item_type))

    config = {"type": "inmem"} if backend_type == "inmem" else {"type": "file", "path": hb.utils.path_to_file_uri(tmp_path / "rows")}
    ws = hb.HeavenBase(f"array-contains-{label}-{backend_type}-ws", detached=True, backends={"main": config})
    try:
        ws.register(ArrayRow)
        ws.upsert_many(
            ArrayRow,
            [
                {"object_id": "a", "title": "x", "items": a_items},
                {"object_id": "b", "title": "y", "items": b_items},
                {"object_id": "c", "title": "z", "items": c_items},
            ],
        )
        expected = ["a", "c"]

        def ids(where):
            return sorted(ws.query(ArrayRow).where(where).execute().ids)

        assert ids({"items": {"$contains": probe}}) == expected
        assert ids(ArrayRow.items.array_contains(probe)) == expected
        assert sorted(ws.query_json(ArrayRow, {"filter": {"items": {"$array_contains": probe}}}).execute().ids) == expected
        assert ids({"$and": [{"title": {"$in": ["x", "z"]}}, {"items": {"$contains": probe}}]}) == expected
        assert ws.query(ArrayRow).where({"items": {"$contains": probe}}).count().execute().scalar() == len(expected)
    finally:
        ws.drop()


def test_array_contains_uses_a_native_item_index_only_where_one_exists():
    """Redis keeps one SET member per array item; the in-memory value map does not."""

    class TaggedRow(hb.Entity):
        identifier = "array-contains-index-route"

        tags = hb.field(hb.Array[hb.ShortText]).store(to="cache")

    redis = hb.backends.RedisBackend("cache", url="redis://127.0.0.1:1")
    ws = hb.HeavenBase("array-contains-index-route-ws", detached=True, backends={"main": hb.backends.InMemBackend("main"), "cache": redis})
    ws.register(TaggedRow)
    redis_step = ws.query(TaggedRow).where(TaggedRow.tags.array_contains("red")).explain()["steps"][0]
    assert redis_step["handler_mode"] == "native"

    class InMemTaggedRow(hb.Entity):
        identifier = "array-contains-index-route-inmem"

        tags = hb.field(hb.Array[hb.ShortText])

    inmem_ws = hb.HeavenBase("array-contains-index-route-inmem-ws", detached=True, backends={"main": {"type": "inmem"}})
    try:
        inmem_ws.register(InMemTaggedRow)
        inmem_step = inmem_ws.query(InMemTaggedRow).where(InMemTaggedRow.tags.array_contains("red")).explain()["steps"][0]
        assert inmem_step["handler_mode"] == "py_scan"
    finally:
        inmem_ws.drop()
