# ADR 0003 - Lineage Extension (Design)

Status: proposed

Date: 2026-06-10

## Problem Statement

Enterprise and agent-assisted analysis workflows need provenance: which upstream asset produced a row, what transform ran, and which version is current. HeavenBase already exposes lineage-shaped fields on `sys-catalog` (`source`, `parent_id`, `history`, `transforms`) and documents them in [id-semantics.md](../../reference/id-semantics.md), but nothing populates them today. The `memory` extension's version chain (`previous_id`, soft delete) solves note history for one entity; it is not a general asset graph. The prototype `database` extension makes schema rows browsable but does not record ingest or snippet execution lineage.

Without a designed extension, lineage will reappear as ad-hoc metadata on entity rows, duplicate version logic per extension, or a second graph runtime inside the workspace — all violations of the mental model.

## Philosophy Gate

| Decision | Rationale | This ADR |
|----------|-----------|----------|
| Extensions are entity bundles | Lineage is rows (`lin-*`), not a planner or scheduler | Compliant: four entities, optional `enable_extension("lineage")` |
| Register, never branch | Capture and exposure plug in through registry seams | Compliant: names only existing + one proposed `record_lineage` helper |
| Minimal mental model | One obvious way to record and query provenance | Compliant: explicit `record_lineage`; Catalog derives for discovery |
| Break and fix, no shims | No parallel lineage APIs or `v1/v2` capture shapes | Compliant: single entity sketch, no compatibility layer |
| `explain()` stays honest | Lineage does not alter query routing | Compliant: lineage is metadata rows, not handler branches |
| Design-only this train | Prototype pass stops at ADR | Compliant: no implementation in 0.1.1 |

## Entity Sketch

All entities follow the one-`object_id` rule. Cross-references are plain `Identifier` fields. Entity ids use the `lin-` prefix.

### `lin-asset` (`LinAsset`)

Registered data artifact — the node in the lineage graph.

| Field | Type | Notes |
|-------|------|-------|
| `object_id` | `Identifier` | PK |
| `name` | `ShortText` | Human label |
| `asset_kind` | `ShortText` | `entity_row`, `db_table`, `db_snippet`, `file`, `capsule`, `other` |
| `target_entity` | `Identifier` | Entity type id when `asset_kind=entity_row` |
| `target_id` | `Identifier` | Row id the asset points at |
| `fingerprint` | `ShortText` | Optional content hash or schema hash |
| `desc` | `MediumText` | Card/search text |
| `metadata` | `JSON` | Opaque host tags (environment, dialect, path) |

### `lin-edge` (`LinEdge`)

Directed relationship between two assets.

| Field | Type | Notes |
|-------|------|-------|
| `object_id` | `Identifier` | PK |
| `source_asset_id` | `Identifier` | Upstream `lin-asset` |
| `target_asset_id` | `Identifier` | Downstream `lin-asset` |
| `edge_kind` | `ShortText` | `derived_from`, `contains`, `references`, `executed_by`, `version_of` |
| `run_id` | `Identifier` | Optional `lin-run` that created this edge |
| `desc` | `MediumText` | Optional note |

### `lin-run` (`LinRun`)

One execution that produced or updated lineage (ingest job, agent tool batch, ETL step).

| Field | Type | Notes |
|-------|------|-------|
| `object_id` | `Identifier` | PK |
| `run_kind` | `ShortText` | `ingest`, `transform`, `query`, `agent_tool`, `manual` |
| `status` | `ShortText` | `running`, `succeeded`, `failed`, `cancelled` |
| `started_at` | `Timestamp` | Epoch ms |
| `ended_at` | `Timestamp` | Epoch ms; optional while running |
| `actor` | `ShortText` | User, agent session id, or host process label |
| `input_asset_ids` | `Array[Identifier]` | Inputs |
| `output_asset_ids` | `Array[Identifier]` | Outputs |
| `error` | `MediumText` | Redacted failure summary |
| `metadata` | `JSON` | Host correlation ids |

### `lin-version` (`LinVersion`)

Version snapshot for an asset (chain semantics aligned with `memory-note`, generalized).

| Field | Type | Notes |
|-------|------|-------|
| `object_id` | `Identifier` | PK |
| `asset_id` | `Identifier` | Parent `lin-asset` |
| `version` | `ShortText` | Label (`1`, `2026-06-10`, semver) |
| `previous_id` | `Identifier` | Prior `lin-version` in chain |
| `fingerprint` | `ShortText` | Snapshot content hash |
| `active` | `Bool` | Current head when true |
| `created_at` | `Timestamp` | Materialized on write |

```text
lin-asset ──version_of──> lin-version (chain via previous_id)
lin-asset ──derived_from──> lin-asset   (via lin-edge)
lin-run   ──executed_by──> lin-edge     (run_id on edge)
```

## Seam Analysis

### Extension bundle

| Seam | Role |
|------|------|
| `Extension("lineage", entities=...).register()` | Registers `lin-asset`, `lin-edge`, `lin-run`, and `lin-version`; HeavenBase-owned bundles join `extensions/bootstrap.py` only when shipped as built-ins. |
| `ws.enable_extension("lineage")` | Same rule as `agent` / `database`: classes import anywhere; rows require enablement |

### Catalog convergence

`sys-catalog` already reserves lineage fields. The extension should populate them through **entity field derives**, not a parallel index:

| Catalog field | Populate from |
|---------------|---------------|
| `source` | Nearest upstream `lin-asset` name or id via `derived_from` edge |
| `parent_id` | `lin-version.previous_id` or upstream asset id |
| `history` | Ordered list of `lin-version` ids or compact event dicts for the target row's asset |
| `transforms` | `lin-run` summaries linked through edges where `edge_kind=executed_by` |

Projection rules stay on the **target entity's** Catalog row (via `target_id` / `target_entity`), matching [id-semantics.md](../../reference/id-semantics.md). Lineage entities are also Catalog-indexed for `ws.search(card_text=...)`.

### Registry seams (existing)

| Seam | Lineage use |
|------|-------------|
| `register_profile` | `lineage` profile: `tools: [read, query]`, `entities: ["lin-*"]`, `serializer: markdown` for graph-friendly tables |
| `ToolSerializer.register` | Optional `lineage` serializer for edge-list / version-chain display in MCP |
| `register_extension` | Primary registration path |
| `Handler(...).register(ws.handlers)` | Only if lineage-specific query ops are needed later; default is DSL + `query_json` on `lin-*` rows |

### Proposed public write seam

| Symbol | Kind | Notes |
|--------|------|-------|
| `hb.ext.record_lineage` | function | `record_lineage(ws, *, run_kind, inputs, outputs, edges=..., actor=...)` upserts `lin-*` rows in one call; mirrors `ingest_database` ergonomics |

`record_lineage` is the **only** recommended capture API. Automatic hooks (workspace upsert interceptors) are out of scope for v1; callers such as `ingest_database`, snippet runners, or agent hosts invoke it explicitly.

### Integration touchpoints (future, not v1 code)

- `database` extension: optional `record_lineage` after `ingest_database` and successful `run_snippet`
- `agent` extension: optional linkage from `agent-session` / `agent-message` rows to `lin-run.actor`
- `memory` extension: remains separate; do not fold Mem0-style notes into `lin-version`

## Explicit Non-Goals

- **No automatic capture of every workspace write in v1.** Callers opt in through `record_lineage`.
- **No column-level lineage in v1.** Asset-level granularity only (`entity_row`, `db-table`, snippet).
- **No graph database backend or Gremlin/SQL graph DSL.** Lineage queries use the normal workspace query surface.
- **No OpenLineage, Marquez, or DataHub protocol adapters in v1.** Export can be a later Capsule or host job reading `lin-*` rows.
- **No scheduler or workflow execution.** Runs are records; execution stays in host frameworks (see ADR 0004).
- **No changes to query routing or `explain()`** for lineage-aware pushdown.

## Decision

Defer implementation to a post-prototype train. When built, `lineage` is an optional extension under `src/heavenbase/extensions/lineage/` contributing four `lin-*` entities, a `lineage` MCP profile, and `hb.ext.record_lineage` for explicit capture. Catalog `source`, `parent_id`, `history`, and `transforms` derive from `lin-*` rows rather than ad-hoc entity metadata.

## Exit Criteria for Promotion to a Plan

Promote this ADR to an execution plan (mid-term roadmap) when **all** of the following hold:

1. **Demand signal:** At least one shipped consumer (likely `database` ingest/snippet path or an enterprise demo) needs provenance queries agents can run without custom glue.
2. **Catalog contract frozen:** Field derives for `history` and `transforms` are written in the plan with example rows and MCP `query_json` specs; reviewers agree they do not collide with `memory-note` version semantics.
3. **Seam review passed:** `record_lineage` signature and `register_profile("lineage")` are listed in the API standard table; no new central planner branches are required.
4. **Test anchor named:** Plan includes `tests/test_lineage_extension.py` covering enablement, `record_lineage` graph shape, Catalog derive population, and profile-scoped MCP read.
5. **Non-goals reconfirmed:** Stakeholders accept explicit capture (no background daemon) for v1.

## Consequences

- Prototype pass and `database` v1 ship without lineage capture; `history` / `transforms` on Catalog remain empty defaults until enablement.
- Enterprise roadmap item "promote lineage ADR to plan" stays gated on database-extension usage feedback ([roadmap.md](../../../goals/roadmap.md)).
- Future implementation must not add a second version-chain mechanism; reuse `lin-version` patterns established here.
