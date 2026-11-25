"""Sparse-GRAM publication across independent SQL databases."""

import sqlite3
from contextlib import closing

import heavenbase as hb
import pytest
from sqlalchemy import event

from heavenbase.backends.sql.strategies import SparseGramIndexConflictError


def _entity(width, secondary="secondary"):
    class SplitDoc(hb.Entity):
        identifier = "r8sql-split-doc"

        primary_tags = hb.field(hb.Array[hb.ShortText]).store(to="main", strategy=hb.SparseGramIndex(gram_width=width))
        secondary_tags = hb.field(hb.Array[hb.ShortText]).store(to=secondary, strategy=hb.SparseGramIndex(gram_width=width))

    return SplitDoc


def _open(tmp_path, name, secondary="secondary"):
    return hb.HeavenBase(
        name,
        detached=True,
        backends={
            route: {
                "type": "sqlite",
                "name": route,
                "database": hb.utils.path_to_file_uri(tmp_path / f"{route}.db"),
            }
            for route in ("main", secondary)
        },
    )


def _check_queries(ws, entity):
    assert ws.query(entity).where(entity.primary_tags.contained_in("ripe apple")).execute().ids == ["x"]
    assert ws.query(entity).where(entity.secondary_tags.contained_in("fresh pear")).execute().ids == ["x"]


def _fingerprints(tmp_path, entity, secondary="secondary"):
    values = []
    for database, field in [("main", "primary_tags"), (secondary, "secondary_tags")]:
        with closing(sqlite3.connect(tmp_path / f"{database}.db")) as connection:
            (value,) = connection.execute(
                "SELECT value FROM hb_gram_meta WHERE ns = ? AND key = ?",
                ("sql_sparse_gram", f"fingerprint:{entity.identifier}:{field}"),
            ).fetchone()
            values.append(value)
    return values


@pytest.mark.fast
def test_registration_can_change_sparse_gram_options_on_two_sql_databases(tmp_path):
    initial, replacement = _entity(2), _entity(3)
    ws = _open(tmp_path, "r8sql-split")
    try:
        ws.register(initial)
        ws.upsert(initial, {"object_id": "x", "primary_tags": ["apple"], "secondary_tags": ["pear"]})
        _check_queries(ws, initial)
        old = _fingerprints(tmp_path, initial)

        ws.register(replacement, allow_replace=True)
        _check_queries(ws, replacement)
        assert all(before != after for before, after in zip(old, _fingerprints(tmp_path, replacement), strict=True))
    finally:
        ws.release()


@pytest.mark.fast
def test_publication_failure_rolls_back_both_sparse_gram_rebuilds(tmp_path, monkeypatch):
    initial, replacement = _entity(2), _entity(3)
    ws = _open(tmp_path, "r8sql-split-rollback")
    try:
        ws.register(initial)
        ws.upsert(initial, {"object_id": "x", "primary_tags": ["apple"], "secondary_tags": ["pear"]})
        old = _fingerprints(tmp_path, initial)

        def fail_publish(entity):
            raise OSError("cannot publish schema")

        monkeypatch.setattr(ws._metaschema, "publish_schema_rows", fail_publish)
        with pytest.raises(OSError, match="cannot publish schema"):
            ws.register(replacement, allow_replace=True)
        assert _fingerprints(tmp_path, initial) == old
        _check_queries(ws, initial)
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_registration_retry_when_only_the_auxiliary_index_committed(tmp_path, monkeypatch, reopen):
    # The analytics route is staged before main and commits before its injected failure.
    secondary = "analytics"
    initial, intended = _entity(2, secondary), _entity(3, secondary)
    ws = _open(tmp_path, "r8sql-split-retry", secondary)
    try:
        ws.register(initial, catalog="identity")
        ws.upsert(initial, {"object_id": "x", "primary_tags": ["apple"], "secondary_tags": ["pear"]})
        old_main, old_secondary = _fingerprints(tmp_path, initial, secondary)
        publish = ws._metaschema.publish_schema_rows
        armed = False

        def publish_and_arm(entity):
            nonlocal armed
            publish(entity)
            armed = entity is intended

        def fail_commit(_connection):
            nonlocal armed
            if armed:
                armed = False
                raise OSError("main commit failed")

        engine = ws.backends.get("main").db.engine
        with monkeypatch.context() as patch:
            patch.setattr(ws._metaschema, "publish_schema_rows", publish_and_arm)
            event.listen(engine, "commit", fail_commit)
            try:
                with pytest.raises(OSError, match="main commit failed"):
                    ws.register(intended, catalog="full", allow_replace=True)
            finally:
                event.remove(engine, "commit", fail_commit)
        main, side = _fingerprints(tmp_path, intended, secondary)
        assert main == old_main
        assert side != old_secondary

        if reopen:
            ws.release()
            ws = _open(tmp_path, "r8sql-split-retry", secondary)
            with pytest.raises(SparseGramIndexConflictError, match="Register the previous entity definition"):
                ws.register(intended, catalog="full", allow_replace=True)
            ws.register(initial, catalog="identity")
            assert _fingerprints(tmp_path, initial, secondary) == [old_main, old_secondary]

        ws.register(intended, catalog="full", allow_replace=True)
        new_main, new_secondary = _fingerprints(tmp_path, intended, secondary)
        assert new_main != old_main
        assert new_secondary == side
        _check_queries(ws, intended)
    finally:
        ws.release()
