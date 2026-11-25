from pathlib import Path

import pytest
import yaml

import heavenbase as hb

pytestmark = pytest.mark.fast

FOUNDATION_ENTITY_IDS = [
    "sys-catalog",
    "sys-metaschema",
    "sys-capsule",
]
PROMPT_ENTITY_IDS = [
    "sys-prompt",
    "sys-translation",
]


class ExtensionNote(hb.Entity):
    identifier = "extension-note"

    name = hb.field(hb.ShortText).default("")


class ExtensionExtra(hb.Entity):
    identifier = "extension-extra"

    name = hb.field(hb.ShortText).default("")


class TestExtensionAPI:
    """Expose a workspace name for Extension API tests."""

    def __init__(self, workspace):
        self.workspace = workspace

    def name(self):
        """Return the bound workspace identifier."""
        return self.workspace.id


def _setup_extension_extra(workspace):
    """Contribute a workspace-dependent Entity in the setup phase."""
    assert workspace.id == "extension-setup-test"
    assert "extension-note" in workspace.entities
    return (ExtensionExtra,)


def _identity_extension_api(workspace):
    """Return the workspace for an API collision test."""
    return workspace


def _none_extension_api(workspace):
    """Return a deliberately falsy Extension API value."""
    return None


def _invalid_extension_setup(workspace):
    """Return an invalid setup result through the public activation path."""
    return "not-an-entity-list"


def _extension_rows(ws):
    return [row for row in ws.query(hb.MetaSchema).where({"kind": "extension"}).execute().rows() if not row["object_id"].startswith("registration:")]


def test_fixed_foundation_and_required_capsule_are_bootstrapped():
    ws = hb.HeavenBase("extension-system-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})

    assert "system" not in ws.extensions()
    assert "capsule" in ws.extensions()
    assert set(FOUNDATION_ENTITY_IDS) <= set(ws.entities)
    assert "database" not in ws.extensions()
    assert not hasattr(ws, "database")
    assert ws.list_entities() == ["agent-agent", "agent-message", "agent-session", "agent-skill"]
    rows = _extension_rows(ws)
    capsule_row = next(row for row in rows if row["subject_id"] == "capsule")
    assert capsule_row["meta"]["required"] is True
    assert capsule_row["meta"]["entities"] == ["sys-capsule"]


def test_custom_extension_registers_entities_and_metadata():
    extension_id = "test-extension"
    extension = hb.ext.Extension(
        extension_id,
        name="Test Extension",
        desc="Test-only entity extension.",
        entities=(ExtensionNote,),
        tags={"future tag": True},
    ).register()
    ws = hb.HeavenBase("extension-custom-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})

    enabled = ws.enable_extension(extension_id)

    assert enabled is extension
    assert hb.ext.Extension.load(extension_id) is extension
    assert "extension-note" in ws.list_entities()
    row = next(row for row in _extension_rows(ws) if row["subject_id"] == extension_id)
    assert row["name"] == "Test Extension"
    assert row["tags"] == ["extension"]
    assert row["meta"]["entities"] == ["extension-note"]
    record = hb.DEFAULT_CONTEXT.modules().inspect("extension", extension_id)[0]["record"]
    assert record["meta"]["tags"] == {"future tag": True}
    assert hb.DEFAULT_CONTEXT.modules().tag("extension", extension_id, "future tag").status == "unresolved"
    assert "tags" not in record["meta"]["definition"]


def test_custom_extension_can_attach_workspace_api_namespace():
    extension_id = "test-api-extension"

    hb.ext.Extension(
        extension_id,
        name="Test API Extension",
        desc="Test-only API extension.",
        api=TestExtensionAPI,
    ).register()
    definition = hb.DEFAULT_CONTEXT.modules().inspect("extension", extension_id)[0]["record"]["meta"]["definition"]
    ws = hb.HeavenBase("extension-api-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})

    assert not hasattr(ws, "test_api_extension")

    ws.enable_extension(extension_id)

    assert ws.test_api_extension.name() == "extension-api-test"
    assert definition["api"]["kind"] == "heavenbase.executable"
    assert definition["api"]["version"] == 1
    assert definition["api"]["source"] == "python-import"
    assert definition["api"]["target"].endswith("test_extensions:TestExtensionAPI")
    assert ws.extensions()[extension_id]["api_name"] == "test_api_extension"
    row = next(row for row in _extension_rows(ws) if row["subject_id"] == extension_id)
    assert row["meta"]["api_name"] == "test_api_extension"


def test_extension_identifier_uses_public_identifier_shape():
    with pytest.raises(ValueError, match="extension identifier"):
        hb.ext.Extension("Bad Extension")


def test_extension_registry_is_idempotent_and_rejects_conflicts():
    extension = hb.ext.Extension("test-registry-extension", desc="Registry contract.")

    assert extension.register() is extension
    assert hb.ext.Extension("test-registry-extension", desc="Registry contract.").register() is extension
    with pytest.raises(ValueError, match="already registered"):
        hb.ext.Extension("test-registry-extension", desc="Different contract.").register()


def test_extension_setup_contributes_workspace_dependent_entities():
    extension = hb.ext.Extension("test-setup-extension", entities=(ExtensionNote,), setup=_setup_extension_extra).register()
    ws = hb.HeavenBase("extension-setup-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})

    assert ws.enable_extension(extension.identifier) is extension
    assert extension.entity_ids() == ["extension-note"]
    assert "extension-note" in ws.list_entities()
    assert "extension-extra" in ws.list_entities()
    row = next(row for row in _extension_rows(ws) if row["subject_id"] == extension.identifier)
    assert row["meta"]["entities"] == ["extension-note", "extension-extra"]


def test_extension_api_collision_fails_before_registering_entities():
    extension = hb.ext.Extension(
        "test-api-collision",
        entities=(ExtensionNote,),
        api=_identity_extension_api,
        api_name="query",
    ).register()
    ws = hb.HeavenBase("extension-api-collision-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})

    with pytest.raises(AttributeError, match="conflicts with an existing workspace attribute"):
        ws.enable_extension(extension.identifier)

    assert "extension-note" not in ws.entities
    assert extension.identifier not in ws.extensions()


def test_extension_api_collision_names_both_extension_owners_before_activation():
    first = hb.ext.Extension(
        "test-api-owner-first",
        api=_identity_extension_api,
        api_name="shared_extension_api",
    ).register()
    second = hb.ext.Extension(
        "test-api-owner-second",
        requires=(first.identifier,),
        api=_identity_extension_api,
        api_name="shared_extension_api",
    ).register()
    ws = hb.HeavenBase("extension-api-owner-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})

    try:
        with pytest.raises(AttributeError, match="test-api-owner-second.*conflicts with extension 'test-api-owner-first'"):
            ws.enable_extension(second.identifier)

        assert first.identifier not in ws.extensions()
        assert second.identifier not in ws.extensions()
        assert not hasattr(ws, "shared_extension_api")
    finally:
        hb.DEFAULT_CONTEXT.modules().unregister("extension", second.identifier)
        hb.DEFAULT_CONTEXT.modules().unregister("extension", first.identifier)


def test_extension_activation_is_workspace_owned_and_reuses_falsy_api_values():
    extension = hb.ext.Extension("test-falsy-api", api=_none_extension_api).register()
    ws = hb.HeavenBase("extension-falsy-api-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})

    try:
        assert not hasattr(extension, "enable")
        assert ws.enable_extension(extension.identifier) is extension
        assert ws.enable_extension(extension.identifier) is extension
        assert hasattr(ws, "test_falsy_api")
        assert ws.test_falsy_api is None
    finally:
        hb.DEFAULT_CONTEXT.modules().unregister("extension", extension.identifier)


def test_extension_setup_rejects_string_results():
    extension = hb.ext.Extension("test-invalid-setup", setup=_invalid_extension_setup).register()
    ws = hb.HeavenBase("extension-invalid-setup-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})

    try:
        with pytest.raises(TypeError, match="iterable of entity declarations"):
            ws.enable_extension(extension.identifier)
        assert extension.identifier not in ws.extensions()
    finally:
        hb.DEFAULT_CONTEXT.modules().unregister("extension", extension.identifier)


def test_required_extension_discovery_keeps_inactive_source_paths_lazy(tmp_path, monkeypatch):
    from heavenbase.modules.artifacts import ModuleArtifactStore
    from heavenbase.modules.discovery import DiscoveredModule
    from heavenbase.modules.service import ModuleService
    import heavenbase.modules.service as module_lifecycle
    from heavenbase.utils.registry import Registry
    from heavenbase.workspace.setup.extensions import trusted_required_extension_ids

    def artifact(identifier: str, *, required: bool, system_scope: bool) -> tuple[Path, dict, Path]:
        root = tmp_path / identifier
        root.mkdir()
        marker = tmp_path / f"{identifier}.loaded"
        (root / "__init__.py").write_text("", encoding="utf-8")
        (root / "entity.py").write_text(
            "from pathlib import Path\n"
            "import heavenbase as hb\n"
            f"Path({str(marker)!r}).write_text('loaded', encoding='utf-8')\n"
            "class LazyEntity(hb.Entity):\n"
            f"    identifier = {f'{identifier}-entity'!r}\n"
            "    name = hb.field(hb.ShortText).default('')\n",
            encoding="utf-8",
        )
        groups = "    groups: {scope: [system]}\n" if system_scope else ""
        descriptor = (
            "manifest_version: 2\n"
            f"coordinate: example/{identifier}\n"
            "version: 1.0.0\n"
            "compatibility: {}\n"
            "items:\n"
            "  - kind: entity\n"
            f"    identifier: {identifier}-entity\n"
            "    name: Lazy Entity\n"
            "    desc: Source-path laziness fixture.\n"
            "    source: path\n"
            "    target: {module: entity, qualname: LazyEntity}\n"
            "    active: true\n"
            "    meta: {schema_version: 1}\n"
            "  - kind: extension\n"
            f"    identifier: {identifier}\n"
            f"{groups}"
            "    name: Lazy Extension\n"
            "    desc: Source-path laziness fixture.\n"
            "    source: inline\n"
            "    target: definition\n"
            "    active: true\n"
            "    meta:\n"
            "      schema_version: 1\n"
            f"      dependencies: [entity:{identifier}-entity]\n"
            "      definition:\n"
            f"        identifier: {identifier}\n"
            "        name: Lazy Extension\n"
            "        version: 1.0.0\n"
            "        desc: Source-path laziness fixture.\n"
            f"        required: {str(required).lower()}\n"
            "        requires: []\n"
            "        tags: [test]\n"
            f"        entities: [{identifier}-entity]\n"
            "        meta: {}\n"
            "        setup: null\n"
            "        api: null\n"
            "        api_name: null\n"
        )
        (root / "meta.yaml").write_text(descriptor, encoding="utf-8")
        return root, yaml.safe_load(descriptor), marker

    optional_root, optional_manifest, optional_marker = artifact("trusted-optional", required=False, system_scope=False)
    untrusted_root, _, untrusted_marker = artifact("untrusted-required", required=True, system_scope=True)
    monkeypatch.setattr(
        module_lifecycle,
        "discover_package_modules",
        lambda package: (DiscoveredModule("trusted-optional/meta.yaml", optional_manifest, optional_root),),
    )
    service = ModuleService(Registry("system-modules"), artifacts=ModuleArtifactStore(tmp_path / "artifacts"))
    assert service.setup()
    service.install(untrusted_root)

    assert trusted_required_extension_ids(service) == ()
    assert not optional_marker.exists()
    assert not untrusted_marker.exists()

    assert hb.ext.Extension.load("trusted-optional", resolver=service).identifier == "trusted-optional"
    assert hb.ext.Extension.load("untrusted-required", resolver=service).identifier == "untrusted-required"
    assert optional_marker.read_text(encoding="utf-8") == "loaded"
    assert untrusted_marker.read_text(encoding="utf-8") == "loaded"


def test_programmatic_extensions_cannot_claim_universal_activation():
    with pytest.raises(ValueError, match="installer-owned"):
        hb.ext.Extension("test-required-extension", entities=(ExtensionNote,), required=True).register()


def test_prompt_entities_are_owned_by_default_prompt_extension():
    old_value = hb.CM_HVNB.get("heavenbase.extensions.default", default=None)
    try:
        hb.CM_HVNB.set("heavenbase.extensions.default", ["prompt"])
        ws = hb.HeavenBase("extension-prompt-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})

        assert "prompt" in ws.extensions()
        assert "sys-prompt" in ws.entities
        assert "sys-translation" in ws.entities
        assert ws.extensions()["prompt"]["required"] is False
        row = next(row for row in _extension_rows(ws) if row["subject_id"] == "prompt")
        assert "sys-prompt" in row["meta"]["entities"]
        assert "sys-translation" in row["meta"]["entities"]
    finally:
        if old_value is None:
            hb.CM_HVNB.unset("heavenbase.extensions.default")
        else:
            hb.CM_HVNB.set("heavenbase.extensions.default", old_value)


def test_default_extensions_can_be_disabled_by_config():
    old_value = hb.CM_HVNB.get("heavenbase.extensions.default", default=None)
    try:
        hb.CM_HVNB.set("heavenbase.extensions.default", [])
        ws = hb.HeavenBase("extension-no-defaults-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})

        assert sorted(ws.extensions()) == ["capsule"]
        assert set(PROMPT_ENTITY_IDS).isdisjoint(ws.entities)
        assert "sys-toolkit" not in ws.entities
        assert "db-database" not in ws.entities
    finally:
        if old_value is None:
            hb.CM_HVNB.unset("heavenbase.extensions.default")
        else:
            hb.CM_HVNB.set("heavenbase.extensions.default", old_value)


def test_configured_default_extensions_still_enable_custom_extensions():
    extension_id = "test-default-extension"
    hb.ext.Extension(
        extension_id,
        name="Default Extension",
        desc="Default test extension.",
        entities=(ExtensionNote,),
        tags={"test": True},
    ).register()
    old_value = hb.CM_HVNB.get("heavenbase.extensions.default", default=None)
    try:
        hb.CM_HVNB.set("heavenbase.extensions.default", [extension_id])
        ws = hb.HeavenBase("extension-config-default-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})
        assert extension_id in ws.extensions()
        assert "extension-note" in ws.list_entities()
    finally:
        if old_value is None:
            hb.CM_HVNB.unset("heavenbase.extensions.default")
        else:
            hb.CM_HVNB.set("heavenbase.extensions.default", old_value)
        hb.DEFAULT_CONTEXT.modules().unregister("extension", extension_id)


def test_extension_activation_resolves_exact_dependencies_but_tracks_only_requested_roots():
    base = hb.ext.Extension("test-dependency-base", entities=(ExtensionNote,)).register()
    dependent = hb.ext.Extension("test-dependency-leaf", requires=(base.identifier,), entities=(ExtensionExtra,)).register()
    try:
        ws = hb.HeavenBase("extension-dependency-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})

        ws.enable_extension(dependent.identifier)

        assert base.identifier in ws.extensions()
        assert dependent.identifier in ws.extensions()
        assert ws._extension_service.roots()[-1:] == (dependent.identifier,)
    finally:
        hb.DEFAULT_CONTEXT.modules().unregister("extension", dependent.identifier)
        hb.DEFAULT_CONTEXT.modules().unregister("extension", base.identifier)


def test_extension_activation_reports_the_full_dependency_cycle():
    left = hb.ext.Extension("test-cycle-left", requires=("test-cycle-right",)).register()
    right = hb.ext.Extension("test-cycle-right", requires=(left.identifier,)).register()
    try:
        ws = hb.HeavenBase("extension-cycle-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})

        with pytest.raises(ValueError, match=r"test-cycle-left -> test-cycle-right -> test-cycle-left"):
            ws.enable_extension(left.identifier)
    finally:
        hb.DEFAULT_CONTEXT.modules().unregister("extension", right.identifier)
        hb.DEFAULT_CONTEXT.modules().unregister("extension", left.identifier)


def test_extensions_cannot_replace_fixed_workspace_schemas():
    extension = hb.ext.Extension("test-fixed-claim", entities=(hb.Catalog,)).register()
    try:
        ws = hb.HeavenBase("extension-fixed-claim-test", detached=True, backends={"main": hb.backends.InMemBackend("main")})

        with pytest.raises(ValueError, match="fixed workspace Entities"):
            ws.enable_extension(extension.identifier)
    finally:
        hb.DEFAULT_CONTEXT.modules().unregister("extension", extension.identifier)
