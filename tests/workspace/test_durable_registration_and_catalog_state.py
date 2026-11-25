"""Regression tests for durable workspace registration and Catalog state."""

import heavenbase as hb
import pytest
from heavenbase.context.bootstrap import load_bootstrap
from heavenbase.utils.config import ConfigManager
from heavenbase.workspace import EntityOptions, WorkspaceSpec
from heavenbase.workspace.catalog import catalog_row_id


class RecoveryNote(hb.Entity):
    identifier = "recovery-note"

    name = hb.field(hb.ShortText)
    keywords = hb.field(hb.Array[hb.ShortText]).default([])


class ExplicitGramNote(hb.Entity):
    identifier = "explicit-gram-note"

    name = hb.field(hb.ShortText)
    keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))


def _sqlite_backend(path):
    return {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(path)}


def _context(root):
    return hb.Context.load(
        {
            "version": 1,
            "root": str(root),
            "workspace": "default",
            "backend": load_bootstrap(home_path=root / "missing-bootstrap.yaml").backend,
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=ConfigManager(root=str(root), setup=True),
    )


def _catalog_id(workspace):
    return catalog_row_id(workspace.id, "recovery-note", "n1", hb.Catalog.schema().get("object_id").dtype.max_chars)


@pytest.mark.slow
@pytest.mark.parametrize("previous,target", [("identity", "full"), ("full", "identity")])
def test_reopen_after_a_failed_catalog_rewrite_and_repair(tmp_path, monkeypatch, previous, target):
    context = _context(tmp_path)
    identifier = "recovery-catalog"
    manifest = {
        "kind": "heavenbase.workspace.manifest",
        "version": 2,
        "id": identifier,
        "construction": {"kind": "backends", "config": {"main": _sqlite_backend(tmp_path / "workspace.db")}},
        "entities": [{"entity_id": "recovery-note", "fields": {"name": {"type": "short-text"}}, "catalog": previous}],
    }
    try:
        workspace = hb.HeavenBase.from_manifest(manifest, context=context)
        workspace.upsert("recovery-note", {"object_id": "n1", "name": "Named"})
        backend = workspace.backends.get("main")

        def fail_write(*args, **kwargs):
            raise OSError("Catalog write failed")

        with monkeypatch.context() as patch:
            patch.setattr(backend, "upsert_owned", fail_write)
            with pytest.raises(RuntimeError, match="Rewriting the stored Catalog rows"):
                workspace.register(workspace.entities["recovery-note"], catalog=target, allow_replace=True)

        assert workspace.entity_options["recovery-note"].catalog == "full"
        assert workspace.repair()["catalog"]["ok"] is True
        workspace.release()
        reopened = hb.HeavenBase.load(identifier, context=context)
        assert reopened.entity_options["recovery-note"].catalog == "full"
        assert reopened.get(_catalog_id(reopened), entity=hb.Catalog)["name"] == "Named"
        assert reopened.audit()["catalog"]["ok"] is True
    finally:
        context.close()


@pytest.mark.slow
def test_export_restores_default_catalog_and_gram_options(tmp_path):
    identifier = "recovery-export"
    first = hb.HeavenBase(identifier, detached=True, backends={"main": _sqlite_backend(tmp_path / "workspace.db")})
    try:
        first.register(RecoveryNote)
        first.upsert(RecoveryNote, {"object_id": "n1", "name": "Named", "keywords": ["search query"]})
        exported = first.to_manifest()
        first.register(RecoveryNote, catalog="none", gram=False, allow_replace=True)
        assert first.get(_catalog_id(first), entity=hb.Catalog) is None
    finally:
        first.release()

    restored = hb.HeavenBase.from_manifest(exported, detached=True, allow_replace=True)
    try:
        assert restored.entity_options["recovery-note"] == EntityOptions()
        assert restored.get(_catalog_id(restored), entity=hb.Catalog)["name"] == "Named"
        hits = restored.query(RecoveryNote).where(RecoveryNote.keywords.contained_in("search query")).execute().rows()
        assert [row["object_id"] for row in hits] == ["n1"]
    finally:
        restored.release()


@pytest.mark.fast
def test_workspace_spec_register_restores_a_workspace_removed_by_another_context(tmp_path):
    identifier = "recovery-registry"
    first = _context(tmp_path)
    second = _context(tmp_path)
    try:
        WorkspaceSpec.register(identifier, context=first)
        assert WorkspaceSpec.unregister(identifier, context=second)
        WorkspaceSpec.register(identifier, context=first)
        third = _context(tmp_path)
        try:
            assert WorkspaceSpec.load(identifier, context=third).id == identifier
        finally:
            third.close()
    finally:
        second.close()
        first.close()


@pytest.mark.slow
def test_rejected_gram_placement_reattaches_the_stored_registration(tmp_path):
    backends = {"main": _sqlite_backend(tmp_path / "workspace.db")}
    first = hb.HeavenBase("recovery-gram", detached=True, backends=backends)
    try:
        first.register(ExplicitGramNote)
        first.upsert(ExplicitGramNote, {"object_id": "n1", "name": "Named", "keywords": ["search query"]})
    finally:
        first.release()

    reopened = hb.HeavenBase("recovery-gram", detached=True, backends=backends)
    try:
        with pytest.raises(ValueError, match="gram=False"):
            reopened.register(ExplicitGramNote, gram=False, allow_replace=True)
        assert reopened.entity_options["explicit-gram-note"] == EntityOptions()
        assert reopened.get("n1", entity=ExplicitGramNote)["name"] == "Named"
        hits = reopened.query(ExplicitGramNote).where(ExplicitGramNote.keywords.contained_in("search query")).execute().rows()
        assert [row["object_id"] for row in hits] == ["n1"]
    finally:
        reopened.release()


@pytest.mark.fast
@pytest.mark.parametrize("argument,declared", [(1, True), (0, False)])
def test_gram_definition_cannot_mask_a_non_boolean_argument(argument, declared):
    workspace = hb.HeavenBase("recovery-options", detached=True, backends={"main": {"type": "inmem"}})
    try:
        definition = {"entity_id": "recovery-note", "fields": {"name": {"type": "short-text"}}, "gram": declared}
        with pytest.raises(TypeError, match="gram must be a boolean"):
            workspace.register(definition, gram=argument)
        assert "recovery-note" not in workspace.entities
    finally:
        workspace.release()


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["full", "identity"])
def test_catalog_rewrites_preserve_zero_creation_timestamp(mode):
    workspace = hb.HeavenBase("recovery-timestamps", detached=True, backends={"main": {"type": "inmem"}})
    try:
        workspace.register(RecoveryNote, catalog=mode)
        workspace.upsert(RecoveryNote, {"object_id": "n1", "name": "Named"})
        catalog_id = _catalog_id(workspace)
        original = workspace.get(catalog_id, entity=hb.Catalog)
        workspace.upsert(hb.Catalog, dict(original, created_at=0))
        assert workspace.get(catalog_id, entity=hb.Catalog)["created_at"] == 0
        workspace.upsert(RecoveryNote, {"object_id": "n1", "name": "Renamed"})
        assert workspace.get(catalog_id, entity=hb.Catalog)["created_at"] == 0
        assert workspace.audit()["catalog"]["ok"] is True
    finally:
        workspace.release()
