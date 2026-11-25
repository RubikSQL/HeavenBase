import heavenbase as hb
import pytest

pytestmark = pytest.mark.fast


def article_type(constrained, *, sparse=False):
    class Article(hb.Entity):
        identifier = "review-article"

        name = hb.field(hb.ShortText)
        region = hb.field(hb.ShortText)
        keywords = (
            hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple")) if sparse else hb.field(hb.Array[hb.ShortText])
        )
        __constraints__ = [hb.unique("name", "region")] if constrained else []

    return Article


def remove_constraint(workspace, constrained, current, legacy):
    constraint_id = f"constraint:{constrained.identifier}.name-region.main.unique"
    removed = workspace.get(constraint_id, entity=hb.MetaSchema)
    workspace.register(current, allow_replace=True)
    if not legacy:
        assert workspace.get(constraint_id, entity=hb.MetaSchema) is None
        return
    entity_row = workspace.get(f"entity:{constrained.identifier}", entity=hb.MetaSchema)
    entity_row["meta"].pop("projection_ids")
    workspace.upsert_many(hb.MetaSchema, [entity_row, removed])


@pytest.mark.slow
@pytest.mark.parametrize("legacy", [False, True])
def test_catalog_change_respects_removed_constraint(tmp_path, legacy):
    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "articles.sqlite")}}
    constrained, current = article_type(True), article_type(False)
    row = {"object_id": "note-1", "name": "Field notes", "region": "west", "keywords": ["field work"]}
    original = hb.HeavenBase("review-articles", detached=True, backends=backends)
    try:
        original.register(constrained, catalog="none")
        original.upsert(constrained, row)
        remove_constraint(original, constrained, current, legacy)
        original.upsert(current, dict(row, object_id="note-2"))
    finally:
        original.release()

    reopened = hb.HeavenBase("review-articles", detached=True, backends=backends)
    try:
        reopened.register(current, catalog="full", allow_replace=True)
        assert {row["target_id"] for row in reopened.query(hb.Catalog).execute().rows()} == {"note-1", "note-2"}
    finally:
        reopened.release()


@pytest.mark.slow
@pytest.mark.parametrize("legacy", [False, True])
def test_rejected_gram_change_reattaches_current_schema_after_constraint_removal(tmp_path, legacy):
    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "articles.sqlite")}}
    constrained, current = article_type(True, sparse=True), article_type(False, sparse=True)
    original = hb.HeavenBase("review-articles", detached=True, backends=backends)
    try:
        original.register(constrained)
        original.upsert(constrained, {"object_id": "note-1", "name": "Field notes", "region": "west", "keywords": ["field work"]})
        remove_constraint(original, constrained, current, legacy)
    finally:
        original.release()

    reopened = hb.HeavenBase("review-articles", detached=True, backends=backends)
    try:
        with pytest.raises(ValueError, match="gram=False conflicts"):
            reopened.register(current, gram=False, allow_replace=True)
        assert reopened.entities.get(current.identifier) is current
        assert reopened.get("note-1", entity=current)["region"] == "west"
    finally:
        reopened.release()
