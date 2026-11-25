# Object ID Semantics

HeavenBase has one user-facing row identifier field: `object_id`.

`object_id` is required on every entity schema, is always the primary key, and must use the logical type `Identifier`. If a Python or JSON entity definition omits it, HeavenBase adds the field during schema compilation. Public row-addressing APIs use object ID values for row identity and use `entity` for the schema/type when disambiguation is needed. `entity_id` remains the entity type ID in `EntitySchema`, JSON entity definitions, query fragments, and system metadata.

The `mysql` (including MariaDB), `dolt`, `seekdb` and `oceanbase` backend
types compare `object_id` exactly. Their new entity and side tables use a
case-sensitive `utf8mb4` collation for `object_id`, chosen from the version the
server reports. MySQL 8.0.17 and later use `utf8mb4_0900_bin`, which
distinguishes trailing spaces. Earlier MySQL 8.0 releases use
`utf8mb4_0900_as_cs`. MariaDB 10.2.2 and later use `utf8mb4_nopad_bin`, and
older servers, including servers that report a MySQL 5.7 version, use
`utf8mb4_bin`. Existing tables keep their declared collation. If a primary key
considers two spellings equal, upserting the second spelling raises a
duplicate-key error and preserves the existing row. Sparse-GRAM writes replace
index entries by exact object ID on these backend types. SQLite, DuckDB, the
PostgreSQL types (whose deterministic collations break ties by bytes), Oracle at
its default `NLS_COMP=BINARY` and ClickHouse compare IDs exactly. The generic
`sql` type compares them as its server does, so on any other dialect each write
call first reads the stored IDs that the server matches with its IDs, one query
per entity for its upserts and one for its deletes: an upsert whose `object_id`
matches a stored ID that differs from it raises `ValueError` before that backend
call writes anything, so the stored row is kept, unless the same call deletes
that ID before its first upsert of the entity (a `ws.upsert` writes the entity
row and its Catalog row in two backend calls, so a Catalog row refused this way
leaves the entity row written); a delete of an ID that is not stored exactly
removes nothing. A write that commits between that read and the call's own
statements is not seen by it. `mssql`, `starrocks` and `trino` send no such
query and have not been run for this: where their server counts two spellings
as equal, as SQL Server's default collation does with case, an upsert of one
replaces a row stored under the other, as in 0.1.2.2.

`.pk()` is therefore redundant for normal user code. It is accepted only on `object_id` for explicit metadata or JSON import compatibility; marking any other field as primary key is rejected. Composite identity is expressed by computing `object_id` from the relevant fields, not by declaring multiple primary-key columns.

`Identifier` validates as a non-empty string. Its descriptor owns a positive `max_chars` value, with 63 as the stable built-in default. SQL-family backends use the descriptor limit for identifier columns; vector and file backends store the same string value as their row key. Serialized schemas include the concrete limit, so changing Context config cannot reinterpret an existing identifier domain.

`object_id` storage placement is fixed by HeavenBase. Users cannot customize `object_id.store(...)`; after all non-ID fields are resolved, HeavenBase stores `object_id` as an inline field on every backend that stores any part of that entity. An entity that stores only `object_id` uses the workspace default backend. This guarantees that every participating backend can perform direct row-identity lookups and deletes without consulting another backend.

When `set`, `set_many`, `delete`, or `delete_many` infers an entity from an
object ID, an entity whose routes all miss the ID is excluded when one of
its oldest routes, those it has had at least as long as any other, is a
backend other than Pinecone. Every backend but Pinecone keeps every row written
to it, and every row is written to every route its entity has at the time, so
a miss on such a route shows the row is absent. A Pinecone fetch can miss
a record written recently, so when every oldest route of an entity is Pinecone,
the miss is inconclusive and aborts the mutation, also when no other entity
holds the ID: `delete` and `delete_many` raise `ValueError` instead of
reporting the ID absent.
`register(..., allow_replace=True)` can add a route to an entity that already
holds rows. It does not copy those rows to the new route, so the new route is
not asked while the entity keeps a route it had before: when those older routes
are all Pinecone, entity inference, `upsert` with `overwrite=False`, and `set`
of a new ID raise `ValueError` as for an entity stored only on Pinecone.
Once the replacement that drops the last older route is registered, the rows
the newer route lacks are held only on routes the entity no longer reads, and
the newer route settles absence. The entity's MetaSchema entity row records
the routes that joined later under `meta.route_generations`, so a later facade
or process keeps the rule. A route that a replacement drops keeps what it
stored, and writes and deletes made while it is dropped do not reach it. A later
replacement that adds the route back reads it as stored, as in 0.1.2.2: a row
deleted in between can return with only that route's fields, which also makes a
create-only write of its ID raise, and a row still stored regains the values the
route held. Delete such a row again, or rewrite it with a full `upsert`, after
adding the route back. Registrations made by HeavenBase 0.1.2.2 or earlier
record no route generations, so every route they stored counts as oldest: if
such a release added a route through a replacement, rewrite each
row stored before that replacement with a full `upsert` (for an entity with a
Pinecone route, read the rows by ID with `get_many`, because Pinecone cannot
list its records) before relying on the absence checks. This check also applies
within the visible entities of an MCP profile. Supply the entity explicitly to
resolve the target.

A native record-graph backend may additionally project a validated cross-entity target as a typed,
id-only endpoint anchor even when that backend is not in the target's `StoragePlan`. Under the coherent
`StoragePlan` that created it, the anchor is not a row fragment: it does not contribute to logical `get`,
`exists`, `count`, hydration, or Catalog state and is deleted with the authoritative target. Route
migrations must remove or rewrite obsolete anchors before the new plan is coherent.

Each participating backend stores only the fields it owns plus the fixed `object_id` identity. On reads, HeavenBase merges backend fragments through the same storage plan and ignores provider-returned fields that the backend does not own. This keeps historical dynamic metadata or provider-normalized stale copies from becoming authoritative conflicts while still detecting real conflicts for replicated owned fields.

Replicated `GraphEdge` fields compare as endpoint-keyed relationship sets, so provider relationship
iteration order is not a conflict; endpoint properties must still agree. Other replicated-field
conflicts, or GraphEdge copies with different endpoints/properties, fail closed. JSON Booleans
and numbers are distinct during replica comparison, including inside arrays and objects. After a partial
multi-backend apply, do not use `unique=False` as reconciliation. Quiesce writers, reconstruct one valid
authoritative full row, and `upsert` it to converge every declared route before audit/retry.

## Identifier Audit

This table is the reference for the `id` / `entity_id` / `object_id` names that still appear in code, docs, demos, and provider payloads. The invariant is that every current entity row has exactly one primary-key field, `object_id`; every other ID name is scoped to a narrower domain.

| Surface | Meaning | Primary key? | Notes |
| --- | --- | --- | --- |
| `object_id` on user entities | Concrete object row identity | Yes | Injected into every Python and JSON entity schema when omitted. |
| `object_id` on `Catalog` | Catalog row identity | Yes | Usually `catalog:<entity_type>:<object_id>` or a hash when the readable form is too long. |
| `Catalog.target_id` | Target object's `object_id` value | No | Use with `Catalog.target_entity` to hydrate the typed row: `ws.get(row["target_id"], entity=row["target_entity"])`. |
| `Catalog.target_entity` | Target entity type ID | No | Points to the concrete entity schema, for example `product`. |
| `object_id` on `MetaSchema` | Metadata row identity | Yes | Metadata rows use readable IDs such as `field:product.name` when they fit. |
| `MetaSchema.subject_id` | Metadata subject | No | Entity, field, backend, storage placement, or capability subject depending on `kind`. |
| Entity class `identifier`, `EntitySchema.entity_id`, JSON top-level `entity_id` | Entity type identifier | No | Names the schema/table/collection namespace; it is not a row primary key. |
| `QuerySpec.entity_id`, `StoragePlan.entity_id`, `StorageBinding.entity_id`, fragment `notes["entity_id"]` | Entity type ID | No | Query routing still groups work by entity type. |
| `RowOp.entity_id` and backend method parameter `entity_id` | Entity type ID | No | `RowOp.object_id` and backend `ids` carry object IDs. |
| `ResultFrame.ids` | Values from the frame identity column | Yes, for identity-bearing frame rows | Entity query frames use `object_id`. Identityless table frames raise for `ids`, `filter_ids`, and merge helpers. |
| SQL main tables | `object_id` column | Yes | Side tables also join through `object_id`. |
| File backend paths | Entity type folder + record file named by the percent-encoded object ID, or by a hashed name (32 hex digits of the ID's SHA-256, `=`, at most the first 32 encoded characters) when the encoded name is too long for the file system (on Windows, also when the record's temporary path would exceed 259 characters), is a Windows device name, or already holds an ID that differs only by letter case | Yes, in filename/payload | `entity_id` appears only as the folder namespace. A lookup accepts an encoded-name file only when its stored `object_id` matches (checked for IDs with an ASCII letter or `~`, the names a case-insensitive file system or a Windows 8.3 short name can alias), so such IDs stay distinct on case-insensitive file systems (default macOS and Windows). On Windows, a record file whose full path exceeds `MAX_PATH` (a long encoded name written on another file system, or a hashed name under a deep folder) is opened through its extended-length `\\?\` path, so it works without long-path support. |
| Vector/search provider record `id` | Provider-native record key | Provider-scoped | Adapters map it to/from `object_id` at the HeavenBase boundary. |
| Capsule, toolkit, prompt, backend, workspace, LLM tool-call `id` | Domain-specific identifier | No | These are not entity row primary keys unless copied into an entity row's `object_id`. |
| Interop argument `id="source_column"` | Source-column selector for object IDs | No | The selected source column becomes the row `object_id`. |

JSON entity definitions intentionally use `entity_id` for the entity type. A top-level `id` key is rejected rather than treated as a compatibility alias.

## Catalog Rows

`Catalog` is a universal AI-facing object directory. It stores a pointer back to the concrete row, generated identity and timing fields, and optional projection fields declared by the entity. Same-named native fields project automatically only when their logical type descriptors match the Catalog field; incompatible same-named fields remain application data and leave the Catalog default unchanged. Explicit derives handle non-matching fields or normalizations.

Field groups follow the UKF layout. `desc` is the short searchable blurb; `summary` is the long payload text.

| Catalog field group | Fields | Ownership |
| --- | --- | --- |
| Identity | `object_id`, `target_id`, `target_entity`, `name`, `version`, `type` | `object_id`, `target_id`, `target_entity` are generated; `name`, `version`, `type` project or default |
| Payload | `summary`, `instructions`, `examples`, `artifacts`, `confidence` | Projection, derive, or defaults |
| Searchable | `desc`, `keywords`, `tags`, `priority` | Projection, derive, or defaults |
| Governance | `ws`, `active`, `visible`, `created_at`, `updated_at`, `expires_at`, `expires_after`, `stale_after`, `creator`, `owner`, `auth`, `metadata`, `note` | `ws`, `created_at`, `updated_at` are generated; `active` and `visible` persist across upserts unless overridden; timing policy fields project; `creator`, `owner`, `auth`, `metadata` accept workspace signing defaults |
| Materialized timing | `expires_compute`, `stale_compute` | Generated on write from policy fields |
| Lineage | `source`, `parent_id`, `history`, `transforms` | Projection, derive, or defaults |
| Usage | `statistics`, `feedback`, `annotations` | Projection, derive, or defaults |
| Reference | `related`, `automations` | Projection, derive, or defaults |

Expiration policy semantics:

- `expires_at` is an absolute ceiling in epoch milliseconds. `hb.Timestamp.default_max` means no absolute expiry.
- `expires_after` and `stale_after` are `Duration` values in milliseconds. `0` means disabled.
- `expires_compute` is the minimum applicable hard-expiry deadline. `stale_compute` is `stale_after + updated_at` when `stale_after` is set. When a policy is off, the materialized field is `hb.Timestamp.default_max`.

For ordinary discovery, use `Query.available()` in Python, the JSON query key
`available`, or the Mongo-style filter operator `$available`. Rows with
`active=false`, `visible=false`, or materialized expiry at or before the
evaluation time should not appear in default agent-facing discovery. Pass
`stale=true` to include stale rows and `invisible=true` to include hidden rows
while still excluding hard-expired rows.

Use `Catalog.target_id` and `Catalog.target_entity` to retrieve the real object row; never read user fields from `Catalog` itself.

The table above describes entities registered with the default
`catalog="full"`. `ws.register(Entity, catalog="identity")` publishes a Catalog
row that keeps only the generated identity fields (`object_id`, `target_id`,
`target_entity`, `ws`), the lifecycle fields, and the field defaults: the
stored row is read by id so its `created_at`, `active`, and `visible` carry
over exactly as under `full` (`updated_at` is the write time), nothing else is
merged from it, nothing is projected or derived, the workspace signature set by
`ws.sign(...)` is not applied (`owner`, `creator`, `auth`, and `metadata` keep
their defaults), and its keyword field is empty. Under
`catalog_mode="merge"` (the default) an `identity` write that finds a stored
Catalog row still runs keyword-index maintenance for it, so keywords that a
direct Catalog write, or a writer still registered as `full`, stored on that row
lose their keyword rows, including a write that lands between the lookup and
the commit. An `identity` write that finds no stored Catalog row, and every
`identity` write under `catalog_mode="fresh"`, skips that maintenance and
writes no keyword rows; on a SQL route that keeps a keyword index it reads the
row's keyword rows once after its row statements and deletes any that another
writer committed for the same Catalog row in that window, in the same
transaction. Such rows still appear in discovery and hydrate through
`target_id`/`target_entity`.
`catalog="none"` publishes no Catalog row at all; those objects are invisible
to Catalog discovery and to the Catalog
audit, and are reached only by reading the entity (`get`, `get_many`,
`read_rows`, `iter_rows`, `query`).

Changing the mode of a registered entity (`allow_replace=True`) rewrites or
deletes its stored Catalog rows in bounded pages. Leaving `full` rewrites
before switching when there is no staged GRAM transaction or when the Catalog
and MetaSchema writes share the sole staged transaction. If those writes can commit
independently, the GRAM transactions commit first, followed by publication of
the new registration and the Catalog rewrite. Entering `full` switches first.
A registration with no live
predecessor is compared with the registration the store's MetaSchema entity
row recorded (the defaults for a row written before options existed), and a
different `catalog` is reconciled from it the same way, so a fresh `identity`
registration over rows stored by an earlier `full` one rewrites them and
removes their keyword rows before the first `identity` write. A change from
`none` to `full` or `identity` first reads the stored ids and raises
`ValueError`, with nothing switched or rewritten, when one is longer than a
Catalog row's `target_id` holds (255 characters). A page that fails raises
`RuntimeError` and reports the registration in effect. Previously committed
pages keep their changes. Pages written in an
open SQL registration transaction roll back with it. `ws.audit()` lists the
rows that differ from the registration in effect and `ws.repair()` rewrites
them. The change can then be requested again.
A Catalog delete keeps keyword-index maintenance whatever the mode, so a
keyword row cannot outlive its Catalog row.

For free-text card discovery, use `ws.search(card_text="customer count")`. It
searches discoverable Catalog object cards and active MetaSchema structure
cards, returning a detached `ResultFrame` with source row identity, target
pointers, card text, and a heuristic `score`; export it through `rows()` or an
interop conversion before application use. Pass
`target_entities=["db-rule", "db-snippet"]` to rank only Catalog object cards
for those entity kinds and exclude MetaSchema cards; the scoped result still
scores every match before applying `limit`. Use `ws.query(...)` when you need
exact structured predicates or backend explain diagnostics.

## Text Lengths

HeavenBase keeps the default string length ladder aligned across logical types and physical backends:

| Logical type | Stable default `max_chars` | Explicit Python descriptor |
| --- | ---: | --- |
| `Identifier` | 63 | `hb.Identifier(max_chars=...)` |
| `ShortText` | 255 | `hb.ShortText(max_chars=...)` |
| `MediumText` | 4095 | `hb.MediumText(max_chars=...)` |
| `LongText` | 65535 | `hb.LongText(max_chars=...)` |

Every instance stores a validated positive integer and `to_dict()` emits it as `max_chars`; `from_dict()` and `clone()` retain it. This applies recursively inside `Array`, `HyperG`, field schemas, and workspace manifests. The logical constraint is therefore part of schema identity rather than a deployment setting.

Timestamp protocol bounds are fixed in milliseconds and exposed as `hb.Timestamp.default_max` and `hb.Timestamp.default_min`:

| Bound | Stable default (ms) | Explicit descriptor field |
| --- | ---: | --- |
| Upper | 253402300799000 | `max` |
| Lower | 0 | `min` |

`Timestamp` values are clipped to `[default_min, default_max]` during validation and query-filter normalization. `Timestamp(max=..., min=...)` serializes explicit instance bounds; omitted bounds always use the protocol constants and never read `CM_HVNB`.

SQL-family backends use the logical type length when creating text columns. StarRocks caps values at its configured `heavenbase.backend_defaults.starrocks.varchar.max_length` when a logical length exceeds the backend limit. Milvus uses the logical text length for `Array` element `VARCHAR` fields, capped by `heavenbase.backend_defaults.milvus.array.max_length`. The `object_id` column of a new SQL, pgvector, StarRocks, or Milvus entity table or collection takes the entity's own `object_id` domain. On StarRocks and Milvus it is further capped by the backend's VARCHAR limit and, when set, by `heavenbase.backend_defaults.starrocks.object_id.max_length` or `heavenbase.backend_defaults.milvus.object_id.max_length`. The keyword table that SQL backends share for sparse-GRAM indexes is not sized per entity: its `object_id` column holds 191 characters, so on a SQL dialect that enforces declared column lengths a write that indexes keywords for a longer id fails. A StarRocks table or Milvus collection that an earlier release created keeps the `object_id` length it declared; it still opens, and the engine rejects longer ids written to it until it is recreated. In 0.1.2.2 a Milvus collection declared 63 characters, or `heavenbase.backend_defaults.milvus.object_id.max_length` when that was smaller, and a StarRocks table declared `heavenbase.backend_defaults.starrocks.object_id.max_length` (63 by default), capped by the StarRocks VARCHAR limit.

## Automatic IDs

When an entity does not declare `object_id`, HeavenBase adds only the built-in `object_id: Identifier` field during schema compilation. `object_id` is always required and cannot be null after materialization. HeavenBase never injects a native `name` field; `name` should appear on an entity only when it is a real domain field. When an incoming row omits `object_id`, HeavenBase can generate the ID from an explicit `catalog.name` derive or a native `name` value plus the entity schema ID. The native field projects into Catalog only when its logical type descriptor matches `Catalog.name`; otherwise it still supplies fallback identity, while a visible Catalog name requires an explicit derive:

```python
class Product(hb.Entity):
    __derive__ = [hb.Entity.derive("catalog.name").compute(lambda title: title, inputs=["title"])]

    title = hb.field(hb.ShortText)

object_id = hb.utils.hash_id(Product.schema().entity_id, catalog_name)
```

`hb.utils.hash_id(...)` calls `heavenbase.utils.md5hash`, then formats the integer as a zero-padded decimal string. The default length is 42 via `CM_HVNB.get("heavenbase.entity.id.hash_length", default=42)`. This identity hash is intentionally fixed to MD5 for persisted `object_id` stability; configurable hash selection is reserved for sampling and candidate-generation helpers, not object-id migration.

The schema ID salt keeps default IDs distinct across entity types without making callers repeat the entity in `get` or `set`. This deterministic default removes the old row-count based fallback. It is stable across imports, exports, backend migrations, and MCP sessions as long as the schema ID and derived catalog name are stable.

If neither `object_id` nor a non-empty `catalog.name` source exists, writes fail with an explicit error asking for `object_id`, an `object_id` compute hook, a non-empty native `name`, or an explicit `catalog.name` derive. Blank and whitespace-only `catalog.name` values count as missing so accidental empty labels do not become stable row IDs.

If an entity explicitly declares `object_id`, HeavenBase treats identity as user-controlled. That is the right shape for rows whose stable identity comes from a business key, a tenant-scoped key, or a computed field combination.

## Computed IDs

Users can define `object_id` as a computed field when the stable identity is another field or a field combination:

```python
class Product(hb.Entity):
    object_id = hb.field(hb.Identifier).compute(hb.utils.hash_id, inputs=["tenant", "sku"])
    tenant = hb.field(hb.ShortText)
    sku = hb.field(hb.ShortText)
```

With `inputs`, the compute hook receives the named field values positionally. Without `inputs`, it receives the whole row dictionary, so `hb.utils.hash_id` can also hash a dict, tuple, or nested JSON-like object. Explicit computed IDs are user-controlled; include a stable discriminator such as tenant or schema ID when different entity types could otherwise produce the same ID.

Entity-level derives use the same compute model for native fields and Catalog projection:

```python
class Note(hb.Entity):
    __derive__ = [
        hb.Entity.derive("catalog.name").compute(lambda title: title, inputs=["title"]),
        hb.Entity.derive("catalog.desc").compute(lambda body: body, inputs=["body"]),
    ]

    title = hb.field(hb.ShortText)
    body = hb.field(hb.LongText)
```

## Manual IDs

Manual IDs are accepted through `object_id`. HeavenBase validates the value and does not rewrite or sort-normalize it. If the ID is numeric or sequence-like, the caller is responsible for lexical sortability, for example by zero-padding integers before write.

Declare a different manual ID domain on the schema itself with `hb.field(hb.Identifier(max_chars=...))`; runtime config does not change logical validation. Backend adapters can still reject or cap a route when the storage engine has a stricter physical maximum. A Catalog row's `target_id` holds ids of up to 255 characters, so under `catalog="full"` or `"identity"` a write of a longer id is rejected before anything is stored; register an entity whose ids exceed 255 characters with `catalog="none"`. A Catalog table that an earlier release created on a SQL dialect that enforces declared column lengths keeps its 63-character `target_id` column, and such a table rejects longer target ids until it is recreated. These dialects include PostgreSQL, MySQL, SQL Server and Oracle, and the backends built on or compatible with them, such as pgvector, Supabase, OceanBase, Dolt and SeekDB. On MySQL that narrower column also leaves the exact text semantics of `target_id` unproven, so its `eq`, `ne` and `in` filters run in Python over a Catalog scan instead of in MySQL until the table is recreated. Internally generated system row IDs use `hb.utils.hash_id(...)` when their readable form would exceed the schema's `Identifier` limit; the readable subject stays in `subject_id`, `name`, `field`, `backend`, and `meta`.

`upsert` keeps its replace semantics by default: writing the same `object_id` again replaces that row in each participating backend. Pass `overwrite=False` to make existing IDs an error. For an entity stored only on Pinecone, `overwrite=False` raises `ValueError` before writing whenever no route returns the ID, because a Pinecone fetch miss does not show that the ID is new; pass `overwrite=True` to write such a row. `upsert(entity, row=None, **values)` reads the keywords `overwrite` and `atomic` as the replace and one-transaction flags, each a bool, unless the row's Entity class declares a field of that name; the keyword is then that field's value, and such an entity passes the flag to `upsert_many`. Workspaces isolate rows independently, so the same ID can exist in different workspaces without conflict.

`upsert_many` rejects duplicate object IDs inside a single batch before writing to any backend, even when `overwrite=True`. That includes explicit duplicates and auto-generated duplicates from repeated `name` values. This keeps batch behavior deterministic and leaves room for backend-level parallelism without order-dependent duplicate handling.

`upsert_many(..., catalog_mode="merge")` is the default and preserves lifecycle
fields from matching existing Catalog rows, which it reads by id in one lookup
per Catalog route for the whole batch. A bounded importer constructing new
Catalog rows may use `catalog_mode="fresh"` to skip that lookup. `fresh` is an
explicit caller assertion: do not use it to update an existing Catalog row,
because HeavenBase will create new lifecycle timestamps for that object. Both
modes retain ordinary Entity materialization, validation, routing, backend
ownership order, Catalog derives, and duplicate-ID checks. Entities registered
with `catalog="full"` or `catalog="identity"` read existing Catalog rows under
`merge`; `catalog="none"` entities publish no Catalog row, whatever
`catalog_mode` says.

Both operations require the Entity class to be present in the workspace before
the row call begins. A class, registered identifier, or Entity instance selects
that already-present canonical schema; none of those input forms authorizes
implicit registration.

## Get And Set

`get(object_id, entity=None, unique=True)` normalizes the ID with the fixed
`Identifier` field, reads participating backend fragments by ID, and merges
storage-owned fields from fragments with the same physical `object_id`. When
`entity` is omitted, HeavenBase reads every class already present in the
workspace, Catalog included, and an ID that exists under more than one entity
raises and asks for `entity=...` whatever `unique` says. When `entity` is
supplied, it must resolve to one of those canonical classes and never
registers a new one; the read then touches only the backends routed by that
entity's storage plan, through batched `get_many` lookups and the workspace
row codec, and never consults Catalog or another entity. Cross-entity
uniqueness is moot in that mode, and a duplicate ID inside one entity cannot
exist because every route stores it as the physical primary key, so `unique`
governs only routed fragments of one row that disagree on a stored field:
`True` raises, `False` keeps the first route's value.

`get_many(object_ids, entity=None, unique=True)` applies the same semantics in input order and returns `None` for missing IDs. It is a backend primitive rather than a query-handler capability: SQL and PgVector backends use batched `WHERE object_id IN (...)` queries; file backends read requested record files directly; in-memory backends use direct dict lookup; LanceDB, ChromaDB, Milvus, Pinecone, and Elasticsearch use provider-native point-lookup APIs. Provider adapters do not keep process-local mirrors for unavailable external clients; unavailable providers fail explicitly instead of returning stale process memory. `exists(object_id, entity=None)` and `exists_many(object_ids, entity=None)` use the same lookup rules across participating backends; `exists_many` returns a boolean list aligned with the input IDs. Unlike `get`, an entity-less existence check honors `unique=False` by reporting presence instead of raising on a cross-entity duplicate.

`read_rows(entity, object_ids)` is the entity-first form of that read. It returns
decoded logical rows aligned to the input, bypasses Catalog, and raises on
disagreeing routed fragments as `unique=True` does. `iter_rows(entity,
batch_size=128)` uses the routed backends' keyset `scan_page` calls and merges
and decodes rows like `get`. Rows are in ascending `object_id` order when all
routes use Python's string ordering. Backend collations can change that order.
The workspace checks liveness and holds the workspace read lock for each page
fetch. Workspace and backend locks are released before the rows are yielded,
so the consumer or another thread may write between pages. Each page uses the
entity schema current at that page and may include newly added fields on the
original routes. Start a new scan to include newly added routes.

`set(row, values=None, entity=None, overwrite=True)` accepts an `Entity` object, a row patch containing `object_id`, or `set(object_id, values, entity=...)`. Existing rows are patched when `overwrite=True`. `overwrite=False` rejects an existing ID before writing. `set` can also create a new ID when the entity type is known, all required fields can be materialized, and one of the entity's oldest routes can show that the ID is new, as a route on any backend other than Pinecone does. Otherwise use a full `upsert`, when replacing a stored row is acceptable. Use `upsert` followed by `delete` for an intentional ID migration.

`delete(target, edges="schema")` intentionally has one row target parameter plus one GraphEdge policy. `object_id` and `entity` are not interchangeable: the ID is the row identity, while the entity is only a type disambiguator. For delete, pass a raw ID when it is workspace-unique, an Entity instance with `object_id`, a mapping like `{"entity": Product, "object_id": "p1"}`, or a pair like `(Product, "p1")`. `edges="schema"` uses each incoming `GraphEdge.on_delete` policy; `edges="restrict"` rejects surviving incoming references, while `edges="detach"` removes those edge values before deletion. Restriction raises `hb.GraphDeleteRestrictedError` without mutating entity/Catalog rows. `delete` returns whether a route returned the row before deletion. For an entity stored only on Pinecone, a fetch miss makes it return `False`, but it still deletes the record and its Catalog row. `delete_many(targets, edges="schema")` applies the same policy to one logical batch, returns booleans in input order, and ignores incoming edges whose source is also in the delete batch.

`upsert_many(entity, rows, overwrite=True, catalog_mode="merge", atomic=False)` and `set_many(rows, entity=None, overwrite=True)` return object IDs in input order. The workspace compiles entity upserts, GraphEdge detach rewrites, Catalog changes, and delete `RowOp` values, then batches them by backend through `Backend.upsert(rows)`. A transactional backend owns atomicity for its batch; independently routed backends do not form a distributed transaction. By default entity batches commit before their Catalog batches; `upsert`/`upsert_many` with `atomic=True` send both through one `Backend.upsert(rows)` call when every routed write lands on one backend that claims the `atomic-upsert` tag (a failed call applies none of its rows), and refuse with `ValueError` otherwise.

`count(entity)` returns the current logical row count for one entity using
participating backend row-count primitives and returns the maximum count when
replicated backend fragments diverge. Normal HeavenBase writes keep those
counts equal; the max rule avoids false undercounts after a partial external
delete or temporary provider inconsistency. A filtered count is the source-only
terminal query
`ws.query(Entity).where(...).count().execute().scalar()`. It is not a
post-projection, grouped, ordered, offset, or limited row count.

## Backend Coverage And Limits

All built-in backends implement `get_many`, `exists_many`, and `count` without the base scan fallback in the normal path:

| Backend family | Row-ID implementation |
| --- | --- |
| In-memory | Dict lookup and bucket length |
| JSON/Pickle file | Direct record-file lookup and file count |
| SQL, SQLite, DuckDB, Postgres, MySQL, OceanBase, MsSQL, Oracle, StarRocks, Trino | Batched SQL `IN` lookup, ID-only existence query, SQL `COUNT(*)` |
| PgVector | Same SQL primitives through Postgres |
| LanceDB | Native ID filters and table row count |
| ChromaDB | Native collection `get(ids=...)` and collection count |
| Milvus | Native primary-key `get` and collection stats |
| Pinecone | Native `fetch(ids=...)` and index stats for vector-backed rows |
| Elasticsearch | Native `_mget` and index count |

Every backend requires the entity schema to be present in the live workspace so
HeavenBase knows storage placements and field names before writing or merging
row fragments. Reopening persistent provider data therefore requires schema
registration or manifest/extension replay before CRUD. A Milvus or Pinecone
record cannot exist without its vector, so neither stores a row without one:
Milvus requires its routed Vector fields to be required, and Pinecone refuses an
upsert row without its vector unless the entity also routes to a backend that
keeps every row (see [Backends](backends.md)) and every other field the row
routes to Pinecone is `None`. Such a row has no Pinecone
record, and any record stored earlier for its id is deleted. The row stays
available through the other backends but cannot be recovered from Pinecone
alone. If the source read for `set`, `set_many`, or a GraphEdge rewrite misses
a Pinecone record, the whole mutation raises `ValueError` before writing.
Retry once the record is visible. Use a full `upsert` for a deliberately
vectorless row.

## Python Operators

HeavenBase also supports lightweight Python mapping-style shortcuts for row IDs:

```python
ws[Product, "p1"] = {"name": "Desk", "price": 89}
row = ws["p1"]
"p1" in ws
del ws[Product, "p1"]
```

Use `ws[entity, id]` when creating a new row or disambiguating an ID. `ws[id]` works when the ID is already unique in the workspace. Missing `__getitem__` and `__delitem__` targets raise `KeyError`.

`iter(ws)`, `len(ws)`, `ws.ids()`, `ws.rows()`, and `ws.items()` are intentionally enabled only when the workspace has exactly one non-system entity. In multi-entity workspaces, pass `entity=...` to `ids`, `rows`, or `items` instead of relying on workspace-wide iteration.
