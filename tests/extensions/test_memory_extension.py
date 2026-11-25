from __future__ import annotations

from types import SimpleNamespace

import pytest

import heavenbase as hb
from heavenbase.extensions.memory.extension import ensure_memory_entity
from heavenbase.extensions.memory.toolkit._utils import default_source, list_limit, search_limit
from heavenbase.utils import hash_id
from heavenbase.utils.config import ConfigManager

_EPOCH = 1_700_000_000


def _ws(name: str):
    return hb.HeavenBase(name, detached=True, backends={"main": {"type": "inmem"}})


def _note(index: int, **values):
    row = {
        "object_id": f"note-{index:04d}",
        "text": f"password rotation note {index}",
        "created_at": _EPOCH + index,
        "updated_at": _EPOCH + index,
    }
    row.update(values)
    return row


def _context(root):
    config = ConfigManager(default={}, setup=False)
    return hb.Context.load(
        {
            "version": 1,
            "root": str(root),
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


def _extension_rows(ws):
    return [row for row in ws.query(hb.MetaSchema).where({"kind": "extension"}).execute().rows() if not row["object_id"].startswith("registration:")]


def test_memory_extension_registers_optional_entity():
    ws = _ws("memory-extension-test")

    assert "memory" in hb.ext.Extension.identifiers()
    assert "memory" not in ws.extensions()
    assert "memory-note" not in ws.entities
    assert not hasattr(ws, "memory")

    spec = ws.enable_extension("memory")

    assert spec.identifier == "memory"
    assert "memory-note" in ws.entities
    assert callable(ws.memory.to_mcp)
    assert "memory-note" in ws.list_entities()
    row = next(row for row in _extension_rows(ws) if row["subject_id"] == "memory")
    assert row["name"] == "Memory"
    assert row["meta"]["entities"] == ["memory-note"]


def test_memory_note_entity_fields():
    fields = set(hb.MemoryNote.schema().fields)
    assert {"text", "title", "tags", "metadata", "source", "project_id", "keypath", "version", "previous_id", "deleted", "created_at", "updated_at"} <= fields


def test_memory_runtime_defaults_accept_context_config_without_changing_registered_identity():
    config = ConfigManager(default={}, setup=False)
    config.set("heavenbase.mcp.memory.default_source", "context-agent")
    config.set("heavenbase.mcp.memory.list_limit", 7)
    config.set("heavenbase.mcp.memory.search_limit", 3)
    workspace = SimpleNamespace(context=SimpleNamespace(config=config), extensions=lambda: ["memory"])

    assert ensure_memory_entity(workspace) == "memory-note"
    assert default_source(config=config) == "context-agent"
    assert list_limit(config=config) == 7
    assert search_limit(config=config) == 3


def test_mcp_memory_profile_requires_explicit_extension_activation():
    ws = _ws("memory-mcp-explicit-enable")
    assert "memory" not in ws.extensions()
    ws.enable_extension("toolkit")

    with pytest.raises(ValueError, match="Unsupported MCP workspace profile 'memory'"):
        ws.to_mcp(profile="memory")

    ws.enable_extension("memory")
    toolkit = ws.to_mcp(profile="memory")
    toolkit.run("remember", key="note-1", text="hello memory")

    assert "memory" in ws.extensions()
    assert "memory-note" in ws.entities
    assert ws.get("note-1", entity="memory-note")["text"] == "hello memory"


def test_memory_remember_generated_id_uses_workspace_context_hash_length(tmp_path):
    context = _context(tmp_path)
    workspace = None
    try:
        workspace_id = "memory-context-id"
        scope = f"{context.config.base_scope}.{workspace_id}"
        context.config.set("heavenbase.entity.id.hash_length", 53, scope=scope)
        workspace = hb.HeavenBase(workspace_id, context=context, backends={"main": {"type": "inmem"}})
        workspace.enable_extension("toolkit")
        workspace.enable_extension("memory")
        toolkit = workspace.to_mcp(profile="memory")

        remembered = toolkit.run("remember", text="context-owned identifier")

        assert len(remembered["object_id"]) == 53
        assert remembered["row"]["object_id"] == remembered["object_id"]
        assert context.config.scope == context.config.base_scope
    finally:
        if workspace is not None:
            workspace.release()
        context.close()


def test_mcp_memstate_profile_requires_explicit_extension_activation():
    ws = _ws("memstate-mcp-explicit-enable")
    assert "memory" not in ws.extensions()
    ws.enable_extension("toolkit")

    with pytest.raises(ValueError, match="Unsupported MCP workspace profile 'memstate'"):
        ws.to_mcp(profile="memstate")

    ws.enable_extension("memory")
    toolkit = ws.to_mcp(profile="memstate")
    result = toolkit.run("memstate_set", project_id="demo", keypath="prefs.theme", value="dark")

    assert "memory" in ws.extensions()
    assert result["version"] == 1
    assert result["row"]["text"] == "dark"


def test_memory_api_exposes_memory_owned_mcp_profiles():
    ws = _ws("memory-api-profiles")
    ws.enable_extension("toolkit")
    ws.enable_extension("memory")

    memory = ws.memory.to_mcp(profile="memory")
    memory.run("remember", key="note-1", text="extension-owned memory")
    memstate = ws.memory.to_mcp(profile="memstate")
    result = memstate.run("memstate_set", project_id="demo", keypath="prefs.theme", value="dark")

    assert ws.get("note-1", entity="memory-note")["text"] == "extension-owned memory"
    assert result["version"] == 1


def test_memory_api_rejects_non_memory_profiles():
    ws = _ws("memory-api-profile-reject")
    ws.enable_extension("toolkit")
    ws.enable_extension("memory")

    with pytest.raises(ValueError, match="memory MCP profile"):
        ws.memory.to_mcp(profile="agent")


def test_memstate_version_chain_and_soft_delete():
    ws = _ws("memory-version-chain")
    ws.enable_extension("memory")
    versions = ws.memory.versions("proj")

    first = versions.append(
        "docs.readme",
        text="v1",
        title="Readme",
    )
    second = versions.append(
        "docs.readme",
        text="v2",
        title="Readme",
    )
    deleted = versions.append(
        "docs.readme",
        text="",
        title="Deleted Readme",
        deleted=True,
    )

    history = versions.history("docs.readme")
    current = versions.current("docs.readme")

    assert first["version"] == 1
    assert second["version"] == 2
    assert second["conflicts"][0]["text"] == "v1"
    assert deleted["version"] == 3
    assert deleted["row"]["deleted"] is True
    assert [row["version"] for row in history] == [1, 2, 3]
    assert current == []


def test_memory_versions_is_the_project_bound_public_protocol():
    ws = _ws("memory-version-protocol")
    ws.enable_extension("memory")

    versions = ws.memory.versions("My Project")
    assert versions.project_id == "my.project"
    assert versions.prefix == "project.my.project"
    assert versions.keypath("Docs/README") == "project.my.project.docs.readme"
    assert versions.title("docs.readme") == "Readme"

    versions.append("docs.readme", "Architecture notes", tags=["design"])
    versions.append("prefs.theme", "dark")
    ws.memory.versions("My Project Extra").append("docs.readme", "Other project")

    assert [row["keypath"] for row in versions.current("docs")] == ["project.my.project.docs.readme"]
    assert [row["keypath"] for row in versions.search("architecture design")] == ["project.my.project.docs.readme"]


def test_memory_reads_report_the_total_their_limit_withheld():
    ws = _ws("memory-truncation-disclosure")
    ws.enable_extension("toolkit")
    ws.enable_extension("memory")
    ws.upsert_many("memory-note", [_note(index) for index in range(25)])
    toolkit = ws.to_mcp(profile="memory")

    listed = toolkit.run("list_memory")
    searched = toolkit.run("search_memory", text="password")
    whole = toolkit.run("list_memory", limit=25)

    assert (listed["n"], listed["total"], listed["truncated"]) == (list_limit(config=ws.context.config), 25, True)
    assert (searched["n"], searched["total"], searched["truncated"]) == (search_limit(config=ws.context.config), 25, True)
    assert whole["n"] == 25 and "total" not in whole and "truncated" not in whole


def test_memstate_reads_report_the_total_their_limit_withheld():
    ws = _ws("memstate-truncation-disclosure")
    ws.enable_extension("toolkit")
    ws.enable_extension("memory")
    versions = ws.memory.versions("proj")
    for index in range(25):
        versions.append(f"keys.k{index:02d}", f"deploy value {index}")
    toolkit = ws.to_mcp(profile="memstate")

    browsed = toolkit.run("memstate_get", project_id="proj")
    found = toolkit.run("memstate_search", query="deploy", project_id="proj")
    whole = toolkit.run("memstate_get", project_id="proj", limit=25)

    assert (browsed["n"], browsed["limit"], browsed["total"], browsed["truncated"]) == (20, 20, 25, True)
    assert (found["n"], found["limit"], found["total"], found["truncated"]) == (10, 10, 25, True)
    assert (whole["n"], whole["total"], whole["truncated"]) == (25, 25, False)


def test_memory_versions_read_the_whole_project_past_the_old_scan_cap():
    ws = _ws("memory-version-coverage")
    ws.enable_extension("toolkit")
    ws.enable_extension("memory")
    bulk = [_note(index, project_id="noise", keypath=f"project.noise.k{index:05d}", text=f"noise {index}") for index in range(10_050)]
    ws.upsert_many("memory-note", bulk)
    versions = ws.memory.versions("small")
    for text in ("v1", "v2", "v3"):
        versions.append("docs.readme", text)
    toolkit = ws.to_mcp(profile="memstate")

    history = toolkit.run("memstate_history", project_id="small", keypath="docs.readme")

    assert len(ws.memory.versions("noise").rows()) == 10_050  # no hidden entity-wide scan cap
    assert [row["version"] for row in versions.history("docs.readme")] == [1, 2, 3]
    assert history["n"] == 3


def test_memstate_append_refuses_to_overwrite_a_version_its_chain_read_missed():
    ws = _ws("memory-version-collision")
    ws.enable_extension("memory")
    versions = ws.memory.versions("proj")
    target = versions.keypath("docs.readme")
    ws.upsert(
        "memory-note",
        _note(0, object_id=f"mem-{hash_id('proj', target, '1', length=32)}", project_id="other", keypath=target, text="unseen v1"),
    )

    with pytest.raises(ValueError, match="exists"):
        versions.append("docs.readme", "v1")


def test_workspace_manifest_roundtrip_with_memory_extension(tmp_path):
    ws = _ws("memory-manifest-source")
    ws.enable_extension("memory")

    data = ws.to_manifest()
    assert "memory" in data["extensions"]
    assert "memory-note" in ws.entities

    path = tmp_path / "memory-workspace.yaml"
    hb.WorkspaceManifest.from_dict(data).save(str(path))
    clone = hb.HeavenBase.from_manifest(hb.WorkspaceManifest.load(str(path)), detached=True)

    assert "memory" in clone.extensions()
    assert "memory-note" in clone.entities
