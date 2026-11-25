"""Providers whose read methods predate the ``columns`` keyword keep working under projected reads."""

from __future__ import annotations

from functools import wraps

import pytest

import heavenbase as hb
from heavenbase.backends.base import Backend, accepts_keyword, read_projected

pytestmark = [pytest.mark.fast, pytest.mark.unit]


class LegacyGetManyBackend(hb.backends.InMemBackend):
    """Third-party style provider that overrides ``get_many`` with the 0.1.2.2 signature."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.get_many_calls = 0

    def get_many(self, entity_id, object_ids):
        """Return complete fragments; this signature has no ``columns`` keyword."""
        self.get_many_calls += 1
        return super().get_many(entity_id, object_ids)


class LegacyFetchOnlyBackend(LegacyGetManyBackend):
    """Lookup-only legacy provider: no enumeration, an exact count proof, old ``get_many``."""

    def scan(self, entity_id):
        raise NotImplementedError("scan unavailable")

    def exact_count(self, entity_id):
        return self.count(entity_id)


class LegacyIterScanBackend(LegacyGetManyBackend):
    """Provider with an unordered bounded scan that predates ``columns`` and relies on the portable page."""

    scan_page = Backend.scan_page

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.iter_scan_calls = 0

    def iter_scan(self, entity_id, *, batch_size):
        """Yield complete fragments in reverse id order; this signature has no ``columns`` keyword."""
        self.iter_scan_calls += 1
        yield from sorted(super().iter_scan(entity_id, batch_size=batch_size), key=lambda row: row["object_id"], reverse=True)


class LegacyDoc(hb.Entity):
    identifier = "legacy-backend-doc"

    title = hb.field(hb.ShortText)
    status = hb.field(hb.ShortText).default("draft")
    units = hb.field(hb.Integer)


class LegacySplitDoc(hb.Entity):
    identifier = "legacy-backend-split-doc"

    region = hb.field(hb.ShortText).required().store(to="rows")
    units = hb.field(hb.Integer).required().store(to="values")


_DOCS = [
    {"object_id": "d2", "title": "two", "units": 20},
    {"object_id": "d1", "title": "one", "status": "live", "units": 10},
    {"object_id": "d3", "title": "three", "status": "live", "units": 30},
]


@pytest.fixture(params=[LegacyGetManyBackend, LegacyIterScanBackend], ids=["legacy-get-many", "legacy-iter-scan"])
def workspace(request):
    backend = request.param("main")
    ws = hb.HeavenBase(f"legacy-backend-{request.param.__name__.lower()}", detached=True, backends={"main": backend})
    ws.register(LegacyDoc)
    ws.upsert_many(LegacyDoc, _DOCS)  # Catalog lifecycle reads are projected lookups on this route
    ws.upsert_many(LegacyDoc, [{"object_id": "d2", "title": "two again", "units": 21}])
    yield ws
    ws.release()


def test_projected_point_reads_trim_complete_legacy_fragments(workspace):
    backend = workspace.backends.get("main")
    calls = backend.get_many_calls

    assert workspace.get("d1", entity=LegacyDoc, columns=["title"]) == {"object_id": "d1", "title": "one"}
    assert workspace.get_many(["d2", "absent", "d3"], entity=LegacyDoc, columns=["status", "units"]) == [
        {"object_id": "d2", "status": "draft", "units": 21},
        None,
        {"object_id": "d3", "status": "live", "units": 30},
    ]
    assert workspace.get("d1", entity=LegacyDoc) == {"object_id": "d1", "title": "one", "status": "live", "units": 10}
    assert workspace.read_rows(LegacyDoc, ["d3", "absent"]) == [{"object_id": "d3", "title": "three", "status": "live", "units": 30}, None]
    assert backend.get_many_calls > calls
    with pytest.raises(TypeError, match="field names"):
        workspace.get("d1", entity=LegacyDoc, columns=["title", 3])


@pytest.mark.slow
@pytest.mark.parametrize("forward_options", [False, True], ids=["ignores-options", "forwards-options"])
@pytest.mark.parametrize("decorated", [False, True], ids=["plain", "wraps-superclass"])
def test_legacy_keyword_options_work_with_catalog_writes_and_projected_reads(forward_options, decorated):
    class LegacyOptionsBackend(hb.backends.InMemBackend):
        def get_many(self, entity_id, object_ids, **options):
            return self.fetch(entity_id, object_ids, **(options if forward_options else {}))

        def fetch(self, entity_id, object_ids, *, trace=False):
            return super().get_many(entity_id, object_ids)

    if decorated:
        LegacyOptionsBackend.get_many = wraps(hb.backends.InMemBackend.get_many)(LegacyOptionsBackend.get_many)

    ws = hb.HeavenBase("legacy-keyword-options", detached=True, backends={"main": LegacyOptionsBackend("main")})
    try:
        ws.register(LegacyDoc)
        ws.upsert(LegacyDoc, _DOCS[1])
        assert ws.get("d1", entity=LegacyDoc, columns=["title"]) == {"object_id": "d1", "title": "one"}
        assert ws.get_many(["d1", "absent"], entity=LegacyDoc, columns=[]) == [{"object_id": "d1"}, None]
    finally:
        ws.release()


def test_row_iteration_and_queries_work_on_a_legacy_route(workspace):
    projected = list(workspace._row_store.iter_rows(LegacyDoc.identifier, batch_size=2, columns=["units"]))
    assert projected == [{"object_id": "d1", "units": 10}, {"object_id": "d2", "units": 21}, {"object_id": "d3", "units": 30}]
    assert [row["title"] for row in workspace.iter_rows(LegacyDoc, batch_size=1)] == ["one", "two again", "three"]

    live = workspace.query(LegacyDoc).where(LegacyDoc.status == "live").order_by("object_id").execute().rows()
    assert [row["object_id"] for row in live] == ["d1", "d3"]
    total = workspace.query(LegacyDoc).where(LegacyDoc.status == "live").aggregate(total=LegacyDoc.units.sum()).execute().scalar()
    assert total == 40


def test_portable_scan_page_trims_a_legacy_iter_scan():
    backend = LegacyIterScanBackend("solo", ws_id="legacy")
    backend.upsert([hb.ext.RowOp("thing", object_id, {"object_id": object_id, "a": index, "b": -index}) for index, object_id in enumerate(["t1", "t2", "t3"])])

    assert backend.scan_page("thing", after=None, batch_size=2, columns=["b"]) == [{"object_id": "t1", "b": 0}, {"object_id": "t2", "b": -1}]
    assert backend.scan_page("thing", after="t2", batch_size=2, columns=["a"]) == [{"object_id": "t3", "a": 2}]
    assert backend.scan_page("thing", after="t2", batch_size=2) == [{"object_id": "t3", "a": 2, "b": -2}]
    assert backend.iter_scan_calls == 3


def test_portable_scan_page_does_not_forward_columns_to_legacy_scan_options():
    class LegacyOptionsScan(LegacyIterScanBackend):
        @wraps(hb.backends.InMemBackend.iter_scan)
        def iter_scan(self, entity_id, *, batch_size, **options):
            return self.fetch(entity_id, batch_size=batch_size, **options)

        def fetch(self, entity_id, *, batch_size, trace=False):
            return super().iter_scan(entity_id, batch_size=batch_size)

    backend = LegacyOptionsScan("solo", ws_id="legacy")
    backend.upsert([hb.ext.RowOp("thing", "t1", {"object_id": "t1", "a": 1, "b": 2})])
    assert backend.scan_page("thing", after=None, batch_size=2, columns=["a"]) == [{"object_id": "t1", "a": 1}]


def test_exact_fold_hydrates_a_legacy_lookup_only_route():
    rows = LegacyGetManyBackend("rows")
    values = LegacyFetchOnlyBackend("values")
    ws = hb.HeavenBase("legacy-backend-exact-fold", detached=True, backends={"rows": rows, "values": values})
    try:
        ws.register(LegacySplitDoc)
        ws.upsert_many(
            LegacySplitDoc,
            [
                {"object_id": "s2", "region": "west", "units": 20},
                {"object_id": "s1", "region": "east", "units": 10},
                {"object_id": "s3", "region": "east", "units": 30},
            ],
        )
        values.get_many_calls = 0

        total = ws.query(LegacySplitDoc).where(LegacySplitDoc.region == "east").aggregate(total=LegacySplitDoc.units.sum()).execute().scalar()

        assert total == 40
        assert values.get_many_calls == 1
        assert ws.get("s3", entity=LegacySplitDoc, columns=["units"]) == {"object_id": "s3", "units": 30}
    finally:
        ws.release()


def test_keyword_support_is_read_from_the_signature_and_instance_overrides():
    legacy = LegacyGetManyBackend("legacy")
    current = hb.backends.InMemBackend("current")

    assert not accepts_keyword(legacy, "get_many", "columns")
    assert accepts_keyword(current, "get_many", "columns")
    assert not accepts_keyword(LegacyIterScanBackend("scan"), "iter_scan", "columns")

    def forwarding(entity_id, object_ids, **options):
        return [{"object_id": object_id, "seen": sorted(options)} for object_id in object_ids]

    current.get_many = forwarding  # an instance override is inspected on its own, not from the class cache
    assert not accepts_keyword(current, "get_many", "columns")
    assert read_projected(current, "get_many", "thing", ["x"], columns=["seen"]) == [{"object_id": "x", "seen": []}]

    @wraps(LegacyGetManyBackend.get_many)
    def projected(entity_id, object_ids, *, columns=None, **options):
        return [{"object_id": object_id, "seen": columns} for object_id in object_ids]

    current.get_many = projected
    assert accepts_keyword(current, "get_many", "columns")
    assert read_projected(current, "get_many", "thing", ["x"], columns=["seen"]) == [{"object_id": "x", "seen": ["seen"]}]
    current.get_many = lambda entity_id, object_ids: [{"object_id": object_id, "a": 1, "b": 2} for object_id in object_ids]
    assert not accepts_keyword(current, "get_many", "columns")
    assert read_projected(current, "get_many", "thing", ["x"], columns=["b"]) == [{"object_id": "x", "b": 2}]
    assert read_projected(current, "get_many", "thing", ["x"], columns=None) == [{"object_id": "x", "a": 1, "b": 2}]
