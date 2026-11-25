"""End-to-end external Extension release-parity contract."""

from __future__ import annotations

import pytest

import heavenbase as hb
from heavenbase.context import load_bootstrap
from heavenbase.utils import get_file_dir, loads_jstr, path_to_file_uri, pj, save_txt, save_yaml, sha256hash


def _bootstrap(root: str) -> dict:
    packaged = load_bootstrap(home_path=pj(root, "missing-bootstrap.yaml"))
    return {
        "version": 1,
        "root": root,
        "workspace": "default",
        "backend": packaged.backend,
        "registry": packaged.registry,
    }


@pytest.mark.fast
@pytest.mark.anyio
async def test_external_extension_reaches_workspace_api_skill_fastmcp_and_stdio(tmp_path, home_env):
    """One external folder should survive the complete supported runtime path."""
    Client = pytest.importorskip("fastmcp").Client
    PythonStdioTransport = pytest.importorskip("fastmcp.client.transports").PythonStdioTransport

    module = pj(get_file_dir(__file__), "..", "fixtures", "external_extension", abs=True)
    bootstrap = _bootstrap(pj(str(tmp_path), "context"))
    home = pj(str(tmp_path), "home")
    save_yaml(bootstrap, pj(home, ".heavenbase", "bootstrap.yaml"))
    context = hb.Context.load(bootstrap)
    service = context.modules()
    workspace_id = f"external-parity-{sha256hash(str(tmp_path))[:12]}"
    receipt = None
    workspace = None
    try:
        service.setup()
        receipt = service.install(module)
        extension_row = service.inspect("extension", "external-parity")[0]
        assert extension_row["record"]["source"] == "inline"
        assert extension_row["groups"]["tag"] == ["external", "test"]

        workspace = hb.HeavenBase(
            workspace_id,
            backends={
                "main": {
                    "type": "sqlite",
                    "name": "main",
                    "database": path_to_file_uri(pj(str(tmp_path), "external-parity.db")),
                }
            },
            context=context,
        )
        workspace.enable_extension("external-parity")
        note_id = workspace.external_parity.add("Installed")
        skill_id = hb.Skill.from_path(pj(module, "skill"), ws=workspace).register(ws=workspace)

        assert note_id == "note-installed"
        assert skill_id
        assert workspace.external_parity.resolver is workspace.module_resolver
        assert workspace.entities["external-parity-note"].__module__.startswith("_heavenbase_artifact_")

        toolkit = workspace.to_mcp(profile="external-parity")
        assert set(toolkit.list_tools()) == {"external_list_notes", "list_skills", "read_skill"}
        assert toolkit.run("external_list_notes")[0]["name"] == "Installed"
        skill = toolkit.run("read_skill", skill_name="external-parity", path="SKILL.md")
        assert "External parity" in skill["text"]

        async with Client(toolkit.to_fastmcp()) as client:
            listed = await client.call_tool("external_list_notes", {})
            assert loads_jstr(listed.content[0].text)[0]["name"] == "Installed"

        script = pj(str(tmp_path), "external_stdio.py")
        save_txt(
            f"""import heavenbase as hb

workspace = hb.HeavenBase.load({workspace_id!r})
workspace.serve(profile="external-parity", transport="stdio")
""",
            script,
        )
        async with Client(PythonStdioTransport(script, env=home_env(home), keep_alive=False)) as client:
            listed = await client.call_tool("external_list_notes", {})
            assert loads_jstr(listed.content[0].text)[0]["name"] == "Installed"
            skill = await client.call_tool("read_skill", {"skill_name": "external-parity", "path": "SKILL.md"})
            assert "External parity" in skill.content[0].text
    finally:
        try:
            if workspace is not None:
                workspace.drop()
        finally:
            try:
                if receipt is not None:
                    service.uninstall(receipt)
                    assert service.inspect("extension", "external-parity") == ()
            finally:
                context.close()
