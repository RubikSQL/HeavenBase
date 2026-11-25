import inspect
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, "src")

import heavenbase as hb
from heavenbase.modules import ModuleService
from heavenbase.utils.registry import Registry, named_key
from heavenbase.workspace import WorkspaceSpec
from heavenbase.workspace.setup.services import workspace_service_roles

LIVE_WORKSPACES = hb.DEFAULT_CONTEXT._live_workspaces
WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry


def _detached_without_pending(identifier, *, backends=None):
    """Construct an internal caller-owned workspace with explicit schema setup."""
    return hb.HeavenBase._construct(
        identifier,
        context=hb.DEFAULT_CONTEXT,
        backends=backends,
        detached=True,
        tracked=False,
        register_workspace=False,
        register_pending=False,
        track_live=False,
        publish=False,
    )


@pytest.fixture(autouse=True)
def _isolate_workspaces():
    before = set(WORKSPACES.keys())
    yield
    for wid in set(WORKSPACES.keys()) - before:
        WORKSPACES.pop(wid, None)


@pytest.mark.parametrize("workspace_id", ["", "../bad", "bad/path", "bad.name", "Bad", "bad space", "_bad"])
def test_workspace_ids_reject_unsafe_names(workspace_id):
    """Workspace ids enter backend paths and provider namespaces, so reject unsafe names early."""
    with pytest.raises(ValueError, match="workspace_id"):
        hb.HeavenBase(workspace_id)

    with pytest.raises(ValueError, match="workspace_id"):
        hb.HeavenBase.load(workspace_id)

    class UnsafeWorkspaceThing(hb.Entity):
        name = hb.field(hb.ShortText)

    with pytest.raises(ValueError, match="workspace_id"):
        UnsafeWorkspaceThing.register(workspace_id)


def test_heavenbase_constructor_exposes_only_construction_inputs():
    """Detached ownership should remain an explicit construction input."""
    assert list(inspect.signature(hb.HeavenBase).parameters) == ["id", "context", "backends", "preset", "detached"]


def test_workspace_service_roles_are_unique_and_attached():
    """Workspace assembly should attach exactly one service for every documented role."""
    workspace = hb.HeavenBase("service-role-test", detached=True, backends={"main": {"type": "inmem"}})
    roles = workspace_service_roles()
    slots = [slot for slot, _ in roles]

    assert len(slots) == len(set(slots))
    assert all(objective.strip() for _, objective in roles)
    assert all(hasattr(workspace, slot) for slot in slots)


def test_registry_creates_aliases_replacements_and_snapshots():
    """The shared Registry owns aliases, snapshots, and replacement policy."""

    class DemoSpec:
        def __init__(self, identifier: str, value: int):
            self.identifier = identifier
            self.value = value

    registry = Registry("demo-specs")
    alpha = DemoSpec("alpha", 1)
    beta = DemoSpec("beta", 2)
    updated_beta = DemoSpec("beta", 3)

    created_alpha = registry.create("alpha", alpha)
    created_beta = registry.create("beta", beta, aliases=("b",))
    assert created_alpha is not alpha
    assert created_beta is not beta
    assert (created_alpha.identifier, created_alpha.value) == ("alpha", 1)
    assert (created_beta.identifier, created_beta.value) == ("beta", 2)
    assert registry["alpha"] is not created_alpha
    assert registry["b"] is not created_beta
    assert {key: (value.identifier, value.value) for key, value in registry.snapshot().items()} == {
        "alpha": ("alpha", 1),
        "beta": ("beta", 2),
    }

    with pytest.raises(ValueError, match="already registered"):
        registry.create("alpha", DemoSpec("alpha", 9))
    with pytest.raises(ValueError, match="alias 'b' is already registered"):
        registry.create("gamma", DemoSpec("gamma", 9), aliases=("b",))

    registry.update("beta", updated_beta, aliases=("bee",))
    assert registry["bee"] is not updated_beta
    assert registry["bee"].value == 3
    assert registry.get("b") is None
    assert registry.snapshot()["beta"].value == 3


def test_registry_derives_atomic_updates_under_the_writer_lock():
    """The shared Registry derives updates from one captured current value."""
    registry = Registry("demo-specs")

    def add_second(current):
        return {"values": [*current["values"], 2]}

    assert registry.create("alpha", {"values": [1]}) == {"values": [1]}
    assert registry.update("alpha", derive=add_second, aliases=("a",)) == {"values": [1, 2]}
    assert registry["a"] == {"values": [1, 2]}
    registry.create("beta", {"values": []}, aliases=("b",))

    with pytest.raises(ValueError, match="alias 'b' is already registered"):
        registry.update("alpha", derive=add_second, aliases=("b",))


def test_registry_delete_removes_item_and_aliases():
    """The shared Registry removes an item and all resolving aliases."""
    registry = Registry("demo-specs")

    registry.create("alpha", {"value": 1}, aliases=("a",))

    assert registry.delete("a") == {"value": 1}
    assert registry.get("alpha") is None
    assert registry.get("a") is None
    assert registry.delete("missing", missing_ok=True) is None

    with pytest.raises(KeyError, match="missing"):
        registry.delete("missing")


def test_registry_accepts_an_explicit_free_form_key_normalizer():
    """Named registries centralize non-empty free-form key policy."""
    registry = Registry("demo-names", normalize=lambda value: named_key(value, "demo name"))

    assert registry.resolve("  array[short-text]  ") == "array[short-text]"
    registry.create("array[short-text]", {"value": 1}, aliases=("short-text[]",))

    assert registry["short-text[]"] == {"value": 1}
    with pytest.raises(ValueError, match="demo name must be non-empty"):
        registry.create(" ", {"value": 2})


def test_registry_supports_normalized_tuple_keys():
    """The same Registry owns exact normalized composite keys."""
    registry = Registry("demo-tuples", normalize=lambda key: tuple(str(part).lower() for part in key))

    assert registry.create(("A", "B"), 1) == 1
    assert registry.get(("a", "b")) == 1
    assert registry.snapshot() == {("a", "b"): 1}

    with pytest.raises(ValueError, match="already registered"):
        registry.create(("a", "b"), 2)

    assert registry.update(("A", "B"), 3) == 3
    assert registry[("a", "b")] == 3
    assert registry.list() == (("a", "b"),)


def embed_json_note(text):
    lowered = (text or "").lower()
    return [float(lowered.count("schema")), float(lowered.count("vector"))]


def identity(value):
    return value


def json_note_def(entity_id="json-note", name="JsonNote"):
    return {
        "entity_id": entity_id,
        "name": name,
        "desc": "JSON-authored note entity.",
        "tags": ["docs", "json"],
        "fields": {
            "object_id": {"type": "identifier", "pk": True, "desc": "Stable note id"},
            "title": {"type": "short-text", "desc": "Display title", "tags": ["display"], "examples": ["Catalog quickstart"]},
            "body": {"type": "long-text", "desc": "Searchable body text", "required": True, "tags": ["search"]},
            "status": {"type": "short-text", "desc": "Workflow status", "default": "draft"},
            "emb": {
                "type": {"type": "vector", "dim": 2},
                "desc": "Tiny in-memory embedding",
                "compute": {"fn": "embed_json_note", "inputs": ["body"]},
                "storage": {"to": "main", "strategy": "vector"},
            },
        },
        "derive": [
            {"target": "catalog.name", "compute": "identity", "inputs": ["title"]},
            {"target": "catalog.desc", "compute": "identity", "inputs": ["body"]},
        ],
    }


def test_crud_requires_registered_entity_class_and_publishes_schema_rows():
    """Row upserts should never create an Entity class as a side effect."""
    ws = hb.HeavenBase("test-explicit-entity-class")

    class TestProduct(hb.Entity):
        name = hb.field(hb.ShortText, desc="Name")
        price = hb.field(hb.Float, desc="Price")

    instance = TestProduct.from_dict({"object_id": "desk", "name": "Desk", "price": 10})
    before_entities = set(ws.entities)
    before_storage_plans = set(ws.storage_plans)
    with pytest.raises(KeyError, match="has no registered Entity class 'test-product'"):
        ws.upsert(instance)

    assert set(ws.entities) == before_entities
    assert set(ws.storage_plans) == before_storage_plans

    ws.register(TestProduct)
    entity_id = ws.upsert(instance)
    assert ws.get(entity_id)["name"] == "Desk"
    assert "test-product" in ws.entities
    fields = ws.query(hb.MetaSchema).where(hb.MetaSchema.kind == "field").where(hb.MetaSchema.subject_id == "test-product").execute().rows()
    assert len(fields) >= 3


def test_text_type_default_lengths():
    """Text logical types should expose and serialize the standard length ladder."""
    assert hb.Identifier().max_chars == 63
    assert hb.ShortText().max_chars == 255
    assert hb.MediumText().max_chars == 4095
    assert hb.LongText().max_chars == 65535
    assert hb.Identifier().to_dict() == {"type": "identifier", "max_chars": 63}
    assert hb.ShortText().to_dict() == {"type": "short-text", "max_chars": 255}
    assert hb.MediumText().to_dict() == {"type": "medium-text", "max_chars": 4095}
    assert hb.LongText().to_dict() == {"type": "long-text", "max_chars": 65535}
    assert hb.MediumText().validate("a" * 4095) == "a" * 4095
    with pytest.raises(ValueError, match="MediumText exceeds 4095"):
        hb.MediumText().validate("a" * 4096)


def test_catalog_name_auto_id_is_workspace_isolated():
    """Catalog-name identity should hash derived catalog.name when object_id is omitted."""
    ws_a = hb.HeavenBase("test-auto-id-a")
    ws_b = hb.HeavenBase("test-auto-id-b")

    class AutoIdThing(hb.Entity):
        __derive__ = [hb.Entity.derive("catalog.name").compute(lambda title: title, inputs=["title"])]

        title = hb.field(hb.ShortText)
        status = hb.field(hb.ShortText).default("draft")

    ws_a.register(AutoIdThing)
    ws_b.register(AutoIdThing)
    entity_id = ws_a.upsert(AutoIdThing, {"title": "Alpha"})
    same_id = ws_b.upsert(AutoIdThing, {"title": "Alpha", "status": "remote"})

    assert entity_id == hb.utils.hash_id("auto-id-thing", "Alpha")
    assert len(entity_id) == 42
    assert entity_id == same_id
    assert type(AutoIdThing.schema().get("object_id").dtype).identifier == "identifier"
    assert AutoIdThing.schema().get("object_id").pk is True
    assert "name" not in AutoIdThing.schema().fields
    assert ws_a.get(entity_id)["status"] == "draft"
    assert ws_b.get(same_id)["status"] == "remote"
    catalog_row = ws_a.query(hb.Catalog).where(hb.Catalog.target_entity == "auto-id-thing").execute().rows()[0]
    assert catalog_row["name"] == "Alpha"


@pytest.mark.detail
def test_catalog_name_auto_id_rejects_blank_name_sources():
    """Blank-looking catalog.name values should not silently become object_id hashes."""
    ws = hb.HeavenBase("test-auto-id-blank-name")

    class BlankNameThing(hb.Entity):
        name = hb.field(hb.ShortText)

    class BlankDerivedNameThing(hb.Entity):
        __derive__ = [hb.Entity.derive("catalog.name").compute(lambda title: title, inputs=["title"])]

        title = hb.field(hb.ShortText)

    ws.register(BlankNameThing)
    ws.register(BlankDerivedNameThing)
    with pytest.raises(ValueError, match="catalog.name"):
        ws.upsert(BlankNameThing, {"name": "   "})
    with pytest.raises(ValueError, match="catalog.name"):
        ws.upsert(BlankDerivedNameThing, {"title": "\t"})


@pytest.mark.detail
def test_same_name_catalog_projection_drives_auto_id_and_discovery_defaults():
    """Same-named native fields should project into Catalog without explicit derive declarations."""
    ws = hb.HeavenBase("test-default-catalog-projection")

    class DefaultCatalogThing(hb.Entity):
        name = hb.field(hb.ShortText)
        desc = hb.field(hb.MediumText)
        summary = hb.field(hb.LongText)
        tags = hb.field(hb.Json).default([])
        owner = hb.field(hb.ShortText).default("")
        creator = hb.field(hb.ShortText).default("")

    ws.register(DefaultCatalogThing)
    object_id = ws.upsert(
        DefaultCatalogThing,
        {
            "name": "Alpha",
            "desc": "Short searchable blurb",
            "summary": "Long payload description",
            "tags": ["docs", "catalog"],
            "owner": "platform",
            "creator": "codex",
        },
    )
    catalog_row = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "default-catalog-thing").execute().rows()[0]

    assert object_id == hb.utils.hash_id("default-catalog-thing", "Alpha")
    assert catalog_row["target_id"] == object_id
    assert catalog_row["name"] == "Alpha"
    assert catalog_row["desc"] == "Short searchable blurb"
    assert catalog_row["summary"] == "Long payload description"
    assert catalog_row["tags"] == ["docs", "catalog"]
    assert catalog_row["owner"] == "platform"
    assert catalog_row["creator"] == "codex"
    assert catalog_row["created_at"] > 0
    assert catalog_row["updated_at"] >= catalog_row["created_at"]


def test_same_name_catalog_projection_ignores_incompatible_native_types():
    """A same-named application field must not overwrite a different Catalog logical type."""
    ws = hb.HeavenBase("test-catalog-incompatible-same-name")

    class TextConfidence(hb.Entity):
        confidence = hb.field(hb.ShortText).required()

    class NumericConfidence(hb.Entity):
        confidence = hb.field(hb.Float).required()

    ws.register(TextConfidence)
    ws.register(NumericConfidence)
    ws.upsert_many(
        TextConfidence,
        [
            {"object_id": "first", "confidence": "high"},
            {"object_id": "second", "confidence": "medium"},
        ],
    )
    ws.upsert(NumericConfidence, {"object_id": "numeric", "confidence": 0.75})

    assert ws.get("first", entity=TextConfidence)["confidence"] == "high"
    text_catalog = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "text-confidence").execute().rows()
    numeric_catalog = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "numeric-confidence").execute().rows()
    assert [row["confidence"] for row in text_catalog] == [1.0, 1.0]
    assert numeric_catalog[0]["confidence"] == 0.75


def test_same_name_catalog_projection_requires_matching_type_parameters():
    """Same type identifiers with different constraints must not project implicitly."""
    ws = hb.HeavenBase("test-catalog-incompatible-type-parameters")

    class NarrowName(hb.Entity):
        name = hb.field(hb.ShortText(max_chars=32)).required()

    ws.register(NarrowName)
    ws.upsert(NarrowName, {"object_id": "narrow", "name": "Application name"})

    row = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "narrow-name").execute().rows()[0]
    assert row["name"] == ""


@pytest.mark.detail
def test_explicit_catalog_derive_overrides_same_name_projection():
    """Explicit Catalog derives should win over same-named native fields."""
    ws = hb.HeavenBase("test-explicit-catalog-projection")

    class ExplicitCatalogThing(hb.Entity):
        __derive__ = [hb.Entity.derive("catalog.name").compute(lambda title: f"derived:{title}", inputs=["title"])]

        name = hb.field(hb.ShortText)
        title = hb.field(hb.ShortText)

    ws.register(ExplicitCatalogThing)
    ws.upsert(ExplicitCatalogThing, {"object_id": "e1", "name": "Native", "title": "Title"})
    catalog_row = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "explicit-catalog-thing").execute().rows()[0]

    assert catalog_row["name"] == "derived:Title"


def test_catalog_active_visible_preserved_and_derived():
    """Catalog active/visible should survive entity upserts unless entity projection overrides them."""
    from heavenbase.workspace.catalog import catalog_materialize_timing

    open_deadline = hb.Timestamp.default_max
    ws = hb.HeavenBase("test-catalog-active-visible")
    backend = ws.backends.get("main")
    catalog_entity_id = hb.Catalog.schema().entity_id

    class PlainThing(hb.Entity):
        identifier = "plain-thing"
        name = hb.field(hb.ShortText)

    ws.register(PlainThing)
    ws.upsert(PlainThing, {"object_id": "p1", "name": "Alpha"})
    catalog_row = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "plain-thing").execute().rows()[0]
    assert catalog_row["expires_compute"] == open_deadline
    assert catalog_row["stale_compute"] == open_deadline
    backend.upsert([hb.ext.RowOp(catalog_entity_id, catalog_row["object_id"], {**catalog_row, "active": False, "visible": False})])
    ws.upsert(PlainThing, {"object_id": "p1", "name": "Alpha updated"})
    preserved = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "plain-thing").execute().rows()[0]
    assert preserved["active"] is False
    assert preserved["visible"] is False

    class ToggleThing(hb.Entity):
        identifier = "toggle-thing"
        __derive__ = [hb.Entity.derive("catalog.active").compute(lambda enabled: enabled, inputs=["enabled"])]

        name = hb.field(hb.ShortText)
        enabled = hb.field(hb.Boolean).default(True)

    ws.register(ToggleThing)
    ws.upsert(ToggleThing, {"object_id": "t1", "name": "Beta", "enabled": False})
    toggled = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "toggle-thing").execute().rows()[0]
    assert toggled["active"] is False

    assert catalog_materialize_timing({}) == {"expires_compute": open_deadline, "stale_compute": open_deadline}
    assert catalog_materialize_timing({"expires_at": 100, "created_at": 10}) == {"expires_compute": 100, "stale_compute": open_deadline}
    assert catalog_materialize_timing({"expires_after": 50, "created_at": 10}) == {"expires_compute": 60, "stale_compute": open_deadline}
    assert catalog_materialize_timing({"expires_at": 100, "expires_after": 50, "created_at": 10}) == {"expires_compute": 60, "stale_compute": open_deadline}
    assert catalog_materialize_timing({"stale_after": 5, "updated_at": 20}) == {"expires_compute": open_deadline, "stale_compute": 25}
    assert catalog_materialize_timing({"expires_at": datetime(2026, 1, 1, tzinfo=timezone.utc), "created_at": 10}) == {
        "expires_compute": hb.Timestamp().validate(datetime(2026, 1, 1, tzinfo=timezone.utc)),
        "stale_compute": open_deadline,
    }


@pytest.mark.detail
def test_catalog_timing_policy_projection_and_recompute_on_upsert():
    """Policy fields should project to Catalog and recompute expires_compute/stale_compute on write."""
    open_deadline = hb.Timestamp.default_max
    ws = hb.HeavenBase("test-catalog-timing-projection")

    class TimedNote(hb.Entity):
        identifier = "timed-note"
        __derive__ = [
            hb.Entity.derive("catalog.expires_at").compute(lambda deadline: deadline, inputs=["deadline"]),
            hb.Entity.derive("catalog.expires_after").compute(lambda ttl: ttl, inputs=["ttl_ms"]),
            hb.Entity.derive("catalog.stale_after").compute(lambda ttl: ttl, inputs=["fresh_ms"]),
        ]

        name = hb.field(hb.ShortText)
        deadline = hb.field(hb.Timestamp).default(open_deadline)
        ttl_ms = hb.field(hb.Duration).default(0)
        fresh_ms = hb.field(hb.Duration).default(0)

    ws.register(TimedNote)
    ws.upsert(
        TimedNote,
        {
            "object_id": "n1",
            "name": "Policy note",
            "deadline": 1_000,
            "ttl_ms": 0,
            "fresh_ms": 100,
        },
    )
    first = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "timed-note").execute().rows()[0]
    created_at = first["created_at"]
    updated_at = first["updated_at"]
    assert first["expires_at"] == 1_000
    assert first["expires_after"] == 0
    assert first["stale_after"] == 100
    assert first["expires_compute"] == 1_000
    assert first["stale_compute"] == updated_at + 100

    ws.upsert(
        TimedNote,
        {
            "object_id": "n1",
            "name": "Policy note refreshed",
            "deadline": open_deadline,
            "ttl_ms": 500,
            "fresh_ms": 100,
        },
    )
    second = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "timed-note").execute().rows()[0]
    assert second["created_at"] == created_at
    assert second["updated_at"] >= updated_at
    assert second["expires_at"] == open_deadline
    assert second["expires_after"] == 500
    assert second["expires_compute"] == created_at + 500
    assert second["stale_compute"] == second["updated_at"] + 100
    assert second["expires_compute"] != open_deadline


def test_catalog_available_excludes_inactive_hidden_expired_and_stale_rows():
    """Every available query surface should hide non-discoverable Catalog rows."""
    from heavenbase.query.catalog import CatalogAvailable

    ws = hb.HeavenBase("test-catalog-available", backends={"main": {"type": "inmem"}})
    open_deadline = hb.Timestamp.default_max
    now = 1_000

    class DiscoveryThing(hb.Entity):
        identifier = "discovery-thing"
        name = hb.field(hb.ShortText)

    ws.register(DiscoveryThing)
    ws.upsert(DiscoveryThing, {"object_id": "ok", "name": "Visible"})
    ws.upsert(DiscoveryThing, {"object_id": "inactive", "name": "Inactive"})
    ws.upsert(DiscoveryThing, {"object_id": "hidden", "name": "Hidden"})
    ws.upsert(DiscoveryThing, {"object_id": "expired", "name": "Expired"})
    ws.upsert(DiscoveryThing, {"object_id": "stale", "name": "Stale"})

    catalog_entity_id = hb.Catalog.schema().entity_id
    backend = ws.backends.get("main")
    patches = {
        "inactive": {"active": False},
        "hidden": {"visible": False},
        "expired": {"expires_compute": now - 1},
        "stale": {"stale_compute": now - 1},
    }
    for target_id, patch in patches.items():
        row = ws.query(hb.Catalog).where(hb.Catalog.target_id == target_id).execute().rows()[0]
        backend.upsert([hb.ext.RowOp(catalog_entity_id, row["object_id"], {**row, **patch})])

    def visible_ids_for(builder):
        return {row["target_id"] for row in builder.select("target_id").execute().rows()}

    base = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "discovery-thing")
    assert visible_ids_for(base.available(now=now)) == {"ok"}
    assert visible_ids_for(
        ws.query_json(
            hb.Catalog,
            {"available": {"now": now}, "filter": {"target_entity": "discovery-thing"}},
        )
    ) == {"ok"}
    assert visible_ids_for(ws.query(hb.Catalog).where({"target_entity": "discovery-thing", "$available": {"now": now}})) == {"ok"}

    stale_visible = visible_ids_for(ws.query(hb.Catalog).where(hb.Catalog.target_entity == "discovery-thing").available(stale=True, now=now))
    assert stale_visible == {"ok", "stale"}
    assert "expired" not in stale_visible

    hidden_visible = visible_ids_for(ws.query(hb.Catalog).where(hb.Catalog.target_entity == "discovery-thing").available(invisible=True, now=now))
    assert hidden_visible == {"ok", "hidden"}
    assert "expired" not in hidden_visible

    predicates = CatalogAvailable(now=now).to_dict()["$and"]
    assert predicates[1]["visible"] is True
    assert predicates[2]["expires_compute"]["$gt"] == now
    assert any("stale_compute" in item for item in predicates)
    assert not any("visible" in item for item in CatalogAvailable(now=now, invisible=True).to_dict()["$and"])
    assert not any("stale_compute" in item for item in CatalogAvailable(now=now, stale=True).to_dict()["$and"])
    assert open_deadline > now


@pytest.mark.detail
def test_catalog_available_rejects_non_catalog_entity():
    """available() should apply only to sys-catalog queries."""
    ws = hb.HeavenBase("test-catalog-available-guard", backends={"main": {"type": "inmem"}})

    class PlainThing(hb.Entity):
        identifier = "plain-available-guard"
        name = hb.field(hb.ShortText)

    ws.register(PlainThing)
    with pytest.raises(ValueError, match="available applies only to Catalog"):
        ws.query(PlainThing).available()
    with pytest.raises(ValueError, match="available applies only to Catalog"):
        ws.query_json(PlainThing, {"available": True})


def test_workspace_search_finds_catalog_and_metaschema_cards():
    """search() should discover object cards and structure cards through one workspace front door."""
    ws = hb.HeavenBase("test-card-search", backends={"main": {"type": "inmem"}})

    class SearchNote(hb.Entity):
        identifier = "search-note"
        __derive__ = [
            hb.Entity.derive("catalog.name").compute(lambda title: title, inputs=["title"]),
            hb.Entity.derive("catalog.desc").compute(lambda body: body, inputs=["body"]),
            hb.Entity.derive("catalog.type").compute(lambda: "note"),
        ]

        title = hb.field(hb.ShortText).desc("Title card text")
        body = hb.field(hb.MediumText).desc("Body card text for search")

    ws.register(SearchNote)
    ws.upsert(SearchNote, {"object_id": "n1", "title": "Count Customers", "body": "count all customers in warehouse"})
    ws.upsert(SearchNote, {"object_id": "n2", "title": "Hidden Customers", "body": "hidden customers card"})

    catalog_entity_id = hb.Catalog.schema().entity_id
    backend = ws.backends.get("main")
    hidden = ws.query(hb.Catalog).where(hb.Catalog.target_id == "n2").execute().rows()[0]
    backend.upsert([hb.ext.RowOp(catalog_entity_id, hidden["object_id"], {**hidden, "visible": False})])

    rows = ws.search(card_text="count customers").rows()
    catalog_rows = [row for row in rows if row["source_entity"] == catalog_entity_id]

    assert catalog_rows[0]["target_entity"] == "search-note"
    assert catalog_rows[0]["target_id"] == "n1"
    assert catalog_rows[0]["type"] == "note"
    assert all(row["target_id"] != "n2" for row in catalog_rows)
    assert len(ws.search("card", limit=1)) == 1

    scoped = ws.search("customers", target_entities=["search-note"])
    assert {row["target_id"] for row in scoped.rows()} == {"n1"}
    assert all(row["source_entity"] == catalog_entity_id for row in scoped.rows())
    assert {row["target_id"] for row in ws.search("customers", target_entities="search-note").rows()} == {"n1"}

    schema_rows = ws.search("body card").rows()
    assert any(
        row["source_entity"] == hb.MetaSchema.schema().entity_id and row["kind"] == "field" and row["subject_id"] == "search-note" and row["field"] == "body"
        for row in schema_rows
    )


@pytest.mark.detail
def test_workspace_search_rejects_empty_text_and_invalid_limit():
    """search() should fail loudly for inputs that cannot produce meaningful discovery results."""
    ws = hb.HeavenBase("test-card-search-inputs", backends={"main": {"type": "inmem"}})

    with pytest.raises(ValueError, match="card_text"):
        ws.search("")
    with pytest.raises(ValueError, match="limit"):
        ws.search("anything", limit=0)
    with pytest.raises(ValueError, match="target_entities"):
        ws.search("anything", target_entities=[])


def test_query_raises_schema_version_mismatch_for_missing_defaulted_fields():
    """Queries and reads should fail when stored rows lack defaulted schema fields."""
    from heavenbase.workspace.rows.errors import SchemaVersionMismatchError

    ws = hb.HeavenBase("test-schema-default-mismatch", backends={"main": {"type": "inmem"}})

    class DefaultedThing(hb.Entity):
        identifier = "defaulted-thing"
        name = hb.field(hb.ShortText)
        visible = hb.field(hb.Boolean).default(True)

    ws.register(DefaultedThing)
    ws.upsert(DefaultedThing, {"object_id": "d1", "name": "Alpha"})
    catalog_entity_id = hb.Catalog.schema().entity_id
    row = ws.query(hb.Catalog).where(hb.Catalog.target_id == "d1").execute().rows()[0]
    backend = ws.backends.get("main")
    backend.upsert([hb.ext.RowOp(catalog_entity_id, row["object_id"], {k: v for k, v in row.items() if k != "visible"})])

    with pytest.raises(SchemaVersionMismatchError, match="predates current schema fields"):
        ws.get(row["object_id"], entity=hb.Catalog)
    with pytest.raises(SchemaVersionMismatchError, match="predates current schema fields"):
        ws.query(hb.Catalog).where(hb.Catalog.target_id == "d1").execute()


def test_query_raises_schema_version_mismatch_for_missing_required_fields():
    """Queries should fail clearly when stored rows lack required schema fields."""
    from heavenbase.workspace.rows.errors import SchemaVersionMismatchError

    ws = hb.HeavenBase("test-schema-version-mismatch", backends={"main": {"type": "inmem"}})

    class StrictThing(hb.Entity):
        identifier = "strict-thing"
        name = hb.field(hb.ShortText)
        status = hb.field(hb.ShortText).required()

    ws.register(StrictThing)
    ws.upsert(StrictThing, {"object_id": "s1", "name": "Alpha", "status": "ok"})
    entity_id = StrictThing.schema().entity_id
    backend = ws.backends.get("main")
    stored = backend.get_many(entity_id, ["s1"])[0]
    backend.upsert([hb.ext.RowOp(entity_id, "s1", {k: v for k, v in stored.items() if k != "status"})])

    with pytest.raises(SchemaVersionMismatchError, match="predates current schema fields"):
        ws.query(StrictThing).execute()

    with pytest.raises(SchemaVersionMismatchError, match="predates current schema fields"):
        ws.get("s1", entity=StrictThing)


@pytest.mark.detail
def test_catalog_available_load_accepts_timestamp_like_values():
    """available now should coerce Timestamp-like inputs and reject bad values clearly."""
    from heavenbase.query.catalog import CatalogAvailable

    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert CatalogAvailable.load({"now": 1_000}).now == 1_000
    assert CatalogAvailable.load({"now": 1_500.9}).now == 1_500
    assert CatalogAvailable.load({"now": instant}).now == hb.Timestamp().validate(instant)
    assert CatalogAvailable.load({"now": "2026-01-01T00:00:00+00:00"}).now == hb.Timestamp().validate(instant)
    assert isinstance(CatalogAvailable.load({"now": None}).now, int)
    assert isinstance(CatalogAvailable.load(True).now, int)

    with pytest.raises(ValueError, match="Catalog available now expects"):
        CatalogAvailable.load({"now": "not-a-time"})


@pytest.mark.detail
def test_workspace_sign_sets_catalog_owner_defaults_until_row_overrides():
    """Workspace signing should provide Catalog owner/creator defaults for future writes."""
    ws = hb.HeavenBase("test-catalog-signature")
    assert ws.sign(owner="platform", creator="codex", auth={"scope": "internal"}, metadata={"source": "unit"}) is ws
    assert ws.catalog_signature()["owner"] == "platform"

    class SignedThing(hb.Entity):
        name = hb.field(hb.ShortText)
        owner = hb.field(hb.ShortText).default("")

    ws.register(SignedThing)
    ws.upsert(SignedThing, {"object_id": "s1", "name": "Signed"})
    ws.upsert(SignedThing, {"object_id": "s2", "name": "Owned", "owner": "ops"})

    rows = {
        row["target_id"]: row
        for row in (
            ws.query(hb.Catalog).where(hb.Catalog.target_entity == "signed-thing").select("target_id", "owner", "creator", "auth", "metadata").execute().rows()
        )
    }

    assert rows["s1"]["owner"] == "platform"
    assert rows["s1"]["creator"] == "codex"
    assert rows["s1"]["auth"] == {"scope": "internal"}
    assert rows["s1"]["metadata"] == {"source": "unit"}
    assert rows["s2"]["owner"] == "ops"
    assert rows["s2"]["creator"] == "codex"


@pytest.mark.detail
def test_id_compute_can_hash_multiple_fields_without_name():
    """Explicit object_id compute hooks let entities avoid a native name field."""
    ws = hb.HeavenBase("test-compute-id")

    class ScopedSku(hb.Entity):
        object_id = hb.field(hb.Identifier).compute(hb.utils.hash_id, inputs=["tenant", "sku"])
        tenant = hb.field(hb.ShortText)
        sku = hb.field(hb.ShortText)

    ws.register(ScopedSku)
    entity_id = ws.upsert(ScopedSku, {"tenant": "acme", "sku": "desk-01"})

    assert entity_id == hb.utils.hash_id("acme", "desk-01")
    assert "name" not in ScopedSku.schema().fields
    assert ws.get(entity_id)["sku"] == "desk-01"


@pytest.mark.detail
def test_optional_identifier_accepts_null_values():
    """Optional Identifier fields should allow explicit nulls without weakening required identifiers."""
    ws = hb.HeavenBase("test-optional-id-null")

    class OptionalLink(hb.Entity):
        name = hb.field(hb.ShortText)
        parent_id = hb.field(hb.Identifier).optional()
        required_id = hb.field(hb.Identifier).required()

    ws.register(OptionalLink)
    entity_id = ws.upsert(OptionalLink, {"object_id": "child", "name": "Child", "parent_id": None, "required_id": "root"})

    assert ws.get(entity_id)["parent_id"] is None
    assert ws.set({"object_id": entity_id, "parent_id": None}, entity=OptionalLink) == entity_id
    assert ws.get(entity_id)["parent_id"] is None
    with pytest.raises(ValueError, match="Identifier cannot be None"):
        ws.upsert(OptionalLink, {"object_id": "broken", "name": "Broken", "required_id": None})


def test_explicit_ids_replace_existing_rows():
    """Explicit ids are accepted as-is, and duplicate upserts replace the row."""
    ws = hb.HeavenBase("test-explicit-id")

    class ManualIdThing(hb.Entity):
        name = hb.field(hb.ShortText)

    ws.register(ManualIdThing)
    ws.upsert(ManualIdThing, {"object_id": "0007", "name": "Before"})
    ws.upsert(ManualIdThing, {"object_id": "0007", "name": "After"})

    assert ws.get("0007")["name"] == "After"
    assert ws.query(ManualIdThing).count().execute().scalar() == 1


def test_upsert_can_reject_existing_ids():
    """Duplicate ids should be detectable when replace semantics are disabled."""
    ws = hb.HeavenBase("test-upsert-overwrite-false")

    class UniqueThing(hb.Entity):
        name = hb.field(hb.ShortText)

    ws.register(UniqueThing)
    ws.upsert(UniqueThing, {"object_id": "u1", "name": "Before"})
    with pytest.raises(ValueError, match="already exists"):
        ws.upsert(UniqueThing, {"object_id": "u1", "name": "After"}, overwrite=False)
    assert ws.get("u1")["name"] == "Before"


def test_get_merges_routed_rows_and_detects_conflicts():
    """A row id should resolve to one logical row across routed backends."""
    ws = hb.HeavenBase("test-get-unique", backends={"main": hb.backends.InMemBackend("main"), "zaux": hb.backends.InMemBackend("zaux")})

    class RoutedThing(hb.Entity):
        name = hb.field(hb.ShortText)
        note = hb.field(hb.MediumText).store(to="main").store(to="zaux")

    ws.register(RoutedThing)
    ws.upsert(RoutedThing, {"object_id": "r1", "name": "Alpha", "note": "first"})
    assert ws.get("r1") == {"object_id": "r1", "name": "Alpha", "note": "first"}

    ws.backends.get("zaux").data[ws.id][RoutedThing.schema().entity_id]["r1"]["note"] = "conflict"
    with pytest.raises(ValueError, match="not unique"):
        ws.get("r1")
    assert ws.get("r1", unique=False)["note"] == "first"


def test_routed_writes_and_reads_respect_field_ownership():
    """Routed backend fragments should not store or contribute unowned fields."""
    ws = hb.HeavenBase("test-routed-field-ownership", backends={"main": hb.backends.InMemBackend("main"), "zaux": hb.backends.InMemBackend("zaux")})

    class OwnedRouteThing(hb.Entity):
        name = hb.field(hb.ShortText)
        note = hb.field(hb.MediumText).store(to="zaux")

    ws.register(OwnedRouteThing)
    ws.upsert(OwnedRouteThing, {"object_id": "r1", "name": "Alpha", "note": "first"})
    entity_id = OwnedRouteThing.schema().entity_id
    main_row = ws.backends.get("main").data[ws.id][entity_id]["r1"]
    aux_row = ws.backends.get("zaux").data[ws.id][entity_id]["r1"]

    assert "note" not in main_row
    assert "name" not in aux_row
    aux_row["name"] = "stale-copy"
    assert ws.get("r1") == {"object_id": "r1", "name": "Alpha", "note": "first"}


def test_universe_scans_only_routed_backends():
    """Full entity materialization should not scan unrelated workspace backends."""

    class NoScanBackend(hb.backends.InMemBackend):
        def scan(self, entity_id):
            raise AssertionError("unrelated backend should not be scanned")

    ws = hb.HeavenBase("test-universe-routed-only", backends={"main": hb.backends.InMemBackend("main"), "zaux": NoScanBackend("zaux")})

    class MainOnlyThing(hb.Entity):
        name = hb.field(hb.ShortText)

    ws.register(MainOnlyThing)
    ws.upsert(MainOnlyThing, {"object_id": "u1", "name": "Universe"})

    assert ws.ids(MainOnlyThing) == ["u1"]
    assert ws.rows(MainOnlyThing) == [{"object_id": "u1", "name": "Universe"}]


def test_id_route_is_fixed_and_replicated_to_routed_backends():
    """The built-in id field should be stored wherever an entity stores routed data."""
    ws = hb.HeavenBase("test-id-route-fixed", backends={"main": hb.backends.InMemBackend("main"), "zaux": hb.backends.InMemBackend("zaux")})

    class RoutedIDThing(hb.Entity):
        name = hb.field(hb.ShortText)
        note = hb.field(hb.MediumText).store(to="zaux")

    ws.register(RoutedIDThing)
    route_backends = {binding.backend for binding in ws.storage_plans[RoutedIDThing.schema().entity_id].field_bindings("object_id")}

    assert route_backends == {"main", "zaux"}
    with pytest.raises(ValueError, match="object_id field storage is fixed"):
        type("BadIDRouteThing", (hb.Entity,), {"object_id": hb.field(hb.Identifier).store(to="main"), "name": hb.field(hb.ShortText)})


def test_exists_and_count_use_all_routed_backends():
    """Existence and count should remain robust when routed backend fragments diverge."""
    ws = hb.HeavenBase("test-id-exists-count-all-backends", backends={"main": hb.backends.InMemBackend("main"), "zaux": hb.backends.InMemBackend("zaux")})

    class DivergedThing(hb.Entity):
        name = hb.field(hb.ShortText)
        note = hb.field(hb.MediumText).store(to="zaux")

    ws.register(DivergedThing)
    ws.upsert(DivergedThing, {"object_id": "d1", "name": "Alpha", "note": "split"})
    ws.backends.get("main").data[ws.id][DivergedThing.schema().entity_id].pop("d1")

    assert ws.exists_many(["d1", "missing"], entity=DivergedThing) == [True, False]
    assert ws.count(DivergedThing) == 1


def test_set_checks_entity_id_uniqueness_and_overwrite():
    """Set should patch by ``object_id`` while preserving explicit overwrite control."""
    ws = hb.HeavenBase("test-set-overwrite")

    class MoveableThing(hb.Entity):
        name = hb.field(hb.ShortText)

    ws.register(MoveableThing)
    ws.upsert(MoveableThing, {"object_id": "a", "name": "Alpha"})
    ws.upsert(MoveableThing, {"object_id": "b", "name": "Beta"})

    assert ws.set(MoveableThing.from_dict({"object_id": "a", "name": "Alpha 2"})) == "a"
    assert ws.get("a")["name"] == "Alpha 2"
    with pytest.raises(ValueError, match="already exists"):
        ws.set(MoveableThing.from_dict({"object_id": "b", "name": "Blocked"}), overwrite=False)

    assert ws.set({"object_id": "a", "name": "Alias Patch"}) == "a"
    assert ws.set("a", {"name": "ID-first Patch"}, entity=MoveableThing) == "a"
    assert ws.get("a")["name"] == "ID-first Patch"
    assert ws.get_many(["a", "missing", "b"], entity=MoveableThing)[1] is None
    assert ws.exists_many(["a", "missing", "b"], entity=MoveableThing) == [True, False, True]
    assert ws.set_many([{"object_id": "a", "name": "Batch A"}, {"object_id": "b", "name": "Batch B"}]) == ["a", "b"]
    assert ws.count(MoveableThing) == 2
    assert ws.query(MoveableThing).count().execute().scalar() == 2
    assert ws.delete((MoveableThing, "missing")) is False
    assert ws.delete(MoveableThing.from_dict({"object_id": "a"})) is True
    assert ws.delete_many([{"entity": MoveableThing, "object_id": "b"}, "missing"]) == [True, False]
    assert ws.count(MoveableThing) == 0


def test_set_patches_existing_row_before_recomputing_fields():
    """Set should patch from the stored row so computed fields can use unchanged inputs."""
    ws = hb.HeavenBase("test-set-recompute")

    def joined_title(title, status):
        return f"{title}:{status}"

    class ComputedPatchIssue(hb.Entity):
        title = hb.field(hb.ShortText)
        status = hb.field(hb.ShortText).default("todo")
        summary = hb.field(hb.ShortText).compute(joined_title, inputs=["title", "status"])

    ws.register(ComputedPatchIssue)
    object_id = ws.upsert(ComputedPatchIssue, {"object_id": "s1", "title": "Prototype"})

    assert ws.set({"object_id": object_id, "status": "done"}, entity=ComputedPatchIssue) == object_id
    assert ws.get(object_id, entity=ComputedPatchIssue)["summary"] == "Prototype:done"
    assert ws.set({"object_id": object_id, "status": "blocked", "summary": "stale"}, entity=ComputedPatchIssue) == object_id
    assert ws.get(object_id, entity=ComputedPatchIssue)["summary"] == "Prototype:blocked"


@pytest.mark.detail
def test_workspace_pythonic_id_operators(default_extensions_without_agent):
    """Workspace item operators should delegate to id-centric CRUD."""
    ws = hb.HeavenBase("test-pythonic-operators")

    class OperatorThing(hb.Entity):
        name = hb.field(hb.ShortText)
        score = hb.field(hb.Float).default(0.0)

    ws.register(OperatorThing)
    ws[OperatorThing, "o1"] = {"name": "Alpha"}

    assert "o1" in ws
    assert (OperatorThing, "o1") in ws
    assert ws["o1"]["name"] == "Alpha"
    assert list(ws) == ["o1"]
    assert len(ws) == 1
    assert ws.ids() == ["o1"]
    assert ws.rows()[0]["score"] == 0.0
    assert ws.items()[0][0] == "o1"

    ws["o1"] = {"name": "Beta"}
    assert ws[OperatorThing, "o1"]["name"] == "Beta"

    del ws["o1"]
    assert "o1" not in ws
    with pytest.raises(KeyError):
        ws["o1"]
    with pytest.raises(KeyError):
        del ws["missing"]

    class OtherOperatorThing(hb.Entity):
        label = hb.field(hb.ShortText)

    ws.register(OtherOperatorThing)
    with pytest.raises(TypeError, match="exactly one"):
        list(ws)


@pytest.mark.detail
def test_workspace_pythonic_operators_require_entity_for_duplicate_ids():
    """Mapping-style row reads should require entity disambiguation for duplicate ids."""
    ws = hb.HeavenBase("test-pythonic-duplicate-id-operators")

    class OperatorThingA(hb.Entity):
        name = hb.field(hb.ShortText)

    class OperatorThingB(hb.Entity):
        title = hb.field(hb.ShortText)

    ws.register(OperatorThingA)
    ws.register(OperatorThingB)
    ws.upsert(OperatorThingA, {"object_id": "shared", "name": "Alpha"})
    ws.upsert(OperatorThingB, {"object_id": "shared", "title": "Beta"})

    with pytest.raises(ValueError, match="multiple entities"):
        ws["shared"]
    assert ws[OperatorThingA, "shared"]["name"] == "Alpha"
    assert ws[OperatorThingB, "shared"]["title"] == "Beta"
    assert "shared" in ws
    assert (OperatorThingA, "shared") in ws


@pytest.mark.full
def test_upsert_many_batches_and_rejects_duplicate_ids():
    """Batch upserts should preserve order and reject duplicate object_id values before writing when requested."""
    ws = hb.HeavenBase("test-upsert-many-batch")

    class BatchThing(hb.Entity):
        __derive__ = [hb.Entity.derive("catalog.name").compute(identity, inputs=["name"])]

        name = hb.field(hb.ShortText)

    ws.register(BatchThing)
    assert ws.upsert_many(BatchThing, [{"object_id": "a", "name": "Alpha"}, {"object_id": "b", "name": "Beta"}]) == ["a", "b"]
    with pytest.raises(ValueError, match="already exists"):
        ws.upsert_many(BatchThing, [{"object_id": "a", "name": "Blocked"}], overwrite=False)
    assert ws.exists("c", entity=BatchThing) is False
    with pytest.raises(ValueError, match="duplicate object_id"):
        ws.upsert_many(BatchThing, [{"object_id": "c", "name": "Gamma"}, {"object_id": "c", "name": "Duplicate"}])
    assert ws.exists("c", entity=BatchThing) is False
    with pytest.raises(ValueError, match="duplicate object_id"):
        ws.upsert_many(BatchThing, [{"name": "Same Name"}, {"name": "Same Name"}])
    assert ws.exists(hb.utils.hash_id("batch-thing", "Same Name"), entity=BatchThing) is False


def test_upsert_many_fresh_catalog_mode_skips_existing_catalog_lookup(monkeypatch):
    """Fresh Catalog batches should retain ordinary rows without probing Catalog per item."""
    ws = hb.HeavenBase("test-upsert-many-fresh-catalog")

    class FreshCatalogThing(hb.Entity):
        __derive__ = [hb.Entity.derive("catalog.name").compute(identity, inputs=["name"])]

        name = hb.field(hb.ShortText)

    ws.register(FreshCatalogThing)
    original_rows_for_ids = ws._row_store.rows_for_ids
    original_hash_length = ws._catalog.hash_length
    hash_length_calls = 0

    def reject_catalog_lookup(entity_id, row_ids, *, columns=None):
        if entity_id == hb.Catalog.schema().entity_id:
            raise AssertionError("fresh Catalog mode performed an existing-row lookup")
        return original_rows_for_ids(entity_id, row_ids, columns=columns)

    def counted_hash_length():
        nonlocal hash_length_calls
        hash_length_calls += 1
        return original_hash_length()

    with monkeypatch.context() as patched:
        patched.setattr(ws._row_store, "rows_for_ids", reject_catalog_lookup)
        patched.setattr(ws._catalog, "hash_length", counted_hash_length)
        assert ws.upsert_many(
            FreshCatalogThing,
            [{"object_id": "a", "name": "Alpha"}, {"object_id": "b", "name": "Beta"}],
            catalog_mode="fresh",
        ) == ["a", "b"]
    assert ws.query(FreshCatalogThing).execute().rows() == [
        {"object_id": "a", "name": "Alpha"},
        {"object_id": "b", "name": "Beta"},
    ]
    assert [row["target_id"] for row in ws.query(hb.Catalog).where(hb.Catalog.target_entity == "fresh-catalog-thing").execute().rows()] == [
        "a",
        "b",
    ]
    assert hash_length_calls == 1


def test_upsert_many_rejects_unknown_catalog_mode_before_writing():
    """Catalog mode is a closed write-state protocol rather than an ignored hint."""
    ws = hb.HeavenBase("test-upsert-many-catalog-mode")

    class CatalogModeThing(hb.Entity):
        name = hb.field(hb.ShortText)

    ws.register(CatalogModeThing)

    with pytest.raises(ValueError, match="catalog_mode"):
        ws.upsert_many(CatalogModeThing, [{"object_id": "a", "name": "Alpha"}], catalog_mode="unsafe")

    assert ws.get("a", entity=CatalogModeThing) is None


@pytest.mark.detail
def test_catalog_rows_wait_for_entity_backend_success():
    """Catalog rows should not publish for a batch that fails on an entity backend."""

    class FailingBackend(hb.backends.InMemBackend):
        def upsert(self, rows):
            if any(row.entity_id == "catalog-failing-thing" for row in rows):
                raise RuntimeError("intentional entity backend failure")
            return super().upsert(rows)

    ws = _detached_without_pending(
        "test-catalog-write-order",
        backends={"main": hb.backends.InMemBackend("main"), "fail": FailingBackend("fail")},
    )

    class CatalogMainThing(hb.Entity):
        identifier = "catalog-main-thing"

        name = hb.field(hb.ShortText)

    class CatalogFailingThing(hb.Entity):
        identifier = "catalog-failing-thing"

        name = hb.field(hb.ShortText).store(to="fail")

    ws.register(CatalogMainThing)
    ws.register(CatalogFailingThing)
    with pytest.raises(RuntimeError, match="intentional entity backend failure"):
        ws.upsert_many(
            CatalogMainThing,
            [
                CatalogMainThing.from_dict(object_id="m1", name="Main"),
                CatalogFailingThing.from_dict(object_id="f1", name="Fail"),
            ],
        )

    catalog_rows = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "catalog-failing-thing").execute().rows()

    assert catalog_rows == []
    assert ws.get("f1", entity=CatalogFailingThing) is None


@pytest.mark.detail
def test_system_catalog_ids_are_shortened_to_identifier_limit():
    """Generated catalog row ids should fit Identifier even when object names are long."""
    ws = hb.HeavenBase("test-system-catalog-short-ids")
    entity_id = "catalog-long-entity-name-with-route-fields-and-backend-names"
    field_name = "field_name_with_a_long_python_identifier_for_catalog_routes"

    LongCatalogThing = type(
        "LongCatalogThing",
        (hb.Entity,),
        {
            "identifier": entity_id,
            field_name: hb.field(hb.ShortText),
        },
    )
    ws.register(LongCatalogThing)

    rows = ws.query(hb.MetaSchema).where(hb.MetaSchema.subject_id == entity_id).execute().rows()

    assert any(row["kind"] == "storage" for row in rows)
    assert all(len(row["object_id"]) <= hb.Identifier().max_chars for row in rows)
    assert any(row["kind"] == "storage" and row["object_id"].startswith("storage:") for row in rows)


@pytest.mark.detail
def test_catalog_rows_derive_fields_and_hydrate_target_object():
    """Catalog rows should expose discovery fields and point back to the typed object."""
    ws = hb.HeavenBase("test-catalog-derived-fields")

    class CatalogProduct(hb.Entity):
        identifier = "catalog-derived-product"
        __derive__ = [
            hb.Entity.derive("catalog.name").compute(identity, inputs=["name"]),
            hb.Entity.derive("catalog.desc").compute(identity, inputs=["desc"]),
            hb.Entity.derive("catalog.tags").compute(identity, inputs=["tags"]),
        ]

        name = hb.field(hb.ShortText)
        desc = hb.field(hb.MediumText)
        tags = hb.field(hb.Json).default([])
        price = hb.field(hb.Float)

    ws.register(CatalogProduct)
    ws.upsert(CatalogProduct, {"object_id": "p1", "name": "Desk", "desc": "Oak writing desk", "tags": ["office"], "price": 120.0})
    catalog_row = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "catalog-derived-product").execute().rows()[0]
    product = ws.get(catalog_row["target_id"], entity=catalog_row["target_entity"])

    assert catalog_row["target_id"] == "p1"
    assert catalog_row["target_entity"] == "catalog-derived-product"
    assert catalog_row["desc"] == "Oak writing desk"
    assert catalog_row["tags"] == ["office"]
    assert product["object_id"] == catalog_row["target_id"]
    assert product["desc"] == catalog_row["desc"]

    class CatalogFallbackProduct(hb.Entity):
        identifier = "catalog-fallback-product"
        __derive__ = [
            hb.Entity.derive("catalog.name").compute(identity, inputs=["label"]),
            hb.Entity.derive("catalog.desc").compute(identity, inputs=["summary"]),
        ]

        label = hb.field(hb.ShortText)
        summary = hb.field(hb.MediumText)

    ws.register(CatalogFallbackProduct)
    ws.upsert(CatalogFallbackProduct, {"object_id": "p2", "label": "Lamp", "summary": "Brass desk lamp"})
    fallback_row = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "catalog-fallback-product").execute().rows()[0]

    assert fallback_row["name"] == "Lamp"
    assert fallback_row["desc"] == "Brass desk lamp"


@pytest.mark.detail
def test_alter_entity_rejects_existing_compute_query_compute_and_derive_changes():
    """alter_entity should reject behavior changes that would leave existing data stale."""
    ws = hb.HeavenBase("test-alter-behavior")

    class AlterCompute(hb.Entity):
        identifier = "alter-compute"

        name = hb.field(hb.ShortText)
        summary = hb.field(hb.ShortText).compute(lambda name: f"old:{name}", inputs=["name"])

    ws.register(AlterCompute)

    class AlterComputeV2(hb.Entity):
        identifier = "alter-compute"

        name = hb.field(hb.ShortText)
        summary = hb.field(hb.ShortText).compute(lambda name: f"new:{name}", inputs=["name"])

    with pytest.raises(ValueError, match="compute hooks"):
        ws.alter_entity(AlterComputeV2, confirm="alter-compute")

    def query_embed_old(value):
        return [float(len(str(value))), 0.0]

    def query_embed_new(value):
        return [0.0, float(len(str(value)))]

    class AlterQueryCompute(hb.Entity):
        identifier = "alter-query-compute"

        title = hb.field(hb.ShortText)
        emb = hb.field(hb.Vector[2]).query_compute(query_embed_old)

    ws.register(AlterQueryCompute)

    class AlterQueryComputeV2(hb.Entity):
        identifier = "alter-query-compute"

        title = hb.field(hb.ShortText)
        emb = hb.field(hb.Vector[2]).query_compute(query_embed_new)

    with pytest.raises(ValueError, match="query_compute hooks"):
        ws.alter_entity(AlterQueryComputeV2, confirm="alter-query-compute")

    class AlterDerive(hb.Entity):
        identifier = "alter-derive"
        __derive__ = [hb.Entity.derive("catalog.name").compute(lambda name: name, inputs=["name"])]

        name = hb.field(hb.ShortText)

    ws.register(AlterDerive)

    class AlterDeriveV2(hb.Entity):
        identifier = "alter-derive"
        __derive__ = [hb.Entity.derive("catalog.name").compute(lambda name: f"new:{name}", inputs=["name"])]

        name = hb.field(hb.ShortText)

    with pytest.raises(ValueError, match="entity derives"):
        ws.alter_entity(AlterDeriveV2, confirm="alter-derive")


def test_entity_derive_materializes_native_fields_and_catalog_projection():
    """Entity.derive should fill native fields and project normalized rows to Catalog."""
    ws = hb.HeavenBase("test-entity-derive")

    def first_sentence(row):
        return row["body"].split(".")[0]

    class DerivedArticle(hb.Entity):
        identifier = "derived-article"
        __derive__ = [
            hb.Entity.derive("desc").compute(first_sentence),
            hb.Entity.derive("slug").compute(lambda title: title.lower().replace(" ", "-"), inputs=["title"]),
            hb.Entity.derive("status").compute(lambda: "active"),
            hb.Entity.derive("catalog.name").compute(identity, inputs=["title"]),
            hb.Entity.derive("catalog.desc").compute(identity, inputs=["desc"]),
            hb.Entity.derive("catalog.summary").compute(identity, inputs=["body"]),
        ]

        title = hb.field(hb.ShortText)
        body = hb.field(hb.LongText)
        desc = hb.field(hb.MediumText)
        slug = hb.field(hb.ShortText)
        status = hb.field(hb.ShortText)

    ws.register(DerivedArticle)
    ws.upsert(DerivedArticle, {"object_id": "a1", "title": "Launch Notes", "body": "Ready now. More details"})
    row = ws.get("a1", entity=DerivedArticle)
    catalog_row = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "derived-article").execute().rows()[0]

    assert not hasattr(hb, "derive")
    assert row["desc"] == "Ready now"
    assert row["slug"] == "launch-notes"
    assert row["status"] == "active"
    assert catalog_row["name"] == "Launch Notes"
    assert catalog_row["desc"] == "Ready now"
    assert catalog_row["summary"] == "Ready now. More details"
    assert catalog_row["created_at"] > 0
    assert catalog_row["updated_at"] >= catalog_row["created_at"]

    ws.set({"object_id": "a1", "title": "Launch Update", "body": "Updated body. Extra"}, entity=DerivedArticle)
    patched = ws.get("a1", entity=DerivedArticle)
    patched_catalog = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "derived-article").execute().rows()[0]

    assert patched["desc"] == "Updated body"
    assert patched["slug"] == "launch-update"
    assert patched_catalog["name"] == "Launch Update"
    assert patched_catalog["desc"] == "Updated body"
    assert patched_catalog["summary"] == "Updated body. Extra"
    assert patched_catalog["created_at"] == catalog_row["created_at"]
    assert patched_catalog["updated_at"] >= catalog_row["updated_at"]


@pytest.mark.detail
def test_entity_derive_later_targets_override_field_compute_and_prior_derives():
    """Later effective derives should win for duplicate native and Catalog targets."""
    ws = hb.HeavenBase("test-entity-derive-overrides")

    class OverrideArticle(hb.Entity):
        identifier = "override-article"
        __derive__ = [
            hb.Entity.derive("summary").compute(lambda title: f"first:{title}", inputs=["title"]),
            hb.Entity.derive("summary").compute(lambda title: f"second:{title}", inputs=["title"]),
            hb.Entity.derive("catalog.name").compute(lambda title: f"first:{title}", inputs=["title"]),
            hb.Entity.derive("catalog.name").compute(lambda title: f"second:{title}", inputs=["title"]),
        ]

        title = hb.field(hb.ShortText)
        summary = hb.field(hb.ShortText).compute(lambda title: f"field:{title}", inputs=["title"])

    ws.register(OverrideArticle)
    ws.upsert(OverrideArticle, {"object_id": "o1", "title": "Alpha"})
    row = ws.get("o1", entity=OverrideArticle)
    catalog_row = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "override-article").execute().rows()[0]

    assert row["summary"] == "second:Alpha"
    assert catalog_row["name"] == "second:Alpha"


@pytest.mark.detail
def test_catalog_derive_rejects_auto_fields():
    """Catalog auto fields should remain owned by the workspace writer."""
    ws = hb.HeavenBase("test-catalog-auto-derive")

    class BadCatalogDerive(hb.Entity):
        identifier = "bad-catalog-derive"
        __derive__ = [hb.Entity.derive("catalog.updated_at").compute(lambda: 1)]

        title = hb.field(hb.ShortText)

    with pytest.raises(ValueError, match="auto-generated"):
        ws.register(BadCatalogDerive)


def test_catalog_allows_same_target_id_across_entities():
    """Catalog target ids are scoped by the target entity type."""
    ws = hb.HeavenBase("test-catalog-duplicate-target-ids")

    class CatalogDuplicateProduct(hb.Entity):
        identifier = "catalog-duplicate-product"

        name = hb.field(hb.ShortText)

    class CatalogDuplicateNote(hb.Entity):
        identifier = "catalog-duplicate-note"

        name = hb.field(hb.ShortText)

    ws.register(CatalogDuplicateProduct)
    ws.register(CatalogDuplicateNote)
    ws.upsert(CatalogDuplicateProduct, {"object_id": "shared", "name": "Desk"})
    ws.upsert(CatalogDuplicateNote, {"object_id": "shared", "name": "Desk note"})

    rows = ws.query(hb.Catalog).where(hb.Catalog.target_id == "shared").select("target_id", "target_entity", "name").execute().rows()

    assert {row["target_entity"] for row in rows} == {"catalog-duplicate-product", "catalog-duplicate-note"}
    assert all(row["target_id"] == "shared" for row in rows)


def test_catalog_audit_and_repair_missing_after_partial_catalog_failure():
    """Catalog repair should rebuild missing rows after an explicit failed catalog publish."""

    class CatalogFailOnceBackend(hb.backends.InMemBackend):
        def __init__(self, name="main"):
            super().__init__(name)
            self.fail_catalog_once = True

        def upsert(self, rows):
            if self.fail_catalog_once and any(row.entity_id == hb.Catalog.schema().entity_id for row in rows):
                self.fail_catalog_once = False
                raise RuntimeError("intentional catalog publish failure")
            return super().upsert(rows)

    ws = _detached_without_pending("test-catalog-audit-missing", backends={"main": CatalogFailOnceBackend("main")})

    class CatalogAuditMissingThing(hb.Entity):
        identifier = "catalog-audit-missing-thing"

        name = hb.field(hb.ShortText)

    ws.register(CatalogAuditMissingThing)
    with pytest.raises(RuntimeError, match="intentional catalog publish failure"):
        ws.upsert(CatalogAuditMissingThing, {"object_id": "m1", "name": "Missing catalog"})

    assert ws.get("m1", entity=CatalogAuditMissingThing)["name"] == "Missing catalog"
    audit = ws.audit()["catalog"]
    assert audit["ok"] is False
    assert [item["expected"]["target_id"] for item in audit["missing"]] == ["m1"]

    repair = ws.repair()["catalog"]
    assert repair["missing_written"] == 1
    assert repair["ok"] is True
    assert ws.audit()["catalog"]["ok"] is True
    assert ws.query(hb.Catalog).where(hb.Catalog.target_entity == "catalog-audit-missing-thing").execute().rows()[0]["target_id"] == "m1"


@pytest.mark.detail
def test_catalog_audit_and_repair_manual_backend_divergence():
    """Catalog repair should rewrite mismatched rows and delete dangling rows explicitly."""
    ws = hb.HeavenBase("test-catalog-audit-divergence")

    class CatalogAuditProduct(hb.Entity):
        identifier = "catalog-audit-product"

        name = hb.field(hb.ShortText)

    ws.register(CatalogAuditProduct)
    ws.upsert(CatalogAuditProduct, {"object_id": "p1", "name": "Desk"})
    catalog_entity_id = hb.Catalog.schema().entity_id
    backend = ws.backends.get("main")
    catalog_row = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "catalog-audit-product").execute().rows()[0]
    backend.upsert([hb.ext.RowOp(catalog_entity_id, catalog_row["object_id"], {**catalog_row, "target_id": "wrong"})])
    backend.upsert(
        [
            hb.ext.RowOp(
                catalog_entity_id,
                "catalog:rogue:ghost",
                {
                    "object_id": "catalog:rogue:ghost",
                    "target_id": "ghost",
                    "target_entity": "catalog-audit-product",
                    "name": "Ghost",
                    "desc": "",
                    "tags": [],
                    "active": True,
                    "ws": ws.id,
                },
            )
        ]
    )

    audit = ws.audit()["catalog"]
    assert audit["ok"] is False
    assert audit["mismatched"][0]["fields"] == ["target_id"]
    assert [item["catalog"]["target_id"] for item in audit["dangling"]] == ["ghost"]

    repair = ws.repair()["catalog"]
    assert repair["mismatched_rewritten"] == 1
    assert repair["dangling_deleted"] == 1
    assert repair["ok"] is True
    repaired_rows = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "catalog-audit-product").execute().rows()
    assert [row["target_id"] for row in repaired_rows] == ["p1"]


@pytest.mark.detail
def test_catalog_audit_and_repair_projected_field_drift():
    """Catalog repair should rebuild projected fields from authoritative entity rows."""
    ws = hb.HeavenBase("test-catalog-audit-projection")

    class CatalogProjectionProduct(hb.Entity):
        identifier = "catalog-projection-product"
        __derive__ = [hb.Entity.derive("catalog.name").compute(identity, inputs=["name"])]

        name = hb.field(hb.ShortText)

    ws.register(CatalogProjectionProduct)
    ws.upsert(CatalogProjectionProduct, {"object_id": "p1", "name": "Desk"})
    catalog_entity_id = hb.Catalog.schema().entity_id
    backend = ws.backends.get("main")
    catalog_row = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "catalog-projection-product").execute().rows()[0]
    backend.upsert([hb.ext.RowOp(catalog_entity_id, catalog_row["object_id"], {**catalog_row, "name": "Wrong"})])

    audit = ws.audit()["catalog"]
    assert audit["mismatched"][0]["fields"] == ["name"]

    repair = ws.repair()["catalog"]
    assert repair["mismatched_rewritten"] == 1
    assert repair["ok"] is True
    repaired = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "catalog-projection-product").execute().rows()[0]
    assert repaired["name"] == "Desk"
    assert repaired["created_at"] == catalog_row["created_at"]
    assert repaired["updated_at"] >= catalog_row["updated_at"]


@pytest.mark.detail
def test_catalog_audit_recomputes_native_derives_before_catalog_projection():
    """Catalog audit should rebuild native derives before deriving Catalog fields."""
    ws = hb.HeavenBase("test-catalog-audit-native-derive")

    def first_sentence(row):
        return row["body"].split(".")[0]

    class CatalogProjectionArticle(hb.Entity):
        identifier = "catalog-projection-article"
        __derive__ = [
            hb.Entity.derive("desc").compute(first_sentence),
            hb.Entity.derive("catalog.name").compute(identity, inputs=["title"]),
            hb.Entity.derive("catalog.desc").compute(identity, inputs=["desc"]),
            hb.Entity.derive("catalog.summary").compute(identity, inputs=["body"]),
        ]

        title = hb.field(hb.ShortText)
        body = hb.field(hb.LongText)
        desc = hb.field(hb.MediumText)

    ws.register(CatalogProjectionArticle)
    ws.upsert(CatalogProjectionArticle, {"object_id": "a1", "title": "Launch", "body": "Fresh body. More"})
    backend = ws.backends.get("main")
    backend.upsert([hb.ext.RowOp("catalog-projection-article", "a1", {"object_id": "a1", "title": "Launch", "body": "Fresh body. More"})])

    catalog_entity_id = hb.Catalog.schema().entity_id
    catalog_row = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "catalog-projection-article").execute().rows()[0]
    backend.upsert([hb.ext.RowOp(catalog_entity_id, catalog_row["object_id"], {**catalog_row, "summary": "Stale"})])

    audit = ws.audit()["catalog"]
    assert audit["mismatched"][0]["fields"] == ["summary"]

    repair = ws.repair()["catalog"]
    assert repair["mismatched_rewritten"] == 1
    repaired = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "catalog-projection-article").execute().rows()[0]
    assert repaired["summary"] == "Fresh body. More"


def test_query_dsl_and_json_surface():
    """Python DSL and JSON query surfaces should produce equivalent query behavior."""
    ws = hb.HeavenBase("test-query")

    class TestArticle(hb.Entity):
        title = hb.field(hb.ShortText)
        body = hb.field(hb.LongText)
        score = hb.field(hb.Float)

    ws.register(TestArticle)
    ws.upsert_many(
        TestArticle,
        [
            TestArticle.from_dict({"object_id": "a1", "title": "A", "body": "schema design", "score": 9}),
            TestArticle.from_dict({"object_id": "a2", "title": "B", "body": "ops", "score": 5}),
        ],
    )
    assert ws.query(TestArticle).where(TestArticle.body.match("schema")).count().execute().scalar() == 1
    frame = ws.query_json(TestArticle, {"filter": {"score": {"$gte": 8}}, "select": ["title", "score"]}).execute()
    assert frame.rows()[0]["title"] == "A"

    with pytest.raises(ValueError, match="Unsupported JSON query key 'limt'.*\\sDid you mean 'limit'"):
        ws.query_json(TestArticle, {"limt": 1})
    with pytest.raises(ValueError, match="Unsupported JSON query key 'merge_rule'"):
        ws.query_json(TestArticle, {"merge_rule": "union"})
    with pytest.raises(TypeError, match="JSON query spec must be an object"):
        ws.query_json(TestArticle, [])

    request = ws.query(TestArticle).limit(1).spec.to_dict()
    assert "merge_rule" not in request


def test_vector_near_preserves_score():
    """Vector near queries should preserve score columns through projection."""
    ws = hb.HeavenBase("test-vector")

    class TestTicket(hb.Entity):
        text = hb.field(hb.LongText)
        emb = hb.field(hb.Vector[2])

    ws.register(TestTicket)
    ws.upsert(TestTicket.from_dict({"object_id": "t1", "text": "a", "emb": [1, 0]}))
    ws.upsert(TestTicket.from_dict({"object_id": "t2", "text": "b", "emb": [0, 1]}))
    frame = ws.query(TestTicket).near(TestTicket.emb, [1, 0], top_k=1).select("text", "score").execute()
    assert frame.rows()[0]["text"] == "a"
    assert "score" in frame.col_names()


def test_vector_near_text_uses_query_compute():
    """Text near queries should use a vector field's query-time compute hook."""
    ws = hb.HeavenBase("test-query-compute-text")
    calls = []

    def embed(text):
        return [1.0, 0.0] if "launch" in text.lower() or "debug" in text.lower() else [0.0, 1.0]

    def issue_embed(title, summary):
        return embed(f"{title} {summary}")

    def query_embed(text):
        calls.append(text)
        return embed(text)

    class QueryComputeIssue(hb.Entity):
        title = hb.field(hb.ShortText)
        summary = hb.field(hb.LongText).default("")
        emb = hb.field(hb.Vector[2]).compute(issue_embed, inputs=["title", "summary"]).query_compute(query_embed)

    ws.register(QueryComputeIssue)
    ws.upsert(QueryComputeIssue.from_dict({"object_id": "i1", "title": "Launch debug", "summary": "Fix risky launch blockers"}))
    ws.upsert(QueryComputeIssue.from_dict({"object_id": "i2", "title": "Billing", "summary": "Invoice cleanup"}))

    frame = ws.query(QueryComputeIssue).near(QueryComputeIssue.emb, "debug launch risk", top_k=1).select("title", "score").execute()

    assert frame.ids == ["i1"]
    assert frame.rows()[0]["title"] == "Launch debug"
    assert calls == ["debug launch risk"]


@pytest.mark.detail
def test_json_near_query_text_uses_query_compute_and_explain_notes():
    """JSON query specs should accept near.query text and explain the query compute step."""
    ws = hb.HeavenBase("test-query-compute-json")

    def embed(text):
        return [1.0, 0.0] if "billing" in text.lower() else [0.0, 1.0]

    def issue_embed(title, summary):
        return embed(f"{title} {summary}")

    class JsonQueryComputeIssue(hb.Entity):
        title = hb.field(hb.ShortText)
        summary = hb.field(hb.LongText).default("")
        emb = hb.field(hb.Vector[2]).compute(issue_embed, inputs=["title", "summary"]).query_compute(embed)

    ws.register(JsonQueryComputeIssue)
    ws.upsert(JsonQueryComputeIssue.from_dict({"object_id": "i1", "title": "Launch", "summary": "Rollout readiness"}))
    ws.upsert(JsonQueryComputeIssue.from_dict({"object_id": "i2", "title": "Billing", "summary": "Invoice cleanup"}))

    spec = {"near": {"field": "emb", "query": "billing invoices", "top_k": 1}, "select": ["title", "score"]}
    frame = ws.query_json(JsonQueryComputeIssue, spec).execute()
    explain = ws.query_json(JsonQueryComputeIssue, spec).explain()

    assert frame.ids == ["i2"]
    near_step = next(step for step in explain["steps"] if step.get("node") == "near")
    assert near_step["query_compute"] is True
    assert near_step["query_input_type"] == "str"
    assert near_step["query_compute_fn"].endswith("embed")


@pytest.mark.detail
def test_vector_near_vector_value_skips_query_compute():
    """Already-valid vector near values should not be transformed."""
    ws = hb.HeavenBase("test-query-compute-vector-skip")
    calls = []

    def query_embed(text):
        calls.append(text)
        return [0.0, 1.0]

    class QueryComputeSkipIssue(hb.Entity):
        title = hb.field(hb.ShortText)
        emb = hb.field(hb.Vector[2]).query_compute(query_embed)

    ws.register(QueryComputeSkipIssue)
    ws.upsert(QueryComputeSkipIssue.from_dict({"object_id": "i1", "title": "A", "emb": [1.0, 0.0]}))
    ws.upsert(QueryComputeSkipIssue.from_dict({"object_id": "i2", "title": "B", "emb": [0.0, 1.0]}))

    frame = ws.query(QueryComputeSkipIssue).near(QueryComputeSkipIssue.emb, [0.0, 1.0], top_k=1).execute()

    assert frame.ids == ["i2"]
    assert calls == []


def test_vector_near_text_without_query_compute_raises_clear_error():
    """Text near values require an explicit query_compute hook."""
    ws = hb.HeavenBase("test-query-compute-missing")

    class PlainVectorIssue(hb.Entity):
        title = hb.field(hb.ShortText)
        emb = hb.field(hb.Vector[2])

    ws.register(PlainVectorIssue)
    ws.upsert(PlainVectorIssue.from_dict({"object_id": "i1", "title": "A", "emb": [1.0, 0.0]}))

    with pytest.raises(ValueError, match="query_compute"):
        ws.query(PlainVectorIssue).near(PlainVectorIssue.emb, "debug launch", top_k=1).execute()


@pytest.mark.detail
def test_query_compute_capability_is_visible_to_mcp_describe_and_query():
    """MCP describe/query tools should expose and use text near capability."""
    ws = hb.HeavenBase("test-query-compute-mcp")

    def embed(text):
        return [1.0, 0.0] if "risk" in text.lower() else [0.0, 1.0]

    class McpQueryComputeIssue(hb.Entity):
        title = hb.field(hb.ShortText)
        summary = hb.field(hb.LongText).default("")
        emb = hb.field(hb.Vector[2]).compute(lambda title, summary: embed(f"{title} {summary}"), inputs=["title", "summary"]).query_compute(embed)

    ws.register(McpQueryComputeIssue)
    ws.upsert(McpQueryComputeIssue.from_dict({"object_id": "i1", "title": "Risk review", "summary": "Launch risk mitigation"}))
    ws.upsert(McpQueryComputeIssue.from_dict({"object_id": "i2", "title": "Docs", "summary": "Workshop cleanup"}))
    toolkit = ws.to_mcp(name="query-compute-mcp-test")

    described = toolkit.run("describe_entity", entity="mcp-query-compute-issue")
    queried = toolkit.run(
        "query",
        entity="mcp-query-compute-issue",
        spec={"near": {"field": "emb", "query": "launch risk", "top_k": 1}, "select": ["title", "score"]},
    )

    assert described["query_capabilities"]["emb"]["near"]["accepts"] == ["vector", "text"]
    assert queried["ids"] == ["i1"]


def test_json_entity_definition_registers_query_compute():
    """JSON entity definitions should resolve query_compute functions from their own registry."""
    ws = hb.HeavenBase("test-query-compute-json-entity")

    def embed(text):
        return [1.0, 0.0] if "alpha" in text.lower() else [0.0, 1.0]

    spec = {
        "entity_id": "json-query-compute-issue",
        "name": "JsonQueryComputeIssue",
        "fields": {
            "title": {"type": "short-text"},
            "emb": {"type": {"type": "vector", "dim": 2}, "query_compute": "embed_query"},
        },
    }
    entity = ws.register(spec, query_computes={"embed_query": embed})
    ws.upsert(entity.from_dict({"object_id": "i1", "title": "Alpha", "emb": [1.0, 0.0]}))
    ws.upsert(entity.from_dict({"object_id": "i2", "title": "Beta", "emb": [0.0, 1.0]}))

    frame = ws.query_json("json-query-compute-issue", {"near": {"field": "emb", "query": "alpha work", "top_k": 1}}).execute()

    assert frame.ids == ["i1"]


def test_query_compute_rejects_unsupported_ops():
    """The current query_compute boundary is explicit: only near is supported."""
    with pytest.raises(ValueError, match="near"):
        hb.field(hb.ShortText).query_compute(str, ops=["match"])


def test_custom_handler():
    """Workspace-specific custom handlers should compile and execute custom ops."""
    ws_id = "test-handler"
    ws = hb.HeavenBase(ws_id, backends={"main": {"type": "inmem"}, "vec": {"type": "inmem"}})

    def compile_ends_with(field_schema, value, ctx):

        def meta(row):
            return str(row.get(field_schema.name, "")).endswith(value)

        return hb.ext.QueryFragment(ctx.backend, "ends_with", field_schema.name, meta)

    hb.ext.Handler(hb.ShortText, "ends_with", "inmem", hb.InlineColumn, compile_ends_with).register(ws.handlers, replace=True)

    class TestAsset(hb.Entity):
        name = hb.field(hb.ShortText)

    ws.register(TestAsset)
    ws.upsert(TestAsset.from_dict({"object_id": "search-index", "name": "Search Index"}))
    ws.upsert(TestAsset.from_dict({"object_id": "search-api", "name": "Search API"}))
    assert ws.query(TestAsset).where(TestAsset.name.op("ends_with", "Index")).count().execute().scalar() == 1


@pytest.mark.detail
def test_entity_register_uses_workspace_id_and_allows_multiple_workspaces():
    """An entity class should bind cleanly to multiple named workspaces."""
    ws_a = hb.HeavenBase("multi-a")
    ws_b = hb.HeavenBase("multi-b")

    class MultiThing(hb.Entity):
        name = hb.field(hb.ShortText)

    # Bidirectional registration: entity-side and workspace-side both work.
    MultiThing.register("multi-a")
    ws_b.register(MultiThing)

    ws_a.upsert(MultiThing.from_dict({"object_id": "one", "name": "Alpha"}))
    ws_b.upsert(MultiThing.from_dict({"object_id": "one", "name": "Beta"}))

    assert MultiThing.workspace_ids() == ["multi-a", "multi-b"]
    assert ws_a.get("one")["name"] == "Alpha"
    assert ws_b.get("one")["name"] == "Beta"


@pytest.mark.detail
def test_entity_can_register_by_workspace_id_before_workspace_exists():
    """Entity classes registered before workspace creation should auto-bind later."""

    class FutureThing(hb.Entity):
        name = hb.field(hb.ShortText)

    FutureThing.register("future-workspace")
    ws = hb.HeavenBase("future-workspace")

    assert "future-thing" in ws.entities
    ws.upsert(FutureThing.from_dict({"object_id": "f1", "name": "ready"}))
    assert ws.query(FutureThing).where(FutureThing.name == "ready").count().execute().scalar() == 1


def test_heavenbase_load_requires_registration_and_reuses_live_workspace():
    """Loading is retrieval-only while construction ensures a workspace."""
    with pytest.raises(KeyError, match="not-registered-ws"):
        hb.HeavenBase.load("not-registered-ws")
    with pytest.raises(KeyError):
        WorkspaceSpec.load("not-registered-ws")

    ws = hb.HeavenBase("existing-load-ws")
    assert isinstance(ws, hb.HeavenBase)
    assert hb.HeavenBase.load("existing-load-ws") is ws


def test_heavenbase_drop():
    """HeavenBase.drop(ws_id) should drop any live workspace by id."""
    ws = hb.HeavenBase("drop-live-ws", backends={"main": {"type": "inmem"}})
    assert hb.HeavenBase.drop("drop-live-ws") is True
    assert hb.HeavenBase.drop("drop-live-ws") is False
    assert ws.id == "drop-live-ws"


@pytest.mark.slow
def test_dropped_workspace_entities_do_not_reappear_in_a_new_workspace_of_the_same_id():
    """Dropping a workspace forgets its entity bindings, as a fresh process would."""

    class DroppedThing(hb.Entity):
        name = hb.field(hb.ShortText)

    ws = hb.HeavenBase("drop-rebind-ws", backends={"main": {"type": "inmem"}})
    ws.register(DroppedThing)
    assert DroppedThing.workspace_ids() == ["drop-rebind-ws"]
    assert hb.HeavenBase.drop("drop-rebind-ws") is True
    WorkspaceSpec.unregister("drop-rebind-ws")
    assert DroppedThing.workspace_ids() == []

    fresh = hb.HeavenBase("drop-rebind-ws", backends={"main": {"type": "inmem"}})
    try:
        assert "dropped-thing" not in fresh.entities
    finally:
        fresh.drop()
        WorkspaceSpec.unregister("drop-rebind-ws")


@pytest.mark.slow
def test_dropping_a_manifest_rebuilt_facade_forgets_the_original_class():
    """A drop through a facade rebuilt from a manifest still forgets the class first registered in the workspace."""

    class ManifestThing(hb.Entity):
        name = hb.field(hb.ShortText)

    ws_id = "drop-manifest-ws"
    first = hb.HeavenBase(ws_id, backends={"main": {"type": "inmem"}})
    first.register(ManifestThing)
    rebuilt = hb.HeavenBase.from_manifest(first.to_manifest())
    assert rebuilt.entities["manifest-thing"] is not ManifestThing
    assert hb.HeavenBase.drop(ws_id) is True
    WorkspaceSpec.unregister(ws_id)
    assert ManifestThing.workspace_ids() == []

    fresh = hb.HeavenBase(ws_id, backends={"main": {"type": "inmem"}})
    try:
        assert "manifest-thing" not in fresh.entities
    finally:
        fresh.drop()
        WorkspaceSpec.unregister(ws_id)


@pytest.mark.slow
def test_declared_entity_bindings_survive_a_workspace_drop():
    """A class declared for a workspace id through Entity.register registers again in a recreated workspace."""

    class DeclaredThing(hb.Entity):
        name = hb.field(hb.ShortText)

    class RegisteredThing(hb.Entity):
        name = hb.field(hb.ShortText)

    DeclaredThing.register("drop-declared-ws")
    ws = hb.HeavenBase("drop-declared-ws", backends={"main": {"type": "inmem"}})
    ws.register(RegisteredThing)
    assert "declared-thing" in ws.entities
    assert hb.HeavenBase.drop("drop-declared-ws") is True
    WorkspaceSpec.unregister("drop-declared-ws")
    assert DeclaredThing.workspace_ids() == ["drop-declared-ws"]
    assert RegisteredThing.workspace_ids() == []

    fresh = hb.HeavenBase("drop-declared-ws", backends={"main": {"type": "inmem"}})
    try:
        assert "declared-thing" in fresh.entities
        assert "registered-thing" not in fresh.entities
    finally:
        fresh.drop()
        WorkspaceSpec.unregister("drop-declared-ws")


def test_heavenbase_constructor_replaces_live_facade_only_after_compatible_ensure():
    """Compatible ensure may replace the facade without rewriting durable identity."""
    prior_active = hb.DEFAULT_CONTEXT.active_workspace_id
    current = hb.HeavenBase("create-only-owner-ws", backends={"main": {"type": "inmem"}})
    registered = WorkspaceSpec.load(current.id).to_dict()
    try:
        replacement = hb.HeavenBase("create-only-owner-ws", backends={"main": {"type": "inmem"}})

        assert replacement is not current
        assert WORKSPACES["create-only-owner-ws"] is replacement
        assert LIVE_WORKSPACES["create-only-owner-ws"] is replacement
        assert WorkspaceSpec.load(current.id).to_dict() == registered
        assert hb.DEFAULT_CONTEXT.active_workspace_id == prior_active
        assert hb.HeavenBase.load("create-only-owner-ws") is replacement
    finally:
        hb.HeavenBase.drop("create-only-owner-ws")


@pytest.mark.detail
def test_heavenbase_constructor_failure_preserves_process_owner_and_selection(monkeypatch):
    """A failed compatible ensure and a failed create must not disturb publication."""
    from heavenbase.workspace.setup import bootstrap as workspace_bootstrap

    prior_active = hb.DEFAULT_CONTEXT.active_workspace_id
    incumbent = hb.HeavenBase("bootstrap-failure-owner", backends={"main": {"type": "inmem"}})
    registered = WorkspaceSpec.load(incumbent.id).to_dict()
    failed = []

    def fail_late_bootstrap(workspace):
        if workspace.id.startswith("bootstrap-failure-"):
            failed.append(workspace)
            raise RuntimeError("late bootstrap failed")

    monkeypatch.setattr(workspace_bootstrap, "_register_pending_entities", fail_late_bootstrap)
    try:
        with pytest.raises(RuntimeError, match="late bootstrap failed"):
            hb.HeavenBase("bootstrap-failure-owner", backends={"main": {"type": "inmem"}})
        assert WORKSPACES["bootstrap-failure-owner"] is incumbent
        assert LIVE_WORKSPACES["bootstrap-failure-owner"] is incumbent
        assert WorkspaceSpec.load(incumbent.id).to_dict() == registered
        assert hb.DEFAULT_CONTEXT.active_workspace_id == prior_active

        with pytest.raises(RuntimeError, match="late bootstrap failed"):
            hb.HeavenBase("bootstrap-failure-new", backends={"main": {"type": "inmem"}})
        assert "bootstrap-failure-new" not in WORKSPACES
        assert "bootstrap-failure-new" not in LIVE_WORKSPACES
        with pytest.raises(KeyError):
            WorkspaceSpec.load("bootstrap-failure-new")
        assert hb.DEFAULT_CONTEXT.active_workspace_id == prior_active
        assert all(workspace._tracked is False for workspace in failed)
    finally:
        incumbent.drop()


@pytest.mark.detail
def test_heavenbase_registration_failure_leaves_no_publication(monkeypatch):
    """Failed durable registration must roll back both live publication caches."""
    target = "publication-failure-new"
    prior_active = hb.DEFAULT_CONTEXT.active_workspace_id
    register = WorkspaceSpec.register.__func__

    def fail_registration(cls, ws_id, config=None, manifest=None, extension_roots=None, *, construction=None, context=None):
        if ws_id == target:
            raise RuntimeError("durable registration failed")
        return register(
            cls,
            ws_id,
            config=config,
            manifest=manifest,
            extension_roots=extension_roots,
            construction=construction,
            context=context,
        )

    monkeypatch.setattr(WorkspaceSpec, "register", classmethod(fail_registration))
    with pytest.raises(RuntimeError, match="durable registration failed"):
        hb.HeavenBase(target, backends={"main": {"type": "inmem"}})
    assert target not in WORKSPACES
    assert target not in LIVE_WORKSPACES
    with pytest.raises(KeyError):
        WorkspaceSpec.load(target)
    assert hb.DEFAULT_CONTEXT.active_workspace_id == prior_active


@pytest.mark.detail
def test_detached_workspace_is_not_owned_by_class_level_drop():
    """Detached workspaces remain exclusively caller-owned."""
    prior_active = hb.DEFAULT_CONTEXT.active_workspace_id
    workspace = hb.HeavenBase("drop-unregistered-live-ws", detached=True, backends={"main": {"type": "inmem"}})
    assert "drop-unregistered-live-ws" not in WORKSPACES
    with pytest.raises(KeyError):
        WorkspaceSpec.load("drop-unregistered-live-ws")
    with pytest.raises(RuntimeError, match="cannot be activated"):
        workspace.activate()
    assert hb.DEFAULT_CONTEXT.active_workspace_id == prior_active
    assert hb.HeavenBase.drop("drop-unregistered-live-ws") is False
    workspace.drop()


@pytest.mark.detail
def test_internal_detached_construction_can_skip_pending_entity_registration():
    """Internal service workspaces can own their schema without replaying user-bound pending entities."""

    class PendingSkipThing(hb.Entity):
        name = hb.field(hb.ShortText)

    PendingSkipThing.register("pending-skip-ws")
    ws = _detached_without_pending("pending-skip-ws", backends={"main": {"type": "inmem"}})
    try:
        assert "pending-skip-thing" not in ws.entities
        ws.register(PendingSkipThing)
        assert "pending-skip-thing" in ws.entities
    finally:
        ws.drop()


def test_native_like_spans_line_breaks_and_matches_whole_values():
    """In-engine LIKE/ILIKE: `%` and `_` cross newlines, and a trailing newline still counts."""
    from heavenbase.execution.handlers.predicates import _like_pattern

    class _Config:
        def get(self, key, default=None):
            return default

    def like(pattern, value, *, ignore_case=False):
        return _like_pattern(pattern, None, _Config(), ignore_case=ignore_case).match(value)

    assert like("%second%", "first line\nsecond line")
    assert like("first%line", "first line\nsecond line")
    assert like("a_b", "a\nb")
    assert like("header%footer", "Header\r\nFOOTER", ignore_case=True)
    assert not like("header%footer", "Header\r\nFOOTER")
    assert not like("tail", "tail\n")
    assert like("tail_", "tail\n")
    assert like("100%", "100% sure") and not like("1.0", "100")

    ws = hb.HeavenBase("test-like-multiline", backends={"main": {"type": "inmem"}})

    class LikeDoc(hb.Entity):
        body = hb.field(hb.LongText)

    try:
        ws.register(LikeDoc)
        ws.upsert_many(LikeDoc, [{"object_id": "m1", "body": "Question?\nHint: use the view"}, {"object_id": "m2", "body": "tail\n"}])
        assert ws.query(LikeDoc).where(LikeDoc.body.like("%Hint%")).execute().ids == ["m1"]
        assert ws.query(LikeDoc).where(LikeDoc.body.ilike("question%VIEW")).execute().ids == ["m1"]
        assert ws.query(LikeDoc).where(LikeDoc.body.like("tail")).count().execute().scalar() == 0
    finally:
        ws.drop()


def test_empty_query_and_string_match_variants():
    """Empty queries and string/array/json filter variants should execute correctly."""
    ws = hb.HeavenBase("test-string-ops")

    class SearchDoc(hb.Entity):
        title = hb.field(hb.ShortText)
        body = hb.field(hb.LongText)
        tags = hb.field(hb.Array[hb.ShortText])
        attrs = hb.field(hb.Json)

    ws.register(SearchDoc)
    ws.upsert_many(
        SearchDoc,
        [
            {"object_id": "d1", "title": "Audio Reset", "body": "bluetooth audio reset guide", "tags": ["audio", "guide"], "attrs": {"tier": "Pro"}},
            {"object_id": "d2", "title": "oak desk", "body": "solid wood desk", "tags": ["furniture"], "attrs": {"tier": "Basic"}},
            {"object_id": "d3", "title": "Archive Copy", "body": "legacy notes", "tags": ["misc"], "attrs": {"tier": "Archive"}},
        ],
    )

    assert set(ws.query(SearchDoc).execute().ids) == {"d1", "d2", "d3"}
    assert set(ws.query_json(SearchDoc, {}).execute().ids) == {"d1", "d2", "d3"}
    assert ws.query(SearchDoc).where(SearchDoc.title.like("%audio%")).count().execute().scalar() == 0
    assert ws.query(SearchDoc).where(SearchDoc.title.contains("Reset")).count().execute().scalar() == 1
    assert ws.query(SearchDoc).where(SearchDoc.title.ilike("%audio%")).count().execute().scalar() == 1
    assert ws.query(SearchDoc).where(SearchDoc.title.wildcard("Audio*")).count().execute().scalar() == 1
    assert ws.query(SearchDoc).where(SearchDoc.body.regex(r"wood\s+desk")).count().execute().scalar() == 1
    assert ws.query(SearchDoc).where(SearchDoc.tags.array_contains("audio")).count().execute().scalar() == 1
    assert hb.ext.normalize_op("$array_contains") == "array_contains"
    with pytest.raises(LookupError, match="Unknown operation '\\$custom'"):
        hb.ext.normalize_op("$custom")
    assert ws.query_json(SearchDoc, {"filter": {"title": {"contains": "Reset"}}}).count().execute().scalar() == 1
    assert ws.query_json(SearchDoc, {"filter": {"tags": {"$array_contains": "guide"}}}).count().execute().scalar() == 1
    assert ws.query_json(SearchDoc, {"filter": {"tags": {"contains": "guide"}}}).count().execute().scalar() == 1
    assert ws.query(SearchDoc).where({"tags": {"contains": "audio"}}).count().execute().scalar() == 1
    assert ws.query(SearchDoc).where(SearchDoc.attrs["tier"].contains("ro")).count().execute().scalar() == 1
    with pytest.raises(LookupError):
        ws.query(SearchDoc).where(SearchDoc.tags.contains("audio")).count().execute()


def test_query_json_supports_nested_boolean_filters():
    """JSON filters should support nested and/or/not boolean trees."""
    ws = hb.HeavenBase("test-nested-json")

    class NestedDoc(hb.Entity):
        title = hb.field(hb.ShortText)
        body = hb.field(hb.LongText)
        score = hb.field(hb.Float)

    ws.register(NestedDoc)
    ws.upsert_many(
        NestedDoc,
        [
            {"object_id": "n1", "title": "Audio Primer", "body": "bluetooth audio setup", "score": 9},
            {"object_id": "n2", "title": "Desk Guide", "body": "oak desk build", "score": 7},
            {"object_id": "n3", "title": "Archive Note", "body": "bluetooth audio archive", "score": 8},
        ],
    )

    filter_spec = {
        "$and": [
            {"$or": [{"title": {"$ilike": "%audio%"}}, {"body": {"$regex": r"oak\s+desk"}}]},
            {"$not": {"title": {"$wildcard": "Archive*"}}},
            {"score": {"$gte": 7}},
        ]
    }
    frame = ws.query_json(
        NestedDoc,
        {
            "filter": filter_spec,
            "select": ["title", "score"],
        },
    ).execute()
    direct = ws.query(NestedDoc).where(filter_spec).select("title", "score").execute()

    assert frame.ids == ["n1", "n2"]
    assert direct.ids == frame.ids
    assert frame.rows() == [
        {"object_id": "n1", "title": "Audio Primer", "score": 9.0},
        {"object_id": "n2", "title": "Desk Guide", "score": 7.0},
    ]


@pytest.mark.detail
def test_hybrid_vector_query_preserves_near_order_after_nested_filter():
    """Hybrid near plus nested filter queries should preserve near ranking order."""
    ws = hb.HeavenBase("test-hybrid-query")

    class HybridDoc(hb.Entity):
        title = hb.field(hb.ShortText)
        body = hb.field(hb.LongText)
        score = hb.field(hb.Float)
        emb = hb.field(hb.Vector[2])

    ws.register(HybridDoc)
    ws.upsert_many(
        HybridDoc,
        [
            {"object_id": "h1", "title": "Audio Repair", "body": "bluetooth audio reset", "score": 9, "emb": [1, 0]},
            {"object_id": "h2", "title": "Desk Setup", "body": "oak desk audio shelf", "score": 7, "emb": [0.9, 0.1]},
            {"object_id": "h3", "title": "Archive Audio", "body": "bluetooth audio archive", "score": 10, "emb": [1, 0]},
            {"object_id": "h4", "title": "Far Result", "body": "networking only", "score": 8, "emb": [0, 1]},
        ],
    )

    frame = ws.query_json(
        HybridDoc,
        {
            "filter": {
                "$and": [
                    {"$or": [{"title": {"$ilike": "%audio%"}}, {"body": {"$regex": r"oak\s+desk"}}]},
                    {"$not": {"title": {"$wildcard": "Archive*"}}},
                    {"score": {"$gte": 7}},
                ]
            },
            "near": {"field": "emb", "query": [1, 0], "top_k": 3},
            "select": ["title", "score"],
        },
    ).execute()

    assert frame.ids == ["h1", "h2"]
    assert frame.rows()[0]["title"] == "Audio Repair"
    assert frame.rows()[0]["score"] >= frame.rows()[1]["score"]


def test_json_entity_definition_registers_schema_routes_and_computes():
    """JSON entity definitions should register fields, routes, defaults, and computes."""
    ws = hb.HeavenBase("test-json-entity", backends={"main": {"type": "inmem"}})

    JsonNote = ws.register(json_note_def(), computes={"embed_json_note": embed_json_note, "identity": identity})
    ws.upsert("json-note", {"object_id": "n1", "title": "Catalog quickstart", "body": "schema rows explain the workspace"})
    ws.upsert("json-note", {"object_id": "n2", "title": "Vector search", "body": "vector search ranks notes"})

    frame = (
        ws.query(JsonNote)
        .where(JsonNote.body.match("schema") | JsonNote.body.match("vector"))
        .near(JsonNote.emb, [0.0, 1.0], top_k=2)
        .select("title", "status", "score")
        .execute()
    )
    fields = ws.query(hb.MetaSchema).where(hb.MetaSchema.subject_id == "json-note").where(hb.MetaSchema.kind == "field").execute().rows()
    routes = ws.query(hb.MetaSchema).where(hb.MetaSchema.subject_id == "json-note").where(hb.MetaSchema.kind == "storage").execute().rows()
    field_rows = {row["field"]: row for row in fields}
    catalog_row = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "json-note").where(hb.Catalog.target_id == "n1").execute().rows()[0]

    assert frame.ids[0] == "n2"
    assert ws.get("n1")["status"] == "draft"
    assert ws.get("n1", entity="json-note")["title"] == "Catalog quickstart"
    assert ws.get("n1", entity=JsonNote)["emb"] == [1.0, 0.0]
    assert catalog_row["name"] == "Catalog quickstart"
    assert catalog_row["desc"] == "schema rows explain the workspace"
    assert field_rows["title"]["tags"] == ["display"]
    assert field_rows["status"]["meta"]["has_default"] is True
    assert any(row["field"] == "emb" and row["strategy"] == hb.VectorIndex.identifier and row["source"] == "json" for row in routes)


@pytest.mark.fast
def test_manual_metaschema_entity_definition_establishes_a_usable_class():
    """An active definition-shaped Entity row should enter through schema authority."""
    ws = _detached_without_pending("metaschema-entity-ingress")

    class ManualMetaNote(hb.Entity):
        identifier = "manual-meta-note"

        title = hb.field(hb.ShortText)

    row_id = "manual:entity-definition"
    ws.upsert(
        hb.MetaSchema,
        {
            "object_id": row_id,
            "kind": "entity",
            "subject_id": "intentionally-noncanonical-subject",
            "name": "Intentionally noncanonical projection",
            "meta": ManualMetaNote.schema().to_dict(),
        },
    )

    established = ws.entities["manual-meta-note"]
    assert established is not ManualMetaNote
    assert established.schema().to_dict() == ManualMetaNote.schema().to_dict()
    assert "manual-meta-note" in ws.storage_plans
    assert ws.get(row_id, entity=hb.MetaSchema)["subject_id"] == "intentionally-noncanonical-subject"
    assert ws.get("entity:manual-meta-note", entity=hb.MetaSchema)["subject_id"] == "manual-meta-note"

    ws.upsert("manual-meta-note", {"object_id": "n1", "title": "usable"})
    assert ws.get("n1", entity="manual-meta-note")["title"] == "usable"


@pytest.mark.fast
def test_manual_metaschema_definition_paths_preserve_existing_class_authority():
    """Set may establish an absent class, while later manual rows never replace one."""
    ws = _detached_without_pending("metaschema-entity-divergence")

    class SetDefinedNote(hb.Entity):
        identifier = "set-defined-note"

        title = hb.field(hb.ShortText)

    row_id = "manual:set-definition"
    ws.upsert(
        hb.MetaSchema,
        {
            "object_id": row_id,
            "kind": "annotation",
            "subject_id": "set-defined-note",
            "meta": SetDefinedNote.schema().to_dict(),
        },
    )
    assert "set-defined-note" not in ws.entities

    ws.set(row_id, {"kind": "entity"}, entity=hb.MetaSchema)
    established = ws.entities["set-defined-note"]
    original_plan = ws.storage_plans["set-defined-note"]

    divergent = SetDefinedNote.schema().to_dict()
    divergent["fields"]["other"] = {
        "name": "other",
        "dtype": hb.ShortText().to_dict(),
        "pk": False,
        "required": False,
        "default": None,
        "has_default": False,
        "desc": "",
        "tags": [],
        "examples": [],
    }
    ws.upsert(
        hb.MetaSchema,
        {
            "object_id": "manual:divergent-definition",
            "kind": "entity",
            "subject_id": "different-search-subject",
            "meta": divergent,
        },
    )

    assert ws.entities["set-defined-note"] is established
    assert ws.storage_plans["set-defined-note"] is original_plan
    assert "other" not in established.schema().fields
    assert ws.get("manual:divergent-definition", entity=hb.MetaSchema)["meta"]["fields"]["other"]["name"] == "other"


@pytest.mark.fast
def test_manual_metaschema_entity_ingress_is_explicit_and_scope_safe():
    """Only active definition rows establish classes, and closed scopes cannot expand."""
    ws = _detached_without_pending("metaschema-entity-scope")

    class ScopedMetaNote(hb.Entity):
        identifier = "scoped-meta-note"

        title = hb.field(hb.ShortText)

    inactive_id = "manual:inactive-definition"
    ws.upsert(
        hb.MetaSchema,
        {
            "object_id": inactive_id,
            "kind": "entity",
            "active": False,
            "meta": ScopedMetaNote.schema().to_dict(),
        },
    )
    assert "scoped-meta-note" not in ws.entities
    assert ws.get(inactive_id, entity=hb.MetaSchema)["active"] is False

    metadata_id = "manual:entity-annotation"
    ws.upsert(
        hb.MetaSchema,
        {
            "object_id": metadata_id,
            "kind": "entity",
            "subject_id": "annotation-only",
            "meta": {"note": "not a class definition"},
        },
    )
    assert "annotation-only" not in ws.entities
    assert ws.get(metadata_id, entity=hb.MetaSchema)["meta"]["note"] == "not a class definition"

    denied_id = "manual:scoped-definition"
    with pytest.raises(PermissionError, match="active entity scope"):
        ws._crud.upsert(
            hb.MetaSchema,
            {
                "object_id": denied_id,
                "kind": "entity",
                "meta": ScopedMetaNote.schema().to_dict(),
            },
            allowed_entities=frozenset({"sys-metaschema"}),
        )
    assert "scoped-meta-note" not in ws.entities
    assert ws.get(denied_id, entity=hb.MetaSchema) is None

    ws._crud.upsert(
        hb.MetaSchema,
        {
            "object_id": denied_id,
            "kind": "entity",
            "meta": ScopedMetaNote.schema().to_dict(),
        },
        allowed_entities=frozenset({"sys-metaschema", "scoped-meta-note"}),
    )
    assert "scoped-meta-note" in ws.entities


@pytest.mark.fast
def test_invalid_manual_metaschema_definition_fails_before_row_write():
    """A clear but invalid class-definition command must not become inert metadata."""
    ws = _detached_without_pending("metaschema-invalid-entity")
    row_id = "manual:invalid-definition"

    with pytest.raises(LookupError):
        ws.upsert(
            hb.MetaSchema,
            {
                "object_id": row_id,
                "kind": "entity",
                "meta": {
                    "entity_id": "invalid-meta-note",
                    "fields": {
                        "title": {
                            "dtype": "logical-type-that-does-not-exist",
                        }
                    },
                },
            },
        )

    assert "invalid-meta-note" not in ws.entities
    assert ws.get(row_id, entity=hb.MetaSchema) is None


@pytest.mark.detail
def test_json_entity_definition_accepts_nested_derive_compute_object():
    """JSON derives should accept the same compute object shape as field computes."""
    ws = hb.HeavenBase("test-json-nested-derive", backends={"main": {"type": "inmem"}})
    spec = json_note_def(entity_id="json-nested-derive-note", name="JsonNestedDeriveNote")
    spec["derive"] = [
        {"target": "catalog.name", "compute": {"fn": "identity", "inputs": ["title"]}},
        {"target": "catalog.desc", "compute": {"fn": "identity", "fields": ["body"]}},
    ]

    ws.register(spec, computes={"embed_json_note": embed_json_note, "identity": identity})
    object_id = ws.upsert("json-nested-derive-note", {"title": "Nested derive", "body": "same shape"})
    catalog_row = ws.query(hb.Catalog).where(hb.Catalog.target_entity == "json-nested-derive-note").execute().rows()[0]

    assert object_id == hb.utils.hash_id("json-nested-derive-note", "Nested derive")
    assert catalog_row["target_id"] == object_id
    assert catalog_row["name"] == "Nested derive"
    assert catalog_row["desc"] == "same shape"


def test_schema_definition_storage_accepts_strategy_class():
    """Python-authored schema definitions should canonicalize strategy classes in storage rules."""
    spec = json_note_def("json-class-strategy-note", "JsonClassStrategyNote")
    spec["fields"]["emb"]["storage"]["strategy"] = hb.VectorIndex
    entity = hb.Entity.from_schema(spec, computes={"embed_json_note": embed_json_note, "identity": identity})

    assert entity._hb_specs["emb"].stores[0].strategy == hb.VectorIndex.identifier
    assert entity._hb_json_spec["fields"]["emb"]["storage"]["strategy"] == hb.VectorIndex.identifier


@pytest.mark.detail
def test_class_and_json_entity_definitions_are_equivalent():
    """Equivalent class and JSON entity definitions should compile to the same schema."""

    class JsonEquivNote(hb.Entity):
        """JSON-authored note entity."""

        identifier = "json-equiv-note"
        __tags__ = ["docs", "json"]
        object_id = hb.field(hb.Identifier, desc="Stable note id").pk()
        title = hb.field(hb.ShortText, desc="Display title", tags=["display"], examples=["Catalog quickstart"])
        body = hb.field(hb.LongText, desc="Searchable body text", tags=["search"]).required()
        status = hb.field(hb.ShortText, desc="Workflow status").default("draft")
        emb = hb.field(hb.Vector[2], desc="Tiny in-memory embedding").store(to="main", strategy=hb.VectorIndex).compute(embed_json_note, inputs=["body"])

    equiv_def = json_note_def("json-equiv-note", "JsonEquivNote")
    equiv_def["fields"]["emb"]["storage"]["reason"] = "field"
    JsonNote = hb.Entity.from_schema(equiv_def, computes={"embed_json_note": embed_json_note, "identity": identity})

    assert JsonEquivNote.schema().to_dict() == JsonNote.schema().to_dict()
    assert JsonEquivNote.schema().entity_id == "json-equiv-note"
    assert JsonNote.schema().entity_id == "json-equiv-note"
    assert JsonEquivNote._hb_specs["emb"].stores == JsonNote._hb_specs["emb"].stores
    assert JsonNote._hb_specs["emb"].computes[0].inputs == ["body"]


@pytest.mark.detail
def test_json_entity_definition_requires_registered_compute():
    """JSON entity definitions should reject unknown compute function names."""
    with pytest.raises(KeyError, match="missing_embed"):
        hb.Entity.from_schema(
            {
                "entity_id": "bad-note",
                "name": "BadNote",
                "fields": {"body": "long-text", "emb": {"type": "vector[2]", "compute": {"fn": "missing_embed", "inputs": ["body"]}}},
            }
        )


@pytest.mark.fast
def test_json_entity_definition_resolves_compute_and_query_hooks_from_the_registry():
    """Schema definitions should resolve both hook families published through ``hb.ext``."""
    hb.ext.register_compute("core_embed_json_note", embed_json_note, replace=True)
    hb.ext.register_compute("core_identity", identity, replace=True)
    hb.ext.register_query_compute("core_query_embed_json_note", embed_json_note, replace=True)

    spec = json_note_def("registry-compute-note", "RegistryComputeNote")
    spec["fields"]["emb"]["compute"]["fn"] = f"{embed_json_note.__module__}.{embed_json_note.__qualname__}"  # serialized reference
    spec["fields"]["emb"]["query_compute"] = f"{embed_json_note.__module__}.{embed_json_note.__qualname__}"
    spec["derive"] = [{"target": "catalog.name", "compute": "core_identity", "inputs": ["title"]}]  # registered identifier
    entity = hb.Entity.from_schema(spec)  # no explicit computes mapping

    assert "core_embed_json_note" in hb.ext.registered_computes()
    assert "core_query_embed_json_note" in hb.ext.registered_query_computes()
    assert entity._hb_specs["emb"].computes[0].fn is embed_json_note
    assert entity._hb_specs["emb"].query_computes[0].fn is embed_json_note
    assert entity._hb_derives["catalog.name"].computes[0].fn is identity


@pytest.mark.fast
@pytest.mark.parametrize(
    ("register", "kind"),
    [
        (hb.ext.register_compute, "entity_compute"),
        (hb.ext.register_query_compute, "entity_query_compute"),
    ],
)
def test_compute_reregistration_reconciles_aliases(register, kind):
    """Replacing one callable should atomically publish its requested aliases."""
    service = ModuleService(Registry("system-modules"))

    assert register("core_alias_hook", embed_json_note, aliases=("old-hook",), replace=True, resolver=service) is embed_json_note
    assert service.inspect(kind, "old-hook")

    assert register("core_alias_hook", embed_json_note, aliases=("new-hook",), replace=True, resolver=service) is embed_json_note
    assert not service.inspect(kind, "old-hook")
    assert service.inspect(kind, "new-hook")[0]["identifier"] == "core_alias_hook"
    assert service.inspect(kind, "core_alias_hook")[0]["aliases"] == ["new-hook"]


@pytest.mark.fast
@pytest.mark.parametrize(
    ("register", "kind"),
    [
        (hb.ext.register_compute, "entity_compute"),
        (hb.ext.register_query_compute, "entity_query_compute"),
    ],
)
def test_compute_reregistration_cannot_claim_an_external_owner(register, kind):
    """Callable identity should not bypass the Registry installation owner."""
    service = ModuleService(Registry("system-modules"))
    service.register(
        kind,
        "core_owned_hook",
        {
            "name": "Externally owned hook",
            "desc": "",
            "source": "python-import",
            "target": f"{embed_json_note.__module__}:{embed_json_note.__qualname__}",
            "active": True,
            "meta": {"schema_version": 1},
        },
        aliases=("external-hook",),
        coordinate="external/compute-hooks",
    )

    with pytest.raises(ValueError, match="belongs to 'external/compute-hooks'"):
        register("core_owned_hook", embed_json_note, aliases=("claimed-hook",), replace=True, resolver=service)

    assert service.inspect(kind, "external-hook")
    assert not service.inspect(kind, "claimed-hook")


@pytest.mark.detail
def test_json_entity_definition_rejects_unsafe_names_and_duplicates():
    """JSON entity definitions should reject unsafe ids and duplicate field names."""
    with pytest.raises(ValueError, match="entity_id, not id"):
        hb.Entity.from_schema({"id": "legacy-note", "name": "LegacyNote", "fields": {"title": "short-text"}})
    with pytest.raises(ValueError, match="entity_id"):
        hb.Entity.from_schema({"entity_id": "../bad", "name": "Bad", "fields": {"title": "short-text"}})
    with pytest.raises(ValueError, match="Duplicate field"):
        hb.Entity.from_schema(
            {
                "entity_id": "duplicate-field-note",
                "name": "DuplicateFieldNote",
                "fields": [{"name": "title", "type": "short-text"}, {"name": "title", "type": "long-text"}],
            }
        )


def test_object_id_contract_rejects_invalid_schema_shapes():
    """Entity schemas should keep one fixed Identifier object_id field."""
    with pytest.raises(TypeError, match="Identifier"):

        class BadObjectIdType(hb.Entity):
            object_id = hb.field(hb.ShortText)
            name = hb.field(hb.ShortText)

    with pytest.raises(ValueError, match="Only fixed object_id"):

        class BadPkField(hb.Entity):
            key = hb.field(hb.Identifier).pk()
            name = hb.field(hb.ShortText)

    class EntityIdField(hb.Entity):
        entity_id = hb.field(hb.Identifier)
        name = hb.field(hb.ShortText)

    assert EntityIdField.schema().get("entity_id").pk is False
    assert EntityIdField.schema().pk_name == "object_id"


def test_auto_id_requires_catalog_name_or_object_id_when_missing_id():
    """Rows without explicit object_id values need catalog.name identity or an object_id compute."""
    ws = hb.HeavenBase("test-auto-id-source")

    class NoNameAutoId(hb.Entity):
        title = hb.field(hb.ShortText)

    assert "name" not in NoNameAutoId.schema().fields
    ws.register(NoNameAutoId)
    with pytest.raises(ValueError, match="catalog.name"):
        ws.upsert(NoNameAutoId, {"title": "No name"})


def test_entity_class_rejects_unsafe_entity_id():
    """Class-authored entities should reject unsafe entity identifiers."""
    with pytest.raises(ValueError, match="entity_id"):

        class BadEntityId(hb.Entity):
            identifier = "bad/path"

            title = hb.field(hb.ShortText)


def test_query_guards_reject_expensive_or_malformed_inputs():
    """Query guards should reject malformed regexes and invalid pagination values."""
    ws = hb.HeavenBase("query-guard-test")

    class GuardDoc(hb.Entity):
        title = hb.field(hb.ShortText)
        body = hb.field(hb.LongText)
        emb = hb.field(hb.Vector[2])

    ws.register(GuardDoc)
    ws.upsert(GuardDoc, {"object_id": "g1", "title": "Audio", "body": "safe text", "emb": [1, 0]})

    with pytest.raises(ValueError, match="Invalid regex"):
        ws.query(GuardDoc).where(GuardDoc.body.regex("(")).count().execute()
    with pytest.raises(ValueError, match="Unsafe regex"):
        ws.query(GuardDoc).where(GuardDoc.body.regex(r"(a+)+$")).count().execute()
    with pytest.raises(ValueError, match="top_k"):
        ws.query(GuardDoc).near(GuardDoc.emb, [1, 0], top_k=0)
    with pytest.raises(ValueError, match="limit"):
        ws.query(GuardDoc).limit(-1)


def test_registry_mirror_publish_caches_and_invalidates_by_content(monkeypatch):
    """Registry mirror writes are skipped only while their generated content is unchanged."""
    ws = hb.HeavenBase("test-mirror-digest", backends={"main": {"type": "inmem"}})
    service = ws._metaschema
    writes = []
    original = type(service).write_meta_rows

    def counting_write(self, rows, **kwargs):
        writes.append(list(rows))
        return original(self, rows, **kwargs)

    monkeypatch.setattr(type(service), "write_meta_rows", counting_write)

    class MirrorProbeA(hb.Entity):
        name = hb.field(hb.ShortText)

    class MirrorProbeB(hb.Entity):
        name = hb.field(hb.ShortText)

    ws.register(MirrorProbeA)
    ws.register(MirrorProbeB)
    # construction already published the registry mirror, so both registers
    # must skip it (no batch carries the workspace root row again) while
    # still publishing their own entity schema rows
    mirror_writes = [batch for batch in writes if any(row.get("kind") == "workspace" for row in batch)]
    schema_writes = [batch for batch in writes if any(row.get("kind") == "entity" for row in batch)]
    assert not mirror_writes
    assert len(schema_writes) == 2
    mirror_rows = ws.query(hb.MetaSchema).where(hb.MetaSchema.kind == "workspace").execute().rows()
    assert len(mirror_rows) == 1  # published once at construction, still present

    writes.clear()
    changed_rows = [dict(row, desc="Changed workspace root") if row.get("kind") == "workspace" else row for row in service.registry.workspace_rows()]
    changed = (changed_rows, hb.utils.sha256hash(changed_rows))
    monkeypatch.setattr(type(service.registry), "workspace_rows_and_digest", lambda self: changed)
    service.publish_workspace_rows()
    service.publish_workspace_rows()

    mirror_writes = [batch for batch in writes if any(row.get("kind") == "workspace" for row in batch)]
    assert len(mirror_writes) == 1
    mirror_rows = ws.query(hb.MetaSchema).where(hb.MetaSchema.kind == "workspace").execute().rows()
    assert mirror_rows[0]["desc"] == "Changed workspace root"
