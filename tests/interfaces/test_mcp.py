import os
from pathlib import Path
import sys

from heavenbase.utils import cmd
from heavenbase.utils.files import pj
from heavenbase.utils.serialize import dumps_jstr, loads_json, loads_jstr, save_txt

import pytest

import heavenbase as hb
import heavenbase.ext as hb_ext
from heavenbase.entity.registry import load_entity_artifact_export
from heavenbase.workspace.setup.lifecycle import _default_workspace_name


def _toolkit_export(module, attribute):
    return load_entity_artifact_export("sys-toolkit", module, attribute)


def _refresh_toolkit_exports():
    global ProfileSpec
    global TOOL_GROUPS
    global entity_allowed
    global profile_spec_from_mapping
    global register_profile
    global registered_profiles
    global registered_profile_tool_names
    global resolve_profile
    global WorkspaceToolkitFamilySpec
    global register_workspace_toolkit_family
    global ToolSerializer

    ProfileSpec = _toolkit_export("profile", "ProfileSpec")
    TOOL_GROUPS = _toolkit_export("profile_tools", "TOOL_GROUPS")
    entity_allowed = _toolkit_export("profiles", "entity_allowed")
    profile_spec_from_mapping = _toolkit_export("profiles", "profile_spec_from_mapping")
    register_profile = _toolkit_export("profiles", "register_profile")
    registered_profiles = _toolkit_export("profiles", "registered_profiles")
    registered_profile_tool_names = _toolkit_export("profile_tools", "registered_profile_tool_names")
    resolve_profile = _toolkit_export("profiles", "resolve_profile")
    WorkspaceToolkitFamilySpec = _toolkit_export("families.family", "WorkspaceToolkitFamilySpec")
    register_workspace_toolkit_family = _toolkit_export("families.registry", "register_workspace_toolkit_family")
    ToolSerializer = _toolkit_export("serializers", "ToolSerializer")


WORKSPACES = hb.DEFAULT_CONTEXT._workspace_registry


def _empty_family_builder(_workspace):
    """Return an empty test toolkit family."""
    return {}


@pytest.fixture(autouse=True)
def _isolate_workspaces():
    _refresh_toolkit_exports()
    before = set(WORKSPACES.keys())
    yield
    for wid in set(WORKSPACES.keys()) - before:
        WORKSPACES.pop(wid, None)


def _product_def(entity_id="mcp-product", with_stock=False):
    fields = {
        "object_id": {"type": "identifier", "pk": True, "desc": "Stable product object id"},
        "name": {"type": "short-text", "desc": "Product name"},
        "body": {"type": "long-text", "desc": "Searchable product description"},
        "price": {"type": "float", "desc": "Current price"},
    }
    if with_stock:
        fields["stock"] = {"type": "integer", "desc": "Units available", "default": 0}
    return {"entity_id": entity_id, "name": "McpProduct", "desc": "Commerce product used by MCP tests", "fields": fields}


def test_single_backend_toolkit_inspects_backend_without_workspace_tools():
    """A backend-bound MCP toolkit should expose backend tools, not workspace CRUD tools."""
    workspace = hb.HeavenBase("mcp-backend-test", backends={"main": {"type": "inmem"}})
    toolkit = workspace.to_mcp(backend="main", name="mcp-backend-test")

    assert toolkit.list_tools() == ["describe_backend", "backend_tags"]
    assert "define_entity" not in toolkit.tools
    assert "query" not in toolkit.tools
    assert "mcp-backend-test" in workspace.to_mcp_json(backend="main", name="mcp-backend-test")

    described = toolkit.run("describe_backend")
    assert described["backend"] == {"name": "main", "type": "inmem"}
    row_lookup = described["tags"]["row-lookup"]
    assert row_lookup["value"]["count"] == "native"
    assert row_lookup["status"] == "resolved"
    assert row_lookup["source"] == "record"
    assert row_lookup["subject"] == ["backend_type", "inmem"]
    assert toolkit.run("backend_tags") == described["tags"]
    for tool_name in toolkit.list_tools():
        tool = toolkit.tools[tool_name]
        assert tool.metadata["runtime_only"] is True
        assert tool.metadata["read_only"] is True
        assert tool.annotations == {"readOnlyHint": True}
        assert tool.capsule.manifest.capabilities["registry_persistence"]["ok"] is False


@pytest.mark.fast
def test_workspace_skill_is_generic_valid_and_credential_free(tmp_path, monkeypatch):
    """Any workspace profile can become one atomic installable MCP skill."""
    workspace = hb.HeavenBase("skill-export-test", backends={"main": {"type": "inmem", "name": "main"}})
    skill = workspace.to_skill(profile="full", name="skill-export", port=17011)

    assert isinstance(skill, hb.WorkspaceSkill)
    assert skill.to_dict()["workspace"] == "skill-export-test"
    assert {"list_entities", "describe_entity", "query", "upsert", "set"} <= set(skill.tools)
    target = Path(skill.install(root=tmp_path))
    markdown = (target / "SKILL.md").read_text(encoding="utf-8")
    metadata = (target / "agents" / "openai.yaml").read_text(encoding="utf-8")
    client = target / "scripts" / "client.py"

    assert markdown.startswith("---\nname: skill-export\ndescription:")
    assert "hb mcp workspace skill-export-test --profile full" in markdown
    assert "python scripts/client.py call list_entities '{}'" in markdown
    assert 'transport: "streamable_http"' in metadata
    assert 'url: "http://127.0.0.1:17011/mcp"' in metadata
    assert "$skill-export" in metadata
    assert "password" not in (markdown + metadata + client.read_text(encoding="utf-8")).lower()
    assert os.name == "nt" or client.stat().st_mode & 0o111  # Windows has no POSIX exec bits

    with pytest.raises(FileExistsError):
        skill.install(root=tmp_path)
    (target / "stale.txt").write_text("stale", encoding="utf-8")
    assert skill.install(root=tmp_path, replace=True) == str(target)
    assert not (target / "stale.txt").exists()

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    assert hb.WorkspaceSkill.default_install_root() == str((tmp_path / "codex-home" / "skills").resolve())
    with pytest.raises(ValueError, match="credentials"):
        workspace.to_skill(profile="full", url="https://user:secret@example.test/mcp")


@pytest.mark.fast
def test_backend_toolkit_reports_effective_installed_tag_evidence():
    """Backend MCP metadata should preserve effective Tag provenance and snapshots."""
    workspace = hb.HeavenBase("mcp-backend-tag-authority", backends={"main": {"type": "inmem"}})
    backend = workspace.backends.get("main")
    record = backend.tag("row-lookup")
    live = backend.tag("strategy-adapters")

    described = backend.to_mcp().run("describe_backend")
    row_lookup = described["tags"]["row-lookup"]
    strategy_adapters = described["tags"]["strategy-adapters"]

    assert row_lookup["value"] == record.value
    assert row_lookup["raw"] == record.raw
    assert row_lookup["status"] == record.status == "resolved"
    assert row_lookup["source"] == record.source == "record"
    assert row_lookup["subject"] == list(record.subject) == ["backend_type", "inmem"]
    assert row_lookup["snapshot"] == record.snapshot
    assert strategy_adapters["value"] == []
    assert strategy_adapters["status"] == live.status == "resolved"
    assert strategy_adapters["source"] == live.source == "live"
    assert strategy_adapters["snapshot"] == live.snapshot


def test_backend_toolkit_does_not_register_workspace():
    """Backend-bound MCP toolkits should not create temporary workspaces."""
    workspace = hb.HeavenBase("mcp-hidden-source-test", backends={"main": {"type": "inmem"}})
    names = set(WORKSPACES)
    default_name = _default_workspace_name()

    toolkit = workspace.backends.get("main").to_mcp(name="mcp-hidden-test")

    assert set(WORKSPACES) == names
    assert _default_workspace_name() == default_name
    assert toolkit.run("describe_backend")["backend"]["name"] == "main"


def test_backend_mcp_methods_export_backend_tool_surface():
    """Backend MCP convenience methods expose backend inspection tools."""
    toolkit = hb.backends.InMemBackend("mcp-backend-lazy-import").to_mcp(name="mcp-backend-lazy-import")
    assert toolkit.name == "mcp-backend-lazy-import"
    assert toolkit.list_tools() == ["describe_backend", "backend_tags"]


def test_backend_mcp_accepts_custom_profile_objects_and_mappings():
    """Backend MCP callers should be able to pass ad-hoc profiles without subclassing a backend."""
    backend = hb.backends.InMemBackend("mcp-custom-profile-backend")
    profile = hb.ext.BackendMCPProfile("tags-only", ("backend_tags",), desc="Tags only.")

    toolkit = backend.to_mcp(profile=profile)
    mapped = backend.to_mcp(profile={"name": "describe-only", "tools": "describe_backend", "description": "Describe only."})

    assert hb.ext.BackendMCPProfile is hb.backends.BackendMCPProfile
    assert toolkit.list_tools() == ["backend_tags"]
    assert mapped.list_tools() == ["describe_backend"]
    assert "Tags only." in toolkit.description
    assert "Describe only." in mapped.description


def test_backend_mcp_full_profile_tracks_provider_tools_without_manual_profile_update():
    """The reserved full profile should include provider tools added after ``super().mcp()``."""

    class CustomMCPBackend(hb.backends.InMemBackend):
        def mcp(self) -> hb.ext.BackendMCP:
            spec = super().mcp()
            spec.tool("provider_ping", lambda: {"ok": True}, desc="Return a provider-specific ping payload.")
            return spec

    backend = CustomMCPBackend("mcp-dynamic-full-backend")

    toolkit = backend.to_mcp(profile="full")
    described = backend.to_mcp().run("describe_backend")

    assert toolkit.list_tools() == ["describe_backend", "backend_tags", "provider_ping"]
    assert toolkit.run("provider_ping") == {"ok": True}
    assert described["profiles"]["full"]["tools"] == ["describe_backend", "backend_tags", "provider_ping"]


def test_backend_mcp_methods_load_after_clean_module_setup(tmp_path, home_env):
    """Backend MCP convenience methods should remain lazy after explicit setup."""
    script = """
import sys

import heavenbase as hb

hb.DEFAULT_CONTEXT.modules().setup()
before = any(name == "heavenbase.toolkit" or name.startswith("heavenbase.toolkit.") for name in sys.modules)
toolkit = hb.backends.InMemBackend("clean-backend-mcp").to_mcp(name="clean-backend-mcp")
after = any(name == "heavenbase.toolkit" or name.startswith("heavenbase.toolkit.") for name in sys.modules)
artifact = toolkit.__class__.__module__.partition(".")[0].startswith("_heavenbase_artifact_")
print(hb.dumps_json({"after": after, "artifact": artifact, "before": before, "name": toolkit.name}, sort_keys=True))
"""
    out = cmd(
        [sys.executable, "-c", script],
        check=True,
        include="out",
        env=home_env(tmp_path),
    )
    assert loads_json(out) == {"after": False, "artifact": True, "before": False, "name": "clean-backend-mcp"}


def test_toolkit_redacts_tool_errors(monkeypatch):
    """MCP tool errors should redact secret-like exception text."""
    workspace = hb.HeavenBase("mcp-error-test", backends={"main": {"type": "inmem"}})
    toolkit = workspace.to_mcp(name="mcp-error-test")

    def fail_get(entity_id, entity=None):
        raise RuntimeError("token=abc123 password:open-sesame postgresql://alice:secret@localhost/db")

    monkeypatch.setattr(workspace, "get", fail_get)

    with pytest.raises(RuntimeError) as err:
        toolkit.run("get", object_id="missing")

    text = str(err.value)
    assert "token=<redacted>" in text
    assert "password:<redacted>" in text
    assert ":<redacted>@" in text
    assert "abc123" not in text
    assert "open-sesame" not in text
    assert ":secret@" not in text


def test_workspace_toolkit_discovers_context_from_catalog_and_metaschema():
    """Workspace MCP tools should discover structures and concrete objects."""
    workspace = hb.HeavenBase("mcp-workspace-test", backends={"main": {"type": "inmem"}})
    toolkit = workspace.to_mcp(name="mcp-workspace-test")

    assert "sys-metaschema" in toolkit.description
    assert "sys-catalog" in toolkit.description

    toolkit.run("define_entity", entity=_product_def("commerce-product"))
    toolkit.run("upsert", entity="commerce-product", row={"object_id": "p1", "name": "Desk", "body": "oak office desk", "price": 80})
    toolkit.run("upsert", entity="commerce-product", row={"object_id": "p2", "name": "Speaker", "body": "portable audio", "price": 45})

    meta = toolkit.run("query", entity="sys-metaschema", spec={"filter": {"subject_id": "commerce-product"}, "select": ["kind", "field", "dtype", "desc"]})
    fields = {row["field"]: row for row in meta["rows"] if row["kind"] == "field"}
    catalog = toolkit.run(
        "query",
        entity="sys-catalog",
        spec={"filter": {"target_entity": "commerce-product"}, "select": ["target_id", "target_entity", "name", "desc"]},
    )
    catalog_rows = {row["target_id"]: row for row in catalog["rows"]}
    hydrated = toolkit.run("get", object_id=catalog_rows["p2"]["target_id"], entity=catalog_rows["p2"]["target_entity"])
    cheap_audio = toolkit.run("query", entity="commerce-product", spec={"filter": {"price": {"$lt": 50}}, "select": ["name", "price"]})

    toolkit.run(
        "define_entity",
        entity={
            "entity_id": "support-note",
            "name": "SupportNote",
            "desc": "Agent-created support note",
            "fields": {"object_id": {"type": "identifier", "pk": True}, "summary": {"type": "long-text"}},
        },
    )
    toolkit.run("upsert", entity="support-note", row={"object_id": "n1", "summary": "Customer asked for audio setup help"})
    note_frame = toolkit.run("query", entity="support-note", spec={"filter": {"summary": {"$match": "audio"}}})

    assert fields["price"]["dtype"] == "float"
    assert catalog_rows["p2"]["name"] == "Speaker"
    assert hydrated["row"]["body"] == "portable audio"
    assert cheap_audio["ids"] == ["p2"]
    assert note_frame["ids"] == ["n1"]


@pytest.mark.parametrize("profile", ["full", "agent", "database"])
def test_every_profile_exposing_query_also_exposes_code_execution(profile):
    """query forwards a caller's spec unchanged, so exposing it exposes code_reduce."""

    class ProfileDoc(hb.Entity):
        identifier = "mcp-profile-doc"

        amount = hb.field(hb.Integer)

    workspace = hb.HeavenBase(f"mcp-profile-{profile}", detached=True, backends={"main": {"type": "inmem"}})
    if profile == "database":
        # this profile scopes entities to the extension's own, so use one of them
        workspace.enable_extension("database")
        entity, rows = "db-snippet", [{"object_id": f"s{i}", "title": f"snippet {i}"} for i in range(5)]
        field, expected = "title", 5
    else:
        workspace.register(ProfileDoc)
        entity, rows = "mcp-profile-doc", [{"object_id": f"d{i}", "amount": i} for i in range(5)]
        field, expected = "amount", 5
    workspace.upsert_many(entity, rows)
    toolkit = workspace.to_mcp(name=f"mcp-profile-{profile}", profile=profile)
    assert "query" in toolkit.list_tools()

    spec = {"aggregate": {"n": {"code_reduce": {"field": field, "code": "lambda v: len(v)"}}}}
    assert toolkit.run("query", entity=entity, spec=spec)["rows"] == [{"n": expected}]

    # and the refusal reaches every one of them, while registered reducers do not care
    previous = hb.CM_HVNB.get("heavenbase.query.allow_code_reduce", default=True)
    hb.CM_HVNB.set("heavenbase.query.allow_code_reduce", False)
    try:
        with pytest.raises(RuntimeError, match="allow_code_reduce"):
            toolkit.run("query", entity=entity, spec=spec)
        named = {"aggregate": {"n": {"count": {"field": field}}}}
        assert toolkit.run("query", entity=entity, spec=named)["rows"] == [{"n": expected}]
    finally:
        hb.CM_HVNB.set("heavenbase.query.allow_code_reduce", previous)


def test_query_tool_marks_a_near_bounded_result():
    """A near clause bounds the pipeline, so its counts must not read as whole-entity counts."""

    class NearDoc(hb.Entity):
        identifier = "mcp-near-doc"

        title = hb.field(hb.ShortText)
        emb = hb.field(hb.Vector[2])

    workspace = hb.HeavenBase("mcp-near-test", backends={"main": {"type": "inmem"}})
    workspace.register(NearDoc)
    workspace.upsert_many(NearDoc, [{"object_id": f"d{i}", "title": f"doc {i}", "emb": [i / 10, 1 - i / 10]} for i in range(5)])
    toolkit = workspace.to_mcp(name="mcp-near-test")

    near = toolkit.run("query", entity="mcp-near-doc", spec={"near": {"field": "emb", "query": [1, 0], "top_k": 2}})
    # top_k fed the pipeline 2 of 5 rows, and nothing downstream can tell that
    # from a complete result -- the frame is not a slice of anything.
    assert near["n"] == 2
    assert near.get("truncated") is None
    assert near["near_bounded"] is True

    whole = toolkit.run("query", entity="mcp-near-doc", spec={})
    assert whole["n"] == 5
    assert "near_bounded" not in whole


def test_workspace_toolkit_mirrors_mcp_registry_rows_to_metaschema():
    """Workspace construction should publish queryable MCP registry metadata rows."""
    workspace = hb.HeavenBase("mcp-registry-metadata-test", backends={"main": {"type": "inmem"}})

    rows = {row["object_id"]: row for row in workspace.rows(hb.MetaSchema)}
    assert "mcp_profile:agent" in rows
    assert "toolkit_family:workspace" in rows
    assert "mcp_serializer:jstr" in rows

    workspace.to_mcp(name="mcp-registry-metadata-test", profile="agent")
    workspace.to_mcp(name="mcp-registry-metadata-test-again", profile="agent")

    rows = {row["object_id"]: row for row in workspace.rows(hb.MetaSchema)}

    profile = rows["mcp_profile:agent"]
    assert profile["kind"] == "mcp_profile"
    assert profile["type"] == "markdown"
    assert profile["source"] == "inline"
    assert profile["meta"]["tools"] == [
        "define_entity",
        "list_entities",
        "describe_entity",
        "upsert",
        "get",
        "set",
        "count",
        "query",
        "explain",
    ]
    assert profile["meta"]["record_kind"] == "mcp_profile"
    assert profile["meta"]["registry_revision"] > 0
    assert len(profile["meta"]["registry_fingerprint"]) == 64

    workspace_family = rows["toolkit_family:workspace"]
    assert workspace_family["kind"] == "toolkit_family"
    assert "define_entity" in workspace_family["meta"]["tool_names"]
    assert "list_skills" not in workspace_family["meta"]["tool_names"]
    assert workspace_family["meta"]["builder"].endswith("workspace_core_tool_functions")

    assert rows["toolkit_family:agent"]["meta"]["tool_names"] == ["list_skills", "read_skill"]
    assert rows["toolkit_family:agent"]["meta"]["builder"].endswith("agent_tool_functions")
    assert rows["toolkit_family:agent"]["meta"]["meta"]["builder_context"] == "workspace_tool_context"
    assert rows["toolkit_family:memory"]["meta"]["meta"] == {"tool_layout": "one_tool_per_file"}
    assert rows["toolkit_family:memstate"]["meta"]["meta"] == {"tool_layout": "one_tool_per_file"}
    assert rows["toolkit_family:database"]["meta"]["tool_names"] == [
        "list_sources",
        "ingest_database",
        "run_sql",
        "run_snippet",
        "save_snippet",
        "suggest_sql",
    ]
    assert rows["toolkit_family:database"]["meta"]["meta"] == {"tool_layout": "one_tool_per_file"}
    assert rows["mcp_serializer:jstr"]["target"].endswith("jstr_serializer")
    assert rows["mcp_serializer:markdown"]["meta"]["record_kind"] == "serializer"

    found = workspace.search("mcp profile agent").rows()
    assert any(row["object_id"] == "mcp_profile:agent" for row in found)


def test_workspace_mcp_registry_metadata_rows_are_cached_and_invalidated(monkeypatch):
    """Repeated MCP builds should reuse MetaSchema rows until workspace metadata changes."""
    workspace = hb.HeavenBase("mcp-registry-cache-test", backends={"main": {"type": "inmem"}})
    entity_id = hb.MetaSchema.schema().entity_id
    backend = workspace.backends.get(workspace._metaschema.system_backend_name(hb.MetaSchema))
    original_scan = backend.scan
    scan_calls = []

    def counted_scan(requested_entity_id):
        if requested_entity_id == entity_id:
            scan_calls.append(requested_entity_id)
        return original_scan(requested_entity_id)

    monkeypatch.setattr(backend, "scan", counted_scan)

    workspace.to_mcp(name="mcp-registry-cache-test", profile="agent")
    workspace.to_mcp(name="mcp-registry-cache-test-again", profile="agent")

    assert scan_calls == [entity_id]

    profile_name = "workspace-cache-profile"
    workspace.set(
        {
            "object_id": f"mcp_profile:{profile_name}",
            "kind": "mcp_profile",
            "subject_id": profile_name,
            "name": profile_name,
            "desc": "Workspace-owned cached profile",
            "type": "jstr",
            "target": "jstr",
            "active": True,
            "ws": workspace.id,
            "meta": {
                "name": profile_name,
                "tools": ["count"],
                "serializer": "jstr",
                "desc": "Workspace-owned cached profile",
            },
        },
        entity=hb.MetaSchema,
    )

    toolkit = workspace.to_mcp(name="mcp-registry-cache-custom-test", profile=profile_name)
    workspace.to_mcp(name="mcp-registry-cache-custom-test-again", profile=profile_name)

    assert toolkit.list_tools() == ["count"]
    assert scan_calls == [entity_id, entity_id]


def test_workspace_toolkit_loads_profile_from_metaschema_row_without_global_registration():
    """Workspace-owned MCP profile rows should resolve without mutating process registries."""
    workspace = hb.HeavenBase("mcp-profile-row-test", backends={"main": {"type": "inmem"}})
    workspace.register(_product_def("workspace-profile-product"))
    workspace.upsert(
        "workspace-profile-product",
        {"object_id": "p1", "name": "Desk", "body": "oak desk", "price": 80},
    )
    profile_name = "workspace-row-read-profile"
    workspace.set(
        {
            "object_id": f"mcp_profile:{profile_name}",
            "kind": "mcp_profile",
            "subject_id": profile_name,
            "name": profile_name,
            "desc": "Workspace-owned read profile",
            "type": "markdown",
            "target": "markdown",
            "active": True,
            "ws": workspace.id,
            "meta": {
                "name": profile_name,
                "tools": ["get", "count", "query"],
                "entities": ["workspace-profile-product"],
                "serializer": "markdown",
                "desc": "Workspace-owned read profile",
            },
        },
        entity=hb.MetaSchema,
    )

    toolkit = workspace.to_mcp(name="mcp-profile-row-test", profile=profile_name)

    assert profile_name not in registered_profiles()
    assert toolkit.list_tools() == ["get", "count", "query", "explain"]
    assert toolkit.run("count", entity="workspace-profile-product") == {"entity": "workspace-profile-product", "n": 1}
    assert "| name | price |" in toolkit.run_to_str(
        "query",
        entity="workspace-profile-product",
        spec={"select": ["name", "price"]},
    )

    row = workspace.get(f"mcp_profile:{profile_name}", entity=hb.MetaSchema)
    assert row["meta"]["source"] == "workspace"
    assert row["meta"]["resolved_tools"] == ["get", "count", "query", "explain"]


def test_workspace_toolkit_rejects_invalid_metaschema_profile_row():
    """Invalid workspace-owned MCP profile rows should fail with profile context."""
    workspace = hb.HeavenBase("mcp-profile-row-invalid-test", backends={"main": {"type": "inmem"}})
    profile_name = "workspace-row-invalid-profile"
    workspace.set(
        {
            "object_id": f"mcp_profile:{profile_name}",
            "kind": "mcp_profile",
            "subject_id": profile_name,
            "name": profile_name,
            "active": True,
            "ws": workspace.id,
            "meta": {
                "name": profile_name,
                "tools": ["missing_workspace_tool"],
                "serializer": "jstr",
            },
        },
        entity=hb.MetaSchema,
    )

    with pytest.raises(ValueError, match="missing_workspace_tool"):
        workspace.to_mcp(name="mcp-profile-row-invalid-test", profile=profile_name)


def test_workspace_toolkit_loads_serializer_alias_from_metaschema_row_without_global_registration():
    """Workspace-owned serializer rows should alias registered callables without mutating process registries."""
    workspace = hb.HeavenBase("mcp-serializer-row-test", backends={"main": {"type": "inmem"}})
    workspace.register(_product_def("serializer-row-product"))
    workspace.upsert(
        "serializer-row-product",
        {"object_id": "p1", "name": "Desk", "body": "oak desk", "price": 80},
    )
    serializer_name = "workspace-row-markdown"
    profile_name = "workspace-row-markdown-profile"
    workspace.set(
        {
            "object_id": f"mcp_serializer:{serializer_name}",
            "kind": "mcp_serializer",
            "subject_id": serializer_name,
            "name": serializer_name,
            "desc": "Workspace markdown serializer alias",
            "type": "serializer",
            "target": "markdown",
            "active": True,
            "ws": workspace.id,
            "meta": {
                "name": serializer_name,
                "serializer": "markdown",
                "desc": "Workspace markdown serializer alias",
            },
        },
        entity=hb.MetaSchema,
    )
    workspace.set(
        {
            "object_id": f"mcp_profile:{profile_name}",
            "kind": "mcp_profile",
            "subject_id": profile_name,
            "name": profile_name,
            "desc": "Workspace profile using serializer alias",
            "type": serializer_name,
            "target": serializer_name,
            "active": True,
            "ws": workspace.id,
            "meta": {
                "name": profile_name,
                "tools": ["query"],
                "entities": ["serializer-row-product"],
                "serializer": serializer_name,
                "desc": "Workspace profile using serializer alias",
            },
        },
        entity=hb.MetaSchema,
    )

    toolkit = workspace.to_mcp(name="mcp-serializer-row-test", profile=profile_name)

    assert serializer_name not in ToolSerializer.snapshot()
    text = toolkit.run_to_str("query", entity="serializer-row-product", spec={"select": ["name", "price"]})
    assert "| name | price |" in text
    serializer_row = workspace.get(f"mcp_serializer:{serializer_name}", entity=hb.MetaSchema)
    assert serializer_row["meta"]["source"] == "workspace"
    assert serializer_row["meta"]["alias_of"] == "markdown"
    assert serializer_row["meta"]["runtime_only"] is False
    assert serializer_row["target"].endswith("markdown_serializer")
    profile_row = workspace.get(f"mcp_profile:{profile_name}", entity=hb.MetaSchema)
    assert profile_row["meta"]["serializer"] == serializer_name
    assert profile_row["meta"]["resolved_tools"] == ["query", "explain"]


def test_workspace_toolkit_rejects_invalid_metaschema_serializer_row():
    """Workspace-owned serializer rows should not import arbitrary callable targets."""
    workspace = hb.HeavenBase("mcp-serializer-row-invalid-test", backends={"main": {"type": "inmem"}})
    serializer_name = "workspace-row-invalid-serializer"
    workspace.set(
        {
            "object_id": f"mcp_serializer:{serializer_name}",
            "kind": "mcp_serializer",
            "subject_id": serializer_name,
            "name": serializer_name,
            "active": True,
            "ws": workspace.id,
            "target": "not.a.registered.serializer",
            "meta": {
                "name": serializer_name,
                "serializer": "not.a.registered.serializer",
            },
        },
        entity=hb.MetaSchema,
    )

    with pytest.raises(ValueError, match="unknown registered serializer target"):
        workspace.to_mcp(name="mcp-serializer-row-invalid-test")


@pytest.mark.fast
def test_workspace_toolkit_loads_family_alias_from_metaschema_row_without_global_registration():
    """Workspace-owned toolkit family rows should alias registered families locally."""
    workspace = hb.HeavenBase("mcp-family-row-test", backends={"main": {"type": "inmem"}})
    workspace.register(_product_def("family-row-product"))
    workspace.upsert(
        "family-row-product",
        {"object_id": "p1", "name": "Desk", "body": "oak desk", "price": 80},
    )
    family_name = "workspace-count-family"
    profile_name = "workspace-count-family-profile"
    workspace.set(
        {
            "object_id": f"toolkit_family:{family_name}",
            "kind": "toolkit_family",
            "subject_id": family_name,
            "name": family_name,
            "desc": "Count-only workspace family alias",
            "source": "workspace",
            "target": "workspace",
            "active": True,
            "ws": workspace.id,
            "meta": {
                "name": family_name,
                "alias_of": "workspace",
                "tool_names": ["count"],
                "desc": "Count-only workspace family alias",
            },
        },
        entity=hb.MetaSchema,
    )
    workspace.set(
        {
            "object_id": f"mcp_profile:{profile_name}",
            "kind": "mcp_profile",
            "subject_id": profile_name,
            "name": profile_name,
            "desc": "Workspace profile using family alias",
            "type": "jstr",
            "target": "jstr",
            "active": True,
            "ws": workspace.id,
            "meta": {
                "name": profile_name,
                "tools": ["count"],
                "entities": ["family-row-product"],
                "serializer": "jstr",
                "desc": "Workspace profile using family alias",
            },
        },
        entity=hb.MetaSchema,
    )

    toolkit = workspace.to_mcp(name="mcp-family-row-test", profile=profile_name)

    assert family_name not in hb_ext.registered_workspace_toolkit_families()
    assert toolkit.list_tools() == ["count"]
    assert "Count-only workspace family alias" in toolkit.description
    assert toolkit.run("count", entity="family-row-product") == {"entity": "family-row-product", "n": 1}
    family_row = workspace.get(f"toolkit_family:{family_name}", entity=hb.MetaSchema)
    assert family_row["source"] == "workspace"
    assert family_row["meta"]["source"] == "workspace"
    assert family_row["meta"]["alias_of"] == "workspace"
    assert family_row["meta"]["runtime_only"] is False
    assert family_row["meta"]["tool_names"] == ["count"]


@pytest.mark.fast
def test_workspace_toolkit_loads_metadata_only_family_from_metaschema_row():
    """Workspace-owned toolkit family rows can group existing tools without a Python builder."""
    workspace = hb.HeavenBase("mcp-family-row-metadata-test", backends={"main": {"type": "inmem"}})
    workspace.register(_product_def("family-row-metadata-product"))
    workspace.upsert(
        "family-row-metadata-product",
        {"object_id": "p1", "name": "Desk", "body": "oak desk", "price": 80},
    )
    family_name = "workspace-read-family"
    profile_name = "workspace-read-family-profile"
    workspace.set(
        {
            "object_id": f"toolkit_family:{family_name}",
            "kind": "toolkit_family",
            "subject_id": family_name,
            "name": family_name,
            "desc": "Workspace-owned read family",
            "source": "workspace",
            "active": True,
            "ws": workspace.id,
            "meta": {
                "name": family_name,
                "tool_names": ["count", "query", "explain"],
                "desc": "Workspace-owned read family",
            },
        },
        entity=hb.MetaSchema,
    )
    workspace.set(
        {
            "object_id": f"mcp_profile:{profile_name}",
            "kind": "mcp_profile",
            "subject_id": profile_name,
            "name": profile_name,
            "desc": "Workspace profile using metadata family",
            "type": "jstr",
            "target": "jstr",
            "active": True,
            "ws": workspace.id,
            "meta": {
                "name": profile_name,
                "tools": ["count", "query"],
                "entities": ["family-row-metadata-product"],
                "serializer": "jstr",
                "desc": "Workspace profile using metadata family",
            },
        },
        entity=hb.MetaSchema,
    )

    toolkit = workspace.to_mcp(name="mcp-family-row-metadata-test", profile=profile_name)

    assert family_name not in hb_ext.registered_workspace_toolkit_families()
    assert toolkit.list_tools() == ["count", "query", "explain"]
    assert "Workspace-owned read family" in toolkit.description
    assert toolkit.run("count", entity="family-row-metadata-product") == {"entity": "family-row-metadata-product", "n": 1}
    frame = toolkit.run("query", entity="family-row-metadata-product", spec={"select": ["name", "price"]})
    assert frame["ids"] == ["p1"]
    family_row = workspace.get(f"toolkit_family:{family_name}", entity=hb.MetaSchema)
    assert family_row["source"] == "workspace"
    assert family_row["target"].endswith("_metadata_only_tool_functions")
    assert family_row["meta"]["source"] == "workspace"
    assert family_row["meta"]["alias_of"] is None
    assert family_row["meta"]["runtime_only"] is False
    assert family_row["meta"]["metadata_only"] is True
    assert family_row["meta"]["tool_names"] == ["count", "query", "explain"]
    second = workspace.to_mcp(name="mcp-family-row-metadata-test-second", profile=profile_name)
    assert second.list_tools() == ["count", "query", "explain"]
    assert second.run("count", entity="family-row-metadata-product") == {"entity": "family-row-metadata-product", "n": 1}


def test_workspace_toolkit_rejects_invalid_metaschema_family_row():
    """Workspace-owned toolkit family rows should not import arbitrary builders."""
    workspace = hb.HeavenBase("mcp-family-row-invalid-test", backends={"main": {"type": "inmem"}})
    family_name = "workspace-row-invalid-family"
    workspace.set(
        {
            "object_id": f"toolkit_family:{family_name}",
            "kind": "toolkit_family",
            "subject_id": family_name,
            "name": family_name,
            "source": "workspace",
            "target": "not.a.registered.family",
            "active": True,
            "ws": workspace.id,
            "meta": {
                "name": family_name,
                "alias_of": "not.a.registered.family",
                "tool_names": ["count"],
            },
        },
        entity=hb.MetaSchema,
    )

    with pytest.raises(ValueError, match="unknown registered toolkit family target"):
        workspace.to_mcp(name="mcp-family-row-invalid-test")


def test_workspace_toolkit_rejects_unknown_tool_in_metadata_only_family_row():
    """Metadata-only toolkit family rows should only group already registered tools."""
    workspace = hb.HeavenBase("mcp-family-row-unknown-tool-test", backends={"main": {"type": "inmem"}})
    family_name = "workspace-row-unknown-tool-family"
    workspace.set(
        {
            "object_id": f"toolkit_family:{family_name}",
            "kind": "toolkit_family",
            "subject_id": family_name,
            "name": family_name,
            "source": "workspace",
            "active": True,
            "ws": workspace.id,
            "meta": {
                "name": family_name,
                "tool_names": ["missing_workspace_tool"],
            },
        },
        entity=hb.MetaSchema,
    )

    with pytest.raises(ValueError, match="unknown workspace tool"):
        workspace.to_mcp(name="mcp-family-row-unknown-tool-test")


def test_describe_entity_exposes_derive_metadata():
    """MCP describe_entity should expose class-level derive declarations."""
    workspace = hb.HeavenBase("mcp-describe-derives-test", backends={"main": {"type": "inmem"}})

    class McpDescribeDerived(hb.Entity):
        identifier = "mcp-describe-derived"
        __derive__ = [hb.Entity.derive("catalog.name").compute(lambda name: name, inputs=["name"])]

        name = hb.field(hb.ShortText)

    workspace.register(McpDescribeDerived)
    described = workspace.to_mcp(name="mcp-describe-derives-test").run("describe_entity", entity="mcp-describe-derived")

    assert described["derives"][0]["target"] == "catalog.name"
    assert described["derives"][0]["computes"][0]["inputs"] == ["name"]


def test_workspace_toolkit_agent_profile_limits_tools_and_runs_core_flow():
    """Agent MCP profile should keep the day-one tool surface narrow."""
    workspace = hb.HeavenBase("mcp-agent-profile-test", backends={"main": {"type": "inmem"}})
    toolkit = workspace.to_mcp(name="mcp-agent-profile-test", profile="agent")

    assert toolkit.list_tools() == [
        "define_entity",
        "list_entities",
        "describe_entity",
        "upsert",
        "get",
        "set",
        "count",
        "query",
        "explain",
        "list_skills",
        "read_skill",
    ]
    assert "delete" not in toolkit.tools
    assert "upsert_many" not in toolkit.tools
    assert "delete for mutations" not in toolkit.description
    assert "Available tools: define_entity, list_entities" in toolkit.description

    toolkit.run("define_entity", entity=_product_def("agent-product"))
    toolkit.run("upsert", entity="agent-product", row={"object_id": "p1", "name": "Desk", "body": "oak desk", "price": 80})
    toolkit.run("set", entity="agent-product", row={"object_id": "p1", "price": 75})

    assert toolkit.run("list_entities")["entities"] == [
        "agent-agent",
        "agent-message",
        "agent-product",
        "agent-session",
        "agent-skill",
    ]
    assert toolkit.run("describe_entity", entity="agent-product")["schema"]["entity_id"] == "agent-product"
    assert toolkit.run("get", object_id="p1", entity="agent-product")["row"]["price"] == 75.0
    assert toolkit.run("count", entity="agent-product")["n"] == 1
    assert toolkit.run("query", entity="agent-product", spec={"filter": {"price": {"$lt": 100}}})["ids"] == ["p1"]
    assert toolkit.run("explain", entity="agent-product", spec={"filter": {"price": {"$lt": 100}}})["steps"]


def test_skill_tools_require_agent_extension_and_profile_skills(tmp_path, default_extensions_without_agent):
    """Skill MCP tools should appear only when agent extension and profile skills are enabled."""
    workspace = hb.HeavenBase("mcp-skill-gate-test", backends={"main": {"type": "inmem"}})
    without_agent = workspace.to_mcp(name="mcp-skill-gate-test", profile="agent")
    assert "list_skills" not in without_agent.list_tools()
    assert "read_skill" not in without_agent.list_tools()

    workspace.enable_extension("agent")
    with_agent = workspace.to_mcp(name="mcp-skill-gate-test-agent", profile="agent")
    assert with_agent.list_tools()[-2:] == ["list_skills", "read_skill"]
    family = workspace.get("toolkit_family:agent", entity=hb.MetaSchema)
    assert family["meta"]["builder"].endswith("agent_tool_functions")

    register_profile(
        ProfileSpec(
            name="no-skills-agent",
            tools=(
                "define_entity",
                "list_entities",
                "describe_entity",
                "upsert",
                "get",
                "set",
                "count",
                "query",
                "explain",
            ),
            skills=(),
            serializer="jstr",
        ),
        replace=True,
    )
    no_skills = workspace.to_mcp(name="mcp-skill-gate-test-no-skills", profile="no-skills-agent")
    assert "list_skills" not in no_skills.list_tools()
    assert "read_skill" not in no_skills.list_tools()


def test_read_skill_workspace_guide_reflects_live_workspace(tmp_path):
    """read_skill(workspace-guide) should describe the active workspace and profile."""
    workspace = hb.HeavenBase("mcp-workspace-guide-test", backends={"main": {"type": "inmem"}})
    workspace.enable_extension("agent")
    toolkit = workspace.to_mcp(name="mcp-workspace-guide-test", profile="agent")
    toolkit.run("define_entity", entity=_product_def("guide-product"))

    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: Demo skill\n---\n# Demo\n", encoding="utf-8")
    skill = hb.Skill.from_path(str(skill_dir))
    skill.register(ws=workspace)

    listed = toolkit.run("list_skills")
    guide = toolkit.run("read_skill", skill_name="workspace-guide")
    persisted = toolkit.run("read_skill", skill_name="demo-skill")

    assert {item["name"] for item in listed["skills"]} == {"workspace-guide", "demo-skill"}
    assert "guide-product" in guide["text"]
    assert "Commerce product used by MCP tests" in guide["text"]
    assert "Profile: agent" in guide["text"] or "Profile: `agent`" in guide["text"]
    assert "object_id" in guide["text"]
    assert "Mongo-style" in guide["text"] or "Mongo-style specs" in guide["text"]
    assert "Loading Skill: demo-skill" in persisted["text"]


def test_workspace_toolkit_memory_profile_runs_memstate_like_flow():
    """Memory MCP profile should expose a small obvious memory surface."""
    workspace = hb.HeavenBase("mcp-memory-profile-test", backends={"main": {"type": "inmem"}})
    workspace.enable_extension("memory")
    toolkit = workspace.to_mcp(name="mcp-memory-profile-test", profile="memory")

    assert toolkit.list_tools() == ["remember", "recall", "search_memory", "list_memory", "set_memory"]
    assert "define_entity" not in toolkit.tools
    assert "Query MetaSchema" not in toolkit.description
    assert "Available tools: remember, recall" in toolkit.description

    remembered = toolkit.run(
        "remember",
        key="repo-context",
        title="Repository context",
        text="HeavenBase stores agent memory in a structured workspace.",
        tags=["agent", "memory"],
        metadata={"source_file": "README.md"},
    )
    recalled = toolkit.run("recall", key="repo-context")
    searched = toolkit.run("search_memory", text="structured", limit=5)
    listed = toolkit.run("list_memory")
    patched = toolkit.run("set_memory", key="repo-context", text="HeavenBase memory profile supports small MCP surfaces.", tags=["mcp"])

    assert remembered["object_id"] == "repo-context"
    assert recalled["row"]["title"] == "Repository context"
    assert searched["ids"] == ["repo-context"]
    assert listed["n"] == 1
    assert patched["row"]["text"] == "HeavenBase memory profile supports small MCP surfaces."
    assert patched["row"]["tags"] == ["mcp"]


def test_workspace_toolkit_memstate_profile_versions_keypaths():
    """Memstate profile should make empty workspaces useful to agents."""
    workspace = hb.HeavenBase("mcp-memstate-profile-test", backends={"main": {"type": "inmem"}})
    workspace.enable_extension("memory")
    toolkit = workspace.to_mcp(name="mcp-memstate-profile-test", profile="memstate")

    assert toolkit.list_tools() == [
        "memstate_remember",
        "memstate_set",
        "memstate_get",
        "memstate_search",
        "memstate_history",
        "memstate_delete",
    ]
    assert "define_entity" not in toolkit.tools
    assert "memstate_get before tasks" in toolkit.description

    empty = toolkit.run("memstate_get", project_id="h4tb2")
    remembered = toolkit.run(
        "memstate_remember",
        project_id="h4tb2",
        content="## Harness\nUse host-side BaseAgent logging.",
        source="codex",
        tags=["agent", "logging"],
    )
    first = toolkit.run("memstate_set", project_id="h4tb2", keypath="cli.command_wrapper", value="host rtk")
    second = toolkit.run("memstate_set", project_id="h4tb2", keypath="cli.command_wrapper", value="logged shell tool")
    browse = toolkit.run("memstate_get", project_id="h4tb2", keypath="cli")
    searched = toolkit.run("memstate_search", project_id="h4tb2", query="logged")
    history = toolkit.run("memstate_history", project_id="h4tb2", keypath="cli.command_wrapper")
    deleted = toolkit.run("memstate_delete", project_id="h4tb2", keypath="cli.command_wrapper")
    after_delete = toolkit.run("memstate_get", project_id="h4tb2", keypath="cli.command_wrapper")
    deleted_history = toolkit.run("memstate_history", project_id="h4tb2", keypath="cli.command_wrapper")
    long_key = ".".join([f"segment{i}" for i in range(20)])
    long_row = toolkit.run("memstate_set", project_id="h4tb2", keypath=long_key, value="bounded id")

    assert empty["rows"] == []
    assert remembered["keypath"] == "project.h4tb2.harness"
    assert first["version"] == 1
    assert second["version"] == 2
    assert second["object_id"].startswith("mem-")
    assert len(second["object_id"]) <= 63
    assert second["conflicts"][0]["text"] == "host rtk"
    assert browse["rows"][0]["text"] == "logged shell tool"
    assert searched["ids"] == [second["object_id"]]
    assert [row["version"] for row in history["rows"]] == [1, 2]
    assert deleted["row"]["deleted"] is True
    assert after_delete["rows"] == []
    assert [row["version"] for row in deleted_history["rows"]] == [1, 2, 3]
    assert len(long_row["keypath"]) > 63
    assert len(long_row["object_id"]) <= 63


def test_define_entity_accepts_json_schema_scalar_aliases():
    """MCP define_entity should accept common JSON-Schema scalar aliases from agents."""
    workspace = hb.HeavenBase("mcp-json-schema-alias-test", backends={"main": {"type": "inmem"}})
    toolkit = workspace.to_mcp(name="mcp-json-schema-alias-test")

    defined = toolkit.run(
        "define_entity",
        entity={
            "name": "Product",
            "fields": {
                "object_id": {"type": "string"},
                "name": {"type": "string"},
                "price": {"type": "number"},
                "category": {"type": "string"},
            },
        },
    )

    fields = defined["schema"]["fields"]
    assert fields["object_id"]["dtype"]["type"] == "identifier"
    assert fields["name"]["dtype"]["type"] == "short-text"
    assert fields["price"]["dtype"]["type"] == "float"
    assert fields["category"]["dtype"]["type"] == "short-text"

    inserted = toolkit.run(
        "upsert_many",
        entity="Product",
        rows=[
            {"object_id": "p1", "name": "Keyboard", "price": 89.99, "category": "electronics"},
            {"object_id": "p2", "name": "Monitor", "price": 349, "category": "electronics"},
        ],
    )
    queried = toolkit.run("query", entity="Product", spec={"filter": {"price": {"$lt": 100}}, "select": ["name", "price"]})

    assert inserted["object_ids"] == ["p1", "p2"]
    assert toolkit.run("describe_entity", entity="Product")["schema"]["entity_id"] == "product"
    assert toolkit.run("count", entity="Product") == {"entity": "product", "n": 2}
    assert queried["ids"] == ["p1"]


def test_define_entity_accepts_json_schema_properties_shape():
    """MCP define_entity should accept a compact JSON-Schema properties shape."""
    workspace = hb.HeavenBase("mcp-json-schema-properties-test", backends={"main": {"type": "inmem"}})
    toolkit = workspace.to_mcp(name="mcp-json-schema-properties-test")

    defined = toolkit.run(
        "define_entity",
        entity={
            "title": "InventoryItem",
            "properties": {
                "object_id": {"type": "string"},
                "stock": {"type": "integer"},
                "attributes": {"type": "object"},
            },
            "required": ["stock"],
        },
    )

    fields = defined["schema"]["fields"]
    assert defined["schema"]["entity_id"] == "inventory-item"
    assert fields["object_id"]["dtype"]["type"] == "identifier"
    assert fields["stock"]["dtype"]["type"] == "integer"
    assert fields["stock"]["required"] is True
    assert fields["attributes"]["dtype"]["type"] == "json"


def test_define_entity_rejects_bare_field_map_with_actionable_error():
    """MCP define_entity should explain the expected wrapper when agents pass only fields."""
    workspace = hb.HeavenBase("mcp-bare-field-map-test", backends={"main": {"type": "inmem"}})
    toolkit = workspace.to_mcp(name="mcp-bare-field-map-test")

    with pytest.raises(RuntimeError, match="define_entity expects an entity object"):
        toolkit.run("define_entity", entity={"object_id": {"type": "string"}, "name": {"type": "string"}})


def test_profile_spec_registers_and_resolves_tool_groups():
    """ProfileSpec should expand tool groups and reject duplicate registration."""
    register_profile(
        ProfileSpec(name="test-read-query", tools=("read", "query"), entities=("demo-entity",), serializer="jstr"),
        replace=True,
    )
    resolved = resolve_profile("test-read-query")
    assert resolved.tools == TOOL_GROUPS["read"] + TOOL_GROUPS["query"]
    assert resolved.entities == ("demo-entity",)
    with pytest.raises(ValueError, match="already registered"):
        register_profile(ProfileSpec(name="test-read-query", tools=("query",)))


def test_profile_extends_flattens_and_detects_cycles():
    """Profile extends chains should flatten and reject cycles."""
    register_profile(ProfileSpec(name="test-parent", tools=("schema",), serializer="jstr"), replace=True)
    register_profile(ProfileSpec(name="test-child", extends=("test-parent",), tools=("query",), serializer="markdown"), replace=True)
    resolved = resolve_profile("test-child")
    assert resolved.tools[:3] == TOOL_GROUPS["schema"]
    assert "query" in resolved.tools
    assert resolved.serializer == "markdown"
    register_profile(ProfileSpec(name="test-cycle-a", extends=("test-cycle-b",), tools=("query",)), replace=True)
    register_profile(ProfileSpec(name="test-cycle-b", extends=("test-cycle-a",), tools=("read",)), replace=True)
    with pytest.raises(ValueError, match="cycle"):
        resolve_profile("test-cycle-a")


def test_profile_extends_allows_shared_parent_dag():
    """Profile inheritance should allow sibling parents to share one base."""
    register_profile(ProfileSpec(name="test-shared-base", tools=("schema",), serializer="jstr"), replace=True)
    register_profile(ProfileSpec(name="test-shared-a", extends=("test-shared-base",), tools=("read",)), replace=True)
    register_profile(ProfileSpec(name="test-shared-b", extends=("test-shared-base",), tools=("query",)), replace=True)
    register_profile(ProfileSpec(name="test-shared-child", extends=("test-shared-a", "test-shared-b"), tools=("count",)), replace=True)

    resolved = resolve_profile("test-shared-child")

    assert "define_entity" in resolved.tools
    assert "get" in resolved.tools
    assert "query" in resolved.tools
    assert "count" in resolved.tools


def test_ext_exports_profile_and_serializer_registry():
    """Profile and serializer registries should live under hb.ext."""
    artifact_root = hb.Toolkit.__module__.partition(".")[0]

    assert artifact_root.startswith("_heavenbase_artifact_")
    assert hb_ext.ProfileSpec is ProfileSpec
    assert not hasattr(hb_ext, "register_profile_tools")
    assert hb_ext.registered_profile_tool_names is registered_profile_tool_names
    assert hb_ext.ProfileSpec.__module__.partition(".")[0] == artifact_root
    assert hb_ext.registered_profile_tool_names.__module__.partition(".")[0] == artifact_root
    assert hb_ext.ToolSerializer.__module__.partition(".")[0] == artifact_root
    assert "full" in hb_ext.registered_profiles()
    assert "query" in hb_ext.registered_profile_tool_names()
    assert "jstr" in hb_ext.ToolSerializer.snapshot()


def test_workspace_toolkit_family_spec_lives_in_artifact_family_contract_module():
    """Workspace toolkit family specs should resolve from the artifact's contract module."""
    package_spec = _toolkit_export("families", "WorkspaceToolkitFamilySpec")
    contract_spec = _toolkit_export("families.family", "WorkspaceToolkitFamilySpec")
    artifact_root = hb.Toolkit.__module__.partition(".")[0]

    assert hb_ext.WorkspaceToolkitFamilySpec is contract_spec
    assert package_spec is contract_spec
    assert contract_spec.__module__ == f"{artifact_root}.families.family"


def test_profile_tool_names_have_no_separate_registration_authority():
    """Concrete profile tool names must come from toolkit family records."""
    with pytest.raises(ValueError, match="MCP profile tool or tool group"):
        register_profile(ProfileSpec(name="test-unowned-tool", tools=("demo_extension_tool",)), replace=True)


def test_workspace_toolkit_family_registers_profile_tool_names():
    """Workspace toolkit families should publish contributed names to profiles."""
    tool_name = "demo_family_profile_tool"

    register_workspace_toolkit_family(
        WorkspaceToolkitFamilySpec("test-family-profile-tools", (tool_name,), _empty_family_builder),
        replace=True,
    )
    register_profile(ProfileSpec(name="test-family-profile-tool", tools=(tool_name,)), replace=True)
    definition = hb.DEFAULT_CONTEXT.modules().inspect("toolkit_family", "test-family-profile-tools")[0]["record"]["meta"]["definition"]

    assert tool_name in registered_profile_tool_names()
    assert definition["builder"]["source"] == "python-import"
    assert definition["builder"]["target"].endswith(":_empty_family_builder")
    assert resolve_profile("test-family-profile-tool").tools == (tool_name,)


def test_entity_allowed_requires_entity_inside_scoped_profile():
    """Scoped profiles should not treat an omitted entity as globally allowed."""
    assert entity_allowed(None, "*") is True
    assert entity_allowed(None, ("mcp-product",)) is False
    assert entity_allowed("mcp-product", ("mcp-product",)) is True


def test_workspace_toolkit_explicit_tool_allowlist_overrides_profile():
    """Explicit MCP tool allowlists should override configured profiles."""
    workspace = hb.HeavenBase("mcp-tool-allowlist-test", backends={"main": {"type": "inmem"}})
    toolkit = workspace.to_mcp(name="mcp-tool-allowlist-test", profile="agent", tools=["list_entities", "count"])

    assert toolkit.list_tools() == ["list_entities", "count"]

    with pytest.raises(ValueError, match="MCP workspace profile"):
        workspace.to_mcp(profile="missing-profile")
    with pytest.raises(ValueError, match="MCP workspace tool"):
        workspace.to_mcp(tools=["missing_tool"])


def test_profile_spec_register_and_resolve_extends():
    """ProfileSpec should flatten extends chains and expand tool groups."""
    register_profile(ProfileSpec(name="read-only-base", tools=("read", "query"), serializer="jstr"), replace=True)
    register_profile(
        ProfileSpec(name="read-only-child", extends=("read-only-base",), tools=("schema",), serializer="markdown"),
        replace=True,
    )

    flat = resolve_profile("read-only-child")

    assert flat.tools[0] == "get"
    assert "define_entity" in flat.tools
    assert flat.serializer == "markdown"


def test_profile_register_rejects_duplicate():
    """register_profile should reject duplicate names unless replace is true."""
    spec = hb.ext.ProfileSpec(name="dup-profile-test", tools=("count",))
    hb.ext.register_profile(spec, replace=True)
    with pytest.raises(ValueError, match="already registered"):
        hb.ext.register_profile(spec)


def test_profile_extends_cycle_raises():
    """resolve_profile should detect extends cycles."""
    register_profile(ProfileSpec(name="cycle-a", extends=("cycle-b",)), replace=True)
    register_profile(ProfileSpec(name="cycle-b", extends=("cycle-a",)), replace=True)
    with pytest.raises(ValueError, match="extends cycle"):
        resolve_profile("cycle-a")


def test_profile_unknown_tool_group_raises():
    """Profile registration should reject unknown tool groups."""
    with pytest.raises(ValueError, match="MCP profile tool or tool group"):
        register_profile(ProfileSpec(name="bad-tools", tools=("not-a-group",)), replace=True)


def test_tool_serializer_protocol_and_markdown_output():
    """ToolSerializer should own registration, lookup, and rendering."""
    jstr = ToolSerializer.load("jstr")
    assert jstr({"value": 1}) == '{"value": 1}'
    assert jstr.to_ref().endswith("jstr_serializer")
    table = ToolSerializer.load("markdown")({"rows": [{"name": "Desk", "price": 80}], "n": 1})
    assert "| name | price |" in table
    assert "Desk" in table
    with pytest.raises(ValueError, match="MCP profile serializer"):
        ToolSerializer.load("missing-serializer")
    ToolSerializer("demo-copy", ToolSerializer.load("jstr").serialize).register(replace=True)
    definition = hb.DEFAULT_CONTEXT.modules().inspect("serializer", "demo-copy")[0]["record"]["meta"]["definition"]
    assert definition["serialize"]["source"] == "python-import"
    with pytest.raises(ValueError, match="already registered"):
        ToolSerializer("demo-copy", ToolSerializer.load("jstr").serialize).register()
    with pytest.raises(TypeError, match=r"accept \(output, \*, config\)"):
        ToolSerializer("missing-config-context", lambda value: str(value))
    with pytest.raises(TypeError, match="must return str"):
        ToolSerializer("bad-output", lambda _value, *, config=None: {"not": "text"})(None)

    workspace = hb.HeavenBase("mcp-markdown-serializer-test", backends={"main": {"type": "inmem"}})
    agent_toolkit = workspace.to_mcp(name="mcp-markdown-serializer-test", profile="agent")
    full_toolkit = workspace.to_mcp(name="mcp-markdown-serializer-jstr-test", profile="full")
    agent_toolkit.run("define_entity", entity=_product_def("markdown-product"))
    agent_toolkit.run("upsert", entity="markdown-product", row={"object_id": "p1", "name": "Desk", "body": "oak desk", "price": 80})
    spec = {"filter": {"price": {"$lt": 100}}, "select": ["name", "price"]}
    markdown_text = agent_toolkit.run_to_str("query", entity="markdown-product", spec=spec)
    json_text = full_toolkit.run_to_str("query", entity="markdown-product", spec=spec)
    assert markdown_text.startswith("{") is False
    assert json_text == dumps_jstr(full_toolkit.run("query", entity="markdown-product", spec=spec), compact=True)


def test_profile_entity_scoping_filters_list_and_blocks_query():
    """Resolved profiles should scope entity access for every data tool."""
    register_profile(
        ProfileSpec(
            name="product-only",
            tools=("schema", "read", "write", "delete", "query"),
            entities=("mcp-product",),
            serializer="jstr",
        ),
        replace=True,
    )
    workspace = hb.HeavenBase("mcp-entity-scope-test", backends={"main": {"type": "inmem"}})
    full = workspace.to_mcp(profile="full")
    full.run("define_entity", entity=_product_def("mcp-product"))
    full.run(
        "define_entity",
        entity={
            "entity_id": "other-product",
            "name": "OtherProduct",
            "fields": {
                "object_id": {"type": "identifier", "pk": True},
                "name": {"type": "short-text"},
                "body": {"type": "long-text"},
                "price": {"type": "float"},
            },
        },
    )
    full.run("upsert", entity="mcp-product", row={"object_id": "p1", "name": "Desk", "body": "oak desk", "price": 80})
    full.run("upsert", entity="other-product", row={"object_id": "o1", "name": "Hidden", "body": "outside scope", "price": 10})

    scoped = workspace.to_mcp(profile="product-only")
    listed = scoped.run("list_entities")

    assert listed["entities"] == ["mcp-product"]
    scoped.run("query", entity="mcp-product", spec={"filter": {"price": {"$lt": 100}}})
    assert scoped.run("get", object_id="p1")["row"]["name"] == "Desk"
    assert scoped.run("get", object_id="o1")["row"] is None
    assert scoped.run("get_many", object_ids=["p1", "o1"])["rows"][1] is None
    assert scoped.run("exists", object_id="p1")["exists"] is True
    assert scoped.run("exists", object_id="o1")["exists"] is False
    assert scoped.run("exists_many", object_ids=["p1", "o1"])["exists"] == [True, False]
    assert scoped.run("set", row={"object_id": "p1", "price": 75})["row"]["price"] == 75.0
    assert scoped.run("set_many", rows=[{"object_id": "p1", "price": 70}])["rows"][0]["price"] == 70.0
    with pytest.raises(RuntimeError, match="inside MCP profile"):
        scoped.run("set_many", rows=[{"object_id": "p1", "price": 65}, {"object_id": "o1", "price": 12}])
    assert workspace.get("p1", entity="mcp-product")["price"] == 70.0
    assert scoped.run("delete", target="o1")["deleted"] is False
    assert scoped.run("delete_many", targets=["o1"])["deleted"] == [False]
    assert workspace.get("o1", entity="other-product")["name"] == "Hidden"
    with pytest.raises(RuntimeError, match="outside MCP profile"):
        scoped.run("query", entity="other-product", spec={})
    with pytest.raises(RuntimeError, match="outside MCP profile"):
        scoped.run("get", object_id="o1", entity="other-product")
    with pytest.raises(RuntimeError, match="inside MCP profile"):
        scoped.run("set", row={"object_id": "o1", "price": 12})
    with pytest.raises(RuntimeError, match="outside MCP profile"):
        scoped.run("delete", target={"entity": "other-product", "object_id": "o1"})
    with pytest.raises(RuntimeError, match="outside MCP profile"):
        scoped.run("define_entity", entity=_product_def("scope-hidden-new"))
    assert "scope-hidden-new" not in workspace.entities


def test_scoped_mcp_patch_resolution_holds_the_workspace_mutation_lock(monkeypatch):
    """Competing workspace mutations stay blocked from patch scope resolution through apply."""
    from threading import Event, Thread

    WorkspaceToolContext = _toolkit_export("families.workspace.context", "WorkspaceToolContext")

    profile_name = "mcp-locked-patch-scope"
    register_profile(
        ProfileSpec(
            name=profile_name,
            tools=("set", "set_many"),
            entities=("mcp-locked-product",),
        ),
        replace=True,
    )
    workspace = hb.HeavenBase("mcp-locked-patch", backends={"main": {"type": "inmem"}})
    full = workspace.to_mcp(profile="full")
    full.run("define_entity", entity=_product_def("mcp-locked-product"))
    full.run("upsert", entity="mcp-locked-product", row={"object_id": "p1", "name": "Desk", "body": "oak", "price": 80})
    scoped = workspace.to_mcp(profile=profile_name)
    original = WorkspaceToolContext.entity_for_patch
    blocked = []
    competitors = []

    def observed_resolution(context, row, entity):
        attempted = Event()
        acquired = Event()

        def compete():
            attempted.set()
            with context.workspace._lock:
                acquired.set()

        thread = Thread(target=compete)
        competitors.append(thread)
        thread.start()
        assert attempted.wait(timeout=1)
        blocked.append(not acquired.wait(timeout=0.05))
        return original(context, row, entity)

    monkeypatch.setattr(WorkspaceToolContext, "entity_for_patch", observed_resolution)

    assert scoped.run("set", row={"object_id": "p1", "price": 75})["row"]["price"] == 75.0
    assert scoped.run("set_many", rows=[{"object_id": "p1", "price": 70}])["rows"][0]["price"] == 70.0
    for thread in competitors:
        thread.join(timeout=1)

    assert blocked == [True, True]
    assert all(not thread.is_alive() for thread in competitors)


def test_workspace_delete_tools_preserve_graph_batch_semantics():
    """MCP delete_many forwards one logical batch and exposes the explicit edge-policy override."""

    class McpGraphDoc(hb.Entity):
        identifier = "mcp-graph-delete-doc"

        title = hb.field(hb.ShortText)
        cites = hb.field(hb.HyperG["cited" : hb.Identifier["mcp-graph-delete-doc"]]).store(strategy=hb.GraphEdge).optional()

    workspace = hb.HeavenBase("mcp-graph-delete", backends={"main": {"type": "inmem"}})
    try:
        workspace.register(McpGraphDoc)
        workspace.upsert_many(
            McpGraphDoc,
            [
                {"object_id": "a", "title": "a", "cites": [{"cited": "b"}]},
                {"object_id": "b", "title": "b", "cites": [{"cited": "a"}]},
            ],
        )
        toolkit = workspace.to_mcp(profile="full")
        targets = [
            {"entity": McpGraphDoc.identifier, "object_id": "a"},
            {"entity": McpGraphDoc.identifier, "object_id": "b"},
        ]
        assert toolkit.run("delete_many", targets=targets) == {
            "targets": targets,
            "deleted": [True, True],
            "n": 2,
            "deleted_n": 2,
        }

        workspace.upsert_many(
            McpGraphDoc,
            [
                {"object_id": "source", "title": "source", "cites": [{"cited": "target"}]},
                {"object_id": "target", "title": "target", "cites": []},
            ],
        )
        with pytest.raises(RuntimeError):
            toolkit.run("delete", target={"entity": McpGraphDoc.identifier, "object_id": "target"})
        result = toolkit.run(
            "delete",
            target={"entity": McpGraphDoc.identifier, "object_id": "target"},
            edges="detach",
        )
        assert result["deleted"] is True
        assert workspace.get("source", entity=McpGraphDoc)["cites"] == []
    finally:
        workspace.drop()


def test_scoped_mcp_graph_delete_enforces_the_full_mutation_closure(monkeypatch):
    """A scoped GraphEdge delete cannot rewrite or disclose an out-of-scope source."""

    class VisibleTarget(hb.Entity):
        identifier = "mcp-scope-visible-target"

        name = hb.field(hb.ShortText)

    class HiddenSource(hb.Entity):
        identifier = "mcp-scope-hidden-source"

        name = hb.field(hb.ShortText)
        links = hb.field(hb.HyperG["target" : hb.Identifier[VisibleTarget]]).store(strategy=hb.GraphEdge).optional()

    profile_name = "mcp-graph-visible-only"
    register_profile(
        ProfileSpec(
            name=profile_name,
            tools=("delete",),
            entities=(VisibleTarget.identifier,),
        ),
        replace=True,
    )
    workspace = hb.HeavenBase("mcp-graph-scope", backends={"main": {"type": "inmem"}})
    try:
        workspace.register(VisibleTarget)
        workspace.register(HiddenSource)
        workspace.upsert(VisibleTarget, {"object_id": "visible-target", "name": "Visible"})
        workspace.upsert(
            HiddenSource,
            {
                "object_id": "secret-source",
                "name": "Hidden",
                "links": [{"target": "visible-target"}],
            },
        )
        system_entity_ids = frozenset(workspace._catalog.system_entity_ids())
        monkeypatch.setattr(workspace._catalog, "system_entity_ids", lambda: system_entity_ids | {HiddenSource.identifier})
        scoped = workspace.to_mcp(profile=profile_name)
        target = {"entity": VisibleTarget.identifier, "object_id": "visible-target"}

        for mode in ("schema", "restrict", "detach"):
            with pytest.raises(RuntimeError) as raised:
                scoped.run("delete", target=target, edges=mode)
            message = str(raised.value)
            assert message.endswith("GraphEdge delete exceeds the active entity scope")
            assert HiddenSource.identifier not in message
            assert "secret-source" not in message
            assert workspace.get("visible-target", entity=VisibleTarget) is not None
            assert workspace.get("secret-source", entity=HiddenSource)["links"] == [{"target": "visible-target"}]

        with pytest.raises(RuntimeError, match="GraphEdge delete mode"):
            scoped.run("delete", target="absent", edges="cascade")

        register_profile(
            ProfileSpec(
                name="mcp-graph-visible-and-source",
                tools=("delete",),
                entities=(VisibleTarget.identifier, HiddenSource.identifier),
            ),
            replace=True,
        )
        allowed = workspace.to_mcp(profile="mcp-graph-visible-and-source")
        assert allowed.run("delete", target=target, edges="detach")["deleted"] is True
        assert workspace.get("secret-source", entity=HiddenSource)["links"] == []
    finally:
        workspace.drop()


def test_scoped_mcp_graph_writes_do_not_expose_hidden_endpoint_existence():
    """Scoped GraphEdge writes reject hidden target types before endpoint lookup."""

    class HiddenTarget(hb.Entity):
        identifier = "mcp-write-scope-hidden-target"

        name = hb.field(hb.ShortText)

    class VisibleSource(hb.Entity):
        identifier = "mcp-write-scope-visible-source"

        name = hb.field(hb.ShortText)
        links = hb.field(hb.HyperG["target" : hb.Identifier[HiddenTarget]]).store(strategy=hb.GraphEdge).optional()

    profile_name = "mcp-graph-write-visible-only"
    register_profile(
        ProfileSpec(
            name=profile_name,
            tools=("upsert", "upsert_many", "set", "set_many"),
            entities=(VisibleSource.identifier,),
        ),
        replace=True,
    )
    workspace = hb.HeavenBase("mcp-graph-write-scope", backends={"main": {"type": "inmem"}})
    try:
        workspace.register(HiddenTarget)
        workspace.register(VisibleSource)
        workspace.upsert(HiddenTarget, {"object_id": "secret-target", "name": "Hidden"})
        workspace.upsert(VisibleSource, {"object_id": "existing-source", "name": "Visible", "links": []})
        scoped = workspace.to_mcp(profile=profile_name)

        for target in ("secret-target", "missing-target"):
            calls = (
                ("upsert", {"entity": VisibleSource.identifier, "row": {"object_id": "new-source", "name": "New", "links": [{"target": target}]}}),
                (
                    "upsert_many",
                    {"entity": VisibleSource.identifier, "rows": [{"object_id": "new-source", "name": "New", "links": [{"target": target}]}]},
                ),
                ("set", {"entity": VisibleSource.identifier, "row": {"object_id": "existing-source", "links": [{"target": target}]}}),
                (
                    "set_many",
                    {"entity": VisibleSource.identifier, "rows": [{"object_id": "existing-source", "links": [{"target": target}]}]},
                ),
            )
            for tool_name, kwargs in calls:
                with pytest.raises(RuntimeError) as raised:
                    scoped.run(tool_name, **kwargs)
                assert str(raised.value).endswith("GraphEdge write exceeds the active entity scope")
                assert HiddenTarget.identifier not in str(raised.value)

        assert workspace.get("new-source", entity=VisibleSource) is None
        assert workspace.get("existing-source", entity=VisibleSource)["links"] == []
    finally:
        workspace.drop()


def test_scoped_mcp_graph_detach_revalidates_the_full_source_closure():
    """A detach rewrite cannot preserve an out-of-scope GraphEdge in the rewritten source row."""

    class VisibleTarget(hb.Entity):
        identifier = "mcp-detach-scope-visible-target"

        name = hb.field(hb.ShortText)

    class HiddenTarget(hb.Entity):
        identifier = "mcp-detach-scope-hidden-target"

        name = hb.field(hb.ShortText)

    class VisibleSource(hb.Entity):
        identifier = "mcp-detach-scope-visible-source"

        visible_links = hb.field(hb.HyperG["target" : hb.Identifier[VisibleTarget]]).store(strategy=hb.GraphEdge).optional()
        hidden_links = hb.field(hb.HyperG["target" : hb.Identifier[HiddenTarget]]).store(strategy=hb.GraphEdge).optional()

    profile_name = "mcp-detach-visible-closure"
    register_profile(
        ProfileSpec(
            name=profile_name,
            tools=("delete",),
            entities=(VisibleTarget.identifier, VisibleSource.identifier),
        ),
        replace=True,
    )
    workspace = hb.HeavenBase("mcp-detach-scope", backends={"main": {"type": "inmem"}})
    try:
        workspace.register(VisibleTarget)
        workspace.register(HiddenTarget)
        workspace.register(VisibleSource)
        workspace.upsert(VisibleTarget, {"object_id": "visible-target", "name": "Visible"})
        workspace.upsert(HiddenTarget, {"object_id": "hidden-target", "name": "Hidden"})
        workspace.upsert(
            VisibleSource,
            {
                "object_id": "visible-source",
                "visible_links": [{"target": "visible-target"}],
                "hidden_links": [{"target": "hidden-target"}],
            },
        )
        scoped = workspace.to_mcp(profile=profile_name)

        with pytest.raises(RuntimeError) as raised:
            scoped.run("delete", target={"entity": VisibleTarget.identifier, "object_id": "visible-target"}, edges="detach")
        assert str(raised.value).endswith("GraphEdge delete exceeds the active entity scope")
        assert HiddenTarget.identifier not in str(raised.value)
        assert workspace.get("visible-target", entity=VisibleTarget) is not None
        source = workspace.get("visible-source", entity=VisibleSource)
        assert source["visible_links"] == [{"target": "visible-target"}]
        assert source["hidden_links"] == [{"target": "hidden-target"}]
    finally:
        workspace.drop()


def test_profile_from_mapping_rejects_removed_extensions_field():
    """Config profiles must use extends, not the removed extensions field."""
    with pytest.raises(ValueError, match="removed field 'extensions'"):
        profile_spec_from_mapping("legacy", {"extensions": ["agent"], "tools": ("query",)})


def test_profile_from_mapping_rejects_bare_list():
    """Config profiles must use ProfileSpec mappings, not bare tool lists."""
    with pytest.raises(ValueError, match="removed bare tool-list shape"):
        profile_spec_from_mapping("legacy", ["query", "get"])


def test_stale_config_profile_shadow_does_not_break_registered_profile():
    """Registered profiles should not parse stale config rows that cannot take effect."""
    with hb.CM_HVNB.scoped("mcp-stale-profile-shadow-test"):
        hb.CM_HVNB.set("heavenbase.mcp.profiles.full", ["query"])
        hb.CM_HVNB.set("heavenbase.mcp.profiles.legacy", ["query"])

        resolved = resolve_profile("full")

        assert "define_entity" in resolved.tools
        assert "query" in resolved.tools
        with pytest.raises(ValueError, match="removed bare tool-list shape"):
            resolve_profile("legacy")


def test_config_defined_custom_profile_end_to_end():
    """Custom registered profiles should compose parents, entities, and serializers."""
    register_profile(
        profile_spec_from_mapping(
            "config-custom",
            {
                "extends": ["agent"],
                "entities": ["config-product"],
                "serializer": "markdown",
            },
        ),
        replace=True,
    )
    resolved = resolve_profile("config-custom")
    assert "query" in resolved.tools
    assert resolved.entities == ("config-product",)
    assert resolved.serializer == "markdown"

    workspace = hb.HeavenBase("mcp-config-profile-test", backends={"main": {"type": "inmem"}})
    toolkit = workspace.to_mcp(profile="config-custom", tools=["define_entity", "upsert", "query"])
    toolkit.run("define_entity", entity=_product_def("config-product"))
    toolkit.run("upsert", entity="config-product", row={"object_id": "p1", "name": "Desk", "body": "oak desk", "price": 80})

    text = toolkit.run_to_str("query", entity="config-product", spec={"select": ["name"]})

    assert "| name |" in text
    with pytest.raises(RuntimeError, match="outside MCP profile"):
        toolkit.run("query", entity="sys-catalog", spec={})


def test_tool_call_logging_off_by_default(default_extensions_without_agent):
    """MCP tool-call logging should stay disabled unless config and agent extension are both enabled."""
    workspace = hb.HeavenBase("mcp-logging-off-test", detached=True, backends={"main": {"type": "inmem"}})
    workspace.enable_extension("agent")
    toolkit = workspace.to_mcp(name="mcp-logging-off-test", profile="agent")
    toolkit.run("define_entity", entity=_product_def("log-off-product"))
    toolkit.run("query", entity="log-off-product", spec={})

    assert workspace.rows(hb.Session) == []
    assert workspace.rows(hb.Message) == []

    hb.CM_HVNB.set("heavenbase.mcp.log_calls", True)
    try:
        without_agent = hb.HeavenBase("mcp-logging-no-agent", detached=True, backends={"main": {"type": "inmem"}})
        silent = without_agent.to_mcp(name="mcp-logging-no-agent", profile="agent")
        silent.run("list_entities")
        assert hb.Session.schema().entity_id not in without_agent.entities
        assert hb.Message.schema().entity_id not in without_agent.entities
    finally:
        hb.CM_HVNB.set("heavenbase.mcp.log_calls", False)


def test_tool_call_logging_writes_rows_when_enabled():
    """Enabled MCP logging should open one session row and append one message per tool call."""
    hb.CM_HVNB.set("heavenbase.mcp.log_calls", True)
    try:
        workspace = hb.HeavenBase("mcp-logging-on-test", detached=True, backends={"main": {"type": "inmem"}})
        workspace.enable_extension("agent")
        toolkit = workspace.to_mcp(name="mcp-logging-on-test", profile="agent")
        assert getattr(toolkit.run, "__func__", None) is hb.Toolkit.run
        assert getattr(toolkit.run_to_str, "__func__", None) is hb.Toolkit.run_to_str
        toolkit.run("define_entity", entity=_product_def("log-on-product"))
        toolkit.run(
            "upsert",
            entity="log-on-product",
            row={"object_id": "p1", "name": "Desk", "body": "oak desk", "price": 80},
        )
        frame = toolkit.run("query", entity="log-on-product", spec={"select": ["name"]})

        sessions = workspace.rows(hb.Session)
        messages = workspace.rows(hb.Message)

        assert len(sessions) == 1
        assert sessions[0]["title"] == "mcp-logging-on-test"
        assert sessions[0]["state"]["source"] == "mcp"
        assert sessions[0]["state"]["scope"] == "toolkit-exposure"
        assert sessions[0]["state"]["toolkit"] == "mcp-logging-on-test"
        assert sessions[0]["state"]["profile"] == "agent"
        assert len(messages) == 3
        assert {message["payload"]["name"] for message in messages} == {"define_entity", "upsert", "query"}
        query_meta = hb.loads_json(messages[-1]["payload"]["content"])
        assert query_meta["error"] is False
        assert query_meta["cached"] is False
        assert query_meta["result_size"] > 0
        assert query_meta["duration_ms"] >= 0.0
        assert frame["ids"] == ["p1"]
    finally:
        hb.CM_HVNB.set("heavenbase.mcp.log_calls", False)


def test_tool_call_logging_redacts_secrets():
    """Logged arg summaries should redact secret-like values."""
    hb.CM_HVNB.set("heavenbase.mcp.log_calls", True)
    try:
        workspace = hb.HeavenBase("mcp-logging-redact-test", detached=True, backends={"main": {"type": "inmem"}})
        workspace.enable_extension("agent")
        workspace.enable_extension("memory")
        toolkit = workspace.to_mcp(name="mcp-logging-redact-test", profile="memory")
        toolkit.run(
            "remember",
            key="secret-note",
            text="store this",
            metadata={"api_key": "super-secret", "dsn": "postgres://user:pass@db.local/app"},
        )

        message = workspace.rows(hb.Message)[-1]
        logged = message["payload"]["content"]

        assert "super-secret" not in logged
        assert "pass" not in logged
        assert "<redacted>" in logged
    finally:
        hb.CM_HVNB.set("heavenbase.mcp.log_calls", False)


def test_tool_call_logging_failure_isolation(monkeypatch):
    """Logging failures must not change MCP tool-call outcomes."""
    hb.CM_HVNB.set("heavenbase.mcp.log_calls", True)
    try:
        workspace = hb.HeavenBase("mcp-logging-isolate-test", detached=True, backends={"main": {"type": "inmem"}})
        workspace.enable_extension("agent")
        toolkit = workspace.to_mcp(name="mcp-logging-isolate-test", profile="agent")

        def _boom(self, message, **meta):
            raise RuntimeError("logging backend unavailable")

        monkeypatch.setattr(hb.Session, "append", _boom)

        toolkit.run("define_entity", entity=_product_def("log-isolate-product"))
        listed = toolkit.run("list_entities")

        assert "log-isolate-product" in listed["entities"]
    finally:
        hb.CM_HVNB.set("heavenbase.mcp.log_calls", False)


@pytest.mark.anyio
async def test_tool_call_logging_records_fastmcp_boundary_calls():
    """Served MCP calls should log through Toolkit hooks, not instance method patches."""
    Client = pytest.importorskip("fastmcp").Client
    hb.CM_HVNB.set("heavenbase.mcp.log_calls", True)
    try:
        workspace = hb.HeavenBase("mcp-logging-fastmcp-test", detached=True, backends={"main": {"type": "inmem"}})
        workspace.enable_extension("agent")
        toolkit = workspace.to_mcp(name="mcp-logging-fastmcp-test", profile="agent")

        async with Client(toolkit.to_fastmcp()) as client:
            await client.call_tool("list_entities", {})

        messages = workspace.rows(hb.Message)
        assert len(messages) == 1
        assert messages[0]["payload"]["name"] == "list_entities"
        meta = hb.loads_json(messages[0]["payload"]["content"])
        assert meta["error"] is False
        assert meta["cached"] is False
        assert meta["result_size"] > 0
    finally:
        hb.CM_HVNB.set("heavenbase.mcp.log_calls", False)


@pytest.mark.anyio
async def test_tool_call_logging_marks_fastmcp_cache_hits():
    """MCP call logs should distinguish execution from an opted-in cache hit."""
    Client = pytest.importorskip("fastmcp").Client
    hb.CM_HVNB.set("heavenbase.mcp.log_calls", True)
    hb.CM_HVNB.set(
        "heavenbase.mcp.cache",
        {
            "enabled": True,
            "tools": ["list_entities"],
            "ttl": 300,
            "max_entries": 8,
        },
    )
    try:
        workspace = hb.HeavenBase("mcp-logging-cache-test", detached=True, backends={"main": {"type": "inmem"}})
        workspace.enable_extension("agent")
        toolkit = workspace.to_mcp(name="mcp-logging-cache-test", profile="agent")

        async with Client(toolkit.to_fastmcp()) as client:
            await client.call_tool("list_entities", {})
            await client.call_tool("list_entities", {})

        messages = workspace.rows(hb.Message)
        assert len(messages) == 2
        assert [hb.loads_json(message["payload"]["content"])["cached"] for message in messages] == [False, True]
    finally:
        hb.CM_HVNB.set("heavenbase.mcp.log_calls", False)
        hb.CM_HVNB.set(
            "heavenbase.mcp.cache",
            {
                "enabled": False,
                "tools": [],
                "ttl": 300,
                "max_entries": 256,
            },
        )


def test_define_entity_replace_adds_sql_columns(tmp_path):
    """MCP define_entity replace should add SQL columns for new fields."""
    workspace = hb.HeavenBase(
        "mcp-sql-replace-test",
        backends={"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(pj(str(tmp_path), "mcp.db"))}},
    )
    toolkit = workspace.to_mcp(name="mcp-sql-replace-test")

    toolkit.run("define_entity", entity=_product_def("sql-product"))
    toolkit.run("upsert", entity="sql-product", row={"object_id": "p1", "name": "Desk", "body": "oak office desk", "price": 80})
    toolkit.run("define_entity", entity=_product_def("sql-product", with_stock=True), replace=True)
    toolkit.run("set", row={"object_id": "p1", "stock": 7}, entity="sql-product")

    frame = toolkit.run("query", entity="sql-product", spec={"select": ["name", "stock"]})

    assert frame["rows"] == [{"object_id": "p1", "name": "Desk", "stock": 7}]


@pytest.mark.anyio
async def test_toolkit_roundtrip_through_fastmcp_client():
    """FastMCP in-process clients should call HeavenBase toolkit tools."""
    Client = pytest.importorskip("fastmcp").Client

    workspace = hb.HeavenBase("mcp-protocol-test", backends={"main": {"type": "inmem"}})
    toolkit = workspace.to_mcp(name="mcp-protocol-test")
    server = toolkit.to_fastmcp()

    async with Client(server) as client:
        tools = await client.list_tools()
        assert "define_entity" in {tool.name for tool in tools}
        defined = await client.call_tool("define_entity", {"entity": _product_def("protocol-product")})
        inserted = await client.call_tool("upsert", {"entity": "protocol-product", "row": {"object_id": "p1", "name": "Desk", "body": "oak desk", "price": 80}})
        queried = await client.call_tool("query", {"entity": "protocol-product", "spec": {"filter": {"name": "Desk"}}})

    defined_data = loads_jstr(defined.content[0].text)
    inserted_data = loads_jstr(inserted.content[0].text)
    queried_data = loads_jstr(queried.content[0].text)

    assert defined_data["schema"]["entity_id"] == "protocol-product"
    assert inserted_data["object_id"] == "p1"
    assert queried_data["ids"] == ["p1"]
    assert defined.data == defined_data
    assert inserted.data == inserted_data
    assert queried.data == queried_data
    assert defined.structured_content == defined_data
    assert inserted.structured_content == inserted_data
    assert queried.structured_content == queried_data


@pytest.mark.anyio
@pytest.mark.slow
async def test_heavenbase_toolkit_stdio_cli_subprocess_roundtrip(tmp_path, home_env):
    """HeavenBase Toolkit stdio serving should work through the CLI entry point."""
    Client = pytest.importorskip("fastmcp").Client
    PythonStdioTransport = pytest.importorskip("fastmcp.client.transports").PythonStdioTransport

    def add(a: int, b: int) -> int:
        return a + b

    workspace_id = f"stdio-registry-{hb.utils.sha256hash(str(tmp_path))[:12]}"
    workspace_db = hb.utils.path_to_file_uri(pj(str(tmp_path), "stdio-registry.db"))
    backends = {"main": {"type": "sqlite", "name": "main", "database": workspace_db}}
    workspace = hb.HeavenBase(workspace_id, backends=backends)
    hb.Toolkit("stdio-smoke", {"add": add}, namespace="demo", version="1").register(ws=workspace)

    script_path = pj(str(tmp_path), "toolkit_stdio.py")
    save_txt(
        f"""import heavenbase as hb

from heavenbase.cli.app import main


if __name__ == "__main__":
    hb.HeavenBase({workspace_id!r}, backends={backends!r})
    main(["mcp", "stdio", "demo/stdio-smoke:1", "--workspace", {workspace_id!r}])
""",
        script_path,
    )

    async with Client(PythonStdioTransport(script_path, env=home_env(tmp_path / "home"))) as client:
        tools = await client.list_tools()
        result = await client.call_tool("add", {"a": 2, "b": 3})

    assert "add" in {tool.name for tool in tools}
    assert result.content[0].text == "5"


@pytest.mark.fast
def test_workspace_serve_releases_lifecycle_lock_before_wait(monkeypatch):
    """Blocking serve must run outside workspace locks and propagate failures."""
    from threading import Thread

    workspace = hb.HeavenBase("mcp-serve-lock-test", detached=True, backends={"main": {"type": "inmem"}})
    observed = {}

    def available_to_another_thread(acquire, release):
        acquired = []

        def probe():
            found = acquire(blocking=False)
            acquired.append(found)
            if found:
                release()

        thread = Thread(target=probe)
        thread.start()
        thread.join(timeout=1)
        return acquired == [True]

    class FakeToolkit:
        def serve(self, **kwargs):
            observed["context_free"] = available_to_another_thread(workspace.context._lock.acquire, workspace.context._lock.release)
            observed["workspace_free"] = available_to_another_thread(workspace._lock.acquire_write, workspace._lock.release_write)
            observed["wait"] = kwargs["wait"]
            raise RuntimeError("server startup failed")

    monkeypatch.setattr(workspace._mcp, "to_mcp", lambda **kwargs: FakeToolkit())

    with pytest.raises(RuntimeError, match="server startup failed"):
        workspace.serve(wait=True)

    assert observed == {"context_free": True, "workspace_free": True, "wait": True}


@pytest.mark.fast
def test_workspace_toolkit_rejects_duplicate_family_tool_names():
    """Two toolkit families must not silently overwrite the same MCP tool name."""
    from heavenbase.toolkit.families.assembly import workspace_tool_functions
    from heavenbase.toolkit.families.family import WorkspaceToolkitFamilySpec
    from heavenbase.toolkit.profile import ProfileSpec

    workspace = hb.HeavenBase("mcp-dup-tools-test", detached=True, backends={"main": {"type": "inmem"}})

    def build_one(context):
        return {"shared_tool": lambda: "one"}

    def build_two(context):
        return {"shared_tool": lambda: "two"}

    families = {
        "one": WorkspaceToolkitFamilySpec(
            name="family-one",
            tool_names=("shared_tool",),
            builder=build_one,
            meta={"builder_context": "workspace_tool_context"},
        ),
        "two": WorkspaceToolkitFamilySpec(
            name="family-two",
            tool_names=("shared_tool",),
            builder=build_two,
            meta={"builder_context": "workspace_tool_context"},
        ),
    }
    profile = ProfileSpec(name="dup", tools=["shared_tool"])
    with pytest.raises(ValueError, match="Duplicate MCP tool name 'shared_tool'.*family-one.*family-two"):
        workspace_tool_functions(workspace, profile, ["shared_tool"], families)


@pytest.mark.fast
def test_workspace_toolkit_projects_shared_builders_through_declared_family_tools():
    """Families sharing one builder should own only their declared tool names."""
    from heavenbase.toolkit.families.assembly import workspace_tool_functions
    from heavenbase.toolkit.families.family import WorkspaceToolkitFamilySpec
    from heavenbase.toolkit.profile import ProfileSpec

    workspace = hb.HeavenBase("mcp-shared-family-builder-test", detached=True, backends={"main": {"type": "inmem"}})

    def build_tools(context):
        return {"tool_one": lambda: "one", "tool_two": lambda: "two"}

    families = {
        "one": WorkspaceToolkitFamilySpec(
            name="family-one",
            tool_names=("tool_one",),
            builder=build_tools,
            meta={"builder_context": "workspace_tool_context"},
        ),
        "two": WorkspaceToolkitFamilySpec(
            name="family-two",
            tool_names=("tool_two",),
            builder=build_tools,
            meta={"builder_context": "workspace_tool_context"},
        ),
    }
    profile = ProfileSpec(name="shared-builder", tools=["tool_one", "tool_two"])

    tools = workspace_tool_functions(workspace, profile, ["tool_one", "tool_two"], families)

    assert tools["tool_one"]() == "one"
    assert tools["tool_two"]() == "two"


@pytest.mark.fast
def test_workspace_toolkit_does_not_materialize_unselected_family_tools():
    """An unrelated family builder should not run for a narrower tool selection."""
    from heavenbase.toolkit.families.assembly import workspace_tool_functions
    from heavenbase.toolkit.families.family import WorkspaceToolkitFamilySpec
    from heavenbase.toolkit.profile import ProfileSpec

    workspace = hb.HeavenBase("mcp-unselected-family-test", detached=True, backends={"main": {"type": "inmem"}})

    def unexpected_builder(context):
        raise AssertionError("unselected family builder ran")

    families = {
        "unselected": WorkspaceToolkitFamilySpec(
            name="family-unselected",
            tool_names=("unselected_tool",),
            builder=unexpected_builder,
            meta={"builder_context": "workspace_tool_context"},
        )
    }

    tools = workspace_tool_functions(workspace, ProfileSpec(name="selected", tools=["count"]), ["count"], families)

    assert "count" in tools


@pytest.mark.fast
def test_workspace_toolkit_rejects_missing_declared_family_tools():
    """A family builder must materialize every tool declared by its descriptor."""
    from heavenbase.toolkit.families.assembly import workspace_tool_functions
    from heavenbase.toolkit.families.family import WorkspaceToolkitFamilySpec
    from heavenbase.toolkit.profile import ProfileSpec

    workspace = hb.HeavenBase("mcp-missing-family-tool-test", detached=True, backends={"main": {"type": "inmem"}})
    families = {
        "missing": WorkspaceToolkitFamilySpec(
            name="family-missing",
            tool_names=("missing_tool",),
            builder=lambda context: {},
            meta={"builder_context": "workspace_tool_context"},
        )
    }

    with pytest.raises(ValueError, match="family-missing.*missing_tool"):
        workspace_tool_functions(workspace, ProfileSpec(name="missing", tools=["missing_tool"]), ["missing_tool"], families)


def _desc_probe_family_builder(_workspace):
    """Build docstring/overlay probe tools for MCP description tests."""

    def described_tool():
        """Return a described probe value."""
        return "ok"

    def overlay_tool():
        """Docstring should lose to family overlay metadata."""
        return "overlay"

    return {"described_tool": described_tool, "overlay_tool": overlay_tool}


@pytest.mark.fast
def test_workspace_toolkit_uses_family_and_docstring_tool_descriptions():
    """External family tools should prefer family overlays, then callable docstrings."""
    from heavenbase.toolkit import workspace_tools as wt

    tools = _desc_probe_family_builder(None)
    overlays = {"overlay_tool": "Family overlay description for overlay_tool."}
    assert wt._tool_description("described_tool", tools["described_tool"]) == "Return a described probe value."
    assert wt._tool_description("overlay_tool", tools["overlay_tool"], overlays=overlays) == "Family overlay description for overlay_tool."
    assert wt._tool_description("count", tools["described_tool"]) == "Return the row count for one entity type."

    register_workspace_toolkit_family(
        WorkspaceToolkitFamilySpec(
            "test-family-tool-descs",
            ("described_tool", "overlay_tool"),
            _desc_probe_family_builder,
            meta={"tool_descriptions": {"overlay_tool": "Family overlay description for overlay_tool."}},
        ),
        replace=True,
    )
    register_profile(ProfileSpec(name="test-family-tool-descs", tools=("described_tool", "overlay_tool")), replace=True)
    workspace = hb.HeavenBase("mcp-tool-desc-test", backends={"main": {"type": "inmem"}})
    toolkit = workspace.to_mcp(name="mcp-tool-desc-test", profile="test-family-tool-descs")

    assert toolkit.tools["described_tool"].description == "Return a described probe value."
    assert toolkit.tools["overlay_tool"].description == "Family overlay description for overlay_tool."


@pytest.mark.fast
def test_workspace_tool_description_overlay_precedence(monkeypatch):
    """Workspace-local family metadata should override its registered base."""
    from heavenbase.toolkit import workspace_tools as wt
    from heavenbase.toolkit.families import registry as family_registry
    from heavenbase.toolkit.families.family import WorkspaceToolkitFamilySpec

    def build_tools(_workspace):
        return {"overlay_priority_tool": lambda: "ok"}

    registered = WorkspaceToolkitFamilySpec(
        "registered-description-family",
        ("overlay_priority_tool",),
        build_tools,
        meta={"tool_descriptions": {"overlay_priority_tool": "Registered description."}},
    )
    workspace_overlay = WorkspaceToolkitFamilySpec(
        "workspace-description-family",
        ("overlay_priority_tool",),
        build_tools,
        meta={"tool_descriptions": {"overlay_priority_tool": "Workspace description."}},
    )
    monkeypatch.setattr(family_registry, "workspace_toolkit_family_specs", lambda **_kwargs: (registered,))

    descriptions = wt._family_tool_descriptions({"workspace": workspace_overlay})

    assert descriptions["overlay_priority_tool"] == "Workspace description."


@pytest.mark.fast
def test_workspace_tool_context_is_exported_from_toolkit_facade():
    """External adapters should import WorkspaceToolContext from heavenbase.toolkit."""
    import heavenbase.toolkit as toolkit_api

    assert toolkit_api.WorkspaceToolContext is _toolkit_export("families.workspace.context", "WorkspaceToolContext")
    assert "WorkspaceToolContext" in toolkit_api.__all__


@pytest.mark.fast
def test_public_tool_error_boundary_redacts_and_suppresses_implementation_errors():
    """External tool adapters should expose one safe error boundary."""
    import heavenbase.toolkit as toolkit_api

    with pytest.raises(RuntimeError, match=r"api_key=<redacted>") as captured:
        toolkit_api.raise_tool_error(ValueError("provider failed api_key=sk-private"))

    assert "sk-private" not in str(captured.value)
    assert captured.value.__cause__ is None
