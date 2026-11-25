"""Rows written without keyword maintenance leave no keyword rows, and the Catalog audit checks the keyword rows."""

import pytest

import heavenbase as hb
from heavenbase.ext import RowOp


class Card(hb.Entity):
    identifier = "sql2-sweep-card"
    tags = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(gram_width=2))


class Article(hb.Entity):
    identifier = "sql2-audit-article"
    name = hb.field(hb.ShortText)
    keywords = hb.field(hb.Array[hb.ShortText]).default([])


def _workspace(tmp_path, name):
    return hb.HeavenBase(name, detached=True, backends={"main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / f"{name}.db")}})


@pytest.mark.fast
def test_an_unmaintained_write_removes_keyword_rows_another_writer_committed(tmp_path):
    """The identity Catalog gap: a row another writer stored with keywords is replaced by a write that skips maintenance."""
    ws = _workspace(tmp_path, "sql2-sweep")
    try:
        ws.register(Card, catalog="none")
        backend = ws.backends.get("main")
        adapter = backend.strategy_adapter(hb.SparseGramIndex)
        # the other writer stored the row with keywords (a maintained write)
        backend.upsert([RowOp(Card.identifier, "c1", {"tags": ["invoice", "receipt"]})])
        assert adapter.posting_count(Card.identifier) > 0
        # a write that asserts the row holds no indexed value replaces it without maintenance
        backend.upsert([RowOp(Card.identifier, "c1", {"tags": None}, strategies=False), RowOp(Card.identifier, "c2", {"tags": None}, strategies=False)])
        assert adapter.posting_count(Card.identifier) == 0
        assert ws.query(Card).where(Card.tags.contained_in("open invoice")).execute().ids == []
        probe = adapter.contained_in_probe(Card.identifier, "tags", "open invoice")
        assert probe["candidates"] == probe["exact_matches"] == 0
    finally:
        ws.release()


@pytest.mark.fast
def test_the_catalog_audit_reports_and_repair_restores_keyword_rows_that_differ_from_their_row(tmp_path):
    ws = _workspace(tmp_path, "sql2-audit")
    try:
        ws.register(Article)
        ws.upsert(Article, {"object_id": "a1", "name": "Quarterly invoice summary", "keywords": ["finance invoice"]})
        ws.upsert(Article, {"object_id": "a2", "name": "Team offsite plan", "keywords": ["team offsite"]})
        report = ws.audit()["catalog"]
        assert report["ok"] and report["stale_keywords"] == []

        backend = ws.backends.get("main")
        adapter = backend.strategy_adapter(hb.SparseGramIndex)
        catalog = hb.Catalog.schema().entity_id
        catalog_id = ws._catalog.row_id(Article.schema(), "a1")
        assert adapter.stale_keyword_objects(catalog, [catalog_id]) == []
        assert adapter.posting_count(catalog, "keywords") > 0
        # lose one Catalog row's keyword rows, as a failed or interrupted maintenance could
        adapter._need_index().delete_objects(catalog, [catalog_id])
        report = ws.audit()["catalog"]
        assert not report["ok"]
        assert report["stale_keywords"] == [{"object_id": catalog_id}]
        assert (report["missing"], report["dangling"], report["mismatched"]) == ([], [], [])

        dry = ws.repair(dry_run=True)["catalog"]
        assert dry["stale_keywords_rewritten"] == 0 and not dry["ok"]
        repaired = ws.repair()["catalog"]
        assert repaired["stale_keywords_rewritten"] == 1
        assert repaired["ok"] and repaired["after"]["stale_keywords"] == []
    finally:
        ws.release()
