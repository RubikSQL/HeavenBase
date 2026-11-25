"""Catalog reconciles only after pending registrations can publish."""

import sqlite3
from contextlib import closing

import heavenbase as hb
import pytest
from sqlalchemy.exc import OperationalError

from heavenbase.backends.base import _RegistrationPublication


def _listing_backend(path, name, journal):
    return {"type": "sqlite", "name": name, "database": path.as_uri(), "pragmas": [f"PRAGMA journal_mode={journal}", "PRAGMA busy_timeout=50"]}


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["identity", "none"])
def test_failed_gram_commit_preserves_catalog_and_schema(tmp_path, mode):
    class Listing(hb.Entity):
        identifier = "market-listing"
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="market", strategy=hb.SparseGramIndex(normalizer="none"))

    class UpdatedListing(hb.Entity):
        identifier = Listing.identifier
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="market", strategy=hb.SparseGramIndex(normalizer="simple"))

    entity_path = tmp_path / "market.db"
    backends = {"main": _listing_backend(tmp_path / "catalog.db", "main", "WAL"), "market": _listing_backend(entity_path, "market", "DELETE")}
    workspace = hb.HeavenBase("market-commit", detached=True, backends=backends)
    try:
        workspace.register(Listing)
        workspace.upsert(Listing, {"object_id": "jasmine", "keywords": ["jasmine tea"]})
        catalog_query = workspace.query(hb.Catalog).where(hb.Catalog.keywords.contained_in("fresh jasmine tea"))
        before = catalog_query.execute().ids
        schema_id = f"entity:{Listing.identifier}"
        schema = workspace.get(schema_id, entity=hb.MetaSchema)["meta"]
        assert len(before) == 1 and schema["options"]["catalog"] == "full"

        with closing(sqlite3.connect(entity_path)) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM sqlite_master").fetchone()
            with pytest.raises(OperationalError, match="database is locked") as error:
                workspace.register(UpdatedListing, catalog=mode, allow_replace=True)
            assert any(entry.name == "do_commit" for entry in error.traceback)

        assert workspace.entity_options[Listing.identifier].catalog == "full"
        assert workspace.query(Listing).where(Listing.keywords.contained_in("fresh jasmine tea")).execute().ids == ["jasmine"]
        assert catalog_query.execute().ids == before
        assert workspace.get(schema_id, entity=hb.MetaSchema)["meta"] == schema
        assert workspace.audit()["catalog"]["ok"]

        workspace.register(UpdatedListing, catalog=mode, allow_replace=True)
        assert workspace.get(schema_id, entity=hb.MetaSchema)["meta"]["options"]["catalog"] == mode
        assert catalog_query.execute().ids == []
        assert workspace.audit()["catalog"]["ok"]
    finally:
        workspace.release()


@pytest.mark.slow
def test_catalog_identity_rewrite_allows_derive_to_update_peer_inventory(tmp_path):
    class Inventory(hb.Entity):
        identifier = "tea-inventory"
        label = hb.field(hb.ShortText)

    class Listing(hb.Entity):
        identifier = "tea-listing"
        name = hb.field(hb.ShortText)
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="none"))

    backends = {"main": _listing_backend(tmp_path / "tea.db", "main", "WAL")}
    peer = hb.HeavenBase("tea-inventory", detached=True, backends=backends)
    workspace = hb.HeavenBase("tea-market", detached=True, backends=backends)

    def update_inventory(name):
        peer.upsert(Inventory, {"object_id": "jasmine", "label": name})
        return name

    class UpdatedListing(hb.Entity):
        identifier = Listing.identifier
        name = hb.field(hb.ShortText)
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))
        inventory_label = hb.field(hb.ShortText).optional()
        __derive__ = [hb.Entity.derive("inventory_label").compute(update_inventory, inputs=["name"])]

    try:
        peer.register(Inventory, catalog="none")
        workspace.register(Listing)
        workspace.upsert(Listing, {"object_id": "jasmine", "name": "Jasmine tea", "keywords": ["jasmine tea"]})
        assert workspace.register(UpdatedListing, catalog="identity", allow_replace=True) is UpdatedListing
        assert peer.get("jasmine", entity=Inventory)["label"] == "Jasmine tea"
        assert workspace.entity_options[Listing.identifier].catalog == "identity"
        assert workspace.audit()["catalog"]["ok"]
    finally:
        workspace.release()
        peer.release()


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["identity", "none"])
def test_reopened_catalog_rewrite_waits_for_independent_gram_commit(tmp_path, monkeypatch, mode):
    class Listing(hb.Entity):
        identifier = "reopened-market-listing"
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="market", strategy=hb.SparseGramIndex())

    class UpdatedListing(hb.Entity):
        identifier = Listing.identifier
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="market", strategy=hb.SparseGramIndex())

    backends = {
        "main": _listing_backend(tmp_path / "catalog.db", "main", "WAL"),
        "market": _listing_backend(tmp_path / "market.db", "market", "WAL"),
    }
    workspace = hb.HeavenBase("reopened-market", detached=True, backends=backends)
    try:
        workspace.register(Listing)
        workspace.upsert(Listing, {"object_id": "jasmine", "keywords": ["jasmine tea"]})
    finally:
        workspace.release()

    reopened = hb.HeavenBase("reopened-market", detached=True, backends=backends)
    try:
        query = reopened.query(hb.Catalog).where(hb.Catalog.keywords.contained_in("fresh jasmine tea"))
        schema_id = f"entity:{Listing.identifier}"
        before = reopened.get(schema_id, entity=hb.MetaSchema)
        ids = query.execute().ids
        assert ids

        def fail_staged_commit(publication):
            assert publication._transactions
            raise OSError("cannot commit keyword index")

        with monkeypatch.context() as patch:
            patch.setattr(_RegistrationPublication, "commit", fail_staged_commit)
            with pytest.raises(OSError, match="cannot commit keyword index"):
                reopened.register(UpdatedListing, catalog=mode, allow_replace=True)

        assert Listing.identifier not in reopened.entities
        assert reopened.get(schema_id, entity=hb.MetaSchema) == before
        assert query.execute().ids == ids
        reopened.register(UpdatedListing, catalog=mode, allow_replace=True)
        assert query.execute().ids == []
        assert reopened.audit()["catalog"]["ok"]
    finally:
        reopened.release()
