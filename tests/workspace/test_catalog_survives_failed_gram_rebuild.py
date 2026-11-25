"""Catalog projections survive failed sparse-GRAM rebuilds."""

import heavenbase as hb
import pytest


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["identity", "none"])
def test_failed_gram_rebuild_preserves_catalog_projection(tmp_path, mode):
    class Listing(hb.Entity):
        identifier = "gram-catalog-listing"
        name = hb.field(hb.ShortText)
        keywords = hb.field(hb.Array[hb.ShortText])

    class InvalidListing(hb.Entity):
        identifier = Listing.identifier
        name = hb.field(hb.ShortText)
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="unregistered-product-normalizer"))

    class UpdatedListing(hb.Entity):
        identifier = Listing.identifier
        name = hb.field(hb.ShortText)
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="none"))

    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "store.db")}}
    workspace = hb.HeavenBase("gram-catalog", detached=True, backends=backends)
    try:
        workspace.register(Listing)
        workspace.upsert(Listing, {"object_id": "apple", "name": "Green apple", "keywords": ["green apple"]})
        catalog_search = workspace.query(hb.Catalog).where(hb.Catalog.keywords.contained_in("green apple"))
        before = catalog_search.execute().ids
        assert len(before) == 1

        with pytest.raises((KeyError, ValueError), match="unregistered-product-normalizer"):
            workspace.register(InvalidListing, catalog=mode, allow_replace=True)

        assert workspace.entity_options[Listing.identifier].catalog == "full"
        assert workspace.query(Listing).where(Listing.keywords.contained_in("green apple")).execute().ids == ["apple"]
        assert catalog_search.execute().ids == before
        assert workspace.audit()["catalog"]["ok"] is True

        workspace.register(UpdatedListing, catalog=mode, allow_replace=True)
        assert workspace.query(UpdatedListing).where(UpdatedListing.keywords.contained_in("green apple")).execute().ids == ["apple"]
        assert catalog_search.execute().ids == []
        assert workspace.audit()["catalog"]["ok"] is True
    finally:
        workspace.release()
