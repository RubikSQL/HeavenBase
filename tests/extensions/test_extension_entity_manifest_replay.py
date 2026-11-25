"""Workspace manifests replay extension setup through the extension root."""

import copy
import warnings

import pytest

import heavenbase as hb
from heavenbase.workspace import WorkspaceSpec

pytestmark = pytest.mark.fast


def _setup_records(workspace):
    return (
        {
            "entity_id": "r9-addon-manifest-record",
            "name": "AddonRecord",
            "fields": {"title": {"type": "ShortText"}, "settings": {"type": "Json", "default": {"visible": False}}},
        },
    )


def _record_definition(manifest):
    return next(entity for entity in manifest["entities"] if entity["entity_id"] == "r9-addon-manifest-record")


@pytest.mark.slow
def test_default_manifest_leaves_setup_entities_to_activation():
    extension = hb.ext.Extension("r9-manifest-addon", setup=_setup_records).register()
    ws = hb.HeavenBase("r9-manifest-addon", detached=True, backends={"main": {"type": "inmem"}})
    try:
        ws.enable_extension(extension.identifier)
        manifest = ws.to_manifest()
        assert extension.identifier in manifest["extensions"]
        assert "r9-addon-manifest-record" not in {entity["entity_id"] for entity in manifest["entities"]}
        complete = ws.to_manifest(include_system=True)
        assert "r9-addon-manifest-record" in {entity["entity_id"] for entity in complete["entities"]}
        # Registering the extension's own definition again is accepted.
        ws.register(_record_definition(complete))
    finally:
        ws.release()
    restored = hb.HeavenBase.from_manifest(complete, detached=True)
    try:
        record = restored.entities["r9-addon-manifest-record"]
        restored.upsert(record, {"object_id": "summary", "title": "Summary"})
        value = restored.get("summary", entity=record)["settings"]["visible"]
        assert value is False
    finally:
        restored.release()


def test_an_extension_entity_definition_cannot_change_after_activation():
    extension = hb.ext.Extension("r9-manifest-fixed-addon", setup=_setup_records).register()
    ws = hb.HeavenBase("r9-manifest-fixed-addon", detached=True, backends={"main": {"type": "inmem"}})
    try:
        ws.enable_extension(extension.identifier)
        before = ws.to_manifest(include_system=True)
        declaration = _setup_records(ws)[0]
        declaration["fields"]["department"] = {"type": "ShortText"}
        with pytest.raises(ValueError, match="defined by extension 'r9-manifest-fixed-addon' and cannot be altered"):
            ws.alter_entity(declaration, confirm="r9-addon-manifest-record")
        with pytest.raises(ValueError, match="defined by extension 'r9-manifest-fixed-addon'"):
            ws.register(declaration, allow_replace=True)
        with pytest.raises(ValueError, match="defined by extension 'r9-manifest-fixed-addon'"):
            ws.register(_setup_records(ws)[0], catalog="none", allow_replace=True)
        with pytest.raises(ValueError, match="defined by extension 'r9-manifest-fixed-addon' and cannot be described"):
            ws.describe("r9-addon-manifest-record", desc="Edited")
        registered = ws.entities["r9-addon-manifest-record"]
        assert ws.alter_entity(_setup_records(ws)[0], confirm="r9-addon-manifest-record") is registered
        assert ws.to_manifest(include_system=True) == before
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("allow_replace", [False, True])
def test_a_manifest_cannot_redefine_an_extension_entity(allow_replace):
    extension = hb.ext.Extension("manifest-settings-addon", setup=_setup_records).register()
    ws = hb.HeavenBase("manifest-settings-addon", detached=True, backends={"main": {"type": "inmem"}})
    try:
        ws.enable_extension(extension.identifier)
        edited = ws.to_manifest(include_system=True)
    finally:
        ws.release()
    # A JSON default of the same value but another JSON type is a different definition.
    _record_definition(edited)["fields"]["settings"]["default"] = {"visible": 0}

    with pytest.raises(ValueError, match="defined by extension 'manifest-settings-addon'"):
        hb.HeavenBase.from_manifest(edited, detached=True, allow_replace=allow_replace)


@pytest.mark.slow
def test_a_manifest_keeps_the_extension_descriptions_of_its_entities(tmp_path):
    """An upgrade can change an extension's descriptions after a manifest saved them; replay leaves them to the extension."""
    extension = hb.ext.Extension("manifest-described-addon", setup=_setup_records).register()
    backends = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "described.db")}}
    ws = hb.HeavenBase("manifest-described-addon", detached=True, backends=backends)
    try:
        ws.enable_extension(extension.identifier)
        exported = ws.to_manifest(include_system=True)
        current = copy.deepcopy(_record_definition(exported))
    finally:
        ws.release()
    edited = _record_definition(exported)
    edited["desc"] = "Records described by an earlier release"
    edited["tags"] = ["earlier"]
    edited["fields"]["title"]["desc"] = "Earlier title"
    edited["fields"]["title"]["examples"] = ["Earlier"]

    restored = hb.HeavenBase.from_manifest(exported)
    try:
        assert _record_definition(restored.to_manifest(include_system=True)) == current
        restored.upsert("r9-addon-manifest-record", {"object_id": "r1", "title": "Kept"})
    finally:
        restored.release()
    saved = WorkspaceSpec.load("manifest-described-addon").manifest
    assert "r9-addon-manifest-record" not in {entity["entity_id"] for entity in saved["entities"]}
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        reopened = hb.HeavenBase("manifest-described-addon")
    try:
        assert _record_definition(reopened.to_manifest(include_system=True)) == current
        assert reopened.get("r1", entity="r9-addon-manifest-record")["title"] == "Kept"
    finally:
        reopened.release()


@pytest.mark.slow
def test_a_tracked_import_cannot_replace_an_extension_entity(tmp_path):
    """A replacement that reopening by id would undo (activation registers the extension's definition first) is refused."""
    extension = hb.ext.Extension("manifest-replaced-addon", setup=_setup_records).register()
    backends = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "replaced.db")}}
    ws = hb.HeavenBase("manifest-replaced-addon", detached=True, backends=backends)
    try:
        ws.enable_extension(extension.identifier)
        exported = ws.to_manifest(include_system=True)
    finally:
        ws.release()
    _record_definition(exported)["fields"]["department"] = {"type": "ShortText"}

    with pytest.raises(ValueError, match="defined by extension 'manifest-replaced-addon'"):
        hb.HeavenBase.from_manifest(exported, allow_replace=True)
    with pytest.raises(KeyError):
        WorkspaceSpec.load("manifest-replaced-addon")


@pytest.mark.slow
def test_an_import_names_the_extension_metadata_it_does_not_apply(tmp_path):
    """The extension keeps its metadata, and a manifest that states others is told so; its own export imports silently."""
    extension = hb.ext.Extension("manifest-noted-addon", setup=_setup_records).register()
    backends = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "noted.db")}}
    ws = hb.HeavenBase("manifest-noted-addon", detached=True, backends=backends)
    try:
        ws.enable_extension(extension.identifier)
        exported = ws.to_manifest(include_system=True)
    finally:
        ws.release()
    current = copy.deepcopy(_record_definition(exported))
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        hb.HeavenBase.from_manifest(exported, detached=True).release()
    record = _record_definition(exported)
    record.update(desc="Records described by an earlier release", tags=["earlier"])
    record["fields"]["title"].update(desc="Earlier title", examples=["Earlier"])
    with pytest.warns(UserWarning, match="manifest's description, tags, description of field 'title', examples of field 'title' for it were not applied"):
        restored = hb.HeavenBase.from_manifest(exported, detached=True)
    try:
        assert _record_definition(restored.to_manifest(include_system=True)) == current
    finally:
        restored.release()
