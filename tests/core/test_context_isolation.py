"""End-to-end isolation checks for independent HeavenBase Contexts."""

from __future__ import annotations

import json

import heavenbase as hb
import pytest
from heavenbase.utils.config import ConfigManager
from heavenbase.database.dialect import DatabaseDialectSpec, database_dialects
from heavenbase.workspace import WorkspaceSpec

pytestmark = pytest.mark.fast


class _ContextAutoId(hb.Entity):
    """Entity used to prove Context-owned automatic identifier policy."""

    identifier = "context-auto-id"

    name = hb.field(hb.ShortText)


def _bootstrap(root) -> dict:
    """Return an in-memory control-plane bootstrap rooted below ``root``."""
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
    """Create an isolated Context with no process-default config state."""
    return hb.Context.load(_bootstrap(root), config=ConfigManager(default={}, setup=False))


def test_contexts_isolate_modules_backends_and_same_id_workspaces(tmp_path):
    first = _context(tmp_path / "first")
    second = _context(tmp_path / "second")
    first_workspace = None
    second_workspace = None
    try:
        first._setup_modules()
        second._setup_modules()
        database_dialects.register(
            DatabaseDialectSpec(
                "context-probe",
                sqlglot_preferred=False,
                name_warning_max_identifier_length=17,
            ),
            resolver=first.modules(),
        )
        database_dialects.register(
            DatabaseDialectSpec(
                "context-probe",
                sqlglot_preferred=False,
                name_warning_max_identifier_length=29,
            ),
            resolver=second.modules(),
        )
        first.config.set("heavenbase.mcp.transport", "sse")
        first.config.set("heavenbase.mcp.host", "127.0.0.11")
        first.config.set("heavenbase.mcp.port", 7111)
        first.config.set("heavenbase.mcp.workspace_toolkit", "first-context")
        first.config.set("heavenbase.mcp.display.max_rows", 1)
        second.config.set("heavenbase.mcp.transport", "http")
        second.config.set("heavenbase.mcp.host", "127.0.0.22")
        second.config.set("heavenbase.mcp.port", 7222)
        second.config.set("heavenbase.mcp.workspace_toolkit", "second-context")
        second.config.set("heavenbase.mcp.display.max_rows", 2)
        first.config.set("heavenbase.entity.id.hash_length", 45)
        second.config.set("heavenbase.entity.id.hash_length", 50)

        first_workspace = hb.HeavenBase("shared-id", context=first, backends={"main": {"type": "inmem"}})
        second_workspace = hb.HeavenBase("shared-id", context=second, backends={"main": {"type": "inmem"}})
        first_workspace.enable_extension("toolkit")
        second_workspace.enable_extension("toolkit")
        first_mcp = json.loads(first_workspace.to_mcp_json())
        second_mcp = json.loads(second_workspace.to_mcp_json())
        first_toolkit = first_workspace.to_mcp(name="first-tools", profile="agent")
        second_toolkit = second_workspace.to_mcp(name="second-tools", profile="agent")
        serializer_probe = {"rows": [{"value": 1}, {"value": 2}, {"value": 3}]}

        assert first_workspace is not second_workspace
        assert first_workspace.context is first
        assert second_workspace.context is second
        assert hb.HeavenBase.load("shared-id", context=first) is first_workspace
        assert hb.HeavenBase.load("shared-id", context=second) is second_workspace
        assert first_workspace.module_resolver is first.modules()
        assert second_workspace.module_resolver is second.modules()
        assert first_workspace.backends.get("main").module_resolver is first.modules()
        assert second_workspace.backends.get("main").module_resolver is second.modules()
        assert first_workspace.backends.get("main").config is first.config
        assert second_workspace.backends.get("main").config is second.config
        first_workspace.register(_ContextAutoId)
        second_workspace.register(_ContextAutoId)
        first_object_id = first_workspace.upsert(_ContextAutoId, {"name": "same-name"})
        second_object_id = second_workspace.upsert(_ContextAutoId, {"name": "same-name"})
        assert len(first_object_id) == 45
        assert len(second_object_id) == 50
        first_meta_id = first_workspace._metaschema.system_row_id("probe", "x" * 200)
        second_meta_id = second_workspace._metaschema.system_row_id("probe", "x" * 200)
        assert len(first_meta_id) == len("probe:") + 45
        assert len(second_meta_id) == len("probe:") + 50
        assert first_toolkit._resolver is first.modules()
        assert second_toolkit._resolver is second.modules()
        assert first_toolkit._config is first.config
        assert second_toolkit._config is second.config
        assert all(tool.capsule._resolver is first.modules() for tool in first_toolkit.tools.values())
        assert all(tool.capsule._resolver is second.modules() for tool in second_toolkit.tools.values())
        assert all(tool.capsule._config is first.config for tool in first_toolkit.tools.values())
        assert all(tool.capsule._config is second.config for tool in second_toolkit.tools.values())
        assert all(tool._serializer_capsule()._resolver is first.modules() for tool in first_toolkit.tools.values())
        assert all(tool._serializer_capsule()._resolver is second.modules() for tool in second_toolkit.tools.values())
        assert all(tool._serializer_capsule()._config is first.config for tool in first_toolkit.tools.values())
        assert all(tool._serializer_capsule()._config is second.config for tool in second_toolkit.tools.values())
        assert database_dialects.get("context-probe", resolver=first.modules()).name_warning_max_identifier_length == 17
        assert database_dialects.get("context-probe", resolver=second.modules()).name_warning_max_identifier_length == 29
        assert first_mcp["mcpServers"]["first-context"] == {
            "transport": "sse",
            "url": "http://127.0.0.11:7111/sse",
        }
        assert second_mcp["mcpServers"]["second-context"] == {
            "transport": "http",
            "url": "http://127.0.0.22:7222/mcp",
        }
        assert "... 2 more row(s) truncated" in first_toolkit["query"].to_str(serializer_probe)
        assert "... 1 more row(s) truncated" in second_toolkit["query"].to_str(serializer_probe)

        assert hb.HeavenBase.drop("shared-id", context=first)
        assert WorkspaceSpec.unregister("shared-id", context=first)
        first_replacement = hb.HeavenBase("shared-id", context=first, backends={"main": {"type": "inmem"}})
        assert first_replacement is not first_workspace
        first_workspace = first_replacement
        assert hb.HeavenBase.load("shared-id", context=second) is second_workspace
    finally:
        if first_workspace is not None:
            first_workspace.release()
        if second_workspace is not None:
            second_workspace.release()
        first.close()
        second.close()


def test_heavenbase_construction_ensures_compatible_workspaces_while_load_only_opens(tmp_path):
    """Construction is idempotent for compatible specs while load never creates."""
    context = _context(tmp_path / "lifecycle")
    workspace = None
    try:
        with pytest.raises(KeyError, match="missing-workspace"):
            hb.HeavenBase.load("missing-workspace", context=context)

        first = hb.HeavenBase("existing-workspace", context=context, backends={"main": {"type": "inmem"}})
        workspace = hb.HeavenBase("existing-workspace", context=context, backends={"main": {"type": "inmem"}})
        assert first.id == workspace.id == "existing-workspace"
        assert hb.HeavenBase.load("existing-workspace", context=context) is workspace
        with pytest.raises(FileExistsError, match="different construction contract"):
            hb.HeavenBase("existing-workspace", context=context, backends={"other": {"type": "inmem"}})

        assert WorkspaceSpec.unregister("existing-workspace", context=context)
        with pytest.raises(KeyError, match="existing-workspace"):
            hb.HeavenBase.load("existing-workspace", context=context)
    finally:
        if workspace is not None:
            workspace.release()
        context.close()


def test_pending_entity_binding_includes_context_authority(tmp_path):
    """A same-id workspace in another Context must not adopt pending entities."""
    first = _context(tmp_path / "pending-first")
    second = _context(tmp_path / "pending-second")

    class FirstOnly(hb.Entity):
        identifier = "context-pending-first-only"

        name = hb.field(hb.ShortText)

    try:
        FirstOnly.register("shared-pending", context=first)
        first_workspace = hb.HeavenBase("shared-pending", context=first, backends={"main": {"type": "inmem"}})
        second_workspace = hb.HeavenBase("shared-pending", context=second, backends={"main": {"type": "inmem"}})

        assert FirstOnly.identifier in first_workspace.entities
        assert FirstOnly.identifier not in second_workspace.entities
    finally:
        first.close()
        second.close()


def test_internal_detached_construction_projects_capabilities_only_through_its_context(tmp_path, monkeypatch):
    """The internal no-pending path must not fall back to the process Context."""
    context = _context(tmp_path / "capability-metadata")
    workspace = None
    try:
        context._setup_modules()
        resolver = context.modules()
        workspace = hb.HeavenBase._construct(
            "isolated-capability-metadata",
            context=context,
            backends={"main": {"type": "inmem"}},
            detached=True,
            tracked=False,
            register_workspace=False,
            register_pending=False,
            track_live=False,
            publish=False,
        )

        def reject_default_modules():
            raise AssertionError("capability metadata touched DEFAULT_CONTEXT")

        monkeypatch.setitem(vars(hb.DEFAULT_CONTEXT), "modules", reject_default_modules)

        option = hb.capabilities.CapabilityOption(
            "short-text",
            "eq",
            "inmem",
            hb.InlineColumn.identifier,
            resolver=resolver,
        )
        rows = workspace._metaschema.registry.workspace_rows()
        filter_domains = resolver.tag("logical_type", "short-text", "filter-domains")

        assert option.to_dict()["dtype"] == "short-text"
        assert filter_domains is not None
        assert filter_domains.status == "resolved"
        assert "text" in filter_domains.value
        assert filter_domains.subject == ("logical_type", "short-text")
        assert any(row["kind"] == "capability" for row in rows)
    finally:
        if workspace is not None:
            workspace.release()
        context.close()
