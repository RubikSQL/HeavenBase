import heavenbase as hb
import pytest

from heavenbase.workspace import WorkspaceSpec

pytestmark = pytest.mark.fast


def test_tracked_workspace_persists_a_json_default_type_change(tmp_path):
    entity_id = "r9ws-manifest-note"
    old = {"entity_id": entity_id, "fields": {"settings": {"type": "json", "default": {"visible": False}}}}
    new = {"entity_id": entity_id, "fields": {"settings": {"type": "json", "default": {"visible": 0}}}}
    manifest = {
        "kind": "heavenbase.workspace.manifest",
        "version": 2,
        "id": "r9ws-manifest",
        "construction": {
            "kind": "backends",
            "config": {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "manifest.db")}},
        },
        "entities": [old],
    }

    workspace = hb.HeavenBase.from_manifest(manifest)
    try:
        workspace.register(new, allow_replace=True)
        saved = WorkspaceSpec.load(workspace.id, context=workspace.context).manifest
        assert type(saved["entities"][0]["fields"]["settings"]["default"]["visible"]) is int
    finally:
        workspace.release()
        WorkspaceSpec.unregister(manifest["id"])
