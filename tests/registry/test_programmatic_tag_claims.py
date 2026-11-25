"""Exact Tag claims authored by programmatic Registry facades."""

from __future__ import annotations

import pytest

from heavenbase.capsule.layers import CapsuleLayerBuildContext, register_capsule_layer
from heavenbase.extensions.memory.api import _memory_profiles
from heavenbase.interop.mapping import InteropReport, InteropTypeInferer, register_interop_type_inferer
from heavenbase.llm.gateway import LLMGateway
from heavenbase.modules import ModuleService
from heavenbase.schema.text import register_schema_text
from heavenbase.toolkit.families.family import WorkspaceToolkitFamilySpec
from heavenbase.toolkit.families.registry import register_workspace_toolkit_family
from heavenbase.toolkit.profile import ProfileSpec
from heavenbase.toolkit.profiles import register_profile
from heavenbase.toolkit.serializers import ToolSerializer
from heavenbase.utils import Any, Mapping
from heavenbase.utils.registry import Registry

pytestmark = pytest.mark.fast


def _service() -> ModuleService:
    """Return an isolated writable module authority."""
    return ModuleService(Registry("system-modules"))


def _serialize(output: Any, *, config: Any | None = None) -> str:
    """Render a test tool result."""
    return str(output)


def _tool() -> str:
    """Return one test tool result."""
    return "tool"


def _tool_builder(workspace: Any) -> Mapping[str, Any]:
    """Build one test Toolkit family."""
    return {"tag-probe": _tool}


def _capsule_builder(context: CapsuleLayerBuildContext) -> Mapping[str, Any] | None:
    """Decline to capture one test Capsule layer."""
    return None


def _infer_type(
    source: str,
    values: list[Any],
    hint: Any,
    report: InteropReport,
) -> None:
    """Decline to infer a logical type."""
    return None


def _compile_gateway(spec: Any, gateway: str, override: Mapping[str, Any]) -> dict[str, Any]:
    """Compile one test LLM route."""
    return {}


def _render_schema(schema: Any) -> str:
    """Render one test schema."""
    return "schema"


def _inline_record(name: str, *, tags: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return one minimal inline module record."""
    meta: dict[str, Any] = {"schema_version": 1, "definition": {}}
    if tags is not None:
        meta["tags"] = dict(tags)
    return {
        "name": name,
        "desc": f"{name} test record.",
        "source": "inline",
        "target": "definition",
        "active": True,
        "meta": meta,
    }


def test_programmatic_facades_author_exact_tags_without_semantic_groups():
    service = _service()
    ToolSerializer("tag-probe", _serialize).register(resolver=service)
    register_workspace_toolkit_family(
        WorkspaceToolkitFamilySpec("tag-probe", ("tag-probe",), _tool_builder),
        resolver=service,
    )
    register_profile(ProfileSpec(name="tag-probe", tools=("tag-probe",)), resolver=service)
    register_capsule_layer("tag-probe", builder=_capsule_builder, resolver=service)
    register_interop_type_inferer(InteropTypeInferer(_infer_type, "tag-probe"), resolver=service)
    LLMGateway("tag-probe", _compile_gateway).register(resolver=service)
    register_schema_text("tag-probe", _render_schema, resolver=service)

    expected = {
        ("serializer", "tag-probe"): {"toolkit": True, "mcp": True},
        ("toolkit_family", "tag-probe"): {"toolkit": True, "mcp": True},
        ("mcp_profile", "tag-probe"): {"toolkit": True, "mcp": True},
        ("capsule_layer", "tag-probe"): {"capsule": True, "executable": True},
        ("interop_inferer", "tag-probe"): {},
        ("llm_gateway", "tag-probe"): {"llm": True},
        ("schema_text", "tag-probe"): {"schema": True},
    }
    for (kind, identifier), claims in expected.items():
        assert service.tags(kind, identifier).raw == claims
        assert "family" not in service.inspect(kind, identifier)[0]["groups"]


def test_memory_profile_discovery_reads_exact_claims_from_the_exact_kind():
    service = _service()
    service.register("mcp_profile", "memory-tagged", _inline_record("memory-tagged", tags={"memory": True}))
    service.register("mcp_profile", "ordinary", _inline_record("ordinary", tags={"memory": False}))
    service.register("toolkit_family", "wrong-kind", _inline_record("wrong-kind", tags={"memory": True}))

    assert _memory_profiles(service) == ("memory-tagged",)
