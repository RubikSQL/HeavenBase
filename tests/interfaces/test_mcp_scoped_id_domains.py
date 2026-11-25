"""A scoped MCP profile skips visible entities whose object_id domain cannot hold an id."""

import pytest

import heavenbase as hb
from heavenbase.entity.registry import load_entity_artifact_export

pytestmark = pytest.mark.fast


class ProductCode(hb.Entity):
    identifier = "scoped-domain-product-code"

    object_id = hb.field(hb.Identifier(max_chars=8)).pk()
    name = hb.field(hb.ShortText)


class Report(hb.Entity):
    identifier = "scoped-domain-report"

    title = hb.field(hb.ShortText)


def _tool(ws, method):
    """Build one MCP workspace tool whose profile sees product codes and reports."""
    profile_type = load_entity_artifact_export("sys-toolkit", "profile", "ProfileSpec")
    context_type = load_entity_artifact_export("sys-toolkit", "families.workspace.context", "WorkspaceToolContext")
    build_tool = load_entity_artifact_export("sys-toolkit", f"families.workspace.tools.{method}", "build_tool")
    profile = profile_type(name="catalog-desk", tools=(method,), entities=(ProductCode.identifier, Report.identifier))
    return build_tool(context_type(ws, profile, (method,)))


@pytest.fixture
def ws():
    ws = hb.HeavenBase("scoped-domains", detached=True, backends={"main": {"type": "inmem"}})
    try:
        ws.register(ProductCode, catalog="none")
        ws.register(Report, catalog="none")
        ws.upsert(Report, {"object_id": "report-2026-q3", "title": "Q3 results"})
        ws.upsert(ProductCode, {"object_id": "pc-1", "name": "Desk lamp"})
        yield ws
    finally:
        ws.release()


@pytest.mark.slow
def test_scoped_tools_resolve_an_id_only_the_wider_entity_can_hold(ws):
    assert _tool(ws, "get")("report-2026-q3")["row"] == {"object_id": "report-2026-q3", "title": "Q3 results"}
    assert _tool(ws, "exists")("report-2026-q3")["exists"] is True
    _tool(ws, "set")({"object_id": "report-2026-q3", "title": "Q3 results, revised"})
    assert ws.get("report-2026-q3", entity=Report)["title"] == "Q3 results, revised"
    assert _tool(ws, "delete")("report-2026-q3")["deleted"] is True
    assert ws.get("report-2026-q3", entity=Report) is None
    assert _tool(ws, "delete")("report-2026-q4")["deleted"] is False


def test_scoped_tools_still_resolve_an_id_both_entities_accept(ws):
    assert _tool(ws, "get")("pc-1")["row"] == {"object_id": "pc-1", "name": "Desk lamp"}
    assert _tool(ws, "delete")("pc-1")["deleted"] is True
    assert ws.get("pc-1", entity=ProductCode) is None


def test_scoped_tools_reject_an_id_no_visible_entity_can_hold(ws):
    with pytest.raises(RuntimeError, match="exceeds 63 characters"):
        _tool(ws, "get")("r" * 64)
