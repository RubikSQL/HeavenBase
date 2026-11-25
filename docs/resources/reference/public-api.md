# Public API Tiers

HeavenBase keeps a broad root facade for notebooks, demos, and developer convenience, but the first-read API is intentionally smaller. Public examples should start with:

```python
import heavenbase as hb
```

Use the primary symbols below before reaching for support exports or `hb.ext`.

## Primary API

| Need | Start with | Notes |
| --- | --- | --- |
| Machine composition | `hb.Context`, `hb.DEFAULT_CONTEXT`, `hb.configure_explicit_context` | Pass `context=` to workspace, manifest, dashboard, and directory operations when isolation matters. Omission selects the process convenience Context, which is constructed on first use rather than on import; `heavenbase.context.set_default_context(context)` installs an explicit root Context as that default before first use, and `hb.configure_explicit_context()` (also `heavenbase.context.configure_explicit_context`) makes any use of the default raise instead. `hb.DEFAULT_CONTEXT` is an attribute, not part of `hb.__all__`. |
| Workspace lifecycle | `hb.HeavenBase`, `hb.WorkspaceManifest` | Ensure or load a durable workspace, construct a detached facade, replay manifests, register schemas, query rows, expose MCP, and search cards through the owning workspace. |
| Generic registration state | `hb.Registry` | Standalone snapshot/CAS utility for caller-owned namespaces. HeavenBase's reserved module catalog is accessed through `context.modules()`, not a raw Registry. |
| Entity authoring | `hb.Entity`, `hb.field`, logical types | Use `hb.ShortText`, `hb.LongText`, `hb.Float`, `hb.Json`, `hb.Vector`, and related logical types in field declarations. |
| Storage placement | `hb.InlineColumn`, `hb.JsonField`, `hb.SideTable`, `hb.VectorIndex`, `hb.InvertedIndex`, `hb.SparseGramIndex`, `hb.GraphEdge`, `hb.ExternalRef` | Use from `field(...).store(to=..., strategy=...)` when default placement is not enough. `GraphEdge` adds strong endpoint and target-delete semantics. |
| Query and exact support inspection | `hb.Query`, `ws.query(...)`, `ws.query_json(...)`, `ws.search(...)`, `hb.Catalog`, `hb.MetaSchema`, `hb.ResultFrame` | `Query` is an immutable spec-only value, `search` is free-text card discovery, and detached frames leave through explicit exports. Catalog / MetaSchema expose ordinary system rows; `hb.capabilities` and `ws.capabilities` inspect exact registered handler keys. |
| Module tag inspection | `context.modules().tag(...)`, `context.modules().tags(...)`, `Backend.tag(...)`, `Backend.tags()` | Static claims come only from installed records. Optional definitions provide typed values and defaults; a concrete Backend adds construction/live overlays. Tags nominate or describe candidates but never prove executable support. |
| LLM and prompts | `hb.LLM`, `hb.LLMSession`, `hb.Prompt`; `heavenbase.llm.LLMTraceEvent`, `heavenbase.llm.TraceCallback` | Preset-driven LLM calls, chat sessions, prompt rows, and translation-aware prompt rendering. `chat`, `achat`, `stream`, and `astream` accept a `trace=` observer of every provider call; see the Tracing section of the [LLM reference](llm.md#tracing). |
| Tooling and MCP | `ws.to_mcp(...)`, `ws.to_skill(...)`, `ws.serve(...)`, `hb.WorkspaceSkill`, `hb.Toolkit`, `hb.Tool` | Workspaces expose profile-scoped tools, can install a selected profile as a credential-free agent skill, and serve that same endpoint; standalone toolkits are developer-facing. |
| Optional extension rows | `hb.MemoryNote`, `hb.Message`, `hb.Session`, `hb.Skill`, `hb.Agent`; `heavenbase.extensions.agent_log.AgentTrace` | Classes import from the root, but rows require `ws.enable_extension("memory")` or `ws.enable_extension("agent")`. `AgentTrace` (the dashboard assistant session log's event row) imports from its extension package and needs `ws.enable_extension("agent-log")`, which the session log does for its own workspace. |

## Workspace Lifecycle

The two durable workspace entry points have distinct absence behavior:

```python
ws = hb.HeavenBase("shop", preset="debug")  # create or compatible reopen
same = hb.HeavenBase.load("shop")           # existing-only; KeyError when absent
ws.activate()                               # select explicitly
```

`HeavenBase(...)` is idempotent at the durable workspace identity: it creates
and registers an absent id, or opens an existing compatible definition. An
explicit `preset=` or `backends=` value asserts the persisted construction spec
and raises `FileExistsError` when it conflicts. Entity registration and later
MetaSchema evolution do not participate in this comparison. With neither
setting, an existing workspace uses its persisted construction spec rather than
reinterpreting current defaults. A preset and an expanded backend mapping are
different explicit construction forms; HeavenBase does not infer semantic
equivalence between them.

For ready Backend instances, compatibility compares the complete mapping from
workspace backend name to canonical Backend identifier and deliberately ignores
connection parameters and other instance details. `load()` reuses those
instances while the live facade exists. After release or process restart,
loading fails clearly until the owning Context again has compatible live
instances or the caller reconstructs with an explicit replacement.

`WorkspaceManifest` version 2 is a complete reconstructive workspace-shell
definition. Its top-level `construction` envelope is the single source of
Backend reconstruction truth: configured Backends isolate their complete map
under `{kind: "backends", config: ...}`, while live Backend objects record only
`{kind: "runtime", identifiers: {name: identifier}}`. Runtime identifiers may
reconnect matching live objects in the same Context, but never fabricate
connection config or clients. Manifest replay accepts `preset=` or `backends=`
as a complete construction replacement for replayable manifests. For a runtime
manifest, those arguments may supply different connection details, but their
complete name-to-identifier mapping must still match the recorded signature.
Legacy version-1 `config` input is normalized at read time; newly serialized
manifests emit version 2 without a parallel `config` field. See
[Workspace Manifests](workspace-manifest.md).

Pass an explicit Context to either public entry point when machine isolation
matters:

```python
ws = hb.HeavenBase("shop", preset="debug", context=context)
same = hb.HeavenBase.load("shop", context=context)
```

`HeavenBase.load()` without an id resolves the active workspace and then the
configured default. It never creates on a miss. Use `WorkspaceSpec.setup()` or
`hb setup` when an idempotent default bootstrap is intended.

Detached construction is part of the constructor rather than a parallel
temporary-workspace API:

```python
scratch = hb.HeavenBase(
    "scratch",
    backends={"main": {"type": "inmem"}},
    detached=True,
)
```

A detached workspace is caller-owned: it is not registered or discoverable by
`load()`, does not change selection, and cannot be activated. It is not
necessarily temporary or storage-isolated, and cleanup is not automatic. Call
`scratch.drop()` explicitly only when its Backend data should be destroyed.

## Entity Classes And Row CRUD

An Entity class must be present in a workspace before any ordinary row operation
targets it. Explicit `ws.register(Entity)`, workspace-manifest replay, extension
enablement, and fixed Catalog/MetaSchema plus trusted required-Capsule bootstrap
are safe schema lifecycle operations:
they establish the canonical workspace class, resolve its `StoragePlan`, ensure
physical schema, and publish inspectable `MetaSchema` rows.

CRUD is a separate instance lifecycle. A class, registered identifier, or
Entity instance supplied to `upsert`, `set`, `delete`, `get`, `read_rows`,
`iter_rows`, `exists`, `count`, `ids`, or `rows` is resolved against the
already-present canonical class. It never registers a class, creates a storage
plan, performs schema DDL, or publishes schema metadata as a side effect. An
unknown or structurally different class is rejected before row mutation.
Reads that name the entity (`get(..., entity=...)`, `get_many(..., entity=...)`,
`read_rows`, `iter_rows`) go straight to that entity's routed backends and
never consult Catalog; see [ID semantics](id-semantics.md#get-and-set).
`iter_rows` pages through `Backend.scan_page(entity_id, after=..., batch_size=...)`,
the keyset page primitive every bundled backend implements (natively on SQL,
in-memory, file and Milvus backends, through a portable default over `iter_scan`
elsewhere, which enumerates the route once per page; a provider without its own
`iter_scan` enumerates through one `scan()` call per page, and so does a subclass
of a native backend that overrides `scan` without its own `iter_scan` and
`scan_page`), so no backend lock is held between pages. Closing the iterator
before its end calls `Backend.end_scan(entity_id, after=...)`, so a provider can
drop what it kept for the next page.

MetaSchema itself has one deliberate expert-level exception. Writing an active
`kind="entity"` row whose `meta` value is a full Entity JSON definition
establishes that class through the same schema service when it is absent. The
caller-authored metadata row may be noncanonical or later diverge, and it never
replaces an already-present class. Inactive or non-definition rows are ordinary
metadata only. Prefer `register`, manifest replay, extension loading, or MCP
`define_entity` when consistency matters.

Root dynamic Extension Entity exports such as `hb.Agent` resolve through
`DEFAULT_CONTEXT`. Another Context or installed artifact generation may restore
a different Python class object for the same Entity identifier. Workspace row
operations validate identifier and structural schema, then use that
workspace's canonical class; cross-Context code should not treat Python class
`is` identity as durable. Source-backed compute and derive callables restored
under different private artifact namespaces compare by relative module,
qualified name, and source hash, so namespace churn is not a structural change
while changed executable source remains one. Catalog and MetaSchema are the
exact-class exception.

```python
ws.register(Product)
product_id = ws.upsert(Product, {"name": "Desk", "price": 89})
product = ws.get(product_id, entity=Product)
summary = ws.get(product_id, entity=Product, columns=["name", "price"])
```

`register` also fixes how much per-row maintenance an entity's writes carry.
`catalog="full"` (the default) projects, signs, and derives a Catalog row per
write and indexes its text. `catalog="identity"` publishes a Catalog row with
`object_id`, `target_id`, `target_entity`, `ws`, lifecycle fields, and field
defaults. In `catalog_mode="merge"` (the default), the stored Catalog row is
read by id and only its `created_at`, `active`, and `visible` carry over, as
under `full`. If the lookup finds a row, the write maintains the keyword index
and clears its postings, including postings added after the lookup. If the
lookup finds no row, or the caller uses `catalog_mode="fresh"`, the write
skips keyword maintenance and writes no keyword rows, but still deletes, in
the same transaction, any keyword rows of that Catalog row that a concurrent
writer committed in that window. `catalog="none"` publishes no
Catalog row, so those rows are absent from
`ws.query(hb.Catalog)`, `ws.search`, `Query.available()`, and the Catalog audit,
while `get`/`get_many`/`read_rows` still find them by `object_id` with or
without `entity=...` because reads go to the entity tables. `gram=False` stores
the entity's own `Array[ShortText]` fields inline without sparse-GRAM
maintenance and makes `contained_in` on them raise `LookupError` instead of
scanning. The choice lives in `ws.entity_options[entity_id]` as an
`EntityOptions` value, is exported with the entity definition in
`to_manifest()` (`catalog` and `gram` keys), and is published in the
MetaSchema entity row; a JSON entity definition may declare the same keys.
Re-registering with a different value needs `allow_replace=True`, like a
structural change. A structural replacement does not move stored rows: a route
it adds holds only the rows written after it, so it does not show that a row is
absent while the entity keeps an older route, and a route added back is read as
it was stored (see [ID semantics](id-semantics.md)). For options, a changed
`catalog` then rewrites or deletes the entity's
stored Catalog rows in bounded pages, and a changed `gram` drops or rebuilds
the field index from stored rows. When the entity has no live registration
(a fresh process on an existing store, typically a detached workspace), the
registration recorded in the store's MetaSchema entity row is the one being
changed: `None` inherits it, and a different `catalog` or `gram` needs
`allow_replace=True` just as a live change does (a `ValueError` otherwise,
so a typo at startup cannot rewrite a large store); with it the stored rows
are reconciled from that registration. An entity row written by a release
without registration options records the defaults; a store with no entity row
has nothing to reconcile. Reopening a manifest-backed workspace by id
(`HeavenBase(id)`, `HeavenBase.load(id)`, `WorkspaceSpec.open_detached()`)
replays the manifest saved in its `WorkspaceSpec`. When the tracked workspace
changes an entity that manifest defines (`register`, `alter_entity`,
`describe`), the saved definition is replaced with the live one as
`to_manifest()` exports it, so the workspace reopens with its new fields,
options and descriptions. A change to the JSON type of a field's default
also updates the saved definition. A change made through a detached or untracked
facade built from the same construction (its first registration of the entity
included, compared with the registry as its Context last read it) or through a
facade whose definition of the entity another process or Context has since
replaced is not saved and emits a `UserWarning`. A definition a reopen could not replay
(a value with no saved JSON form or an unregistered compute hook) is not saved either and
emits a `UserWarning`. The saved entry receives the current `catalog` and
`gram` options when its replay would otherwise disagree with the store. See
[Saved Replay Manifest](workspace-manifest.md#saved-replay-manifest). An
explicit `from_manifest(...)` call compares its own definitions with the
stored registration as above. `alter_entity` keeps the registered options.
Omit `catalog` or `gram` from the JSON definition, or pass JSON `null`, to
inherit that option. A changed option raises `ValueError`. A non-null,
non-Boolean `gram` value raises `TypeError`.
An entity an enabled extension registered (such as `memory-note`) keeps the
extension's definition: `alter_entity`, `describe`, and `register` (with or
without `allow_replace=True`) raise `ValueError` when they would change its
structure, `catalog` or `gram` option, or metadata (name, descriptions,
tags and examples). A call that changes
nothing is accepted and leaves the extension's class registered, including
the definition `to_manifest(include_system=True)` exports for an entity with
derive or compute hooks.
When leaving `full`, Catalog and MetaSchema writes that could commit
independently wait for any staged GRAM transactions to commit. A Catalog
rewrite in the shared registration transaction rolls back if that
transaction fails. Catalog pages that already committed stay in their
new form. Rewrites into `full` follow publication. A live change from
`full` to `identity` materializes source rows before the GRAM transactions
open so that entity derives run outside those transactions. A page
failure raises `RuntimeError` naming the mode that remains in effect.
`ws.audit()` lists rows that differ from that mode, and `ws.repair()` rewrites
them. A tracked workspace saves the committed definition before a
post-publication rewrite, subject to the saved-manifest rules above, so
reopening by id uses the committed mode even if that rewrite failed.
On a tracked manifest replacement, a committed option change updates the
existing saved entity definition before replay continues.
After reopening a workspace, changing from `catalog="none"` checks the
stored routes for IDs that Catalog cannot hold before publishing the
incoming schema. When reopening with a matching definition, Catalog-mode
changes can reattach the stored registration without republishing MetaSchema.
When leaving `catalog="full"` with a different definition, registration
captures IDs from the stored routes before preparing the replacement. If
a failed rewrite leaves the previous definition unattached, register it
before running `ws.audit()` or `ws.repair()`. When both options
change on reopen, the incoming GRAM placement is validated before
reattachment. A GRAM-only option
change applies directly. If it fails, reattachment requires a definition
that matches the complete stored projection, including indexes, constraints
and the JSON types of defaults, and a compatible index. Successful
reattachment reuses the existing MetaSchema rows. If reattachment fails,
register the previous entity definition with its stored options first.

Registration reads current MetaSchema rows. Routes that inherit portable
point lookup can use a native key filter when the backend supports one.
Other routes use an exhaustive read. Schema publications record the IDs
in their projection and remove rows for dropped fields, bindings, indexes
and constraints. The recorded IDs allow later registration to fetch the
projection directly.

SQL sparse-GRAM rebuilds use staged transactions. One
registration can change existing indexes on several SQL databases. Transactions
that change an existing index fingerprint or update legacy index retirement
state commit after the other staged SQL transactions.
A failure rolls back open transactions and restores local bindings. Across
independent transactions, committed index changes can remain after a later
commit fails. On SQLite and in-memory storage, replacement recovery
conditionally restores the MetaSchema rows still holding the failed
publication's values. A later edit to a row is preserved. Recovery emits a
`RuntimeWarning` if it needs to restore rows and the backend cannot apply
the restore safely, or if the restore fails. In the same workspace, retry the intended definition with
`allow_replace=True`. In a reopened
workspace, a Catalog-mode change can encounter incompatible index options
while reattaching the stored registration. If this happens, first register the
previous entity definition with its stored Catalog mode and GRAM option, then
retry the intended definition with `allow_replace=True`.
Unused indexes are released after publication. A field still indexed
through another route to the same SQLite file or PostgreSQL index keeps
its shared index. A mutation through a registration that lacks a binding
for a currently indexed field is refused, except on an Oracle engine at
`SERIALIZABLE` for a write whose transaction began before another workspace
started indexing the entity (write at `READ COMMITTED` there while indexes
change), and on a MySQL engine at `READ COMMITTED` for a write that meets the
first index of its entity. `pgvector` and `starrocks` routes,
and engines that may commit every statement, keep no keyword index and scan
for `contained_in`; their writes are checked the same way, also against an
index another workspace starts after the route registered the entity. On
the `sqlite`, `duckdb`, `postgres`, `pgvector`, `mysql`, `dolt`, `seekdb`,
`oceanbase` and `oracle` backend types the check rides on the write's own row
statements while the stored index state is unchanged, on a route that may
commit every statement on its first row statement only (see the sparse-GRAM
backend architecture). If releasing an index
after the registration is published, or re-establishing the previous
registration after a failed one, fails as well, a `RuntimeWarning` names the
backend: an index the live registration does not bind may stay maintained,
and a scalar index the failed registration dropped may stay missing, until
the entity is registered again with `allow_replace=True` or in a new
process. `ws.audit()`
compares Catalog rows owned by the workspace with the rows its current registrations expect
(missing, dangling, mismatched). On a SQL Catalog route that keeps a keyword
index it also compares the sparse-GRAM keyword and posting rows of each Catalog
key in its scope with the rows the stored Catalog row indexes, and lists a key
whose rows differ, or that has keyword rows without a stored row, under
`stale_keywords`; `repair()` writes such a row again with keyword maintenance
(`stale_keywords_rewritten`). For that check the audit reads every Catalog row in
its scope again with all of its keyword and posting rows and indexes it once
more: on a `full` Catalog of 4,000 rows with three keywords each, `ws.audit()`
took 2.6 to 2.9 times as long as without the check (about 10 seconds instead of
4 on SQLite, PostgreSQL and MySQL), and `repair()` audits before and after. The row writes and deletes that `repair()`
issues maintain the keyword rows, and Catalog deletes keep that maintenance
whatever the mode.

On shared storage, the stored `Catalog.ws` field determines ownership for audit,
repair, and Catalog mode changes. These operations skip Catalog keys occupied
by rows whose `ws` differs from the workspace id, including empty values.
Repair and mode changes check ownership at mutation time. When rows need to
change, each Catalog route must store `ws` and support ownership-guarded
mutations. In-memory, transactional SQL, workspace-scoped File, and TinyDB with
workspace-scoped JSON storage provide this operation. Other backend
configurations need an implementation of `Backend.upsert_owned` before repair
or a mode change can mutate their Catalog rows.

```python
ws.register(Run, catalog="identity", gram=False)
ws.upsert_many(Run, rows, atomic=True)  # one backend write call; one transaction on SQL
```

`upsert(..., atomic=True)` and `upsert_many(..., atomic=True)` commit the
entity rows and their Catalog rows through one backend write call, so they all
land or none does; on a SQL backend that accepts it, that call is one
transaction. Every routed write must land on one backend that claims the
`atomic-upsert` tag: `sqlite`, `duckdb`, `postgres`, `supabase`, `mysql`,
`seekdb`, `dolt`, `oceanbase`, `mssql`, `oracle`, `pgvector`, `surreal`,
`neo4j` and `inmem`. Every other backend makes no claim: the generic `sql`
type, `clickhouse`, `starrocks`, `trino`, the `file`, `json` and `pickle`
backends, and the other vector providers, such as Chroma, which sends a batch
larger than the client's `get_max_batch_size()` (5,461 on chromadb 1.5.9) as
several upsert calls that each commit on their own. Neither does a SQL backend
whose engine may commit every statement: SQLAlchemy
`isolation_level="AUTOCOMMIT"`, given directly or through `execution_options`,
or a DBAPI connection that may open in autocommit mode. The latter means an
`autocommit` connect argument, from `params` or `connect_args`, whose value the
driver receives as true in Python, or as `None`, which leaves the mode to the
driver or server default (MySQL's is autocommit); `params` reach pymysql and
pymssql as strings, so any non-empty value, even `"false"`, turns autocommit
on. For `sqlite3` it means `autocommit=True`, or `isolation_level=None` in its
default legacy mode. A `pragmas` statement that names `autocommit` or
`implicit_transactions`, in any case, may switch the mode after the driver set
it, so it withdraws the claim too. The claim relies on the database's transactions. On a MySQL-protocol
server each table names its own storage engine, so a backend withdraws its claim
when it registers a table, keyword-index tables included, whose engine keeps no
transactions: one that `information_schema.ENGINES` lists without transactions,
such as MyISAM, MEMORY or Aria. From then on `atomic=True` through that backend
raises `ValueError`. An engine changed with `ALTER TABLE` after the table was
registered is not seen until the entity is registered again. A plan that
spans backends, or whose backend makes no claim, raises `ValueError` before
writing anything, so a refused MetaSchema row establishes no Entity either. The
guarantee covers the rows of the call only: a definition-shaped MetaSchema row
registers its Entity class, creating its storage and publishing its own
MetaSchema row, before the write, and a failed write does not undo that
registration.
Without `atomic`, entity rows commit before Catalog rows in separate calls.
`upsert(entity, row=None, **values)` reads the keywords `overwrite` (default
`True`) and `atomic` (default `False`) as write flags, each a bool, unless the
row's Entity class declares a field of that name: the keyword is then that
field's value, and such an entity takes the flag through `upsert_many`.
`get` and `get_many` accept `columns=` together with `entity=`. The row carries
`object_id` plus the named declared fields only. The `SQLBackend.get_many`
implementation selects those fields in storage, including when a third-party
backend inherits it. That avoids fetching or decoding unrequested fields such
as vectors. HeavenBase passes the selection to backend methods that explicitly
accept `columns`. When a method omits that parameter, HeavenBase calls it with
its original arguments and trims the returned fragments before decoding.
`columns` is a sequence of names: a bare string raises `TypeError` rather than
selecting one field per character, and the selection is checked even when
`get_many` receives no ids.

SQL text queries use portable Python comparison semantics by default and may
scan when the concrete physical field lacks parity proof. When throughput is
more important than cross-provider equality, make the tradeoff query-scoped and
auditable:

```python
rows = ws.query(Product).where(Product.name.match("desk")).unsafe().execute().rows()
rows = ws.query_json(Product, {"filter": {"name": {"$match": "desk"}}, "unsafe": True}).execute().rows()
```

The frozen unsafe policy authorizes documented result-relaxing query
optimizations. It currently bypasses portable text proof and static
collation/case guards for an available compiler; it does not bypass validation,
security, parameterization, or resource controls. Provider collation, padding,
pattern, regex, and bounded-candidate behavior may change results. Inspect
`explain()` fields `unsafe`, `unsafe_reasons`, and `portable_proof` when
portability matters.

## Query Values And Result Exits

Every fluent query transform returns a new immutable `hb.Query`; no transform
mutates its source, so assignment is the branching mechanism and there is no
builder, `copy`, or `fork` API. `explain()` and `execute()` may build temporary
routing artifacts, but the query retains only request data.

Every `execute()` recomputes and returns a newly detached `hb.ResultFrame`.
Results are never persisted or cached automatically. Treat the frame as a
transport boundary: explicitly call `rows()`, `scalar()`, `to_pandas()`,
`to_pyarrow()`, `to_numpy()`, `to_pydantic()`, or `to_daft()` rather than
printing or implicitly coercing it.

`count(field="object_id", distinct=True)` is source-only and terminal. Only
`where`, `near`, `traverse`, and `unsafe` may precede it, and only `explain` and
`execute` may follow it:

```python
total = ws.query(Product).where(Product.name == "Desk").count().execute().scalar()
```

See
[Query Values and Result Exports](../architecture/query-values-and-results.md)
for the complete lifecycle contract.

## Function-Like Tool Construction

`hb.Capsule`, `hb.Tool`, and `hb.Prompt` accept callables directly and support
configured decorator construction. `Tool` also accepts an existing Capsule and
remains callable with the wrapped function's signature:

```python
import heavenbase as hb


@hb.Tool
def add(left: int, right: int) -> int:
    """Add two integers."""
    return left + right


@hb.Tool(name="times")
def multiply(left: int, right: int) -> int:
    """Multiply two integers."""
    return left * right


capsule = hb.Capsule(add, name="captured-add")
wrapped = hb.Tool(capsule, name="plus")

assert add(2, 3) == 5
assert multiply(left=2, right=3) == 6
assert wrapped(left=2, right=3) == 5
```

`Toolkit.add(...)` accepts any of these Tool values, a Capsule, or a plain
callable. Toolkit execution keeps keyword-only agent/MCP argument dispatch even
though an individual Tool behaves like its wrapped Python function.

Concrete Toolkit persistence is workspace-owned:

```python
ws = hb.HeavenBase("application")
toolkit = hb.Toolkit("math", [add, multiply])

toolkit_id = toolkit.register(ws=ws)
toolkit_row = ws.get(toolkit_id, entity=hb.Toolkit)
loaded = hb.Toolkit.load(toolkit_id, ws=ws)

assert toolkit_row["object_id"] == toolkit_id
assert loaded.run("add", left=2, right=3) == 5
```

The selected workspace stores both the `sys-toolkit` row and every referenced
`sys-capsule` row. `Toolkit.list`, `Toolkit.describe`, and `Toolkit.delete`
accept the same `ws=` argument. Omission uses existing-only
`hb.HeavenBase.load()`; it never creates a workspace or falls back to a
process-global Toolkit store.

Context ownership is explicit when requested:

```python
import heavenbase as hb

context = hb.Context.load(home_path="bootstrap.yaml")
workspace = hb.HeavenBase("shop", preset="debug", context=context)

module_rows = context.modules().catalog.inspect("backend_type")
vector_claim = context.modules().tag("backend_type", "inmem", "vector")
assert vector_claim.value is True
```

Every nested Registry-backed lookup inherits `workspace.context` and its
`ModuleResolver`. `hb.DEFAULT_CONTEXT` is the exact owner of `hb.CM_HVNB`; it is a
convenience for unbound calls, not a hidden authority inside an explicitly bound
workspace.

## Support Exports

The root facade also exports useful support objects such as `hb.Context`,
`hb.Registry`, `hb.backends`, `hb.capabilities`, `hb.Database`, lazy interop helpers
like `hb.from_pandas(...)`, and the standalone configuration surface:
`hb.ConfigManager`, `hb.ConfigSnapshot`, `hb.ConfigEngine`, `hb.ConfigSpec`, and
`hb.InterpolationPolicy`. Use these when building integrations, provider checks,
application settings, or data import flows. Runtime-heavy names are lazy attributes,
including `hb.HeavenBase`, `hb.WorkspaceManifest`, `hb.BackendType`, `hb.backends`,
`hb.capabilities`, `hb.Database`, `hb.LLM`, `hb.Agent`, `hb.Toolkit`,
`hb.from_pandas`, and `hb.ext`; this preserves the root API while keeping
`import heavenbase` light. The `hb.backends` facade lazy-loads concrete provider
classes, so resolving the package is cheap and resolving a provider class imports only
its cohesive provider family. Every HeavenBase package facade loads the same way:
importing a package imports none of its submodules, and `package.submodule` imports
that submodule on first access. Low-level SQL configuration and execution have the
explicit `heavenbase.database` owner. The complete LLM domain has the explicit
`heavenbase.llm` owner, including resolver-backed gateway modules. There are no
secondary owners at `heavenbase.utils.db`, `heavenbase.utils.dialect`, or
`heavenbase.utils.llm`. Prefer Entity-owned import staging such as
`hb.Entity.from_pandas(...)` in new examples.

Prompt-independent text composition and response parsing stay below the root
facade. Use `heavenbase.utils.strings.fast_prompt_section(...)` to assemble
message dictionaries, and use `heavenbase.utils.parsers.parse_md(...)` or
`parse_keys(...)` to recover tagged, fenced, or line-oriented values. The three
functions are also lazy exports from `heavenbase.utils`; they are not
`heavenbase` root or prompt-extension exports.

Backend registry contracts, neutral execution fragments, handler protocols, placement
profiles, MCP profiles, serializers, GRAM normalizer/tokenizer registration, and
extension registration live under `hb.ext`. Static semantic claims are authored as
the subject record's `meta.tags`; programmatic registration writes that same record
shape. Use `hb.ext` for extension authorship and Backend/provider work, not
first-user onboarding. The module itself is import-light;
provider, handler, workspace, toolkit, and optional-extension factories resolve on
attribute access. Advanced direct imports follow the owning packages:
`heavenbase.placement`, `heavenbase.execution`, `heavenbase.capabilities`,
`heavenbase.database`, `heavenbase.llm`, `heavenbase.backends`,
`heavenbase.modules`, and `heavenbase.context`. The retired
`heavenbase.registry`, `heavenbase.storage`, `heavenbase.frame`,
`heavenbase.handlers`, and `heavenbase.discovery` packages are not compatibility
surfaces.

Parser-neutral command specifications and the Typer, Click, and argparse
compilers consume `heavenbase.cli.CLIContext`. Applications implement its
`package`, `version`, and `fail(...)` contract; `PackageCLIContext` remains the
HeavenBase-specific adapter with configuration, output, and workspace services.
`heavenbase.cli.OptionSpec` carries the callback destination, public flags,
value type, default, repeated-value behavior, Boolean/negatable behavior, and a
backend-neutral `hidden` flag. A hidden option remains parseable but is omitted
from Typer, Click, and argparse help output. Pass a caller-owned `CLIRegistry`
to `create_cli(..., registry=...)` when composing an application command tree.

Owner-local resource descriptors are imported from
`heavenbase.database.resources.sql` and `heavenbase.toolkit.prompts`. The former
central `heavenbase.resources.sql` and `heavenbase.resources.prompts` paths are
retired without compatibility shims in the `0.1.2.1` architecture-convergence
train.

## GraphEdge Lifecycle

Use `GraphEdge` when an edge endpoint must remain a valid object under mutations serialized through one
live workspace rather than a weak identifier:

```python
import heavenbase as hb


class Doc(hb.Entity):
    identifier = "doc"

    title = hb.field(hb.ShortText)
    cites = hb.field(
        hb.HyperG["cited": hb.Identifier["doc"]]
    ).store(
        strategy=hb.GraphEdge(target="cited", on_delete="restrict")
    )
```

`restrict` is the default, so `on_delete="restrict"` may be omitted. `on_delete="detach"` declares that
deleting a target removes matching edge mini-rows from surviving sources first. Both policies are
backend-independent: Neo4j relationships, SurrealDB's canonical inline value, and ordinary row-store
lists expose the same logical result. SurrealDB also maintains `RELATE` records as a derived shadow
projection; public traversal and delete discovery do not trust that projection without a future
versioned readiness/rebuild contract. Use an ordinary `Identifier` or non-`GraphEdge` `HyperG` field
when the domain intentionally permits weak references.

GraphEdge mini-rows form an endpoint-keyed relationship set: one edge per `(source, field, target)`.
List order is not semantic, although each edge's scalar properties are. Native record-graph backends may
maintain a typed, id-only target anchor after logical validation when the target's authoritative fields
live on another route. Under an unchanged coherent `StoragePlan`, anchors are physical projections only:
they do not affect `get`, `exists`, `count`, hydration, or Catalog rows. An anchor-capable backend must
merge with rather than replace a co-located full row and accept idempotent anchor deletion for target
entities it has not ensured. Route migrations must rewrite or remove obsolete anchors before the new
plan is considered coherent.

A workspace upsert or set accepts a `GraphEdge` only when its endpoint already exists or is created in
the same accepted logical batch. Missing endpoints reject the entire batch before entity/Catalog row
mutation. The source and endpoint Entity classes, storage plans, and physical
schema must already exist before the row operation starts; a rejected mutation
does not register either class. `upsert` replaces the
source field's complete outgoing edge list; `set` is the patch verb.

Target deletion has one policy parameter:

| Call | Incoming-edge behavior |
| --- | --- |
| `ws.delete(target)` | Uses each field's schema policy (`edges="schema"`). |
| `ws.delete(target, edges="restrict")` | Rejects if any surviving source points to the target. |
| `ws.delete(target, edges="detach")` | Removes every matching edge from surviving sources, then deletes the target. |
| `ws.delete_many(targets, edges=...)` | Applies the selected policy to one logical batch and preserves caller-order boolean results. |

Restriction raises `hb.GraphDeleteRestrictedError`. Its `references` list contains deterministic
`entity`, `field`, `source`, `target_entity`, and `target` records, and no entity/Catalog row in the
restricted batch is mutated. In schema mode, any restricted incoming field aborts the whole batch before
detach rewrites are applied. Detach removes relationships, not neighbouring objects; it is deliberately
not called cascade.

Outgoing edges belong to the source row. If that source is also being deleted, its outgoing edges,
self-loops, and batch-internal cycle edges disappear with it and do not block `delete_many`. Missing
targets remain false/no-op delete results. A missing target may still exist when none of its entity's
oldest routes can show that it is absent, as when they are all on Pinecone, whose fetch miss does not
show it. Its incoming references are then restricted or detached as for a present target; the delete
returns `False` for it, but still deletes its record and its Catalog row. Confirmed repair of such a
target raises `ValueError`, because it would
remove edges to a target that may exist. After detach, recreating the same target id does not resurrect
an old edge.

`ws.check_graph()` detects logical edge values whose targets are missing, and
`ws.scrub_dangling(confirm=True)` removes those values. A field argument selects source rows through that
field; confirmed repair removes all dangling GraphEdges on each selected row. These tools cover legacy
data, direct target removal, and mergeable dangling rows after an apply error. They do not replay failed
intent, choose between conflicting physical replicas, or detect every native-projection mismatch while a
target still exists. After a multi-backend apply error, quiesce writers and converge declared routes with
one authoritative valid full-row upsert before retrying audit/delete. Successful calls serialized through
one live workspace maintain the strong-reference contract in the mutation's affected closure when they
start from a coherent logical pre-state; an unrelated successful call does not repair legacy corruption
elsewhere. Independently routed backends do not form a distributed transaction, and another process can
race endpoint preflight and commit unless the provider supplies stronger serialization.

The durable contract is [ADR 0005](../architecture/adr/0005-graph-edge-capability.md); current conformance evidence lives in the GraphEdge lifecycle, backend, MCP, audit, and repair tests.

## Keyword Search Surface

`hb.Array[hb.ShortText]` fields route to `SparseGramIndex` by default when a SQL backend is available. Use `FieldRef.contained_in(...)` for reverse containment:

```python
rows = (
    ws.query(hb.Catalog)
    .where(hb.Catalog.keywords.contained_in("the request mentioned Redis Query Engine"))
    .select("target_id", "match")
    .execute()
    .rows()
)
```

The JSON alias is `"$contained_in"`. `hb.SparseGramIndex` is the storage strategy value; SQL backends host the default `sql_sparse_gram` adapter for `Array[ShortText]` reverse-containment search. An entity registered with `gram=False` keeps no such index, and `contained_in` on its `Array[ShortText]` fields raises `LookupError` (the explain step reports the `unsupported_reason`) rather than scanning. `match` is a reserved query-output metadata column carrying keyword, span, matched query text, normalizer, and precision metadata when the backend or scan fallback can compute it. Match evidence is preserved rather than collapsed: overlapping, containing, or repeated keyword entries can all appear in the `match` list, while result rows are still merged by `object_id`. Result metadata merging is generic at the `ResultFrame` layer: backends and handlers mark metadata columns, and list-valued metadata is concatenated during row merges. Sparse-GRAM normalizers and tokenizers resolve through built-in registrations, `hb.ext.register_gram_normalizer(...)` / `hb.ext.register_gram_tokenizer(...)`, or direct live callables for non-manifest use. CRC32 utility helpers are available as `hb.utils.crc32int` and `hb.utils.crc32hash`; configurable integer hashes use `hb.utils.hash_int` and `hb.utils.resolve_hash_int`.

## Non-Goals

- Do not add a second "simple API" package or compatibility facade.
- Do not duplicate `hb.ext` registry seams at the root.
- Do not remove support exports only to shrink `__all__`; tier the docs first and remove exports only when a concrete misuse or maintenance cost appears.
