"""A registered serializer whose Capsule a drop removed must not break unrelated profiles."""

from __future__ import annotations

from contextlib import closing
from weakref import WeakSet, WeakValueDictionary

import pytest

import heavenbase as hb
from heavenbase.capsule import registry as capsule_module
from heavenbase.capsule.registry import capsule_registry, reset_capsule_registry
from heavenbase.context import runtime
from heavenbase.entity.registry import load_entity_artifact_export
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast

_SERIALIZER = "dropped-capsule-serializer"


def labelled_text_serializer(output, *, config=None) -> str:
    """Render tool output as labelled text."""
    return f"labelled:{output}"


def _context(root) -> hb.Context:
    return hb.Context.load(
        {
            "version": 1,
            "root": str(root),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=ConfigManager(root=str(root), setup=False),
    )


def _toolkit_export(resolver, module: str, name: str):
    return load_entity_artifact_export("sys-toolkit", module, name, resolver=resolver)


@pytest.fixture
def isolated_roots(monkeypatch):
    """Keep a process-wide drop to the Contexts the test builds, away from the worker's default Context."""
    monkeypatch.setattr(capsule_module, "_AUTHORITY_REGISTRIES", WeakValueDictionary())
    monkeypatch.setattr(capsule_module, "_DEFAULT_REGISTRY", None)
    monkeypatch.setattr(runtime, "_ROOT_CONTEXTS", WeakSet(), raising=False)


def _drop_serializer_capsule(context: hb.Context, *, process_wide: bool = False) -> None:
    """Register a Capsule-backed serializer, then drop the Context's Capsule registry under it."""
    resolver = context.modules()
    capsule = hb.Capsule(labelled_text_serializer, name=_SERIALIZER, namespace="tests", version="1")
    # No registry facade is kept: 0.1.2.2 left the Capsule in place when a process-wide drop found no live facade.
    capsule_registry(resolver=resolver, config=context.config).register(capsule, reason="serializer executable reference")
    ToolSerializer = _toolkit_export(resolver, "serializers", "ToolSerializer")
    ToolSerializer(_SERIALIZER, capsule).register(resolver=resolver)
    assert ToolSerializer.load(_SERIALIZER, resolver=resolver)("value") == "labelled:value"
    if process_wide:
        reset_capsule_registry(drop=True)
    else:
        reset_capsule_registry(drop=True, resolver=resolver, config=context.config)
    assert resolver.inspect("serializer", _SERIALIZER), "the serializer row outlives its Capsule"


@pytest.mark.slow
@pytest.mark.parametrize("process_wide", [False, True], ids=["scoped-drop", "process-wide-drop"])
def test_profile_resolution_ignores_an_unrelated_serializer_whose_capsule_was_dropped(tmp_path, isolated_roots, process_wide):
    with closing(_context(tmp_path / "ctx")) as context:
        context._setup_modules()
        _drop_serializer_capsule(context, process_wide=process_wide)
        resolver = context.modules()
        resolve_profile = _toolkit_export(resolver, "profiles", "resolve_profile")

        assert resolve_profile("agent", resolver=resolver, config=context.config).serializer == "markdown"
        assert resolve_profile("full", resolver=resolver, config=context.config).serializer == "jstr"


@pytest.mark.slow
def test_workspace_toolkit_ignores_an_unrelated_serializer_whose_capsule_was_dropped(tmp_path):
    with closing(_context(tmp_path / "ctx")) as context:
        context._setup_modules()
        workspace = hb.HeavenBase("dangling-serializer", context=context, backends={"main": {"type": "inmem"}})
        try:
            workspace.enable_extension("toolkit")
            _drop_serializer_capsule(context)
            toolkit = workspace.to_mcp(name="dangling-serializer", profile="agent")
            assert "query" in toolkit.list_tools()
            # The dangling serializer itself still reports that its Capsule is gone.
            ToolSerializer = _toolkit_export(context.modules(), "serializers", "ToolSerializer")
            with pytest.raises(ImportError, match="unavailable"):
                ToolSerializer.load(_SERIALIZER, resolver=context.modules())
        finally:
            workspace.release()


@pytest.mark.slow
@pytest.mark.parametrize("by_reference", [False, True], ids=["by-name", "by-reference"])
def test_workspace_serializer_alias_ignores_an_unrelated_serializer_whose_capsule_was_dropped(tmp_path, isolated_roots, by_reference):
    with closing(_context(tmp_path / "ctx")) as context:
        context._setup_modules()
        workspace = hb.HeavenBase("dangling-serializer-alias", context=context, backends={"main": {"type": "inmem"}})
        try:
            workspace.enable_extension("toolkit")
            resolver = context.modules()
            target = "markdown"
            if by_reference:
                target = _toolkit_export(resolver, "serializers", "ToolSerializer").load("markdown", resolver=resolver).to_ref()
            workspace.set(
                {
                    "object_id": "mcp_serializer:aliased-markdown",
                    "kind": "mcp_serializer",
                    "subject_id": "aliased-markdown",
                    "name": "aliased-markdown",
                    "type": "serializer",
                    "target": target,
                    "active": True,
                    "ws": workspace.id,
                    "meta": {"name": "aliased-markdown", "serializer": target},
                },
                entity=hb.MetaSchema,
            )
            _drop_serializer_capsule(context)
            workspace.to_mcp(name="dangling-serializer-alias", profile="agent")
            row = workspace.get("mcp_serializer:aliased-markdown", entity=hb.MetaSchema)
            assert row["meta"]["alias_of"] == "markdown"
        finally:
            workspace.release()
