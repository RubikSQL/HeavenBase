# Workspace Foundation and Extensions

Status: current for the `0.1.2.3` train.

This page owns the workspace foundation and extension-lifecycle contract. Read
[the module-folder protocol](adr/0012-unified-module-folder-protocol.md) for
artifact acquisition and [Capsule, Toolkit, and MCP](capsule-toolkit.md) for
executable persistence.

## Composition Layers

HeavenBase keeps four distinct layers:

| Layer | Current owners | Lifecycle |
| --- | --- | --- |
| Context mechanisms | Registry, ModuleService, configuration, Backend definitions | Installed and resolved before workspace policy |
| Fixed workspace foundation | Catalog and MetaSchema | Exact code-owned classes bootstrapped for every workspace |
| Required and optional domains | required Capsule; optional Toolkit and domain bundles | Activated per workspace through Registry-defined Extensions |
| Conditional contributions | cross-domain Registry bridges | Eligible only when their workspace requirements are active |

`built-in`, `required`, `default`, and `active` are different properties:

- **built-in** is trusted package provenance assigned by the installer;
- **required** is an invariant of every usable workspace and may be declared
  only by an installer-trusted built-in Extension;
- **default** is configuration applied when a workspace first snapshots its
  requested roots;
- **active** is the resolved runtime closure for one workspace.

Installing a module makes its definitions available. It does not activate an
optional Extension in any workspace.

## Fixed Workspace Foundation

Catalog and MetaSchema are the only fixed workspace Entity contracts:

- `sys-catalog` is the directory of concrete workspace objects;
- `sys-metaschema` is the queryable projection of schemas, placement, Backends,
  exact handler support, safe Registry metadata, and active Extensions.

Semantic module claims remain solely on subject Registry records. MetaSchema
redacts `meta.tags` and does not flatten Boolean or value tags into its search
labels; callers use the owning Context's `ModuleService.tag(...)` /
`.tags(...)` API instead.

Workspace bootstrap registers the exact
`heavenbase.workspace.catalog.Catalog` and
`heavenbase.workspace.metaschema.MetaSchema` classes before activating any
Extension. Their identifiers and structures cannot be replaced through
`register`, `alter_entity`, MetaSchema Entity-definition ingress, manifest
replay, or an Extension, including when a generic path offers
`allow_replace=True`.

Their module records remain inspectable, but those records do not make the
classes replaceable Extension content. Additional metadata belongs in a
separate Entity joined through stable Catalog or MetaSchema identifiers; it
does not add fields to either fixed schema.

## Required Capsule and Optional Domains

Capsule is a separate universal domain, not part of the fixed Catalog and
MetaSchema kernel. The installer-trusted `capsule` Extension is marked
`required`, so every workspace activates `sys-capsule` through the ordinary
dependency planner. Programmatic Extensions cannot self-assert
`required=True`.

Toolkit is not universal. The `toolkit` Extension owns `sys-toolkit`,
Toolkit persistence, serializers, profiles, and MCP composition, and has a
hard activation dependency on `capsule`. Runtime `Tool` and `Toolkit` values
may exist in memory without activating Toolkit persistence in a workspace.

The packaged default root list is currently:

```yaml
heavenbase:
  extensions:
    default:
      - toolkit
      - prompt
```

This is creation policy, not privilege. Agent, Memory, and Database remain
opt-in. Prompt and Toolkit require Capsule. Agent, Memory, and Database remain
independent base domains; their Toolkit integrations are conditional
contributions rather than hard dependencies.

Agent still owns its current Message, Session, Skill, and Agent Entity
definitions. The accepted memory-first redesign of Message and Session
persistence remains deferred to the
[LLM session persistence boundary](llm-session-persistence.md).

## Three Dependency Contracts

One generic dependency field cannot express all lifecycle boundaries:

| Contract | Descriptor location | Meaning |
| --- | --- | --- |
| Installed-definition dependency | record `meta.dependencies` | Required records exist before publication and are removal-protected |
| Workspace activation dependency | Extension definition `requires` | Exact Extension identifiers are activated dependency-first for one workspace |
| Conditional contribution requirement | record `meta.workspace_requires` | Usable only when every named Extension is active |

`Extension.requires` forms a deterministic exact-identifier DAG. Activation
reports the full cycle path and preflights fixed-schema ownership, Entity
structure conflicts, and workspace API-name conflicts before ordinary schema
mutation. Dependencies activate before their consumer.

`workspace_requires` is an eligibility predicate, not an activation request.
Toolkit/profile/serializer and similar consumers filter Registry inspection
through the shared contribution helper. Resolving a contribution must not
silently activate its required domains. This exact-id structural dependency is
deliberately separate from the implemented
[canonical tags architecture](canonical-tags.md): an author-defined semantic
claim cannot activate an Extension or replace dependency closure.

## Requested Roots, Defaults, and Replay

A tracked workspace durably stores **requested roots**, not a second writable
copy of its resolved closure:

1. On first construction, `heavenbase.extensions.default` supplies the optional
   roots unless the caller or replay manifest supplies an explicit list.
2. The chosen list is stored in `WorkspaceSpec.extension_roots`.
3. Reopening activates trusted required Extensions, then recomputes the hard
   dependency closure of the stored roots.
4. `ws.enable_extension("x")` activates `x`, then appends and persists `x` as a
   requested root only after activation succeeds.

Required and transitive dependency Extensions are not added as user-requested
roots. A legacy workspace whose stored roots are `None` snapshots current
defaults once; an explicit empty list is authoritative. Later global-default
changes affect new or unsnapshotted workspaces, not an existing workspace.

MetaSchema reports the resolved active closure but is not activation authority.
Workspace extension metadata lists the entities registered during activation,
including declarations returned by the setup hook. The same list controls
which extension entities `ws.list_entities()` hides when the extension sets
`hide_from_list_entities`, and which schemas a default manifest leaves to the
extension's activation. Activation registers these definitions again each time
the workspace opens, so they belong to the extension: `register`,
`alter_entity` and `describe` raise `ValueError` for a change to their
structure, `catalog` or `gram` option, or metadata (name, descriptions,
tags and examples), and accept a call that
changes nothing. Manifest replay skips a definition with the extension's
structure and options, with a `UserWarning` naming metadata it states
differently, and raises `ValueError` for any other before it registers any
definition.
Generated workspace manifests normally serialize the requested roots, including
roots that originally came from defaults. A detached workspace keeps its roots
only in its live facade or explicit manifest because it has no durable
`WorkspaceSpec`.

Activation preflights predictable failures, but arbitrary physical Backends do
not form a distributed schema transaction. A requested root is not persisted
when activation fails; a failure after a dependency has performed physical
work may require repair followed by an idempotent retry.

## Monotonic Workspace Activation

HeavenBase has no generic `disable_extension` or workspace unload operation.
Schemas, rows, APIs, derived effects, and lineage have domain-specific
semantics that cannot be reversed by a universal technical toggle. An
Extension may expose an honest domain cleanup workflow when it owns and can
explain all affected state.

Module installation lifecycle is separate. `ModuleService.uninstall(...)`
removes an exact installed definition generation under Registry dependency
rules; it is not a request to undo an Extension's effects in existing
workspaces.

Generic CRUD never activates an Extension or registers a schema as a row-side
effect. An Extension-owned API may explicitly ensure its own bundle before
persisting domain rows. That remains ordinary monotonic activation through
`ws.enable_extension(...)`, not a second schema-registration path.

## Self-Contained Artifact Folders

Optional built-in and external domain code follows the same ADR 0012 folder
protocol:

```text
example/
  __init__.py
  meta.yaml
  entities.py
  extension.py
  toolkit/              # optional contribution roles
```

An author-declared top-level or nested `source: path` reference causes the
complete folder resource to be captured into the content-addressed artifact
store before Registry publication. Targets use artifact-root-relative
`{module, qualname}` values. The persisted source becomes installer-owned
`artifact`; authors never write that form directly.

Folder-internal imports must stay within the captured root. Cross-domain code
uses a stable absolute HeavenBase owner API or a Registry contribution
contract; it must not escape through relative imports into a sibling optional
folder. Generated caches and bytecode are excluded from capture and never
become artifact content. Built-in package resources, ZIP-backed resources, and
external folders converge on the same verified artifact representation.

The Registry, not the Python package path, is activation and resolution
authority. Public package facades are exposure surfaces; they are not a hidden
built-in inventory.

## Class Identity Boundary

Artifact modules load under a private namespace derived from coordinate,
generation, and content identity. Consequently, a restored Entity class is
local to its `ModuleResolver` and installation generation:

- the root dynamic exports such as `hb.Agent` resolve through
  `DEFAULT_CONTEXT`;
- another Context may restore a different Python class object for the same
  Registry Entity identifier;
- reinstalling a folder may publish a new generation and therefore a new class
  object;
- a direct implementation-package import is not the workspace's registration
  authority.

Workspace schema services own one canonical class per Entity identifier. Row
operations validate a supplied class or instance against that registered
identifier and structural schema, then continue with the workspace's canonical
class. Cross-Context code must not use Python `is`, `__module__`, or ambient
`isinstance` checks as durable Entity identity. Structural comparisons
canonicalize source-backed artifact callables to their relative module,
qualified name, and source hash; private generation namespace alone is not
semantic identity. Use the Registry identifier, schema contract, and the owning
Context's resolver.

Catalog and MetaSchema are the deliberate exception: fixed workspace bootstrap
requires their exact code-owned class objects.

## Current Package Ownership

| Folder | Workspace role |
| --- | --- |
| `workspace/catalog/` | fixed Catalog class and service |
| `workspace/metaschema/` | fixed MetaSchema class and projection service |
| `capsule/` | required Capsule domain, Entity, layers, and row-store protocol |
| `toolkit/` | optional Toolkit/Tool/MCP domain and conditional contribution consumers |
| `extensions/prompt/` | optional Prompt and Translation bundle |
| `extensions/agent/` | optional Agent-domain bundle and Toolkit bridge records |
| `extensions/agent_log/` | optional `agent-log` bundle: the `agent-trace` event entity of the dashboard assistant session log; never a default |
| `extensions/memory/` | optional Memory bundle and Toolkit bridge records |
| `extensions/database/` | optional database-metadata bundle and Toolkit bridge records |

Capsule owns no Toolkit persistence verbs. `CapsuleRegistry` consumes an
injected Capsule row-store port. Toolkit depends one-way on Capsule and composes
both Registry services over one explicit application workspace for concrete
Toolkit and referenced Capsule rows. Context-private Capsule stores remain an
advanced standalone/module administration path, not Toolkit persistence.

## Verification Anchors

- workspace-foundation tests cover exact fixed classes and replacement
  rejection;
- extension tests cover trusted required activation, dependency ordering,
  cycles, preflight, roots, default snapshots, reopen, and no unload surface;
- contribution tests cover exact `workspace_requires` filtering without
  implicit activation;
- module artifact tests cover package/external parity, nested executable
  ownership, private namespaces, resources, and class isolation.
