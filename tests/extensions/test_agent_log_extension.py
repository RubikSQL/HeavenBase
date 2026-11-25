"""The optional agent-log extension: AgentTrace rows exist only where enabled."""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3

import pytest

import heavenbase as hb
import heavenbase.extensions.agent as agent_package
import heavenbase.extensions.agent_log as agent_log_package
from heavenbase.extensions.agent_log import AGENT_LOG_EXTENSION_ID
from heavenbase.utils import path_to_file_uri
from heavenbase.workspace.registry import WorkspaceSpec

pytestmark = pytest.mark.fast

# The agent extension's entity set as 0.1.2.2 shipped it. A reopened 0.1.2.2
# workspace replays exactly this set, so it must not grow.
_AGENT_ENTITIES_0122 = {"agent-message", "agent-session", "agent-skill", "agent-agent"}


def _tables(path) -> set:
    with sqlite3.connect(path) as conn:
        return {str(row[0]).lower() for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_agent_trace_lives_in_its_own_non_default_extension():
    AgentTrace = agent_log_package.AgentTrace
    agent = hb.ext.Extension.load("agent")
    agent_log = hb.ext.Extension.load(AGENT_LOG_EXTENSION_ID)

    assert set(agent.entity_ids()) == _AGENT_ENTITIES_0122  # the default agent extension is back to its 0.1.2.2 shape
    assert agent_log.identifier == "agent-log" and agent_log.entity_ids() == ["agent-trace"]
    assert agent_log.requires == ("agent",) and agent_log.required is False
    assert AGENT_LOG_EXTENSION_ID not in (hb.CM_HVNB.get("heavenbase.extensions.default", default=[]) or [])
    assert AgentTrace.identifier == "agent-trace" and AgentTrace.__name__ == "AgentTrace"
    assert {"created_at", "updated_at"}.isdisjoint(AgentTrace.schema().fields)  # Catalog owns row timestamps
    assert not hasattr(AgentTrace, "save")


def test_agent_trace_adds_no_top_level_or_agent_package_name():
    assert "Trace" not in hb.__all__ and not hasattr(hb, "Trace")
    assert "AgentTrace" not in hb.__all__  # the session-log row type stays in its extension package
    assert "Trace" not in agent_package.__all__ and not hasattr(agent_package, "Trace")


def test_canonical_tags_documents_workspace_opt_in():
    root = Path(__file__).resolve().parents[2]
    tags = (root / "docs/resources/architecture/canonical-tags.md").read_text(encoding="utf-8")
    [entry] = [line for line in tags.splitlines() if "extensions/agent_log/meta.yaml" in line]
    assert f'ws.enable_extension("{AGENT_LOG_EXTENSION_ID}")' in entry


@pytest.mark.slow
def test_default_workspaces_never_gain_the_trace_table_on_create_or_reopen(tmp_path):
    AgentTrace = agent_log_package.AgentTrace
    name = "agent-log-default-" + (re.sub(r"[^a-z0-9]", "", tmp_path.name.lower())[-8:] or "x")
    database = tmp_path / "data.db"
    backends = {"main": {"type": "sqlite", "name": "main", "database": path_to_file_uri(database)}}
    try:
        created = hb.HeavenBase(name, backends=backends)
        assert "agent" in created.extensions() and AGENT_LOG_EXTENSION_ID not in created.extensions()
        assert _AGENT_ENTITIES_0122 <= set(created.entities) and "agent-trace" not in created.entities
        assert "agent-trace" not in created.list_entities()

        reopened = hb.HeavenBase(name)  # replays the stored 0.1.2.2-shaped extension roots
        assert AGENT_LOG_EXTENSION_ID not in reopened.extensions() and "agent-trace" not in reopened.entities
        assert not any("trace" in table for table in _tables(database))  # opening never creates the table

        reopened.enable_extension(AGENT_LOG_EXTENSION_ID)  # the explicit opt-in is the only way in
        assert "agent-trace" in reopened.entities and reopened.entities["agent-trace"].__name__ == "AgentTrace"
        reopened.upsert("agent-trace", AgentTrace.event("conv", "turn", 0, "turn", payload={"text": "hi"}))
        assert any("trace" in table for table in _tables(database))

        again = hb.HeavenBase(name)
        assert AGENT_LOG_EXTENSION_ID in again.extensions()  # the opt-in is a durable root
        assert again.get(AgentTrace.event_id("conv", "turn", 0, "turn"), entity="agent-trace")["payload"] == {"text": "hi"}
    finally:
        try:
            hb.HeavenBase.drop(name)
        finally:
            WorkspaceSpec.unregister(name)
