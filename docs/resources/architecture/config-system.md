# Configuration System

Status: implemented for the `0.1.2.1` architecture-convergence train.

## Ownership

HeavenBase separates raw scoped configuration from domain resolution:

```text
bootstrap -> Context -> Backend -> Registry namespaces
                                -> ConfigManager -> ConfigSnapshot
                                                   |
                                                   v
Preset -> Normalize -> Validate -> Apply(profile) -> value
             ConfigEngine + immutable ConfigSpec
```

- `Context` owns the machine Backend and injects its `system-config` Registry
  into `ConfigManager`.
- `ConfigManager` owns defaults, scope chains, retained layers, tombstones,
  interpolation, and source snapshots. Scope records persist through Registry;
  configuration has no separate database schema.
- `ConfigEngine` owns the fixed domain-resolution pipeline, source capture,
  cache identity, and Apply projections. A private process `Registry` owns
  profile identifiers, aliases, collision checks, and immutable publication.
- Domain engines own vocabulary and policy. Generic config never selects an
  LLM provider, SQL dialect, filesystem root, SDK, or live resource.

This split gives Registry one persistence protocol and ConfigEngine one
resolution protocol without combining their responsibilities.

## Workspace-Derived Layers

A workspace has no separately stored configuration scope. Its stable id derives
one layer below the owning manager's base scope: workspace `shop` uses
`heavenbase.shop`, including when `shop` is the configured default. A future
display name does not participate in this identity.

Workspace construction and ordinary workspace operations enter that exact
id-derived layer when resolving workspace-owned configuration. Explicit
constructor arguments and replayed manifest construction data remain
authoritative over scoped defaults. Context control state—including workspace
registration, configured default, active selection, module setup, and the
control Backend—remains in the base layer. Activation therefore changes
name-free workspace selection only; it never changes configuration scope.

Workspace manifests do not store their Backend inputs as another config scope.
Version 2 isolates one complete construction contract at the top level:
replayable Backend settings live under `construction.config`, while runtime
Backend objects contribute only a name-to-canonical-identifier signature.
Explicit `preset=` or `backends=` replay input replaces an ambient, preset, or
replayable-Backend envelope in full rather than merging with it. For a runtime
envelope, explicit input may provide environment-specific connection details
only when its complete Backend name and identifier topology still matches.
Identifiers alone never cause config resolution to invent connection details
or a live client.

Live observation is deliberately best effort. A later operation can see a
scoped edit when it resolves that setting lazily, but an existing Backend,
client, cache, worker thread, or already-resolved spec need not reconfigure
itself. Reopening reconstructs the workspace under its id-derived layer and is
the safe synchronization boundary.

## Extension Default Roots

`heavenbase.extensions.default` is new-workspace composition policy. When a
tracked workspace first opens without explicit or replayed Extension roots, it
snapshots that configured list into `WorkspaceSpec.extension_roots`. A legacy
`None` value snapshots once on its next open; an explicit empty list means no
optional defaults. Later configuration changes affect new or unsnapshotted
workspaces and never deactivate an existing workspace. The bundled roots include
`agent` because the default CLI chat and session flows persist their completed
runs there; explicitly composed workspaces may still omit it.

Reopen recomputes hard activation dependencies from the stored roots. Required
and transitive dependencies are not independently stored as roots, and
MetaSchema is reporting rather than activation authority. See
[Workspace Foundation and Extensions](workspace-foundation-and-extensions.md).

## Fixed Pipeline

Every domain follows the same four stages:

1. **Preset** expands one complete named template. `default_preset` applies when
   the caller omits `preset`; explicit `preset: null` disables it.
2. **Normalize** resolves aliases, shorthands, precedence, and canonical values.
   `_bind` captures selected executable facts from the same frozen source.
3. **Validate** checks the canonical spec without filling defaults or repairing
   routes.
4. **Apply(profile)** projects the validated spec to a deterministic value. It
   never owns a live client, connection, response, or resource registry.

The public `normalize`, `validate`, `resolve`, `apply`, and
`assert_idempotent` methods are final. A domain implements protected hooks:

```python
class ServiceEngine(hb.ConfigEngine):
    domain = "service"
    config_path = "heavenbase.service"

    def _normalize(self, data, *, context):
        return data

    def _bind(self, data, *, context):
        return {"contract": "service-route/v1"}

    def _validate(self, spec, *, context):
        if not spec.get("endpoint"):
            raise ValueError("service.endpoint is required")

    def _default_profile(self, spec):
        return "runtime"
```

Schema-backed domains may declare an Entity schema for defaults, coercion, and
strict fields. Domain Normalize still runs before schema validation so aliases
and shorthands become canonical first.

## ConfigSpec

`ConfigSpec` is one recursively frozen executable envelope:

| Field | Contract |
| --- | --- |
| `domain` | Stable domain identifier. |
| `data` | Minimal canonical user-facing data. |
| `bindings` | Selected executable facts, including captured route contracts. |
| `raw` | Original direct input for diagnostics. |
| `meta` | Non-behavioral resolution metadata. |

Behavioral equality and cache identity use `domain + data + bindings`; `raw`
and `meta` do not change behavior. `to_dict()` and `from_dict()` preserve the
executable envelope, so Validate and Apply can use a restored spec without
rereading mutable manager or catalog state.

Bindings may contain credentials. Logging and CLI output use a domain-owned,
redacted `print` profile rather than `to_dict()`.

## Source Capture

One resolution captures:

- the current immutable `ConfigManager` snapshot;
- the engine's frozen root overlay;
- interpolation policy and referenced environment values;
- only the catalog metadata needed by the raw input;
- domain contract, binding, and validator revisions.

Normalize and Bind see one frozen source. Catalog-backed domains load selected
metadata before capture. For example, resolving SQLite registers SQLite dialect
metadata only; enumerating the complete dialect catalog remains an explicit
operation and may load all providers.

`assert_idempotent(config)` runs exactly two uncached Normalize/Bind passes
against that one source and requires `N(C) == N(N(C))`. It reports only the
first changed path and digests secret-bearing differences.

Apply cache identity captures the selected profile and profile revision
separately, so profile replacement does not invalidate resolved specs.

## Apply Profiles

Profiles are registered values, not branches in the base engine. ConfigEngine
uses the same `Registry` identity and alias contract as other component
namespaces, but keeps profile storage process-only because callbacks are live
Python callables rather than durable data:

```python
engine.register_profile(
    "runtime",
    apply_runtime,
    validate=validate_runtime,
    aliases=("args",),
)
value = engine.apply(config, profile="runtime")
```

JSON-like results use structural defensive caching. A domain can disable Apply
caching or provide `fingerprint`, `freeze`, and `thaw` functions for a
deterministic value type. Live resources stay in their downstream owners:
SQLAlchemy engines in `DatabaseEngineRegistry`, LLM clients in the client
cache, and responses in the response cache.

Replacing a profile atomically replaces its aliases and invalidates only the
Apply cache. It does not invalidate resolved `ConfigSpec` values.

## Interpolation Policy

`InterpolationPolicy` is the safety boundary for configuration placeholders:

| Permission | Syntax | Default | Meaning |
| --- | --- | --- | --- |
| `allow_env` | `${env:NAME}` or `${env:NAME,default}` | `True` | Read one environment variable. |
| `allow_cmd` | `${cmd:COMMAND}` | `False` | Execute a shell command and use its standard output. |

Command interpolation should be enabled only for trusted local configuration;
it executes with the application's process permissions. The policy and hashes
of referenced environment values participate in resolution identity, so an
environment change does not return a stale resolved-cache entry. Direct-input
environment observations add hashes, rather than raw values, to source identity.

## Cache Contract

Each engine owns two bounded thread-safe LRUs:

- resolved specs keyed by raw input plus source/catalog/contract identity;
- Apply values keyed by the behavioral spec plus profile revision and args.

`heavenbase.config.cache.max_resolved` and `max_applied` set bounds; zero
disables a cache. `ConfigManager` separately bounds source snapshots and
generation observations; a snapshot is keyed by a digest of each scope's
generation and latest layer, because a reset scope restarts its generation,
and that digest and the snapshot's content are read from one Registry state.
Scope changes, Registry revisions, referenced
environment changes, root overlays, catalogs, and profile registration all
participate in the appropriate identity.

## Observation Contract

Registry and ConfigManager share a pull-based visibility contract:

- `refresh() -> bool` publishes a newer persistent Registry snapshot when one
  exists and reports whether publication occurred;
- `arefresh() -> bool` performs the same work through worker-thread delegation;
- process-only instances return `False`; ConfigManager still clears its derived
  generation and snapshot caches;
- neither component installs filesystem/database watchers or push callbacks;
- Registry never retries a persistent compare-and-set conflict; a
  ConfigManager write (`set`, `unset`, `setdef`, `init`, `remove`, `compact`,
  `setup`) reads the values it depends on, list items and the `setdef` check
  included, from one revision. A write of a Context's ConfigManager
  (`CM_HVNB`, `Context.config`) holds an OS file lock on
  `.config-write.lock` in that Context's home directory (its bootstrap root,
  `~/.heavenbase` for the default Context) around that read-modify-write, so
  writers of one home run one after another; a lock holder loses its
  compare-and-set at most once, to a write saved before it took the lock, and
  then runs again on the latest revision. A ConfigManager without a Context
  takes no lock, so no file appears in its `root`. A writer waits at most 5
  seconds for the lock, then goes on without it, as when the home does not
  exist, the lock file cannot be created, or the filesystem refuses the lock
  (NFS without lockd, some SMB or FUSE mounts); a child forked during a write
  does not keep the lock. A write without the lock, or one racing writers
  that do not take it (another host sharing the control store), retries with
  jittered backoff and raises the conflict only when they keep winning for
  300 seconds. A write that changes something publishes
  against that revision, so an up-to-date
  write reads the store only to publish. A write that changes nothing (it
  leaves the stored layer unchanged, list writes included, or `setdef` finds
  a value, or `remove` or `compact` finds nothing to remove) saves no version
  and publishes nothing, so it cannot conflict; it reads the store once
  instead and runs again on the newer records when another process has
  written since that revision. A change that fails on its revision, such as
  a list index past the end of the list it read, is handled the same way: it
  runs again on the newer records when the store has moved past that
  revision and is raised only when that revision is the latest. A list write
  (`items[]`, `items[i]`, `items[i+]`, `unset("items[i]")`) edits the stored,
  unresolved list of the chain that reads the written scope, so a write with
  `scope=` never copies the current scope's list into another scope. A write
  that goes through a list or through a node held by an interpolation, such
  as `servers[0].port`, or `y.port` under `y: ${src}`, first saves that whole
  list or node in the written scope, because the written layer's list
  replaces the list a read merges and its mapping replaces the interpolation.
  A plain node interpolation such as `${src}`, `${..src}` or `${服务.db}`
  (any key OmegaConf accepts) is followed to the stored node it names, so an
  item such as `${env:NAME}` is saved as written, never as its value. In the
  saved copy, a relative interpolation that names a node outside the copy is
  rewritten to the absolute path it named, and a string holding a resolver
  other than `env` or `oc.env`, whose value may depend on where it is stored
  (such as `${oc.select:..host}`), becomes a node interpolation naming that
  string in the source node. The saved copy otherwise no longer follows later
  changes to the source node. A write that goes through a mapping or list
  that another kind of interpolation yields, such as a resolver, raises
  `ValueError` instead of saving its resolved values, and so does a write
  whose copy cannot name such a path, because a key on it is not one
  OmegaConf accepts in an interpolation. Finding out whether such an
  interpolation yields a mapping or a list resolves only it and the
  interpolations on the way to it. A list write to a value that is not a list raises `TypeError`,
  and a missing or `null` value starts a new list. An `unset` below a list or
  an interpolation whose key is missing saves nothing. `setdef` resolves the config
  it reads before the write starts, from the snapshot cache when a read
  already resolved it, and resolves again under the Registry lock only when
  the draft holds newer records. A write clears the
  derived generation and snapshot caches only when the Registry published,
  by the write itself or by a refresh that found another process's write.

ConfigManager additionally polls generations on reads according to its
`refresh_interval`. Call `refresh()` when a GUI, service, or test needs an
immediate observation boundary. ConfigEngine then captures one immutable
manager snapshot for the complete resolution pipeline.

`ConfigManager.last_write_persisted` reports the destination of the most
recent completed mutation in the current thread or async execution context.
`True` means the configured store applied the mutation or already held the
requested state, `False` means the operation used process-only storage, and
`None` means the execution context has no prior status. A custom
Registry store controls its own durability. Each mutation keeps its existing
return value, and reads leave the persistence status unchanged.

If `CM_HVNB` cannot initially open the saved configuration, it uses a
process-only Registry for reads and writes. Once the store is available,
`refresh()` adopts saved settings if the temporary state has no edits.
With local edits, `refresh(discard_process_changes=True)` deliberately
replaces them with the saved state. Refresh retains the fallback and returns
`False` while only in-memory storage is available. This works through
`arefresh()` too.

## Built-in Domains

### Workspace presets

Workspace preset resolution consumes `heavenbase.workspace.presets`, normalizes
backend declarations, validates stable names and replayable config, and applies
to detached construction data. `WorkspaceSpec` persists the resulting durable
definition through the Context-owned Registry.

### LLM

`LLMEngine` canonicalizes semantic provider, model, endpoint, gateway, cache,
and request args. `LLMSpec` binds a registered gateway compiler and endpoint
contract. Gateway descriptors register themselves through `Registry`; each one
also installs its Apply profile on the engine. Apply returns detached client
arguments, never an SDK client.

The normal live integration preset is `deepseek-v4-flash`, while `mock` remains
the deterministic fast-suite preset.

### Database

`DBEngine` canonicalizes provider/dialect routes, local versus server database
scope, URLs, query parameters, pooling, pragmas, lifecycle superuser config,
and runtime args. `DBSpec` binds the selected `DatabaseDialectSpec`. Apply
profiles return a SQLAlchemy `URL`, serialized URI, engine args, superuser
config, cache key, pragmas, or redacted print value.

Provider-specific facts belong in `DatabaseDialectSpec`. Ordinary SQL backends
consume the active DB engine config; they do not inherit a hidden bootstrap
override.

## Local File URI Contract

Explicit local storage locations have one grammar:

| Form | Meaning |
| --- | --- |
| `file:///absolute/path` | Canonical absolute local path. |
| `file://%/relative/path` | Path below the configured package home. |
| `file://&/relative/path` | Read-only path below packaged resources. |

UNC/network authorities, credentials, queries, fragments, backslashes, root
escapes, and legacy `file:` spellings are rejected. Writable storage rejects
the resource alias. A DB `database` field may also use a separator-free logical
name; DB Normalize resolves it below `heavenbase.db.local_root` and stores the
resulting canonical `file:///` URI.

`utils/files.py` owns encoding, decoding, and normalization. Domain code turns
canonical URIs into native paths only at Apply or IO boundaries.

`scripts/migrate_config_locations.py` is the one-shot migration boundary for
owned YAML/JSON config. It classifies old locators, proves old and new native
targets are equal, defaults to dry-run, writes atomically, and refuses ambiguous
or network locations. Runtime resolution contains no dual parser.

## Extension Rules

To add a domain:

1. declare one stable `domain` and optional `config_path`;
2. keep raw aliases in Normalize and canonical values in `ConfigSpec.data`;
3. capture only the executable facts required by Apply in `bindings`;
4. validate without mutation;
5. register value-returning Apply profiles;
6. add idempotence, serialization, cache-invalidation, redaction, and source-free
   restored-spec tests.

Do not add another orchestrator, persistence store, path grammar, provider
switch in generic config, or live-resource cache to ConfigEngine.

## Verification Anchors

- `tests/config/test_config_spec.py`, `test_config_resolution.py`, and
  `test_config_manager.py`: generic envelope, pipeline, cache, interpolation,
  scopes, and Registry-backed history.
- `tests/workspace/test_workspace_presets.py`: workspace domain behavior.
- `tests/llm/test_llm.py`: semantic routes, gateway profiles, source-free Apply,
  redaction, and live provider smoke.
- `tests/core/test_database.py`: dialect contracts, URL/URI round trips, strict local
  paths, pool/lifecycle projections, source-free Apply, and live SQL behavior.
- `tests/core/test_file_uri.py` and `tests/config/test_config_location_migration.py`: strict
  grammar and verified migration.
- `tests/core/test_context.py`, `tests/registry/test_registry.py`, and `test_registry_backend.py`:
  ownership, durable scope records, CAS, and import laziness.
