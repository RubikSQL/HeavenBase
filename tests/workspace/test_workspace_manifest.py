import datetime
import decimal
import json
from pathlib import Path
import warnings

import pytest

import heavenbase as hb
from heavenbase.context import load_bootstrap
from heavenbase.cli.context import PackageCLIContext
from heavenbase.cli.groups.ws.manifest import import_manifest
from heavenbase.workspace import WorkspaceManifest, WorkspaceSpec
from heavenbase.workspace.registry import _registry
from heavenbase.workspace.setup.backends import WorkspaceConstruction
from heavenbase.workspace.setup.lifecycle import _default_workspace_name, _release_workspace
from heavenbase.utils.config import ConfigManager

LIVE_WORKSPACES = hb.DEFAULT_CONTEXT._live_workspaces
WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry


def _manifest_embed(text):
    """Embed one text value as a tiny deterministic vector."""
    return [float(len(str(text or ""))), 1.0]


class _Output:
    def __init__(self):
        self.yaml_payloads = []

    def yaml(self, payload):
        self.yaml_payloads.append(payload)


def _isolated_context(tmp_path):
    config = ConfigManager(
        root=str(tmp_path),
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


def test_detached_workspace_stays_out_of_process_registry():
    hb.HeavenBase("workspace-detached-api-test", detached=True, backends={"main": {"type": "inmem"}})

    assert "workspace-detached-api-test" not in WORKSPACES


def test_workspace_construction_separates_replayable_and_runtime_inputs():
    """Construction provenance should isolate config and serialize runtime identifiers."""
    raw = {"main": {"type": "inmem"}}
    construction = WorkspaceConstruction.from_inputs(raw, None)
    raw["main"]["type"] = "changed"

    assert construction.to_dict() == {
        "kind": "backends",
        "config": {"main": {"type": "inmem"}},
    }
    assert WorkspaceConstruction.from_inputs({}, None).to_dict() == {
        "kind": "backends",
        "config": {},
    }
    assert WorkspaceConstruction.from_inputs(None, None).to_dict() == {"kind": "ambient"}
    assert WorkspaceConstruction.from_inputs(None, "debug").to_dict() == {
        "kind": "preset",
        "preset": "debug",
    }

    runtime = WorkspaceConstruction.from_inputs({"main": hb.backends.InMemBackend("main")}, None)
    assert runtime.runtime_only is True
    assert runtime.to_dict() == {
        "kind": "runtime",
        "identifiers": {"main": "inmem"},
    }

    with pytest.raises(TypeError, match="either config mappings or live Backend instances"):
        WorkspaceConstruction.from_inputs({"main": hb.backends.InMemBackend("main"), "vec": {"type": "inmem"}}, None)
    with pytest.raises(ValueError, match="unique backend names"):
        WorkspaceConstruction.from_inputs(
            {
                "left": hb.backends.InMemBackend("same"),
                "right": hb.backends.InMemBackend("same"),
            },
            None,
        )


def test_workspace_manifest_isolates_nested_constructor_values():
    """Manifest values should not retain mutable aliases from callers or exports."""
    backend_config = {"main": {"type": "inmem"}}
    construction = {"kind": "backends", "config": backend_config}
    entities = [{"entity_id": "isolated-note", "fields": {}}]
    manifest = hb.WorkspaceManifest("manifest-isolation", construction=construction, entities=entities)
    backend_config["main"]["type"] = "changed"
    entities[0]["entity_id"] = "changed"
    payload = manifest.to_dict()
    payload["construction"]["config"]["main"]["type"] = "changed-again"

    assert manifest.to_dict()["construction"] == {
        "kind": "backends",
        "config": {"main": {"type": "inmem"}},
    }
    assert manifest.entities[0]["entity_id"] == "isolated-note"

    with pytest.raises(ValueError, match="manifest kind"):
        hb.WorkspaceManifest("manifest-isolation", kind="other")
    with pytest.raises(ValueError, match="manifest version"):
        hb.WorkspaceManifest("manifest-isolation", version=1)
    with pytest.raises(ValueError, match="construction"):
        hb.WorkspaceManifest("manifest-isolation", construction={"kind": "unknown"})


@pytest.mark.fast
def test_workspace_manifest_accepts_v1_config_and_normalizes_to_v2():
    manifest = hb.WorkspaceManifest.from_dict(
        {
            "kind": "heavenbase.workspace.manifest",
            "version": 1,
            "id": "legacy-manifest",
            "config": {"backends": {"main": {"type": "inmem"}}},
            "extensions": [],
            "entities": [],
            "backend_summary": [],
        }
    )

    data = manifest.to_dict()

    assert data["version"] == 2
    assert "config" not in data
    assert data["construction"] == {
        "kind": "backends",
        "config": {"main": {"type": "inmem"}},
    }


def test_workspace_manifest_roundtrip_replays_extensions_storage_and_entities(tmp_path):
    class ManifestNote(hb.Entity):
        identifier = "manifest-note"

        title = hb.field(hb.ShortText(max_chars=17)).store(to="main")
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex)

    source = hb.HeavenBase(
        "manifest-source",
        detached=True,
        backends={
            "main": {"type": "inmem"},
            "vec": {"type": "inmem"},
        },
    )
    source.enable_extension("agent")
    source.register(ManifestNote)

    data = source.to_manifest()

    assert data["kind"] == "heavenbase.workspace.manifest"
    assert data["version"] == 2
    assert data["construction"] == {
        "kind": "backends",
        "config": {
            "main": {"type": "inmem"},
            "vec": {"type": "inmem"},
        },
    }
    assert data["extensions"] == ["toolkit", "prompt", "agent"]
    assert "prompt" in source.extensions()
    assert "database" not in source.extensions()
    assert [entity["entity_id"] for entity in data["entities"]] == ["manifest-note"]
    assert data["entities"][0]["fields"]["title"]["type"] == {"type": "short-text", "max_chars": 17}
    assert data["entities"][0]["fields"]["embedding"]["stores"] == [
        {"backend": "vec", "strategy": hb.VectorIndex.identifier, "optional": False, "reason": "field"}
    ]

    path = tmp_path / "workspace.yaml"
    hb.WorkspaceManifest.from_dict(data).save(str(path))
    loaded = hb.WorkspaceManifest.load(str(path))
    clone = hb.HeavenBase.from_manifest(loaded, detached=True)

    assert "agent-message" in clone.entities
    assert "manifest-note" in clone.entities
    assert clone.entities["manifest-note"].schema().get("title").dtype.max_chars == 17
    assert clone.storage_plans["manifest-note"].first("title").backend == "main"
    vector_binding = clone.storage_plans["manifest-note"].first("embedding")
    assert vector_binding.backend == "vec"
    assert vector_binding.strategy == hb.VectorIndex.identifier


@pytest.mark.fast
def test_workspace_manifest_never_replays_fixed_entity_definitions(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        source = hb.HeavenBase(
            "manifest-fixed-entity-conflict",
            context=context,
            detached=True,
            backends={"main": {"type": "inmem"}},
        )
        exported = source.to_manifest(include_system=True)
        assert "sys-catalog" not in {entity["entity_id"] for entity in exported["entities"]}
        exported["entities"].append({"entity_id": "sys-catalog", "fields": {}})

        with pytest.raises(ValueError, match="cannot define fixed workspace Entities"):
            hb.HeavenBase.from_manifest(exported, context=context, detached=True)

        with pytest.raises(ValueError, match="cannot define fixed workspace Entities"):
            hb.HeavenBase.from_manifest(
                exported,
                context=context,
                detached=True,
                allow_replace=True,
            )
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_manifest_keeps_requested_roots_when_including_extension_schemas(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        context.config.set("heavenbase.extensions.default", [])
        source = hb.HeavenBase(
            "manifest-extension-roots",
            context=context,
            detached=True,
            backends={"main": {"type": "inmem"}},
        )
        source.enable_extension("prompt")

        exported = source.to_manifest(include_system=True)

        assert exported["extensions"] == ["prompt"]
        assert {"sys-prompt", "sys-translation"} <= {entity["entity_id"] for entity in exported["entities"]}
        assert "sys-capsule" in {entity["entity_id"] for entity in exported["entities"]}
    finally:
        context.close()


@pytest.mark.fast
def test_same_id_manifest_replay_cannot_remove_durable_extension_roots(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        context.config.set("heavenbase.extensions.default", [])
        source = hb.HeavenBase(
            "manifest-monotonic-roots",
            context=context,
            backends={"main": {"type": "inmem"}},
        )
        source.enable_extension("memory")
        incoming = hb.WorkspaceManifest.from_dict(
            {
                "kind": "heavenbase.workspace.manifest",
                "version": 2,
                "id": source.id,
                "construction": source._construction.to_dict(),
                "extensions": ["prompt"],
                "entities": [],
                "backend_summary": [],
            }
        )

        reopened = hb.HeavenBase.from_manifest(incoming, context=context)
        persisted = WorkspaceSpec.load(source.id, context=context)

        assert reopened._extension_service.roots() == ("memory", "prompt")
        assert sorted(reopened.extensions()) == ["capsule", "memory", "prompt"]
        assert persisted.extension_roots == ("memory", "prompt")
        assert persisted.manifest["extensions"] == ["memory", "prompt"]
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_manifest_canonicalizes_requested_extension_roots(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        context.config.set("heavenbase.extensions.default", [])
        data = {
            "kind": "heavenbase.workspace.manifest",
            "version": 2,
            "id": "manifest-canonical-roots",
            "construction": {
                "kind": "backends",
                "config": {"main": {"type": "inmem"}},
            },
            "extensions": [" prompt ", "", "prompt", "capsule"],
            "entities": [],
            "backend_summary": [],
        }

        workspace = hb.HeavenBase.from_manifest(data, context=context)
        persisted = WorkspaceSpec.load(workspace.id, context=context)

        assert workspace._extension_service.roots() == ("prompt",)
        assert sorted(workspace.extensions()) == ["capsule", "prompt"]
        assert persisted.extension_roots == ("prompt",)
        assert persisted.manifest["extensions"] == ["prompt"]
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_manifest_rejects_scalar_extension_roots():
    with pytest.raises(TypeError, match="extensions must be an iterable"):
        hb.WorkspaceManifest.from_dict(
            {
                "kind": "heavenbase.workspace.manifest",
                "version": 2,
                "id": "manifest-scalar-root",
                "construction": {"kind": "ambient"},
                "extensions": "prompt",
            }
        )


def test_workspace_constructor_replays_registered_manifest_on_ensure():
    data = {
        "kind": "heavenbase.workspace.manifest",
        "version": 2,
        "id": "manifest-ensure-replay",
        "construction": {
            "kind": "backends",
            "config": {"main": {"type": "inmem"}},
        },
        "extensions": [],
        "entities": [
            {
                "entity_id": "manifest-ensure-note",
                "fields": {"title": {"type": "short-text"}},
            }
        ],
        "backend_summary": [],
    }

    imported = hb.HeavenBase.from_manifest(data)
    imported.release()
    reopened = hb.HeavenBase("manifest-ensure-replay")
    try:
        assert "manifest-ensure-note" in reopened.entities
        assert hb.HeavenBase.load(reopened.id) is reopened
    finally:
        reopened.drop()
        WorkspaceSpec.unregister(reopened.id)


@pytest.mark.detail
def test_workspace_manifest_failure_never_publishes_partial_facade():
    """Fallible manifest replay must preserve the same-id owner and selection."""
    hb.HeavenBase(hb.DEFAULT_CONTEXT.default_workspace_id)
    default = hb.HeavenBase.default()
    prior_default = _default_workspace_name()
    prior_active = hb.DEFAULT_CONTEXT.active_workspace_id
    incumbent = hb.HeavenBase("manifest-failure-owner", backends={"main": {"type": "inmem"}})
    data = {
        "kind": "heavenbase.workspace.manifest",
        "version": 2,
        "id": incumbent.id,
        "construction": {
            "kind": "backends",
            "config": {"main": {"type": "inmem"}},
        },
        "extensions": ["not-real"],
        "entities": [],
        "backend_summary": [],
    }
    try:
        with pytest.raises(KeyError, match="not-real"):
            hb.HeavenBase.from_manifest(data)
        assert WORKSPACES[incumbent.id] is incumbent
        assert LIVE_WORKSPACES[incumbent.id] is incumbent
        assert _default_workspace_name() == prior_default
        assert hb.DEFAULT_CONTEXT.active_workspace_id == prior_active
        assert hb.HeavenBase.default() is default
    finally:
        incumbent.drop()


@pytest.mark.detail
def test_workspace_manifest_success_binds_replayed_entities_after_publication():
    """Successful tracked replay should retain entity-side workspace binding."""
    data = {
        "kind": "heavenbase.workspace.manifest",
        "version": 2,
        "id": "manifest-binding-owner",
        "construction": {
            "kind": "backends",
            "config": {"main": {"type": "inmem"}},
        },
        "extensions": [],
        "entities": [{"entity_id": "manifest-bound-note", "fields": {"title": {"type": "short-text"}}}],
        "backend_summary": [],
    }
    workspace = hb.HeavenBase.from_manifest(data)
    try:
        entity = workspace.entities["manifest-bound-note"]
        assert entity.workspace_ids() == [workspace.id]
    finally:
        workspace.drop()


@pytest.mark.fast
def test_workspace_manifest_replays_computed_fields_through_the_compute_registry():
    """A manifest keeps compute hooks as module-qualified names, so replay needs the registry."""
    compute_ref = f"{_manifest_embed.__module__}.{_manifest_embed.__qualname__}"

    class ComputedNote(hb.Entity):
        identifier = "manifest-computed-note"

        object_id = hb.field(hb.Identifier).pk()
        body = hb.field(hb.LongText).store(to="main")
        embedding = hb.field(hb.Vector[2]).store(to="main", strategy=hb.VectorIndex).compute(_manifest_embed, inputs=["body"])

    source = hb.HeavenBase("manifest-compute-source", detached=True, backends={"main": {"type": "inmem"}})
    source.register(ComputedNote)
    data = source.to_manifest()

    assert data["entities"][0]["fields"]["embedding"]["computes"] == [{"fn": compute_ref, "inputs": ["body"]}]
    with pytest.raises(KeyError, match=compute_ref):
        hb.HeavenBase.from_manifest(data, detached=True)  # unregistered: the stored reference resolves to nothing

    hb.ext.register_compute("manifest_embed_hook", _manifest_embed, replace=True)
    clone = hb.HeavenBase.from_manifest(data, detached=True)
    object_id = clone.upsert("manifest-computed-note", {"object_id": "note-1", "body": "abcd"})

    assert clone.entities["manifest-computed-note"]._hb_specs["embedding"].computes[0].fn is _manifest_embed
    assert clone.get(object_id, entity="manifest-computed-note")["embedding"] == [4.0, 1.0]


@pytest.mark.fast
def test_workspace_manifest_serializes_runtime_backend_identifiers():
    backend = hb.backends.InMemBackend("main")
    source = hb.HeavenBase("manifest-runtime-backend", detached=True, backends={"main": backend})

    data = source.to_manifest()

    assert isinstance(source._construction, WorkspaceConstruction)
    assert data["construction"] == {
        "kind": "runtime",
        "identifiers": {"main": "inmem"},
    }
    assert data["backend_summary"] == [{"name": "main", "type": "inmem"}]
    assert backend.ws_id == "manifest-runtime-backend"


@pytest.mark.fast
def test_workspace_manifest_backend_override_replaces_the_complete_replayable_config(tmp_path):
    context = _isolated_context(tmp_path)
    manifest = hb.WorkspaceManifest.from_dict(
        {
            "kind": "heavenbase.workspace.manifest",
            "version": 2,
            "id": "manifest-config-replacement",
            "construction": {
                "kind": "backends",
                "config": {
                    "main": {"type": "inmem"},
                    "unused": {"type": "inmem"},
                },
            },
            "extensions": [],
            "entities": [],
            "backend_summary": [],
        }
    )
    try:
        workspace = manifest.open(
            context=context,
            backends={"main": {"type": "inmem"}},
            detached=True,
        )

        assert workspace.backends.names() == ["main"]
        assert manifest.to_dict()["construction"]["config"] == {
            "main": {"type": "inmem"},
            "unused": {"type": "inmem"},
        }
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_manifest_preset_override_replaces_non_runtime_construction(tmp_path):
    context = _isolated_context(tmp_path)
    context.config.set(
        "heavenbase.workspace.presets.manifest-test",
        {
            "backends": {
                "replacement": {"type": "inmem"},
            }
        },
    )
    manifest = hb.WorkspaceManifest.from_dict(
        {
            "kind": "heavenbase.workspace.manifest",
            "version": 2,
            "id": "manifest-preset-replacement",
            "construction": {
                "kind": "backends",
                "config": {"original": {"type": "inmem"}},
            },
            "extensions": [],
            "entities": [],
            "backend_summary": [],
        }
    )
    try:
        workspace = manifest.open(
            context=context,
            preset="manifest-test",
            detached=True,
        )

        assert workspace.preset == "manifest-test"
        assert workspace.backends.names() == ["replacement"]
        assert "original" not in workspace.backends.names()
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_manifest_runtime_reconnects_from_same_context_live_facade(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        backend = hb.backends.InMemBackend(
            "main",
            resolver=context.modules(),
            config=context.config,
        )
        source = hb.HeavenBase(
            "manifest-runtime-reconnect",
            context=context,
            backends={"ignored-alias": backend},
        )

        reconnected = hb.HeavenBase.from_manifest(source.to_manifest(), context=context)

        assert reconnected.backends.get("main") is backend
        assert context._workspace_registry.get(source.id) is reconnected
        assert context._live_workspaces.get(source.id) is reconnected
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_manifest_runtime_accepts_matching_backend_config(tmp_path):
    context = _isolated_context(tmp_path)
    data = {
        "kind": "heavenbase.workspace.manifest",
        "version": 2,
        "id": "manifest-runtime-config",
        "construction": {
            "kind": "runtime",
            "identifiers": {
                "main": "inmem",
                "secondary": "inmem",
            },
        },
        "extensions": [],
        "entities": [],
        "backend_summary": [],
    }
    try:
        reconstructed = hb.HeavenBase.from_manifest(
            data,
            context=context,
            backends={
                "ignored-main-alias": {
                    "type": "memory",
                    "name": "main",
                },
                "aux": {
                    "ignored-secondary-alias": {
                        "type": "inmem",
                        "name": "secondary",
                    }
                },
            },
            detached=True,
        )

        assert reconstructed.backends.names() == ["main", "secondary"]
        assert reconstructed.backends.get("main").type == "inmem"
        assert reconstructed.backends.get("secondary").type == "inmem"
        assert reconstructed.to_manifest()["construction"] == data["construction"]
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_manifest_runtime_accepts_matching_explicit_backends(tmp_path):
    context = _isolated_context(tmp_path)
    source_backend = hb.backends.InMemBackend(
        "main",
        resolver=context.modules(),
        config=context.config,
    )
    source = hb.HeavenBase(
        "manifest-runtime-explicit",
        context=context,
        backends={"main": source_backend},
        detached=True,
    )
    replacement = hb.backends.InMemBackend(
        "main",
        resolver=context.modules(),
        config=context.config,
    )
    try:
        reconstructed = hb.HeavenBase.from_manifest(
            source.to_manifest(),
            context=context,
            backends={"another-alias": replacement},
            detached=True,
        )

        assert reconstructed.backends.get("main") is replacement
        assert reconstructed.backends.get("main") is not source_backend
        assert reconstructed.to_manifest()["construction"] == source.to_manifest()["construction"]
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_manifest_runtime_requires_reconnectable_backends(tmp_path):
    context = _isolated_context(tmp_path)
    data = {
        "kind": "heavenbase.workspace.manifest",
        "version": 2,
        "id": "manifest-runtime-missing",
        "construction": {
            "kind": "runtime",
            "identifiers": {"main": "inmem"},
        },
        "extensions": [],
        "entities": [],
        "backend_summary": [],
    }
    try:
        with pytest.raises(RuntimeError, match="(?i)runtime"):
            hb.HeavenBase.from_manifest(data, context=context)

        with pytest.raises(KeyError, match="manifest-runtime-missing"):
            WorkspaceSpec.load("manifest-runtime-missing", context=context)
        assert context._workspace_registry.get("manifest-runtime-missing") is None
        assert context._live_workspaces.get("manifest-runtime-missing") is None
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_manifest_runtime_rejects_incomplete_or_mismatched_explicit_backends(tmp_path):
    context = _isolated_context(tmp_path)
    data = {
        "kind": "heavenbase.workspace.manifest",
        "version": 2,
        "id": "manifest-runtime-mismatch",
        "construction": {
            "kind": "runtime",
            "identifiers": {
                "main": "inmem",
                "secondary": "inmem",
            },
        },
        "extensions": [],
        "entities": [],
        "backend_summary": [],
    }
    try:
        with pytest.raises(RuntimeError, match="(?i)runtime"):
            hb.HeavenBase.from_manifest(
                data,
                context=context,
                backends={
                    "main": {"type": "inmem"},
                },
                detached=True,
            )

        secondary = hb.backends.JsonFileBackend("secondary", resolver=context.modules(), config=context.config)
        with pytest.raises(RuntimeError, match="(?i)runtime"):
            hb.HeavenBase.from_manifest(
                data,
                context=context,
                backends={
                    "main": hb.backends.InMemBackend(
                        "main",
                        resolver=context.modules(),
                        config=context.config,
                    ),
                    "secondary": secondary,
                },
                detached=True,
            )
        # The backend's default folder is under the context's package home, not the developer's.
        assert Path(secondary.path).resolve().is_relative_to(tmp_path.resolve())
    finally:
        context.close()


@pytest.mark.fast
def test_workspace_manifest_conflict_preserves_tracked_owner(tmp_path):
    context = _isolated_context(tmp_path)
    try:
        incumbent = hb.HeavenBase(
            "manifest-construction-conflict",
            context=context,
            backends={"main": {"type": "inmem"}},
        )
        original_spec = WorkspaceSpec.load(incumbent.id, context=context)
        data = {
            "kind": "heavenbase.workspace.manifest",
            "version": 2,
            "id": incumbent.id,
            "construction": {
                "kind": "backends",
                "config": {"replacement": {"type": "inmem"}},
            },
            "extensions": [],
            "entities": [
                {
                    "entity_id": "must-not-be-registered",
                    "fields": {"title": {"type": "short-text"}},
                }
            ],
            "backend_summary": [],
        }

        with pytest.raises(FileExistsError, match="construction"):
            hb.HeavenBase.from_manifest(data, context=context)

        preserved_spec = WorkspaceSpec.load(incumbent.id, context=context)
        assert context._workspace_registry.get(incumbent.id) is incumbent
        assert context._live_workspaces.get(incumbent.id) is incumbent
        assert preserved_spec.construction == original_spec.construction
        assert preserved_spec.manifest == original_spec.manifest
        assert "must-not-be-registered" not in incumbent.entities
    finally:
        context.close()


def test_workspace_manifest_registry_preserves_schema_when_config_updates():
    data = {
        "kind": "heavenbase.workspace.manifest",
        "version": 2,
        "id": "manifest-registered",
        "construction": {
            "kind": "backends",
            "config": {"main": {"type": "inmem"}},
        },
        "extensions": [],
        "entities": [
            {
                "entity_id": "manifest-registered-note",
                "name": "ManifestRegisteredNote",
                "desc": "",
                "tags": [],
                "fields": {
                    "object_id": {"type": "identifier", "pk": True, "required": True, "desc": "", "tags": [], "examples": []},
                    "title": {"type": "short-text", "pk": False, "required": True, "desc": "", "tags": [], "examples": []},
                },
            }
        ],
        "backend_summary": [],
    }

    WorkspaceSpec.register("manifest-registered", config={"backends": data["construction"]["config"]}, manifest=data)
    updated = WorkspaceSpec.register("manifest-registered", config={"preset": "debug"})

    assert updated.config == {"preset": "debug"}
    assert updated.manifest["construction"] == {"kind": "preset", "preset": "debug"}
    assert [entity["entity_id"] for entity in updated.manifest["entities"]] == ["manifest-registered-note"]


@pytest.mark.slow
def test_workspace_manifest_cli_import_persists_schema_for_reopen(tmp_path):
    class ImportedNote(hb.Entity):
        identifier = "manifest-import-note"

        title = hb.field(hb.ShortText).store(to="main")

    source = hb.HeavenBase(
        "manifest-imported",
        detached=True,
        backends={"main": {"type": "inmem"}},
    )
    source.register(ImportedNote)
    path = tmp_path / "workspace.yaml"
    hb.WorkspaceManifest.from_dict(source.to_manifest()).save(str(path))

    out = _Output()
    prior_active = hb.DEFAULT_CONTEXT.active_workspace_id
    import_manifest(PackageCLIContext(cm=hb.CM_HVNB, out=out), str(path))

    assert out.yaml_payloads[0]["imported"] == "manifest-imported"
    assert hb.DEFAULT_CONTEXT.active_workspace_id == prior_active
    registered = WorkspaceSpec.load("manifest-imported")
    assert [entity["entity_id"] for entity in registered.manifest["entities"]] == ["manifest-import-note"]

    updated = WorkspaceSpec.register("manifest-imported", config={"preset": "debug"})
    assert updated.config == {"preset": "debug"}
    assert updated.manifest["construction"] == {"kind": "preset", "preset": "debug"}
    assert [entity["entity_id"] for entity in updated.manifest["entities"]] == ["manifest-import-note"]

    live = hb.HeavenBase.load("manifest-imported")
    _release_workspace(live)
    reopened = updated.open()

    assert "manifest-import-note" in reopened.entities


def _sqlite_context(tmp_path):
    """Open an isolated Context whose configuration includes the default SQLite provider."""
    return hb.Context.load(
        {
            "version": 1,
            "root": str(tmp_path),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=ConfigManager(root=str(tmp_path), setup=True),
    )


def _saved_note_manifest(identifier, database):
    return {
        "kind": "heavenbase.workspace.manifest",
        "version": 2,
        "id": identifier,
        "construction": {"kind": "backends", "config": {"main": {"type": "sqlite", "name": "main", "database": database}}},
        "extensions": [],
        "entities": [{"entity_id": "saved-note", "fields": {"name": {"type": "short-text"}}}],
        "backend_summary": [],
    }


_EXTENDED_NOTE = {
    "entity_id": "saved-note",
    "fields": {"name": {"type": "short-text"}, "extra": {"type": "short-text", "required": False}},
}


@pytest.mark.slow
@pytest.mark.parametrize("change", ["alter", "replace"])
def test_a_manifest_backed_workspace_reopens_by_id_with_its_later_schema_changes(tmp_path, change):
    """A live schema change is written to the saved replay manifest, so reopening by id keeps the field and its values."""
    context = _sqlite_context(tmp_path / "root")
    identifier = "manifest-saved-changes"
    data = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))
    try:
        imported = hb.HeavenBase.from_manifest(data, context=context)
        imported.upsert("saved-note", {"object_id": "n1", "name": "one"})
        if change == "alter":
            imported.alter_entity(dict(_EXTENDED_NOTE), confirm="saved-note")
        else:
            imported.register(dict(_EXTENDED_NOTE), allow_replace=True)
        imported.describe("saved-note", desc="Notes with an extra field")
        imported.upsert("saved-note", {"object_id": "n2", "name": "two", "extra": "X"})

        class LaterNote(hb.Entity):
            identifier = "manifest-later-note"
            title = hb.field(hb.ShortText)

        imported.register(LaterNote)
        imported.release()

        saved = WorkspaceSpec.load(identifier, context=context).manifest
        assert [entity["entity_id"] for entity in saved["entities"]] == ["saved-note"]
        assert set(saved["entities"][0]["fields"]) == {"object_id", "name", "extra"}
        assert saved["entities"][0]["desc"] == "Notes with an extra field"

        for reopen in (
            lambda: hb.HeavenBase.load(identifier, context=context),
            lambda: hb.HeavenBase(identifier, context=context),
            lambda: WorkspaceSpec.load(identifier, context=context).open_detached(context=context),
        ):
            reopened = reopen()
            note = reopened.entities["saved-note"]
            assert set(note.schema().fields) == {"object_id", "name", "extra"}
            assert note.schema().desc == "Notes with an extra field"
            assert "manifest-later-note" not in reopened.entities
            assert reopened.get("n2", entity="saved-note")["extra"] == "X"
            assert [row["object_id"] for row in reopened.query("saved-note").where({"extra": "X"}).execute().rows()] == ["n2"]
            reopened.upsert("saved-note", {"object_id": "n3", "name": "three", "extra": "Z"})
            assert reopened.get("n3", entity="saved-note")["extra"] == "Z"
            assert reopened.get("n2", entity="saved-note")["extra"] == "X"
            reopened.release()
    finally:
        context.close()


@pytest.mark.slow
def test_a_detached_schema_change_leaves_the_saved_replay_manifest_alone_and_warns(tmp_path):
    """Only the tracked workspace writes its saved replay manifest; a detached change of the same store warns that reopening loses it."""
    context = _sqlite_context(tmp_path / "root")
    identifier = "manifest-saved-detached"
    data = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))
    try:
        hb.HeavenBase.from_manifest(data, context=context).release()
        before = WorkspaceSpec.load(identifier, context=context).manifest
        detached = WorkspaceSpec.load(identifier, context=context).open_detached(context=context)
        with pytest.warns(UserWarning, match="changed through a detached or untracked facade.*without this change"):
            detached.alter_entity(dict(_EXTENDED_NOTE), confirm="saved-note")
        detached.release()
        assert WorkspaceSpec.load(identifier, context=context).manifest == before

        # a detached facade of another store does not describe the saved workspace and stays silent
        elsewhere = {**data, "construction": _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "other.db"))["construction"]}
        other = hb.HeavenBase.from_manifest(elsewhere, context=context, detached=True)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            other.alter_entity(dict(_EXTENDED_NOTE), confirm="saved-note")
        other.release()
        assert WorkspaceSpec.load(identifier, context=context).manifest == before
    finally:
        context.close()


def _shared_sqlite_contexts(tmp_path, count):
    """Open Contexts that share one durable SQLite system store, as separate processes do."""
    bootstrap = {
        "version": 1,
        "root": str(tmp_path),
        "workspace": "default",
        "backend": load_bootstrap(home_path=tmp_path / "missing-bootstrap.yaml").backend,
        "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
    }
    return [hb.Context.load(bootstrap, config=ConfigManager(root=str(tmp_path), setup=True)) for _ in range(count)]


@pytest.mark.slow
def test_registry_retry_preserves_extension_roots_from_an_iterator(tmp_path):
    stale, writer, reader = _shared_sqlite_contexts(tmp_path / "root", 3)
    try:
        WorkspaceSpec.list(context=stale)
        WorkspaceSpec.register("iterator-roots", extension_roots=["prompt"], context=writer)
        spec = WorkspaceSpec.register("iterator-roots", extension_roots=iter(["memory"]), context=stale)

        assert spec.extension_roots == ("prompt", "memory")
        persisted = WorkspaceSpec.load(spec.id, context=reader)
        assert persisted.extension_roots == ("prompt", "memory")
        reopened = persisted.open_detached(context=reader)
        assert "memory" in reopened.extensions()
        reopened.release()
    finally:
        for context in (stale, writer, reader):
            context.close()


@pytest.mark.slow
def test_the_saved_replay_manifest_follows_changes_made_after_another_context_wrote_the_registry(tmp_path):
    """Each Context refreshes the workspace registry before it saves a change, so neither a stale snapshot nor a concurrent change is lost."""
    first, second, third = _shared_sqlite_contexts(tmp_path / "root", 3)
    identifier = "manifest-saved-shared"
    data = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))
    data["entities"].append({"entity_id": "saved-memo", "fields": {"name": {"type": "short-text"}}})
    memo_extended = {"entity_id": "saved-memo", "fields": {"name": {"type": "short-text"}, "extra": {"type": "short-text", "required": False}}}
    try:
        imported = hb.HeavenBase.from_manifest(data, context=first)
        imported.upsert("saved-note", {"object_id": "n1", "name": "one"})
        loaded = hb.HeavenBase.load(identifier, context=second)
        hb.HeavenBase("manifest-saved-unrelated", backends={"main": {"type": "inmem"}}, context=first).release()

        loaded.alter_entity(dict(_EXTENDED_NOTE), confirm="saved-note")
        loaded.describe("saved-note", desc="described by the second Context")
        loaded.upsert("saved-note", {"object_id": "n2", "name": "two", "extra": "X"})
        # the first Context still holds the registry snapshot from before the second Context's writes
        imported.alter_entity(memo_extended, confirm="saved-memo")
        imported.upsert("saved-memo", {"object_id": "m1", "name": "memo", "extra": "Y"})
        loaded.release()
        imported.release()

        saved = {entity["entity_id"]: entity for entity in WorkspaceSpec.load(identifier, context=third).manifest["entities"]}
        assert set(saved["saved-note"]["fields"]) == set(saved["saved-memo"]["fields"]) == {"object_id", "name", "extra"}
        assert saved["saved-note"]["desc"] == "described by the second Context"
        reopened = hb.HeavenBase.load(identifier, context=third)
        assert reopened.get("n2", entity="saved-note")["extra"] == "X"
        assert reopened.get("m1", entity="saved-memo")["extra"] == "Y"
        reopened.release()
    finally:
        for context in (first, second, third):
            context.close()


@pytest.mark.slow
def test_saving_a_changed_definition_sets_up_no_modules(tmp_path, monkeypatch):
    """A schema change writes only the saved manifest: the extension roots, and so the modules, stay as they are."""
    context = _sqlite_context(tmp_path / "root")
    identifier = "manifest-saved-no-setup"
    try:
        imported = hb.HeavenBase.from_manifest(_saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db")), context=context)
        roots = WorkspaceSpec.load(identifier, context=context).extension_roots
        calls = []
        original = type(context)._setup_modules

        def counted(self, *args, **kwargs):
            calls.append(args)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(type(context), "_setup_modules", counted)
        imported.describe("saved-note", desc="first")
        imported.alter_entity(dict(_EXTENDED_NOTE), confirm="saved-note")
        imported.describe("saved-note", desc="second")
        imported.release()

        assert calls == []
        spec = WorkspaceSpec.load(identifier, context=context)
        assert spec.extension_roots == roots
        assert spec.manifest["entities"][0]["desc"] == "second"
        assert set(spec.manifest["entities"][0]["fields"]) == {"object_id", "name", "extra"}
    finally:
        context.close()


@pytest.mark.slow
def test_registering_a_workspace_reads_the_registry_again_after_another_context_published_it(tmp_path):
    """A Context whose registry snapshot is out of date still registers a workspace, keeping what the other Context published."""
    first, second = _shared_sqlite_contexts(tmp_path / "root", 2)
    try:
        WorkspaceSpec.list(context=second)  # the second Context reads the registry now
        hb.HeavenBase("registry-retry-first", backends={"main": {"type": "inmem"}}, context=first).release()
        # published against the snapshot the second Context read before the first one wrote
        WorkspaceSpec.register("registry-retry-second", context=second)

        _registry(first).refresh()
        for context in (first, second):
            names = {spec.id for spec in WorkspaceSpec.list(context=context)}
            assert {"registry-retry-first", "registry-retry-second"} <= names
    finally:
        for context in (first, second):
            context.close()


@pytest.mark.slow
def test_a_saved_definition_without_entity_id_follows_a_schema_change(tmp_path):
    """A manifest entity named only by ``name`` is matched by the id derived from it, as the compiler derives it."""
    context = _sqlite_context(tmp_path / "root")
    identifier = "manifest-saved-named"
    data = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))
    data["entities"] = [{"name": "SavedNote", "fields": {"name": {"type": "short-text"}}}]
    try:
        imported = hb.HeavenBase.from_manifest(data, context=context)
        imported.upsert("saved-note", {"object_id": "n1", "name": "one"})
        imported.alter_entity({"name": "SavedNote", "fields": dict(_EXTENDED_NOTE["fields"])}, confirm="saved-note")
        imported.upsert("saved-note", {"object_id": "n2", "name": "two", "extra": "X"})
        imported.release()

        saved = WorkspaceSpec.load(identifier, context=context).manifest["entities"]
        assert [(entity["entity_id"], set(entity["fields"])) for entity in saved] == [("saved-note", {"object_id", "name", "extra"})]
        reopened = hb.HeavenBase.load(identifier, context=context)
        assert set(reopened.entities["saved-note"].schema().fields) == {"object_id", "name", "extra"}
        assert reopened.get("n2", entity="saved-note")["extra"] == "X"
        reopened.upsert("saved-note", {"object_id": "n3", "name": "three", "extra": "Z"})
        assert reopened.get("n3", entity="saved-note")["extra"] == "Z"
        reopened.release()
    finally:
        context.close()


def _manifest_slug(name):
    """A compute hook that is not registered as a module, so a reopen by id cannot resolve it."""
    return str(name).lower()


@pytest.mark.slow
@pytest.mark.parametrize("change", ["tuple-default", "decimal-example", "unregistered-compute"])
def test_a_definition_that_cannot_be_replayed_leaves_the_saved_manifest_and_warns(tmp_path, change):
    """The schema change still applies; the saved copy keeps a definition a reopen by id can replay."""
    context = _sqlite_context(tmp_path / "root")
    identifier = f"manifest-saved-{change}"
    data = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))

    class PairedNote(hb.Entity):
        identifier = "saved-note"
        name = hb.field(hb.ShortText)
        pair = hb.field(hb.Json).default((1, 2))

    class SluggedNote(hb.Entity):
        identifier = "saved-note"
        name = hb.field(hb.ShortText)
        slug = hb.field(hb.ShortText).compute(_manifest_slug, inputs=["name"])

    try:
        imported = hb.HeavenBase.from_manifest(data, context=context)
        before = WorkspaceSpec.load(identifier, context=context).manifest
        if change == "decimal-example":
            imported.alter_entity(dict(_EXTENDED_NOTE), confirm="saved-note")
            before = WorkspaceSpec.load(identifier, context=context).manifest
            with pytest.warns(UserWarning, match="not JSON"):
                imported.describe("saved-note", fields={"extra": {"examples": [decimal.Decimal("1.50")]}})
            assert list(imported.entities["saved-note"].schema().get("extra").examples) == [decimal.Decimal("1.50")]
        else:
            entity = PairedNote if change == "tuple-default" else SluggedNote
            with pytest.warns(UserWarning, match="not JSON" if change == "tuple-default" else "cannot be replayed.*_manifest_slug"):
                imported.register(entity, allow_replace=True)
            assert imported.entities["saved-note"] is entity
            imported.upsert("saved-note", {"object_id": "n1", "name": "One"})
            stored = imported.get("n1", entity="saved-note")
            assert list(stored["pair"]) == [1, 2] if change == "tuple-default" else stored["slug"] == "one"
        imported.release()
        assert WorkspaceSpec.load(identifier, context=context).manifest == before
        hb.HeavenBase.load(identifier, context=context).release()
    finally:
        context.close()


@pytest.mark.slow
@pytest.mark.parametrize("change", ["describe", "alter"])
def test_an_out_of_date_facade_keeps_the_definition_another_context_saved_and_warns(tmp_path, change):
    """A facade whose live definition is older than the saved one does not overwrite it, so a field another Context added survives a reopen."""
    from heavenbase.workspace.registry import _registry

    first, second, third = _shared_sqlite_contexts(tmp_path / "root", 3)
    identifier = f"manifest-saved-stale-{change}"
    data = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))
    other = {"entity_id": "saved-note", "fields": {"name": {"type": "short-text"}, "other": {"type": "short-text", "required": False}}}
    try:
        imported = hb.HeavenBase.from_manifest(data, context=first)
        loaded = hb.HeavenBase.load(identifier, context=second)
        imported.alter_entity(dict(_EXTENDED_NOTE), confirm="saved-note")
        imported.upsert("saved-note", {"object_id": "n2", "name": "two", "extra": "X"})
        _registry(third).refresh()  # the third Context reads the store again, as a new process would
        saved_before = WorkspaceSpec.load(identifier, context=third).manifest
        with pytest.warns(UserWarning, match="another Context or process saved a different definition"):
            if change == "describe":
                loaded.describe("saved-note", desc="described by an out-of-date facade")
            else:
                loaded.alter_entity(other, confirm="saved-note")
        _registry(third).refresh()
        assert WorkspaceSpec.load(identifier, context=third).manifest == saved_before
        # the facade that wrote the saved definition still follows its own later changes
        imported.describe("saved-note", desc="described by the up-to-date facade")
        _registry(third).refresh()
        saved = WorkspaceSpec.load(identifier, context=third).manifest["entities"][0]
        assert set(saved["fields"]) == {"object_id", "name", "extra"} and saved["desc"] == "described by the up-to-date facade"
        loaded.release()
        imported.release()

        reopened = hb.HeavenBase.load(identifier, context=third)
        assert set(reopened.entities["saved-note"].schema().fields) == {"object_id", "name", "extra"}
        assert reopened.get("n2", entity="saved-note")["extra"] == "X"
        reopened.release()
    finally:
        for context in (first, second, third):
            context.close()


@pytest.mark.slow
def test_an_unreplayable_change_still_saves_its_catalog_and_gram_options(tmp_path):
    """The saved definition keeps its fields but takes the new options, so reopening by id does not raise."""
    context = _sqlite_context(tmp_path / "root")
    identifier = "manifest-saved-unreplayable-options"
    data = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))
    data["entities"][0]["catalog"] = "identity"

    class PairedNote(hb.Entity):
        identifier = "saved-note"
        name = hb.field(hb.ShortText)
        pair = hb.field(hb.Json).default((1, 2))

    try:
        imported = hb.HeavenBase.from_manifest(data, context=context)
        with pytest.warns(UserWarning, match="not JSON.*current catalog and gram options"):
            imported.register(PairedNote, allow_replace=True, catalog="full")
        imported.release()
        saved = WorkspaceSpec.load(identifier, context=context).manifest["entities"][0]
        assert saved == {"entity_id": "saved-note", "fields": {"name": {"type": "short-text"}}, "catalog": "full", "gram": True}
        for reopen in (
            lambda: hb.HeavenBase.load(identifier, context=context),
            lambda: hb.HeavenBase(identifier, context=context),
            lambda: WorkspaceSpec.load(identifier, context=context).open_detached(context=context),
        ):
            reopened = reopen()
            assert reopened.entity_options["saved-note"].catalog == "full"
            reopened.release()
    finally:
        context.close()


@pytest.mark.slow
def test_a_detached_option_change_warns_that_reopening_by_id_raises_until_replayed(tmp_path):
    """The warning states what a reopen does: a saved definition stating other options than the store raises, and from_manifest fixes it."""
    context = _sqlite_context(tmp_path / "root")
    identifier = "manifest-saved-detached-options"
    data = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))
    data["entities"][0]["catalog"] = "identity"
    try:
        hb.HeavenBase.from_manifest(data, context=context).release()
        detached = WorkspaceSpec.load(identifier, context=context).open_detached(context=context)
        with pytest.warns(UserWarning, match="detached or untracked facade.*raises ValueError.*allow_replace=True"):
            detached.register({"entity_id": "saved-note", "fields": {"name": {"type": "short-text"}}}, allow_replace=True, catalog="full")
        detached.release()
        with pytest.raises(ValueError, match="stored with catalog='full'"):
            hb.HeavenBase.load(identifier, context=context)
        data["entities"][0]["catalog"] = "full"
        hb.HeavenBase.from_manifest(data, context=context, allow_replace=True).release()
        reopened = hb.HeavenBase.load(identifier, context=context)
        assert reopened.entity_options["saved-note"].catalog == "full"
        reopened.release()
    finally:
        context.close()


@pytest.mark.slow
def test_a_first_registration_through_a_detached_facade_warns_when_it_changes_the_saved_definition(tmp_path):
    """A detached facade that did not replay the manifest registers every entity for the first time; a change still warns."""
    from heavenbase.workspace.registry import _registry

    first, second = _shared_sqlite_contexts(tmp_path / "root", 2)
    identifier = "manifest-saved-detached-first"
    database = hb.utils.path_to_file_uri(tmp_path / "saved.db")
    data = _saved_note_manifest(identifier, database)
    try:
        hb.HeavenBase.from_manifest(data, context=first).release()
        _registry(second).refresh()  # a new process
        before = WorkspaceSpec.load(identifier, context=second).manifest
        same = hb.HeavenBase(identifier, detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": database}}, context=second)
        assert "saved-note" not in same.entities
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            same.register({"entity_id": "saved-note", "fields": {"name": {"type": "short-text"}}})  # matches the saved definition
        same.release()
        changed = hb.HeavenBase(identifier, detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": database}}, context=second)
        with pytest.warns(UserWarning, match="changed through a detached or untracked facade.*without this change"):
            changed.register(dict(_EXTENDED_NOTE))
        changed.release()
        assert WorkspaceSpec.load(identifier, context=second).manifest == before
    finally:
        for context in (first, second):
            context.close()


@pytest.mark.slow
@pytest.mark.parametrize("change", ["describe", "alter"])
def test_the_same_change_saved_first_by_another_context_raises_no_out_of_date_warning(tmp_path, change):
    """Two Contexts that make the same change, such as one startup migration in two workers, both end with it saved and no warning."""
    from heavenbase.workspace.registry import _registry

    first, second, third = _shared_sqlite_contexts(tmp_path / "root", 3)
    identifier = f"manifest-saved-same-{change}"
    data = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))
    try:
        imported = hb.HeavenBase.from_manifest(data, context=first)
        loaded = hb.HeavenBase.load(identifier, context=second)
        for facade in (imported, loaded):
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                if change == "describe":
                    facade.describe("saved-note", desc="Notes")
                else:
                    facade.alter_entity(dict(_EXTENDED_NOTE), confirm="saved-note")
        # the second facade now knows the saved definition, so its own later change is saved
        loaded.describe("saved-note", desc="described later")
        loaded.release()
        imported.release()
        _registry(third).refresh()
        saved = WorkspaceSpec.load(identifier, context=third).manifest["entities"][0]
        assert saved["desc"] == "described later"
        assert set(saved["fields"]) == ({"object_id", "name", "extra"} if change == "alter" else {"object_id", "name"})
    finally:
        for context in (first, second, third):
            context.close()


@pytest.mark.slow
def test_a_manifest_imported_with_a_computes_mapping_names_the_real_cause_of_an_unsaved_change(tmp_path):
    """The saved definition names a hook the mapping supplied; it is compared through that mapping, so no other writer is blamed."""
    context = _sqlite_context(tmp_path / "root")
    identifier = "manifest-saved-computes-mapping"
    data = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))
    data["entities"][0]["fields"]["slug"] = {"type": "short-text", "required": False, "compute": {"fn": "slugger", "inputs": ["name"]}}
    try:
        imported = hb.HeavenBase.from_manifest(data, context=context, computes={"slugger": _manifest_slug})
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            imported.describe("saved-note", desc="described")
        messages = [str(item.message) for item in caught if issubclass(item.category, UserWarning)]
        assert len(messages) == 1 and "cannot be replayed" in messages[0] and "another Context" not in messages[0]
        imported.release()
    finally:
        context.close()


@pytest.mark.slow
def test_an_out_of_date_facade_saves_the_change_after_it_is_released_and_reopened(tmp_path):
    """The out-of-date warning's advice works: a reload in the same Context returns the stale facade until it is released."""
    from heavenbase.workspace.registry import _registry

    first, second, third = _shared_sqlite_contexts(tmp_path / "root", 3)
    identifier = "manifest-saved-stale-advice"
    data = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))
    try:
        imported = hb.HeavenBase.from_manifest(data, context=first)
        loaded = hb.HeavenBase.load(identifier, context=second)
        imported.alter_entity(dict(_EXTENDED_NOTE), confirm="saved-note")
        with pytest.warns(UserWarning, match="release this facade, reopen the workspace with HeavenBase.load"):
            loaded.describe("saved-note", desc="by the stale facade")
        assert hb.HeavenBase.load(identifier, context=second) is loaded  # still the stale facade
        loaded.release()
        reopened = hb.HeavenBase.load(identifier, context=second)
        assert set(reopened.entities["saved-note"].schema().fields) == {"object_id", "name", "extra"}
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            reopened.describe("saved-note", desc="after the reopen")
        reopened.release()
        imported.release()
        _registry(third).refresh()
        saved = WorkspaceSpec.load(identifier, context=third).manifest["entities"][0]
        assert saved["desc"] == "after the reopen" and set(saved["fields"]) == {"object_id", "name", "extra"}
    finally:
        for context in (first, second, third):
            context.close()


class SavedDatedNote(hb.Entity):
    identifier = "saved-note"
    name = hb.field(hb.ShortText)
    due = hb.field(hb.Date).default(datetime.date(2020, 1, 2))
    at = hb.field(hb.Timestamp).default(datetime.datetime(2020, 1, 2, 3, 4, 5, 6, tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))))


@pytest.mark.slow
def test_date_and_datetime_defaults_and_examples_are_saved_and_replay_to_the_same_values(tmp_path):
    """Reopening by id registers the dated fields with the class's own values, so registering the class again changes nothing."""
    context = _sqlite_context(tmp_path / "root")
    identifier = "manifest-saved-dated"
    data = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))
    examples = [datetime.date(2021, 1, 1), "plain"]
    try:
        imported = hb.HeavenBase.from_manifest(data, context=context)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            imported.register(SavedDatedNote, allow_replace=True)
            imported.describe("saved-note", fields={"name": {"examples": examples}})
        imported.upsert("saved-note", {"object_id": "n1", "name": "One"})
        exported = imported.to_manifest()["entities"][0]
        imported.release()
        saved = WorkspaceSpec.load(identifier, context=context).manifest["entities"][0]
        assert json.loads(json.dumps(exported)) == exported == saved
        assert (saved["fields"]["due"]["default"], saved["fields"]["due"]["default_type"]) == ("2020-01-02", "datetime.date")
        assert saved["fields"]["name"]["example_types"] == ["datetime.date", None]
        compiled = hb.Entity.from_schema(exported).schema()
        assert compiled.get("due").default == datetime.date(2020, 1, 2) and list(compiled.get("name").examples) == examples
        reopened = hb.HeavenBase.load(identifier, context=context)
        schema = reopened.entities["saved-note"].schema()
        assert schema.get("due").default == datetime.date(2020, 1, 2)
        assert schema.get("at").default == SavedDatedNote.schema().get("at").default
        assert list(schema.get("name").examples) == examples
        reopened.register(SavedDatedNote)
        reopened.upsert("saved-note", {"object_id": "n2", "name": "Two"})
        first, second = (reopened.get(object_id, entity="saved-note") for object_id in ("n1", "n2"))
        assert (first["due"], first["at"]) == (second["due"], second["at"]) and first["due"] == "2020-01-02"
        reopened.release()
    finally:
        context.close()


@pytest.mark.slow
@pytest.mark.parametrize("suffix", ["yaml", "json"])
def test_a_dated_workspace_reopened_by_id_exports_a_manifest_that_imports(tmp_path, suffix):
    """The export a reopened workspace saves to a file imports in another Context, and again under its own id."""
    context, other = _sqlite_context(tmp_path / "root"), _sqlite_context(tmp_path / "other")
    identifier = "manifest-dated-export"
    data = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))
    path = str(tmp_path / f"exported.{suffix}")
    try:
        imported = hb.HeavenBase.from_manifest(data, context=context)
        imported.register(SavedDatedNote, allow_replace=True)
        imported.describe("saved-note", fields={"name": {"examples": [datetime.date(2021, 1, 1)]}})
        imported.release()
        reopened = hb.HeavenBase.load(identifier, context=context)
        exported = reopened.to_manifest()
        reopened.release()
        moved = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "moved.db"))["construction"]
        WorkspaceManifest.from_dict({**exported, "construction": moved}).save(path)
        copy = WorkspaceManifest.load(path).open(context=other)
        assert copy.entities["saved-note"].schema().get("at").default == SavedDatedNote.schema().get("at").default
        copy.upsert("saved-note", {"object_id": "n1", "name": "One"})
        assert copy.get("n1", entity="saved-note")["due"] == "2020-01-02"
        copy.release()
        assert WorkspaceSpec.load(identifier, context=other).manifest["entities"] == exported["entities"]
        for allow_replace in (False, True):
            hb.HeavenBase.from_manifest(exported, context=context, allow_replace=allow_replace).release()
        hb.HeavenBase.load(identifier, context=context).release()
    finally:
        context.close()
        other.close()


@pytest.mark.slow
@pytest.mark.parametrize("allow_replace", [False, True])
def test_a_definition_that_holds_date_objects_imports_and_reopens(tmp_path, allow_replace):
    """A YAML file states dates unquoted, so a tracked import saves them in the form a reopen restores."""
    context = _sqlite_context(tmp_path / "root")
    identifier = "manifest-dated-import"
    data = _saved_note_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))
    data["entities"][0]["catalog"] = "identity"
    try:
        hb.HeavenBase.from_manifest(data, context=context).release()
        due = {"type": "date", "required": False, "default": datetime.date(2020, 1, 2), "examples": [datetime.date(2021, 1, 1)]}
        data["entities"][0]["fields"]["due"] = due
        data["entities"][0]["catalog"] = "full" if allow_replace else "identity"
        hb.HeavenBase.from_manifest(data, context=context, allow_replace=allow_replace).release()
        reopened = hb.HeavenBase.load(identifier, context=context)
        field = reopened.entities["saved-note"].schema().get("due")
        assert field.default == datetime.date(2020, 1, 2) and list(field.examples) == [datetime.date(2021, 1, 1)]
        assert reopened.entity_options["saved-note"].catalog == data["entities"][0]["catalog"]
        reopened.release()
    finally:
        context.close()


@pytest.mark.fast
def test_dated_definition_text_is_restored_the_same_on_every_python():
    """Only the text isoformat() writes is restored; other text stays text, as it was before the type was named."""
    from heavenbase.entity.compiler import _decoded_value, _encoded_value

    for value in (
        datetime.date(2024, 2, 29),
        datetime.datetime(2024, 1, 2, 3, 4, 5),
        datetime.datetime(2024, 1, 2, 3, 4, 5, 6, tzinfo=datetime.timezone.utc),
        datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.timezone(datetime.timedelta(hours=-5, seconds=-7))),
    ):
        restored = _decoded_value(*_encoded_value(value))
        assert restored == value and type(restored) is type(value)
    day, moment = "datetime.date", "datetime.datetime"
    for text, name in (("20240229", day), ("2024-02-30", day), ("soon", day), ("20240102T030405", moment), ("2024-01-02T03:04:05Z", moment)):
        assert _decoded_value(text, name) == text
    assert _decoded_value("2024-02-29", "date") == "2024-02-29" and _decoded_value("plain", None) == "plain"
    field = {"type": "short-text", "default": "soon", "default_type": day}
    assert hb.Entity.from_schema({"entity_id": "dated-prose", "fields": {"when": field}}).schema().get("when").default == "soon"


@pytest.mark.slow
def test_a_class_registered_after_the_import_may_gain_a_field_between_runs(tmp_path):
    context = _sqlite_context(tmp_path / "root")
    database = hb.utils.path_to_file_uri(tmp_path / "evolve.db")
    try:
        first = hb.HeavenBase.from_manifest(_saved_note_manifest("r17-evolve", database), context=context)
        first.register({"entity_id": "app-task", "fields": {"title": {"type": "short-text"}}})
        first.upsert("app-task", {"object_id": "t1", "title": "one"})
        first.release()
        second = hb.HeavenBase.load("r17-evolve", context=context)  # the next run of the program, its class edited
        second.register({"entity_id": "app-task", "fields": {"title": {"type": "short-text"}, "due": {"type": "short-text", "required": False}}})
        second.upsert("app-task", {"object_id": "t2", "title": "two", "due": "soon"})
        assert second.get("t2", entity="app-task")["due"] == "soon"
        assert second.get("t1", entity="app-task")["title"] == "one"
        second.release()
    finally:
        context.close()


@pytest.mark.slow
def test_load_keeps_the_rows_of_an_in_memory_workspace(tmp_path):
    context = _sqlite_context(tmp_path / "root")
    try:
        data = _saved_note_manifest("r17-inmem", "")
        data["construction"]["config"]["main"] = {"type": "inmem", "name": "main"}
        live = hb.HeavenBase.from_manifest(data, context=context)
        live.upsert("saved-note", {"object_id": "n1", "name": "one"})
        staging = WorkspaceSpec.load("r17-inmem", context=context).open_detached(context=context)  # its own, empty store
        with pytest.warns(UserWarning, match="changed through a detached"):
            staging.alter_entity(dict(_EXTENDED_NOTE), confirm="saved-note")
        staging.release()
        assert (hb.HeavenBase.load("r17-inmem", context=context).get("n1", entity="saved-note") or {}).get("name") == "one"
        live.describe("saved-note", desc="still described")
        live.release()
    finally:
        context.close()
