# ADR 0012 - Unified Module Folder Protocol

Status: implemented; artifact and package-resource restoration amended in
`0.1.2.1`

Date: 2026-07-22

Implemented: `0.1.2.1` source candidate

Closeout evidence: the completed `HB-002` work is preserved in Git history.

Working input at implementation time: `docs/scratch/RAW.md`, a
non-authoritative provenance note (alongside the consolidated SDK thought
experiment) that reached its recorded scratch expiry and now lives in Git
history only.

Supersedes in part: [ADR 0011](0011-standalone-registry-and-lego-resolution.md), limited to the descriptor filename, centralized built-in resource layout, and first-`Context.modules()` discovery timing. ADR 0011's standalone Registry and Registry-first runtime decisions remain authoritative.

## 2026-07-22 Artifact Amendment

The folder protocol also defines the capture boundary for a local module.
An installable path is the root package folder containing both `__init__.py` and the
exact `meta.yaml`, or that exact descriptor file. A mapping manifest remains valid for
records that do not need relative path restoration.

Before publishing any author-declared `source: path` item, `ModuleService` captures the
complete root package into a content-addressed artifact store. Capture excludes
generated caches and bytecode and rejects symlinks, special files, escaping paths, a
missing or untraversable root-package entry, and a tree whose whole-root content
digest does not match across two complete reads. The persisted record contains the
artifact digest plus a package-relative module and qualified name; the caller's
absolute path is neither authority nor installation identity. A persistent module
catalog requires an explicitly durable artifact store for path installation.

Resolution re-reads and verifies the stored artifact, then loads it under a private
namespace derived from content identity. Coordinate and installation generation keep
independent installs isolated even when their author package names match. The loader
does not prepend `sys.path` or publish the author's package names in `sys.modules`;
relative imports, virtual `__file__`, and `importlib.resources` resolve against only
the verified generation bytes. A changed source folder has no effect until a new
installation captures and publishes a new artifact.

`artifact` is therefore an installer-owned persisted source form, not a value authors
may self-assert in `meta.yaml`. This amendment covers local acquisition and immutable
restoration only. Remote transport, signatures, publisher verification, revocation,
and ecosystem trust policy remain outside this ADR.

The same capture boundary applies to tagged executable references nested inside an
inline record definition. A folder author may declare the reference with
`source: path` and a package-relative `{module, qualname}` target. Installation
rewrites it to a versioned `heavenbase.executable` artifact reference using the
folder's captured digest. Resolution requires the owning record, so the nested
callable restores under that record's coordinate and generation rather than through
ambient imports. Explicit `python-import` and already-registered `capsule` references
remain available when those are the intended executable owners.

## 2026-07-24 Package-Resource and Extension Amendment

Trusted package discovery retains the resource folder containing each
`meta.yaml`, not only the parsed mapping. When a packaged descriptor contains a
top-level or nested `source: path` reference, setup captures that complete
folder through the same content-addressed representation as an external
filesystem install. Ordinary directories and ZIP-backed package resources
therefore converge before publication.

A `source: path` folder is self-contained. Internal relative imports stay
within its captured root; cross-domain dependencies use a stable absolute owner
API or a Registry contribution contract rather than escaping into a sibling
optional folder. Generated caches and bytecode never enter the artifact and do
not make setup depend on whether implementation code was imported earlier.

Artifacts restore under coordinate/generation-private namespaces. Python class
identity is therefore local to one resolver and installation generation.
Registry identifiers and validated domain contracts are durable identity;
workspace Entity ingress canonicalizes a compatible class to the class already
registered in that workspace. Fixed Catalog and MetaSchema bootstrap is the
intentional exact-class exception.

Module installation remains separate from workspace Extension activation. The
fixed kernel, required Capsule, optional domains, requested-root durability, and
conditional contribution rules are current in
[Workspace Foundation and Extensions](../workspace-foundation-and-extensions.md).

## Context

ADR 0011 established one persisted `system-modules` Registry, one ordinary installer and resolver path for built-in and external contributions, import-free inspection, and failure-atomic publication. Its first implementation kept all built-in descriptors under `src/heavenbase/resources/registry/`, used `registration.yaml` for external folders, and scanned packaged manifests on the first `Context.modules()` call.

That implementation gives consumers one runtime authority, but it does not give developers a standalone lego piece. An Elasticsearch change, for example, crosses a provider backend package, a separate handler package, and central registration files. The physical folder does not state the complete contribution it owns, and opening a runtime service still performs installation work as a side effect.

HeavenBase needs one authoring model broad enough for Backends, handlers, strategies, extensions, entities, toolkits, and the other existing Registry kinds without introducing another universal runtime abstraction.

## Decision

### A module is a folder protocol

A HeavenBase module is a cohesive folder containing one exact `meta.yaml` descriptor. It is a packaging and registration protocol, not a `Module` superclass, runtime container, service locator, or second component model. Existing domain objects and Registry kinds keep their own vocabulary and validation.

One `meta.yaml` may contribute any compatible mixture of ordinary Registry records, including `backend_type`, `handler`, `strategy`, `extension`, `entity`, `toolkit_family`, profile, dialect, operation, reducer, serializer, inferer, and Capsule-layer records. A folder boundary expresses one reason to ship and change together; it does not force every record into one generic Python interface.

The minimum module is:

```text
example/
    __init__.py
    meta.yaml
```

Implementation roles may be ordinary modules or optional subpackages when they need room to grow:

```text
example/
    __init__.py
    meta.yaml
    backend/
        __init__.py
        runtime.py
    handler/
        __init__.py
        runtime.py
    strategy/
        __init__.py
    extension/
        __init__.py
```

Role folders are organizational, not required framework interfaces. Provider-owned handlers belong with their provider module. A genuinely shared handler, strategy, or vocabulary remains its own cohesive module rather than being duplicated under every consumer.

Built-in and external modules use this same exact descriptor name and manifest shape. Installer-assigned provenance and trust may differ, but runtime registration, inspection, resolution, validation, caching, and failure behavior do not. The built-in package owner, module-set marker, and `provenance:system` / `tag:builtin` groups are reserved for the trusted setup path; an external folder cannot self-assert them. `registration.yaml` and arbitrary descriptor filenames are removed and are not accepted as fallback aliases, even when passed directly as files.

### Built-in discovery is setup work

`src/heavenbase/resources/modules.yaml` contains only trusted package scan roots. It contains no Registry items, implementation locators, provider inventory, or per-module metadata. The scanner recursively walks each declared package root in deterministic relative-path order and selects files whose basename is exactly `meta.yaml`. Discovery parses inert YAML and validates locators as data; it does not import or execute implementation modules.

Discovery may run only from an explicit setup boundary:

- the `hb setup` flow;
- `WorkspaceSpec.setup(...)`; or
- live workspace creation, which ensures setup before it resolves module-backed definitions.

Calling `Context.modules()` only returns the Context-owned `ModuleService`. `Context.modules()`, `inspect(...)`, and `resolve(...)` never scan package folders or read descriptors. After setup, the persisted Registry is the sole runtime authority.

Setup stores a durable `("module_set", "heavenbase")` marker with the installed HeavenBase module-set version. Each non-forced setup boundary refreshes the Registry revision before evaluating that marker. A matching marker lets later setup and workspace construction skip publication, including in a new process or a new `Context` over the same durable Registry; within one process, discovery reuses its readings of the package folders while their stat fingerprints are unchanged. A process older than the indexed marker fails instead of silently downgrading the shared Registry; `force=True` is the explicit downgrade escape hatch. Forced setup otherwise bypasses the marker and rescans, which supports development, repair, and an intentional package reconcile. Ordinary runtime lookup never turns a missing or stale marker into an implicit scan.

### Reconciliation is one owned transaction

A setup scan stages the complete built-in descriptor set before publication. It validates descriptor syntax, record shape, targets, aliases, groups, dependencies, duplicate keys, ownership, and the module-set marker as one candidate change. Any error or ownership collision leaves the prior Registry revision unchanged.

Successful reconcile publishes the complete set in one Registry mutation. It may create, update, or remove only rows owned by the HeavenBase package installation and updates the durable module-set marker in the same transaction. It never deletes, rewrites, or adopts an external owner's rows. An external record occupying a required built-in key is a conflict, not permission to overwrite it. Resolvers can therefore observe either the old complete package set or the new complete package set, never a partially migrated set.

Folder-local descriptors remain independently readable and installable, but built-in setup is atomic across the entire declared package scan set. Physical locality improves authoring and packaging; the Registry remains the logical authority.

### Reference layout and migration boundary

Elasticsearch is the reference provider slice:

```text
src/heavenbase/backends/elasticsearch/
    __init__.py
    meta.yaml
    backend/
        __init__.py
        runtime.py
    handler/
        __init__.py
        runtime.py
```

Its descriptor owns both the Backend registration and Elasticsearch-specific handler registration. The `backend/` and `handler/` packages replace the former single files so each role can grow without moving the module boundary.

SQL remains one family module for now. Its dialects, Backend types, and shared handlers have coupled definitions and no descriptor inheritance protocol. Splitting every SQL provider immediately would duplicate shared metadata or invent an unproven inheritance system. A later ADR may define descriptor composition and then divide the family without changing this folder protocol.

All built-in registration descriptors move out of `resources/registry/` into their owning folders. `resources/registry/` and `registration.yaml` are deleted without a compatibility shim, dual scan, redirect, or fallback branch. Owned imports and tests change in the same migration; historical documentation remains history and current documentation points to this decision.

## Consequences

- A developer can inspect one module folder to see the code roles and complete Registry contribution that ship together.
- Focused verification may remain in the repository-wide test surface when it exercises several modules; physical test co-location is not part of the descriptor protocol.
- Adding a new Registry kind does not require a new plugin framework. The folder protocol carries ordinary records, and the existing typed facade continues to validate and consume each kind.
- Provider-specific handlers can evolve beside their Backend, while shared family behavior retains a clear shared owner.
- `Context.modules()` becomes side-effect-free with respect to discovery and installation. Runtime latency and behavior do not depend on filesystem scans.
- Setup must be invoked before module-backed construction. Live workspace creation owns that guarantee for the normal user path; lower-level callers that deliberately bypass setup receive normal missing-registration failures.
- A package version change does not affect a durable installation until setup reconciles it. Normal workspace creation upgrades an older set, rejects an implicit downgrade from a newer set, and leaves forced setup as the explicit repair, development, or downgrade escape hatch.
- A malformed descriptor anywhere in the trusted built-in scan set blocks the whole reconcile. This is intentional because partial built-in publication would violate the package's tested composition.
- Wheels and source distributions must package every `meta.yaml` below the HeavenBase package, `resources/modules.yaml`, and module-local developer documentation such as the Elasticsearch `README.md`. Artifact tests must verify that source and distribution descriptor inventories agree.
- Descriptor paths are authoring and acquisition facts only. Consumers still identify pieces by Registry kind and identifier, never by source-folder location.

## Rejected Alternatives

### Keep centralized manifests under `resources/registry/`

This preserves a convenient aggregate inventory but makes central files change for every provider and prevents a provider folder from being a standalone developer unit. The durable Registry already supplies the aggregate runtime view.

### Introduce a universal `Module` base class

Backends, strategies, handlers, entities, and extensions have different runtime contracts. A universal superclass would either erase useful domain types or become an empty marker plus a second abstraction. The shared need is descriptor and installation protocol, not object inheritance.

### Scan when `Context.modules()`, `inspect(...)`, or `resolve(...)` is called

Lazy runtime scanning hides mutation behind reads, repeats work across Context instances, couples lookup to package layout, and makes inspection timing affect system state. Explicit setup plus a durable marker keeps reads deterministic.

### Publish each discovered folder independently

Per-folder commits could expose a mixed built-in version after an invalid later descriptor or a process failure. Staging all descriptors and publishing one package-owned mutation preserves the complete-set invariant.

### Nest every handler and strategy under a Backend

Provider-owned behavior benefits from locality, but generic fallback handlers and family-wide SQL behavior serve multiple Backends. Forced nesting would duplicate shared policy and make ownership less accurate. Cohesion, not a mandatory directory depth, chooses the module boundary.

### Split every SQL provider now

The current SQL family shares handler and dialect definitions. Splitting it before descriptor inheritance exists would duplicate those records or add special merge behavior. Keeping one SQL family module is the smaller honest boundary.

### Accept `registration.yaml` as a deprecated alias

A fallback would create two descriptor names, complicate deterministic discovery, and leave an indefinite compatibility path. This is an intentional pre-release authoring-protocol migration, so all owned callers move to `meta.yaml` together.
