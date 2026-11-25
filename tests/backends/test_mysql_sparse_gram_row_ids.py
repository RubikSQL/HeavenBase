"""Sparse-GRAM updates keep separate postings for separate MySQL row IDs."""

import os
from uuid import uuid4

import pytest
import sqlalchemy as sa

import heavenbase as hb
from heavenbase.ext import RowOp


class GramCaseDoc(hb.Entity):
    identifier = "r9sqlx-gram-case-doc"
    keywords = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(normalizer="none"))


@pytest.mark.full
def test_mysql_sparse_gram_replaces_only_the_requested_object_id():
    url = os.environ.get("HVNB_MYSQL_URL")
    if not url:
        pytest.skip("requires a dedicated MySQL test database in HVNB_MYSQL_URL")
    engine = sa.create_engine(url)
    database = f"r9sqlx_gram_{uuid4().hex}"
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci")
        test_url = engine.url.set(database=database).render_as_string(hide_password=False)
        try:
            ws = hb.HeavenBase(database, detached=True, backends={"main": {"type": "mysql", "url": test_url}})
            try:
                ws.register(GramCaseDoc)
                backend = ws.backends.get("main")
                assert backend.db.spec.database == database
                entity = GramCaseDoc.identifier
                table = backend.tables[entity]
                backend.db.execute(f"ALTER TABLE `{table.name}` MODIFY object_id VARCHAR(63) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL").close()

                def matches():
                    return set(ws.query(GramCaseDoc).where(GramCaseDoc.keywords.contained_in("alpha bravo")).execute().ids)

                backend.upsert([RowOp(entity, "ProductA", {"keywords": ["alpha"]})])
                assert matches() == {"ProductA"}
                backend.upsert([RowOp(entity, "producta", {"keywords": ["bravo"]})])
                assert {row["object_id"] for row in backend.scan(entity)} == {"ProductA", "producta"}
                assert matches() == {"ProductA", "producta"}
                backend.upsert([RowOp(entity, "producta", {}, op="delete")])
                assert matches() == {"ProductA"}
            finally:
                ws.release()
        finally:
            with engine.begin() as conn:
                conn.exec_driver_sql(f"DROP DATABASE `{database}`")
    finally:
        engine.dispose()
