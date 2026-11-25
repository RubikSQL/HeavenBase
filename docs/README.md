# HeavenBase Engineering Guide

This is the engineering entry point for maintainers, contributors, agents, and architects. HeavenBase keeps current source-of-truth documentation in the repository; superseded plans, dated merge reviews, and closed task state belong in Git history rather than parallel authority files.

## Four Documentation Surfaces

| Surface | Canonical home | Audience | Lifecycle |
| --- | --- | --- | --- |
| User documentation | [`README.en.md`](../README.en.md) | Users and evaluators | Update when shipped behavior, setup, or supported usage changes. `README.md` and the packaged README are generated copies. |
| Engineering documentation | `docs/README.md` and the durable folders below | Engineers, agents, and architects | Keep current and source-backed. Stable conclusions have one normative home; accepted designs must be labeled as targets until implemented. |
| Development log | [`docs/DEVLOG.md`](DEVLOG.md) | Future maintainers and agents resuming work | Record concise change, verification, and handoff evidence newest first. Git preserves older detail. |
| Scratch | [`docs/scratch/`](scratch/README.md) and ignored `.temp/notes/` | Short-lived requirements, comparisons, and rough analysis | Tracked notes expire within 45 days and must be promoted or deleted. Local-only notes remain ignored. |

The only writable queue for active, resumable repository work is [`docs/tasks.yaml`](tasks.yaml). Issues, pull requests, roadmaps, ADRs, reports, chat, and the development log may link to a task, but they do not own task status.

## Engineering Areas

| Area | Purpose |
| --- | --- |
| [Goals](goals/README.md) | Current outcomes and roadmap pressure. Goals do not own task state. |
| [Architecture](resources/architecture/) | Current mental model, invariants, decisions, accepted targets, and known gaps. Read the [mental model](resources/architecture/mental-model.md) first. |
| [Reference](resources/reference/) | Current public API and operational contracts. |
| [Reports](resources/reports/README.md) | Generated or deliberately refreshed evidence that remains relevant to the current surface. |
| [Scratch](scratch/README.md) | Expiring notes that are not authority sources. |

## Authority Order

When sources disagree, resolve them in this order:

1. User instructions and repository policy.
2. Shipped code, tests, generated artifacts, packaging, and release configuration.
3. Canonical user documentation.
4. Stable engineering resources and accepted ADRs.
5. Goals and the active task queue.
6. The development log and scratch notes.

Architecture and goal documents use four explicit labels:

- **Current**: behavior supported by the checked-out code and behavioral evidence.
- **Target**: an accepted desired design that is not necessarily implemented.
- **Gap**: the observable difference between current behavior and a target.
- **Non-goal**: behavior intentionally excluded from the stated scope.

An accepted ADR is not shipped behavior by itself. [ADR 0011](resources/architecture/adr/0011-standalone-registry-and-lego-resolution.md) remains the implemented Registry-first runtime foundation. [ADR 0012](resources/architecture/adr/0012-unified-module-folder-protocol.md) supersedes its centralized built-in layout and lazy-scan timing with the current co-located module-folder and explicit-setup protocol.

## Current Navigation

### Goals

- [Current goals](goals/current.md) — current branch baseline, accepted target, known gaps, operating priorities, and non-goals.
- [Roadmap](goals/roadmap.md) — durable short-, mid-, and long-horizon direction.

### Architecture

- [Mental model](resources/architecture/mental-model.md) — first-read ownership and boundary contract.
- [Workspace foundation and extensions](resources/architecture/workspace-foundation-and-extensions.md) — fixed schemas, extension dependencies, requested roots, artifact folders, and runtime identity.
- [Design philosophy](resources/architecture/design-philosophy.md) — composability principles and extension recipes.
- [Configuration system](resources/architecture/config-system.md) — current `Preset -> Normalize -> Validate -> Apply(profile)` pipeline.
- [Registry and Context](resources/architecture/registry-context.md) — current process/durable Registry, Backend CAS, Context, and ConfigManager ownership.
- [Canonical tags](resources/architecture/canonical-tags.md) — current open semantic claim model, optional definitions, typed inspection, and named consumers.
- [Query values and result exports](resources/architecture/query-values-and-results.md) — immutable query composition, execution lifecycle, detached frames, and terminal count.
- [Deferred LLM session persistence](resources/architecture/llm-session-persistence.md) — accepted memory-first boundary and unresolved future-extension decisions.
- [Concepts and classes](resources/architecture/concepts-and-classes.md)
- [Data flows](resources/architecture/data-flows.md)
- [Extension layout](resources/architecture/extension-layout.md)
- [Backend strategy handlers](resources/architecture/backend-strategy-handlers.md)
- [Routing and explain](resources/architecture/routing-and-explain.md)
- [Text and scalar semantic boundaries](resources/architecture/text-semantics-boundaries.md)
- [Dashboard](resources/architecture/dashboard.md)
- [Architecture study guide](resources/architecture/STUDY.md)
- [ADR 0012: unified module folder protocol](resources/architecture/adr/0012-unified-module-folder-protocol.md) — current `meta.yaml` authoring, setup discovery, and runtime boundary.
- [ADR 0013: canonical open-world tags](resources/architecture/adr/0013-canonical-tags.md) — implemented authority and execution-proof boundary.
- [ADR 0014: workspace-owned Toolkit persistence](resources/architecture/adr/0014-workspace-owned-toolkit-persistence.md) — concrete Toolkit/Capsule row authority and application lifecycle boundary.
- [ADRs](resources/architecture/adr/)

### Reference

- [Public API tiers](resources/reference/public-api.md)
- [Built-in backend providers](resources/reference/backends.md)
- [Configuration spec](resources/reference/config-spec.md)
- [Entity JSON](resources/reference/entity-json.md)
- [ID semantics](resources/reference/id-semantics.md)
- [Workspace manifests](resources/reference/workspace-manifest.md)
- [Workspace presets](resources/reference/workspace-presets.md)
- [MCP](resources/reference/mcp.md)
- [LLM](resources/reference/llm.md)
- [Agent extension](resources/reference/agent.md)
- [Memory extension](resources/reference/memory.md)
- [Database facade and extension](resources/reference/database.md)
- [External extension tutorial](resources/reference/external-extensions.md)
- [Built-in tags](resources/reference/tags.md) — generated definitions, defaults, subjects, and exact built-in claims.
- [Naming vocabulary](resources/reference/naming-vocabulary.md)
- [Release process](resources/release.md)

### Current Reports

- [Capability matrix](resources/reports/capabilities.md) — generated from live handler/backend registries.
- [Benchmark guide](resources/reports/benchmark.md)
- [SQL text fallback estimate](resources/reports/text-fallback-estimate.md)

## Work and Cleanup Loop

1. Read `docs/tasks.yaml` before proposing resumable work; claim or update one existing task when it already owns the outcome.
2. Keep detailed architecture truth in its durable resource. The queue stores only current task state and acceptance criteria.
3. Update user docs for shipped behavior and engineering docs for durable architecture or workflow changes.
4. Close work by verifying acceptance criteria, adding one concise `docs/DEVLOG.md` entry, promoting durable conclusions, deleting or promoting scratch notes, and removing completed task state from the queue.
5. Delete stale links and superseded current-state documents in the same change that promotes their surviving truth.

## Maintenance

- Regenerate capabilities with `rtk uv run python scripts/gen_capabilities.py` after handler changes; verify drift with `rtk uv run python scripts/gen_capabilities.py --check`.
- Run `rtk bash scripts/benchmark.bash` for performance-relevant work and commit an updated `BENCHMARK.md` only when results are part of the change.
- Keep `README.en.md` canonical and sync generated copies with `rtk bash scripts/sync-readme.bash`.
- Validate the documentation lifecycle and local links with `rtk uv run python scripts/docs.py check`; hooks and CI run the same contract.
- Inspect actionable queue work with `rtk uv run python scripts/docs.py tasks --ready`.
