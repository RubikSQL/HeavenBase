"""A query match whose follow-up row read misses on Pinecone is read again, and kept.

Query hydration reads the matched rows by id after the backend query. A row that no
route returns there is left out only when some route shows it is absent. A Pinecone
fetch can miss a record written recently, so Pinecone fetches a missed match again
within a short bound: a record found then is hydrated from that fetch, and a match
still missing keeps the values the query returned, because a miss never shows that
the record was deleted.
"""

import time

import pytest

import heavenbase as hb
from test_pinecone_records import _workspace

pytestmark = pytest.mark.fast


@pytest.fixture
def notes(monkeypatch):
    """Notes stored only on Pinecone; ``n2`` has no summary, so a near query hydrates its rows."""
    ws, indexes = _workspace(monkeypatch, "pinecone-query-hydration")

    class Note(hb.Entity):
        identifier = "pinecone-query-hydration-note"

        title = hb.field(hb.ShortText).store(to="vec")
        summary = hb.field(hb.ShortText).store(to="vec").optional()
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    try:
        ws.register(Note)
        ws.upsert_many(
            Note,
            [
                {"object_id": "n1", "title": "first", "summary": "s1", "embedding": [1, 0]},
                {"object_id": "n2", "title": "second", "embedding": [0.9, 0.1]},
            ],
        )
        [index] = indexes.values()
        yield ws, index, Note
    finally:
        ws.release()


def test_near_match_survives_a_missed_hydration_fetch(notes):
    ws, index, Note = notes
    query = ws.query(Note).near(Note.embedding, [1, 0], top_k=2)
    assert query.execute().ids == ["n1", "n2"]

    index.stale.add("n2")
    rows = query.execute().rows()

    assert [(row["object_id"], row["title"], row["summary"]) for row in rows] == [("n1", "first", "s1"), ("n2", "second", None)]
    assert not index.stale  # the hydration fetch did miss n2


def test_near_match_of_a_deleted_row_is_left_out_when_another_route_settles_absence(monkeypatch):
    """With an InMem route beside Pinecone, a row no route holds any more is dropped from hydration."""
    ws, indexes = _workspace(monkeypatch, "pinecone-query-hydration-mixed")

    class Doc(hb.Entity):
        identifier = "pinecone-query-hydration-doc"

        title = hb.field(hb.ShortText)
        summary = hb.field(hb.ShortText).optional()
        embedding = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex.identifier)

    try:
        ws.register(Doc)
        ws.upsert_many(Doc, [{"object_id": "a", "title": "kept", "embedding": [1, 0]}, {"object_id": "b", "title": "gone", "embedding": [0.9, 0.1]}])
        store = ws._row_store
        original_rows_for_ids = store.rows_for_ids

        def rows_after_b_is_deleted(entity_id, row_ids, **kwargs):
            # the row is deleted between the backend query and the hydration read
            rows = original_rows_for_ids(entity_id, row_ids, **kwargs)
            rows["b"] = []
            return rows

        monkeypatch.setattr(store, "rows_for_ids", rows_after_b_is_deleted)
        assert ws.query(Doc).near(Doc.embedding, [1, 0], top_k=2).execute().ids == ["a"]
    finally:
        ws.release()


@pytest.mark.slow
def test_lagging_hydration_fetch_is_retried_until_a_later_fetch_returns_the_record(notes):
    """A record the query returned but a lagging fetch missed is hydrated from a later fetch."""
    ws, index, Note = notes
    index.report_lsn = True
    before = index.lsn
    ws.upsert(Note, {"object_id": "n3", "title": "third", "embedding": [0.8, 0.2]})
    index.lag_fetches(2, view=before)  # the next two fetches read the index as it was before n3

    rows = ws.query(Note).near(Note.embedding, [0.8, 0.2], top_k=1).execute().rows()

    assert [(row["object_id"], row["title"], row["embedding"]) for row in rows] == [("n3", "third", pytest.approx([0.8, 0.2]))]


@pytest.mark.slow
def test_a_match_deleted_after_the_query_keeps_the_query_values(notes, monkeypatch):
    """A miss never counts as a deletion: the match keeps the query's values, as in 0.1.2.2."""
    ws, index, Note = notes
    index.report_lsn = True
    store = ws._row_store
    original_rows_for_ids = store.rows_for_ids

    def rows_after_n2_is_deleted(entity_id, row_ids, **kwargs):
        index.delete(ids=["n2"])  # another client deletes n2 between the query and the hydration read
        return original_rows_for_ids(entity_id, row_ids, **kwargs)

    monkeypatch.setattr(store, "rows_for_ids", rows_after_n2_is_deleted)
    started = time.monotonic()
    rows = ws.query(Note).near(Note.embedding, [1, 0], top_k=2).execute().rows()

    assert time.monotonic() - started < 3  # a fetch at the query's reported view stops the wait
    assert [(row["object_id"], row["title"], row["embedding"]) for row in rows] == [("n1", "first", pytest.approx([1.0, 0.0])), ("n2", "second", None)]


@pytest.mark.slow
def test_a_live_match_newer_than_the_reported_query_view_is_kept(notes, monkeypatch):
    """A query can return a write newer than the LSN it reports; a fetch at that LSN misses the live record."""
    ws, index, Note = notes
    index.report_lsn = True
    indexed = index.lsn
    ws.upsert(Note, {"object_id": "n3", "title": "third", "embedding": [0.8, 0.2]})
    original_query = index.query

    def query_reporting_the_indexed_view(**kwargs):
        response = original_query(**kwargs)
        response.update(index._headers(indexed))
        return response

    monkeypatch.setattr(index, "query", query_reporting_the_indexed_view)
    index.lag_fetches(1000, view=indexed)

    rows = ws.query(Note).near(Note.embedding, [0.8, 0.2], top_k=1).execute().rows()

    assert "n3" in index.records
    assert [(row["object_id"], row["title"]) for row in rows] == [("n3", "third")]


@pytest.mark.slow
def test_an_unsettled_miss_is_retried_until_the_bound_then_keeps_the_query_values(notes):
    """While fetches lag behind the query's view, the record is fetched again until the configured bound expires."""
    ws, index, Note = notes
    index.report_lsn = True
    ws.backends.get("vec").miss_settle_seconds = 0.3
    before = index.lsn
    ws.upsert(Note, {"object_id": "n3", "title": "third", "embedding": [0.8, 0.2]})
    index.lag_fetches(1000, view=before)
    fetches = index.fetches

    started = time.monotonic()
    rows = ws.query(Note).near(Note.embedding, [0.8, 0.2], top_k=1).execute().rows()
    elapsed = time.monotonic() - started

    # The hydration fetch, then a retry at once and after 0.05 s (and after 0.1 s more, when the host is not slow);
    # the next wait would pass the 0.3 s bound, so the loop stops although 1000 lagging fetches remain.
    assert 3 <= index.fetches - fetches <= 4
    assert 0.05 <= elapsed < 3
    assert [(row["object_id"], row["title"], row["embedding"]) for row in rows] == [("n3", "third", None)]


@pytest.mark.slow
def test_a_failed_retry_keeps_the_query_values_and_the_client(notes, monkeypatch):
    """A timeout on a retry fetch neither fails the query nor drops the Pinecone client."""
    ws, index, Note = notes
    index.stale.add("n2")
    original_fetch = index.fetch
    calls = []

    def fetch_that_times_out_on_the_retry(ids):
        calls.append(list(ids))
        if len(calls) > 1:
            raise TimeoutError("Pinecone fetch timed out")
        return original_fetch(ids)

    monkeypatch.setattr(index, "fetch", fetch_that_times_out_on_the_retry)

    rows = ws.query(Note).near(Note.embedding, [1, 0], top_k=2).execute().rows()

    assert len(calls) == 2  # the hydration fetch missed n2, and its retry timed out
    assert [(row["object_id"], row["title"], row["summary"]) for row in rows] == [("n1", "first", "s1"), ("n2", "second", None)]
    backend = ws.backends.get("vec")
    assert backend.client is not None and backend.client_error == ""
    monkeypatch.setattr(index, "fetch", original_fetch)
    assert ws.get("n2", entity=Note)["title"] == "second"
