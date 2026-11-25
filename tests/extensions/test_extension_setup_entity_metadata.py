"""Workspace extension metadata includes entities contributed during setup."""

import pytest

import heavenbase as hb
from heavenbase.extensions.python import python_namespace, run_python_code

pytestmark = pytest.mark.fast


class Session(hb.Entity):
    identifier = "r9-caller-session"

    title = hb.field(hb.ShortText)


def _addon_sessions(workspace):
    return ({"entity_id": "r9-addon-session", "name": "Session", "fields": {"title": {"type": "ShortText"}}},)


def test_setup_hook_entity_does_not_shadow_caller_entity(monkeypatch):
    get = hb.CM_HVNB.get

    def config_get(key, *args, **kwargs):
        return [] if key == "heavenbase.extensions.default" else get(key, *args, **kwargs)

    monkeypatch.setitem(vars(hb.CM_HVNB), "get", config_get)
    extension = hb.ext.Extension("r9-python-addon", setup=_addon_sessions).register()
    ws = hb.HeavenBase("r9-python-addon", detached=True, backends={"main": {"type": "inmem"}})
    try:
        ws.enable_extension(extension.identifier)
        ws.register(Session)
        ws.upsert(Session, {"object_id": "caller", "title": "mine"})

        result = run_python_code("ws.count(Session)", python_namespace(ws))

        assert result["ok"] is True
        assert result["result"] == 1
    finally:
        ws.release()


def _setup_notes(workspace):
    return ({"entity_id": "r9-addon-hidden-note", "name": "HiddenNote", "fields": {"title": {"type": "ShortText"}}},)


def test_workspace_extension_reports_and_hides_its_setup_entities():
    extension = hb.ext.Extension("r9-python-hidden-addon", setup=_setup_notes, meta={"hide_from_list_entities": True}).register()
    ws = hb.HeavenBase("r9-python-hidden-addon", detached=True, backends={"main": {"type": "inmem"}})
    try:
        ws.enable_extension(extension.identifier)
        assert extension.entity_ids() == []
        assert "r9-addon-hidden-note" in ws.entities
        assert "r9-addon-hidden-note" not in ws.list_entities()
        assert ws.extensions()[extension.identifier]["entities"] == ["r9-addon-hidden-note"]
    finally:
        ws.release()
