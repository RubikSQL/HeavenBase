import heavenbase as hb
import pytest
from heavenbase.backends.sql.strategies import SparseGramIndexConflictError
from sqlalchemy import event


class KeywordDoc(hb.Entity):
    identifier = "sparse-gram-shared-writes-doc"

    title: hb.ShortText
    keywords = hb.field(hb.Array[hb.ShortText])


class OlderDoc(hb.Entity):
    identifier = KeywordDoc.identifier

    title: hb.ShortText


class WideGramDoc(hb.Entity):
    identifier = "sparse-gram-wide-query-doc"

    keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple", gram_width=128))


def _workspace(tmp_path, name, **backend_config):
    return hb.HeavenBase(
        name, detached=True, backends={"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "shared.db"), **backend_config}}
    )


def _search(workspace, entity, text):
    return workspace.query(entity).where(entity.keywords.contained_in(text)).execute().ids


@pytest.mark.slow
@pytest.mark.parametrize("mutation", ["delete", "upsert"])
def test_index_conflict_precedes_autocommit_row_mutations(tmp_path, mutation):
    plain = _workspace(tmp_path, "plain", pool={"connect_args": {"isolation_level": None}})
    indexed = _workspace(tmp_path, "indexed")
    try:
        plain.register(KeywordDoc, gram=False, catalog="none")
        plain.upsert(KeywordDoc, {"object_id": "x", "title": "Original", "keywords": ["needle"]})
        indexed.register(KeywordDoc, gram=True, catalog="none", allow_replace=True)
        assert _search(indexed, KeywordDoc, "find the needle here") == ["x"]

        with pytest.raises(SparseGramIndexConflictError, match="without the index"):
            if mutation == "delete":
                plain.delete((KeywordDoc, "x"))
            else:
                plain.upsert(KeywordDoc, {"object_id": "x", "title": "Replacement", "keywords": ["haystack"]})

        row = indexed.get("x", entity=KeywordDoc)
        assert row is not None and row["title"] == "Original" and row["keywords"] == ["needle"]
        assert _search(indexed, KeywordDoc, "find the needle here") == ["x"]
    finally:
        indexed.release()
        plain.release()


@pytest.mark.slow
def test_writer_registered_before_the_keyword_field_cannot_replace_its_rows(tmp_path):
    older = _workspace(tmp_path, "older")
    indexed = _workspace(tmp_path, "indexed")
    try:
        older.register(OlderDoc)
        older.upsert(OlderDoc, {"object_id": "x", "title": "Before indexing"})
        indexed.register(KeywordDoc, gram=True)
        indexed.upsert(KeywordDoc, {"object_id": "x", "title": "Indexed", "keywords": ["needle"]})
        assert _search(indexed, KeywordDoc, "find the needle here") == ["x"]

        with pytest.raises(SparseGramIndexConflictError, match="without the field"):
            older.upsert(OlderDoc, {"object_id": "x", "title": "Replacement"})

        row = indexed.get("x", entity=KeywordDoc)
        assert row is not None and row["title"] == "Indexed" and row["keywords"] == ["needle"]
        assert _search(indexed, KeywordDoc, "find the needle here") == ["x"]
    finally:
        indexed.release()
        older.release()


@pytest.mark.slow
def test_sparse_gram_reads_short_widths_in_one_query_and_sees_new_widths(tmp_path):
    reader = _workspace(tmp_path, "reader")
    writer = _workspace(tmp_path, "writer")
    width_queries = []
    try:
        reader.register(WideGramDoc)
        writer.register(WideGramDoc)
        writer.upsert(WideGramDoc, {"object_id": "x", "keywords": ["needle"]})
        engine = reader.backends.get("main").db.engine

        def record_width_query(_conn, _cursor, statement, *_):
            if statement.startswith(("SELECT hb_gram_keywords.keyword_len", "SELECT DISTINCT hb_gram_keywords.keyword_len")):
                width_queries.append(statement)

        event.listen(engine, "before_cursor_execute", record_width_query)

        assert _search(reader, WideGramDoc, "find the needle here") == ["x"]
        assert len(width_queries) == 1

        writer.upsert(WideGramDoc, {"object_id": "y", "keywords": ["hay"]})
        width_queries.clear()
        assert _search(reader, WideGramDoc, "a pile of hay") == ["y"]
        assert len(width_queries) == 1
    finally:
        writer.release()
        reader.release()
