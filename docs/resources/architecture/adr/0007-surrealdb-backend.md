# ADR 0007 - SurrealDB multi-model backend

Status: accepted

Date: 2026-06-27

Amended: 2026-07-22 to reflect ADR 0011's ordinary Backend/handler record cutover; 2026-07-15 to align SurrealDB's dual graph representation with the strong-reference lifecycle accepted in [ADR 0005](0005-graph-edge-capability.md) for [GitHub issue #14](https://github.com/Magolor/HeavenBase-legacy/issues/14).

## Problem Statement

SurrealDB is a multi-model store: one engine does document/relational rows, graph relationships
(`RELATE`), HNSW/MTREE vector search, and BM25 full-text. HeavenBase wants it as a backend so an
entity can live in SurrealDB with row CRUD + filtering today, and (later) vector / full-text / graph
on the same store. The open question was *how* it attaches.

The tempting framing — "make it the 11th SQL backend, a thin dialect like `sqlite`/`postgres`" — does
not work. Those ten are ~40-line subclasses only because SQLAlchemy already ships a dialect for each:
the subclass is a label on a pre-built engine. SurrealDB has **no SQLAlchemy dialect**, speaks
**SurrealQL (not standard SQL, no Postgres wire protocol)**, and its Python driver is an RPC/JSON
client, not a PEP-249 DBAPI. Forcing it onto the SQLAlchemy path would mean writing an entire
SQLAlchemy dialect + DBAPI shim for a non-SQL database — large, leaky, and it would discard exactly the
graph/vector/full-text capabilities that motivate using SurrealDB.

Without a designed seam, SurrealDB support would reappear as a bespoke planner branch, or as a
multi-model store crammed through a relational-only pipe.

## Philosophy Gate

| Decision | Rationale | This ADR |
|----------|-----------|----------|
| Register, never branch | A backend is a registered family member, not a planner special-case | Compliant: ordinary `backend_type` and `handler` records own SurrealDB's class, aliases, families, and compiler locators; consumers resolve those records without a provider list or `if backend == "surreal"` branch |
| Minimal mental model | Same `storage_row` job as every row store; no SurrealDB-only surface | Compliant: it is selected like any backend; reuse `.where`/`.order_by`/`.limit`; native scalar filters compile to SurrealQL, everything else degrades to the inherited scan |
| Any op degrades to scan | Capability is never backend-gated | Compliant: `SurrealBackend(Backend)` pushes the parity-safe subset to a `surreal_where` SELECT and delegates the rest to `Backend.execute()` for portable Python scan; a tree with a non-native leaf composes through `QueryFragment.execute` |
| `explain()` stays honest | Routing must be inspectable | Compliant: native filters report `handler_mode == "native"`, scan-only ops report `py_scan` — both verified live against the embedded engine |
| Storage stays explicit, opt-in | No hidden cross-backend capture | Compliant: SurrealDB is used only when configured / `.store`-targeted; it is never the auto-preferred default, so existing field placement is unchanged |
| Break and fix, no shims | One live API, no SQL-emulation layer | Compliant: native SurrealQL via the official `surrealdb` driver; no SQLAlchemy dialect, no parallel API |

## Decision

- **Standalone native backend, not a SQL dialect.** `SurrealBackend` (`backends/surreal/backend.py`)
  talks to SurrealDB through the official `surrealdb` Python driver and emits SurrealQL directly —
  modelled on `neo4j`/`redis` (the existing native non-SQLAlchemy row stores), **not** on the SQL
  family. It does **not** join `BACKEND_FAMILY_SQL`; it does not touch SQLAlchemy.
- **`storage_row` first; multi-model by growth.** P0 registers `families=(handler, storage_row)` and
  does the full row job: `ensure` (`DEFINE TABLE … SCHEMALESS` + per-field `DEFINE INDEX`), `upsert`
  (`UPSERT type::thing(table, id) CONTENT …`, full-row replace; `DELETE` for removals), `scan`, native
  `get_many`/`exists_many`/`count`, and native `WHERE` pushdown (`handlers/surreal.py`) for the
  parity-safe scalar subset (`eq`/`in` on any indexable scalar; `lt`/`lte`/`gt`/`gte` on ordered
  scalars) with a portable scan fallback for the rest. Later phases widen the same backend's family
  tuple and add handlers, each verified on the embedded engine:
  - **P1 `storage_vector`** — `DEFINE INDEX … HNSW`, native cosine KNN via the index-backed `<|k,ef|>`
    operator (the metric-keyword `<|k,COSINE|>` form is a brute-force table scan even when the index
    exists — verified via `EXPLAIN` — so it is used only for a dimensionless vector that has no index) +
    `vector::distance::knn()` (cosine distance → similarity), degrading to the metric-matched scan for a
    non-cosine metric or a near co-located with a filter.
  - **P2 `storage_text`** — native substring/affix text ops (`contains`/`match`/`starts_with`/`ends_with`)
    push to SurrealDB **string functions** (`string::contains`/`starts_with`/`ends_with`, NONE-guarded),
    which match HeavenBase's substring predicates exactly. SurrealDB's BM25 `@@` is a **token** match, not
    a substring, so wiring it would break native==scan parity; the `search`/BM25 family is therefore
    deliberately **not** added (a future token full-text op could revisit this). `like`/`wildcard`/`regex`
    stay on the scan.
  - **P3 graph (GraphEdge)** — inline-canonical storage plus a `RELATE` shadow, joining the `graph` family. A `GraphEdge` HyperG field is
    stored **both** inline (its mini-rows round-trip through scan/get like any document field, so `ws.get`
    and repair audits keep working) **and** materialized as SurrealDB edge records (`insert_relation` with
    `in`/`out` record links + scalar edge props), synced as a full-list replace per source on upsert, with
    parallel edges rejected. Workspace mutation preflight requires real endpoints and applies the field's
    `restrict`/`detach` target-deletion policy before the backend executes one provider-local transaction;
    the inline value and native relation projection are therefore rewritten together. When a cross-entity
    target's authoritative fields live on another route, SurrealDB uses `UPSERT ... MERGE` to create a
    typed, id-only endpoint anchor before `RELATE`. Declaring this anchor mode requires merge-only creation
    that preserves a co-located row and idempotent deletion without prior target `ensure`. The anchor is
    non-authoritative under the coherent `StoragePlan` that created it, never enters the target plan, and
    is deleted with the logical target. Route migrations must remove or rewrite obsolete anchors before
    the new plan is coherent.
    Target deletion or later recreation must not resurrect an old logical edge.
    Provider-level `graph_traverse[_refs]` / `graph_shortest_path[_refs]` run a BFS whose
    per-hop frontier expansion is one native arrow query (`->edge->tbl.object_id` / `<-edge<-`), so a
    typed reference set handles cross-entity edges + `out`/`in`/`any`. These methods verify the derived
    projection, but workspace traversal remains `py_traverse` over the canonical inline value. An
    unversioned projection cannot prove that pre-refactor rows were rebuilt, so it is not a public read or
    delete-discovery authority. `ws.check_graph()` and confirmed
    scrub repair mergeable logical edge values whose targets are missing; they neither choose between
    conflicting routed replicas nor detect every inline/native projection mismatch while a target still
    exists. Target deletion exhaustively scans every physical owner of the logical GraphEdge field, then
    hydrates and rechecks candidates before mutation. A source rewrite synchronizes its inline value,
    anchors, and relations transactionally; stale or missing shadow relations cannot change public
    behavior. A future versioned readiness-and-rebuild contract may safely restore native public routing.
    (A pure rebuild-from-edges
    scan — dropping the inline copy — is a later space optimization; the `exists` edge filter stays on the
    portable scan.)
- **One record per object, addressed by `type::thing`.** The logical `object_id` is also stored as a
  field, so reads never depend on SurrealDB's record-id coercion; tables are `SCHEMALESS`. `Json`/`Array`/
  `HyperG` arrive workspace-encoded as JSON strings and are decoded back to native dicts/lists on read
  (mirrors the neo4j `_decode_node` contract), so scan-filter predicates and the GraphEdge portable
  traversal see native values, not strings.
- **Workspace isolation by database.** Every `(instance name, workspace)` pair maps to its own
  SurrealDB `DATABASE` under a shared `NAMESPACE`, so two SurrealDB backends sharing a server stay
  isolated when their normalized provider names are distinct. Physical names (table, database) use the
  backend helper's transparent separator normalization (`-` / `_` to the provider separator). HeavenBase
  deliberately does not hash, truncate, or otherwise guarantee collision-free provider names; users are
  responsible for choosing logical ids that stay portable across the backends they operate directly.
- **Embedded-first testing.** The `surrealdb` wheel ships an in-process engine, so the integration
  tests run against `mem://` in the fast suite with no docker; they skip when the driver is absent. A
  `ws://` server path remains available for server-only features. The driver is an optional `full`
  extra; an absent driver or unreachable server degrades to `client=None` and raises lazily.
- **Out of scope (non-goals):** the BM25 `search` family (`@@` is a token match, not a substring, so
  it would break native==scan parity — full-text is served via `string::*` substring pushdown instead);
  joining the `sql` family or any SQLAlchemy dialect (rejected above); federation — SurrealDB is a
  storage engine, not a query engine over external databases (existing Postgres data must be migrated
  in, not read in place, unlike `trino`). *(Vector / full-text / graph families were initially deferred
  past P0 but are now all implemented — see the Decision section.)*

## Why not a SQL dialect (recorded so the question is not re-litigated)

`sql` and `storage_row` are different families. The ten SQL backends carry both, but "doing the row
job" is the `storage_row` half — which `redis`, `inmem`, and `neo4j` also serve without being `sql`.
`sql` specifically means "reached through SQLAlchemy", which requires a SQLAlchemy dialect for the
database. SurrealDB has none and is not SQL-wire-compatible, so it serves `storage_row` natively
(its own driver + handler), exactly like `redis`/`neo4j`. Having its own `handlers/surreal.py` is the
standard non-SQLAlchemy pattern, not a special-case.

## Routing & Explain Contract

This honours `routing-and-explain.md`: storage is explicit (rows live where the field is bound),
the backend executes `QueryFragment`s, native filters carry the portable `row_payload` predicate so
mixed boolean trees compose native and scan leaves, and `explain()` labels each filter `native` vs
`py_scan`. GraphEdge traversal honestly reports `py_traverse`; direct RELATE methods are projection
verification helpers, not routed execution. Universality ("any op degrades to scan") holds because `Backend.execute()` delegates to
the scanning module for every op not pushed down.

## Consequences

- HeavenBase gains an opt-in multi-model store: today a row-storage peer to the ten SQL backends
  (different vehicle — native SurrealQL, not SQLAlchemy), and a clean additive path to graph/vector/
  full-text on the same backend.
- The capability matrix (`capabilities.md`) is regenerated from the live handler registry and now
  carries a `surreal` column. Like `neo4j`/`redis`, `surreal` is not added to `TESTED_BACKENDS`
  (the allowlist that promotes cells to verified marks), so its cells report the registered surface
  rather than verified pushdown.
- Pinned to `surrealdb>=2.0` (SurrealQL `DEFINE INDEX … HNSW`, KNN `<|k,ef|>` / `<|k,DIST|>`, `string::*`
  pushdown, `RELATE`); a future SDK/server major may move the DDL keywords, so the driver version is held
  in `requirements-full.txt`.
- SurrealDB transactionality is provider-local. The workspace lock serializes calls within one live
  workspace instance, but independently routed backends do not form a distributed transaction and
  cross-process serialization remains backend-specific. After a failed multi-backend fan-out, quiesce
  writers and converge conflicting routed fragments with one authoritative valid full-row upsert before
  graph audit/retry; dangling scrub does not replay the failed intent.
- `decimal` vs `float` and `datetime`/`duration` round-trip as native SurrealDB types; for an entity
  co-owned with a SQL backend (which stores timestamps as integers), value identity across backends is
  a known follow-up if cross-backend co-ownership of those columns is ever used.
