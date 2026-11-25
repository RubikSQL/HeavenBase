# Current Goals

This page owns the current source baseline and outcome-level direction. Active status, ownership, dependencies, and acceptance criteria live only in [`../tasks.yaml`](../tasks.yaml).

## Current

The checked-out source is the `0.1.2.3` train. It builds on the published `0.1.2.2` baseline and implements ADR 0011's standalone Registry-first runtime, ADR 0012's unified module-folder protocol, and ADR 0013's canonical open-world tags: Registry has one standard-library-only kernel under `heavenbase.utils.registry`, injected process/SQLite/HeavenBase stores, preserved canonical snapshot bytes, one Context-owned `ModuleService`, co-located `meta.yaml` descriptors, explicit setup discovery, and Registry-first declarative, Backend, handler, extension, and system-metadata records. Root and utility package facades are declaration-only, so importing the canonical Registry initializes only its parent packages and standard-library kernel. Publication, release tagging, and release upload remain separately authorized work.

- A workspace owns schema registration, storage placement, routing, mutation coordination, Catalog/MetaSchema projection, and MCP exposure.
- `HeavenBase(...)` is the idempotent durable workspace entry point: it creates
  an absent identity or opens a compatible registered definition. Explicit
  preset/backend construction assertions reject conflicts; `load` requires an
  existing registration; and activation remains a separate explicit action.
  `detached=True` constructs an unregistered caller-owned facade without
  temporary-storage or automatic-cleanup promises. `hb setup` remains the
  idempotent default-workspace ensure operation.
- The source tree is organized by reason to change: `placement/` owns logical
  storage plans; `query/` owns immutable request syntax and validation;
  `execution/` owns neutral frames, deterministic frame transforms, row
  operations, and portable handler contracts; `capabilities/` owns read-only
  inspection; and the workspace implementation binds those owners under setup,
  schema, rows, query, graph, catalog, MetaSchema, and MCP roles. `filters/`,
  `query/`, `interop/`, and `benchmark/` remain deliberate sibling domains
  rather than an artificial query hierarchy.
- The generic Registry has one owner at `heavenbase.utils.registry`. Module lifecycle lives in `modules/`, Context integration in `context/`, and no compatibility package remains at the former root Registry, storage, frame, handlers, or discovery paths.
- Canonical semantic metadata has one static authority: the installed subject
  record's `meta.tags` mapping. Optional distributed `kind: tag` records provide
  inert schemas, defaults, subject/layer guidance, and typed diagnostics without
  closing the vocabulary. `ModuleService.tag(...)` and `.tags(...)` inspect the
  current Registry snapshot without importing implementations; a concrete
  Backend's `tag(...)` and `tags()` add construction and live overlays without
  persisting them.
- Low-level SQL has one `heavenbase.database` owner, including resolver-backed physical dialect contracts; the optional database extension consumes that domain rather than duplicating it. The complete LLM domain has one `heavenbase.llm` owner, with each compiler/transport gateway installed from a cohesive module folder.
- The optional Database extension registers seven standalone, searchable
  context entities; selectively imports and profiles tables and columns while
  preserving agent-authored descriptions; exposes scoped entity editing and
  guarded live read-only SQL; and persists a `DbSnippet` only after successful
  verification. Direct secret-bearing sources remain process-local, while
  named workspace SQL backends own durable reconnection. Its registered MCP
  profile also opts into dashboard chat: read-only turns can retrieve SQL
  context and query the live source, while interactive turns can ingest a named
  backend, edit context, and persist explicitly requested snippets and rules
  without sending credentials through the model.
- Any registered or active/default workspace can export a designated MCP
  profile as an installable, credential-free `WorkspaceSkill` through Python or
  the parser-neutral CLI. The skill, server, and dashboard resolve the same
  profile authority. Declared SQL evidence frames preserve the executed
  statement, actual bounded result, and analysis across blocking, streaming,
  and UI routes.
- SQL template resources are owned by `heavenbase.database.resources.sql`;
  Toolkit prompt descriptions are owned by `heavenbase.toolkit.prompts`; the
  root `resources/` tree contains only package-wide bootstrap assets.
- Concrete Toolkits and their referenced Capsules are rows in one explicit
  application workspace. Toolkit and Capsule Registry services consume typed
  row ports without importing workspace implementation. Context-private
  adapters remain limited to machine/domain state such as LLM caches and
  advanced standalone Capsule/module administration. `WorkspaceSpec.setup()`
  configures the machine and module catalog without initializing a concrete
  bundled domain.
- Backends, strategies, handlers, logical types, entities, and extensions vary through registered seams; central planners do not own provider branches.
- Schema lifecycle operations establish canonical Entity classes, storage plans,
  physical schema, and MetaSchema projections. Generic CRUD is strictly a row
  lifecycle: it resolves classes, identifiers, and Entity instances against an
  already-present canonical class and never registers schema as a side effect.
  The narrow expert exception is an active definition-shaped Entity-kind
  MetaSchema write, which delegates once to that same schema service while the
  stored manual row remains a non-authoritative projection.
- An exact `HandlerKey` or descriptive tag is not proof of native execution.
  `explain()` reports the concrete native, adapter, scan, fallback, or
  unsupported route.
- Public queries are immutable specification values. Transformations branch without mutation; every execution recomputes and returns a detached transport frame that application code explicitly exports. `Query.count()` is a source-only terminal specification whose one-cell frame is read with `scalar()`.
- SQL text filters and aggregate keys require operation-specific compiler support, a provider candidate, and immutable concrete-field evidence before native execution.
- One lock-free copy-on-write `heavenbase.utils.registry.Registry` owns process registration and optional store-backed durable namespaces. Each `Context` owns its machine Backend, `RegistryStore` adapter, Registry namespaces, configuration authority, module resolver, and active/live workspace state. `DEFAULT_CONTEXT` and `CM_HVNB` are the unbound process convenience, not hidden runtime authority for explicitly constructed Contexts; the default Context is constructed on first use, never on import, and `set_default_context` installs an explicit root Context before that.
- `Context.modules()` returns the scan-free `ModuleService` over `system-modules`; its install/inspect/resolve/refresh/retire/unregister/uninstall paths never discover built-ins. Built-in and external module folders use the same exact `meta.yaml`, which may mix any Registry kinds owned by that cohesive folder. `ModuleService.setup()` reads only package roots from `resources/modules.yaml`, recursively discovers exact-name descriptors without importing implementations, and reconciles the complete package-owned set atomically during explicit setup or workspace creation. A durable `("module_set", "heavenbase")` package-version marker skips repeat scans, blocks implicit downgrade from an older process, and reserves built-in ownership/provenance unless setup is forced.
- Operations, reducers, logical types, strategies, storage profiles, tag
  definitions, SQL handler dialects, and physical database dialects are ordinary
  module records. Their typed facades resolve records before implementations,
  isolate materialization by resolver identity and process publication, and key
  records by durable revision, canonical key, installation generation where
  applicable, and record fingerprint. A missing executable record fails even
  when an old implementation remains importable. Tag claims are the deliberate
  exception: a missing optional tag definition leaves the raw claim visible
  with unresolved status.
- Python field expressions and JSON query mappings retain authoring intent until they enter a workspace, then resolve operations and reducers through that workspace's ModuleResolver. Near/traverse limits likewise come from the owning Context, so two Contexts can safely expose different query vocabularies and policy.
- Backend classes, factories, aliases, and static tag claims are ordinary
  `backend_type` records consumed through `BackendType`; executable classes are
  never a runtime fallback for installed static claims. Direct control and
  ordinary workspace construction share one normalized `BackendFactory`
  contract. Provider compilers and routing reasons are ordinary `handler`
  records, while live Backend and Handler registries remain workspace runtime
  state.
- Entities, extensions, toolkit families, MCP profiles, serializers, interop inferers, Capsule layers, schema text, and sparse-GRAM callables are ordinary records. Typed authoring facades publish durable locators, and extension enablement is identifier-first.
- MetaSchema projects safe registration metadata directly from record inspection
  with Registry revision and fingerprint evidence; it redacts authored
  `meta.tags` and does not resolve implementations, expose local paths or
  installation control data, flatten semantic tags, or place registration rows
  in Catalog. Direct Tag inspection remains owned by `ModuleService`. Manual
  Entity-definition command ingress establishes an absent workspace class
  immediately and never turns later row lookup into module or class resolution.
- Catalog and MetaSchema are exact, non-replaceable workspace-foundation
  classes rather than Extension content. The installer-trusted `capsule`
  Extension is required in every workspace; Toolkit, Prompt, Agent, Memory,
  and Database remain separate domains.
- Toolkit and Prompt are the current packaged default roots, while Agent,
  Memory, and Database are opt-in. Tracked workspaces snapshot requested roots
  once, recompute hard `Extension.requires` closure on reopen, and filter
  conditional Registry contributions through exact `workspace_requires`
  identifiers.
- FastMCP 4 is an adapter boundary rather than a second application runtime:
  Toolkit calls retain native structured results and serializer text as
  separate channels, server resources use FastMCP lifespan, result caching is
  disabled by default and explicitly allowlisted, and configured transforms
  resolve through ordinary Registry records. Profiles, workspaces, Capsules,
  and Backends remain the state and policy authorities; FastMCP Providers are
  deferred until a real multi-component requirement exists.
- Parser-neutral CLI specifications, registry values, and Typer/Click/argparse
  compilers depend on the public minimal `heavenbase.cli.CLIContext` protocol.
  `PackageCLIContext` is the HeavenBase runtime adapter that supplies
  configuration, output, and workspace services.
- Eight executable package-boundary guards enforce the accepted dependency
  direction. The tree remains a flat owner map; `core`, `runtime`, `infra`,
  `interfaces`, and `dev` buckets are not architecture layers.
- Workspace extension loading is monotonic. HeavenBase has no universal
  disable/unload lifecycle; an extension may define domain-specific cleanup
  only when it owns and can explain the affected entities, data, and derived
  effects.
- The normal test wrapper runs the focused `fast and not full` integrity tier; provider, external, online-LLM, slow, and implementation-detail coverage is opt-in.

User-visible shipped behavior is described in [`../../README.en.md`](../../README.en.md). Detailed ownership and invariants live in the [mental model](../resources/architecture/mental-model.md).

## Target

[ADR 0011](../resources/architecture/adr/0011-standalone-registry-and-lego-resolution.md) is implemented in the `0.1.2.1` source train: one intentional bootstrap ladder, one standard-library Registry kernel, data-first persisted module records, import-free inspection, and the same install/resolve/lifecycle path for built-in and external pieces. [ADR 0012](../resources/architecture/adr/0012-unified-module-folder-protocol.md) is the implemented authoring and setup refinement: one folder protocol rather than a runtime superclass, co-located `meta.yaml` descriptors, explicit import-free package discovery, scan-free runtime reads, and whole-root local package capture into verified content-addressed artifacts.

The ADR 0011 implementation train, ADR 0012 module-folder refinement,
[ADR 0013 canonical-tag cutover](../resources/architecture/adr/0013-canonical-tags.md),
and [ADR 0014 workspace-owned Toolkit persistence](../resources/architecture/adr/0014-workspace-owned-toolkit-persistence.md)
are closed. Future work starts from this one Registry authority, one descriptor
protocol, one open-world semantic claim model, and one application-workspace
authority for concrete Toolkits rather than reopening those migrations. The
current fixed-foundation, required-Capsule, optional-domain,
activation-dependency, and durable-root contract is defined in
[Workspace Foundation and Extensions](../resources/architecture/workspace-foundation-and-extensions.md).

The dashboard now meets the full heaven-style `0.1.2.3` GUI policy:
presentation and both macOS system-WebView hosts live under
`apps/gui/{ui,host}/`, application orchestration and the shared-model
REST/OpenAPI catalog live under `src/heavenbase/api/`,
`src/heavenbase/dashboard/` stays a thin launcher/packager, every owned UI
module is strict TypeScript with generated protocol types, and
`hb dashboard --install-app` produces the user-local Finder/Dock application.
React remains allowed but was not needed by the current component/state
boundaries. Current behavior is documented in
[Dashboard](../resources/architecture/dashboard.md).

## Gap

- Remote artifact acquisition, signatures, and ecosystem trust policy remain later work. Local `path` sources are already captured as immutable whole-root content-addressed artifacts and restored from verified bytes under private module namespaces; runtime performs no network acquisition.
- Descriptor inheritance and composition are not implemented. SQL and vector remain cohesive family modules where shared dialects, handlers, and provider definitions genuinely change together; standalone providers such as Elasticsearch own provider-local Backend and handler role packages.
- Some provider operations intentionally use exact portable scan/fold execution. Pinecone cannot provide exhaustive enumeration or exact fragment counts for unbounded folds; provider-native text proof remains deliberately narrow.
- Workspace manifest version 2 is a complete reconstructive shell definition:
  one construction envelope, requested optional Extension roots, and schemas,
  but no stored rows or physical Backend data. Required and transitive
  Extensions are recomputed during replay. Runtime Backend identifiers
  constrain reconnection and never fabricate connection configuration.
- Independently routed backends do not provide a distributed transaction or cross-process serialization boundary. A failed fan-out can leave replicas divergent and requires explicit convergence.
- Pickle remains trusted-local, and automatic SQL collation DDL/schema migration is not part of the current text-proof surface.
- Callable serialization is not yet unified under Capsule. Programmatic
  callable families currently publish import- or artifact-backed module
  records, and workspace compute hooks retain lightweight module-qualified
  references. The accepted future direction is a conditionally first,
  Registry-module Capsule layer plus explicit Capsule persistence; see
  [Capsule, Toolkit, and MCP Architecture](../resources/architecture/capsule-toolkit.md#future-callable-serialization-convergence).
- The [LLM session persistence boundary](../resources/architecture/llm-session-persistence.md) remains explicitly deferred to a future LLM-related extension. Current `LLMSession` runtime state and optional `agent` persistence rows have not yet converged on the accepted memory-first, explicit-persistence target.
- Multi-agent orchestration, lineage, and automation are not implemented product surfaces.
- The Database extension has no dedicated dashboard page, automatic-learning
  loop, judge loop, or implicit conversation-memory policy. Dashboard chat uses
  its shipped MCP profile, while those remaining capabilities stay separate
  future product and Memory-extension decisions.
- The generated dashboard macOS `.app` is local and environment-bound rather
  than signed/notarized. Windows packaging remains deferred without a Windows
  host scaffold. These are later packaging concerns, not gaps in the completed
  service/protocol/TypeScript/macOS slice.

## Operating Priorities

- Preserve ADR 0011's Registry-first runtime, ADR 0012's one-name module-folder
  protocol, and ADR 0013's sole static tag-claim authority; remove replaced
  authoring paths and update owned callers instead of introducing dual truth.
- Keep new mandatory architecture rules paired with focused behavioral tests or concrete review guidance.
- Preserve mutation order, failure atomicity claims, identity-safe publication/rollback, import laziness, and honest `explain()` output.
- Keep generated capability and benchmark evidence synchronized through its owning script.
- Keep daily tests focused while retaining explicit full/provider tiers for release and high-risk work.
- Keep user, engineering, development-log, scratch, and task-queue authority separate.

## Non-Goals

- No second planner layer before it is independently testable.
- No empty catch-all `api/` facade or second planner. The existing
  `src/heavenbase/api/` boundary stays dashboard-scoped while the CLI launcher
  and GUI share its independently testable application service; additional
  domains join only when they have the same concrete multi-interface need.
- No universal `Module` runtime superclass, package scan during lookup, or second descriptor filename.
- No provider behavior through central routing branches.
- No generic tag selector, tag-derived execution proof, semantic-tag copy in
  MetaSchema, or second capability-definition authority.
- No replaceable Extension ownership for fixed Catalog or MetaSchema, and no
  kitchen-sink `system` Extension combining fixed, required, and optional
  domains.
- No universal extension disable/unload abstraction without domain-owned
  cleanup semantics.
- No permanent backward-compatibility shim or dual Registry path.
- No root-package buckets created only to reduce the visible directory count,
  and no independent database, LLM, or CLI distribution without an external
  consumer or stable separate release boundary.
- No row-data snapshot/export without a concrete portable-bundle use case.
- No provider-specific GraphEdge dangling mode or neighbour cascade.
- No automatic collation DDL, schema migration, Identifier restriction, or public backend-dependent text mode as part of the current parity gate.
- No speculative multi-agent, lineage, automation, or federation runtime before concrete product demand establishes the contract.
