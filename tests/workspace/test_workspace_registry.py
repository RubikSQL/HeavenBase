from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

import heavenbase as hb
from heavenbase.workspace import WorkspaceSpec
from heavenbase.workspace.setup.lifecycle import _default_workspace_name
from heavenbase.utils.config import ConfigManager

WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry


class WorkspaceScopedNote(hb.Entity):
    """Small entity for workspace-scoped runtime configuration tests."""

    identifier = "workspace-scoped-note"

    name = hb.field(hb.ShortText)


def _workspace_scope_serializer(_output, *, config) -> str:
    """Return the active config scope observed at MCP serialization."""
    return config.scope


def _isolated_context(tmp_path):
    config = ConfigManager(
        default={
            "heavenbase": {
                "registry": {
                    "default_workspace": "default",
                    "active": {"workspace": None},
                }
            }
        },
        setup=False,
    )
    return hb.Context.load(
        {
            "version": 1,
            "root": str(tmp_path),
            "workspace": "default",
            "backend": {
                "type": "inmem",
                "module": "heavenbase.backends.inmem.backend",
                "name": "system",
            },
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=config,
    )


def test_workspace_spec_defaults_to_the_process_context_authority():
    from heavenbase.workspace import registry as workspace_registry

    registry = workspace_registry._registry(hb.DEFAULT_CONTEXT)

    assert registry is hb.DEFAULT_CONTEXT.registry(
        "system-workspaces", encode_value=workspace_registry._encode_workspace, decode_value=workspace_registry._decode_workspace
    )
    assert hb.DEFAULT_CONTEXT.config is hb.CM_HVNB


def test_workspace_spec_manifest_id_must_match_workspace_id():
    with pytest.raises(ValueError, match="does not match registered workspace"):
        WorkspaceSpec.from_dict({"id": "workspace-a", "manifest": {"id": "workspace-b"}})


def test_workspace_spec_ignores_legacy_scope_and_writes_canonical_row():
    spec = WorkspaceSpec.from_dict(
        {
            "id": "workspace-a",
            "scope": "heavenbase.workspace-a",
        }
    )

    assert "scope" not in spec.to_dict()
    assert not hasattr(spec, "scope")


def test_workspace_spec_copies_mutable_config_and_manifest_payloads():
    raw = {
        "id": "workspace-a",
        "construction": {"kind": "preset", "preset": "debug"},
        "manifest": {
            "kind": "heavenbase.workspace.manifest",
            "version": 2,
            "id": "workspace-a",
            "construction": {"kind": "preset", "preset": "debug"},
            "entities": [],
        },
    }

    spec = WorkspaceSpec.from_dict(raw)
    raw["construction"]["preset"] = "changed"
    raw["manifest"]["construction"]["preset"] = "changed"

    data = spec.to_dict()
    data["construction"]["preset"] = "changed-again"
    data["manifest"]["construction"]["preset"] = "changed-again"

    assert spec.config == {"preset": "debug"}
    assert spec.construction.to_dict() == {"kind": "preset", "preset": "debug"}
    assert spec.manifest["construction"] == {"kind": "preset", "preset": "debug"}


def test_workspace_spec_rejects_manifest_construction_divergence():
    with pytest.raises(ValueError, match="manifest construction disagrees"):
        WorkspaceSpec.from_dict(
            {
                "id": "workspace-divergent",
                "construction": {"kind": "preset", "preset": "debug"},
                "manifest": {
                    "kind": "heavenbase.workspace.manifest",
                    "version": 2,
                    "id": "workspace-divergent",
                    "construction": {
                        "kind": "backends",
                        "config": {"main": {"type": "inmem"}},
                    },
                },
            }
        )


def test_workspace_spec_open_detached_has_explicit_caller_owned_lifecycle():
    prior_default = _default_workspace_name()
    spec = WorkspaceSpec.from_dict(
        {
            "id": "detached-spec",
            "config": {"backends": {"main": {"type": "inmem"}}},
        }
    )

    workspace = spec.open_detached()
    try:
        assert workspace.id == "detached-spec"
        assert "detached-spec" not in WORKSPACES
        assert hb.HeavenBase.drop("detached-spec") is False
        assert _default_workspace_name() == prior_default
    finally:
        workspace.drop()


@pytest.mark.fast
def test_workspace_constructor_ensures_compatible_durable_identity(tmp_path):
    context = _isolated_context(tmp_path)
    config = {"main": {"type": "inmem"}}
    try:
        first = hb.HeavenBase("ensure-compatible", context=context, backends=config)
        first_spec = WorkspaceSpec.load(first.id, context=context)

        second = hb.HeavenBase("ensure-compatible", context=context, backends=config)
        second_spec = WorkspaceSpec.load(second.id, context=context)

        assert second is not first
        assert hb.HeavenBase.load(second.id, context=context) is second
        assert second_spec.construction.to_dict() == {
            "kind": "backends",
            "config": config,
        }
        assert second_spec.created_at == first_spec.created_at
        assert second_spec.updated_at == first_spec.updated_at

        implicit = hb.HeavenBase("ensure-compatible", context=context)
        assert implicit.backends.names() == ["main"]
        assert hb.HeavenBase.load(implicit.id, context=context) is implicit

        with pytest.raises(FileExistsError, match="different construction contract"):
            hb.HeavenBase(
                "ensure-compatible",
                context=context,
                backends={"other": {"type": "inmem"}},
            )
        assert hb.HeavenBase.load(implicit.id, context=context) is implicit
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_runtime_backend_compatibility_uses_names_and_identifiers(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        resolver = context.modules()
        first_backend = hb.backends.InMemBackend(
            "main",
            resolver=resolver,
            config=context.config,
        )
        hb.HeavenBase(
            "runtime-compatible",
            context=context,
            backends={"ignored-input-alias": first_backend},
        )
        spec = WorkspaceSpec.load("runtime-compatible", context=context)

        assert spec.construction.to_dict() == {
            "kind": "runtime",
            "identifiers": {"main": "inmem"},
        }

        replacement_backend = hb.backends.InMemBackend(
            "main",
            resolver=resolver,
            config=context.config,
        )
        replacement = hb.HeavenBase(
            "runtime-compatible",
            context=context,
            backends={"another-input-alias": replacement_backend},
        )
        assert replacement.backends.get("main") is replacement_backend

        implicit = hb.HeavenBase("runtime-compatible", context=context)
        assert implicit.backends.get("main") is replacement_backend

        with pytest.raises(FileExistsError, match="different construction contract"):
            hb.HeavenBase(
                "runtime-compatible",
                context=context,
                backends={
                    "main": hb.backends.InMemBackend(
                        "other",
                        resolver=resolver,
                        config=context.config,
                    )
                },
            )

        implicit.release()
        with pytest.raises(RuntimeError, match="runtime-only backends"):
            hb.HeavenBase.load("runtime-compatible", context=context)
    finally:
        context.close()


@pytest.mark.fast
def test_detached_workspace_has_only_caller_owned_lifecycle(tmp_path):
    context = _isolated_context(tmp_path)
    workspace = None
    try:
        workspace = hb.HeavenBase(
            "caller-owned",
            context=context,
            backends={"main": {"type": "inmem"}},
            detached=True,
        )

        assert workspace.detached is True
        assert context._workspace_registry.get(workspace.id) is None
        assert context._live_workspaces.get(workspace.id) is None
        with pytest.raises(KeyError, match="caller-owned"):
            WorkspaceSpec.load(workspace.id, context=context)
        with pytest.raises(RuntimeError, match="cannot be activated"):
            workspace.activate()
        assert hb.HeavenBase.drop(workspace.id, context=context) is False
    finally:
        if workspace is not None:
            workspace.drop()
        context.close()


@pytest.mark.fast
def test_load_requires_durable_registration_before_consulting_live_cache(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        workspace = hb.HeavenBase(
            "unregistered-live-cache",
            context=context,
            backends={"main": {"type": "inmem"}},
        )
        assert WorkspaceSpec.unregister(workspace.id, context=context)
        assert context._workspace_registry.get(workspace.id) is workspace

        with pytest.raises(KeyError, match="unregistered-live-cache"):
            hb.HeavenBase.load(workspace.id, context=context)
    finally:
        context.close()


@pytest.mark.fast
def test_load_reconstructs_when_durable_construction_changes(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        first = hb.HeavenBase(
            "durable-construction-wins",
            context=context,
            backends={"main": {"type": "inmem"}},
        )
        WorkspaceSpec.register(
            first.id,
            config={"backends": {"replacement": {"type": "inmem"}}},
            context=context,
        )

        reopened = hb.HeavenBase.load(first.id, context=context)

        assert reopened is not first
        assert reopened.backends.names() == ["replacement"]
        assert context._workspace_registry.get(first.id) is reopened
    finally:
        context.close()


@pytest.mark.fast
def test_live_backend_instance_cannot_be_rebound_across_workspaces(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        backend = hb.backends.InMemBackend(
            "main",
            resolver=context.modules(),
            config=context.config,
        )
        hb.HeavenBase(
            "runtime-owner-a",
            context=context,
            backends={"main": backend},
        )

        with pytest.raises(ValueError, match="already bound to another workspace"):
            hb.HeavenBase(
                "runtime-owner-b",
                context=context,
                backends={"main": backend},
            )

        assert backend.ws_id == "runtime-owner-a"
        with pytest.raises(KeyError, match="runtime-owner-b"):
            WorkspaceSpec.load("runtime-owner-b", context=context)
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_creation_preserves_active_selection_and_configured_default(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        base = hb.HeavenBase("default", context=context, backends={"main": {"type": "inmem"}})
        workspace = hb.HeavenBase("project", context=context, backends={"main": {"type": "inmem"}})

        assert context.active_workspace_id is None
        assert WorkspaceSpec.load("project", context=context).id == "project"
        assert hb.HeavenBase.default(context=context) is base
        assert hb.HeavenBase.load(context=context) is base

        assert workspace.activate() is workspace
        assert context.active_workspace_id == "project"
        assert hb.HeavenBase.default(context=context) is base
        assert hb.HeavenBase.load(context=context) is workspace

        assert workspace.deactivate() is workspace
        assert context.active_workspace_id is None
        assert hb.HeavenBase.default(context=context) is base
        assert hb.HeavenBase.load(context=context) is base
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_extension_defaults_are_snapshotted_as_requested_roots(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        context.config.set("heavenbase.extensions.default", ["prompt"])
        workspace = hb.HeavenBase(
            "extension-root-snapshot",
            context=context,
            backends={"main": {"type": "inmem"}},
        )
        created = WorkspaceSpec.load(workspace.id, context=context)

        assert created.extension_roots == ("prompt",)
        assert sorted(workspace.extensions()) == ["capsule", "prompt"]

        context.config.set("heavenbase.extensions.default", ["toolkit"])
        workspace.release()
        reopened = hb.HeavenBase.load("extension-root-snapshot", context=context)
        loaded = WorkspaceSpec.load(reopened.id, context=context)

        assert loaded.extension_roots == ("prompt",)
        assert sorted(reopened.extensions()) == ["capsule", "prompt"]
    finally:
        context.close()


@pytest.mark.fast
def test_manual_extension_activation_persists_only_the_requested_root(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        context.config.set("heavenbase.extensions.default", [])
        workspace = hb.HeavenBase(
            "manual-extension-root",
            context=context,
            backends={"main": {"type": "inmem"}},
        )

        workspace.enable_extension("toolkit")
        persisted = WorkspaceSpec.load(workspace.id, context=context)

        assert persisted.extension_roots == ("toolkit",)
        assert sorted(workspace.extensions()) == ["capsule", "toolkit"]

        workspace.release()
        reopened = hb.HeavenBase.load(workspace.id, context=context)
        assert WorkspaceSpec.load(reopened.id, context=context).extension_roots == ("toolkit",)
        assert sorted(reopened.extensions()) == ["capsule", "toolkit"]
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_extension_roots_are_monotonic_across_same_id_facades(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        context.config.set("heavenbase.extensions.default", [])
        first = hb.HeavenBase(
            "monotonic-extension-roots",
            context=context,
            backends={"main": {"type": "inmem"}},
        )
        stale = hb.HeavenBase(
            "monotonic-extension-roots",
            context=context,
            backends={"main": {"type": "inmem"}},
        )

        first.enable_extension("memory")
        stale.enable_extension("prompt")

        persisted = WorkspaceSpec.load(first.id, context=context)
        assert persisted.extension_roots == ("memory", "prompt")
        assert stale._extension_service.roots() == ("memory", "prompt")
        assert sorted(stale.extensions()) == ["capsule", "memory", "prompt"]
    finally:
        context.close()


@pytest.mark.fast
def test_direct_workspace_registration_serializes_monotonic_root_updates(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        context.config.set("heavenbase.extensions.default", [])
        workspace = hb.HeavenBase(
            "concurrent-extension-roots",
            context=context,
            backends={"main": {"type": "inmem"}},
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            updates = [
                executor.submit(
                    WorkspaceSpec.register,
                    workspace.id,
                    extension_roots=[extension_id],
                    context=context,
                )
                for extension_id in ("memory", "prompt")
            ]
            for update in updates:
                update.result()

        persisted = WorkspaceSpec.load(workspace.id, context=context)
        assert set(persisted.extension_roots or ()) == {"memory", "prompt"}
        assert len(persisted.extension_roots or ()) == 2
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_spec_open_detached_replays_durable_extension_roots(tmp_path):
    context = _isolated_context(tmp_path)
    detached = None
    try:
        context.config.set("heavenbase.extensions.default", [])
        source = hb.HeavenBase(
            "detached-extension-roots",
            context=context,
            backends={"main": {"type": "inmem"}},
        )
        source.enable_extension("memory")
        spec = WorkspaceSpec.load(source.id, context=context)
        context.config.set("heavenbase.extensions.default", ["prompt"])

        detached = spec.open_detached(context=context)

        assert detached._extension_service.roots() == ("memory",)
        assert sorted(detached.extensions()) == ["capsule", "memory"]
    finally:
        if detached is not None:
            detached.release()
        context.close()


@pytest.mark.fast
def test_workspace_id_scope_drives_implicit_construction_and_reopen(tmp_path):
    context = _isolated_context(tmp_path)
    scope = f"{context.config.base_scope}.scoped-build"
    try:
        context.config.set("heavenbase.backends", {"first": {"type": "inmem"}}, scope=scope)

        workspace = hb.HeavenBase("scoped-build", context=context)

        assert workspace.backends.names() == ["first"]
        assert context.config.scope == context.config.base_scope

        context.config.set("heavenbase.backends", {"second": {"type": "inmem"}}, scope=scope)
        assert workspace.backends.names() == ["first"]

        workspace.release()
        reopened = WorkspaceSpec.load("scoped-build", context=context).open(context=context)

        assert reopened.backends.names() == ["second"]
        assert context.config.scope == context.config.base_scope
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_terminal_calls_observe_live_id_scope_without_leaking(tmp_path):
    context = _isolated_context(tmp_path)
    alpha_scope = f"{context.config.base_scope}.alpha"
    beta_scope = f"{context.config.base_scope}.beta"
    try:
        context.config.set("heavenbase.entity.id.hash_length", 45, scope=alpha_scope)
        context.config.set("heavenbase.entity.id.hash_length", 50, scope=beta_scope)
        alpha = hb.HeavenBase("alpha", context=context, backends={"main": {"type": "inmem"}})
        beta = hb.HeavenBase("beta", context=context, backends={"main": {"type": "inmem"}})
        alpha.register(WorkspaceScopedNote)
        beta.register(WorkspaceScopedNote)

        assert len(alpha.upsert(WorkspaceScopedNote, {"name": "alpha-one"})) == 45
        assert context.config.scope == context.config.base_scope
        assert len(beta.upsert(WorkspaceScopedNote, {"name": "beta-one"})) == 50
        assert context.config.scope == context.config.base_scope

        context.config.set("heavenbase.entity.id.hash_length", 0, scope=alpha_scope)
        with pytest.raises(ValueError, match="hash_id length must be positive"):
            alpha.upsert(WorkspaceScopedNote, {"name": "alpha-invalid"})
        assert context.config.scope == context.config.base_scope
        assert len(beta.upsert(WorkspaceScopedNote, {"name": "beta-two"})) == 50

        context.config.set("heavenbase.entity.id.hash_length", 55, scope=alpha_scope)
        assert len(alpha.upsert(WorkspaceScopedNote, {"name": "alpha-two"})) == 55
        assert len(beta.upsert(WorkspaceScopedNote, {"name": "beta-three"})) == 50
        assert context.config.scope == context.config.base_scope
    finally:
        context.close()


@pytest.mark.fast
def test_query_terminal_rechecks_workspace_policy_after_builder_creation(tmp_path):
    context = _isolated_context(tmp_path)
    scope = f"{context.config.base_scope}.query-policy"
    try:
        context.config.set("heavenbase.query.graph.max_hops", 3, scope=scope)
        workspace = hb.HeavenBase("query-policy", context=context, backends={"main": {"type": "inmem"}})
        workspace.register(WorkspaceScopedNote)
        builder = workspace.query(WorkspaceScopedNote).traverse("links", "note-1", max_hops=2)

        context.config.set("heavenbase.query.graph.max_hops", 1, scope=scope)

        with pytest.raises(ValueError, match="max_hops 2 exceeds configured maximum 1"):
            builder.execute()
        assert context.config.scope == context.config.base_scope
    finally:
        context.close()


@pytest.mark.fast
def test_context_control_plane_ignores_workspace_scope_and_runs_registry_backend_at_base(tmp_path, monkeypatch):
    context = _isolated_context(tmp_path)
    base_scope = context.config.base_scope
    workspace_scope = f"{base_scope}.control-plane-probe"
    try:
        context.config.set("heavenbase.registry.active.workspace", "base-active", scope=base_scope)
        context.config.set("heavenbase.registry.default_workspace", "base-default", scope=base_scope)
        context.config.set("heavenbase.registry.active.workspace", "shadow-active", scope=workspace_scope)
        context.config.set("heavenbase.registry.default_workspace", "shadow-default", scope=workspace_scope)

        backend = context._control_backend()
        observed = []
        for method_name in ("ensure", "get_many", "compare_upsert"):
            original = getattr(backend, method_name)

            def record_scope(*args, _method_name=method_name, _original=original, **kwargs):
                observed.append((_method_name, context.config.scope))
                return _original(*args, **kwargs)

            monkeypatch.setattr(backend, method_name, record_scope)

        with context.config.scoped(workspace_scope):
            assert context.active_workspace_id == "base-active"
            assert context.default_workspace_id == "base-default"
            assert context.effective_workspace_id == "base-active"
            context.registry("control-plane-scope-probe").create("value", 1)

        assert {name for name, _scope in observed} == {"ensure", "get_many", "compare_upsert"}
        assert {scope for _name, scope in observed} == {base_scope}
        assert context.config.scope == base_scope
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_mcp_callable_reenters_id_scope_in_worker_thread(tmp_path):
    context = _isolated_context(tmp_path)
    base_scope = context.config.base_scope
    workspace_scope = f"{base_scope}.thread-mcp"
    try:
        context.config.set("heavenbase.entity.id.hash_length", 19, scope=base_scope)
        context.config.set("heavenbase.entity.id.hash_length", 47, scope=workspace_scope)
        workspace = hb.HeavenBase("thread-mcp", context=context, backends={"main": {"type": "inmem"}})
        workspace.enable_extension("toolkit")
        workspace.register(WorkspaceScopedNote)
        toolkit = workspace.to_mcp(name="thread-mcp", tools=["upsert"])

        def invoke():
            before = context.config.scope
            result = toolkit.run("upsert", entity=WorkspaceScopedNote.identifier, row={"name": "from-worker"})
            return before, result, context.config.scope

        with ThreadPoolExecutor(max_workers=1) as executor:
            before, result, after = executor.submit(invoke).result()

        assert before == base_scope
        assert len(result["object_id"]) == 47
        assert result["row"]["name"] == "from-worker"
        assert after == base_scope
        assert context.config.scope == base_scope
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_mcp_serializer_reenters_id_scope_in_worker_threads(tmp_path):
    context = _isolated_context(tmp_path)
    base_scope = context.config.base_scope
    workspace_scope = f"{base_scope}.thread-mcp-serializer"
    try:
        workspace = hb.HeavenBase("thread-mcp-serializer", context=context, backends={"main": {"type": "inmem"}})
        workspace.enable_extension("toolkit")
        serializer_name = "workspace-scope-observer"
        profile_name = "workspace-scope-serializer"
        hb.ext.ToolSerializer(serializer_name, _workspace_scope_serializer).register(
            replace=True,
            resolver=workspace.module_resolver,
        )
        hb.ext.register_profile(
            hb.ext.ProfileSpec(
                name=profile_name,
                tools=("list_entities",),
                serializer=serializer_name,
            ),
            replace=True,
            resolver=workspace.module_resolver,
        )
        toolkit = workspace.to_mcp(name="thread-mcp-serializer", profile=profile_name)

        def invoke_sync():
            before = context.config.scope
            result = toolkit.run_to_str("list_entities")
            return before, result, context.config.scope

        def invoke_async():
            before = context.config.scope
            result = asyncio.run(toolkit.arun_to_str("list_entities"))
            return before, result, context.config.scope

        with ThreadPoolExecutor(max_workers=2) as executor:
            sync_future = executor.submit(invoke_sync)
            async_future = executor.submit(invoke_async)
            sync_result = sync_future.result()
            async_result = async_future.result()

        assert sync_result == (base_scope, workspace_scope, base_scope)
        assert async_result == (base_scope, workspace_scope, base_scope)
        assert context.config.scope == base_scope
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_has_no_process_default_mutator():
    assert not hasattr(hb.HeavenBase, "set_default")
