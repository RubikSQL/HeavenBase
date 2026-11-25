"""Sparse-GRAM scans use the calling workspace's field options."""

import heavenbase as hb
import pytest


@pytest.mark.slow
@pytest.mark.parametrize("exact", [False, True], ids=["combined", "exact"])
@pytest.mark.parametrize("peer_gram", [True, False], ids=["rebuilt", "retired"])
def test_scan_keeps_local_gram_semantics_after_peer_registration(tmp_path, exact, peer_gram):
    class CharacterListing(hb.Entity):
        identifier = "gram-peer-listing"
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="none", gram_unit="char"))
        embedding = hb.field(hb.Vector[2])

    class TokenListing(hb.Entity):
        identifier = CharacterListing.identifier
        keywords = hb.field(hb.Array[hb.ShortText]).store(
            to="main",
            strategy=hb.SparseGramIndex(normalizer="none", gram_unit="token", tokenizer="space"),
        )
        embedding = hb.field(hb.Vector[2])

    class InlineListing(hb.Entity):
        identifier = CharacterListing.identifier
        keywords = hb.field(hb.Array[hb.ShortText])
        embedding = hb.field(hb.Vector[2])

    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "store.db")}}
    first = hb.HeavenBase("character-listings", detached=True, backends=backends)
    second = hb.HeavenBase("token-listings", detached=True, backends=backends)
    try:
        first.register(CharacterListing)
        first.upsert(CharacterListing, {"object_id": "tea", "keywords": ["Tea"], "embedding": [1.0, 0.0]})
        filtered = first.query(CharacterListing).where(CharacterListing.keywords.contained_in("Hot Tea"))
        assert filtered.execute().ids == ["tea"]
        assert filtered.near(CharacterListing.embedding, [1.0, 0.0], top_k=1, exact=exact).execute().ids == ["tea"]

        if peer_gram:
            second.register(TokenListing)
            assert second.query(TokenListing).where(TokenListing.keywords.contained_in("Hot Tea")).execute().ids == ["tea"]
        else:
            second.register(InlineListing, gram=False, allow_replace=True)

        assert first.get("tea", entity=CharacterListing)["keywords"] == ["Tea"]
        assert filtered.near(CharacterListing.embedding, [1.0, 0.0], top_k=1, exact=exact).execute().ids == ["tea"]
    finally:
        second.release()
        first.release()
