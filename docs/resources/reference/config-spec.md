# Configuration Resolution

HeavenBase configuration follows one fixed pipeline:

```text
Preset -> Normalize -> Validate -> Apply(profile)
```

`ConfigEngine` owns the ordering, source capture, caches, and profile registry. A
domain supplies a small subclass with protected policy hooks. `ConfigSpec` is
the immutable result carried between Validate and Apply.

`ConfigManager`, `ConfigSnapshot`, `ConfigEngine`, `ConfigSpec`, and
`InterpolationPolicy` are available from the canonical `import heavenbase as
hb` facade.

## Standalone Application Settings

A GUI or SDK can use ConfigManager without constructing a HeavenBase workspace:

```python
import heavenbase as hb


gui = hb.ConfigManager(
    package="my-gui",
    scope="my-gui",
    default={"gui": {"theme": "system", "scale": 1.0}},
)

gui.set("gui.theme", "dark")
assert gui.get("gui.theme") == "dark"

snapshot = gui.snapshot()
assert snapshot["gui.scale"] == 1.0
```

Omitting `registry` creates an independent process-only Registry. For durable
settings, inject an application-owned existing namespace:

```python
registry = hb.DEFAULT_CONTEXT.registry("my-gui-config")
gui = hb.ConfigManager(
    package="my-gui",
    scope="my-gui",
    default={"gui": {"theme": "system"}},
    registry=registry,
)

changed = gui.refresh()          # immediate pull; True only for a newer revision
# In async application code: changed = await gui.arefresh()
```

Use `ConfigManager` directly when dotted settings, scopes, history,
interpolation, and immutable config snapshots are enough. Add a ConfigEngine
subclass only when the domain needs the fixed preset/normalize/validate/apply
contract.

## ConfigSpec

A spec contains four recursively frozen mappings:

| Field | Meaning |
|---|---|
| `data` | Minimal canonical, user-facing configuration. |
| `bindings` | Selected executable facts such as a compiler or dialect contract. May contain secrets. |
| `raw` | Original direct input for diagnostics and provenance. |
| `meta` | Non-behavioral resolution metadata. |

Behavioral equality and cache identity use `domain + data + bindings`; `raw`
and `meta` do not affect behavior. `to_dict()` and
`ConfigSpec.from_dict(...)` preserve the executable envelope. Do not log that
envelope: bindings may contain credentials. Secret-bearing domains register a
redacted `print` Apply profile instead.

```python
payload = spec.to_dict()
restored = hb.ConfigSpec.from_dict(payload)
assert restored == spec
```

`spec.get("args.timeout")` reads canonical data only.

## Define A Domain

An Entity schema is optional. When present, it supplies defaults, coercion,
and strict field checks after domain normalization.

```python
import heavenbase as hb


class ServiceConfig(hb.Entity):
    identifier = "service-config"

    preset = hb.field(hb.ShortText).optional()
    name = hb.field(hb.ShortText).required()
    retries = hb.field(hb.Integer).default(2)
    params = hb.field(hb.Json).default({})


class ServiceEngine(hb.ConfigEngine):
    domain = "service"
    schema = ServiceConfig
    extra = "collect"
    extras_field = "params"

    def __init__(self, root=None):
        super().__init__(manager=None, root=root)
        self.register_profile("runtime", self._apply_runtime)

    def _normalize(self, data, *, context):
        return data

    def _validate(self, spec, *, context):
        if spec.get("retries") < 0:
            raise ValueError("service.retries must be non-negative")

    def _default_profile(self, spec):
        return "runtime"

    @staticmethod
    def _apply_runtime(spec):
        return {
            "name": spec.get("name"),
            "retries": spec.get("retries"),
            **dict(spec.get("params")),
        }


engine = ServiceEngine(
    {
        "default_preset": "worker",
        "presets": {"worker": {"name": "jobs", "retries": 4}},
    }
)
spec = engine.resolve({"timeout": 30})
assert spec.data["preset"] == "worker"
assert spec.data["params"]["timeout"] == 30
assert engine.apply(spec) == {
    "name": "jobs",
    "retries": 4,
    "timeout": 30,
}
```

For managed domains, declare `config_path`, such as
`"heavenbase.service"`, and keep the default `manager=CM_HVNB`. A `root`
mapping is a frozen overlay above the captured manager root; it is intended for
bootstrap and tests, not per-call mutation.

## Stage Contract

- Preset expands the complete named template. `default_preset` is used when
  `preset` is omitted; `preset: null` disables it.
- Normalize resolves aliases, shorthands, precedence, and canonical semantic
  values. `_bind` may select immutable executable facts from the same frozen
  source/catalog snapshot.
- Validate is strict and non-mutating. It does not fill defaults or repair
  routes.
- Apply invokes one registered profile and returns values only, never live
  clients, open connections, or resource registries.

Public `normalize`, `validate`, `resolve`, `apply`, and `assert_idempotent`
cannot be overridden by subclasses. Domain policy belongs in `_normalize`,
`_bind`, `_validate`, `_default_profile`, and `_capture_catalog`.

`normalize(config)` is uncached and stops before validation. `resolve(config)`
runs Normalize and Validate and caches the immutable spec. `validate(spec)`
validates a compatible restored spec without rereading its original manager or
catalog. Unknown executable binding contracts fail explicitly.

## Apply Profiles

Register profiles on the engine instance:

```python
engine.register_profile(
    "runtime",
    apply_runtime,
    validate=validate_runtime,
    aliases=("args",),
)
value = engine.apply(config, profile="runtime")
```

The base engine requires an explicit profile. A domain may choose a pure
default from its resolved spec. Profile discovery is available through
`engine.profiles()`; preset discovery is available through
`engine.presets()`.

JSON-like Apply results are cached and returned defensively. Set
`cache_policy=False` for non-cacheable output, or provide a policy with
`fingerprint`, `freeze`, and `thaw` for deterministic domain objects. Profile
callbacks must not return live resources.

## Caches And Invalidation

Each engine owns two bounded, thread-safe LRUs:

- the resolved cache keys raw input with manager, interpolation, catalog, and
  contract revisions;
- the applied cache keys the behavioral spec, profile revision, profile name,
  and Apply arguments.

Bounds come from `heavenbase.config.cache.max_resolved` and
`max_applied`; `0` disables the corresponding cache. Manager refresh, scope or
catalog changes invalidate resolution identity. Profile registration changes
invalidate Apply identity. Live SQL engines, SDK clients, and LLM responses
remain in their downstream caches.

Use `engine.clear_cache()` for explicit local invalidation.

## Idempotence And Interpolation

Normalization must satisfy `N(C) == N(N(C))`. This is a development contract,
not an extra production pass:

```python
engine.assert_idempotent(raw_config)
```

The diagnostic performs exactly two uncached Normalize/Bind passes against one
captured source and reports the first changed path without displaying secret
values.

`InterpolationPolicy` says which placeholders may consult state outside the
config mapping. Environment reads are enabled by default; shell commands are
disabled:

```python
policy = hb.InterpolationPolicy(allow_env=True, allow_cmd=False)

resolved = hb.resolve_interpolations(
    {"endpoint": "${env:MY_GUI_ENDPOINT,http://localhost:8000}"},
    policy=policy,
)
engine = ServiceEngine(root={"endpoint": "${env:MY_GUI_ENDPOINT}"}, policy=policy)
```

`${cmd:COMMAND}` runs a shell command with the application's permissions, so
enable `allow_cmd=True` only for trusted local configuration. The policy and
referenced environment observations participate in cache identity. Direct-input
observations add value hashes, rather than raw environment values, to source identity.

## Public Surface

- `ConfigSpec`: immutable executable configuration envelope.
- `ConfigEngine`: final staged orchestration and profile registry.
- `InterpolationPolicy`: interpolation permissions.
- `resolve_interpolations`: standalone interpolation helper.
- `ConfigManager` and `ConfigSnapshot`: Registry-backed scoped persistence and
  immutable source snapshots; they do not own domain normalization.
