"""An enabled extension is the only source of its entities' definitions."""

import copy

import pytest

import heavenbase as hb
from heavenbase.context import load_bootstrap
from heavenbase.utils.config import ConfigManager

pytestmark = pytest.mark.fast


def _contexts(root, count):
    """Open Contexts that share one durable SQLite system store, as separate processes do."""
    bootstrap = {
        "version": 1,
        "root": str(root),
        "workspace": "default",
        "backend": load_bootstrap(home_path=root / "missing-bootstrap.yaml").backend,
        "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
    }
    return [hb.Context.load(bootstrap, config=ConfigManager(root=str(root), setup=True)) for _ in range(count)]


def _sqlite(path):
    return {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(path)}}


def _note_definition(ws):
    exported = ws.to_manifest(include_system=True)["entities"]
    return copy.deepcopy(next(item for item in exported if item["entity_id"] == "memory-note"))


def _without_options(definition):
    return {key: copy.deepcopy(value) for key, value in definition.items() if key not in ("catalog", "gram")}


def _refused_changes(ws):
    """Try each change to memory-note; every one must be refused before it touches the registration."""
    note = _note_definition(ws)
    registered = ws.entities["memory-note"]
    extended = _without_options(note)
    extended["fields"]["reviewer_note"] = {"type": "short-text", "required": False}
    with pytest.raises(ValueError, match="defined by extension 'memory' and cannot be altered"):
        ws.alter_entity(extended, confirm="memory-note")
    with pytest.raises(ValueError, match="defined by extension 'memory'"):
        ws.register(extended, allow_replace=True)
    with pytest.raises(ValueError, match="defined by extension 'memory'"):
        ws.register(_without_options(note), catalog="none", allow_replace=True)
    with pytest.raises(ValueError, match="defined by extension 'memory' and cannot be described"):
        ws.describe("memory-note", desc="Reviewed notes")
    with pytest.raises(ValueError, match="defined by extension 'memory' and cannot be described"):
        ws.describe("memory-note", fields={"text": {"examples": ["Remember the milk"]}})
    # Tags and examples are metadata like descriptions: 0.1.2.2 accepted them and lost them on reopen.
    with pytest.raises(ValueError, match="defined by extension 'memory' and cannot be described"):
        ws.describe("memory-note", tags=["team-notes"])
    with pytest.raises(ValueError, match="defined by extension 'memory' and cannot be described"):
        ws.describe("memory-note", fields={"text": {"tags": ["team-notes"]}})
    tagged = copy.deepcopy(note)
    tagged["tags"] = [*tagged.get("tags", []), "team-notes"]
    with pytest.raises(ValueError, match="defined by extension 'memory'"):
        ws.register(tagged, allow_replace=True)
    renamed = copy.deepcopy(note)
    renamed["name"] = "TeamNote"  # the name is metadata too; the structure leaves it out
    with pytest.raises(ValueError, match="defined by extension 'memory'"):
        ws.register(renamed, allow_replace=True)
    # Calls that change nothing, as idempotent start-up code makes them, are accepted and keep the extension's class.
    assert ws.register(note) is registered
    assert ws.register(note, allow_replace=True) is registered
    assert ws.alter_entity(_without_options(note), confirm="memory-note") is registered
    text = registered.schema().get("text")
    assert ws.describe("memory-note", desc=registered.schema().desc, fields={"text": {"desc": text.desc, "tags": list(text.tags)}}) is registered
    assert ws.entities["memory-note"] is registered
    assert _note_definition(ws) == note


@pytest.mark.slow
def test_a_memory_workspace_round_trips_through_its_default_manifest(tmp_path):
    (context,) = _contexts(tmp_path / "root", 1)
    try:
        ws = hb.HeavenBase("ext-defs-round-trip", context=context, detached=True, backends=_sqlite(tmp_path / "source.db"))
        ws.enable_extension("memory")
        note = _note_definition(ws)
        _refused_changes(ws)
        manifest = ws.to_manifest()
        complete = ws.to_manifest(include_system=True)
        ws.release()
        assert "memory-note" not in {item["entity_id"] for item in manifest["entities"]}
        # The agent extension's entities derive Catalog fields through lambdas, which a definition cannot name.
        assert {"memory-note", "agent-session"} <= {item["entity_id"] for item in complete["entities"]}

        for index, data in enumerate((manifest, complete)):
            restored = hb.HeavenBase.from_manifest(data, context=context, backends=_sqlite(tmp_path / f"copy{index}.db"), detached=True)
            try:
                assert _note_definition(restored) == note
                assert restored.entity_options["memory-note"].catalog == "full"
            finally:
                restored.release()
    finally:
        context.close()


@pytest.mark.slow
def test_reopening_keeps_an_extension_entity_and_its_metaschema_rows(tmp_path):
    first, second = _contexts(tmp_path / "root", 2)
    try:
        ws = hb.HeavenBase("ext-defs-reopen", context=first, backends=_sqlite(tmp_path / "store.db"))
        ws.enable_extension("memory")
        note = _note_definition(ws)
        _refused_changes(ws)
        rows = set(ws._metaschema.stored_schema_rows("memory-note"))
        ws.release()

        reopened = hb.HeavenBase("ext-defs-reopen", context=second)
        try:
            assert _note_definition(reopened) == note
            assert set(reopened._metaschema.stored_schema_rows("memory-note")) == rows
            assert reopened.to_manifest()["entities"] == []
        finally:
            reopened.release()
    finally:
        first.close()
        second.close()


@pytest.mark.slow
def test_a_refused_import_registers_none_of_its_definitions(tmp_path):
    """A manifest that changes an extension entity is refused before any of its definitions reaches the store."""
    from heavenbase.workspace import WorkspaceSpec

    (context,) = _contexts(tmp_path / "root", 1)
    try:
        source = hb.HeavenBase("ext-defs-refused-source", context=context, detached=True, backends={"main": {"type": "inmem"}})
        source.enable_extension("memory")
        source.register({"entity_id": "aaa-thing", "fields": {"title": {"type": "short-text"}}})
        manifest = source.to_manifest(include_system=True)
        source.release()
        assert [item["entity_id"] for item in manifest["entities"]][0] == "aaa-thing"
        note = next(item for item in manifest["entities"] if item["entity_id"] == "memory-note")
        note["fields"]["reviewer_note"] = {"type": "short-text", "required": False}

        with pytest.raises(ValueError, match="defined by extension 'memory' and cannot be imported"):
            hb.HeavenBase.from_manifest(manifest, context=context, backends=_sqlite(tmp_path / "store.db"), allow_replace=True)
        with pytest.raises(KeyError):
            WorkspaceSpec.load("ext-defs-refused-source", context=context)
        store = hb.HeavenBase("ext-defs-refused-probe", context=context, detached=True, backends=_sqlite(tmp_path / "store.db"))
        try:
            assert store._metaschema.stored_options("aaa-thing") is None
        finally:
            store.release()
    finally:
        context.close()


@pytest.mark.slow
@pytest.mark.parametrize("entity_id", ["agent-session", "agent-message"])
def test_the_extension_class_loaded_under_another_root_is_its_own_definition(tmp_path, entity_id):
    """Extension code loads in a package named per Context root; its class from another root still changes nothing."""
    (context_a,) = _contexts(tmp_path / "root-a", 1)
    (context_b,) = _contexts(tmp_path / "root-b", 1)
    other = hb.HeavenBase("ext-defs-root-a", context=context_a, detached=True, backends={"main": {"type": "inmem"}})
    ws = hb.HeavenBase("ext-defs-root-b", context=context_b, detached=True, backends={"main": {"type": "inmem"}})
    try:
        other.enable_extension("agent")
        ws.enable_extension("agent")
        foreign, registered = other.entities[entity_id], ws.entities[entity_id]
        # The derive references name each Context's own package, so the exported definitions differ as text.
        assert foreign.__module__.partition(".")[0] != registered.__module__.partition(".")[0]
        definition = _without_options(next(item for item in ws.to_manifest(include_system=True)["entities"] if item["entity_id"] == entity_id))
        assert ws.register(foreign) is registered
        assert ws.register(foreign, allow_replace=True) is registered
        assert ws.alter_entity(foreign, confirm=entity_id) is registered
        assert ws.entities[entity_id] is registered
        assert _without_options(next(item for item in ws.to_manifest(include_system=True)["entities"] if item["entity_id"] == entity_id)) == definition
    finally:
        other.release()
        ws.release()
        context_a.close()
        context_b.close()


@pytest.mark.slow
@pytest.mark.parametrize("name", ["Message", "Session"])
def test_a_public_extension_facade_registers_in_a_context_with_its_own_root(tmp_path, name):
    """``hb.Message`` and ``hb.Session`` resolve in the process Context and register unchanged in any other root."""
    (context,) = _contexts(tmp_path / "root", 1)
    ws = hb.HeavenBase("ext-defs-facade", context=context, detached=True, backends={"main": {"type": "inmem"}})
    try:
        ws.enable_extension("agent")
        facade = getattr(hb, name)
        registered = ws.entities[facade.schema().entity_id]
        assert ws.register(facade) is registered
        if name == "Message":
            row = {"object_id": "m1", "session_id": "s1", "idx": 0, "role": "user", "content_text": "hello", "tool_call_count": 0, "payload": {}}
            ws.upsert(facade, row)
            assert ws.get("m1", entity="agent-message")["content_text"] == "hello"
    finally:
        ws.release()
        context.close()


def _hooked_workspace(workspace_id):
    """Return a detached workspace whose extension entities include lambda derives (agent) and compute hooks (database)."""
    ws = hb.HeavenBase(workspace_id, detached=True, backends={"main": {"type": "inmem"}})
    for extension in ("agent", "memory", "database"):
        ws.enable_extension(extension)
    return ws


_HOOKED_ENTITIES = ["memory-note", "agent-message", "agent-session", "db-table"]


@pytest.mark.slow
@pytest.mark.parametrize("entity_id", _HOOKED_ENTITIES)
def test_an_extension_entity_takes_back_its_exported_definition(entity_id):
    """The definition ``to_manifest(include_system=True)`` exports changes nothing, also for an entity with derive or compute hooks."""
    ws = _hooked_workspace("ext-defs-exported-again")
    try:
        registered = ws.entities[entity_id]
        exported = copy.deepcopy(next(item for item in ws.to_manifest(include_system=True)["entities"] if item["entity_id"] == entity_id))
        assert ws.register(exported) is registered
        assert ws.register(exported, allow_replace=True) is registered
        assert ws.register(_without_options(exported)) is registered
        assert ws.alter_entity(_without_options(exported), confirm=entity_id) is registered
        renamed = copy.deepcopy(exported)
        renamed["name"] = f"{renamed['name']}Renamed"
        with pytest.raises(ValueError, match="cannot be registered with a different definition"):
            ws.register(renamed, allow_replace=True)
        with pytest.raises(ValueError, match="cannot be altered"):
            ws.alter_entity(_without_options(renamed), confirm=entity_id)
        extended = _without_options(exported)
        extended["fields"]["reviewer_note"] = {"type": "short-text", "required": False}
        with pytest.raises(ValueError, match="cannot be registered with a different definition"):
            ws.register(extended, allow_replace=True)
        with pytest.raises(ValueError, match="cannot be altered"):
            ws.alter_entity(extended, confirm=entity_id)
        with pytest.raises(ValueError, match="cannot be registered with a different definition"):
            ws.register(_without_options(exported), catalog="none" if exported.get("catalog") != "none" else "full", allow_replace=True)
        assert ws.entities[entity_id] is registered
        assert next(item for item in ws.to_manifest(include_system=True)["entities"] if item["entity_id"] == entity_id) == exported
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("entity_id", _HOOKED_ENTITIES)
def test_an_import_skips_an_extension_definition_that_only_renames_it(entity_id):
    """The name is metadata, which an import leaves to the extension, whether or not the entity has hooks."""
    source = _hooked_workspace("ext-defs-renamed-source")
    manifest = copy.deepcopy(source.to_manifest(include_system=True))
    source.release()
    definition = next(item for item in manifest["entities"] if item["entity_id"] == entity_id)
    name = definition["name"]
    definition["name"] = f"{name}Renamed"
    manifest["id"] = "ext-defs-renamed-copy"
    restored = hb.HeavenBase.from_manifest(manifest, detached=True, backends={"main": {"type": "inmem"}})
    try:
        assert restored.entities[entity_id].schema().name == name
    finally:
        restored.release()
