"""Resolver-isolation contracts for backend and execution facades."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from heavenbase.backends import Backend, BackendRegistry, BackendType
from heavenbase.execution.contracts import HandlerContext
from heavenbase.backends.sql.capabilities import register_sql_dialect, sql_dialect_spec
from heavenbase.backends.sql.dialect import SQLDialectSpec
from heavenbase.backends.sql.strategies.gram import GramConfig, normalize_text, register_gram_normalizer
from heavenbase.filters.ops import register_op
from heavenbase.execution.handlers import Handler, HandlerPlugin, HandlerRegistry, Predicate, handler_plugins
from heavenbase.modules import ExecutableRef, ModuleService
from heavenbase.query.aggregate import reducer_for, register_aggregate_reducer
from heavenbase.strategies import StorageStrategy
from heavenbase.types import LogicalType
from heavenbase.database.config import DBEngine
from heavenbase.database.dialect.registry import ResolverDialectCatalog, database_dialects
from heavenbase.database.dialect.spec import DatabaseDialectSpec
from heavenbase.utils.registry import Registry

pytestmark = pytest.mark.fast


class ResolverBackendA(Backend):
    """First implementation of one resolver-local Backend identity."""

    identifier = "execution-backend"


class ResolverBackendB(Backend):
    """Second implementation of one resolver-local Backend identity."""

    identifier = "execution-backend"


class ResolverTypeA(LogicalType):
    """First logical type used by HandlerRegistry isolation probes."""

    identifier = "execution-type"


class ResolverTypeB(LogicalType):
    """Second logical type used by HandlerRegistry isolation probes."""

    identifier = "execution-type"


class ResolverStrategyA(StorageStrategy):
    """First strategy used by HandlerRegistry isolation probes."""

    identifier = "execution-strategy"


class ResolverStrategyB(StorageStrategy):
    """Second strategy used by HandlerRegistry isolation probes."""

    identifier = "execution-strategy"


class _Config:
    """Small ConfigManager-compatible authority for binding assertions."""

    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def get(self, path: str, default=None):
        """Return one configured value or its caller-supplied default."""
        return self.values.get(path, default)


def _first_normalizer(text: str, _config: GramConfig) -> str:
    """Return resolver-A normalization output."""
    return f"first:{text}"


def _second_normalizer(text: str, _config: GramConfig) -> str:
    """Return resolver-B normalization output."""
    return f"second:{text}"


def _first_fold(values: list[int]) -> str:
    """Return a stable resolver-A aggregate marker."""
    return f"first:{sum(values)}"


def _second_fold(values: list[int]) -> str:
    """Return a stable resolver-B aggregate marker."""
    return f"second:{sum(values)}"


def _first_compile(_field, value, ctx):
    """Compile one resolver-A test fragment."""
    from heavenbase.execution.contracts import QueryFragment

    return QueryFragment(ctx.backend, "execution-op-first", "value", ("first", value))


def _second_compile(_field, value, ctx):
    """Compile one resolver-B test fragment."""
    from heavenbase.execution.contracts import QueryFragment

    return QueryFragment(ctx.backend, "execution-op-second", "value", ("second", value))


def _seed_first(_registry, _ctx) -> None:
    """Seed marker for the first resolver-local handler plugin."""
    return None


def _seed_second(_registry, _ctx) -> None:
    """Seed marker for the second resolver-local handler plugin."""
    return None


def _normalize_aggregate_options(options):
    """Return one isolated aggregate-options fixture."""
    return dict(options)


def _predicate_matcher(left, right):
    """Compare values for one executable-reference fixture."""
    return left == right


def _predicate_preparer(value):
    """Preserve one executable-reference predicate operand."""
    return value


def _aggregate_result_type(source_dtype, _options):
    """Preserve the aggregate fixture's source logical type."""
    return source_dtype


def _sql_text_evidence(_backend, _entity_id, _field, _strategy):
    """Authorize the equality fixture for one SQL dialect."""
    return ("eq",)


def _build_resolver_backend(name, data, *, resolver, config):
    """Build the resolver fixture through an explicit record-owned factory."""
    return ResolverBackendA(name, ws_id=data.get("ws_id"), resolver=resolver, config=config)


def _service() -> ModuleService:
    """Return one isolated process-local module authority."""
    return ModuleService(Registry("system-modules"))


def _assert_python_executable_ref(value, target: str) -> None:
    """Assert one canonical ambient Python executable reference."""
    assert value == {
        "kind": "heavenbase.executable",
        "version": 1,
        "source": "python-import",
        "target": target,
    }


def _register_execution_vocabulary(first: ModuleService, second: ModuleService) -> None:
    """Publish equal facade coordinates with independent implementations."""
    ResolverBackendA.register(tags={"execution-capability": "first"}, resolver=first)
    ResolverBackendB.register(tags={"execution-capability": "second"}, resolver=second)
    ResolverTypeA.register(resolver=first)
    ResolverTypeB.register(resolver=second)
    ResolverStrategyA.register(resolver=first)
    ResolverStrategyB.register(resolver=second)
    register_op("execution-op-first", aliases=("$execution",), resolver=first)
    register_op("execution-op-second", aliases=("$execution",), resolver=second)


def test_backend_registry_preserves_construction_authority_and_rejects_rebinding():
    first = _service()
    second = _service()
    _register_execution_vocabulary(first, second)
    first_config = _Config({"policy": "first"})
    second_config = _Config({"policy": "second"})

    first_backend = BackendType.build(
        "first",
        {"type": "execution-backend"},
        resolver=first,
        config=first_config,
    )
    second_backend = BackendType.build(
        "second",
        {"type": "execution-backend"},
        resolver=second,
        config=second_config,
    )

    assert type(first_backend) is ResolverBackendA
    assert type(second_backend) is ResolverBackendB
    assert first_backend.module_resolver is first
    assert second_backend.module_resolver is second
    assert first_backend.config is first_config
    assert second_backend.config is second_config
    assert first_backend.tag("execution-capability").value == "first"
    assert second_backend.tag("execution-capability").value == "second"

    first_live = ResolverBackendA("live-first", resolver=first, config=first_config)
    second_registry = BackendRegistry(resolver=second, config=second_config)
    with pytest.raises(ValueError, match="cannot be registered under different"):
        second_registry.register(first_live)
    assert first_live.module_resolver is first
    assert first_live.config is first_config
    assert first_live.tag("execution-capability").value == "first"

    unbound = ResolverBackendB("live-unbound")
    with pytest.raises(ValueError, match="unbound live Backend"):
        second_registry.register(unbound)
    assert unbound._module_resolver is None
    assert unbound._config is None

    second_live = ResolverBackendB("live-second", resolver=second, config=second_config)
    second_registry.register(second_live)
    assert second_live.module_resolver is second
    assert second_live.config is second_config
    assert second_live.tag("execution-capability").value == "second"
    assert HandlerContext("live", "execution-backend", "execution-strategy", backend_instance=second_live).resolver is second


def test_backend_construction_rejects_partial_explicit_authority():
    resolver = _service()
    ResolverBackendA.register(resolver=resolver)
    config = _Config({"policy": "isolated"})

    with pytest.raises(ValueError, match="resolver and config together"):
        BackendType.build("partial", {"type": "execution-backend"}, resolver=resolver)
    with pytest.raises(ValueError, match="resolver and config together"):
        BackendType.build("partial", {"type": "execution-backend"}, config=config)
    with pytest.raises(ValueError, match="resolver and config together"):
        BackendRegistry(resolver=resolver)
    with pytest.raises(ValueError, match="resolver and config together"):
        BackendRegistry(config=config)


def test_parallel_backend_construction_keeps_resolver_and_config_authorities_isolated():
    first = _service()
    second = _service()
    _register_execution_vocabulary(first, second)
    first_config = _Config({"policy": "first"})
    second_config = _Config({"policy": "second"})
    barrier = threading.Barrier(2)

    def build_many(prefix, resolver, config):
        barrier.wait()
        return [
            BackendType.build(
                f"{prefix}-{index}",
                {"type": "execution-backend"},
                resolver=resolver,
                config=config,
            )
            for index in range(32)
        ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(build_many, "first", first, first_config)
        second_future = pool.submit(build_many, "second", second, second_config)
        first_backends = first_future.result()
        second_backends = second_future.result()

    assert all(type(backend) is ResolverBackendA for backend in first_backends)
    assert all(type(backend) is ResolverBackendB for backend in second_backends)
    assert all(backend.module_resolver is first and backend.config is first_config for backend in first_backends)
    assert all(backend.module_resolver is second and backend.config is second_config for backend in second_backends)


def test_database_specs_capture_resolver_local_dialect_metadata_and_cache_identity():
    first = _service()
    second = _service()
    database_dialects.register(DatabaseDialectSpec("execution-db", requires_host=False, varchar_max=100), resolver=first)
    database_dialects.register(DatabaseDialectSpec("execution-db", requires_host=False, varchar_max=200), resolver=second)
    root = {
        "default_provider": "execution",
        "providers": {"execution": {"dialect": "execution-db"}},
    }
    first_engine = DBEngine(config=root, manager=None, dialects=ResolverDialectCatalog(first))
    second_engine = DBEngine(config=root, manager=None, dialects=ResolverDialectCatalog(second))

    first_spec = first_engine.resolve({"database": "app"})
    second_spec = second_engine.resolve({"database": "app"})

    assert first_spec.dialect_spec.varchar_max == 100
    assert second_spec.dialect_spec.varchar_max == 200
    assert first_engine.apply(first_spec, profile="cache-key") != second_engine.apply(second_spec, profile="cache-key")

    database_dialects.register(
        DatabaseDialectSpec("execution-db", requires_host=False, varchar_max=300),
        replace=True,
        resolver=first,
    )
    assert first_spec.dialect_spec.varchar_max == 100


def test_sql_gram_plugin_and_reducer_materializations_do_not_cross_resolvers():
    first = _service()
    second = _service()
    _register_execution_vocabulary(first, second)
    register_sql_dialect(SQLDialectSpec("execution-sql", supports_regex=True), resolver=first)
    register_sql_dialect(SQLDialectSpec("execution-sql", supports_regex=False), resolver=second)
    register_gram_normalizer("execution-normalizer", _first_normalizer, resolver=first)
    register_gram_normalizer("execution-normalizer", _second_normalizer, resolver=second)
    HandlerPlugin("execution-backend", _seed_first, resolver=first).register(resolver=first)
    HandlerPlugin("execution-backend", _seed_second, resolver=second).register(resolver=second)
    register_aggregate_reducer("execution_reducer", _first_fold, resolver=first)
    register_aggregate_reducer("execution_reducer", _second_fold, resolver=second)
    gram = GramConfig.load(manager=None, normalizer="execution-normalizer", normalize_cache_size=8)

    assert sql_dialect_spec("execution-sql", resolver=first).supports_regex is True
    assert sql_dialect_spec("execution-sql", resolver=second).supports_regex is False
    assert normalize_text("value", gram, resolver=first).text == "first:value"
    assert normalize_text("value", gram, resolver=second).text == "second:value"
    assert handler_plugins(resolver=first)[0].seed is _seed_first
    assert handler_plugins(resolver=second)[0].seed is _seed_second
    assert reducer_for("execution_reducer", resolver=first).reduce([1, 2], {}) == "first:3"
    assert reducer_for("execution_reducer", resolver=second).reduce([1, 2], {}) == "second:3"


def test_execution_facades_persist_and_restore_canonical_nested_executable_references():
    service = _service()
    ResolverBackendA.register(factory=_build_resolver_backend, resolver=service)
    service.register(
        "tag",
        "execution-ref-tag",
        {
            "name": "Execution reference Tag",
            "desc": "Declarative Tag-definition fixture.",
            "source": "inline",
            "target": "definition",
            "active": True,
            "meta": {
                "schema_version": 1,
                "definition": {
                    "schema": {"type": "integer"},
                    "default": 1,
                    "subjects": ["backend_type"],
                    "subject_policy": "exact",
                    "layers": ["static"],
                },
            },
        },
        coordinate="test/tag/execution-ref-tag",
    )
    register_op("execution-ref-predicate", resolver=service)
    Predicate("execution-ref-predicate", _predicate_matcher, _predicate_preparer).register(resolver=service)
    HandlerPlugin("execution-backend", _seed_first, _seed_second, resolver=service).register(resolver=service)
    register_aggregate_reducer(
        "execution_ref_reducer",
        _first_fold,
        normalize_options=_normalize_aggregate_options,
        result_type=_aggregate_result_type,
        resolver=service,
    )
    register_sql_dialect(
        SQLDialectSpec(
            "execution-ref-sql",
            native_text_ops=("eq",),
            text_evidence=_sql_text_evidence,
        ),
        resolver=service,
    )

    backend_meta = service.inspect("backend_type", "execution-backend")[0]["record"]["meta"]
    tag_definition = service.tag_definition("execution-ref-tag")
    predicate = service.resolve("predicate", "execution-ref-predicate")
    handler = service.resolve("handler", "execution-backend")
    reducer_meta = service.inspect("reducer", "execution_ref_reducer")[0]["record"]["meta"]["reducer"]
    dialect = service.resolve("sql_dialect", "execution-ref-sql")

    _assert_python_executable_ref(
        backend_meta["factory"],
        "test_execution_resolver_facades:_build_resolver_backend",
    )
    assert tag_definition is not None
    assert tag_definition.default == 1
    assert tag_definition.schema == {"type": "integer"}
    _assert_python_executable_ref(
        predicate["matcher"],
        "test_execution_resolver_facades:_predicate_matcher",
    )
    _assert_python_executable_ref(
        predicate["preparer"],
        "test_execution_resolver_facades:_predicate_preparer",
    )
    _assert_python_executable_ref(
        handler["seed"],
        "test_execution_resolver_facades:_seed_first",
    )
    _assert_python_executable_ref(
        handler["reasons"],
        "test_execution_resolver_facades:_seed_second",
    )
    _assert_python_executable_ref(
        reducer_meta["normalize_options"],
        "test_execution_resolver_facades:_normalize_aggregate_options",
    )
    _assert_python_executable_ref(
        reducer_meta["result_type"],
        "test_execution_resolver_facades:_aggregate_result_type",
    )
    _assert_python_executable_ref(
        dialect["text_evidence"],
        "test_execution_resolver_facades:_sql_text_evidence",
    )

    assert BackendType.factory("execution-backend", resolver=service) is _build_resolver_backend
    assert Predicate.load("execution-ref-predicate", resolver=service).matcher is _predicate_matcher
    assert handler_plugins(resolver=service)[0].seed is _seed_first
    assert reducer_for("execution_ref_reducer", resolver=service).normalize_options is _normalize_aggregate_options
    assert sql_dialect_spec("execution-ref-sql", resolver=service).text_evidence is _sql_text_evidence


def test_handler_plugins_accept_capsule_refs_and_reject_legacy_nested_import_strings():
    restored = []

    def restore_capsule(capsule_id, resolver):
        restored.append((capsule_id, resolver))
        return _seed_first

    capsule_service = ModuleService(
        Registry("system-modules"),
        source_restorers={"capsule": restore_capsule},
    )
    capsule_service.register(
        "handler",
        "capsule-handler",
        {
            "name": "Capsule handler",
            "desc": "Capsule-restored handler contribution.",
            "source": "inline",
            "target": "definition",
            "active": True,
            "meta": {
                "schema_version": 1,
                "priority": 10,
                "definition": {
                    "seed": ExecutableRef.capsule("capsule-seed").to_dict(),
                    "reasons": None,
                },
            },
        },
    )

    assert handler_plugins(resolver=capsule_service)[0].seed is _seed_first
    assert restored == [("capsule-seed", capsule_service)]

    legacy_service = _service()
    legacy_service.register(
        "handler",
        "legacy-handler",
        {
            "name": "Legacy handler",
            "desc": "Rejected legacy nested import string.",
            "source": "inline",
            "target": "definition",
            "active": True,
            "meta": {
                "schema_version": 1,
                "definition": {
                    "seed": "test_execution_resolver_facades:_seed_first",
                    "reasons": None,
                },
            },
        },
    )
    with pytest.raises(TypeError, match="seed must be an executable reference"):
        handler_plugins(resolver=legacy_service)


def test_handler_registry_normalizes_every_dimension_through_its_resolver():
    first = _service()
    second = _service()
    _register_execution_vocabulary(first, second)
    first_registry = HandlerRegistry(resolver=first)
    second_registry = HandlerRegistry(resolver=second)
    first_handler = Handler(
        "execution-type",
        "$execution",
        "execution-backend",
        "execution-strategy",
        _first_compile,
        resolver=first,
    ).register(first_registry)
    second_handler = Handler(
        "execution-type",
        "$execution",
        "execution-backend",
        "execution-strategy",
        _second_compile,
        resolver=second,
    ).register(second_registry)

    assert first_handler.key.op == "execution-op-first"
    assert second_handler.key.op == "execution-op-second"
    assert first_registry.get("execution-type", "$execution", "execution-backend", "execution-strategy") is first_handler
    assert second_registry.get("execution-type", "$execution", "execution-backend", "execution-strategy") is second_handler
