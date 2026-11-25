# ADR 0005 - Graph Edge Capability (GraphEdge + traversal)

Status: accepted

Date: 2026-06-24

Amended: 2026-07-15 for [GitHub issue #14](https://github.com/Magolor/HeavenBase-legacy/issues/14). The current implementation contract is documented here and protected by the GraphEdge lifecycle, backend, MCP, audit, and repair tests.

## Problem Statement

Knowledge-base and agent workflows need real relationships between records — citations, authorship,
dependency, lineage-of-data — and the graph operations over them (neighbours, multi-hop reach,
shortest path). HeavenBase already stores a record's edge list as a `HyperG` field, but on every
backend that is an opaque JSON blob: you can read it back, but you cannot ask "who cites d2", "what is
reachable from d1 within 3 hops", or "is there a path d1 → d9". The Neo4j backend (a normal
entity/row + vector store) sits on a graph engine that answers exactly these questions natively, yet
nothing wired that capability through.

Without a designed capability, graph features would reappear as a bespoke graph DSL bolted onto the
query surface, a second graph runtime inside the workspace, or backend-gated operations that raise on
non-graph stores — each a violation of the "one mental model / any op degrades to scan" invariants.

The first implementation also exposed a lifecycle inconsistency: a physical Neo4j target delete
removed inbound relationships, while inline row stores retained source-owned mini-rows and SurrealDB
could retain an inline value after its native relation disappeared. Reads, traversal, edge filters,
repair, and target recreation could therefore disagree by backend. A storage engine's native delete
behavior cannot define the public workspace contract.

## Philosophy Gate

| Decision | Rationale | This ADR |
|----------|-----------|----------|
| Register, never branch | A graph edge is a storage *strategy* + a backend that honours it | Compliant: `GraphEdge` is a registered `StorageStrategy`; neo4j joins `BACKEND_FAMILY_GRAPH` and registers a handler — no `if backend == "neo4j"` branches |
| Minimal mental model | Edges are just a `HyperG` field with `.store(strategy=GraphEdge)`; no new graph DSL | Compliant: reuse `.where`/`.order_by`/`.limit`; traversal is one routed op + two delegate methods |
| Exhaustive fallback or fail closed | Graph ops must not silently omit provider state | Compliant: readiness-trusted Neo4j runs native Cypher; every other backend scans every physical edge owner to exhaustion and raises when completeness cannot be proven |
| `explain()` stays honest | Routing must be inspectable | Compliant: `explain()` reports `native_traverse` with its one executing backend or `py_traverse` with every scanned physical owner, plus `native` vs scan for the edge filter, without executing traversal |
| Storage stays explicit | No hidden cross-backend capture | Compliant: an edge is materialised only on declared field routes; workspace preflight, traversal, audit, and rewrites include every physical owner, including mixed-strategy replicas |
| Break and fix, no shims | No parallel graph API or `v1/v2` edge shapes | Compliant: a single `GraphEdge`/Model A design; the delegate methods and the routed op share one resolver |

## Decision

- **Edge = `HyperG` field with `.store(strategy=GraphEdge(target="col", on_delete="restrict"))`** (Model A: the record is the
  source; the `target` column is the endpoint → target node, the remaining columns are edge properties,
  the field name is the relationship type). `target` names the endpoint explicitly — required when the
  endpoint is a plain `Identifier` or when more than one `Identifier` column is present; omit it (bare
  `GraphEdge`) to auto-detect the sole `Identifier[Entity]` column. The target entity comes from the
  endpoint's `Identifier[Entity]` scope (a subscripted logical type carrying the entity as schema-only
  metadata; storage keeps the bare `object_id`), else defaults to the owning entity (a self-ref edge).
  `on_delete` is a closed schema policy: `restrict` is the default and `detach` is the only alternative.
- **GraphEdge is a strong workspace reference on every backend within one live workspace's mutation
  boundary.** An upsert or set containing an edge is accepted only when each endpoint already exists or
  is included in the same accepted logical batch. Validation is workspace-owned, batched by target
  entity, and completes before entity/Catalog row mutation. A provider must not create placeholder
  logical targets to weaken this contract. After validation, a native record-graph provider may create a
  typed, id-only endpoint anchor as a non-authoritative projection when the real target row lives on
  another route. Direct backend writes, external database mutations, and concurrent writes through
  another process remain outside this serialization guarantee.
- **GraphEdge values are endpoint-keyed sets.** The public container is a `HyperG` mini-row list, but
  relationship identity is `(source, field, target)`, parallel edges are rejected, and list order is not
  semantic. Replicated routes therefore compare edge mini-rows by endpoint and properties, not position.
- **Target deletion has one explicit policy surface.** `ws.delete(..., edges="schema")` and
  `ws.delete_many(..., edges="schema")` use each incoming field's `GraphEdge.on_delete` policy.
  `edges="restrict"` forces safe restriction for every incoming field; `edges="detach"` removes matching
  edge mini-rows from surviving sources before deleting the target. Restriction raises
  `hb.GraphDeleteRestrictedError`, whose deterministic `references` records identify `entity`, `field`,
  `source`, `target_entity`, and `target`. In schema mode, any restricted incoming field aborts the whole
  batch before detach rewrites are applied. A restricted batch performs no entity/Catalog row mutations.
- **The source owns its outgoing edge values.** Deleting a source removes its outgoing edges with the
  source row. An edge whose source is also in the same `delete_many` batch does not restrict that batch;
  this covers self-loops and cycles wholly contained in the batch. Detach never deletes neighbouring
  nodes, and recursive entity cascade is not part of `GraphEdge`. Duplicate delete targets are planned
  once while caller-order boolean results are preserved; an absent target remains a false/no-op.
- **Backends implement, but do not choose, the semantics.** Neo4j materialises real relationships;
  SurrealDB keeps a canonical inline value plus a derived `RELATE` shadow projection; file, memory, and
  SQL row stores keep source-owned values. From a coherent logical pre-state, a successful GraphEdge
  mutation keeps its affected closure consistent across `get`, edge `exists`, traversal, and stored
  logical rows. A source rewrite also synchronizes provider projections for that source, and target
  deletion cleans incident projections. Recreating a detached target cannot resurrect an old edge. A
  graph backend is resolved through
  `BACKEND_FAMILY_GRAPH`. Neo4j and SurrealDB declare `identity_anchor` endpoint materialization for
  split cross-entity routes. That declaration requires merge-only anchor creation, preservation of any
  co-located full row, and idempotent unensured-target deletion in the same provider batch. Anchors stay
  outside logical storage routes and are deleted with the logical target under the coherent `StoragePlan`
  that created them. Route migrations must remove or rewrite obsolete anchors before the new plan is
  coherent.
- **Operations, all surfaced through `ws.`:** the 1-hop `exists` edge filter (native relationship
  `EXISTS` on neo4j, portable scan elsewhere); `ws.traverse` / `ws.shortest_path` delegate methods; and
  a routed `ws.query(E).traverse(field, start, …).where(…)` op that folds the reachable set into the
  query filter so it composes with `.where`/`.order_by`/`.limit`.
- **Portable fallback (never backend-gated):** because the edge list is readable on every backend, a
  non-graph backend answers traversal with a Python frontier-expansion / BFS over the scanned edge
  lists. SurrealDB also uses this canonical path publicly: its unversioned `RELATE` shadow cannot be an
  authority for migrated workspaces. A readiness-trusted native traversal is eligible only when the
  logical field has one physical owner; replicated fields use the exhaustive portable merge so replica
  conflicts fail closed and `explain()` remains honest. A hard `max_hops` ceiling (config
  `heavenbase.query.graph.max_hops`) bounds runaway traversal.
- **Audit and scrub are dangling-reference repair tools.** `ws.check_graph()` detects logical edge values
  whose targets are missing; `ws.scrub_dangling(confirm=True)` removes those values. A field-scoped scrub
  selects source rows through that field and repairs every dangling GraphEdge on each selected row. They
  cover legacy rows, direct backend target removal, and mergeable dangling rows after an apply error.
  They do not replay failed intent, choose between conflicting replicas, prove arbitrary provider health,
  or detect every native-projection mismatch while a target still exists.
- **Concurrency and atomicity are bounded honestly.** The workspace lock serializes public mutations
  within one live workspace instance. Source rewrites and target deletes sent to one transactional
  backend form one provider-local mutation batch. Independently routed backends do not form a
  distributed transaction. A failed fan-out may leave physical fragments or projections partial or
  divergent. Automatic union/intersection would guess at user intent, so recovery requires quiescing
  writers, reconstructing one authoritative valid full row, upserting it across declared routes, repairing
  Catalog metadata if needed, then rerunning graph audit/delete. Endpoint preflight is not a cross-process
  compare-and-set, so another writer can race unless the provider supplies stronger serialization.
- **Out of scope (deferred):** two-endpoint reification (Model B), true hyper-edges (3+ endpoints), a
  mixed-target single edge field (e.g. an `Identifier[A,B]` union or per-row endpoint type — one edge
  field points at exactly one entity for now), symmetric auto-mirrored edges, and reference-as-edge (Path B).

## Relationship to ADR 0003

ADR 0003 (Lineage Extension) lists "a second graph runtime inside the workspace" as a non-goal. That
constraint is **scoped to lineage**: lineage is provenance metadata recorded as ordinary `lin-*` rows
with plain `Identifier` cross-references, and must not grow its own graph engine. It does **not**
forbid a storage backend from exposing the graph capability its engine already has. The global
`backend-strategy-handlers.md` lists `graph` as a supported backend type, and this capability is a
normal new-backend extension (a strategy + a family + a handler), not a planner or a parallel runtime.
This ADR records that distinction so the 0003 non-goal is not misread as a global "no graph" ban.

## Routing & Explain Contract

This honours `routing-and-explain.md`: storage is explicit (edges live where the field is bound),
backends execute `QueryFragment`s, the edge filter degrades to scan when it cannot push down, and
`explain()` is the source of truth — the routed traverse adds an honest plan step, the edge filter is
labelled `native` vs scan, and a routed traverse over a cross-entity edge is rejected in both
`execute()` and `explain()`. Because every op has a portable fallback, universality ("any op degrades
to scan") is preserved.

## Consequences

- Neo4j becomes a genuine graph store for HeavenBase, not just a row/vector store; the same workspace
  API yields native graph performance when a capability is complete and readiness-trusted (currently
  Neo4j), and exhaustive portable results everywhere else.
- Edge properties are restricted to scalar types (a Neo4j relationship property cannot hold a nested
  map / heterogeneous list / vector); such data belongs on the node.
- The default restriction prevents accidental reference loss. Schema-level `detach` makes intentional
  lifecycle coupling declarative, while the delete override supports an explicit one-off decision. Code
  that intentionally needs weak references should use an ordinary `Identifier` or non-`GraphEdge`
  `HyperG` field instead of relying on backend-specific dangling behavior.
- Restrict/detach preflight uses Neo4j's backend-declared native incoming-reference lookup. Every other
  physical owner of the logical GraphEdge field—including a replica stored through another physical
  strategy—uses one focused exhaustive scan per affected source entity, followed by full hydration of
  candidate ids. `Backend.scan()` is an all-rows contract: Elasticsearch scrolls every page and Milvus
  consumes its query iterator rather than treating a page-size tunable as a result cap. Completeness also
  includes freshness within the serialized workspace history, so Milvus integrity scans and point lookups
  force Strong consistency rather than inheriting bounded staleness. Native lookup
  returns a complete source-id superset; the workspace always rechecks hydrated canonical values.
- The graph is not required to be connected — orphan nodes and multiple components are valid; deleting a
  node removes that node and its outgoing edges, subject to incoming restrict/detach policy. Node-to-neighbour
  cascade is intentionally not provided (it would be meaningful only for ownership/containment edges,
  which are out of scope here).

## Upgrade checklist

1. Before deploying strong lifecycle semantics, run `ws.check_graph()`, inspect the dry-run
   `ws.scrub_dangling()`, then confirm repair while existing routed fragments are still mergeable.
2. Review every `GraphEdge`: an omitted policy now means safe `on_delete="restrict"`; declare
   `on_delete="detach"` only where target removal should erase surviving relationship values.
3. Remove `Neo4jBackend(graph_on_missing=...)`. Missing logical endpoints are rejected; validated
   split-route endpoints are projected automatically as non-authoritative identity anchors.
4. If an earlier multi-backend apply failed and replicas conflict, quiesce writers and issue one
   authoritative valid full-row upsert before graph audit/retry. Scrub does not choose a winning replica.
5. No SurrealDB projection rebuild is required for public correctness: workspace traversal and delete
   discovery use the canonical inline value. `RELATE` remains a synchronized shadow for affected source
   writes and provider-level conformance checks until a future versioned readiness gate can safely route
   public reads through it.
