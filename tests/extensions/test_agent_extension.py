from __future__ import annotations

import heavenbase as hb
from heavenbase.utils.config import ConfigManager


def make_skill_dir(tmp_path):
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill\ntoolkit: demo/math-tools:1\n---\n# Demo\nUse the guide.\n",
        encoding="utf-8",
    )
    (skill_dir / "guide.md").write_text("Guide body.", encoding="utf-8")
    return skill_dir


def _ws(name: str):
    return hb.HeavenBase(name, detached=True, backends={"main": {"type": "inmem"}})


def _context(tmp_path):
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
        config=ConfigManager(default={}, setup=False),
    )


def test_agent_extension_registers_optional_entities():
    ws = _ws("agent-extension-test")

    assert "agent" in hb.ext.Extension.identifiers()
    assert "agent" in ws.extensions()
    assert {"agent-message", "agent-session", "agent-skill", "agent-agent"} <= set(ws.entities)

    spec = ws.enable_extension("agent")

    assert spec.identifier == "agent"
    assert {"agent-message", "agent-session", "agent-skill", "agent-agent"} <= set(ws.entities)


def test_public_agent_entities_cross_context_artifact_generations(tmp_path):
    """Public Agent entities should remain compatible with a workspace's restored generation."""
    PublicPrompt = hb.Prompt
    PublicSkill = hb.Skill
    PublicAgent = hb.Agent
    context = _context(tmp_path / "context")
    ws = hb.HeavenBase("agent-cross-context", context=context, backends={"main": {"type": "inmem"}})
    try:
        ws.enable_extension("agent")
        assert PublicSkill is not ws.entities[PublicSkill.identifier]
        assert PublicAgent is not ws.entities[PublicAgent.identifier]

        PublicPrompt("Use the registered skill.", name="cross.context.prompt", ws=ws).register()
        skill = PublicSkill.from_path(str(make_skill_dir(tmp_path)))
        skill.register(ws=ws)
        PublicAgent(
            name="cross-context-agent",
            llm_preset="mock",
            prompt_ref="cross.context.prompt",
            skill_refs=[skill.name],
            ws=ws,
        ).register()

        assert PublicPrompt.load("cross.context.prompt", ws=ws)() == "Use the registered skill."
        assert PublicSkill.load("demo-skill", ws=ws).name == "demo-skill"
        assert PublicAgent.load("cross-context-agent", ws=ws).name == "cross-context-agent"
    finally:
        ws.release()
        context.close()


def test_artifact_callable_structure_refs_ignore_generation_but_keep_source_changes():
    """Artifact namespace churn must not hide actual source-backed callable changes."""
    ws = _ws("artifact-callable-structure")

    def first(value):
        return value

    def changed(value):
        return f"changed:{value}"

    namespace_a = f"_heavenbase_artifact_{'a' * 64}.sample"
    namespace_b = f"_heavenbase_artifact_{'b' * 64}.sample"
    first.__qualname__ = "Example.compute"
    first.__module__ = namespace_a
    original_ref = ws._schema_metadata.callable_structure_ref(first)
    first.__module__ = namespace_b
    restored_ref = ws._schema_metadata.callable_structure_ref(first)
    changed.__qualname__ = "Example.compute"
    changed.__module__ = namespace_b

    assert original_ref == restored_ref
    assert original_ref != ws._schema_metadata.callable_structure_ref(changed)
    assert restored_ref.startswith("artifact:sample.Example.compute:")
    assert ws._schema_metadata.callable_ref(first) == f"{namespace_b}.Example.compute"


def test_agent_without_toolkit_refs_does_not_require_toolkit_extension():
    old_value = hb.CM_HVNB.get("heavenbase.extensions.default", default=None)
    try:
        hb.CM_HVNB.set("heavenbase.extensions.default", [])
        ws = _ws("agent-without-toolkit-test")
        ws.enable_extension("agent")

        runtime = hb.Agent(name="plain-agent", llm_preset="mock", ws=ws).build()

        assert sorted(ws.extensions()) == ["agent", "capsule"]
        assert runtime.toolkits == []
        assert runtime.session.tools == []
    finally:
        if old_value is None:
            hb.CM_HVNB.unset("heavenbase.extensions.default")
        else:
            hb.CM_HVNB.set("heavenbase.extensions.default", old_value)


def test_agent_entities_leave_catalog_timestamps_to_catalog():
    for entity in (hb.Message, hb.Session, hb.Skill, hb.Agent):
        fields = set(entity.schema().fields)
        assert "created_at" not in fields
        assert "updated_at" not in fields


def test_agent_row_persistence_uses_register_or_workspace_upsert():
    assert not hasattr(hb.Message, "save")
    assert not hasattr(hb.Session, "save")
    assert not hasattr(hb.Skill, "save")
    assert not hasattr(hb.Agent, "save")
    assert hasattr(hb.Skill, "register")
    assert hasattr(hb.Agent, "register")


def test_message_wraps_openai_payload_without_runtime_conversion():
    payload = {"role": "assistant", "content": "ok", "tool_calls": [{"id": "c1"}]}

    msg = hb.Message.wrap(payload, session_id="s1", idx=2)

    assert msg.payload == payload
    assert msg.role == "assistant"
    assert msg.content_text == "ok"
    assert msg.tool_call_count == 1


def test_session_appends_openai_payloads_without_conversion():
    ws = _ws("agent-log-test")
    ws.enable_extension("agent")
    session = hb.Session(title="Run", ws=ws)
    ws.upsert(hb.Session, session.to_dict())

    written = session.append_messages(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "ok", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "name": "lookup", "content": "done"},
        ],
        source="hb llm chat",
        usage={"completion_tokens": 2},
    )

    assert [message.payload for message in written] == session.messages()
    loaded = hb.Session.load(session.object_id, ws=ws)
    assert loaded.tool_call_count == 1
    assert loaded.turn_count == 1
    assert loaded.usage == {"completion_tokens": 2}


def test_open_mcp_call_log_stores_toolkit_exposure_metadata():
    ws = _ws("agent-mcp-call-log-test")
    ws.enable_extension("toolkit")
    ws.enable_extension("agent")

    from heavenbase.extensions.contributions import resolve_contribution

    open_mcp_call_log = resolve_contribution(
        "mcp_call_logger",
        "agent",
        resolver=ws.module_resolver,
        workspace=ws,
        callable_only=True,
    )
    session = open_mcp_call_log(ws, toolkit_name="demo-mcp", profile="agent")

    loaded = hb.Session.load(session.object_id, ws=ws)

    assert loaded.title == "demo-mcp"
    assert loaded.agent_ref == "demo-mcp"
    assert loaded.state == {
        "source": "mcp",
        "scope": "toolkit-exposure",
        "toolkit": "demo-mcp",
        "profile": "agent",
    }


def test_session_persists_ordered_messages():
    ws = _ws("agent-session-test")
    ws.enable_extension("agent")

    session = hb.Session(title="Demo", ws=ws)
    session.append({"role": "user", "content": "hello"})
    session.append({"role": "assistant", "content": "hi"})

    loaded = hb.Session.load(session.object_id, ws=ws)

    assert loaded.title == "Demo"
    assert [message["role"] for message in loaded.messages()] == ["user", "assistant"]
    llm_session = loaded.to_llm_session()
    assert llm_session.messages == loaded.messages()
    assert llm_session.llm.spec.resolver is ws.module_resolver
    assert llm_session.llm.engine.manager is ws.context.config


def test_session_set_messages_replaces_rows_and_aggregates():
    ws = _ws("agent-session-set-test")
    ws.enable_extension("agent")
    session = hb.Session(title="Demo", ws=ws)
    session.append_messages(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "tool", "tool_calls": [{"id": "call-1"}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "done"},
        ],
        usage={"total_tokens": 12},
    )

    rows = session.set_messages([{"role": "user", "content": "kept"}], source="test")
    loaded = hb.Session.load(session.object_id, ws=ws)

    assert [row.payload for row in rows] == [{"role": "user", "content": "kept"}]
    assert loaded.messages() == [{"role": "user", "content": "kept"}]
    assert loaded.turn_count == 1
    assert loaded.tool_call_count == 0
    assert loaded.usage == {}
    assert [row["payload"] for row in ws.rows(hb.Message)] == [{"role": "user", "content": "kept"}]


def test_skill_from_path_stores_artifact_archive(tmp_path):
    skill = hb.Skill.from_path(str(make_skill_dir(tmp_path)))

    assert skill.name == "demo-skill"
    assert skill.description == "Demo skill"
    assert skill.toolkit_refs == ["demo/math-tools:1"]
    assert isinstance(skill.archive, bytes)
    assert "guide.md" in skill.file_index


def test_skill_read_tool_loads_skill_content(tmp_path):
    ws = _ws("agent-skill-tool-test")
    ws.enable_extension("agent")
    skill = hb.Skill.from_path(str(make_skill_dir(tmp_path)))
    skill.register(ws=ws)

    toolkit = hb.Skill.toolkit([skill], ws=ws)
    text = toolkit.run("read_skill", skill_name="demo-skill", path="SKILL.md")

    assert "Loading Skill: demo-skill" in text
    assert "SKILL.md" in text
    assert "Use the guide." in text


def test_agent_loads_prompt_skills_toolkits_and_llm(tmp_path):
    ws = _ws("agent-lego-test")
    ws.enable_extension("agent")
    hb.Prompt("You are concise.", name="demo.agent.system", ws=ws).register()
    skill = hb.Skill.from_path(str(make_skill_dir(tmp_path)))
    skill.toolkit_refs = []
    skill._data["toolkit_refs"] = []
    skill.register(ws=ws)

    agent = hb.Agent(name="demo-agent", llm_preset="mock", prompt_ref="demo.agent.system", skill_refs=[skill.name], args={"max_steps": 3}, ws=ws)
    agent.register()

    runtime = hb.Agent.load("demo-agent", ws=ws).build()

    assert runtime.llm.spec.preset == "mock"
    assert runtime.llm.spec.resolver is ws.module_resolver
    assert runtime.llm.engine.manager is ws.context.config
    assert runtime.session.messages == [{"role": "system", "content": "You are concise."}]
    assert "read_skill" in runtime.session.list_tools()
    assert runtime.session.tools[0]._resolver is ws.module_resolver
    assert runtime.session.tools[0]._config is ws.context.config


def test_skill_from_path_and_register_use_workspace_entity_class(tmp_path):
    """Ambient hb.Skill must register through the workspace's agent-skill class."""
    ws = _ws("agent-skill-context-test")
    ws.enable_extension("agent")
    canonical = ws.entities["agent-skill"]

    skill = hb.Skill.from_path(str(make_skill_dir(tmp_path)), ws=ws)
    assert type(skill) is canonical

    ambient_root = tmp_path / "ambient"
    ambient_root.mkdir()
    ambient = hb.Skill.from_path(str(make_skill_dir(ambient_root)))
    ambient.register(ws=ws)
    loaded = hb.Skill.load("demo-skill", ws=ws)
    assert type(loaded) is canonical
