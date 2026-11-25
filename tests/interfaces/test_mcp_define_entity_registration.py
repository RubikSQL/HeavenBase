"""MCP ``define_entity`` registers the definition it is given, as ``HeavenBase.register`` does."""

import copy

import pytest

import heavenbase as hb

pytestmark = pytest.mark.fast


def _note_definition(entity_id, **options):
    return {
        "entity_id": entity_id,
        "fields": {"object_id": {"type": "identifier", "pk": True}, "title": {"type": "short-text"}, "labels": {"type": "array[short-text]"}},
        **options,
    }


def _exported(workspace, entity_id, **kwargs):
    return copy.deepcopy(next(item for item in workspace.to_manifest(**kwargs)["entities"] if item["entity_id"] == entity_id))


@pytest.fixture
def workspace():
    ws = hb.HeavenBase("mcp-define-entity", detached=True, backends={"main": {"type": "inmem"}})
    try:
        yield ws
    finally:
        ws.release()


def test_define_entity_keeps_the_catalog_and_gram_options_a_definition_states(workspace):
    toolkit = workspace.to_mcp(name="mcp-define-options")

    toolkit.run("define_entity", entity=_note_definition("field-note", catalog="none", gram=False))

    assert workspace.entity_options["field-note"].catalog == "none"
    assert workspace.entity_options["field-note"].gram is False
    exported = _exported(workspace, "field-note")
    assert (exported.get("catalog"), exported.get("gram")) == ("none", False)
    # The same definition through the Python API is the same registration, not a conflicting one.
    assert workspace.register(_note_definition("field-note", catalog="none", gram=False)) is workspace.entities["field-note"]


def test_define_entity_replace_applies_the_options_a_definition_states(workspace):
    toolkit = workspace.to_mcp(name="mcp-define-replace-options")
    toolkit.run("define_entity", entity=_note_definition("field-note"))
    assert workspace.entity_options["field-note"].catalog == "full"

    toolkit.run("define_entity", entity=_note_definition("field-note", catalog="identity"), replace=True)

    assert workspace.entity_options["field-note"].catalog == "identity"
    assert _exported(workspace, "field-note").get("catalog") == "identity"


@pytest.mark.slow
def test_define_entity_accepts_an_extension_entity_exported_definition(workspace):
    workspace.enable_extension("agent")
    exported = _exported(workspace, "agent-message", include_system=True)
    registered = workspace.entities["agent-message"]
    toolkit = workspace.to_mcp(name="mcp-define-extension")

    described = toolkit.run("define_entity", entity=exported)

    assert workspace.entities["agent-message"] is registered
    assert described["schema"]["entity_id"] == "agent-message"
