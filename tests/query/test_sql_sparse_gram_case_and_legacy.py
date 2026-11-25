"""Sparse-GRAM autocommit and legacy reconciliation regressions."""

import sqlite3
from contextlib import closing

import heavenbase as hb
import pytest
from test_sql_sparse_gram import _act_as_0_1_2_2


def _open(path, workspace, **engine):
    return hb.HeavenBase(
        workspace,
        detached=True,
        backends={"main": {"type": "sqlite", "name": "main", "database": hb.utils.path_to_file_uri(path), **engine}},
    )


class CaseDoc(hb.Entity):
    identifier = "r8sql-case-doc"

    keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="none"))


class LegacyDoc(hb.Entity):
    identifier = "r8sql-legacy-doc"

    keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="simple"))


def _hits(ws, entity, text):
    return ws.query(entity).where(entity.keywords.contained_in(text)).execute().ids


@pytest.mark.slow
@pytest.mark.parametrize(
    "engine",
    [
        pytest.param({}, id="transactional"),
        pytest.param({"isolation_level": "AUTOCOMMIT"}, id="autocommit"),
    ],
)
def test_sparse_gram_scans_respect_case_sensitive_options(tmp_path, engine):
    ws = _open(tmp_path / "auto.db", "r8sql-case", **engine)
    try:
        ws.register(CaseDoc)
        ws.upsert(CaseDoc, {"object_id": "x", "keywords": ["Apple"]})
        assert _hits(ws, CaseDoc, "ripe Apple") == ["x"]
        assert _hits(ws, CaseDoc, "ripe apple") == []
    finally:
        ws.release()


def _legacy_write(path, workspace, keyword, monkeypatch):
    with monkeypatch.context() as patch:
        _act_as_0_1_2_2(patch)
        ws = _open(path, workspace)
        try:
            ws.register(LegacyDoc)
            ws.upsert(LegacyDoc, {"object_id": "x", "keywords": [keyword]})
            assert _hits(ws, LegacyDoc, keyword) == ["x"]
        finally:
            ws.release()


def _legacy_ids(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT ns, keyword_id FROM hb_gram_keywords WHERE entity_id = ? AND ns LIKE 'r8sql-legacy-%' ORDER BY ns",
            (LegacyDoc.identifier,),
        ).fetchall()


@pytest.mark.slow
def test_upgrade_reconciles_legacy_rewrite_that_keeps_keyword_ids(tmp_path, monkeypatch):
    path = tmp_path / "shared.db"
    _legacy_write(path, "r8sql-legacy-a", "apple", monkeypatch)
    _legacy_write(path, "r8sql-legacy-b", "pear", monkeypatch)
    ids = _legacy_ids(path)
    assert len(ids) == 2

    upgraded = _open(path, "r8sql-legacy-current")
    try:
        upgraded.register(LegacyDoc)
        assert _hits(upgraded, LegacyDoc, "pear") == ["x"]
    finally:
        upgraded.release()

    _legacy_write(path, "r8sql-legacy-a", "apple", monkeypatch)
    assert _legacy_ids(path) == ids

    reopened = _open(path, "r8sql-legacy-current")
    try:
        reopened.register(LegacyDoc)
        assert reopened.read_rows(LegacyDoc, ["x"])[0]["keywords"] == ["apple"]
        assert _hits(reopened, LegacyDoc, "apple") == ["x"]
        assert _hits(reopened, LegacyDoc, "pear") == []
    finally:
        reopened.release()
