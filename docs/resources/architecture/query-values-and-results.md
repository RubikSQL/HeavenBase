# Query Values and Result Exports

Status: current for the `0.1.2.3` train.

This page is the canonical lifecycle contract for the Python query surface.
Routing details remain in [routing and explain](routing-and-explain.md), and
available result conversions remain in [interop](interop.md).

## One Query Model

`ws.query(Entity)` returns an immutable `Query`. A query contains declarative
request data only: its entity, clauses, projection or aggregate request, and
execution policy. It never contains a builder, execution plan, backend
fragment, cached result, or persistence state.

Every fluent transform returns another `Query` and leaves its source unchanged:

```python
base = ws.query(User).where(User.status == "active")

names = base.select("name")
total = base.count()
```

`names` and `total` are independent values derived from `base`; callers never
need `copy()` or `fork()`. Ignoring a transform's return value also leaves the
query unchanged.

`QuerySpec` is the frozen normalized request passed into workspace routing. It
is a detached representation of the same caller intent, not an execution plan.
Backend-ready plans and `QueryFragment` values may be produced temporarily by
`explain()` or `execute()`. An implementation may cache those build artifacts
outside the `Query`, provided the cache is keyed by all relevant runtime state
and does not change query or result semantics.

## Explain and Execute

`query.explain()` inspects the route without executing the query. It may compile
temporary route artifacts, but stores none of them on the `Query`.

`query.execute()` evaluates ordinary queries against current workspace data. An
explicit near `cache_version` may reuse a declared candidate generation, as
specified below. Execution always recalculates filtering, distances and Top-K. It
does not reuse, attach, or automatically persist a previous result. Calling it
again recomputes the result and returns a newly detached `ResultFrame`.

The frame is an execution transport container, not a continuation of the query.
Query transforms cannot be appended after execution, and frame operations do
not modify or specialize the source query. A future query-from-frame feature
would be an explicit query source/non-terminal operator, not hidden
post-execution mutation.

## Result Export Boundary

Application code should export a `ResultFrame` immediately into the
representation it intends to use. Do not print a frame, rely on implicit
coercion, or treat frame transformation helpers as the ordinary application
data API.

Use an explicit exit. The following lines are alternatives; each call to
`execute()` recomputes:

```python
rows = query.execute().rows()
table = query.execute().to_pyarrow()
dataframe = query.execute().to_pandas()
models = query.execute().to_pydantic(UserModel)
value = scalar_query.execute().scalar()
```

The current exports include row dictionaries, Pandas, PyArrow, NumPy, Pydantic,
and Daft. Additional explicit exits may provide Entity lists, row-value lists,
or other ecosystem-native values without expanding the frame into a second
query language. Internal routing may still use frame identity and composition
operations to merge backend results before returning the detached final
container.

## Result Columns

A row query declares its result columns: the identity, every entity field, the
result metadata its executed filters declare (such as the `match` column of
`contained_in`), and the `score` of a near clause. Ordering and projection read
these declarations, not the keys that matched rows happened to store:

- An empty result carries every declared column, so it orders, pages, projects,
  and exports like a populated result. `select` narrows any result to the
  identity and the selected columns.
- A declared field that no matched row stored reads as null, so a populated
  result carries the same columns, in the same order, as an empty one on every
  backend, including the in-memory, file, and document backends that store
  only the keys a row has. The order is the identity, the other entity fields
  sorted by name, the declared filter metadata sorted by name, and then
  `score`. A dotted Json path added only to order rows does not widen the
  returned rows.
- Any other name fails with an error that names it, whether or not rows
  matched, unless it is a dotted path into a declared Json field.

Aggregate results always carry their group and metric columns. At the frame
level, `ResultFrame.sort` and `ResultFrame.project` accept only columns that the
frame holds, and an empty frame holds the columns it was built with.

## Source-Only Terminal Count

`Query.count(field="object_id", distinct=True)` is a terminal query transform.
It returns an executable `Query`; it never executes eagerly and never returns an
integer directly.

Only source-shaping clauses may precede it:

- `where(...)`
- `near(...)`
- `traverse(...)`
- `unsafe()`

Projection, grouping, aggregation, result filtering, ordering, offset, and
limit cannot precede `count()`. After `count()`, only `explain()` and
`execute()` are valid. The explicit scalar exit is:

```python
total = (
    ws.query(User)
    .where(User.status == "active")
    .count()
    .execute()
    .scalar()
)
```

This count describes the source row set selected by `where`, `near`, and
`traverse`; it does not mean “count this already projected, grouped, ordered, or
paginated result.” The default unique, non-null `object_id` field therefore
counts source rows; another field counts its non-null values with the requested
distinct policy. For an executed container, export first and use the target
representation's own operations.

`FieldRef.count(...)` inside `aggregate(...)` remains an aggregate metric and is
distinct from terminal `Query.count(...)`.

## Exact Nearest Neighbors

Declare exhaustive metric Top-K in the existing query, not a separate pool API:

```python
query = (ws.query(Document).where({"tenant": "demo"})
         .near("embedding", query_vector, top_k=8, exact=True))
route = query.explain()
hits = query.select("title", "score").execute().rows()
```

The JSON `near` object also accepts `"exact": true`. Omission or `False` preserves
the existing provider-default path, which may be approximate. `exact` must be a
boolean. It survives query transformations, policy binding, and query-compute.

`HandlerContext.exact` carries the requirement to existing registered handlers.
A native fragment sets `notes["exact"] = True` only when its executor guarantees
exhaustive metric Top-K. Unknown or approximate handlers use HB's existing portable
scorer and scan executor. An unavailable scan or unsupported filter fails; HB
does not silently search only an ANN shortlist and call reranking exact.

Complete filters apply before Top-K. A proven native near/filter pair is pushed
down together. Other cases, including cross-backend filters, evaluate the complete
logical row stream and score all admitted vectors. Candidate-prefilter size caps do
not truncate exact requests. `explain()` reports `exact_required`,
`exact_guaranteed`, `handler_mode`, `near_filter_mode`, and a fallback reason.
For an unsupported filter it reports `near_filter_mode: "unsupported"` and the
filter step's `unsupported_reason`, where `execute()` raises `LookupError`.
Its native guarantee is a provider contract; live validation occurs at execution.

Milvus Server supports native exact search when its explicit backend
`index_type` is `FLAT` and the requested metric matches the collection. The
default is `AUTOINDEX`. Milvus Lite uses a binary64 scan for every exact query
and for ordinary cosine queries because native float32 scores can overflow or
underflow. Native exact execution on Milvus Server rechecks the live index
type and metric. An incompatible or missing index fails closed; no automatic
index replacement occurs. This follows the provider's
[exhaustive FLAT contract](https://milvus.io/docs/index.md).

Exact means exhaustive ranking under the selected implementation's numeric
contract, not bit-identical scores across engines, deterministic membership of
native equal-score boundary ties, or semantic relevance. Portable scoring follows
HB's binary64 cosine, negative Euclidean distance, and inner product semantics.
The existing scan executor scores admitted rows in configurable batches (256 by default);
NumPy, when installed, accelerates those batches. Without it the dependency-free
scalar functions execute. Row-wise reductions preserve identical-vector ties
independently of batch position; last-bit differences from scalar sums are permitted. Missing vectors are excluded; portable zero-vector cosine scores are
zero. Nonfinite query coordinates and nonfinite portable scores are rejected.
Portable ties use string object IDs. Native distance scales and zero-vector
conventions remain provider-specific (for example, Milvus L2 is squared).
A near ranked by the portable scorer reports the portable scale even on a backend
whose native route reports another (Chroma L2, for example, reports one minus the
squared distance natively and the negative distance through the scan). On a backend
with native vector search, a filter that sends a near through the scan can therefore
change its score values, but it ranks the same rows by the same metric. Native search
never returns a row without a vector, and the scan scorer of such a backend leaves
that row out under every metric too; only an approximate native index (Chroma's HNSW,
for example) can miss a neighbour that the exhaustive scan finds. A backend without
native vector search scans every near: there a row without a vector scores `0.0`
under cosine and is left out under the other metrics, and an exact near leaves it out
on every backend. Chroma, Pinecone and RediSearch rank natively only by the metric
their collection or index was built with, which HB reads from the collection or
index itself: changing the backend's `metric` setting does not change an existing
one. On Chroma and RediSearch a near requesting
another metric ranks through the portable scorer; Pinecone, which has no row scan,
rejects it with `ValueError`.

Chroma stores one field as the collection embedding and stores additional vector
fields as metadata. A near on an additional vector field uses the portable
scorer with that field's stored values, including when the query has a filter.
Reordering the fields during registration preserves the embedding field. New
collections created by HeavenBase record its name, so a fresh backend can
recover it and requires that the schema still route that field to Chroma. Open
an older unlabeled collection with its original field order before reordering
the fields on that backend.

0.1.2.2 took the first Vector field of the whole schema as the embedding. When another
backend stored that field, it sent no embeddings, and Chroma computed 384-wide ones from
the documents. HeavenBase recognizes such an unlabeled collection by that width, when the
first Vector field stored on Chroma declares another dimension. Writes and patches then
work as in 0.1.2.2: no field is the collection embedding, and every Vector field stored on
Chroma is kept as metadata. 0.1.2.2 did not store those vectors, so a row written by it
has none until it is upserted with its vectors, and a near on the field ranks the stored
ones with the portable scorer. 0.1.2.2 ranked the computed embeddings for a 384-wide
query, which a field takes when its dimension is 384 or not declared; any other near
raised.

A first Vector field on Chroma of dimension 384, or one that declares no dimension, can
hold 384-wide vectors that the caller wrote, so its collection is not recognized. Its rows
read the computed embeddings as the field's vectors until they are upserted with their
vectors. Without a declared dimension, an upsert of vectors of another width fails with
Chroma's dimension error, where 0.1.2.2 wrote the row without its vector; declare the
field's dimension to have the collection recognized. To use Chroma's native search again,
delete the collection, open the workspace in a new process, register the entity and upsert
its complete rows. A process that registered the entity before the deletion recreates the
collection unlabeled and, when it was recognized, with placeholder embeddings that a later
process cannot use.

No result cache or cross-backend transactional snapshot is added. Callers must
coordinate concurrent data and index changes when requiring a stable candidate set.
Portable exact execution reads each route of the entity in bounded batches,
hydrates and checks complete logical rows per batch, and uses a bounded Top-K heap.
Cross-route duplicate identities are usually eliminated through batched existence
lookups. If the one streamed route inherits `Backend.get_many`, exact execution
streams it first and retains its IDs for duplicate checks on later routes. Those
routes can then skip lookups that would re-run a full scan. A route with a native keyset
`Backend.scan_page` is read one page query at a time: SQL backends (keyset
pagination), the in-memory and file backends, and Milvus (a Strong-consistency
query iterator, which a Milvus server returns in primary-key order; Milvus Lite
answers in storage order, so it lists and sorts the matching keys once per
enumeration and fetches each page's rows by key). A scan closed before its last
page calls `Backend.end_scan`, which drops the Milvus Lite key listing. A route without a native page, such as Chroma, is
read in one pass of `Backend.iter_scan(entity_id, batch_size=...)` (Chroma has
no key cursor and its offset pages slow down with the offset, so it lists its
ids once and fetches rows by id, which keeps the pass linear); the query
holds the workspace read lock for its whole run, so no writer runs between
batches. When two routes of one entity both lack a native page, neither is read
in one pass, because both could hold their provider locks while looking rows up
in each other: both use the portable `scan_page` default, which enumerates the
route once per page (O(N²/batch size)). A provider that implements only the
older materializing `scan()` (TinyDB, MongoDB, Redis, Neo4j, SurrealDB,
Elasticsearch, OpenSearch, Qdrant, Weaviate, RediSearch, Lance, and any backend
written for 0.1.2.2) inherits a
`Backend.iter_scan` default that yields the rows of one `scan()` call: the exact
scan still returns its exhaustive Top-K, but holds that route's rows in memory
once per enumeration. A subclass of a backend with a native page (for example
an `InMemBackend` or `SQLiteBackend` subclass) that overrides `scan` or
`iter_scan` gets the portable default in place of each inherited native layer
above its override (`iter_scan` and `scan_page` above `scan`, `scan_page` above
`iter_scan`), so its override (hiding soft-deleted rows, or hydrating rows
lazily) decides what the exact scan reads. The override may itself read the
parent's enumeration: `super().scan()` and `super().iter_scan()` read the
inherited layers on any thread, and `self.iter_scan()` or `self.scan_page()`
called from a `scan` override, on the thread running it, answers with the
inherited native layer, so the override does not recurse. A worker thread the
override starts must use `super()`, because there `self.iter_scan()` reaches the
portable layer and runs the override again. The portable layers take no backend
lock of their own, so an override may hand reads of the same backend to worker
threads. Such a subclass defines those layers itself to keep a native page. A provider with neither `iter_scan` nor a working `scan`
(Pinecone, which has no row scan API) fails explicitly on this path.

An exact scan applies the complete filter as a true/false predicate over
logical rows, so it does not produce filter metadata columns: after a
`contained_in` filter, `select("match")` on an exact query raises
`Unsupported result column`. Read `match` from the same filter in a query
without `near`.

Working memory is O(batch size × row width + K × row width), excluding the
provider's own storage/index buffers and any explicitly retained cache. A streamed
route with scan-based lookups also needs O(number of IDs on that route) memory. One
oversized row still requires memory proportional to that row. The selected result
can be projected before frame copies; logical schema checks run before projection.
Exact arithmetic remains O(rows × dimensions). Ordinary queries deliberately
reread data, including updates that leave row counts unchanged.

## Explicit Versioned Candidate Reuse

`cache_version` is experimental: its spelling, capacity defaults and
invalidation rules may change in a later release without a deprecation period.

For immutable knowledge generations, opt in on the existing near clause:

```python
query = (ws.query(Document).where({"tenant": "demo"})
         .near("embedding", query_vector, top_k=8, exact=True,
               cache_version="knowledge-2026-09-05-v1"))
hits = query.select("title", "score").execute().rows()
# After an external write, before reusing the same version:
ws.refresh(entity=Document)
```

Python and JSON accept the same non-empty string `cache_version`; it requires
`exact=True` and survives immutable composition and policy binding. No new pool
class, result cache, or application scorer exists. This token declares reusable
source data; **it does not select historical data or create a database snapshot**.
Native proven-exact routes bypass candidate caching and read the provider.

Milvus native searches explicitly request Strong consistency, as its scans and
point lookups already do. This applies to both provider-default and FLAT exact
searches; index exactness and write visibility are separate contracts.

The cache belongs to one live workspace instance. One complete generation per
entity is retained, independently of query vector, filter and Top-K; every query
reapplies its complete filter. Optional NumPy binary64 coordinates and magnitudes
are reused under the same capacity budget. Public results remain copy-isolated.
A new version evicts the previous entity generation. Row writes (including failed
partial writes), schema replacement, explicit refresh, release and drop invalidate
owned entries. Builds publish only after complete enumeration; source errors,
cancellation and capacity overflow never publish a partial entry. Two internal
maintenance writes to the Catalog and MetaSchema system entities do not
invalidate entries; those entities have no Vector field, so no exact near reads
them.

Default bounds are `heavenbase.query.near.cache.max_bytes: 67108864` and
`max_entries: 16`. The byte budget conservatively accounts retained Python row
payloads and prepared matrices; it is not a whole-process RSS cap. Zero disables
reuse. LRU eviction releases complete generations. An oversized candidate set
continues as a bounded stream without retaining the set. The source must be
unchanged during enumeration; this cache does not add distributed leases.

Writes through another workspace/process, raw backend calls or direct database
clients are external writes. Callers must change the version or call `refresh()`
after them. HB cannot infer those writes from an unchanged caller token and does
not use row counts or TTL as freshness proofs. Omit the token whenever that
ownership contract cannot be guaranteed. `explain()` distinguishes live reads from
explicit versioned candidate reuse and reports the configured `max_bytes` and
`max_entries`. On an exact scan route, a zero bound disables candidate reuse
and the scan reads live. Proven-exact native routes continue to read the
provider. Execution and explain validate the bounds on exact scan routes.

Invalidating HB candidates does not repair provider-owned stale client/index
state. In particular, an embedded Chroma reader retained across another process's
writes needs a fresh reader process for this usage. Consumer-controlled rebuilds
must coordinate the provider lifecycle as well as changing the HB token. HB does
not supervise application processes or implement a business publication protocol.

## Invariants

Internal frame projection and pagination share privately owned read-only cells;
trusted validation and portable exact scoring borrow read-only row views. This
avoids repeated deep copies of complete vector matrices inside one execution.
Constructor inputs and public row/column/scalar exports remain deep-copy isolated.
Each execution creates its own result; only explicitly versioned candidate data
may be reused. Structural sharing and candidate reuse are not a result cache.

- Query construction and transformation perform no backend IO.
- Queries are immutable values; branching is ordinary assignment.
- Queries store request data only.
- Explain and execution artifacts are temporary and execution-owned.
- Repeated execution recomputes; query results are never cached or persisted
  automatically.
- Every execution returns a detached frame.
- User code crosses the result boundary through an explicit export.
- Terminal count has one source-only meaning and one scalar extraction path.
