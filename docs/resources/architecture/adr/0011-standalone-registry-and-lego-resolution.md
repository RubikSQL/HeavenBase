# ADR 0011 - Standalone Registry and Lego-Style Resolution

Status: implemented; amended by ADR 0012 and the `0.1.2.1` HB-003
architecture convergence

Date: 2026-07-20

Implemented: `0.1.2.1` source candidate, after the immutable published `0.1.2.0` release boundary

Closeout evidence: the completed `HB-001` train is preserved in Git history.
This ADR owns the implemented decision and constraints; publication, tagging,
and release upload remain separately authorized work.

Superseded in part: [ADR 0012](0012-unified-module-folder-protocol.md) replaces this ADR's descriptor filename, centralized built-in resource layout, and first-`Context.modules()` discovery timing. The standalone Registry, persisted-record authority, import-free inspection, atomic installation, and Registry-first resolution decisions remain in force.

Superseded in package placement: the `0.1.2.1` package-ownership convergence
removes the `heavenbase.registry` integration facade described below. The one
Registry kernel remains `heavenbase.utils.registry`; module lifecycle now lives in
`heavenbase.modules`, machine adapters in `heavenbase.context`, and durable
workspace rows in `heavenbase.workspace`. This note preserves the historical
decision text while making the current no-shim ownership explicit.

Superseded in workspace foundation and Extension ownership: Catalog and
MetaSchema are exact fixed workspace classes, Capsule is a separate
installer-trusted required Extension, and Toolkit plus domain bundles are
optional. The historical `extensions/system` slice below remains decision
history; the current contract is
[Workspace Foundation and Extensions](../workspace-foundation-and-extensions.md).

## 2026-07-22 HB-003 Amendment

The original decision established the correct authority but its first implementation
left several ownership and isolation promises weaker than the prose. HB-003 makes the
following refinements part of the implemented contract:

1. **Registry owns snapshots.** The default value policy deep-copies at every read
   and write boundary. Caller mutation and failed `mutate(...)` drafts cannot alter
   visible or persisted state. Live-object identity is an explicit process-only
   `process_identity` policy.
2. **Publication and persistence are distinct.** `revision` is the durable store CAS
   sequence; `publication` identifies every observable process publication, including
   process-only Registries and same-revision local replacement.
3. **The module catalog is read-only.** `Context.registry("system-modules")` is
   reserved. Consumers inspect `Context.modules().catalog`; every mutation passes
   through `ModuleService` schema, dependency, provenance, and lifecycle checks.
   Programmatic registration rejects installer envelope fields rather than allowing a
   caller record to override `kind`, identifier, indexes, or installation identity.
4. **Installation identity is explicit.** Stable coordinate, release version,
   HeavenBase compatibility interval, per-publication generation, publisher claim,
   provenance, and physical/artifact locator are separate fields. Reconcile owns one
   coordinate, and a stale receipt cannot remove a newer generation.
5. **Context is the runtime authority.** Workspaces retain the Context supplied at
   construction. Typed facades receive/inherit its `ModuleResolver`; `DEFAULT_CONTEXT`
   is only the omission default. Materialization caches are partitioned by resolver
   identity and include publication, durable revision, canonical key, record
   fingerprint, and installation generation where applicable.
6. **Local paths become immutable artifacts.** Installing an author-declared `path`
   captures the complete ADR 0012 root package into a content-addressed store before
   publication. Resolution verifies those bytes and loads them under a private
   coordinate/generation namespace without changing `sys.path` or occupying the
   author's public names in `sys.modules`.
7. **Packages reflect responsibility.** Generic Registry mechanics live only in
   `utils/registry/`; Context adapters in `context/`; module lifecycle and artifacts
   in `modules/`; logical placement in `placement/`; neutral fragments, frames, and
   shared handlers in `execution/`; capability inspection in `capabilities/`; and
   runtime workspace policy in role packages under `workspace/`. Retired package
   paths have no compatibility shims.

This amendment narrows ambiguity; it does not add a second catalog, planner, module
superclass, or provider dispatch path. The normative current explanation is
[Registry and Context](../registry-context.md). The sections below preserve the
original 0.1.2.1 decision and migration record, including paths that were current at
that historical boundary.

## Historical 0.1.2.1 Decision Record

## Context

At the time this decision was accepted, HeavenBase had one copy-on-write `Registry`, Backend-backed persistence, a lazy
machine `Context`, replayable `WorkspaceSpec.config`, and typed process
registries for many extension families. That foundation did not yet provide lego-style extension parity:

- built-in Backends, logical types, strategies, handlers, entities, extensions,
  and related items still have fixed import lists or package bootstrap paths;
- several process registries store imported Python objects rather than durable
  source descriptions;
- the persistent Registry implementation imports HeavenBase Backend, Entity,
  logical-type, strategy, and root-package machinery; and
- importing a HeavenBase Registry submodule executes the eager root bootstrap.

The intended result is stronger than a plugin hook. A built-in and an external
item must have the same registration, resolution, validation, caching, and use
path. Normal consumer syntax must reveal no distinction between them. A built-in
may differ only through installer-assigned provenance such as `system` or
`builtin` tags.

This ADR supersedes any proposal for a pre-Backend component database, a
`ComponentCatalog`, or a generic `ComponentSpec`. It preserves the intended
bootstrap cycle and reuses HeavenBase's existing global entities and data
formats.

## Decision Summary

1. Keep the one Registry implementation as the standard-library-only
   `heavenbase.utils.registry` subpackage. It is owned and shipped by
   HeavenBase, not exposed as a second top-level package. Its kernel must still
   pass extraction tests when copied into an isolated temporary package with
   only the standard library available.
2. Keep the intentional bootstrap ladder: explicit mapping, home YAML, or
   packaged YAML constructs one control Backend directly; that Backend opens the
   central Registry; Registry-resolved configuration then constructs ordinary
   workspace Backends.
3. Preserve the existing workspace constructor envelope (`{"backends": ...}`
   or `{"preset": ...}`) regardless of whether it came from a caller or
   `WorkspaceSpec.config`. Every leaf Backend mapping uses the same normalizer,
   factory contract, and constructor. Only dependency availability differs:
   before Registry exists an explicit source module is required; afterwards the
   Backend factory is resolved through Registry.
4. Make `system-modules` the one authoritative persistent namespace for
   extensible code and definitions. Its canonical key is `(kind, identifier)`.
   Domain APIs remain typed facades over this authority.
5. Store JSON-compatible registration records, not imported objects. Reuse
   existing domain dictionaries and Capsule/MetaSchema vocabulary rather than
   adding another public Spec or Entity.
6. Keep `MetaSchema` as the queryable projection of registrations and `Catalog`
   as the concrete-object directory. They are HeavenBase's only special global
   domain Entity types. An active definition-shaped Entity-kind MetaSchema
   write may be consumed immediately as schema command ingress, but later
   stored-row lookup never becomes resolution authority. Registry control
   storage is private infrastructure, not a third catalog Entity.
7. Install packaged and developer items through the same atomic installer.
   Runtime lookup always asks Registry first and then follows the registered
   restoration locator. It never falls back to a built-in import map or a fixed
   package scan.

## Non-Negotiable Invariants

### Use parity

Given the same `kind` and identifier, consumers call the same API and receive an
object validated against the same contract. Resolution must not expose whether
the item was shipped by HeavenBase, installed from another distribution, loaded
from a local developer folder, or represented as an inline definition.

### Source parity

All runtime resolution starts with Registry. Package-local code is not a second
source of truth. Packaged manifests are installation inputs used to put built-ins
into Registry through the ordinary registration path.

### Locality

One developer item should keep its implementation, tests, documentation, and
registration manifest together in one folder. A manifest may register an
atomic bundle when the item contributes several related kinds. Installing that
folder must not require an edit to HeavenBase source.

### One authority, typed facades

`system-modules` owns durable registration identity and source metadata.
`BackendType`, logical-type helpers, strategy helpers, extension loading, and
other domain APIs validate and adapt its records; they do not maintain parallel
authoritative registries. Imported objects are revision-keyed runtime caches,
not persisted Registry values.

### Inspect without executing

Listing, grouping, alias resolution, provenance inspection, and MetaSchema
projection must not import the registered implementation. Code is loaded only
when a consumer asks to materialize an item.

### No silent network execution

Registry lookup never downloads or executes an unverified remote artifact.
Acquisition is an explicit installer concern. Runtime records may point to an
already installed or immutable cached artifact after integrity and trust policy
has accepted it.

## The Intended Bootstrap Ladder

The bootstrap dependency cycle is intentional and remains visible:

```text
explicit mapping / ~/.heavenbase/bootstrap.yaml / packaged bootstrap.yaml
                                |
                                v
              validate one Backend construction mapping
                                |
                                v
       directly locate and construct the control/system Backend
                                |
                                v
             adapt that Backend to RegistryStore and open Registry
                                |
                                v
        install/reconcile packaged records through the normal installer
                                |
                                v
       resolve registered code and retrieve WorkspaceSpec.config
                         /                  \
       direct constructor envelope    Registry-loaded envelope
                         \                  /
                          v                v
               existing WorkspaceConstruction path
                                |
                                v
            one leaf-Backend normalizer and factory contract
                                |
                                v
                     user workspace Backends
```

There is no Registry lookup before the Backend that persists Registry exists.
The control Backend is the sole source-resolution exception: its trusted
bootstrap mapping must contain an explicit importable `module`. This does not
privilege built-in code; an external Backend can be the control Backend when its
module is locally importable and it satisfies the Registry persistence
contract.

The current files already encode most of this ladder:

- `bootstrap.py` selects explicit mapping, home YAML, then packaged YAML;
- `BootstrapSpec.backend` is a plain construction dictionary;
- `Context.backend()` constructs the selected Backend lazily and exposes it only
  after binding the Context-owned module authority;
- `Context.registry()` opens Registry only after that Backend exists;
- `WorkspaceSpec.config` stores replayable constructor keyword arguments; and
- Registry-loaded workspaces already pass that dictionary into the ordinary
  `HeavenBase` construction path.

One current defect must be fixed before relying on this contract:
`Context.backend()` replaces every SQL bootstrap `_override` with `{}` even
though the packaged bootstrap contains a complete override. A complete
bootstrap override must be preserved and resolved without consulting
Registry-backed `ConfigManager` state. Bootstrap must remain operable during
recovery even when Registry data or ordinary configuration is unavailable.

## One Backend Leaf Configuration Shape

Do not add another Backend or workspace Spec. `WorkspaceSpec.config` remains the
existing constructor envelope, normally `{"backends": {alias: mapping}}` or
`{"preset": name}`. Preset resolution and direct construction continue to
produce the same alias-to-mapping collection. Each leaf retains the existing
flat shape:

```yaml
type: sqlite
module: heavenbase.backends.sql.sqlite  # required only before Registry exists
name: system
database: file://%/system.db
_override:
  default_provider: sqlite
  providers: {}
```

The canonical normalizer consumes control fields and returns the provider
arguments without knowing the mapping's source:

| Field | Meaning | Bootstrap | Ordinary workspace |
| --- | --- | --- | --- |
| `type` | Registered Backend type identifier | required | required |
| `name` | Workspace Backend instance name | required | required |
| `module` | Direct local locator used before Registry | required | normally omitted; never a built-in fallback |
| remaining keys | Existing provider/config arguments | passed unchanged after validation | passed unchanged after validation |

Construction has two injected resolution modes, not two leaf configuration
formats:

- without Registry, resolve the explicit `module`, select the matching Backend
  type, validate it, and instantiate it; or
- with Registry, resolve `("backend_type", type)` through `system-modules`,
  restore and validate the class, and instantiate it.

Both routes strip `type`, `name`, and `module` before calling one factory
contract:

```python
BackendFactory = Callable[[str, Mapping[str, Any]], Backend]
```

A restored Backend subclass is adapted to that contract through
`BackendClass.from_config`; a record may optionally name a custom factory with
the same signature. Direct bootstrap adapts its explicitly located class in the
same way. The common constructor calls only `BackendFactory(name,
provider_config)` and validates that the result is a Backend bound to the
requested name/workspace.

One Registry record owns the Backend class metadata and optional factory
locator. Do not retain `BackendType` and `BackendBuilder` as separate durable
authorities. `BackendType` is the typed public facade; the former process
`_BACKEND_TYPES` and `_BACKEND_BUILDERS` authorities are removed.

A direct workspace envelope and an identical envelope loaded through
`WorkspaceSpec.config` must yield equivalent leaf mappings, normalized Backend
specs, instances, and behavior. Presets remain a way to produce that same
envelope, not a separate construction model.

## Self-Contained Registry Boundary

The canonical Registry kernel lives at this dependency leaf:

```text
src/heavenbase/utils/registry/
    __init__.py
    identity.py
    registry.py
    codec.py
    store.py
```

It may import only the Python standard library. It owns:

- copy-on-write state and one-writer publication;
- canonical keys, aliases, groups, and revisions;
- create, update, delete, lookup, snapshots, and atomic batch mutation;
- key/value codecs, canonical bytes, digests, size/item guards, and integrity
  validation;
- conflict and corruption errors; and
- synchronous and asynchronous wrappers.

Durability is injected through a narrow protocol, provisionally named
`RegistryStore`:

```python
class RegistryStore(Protocol):
    def prepare(self) -> None: ...

    def read(self, namespace: str) -> tuple[int, bytes, str] | None: ...

    def compare_and_set(
        self,
        namespace: str,
        *,
        expected_revision: int,
        revision: int,
        payload: bytes,
        digest: str,
    ) -> bool: ...
```

This protocol is dependency inversion, not a new Spec, catalog, configuration
format, or persistence authority. Exact private carrier types may change while
preserving the semantic contract: read one opaque snapshot and atomically
replace the expected revision.

Direct use supports a process-only Registry and a standard-library SQLite
store. HeavenBase supplies `HeavenBaseRegistryStore` under
`src/heavenbase/context/registry.py`; `Context` injects it after constructing the control
Backend. HeavenBase never silently opens a second database for Registry.

The implementation guarantees dependency independence and extraction fitness,
not a separate import or wheel boundary. An isolated test copies
`src/heavenbase/utils/registry` to a temporary `registry` package and runs the
full Registry/store contract with only the standard library. The canonical
installed import remains `heavenbase.utils.registry`; another Registry
distribution is not part of this architecture.

The `sys-registry-state` row remains behind the HeavenBase adapter as an opaque
control table. It is never an
extension Entity, never appears in Catalog, and is not another global metadata
table. A later Backend-native raw CAS capability may replace its current
EntitySchema-based implementation without changing Registry.

`heavenbase.utils.registry.Registry` is the canonical package import and
`hb.Registry` is its intentional lazy root re-export. There is no
`heavenbase.registry` compatibility package: module lifecycle belongs to
`heavenbase.modules`, workspace records to `heavenbase.workspace`, and the
Backend adapter to `heavenbase.context`.

## Central Module Registry

`Context.registry("system-modules")` is authoritative for all extensible item
kinds. Canonical keys and indexes use existing Registry facilities:

```text
key       = (kind, identifier)
alias     = (kind, alias)
group     = (group_kind, value)
```

Recommended group kinds are `family`, `tag`, `provenance`, `bundle`, and
`scope`. A built-in is therefore an ordinary record in groups such as
`("provenance", "system")` and `("tag", "builtin")`; those groups do not
select a different loader.

Initial item kinds should preserve established domain vocabulary, for example:

- `backend_type`, whose record owns class metadata and any custom factory;
- `logical_type` and `strategy`;
- `operation`, `reducer`, and `handler`;
- `entity` and `extension`;
- `storage_profile`, `capability`, and `dialect`;
- `toolkit_family`, `mcp_profile`, `serializer`, and `interop_inferer`; and
- `capsule` or another existing inline-definition kind.

Kind names are data vocabulary, not new Python class hierarchies. A domain
facade decides which contract a restored value must satisfy.

## Why No New Component Spec Is Needed

The registration transport is a versioned JSON/YAML mapping called a
**registration record**. It is not a new Entity or universal runtime model. A
private `TypedDict` may later improve static checking, but it must not become an
authority parallel to Registry or MetaSchema.

The persisted value deliberately uses the existing non-identity portion of the
MetaSchema shape: `name`, `desc`, `source`, `target`, `active`, and `meta`.
No new top-level Entity field is required. Field-by-field ownership is:

| Concern | Existing owner or vocabulary | Decision |
| --- | --- | --- |
| kind and canonical identifier | Registry canonical key | Store as `(kind, identifier)`, not duplicated in the value |
| aliases | Registry alias index | Store typed aliases such as `(kind, alias)` |
| families, tags, built-in provenance, bundle membership, scope | Registry groups | Use typed groups; mirror safe values to MetaSchema |
| record grammar evolution | MetaSchema `meta` | Standardize `meta.schema_version`; do not add a column or public Spec |
| primary executable/inline restoration | MetaSchema `source` and `target` | Standardize source kinds and locator interpretation |
| optional restoration fallback | Capsule `restore_layers` | Reuse as `meta.restore_layers` only when more than the primary locator is needed |
| name and description | MetaSchema `name` and `desc` | Use those exact field names |
| enablement/lifecycle | MetaSchema `active` | Use `active`; keep richer retired/error detail in `meta.status` only when needed |
| implementation version | Extension/Capsule version vocabulary | Keep as `meta.version` |
| domain definition | EntitySchema, logical-type/strategy descriptors, Extension mapping, Backend metadata | Preserve the existing kind-specific dictionary inside `meta`; do not flatten it into a universal schema |
| custom Backend construction | existing `BackendBuilder` callable | Optional Backend-only `meta.factory` using the same `source`/`target` locator shape; otherwise use the restored class's `from_config` |
| runtime construction data | Backend mapping, `WorkspaceSpec.config`, ConfigSpec | Preserve under the existing config owner; do not turn ConfigSpec into registration identity |
| dependencies/compatibility | Capsule `dependencies` | Reuse as `meta.dependencies` only where needed |
| capabilities | existing domain metadata and Capsule vocabulary | Keep in the kind-specific payload or `meta.capabilities` |
| provenance | Capsule provenance vocabulary and Registry groups | Installer assigns groups; keep structured detail in `meta.provenance` |
| integrity/trust | Capsule `fingerprints` and `trust` | Reuse under `meta` for path/artifact records; do not trust self-asserted values |
| MetaSchema `dtype`, `backend`, `type`, `strategy`, `ws`, and `field` | existing query columns | Populate only when the registration kind has that established meaning; do not create generic substitutes |
| revision and snapshot digest | Registry state | Do not duplicate as mutable record fields |
| user-facing inspection | MetaSchema | Project Registry records; an Entity-definition command may delegate immediately to schema authority, but the stored projection is never runtime authority |
| concrete object discovery | Catalog | Leave unchanged |

A minimal record can therefore remain data:

```yaml
name: SQLite Backend
desc: SQLite SQL-family Backend
source: python-import
target: heavenbase.backends.sql.sqlite:SQLiteBackend
active: true
meta:
  schema_version: 1
  version: 0.1.2.1
```

The folder manifest is only a transport envelope. It supplies Registry keys and
indexes without persisting them twice inside each value. `manifest_version` is
the only envelope-version field; the item body already follows Registry plus
MetaSchema vocabulary:

```yaml
manifest_version: 1
items:
  - kind: backend_type
    identifier: sqlite
    aliases: [sqlite3]
    groups:
      family: [sql, storage_row]
    name: SQLite Backend
    desc: SQLite SQL-family Backend
    source: python-import
    target: heavenbase.backends.sql.sqlite:SQLiteBackend
    active: true
    meta:
      schema_version: 1
      version: 0.1.2.1
```

The installer converts the envelope to the tuple key, aliases, groups, and
MetaSchema-shaped value. Trusted install policy, not a developer's
self-assertion, adds `system`, `builtin`, or verified provenance groups.
Restoration layer variants initially cover:

- `python-import`: an installed Python module and qualified name;
- `path`: a canonical local package/artifact path plus module and qualified
  name;
- `inline`: an existing JSON-safe Entity/Capsule/domain definition;
- `capsule`: an existing captured code reference; and
- `artifact`: an already acquired immutable artifact coordinate and verified
  fingerprint.

The primary layer uses `source` and `target`. When fallback layers are useful,
`meta.restore_layers` reuses Capsule's list-of-mappings vocabulary and each
layer carries the same `source`/`target` pair plus kind-specific details. Inline
payloads stay inside `meta` in their existing domain dictionary shape; the
domain loader, selected by the Registry key kind, validates them. Secret or
machine-specific resolved configuration must not be copied into a registration
record or MetaSchema.

## Catalog and MetaSchema Ownership

`Catalog` remains the directory of concrete entity instances. It must never own
Python module definitions, loader paths, Backend types, or extension
registrations.

`MetaSchema` remains the global engine-facing metadata table. Once a workspace
has enabled its required system surface, it mirrors safe `system-modules`
records:

| Registration information | MetaSchema projection |
| --- | --- |
| key kind | `kind` |
| key identifier | `subject_id` |
| name and description | `name`, `desc` |
| Registry tag/family/provenance groups | `tags` and safe values in `meta` |
| selected restoration kind | `source` |
| module/qualified name, local artifact, or definition reference | `target` |
| active/retired status | `active` |
| domain metadata, aliases, Registry revision/fingerprint | redacted `meta` |
| workspace/scope group | `ws` where applicable |

Projection records the source Registry revision or digest so staleness is
detectable. Projection failure never corrupts or replaces Registry. Listing
registrations directly remains available before MetaSchema exists.

Catalog and MetaSchema are the only semantically privileged global domain
Entity types. They may be initialized directly after Registry opens and the
minimal schema vocabulary is resolvable. Capsule, Tool, Toolkit, MCP profile
metadata, and other current system-extension content must migrate to ordinary
registered items. Catalog and MetaSchema can themselves have registration
records for inspection; their special status concerns global behavior, not a
different source loader.

## Registration, Installation, and Loading

### Local item layout

The recommended developer layout is co-located:

```text
my_item/
    registration.yaml
    package_or_module/
    tests/
    README.md
```

One manifest may contain several registration records when an extension
contributes an entity bundle, handlers, toolkit families, and an API together.
The existing `Extension` contract remains the domain bundle description;
Registry `bundle` groups connect its registrations. No `BundleSpec` is added.

### Atomic installation

Registry gains a batch mutation primitive that validates and publishes a whole
manifest in one revision and one durable compare-and-set. Installation follows:

1. parse the plain manifest without importing implementations;
2. validate schema version, keys, aliases, groups, restoration layers, and
   existing domain dictionaries;
3. canonicalize paths and verify installer-owned fingerprints/trust policy;
4. reject duplicate ownership, alias collisions, unmet hard dependencies, or
   unsupported layers;
5. publish all records and indexes atomically; and
6. materialize nothing until a consumer asks for an item.

On a CAS conflict, refresh, revalidate the entire candidate set, and require an
explicit retry. A partial bundle is never observable.

### Built-in resources

Built-in records live in cohesive YAML manifests under the dedicated
`src/heavenbase/resources/registry/` package-resource hierarchy. After the
control Registry opens, Context enumerates those files in deterministic path
order, parses and validates the complete set, aggregates records by
`(kind, identifier)`, and publishes them through one ordinary installer
mutation. There is no generated aggregate or implementation-package scan.

Reconciliation is idempotent by the aggregate manifest fingerprint and
ownership. It may update a record previously owned by the same packaged source,
but it must fail rather than silently overwrite an external owner. Packaged
resources are not queried by runtime resolvers.

### Runtime resolution

Every domain facade follows one algorithm:

1. resolve `(kind, identifier_or_alias)` in `system-modules`;
2. choose the first locally supported, policy-accepted restoration layer;
3. restore the inline definition or import code from the registered target;
4. validate identifier, class/protocol, domain definition, and compatibility;
5. cache the materialized value by Registry revision plus record fingerprint;
   and
6. return it through the existing domain API.

There is no `try built-in map, then Registry` branch. A missing registration is
an actionable error. Registry changes apply to future resolutions and new
workspaces; already imported Python classes are not hot-swapped unless a domain
explicitly defines safe replacement semantics.

### Developer-facing lifecycle

The standalone storage injection surface preserves the current distinction
between process and durable namespaces:

```python
scratch = Registry("scratch")
shared = Registry.load("shared", store=store)
```

HeavenBase adds one service over `Context.registry("system-modules")`,
provisionally exposed as `Context.modules()`. It is not another Registry,
Catalog, Entity, or source of truth. Its minimum lifecycle is:

| Operation | Contract |
| --- | --- |
| `register(kind, identifier, record, aliases=(), groups=())` | Validate and atomically create or same-owner reconcile one programmatic registration; ownership conflicts fail |
| `install(source)` | Parse and atomically register one manifest mapping or co-located folder; return its keys/revision/fingerprint receipt |
| `resolve(kind, identifier)` | Registry-first restore, domain validation, and revision-keyed cache lookup |
| `inspect(kind=None, identifier=None)` | Return safe records/index metadata without importing implementations |
| `refresh()` | Pull the newer durable Registry revision and invalidate affected object-cache entries |
| `retire(kind, identifier)` | Set `active=False` while retaining metadata and ownership history |
| `unregister(kind, identifier)` | Explicitly remove one record and its aliases/groups after dependency checks |
| `uninstall(receipt_or_bundle)` | Atomically remove all records owned by one installation after dependency checks |

Typed domain APIs call the same resolver and remain the normal consumer
surface. `register` and `install` are the developer entry points for “at any
time, anywhere”; filesystem location affects only the persisted `source` and
`target`, never consumer syntax.

## Implemented Source Ownership

```text
src/heavenbase/utils/registry/     # self-contained stdlib-only authority
src/heavenbase/registry/           # HeavenBase integration and public re-export
    __init__.py
    _backend_store.py              # control Backend -> RegistryStore adapter
    modules.py                     # system-modules access and typed resolution
    registration.py                # plain-record validation and atomic install
    workspace.py                   # existing WorkspaceSpec domain codec/facade
src/heavenbase/backends/
    _construction.py               # one Backend mapping normalizer/constructor
src/heavenbase/resources/registry/
    backends/                       # Backend, capability, and dialect manifests
    query/                          # operation, reducer, and handler manifests
    schema/                         # logical type, strategy, profile, and text manifests
    extensions/                     # extension, toolkit, and Capsule manifests
    interop.yaml                    # interop inference manifest
```

The exact private filenames may change, but dependency direction may not:

```text
heavenbase.utils.registry <- HeavenBase adapter <- Context/control Backend
                    <- module installer/resolver <- typed domain facades
```

## Completed Refactor Route

The following slices record the completed implementation order and their
architectural exit gates. Artifact acquisition remains the explicit later slice.

Each slice removes the superseded path in the same change. Do not keep a
permanent dual-write or built-in fallback during migration.

### Slice 0 - Freeze contracts and the architecture

Files:

- this ADR and the docs menu/roadmap;
- `tests/config/test_bootstrap.py`;
- `tests/core/test_context.py`;
- new configuration-source parity tests.

Work:

- characterize the current bootstrap order and Registry persistence bytes;
- prove explicit/home/packaged mappings retain precedence;
- prove the complete SQL `_override` reaches the database resolver unchanged
  and does not read `CM_HVNB.default`; and
- prove a direct workspace constructor envelope and the same
  `WorkspaceSpec.config` envelope produce identical leaf Backend mappings and
  construct equivalent Backends.

Exit gate: tests fail on the current `_override = {}` replacement and protect
the intended cycle before implementation moves.

### Slice 1 - Consolidate the self-contained Registry kernel

Files:

- consolidate generic code under `src/heavenbase/utils/registry/` and remove
  the temporary top-level package boundary;
- split snapshot encoding from `src/heavenbase/registry/_backend.py`;
- add `tests/registry/test_registry_kernel.py` and fake-store contracts.

Work:

- remove imports of `heavenbase.utils.ids`, config, Backend, Entity, types,
  strategies, and storage bindings;
- add `RegistryStore` injection and preserve existing process/persistent API
  semantics, codecs, revisions, corruption checks, bounds, and async wrappers;
- add atomic multi-record mutation at the state layer; and
- change internal imports to the one canonical implementation.

Exit gate: the copied kernel passes its contract tests as an isolated temporary
`registry` package with only the standard library importable. Importing that
extracted test package loads no `heavenbase` module.

### Slice 2 - Reattach HeavenBase persistence

Files:

- reduce `src/heavenbase/registry/_backend.py` into
  `src/heavenbase/registry/_backend_store.py`;
- update `src/heavenbase/context.py` to inject the adapter;
- retain and extend `tests/registry/test_registry_backend.py` and Backend CAS
  contracts.

Work:

- adapt `ensure`, `get_many`, and `compare_upsert` to `RegistryStore`;
- preserve existing on-disk namespace payloads or provide an explicit one-time
  migration with rollback evidence;
- keep `sys-registry-state` private and absent from Entity discovery; and
- prove refresh/conflict behavior across two Contexts/processes.

Exit gate: current Registry persistence tests pass unchanged at the public
boundary, standalone and HeavenBase stores share one contract suite, and Context
never creates a separate Registry database.

### Slice 3 - Unify Backend construction and repair bootstrap

Files:

- `src/heavenbase/bootstrap.py`;
- `src/heavenbase/context.py`;
- `src/heavenbase/backends/_construction.py`;
- `src/heavenbase/backends/registry.py`;
- `src/heavenbase/database/database.py`;
- workspace construction tests.

Work:

- extract one flat-mapping normalizer and constructor;
- preserve bootstrap `_override` and make the complete override independent of
  `CM_HVNB`;
- use direct `module` resolution only when no Registry has been injected;
- adapt a located Backend class or optional registered custom builder to the
  one `BackendFactory(name, provider_config)` contract;
- route post-bootstrap Backend type/factory resolution through one
  `system-modules` record; and
- keep `WorkspaceConstruction`, presets, manifests, and `WorkspaceSpec.config`
  as the existing replay format.

Exit gate: bootstrap and ordinary leaf mappings share the normalizer/factory,
direct and Registry-loaded workspace envelopes produce equivalent Backend specs
and behavior, and an out-of-package control Backend works from an explicit
bootstrap module.

### Slice 4 - Add the registration record and atomic installer

Files:

- new `src/heavenbase/registry/registration.py` and `modules.py`;
- Registry batch mutation tests;
- local-folder installer and failure/concurrency tests.

Work:

- validate the manifest envelope, MetaSchema-shaped record, `source`/`target`,
  and optional `meta.restore_layers` without a public `ComponentSpec`;
- store records under tuple keys with Registry aliases/groups;
- implement import, path, and inline restoration first;
- register a multi-item folder in one CAS revision; and
- expose the register/install/resolve/inspect/refresh/retire/unregister lifecycle
  over the one Registry; and
- keep list/inspect paths import-free.

Exit gate: a folder outside `src/heavenbase` can register an item in one process,
another process can refresh and use it, and a single invalid contribution leaves
the Registry revision and every item unchanged.

### Slice 5 - Install packaged items through the same path

Files:

- modular built-in YAML files under `src/heavenbase/resources/registry/`;
- deterministic resource enumeration and aggregation in `ModuleService`;
- Context module bootstrap wiring.

Work:

- validate and aggregate every packaged manifest before publication;
- reconcile the complete set after Registry opens using the ordinary installer;
- assign `system`/`builtin` provenance in the trusted packaged installer; and
- forbid implementation-package scans and packaged-resource lookup by resolvers.

Exit gate: moving a built-in implementation plus its manifest to an external
test package requires no consumer or resolver change and passes the same family
contract tests.

### Slice 6 - Migrate domain families by dependency order

For each family, add registration records, point the existing typed API at
`system-modules`, key its object cache by Registry revision/fingerprint, add an
external extraction test, and delete its fixed inventory/process authority in
the same PR.

Order:

1. operations, reducers, profiles, dialect/capability metadata, and other
   declarative low-dependency items;
2. logical types and storage strategies;
3. Backend classes, builders, families, and provider metadata;
4. handlers and compilers;
5. entities and extensions; and
6. toolkit families, MCP profiles, serializers, interop inferers, and Capsules.

The Backend slice folds `register_backend_class` and
`register_backend_builder` into one `backend_type` record plus the common
factory adapter. Existing facade calls may remain, but they must write and read
that record rather than two durable or process authorities.

Named removals include `_BUILTIN_BACKEND_MODULES`, provider default import
aliases, root `_bootstrap.py` type/strategy seeding, fixed handler plugin tuples,
fixed extension factory tuples, and equivalent package inventories. A removal
lands only after the family has Registry seed and extraction parity.

Exit gate per family: built-in and extracted-external implementations pass the
identical contract suite and consumer syntax; unknown items fail from Registry
without a package fallback.

### Slice 7 - Converge the system entities and root imports

Files:

- `src/heavenbase/workspace/_metaschema_registry.py`;
- `src/heavenbase/workspace/_metaschema_service.py`;
- `src/heavenbase/extensions/system/`;
- `src/heavenbase/__init__.py` and public/import-fitness tests.

Work:

- project `system-modules` records and Registry alias/group metadata directly
  into MetaSchema with source revision/fingerprint;
- keep Catalog free of registration rows;
- formalize Catalog and MetaSchema as the only special global domain Entities;
- migrate Capsule, Toolkit, Tool, MCP metadata, and other system content through
  ordinary records; and
- remove eager root registration/import side effects while retaining lazy
  `hb.Registry` and domain facades.

Exit gate: MetaSchema inspection does not import implementations, Catalog is
unchanged, and Registry-only imports do not load Context, ConfigManager,
Backends, workspace, Entity, logical types, strategies, or extensions.

### Slice 8 - Add artifact acquisition only after local parity

Work:

- define an explicit installer/acquirer boundary for immutable artifacts;
- require fingerprint, provenance, trust decision, local cache ownership, and
  rollback before registration;
- prohibit lookup-time downloads and uncontrolled `sys.path` mutation; and
- add adversarial path, digest, signature/trust, and rollback tests.

Exit gate: an acquired artifact is indistinguishable after registration, while
offline lookup, inspection, and failure behavior remain deterministic.

## Verification Matrix

The implementation is complete only when all of these properties are automated:

| Property | Required evidence |
| --- | --- |
| self-contained Registry | isolated subprocess imports the extracted kernel and only standard-library modules |
| store neutrality | the same contract suite passes process/fake, SQLite, and HeavenBase Backend stores |
| intended bootstrap | explicit, home, and packaged config construct the control Backend before Registry |
| bootstrap isolation | complete `_override` is preserved and does not consult Registry/ConfigManager |
| config source parity | direct and `WorkspaceSpec.config` copies of one constructor envelope yield identical normalized leaf mappings and behavior |
| one Backend factory | direct class loading and Registry class/custom-factory loading both call the same `BackendFactory` contract |
| external control Backend | explicit out-of-package module can open the central Registry |
| Registry-first loading | every migrated family resolves the key before importing; no built-in fallback exists |
| import-free inspection | list, aliases, groups, and MetaSchema projection import no implementation module |
| atomic bundle | any validation or CAS failure publishes no item, alias, group, or revision |
| cross-process use | another process refreshes and materializes a newly registered external item |
| developer lifecycle | register/install/inspect/resolve/refresh/retire/unregister/uninstall preserve one Registry authority and dependency checks |
| extraction parity | moved built-in code passes the same consumer/contract tests outside the package |
| special entities | only Catalog and MetaSchema have privileged global domain behavior; control rows stay private |
| projection ownership | MetaSchema reports Registry revision/fingerprint; Catalog receives no module record |
| consumer parity | consumer calls and results do not vary with built-in/external provenance |
| no new parallel schema | no `ComponentCatalog`, `ComponentSpec`, bundle Spec, or second registry authority exists |

Required gates for the completed train include targeted Registry/bootstrap/
workspace tests on every slice, `rtk bash scripts/flake.bash --ci`, the default
suite, the full suite at the final convergence gate, capability regeneration
when handler metadata changes, environment drift checks, and a clean build.

## Risks and Controls

- **Bootstrap trust:** direct bootstrap modules execute before Registry policy.
  Restrict them to explicit local machine configuration; never accept an
  incidental remote URL.
- **Existing bootstrap defect:** losing `_override` can silently reintroduce the
  cycle through `CM_HVNB`. Characterization tests land before refactoring.
- **Snapshot size:** one `system-modules` namespace may approach the current
  4 MiB bound. Measure encoded size and churn before changing granularity; do
  not preemptively split authority.
- **Concurrent installers:** one CAS covers the entire manifest. Conflicts cause
  refresh and full revalidation, never automatic partial retry.
- **Stale Python objects:** Python modules are not generally hot-reloadable.
  Revision-keyed caches affect future materialization/new workspaces unless a
  domain explicitly supports safe replacement.
- **Stale MetaSchema:** record source revision/fingerprint and rebuild the
  projection; never read it as the loader authority.
- **Built-in ownership:** idempotent packaged-resource reconciliation may update only its own
  fingerprinted records. Identifier takeover fails explicitly.
- **Path and artifact security:** canonicalize paths, bind fingerprints to
  bytes, and separate acquisition from resolution.
- **Secrets:** registration records and MetaSchema are inspectable; resolved
  credentials remain in configuration/runtime owners.
- **Migration ambiguity:** each family removes its prior authority when it
  switches. Temporary test adapters may exist inside one PR, but no released
  dual truth is accepted.

## Consequences

### Positive

- Any developer can add an item without modifying HeavenBase source.
- Built-ins continuously test the extension mechanism instead of bypassing it.
- Registry can serve other Python code without dragging in HeavenBase's runtime.
- The existing Catalog, MetaSchema, Extension, WorkspaceSpec, Backend config,
  and domain schemas keep their meanings.
- Persistent metadata can be listed and audited without importing arbitrary
  code.

### Negative

- Registry records and loaded-object caches become separate concepts that
  extension authors must understand.
- The control Backend module remains an explicit trusted bootstrap escape hatch.
- Atomic installation, ownership, artifact trust, and migration require stronger
  tests than fixed package imports.
- A dependency-ordered family migration is substantial and must not be hidden
  behind permanent compatibility fallbacks.

## Rejected Alternatives

### A Registry database opened before the control Backend

Rejected. It defeats the intended bootstrap cycle and creates a second storage
authority. The explicit control Backend mapping exists specifically to break the
cycle.

### `ComponentCatalog`

Prohibited. Catalog already means concrete-object discovery, while MetaSchema is
the engine-facing global metadata table. Another catalog would fracture the two
highest-level global entity contracts.

### Generic `ComponentSpec` or `BundleSpec`

Rejected. Registry owns identity/indexing, existing domains own definitions and
configuration, Capsule vocabulary covers restoration/dependency/trust concerns,
and Extension already owns entity bundles. A plain mapping plus validators is
sufficient.

### Store imported classes directly in durable Registry values

Rejected. Imported objects are not portable, safely serializable, inspectable
without execution, or stable across processes. Persist restoration data and
cache validated objects in process.

### Runtime scanning of package folders or Python entry points

Rejected as an authority. Discovery may help an explicit installer locate a
manifest, but runtime resolution must use the persisted Registry record.

### Implicit remote loading during lookup

Rejected. It makes ordinary use depend on network availability and turns a data
lookup into unreviewed code acquisition and execution.

### A separately installable Registry distribution

Rejected. Registry is shared HeavenBase infrastructure and therefore belongs
under `heavenbase.utils.registry`. The extraction fitness test protects its
standard-library dependency boundary without creating another public package or
distribution identity.

## Implemented Scope and Follow-On

The stable `0.1.2.1` source candidate implements the bootstrap precedence,
control-Backend-before-Registry boundary, self-contained Registry utility, common Backend
constructor, `system-modules` authority, registration installer, built-in resources,
Registry-first family migration, and special-entity convergence described here.
The published `0.1.2.0` package remains unchanged.

The original resolver supported installed Python imports, fingerprinted local paths,
detached inline definitions, and existing Capsule references. The HB-003 amendment
above replaces physical-path restoration with local content-addressed artifacts and
defines immutable resolver-local cache identity. Remote acquisition, signatures, and
ecosystem trust policy remain follow-on work; ordinary lookup never performs network
acquisition.
