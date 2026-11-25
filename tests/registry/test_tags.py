"""Open-world Tag definition, claim, and static inspection contracts."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import importlib
import json
import sys

import pytest

from heavenbase import __version__
from heavenbase.modules import ModuleResolver, ModuleService
from heavenbase.modules.discovery import discover_package_modules
from heavenbase.modules.tags import tag as inspect_tag
from heavenbase.modules.tags import tag_definition, tags
from heavenbase.utils.registry import Registry
from heavenbase.workspace.metaschema.projection import _MetaSchemaRegistryRows

pytestmark = pytest.mark.fast

BUILTIN_TAG_IDENTIFIERS = (
    "agent",
    "aggregate",
    "aggregate-input-modes",
    "aggregate-modes",
    "atomic-upsert",
    "backend-availability",
    "benchmark-suites",
    "binary-storage",
    "capsule",
    "compare-upsert",
    "database",
    "database-blob-native",
    "database-create",
    "database-drop",
    "database-dynamic-typing",
    "database-json-native",
    "database-local-path",
    "executable",
    "filter",
    "filter-domains",
    "gram",
    "graph",
    "graph-endpoint-materialization",
    "graph-endpoint-pushdown",
    "graph-incoming-lookup-native",
    "graph-traversal-native",
    "indexable-types",
    "llm",
    "mcp",
    "memory",
    "near",
    "near-filter-modes",
    "nested-filter",
    "portable",
    "prompt",
    "row-lookup",
    "scalar-index",
    "schema",
    "search",
    "session",
    "side",
    "skill",
    "sql",
    "storage-roles",
    "strategy-adapters",
    "toolkit",
    "translation",
    "unsupported-storage-types",
    "vector",
    "workspace-database-namespace",
)


def _record(
    *,
    definition=None,
    tags=None,
    source: str = "inline",
    target: str = "definition",
) -> dict:
    """Return one plain module record for focused Tag contracts."""
    meta = {"schema_version": 1, "definition": {"ok": True} if definition is None else definition}
    if tags is not None:
        meta["tags"] = tags
    return {
        "name": "Tag fixture",
        "desc": "Focused Tag contract fixture.",
        "source": source,
        "target": target,
        "active": True,
        "meta": meta,
    }


def _definition(
    service: ModuleService,
    identifier: str,
    schema: Mapping,
    *,
    default=...,
    subjects=("test_kind",),
    layers=("static",),
    subject_policy="exact",
    aliases=(),
) -> None:
    """Register one inert Tag definition through the ordinary module protocol."""
    definition = {
        "schema": dict(schema),
        "subjects": list(subjects),
        "subject_policy": subject_policy,
        "layers": list(layers),
    }
    if default is not ...:
        definition["default"] = default
    service.register(
        "tag",
        identifier,
        _record(definition=definition),
        aliases=aliases,
        coordinate=f"test/tag/{identifier}",
    )


def _subject(service: ModuleService, identifier: str = "subject", *, tags=None, kind: str = "test_kind", aliases=()) -> None:
    """Register one inert subject record with optional raw claims."""
    service.register(
        kind,
        identifier,
        _record(tags={} if tags is None else tags),
        aliases=aliases,
        coordinate=f"test/{kind}/{identifier}",
    )


def test_manifest_preserves_detached_raw_tags_and_rejects_only_non_mapping_shape():
    service = ModuleService(Registry("system-modules"))
    raw = {"future tag": {"nested": [1, None, True]}}

    _subject(service, tags=raw)
    row = service.inspect("test_kind", "subject")[0]
    raw["future tag"]["nested"][0] = 99

    assert row["record"]["meta"]["tags"] == {"future tag": {"nested": [1, None, True]}}
    unresolved = service.tag("test_kind", "subject", "future tag")
    assert unresolved is not None
    assert unresolved.tag == "future tag"
    assert unresolved.status == "unresolved"
    assert unresolved.value == {"nested": (1, None, True)}
    assert unresolved.raw_keys == ("future tag",)

    with pytest.raises(TypeError, match=r"meta\.tags must be a string-keyed mapping"):
        service.register("test_kind", "invalid-tags", _record(tags=["not", "a", "mapping"]))
    assert service.inspect("test_kind", "invalid-tags") == ()


def test_definition_alias_defaults_statuses_and_ambiguity_are_snapshot_bound():
    service = ModuleService(Registry("system-modules"))
    _definition(service, "enabled", {"type": "boolean"}, default=False, aliases=("legacy enabled",))
    _definition(service, "roles", {"type": "set", "items": {"type": "string"}}, default=["vector", "row"])
    _definition(service, "strict-only", {"type": "boolean"}, default=False, subjects=("other_kind",))
    _definition(
        service,
        "open-category",
        {"type": "boolean"},
        default=False,
        subjects=("test_kind",),
        subject_policy="category-open",
    )
    _subject(
        service,
        tags={
            "enabled": True,
            "legacy enabled": False,
            "strict-only": True,
            "open-category": True,
        },
    )

    view = service.tags("test_kind", "subject")

    assert view.subject == ("test_kind", "subject")
    assert view.publication == service.catalog.publication
    assert view.revision == service.catalog.revision
    assert view.snapshot
    assert view["enabled"] is view["legacy enabled"]
    assert view["enabled"].status == "ambiguous"
    assert view["enabled"].raw_keys == ("enabled", "legacy enabled")
    assert view["roles"].status == "resolved"
    assert view["roles"].source == "default"
    assert view["roles"].value == ("row", "vector")
    assert view["strict-only"].status == "invalid"
    assert "subject kind" in view["strict-only"].diagnostics[0]
    assert view["open-category"].status == "resolved"
    assert view.raw["enabled"] is True


@pytest.mark.parametrize(
    ("identifier", "schema", "value", "expected"),
    (
        ("boolean-value", {"type": "boolean"}, True, True),
        ("string-value", {"type": "string", "pattern": "[a-z]+", "min_length": 2, "max_length": 5}, "tag", "tag"),
        ("integer-value", {"type": "integer", "minimum": 1, "maximum": 4}, 3, 3),
        ("number-value", {"type": "number", "minimum": 1.5, "maximum": 4}, 2.5, 2.5),
        ("identifier-value", {"type": "identifier"}, "logical-type", "logical-type"),
        ("enum-value", {"type": "enum", "values": ["scan", "native"]}, "native", "native"),
        ("list-value", {"type": "list", "items": {"type": "integer"}}, [2, 1], (2, 1)),
        ("set-value", {"type": "set", "items": {"type": "integer"}}, [2, 1], (1, 2)),
        ("mapping-value", {"type": "mapping", "key_pattern": "[a-z]+", "values": {"type": "boolean"}}, {"z": True, "a": False}, {"a": False, "z": True}),
        (
            "object-value",
            {
                "type": "object",
                "properties": {
                    "supported": {"type": "set", "items": {"type": "identifier"}},
                    "defaults": {"type": "set", "items": {"type": "identifier"}},
                },
                "required": ["supported", "defaults"],
                "constraints": [{"kind": "subset", "field": "defaults", "of": "supported"}],
            },
            {"supported": ["integer", "float"], "defaults": ["integer"]},
            {"defaults": ("integer",), "supported": ("float", "integer")},
        ),
        ("json-value", {"type": "json"}, {"z": [2], "a": True}, {"a": True, "z": (2,)}),
    ),
)
def test_declarative_schema_grammar_resolves_canonical_values(identifier, schema, value, expected):
    service = ModuleService(Registry("system-modules"))
    _definition(service, identifier, schema)
    _subject(service, tags={identifier: value})

    result = service.tag("test_kind", "subject", identifier)

    assert result is not None
    assert result.status == "resolved"
    assert result.value == expected


@pytest.mark.parametrize(
    ("identifier", "schema", "value", "message"),
    (
        ("strict-boolean", {"type": "boolean"}, "true", "must be Boolean"),
        ("strict-integer", {"type": "integer"}, True, "must be an integer"),
        ("no-null", {"type": "json"}, None, "must not be null"),
        ("unique-set", {"type": "set", "items": {"type": "integer"}}, [1, 1], "duplicate set item"),
        (
            "closed-object",
            {"type": "object", "properties": {}, "additional_properties": False},
            {"extra": True},
            "unsupported properties",
        ),
        (
            "subset-object",
            {
                "type": "object",
                "properties": {
                    "supported": {"type": "set", "items": {"type": "identifier"}},
                    "defaults": {"type": "set", "items": {"type": "identifier"}},
                },
                "required": ["supported", "defaults"],
                "constraints": [{"kind": "subset", "field": "defaults", "of": "supported"}],
            },
            {"supported": ["integer"], "defaults": ["float"]},
            "must be a subset",
        ),
    ),
)
def test_invalid_typed_values_remain_stored_and_local(identifier, schema, value, message):
    service = ModuleService(Registry("system-modules"))
    _definition(service, identifier, schema)
    _subject(service, tags={identifier: value})

    result = service.tag("test_kind", "subject", identifier)

    assert result is not None
    assert result.status == "invalid"
    assert result.value is None
    assert message in result.diagnostics[-1]
    assert service.inspect("test_kind", "subject")[0]["record"]["meta"]["tags"][identifier] == value


def test_list_unique_keys_and_reference_targets_are_checked_without_dependencies():
    service = ModuleService(Registry("system-modules"))
    _definition(
        service,
        "adapters",
        {
            "type": "list",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "identifier"},
                    "target": {"type": "reference", "kind": "adapter", "state": "active"},
                },
                "required": ["name", "target"],
            },
            "unique_keys": [["name", "target"]],
        },
    )
    _subject(service, tags={"adapters": [{"name": "main", "target": "missing"}]})

    missing = service.tag("test_kind", "subject", "adapters")
    assert missing is not None
    assert missing.status == "unresolved"
    assert "references missing adapter:missing" in missing.diagnostics[0]

    _subject(service, "missing", kind="adapter")
    present = service.tag("test_kind", "subject", "adapters")
    assert present is not None
    assert present.status == "resolved"
    assert present.snapshot != missing.snapshot

    service.retire("adapter", "missing")
    inactive = service.tag("test_kind", "subject", "adapters")
    assert inactive is not None
    assert inactive.status == "unresolved"
    assert "references inactive adapter:missing" in inactive.diagnostics[0]


def test_malformed_definition_and_definition_lifecycle_affect_only_derived_views():
    service = ModuleService(Registry("system-modules"))
    _subject(service, tags={"late-tag": True, "other raw": 7})
    _subject(service, "empty")

    before = service.tags("test_kind", "subject")
    assert before["late-tag"].status == "unresolved"
    assert before["other raw"].status == "unresolved"

    _definition(service, "late-tag", {"type": "boolean"}, default="false")
    invalid_definition = service.tag_definition("late-tag")
    invalid_claim = service.tag("test_kind", "subject", "late-tag")
    assert invalid_definition is not None
    assert invalid_definition.status == "invalid"
    assert invalid_claim is not None
    assert invalid_claim.status == "invalid"
    assert service.tag("test_kind", "empty", "late-tag") is None
    assert service.tag("test_kind", "subject", "other raw").status == "unresolved"

    service.unregister("tag", "late-tag")
    after = service.tag("test_kind", "subject", "late-tag")
    assert after is not None
    assert after.status == "unresolved"
    assert after.raw is True
    assert after.snapshot != before.snapshot


def test_overlay_validates_known_no_default_tags_and_preserves_soft_reference_status():
    service = ModuleService(Registry("system-modules"))
    _definition(
        service,
        "constructed-target",
        {"type": "reference", "kind": "adapter", "state": "active"},
        layers=("construction", "live"),
    )
    _subject(service)
    static = service.tags("test_kind", "subject")

    publisher_raw = {"nested": [{"ok": True}]}
    construction = static.overlay(
        {"constructed-target": "missing", "publisher raw": publisher_raw},
        source="construction",
        instance="ctx:ws:backend",
    )
    publisher_raw["nested"][0]["ok"] = False
    assert construction["constructed-target"].definition is not None
    assert construction["constructed-target"].status == "unresolved"
    assert construction["constructed-target"].source == "construction"
    assert construction["constructed-target"].instance == "ctx:ws:backend"
    assert construction["publisher raw"].status == "unresolved"
    assert construction["publisher raw"].value == {"nested": ({"ok": True},)}
    assert construction.raw == static.raw == {}
    with pytest.raises(TypeError):
        construction["publisher raw"].value["nested"] = ()
    with pytest.raises(TypeError, match="non-JSON"):
        static.overlay({"publisher raw": {"invalid": {1}}}, source="live", instance="ctx:ws:backend")

    live = construction.overlay(
        {"constructed-target": 1},
        source="live",
        instance="ctx:ws:backend",
    )
    assert live["constructed-target"].status == "invalid"
    assert live["constructed-target"].source == "live"
    assert live.snapshot == static.snapshot


def test_static_tag_inspection_never_resolves_or_imports_subject_targets():
    service = ModuleService(Registry("system-modules"))
    _definition(service, "safe-inspection", {"type": "boolean"}, default=False)
    service.register(
        "test_kind",
        "external",
        _record(
            tags={"safe-inspection": True},
            source="python-import",
            target="heavenbase_nonexistent_tag_fixture:VALUE",
        ),
        coordinate="test/test_kind/external",
    )

    assert "heavenbase_nonexistent_tag_fixture" not in sys.modules
    assert service.tag("test_kind", "external", "safe-inspection").value is True
    assert "heavenbase_nonexistent_tag_fixture" not in sys.modules


def test_generic_free_functions_use_one_inspection_snapshot_and_match_service_methods():
    service = ModuleService(Registry("system-modules"))
    _definition(service, "enabled", {"type": "boolean"}, default=False)
    _subject(service, aliases=("subject alias",))
    rows = service.inspect(include_inactive=True)

    class Catalog:
        publication = rows[0]["publication"]
        revision = rows[0]["revision"]
        fingerprint = None

    class Resolver:
        def __init__(self):
            self.catalog = Catalog()
            self.calls = 0

        def inspect(self, kind=None, identifier=None, *, include_inactive=False):
            self.calls += 1
            assert kind is None
            assert identifier is None
            assert include_inactive
            return rows

        def resolve(self, kind, identifier):
            raise AssertionError("Tag inspection must not resolve targets")

        def resolve_executable(self, reference, *, owner=None):
            raise AssertionError("Tag inspection must not resolve executables")

    resolver = Resolver()
    assert isinstance(resolver, ModuleResolver)

    definition = tag_definition(resolver, "enabled")
    assert definition is not None
    assert resolver.calls == 1
    view = tags(resolver, "test_kind", "subject alias")
    assert resolver.calls == 2
    value = inspect_tag(resolver, "test_kind", "subject", "enabled")
    assert resolver.calls == 3
    assert view["enabled"].value is False
    assert value is not None and value.value is False


def test_service_tag_inspection_reuses_record_fingerprints_until_publication(monkeypatch):
    import heavenbase.modules.service as module_lifecycle

    service = ModuleService(Registry("system-modules"))
    _definition(service, "enabled", {"type": "boolean"}, default=False)
    _subject(service)
    original = module_lifecycle._record_fingerprint
    fingerprints = 0

    def counted(record):
        nonlocal fingerprints
        fingerprints += 1
        return original(record)

    monkeypatch.setattr(module_lifecycle, "_record_fingerprint", counted)

    assert service.inspect("test_kind", "subject")[0]["identifier"] == "subject"
    assert fingerprints == 1
    assert service.tag("test_kind", "subject", "enabled").value is False
    first = fingerprints
    assert first == len(service.catalog.list())
    assert service.tags("test_kind", "subject")["enabled"].value is False
    assert service.tag_definition("enabled").default is False
    assert fingerprints == first

    _subject(service, "second", tags={"enabled": True})
    assert service.inspect("test_kind", "second")[0]["identifier"] == "second"
    assert fingerprints == first + 1
    assert service.tag("test_kind", "second", "enabled").value is True
    assert fingerprints == first + len(service.catalog.list())


def test_service_tag_definitions_and_views_are_built_once_per_publication(monkeypatch):
    # ``heavenbase.modules.tags`` is the public ``tags()`` function, so the
    # submodule is imported by name.
    tag_module = importlib.import_module("heavenbase.modules.tags")

    service = ModuleService(Registry("system-modules"))
    _definition(service, "enabled", {"type": "boolean"}, default=False)
    _definition(service, "level", {"type": "integer"}, default=1, aliases=("lvl",))
    _subject(service, tags={"enabled": True})
    _subject(service, "other")
    original = tag_module._definition_from_row
    built = 0

    def counted(row, captured):
        nonlocal built
        built += 1
        return original(row, captured)

    monkeypatch.setattr(tag_module, "_definition_from_row", counted)
    definition_count = sum(1 for row in service.inspect("tag"))

    first = service.tags("test_kind", "subject")
    assert built == definition_count
    assert service.tags("test_kind", "subject") is first
    assert service.tag("test_kind", "subject", "lvl") is first["level"]
    assert service.tags("test_kind", "other")["enabled"].value is False
    assert service.tag_definition("enabled") is first["enabled"].definition
    assert built == definition_count

    _subject(service, "late", tags={"level": 3})
    second = service.tags("test_kind", "subject")
    assert second is not first and second.snapshot != first.snapshot
    assert second["enabled"].value is True and second["level"].definition is not first["level"].definition
    assert service.tags("test_kind", "late")["level"].value == 3
    assert built == 2 * definition_count

    rows = service.inspect("tag", "enabled")
    rows[0]["record"]["meta"]["definition"]["schema"]["type"] = "string"
    assert service.inspect("tag", "enabled")[0]["record"]["meta"]["definition"]["schema"] == {"type": "boolean"}
    assert service.tags("test_kind", "subject") is second


def test_generic_free_functions_retry_a_concurrent_publication_without_torn_evidence():
    service = ModuleService(Registry("system-modules"))
    _definition(service, "enabled", {"type": "boolean"}, default=False)
    _subject(service)
    before = service.inspect(include_inactive=True)
    service.unregister("test_kind", "subject")
    _subject(service, tags={"enabled": True})
    after = service.inspect(include_inactive=True)

    class Catalog:
        publication = before[0]["publication"]
        revision = before[0]["revision"]

    class Resolver:
        def __init__(self):
            self.catalog = Catalog()
            self.calls = 0

        def inspect(self, kind=None, identifier=None, *, include_inactive=False):
            self.calls += 1
            assert kind is None
            assert identifier is None
            assert include_inactive
            if self.calls == 1:
                self.catalog.publication = after[0]["publication"]
                self.catalog.revision = after[0]["revision"]
                return before
            return after

        def resolve(self, kind, identifier):
            raise AssertionError("Tag inspection must not resolve targets")

        def resolve_executable(self, reference, *, owner=None):
            raise AssertionError("Tag inspection must not resolve executables")

    resolver = Resolver()
    assert isinstance(resolver, ModuleResolver)

    view = tags(resolver, "test_kind", "subject")

    assert resolver.calls == 2
    assert view.publication == after[0]["publication"]
    assert view.revision == after[0]["revision"]
    assert view["enabled"].value is True


def test_independent_resolvers_keep_definition_defaults_and_publications_isolated():
    first = ModuleService(Registry("system-modules"))
    second = ModuleService(Registry("system-modules"))
    _definition(first, "enabled", {"type": "boolean"}, default=True)
    _definition(second, "enabled", {"type": "boolean"}, default=False)
    _subject(first)
    _subject(second)

    first_value = first.tag("test_kind", "subject", "enabled")
    second_value = second.tag("test_kind", "subject", "enabled")

    assert first_value is not None and first_value.value is True
    assert second_value is not None and second_value.value is False
    assert first_value.definition is not second_value.definition


def test_metaschema_redacts_raw_claims_and_flattens_only_structural_labels():
    row = {
        "kind": "test_kind",
        "identifier": "subject",
        "aliases": [],
        "groups": {
            "family": ["semantic"],
            "tag": ["builtin", "semantic-extension"],
            "bundle": ["fixture"],
            "backend": ["main"],
        },
        "record": _record(tags={"secret-semantic-claim": True}),
        "revision": 7,
        "fingerprint": "f" * 64,
    }
    projection = object.__new__(_MetaSchemaRegistryRows)

    meta = projection._safe_record_meta(row)
    labels = projection._module_tags("test_kind", row["groups"])

    assert "tags" not in meta
    assert labels == ["registry", "test_kind", "builtin", "fixture", "main"]
    assert "semantic" not in labels
    assert "semantic-extension" not in labels


def test_builtin_catalog_has_exact_tag_vocabulary_and_no_legacy_semantic_authority():
    discovered = discover_package_modules("heavenbase")
    service = ModuleService(Registry("system-modules"))

    assert len(discovered) == 40
    assert all(module.manifest["version"] == __version__ for module in discovered)
    assert service.setup()
    rows = service.inspect(include_inactive=True)
    package_rows = tuple(row for row in rows if row["kind"] != "module_set")

    # +20 rows and +18 claimants over the 0.1.2.1 baseline: the workspace_explain_tool
    # and resolve_workspace_mcp_profile contributions, the python extension's
    # extension/toolkit_family/mcp_profile, the key_counts/key_sums/value_counts/
    # code_reduce/column_summary operation+reducer pairs, the database extension's
    # DbRule entity, and the optional agent-log extension with its AgentTrace entity
    # (the assistant session log's event row), plus the 0.1.2.3 database-dynamic-typing
    # tag definition that SQLite claims and the 0.1.2.3 `atomic-upsert` Backend tag
    # definition, whose layers include construction because an autocommit SQL engine
    # withdraws the claim. The definition fingerprint below is the real guard: only a deliberate
    # new definition, never a new claimant, may change it.
    assert len(package_rows) == 314
    assert tuple(sorted(row["identifier"] for row in package_rows if row["kind"] == "tag")) == BUILTIN_TAG_IDENTIFIERS
    assert not any(row["kind"] == "capability" for row in package_rows)
    assert not any(row["groups"].get("family") for row in package_rows)

    definitions = {str(row["identifier"]): row["record"]["meta"]["definition"] for row in package_rows if row["kind"] == "tag"}
    claims = {f"{row['kind']}:{row['identifier']}": row["record"]["meta"]["tags"] for row in package_rows if row["record"]["meta"].get("tags")}
    definition_fingerprint = hashlib.sha256(json.dumps(definitions, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    claim_fingerprint = hashlib.sha256(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    assert len(claims) == 237
    assert definition_fingerprint == "6916b59b666e6231c6a5e69d9fe658f4b374e89e9ab88c949eebf53c9905d23c"
    assert claim_fingerprint == "999b425457016d8e30842edffded52fae75915680f1865df46ac6c6930fb7698"
