"""Contract tests for execution-handler capability registration."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

import heavenbase as hb
import heavenbase.execution.handlers.seed as handler_seed
from heavenbase.execution.handlers import Handler, HandlerKey, HandlerPlugin, HandlerRegistry, HandlerSeedContext
from heavenbase.execution.handlers.seed import _handler_seed_context, register_generic_handlers, seed_handlers
from heavenbase.modules import ModuleService
from heavenbase.utils.registry import Registry


class ProtocolVectorStrategy(hb.StorageStrategy):
    """Test-only vector strategy contributed through canonical claims."""

    identifier = "protocol-vector"


class ProtocolSideFilterStrategy(hb.StorageStrategy):
    """Test-only nested-filter strategy contributed through canonical claims."""

    identifier = "protocol-side-filter"


@pytest.fixture(autouse=True)
def _registered_protocol_operation():
    """Publish the operation contract consumed by protocol handlers."""
    modules = hb.DEFAULT_CONTEXT.modules()
    hb.ext.register_op("protocol-op")
    yield
    if modules.inspect("operation", "protocol-op"):
        modules.unregister("operation", "protocol-op")


@pytest.fixture(scope="module")
def packaged_modules():
    """Return one isolated authority containing only packaged module records."""
    service = ModuleService(Registry("system-modules"))
    assert service.setup()
    return service


def test_handler_package_exports_protocol_not_provider_compilers():
    """Keep the package front door limited to handler extension contracts."""
    import heavenbase.execution.handlers as handlers

    protocol = {
        "Handler",
        "HandlerCompiler",
        "HandlerKey",
        "HandlerPlugin",
        "HandlerRegistry",
        "HandlerSeedContext",
        "Predicate",
        "AggregateCompileResult",
        "AggregateCompiler",
        "AggregateCompilerInput",
        "AggregateMetricInput",
    }
    assert set(handlers.__all__) == protocol | {"handler_plugins"}


def test_handler_layer_does_not_import_the_query_request_layer():
    """Keep handler contracts below query request objects in the dependency graph."""
    import heavenbase.execution.handlers as handlers

    offenders = []
    for path in Path(handlers.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module and (node.module == "query" or node.module.startswith(("query.", "heavenbase.query"))):
                offenders.append(f"{path.relative_to(Path(handlers.__file__).parent)}:{node.lineno}")
    assert offenders == []


def test_execution_layer_does_not_import_backend_packages():
    """Keep neutral execution contracts independent from provider ownership."""
    import heavenbase.execution as execution

    offenders = []
    for path in Path(execution.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = (alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names = (node.module or "",)
            else:
                continue
            if any(name == "backends" or name.startswith(("backends.", "heavenbase.backends")) for name in names):
                offenders.append(f"{path.relative_to(Path(execution.__file__).parent)}:{node.lineno}")
    assert offenders == []


def test_provider_handler_roles_live_with_their_backend_modules():
    """Keep provider compiler entry points under their cohesive Backend owners."""
    from heavenbase.backends.elasticsearch import handler as elasticsearch_handler
    from heavenbase.backends.sql import handler as sql_handler
    from heavenbase.backends.vector import handler as vector_handler

    assert {"register_handlers", "register_reasons"} <= set(elasticsearch_handler.__all__)
    assert set(sql_handler.__all__) == {"register_handlers", "register_reasons"}
    assert vector_handler.__all__ == []


def _compile(_field_schema, value, ctx):
    return hb.ext.QueryFragment(ctx.backend, "protocol-op", "value", value)


_PROTOCOL_SEEDED = []


def _seed_protocol_handler(registry, ctx) -> None:
    """Record one protocol plugin application for contract assertions."""
    _PROTOCOL_SEEDED.append((registry, ctx))


def _seed_protocol_reasons(fallback, _unsupported) -> None:
    """Provide one importable routing-reason contribution."""
    fallback.register("inmem", "protocol-op", "short-text", "inline", "protocol-local-fallback")


def test_handler_key_normalizes_and_serializes_capability_dimensions():
    key = HandlerKey(hb.ShortText, "protocol-op", hb.backends.InMemBackend, hb.InlineColumn)

    assert key.to_tuple() == ("short-text", "protocol-op", "inmem", "inline")
    assert key.to_dict() == {
        "dtype": "short-text",
        "op": "protocol-op",
        "backend_identifier": "inmem",
        "strategy": "inline",
    }
    assert set(key.aliases()) == {"dtype", "op", "backend", "strategy"}

    vector_key = HandlerKey(hb.Vector(3), "near", "inmem", hb.VectorIndex)
    assert vector_key.dtype == "vector"


def test_handler_is_the_only_registry_registration_value():
    registry = HandlerRegistry()
    handler = Handler(hb.ShortText, "protocol-op", "inmem", hb.InlineColumn, _compile)

    assert handler.register(registry) is handler
    assert registry.get(hb.ShortText, "protocol-op", "inmem", hb.InlineColumn) is handler
    assert registry.capabilities() == (handler.key,)
    assert registry.capability_rows() == [handler.key.to_dict()]

    with pytest.raises(ValueError, match="already registered"):
        Handler(hb.ShortText, "protocol-op", "inmem", hb.InlineColumn, _compile).register(registry)

    replacement = Handler(hb.ShortText, "protocol-op", "inmem", hb.InlineColumn, _compile)
    assert replacement.register(registry, replace=True) is replacement
    assert registry.get(hb.ShortText, "protocol-op", "inmem", hb.InlineColumn) is replacement


def test_handler_validates_compiler_output():
    handler = Handler(hb.ShortText, "protocol-op", "inmem", hb.InlineColumn, lambda *_args: None)
    ctx = hb.ext.HandlerContext("main", "inmem", "inline")

    with pytest.raises(TypeError, match="must return QueryFragment"):
        handler(None, "value", ctx)


def test_handler_plugin_owns_provider_registration():
    _PROTOCOL_SEEDED.clear()
    plugin = HandlerPlugin("protocol-provider", _seed_protocol_handler, _seed_protocol_reasons).register()
    try:
        registry = HandlerRegistry()
        ctx = HandlerSeedContext((), (), (), lambda _dtype: ())

        plugin.apply(registry, ctx)
        plugin.apply_reasons(registry)

        assert _PROTOCOL_SEEDED == [(registry, ctx)]
        assert plugin.reasons is _seed_protocol_reasons
        assert registry.fallback_reason("inmem", "protocol-op", "short-text", "inline") == "protocol-local-fallback"
        assert HandlerRegistry().fallback_reason("inmem", "protocol-op", "short-text", "inline") != "protocol-local-fallback"
        with pytest.raises(ValueError, match="already registered"):
            HandlerPlugin("protocol-provider", _seed_protocol_handler).register()
    finally:
        hb.DEFAULT_CONTEXT.modules().unregister("handler", "protocol-provider")


def test_generic_handler_seed_preserves_exact_claim_matrix_contribution(monkeypatch, packaged_modules):
    modules = packaged_modules
    captures = []
    capture = handler_seed._capture_seed_claims

    def capture_once(resolver):
        captures.append(resolver)
        return capture(resolver)

    monkeypatch.setattr(handler_seed, "_capture_seed_claims", capture_once)
    ctx = _handler_seed_context(modules)
    registry = HandlerRegistry(resolver=modules)

    register_generic_handlers(registry, ctx)

    assert captures == [modules]
    assert ctx.claim_snapshot is not None
    assert tuple(dtype.identifier for dtype in ctx.filter_types) == (
        "array",
        "boolean",
        "categorical",
        "date",
        "duration",
        "float",
        "hyper-g",
        "identifier",
        "integer",
        "json",
        "long-text",
        "medium-text",
        "short-text",
        "timestamp",
    )
    assert ctx.filter_strategies == ("inline", "inverted", "json")
    assert tuple(dtype.identifier for dtype in ctx.vector_types) == ("vector",)
    assert len(registry.capabilities()) == 14_688
    rows = sorted(key.to_tuple() for key in registry.capabilities())
    fingerprint = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
    assert fingerprint == "61ad8c58efdddc33a6cba8e0af9ac7eb89df0a008bc30920ed2ad8ae2468b027"


def test_complete_handler_seed_preserves_exact_provider_matrix(packaged_modules):
    """Keep tag-driven generic and provider plugins at the accepted exact keys."""
    registry = seed_handlers(resolver=packaged_modules)

    assert len(registry.capabilities()) == 14_853
    rows = sorted(key.to_tuple() for key in registry.capabilities())
    fingerprint = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
    assert fingerprint == "f8991201f827bb766bfa382a62b2efe82266f1fbc8cbcacdd7bd65540740efed"


def test_portable_vector_handlers_follow_registered_strategy_tags():
    modules = hb.DEFAULT_CONTEXT.modules()
    ProtocolVectorStrategy.register(tags={"near": True, "vector": True})
    try:
        registry = seed_handlers()

        near = registry.get(hb.Vector, "near", "inmem", ProtocolVectorStrategy)
        exact = registry.get(hb.Vector, "eq", "inmem", ProtocolVectorStrategy)

        assert near.key.strategy == ProtocolVectorStrategy.identifier
        assert exact.key.strategy == ProtocolVectorStrategy.identifier
    finally:
        modules.unregister("strategy", ProtocolVectorStrategy.identifier)


def test_portable_hyperg_handlers_follow_registered_nested_filter_tag():
    modules = hb.DEFAULT_CONTEXT.modules()
    ProtocolSideFilterStrategy.register(tags={"nested-filter": True, "side": True})
    try:
        registry = seed_handlers()

        exists = registry.get(hb.HyperG, "exists", "inmem", ProtocolSideFilterStrategy)
        all_items = registry.get(hb.HyperG, "all", "inmem", ProtocolSideFilterStrategy)

        assert exists.key.strategy == ProtocolSideFilterStrategy.identifier
        assert all_items.key.strategy == ProtocolSideFilterStrategy.identifier
    finally:
        modules.unregister("strategy", ProtocolSideFilterStrategy.identifier)
