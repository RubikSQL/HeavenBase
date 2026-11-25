"""MCP helper authority contracts for isolated Contexts."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import heavenbase as hb
import pytest
from heavenbase.entity.registry import load_entity_artifact_export
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast


def _bootstrap(root) -> dict:
    """Return an isolated in-memory control-plane bootstrap."""
    return {
        "version": 1,
        "root": str(root),
        "workspace": "default",
        "backend": {
            "type": "inmem",
            "module": "heavenbase.backends.inmem.backend",
            "name": "system",
        },
        "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
    }


def _context(root) -> hb.Context:
    """Create one Context without process-default configuration state."""
    return hb.Context.load(_bootstrap(root), config=ConfigManager(default={}, setup=False))


def _backend_scope_serializer(output, *, config) -> str:
    """Report tool and serializer scopes for backend MCP boundary tests."""
    return f"{output}|{config.scope}"


def _toolkit_export(resolver, module: str, name: str):
    """Resolve one helper from a Context's installed Toolkit artifact."""
    return load_entity_artifact_export("sys-toolkit", module, name, resolver=resolver)


def test_direct_mcp_helpers_inherit_the_owning_workspace_and_backend_resolver(tmp_path):
    """Public MCP helpers must not replace a live owner's Context with DEFAULT_CONTEXT."""
    first = _context(tmp_path / "first")
    second = _context(tmp_path / "second")
    first_workspace = None
    second_workspace = None
    try:
        first._setup_modules()
        second._setup_modules()
        first_resolver = first.modules()
        second_resolver = second.modules()
        first_profile_type = _toolkit_export(first_resolver, "profile", "ProfileSpec")
        second_profile_type = _toolkit_export(second_resolver, "profile", "ProfileSpec")
        _toolkit_export(first_resolver, "profiles", "register_profile")(
            first_profile_type("owner-probe", tools=("query",), serializer="jstr"),
            resolver=first.modules(),
        )
        _toolkit_export(second_resolver, "profiles", "register_profile")(
            second_profile_type("owner-probe", tools=("count",), serializer="markdown"),
            resolver=second.modules(),
        )
        first_workspace = hb.HeavenBase("mcp-owner", context=first, backends={"main": {"type": "inmem"}})
        second_workspace = hb.HeavenBase("mcp-owner", context=second, backends={"main": {"type": "inmem"}})
        for workspace in (first_workspace, second_workspace):
            workspace.enable_extension("toolkit")
            workspace.enable_extension("memory")

        first_toolkit = _toolkit_export(first_resolver, "workspace_tools", "workspace_toolkit")(first_workspace, profile="owner-probe")
        second_toolkit = _toolkit_export(second_resolver, "workspace_tools", "workspace_toolkit")(second_workspace, profile="owner-probe")
        first_backend_toolkit = _toolkit_export(first_resolver, "backend_tools", "backend_toolkit")(first_workspace.backends.get("main"))
        second_backend_toolkit = _toolkit_export(second_resolver, "backend_tools", "backend_toolkit")(second_workspace.backends.get("main"))

        assert first_toolkit.list_tools() == ["query", "explain"]
        assert second_toolkit.list_tools() == ["count"]
        assert first_toolkit._resolver is first.modules()
        assert second_toolkit._resolver is second.modules()
        assert first_backend_toolkit._resolver is first.modules()
        assert second_backend_toolkit._resolver is second.modules()
        assert first_workspace.memory._resolver is first.modules()
        assert second_workspace.memory._resolver is second.modules()
    finally:
        if first_workspace is not None:
            first_workspace.release()
        if second_workspace is not None:
            second_workspace.release()
        first.close()
        second.close()


def test_workspace_bound_backend_mcp_reenters_scope_in_worker_threads(tmp_path):
    """Backend tools and serializers should use their bound workspace scope."""
    context = _context(tmp_path / "bound-backend")
    workspace = None
    try:
        workspace = hb.HeavenBase("backend-scope", context=context, backends={"main": {"type": "inmem"}})
        workspace.enable_extension("toolkit")
        backend = workspace.backends.get("main")
        serializer_name = "backend-scope-observer"
        ToolSerializer = _toolkit_export(workspace.module_resolver, "serializers", "ToolSerializer")
        ToolSerializer(serializer_name, _backend_scope_serializer).register(
            replace=True,
            resolver=workspace.module_resolver,
        )
        original_mcp = backend.mcp

        def scoped_mcp():
            spec = original_mcp()
            spec.serializer = serializer_name
            spec.tool("config_scope", lambda: backend.config.scope)
            spec.profile("scope", ("config_scope",))
            return spec

        backend.mcp = scoped_mcp
        toolkit = workspace.to_mcp(backend="main", profile="scope")
        base_scope = context.config.base_scope
        workspace_scope = f"{base_scope}.{workspace.id}"

        def invoke_sync():
            before = context.config.scope
            result = toolkit.run_to_str("config_scope")
            return before, result, context.config.scope

        def invoke_async():
            before = context.config.scope
            result = asyncio.run(toolkit.arun_to_str("config_scope"))
            return before, result, context.config.scope

        with ThreadPoolExecutor(max_workers=2) as executor:
            sync_future = executor.submit(invoke_sync)
            async_future = executor.submit(invoke_async)
            sync_result = sync_future.result()
            async_result = async_future.result()

        expected = f"{workspace_scope}|{workspace_scope}"
        assert sync_result == (base_scope, expected, base_scope)
        assert async_result == (base_scope, expected, base_scope)
        assert context.config.scope == base_scope
    finally:
        if workspace is not None:
            workspace.release()
        context.close()


def test_unbound_backend_mcp_preserves_caller_scope_in_worker_thread():
    """A genuinely standalone Backend should not invent workspace scoping."""
    backend = hb.backends.InMemBackend("standalone-scope")
    original_mcp = backend.mcp

    def standalone_mcp():
        spec = original_mcp()
        spec.tool("config_scope", lambda: backend.config.scope)
        spec.profile("scope", ("config_scope",))
        return spec

    backend.mcp = standalone_mcp
    toolkit = backend.to_mcp(profile="scope")

    def invoke():
        with hb.CM_HVNB.scoped("standalone-caller"):
            before = hb.CM_HVNB.scope
            result = toolkit.run("config_scope")
            return before, result, hb.CM_HVNB.scope

    with ThreadPoolExecutor(max_workers=1) as executor:
        before, result, after = executor.submit(invoke).result()

    expected = f"{hb.CM_HVNB.base_scope}.standalone-caller"
    assert (before, result, after) == (expected, expected, expected)
    assert hb.CM_HVNB.scope == hb.CM_HVNB.base_scope
