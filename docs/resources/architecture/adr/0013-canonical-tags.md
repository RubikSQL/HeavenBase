# ADR 0013 - Canonical Open-World Tags

Status: accepted and implemented in `0.1.2.1`

Date: 2026-07-24

Detailed contract:
[Canonical Tags](../canonical-tags.md)

Builds on:
[ADR 0011](0011-standalone-registry-and-lego-resolution.md) and
[ADR 0012](0012-unified-module-folder-protocol.md)

## Context

At the time of this decision, HeavenBase described related semantic facts
through Registry `groups.family`, extension tag lists, typed Backend capability
records, Python class capability declarations, physical-database capability
fields, and handler seed conventions. Those sources could not provide one
complete, import-free, Context-bound view, and several extension relationships
still required host-code knowledge.

The Registry and module-folder protocol were already the sole installed module
authority. The decision was how semantic declarations should participate in
that architecture without becoming a second planner, a closed global schema,
or an executable publication hook.

## Decision

HeavenBase uses one open-world tag model:

- A subject's published `ModuleRecord.meta.tags` mapping is the sole static
  claim authority.
- An optional ordinary `kind: tag` record may define a declarative value
  schema, default, intended subjects, layers, and documentation.
- Claims remain valid raw JSON-compatible data when a definition is absent,
  invalid, retired, or replaced. Typed inspection reports record-local
  `resolved`, `unresolved`, `invalid`, or `ambiguous` status.
- Tag definitions and tag-carried references are soft. They do not enter
  structural dependency closure or gate Registry publication, Context startup,
  or unrelated records.
- Definitions are distributed among the semantic domains that own their
  meaning. The accepted built-in convention contained 48 definitions; 0.1.2.3
  adds the `database-dynamic-typing` physical-database tag and the
  `atomic-upsert` Backend behavior tag, for 50. Source-owned lint and
  contract tests enforce the convention rather than a kernel completeness gate.
- Construction and live Backend values overlay static claims only for an
  explicit concrete instance. They remain Context- and instance-local and are
  never persisted back into the subject record.
- Named application consumers may use tag claims to nominate or order
  candidates. Exact `HandlerKey` rows, compilers, dialect relations, concrete
  resources, and readiness checks remain execution proof.
- General tag selectors, tag inheritance, tag-on-tag claims, derived
  CapabilityIndex categories, and semantic copies in flat MetaSchema labels
  are excluded.

The existing construction-only bootstrap remains the intentional cold-start
exception. Bootstrap and Registry-backed readers may share narrow data
contracts, but never an instance, cache, mutable state, or normal
configuration authority.

Installer-owned `provenance:system` and `tag:builtin` groups remain structural
trust metadata and are excluded from the semantic Tag API. `Catalog` and
`MetaSchema` retain their fixed schemas.

## Implementation Evidence

- At acceptance the packaged catalog contained 35 descriptors, 277 ordinary
  records, 48 distributed `kind: tag` definitions, and 205 subjects with
  explicit claims. The canonical-tags document keeps the current counts.
- Packaged descriptors contain no `kind: capability` rows or semantic
  `groups.family` authors.
- The exact generic handler contribution remains 10,368 keys, and the complete
  provider matrix remains 10,489 keys with its accepted fingerprint.
- [Built-in Tag reference](../../reference/tags.md) is generated from the live
  installed definitions; the capability report remains generated from exact
  handlers and compiler probes.
- Static claims, typed defaults and diagnostics, construction/live overlays,
  and generic-resolver snapshot retry behavior have focused regression tests.

## Consequences

- Built-in and external modules publish and inspect semantic declarations
  through the same inert record protocol.
- Unknown or malformed claims cannot take down a Registry or workspace; each
  consumer owns its local warning, skip, fallback, decline, or affected-record
  failure policy.
- Static inspection becomes complete without importing provider code.
- Backend placement, aggregate compatibility, dialect behavior, and handler
  seeding migrate to named tag consumers while preserving order, fallback, and
  exact dispatch.
- Class `families`, Backend `capability_spec`, `kind: capability`, duplicated
  extension tags, semantic `family` readers, and `family=` public filters are
  removed after parity. No permanent compatibility shim remains.
- The logical catalog is extensible and advisory. A publisher may make a false
  claim, but that claim cannot manufacture an executable route.

## Rejected Alternatives

### Keep class and Registry claims co-authoritative

This preserves incomplete import-free inspection, permanent precedence rules,
and different static contracts for inert external modules and bundled Python
classes.

### Require every claim to resolve through a definition

This turns an open extension vocabulary into a globally coupled schema graph
and makes unrelated module lifecycle depend on optional semantic records.

### Use tags as route proof

This would duplicate the exact HandlerRegistry/compiler boundary and let
publisher declarations bypass concrete provider and resource evidence.

### Add a general selector language now

Negative and live-value semantics, priority, conflicts, and snapshot behavior
need a separate design. Named bounded consumers cover the accepted migration
without creating another planner.
