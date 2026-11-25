"""Context-isolated resolver contracts for core typed facades."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import heavenbase as hb
from heavenbase.entity.registry import load_entity_type, registered_entity_identifiers
from heavenbase.extensions.memory.extension import _register_memory_entities, memory_entities
from heavenbase.filters import Expr
from heavenbase.filters.ops import normalize_op, register_op
from heavenbase.modules import ModuleService
from heavenbase.schema import EntitySchema, FieldSchema
from heavenbase.schema.text import register_schema_text, registered_schema_texts, resolve_schema_text
from heavenbase.placement import StorageProfile, StorageResolver, StorageRule, registered_storage_profiles
from heavenbase.strategies import InlineColumn, StorageStrategy
from heavenbase.types import Array, Identifier, LogicalType
from heavenbase.utils.registry import Registry
from heavenbase.workspace.catalog import Catalog
from heavenbase.workspace.foundation import FIXED_ENTITY_TYPES, fixed_entity_type
from heavenbase.workspace.metaschema import MetaSchema

pytestmark = pytest.mark.fast


class ResolverTypeA(LogicalType):
    """First implementation of one resolver-local logical-type identity."""

    identifier = "resolver-type"


class ResolverTypeB(LogicalType):
    """Second implementation of one resolver-local logical-type identity."""

    identifier = "resolver-type"


class ResolverStrategyA(StorageStrategy):
    """First implementation of one resolver-local strategy identity."""

    identifier = "resolver-strategy"
    option_fields = ("mode",)


class ResolverStrategyB(StorageStrategy):
    """Second implementation of one resolver-local strategy identity."""

    identifier = "resolver-strategy"
    option_fields = ("mode",)


def _render_first(schema: EntitySchema) -> str:
    """Render one schema through the first isolated resolver."""
    return f"first:{schema.entity_id}"


def _render_second(schema: EntitySchema) -> str:
    """Render one schema through the second isolated resolver."""
    return f"second:{schema.entity_id}"


class _ReadResolver:
    """Expose one ModuleService through only the read resolver contract."""

    def __init__(self, service: ModuleService) -> None:
        self._service = service
        self.catalog = service.catalog

    def inspect(self, kind=None, identifier=None, *, include_inactive=False):
        return self._service.inspect(kind, identifier, include_inactive=include_inactive)

    def resolve(self, kind, identifier):
        return self._service.resolve(kind, identifier)

    def resolve_executable(self, reference, *, owner=None):
        return self._service.resolve_executable(reference, owner=owner)


def _service() -> ModuleService:
    """Return one isolated process-local module authority."""
    return ModuleService(Registry("system-modules"))


def _register_types(service: ModuleService, dtype: type[LogicalType]) -> None:
    """Register the logical types used by nested schema probes."""
    Identifier.register(resolver=service)
    Array.register(resolver=service)
    dtype.register(tags={"resolver-implementation": dtype.__name__}, aliases=("resolver-alias",), resolver=service)


def _register_entity(service: ModuleService) -> None:
    """Register one inline Entity schema without resolving its implementation."""
    service.register(
        "entity",
        "resolver-entity",
        {
            "name": "ResolverEntity",
            "desc": "Resolver-local inline Entity schema.",
            "source": "inline",
            "target": "definition",
            "active": True,
            "meta": {
                "schema_version": 1,
                "definition": {
                    "entity_id": "resolver-entity",
                    "name": "ResolverEntity",
                    "fields": {
                        "object_id": {"type": "identifier", "pk": True, "required": True},
                        "value": {"type": "resolver-alias"},
                    },
                },
            },
        },
    )


def _register_memory_entity(service: ModuleService, label: str) -> None:
    """Register one resolver-local memory Entity schema."""
    Identifier.register(resolver=service)
    service.register(
        "entity",
        "memory-note",
        {
            "name": f"{label}Memory",
            "desc": "Resolver-local memory Entity schema.",
            "source": "inline",
            "target": "definition",
            "active": True,
            "meta": {
                "schema_version": 1,
                "definition": {
                    "entity_id": "memory-note",
                    "name": f"{label}Memory",
                    "fields": {"object_id": {"type": "identifier", "pk": True, "required": True}},
                },
            },
        },
    )


def test_logical_type_and_strategy_facades_isolate_resolver_identity_and_publication():
    first = _service()
    second = _service()
    _register_types(first, ResolverTypeA)
    _register_types(second, ResolverTypeB)
    ResolverStrategyA.register(tags={"resolver-implementation": "first"}, aliases=("resolver-strategy-alias",), resolver=first)
    ResolverStrategyB.register(tags={"resolver-implementation": "second"}, aliases=("resolver-strategy-alias",), resolver=second)

    assert LogicalType.load("resolver-alias", resolver=first) is ResolverTypeA
    assert LogicalType.load("resolver-alias", resolver=second) is ResolverTypeB
    assert type(LogicalType.from_any("array[resolver-alias]", resolver=first).item_type) is ResolverTypeA
    assert type(LogicalType.from_any("array[resolver-alias]", resolver=second).item_type) is ResolverTypeB
    assert first.tags("logical_type", "resolver-type").raw == {"resolver-implementation": "ResolverTypeA"}
    assert second.tags("logical_type", "resolver-type").raw == {"resolver-implementation": "ResolverTypeB"}
    assert StorageStrategy.load("resolver-strategy-alias", resolver=first) is ResolverStrategyA
    assert StorageStrategy.load("resolver-strategy-alias", resolver=second) is ResolverStrategyB
    assert isinstance(StorageStrategy.value_of({"type": "resolver-strategy-alias", "mode": "a"}, resolver=first), ResolverStrategyA)
    assert isinstance(StorageStrategy.value_of({"type": "resolver-strategy-alias", "mode": "b"}, resolver=second), ResolverStrategyB)

    ResolverTypeB.register(tags={"resolver-implementation": "ResolverTypeB"}, aliases=("resolver-alias",), resolver=first)
    assert LogicalType.load("resolver-alias", resolver=first) is ResolverTypeB


def test_operation_filter_and_schema_text_facades_use_one_explicit_resolver():
    first = _service()
    second = _service()
    register_op("resolver-first", aliases=("$resolver",), tags={"resolver-implementation": "first"}, resolver=first)
    register_op("resolver-second", aliases=("$resolver",), tags={"resolver-implementation": "second"}, resolver=second)
    register_schema_text("resolver", _render_first, resolver=first)
    register_schema_text("resolver", _render_second, resolver=second)
    schema = EntitySchema("resolver-entity", "ResolverEntity", fields={"object_id": FieldSchema("object_id", Identifier(), pk=True, required=True)})

    assert normalize_op("$resolver", resolver=first) == "resolver-first"
    assert normalize_op("$resolver", resolver=second) == "resolver-second"
    assert Expr.from_dict({"value": {"$resolver": 1}}, resolver=first).op == "resolver-first"
    assert Expr.from_dict({"value": {"$resolver": 1}}, resolver=second).op == "resolver-second"
    assert resolve_schema_text("resolver", resolver=first) is _render_first
    assert resolve_schema_text("resolver", resolver=second) is _render_second
    assert registered_schema_texts(resolver=first)["resolver"] is _render_first
    assert schema.to_str("resolver", resolver=second) == "second:resolver-entity"


def test_entity_and_storage_materializations_do_not_cross_resolvers():
    first = _service()
    second = _service()
    _register_types(first, ResolverTypeA)
    _register_types(second, ResolverTypeB)
    _register_entity(first)
    _register_entity(second)
    ResolverStrategyA.register(resolver=first)
    ResolverStrategyB.register(resolver=second)
    InlineColumn.register(resolver=first)
    StorageProfile(
        ResolverTypeA,
        ({"tag": "storage-roles", "contains": "row"},),
        ResolverStrategyA,
        "first-profile",
        resolver=first,
    ).register(resolver=first)
    StorageProfile(
        ResolverTypeB,
        ({"tag": "storage-roles", "contains": "row"},),
        ResolverStrategyB,
        "second-profile",
        resolver=second,
    ).register(resolver=second)

    first_entity = load_entity_type("resolver-entity", resolver=first)
    second_entity = load_entity_type("resolver-entity", resolver=second)
    assert type(first_entity.schema().fields["value"].dtype) is ResolverTypeA
    assert type(second_entity.schema().fields["value"].dtype) is ResolverTypeB
    assert first_entity is not second_entity
    assert registered_entity_identifiers(resolver=first) == ("resolver-entity",)
    assert registered_entity_identifiers(resolver=second) == ("resolver-entity",)
    assert registered_storage_profiles(resolver=first)["resolver-type"].reason == "first-profile"
    assert registered_storage_profiles(resolver=second)["resolver-type"].reason == "second-profile"

    first_resolver = StorageResolver(resolver=first)
    second_resolver = StorageResolver(resolver=second)
    first_rule = first_resolver.default_rule(ResolverTypeA(), ["main"])
    second_rule = second_resolver.default_rule(ResolverTypeB(), ["main"])
    assert first_rule.reason == "first-profile"
    assert second_rule.reason == "second-profile"
    assert first_rule._module_resolver is first
    assert second_rule._module_resolver is second

    authored = StorageRule(strategy="ResolverStrategyA")
    plan = first_resolver.resolve(first_entity.schema(), ["main"], {"value": [authored]})
    assert plan.first("value").strategy == ResolverStrategyA.identifier
    assert plan.first("value")._module_resolver is first


def test_fixed_workspace_entities_are_exact_across_resolver_generations():
    first = _service()
    second = _service()
    first.setup()
    second.setup()

    assert FIXED_ENTITY_TYPES == (Catalog, MetaSchema)
    assert fixed_entity_type("sys-catalog") is Catalog is hb.Catalog
    assert fixed_entity_type("sys-metaschema") is MetaSchema is hb.MetaSchema
    assert load_entity_type("sys-catalog", resolver=first) is Catalog
    assert load_entity_type("sys-catalog", resolver=second) is Catalog
    assert load_entity_type("sys-metaschema", resolver=first) is MetaSchema
    assert load_entity_type("sys-metaschema", resolver=second) is MetaSchema


def test_memory_setup_uses_the_workspace_module_resolver():
    first = _service()
    second = _service()
    _register_memory_entity(first, "First")
    _register_memory_entity(second, "Second")

    assert memory_entities(resolver=first)[0].schema().name == "FirstMemory"
    assert memory_entities(resolver=second)[0].schema().name == "SecondMemory"
    assert _register_memory_entities(SimpleNamespace(module_resolver=first, entities={}))[0].schema().name == "FirstMemory"
    assert _register_memory_entities(SimpleNamespace(module_resolver=second, entities={}))[0].schema().name == "SecondMemory"


def test_registration_requires_mutable_module_service_authority():
    readonly = _ReadResolver(_service())

    with pytest.raises(TypeError, match="mutable ModuleService"):
        ResolverTypeA.register(resolver=readonly)
    with pytest.raises(TypeError, match="mutable ModuleService"):
        ResolverStrategyA.register(resolver=readonly)
    with pytest.raises(TypeError, match="mutable ModuleService"):
        register_op("resolver-readonly", resolver=readonly)
    with pytest.raises(TypeError, match="mutable ModuleService"):
        register_schema_text("resolver-readonly", _render_first, resolver=readonly)
    with pytest.raises(TypeError, match="mutable ModuleService"):
        StorageProfile(
            ResolverTypeA,
            ({"tag": "storage-roles", "contains": "row"},),
            ResolverStrategyA,
            "readonly",
        ).register(resolver=readonly)
