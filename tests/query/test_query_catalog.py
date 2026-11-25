from datetime import datetime, timezone

import heavenbase as hb
import pytest

from heavenbase.query.catalog import CatalogAvailable

pytestmark = [pytest.mark.fast, pytest.mark.unit]


def test_catalog_available_has_one_normalized_rendering_protocol():
    """One policy should render equivalent typed and mapping predicates."""
    available = CatalogAvailable(now=1_000)

    assert available.to_expr().to_dict() == {
        "and": [
            {"filter": {"field": "active", "op": "eq", "value": True}},
            {"filter": {"field": "visible", "op": "eq", "value": True}},
            {"filter": {"field": "expires_compute", "op": "gt", "value": 1_000}},
            {"filter": {"field": "stale_compute", "op": "gt", "value": 1_000}},
        ]
    }
    assert available.to_dict() == {
        "$and": [
            {"active": True},
            {"visible": True},
            {"expires_compute": {"$gt": 1_000}},
            {"stale_compute": {"$gt": 1_000}},
        ]
    }


def test_catalog_available_loads_or_disables_external_values():
    """Loading should normalize supported values and reject ambiguous input."""
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert CatalogAvailable.load(False) is None
    assert CatalogAvailable.load(None) is None
    assert isinstance(CatalogAvailable.load(True), CatalogAvailable)
    assert CatalogAvailable.load({"now": instant, "stale": True, "invisible": True}) == CatalogAvailable(
        now=hb.Timestamp().validate(instant), stale=True, invisible=True
    )
    with pytest.raises(ValueError, match="Unsupported Catalog available key 'visible'"):
        CatalogAvailable.load({"visible": True})
    with pytest.raises(TypeError, match="stale must be a boolean"):
        CatalogAvailable.load({"stale": 1})
    with pytest.raises(TypeError, match="expects true, false, or an object"):
        CatalogAvailable.load([])


def test_catalog_available_checks_entity_and_option_rules():
    """Catalog ownership and optional visibility rules should fail or render explicitly."""
    with pytest.raises(ValueError, match="available applies only to Catalog"):
        CatalogAvailable().check("note")

    predicates = CatalogAvailable(now=1_000, stale=True, invisible=True).check("sys-catalog").to_dict()["$and"]
    assert predicates == [{"active": True}, {"expires_compute": {"$gt": 1_000}}]


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["inmem", "sqlite"])
def test_none_comparisons_on_timestamp_fields_test_for_a_missing_value(tmp_path, kind):
    """`== None`, `!= None` and `$in [None]` on an optional Timestamp or a Catalog timestamp run as on a text field."""

    class StampedEvent(hb.Entity):
        identifier = "none-stamped-event"

        at = hb.field(hb.Timestamp).optional()
        tag = hb.field(hb.ShortText).optional()

    backend = hb.backends.InMemBackend("main") if kind == "inmem" else hb.backends.SQLiteBackend("main", hb.utils.path_to_file_uri(tmp_path / "none.db"))
    ws = hb.HeavenBase(f"timestamp-none-{kind}", detached=True, backends={"main": backend})
    try:
        ws.register(StampedEvent)
        ws.upsert(StampedEvent, {"object_id": "a", "at": 1000, "tag": "x"})
        ws.upsert(StampedEvent, {"object_id": "b"})

        def ids(where, entity=StampedEvent):
            return sorted(ws.query(entity).where(where).execute().ids)

        for field in ("at", "tag"):
            column = getattr(StampedEvent, field)
            assert ids(column == None) == ids({field: None}) == ids({field: {"$in": [None]}}) == ["b"]  # noqa: E711
            assert ids(column != None) == ids({field: {"$ne": None}}) == ["a"]  # noqa: E711
        assert ids({"at": {"$in": [None, 1000]}}) == ["a", "b"]
        with pytest.raises(ValueError, match="'gt' filter needs a value"):
            ids({"at": {"$gt": None}})
        catalog = sorted(ws.query(hb.Catalog).execute().ids)
        assert catalog and ids({"updated_at": None}, hb.Catalog) == [] and ids({"created_at": {"$ne": None}}, hb.Catalog) == catalog
    finally:
        ws.release()
