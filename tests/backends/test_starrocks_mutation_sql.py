"""StarRocks mutations use its SQL grammar over the MySQL wire protocol."""

from contextlib import nullcontext
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import dialect

import heavenbase as hb
from heavenbase.ext import RowOp


@pytest.mark.fast
@pytest.mark.parametrize("operation", ["upsert", "delete"])
def test_starrocks_mutation_uses_plain_id_predicate(monkeypatch, operation):
    monkeypatch.setattr(hb.backends.StarRocksBackend, "_ensure_database", lambda *args: None)
    backend = hb.backends.StarRocksBackend("starrocks")
    backend.tables["catalog-note"] = sa.Table(
        "catalog_notes", backend.metadata, sa.Column("object_id", sa.String(63), primary_key=True), sa.Column("title", sa.String(63))
    )
    monkeypatch.setattr(backend, "_pool_slots", lambda db: 1)
    monkeypatch.setattr(backend, "_row_write_tx", lambda **kwargs: nullcontext())
    execute = Mock()
    monkeypatch.setattr(backend.db, "orm_execute", execute)

    fields = {"title": "Catalog note"} if operation == "upsert" else {}
    backend.upsert([RowOp("catalog-note", "ProductA", fields, op=operation, strategies=False)])

    statement, params = execute.call_args_list[0].args
    assert str(statement.compile(dialect=dialect())) == "DELETE FROM catalog_notes WHERE catalog_notes.object_id IN (__[POSTCOMPILE_object_ids])"
    assert params == {"object_ids": ["ProductA"]}
    assert execute.call_count == (2 if operation == "upsert" else 1)
