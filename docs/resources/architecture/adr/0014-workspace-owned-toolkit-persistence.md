# ADR 0014 - Workspace-Owned Toolkit Persistence

Status: implemented for `0.1.2.1`

Date: 2026-07-25

## Context

Toolkit persistence originally constructed a Context-private HeavenBase store
for both `sys-toolkit` and `sys-capsule` rows. Agent, Skill, Prompt, and
Extension activation were workspace-owned, but the concrete Toolkits referenced
by those rows lived under a second invisible authority. Applications could not
inspect, isolate, back up, or remove their executable rows through the
workspace that used them.

The Registry service boundaries were already sound: `ToolkitRegistry` owned
Toolkit identity and manifests, `CapsuleRegistry` owned executable identity and
verification, and both consumed narrow Entity-row ports. The problem was the
composition root, not those domain contracts.

## Decision

One explicit `HeavenBase` workspace owns a concrete Toolkit and every Capsule
row referenced by that Toolkit:

```text
HeavenBase workspace
  +-- WorkspaceEntityRowStore(sys-capsule) -> CapsuleRegistry
  +-- WorkspaceEntityRowStore(sys-toolkit) -> ToolkitRegistry
                                              +-- same CapsuleRegistry
```

The public persistence API is workspace-first:

```python
toolkit_id = toolkit.register(ws=ws)
toolkit_row = ws.get(toolkit_id, entity=hb.Toolkit)
loaded = hb.Toolkit.load(toolkit_id, ws=ws)
```

`Toolkit.list`, `Toolkit.describe`, and `Toolkit.delete` follow the same
contract. Omitting `ws` resolves `HeavenBase.load()` and therefore requires an
existing active or configured-default workspace; persistence never creates a
workspace implicitly. The selected workspace enables the optional `toolkit`
Extension and its required `capsule` dependency.

The Context module Registry continues to own Toolkit Entity definitions,
families, profiles, serializers, and verified implementation artifacts.
Concrete Toolkits remain application rows and never become module records.

Standalone `Capsule.register` and direct `CapsuleRegistry` construction remain
advanced executable/module administration surfaces. They may use a
Context-private store, including for installed module executable references,
but Toolkit persistence does not use that path.

## Write and lifecycle policy

Toolkit registration validates executable persistence capability, writes
referenced Capsules, and writes the Toolkit manifest last. A failed write may
leave an unreferenced but independently valid Capsule; it must not publish an
active Toolkit row with a missing reference. HeavenBase does not claim a
distributed transaction across independently routed Backends.

Toolkit deletion does not cascade to Capsules because multiple Toolkits may
share the same content-addressed executable. Destructive garbage collection is
a separate future maintenance policy.

This is a pre-release cutover. Existing private-store Toolkit data is
disposable and must be re-registered into its owning workspace. There is no
dual-read fallback, migration shim, or ambiguous shadowing rule.

## Consequences

- Agent, Skill, CLI, and MCP Toolkit resolution follows the workspace selected
  by the caller.
- Two workspaces in one Context may use the same Toolkit identity without
  sharing concrete rows.
- `ws.get(...)` exposes the durable Entity row while `Toolkit.load(...)`
  restores the executable runtime from the same authority.
- Extension activation, application rows, storage routing, inspection, and
  operational lifecycle now agree on one workspace boundary.
- Custom persistence remains possible through the narrow row-store and
  Registry classes without expanding the root public API.

## Rejected alternatives

### Keep the private executable store as the Toolkit default

This preserves the split authority and makes workspace isolation and
operational ownership misleading.

### Store concrete Toolkits in the module Registry

Installed implementation metadata and application-owned executable instances
have different lifecycle, mutability, and policy.

### Embed every Capsule manifest in each Toolkit row

That duplicates shared executable content and discards independent Capsule
identity and verification.

### Add a workspace-to-private-store fallback

A fallback creates two authorities and permanent precedence rules. The
pre-release boundary permits an explicit clean cutover.

## Verification anchors

- focused tests prove that Toolkit and referenced Capsule rows share one
  explicit workspace;
- same-Context workspace isolation is covered;
- Agent, CLI, MCP, version selection, serializer restoration, and installed
  module artifact paths resolve through the selected workspace;
- the fast integrity suite and complete opt-in suite are release gates.
