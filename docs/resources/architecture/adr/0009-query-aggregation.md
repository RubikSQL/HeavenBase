# ADR 0009 - Query aggregation

Status: accepted

Date: 2026-07-15

Revised: 2026-07-23 — immutable Query and source-only terminal count contract

Revised: 2026-07-30 — dynamic Json sources: dotted-path metric sources and the `explode` clause

## Decision

Aggregation is an identityless projection on an immutable `Query`. Common
reducers are methods on field references:

```python
summary_rows = (
    ws.query(Sale)
    .where(Sale.created_at >= cutoff)
    .group_by(Sale.region)
    .aggregate(
        orders=Sale.object_id.count(),
        customers=Sale.customer_id.count(distinct=True),
        revenue=Sale.amount.sum(),
        average=Sale.amount.avg(),
        median=Sale.amount.median(),
        p95=Sale.amount.percentile(0.95),
    )
    .having({"revenue": {"$gte": 1000}})
    .order_by("revenue", desc=True)
    .execute()
    .rows()
)
```

This replaces tuple syntax such as `revenue=(Sale.amount, "sum")`. The field is
the subject, the reducer reads as an operation on it, and keyword assignment is
the output alias. `group_by` remains separate because grouping and result metrics
are independent concerns.

The canonical strict JSON shape is similarly direct:

```json
{
  "group_by": ["region"],
  "aggregate": {
    "orders": {"count": {"field": "object_id"}},
    "customers": {"count": {"field": "customer_id", "distinct": true}},
    "revenue": {"sum": {"field": "amount"}},
    "median": {"median": {"field": "amount"}},
    "p95": {"percentile": {"field": "amount", "q": 0.95}}
  },
  "having": {"revenue": {"$gte": 1000}},
  "order": ["-revenue"]
}
```

Every metric uses the same operation-plus-parameter-object shape, including
parameter-free reducers. Unknown top-level keys, multi-operation metric objects,
unknown reducers, missing/non-string fields, and invalid reducer parameters fail
at the JSON boundary.

## Request and execution ownership

- `AggregateMetric`, `Aggregate`, and `QuerySpec` are frozen normalized request
  values. Aggregate mappings use mapping proxies, group fields use tuples, and
  nested reducer option objects/arrays are recursively isolated and frozen.
- `Query` keeps only immutable authored request data. Every fluent transform
  returns a derived query, and the read-only `spec` property exposes its frozen
  normalized request. Query values never store builders, plans, fragments, or
  results.
- The reducer registry owns operation identity, accepted logical-type families,
  parameter signatures/defaults, empty-set behavior, and the portable fold.
  Custom reducers register through `hb.ext.register_aggregate_reducer`, appear
  as live field methods, and automatically enter operation, MetaSchema, and
  generated capability metadata. Query construction is live for existing
  workspaces; register extensions before workspace creation when their
  MetaSchema rows must be published during bootstrap.
- `_AggregateRouteService` owns schema-aware validation, native/fold selection,
  execution, universal post-aggregate `HAVING`, and the explain step. Providers
  do not receive query AST values.
- The portable fold operates on the complete post-filter/post-near/post-traverse
  materialized frame. It loads only routes that can affect the filter, groups,
  metrics, or required/default-bearing schema checks. A selected exhaustive
  scanner supplies the identity set; every other exhaustive `object_id` binding
  is scanned identity-only and must agree, without hydrating unrelated optional
  data. When selected dependencies are lookup-only, those identity bindings
  still supply the candidate set.
  Each selected lookup-only route must expose an exact physical fragment count,
  and the number of fragments fetched for the enumerated identities must match
  it. Missing optional fragments become `None`; an unproved count, hidden extra
  identity, or incomplete required/default-bearing row fails closed. Unrelated
  optional routes are neither fetched nor allowed to block the fold. Aggregation
  and `HAVING` happen before result ordering, offset, and limit.
- Aggregate output is a detached identityless `ResultFrame`. Application code
  explicitly exports it through `rows()`, or through `scalar()` when the result
  has exactly one row and one column.

Reducer parameters come from the fold signature, so one registration drives
both query surfaces without parser edits:

```python
hb.ext.register_aggregate_reducer(
    "spread",
    lambda values, scale=1.0: (max(values) - min(values)) * scale if values else None,
    type_families=(hb.ext.TYPE_FAMILY_AGG_NUMERIC,),
    result_type=hb.Float,
)

generic_metric = Sale.amount.aggregate("spread", scale=2.0)
dynamic_metric = Sale.amount.spread(scale=2.0)
```

The equivalent JSON metric is
`{"spread": {"spread": {"field": "amount", "scale": 2.0}}}`. Supplying
`result_type` gives the custom result the same typed `HAVING` validation and
operand normalization as a core reducer; `"source"` declares a reducer that
preserves its input logical type, and a resolver callable may derive the type
from the source field and normalized parameters.

Reducer identifiers must be public Python identifiers and must not collide with
existing `FieldRef` operations. Parameters must be named and JSON-compatible;
an optional registry normalizer can enforce richer value constraints. The seven
core reducer identifiers cannot be replaced because their public semantics and
native compiler contracts are fixed. They otherwise use the same registry
descriptor, portable fold, Python/JSON construction, result-type metadata, and
`HAVING` path as custom reducers.

## Core semantics

- Core reducers are `count`, `min`, `max`, `sum`, `avg`, `median`, and
  `percentile(q)`.
- `None` source values are skipped.
- `count(field)` counts non-null values. `count(object_id)` is row count because
  the primary key is always non-null.
- `count(field, distinct=True)` counts Python-equal non-null logical values once.
  Native SQL distinct is used only where equality parity is established; floats
  and collation-sensitive text otherwise use the portable fold.
- `percentile(q)` requires a finite `q` from `0.0` through `1.0` and computes the
  exact continuous percentile at rank `(n - 1) * q` with linear interpolation.
  `median()` is the same registered reducer family at `q=0.5`. Both return a
  `float` for non-empty numeric input.
- Ungrouped empty/all-null input returns one row: count is `0`; every other core
  reducer is `None`.
- Grouped empty input returns zero rows. `None` group keys form one group.
- `avg`, `median`, and `percentile` return `float`; `count` returns `int`;
  `min`, `max`, and `sum` retain their natural logical value.
- Reducer values and first-seen groups use ascending `object_id` order, making
  custom folds independent of backend scan order. Without explicit `order_by`,
  each group is positioned by its smallest `object_id`; explicit result ordering
  is stable on top of that canonical order.
- `having(expr_or_mapping)` may reference only group columns and named metric
  aliases. It uses registered portable predicates after either native or folded
  aggregation and before ordering/pagination.
- `select()` cannot be combined with `aggregate()` because aggregation already
  defines the complete result projection.
- `Query.count(field="object_id", distinct=True)` is not eager `len(execute())`
  and is not a post-result operation. It is a source-only terminal query:
  `where`, `near`, `traverse`, and `unsafe` may precede it; only `explain` and
  `execute` may follow it. Projection, grouping, aggregation, `having`,
  ordering, offset, and limit are rejected before count. Execution returns a
  one-cell identityless frame, and callers extract the value with `scalar()`.
- `FieldRef.count(...)` remains an aggregate metric for use inside
  `aggregate(...)`; it is independent of terminal `Query.count(...)`.

## Dynamic Json sources

A metric source is either a declared field or a dynamic payload location: a
dotted Json path (`meta.review.score`) or a column produced by the `explode`
clause. Declared sources prove reducer compatibility once, against the field's
logical type. Dynamic sources have no declared type, so they prove compatibility
per group instead, against the reducer's registered `aggregate-input-modes`:

- A missing key, a JSON null, or a path crossing a non-object yields `None` and
  is skipped by the reducer — never folded as zero. A group whose values are all
  absent returns the reducer's empty result (`None` for `sum`, `0` for `count`).
- A present value the reducer cannot fold raises. `Json` restores tagged
  payloads to rich Python values, so the guard tests what the reducer needs
  rather than a fixed JSON kind list: `numeric` reducers require real numbers —
  a `Decimal` folds, a Boolean is rejected rather than silently folded as 0/1 —
  and `order` reducers reject containers and require one comparable value family
  per group, so a payload `date` orders exactly as its declared twin does.
  Reducers declaring no input modes, or a mode this engine does not model, keep
  full ownership of value validation.
- A `result_type: source` reducer over a dynamic source resolves untyped, so
  `HAVING` compares its output as a raw operand. This is the same contract
  dotted group columns already had.

`explode` expands one Json object field into a `key`/`value` column pair, one
row per key, so an aggregate can group by keys discovered in the data rather
than keys named in the query. It is an aggregate input, not a projection: it
requires `aggregate` and excludes `select`, `near`, `traverse`, and terminal
`count`. Rows whose payload is missing, null, or not an object contribute no
pairs. Arrays are not iterated; array payloads are typed by `Array`, not by this
clause. The clause is proven against the schema during planning — the exploded
field must resolve to a declared Json field, dotted or not, and the derived
names must not shadow a declared column whose values the pair would replace —
so a wrong clause fails before any row is materialized.

Dynamic sources are portable-only. Any aggregate reading a dotted clause or an
explode clause selects the named fold route, so every backend answers
identically. Native lowering of `json_extract`/`json_each` is deliberately
deferred: `sum` and `avg` push down nowhere today, and SQL cannot reproduce the
raise-on-unfoldable-value contract above, so parity would be unprovable.

## Scope control

The core intentionally excludes arithmetic between aggregate outputs, majority,
rollups, window functions, and scalar aggregate subqueries. User-defined
reducers extend the registry but do not introduce a separate aggregate
expression language. Deterministic sampling is a separate query-stage decision
and is not part of this ADR.

Registered reducers remain the sanctioned extension seam when the vocabulary is
missing an operation: `hb.ext.register_aggregate_reducer` extends the Python and
JSON surfaces from one registration and costs a model one new `op` identifier of
context. Registration is a maintainer action, deliberately not reachable from a
read-only agent surface. Executing caller-authored code is a rejected
alternative — see `docs/resources/architecture/adr/0015-rejected-query-code-execution.md`.

Native aggregation is an optimization, never a second semantics. A provider may
participate only through a registered handler-layer aggregate compiler that emits
a provider-ready `QueryFragment`. Execute and explain use the same pre-execution
route decision. Unsupported shape selects the named fold route; provider IO or
programming failures after a native route is selected propagate and are never
silently swallowed.

Explain and execution may create or externally cache temporary build artifacts,
but those artifacts are never attached to the immutable query. Every
`execute()` recomputes against current workspace data and returns a fresh
detached frame; result persistence and result caching are never automatic.

## Native SQL policy

SQL native compilation is provider-owned metadata on `SQLDialectSpec`, not a
backend allowlist in the router. SQLite, DuckDB, PostgreSQL, MySQL, and SQL
Server currently register the parity-safe `count`, `min`, and `max` subset.
Generic SQL, OceanBase, Oracle, Trino, and StarRocks do not opt in until their
exact live contract is verified.
The compiler still declines an individual request before execution when:

- aggregate fields are not colocated as inline columns;
- required/default-bearing dependencies are not colocated on the candidate;
- the entity has more than one physical `object_id` route, so one native table
  cannot prove split-route identity integrity;
- a filter cannot compile natively on the same backend;
- `near` or graph traversal changes the input row set;
- grouped output would depend on `object_id` ordering without concrete-field
  parity proof;
- a text group/count-distinct lacks equality proof, or text `min`/`max` lacks
  ordering proof;
- a floating group or floating `min`/`max` lacks exact Python parity; or
- a distinct count uses floating or collation-sensitive values without proven
  equality parity; or
- a reducer is absent from the dialect's registered native operations.

`sum`, `avg`, `median`, `percentile`, and custom reducers remain portable-fold
operations. Database floating reduction
order can change rounding, while integer accumulator width and overflow differ
by dialect; neither is allowed to create a second result semantics merely for
pushdown. Text aggregation consumes the same provider-owned concrete-field
evidence as filtering. No built-in SQL provider currently supplies that proof,
so text group/count-distinct/order aggregate shapes fold.
SQL pre-aggregate predicates are lowered to total two-valued expressions so
nullable `eq`, `ne`, `in`, range, text, and negated filters match the portable
predicate registry rather than inheriting SQL three-valued logic. This total
null handling does not independently prove text comparison or collation parity;
text-stored fields still require the ADR 0010 concrete-field proof. A logical
filter tree containing any scan child declines native aggregation as a whole.
`HAVING` is deliberately a universal post-result filter today, even after native
SQL aggregation; native `HAVING` can be added later only as a parity-preserving
compiler optimization.

Compiler capability rows are generated from the live handler registry and
mirrored into MetaSchema. The SQL backend receives only a bound
`sql_aggregate` fragment containing a lazy SQLAlchemy statement builder,
output columns, casts, and empty-input projection; it never imports or parses
the aggregate request AST.

## Rejected alternatives

- A terminal `.aggregate()` returning a dict forks query return types and bypasses
  JSON, explain, MCP, ordering, and pagination composition.
- Tuple/string metric declarations and string-or-object JSON unions put the
  operation, field, and parameters in ambiguous shapes that survive too far into
  routing.
- `Backend.aggregate(query_ast)` reverses the handler-to-backend dependency and
  makes providers parse query-layer models.
- Static reducer allowlists make registered custom reducers invisible to JSON,
  MetaSchema, and generated capability documentation.
