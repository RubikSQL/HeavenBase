# Data Flows

Every flow below is scoped to `workspace.context`. Registry-backed type, strategy,
handler, capability, dialect, extension, and interop lookups inherit that Context's
`ModuleResolver`; only a public call that omits Context/resolver uses
`DEFAULT_CONTEXT`.

## Schema Definition

```text
Entity definition
  -> ws.register(...) / manifest replay / extension enablement
     / active definition-shaped Entity-kind MetaSchema command
  -> canonical workspace Entity class
  -> EntityMeta
  -> EntitySchema
  -> placement.StorageResolver
  -> StoragePlan
  -> Backend.ensure_plan (adds storage and strategy state, reconciles scalar indexes)
  -> registration switch
  -> MetaSchema rows
  -> Backend.prune (releases strategy state the published registration no longer binds)
     (On failure before publication: roll back staged SQL keyword rows and
      restore the previous storage plan. Backend.abandon releases strategy
      state specific to the failed registration.)
```

Schema lifecycle operations establish Entity classes before ordinary row IO.
Registration writes entity, field, workspace, backend, capability, and storage
placement metadata rows to `MetaSchema`. Manifest replay, extension enablement,
and an active Entity-kind MetaSchema row whose `meta` value contains a full
Entity definition reach the same registration service when that class is
absent. The manual row is written separately and may use a noncanonical id or
search fields; if a class is already present, the row never replaces or alters
it. Inactive, deleted, field-kind, storage-kind, and annotation-only rows do not
change class or placement state.

This exception makes MetaSchema a permissive command ingress without making its
stored rows an alternate runtime catalog. Rich schemas and changes should use
`register`, manifest replay, extension enablement, or guarded schema APIs.

Concrete user-object writes maintain instance rows in `Catalog`. They do not
register an Entity class, resolve a new `StoragePlan`, call `Backend.ensure`, or
publish schema metadata.

## Write

```text
ws.upsert(Entity, row)
  -> require the already-present canonical Entity class
  -> defaults
  -> native derives/compute hooks
  -> object_id
  -> validate()
  -> GraphEdge endpoint preflight
  -> encode()
  -> RowOp
  -> Backend.upsert
  -> Catalog row
```

An Entity class, registered identifier, or Entity instance supplied to CRUD is
only a type selector and/or row value. HeavenBase resolves its entity id to the
canonical class already owned by the workspace and rejects an absent or
structurally different class before row mutation.

Native derives and field compute hooks run before type validation when the target field is omitted or `None` (`""` also triggers an `object_id` derive); an explicit non-null value in the input row skips the compute hook, so callers can precompute network-bound values such as embeddings before taking the workspace write lock. When a row omits `object_id`, HeavenBase computes it from an `object_id` compute hook, an explicit non-blank `catalog.name` derive, or a non-blank native `name` field. Catalog projection runs after the entity row is validated, applies generated timestamps, workspace signing defaults, same-name projections whose logical type descriptors match, then explicit Catalog derives. An incompatible native `name` can still supply fallback identity but leaves the Catalog name empty unless an explicit `catalog.name` derive normalizes it; other incompatible same-named application fields likewise leave the Catalog default unchanged. Each entity field is normalised with `LogicalType.validate()`, then encoded with `LogicalType.encode()` before the backend receives the row fragment.

The Catalog step follows the entity's registration options (`ws.entity_options`). Under `catalog_mode="merge"` the writer reads the stored Catalog rows of the batch by id in one lookup per Catalog route, so `created_at`, `active`, and `visible` carry over in both modes. With `catalog="full"` it projects a new row and the sparse-GRAM adapter indexes its text. With `catalog="identity"` it builds the row from identity, those lifecycle fields, and defaults alone, merges nothing else, and marks its `RowOp` with `strategies=False`, so strategy adapters do not run for it, unless the stored row it replaces holds different values in a field an adapter maintains (a direct Catalog write, or a writer still registered as `full`), in which case maintenance runs and removes the keyword rows derived from them; a Catalog delete keeps `strategies=True` whatever the mode, so a keyword row can never outlive its Catalog row. With `catalog="none"` the step is absent. Entity rows commit before Catalog rows in separate `Backend.upsert` calls unless the caller passes `atomic=True`, which sends both to the single routed backend in one call and is refused unless that backend claims the `atomic-upsert` tag. An entity registered with `gram=False` has its `Array[ShortText]` fields placed inline instead of on `SparseGramIndex`, so no keyword rows are written for them and `contained_in` on them is refused at compile time.

### GraphEdge Write

```text
materialized logical batch
  -> resolve GraphEdge fields and endpoint entities
  -> collect distinct endpoints by target entity
  -> satisfy endpoints present in the same batch
  -> exists_many once per remaining target entity
  -> reject the complete batch or route backend RowOps
```

Within one live workspace's mutation boundary, `GraphEdge` is a strong reference on graph and non-graph
stores alike. Every endpoint must already exist or be created in the same accepted logical batch.
Missing endpoints and parallel edges fail before entity/Catalog row mutation, so a provider cannot
create a logical placeholder or retain a partial source row to weaken the contract. A native record-graph
backend may then materialize a typed, id-only endpoint anchor as a non-authoritative physical projection;
under an unchanged coherent `StoragePlan`, anchors never participate in logical existence or hydration.
An `identity_anchor` backend must merge rather than replace co-located rows and accept idempotent anchor
deletes without prior target `ensure`. Schema registration and physical `ensure`
must already have completed before the row mutation begins. `upsert` replaces a source field's complete outgoing edge set; `set` keeps its
normal patch semantics. GraphEdge identity is `(source, field, target)`, so mini-row order is not semantic.

### GraphEdge Delete

```text
ws.delete / ws.delete_many
  -> workspace lock
  -> normalize targets and caller-order existence flags
  -> resolve incoming GraphEdge fields and schema policies
  -> discover candidate sources through complete native incoming indexes where available
  -> otherwise exhaustively scan every physical owner of the logical GraphEdge once per source entity
  -> hydrate candidate source ids across every declared row route
  -> raise one restriction error, or compile source detach rewrites
  -> group source rewrites, Catalog changes, and target deletes by backend
  -> one mutation batch per backend
```

The default `edges="schema"` mode uses each incoming field's `GraphEdge.on_delete` policy. That policy
defaults to `restrict`; `detach` removes matching mini-rows from surviving sources before target
deletion. `edges="restrict"` and `edges="detach"` explicitly override every incoming field for one
delete call. A restricted batch raises `hb.GraphDeleteRestrictedError` with deterministic reference
records and performs no entity/Catalog row mutations.

Outgoing edges belong to their source. References whose source is also in the `delete_many` target set,
including self-loops and cycles wholly inside the batch, disappear with that source and do not restrict
the batch. Detach removes relationship values only; it never deletes neighbouring nodes. Duplicate
targets are planned once while return booleans remain aligned with caller order, and missing targets
remain false/no-op results.

From a coherent logical pre-state, after a successful delete serialized through the same live workspace,
logical reads, edge filters, and traversal agree throughout the affected mutation closure; recreating a
target cannot resurrect an old edge. Provider projections touched by that mutation are synchronized or
removed in the same provider batch. Unrelated legacy corruption is not repaired by an unrelated call.
`ws.check_graph()` detects logical edge values whose targets are missing, and confirmed
`ws.scrub_dangling(...)` removes those values. A field-scoped scrub selects affected source rows and
removes every dangling GraphEdge on each selected row so the replacement remains strongly valid. These
commands support migration, legacy rows, direct target removal, and mergeable dangling rows after an
apply error; they neither choose between conflicting physical replicas nor detect every provider
projection drift.

`Backend.scan(entity_id)` means exhaustive enumeration, not a best-effort page. Elasticsearch consumes
the complete scroll and Milvus consumes its query iterator; a backend that cannot enumerate raises rather
than letting an integrity absence proof use truncated data. SurrealDB's inline GraphEdge is canonical, so
workspace traversal and incoming-edge discovery use that exhaustive path. Its `RELATE` data is a derived
shadow until a future versioned readiness/rebuild seam can prove it safe for public native routing.
A readiness-trusted native traversal is used only for a GraphEdge field with one physical owner.
Replicated fields use portable exhaustive merge even when one owner is native, so divergent copies fail
closed instead of being hidden behind the native projection; query `explain()` reports that fallback.

The workspace lock is a reader-writer lock shared by every facade of one workspace id in one Context
tree (a detached facade has its own). A public mutation made outside any other HeavenBase call (an
outer write) first waits, in arrival order behind other outer writers of the same workspace and
without holding the Context lock, for the workspace's readers that were already running. It then
takes the Context lock and the exclusive side, so one writer per Context runs at a time. A write
nested in another HeavenBase call (a compute hook, or a write inside a read) skips that queue: it
takes the Context lock first and then waits for the workspace's running readers, so a long read can
then hold up writers of every workspace of the Context. Public reads share the lock, run
concurrently with each other and with writers of other workspaces, and never take the Context lock.
While a writer waits for its readers, new reads of its workspace are held back only when no other
thread holds the Context lock, and then only for one waiting writer per Context at a time, so a write
of another workspace never delays them. A held-back read enters at the latest once the waiting write
completes, before the next queued writer of the workspace. If reads that entered meanwhile are still
running when the writer gets the Context lock, it lets them finish without the lock once more, and
after that waits for them holding it, so a long read that entered meanwhile then holds up writers of
every workspace of the Context. Reads and writes may nest
across workspaces, for example a compute hook that reads another workspace or embeds through a cached
`LLM`: a writer does not wait for a reader that is itself waiting for the Context lock or for a
workspace the writer holds, and such a reader resumes only after that write, so its view is not a snapshot across the wait. A reader may also write the
workspace it reads: that write is nested, so it takes the Context lock and then waits for the
workspace's other running readers, not for its own read. Two readers that both write the workspace
write one after the other: the one that waits for the Context lock is parked behind the other, and it
writes once the other one's write and the rest of its read have ended. An exception that a signal
handler raises during a call (Ctrl-C, an alarm) leaves neither the Context lock nor a workspace lock
held: the call gives back what it has taken before the exception reaches its caller. Only exceptions
in such quick succession that the call still handles the first one can leave one of them held: a
second one on Python 3.10, as with any Python lock, and a third one on later versions. On Python 3.13
the interpreter itself can skip the exit of a `with` or `finally` when the exception arrives at the
end of a pass of a conditional loop, as in 0.1.2.2.
Context close and `reset_default_local_state()` called by a thread that holds no Context or
workspace lock wait, for at most 30 seconds in all, for work of other threads that they must not
overlap. They first wait for a hold of the Context lock (such as a write). While they wait for it, the
lock is reserved for them, so they get it as soon as its current holder lets it go, ahead of other
threads that ask for it. Close also waits for a reset that another thread runs. Then they mark the
Context, so that new resolver operations and workspace calls fail at once, as in 0.1.2.2, and wait,
without the Context lock, for the resolver operations of a root that are already running, and, for a
reset that would drop hidden storage or delete local database files, for the workspace reads already
running. A resolver operation that is already running goes on, with the resolver operations it
nests, so the running ones can end; a new workspace call that it makes fails like any other. When all
of this ends in time, they go ahead;
when the 30 seconds run out, they raise `RuntimeError` and leave the Context as it was, so new
operations work again. A caller that holds a Context or workspace lock is refused at once, because the
work it would wait for may wait for it, except that it may close an idle child, as below; one that
runs a database drop is refused at once if it would have to wait; so is a close or reset from a
resolver operation of the same root, also while another thread holds the Context lock, and a close by
a thread with a database connection in use while another thread resets the Context.
Once shutdown marks the Context, new workspace calls fail before entering the workspace
lock queue. Admitted reads can finish or fail if their resources retire during the read.
Shutdown releases the Context lock before dropping or disposing resources.
Reset runs each hidden backend drop in its workspace's config scope. It tries
every hidden store's drop and retires every store, even after a drop raises. If
a hidden drop raises, reset retires the resource generation, skips its local
database file cleanup and then raises the drop error; when several drops raise,
it raises one `RuntimeError` that combines their messages and is chained from
the first failure. The other stores' data may already be removed. Services such as the LLM cache then open a new hidden
store in the new generation. The local database files of the default LLM cache and Capsule registry configuration are
dropped like any database. The Context's own SQLite system database file is deleted directly, so another Context with an
engine on it, such as one over the same root, keeps that engine on the unlinked file. A drop of a SQL database
(`Database.drop_database()` or `Database.drop()`, which every SQL backend drop uses) removes it for the whole process:
every hidden store (an LLM cache's or a Capsule registry's) of any Context whose workspace has a SQL backend on the
database, also through another path to a local file, is detached and opens it again, recreating its tables on fresh
storage, at its next use, and its routes on other storage keep their rows. These drops of one database run one after
another when they overlap, from any Context, from two threads of one Context too, and through any name of a local file
that the process opened or dropped while the file existed: the engine registry holds the database's drop locks for the
whole drop. The module-level `heavenbase.database.drop_database(spec)` deletes the database without the drop locks, as
in 0.1.2.2: it detaches no store, but it refuses a thread with a connection to the database in use and closes
HeavenBase's connections to a DuckDB file first, as below. A thread that has a connection to the database in use, such
as an open transaction or a response it has neither consumed nor closed, cannot drop it: `drop_database()`, `drop()`
and the module-level function raise `DropRefusedError` (a `RuntimeError`) at once, because the drop's statement or file
removal would wait for that connection. This applies to a stored database; an in-memory database belongs to one facade,
so no check is made for it, and `drop_database()` and the module-level function remove nothing from it. A response dropped unconsumed releases its connection at once. A response that
a garbage collection frees together with an iterator over it keeps its connection until the
`heavenbase-sqlresponse-settler` thread has read its remaining rows and ended its transaction. That connection does not
count as in use by the thread that executed the response: that thread's calls may wait for a drop, and its own drop
first settles such responses itself. Once the first detach has run, a `drop_database()` closes the dropping
Context's scope on the database until it has removed it. Meanwhile that Context's facade, engine and schema-index calls
on the database wait for the removal, also through an engine or index cached before the drop, unless the calling
thread holds a Context or workspace lock, runs a drop itself, or has a connection to the database in use: the drop may
wait for such a thread (a thread whose server transaction its statement waits for, or one waiting for a lock that
thread holds), so its call raises `Database is being dropped by this Context` at once. A workspace creates its facade
under the Context lock, so a workspace that the Context opens on the database meanwhile fails that way. Its LLM cache
and Capsule registry calls on the database run normally until the drop has detached their stores; a call that starts
after that waits for the drop to end, outside the Context lock, and then reopens the store. A thread that has a
connection in use on the dropped database, such as an open transaction, raises `RuntimeError` instead of waiting for the
drop, because the drop may wait for that connection (on MySQL, `DROP DATABASE` waits for the transaction's metadata
lock); a connection on another database does not change the wait. A store that is opening
while one of its databases is dropped does not attach the workspace it built: it waits for the drop to end and builds
another one, and a build that fails while the drop runs, such as one of the dropping Context, which cannot create its
facade on the database under the Context lock meanwhile, is tried again once it ends. The next drop of a database that
a discarded build used first waits until the stores it made build again have built, for at most ten seconds in all.
Stores with one storage configuration build one at a time, across Contexts, so a store whose rebuild has not ended
within that time can be postponed again. A detached store stays in its
Context, so its own drop and a reset of its Context still drop every route it had opened. A detach waits for the LLM
cache hit bookkeeping already admitted on the detached workspace, so a hit never counts on a row written after the
drop; the dropping Context's scope is still open then, so its own bookkeeping ends normally too. Any other call already
running on a detached store when the drop starts is not waited for and can fail once.
On DuckDB, which keeps a database open in the process while any connection to its file is open, a drop removes the
file only once no connection of HeavenBase's engines to it, or to another name of it, counts as open or being opened, in any
Context. It closes the idle connections, in whichever pool they sit (also one that a dispose replaced), and each
connection returned meanwhile; opening a connection to the file waits for the drop before DuckDB opens the file (or
fails at once, as above), and a checkout of a closed connection connects again, so no call reads the removed database
or writes to it afterwards, with one gap, as in 0.1.2.2: a connection whose close has begun but not finished no
longer counts as open, so a call that opens the file in that moment can still reach the removed database. When
connections stay in use for 30 seconds, such as one in another thread's long transaction, the drop raises
`DropRefusedError` and removes nothing. Before a workspace drop drops any route, it checks every route for a connection
of the dropping thread and raises `DropRefusedError` with no route dropped when it finds one. It then waits, before it
takes the workspace and Context locks, for each of its DuckDB files in turn, until every connection that was in use when
the wait for that file began has been returned. This wait holds no lock and closes no file, so other calls go on, and
connections lent meanwhile do not prolong it; when one of the awaited connections stays in use for 30 seconds, it raises
`DropRefusedError` with no route dropped and the workspace usable. Under those locks it then drops its routes in order,
and a DuckDB file is closed only while it is dropped. A connection that was lent after that file's wait began, or opened
through a hard link of the file, can still make that file's own drop wait, now under the locks, and refuse after 30
seconds. A refusal there stops the drop: the routes after it are kept, and earlier drops that succeeded stay in effect. The
error says which routes were dropped and which routes' drops failed with another error before the refusal and can be
partly dropped, or that no route was dropped when neither happened; another `drop()` drops the routes that were kept. When a DuckDB file cannot be removed
for another reason, such as a single file that is bind-mounted into a container, the drop tries to remove the database's tables
instead, as for the other SQL databases. DuckDB's `<file>.wal` file is removed with the file.
HeavenBase's own drops of hidden storage (LLM cache and Capsule registry drops and Context resets) detach those stores,
and the stores that share a Backend object with the dropped ones, before dropping anything, and no store with the storage
configuration of a dropped or detached store opens until the drop ends: a call that starts on one meanwhile, in any
Context, waits for the drop, and a store that was opening is waited for. These drops hold their Context lock while they
run, so a thread of that Context with a connection in use on the dropped database, which the drop may wait for, raises
`RuntimeError` instead of waiting for the Context lock (a workspace write, an LLM cache or Capsule registry call), and
so does a call that would wait for the open lock such a drop holds. A hidden store builds its workspace and
registers its entities under its Context lock, which workspace construction also held in 0.1.2.2, so the stores of one
Context open one at a time, and a slow first LLM cache or Capsule registry open, such as a slow connect, holds up the
other writes of that Context until the build ends. Stores of other Contexts with other storage configurations open
independently. A store that must wait for a drop that ran during its build waits outside the Context lock, unless its
caller already holds it.
A call made under any Context or workspace lock can only close an idle child whose
shared Context lock is immediately available and that has no admitted reads or owned backend
to dispose. This also works for a child retained across its parent's reset. A call to `close()`
during an ongoing close returns immediately, without waiting for it, and the ongoing close may still
give up, so a `close()` that returned without an error can leave the Context open. A reentrant
`close()` on the thread performing a reset also returns immediately, and a call from another thread
during reset waits for the reset to end and then closes the Context, as above. A
transactional backend applies its grouped rewrite/delete operations atomically. Independently routed backends do not
form a distributed transaction. After an apply error, physical fragments or native projections may be
partial or divergent. Quiesce writers, reconstruct and upsert one authoritative valid full row to
converge declared routes, repair Catalog metadata if needed, then rerun graph audit/delete. Another
process can race endpoint preflight and commit unless the provider supplies stronger serialization.

## Read

```text
Backend.get_many -> decode() per owned field -> merge routed fragments -> caller dict
```

Each backend-owned field is restored with `LogicalType.decode()` during row merge.

`ws.iter_rows` pages the same merge through `Backend.scan_page` keyset pages; the read lock is taken per page and no backend lock survives a page fetch, so writers are admitted between pages. File backends hydrate the entity folder before serving a page, so a cold store pages every stored row. On SQL backends a `Vector` column is decoded from the form the dialect selects: a `float64_blob` database reads packed BLOBs and JSON text, a `json_text` (default) database reads text only and refuses a packed row; the write form is the database's `vector_packed_type`, JSON text on SQLite unless the database opts in to `float64_blob` (see [Vector Storage Form](../reference/database.md#vector-storage-form)).

## Query

```text
Python DSL / JSON -> immutable Query -> QuerySpec -> temporary routes -> execution handlers -> grouped QueryFragments -> Backend.execute -> execution.frame merge -> detached ResultFrame -> explicit export
```

Each fluent transform returns a new spec-only `Query`; it stores no route,
fragment, or result. Each filter leaf selects a storage binding by field and
compiles through `(LogicalType, op, type, strategy)` during explain or
execution. Same-operator `AND` and `OR` levels are flattened recursively, then
clauses that live on the same backend are grouped into one logical
`QueryFragment` even when other backends appear between them. SQL backends lower
that group to one SQLAlchemy `where` expression; scanning backends evaluate the
same fragment as row predicates. Clauses that span different backends execute
per backend and merge frames with intersect, union, or difference.

`near` returns a scored frame. When filters are present, the router first tries same-backend combined execution, then explicit duplicated-storage vector metadata prefiltering, then bounded candidate-ID prefiltering for vector backends that advertise it. If none applies, the scored frame is intersected with filter frames so columns from both sides survive projection.

Subtrees that mix multiple backends are not rewritten into a global normal form yet. For example, `(A_sql & B_lance) | (C_sql & D_lance)` is evaluated as nested subtrees for correctness; cross-subtree regrouping is future router work.

`contains` means text substring containment on text-like fields. `array_contains` means array membership on `Array` fields. `contained_in` means the stored text or stored `Array[ShortText]` keyword is contained in the long query text; with `SparseGramIndex` on SQL this is native reverse-containment search over a sparse GRAM index.

Backend result frames may include reserved non-schema metadata columns such as `score` and `match`. Hydration and frame merges preserve those columns, and projection keeps them only when selected, for example `.select("target_id", "match")`.

## Landscape Search

```text
ws.query(hb.Catalog).where(...).execute().rows()
ws.query(hb.MetaSchema).where(...).execute().rows()
```

Catalog and MetaSchema browsing are ordinary query execution followed by an
explicit result export.

## Extension

```text
module setup/install
  -> inert Extension, Entity, and contribution records in system-modules
  -> workspace fixed Catalog/MetaSchema bootstrap
  -> trusted required Capsule activation
  -> requested optional roots
  -> Extension.requires dependency-first closure
  -> register canonical Entity classes and attach owned APIs
  -> publish active Extension projections
  -> filter conditional records by workspace_requires
  -> persist requested roots only after successful requested activation
```

Installation does not activate an optional Extension. Generic CRUD does not
activate one either. An Extension-owned API may explicitly ensure its own
bundle before domain persistence, while conditional contribution resolution
never enables missing requirements. Required and transitive Extensions are
part of the resolved runtime closure but not additional requested roots.

Artifact Entity classes may differ by Context or installation generation.
Schema ingress validates identifier and structure, then uses the workspace's
canonical class. Catalog and MetaSchema alone require exact fixed class
identity. See
[Workspace Foundation and Extensions](workspace-foundation-and-extensions.md).
