# Contributor Guide: Add a Backend, Strategy, and Handler

Status: current for the `0.1.2.3` package and Context model.

This guide describes the stable extension boundary. Use `hb.ext` for published
authoring contracts and keep a cohesive contribution in one ADR 0012 module folder.
Detailed SQL parity policy lives in [text semantics boundaries](text-semantics-boundaries.md);
route diagnostics live in [routing and explain](routing-and-explain.md).

## Mental Model

```text
Entity field
   -> authored placement rule/profile
   -> StoragePlan binding
   -> HandlerRegistry compile
   -> QueryFragment
   -> Backend physical IO
   -> ResultFrame
   -> workspace merge
```

- A **Backend** owns physical persistence, provider clients, and fragment execution.
- A **strategy** is immutable logical placement/index semantics.
- A **handler** compiles one logical operation into a neutral fragment; it performs no
  IO.
- The **workspace** owns routing, cross-Backend coordination, and honest explain
  output.
- A **Context** owns the ModuleResolver, config, and workspace identities that all
  those objects inherit.

## Choose the Honest Folder Boundary

A standalone provider keeps its complete contribution together:

```text
example_provider/
  __init__.py
  meta.yaml                 # backend_type + handler + related provider facts
  README.md
  backend/
    __init__.py
    runtime.py
  handler/
    __init__.py
    runtime.py
```

SQL or vector behavior that genuinely changes as one family may remain in a cohesive
family module. Do not split it only for symmetry, and do not duplicate shared
compilers under every provider.

| Concern | Owner |
| --- | --- |
| Neutral `RowOp`, `QueryFragment`, and `HandlerContext` | `execution/contracts.py` |
| `ResultFrame` and identity merge | `execution/frame/` |
| Shared handler Registry, seed protocol, predicates, and portable compilers | `execution/handlers/` |
| Provider-native handlers and reasons | provider or cohesive family under `backends/` |
| Logical placement rules/profiles/plans | `placement/` |
| Physical adapter, provider facts, and effective tag overlays | `backends/` |

Built-in and external folders use the same `meta.yaml` shape. Install an external
folder through `context.modules().install(path)`. Built-ins are discovered only by
explicit setup and then use the same catalog and resolver path. There is no provider
import list or built-in lookup fallback.

## 1. Implement Physical Backend Behavior

Subclass `hb.ext.Backend`. A Backend receives neutral execution values, returns root
`hb.ResultFrame` values, and never parses `QuerySpec` or user DSL objects.

```python
from collections.abc import Sequence
from typing import Any

import heavenbase as hb


class ExampleBackend(hb.ext.Backend):
    identifier = "example"

    @classmethod
    def from_config(
        cls,
        name: str,
        data: dict[str, Any],
        *,
        resolver=None,
        config=None,
    ) -> "ExampleBackend":
        provider_config = dict(data)
        return cls(
            name,
            ws_id=provider_config.pop("ws_id", None),
            resolver=resolver,
            config=config,
            **provider_config,
        )

    def __init__(
        self,
        name: str,
        *,
        resolver=None,
        config=None,
        ws_id=None,
        **provider_config: Any,
    ) -> None:
        super().__init__(
            name,
            ws_id=ws_id,
            resolver=resolver,
            config=config,
        )
        self.provider_config = provider_config

    @hb.ext.Backend.locked
    def ensure(self, schema: Any, bindings: Any) -> None:
        """Create or validate physical provider structures."""
        ...

    @hb.ext.Backend.locked
    def upsert(self, rows: Sequence[hb.ext.RowOp]) -> None:
        """Apply ordered row mutations without changing their observable order."""
        ...

    @hb.ext.Backend.locked
    def execute(self, fragment: hb.ext.QueryFragment) -> hb.ResultFrame:
        """Execute one bound provider fragment."""
        ...

    @hb.ext.Backend.locked
    def scan(self, entity_id: str) -> list[dict[str, Any]]:
        """Return detached canonical rows for portable execution."""
        ...
```

Construction receives the owning workspace id, Context `config`, and
`module_resolver` through the common Backend construction path. Do not read
`DEFAULT_CONTEXT`, `CM_HVNB`, or a process-global resolver in a constructor. Keep
provider config values intact after consuming common control fields.

Publish a `backend_type` record in the folder descriptor. Its `meta.tags`
mapping is the sole static semantic-claim authority:

```yaml
- kind: backend_type
  identifier: example
  name: Example Backend
  source: python-import
  target: example_provider.backend.runtime:ExampleBackend
  active: true
  meta:
    schema_version: 1
    version: 1.0.0
    tags:
      portable: true
      vector: true
      storage-roles: [vector]
      near-filter-modes: [combined_native]
```

Programmatic packages may write the same claims through the same record path:

```python
ExampleBackend.register(
    tags={
        "portable": True,
        "vector": True,
        "storage-roles": ["vector"],
    }
)
```

This is not a second inventory. Runtime inspection never falls back to class
attributes for static claims. Use `Backend.tag(...)` or `Backend.tags()` only
on a concrete instance when construction/live overlays matter.

### Thread and Mutation Boundary

- Guard mutable clients, schema/index maps, lazy initialization, and dirty state with
  the Backend lock or `@Backend.locked`.
- Return detached rows, not provider-owned mutable views.
- Preserve `RowOp` order unless one provider transaction makes a reorder observably
  failure-atomic.
- Treat an upsert `RowOp` as a whole replacement of the fragment, so a field
  missing from `values` is `None`. The workspace calls
  `Backend.validate_missing_row` for absent patch fragments and when entity
  inference, a create-only write, or a GraphEdge mutation depends on a row
  being absent. For absence one accepting route suffices, so a backend that
  accepts asserts that it keeps every row written to it. Pinecone rejects every
  miss before any route is written. Only the entity's oldest routes are asked:
  a route added by a replacement registration holds no row stored before it.
- Never share SQL session/transaction state across threads.
- Document cross-process locking or MVCC honestly; a Python lock is not a distributed
  transaction.

## 2. Reuse or Add a Placement Strategy

Prefer an existing strategy such as `hb.VectorIndex`, `hb.InlineColumn`, or
`hb.SideTable`:

```python
embedding = hb.field(hb.Vector[1536]).store(
    to="example",
    strategy=hb.VectorIndex,
)
```

Add a strategy only when the physical layout has distinct immutable semantics:

```python
class HnswIndex(hb.ext.StorageStrategy):
    identifier = "hnsw"


hb.ext.register_strategy(
    HnswIndex,
    tags={"filter": True, "near": True, "vector": True},
)
```

Strategy values may own immutable options. They must not hold clients, perform IO,
compile queries, or inspect a workspace. Default placement is expressed through
Registry-backed `StorageProfile` records under `placement/`, not through provider-name
branches in a resolver.

## 3. Publish Operations and Logical Types as Needed

Operations and logical types are independent open registered vocabularies.
Register a new operation once in `filters/`; query lowering and handler seeding
consume the same record and its exact tag claims:

```python
hb.ext.register_op(
    "semantic_match",
    aliases=("$semantic_match",),
    tags={"filter": True, "filter-domains": ["text"]},
)
```

Register a logical type's relevant claims, such as `filter-domains`,
`aggregate-modes`, `near`, or `nested-filter`, and its default
`StorageProfile` rather than adding type-name tests to query, placement, or
Backend code. These claims nominate candidates only. Missing or retired
executable records must fail even if an old implementation remains importable.

## 4. Add Provider-Native Handlers

A provider-local compiler receives field schema, validated value, and
`hb.ext.HandlerContext`, then returns `hb.ext.QueryFragment`:

```python
def compile_near(field_schema, value, ctx):
    vector = field_schema.dtype.validate(value)
    return hb.ext.QueryFragment(
        ctx.backend,
        "near",
        field_schema.name,
        {"vector": vector, "top_k": ctx.top_k, "metric": ctx.metric},
        notes={"kind": "example-near", "mode": "native"},
    )


def register_handlers(registry, ctx):
    for dtype in ctx.vector_types:
        hb.ext.Handler(
            dtype,
            "near",
            "example",
            hb.VectorIndex,
            compile_near,
        ).register(registry, replace=True)
```

Publish that seed callback as an ordinary `handler` record in the provider's
`meta.yaml`. `HandlerPlugin(...).register()` is the programmatic facade for the same
record shape.

Portable fallback compilers live under `execution/handlers/`. Provider-native
compilers stay with their provider or honest family owner. Neither path performs IO.
Portable candidates are seeded from named tag rules and compiled to exact
HandlerKeys. For example, vector-near seeding requires `portable=true` on the
Backend type plus `near=true` on the logical type, operation, and strategy;
provider-native callbacks may still select narrower strategy semantics when
their physical implementation requires them.

### Modes and Reasons

Every compiled fragment reports what it can actually do:

- `native`: provider pushdown with the required semantic proof;
- `py_scan`: portable row-payload evaluation;
- a documented combined mode only when execution truly depends on another fragment.

Provider reason callbacks populate the workspace HandlerRegistry's resolver-bound
fallback and unsupported catalogs during seeding. Handlers call
`ctx.fallback_reason(op, dtype, strategy=None)` or
`ctx.unsupported_reason(op, dtype, strategy=None)`; omitted strategy means
`ctx.strategy`. They do not import a process-global reason Registry. This keeps
same-key registrations in two Contexts isolated and keeps `explain()` tied to the
workspace that compiled the fragment.

Tag metadata is a candidate claim, not proof of native execution. For SQL text
and aggregate keys, an exact HandlerKey, compiler availability, provider
candidacy, and immutable physical-field evidence are all required. See
[text semantics boundaries](text-semantics-boundaries.md).

## 5. Declare and Inspect Tags

Tags use one open Registry-backed vocabulary. A subject may claim a tag without
defining it; a distributed `kind: tag` record optionally provides an inert
schema, default, intended subjects/layers, and description. Undefined claims
remain inspectable unresolved metadata instead of blocking publication.

An external provider should prefix a new definition when the name is not an
established shared convention:

```yaml
- kind: tag
  identifier: acme-batch-size
  name: Acme mutation batch size
  desc: Preferred provider mutation batch size.
  source: inline
  target: definition
  active: true
  meta:
    schema_version: 1
    version: 1.0.0
    definition:
      schema: {type: integer, minimum: 1}
      default: 100
      subjects: [backend_type]
      layers: [static]

- kind: backend_type
  identifier: acme
  name: Acme Backend
  source: python-import
  target: acme.backend:AcmeBackend
  active: true
  meta:
    schema_version: 1
    version: 1.0.0
    tags:
      portable: true
      acme-batch-size: 500
```

Static inspection is Context-local and import-free:

```python
result = context.modules().tag("backend_type", "acme", "acme-batch-size")
assert result.status == "resolved"
assert result.value == 500
```

Use `backend.tag(...)` / `backend.tags()` for construction and live overlays.
Neither API proves a native route. `hb.capabilities` and `ws.capabilities`
remain the read-only exact HandlerKey support surfaces; `explain()` reports
what a concrete query actually selected.

Provider-specific SQL and physical-database properties belong as tag claims on
co-located `sql_dialect` and `database_dialect` records. Exact aliases,
functions, reserved words, path rules, and type quirks remain ordinary dialect
definition data rather than central tables or tags.

## 6. Test the Contract

At minimum cover:

- typed construction through an explicit Context and resolver;
- `ensure`, ordered mutation, scan, delete, and detached return values;
- one truly native fragment and one portable/unsupported path;
- correct `handler_mode`, reason, and `explain()` output;
- placement validation and cross-Backend frame merge when relevant;
- two Contexts with the same registration key but different definitions;
- missing and retired registration behavior;
- module inspection without implementation import;
- the provider's thread-safety and conditional-write promises.

Optional dependencies use explicit test markers or dependency gates. Built-in and
external implementations should run the same contract suite.

## 7. Verify and Document

```bash
rtk uv run python scripts/gen_capabilities.py
rtk uv run python scripts/gen_capabilities.py --check
rtk bash scripts/test.bash tests/backends tests/execution tests/query -q
rtk bash scripts/flake.bash --ci
rtk uv run python scripts/docs.py check
```

Update the capability report when facts change and keep the provider README beside
its module folder when external authors need focused guidance. Do not put runtime
demos or generated provider data in the source package.

## Review Checklist

- The folder can move outside HeavenBase without a central source edit.
- The descriptor is inert and complete; inspection imports nothing.
- Context/resolver/config ownership is explicit from construction through handlers.
- No provider branch entered workspace, query, placement, capabilities, or shared
  execution code.
- Strategies remain immutable metadata; handlers remain pure compilers; Backends own
  IO.
- Native claims have behavioral parity evidence and portable fallback is honest.
- No retired import path or compatibility shim remains.
