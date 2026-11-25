from __future__ import annotations

import io
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

import heavenbase as hb
from heavenbase.capsule.layers import get_capsule_layer
from heavenbase.capsule.registry import capsule_registry, reset_capsule_registry
from heavenbase.entity.registry import load_entity_artifact_export
from heavenbase.llm.gateways import _protocols as adapters
from heavenbase.execution.handlers.plugins import HandlerPlugin, handler_plugins
from heavenbase.execution.handlers.reasons import ReasonRegistry
from heavenbase.strategies.base import StorageStrategy
from heavenbase.database.dialect.registry import DatabaseDialectSpec, database_dialects
from heavenbase.utils.log import get_logger, redirect_logs, restore_logs, set_log_level, suppress_logs
from heavenbase.llm.cache import LLMCache
from heavenbase.llm.repair import LLMToolCallRepair
from heavenbase.workspace.registry import WorkspaceSpec

WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry
WORKSPACE_LOCK = hb.DEFAULT_CONTEXT._workspace_lock


def _durable_test_class(name: str, base: type, identifier: str) -> type:
    """Create and publish one importable module-level concurrency fixture."""
    target = type(name, (base,), {"__module__": __name__, "__qualname__": name, "__doc__": "Durable Registry concurrency fixture.", "identifier": identifier})
    globals()[name] = target
    return target


_THREAD_STRATEGIES = tuple(_durable_test_class(f"ThreadStrategy{i}", StorageStrategy, f"thread-strategy-{i}") for i in range(16))
_THREAD_TYPES = tuple(_durable_test_class(f"ThreadType{i}", hb.LogicalType, f"thread-type-{i}") for i in range(16))
_THREAD_BACKENDS = tuple(_durable_test_class(f"ThreadBackend{i}", hb.ext.Backend, f"thread-backend-type-{i}") for i in range(16))


def _thread_handler_seed(registry, ctx) -> None:
    """Provide an importable no-op handler seed for concurrency tests."""
    registry, ctx


def _thread_handler_reasons(fallback, unsupported) -> None:
    """Provide importable no-op reason registration for concurrency tests."""
    fallback, unsupported


def _thread_schema_text(schema) -> str:
    """Render a schema identifier for concurrent registration tests."""
    return schema.entity_id


def _thread_interop_inferer(source, values, hint, report):
    """Infer short text for concurrent test columns."""
    del values, hint, report
    return hb.ShortText() if source.startswith("thread_col_") else None


def _thread_mcp_builder(_workspace):
    """Return no runtime tools for registration-only concurrency tests."""
    return {}


def _thread_mcp_serializer(value, *, config=None) -> str:
    """Serialize a value for concurrent MCP registration tests."""
    return repr(value)


def _thread_capsule_builder(ctx):
    """Build one inert custom Capsule layer payload."""
    del ctx
    return {"value": 1}


def _thread_capsule_result():
    """Return the fixed custom Capsule layer result."""
    return 1


def _thread_capsule_restorer(manifest, layer, policy):
    """Restore the fixed custom Capsule layer callable."""
    del manifest, layer, policy
    return _thread_capsule_result


def _resolve_profile(name: str):
    """Resolve one MCP profile through the installed Toolkit artifact."""
    resolve_profile = load_entity_artifact_export("sys-toolkit", "profiles", "resolve_profile")
    return resolve_profile(name)


def _db(tmp_path, name: str) -> str:
    return hb.utils.path_to_file_uri(tmp_path / name)


def _clear_workspace(ws_id: str) -> None:
    with WORKSPACE_LOCK:
        WORKSPACES.pop(ws_id, None)


def test_heavenbase_load_is_singleton_under_threads():
    ws_id = "thread-registry-test"
    _clear_workspace(ws_id)
    WorkspaceSpec.register(ws_id, config={"preset": "debug"})
    barrier = threading.Barrier(12)

    def open_one(_: int) -> int:
        barrier.wait()
        return id(hb.HeavenBase.load(ws_id))

    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            ids = list(pool.map(open_one, range(12)))
        assert len(set(ids)) == 1
    finally:
        _clear_workspace(ws_id)
        WorkspaceSpec.unregister(ws_id)


def test_inmem_backend_supports_concurrent_workspace_writes():
    ws = hb.HeavenBase("thread-inmem-test", detached=True, backends={"main": {"type": "inmem"}})

    class ThreadItem(hb.Entity):
        identifier = "thread-inmem-item"

        name = hb.field(hb.ShortText)

    ws.register(ThreadItem)
    n_workers = 16
    barrier = threading.Barrier(n_workers)

    def write_one(i: int) -> str:
        barrier.wait()
        row_id = f"item-{i}"
        ws.upsert(ThreadItem, {"object_id": row_id, "name": f"name-{i}"})
        return ws.get(row_id, entity=ThreadItem)["name"]

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        names = list(pool.map(write_one, range(n_workers)))

    assert set(names) == {f"name-{i}" for i in range(n_workers)}
    assert ws.count(ThreadItem) == n_workers


def test_file_backend_reads_observe_other_instance_writes(tmp_path):
    path = hb.utils.path_to_file_uri(tmp_path / "json-store")
    ws_id = "thread-file-refresh-test"

    def workspace_with_store() -> hb.HeavenBase:
        ws = hb.HeavenBase(ws_id, detached=True, backends={"main": hb.backends.InMemBackend("main")})
        store = hb.backends.JsonFileBackend("store", path)
        store.bind_workspace(ws_id)
        ws.backends.register(store)
        return ws

    ws_a = workspace_with_store()
    ws_b = workspace_with_store()

    class FileItem(hb.Entity):
        identifier = "thread-file-item"

        name = hb.field(hb.ShortText).store(to="store")

    ws_a.register(FileItem)
    ws_b.register(FileItem)
    ws_a.upsert(FileItem, {"object_id": "shared", "name": "one"})

    assert ws_b.get("shared", entity=FileItem)["name"] == "one"

    ws_a.upsert(FileItem, {"object_id": "shared", "name": "two"})
    assert ws_b.get("shared", entity=FileItem)["name"] == "two"

    ws_a.delete((FileItem, "shared"))
    assert ws_b.get("shared", entity=FileItem) is None
    assert ws_b.exists("shared", entity=FileItem) is False
    assert ws_b.count(FileItem) == 0

    ws_b.refresh(entity=FileItem)
    assert ws_b.get("shared", entity=FileItem) is None


def test_file_backend_supports_concurrent_workspace_writes(tmp_path):
    backend = hb.backends.JsonFileBackend("main", hb.utils.path_to_file_uri(tmp_path / "json-store"), ws_id="thread-file-write-test")
    n_workers = 16
    barrier = threading.Barrier(n_workers)

    def write_one(i: int) -> str:
        barrier.wait()
        row_id = f"item-{i}"
        backend.upsert([hb.ext.RowOp("thread-file-write-item", row_id, {"object_id": row_id, "name": f"name-{i}"})])
        return backend.get_many("thread-file-write-item", [row_id])[0]["name"]

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        names = list(pool.map(write_one, range(n_workers)))

    assert set(names) == {f"name-{i}" for i in range(n_workers)}
    assert backend.count("thread-file-write-item") == n_workers


def test_workspace_register_and_reads_are_serialized():
    ws = hb.HeavenBase("thread-register-read-test", detached=True, backends={"main": {"type": "inmem"}})

    class RegistryBaseItem(hb.Entity):
        identifier = "thread-register-base-item"

        name = hb.field(hb.ShortText)

    ws.register(RegistryBaseItem)
    ws.upsert(RegistryBaseItem, {"object_id": "base", "name": "base"})
    n_workers = 16
    barrier = threading.Barrier(n_workers)

    def work(i: int) -> int:
        entity = type(
            f"ThreadRegisterItem{i}",
            (hb.Entity,),
            {"identifier": f"thread-register-item-{i}", "name": hb.field(hb.ShortText)},
        )
        barrier.wait()
        if i % 2 == 0:
            ws.register(entity)
            return len(ws.list_entities())
        return ws.count(RegistryBaseItem)

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        values = list(pool.map(work, range(n_workers)))

    assert values
    assert ws.count(RegistryBaseItem) == 1


def test_workspace_reads_share_the_lock_and_skip_the_context_lock(monkeypatch):
    ws = hb.HeavenBase("thread-shared-read-test", detached=True, backends={"main": {"type": "inmem"}})

    class SharedReadItem(hb.Entity):
        identifier = "thread-shared-read-item"

        name = hb.field(hb.ShortText)

    ws.register(SharedReadItem)
    ws.upsert(SharedReadItem, {"object_id": "r1", "name": "one"})
    n_workers = 4
    barrier = threading.Barrier(n_workers, timeout=5)
    backend = ws.backends.get("main")
    original_get_many = backend.get_many

    def gated_get_many(*args, **kwargs):
        barrier.wait()  # every reader must be inside the backend read at the same time
        return original_get_many(*args, **kwargs)

    monkeypatch.setattr(backend, "get_many", gated_get_many)
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        with ws.context._lock:  # a held Context lock never blocks readers
            values = list(pool.map(lambda _: ws.get("r1", entity=SharedReadItem)["name"], range(n_workers)))

    assert values == ["one"] * n_workers


def test_workspace_writer_waits_for_readers_and_a_reader_can_write():
    ws = hb.HeavenBase("thread-writer-wait-test", detached=True, backends={"main": {"type": "inmem"}})

    class WriterWaitItem(hb.Entity):
        identifier = "thread-writer-wait-item"

        name = hb.field(hb.ShortText)

    ws.register(WriterWaitItem)
    ws.upsert(WriterWaitItem, {"object_id": "r1", "name": "one"})
    written = threading.Event()

    def write() -> None:
        ws.upsert(WriterWaitItem, {"object_id": "r2", "name": "two"})
        written.set()

    with ws._lock.read():
        ws.upsert(WriterWaitItem, {"object_id": "r3", "name": "three"})  # the reader writes its own workspace
        writer = threading.Thread(target=write)
        writer.start()
        assert not written.wait(0.3)  # the writer waits while a reader holds the lock
        assert ws.get("r3", entity=WriterWaitItem)["name"] == "three"  # the reader may still nest reads
    writer.join(timeout=5)

    assert written.is_set()
    assert ws.count(WriterWaitItem) == 3


def test_single_sql_workspace_concurrent_writes_use_pooled_connections(tmp_path):
    ws = hb.HeavenBase(
        "thread-sql-test",
        detached=True,
        backends={"main": {"type": "sqlite", "name": "main", "database": _db(tmp_path, "thread.db")}},
    )

    class SQLItem(hb.Entity):
        identifier = "thread-sql-item"

        name = hb.field(hb.ShortText)

    ws.register(SQLItem)
    n_workers = 8
    barrier = threading.Barrier(n_workers)

    def write_one(i: int) -> str:
        barrier.wait()
        row_id = f"item-{i}"
        ws.upsert(SQLItem, {"object_id": row_id, "name": f"name-{i}"})
        return row_id

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        row_ids = list(pool.map(write_one, range(n_workers)))

    assert sorted(row_ids) == [f"item-{i}" for i in range(n_workers)]
    assert ws.count(SQLItem) == n_workers


@pytest.mark.slow
def test_multiple_sql_workspaces_same_id_concurrent_writes(tmp_path):
    ws_id = "thread-sql-shared-test"
    database = _db(tmp_path, "shared.db")

    class SQLSharedItem(hb.Entity):
        identifier = "thread-sql-shared-item"

        name = hb.field(hb.ShortText)

    def build_workspace() -> hb.HeavenBase:
        return hb.HeavenBase(
            ws_id,
            detached=True,
            backends={"main": {"type": "sqlite", "name": "main", "database": database}},
        )

    primer = build_workspace()
    primer.register(SQLSharedItem)
    n_workers = 8
    barrier = threading.Barrier(n_workers)

    def write_one(i: int) -> str:
        ws = build_workspace()
        ws.register(SQLSharedItem)
        barrier.wait()
        row_id = f"item-{i}"
        ws.upsert(SQLSharedItem, {"object_id": row_id, "name": f"name-{i}"})
        return row_id

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        row_ids = list(pool.map(write_one, range(n_workers)))

    checker = build_workspace()
    checker.register(SQLSharedItem)
    assert sorted(row_ids) == [f"item-{i}" for i in range(n_workers)]
    assert checker.count(SQLSharedItem) == n_workers


def test_sqlite_lock_retry_config_is_available(tmp_path):
    backend = hb.backends.SQLiteBackend("main", database=_db(tmp_path, "retry.db"))
    retry = backend.db.spec.args.get("lock_retry") or {}

    assert int(retry.get("attempts", 0)) >= 1
    assert backend.db._retryable_lock_error(RuntimeError("database is locked")) is True
    assert backend.db._retryable_lock_error(RuntimeError("permission denied")) is False


def test_entity_pending_and_workspace_ids_are_thread_safe():
    n_workers = 16
    barrier = threading.Barrier(n_workers)

    def define_one(i: int) -> tuple[str, list[str]]:
        barrier.wait()
        entity = type(
            f"ThreadPendingEntity{i}",
            (hb.Entity,),
            {"identifier": f"thread-pending-entity-{i}", "name": hb.field(hb.ShortText)},
        )
        entity.register(f"thread-pending-ws-{i}")
        return entity.schema().entity_id, entity.workspace_ids()

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        rows = list(pool.map(define_one, range(n_workers)))

    assert {entity_id for entity_id, _ in rows} == {f"thread-pending-entity-{i}" for i in range(n_workers)}
    assert all(workspace_ids == [f"thread-pending-ws-{i}"] for i, (_, workspace_ids) in enumerate(rows))


def test_workspace_owned_registries_are_thread_safe():
    backend_registry = hb.ext.BackendRegistry()
    handler_registry = hb.ext.HandlerRegistry()
    reason_registry = ReasonRegistry(default_template="default {backend} {op}")
    n_workers = 16
    barrier = threading.Barrier(n_workers)
    for i in range(n_workers):
        hb.ext.register_op(f"thread-op-{i}")

    def handler(value, condition, ctx):
        return value, condition, ctx

    def register_one(i: int) -> tuple[list[str], int, str]:
        barrier.wait()
        backend_registry.register(hb.backends.InMemBackend(f"thread-backend-{i}"))
        hb.ext.Handler(hb.ShortText, f"thread-op-{i}", hb.backends.InMemBackend, "inline", handler).register(handler_registry)
        reason_registry.register("inmem", f"thread-op-{i}", "ShortText", "inline", f"reason-{i}")
        names = backend_registry.names()
        rows = handler_registry.capability_rows()
        reason = reason_registry.get("inmem", f"thread-op-{i}", "ShortText", "inline")
        return names, len(rows), reason or ""

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        results = list(pool.map(register_one, range(n_workers)))

    assert set(backend_registry.names()) == {f"thread-backend-{i}" for i in range(n_workers)}
    assert len(list(backend_registry.items())) == n_workers
    assert len(handler_registry.capability_rows()) == n_workers
    assert {row["backend_identifier"] for row in handler_registry.capability_rows()} == {"inmem"}
    assert {reason for _, _, reason in results} == {f"reason-{i}" for i in range(n_workers)}


def _unregister_registration_facade_rows(n_workers: int) -> None:
    """Remove the rows test_registration_facades_are_thread_safe adds to the worker's default Context.

    Dependants go first: a storage profile names its logical type and strategy.
    """
    service = hb.DEFAULT_CONTEXT.modules()
    for kind, pattern in (
        ("storage_profile", "thread-type-{i}"),
        ("database_dialect", "thread-db-dialect-{i}"),
        ("sql_dialect", "thread-sql-{i}"),
        ("operation", "thread-op-global-{i}"),
        ("logical_type", "thread-type-{i}"),
        ("strategy", "thread-strategy-{i}"),
        ("backend_type", "thread-backend-type-{i}"),
    ):
        for i in range(n_workers):
            if service.inspect(kind, pattern.format(i=i)):
                service.unregister(kind, pattern.format(i=i))


def test_registration_facades_are_thread_safe():
    n_workers = 16
    barrier = threading.Barrier(n_workers)

    def register_one(i: int) -> tuple[str, str, str, str, bool, str, bool]:
        strategy = _THREAD_STRATEGIES[i]
        dtype = _THREAD_TYPES[i]
        backend = _THREAD_BACKENDS[i]
        barrier.wait()
        backend.register(tags={"thread-probe": True})
        hb.ext.register_strategy(strategy, tags={"thread-probe": True}, aliases=(f"thread-strategy-alias-{i}",))
        hb.ext.register_logical_type(dtype, tags={"thread-probe": True}, aliases=(f"thread-type-alias-{i}",))
        hb.ext.register_op(f"thread-op-global-{i}", aliases=(f"$thread-op-global-{i}",), tags={"thread-probe": True})
        hb.ext.register_sql_dialect(hb.ext.SQLDialectSpec(f"thread-sql-{i}", supports_regex=bool(i % 2)), replace=True)
        hb.ext.StorageProfile(dtype, ({"tag": "thread-probe", "equals": True},), strategy, f"thread-storage-{i}").register()
        database_dialects.register(
            DatabaseDialectSpec(f"thread-db-dialect-{i}", tags={"database-json-native": bool(i % 2)}),
            replace=True,
        )
        return (
            hb.BackendType.identifier_of(f"thread-backend-type-{i}"),
            StorageStrategy.load(f"thread-strategy-alias-{i}").identifier,
            hb.ext.get_logical_type(f"thread-type-alias-{i}").identifier,
            hb.ext.normalize_op(f"$thread-op-global-{i}"),
            hb.ext.sql_dialect_spec(f"thread-sql-{i}").supports_regex,
            hb.ext.registered_storage_profiles()[f"thread-type-{i}"].default_strategy,
            database_dialects.get(f"thread-db-dialect-{i}").supports("database-json-native"),
        )

    try:
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            rows = list(pool.map(register_one, range(n_workers)))

        assert {row[0] for row in rows} == {f"thread-backend-type-{i}" for i in range(n_workers)}
        assert {row[1] for row in rows} == {f"thread-strategy-{i}" for i in range(n_workers)}
        assert {row[2] for row in rows} == {f"thread-type-{i}" for i in range(n_workers)}
        assert {row[3] for row in rows} == {f"thread-op-global-{i}" for i in range(n_workers)}
        assert {row[4] for row in rows} == {False, True}
        assert {row[5] for row in rows} == {f"thread-strategy-{i}" for i in range(n_workers)}
        assert {row[6] for row in rows} == {False, True}
        assert all(hb.DEFAULT_CONTEXT.modules().tag("backend_type", f"thread-backend-type-{i}", "thread-probe").value is True for i in range(n_workers))
        assert all(f"thread-sql-{i}" in hb.ext.registered_sql_dialects() for i in range(n_workers))
        assert all(f"thread-type-{i}" in hb.ext.registered_storage_profiles() for i in range(n_workers))
        assert all(f"thread-db-dialect-{i}" in database_dialects.snapshot() for i in range(n_workers))
    finally:
        _unregister_registration_facade_rows(n_workers)


def test_logger_registry_and_routing_are_thread_safe():
    n_workers = 16
    barrier = threading.Barrier(n_workers)
    names = [f"thread-logger-{i}" for i in range(n_workers)]
    streams = [io.StringIO() for _ in range(n_workers)]

    def register_one(i: int) -> tuple[str, int, str]:
        name = names[i]
        barrier.wait()
        logger = get_logger(name, level="DEBUG", fmt="%(message)s")
        redirect_logs(streams[i], loggers=[name], level="INFO", fmt="%(message)s")
        set_log_level("DEBUG" if i % 2 == 0 else "INFO", [name])
        logger.debug("debug-%s", i)
        logger.info("value-%s", i)
        if i % 3 == 0:
            suppress_logs([name])
            logger.info("suppressed-%s", i)
            redirect_logs(streams[i], loggers=[name], level="INFO", fmt="%(message)s")
        logger.info("after-%s", i)
        return name, id(logger), streams[i].getvalue()

    try:
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            rows = list(pool.map(register_one, range(n_workers)))
    finally:
        restore_logs(names)

    assert {name for name, _, _ in rows} == set(names)
    assert len({logger_id for _, logger_id, _ in rows}) == n_workers
    output_by_name = {name: output for name, _, output in rows}
    for i, name in enumerate(names):
        assert f"value-{i}" in output_by_name[name]
        assert f"after-{i}" in output_by_name[name]
        assert f"suppressed-{i}" not in output_by_name[name]


def test_schema_text_and_interop_registries_are_thread_safe():
    n_workers = 16
    barrier = threading.Barrier(n_workers)

    def register_one(i: int) -> tuple[str, str]:
        schema_format = f"thread-schema-text-{i}"
        source_name = f"thread_col_{i}"

        barrier.wait()
        hb.ext.register_schema_text(schema_format, _thread_schema_text, replace=True)
        hb.register_interop_type_inferer(hb.InteropTypeInferer(_thread_interop_inferer, identifier=f"thread-inferer-{i}"), first=True)
        schema_value = (
            hb.Entity.from_schema(
                {
                    "entity_id": f"thread-schema-{i}",
                    "fields": {"object_id": {"dtype": "identifier", "pk": True}, "name": {"dtype": "short-text"}},
                }
            )
            .schema()
            .to_str(schema_format)
        )
        interop = hb.from_sql_result([{"sku": f"p{i}", source_name: "value"}], entity_id=f"thread-interop-{i}", id="sku")
        dtype = interop.entity.schema().get(source_name).dtype.identifier
        return schema_value, dtype

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        rows = list(pool.map(register_one, range(n_workers)))

    assert {row[0] for row in rows} == {f"thread-schema-{i}" for i in range(n_workers)}
    assert {row[1] for row in rows} == {hb.ShortText.identifier}
    assert all(f"thread-schema-text-{i}" in hb.ext.registered_schema_texts() for i in range(n_workers))


def test_toolkit_serializer_cache_is_thread_safe():
    n_workers = 16
    barrier = threading.Barrier(n_workers)
    owner = hb.Capsule(
        _thread_capsule_result,
        name="thread-toolkit-cache",
        namespace="thread",
        version="v1",
    )
    tool = hb.Tool(owner, name="thread-toolkit-cache")

    def load_one(i: int) -> tuple[int, str]:
        barrier.wait()
        capsule = tool._serializer_capsule()
        return id(capsule), capsule.manifest.id

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        rows = list(pool.map(load_one, range(n_workers)))

    assert len({row[0] for row in rows}) == 1
    assert len({row[1] for row in rows}) == 1


def test_mcp_profile_and_tool_registries_are_thread_safe():
    n_workers = 16
    barrier = threading.Barrier(n_workers)

    def register_one(i: int) -> tuple[str, str, bool, bool, bool]:
        tool_name = f"thread-mcp-tool-{i}"
        provider_name = f"thread-mcp-provider-{i}"
        serializer_name = f"thread-mcp-serializer-{i}"
        profile_name = f"thread-mcp-profile-{i}"

        barrier.wait()
        hb.ext.register_workspace_toolkit_family(
            hb.ext.WorkspaceToolkitFamilySpec(provider_name, (tool_name,), _thread_mcp_builder, desc="thread MCP test"),
            replace=True,
        )
        hb.ext.ToolSerializer(serializer_name, _thread_mcp_serializer).register(replace=True)
        hb.ext.register_profile(
            hb.ext.ProfileSpec(name=profile_name, tools=(tool_name,), serializer=serializer_name),
            replace=True,
        )
        resolved = _resolve_profile(profile_name)
        return (
            resolved.tools[0],
            resolved.serializer,
            tool_name in hb.ext.registered_workspace_tool_names(),
            tool_name in hb.ext.registered_profile_tool_names(),
            hb.ext.registered_workspace_toolkit_families()[provider_name].tool_names == (tool_name,),
        )

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        rows = list(pool.map(register_one, range(n_workers)))

    assert {row[0] for row in rows} == {f"thread-mcp-tool-{i}" for i in range(n_workers)}
    assert {row[1] for row in rows} == {f"thread-mcp-serializer-{i}" for i in range(n_workers)}
    assert all(row[2] and row[3] and row[4] for row in rows)


def test_capsule_layer_registry_is_thread_safe():
    n_workers = 16
    barrier = threading.Barrier(n_workers)

    def register_one(i: int) -> tuple[str, bool, bool, bool]:
        layer_name = f"thread-capsule-layer-{i}"

        barrier.wait()
        hb.ext.register_capsule_layer(layer_name, builder=_thread_capsule_builder, restorer=_thread_capsule_restorer, replace=True)
        layer = get_capsule_layer(layer_name)
        return (
            layer.name,
            layer.builder is _thread_capsule_builder,
            layer_name in hb.ext.registered_capsule_layers(builders=True),
            layer_name in hb.ext.registered_capsule_layers(restorers=True),
        )

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        rows = list(pool.map(register_one, range(n_workers)))

    assert {row[0] for row in rows} == {f"thread-capsule-layer-{i}" for i in range(n_workers)}
    assert all(row[1] and row[2] and row[3] for row in rows)


def test_mcp_call_logger_has_one_record_authority():
    rows = hb.DEFAULT_CONTEXT.modules().inspect("mcp_call_logger", "agent")
    attach_logging = load_entity_artifact_export("sys-toolkit", "logging", "attach_mcp_call_logging")
    logging_module = sys.modules[attach_logging.__module__]

    assert len(rows) == 1
    target = rows[0]["record"]["target"]
    assert target["module"] == "session"
    assert target["qualname"] == "open_mcp_call_log"
    assert isinstance(target["artifact"], str)
    assert not hasattr(logging_module, "register_mcp_call_logger")


def test_handler_plugin_registry_is_thread_safe():
    n_workers = 16
    barrier = threading.Barrier(n_workers)

    def register_one(i: int) -> tuple[str, int]:
        barrier.wait()
        plugin = HandlerPlugin(f"thread-plugin-{i}", _thread_handler_seed, _thread_handler_reasons).register()
        return plugin.identifier, len(handler_plugins())

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        rows = list(pool.map(register_one, range(n_workers)))

    assert {plugin_id for plugin_id, _ in rows} == {f"thread-plugin-{i}" for i in range(n_workers)}
    assert all(any(plugin.identifier == f"thread-plugin-{i}" for plugin in handler_plugins()) for i in range(n_workers))
    for i in range(n_workers):
        hb.DEFAULT_CONTEXT.modules().unregister("handler", f"thread-plugin-{i}")


def test_capsule_registry_default_is_singleton_under_threads():
    reset_capsule_registry()
    n_workers = 16
    barrier = threading.Barrier(n_workers)

    def open_one(_: int) -> int:
        barrier.wait()
        return id(capsule_registry())

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        ids = list(pool.map(open_one, range(n_workers)))

    assert len(set(ids)) == 1
    reset_capsule_registry()


def test_translation_cache_invalidation_is_thread_safe():
    ws = hb.HeavenBase("thread-translation-cache-test", detached=True, backends={"main": {"type": "inmem"}})
    tr = hb.Translation(ws=ws, dicts=["thread.translation"])
    tr.set("Hello", "zz", "one")
    assert tr.get("Hello", "zz") == "one"

    n_workers = 8
    barrier = threading.Barrier(n_workers)

    def write_and_read(i: int) -> str:
        barrier.wait()
        tr.set(f"Hello {i}", "zz", f"value-{i}")
        return tr.get(f"Hello {i}", "zz")

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        values = list(pool.map(write_and_read, range(n_workers)))

    assert sorted(values) == [f"value-{i}" for i in range(n_workers)]
    tr.set("Hello", "zz", "two")
    assert tr.get("Hello", "zz") == "two"


def test_llm_cache_default_is_singleton_under_threads():
    LLMCache.reset()
    n_workers = 16
    barrier = threading.Barrier(n_workers)

    def open_one(_: int) -> int:
        barrier.wait()
        return id(LLMCache.current())

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        ids = list(pool.map(open_one, range(n_workers)))

    assert len(set(ids)) == 1
    LLMCache.reset()


def test_llm_adapter_client_and_repair_caches_are_thread_safe(monkeypatch):
    created = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    clients: dict = {}
    monkeypatch.setattr(adapters, "_OPENAI_CLIENTS", clients)
    LLMToolCallRepair.reset()
    spec = hb.LLM(model="deepseek-v4-flash", provider="deepseek", gateway="openai").spec
    n_workers = 16
    barrier = threading.Barrier(n_workers)

    def open_one(_: int) -> tuple[int, int]:
        barrier.wait()
        client, _runtime = adapters._openai_client(spec, "openai")
        return id(client), id(LLMToolCallRepair.current())

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        rows = list(pool.map(open_one, range(n_workers)))

    assert len({row[0] for row in rows}) == 1
    assert len({row[1] for row in rows}) == 1
    assert len(created) == 1
    assert not [slot for slot in adapters._CLIENT_BUILDS if slot[0] == id(clients)]
    LLMToolCallRepair.reset()
