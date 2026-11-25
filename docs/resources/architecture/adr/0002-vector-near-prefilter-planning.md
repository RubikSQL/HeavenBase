# ADR 0002 - Vector Near Prefilter Planning

Status: accepted

Date: 2026-04-30

## Context

HeavenBase queries can combine scalar `where(...)` predicates with vector `near(...)` search. Before this decision, the runtime evaluated the filter frame and the near frame independently, then intersected them in Python. That is correct, but expensive for large entities because vector search usually needs only a bounded top-k candidate set while scalar filters may fan out across every participating backend.

Vector providers also have different metadata-filter surfaces. PgVector can keep scalar predicates in one SQL statement, LanceDB and Milvus expose filter expressions next to vector search, and Chroma/Pinecone expose metadata filters with narrower operator/type support. Multi-storage fields add one more choice: a scalar field can have a default SQL placement and an explicit duplicated placement on a vector backend.

## Decision

Keep `near` as the compiled backend operation. Do not add a separate public `near_filter` operator. The router may attach a filter fragment to the `near` fragment payload and notes; the vector backend then decides whether to push that filter down natively or use its backend-local row predicate fallback.

`Query.explain()` reports one of these `near_filter_mode` values on the `near` step:

| Mode | Meaning |
|------|---------|
| `none` | No scalar filter is present. |
| `combined_native` | The whole filter tree and `near` use the same backend, and both fragments compile to native provider payloads. |
| `combined_scan` | The whole filter tree and `near` use the same backend, but at least one fragment uses backend-local row predicates. |
| `metadata_prefilter` | Filter fields have explicit duplicated storage placements on the vector backend, and the router chooses the vector metadata copy for this near query. |
| `candidate_id_prefilter` | Filters stay on other authoritative backends; HeavenBase first computes a bounded candidate ID set and passes `object_id IN (...)` into the vector search. |
| `post_filter` | The vector backend cannot take the filter, so HeavenBase runs near and filters separately and intersects frames by `object_id`. |
| `exact_scan` | A `near(..., exact=True)` query has no proven native near/filter pair, so HeavenBase scores every logical row the complete filter admits. |
| `unsupported` | At least one filter clause cannot compile on its authoritative storage binding; `execute()` raises `LookupError`. |

The execution preference order is:

1. Same-backend combined near/filter.
2. Duplicated-storage metadata prefilter on the vector backend.
3. Candidate-ID prefilter when the near backend advertises `candidate_id_prefilter`.
4. Post-filter intersect fallback.

Backend capabilities are exposed through `backend.capabilities()` and published in workspace health / MetaSchema backend payloads. The current candidate-ID native backend set is LanceDB and Milvus. The candidate set is bounded by `CM_HVNB.get("heavenbase.query.near.candidate_prefilter.max_ids", default=4096)`; when the filter result exceeds the bound, execution falls back to `post_filter`.

## Provider Rules

PgVector: prefer one SQL statement when scalar filters and vector fields share PgVector/Postgres storage. Cross-backend filters use `post_filter` until SQL Identifier membership semantics are concretely proved; an unproved `object_id IN (...)` can admit collation false positives that consume bounded top-k slots.

LanceDB: prefer native `where(...)` metadata filters before vector search. A metadata copy is eligible only when every filter field has an explicit LanceDB storage placement and compiles to a `lance_where` fragment.

Milvus: prefer native boolean filter expressions before vector search. Candidate IDs compile as an `object_id in [...]` Milvus filter.

Chroma/Pinecone: same-backend metadata filters can be combined with `near`. Candidate-ID prefilter is not advertised yet because the current adapters do not store `object_id` as metadata in a provider-neutral way.

Mixed SQLite + LanceDB: if filter fields do not have explicit LanceDB storage placements, SQLite remains the default filter binding. HeavenBase computes bounded candidate IDs, passes them to LanceDB, executes vector search over those candidates, then intersects with the SQLite filter frame to preserve columns and correctness.

Metadata-copy case: explicit multi-storage order still matters. Normal scalar-only filters use the first effective binding. A `where + near` query may choose the vector binding only when all filter clauses have explicit storage placements on the near backend and compile natively there. This keeps metadata prefiltering opt-in through field storage.

## Consequences

- Backends still execute only `QueryFragment`s; they do not parse `QuerySpec`.
- `near` remains the only vector search operation exposed to handlers and providers.
- Metadata prefiltering is conservative and opt-in. It does not infer hidden copies.
- Candidate-ID prefiltering is bounded to avoid large unchunked vector-provider filter payloads.
- Broader duplicate-storage pruning remains future router work.
