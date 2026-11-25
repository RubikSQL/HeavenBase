# Interop

HeavenBase interop turns familiar Python data objects into Entity schemas and rows, then exports `ResultFrame` data back to those ecosystems.

## Import Surfaces

```python
import heavenbase as hb

iset = hb.Entity.from_sql_result(
    [["p-1", "Oak Desk", 95.0]],
    columns=["sku", "name", "price"],
    entity_id="product",
    id="sku",
)
ws = hb.HeavenBase("interop-doc")
iset.register(ws)
iset.upsert(ws)
```

Supported import sources:

- `hb.Entity.from_pydantic(Model, rows=[...])`
- `hb.Entity.from_pandas(df, ...)`
- `hb.Entity.from_pyarrow(table, ...)`
- `hb.Entity.from_numpy(array, columns=[...], ...)`
- `hb.Entity.from_sql_result(result, ...)` for SQLAlchemy Result/RowMapping, SQLResponse-like objects, `List[Dict]`, or `List[List]` with `columns`
- `hb.Entity.from_daft(df, ...)` for Daft DataFrames (requires `pip install heavenbase[daft]`)

The root `hb.from_*` functions remain as support exports for existing code and compact scripts, but new docs should prefer the Entity-owned form.

All import calls return an `InteropSet`. `iset.entity` is the Entity class,
`iset.rows` are normalized row dictionaries, and `iset.report` records
assumptions or errors. Schema and row persistence are separate:
`iset.register(ws)` explicitly establishes the inferred Entity class, while
`iset.upsert(ws)` writes staged rows only after that class is already present.

Every importer accepts `resolver=` for Registry-backed logical types and inference
rules. When staging for an explicitly constructed Context, pass
`resolver=context.modules()`; omission deliberately selects `DEFAULT_CONTEXT`.
Materialized inferers are cached by resolver identity and record publication state, so
same-key rules in two Contexts cannot contaminate one another.

`report.ok` must be true before registration or upsert. Both operations call
`report.raise_if_error()`, so duplicate columns, invalid names, and unknown ID
mappings fail before schema or row writes respectively. `InteropSet.upsert(ws)`
also fails if its Entity class is absent or differs structurally from the
workspace's canonical class; it never registers as a side effect.

## Implementation Roles

| Module | Objective |
| --- | --- |
| `interop/base.py` | Owns the staging contracts and diagnostics: `InteropIssue`, `InteropReport`, `InteropSet`, optional dependency loading, and metadata application. |
| `interop/rows.py` | Coerces external record-shaped values into plain row dictionaries and preserves null-only columns for inference. |
| `interop/mapping.py` | Infers HeavenBase fields, applies rename/overrides/defaults, and builds the dynamic Entity class. |
| `interop/{pandas,pyarrow,numpy,pydantic,sql,daft}.py` | Reads one source family, gathers source hints, and delegates staging to `build_set(...)`. |

The row layer is deliberately schema-agnostic. It normalizes values and object shapes; it does not
choose logical types, field names, requiredness, defaults, or storage placement.

## Type Inference

Interop inference is conservative:

- Unknown-length strings become `LongText`.
- Fixed object IDs use `Identifier` when `id="source_column"` is provided.
- Vectors require explicit `hb.Vector[N]` unless the source type carries a fixed dimension.
- Nested mappings become `Json` with a report warning.
- Ambiguous columns should use `overrides={"field": hb.Type}` or `overrides={"field": hb.field(hb.Type).store(...)}`.

`overrides` may refer to the source name or the renamed HeavenBase field name. For example, this applies a `ShortText` field after renaming `display_name` to `name`:

```python
iset = hb.Entity.from_sql_result(
    [{"sku": "p1", "display_name": "Desk"}],
    entity_id="product",
    id="sku",
    rename={"display_name": "name"},
    overrides={"name": hb.field(hb.ShortText).required()},
)
```

Columns that exist only as NULL or missing values stay in the inferred schema and produce an `empty_column` warning, while normalized row dictionaries omit the missing value. Use an override when the intended type is not `LongText`.

Custom logical types can extend inference without editing the interop core:

```python
class Category(hb.LogicalType):
    identifier = "category"


def infer_category(source, values, hint, report):
    return Category() if source == "category" and len(set(values)) < 32 else None


hb.register_interop_type_inferer(
    infer_category,
    first=True,
    resolver=context.modules(),
)
```

The hook receives the source column name, non-missing sample values, the source hint or dtype, and
the active `InteropReport`. Return `None` to let the next inferer decide. For a named inspectable
rule, wrap the callable in `hb.InteropTypeInferer(fn, identifier=..., priority=...)`;
`hb.register_interop_type_inferer(...)` stores that spec in execution-priority order.

Minimal protocol:

| Surface | Role |
| --- | --- |
| `identifier` | Registry key for inspection and replacement. |
| `priority` | Ordering value; lower values run first. |
| `infer(source, values, hint, report)` | Return a `LogicalType` or `None`. |
| `to_dict()` | Return identifier, callable reference, and priority. |

## Metadata

Sources such as PyArrow tables and SQL result rows often do not carry descriptions. Use `desc`, `tags`, and `field_meta` during import:

```python
iset = hb.Entity.from_sql_result(
    [{"sku": "p-1", "name": "Oak Desk"}],
    entity_id="product",
    id="sku",
    desc="Sellable product",
    field_meta={"name": {"desc": "Display name"}},
)
```

For already registered entities, use `ws.describe(entity, desc=..., fields=...)`. Metadata-only changes refresh MetaSchema rows. Structural changes such as dtype or storage placement changes require explicit replacement and are rejected by default. An entity an enabled extension registered keeps the extension's definition, so `describe` and replacement raise `ValueError` for it when they would change it.

## Export Surfaces

`ResultFrame` is the detached transport returned by query execution. Entity
query frames are identity-bearing and backend-merged by `object_id`;
table-shaped projection and aggregate frames can be identityless. Application
code should immediately choose an explicit export instead of printing,
implicitly coercing, or using frame transformations as its working data model:

- `frame.rows()` returns ordinary row dictionaries.
- `frame.scalar()` returns the sole value of an exactly one-row, one-column
  result.
- `to_pandas`, `to_pyarrow`, `to_numpy`, `to_pydantic`, and `to_daft` use late
  optional imports.

Identity, column, projection, sorting, limiting, filtering, and merge helpers
remain useful to workspace execution and low-level integrations. They are not a
second query language and are not the recommended application boundary.

Pre-merge backend frames are not a stable user API. Use `query.explain()` to inspect how HeavenBase grouped and merged backend work.

```python
frame = ws.query(iset.entity).execute()
rows = frame.rows()
df = frame.to_pandas()
table = frame.to_pyarrow()
array = frame.to_numpy("name", "price")
models = frame.to_pydantic(ProductModel, rename={"object_id": "sku"})
df_daft = frame.to_daft()
```

The example enumerates the available exits; ordinary code should select the one
native representation it needs. Re-running `execute()` would recompute the
query rather than reuse or persist this frame.

## Daft

Daft is an optional extra, separate from `[interop]`:

```bash
pip install heavenbase[daft]
```

Importing `heavenbase` does not import Daft. Missing Daft raises only when you call `from_daft`, `to_daft`, or `ResultFrame.to_daft()`.

Daft conversion uses PyArrow as the pivot: `from_daft` calls
`DataFrame.to_arrow()` then reuses `from_pyarrow`; `to_daft` calls `to_pyarrow`
then `daft.from_arrow`. Use Daft for distributed or lazy relational transforms,
then explicitly call `iset.register(ws)` before the first
`iset.upsert(ws)`. Export query results with `frame.to_daft()` when you need to
continue in Daft.

Business keys such as `customer_id` often map to HeavenBase `object_id` on import:

```python
iset = hb.Entity.from_daft(
    customer_df,
    entity_id="customer",
    id="customer_id",
    rename={"customer_id": "object_id"},
)
```

Importing `heavenbase` does not import Pydantic, Pandas, PyArrow, NumPy, or Daft. Missing optional packages raise at the specific `from_*` or `to_*` call site with an `install heavenbase[interop]` or `install heavenbase[daft]` hint.
