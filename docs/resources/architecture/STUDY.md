# HeavenBase Architecture Study Guide

This page is a reading route, not a second architecture specification. Earlier
versions duplicated thousands of lines of package, API, and extension prose here;
those copies drifted whenever code moved. Each topic now has one canonical owner
linked below.

## First Read

1. [Mental model](mental-model.md) — authority, package ownership, dependency
   direction, and the one-minute runtime flow.
2. [Package and extension layout](extension-layout.md) — final source tree,
   module-folder locality, shared/provider handler boundary, and workspace roles.
3. [Registry and Context](registry-context.md) — standalone Registry semantics,
   Context ownership, module lifecycle, artifacts, and cache identity.
4. [Public API tiers](../reference/public-api.md) — supported user and
   extension-author front doors.

Those four pages are enough to orient a normal change.

## Read by Task

| Task | Canonical page |
| --- | --- |
| Understand the design values | [Design philosophy](design-philosophy.md) |
| Add a Backend, strategy, or handler | [Backend, strategy, and handler guide](backend-strategy-handlers.md) |
| Understand placement and route selection | [Routing and explain](routing-and-explain.md) |
| Work on configuration | [Configuration system](config-system.md) and [configuration reference](../reference/config-spec.md) |
| Work on Registry or module installation | [Registry and Context](registry-context.md), [ADR 0011](adr/0011-standalone-registry-and-lego-resolution.md), and [ADR 0012](adr/0012-unified-module-folder-protocol.md) |
| Work on Entity/schema concepts | [Concepts and classes](concepts-and-classes.md), [Entity JSON](../reference/entity-json.md), and [ID semantics](../reference/id-semantics.md) |
| Trace runtime data flow | [Data flows](data-flows.md) |
| Work on interop | [Interop](interop.md) |
| Work on Capsule or toolkit APIs | [Capsule and toolkit](capsule-toolkit.md) and [MCP reference](../reference/mcp.md) |
| Work on the dashboard | [Dashboard](dashboard.md) |
| Replay or export workspaces | [Workspace manifests](../reference/workspace-manifest.md) and [workspace presets](../reference/workspace-presets.md) |
| Review text/SQL parity | [Text semantics boundaries](text-semantics-boundaries.md) |
| Review aggregation or graph decisions | [ADRs](adr/) |

## Core Questions

Before editing, be able to answer these questions from the linked owner:

1. Which object owns identity and lifecycle: Context, workspace, Backend, Registry,
   or module installation?
2. Is the variation family open (Registry-backed) or closed (exhaustive protocol)?
3. Does the proposed dependency point toward a stable neutral contract?
4. Is a fact authoritative, a read-only projection, or a materialized cache?
5. Can a built-in implementation move outside the distribution without changing
   consumer syntax or central policy?
6. Which focused test proves the claimed boundary?

If the answer requires searching a central provider list, a fallback import map, or a
second registry, stop and revisit the mental model.

## Five Concepts to Keep Distinct

| Concept | Meaning |
| --- | --- |
| Registry | Generic keyed snapshot/CAS infrastructure under `utils/registry/` |
| Module service | Context-owned validation, installation, artifact, and resolution lifecycle over `system-modules` |
| Placement | Logical field-to-Backend bindings; no physical IO |
| Execution | Neutral fragments, row operations, frames, and portable handler behavior |
| Backend | Physical provider adapter and provider-local native behavior |

The same distinction explains two common questions:

- `execution/handlers/` and provider-local `handler/` folders are not duplicate
  systems. The former owns the shared protocol and portable behavior; the latter owns
  native provider behavior. Both publish the same record type.
- `filters/`, `query/`, `execution/frame/`, and `interop/` are not one package.
  Filters are shared syntax, query owns user-facing lowering, frames are neutral
  execution values, and interop spans both ingestion and export.

## Verification Route

Use the repository wrappers from `AGENTS.md`:

```bash
rtk uv run python scripts/docs.py check
rtk bash scripts/test.bash
rtk bash scripts/flake.bash --ci
```

Architecture claims need focused behavioral or import-boundary evidence in addition
to prose. The active work owner and acceptance criteria live only in
[`docs/tasks.yaml`](../../tasks.yaml); this study guide must never become another task
list or progress log.
