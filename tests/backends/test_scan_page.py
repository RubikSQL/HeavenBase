"""``Backend.scan_page`` returns detached keyset pages without holding a backend lock across them."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import heavenbase as hb
from heavenbase.backends.base import Backend, is_portable_layer

pytestmark = [pytest.mark.fast, pytest.mark.unit]

ENTITY = "page-doc"


class PageDoc(hb.Entity):
    identifier = ENTITY

    title = hb.field(hb.ShortText)
    tags = hb.field(hb.Array[hb.ShortText]).store(strategy=hb.SideTable, optional=True)


def _rows(count: int) -> list[dict]:
    return [{"object_id": f"p{index:02}", "title": f"title {index}", "tags": ["shared", str(index)]} for index in range(count)]


def _pages(backend, *, batch_size: int, columns=None) -> list[list[dict]]:
    pages, after = [], None
    while page := backend.scan_page(ENTITY, after=after, batch_size=batch_size, columns=columns):
        pages.append(page)
        after = page[-1]["object_id"]
    return pages


class UnorderedBackend(Backend):
    """Provider whose bounded scan has no order and keeps the lock across yields, like a client-side iterator."""

    identifier = "unordered"

    def __init__(self):
        super().__init__("unordered")
        self.rows = {row["object_id"]: row for row in reversed(_rows(7))}
        self.scans = 0
        self.closed = 0

    def iter_scan(self, entity_id, *, batch_size, columns=None):
        self.scans += 1
        with self._lock:
            try:
                for row in self.rows.values():
                    yield dict(row)
            finally:
                self.closed += 1


class LockProbeIterBackend(Backend):
    """Records whether the backend lock is held while its own ``iter_scan`` runs."""

    identifier = "lock-probe-iter"

    def __init__(self):
        super().__init__("lock-probe-iter")
        self.owned = []

    def iter_scan(self, entity_id, *, batch_size, columns=None):
        self.owned.append(self._lock._is_owned())
        yield from (dict(row) for row in _rows(3))


class LockProbeScanBackend(Backend):
    """A provider written for 0.1.2.2 that records whether the backend lock is held while its ``scan`` runs."""

    identifier = "lock-probe-scan"

    def __init__(self):
        super().__init__("lock-probe-scan")
        self.owned = []

    def scan(self, entity_id):
        self.owned.append(self._lock._is_owned())
        return _rows(3)


def test_portable_scan_page_holds_the_lock_only_around_a_provider_iter_scan():
    # an offset-paged iter_scan sees no write of this backend while one page is enumerated
    with_iter = LockProbeIterBackend()
    assert [row["object_id"] for row in with_iter.scan_page(ENTITY, after=None, batch_size=2)] == ["p00", "p01"]
    assert with_iter.owned == [True]
    # a scan is called as 0.1.2.2 called it, so it may hand reads of this backend to other threads
    with_scan = LockProbeScanBackend()
    assert [row["object_id"] for row in with_scan.scan_page(ENTITY, after="p00", batch_size=2)] == ["p01", "p02"]
    assert with_scan.owned == [False]


def test_portable_scan_page_orders_and_pages_an_unordered_scan_and_closes_it():
    backend = UnorderedBackend()

    pages = _pages(backend, batch_size=3)

    assert [[row["object_id"] for row in page] for page in pages] == [["p00", "p01", "p02"], ["p03", "p04", "p05"], ["p06"]]
    assert backend.scan_page(ENTITY, after="p06", batch_size=3) == []
    # every page, the empty one included, is one complete enumeration whose generator is closed, so no lock outlives the call
    assert backend.scans == backend.closed == 5
    assert not backend._lock._is_owned()
    with pytest.raises(ValueError, match="batch_size must be a positive integer"):
        backend.scan_page(ENTITY, after=None, batch_size=0)


def test_inmem_scan_page_slices_sorted_ids_and_releases_the_lock_before_returning():
    backend = hb.backends.InMemBackend("main")
    ws = hb.HeavenBase("scan-page-inmem", detached=True, backends={"main": backend})
    ws.register(PageDoc)
    ws.upsert_many(PageDoc, list(reversed(_rows(5))))

    pages = _pages(backend, batch_size=2, columns=["title"])

    assert pages == [
        [{"object_id": "p00", "title": "title 0"}, {"object_id": "p01", "title": "title 1"}],
        [{"object_id": "p02", "title": "title 2"}, {"object_id": "p03", "title": "title 3"}],
        [{"object_id": "p04", "title": "title 4"}],
    ]
    assert backend.scan_page(ENTITY, after="p01", batch_size=10)[0]["object_id"] == "p02"
    assert backend.scan_page("unknown", after=None, batch_size=2) == []
    taken = []

    def probe_lock():
        taken.append(backend._lock.acquire(timeout=2))
        if taken[-1]:
            backend._lock.release()

    thread = threading.Thread(target=probe_lock, daemon=True)
    thread.start()
    thread.join(5)
    assert taken == [True]


def test_inmem_scan_page_sorts_once_per_id_set_and_sees_every_change(monkeypatch):
    import heavenbase.backends.inmem.backend as inmem_module

    sorts = []

    def counting_sorted(values, *args, **kwargs):
        sorts.append(1)
        return sorted(values, *args, **kwargs)

    monkeypatch.setattr(inmem_module, "sorted", counting_sorted, raising=False)
    backend = hb.backends.InMemBackend("main")
    ws = hb.HeavenBase("scan-page-inmem-sort", detached=True, backends={"main": backend})
    ws.register(PageDoc)
    ws.upsert_many(PageDoc, list(reversed(_rows(7))))

    def ids():
        return [row["object_id"] for page in _pages(backend, batch_size=2) for row in page]

    assert ids() == [f"p{index:02}" for index in range(7)]
    assert len(sorts) == 1  # four pages and the empty end page share one sort
    ws.upsert(PageDoc, {"object_id": "p03", "title": "changed"})
    assert ids() == [f"p{index:02}" for index in range(7)] and len(sorts) == 1  # same ids, no new sort
    ws.upsert(PageDoc, {"object_id": "p99", "title": "new"})
    ws.delete((PageDoc, "p00"))
    assert ids() == [f"p{index:02}" for index in range(1, 7)] + ["p99"]
    # A direct write that keeps the row count but swaps an id is still seen.
    bucket = backend.data[backend.ws_id][ENTITY]
    bucket["p50"] = {**bucket.pop("p01"), "object_id": "p50"}
    assert ids() == ["p02", "p03", "p04", "p05", "p06", "p50", "p99"]
    backend.drop()
    assert ids() == []


class CountingBucket(dict):
    """Row bucket that counts whole-id-set passes made through ``keys()``."""

    passes = 0

    def keys(self):
        self.passes += 1
        return super().keys()


def test_inmem_keyset_scan_checks_the_id_set_once_per_enumeration_and_sees_writes_between_pages():
    backend = hb.backends.InMemBackend("main")
    ws = hb.HeavenBase("scan-page-inmem-linear", detached=True, backends={"main": backend})
    ws.register(PageDoc)
    ws.upsert_many(PageDoc, _rows(40))
    bucket = backend.data[backend.ws_id][ENTITY] = CountingBucket(backend.data[backend.ws_id][ENTITY])

    pages = _pages(backend, batch_size=3)

    assert [row["object_id"] for page in pages for row in page] == [f"p{index:02}" for index in range(40)]
    # fourteen pages and the empty end page: only the first one compares the whole id set, so a keyset scan is linear
    assert bucket.passes <= 1
    # writes between pages of one enumeration: ids added ahead of the cursor are seen, removed ones are not
    seen = [row["object_id"] for row in backend.scan_page(ENTITY, after=None, batch_size=3)]
    ws.upsert(PageDoc, {"object_id": "p05a", "title": "new"})
    ws.delete((PageDoc, "p07"))
    seen += [row["object_id"] for row in backend.scan_page(ENTITY, after=seen[-1], batch_size=3)]
    # a direct write that swaps an id behind the cursor for one ahead of it keeps the id count
    bucket["p00a"] = {**bucket.pop("p12"), "object_id": "p00a"}
    while page := backend.scan_page(ENTITY, after=seen[-1], batch_size=3):
        seen.extend(row["object_id"] for row in page)
    expected = [f"p{index:02}" for index in range(40) if index not in (7, 12)]
    assert seen == expected[:6] + ["p05a"] + expected[6:]
    ws.release()


def test_sql_scan_pages_are_the_rows_that_iter_scan_and_scan_produce(tmp_path):
    backend = hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / "pages.db"))
    ws = hb.HeavenBase("scan-page-sql", detached=True, backends={"main": backend})
    ws.register(PageDoc)
    ws.upsert_many(PageDoc, _rows(7))

    full = _pages(backend, batch_size=3)
    projected = _pages(backend, batch_size=4, columns=["tags"])

    assert [len(page) for page in full] == [3, 3, 1]
    assert [row for page in full for row in page] == list(backend.iter_scan(ENTITY, batch_size=3)) == backend.scan(ENTITY)
    assert [row for page in projected for row in page] == list(backend.iter_scan(ENTITY, batch_size=4, columns=["tags"]))
    assert projected[0][0] == {"object_id": "p00", "tags": ["shared", "0"]}
    assert backend.scan_page(ENTITY, after="p06", batch_size=3) == []
    assert backend.scan_page("unknown", after=None, batch_size=3) == []
    # a name the backend does not store is left out, as the Backend contract states
    assert backend.scan_page(ENTITY, after=None, batch_size=2, columns=["tags", "missing"]) == projected[0][:2]
    # page size and cursor are bind parameters: one construct per selection, whatever the page size
    shapes = {shape for entity, shape in backend._constructs if entity == ENTITY and shape.startswith("scan_page")}
    assert shapes == {"scan_page", "scan_page_after", "scan_page:object_id,tags", "scan_page_after:object_id,tags"}
    ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("factory", [hb.backends.JsonFileBackend, hb.backends.PickleFileBackend], ids=["json", "pickle"])
def test_file_backends_hydrate_record_files_before_paging(tmp_path, factory):
    """A cold file backend loads the entity folder for ``scan_page`` and ``iter_scan``, so ``iter_rows`` sees every stored row."""
    uri = hb.utils.path_to_file_uri(tmp_path / "records")
    ids = [row["object_id"] for row in _rows(5)]
    writer = factory("main", uri, ws_id="file-pages")
    writer.upsert([hb.ext.RowOp(ENTITY, row["object_id"], row) for row in reversed(_rows(5))])

    cold = factory("main", uri, ws_id="file-pages")
    assert [[row["object_id"] for row in page] for page in _pages(cold, batch_size=2)] == [["p00", "p01"], ["p02", "p03"], ["p04"]]
    assert _pages(factory("main", uri, ws_id="file-pages"), batch_size=3, columns=["title"])[0] == [
        {"object_id": "p00", "title": "title 0"},
        {"object_id": "p01", "title": "title 1"},
        {"object_id": "p02", "title": "title 2"},
    ]
    assert sorted(row["object_id"] for row in factory("main", uri, ws_id="file-pages").iter_scan(ENTITY, batch_size=2)) == ids
    assert cold.scan_page("unknown", after=None, batch_size=2) == [] and list(cold.iter_scan("unknown", batch_size=2)) == []
    with pytest.raises(ValueError, match="batch_size must be a positive integer"):
        cold.scan_page(ENTITY, after=None, batch_size=0)

    backend = factory("main", uri)
    ws = hb.HeavenBase("scan-page-file", detached=True, backends={"main": backend})
    ws.register(PageDoc)
    ws.upsert_many(PageDoc, _rows(5))
    for _ in range(2):
        backend.refresh()  # models a fresh process: nothing cached, every row on disk
        assert [row["object_id"] for row in ws.iter_rows(PageDoc, batch_size=2)] == ids
        assert ws.count(PageDoc) == len(ws.rows(PageDoc)) == 5
    # a record written by another process is seen by the next enumeration without an explicit refresh
    other = factory("main", uri, ws_id=backend.ws_id)
    other.upsert([hb.ext.RowOp(ENTITY, "p99", {"object_id": "p99", "title": "title 99", "tags": ["shared", "99"]})])
    assert [row["object_id"] for row in ws.iter_rows(PageDoc, batch_size=2)] == ids + ["p99"]
    ws.release()


class EndScanRecordingBackend(hb.backends.InMemBackend):
    """In-memory provider that records the scans ``iter_rows`` ends early."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ended = []

    def end_scan(self, entity_id, *, after):
        self.ended.append((entity_id, after))


def test_iter_rows_ends_a_scan_closed_before_its_last_page():
    backend = EndScanRecordingBackend("main")
    ws = hb.HeavenBase("scan-page-end-scan", detached=True, backends={"main": backend})
    try:
        ws.register(PageDoc)
        ws.upsert_many(PageDoc, _rows(5))

        assert len(list(ws.iter_rows(PageDoc, batch_size=2))) == 5
        assert backend.ended == []

        rows = ws.iter_rows(PageDoc, batch_size=2)
        assert next(iter(rows))["object_id"] == "p00"
        rows.close()
        assert backend.ended == [(ENTITY, "p01")]
        # the portable default keeps no state and accepts the call
        assert Backend.end_scan(backend, ENTITY, after="p01") is None
    finally:
        ws.release()


class SplitPageDoc(hb.Entity):
    identifier = "split-page-doc"

    body = hb.field(hb.ShortText)
    title = hb.field(hb.ShortText).store(to="zaux")


class FailingEndScanBackend(EndScanRecordingBackend):
    """Recording provider whose ``end_scan`` fails after recording the call."""

    def end_scan(self, entity_id, *, after):
        super().end_scan(entity_id, after=after)
        raise RuntimeError(f"end_scan failed on {self.name}")


def test_iter_rows_ends_every_route_of_a_merged_scan_closed_before_its_last_page():
    """A multi-route merge closed early ends each route's scan after that route's latest page, even if one end fails."""
    main, zaux = EndScanRecordingBackend("main"), FailingEndScanBackend("zaux")
    ws = hb.HeavenBase("scan-page-end-scan-routes", detached=True, backends={"main": main, "zaux": zaux})
    try:
        ws.register(SplitPageDoc)
        ws.upsert_many(SplitPageDoc, [{"object_id": f"p{index:02}", "body": "b", "title": f"title {index}"} for index in range(5)])
        assert ws._row_store.routed_backend_names(SplitPageDoc.identifier) == ["main", "zaux"]

        assert [row["object_id"] for row in ws.iter_rows(SplitPageDoc, batch_size=2)] == [f"p{index:02}" for index in range(5)]
        assert main.ended == zaux.ended == []

        rows = ws.iter_rows(SplitPageDoc, batch_size=2)
        assert [next(rows)["object_id"] for _ in range(3)] == ["p00", "p01", "p02"]
        with pytest.raises(RuntimeError, match="end_scan failed on zaux"):
            rows.close()
        assert main.ended == zaux.ended == [(SplitPageDoc.identifier, "p03")]
    finally:
        ws.release()


class _KeysetMilvusClient:
    """Fake MilvusClient whose query iterator, like the server's, returns rows in primary-key order."""

    def __init__(self, **kwargs):
        self.rows = {}
        self.iterators = []
        self.closed = 0

    def has_collection(self, name):
        return True

    def query_iterator(self, *, collection_name, batch_size, filter, output_fields, consistency_level):
        self.iterators.append({"filter": filter, "batch_size": batch_size, "consistency_level": consistency_level})
        after = None
        if filter:
            prefix = 'object_id > "'
            assert filter.startswith(prefix) and filter.endswith('"'), filter
            after = filter[len(prefix) : -1].replace('\\"', '"').replace("\\\\", "\\")
        ordered = [dict(self.rows[key]) for key in sorted(self.rows) if after is None or key > after]
        client = self

        class Iterator:
            def next(self):
                nonlocal ordered
                batch, ordered = ordered[:batch_size], ordered[batch_size:]
                return batch

            def close(self):
                client.closed += 1

        return Iterator()


def test_milvus_scan_page_is_one_keyset_iterator_batch(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "pymilvus", type("FakePymilvus", (), {"MilvusClient": _KeysetMilvusClient}))
    backend = hb.backends.MilvusBackend("milvus", uri="mock://milvus")
    ids = ["b", 'q"uote', "a", "é", "c\\d", "z"]
    backend.client.rows = {object_id: {"object_id": object_id, "title": object_id.upper()} for object_id in ids}

    pages = _pages(backend, batch_size=2, columns=["title"])

    assert [row["object_id"] for page in pages for row in page] == sorted(ids)
    assert pages[0] == [{"object_id": "a", "title": "A"}, {"object_id": "b", "title": "B"}]
    # three full pages and the empty end page: one iterator each, all closed, no lock outlives a call
    assert len(backend.client.iterators) == backend.client.closed == 4
    assert backend.client.iterators[0]["filter"] == ""
    assert backend.client.iterators[2]["filter"] == 'object_id > "q\\"uote"'
    assert {item["consistency_level"] for item in backend.client.iterators} == {"Strong"}
    assert not backend._lock._is_owned()
    with pytest.raises(ValueError, match="batch_size must be a positive integer"):
        backend.scan_page(ENTITY, after=None, batch_size=0)


_MILVUS_LEXER_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "a": "\a"}


def _milvus_key_bounds(expr: str) -> list[str]:
    """Read the leading ``object_id > "..."`` clauses of an expression as the Milvus server lexer reads them.

    A double-quoted literal takes every character raw except a quote, a backslash, LF and CR,
    and a backslash starts a Go-style escape; any other expression is accepted for every row.
    """
    prefix = 'object_id > "'
    bounds = []
    while expr.startswith(prefix):
        text, index = [], len(prefix)
        while (char := expr[index : index + 1]) != '"':
            if char in ("", "\n", "\r"):
                raise ValueError(f"unterminated string literal in {expr!r}")
            if char == "\\":
                index += 1
                if expr[index : index + 1] not in _MILVUS_LEXER_ESCAPES:
                    raise ValueError(f"unknown escape sequence in {expr!r}")
                char = _MILVUS_LEXER_ESCAPES[expr[index]]
            text.append(char)
            index += 1
        bounds.append("".join(text))
        rest = expr[index + 1 :]
        if rest and not (rest.startswith(" and (") and rest.endswith(")")):
            raise ValueError(f"syntax error at {rest!r}")
        expr = rest[len(" and (") : -1]
    return bounds


class _RawCursorMilvusClient(_KeysetMilvusClient):
    """Server-like fake whose iterator writes its next cursor unescaped, as pymilvus 3.0's QueryIterator does."""

    def query_iterator(self, *, collection_name, batch_size, filter, output_fields, consistency_level):
        self.iterators.append({"filter": filter, "batch_size": batch_size, "consistency_level": consistency_level})
        client = self

        class Iterator:
            last = None
            calls = 0

            def next(self):
                self.calls += 1
                if self.calls > len(client.rows) + 1:
                    raise RuntimeError("the iterator cursor does not advance")
                expr = filter if self.last is None else f'object_id > "{self.last}"' + (f" and ({filter})" if filter else "")
                bounds = _milvus_key_bounds(expr)
                batch = [dict(client.rows[key]) for key in sorted(client.rows) if all(key > bound for bound in bounds)][:batch_size]
                if batch:
                    self.last = batch[-1]["object_id"]
                return batch

            def close(self):
                client.closed += 1

        return Iterator()


def test_milvus_server_scan_restarts_its_iterator_after_a_key_the_cursor_cannot_quote(monkeypatch):
    """A batch ending at a key with a quote, backslash, LF or CR continues on a new, escaped iterator, so no row is lost or repeated."""
    import sys

    monkeypatch.setitem(sys.modules, "pymilvus", type("FakePymilvus", (), {"MilvusClient": _RawCursorMilvusClient}))
    backend = hb.backends.MilvusBackend("milvus", uri="mock://milvus")
    ids = ["a", 'q"uote', "b", "c\\bd", "d", "e\nf", "g", "h\\", "i", "cr\rx", "é"]
    backend.client.rows = {object_id: {"object_id": object_id, "title": "t"} for object_id in ids}

    assert [row["object_id"] for row in backend.iter_scan(ENTITY, batch_size=1)] == sorted(ids)
    # one iterator, plus one after each of the five keys that need escaping
    assert len(backend.client.iterators) == backend.client.closed == 6
    assert backend.client.iterators[1]["filter"] == 'object_id > "c\\\\bd"'
    assert not backend._lock._is_owned()

    backend.client.iterators.clear()
    batches = backend._key_ordered_batches(backend._collection(ENTITY), filter_expr='title != ""', batch_size=2)
    assert [row["object_id"] for batch in batches for row in batch] == sorted(ids)
    assert [item["filter"] for item in backend.client.iterators] == [
        'title != ""',
        'object_id > "cr\\rx" and (title != "")',
        'object_id > "e\\nf" and (title != "")',
        'object_id > "h\\\\" and (title != "")',
        'object_id > "q\\"uote" and (title != "")',
    ]


class _CappedMilvusClient(_KeysetMilvusClient):
    """Fake MilvusClient whose query iterator, like pymilvus 3, rejects a batch above 16384 rows."""

    def query_iterator(self, *, batch_size, **kwargs):
        if batch_size > 16384:
            raise ValueError("batch size cannot be larger than 16384")
        return super().query_iterator(batch_size=batch_size, **kwargs)


def test_milvus_server_enumeration_caps_batch_at_the_iterator_maximum(monkeypatch):
    """A batch_size above the pymilvus iterator maximum is capped, not rejected, on every server enumeration."""
    import sys

    monkeypatch.setitem(sys.modules, "pymilvus", type("FakePymilvus", (), {"MilvusClient": _CappedMilvusClient}))
    backend = hb.backends.MilvusBackend("milvus", uri="mock://milvus")
    ids = [f"k{index:02}" for index in range(5)]
    backend.client.rows = {object_id: {"object_id": object_id, "title": object_id.upper()} for object_id in ids}

    assert [row["object_id"] for row in backend.scan_page(ENTITY, after=None, batch_size=20000)] == ids
    assert [row["object_id"] for row in backend.scan_page(ENTITY, after="k02", batch_size=20000, columns=["title"])] == ids[3:]
    assert [row["object_id"] for row in backend.iter_scan(ENTITY, batch_size=20000)] == ids

    configured = type("Config", (), {"get": staticmethod(lambda key, default=None: 20000 if key.endswith(".scan_limit") else default)})()
    monkeypatch.setattr(hb.backends.MilvusBackend, "config", property(lambda self: configured))
    assert [row["object_id"] for row in backend.scan(ENTITY)] == ids

    assert {item["batch_size"] for item in backend.client.iterators} == {16384}
    assert len(backend.client.iterators) == backend.client.closed


def test_real_pymilvus_server_iterator_accepts_a_batch_above_its_maximum(tmp_path):
    """The server enumeration path, run on a real pymilvus client, takes batch_size 20000."""
    pytest.importorskip("pymilvus")
    pytest.importorskip("milvus_lite")
    from pymilvus.orm.constants import MAX_BATCH_SIZE

    from heavenbase.backends.vector import milvus

    backend = hb.backends.MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "capped.db"))
    ws = hb.HeavenBase("scan-page-milvus-capped", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    try:
        ws.register(LitePageDoc)
        ids = [f"p{index:02}" for index in range(5)]
        ws.upsert_many(LitePageDoc, [{"object_id": object_id, "title": f"title {object_id}", "embedding": [1.0, 0.5]} for object_id in ids])
        # MilvusLiteBackend lists keys instead; run the server's query-iterator path on this real client
        batches = milvus.MilvusBackend._key_ordered_batches(backend, backend._collection(LitePageDoc.identifier), filter_expr="", batch_size=20000)
        try:
            assert sorted(row["object_id"] for batch in batches for row in batch) == ids
        finally:
            batches.close()
    finally:
        ws.release()
    assert milvus.MilvusBackend._max_iterator_batch_size == MAX_BATCH_SIZE


class LitePageDoc(hb.Entity):
    identifier = "lite-page-doc"

    title = hb.field(hb.ShortText).store(to="vec")
    embedding = hb.field(hb.Vector[2]).required().store(to="vec", strategy=hb.VectorIndex)


def test_real_milvus_lite_scan_page_matches_python_order(tmp_path):
    """The embedded engine orders varchar keys as Python orders strings, so keyset pages are exhaustive."""
    pytest.importorskip("pymilvus")
    pytest.importorskip("milvus_lite")
    backend = hb.backends.MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "pages.db"))
    ws = hb.HeavenBase("scan-page-milvus-lite", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    try:
        ws.register(LitePageDoc)
        ids = [f"p{index:02}" for index in range(11)] + ["Zeta", "alpha", "é-accent", "日本", 'q"uote']
        ws.upsert_many(LitePageDoc, [{"object_id": object_id, "title": f"title {object_id}", "embedding": [1.0, 0.5]} for object_id in ids])
        pages, after = [], None
        while page := backend.scan_page(LitePageDoc.identifier, after=after, batch_size=3, columns=["title"]):
            pages.append(page)
            after = page[-1]["object_id"]
        assert [row["object_id"] for page in pages for row in page] == sorted(ids)
        assert all(len(page) <= 3 for page in pages)
        assert pages[0][0] == {"object_id": "Zeta", "title": "title Zeta"}
        assert [row["object_id"] for row in ws.iter_rows(LitePageDoc, batch_size=4)] == sorted(ids)
    finally:
        ws.release()


# Every C0 control character Milvus Lite cannot take as a JSON escape (``\\b``, ``\\f``, ``\\u00XX``) is here too.
ESCAPED_IDS = [
    "plain",
    "it's",
    'dq"x',
    "back\\slash",
    "a\\nb",
    "new\nline",
    "cr\rx",
    "tab\tx",
    "end\\",
    "é-日本",
    "nul\x00x",
    "soh\x01x",
    "bs\bx",
    "vt\vx",
    "ff\fx",
    "us\x1fx",
    "del\x7fx",
    "ls\u2028x",
]


def _lite_pages(backend, entity_id: str, *, batch_size: int) -> list[str]:
    keys, after = [], None
    while page := backend.scan_page(entity_id, after=after, batch_size=batch_size):
        keys += [row["object_id"] for row in page]
        after = page[-1]["object_id"]
    return keys


def test_real_milvus_lite_reads_and_deletes_object_ids_that_need_escaping(tmp_path):
    """A quote, backslash or control character in an ``object_id`` neither breaks nor changes a Lite read or delete."""
    pytest.importorskip("pymilvus")
    pytest.importorskip("milvus_lite")
    backend = hb.backends.MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "escape.db"))
    ws = hb.HeavenBase("scan-page-milvus-lite-escape", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    try:
        ws.register(LitePageDoc)
        ws.upsert_many(LitePageDoc, [{"object_id": object_id, "title": object_id, "embedding": [1.0, 0.5]} for object_id in ESCAPED_IDS])
        expected = sorted(ESCAPED_IDS)

        assert sorted(row["object_id"] for row in backend.scan(LitePageDoc.identifier)) == expected
        assert [row["object_id"] for row in backend.iter_scan(LitePageDoc.identifier, batch_size=2)] == expected
        assert _lite_pages(backend, LitePageDoc.identifier, batch_size=2) == expected
        assert [row["object_id"] for row in ws.iter_rows(LitePageDoc, batch_size=3)] == expected
        rows = backend.get_many(LitePageDoc.identifier, [*ESCAPED_IDS, "missing"])
        assert [row and (row["object_id"], row["title"]) for row in rows] == [(object_id, object_id) for object_id in ESCAPED_IDS] + [None]
        assert backend.exists_many(LitePageDoc.identifier, ["it's", "a\\nb", "a\nb", "missing"]) == [True, True, False, False]
        # native filters compare the stored text exactly, quotes and control characters included
        for title in ESCAPED_IDS:
            assert ws.query(LitePageDoc).where(LitePageDoc.title == title).execute().ids == [title]

        deleted = {"it's", "back\\slash", "cr\rx", "nul\x00x", "bs\bx", "ff\fx"}
        for object_id in sorted(deleted):
            assert ws.delete((LitePageDoc, object_id)) is True
        assert sorted(row["object_id"] for row in backend.scan(LitePageDoc.identifier)) == sorted(set(ESCAPED_IDS) - deleted)
    finally:
        ws.release()


def test_real_milvus_lite_keyset_scan_lists_its_keys_once_and_sees_its_own_writes(tmp_path):
    """A full Lite keyset scan costs one key listing, not one per page, and a write between pages is seen."""
    pytest.importorskip("pymilvus")
    pytest.importorskip("milvus_lite")
    backend = hb.backends.MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "listing.db"))
    ws = hb.HeavenBase("scan-page-milvus-lite-listing", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    try:
        ws.register(LitePageDoc)
        ids = [f"k{index:03}" for index in range(40)]
        shuffled = ids[7:] + ids[:7]
        ws.upsert_many(LitePageDoc, [{"object_id": object_id, "title": object_id, "embedding": [1.0, 0.5]} for object_id in shuffled])
        listings = []
        query = backend.client.query

        def counting_query(**kwargs):
            if kwargs.get("output_fields") == ["object_id"]:
                listings.append(kwargs["filter"])
            return query(**kwargs)

        backend.client.query = counting_query
        assert _lite_pages(backend, LitePageDoc.identifier, batch_size=3) == ids
        assert len(listings) == 1
        listings.clear()
        assert [row["object_id"] for row in ws.iter_rows(LitePageDoc, batch_size=4)] == ids
        assert len(listings) == 1

        # two interleaved enumerations each keep their own listing
        first = backend.scan_page(LitePageDoc.identifier, after=None, batch_size=5)
        second = backend.scan_page(LitePageDoc.identifier, after=None, batch_size=7)
        assert backend.scan_page(LitePageDoc.identifier, after=first[-1]["object_id"], batch_size=5)[0]["object_id"] == "k005"
        assert backend.scan_page(LitePageDoc.identifier, after=second[-1]["object_id"], batch_size=7)[0]["object_id"] == "k007"

        # a write between pages drops the listing: the next page lists again from its cursor and sees it
        listings.clear()
        page = backend.scan_page(LitePageDoc.identifier, after=None, batch_size=10)
        ws.upsert(LitePageDoc, {"object_id": "k0105", "title": "late", "embedding": [0.0, 1.0]})
        ws.delete((LitePageDoc, "k011"))
        rest, after = [], page[-1]["object_id"]
        while more := backend.scan_page(LitePageDoc.identifier, after=after, batch_size=10):
            rest += [row["object_id"] for row in more]
            after = more[-1]["object_id"]
        assert [row["object_id"] for row in page] + rest == sorted({*ids, "k0105"} - {"k011"})
        assert listings == ['object_id != ""', 'object_id > "k009"']
    finally:
        ws.release()


def test_real_milvus_lite_keyset_scan_sees_a_write_through_another_backend_on_its_file(tmp_path):
    """A page sees a row written ahead of its cursor through a second backend that opened the same Lite file."""
    pytest.importorskip("pymilvus")
    pytest.importorskip("milvus_lite")
    uri = hb.utils.path_to_file_uri(tmp_path / "shared.db")
    reader = hb.backends.MilvusLiteBackend("vec", uri)
    writer = hb.backends.MilvusLiteBackend("vec", uri)
    read_ws = hb.HeavenBase("scan-page-milvus-lite-shared", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": reader})
    write_ws = hb.HeavenBase("scan-page-milvus-lite-shared", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": writer})
    try:
        read_ws.register(LitePageDoc)
        write_ws.register(LitePageDoc)
        assert reader.uri == writer.uri
        ids = [f"k{index:03}" for index in range(0, 100, 10)]
        read_ws.upsert_many(LitePageDoc, [{"object_id": object_id, "title": object_id, "embedding": [1.0, 0.5]} for object_id in ids])
        page = reader.scan_page(LitePageDoc.identifier, after=None, batch_size=3)
        seen = [row["object_id"] for row in page]
        write_ws.upsert(LitePageDoc, {"object_id": "k055", "title": "late", "embedding": [0.0, 1.0]})
        while page := reader.scan_page(LitePageDoc.identifier, after=seen[-1], batch_size=3):
            seen += [row["object_id"] for row in page]
        assert seen == sorted([*ids, "k055"])
    finally:
        write_ws.release()
        read_ws.release()


def test_real_milvus_lite_keyset_listing_is_dropped_when_a_scan_stops_early(tmp_path):
    """Closing ``iter_rows`` early, or a backend refresh, frees the key listing an unfinished enumeration kept."""
    pytest.importorskip("pymilvus")
    pytest.importorskip("milvus_lite")
    from heavenbase.backends.vector import milvus as milvus_module

    backend = hb.backends.MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "abandon.db"))
    ws = hb.HeavenBase("scan-page-milvus-lite-abandon", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    store = milvus_module._LITE_KEY_LISTINGS

    def open_listings():
        return [key for key in store._listings if key[0] == backend._listing_file()]

    try:
        ws.register(LitePageDoc)
        ws.upsert_many(LitePageDoc, [{"object_id": f"k{index:03}", "title": "t", "embedding": [1.0, 0.5]} for index in range(30)])

        for _ in ws.iter_rows(LitePageDoc, batch_size=4):
            break
        assert open_listings() == []

        rows = ws.iter_rows(LitePageDoc, batch_size=4)
        assert [next(rows)["object_id"] for _ in range(6)] == [f"k{index:03}" for index in range(6)]
        assert len(open_listings()) == 1
        rows.close()
        assert open_listings() == []

        assert [row["object_id"] for row in ws.iter_rows(LitePageDoc, batch_size=4)][-1] == "k029"
        assert open_listings() == []

        backend.scan_page(LitePageDoc.identifier, after=None, batch_size=4)
        assert len(open_listings()) == 1
        ws.refresh(backend="vec")
        assert open_listings() == []
    finally:
        ws.release()


class LiteSplitDoc(hb.Entity):
    identifier = "lite-split-doc"

    title = hb.field(hb.ShortText)
    embedding = hb.field(hb.Vector[2]).required().store(to="vec", strategy=hb.VectorIndex)


def test_real_milvus_lite_listing_follows_a_multi_route_merge_and_ends_with_it(tmp_path):
    """A merge with another route lists the Lite keys once, even using Lite pages only partway, and leaves no listing."""
    pytest.importorskip("pymilvus")
    pytest.importorskip("milvus_lite")
    from heavenbase.backends.vector import milvus as milvus_module

    backend = hb.backends.MilvusLiteBackend("vec", hb.utils.path_to_file_uri(tmp_path / "merge.db"))
    ws = hb.HeavenBase("scan-page-milvus-lite-merge", detached=True, backends={"main": hb.backends.InMemBackend("main"), "vec": backend})
    store = milvus_module._LITE_KEY_LISTINGS

    def open_listings():
        return [key for key in store._listings if key[0] == backend._listing_file()]

    try:
        ws.register(LiteSplitDoc)
        assert ws._row_store.routed_backend_names(LiteSplitDoc.identifier) == ["main", "vec"]
        ids = [f"k{index:03}" for index in range(30)]
        ws.upsert_many(LiteSplitDoc, [{"object_id": object_id, "title": object_id, "embedding": [1.0, 0.5]} for object_id in ids])
        listings = []
        query = backend.client.query

        def counting_query(**kwargs):
            if kwargs.get("output_fields") == ["object_id"]:
                listings.append(kwargs["filter"])
            return query(**kwargs)

        backend.client.query = counting_query
        assert [row["object_id"] for row in ws.iter_rows(LiteSplitDoc, batch_size=4)] == ids
        assert len(listings) == 1 and open_listings() == []

        rows = ws.iter_rows(LiteSplitDoc, batch_size=4)
        assert [next(rows)["object_id"] for _ in range(6)] == ids[:6]
        assert len(open_listings()) == 1
        rows.close()
        assert open_listings() == []

        # rows only the in-memory route holds make each step use the Lite page only partway
        extra = [f"{object_id}a" for object_id in ids]
        ws.backends.get("main").upsert([hb.ext.RowOp(LiteSplitDoc.identifier, object_id, {"object_id": object_id, "title": object_id}) for object_id in extra])
        listings.clear()
        pages = ws._row_store.iter_row_pages(LiteSplitDoc.identifier, batch_size=4, columns=["title"])
        assert [row["object_id"] for page in pages for row in page] == sorted(ids + extra)
        # one listing serves every Lite page; the step after its last page asks the used-up route once more
        assert listings == ['object_id != ""', 'object_id > "k029"'] and open_listings() == []

        pages = ws._row_store.iter_row_pages(LiteSplitDoc.identifier, batch_size=4, columns=["title"])
        assert [row["object_id"] for row in next(pages)] == ["k000", "k000a", "k001", "k001a"]
        assert len(open_listings()) == 1
        pages.close()
        assert open_listings() == []
    finally:
        ws.release()


def test_milvus_lite_key_listing_resumes_from_a_cursor_inside_a_served_page():
    """A cursor inside a page a listing already served continues the nearest such listing; other cursors list again."""
    from heavenbase.backends.vector import milvus as milvus_module

    listings = milvus_module._LiteKeyListings()
    keys = [f"k{index}" for index in range(8)]
    listings.keep("f.db", "c", "k3", (keys, 4, 0))
    listings.keep("f.db", "c", "k5", (keys, 6, 0))
    listings.keep("f.db", "other", "k2", (keys, 3, 0))
    assert listings.resume("f.db", "c", "k6") is None  # not served yet by any listing
    assert listings.resume("g.db", "c", "k1") is None
    assert listings.resume("f.db", "c", "k1") == (keys, 2, 0)
    assert listings.take("f.db", "c", "k3") is None  # the nearest later cursor's listing was taken
    assert listings.resume("f.db", "c", "k5") == (keys, 6, 0)
    assert listings.resume("f.db", "other", "k2") == (keys, 3, 0)
    assert listings._listings == {}


def test_milvus_lite_key_listings_drop_the_oldest_beyond_their_bounds_and_refuse_stale_ones(monkeypatch):
    """Abandoned listings stay bounded by count and retained keys, and a listing made before a write is not kept."""
    from heavenbase.backends.vector import milvus as milvus_module

    monkeypatch.setattr(milvus_module, "_MAX_OPEN_KEY_LISTINGS", 3)
    monkeypatch.setattr(milvus_module, "_MAX_LISTED_KEYS", 10)
    listings = milvus_module._LiteKeyListings()
    for cursor in "abcd":
        listings.keep("f.db", "c", cursor, (["k"] * 2, 1, 0))
    assert listings.take("f.db", "c", "a") is None
    assert [listings.take("f.db", "c", cursor) is not None for cursor in "bcd"] == [True, True, True]

    listings.keep("f.db", "c", "small", (["k"] * 4, 1, 0))
    listings.keep("f.db", "c", "big", (["k"] * 20, 1, 0))
    assert listings.take("f.db", "c", "small") is None
    assert listings.take("f.db", "c", "big") == (["k"] * 20, 1, 0)

    # a listing kept again for a cursor already kept is the newest, so the bound drops the other one
    listings.keep("f.db", "c", "again", (["k"] * 2, 1, 0))
    listings.keep("f.db", "c", "other", (["k"] * 2, 1, 0))
    listings.keep("f.db", "c", "again", (["k"] * 20, 1, 0))
    assert listings.take("f.db", "c", "other") is None
    assert listings.take("f.db", "c", "again") == (["k"] * 20, 1, 0)

    generation = listings.generation("f.db")
    listings.keep("other.db", "c", "x", (["k"], 1, listings.generation("other.db")))
    listings.forget("f.db")
    listings.keep("f.db", "c", "stale", (["k"], 1, generation))
    assert listings.take("f.db", "c", "stale") is None
    assert listings.take("other.db", "c", "x") == (["k"], 1, 0)


def test_milvus_lite_key_listing_dropped_by_the_bounds_continues_one_that_served_its_cursor(monkeypatch):
    """An enumeration whose listing the bounds dropped continues another that served its cursor; that one's enumeration lists again."""
    from heavenbase.backends.vector import milvus as milvus_module

    monkeypatch.setattr(milvus_module, "_MAX_OPEN_KEY_LISTINGS", 2)
    listings = milvus_module._LiteKeyListings()
    keys = [f"k{index:02d}" for index in range(10)]
    listings.keep("f.db", "c", "k03", (keys, 4, 0))  # enumeration A
    listings.keep("f.db", "c", "k07", (keys, 8, 0))  # enumeration B
    listings.keep("f.db", "c", "x", (["x", "y"], 1, 0))  # enumeration C; the bound drops A's listing, the oldest
    assert list(listings._listings) == [("f.db", "c", "k07"), ("f.db", "c", "x")]
    # A continues B's listing, which already served k03, instead of listing its keys again
    assert listings.resume("f.db", "c", "k03") == (keys, 4, 0)
    # B's own listing is gone, so B lists its keys again; C's listing never served k07
    assert listings.resume("f.db", "c", "k07") is None
    assert listings.take("f.db", "c", "x") == (["x", "y"], 1, 0)


class ScanOnlyBackend(hb.backends.InMemBackend):
    """A provider written for 0.1.2.2: it has ``scan`` but neither ``iter_scan`` nor ``scan_page``."""

    iter_scan = Backend.iter_scan
    scan_page = Backend.scan_page


class NoScanBackend(ScanOnlyBackend):
    """A lookup-only provider: ``scan`` is the unimplemented ``Backend`` default."""

    scan = Backend.scan


class ScanOnlyDoc(hb.Entity):
    identifier = "scan-only-doc"

    scope = hb.field(hb.ShortText)
    embedding = hb.field(hb.Vector[2])


def _scan_only_rows() -> list[dict]:
    return [{"object_id": f"s{index:02}", "scope": "one", "embedding": [1, index]} for index in range(7)]


def _assert_enumerates_through_scan(ws):
    assert [row["object_id"] for row in ws.iter_rows(ScanOnlyDoc, batch_size=3)] == [f"s{index:02}" for index in range(7)]
    query = ws.query(ScanOnlyDoc).where({"scope": "one"}).near("embedding", [1, 0], top_k=3, exact=True)
    assert query.execute().ids == ["s00", "s01", "s02"]
    # a Catalog mode change reconciles the stored rows through the same enumeration
    assert ws.register(ScanOnlyDoc, catalog="identity", allow_replace=True) is ScanOnlyDoc


def test_a_backend_with_only_scan_enumerates_through_the_default_iter_scan():
    backend = ScanOnlyBackend("main")
    ws = hb.HeavenBase("scan-page-scan-only", detached=True, backends={"main": backend})
    try:
        ws.register(ScanOnlyDoc)
        ws.upsert_many(ScanOnlyDoc, list(reversed(_scan_only_rows())))
        assert list(backend.iter_scan(ScanOnlyDoc.identifier, batch_size=2, columns=["scope"]))[0].keys() == {"object_id", "scope"}
        _assert_enumerates_through_scan(ws)
    finally:
        ws.release()
    with pytest.raises(ValueError, match="batch_size must be a positive integer"):
        backend.iter_scan(ScanOnlyDoc.identifier, batch_size=0)
    with pytest.raises(NotImplementedError, match="supports neither iter_scan nor scan"):
        NoScanBackend("main").iter_scan(ScanOnlyDoc.identifier, batch_size=2)


def test_tinydb_routes_page_exact_scan_and_reconcile(tmp_path):
    pytest.importorskip("tinydb")
    ws = hb.HeavenBase("scan-page-tinydb", detached=True, backends={"main": {"type": "tinydb", "database": hb.utils.path_to_file_uri(tmp_path / "tiny.json")}})
    try:
        ws.register(ScanOnlyDoc)
        ws.upsert_many(ScanOnlyDoc, _scan_only_rows())
        _assert_enumerates_through_scan(ws)
    finally:
        ws.release()


class CountingScanOnlyBackend(ScanOnlyBackend):
    """A 0.1.2.2-style route that counts its complete enumerations."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scan_calls = 0

    def scan(self, entity_id):
        self.scan_calls += 1
        return super().scan(entity_id)


class SplitScanOnlyDoc(hb.Entity):
    identifier = "split-scan-only-doc"

    scope = hb.field(hb.ShortText)
    embedding = hb.field(hb.Vector[2]).store(to="vec")


@pytest.mark.parametrize("split", [False, True], ids=["one-route", "with-a-native-route"])
def test_a_catalog_mode_change_enumerates_a_scan_only_route_once(monkeypatch, split):
    from heavenbase.workspace.catalog import audit as catalog_audit

    # Seven rows in pages of two: paging through the portable scan_page would enumerate the route once per page.
    monkeypatch.setattr(catalog_audit, "_RECONCILE_PAGE", 2)
    entity = SplitScanOnlyDoc if split else ScanOnlyDoc
    vec = CountingScanOnlyBackend("vec")
    backends = {"main": hb.backends.InMemBackend("main"), "vec": vec} if split else {"main": CountingScanOnlyBackend("main")}
    counted = vec if split else backends["main"]
    ws = hb.HeavenBase("scan-page-reconcile-split" if split else "scan-page-reconcile", detached=True, backends=backends)
    try:
        ws.register(entity)
        assert ws._row_store.routed_backend_names(entity.identifier) == (["main", "vec"] if split else ["main"])
        ws.upsert_many(entity, _scan_only_rows())
        objects = {}
        previous = "full"
        for mode in ("identity", "none", "full", "none", "identity"):
            counted.scan_calls = 0
            assert ws.register(entity, catalog=mode, allow_replace=True) is entity
            # Leaving none first checks every stored ID before any Catalog write.
            assert counted.scan_calls == (2 if previous == "none" else 1), mode
            previous = mode
            report = ws.audit()["catalog"]
            assert report["ok"] is True, (mode, report)
            objects.setdefault(mode, report["checked"]["objects"])
        # Every stored row was rewritten or deleted: seven Catalog rows under identity and full, none under none.
        assert objects["identity"] == objects["full"] == objects["none"] + 7
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("portable_name", ["a-scan", "z-scan"])
def test_mixed_routes_scan_the_portable_route_once_per_pass(portable_name):
    class ScanOnlyLookupBackend(CountingScanOnlyBackend):
        get_many = Backend.get_many
        exists_many = Backend.exists_many

    class Doc(hb.Entity):
        identifier = "mixed-scan-only-doc"

        title = hb.field(hb.ShortText)
        embedding = hb.field(hb.Vector[2]).store(to=portable_name).optional()

    portable = ScanOnlyLookupBackend(portable_name)
    native = hb.backends.InMemBackend("main")
    ws = hb.HeavenBase("mixed-scan-only", detached=True, backends={"main": native, portable_name: portable})
    try:
        ws.register(Doc, catalog="none")
        ws.upsert_many(Doc, [{"object_id": f"r{i:04}", "title": "title", "embedding": [1, i]} for i in range(513)])
        portable.delete(Doc.identifier, ["r0512"])

        portable.scan_calls = 0
        assert ws.query(Doc).near(Doc.embedding, [1, 0], top_k=3, exact=True).execute().ids == ["r0000", "r0001", "r0002"]
        exact_scans = portable.scan_calls

        portable.scan_calls = 0
        ws.register(Doc, catalog="identity", allow_replace=True)
        reconcile_scans = portable.scan_calls
        assert ws.audit()["catalog"]["ok"] is True
        assert (exact_scans, reconcile_scans) == (1, 2)
    finally:
        ws.release()


class _SoftDeleteReads:
    """0.1.2.2-style read overrides that hide rows marked ``hidden``, as a soft-delete provider does."""

    def scan(self, entity_id):
        return [row for row in super().scan(entity_id) if not row.get("hidden")]

    def get_many(self, entity_id, object_ids, *, columns=None):
        return [None if row is None or row.get("hidden") else row for row in super().get_many(entity_id, object_ids, columns=columns)]


class SoftDeleteInMem(_SoftDeleteReads, hb.backends.InMemBackend):
    pass


class SoftDeleteSQLite(_SoftDeleteReads, hb.backends.SQLiteBackend):
    pass


class LazyInMem(hb.backends.InMemBackend):
    """Hydrates its rows from an external source on ``scan``, as a lazily loading provider does."""

    def __init__(self, name, source):
        super().__init__(name)
        self.source = source

    def scan(self, entity_id):
        self.data.setdefault(self.ws_id, {})[entity_id] = {row["object_id"]: dict(row) for row in self.source}
        return super().scan(entity_id)


class SoftDoc(hb.Entity):
    identifier = "soft-doc"

    scope = hb.field(hb.ShortText)
    hidden = hb.field(hb.Boolean)
    embedding = hb.field(hb.Vector[2])


def _soft_rows() -> list[dict]:
    return [{"object_id": f"x{index}", "scope": "one", "hidden": index % 2 == 1, "embedding": [1, index]} for index in range(6)]


@pytest.mark.slow
@pytest.mark.parametrize(
    "factory",
    [lambda tmp_path: SoftDeleteInMem("main"), lambda tmp_path: SoftDeleteSQLite("main", hb.utils.path_to_file_uri(tmp_path / "soft.db"))],
    ids=["inmem", "sqlite"],
)
def test_a_subclass_scan_override_is_honoured_by_every_enumeration(tmp_path, factory):
    backend = factory(tmp_path)
    ws = hb.HeavenBase("scan-page-soft-delete", detached=True, backends={"main": backend})
    try:
        ws.register(SoftDoc)
        ws.upsert_many(SoftDoc, _soft_rows())
        visible = ["x0", "x2", "x4"]
        assert [row["object_id"] for row in backend.scan(SoftDoc.identifier)] == visible
        assert [row["object_id"] for row in backend.iter_scan(SoftDoc.identifier, batch_size=2)] == visible
        assert [row["object_id"] for row in backend.scan_page(SoftDoc.identifier, after="x0", batch_size=1)] == ["x2"]
        assert [row["object_id"] for row in ws.iter_rows(SoftDoc, batch_size=2)] == visible
        query = ws.query(SoftDoc).where({"scope": "one"}).near("embedding", [1, 0], top_k=6, exact=True)
        assert sorted(query.execute().ids) == visible
    finally:
        ws.release()


def test_a_lazily_hydrating_subclass_enumerates_through_its_scan():
    backend = LazyInMem("main", _soft_rows())
    ws = hb.HeavenBase("scan-page-lazy", detached=True, backends={"main": backend})
    try:
        ws.register(SoftDoc)
        backend.data.clear()  # a cold cache: the rows live only in the external source
        ids = [f"x{index}" for index in range(6)]
        assert [row["object_id"] for row in ws.iter_rows(SoftDoc, batch_size=4)] == ids
        backend.data.clear()
        query = ws.query(SoftDoc).where({"scope": "one"}).near("embedding", [1, 0], top_k=2, exact=True)
        assert query.execute().ids == ["x0", "x1"]
    finally:
        ws.release()


def test_only_a_layer_overridden_below_a_native_page_falls_back_to_the_portable_one():
    class IterOnly(hb.backends.InMemBackend):
        def iter_scan(self, entity_id, *, batch_size, columns=None):
            yield from super().iter_scan(entity_id, batch_size=batch_size, columns=columns)

    # a layer defined below the native page sends only the layers above it to the portable defaults
    for soft in (SoftDeleteInMem, SoftDeleteSQLite):
        assert is_portable_layer(soft.iter_scan, "iter_scan") and is_portable_layer(soft.scan_page, "scan_page")
    assert is_portable_layer(IterOnly.scan_page, "scan_page") and not is_portable_layer(IterOnly.iter_scan, "iter_scan")
    # built-in providers that define all three layers, and subclasses that override none, keep their native pages
    for native in (hb.backends.InMemBackend, hb.backends.JsonFileBackend, hb.backends.SQLiteBackend, hb.backends.DuckDBBackend):
        assert not is_portable_layer(native.scan_page, "scan_page") and not is_portable_layer(native.iter_scan, "iter_scan")
        assert native.scan is not SoftDeleteInMem.scan  # built-in classes are left untouched


class _Counted:
    """Counts the calls of a subclass's enumeration override."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0


class IterViaSuperSQLite(_Counted, hb.backends.SQLiteBackend):
    """Overrides only ``iter_scan``, filtering the parent's native iteration."""

    def iter_scan(self, entity_id, *, batch_size, columns=None):
        self.calls += 1
        for row in super().iter_scan(entity_id, batch_size=batch_size, columns=columns):
            if not row.get("hidden"):
                yield row


class ScanViaSuperIterSQLite(_Counted, hb.backends.SQLiteBackend):
    """Overrides ``scan`` over the parent's native ``iter_scan``."""

    def scan(self, entity_id):
        self.calls += 1
        return [row for row in super().iter_scan(entity_id, batch_size=2) if not row.get("hidden")]


class ScanViaIterInMem(_Counted, hb.backends.InMemBackend):
    """Overrides ``scan`` over ``self.iter_scan``, which it does not override."""

    def scan(self, entity_id):
        self.calls += 1
        return [row for row in self.iter_scan(entity_id, batch_size=2) if not row.get("hidden")]


class LoggedSoftDeleteSQLite(SoftDeleteSQLite):
    """Adds an ``iter_scan`` over a parent that itself overrides ``scan``."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen = []

    def iter_scan(self, entity_id, *, batch_size, columns=None):
        for row in super().iter_scan(entity_id, batch_size=batch_size, columns=columns):
            self.seen.append(row["object_id"])
            yield row


class PageLoopInMem(_Counted, hb.backends.InMemBackend):
    """Overrides ``scan`` as a loop over ``self.scan_page``, which it does not override."""

    def scan(self, entity_id):
        self.calls += 1
        rows, after = [], None
        while page := self.scan_page(entity_id, after=after, batch_size=2):
            rows += [row for row in page if not row.get("hidden")]
            after = page[-1]["object_id"]
        return rows


class _Lazy:
    """A re-iterable, cursor-like result that is not an iterator."""

    def __init__(self, produce):
        self.produce = produce

    def __iter__(self):
        return self.produce()


class LazyIterableSQLite(_Counted, hb.backends.SQLiteBackend):
    """``iter_scan`` returns a lazy iterable whose ``__iter__`` reads the parent's iteration."""

    def iter_scan(self, entity_id, *, batch_size, columns=None):
        self.calls += 1
        parent = super()
        return _Lazy(lambda: (row for row in parent.iter_scan(entity_id, batch_size=batch_size, columns=columns) if not row.get("hidden")))


class LazyPagesInMem(_Counted, hb.backends.InMemBackend):
    """``iter_scan`` returns a lazy iterable whose ``__iter__`` pages through ``self.scan_page``."""

    def iter_scan(self, entity_id, *, batch_size, columns=None):
        self.calls += 1

        def produce():
            after = None
            while page := self.scan_page(entity_id, after=after, batch_size=batch_size, columns=columns):
                yield from (row for row in page if not row.get("hidden"))
                after = page[-1]["object_id"]

        return _Lazy(produce)


def _sqlite_file(tmp_path, name):
    return hb.utils.path_to_file_uri(tmp_path / f"{name}.db")


@pytest.mark.slow
@pytest.mark.parametrize(
    "factory",
    [
        lambda tmp_path: IterViaSuperSQLite("main", _sqlite_file(tmp_path, "iter")),
        lambda tmp_path: ScanViaSuperIterSQLite("main", _sqlite_file(tmp_path, "scan-iter")),
        lambda tmp_path: ScanViaIterInMem("main"),
        lambda tmp_path: LoggedSoftDeleteSQLite("main", _sqlite_file(tmp_path, "logged")),
        lambda tmp_path: PageLoopInMem("main"),
        lambda tmp_path: LazyIterableSQLite("main", _sqlite_file(tmp_path, "lazy-iterable")),
        lambda tmp_path: LazyPagesInMem("main"),
    ],
    ids=[
        "sqlite-iter-over-super",
        "sqlite-scan-over-super-iter",
        "inmem-scan-over-self-iter",
        "sqlite-iter-over-soft-scan",
        "inmem-scan-over-self-pages",
        "sqlite-lazy-iterable-over-super",
        "inmem-lazy-iterable-over-self-pages",
    ],
)
def test_an_override_that_reads_the_parent_enumeration_is_honoured_without_recursion(tmp_path, factory):
    backend = factory(tmp_path)
    ws = hb.HeavenBase("scan-page-reentry", detached=True, backends={"main": backend})
    try:
        ws.register(SoftDoc)
        ws.upsert_many(SoftDoc, _soft_rows())
        visible = ["x0", "x2", "x4"]
        assert [row["object_id"] for row in backend.iter_scan(SoftDoc.identifier, batch_size=2)] == visible
        assert [row["object_id"] for row in backend.iter_scan(SoftDoc.identifier, batch_size=2, columns=["scope", "hidden"])] == visible
        assert [row["object_id"] for row in backend.scan_page(SoftDoc.identifier, after="x0", batch_size=1)] == ["x2"]
        assert [row["object_id"] for row in ws.iter_rows(SoftDoc, batch_size=2)] == visible
        query = ws.query(SoftDoc).where({"scope": "one"}).near("embedding", [1, 0], top_k=6, exact=True)
        assert sorted(query.execute().ids) == visible
        if isinstance(backend, (ScanViaSuperIterSQLite, ScanViaIterInMem, LoggedSoftDeleteSQLite, PageLoopInMem)):  # these override scan
            assert [row["object_id"] for row in backend.scan(SoftDoc.identifier)] == visible
    finally:
        ws.release()


@pytest.mark.slow
@pytest.mark.parametrize("cls", [ScanViaSuperIterSQLite, ScanViaIterInMem, IterViaSuperSQLite])
def test_a_direct_read_runs_the_override_once(tmp_path, cls):
    backend = cls("main", _sqlite_file(tmp_path, "once")) if issubclass(cls, hb.backends.SQLiteBackend) else cls("main")
    ws = hb.HeavenBase("scan-page-once", detached=True, backends={"main": backend})
    try:
        ws.register(SoftDoc)
        extra = [{"object_id": f"y{index}", "scope": "one", "hidden": False, "embedding": [0, 1]} for index in range(9)]
        ws.upsert_many(SoftDoc, _soft_rows() + extra)
        backend.calls = 0
        read = backend.iter_scan if cls is IterViaSuperSQLite else backend.scan
        rows = list(read(SoftDoc.identifier, batch_size=2) if cls is IterViaSuperSQLite else read(SoftDoc.identifier))
        assert len(rows) == 12
        # the native layers the override reads stay native: no page re-enters the override
        assert backend.calls == 1
    finally:
        ws.release()


def _pooled(read):
    """Run ``read`` on a fresh worker thread and return its result, as an override handing work to a pool does."""
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(read).result()


class PooledScanInMem(hb.backends.InMemBackend):
    """A 0.1.2.2-style ``scan`` that runs the parent's locked ``scan`` on a worker thread."""

    def scan(self, entity_id):
        return [row for row in _pooled(lambda: super(PooledScanInMem, self).scan(entity_id)) if not row.get("hidden")]


class PooledScanSQLite(hb.backends.SQLiteBackend):
    """A 0.1.2.2-style ``scan`` that runs the parent's locked ``scan`` on a worker thread."""

    def scan(self, entity_id):
        return [row for row in _pooled(lambda: super(PooledScanSQLite, self).scan(entity_id)) if not row.get("hidden")]


class PooledIterSQLite(hb.backends.SQLiteBackend):
    """A ``scan`` that reads the parent's native ``iter_scan`` on a worker thread."""

    def scan(self, entity_id):
        parent = super().iter_scan
        return _pooled(lambda: [row for row in parent(entity_id, batch_size=2) if not row.get("hidden")])


class PooledHydrationInMem(hb.backends.InMemBackend):
    """A ``scan`` that hydrates its rows through locked ``get_many`` calls on a worker thread."""

    def scan(self, entity_id):
        ids = [row["object_id"] for row in super().scan(entity_id)]
        return [row for row in _pooled(lambda: self.get_many(entity_id, ids)) if row is not None and not row.get("hidden")]


def _finishes(read, *, seconds=60):
    """Run ``read`` on a daemon thread and return its result, failing instead of hanging when it never returns."""
    outcome = {}

    def run():
        try:
            outcome["value"] = read()
        except BaseException as exc:  # noqa: BLE001 - reported below
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "the read did not return"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


@pytest.mark.slow
@pytest.mark.parametrize(
    "factory",
    [
        lambda tmp_path: PooledScanInMem("main"),
        lambda tmp_path: PooledScanSQLite("main", _sqlite_file(tmp_path, "pooled-scan")),
        lambda tmp_path: PooledIterSQLite("main", _sqlite_file(tmp_path, "pooled-iter")),
        lambda tmp_path: PooledHydrationInMem("main"),
    ],
    ids=["inmem-pooled-super-scan", "sqlite-pooled-super-scan", "sqlite-pooled-super-iter", "inmem-pooled-get-many"],
)
def test_an_override_that_reads_this_backend_on_a_worker_thread_does_not_deadlock(tmp_path, factory):
    backend = factory(tmp_path)
    ws = hb.HeavenBase("scan-page-pooled", detached=True, backends={"main": backend})
    try:
        ws.register(SoftDoc)
        ws.upsert_many(SoftDoc, _soft_rows())
        visible = ["x0", "x2", "x4"]
        ids = lambda rows: [row["object_id"] for row in rows]  # noqa: E731
        assert _finishes(lambda: ids(backend.scan(SoftDoc.identifier))) == visible
        assert _finishes(lambda: ids(backend.iter_scan(SoftDoc.identifier, batch_size=2))) == visible
        assert _finishes(lambda: ids(backend.scan_page(SoftDoc.identifier, after=None, batch_size=10))) == visible
        assert _finishes(lambda: ids(ws.iter_rows(SoftDoc, batch_size=2))) == visible
        query = ws.query(SoftDoc).where({"scope": "one"}).near("embedding", [1, 0], top_k=6, exact=True)
        assert _finishes(lambda: sorted(query.execute().ids)) == visible
        assert not backend._lock._is_owned()
    finally:
        ws.release()


@pytest.fixture
def collector_off():
    """Leave garbage collection to the test, so an abandoned enumeration ends exactly where the test collects."""
    import gc

    enabled = gc.isenabled()
    gc.disable()
    try:
        yield gc
    finally:
        if enabled:
            gc.enable()


def _abandon_in_cycle(listings, path: str, cursor: str) -> None:
    """Start an enumeration that ends the listing for ``cursor`` when it is finalized, then leave it only to the collector."""

    def enumeration():
        try:
            yield
        finally:
            listings.take(path, "c", cursor)

    rows = enumeration()
    next(rows)
    holder: dict = {"rows": rows}
    holder["self"] = holder


@pytest.mark.parametrize("ended", ["k3", "k5"])
def test_milvus_lite_key_listing_resumes_when_a_collected_enumeration_ends_during_the_search(monkeypatch, collector_off, ended):
    """An abandoned ``iter_rows`` finalized inside ``resume``'s search drops its listing without failing the page."""
    from heavenbase.backends.vector import milvus as milvus_module

    listings = milvus_module._LiteKeyListings()
    keys = [f"k{index}" for index in range(8)]
    listings.keep("f.db", "c", "k3", (keys, 4, 0))
    listings.keep("f.db", "c", "k5", (keys, 6, 0))
    _abandon_in_cycle(listings, "f.db", ended)
    collections = []
    bisect_left = milvus_module.bisect_left

    def collecting_bisect_left(*args):
        if not collections:
            collections.append(collector_off.collect())
        return bisect_left(*args)

    monkeypatch.setattr(milvus_module, "bisect_left", collecting_bisect_left)
    # both listings served k1; whichever the collected enumeration leaves continues from it
    assert listings.resume("f.db", "c", "k1") == (keys, 2, 0)
    assert collections and listings._listings == {}


def test_milvus_lite_key_listings_forget_a_file_while_a_collected_enumeration_ends(collector_off):
    """An abandoned ``iter_rows`` finalized inside ``forget`` (the write path of every upsert) does not fail the write."""
    from heavenbase.backends.vector import milvus as milvus_module

    listings = milvus_module._LiteKeyListings()
    for path, cursor in [("f.db", "a"), ("f.db", "b"), ("f.db", "c"), ("g.db", "x")]:
        listings.keep(path, "c", cursor, (["k"], 1, 0))
    _abandon_in_cycle(listings, "f.db", "b")
    collections = []

    class CollectingPath(str):
        """The file path; comparing it with a listing's file runs a collection once, as any allocation may."""

        __hash__ = str.__hash__

        def __eq__(self, other):
            if not collections:
                collections.append(collector_off.collect())
            return str.__eq__(self, other)

    listings.forget(CollectingPath("f.db"))
    assert collections and list(listings._listings) == [("g.db", "c", "x")]
    assert listings.generation("f.db") == 1


@pytest.fixture
def collector_on():
    """Leave garbage collection on at whatever threshold the test sets, and restore the process's own settings."""
    import gc

    enabled, threshold = gc.isenabled(), gc.get_threshold()
    gc.enable()
    try:
        yield gc
    finally:
        gc.set_threshold(*threshold)
        if not enabled:
            gc.disable()


@pytest.mark.parametrize("method", ["resume", "keep", "forget"])
def test_milvus_lite_key_listings_survive_a_collection_at_any_allocation(monkeypatch, collector_on, method):
    """An abandoned ``iter_rows`` finalized at any allocation inside the listings fails neither the page nor the write.

    Python 3.10 and 3.11 collect at an allocation inside C code too, such as a copy of the listings' items. Each
    first-generation threshold below starts the collection at a different allocation after the call begins. Pairs
    are held alive so that a copy allocates new ones instead of reusing freed pairs, which the collector does not count.
    """
    from heavenbase.backends.vector import milvus as milvus_module

    monkeypatch.setattr(milvus_module, "_MAX_OPEN_KEY_LISTINGS", 8)
    keys = [f"k{index:02}" for index in range(16)]
    thresholds = collector_on.get_threshold()
    for threshold in range(1, 64):
        listings = milvus_module._LiteKeyListings()
        # every listing has served k00; the nearest later cursor's listing, k01, is the one the enumeration ends
        listings._listings = {("f.db", "c", keys[index]): (keys, index + 1, 0) for index in range(1, 13)}
        listings._listings[("g.db", "c", "x")] = (keys, 1, 0)
        pairs = [(index, threshold) for index in range(4096)]
        collector_on.collect(0)
        _abandon_in_cycle(listings, "f.db", keys[1])
        collector_on.set_threshold(threshold)
        try:
            if method == "resume":
                assert listings.resume("f.db", "c", keys[0]) == (keys, 1, 0), threshold
            elif method == "keep":
                listings.keep("f.db", "c", "new", (keys, 1, 0))
                assert len(listings._listings) <= 8 and list(listings._listings)[-1] == ("f.db", "c", "new"), threshold
            else:
                listings.forget("f.db")
                assert list(listings._listings) == [("g.db", "c", "x")], threshold
        finally:
            collector_on.set_threshold(*thresholds)
            del pairs
            collector_on.collect(0)
