# Canonical Tags

This document is the self-contained design handoff for the canonical-tags
refactor. It reconstructs the original problem from the checked-in `0.1.2.1`
code, records the accepted decisions, inventories every built-in module,
defines one open-world, optionally typed tag catalog, and records the
implementation and verification that removed the split authorities.

This is the implemented architecture and durable reference for `HB-003`.
Canonical tags shipped on the `0.1.2.1` architecture-convergence train on
2026-07-24. Sections explicitly labeled **Current** retain pre-refactor
evidence where that history explains the migration; the accepted contracts and
implementation boundaries describe shipped behavior.

## Decision Status

The status words in this document are normative:

| Status | Meaning |
| --- | --- |
| **Accepted** | The user explicitly chose this direction. An implementation may rely on it. |
| **Repository invariant** | Already-established HeavenBase architecture that this refactor must preserve. |
| **Recommended** | Delegable implementation guidance inside the accepted architecture. |
| **Superseded** | An earlier option retained only where its rejection clarifies the boundary. |
| **Not applicable** | A conditional decision whose predecessor was deferred or rejected. |
| **Current** | Evidence from the checked-in code, not necessarily the desired design. |

Accepted on 2026-07-24:

1. HeavenBase will have **one unified tag model**. A loaded definition gives a
   claim a typed view; an undefined claim remains valid unresolved metadata.
2. The public and architectural term remains **tag**, even when the value is
   not Boolean. Do not introduce a parallel `Feature`, `CapabilityTag`, or
   `Family` authority.
3. Tags are string-identified, author-supplied declarations. They may describe
   behavior, nominate routing candidates, or provide ordinary categorization,
   but they never certify an implementation.
4. The published subject module record is the **sole static tag-claim
   authority**. Python class declarations may be optional pre-publication
   authoring input, but runtime resolution never falls back to them.
5. Static claims use the existing `ModuleRecord.meta.tags` mapping. There is no
   top-level alternative and no second class-owned claim location.
6. Tag references are open-world and soft. Missing definitions, values, or
   tag-carried record references never invalidate the Registry or Context.
   Optional lint and named consumers may warn, ignore, or fail only the
   affected record or operation.
7. The built-in canonical catalog is a logical convention distributed among
   semantic module owners, not a kernel-enforced closed set. Claims may use a
   tag whose definition is absent.
8. Open-domain values are strings. Built-ins use concise canonical identifiers;
   external publisher prefixes are recommended but not required. Ordinary
   Registry identity and ownership still reject two conflicting
   `kind: tag` records with the same key.
9. Category subjects are open for future module-record kinds. Subject lists,
   schemas, and defaults are declarative guidance used by typed consumers, not
   install-time referential-integrity rules.
10. Broad tags may drive named candidate discovery. For example,
    `vector: true` lets automatic routing try a Backend as a vector candidate,
    but exact HandlerKeys, compilers, concrete resources, and execution remain
    the proof. False third-party claims are publisher errors and fail or fall
    back only at the affected operation.
11. Use the minimal direct inspection model: no tag-on-tag claims, no derived
    handler category views, and no semantic-tag copy in flat MetaSchema labels.
    `CapabilityIndex` remains exact HandlerKey inspection.
12. A general Boolean selector language is deferred.
13. Cold start uses the existing dedicated bootstrap file and
    `BootstrapSpec`. Bootstrap and Registry may implement the same narrow read
    contracts and use the same data definitions, but never share an instance,
    state, cache, or configuration authority. Bootstrap data is
    construction-only and is never normal Registry or workspace configuration.

Repository invariants:

1. One Context owns one Registry authority. A tag index may be a derived,
   Context-local cache; it may not become a second durable registry.
2. Module manifests remain inert and import-free during discovery,
   installation, validation, and inspection.
3. Runtime handler dispatch remains exact:
   `(logical_type, operation, backend_type, strategy)`.
4. A matching tag identifies a candidate or a declared property. It is never
   proof that a concrete query has a native route. Compiler output plus
   concrete resource/readiness evidence remains the proof.
5. `Catalog` and `MetaSchema` remain fixed system schemas. This refactor does
   not add fields to either Entity.
6. Version authority remains `src/heavenbase/version.py`, currently
   `0.1.2.1`.
7. Installer-owned provenance, required-extension trust, and lifecycle
   ownership remain structural metadata outside author-defined semantic tags.

No major architecture choice remains. The
[Major Decision Guide](#major-decision-guide) now summarizes the accepted
outcomes and the implementation boundaries they imply. Exact class names,
diagnostic envelopes, cache structures, and test partitioning remain
delegable.

## Executive Summary

HeavenBase modules contribute records such as logical types, operations,
strategies, Backends, handlers, extensions, and serializers. The Registry tells
one Context which records are installed.

Today, related facts are split across four mechanisms:

- untyped Registry `groups`, especially `family`;
- typed Backend capability definitions in Registry records;
- static capability values and families on executable Python classes;
- handler seed callbacks containing prefix conventions and literal identifiers.

The mechanisms are individually useful, but there is no single registered
vocabulary or import-free static view. For example, `graph` is a family,
`native_graph_traversal` is a Backend capability, and HyperG seeding contains a
literal `GraphEdge.identifier` branch. An external module cannot inspect or
extend all three facts through one protocol.

The accepted direction is one open-world tag vocabulary in which Boolean and
non-Boolean properties share the same wire model, with the published subject
record as the sole static claim authority:

- explicit `meta.tags` claims are always inert string-keyed data;
- a loaded `kind: tag` definition may type, document, and provide a default for
  a claim, while an absent or malformed definition leaves that claim raw and
  unresolved;
- Boolean markers, enums, sets, mappings, and structured values share one
  optional definition/resolution machinery;
- static claims are installed with module records;
- construction-derived and live Backend claims remain in memory;
- the kernel does not build tag-owned dependency closures or reject module
  publication for semantic tag inconsistency;
- built-in claims are kept trustworthy through source-owned contract tests and
  lint, while external publishers own the truthfulness of their claims;
- exact handler keys and compilers remain the execution boundary;
- structural relations and trust metadata remain structural rather than
  becoming forgeable semantic tags.

The minimal 48-tag proposal in this document covered:

- the 44 current `family` values;
- the semantic portion of the 14 current extension `tag` values;
- all 13 current Backend capability definitions;
- one new live Backend availability tag to separate readiness from behavior;
- the five typed capabilities embedded in physical database dialect specs;
- all 32 built-in module descriptors and their 242 records.

The accepted logical built-in catalog had 48 definitions; 0.1.2.3 adds the
`database-dynamic-typing` physical-database tag and the `atomic-upsert`
Backend behavior tag, for 50. The former 49-tag
strict-category alternative is superseded: it incorrectly treated tags as
centrally verified route proof.

## Vocabulary

| Term | Definition |
| --- | --- |
| Tag definition | An optional registered `kind: tag` module record describing one identifier, value schema, default, intended subjects, lifecycle layers, and meaning. |
| Tag claim | Inert data assigned by a subject under one string identifier. `true` is still a value, not a separate membership mechanism. A definition is not required for storage. |
| Marker tag | A Boolean tag, normally defaulting to `false`. |
| Resolved tag | A claim or definition default successfully interpreted through a loaded definition. |
| Unresolved tag | A raw claim whose definition or soft reference is absent or unusable. It remains inspectable and may become resolved after a later Registry publication. |
| Value tag | A non-Boolean tag, such as an enum, list, set, mapping, or object. This is descriptive terminology, not another mechanism. |
| Category tag | A tag used for taxonomy, discovery, or candidate nomination. It is an unverified publisher assertion, never route proof. |
| Behavior tag | A tag describing an implementation candidate, such as native graph traversal. It is not execution or live-readiness proof. |
| Subject | The module record or live object whose property is described by a claim. |
| Static tag view | Definitions, defaults, and record claims resolved from one published Registry snapshot without importing executable code. |
| Effective tag view | A static view overlaid with construction-derived and live values from one concrete object, with provenance. |
| Structural group | Registry lifecycle/index metadata such as bundle ownership, provenance, or an exact foreign-key relation. It is not an author-defined semantic tag. |
| Handler key | The existing exact `(dtype, operation, backend, strategy)` dispatch key. |
| Compiler | Executable code that examines a concrete query and either emits an executable fragment or declines. |

There is no `Feature` class or feature subsystem in the target design.
“Capability” may remain an ordinary English description of what a tag means,
but `kind: capability`, `BackendCapability`, and a separate capability registry
are migration sources to remove.

Tags are orthogonal and non-exclusive. They do not form a parent/child family
tree: a Backend, strategy, or other subject may independently claim `graph`,
`side`, `vector`, or any compatible combination. Typed set values describe
dimensions within one property; they do not introduce hidden inheritance.

## Current Architecture and Evidence

### Registry groups

A current logical type may contain:

```yaml
- kind: logical_type
  identifier: short-text
  groups:
    family: [all, filter, filter_text, filter_order, aggregate_group]
```

Groups are normalized and indexed efficiently, but a group value has no
registered owner, value type, default, allowed subject kinds, or compatibility
contract. The `family` namespace currently mixes:

- taxonomy: `sql`, `graph`, `search`, `agent`, `prompt`;
- placement participation: `storage_row`, `storage_text`, `storage_vector`;
- compatibility dimensions: `filter_text`, `aggregate_numeric`;
- implementation roles: `handler`, `compiler`;
- redundant browsing labels: `all`, `backend`, `physical-database`.

An unknown or misspelled group is valid data, so a consumer can silently
receive an incomplete candidate set.

### Backend capabilities

The current Backend capability system is already open and typed. Its 13
definitions include:

- Boolean values such as `native_graph_traversal`;
- enums such as `compare_upsert`;
- unique sequences such as `near_filter_modes`;
- operation mappings such as `row_lookup`;
- structured `ScalarIndexSpec` values;
- live `strategy_adapters`.

Definitions are Registry records, but static claims normally live in
`Backend.capability_spec` on executable classes. Import-free Registry
inspection therefore cannot reconstruct the Backend's static behavior.

### Exact handler dispatch

`HandlerRegistry` dispatches on the exact key:

```text
(logical type, operation, Backend type, storage strategy)
```

Seed callbacks expand groups and provider rules into concrete keys. The query
router then invokes an exact compiler. This is a sound boundary: tags should
help declare and inspect relationships, but must not create a second runtime
planner.

### Hidden domain rules

`src/heavenbase/execution/handlers/seed.py` currently contains two semantic
rules that the Registry cannot describe:

1. filter compatibility is inferred by intersecting group names whose values
   begin with `filter_`;
2. nested HyperG seeding includes the concrete `GraphEdge.identifier`.

These rules are small but architectural. A legitimate external logical type,
operation, or strategy may require host-code edits instead of a peer module
claim.

### Inventory baseline

The checked-in built-ins comprise:

- 32 `meta.yaml` descriptors;
- 242 module records;
- 26 record kinds;
- 44 distinct `family` values;
- 14 semantic extension/Capsule `tag` values plus one installer trust value;
- 13 Backend capability definitions;
- five Boolean capability fields embedded in physical database dialect specs.

The tables below are the migration baseline. If the source changes before
implementation, regenerate and reconcile this inventory rather than treating
this snapshot as timeless.

## Problem Statement

The problem is not merely naming.

### Unmanaged vocabulary

Current family/tag values have no definitions or subject contracts. Typos,
accidental collisions, and incompatible meanings cannot be diagnosed through a
generic inspection surface. The target deliberately does **not** make them
publication errors; it adds optional definitions, typed views, and local
diagnostics without closing the vocabulary.

### Split static authority

The Registry owns installed identity and family claims while executable classes
own most static Backend behavior. Built-in and external modules use one loader,
but their complete static contracts cannot be inspected through one authority.

### Hidden relationship knowledge

Prefix conventions and concrete identifiers in seeding code prevent declarative
extension parity.

### Mixed behavior and readiness

`row_lookup` currently uses `provider_unavailable` as a value beside `native`
and `scan`. This mixes an implementation mode with the momentary state of a
live connection.

### Trust naming boundary

The installer currently injects `groups.tag: [builtin]`, and required-extension
activation checks it together with `provenance:system`. Despite its group name,
this is already installer-owned structural metadata: external publication of
the reserved package groups is rejected. It must remain excluded from the
semantic Tag API so an ordinary claim can never be mistaken for trust.

### Risk of over-correction

Making every claim depend on a loaded definition would turn an open extension
vocabulary into a globally coupled schema graph. Conversely, treating every
value as an unstructured Boolean label would discard useful definitions,
defaults, live overlays, and diagnostics. The solution is an open
`string -> JSON` claim mapping with optional typed interpretation, not a
tag-only planner or a global referential-integrity system.

## Goals

1. One claim representation and one optional definition/resolution model for
   every tag.
2. One import-free static view after Registry publication.
3. Built-in and external module parity.
4. Honest JSON values and typed views when definitions are present.
5. Preserve raw unknown, invalid, and ambiguous claims without invalidating
   unrelated Registry records or Context startup.
6. Context isolation and snapshot-coherent direct views and optional hint
   indexes.
7. Explicit separation of static facts, construction facts, and live facts.
8. Exact handler and compiler boundaries preserved.
9. Removal of prefix, literal-identifier, and duplicate-class authorities when
   parity is proven.
10. A small manifest and SDK vocabulary an independent module author can use.

## Non-Goals

This refactor does not:

- create a closed HeavenBase-only enum of all future tags;
- make category tags proof of execution;
- make tag definitions, allowed subjects, or tag-carried references hard
  Registry dependencies;
- persist live provider availability;
- replace exact `HandlerKey` dispatch;
- place Python, `eval`, callbacks, source code, or Capsules in tag definitions;
- change `Catalog` or `MetaSchema` fields;
- make ordinary user Entity/field labels part of this first migration;
- make Capsule records, Registry records, and package modules universally
  interchangeable yet;
- design remote module acquisition or ecosystem trust;
- unload extensions;
- implement missing Backends;
- revisit deferred LLM session persistence;
- change the package version from `0.1.2.1`.

## Prior Requirement and Decision Traceability

This refactor sits inside the broader modularization work but does not reopen
already-settled behavior:

| Prior requirement or decision | Consequence for canonical tags |
| --- | --- |
| Lego-composable built-in and external modules | A peer module defines and claims tags through the same inert module-folder protocol; adding a valid participant must not require a new central provider branch. |
| One Registry and one module protocol | Definitions and claims use the existing Context-owned Registry. Soft tag references create no lifecycle dependency graph; only structural module dependencies remain hard. |
| Registry records may someday join a wider workspace catalog | Keep the wire model inert and portable, but do not implement that future catalog now. |
| Capsule is the future executable-composition direction | Keep Capsules relevant for later runtime providers/facades, never for install-time tag validation. |
| Workspace construction does not activate a workspace | Tag installation/inspection cannot change active workspace selection; activation remains explicit. |
| Workspace ID is the config scope; live changes are best effort and reopen is safe | Effective live tag views follow the owning Context/workspace and are reconstructed on reopen. |
| Extensions load but have no universal disable/unload behavior | Tag work must not invent extension unloading. Tag claims create no definition/claim lifecycle dependency; genuine hard requirements continue to use structural module dependencies. |
| Queries are immutable specs; execution artifacts and ResultFrames are separate | Tags may help seed/inspect handlers, but never attach build artifacts or persisted results to a query or frame. |
| `count` is terminal before execution, not a post-execution operation | Aggregate tags describe query vocabulary and type compatibility only. |
| Entity class registration precedes instance upsert | Tag claims on module records do not add schema-registration shorthand to row operations. |
| Session/message persistence is deferred | Category tags may describe agent/session records, but persistence behavior is out of scope. |
| Missing Backends are deferred | Catalog existing behavior honestly; do not implement providers in this refactor. |
| Version is unified at `0.1.2.1` | Every changed built-in descriptor and generated projection must stay synchronized to that version. |

## Scope Boundary

The first migration applies tags to installed module-record subjects and to
live Backend overlays. It does **not** automatically reinterpret every object
currently named “tag.”

Included:

- logical types, operations, reducers, strategies, storage profiles;
- Backend types, handlers, capability/tag definitions;
- extensions and their contributed module records;
- dialects, serializers, Toolkit/MCP records, Capsule layers, gateways;
- benchmark recipes;
- live Backend instances.

Excluded for now:

- free-form user Entity labels;
- field annotations in user schemas;
- tags inside arbitrary `MetaSchema` definitions;
- persisted Capsule categorization beyond the installed Capsule module records;
- runtime result data;
- query specifications.

The exclusion prevents an architecture migration from silently defining a
user-data ontology. A later feature may deliberately reuse the same tag kernel,
but it must specify persistence, authorization, indexing, and schema semantics.

## Proposed Unified Tag Kernel

This section records the accepted target. Specific Python symbol names remain
provisional.

### Definition record

A definition is an optional inert module record. It lets generic tools and
typed consumers understand a claim, but the Registry does not require one
before storing `meta.tags`. The manifest schema version shown here is the
current valid value:

```yaml
manifest_version: 2
coordinate: heavenbase/backends/capabilities
version: 0.1.2.1
compatibility: {heavenbase: {min: 0.1.2.1, before: 0.1.3.0}}
bundle: backend-capabilities
items:
  - kind: tag
    identifier: graph-traversal-native
    name: Native graph traversal
    desc: Backend implementation can be a candidate for complete native graph traversal.
    source: inline
    target: definition
    active: true
    meta:
      schema_version: 1
      version: 0.1.2.1
      definition:
        schema: {type: boolean}
        default: false
        subjects: [backend_type]
        layers: [static]
```

`kind: tag` uses the existing module protocol. There is no second registry.
The definition payload must be declarative and JSON-compatible to resolve as a
typed definition. A malformed definition remains an ordinary stored module
record but resolves as invalid for that record; it cannot make Context startup
or unrelated module publication fail.

### Static claim

The accepted authoring location is the existing open `meta` payload:

```yaml
- kind: backend_type
  identifier: neo4j
  name: Neo4j Backend
  source: python-import
  target: heavenbase.backends.neo4j.backend:Neo4jBackend
  active: true
  meta:
    schema_version: 1
    version: 0.1.2.1
    tags:
      graph: true
      storage-roles: [row, vector]
      graph-traversal-native: true
      graph-endpoint-pushdown: true
      graph-endpoint-materialization: identity_anchor
      graph-incoming-lookup-native: true
      binary-storage: base64
```

Why `meta.tags` is accepted:

- `ModuleRecord.meta` is already the open declarative payload;
- no `Catalog` or `MetaSchema` field changes are needed;
- current manifest validation already preserves arbitrary JSON mappings there;
- typed values are not forced into the existing `groups` list-of-identifiers
  shape.

There is no top-level `tags` alternative and no runtime fallback to class
metadata.

### Declarative value-schema grammar

Install and inspection must remain inert, so a tag definition cannot require
an imported Python normalizer, serializer, callback, or Capsule. The accepted
grammar is a small declarative subset. It defines typed **resolution**, not a
global Registry publication gate:

| Schema kind | Canonical value | Required definition data | Validation and canonicalization |
| --- | --- | --- | --- |
| `boolean` | JSON Boolean | none | Accept only `true` or `false`; never coerce strings or integers. |
| `string` | JSON string | optional `pattern`, `min_length`, `max_length` | Exact Unicode string; no implicit trimming or case folding. |
| `integer` | JSON integer | optional `minimum`, `maximum` | Reject Boolean and fractional values. |
| `number` | finite JSON number | optional `minimum`, `maximum` | Reject Boolean, NaN, and infinity. |
| `identifier` | exact JSON string | optional `pattern` | Validate only when a typed consumer asks; do not resolve it as a Registry record. |
| `enum` | one canonical scalar | non-empty `values` | Exact membership; this grammar does not define enum-value aliases. |
| `list` | JSON array | `items` schema; optional `unique_keys` for object items | Preserve authored order; validate each item; enforce each declared field-tuple uniqueness key. |
| `set` | JSON array with unique items | `items` schema | Validate items, deduplicate only as an error check, then store in deterministic canonical order. |
| `mapping` | JSON object | `values` schema; optional key pattern | String keys only; canonical key order for hashing/equality. |
| `object` | JSON object | named `properties`, optional `required`, `additional_properties`, declarative `constraints` | Validate recursively; reject unknown fields by default; support only the closed cross-field constraints named below. |
| `reference` | exact JSON string | advisory referenced record `kind`; optional `state` (`any` or `active`) | Preserve the authored string. A named consumer may resolve it at use and report a local unresolved target. |
| `json` | any non-null canonical JSON-compatible value | none | Validate recursively, reject null/non-JSON values, and canonicalize object-key order. Use only inside a stable typed envelope. |

Resolution rules:

- when a usable definition exists, absence means “use the definition default”;
- when no usable definition exists, absence is simply absence and an explicit
  claim remains raw unresolved metadata;
- `null` is invalid unless a future schema explicitly adds nullability;
- defaults and explicit claims pass through the same validator only when
  producing a typed view;
- no implicit scalar coercion;
- set uniqueness uses canonical item equality;
- object and mapping claims replace the whole inherited value;
- canonical serialization is stable JSON-compatible data;
- domain facades may materialize immutable Python views, such as
  `ScalarIndexSpec`, only after canonical validation;
- validation never rewrites the raw `meta.tags` mapping;
- known invalid values, ambiguous alias/canonical duplicates, absent
  definitions, and missing soft-reference targets become record-local
  `invalid`, `ambiguous`, or `unresolved` diagnostics;
- executable normalizers may remain temporarily as parity or migration code,
  but cannot be the installed definition authority.

The initial cross-field constraint set contains one operation:

```yaml
constraints:
  - {kind: subset, field: default_types, of: supported_types}
```

Both fields must be declared set/list properties with compatible item schemas.
`unique_keys` is a list of object-field tuples; each tuple must be unique
across list items. These two refinements cover current scalar-index and
strategy-adapter invariants without introducing a general expression language.

### Closed and extensible value domains

The value schema and the value domain are separate concerns. Closed enums are
checked by typed consumers when their definition is usable. Every extensible
domain uses exact strings and creates no `tag_value` Registry records.

Recommended domain classification:

| Domain | Classification | Reason |
| --- | --- | --- |
| `aggregate-modes`, `aggregate-input-modes` | extensible identifiers in one aggregate-mode domain | Third-party logical types and reducers can already share a custom compatibility family. Built-ins are `group`, `numeric`, and `order`. An empty reducer-input set means unconstrained, preserving the current reducer contract without a kernel-only wildcard. |
| `near-filter-modes` | closed enum | Each value names a planner contract; an unknown mode cannot safely route. |
| `row-lookup` member values | closed enum | Each operation mode is exactly `scan` or `native`; availability is separate. |
| `backend-availability` | closed enum | Consumers need exact three-state semantics. |
| `binary-storage`, `graph-endpoint-materialization`, `compare-upsert` | closed enum | Each value selects a defined codec/materialization/mutation contract. |
| `filter-domains` | extensible identifiers | A third-party type and operation may introduce a peer compatibility domain without editing HeavenBase host code. |
| `storage-roles` | extensible identifiers | A third-party Backend and profile policy may introduce a new storage role. |
| `benchmark-suites` | extensible identifiers | External benchmark collections may add suites without a core release. |
| Reference sets such as `unsupported-storage-types` | open string identifiers | A named consumer may resolve a value at use; missing targets remain local diagnostics. |
| `strategy-adapters` identities and metadata | open instances inside a closed envelope | Adapter identity is extensible; envelope fields remain stable. |

Built-in canonical identifiers use lower-case kebab case. External publishers
are encouraged, but not required, to prefix new identifiers. A raw claim may
use any identifier accepted by the ordinary manifest convention. A
`kind: tag` definition still has ordinary Registry identity
`("tag", identifier)`: another coordinate cannot silently replace it, and a
collision fails under existing module ownership rules. This unavoidable
structural collision rule does not make namespaces or definitions mandatory.

If a later definition gives an old raw identifier a meaning, the derived view
may change from unresolved to resolved or invalid. The raw record does not
change. Typos in open values may therefore fail to match; optional lint and
publisher tests are the intended protection.

### Allowed subjects

Definitions document intended subject kinds. Category definitions are
`category-open`, so a future module-record kind may claim `graph`, `mcp`,
`agent`, or another category without a HeavenBase release. Behavioral
definitions may list exact intended built-in kinds.

These lists are advisory to the generic kernel. A typed consumer may warn,
ignore, or fail the affected subject/operation when a known claim appears on an
unexpected kind, but module installation, Context startup, and unrelated
records remain usable. An unknown kind gains no behavior merely by claiming a
known category: each named consumer still selects the exact subject dimensions
it understands.

This first migration still applies only to installed module records and live
Backend overlays; it does not admit arbitrary user Entity rows or field
annotations.

Live object kinds are overlays of a registered static subject, not arbitrary
new kinds. A live Backend instance therefore overlays its
`backend_type:<identifier>` record.

### Publication and lifecycle

Tag publication deliberately has no tag-owned dependency transaction:

1. the ordinary module protocol validates the manifest envelope, JSON safety,
   module identity, ownership, aliases/groups, trust metadata, and structural
   `meta.dependencies`;
2. `meta.tags` is stored verbatim as `string -> JSON`;
3. definition existence, definition validity, subject guidance, claim value
   shape, aliases, and tag-carried reference targets do not gate publication;
4. a snapshot-bound Tag view interprets each claim as `resolved`,
   `unresolved`, `invalid`, or `ambiguous`;
5. adding, retiring, replacing, or removing a definition invalidates only
   derived Tag views and optional hint indexes; it never rewrites claims or
   blocks the Registry mutation;
6. named consumers own their warning, skip, fallback, or affected-record
   failure policy.

A definition and its first claim may therefore arrive in any order or in
different bundles. Active and inactive records create no tag-specific
dependency closure. A hard operational dependency must continue to use
structural `meta.dependencies`, an exact relation field, Extension
dependencies, or the existing HandlerKey/provider protocol. Do not encode a
soft tag reference as `meta.dependencies`.

Standard HeavenBase setup publishes and protects the built-in package set, so
ordinary workspace APIs do not normally create a definition-incomplete
built-in catalog. A custom package set, trusted repair, or future expert
lifecycle operation may do so. The resulting absence is still not a
whole-Context error; any named consumer that requires a typed built-in
definition may diagnose or fail only its affected record or operation. That
local consequence belongs to the catalog operator.

Alias resolution is also derived. If a subject supplies two raw keys that a
loaded definition view resolves to the same tag, that subject/tag is
`ambiguous`; the raw record remains intact and only the consuming operation may
warn, skip, or fail.

### Cold-start authority cycle

One bootstrap cycle must be resolved before class `capability_spec` can be
removed:

```text
Context needs control Backend
    -> control Backend persists system-modules
    -> ModuleService and published tag records become available
```

Today `_control_backend()` imports and constructs the configured Backend before
`ModuleService` exists. SQL construction reads
`workspace_database_namespace` from the class capability spec, and the control
plane also builds `BootstrapDialectCatalog` from dialect data duplicated under
the bootstrap configuration. PostgreSQL/MySQL workspace database naming and
SQLite bootstrap dialect behavior therefore cannot simply wait for the
published Tag API.

The accepted cycle break is the already-implemented bootstrap ladder:

```text
explicit mapping / ~/.heavenbase/bootstrap.yaml / packaged bootstrap.yaml
    -> BootstrapSpec and construction-only adapters
    -> control Backend
    -> RegistryStore and system-modules
    -> ordinary Registry-backed module/tag views
```

The bootstrap file contains only the minimum root, control-Backend,
Registry-safety, dialect, and tag-shaped construction facts required to create
the actual Registry. It is not a temporary module Registry, is not
`ConfigManager` or workspace configuration, and never answers normal module or
Tag API reads.

Bootstrap and Registry adapters may implement the same narrow methods and use
the same dataclass/schema definitions, as `BootstrapDialectCatalog` and
`ResolverDialectCatalog` already do. They never share an instance, cache,
snapshot, or mutable state. `Context.bootstrap` may retain the construction
spec for lifecycle/recovery purposes; after the Registry opens, the ordinary
Registry-backed adapter owns normal inspection and resolution.

No descriptor fingerprint parity is required. Bootstrap values are a deliberate
construction-layer exception rather than duplicate static subject claims. A
malformed construction-critical bootstrap still prevents that Context from
starting; a missing or inconsistent optional tag definition does not.

### Resolution layers

The accepted effective-value precedence for a usable definition is:

```text
live explicit overlay
    > construction-derived overlay
    > static module-record claim
    > definition default
```

Without a usable definition, an explicit claim remains available with
`status="unresolved"` and no implicit default. A known claim that fails its
definition is `invalid`; multiple raw keys resolving to one known definition
are `ambiguous`.

Each returned value carries provenance:

```text
ResolvedTagValue(
    tag="binary-storage",
    value="base64",
    status="resolved",
    source="construction",
    static_subject="backend_type:file",
    instance=BackendInstanceRef(
        context=<authority token>,
        workspace="research",
        name="archive",
    ),
    snapshot=<registry snapshot token>,
)
```

Lists, sets, mappings, and objects use whole-value replacement. There is no
implicit deep merge between layers. A Backend may expose live changes when
practical, but the broader accepted workspace rule applies: live change is
best effort; reopen is the safe reconciliation boundary.

Static inspection never constructs or imports a Backend. Effective inspection
requires an explicit live instance or workspace binding. Construction and live
overlays belong to that concrete object, never to the static
`backend_type:<identifier>` index. Two instances of the same Backend type may
therefore expose different values without sharing a cache. A detached Backend
uses an opaque process-instance token rather than a fabricated workspace/name
identity.

Every direct Tag view and optional tag-hint index records the Registry snapshot
from which it was built. A named consumer that uses such an index must refresh
or rebuild it atomically, or report stale/reopen-required. Exact
`HandlerRegistry` and `CapabilityIndex` state retain their own existing
Registry/runtime snapshot contracts; they do not become derived Tag
authorities merely because a tag was one input to candidate generation.
Reopen remains the guaranteed reconciliation boundary.

### Capsule boundary

Capsules remain relevant to future executable composition, but not to the inert
tag kernel. A later design may represent live overlay providers, serializers,
or domain facades as Capsules or resolve them through an override registry,
package code tree, or registered module. It may also place Registry records,
modules, and Capsules in a broader workspace catalog.

This refactor records that direction without implementing it. A Capsule must
not be required to validate a definition, install a claim, build a static
index, or evaluate a declarative selector. Executable resolution happens only
after the inert Registry contract is valid and an explicit runtime consumer
asks for it.

### Trust and structural metadata

Tags are module-authored semantics. They may not grant installer trust,
required-extension status, lifecycle ownership, or provenance.

Keep current installer-owned `groups.tag: [builtin]` and
`groups.provenance: [system]` as reserved structural groups for this refactor.
They are not definitions, claims, or values returned by the Tag API. Preserve
the existing external-publication rejection and required-extension trust
checks. An ordinary semantic tag named `builtin` or `system` cannot substitute
for either group.

Renaming or redesigning trust metadata would expand this work without fixing a
live defect and belongs in a separate security decision if a concrete need
appears.

### Exact dispatch boundary

Tags may populate candidate sets, candidate order, or let a seed compiler
expand exact keys, but runtime dispatch remains:

```text
optional raw/resolved tag hints
    -> named dimension-aware candidate/key expansion
    -> exact HandlerKey
    -> compiler
    -> concrete resource/readiness checks
    -> executable fragment or explicit decline
```

A broad `graph` tag means “graph-related.” A
`graph-traversal-native: true` claim means “this implementation may support
native traversal.” Neither means a driver is connected, the relevant graph is
materialized, or a concrete predicate can be pushed down.

### Accepted category and routing boundary

Categories are descriptive publisher declarations that may be used by a named
application consumer as search or routing hints. “Descriptive” does not mean
inert: `vector: true` may let automatic routing try a Backend for vector work.
It means unverified: HeavenBase does not certify the claim at installation.

Named consumers own the exact identifier and value shape they understand. They
may consume a raw unresolved claim, preferably emitting a diagnostic, and must
dimension-filter the subject kinds they know. A claim on a future unknown kind
does not acquire built-in behavior automatically.

A positive claim may nominate or prefer a candidate; it never manufactures a
HandlerKey, compiler, driver, resource, or successful execution. A false
third-party claim is exposed in `explain()` where practical and produces only
the affected skip, fallback, unsupported result, or execution failure.
Conversely, an explicitly registered exact provider/HandlerKey route does not
become invalid merely because an optional descriptive tag is absent.

The accepted catalog is therefore this declarative tag model: 48 definitions
when it was accepted, 50 since 0.1.2.3 added `database-dynamic-typing` and
`atomic-upsert`. Do not add
`storage-strategies` or replace `near` with `vector-filter` merely to create a
kernel-enforced proof layer.

## Canonical Tag Catalog

The identifiers in the following catalog are the accepted **logical built-in
vocabulary**. They are conventions and built-in contract-test targets, not a
closed Registry namespace or a completeness requirement for every Context.
Canonical built-in **tag identifiers** use lower-case kebab case. External
publishers may use unqualified identifiers but are encouraged to prefix new
ones. Referenced module identifiers
and domain enum/object values retain the canonical spelling of their owning
domain; this refactor does not rename operations or capability values merely
for visual consistency. Existing underscore capability *identifiers* and
family values are migration sources; permanent aliases should not be retained
unless an accepted compatibility policy requires them.

The catalog contains 50 built-in definitions: 16 routing/relationship
tags, 13 cross-domain category tags, 15 Backend behavior tags, and six
physical-database behavior tags. A definition is included only when it has a
named descriptive use, optional hint consumer, or meaningful inspection
contract. A Context may lack any definition or claim without becoming
unusable.

### Definition ownership and folder placement

One tag kernel does not require one central manifest. The accepted
ownership rule is: the domain that defines a tag's meaning owns its definition;
other modules may claim that identifier whether or not the owner is currently
installed.

| Definition owner | Proposed tag definitions |
| --- | --- |
| `src/heavenbase/query/meta.yaml` | `aggregate`, `aggregate-modes`, `aggregate-input-modes` |
| `src/heavenbase/filters/meta.yaml` | `filter`, `filter-domains`, `nested-filter`, `near` |
| New neutral `src/heavenbase/placement/meta.yaml` | `storage-roles`, `graph`, `side`, `vector`, `search`, `sql` |
| `src/heavenbase/backends/meta.yaml` | All 15 Backend behavior tags |
| `src/heavenbase/execution/handlers/meta.yaml` | `portable` |
| `src/heavenbase/backends/sql/strategies/gram/meta.yaml` | `gram` |
| `src/heavenbase/benchmark/meta.yaml` | `benchmark-suites` |
| `src/heavenbase/capsule/meta.yaml` | `capsule`, `executable` |
| `src/heavenbase/extensions/agent/meta.yaml` | `agent`, `session`, `skill` |
| New core-domain `src/heavenbase/database/meta.yaml` | `database` and the five physical-database behavior tags |
| `src/heavenbase/extensions/memory/meta.yaml` | `memory` |
| `src/heavenbase/extensions/prompt/meta.yaml` | `prompt`, `translation` |
| New neutral `src/heavenbase/llm/meta.yaml` | `llm` |
| `src/heavenbase/toolkit/meta.yaml` | `mcp`, `toolkit` |
| `src/heavenbase/schema/meta.yaml` | `schema` |

The three proposed descriptors (`placement`, `database`, and `llm`) own
cross-cutting vocabulary already owned by those code domains; they contain
definitions, not executable provider registries. In particular, core physical
dialects must not depend on the optional database extension for vocabulary.
This is preferable to a central `heavenbase/tags/meta.yaml` that would become
an unrelated inventory of every domain.

This distributed placement changes the target module-folder set from the
32-descriptor current baseline to 35 descriptors. A definition record has one
ordinary module owner. Cross-bundle claims do not create tag-specific
dependencies, so an owner may be absent, retired, or later restored without
blocking other bundles.

Accepted runtime dependency ownership:

| Layer | Owns | Must not own/import |
| --- | --- | --- |
| `heavenbase.utils.registry` | Generic snapshot, indexing, publication, and lifecycle primitives | Tag schemas, module-record policy, Backends, or domain consumers |
| `heavenbase.modules` (likely a focused `tags` module) | Optional Tag-definition parsing, direct raw/resolved inspection, per-record diagnostics, `TagDefinition`, `ResolvedTagValue`, and static `TagView` | Global tag-semantic publication gates, Backend/provider imports, storage-policy semantics, workspace selection, placement decisions, or execution |
| `heavenbase.context` bootstrap seam | Dedicated construction-only bootstrap source until `system-modules` can open | Runtime Tag API, normal ConfigManager state, or a provisional Registry instance |
| `heavenbase.backends` | Construction/live overlay providers and Backend convenience methods | Static definition/claim authority or a second tag registry |
| `heavenbase.placement` | Pure-data storage-policy schema, order/fallback semantics, and application-local checks over generic raw/resolved tag views | Generic tag authority, global semantic admission gates, or provider-specific execution |
| `query`, `filters`, `capabilities`, extensions, and MCP projections | Named consumers of Context-owned tag views | New definitions inferred from executable classes or ad hoc family fallbacks |

The generic Registry remains extractable, and the tag/module layer never
imports Backend or provider code. `ModuleService` continues to validate only
ordinary manifest/Registry structure and genuine structural dependencies. It
does not inject domain tag validators into the publication transaction.

Built-ins are discovered and published as one package batch by
`ModuleService.setup()`, but this is not a definition-order requirement.
Source-owned CI/lint proves the built-in definitions and claims agree.
Independent modules may claim an existing, future, private, or entirely
undefined tag identifier.

Catalog-wide definition defaults:

- subject lists document intended built-in claim sites; category rows are open
  to future module-record kinds and generic publication does not enforce either
  list;
- the layer is `static` unless a row says otherwise;
- there is no selector-policy field in the initial kernel because a general
  selector contract is not accepted;
- all 15 Backend behavior definitions document
  `subjects:[backend_type]`;
  catalog `layers` controls which value sources are legal. Rows that include
  `static` describe authored Backend-type claims; construction/live layers
  overlay a concrete instance of that same subject. An unexpected authored
  layer is a typed-view diagnostic, not a publication rejection;
- physical-database behavior tags are static on `database_dialect`.

Built-in admission/lint rule: do not persist a tag claim merely to repeat a subject's kind,
fixed identity, exact structural relation, or another authoritative claim with
the same semantics. Query that authority directly or expose a clearly labeled,
non-authoritative derived view. Related tags may coexist only when their
meanings and consumers are genuinely different. This rule removes proposed
`handler`, `filter-strategy`, `interop`, and `system` claims rather than
replacing old duplication with new duplication.

A cross-kind built-in category may include every current record of one specialized kind
when its named consumer needs one semantic union across several kinds—for
example, MCP surface assembly across extensions, Toolkit functions,
serializers, profiles, and session openers. That category does not replace
exact kind inspection. A proposed built-in tag whose only subjects and
consumer duplicate one exact kind, such as current `interop`, fails source
catalog review. This is not a restriction on external claims.

Likewise, a broad category such as `vector` may coexist with
`storage-roles:[vector]`: the former expresses a cross-kind relationship,
while the latter is the Backend's more specific self-declared storage-support
hint. A category affects behavior only where application code names an exact,
dimension-filtered consumer; claims on unknown subject kinds are ignored by
that consumer. Such a claim can be a candidate or policy hint but never
concrete execution proof. Compilers/resources still decide execution.

### Routing and relationship tags

“Subject policy” is descriptive definition metadata. `exact` documents the
intended built-in behavioral subjects. `category-open` documents the complete
built-in claim set while allowing a third-party module-record kind to
categorize itself. Neither mode is a generic publication guard; every named
consumer still filters to the dimensions it understands.

| Identifier | Type / default | Subject policy | Built-in subjects | Short description | Named hint/use |
| --- | --- | --- | --- | --- | --- |
| `aggregate` | Boolean / `false` | `category-open` | `operation`, `reducer` | Record belongs to the aggregate vocabulary. | Aggregate discovery and portable reducer pairing. |
| `aggregate-modes` | set of open mode identifiers / `[]` | `exact` | `logical_type` | Aggregate dimensions advertised by a logical type. Built-ins are `group`, `numeric`, `order`. | Replaces `aggregate_group`, `aggregate_numeric`, `aggregate_order` while preserving custom compatibility dimensions. |
| `aggregate-input-modes` | set of the same open mode identifiers / `[]` | `exact` | `reducer` | Logical-type dimensions accepted by a reducer; an empty set is unconstrained. | Replaces reducer `type_families`; otherwise a reducer accepts a type when the two mode sets intersect. |
| `filter` | Boolean / `false` | `category-open` | `logical_type`, `operation`, `strategy` | Broad filter-related category. | Cross-kind discovery; type/operation hinting may also use `filter-domains`, `nested-filter`, or `near`. |
| `filter-domains` | set of open mode strings / `[]` | `exact` | `logical_type`, `operation` | Dimensions on which a type and operation are expected to be compatible. Built-ins are `array`, `boolean`, `json`, `order`, `struct`, `text`. | Replaces `filter_*` prefix intersections as an application-owned candidate hint. |
| `nested-filter` | Boolean / `false` | `exact` | `logical_type`, `operation`, `strategy` | Subject declares participation in nested-row quantifiers. | Replaces the HyperG/`GraphEdge.identifier` seed hint; exact handlers and compilers still decide. |
| `near` | Boolean / `false` | `exact` | `logical_type`, `operation`, `strategy` | Subject declares participation in vector-neighbor queries. | Connects Vector, `near`, and VectorIndex candidates; not native proof. |
| `portable` | Boolean / `false` | `exact` | `backend_type`, `strategy`, `predicate`, `handler` | Subject participates in provider-neutral scan execution. | Selects Backend types and ordinary strategies eligible for generic handler seeding and identifies the generic handler/predicates. |
| `storage-roles` | set of open role identifiers / `[]` | `exact` | `backend_type` | Storage roles a Backend implementation provides. Built-ins are `row`, `text`, `vector`. | Backend candidate facts used by ordered storage-profile policy; it does not encode profile preference. |
| `graph` | Boolean / `false` | `category-open` | `backend_type`, `logical_type`, `strategy`, `storage_profile` | Broad graph categorization. | Discovery and graph candidate preference; exact graph behavior tags and compilers prove individual behavior. |
| `side` | Boolean / `false` | `category-open` | `logical_type`, `strategy`, `storage_profile` | Side/nested representation category. | Discovery; on known dimensions it may help nominate SQL SideTable candidates. |
| `vector` | Boolean / `false` | `category-open` | `backend_type`, `logical_type`, `operation`, `strategy`, `storage_profile` | Broad vector category. | Discovery and vector candidate nomination on known dimensions. A false publisher claim may be tried and then decline/fail locally. |
| `search` | Boolean / `false` | `category-open` | `backend_type`, `logical_type`, `strategy`, `storage_profile` | Text-search/index category. | Discovery and long-text placement preference, optionally combined with `storage-roles:[text]`. |
| `sql` | Boolean / `false` | `category-open` | `backend_type`, `database_dialect`, `sql_dialect`, `storage_profile`, `extension` | SQL ecosystem category. | Discovery, SQL Backend candidate selection, and SQL placement preference; still not execution proof. |
| `gram` | Boolean / `false` | `category-open` | `gram_normalizer`, `gram_tokenizer`, `strategy`, `storage_profile` | Sparse-GRAM text-index category. | GRAM component/strategy discovery; the exact two-operation plugin declaration remains authoritative. |
| `benchmark-suites` | set of open suite strings / `[]` | `exact` | `benchmark_combination` | Benchmark recipe selection. Built-ins are `default`, `optional-local`, `external`. | Replaces `groups.suite` as an application-owned string claim. |

The names `filter-domains`, `nested-filter`, and `storage-roles` and the ordered
profile policy that consumes them are accepted built-in conventions. They are
not generic kernel rules.

### Cross-domain category tags

These are deliberately ordinary category tags. They only influence behavior
where the “Named usage” column identifies a consumer.

| Identifier | Type / default | Subject policy | Built-in subjects | Short description | Built-in usage |
| --- | --- | --- | --- | --- | --- |
| `agent` | Boolean / `false` | `category-open` | `entity`, `extension`, `toolkit_family`, `mcp_call_logger` | Agent-domain contribution. | Inspect agent records and agent-facing extension dependencies. |
| `capsule` | Boolean / `false` | `category-open` | `entity`, `extension`, `capsule_layer` | Capsule-domain contribution. | Discover Capsule schema and restoration layers. |
| `database` | Boolean / `false` | `category-open` | `entity`, `extension`, `toolkit_family`, `mcp_profile`, `database_dialect` | External-database domain contribution. | Discover database entities and tools. |
| `executable` | Boolean / `false` | `category-open` | `extension`, `capsule_layer` | Contribution restores or represents executable artifacts. | Capsule inspection only; never install-time execution. |
| `llm` | Boolean / `false` | `category-open` | `llm_gateway`, `extension`, `schema_text` | LLM-domain contribution. | Gateway and LLM schema-text discovery. |
| `mcp` | Boolean / `false` | `category-open` | `extension`, `toolkit_family`, `toolkit_function`, `serializer`, `mcp_profile`, `mcp_call_logger`, `mcp_transform` | MCP-facing contribution. | MCP surface assembly and inspection. |
| `memory` | Boolean / `false` | `category-open` | `entity`, `extension`, `toolkit_family`, `mcp_profile` | Memory-domain contribution. | Memory extension/tool discovery. |
| `prompt` | Boolean / `false` | `category-open` | `entity`, `extension` | Prompt-domain contribution. | Prompt entity discovery. |
| `schema` | Boolean / `false` | `category-open` | `extension`, `schema_text` | Schema-facing contribution. | Schema renderer and database-extension inspection. |
| `session` | Boolean / `false` | `category-open` | `entity`, `extension`, `mcp_call_logger` | Session-domain contribution. | Agent Session and call-log persistence discovery; never MCP protocol state. |
| `skill` | Boolean / `false` | `category-open` | `entity`, `extension` | Skill-domain contribution. | Agent skill discovery. |
| `toolkit` | Boolean / `false` | `category-open` | `entity`, `extension`, `toolkit_family`, `toolkit_function`, `serializer`, `mcp_profile`, `mcp_transform` | Toolkit-domain contribution. | Toolkit/MCP assembly and inspection. |
| `translation` | Boolean / `false` | `category-open` | `entity`, `extension` | Translation-domain contribution. | Prompt extension discovery. |

`toolkit_family` is an existing module-record kind for one Toolkit extension
unit. It remains named that way. It is not the untyped `groups.family`
authority being removed, and this refactor must not rename or reinterpret it.

### Backend behavior tags

The following table converts all 13 current capability definitions into the
same tag vocabulary and introduces `backend-availability` to separate live
readiness from declared behavior.

| Identifier | Type / default | Layers | Short description | Named usage |
| --- | --- | --- | --- | --- |
| `near-filter-modes` | set enum `combined_native,metadata_prefilter,candidate_id_prefilter` / `[]` | static | Native ways a Backend can combine near search and filtering. | Near planner candidate selection. Generic `combined_scan` is omitted because it is the portable baseline, not an opt-in behavior. |
| `row-lookup` | object with `get_many`, `exists_many`, and `count` values of `scan` or `native`; all `scan` | static | Implementation mode for exact object-ID lookup operations. | Row lookup planning. Live unavailability is read from `backend-availability`, not encoded here. |
| `backend-availability` | enum `unknown,ready,unavailable` / `unknown` | live | Best-effort current connection/provider readiness. | Effective inspection and preflight diagnostics. `ready` is not execution proof; `unknown` is not denial. |
| `binary-storage` | enum `bytes,base64` / `bytes` | static, construction | Physical representation used for Artifact bytes. | Artifact codec selection. Generic File is construction-derived; fixed JSON/Pickle Backend types can claim statically. |
| `graph-traversal-native` | Boolean / `false` | static | Implementation can perform complete native graph traversal. | Graph compiler candidate selection; readiness checked separately. |
| `graph-endpoint-pushdown` | Boolean / `false` | static | Native traversal can fuse endpoint predicates. | Graph compiler candidate selection. |
| `graph-endpoint-materialization` | enum `none,identity_anchor` / `none` | static | Physical projection used for graph endpoints. | Graph compiler/materializer selection. |
| `graph-incoming-lookup-native` | Boolean / `false` | static | Native state can provide a complete incoming-source candidate superset. | Incoming-edge compiler candidate selection. |
| `workspace-database-namespace` | Boolean / `false` | static | Backend type interprets a workspace binding as a provider database namespace. | Backend construction and workspace rebinding combine this declared property with the instance's actual `ws_id`. |
| `unsupported-storage-types` | set of `logical_type` references / `[]` | static | Logical types this Backend must not own. | Placement exclusion before compiler selection. |
| `indexable-types` | set of `logical_type` references / current scalar default | static | Types representable by the Backend's default value/property index policy. | `HandlerContext.indexable`; remains distinct from index lifecycle support. |
| `scalar-index` | closed object / disabled object | static | Physical scalar-index policy and lifecycle support. | Index planning, ensure, and opt-out policy. |
| `compare-upsert` | enum `unsupported,process,provider` / `unsupported` | static | Atomic compare-and-set upsert scope. | Revision-aware mutation planning. |
| `atomic-upsert` | Boolean / `false` | static, construction | A failed upsert call applies none of its row mutations. | `upsert(..., atomic=True)` and `upsert_many(..., atomic=True)` refuse a backend without this claim. A SQL Backend whose engine may commit every statement withdraws its type's claim at construction: SQLAlchemy `AUTOCOMMIT` isolation; an `autocommit` connect argument from `params` or `connect_args` that is true in Python or `None` (the driver or server default); `sqlite3` `isolation_level=None`; or a `pragmas` statement naming `autocommit` or `implicit_transactions`. |
| `strategy-adapters` | list of stable adapter envelopes / `[]` | live | Strategy adapters installed on a concrete Backend instance. | Live strategy inspection; not a static selector. |

Recommended `scalar-index` schema:

```yaml
type: object
additional_properties: false
properties:
  value: {type: boolean}
  unique: {type: boolean}
  composite: {type: boolean}
  composite_unique: {type: boolean}
  opt_out: {type: boolean}
  supported_types:
    type: set
    items: {type: reference, kind: logical_type}
  default_types:
    type: set
    items: {type: reference, kind: logical_type}
required:
  - value
  - unique
  - composite
  - composite_unique
  - opt_out
  - supported_types
  - default_types
constraints:
  - {kind: subset, field: default_types, of: supported_types}
```

`indexable-types` and `scalar-index.supported_types` look similar but have
different current consumers. The former answers whether a compiler may use a
value/property index representation; the latter describes a Backend's managed
scalar-index lifecycle. Keep both during parity migration. Consolidation is a
separate evidence-backed design decision, not a mechanical cleanup.

Recommended `strategy-adapters` item schema:

```yaml
type: list
unique_keys:
  - [strategy]
  - [identifier]
items:
  type: object
  additional_properties: false
  properties:
    identifier: {type: identifier}
    strategy: {type: reference, kind: strategy, state: active}
    metadata:
      type: mapping
      values: {type: json}
  required: [identifier, strategy, metadata]
```

The live overlay provider preserves current adapter registration order, and
generic `list` validation preserves supplied order. Consumers must use the
typed `strategy` field—not list position—for exact lookup or routing. The
stable envelope keeps routing-relevant identity typed while permitting
adapter-specific diagnostic metadata. During migration, the current sparse
GRAM keys `operations` and `provider` move under `metadata`; the base adapter
uses an empty object. Consumers may display metadata but must not interpret an
arbitrary metadata key as a routing contract. Adding a routing contract later
requires a named tag or a versioned envelope field.

### Physical database behavior tags

Five current Boolean values live inside
`DatabaseDialectSpec.definition.caps`. They should use the same tag kernel; the
rest of a dialect definition remains ordinary dialect configuration.
`database-dynamic-typing` (0.1.2.3) is a sixth, declared as a tag from the start.

| Identifier | Type / default | Allowed subject | Short description | Named usage | True built-in claims |
| --- | --- | --- | --- | --- | --- |
| `database-json-native` | Boolean / `false` | `database_dialect` | Dialect stores JSON natively. | JSON SQL type selection and bind/result conversion. | `postgresql`, `duckdb`, `trino` |
| `database-blob-native` | Boolean / `false` | `database_dialect` | Dialect stores binary blobs natively. | Binary SQL type selection and codec behavior. | `sqlite`, `postgresql`, `mysql`, `duckdb`, `mssql`, `oracle` |
| `database-dynamic-typing` | Boolean / `false` | `database_dialect` | Dialect columns hold any value class whatever their declared type. | Admits `vector_packed_type: float64_blob`, whose BLOBs land in a Vector column an existing database created as text. | `sqlite` |
| `database-local-path` | Boolean / `false` | `database_dialect` | Database is addressed by a local path. | URL materialization and create/drop path handling. | `sqlite`, `duckdb` |
| `database-create` | Boolean / `false` | `database_dialect` | Dialect implementation supports database creation. | Explicit/autocreate DDL selection. | `sqlite`, `postgresql`, `mysql`, `duckdb`, `mssql`, `oracle`, `starrocks`, `hive` |
| `database-drop` | Boolean / `false` | `database_dialect` | Dialect implementation supports database removal. | Explicit teardown and workspace cleanup DDL selection. | Same claimants as `database-create` |

`default`, `sql`, and `oceanbase` claim none of these six today.

## Built-In Claim Matrices

These matrices make the proposed catalog actionable. They are a faithful
semantic translation of current families, class capability specs, live
overrides, and dialect caps, except where a row explicitly says it is a
recommended correction.

### Logical types and operations

`aggregate-modes` claims:

| Logical types | Claim |
| --- | --- |
| `boolean` | `[group]` |
| `categorical`, `date`, `identifier`, `long-text`, `medium-text`, `short-text`, `timestamp` | `[group, order]` |
| `duration`, `float`, `integer` | `[group, numeric, order]` |
| `artifact`, `array`, `hyper-g`, `json`, `vector` | default `[]` |

`aggregate-input-modes` claims on reducers:

| Reducers | Claim |
| --- | --- |
| `count` | default `[]` (unconstrained for this reducer-input tag) |
| `min`, `max` | `[order]` |
| `sum`, `avg`, `median`, `percentile` | `[numeric]` |

Reducer validation preserves current semantics: an empty input-mode set accepts
every source type; otherwise its claim must intersect the source logical type's
`aggregate-modes`. This is a named aggregate consumer rule, not a special case
inside the generic tag validator. Group-by eligibility independently requires
`group` in the logical type's `aggregate-modes`. Third-party mode identifiers
must use the accepted extensible-domain ownership/namespace policy.

`filter-domains` claims on logical types:

| Logical types | Claim |
| --- | --- |
| `array` | `[array]` |
| `boolean` | `[boolean]` |
| `categorical`, `date` | `[order, text]` |
| `duration`, `float`, `integer`, `timestamp` | `[order]` |
| `hyper-g` | `[struct]` |
| `identifier`, `long-text`, `medium-text`, `short-text` | `[text]` |
| `json` | `[json]` |
| `artifact`, `vector` | default `[]` |

`filter-domains` claims on operations:

| Operations | Claim |
| --- | --- |
| `eq`, `ne`, `in` | `[array, boolean, json, order, struct, text]` |
| `lt`, `lte`, `gt`, `gte` | `[order, text]` |
| `match` | `[struct, text]` |
| `like`, `ilike`, `wildcard`, `regex`, `contains`, `starts_with`, `ends_with` | `[text]` |
| `array_contains` | `[array]` |
| `contained_in` | `[array, text]` |
| `get` | `[json]` |
| `exists`, `all`, `near` | default `[]`; modeled by `nested-filter` or `near` |

Other relationship claims:

| Tag | Exact built-in claims |
| --- | --- |
| `aggregate` | Operations and reducers `count`, `min`, `max`, `sum`, `avg`, `median`, `percentile`. |
| `filter` | Logical types with non-empty `filter-domains` plus `hyper-g` and `vector`; all 21 filter operations; strategies `inline`, `inverted`, `json`, `side`, `graph`, `vector`, `sparse-gram`. This is cross-kind categorization; application-specific compatibility hints come from relationship tags and compilers. |
| `portable` | All 34 current Backend types; strategies `inline`, `inverted`, `json`; generic handler and its 21 predicate records. Generic seeding requires both the Backend and ordinary strategy claims. |
| `nested-filter` | `logical_type:hyper-g`; operations `exists`, `all`; strategies `inline`, `inverted`, `json`, `side`, `graph`. |
| `near` | `logical_type:vector`, `operation:near`, `strategy:vector`. |
| `side` | Logical types `array`, `hyper-g`; strategy `side`; storage profiles `array`, `hyper-g`. |
| `vector` | `logical_type:vector`; operations `eq`, `near`; `strategy:vector`; `storage_profile:vector`; Backends `inmem`, `neo4j`, `surreal`, `pgvector`, `lance`, `milvus`, `milvus-lite`, `chroma`, `pinecone`, `qdrant`, `weaviate`, `redisearch`. Backend ownership is still decided only by `storage-roles:[vector]`. The `eq` claim preserves current vector-equality seeding. |
| `graph` | `logical_type:hyper-g`, `strategy:graph`, `storage_profile:hyper-g`, Backends `neo4j`, `surreal`. |
| `search` | `logical_type:long-text`, `strategy:inverted`, `storage_profile:long-text`, Backends `elasticsearch`, `opensearch`, `redisearch`. |
| `sql` | Backend types `sql`, `sqlite`, `duckdb`, `postgres`, `supabase`, `trino`, `clickhouse`, `mysql`, `seekdb`, `dolt`, `starrocks`, `oceanbase`, `mssql`, `oracle`, and `pgvector`; all built-in `database_dialect` and `sql_dialect` records in the SQL module; storage profile `array-short-text`; database extension. |
| `gram` | All GRAM normalizers/tokenizers, `strategy:sparse-gram`, `storage_profile:array-short-text`. |

The following are accepted built-in, dimension-filtered handler-candidate
rules. They are application-owned interpretations of optional claims, compile
to exact keys, and are not a general selector language:

| Candidate path | Tag hints used to nominate exact candidates |
| --- | --- |
| Portable ordinary filter | `portable=true` on Backend type and strategy, plus non-empty intersection of the logical type's and operation's `filter-domains`. |
| Portable near | `portable=true` on Backend type and `near=true` on logical type, operation, and strategy. |
| Portable vector equality | `portable=true` on Backend type and `vector=true` on logical type, `eq` operation, and strategy. |
| Portable nested quantifier | `portable=true` on Backend type and `nested-filter=true` on logical type, `exists`/`all` operation, and strategy. `side`/`graph` are descriptive here, not extra gates. |
| SQL ordinary override | `sql=true` plus `storage-roles` containing `row` on Backend type, `portable=true` on ordinary strategy, and non-empty type/operation `filter-domains`. Dialect metadata and compiler checks can still decline native execution. |
| SQL SideTable ordinary filter | The same SQL Backend facts, `side=true` on logical type and strategy, and non-empty type/operation `filter-domains`. In formula form: `backend.sql AND row-storage AND dtype.side AND strategy.side AND intersects(dtype.filter-domains, op.filter-domains)`. This covers current Array/HyperG side-table predicates; `nested-filter` is reserved for `exists`/`all`. |
| SQL sparse-GRAM overrides | The plugin's exact two-operation declaration remains authoritative; `gram=true` resolves/discovers the strategy but must not expand all array-domain operations. Current exact keys are `Array × contained_in × SQL Backend × sparse-gram` and `Array × array_contains × SQL Backend × sparse-gram`. The target declaration lives in inert SQL-handler record metadata and stores canonical `logical_type`, operation, and resolved strategy identifiers—not Python class literals. |
| SQL nested quantifier | The same SQL Backend facts and `nested-filter=true` on logical type, operation, and strategy. |

Provider plugins may retain exact inert dialect/operator declarations and
compiler-side semantic checks where they describe real implementation limits.
The owning provider/handler record contract validates its own exact structural
metadata; generic tag publication does not validate tag-carried references.
Executable plugins consume those declarations and may not rediscover candidate
dimensions from family prefixes or concrete logical-type/strategy class
literals after migration. The exact small metadata envelope is accepted with
the claim matrices; it does not require another tag.

The `nested-filter` row is a generalization of current HyperG behavior. Its
name and intended subjects are accepted as a built-in convention; an external
claim remains an unverified string declaration.

### Backend storage roles and ordered placement policy

`storage-roles` answers only what a Backend implementation can store. It does
not encode which Backend a profile prefers or which strategy should be chosen:

| Backend types | Claim |
| --- | --- |
| `elasticsearch`, `opensearch` | `[text]` |
| `file`, `json`, `pickle` | `[row, text]` |
| `inmem`, `surreal`, `redisearch` | `[row, text, vector]` |
| `neo4j`, `pgvector` | `[row, vector]` |
| `redis`, `mongodb`, `tinydb` | `[row]` |
| `sql`, `sqlite`, `duckdb`, `postgres`, `supabase`, `trino`, `clickhouse`, `mysql`, `seekdb`, `dolt`, `starrocks`, `oceanbase`, `mssql`, `oracle` | `[row]` |
| `lance`, `milvus`, `milvus-lite`, `chroma`, `pinecone`, `qdrant`, `weaviate` | `[vector]` |

Profiles need ordered policy, not an unordered claim. The current
`backend_families` list is preference order, and
`strategy_by_backend_family` uses first matching family. Converting either one
to an unordered tag set would lose semantics. The accepted replacement keeps the
profile as the sole placement-policy owner and makes each predicate consume a
canonical tag:

```yaml
profile:
  backend_preferences:
    - all:
        - {tag: search, equals: true}
        - {tag: storage-roles, contains: text}
    - {tag: storage-roles, contains: row}
  strategy_rules:
    - when:
        all:
          - {tag: search, equals: true}
          - {tag: storage-roles, contains: text}
      strategy: inverted
  default_strategy: inline
  reason: builtin-longtext
```

Exact field names are delegable. The envelope must preserve authored order and
first-match behavior and support a bounded conjunction. It is a
placement-owned policy format, not the deferred general selector language.

Exact built-in profile policy to preserve:

| Profile | Ordered Backend predicates | Default strategy | Ordered strategy rules |
| --- | --- | --- | --- |
| `default` | `storage-roles contains row` | `inline` | none |
| `vector` | `storage-roles contains vector` | `vector` | none |
| `json` | `storage-roles contains row` | `json` | none |
| `array` | `storage-roles contains row` | `inline` | `sql=true AND storage-roles contains row -> side` |
| `array-short-text` | `sql=true AND storage-roles contains row`; then `storage-roles contains row` | `sparse-gram` | `sql=true AND storage-roles contains row -> sparse-gram`; then `storage-roles contains row -> inline` |
| `hyper-g` | `storage-roles contains row` | `inline` | `sql=true AND storage-roles contains row -> side` |
| `long-text` | `search=true AND storage-roles contains text`; then `storage-roles contains row` | `inline` | `search=true AND storage-roles contains text -> inverted` |

The conjunctions preserve every built-in selection and avoid some incoherent
external candidates, but they are necessary rather than sufficient: row storage
does not prove SideTable/sparse-GRAM support, text storage does not prove
inverted-index support, and `sql` does not by itself create the SQL-dialect
relation required by the handler plugin. The ordered resolver may try a
self-declared candidate, then use its existing fallback or fail only the
affected explicitly authored binding when exact strategy/provider support is
absent. Publication does not certify `sql`, `search`, or `storage-roles`.

Accepted resolution semantics to preserve:

1. evaluate Backend predicates in authored order;
2. for each predicate, choose `default_backend` first when that concrete
   instance matches;
3. otherwise choose the first matching Backend type in Registry order, then
   the first workspace instance of that type in Backend-registry order;
4. stop at the first predicate that yields an instance;
5. if no predicate matches, return no profile-selected Backend; the enclosing
   storage resolver falls back to `default_backend`;
6. evaluate strategy rules against the profile-selected Backend only; on the
   no-match fallback, use `default_strategy` without re-evaluating rules.

Module-owned profile predicates are static `backend_type` queries. They cannot
read live overlays such as `backend-availability`, because durable placement
must not change with an observational connection hint. The placement consumer
reads raw claims, validates only the values/operators it needs, and reports a
local diagnostic or follows ordered fallback. It creates no tag-definition or
tag-value reverse dependency.

Workspace MetaSchema profile overrides are not Registry module records. The
accepted boundary is to validate them when materialized and on use against
the current Registry snapshot; they do not block module lifecycle globally and
become explicitly stale/invalid if a referenced definition/value/strategy is
removed. Reopen is the safe recovery boundary; there is no global workspace
dependency index.

Logical types do not receive storage-role claims. Their semantic categories
remain `side`, `vector`, or `search` where applicable, while JSON is identified
by `kind:logical_type, identifier:json`. A storage profile may claim those
category tags for inspection, but its executable placement truth remains the
ordered policy above.

### Backend categories and static behavior

Category claims:

| Tag | Backend types |
| --- | --- |
| `portable` | All 34 current Backend types. |
| `sql` | `sql`, `sqlite`, `duckdb`, `postgres`, `supabase`, `trino`, `clickhouse`, `mysql`, `seekdb`, `dolt`, `starrocks`, `oceanbase`, `mssql`, `oracle`, `pgvector`. |
| `graph` | `neo4j`, `surreal`. |
| `search` | `elasticsearch`, `opensearch`, `redisearch`. |
| `vector` | `inmem`, `neo4j`, `surreal`, `pgvector`, `lance`, `milvus`, `milvus-lite`, `chroma`, `pinecone`, `qdrant`, `weaviate`, `redisearch`. |

There is no `handler` tag: `kind:handler` and the exact handler-to-Backend
relation remain structural. Backend eligibility for the generic portable
compiler is not inferred from `kind:backend_type`; it requires
`portable:true`, preserving the current opt-in semantics of
`family:handler` for automatic generic seeding. A `vector` claim makes a
Backend discoverable as a vector candidate and tells routing it may try vector
work there. The claim is not certified: exact storage/handler/compiler/resource
checks may still skip, fall back, or fail that operation. Handler records do
not duplicate Backend category claims, and the minimal inspection model adds
no derived handler-category view. Their exact structural
`backend:<identifier>` relation remains authoritative for dispatch.

Near/filter claims omit the current `combined_scan` value. That path is the
portable baseline and is not currently used as an opt-in gate:

| Backend types | `near-filter-modes` |
| --- | --- |
| `pgvector`, `chroma`, `pinecone` | `[combined_native, metadata_prefilter]` |
| `lance`, `milvus`, `milvus-lite`, `qdrant`, `weaviate` | `[combined_native, metadata_prefilter, candidate_id_prefilter]` |
| `redisearch` | `[combined_native]` |
| Every other Backend | default `[]` |

Static row lookup claims:

| Backend types | `row-lookup` |
| --- | --- |
| Every Backend type except `redis` | `get_many=native`, `exists_many=native`, `count=native` |
| `redis` | `get_many=native`, `exists_many=native`, `count=scan` |

Implementation mode is static; best-effort connection state is live. A live
availability observation may permit an early decline but never replaces the
concrete operation check. Redis uses the same split for its native get/exists
and scan count modes rather than treating `provider_unavailable` as a third
algorithm.

Other non-default claims:

| Tag | Exact built-in claims |
| --- | --- |
| `binary-storage=base64` | Static: `json`, `elasticsearch`, `opensearch`, `tinydb`, `neo4j`, `redis`, `surreal`, `milvus`, `milvus-lite`, `chroma`, `pinecone`, `qdrant`, `weaviate`, `clickhouse`. Construction-derived: generic `file` when `fmt=json`. Every other Backend defaults to `bytes`. |
| `graph-traversal-native=true` | `neo4j` |
| `graph-endpoint-pushdown=true` | `neo4j` |
| `graph-endpoint-materialization=identity_anchor` | `neo4j`, `surreal` |
| `graph-incoming-lookup-native=true` | `neo4j` |
| `workspace-database-namespace=true` | `postgres`, `mysql`, `seekdb`, `dolt`, `oceanbase`, `mongodb`, `surreal`, `pgvector` |
| `unsupported-storage-types=[hyper-g]` | `lance`, `chroma`, `pinecone`, `qdrant` |
| `unsupported-storage-types=[array,json,hyper-g]` | `weaviate` |
| `unsupported-storage-types=[array,hyper-g]` | `clickhouse` |
| `compare-upsert=process` | `file`, `json`, `pickle`, `inmem` |
| `compare-upsert=provider` | `sqlite` |
| `atomic-upsert=true` | `sqlite`, `duckdb`, `postgres`, `supabase`, `mysql`, `seekdb`, `dolt`, `oceanbase`, `mssql`, `oracle`, `pgvector`, `surreal`, `neo4j`, `inmem`. The generic `sql` type, whose provider is not known, `clickhouse`, `starrocks`, `trino`, the File Backends, and every other Backend default to `false`. A SQL Backend whose engine may commit every statement (see the `atomic-upsert` tag above) overlays `false` at construction. |
| `strategy-adapters` | No static claims. Each live Backend reports its installed adapters. |
| `backend-availability` | No static claims. A live Backend reports `unknown`, `ready`, or `unavailable`. |

The `json` Backend's static base64 claim is a deliberate layer normalization:
current `JsonFileBackend` always constructs the inherited File Backend with
JSON format, so its effective value is base64 even though the inherited class
spec starts at bytes. Parity compares that current effective behavior. The
generic configurable `file` Backend remains construction-derived.

Current live-availability observations:

| Backend implementation | Proposed observation |
| --- | --- |
| `elasticsearch`, `opensearch`, `mongodb`, `tinydb`, `redis`, `surreal`, `chroma`, `milvus`, `pinecone`, `redisearch` | `ready` when the current provider references are usable; otherwise `unavailable`. |
| `neo4j` | `ready` when the current driver reference is non-null; otherwise `unavailable`. |
| `milvus-lite`, `qdrant`, `weaviate` | `unknown` while lazy connection is pending, `ready` with a client, and `unavailable` after failure, close, or drop. |
| Every other Backend | `unknown` until that Backend defines an accepted readiness observation. |

These observations do not invent readiness for other Backends. A non-null
client may remain after an operation error, so `ready` is a best-effort
diagnostic, not proof that the next operation succeeds. `unknown` allows normal
execution and its concrete resource checks; it does not mean unavailable.
`unavailable` may support an early explicit decline or diagnostic for the
reporting instance, but it does not mutate static behavior tags.

Current `scalar-index` profiles:

| Profile | Backend types | Value | Unique | Composite | Composite unique | Opt out | Supported/default types |
| --- | --- | --- | --- | --- | --- | --- | --- |
| F | `file`, `json`, `pickle`, `inmem` | yes | yes | no | yes | yes | Current 11 scalar types / none |
| E | `elasticsearch`, `opensearch`, `redis` | yes | no | no | no | yes | Current 11 scalar types / same 11 |
| N | `neo4j` | yes | yes | yes | no | yes | Current 11 scalar types / same 11 |
| S | `sql`, `sqlite`, `duckdb`, `postgres`, `supabase`, `mysql`, `seekdb`, `dolt`, `oceanbase`, `mssql`, `oracle`, `pgvector` | yes | yes | yes | yes | yes | Nine SQL scalar types / `identifier`, `short-text`, `integer`, `float` |
| U | `surreal`, `mongodb` | yes | yes | yes | yes | yes | Current 11 scalar types / same 11 |
| 0 | `trino`, `clickhouse`, `starrocks`, `tinydb`, `lance`, `milvus`, `milvus-lite`, `chroma`, `pinecone`, `qdrant`, `weaviate`, `redisearch` | no | no | no | no | no | none / none |

The “current 11 scalar types” are:
`boolean`, `categorical`, `date`, `duration`, `float`, `identifier`, `integer`,
`long-text`, `medium-text`, `short-text`, and `timestamp`.

The nine SQL scalar types are:
`boolean`, `categorical`, `date`, `duration`, `float`, `identifier`, `integer`,
`short-text`, and `timestamp`.

### Cross-domain category claims

| Tag | Exact built-in claim groups |
| --- | --- |
| `agent` | Entities `agent-message`, `agent-session`, `agent-skill`, `agent-agent`; extension and Toolkit family `agent`; agent MCP session opener; memory extension retains its current agent affiliation. |
| `capsule` | Entity `sys-capsule`; extension `capsule`; layers `source`, `import`, `cloudpickle`. |
| `database` | Six `db-*` Entities; extension, Toolkit family, and MCP profile `database`; all built-in physical database dialect records. |
| `executable` | Extension `capsule` and all Capsule layers. |
| `llm` | Gateways `anthropic`, `bifrost`, `litellm`, `mock`, `openai`, `portkey`; agent extension; `schema_text:llm`. |
| `mcp` | Agent, database, and Toolkit extensions; MCP-facing Toolkit families, functions, serializers, profiles, and session opener. |
| `memory` | Entity `memory-note`; extension `memory`; Toolkit families/profiles `memory`, `memstate`. |
| `prompt` | Entities `sys-prompt`, `sys-translation`; extension `prompt`. |
| `schema` | `schema_text:llm` and extension `database`. |
| `session` | Entity `agent-session`, extension `agent`, agent MCP session opener. |
| `skill` | Entity `agent-skill`, extension `agent`. |
| `toolkit` | Entity and extension `toolkit`; all eight Toolkit functions; Toolkit family `workspace`; Toolkit MCP profiles and serializers. |
| `translation` | Entity `sys-translation` and extension `prompt`. |

### No tag-on-tag claims

The accepted minimal inspection model does not make `kind: tag` records tag
subjects. Definitions do not claim `graph`, `filter`, `agent`, or other tags;
their name, description, subjects, and owning module provide direct
inspection. There is no tag inheritance, category propagation, or derived
handler-category view.

## Complete Built-In Module Coverage

This table covers all 40 built-in descriptors and 314 installed records. The
definition-owner table above is the complete placement authority for
definitions; this table summarizes each descriptor's current claims,
structural facts, or policy.

| # | Built-in descriptor | Current records | Current claims or policy |
| --- | --- | --- | --- |
| 1 | `src/heavenbase/backends/elasticsearch/meta.yaml` | `backend_type:elasticsearch`, `handler:elasticsearch` | Backend claims `portable`, `search`, `storage-roles:[text]`, `binary-storage:base64`, scalar profile E, and native row lookup; handler keeps structural `backend:elasticsearch`; availability is live. |
| 2 | `src/heavenbase/backends/file/meta.yaml` | Backend types `file`, `json`, `pickle` | All claim `portable`, `storage-roles:[row,text]`, compare-upsert process, scalar profile F, and native row lookup. `json` claims base64; `pickle` uses bytes; generic `file` derives representation from construction. |
| 3 | `src/heavenbase/backends/inmem/meta.yaml` | `backend_type:inmem`, `handler:inmem` | Claims `portable`, broad `vector`, `storage-roles:[row,text,vector]`, compare-upsert process, `atomic-upsert`, scalar profile F, and native row lookup. |
| 4 | `src/heavenbase/backends/meta.yaml` | 15 Backend tag definitions | Defines the static, construction, and live Backend claim vocabulary, including `backend-availability`; no legacy `capability` records remain. |
| 5 | `src/heavenbase/backends/mongodb/meta.yaml` | `backend_type:mongodb` | Claims `portable`, `storage-roles:[row]`, native row lookup, native bytes, workspace database namespacing, and the all-scalar index profile. |
| 6 | `src/heavenbase/backends/neo4j/meta.yaml` | `backend_type:neo4j`, `handler:neo4j` | Claims `portable`, broad `graph`/`vector`, `storage-roles:[row,vector]`, base64, four graph behavior tags, `atomic-upsert`, scalar profile N, and native row lookup; availability is live. |
| 7 | `src/heavenbase/backends/opensearch/meta.yaml` | `backend_type:opensearch`, `handler:opensearch` | Claims `portable`, `search`, `storage-roles:[text]`, base64, scalar profile E, and native row lookup; the handler relation remains structural and availability is live. |
| 8 | `src/heavenbase/backends/redis/meta.yaml` | `backend_type:redis`, `handler:redis` | Claims `portable`, `storage-roles:[row]`, base64, scalar profile E, native get/exists, and scan count; availability is live. |
| 9 | `src/heavenbase/backends/sql/meta.yaml` | 14 Backend types, 13 database dialects, 16 SQL dialects, `handler:sql` | Backend types claim `portable`, `sql`, `storage-roles:[row]`, and the behavior matrix (`atomic-upsert` on every type except the generic `sql`, `clickhouse`, `starrocks`, and `trino`); `database_dialect` records claim `database` and `sql`, while `sql_dialect` records claim only `sql`; physical-database tags and handler relations remain Registry-owned. |
| 10 | `src/heavenbase/backends/sql/strategies/gram/meta.yaml` | `tag:gram`, five normalizers, two tokenizers | The components claim `gram`; no executable or family-specific authority remains. |
| 11 | `src/heavenbase/backends/surreal/meta.yaml` | `backend_type:surreal`, `handler:surreal` | Claims `portable`, broad `graph`/`vector`, `storage-roles:[row,text,vector]`, base64, `identity_anchor`, workspace namespace, `atomic-upsert`, scalar profile U, and native row lookup; availability is live. |
| 12 | `src/heavenbase/backends/tinydb/meta.yaml` | `backend_type:tinydb` | Claims `portable`, `storage-roles:[row]`, base64, no scalar indexes, and native row lookup; availability is live. |
| 13 | `src/heavenbase/backends/vector/meta.yaml` | Nine vector Backend types and eight handlers | Claims `portable`, broad `vector`, exact per-Backend `storage-roles`, near/filter modes, row lookup, exclusions, scalar profiles, binary representation, and `sql` plus `atomic-upsert` for `pgvector`; the Milvus handler structurally owns both server and Lite Backend types. |
| 14 | `src/heavenbase/benchmark/meta.yaml` | `tag:benchmark-suites`, 13 benchmark combinations | Combinations claim suite values from `groups.suite`; the benchmark consumer interprets the strings and reports local diagnostics. |
| 15 | `src/heavenbase/capsule/meta.yaml` | Two tags, `sys-capsule`, extension `capsule`, three layers | Entity/extension/layers claim `capsule`, while the extension/layers claim `executable`; activation and trust remain structural. |
| 16 | `src/heavenbase/database/meta.yaml` | Seven database tag definitions | Defines broad `database` plus the six physical database behavior claims consumed by database dialect records. |
| 17 | `src/heavenbase/execution/handlers/meta.yaml` | `tag:portable`, generic handler, 21 predicates | Generic handler and predicates claim `portable`; typed predicate `dtype` relations remain structural. |
| 18 | `src/heavenbase/extensions/agent/meta.yaml` | Three tags, four Entities, extension, family, session opener | Owns `agent`, `session`, and `skill`; exact applicable subjects also claim `llm` or `mcp`. |
| 19 | `src/heavenbase/extensions/agent_log/meta.yaml` | `entity:agent-trace`, extension `agent-log` | The Entity claims `agent` and `session`. The optional extension claims `agent`, `llm`, and `session`. A workspace can opt in with `ws.enable_extension("agent-log")`. The dashboard assistant session log enables it for its own workspace. |
| 20 | `src/heavenbase/extensions/database/meta.yaml` | Seven Entities, extension, family, profile | Claims core-owned `database`; exact applicable subjects also claim `sql`, `schema`, and `mcp`. |
| 21 | `src/heavenbase/extensions/memory/meta.yaml` | `tag:memory`, Entity, extension, two families, two profiles | Claims `memory` and retains intentional `agent` affiliation on the extension. |
| 22 | `src/heavenbase/extensions/prompt/meta.yaml` | Two tags, two Entities, prompt function, extension | Owns `prompt` and `translation`; exact subjects claim them according to the category matrix. |
| 23 | `src/heavenbase/extensions/python/meta.yaml` | Extension, toolkit family, and MCP profile `python` | Each claims `mcp`; the profile exposes `run_python` and `python_guide`. |
| 24 | `src/heavenbase/filters/meta.yaml` | Four tags, 21 operations | Owns `filter`, `filter-domains`, `nested-filter`, and `near`; operations claim broad `filter` plus exact domains or relationship tags. |
| 25 | `src/heavenbase/interop/meta.yaml` | Six inferers | Exact `interop_inferer` kind is the authority; no redundant semantic `interop` claim exists. |
| 26 | `src/heavenbase/llm/gateways/anthropic/meta.yaml` | `llm_gateway:anthropic` | Claims broad `llm`; exact kind remains the gateway-shape authority. |
| 27 | `src/heavenbase/llm/gateways/bifrost/meta.yaml` | `llm_gateway:bifrost` | Claims broad `llm`; exact kind remains the gateway-shape authority. |
| 28 | `src/heavenbase/llm/gateways/litellm/meta.yaml` | `llm_gateway:litellm` | Claims broad `llm`; exact kind remains the gateway-shape authority. |
| 29 | `src/heavenbase/llm/gateways/mock/meta.yaml` | `llm_gateway:mock` | Claims broad `llm`; exact kind remains the gateway-shape authority. |
| 30 | `src/heavenbase/llm/gateways/openai/meta.yaml` | `llm_gateway:openai` | Claims broad `llm`; exact kind remains the gateway-shape authority. |
| 31 | `src/heavenbase/llm/gateways/portkey/meta.yaml` | `llm_gateway:portkey` | Claims broad `llm`; exact kind remains the gateway-shape authority. |
| 32 | `src/heavenbase/llm/meta.yaml` | `tag:llm` | Owns the broad LLM category definition used by gateways and LLM-facing records. |
| 33 | `src/heavenbase/placement/meta.yaml` | Six placement tag definitions | Owns `storage-roles` and the broad `graph`, `side`, `vector`, `search`, and `sql` categories. |
| 34 | `src/heavenbase/query/meta.yaml` | Three tags, 12 aggregate operations, 12 reducers | Owns aggregate categorization and input modes; count remains terminal before execution, not a frame mutation. |
| 35 | `src/heavenbase/schema/meta.yaml` | `tag:schema`, seven storage profiles, `schema_text:llm` | Profiles own exact ordered Backend/strategy policy and inspection claims; the renderer claims `schema` and `llm`. |
| 36 | `src/heavenbase/strategies/meta.yaml` | Eight strategies | `inline`, `inverted`, and `json` claim `portable`; exact strategies also claim relevant filter, placement, and search categories. |
| 37 | `src/heavenbase/toolkit/meta.yaml` | Two tags, Toolkit Entity/extension, nine functions, two serializers, MCP transform, family, two profiles | Owns `toolkit`; MCP-facing records claim `mcp`, while exact dependency records remain structural. |
| 38 | `src/heavenbase/types/meta.yaml` | 16 logical types | Claims broad `filter` where applicable plus exact aggregate, filter, placement, graph, and search categories; no storage-role duplication. |
| 39 | `src/heavenbase/workspace/catalog/meta.yaml` | Fixed Entity `sys-catalog` | No semantic `system` claim; fixed identity and required installation remain hard system invariants. |
| 40 | `src/heavenbase/workspace/metaschema/meta.yaml` | Fixed Entity `sys-metaschema` | No semantic `system` claim; fixed identity and required installation remain hard system invariants. |

## Current Vocabulary Disposition

Every current `family` value has an explicit destination:

| Current `family` value(s) | Target |
| --- | --- |
| `agent`, `capsule`, `database`, `llm`, `mcp`, `memory`, `prompt`, `schema`, `toolkit` | Same kebab-case marker tag on the non-redundant subjects in the claim matrices. |
| `aggregate` | Marker tag `aggregate`. |
| `aggregate_group`, `aggregate_numeric`, `aggregate_order` on logical types | One typed `aggregate-modes` claim with values `group`, `numeric`, `order`. |
| Reducer `meta.reducer.type_families` values `aggregate_order`, `aggregate_numeric` | `aggregate-input-modes:[order]` or `[numeric]`; current empty input migrates to absence/default `[]`, whose named reducer-input semantics are unconstrained. |
| `filter` | Broad cross-kind marker on filter-related types, operations, and strategies; current ordinary filter strategies additionally claim `portable` for exact generic seeding. |
| `filter_array`, `filter_bool`, `filter_json`, `filter_order`, `filter_struct`, `filter_text` | One typed `filter-domains` claim. `filter_bool` becomes canonical value `boolean`. |
| `filter_side` | Marker/category `side` on the relevant logical types; nested compatibility uses `nested-filter`. |
| `filter_vector`, `vector`, `near` | `vector` and/or `near` according to the subject's exact role. |
| `side_filter` | `nested-filter` on `strategy:side`; broad representation also claims `side`. |
| `graph`, `search`, `sql`, `portable` | Same marker tags on the allowed, non-redundant subjects in the claim matrices. |
| `gram` | Marker tag on GRAM components, strategy, and profile. |
| `storage_row`, `storage_text`, `storage_vector` on Backend types | Values `row`, `text`, `vector` inside the Backend's `storage-roles` claim. |
| `storage_row`, `storage_vector`, `sql`, `search` on storage profiles | Ordered Backend predicates in the profile's placement policy; category claims remain only where useful for inspection. |
| `storage_side` | `side` category on Array/HyperG/profile subjects; nested routing uses `nested-filter`. |
| `storage_json` | Remove; exact logical type/profile identity and the profile's ordered policy are already authoritative. |
| `storage_long_text` | `search` on the long-text logical type/profile; placement remains ordered profile policy. |
| `handler` on Backend types | Replace with `portable`; this preserves opt-in to generic scan-handler seeding without confusing it with `kind:handler`. |
| `interop` | Remove; every current claimant is already identified by exact `kind:interop_inferer`. |
| `system` | Remove; fixed system Entity identity and installer-owned lifecycle/trust policy are authoritative. |
| `all` | Remove; record-kind inspection already supplies “all.” |
| `backend` | Remove; direct definition inspection identifies Backend-oriented tag definitions. |
| `compiler` | Remove; `kind: handler` identifies compiler contributions. |
| `physical-database` | Remove; `kind: database_dialect` is exact. |
| `sql-handler` | Remove; `kind: sql_dialect` is exact. |

Other group namespaces:

| Current group/index | Disposition | Reason |
| --- | --- | --- |
| `bundle:<id>` | Keep structural. | Installation ownership and atomic lifecycle. |
| `provenance:system` | Keep structural and installer-owned. | Origin/trust metadata must not be author-forgeable. |
| Installer `tag:builtin` | Keep reserved, structural, and excluded from the Tag API. | Required-extension activation currently treats it as installer trust evidence; relocating it is unrelated scope. |
| `backend:<id>` on handlers | Keep structural or promote to an exact relation field. | It is a foreign-key relation, not categorization. |
| `dtype:<id>` on typed predicates | Keep structural or promote to an exact relation field. | It is exact predicate applicability. |
| `sqlglot:<id>` | Keep structural or promote to an explicit dialect alias field. | It maps an adapter name exactly. |
| `scope:system` | Keep as the existing module/extension required-activation policy; this is unrelated to the abolished workspace-scope concept. | It requests system activation only. Installer-owned provenance/`builtin` evidence establishes trust separately. |
| `scope:generic`, `scope:typed` | Remove as redundant. | Predicate fields already distinguish generic and typed resolution. |
| `scope:global` | Remove as redundant. | Fixed Entity identity owns the invariant. |
| `suite:*` | Migrate to `benchmark-suites`. | Authored semantic selection uses the one tag-claim representation and application-owned validation. |
| Semantic extension `tag:*` values | Migrate to ordinary Boolean tag claims. | Removes the separate extension-only taxonomy. |

Current extension records also duplicate their group tags inside
`meta.definition.tags`. After parity, keep only the canonical subject claim and
derive any extension-facing view from it.

## Static and Effective Inspection API

Exact SDK names are delegable. The accepted contract is a minimal direct API
that preserves raw claims, optional typed status, and the separation between a
Context-owned static view and an explicit live-object view.

| Proposed API | Contract |
| --- | --- |
| `TagDefinition` | Immutable facade over one usable `kind: tag` record. Exposes identifier, schema, default, intended subjects/layers, description, and Registry evidence. Malformed records produce a local invalid-definition diagnostic. |
| `ResolvedTagValue` | Immutable explicit claim or definition default with `resolved`, `unresolved`, `invalid`, or `ambiguous` status; raw value; optional typed value/definition; subject; source layer; and snapshot/provenance. |
| `resolver.tag_definition(identifier)` | Inspect a definition or its local invalid/absent state inside exactly one Context. Never import target code. |
| `resolver.tags(kind, identifier)` | Return explicit static claims plus resolvable defaults for one installed record. Unknown claims remain visible. |
| `resolver.tag(kind, identifier, tag)` | Return one static tag result without rewriting the record or requiring a definition. |
| `backend.tags()` | Return the effective view for one concrete Backend: static claims plus construction and live overlays. |
| `backend.tag(identifier)` | Return one effective tag result. |

An ergonomic public facade may be named `hb.tags`, but it must bind to the
selected/default Context in the same way as other Registry-backed facades. It
must not own process-global mutable definitions.

### Static inspection

Illustrative API:

```python
ctx = hb.Context.load()
modules = ctx.modules()
modules.setup()

neo4j = modules.tags("backend_type", "neo4j")
assert neo4j["graph"].value is True
assert neo4j["graph-traversal-native"].value is True
assert neo4j["backend-availability"].value == "unknown"
assert neo4j["graph"].source == "record"
```

Tag setup and inspection import no registered target such as Neo4j. Opening
the Context's explicitly configured control Backend remains the documented
cold-bootstrap exception discussed below; inspection does not construct any
inspected provider or assert that a Neo4j instance is connected.

### Effective live inspection

```python
ws = hb.HeavenBase.load("research", context=ctx)
graph = ws.backends.get("primary-graph")

availability = graph.tag("backend-availability")
if availability.value == "unavailable":
    raise RuntimeError(f"Graph Backend is {availability.value}")

if graph.tag("graph-traversal-native").value:
    # Still only a candidate. The exact compiler and resource checks decide.
    rows = ws.query("paper").where(...).execute().rows()
```

Live values are not persisted into module records. Reopening reconstructs the
safe effective view. An `unknown` availability value continues to the concrete
resource checks; it is not an execution denial. The ResultFrame returned by
`execute()` is treated as a transport container and explicitly exported rather
than printed or implicitly coerced.

### Typed tag use

```python
modes = ctx.modules().tag("logical_type", "integer", "filter-domains").value
assert modes == ("order",)

lookup = graph.tag("row-lookup").value
assert lookup["get_many"] == "native"
```

The returned Python facade may use tuples and immutable mappings, but the
authoritative wire value remains canonical JSON-compatible data.

### Existing capability-index boundary

`heavenbase.capabilities.CapabilityIndex`, the default
`heavenbase.capabilities` functions, and `ws.capabilities` are not the Backend
capability-definition authority being removed. They are the specialized
inspection projection over exact registered `HandlerKey` rows and, for a
workspace, concrete Backend instances. That exact support matrix remains useful
and keeps the name “capabilities.”

Accepted minimal boundary:

- `CapabilityIndex.capabilities()` and `.supports()` remain exact
  type/operation/Backend/strategy inspection;
- `hb.capabilities` and `ws.capabilities` do not become generic tag registries;
- `LogicalTypeOption`, `StrategyOption`, `BackendOption`,
  `CapabilityOption`, and `HandlerKey` do not copy raw or resolved semantic
  tags and expose no derived category view;
- `CapabilityIndex.supports()` consults only exact registered HandlerKeys and
  never availability or tags;
- generic tag definitions and claims remain owned by the Context's
  `ModuleService`.

The current `family=` filters on
`CapabilityIndex.logical_types()`, `.strategies()`, and `.backends()`, plus the
default facade functions, are removed without a selector replacement. Callers
use direct Tag inspection for semantic metadata and CapabilityIndex for exact
support. Do not retain `family=` as a permanent translation shim.

### MetaSchema and MCP projection boundary

`MetaSchema.tags` is already a flat, derived search projection; it is not
limited to authored user-schema metadata. Current module-row projection writes
`registry`, the module record kind, and every value from every Registry group.
Current extension publication separately writes `Extension.tags`. This makes
the list useful for search, but it also flattens semantic families, structural
relations, lifecycle/trust labels, and extension-side duplicate data without
type or provenance.

It must not become a Tag authority. Flattening `row-lookup`,
`scalar-index`, false markers, or non-Boolean values into a string list would
be lossy. The accepted minimal boundary is:

- authored claims remain solely in Registry `meta.tags`;
- direct `ModuleService`/Tag API inspection returns raw and resolved status;
- flat `MetaSchema.tags` retains structural/search labels only, using an
  explicit installer-owned allowlist rather than copying semantic claims;
- MetaSchema does not store a semantic `tag_view`, live Backend tag payload,
  or derived HandlerKey category view;
- `_safe_record_meta()` redacts the raw claim-authoring mapping when projecting
  Registry metadata so a MetaSchema row cannot become a second claim surface;
- MCP may delegate a direct read to the same Tag API, but stores or invents no
  semantic projection of its own;
- exact handler/backend/dialect relations remain in their structural fields or
  safe registration metadata;
- `Catalog` and `MetaSchema` fields remain unchanged.

Exact SDK/MCP method names and the structural-label allowlist are delegable
within this boundary.

### Module authoring

An external module may define a prefixed tag and claim an existing built-in
tag in one package:

```yaml
manifest_version: 2
coordinate: acme/geo
version: 1.0.0
compatibility: {heavenbase: {min: 0.1.2.1, before: 0.1.3.0}}
bundle: acme-geo
items:
  - kind: tag
    identifier: acme-geo-index-level
    name: Geo index level
    source: inline
    target: definition
    active: true
    meta:
      schema_version: 1
      version: 1.0.0
      definition:
        schema:
          type: enum
          values: [none, bounding-box, exact]
        default: none
        subjects: [backend_type]
        layers: [static]

  - kind: backend_type
    identifier: acme-geo
    name: Acme Geo Backend
    source: python-import
    target: acme_geo.backend:GeoBackend
    active: true
    meta:
      schema_version: 1
      version: 1.0.0
      tags:
        graph: true
        storage-roles: [row]
        acme-geo-index-level: exact
```

The prefix is recommended, not mandatory. An unqualified definition is allowed
but receives no special namespace protection: an ordinary
`("tag", identifier)` ownership collision fails deterministically, while an
undefined unqualified claim risks later reinterpretation if a definition with
that identifier appears. A module may also claim a tag without defining it.

If this Backend is routable, its handler remains a separate exact
`kind:handler` record with the structural `backend:acme-geo` relation; no
`handler` tag is required.

## Selector Boundary

A general selector language is explicitly deferred and is not required to ship
the tag kernel. Named tag consumers use direct claims without exposing
user-authored Boolean expressions.

Why defer it:

- exact handler keys already solve runtime dispatch;
- absence, default, unknown definition, inactive record, and live-only values
  need precise truth semantics;
- negative expressions are dangerous in an open vocabulary;
- priority and conflict rules can accidentally create a second planner;
- a selector over static records cannot silently depend on live overlays;
- provider allowlists and executable compiler checks remain exact evidence.

Any future selector requires a separate design. It is not an optional
implementation slice hidden inside this refactor.

## Sole-Authority Decision

Accepted on 2026-07-24: static truth lives only in the published subject
record after publication.

### Option A — module record is the sole static authority (accepted)

After installation, the accepted claim field on the subject's published module
record is the only static claim authority. That field is `meta.tags`. Python class
`families`, `capability_spec`, and extension-side tag lists are removed after
migration. A built-in registration helper may accept claims as authoring input
before publication, but runtime resolution never falls back to the class.

Manifest installation never imports a class to discover that input. An
explicit SDK `register(..., tags=...)` call may translate caller-supplied data
into the record it publishes; once published, that record—not the class or
caller's mutable object—is the static authority.

Benefits:

- import-free inspection is complete;
- built-in and external modules have identical static semantics;
- raw claims round-trip without target imports or global semantic coupling;
- one Registry snapshot explains routing candidates;
- no drift between class and manifest.

Costs:

- all built-in class declarations must migrate;
- construction-critical control-Backend facts must remain explicitly
  represented in the dedicated bootstrap source before Registry publication;
- authors must update manifest claims when implementation behavior changes;
- parity tests must catch manifest/class drift while both exist temporarily.

### Option B — records and classes remain co-authoritative (rejected)

The Registry stores some claims, while resolved classes may add or replace
static claims indefinitely.

Benefits:

- smaller initial migration;
- class-local declarations remain convenient for Python authors.

Costs:

- import-free static inspection is knowingly incomplete;
- built-ins and external inert modules are not peers;
- validation cannot see the whole contract before import;
- precedence and cache invalidation become permanent complexity;
- the refactor preserves the split truth it exists to remove.

Option B conflicts with the single-source-of-truth goal and is rejected.

## Before/After Authority Map

| Concern | Current authority | Target authority |
| --- | --- | --- |
| Family/category membership | `ModuleRecord.groups.family`; class `families` in some domains | Open claims on the published subject record |
| Extension categories | `groups.tag` plus duplicated `meta.definition.tags` | Open claims on the extension record |
| Tag definitions | None for families/tags; `kind: capability` for Backend-only values | Optional `kind: tag` records for typed inspection |
| Static Backend behavior | Python `Backend.capability_spec` | Backend type record claims |
| Cold control-Backend/dialect facts | Class capability spec plus bootstrap dialect config | Minimal construction-only `BootstrapSpec`/adapters, separate from Registry and normal configuration |
| Construction-dependent behavior | Mutated instance `capability_spec` | Construction overlay on the effective tag view |
| Provider readiness | `capability_overrides()` mixed into values | Live `backend-availability` plus other truly live overlays |
| Physical database caps | `DatabaseDialectSpec.definition.caps` | Database dialect record claims |
| Aggregate type/reducer compatibility | Logical-type families plus reducer `type_families` | Named aggregate consumer interprets `aggregate-modes` and `aggregate-input-modes` claims |
| Backend storage support | Backend/profile family strings | Backend `storage-roles` self-declaration consumed by placement |
| Storage placement order and strategy overrides | `backend_families` and `strategy_by_backend_family` in profiles | One ordered profile policy whose application-owned predicates consume tags and keep local fallback |
| Filter compatibility | `filter_*` family-name prefix intersection | Named seeding consumer interprets `filter-domains` intersection |
| Generic scan seeding | Backend `family:handler` plus strategy `family:filter` | Named seeding consumer reads `portable=true` claims and emits exact HandlerKeys |
| Nested HyperG eligibility | Literal `GraphEdge.identifier` rule | Accepted `nested-filter` candidate hint; exact HandlerKey/compiler remains proof |
| Handler relation to Backend | `groups.backend` | Keep exact structural relation or explicit relation field |
| Exact dispatch | `HandlerKey` | Unchanged |
| Execution proof | compiler and concrete resources | Unchanged |
| Installer trust | `provenance:system` plus current `tag:builtin` | Keep both installer-owned structural groups, excluded from the Tag API |

## Public API Migration Map

Names in the target column remain delegable; the behavioral boundaries are
accepted.

| Current API/data | Target |
| --- | --- |
| `ModuleRecord.groups`, `ModuleService.groups()` for semantic families | Direct raw/resolved Tag inspection and application-owned hint indexes |
| `BackendType.families()`, `in_family()`, `identifiers_for()` | Exact tag queries or domain-specific facades backed only by tags |
| `BACKEND_FAMILY_*` and other family constants | Canonical tag identifiers local to their named consumers, or remove constants entirely |
| class `families` tuples | Static record claims; remove runtime fallback |
| `BackendCapability` | `TagDefinition` facade |
| Backend capability registry/facade | Generic Context-owned tag definitions |
| `BackendCapabilities` | Immutable static/effective `TagView`; domain facade only if needed |
| class `capability_spec` | Static record claims |
| `capability_overrides()` | Explicit live/construction tag overlay provider |
| `Backend.capability()` | `Backend.tag()` |
| `Backend.capabilities()` | `Backend.tags()` |
| `Backend.supports()` | Boolean `Backend.tag()` convenience only if it remains clear; no separate registry |
| `Extension.tags` and duplicated definition tags | Extension record claims |
| `DatabaseDialectSpec.definition.caps` | Database dialect record claims |
| storage profile `backend_families` | Placement-owned ordered tag-predicate field |
| `strategy_by_backend_family` | Placement-owned ordered tag-oriented strategy-rule field |
| `filter_*` prefix logic | Named seeding consumer interprets `filter-domains` intersection |
| HyperG `GraphEdge.identifier` branch | Named seeding consumer interprets `nested-filter` |
| MetaSchema/MCP projections that expose families/capabilities | Flat MetaSchema remains structural; MCP may delegate direct Tag inspection without storing a projection |
| `CapabilityIndex`, `hb.capabilities`, `ws.capabilities` | Retain unchanged as the exact HandlerKey support projection |
| Atomic option `.families` | Remove; callers inspect the subject directly through the Tag API |
| Composite `CapabilityOption` families and `HandlerKey.families()` | Remove; no derived Tag view is added |
| `logical_types/strategies/backends(family=...)` | Remove with no selector shim |
| `register_logical_type(..., families=...)` and its class helper | `tags={...}` raw authoring input published on the logical-type record |
| `register_op(..., families=...)` | `tags={...}`; remove implicit `filter_*` prefix expansion |
| `register_strategy(..., families=...)` and its class helper | `tags={...}` authoring input on the strategy record |
| `Backend.register(..., families=...)` and internal `BackendType._register` | `tags={...}` authoring input on the Backend-type record |
| `register_aggregate_reducer(..., type_families=...)` | `tags={"aggregate-input-modes": [...]}` plus the canonical `aggregate` claim |
| `logical_type_families`, `op_families`, `strategy_families`, and Backend family helpers | Generic one-subject tag inspection or a named domain facade; delete family readers |
| Public exports and type stubs for capability/family APIs | Generic tag exports and stubs while retaining the specialized exact CapabilityIndex surface |

No permanent compatibility shim should preserve both authorities. Temporary
parity readers are test scaffolding with a deletion condition and must not
become public API.

Programmatic registration remains an explicit Registry mutation. Its `tags`
argument is only pre-publication authoring input, copied into the published
record. A definition need not exist and registration never creates one as
shorthand. The published record becomes the sole static claim authority.

## Implementation Plan

### Stop gate 0 — accept and promote the design

Before runtime edits:

1. confirm the accepted outcomes in the decision ledger;
2. promote this design into
   `docs/resources/architecture/` and, if appropriate, a focused ADR;
3. create one active task in `docs/tasks.yaml`;
4. capture the then-current source inventory as test fixtures or generated
   expectations.

### Slice 1 — open tag kernel and bootstrap isolation

**Goal:** store every claim without semantic coupling, optionally resolve it
through an inert definition, and preserve the existing construction-only
bootstrap boundary.

Deliver:

- `kind: tag` definition parsing;
- the accepted declarative schema grammar;
- verbatim `meta.tags: string -> JSON` claim storage;
- per-claim `resolved`, `unresolved`, `invalid`, and `ambiguous` views;
- optional subject/value/reference diagnostics with no tag-semantic
  publication rejection;
- soft string references with no tag-owned `meta.dependencies`;
- a narrow common read protocol for construction-only bootstrap and
  Registry-backed sources, with separate instances and state;
- Context-local direct-view and optional hint-index invalidation;
- no routing or behavior change.

Focused proof:

- strict definition/default parsing when explicitly resolved;
- unknown, invalid, unexpected-subject, ambiguous, and missing-target claims
  remain stored and affect only their own diagnostic/consumer;
- a definition and claim may arrive in either order;
- adding/removing/changing a definition changes only the derived view;
- structural `meta.dependencies` remain strict and separate;
- cold SQLite and PostgreSQL/MySQL namespace construction uses only
  `BootstrapSpec`/bootstrap adapters until Registry binding;
- bootstrap never becomes ConfigManager/workspace config or a Registry/Tag API
  instance;
- installation remains import-free.

### Slice 2 — built-in definitions and static claims

**Goal:** publish the distributed 48-definition logical catalog and migrate
built-in static claims while keeping semantic enforcement out of the Registry
kernel.

Deliver:

- publish the accepted 48 built-in definitions under their semantic owners;
- add all built-in claims from the matrices;
- add no tag-on-tag claims;
- retain and enforce the reserved installer-owned `tag:builtin` and
  `provenance:system` groups while excluding both from the Tag API;
- keep old family/capability readers temporarily for parity only;
- add source-owned lint/contract fixtures for built-in definitions and claims;
- no reader cutover.

Focused proof:

- all 32 current descriptors plus the three accepted definition-owner
  descriptors install;
- all 242 existing records reconcile whether or not every optional definition
  is present;
- exact expected claim snapshots;
- a third-party record can use an undefined tag or unqualified external tag;
- a conflicting `kind: tag` definition still fails through ordinary Registry
  identity/ownership;
- no author tag can grant installer trust;
- package version reconciliation remains `0.1.2.1`.

### Slice 3 — minimal static/effective inspection

**Goal:** expose direct Context-bound claim status and concrete Backend
overlays without copying semantic tags into other inspection systems.

Deliver:

- immutable `TagDefinition`, `ResolvedTagValue`, and static tag-view facades;
- Context-bound raw/definition-backed claim resolution;
- derived alias handling that reports ambiguity without rewriting records;
- construction/live overlay precedence and provenance;
- optional owner-local hint indexes only where a named consumer needs them;
- import-free SDK inspection and optional MCP delegation to the same API;
- structural-only flat MetaSchema labels;
- no CapabilityIndex/HandlerKey Tag copies or derived category views;
- no behavior cutover.

Focused proof:

- two Contexts with different definitions/claims remain isolated;
- publication invalidates only the owning Context's derived view;
- unknown, invalid, ambiguous, absent, and definition-default states are
  distinguishable;
- raw claims remain visible after a definition is retired or removed;
- two live instances of one Backend type may expose different overlays;
- MetaSchema and CapabilityIndex remain non-authoritative and minimal;
- no target import occurs during inspection.

### Slice 4 — family and placement migration

**Goal:** replace semantic family readers with named application consumers of
open claims while preserving order, fallback, and local failure isolation.

Deliver:

- switch taxonomy/discovery consumers from semantic groups to tags;
- switch group-by and reducer/source logic to its locally validated
  `aggregate-modes` and `aggregate-input-modes` interpretation;
- switch Backend support to `storage-roles` and profile placement to the
  accepted ordered tag-predicate policy;
- migrate benchmark suites and extension category inspection;
- make tag-derived placement hints snapshot-bound, with atomic best-effort
  refresh or an explicit stale/reopen-required result;
- keep CapabilityIndex exact and tag-free;
- retain structural groups;
- delete migrated family constants and duplicate extension definitions once
  parity passes.

Focused proof:

- placement results match the existing suite for every built-in profile and
  Backend combination;
- a false or malformed third-party placement claim affects only that candidate:
  ordered fallback continues or an explicitly authored binding fails locally;
- groupability and all seven reducer/source combinations match current
  behavior, including unconstrained `count`;
- undefined tag definitions do not prevent raw built-in claims from being
  consumed by their named application logic;
- extension and Toolkit/MCP assembly matches existing behavior;
- structural ownership/provenance relations remain intact;
- no `family` semantic reader remains in migrated paths.

### Slice 5 — Backend and dialect behavior migration

**Goal:** move static Backend/dialect self-declarations to records and keep
construction/live facts instance-local.

Deliver:

- switch static Backend capability reads to tag claims;
- implement construction and live overlays with provenance;
- split readiness into `backend-availability`;
- switch dialect cap readers;
- keep `ScalarIndexSpec` only as a typed facade if useful;
- remove `kind: capability`, class static specs, and the capability registry
  after exact parity.

Focused proof:

- all Backend claim matrices match current behavior;
- live unavailable providers never erase their declared implementation modes;
- construction-dependent File binary representation updates correctly;
- live adapter changes update effective views without altering static records;
- reopen reconstructs a safe view;
- malformed or false third-party claims do not invalidate Context; the exact
  affected consumer warns, falls back, declines, or fails;
- exact external-provider integration tests remain the proof for provider
  behavior.

### Slice 6 — hidden handler-rule migration

**Goal:** let named tag consumers nominate exact handler candidates without
making a tag claim execution proof.

Deliver:

- replace Backend `family:handler` with `portable:true`;
- replace strategy `family:filter` with `portable:true` only for the ordinary
  filter-domain path; near and nested paths use exact `near` and
  `nested-filter`, while the proposed vector-equality and SQL SideTable paths
  use the explicitly named, dimension-filtered category rules above;
- replace the `filter_*` prefix rule with application-owned domain
  intersection;
- replace the concrete GraphEdge branch with the accepted nested relation;
- replace SQL-family and SideTable family/literal expansion with the accepted
  named tag rules; let `gram` resolve the sparse-GRAM strategy while retaining
  its exact two-operation plugin declaration and other real dialect/compiler
  allowlists;
- compile declarations to the same exact `HandlerKey` set;
- make tag-derived seeding input snapshot-bound while keeping the resulting
  exact HandlerRegistry under its existing runtime authority;
- keep provider-specific executable allowlists where they express real
  implementation limits.

Focused proof:

- snapshot exact handler keys before and after for all built-ins;
- external peer module can participate without host-code edits;
- a Backend without `portable:true` is never generic-seeded; a strategy without
  it is never seeded through the ordinary filter-domain path but may
  participate through an accepted explicit vector/nested relationship;
- SQL ordinary, side, GRAM, and nested key sets match current plugin seeding,
  including Array/HyperG SideTable filters and HyperG `exists`/`all`;
- unknown or malformed claims are diagnosed/skipped by the affected seed path,
  never rejected at module publication;
- a false positive claim may nominate a candidate but cannot bypass exact key,
  compiler, or resource checks;
- an explicit exact provider handler route remains usable without a semantic
  tag claim;
- compiler declines remain explicit and unchanged;
- no runtime tag-expression search enters dispatch.

### Slice 7 — authority cleanup and handoff

Deliver:

- remove temporary parity code and legacy semantic groups;
- remove dead capability/family APIs, exports, stubs, docs, and tests;
- update `README.en.md`, generated README copies, architecture docs, MCP
  metadata, and intuitive API examples;
- generate the durable logical built-in tag reference from installed
  `kind:tag` descriptors or source-owned fixtures. Drift checks cover the
  built-in convention only; they do not make definitions mandatory for
  external claims;
- run repository drift/docs checks;
- add one concise DEVLOG entry;
- remove the completed queue task and obsolete scratch copy.

Exit condition:

```text
one Registry
+ one open meta.tags claim representation
+ optional kind:tag typed definitions
+ one static subject-claim authority
+ explicit live overlays
+ exact HandlerKey/compiler proof
+ zero runtime fallback to family/class capability truth
+ zero global tag-semantic publication gates
```

## Verification Strategy

The user requested fewer, more focused test runs. During implementation, run
the narrow owner suite for each slice and consolidate broad checks at defined
milestones rather than after every edit.

Required fitness tests:

| Area | Required evidence |
| --- | --- |
| Value grammar | Each schema kind, nested objects, invalid defaults, strict no-coercion, deterministic set/map resolution, null rejection, subset constraints, and record-local diagnostics. |
| Permissive publication | Undefined, invalid, unexpected-subject, ambiguous, and missing-target claims remain stored; unrelated records and Context startup remain usable. |
| Lifecycle | Definition addition/removal/replacement changes only derived status; tag claims create no reverse dependency or inactive closure. Structural dependencies remain unchanged. |
| Identity | Raw string keys/values, advisory prefixes, derived aliases/ambiguity, ordinary `(kind, identifier)` collision and owner rules. |
| Inertness | Manifest scan/install/static inspection imports no targets and executes no Capsule. |
| Cold bootstrap | Fresh/reopened Contexts use the dedicated bootstrap mapping and construction adapters; bootstrap and Registry share no instance/cache/config state; normal Tag reads never consult bootstrap. |
| Context isolation | Different Contexts can own different tag snapshots with no shared materialized state. |
| Cache coherence | Publication invalidates direct Tag views and optional hint indexes; tag-derived placement/seeding inputs refresh atomically or fail stale, while exact HandlerRegistry/CapabilityIndex retain their own authority. |
| Effective overlays | Static, construction, and live precedence; whole-value replacement; provenance; safe reopen; two instances of one Backend type resolve different construction/readiness values without cache bleed. |
| Trust | Author claims cannot satisfy required-extension trust or provenance checks. |
| Fixed schemas | `Catalog` and `MetaSchema` shapes are unchanged. |
| Handler parity | Exact before/after key set; portable Backend/strategy opt-in; filter-domain and nested-filter external cases; false claims never bypass exact compiler/resource checks; no family-prefix/literal rule remains. |
| Aggregate parity | Group-by eligibility plus count/order/numeric reducer-source acceptance matches current behavior; custom reducer registration uses raw claims interpreted by the aggregate consumer. |
| Backend parity | Static matrix, live readiness, construction-derived File representation, strategy adapters, scalar-index profiles. |
| Dialect parity | All five physical database tag claims match current behavior. |
| Placement parity | Every built-in profile preserves predicate order, preferred default, type/instance tie-breaking, no-match fallback, and strategy behavior; false/malformed claims skip/fallback or fail only an explicit binding; stale workspace overrides fail on use. |
| External parity | A third-party module may define and claim a tag, claim an undefined tag, use an unqualified ID, or make a false claim through the same package protocol without system-wide failure. |
| Single truth | Deliberately conflicting temporary class/record values fail parity, then all class fallback paths are deleted. |
| API/docs | Public stubs, lazy exports, minimal direct Tag API, unchanged exact CapabilityIndex, structural-only MetaSchema labels, optional MCP delegation, README generation, and logical built-in catalog fixtures agree. |
| Version | `src/heavenbase/version.py` and every packaged module descriptor remain synchronized at `0.1.2.1`. |

Milestone checks:

1. after slices 1–3: focused module/Registry/docs suites;
2. after slices 4–6: focused placement, Backend capability, handler, and
   extension suites;
3. before cleanup/commit: default fast wrapper suite, flake, environment/docs
   drift checks;
4. before merge/release: full opt-in suite only once, plus relevant external
   provider tests.

Always invoke tests through `bash scripts/test.bash`, never `pytest` directly.

## Failure Semantics

| Failure | Required behavior |
| --- | --- |
| Unknown claimed tag | Preserve the raw claim as `unresolved`; optional lint or the named consumer may warn. |
| Claim on an unexpected subject | Preserve the raw claim; typed inspection reports a local diagnostic and the named consumer chooses ignore/skip/fail for that subject. |
| Invalid claim value or malformed definition/default | Preserve the record; typed inspection reports `invalid` with a value path. A built-in CI fixture fails during development, not Context startup. |
| Definition addition/removal/narrowing | Recompute derived status/defaults without rewriting or blocking claims. |
| Missing tag-carried module/value reference | Report unresolved at use. Hard lifecycle requirements must use structural dependencies instead. |
| Invalid scalar-index subset or duplicate live adapter key | The owning Backend consumer rejects that claim/overlay locally; static Registry truth remains unchanged. |
| Invalid module storage hint | Skip/fallback or fail only the affected explicitly authored binding, with profile/subject/tag diagnostics. |
| Stale workspace storage predicate | Reject profile materialization/use explicitly against the current snapshot; reopen is safe. |
| Conflicting `kind: tag` record identity/alias | Use ordinary Registry ownership/collision failure; never last-writer-wins. |
| One subject supplies multiple raw keys resolving to one definition | Mark that subject/tag `ambiguous`; preserve the raw record and let only the named consumer skip/fail. |
| Optional tag-hint index build failure | Keep the Registry snapshot and prior usable derived view; disable or report the affected hint consumer. |
| Malformed construction-critical bootstrap | Fail that Context before its control Backend is exposed. Optional tag-definition disagreement is not a bootstrap failure. |
| Tag-derived placement/seeding input uses an old snapshot | Atomically refresh from one new snapshot when possible; otherwise report stale/reopen-required before using that hint. |
| Live overlay invalid value | Reject or mark only the overlay invalid and surface its Backend/tag source; never mutate static truth. |
| Live provider unavailable | Return explicit availability and let execution fail/decline honestly; do not rewrite implementation tags. |
| Author claims reserved trust metadata | Reject installation before publication. |

## Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Tags become a second planner | Keep exact handler keys, named consumers, and compiler/resource proof; defer general selectors. |
| Manifest and code drift during migration | Temporary parity assertions with a mandatory deletion milestone. |
| Open vocabulary collision or typo | Recommend publisher prefixes, preserve exact strings, expose unresolved/ambiguous diagnostics, and let ordinary Registry identity reject definition-record collisions. |
| Applications accidentally treat optional definitions as hard dependencies | Keep claims raw, forbid tag-owned `meta.dependencies`, and test definition removal with process continuity. |
| Executable validation leaks into install | Declarative definition parsing and application-owned consumers only; Python/Capsule facades never become global publication validators. |
| Live data pollutes durable records | Effective overlays are object-local and provenance-bearing; never auto-persist. |
| Trust becomes forgeable | Keep installer `tag:builtin` and `provenance:system` reserved, reject author claims, and exclude them from the semantic Tag API. |
| Typed sets/maps hide merge semantics | Whole-value replacement and canonical serialization. |
| Broad or false categories over-promise | Label them unverified declarations, keep exact compiler/resource proof, and isolate skip/fallback/failure to the affected operation. |
| Too many weak tags | Each built-in tag has a documented subject/use and source-owned fixture; external vocabulary remains unrestricted. |
| Permanent compatibility surface | No dual-read fallback after parity; remove old APIs in the same convergence train. |
| Excessive test cost | Owner-focused suites per slice; broad suite only at milestones. |

## Rollback and Recovery

Each slice should be independently reviewable:

- slices 1–3 are additive and may be reverted before reader cutover;
- slices 4–6 switch one named consumer group at a time behind parity evidence;
- a failed Registry mutation always restores the prior published snapshot;
- live overlays are reconstructed, so reopen is the safe recovery path;
- once a legacy authority is removed, rollback should revert the whole owning
  slice rather than reintroduce a permanent runtime fallback.

Do not create data migrations in user workspaces merely to cache optional tag
hint indexes. Installed module records are the durable claim source; indexes
are rebuilt.

## Independent Engineer Handoff Checklist

Before coding:

- read this whole document;
- verify the current 32-descriptor/242-record inventory and the accepted target
  definition-owner map against the branch;
- confirm Decisions 1–15 are Accepted and Decision 16 is Not applicable;
- confirm the design was promoted and one queue task exists;
- do not touch deferred LLM persistence or missing Backends;
- preserve unrelated worktree changes;
- keep version `0.1.2.1`.

While coding:

- use the existing module protocol and Registry;
- keep manifest discovery/import validation inert;
- preserve raw claims and keep typed validation application-local;
- keep tag references out of structural dependency closure;
- preserve Context-local authority;
- keep structural relations and trust separate;
- prefer focused test-wrapper runs;
- delete parity scaffolding at its named exit condition.

Before handoff:

- prove every built-in claim matrix;
- prove exact handler and placement parity;
- prove third-party defined, undefined, malformed, unqualified, and false
  claim isolation;
- verify no semantic `family`, `kind: capability`, class static capability, or
  duplicate extension-tag authority remains;
- verify no fixed system schema changed;
- update durable docs, stubs, generated README copies, DEVLOG, and queue state;
- run the milestone checks.

## Major Decision Guide

All six architecture groups are resolved. This section is a plain-language
summary; the Acceptance Record below owns the numbered status.

| Group | Accepted outcome | Practical consequence |
| --- | --- | --- |
| 1. Cold start | Use the existing minimal bootstrap resource through a construction-only adapter. | Bootstrap and Registry may share protocol and data definitions, but never an instance, state, cache, or normal configuration role. |
| 2. Publication and ownership | Store claims in `meta.tags`; use soft, open-world references; distribute built-in definitions to their semantic module owners. | Missing definitions never block module installation or Context startup, and tag references never enter structural dependency closure. |
| 3. Third-party vocabulary | Use the declarative grammar when a definition resolves, represent open values as strings, recommend rather than require publisher prefixes, and keep categories open to future subject kinds. | Unknown raw claims remain valid data; a published `kind: tag` definition uses ordinary Registry identity and collision rules. |
| 4. Categories versus behavior | Keep the logical 48-tag catalog; treat tags as unverified declarations and routing hints. | A claim may nominate a candidate, while exact HandlerKey, compiler, resources, and execution remain the proof that an operation works. |
| 5. Runtime and inspection | Provide minimal direct raw/resolved inspection with live overlays and per-claim status. | Do not add tag-on-tag claims, semantic MetaSchema copies, or derived tag views to `CapabilityIndex`. |
| 6. Selector scope | Defer the general selector language. | Remove legacy `family=` filtering without adding a selector compatibility shim. |

The authority boundary is deliberately asymmetric:

- Registry publication remains strict about record shape, identity, lifecycle,
  ownership, and structural dependencies.
- `meta.tags` remains open data. The system stores it without semantic
  admission gates.
- Optional `kind: tag` records add shared schema, documentation, and defaults
  when available; they are not prerequisites for a claim.
- Each named consumer owns the validation and failure policy for the values it
  interprets. It may warn, skip, fall back, or fail the affected record or
  operation, but it must not invalidate the whole workspace merely because a
  tag is missing, undefined, malformed, or false.

Cold start is a construction-phase exception, not a second static tag
authority. The minimal bootstrap resource contains only the facts needed to
construct and open the control Registry. After that handoff, normal tag and
module reads come only from Registry records. Bootstrap data must never be
exposed as workspace configuration or mixed into normal Tag API results.

No major architecture choice remains in this proposal. Exact SDK symbol names,
cache structures, warning types, and consumer-specific fallback details are
delegable implementation decisions as long as they preserve the accepted
boundaries above.

## Acceptance Record

| ID | Date | Decision | Status |
| --- | --- | --- | --- |
| Foundation | 2026-07-24 | Use one unified open-world tag model; optional definitions may type values, and “tag” includes non-Boolean and non-behavioral properties. | Accepted |
| Invariants | 2026-07-24 | Preserve one Registry, exact handler keys, compiler/resource execution proof, fixed system schemas, version `0.1.2.1`, and structural installer trust/provenance outside semantic tags. | Repository invariant |
| D1 | 2026-07-24 | Published subject record is the sole static claim authority; class declarations are pre-publication input only, with no runtime fallback. | Accepted |
| D2 | 2026-07-24 | Use the existing minimal bootstrap resource through a construction-only adapter; share protocol and definitions with Registry resolution, never an instance, state, cache, or normal configuration role. | Accepted |
| D3 | 2026-07-24 | Store raw claims in `meta.tags`; tag references are soft and never participate in structural dependency closure or whole-workspace admission. | Accepted |
| D4 | 2026-07-24 | Built-in definitions are distributed to semantic module owners; consistency is source-owned and CI-checked rather than enforced by a central runtime validator. | Accepted |
| D5 | 2026-07-24 | Use the strict inert declarative grammar to interpret a claim when its optional definition resolves; raw claims are not publication-gated by that grammar. | Accepted |
| D6 | 2026-07-24 | Open-domain values are strings; do not add `tag_value` records or a separate value lifecycle. | Accepted |
| D7 | 2026-07-24 | Built-ins use concise lower-kebab identifiers; publisher prefixes are recommended for external tags but unqualified identifiers are allowed, with ordinary Registry collision handling for definitions. | Accepted |
| D8 | 2026-07-24 | Subject and category declarations are advisory and category-open; application consumers decide whether a claim applies to a subject kind. | Accepted |
| D9 | 2026-07-24 | Preserve raw static claims, optional resolved/default views, construction/live overlays, provenance, and per-claim `resolved`/`unresolved`/`invalid`/`ambiguous` status; reopening is the safe refresh boundary. | Accepted |
| D10 | 2026-07-24 | Ordered placement may consume self-declared Backend hints; workspace overrides validate when materialized or used, with local warning, fallback, or failure rather than global reverse-dependency enforcement. | Accepted |
| D11 | 2026-07-24 | Tags are unverified declarations and routing hints: they may nominate candidates but never replace exact handler, compiler, resource, or execution proof; retain the 48-tag model and do not add `storage-strategies` or `vector-filter`. | Accepted |
| D12 | 2026-07-24 | The 48 built-in definitions and claim matrices form a logical, distributed catalog maintained by source-owned lint and tests, not a mandatory completeness gate for third-party modules. | Accepted |
| D13 | 2026-07-24 | Do not add tag-on-tag claims or derived Handler/CapabilityIndex category views. | Accepted |
| D14 | 2026-07-24 | Expose minimal direct raw/resolved Tag inspection; keep `CapabilityIndex` exact, MetaSchema labels structural, and MCP delegation direct rather than creating semantic projections; remove `family=` without a shim. | Accepted |
| D15 | 2026-07-24 | Defer a general tag selector language. | Accepted |
| D16 | 2026-07-24 | Selector grammar and semantics are outside this refactor because selectors are deferred. | Not applicable |
