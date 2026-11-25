# ADR 0008 - Scalar indexes and uniqueness constraints

Status: accepted

Date: 2026-07-15

## Decision

HeavenBase exposes two concepts instead of an index-mode vocabulary:

```python
class Product(hb.Entity):
    __indexes__ = [hb.index("tenant", "created_at")]
    __constraints__ = [hb.unique("tenant", "sku", to="main")]

    email = hb.field(hb.ShortText).unique()
    active = hb.field(hb.Boolean).index()
    note = hb.field(hb.ShortText).index(enabled=False)
```

- `index` is physical lookup acceleration. It may be enabled, disabled for a
  provider default, or targeted to one backend instance with `to=`.
- `unique` is a data-integrity constraint. It always has exactly one resolved
  enforcement owner and never degrades to a warning or plain index.

There are no public `auto`, `value`, `off`, or `unique` mode strings. Provider
defaults require no declaration, explicit lookup intent uses `.index()`, opt-out
uses `.index(enabled=False)`, and correctness uses `.unique()`.

## Ownership

Authored `IndexRule` and `UniqueRule` values belong to entity assembly metadata.
They are deliberately absent from passive `FieldSchema` and `EntitySchema`.
`IndexResolver` combines those rules with storage placement and typed backend
capabilities, producing `IndexBinding` and `UniqueBinding` values on the resolved
`StoragePlan`.

This keeps one dependency direction:

```text
FieldSpec / entity declarations
        -> EntityAssembly authored intent
        -> StorageResolver placement
        -> IndexResolver physical bindings
        -> Backend.ensure_plan reconciliation
        -> workspace and MetaSchema publication
```

Backends advertise scalar-index behavior through the registered `scalar_index`
capability whose closed value contract is `ScalarIndexSpec`:
single/composite lookup support, single/composite uniqueness, opt-out support,
explicitly supported logical types, and provider-default types. An explicit
request that cannot be honored fails before logical registration.

## Invariants

- Every entity still has only the fixed `object_id` primary key. Its backend
  lookup structure is implicit and cannot be customized.
- Composite rules require every field to share one inline storage route.
- A uniqueness tuple containing `None` is ignored, matching portable SQL null
  semantics. SQL Server uses a filtered unique index to preserve this rule.
- Replicated lookup indexes may exist on every compatible copy. Replicated
  uniqueness must name one owner when ownership would otherwise be ambiguous.
- Every unique constraint on one replicated entity must resolve to that same
  owner. Splitting a row's constraints across providers would require a
  distributed transaction merely to validate one write, so registration fails
  instead.
- Schema replacement scans the enforcement owner for existing duplicate tuples
  before DDL. `allow_replace=True` cannot bypass this preflight.
- Provider DDL is idempotent and reconciles only Heaven-owned names. External
  indexes are preserved.
- A SQL registration drops a Heaven-owned index on columns its route declares
  when no route of the plan asks for it. An index on a column that the table this
  workspace object has open does not declare can belong to another definition of
  the entity (another route's, another workspace's or a newer release's), so no
  registration drops it, with or without `allow_replace=True`: a field that a new
  process registers without keeps its index and its unique constraint, while one
  removed in the workspace object that registered it loses them, as in 0.1.2.2.
  No registration drops an index on
  DuckDB, because DuckDB cannot
  replay a write-ahead log that drops indexes and then writes rows: a removed
  unique constraint keeps refusing duplicates there.
- For a like reason a table of a DuckDB file that has held rows gets no index with
  a column that none of its indexes covers, whether the field is new or was not
  indexed before: reads scan for it, and later registrations do not add it. A
  registration that declares a unique constraint with such a column raises
  `ValueError` before it changes the table. An index or a unique constraint over
  columns that are all indexed already is created. An in-memory DuckDB database is
  not affected.
- Logical entity/plan/tracking state is published only after every provider
  ensure succeeds. A later metadata failure restores the previous logical state;
  idempotent reconciliation makes retry safe.

There is no distributed transaction across replicated providers. Writes are
partitioned by entity into an enforcement-owner phase followed by a replica
phase, including mixed-entity batches whose owners differ. A rejected constraint
therefore does not fan out that invalid row; a later owner or replica failure can
still leave an incomplete cross-provider batch and must be retried by the caller.

## Index families

The public `.index()` contract is intentionally limited to comparable scalar
lookup indexes. Full-text, sparse-GRAM, vector, and graph indexes are storage
strategies or provider-required structures, not scalar modes:

| Need | HeavenBase contract |
|---|---|
| Exact/range scalar lookup | `.index()` or provider default |
| Logical uniqueness | `.unique()` / `hb.unique(...)` |
| Full-text retrieval | `InvertedIndex` placement/search handler |
| Sparse keyword containment | `SparseGramIndex` strategy |
| Vector nearest neighbors | `VectorIndex` strategy |
| Graph traversal | `GraphEdge` strategy |

This split prevents one overloaded `index(mode=...)` API from mixing correctness,
query semantics, and provider implementation details.

## Provider policy

- SQL supports safe portable scalar types; medium/long text is excluded from the
  generic contract because several dialects require provider-specific prefix or
  collation choices. Trino and StarRocks do not advertise generic scalar DDL.
- InMem implements value maps and atomic uniqueness checks as the correctness
  reference.
- Redis implements planned single-field SET indexes; it does not advertise
  uniqueness or composite scalar indexes.
- Neo4j and SurrealDB reconcile property indexes and native constraints.
- Elasticsearch exposes per-field scalar index enablement, but mapping policy is
  immutable after creation; a policy change requires a migrated physical index.
- RediSearch/search-engine indexes remain provider-managed strategy structures
  and cannot be disabled through `.index(enabled=False)`.

## Rejected alternatives

- Storing physical modes on logical schema IR couples entity identity to a
  workspace/provider choice.
- Treating `unique` as an index mode obscures its correctness guarantee.
- Warning on unsupported explicit intent creates a contract lie.
- A static logical-type allowlist prevents registered extensions from declaring
  capability; typed provider metadata is the resolution authority.
