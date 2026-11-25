# Architecture Design Philosophy

This page summarizes the durable design philosophy behind HeavenBase. It focuses on the mental model, boundaries, and why the runtime stays extensible even where some features are intentionally incomplete.

## Short Pitch

HeavenBase is a typed workspace for agent-manageable data. Users define one logical entity model, the workspace places fields on the right backends, registered handlers compile operations, backends execute provider-specific fragments, and system rows let agents inspect the data landscape.

The design bet is simple: keep the front door small and make the internals composable. A new provider, strategy, logical type, operation, MCP profile, or extension should be one registered piece, not a central rewrite.

Formal decision records:
[ADR 0006 - Boundary-First Workspace Composition](adr/0006-boundary-first-composition.md)
and [ADR 0013 - Canonical Open-World Tags](adr/0013-canonical-tags.md).

## Design Philosophy

| Principle | Meaning in HeavenBase |
|-----------|-----------------------|
| Minimal mental model | Users start with `import heavenbase as hb`, define `Entity` classes, create a `HeavenBase` workspace, then call `register`, `set`, `get`, `query`, `search`, or `to_mcp`. |
| Explicit composition | A Context owns config, module resolution, and workspace identities. `DEFAULT_CONTEXT` is only the omission default. |
| Lego-like composition | Types, strategies, handlers, backends, extensions, profiles, serializers, and toolkit families are independent registered pieces with narrow contracts. |
| Registries over branches | New behavior is added through registration APIs instead of provider-name `if` chains in central planners. |
| Metadata is data | `Catalog` and `MetaSchema` expose safe workspace and Registry projections as queryable rows; the Context Tag API directly inspects semantic module claims without flattening them into MetaSchema. |
| Honest execution | `explain()` reports whether work was native, scan fallback, metadata prefilter, candidate-ID prefilter, post-filter, or unsupported. |
| Boundaries are reviewed | Architecture docs and focused tests make import direction and lazy-loading rules explicit. |
| Explicit non-goals | Incomplete areas are visible design choices rather than hidden half-features: row-data export, multi-agent orchestration, lineage, automation, and a materialized `QueryPlan` are intentionally deferred. |

## Boundary Contract

| Layer | Role | Example module | Forbidden ownership |
|-------|------|----------------|---------------------|
| Shared infrastructure | Generic Registry, config, file, serialization, concurrency, and logging primitives | `utils/` | HeavenBase domain, provider, workspace, or extension policy |
| Reusable runtime domains | Low-level database lifecycle and dialects; LLM configuration, runtime, sessions, cache, and gateways | `database/`, `llm/` | Workspace routing or optional extension policy |
| Composition and modules | Machine lifecycle, RegistryStore adapter, module lifecycle/artifacts | `context/`, `modules/` | Domain dispatch or a second Registry |
| Vocabulary and schema | Types, strategies, filters, Entity declarations, compiled schema | `types/`, `strategies/`, `filters/`, `entity/`, `schema/` | Provider IO |
| Placement | Authored rules/profiles and resolved field-to-Backend bindings | `placement/` | Physical storage or provider imports |
| Neutral execution | Fragments, row operations, frames, shared handler protocol and portable compilers | `execution/` | Concrete providers or workspace routing |
| Physical providers | Backend lifecycle, IO, effective tag overlays, and provider-local handlers | `backends/` | User DSL parsing or central provider switches |
| Query and inspection | Python/JSON query specs and read-only exact HandlerKey discovery | `query/`, `capabilities/` | Provider-specific execution or generic tag ownership |
| Workspace foundation and policy | Orchestration, CRUD, routing, fixed Catalog/MetaSchema, Extension activation | `workspace/` | A second planner, storage runtime, or config system |
| Executable and optional domains | required Capsule; optional Toolkit/MCP and domain bundles | `capsule/`, `toolkit/`, `extensions/` | Fixed workspace schemas or hidden activation authority |

The direction is intentional: high-level policy stays stable, low-level provider details depend inward on registered abstractions.

## How To Explain The Pieces

### Entity

An `Entity` is the domain contract. It says what a row means, how fields are typed, and which fields may have explicit storage hints. It does not know how SQLite, Lance, Neo4j, or Elasticsearch store the row.

### Storage Strategy

A strategy is a storage meaning, not an adapter. `VectorIndex`, `SparseGramIndex`, `SideTable`, and `GraphEdge` say what kind of placement is needed. The backend-local adapter decides how that meaning is implemented on one provider.

### Handler

A handler translates one logical operation into one backend fragment. It is pure compilation: no IO, no global routing, no provider client mutation.

### Backend

A backend owns physical IO and fragment execution. It can optimize deeply for its provider, but it receives `QueryFragment`s rather than parsing the user DSL.

### Workspace

The workspace is the orchestrator. It registers schemas, resolves storage, routes queries, fans writes out to bound backends, merges frames, and keeps Catalog/MetaSchema rows synchronized.

### Extension

An Extension is one Registry-defined Entity/API bundle activated by identifier
in a workspace. Catalog and MetaSchema are fixed foundation classes rather than
Extension content. Capsule is an installer-trusted required Extension; Toolkit,
Prompt, Agent, Memory, and Database are separate optional domains. Installed
definitions, hard activation dependencies, and conditional contribution
requirements remain distinct contracts.

The detailed lifecycle is
[Workspace Foundation and Extensions](workspace-foundation-and-extensions.md).

## Extension Recipes

| Goal | Add | Register | Verify |
|------|-----|----------|--------|
| New backend | `backend_type` record or Backend class | Install class/factory locators with aliases and `meta.tags`, or call `Backend.register(tags=..., factory=...)`; `from_config(...)` is the default factory | CRUD/query smoke test, Tag inspection, and exact-support docs |
| New strategy | Immutable `StorageStrategy` marker/value | `register_strategy(..., tags=...)` and a placement profile if it becomes a default | Placement/explain test |
| New operation | Operation id, aliases, and applicable tag claims | `register_op(..., tags=...)`, then exact handler coverage | Query and JSON-spec tests |
| Provider-native pushdown | `Handler` compiler descriptor | `HandlerPlugin(...).register()` | `explain()` shows `handler_mode == "native"` |
| New entity pack | One self-contained module folder containing its Entity classes and `Extension` record | Install `meta.yaml` or use `Extension.register()` for a durable programmatic package | `ws.enable_extension(...)`, reopen, and inactive-contribution tests |
| New MCP surface | Profile, toolkit family, serializers | `register_profile(...)`, `register_workspace_toolkit_family(...)`, `ToolSerializer(...).register()` | `ws.to_mcp(...)` profile test |

## Current Architecture Assessment

This assessment summarizes the `0.1.2.1` convergence architecture. Active
implementation status belongs only in `docs/tasks.yaml`; ADR 0011 and ADR 0012 own
the durable Registry and folder decisions, while current ownership is summarized in
the mental model.

| Check | Assessment | Evidence |
|-------|------------|----------|
| Rigidity | Low for provider/type/strategy growth | Backends, handlers, types, strategies, operations, profiles, serializers, and extensions have registration APIs. |
| Fragility | Moderate but managed | Multi-backend routing is complex, but exact HandlerKey inspection and `explain()` make fallback behavior visible. |
| Immobility | Low for reusable pieces | Generic Registry, neutral execution contracts, logical placement, provider handlers, and workspace policy have separate owners. |
| Viscosity | Low when contributors follow docs | The clean path is explicit in `backend-strategy-handlers.md`; focused review catches shortcut imports. |
| Needless complexity | Moderate but intentional | The system has many pieces because it models multiple axes of variation; the user-facing front door remains `hb` + workspace methods. |
| Needless repetition | Low | The mental model owns boundaries; this page states values and links detailed Registry, module, routing, and extension contracts. |
| Opacity | Improved | The architecture can be summarized as: schema -> placement -> handler -> backend -> frame, all owned by a workspace and exposed through system rows. |

Maintenance rule: keep [mental-model.md](mental-model.md) as the canonical first-read explanation, link repeated architecture prose back to it, and pair every new mandatory boundary with a focused behavioral test or concrete review criterion. Materialized planning, row-data export, lineage, automation, multi-agent orchestration, and federation remain explicit non-goals until concrete demand changes the contract.

## One-Minute Explanation

HeavenBase is a workspace runtime for agent-manageable data. One Context supplies
machine configuration, module resolution, and workspace identity; the user defines
entities, registers them with a workspace, then queries or exposes tools. Internally,
logical types validate data, strategies describe placement, handlers compile neutral
fragments, and Backends perform physical IO. Catalog and MetaSchema turn safe
projections into queryable metadata; direct Tag inspection preserves typed
semantic claims and provenance. New definitions and claims enter through the
Context-owned Registry path rather than central planners, while exact handlers,
concrete resources, and `explain()` establish the executed route.
