"""Regression tests for workspace registration recovery after publication errors."""

import heavenbase as hb
import pytest

from test_durable_registration_and_catalog_state import ExplicitGramNote, _catalog_id, _context, _sqlite_backend


class ProposedGramNote(hb.Entity):
    identifier = ExplicitGramNote.identifier

    name = hb.field(hb.ShortText)
    keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))
    extra = hb.field(hb.ShortText).default("")


class ExpiringRecoveryNote(hb.Entity):
    identifier = "recovery-note"

    name = hb.field(hb.ShortText)
    expires_after = hb.field(hb.Duration)


@pytest.fixture
def tracked_catalog(tmp_path):
    context = _context(tmp_path)
    backends = {"main": _sqlite_backend(tmp_path / "workspace.db")}
    manifest = {
        "kind": "heavenbase.workspace.manifest",
        "version": 2,
        "id": "replay-recovery",
        "construction": {"kind": "backends", "config": backends},
        "entities": [
            {"entity_id": "recovery-note", "fields": {"name": {"type": "short-text"}}, "catalog": "identity"},
            {"entity_id": "unchanged-note", "fields": {"name": {"type": "short-text"}}, "catalog": "identity"},
        ],
    }
    try:
        workspace = hb.HeavenBase.from_manifest(manifest, context=context)
        workspace.upsert("recovery-note", {"object_id": "n1", "name": "Named"})
        workspace.release()
        yield context, backends, manifest
    finally:
        context.close()


@pytest.mark.slow
def test_failed_tracked_manifest_replay_can_be_repaired_and_reopened(tracked_catalog, monkeypatch):
    context, _, manifest = tracked_catalog
    identifier = manifest["id"]
    target = dict(manifest, entities=[dict(entity, catalog="full") for entity in manifest["entities"]])
    detached = hb.HeavenBase.from_manifest(manifest, context=context, detached=True)
    backend_type = type(detached.backends.get("main"))
    detached.release()

    def fail_catalog_write(*args, **kwargs):
        raise OSError("Catalog write failed")

    with monkeypatch.context() as patch:
        patch.setattr(backend_type, "upsert_owned", fail_catalog_write)
        with pytest.raises(RuntimeError, match="Rewriting the stored Catalog rows"):
            hb.HeavenBase.from_manifest(target, context=context, allow_replace=True)

    detached = hb.HeavenBase.from_manifest(dict(target, entities=target["entities"][:1]), context=context, detached=True)
    try:
        assert detached.repair()["catalog"]["ok"] is True
    finally:
        detached.release()

    reopened = hb.HeavenBase.load(identifier, context=context)
    assert reopened.entity_options["recovery-note"].catalog == "full"
    assert reopened.entity_options["unchanged-note"].catalog == "identity"
    assert reopened.get(_catalog_id(reopened), entity=hb.Catalog)["name"] == "Named"


@pytest.mark.fast
def test_rejected_gram_change_does_not_publish_proposed_schema(tmp_path):
    manifest = {
        "kind": "heavenbase.workspace.manifest",
        "version": 2,
        "id": "gram-recovery",
        "construction": {"kind": "backends", "config": {"main": _sqlite_backend(tmp_path / "workspace.db")}},
        "entities": [],
    }
    original = hb.HeavenBase.from_manifest(manifest, detached=True)
    try:
        original.register(ExplicitGramNote)
        original.upsert(ExplicitGramNote, {"object_id": "n1", "name": "Named", "keywords": ["search query"]})
        before = original.get("entity:explicit-gram-note", entity=hb.MetaSchema)
    finally:
        original.release()

    reopened = hb.HeavenBase.from_manifest(manifest, detached=True)
    try:
        with pytest.warns(RuntimeWarning, match="differs from the stored schema"), pytest.raises(ValueError, match="gram=False"):
            reopened.register(ProposedGramNote, gram=False, allow_replace=True)
        assert reopened.get("entity:explicit-gram-note", entity=hb.MetaSchema) == before
        assert reopened.get("field:explicit-gram-note.extra", entity=hb.MetaSchema) is None
        assert "explicit-gram-note" not in reopened.entities
        reopened.register(ExplicitGramNote)
        assert reopened.get("n1", entity=ExplicitGramNote)["name"] == "Named"
    finally:
        reopened.release()


@pytest.mark.slow
@pytest.mark.filterwarnings("error:Entity .* changed through a detached or untracked facade:UserWarning")
def test_manifest_warning_as_error_does_not_skip_catalog_rewrite(tracked_catalog):
    context, backends, manifest = tracked_catalog
    detached = hb.HeavenBase(manifest["id"], context=context, backends=backends, detached=True)
    try:
        entity = detached.register(manifest["entities"][0])
        with pytest.raises(UserWarning, match="detached or untracked facade"):
            detached.register(entity, catalog="full", allow_replace=True)
        assert detached.entity_options["recovery-note"].catalog == "full"
        assert detached.get(_catalog_id(detached), entity=hb.Catalog)["name"] == "Named"
    finally:
        detached.release()


@pytest.mark.slow
@pytest.mark.parametrize("rewrite", ["upsert", "repair"])
def test_expired_epoch_zero_row_stays_unavailable_after_catalog_rewrite(rewrite):
    workspace = hb.HeavenBase("epoch-recovery", detached=True, backends={"main": {"type": "inmem"}})
    try:
        workspace.register(ExpiringRecoveryNote)
        row = {"object_id": "n1", "name": "Named", "expires_after": 60_000}
        workspace.upsert(ExpiringRecoveryNote, row)
        catalog_id = _catalog_id(workspace)
        original = workspace.get(catalog_id, entity=hb.Catalog)
        workspace.upsert(hb.Catalog, dict(original, created_at=0, expires_compute=60_000))
        assert workspace.query(hb.Catalog).available(now=60_001).execute().ids == []

        if rewrite == "upsert":
            workspace.upsert(ExpiringRecoveryNote, dict(row, name="Renamed"))
        else:
            workspace.upsert(hb.Catalog, dict(original, created_at=0, expires_compute=60_000, name="Stale"))
            assert workspace.repair()["catalog"]["ok"] is True

        assert workspace.get(catalog_id, entity=hb.Catalog)["expires_compute"] == 60_000
        assert workspace.query(hb.Catalog).available(now=60_001).execute().ids == []
    finally:
        workspace.release()
