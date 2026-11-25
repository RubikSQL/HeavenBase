# Concepts And Classes

| Class | Owns | Must Not Own |
|-------|------|--------------|
| `Context` | config, control Backend, Registry namespaces, ModuleService, workspace directory/selection/live identity | domain dispatch or a second planner |
| `Registry` | generic immutable snapshots, indexes, optional store CAS, publication/revision | HeavenBase Context, domain, provider, or module policy |
| `ModuleService` / `ModuleCatalog` | validated lifecycle and lazy resolution / read-only inspection over `system-modules` | another catalog or raw consumer mutation |
| `ModuleResolver` | minimum read/resolve protocol inherited by typed domains | module installation authority |
| `HeavenBase` | one Context-bound workspace, live Backends/handlers, storage plans, system rows | logical type semantics or global machine state |
| `Entity` | declarative schema surface | physical IO |
| `FieldSpec` | type, metadata, default, compute hook, storage hint | execution |
| `FieldSchema` | one compiled field in `EntitySchema` | storage, compute hooks |
| `EntitySchema` | normalized entity schema | storage rules, compute hooks, or runtime services |
| `EntityAssembly` | one class declaration's schema, isolated specs, and query refs | workspace registration or row IO |
| `InteropTypeInferer` | sample-based external type inference rule and registry metadata | row import orchestration |
| `Catalog` | concrete object directory rows | entity or storage placement metadata |
| `MetaSchema` | entity, field, backend, storage placement, capability, and extension metadata rows | user data |
| `Prompt` / `Translation` | prompt-extension persistence rows | LLM provider execution |
| `Capsule` | required executable-manifest capture/restore domain over an injected row store | Toolkit persistence or workspace construction |
| `Tool` / `Toolkit` | optional Capsule-backed tool execution, Toolkit persistence, Registry metadata, and MCP exports | Capsule persistence or backend query routing |
| `Backend` | physical row storage and fragment execution | query parsing |
| `StorageStrategy` | immutable storage/index policy identity plus strategy-owned options | query compilation or physical IO |
| `HandlerKey` | normalized logical type, operation, backend, and strategy capability identity | compiler execution |
| `Handler` | one capability key and one pure fragment compiler | IO, placement, or query parsing |
| `StorageRule` | authored field placement intent | backend selection or IO |
| `StorageProfile` | registered default placement policy for one logical type | workspace backend instances |
| `StorageResolver` | rule/profile precedence against declared workspace backends | physical storage execution |
| `StorageBinding` / `StoragePlan` | resolved field routes for one entity | placement policy |
| `StrategyAdapter` | one strategy's physical setup, mutation maintenance, and optional query execution inside a backend | workspace routing or user query parsing |
| `HandlerRegistry` | operation compiler lookup | backend lifecycle |
| `HandlerPlugin` | provider-wide handler and resolver-bound reason seeding | workspace routing or process-global reason state |
| `Extension` | entity-bundle metadata, registration, and workspace enablement | direct storage writes |
| `Query` | immutable fluent, spec-only query value | builder state, plans, fragments, cached results, or persistence |
| `QuerySpec` | frozen normalized query request | execution or result ownership |
| `ResultFrame` | detached table-shaped execution transport | query continuation, application-domain behavior, hydration policy, or pre-merge trace state |
| `CapabilityIndex` | one workspace's Context-local read-only component/support index | registration, placement, or execution |

Naming rules (Spec / Schema / Plan / Fragment): [`naming-vocabulary.md`](../reference/naming-vocabulary.md).

Dependency direction:

```text
interfaces -> workspace/optional extensions -> query/capabilities -> backends
           -> execution -> placement -> schema/entity/types/strategies/filters
           -> modules/context -> utils.registry
```

Backends never parse `QuerySpec`. Strategies never compile queries. Handlers compile
leaf operations into `QueryFragment` objects. Shared contracts, frames, and portable
handlers live under `execution/`; provider-local handlers stay with their Backend.
`hb.capabilities` is a package front door over read-only discovery, while
`workspace.capabilities` is that workspace's Context-local index.

Catalog and MetaSchema are exact workspace-foundation classes and cannot be
owned, replaced, or altered by an Extension. The installer-trusted `capsule`
Extension is required and owns `sys-capsule`. Toolkit owns `sys-toolkit` and is
optional; Toolkit and Prompt are the current packaged default roots, while
Agent, Memory, and Database are opt-in.

Entity Extensions are enabled through `ws.enable_extension(...)`. They register
normal Entity classes through the same schema service, may attach
Extension-owned APIs such as `ws.database`, and publish active Extension rows in
MetaSchema; they do not bypass storage planning or write directly to Backends.
Hard activation dependencies use `Extension.requires`. Installed cross-domain
records use `workspace_requires` and remain ineligible until every exact
Extension id is active.

Artifact-restored Entity classes are scoped to one resolver and installation
generation. The root dynamic Entity exports use `DEFAULT_CONTEXT`; another
Context may restore a different Python class object for the same identifier.
Workspace operations compare the registered identifier and structural schema,
then continue with that workspace's canonical class. Only fixed Catalog and
MetaSchema bootstrap requires exact class identity.

Workspace extension enablement is monotonic. The generic workspace contract has
no disable or unload operation because reversing extension-owned entities,
stored rows, derived effects, and lineage is domain business logic rather than
a universal technical lifecycle. An extension may expose its own cleanup
workflow when it can define those semantics without pretending to remove
effects it does not own.

The complete contract lives in
[Workspace Foundation and Extensions](workspace-foundation-and-extensions.md).

## Logical Types

HeavenBase ships scalar, structural, and vector logical types. Recent additions include enum-like and temporal semantics while preserving the same handler/backends pipeline:

| Type | Core semantics | Typical filters |
|------|----------------|-----------------|
| `Categorical([...])` | Enum-constrained string or integer values | `eq`, `ne`, `in`; text ops for string base; order ops for integer base |
| `Timestamp(unit="ms")` | UTC epoch timestamp (`s` / `ms` / `us`) | `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in` |
| `Duration()` | Non-negative millisecond interval (`interval` alias) | `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in` |
| `Date()` | Canonical ISO calendar date (`YYYY-MM-DD`) | `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, optional text ops |

`Categorical` supports both explicit declaration (`field(Categorical([...]))`) and fluent shorthand (`field(ShortText).choices(...)`). String categoricals do not support order filters, and integer categoricals do not support text filters.

`Identifier`, `ShortText`, `MediumText`, and `LongText` own a positive immutable `max_chars` value. Their stable defaults are 63, 255, 4095, and 65535 respectively; custom values use constructors such as `ShortText(max_chars=80)`. The concrete value is part of `to_dict()`, nested descriptors, schema signatures, SQL DDL, and workspace manifests rather than `CM_HVNB` policy.

`Timestamp` accepts `int`, `float`, `datetime`, and ISO-8601 strings. Its default millisecond bounds (`0` through `253402300799000`) are stable protocol constants; explicit `min` and `max` instance bounds serialize with the descriptor. `Date` accepts ISO date strings, `date`, and `datetime` inputs and stores canonical ISO date text via `validate()` / `encode()`. Timezone-aware `datetime` values are converted to UTC+0 before storage. Naive `datetime` values and offsetless ISO timestamp strings are accepted as UTC+0 by convention; callers should prefer timezone-aware `datetime` values when the source timezone is known.

HeavenBase intentionally does not ship a separate `Datetime` logical type yet. Use `Timestamp` for instants, `Date` for calendar dates, and `Duration` (JSON alias `interval`) for non-negative millisecond intervals such as Catalog TTL policies.
