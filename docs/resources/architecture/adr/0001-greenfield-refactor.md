# ADR 0001 - Merged Workspace-Centric Runtime

Status: accepted

Date: 2026-04-25

## Context

The greenfield refactor has already been merged into `src/heavenbase`. The active runtime is now workspace-centric, provider-driven, and documented around `type`, not the old legacy HeavenBase surface.

## Decision

Treat the merged `src/heavenbase` package as the only supported implementation.

The canonical surface is:

- `HeavenBase` owns workspace state.
- `Entity` classes are registered into a workspace and expose typed query surfaces.
- `Catalog` and `MetaSchema` are queryable system entities.
- Backend configuration uses `type`; the legacy `kind` key is removed.

## Consequences

- There is no separate `refactored/` package or merge-back phase to plan around.
- Documentation, demos, tests, and issue tracking should describe only the merged runtime.
- Any lingering compatibility text that assumes a future cutover is stale and should be removed when touched.
