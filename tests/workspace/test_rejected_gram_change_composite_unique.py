import heavenbase as hb
import pytest

from heavenbase.database.execution import DatabaseError

pytestmark = pytest.mark.fast


def article_type(constrained):
    class Article(hb.Entity):
        identifier = "r9ws-composite"

        name = hb.field(hb.ShortText)
        region = hb.field(hb.ShortText)
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))
        __constraints__ = [hb.unique("name", "region")] if constrained else []

    return Article


@pytest.mark.slow
def test_rejected_gram_change_keeps_composite_unique_constraint(tmp_path):
    backends = {"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "db.sqlite")}}
    stored = article_type(True)
    proposed = article_type(False)
    row = {"object_id": "n1", "name": "Note", "region": "west", "keywords": ["search query"]}
    duplicate = dict(row, object_id="n2")

    original = hb.HeavenBase("r9ws-composite", detached=True, backends=backends)
    try:
        original.register(stored)
        original.upsert(stored, row)
        with pytest.raises(DatabaseError, match="UNIQUE constraint failed"):
            original.upsert(stored, duplicate)
    finally:
        original.release()

    reopened = hb.HeavenBase("r9ws-composite", detached=True, backends=backends)
    try:
        with pytest.warns(RuntimeWarning, match="differs from the stored schema"), pytest.raises(ValueError, match="gram=False conflicts"):
            reopened.register(proposed, gram=False, allow_replace=True)
        assert stored.identifier not in reopened.entities
        constraint = reopened.get("constraint:r9ws-composite.name-region.main.unique", entity=hb.MetaSchema)
        assert constraint["meta"]["fields"] == ["name", "region"]
        reopened.register(stored)
        with pytest.raises(DatabaseError, match="UNIQUE constraint failed"):
            reopened.upsert(stored.identifier, duplicate)
    finally:
        reopened.release()
