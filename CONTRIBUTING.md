# Contributing to HeavenBase

Thank you for contributing to HeavenBase. This guide covers the minimum workflow for code, docs, and agent-skill changes in this repository.

## Before You Start

- Read `AGENTS.md` for environment policy, commands, and docs authority.
- Read `docs/README.md`, then check `docs/tasks.yaml` before creating resumable work. Claim the existing task when it already owns the requested outcome.
- For cross-module, extension, MCP/toolkit, registry, storage, query, or root-import changes, read `docs/resources/architecture/mental-model.md`, the relevant current/target/gap section, and the owning queue task before editing.
- Use the GitHub issue templates for bugs, features, documentation, and questions.

## Development Setup

In local agent sessions that provide `rtk`, prefix repo commands with `rtk`; otherwise run the underlying command directly.

```bash
rtk bash scripts/sync-env.bash
rtk bash scripts/flake.bash --ci
rtk bash scripts/test.bash
```

Edit dependencies in `requirements*.txt`, then regenerate lockfiles and compatibility files through `rtk bash scripts/sync-env.bash`.

For a version bump, edit only `src/heavenbase/version.py`, then run the same sync command. It updates Poetry metadata and the generated four-line header in every built-in `meta.yaml`; review and stage those descriptors because the remainder of each manifest stays author-owned.

## Pull Requests

Use `.github/pull_request_template.md`.

Before opening a PR:

1. Run `rtk bash scripts/sync-env.bash --check`, `rtk uv run python scripts/docs.py check`, `rtk bash scripts/flake.bash --ci`, and `rtk bash scripts/test.bash`.
2. Review `docs/resources/architecture/mental-model.md` when the change touches layer boundaries, root imports, workspace construction, extension loading, system toolkit/MCP code, storage/entity/query imports, or registry seams.
3. Prefer targeted tests while iterating; reserve the default fast suite for merge readiness. Put provider or implementation-detail coverage behind `@pytest.mark.detail`, and put slow, external, or LLM integration coverage behind their explicit markers unless it protects a core contract.
4. Update canonical English docs when user-facing behavior, public APIs, architecture invariants, MCP profiles/toolkit families, registry metadata, or contributor workflow changes.
5. Keep generated artifacts in sync when their source changes, for example capability docs after handler or SQL dialect metadata changes.
6. Treat the globally installed `heaven-style` skill as externally managed tooling; report a missing or stale install instead of copying or editing it in this repository.
7. Do not commit secrets, local database credentials, or machine-specific overrides.
8. Keep active task state only in `docs/tasks.yaml`. Close substantial work with acceptance evidence in `docs/DEVLOG.md`, then remove completed queue state.
9. Pass external-test credentials through explicit environment variables or CI secrets; do not depend on a contributor's shell startup files.

## Documentation Lifecycle

- `README.en.md` owns user-facing truth; `docs/README.md` and linked resources own engineering truth; `docs/DEVLOG.md` owns concise handoff evidence; `docs/scratch/` owns expiring notes.
- Architecture guidance must label current behavior, accepted target, remaining gap, and non-goal. An accepted ADR is not shipped behavior until code and behavioral evidence support it.
- Roadmaps, ADRs, issues, PRs, reports, and scratch notes may link to `docs/tasks.yaml` but must not become parallel writable queues.
- Delete stale paths, closed review status, and superseded current-state documents in the same change that promotes their durable conclusions.

## Architecture Change Rules

- Maintain the import-boundary invariants in `docs/resources/architecture/mental-model.md`: placement must not import Backend implementations; compiled schema, logical types, and filters must not import workspace or provider code; execution must not import concrete providers; and the required system toolkit must not import optional agent internals. The user-facing Entity DSL may adapt query and placement declarations, but compiled `EntitySchema` stays passive.
- Keep Capsule, Tool, Toolkit, and MCP/profile/toolkit control under the required system extension. Optional extensions may contribute tools, profiles, logging hooks, and entities through registered seams.
- Register new behavior through extension points (`hb.ext.register_profile`, `hb.ext.register_workspace_toolkit_family`, backend/handler/type/strategy registries) instead of editing central planners or adding provider branches.
- Treat workspace system rows (`sys-catalog`, `sys-metaschema`, `mcp_profile`, `toolkit_family`, `mcp_serializer`, `storage_profile`, `sql_dialect`, `database_dialect`) as the path toward self-contained, agent-manageable metadata. Process registries are still runtime callable caches and must stay lock/snapshot safe.
- Keep optional support surfaces lazy. `import heavenbase` and `import heavenbase.utils` should not eagerly load DB, LLM, optional agent/database extension, or system toolkit modules.

## heaven-style Tooling

The standard global skill path is `~/.agents/skills/heaven-style`. That installation is maintained outside HeavenBase. Do not locate, clone, synchronize with, or modify its source as part of ordinary repository work, and do not track an in-repository mirror or harness-specific copies. If the global installation is missing or stale, report the tooling issue for separate manual maintenance.
