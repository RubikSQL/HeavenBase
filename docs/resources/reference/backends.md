# Built-In Backend Providers

Backend configuration uses lower-case Registry identifiers. Product names are
not planner branches: each selector resolves through the same Context-local
`backend_type` record and module-folder protocol as an external provider.

## Provider Matrix

| Storage role | Canonical selectors | Useful aliases |
| --- | --- | --- |
| Local and file | `inmem`, `file`, `json`, `pickle`, `sqlite`, `duckdb`, `tinydb` | `memory` → `inmem`; `tiny` → `tinydb` |
| SQL and analytics | `postgres`, `supabase`, `mysql`, `seekdb`, `oceanbase`, `dolt`, `mssql`, `oracle`, `starrocks`, `trino`, `clickhouse` | `postgresql`, `pg` → `postgres`; `sqlserver`, `sql-server`, legacy `msql` → `mssql` |
| Document, key-value, and text search | `mongodb`, `redis`, `elasticsearch`, `opensearch` | `mongo` → `mongodb`; `elastic`, `search` → `elasticsearch`; `open-search` → `opensearch` |
| Vector and hybrid search | `pgvector`, `lance`, `milvus`, `milvus-lite`, `chroma`, `pinecone`, `qdrant`, `weaviate`, `redisearch` | `lancedb` → `lance`; `milvus_lite` → `milvus-lite`; `redis_query_engine` → `redisearch` |
| Graph and multi-model | `neo4j`, `surreal` | `surrealdb` → `surreal` |

The protocol-compatible SQL selectors remain distinct backend identities.
`supabase` uses PostgreSQL connectivity with workspace-owned table names inside
the managed database. `seekdb` and `dolt` use their MySQL-compatible server
protocols without inheriting unproved text semantics. A `seekdb` route with a
`url` connects to the server the URL names; without one it uses seekdb's local
defaults. Every engine HeavenBase builds for a MySQL-protocol server, a
plain `hb.Database` included, turns the server's autocommit off on each new
PyMySQL connection that did not ask for autocommit: dolt and OceanBase report
no autocommit flag when a connection opens, so PyMySQL would otherwise leave
their sessions committing every statement and a rollback would undo nothing.
`clickhouse` owns its MergeTree lifecycle and non-transactional mutation behavior.

Replacing a stored row on a `duckdb` backend, or on a `sql` backend whose
database is DuckDB, needs the `duckdb` package 1.2 or later. Before 1.2, DuckDB
rejects inserting a key again in the transaction that deleted it, and updating
an indexed column, so it cannot replace a row atomically. On such a release the
backend refuses the replacement with a `RuntimeError` before running it, and the
rest of that write rolls back with it. This also covers the sparse-GRAM rebuild
or metadata change of an indexed field.
Connecting, inserting new rows, deleting and reading work as before. A
workspace whose main route is DuckDB replaces its own MetaSchema rows while it
starts, so it needs 1.2 or later.

`pgvector` defaults to one PostgreSQL database per workspace. Set
`shared_database: true` to retain the configured database and scope every
physical table by workspace and backend name; `web-lts` uses this managed
database mode.

LanceDB commits each merge batch in its own transaction. If a merge fails,
HeavenBase reports the provider error and stops processing later batches.
Earlier batches and other clients' commits remain in effect.

`milvus-lite` is not an alias for the Milvus server. It owns a collision-resistant
local database file per workspace and backend instance, and omits server-only
database administration. Vector fields routed to either Milvus deployment must
be required because Milvus rows cannot represent a missing `FLOAT_VECTOR`.
A Json field routed to Milvus uses a Milvus JSON column. Objects and arrays keep
the storage layout used by HeavenBase 0.1.2.2, including objects whose only key
is `__heavenbase_json__`. Scalar values use an object that holds their JSON text
under `__heavenbase_json__` and a marker under `__heavenbase_json_version__`.
The Json encoder always represents the marker integer as a tagged object in
user data. Reads use the field schema to unwrap scalars in Json fields and
preserve values stored by other logical types.
Reading a newly written scalar with HeavenBase 0.1.2.2 exposes the wrapper object.
pymilvus rejects a query-iterator batch above 16384 rows, so HeavenBase caps
each Milvus server iterator batch there and a larger `batch_size` returns
shorter pages. A Milvus server also rejects a vector search limit above 16384
by default. A `near` whose `top_k` is above 16384
ranks through the portable scan on a Milvus server and returns every requested
neighbour. Milvus Lite uses a binary64 scan for every `exact=True` search and
for ordinary cosine searches. The scan prevents native float32 scoring from
overflowing or rounding distinct scores to the same value under extreme
coordinates. Ordinary L2 and inner-product queries can use native search at
any `top_k` in Milvus Lite when their metric matches the index. Native Lite
searches request routed fields by name so hit rows include those values.
The `heavenbase.query.near.scan_batch_size` setting (256 by default) controls
the read batch size for portable exact scans and ordinary Milvus Lite cosine
scans. `explain` reports the selected route. The workspace setting
`heavenbase.query.near.max_top_k` (1000 by default) bounds every `top_k` first.
Milvus Lite requires both `pymilvus` and `milvus-lite`. The
`pymilvus[milvus-lite]` extra in PyMilvus 3.0.0 omits `milvus-lite` on Windows.
The separately published Milvus Lite 3.1.0 package lists Windows support
where its dependencies are available. [PyMilvus packaging](https://github.com/milvus-io/pymilvus/blob/v3.0.0/pyproject.toml)
[Milvus Lite requirements](https://pypi.org/project/milvus-lite/3.1.0/#description)

TinyDB uses the same collision-resistant owner scoping for its local file.
Qdrant may also use its client library's local path mode. The configured
`path` is a base directory beneath which HeavenBase
derives one workspace-and-backend-owned directory. `qdrant` retains the same
canonical provider identity in local and server deployments.

A Pinecone record is one dense vector with its metadata. The fields an entity
routes to Pinecone must include exactly one Vector field, with a fixed
dimension; registration refuses an entity that routes no Vector field there, or
more than one, and an existing index of another dimension. Every other field
routed to Pinecone is metadata. An `upsert` row without its vector is written
only when the entity also routes to a backend that keeps every row and every
other field the row routes to Pinecone is `None`. Each required or defaulted
field routed to Pinecone must also be stored on a backend that keeps every
row, including a nullable field explicitly set to `None`. Pinecone then
deletes any record stored earlier for that ID. Every backend but Pinecone
keeps every row. Another Pinecone backend keeps every row only when the Vector
field routed to it is required. A `set` that names the vector with
`None` clears it the same way. Any other row without its vector fails the
upsert with `ValueError` before the workspace writes any route. The workspace
applies routes one after another. A provider failure during application can
leave earlier routes committed.

Pinecone reads are eventually consistent. `set`, `set_many`, and GraphEdge
rewrites require a record from every Pinecone route of each source row. A
missing record raises `ValueError` before any row is written or deleted in the
batch. Retry when Pinecone can read the record. A successful fetch can still
return an older version. When no route returns a row, a route other than
Pinecone shows the row is absent if it is one of the entity's oldest routes; a
route that a replacement registration added is not asked while an older route
is left (see [ID semantics](id-semantics.md)). An entity stored only on Pinecone has no such
route, and neither has one whose routes from before such a replacement are all
Pinecone, so a mutation that needs the row to be absent raises `ValueError`: entity
inference, `upsert` with `overwrite=False`, `set` of a new ID, and a confirmed
repair of a missing target. Use a full `upsert` or `upsert_many` to create a row,
update a deliberately vectorless row, or add a Pinecone record to it. A
GraphEdge delete of such a missing target restricts or detaches its incoming
references as for a present target.

Pinecone documents no bound on how long a write takes to become readable, and
its read-after-write check (a query's `x-pinecone-max-indexed-lsn` at or past a
write's log sequence number) needs that write's LSN, which a reader has only for
its own writes. So a miss of a record that another client or an earlier process
wrote cannot be settled, which is why the checks above stay. When hydrating near
results, the backend fetches a missed match again with a short backoff, bounded
by `PineconeBackend.miss_settle_seconds` (5 seconds by default) plus the fetch in
progress. The wait runs inside the query, which holds the workspace read lock, so
writers of the workspace wait for it. A record a later fetch returns is hydrated
from it. A match still missing keeps the values the query returned, as in 0.1.2.2,
including a record deleted after the query, because a miss never shows that the
record is gone. When a fetch reports an index-view LSN at or past the one the query
reported, the backend stops waiting for that match. Pinecone documents that LSN for
queries, not for fetches, so a fetch may report none; then the backend fetches the
match only once more.

Chroma and Pinecone preserve explicit `None` in nullable metadata fields with
defaults when storing a record. New writes mark the null so reads can
distinguish it from an absent schema field. If an earlier release omitted the
key when clearing a defaulted field, re-upsert the full row with the intended value.

Remote Milvus databases and Qdrant, Weaviate, MongoDB, and OpenSearch resource
names include both the workspace and backend instance identity. Their drop
operations discover only that bounded owner scope, including from a fresh
process.

## Configuration

Every provider accepts the ordinary explicit backend map:

```yaml
heavenbase:
  backends:
    main:
      type: mongodb
      name: main
      uri: "${env:HVNB_MONGODB_URI,mongodb://localhost:27017}"
    vec:
      type: qdrant
      name: vec
      url: "${env:HVNB_QDRANT_URL,http://localhost:6333}"
    search:
      type: opensearch
      name: search
      hosts:
        - "${env:HVNB_OPENSEARCH_URL,http://localhost:9201}"
```

Local-file providers require the shared `file://` locator grammar rather than
bare paths. Keep credentials in environment interpolation or explicit process
configuration; backend diagnostics omit authentication values.

Provider packages are optional. The `sql` extra installs maintained SQL
drivers, while `full` installs the built-in provider clients available for the
current platform. Reachability and credentials are live deployment facts, so
static module tags remain descriptive claims rather than execution proof.

## Capability Evidence

Storage roles do not imply that every operation is native. Consult the
[generated capability matrix](../reports/capabilities.md), then use
`Query.explain()` for the exact handler, mode, and fallback reason selected in
one workspace. Row lookup and exhaustive scan contracts are implemented by each
provider adapter even when a filter intentionally falls back to portable Python
evaluation. For example, OpenSearch `match` and `ilike` use the portable path
because its case-insensitive wildcard implementation is ASCII-only while
HeavenBase text matching is Unicode-aware.
