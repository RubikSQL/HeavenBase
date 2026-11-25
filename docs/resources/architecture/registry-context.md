# Registry and Context

Status: current in the `0.1.2.3` train after package-ownership convergence.

This document describes the current source contract. [ADR 0011](adr/0011-standalone-registry-and-lego-resolution.md)
is implemented: the self-contained Registry utility, injected store, `system-modules` installer,
declarative core-record, Backend/handler, extension,
toolkit, interop, Capsule-layer, and MetaSchema-projection cutovers are current.
[ADR 0012](adr/0012-unified-module-folder-protocol.md) is the current authoring and setup boundary:
co-located `meta.yaml` module folders replace centralized registration resources, and explicit setup
replaces first-access discovery.
[ADR 0013](adr/0013-canonical-tags.md) is also implemented: subject
`meta.tags` is the sole static semantic-claim surface, optional `kind: tag`
records provide typed inspection, and executable proof remains exact.
Local module folders are captured into a content-addressed artifact store before publication.
Remote acquisition, signatures, and ecosystem trust policy remain later work.
Workspace foundation and Extension activation are separate from module
installation and are defined in
[Workspace Foundation and Extensions](workspace-foundation-and-extensions.md).

## Outcome

HeavenBase has one standard-library-only `heavenbase.utils.registry.Registry[K, V]`
implementation for process components, durable definitions, configuration scope
records, workspace definitions, and live workspace tracking. Persistence is optional
and uses a caller-owned `RegistryStore`. Context adapts its already constructed
Backend to that protocol; Registry never imports HeavenBase.

The dependency direction is:

```text
bootstrap mapping/file
        |
        v
     Context --------------------------> Backend
        |                                  |
        +--> HeavenBaseRegistryStore ------+
        |             |
        +--> named Registry namespaces
        |       |
        |       +--> system-modules -> ModuleService
        |
        +--> ConfigManager -> ConfigSnapshot
        |
        +--> workspace directory and configured active id
        |
        +--> live workspace cache
```

Registry never imports HeavenBase or ConfigManager. ConfigManager never selects or
constructs a Backend. Context owns Backend lifecycle, injects its store into Registry,
and injects Registry into ConfigManager.

## Process Contract

```python
import heavenbase as hb

registry = hb.Registry("plugin")
registry.create("example", {"enabled": True}, aliases=("demo",), groups=("builtin",))
```

Calling `hb.Registry("plugin")` creates a new independent process namespace.
To access the application-owned durable namespace with that identity, ask its
Context instead:

```python
plugins = hb.DEFAULT_CONTEXT.registry("plugin")
plugins.create("example", {"enabled": True}, aliases=("demo",))

same_plugins = hb.DEFAULT_CONTEXT.registry("plugin")
assert same_plugins.get("demo") == {"enabled": True}
```

Context memoizes the open namespace in the current process. Another process
opens the same durable namespace through its own Context and calls `refresh()`
to publish newer stored revisions locally.

The protocol includes:

- `resolve`, `get`, strict indexing, membership, length, and iteration;
- `list`/`keys`, `values`, `items`, and immutable `snapshot`;
- display-preserving aliases and flat many-to-many groups;
- create-only `create`, upsert/derive `update`, and `delete`/`pop`;
- atomic synchronous derivation callbacks and multi-record `mutate` drafts;
- async wrappers implemented through worker-thread delegation;
- `max_items`, bounded to 65,536 by default; explicit `None` is available for
  caller-owned process domains that are intentionally unbounded.

Canonical keys may be any hashable value in a process Registry. Tuple keys replace the
former special composite-key registry. Generic groups remain available for
structural indexes such as installation ownership, provenance, and exact
relations. Domain facades do not reinterpret semantic groups as families;
semantic metadata uses record `meta.tags`.

Reads capture the current immutable-state reference once and acquire no writer lock.
The default `snapshot` value policy deep-copies values at every read and write boundary;
`process_identity` is an explicit process-only opt-in for live objects whose identity is
their contract. One writer lock protects next-state validation and construction.
Publication is one state-reference assignment.

Persistent `revision` is the store CAS sequence. Every Registry also exposes a
process-local `publication` sequence, including process-only Registries, so caches can
distinguish every observable state without pretending a process snapshot is durable.

## Persistent Contract

```python
from heavenbase.utils.registry import Registry, SQLiteRegistryStore

store = SQLiteRegistryStore("registry.db")
registry = Registry.load("system-modules", store=store)
```

The caller owns the store. Registry uses only `prepare`, `read`, and
`compare_and_set`. `SQLiteRegistryStore` supplies a standard-library durable store.
For ordinary HeavenBase control state, `Context.registry(...)` is the front door:

```python
registry = hb.DEFAULT_CONTEXT.registry("application-state")
```

`system-modules` is reserved. Consumers use `Context.modules().catalog` for read-only
inspection and the validated `ModuleService` lifecycle for publication; Context never
exposes the mutable raw Registry for that namespace.

Context injects one `HeavenBaseRegistryStore` over its selected Backend. The adapter
maps `prepare`, `read`, and `compare_and_set` to `ensure`, `get_many`, and
`compare_upsert`; it never selects, constructs, or disposes a Backend.

The control Backend is allowed one private deferred-resolver state while Context
uses it to open `system-modules`. Public `Context.backend()` completes that cycle
first and returns only the exact `(ModuleService, ConfigManager)` authority pair
owned by the Context; it never exposes a Backend that could fall through to
`DEFAULT_CONTEXT`.

Each Backend workspace stores one row per namespace in `sys-registry-state`:

| Field | Role |
| --- | --- |
| `object_id` | Registry identifier and physical key |
| `revision` | monotonically increasing snapshot revision |
| `payload` | versioned canonical snapshot bytes |
| `digest` | optional identifier/revision/codec/payload digest |
| `updated_at` | diagnostic UTC timestamp |

The default persistent identity codec supports strings and recursively tagged tuples.
Other key types require paired encoders and decoders. Values are JSON-safe by default;
typed or otherwise specialized values use a paired value codec.

An absent row loads as revision zero without inserting anything. The first mutation
uses create-if-absent CAS; later writes compare the current revision. Encoding,
`max_bytes`, hashing, and Backend CAS all complete before the local state is published.
Any failure leaves the local snapshot unchanged.

With hashing enabled, `refresh()` verifies the stored digest against its payload, and
a stored snapshot whose revision and digest equal the published ones is not decoded
again. Any other stored snapshot is decoded, and its identity, codec version, and state
references are validated. Equal or missing storage is a no-op. A newer revision is
published. A lower revision is treated
as rollback and raises `RegistryIntegrityError`.

CAS conflict handling is explicit:

```python
try:
    registry.update("counter", derive=lambda value: value + 1)
except hb.RegistryConflictError:
    registry.refresh()
    # Recompute and retry only when the caller decides that is safe.
```

Derived mutations are never automatically retried because their callback may have
side effects.

`mutate(callback)` runs several draft `create`, `update`, or `delete` calls and
publishes the complete result in one local assignment and one durable revision. A
callback exception, encoding failure, or CAS conflict publishes no draft record,
alias, group, or revision.

## Central Module Registry

`Context.modules()` memoizes one `ModuleService` over its private opening of the
reserved `system-modules` namespace. The service owns validation and lifecycle,
not record authority: the underlying Registry remains the only durable catalog. A child
Context is a workspace-selection view and shares its parent's exact Backend,
ModuleService, and ConfigManager authority. A separate resolver or config authority
requires an independent `Context.load(...)`; one live Backend is never rebound across
module publications.

Canonical module identities and indexes are:

```text
key   = (kind, identifier)
alias = (kind, alias)
group = (group_kind, value)
```

Persisted values use the non-identity MetaSchema vocabulary: `name`, `desc`,
`source`, `target`, `active`, `meta`, plus an established optional MetaSchema
field such as `dtype` or `strategy`. `meta.schema_version` is required and is
currently `1`; optional author-defined semantic claims live only in the
subject's string-keyed `meta.tags` mapping. The installer reserves
`meta.installation` for stable coordinate,
unique publication generation, module version, HeavenBase compatibility interval,
publisher claim, and canonical manifest fingerprint. Publisher and physical source
location are not installation identity. Keys, aliases, and groups are not duplicated
inside the record.

The lifecycle is intentionally available from the Context-owned module service,
not the broad root facade or the generic Registry utility:

```python
import heavenbase as hb

modules = hb.DEFAULT_CONTEXT.modules()
receipt = modules.install("my_extension")  # reads my_extension/meta.yaml

rows = modules.inspect("serializer")       # imports no implementation code
serializer = modules.resolve("serializer", "custom")

modules.refresh()                           # pull a newer durable revision
modules.retire("serializer", "custom")
modules.unregister("serializer", "custom")
modules.uninstall(receipt)
```

`register(...)` compiles the same one-item envelope as `install(...)`. Both
validate the complete candidate before publishing and use one `Registry.mutate`
call. Coordinate/publisher conflicts, typed-alias collisions, unsupported restoration
layers, and missing hard dependencies leave the Registry revision and every
record unchanged. Every successful publication receives a new generation, including
an identical reinstall. Reconciliation replaces exactly one coordinate's complete
record set and cannot delete another coordinate published by the same party. An old
receipt cannot uninstall a newer generation.

Path installation accepts only an ADR 0012 root-package folder or a file named exactly
`meta.yaml`; mappings remain the programmatic form. Persistent services require an
explicit durable artifact store. The built-in package publisher,
module-set marker identity, and trusted built-in provenance groups are installer-owned
and cannot be claimed through an external descriptor or `register(...)` call.

`retire` preserves the record with `active=False`; `unregister` removes one key;
`uninstall` removes an exact receipt generation; `uninstall_bundle` removes only a
bundle that identifies one complete installation. Each operation validates
remaining hard dependencies in the same draft. CAS conflicts are reported
without refresh or retry. Public lifecycle methods reject built-in records so the
package marker cannot certify an inventory that ordinary calls have removed;
`setup(force=True)` is the trusted repair path.

## Registration Sources and Inspection

The current installer supports four validated restoration sources:

| Source | Persisted target | Restore behavior |
| --- | --- | --- |
| `python-import` | `module:qualified_name` | import the installed top-level object only in `resolve` |
| `path` | relative module and qualified name in the author manifest | capture the complete root package, persist only its digest, and load verified bytes under a private coordinate/generation namespace |
| `inline` | an identifier selecting a JSON value under `meta` | return a detached definition |
| `capsule` | an existing Capsule identifier | restore through the registered Capsule layers only in `resolve` |

`artifact` is the installer-owned persisted form of an author-declared `path`; authors
cannot submit it directly. Captures exclude generated caches and bytecode and reject
symlinks, special files, unstable whole-tree reads, and missing root
`__init__.py`/`meta.yaml`. Loading does not
change `sys.path` or publish author package names in `sys.modules`; relative imports,
virtual `__file__`, and `importlib.resources` read only the verified in-memory generation.
`meta.restore_layers` may contain additional supported source/target pairs. Lambda,
nested-local, non-JSON, mapping-only path, and self-asserted installation metadata
are rejected.

Callbacks nested inside inline definitions use one tagged, versioned executable
reference instead of untyped locator strings:

```yaml
kind: heavenbase.executable
version: 1
source: python-import
target: package.module:callable
```

The runtime sources are `python-import`, `artifact`, and `capsule`. A local-folder
author may write `source: path` with `{module, qualname}`; installation rewrites it
to an artifact reference carrying the captured digest. Authors cannot submit that
persisted form directly. Typed consumers pass the reference and its owning inspection
row to `ModuleResolver.resolve_executable(...)`. That is the same ModuleService-owned
restoration path used by top-level records, so nested package callbacks inherit the
owner's verified artifact and installation generation. Capsule references identify
an already registered Capsule and never register or persist one as a side effect.

`inspect(...)` returns detached record, alias, typed-group, publication, revision, and record
fingerprint data. It never calls `import_module`, reads a manifest, scans a package
folder, or materializes a domain object. `resolve(...)` reads only the Registry
record, selects its supported layers, and caches the restored value by Registry
identity, publication, revision, canonical key, installation generation, and record
fingerprint. Arbitrary plugin code executes outside service cache locks.

## Unified Built-in Module Folders

A module is a cohesive folder with one exact `meta.yaml`; it is a descriptor and
packaging protocol, not a runtime superclass. One descriptor may mix any ordinary
Registry kinds that belong to that unit, including Backend, handler, strategy,
extension, entity, toolkit, profile, dialect, operation, reducer, and serializer
records, plus optional tag definitions and subject claims. Built-in and external
folders use the same manifest shape. Optional role
subpackages such as `backend/` and `handler/` organize implementation without adding
framework interfaces.

`src/heavenbase/resources/modules.yaml` contains only trusted package scan roots. It
owns no Registry items, implementation targets, or provider inventory. During
`hb setup`, `WorkspaceSpec.setup(...)`, or live workspace creation,
`ModuleService.setup()` recursively discovers files named exactly `meta.yaml` below
those roots in deterministic path order. Discovery parses and validates inert data
without importing registered implementations.

Setup validates the complete discovered set, combines records by `(kind,
identifier)`, and reconciles it in one Registry mutation. Any malformed descriptor,
duplicate, dependency error, or ownership conflict leaves the previous revision
unchanged. Reconcile creates, updates, or removes only rows owned by
`package:heavenbase`; external rows are never adopted or overwritten. The installer
assigns `("provenance", "system")` / `("tag", "builtin")` groups and publishes the
durable `("module_set", "heavenbase")` package-composition marker in the same mutation.

Discovery retains the package-resource root containing each descriptor. If any
top-level or nested item declares `source: path`, setup captures that complete
resource folder through the same artifact representation used by an external
filesystem install. ZIP-backed package resources and ordinary package
directories therefore converge before publication; no built-in record resolves
through a privileged package-import fallback.

A non-forced setup refreshes the durable Registry revision, discovers and
validates the inert packaged descriptors, and hashes their normalized
fingerprints together with the package's opaque build identity. A matching
marker skips publication. Any running installation whose exact composition
differs reconciles its own package-owned rows atomically; setup never interprets
or orders package version strings. This keeps shared-home state usable by
release, development, local, and downstream builds without a format-specific
downgrade exception. `hb setup --force` republishes the current composition
without the destructive reset performed by `hb setup --reset`. Calling
`Context.modules()`, `inspect(...)`, or `resolve(...)` never reads
`resources/modules.yaml`, scans a package folder, or opens a `meta.yaml`; the
persisted Registry is the sole runtime authority.

Elasticsearch is the standalone reference layout: its provider folder owns one
`meta.yaml` containing the `backend_type` and `handler` records plus separate
`backend/` and `handler/` implementation roles. SQL and vector remain cohesive family
modules where shared dialects, handlers, and provider definitions still change
together; descriptor inheritance is not inferred from directory nesting.

Artifact modules load under private coordinate/generation namespaces. A class
restored in one Context or installation generation is not promised to be the
same Python object as a same-identifier class restored elsewhere. Typed domains
validate Registry identity and their structural contract; workspace Entity
ingress then continues with that workspace's canonical class. Catalog and
MetaSchema fixed bootstrap is the deliberate exact-identity exception.

## Registry-First Definitions and Canonical Tags

Operations, reducers, logical types, strategies, storage profiles, tag
definitions, SQL handler dialects, and physical database dialects are ordinary
module records. Each executable public typed facade reads its definition through
`ModuleService` before restoring referenced Python code. Built-ins and external
folders therefore exercise the same consumer path, and deleting or uninstalling
an executable definition makes the typed lookup fail even if its former
implementation module is still importable.

Semantic metadata has one separate, deliberately open contract:

- the installed subject record's `meta.tags` is the sole static claim authority;
- an optional `kind: tag` record supplies an inert value schema, default,
  intended subjects/layers, and description;
- `ModuleService.tags(kind, identifier)` returns one snapshot-coherent
  `TagView`, while `.tag(kind, identifier, tag)` reads one result;
- an undefined claim remains visible with unresolved status instead of blocking
  installation or Registry publication;
- a concrete Backend's `tags()` and `tag(...)` overlay construction-derived and
  live values without persisting them;
- MetaSchema redacts the authored mapping rather than copying semantic claims
  into its flat labels.

Tags describe properties and nominate candidates. They never replace exact
`HandlerKey` lookup, compiler output, physical resources, or `explain()`
evidence. `heavenbase.capabilities` remains the specialized exact HandlerKey
inspection surface and is not a second semantic tag registry.

Materialized definitions are optimization only. Typed-facade caches are partitioned
by resolver identity and invalidated by process publication; each entry includes the
durable revision, canonical `(kind, identifier)`, and record fingerprint. The module
service additionally includes the installation generation for restored objects. A
reconcile, refresh, retire, uninstall, Context replacement, or same-revision process
publication therefore cannot leave an older object authoritative. Manifests own
aliases and static tags; typed facades validate that restored values agree with
the inert record rather than maintaining another semantic index.

## Extension Installation and Activation

An `extension` record is an installed definition, not live workspace state.
Workspace bootstrap first registers the exact Catalog and MetaSchema foundation
classes, then activates installer-trusted required Extensions and the stored
requested roots.

The Registry contracts remain separate:

- record `meta.dependencies` protects installed definitions;
- Extension `requires` defines exact-id, dependency-first workspace activation;
- record `meta.workspace_requires` filters conditional contributions without
  activating their requirements.

Tracked workspaces persist requested roots in `WorkspaceSpec`. Defaults are
snapshotted once for a new or legacy workspace; reopen recomputes the resolved
closure. Required and transitive Extensions are not duplicated as requested
roots. MetaSchema reports the active closure without becoming its authority.
There is no generic workspace disable/unload operation.

The fixed foundation, trusted required Capsule, optional Toolkit/domains, and
failure boundary are specified in
[Workspace Foundation and Extensions](workspace-foundation-and-extensions.md).

The only pre-Registry dialect data is the complete `default` and `sqlite` mapping under
the packaged bootstrap override. Context validates it into an immutable
`BootstrapDialectCatalog`, binds that catalog to one explicit `DBEngine`, and injects
the engine into the selected control Backend. There is no ContextVar, temporary global
override, or boolean direct-catalog mode. The bootstrap catalog contains no Python
target, loader, provider scan, or mutable authority; normal database construction uses
a `ResolverDialectCatalog` over the owning Context's `system-modules` records.

## Backend Factory and Handler Resolution

Every ordinary Backend type is a `backend_type` record. Its target restores exactly
one `Backend` subclass; aliases use the central record index, static semantic
claims use that record's `meta.tags`, and `meta.factory` may name one importable
custom factory. When no factory locator is present, resolution adapts the
restored class's `from_config(...)` method.

The construction contract is intentionally singular:

```python
class BackendFactory(Protocol):
    def __call__(
        self,
        name: str,
        provider_config: Mapping[str, Any],
        *,
        resolver: ModuleResolver | None,
        config: ConfigManager,
    ) -> Backend: ...

backend = hb.BackendType.build(
    "search",
    {"type": "example", "name": "primary", "token": "..."},
    ws_id="workspace",
)
```

The shared normalizer consumes only control fields: `type`, optional `name`, and the
direct-control-only `module`. Every other value remains provider config, including
underscored provider keys such as `_override`; `ws_id` is injected only when supplied
by the owning workspace. The constructor validates that the factory returned a
`Backend` with the requested name, canonical type, and workspace binding.

Direct control bootstrap uses the same normalizer, factory adapter, and validation.
Its explicit `module` is the one permitted pre-Registry import: Context imports that
module and requires exactly one matching Backend class. Ordinary workspaces never use
the module field or scan imports; `BackendType` first resolves the installed record.
An absent record therefore fails even when the former implementation module remains
importable.

`BackendRegistry` remains legitimate workspace-owned runtime state: it stores live
Backend instances by instance name. It is not a type, alias, tag, or factory
authority. Programmatic authors publish the same ordinary record shape through
`BackendSubclass.register(tags=..., aliases=..., factory=...)`; built-ins and
durable external packages install co-located `meta.yaml` descriptors instead.

Handler compiler contributions are ordinary `handler` records. Each inert inline
definition contains a versioned executable reference for `seed` and an optional one
for `reasons`.
Workspace seeding inspects those records in deterministic priority/identifier order,
materializes callbacks only on use, and caches descriptors through the same
resolver/publication/revision/key/fingerprint identity. `HandlerPlugin(...).register()`
is the typed programmatic authoring facade for the same record shape, not a second
plugin inventory. The live `HandlerRegistry` remains workspace execution state.

## Breaking migration

The `0.1.2.1` train intentionally removed the replaced authority and argument without a compatibility
shim:

| Removed candidate path or API | Current |
| --- | --- |
| `from heavenbase_registry import Registry` | `from heavenbase.utils.registry import Registry`, or `hb.Registry` from the HeavenBase facade |
| `from heavenbase_registry import class_identifier, identifier_key, named_key` | `from heavenbase.utils.registry import class_identifier, identifier_key, named_key` |
| `Registry.load(identifier, backend=backend)` | `Registry.load(identifier, store=store)` |
| Direct Backend-bound namespace construction | `context.registry(identifier)` for HeavenBase-owned control state |
| Legacy external descriptor filename | Exact `meta.yaml`; no fallback descriptor name |
| Central built-in descriptor directory | Co-located module-folder `meta.yaml` files indexed only by explicit setup |
| `heavenbase.registry` integration package | `heavenbase.utils.registry`, `heavenbase.modules`, and `heavenbase.workspace` owning their distinct roles |
| `heavenbase.bootstrap` | `heavenbase.context` or `heavenbase.context.bootstrap` |

`hb.Registry` is the exact root re-export of `heavenbase.utils.registry.Registry`.
There is no top-level Registry integration package or compatibility module.

The train adds `ModuleService` and immutable `InstallReceipt` only to
`heavenbase.modules`. They are deliberately absent from `hb.__all__`; normal
consumers use typed domain APIs, while extension authors enter through
`Context.modules()` or the module-lifecycle facade. `WorkspaceSpec` resolves from
the lazy `heavenbase.workspace` facade beside `HeavenBase` and `WorkspaceManifest`.

It removes permissive operation normalization and process-owned semantic
inventories. Custom operations and every other open registered kind must be
published before use; there is no implicit string-operation or
importable-built-in fallback.

It removes the Backend class/builder inventories, fixed provider import list, handler
plugin tuples, and callback inventory. It also removes the free Backend
construction/discovery surface without aliases:

| Removed | Current |
| --- | --- |
| `register_backend_class(BackendClass, ...)` | `BackendClass.register(...)` |
| `register_backend_builder(identifier, builder, ...)` | `BackendClass.register(factory=builder, ...)` |
| `get_backend_builder(identifier)` | `BackendType.factory(identifier)` |
| `registered_backend_identifiers()` | `BackendType.identifiers()` |
| `build_backend(alias, spec, ws_id=...)` | `BackendType.build(alias, spec, ws_id=...)` |
| `load_configured_backends(config, ws_id=...)` | `BackendRegistry.from_config(config, ws_id=...)`; use `.backends` only when a snapshot mapping is required |

The canonical-tag cutover likewise removes split semantic authorities without
translation shims:

| Removed | Current |
| --- | --- |
| semantic `groups.family`, `families=...`, and family constants/readers | subject-record `meta.tags`; inspect through `ModuleService.tag(...)` / `.tags(...)` |
| `BackendCapability`, `BackendCapabilities`, and class `capability_spec` | optional `kind: tag` definitions plus static Backend-record claims and effective `Backend.tag(...)` / `.tags()` overlays |
| physical database `caps` and `CAP_*` constants | static tag claims on the `database_dialect` record |
| family filters on `CapabilityIndex` | direct Tag inspection for metadata; exact `CapabilityIndex` methods for HandlerKey support |
| Backend-family storage-profile fields | ordered `backend_preferences`, `strategy_rules`, and `default_strategy` tag predicates |

It removes Extension singleton/factory APIs and the separate MCP tool-name authority.
Workspace enablement is identifier-first, and every retained typed authoring API writes
the same ordinary record path:

| Removed | Current |
| --- | --- |
| `ExtensionRegistry` / `extension_registry()` | `Extension.register()`, `Extension.load(identifier)`, and `Extension.identifiers()` |
| `register_extension(extension, replace=...)` | `extension.register()`; ownership conflicts fail instead of bypassing with `replace` |
| `get_extension(identifier)` | `Extension.load(identifier)` |
| `registered_extensions()` | `Extension.identifiers()` |
| `agent_extension()`, `database_extension()`, `memory_extension()`, `prompt_extension()` | `Extension.load("agent")`, `Extension.load("database")`, `Extension.load("memory")`, or `Extension.load("prompt")` |
| `system_extension()` | No replacement: Catalog and MetaSchema are fixed foundation classes, Capsule is a trusted required Extension, and Toolkit is optional |
| `ws.enable_extension(extension_object)` | Register once, then call `ws.enable_extension(extension.identifier)` |
| `register_profile_tools(tool_names)` | Publish one `WorkspaceToolkitFamilySpec(..., tool_names=...)`; profile validation reads ordinary `toolkit_family` records |
| `register_agent_profiles()`, `register_memory_profiles()`, `register_database_profiles()` | Co-located `toolkit_family` and `mcp_profile` manifest records; inspect through the retained typed family/profile facades |
| Former MCP-session opener helper | Install an `mcp_call_logger` record with a durable callable locator; the returned Agent Session is a persistence grouping, not protocol state |

Built-in `serializer`, `mcp_profile`, `toolkit_family`, `interop_inferer`,
`capsule_layer`, `schema_text`, and sparse-GRAM definitions no longer self-register at
import time. MetaSchema projects safe inspection records with Registry revision and
record fingerprint without resolving implementations; authored `meta.tags`,
absolute local paths, installer-owned fields, and restore fingerprints stay
private. Direct semantic inspection uses the same Context's Tag API. Catalog
remains a directory of concrete objects and receives no module-registration
rows.

## Backend Qualification

`RegistryStore.compare_and_set(...)` has no read-then-write fallback. The standard-library
SQLite store uses an immediate transaction. `HeavenBaseRegistryStore` delegates to
`Backend.compare_upsert(...)`, which fails loudly by default. InMem implements
instance-atomic comparison under its Backend lock. The File Backends compare the
row's record file and write it under a lock shared by every File Backend over the
same folder in one process; writers in other processes are not serialized against
it. HeavenBase SQLite implements
conflict-safe insertion and revision-qualified replacement in provider transactions.
Other Backends remain readable but cannot mutate persistent Registry state until they
implement and pass the shared conditional-write contract.

## Context and Bootstrap

`hb.DEFAULT_CONTEXT` is the process default machine Context. The lazily
constructed default owns the exact `hb.CM_HVNB` instance.
It is constructed on first use, never on import. Any of these first uses loads the
machine bootstrap: an omitted `context=`, an `hb.DEFAULT_CONTEXT` or
`default_context()` access, or the first read or write of `hb.CM_HVNB`. That last one
includes HeavenBase utilities that read `CM_HVNB`, such as emitting a HeavenBase
log record, `hash_id`, and the RNG, parallel and serialization helpers, whatever
module they were imported from. The selected Backend stays lazy until a Registry
or other Backend service is requested. The first `CM_HVNB` access opens the
persisted config, so it creates the package home on a fresh `HOME`. When that
home is unwritable or its bootstrap file is invalid (it fails validation, or
names a backend module or driver that cannot be imported or settings that the
backend rejects), HeavenBase issues a one-time warning and uses process-only
config scopes seeded with packaged defaults. Reads in that process see
subsequent local writes, and `CM_HVNB.last_write_persisted` is `False` after
each local mutation. `hb config set` and the other config mutation commands
exit with an error when their changes were only local.

Call `CM_HVNB.refresh()` or `CM_HVNB.load(refresh=True)` to retry the
open after fixing the problem. If you have made local config changes, an
ordinary refresh keeps them. Call
`CM_HVNB.refresh(discard_process_changes=True)` to switch to the saved
configuration once a persistent store is available. Explicitly binding
`CM_HVNB` to a Context or resetting that Context retires its temporary
scopes. If the default Context already loaded
the bootstrap, a change to its backend settings takes effect in a new
process. A transient failure, such as a locked database, is raised by config
reads, `hash_id` and the RNG and retried on the next access.

The global log helpers
(`configure_logs`, `set_log_level`, `redirect_logs`, `suppress_logs`,
`restore_logs`) apply only to the calling process by default. Pass
`persist=True` or use `hb config set` to save settings through the default
Context's config. Processes sharing that config apply saved settings at their first log record after their
config cache refreshes (`refresh_interval`, two seconds by default).
Process overrides take precedence over saved settings. A log helper keeps
any setting it could not save as a process override. A global relative file
path is resolved against the calling process's working directory. A stream
object is local to the calling process.
In 0.1.2.2, calling `set_log_level()` after `DEFAULT_CONTEXT` bound
`CM_HVNB` to persistent storage could change the level seen by other
processes using the same home.

A process that must never touch the personal bootstrap or
the package-home control database installs its own open root Context first with
`heavenbase.context.set_default_context(context)`. First installation and
implicit adoption of a root that owns `CM_HVNB` require an open Context.
Reinstalling the same open root succeeds. Installing a different root after
default selection raises `RuntimeError`.
`CM_HVNB` persists through its owning Context. With a default installed and
no owner, `CM_HVNB` uses process-only scopes over the packaged defaults.
Standalone Database, SQL backend, LLMEngine, and Capsule calls supplied
`CM_HVNB` alone use its owner's config and resolver when it has an owner.
An unowned `CM_HVNB` selects both from the installed default Context.
Explicitly supplied pairs must still match.
Assigning the module attribute `heavenbase.context.runtime.DEFAULT_CONTEXT =
context` installs a root `context` the same way before first use. After first
use it replaces the default in use, as rebinding that global did in 0.1.2.2:
later omissions of `context=` resolve to `context`, while `CM_HVNB` persistence
and an `hb.DEFAULT_CONTEXT` or `heavenbase.context.DEFAULT_CONTEXT` already read
(each keeps the Context it first returned) still hold the previous one.
`del heavenbase.context.runtime.DEFAULT_CONTEXT` undoes every such assignment
and restores the default in use before the first one, or none. So
`unittest.mock.patch.object(runtime, "DEFAULT_CONTEXT", context)` swaps the
default and restores it without selecting one. If this first selection opened
a process-only Registry for `CM_HVNB`, creating its owner or restoring the
unselected state discards those scopes. Config writes after the owner binds
use its Registry. Undoing the swap does not reach the
two facades: an `hb.DEFAULT_CONTEXT` or `heavenbase.context.DEFAULT_CONTEXT`
first read while the swap is in place keeps returning the swapped-in Context,
even after it is closed, for the rest of the process, so code under such a
swap reads `runtime.default_context()` instead. pytest's
`monkeypatch.setattr(runtime, "DEFAULT_CONTEXT", context)` also restores it,
but it reads the attribute first, and that read selects the default like any
other access, building it from the bootstrap when none is in use. The assigned
value must be a root Context, and in explicit Context mode the assignment
raises `RuntimeError`.
An embedding application that passes `context=` everywhere can call
`hb.configure_explicit_context()` before any default is used: from then on,
building the default Context (an omitted `context=`, `hb.DEFAULT_CONTEXT`,
`default_context()`, or a `CM_HVNB` read or write) and `set_default_context` all
raise `RuntimeError`, so a missed `context=` fails loudly instead of reaching the
personal bootstrap. HeavenBase's own Context-scoped code still runs: log records,
`hash_id`, the RNG and the parallel and serialization helpers use the packaged
values, and the global log helpers apply to the current process only.
The call is idempotent and is refused once a default Context already exists, so
it must come before any first use above; it is also importable as
`heavenbase.context.configure_explicit_context`.
`DEFAULT_CONTEXT` is an attribute of `heavenbase` and `heavenbase.context` but not
part of either `__all__`. A star import of `heavenbase.context` never touches the
default. The Registry-backed exports of the package facades (entity classes
such as `hb.Agent`, `hb.Toolkit` and `hb.Session`, non-entity exports such as
`hb.Tool`, and extension exports such as
`heavenbase.extensions.agent_log.AgentTrace`) load their class through the
default Context, whether they are reached by attribute, by a named import
(`from heavenbase import Agent`,
`from heavenbase.extensions.agent_log import AgentTrace`), by a star import of
either module or by `inspect.getmembers()` on it; in explicit mode each of
these raises `RuntimeError`. An explicit-mode application takes
such a class from its own Context instead. A workspace that enables the class's
extension holds it under its Registry identifier, for example
`ws.entities["agent-agent"]`, `ws.entities["sys-toolkit"]` or, once
`agent-log` is enabled, `ws.entities["agent-trace"]`. Without a workspace, the
Context's modules must first be indexed with `context.modules().setup()`, which
creating a workspace on the Context also runs; before that, the calls below
raise `KeyError` for an unknown registration. Then
`load_entity_type("agent-agent", resolver=context.modules())` from
`heavenbase.entity.registry` returns the same class, and
`load_entity_artifact_export("sys-toolkit", "tool", "Tool", resolver=context.modules())`
from the same module returns a non-entity export such as `Tool`. Do not import
the class from the module that defines it, such as
`heavenbase.extensions.agent.agent`: that class is not the one the Registry
restores, and for several entities, including `Agent`, `Session` and
`AgentTrace`, its structure differs, so a workspace that registers the
Registry class refuses it with `ValueError`.

Bootstrap precedence is:

1. an explicit mapping passed to `Context.load`;
2. an existing `~/.heavenbase/bootstrap.yaml`;
3. the packaged bootstrap resource.

Malformed existing home bootstrap fails with its path and never falls through. The
bootstrap contains only construction-critical root, workspace, selected Backend, and
Registry safety policy. Context resolves `file://%/` locations against the validated
root before constructing the Backend. The selected provider module is explicit, so
opening SQLite does not import unrelated providers.

Context memoizes Registry namespaces and `ModuleService`, owns active workspace
selection, its durable workspace directory, live workspace identities, and the lock
that coordinates those identities. Its child workspace-selection views share the
machine Backend, ModuleService, and ConfigManager while retaining separate ordinary
Registry facade caches, active-id projections, and workspace identity maps. It closes
owned live resources and materialized caches without deleting durable definitions.
`HeavenBase(..., context=ctx)`, `WorkspaceSpec` operations, and
manifest replay retain that Context and inherit its `ModuleResolver`; no nested domain
lookup may silently switch to `DEFAULT_CONTEXT`.

`Context.modules()` only opens the service. `hb setup`, `WorkspaceSpec.setup(...)`,
and live workspace creation ensure `ModuleService.setup()` has indexed the installed
package version before module-backed definitions are needed. The durable
`("module_set", "heavenbase")` marker avoids repeated publication: each setup
compares the installed module composition with the marker in that Context's
Registry. A process keeps its readings of the package's module folders (parsed
descriptors, folder contents and digests, verified captures) while their stat
fingerprints are unchanged, so a workspace construction or hidden store rebuild
in the same process does not rescan an unchanged package. Until its catalog
publishes, each `ModuleService` also keeps its storage profiles and its seeding
of the package's own leading handler contributions: those before the first
contribution that anyone else installed, in priority order. Each workspace gets
a copy of that seeding. Every contribution from the first third-party one on,
later package ones included, is seeded again for each workspace. Any other
resolver, such as a facade over the same catalog, is seeded and its profiles are
read afresh at every call. Package folders are read on every
setup when the package is not on a file system (a zip import) or when one of
their files changed within the last two seconds, since a coarse timestamp may
not show a further change. On Windows, where a file has no change time, the
fingerprints of the package folders also hold digests of the descriptor files
and of the path-source module files, so an in-place rewrite that keeps the size
and puts the modification time back is noticed there too; a stored capture
rewritten that way keeps serving its verified bytes while the process still
remembers that capture: both per-process memos (verified captures by store
folder, and their bytes by digest) keep the 512 most recently recorded
entries, and a capture that drops out of either is read and verified again at
its next load.
A reset or explicit forced setup rescans intentionally.

## ConfigManager

ConfigManager privately composes `Registry[str, ConfigScopeRecord]`. Registry scope
keys are opaque names such as `heavenbase` and `heavenbase.project`; dots are not parsed
by Registry. Each record contains a monotonic generation and a bounded retained list of
versioned nested data layers.

ConfigManager continues to own dotted and list paths, scope chains, tombstones,
overwrite markers, merging, interpolation, version history, compaction, and derived
snapshot LRU caches. Registry owns only raw records, immutable publication, integrity,
revision, and CAS. There is no separate configuration persistence schema.

Workspace config layers are derived rather than registered metadata. Stable workspace
id `shop` maps to `heavenbase.shop`, including when it is the configured default;
`WorkspaceSpec` stores no scope field, and a future display name has no effect.
Workspace-owned resolution enters that exact layer, while Context control state,
module setup, and the control Backend remain at the base scope. Explicit constructor
or manifest construction settings outrank scoped defaults.

Runtime observation is best effort: lazily resolved settings can see later layer
edits, but existing resources and worker execution are not promised live
reconfiguration. Reopening reconstructs the workspace under its derived layer and
is the safe synchronization boundary.

`ConfigManager.refresh() -> bool` delegates durable publication to its Registry
and always clears derived generation/snapshot caches. `arefresh()` is the
non-blocking event-loop wrapper. Reads can also poll for generations according
to `refresh_interval`; there are no push callbacks or background watchers.

## Shared Guarantees And Intentional Differences

| Concern | Registry | ConfigManager / ConfigEngine |
| --- | --- | --- |
| Read snapshot | Immutable structural state; the default value policy returns detached deep copies. `process_identity` is an explicit process-only policy. | `ConfigSnapshot` recursively freezes config values and indexes paths. |
| Parallel reads | Lock-free state capture. | Immutable snapshots are safe to share; cache bookkeeping is locked. |
| Writes | One writer publishes a copied state; durable writes use Backend CAS. | ConfigManager serializes versioned scope updates through Registry. |
| Refresh | Explicit `refresh()` / `arefresh()`; no automatic polling. | Same explicit API plus read-time generation polling by interval. |
| Async | Thread-delegated load/mutations/refresh. | Only refresh is mirrored; multi-step config mutations remain explicit synchronous transactions. |
| Cache | No value cache beyond the published state itself. | Bounded generation/snapshot LRUs and separate ConfigEngine resolved/Apply LRUs. |
| Hot change | Visible after local write or explicit refresh. | Local writes invalidate caches; external writes become visible after polling or explicit refresh. |
| Conflict retry | Never automatic. | Inherits Registry CAS behavior; callers decide whether recomputation is safe. |

The systems deliberately share publication and observation semantics without a
common base class. Registry is the low-level component namespace; ConfigManager
owns configuration-specific merging/history/interpolation, and ConfigEngine
owns deterministic domain resolution.

## Durable Workspace Definitions

`WorkspaceSpec` is the typed value in the Context-owned `system-workspaces`
Registry; an explicit value codec persists its canonical construction contract
and optional replay manifest.
Constructing `HeavenBase("shop", context=ctx)` idempotently creates an absent
durable identity or opens its compatible registered definition. Explicit
`preset=` or `backends=` settings assert the persisted construction spec and
raise `FileExistsError` when they conflict. Entity schemas evolve through their
own registration and MetaSchema lifecycle and are never construction
compatibility inputs. If neither setting is explicit for an existing id, the
persisted construction spec wins over current defaults. A preset and an
expanded backend mapping remain distinct construction forms; the runtime does
not guess that they are equivalent. `HeavenBase.load(...)` requires an existing
registration and raises `KeyError` on a miss. Replaying a manifest or registering a
`WorkspaceSpec` never changes selection. Definition rows store no independent
configuration scope; the workspace id is the single source for the derived
layer described above.

For mappings of ready Backend instances, the durable compatibility signature is
the complete backend-name-to-canonical-identifier mapping. Connection settings,
credentials, and other instance state are deliberately ignored. `load()` may
reuse a live facade. After release or process restart, reconstruction requires
matching live instances from the same Context or explicit Backend input with
that same complete signature.

`WorkspaceManifest` version 2 projects the same construction authority through
one top-level envelope and has no parallel top-level `config`. Replayable
Backend config is isolated under `construction.config`; a runtime workspace
records `construction.identifiers` only. Replayable construction may be
replaced as a whole during manifest replay. A runtime signature remains
authoritative even when explicit config supplies environment-specific
connection details: every Backend name and canonical identifier must match.
Identifier resolution alone never constructs a Backend. Version-1 manifest
input normalizes into this value at the boundary and all output is version 2.

`WorkspaceSpec.extension_roots` stores the optional Extensions explicitly
requested for that workspace, including roots selected from configuration when
the identity first snapshots defaults. Required and transitive dependencies are
recomputed rather than persisted as additional roots. Generated manifests
normally carry the same requested roots, so a later change to global defaults
cannot silently deactivate an existing workspace.

`HeavenBase(..., detached=True)` bypasses durable registration, process lookup,
and selection. The caller owns the returned facade, which cannot activate or be
found by `load()`. Detached does not promise temporary or isolated Backend data,
and it performs no automatic cleanup; instance-level `drop()` remains the
explicit destructive operation.

Context owns current workspace selection. `Context.activate_workspace("shop")` and
`Context.deactivate_workspace()` are the advanced name-only owner operations used by
the `hb ws activate shop` and `hb ws deactivate` commands. The ordinary Python
operations are `ws.activate()` and `ws.deactivate()`. Activation is persisted at
`heavenbase.registry.active.workspace` in the Context configuration profile, while
`Context.active_workspace_id` is the in-memory projection used for resolution.
Already-running Context objects do not poll that key or switch implicitly;
constructing or reloading a Context from the same configuration profile observes the
persisted selection.

`WorkspaceSpec.active` is computed from the owning Context's projection and is never
persisted in workspace definition rows. Live workspace facades are tracked by process
Registries, but those Registries are disposable caches rather than definition,
selection, or default-workspace authorities.

The stable configured default lives at `heavenbase.registry.default_workspace` and is
separate from active selection. `hb setup` creates it. With no explicit active id,
ordinary resolution falls back to that default.
`HeavenBase.load(context=ctx)` resolves active first and then default. This read
never creates a missing identity;
`WorkspaceSpec.setup(...)` / `hb setup` is the explicit idempotent ensure
operation. Activation and deactivation never rewrite the default, and ordinary
workspace construction exposes no implicit `set_default` behavior.

## Single Path

There are no parallel registry implementations or compatibility shims. Every caller
uses Registry directly or a typed domain facade over Registry. Active selection is
Context-owned configuration projected into process state; live workspace tracking is
disposable process state owned by Context and workspace lifecycle.

## Verification Anchors

- `tests/registry/test_registry.py`: process semantics, immutable snapshots, lock-free reads,
  reader/writer stress, capacity, mapping protocol, and async mutation;
- `tests/backends/test_backend_conditional.py`: InMem and SQLite CAS contracts and races;
- `tests/registry/test_registry_backend.py`: persistence, codecs, corruption, conflicts,
  refresh, byte bounds, hot-read behavior, and async persistence;
- `tests/registry/test_modules.py`: record/source validation, failure-atomic bundles,
  canonical programmatic envelopes, coordinate/generation ownership, explicit CAS
  retry, stale receipts, import-free inspection, module-folder setup reconciliation,
  durable package-version marker behavior, scan-free Context lifecycle, external
  typed-consumer parity, versioned nested executable references, Capsule-source
  restoration, safe MetaSchema projection,
  unchanged Catalog ownership, and fail-closed uninstall;
- `tests/registry/test_module_artifacts.py`: whole-root content-stable and fail-closed
  traversal, content-addressed persistence, corruption rejection, private generation
  namespaces, relative imports/resources, and cross-process restore;
- `tests/registry/test_module_resolver.py`: weak resolver identity, publication and
  record-state cache isolation, concurrent materialization, and stale-build rejection;
- packaged-module tests: exact-name recursive discovery, scan-root validation, complete-set
  validation before publication, forced setup, and idempotent concurrent reconciliation;
- `tests/config/test_bootstrap.py`: data-only control dialect bootstrap and selected-provider imports;
- logical-type, query, placement, Backend, handler, capability, and dialect tests:
  Context-local Registry-first behavior, strict missing records, and cache isolation;
- `tests/config/test_bootstrap.py` and `tests/core/test_context.py`: precedence,
  validation, lazy construction, Context-owned workspace state, two-Context resolver
  isolation, child inheritance, thread-local bootstrap control, and disposal;
- `tests/config/test_config_manager.py`: dotted/list paths, retained versions, compaction,
  interpolation, caches, and persistent Registry records;
- workspace, backend, public API, and full integrity suites: migrated domain parity and
  import fitness.
