# ADR 0004 - Automation Extension (Design)

Status: proposed

Date: 2026-06-10

## Problem Statement

Operators want declarative reactions to workspace events: re-ingest after a seed file changes, run a readonly snippet when an agent registers a predicate, or kick off an ETL Capsule on a schedule. HeavenBase stores an `automations` reference list on `sys-catalog` ([id-semantics.md](../../reference/id-semantics.md)) but has no entity model, no trigger definitions, and no run history. Agent MCP logging (prototype Slice 5) records what happened; it does not define what should happen next.

Embedding a job scheduler inside the workspace would violate the extension invariant ("must not introduce a second runtime, planner, or config system") and duplicate host-framework schedulers teams already operate (cron, Airflow, GitHub Actions, Kubernetes CronJob, MCP host loops).

## Philosophy Gate

| Decision | Rationale | This ADR |
|----------|-----------|----------|
| Extensions are entity bundles | Automation is stored definitions + run records, not a daemon | Compliant: three `auto-*` entities, optional enablement |
| Register, never branch | Triggers and workflows register like other extensions | Compliant: `register_extension`, `register_profile` only |
| Minimal mental model | Host fires triggers; HeavenBase stores intent and outcomes | Compliant: no in-process scheduler |
| Break and fix, no shims | One workflow row shape; no dual trigger APIs | Compliant: single sketch |
| Profiles describe exposure | Agents see only automation rows the profile allows | Compliant: `automation` / `automation-admin` profiles |
| Design-only this train | Prototype pass stops at ADR | Compliant: no implementation in 0.1.1 |

## Entity Sketch

All entities use one `object_id`. Entity ids use the `auto-` prefix.

### `auto-trigger` (`AutoTrigger`)

When and on what scope a workflow should be considered for execution. **HeavenBase stores the definition; a host framework evaluates and fires it.**

| Field | Type | Notes |
|-------|------|-------|
| `object_id` | `Identifier` | PK |
| `name` | `ShortText` | Human label |
| `trigger_kind` | `ShortText` | `schedule_ref`, `row_upsert`, `row_delete`, `webhook_stub`, `manual` |
| `entity_scope` | `ShortText` | Entity type id or `*` |
| `filter_json` | `JSON` | Mongo-style predicate for row events; ignored for `schedule_ref` |
| `schedule_ref` | `ShortText` | Opaque host cron/interval id (e.g. `airflow:daily_ingest`); not interpreted by HeavenBase |
| `workflow_id` | `Identifier` | Target `auto-workflow` |
| `enabled` | `Bool` | Host should skip when false |
| `host_dispatcher` | `ShortText` | Label for the firing system (`cron`, `airflow`, `github_actions`, `mcp_host`) |
| `desc` | `MediumText` | Card/search text |
| `metadata` | `JSON` | Host correlation (webhook path token, DAG id) |

### `auto-workflow` (`AutoWorkflow`)

Declarative workflow definition — ordered steps referencing existing HeavenBase surfaces, not an embedded execution engine.

| Field | Type | Notes |
|-------|------|-------|
| `object_id` | `Identifier` | PK |
| `name` | `ShortText` | Human label |
| `steps` | `JSON` | Ordered list: `{ "kind": "capsule" \| "snippet" \| "tool", "ref": "<id>", "params": {} }` |
| `input_entity` | `ShortText` | Optional entity scope for inputs |
| `output_entity` | `ShortText` | Optional entity scope for outputs |
| `readonly` | `Bool` | Default true; write steps require host + config approval |
| `desc` | `MediumText` | Card/search text |
| `tags` | `Array[ShortText]` | Discovery |
| `metadata` | `JSON` | Version, owner |

### `auto-run` (`AutoRun`)

Append-only execution record written by the host after it fires a trigger.

| Field | Type | Notes |
|-------|------|-------|
| `object_id` | `Identifier` | PK |
| `workflow_id` | `Identifier` | `auto-workflow` |
| `trigger_id` | `Identifier` | `auto-trigger` that initiated the run |
| `status` | `ShortText` | `pending`, `running`, `succeeded`, `failed`, `cancelled` |
| `started_at` | `Timestamp` | Epoch ms |
| `ended_at` | `Timestamp` | Optional until terminal |
| `step_logs` | `JSON` | Per-step `{ ref, status, duration_ms, summary }` |
| `error` | `MediumText` | Redacted failure |
| `host_dispatcher` | `ShortText` | Copied from trigger for audit |
| `metadata` | `JSON` | Host run id |

```text
auto-trigger ──fires──> (host framework) ──executes steps──> auto-run
                              │
                              └── reads auto-workflow.steps (capsule / snippet / MCP tool refs)
```

## Seam Analysis

### Extension bundle

| Seam | Role |
|------|------|
| `Extension("automation", entities=(...)).register()` | Registers `auto-trigger`, `auto-workflow`, `auto-run` through ordinary records |
| `ws.enable_extension("automation")` | Optional; rows require enablement |

### Catalog convergence

| Catalog field | Populate from |
|---------------|---------------|
| `automations` | List of `auto-workflow` `object_id` values linked to the target entity via entity field derive or explicit registration on the entity class |

Example: a `db-database` row derives `automations: ["auto-workflow:nightly-refresh"]` when an operator attaches a workflow. Discovery uses `ws.search(card_text=...)` on Catalog and active `auto-*` structure cards.

### Registry seams (existing)

| Seam | Automation use |
|------|----------------|
| `register_profile` | **`automation`**: read + query on `auto-*`, markdown serializer — for agents auditing definitions and run history. **`automation-admin`**: adds write tool group on `auto-trigger` / `auto-workflow` only (not arbitrary entities). |
| `ToolSerializer.load` | Reuse `markdown` for run log tables |
| `register_extension` | Primary registration |
| Capsule registry (`sys-capsule`) | Workflow `steps` reference capsule ids; execution stays in host calling `Capsule.run` |
| `database` `run_snippet` | Workflow steps may reference `db-snippet` ids; host enforces readonly default |

### Proposed public write seams

| Symbol | Kind | Notes |
|--------|------|-------|
| `hb.ext.record_auto_run` | function | Host calls after execution to append `auto-run` rows |
| `hb.ext.list_triggers` | function | Thin query helper for hosts polling `enabled=true` triggers (optional sugar over `ws.query`) |

No `fire_trigger` inside HeavenBase — firing is always host-side.

### Host integration pattern (design contract)

1. **Poll model:** Host periodically queries `auto-trigger` where `enabled=true` and `host_dispatcher` matches self; evaluates `filter_json` or `schedule_ref` externally.
2. **Event model:** Host subscribes to workspace write stream (application-level) or webhook stub; matches row events to triggers.
3. **Execute:** Host loads `auto-workflow.steps` and invokes Capsules, `run_snippet`, or MCP tools in process.
4. **Record:** Host calls `record_auto_run` with `step_logs` and terminal `status`.

Triggers **fire through host frameworks**, not inside the HeavenBase Python process during `upsert` or query routing.

## Explicit Non-Goals

- **No scheduler runtime inside HeavenBase v1.** No cron daemon, job queue, Celery worker, or sleep loop in the workspace package.
- **No in-process trigger evaluation on `ws.set` / `ws.upsert`.** Synchronous side effects would surprise SDK users and break `explain()` predictability.
- **No workflow DAG engine.** Steps are an ordered JSON list; branching belongs in host DAG tools or Capsule composition.
- **No new query planner ops** for automation scheduling.
- **No default auto-fire on MCP tool calls** even when agent logging is enabled.
- **No authz model beyond profile entity scoping** in v1; enterprise RBAC is a later train.

## Decision

Defer implementation to a post-prototype train. When built, `automation` is an optional extension under `src/heavenbase/extensions/automation/` with three `auto-*` entities, `automation` and `automation-admin` profiles, and host-side firing with `record_auto_run` for audit rows. Catalog `automations` derives from linked `auto-workflow` ids.

## Exit Criteria for Promotion to a Plan

Promote this ADR to an execution plan when **all** of the following hold:

1. **Host spike documented:** One reference integration (e.g. cron script or Airflow DAG) demonstrates poll → execute steps → `record_auto_run` without new HeavenBase runtime threads.
2. **Database/automation intersection reviewed:** `database` snippet execution and readonly defaults align with `auto-workflow.readonly` and `heavenbase.database.allow_write` policy.
3. **Profile shapes agreed:** `automation` vs `automation-admin` tool groups are listed in the API standard table; entity scoping prevents agents from mutating unrelated rows.
4. **Catalog derive specified:** Example derive for `automations` on a concrete entity (e.g. `db-database`) is in the plan with tests.
5. **Lineage coordination:** If both ADR 0003 and this plan land in the same train, `auto-run` optionally links to `lin-run` via `metadata` — optional cross-reference only, not a merge of extensions.

## Consequences

- Prototype pass ships without automation entities; `automations` on Catalog stays an empty default list.
- Teams needing scheduled ingest continue with external cron calling `ingest_database` or MCP tools until this extension exists.
- HeavenBase remains embeddable in constrained runtimes (serverless, notebooks) because no background scheduler thread is required.
