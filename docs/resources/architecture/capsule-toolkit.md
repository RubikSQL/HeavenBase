# Capsule, Toolkit, and MCP Architecture

Status: current for the `0.1.2.3` train.

Capsule and Toolkit are separate root domains with a one-way dependency:

```text
Context ModuleResolver + ConfigManager
                  |
                  v
       required Capsule Extension
                  |
                  v
       optional Toolkit Extension
                  |
                  v
 serializers / profiles / MCP families
```

Catalog and MetaSchema are fixed workspace-foundation classes. Capsule is a
required but open executable domain. Toolkit is optional workspace persistence
and composition policy. See
[Workspace Foundation and Extensions](workspace-foundation-and-extensions.md)
for activation, defaults, and dependency semantics.

## Capsule Ownership

`heavenbase.capsule` owns:

- `Capsule`, `CapsuleManifest`, capture, restoration, and verification;
- the `sys-capsule` Entity contract;
- versioned Capsule-layer Registry definitions;
- `CapsuleRegistry` identity, conflict, and row-persistence behavior;
- the `CapsuleRowStore` port consumed by that registry.

Capsule owns no Toolkit verbs and does not construct a HeavenBase workspace.
Composition supplies the adapter that implements `CapsuleRowStore`; the
adapter, not Capsule, decides how those rows reach a Backend. Toolkit
persistence injects an adapter for the selected application workspace.
Standalone Capsule/module administration may instead use a Context-private
store.

The installer-trusted `capsule` Extension is `required=True`, so every usable
workspace registers `sys-capsule` through the ordinary Extension dependency
planner. Required status is installer policy; a programmatic Extension cannot
self-assert it.

## Capsule Data Flow

1. A Python callable enters `CallableSerialization` or `Capsule`.
2. Registry-backed layer builders inspect source, import path, signature,
   dependencies, trust flags, and requested fallback layers.
3. The pipeline emits a `CapsuleManifest` with explicit namespace, name, and
   version identity.
4. `capsule.register()` writes that manifest through the injected
   `CapsuleRowStore` as a `sys-capsule` row.
5. `Capsule.load(...)` selects a row by id or versioned name.
6. `Capsule.to_func()` restores the first permitted working layer, or follows
   an explicit `RestorePolicy`.

The compact construction path is `hb.Capsule(fn, ...)` and `@hb.Capsule(...)`.
Standalone `register` and `load` remain advanced executable/module
administration operations. Factory constructors such as `from_func`,
`from_code`, `from_manifest`, `from_row`, and `from_str` remain available.
Direct `CapsuleRegistry` use is likewise an advanced administration surface.

## Runtime Authority and Isolation

Capsule construction, capture, restore, verification, and Registry load bind
one immutable authority containing the exact `ModuleResolver` and
`ConfigManager`. Workspace, Prompt, and Toolkit composition pass both
authorities through layer, profile, family, serializer, and Tool resolution.
Omission selects process defaults only at a standalone public boundary.

Resolver-local caches include Registry publication state. Capsule verification
caches additionally include resolver, configuration, and frozen restore-policy
identity. Two Contexts can therefore install different same-name layers or use
different trust policy without cross-contamination.

The generic module resolver does not import Capsule implementation while
inspecting records. Context injects a Capsule-source restoration adapter into
`ModuleService`. Only a genuinely absent Capsule reference advances to another
declared restore layer; integrity, policy, and execution failures remain
visible.

Registry definitions refer to nested executable behavior through the shared
`heavenbase.executable` envelope. A reference intentionally selects one owner:

- `python-import` for environment-owned ambient code;
- the owning module's verified `artifact` and installation generation; or
- an already registered `capsule`.

ModuleService owns all three restoration paths. A Capsule reference never
captures or persists a callable as a side effect. A `Tool` is an exposure
adapter over an execution Capsule and a serializer Capsule, not another
executable identity.

Making every function a Capsule, adding package-tree or registered-module
Capsule layers, and projecting Registry/module records into Catalog remain
future convergence ideas rather than current runtime contracts.

## Future Callable-Serialization Convergence

Accepted future work is to route callable serialization through
`CallableSerialization` and Capsule instead of letting each callable-owning
domain retain its own persisted reference shape. This is a target, not current
behavior: entity compute and query-compute hooks, schema-text renderers,
sparse-GRAM callables, reducers, and similar programmatic registrations
currently publish ordinary `python-import` module records, while workspace
manifests retain lightweight module-qualified references.

The preferred first slice is a Registry-module Capsule layer. Its builder
should emit a layer only when the selected Context has one exact active module
record for the callable. The payload should retain canonical kind and
identifier plus enough installation fingerprint, artifact, and generation
evidence to fail closed when ownership changes. Its restorer should delegate to
that Context's `ModuleService`; it must not scan package folders, privilege
built-ins, or import an implementation during inspection.

This layer should be the highest-priority *eligible* Capsule layer because a
verified installed-module identity is stronger than recapturing the callable.
It is not a universal ambient-import layer: when no exact module owner exists,
the builder declines and the existing `source`, `import`, and explicitly
requested `cloudpickle` layers retain their fallback roles. Bundled and
external modules must use the same layer and evidence contract.

Convergence must remain explicit and side-effect free. A `register_*` call may
publish a module descriptor, but must not silently construct or persist a
Capsule. A caller or owning domain explicitly captures and registers the
Capsule, then stores a canonical `heavenbase.executable` reference. Migration
should replace domain-specific callable reference shapes one owner at a time
without adding a second Registry, resolver, or compatibility path.

Required fitness evidence includes cross-process restoration, Context
isolation, built-in/external artifact parity, import-free inspection, stale
generation and fingerprint rejection, and fallback behavior when no eligible
module record exists.

## Capsule Layers and Trust

Built-in layers are:

- `source`: Python source with captured safe globals and checksums;
- `import`: module and qualified-name restoration;
- `cloudpickle`: explicitly requested trusted-local binary fallback.

The default capture order is `("source", "import")`. Extension authors register
additional layers through `hb.ext.register_capsule_layer(...)`; layer builders
emit JSON-safe payloads and restorers recreate callables without modifying
Capsule's central code.

Capsules and Toolkits are executable-code storage. Loading a Capsule can restore
source, import environment code, or use a trusted binary payload. Loading a
Toolkit restores every referenced execution and serializer Capsule. Treat the
configured Registry Backend as a code repository and expose only reviewed
Toolkits to agents or MCP clients.

Callable serialization lives at
`heavenbase.capsule.serialize.CallableSerialization`. Generic
`heavenbase.utils.serialize` deliberately writes only lightweight import
references and does not acquire Capsule semantics.

## Toolkit Ownership

`heavenbase.toolkit` owns:

- the `Tool` and `Toolkit` runtime values;
- the optional `sys-toolkit` Entity and `ToolkitRegistry`;
- the separate `ToolkitRowStore` adapter;
- serializer, profile, Toolkit-family, MCP, and server composition;
- workspace and Backend Toolkit factories.

The `toolkit` Extension is optional and has `requires: [capsule]`. It is in the
current packaged default root list, but a configured workspace may omit it.
Constructing an in-memory `Toolkit` does not activate Toolkit persistence.
Workspace-bound persistence, profiles, families, and MCP composition require
the `toolkit` Extension to be active.

Toolkit depends one-way on Capsule. `ToolkitRegistry` stores Toolkit manifests
through its own row store and delegates executable persistence to an injected
`CapsuleRegistry`. Both row stores bind to the same application workspace.

## Toolkit Data Flow

1. `Toolkit.add(callable)` captures the callable as a Tool's execution Capsule.
2. An explicit serializer becomes a second Capsule; omission selects the
   registered default serializer.
3. `toolkit.register(ws=ws)` registers referenced Capsules and writes one
   `sys-toolkit` manifest row containing ordered execution and serializer refs,
   Tool schemas, and Tool metadata in the same workspace.
4. `Toolkit.load(..., ws=ws)` restores the runtime Toolkit from those refs.
5. `Toolkit.run(...)` returns the raw Python value.
6. `Toolkit.run_to_str(...)` passes that value through the serializer Capsule.
7. MCP and Anthropic exporters adapt the same Tool contracts without creating a
   second persistence format. FastMCP structured output carries the native
   JSON-compatible result; serializer text is a separate presentation channel.

The FastMCP adapter treats every tool call as request-local. Durable identity,
state, authorization policy, and executable ownership remain in the
HeavenBase workspace, Registry, and Capsule domains; protocol session state and
server-initiated client callbacks are not runtime authorities. FastMCP lifespan
owns server-scoped resources and cleanup. The optional native-result cache is
disabled by default, requires an explicit tool allowlist, and cannot outlive
that lifespan.

Each persisted Tool reference includes both `capsule_id` and
`serializer_capsule_id`, plus input/output schemas and Tool metadata.
Capability summaries for the execution and serializer Capsules remain nested
under that Tool's alias.

Exposure-time FastMCP transforms resolve through ordinary `mcp_transform`
Registry builders when selected from JSON configuration, or arrive as live
objects through the direct Python API. They do not own Tool execution,
persistence, profiles, or authorization. FastMCP Providers remain deferred
until HeavenBase has a multi-component or dynamic-discovery requirement that
cannot be represented by the ordered Toolkit contract.

`Toolkit.list`, `Toolkit.describe`, and `Toolkit.delete` use the same `ws=`
authority. Omitting it resolves existing-only `HeavenBase.load()` and never
creates a workspace. `ws.get(toolkit_id, entity=hb.Toolkit)` exposes the raw
Entity row; `Toolkit.load(...)` additionally restores executable values.
Pre-release private-store Toolkit rows require re-registration, with no
dual-read compatibility fallback. See
[ADR 0014](adr/0014-workspace-owned-toolkit-persistence.md).

## Workspace Contributions

Installed Toolkit records are not automatically active in every workspace.
Profiles, serializers, Toolkit families, and cross-domain bridges declare exact
`meta.workspace_requires` Extension identifiers. Their consumers filter
Registry inspection through the shared eligibility helper.

Examples:

- base workspace profiles and serializers require `[toolkit]`;
- Agent tools require `[agent, toolkit]`;
- Memory tools require `[memory, toolkit]`;
- Database tools require `[database, toolkit]`.

Eligibility never activates a missing Extension. This keeps Agent, Memory, and
Database useful without Toolkit and prevents an installed bridge from gaining
workspace side effects merely because its record exists.

Workspace MCP resolves eligible profiles, serializers, and Toolkit families
through `workspace.module_resolver`, then reads workspace-owned MetaSchema
overlays without treating those rows as another module catalog. Family builders
are executable references owned by their verified module artifacts.

Backend MCP remains Backend-owned. `Backend.mcp()` returns one
`BackendMCP` specification, while Toolkit adapts that specification to local,
JSON, or served MCP forms. It does not create a temporary user workspace.

## Class Identity

The Toolkit module is a self-contained `source: path` artifact. The registered
`sys-toolkit` Entity class is therefore resolver- and installation-generation
local. `hb.Toolkit` resolves through `DEFAULT_CONTEXT`; another Context may own
a distinct but schema-equivalent class. Toolkit persistence validates the
`sys-toolkit` schema contract instead of relying on ambient Python class
identity.

The same rule applies to optional Extension Entities and callbacks. Durable
identity is the Registry identifier, schema, artifact digest, and installation
generation—not an implementation package path.
