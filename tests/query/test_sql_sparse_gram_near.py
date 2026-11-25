import heavenbase as hb
import pytest


def _mark_empty(text, _config):
    return text or "placeholder-token"


@pytest.mark.parametrize(
    ("options", "closer", "matching", "text"),
    [
        pytest.param({"normalizer": "none"}, {"keywords": ["Apple"]}, ["apple"], "ripe apple", id="case-sensitive"),
        pytest.param(
            {"normalizer": "none", "gram_unit": "token", "tokenizer": "space"},
            {"keywords": ["Apple"]},
            ["Apple", "RedApple", "RedApple"],
            "ripe RedApple",
            id="whole-token",
        ),
        pytest.param(
            {"normalizer": "r7gramnorm_near_empty"},
            {},
            ["", "placeholder-token"],
            "placeholder-token",
            id="missing-array",
        ),
        pytest.param(
            {"normalizer": "r7gramnorm_near_empty"},
            {"keywords": []},
            [""],
            "placeholder-token",
            id="empty-array",
        ),
    ],
)
@pytest.mark.parametrize("exact", [False, True], ids=["combined_scan", "exact_scan"])
def test_near_uses_the_sql_sparse_gram_filter(tmp_path, exact, options, closer, matching, text):
    """Rank only rows admitted by the configured sparse-GRAM filter."""
    if options["normalizer"] == "r7gramnorm_near_empty":
        hb.ext.register_gram_normalizer("r7gramnorm_near_empty", _mark_empty, replace=True)
    workspace = hb.HeavenBase(
        "gram-near-filter",
        detached=True,
        backends={"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "gram-near.db")}},
    )

    class TaggedDoc(hb.Entity):
        identifier = "gram-near-doc"

        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(**options), optional=True)
        embedding = hb.field(hb.Vector[2])

    try:
        workspace.register(TaggedDoc)
        workspace.upsert_many(
            TaggedDoc,
            [
                {"object_id": "closer", "embedding": [1.0, 0.0], **closer},
                {"object_id": "matching", "keywords": matching, "embedding": [0.0, 1.0]},
            ],
        )
        filtered = workspace.query(TaggedDoc).where(TaggedDoc.keywords.contained_in(text))
        expected = filtered.execute()
        assert expected.ids == ["matching"]

        query = filtered.near(TaggedDoc.embedding, [1.0, 0.0], top_k=1, exact=exact)
        near_step = next(step for step in query.explain()["steps"] if step.get("node") == "near")
        assert near_step["near_filter_mode"] == ("exact_scan" if exact else "combined_scan")
        result = query.execute()
        assert result.ids == expected.ids
        if not exact:
            assert result.column("match") == expected.column("match")
    finally:
        workspace.release()
