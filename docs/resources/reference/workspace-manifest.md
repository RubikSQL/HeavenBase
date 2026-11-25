# Workspace Manifests

Workspace manifests are complete, reconstructive workspace-shell definitions.
They capture Backend construction, requested optional Extension roots, and user Entity
schemas with storage placements. They do not export stored rows or physical
Backend data.

## Python

```python
import heavenbase as hb

context = hb.Context.load(home_path="bootstrap.yaml")
ws = hb.HeavenBase("shop", preset="debug", context=context)
manifest = ws.to_manifest()

restored = hb.HeavenBase.from_manifest(manifest, context=context)
detached = hb.HeavenBase.from_manifest(
    manifest,
    context=context,
    detached=True,
)
```

Durable replay follows the same identity contract as `HeavenBase(...)`: it
creates an absent workspace or opens a compatible registered one without
activating it. A conflicting durable construction raises `FileExistsError`.
Entity definitions are replayed through the schema lifecycle and are not
construction-compatibility inputs.

`WorkspaceManifest` deep-copies nested construction values and every
`to_dict()` result, so callers can safely reuse or edit their source and
exported mappings without mutating the manifest.

Use `detached=True` when a test or hidden runtime needs a caller-owned facade
outside durable registration and Context live lookup. Detached does not imply
temporary or isolated Backend storage, cannot be activated, and performs no
automatic cleanup.

## Version 2 Construction

Version 2 has one top-level `construction` envelope. It is the sole authority
for Backend reconstruction; emitted manifests have no parallel top-level
`config` field.

| Kind | Envelope | Replay |
| --- | --- | --- |
| Ambient | `{"kind": "ambient"}` | Resolve the ordinary workspace defaults. |
| Preset | `{"kind": "preset", "preset": "debug"}` | Resolve the named workspace preset. |
| Configured Backends | `{"kind": "backends", "config": {...}}` | Rebuild the complete named Backend configuration. |
| Live Backends | `{"kind": "runtime", "identifiers": {"main": "inmem"}}` | Reconnect matching live instances; never synthesize them from identifiers. |

For configured Backends, the nested `config` mapping is deliberately isolated
from extensions and Entity schemas. It is the complete workspace Backend map,
not a patch onto scoped defaults. For example:

```yaml
kind: heavenbase.workspace.manifest
version: 2
id: shop
construction:
  kind: backends
  config:
    main:
      type: sqlite
      database: shop.db
extensions:
  - agent
  - memory
entities:
  - entity_id: product
    fields:
      object_id: {type: identifier, pk: true}
      name: {type: short-text}
```

`backend_summary` remains optional inspection metadata. It never selects,
configures, or reconnects a Backend.

Each `entities` entry is a JSON entity definition. Exported definitions
include both registered options, including the defaults `catalog: full` and
`gram: true`. When a definition omits an option, it inherits the stored
registration's value or uses the default for a new entity. See
[JSON entity definitions](entity-json.md#entity-object).
When the store already has a MetaSchema entity row for the entity (written
when it was registered there), that row records the registration its rows were
written under, and a replayed definition whose `catalog` or `gram` differs from
it needs `allow_replace=True`.

### Replacing Backend Construction

For ambient, preset, and configured-Backend manifests, replay may replace the
complete construction with a `preset=` or `backends=` argument:

```python
local = hb.HeavenBase.from_manifest(
    manifest,
    preset="debug",
    context=context,
)

production = hb.HeavenBase.from_manifest(
    manifest,
    backends={
        "main": {
            "type": "postgres",
            "database": "shop",
        }
    },
    context=context,
)
```

Replacement is whole-envelope substitution, never a recursive merge. The
manifest still owns the workspace id, extensions, and Entity schemas.
`WorkspaceManifest.open(...)` accepts the same `preset=` or `backends=`
replacement. For a durable replay, the resulting construction must still be
compatible with any already-registered workspace of the same id; use detached
replay or a different id when intentionally staging a different shell.

This separation lets an exported schema move between environments without
editing Entity definitions or extension declarations. Treat an explicit
Backend config as sensitive if it contains credentials: the manifest preserves
those supplied values. Prefer environment-owned replacement config when secrets
must not travel with the manifest.

### Reconnecting Live Backends

A workspace built from live Backend objects emits only the complete mapping
from workspace Backend name to canonical Backend identifier:

```yaml
construction:
  kind: runtime
  identifiers:
    main: inmem
```

Without explicit Backend input, replay searches only the owning `Context` for
live instances whose complete name-to-identifier mapping matches. Connection
details and other instance state are deliberately ignored for this match.
Missing or mismatched instances raise `RuntimeError`.

An identifier proves Backend kind; it is not connection configuration.
Manifest replay never calls a registered Backend factory merely because the
identifier is installed, and never fabricates an endpoint, credential, file
path, or client. Supply `backends=` or `preset=` when reconstructing in another
process or environment. For a runtime manifest these arguments provide the
missing connection configuration or live objects; they do not replace its
identity contract. Their resolved complete name-to-identifier mapping must
match the recorded `identifiers`, while endpoints, credentials, paths, and
other config details may differ. A missing name, extra name, or different
identifier raises instead of becoming a different reconstruction.

## Saved Replay Manifest

Importing a manifest into a tracked workspace saves it in the workspace's
`WorkspaceSpec`, and reopening the workspace by id (`HeavenBase(id)`,
`HeavenBase.load(id)`, `WorkspaceSpec.open_detached()`) replays that saved
copy. When the tracked workspace later changes an entity the saved copy
defines (by `entity_id`, or by the `name` it is derived from), through
`register` (including `allow_replace=True`), `alter_entity` or `describe`, the
saved definition is replaced with the live one, as `to_manifest()` would export
it. A reopened workspace therefore registers the added fields, the changed
`catalog`/`gram` options and the new descriptions, and does not write `NULL`
over values stored in an added column. The workspace registry is re-read
before the write, and the write is repeated when another process or Context
saved the workspace in between, so their changes to other entities are kept.
Entities registered after the import are not added to the saved copy, so
reopening by id does not replay them. Definitions of entities an enabled
extension registers are left out of the saved copy, because extension
activation registers them again on reopen (see [Extensions](#extensions)).

The saved definition is left as it was, and a `UserWarning` names the entity,
in three cases. A change made through a detached or untracked facade of the
same store is not saved. This includes the first registration of an entity on
a detached facade that did not replay the saved copy, such as
`HeavenBase(id, detached=True, backends=...)`, when that registration differs
from the saved definition. A facade counts as one of the same store when it is
built from the same construction as the saved workspace: the same backend
configuration, written the same way (a backend config that names the same
database but leaves out a key the saved one states is another construction,
and its changes are not compared). A first registration is compared with the
workspace registry as this Context last read it, so a saved definition that
another process or Context published after that is not seen and the
registration emits no warning; it is not saved either. A change made through a facade whose definition of
the entity is out of date is not saved: when another process or Context saved
a different definition of that entity after this facade read it, that
definition is kept. `HeavenBase.load(id)` in the same Context returns the same
out-of-date facade, so release it first; reopening with `HeavenBase.load(id)`
then replays the saved definition, and making the change again saves both. A
live definition that a reopen could not replay is not saved: one holding a
value that is not JSON (such as a `decimal.Decimal` or tuple default) or a
compute hook that is not registered as a module (such as a function defined in
a script). `to_manifest()` and the saved copy write a `datetime.date` or
`datetime.datetime` default or example as its `isoformat()` text, with
`datetime.date` or `datetime.datetime` under `default_type` or
`example_types`; import, reopening and `Entity.from_schema` restore the value.
Any other text beside such a type is kept as text. A tuple example is saved as
a list. The saved entry receives the current `catalog` and `gram` options
when its replay would otherwise disagree with the store. In these cases
reopening by id replays the saved definition without the change, so a field it
added is not read and the next write of a row stores `NULL` in its column. When
a detached facade or an out-of-date one changed `catalog` or `gram` and the
saved definition states other values, reopening by id raises `ValueError` until
`from_manifest(..., allow_replace=True)` replays a definition with the new
options; the warning says so. Make the change through the tracked workspace
(`HeavenBase.load(id)`) with a replayable definition to save it.

No warning is emitted when the saved definition already replays to the live
one: a detached registration that matches it, or the same change saved first
by another process or Context (two workers running one startup migration). A
saved definition that names a compute hook through the `computes=` or
`query_computes=` mapping given to this facade is compared through that
mapping. Reopening by id replays the saved definitions. A tracked
`from_manifest(..., allow_replace=True)` call against the saved construction
saves each already-defined entity's supplied definition when its option
change commits. If a later replay step fails, the committed option changes
are already available when reopening by id.

## Version 1 Compatibility

Legacy version-1 manifests may still be read. Their top-level constructor
`config` is validated and normalized into the version-2 `construction`
envelope at the input boundary. New objects and every subsequent `to_dict()` or
`save()` emit version 2 only; `config` is not retained as a second authority.

Version 1 could not distinguish true ambient construction from an export made
from live Backend objects because both used `config: {}`. That legacy shape is
read as ambient. Supply explicit `backends=` when replaying a pre-version-2
runtime export; the resulting version-2 manifest will retain the recovered
runtime identifier signature.

## Files

Use `WorkspaceManifest` to save or load a manifest file:

```python
import heavenbase as hb

hb.WorkspaceManifest.from_dict(ws.to_manifest()).save("workspace.yaml")

manifest = hb.WorkspaceManifest.load("workspace.yaml")
restored = hb.HeavenBase.from_manifest(manifest, context=context)
detached = hb.HeavenBase.from_manifest(
    manifest,
    backends={"main": {"type": "inmem"}},
    context=context,
    detached=True,
)
```

YAML is used for non-`.json` paths. JSON paths use JSON.

## CLI

```bash
hb ws manifest shop > workspace.yaml
hb ws import workspace.yaml
hb ws activate shop
```

`hb ws import` replays the manifest in the CLI's runtime Context, registers the
workspace in that Context's workspace directory, and leaves active selection
unchanged. `--allow-replace` lets a manifest entity replace an existing
schema with the same identifier, such as one stored with other `catalog` or
`gram` options, as `allow_replace=True` does in Python. Creation and import
have no `--active` option; activation is always
a separate `hb ws activate <id>` command. In Python, call `ws.activate()` after
opening when selection should change, or use `context.activate_workspace(id)`
when a name-only operation is required. Pass `context=` to `WorkspaceSpec`
operations or manifest replay; omission selects `hb.DEFAULT_CONTEXT`.

`WorkspaceSpec` stores one canonical construction contract and an optional
replay manifest. The manifest's `construction` is a serialized projection of
that same contract, not an independent config value. Registry updates preserve
imported extensions and entities unless the caller explicitly clears the
stored replay manifest.

`HeavenBase.load(id, context=...)` follows that Context's process lookup and may
reuse an already-live same-id workspace in the same Context. Two Contexts may
own distinct live workspaces with the same id. Loading requires the registered
spec and never creates a missing identity. For an ordinary live workspace,
`ws.release()` removes only the owning Context's live lookup and keeps Backend
data; `ws.drop()` is the destructive physical-data operation. Detached cleanup
is likewise explicit and is never implied by object lifetime. Neither
construction mode changes the active workspace or the separately configured
default.

## Extensions

List requested optional Extension ids under `extensions` to replay enablement on import.
Extension-owned entities such as `memory-note`, `db-*`, and `agent-session` are
reconstructed when each extension loads, so a default manifest
(`include_system=False`) leaves their definitions out. This includes the
entities an extension's setup hook returns, which 0.1.2.2 exported. The
extension is the only source of these definitions: `alter_entity`, `describe`,
and a `register` call (with or without `allow_replace=True`) that would change
an enabled extension's entity structure, `catalog` or `gram` option, or
metadata (name, descriptions, tags and examples) raise `ValueError`. A call that changes nothing is accepted and
leaves the extension's class registered, including the definition `include_system=True` exports for an entity
with derive or compute hooks. Keep extra fields in a separate entity.

On import and on reopening by id, a manifest definition of such an entity (as
`include_system=True` exports it) with the extension's structure and options is
skipped; its name, descriptions, tags and examples are not applied, because the
extension supplies them, and a `UserWarning` names those the definition states
differently. Compute and derive references into extension code are
compared without the package name an extension's code is loaded under, which
differs per HOME and Context root, so a manifest exported on another machine or
by 0.1.2.2 is compared by the module path and name inside the extension. A
definition with another structure, `catalog` or `gram` raises `ValueError`
before the import registers any of the manifest's definitions, and the import
writes no `WorkspaceSpec`. Generated manifests retain the workspace's requested roots,
including roots originally selected from `heavenbase.extensions.default`.
Required Capsule and other transitive activation dependencies are recomputed
and are not duplicated as independently writable roots. Target-environment
default changes therefore cannot silently deactivate a reconstructed workspace.

Re-ingest external databases after import when their live catalog metadata is
required (`ws.database.ingest(...)` after loading the `database` extension).

## Scope

Included:

- Workspace id.
- One complete Backend construction envelope.
- Requested optional Extension roots such as `toolkit`, `prompt`, `agent`,
  `memory`, and `database`.
- User Entity definitions, including field types, defaults, descriptions,
  tags, explicit storage placements, strategy-owned options such as
  `GraphEdge.on_delete`, placement parameters such as its endpoint column, and
  serializable compute metadata.
- Backend summaries for inspection.

Not included:

- Stored Entity rows.
- Catalog object rows beyond what the reconstructed workspace can republish
  from schemas.
- Backend files, SQL databases, vector indexes, search indexes, or live
  clients.
- Connection details for runtime-only Backend objects.

If an Entity manifest references compute or query-compute hooks, pass the
matching function registries and owning `context=` to
`HeavenBase.from_manifest(...)` or `WorkspaceManifest.open(...)`.
