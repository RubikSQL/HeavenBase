# HeavenBase Naming Vocabulary

Canonical reference for type suffixes and instance naming across HeavenBase modules.

Last updated: 2026-07-23

Related: [`docs/resources/architecture/concepts-and-classes.md`](../architecture/concepts-and-classes.md)

---

## Core suffix rules

| Suffix / term | Meaning | When it appears | Examples |
|---------------|---------|-----------------|----------|
| **Spec** | Caller-authored or fluent **declaration** | Before compilation; may be incomplete or overridden | `FieldSpec`, `QuerySpec`, `StorageRule`, `ConfigSpec` |
| **Schema** | Compiled **logical structure** | After `EntityMeta` or IR deserialization; describes what a row means | `EntitySchema`, `FieldSchema`, persisted `MetaSchema` |
| **Plan** | **Resolved** placement or execution artifact | After registration or (future) query planning | `StoragePlan` |
| **Fragment** | Backend-bound **operation slice** | Handler output | `QueryFragment` |
| **Binding** | One resolved link in a plan | Field → backend + strategy | `StorageBinding` |
| **Ref** | Lightweight proxy for DSL / queries | Not data; not storage | `FieldRef` |
| **IR** | Documentation label for a compile layer | **Never** a Python class suffix | “schema IR”, “query IR” in architecture docs |

**Do not use `Def` for new types.** Prefer **Spec** (input) or **Schema** (compiled output).

---

## Instance naming conventions

Use snake_case locals that mirror the type role:

| Type | Preferred local name | Avoid |
|------|---------------------|-------|
| `FieldSchema` | `field_schema` | `field_def`, `fdef` |
| `EntitySchema` | `entity_schema` or `schema` when unambiguous | `entity_def` |
| `FieldSpec` | `field_spec` or `spec` on entity classes | `field_schema` (wrong layer) |
| `StoragePlan` | `storage_plan` | `plan` alone in storage code |
| `StorageBinding` | `binding` | `route` |
| `QuerySpec` | `query_spec` | `query_ir` |
| `QueryFragment` | `fragment` | `compiled` |

Handler signatures use `(field_schema, value, ctx)` — the first argument is always a `FieldSchema` instance.

---

## Data model layer

```text
User / agent
    │
    ├─ Entity subclass + FieldSpec     (declaration)
     └─ JSON dict ──Entity.from_schema──► Entity subclass
                │
                ▼ EntityMeta.__new__
         EntitySchema { FieldSchema }
         entity._hb_specs { FieldSpec }   ← stores, computes stay here
```

| Name | Module | Role |
|------|--------|------|
| `LogicalType` | `types/` | Value semantics: validate, encode, decode, filter ops |
| `FieldSpec` | `entity/fields.py` | Field declaration: `store()`, `compute()`, defaults |
| `FieldSchema` | `schema/` | One compiled field (metadata only) |
| `EntitySchema` | `schema/` | Compiled entity type |
| `Entity` | `entity/base.py` | Public entity class |
| `FieldRef` | `entity/ref.py` | Query expression proxy |
| `MetaSchema` | `workspace/metaschema/` | Fixed workspace entity: queryable structure and Registry projection |
| `Catalog` | `workspace/catalog/` | Fixed workspace entity: concrete-object discovery rows |
| `_HyperGCol` | `types/hyperg.py` | HyperG sub-field (private; has `filterable`) |

**Naming decisions (fixed):**

- **Keep `EntitySchema`** — compiled logical structure; not `EntitySpec` (Spec = declaration) or `EntityDef` (Def retired).
- **`FieldSchema`** (was `FieldDef`) — pairs with `EntitySchema`.
- **No `EntitySpec` class** — entity authoring is the `Entity` subclass or dict → `Entity.from_schema()`.

Serialization: `FieldSchema.from_dict()` / `EntitySchema.from_dict()` for IR roundtrip; `Entity.from_schema()` for full defs with storage and compute.

---

## Storage layer

```text
FieldSpec.stores + EntitySchema
        │
        ▼ StorageResolver.resolve()
   StoragePlan { StorageBinding[] }
        │
        ▼ Backend.ensure(schema, bindings)
   physical tables / collections
```

| Name | Role |
|------|------|
| `StorageRule` | One authored or default placement rule |
| `StorageBinding` | One resolved field → backend + strategy |
| `StoragePlan` | All bindings for one entity |
| `StorageResolver` | Rule resolution at registration |
| `StorageProfile` | Default rule template per logical type |
| `StorageStrategy` | Layout marker (`InlineColumn`, `VectorIndex`, `SideTable`, …) |

Avoid **Route** in new code — use **Storage** for field-to-backend placement.

### Registry Identity (Types, Strategies, Backends)

| Kind | Canonical Identifier | Lookup |
|------|----------------|--------|
| Logical type | `short-text` | `LogicalType.load("short-text")` |
| Storage strategy | `inline` | `StorageStrategy.load("inline")` |
| Backend identifier | `sqlite`, `inmem` | `BackendType.identifier_of("sqlite")` |
| Query op | `eq` | `hb.ext.normalize_op("$eq")` |

Handler keys and storage bindings store canonical **identifiers**. Serialized instance configs use `type` to point at class identifiers, and entity rows use `object_id` for object identity.

See [`docs/resources/reference/id-semantics.md`](id-semantics.md).

---

## Query layer

```text
Query / QuerySpec / Expr
        │
        ▼ explain() / execute()
 temporary routes + QueryFragment
        │
        ▼ Backend.execute(fragment)
   detached ResultFrame
        │
        ▼ explicit export
 rows / scalar / ecosystem value
```

| Name | Role |
|------|------|
| `Query` | Immutable fluent query value containing spec-only caller intent |
| `QuerySpec` | Frozen normalized request passed to workspace routing |
| `Expr`, `Filter`, `And`, `Or`, `Not` | Filter request tree nodes owned by `heavenbase.filters` |
| `QueryFragment` | Backend-ready operation |
| `HandlerContext` | Backend instance name, backend `type` identifier, strategy options, keyword near params |
| `Near` | Vector query request node |

Every query transform returns another `Query`; there is no public builder,
`copy`, or `fork`. `QueryPlan` is reserved for a future materialized execution
plan and must never be stored on `Query`. `ResultFrame` is a detached transport
container; application code should explicitly export it.

---

## Extensions and infrastructure

| Name | Role |
|------|------|
| `Extension` | Extension entity-bundle descriptor and lifecycle |
| `WorkspaceSpec` | Workspace registry row |
| `ConfigSpec`, `DBSpec`, `LLMSpec` | Config declarations |
| `PromptSpec` | Prompt template declaration |
| `ArgumentSpec`, `OptionSpec`, `CommandSpec` | CLI surface |

---

## Module name `schema/`

The Python package `heavenbase.schema` holds in-memory **EntitySchema** / **FieldSchema** IR only.

It is **not**:

- the persisted `MetaSchema` system entity
- SQL DDL or database schema
- JSON Schema validation

“Schema IR” in developer docs refers to this compile layer conceptually.

---

## Quick lookup: “what suffix should I use?”

| I am building… | Use |
|----------------|-----|
| User-facing field declaration on `Entity` | `FieldSpec` via `hb.field(...)` |
| Compiled field metadata after registration | `FieldSchema` |
| JSON entity definition compiler output | `Entity` subclass (→ `EntitySchema` internally) |
| Where a field is stored | `StorageRule` → `StoragePlan` |
| What the user wants to query | `Query` → normalized `QuerySpec` |
| What a backend executes | `QueryFragment` |
