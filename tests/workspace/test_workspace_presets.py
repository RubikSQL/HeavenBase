from __future__ import annotations

import pytest

import heavenbase as hb
from heavenbase.workspace.presets import WorkspacePresetEngine, list_workspace_presets, resolve_workspace_backends, resolve_workspace_preset
from heavenbase.database import DB_ENGINE


class PresetNote(hb.Entity):
    """Tiny entity used to verify preset-backed workspace storage."""

    identifier = "preset-note"

    title = hb.field(hb.ShortText)
    body = hb.field(hb.LongText)
    emb = hb.field(hb.Vector[2])


def test_workspace_debug_preset_boots_without_backend_dict():
    ws = hb.HeavenBase("preset-debug-test", detached=True, preset="debug")
    ws.register(PresetNote)
    ws.upsert(PresetNote, {"object_id": "n1", "title": "Hello", "body": "local smoke", "emb": [1.0, 0.0]})

    assert ws.preset == "debug"
    assert ws.backends.names() == ["main", "vec", "search"]
    assert ws.get("n1", entity=PresetNote)["title"] == "Hello"
    assert ws.query(PresetNote).near(PresetNote.emb, [1.0, 0.0], top_k=1).execute().ids == ["n1"]


def test_workspace_preset_resolution_lists_and_templates_ws_id():
    presets = list_workspace_presets()
    local = resolve_workspace_backends("local-lts", ws_id="agent-shop")
    web = resolve_workspace_backends("web-lts", ws_id="agent-shop")
    spec = resolve_workspace_preset("local-lts")

    assert {"debug", "local-lts", "web-lts"}.issubset(presets)
    assert local["main"]["type"] == "postgres"
    assert web["main"]["type"] == "supabase"
    assert web["vec"]["type"] == "pgvector"
    assert web["vec"]["shared_database"] is True
    assert web["vec"]["database"] == "postgres"
    assert local["main"]["superuser"]["username"] == "rubik"
    assert local["vec"]["uri"] == "file://%/workspaces/lance"
    assert spec.get("preset") == "local-lts"
    assert spec.get("backends.vec.uri") == "file://%/workspaces/lance"
    db_spec = DB_ENGINE.resolve({"provider": "postgres", **local["main"], "database": "agent_shop"})
    assert (
        DB_ENGINE.apply(db_spec, profile="superuser")["url"]
        .render_as_string(hide_password=False)
        .startswith("postgresql+psycopg2://rubik:password@localhost:5432/postgres")
    )


def test_workspace_preset_engine_applies_profiles_and_is_idempotent():
    engine = WorkspacePresetEngine()
    spec = engine.resolve({"preset": "local-lts"})
    data = engine.apply(spec, profile="preset", ws_id="agent-shop")

    assert data["preset"] == "local-lts"
    assert data["backends"]["vec"]["uri"] == "file://%/workspaces/lance"
    assert engine.assert_idempotent(spec) == spec
    assert engine.resolve(hb.ConfigSpec.from_dict(spec.to_dict())) == spec
    with pytest.raises(ValueError, match="Unknown config profile"):
        engine.apply({"preset": "debug"}, profile="unknown")


def test_workspace_preset_unknown_and_backend_override():
    with pytest.raises(Exception, match="preset"):
        resolve_workspace_preset("missing-preset")

    ws = hb.HeavenBase(
        "preset-override-test",
        detached=True,
        preset="debug",
        backends={"main": {"type": "inmem"}},
    )

    assert ws.preset is None
    assert ws.backends.names() == ["main"]


@pytest.mark.fast
def test_pgvector_dedicated_and_shared_database_namespace_contracts():
    """Pgvector keeps dedicated defaults while shared mode retains its configured database."""
    dedicated = hb.BackendType.build(
        "vec",
        {
            "type": "pgvector",
            "database": "application",
            "host": "localhost",
            "port": 5432,
            "username": "postgres",
            "password": "",
        },
        ws_id="workspace-one",
    )
    shared = hb.BackendType.build(
        "vec",
        {
            "type": "pgvector",
            "shared_database": True,
            "database": "postgres",
            "host": "localhost",
            "port": 5432,
            "username": "postgres",
            "password": "",
        },
        ws_id="workspace-one",
    )

    assert dedicated.shared_database is False
    assert dedicated.database == hb.backends.Backend._physical_name("workspace-one")
    assert dedicated.db.spec.database == hb.backends.Backend._physical_name("workspace-one")
    assert dedicated._table_name("catalog-entry") == hb.backends.Backend._physical_name("catalog-entry")
    assert dedicated.tag("workspace-database-namespace").value is True

    assert shared.shared_database is True
    assert shared.database == "postgres"
    assert shared.db.spec.database == "postgres"
    assert shared._table_name("catalog-entry") != dedicated._table_name("catalog-entry")
    assert shared.tag("workspace-database-namespace").value is False
    assert hb.DEFAULT_CONTEXT.modules().tag("backend_type", "pgvector", "workspace-database-namespace").value is True

    first_name = shared._table_name("catalog-entry")
    shared.bind_workspace("workspace-two")
    assert shared.database == "postgres"
    assert shared.db.spec.database == "postgres"
    assert shared._table_name("catalog-entry") != first_name


@pytest.mark.fast
def test_pgvector_shared_drop_is_scoped_to_one_backend_namespace():
    """Dropping shared pgvector storage cannot remove a sibling workspace's tables."""
    first = hb.backends.PgVectorBackend("vec", database="postgres", ws_id="workspace-one", shared_database=True)
    second = hb.backends.PgVectorBackend("vec", database="postgres", ws_id="workspace-two", shared_database=True)
    first_main = first._table_name("catalog-entry")
    first_side = first._side_table_name("catalog-entry", "labels")
    second_main = second._table_name("catalog-entry")

    class FakeDatabase:
        def __init__(self) -> None:
            self.dropped: list[str] = []
            self.closed = False

        def tables(self) -> list[str]:
            return [first_main, first_side, second_main, "unrelated"]

        def drop_table(self, name: str) -> None:
            self.dropped.append(name)

        def close(self) -> None:
            self.closed = True

    fake = FakeDatabase()
    first.db = fake
    first.drop()

    assert set(fake.dropped) == {first_main, first_side}
    assert second_main not in fake.dropped
    assert "unrelated" not in fake.dropped
    assert fake.closed is True
    assert first.db is None
