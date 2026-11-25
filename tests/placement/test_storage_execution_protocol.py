"""End-to-end proof for placement extension protocols."""

import heavenbase as hb
from heavenbase.backends.strategy import StrategyAdapter
from heavenbase.execution.frame import ResultFrame
from heavenbase.placement import StorageProfile, StorageResolver


class ExtensionCode(hb.LogicalType):
    """Normalize the custom values used by the extension proof."""

    identifier = "extension-code"

    def validate(self, value):
        """Normalize one extension code.

        Args:
            value: Caller-authored code.

        Returns:
            str: Canonical lowercase code.
        """
        return str(value).strip().lower()


class ExtensionIndex(hb.StorageStrategy):
    """Describe the custom index without owning backend state."""

    identifier = "extension-index"
    option_fields = ("mode",)


class ExtensionRecord(hb.Entity):
    """Provide a schema for the cumulative extension proof."""

    identifier = "extension-record"

    code = hb.field(ExtensionCode)


class ExtensionAdapter(StrategyAdapter):
    """Implement the custom index inside one backend instance."""

    identifier = "extension-index-adapter"
    strategy = ExtensionIndex.identifier

    def __init__(self, backend):
        super().__init__(backend)
        self.bindings = []
        self.rows = []

    def ensure(self, schema, binding):
        """Record one prepared binding.

        Args:
            schema: Entity schema being prepared.
            binding: Resolved field storage binding.

        Returns:
            None: The binding is recorded in place.
        """
        self.bindings.append((schema.entity_id, binding))

    def upsert(self, rows):
        """Record backend-ready replacement mutations.

        Args:
            rows: Backend-ready row operations.

        Returns:
            None: Mutations are recorded in place.
        """
        self.rows.extend(rows)

    def can_execute(self, fragment):
        """Return whether this adapter owns a fragment.

        Args:
            fragment: Compiled query fragment.

        Returns:
            bool: True for fragments tagged with this strategy.
        """
        return fragment.notes.get("strategy") == self.strategy

    def execute(self, fragment):
        """Return rows maintained by this adapter.

        Args:
            fragment: Adapter-owned query fragment.

        Returns:
            ResultFrame: Identity-bearing rows matching the payload.
        """
        rows = [row.to_row() for row in self.rows if row.values.get(fragment.field) == fragment.payload]
        return ResultFrame.from_rows(rows)


class StorageExtensionAPI:
    """Expose the extension-owned adapter for the integration proof."""

    def __init__(self, workspace):
        self.workspace = workspace

    def adapter(self):
        """Return the backend-local extension adapter."""
        return self.workspace.backends.get("main").strategy_adapter(ExtensionIndex)


def _setup_storage_extension(workspace):
    """Install the storage extension's backend-local adapter and Entity."""
    backend = workspace.backends.get("main")
    ExtensionAdapter(backend).register(replace=True)
    return (ExtensionRecord,)


def _infer_extension_code(source, values, hint, report):
    """Infer the extension-owned logical type for a `code` column."""
    del values, hint, report
    return ExtensionCode() if source == "code" else None


def _setup_interop_storage(workspace):
    """Install the interop proof's backend-local adapter."""
    ExtensionAdapter(workspace.backends.get("main")).register(replace=True)


def test_custom_storage_components_compose_without_planner_changes():
    """Prove the complete custom storage extension path."""
    ExtensionCode.register(tags={"filter": True, "filter-domains": ["text"]})
    ExtensionIndex.register()
    StorageProfile(
        ExtensionCode,
        ({"tag": "storage-roles", "contains": "row"},),
        ExtensionIndex(mode="exact"),
        "extension-profile",
    ).register(replace=True)

    backend = hb.backends.InMemBackend("main")
    adapter = ExtensionAdapter(backend).register()
    schema = ExtensionRecord.schema()
    plan = StorageResolver().resolve(schema, [backend.name], {})
    binding = plan.first("code")

    assert binding is not None
    assert binding.strategy == ExtensionIndex.identifier
    assert binding.source == "extension-profile"
    assert backend.supports_strategy(schema, binding)

    adapter.ensure(schema, binding)
    adapter.upsert([hb.ext.RowOp(schema.entity_id, "row-1", {"code": "alpha"})])
    fragment = hb.ext.QueryFragment("main", "eq", "code", "alpha", notes={"strategy": ExtensionIndex.identifier}).bind(schema.entity_id)

    assert backend.strategy_adapter_for_fragment(fragment) is adapter
    assert adapter.execute(fragment).ids == ["row-1"]
    assert adapter.bindings == [(schema.entity_id, binding)]


def test_extension_composes_storage_protocols_behind_one_enablement():
    """Prove an extension can install one complete storage capability."""
    ExtensionCode.register(tags={"filter": True, "filter-domains": ["text"]})
    ExtensionIndex.register()
    StorageProfile(
        ExtensionCode,
        ({"tag": "storage-roles", "contains": "row"},),
        ExtensionIndex(mode="exact"),
        "extension-bundle-profile",
    ).register(replace=True)

    extension = hb.ext.Extension(
        "storage-execution-proof",
        desc="Install one custom logical type storage capability.",
        setup=_setup_storage_extension,
        api=StorageExtensionAPI,
    ).register()
    workspace = hb.HeavenBase("storage-extension-proof", detached=True, backends={"main": {"type": "inmem"}})

    assert workspace.enable_extension(extension.identifier) is extension
    assert ExtensionRecord.identifier in workspace.entities
    adapter = workspace.storage_execution_proof.adapter()
    binding = workspace.storage_plans[ExtensionRecord.identifier].first("code")
    assert isinstance(adapter, ExtensionAdapter)
    assert adapter.supports(ExtensionRecord.schema(), binding)

    workspace.upsert(ExtensionRecord, {"object_id": "stored-row", "code": " Alpha "})
    assert workspace.get("stored-row", entity=ExtensionRecord)["code"] == "alpha"
    workspace.set({"object_id": "stored-row", "code": "beta"}, entity=ExtensionRecord)
    assert workspace.get("stored-row", entity=ExtensionRecord)["code"] == "beta"
    assert workspace.delete((ExtensionRecord, "stored-row")) is True
    assert workspace.get("stored-row", entity=ExtensionRecord) is None

    adapter.upsert([hb.ext.RowOp(ExtensionRecord.identifier, "row-2", {"code": "beta"})])
    fragment = hb.ext.QueryFragment("main", "eq", "code", "beta", notes=binding.route_notes()).bind(ExtensionRecord.identifier)

    assert workspace.backends.get("main").strategy_adapter_for_fragment(fragment) is adapter
    assert adapter.execute(fragment).ids == ["row-2"]

    manifest = hb.WorkspaceManifest.from_workspace(workspace)
    clone = manifest.open(detached=True)
    clone_adapter = clone.storage_execution_proof.adapter()
    clone_binding = clone.storage_plans[ExtensionRecord.identifier].first("code")

    assert manifest.to_dict()["construction"] == {
        "kind": "backends",
        "config": {"main": {"type": "inmem"}},
    }
    assert manifest.extensions == ["toolkit", "prompt", "agent", extension.identifier]
    assert ExtensionRecord.identifier in clone.entities
    assert isinstance(clone_adapter, ExtensionAdapter)
    assert clone_adapter.supports(ExtensionRecord.schema(), clone_binding)


def test_interop_inference_enters_the_extension_write_protocol():
    """Prove external rows lower into the same entity and write contracts."""
    ExtensionCode.register(tags={"filter": True, "filter-domains": ["text"]})
    ExtensionIndex.register()
    StorageProfile(
        ExtensionCode,
        ({"tag": "storage-roles", "contains": "row"},),
        ExtensionIndex(mode="exact"),
        "interop-extension-profile",
    ).register(replace=True)

    hb.register_interop_type_inferer(
        hb.InteropTypeInferer(_infer_extension_code, identifier="storage-extension-code-inferer"),
        first=True,
        replace=True,
    )
    staging = hb.from_sql_result(
        [{"external_id": "row-3", "code": " Gamma "}],
        entity_id="interop-extension-record",
        id="external_id",
    )

    extension = hb.ext.Extension(
        "interop-storage-proof",
        entities=(staging.entity,),
        setup=_setup_interop_storage,
    ).register()
    workspace = hb.HeavenBase("interop-storage-proof", detached=True, backends={"main": {"type": "inmem"}})
    workspace.enable_extension(extension.identifier)

    assert staging.entity.schema().get("code").dtype.identifier == ExtensionCode.identifier
    assert staging.upsert(workspace) == ["row-3"]
    assert workspace.get("row-3", entity=staging.entity)["code"] == "gamma"
    binding = workspace.storage_plans[staging.entity.identifier].first("code")
    assert binding.strategy == ExtensionIndex.identifier
    assert workspace.backends.get("main").supports_strategy(staging.entity.schema(), binding)
