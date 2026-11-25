# Package and Extension Layout

Status: current for the `0.1.2.3` ownership model.

This page is the directory-level guide. It deliberately does not inventory every
file: filenames are implementation detail, while package ownership and dependency
direction are contracts. Read [the mental model](mental-model.md) first and
[ADR 0012](adr/0012-unified-module-folder-protocol.md) for the exact module-folder
authoring protocol.

## Source Tree

```text
src/heavenbase/
  __init__.py / __init__.pyi   # finite lazy user facade
  _lazy.py                     # shared stdlib-only facade loader
  ext.py / ext.pyi             # extension-author facade

  utils/                       # reusable infrastructure
    registry/                  # sole generic Registry implementation
    config/

  context/                     # machine composition and RegistryStore adapter
  modules/                     # manifest, discovery, lifecycle, artifacts
  database/                    # SQL facade, config, engines, physical dialects
    dialect/                   # resolver-backed physical-dialect contracts
  llm/                         # complete LLM domain and HeavenBase cache
    gateways/                  # cohesive resolver-backed gateway modules
  capsule/                     # required Capsule domain and store protocol
  toolkit/                     # optional Toolkit/Tool/MCP domain

  entity/                      # Entity declaration and compiler
  schema/                      # compiled schema IR and metadata
  types/                       # logical-type family
  strategies/                  # placement-strategy family
  filters/                     # expression tree and operation vocabulary
  placement/                   # authored rules and resolved StoragePlan

  execution/                   # provider-neutral execution layer
    contracts.py               # QueryFragment, RowOp, HandlerContext
    frame/                     # ResultFrame and identity merge
    handlers/                  # shared handler protocol and portable compilers

  capabilities/               # read-only capability inspection
  query/                       # immutable Query/QuerySpec, JSON lowering, aggregates
  backends/                    # physical adapters and provider modules
  workspace/                   # public facade plus role-owned services
    runtime.py
    registry.py
    manifest.py
    presets.py
    mcp/
    setup/
    schema/
    rows/
    query/
    graph/
    catalog/                   # fixed Catalog contract and service
    metaschema/                # fixed MetaSchema contract and projection

  interop/                     # inbound inference and outbound conversion
  extensions/                  # optional domain bundles and integration records
  api/                         # dashboard application service + HTTP transport
  cli/                         # thin command adapter
  dashboard/                   # thin desktop-host launcher
  benchmark/                   # supported API, Context-bound runner, recipe descriptor
  resources/

apps/gui/
  ui/                          # TypeScript-first presentation
  host/                        # system WebView host
```

No compatibility package remains at `heavenbase.registry`, `heavenbase.storage`,
`heavenbase.frame`, `heavenbase.handlers`, `heavenbase.discovery`,
`heavenbase.utils.db`, `heavenbase.utils.dialect`, or `heavenbase.utils.llm`.
Owned callers, stubs, descriptors, tests, scripts, and docs move with each package.

## Dependency Direction

```text
cli / dashboard / apps/gui --> api --> workspace <-- optional extensions
                         |
                         +--> fixed Catalog / MetaSchema
                         +--> query / capabilities
                         +--> backends / execution
                         +--> placement / schema / entity / types / strategies / filters

optional Toolkit --> required Capsule
          resolver consumers --> modules + context
                                  |
                                  v
                         utils.registry + utilities
```

The diagram is a direction check, not permission for every peer import. The hard
boundaries are:

- `utils/registry/` imports only the standard library and itself.
- `modules/` depends on Registry mechanics and artifact policy, not domain
  implementations.
- typed domains receive a `ModuleResolver`; they do not import a Context global.
- `database/` owns physical SQL and dialect mechanics; `extensions/database/`
  consumes that surface to provide optional workspace entities and tools.
- `llm/` owns its complete domain and resolves gateway compiler/transport modules
  through the creating Context's resolver.
- `workspace/catalog/` and `workspace/metaschema/` own exact, non-replaceable
  foundation classes; no Extension owns them.
- `capsule/` owns the installer-trusted required Capsule domain and never
  imports Toolkit. `toolkit/` depends one-way on Capsule and remains optional.
- Optional extension folders do not import sibling extension internals.
  Cross-domain behavior enters through stable owner APIs or Registry
  contributions guarded by exact workspace requirements.
- `execution/` contains neutral values and portable behavior and never imports a
  provider.
- provider modules may depend on execution contracts; workspace policy must not
  import provider implementations.
- interfaces call Context/workspace APIs and own no planner or persistence.

## Cohesive Module Folders

A module is one folder with one exact `meta.yaml`. It is a packaging and Registry
protocol, not a superclass or runtime container.

```text
example_provider/
  __init__.py
  meta.yaml
  README.md                 # optional developer documentation
  backend/                  # optional implementation role
    __init__.py
    runtime.py
  handler/                  # optional provider-local role
    __init__.py
    runtime.py
```

One descriptor may publish several related kinds—such as `backend_type`, `handler`,
`sql_dialect`, and `database_dialect`—when they form one independently shipped unit.
The descriptor does not inherit meaning from directory depth. Built-in and external
folders use the same manifest shape and lifecycle.

`resources/modules.yaml` contains trusted scan roots only. `ModuleService.setup()`
finds exact-name descriptors during explicit setup, validates the complete package
set, and reconciles it atomically. Runtime inspection and resolution never scan
folders.

### Self-contained `source: path` folders

Any module folder with a top-level or nested `source: path` reference is an
independent artifact root. It needs root `__init__.py` and exact `meta.yaml`;
targets use artifact-root-relative `{module, qualname}` values. Installation
captures the complete folder and rewrites author-declared `path` references to
installer-owned artifact references.

Internal relative imports must remain inside that root. A dependency on
Capsule, Toolkit, another domain, or a HeavenBase runtime contract uses the
stable absolute owner API or a Registry contribution seam, never `..` traversal
into a sibling folder. Generated cache and bytecode entries are excluded from
artifact content. Package resources and external filesystem folders restore
through the same private coordinate/generation namespace.

An artifact-restored class may be a different Python object in another Context
or installation generation. Registry identifiers and validated domain
contracts are durable identity; package paths and class `is` checks are not.

## Handler Locality

There is one handler protocol with two honest implementation owners:

| Owner | Put here | Do not put here |
| --- | --- | --- |
| `execution/handlers/` | shared Registry/seed protocol, portable predicates, generic fallback compilers and reasons | provider SDKs, provider identifiers, native query payloads |
| `backends/<provider>/handler/` | one provider's native compiler, parity evidence, and provider-specific reasons | a second Registry or workspace router |
| cohesive family package, such as SQL or vector | behavior genuinely shared by that family | duplicated copies under each provider for symmetry |

Every contribution becomes an ordinary `handler` record and enters the same live
`HandlerRegistry`. A provider folder can move out of the HeavenBase distribution
without changing consumer syntax, dispatch, validation, or tests.

## Workspace Locality

The workspace root contains only the public facade implementation and durable public
contracts. Private behavior is grouped by the role that changes:

- `setup/`: Context-bound construction, live lifecycle, Backend/handler assembly,
  extensions, and maintenance;
- `schema/`: entity registration, metadata, profiles, and placement;
- `rows/`: CRUD, encoding, lookup, materialization, and writes;
- `query/`: search, routing, aggregate/filter/near/traverse execution, and explain;
- `graph/`: edge policy, mutation, traversal, audit, and repair;
- `catalog/`: concrete object directory, signatures, audit, and repair planning;
- `metaschema/`: safe projections of schema and registered/runtime capability data.

`workspace/__init__.py` is exposure-only. Public verbs stay on `HeavenBase`; private
services call each other directly and do not use the facade as an internal event bus.

## Why Placement, Capabilities, and Interop Are Independent

`placement/` retains the established `StorageRule`, `StorageProfile`, `StoragePlan`,
and `StorageResolver` vocabulary, but its package name tells the truth: it chooses
logical field routes. Backends own storage and IO.

`capabilities/` reads registered Backend, handler, type, strategy, and operation facts
to answer inspection questions. It owns no facts and performs no module discovery.

`interop/` is a bidirectional ecosystem boundary. Its inbound side infers Entity and
logical types from external tabular/model formats; its outbound side converts
`ResultFrame`. Nesting it under query would hide half its responsibility.

## Author Workflow

1. Choose the smallest folder whose records ship and change together.
2. Implement domain objects against their public contract (`hb.ext` for extension
   authors; owner packages for advanced integrations).
3. Declare inert records in the folder's exact `meta.yaml`; use `source: path`
   for folder-owned code and keep nested executable references in the same
   artifact.
4. Install with `context.modules().install(folder)` or publish the equivalent typed
   programmatic record. Start with the
   [external Extension tutorial](../reference/external-extensions.md) for a
   complete install, enable, CRUD, and uninstall smoke test.
5. Exercise the same contract suite used by built-ins, including missing record,
   retired record, Context isolation, and inspection-without-import behavior.
6. For bundled modules, add the parent to `resources/modules.yaml` only when it is a
   new trusted scan root; do not add a central item inventory.

## Fitness Checks

- Importing `heavenbase.utils.registry` loads no Context, config, domain, workspace,
  extension, handler, or provider implementation.
- Moving a built-in module folder outside the package does not require a consumer or
  planner edit.
- Moving an optional Extension folder outside the package does not require a
  built-in import list, and its internal imports do not escape its artifact root.
- `Context.modules().inspect(...)` imports no registered implementation.
- Two Contexts can resolve different records with the same key without cache or
  workspace-state contamination.
- `execution/` has no concrete provider dependency.
- Retired package names are absent from source imports, string locators, descriptors,
  stubs, tests, and built distributions.

See [Registry and Context](registry-context.md) for lifecycle and cache identity,
[Workspace Foundation and Extensions](workspace-foundation-and-extensions.md)
for activation and dependency boundaries,
[backend/strategy/handler contribution](backend-strategy-handlers.md) for an authoring
walkthrough, and [public API tiers](../reference/public-api.md) for supported front
doors.
