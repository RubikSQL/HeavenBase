"""A DuckDB route replaces rows on DuckDB 1.2 or later, and a release before 1.2 refuses only row replacements, clearly."""

from __future__ import annotations

import re

import pytest

import heavenbase as hb
from heavenbase.backends.sql.duckdb import DuckDBBackend
from heavenbase.backends.sql.strategies.sparse_gram import _SQLGramIndex

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("duckdb_engine")

_RELEASE = re.match(r"(\d+)\.(\d+)", duckdb.__version__)
_REPLACES_ROWS = _RELEASE is None or (int(_RELEASE[1]), int(_RELEASE[2])) >= (1, 2)
_REFUSAL = r"duckdb {version} keeps a deleted key.*needs duckdb 1\.2 or later"


class DuckNote(hb.Entity):
    identifier = "duck-main-note"

    title = hb.field(hb.ShortText)
    sku = hb.field(hb.ShortText).unique()
    tags = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)


class SideNote(hb.Entity):
    identifier = "duck-side-note"

    title = hb.field(hb.ShortText)
    body = hb.field(hb.ShortText).store(to="side")


def _main(tmp_path) -> dict:
    return {"main": {"type": "duckdb", "database": hb.utils.path_to_file_uri(tmp_path / "main.duckdb")}}


def _side(tmp_path) -> dict:
    return {
        "main": {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / "main.db")},
        "side": {"type": "duckdb", "database": hb.utils.path_to_file_uri(tmp_path / "side.duckdb")},
    }


@pytest.mark.skipif(not _REPLACES_ROWS, reason="DuckDB before 1.2 cannot replace a stored row")
def test_duckdb_main_route_bootstraps_registers_and_replaces_rows(tmp_path):
    ws = hb.HeavenBase("duck-main-route", detached=True, backends=_main(tmp_path))
    try:
        ws.register(DuckNote)
        ws.upsert(DuckNote, {"object_id": "n1", "title": "Plan", "sku": "a-1", "tags": ["x"]})
        # a replacement deletes the committed row and inserts it again, and changes its unique and side-table values
        ws.upsert(DuckNote, {"object_id": "n1", "title": "Plan v2", "sku": "a-2", "tags": ["y", "z"]})
        stored = ws.get("n1", entity=DuckNote)
        assert (stored["title"], stored["sku"], stored["tags"]) == ("Plan v2", "a-2", ["y", "z"])
    finally:
        ws.release()


@pytest.mark.parametrize("version", ["0.10.3", "1.0.0", "1.1.3"])
def test_duckdb_main_route_before_1_2_refuses_the_metaschema_replacement(monkeypatch, tmp_path, version):
    monkeypatch.setattr(duckdb, "__version__", version)
    # bootstrap publishes the sys-metaschema row a second time; DuckDB itself would report a duplicate key
    with pytest.raises(RuntimeError, match=_REFUSAL.format(version=re.escape(version))):
        hb.HeavenBase("duck-old-route", detached=True, backends=_main(tmp_path))


@pytest.mark.parametrize("version", ["1.1.3", "0.10.3"])
def test_duckdb_route_before_1_2_inserts_and_reads_but_refuses_a_replacement(monkeypatch, tmp_path, version):
    monkeypatch.setattr(duckdb, "__version__", version)
    ws = hb.HeavenBase("duck-old-side", detached=True, backends=_side(tmp_path))
    try:
        ws.register(SideNote)
        ws.upsert(SideNote, {"object_id": "s1", "title": "one", "body": "first"})
        ws.upsert(SideNote, {"object_id": "s2", "title": "two", "body": "second"})
        assert ws.get("s1", entity=SideNote)["body"] == "first"
        with pytest.raises(RuntimeError, match=_REFUSAL.format(version=re.escape(version))):
            ws.upsert(SideNote, {"object_id": "s1", "title": "one", "body": "changed"})
        assert ws.get("s1", entity=SideNote)["body"] == "first"
        ws.delete((SideNote, "s2"))
        assert ws.get("s2", entity=SideNote) is None
    finally:
        ws.release()


def test_duckdb_route_before_1_2_refuses_a_sparse_gram_metadata_replacement(monkeypatch, tmp_path):
    backend = DuckDBBackend("side", database=hb.utils.path_to_file_uri(tmp_path / "gram.duckdb"))
    index = _SQLGramIndex(backend, namespace="probe")
    index.ensure()
    monkeypatch.setattr(duckdb, "__version__", "1.1.3")
    index.set_meta("fingerprint", "a")
    with pytest.raises(RuntimeError, match=_REFUSAL.format(version=r"1\.1\.3")):
        index.set_meta("fingerprint", "b")
    assert index.get_meta("fingerprint") == "a"


@pytest.mark.parametrize("version", ["1.2.0", "1.3.0-dev", "unreleased"])
def test_duckdb_release_1_2_or_unknown_replaces_rows(monkeypatch, tmp_path, version):
    if not _REPLACES_ROWS:
        pytest.skip("DuckDB before 1.2 cannot replace a stored row")
    monkeypatch.setattr(duckdb, "__version__", version)
    ws = hb.HeavenBase("duck-new-side", detached=True, backends=_side(tmp_path))
    try:
        ws.register(SideNote)
        ws.upsert(SideNote, {"object_id": "s1", "title": "one", "body": "first"})
        ws.upsert(SideNote, {"object_id": "s1", "title": "one", "body": "changed"})
        assert ws.get("s1", entity=SideNote)["body"] == "changed"
    finally:
        ws.release()
