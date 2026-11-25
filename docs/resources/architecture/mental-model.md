# HeavenBase Mental Model

Status: current for the `0.1.2.3` train.

Read this page before changing a cross-package interface. The short model is:

> A `Context` owns one machine composition; a `HeavenBase` workspace owns one
> logical data world; everything that varies is a registered piece.

The durable Registry and module-folder decisions are
[ADR 0011](adr/0011-standalone-registry-and-lego-resolution.md) and
[ADR 0012](adr/0012-unified-module-folder-protocol.md). Semantic module
metadata follows
[ADR 0013](adr/0013-canonical-tags.md) and the
[canonical tags contract](canonical-tags.md). The original RAW/SDK working
requirements were non-authoritative scratch inputs that reached their recorded
expiry and were deleted (see Git history); shipped
behavior is incorporated into this current contract. Concrete executable
application rows follow
[ADR 0014](adr/0014-workspace-owned-toolkit-persistence.md).
The fixed workspace kernel, required Capsule, optional domains, and activation
rules are defined in
[Workspace Foundation and Extensions](workspace-foundation-and-extensions.md).

## Sixty-Second Explanation

1. A `Context` owns configuration, durable Registry namespaces, module resolution,
   workspace definitions and active selection, and its live workspace cache.
   `DEFAULT_CONTEXT` is only the convenience authority used when a caller does not
   pass one.
2. Schema lifecycle operations establish canonical `Entity` classes in a
   `HeavenBase` workspace. Explicit `ws.register(...)`, manifest replay,
   extension enablement, exact Catalog/MetaSchema foundation bootstrap, and
   active definition-shaped Entity-kind MetaSchema commands all delegate to
   that one boundary. The installer-trusted Capsule Extension is then required
   through the ordinary activation planner.
3. `placement/` turns authored storage rules and profiles into a logical
   `StoragePlan`. It performs no physical IO.
4. Ordinary CRUD targets concrete rows and resolves their class against the
   workspace's already-present canonical Entity. It never registers a class or
   publishes schema metadata as a side effect. The narrow exception is a write
   whose target is MetaSchema itself and whose active Entity-kind `meta`
   payload is a full class definition; that command is consumed by the schema
   service before the metadata row is written.
5. `query/` and `filters/` define immutable, spec-only query values and a closed
   expression tree. Shared handlers under `execution/handlers/` or provider-local
   handlers compile leaves into neutral execution fragments only during explain or
   execution.
6. `backends/` perform physical IO and return `ResultFrame` values from
   `execution/frame/`. The workspace merges those frames by `object_id`.
7. Catalog and MetaSchema rows make workspace schemas, placements, exact
   handler support, and safe module metadata inspectable without becoming
   alternate runtime authorities. Semantic module claims are inspected directly
   through the Context's Tag API; MetaSchema does not flatten or duplicate
   them. A manual Entity-kind MetaSchema definition is command ingress, not
   later row-as-Registry resolution.
8. Built-in and external contributions use the same `meta.yaml`, Registry record,
   resolver, validation, lifecycle, and consumer path.
9. Installing an Extension definition is not workspace activation. A workspace
   persists requested roots, computes exact hard-dependency closure, and admits
   conditional contributions only when their `workspace_requires` ids are active.
10. A concrete Toolkit and its referenced Capsules are application rows owned
    by one explicit workspace. Installed Toolkit families, serializers,
    profiles, and executable module references remain Registry contributions.

## How to Read the Source Tree

The root packages stay flat because each one names an owner and a reason to
change. The following views are navigation aids, not parent directories or one
strict dependency ladder.

The principal query runtime flow is:

```text
user Query / QuerySpec
          |
          v
workspace/query/          routing and schema/resolver binding
          |
          +--> execution/ neutral fragments, frame transforms, portable compilers
          |
          v
backends/                 physical IO and provider-local compilation
          |
          v
execution/frame/          ResultFrame transport
          |
          v
workspace/                hydration, merge, projection, explicit export
```

The role view groups owners for discovery without creating physical buckets:

| Role | Root packages | Central question |
| --- | --- | --- |
| Generic kernel and module lifecycle | `utils`, `modules` | Which low-coupling primitives, installed definitions, and canonical tag claims exist? |
| Authored domain vocabulary | `entity`, `schema`, `types`, `strategies`, `filters` | What may a caller express without performing IO? |
| Declaration and planning | `placement`, `query` | How are storage and query requests described without choosing a provider implementation? |
| Neutral execution | `execution` | Which fragments, row/frame transforms, and portable handler contracts connect plans to adapters? |
| Physical implementation | `database`, `backends` | How is provider IO and provider-local compilation performed? |
| Composition and inspection | `context`, `workspace`, `capabilities` | Which authority composes a machine and workspace, and how are registered facts inspected? |
| Cohesive domains and adapters | `capsule`, `toolkit`, `extensions`, `interop`, `llm` | Which installable domains or external boundaries add behavior? |
| Application and human interfaces | `api`, `cli`, `dashboard`, `apps/gui` | How is Python behavior composed once, transported, and presented without copying business rules? |
| Supported developer surface | `benchmark` | How are workloads, recipes, runners, and reports modeled? |

The audience view determines where callers should start:

| Audience | Entry point | Stability intent |
| --- | --- | --- |
| SDK users | `import heavenbase as hb` | Primary Context, workspace, Entity, field, and query flows |
| Extension authors | `hb.ext` plus the owning package | Stable authoring contracts under owners such as `execution`, `placement`, and `backends` |
| Advanced domain users | `database`, `llm`, `interop`, `capabilities`, `modules` | Complete domain-specific surfaces |
| Interface and benchmark maintainers | `api`, `cli`, `dashboard`, `apps/gui`, `benchmark` | Supported surfaces that need not dominate first-user navigation |

## Composition and Authority

```text
explicit bootstrap
       |
       v
    Context
       +-- config: ConfigManager
       +-- control Backend -> RegistryStore
       +-- named Registry namespaces
       +-- modules: ModuleService -> read-only ModuleCatalog
       +-- private domain-state adapters
       +-- workspace directory and configured active id
       +-- live workspace cache
       |
       v
  HeavenBase workspace
       +-- fixed Catalog and MetaSchema classes
       +-- required Capsule Extension
       +-- optional Extension roots and dependency closure
       +-- schema and placement
       +-- live Backends and handlers
       +-- row/query/graph services
       +-- Catalog and MetaSchema projections
```

There is one authority at each boundary:

| Concern | Authority | Read/adaptation surface |
| --- | --- | --- |
| Generic keyed state | `heavenbase.utils.registry.Registry` | immutable `RegistryView` |
| Installed definitions | Context-private `system-modules` Registry | `ModuleService.catalog`, typed facades |
| Semantic module metadata | subject records' `meta.tags` plus optional `kind: tag` definitions | `ModuleService.tag(...)` / `.tags(...)`; concrete `Backend.tag(...)` / `.tags()` for effective overlays |
| Module lifecycle | `ModuleService` | `install`, `register`, `retire`, `unregister`, `uninstall`, `setup` |
| Machine configuration | `Context.config` | `ConfigManager`, `ConfigSnapshot`, `ConfigOps` |
| Workspace directory and selection | the owning `Context` and its configuration profile | `WorkspaceSpec(..., context=...)`, `HeavenBase` construction/loading, Context selection methods |
| Physical data | live `Backend` instances in one workspace | neutral execution contracts |
| Hidden domain persistence | Context-owned untracked workspace adapters | Capsule/Toolkit row ports and LLM cache services |
| Fixed workspace foundation | exact Catalog and MetaSchema classes | bootstrap and their role-owned services |
| Extension activation | requested roots in `WorkspaceSpec`; resolved live closure in the workspace | `ws.enable_extension(...)`, `ws.extensions()`, MetaSchema projection |
| Inspectable system metadata and manual Entity-definition ingress | workspace Catalog and MetaSchema services | ordinary workspace queries and active Entity-kind MetaSchema writes |

`Context.registry("system-modules")` is deliberately unavailable. Callers inspect
`Context.modules().catalog`; all mutations pass through `ModuleService` validation.
An explicitly constructed workspace retains its Context and resolver. It must not
fall back to `DEFAULT_CONTEXT` during construction, lookup, caching, or bootstrap.

Durable workspace construction, strict loading, and selection are separate
operations. `HeavenBase("shop", context=ctx)` idempotently creates and
registers an absent `shop`, or opens its compatible registered definition,
without activating it. Explicit preset/backend construction settings are
assertions and raise `FileExistsError` when they conflict; changing Entity
schemas does not constitute a construction conflict. When both settings are
omitted for an existing id, its persisted construction spec remains
authoritative and current defaults are not reapplied. Presets and expanded
backend mappings are distinct explicit construction forms rather than inferred
equivalents.
`HeavenBase.load("shop", context=ctx)` requires an existing registration and
raises `KeyError` on a miss. The ordinary Python selection interface is
`ws.activate()` and `ws.deactivate()`; the advanced name-only owner interface is
`ctx.activate_workspace("shop")` and
`ctx.deactivate_workspace()`, which also backs `hb ws activate shop` and
`hb ws deactivate`. The active id is persisted under
`heavenbase.registry.active.workspace` in the Context configuration profile and
projected into `Context.active_workspace_id`. An already-running Context keeps its
in-memory projection; constructing or reloading a Context from the same profile
observes the persisted selection. `WorkspaceSpec.active` is computed from that
projection, and live Registry entries are caches rather than selection authority.

The configured default and the active workspace are independent. `hb setup`
idempotently ensures the stable configured default, and a missing active id
falls back to it.
`HeavenBase.load(context=ctx)` resolves the active workspace first and then the
default. Loading requires an existing registration and never creates on a miss.
Activation never changes the configured default.

Ready Backend instances have a deliberately narrow durable compatibility
signature: the complete mapping from workspace backend name to canonical
Backend identifier. Connection details and other runtime state are ignored.
`load()` may reuse those instances while the live facade exists. After release
or process restart, reconstruction requires matching same-Context live
instances or explicit Backend input with the same complete signature.

`WorkspaceManifest` version 2 is a complete reconstructive workspace-shell
contract. Its closed `construction` envelope is the sole serialized Backend
authority: ambient, preset, replayable Backend config, or a runtime
name-to-identifier signature. Replayable construction may be replaced
whole-envelope by an explicit `preset=` or `backends=`. A runtime signature
instead remains an identity constraint: supplied configs or objects may change
connection details but must preserve every Backend name and canonical
identifier. Identifier lookup never manufactures connection configuration or
live resources. Legacy version-1 `config` input is normalized at the manifest
boundary and is not retained as a second authority.

`HeavenBase(..., detached=True)` builds a caller-owned facade outside the
durable directory and live lookup. It cannot be activated or found by `load()`.
Detached describes lifecycle ownership, not Backend storage: data is not
promised to be temporary or isolated, and cleanup requires an explicit
instance-level `drop()`.

Workspace configuration identity is also independent of selection. A workspace
derives its config layer directly from its stable id, so `shop` overlays
`heavenbase.shop` on the Context's base `heavenbase` layer; the configured default
uses the same rule. `WorkspaceSpec` stores no separate scope, and a future display
name would not affect this identity. Explicit constructor and manifest construction
settings remain authoritative. Workspace operations observe id-scoped edits on a
best-effort basis because already-built resources may retain resolved state;
reopening the workspace is the safe synchronization boundary.

## Package Ownership

The tree is organized by reason to change, not by a desire to minimize the number
of root directories.

| Package | Owns | Does not own |
| --- | --- | --- |
| `utils/` | reusable infrastructure; the standalone Registry kernel, config, file, serialization, concurrency, and logging primitives | HeavenBase domain, provider, workspace, or extension policy |
| `database/` | the low-level SQLAlchemy facade, immutable DB configuration, engine lifecycle, owner-local SQL templates, and resolver-backed physical-dialect contracts | workspace routing or the optional database-inspection extension |
| `llm/` | the complete LLM domain: configuration, runtime, values, sessions, semantic cache, and resolver-backed gateway module folders | optional agent process or persistence policy |
| `capsule/` | required executable-manifest domain, restoration layers, and Capsule row-store protocol | Toolkit persistence or workspace construction |
| `toolkit/` | optional Tool/Toolkit persistence, serializers, profiles, owner-local prompt descriptions, and MCP composition | Capsule persistence or optional domain Entities |
| `context/` | machine composition, control Backend adapter, reset/disposal, default Context | domain definitions or a second Registry |
| `modules/` | module manifest, explicit discovery, install/resolve/lifecycle, local artifact restoration, and static tag inspection | generic Registry mechanics, domain dispatch, or a second semantic claim store |
| `entity/`, `schema/` | authored Entity DSL and compiled schema IR | placement or IO |
| `types/`, `strategies/` | open registered logical-type and placement-strategy vocabularies | provider execution |
| `filters/` | closed expression tree plus open operation vocabulary | routing or Backend IO |
| `placement/` | `StorageRule`, `StorageProfile`, `StorageBinding`, `StoragePlan`, and `StorageResolver` | physical storage |
| `execution/` | neutral fragments/row operations, `ResultFrame`, deterministic frame grouping/filtering/derived columns, generic handler protocols, and portable compilers | provider clients, query syntax, or workspace policy |
| `backends/` | physical adapters, provider-owned module folders, provider-local handlers and facts | user query parsing or central provider branches |
| `query/` | immutable `Query` / `QuerySpec` request values, JSON/path syntax lowering, aggregate declarations, and semantic validation | frame materialization, execution plans, cached results, or provider implementation |
| `capabilities/` | read-only exact `HandlerKey` support and option discovery | module-folder discovery, generic tag inspection, or execution proof |
| `workspace/` | public workspace facade and role-owned runtime services | provider-specific branches |
| `interop/` | inbound schema/row inference and outbound result conversion | query planning |
| `extensions/` | optional cohesive domain bundles and their conditional integration records | fixed workspace schemas, Capsule/Toolkit ownership, another planner, config store, or runtime |
| `api/` | independently testable dashboard application service plus thin HTTP transport | a catch-all facade, planner, physical implementation, or persistence |
| `cli/`, `dashboard/`, `apps/gui/` | thin command, desktop-host, and presentation interfaces over `api/`; parser-neutral CLI specs and compilers depend on the minimal `CLIContext` protocol | duplicated application logic, physical implementation, or persistence |
| `benchmark/` | supported benchmark model, Context-bound runner, registered recipes, and reports | production routing policy or provider construction shortcuts |

### Why These Root Packages Remain Separate

- `database/` is a reusable physical SQL domain used by Backends and direct
  callers. `extensions/database/` adds optional HeavenBase entities, ingestion,
  and tools; nesting or merging them would make low-level SQL depend on workspace
  policy.
- `llm/` contains domain configuration, values, transports, sessions, and cache
  policy. Those concepts are extensible HeavenBase runtime behavior, not generic
  utilities; gateway implementations therefore live in cohesive module folders
  under their one domain owner.
- `capsule/` is universal but not fixed like Catalog or MetaSchema. Its
  installer-trusted Extension is required and its restoration layers remain
  open Registry records. `toolkit/` depends one-way on Capsule and is optional
  workspace policy.
- `filters/` is shared by Entity field references, logical types, handlers, and
  query lowering. Nesting it under query would invert those dependencies.
- `ResultFrame` is a neutral Backend/workspace transport value, so it lives in
  `execution/frame/`, below query and workspace policy. User code explicitly
  exports it into rows, models, scalar values, or an ecosystem-native table.
- `interop/` handles both ingestion and export. Only half of it consumes query
  results, so it is not a query subpackage.
- `capabilities/` inspects exact registered HandlerKeys;
  `ModuleService.tag(...)` / `.tags(...)` inspect semantic record claims; and
  `modules/discovery.py` discovers exact `meta.yaml` files only during setup.
  These are separate jobs and none is an execution-proof authority.
- `benchmark/` remains a root package because it is a supported programmatic
  surface with its own result and workload model. Its Backend combinations are
  ordinary `benchmark_combination` module records, and its runner requires an
  explicit Context; benchmarking is not a privileged provider-import path.

## Dependency Guards

[`tests/core/test_package_boundaries.py`](../../../tests/core/test_package_boundaries.py)
turns the accepted owner model into eight executable import guards:

1. `utils` imports no HeavenBase domain, Context, workspace, provider, or
   interface package.
2. Authored vocabulary and placement import no physical Backend client,
   workspace, or user interface.
3. `execution` may consume neutral vocabulary and expression contracts, but
   imports no provider, workspace policy, high-level query orchestration, or
   optional bundled domain.
4. Backends depend on execution contracts; `execution` never imports
   `backends` or `database`.
5. `query` owns requests and validation, not provider routing or generic
   `ResultFrame` materialization.
6. `workspace` may orchestrate lower owners; lower owners never re-enter
   `workspace`.
7. Workspace foundation imports no replaceable Capsule, Toolkit, or optional
   Extension implementation; it consumes their descriptors and Registry
   records.
8. API, CLI, dashboard, and GUI import no physical Backend, database, execution,
   or placement implementation. Core packages do not import those interfaces.
   Parser-neutral CLI specs, registry, and compilers depend only on
   `CLIContext`, not `PackageCLIContext`, workspace, or `CM_HVNB`.

These guards test direction, not a claim that all root packages form one linear
layer stack. MCP remains a workspace-owned interface and is covered by the
workspace and bundled-extension rules.

## Shared and Provider-Local Handlers

The two handler locations are complementary, not duplicate systems:

| Kind | Location | Purpose |
| --- | --- | --- |
| Generic contract and portable compiler | `execution/handlers/` | `HandlerKey`, Registry, plugin/seed protocol, scan-safe predicates, common fallback behavior |
| Provider or cohesive-family implementation | `backends/<provider>/handler/` or a family-local handler package | native payloads, provider parity evidence, provider-specific fallback reasons |

Both publish ordinary `handler` records and seed the same live
`HandlerRegistry`. Provider code may depend on neutral execution contracts;
`execution/` never imports concrete providers. A new provider must not require a
central import list or provider-name branch.

## Workspace Roles

`HeavenBase` in `workspace/runtime.py` is the public OOP front door. The package
facade is exposure-only; implementation is grouped by operational role:

| Role | Package | Owns |
| --- | --- | --- |
| Setup | `workspace/setup/` | Context-bound construction, Backend/handler assembly, fixed foundation, required/default Extension activation, maintenance |
| Schema | `workspace/schema/` | entity normalization, registration, metadata projection, placement stability |
| Rows | `workspace/rows/` | input normalization, CRUD, encoding, lookup, materialization, routed writes |
| Query | `workspace/query/` | filter/aggregate/near/traverse routing, execution, search, explain |
| Graph | `workspace/graph/` | GraphEdge mutation, traversal, audit, repair, restriction errors |
| Catalog | `workspace/catalog/` | concrete-object directory rows, signatures, audit and repair planning |
| MetaSchema | `workspace/metaschema/` | safe schema, Registry, Backend, handler, exact-support, profile, and dialect projections without semantic-tag flattening |
| Durable/public contracts | `workspace/registry.py`, `workspace/manifest.py`, `workspace/presets.py`, `workspace/mcp/` | workspace directory rows, replay, presets, MCP exposure |

Private services call their owning service directly rather than re-entering public
`HeavenBase` verbs. Caller-owned hidden workspaces use
`HeavenBase(..., detached=True)`; lower-level direct construction remains private
to `workspace/`.

## Registry and Module Rules

- `heavenbase.utils.registry` imports only the standard library and its own
  package. HeavenBase adapters live in `context/`; module lifecycle lives in
  `modules/`.
- Snapshot Registries clone values at read and write boundaries. Live-object
  identity is an explicit process-only `process_identity` policy.
- Persistent `revision` is the store CAS sequence. Process-local `publication`
  changes for every observable local publication, including process Registries.
- One module record is inert data keyed by `(kind, identifier)`. Inspection never
  imports the implementation.
- Typed materialization caches are isolated by resolver identity and include
  publication, durable revision, canonical key, record fingerprint, and install
  generation where the restored object is installation-scoped.
- A local `path` source is captured as a stable whole-root, content-addressed
  artifact before publication. Resolution uses verified bytes in a private
  coordinate/generation namespace; it does not mutate `sys.path` or claim the
  author's public module names.
- Artifact-restored classes are local to one resolver and installation
  generation. Durable Entity identity is the Registry identifier plus schema
  contract; a workspace canonicalizes compatible inputs to its own registered
  class. Catalog and MetaSchema alone require exact fixed class identity.
- Only explicit setup discovers built-in `meta.yaml` files. Runtime inspection and
  resolution use the persisted catalog and never scan the package tree.

## Core Invariants

- Every entity has one user-facing `object_id: Identifier` primary key.
- Every fluent query transform returns a new immutable `Query`; explain and
  execution artifacts are never stored on it.
- Query execution always recomputes and returns a detached result transport;
  results are not cached or persisted automatically.
- The workspace routes; handlers compile; Backends execute; frames merge.
- A storage strategy is immutable placement semantics, not a provider adapter.
- A semantic tag is a descriptive declaration or candidate hint, never proof
  that a route executed natively. Exact HandlerKeys nominate compilers;
  compiler output and concrete resources decide execution, and `explain()`
  reports the route and fallback reason.
- Catalog stores concrete workspace objects. MetaSchema projects safe definitions
  and exact support evidence without copying semantic tags. A
  definition-shaped Entity-kind write may delegate once to
  schema registration, but stored rows never replace Registry or live workspace
  class authority.
- Open extension dimensions use persisted registration and optional tag
  declarations; closed expression and state protocols use exhaustive variants.
- Built-in and external modules differ only by installer-assigned provenance and
  trust metadata, never by consumer path.
- There is one obvious path for each operation: no parallel planners, duplicate
  registries, provider switches, or permanent compatibility packages.

## Public Entry Points

End users start with `import heavenbase as hb`. Pass `context=` when isolation or
composition matters; omission selects `hb.DEFAULT_CONTEXT`.

Extension authors use `hb.ext` for stable authoring contracts and install cohesive
folders through `context.modules().install(...)`. Direct package paths reflect the
owners above: `heavenbase.execution`, `heavenbase.placement`,
`heavenbase.database`, `heavenbase.llm`, `heavenbase.capabilities`,
`heavenbase.backends`, and role-specific workspace
packages. Static semantic claims are read with
`context.modules().tag(kind, identifier, tag)` or
`context.modules().tags(kind, identifier)`; a concrete Backend's `tag(...)` and
`tags()` add construction/live overlays. Database SQL descriptors live at
`heavenbase.database.resources.sql`; Toolkit prompt descriptors live at
`heavenbase.toolkit.prompts`. There are no compatibility packages at the
retired `heavenbase.registry`, `heavenbase.storage`, `heavenbase.frame`,
`heavenbase.handlers`, `heavenbase.discovery`,
`heavenbase.resources.sql`, or `heavenbase.resources.prompts` paths.

See [extension layout](extension-layout.md) for the authoring tree,
[workspace foundation and extensions](workspace-foundation-and-extensions.md)
for activation and dependency semantics,
[Registry and Context](registry-context.md) for lifecycle details, and
[query values and result exports](query-values-and-results.md) for the query
lifecycle, and [routing and explain](routing-and-explain.md) for the execution
path.
