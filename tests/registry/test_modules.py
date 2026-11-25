"""Central module registration, installation, and resolution contracts."""

from __future__ import annotations

from pathlib import Path
import sys
import threading
import types
import warnings

import pytest
import yaml

import heavenbase as hb
from heavenbase.entity.registry import load_entity_artifact_export
from heavenbase.modules import ExecutableRef, InstallReceipt, ModuleService
from heavenbase.modules.artifacts import ModuleArtifactStore
from heavenbase.modules.discovery import DiscoveredModule
from heavenbase.utils import cmd, loads_json
from heavenbase.utils.config import ConfigManager
from heavenbase.utils.registry import Registry, RegistryConflictError, SQLiteRegistryStore


def _record(source="inline", target="definition", **meta):
    return {
        "name": "Example",
        "desc": "Test registration.",
        "source": source,
        "target": target,
        "active": True,
        "meta": {"schema_version": 1, "definition": {"ok": True}, **meta},
    }


def _manifest(*items, coordinate="example/test-owner", bundle=None, version="1.0.0"):
    value = {
        "manifest_version": 2,
        "coordinate": coordinate,
        "version": version,
        "compatibility": {"heavenbase": {"min": "0.1.2.1", "before": "0.1.3.0"}},
        "items": list(items),
    }
    if bundle is not None:
        value["bundle"] = bundle
    return value


def _item(identifier, *, kind="test_kind", aliases=(), groups=None, record=None):
    return {
        "kind": kind,
        "identifier": identifier,
        "aliases": list(aliases),
        "groups": groups or {},
        **(record or _record()),
    }


def _bootstrap(tmp_path):
    return {
        "version": 1,
        "root": str(tmp_path),
        "workspace": "default",
        "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
        "registry": {"hash": "sha256", "max_items": None, "max_bytes": 4 * 1024 * 1024},
    }


def test_module_service_register_inspect_resolve_and_reconcile():
    service = ModuleService(Registry("system-modules"))

    first = service.register(
        "example_kind",
        "alpha",
        _record(),
        aliases=("first",),
        groups=(("family", "demo"),),
        coordinate="example/author",
    )
    second = service.register(
        "example_kind",
        "alpha",
        _record(),
        aliases=("first",),
        groups=(("family", "demo"),),
        coordinate="example/author",
    )

    assert first.coordinate == second.coordinate == "example/author"
    assert first.generation != second.generation
    assert first.manifest_fingerprint == second.manifest_fingerprint
    assert isinstance(first, InstallReceipt)
    assert first.keys == (("example_kind", "alpha"),)
    assert service.resolve("example_kind", "first") == {"ok": True}
    inspection = service.inspect("example_kind", "first")
    assert inspection == (
        {
            "kind": "example_kind",
            "identifier": "alpha",
            "aliases": ["first"],
            "groups": {"family": ["demo"]},
            "record": service.catalog[("example_kind", "alpha")],
            "publication": service.catalog.publication,
            "revision": 0,
            "fingerprint": inspection[0]["fingerprint"],
        },
    )
    inspection[0]["record"]["meta"]["definition"]["ok"] = False
    assert service.resolve("example_kind", "alpha") == {"ok": True}

    changed = _record()
    changed["meta"]["definition"]["ok"] = False
    service.register("example_kind", "alpha", changed, aliases=("first",), groups=(("family", "demo"),), coordinate="example/author")
    assert service.resolve("example_kind", "alpha") == {"ok": False}

    with pytest.raises(ValueError, match="belongs to module"):
        service.register("example_kind", "alpha", _record(), coordinate="example/other")


def test_module_service_requires_snapshot_values_and_shared_catalogs_observe_process_publications():
    with pytest.raises(ValueError, match="value_policy='snapshot'"):
        ModuleService(Registry("system-modules", value_policy="process_identity"))

    registry = Registry("system-modules")
    writer = ModuleService(registry)
    reader = ModuleService(registry)
    assert reader.inspect() == ()

    writer.install(_manifest(_item("published")))

    assert reader.inspect("test_kind", "published")[0]["identifier"] == "published"


@pytest.mark.parametrize("field", ["kind", "identifier", "aliases", "groups", "coordinate", "publisher", "generation"])
def test_programmatic_registration_rejects_installer_envelope_fields_atomically(field):
    service = ModuleService(Registry("system-modules"))
    record = _record()
    record[field] = "caller-owned"

    with pytest.raises(ValueError, match="installer envelope fields"):
        service.register("test_kind", "guarded", record)

    assert service.inspect() == ()


def test_module_coordinate_generation_and_publisher_are_independent_identities():
    service = ModuleService(Registry("system-modules"))
    first = service.install(_manifest(_item("first"), coordinate="example/first"), publisher="example-publisher")
    second = service.install(_manifest(_item("second"), coordinate="example/second"), publisher="example-publisher")

    identical = service.install(_manifest(_item("first"), coordinate="example/first"), publisher="example-publisher")

    assert identical.generation != first.generation
    assert identical.manifest_fingerprint == first.manifest_fingerprint
    assert service.inspect("test_kind", "second")
    with pytest.raises(ValueError, match="stale"):
        service.uninstall(first)
    with pytest.raises(ValueError, match="published by"):
        service.install(_manifest(_item("first"), coordinate="example/first"), publisher="another-publisher")
    assert service.inspect("test_kind", "first")
    assert service.inspect("test_kind", "second")

    service.uninstall(identical)
    service.uninstall(second)
    assert service.inspect() == ()


def test_module_catalog_is_detached_and_retired_rows_require_explicit_inspection():
    service = ModuleService(Registry("system-modules"))
    service.install(_manifest(_item("detached", aliases=("alias",), groups={"family": ["demo"]})))

    record = service.catalog[("test_kind", "alias")]
    record["meta"]["definition"]["ok"] = False
    inspected = service.catalog.inspect("test_kind", "alias")
    inspected[0]["record"]["meta"]["definition"]["ok"] = False

    assert service.resolve("test_kind", "detached") == {"ok": True}
    assert service.catalog.resolve(("test_kind", "alias")) == ("test_kind", "detached")
    assert service.catalog.aliases(("test_kind", "detached")) == (("test_kind", "alias"),)
    assert service.catalog.groups(("test_kind", "detached")) == (("family", "demo"),)

    service.retire("test_kind", "alias")

    assert service.inspect("test_kind", "detached") == ()
    inactive = service.inspect("test_kind", "detached", include_inactive=True)
    assert inactive[0]["record"]["active"] is False
    assert inactive[0]["revision"] == service.catalog.revision


def test_metaschema_capability_projection_batches_module_alias_inspection(monkeypatch):
    workspace = hb.HeavenBase("batched-capability-aliases", detached=True, backends={"main": {"type": "inmem"}})
    resolver = workspace.module_resolver
    original = resolver.inspect
    inspections = []

    def counted(kind=None, identifier=None, *, include_inactive=False):
        inspections.append((kind, identifier, include_inactive))
        return original(kind, identifier, include_inactive=include_inactive)

    monkeypatch.setitem(vars(resolver), "inspect", counted)
    # Construction already built and kept the registry rows; build them again.
    workspace._metaschema.registry._registry_memo = None

    rows = workspace._metaschema.registry.workspace_rows()

    assert any(row["kind"] == "capability" for row in rows)
    assert inspections.count((None, None, False)) == 1
    assert not any(kind == "operation" for kind, _identifier, _include_inactive in inspections)


def test_record_fingerprint_accepts_read_only_mapping_values():
    from types import MappingProxyType

    from heavenbase.modules.manifest import _record_fingerprint

    record = MappingProxyType(
        {
            "active": True,
            "meta": MappingProxyType({"schema_version": 1}),
        }
    )

    assert _record_fingerprint(record) == _record_fingerprint({"active": True, "meta": {"schema_version": 1}})


def test_manifest_v2_compatibility_and_installer_owned_provenance_are_enforced(monkeypatch):
    import heavenbase.modules.service as module_lifecycle

    service = ModuleService(Registry("system-modules"))
    legacy = _manifest(_item("legacy"))
    legacy["manifest_version"] = 1
    with pytest.raises(ValueError, match="manifest_version must be 2"):
        service.install(legacy)

    provenance = _record()
    provenance["meta"]["installation"] = {"owner": "caller"}
    with pytest.raises(ValueError, match="installer-owned"):
        service.register("test_kind", "forged", provenance)

    monkeypatch.setattr(module_lifecycle, "__version__", "0.1.2.1")
    future = _manifest(_item("future"), coordinate="example/future")
    future["compatibility"] = {"heavenbase": {"min": "0.1.2.2"}}
    with pytest.raises(ValueError, match=r"requires HeavenBase >= 0\.1\.2\.2"):
        service.install(future)

    bounded = _manifest(_item("bounded"), coordinate="example/bounded")
    service.install(bounded)
    monkeypatch.setattr(module_lifecycle, "__version__", "0.1.3.0")
    with pytest.raises(ValueError, match=r"requires HeavenBase < 0\.1\.3\.0"):
        service.resolve("test_kind", "bounded")


def test_bundle_uninstall_rejects_a_name_shared_by_independent_installations():
    service = ModuleService(Registry("system-modules"))
    service.install(_manifest(_item("first"), coordinate="example/first", bundle="shared"))
    service.install(_manifest(_item("second"), coordinate="example/second", bundle="shared"))

    with pytest.raises(ValueError, match="spans multiple installations"):
        service.uninstall_bundle("shared")

    assert service.inspect("test_kind", "first")
    assert service.inspect("test_kind", "second")


def test_module_service_supports_python_import_and_rejects_ephemeral_locators():
    service = ModuleService(Registry("system-modules"))
    service.register("serializer", "json-loads", _record("python-import", "json:loads"))
    service.register(
        "serializer",
        "fallback",
        _record("python-import", "missing_heavenbase_fixture:value", restore_layers=[{"source": "inline", "target": "definition"}]),
    )

    assert service.resolve("serializer", "json-loads")('{"ok": true}') == {"ok": True}
    assert service.resolve("serializer", "fallback") == {"ok": True}
    with pytest.raises(ValueError, match="module:qualified_name"):
        service.register("serializer", "nested", _record("python-import", "tests.fake:<locals>.call"))


def test_module_service_uses_an_injected_source_restorer_with_its_own_resolver():
    restored = object()
    seen = []

    def restore_capsule(target, resolver):
        seen.append((target, resolver))
        return restored

    service = ModuleService(
        Registry("system-modules"),
        source_restorers={"capsule": restore_capsule},
    )
    service.register("test_kind", "capsule-backed", _record("capsule", "capsule-reference"))

    assert service.resolve("test_kind", "capsule-backed") is restored
    assert seen == [("capsule-reference", service)]


def test_module_service_resolves_versioned_python_and_capsule_executable_refs():
    seen = []

    def restore_capsule(target, resolver):
        seen.append((target, resolver))
        return lambda value: f"capsule:{value}"

    service = ModuleService(
        Registry("system-modules"),
        source_restorers={"capsule": restore_capsule},
    )

    python_fn = service.resolve_executable(ExecutableRef.python("json:loads").to_dict())
    capsule_fn = service.resolve_executable(ExecutableRef.capsule("capsule-id").to_dict())

    assert python_fn('{"ok": true}') == {"ok": True}
    assert capsule_fn("value") == "capsule:value"
    assert seen == [("capsule-id", service)]


def test_nested_path_executable_ref_restores_from_owning_module_artifact(tmp_path):
    source = tmp_path / "nested-executable"
    source.mkdir()
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "runtime.py").write_text(
        "def build():\n    from .value import VALUE\n    return VALUE + 1\n",
        encoding="utf-8",
    )
    (source / "value.py").write_text("VALUE = 41\n", encoding="utf-8")
    (source / "meta.yaml").write_text(
        """
manifest_version: 2
coordinate: example/nested-executable
version: 1.0.0
compatibility: {heavenbase: {min: 0.1.2.1, before: 0.1.3.0}}
items:
  - kind: test_kind
    identifier: nested
    name: Nested executable
    desc: Artifact-owned nested executable.
    source: inline
    target: definition
    active: true
    meta:
      schema_version: 1
      definition:
        build:
          kind: heavenbase.executable
          version: 1
          source: path
          target: {module: runtime, qualname: build}
""".lstrip(),
        encoding="utf-8",
    )
    service = ModuleService(
        Registry("system-modules"),
        artifacts=ModuleArtifactStore(tmp_path / "artifacts"),
    )

    service.install(source)
    row = service.inspect("test_kind", "nested")[0]
    definition = service.resolve("test_kind", "nested")
    source.rename(tmp_path / "retired-source")
    build = service.resolve_executable(definition["build"], owner=row)
    service.install(_manifest(_item("unrelated"), coordinate="example/unrelated"))

    assert definition["build"]["source"] == "artifact"
    assert build() == 42
    assert build.__module__.startswith("_heavenbase_artifact_")


def test_entity_artifact_export_is_anchored_at_the_root_for_nested_entity_modules(tmp_path):
    source = tmp_path / "nested-entity-export"
    source.mkdir()
    (source / "__init__.py").write_text(
        "from heavenbase import Entity, ShortText, field\n"
        "class RootEntity(Entity):\n"
        "    identifier = 'root-artifact-entity'\n"
        "    value = field(ShortText)\n",
        encoding="utf-8",
    )
    (source / "models").mkdir()
    (source / "models" / "__init__.py").write_text("", encoding="utf-8")
    (source / "models" / "entity.py").write_text(
        "from heavenbase import Entity, ShortText, field\n"
        "class NestedEntity(Entity):\n"
        "    identifier = 'nested-artifact-entity'\n"
        "    value = field(ShortText)\n",
        encoding="utf-8",
    )
    (source / "services").mkdir()
    (source / "services" / "__init__.py").write_text("", encoding="utf-8")
    (source / "services" / "api.py").write_text("VALUE = 42\n", encoding="utf-8")
    (source / "meta.yaml").write_text(
        """
manifest_version: 2
coordinate: example/nested-entity-export
version: 1.0.0
compatibility: {heavenbase: {min: 0.1.2.1, before: 0.1.3.0}}
items:
  - kind: entity
    identifier: root-artifact-entity
    name: RootArtifactEntity
    desc: Root-package Entity used to anchor a sibling artifact export.
    source: path
    target: {module: null, qualname: RootEntity}
    active: true
    meta: {schema_version: 1}
  - kind: entity
    identifier: nested-artifact-entity
    name: NestedArtifactEntity
    desc: Nested Entity used to anchor a sibling artifact export.
    source: path
    target: {module: models.entity, qualname: NestedEntity}
    active: true
    meta: {schema_version: 1}
""".lstrip(),
        encoding="utf-8",
    )
    service = ModuleService(
        Registry("system-modules"),
        artifacts=ModuleArtifactStore(tmp_path / "artifacts"),
    )

    service.install(source)
    entity = service.resolve("entity", "nested-artifact-entity")
    root_entity = service.resolve("entity", "root-artifact-entity")

    assert entity.__module__.split(".", 1)[1] == "models.entity"
    assert root_entity.__module__.startswith("_heavenbase_artifact_")
    assert "." not in root_entity.__module__
    assert (
        load_entity_artifact_export(
            "nested-artifact-entity",
            "services.api",
            "VALUE",
            resolver=service,
        )
        == 42
    )
    assert (
        load_entity_artifact_export(
            "root-artifact-entity",
            "services.api",
            "VALUE",
            resolver=service,
        )
        == 42
    )


def test_nested_artifact_executable_ref_is_installer_owned(tmp_path):
    service = ModuleService(
        Registry("system-modules"),
        artifacts=ModuleArtifactStore(tmp_path / "artifacts"),
    )
    record = _record()
    record["meta"]["definition"]["build"] = {
        "kind": "heavenbase.executable",
        "version": 1,
        "source": "artifact",
        "target": {
            "artifact": "0" * 64,
            "module": "runtime",
            "qualname": "build",
        },
    }

    with pytest.raises(ValueError, match="installer-owned"):
        service.register("test_kind", "forged-executable", record)


def test_context_capsule_adapter_falls_back_only_for_an_absent_reference(tmp_path, monkeypatch):
    from heavenbase.capsule.errors import CapsuleNotFoundError
    from heavenbase.capsule import registry as capsule_registry_module

    config = ConfigManager(default={}, setup=False)
    context = hb.Context.load(_bootstrap(tmp_path), config=config)
    service = context.modules()
    seen = []

    class MissingCapsuleRegistry:
        def load(self, capsule_id):
            seen.append(capsule_id)
            raise CapsuleNotFoundError(capsule_id)

    def registry(*, resolver, config):
        assert resolver is service
        assert config is context.config
        return MissingCapsuleRegistry()

    monkeypatch.setattr(capsule_registry_module, "capsule_registry", registry)
    service.register(
        "test_kind",
        "capsule-fallback",
        _record(
            "capsule",
            "missing-capsule",
            restore_layers=[{"source": "inline", "target": "definition"}],
        ),
    )

    assert service.resolve("test_kind", "capsule-fallback") == {"ok": True}
    assert seen == ["missing-capsule"]
    context.close()


def test_module_source_restorer_errors_do_not_silently_fall_back():
    def broken_capsule(target, resolver):
        del target, resolver
        raise KeyError("corrupt-capsule")

    service = ModuleService(
        Registry("system-modules"),
        source_restorers={"capsule": broken_capsule},
    )
    service.register(
        "test_kind",
        "capsule-error",
        _record(
            "capsule",
            "corrupt-capsule",
            restore_layers=[{"source": "inline", "target": "definition"}],
        ),
    )

    with pytest.raises(KeyError, match="corrupt-capsule"):
        service.resolve("test_kind", "capsule-error")


def test_manifest_validation_and_bundle_publication_are_failure_atomic(tmp_path):
    registry = Registry.load("system-modules", store=SQLiteRegistryStore(tmp_path / "registry.db"))
    service = ModuleService(registry)
    service.install(_manifest(_item("existing", aliases=("taken",))))
    revision = registry.revision

    with pytest.raises(ValueError, match="unsupported"):
        service.install(
            _manifest(
                _item("would-publish"),
                _item("invalid", record=_record("artifact", "remote:item")),
                coordinate="example/invalid-source",
            )
        )
    assert registry.revision == revision
    assert ("test_kind", "would-publish") not in registry

    with pytest.raises(ValueError, match="already registered"):
        service.install(
            _manifest(
                _item("draft-only"),
                _item("conflict", aliases=("taken",)),
                coordinate="example/alias-conflict",
            )
        )
    assert registry.revision == revision
    assert ("test_kind", "draft-only") not in registry


def test_dependencies_retirement_and_uninstall_share_one_atomic_state(tmp_path):
    registry = Registry.load("system-modules", store=SQLiteRegistryStore(tmp_path / "registry.db"))
    service = ModuleService(registry)
    receipt = service.install(
        _manifest(
            _item("base"),
            _item("consumer", record=_record(dependencies=["test_kind:base"])),
            coordinate="example/dependency-bundle",
            bundle="dependency-demo",
        )
    )
    revision = registry.revision

    with pytest.raises(ValueError, match="inactive dependency"):
        service.retire("test_kind", "base")
    assert registry.revision == revision
    assert registry[("test_kind", "base")]["active"] is True

    with pytest.raises(ValueError, match="missing dependency"):
        service.unregister("test_kind", "base")
    assert registry.revision == revision

    removed = service.uninstall(receipt)
    assert len(removed) == 2
    assert registry.revision == revision + 1
    assert service.inspect() == ()

    service.install(_manifest(_item("retirable"), coordinate="example/retirement"))
    retired = service.retire("test_kind", "retirable")
    assert retired["active"] is False
    assert retired["meta"]["status"] == "retired"
    with pytest.raises(KeyError, match="retired"):
        service.resolve("test_kind", "retirable")
    assert service.unregister("test_kind", "retirable")["active"] is False


def test_registry_conflicts_require_explicit_refresh_and_retry(tmp_path):
    store_path = tmp_path / "registry.db"
    first = ModuleService(Registry.load("system-modules", store=SQLiteRegistryStore(store_path)))
    stale = ModuleService(Registry.load("system-modules", store=SQLiteRegistryStore(store_path)))

    first.install(_manifest(_item("first"), coordinate="example/first-owner"))
    with pytest.raises(RegistryConflictError, match="refresh before retrying"):
        stale.install(_manifest(_item("second"), coordinate="example/second-owner"))
    assert stale.catalog.revision == 0
    assert stale.refresh()
    receipt = stale.install(_manifest(_item("second"), coordinate="example/second-owner"))

    assert receipt.revision == 2
    assert first.refresh()
    assert tuple(key[1] for key in first.catalog) == ("first", "second")


def test_builtin_module_setup_reconciles_after_concurrent_writer(tmp_path):
    store_path = tmp_path / "registry.db"
    artifact_path = tmp_path / "artifacts"
    first = ModuleService(
        Registry.load("system-modules", store=SQLiteRegistryStore(store_path)),
        artifacts=ModuleArtifactStore(artifact_path),
    )
    stale = ModuleService(
        Registry.load("system-modules", store=SQLiteRegistryStore(store_path)),
        artifacts=ModuleArtifactStore(artifact_path),
    )

    assert first.setup()
    stale.setup()

    assert stale.catalog.revision == first.catalog.revision
    assert stale.inspect() == first.inspect()
    assert stale.catalog.get(("module_set", "heavenbase")) is not None


def test_module_folder_validation_is_failure_atomic(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "meta.yaml").write_text(
        "manifest_version: 2\ncoordinate: example/failure-atomic\nversion: 1.0.0\ncompatibility: {}\nitems:\n"
        "  - kind: test_kind\n    identifier: valid\n"
        "    name: Valid\n    desc: Valid packaged record.\n    source: inline\n"
        "    target: definition\n    active: true\n    meta: {schema_version: 1, definition: {ok: true}}\n"
        "  - kind: test_kind\n    identifier: invalid\n    name: Invalid\n"
        "    desc: Invalid packaged record.\n    source: inline\n    target: missing\n"
        "    active: true\n    meta: {schema_version: 1}\n",
        encoding="utf-8",
    )
    service = ModuleService(Registry("system-modules"))

    with pytest.raises(ValueError, match="inline target"):
        service.install(package)

    assert service.catalog.revision == 0
    assert service.inspect() == ()


def test_persistent_path_install_requires_an_explicit_durable_artifact_store(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "meta.yaml").write_text(
        "manifest_version: 2\ncoordinate: example/durable-artifact\nversion: 1.0.0\ncompatibility: {}\nitems:\n"
        "  - kind: test_kind\n    identifier: durable\n    name: Durable\n    desc: Durable artifact fixture.\n"
        "    source: path\n    target: {module: null, qualname: VALUE}\n    active: true\n    meta: {schema_version: 1}\n",
        encoding="utf-8",
    )
    service = ModuleService(Registry.load("system-modules", store=SQLiteRegistryStore(tmp_path / "registry.db")))

    with pytest.raises(ValueError, match="explicit durable ModuleArtifactStore"):
        service.install(package)

    assert service.inspect() == ()


def test_module_folder_rejects_registration_yaml_without_meta_yaml(tmp_path):
    package = tmp_path / "legacy-package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "registration.yaml").write_text(
        "manifest_version: 2\ncoordinate: example/legacy-package\nversion: 1.0.0\ncompatibility: {}\nitems:\n"
        "  - kind: test_kind\n    identifier: legacy\n"
        "    name: Legacy\n    desc: Legacy folder name.\n    source: inline\n"
        "    target: definition\n    active: true\n    meta: {schema_version: 1, definition: {ok: true}}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="meta.yaml"):
        ModuleService(Registry("system-modules")).install(package)

    service = ModuleService(Registry("system-modules"))
    with pytest.raises(ValueError, match="must be named 'meta.yaml'"):
        service.install(package / "registration.yaml")
    arbitrary = package / "module.yaml"
    arbitrary.write_text((package / "registration.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError, match="must be named 'meta.yaml'"):
        service.install(arbitrary)


def test_package_discovery_uses_only_configured_roots_and_exact_descriptor_names(tmp_path, monkeypatch):
    from heavenbase.modules import discovery

    package = tmp_path / "package"
    (package / "resources").mkdir(parents=True)
    (package / "resources" / "modules.yaml").write_text("schema: heaven.modules/v1\nroots: [modules]\n", encoding="utf-8")
    alpha = package / "modules" / "alpha"
    zeta = package / "modules" / "zeta" / "nested"
    outside = package / "outside"
    for folder in (alpha, zeta, outside):
        folder.mkdir(parents=True)
    descriptor = (
        "manifest_version: 2\ncoordinate: example/IDENTIFIER\nversion: 1.0.0\ncompatibility: {}\nitems:\n"
        "  - kind: test_kind\n    identifier: IDENTIFIER\n"
        "    name: Fixture\n    desc: Discovery fixture.\n    source: inline\n"
        "    target: definition\n    active: true\n    meta: {schema_version: 1, definition: {ok: true}}\n"
    )
    (alpha / "meta.yaml").write_text(descriptor.replace("IDENTIFIER", "alpha"), encoding="utf-8")
    (zeta / "meta.yaml").write_text(descriptor.replace("IDENTIFIER", "zeta"), encoding="utf-8")
    (alpha / "registration.yaml").write_text(": deliberately invalid and ignored", encoding="utf-8")
    (outside / "meta.yaml").write_text(": deliberately invalid and outside the scan root", encoding="utf-8")
    monkeypatch.setattr(discovery.resources, "files", lambda package_name: package)

    found = discovery.discover_package_modules("fixture-package")

    assert tuple(module.name for module in found) == ("modules/alpha/meta.yaml", "modules/zeta/nested/meta.yaml")
    assert tuple(module.manifest["items"][0]["identifier"] for module in found) == ("alpha", "zeta")
    assert tuple(module.root for module in found) == (alpha, zeta)


def test_packaged_and_external_path_modules_share_the_artifact_loader(tmp_path, monkeypatch):
    import heavenbase.modules.service as module_lifecycle

    plugin = tmp_path / "portable"
    plugin.mkdir()
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "value.py").write_text("NUMBER = 41\n", encoding="utf-8")
    imported = tmp_path / "imported.txt"
    (plugin / "runtime.py").write_text(
        "from pathlib import Path\n"
        f"with Path({str(imported)!r}).open('a', encoding='utf-8') as stream:\n"
        "    stream.write('x')\n"
        "VALUE = {'portable': True}\n"
        "def build():\n"
        "    from .value import NUMBER\n"
        "    return NUMBER + 1\n",
        encoding="utf-8",
    )
    descriptor = """
manifest_version: 2
coordinate: example/artifact-parity
version: 1.0.0
compatibility: {}
items:
  - kind: parity_value
    identifier: portable
    name: Portable value
    desc: Top-level artifact reference.
    source: path
    target: {module: runtime, qualname: VALUE}
    active: true
    meta: {schema_version: 1}
  - kind: parity_factory
    identifier: portable
    name: Portable factory
    desc: Nested artifact reference.
    source: inline
    target: definition
    active: true
    meta:
      schema_version: 1
      definition:
        build:
          kind: heavenbase.executable
          version: 1
          source: path
          target: {module: runtime, qualname: build}
""".lstrip()
    (plugin / "meta.yaml").write_text(descriptor, encoding="utf-8")
    manifest = yaml.safe_load(descriptor)
    monkeypatch.setattr(
        module_lifecycle,
        "discover_package_modules",
        lambda package: (DiscoveredModule("portable/meta.yaml", manifest, plugin),),
    )
    packaged = ModuleService(Registry("system-modules"), artifacts=ModuleArtifactStore(tmp_path / "packaged-artifacts"))
    external = ModuleService(Registry("system-modules"), artifacts=ModuleArtifactStore(tmp_path / "external-artifacts"))

    assert packaged.setup()
    external.install(plugin)
    packaged_value = packaged.inspect("parity_value", "portable")[0]
    external_value = external.inspect("parity_value", "portable")[0]
    packaged_factory = packaged.inspect("parity_factory", "portable")[0]
    external_factory = external.inspect("parity_factory", "portable")[0]

    assert not imported.exists()
    assert packaged_value["record"]["source"] == external_value["record"]["source"] == "artifact"
    assert packaged_value["record"]["target"] == external_value["record"]["target"]
    packaged_definition = packaged.resolve("parity_factory", "portable")
    external_definition = external.resolve("parity_factory", "portable")
    assert packaged_definition["build"] == external_definition["build"]
    assert packaged_definition["build"]["source"] == "artifact"
    assert packaged_factory["record"]["meta"]["installation"]["publisher"] == "heavenbase"
    assert external_factory["record"]["meta"]["installation"]["publisher"] is None
    assert not imported.exists()

    assert packaged.resolve("parity_value", "portable") == external.resolve("parity_value", "portable") == {"portable": True}
    packaged_build = packaged.resolve_executable(packaged_definition["build"], owner=packaged_factory)
    external_build = external.resolve_executable(external_definition["build"], owner=external_factory)

    assert imported.read_text(encoding="utf-8") == "xx"
    assert packaged_build() == external_build() == 42
    assert packaged_build.__module__.startswith("_heavenbase_artifact_")
    assert external_build.__module__.startswith("_heavenbase_artifact_")
    assert packaged_build.__module__ != external_build.__module__


@pytest.mark.parametrize("root", ["../outside", "/absolute", "windows\\escape"])
def test_package_discovery_rejects_scan_roots_outside_the_package(tmp_path, monkeypatch, root):
    from heavenbase.modules import discovery

    package = tmp_path / "package"
    (package / "resources").mkdir(parents=True)
    (package / "resources" / "modules.yaml").write_text(f"schema: heaven.modules/v1\nroots: [{root!r}]\n", encoding="utf-8")
    monkeypatch.setattr(discovery.resources, "files", lambda package_name: package)

    with pytest.raises(ValueError, match="inside the package"):
        discovery.discover_package_modules("fixture-package")


def test_builtin_setup_validates_every_discovered_folder_before_publication(monkeypatch):
    import heavenbase.modules.service as module_lifecycle

    valid = _manifest(_item("valid"), coordinate="example/valid")
    invalid = _manifest(_item("invalid", record=_record(target="missing")), coordinate="example/invalid")
    monkeypatch.setattr(
        module_lifecycle,
        "discover_package_modules",
        lambda package: (
            DiscoveredModule("valid/meta.yaml", valid, Path(".")),
            DiscoveredModule("invalid/meta.yaml", invalid, Path(".")),
        ),
    )
    service = ModuleService(Registry("system-modules"))

    with pytest.raises(ValueError, match="invalid/meta.yaml"):
        service.setup()

    assert service.catalog.revision == 0
    assert service.inspect() == ()


def test_external_install_cannot_claim_builtin_owner_marker_or_trust_groups():
    service = ModuleService(Registry("system-modules"))
    assert service.setup()
    revision = service.catalog.revision
    rows = service.inspect()
    attempts = (
        _manifest(_item("coordinate-claim"), coordinate="heavenbase/external"),
        _manifest(_item("heavenbase", kind="module_set"), coordinate="example/external-owner"),
        _manifest(_item("marker-alias", kind="module_set", aliases=("heavenbase",)), coordinate="example/external-owner"),
        _manifest(_item("provenance-claim", groups={"provenance": ["system"]}), coordinate="example/external-owner"),
        _manifest(_item("tag-claim", groups={"tag": ["builtin"]}), coordinate="example/external-owner"),
    )

    for attempt in attempts:
        with pytest.raises(ValueError, match="reserved for built-in package setup"):
            service.install(attempt)
        assert service.catalog.revision == revision
        assert service.inspect() == rows


def test_public_lifecycle_cannot_desynchronize_builtin_records_from_the_module_set_marker():
    service = ModuleService(Registry("system-modules"))
    assert service.setup()
    rows = service.inspect()
    revision = service.catalog.revision
    file_row = service.inspect("backend_type", "file")[0]
    installation = file_row["record"]["meta"]["installation"]
    coordinate_rows = tuple(row for row in rows if row["record"]["meta"]["installation"]["coordinate"] == installation["coordinate"])
    receipt = InstallReceipt(
        installation["coordinate"],
        installation["generation"],
        tuple((row["kind"], row["identifier"]) for row in coordinate_rows),
        ("backend-file",),
        revision,
        installation["manifest_fingerprint"],
    )

    attempts = (
        lambda: service.retire("backend_type", "file"),
        lambda: service.unregister("backend_type", "file"),
        lambda: service.uninstall(receipt),
        lambda: service.uninstall_bundle("backend-file"),
    )
    for attempt in attempts:
        with pytest.raises(ValueError, match="built-in package record"):
            attempt()
        assert service.catalog.revision == revision
        assert service.inspect("backend_type", "file")

    assert not service.setup()
    assert service.catalog.get(("module_set", "heavenbase")) is not None


def test_package_setup_reconciles_the_running_installation_without_version_policy(monkeypatch):
    import heavenbase.modules.service as module_lifecycle

    source = _manifest(_item("packaged"), coordinate="heavenbase/test-packaged", version="0.1.2.2")
    source["compatibility"] = {}
    monkeypatch.setattr(
        module_lifecycle,
        "discover_package_modules",
        lambda package: (DiscoveredModule("fixture/meta.yaml", source, Path(".")),),
    )
    service = ModuleService(Registry("system-modules"))
    monkeypatch.setattr(module_lifecycle, "__version__", "0.1.2.2")
    assert service.setup()
    assert service.resolve("module_set", "heavenbase")["version"] == "0.1.2.2"

    # Package versions are opaque cache identity, not an ordering policy. A
    # process must reconcile its own exact packaged descriptors regardless of
    # whether another installation's label sorts before or after it.
    monkeypatch.setattr(module_lifecycle, "__version__", "0.1.2.1")
    assert service.setup()
    assert service.resolve("module_set", "heavenbase")["version"] == "0.1.2.1"

    monkeypatch.setattr(module_lifecycle, "__version__", "build-local")
    assert service.setup()
    assert service.resolve("module_set", "heavenbase")["version"] == "build-local"
    assert not service.setup()


def test_setup_refreshes_a_stale_current_marker_before_version_decision(tmp_path, monkeypatch):
    import heavenbase.modules.service as module_lifecycle

    source = _manifest(_item("packaged"), coordinate="heavenbase/test-packaged", version="0.1.2.1")
    source["compatibility"] = {}
    monkeypatch.setattr(
        module_lifecycle,
        "discover_package_modules",
        lambda package: (DiscoveredModule("fixture/meta.yaml", source, Path(".")),),
    )
    store_path = tmp_path / "registry.db"
    monkeypatch.setattr(module_lifecycle, "__version__", "0.1.2.1")
    old = ModuleService(Registry.load("system-modules", store=SQLiteRegistryStore(store_path)))
    assert old.setup()
    assert old.resolve("module_set", "heavenbase")["version"] == "0.1.2.1"

    newer = ModuleService(Registry.load("system-modules", store=SQLiteRegistryStore(store_path)))
    monkeypatch.setattr(module_lifecycle, "__version__", "0.1.2.2")
    assert newer.setup()
    assert old.resolve("module_set", "heavenbase")["version"] == "0.1.2.1"

    monkeypatch.setattr(module_lifecycle, "__version__", "0.1.2.1")
    assert old.setup()
    assert old.resolve("module_set", "heavenbase")["version"] == "0.1.2.1"


def test_stale_install_receipt_cannot_remove_a_newer_same_owner_reconcile(tmp_path):
    service = ModuleService(Registry.load("system-modules", store=SQLiteRegistryStore(tmp_path / "registry.db")))
    old = service.install(_manifest(_item("value"), coordinate="example/stable-owner"))
    updated_record = _record()
    updated_record["desc"] = "Updated registration."
    current = service.install(_manifest(_item("value", record=updated_record), coordinate="example/stable-owner"))

    with pytest.raises(ValueError, match="stale"):
        service.uninstall(old)
    assert service.catalog.revision == current.revision
    assert service.inspect("test_kind", "value")[0]["record"]["desc"] == "Updated registration."


def test_manifest_reconcile_removes_only_stale_rows_owned_by_that_installation():
    service = ModuleService(Registry("system-modules"))
    service.install(_manifest(_item("kept"), _item("stale"), coordinate="example/stable-owner"))
    service.install(_manifest(_item("external"), coordinate="example/other-owner"))

    service.install(_manifest(_item("kept"), coordinate="example/stable-owner"))

    assert service.inspect("test_kind", "kept")
    assert service.inspect("test_kind", "external")
    assert service.inspect("test_kind", "stale") == ()


def test_external_path_install_and_cross_process_inspection_do_not_import_code(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    sentinel = tmp_path / "imported.txt"
    (plugin / "external_component.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('imported', encoding='utf-8')\nVALUE = {{'external': True}}\n",
        encoding="utf-8",
    )
    (plugin / "meta.yaml").write_text(
        """manifest_version: 2
coordinate: example/external-fixture
version: 1.0.0
compatibility: {}
items:
  - kind: external_kind
    identifier: portable
    aliases: [moved]
    groups: {bundle: [external-demo]}
    name: External value
    desc: Cross-process path registration.
    source: path
    target: {module: external_component, qualname: VALUE}
    active: true
    meta: {schema_version: 1}
""",
        encoding="utf-8",
    )
    store_path = tmp_path / "registry.db"
    artifact_path = tmp_path / "artifacts"
    service = ModuleService(
        Registry.load("system-modules", store=SQLiteRegistryStore(store_path)),
        artifacts=ModuleArtifactStore(artifact_path),
    )
    receipt = service.install(plugin)

    assert receipt.bundles == ("external-demo",)
    assert not sentinel.exists()
    script = f"""
import json
from heavenbase.modules import ModuleService
from heavenbase.modules.artifacts import ModuleArtifactStore
from heavenbase.utils.registry import Registry, SQLiteRegistryStore
service = ModuleService(
    Registry.load('system-modules', store=SQLiteRegistryStore({str(store_path)!r})),
    artifacts=ModuleArtifactStore({str(artifact_path)!r}),
)
print(json.dumps(service.inspect('external_kind', 'moved')))
"""
    inspection = loads_json(cmd([sys.executable, "-c", script], check=True, include="out"), restore=False)

    assert inspection[0]["identifier"] == "portable"
    assert inspection[0]["record"]["source"] == "artifact"
    assert len(inspection[0]["record"]["target"]["artifact"]) == 64
    assert str(plugin) not in str(inspection)
    assert not sentinel.exists()

    resolution_script = f"""
import json
from heavenbase.modules import ModuleService
from heavenbase.modules.artifacts import ModuleArtifactStore
from heavenbase.utils.registry import Registry, SQLiteRegistryStore
service = ModuleService(
    Registry.load('system-modules', store=SQLiteRegistryStore({str(store_path)!r})),
    artifacts=ModuleArtifactStore({str(artifact_path)!r}),
)
print(json.dumps(service.resolve('external_kind', 'portable')))
"""
    assert loads_json(cmd([sys.executable, "-c", resolution_script], check=True, include="out")) == {"external": True}
    assert sentinel.read_text(encoding="utf-8") == "imported"

    reader = ModuleService(
        Registry.load("system-modules", store=SQLiteRegistryStore(store_path)),
        artifacts=ModuleArtifactStore(artifact_path),
    )
    assert reader.resolve("external_kind", "portable") == {"external": True}

    (plugin / "external_component.py").write_text("VALUE = {'changed': True}\n", encoding="utf-8")
    unchanged_reader = ModuleService(
        Registry.load("system-modules", store=SQLiteRegistryStore(store_path)),
        artifacts=ModuleArtifactStore(artifact_path),
    )
    assert unchanged_reader.resolve("external_kind", "portable") == {"external": True}
    current = unchanged_reader.install(plugin)
    assert current.generation != receipt.generation
    assert unchanged_reader.resolve("external_kind", "portable") == {"changed": True}
    with pytest.raises(ValueError, match="stale"):
        unchanged_reader.uninstall(receipt)
    unchanged_reader.uninstall(current)


def test_module_service_collects_obsolete_artifact_runtime_generations(tmp_path):
    plugin = tmp_path / "runtime_generation"
    plugin.mkdir()
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    component = plugin / "component.py"
    component.write_text("class Value:\n    marker = 'first'\n", encoding="utf-8")
    (plugin / "meta.yaml").write_text(
        """manifest_version: 2
coordinate: example/runtime-generation
version: 1.0.0
compatibility: {}
items:
  - kind: test_kind
    identifier: runtime-generation
    name: Runtime generation
    desc: Artifact generation retirement fixture.
    source: path
    target: {module: component, qualname: Value}
    active: true
    meta: {schema_version: 1}
""",
        encoding="utf-8",
    )
    artifacts = ModuleArtifactStore(tmp_path / "artifacts")
    service = ModuleService(Registry("system-modules"), artifacts=artifacts)

    first_receipt = service.install(plugin)
    first = service.resolve("test_kind", "runtime-generation")
    first_namespace = first.__module__.split(".", 1)[0]
    first_digest = service.inspect("test_kind", "runtime-generation")[0]["record"]["target"]["artifact"]

    component.write_text("class Value:\n    marker = 'second'\n", encoding="utf-8")
    second_receipt = service.install(plugin)
    second = service.resolve("test_kind", "runtime-generation")
    second_namespace = second.__module__.split(".", 1)[0]
    second_digest = service.inspect("test_kind", "runtime-generation")[0]["record"]["target"]["artifact"]

    assert first.marker == "first"
    assert second.marker == "second"
    assert first_namespace != second_namespace
    assert not any(name == first_namespace or name.startswith(f"{first_namespace}.") for name in sys.modules)
    assert second_namespace in sys.modules

    with pytest.raises(ValueError, match="stale"):
        service.uninstall(first_receipt)
    assert second_namespace in sys.modules

    service.uninstall(second_receipt)

    assert not any(name == second_namespace or name.startswith(f"{second_namespace}.") for name in sys.modules)
    assert artifacts.get(first_digest).root.is_dir()
    assert artifacts.get(second_digest).root.is_dir()


def test_module_resolution_does_not_hold_the_service_lock_while_plugin_code_executes(tmp_path):
    plugin = tmp_path / "nested_resolution"
    plugin.mkdir()
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    bridge_name = "_heavenbase_test_nested_resolution"
    (plugin / "dependency.py").write_text("VALUE = 'dependency'\n", encoding="utf-8")
    (plugin / "consumer.py").write_text(
        "import threading\n"
        f"import {bridge_name} as bridge\n"
        "values = []\n"
        "errors = []\n"
        "def resolve_dependency():\n"
        "    try:\n"
        "        values.append(bridge.service.resolve('test_kind', 'dependency'))\n"
        "    except BaseException as error:\n"
        "        errors.append(error)\n"
        "worker = threading.Thread(target=resolve_dependency)\n"
        "worker.start()\n"
        "worker.join(2)\n"
        "if worker.is_alive():\n"
        "    raise RuntimeError('nested module resolution deadlocked')\n"
        "if errors:\n"
        "    raise errors[0]\n"
        "VALUE = f'consumer:{values[0]}'\n",
        encoding="utf-8",
    )
    (plugin / "meta.yaml").write_text(
        """manifest_version: 2
coordinate: example/nested-resolution
version: 1.0.0
compatibility: {}
items:
  - kind: test_kind
    identifier: dependency
    name: Dependency
    desc: Nested resolution dependency.
    source: path
    target: {module: dependency, qualname: VALUE}
    active: true
    meta: {schema_version: 1}
  - kind: test_kind
    identifier: consumer
    name: Consumer
    desc: Nested resolution consumer.
    source: path
    target: {module: consumer, qualname: VALUE}
    active: true
    meta: {schema_version: 1, dependencies: [test_kind:dependency]}
""",
        encoding="utf-8",
    )
    service = ModuleService(Registry("system-modules"), artifacts=ModuleArtifactStore(tmp_path / "artifacts"))
    bridge = types.ModuleType(bridge_name)
    bridge.service = service
    sys.modules[bridge_name] = bridge
    try:
        service.install(plugin)

        assert service.resolve("test_kind", "consumer") == "consumer:dependency"
    finally:
        sys.modules.pop(bridge_name, None)


def test_module_catalog_reads_hold_the_authority_lease_until_complete(monkeypatch):
    service = ModuleService(Registry("system-modules"))
    service.install(_manifest(_item("catalog-lease")))
    entered = threading.Event()
    resume = threading.Event()
    retire_started = threading.Event()
    retired = threading.Event()
    failures = []
    original_capture = service.catalog._capture
    original_need_retirable = service._need_retirable

    def blocking_capture():
        entered.set()
        assert resume.wait(timeout=10)
        return original_capture()

    def inspect():
        try:
            service.catalog.inspect()
        except BaseException as error:
            failures.append(error)

    def mark_retirement():
        original_need_retirable()
        retire_started.set()

    def retire():
        service._retire_authority()
        retired.set()

    monkeypatch.setattr(service.catalog, "_capture", blocking_capture)
    monkeypatch.setattr(service, "_need_retirable", mark_retirement)
    reader = threading.Thread(target=inspect)
    reader.start()
    assert entered.wait(timeout=10)
    closer = threading.Thread(target=retire)
    closer.start()
    assert retire_started.wait(timeout=10)
    with service._authority_condition:
        assert service._active is False
        assert service._authority_users == 1
        assert not retired.is_set()
    resume.set()
    reader.join(timeout=10)
    closer.join(timeout=10)

    assert not reader.is_alive()
    assert not closer.is_alive()
    assert failures == []
    assert retired.is_set()


def test_external_module_folder_can_contribute_and_remove_an_extension(tmp_path):
    plugin = tmp_path / "external_extension"
    plugin.mkdir()
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "entities.py").write_text(
        "from heavenbase import Entity, ShortText, field\n"
        "class ExternalExtensionNote(Entity):\n"
        "    identifier = 'external-extension-note'\n"
        "    name = field(ShortText)\n",
        encoding="utf-8",
    )
    (plugin / "meta.yaml").write_text(
        """manifest_version: 2
coordinate: example/external-extension
version: 1.0.0
compatibility: {}
bundle: external-extension
items:
  - kind: entity
    identifier: external-extension-note
    name: ExternalExtensionNote
    desc: Out-of-tree extension entity.
    source: path
    target: {module: entities, qualname: ExternalExtensionNote}
    active: true
    meta: {schema_version: 1}
  - kind: extension
    identifier: external-extension
    groups: {tag: [external, test]}
    name: External Extension
    desc: Out-of-tree module-folder extension.
    source: inline
    target: definition
    active: true
    meta:
      schema_version: 1
      dependencies: [entity:external-extension-note]
      definition:
        identifier: external-extension
        name: External Extension
        version: test
        desc: Out-of-tree module-folder extension.
        required: false
        tags: [external, test]
        entities: [external-extension-note]
        meta: {}
        setup: null
        api: null
        api_name: null
""",
        encoding="utf-8",
    )
    service = hb.DEFAULT_CONTEXT.modules()
    receipt = service.install(plugin)
    workspace = None
    try:
        row = service.inspect("extension", "external-extension")[0]
        assert row["groups"]["tag"] == ["external", "test"]
        assert row["record"]["source"] == "inline"

        workspace = hb.HeavenBase("external-extension-test", detached=True, backends={"main": {"type": "inmem"}})
        enabled = workspace.enable_extension("external-extension")

        assert enabled.identifier == "external-extension"
        assert "external-extension-note" in workspace.list_entities()
        note = workspace.entities["external-extension-note"]
        assert note.__module__.startswith("_heavenbase_artifact_")
        assert workspace.extensions()["external-extension"]["entities"] == ["external-extension-note"]
        note_id = workspace.upsert(note, {"object_id": "note-1", "name": "Installed outside HeavenBase"})
        assert note_id == "note-1"
        assert workspace.get(note_id, entity=note)["name"] == "Installed outside HeavenBase"
        extension_rows = workspace.query(hb.MetaSchema).where({"kind": "extension"}).execute().rows()
        assert any(row["subject_id"] == "external-extension" for row in extension_rows)
    finally:
        if workspace is not None:
            workspace.drop()
        service.uninstall(receipt)

    with pytest.raises(KeyError, match="No extension registered"):
        hb.ext.Extension.load("external-extension")


def test_metaschema_projects_external_records_without_importing_or_leaking_paths(tmp_path):
    plugin = tmp_path / "projection_plugin"
    plugin.mkdir()
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    module_name = "external_projection_component"
    sentinel = tmp_path / "projection-imported.txt"
    (plugin / f"{module_name}.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('imported', encoding='utf-8')\nVALUE = {{'external': True}}\n",
        encoding="utf-8",
    )
    (plugin / "meta.yaml").write_text(
        f"""manifest_version: 2
coordinate: example/metaschema-projection
version: 1.0.0
compatibility: {{}}
items:
  - kind: external_kind
    identifier: projection
    aliases: [projection-alias]
    groups: {{tag: [fixture]}}
    name: External projection
    desc: Import-free MetaSchema projection fixture.
    source: path
    target: {{module: {module_name}, qualname: VALUE}}
    active: true
    meta:
      schema_version: 1
      tags: {{external: true}}
""",
        encoding="utf-8",
    )
    service = hb.DEFAULT_CONTEXT.modules()
    receipt = service.install(plugin)
    workspace = hb.HeavenBase("metaschema-projection-test", detached=True, backends={"main": {"type": "inmem"}})
    try:
        rows = {row["object_id"]: row for row in workspace.rows(hb.MetaSchema)}
        projected = rows["external_kind:projection"]

        assert module_name not in sys.modules
        assert not sentinel.exists()
        assert projected["source"] == "artifact"
        assert projected["target"] == f"{module_name}:VALUE"
        assert projected["meta"]["aliases"] == ["projection-alias"]
        assert projected["meta"]["groups"] == {"tag": ["fixture"]}
        assert "tags" not in projected["meta"]
        assert "external" not in projected["tags"]
        assert dict(service.tags("external_kind", "projection").raw) == {"external": True}
        assert projected["meta"]["registry_revision"] == service.catalog.revision
        assert len(projected["meta"]["registry_fingerprint"]) == 64
        assert str(plugin) not in str(projected)
        assert all(not str(row.get("object_id", "")).startswith("external_kind:") for row in workspace.rows(hb.Catalog))
    finally:
        service.uninstall(receipt)
        workspace.drop()


def test_context_module_setup_is_explicit_idempotent_and_registry_first(tmp_path, monkeypatch):
    context = hb.Context.load(_bootstrap(tmp_path), config=ConfigManager(default={}, setup=False))
    service = context.modules()

    assert service is context.modules()
    with pytest.raises(ValueError, match="reserved"):
        context.registry("system-modules")
    assert service.inspect() == ()
    assert service.catalog.revision == 0

    assert context._setup_modules()
    assert {row["identifier"] for row in service.inspect("operation")} >= {"eq", "near", "count", "percentile"}
    assert service.resolve("operation", "$eq") == {"kind": "operation"}
    row = service.inspect("operation", "eq")[0]
    assert row["groups"]["provenance"] == ["system"]
    assert row["groups"]["tag"] == ["builtin"]
    assert service.catalog.get(("module_set", "heavenbase")) is not None
    assert not context._setup_modules()
    assert context._setup_modules(force=True)

    child = context.child("child")
    child_service = child.modules()
    assert child_service is service
    assert child_service.catalog is service.catalog
    assert child_service.catalog.revision == service.catalog.revision
    assert child_service.inspect("operation", "near")
    assert not child._setup_modules()

    import heavenbase.modules.discovery as discovery

    monkeypatch.setattr(discovery.resources, "files", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("resolver scanned package folders")))
    assert service.resolve("operation", "near") == {"kind": "operation"}

    child.close()
    context.close()
    assert context._modules is None


def test_builtin_module_discovery_does_not_import_provider_implementations(tmp_path):
    script = f"""
import json
import sys

from heavenbase import Context
from heavenbase.utils.config import ConfigManager

context = Context.load({repr(_bootstrap(tmp_path))}, config=ConfigManager(default={{}}, setup=False))
service = context.modules()
ran = context._setup_modules()
prefixes = ("heavenbase.backends.elasticsearch",)
loaded = sorted(name for name in sys.modules if name.startswith(prefixes))
marker = service.catalog.get(("module_set", "heavenbase")) is not None
context.close()
print(json.dumps({{"ran": ran, "marker": marker, "loaded": loaded}}))
"""

    result = loads_json(cmd([sys.executable, "-c", script], check=True, include="out"))

    assert result == {"ran": True, "marker": True, "loaded": []}


def test_external_logical_type_and_strategy_use_builtin_consumer_contracts(tmp_path):
    plugin = tmp_path / "external_tags"
    plugin.mkdir()
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "external_tags.py").write_text(
        "from heavenbase.strategies import StorageStrategy\n"
        "from heavenbase.types import LogicalType\n"
        "class ExternalLogical(LogicalType):\n"
        "    identifier = 'external-logical'\n"
        "    def validate(self, value):\n"
        "        return f'external:{value}'\n"
        "class UnregisteredLogical(LogicalType):\n"
        "    identifier = 'unregistered-logical'\n"
        "class ExternalStrategy(StorageStrategy):\n"
        "    identifier = 'external-strategy'\n"
        "    option_fields = ('mode',)\n"
        "def external_handler(field_schema, value, ctx):\n"
        "    from heavenbase.execution import QueryFragment\n"
        "    return QueryFragment(ctx.backend, 'external_total', field_schema.name, value, notes={'mode': 'external'})\n"
        "def register_external_handlers(registry, ctx):\n"
        "    from heavenbase.execution.handlers import Handler\n"
        "    Handler(ExternalLogical, 'external_total', 'inmem', 'inline', external_handler).register(registry, replace=True)\n"
        "def external_total(values):\n"
        "    return sum(values)\n",
        encoding="utf-8",
    )
    (plugin / "meta.yaml").write_text(
        "manifest_version: 2\n"
        "coordinate: example/external-tags\n"
        "version: 1.0.0\n"
        "compatibility: {}\n"
        "bundle: external-tags\n"
        "items:\n"
        "  - kind: logical_type\n"
        "    identifier: external-logical\n"
        "    aliases: [ExternalLogical, external-alias]\n"
        "    name: ExternalLogical\n"
        "    desc: Extracted external logical type.\n"
        "    source: path\n"
        "    target: {module: external_tags, qualname: ExternalLogical}\n"
        "    active: true\n"
        "    meta: {schema_version: 1, tags: {external-category: true, external-batch: 7}}\n"
        "  - kind: strategy\n"
        "    identifier: external-strategy\n"
        "    aliases: [ExternalStrategy, external-strategy-alias]\n"
        "    name: ExternalStrategy\n"
        "    desc: Extracted external storage strategy.\n"
        "    source: path\n"
        "    target: {module: external_tags, qualname: ExternalStrategy}\n"
        "    active: true\n"
        "    meta: {schema_version: 1, tags: {external-category: true}}\n"
        "  - kind: storage_profile\n"
        "    identifier: external-logical\n"
        "    name: External logical storage profile\n"
        "    desc: Extracted external storage profile.\n"
        "    source: inline\n"
        "    target: profile\n"
        "    dtype: external-logical\n"
        "    strategy: external-strategy\n"
        "    active: true\n"
        "    meta:\n"
        "      schema_version: 1\n"
        "      dependencies: [logical_type:external-logical, strategy:external-strategy]\n"
        "      profile:\n"
        "        backend_preferences: [{tag: storage-roles, contains: row}]\n"
        "        default_strategy: external-strategy\n"
        "        reason: external-profile\n"
        "        strategy_rules: []\n"
        "  - kind: tag\n"
        "    identifier: external-batch\n"
        "    name: External batch Tag\n"
        "    desc: External integer Tag.\n"
        "    source: inline\n"
        "    target: definition\n"
        "    active: true\n"
        "    meta:\n"
        "      schema_version: 1\n"
        "      definition:\n"
        "        schema: {type: integer}\n"
        "        default: 4\n"
        "        subjects: [logical_type]\n"
        "        subject_policy: exact\n"
        "        layers: [static]\n"
        "  - kind: sql_dialect\n"
        "    identifier: external-sql\n"
        "    name: External SQL dialect\n"
        "    desc: External SQL handler metadata.\n"
        "    source: inline\n"
        "    target: definition\n"
        "    active: true\n"
        "    meta: {schema_version: 1, definition: {supports_regex: true}}\n"
        "  - kind: database_dialect\n"
        "    identifier: external-db\n"
        "    aliases: [external-db-alias]\n"
        "    groups: {sqlglot: [external-sqlglot]}\n"
        "    name: External database dialect\n"
        "    desc: External physical database metadata.\n"
        "    source: inline\n"
        "    target: definition\n"
        "    active: true\n"
        "    meta:\n"
        "      schema_version: 1\n"
        "      tags: {database-json-native: true}\n"
        "      definition: {sqlalchemy_aliases: [external-db-alias], sqlglot_dialect: external-sqlglot, requires_host: false}\n"
        "  - kind: operation\n"
        "    identifier: external_total\n"
        "    aliases: [$external_total]\n"
        "    name: External total\n"
        "    desc: External aggregate operation.\n"
        "    source: inline\n"
        "    target: definition\n"
        "    active: true\n"
        "    meta: {schema_version: 1, tags: {aggregate: true}, definition: {kind: operation}}\n"
        "  - kind: reducer\n"
        "    identifier: external_total\n"
        "    name: External total reducer\n"
        "    desc: External aggregate reducer.\n"
        "    source: path\n"
        "    target: {module: external_tags, qualname: external_total}\n"
        "    active: true\n"
        "    meta:\n"
        "      schema_version: 1\n"
        "      tags: {aggregate: true}\n"
        "      dependencies: [operation:external_total]\n"
        "      reducer: {normalize_options: null, result_type: integer}\n"
        "  - kind: handler\n"
        "    identifier: external-handler\n"
        "    groups: {backend: [inmem]}\n"
        "    name: External handler contribution\n"
        "    desc: Extracted external Handler compiler.\n"
        "    source: path\n"
        "    target: {module: external_tags, qualname: register_external_handlers}\n"
        "    active: true\n"
        "    meta:\n"
        "      schema_version: 1\n"
        "      dependencies: [backend_type:inmem, logical_type:external-logical, operation:external_total]\n"
        "      definition: {reasons: null}\n",
        encoding="utf-8",
    )
    service = hb.DEFAULT_CONTEXT.modules()
    receipt = service.install(plugin)

    external_type = hb.LogicalType.load("external-alias")
    external_strategy = hb.StorageStrategy.load("external-strategy-alias")
    assert "external_tags" not in sys.modules
    imported = sys.modules[external_type.__module__]
    from heavenbase.query.aggregate import reducer_for
    from heavenbase.execution.handlers.seed import seed_handlers
    from heavenbase.schema import FieldSchema

    assert external_type().validate("value") == "external:value"
    external_type_tags = service.tags("logical_type", "external-logical")
    assert external_type_tags["external-category"].status == "unresolved"
    assert external_type_tags["external-category"].value is True
    assert external_type_tags["external-batch"].value == 7
    assert "external-alias" in hb.ext.logical_type_aliases(external_type)
    assert hb.StorageStrategy.value_of({"type": "external-strategy-alias", "mode": "strict"}).options() == {"mode": "strict"}
    assert service.tag("strategy", "external-strategy", "external-category").status == "unresolved"
    assert "external-strategy-alias" in hb.ext.strategy_aliases(external_strategy)
    assert hb.ext.registered_storage_profiles()["external-logical"].to_dict() == {
        "dtype": "external-logical",
        "backend_preferences": [{"tag": "storage-roles", "contains": "row"}],
        "default_strategy": "external-strategy",
        "reason": "external-profile",
        "strategy_rules": [],
    }
    assert service.tag_definition("external-batch").default == 4
    assert hb.ext.sql_dialect_spec("external-sql").supports_regex is True
    assert hb.ext.database_dialects.get("external-db-alias").requires_host is False
    assert hb.ext.database_dialects.get("external-db-alias").supports("database-json-native")
    assert hb.ext.database_dialects.from_sqlglot("external-sqlglot") == "external-db"
    assert hb.ext.normalize_op("$external_total") == "external_total"
    assert reducer_for("external_total").reduce([1, 2, 3], {}) == 6
    external_handler = seed_handlers().get(external_type, "external_total", "inmem", "inline")
    fragment = external_handler(FieldSchema("value", external_type()), "needle", hb.ext.HandlerContext("main", "inmem", "inline"))
    assert fragment.notes["mode"] == "external"
    assert fragment.payload == "needle"
    with pytest.raises(KeyError, match="unregistered-logical"):
        hb.LogicalType.load(imported.UnregisteredLogical)

    service.uninstall(receipt)
    with pytest.raises(KeyError, match="external-logical"):
        hb.LogicalType.load(external_type)
    with pytest.raises(KeyError, match="external-strategy"):
        hb.StorageStrategy.load(external_strategy)
    assert service.tag_definition("external-batch") is None
    with pytest.raises(LookupError, match="external-sql"):
        hb.ext.sql_dialect_spec("external-sql")
    with pytest.raises(LookupError, match="external-db"):
        hb.ext.database_dialects.get("external-db")
    with pytest.raises((KeyError, LookupError)):
        reducer_for("external_total")
    with pytest.raises((KeyError, LookupError)):
        seed_handlers().get("external-logical", "external_total", "inmem", "inline")


def test_module_service_exports_are_scoped_to_modules_facade():
    import heavenbase.modules as modules_api

    assert modules_api.ModuleService is ModuleService
    assert modules_api.InstallReceipt is InstallReceipt
    assert "ModuleService" not in hb.__all__
    assert "InstallReceipt" not in hb.__all__
