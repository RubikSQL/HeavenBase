# JSON Entity Definitions

This page describes the JSON-compatible entity definition format used by `ws.register(...)` and `hb.Entity.from_schema(...)`.

Use it when an entity shape should come from a config file, UI form, API payload, or generated schema instead of a Python class body. The compiler builds a normal `hb.Entity` subclass, so registration, default values, compute hooks, validation, storage placement, `Catalog`, `MetaSchema`, and queries all use the same runtime path as hand-written Python entities.

## Quick Use

```python
import heavenbase as hb


def embed_text(text: str | None) -> list[float]:
    lowered = (text or "").lower()
    return [float(lowered.count("schema")), float(lowered.count("vector"))]


def identity(value):
    return value


NOTE_DEF = {
    "entity_id": "note",
    "name": "Note",
    "desc": "Short note with a searchable body and tiny computed embedding.",
    "fields": {
        "object_id": {"type": "identifier", "pk": True, "desc": "Built-in stable object id"},
        "title": {"type": "short-text", "desc": "Display title"},
        "body": {"type": "long-text", "desc": "Searchable body text"},
        "emb": {
            "type": {"type": "vector", "dim": 2},
            "desc": "Tiny in-memory embedding",
            "compute": {"fn": "embed_text", "inputs": ["body"]},
        },
    },
    "derive": [
        {"target": "catalog.name", "compute": "identity", "inputs": ["title"]},
        {"target": "catalog.desc", "compute": "identity", "inputs": ["body"]},
    ],
}

ws = hb.HeavenBase("demo", backends={"main": {"type": "inmem"}})
Note = ws.register(NOTE_DEF, computes={"embed_text": embed_text, "identity": identity})

ws.upsert("note", {"object_id": "n1", "title": "Catalog quickstart", "body": "metaschema rows explain the workspace"})
matches = ws.query(Note).where(Note.body.match("metaschema")).select("title").execute().rows()
```

If you only want the class and will register it later, use:

```python
Note = hb.Entity.from_schema(NOTE_DEF, computes={"embed_text": embed_text, "identity": identity})
ws.register(Note)
```

## LLM Schema Text

`Entity.schema().to_dict()` remains the JSON-compatible export. For agent prompts and future structured information extraction flows, `Entity.schema().to_str()` returns a compact LLM-friendly schema description:

```python
print(Note.schema().to_str())
```

```text
Entity schema: Note (note)
Description: Short note with a searchable body and tiny computed embedding.
Use field names exactly.

Fields:
- object_id: identifier (primary key; required)
  Description: Built-in stable object id
- title: short-text (optional)
  Description: Display title
- body: long-text (optional)
  Description: Searchable body text
- emb: vector(dim=2) (optional)
  Description: Tiny in-memory embedding
```

The built-in text format is named `llm`. Extension authors can register alternate formats for specialized extraction prompts without changing the entity model:

```python
hb.ext.register_schema_text("compact", lambda schema: schema.entity_id, replace=True)
Note.schema().to_str(format="compact")
```

`ws.register(...)` establishes an Entity class from an `hb.Entity` subclass or
a JSON-compatible definition and returns the canonical class owned by that
workspace. If an Entity instance is supplied for convenience, only its class
definition is used; its row values are ignored and no instance is written.

An expert may also write an active Entity-kind `hb.MetaSchema` row with this
complete JSON object under `meta`. When the entity id is absent from the
workspace, HeavenBase consumes that payload as schema command ingress and
registers it before writing the caller's metadata row. The top-level
`subject_id`, `name`, and `desc` remain searchable projection fields and do not
override `meta`. A later manual row never replaces an already-present class;
use `ws.register(...)` for the ordinary consistency-guaranteeing path.

`ws.upsert(...)`, `ws.query(...)`, and `ws.query_json(...)` may use an
already-present Entity class or its registered identifier, such as `"note"`,
when the caller does not have the generated class in scope. Passing a class,
identifier, or Entity instance to CRUD never registers it: HeavenBase first
resolves the entity id to the workspace's canonical class and rejects an absent
or structurally different class. This statement concerns CRUD for the target
Entity; the explicit MetaSchema command-ingress exception above is not an
instance shorthand. Row reads use `ws.get(object_id)` and only need
`entity="note"` when the same object identity exists under multiple entity
types.

## Entity Object

The top-level object describes one entity type.

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `entity_id` | string | No | Stable catalog id. If omitted, HeavenBase derives kebab-case from `name`. Must be a lower-case slug: letters, digits, `-`, or `_`, starting with a letter. |
| `name` | string | Yes, unless `entity_id` can derive it | Python class name for the generated entity. Use a public Python identifier such as `Note`, `Product`, or `SupportTicket`. |
| `desc` | string | No | Entity description. Stored in `MetaSchema` and searchable as metadata. |
| `description` | string | No | Alias for `desc`. |
| `tags` | list[string] | No | Entity-level searchable tags. |
| `card` | object | No | Metadata alias. `short_desc`, `desc`, or `long_desc` fills `desc`; `tags` fills `tags` when direct keys are absent. |
| `derive` | object or list[object] | No | Entity and Catalog derivation rules. |
| `derives` | list[object] | No | Additional derivation rules. |
| `fields` | object or list | Yes | Field definitions. Object form is recommended. |
| `catalog` | string | No | Catalog projection mode used when the definition is registered: `full` (default), `identity`, or `none`. Same meaning as `ws.register(..., catalog=...)`. Generated manifests include the registered value. `ws.alter_entity(...)` rejects a value that differs from the registered one. |
| `gram` | boolean | No | Whether the entity's `Array[ShortText]` fields keep a sparse-GRAM keyword index (default `true`). Same meaning as `ws.register(..., gram=...)`. Generated manifests include the registered value. `ws.alter_entity(...)` rejects a value that differs from the registered one. |

Object-style fields are concise and preserve field order:

```json
{
  "fields": {
    "title": {"type": "short-text", "desc": "Display title"},
    "body": {"type": "long-text", "desc": "Searchable body text"}
  }
}
```

List-style fields are useful for UIs that naturally emit arrays:

```json
{
  "fields": [
    {"name": "title", "type": "short-text", "desc": "Display title"},
    {"name": "body", "type": "long-text", "desc": "Searchable body text"}
  ]
}
```

Field names must be public Python identifiers: `title`, `body_text`, `emb`. They cannot start with `_`, cannot be Python keywords, and list-style field names must be unique. This keeps `Note.body.match(...)` and other DSL access valid.

## Field Object

Each field object maps to `hb.field(Type)` plus the same fluent options available in Python.

| Key | Type | Required | Python equivalent | Meaning |
| --- | --- | --- | --- | --- |
| `type` | string or object | Yes | `hb.field(T)` | Logical type. See the type section below. |
| `dtype` | string or object | No | `hb.field(T)` | Alias for `type`. |
| `pk` | boolean | No | `.pk()` | Compatibility marker for `object_id`. HeavenBase always primary-keys `object_id` and rejects `pk` on other fields. |
| `required` | boolean | No | `.required()` or `.optional()` | Requires the field on write unless a default or compute hook fills it. |
| `default` | JSON value | No | `.default(value)` | Default value applied before compute hooks. Non-null values set a default automatically. |
| `has_default` | boolean | No | `.default(None)` support | Set true when the intended default is JSON `null`. |
| `desc` | string | No | `.desc(text)` | Field description stored in `MetaSchema` rows. |
| `description` | string | No | `.desc(text)` | Alias for `desc`. |
| `tags` | list[string] | No | `.tags(...)` | Searchable field tags. |
| `examples` | list | No | `.example(...)` | Example values for humans and tools. |
| `storage` | object or string | No | `.store(...)` | One storage placement. A string means backend name. |
| `stores` | list[object|string] | No | repeated `.store(...)` | Multiple storage placements for the same field. |
| `compute` | object, string, or callable | No | `.compute(fn, inputs=[...])` | One compute hook. JSON files should use a function name string. |
| `computes` | list | No | repeated `.compute(...)` | Multiple compute hooks. Most fields need at most one. |
| `card` | object | No | metadata alias | `short_desc`, `desc`, or `long_desc` fills field `desc`; `tags` and `examples` fill direct keys when absent. |

If the JSON definition omits an `object_id` field, HeavenBase adds the usual built-in `object_id: Identifier` primary key field. `object_id` is required and cannot be null. Rows may omit it only when HeavenBase can compute it from an object ID compute hook, an explicit non-empty `catalog.name` derive, or a non-empty native `name` field. Blank and whitespace-only name sources are treated as missing. A native `name` projects to Catalog only when its logical type descriptor matches the Catalog field; otherwise it supplies identity without a visible Catalog name, and a top-level `derive` for `catalog.name` is required for that projection. Include `object_id` explicitly when you want a custom description, tags, default, or compute hook for the object ID. `object_id` storage placements are fixed by HeavenBase and cannot be customized. See [id-semantics.md](id-semantics.md).

## Logical Types

Use the same logical types that Python entities use.

| JSON | Python |
| --- | --- |
| `"identifier"` | `hb.Identifier` |
| `"short-text"` | `hb.ShortText` |
| `{ "type": "short-text", "max_chars": 80 }` | `hb.ShortText(max_chars=80)` |
| `"medium-text"` | `hb.MediumText` |
| `"long-text"` | `hb.LongText` |
| `"integer"` | `hb.Integer` |
| `"float"` | `hb.Float` |
| `"boolean"` | `hb.Boolean` |
| `{ "type": "categorical", "values": ["todo", "doing", "done"], "base": "string" }` | `hb.Categorical(["todo", "doing", "done"])` |
| `{ "type": "categorical", "values": [1, 2, 3], "base": "integer" }` | `hb.Categorical([1, 2, 3], base="integer")` |
| `"timestamp"` | `hb.Timestamp` |
| `{ "type": "timestamp", "unit": "us" }` | `hb.Timestamp(unit="us")` |
| `"date"` | `hb.Date` |
| `"json"` | `hb.Json` |
| `"hyper-g[slot,value,topic]"` | `hb.HyperG["slot", "value", "topic"]` |
| `{ "type": "hyper-g", "fields": [{"name": "subject", "dtype": "identifier"}, {"name": "relation", "dtype": {"type": "categorical", "values": ["cites", "mentions"]}}, {"name": "object", "dtype": "identifier"}] }` | `hb.HyperG["subject": hb.Identifier, "relation": hb.Categorical["cites", "mentions"], "object": hb.Identifier]` |
| `"artifact"` | `hb.Artifact` |
| `"vector[1536]"` | `hb.Vector[1536]` |
| `{ "type": "vector", "dim": 1536 }` | `hb.Vector[1536]` |
| `"array[short-text]"` | `hb.Array[hb.ShortText]` |
| `{ "type": "array", "item_type": {"type": "short-text", "max_chars": 32} }` | `hb.Array(hb.ShortText(max_chars=32))` |

Type aliases are accepted for lookup, but docs and generated payloads should use canonical lower-hyphen identifiers.

Default text limits are stable protocol values: `Identifier=63`, `ShortText=255`, `MediumText=4095`, and `LongText=65535`. Each concrete logical-type descriptor owns and serializes its positive `max_chars` value, including the default, so nested types, schema signatures, and workspace manifests do not depend on process or Context configuration. Use an explicit descriptor such as `{"type": "short-text", "max_chars": 80}` to select another limit.

`Categorical` values are required and must be unique. String categoricals support text filters; integer categoricals support order filters; both support `eq`, `ne`, and `in`. Python code may use `hb.Categorical["todo", "doing", "done"]` and `hb.Categorical[1, 2, 3]` as compact shorthands.

Every new `HyperG` field should declare its mini-row schema explicitly. In Python, use quoted slice-style child field names, for example `hb.HyperG["subject": hb.Identifier, "relation": hb.ShortText, "object": hb.Identifier]`. JSON descriptors use a `fields` list; each child field accepts `name`, `dtype`, `required`, and `filterable`. Data child fields such as `Json` should set `filterable=false` when they are stored for payload only. The compact string `"HyperG"` is still parsed as `slot/value/topic` for old descriptors, but docs, demos, and new code should write `"HyperG[slot,value,topic]"` or the full `fields` descriptor. Query mini-rows with `$exists` / `.exists(...)`; `$any` / `.any(...)` are aliases. Use `$all` / `.all(where={...}, ...)` for universal constraints over matching mini-rows; omitted child fields act as wildcards.

`Timestamp.unit` supports `s`, `ms`, and `us`. `Timestamp` stores UTC+0 epoch integers. `Date` always lowers to canonical `YYYY-MM-DD` text, and `datetime` inputs are converted to UTC+0 before the date part is stored. Naive `datetime` values and offsetless ISO timestamp strings are assumed to already be UTC+0.

There are no separate JSON logical types for `Datetime`. Model instants with `Timestamp`, calendar dates with `Date`, and elapsed intervals with `Duration` (alias `interval`) in milliseconds. `0` means a disabled duration policy; positive values store non-negative millisecond counts.

## Storage Placements

Storage placements are optional. If omitted, `StorageResolver` chooses the default backend and storage strategy from the field type and workspace backend registry.

```json
{
  "emb": {
    "type": "vector[1536]",
    "storage": {"to": "vec", "strategy": "vector", "reason": "semantic-search"}
  }
}
```

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `to` | string | No | Backend instance name, such as `main`, `vec`, or `docs`. |
| `backend` | string | No | Alias for `to`. |
| `strategy` | string or object | No | Lower-hyphen strategy identifier, or `{"type": identifier, ...options}`. Defaults to `inline` for explicit JSON storage placements. |
| `optional` | boolean | No | If true, silently skips this storage placement when the backend is absent. |
| `reason` | string | No | Placement source stored in `MetaSchema.source`. Defaults to `json`. |
| `params` | object | No | Per-field placement parameters, such as a `GraphEdge` endpoint column. |

Built-in strategy identifiers are `inline`, `json`, `side`, `vector`, `inverted`, `sparse-gram`, `graph`, and `external`; PascalCase class names such as `VectorIndex` and `SparseGramIndex` are accepted as lookup aliases.

`GraphEdge` stores lifecycle policy in the strategy specification and its endpoint column in placement
parameters:

```json
{
  "cites": {
    "type": {
      "type": "hyper-g",
      "fields": [
        {
          "name": "cited",
          "dtype": {"type": "identifier", "target_entity": "doc"}
        }
      ]
    },
    "storage": {
      "strategy": {"type": "graph", "on_delete": "detach"},
      "params": {"target": "cited"}
    }
  }
}
```

`on_delete` accepts only `restrict` (the default) or `detach`. The same strong endpoint validation and
delete semantics apply to Python- and JSON-authored entities within one live workspace's serialized
mutation boundary. See [Public API Tiers](public-api.md#graphedge-lifecycle).

## Computes

JSON cannot carry a Python callable, so compute hooks should name a callable supplied at compile time.

```json
{
  "emb": {
    "type": {"type": "vector", "dim": 2},
    "compute": {"fn": "embed_text", "inputs": ["body"]}
  }
}
```

```python
Note = ws.register(NOTE_DEF, computes={"embed_text": embed_text})
```

`inputs` controls the callable signature. With `inputs`, HeavenBase calls `fn(row[input_1], row[input_2], ...)`. Without `inputs`, HeavenBase calls `fn(row_dict)`.

Compute hooks run during `ws.upsert(...)` only when the computed field is absent from the incoming row. A provided value wins. Defaults are applied before compute hooks, so do not put `default` on a field that should normally be computed.

## Defaults And Required Fields

Required fields are checked after defaults and compute hooks materialize values.

```json
{
  "status": {"type": "short-text", "default": "draft"},
  "body": {"type": "long-text", "required": true},
  "attrs": {"type": "json", "default": null, "has_default": true}
}
```

Use `has_default: true` when the intended default is `null`. Without `has_default`, `default: null` is treated like an omitted default so schemas round-trip cleanly from catalog payloads that use `null` to mean no default.

## Derives

Top-level derives use the same callable registry and compute shape. Bare targets fill native fields; `catalog.<field>` targets project values into Catalog:

```json
{
  "derive": [
    {"target": "summary", "compute": "summarize", "inputs": ["body"]},
    {"target": "catalog.name", "compute": {"fn": "identity", "inputs": ["title"]}},
    {"target": "catalog.desc", "compute": "identity", "inputs": ["body"]}
  ]
}
```

The shorthand form with top-level `inputs` and the nested compute-object form both resolve through the same `computes` registry. `fields` is accepted as an alias for `inputs` in compute objects emitted by schema tools.

## Resolving Compute Names Without a `computes` Mapping

A compute name resolves through the Entity's Context-local module resolver in three steps: the `computes` mapping passed to this call, then a hook registered with `hb.ext.register_compute(...)` under that identifier, then a registered hook whose import target matches the name.

That last step matters when a workspace is reopened from its stored manifest. A Python-authored field carries a live callable, and the manifest serializes it as `f"{fn.__module__}.{fn.__qualname__}"` — a name no `computes` mapping supplies, because nobody wrote it by hand. Registering the callable in the owning Context makes that stored reference resolve in another process that opens the same module registry and can import the callable's module:

```python
import heavenbase as hb

from myapp.vectors import embed_text

hb.ext.register_compute("myapp_embed_text", embed_text)
```

For an explicit Context, pass its module service as `resolver=context.modules()` to registration and use that Context when opening the workspace.

Query-compute hooks use `hb.ext.register_query_compute(...)` and resolve the same way. `hb.ext.registered_computes()` and `registered_query_computes()` list what is currently registered.

Catalog auto fields such as `target_id`, `target_entity`, `created_at`, `updated_at`, `expires_compute`, and `stale_compute` are generated by HeavenBase and cannot be derived.

Same-named native fields with the same logical type descriptor project to matching Catalog fields automatically, including `name`, `desc`, `summary`, `tags`, `owner`, and `creator`. A same-named field with another logical type remains application data and leaves the Catalog default unchanged. In Catalog, `desc` is the short searchable blurb and `summary` is the long payload text. Use explicit derives when the source field has another name, such as `title -> catalog.name`, or when the Catalog value needs normalization.

Timing policy fields are projectable: `expires_at` uses `hb.Timestamp.default_max` as the disabled sentinel; `expires_after` and `stale_after` use the `Duration` logical type with `0` as disabled.

## Catalog Output

After registration, JSON-authored entities appear in the same Catalog and MetaSchema surfaces as Python-authored entities. Catalog rows always include generated identity and timestamps, project same-named discovery fields with matching logical type descriptors by default, and use top-level derives for non-matching source fields or normalizations.

```python
objects = (
    ws.query(hb.Catalog)
    .available()
    .where(hb.Catalog.target_entity == "note")
    .execute()
    .rows()
)
fields = (
    ws.query(hb.MetaSchema)
    .where(hb.MetaSchema.kind == "field")
    .where(hb.MetaSchema.subject_id == "note")
    .execute()
    .rows()
)
storage = (
    ws.query(hb.MetaSchema)
    .where(hb.MetaSchema.kind == "storage")
    .where(hb.MetaSchema.subject_id == "note")
    .execute()
    .rows()
)
```

JSON and MCP queries use the top-level `available` key or an embedded `$available` filter operator with the same semantics. Pass `stale: true` to include stale rows or `invisible: true` to include hidden rows.

Field `desc`, `tags`, `examples`, `default`, `has_default`, primary-key state, and logical type are stored in each MetaSchema field row's `meta` payload. Storage rows record the backend instance name, backend type identifier, strategy identifier, and placement source.

## Validation Errors

The compiler fails early for malformed definitions:

| Problem | Error |
| --- | --- |
| Missing `fields` | `ValueError` |
| Unknown logical type | `ValueError` |
| Invalid field name | `ValueError` |
| Duplicate list-style field name | `ValueError` |
| Unsafe `entity_id` slug | `ValueError` |
| Top-level `id` key | `ValueError`; use `entity_id` for the entity type ID |
| Storage placement that is not a string or object | `TypeError` |
| Compute function name absent from `computes` and from the compute registry | `KeyError` |

This keeps bad configuration from creating half-registered workspace state.
