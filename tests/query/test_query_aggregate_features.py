"""Aggregate parameter, HAVING, and registered-reducer behavior tests."""

import math

import pytest

import heavenbase as hb
from heavenbase.utils.config import ConfigManager

_NORMALIZED_DEFAULT_SEEN = []
_MUTATING_SEEN_TYPES = []


def _strict_options_fold(values, config):
    """Return the size of one required JSON-addressable config mapping."""
    return len(config)


def _spread_fold(values, scale=1.0):
    """Return a scaled numeric range."""
    return (max(values) - min(values)) * scale if values else None


def _json_summary_fold(values):
    """Return a JSON summary used by typed HAVING tests."""
    return {"flag": bool(values)}


def _normalized_default_fold(values, scale=2):
    """Return a scaled sum after option normalization."""
    return sum(values) * scale


def _normalize_default_options(options):
    """Track and normalize one reducer's detached default options."""
    _NORMALIZED_DEFAULT_SEEN.append(dict(options))
    return {"scale": float(options["scale"])}


def _mutating_options_fold(values, config):
    """Attempt to mutate detached request options during a fold."""
    _MUTATING_SEEN_TYPES.append((isinstance(config, dict), isinstance(config["weights"], list)))
    result = len(config["weights"])
    config["weights"].append("fold-mutation")
    return result


def _mutable_default_fold(values, cache=[]):
    """Mutate a default list whose detached binding must isolate each call."""
    cache.append(len(values))
    return len(cache)


def _ordered_values_fold(values):
    """Join values in the order supplied to a custom portable fold."""
    return ",".join(str(value) for value in values)


class AggregateFeatureRow(hb.Entity):
    """Small aggregate fixture with duplicates, nulls, and stable identities."""

    identifier = "aggregate-feature-row"

    region = hb.field(hb.ShortText)
    category = hb.field(hb.ShortText).optional()
    units = hb.field(hb.Integer)


class SplitAggregateFeatureRow(hb.Entity):
    """Aggregate fixture split across enumerable and fetch-only row routes."""

    identifier = "split-aggregate-feature-row"

    region = hb.field(hb.ShortText).required().store(to="rows")
    units = hb.field(hb.Integer).required().store(to="values")


class OptionalLookupAggregateRow(hb.Entity):
    """Aggregate fixture with an unrelated sparse lookup-only route."""

    identifier = "optional-lookup-aggregate-row"

    region = hb.field(hb.ShortText).store(to="rows")
    units = hb.field(hb.Integer).store(to="rows")
    score = hb.field(hb.Integer).optional().store(to="optional")


class SplitOptionalIdentityRow(hb.Entity):
    """Optional split fixture used to prove identity-route agreement."""

    identifier = "split-optional-identity-row"

    left_value = hb.field(hb.Integer).optional().store(to="left")
    right_value = hb.field(hb.Integer).optional().store(to="right")


class _TrackingInMemBackend(hb.backends.InMemBackend):
    """In-memory backend that records point hydration calls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.get_many_calls = 0

    def get_many(self, entity_id, object_ids, *, columns=None):
        """Count and delegate one point-hydration request."""
        self.get_many_calls += 1
        return super().get_many(entity_id, object_ids, columns=columns)


class _FetchOnlyInMemBackend(_TrackingInMemBackend):
    """In-memory fake with Pinecone-like known-ID lookup but no enumeration."""

    def scan(self, entity_id: str) -> list[dict]:
        """Reject exhaustive scans while preserving direct identity lookup."""
        raise NotImplementedError("scan unavailable")

    def exact_count(self, entity_id: str) -> int:
        """Opt this lookup-only test backend into its exact in-memory count."""
        return self.count(entity_id)


class _ProoflessFetchOnlyInMemBackend(_FetchOnlyInMemBackend):
    """Lookup-only fake whose physical count cannot prove completeness."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.exact_count_calls = 0

    def exact_count(self, entity_id: str) -> int:
        """Reject exact-count proofs while tracking attempts."""
        self.exact_count_calls += 1
        raise NotImplementedError(f"no exact count for {entity_id}")


class _SparseFetchOnlyInMemBackend(_FetchOnlyInMemBackend):
    """Lookup-only fake that stores fragments only for non-null optional values."""

    def upsert(self, rows):
        """Drop null optional fragments while preserving deletes."""
        super().upsert([row for row in rows if row.is_delete() or row.values.get("score") is not None])


def _workspace(name: str, backend=None) -> hb.HeavenBase:
    """Create an aggregate workspace with deliberately shuffled source rows."""
    ws = hb.HeavenBase(name, detached=True, backends={"main": backend or {"type": "inmem"}})
    ws.register(AggregateFeatureRow)
    ws.upsert_many(
        AggregateFeatureRow,
        [
            {"object_id": "s3", "region": "east", "category": "a", "units": 30},
            {"object_id": "s1", "region": "east", "category": "a", "units": 10},
            {"object_id": "s5", "region": "west", "category": None, "units": 50},
            {"object_id": "s2", "region": "east", "category": "b", "units": 20},
            {"object_id": "s4", "region": "west", "category": "b", "units": 40},
        ],
    )
    return ws


@pytest.mark.parametrize(
    "backend",
    ({"type": "inmem"}, {"type": "sqlite", "database": ":memory:"}),
    ids=("fold", "sqlite"),
)
def test_having_filters_aggregate_rows_before_order_and_limit(backend):
    """HAVING should see aliases and run before aggregate result pagination."""
    ws = _workspace(f"aggregate-feature-having-{backend['type']}-ws", backend)
    query = (
        ws.query(AggregateFeatureRow)
        .group_by(AggregateFeatureRow.region)
        .aggregate(high=AggregateFeatureRow.units.max(), rows=AggregateFeatureRow.object_id.count())
        .having({"high": {"$lt": 40}})
        .order_by("high", desc=True)
        .limit(1)
    )

    assert query.execute().rows() == [{"region": "east", "high": 30, "rows": 3}]
    aggregate_step = query.explain()["steps"][-1]
    assert aggregate_step["having"] == {"filter": {"field": "high", "op": "lt", "value": 40}}
    assert aggregate_step["handler_mode"] == ("native" if backend["type"] == "sqlite" else "py_fold")

    json_query = ws.query_json(
        AggregateFeatureRow,
        {
            "group_by": ["region"],
            "aggregate": {"high": {"max": {"field": "units"}}},
            "having": {"$or": [{"region": "east"}, {"high": {"$gt": 100}}]},
            "order": ["region"],
        },
    )
    assert json_query.execute().rows() == [{"region": "east", "high": 30}]


def test_having_requires_aggregate_outputs_and_registered_predicates():
    """HAVING should reject pre-aggregate use, unknown aliases, and unknown predicates."""
    ws = _workspace("aggregate-feature-having-validation-ws")

    with pytest.raises(ValueError, match=r"having\(\) needs aggregate"):
        ws.query(AggregateFeatureRow).having({"units": {"$gt": 0}}).spec
    with pytest.raises(ValueError, match="Unknown HAVING field 'units'"):
        ws.query(AggregateFeatureRow).aggregate(total=AggregateFeatureRow.units.sum()).having({"units": {"$gt": 0}}).execute()
    with pytest.raises(LookupError, match="Unknown operation"):
        ws.query(AggregateFeatureRow).aggregate(total=AggregateFeatureRow.units.sum()).having({"total": {"$missing": 0}}).execute()
    with pytest.raises(LookupError, match="integer aggregate fields do not support match predicates"):
        ws.query(AggregateFeatureRow).aggregate(total=AggregateFeatureRow.units.sum()).having({"total": {"$match": "1"}}).execute()


def test_having_range_predicate_treats_a_null_metric_as_no_match():
    """Empty-input nullable metrics should be filterable without Python comparison errors."""
    ws = _workspace("aggregate-feature-null-having-ws")

    assert (
        ws.query(AggregateFeatureRow)
        .where(AggregateFeatureRow.units > 100)
        .aggregate(average=AggregateFeatureRow.units.avg())
        .having({"average": {"$gt": 1}})
        .execute()
        .rows()
        == []
    )


@pytest.mark.parametrize(
    "backend",
    ({"type": "inmem"}, {"type": "sqlite", "database": ":memory:"}),
    ids=("fold", "native"),
)
def test_having_can_test_nullable_metric_results_for_null(backend):
    """Typed HAVING normalization must preserve null equality and membership."""
    ws = _workspace(f"aggregate-feature-null-having-{backend['type']}-ws", backend)

    def empty_max():
        return ws.query(AggregateFeatureRow).where(AggregateFeatureRow.units > 100).aggregate(high=AggregateFeatureRow.units.max())

    assert empty_max().having({"high": None}).execute().rows() == [{"high": None}]
    assert empty_max().having({"high": {"$in": [None, 100]}}).execute().rows() == [{"high": None}]
    assert empty_max().having({"high": {"$ne": None}}).execute().rows() == []


@pytest.mark.parametrize(
    "backend",
    ({"type": "inmem"}, {"type": "sqlite", "database": ":memory:"}),
    ids=("fold", "sqlite"),
)
def test_nullable_preaggregate_filter_keeps_portable_two_valued_semantics(backend):
    """Null values should preserve the established portable predicate results."""
    ws = _workspace(f"aggregate-feature-null-filter-{backend['type']}-ws", backend)
    query = ws.query(AggregateFeatureRow).where(AggregateFeatureRow.category != "a").aggregate(rows=AggregateFeatureRow.object_id.count())

    assert query.execute().scalar() == 3
    assert (
        ws.query(AggregateFeatureRow)
        .where(AggregateFeatureRow.category.in_(["a", None]))
        .aggregate(rows=AggregateFeatureRow.object_id.count())
        .execute()
        .scalar()
        == 3
    )
    assert (
        ws.query(AggregateFeatureRow).where(~AggregateFeatureRow.category.in_(["a"])).aggregate(rows=AggregateFeatureRow.object_id.count()).execute().scalar()
        == 3
    )
    assert (
        ws.query(AggregateFeatureRow).where(AggregateFeatureRow.category.contains("")).aggregate(rows=AggregateFeatureRow.object_id.count()).execute().scalar()
        == 5
    )
    if backend["type"] == "sqlite":
        assert query.explain()["steps"][-1]["handler_mode"] == "native"
        assert query.explain()["steps"][-1]["portable_proof"] is True


def test_logical_filter_with_a_scan_child_declines_native_aggregation():
    """A native logical parent must not hide a Python-scan child from routing."""
    ws = _workspace("aggregate-feature-mixed-filter-ws", {"type": "sqlite", "database": ":memory:"})
    query = (
        ws.query(AggregateFeatureRow)
        .where(AggregateFeatureRow.region.wildcard("[ew]*") & (AggregateFeatureRow.units > 15))
        .aggregate(total=AggregateFeatureRow.units.sum())
    )

    assert query.explain()["steps"][-1]["fallback_reason"] == "text_semantics_not_proven"
    assert query.execute().scalar() == 140


def test_fold_filtering_preserves_registered_type_operation_support():
    """Portable aggregate filtering must not broaden unsupported filter pairs."""
    ws = _workspace("aggregate-feature-filter-support-ws")

    with pytest.raises(LookupError, match="integer fields do not support text match"):
        ws.query(AggregateFeatureRow).where(AggregateFeatureRow.units.match("1")).aggregate(total=AggregateFeatureRow.units.sum()).execute()


def test_parameterized_metrics_use_the_canonical_json_object_shape():
    """Reducer options should serialize under the operation beside a required field."""
    ws = _workspace("aggregate-feature-shape-ws")
    spec = (
        ws.query(AggregateFeatureRow)
        .aggregate(
            unique=AggregateFeatureRow.category.count(distinct=True),
            lower_quartile=AggregateFeatureRow.units.percentile(0.25),
            middle=AggregateFeatureRow.units.median(),
        )
        .spec
    )

    assert spec.aggregate is not None
    assert spec.aggregate.to_dict()["metrics"] == {
        "unique": {"count": {"field": "category", "distinct": True}},
        "lower_quartile": {"percentile": {"field": "units", "q": 0.25}},
        "middle": {"median": {"field": "units"}},
    }
    assert ws.query_json(
        AggregateFeatureRow,
        {
            "aggregate": {
                "unique": {"count": {"field": "category", "distinct": True}},
                "lower_quartile": {"percentile": {"field": "units", "q": 0.25}},
                "middle": {"median": {"field": "units"}},
            }
        },
    ).execute().rows() == [{"unique": 2, "lower_quartile": 20.0, "middle": 30.0}]
    with pytest.raises(TypeError, match="parameters must be an object"):
        ws.query_json(AggregateFeatureRow, {"aggregate": {"middle": {"median": "units"}}})


@pytest.mark.parametrize(
    "backend",
    ({"type": "inmem"}, {"type": "sqlite", "database": ":memory:"}),
    ids=("fold", "sqlite"),
)
def test_count_distinct_skips_nulls_and_preserves_empty_group_semantics(backend):
    """Distinct count should match SQL null, grouped, and empty-input behavior."""
    ws = _workspace(f"aggregate-feature-count-distinct-{backend['type']}-ws", backend)

    ungrouped = ws.query(AggregateFeatureRow).aggregate(unique=AggregateFeatureRow.category.count(distinct=True))
    assert ungrouped.execute().scalar() == 2
    assert ungrouped.explain()["steps"][-1]["handler_mode"] == ("native" if backend["type"] == "sqlite" else "py_fold")
    grouped = (
        ws.query(AggregateFeatureRow)
        .group_by(AggregateFeatureRow.region)
        .aggregate(unique=AggregateFeatureRow.category.count(distinct=True))
        .order_by("region")
        .execute()
    )
    assert grouped.rows() == [{"region": "east", "unique": 2}, {"region": "west", "unique": 1}]
    assert ws.query(AggregateFeatureRow).where(AggregateFeatureRow.units > 100).aggregate(
        unique=AggregateFeatureRow.category.count(distinct=True)
    ).execute().rows() == [{"unique": 0}]
    assert (
        ws.query(AggregateFeatureRow)
        .where(AggregateFeatureRow.units > 100)
        .group_by(AggregateFeatureRow.region)
        .aggregate(unique=AggregateFeatureRow.category.count(distinct=True))
        .execute()
        .rows()
        == []
    )


def test_median_and_percentile_cover_boundaries_interpolation_and_empty_input():
    """Continuous percentiles should interpolate and return floats at unit bounds."""
    ws = _workspace("aggregate-feature-percentile-ws")
    query = (
        ws.query(AggregateFeatureRow)
        .where(AggregateFeatureRow.units < 50)
        .aggregate(
            middle=AggregateFeatureRow.units.median(),
            minimum=AggregateFeatureRow.units.percentile(0.0),
            lower_quartile=AggregateFeatureRow.units.percentile(0.25),
            maximum=AggregateFeatureRow.units.percentile(1.0),
        )
    )

    assert query.execute().rows() == [{"middle": 25.0, "minimum": 10.0, "lower_quartile": 17.5, "maximum": 40.0}]
    assert ws.query(AggregateFeatureRow).where(AggregateFeatureRow.units > 100).aggregate(
        middle=AggregateFeatureRow.units.median(), percentile=AggregateFeatureRow.units.percentile(0.5)
    ).execute().rows() == [{"middle": None, "percentile": None}]


@pytest.mark.parametrize(
    ("q", "error", "message"),
    [
        (True, TypeError, "q must be a float"),
        ("not-a-number", TypeError, "q must be a float"),
        (-0.01, ValueError, "q must be between 0 and 1 inclusive"),
        (1.01, ValueError, "q must be between 0 and 1 inclusive"),
        (math.inf, ValueError, "q must be between 0 and 1 inclusive"),
        (math.nan, ValueError, "q must be between 0 and 1 inclusive"),
    ],
)
def test_percentile_rejects_invalid_controls(q, error, message):
    """Percentile controls should be finite numeric values in the closed unit interval."""
    with pytest.raises(error, match=message):
        AggregateFeatureRow.units.percentile(q)


def test_json_reducer_parameters_are_required_and_strict():
    """JSON parameter objects should bind through the reducer signature."""
    ws = _workspace("aggregate-feature-json-parameter-validation-ws")

    with pytest.raises(TypeError, match="missing a required argument: 'q'"):
        ws.query_json(AggregateFeatureRow, {"aggregate": {"p": {"percentile": {"field": "units"}}}})
    with pytest.raises(TypeError, match="unexpected keyword argument 'extra'"):
        ws.query_json(AggregateFeatureRow, {"aggregate": {"p": {"percentile": {"field": "units", "q": 0.5, "extra": True}}}})
    with pytest.raises(TypeError, match="distinct must be a boolean"):
        ws.query_json(AggregateFeatureRow, {"aggregate": {"unique": {"count": {"field": "category", "distinct": 1}}}})

    hb.ext.register_aggregate_reducer("strict_options", _strict_options_fold, replace=True)
    with pytest.raises(TypeError, match="parameters must be JSON-compatible"):
        AggregateFeatureRow.units.strict_options(config={1: "value"})


def test_parameterized_custom_reducer_is_live_in_python_and_json():
    """One registration should expose a dynamic field method and JSON parameter binding."""
    ws = _workspace("aggregate-feature-custom-ws")
    hb.ext.register_aggregate_reducer(
        "spread",
        _spread_fold,
        tags={"aggregate-input-modes": ["numeric"]},
        result_type=hb.Float,
        replace=True,
    )

    python_query = ws.query(AggregateFeatureRow).aggregate(spread=AggregateFeatureRow.units.spread(scale=2.0))
    generic_query = ws.query(AggregateFeatureRow).aggregate(spread=AggregateFeatureRow.units.aggregate("spread", scale=2.0))
    json_query = ws.query_json(
        AggregateFeatureRow,
        {"aggregate": {"spread": {"spread": {"field": "units", "scale": 2.0}}}},
    )

    assert python_query.execute().scalar() == 80.0
    assert generic_query.execute().scalar() == 80.0
    assert json_query.execute().scalar() == 80.0
    assert ws.query(AggregateFeatureRow).aggregate(spread=AggregateFeatureRow.units.spread()).having({"spread": {"$gt": "39"}}).execute().scalar() == 40.0
    with pytest.raises(LookupError, match="float aggregate fields do not support match predicates"):
        ws.query(AggregateFeatureRow).aggregate(spread=AggregateFeatureRow.units.spread()).having({"spread": {"$match": "40"}}).execute()
    assert python_query.spec.aggregate.to_dict()["metrics"] == {"spread": {"spread": {"field": "units", "scale": 2.0}}}
    reducer = next(item for item in hb.ext.aggregate_reducers() if item.identifier == "spread")
    assert reducer.to_dict()["parameters"] == [{"name": "scale", "required": False, "default": 1.0}]
    assert reducer.to_dict()["result_type"] == "float"


def test_json_result_reducer_uses_typed_having_predicates():
    """JSON reducer outputs must retain strict JSON equality in HAVING."""
    hb.ext.register_aggregate_reducer(
        "json_summary",
        _json_summary_fold,
        tags={"aggregate-input-modes": ["numeric"]},
        result_type=hb.Json,
        replace=True,
    )
    ws = _workspace("aggregate-feature-json-result-ws")
    query = ws.query(AggregateFeatureRow).aggregate(summary=AggregateFeatureRow.units.json_summary())

    assert query.having({"summary": {"$eq": {"flag": True}}}).execute().scalar() == {"flag": True}
    assert query.having({"summary": {"$eq": {"flag": 1}}}).execute().rows() == []


def test_reducer_option_normalizer_receives_isolated_defaults():
    """Omitted defaults should be validated without becoming explicit request options."""
    _NORMALIZED_DEFAULT_SEEN.clear()

    hb.ext.register_aggregate_reducer(
        "normalized_default",
        _normalized_default_fold,
        normalize_options=_normalize_default_options,
        result_type=hb.Float,
        replace=True,
    )
    metric = AggregateFeatureRow.units.normalized_default()
    assert dict(metric.options) == {}

    ws = _workspace("aggregate-feature-normalized-default-ws")
    assert ws.query(AggregateFeatureRow).aggregate(total=metric).execute().scalar() == 300.0
    assert _NORMALIZED_DEFAULT_SEEN and all(item == {"scale": 2} for item in _NORMALIZED_DEFAULT_SEEN)


def test_aggregate_options_are_deeply_frozen_and_thawed_for_each_fold():
    """Nested reducer parameters must not let callers or folds mutate a query."""
    _MUTATING_SEEN_TYPES.clear()
    hb.ext.register_aggregate_reducer("mutable_options", _mutating_options_fold, replace=True)
    source = {"weights": [2]}
    metric = AggregateFeatureRow.units.mutable_options(config=source)
    source["weights"].append(3)

    with pytest.raises(AttributeError):
        metric.options["config"]["weights"].append(4)
    serialized = metric.to_dict()
    hb.dumps_json(serialized)
    serialized["mutable_options"]["config"]["weights"].append(5)
    assert metric.to_dict()["mutable_options"]["config"]["weights"] == [2]

    ws = _workspace("aggregate-feature-frozen-options-ws")
    query = ws.query(AggregateFeatureRow).aggregate(size=metric)
    assert query.execute().scalar() == 1
    assert query.execute().scalar() == 1
    assert _MUTATING_SEEN_TYPES == [(True, True), (True, True)]


def test_mutable_reducer_defaults_are_isolated_per_execution():
    """Python default containers must not become hidden reducer state."""

    hb.ext.register_aggregate_reducer("mutable_default", _mutable_default_fold, replace=True)
    ws = _workspace("aggregate-feature-mutable-default-ws")
    query = ws.query(AggregateFeatureRow).aggregate(size=AggregateFeatureRow.units.mutable_default())

    assert query.execute().scalar() == 1
    assert query.execute().scalar() == 1


def test_builtin_reducers_cannot_be_replaced():
    """Core wrapper and native-compiler semantics must stay registry-stable."""
    for op in ("count", "min", "max", "sum", "avg", "median", "percentile"):
        with pytest.raises(ValueError, match=rf"Built-in aggregate reducer '{op}' cannot be replaced"):
            hb.ext.register_aggregate_reducer(op, lambda values: 999, replace=True)

    fold = _workspace("aggregate-feature-protected-count-fold-ws")
    native = _workspace("aggregate-feature-protected-count-native-ws", {"type": "sqlite", "database": ":memory:"})
    assert fold.query(AggregateFeatureRow).aggregate(unique=AggregateFeatureRow.category.count(distinct=True)).execute().scalar() == 2
    assert native.query(AggregateFeatureRow).aggregate(unique=AggregateFeatureRow.category.count(distinct=True)).execute().scalar() == 2


def test_custom_reducer_registration_enforces_dynamic_json_addressability():
    """Invalid method names, collisions, signatures, and defaults should fail early."""

    async def async_fold(values):
        return len(values)

    with pytest.raises(ValueError, match="public Python identifier"):
        hb.ext.register_aggregate_reducer("not-dynamic", lambda values: len(values))
    with pytest.raises(ValueError, match=r"collides with FieldRef\.match"):
        hb.ext.register_aggregate_reducer("match", lambda values: len(values))
    with pytest.raises(ValueError, match=r"collides with FieldRef\.name"):
        hb.ext.register_aggregate_reducer("name", lambda values: len(values))
    with pytest.raises(TypeError, match="must be named and JSON-addressable"):
        hb.ext.register_aggregate_reducer("variadic_reducer", lambda values, *args: len(values))
    with pytest.raises(ValueError, match="parameter name 'field' is reserved"):
        hb.ext.register_aggregate_reducer("reserved_parameter", lambda values, field=None: len(values))
    with pytest.raises(TypeError, match="default must be JSON-compatible"):
        hb.ext.register_aggregate_reducer("opaque_default", lambda values, marker=object(): len(values))
    with pytest.raises(TypeError, match="default must be JSON-compatible"):
        hb.ext.register_aggregate_reducer("non_string_key_default", lambda values, marker={1: "value"}: len(values))
    with pytest.raises(TypeError, match="fold must be synchronous"):
        hb.ext.register_aggregate_reducer("async_reducer", async_fold)
    with pytest.raises(TypeError, match="result_type resolver must accept source dtype and options"):
        hb.ext.register_aggregate_reducer("invalid_result_type", lambda values: len(values), result_type=lambda source: source)


def test_aggregate_method_contract_covers_the_field_ref_namespace():
    """Query-owned reducer names must track the concrete field DSL surface."""
    from heavenbase.entity.ref import FieldRef
    from heavenbase.query.spec import _check_aggregate_method_name

    wrappers = {"avg", "code_reduce", "count", "max", "median", "min", "percentile", "sum"}
    reserved = {"entity_id", "name", *(name for name in FieldRef.__dict__ if not name.startswith("_"))} - wrappers

    for name in sorted(reserved):
        with pytest.raises(ValueError, match=rf"collides with FieldRef\.{name}"):
            _check_aggregate_method_name(name)


def test_portable_custom_reducers_receive_values_in_object_id_order():
    """Custom reducer input order should not inherit backend scan or insertion order."""
    hb.ext.register_aggregate_reducer(
        "ordered_values",
        _ordered_values_fold,
        tags={"aggregate-input-modes": ["numeric"]},
        replace=True,
    )
    ws = _workspace("aggregate-feature-custom-order-ws")

    assert ws.query(AggregateFeatureRow).aggregate(values=AggregateFeatureRow.units.ordered_values()).execute().scalar() == "10,20,30,40,50"


@pytest.mark.parametrize(
    "backend",
    ({"type": "inmem"}, {"type": "sqlite", "database": ":memory:"}),
    ids=("fold", "sqlite"),
)
def test_default_group_order_uses_first_object_id_before_pagination(backend):
    """Native and folded groups should share the canonical first-identity order."""
    ws = hb.HeavenBase(f"aggregate-feature-group-order-{backend['type']}-ws", detached=True, backends={"main": backend})
    ws.register(AggregateFeatureRow)
    ws.upsert_many(
        AggregateFeatureRow,
        [
            {"object_id": "a", "region": "z", "category": None, "units": 1},
            {"object_id": "b", "region": "a", "category": None, "units": 2},
        ],
    )

    def grouped():
        return ws.query(AggregateFeatureRow).group_by(AggregateFeatureRow.region).aggregate(rows=AggregateFeatureRow.object_id.count())

    assert grouped().execute().rows() == [{"region": "z", "rows": 1}, {"region": "a", "rows": 1}]
    assert grouped().limit(1).execute().rows() == [{"region": "z", "rows": 1}]
    assert grouped().order_by("region").execute().rows() == [{"region": "a", "rows": 1}, {"region": "z", "rows": 1}]


def test_exact_fold_hydrates_fetch_only_routes_from_an_exhaustive_identity_route():
    """Mixed stores should fetch reducer values by IDs enumerated elsewhere."""
    rows = _TrackingInMemBackend("rows")
    values = _FetchOnlyInMemBackend("values")
    ws = hb.HeavenBase(
        "aggregate-feature-fetch-only-mixed-ws",
        detached=True,
        backends={"rows": rows, "values": values},
    )
    ws.register(SplitAggregateFeatureRow)
    ws.upsert_many(
        SplitAggregateFeatureRow,
        [
            {"object_id": "s2", "region": "west", "units": 20},
            {"object_id": "s1", "region": "east", "units": 10},
            {"object_id": "s3", "region": "east", "units": 30},
        ],
    )
    rows.get_many_calls = 0
    values.get_many_calls = 0

    assert (
        ws.query(SplitAggregateFeatureRow)
        .where(SplitAggregateFeatureRow.region == "east")
        .aggregate(total=SplitAggregateFeatureRow.units.sum())
        .execute()
        .scalar()
        == 40
    )
    assert rows.get_many_calls == 0
    assert values.get_many_calls == 1


def test_exact_fold_ignores_an_unrelated_proofless_optional_route():
    """An optional route outside the aggregate dependency set must not block a fold."""
    rows = _TrackingInMemBackend("rows")
    optional = _ProoflessFetchOnlyInMemBackend("optional")
    ws = hb.HeavenBase(
        "aggregate-feature-unused-lookup-ws",
        detached=True,
        backends={"rows": rows, "optional": optional},
    )
    ws.register(OptionalLookupAggregateRow)
    ws.upsert_many(
        OptionalLookupAggregateRow,
        [
            {"object_id": "s1", "region": "east", "units": 10, "score": 1},
            {"object_id": "s2", "region": "west", "units": 20, "score": None},
        ],
    )
    optional.get_many_calls = 0

    assert (
        ws.query(OptionalLookupAggregateRow)
        .where(OptionalLookupAggregateRow.region == "east")
        .aggregate(total=OptionalLookupAggregateRow.units.sum())
        .execute()
        .scalar()
        == 10
    )
    assert optional.get_many_calls == 0
    assert optional.exact_count_calls == 0

    with pytest.raises(RuntimeError, match="no exact fragment-count proof"):
        ws.query(OptionalLookupAggregateRow).aggregate(scores=OptionalLookupAggregateRow.score.count()).execute()
    assert optional.exact_count_calls == 1


@pytest.mark.parametrize("scores", ([7, None, None], [None, None, None]), ids=("sparse", "all-null"))
def test_exact_fold_treats_absent_optional_lookup_fragments_as_null(scores):
    """Exact sparse lookup counts should distinguish null fragments from hidden IDs."""
    rows = _TrackingInMemBackend("rows")
    optional = _SparseFetchOnlyInMemBackend("optional")
    ws = hb.HeavenBase(
        f"aggregate-feature-sparse-lookup-{sum(score is not None for score in scores)}-ws",
        detached=True,
        backends={"rows": rows, "optional": optional},
    )
    ws.register(OptionalLookupAggregateRow)
    ws.upsert_many(
        OptionalLookupAggregateRow,
        [{"object_id": f"s{index}", "region": "east", "units": index, "score": score} for index, score in enumerate(scores, start=1)],
    )
    optional.get_many_calls = 0

    result = ws.query(OptionalLookupAggregateRow).aggregate(
        scores=OptionalLookupAggregateRow.score.count(),
        total=OptionalLookupAggregateRow.score.sum(),
    )
    assert result.execute().rows() == [
        {"scores": sum(score is not None for score in scores), "total": sum(score for score in scores if score is not None) or None}
    ]
    assert optional.get_many_calls == 1


def test_exact_fold_rejects_hidden_lookup_only_identities():
    """An exact lookup count must expose fragments outside the enumerated identity set."""
    rows = _TrackingInMemBackend("rows")
    values = _FetchOnlyInMemBackend("values")
    ws = hb.HeavenBase(
        "aggregate-feature-hidden-lookup-identity-ws",
        detached=True,
        backends={"rows": rows, "values": values},
    )
    ws.register(SplitAggregateFeatureRow)
    ws.upsert_many(
        SplitAggregateFeatureRow,
        [
            {"object_id": "s1", "region": "east", "units": 10},
            {"object_id": "s2", "region": "west", "units": 20},
            {"object_id": "s3", "region": "east", "units": 30},
        ],
    )
    values.upsert([hb.ext.RowOp(SplitAggregateFeatureRow.identifier, "s4", {"units": 100})])

    with pytest.raises(RuntimeError, match=r"reported 4 fragment\(s\) but 3 were reachable"):
        ws.query(SplitAggregateFeatureRow).aggregate(total=SplitAggregateFeatureRow.units.sum()).execute()


def test_exact_fold_compares_every_scannable_identity_binding():
    """An object-id-only fold must not trust the first partial scanner."""
    left = hb.backends.InMemBackend("left")
    right = hb.backends.InMemBackend("right")
    ws = hb.HeavenBase(
        "aggregate-feature-partial-identity-ws",
        detached=True,
        backends={"left": left, "right": right},
    )
    ws.register(SplitOptionalIdentityRow)
    ws.upsert_many(
        SplitOptionalIdentityRow,
        [
            {"object_id": "s1", "left_value": 1, "right_value": 10},
            {"object_id": "s2", "left_value": 2, "right_value": 20},
        ],
    )
    left.delete(SplitOptionalIdentityRow.identifier, ["s2"])

    with pytest.raises(RuntimeError, match=r"scannable backend 'left'.*missing 1 routed identity"):
        ws.query(SplitOptionalIdentityRow).aggregate(rows=SplitOptionalIdentityRow.object_id.count()).execute()


def test_native_aggregate_declines_split_required_dependencies():
    """Native pushdown must not bypass a required fragment on another route."""
    rows = hb.backends.InMemBackend("rows")
    values = hb.backends.SQLiteBackend("values", database=":memory:")
    ws = hb.HeavenBase(
        "aggregate-feature-native-split-integrity-ws",
        detached=True,
        backends={"rows": rows, "values": values},
    )
    ws.register(SplitAggregateFeatureRow)
    ws.upsert_many(
        SplitAggregateFeatureRow,
        [
            {"object_id": "s1", "region": "east", "units": 10},
            {"object_id": "s2", "region": "west", "units": 20},
        ],
    )
    query = ws.query(SplitAggregateFeatureRow).aggregate(high=SplitAggregateFeatureRow.units.max())

    assert query.explain()["steps"][-1]["fallback_reason"] == "aggregate_dependencies_not_colocated"
    rows.delete(SplitAggregateFeatureRow.identifier, ["s2"])
    with pytest.raises(RuntimeError, match=r"scannable backend 'rows'.*missing 1 routed identity"):
        query.execute()


def test_exact_fold_fails_closed_without_an_exhaustive_identity_route():
    """A fetch-only backend must never be mistaken for an empty aggregate input."""
    ws = _workspace("aggregate-feature-fetch-only-fail-ws", _FetchOnlyInMemBackend("main"))

    with pytest.raises(RuntimeError, match="no exhaustive identity route"):
        ws.query(AggregateFeatureRow).aggregate(total=AggregateFeatureRow.units.sum()).execute()


class JsonPayloadRow(hb.Entity):
    """Fixture whose Json column holds objects, lists, and scalars."""

    identifier = "json-payload-row"

    region = hb.field(hb.ShortText)
    calls = hb.field(hb.Json).optional()
    tags = hb.field(hb.Json).optional()


def _payload_workspace(name: str) -> hb.HeavenBase:
    ws = hb.HeavenBase(name, detached=True, backends={"main": {"type": "inmem"}})
    ws.register(JsonPayloadRow)
    ws.upsert_many(
        JsonPayloadRow,
        [
            {"object_id": "p1", "region": "east", "calls": {"run": 9, "read": 7}, "tags": ["a", "b"]},
            {"object_id": "p2", "region": "east", "calls": {"run": 5}, "tags": ["a"]},
            {"object_id": "p3", "region": "west", "calls": {"read": 3}, "tags": ["c", "a"]},
            {"object_id": "p4", "region": "west", "calls": None, "tags": None},
        ],
    )
    return ws


def test_json_dict_and_list_folds_cover_every_row():
    """The registered folds answer per-key and per-element questions the spec cannot name."""
    ws = _payload_workspace("json-payload-folds")

    def agg(op, field, **extra):
        return ws.query_json(JsonPayloadRow, {"aggregate": {"r": {op: {"field": field}}}, **extra}).execute()

    assert agg("key_sums", "calls").scalar() == {"run": 14, "read": 10}  # values totalled, missing keys skipped not zeroed
    assert agg("key_counts", "calls").scalar() == {"read": 2, "run": 2}  # rows carrying each key
    assert agg("value_counts", "tags").scalar() == {"a": 3, "b": 1, "c": 1}  # elements inside the lists

    grouped = ws.query_json(
        JsonPayloadRow,
        {"group_by": ["region"], "aggregate": {"totals": {"key_sums": {"field": "calls"}}}, "order": ["region"]},
    ).execute()
    assert grouped.rows() == [{"region": "east", "totals": {"run": 14, "read": 7}}, {"region": "west", "totals": {"read": 3}}]

    assert ws.query(JsonPayloadRow).aggregate(t=JsonPayloadRow.calls.key_sums()).execute().scalar() == {"run": 14, "read": 10}


def test_json_folds_refuse_a_result_that_would_read_as_empty():
    """A fold that matched nothing must raise, since an empty mapping reads as real data."""
    ws = _payload_workspace("json-payload-guards")
    ws.upsert_many(
        JsonPayloadRow,
        [
            {"object_id": "s1", "region": "south", "calls": {"label": "text"}, "tags": ["z"]},
            {"object_id": "s2", "region": "south", "calls": {"nested": {"x": 1}}, "tags": ["z"]},
        ],
    )

    def agg(op, field, where):
        spec = {"filter": {"region": where}, "aggregate": {"r": {op: {"field": field}}}}
        return ws.query_json(JsonPayloadRow, spec).execute().scalar()

    with pytest.raises(ValueError, match="no numeric value"):
        agg("key_sums", "calls", "south")  # objects present, nothing summable
    with pytest.raises(ValueError, match="no object payload"):
        agg("key_sums", "tags", "south")  # lists are not objects
    with pytest.raises(ValueError, match="no countable element"):
        agg("value_counts", "calls", "south")  # objects are not elements

    assert agg("key_sums", "calls", "east") == {"run": 14, "read": 7}  # a sparse but foldable group still works


def test_json_key_sums_rejects_a_boolean_that_would_fold_as_one():
    """A Boolean under a numeric fold is rejected rather than counted as 0/1."""
    ws = _payload_workspace("json-payload-boolean")
    ws.upsert_many(JsonPayloadRow, [{"object_id": "b1", "region": "north", "calls": {"flag": True}, "tags": ["q"]}])

    with pytest.raises(ValueError, match="key_sums value"):
        ws.query_json(JsonPayloadRow, {"filter": {"region": "north"}, "aggregate": {"r": {"key_sums": {"field": "calls"}}}}).execute()


class CodeReduceRow(hb.Entity):
    """Small numeric fixture for the caller-supplied reduction."""

    identifier = "code-reduce-row"

    region = hb.field(hb.ShortText)
    amount = hb.field(hb.Integer)


def _code_workspace(name: str) -> hb.HeavenBase:
    ws = hb.HeavenBase(name, detached=True, backends={"main": {"type": "inmem"}})
    ws.register(CodeReduceRow)
    ws.upsert_many(
        CodeReduceRow,
        [
            {"object_id": "c1", "region": "east", "amount": 10},
            {"object_id": "c2", "region": "east", "amount": 41},
            {"object_id": "c3", "region": "west", "amount": 25},
        ],
    )
    return ws


def test_code_reduce_can_be_refused_engine_wide(monkeypatch):
    """The operator runs by default and one config key refuses it for every caller."""
    ws = _code_workspace("code-reduce-gate")
    metric = CodeReduceRow.amount.code_reduce(code="lambda v: sum(v)")

    assert ws.query(CodeReduceRow).aggregate(total=metric).execute().scalar() == 76

    monkeypatch.setitem(vars(hb.utils.CM_HVNB), "get", lambda key, default=None: False if key == "heavenbase.query.allow_code_reduce" else default)
    with pytest.raises(ValueError, match="disabled; set heavenbase.query.allow_code_reduce"):
        ws.query(CodeReduceRow).aggregate(total=metric).execute()


def test_code_reduce_uses_the_executing_workspace_config(tmp_path):
    """An isolated Context can refuse caller code even when the process default allows it."""
    config = ConfigManager(default={"heavenbase": {"query": {"allow_code_reduce": False}}}, setup=False)
    context = hb.Context.load(
        {
            "version": 1,
            "root": str(tmp_path),
            "workspace": "default",
            "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
            "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
        },
        config=config,
    )
    try:
        ws = hb.HeavenBase("code-reduce-context", detached=True, backends={"main": {"type": "inmem"}}, context=context)
        ws.register(CodeReduceRow)
        ws.upsert(CodeReduceRow, {"object_id": "c1", "region": "east", "amount": 10})
        metric = CodeReduceRow.amount.code_reduce(code="lambda v: sum(v)")

        with pytest.raises(ValueError, match="disabled; set heavenbase.query.allow_code_reduce"):
            ws.query(CodeReduceRow).aggregate(total=metric).execute()
    finally:
        context.close()


def test_code_reduce_fills_the_same_slot_as_a_registered_reducer():
    """The operator behaves like any other metric across both query surfaces."""
    ws = _code_workspace("code-reduce-surfaces")

    spread = "lambda values: max(values) - min(values)"
    assert ws.query(CodeReduceRow).aggregate(s=CodeReduceRow.amount.code_reduce(code=spread)).execute().scalar() == 31

    grouped = ws.query_json(
        CodeReduceRow,
        {
            "group_by": ["region"],
            "aggregate": {"s": {"code_reduce": {"field": "amount", "code": spread}}, "n": {"count": {"field": "object_id"}}},
            "order": ["region"],
        },
    ).execute()
    assert grouped.rows() == [{"region": "east", "s": 31, "n": 2}, {"region": "west", "s": 0, "n": 1}]


def test_code_reduce_dtype_normalizes_the_result():
    """``dtype`` declares the output column and normalizes into it, like a declared field."""
    ws = _code_workspace("code-reduce-dtype")
    mean = "lambda v: sum(v) / len(v)"

    def scalar(**options):
        return ws.query(CodeReduceRow).aggregate(x=CodeReduceRow.amount.code_reduce(code=mean, **options)).execute().scalar()

    assert scalar() == pytest.approx(76 / 3)  # untyped keeps whatever the expression produced
    assert scalar(dtype="float") == pytest.approx(76 / 3)  # identifier form
    assert scalar(dtype=hb.Float) == pytest.approx(76 / 3)  # type form, as a field declares one
    assert scalar(dtype=hb.Integer) == 25  # truncation is normalization, not an error

    bounded = CodeReduceRow.amount.code_reduce(code="lambda v: 'abc'", dtype=hb.ShortText(max_chars=3))
    assert bounded.to_dict()["code_reduce"]["dtype"] == {"type": "short-text", "max_chars": 3}
    assert ws.query(CodeReduceRow).aggregate(x=bounded).execute().scalar() == "abc"
    with pytest.raises(ValueError, match="exceeds 3 characters"):
        ws.query(CodeReduceRow).aggregate(x=CodeReduceRow.amount.code_reduce(code="lambda v: 'long'", dtype=hb.ShortText(max_chars=3))).execute()

    typed = (
        ws.query(CodeReduceRow)
        .group_by(CodeReduceRow.region)
        .aggregate(x=CodeReduceRow.amount.code_reduce(code=mean, dtype=hb.Float))
        .having({"x": {"$gte": 25.2}})
        .execute()
    )
    assert typed.rows() == [{"region": "east", "x": pytest.approx(25.5)}]  # a declared dtype restores typed HAVING

    integer_having = ws.query(CodeReduceRow).aggregate(x=CodeReduceRow.amount.code_reduce(code=mean, dtype=hb.Integer)).having({"x": {"$gte": "25"}}).execute()
    assert integer_having.scalar() == 25


def test_code_reduce_rejects_expressions_it_cannot_use():
    """Every failure names what was wrong instead of surfacing a bare interpreter error."""
    ws = _code_workspace("code-reduce-errors")

    def run(**options):
        return ws.query(CodeReduceRow).aggregate(x=CodeReduceRow.amount.code_reduce(**options)).execute().scalar()

    with pytest.raises(ValueError, match="does not parse"):
        run(code="lambda v:")
    with pytest.raises(ValueError, match="must evaluate to a callable"):
        run(code="1 + 1")
    with pytest.raises(KeyError, match="Unknown logical type"):
        run(code="lambda v: sum(v)", dtype="nope")
    with pytest.raises(ValueError):
        run(code="lambda v: 'text'", dtype=hb.Float)  # a result the declared type cannot accept
