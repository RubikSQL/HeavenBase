import heavenbase as hb
import pytest


@pytest.mark.slow
@pytest.mark.parametrize("engine", [{"isolation_level": "AUTOCOMMIT"}, {"connect_args": {"isolation_level": None}}])
def test_autocommit_contained_in_uses_field_options(tmp_path, engine):
    class Doc(hb.Entity):
        identifier = "gram-autocommit-options"
        keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="none", gram_unit="token", tokenizer="space"))

    database = hb.utils.path_to_file_uri(tmp_path / "gram-autocommit-options.db")
    ws = hb.HeavenBase("gram-autocommit-options", detached=True, backends={"main": {"type": "sqlite", "database": database, **engine}})
    try:
        ws.register(Doc)
        ws.upsert(Doc, {"object_id": "x", "keywords": ["Cat"]})
        partial = ws.query(Doc).where(Doc.keywords.contained_in("the Catfish swims")).execute()
        wrong_case = ws.query(Doc).where(Doc.keywords.contained_in("the cat sits")).execute()
        exact = ws.query(Doc).where(Doc.keywords.contained_in("the Cat sits")).execute()
        assert (partial.ids, wrong_case.ids, exact.ids) == ([], [], ["x"])
        assert [match["query_span"] for match in exact.column("match")[0]] == [[4, 7]]
    finally:
        ws.release()
