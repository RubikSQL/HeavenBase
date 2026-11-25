"""Natural aggregate syntax, portable semantics, and strict boundary tests."""

import os
from importlib.util import find_spec

import pytest

import heavenbase as hb
from heavenbase.query import Aggregate, AggregateMetric


class Sale(hb.Entity):
    identifier = "aggregate-sale"

    region = hb.field(hb.ShortText)
    category = hb.field(hb.ShortText)
    units = hb.field(hb.Integer)
    price = hb.field(hb.Float)
    note = hb.field(hb.ShortText).optional()
    payload = hb.field(hb.Json).optional()


def _spread_test_fold(values):
    """Return the numeric range for the custom reducer contract."""
    return max(values) - min(values) if values else None


def _workspace(name="aggregate-ws", backend=None):
    ws = hb.HeavenBase(name, detached=True, backends={"main": backend or {"type": "sqlite", "database": ":memory:"}})
    ws.register(Sale)
    ws.upsert_many(
        Sale,
        [
            {"object_id": "s1", "region": "east", "category": "a", "units": 2, "price": 10.0, "note": "first", "payload": None},
            {"object_id": "s2", "region": "east", "category": "b", "units": 3, "price": 20.0, "note": None, "payload": None},
            {"object_id": "s3", "region": "west", "category": "a", "units": 5, "price": 30.0, "note": "third", "payload": None},
            {"object_id": "s4", "region": "west", "category": "a", "units": 0, "price": 40.0, "note": None, "payload": None},
        ],
    )
    return ws


def test_natural_ungrouped_common_operators_and_scalar_result():
    """Field reducer methods should replace tuple-shaped aggregate specifications."""
    ws = _workspace("aggregate-common-ws")
    frame = (
        ws.query(Sale)
        .aggregate(rows=Sale.object_id.count(), low=Sale.price.min(), high=Sale.price.max(), units=Sale.units.sum(), mean=Sale.price.avg())
        .execute()
    )

    assert frame.identity_column is None
    assert frame.rows() == [{"rows": 4, "low": 10.0, "high": 40.0, "units": 10, "mean": 25.0}]
    assert ws.query(Sale).aggregate(total=Sale.units.sum()).execute().scalar() == 10
    with pytest.raises(ValueError, match="exactly one row and one column"):
        frame.scalar()


def test_grouping_composes_with_filter_order_and_pagination():
    """Aggregation should occur after row selection and before result ordering/pagination."""
    ws = _workspace("aggregate-group-ws")
    frame = (
        ws.query(Sale)
        .where(Sale.units >= 2)
        .group_by(Sale.region)
        .aggregate(rows=Sale.object_id.count(), units=Sale.units.sum(), average=Sale.price.avg())
        .order_by("average", desc=True)
        .limit(1)
        .execute()
    )

    assert frame.rows() == [{"region": "west", "rows": 1, "units": 5, "average": 30.0}]
    assert frame.identity_column is None
    assert frame.col_names() == ["region", "rows", "units", "average"]


def test_null_and_empty_set_semantics_match_sql():
    """Reducers should skip nulls and preserve grouped versus ungrouped empty shapes."""
    ws = _workspace("aggregate-null-ws")
    assert ws.query(Sale).aggregate(notes=Sale.note.count(), first=Sale.note.min()).execute().rows() == [{"notes": 2, "first": "first"}]

    empty = ws.query(Sale).where(Sale.units > 100).aggregate(rows=Sale.object_id.count(), total=Sale.units.sum(), average=Sale.price.avg()).execute()
    assert empty.rows() == [{"rows": 0, "total": None, "average": None}]

    grouped = ws.query(Sale).where(Sale.units > 100).group_by(Sale.region).aggregate(rows=Sale.object_id.count()).execute()
    assert grouped.rows() == []
    assert grouped.col_names() == ["region", "rows"]


def test_json_aggregate_uses_one_strict_natural_shape():
    """JSON metrics should use named one-key reducer mappings with separate group_by."""
    ws = _workspace("aggregate-json-ws")
    spec = {
        "filter": {"units": {"$gte": 2}},
        "group_by": ["region"],
        "aggregate": {
            "rows": {"count": {"field": "object_id"}},
            "total": {"sum": {"field": "units"}},
            "average": {"avg": {"field": "price"}},
        },
        "order": ["region"],
    }
    assert ws.query_json(Sale, spec).execute().rows() == [
        {"region": "east", "rows": 2, "total": 5, "average": 15.0},
        {"region": "west", "rows": 1, "total": 5, "average": 30.0},
    ]

    with pytest.raises(ValueError, match="exactly one operation"):
        ws.query_json(Sale, {"aggregate": {"bad": {"sum": "units", "avg": "units"}}})
    with pytest.raises(ValueError, match="JSON aggregate operation"):
        ws.query_json(Sale, {"aggregate": {"bad": {"majority": {"field": "units"}}}})
    with pytest.raises(TypeError, match="parameters must be an object"):
        ws.query_json(Sale, {"aggregate": {"bad": {"sum": ["units"]}}})


def test_frozen_request_model_and_query_conflicts_fail_early():
    """Aggregate request values should be immutable and reject ambiguous projections."""
    metric = AggregateMetric("units", "sum", Sale.identifier)
    clause = Aggregate({"total": metric}, ("region",))
    spec = hb.QuerySpec(Sale.identifier, aggregate=clause)

    with pytest.raises(TypeError):
        clause.metrics["other"] = metric
    with pytest.raises(Exception):
        clause.group_by += ("category",)
    assert spec.to_dict()["aggregate"]["metrics"] == {"total": {"sum": {"field": "units"}}}

    with pytest.raises(ValueError, match="do not combine"):
        hb.QuerySpec(Sale.identifier, select=("region",), aggregate=clause)
    pending_group = _workspace("aggregate-group-only-ws").query(Sale).group_by(Sale.region)
    assert pending_group.spec.group_by == ("region",)
    with pytest.raises(ValueError, match="group_by.*needs aggregate"):
        pending_group.spec.require_executable()
    with pytest.raises(ValueError, match="already has an aggregate"):
        _workspace("aggregate-twice-ws").query(Sale).aggregate(total=Sale.units.sum()).aggregate(other=Sale.units.avg())


def test_schema_aware_validation_rejects_wrong_types_fields_and_entities():
    """The workspace boundary should validate reducer families and field ownership."""
    ws = _workspace("aggregate-validation-ws")

    with pytest.raises(ValueError, match="does not support short-text"):
        ws.query(Sale).aggregate(total=Sale.region.sum()).execute()
    with pytest.raises(ValueError, match="cannot be used as an aggregate group"):
        ws.query(Sale).group_by(Sale.payload).aggregate(rows=Sale.object_id.count()).execute()

    class Other(hb.Entity):
        identifier = "aggregate-other"

        value = hb.field(hb.Integer)

    with pytest.raises(ValueError, match="query targets aggregate-sale"):
        ws.query(Sale).aggregate(total=Other.value.sum()).execute()
    with pytest.raises(ValueError, match="group_by field belongs to aggregate-other"):
        ws.query(Sale).group_by(Other.value)
    with pytest.raises(ValueError, match="Unknown aggregate field"):
        ws.query_json(Sale, {"aggregate": {"total": {"sum": {"field": "missing"}}}}).execute()


def test_registered_custom_reducer_drives_python_json_and_metadata():
    """Reducer extensions should not require edits to a static operation allowlist."""
    hb.ext.register_aggregate_reducer(
        "spread_test",
        _spread_test_fold,
        tags={"aggregate-input-modes": ["numeric"]},
        replace=True,
    )
    ws = _workspace("aggregate-custom-ws")

    assert ws.query(Sale).aggregate(spread=Sale.units.spread_test()).execute().scalar() == 5
    assert ws.query_json(Sale, {"aggregate": {"spread": {"spread_test": {"field": "units"}}}}).execute().scalar() == 5
    operation = ws.query(hb.MetaSchema).where(hb.MetaSchema.object_id == "operation:spread_test").execute().rows()
    assert operation and "families" not in operation[0]["meta"]
    record = ws.module_resolver.inspect("operation", "spread_test")[0]["record"]
    assert record["meta"]["tags"] == {"aggregate": True}


def test_native_and_fold_routes_share_semantics_and_explain_decisions():
    """Registered native compilation and portable folding should return identical groups."""
    ws = _workspace("aggregate-explain-ws")
    folded_ws = _workspace("aggregate-explain-fold-ws", {"type": "inmem"})
    query = ws.query(Sale).group_by(Sale.region).aggregate(rows=Sale.object_id.count(), low=Sale.units.min(), high=Sale.units.max())
    folded = folded_ws.query(Sale).group_by(Sale.region).aggregate(rows=Sale.object_id.count(), low=Sale.units.min(), high=Sale.units.max())
    step = query.explain()["steps"][-1]

    assert step == {
        "node": "aggregate",
        "group_by": ["region"],
        "metrics": {
            "rows": {"count": {"field": "object_id"}},
            "low": {"min": {"field": "units"}},
            "high": {"max": {"field": "units"}},
        },
        "handler_mode": "native",
        "fallback_reason": None,
        "backend": "main",
        "type": "sqlite",
        "compiler": "sqlite",
        "portable_proof": True,
        "unsafe": False,
    }
    assert folded.explain()["steps"][-1]["fallback_reason"] == "native_aggregate_compiler_not_registered"
    assert query.execute().rows() == folded.execute().rows()
    with pytest.raises(ValueError, match="count"):
        query.count()
    with pytest.raises(ValueError, match="count"):
        ws.query(Sale).where(Sale.units > 2).limit(1).count()
    count_frame = ws.query(Sale).where(Sale.units > 2).count().execute()
    assert count_frame.identity_column is None
    assert count_frame.scalar() == 2

    arithmetic = ws.query(Sale).aggregate(total=Sale.units.sum(), average=Sale.units.avg())
    arithmetic_step = arithmetic.explain()["steps"][-1]
    assert arithmetic_step["handler_mode"] == "py_fold"
    assert arithmetic_step["fallback_reason"] == "backend_reducer_not_registered:avg,sum"
    assert arithmetic.execute().rows() == [{"total": 10, "average": 2.5}]
    float_distinct = ws.query(Sale).aggregate(unique_prices=Sale.price.count(distinct=True))
    assert float_distinct.explain()["steps"][-1]["fallback_reason"] == "floating_distinct_parity_not_proven"
    assert float_distinct.execute().scalar() == 4
    assert ws.handlers.aggregate_compiler("starrocks") is None
    assert ws.handlers.aggregate_compiler("trino") is None


def test_non_native_filter_is_planned_for_fold_before_provider_execution():
    """A scan-only filter should select the fold route rather than fail over after IO."""
    ws = _workspace("aggregate-filter-fold-ws")
    query = ws.query(Sale).where(Sale.payload == {"kind": "missing"}).aggregate(rows=Sale.object_id.count())

    step = query.explain()["steps"][-1]
    assert step["handler_mode"] == "py_fold"
    assert step["fallback_reason"] == "filter_not_native"
    assert query.execute().scalar() == 0


def test_duckdb_native_safe_subset_when_optional_driver_is_available():
    """DuckDB should opt into only the same parity-safe registered SQL subset."""
    pytest.importorskip("duckdb_engine")
    ws = _workspace("aggregate-duckdb-ws", {"type": "duckdb", "database": ":memory:"})
    query = ws.query(Sale).aggregate(rows=Sale.object_id.count(), low=Sale.units.min(), high=Sale.units.max())

    step = query.explain()["steps"][-1]
    assert step["handler_mode"] == "native"
    assert step["compiler"] == "duckdb"
    assert query.execute().rows() == [{"rows": 4, "low": 0, "high": 5}]


def _live_sql_backend(provider):
    """Build one Docker-backed SQL provider from explicit external-test credentials."""
    drivers = {"postgres": "psycopg2", "mysql": "pymysql", "mssql": "pymssql"}
    if find_spec(drivers[provider]) is None:
        pytest.skip(f"{drivers[provider]} is not installed")
    password = os.environ.get(f"HVNB_MATRIX_{provider.upper()}_PASSWORD")
    if not password:
        pytest.skip(f"HVNB_MATRIX_{provider.upper()}_PASSWORD is not configured")
    settings = {
        "postgres": (hb.backends.PostgresBackend, "postgres", "postgres", 5432),
        "mysql": (hb.backends.MySQLBackend, "mysql", "root", 3306),
        "mssql": (hb.backends.MsSQLBackend, "hvnb_pr2122_mssql", "sa", 1433),
    }
    backend_type, database, default_user, default_port = settings[provider]
    return backend_type(
        "main",
        database=database,
        host=os.environ.get(f"HVNB_MATRIX_{provider.upper()}_HOST", "localhost"),
        port=int(os.environ.get(f"HVNB_MATRIX_{provider.upper()}_PORT", default_port)),
        username=os.environ.get(f"HVNB_MATRIX_{provider.upper()}_USER", default_user),
        password=password,
    )


@pytest.mark.external
@pytest.mark.parametrize("provider", ("postgres", "mysql", "mssql"))
def test_live_sql_index_and_aggregate_contract(provider):
    """Real SQL providers should enforce index intent and share safe aggregate routing."""
    backend = _live_sql_backend(provider)
    ws = hb.HeavenBase(f"pr2122-{provider}-live-ws", detached=True, backends={"main": backend})

    class LiveSale(hb.Entity):
        identifier = f"pr2122-{provider}-live-sale"

        code = hb.field(hb.ShortText).optional().unique()
        region = hb.field(hb.Integer).index()
        amount = hb.field(hb.Integer)

    try:
        ws.register(LiveSale)
        ws.upsert_many(
            LiveSale,
            [
                {"object_id": "s1", "code": None, "region": 1, "amount": 2},
                {"object_id": "s2", "code": None, "region": 1, "amount": 3},
                {"object_id": "s3", "code": "kept", "region": 2, "amount": 5},
            ],
        )

        indexes = backend.db.index_column_sets(backend.tables[LiveSale.identifier])
        assert indexes[("code",)] is True
        assert indexes[("region",)] is False
        with pytest.raises(Exception):
            ws.upsert(LiveSale, {"object_id": "s4", "code": "kept", "region": 3, "amount": 8})
        assert ws.get("s4", entity=LiveSale) is None

        grouped = ws.query(LiveSale).group_by(LiveSale.region).aggregate(rows=LiveSale.object_id.count(), low=LiveSale.amount.min(), high=LiveSale.amount.max())
        step = grouped.explain()["steps"][-1]
        assert step["handler_mode"] == "py_fold"
        assert step["fallback_reason"] == "text_semantics_not_proven"
        assert grouped.order_by("region").execute().rows() == [
            {"region": 1, "rows": 2, "low": 2, "high": 3},
            {"region": 2, "rows": 1, "low": 5, "high": 5},
        ]
        assert grouped.having({"high": {"$lt": 5}}).execute().rows() == [{"region": 1, "rows": 2, "low": 2, "high": 3}]

        distinct = ws.query(LiveSale).aggregate(regions=LiveSale.region.count(distinct=True))
        assert distinct.explain()["steps"][-1]["handler_mode"] == "native"
        assert distinct.execute().scalar() == 2

        arithmetic = ws.query(LiveSale).aggregate(total=LiveSale.amount.sum(), average=LiveSale.amount.avg())
        arithmetic_step = arithmetic.explain()["steps"][-1]
        assert arithmetic_step["handler_mode"] == "py_fold"
        assert arithmetic_step["fallback_reason"] == "backend_reducer_not_registered:avg,sum"
        assert arithmetic.execute().rows() == [{"total": 10, "average": pytest.approx(10 / 3)}]

        text_group = ws.query(LiveSale).group_by(LiveSale.code).aggregate(rows=LiveSale.object_id.count())
        text_step = text_group.explain()["steps"][-1]
        assert text_step["handler_mode"] == "py_fold"
        assert text_step["fallback_reason"] == "text_semantics_not_proven"
    finally:
        ws.drop()
