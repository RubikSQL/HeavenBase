"""Per-entity Catalog and keyword-index registration options."""

from decimal import Decimal


from types import SimpleNamespace

import heavenbase as hb
import pytest
from heavenbase.utils.config import ConfigManager
from heavenbase.workspace import EntityOptions
from heavenbase.workspace.catalog import catalog_row_id


class OptionNote(hb.Entity):
    """Entity with a projectable name and a gram-eligible keyword field."""

    identifier = "option-note"

    name = hb.field(hb.ShortText)
    keywords = hb.field(hb.Array[hb.ShortText]).default([])


class PinnedGramNote(hb.Entity):
    """Entity whose keyword field explicitly asks for the sparse-GRAM index."""

    identifier = "pinned-gram-note"

    name = hb.field(hb.ShortText)
    keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))


def _inmem_workspace(identifier):
    return hb.HeavenBase(identifier, detached=True, backends={"main": {"type": "inmem"}})


def _sqlite_workspace(tmp_path, identifier):
    database = hb.utils.path_to_file_uri(tmp_path / f"{identifier}.db")
    return hb.HeavenBase(identifier, detached=True, backends={"main": {"type": "sqlite", "name": "main", "database": database}})


def _catalog_id(workspace, entity, object_id):
    max_chars = hb.Catalog.schema().get("object_id").dtype.max_chars
    return catalog_row_id(workspace.id, entity.schema().entity_id, object_id, max_chars)


@pytest.mark.slow
def test_catalog_repair_preserves_rows_owned_by_another_workspace(tmp_path):
    backends = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "shared.db")}}
    first = hb.HeavenBase("first-catalog-owner", detached=True, backends=backends)
    second = hb.HeavenBase("second-catalog-owner", detached=True, backends=backends)
    try:
        first.register(OptionNote)
        second.register(PinnedGramNote)
        first.upsert(OptionNote, {"object_id": "stale", "name": "Orphan", "keywords": ["stale keyword"]})
        second.upsert(PinnedGramNote, {"object_id": "foreign", "name": "Keep", "keywords": ["foreign keyword"]})
        stale_id = _catalog_id(first, OptionNote, "stale")
        foreign_id = _catalog_id(second, PinnedGramNote, "foreign")
        before = second.get(foreign_id, entity=hb.Catalog)
        assert before["ws"] == second.id
        first.backends.get("main").delete(OptionNote.schema().entity_id, ["stale"])

        assert first.repair()["catalog"]["dangling_deleted"] == 1
        assert first.get(stale_id, entity=hb.Catalog) is None
        assert first.audit()["catalog"]["ok"] is True
        assert second.get(foreign_id, entity=hb.Catalog) == before
        assert second.get("foreign", entity=PinnedGramNote)["name"] == "Keep"
        assert second.audit()["catalog"]["ok"] is True
    finally:
        first.release()
        second.release()


@pytest.mark.slow
@pytest.mark.parametrize("action", ["repair", "disable_catalog"])
def test_catalog_maintenance_preserves_a_foreign_row_with_the_same_key(tmp_path, action):
    backends = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "shared.db")}}
    first = hb.HeavenBase("first-catalog-owner", detached=True, backends=backends)
    second = hb.HeavenBase("second-catalog-owner", detached=True, backends=backends)
    try:
        first.register(OptionNote)
        second.register(OptionNote)
        second.upsert(OptionNote, {"object_id": "shared", "name": "Keep", "keywords": ["shared keyword"]})
        catalog_id = _catalog_id(second, OptionNote, "shared")
        assert _catalog_id(first, OptionNote, "shared") == catalog_id
        before = second.get(catalog_id, entity=hb.Catalog)
        assert before["ws"] == second.id

        if action == "repair":
            assert first.repair()["catalog"]["ok"] is True
        else:
            first.register(OptionNote, catalog="none", allow_replace=True)
        assert second.get(catalog_id, entity=hb.Catalog) == before
        assert second.audit()["catalog"]["ok"] is True
    finally:
        first.release()
        second.release()


@pytest.mark.slow
@pytest.mark.parametrize("action", ["repair-dangling", "repair-missing", "repair-mismatched", "disable", "rewrite"])
def test_catalog_mutations_preserve_an_owner_published_after_planning(tmp_path, monkeypatch, action):
    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "shared.db")}}
    first = hb.HeavenBase("catalog-before", detached=True, backends=backends)
    second = hb.HeavenBase("catalog-after", detached=True, backends=backends)
    try:
        first.register(OptionNote)
        second.register(OptionNote)
        first.upsert(OptionNote, object_id="shared", name="Original")
        catalog_id = _catalog_id(first, OptionNote, "shared")
        backend = first.backends.get("main")
        if action == "repair-dangling":
            backend.delete(OptionNote.identifier, ["shared"])
        elif action == "repair-missing":
            backend.delete(hb.Catalog.identifier, [catalog_id])
        elif action == "repair-mismatched":
            backend.upsert([hb.ext.RowOp(hb.Catalog.identifier, catalog_id, {**first.get(catalog_id, entity=hb.Catalog), "name": "Stale"})])

        write = backend.upsert_owned
        publications = []

        def publish_before_write(ops, *, owner_field, owner):
            assert [op.object_id for op in ops] == [catalog_id]
            second.upsert(OptionNote, object_id="shared", name="Keep", keywords=["foreign keyword"])
            published = second.get(catalog_id, entity=hb.Catalog)
            assert published["ws"] == second.id
            publications.append(published)
            return write(ops, owner_field=owner_field, owner=owner)

        monkeypatch.setattr(backend, "upsert_owned", publish_before_write)
        if action.startswith("repair-"):
            result = first.repair()["catalog"]
            assert (result["dangling_deleted"], result["missing_written"], result["mismatched_rewritten"]) == (0, 0, 0)
        else:
            first.register(OptionNote, allow_replace=True, catalog="none" if action == "disable" else "identity")
        assert len(publications) == 1
        assert second.get("shared", entity=OptionNote)["name"] == "Keep"
        assert second.get(catalog_id, entity=hb.Catalog) == publications[0]
        assert second.query(hb.Catalog).where(hb.Catalog.keywords.contained_in("foreign keyword")).execute().ids == [catalog_id]
    finally:
        first.release()
        second.release()


@pytest.mark.fast
def test_entity_options_validate_and_resolve_in_precedence_order():
    """Explicit arguments win over definition keys, which win over the live registration, then defaults."""
    assert EntityOptions() == EntityOptions(catalog="full", gram=True)
    assert EntityOptions(catalog="identity", gram=False).to_dict() == {"catalog": "identity", "gram": False}

    with pytest.raises(ValueError, match="catalog must be one of"):
        EntityOptions(catalog="partial")
    with pytest.raises(TypeError, match="gram must be a boolean"):
        EntityOptions(gram="no")
    with pytest.raises(TypeError, match="^gram must be a boolean, got numpy.bool$"):
        EntityOptions(gram=type("bool", (), {"__module__": "numpy"})())  # NumPy 2's boolean scalar type is named ``bool``
    # Every type outside the builtins is named with its module; builtin types keep their bare name.
    with pytest.raises(TypeError, match="^gram must be a boolean, got decimal.Decimal$"):
        EntityOptions(gram=Decimal(1))
    with pytest.raises(TypeError, match="^gram must be a boolean, got int$"):
        EntityOptions(gram=1)
    with pytest.raises(TypeError, match="^gram must be a boolean, got NoneType$"):
        EntityOptions(gram=None)

    live = EntityOptions(catalog="identity", gram=False)
    assert EntityOptions.resolve(None, None, catalog=None, gram=None) == EntityOptions()
    assert EntityOptions.resolve(live, None, catalog=None, gram=None) == live
    assert EntityOptions.resolve(live, None, catalog="full", gram=None) == EntityOptions(catalog="full", gram=False)
    assert EntityOptions.resolve(live, {"catalog": "none"}, catalog=None, gram=True) == EntityOptions(catalog="none", gram=True)
    assert EntityOptions.resolve(None, {"gram": False}, catalog=None, gram=False) == EntityOptions(gram=False)
    with pytest.raises(ValueError, match="conflicts with the entity definition"):
        EntityOptions.resolve(None, {"catalog": "identity"}, catalog="full", gram=None)


@pytest.mark.fast
def test_register_records_options_and_defaults_stay_full():
    """Registration stores the effective options next to the class and plan; defaults are unchanged."""
    workspace = _inmem_workspace("entity-options-defaults")
    workspace.register(OptionNote)

    assert workspace.entity_options["option-note"] == EntityOptions()
    assert workspace.entity_options[hb.Catalog.schema().entity_id] == EntityOptions()
    assert set(workspace.entity_options) == set(workspace.entities)

    workspace.upsert(OptionNote, {"object_id": "n1", "name": "Named"})
    assert workspace.get(_catalog_id(workspace, OptionNote, "n1"), entity=hb.Catalog)["name"] == "Named"

    with pytest.raises(ValueError, match="catalog must be one of"):
        workspace.register(OptionNote, catalog="partial", allow_replace=True)
    with pytest.raises(TypeError, match="gram must be a boolean"):
        workspace.register(OptionNote, gram="no", allow_replace=True)
    with pytest.raises(ValueError, match="cannot be registered or replaced"):
        workspace.register(hb.Catalog, catalog="none")


@pytest.mark.fast
def test_reregistration_keeps_options_unless_explicitly_changed_with_allow_replace():
    """``None`` inherits the live options; a different value is a replacement like a structural change."""
    workspace = _inmem_workspace("entity-options-replace")
    workspace.register(OptionNote, catalog="identity", gram=False)

    assert workspace.register(OptionNote) is OptionNote
    assert workspace.register(OptionNote, catalog="identity", gram=False) is OptionNote
    assert workspace.entity_options["option-note"] == EntityOptions(catalog="identity", gram=False)

    with pytest.raises(ValueError, match="already registered with catalog='identity' and gram=False; pass allow_replace=True"):
        workspace.register(OptionNote, catalog="full")
    with pytest.raises(ValueError, match="pass allow_replace=True"):
        workspace.register(OptionNote, gram=True)
    assert workspace.entity_options["option-note"] == EntityOptions(catalog="identity", gram=False)

    workspace.register(OptionNote, catalog="none", allow_replace=True)
    assert workspace.entity_options["option-note"] == EntityOptions(catalog="none", gram=False)
    workspace.alter_entity(OptionNote, confirm="option-note")
    assert workspace.entity_options["option-note"] == EntityOptions(catalog="none", gram=False)


@pytest.mark.fast
def test_gram_false_replaces_profile_routes_and_refuses_explicit_gram_placement(tmp_path):
    """``gram=False`` stores keyword arrays inline; an explicit SparseGramIndex placement cannot be silenced."""
    workspace = _sqlite_workspace(tmp_path, "entity-options-gram-plan")
    workspace.register(OptionNote, gram=False)

    binding = workspace.storage_plans["option-note"].first("keywords")
    assert binding.strategy == hb.InlineColumn.identifier
    assert binding.backend == "main"
    with pytest.raises(ValueError, match="gram=False conflicts with the explicit SparseGramIndex placement"):
        workspace.register(PinnedGramNote, gram=False)
    assert "pinned-gram-note" not in workspace.entities

    workspace.upsert(OptionNote, {"object_id": "n1", "name": "Named", "keywords": ["redis query engine"]})
    assert workspace.get("n1", entity=OptionNote)["keywords"] == ["redis query engine"]
    assert [row["object_id"] for row in workspace.query(OptionNote).where(OptionNote.keywords.array_contains("redis query engine")).execute().rows()] == ["n1"]
    query = workspace.query(OptionNote).where(OptionNote.keywords.contained_in("mentioned redis query engine"))
    with pytest.raises(LookupError, match="registered with gram=False, so keywords.contained_in has no sparse-GRAM keyword index"):
        query.execute()
    step = workspace.explain(query.spec)["steps"][0]
    assert step["unsupported_reason"].startswith("Entity 'option-note' is registered with gram=False")


@pytest.mark.slow
def test_catalog_none_rows_are_absent_from_discovery_but_readable_by_id():
    """A ``catalog="none"`` entity has no Catalog rows; reads by id still reach its own table."""
    workspace = _inmem_workspace("entity-options-none-discovery")
    workspace.register(OptionNote, catalog="none")
    workspace.upsert(OptionNote, {"object_id": "n1", "name": "Hidden", "keywords": ["hidden keyword"]})

    assert workspace.get(_catalog_id(workspace, OptionNote, "n1"), entity=hb.Catalog) is None
    assert workspace.query(hb.Catalog).execute().rows() == []
    assert workspace.search("Hidden").rows() == []
    assert workspace.get("n1", entity=OptionNote)["name"] == "Hidden"
    assert workspace.get("n1")["name"] == "Hidden"
    assert workspace.exists("n1") is True

    audit = workspace.audit()["catalog"]
    assert audit["ok"] is True
    assert (audit["checked"]["objects"], audit["checked"]["catalog_rows"]) == (0, 0)
    assert "option-note" not in workspace._catalog_audit.participating_entity_ids()
    assert workspace.delete((OptionNote, "n1")) is True
    assert workspace.count(OptionNote) == 0


class LongIdNote(hb.Entity):
    """Entity whose object_id domain allows ids longer than the default Identifier limit, with a side-table field."""

    identifier = "long-id-note"

    object_id = hb.field(hb.Identifier(max_chars=300)).pk()
    name = hb.field(hb.ShortText)
    labels = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["inmem", "sqlite"])
@pytest.mark.parametrize("catalog", ["full", "identity"])
def test_a_catalog_row_holds_a_target_id_longer_than_the_default_identifier_limit(tmp_path, kind, catalog):
    """Under ``full`` and ``identity`` a declared longer id domain writes, reads, audits, and deletes up to 255 characters."""
    workspace = _inmem_workspace(f"entity-options-long-{catalog}") if kind == "inmem" else _sqlite_workspace(tmp_path, f"entity-options-long-{catalog}")
    workspace.register(LongIdNote, catalog=catalog)
    ids = ["a" * 64, "b" * 200, "c" * 255]
    workspace.upsert_many(LongIdNote, [{"object_id": object_id, "name": f"Named {len(object_id)}", "labels": ["x", "y"]} for object_id in ids])

    for object_id in ids:
        assert workspace.get(object_id, entity=LongIdNote)["labels"] == ["x", "y"]
        catalog_row = workspace.get(_catalog_id(workspace, LongIdNote, object_id), entity=hb.Catalog)
        assert (catalog_row["target_id"], catalog_row["target_entity"]) == (object_id, "long-id-note")
        assert catalog_row["name"] == ("" if catalog == "identity" else f"Named {len(object_id)}")
        assert workspace.get(catalog_row["target_id"], entity=catalog_row["target_entity"])["object_id"] == object_id
    assert workspace.audit()["catalog"]["ok"] is True

    # longer than Catalog.target_id holds: refused before anything is stored, naming the way out
    refused = r"'long-id-note' object_id has 256 characters, but its Catalog row's target_id holds at most 255; register the entity with catalog='none'"
    with pytest.raises(ValueError, match=refused):
        workspace.upsert(LongIdNote, {"object_id": "d" * 256, "name": "Too long"})
    assert workspace.get("d" * 256, entity=LongIdNote) is None

    assert workspace.delete((LongIdNote, ids[1])) is True
    assert workspace.get(_catalog_id(workspace, LongIdNote, ids[1]), entity=hb.Catalog) is None
    assert workspace.audit()["catalog"]["ok"] is True


@pytest.mark.fast
def test_catalog_none_holds_ids_up_to_the_declared_domain_and_sql_side_tables_take_that_domain(tmp_path):
    """Without a Catalog row only the entity's own domain bounds an id, and SQL side tables size object_id by it."""
    workspace = _sqlite_workspace(tmp_path, "entity-options-long-none")
    workspace.register(LongIdNote, catalog="none")
    long_id = "e" * 300
    workspace.upsert(LongIdNote, {"object_id": long_id, "name": "Longest", "labels": ["z"]})
    assert workspace.get(long_id, entity=LongIdNote)["labels"] == ["z"]
    side_table = workspace.backends.get("main")._side_specs["long-id-note"]["labels"].table
    assert side_table.c.object_id.type.length == 300


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["inmem", "sqlite"])
@pytest.mark.parametrize("catalog", ["full", "identity"])
def test_a_change_from_catalog_none_is_refused_while_a_stored_id_is_too_long_for_a_catalog_row(tmp_path, kind, catalog):
    """Leaving ``none`` with a stored id over 255 characters is refused before the switch, and the audit still runs."""
    workspace = _inmem_workspace(f"entity-options-switch-{catalog}") if kind == "inmem" else _sqlite_workspace(tmp_path, f"entity-options-switch-{catalog}")
    workspace.register(LongIdNote, catalog="none")
    workspace.register(OptionNote)
    workspace.upsert(OptionNote, {"object_id": "other-1", "name": "Unrelated"})
    workspace.upsert_many(LongIdNote, [{"object_id": "s-1", "name": "Short"}, {"object_id": "w" * 280, "name": "Long"}])
    catalog_rows = workspace.count(hb.Catalog)

    refused = rf"'long-id-note' cannot change to catalog='{catalog}': its stored object_id starting 'w+' has 280 characters, but a Catalog row's target_id holds at most 255"
    with pytest.raises(ValueError, match=refused):
        workspace.register(LongIdNote, catalog=catalog, allow_replace=True)
    assert workspace.entity_options["long-id-note"].catalog == "none"
    assert workspace.count(hb.Catalog) == catalog_rows
    assert workspace.audit()["catalog"]["ok"] is True
    assert workspace.get("w" * 280, entity=LongIdNote)["name"] == "Long"

    # once the long row is gone the same change goes through and publishes the remaining rows
    assert workspace.delete((LongIdNote, "w" * 280)) is True
    workspace.register(LongIdNote, catalog=catalog, allow_replace=True)
    assert workspace.entity_options["long-id-note"].catalog == catalog
    assert workspace.get(_catalog_id(workspace, LongIdNote, "s-1"), entity=hb.Catalog)["target_id"] == "s-1"
    assert workspace.audit()["catalog"]["ok"] is True


class LongIdGramNote(hb.Entity):
    """Entity whose object_id domain allows ids longer than a Catalog row holds, with a gram-eligible keyword field."""

    identifier = "long-id-gram-note"

    object_id = hb.field(hb.Identifier(max_chars=300)).pk()
    name = hb.field(hb.ShortText)
    keywords = hb.field(hb.Array[hb.ShortText]).default([])


@pytest.mark.slow
@pytest.mark.parametrize("catalog", ["full", "identity"])
def test_a_refused_change_from_catalog_none_in_a_fresh_process_keeps_the_stored_gram_option(tmp_path, catalog):
    """A fresh process asking to leave ``none`` and drop ``gram`` is refused before either option or the keyword index changes."""
    backends = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "fresh-refusal.db")}}
    writer = hb.HeavenBase("entity-options-fresh-refusal", detached=True, backends=backends)
    writer.register(LongIdGramNote, catalog="none")
    writer.upsert_many(
        LongIdGramNote,
        [{"object_id": "s1", "name": "Short", "keywords": ["redis query engine"]}, {"object_id": "w" * 280, "name": "Long"}],
    )
    writer.release()

    reopened = hb.HeavenBase("entity-options-fresh-refusal", detached=True, backends=backends)
    try:
        with pytest.raises(ValueError, match="cannot change to catalog=.*a Catalog row's target_id holds at most 255"):
            reopened.register(LongIdGramNote, catalog=catalog, gram=False, allow_replace=True)
        stored = EntityOptions(catalog="none", gram=True)
        assert reopened.entity_options["long-id-gram-note"] == stored
        assert reopened._metaschema.stored_options("long-id-gram-note") == stored
        adapter = reopened.backends.get("main").strategy_adapter(hb.SparseGramIndex)
        assert adapter.posting_count("long-id-gram-note", "keywords") > 0
        rows = reopened.query(LongIdGramNote).where(LongIdGramNote.keywords.contained_in("the redis query engine")).execute().rows()
        assert [row["object_id"] for row in rows] == ["s1"]
        assert reopened.audit()["catalog"]["ok"] is True
    finally:
        reopened.release()


@pytest.mark.fast
def test_a_first_identity_write_publishes_the_identity_row_from_defaults():
    """With no stored row an identity write names its target, takes fresh timestamps, and projects nothing."""
    workspace = _inmem_workspace("entity-options-identity-rows")
    workspace.sign(owner="owner-a")
    workspace.register(OptionNote, catalog="identity")
    workspace.upsert(OptionNote, {"object_id": "n1", "name": "Named", "keywords": ["redis query engine"]})

    catalog_id = _catalog_id(workspace, OptionNote, "n1")
    row = workspace.get(catalog_id, entity=hb.Catalog)
    assert (row["target_id"], row["target_entity"], row["ws"]) == ("n1", "option-note", workspace.id)
    assert (row["name"], row["keywords"], row["owner"]) == ("", [], "")
    assert (row["active"], row["visible"]) == (True, True)
    assert row["created_at"] == row["updated_at"] > 0
    assert (row["expires_compute"], row["stale_compute"]) == (hb.Timestamp.default_max, hb.Timestamp.default_max)
    assert [item["target_id"] for item in workspace.query(hb.Catalog).available().execute().rows()] == ["n1"]
    assert workspace.audit()["catalog"]["ok"] is True
    assert workspace.delete((OptionNote, "n1")) is True
    assert workspace.get(catalog_id, entity=hb.Catalog) is None


@pytest.mark.slow
def test_an_identity_rewrite_keeps_the_stored_lifecycle_and_merges_nothing_else(tmp_path):
    """A later identity write keeps ``created_at``/``active``/``visible`` as a full write would, advances ``updated_at``, and projects nothing."""
    workspace = _sqlite_workspace(tmp_path, "entity-options-identity-lifecycle")
    workspace.sign(owner="owner-a")
    workspace.register(OptionNote, catalog="identity")
    workspace.upsert(OptionNote, {"object_id": "n1", "name": "Named", "keywords": ["redis query engine"]})
    catalog_id = _catalog_id(workspace, OptionNote, "n1")
    first = workspace.get(catalog_id, entity=hb.Catalog)

    # another writer changes the lifecycle fields and a projected field directly
    workspace.upsert(hb.Catalog, dict(first, created_at=1_000, updated_at=1_000, active=False, visible=False, name="Manual"))
    stored = workspace.get(catalog_id, entity=hb.Catalog)
    assert (stored["created_at"], stored["updated_at"], stored["active"], stored["visible"], stored["name"]) == (1_000, 1_000, False, False, "Manual")

    workspace.upsert(OptionNote, {"object_id": "n1", "name": "Renamed", "keywords": ["redis query engine"]})
    rewritten = workspace.get(catalog_id, entity=hb.Catalog)
    assert (rewritten["created_at"], rewritten["active"], rewritten["visible"]) == (1_000, False, False)
    assert rewritten["updated_at"] >= first["updated_at"] > 1_000
    assert (rewritten["name"], rewritten["keywords"], rewritten["owner"]) == ("", [], "")
    assert (rewritten["expires_compute"], rewritten["stale_compute"]) == (hb.Timestamp.default_max, hb.Timestamp.default_max)
    assert (_posting_count(workspace), _gram_hits(workspace)) == (0, [])
    assert workspace.audit()["catalog"]["ok"] is True
    assert workspace.get("n1", entity=OptionNote)["name"] == "Renamed"


@pytest.mark.fast
def test_an_identity_rewrite_removes_keyword_rows_that_a_direct_catalog_write_left(tmp_path):
    """Keywords written straight into an identity row's Catalog row are indexed; the next identity rewrite clears them and their keyword rows."""
    workspace = _sqlite_workspace(tmp_path, "entity-options-identity-direct-keywords")
    workspace.register(OptionNote, catalog="identity")
    workspace.upsert(OptionNote, {"object_id": "n1", "name": "first"})
    catalog_id = _catalog_id(workspace, OptionNote, "n1")
    workspace.upsert(hb.Catalog, dict(workspace.get(catalog_id, entity=hb.Catalog), keywords=["redis query engine"]))
    assert _gram_hits(workspace) == ["n1"]

    workspace.upsert(OptionNote, {"object_id": "n1", "name": "second"})
    assert workspace.get(catalog_id, entity=hb.Catalog)["keywords"] == []
    assert (_posting_count(workspace), _gram_hits(workspace)) == (0, [])
    assert workspace.audit()["catalog"]["ok"] is True


@pytest.mark.slow
def test_an_identity_rewrite_removes_keyword_rows_that_a_facade_still_registered_full_wrote(tmp_path):
    """A second facade on the same store keeps its ``full`` registration; an identity rewrite of a row it indexed clears the keyword rows."""
    backends = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "stale-facade.db")}}
    full = _cold_workspace(backends)
    identity = _cold_workspace(backends, catalog="identity", allow_replace=True)
    try:
        full.upsert(OptionNote, {"object_id": "n1", "name": "Named", "keywords": ["redis query engine"]})
        assert _gram_hits(identity) == ["n1"]

        identity.upsert(OptionNote, {"object_id": "n1", "name": "Renamed", "keywords": ["redis query engine"]})
        assert identity.get(_catalog_id(identity, OptionNote, "n1"), entity=hb.Catalog)["keywords"] == []
        assert (_posting_count(identity), _gram_hits(identity), _gram_hits(full)) == (0, [], [])
    finally:
        identity.release()
        full.release()


def _write_between_lookup_and_commit(workspace, interleaved):
    """Run ``interleaved`` once, after the workspace's next upsert has planned its rows and before it writes them."""
    writer = workspace._writer
    write_upsert = writer.write_upsert
    pending = [interleaved]

    def write_after_interleaving(plan, **options):
        if pending:
            pending.pop()()
        write_upsert(plan, **options)

    writer.write_upsert = write_after_interleaving


@pytest.mark.fast
def test_an_identity_rewrite_removes_keyword_rows_written_between_its_lookup_and_its_commit(tmp_path):
    """A direct Catalog write that lands after the identity write's lookup still loses its keyword rows when that write commits."""
    workspace = _sqlite_workspace(tmp_path, "entity-options-identity-interleaved")
    workspace.register(OptionNote, catalog="identity")
    workspace.upsert(OptionNote, {"object_id": "n1", "name": "first"})
    catalog_id = _catalog_id(workspace, OptionNote, "n1")
    stored = workspace.get(catalog_id, entity=hb.Catalog)

    _write_between_lookup_and_commit(workspace, lambda: workspace.upsert(hb.Catalog, dict(stored, keywords=["redis query engine"])))
    workspace.upsert(OptionNote, {"object_id": "n1", "name": "second"})

    assert workspace.get(catalog_id, entity=hb.Catalog)["keywords"] == []
    assert (_posting_count(workspace), _gram_hits(workspace)) == (0, [])
    assert workspace.audit()["catalog"]["ok"] is True


@pytest.mark.slow
def test_keyword_rows_of_a_catalog_row_created_during_an_identity_write_go_with_that_write(tmp_path):
    """An identity write that found no stored row skips maintenance, but still removes the keyword rows of a row a ``full`` writer created meanwhile."""
    backends = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "created-meanwhile.db")}}
    full = _cold_workspace(backends)
    identity = _cold_workspace(backends, catalog="identity", allow_replace=True)
    try:
        note = {"object_id": "n1", "name": "Named", "keywords": ["redis query engine"]}
        _write_between_lookup_and_commit(identity, lambda: full.upsert(OptionNote, note))
        identity.upsert(OptionNote, note)
        assert identity.get(_catalog_id(identity, OptionNote, "n1"), entity=hb.Catalog)["keywords"] == []
        assert (_posting_count(identity), _gram_hits(identity), _gram_hits(full)) == (0, [], [])
        assert identity.audit()["catalog"]["stale_keywords"] == []
    finally:
        identity.release()
        full.release()


@pytest.mark.fast
def test_catalog_writes_do_not_resolve_strategy_bindings(tmp_path, monkeypatch):
    """Full and identity writes use the planned strategy bindings without resolving them again."""
    workspace = _sqlite_workspace(tmp_path, "entity-options-strategy-fields")
    workspace.register(OptionNote)
    backend_type = type(workspace.backends.get("main"))
    real = backend_type.strategy_adapter
    lookups: list = []
    monkeypatch.setattr(backend_type, "strategy_adapter", lambda self, strategy: lookups.append(strategy) or real(self, strategy))
    workspace.upsert(OptionNote, {"object_id": "n1", "name": "first"})
    assert lookups == []

    workspace.register(OptionNote, catalog="identity", allow_replace=True)
    lookups.clear()
    workspace.upsert(OptionNote, {"object_id": "n1", "name": "second"})
    assert lookups == []


@pytest.mark.fast
def test_changing_the_catalog_mode_reconciles_stored_catalog_rows(tmp_path):
    """A mode change with ``allow_replace`` rewrites or deletes the entity's Catalog rows at once."""
    workspace = _sqlite_workspace(tmp_path, "entity-options-reconcile")
    workspace.register(OptionNote)
    workspace.upsert_many(OptionNote, [{"object_id": f"n{index}", "name": f"Named {index}", "keywords": ["redis query engine"]} for index in range(3)])
    adapter = workspace.backends.get("main").strategy_adapter(hb.SparseGramIndex)
    catalog_entity_id = hb.Catalog.schema().entity_id
    catalog_ids = [_catalog_id(workspace, OptionNote, f"n{index}") for index in range(3)]
    gram_query = workspace.query(hb.Catalog).where(hb.Catalog.keywords.contained_in("mentioned redis query engine"))
    assert adapter.posting_count(catalog_entity_id, "keywords") > 0
    assert len(gram_query.execute().rows()) == 3

    workspace.register(OptionNote, catalog="identity", allow_replace=True)
    rows = workspace.read_rows(hb.Catalog, catalog_ids)
    assert [row["name"] for row in rows] == ["", "", ""]
    assert adapter.posting_count(catalog_entity_id, "keywords") == 0
    assert gram_query.execute().rows() == []
    assert workspace.audit()["catalog"]["ok"] is True

    workspace.register(OptionNote, catalog="none", allow_replace=True)
    assert workspace.read_rows(hb.Catalog, catalog_ids) == [None, None, None]
    assert workspace.audit()["catalog"]["ok"] is True

    workspace.register(OptionNote, catalog="full", allow_replace=True)
    assert [row["name"] for row in workspace.read_rows(hb.Catalog, catalog_ids)] == ["Named 0", "Named 1", "Named 2"]
    assert len(gram_query.execute().rows()) == 3
    assert workspace.audit()["catalog"]["ok"] is True


@pytest.mark.slow
@pytest.mark.parametrize("keep_gram", [True, False], ids=["staged", "unstaged"])
def test_a_rewrite_that_fails_part_way_keeps_index_maintenance_and_repair_finishes_it(tmp_path, monkeypatch, keep_gram):
    """A mode change rewrites rows under the registration that maintains the index; a failed page is loud and repairable.

    Leaving ``full`` rewrites before the registration switches, so the failure keeps
    ``full``; entering ``full`` switches first, so the failure keeps the new ``full``.
    Either way a write that lands before ``repair()`` maintains the keyword index.
    """
    from heavenbase.workspace.catalog import audit as catalog_audit

    monkeypatch.setattr(catalog_audit, "_RECONCILE_PAGE", 2)
    workspace = _sqlite_workspace(tmp_path, "entity-options-reconcile-failure")
    workspace.register(OptionNote)
    workspace.upsert_many(OptionNote, [{"object_id": f"n{index}", "name": f"Named {index}", "keywords": ["redis query engine"]} for index in range(5)])
    adapter = workspace.backends.get("main").strategy_adapter(hb.SparseGramIndex)
    catalog_entity_id = hb.Catalog.schema().entity_id
    catalog_ids = [_catalog_id(workspace, OptionNote, f"n{index}") for index in range(5)]
    gram_query = workspace.query(hb.Catalog).where(hb.Catalog.keywords.contained_in("mentioned redis query engine"))
    write_owned = workspace._catalog_audit.write_owned

    def fail_second_page():
        pages = []

        def write(ops):
            if ops and len(pages) == 1:
                raise OSError("disk full")
            if ops:
                pages.append(True)
            return write_owned(ops)

        monkeypatch.setattr(workspace._catalog_audit, "write_owned", write)

    def catalog_names():
        return [None if row is None else row["name"] for row in workspace.read_rows(hb.Catalog, catalog_ids)]

    def options_published():
        return workspace.get("entity:option-note", entity=hb.MetaSchema)["meta"]["options"]["catalog"]

    # Leaving full keeps the prior registration. The first page rolls back when it joined a GRAM stage.
    fail_second_page()
    with pytest.raises(RuntimeError, match="into catalog='identity' form failed after 2 written and 0 deleted; the entity keeps catalog='full'") as failure:
        workspace.register(OptionNote, catalog="identity", gram=keep_gram, allow_replace=True)
    assert isinstance(failure.value.__cause__, OSError)
    monkeypatch.setattr(workspace._catalog_audit, "write_owned", write_owned)
    assert workspace.entity_options["option-note"] == EntityOptions() and options_published() == "full"
    original_names = [f"Named {index}" for index in range(5)]
    assert catalog_names() == (original_names if keep_gram else ["", "", *original_names[2:]])
    audit = workspace.audit()["catalog"]
    assert (audit["ok"], len(audit["mismatched"]), audit["missing"], audit["dangling"]) == (keep_gram, 0 if keep_gram else 2, [], [])
    workspace.upsert(OptionNote, {"object_id": "n0", "name": "Full again", "keywords": ["redis query engine"]})  # a full write maintains the index
    repaired = workspace.repair()["catalog"]
    assert (repaired["mismatched_rewritten"], repaired["ok"]) == (0 if keep_gram else 1, True)
    assert catalog_names() == ["Full again", "Named 1", "Named 2", "Named 3", "Named 4"]
    assert sorted(row["target_id"] for row in gram_query.execute().rows()) == [f"n{index}" for index in range(5)]

    # the retried change completes and leaves no keyword rows behind
    assert workspace.register(OptionNote, catalog="identity", allow_replace=True) is OptionNote
    assert catalog_names() == ["", "", "", "", ""] and adapter.posting_count(catalog_entity_id, "keywords") == 0

    # entering full: the registration switches first, so a write during the window already indexes
    fail_second_page()
    with pytest.raises(RuntimeError, match="into catalog='full' form failed after 2 written and 0 deleted; the entity keeps catalog='full'"):
        workspace.register(OptionNote, catalog="full", allow_replace=True)
    monkeypatch.setattr(workspace._catalog_audit, "write_owned", write_owned)
    assert workspace.entity_options["option-note"] == EntityOptions() and options_published() == "full"
    assert catalog_names() == ["Full again", "Named 1", "", "", ""]
    assert len(workspace.audit()["catalog"]["mismatched"]) == 3
    workspace.upsert(OptionNote, {"object_id": "n4", "name": "Named 4", "keywords": ["redis query engine"]})
    repaired = workspace.repair()["catalog"]
    assert (repaired["mismatched_rewritten"], repaired["ok"]) == (2, True)
    assert catalog_names() == ["Full again", "Named 1", "Named 2", "Named 3", "Named 4"]
    assert sorted(row["target_id"] for row in gram_query.execute().rows()) == [f"n{index}" for index in range(5)]
    assert workspace.audit()["catalog"]["ok"] is True


@pytest.mark.slow
def test_changing_gram_drops_and_rebuilds_the_keyword_index(tmp_path):
    """``gram`` switches drop the field index and rebuild it from stored rows, in-process and across reopen."""
    database = hb.utils.path_to_file_uri(tmp_path / "gram-switch.db")
    backends = {"main": {"type": "sqlite", "name": "main", "database": database}}
    workspace = hb.HeavenBase("entity-options-gram-switch", detached=True, backends=backends)
    workspace.register(OptionNote)
    workspace.upsert(OptionNote, {"object_id": "n1", "name": "Named", "keywords": ["redis query engine"]})
    adapter = workspace.backends.get("main").strategy_adapter(hb.SparseGramIndex)
    assert adapter.posting_count("option-note", "keywords") > 0

    workspace.register(OptionNote, gram=False, allow_replace=True)
    assert adapter.posting_count("option-note", "keywords") == 0
    workspace.upsert(OptionNote, {"object_id": "n2", "name": "Later", "keywords": ["embedding search"]})
    assert adapter.posting_count("option-note", "keywords") == 0

    reopened = hb.HeavenBase("entity-options-gram-switch", detached=True, backends=backends)
    with pytest.raises(ValueError, match="is stored with catalog='full' and gram=False; pass allow_replace=True"):
        reopened.register(OptionNote, gram=True)
    reopened.register(OptionNote, gram=True, allow_replace=True)
    rows = reopened.query(OptionNote).where(OptionNote.keywords.contained_in("the embedding search request")).execute().rows()
    assert [row["object_id"] for row in rows] == ["n2"]
    assert reopened.backends.get("main").strategy_adapter(hb.SparseGramIndex).posting_count("option-note", "keywords") > 0


@pytest.mark.slow
def test_options_round_trip_through_manifest_definitions_and_metaschema():
    """Options travel as entity definition keys and are published in the MetaSchema entity row."""
    source = _inmem_workspace("entity-options-manifest")
    source.register(OptionNote, catalog="identity", gram=False)
    source.register(PinnedGramNote)

    entities = {entity["entity_id"]: entity for entity in source.to_manifest()["entities"]}
    assert (entities["option-note"]["catalog"], entities["option-note"]["gram"]) == ("identity", False)
    assert (entities["pinned-gram-note"]["catalog"], entities["pinned-gram-note"]["gram"]) == ("full", True)
    assert source.get("entity:option-note", entity=hb.MetaSchema)["meta"]["options"] == {"catalog": "identity", "gram": False}
    assert source.get("entity:pinned-gram-note", entity=hb.MetaSchema)["meta"]["options"] == {"catalog": "full", "gram": True}

    clone = hb.HeavenBase.from_manifest(source.to_manifest(), detached=True)
    assert clone.entity_options["option-note"] == EntityOptions(catalog="identity", gram=False)
    assert clone.entity_options["pinned-gram-note"] == EntityOptions()
    clone.upsert(OptionNote, {"object_id": "n1", "name": "Named"})
    assert clone.get(_catalog_id(clone, OptionNote, "n1"), entity=hb.Catalog)["name"] == ""

    by_definition = _inmem_workspace("entity-options-definition")
    by_definition.register(dict(entities["option-note"]))
    assert by_definition.entity_options["option-note"] == EntityOptions(catalog="identity", gram=False)
    with pytest.raises(ValueError, match="conflicts with the entity definition"):
        _inmem_workspace("entity-options-definition-conflict").register(dict(entities["option-note"]), catalog="full")


@pytest.mark.slow
def test_options_survive_a_durable_reopen_from_the_registered_manifest(tmp_path):
    """A workspace loaded from its durable manifest registers with the options the manifest recorded."""
    context = _context(tmp_path)
    data = {
        "kind": "heavenbase.workspace.manifest",
        "version": 2,
        "id": "entity-options-durable",
        "construction": {"kind": "backends", "config": {"main": {"type": "inmem"}}},
        "extensions": [],
        "entities": [
            {
                "entity_id": "option-note",
                "fields": {"name": {"type": "short-text"}, "keywords": {"type": "array[short-text]", "default": []}},
                "catalog": "none",
                "gram": False,
            }
        ],
        "backend_summary": [],
    }
    try:
        imported = hb.HeavenBase.from_manifest(data, context=context)
        imported.release()
        reopened = hb.HeavenBase.load("entity-options-durable", context=context)
        assert reopened.entity_options["option-note"] == EntityOptions(catalog="none", gram=False)
        reopened.upsert("option-note", {"object_id": "n1", "name": "Named", "keywords": ["hidden keyword"]})
        assert reopened.query(hb.Catalog).execute().rows() == []
        assert reopened.get("n1", entity="option-note")["name"] == "Named"
        entity = reopened.entities["option-note"]
        with pytest.raises(LookupError, match="registered with gram=False"):
            reopened.query(entity).where(entity.keywords.contained_in("the hidden keyword")).execute()
        with pytest.raises(ValueError, match="pass allow_replace=True"):
            reopened.register(entity, catalog="full")
    finally:
        context.close()


def _saved_manifest(identifier, database):
    return {
        "kind": "heavenbase.workspace.manifest",
        "version": 2,
        "id": identifier,
        "construction": {"kind": "backends", "config": {"main": {"type": "sqlite", "name": "main", "database": database}}},
        "extensions": [],
        "entities": [
            {
                "entity_id": "option-note",
                "fields": {"name": {"type": "short-text"}, "keywords": {"type": "array[short-text]", "default": []}},
                "catalog": "identity",
                "gram": False,
            }
        ],
        "backend_summary": [],
    }


@pytest.mark.slow
@pytest.mark.parametrize("change", [{"catalog": "full"}, {"gram": True}, {"catalog": "none", "gram": True}])
def test_a_manifest_backed_workspace_reopens_by_id_after_a_replacing_option_change(tmp_path, change):
    """A replacing option change is written to the saved replay manifest, so reopening by id registers it again."""
    from heavenbase.workspace import WorkspaceSpec

    context = _sqlite_context(tmp_path)
    identifier = "entity-options-saved"
    data = _saved_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "saved.db"))
    changed = EntityOptions(**{"catalog": "identity", "gram": False, **change})
    try:
        imported = hb.HeavenBase.from_manifest(data, context=context)
        imported.upsert("option-note", {"object_id": "n1", "name": "Named", "keywords": ["redis"]})
        imported.register(imported.entities["option-note"], allow_replace=True, **change)
        assert imported.entity_options["option-note"] == changed
        imported.release()
        saved = WorkspaceSpec.load(identifier, context=context).manifest["entities"][0]
        assert {key: saved[key] for key in ("catalog", "gram")} == changed.to_dict()

        for reopen in (
            lambda: hb.HeavenBase.load(identifier, context=context),
            lambda: hb.HeavenBase(identifier, context=context),
            lambda: WorkspaceSpec.load(identifier, context=context).open_detached(context=context),
        ):
            reopened = reopen()
            assert reopened.entity_options["option-note"] == changed
            assert reopened.get("n1", entity="option-note")["name"] == "Named"
            assert reopened.audit()["catalog"]["ok"] is True
            reopened.release()

        # the caller's copy of the import still declares the old options and needs consent to change them back
        with pytest.raises(ValueError, match="'option-note' is stored with .*pass allow_replace=True explicitly"):
            hb.HeavenBase.from_manifest(data, context=context)
    finally:
        context.close()


@pytest.mark.slow
def test_a_manifest_registered_through_workspace_spec_is_replayed_as_declared(tmp_path):
    """A manifest a caller saves with WorkspaceSpec.register declares its options like an explicit from_manifest."""
    from heavenbase.workspace import WorkspaceSpec

    context = _sqlite_context(tmp_path)
    identifier = "entity-options-registered"
    data = _saved_manifest(identifier, hb.utils.path_to_file_uri(tmp_path / "registered.db"))
    try:
        imported = hb.HeavenBase.from_manifest(data, context=context)
        imported.upsert("option-note", {"object_id": "n1", "name": "Named", "keywords": ["redis"]})
        imported.release()
        declared = {**data, "entities": [{**data["entities"][0], "catalog": "full"}]}
        WorkspaceSpec.register(identifier, manifest=declared, context=context)
        for reopen in (
            lambda: hb.HeavenBase.load(identifier, context=context),
            lambda: hb.HeavenBase(identifier, context=context),
            lambda: WorkspaceSpec.load(identifier, context=context).open_detached(context=context),
            lambda: hb.HeavenBase.from_manifest(declared, context=context),
        ):
            with pytest.raises(ValueError, match="'option-note' is stored with catalog='identity' and gram=False"):
                reopen()
    finally:
        context.close()


@pytest.mark.fast
def test_alter_entity_keeps_the_registered_options_and_rejects_a_definition_that_changes_them():
    """alter_entity is additive: a definition may repeat the registered catalog/gram options but not change them."""
    workspace = _inmem_workspace("entity-options-alter")
    definition = _saved_manifest("entity-options-alter", "unused")["entities"][0]
    workspace.register(dict(definition))
    registered = EntityOptions(catalog="identity", gram=False)
    with_extra = dict(definition, fields={**definition["fields"], "extra": {"type": "short-text"}})

    for changed, error, message in (
        ({"catalog": "full"}, ValueError, "alter_entity cannot change the catalog or gram option of 'option-note'"),
        ({"gram": True}, ValueError, "alter_entity cannot change the catalog or gram option of 'option-note'"),
        ({"catalog": "bogus"}, ValueError, "catalog must be one of"),
        ({"gram": "yes"}, TypeError, "gram must be a boolean"),
    ):
        with pytest.raises(error, match=message):
            workspace.alter_entity(dict(with_extra, **changed), confirm="option-note")
        assert "extra" not in workspace.entities["option-note"].schema().fields
        assert workspace.entity_options["option-note"] == registered

    workspace.alter_entity(with_extra, confirm="option-note")
    assert "extra" in workspace.entities["option-note"].schema().fields
    assert workspace.entity_options["option-note"] == registered
    without_options = {key: value for key, value in with_extra.items() if key not in ("catalog", "gram")}
    without_options["fields"] = {**with_extra["fields"], "more": {"type": "short-text"}}
    workspace.alter_entity(without_options, confirm="option-note")
    assert "more" in workspace.entities["option-note"].schema().fields
    assert workspace.entity_options["option-note"] == registered


def _cold_workspace(backends, **options):
    """Open the store as a fresh process would: a detached workspace with no live registration."""
    workspace = hb.HeavenBase("entity-options-cold", detached=True, backends=backends)
    workspace.register(OptionNote, **options)
    return workspace


def _gram_hits(workspace):
    return sorted(
        row["target_id"] for row in workspace.query(hb.Catalog).where(hb.Catalog.keywords.contained_in("mentioned redis query engine")).execute().rows()
    )


def _posting_count(workspace):
    return workspace.backends.get("main").strategy_adapter(hb.SparseGramIndex).posting_count(hb.Catalog.schema().entity_id, "keywords")


@pytest.mark.slow
def test_a_fresh_registration_reconciles_the_stored_catalog_mode_across_processes(tmp_path):
    """Without a live registration the stored MetaSchema entity row names the mode the Catalog rows carry; a different mode needs allow_replace and rewrites them."""
    backends = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "cold-reconcile.db")}}
    first = _cold_workspace(backends)
    first.upsert_many(OptionNote, [{"object_id": f"n{index}", "name": f"Named {index}", "keywords": ["redis query engine"]} for index in range(3)])
    catalog_ids = [_catalog_id(first, OptionNote, f"n{index}") for index in range(3)]
    assert _gram_hits(first) == ["n0", "n1", "n2"]
    first.release()

    # a different catalog or gram without allow_replace is refused before anything is rewritten, exactly as a live change
    refused = hb.HeavenBase("entity-options-cold", detached=True, backends=backends)
    with pytest.raises(ValueError, match="'option-note' is stored with catalog='full' and gram=True; pass allow_replace=True explicitly"):
        refused.register(OptionNote, catalog="identity")
    with pytest.raises(ValueError, match="is stored with catalog='full' and gram=True; pass allow_replace=True explicitly"):
        refused.register(OptionNote, gram=False)
    assert "option-note" not in refused.entity_options
    assert refused.register(OptionNote) is OptionNote and refused.entity_options["option-note"] == EntityOptions()
    assert _gram_hits(refused) == ["n0", "n1", "n2"] and refused.audit()["catalog"]["ok"] is True
    refused.release()

    # full -> identity: the stored registration is re-established, its rows rewritten, then the requested one takes over
    second = _cold_workspace(backends, catalog="identity", allow_replace=True)
    assert second.entity_options["option-note"] == EntityOptions(catalog="identity")
    assert second.get("entity:option-note", entity=hb.MetaSchema)["meta"]["options"] == {"catalog": "identity", "gram": True}
    assert [row["name"] for row in second.read_rows(hb.Catalog, catalog_ids)] == ["", "", ""]
    assert (_posting_count(second), _gram_hits(second)) == (0, [])
    assert second.audit()["catalog"]["ok"] is True
    second.upsert(OptionNote, {"object_id": "n0", "name": "beta", "keywords": ["green"]})
    assert second.query(hb.Catalog).where(hb.Catalog.keywords.contained_in(["green"])).execute().rows() == []
    assert second.delete((OptionNote, "n0")) is True
    assert second.query(hb.Catalog).where(hb.Catalog.keywords.contained_in(["redis query engine"])).execute().rows() == []
    assert second.audit()["catalog"]["ok"] is True
    second.release()

    # no option inherits the stored registration, exactly as it inherits a live one; nothing is rewritten
    third = _cold_workspace(backends)
    assert third.entity_options["option-note"] == EntityOptions(catalog="identity")
    assert [None if row is None else row["name"] for row in third.read_rows(hb.Catalog, catalog_ids)] == [None, "", ""]
    with pytest.raises(ValueError, match="already registered with catalog='identity'"):
        third.register(OptionNote, catalog="full")
    third.release()

    # identity -> full: the registration switches first, then every stored row is projected and indexed again
    fourth = _cold_workspace(backends, catalog="full", allow_replace=True)
    assert [None if row is None else row["name"] for row in fourth.read_rows(hb.Catalog, catalog_ids)] == [None, "Named 1", "Named 2"]
    assert _gram_hits(fourth) == ["n1", "n2"]
    assert fourth.audit()["catalog"]["ok"] is True
    fourth.release()

    # full -> identity -> none across processes deletes the rows and their keyword rows; none -> full restores them
    _cold_workspace(backends, catalog="identity", allow_replace=True).release()
    fifth = _cold_workspace(backends, catalog="none", allow_replace=True)
    assert fifth.read_rows(hb.Catalog, catalog_ids) == [None, None, None]
    assert (_posting_count(fifth), fifth.audit()["catalog"]["ok"]) == (0, True)
    fifth.release()
    sixth = _cold_workspace(backends, catalog="full", allow_replace=True)
    assert _gram_hits(sixth) == ["n1", "n2"] and sixth.audit()["catalog"]["ok"] is True
    sixth.release()


@pytest.mark.slow
def test_a_store_written_without_registration_options_is_reconciled_from_the_defaults(tmp_path):
    """An entity row published by a release without options means the defaults, so a fresh identity registration rewrites its full rows."""
    database = hb.utils.path_to_file_uri(tmp_path / "unpatched.db")
    backends = {"main": {"type": "sqlite", "name": "main", "database": database}}
    first = _cold_workspace(backends)
    first.upsert(OptionNote, {"object_id": "n1", "name": "alpha", "keywords": ["redis query engine"]})
    catalog_id = _catalog_id(first, OptionNote, "n1")
    assert first._metaschema.stored_options("option-note") == EntityOptions() and first._metaschema.stored_options("unknown") is None
    # strip the recorded options, which is what the entity row of an earlier release looks like
    row = first.get("entity:option-note", entity=hb.MetaSchema)
    first.upsert(hb.MetaSchema, dict(row, meta={key: value for key, value in row["meta"].items() if key != "options"}))
    assert "options" not in first.get("entity:option-note", entity=hb.MetaSchema)["meta"]
    assert first._metaschema.stored_options("option-note") == EntityOptions()
    first.release()

    refused = hb.HeavenBase("entity-options-cold", detached=True, backends=backends)
    with pytest.raises(ValueError, match="is stored with catalog='full' and gram=True; pass allow_replace=True"):
        refused.register(OptionNote, catalog="identity")
    refused.release()
    second = _cold_workspace(backends, catalog="identity", allow_replace=True)
    assert second.entity_options["option-note"] == EntityOptions(catalog="identity")
    assert second.get(catalog_id, entity=hb.Catalog)["name"] == ""
    assert (_posting_count(second), _gram_hits(second)) == (0, [])
    assert second.delete((OptionNote, "n1")) is True
    assert second.query(hb.Catalog).where(hb.Catalog.keywords.contained_in(["redis query engine"])).execute().rows() == []
    assert second.audit()["catalog"]["ok"] is True
    second.release()


def _without_native_row_lookup(monkeypatch, workspace):
    """Make the workspace's main Backend claim scan lookups, as a third-party Backend type without a ``row-lookup`` claim does."""
    backend = workspace.backends.get("main")
    tag = backend.tag
    scan = SimpleNamespace(value={"get_many": "scan", "exists_many": "scan", "count": "scan"})
    monkeypatch.setattr(backend, "tag", lambda identifier: scan if identifier == "row-lookup" else tag(identifier))
    assert backend.tag("row-lookup").value["get_many"] == "scan"


@pytest.mark.slow
def test_a_fresh_registration_reads_the_stored_options_from_a_backend_without_native_row_lookup(tmp_path, monkeypatch):
    """The stored options are read through the portable scan fallback too, so a fresh process keeps them and still asks for consent."""
    backends = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "scan-lookup.db")}}
    first = _cold_workspace(backends, catalog="identity", gram=False)
    first.upsert_many(OptionNote, [{"object_id": f"n{index}", "name": f"Named {index}", "keywords": ["redis"]} for index in range(3)])
    first.release()

    refused = hb.HeavenBase("entity-options-cold", detached=True, backends=backends)
    _without_native_row_lookup(monkeypatch, refused)
    assert refused._metaschema.stored_options("option-note") == EntityOptions(catalog="identity", gram=False)
    with pytest.raises(ValueError, match="'option-note' is stored with catalog='identity' and gram=False; pass allow_replace=True explicitly"):
        refused.register(OptionNote, catalog="full")
    refused.release()

    second = hb.HeavenBase("entity-options-cold", detached=True, backends=backends)
    _without_native_row_lookup(monkeypatch, second)
    second.register(OptionNote)
    assert second.entity_options["option-note"] == EntityOptions(catalog="identity", gram=False)
    assert second.get("entity:option-note", entity=hb.MetaSchema)["meta"]["options"] == {"catalog": "identity", "gram": False}
    assert second.audit()["catalog"]["ok"] is True
    second.release()


@pytest.mark.fast
def test_a_catalog_delete_removes_keyword_rows_whatever_the_registered_mode(tmp_path):
    """A Catalog delete keeps full index maintenance, so a keyword row left by an earlier full form cannot break a later read."""
    workspace = _sqlite_workspace(tmp_path, "entity-options-delete-maintenance")
    workspace.register(OptionNote)
    workspace.upsert(OptionNote, {"object_id": "n1", "name": "Named", "keywords": ["redis query engine"]})
    assert _posting_count(workspace) > 0
    # the state an interrupted rewrite or an earlier build leaves behind: identity registration over a full row with postings
    workspace.entity_options["option-note"] = EntityOptions(catalog="identity")

    assert workspace.delete((OptionNote, "n1")) is True
    assert _posting_count(workspace) == 0
    assert workspace.query(hb.Catalog).where(hb.Catalog.keywords.contained_in(["redis query engine"])).execute().rows() == []
    assert workspace.audit()["catalog"]["ok"] is True


@pytest.mark.slow
def test_gram_true_to_false_across_reopen_prunes_and_false_to_true_in_process_rebuilds(tmp_path):
    """The remaining gram directions converge too: a cold ``gram=False`` drops the stored index, a live ``gram=True`` rebuilds it."""
    backends = {"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(tmp_path / "gram-directions.db")}}
    first = _cold_workspace(backends)
    first.upsert(OptionNote, {"object_id": "n1", "name": "Named", "keywords": ["redis query engine"]})
    assert first.backends.get("main").strategy_adapter(hb.SparseGramIndex).posting_count("option-note", "keywords") > 0
    first.release()

    reopened = _cold_workspace(backends, gram=False, allow_replace=True)
    adapter = reopened.backends.get("main").strategy_adapter(hb.SparseGramIndex)
    assert reopened.entity_options["option-note"] == EntityOptions(gram=False)
    assert adapter.posting_count("option-note", "keywords") == 0
    with pytest.raises(LookupError, match="registered with gram=False"):
        reopened.query(OptionNote).where(OptionNote.keywords.contained_in("the redis query engine")).execute()

    reopened.register(OptionNote, gram=True, allow_replace=True)
    assert adapter.posting_count("option-note", "keywords") > 0
    rows = reopened.query(OptionNote).where(OptionNote.keywords.contained_in("the redis query engine")).execute().rows()
    assert [row["object_id"] for row in rows] == ["n1"]
    reopened.release()


@pytest.mark.slow
@pytest.mark.parametrize("tracked", [False, True])
def test_fresh_workspace_can_enable_explicit_gram_placement(tmp_path, tracked):
    identifier = f"enable-explicit-gram-{int(tracked)}"
    first = _sqlite_workspace(tmp_path, identifier)
    try:
        first.register(OptionNote, gram=False)
        first.upsert(OptionNote, {"object_id": "n1", "name": "Named", "keywords": ["redis query engine"]})
    finally:
        first.release()

    class ExplicitNote(hb.Entity):
        identifier = OptionNote.identifier

        name = hb.field(hb.ShortText)
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))

    fresh = hb.HeavenBase(identifier, detached=not tracked, backends=first._construction.backends)
    try:
        assert fresh.register(ExplicitNote, gram=True, allow_replace=True) is ExplicitNote
        assert fresh.storage_plans["option-note"].first("keywords").strategy == hb.SparseGramIndex.identifier
        assert fresh.query(ExplicitNote).where(ExplicitNote.keywords.contained_in("the redis query engine")).execute().ids == ["n1"]
    finally:
        fresh.release()
    if tracked:
        reopened = hb.HeavenBase.load(identifier)
        try:
            assert reopened.entities[OptionNote.identifier] is ExplicitNote
            assert reopened.query(ExplicitNote).where(ExplicitNote.keywords.contained_in("the redis query engine")).execute().ids == ["n1"]
        finally:
            reopened.release()


@pytest.mark.slow
def test_metaschema_ingress_registers_with_the_recorded_options():
    """An Entity defined through a MetaSchema row is registered with the options the row records, in either row shape."""
    published = _inmem_workspace("entity-options-ingress-published")
    meta = dict(OptionNote.schema().to_dict(), options={"catalog": "identity", "gram": False})
    published.upsert(hb.MetaSchema, {"object_id": "manual:option-note", "kind": "entity", "subject_id": "option-note", "meta": meta})
    assert published.entity_options["option-note"] == EntityOptions(catalog="identity", gram=False)
    assert published.get("entity:option-note", entity=hb.MetaSchema)["meta"]["options"] == {"catalog": "identity", "gram": False}

    definition = _inmem_workspace("entity-options-ingress-definition")
    definition.upsert(
        hb.MetaSchema,
        {"object_id": "manual:option-note", "kind": "entity", "subject_id": "option-note", "meta": dict(OptionNote.schema().to_dict(), catalog="none")},
    )
    assert definition.entity_options["option-note"] == EntityOptions(catalog="none")

    plain = _inmem_workspace("entity-options-ingress-plain")
    plain.upsert(hb.MetaSchema, {"object_id": "manual:option-note", "kind": "entity", "subject_id": "option-note", "meta": OptionNote.schema().to_dict()})
    assert plain.entity_options["option-note"] == EntityOptions()


def _context(tmp_path):
    config = ConfigManager(default={"heavenbase": {"registry": {"default_workspace": "default", "active": {"workspace": None}}}}, setup=False)
    return hb.Context.load(
        {
            "version": 1,
            "root": str(tmp_path),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=config,
    )


def _sqlite_context(tmp_path):
    """Open an isolated Context whose configuration includes the default SQLite provider."""
    return hb.Context.load(
        {
            "version": 1,
            "root": str(tmp_path / "root"),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=ConfigManager(root=str(tmp_path / "root"), setup=True),
    )


def _sql_workspace(tmp_path, kind, identifier):
    """Open a detached workspace on one file-backed SQLite database or one in-memory DuckDB database."""
    if kind == "sqlite":
        return _sqlite_workspace(tmp_path, identifier)
    pytest.importorskip("duckdb_engine")
    return hb.HeavenBase(identifier, detached=True, backends={"main": {"type": "duckdb", "name": "main", "database": ":memory:"}})


def _keyword_hits(workspace):
    rows = workspace.query(OptionNote).where(OptionNote.keywords.contained_in("mentioned redis query engine")).execute().rows()
    return sorted(row["object_id"] for row in rows)


def _keyword_postings(workspace):
    return workspace.backends.get("main").strategy_adapter(hb.SparseGramIndex).posting_count("option-note", "keywords")


def _indexed_notes(workspace, count=3):
    workspace.register(OptionNote)
    workspace.upsert_many(OptionNote, [{"object_id": f"n{index}", "name": f"Named {index}", "keywords": ["redis query engine"]} for index in range(count)])
    assert _keyword_hits(workspace) == [f"n{index}" for index in range(count)]


def _fail_once(monkeypatch, owner, name, call=1):
    """Make the ``call``-th next call of ``owner.name`` raise ``OSError`` and every other call behave normally."""
    original = getattr(owner, name)
    calls = []

    def failing(*args, **kwargs):
        calls.append(True)
        if len(calls) == call:
            raise OSError("disk full")
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, failing)


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["sqlite", "duckdb"])
def test_a_failed_catalog_change_that_also_drops_gram_keeps_the_keyword_index(tmp_path, monkeypatch, kind):
    """Leaving ``full`` fails before the switch, so the previous ``gram=True`` registration keeps a maintained keyword index."""
    workspace = _sql_workspace(tmp_path, kind, f"entity-options-gram-reconcile-failure-{kind}")
    _indexed_notes(workspace)
    _fail_once(monkeypatch, workspace.backends.get("main"), "upsert_owned")

    with pytest.raises(RuntimeError, match="the entity keeps catalog='full'"):
        workspace.register(OptionNote, catalog="identity", gram=False, allow_replace=True)

    assert workspace.entity_options["option-note"] == EntityOptions()
    assert _keyword_hits(workspace) == ["n0", "n1", "n2"]
    workspace.upsert(OptionNote, {"object_id": "n9", "name": "Later", "keywords": ["redis query engine"]})
    assert _keyword_hits(workspace) == ["n0", "n1", "n2", "n9"]
    # the retried change completes and retires the index
    workspace.register(OptionNote, catalog="identity", gram=False, allow_replace=True)
    assert _keyword_postings(workspace) == 0
    with pytest.raises(LookupError, match="registered with gram=False"):
        _keyword_hits(workspace)


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["sqlite", "duckdb"])
def test_a_gram_false_registration_that_fails_to_publish_keeps_the_keyword_index(tmp_path, monkeypatch, kind):
    """The index is released only once the registration is published, so a failed publish leaves it in place and maintained."""
    workspace = _sql_workspace(tmp_path, kind, f"entity-options-gram-publish-failure-{kind}")
    _indexed_notes(workspace)
    _fail_once(monkeypatch, workspace._metaschema, "publish_schema_rows")

    with pytest.raises(OSError, match="disk full"):
        workspace.register(OptionNote, gram=False, allow_replace=True)

    assert workspace.entity_options["option-note"] == EntityOptions()
    assert _keyword_hits(workspace) == ["n0", "n1", "n2"]
    workspace.upsert(OptionNote, {"object_id": "n9", "name": "Later", "keywords": ["redis query engine"]})
    assert _keyword_hits(workspace) == ["n0", "n1", "n2", "n9"]
    workspace.register(OptionNote, gram=False, allow_replace=True)
    assert workspace.entity_options["option-note"] == EntityOptions(gram=False)
    assert _keyword_postings(workspace) == 0


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["sqlite", "duckdb"])
def test_a_gram_false_registration_whose_index_retirement_fails_keeps_the_index_maintained(tmp_path, monkeypatch, kind):
    """A failed release after the publish warns and leaves the whole index maintained, so binding it again never trusts a stale index."""
    from heavenbase.backends.sql.strategies.sparse_gram import _SQLGramIndex

    workspace = _sql_workspace(tmp_path, kind, f"entity-options-gram-retire-failure-{kind}")
    _indexed_notes(workspace)
    # the fingerprint delete, the first statement of the transaction that also deletes the keyword rows, fails
    _fail_once(monkeypatch, _SQLGramIndex, "delete_meta")

    with pytest.warns(RuntimeWarning, match="releasing the strategy indexes it no longer binds failed"):
        workspace.register(OptionNote, gram=False, allow_replace=True)

    assert workspace.entity_options["option-note"] == EntityOptions(gram=False)
    assert _keyword_postings(workspace) > 0
    workspace.upsert(OptionNote, {"object_id": "n9", "name": "Later", "keywords": ["redis query engine"]})
    # the index kept its fingerprint, so binding it again trusts it: it must hold the write made under gram=False
    workspace.register(OptionNote, gram=True, allow_replace=True)
    assert _keyword_hits(workspace) == ["n0", "n1", "n2", "n9"]


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["sqlite", "duckdb"])
def test_a_failed_gram_false_registration_whose_restore_fails_keeps_the_keyword_index(tmp_path, monkeypatch, kind):
    """Re-establishing the previous registration can fail too; that warns, and the index it never released keeps answering."""
    workspace = _sql_workspace(tmp_path, kind, f"entity-options-gram-restore-failure-{kind}")
    _indexed_notes(workspace)
    adapter = workspace.backends.get("main").strategy_adapter(hb.SparseGramIndex)
    _fail_once(monkeypatch, workspace._metaschema, "publish_schema_rows")
    _fail_once(monkeypatch, adapter, "ensure")

    def no_rebuild(*args, **kwargs):
        raise OSError("disk full")

    # a sustained outage: no index rebuild could succeed during the failed registration
    monkeypatch.setattr(adapter, "_rebuild_field_index", no_rebuild)
    with pytest.warns(RuntimeWarning, match="re-establishing its previous registration failed as well"), pytest.raises(OSError, match="disk full"):
        workspace.register(OptionNote, gram=False, allow_replace=True)
    monkeypatch.undo()

    assert workspace.entity_options["option-note"] == EntityOptions()
    assert _keyword_hits(workspace) == ["n0", "n1", "n2"]
    workspace.upsert(OptionNote, {"object_id": "n9", "name": "Later", "keywords": ["redis query engine"]})
    assert _keyword_hits(workspace) == ["n0", "n1", "n2", "n9"]


@pytest.mark.slow
def test_a_fresh_process_whose_catalog_and_gram_change_fails_keeps_the_stored_registration(tmp_path, monkeypatch):
    """From a fresh process the stored registration is re-established as stored, so a failed change leaves its keyword index."""
    first = _sqlite_workspace(tmp_path, "entity-options-fresh-gram")
    _indexed_notes(first)
    first.release()
    second = _sqlite_workspace(tmp_path, "entity-options-fresh-gram")
    _fail_once(monkeypatch, second._catalog_audit, "reconcile_entity")

    with pytest.raises(OSError, match="disk full"):
        second.register(OptionNote, catalog="identity", gram=False, allow_replace=True)

    assert second.entity_options["option-note"] == EntityOptions()
    assert second._metaschema.stored_options("option-note") == EntityOptions()
    second.upsert(OptionNote, {"object_id": "n9", "name": "Later", "keywords": ["redis query engine"]})
    assert _keyword_hits(second) == ["n0", "n1", "n2", "n9"]
    second.release()
    third = _sqlite_workspace(tmp_path, "entity-options-fresh-gram")
    third.register(OptionNote)
    assert _keyword_hits(third) == ["n0", "n1", "n2", "n9"]
    third.release()


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["sqlite", "duckdb"])
def test_a_gram_true_registration_that_fails_writes_no_keyword_rows_for_the_gram_false_one(tmp_path, monkeypatch, kind):
    """The index a failed ``gram=True`` change built is pruned again, so the live ``gram=False`` registration writes no keyword rows."""
    workspace = _sql_workspace(tmp_path, kind, f"entity-options-gram-enable-failure-{kind}")
    workspace.register(OptionNote, gram=False)
    workspace.upsert(OptionNote, {"object_id": "n0", "name": "Named", "keywords": ["redis query engine"]})
    _fail_once(monkeypatch, workspace._metaschema, "publish_schema_rows")

    with pytest.raises(OSError, match="disk full"):
        workspace.register(OptionNote, gram=True, allow_replace=True)

    assert workspace.entity_options["option-note"] == EntityOptions(gram=False)
    workspace.upsert(OptionNote, {"object_id": "n1", "name": "Later", "keywords": ["redis query engine"]})
    assert _keyword_postings(workspace) == 0
    # the retried change builds the index from every stored row
    workspace.register(OptionNote, gram=True, allow_replace=True)
    assert _keyword_hits(workspace) == ["n0", "n1"]


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["sqlite", "duckdb"])
def test_a_gram_false_registration_whose_legacy_index_retirement_fails_rebuilds_on_the_next_binding(tmp_path, monkeypatch, kind):
    """A release that fails leaves the shared index maintained, so binding it again sees every write."""
    workspace = _sql_workspace(tmp_path, kind, f"entity-options-gram-legacy-retire-failure-{kind}")
    _indexed_notes(workspace)
    adapter = workspace.backends.get("main").strategy_adapter(hb.SparseGramIndex)
    # Looking up legacy namespaces fails after the shared delete, rolling it back.
    _fail_once(monkeypatch, adapter, "_legacy_namespaces")

    with pytest.warns(RuntimeWarning, match="releasing the strategy indexes it no longer binds failed"):
        workspace.register(OptionNote, gram=False, allow_replace=True)

    assert workspace.entity_options["option-note"] == EntityOptions(gram=False)
    workspace.upsert(OptionNote, {"object_id": "n9", "name": "Later", "keywords": ["redis query engine"]})
    workspace.register(OptionNote, gram=True, allow_replace=True)
    assert _keyword_hits(workspace) == ["n0", "n1", "n2", "n9"]


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["sqlite", "duckdb"])
def test_a_failed_gram_true_registration_rolls_back_the_index_even_when_storage_restore_fails(tmp_path, monkeypatch, kind):
    """The keyword index rolls back before restoring the previous registration's storage."""
    workspace = _sql_workspace(tmp_path, kind, f"entity-options-gram-restore-enable-failure-{kind}")
    workspace.register(OptionNote, gram=False)
    workspace.upsert(OptionNote, {"object_id": "n0", "name": "Named", "keywords": ["redis query engine"]})
    backend = workspace.backends.get("main")
    _fail_once(monkeypatch, workspace._metaschema, "publish_schema_rows")
    _fail_once(monkeypatch, backend, "ensure_plan", call=2)

    with pytest.warns(RuntimeWarning, match="re-establishing its previous registration failed as well"), pytest.raises(OSError, match="disk full"):
        workspace.register(OptionNote, gram=True, allow_replace=True)

    assert workspace.entity_options["option-note"] == EntityOptions(gram=False)
    workspace.upsert(OptionNote, {"object_id": "n1", "name": "Later", "keywords": ["redis query engine"]})
    assert _keyword_postings(workspace) == 0
    workspace.register(OptionNote, gram=True, allow_replace=True)
    assert _keyword_hits(workspace) == ["n0", "n1"]


@pytest.mark.slow
def test_a_fresh_process_whose_gram_false_change_fails_keeps_the_stored_registration(tmp_path, monkeypatch):
    """From a fresh process a failed gram-only change re-establishes the stored registration, so its keyword index keeps answering."""
    first = _sqlite_workspace(tmp_path, "entity-options-fresh-gram-only")
    _indexed_notes(first)
    first.release()
    second = _sqlite_workspace(tmp_path, "entity-options-fresh-gram-only")
    # the change is published first; the stored registration is re-established only after it fails
    _fail_once(monkeypatch, second._metaschema, "publish_schema_rows")

    with pytest.raises(OSError, match="disk full"):
        second.register(OptionNote, gram=False, allow_replace=True)

    assert second.entity_options["option-note"] == EntityOptions()
    assert second._metaschema.stored_options("option-note") == EntityOptions()
    second.upsert(OptionNote, {"object_id": "n9", "name": "Later", "keywords": ["redis query engine"]})
    assert _keyword_hits(second) == ["n0", "n1", "n2", "n9"]
    second.release()


@pytest.mark.slow
def test_a_fresh_process_whose_gram_true_change_fails_prunes_the_index_it_built(tmp_path, monkeypatch):
    """From a fresh process a failed ``gram=True`` change leaves ``gram=False`` live and writing no keyword rows."""
    first = _sqlite_workspace(tmp_path, "entity-options-fresh-gram-enable")
    first.register(OptionNote, gram=False)
    first.upsert(OptionNote, {"object_id": "n0", "name": "Named", "keywords": ["redis query engine"]})
    first.release()
    second = _sqlite_workspace(tmp_path, "entity-options-fresh-gram-enable")
    _fail_once(monkeypatch, second._metaschema, "publish_schema_rows")

    with pytest.raises(OSError, match="disk full"):
        second.register(OptionNote, gram=True, allow_replace=True)

    assert second.entity_options["option-note"] == EntityOptions(gram=False)
    second.upsert(OptionNote, {"object_id": "n1", "name": "Later", "keywords": ["redis query engine"]})
    assert _keyword_postings(second) == 0
    second.register(OptionNote, gram=True, allow_replace=True)
    assert _keyword_hits(second) == ["n0", "n1"]
    second.release()


class SideGramNote(hb.Entity):
    """Entity whose keyword field is indexed on a second backend."""

    identifier = "side-gram-note"

    name = hb.field(hb.ShortText)
    keywords = hb.field(hb.Array[hb.ShortText]).store(to="side", strategy=hb.SparseGramIndex(normalizer="simple"))


class MainOnlyNote(hb.Entity):
    """The same entity with every field on the main backend."""

    identifier = "side-gram-note"

    name = hb.field(hb.ShortText)
    keywords = hb.field(hb.Array[hb.ShortText]).default([])


class NarrowSideGramNote(hb.Entity):
    """The same side field with narrower grams."""

    identifier = "side-gram-note"

    name = hb.field(hb.ShortText)
    keywords = hb.field(hb.Array[hb.ShortText]).store(to="side", strategy=hb.SparseGramIndex(normalizer="simple", gram_width=2))


def _side_workspace(tmp_path, identifier, *, side_options=None, same_database=False):
    """Open a detached workspace with its own main database and a ``side`` database every such workspace shares."""
    return hb.HeavenBase(
        identifier,
        detached=True,
        backends={
            "main": {
                "type": "sqlite",
                "name": "main",
                "database": hb.utils.path_to_file_uri(tmp_path / ("side.db" if same_database else f"{identifier}.db")),
            },
            "side": {
                "type": "sqlite",
                "name": "side",
                "database": hb.utils.path_to_file_uri(tmp_path / "side.db"),
                **(side_options or {}),
            },
        },
    )


def _side_hits(workspace):
    rows = workspace.query(SideGramNote).where(SideGramNote.keywords.contained_in("mentioned redis query engine")).execute().rows()
    return sorted(row["object_id"] for row in rows)


def _side_fingerprint(tmp_path, database="side.db", field="keywords"):
    import sqlite3

    with sqlite3.connect(tmp_path / database) as connection:
        if not connection.execute("SELECT name FROM sqlite_master WHERE name = 'hb_gram_meta'").fetchall():
            return None
        row = connection.execute("SELECT value FROM hb_gram_meta WHERE ns = 'sql_sparse_gram' AND key = ?", (f"fingerprint:side-gram-note:{field}",)).fetchone()
    return None if row is None else row[0]


@pytest.mark.fast
def test_registration_can_publish_through_two_backend_aliases_for_one_sqlite_file(tmp_path):
    """A side index and MetaSchema can be routed through aliases for the same database file."""

    class AliasGramNote(hb.Entity):
        identifier = "side-gram-note"
        name = hb.field(hb.ShortText).store(to="side")
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="side", strategy=hb.SparseGramIndex(normalizer="simple"))

    workspace = _side_workspace(tmp_path, "entity-options-side-alias", same_database=True)
    try:
        assert workspace.register(AliasGramNote) is AliasGramNote
        assert workspace._metaschema.stored_options("side-gram-note") == EntityOptions()
        assert _side_fingerprint(tmp_path) is not None
        workspace.upsert(AliasGramNote, {"object_id": "s1", "name": "Shared", "keywords": ["redis query engine"]})
        assert workspace.query(AliasGramNote).where(AliasGramNote.keywords.contained_in("redis query engine")).execute().ids == ["s1"]
    finally:
        workspace.release()


@pytest.mark.slow
@pytest.mark.parametrize(
    ("failure", "replacement", "side_options"),
    [
        ("ensure", SideGramNote, {}),
        ("publish", SideGramNote, {}),
        ("publish", NarrowSideGramNote, {}),
    ],
    ids=["ensure", "publish", "publish-after-rebuild"],
)
def test_a_failed_registration_on_a_new_backend_keeps_the_shared_index_another_workspace_uses(tmp_path, monkeypatch, failure, replacement, side_options):
    """Undoing a failed move to another backend never deletes the shared index it found there in use by another workspace."""
    other = _side_workspace(tmp_path, "entity-options-side-other")
    other.register(SideGramNote)
    other.upsert(SideGramNote, {"object_id": "s1", "name": "Shared", "keywords": ["redis query engine"]})
    assert _side_hits(other) == ["s1"]
    fingerprint = _side_fingerprint(tmp_path)
    mine = _side_workspace(tmp_path, "entity-options-side-mine", side_options=side_options)
    mine.register(MainOnlyNote, gram=False)
    if failure == "ensure":
        adapter = mine.backends.get("side").strategy_adapter(hb.SparseGramIndex)
        _fail_once(monkeypatch, adapter, "_ensure_fingerprint")
    else:
        _fail_once(monkeypatch, mine._metaschema, "publish_schema_rows")

    with pytest.raises(OSError, match="disk full"):
        mine.register(replacement, gram=True, allow_replace=True)

    assert mine.entity_options["side-gram-note"] == EntityOptions(gram=False)
    assert _side_fingerprint(tmp_path) == fingerprint
    assert _side_hits(other) == ["s1"]
    other.upsert(SideGramNote, {"object_id": "s2", "name": "Later", "keywords": ["redis query engine"]})
    assert _side_hits(other) == ["s1", "s2"]
    # the failed registration no longer maintains the field on the side backend
    assert "side-gram-note" not in mine.backends.get("side").strategy_adapter(hb.SparseGramIndex)._fields
    mine.release()
    other.release()


@pytest.mark.slow
def test_reopened_catalog_change_preserves_an_incompatible_shared_index(tmp_path):
    """A reopen attaches the existing GRAM configuration before attempting the Catalog change."""
    from heavenbase.backends.sql.strategies import SparseGramIndexConflictError

    other = _side_workspace(tmp_path, "entity-options-reattach-other")
    mine = _side_workspace(tmp_path, "entity-options-reattach-mine")
    try:
        other.register(SideGramNote)
        mine.register(SideGramNote)
        other.upsert(SideGramNote, {"object_id": "s1", "name": "Shared", "keywords": ["redis query engine"]})
        fingerprint = _side_fingerprint(tmp_path)
        mine.release()
        mine = _side_workspace(tmp_path, "entity-options-reattach-mine")
        with pytest.raises(SparseGramIndexConflictError, match="Register the previous entity definition"):
            mine.register(NarrowSideGramNote, catalog="identity", allow_replace=True)

        assert SideGramNote.identifier not in mine.entities
        assert mine._metaschema.stored_options(SideGramNote.identifier) == EntityOptions()
        assert _side_fingerprint(tmp_path) == fingerprint
        assert _side_hits(other) == ["s1"]
        mine.register(SideGramNote)
        mine.register(NarrowSideGramNote, catalog="identity", allow_replace=True)
        assert mine.entity_options[SideGramNote.identifier] == EntityOptions(catalog="identity")
        assert _side_fingerprint(tmp_path) != fingerprint
    finally:
        mine.release()
        other.release()


@pytest.mark.fast
def test_interrupted_publication_restores_live_registration(tmp_path, monkeypatch):
    """A BaseException during publication rolls back the shared index and workspace maps."""
    workspace = _side_workspace(tmp_path, "entity-options-interrupt")
    try:
        workspace.register(SideGramNote)
        workspace.upsert(SideGramNote, {"object_id": "s1", "name": "Shared", "keywords": ["redis query engine"]})
        fingerprint = _side_fingerprint(tmp_path)
        original_plan = workspace.storage_plans[SideGramNote.identifier]

        def interrupt(_entity):
            raise KeyboardInterrupt

        monkeypatch.setattr(workspace._metaschema, "publish_schema_rows", interrupt)
        with pytest.raises(KeyboardInterrupt):
            workspace.register(NarrowSideGramNote, allow_replace=True)

        assert workspace.entities[SideGramNote.identifier] is SideGramNote
        assert workspace.storage_plans[SideGramNote.identifier] is original_plan
        assert workspace.entity_options[SideGramNote.identifier] == EntityOptions()
        assert _side_fingerprint(tmp_path) == fingerprint
        assert _side_hits(workspace) == ["s1"]
    finally:
        workspace.release()


def _split_gram_note(main_width, side_width):
    class SplitGramNote(hb.Entity):
        identifier = SideGramNote.identifier

        name = hb.field(hb.ShortText)
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="side", strategy=hb.SparseGramIndex(normalizer="simple", gram_width=side_width))
        other_words = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple", gram_width=main_width))

    return SplitGramNote


@pytest.mark.slow
@pytest.mark.parametrize("main_width", [2, 3], ids=["first-rebuild-commit", "commit-rebuild-last"])
def test_split_registration_preserves_existing_indexes_if_it_fails(tmp_path, monkeypatch, main_width):
    """Protect established indexes through independent SQL commits."""
    from sqlalchemy import event

    identifier = "entity-options-multi-commit"
    other = _side_workspace(tmp_path, "entity-options-multi-reader")
    mine = _side_workspace(tmp_path, identifier)
    original = _split_gram_note(3, 3)
    try:
        other.register(SideGramNote)
        mine.register(original)
        other.upsert(SideGramNote, {"object_id": "s1", "name": "Shared", "keywords": ["redis query engine"]})
        side_fingerprint = _side_fingerprint(tmp_path)
        main_fingerprint = _side_fingerprint(tmp_path, f"{identifier}.db", "other_words")
        assert side_fingerprint and main_fingerprint
        main = mine.backends.get("main").db
        publish = mine._metaschema.publish_schema_rows
        armed = False

        def arm_commit_failure(entity):
            nonlocal armed
            publish(entity)
            armed = True

        def fail_commit(_connection):
            if armed:
                raise OSError("disk full")

        monkeypatch.setattr(mine._metaschema, "publish_schema_rows", arm_commit_failure)
        event.listen(main.engine, "commit", fail_commit)
        try:
            with pytest.raises(OSError, match="disk full"):
                mine.register(_split_gram_note(main_width, 2), allow_replace=True)
        finally:
            event.remove(main.engine, "commit", fail_commit)

        assert armed
        assert mine.entities[SideGramNote.identifier] is original
        assert _side_fingerprint(tmp_path) == side_fingerprint
        assert _side_fingerprint(tmp_path, f"{identifier}.db", "other_words") == main_fingerprint
        assert _side_hits(other) == ["s1"]
    finally:
        mine.release()
        other.release()


@pytest.mark.slow
def test_a_failed_reregistration_does_not_rebuild_the_stale_local_index_during_rollback(tmp_path, monkeypatch):
    """The previous local options can differ from the options another workspace installed on the shared backend."""
    mine = _side_workspace(tmp_path, "entity-options-stale-mine")
    other = _side_workspace(tmp_path, "entity-options-stale-other")
    try:
        mine.register(NarrowSideGramNote)
        other.register(SideGramNote)
        other.upsert(SideGramNote, {"object_id": "s1", "name": "Shared", "keywords": ["redis query engine"]})
        fingerprint = _side_fingerprint(tmp_path)
        assert _side_hits(other) == ["s1"]
        _fail_once(monkeypatch, mine._metaschema, "publish_schema_rows")

        with pytest.raises(OSError, match="disk full"):
            mine.register(SideGramNote, allow_replace=True)

        assert mine.entities["side-gram-note"] is NarrowSideGramNote
        assert _side_fingerprint(tmp_path) == fingerprint
        assert _side_hits(other) == ["s1"]
    finally:
        mine.release()
        other.release()


@pytest.mark.fast
def test_a_failed_registration_on_a_new_backend_deletes_the_index_it_built_there(tmp_path, monkeypatch):
    """An index that only the failed registration built is deleted again, so the database is left as it was."""
    mine = _side_workspace(tmp_path, "entity-options-side-alone")
    mine.register(MainOnlyNote, gram=False)
    _fail_once(monkeypatch, mine._metaschema, "publish_schema_rows")

    with pytest.raises(OSError, match="disk full"):
        mine.register(SideGramNote, gram=True, allow_replace=True)

    assert mine.entity_options["side-gram-note"] == EntityOptions(gram=False)
    assert _side_fingerprint(tmp_path) is None
    assert "side-gram-note" not in mine.backends.get("side").strategy_adapter(hb.SparseGramIndex)._fields
    mine.release()


@pytest.mark.fast
def test_a_failed_first_registration_leaves_no_index_it_built(tmp_path, monkeypatch):
    """Without a previous registration, a failed one releases every keyword field it bound and the index it built."""
    mine = _side_workspace(tmp_path, "entity-options-side-first")
    _fail_once(monkeypatch, mine._metaschema, "publish_schema_rows")

    with pytest.raises(OSError, match="disk full"):
        mine.register(SideGramNote)

    assert "side-gram-note" not in mine.entities
    assert _side_fingerprint(tmp_path) is None
    assert "side-gram-note" not in mine.backends.get("side").strategy_adapter(hb.SparseGramIndex)._fields
    mine.release()


@pytest.mark.slow
def test_a_fresh_process_whose_gram_change_and_its_restore_both_fail_warns_and_keeps_the_index(tmp_path, monkeypatch):
    """When re-establishing the stored registration fails too, the entity is left unregistered here and its index untouched."""
    first = _sqlite_workspace(tmp_path, "entity-options-fresh-gram-restore")
    _indexed_notes(first)
    first.release()
    second = _sqlite_workspace(tmp_path, "entity-options-fresh-gram-restore")
    backend = second.backends.get("main")
    stage = backend._stage_registration

    def unavailable(*args, **kwargs):
        raise OSError("disk full")

    def refuse_reattach(*args, **kwargs):
        if kwargs.get("reattaching"):
            raise OSError("storage unavailable")
        return stage(*args, **kwargs)

    monkeypatch.setattr(second._metaschema, "publish_schema_rows", unavailable)
    monkeypatch.setattr(backend, "_stage_registration", refuse_reattach)
    with pytest.warns(RuntimeWarning, match="re-establishing its stored registration"), pytest.raises(OSError, match="disk full"):
        second.register(OptionNote, gram=False, allow_replace=True)
    monkeypatch.undo()

    assert "option-note" not in second.entities
    second.register(OptionNote)
    assert second.entity_options["option-note"] == EntityOptions()
    assert _keyword_hits(second) == ["n0", "n1", "n2"]
    second.release()
