# User Guide: Storage Placement and `explain()`

This guide shows how HeavenBase places fields on backends, groups query work by selected backend, and exposes those decisions with `query.explain()`.

---

## How Storage Placements Are Set

Every field on an Entity is bound to one or more backends through **storage placements**.

### Explicit storage

```python
import heavenbase as hb

class Product(hb.Entity):
    name = hb.field(hb.ShortText).store(to="main")
    embedding = hb.field(hb.Vector[256]).store(to="vec", strategy=hb.VectorIndex)
```

### Default storage

If you do not call `.store(...)`, HeavenBase picks a backend automatically:

| Logical type | Ordered claim preference | Default strategy |
|--------------|--------------------------|------------------|
| `Vector` | First Backend whose `storage-roles` contains `vector`. Current built-ins: `inmem`, `neo4j`, `surreal`, `pgvector`, `lance`, `milvus`, `milvus-lite`, `chroma`, `pinecone`, `qdrant`, `weaviate`, `redisearch`. | `VectorIndex` |
| `LongText` | First Backend with `search:true` and text storage (`elasticsearch`, `opensearch`, `redisearch`), then the first row store. | `InvertedIndex` on the search route; otherwise `InlineColumn` |
| `Json` | First Backend whose `storage-roles` contains `row`. | `JsonField` |
| `Array[ShortText]` | First SQL row store, then the first row store. | `SparseGramIndex` on SQL; otherwise `InlineColumn` |
| `Array`, `HyperG` | First row store that does not reject the logical type. | `SideTable` on SQL; otherwise `InlineColumn` |
| Everything else | First Backend whose `storage-roles` contains `row`. | `InlineColumn` |

Registry Backend-type order and then workspace instance order break ties inside
one claim preference. The complete current selector and alias inventory lives in
[Built-In Backend Providers](../reference/backends.md).

Write an explicit schema for every `HyperG`, including tag-shaped fields such
as `hb.HyperG["slot", "value", "topic"]`. A Backend that cannot preserve a
logical type as a queryable payload declares
`meta.tags.unsupported-storage-types`, and placement rejects it locally.
Current exclusions are HyperG on `lance`, `chroma`, `pinecone`, and `qdrant`;
JSON and HyperG on `weaviate`; and Array and HyperG on `clickhouse`. Other
routes still require an exact handler or the portable scan fallback.

When fields of one entity are stored on two SQL routes that open the same
database, both routes use the same main table. HeavenBase then writes each row
once, with the fields of every such route, through the first of those routes by
name. Each field is stored as its own route stores it: in that route's column or
side table (the `pgvector` route, for example, keeps `SideTable` fields in its
main table). HeavenBase recognizes the routes as one table when they share an
engine, open the same SQLite or DuckDB file, resolve the entity's table name to
the same table of one PostgreSQL database (whatever their roles and search
paths), or reach the same current database on one server through the `mysql`,
`dolt`, `seekdb` or `oceanbase` backend types (by a named lock). Routes of the
generic `sql` type on one SQLAlchemy dialect, and MySQL-family routes of a server
without the named-lock functions, are recognized by a table made for the check: when one
route's table holds the columns of another route, the route creates a table
`hb_probe_...`, the other route looks it up in its own default schema, and the
table is dropped again. Nothing is created for routes whose tables hold
different columns. HeavenBase decides this while it registers the entity, after
every route has created its tables and before the registration is published, so
the routes agree even when the first of them creates the database file or
tables. If a server cannot be asked, the registration fails; register
the entity again once it can. A server that refuses to create the table, such as
for a login without that right, gives no answer, and the routes are not
recognized; one that refuses to drop it keeps the table, and a `RuntimeWarning`
names it. PostgreSQL routes
that write one table but resolve different keyword index tables, through other
roles or search paths, raise `ValueError` at registration when either binds a
`SparseGramIndex` field of the entity. Give such routes separate databases on
any other SQL backend type (`oracle`, `mssql`, `starrocks`, `trino`, `clickhouse`): they are recognized at most through a shared engine, and routes
that are not recognized but share a table each replace the whole row with only their own fields.

Storage placements are resolved once when the entity is registered and stored in the workspace's **StoragePlan**.
Registered default storage profiles are also published into MetaSchema as
`storage_profile` rows during workspace construction. A storage profile owns
ordered `backend_preferences`, ordered `strategy_rules`, and one
`default_strategy`. Each preference/rule is a bounded conjunction of direct
tag predicates. The resolver evaluates preferences in authored order and
chooses the default Backend first only when that concrete instance matches the
current predicate; otherwise Registry Backend-type order and then workspace
instance order decide the candidate. These predicates nominate candidates and
do not prove a strategy/provider route. If no preference matches, the enclosing
storage resolver falls back to the workspace default Backend and uses
`default_strategy` without re-evaluating the strategy rules. Durable placement
evaluates static Backend-type claims only; it never changes because a live
`backend-availability` observation changes.

SQL provider metadata is published as `sql_dialect` rows for filter
pushdown/explain behavior and `database_dialect` rows for physical
JSON/BLOB/text/vector storage type selection, SQLAlchemy aliases, SQLGlot
parser translation, local file-database path behavior, URL/host policy,
retryable error messages, lifecycle namespace/quoting/force-drop/post-create
metadata, current/list database introspection SQL, reflection fallback SQL,
table rename DDL, and create/drop namespace support. The five physical
database behavior flags are canonical tag claims on those dialect records,
not a parallel dialect-capability mapping. Query the Registry Tag API for
those claims and the safe MetaSchema rows for ordinary dialect definition data
without importing implementations. Active workspace-owned `storage_profile`
rows (`source == "workspace"` or `meta.source == "workspace"`) override
default placement for entities registered after the row is written; existing
storage plans are not rewritten automatically.

Query and row APIs only consume that registered state. They resolve an Entity
class, identifier, or instance to the canonical class and existing
`StoragePlan`; they never register a schema or publish `MetaSchema` rows as a
read or CRUD side effect.

### Multi-placement fields

A field can have more than one storage placement. Writes are duplicated to every backend named in the field's effective bindings; duplicated storage is not an exclusive storage choice. Query-time grouping is still conservative: the current router uses the first effective binding for a filter or near clause, then groups clauses by those backend copies. Future router work will reason over all duplicate bindings and may prune redundant backend groups when another group fully contains the same filters.

### Storage mental model

For a normal query, you can usually predict backend selection before calling `explain()` with four rules:

1. Look at the Entity field storage after registration. If a field has one storage binding, that backend owns ordinary filters on that field.
2. If a field has multiple bindings, ordinary filters use the first effective binding. The later bindings are physical field copies, not hidden provider mirrors.
3. Same-level `AND` and `OR` clauses are order-insensitively grouped by the selected backend. For example, `A_sql & B_search & C_sql` still sends `A_sql & C_sql` to SQL as one backend group.
4. `where + near` starts from the vector field's backend. It can use `metadata_prefilter` only when every filter field has an explicit duplicated storage binding on that vector backend and every copied filter compiles natively there; otherwise it may use candidate IDs or post-filter merging.

`explain()` is how you confirm the prediction. Check `field_storage` to see every physical copy, `selected` to see the default storage binding, `backend_group` to see order-insensitive grouping, and `near_filter_mode` to see how filters were combined with vector search.

### Router boundary

`src/heavenbase/workspace/query/router.py` is the query execution coordinator. It applies route services, executes backend fragments, merges frames, hydrates rows, and applies ordering, limits, and projection. `query/filter.py` owns scalar storage-binding selection, boolean backend grouping, scalar fragment compilation, and scalar filter execution. `query/aggregate.py` validates aggregate/HAVING requests, selects a registered parity-safe native compiler or the exact portable fold, executes the fold over complete identity-ordered rows, and reports that decision to explain. `query/explain.py` owns final `explain()` assembly, including scalar filter steps, backend groups, near steps, aggregate mode/reason, and duplicated-storage pruning markers. It uses the same filter, near, and aggregate route services so explain output stays tied to execution behavior. `query/near.py` owns both near query-value normalization and near/filter route selection, including metadata/candidate prefilter execution; the selector is shared by `execute()` and `explain()` so the reported `near_filter_mode` tracks the execution path. Provider behavior stays in exact handlers, concrete Backend resources, and Backend tag declarations/overlays. Future routing improvements must preserve the public contracts: storage placements are explicit, writes fan out to all effective bindings, Backends execute `QueryFragment`s, and `explain()` remains the source of truth for Backend selection decisions.

---

## Query and Result Lifecycle

The public `Query` is immutable, spec-only data. Every fluent method returns a
derived query; no plan, fragment, or result is stored on either value.
`explain()` and `execute()` may build or externally cache temporary routing
artifacts. `execute()` nevertheless evaluates current workspace data on every
call, never automatically persists or reuses a result, and returns a newly
detached `ResultFrame`.

Application code should explicitly export that transport frame with `rows()`,
`scalar()`, `to_pandas()`, `to_pyarrow()`, `to_numpy()`, `to_pydantic()`, or
another supported exit. It should not print, implicitly coerce, or treat the
frame as a post-execution query. The complete contract is
[query values and result exports](query-values-and-results.md).

---

## How the Router Decides

When application code runs and explicitly exports a query:

```python
rows = ws.query(Product).where(Product.name == "Desk").execute().rows()
```

HeavenBase builds a **QuerySpec** and then, for an ordinary entity query:

1. **Backend grouping** — boolean `AND` / `OR` trees are flattened per same operator level and grouped by backend. Clauses that touch the same backend are sent there together even when clauses for other backends appear between them.
2. **Handler selection** — for each clause, the router asks the **HandlerRegistry** for the best compiler: `(dtype, op, type, strategy)`.
3. **Fragment compilation** — the handler returns a `QueryFragment` with a payload (SQLAlchemy expression, Lance where-string, vector search dict, etc.) and metadata.
4. **Execution** — each backend executes its fragment and returns an identity-bearing `ResultFrame`.
5. **Merging** — routed entity frames from different backends are merged by `object_id`. Columns from all backends are preserved.

The public result of `execute()` is a detached `ResultFrame` after backend execution,
cross-backend merge, optional hydration, ordering, offset, limit, and projection.
Its identity and composition helpers support execution internals, but the recommended
application boundary is an explicit export. Pre-merge and mid-merge frames are
internal execution details; use `explain()` when you need to understand backend
grouping, fallback, or vector prefilter choices.

An aggregate query takes a distinct terminal route. `workspace/query/aggregate.py` first
validates grouping, reducers, and typed `HAVING`, then asks the registered
backend aggregate compiler for a parity-safe native fragment. If placement,
identity, filter, collation, or reducer evidence is insufficient, it names the
reason and folds the complete candidate rows with the portable reducer oracle.
Both paths return an identityless `ResultFrame`: its columns are grouping keys
and named reducer outputs, so `object_id` and `ids` are deliberately absent.
Ordering, offset, limit, and post-aggregate `HAVING` apply to that result frame.

`Query.count(field="object_id", distinct=True)` is a separate source-only
terminal transform. Only `where`, `near`, `traverse`, and `unsafe` may precede
it; only `explain` and `execute` may follow it. It does not count a projected,
grouped, ordered, offset, or limited result. Execution returns a one-cell
identityless frame, and user code obtains the integer through `scalar()`.

When a query has both `.where(...)` and `.near(...)`, HeavenBase first checks whether the whole filter tree and the vector field use the same backend. If they do, it keeps `near` as the operation and attaches the grouped filter fragment to the near payload/notes. The vector backend then decides whether to use provider-native pre-filtering or backend-local filtering.

If the filters and vector field use different backends, HeavenBase checks two
bounded pushdown options before falling back to a plain intersect. A filter
field with an explicit duplicated storage placement on the vector backend can
be used as vector metadata when every filter clause has such a backend copy
and compiles natively there. Otherwise, a Backend whose static
`near-filter-modes` tag contains `candidate_id_prefilter` can receive a bounded
`object_id IN (...)` filter derived from the authoritative filter frame. The
tag nominates that attempt; the exact compiler and concrete execution still
decide. If neither path applies, HeavenBase executes filter and near frames
separately and intersects them by `object_id`.

### Cross-backend example

```python
ws.query(Product).where(
    (Product.name == "Desk")           # stored on "text" backend
    & (Product.price > 100)            # stored on "nums" backend
    & (Product.stock >= 0)             # stored on "nums" backend
).execute().rows()
```

`explain()` shows two backend groups:

```json
{"node": "backend_group", "op": "and", "backend": "nums", "n": 2}
{"node": "backend_group", "op": "and", "backend": "text", "n": 1}
```

---

## Reading `explain()` Output

Call `explain()` on any query before (or instead of) `execute()`:

```python
explain = ws.query(Product).where(Product.price > 100).explain()
```

### Top-level structure

```json
{
  "workspace": "default",
  "query": { "entity_id": "product", "filter": {...}, ... },
  "storage_plan": {
    "entity_id": "product",
    "bindings": [
      {"entity_id": "product", "field_name": "price", "backend": "sql", "strategy": "inline", "source": "default"}
    ]
  },
  "steps": [
    {
      "node": "filter",
      "field": "price",
      "op": "gt",
      "handler_op": "gt",
      "logical_type": "Float",
      "backend": "sql",
      "type": "sqlite",
      "strategy": "inline",
      "handler": "Float.gt.sqlite.inline",
      "handler_kind": "sql_where",
      "handler_mode": "native",
      "fallback_reason": null,
      "unsupported_reason": null
    }
  ]
}
```

### Step fields

| Field | Meaning |
|-------|---------|
| `node` | `filter`, `near`, `and`, `or`, `not`, `backend_group`, `aggregate`, `json_path` |
| `field` | Entity field being filtered |
| `op` / `handler_op` | Logical operation (`eq`, `gt`, `match`, `near`, ...) |
| `logical_type` | HeavenBase logical type (`Float`, `ShortText`, `Vector`, ...) |
| `backend` | Named backend instance (`sql`, `vec`, `search`) |
| `type` | Provider family (`sqlite`, `postgres`, `lance`, `elasticsearch`, ...) |
| `strategy` | Storage strategy (`InlineColumn`, `VectorIndex`, ...) |
| `field_storage` | All effective storage placements for the filtered field, with `selected` marking the current default binding |
| `handler` | Fully qualified handler key |
| `handler_kind` | Fragment category (`sql_where`, `lance_search`, `sql_sparse_gram_contained_in`, `row_payload`, ...) |
| `handler_mode` | `native` (provider pushdown), `py_scan` (Python scan fallback), `native_with_near` |
| `strategy_adapter` | Backend-hosted strategy adapter used by the fragment, such as `sql_sparse_gram`, when applicable |
| `near_filter_mode` | Near/filter combination mode on `near` steps: `combined_native`, `combined_scan`, `metadata_prefilter`, `candidate_id_prefilter`, `post_filter`, `exact_scan`, `unsupported`, or `none` |
| `pruned` / `pruned_by` | Present when duplicated storage work was covered by another backend group, such as a vector metadata prefilter |
| `fallback_reason` | Human-readable reason when `handler_mode == "py_scan"` |
| `unsupported_reason` | Human-readable reason when the combination is not supported |
| `portable_proof` | `true` when the selected native text operation/key has concrete physical parity proof; `false` when unsafe execution selected it without proof; null/not present when not applicable |
| `unsafe` | Whether this frozen query authorized documented result-relaxing execution optimizations |
| `unsafe_reasons` | Present on a step when unsafe authorization was actually consumed; currently includes `text_semantics_not_proven` for unproved native SQL text execution |

Dotted Json path columns (`meta.venue`) in `group_by`, `order`, `select`, or an aggregate metric `field` always take the portable Python route: an aggregate reading any dotted clause reports `handler_mode: "py_fold"` with `fallback_reason: "json_path_clause_present"` on its `aggregate` step, and a plain query with dotted `order`/`select` emits a `json_path` step with `handler_mode: "py_scan"` naming the derived columns. Rows with a missing or null path keep a `None` value — they group under the `None` key, sort last in both directions, and are skipped by reducers rather than folded as zero. A payload value the reducer cannot fold raises instead: dotted metric sources carry no declared logical type, so each group proves its values against the reducer's registered input modes before folding. Declared-column queries remain eligible for their normal routes, including the independent text-proof gate. Json `get` filters over dynamic string operands likewise report `text_semantics_not_proven` until the provider extraction expression has its own proof; proved non-text scalar paths may still compile natively.

The `explode` clause is the same portable route with row multiplicity: it expands one Json object field into a `key`/`value` column pair before the fold, reports `fallback_reason: "explode_clause_present"`, and echoes its clause under an `explode` key on the `aggregate` step. Exploded columns are dynamic like dotted paths — untyped in `HAVING`, guarded per group by the reducer's input modes. Rows whose payload is missing, null, or not an object contribute no pairs, so a fully non-object input folds to the ungrouped empty-set contract rather than a synthetic key.

---

## Trust Checklist

Before trusting query results, especially in agentic or automated pipelines:

1. **Check `handler_mode`**
   - `native` → the provider is doing the work. Fast and scalable.
   - `py_scan` → HeavenBase is scanning rows in Python. Correct, but may be slow on large datasets.
   - `native_with_near` → native when combined with a `near` clause; may scan otherwise.

2. **Read `fallback_reason`**
   - If `handler_mode` is `py_scan`, `fallback_reason` tells you exactly why.
   - `text_semantics_not_proven` means a SQL compiler may exist, but the concrete physical field has no proof that it matches the portable Python text operation.
   - If no handler author provided a specific reason, the workspace
     HandlerRegistry's resolver-bound reason catalog derives a readable default such
     as `"regex on ShortText (InlineColumn) is not natively supported by sqlite;
     using Python scan fallback."`.
   - If the reason is acceptable for your data size, the operator result still follows the portable predicate, assuming the backend has losslessly round-tripped canonical field values and identities. Scan cannot reconstruct text a provider has already changed.
   - Set `heavenbase.query.warn_on_python_fallback: true` to emit a runtime `UserWarning` whenever execution actually uses the Python scan fallback.

   For a query-scoped efficiency tradeoff, append `.unsafe()` or set
   `"unsafe": true` in `query_json`. This frozen execution policy currently
   bypasses portable text proof and static collation/case guards; it does not
   bypass type validation, compiler support, parameterization, security, or
   resource guards. Provider collation, padding, pattern, regex, grouping, and
   bounded-candidate behavior may change the result. Check `unsafe_reasons` to
   see whether authorization was consumed and `portable_proof` before treating
   a native text result as backend-portable.

3. **Check `unsupported_reason`**
   - If present, the query **cannot execute**. `execute()` will raise `LookupError`.
   - Example: `"Float fields do not support regex operations."`
   - Fix: change the filter to a supported operation.

4. **Inspect `backend_group` steps**
   - Confirm that clauses are grouped as expected.
  - Unexpected single-clause groups may indicate a storage placement misconfiguration.

5. **Inspect `near_filter_mode` on vector queries**
  - `combined_native` means the backend received one `near` fragment with attached filter context and can push both parts down natively.
  - `combined_scan` means HeavenBase still uses one `near` execution against that backend, but at least one part falls back to backend-local row predicates.
  - `metadata_prefilter` means all filter fields have explicit duplicated storage placements on the vector backend and compile as vector metadata filters.
  - `candidate_id_prefilter` means HeavenBase first computed a bounded authoritative filter ID set and passed those IDs into vector search.
  - `post_filter` means filters and vector search are on different backends and are merged after execution.
  - `exact_scan` means a filtered `near(..., exact=True)` query uses exhaustive scoring after applying its complete filter. An unfiltered portable exact scan reports `near_filter_mode: "none"`, `handler_mode: "py_scan"`, and `exact_guaranteed: true`.
  - `unsupported` means at least one filter clause cannot compile on its authoritative storage binding, with or without `exact=True`; `execute()` raises `LookupError`, so inspect `unsupported_reason` on the filter step.

6. **Check duplicated storage pruning**
  - A filter step with `pruned: true` was not executed on its default backend because another backend group covered the same predicate over a duplicated field copy.
  - A `pruned_backend_group` step explains which backend was skipped and which backend covered it.

---

## Troubleshooting

### "My query is slow"

Run `explain()` and look for `py_scan` steps.

```python
explain = ws.query(Product).where(Product.name.regex(r"^A")).explain()
for step in explain["steps"]:
    if step.get("handler_mode") == "py_scan":
        print(f"Slow step: {step['field']}.{step['op']} — {step['fallback_reason']}")
```

**Fix options:**
- Prefer an operation whose concrete field has proved native semantics; SQL pattern and regex compiler availability alone is not such proof.
- Place the field on a backend that supports the operation natively.
- Add an index or change the strategy.
- For vector queries, co-locate scalar metadata with the vector backend when you want `near_filter_mode == "combined_native"`.
- For mixed SQL + vector queries, explicitly store scalar fields on the vector backend as additional field copies when you want `near_filter_mode == "metadata_prefilter"`; otherwise supported vector backends may use `candidate_id_prefilter` up to the configured candidate-ID limit.

### "My query raises LookupError"

```python
explain = ws.query(Product).where(Product.price.regex(r"^1")).explain()
print(explain["steps"][0]["unsupported_reason"])
# → "Float fields do not support regex operations."
```

**Fix:** Use a supported operation for the field's logical type.

### "Results are missing columns"

Cross-backend merges preserve all columns by default. If you use `.select(...)`, only requested columns + `object_id` are kept.

```python
# All columns from all backends
rows = ws.query(Product).where(...).execute().rows()

# Only name and price
rows = ws.query(Product).where(...).select("name", "price").execute().rows()

# Include reserved query metadata explicitly
rows = ws.query(hb.Catalog).where(...).select("target_id", "match").execute().rows()
```

---

## Example: Reverse Keyword Containment

`contained_in` is the inverse of ordinary text `contains`: it returns rows whose stored short keyword appears inside a longer query text. By default, `Array[ShortText]` fields use `SparseGramIndex` on the SQL `main` backend when SQL is available, and the SQL backend delegates postings lookup to its `sql_sparse_gram` strategy adapter.

```python
rows = (
    ws.query(hb.Catalog)
    .where(hb.Catalog.keywords.contained_in("The user mentioned Redis Query Engine"))
    .select("target_id", "match")
    .execute()
    .rows()
)
```

An abbreviated `explain()` step should show:

```json
{
  "node": "filter",
  "field": "keywords",
  "op": "contained_in",
  "logical_type": "array",
  "backend": "main",
  "type": "sqlite",
  "strategy": "sparse-gram",
  "handler_kind": "sql_sparse_gram_contained_in",
  "strategy_adapter": "sql_sparse_gram",
  "handler_mode": "native"
}
```

If no SQL backend is configured, `Array[ShortText]` falls back to row-store `InlineColumn`, and `contained_in` runs through Python scan fallback with the same `match` metadata shape. SQL `SideTable` remains available explicitly for ordinary array membership workloads.

---

## Example: Annotated Explain Output

```python
class Doc(hb.Entity):
    identifier = "doc"
    title = hb.field(hb.ShortText).store(to="search")
    body = hb.field(hb.LongText).store(to="search")
    emb = hb.field(hb.Vector[2]).store(to="vec", strategy=hb.VectorIndex)
    score = hb.field(hb.Float)

ws.register(Doc)
ws.upsert(Doc, {"object_id": "d1", "title": "alpha", "body": "beta", "emb": [1, 0], "score": 9})

query = (
    ws.query(Doc)
    .where((Doc.title.match("al") | Doc.body.contains("be")) & Doc.score > 5)
    .near(Doc.emb, [1, 0], top_k=3)
)

import json
print(json.dumps(query.explain(), indent=2))
```

**Sample output (abbreviated):**

```json
{
  "workspace": "default",
  "query": { "entity_id": "doc", "filter": {...}, "near": {...} },
  "storage_plan": { "entity_id": "doc", "bindings": [...] },
  "steps": [
    { "node": "and", "n": 3 },
    { "node": "backend_group", "op": "and", "backend": "search", "n": 2 },
    {
      "node": "filter",
      "field": "title",
      "op": "match",
      "handler_op": "match",
      "logical_type": "ShortText",
      "backend": "search",
      "type": "elasticsearch",
      "strategy": "inline",
      "handler": "ShortText.match.elasticsearch.inline",
      "handler_kind": "es_query",
      "handler_mode": "native",
      "fallback_reason": null,
      "unsupported_reason": null
    },
    {
      "node": "filter",
      "field": "body",
      "op": "contains",
      "handler_op": "contains",
      "logical_type": "LongText",
      "backend": "search",
      "type": "elasticsearch",
      "strategy": "inline",
      "handler": "LongText.contains.elasticsearch.inline",
      "handler_kind": "es_query",
      "handler_mode": "native",
      "fallback_reason": null,
      "unsupported_reason": null
    },
    {
      "node": "filter",
      "field": "score",
      "op": "gt",
      "handler_op": "gt",
      "logical_type": "Float",
      "backend": "main",
      "type": "sqlite",
      "strategy": "inline",
      "handler": "Float.gt.sqlite.inline",
      "handler_kind": "sql_where",
      "handler_mode": "native",
      "fallback_reason": null,
      "unsupported_reason": null
    },
    {
      "node": "near",
      "field": "emb",
      "op": "near",
      "handler_op": "near",
      "logical_type": "Vector",
      "backend": "vec",
      "type": "inmem",
      "strategy": "vector",
      "handler": "Vector.near.inmem.vector",
      "handler_kind": "row_payload",
      "handler_mode": "py_scan",
      "fallback_reason": "In-memory backend always uses Python scan fallback.",
      "unsupported_reason": null,
      "metric": "cosine",
      "top_k": 3
    }
  ]
}
```

**Interpretation:**
- `title.match` and `body.contains` run natively on Elasticsearch.
- `score.gt` runs natively on SQLite.
- `emb.near` falls back to Python scan because the backend is `inmem`. A
  Backend with an exact native near handler—such as `pgvector`, `lance`,
  `milvus`, `milvus-lite`, `chroma`, `pinecone`, `qdrant`, `weaviate`, or
  `redisearch`—can report `native` for its supported metric and route.

---

## See Also

- [Backend strategy handlers](backend-strategy-handlers.md) - how to add new backends and handlers.
- [Capabilities matrix](../reports/capabilities.md) - full matrix of which (type, op, backend, strategy) combinations are native vs fallback.
