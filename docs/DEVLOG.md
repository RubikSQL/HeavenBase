# Development Log

This rolling log records concise change, verification, and handoff evidence.
Stable behavior belongs in user or engineering documentation; active work
belongs only in `docs/tasks.yaml`. Detailed convergence history remains
available in Git.

## 2026-11-25 — Finalized HeavenBase 0.1.2.3 for release

- Changed: Merged PR #96 (`heavenbase-0.1.2.3`) into `master`, updated repository metadata and documentation links for `Magolor/HeavenBase-legacy`, aligned architecture and roadmap status headers with the `0.1.2.3` train, documented precomputed derive values for network-bound writes, and set the `0.1.2.3` changelog release date.
- Verified: `scripts/sync-env.bash --check`, `scripts/openapi.py --check`, `scripts/docs.py check`, `scripts/gen_capabilities.py --check`, `scripts/flake.bash --ci`, `apps/gui` (`bun run check`), `scripts/test.bash` (`fast` and `full` tiers), `uv build`, `scripts/release.py check-modules dist`, `twine check`, and `scripts/release.py smoke` pass.
- Next: none

## 2026-11-21 — 0.1.2.3 release candidate

- Context shutdown waits, for at most 30 seconds, for other threads' work it
  must not overlap, then marks the Context; a thread that holds a lock or runs
  a drop is refused at once. Hidden store drop
  hooks run in their workspace's config scope. Temporary defaults release
  process-only config scopes before durable ownership. No-op config writes
  revalidate stale scope chains while retaining matching snapshots.
- SQLite invalidates schema across pools and aliases for the same file and
  preserves vector storage classes through Python 3.10 prefetch and Unicode
  text factories. SQL preserves input insert order and the original rollback
  error. Connection initialization survives pooled reads. MySQL row IDs and
  sparse-GRAM postings use exact comparisons. Pickle point reads deserialize
  each returned row once.
- Exact Milvus Lite vector queries and ordinary cosine use binary64 scans.
  Portable L2 and Chroma rescoring handle large finite distances. Pinecone
  mutations reject inconclusive source reads before writing, and Pinecone and
  Chroma retain explicit nulls for defaulted metadata when storing a record.
- Reopened Catalog transitions preflight stored routes, and independent
  projection writes wait for staged keyword-index commits. Failed replacement
  publication can restore saved MetaSchema rows without overwriting later edits.
  Manifests preserve JSON default types.
- Assistant turns serialize per conversation and replay completed answers from
  trace. A durable seal counter avoids fetching previous answers on normal
  completion. Session repair preserves full transcript payloads. Blank
  answers add their usage to the session once, live, on rebuild and on replay
  repair, without transcript messages. Retention counts active and waiting
  conversations with session rows.
- SQLite and PostgreSQL cache hits update only the original row. SDK clients
  close after generator cleanup, including through a retained shutdown method.
  First use during asyncio or uvloop generator shutdown uses zero idle
  connections.
- The CLI resolves the installed Context, preserves it across copies of default
  CLI contexts, and reports the bound dashboard URL for a dynamic port. Click
  honors positional defaults and typed options. Python runs capture scoped
  worker output and render cyclic results within their output budget.
- Reset drops and retires every hidden store when a drop fails; Capsule
  reset does so for the Capsule registry stores only, and a process-wide
  Capsule drop also reaches Contexts whose facade was collected. Dropping
  cache and Capsule resets open the configured store of each selected open
  Context before dropping it, so storage nothing opened yet is dropped too;
  closed Contexts are skipped before their configuration is read. Every
  database drop records itself and detaches the hidden stores of every
  Context on it, before the drop and again after it, through a drop listener
  in the engine registry, which holds a lock per database for the whole
  drop, so drops of one database run one after another; a store whose
  workspace was built while a recorded drop of one of its databases ran
  waits for that drop to end and builds again, and a build that fails while
  a drop runs is tried again after it (the registry reopens the scope before
  its listeners end the drop). A detach waits for the hit writes admitted on
  the store, except on a store that holds the Backend object being dropped
  (its late write fails on the dropped backend). HeavenBase's hidden-storage
  drops detach the stores sharing their storage first and hold the open
  locks of the storage configurations involved, one lock per configuration,
  taken in key order; a store opens under its configuration's lock. The
  cache service's stores live in the Context, one per storage
  configuration; a cache built from a config owns its store. A database
  drop removes the engines and schema indexes every Context cached on that
  database, path aliases of a local file included; it compares path aliases
  outside the registry lock, and a new local-file engine resolves its path
  before taking it. A drop takes its drop locks before the scope closes, so
  two drops from one Context also run one after another, and names the
  locks of a local file by resolved path and by device and inode while the
  file exists. During its drop the dropping Context's own facade, engine and
  schema-index calls wait for the removal, except calls from a thread that
  holds a Context or workspace lock, runs a drop or has a connection to the
  database in use, which fail at once; a hidden store that must wait for a
  drop waits outside the Context lock. A SQLite workspace drop no longer
  deletes `-wal`/`-shm` after the drop returns. The multi-Context drop
  stress test runs in the full
  tier, since it takes about five minutes. Usage merges add a count that
  follows a null count, and every response derives its own `total_tokens`,
  so streamed, blocking and assistant-stream usage agree.
- Engine lifecycles group by database identity for every dialect. Batched L2
  scoring uses the sum of squares, and cosine scoring and native float32
  cosine queries handle vectors of any finite magnitude through one shared
  norm helper and exact power-of-two query scaling; exact NumPy cosine scores
  rows outside the safe norm range from unit-scaled coordinates.
- MySQL-family side-table reads use the object_id index and keep item order.
  Dolt, seekdb and OceanBase compare IDs exactly. SQL routes that share a
  database, decided once at registration after every route's DDL and before
  any publication opens, write one physical row; PostgreSQL routes match by
  database, roles and search path without needing sparse-GRAM tables. Every
  shared-database check fails the registration with `RuntimeError`, and the
  first route ensures the keyword index its peers write during registration.
  A cached SQL table gains a column only after its `ALTER TABLE` succeeds;
  pgvector ensures its table through the same path, and StarRocks caches a
  table only after creating it.
- A non-Pinecone route settles that a row is absent; create-only upserts on
  Pinecone-only entities refuse IDs no route returns, and inferred mutations
  refuse an inconclusive miss even when no entity matched. Equal non-finite
  float replicas agree; byte replicas compare exactly. Scoped MCP inference
  skips entities whose ID domain cannot hold the ID.
- Extension entities keep their extension's definition: changing calls are
  refused, default manifests leave them out, and replay compares them
  portably before registering anything. `hb ws import --allow-replace` exists
  for ordinary entities. The extension's own class loaded under another
  Context root registers as its own definition, and the definition an
  extension entity exports is accepted back by register and import.
- Assistant-log retention reads only bookkeeping columns outside the shared
  lock, and rebuilds merge usage as appends do. A Python run's end puts back
  what the run found in `sys.stdout`, as 0.1.2.2's redirect did, unless a
  capture view in use is there; such a view gives way to the stream it wraps
  once no active run writes through it. A run never changes the type of
  `sys`. Run stdout routing takes no lock, so interpreter
  exit and fork never wait on it, and a write racing a run's end reaches the
  run or the host stream. Module capture skips interpreter cache folders.
  Ctrl+C ends the dashboard cleanly; a busy port exits with status 1, and
  `serve(on_started=...)` is in the type stub.
- Anthropic Messages streams report usage as increments that add up to the
  message's final counts, and carry its id and served model on every chunk.
  A `drop_database()` that cannot remove anything disposes only the caller's
  engine.
- Streamed Anthropic thinking keeps block signatures out of the think text,
  and Claude thinking tool loops send the complete signed blocks back through
  the additive `thinking_blocks` key on the Anthropic and LiteLLM routes;
  OpenAI-compatible payloads leave the key out. Usage merges add reported
  totals to prompt plus completion over the parts without one, so split
  stream usage totals match unstreamed calls. A logged assistant turn's usage
  covers every provider response of its attempt, compaction summaries
  included, on the blocking and streamed paths alike.
- MCP `define_entity` registers the normalized definition it is given, so its
  `catalog` and `gram` options apply and an extension's exported definition
  is accepted. Default agent Session ids and missing assistant-log
  conversation and turn ids come from random tokens instead of the clock.
- pgvector and StarRocks routes keep no sparse-GRAM keyword index and scan
  for `contained_in`. StarRocks ensures its table through the shared SQL path
  and adds missing columns. Registrations build only the scalar indexes the
  plan asks for, a failed index build leaves no declared index on the cached
  table, and split routes ensure the peers' keyword index before opening the
  write transaction. Local-file path bookkeeping in the engine registry is
  pruned in one pass after the map doubles. A dialect's
  `index_reflection_sql` reads DuckDB indexes from `duckdb_indexes()`, and
  before DuckDB 1.1 their columns from the stored `CREATE INDEX` statement,
  so a repeat registration creates none again; no registration drops an index
  on DuckDB. Elsewhere a registration drops only indexes named as HeavenBase
  names an index on their columns, whose columns its table all declares and
  that no route of the plan asks for, and a removed unique constraint leaves
  the default index built in the same registration. Adapter-less SQL routes
  match `contained_in` with the
  field's options. Every SQL route checks its writes against keyword
  indexes it does not maintain, also a route that keeps no index. On the
  `sqlite`, `duckdb`, `postgres`, `pgvector`, `mysql`, `dolt`, `seekdb`,
  `oceanbase` and `oracle` types the check rides on the first `DELETE` or `INSERT ... SELECT` of each
  table, guarded by the index state the adapter last read, so an unchanged
  state costs no query; other routes make one query per write call (two on
  the first checked write after another workspace creates the
  keyword-index tables). A registration that changes an entity's stored
  fingerprints first waits for that entity's writes in progress on the
  `sqlite`, `duckdb`, `postgres`, `pgvector`, `mysql` and `oracle` types.
- `run_python` capture views stay alive after they leave `sys.stdout`, so a
  thread printing through one while a run ends cannot crash Python 3.10 or
  3.11. A view that is not in use, or a copy of one, holds its stream weakly;
  where that stream is gone, a later run's end puts back `sys.__stdout__`, or
  leaves the view where `sys.__stdout__` takes no output. Finished run threads are
  released, copies of stdout during a run are ordinary, and threads outside
  a run that started with `sys.stdout` None see a null stream. A capture
  view is an `io.TextIOBase` that answers as the calling thread's stream;
  its `close()` closes only the calling thread's active run capture, the
  attributes a program sets on it are cleared when the view retires or a run
  takes it up, and a deleted `sys.stdout` is put back at the run's end.
- Claude requests with thinking on use Claude's own `thinking` setting on
  the Anthropic Messages routes and on LiteLLM, send no `temperature` or
  `top_k`, put the effort in `output_config.effort`, and give models before
  4.6 a manual budget; non-Claude models keep their requests. The Messages
  routes also leave out a configured `temperature` or `top_k` in `extra_body`,
  refuse one the caller passed with `ValueError`, and `reasoning_effort` is
  refused before any request on Claude models without an effort setting. `opus` sets no default `temperature` or `seed`.
  Compaction of a Claude thinking loop puts the memo after the kept rounds,
  and Messages routes send unknown `extra_body` fields in the request body.
- MCP `describe_entity` and the query compiler share one rule for the filter
  operations an entity's registration options refuse. Query hydration drops
  a missed row only when a route settles its absence. Pinecone, RediSearch
  and Chroma near queries and explain plans read the live index metadata
  once, when the near clause is compiled, and execution and hydration reuse
  the compiled fragment.
- Tests restore shared method patches on their class, isolate destructive
  workspace setup, and restore worker configuration and config history.

- SQL reads use the captured database, the engine's atomic-upsert claim,
  session isolation and the pool's available slots. A caller using the engine
  directly can still take a reserved connection after concurrent reads fill
  the others. See the database reference for the connection guarantees.
- Sparse-GRAM retirement follows publication and commits shared and eligible
  legacy changes together. Identity Catalog rewrites maintain an existing
  row's keyword index in the same transaction. Fresh registration and explicit
  replacement rebuild a shared index when multiple older indexes remain.
  Autocommit scans and combined or exact SQL near scans use the registered
  options. Token verification preserves query context. PostgreSQL serializes
  registrations per entity and metadata table and shares a transaction between
  compatible routes. A failure rolls back open transactions. Changes committed
  on another database remain in place.
- Config resolution keys snapshots by typed identity. List and node edits
  preserve unresolved values. The scope-stamp memo is bounded and cleared
  with the derived caches. An unchanged Registry refresh verifies the
  stored digest before skipping decode.
- Chroma and Pinecone preserve metadata values through tagged JSON when
  native metadata cannot represent them. Chroma requires 1.5.0 and replaces
  stored metadata while preserving an omitted vector. Native and portable
  vector routes use the requested metric under their documented constraints.
- Assistant session-log validation shares packaged module setup per module
  registry. Subprocess tests isolate the Windows profile alongside HOME.
  Dashboard entry points follow the installed Context and the HTTP adapter
  respects overrides without session identifiers. Trace options recognize
  schema property names and retain the URL-redaction rules. Proxy exits claim
  distinct holds and recover interrupted lock sections. `mock.patch.object`
  can swap the default Context before first access, while `monkeypatch.setattr`
  reads the default first.
- Absence of a row is settled only by an entity's oldest routes; routes that
  joined later are recorded in its MetaSchema entity row
  (`meta.route_generations`). Two related changes were tried and dropped.
  Deleting a joining route's orphan fragments lost data, because the older
  route may be volatile and storage may be shared. Withdrawing the
  registration of a failed atomic upsert could delete another process's
  committed registration, and conditional MetaSchema restore exists only on
  SQLite, in-memory and file storage among the atomic-upsert backends.
- Workspace construction reuses package readings, captures, handler seeding,
  storage profiles and MetaSchema registry rows while their inputs are
  unchanged. Module setup still checks the Registry marker on every call.
  Third-party handler seeds run for each workspace. Seeding and storage
  profiles are kept only for the ModuleService that made them, and no memo
  hands out an object that a caller can change.
- Context and workspace locks survive exceptions raised inside them. A
  block's exit is `partial(max, 0, key=generator.send)`, so it enters no
  Python function before the generator's try statement; give-back steps are
  idempotent and retried; waiting threads use per-call gates instead of
  `threading.Condition`; a block checks for recursion room before it takes
  its lock. `workspace_read_scope` returns one block for the shared lock, the
  read count and the config scope, and nested blocks are no-ops.
  `DBSpec._database_key` and `ConfigSpec._cache_identity` are lock-free
  memoized attributes (`_Memoized` in `utils/config/spec.py`).
- Interrupt safety of the engine registry's connection bookkeeping, the
  DuckDB open and close gate, the read slots and the read gate: enter the C
  lock, keep membership instead of counters, remove first in `finally`, and
  bound the waits. A pool entry that SQLAlchemy calls not in use can still be
  on its way back to the pool, so a DuckDB drop waits 5 seconds before it
  takes it for an interrupted return.
- PostgreSQL metadata locking is limited to write checks and index changes
  (`_SQLGramIndex._read`, `may_lock_meta`). Serialization failures and
  deadlocks are never renamed: a row statement cannot tell a row conflict
  from an index change. `lock_fingerprint` needs no `UPDATE` right: a
  PostgreSQL login without it relies on the registration advisory lock, and
  MySQL servers delete and insert the row.
- The daily test tier fits the hosted runner (two CPUs, four workers, 45
  minutes): every PR-added daily test of 3 seconds or more, measured with one
  core per worker, runs in the full tier (about 13 minutes emulated, 103
  before).

- Task: one 0.1.2.3 for PyPI that downstream consumers can depend on instead
  of locally built wheels; no queue item.
- Changed: three fixes that downstream wheels carried as patches are now
  commits with tests: opt-in explicit Context mode
  (`hb.configure_explicit_context`, also `heavenbase.context.configure_explicit_context`);
  garbage-collection-safe weak-reference callbacks in `ResolverCache` and the
  artifact finder plus a thread- and collection-aware `SQLResponse` iterator
  finalizer. Portable `LIKE`/`ILIKE` match wildcard segments. `python_namespace`
  binds a caller's entity before a packaged or
  extension-provided one with the same class name. Backends that override
  `get_many`/`iter_scan` without `columns=` keep working; optional-backend
  unit tests skip without their driver; a plain `postgresql://` URL selects
  `postgres` again (`infer_from_url: false` on `supabase`); the workspace
  skill resolves its MCP profile through a Toolkit contribution; private
  in-memory `Database` engines are keyed by a fresh id, not the facade
  address.
- Changed after a review of the whole branch against 0.1.2.2: `describe()`
  takes the exclusive workspace lock again; the first default-Context build
  cannot deadlock with a first config read; writes take the Context lock and
  then the workspace lock without waiting for parked readers, so nested reads
  and writes across workspaces (including a cached `LLM` inside a compute
  hook) cannot deadlock; one lock per (Context tree, workspace id);
  `Context.close()` and reset drain reads in flight; assigning
  `runtime.DEFAULT_CONTEXT` installs through `set_default_context`;
  explicit-mode errors name the real cause; `DEFAULT_CONTEXT` left `__all__`;
  a non-bool `atomic` raises; a fresh process refuses a stored `catalog`/`gram`
  change without `allow_replace`. Portable and backend scan scorers leave a row
  with no vector out under `l2`/`ip`; Milvus has a native keyset `scan_page`,
  and Milvus Lite enumerates by sorted key, because its iterator skipped rows
  inserted out of key order; an exact scan reads a route without a native page
  in one `iter_scan` pass; the in-memory `scan_page` sorts ids once per id-set
  change; `cache_version` is marked experimental. Threads that race for one
  gateway key build one OpenAI-compatible or Anthropic SDK client.
- Changed after a second review: every path that falls back to `CM_HVNB`
  (a standalone backend, `Database`, `LLMEngine`, `LLMCache`, a handler or
  predicate, the CLI) selects the lazy default Context through
  `runtime._config_context`; the package root binds `CM_HVNB`'s persistent
  scopes through a late-bound hook, so a first `CM_HVNB` access on any import
  path reads saved config and persists its writes; `ConfigManager` writes apply
  to the latest persisted revision through `Registry.mutate` and retry a lost
  compare-and-set. No package `__init__` imports its own submodules: every
  facade loads exports and submodules on first access (the Registry kernel
  with its own standard-library hook; `placement.index` stays the function),
  third-party packages are imported before their submodules, and a static
  wait-graph guard plus fresh-interpreter stress tests cover concurrent first
  imports.
- Changed after a third review: a log record reads the saved log settings
  before it takes its handler's lock, and `restore_logs` and `redirect_logs`
  write config before the logger table lock, so threads that log their first
  records in a fresh process cannot deadlock; `_config_context` reads the
  installed default before `CM_HVNB`'s owner; config writes back off with
  jitter (up to 1 s) and retry until a 300 s deadline instead of 64 attempts,
  and a list write builds its view from the revision it publishes against;
  config snapshots are keyed by a digest of each stored scope record, because
  a reset restarts its generation; log records and the serialization and
  parallel helpers use packaged values once the saved config cannot be
  opened; SQLite connect pragmas run `busy_timeout` first and retry `database
  is locked`; the python toolkit's `tools` package binds its tool modules on
  access.
- Changed after a fourth review: in explicit Context mode `hash_id` without
  `length=` uses the packaged length instead of selecting the default; an
  unbound standalone backend, `BackendRegistry` or `BackendType.build` takes
  the installed default Context's config (`CM_HVNB` before a default
  exists), and `Database`, a SQL backend, `LLMEngine` or a Capsule function
  given `CM_HVNB` without a resolver under a default with its own config
  raises `TypeError`; the first default selection adopts a root Context built over
  `CM_HVNB`, and a Context checks and binds its `ConfigManager` under the
  manager's lock so that decision cannot race; `python_namespace` treats an
  entity as packaged only when an enabled extension owns it or a loaded
  module artifact or imported `heavenbase` module defines it, so a
  schema-compiled caller entity wins; the `AgentTrace` docstrings state the
  two sanctioned row mutations. An outer writer queues in arrival order
  behind the outer writers of its workspace and drains that workspace's
  outer readers before it takes the Context lock; a reader held back by a
  queued writer enters after at most one write, before the next queued
  writer. The draining writer holds new readers back only while it owns the
  Context lock or no thread owns it, so a Context-lock owner that waits for
  such a reader (through a hook's worker thread or a lock outside
  HeavenBase) cannot deadlock with it; a write nested in another HeavenBase
  call keeps the earlier path. A placement test that registered a shared storage profile now
  passes `replace=True`.
- Changed after a fifth review: `NetworkProxy` tracks the holds of every
  instance together under its process-wide lock. Each hold records the
  environment it found; the newest hold restores it on exit, and an older
  hold that exits first hands it to the newer holds, so concurrent
  `LLM.achat` calls (one instance each) restore the original environment
  whatever their exit order. While holds of different instances overlap, the
  newest hold's values are live for every thread, including one still inside
  an older hold; this limit remains. A setting that cannot be applied leaves the
  environment as it was and records no hold, and only an empty string
  clears a variable. `LLMCache.current` finds every manager's cache in one
  map and only pins the default Context's, so a Context installed with
  `set_default_context` after its first cached call keeps one cache and one
  hidden store, and a scoped reset retires it. `SQLResponse` copies and
  pickles keep the rendered `query` text, not the executed construct. Exact
  near scans with `cache_version` score a batch with the prepared matrix only
  when every row comes from that matrix's generation, and from live values
  otherwise.
- Changed after a sixth review: `HbLogHandler.handle` sends a record through
  `emit`, as `logging.Handler.handle` does in 0.1.2.2, so a subclass that
  overrides `emit` receives every record; the settings it reads before the
  handler lock reach the base `emit` through a thread-local. Bootstrap
  backend discovery skips a name in the module's `__all__` that does not
  load, so a module listing a name it does not define, or a lazy facade
  whose optional submodule fails to import, no longer aborts Context backend
  construction; 0.1.2.2 scanned only the names the module bound. A SQLite
  test runs the connect pragmas through a real engine connect, including a
  retried `database is locked`.
- Also changed after a sixth review: an abandoned `SQLResponse` iterator is
  drained and closed on whichever thread drops it, as in 0.1.2.2, so an
  `INSERT ... RETURNING` built on one thread and dropped on another commits
  again; the earlier owner-thread-only close left its transaction open and
  then rolled it back. The response serializes its cursor reads under a
  reentrant lock, and the close callback that ends the statement's
  transaction runs after the outermost read releases that lock, so an engine
  `commit` listener may use the same response. A drop inside a garbage
  collection or inside a read of the same response queues the response for a
  settler thread, which drains and commits it soon after. A response with an
  iterator is pinned, so a collection that frees it together with the
  iterator does not roll it back first. A drop on the executing thread
  settles that thread's queued responses itself before it checks the
  thread's connections.
- Changed after a further review: `upsert` reads the keywords `overwrite`
  and `atomic` as write flags, each a bool, only when the row's Entity class
  declares no field of that name; a declared field takes the keyword as its
  value. `_CrudService.upsert` takes keyword field values as one mapping, so
  a field named `allowed_entities` is written instead of binding the MCP
  entity scope. Extension writers whose entity class can be a user subclass
  (Agent, Skill, Prompt, memory versions) pass `overwrite` through
  `upsert_many`. With several routes `iter_rows` merges one keyset page per
  route per step by `object_id`, so rows come in ascending order, once each;
  only an empty `scan_page` page ends a route. The never-set
  `DeleteRoute.strategies` field is gone.
- Changed after a review of the vector backends: Milvus key lookups and
  deletes send an escaped `object_id in [...]` filter instead of
  `MilvusClient.get`/`delete(ids=...)`, and filter literals escape exactly
  `\`, `"`, LF and CR, so ids holding a quote or backslash no longer break
  Milvus Lite scans, filters, pages and point reads (the sorted-key Lite scan
  above had spread the old `get` escaping bug to every scan). A Milvus server
  scan opens a new iterator after a batch that ends at a key pymilvus cannot
  quote in its cursor; this is tested against a fake that models the pymilvus
  3.0.0 cursor and the Milvus plan grammar, not a live server. Milvus Lite
  lists and sorts an enumeration's keys once, shared per database file and
  invalidated by any write, instead of once per page, so keyset scans are
  linear; the new `Backend.end_scan` hook (no-op default) lets `ws.iter_rows`
  and exact scans drop that listing when closed early, and
  `MilvusLiteBackend.refresh()` drops a file's listings. `float64_blob` is
  refused on dialects without the new `database-dynamic-typing` tag (only
  SQLite has it) and without a native vector type, because DuckDB stored
  packed bytes in a typed JSON column as unreadable text. The `columns=`
  documentation says only SQL backends skip fetching unrequested fields.
  A Milvus server enumeration caps each query-iterator batch at pymilvus's
  16384 rows instead of failing on a larger `batch_size` or `scan_limit`. A
  Milvus server `near` whose `top_k` is above the server's default search
  limit of 16384 ranks through the portable scan instead of failing, never
  returning fewer neighbours; Milvus Lite accepts such a limit (checked on
  the real engine) and stays native. The server limit itself is not tested
  against a live server.
- Changed after a CI-equivalent matrix run (Python 3.10 to 3.13, unlocked
  dependencies, Windows): `ChromaBackend.upsert` always passes explicit
  embeddings, so Chroma never loads its default ONNX model; a row that omits
  its vector keeps the stored one, a new row without one or a vector set to
  `None` raises `ValueError`, and an entity with no Vector field stored on
  Chroma gets a placeholder as wide as the collection. Dropping a tracked
  workspace discards the implicit bindings of every known entity class to
  that (Context, id) pair and keeps those declared through
  `Entity.register`. The `env` resolver registers through OmegaConf 2.4's
  `register_resolver` when available. Module artifact stores on Windows use
  extended-length paths. The `python-test` fast matrix sets `UV_PYTHON` per
  leg and checks the interpreter. The
  OmegaConf regression test fails only on OmegaConf 2.4; the lock pins 2.3,
  so locked CI cannot catch a regression there.
- Changed after a review of the combined increments: the ordered merge of
  several routes, which `ws.iter_rows` and exact scans use when an entity has
  more than one route, ends each route's scan with `Backend.end_scan` when it
  is closed early, after the last `object_id` of that route's latest page, as
  the single-route path already did; one failing `end_scan` does not keep the
  others from running. Before, only single-route and streamed enumerations
  called the hook, so closing a vector entity's `iter_rows` early left its
  Milvus Lite key listing open. A Lite page whose cursor lies inside a page a
  listing already served continues that listing, so a merge that uses a Lite
  page only partway, when another route holds rows Lite lacks, no longer
  lists the remaining keys again on every step.
- Changed after a review of the failed-open paths: `hash_id` without
  `length=` and `StableRNG` read `CM_HVNB` through `_get_or_packaged`, so on
  an unwritable home, an invalid bootstrap file or in explicit Context mode
  they use the packaged values and Context-scoped LLM cache keys, the mock
  LLM and prompt ids work; a global log helper whose `CM_HVNB.set` cannot be
  saved keeps its setting for the current process
  (`ConfigManager._set_if_openable`) instead of silently doing nothing. In
  explicit Context mode `hash_id` still uses the packaged length without
  attempting the open.
- Changed after a review of config write contention: a `ConfigManager` write
  that leaves the stored layer unchanged saves no version and publishes
  nothing, so processes that save the same log setting at once no longer wait
  in compare-and-set backoff; only a lasting failed open (an unwritable home,
  an invalid bootstrap file, explicit Context mode) makes `_get_or_packaged`
  and `_set_if_openable` fall back and stop retrying, while any other error
  (a busy database, a Registry error) is raised and retried on the next
  access, so `hash_id` never switches to the packaged length after a passing
  failure; the serialization and parallel helpers keep 0.1.2.2's default on
  any config error.
- Changed after reviews of the SQL, rows, Chroma, trace and schema changes:
  reads on a `singleton` pool keep the backend lock like `static` and
  `assertion`, and an engine that serves one thread at a time (those pools
  or an in-memory database, `DBSpec.serialized`) is private to its
  `Database` facade; a backend whose type does not claim `atomic-upsert`
  (the generic `sql` type, ClickHouse, StarRocks, Trino, or an unknown type)
  keeps the lock across its reads because its upsert is not promised to be
  one transaction (one rule: `db.spec.serialized or not atomic-upsert`, the claim read from
  the Registry). A failed database drop
  that falls back to dropping tables clears the facade's private engine
  key. `Backend.iter_scan` has a default over one `scan()` call, so
  scan-only providers enumerate; a subclass that overrides `scan` or
  `iter_scan` below a native page pages through its override. The
  in-memory and file backends page a keyset scan in linear time. A SQL
  `upsert_many` batch whose rows set different optional fields inserts one
  statement per field set (two fixes did this the same way; one is kept
  with both tests). The sparse-GRAM index is shared by every workspace
  on a database, keyword ids include the namespace, short keyword widths
  are read per query, and an index 0.1.2.2 wrote per workspace is retired
  behind a marker fingerprint so either version rebuilds from stored rows
  after the other wrote. `Database.execute` streams only a statement it
  can classify as a single read and commits every other statement before
  returning, which also makes DuckDB keep DDL and DML whose response was
  dropped. `columns=` is validated whatever the ids, and an unstored name
  is left out of a route's fragments. `atomic=True` needs the new
  `atomic-upsert` Backend tag (50 tag definitions in all with
  `database-dynamic-typing`). A populated row result without `select`
  carries every declared column in one order. Chroma `iter_scan` is one
  linear pass by id, and every Chroma read stays under SQLite's variable
  limit. LLM trace options are redacted inside every container and every
  embedded URL whose user info is well formed or holds a `:` before an
  unescaped `/`, `?` or `#` (then it runs to the last `@` of the URL), and
  `redact_url` keeps the host as written; retention never deletes the
  trace of a conversation without a session row, and `rebuild_session`
  runs no retention; `python_guide` lists exactly the
  bound names. A schema change of a manifest-defined entity updates the
  saved replay manifest under a registry re-read and compare-and-set
  retry, and warns instead when the change came through a detached facade
  or the definition cannot be replayed. Date and datetime defaults and
  examples are exported and saved as isoformat() text with their type, and
  `Entity.from_schema` restores them. In explicit Context mode `hash_id`
  keeps using the packaged length without attempting the open, beside the
  general failed-open fallback. Because `INSERT ... RETURNING` and every
  other non-read now commits before `execute` returns, the drop and
  garbage-collection rules of `SQLResponse` iterators matter only for a
  streamed read, such as one that writes through a function it calls; the
  CHANGELOG and the `SQLResponse` docstring say so.
- Changed after the gate and reviews of the merged branch: a SQL read keeps
  the backend lock across its statement whenever the backend type does not
  claim `atomic-upsert` (the generic `sql` type, ClickHouse, StarRocks,
  Trino, or a type the Registry does not know), read from the Registry
  instead of a ClickHouse-only class flag, so no read sees an upsert between
  its delete and its insert. A facade whose definition of an entity is older
  than the saved replay definition another Context or process wrote keeps
  that definition and warns instead of overwriting it; an unreplayable
  change still saves its `catalog` and `gram` options, so reopening by id
  does not raise; the detached-facade warning says when a reopen will raise.
  Trace URL redaction also finds user info whose password holds an
  unescaped `/`, `?` or `#`. The commit-listener test of `SQLResponse` now
  drives a streamed read, since a standalone `INSERT ... RETURNING` commits
  inside `execute`.
- Changed after a review of the integrated branch: `import heavenbase`
  imports `asyncio` first, so a thread whose first use reaches
  `typing_extensions.deprecated` (which imports `asyncio.coroutines` before
  `asyncio`) cannot deadlock against another thread's first `import asyncio`;
  a `ConfigManager` write reads the list it edits, the retained-version count
  and the `setdef` check from the draft it publishes, refreshes only after a
  lost compare-and-set and takes no manager lock (the Registry runs one
  process's writes one at a time), so `setdef` cannot overwrite a value saved
  after its check, a list write that changes nothing cannot conflict, an
  up-to-date write reads the store only to publish, and a first `remove`,
  `compact` or `setup(reset=True)` cannot deadlock against a Context-lock
  holder that writes config; snapshot stamps digest each scope's generation
  and latest layer and are read with those layers from one Registry state, so
  a snapshot is never cached under another revision's stamps; a bootstrap
  whose backend cannot be built (a module or driver that cannot be imported,
  settings the backend rejects) counts as an invalid bootstrap file for the
  tolerant readers; the log helper docs say that saved settings also reach
  processes already running.
- Changed after a review of the no-op write path: a `ConfigManager` change
  that stages nothing published nothing, so no compare-and-set checked the
  revision it decided on, and a process that had not yet seen another
  process's write dropped its own `set`, `unset`, list item or global log
  helper while `setdef`, `remove`, `compact` and `init` answered from the old
  revision. `_commit` now learns from each change whether it staged a write;
  one that staged nothing refreshes and runs again when the store moved past
  the revision it read, so only writes that change nothing pay that store
  read. `setup()` reads the store before it skips a base scope held in
  memory, `init` reads its retained-version count from the draft, and a
  `KeyError` or `IndexError` raised while building the control backend is no
  longer counted as a bootstrap fault (a plain `LookupError`, which the
  bootstrap dialect catalog raises for a dialect it lacks, still is).
- Changed after a further round of reviews of the whole branch: while the
  Context lock is free, only one draining writer per Context (the drainer)
  holds new readers of its workspace back, so two writers draining at once
  can no longer deadlock through readers that fan out reads of each other's
  workspace. `import heavenbase` imports `asyncio` first; a bootstrap whose
  backend cannot be built falls back like an invalid one. The sparse-GRAM
  adapter also retires a field removed from the entity or retyped away from
  `Array[ShortText]`, an identity Catalog write removes keyword rows its
  stored row left, `ensure` only adds storage and `Backend.prune` retires an
  index once the registration takes effect (a failed registration restores
  the previous one's index), and a retired field's keyword rows and
  fingerprint go in one transaction. A SELECT construct that nests SQL text
  which might write, and a `text().columns()` construct, are classified by
  their compiled text, and DDL run as `text().columns()` invalidates the
  schema index. Trace options keep nulls, booleans, numbers and lists of
  only those under credential-looking names and replace everything else
  there whole, so distinct requests keep distinct fingerprints; JSON-schema
  property names are recognised structurally wherever a schema travels,
  and only the four top-level model-visible options are exempt; user info
  that holds a `:` and an unescaped `/`, `?` or `#` runs to the last `@` of
  the URL. The session log refuses every in-memory backend declaration,
  also in a HOME never bootstrapped, and a refused open removes a
  registration it created. A packed SQLite
  vector column reads JSON text on SQLAlchemy before 2.0.11; `get_many`
  validates `columns=` before the provider read; `LLMCache.reset(drop=True)`
  drops stores under the Context lock; a combined near scan keeps its
  filter's metadata; `None` in eq, ne and in filters tests for a missing
  value on Timestamp, numeric, Boolean, Categorical and Json fields, on
  LanceDB natively, and an order filter given `None` raises. A SQL engine that commits every
  statement withdraws its `atomic-upsert` claim at construction, so
  `atomic=True` is refused on it and its reads keep the backend lock; Chroma
  splits an upsert above the client's batch limit, which is why it makes no
  claim. `NetworkProxy` uses a reentrant lock and defers an exit that a
  finalizer or signal handler makes inside another hold's locked step.
  Milvus Lite key listings tolerate an enumeration that the garbage
  collector finalizes during a listing loop. Assigning or patching
  `runtime.DEFAULT_CONTEXT` swaps the default as in 0.1.2.2, unset dashboard
  request fields reach only service methods that accept them, and write-flag
  errors name a foreign bool's type by module.
- Changed after the second review of the merged branch: the shared
  sparse-GRAM index records one set of `SparseGramIndex` options per field.
  A registration with other options rebuilds it, writing the rows and the
  fingerprint in one transaction, and a backend registered with the earlier
  options (or one whose field another workspace unbound) raises
  `SparseGramIndexConflictError` on its next `contained_in` or indexed write
  instead of silently missing rows; re-registering with `allow_replace=True`
  rebuilds it. A per-workspace index that 0.1.2.2 wrote through any
  workspace id now triggers the rebuild, and it is retired only after the
  rebuild commits, so an interrupted rebuild is retried. A SQL read decides
  whether it keeps the lock from the `Database` it captured, so a read that
  races a `drop` fails with the database's error rather than
  `AttributeError`. A detached facade's first registration of a saved entity
  warns when it differs from the saved definition; the same change saved
  first by another Context no longer warns; a definition naming a hook from
  the facade's `computes=` mapping is compared through that mapping, so its
  unsaved change names the real cause; and the out-of-date warning says to
  release the facade before reopening it. The CHANGELOG, trace, database,
  gram and manifest docs now state the exact rules.
- Changed after the third review of the merged branch: a write through a
  backend that registered a field without the sparse-GRAM index raises
  `SparseGramIndexConflictError` once another registration indexes the field
  on the same database. A rebuild deletes the old index rows before it reads
  the stored rows, so on SQLite no concurrent write is lost to it. Several
  per-workspace 0.1.2.2 indexes of one field are kept instead of retired,
  because 0.1.2.2 could not reopen the second workspace after rebuilding
  both; a digest of their keyword rows decides when the shared index is
  rebuilt, and every registration, in a long-lived process too, looks for
  them. A failed `Database.execute` outside `db.tx()` rolls back every
  parameter set instead of committing the ones that ran. Saving a changed
  definition to the saved replay manifest writes only the manifest, without
  setting up the modules again (about 1.5 s per `describe` before), and
  `WorkspaceSpec.register` reads the registry again after a compare-and-set
  conflict, so creating a workspace no longer fails while another process
  writes the registry.
- Changed after the gate and reviews of the third integration batch: the
  session log's in-memory check fell back to "not in memory" when the
  registry did not know a lowercased mixed-case alias (`InMemBackend`), so a
  HOME never bootstrapped accepted it; the check now compares the identifier
  and registered aliases in any case, and the Chroma-without-a-path check
  resolves aliases the same way. A definition without an indexed field
  retires the shared sparse-GRAM index as `gram=False` does, so the field is
  recorded as unbound and both sides raise `SparseGramIndexConflictError`
  instead of silently missing rows. The autocommit detection reads the DBAPI's
  real connect arguments, treats `autocommit=None` and autocommit pragmas as
  possibly autocommitting, and `LLMCache.reset(drop=True)` closes the other
  caches' open stores before it drops the database. The CHANGELOG no longer
  advises importing entity classes from their defining modules in explicit
  mode, and its autocommit, cache-reset, trace-name, order-filter and NumPy
  wording states the exact rules. The two entries above that say an engine
  that commits every statement withdraws the claim, and that the drop runs
  under the Context lock, are completed by this one.
- Changed after a second review of the integrated branch: a `ConfigManager`
  change that raises, such as a list index past the end of a list another
  process has since extended, runs again when a refresh shows the store moved
  past the revision it read, and is raised only on the latest revision;
  `setdef` takes the resolved config of the records this process holds
  before the write, from the snapshot cache when available,
  and resolves under the Registry lock only when the draft holds other
  records, so a `setdef` that finds a value no longer resolves the whole
  config while it holds that lock; a write clears the derived caches only
  when the Registry published, so one that changes nothing on the latest
  revision keeps them;
  an installed backend module or driver whose import fails with a plain
  `ImportError` counts as a bootstrap fault like a missing one, unless the
  error shows a module that another thread was still importing; and
  `import heavenbase` runs its early `asyncio` import again when it is the
  thread that meets the import-lock deadlock.
- Changed for speed: a Vector stored as JSON text is encoded with
  `dumps_json(..., adapt=False)` and decoded with `loads_json(...,
  restore=False)`, so the stored text is byte-identical and vector text
  holding a tagged object (`__obj_type__`), which no release writes for a
  vector, raises instead of being restored. `ConfigManager` memoizes each
  scope's generation and record digest for the Registry object and
  publication it read, reading the publication before any record; the memo
  keeps at most `max_generations` scopes, least recently used first out, is
  off at 0, and is cleared with the config caches. `Registry.refresh`
  recognizes a stored snapshot at the published revision and digest,
  verifies that digest against the stored payload, and skips the decode,
  copy and validation. Measured on a loaded aarch64 server (median per
  call): an unchanged refresh of a SQLite-backed config Registry with a
  301 KB payload took about 22 ms and now takes 0.75 to 0.9 ms, of which the
  digest check is about 0.18 ms; a two-scope generation read with a store
  refresh went from about 94 ms to under 1 ms; a 1,024-component vector
  (22.5 KB of text) encodes in about 0.6 ms instead of 1.5 ms and decodes in
  about 0.4 ms instead of 0.55 ms.
- Superseded: entries below record packed SQLite vectors as the default, the
  LLM cache and the session log off by default, the session log on by
  default, and `agent-trace` in the default `agent` extension. This release
  instead keeps JSON-text SQLite vectors by default (`vector_packed_type:
  float64_blob` opts in), keeps the LLM cache on as in 0.1.2.2, ships the
  session log off, and moves the entity to the optional `agent-log` extension
  as `AgentTrace` (entity id `agent-trace`), which only the session log's own
  workspace enables. The session log reads trace rows per conversation and
  never leaves a failed turn's conversation pinned; trace options redact
  credential-looking values at any depth; `LLMTraceEvent` and `TraceCallback`
  are exported from `heavenbase.llm`. `uv.lock` matches `master`. The
  2026-09-12 entry's per-instance `NetworkProxy` hold count is replaced by
  the per-process hold tracking above. Its limit that two instances entered
  at once interleave their changes is reduced, not removed: the final
  environment no longer depends on the exit order, but while holds of
  different instances overlap the newest hold's values are live, so a thread
  still inside an older hold sees the newer instance's settings.
- Verified (Linux aarch64, Python 3.12, `uv sync --python 3.12 --extra dev
  --locked`): on the integrated branch through the SDK-client fix
  (`d512d55b`), `uv lock --check` and the five hook checks pass, and the
  default tier passed 1,224 tests with 18 optional-provider/Node.js skips.
  Each commit of the second review passes the same checks and the default
  tier on its own; at its last code commit the default tier passed 1,300
  tests with the same 18 skips. Each review series passed the `fast or
  full` tier on its own branch (2,363 to 2,372 passed, 115 to 117 skipped,
  0 failed). After the third review the same checks and the default tier
  pass at its last commit, each new regression test fails on the previous
  head, and 128 processes, 32 at a time, writing one fresh home through
  `CM_HVNB` saved every value in 7 of 7 runs. After the failed-open review the
  same checks pass at its last code commit and the default tier passed 1,315
  tests with the same 18 skips; each of its new regression tests fails on the
  previous head except the real-engine pragma test, which guards the earlier
  pragma fix. After the write-contention review the same checks pass and the
  default tier passed 1,324 tests with the same 18 skips; five of its seven
  new regression tests fail on the previous head, while the lasting-cause
  fallback test (three cases) and the failed-save log helper test pass there
  too, because that head fell back on every failed open, so they guard that
  the narrowed fallback still covers each lasting cause; 32 processes calling `suppress_logs()`
  at once on one home took a median of about 1 s per call instead of about
  16 s, each process ending suppressed. The pull request records the final tier counts of the branch
  head against `master`.
- Next: none

## 2026-10-02 — Read-only assistant refuses every code_reduce spelling

- Task: direct consumer request from a downstream lab audit; no queue item.
- Changed: the read-only `query` check resolves every spec key, and each `op`
  value, through `normalize_op` with the executing workspace's resolver, so
  case, surrounding space, and aliases such as `$code_reduce` are refused like
  the literal key, at any depth and in JSON text. Before, those spellings ran
  in read-only mode. Specs without `code_reduce` still run.
- Verified: the new dashboard contract fails on the previous head and passes;
  it ran in the same targeted (478) and default integrity (1,101) runs and
  static gates as the result-column fix.
- Next: none

## 2026-10-02 — Empty and sparse query results keep their declared columns

- Task: direct consumer request from a downstream lab audit; no queue item.
- Changed: row queries order and project against the columns they declare
  (entity fields, executed-filter metadata such as `match`, a near clause's
  `score`) instead of the keys that matched rows stored. Ordering, paging, or
  selecting over no rows returns an empty frame with the requested columns
  instead of raising `Unsupported result column`, and a declared field that no
  matched row stored reads as null for `order` and `select`. An undeclared name
  fails by name whether or not rows matched: `ResultFrame.project` no longer
  invents columns on an empty frame, and SparseGram misses and empty candidate
  prefilters declare `match` as the scan route does.
- Verified: 24 of the 39 new cases fail on the previous head; all pass. The
  query, execution-frame, and dashboard files passed 478 tests with 22
  optional-provider, external, and Node.js skips; the default integrity suite
  passed 1,101 tests with 17 optional-provider/Node.js skips. Black, Flake8,
  docs, capabilities, and environment/README checks pass.
- Next: none

## 2026-09-12 — SQL reads run their statements outside the backend lock

- Task: a consumer whose single `main` SQLite backend serves every read saw
  reader threads queue behind each other, because `SQLBackend.get_many`,
  `scan_page`, `exists_many`, `count`, `scan`, and `execute` were
  `@backend_locked` and held the per-backend RLock across `orm_execute`,
  although the workspace lock already admits reads concurrently. Prototype for
  that consumer; no protocol, storage-format, or default change. A read still
  waits for a mutation in progress (see Limits); that latency is not addressed
  here.
- Changed: `SQLBackend` reads take the backend lock only to plan: `_need_db`,
  the entity's table, the projection, a copy of the side-table specs, and the
  construct-cache statement (`_read_storage` returns that snapshot; `_construct`
  is documented as lock-guarded because every hit reorders the cache), then
  run the statement on a pooled connection and decode its rows
  (`_decode_side_rows` is now a static method over the snapshotted specs)
  outside the lock. `execute` plans its aggregate and pushdown statements under
  the lock in `_aggregate_read` and `_fragment_read` and runs them outside; its
  scan fallbacks go through the public `scan` and `execute`, which lock their
  own planning; a strategy-adapter fragment still runs entirely under the lock
  because the sparse-GRAM adapter's index handles and fingerprints are guarded
  by that lock alone. `upsert`, `ensure`, `ensure_indexes`, `drop`,
  `_refresh_workspace`, SQLite `ensure` and `compare_upsert`, and the resolver
  clear keep the lock as before; the workspace lock and the exclusive side of
  mutations are untouched. The `Backend.scan_page` docstring no longer states
  that every provider fetches its page under the lock. Review of the two
  drafts added three fixes. (1) An engine that cannot serve two threads at
  once defeats the split: `:memory:` SQLite and DuckDB default to
  `pool_class: static` (one shared DBAPI connection), and a reader's `_exec`
  rollback on that connection discarded a locked writer's uncommitted `upsert`
  (measured: the writer took the lock while a read executed, the read saw the
  uncommitted row, and the row was gone after the writer's commit); with an
  explicit `queue` or `null` pool on `:memory:` the second checkout is instead
  a separate empty database (measured on the first draft: 8 threads × 20 scans
  gave 154 `no such table` errors), which the parent avoided only because its
  lock never let two checkouts overlap. `_connect_database` now records
  `_serialized_reads` when the spec is in-memory or the resolved `pool_class`
  is `static` or `assertion`, and `_read_scope` makes every read on such an
  engine take the lock again for its execute-and-decode phase, so those reads
  run one at a time as before; every other pool gives each read its own
  connection and stays unlocked. (2) Every read enters `Database.engine`,
  which wraps the registry lookup in the database's one `NetworkProxy`; that
  context kept its backup in a shared instance dict, so two unlocked readers
  could raise `dictionary changed size during iteration` (measured on the first
  draft: 51 errors in 640,000 `backend.db.engine` calls from 16 threads) or,
  with a proxy configured, leave `HTTP_PROXY` set after both exits.
  `NetworkProxy` now counts its holds under one process-wide lock: the first
  concurrent or nested hold applies the settings and takes the backup, the
  last exit restores it, and an instance with no settings changes nothing and
  takes no lock (0.1.2.3 replaces this per-instance count; see the
  2026-11-20 entry). (3) Changing `_decode_side_rows` to take the specs required
  updating `PgVectorBackend.execute`, which the first draft (bd129dfd) missed,
  so every pgvector near query on that draft raised `AttributeError`; the
  parent's call matched the parent's signature. The pgvector near path also
  planned its statement through the construct cache without the lock on the
  parent; it now plans under the lock, runs through `_read_scope`, and decodes
  with the snapshotted specs, and `PgVectorBackend.ensure`,
  `StarRocksBackend.ensure`, and `StarRocksBackend.drop`, which mutate `tables`
  and `db`, are `@backend_locked` like the base methods.
- Pool: no code change. A `pool` mapping in a SQL backend map already reaches
  `create_engine` (`from_config` → `Database(pool=...)` → `_pool_kwargs`):
  `pool_class`, `pool_size`, `max_overflow`, `pool_timeout`, `pool_pre_ping`,
  `pool_recycle`, `connect_args`; SQLite keeps `queue`, 5 + 10 by default.
  Documented in `database.md`, the SQLite `from_config` docstring, and
  `CHANGELOG.md`, together with the `static`/`assertion` exception and the
  fact that on `:memory:` only `static` shares the one database (`singleton`
  gives each thread a private one; `queue` and `null` are not meaningful) and
  reads keep the lock whatever the pool class.
- Verified: `tests/backends/test_sql_read_lock.py` (fast, 10 tests): four
  readers' statements are in flight together behind a barrier placed inside
  `orm_execute`, the backend lock is acquirable meanwhile, and the reads finish
  while the test thread still holds it (the same test breaks its barrier on
  the parent); `upsert` asks for the lock and cannot finish until the holder
  releases it; eight concurrent readers equal the sequential results; a `pool`
  mapping sizes the QueuePool and omitted keys keep the defaults; on
  `:memory:` with its default static pool, on `:memory:` with
  `pool_class: queue`, and on a file with `pool_class: static` a reader gated
  inside `orm_execute` still owns the backend lock, a write started meanwhile
  lands after the read and is kept, and four readers looping `count`/`scan`
  beside 150 upserts on `:memory:` see no error and leave all 150 rows; two
  readers of a backend with `http_proxy` configured are gated inside
  `Database.engine` at once, the first to leave keeps `HTTP_PROXY` applied for
  the second, and the last exit restores the original value; a pgvector near
  query against a stubbed `orm_execute` runs its statement outside the lock,
  scores its rows, and returns empty for an unknown entity.
  `tests/core/test_utils_basic.py`: a `NetworkProxy` without settings leaves
  `backup` empty and the environment untouched; two threads' interleaved holds
  and a nested hold restore the original only on the last exit (the
  interleaved case leaves the applied value in place on the previous
  implementation); 16 threads × 2,000 holds see the applied value every time
  and restore it. Also `tests/backends/test_scan_page.py`,
  `test_sql_statement_reuse.py`, `test_sql_projection.py`,
  `test_sql_filter_cache.py`, `tests/core/test_database.py`; the fast suite;
  `tests/core/test_thread_safety.py`, `tests/backends/test_scan_page.py`, and
  `tests/backends/test_backends.py` with `-m ""`; Black, Flake8, and docs
  checks. Windows (Python 3.12.13): the `fast or full` suite with
  `-n 16` reported 27 more failures than the parent commit's run of the same
  suite; rerun serially, 23 of them passed (their `SourceUnavailable` /
  "module artifact contains a special file" errors came from the parallel
  run's shared system temp directory, not from this change), and the 4 that
  remained were test bugs fixed in the follow-up commit: the two proxy tests
  deleted the lowercase `http_proxy`, which on Windows is the same variable
  as `HTTP_PROXY`, and the pgvector test imported `pgvector` unconditionally.
- Limits: a read still waits for a mutation in progress, because planning
  needs the lock that `upsert`, `ensure`, and `drop` hold for their whole
  call; letting reads proceed during a write (planning inputs published under
  a shorter hold, or a snapshot a writer swaps atomically) is a separate
  design and is not done here. A reader in flight during a workspace rebind
  finishes against the previous database; `get_many` and `exists_many` over
  more than one batch hold the lock across their batches, as in 0.1.2.2;
  `drop` waits up to 30 s for the reads running outside the lock (0.1.2.2
  waited without a limit), and a read of a dropped database raises `RuntimeError("unavailable after drop")`;
  sparse-GRAM query execution and the portable
  `Backend.scan_page` default still hold the lock across their queries; an
  in-memory database, or any `pool_class: static` or `assertion` engine, gains
  nothing from this change because its reads keep the lock; two
  `NetworkProxy` instances with different settings entered at once still
  interleave their process-wide environment changes, as before (reduced in
  0.1.2.3: while they overlap the newest hold's values are live, and the
  original environment returns after the last exit; see the 2026-11-20
  entry); the pgvector
  and StarRocks changes are verified against stubs only, no live Postgres or
  StarRocks ran; SQLite throughput under real contention was not measured
  here.
- Next: none

## 2026-09-11 — Identity-mode Catalog writes keep the stored lifecycle

- Task: a downstream consumer registers every entity with
  `catalog="identity"`, and one of its acceptance checks failed: an identity write over a Catalog row whose `created_at`, `active`,
  or `visible` another writer had changed reset them (`created_at` to the
  write time, the flags to their defaults), because the identity branch of
  `_CatalogService.row` passed `existing=None` and the writer never read the
  stored row. `full` preserves exactly those three fields
  (`catalog_write_timestamps`); identity must preserve the same set.
- Changed: `_EntityWriter.prepare_upsert_rows` collects the Catalog id of
  every `full` and `identity` row in the batch and, under
  `catalog_mode="merge"`, reads their stored rows through one projected
  `rows_for_ids` call per Catalog route (`_existing_catalog_rows`, fetching
  only `CATALOG_LIFECYCLE_FIELDS`; a row that lacks one still counts as
  absent). `full` therefore issues one SELECT per batch instead of one per
  row, and `"fresh"` skips the read for both modes. `_CatalogService.row`
  applies `catalog_write_timestamps(existing, now)` in both modes, so an
  identity row keeps `created_at`, `active`, and `visible`, takes a new
  `updated_at`, and still projects, signs, derives, and indexes nothing.
  `catalog="none"` is unchanged. `EntityOptions`, `register`, `upsert_many`,
  `public-api.md`, `id-semantics.md`, `data-flows.md`, and `CHANGELOG.md` no
  longer say the stored row is not read back.
- Measured (scratch script, 200 rewrites of existing rows into SQLite with
  `gram=False`, min of 5 repeats, two interleaved rounds on a loaded host):
  identity single `upsert` 1.02–1.13 → 1.30–1.36 ms per row (0 → 1 Catalog
  SELECT per call); identity `upsert_many` 0.23 → 0.24–0.26 ms per row (0 → 1
  SELECT per batch); full `upsert_many` 0.81–1.20 → 0.46–0.71 ms per row
  (200 → 1 SELECTs per batch); full single `upsert` unchanged within noise.
- Verified: `tests/workspace/test_workspace_entity_options.py` (first identity
  write from defaults; an identity rewrite keeps the changed lifecycle,
  advances `updated_at`, projects nothing, leaves no keyword rows),
  `tests/workspace/test_workspace_rows.py` (an identity write issues one
  Catalog SELECT; a two-row atomic batch reads both stored rows in one),
  `tests/core/test_core.py::test_upsert_many_fresh_catalog_mode_skips_existing_catalog_lookup`
  (rejects any Catalog `rows_for_ids` under `fresh`): 54 passed with every
  marker; Black, Flake8, and docs checks.
- Next: none

## 2026-09-11 — Structural row copies and per-publication Tag captures

- Task: a downstream consumer's write-path CPU profile put `copy.deepcopy` at a third of the
  run: 36 default deep copies per Catalog row, two full copies of every
  encoded entity row (a 2048-float vector each) in `RowOp`, one deep copy per
  `ModuleCatalog.inspect` row, per-leaf deep copies in config thaw and merge,
  and Tag definitions re-frozen on every `tags()` read. No behavior change.
- Changed: `heavenbase.utils.copy_data` rebuilds `dict`/`list`/`tuple` data
  and shares immutable leaves (anything else is deep-copied), so a copied row
  stays as detached as before at one list construction per vector.
  `RowOp.__post_init__` and `to_row` (which now filters before copying),
  `SQLResponse.to_list`, `ModuleCatalog.inspect`/`get`/`__getitem__`,
  `_CatalogService.copy_value`, config `_unfreeze`/`_merge_value`, and the Tag
  `_freeze` leaf use it. `build_catalog_row` copies `CATALOG_DEFAULTS` once
  per row instead of one deep copy per field.
- Changed: `_captured_rows` keeps one `_CapturedRows` per service publication
  on `_CatalogRows.derived`; `_definitions` and the static subject views of
  `_tags` are memoized on that capture, so a stable Registry builds each
  `TagDefinition` and `TagView` once and later `tags()`, `tag()`, and
  `tag_definition()` calls return the same immutable objects. A publication
  creates a new capture, which is the only invalidation; generic resolvers
  are captured per call as before.
- Measured (scratch script, 200 single-row upserts of a 2048-float Vector
  into SQLite, Python 3.12, on a loaded host): `catalog="identity", gram=False`
  0.741 s → 0.377 s per 200 rows with 7,506 → 4 top-level deep copies;
  defaults 1.225 s → 0.596 s with 8,306 → 4; 50 `get` reads 0.060 s → 0.024 s.
- Verified: `tests/config/test_config_manager.py` (copy_data isolation; no
  deepcopy for JSON leaves in thaw and merge), `tests/workspace/test_workspace_rows.py`
  (RowOp and `to_row` isolation, Catalog default containers without deepcopy),
  `tests/registry/test_tags.py` (definitions and views built once per
  publication, replaced by a publication, inspect rows still detached),
  `tests/core/test_database.py` (`to_list` rows detached); the registry,
  config, workspace, execution-protocol, and core suites with every marker
  (295 passed; `test_builtin_database_dialect_metadata_does_not_import_provider_modules`
  fails identically at the base commit); Black, Flake8, and docs checks.
- Next: none

## 2026-09-11 — Cold file-backend paging and stored-registration Catalog reconcile

- Task: a second independent verification of the combined series found two
  blocking defects and four small ones; this entry closes them. No default
  changes.
- Changed: `FileBackend.scan_page` and `iter_scan` hydrate the entity folder
  before paging (the first page and a full enumeration reload it exactly as
  `scan` does; later pages use the loaded rows), so `ws.iter_rows` and
  `_CatalogAuditService.reconcile_entity` on a cold or refreshed JSON/Pickle
  store see every row instead of none.
- Changed: `_SchemaService._register` compares a registration that has no
  live predecessor with the registration the store's MetaSchema entity row
  recorded (`_MetaSchemaService.stored_options`; a row from a release without
  options means the defaults, no row means nothing stored). `None` inherits
  that stored registration; a different `catalog` re-establishes the stored
  registration first and then changes it, so the rewrite keeps the live
  ordering rule (leaving `full` rewrites before the switch, entering it
  after) and a failure leaves the same registration behind. Catalog deletes
  now carry `strategies=True` whatever the mode, so a keyword row can never
  outlive its Catalog row and make a later `contained_in` raise
  `SchemaVersionMismatchError`. `establish_entity_classes` passes the options
  a MetaSchema entity row records (`meta.options` or definition keys) to
  `register`. Gram switches already converged in every direction through the
  adapter's fingerprint (prune on unbind, rebuild from rows on bind); the two
  previously untested directions are now pinned.
- Changed: `DatabaseVectorType` decodes bytes as a packed float64 payload only
  when the bound dialect selects packed storage; on a JSON-text dialect bytes
  are decoded as JSON text and a packed payload raises `ValueError` instead of
  decoding as garbage. `SQLBackend._construct` evicts least-recently-used
  shapes (a hit moves the construct to the back). `audit()` is documented as
  comparing Catalog rows only, never keyword rows.
- Documented: the stored-registration reconcile, the no-rollback recovery
  (ordering plus `audit()`/`repair()`), what `audit()` covers, and the one-way
  SQLite vector change with the `json_text` opt-out in `CHANGELOG.md`,
  `public-api.md`, `id-semantics.md`, `database.md`, and `data-flows.md`.
- Verified: `tests/backends/test_scan_page.py` (JSON and Pickle backends
  reopened cold, `scan_page`, `iter_scan`, `ws.iter_rows` after `refresh()`,
  another writer's file seen), `tests/workspace/test_workspace_entity_options.py`
  (full → identity → inherited → full → identity → none → full across
  processes with keyword rows, delete, and truthful audit; a store without
  recorded options; a Catalog delete over stale keyword rows; cold
  `gram=False` then live `gram=True`; MetaSchema ingress with recorded
  options), `tests/backends/test_sql_statement_reuse.py` (LRU order),
  `tests/backends/test_sql_vector_storage.py` (bytes as JSON text, packed
  bytes refused on `json_text`), `tests/workspace/test_workspace_rows.py`
  (identity delete statements include the keyword cleanup); the targeted
  backends/workspace/core/query suites with all markers, the fast suite,
  Black, Flake8, and docs checks.
- Next: none

## 2026-09-10 — Repair of the combined entity-reads, options, statement-cache, and projection series

- Task: independent verification of the four combined patches found one test
  regression, one deadlock in a new public API, and four medium or low
  findings; this entry closes them before the series lands. No new default
  changes; the SQLite packed-vector default stays.
- Changed: `Backend.scan_page(entity_id, after=..., batch_size=..., columns=None)`
  is the keyset page primitive of the backend contract. SQL implements it with
  the cursor and page size bound as parameters, in-memory as a sorted-key
  slice under the lock, and the base class drives `iter_scan` to one page
  under the lock and closes the generator (documented as one enumeration per
  page). `_RowStore.iter_row_pages` fetches each page through it, so
  `HeavenBase.iter_rows` never leaves a backend lock held across a `yield`;
  the previous in-memory route deadlocked a reader paused between pages
  against a writer thread. SQL `iter_scan` is now `scan_page` in a loop.
- Changed: the `SQLBackend` construct cache is insertion-ordered and bounded
  (`_construct_cache_size = 512`, oldest evicted), shapes name column
  selections only, `_reset_storage_state` and the readers tolerate an
  instance built without `__init__`, and the bootstrap-resolver clear runs
  under the backend lock.
- Changed: a Catalog mode change rewrites rows under whichever registration
  carries full maintenance (`reconcile_entity(entity_cls, mode=...)` runs
  before the switch when leaving `full`, after it when entering `full`), so a
  write that lands after a failed page still maintains the keyword index;
  the failure is a chained `RuntimeError` naming the registration kept, and
  `audit()`/`repair()` finish the rows. Sparse-GRAM `prune` no longer creates
  the index tables for an entity that never had them. `vector_packed_type`
  accepts `json_text` and is a per-database `DBSpec` field (backend kwarg,
  backend config mapping, `Database(...)`, or
  `heavenbase.db.providers.<provider>.vector_packed_type`) that overrides the
  dialect record; `SQL_TYPE_JSON_TEXT` is exported.
- Documented: the one-way SQLite vector format change and the opt-out in
  `docs/resources/reference/database.md`, `data-flows.md`, and `CHANGELOG.md`;
  `scan_page` in `public-api.md`; identity mode dropping the workspace
  signature and the rewrite-failure contract in `register`'s docstring and
  `id-semantics.md`; the reserved `atomic` keyword of `upsert`.
- Verified: `bash scripts/test.bash tests/backends/test_backends.py -m "" -q`
  (the regressed lifecycle-tag test passes), new contracts in
  `tests/backends/test_scan_page.py` (portable default, in-memory, SQL pages
  equal `iter_scan` and `scan`), `tests/workspace/test_workspace_rows.py`
  (two-thread reader/writer completes), `tests/backends/test_sql_statement_reuse.py`
  (200 page sizes keep two shapes; 300 selections stay under the bound and
  read correctly after eviction), `tests/workspace/test_workspace_entity_options.py`
  (failed page in both directions, then repair), `tests/backends/test_sql_vector_storage.py`
  (`json_text` per backend and per provider, bad token refused),
  `tests/query/test_sql_sparse_gram.py` (statement capture shows no
  `hb_gram` statement or table for an inline-only entity); the fast suite,
  the backends/query/core/execution/workspace suites with all markers, Black,
  Flake8, and docs checks.
- Next: none

## 2026-09-10 — Per-entity Catalog and keyword-index opt-outs; one-transaction upsert

- Task: direct request from a consumer that writes entities it never
  discovers through Catalog text; measured on its SQLite hot path, about half
  of one write's statements were the Catalog merge `SELECT`, the projected
  Catalog row, and the sparse-GRAM keyword maintenance for it. Defaults are
  unchanged; every new behavior is an opt-in registration or call option.
- Changed: `HeavenBase.register(entity, *, catalog=None, gram=None)` records an
  `EntityOptions(catalog="full"|"identity"|"none", gram=True|False)` value in
  `ws.entity_options[entity_id]`. `identity` publishes an identity-only Catalog
  row without the merge read and without strategy maintenance; `none`
  publishes no Catalog row; `gram=False` re-places the entity's
  `Array[ShortText]` fields inline (an explicit `SparseGramIndex` placement is
  rejected) and `contained_in` on them compiles to an unsupported fragment that
  raises `LookupError` instead of scanning. `None` inherits the live
  registration, then the definition's `catalog`/`gram` keys, then the defaults;
  a different value needs `allow_replace=True`, and a changed `catalog` then
  rewrites or deletes the entity's stored Catalog rows through
  `_CatalogAuditService.reconcile_entity`. Options travel with the entity
  definition in manifests (`catalog`/`gram` keys), replay through
  `from_manifest`/`HeavenBase.load`, and appear in the MetaSchema entity row's
  `meta.options`. Audit and repair follow the same modes.
- Changed: `RowOp.strategies` (default `True`) lets a write skip strategy
  adapters; `StrategyAdapter.prune(schema, bindings)` is called by the SQL
  backend before `ensure`, and the sparse-GRAM adapter drops the keyword rows
  and fingerprint of fields no longer bound to it, then rebuilds a field that
  has rows but no fingerprint, so `gram` switches converge in-process and across
  reopen. Catalog deletes always run strategy maintenance, whatever the
  Catalog mode; `DeleteRoute` names only a backend and an entity.
- Changed: `upsert(..., atomic=True)` and `upsert_many(..., atomic=True)` send
  the entity and Catalog operations of one backend through one
  `Backend.upsert` call (one SQL transaction) in the same owner-then-replica,
  entity-then-Catalog order; a plan that spans backends raises before writing,
  so the documented cross-backend ordering is never weakened.
- Verified: `tests/workspace/test_workspace_entity_options.py` (option
  precedence, replace gating, gram placement, discovery of `none` rows,
  identity row shape, catalog-mode reconcile, gram switch across reopen,
  manifest/definition/MetaSchema round trip, durable reload);
  `tests/workspace/test_workspace_rows.py` statement captures for full,
  identity, none, and `gram=False` writes and deletes plus one-commit atomic
  writes and the multi-backend refusal; `tests/query/test_sql_sparse_gram.py`
  adapter opt-out and prune/rebuild; `tests/placement/test_strategy_adapter.py`
  `RowOp.strategies` and the `prune` default; the existing `tests/core`,
  `tests/workspace`, `tests/query`, `tests/placement`, and `tests/backends`
  suites; Black, Flake8, and docs checks.
- Next: none

## 2026-09-10 — Entity-known row reads: public decoded `read_rows` and `iter_rows`

- Task: direct request from a consumer whose writer always names the Entity
  and reads rows on its hot path; it had reached for the private
  `_lock`, `_need_live`, and `_row_codec` members to skip the facade. No
  protocol, storage-format, or default change.
- Verified first: on this base `get(id, entity=...)` and `get_many(...,
  entity=...)` already issue one statement per routed backend and never read
  `sys_catalog`; only the entity-less read consults Catalog and every other
  present entity. That contract is now pinned by SQL-statement capture tests.
- Changed: `HeavenBase.read_rows(entity, object_ids)` returns decoded logical
  rows aligned to the input without Catalog; `HeavenBase.iter_rows(entity,
  batch_size=128)` streams decoded rows through the routed backends' keyset
  scan, holding the read lock and liveness check per page and releasing them
  before yielding, so writers are admitted between pages and the iteration is
  documented as non-snapshot. Both validate the entity and page size at the
  call. `_RowLookupService.entity_matches_many` now owns the entity-known
  path for `get`, `get_many`, and `read_rows`; `_RowStore.iter_row_pages`
  yields the merged pages that `iter_rows` flattens; `workspace_read_scope`
  and `workspace_read_pages` factor the read-lock discipline out of
  `workspace_read_locked`.
- Documented: with `entity` given, `unique` governs only disagreeing routed
  fragments of one row (`True` raises, `False` keeps the first route); without
  it, `get` raises on a cross-entity duplicate whatever `unique` says, while
  `exists` honors `unique=False`. The `get`/`get_many` docstrings and
  `docs/resources/reference/id-semantics.md` now state that behavior, which
  is unchanged.
- Verified: `tests/workspace/test_workspace_rows.py` fast contracts for
  no-Catalog reads by class, identifier, and instance, decode parity of
  `read_rows`/`iter_rows` with `get` for Json, Timestamp, Vector, and optional
  fields, per-page lock release and non-snapshot visibility, eager validation
  and per-page liveness, and `unique` fragment-conflict semantics; the
  existing `tests/core`, `tests/workspace`, `tests/query`, and
  `tests/placement` suites; Black, Flake8, and docs checks.
- Next: none

## 2026-09-10 — Column projection on row reads; SQLite vectors stored as packed float64 blobs

- Task: direct request from a downstream consumer, which measured every
  row read decoding the complete row — including a 2048-float Vector parsed
  from JSON text and re-validated — when its write planning needed only
  identity and ordering fields (about 0.48 s per 128-row page). Two
  independent changes; the vector change is a storage-format change for
  SQLite only, with read compatibility for existing rows.
- Changed: `Backend.get_many(..., columns=None)` and
  `Backend.iter_scan(..., columns=None)` are part of the backend contract. A
  selection returns `object_id` plus the named fields the backend stores;
  `SQLBackend` pushes it into the SELECT list (main columns, side-table
  aggregates and their presence markers, never an unrequested Vector) and keys
  its reusable constructs per selection, while every other bundled backend
  trims the complete fragment in Python through the shared
  `Backend._project_rows` helper. `HeavenBase.get` and `get_many` accept
  `columns=` with `entity=` required; the lookup service validates the names
  against the schema, the row store asks each routed backend only for the
  requested fields it owns (a route owning none still answers with identity,
  so presence is judged exactly as before), and the schema-version check
  covers only the requested fields. `_RowStore.iter_rows` threads the same
  selection so a public decoded iterator can expose it. `Query.select` is
  unchanged: the router evaluates filters, near scoring, hydration, JSON-path
  derivation, ordering, and schema completion on complete rows before
  `frame.project`, and those stages need columns the selection does not name.
- Changed: `DatabaseDialectSpec.vector_packed_type` (token `float64_blob`,
  exported as `SQL_TYPE_FLOAT64_BLOB`) selects packed little-endian IEEE-754
  float64 storage for Vector columns when no native vector type exists. The
  SQLite dialect record and its bootstrap copy declare it, so new SQLite
  Vector columns are BLOB and new writes are packed; `DatabaseVectorType`
  reads packed bytes, legacy JSON text, and native arrays through the same
  column, so existing workspaces keep working and a rewritten row moves to the
  packed form. float64 was chosen over float32 because the inline copy is the
  authoritative one (`_soft_vector_fields` treats vector-store replicas as
  lossy), the JSON path round-tripped doubles exactly, and exact near ties
  depend on identical scores. Other dialects are unchanged: their existing
  TEXT/JSON vector columns cannot accept binary payloads without a migration.
- Verified: `bash scripts/test.bash tests/backends/test_sql_projection.py
  tests/backends/test_sql_vector_storage.py tests/query/test_near_vector_storage.py
  tests/workspace/test_workspace_projection.py tests/core/test_database.py -q`
  — statement capture proves projected lookups and keyset pages select only
  the requested columns (side-table markers included, vectors excluded) and
  compile zero times after warm-up; ten awkward doubles round-trip bit-exactly
  through a `typeof() = 'blob'` column; JSON-text rows read next to packed
  rows; exact near ranks and scores are identical across the two storage
  forms for cosine, l2, and ip including exact ties; workspace reads send
  each route only its owned selection. On a temp-root SQLite workspace with
  512 rows carrying a 2048-float Vector, measured while the full suite ran
  concurrently: `get_many` of 128 rows 224 ms → 3.5 ms projected to two
  scalar fields, keyset `iter_scan` of 512 rows 950 ms → 19 ms projected, and
  the complete read with the vector 457 ms (JSON text) → 224 ms (packed).
  `bash scripts/test.bash tests/backends tests/query tests/core tests/workspace
  tests/registry tests/config tests/execution -q` — 838 passed, 16 skipped;
  `bash scripts/flake.bash --ci`, `scripts/gen_capabilities.py --check`, and
  `scripts/docs.py check` are clean.
- Observed, not changed: SQL `upsert_many` executes one `INSERT` per batch
  with the first row's keys, so a batch whose rows omit different optional
  fields fails in SQLAlchemy's executemany key check; this predates the
  statement-reuse change. Inline SQLite vectors have only the exact near
  route (`exact=True`); non-exact near has no handler there.
- Next: none

## 2026-09-10 — SQL statements compile once: no per-execution stringification, reusable backend constructs

- Task: direct request from a downstream consumer, which measured three
  SQLAlchemy compiles per HeavenBase execution (8,374 compiles for 2,795
  executions in one plan) plus a full config fingerprint of the database spec
  on every engine lookup. No protocol or storage-format change.
- Changed: `Database` execution no longer calls `str(statement)` on the success
  path. `is_sql_readonly` classifies SELECT, DML, and DDL constructs
  structurally (a SELECT that nests a data-modifying CTE is still rejected),
  `text()` clauses use their raw text, and only unknown constructs fall back to
  rendering. Schema-index invalidation skips SELECT and DML constructs instead
  of compiling them to read the first token. `SQLResponse.from_cursor` and
  `from_error` accept `statement=`; `SQLResponse.query` renders it on first
  access, so diagnostics keep the same text without paying for it up front.
- Changed: `ConfigSpec._cache_identity` memoizes the behavioral fingerprint of
  one immutable spec; `ConfigEngine.resolve`/`apply` key their caches on it, so
  `DatabaseEngineRegistry.key` (the `cache-key` profile) stops re-walking the
  spec on every `Database.engine` access.
- Changed: `SQLBackend` keeps one construct per `(entity, shape)` for the
  column projection, `get_many`, `exists_many`, `count`, `scan`, keyset
  `iter_scan` pages, the upsert delete/insert pair, and side-table
  delete/insert; ids travel as expanding bind parameters and the keyset cursor
  as a bind parameter. Constructs are dropped after `ensure`, on storage reset,
  and when the bootstrap resolver is finalized. Query filters still build their
  predicate per call and rely on SQLAlchemy's compiled cache as before.
- Verified: `bash scripts/test.bash tests/core/test_database_statements.py
  tests/config/test_config_spec.py tests/backends/test_sql_statement_reuse.py
  tests/backends tests/execution -q` — 172 passed, 9 skipped. New contracts
  count `SQLCompiler` constructions and `ClauseElement.__str__` calls: repeated
  lookups, scans, and writes compile zero times after warm-up, the read-only
  guard classifies constructs without compiling, DDL still invalidates the
  schema index, and a replaced schema rebuilds the projection. Under a heavily
  loaded host the sequential probe showed `get_many` 4.6 → 1.7 ms,
  `exists_many` 3.2 → 0.6 ms, and one-row workspace upsert 57 → 16 ms; the
  compile counters (900 → 0 per 300 reads) are the stable evidence.
- Next: none

## 2026-09-06 — Explicit-consumer hardening: lazy default Context, shared reads, safe defaults

- Task: direct request from a consumer that owns its own root Context and
  must never touch the personal bootstrap or package-home databases. Four
  independent commits; no protocol or storage-format change.
- Changed: `heavenbase.llm.cache.enabled` and
  `heavenbase.dashboard.assistant.session_log` ship `false`; both opt-in
  paths and per-client `cache=True` are unchanged.
- Changed: `Timestamp.validate(None)` raises instead of substituting the
  current time; nullable fields declare `.optional()` and "now" is passed
  explicitly.
- Changed: `DEFAULT_CONTEXT` is constructed on first use through a module
  `__getattr__` and `default_context()`; importing HeavenBase no longer reads
  `~/.heavenbase/bootstrap.yaml`. `heavenbase.context.set_default_context`
  installs an explicit root Context before first use and is refused after.
  `CM_HVNB` persistence still binds through the default Context when that
  Context owns it; identity checks against the default never construct it.
- Changed: the workspace lock is a reentrant reader-writer lock. Mutations
  keep the exclusive side plus the Context lock; reads (`get`, `query`,
  `search`, `count`, `describe`, `health`, MCP/skill export, …) share the
  lock, run concurrently across threads and workspaces, and never take the
  Context lock. A read can nest a write of the workspace it reads. The near
  candidate cache
  owns its own bookkeeping lock so concurrent readers cannot corrupt it.
- Verified: default integrity suite passed on each commit; targeted contracts
  added for import purity under an invalid personal bootstrap, default
  installation, shared reads under a held Context lock, writer/reader
  exclusion, default-off session log on a pristine ConfigManager, and
  `Timestamp` None rejection. Black, Flake8, docs and environment sync
  checks passed through the pre-commit hook.
- Next: none

## 2026-09-05 — Native Milvus searches honor current-data visibility

- Task: direct request from a downstream consumer that searches data right
  after writing it. No publication workflow or process supervisor is added
  to HB.
- Changed: native Milvus searches request Strong consistency for both ordinary
  and exact FLAT execution, matching existing scans and point reads. Documented
  the distinction between HB cache invalidation and provider-local index state.
- Verified: two focused native-search contracts passed; the default integrity
  suite passed 1,066 tests with 15 environment/optional-provider skips. Black,
  Flake8, docs and environment/README sync checks passed. Validation against a
  real Milvus service belongs to the consumer.
- Packaging: no dependency change; a lock re-sync made here was later
  reverted, and `uv.lock` matches `master`.
- Next: none

## 2026-09-05 — Unified HB retrieval with explicit candidate reuse and streaming

- Task: direct request from a downstream consumer that replaces its own vector
  pool engines with HB queries; exactness and optional unchanged-generation
  identity are declarations on existing HB queries. Business scope and fusion
  remain with the consumer.
- Changed: `near(exact=True, cache_version="knowledge-v1")` permits workspace-owned
  candidate and binary64 matrix reuse, never result caching. Ordinary queries
  remain live. Owned writes, schema replacement, refresh, release and drop
  invalidate; external writes require a new token or refresh. Retention is
  bounded by bytes/entry count with LRU eviction and no partial publication.
- Changed: SQL keyset pages, Chroma pages, Milvus query iterators and InMem
  iteration feed bounded cross-route hydration and Top-K batches. Unsupported
  streaming fails explicitly. Public results remain isolated; whole-entity
  preparation cannot make excluded invalid vectors fail an otherwise valid
  query. Registry-publication-aware SQL filter metadata reuse removes repeated
  Tag materialization without caching data visibility.
- Verified: default gate 1,067 passed / 1 NO_COLOR skip; query/execution follow-up
  165 passed; final cache/batch/metadata gate 34 passed. Black/Flake8,
  environment/README-sync and docs checks pass. The consumer's full regression
  passes 1,880 tests, and six real SQLite+Chroma/Milvus Lite examples pass.
- Evidence: 818 offline comparisons, 7,224 ranked rows and 36 complete tool
  outputs match the consumer's previous float32 engine within declared
  tolerances, without ranking changes. Warm latency improved substantially over
  uncached unified execution but stays above that engine's, so this is not
  performance equivalence. 1,024→8,192-row no-cache scans retain about 2.2 MB
  Python-traced peak working memory. Full Python candidate rows still make the
  optional cache larger than float32 matrices alone.
- Boundary: no historical/cross-backend snapshot or distributed cache is added.
  Native FLAT stays native; exact scans still cost O(N × dimension). This task
  made no model calls, and no remote-provider production acceptance is implied.
- Next: none

## 2026-09-05 — Exact intent belongs in the existing query pipeline

- Task: direct request from a downstream consumer.
- Changed: Python/JSON `near(..., exact=True)` reuses registered handlers,
  existing metric functions, and scan execution. Complete filters precede
  Top-K; approximate handlers fall back exhaustively. Milvus FLAT is explicit
  and rechecked against the live index. Explain exposes the contract and route.
  Unfiltered near no longer performs an unused universe scan. No result cache
  or parallel query API is added. An unmerged public vector-pool experiment
  was withdrawn; the consumer keeps its own float32 compatibility policy.
- Verified: 220 fast query/public-API/provider tests, 18 detail query/replica
  tests, and one additional server index-configuration contract, including real Milvus
  Lite FLAT search with scans forbidden, cross-backend exact fallback, and
  index-drift rejection. This is not remote Milvus server acceptance, a
  downstream accuracy result, or a concurrency/snapshot guarantee. All 617 Python source/stub
  members match the wheel and source archive; the removed pool is absent.
  The consumer's full regression passed 1,852 tests; its comparison with its
  previous retrieval covers 326 cases, 6,384 ranked rows (zero score delta), and 36 identical tool outputs.
- Next: none

## 2026-08-24 — Assistant session log: model-visible means logged

- Task: direct request — durable, append-only session logging for the
  dashboard assistant (design plan externally double-audited before
  implementation).
- Changed: `LLM.chat/achat/stream/astream` gain a `trace` observer emitting a
  canonical request event before every provider call (rounds, in-turn
  compaction, wrap-up) plus paired terminals; new `agent-trace` entity in the
  agent extension (moved on 2026-11-20 to the optional `agent-log` extension
  as `AgentTrace`); `api/assist_log.py` recorder with write-ahead idempotent
  event rows, session projection, retention cap, and `rebuild_session`;
  `session_log` config block with boot validation (durability, credentials at
  rest); `conversation_id`/`turn_id` through models/HTTP/OpenAPI/TS/GUI with
  turn-id reuse on the streaming→blocking fallback; `capture_warning` on
  degraded recording.
- Verified: `tests/llm/test_llm_trace.py` (10) and
  `tests/interfaces/test_assist_session_log.py` (12) including the
  reconstruction acceptance test — every provider view, including repeated post-compaction
  views, rebuilds byte-for-byte from log rows alone; dashboard (97),
  llm session tools (21), extensions (33) suites green; GUI
  format/lint/typecheck/build green (bun test left to CI — no bun on the
  build host).
- Hardened: three independent adversarial reviews of the same brief
  converged on four blockers, all fixed — credential
  exclusion from canonical requests, precise duplicate-vs-validation write
  handling with 128→63 id normalization, attempt-namespaced fallback retries
  with sanctioned turn-row supersede, and active-conversation-safe retention
  with orphan sweep — plus per-turn config re-validation, run_server-boundary
  boot checks, and capture_warning on the correct protocol model.
- Follow-up hardening: completed same-turn retries now replay the persisted
  answer without provider I/O; an interrupted request with no turn row resumes
  under a fresh attempt namespace. Canonical request fingerprints also retain
  the safe thinking controls that the gateway compiles into provider payloads.
- Handoff: `llm-session-persistence.md` records the resolved authority model
  (events authoritative, projections rebuildable); Phase 2 (log-derived
  replay, evidence-LRU retirement, resume/fork) intentionally out of scope.
- Default flip (operator decision, reverted on 2026-09-06): `session_log` ships ON — model-visible
  means logged is the stock posture; `session_log: false` is the single
  opt-out. The default-off zero-change test became an explicit-off test plus
  a pristine-context default-on acceptance (a `CM_HVNB.unset` writes a layer
  tombstone that masks packaged defaults, so default-value tests must build
  their own ConfigManager).
- Next: none

## 2026-08-21 — Converged dashboard assistant extension seams

- Task: direct request — review and repair PR #91 on the reviewed PR #90
  predecessor to the Heaven style standard.
- Changed: schema-first protocols can attach an explicit JSON schema to a plain
  callable, installed modules can contribute ordinary assistant tool providers
  through the Registry, and configured external MCP servers enter through a
  shared FastMCP runtime-callable adapter. The final design removes the proposed
  dashboard-owned MCP protocol client, serve-time configuration mutation and
  model-ID rewrite, workspace-group activation axis, and parallel surface-policy
  DSL; normal providers, tool metadata, workspace requirements, and dashboard
  collision/evidence policy remain the compositional authorities.
- Verified: the fast integrity tier passes with 981 tests and 10 optional-backend
  or environment skips; complete dashboard and LLM tool-session files pass with
  120 tests. FastMCP Toolkit/runtime import regressions, Black/Flake8,
  documentation, environment/README sync, and lazy-facade type contracts pass.
- Next: none

## 2026-08-21 — Converged dashboard assistant resilience and package setup

- Task: direct request — review and repair PR #90 to the Heaven style standard.
- Changed: assistant streaming now has bounded inactivity handling, explicit
  per-call tool/context policy, scoped evidence replay, cached prefix
  compaction, and matching sync/async LLM behavior. Generic LLM defaults remain
  neutral and the dashboard no longer probes providers in the background.
  Packaged-module setup now reconciles exact composition while treating package
  versions as opaque identity instead of encoding ordering or development-build
  exceptions.
- Verified: the fast integrity tier passes with 975 tests and 10 environment
  skips; the complete 118-test LLM slice also passes. GUI
  format/lint/typecheck, 9 GUI tests, generated JavaScript build, Black/Flake8,
  documentation, environment, README, and OpenAPI drift checks pass.
- Next: none

## 2026-08-10 — Prepared the 0.1.2.3 general update

- Changed: the optional Database extension now exposes seven standalone,
  searchable context entities, selective schema/profile import, guarded live
  SQL, entity editing, verified snippets, and natural-language rules. Any
  workspace can export a designated MCP profile as an atomic credential-free
  `WorkspaceSkill` and serve that same profile through the parser-neutral CLI.
  Dashboard profile metadata can declare structured evidence so SQL, JSON-safe
  result rows, truncation, and analysis stay aligned across blocking, streaming,
  OpenAPI, and UI boundaries. Live source credentials remain local and redacted,
  and the Memory extension remains a separate authority.
- Proved: temporary local and external SQL fixtures exercised selected
  table/column ingestion, profile preservation, schema and value discovery,
  read/write MCP context operations, rule retrieval, read-only query execution,
  save-after-success SQL memory, clean-context skill use, and persisted-workspace
  reopen. No source-specific skill or connection data is packaged.
- Verified: focused Database, MCP, WorkspaceSkill, CLI, dashboard, HTTP, and GUI
  contracts pass; the fast integrity tier passes with 967 tests and one
  environment skip. Black/Flake8, documentation, generated README/environment
  and OpenAPI drift, strict TypeScript, GUI format/lint/tests/build, skill
  validation, source/wheel build, and all 39 packaged module-parity checks pass.
- Next: none

## 2026-08-10 — Reworked the dashboard configuration workbench

- Changed: grouped provider/model/preset/gateway/default/database fields into
  consistent sections; added searchable references with visible inherited
  values, environment-first credential/location controls that show the actual
  source and masked final value, structured provider model-ID mappings, dedicated
  thinking/effort controls, progressive default-argument rows, and a compact
  animated Config/JSON switch. JSON mode uses pinned vendored CodeMirror with
  editor-local copy, format, validation, and save actions. The assistant chat now
  exposes searchable per-turn preset selection with model/reasoning hints and a
  server-enforced read-only switch. Follow-up convergence made provider backend
  routing a read-only effective summary across every gateway, removed the scope
  selector and synthetic assistant preset, moved preset/access controls into a
  Codex-style composer defaulting to `chat`, and replaced the clear-history
  glyph with a new-conversation edit glyph. Generic configuration pages now use
  the same section grammar as LLM pages; built-in database provider definitions
  cover all concrete SQL backends and all bundled backend types have stable
  brand or semantic icons.
- Verified: GUI format, vendor/API drift, lint, typecheck, unit, build, direct
  browser-behavior, dashboard interface, Flake8/Black, documentation, source
  distribution, and wheel-asset checks pass.
- Next: none

## 2026-08-10 — Accelerated asserted-fresh Catalog batches

- Cause: a 5,000-row typed write resolved presentation configuration about
  70,000 times: once per Catalog row for identifier length and once for each
  text-backed JSON column. Fresh databases also paid the merge-safe per-row
  Catalog lookup even though no prior projection could exist.
- Changed: `HeavenBase.upsert_many` now accepts an explicit
  `catalog_mode="fresh"` assertion that skips only prior-Catalog lookup while
  retaining Entity materialization, derives, route planning, backend ordering,
  graph validation, and Catalog writes. Merge remains the default. Catalog hash
  length is resolved once per batch, and SQLite JSON fallback storage now uses
  the compact persistence encoding without consulting presentation indentation.
  The ID-semantics reference documents both modes and the caller obligation.
- Verified: the exact 20,000-row public workload fell from 16.155 to 3.298
  seconds (79.6%) and from 22,962,176 to 21,819,392 bytes with all Entity and
  Catalog rows retained. Focused CRUD/Catalog/SQL tests, fast and full tiers,
  Black, Flake8, documentation, and diff checks pass.
- Next: none

## 2026-08-09 — Added official MCP ToolAnnotations round-tripping

- Task: direct request — validate OfficeQA experiment feedback against the
  current HeavenBase/FastMCP 4 tree and integrate official tool-behavior hints.
- Changed: Added first-class, persistable `Tool.annotations`, exposed the same
  mapping through `Toolkit.add` and `Toolkit.tool`, round-tripped official MCP
  `ToolAnnotations` through FastMCP export/import, and projected backend tool
  read-only contracts to `readOnlyHint`. Documented the five advisory protocol
  fields and their conservative defaults.
- Verified: Four focused annotation/persistence/import tests and the 106-test
  MCP/Toolkit slice pass. Black, Flake8, documentation-contract, and diff checks
  pass; an in-process FastMCP 4 probe exposes `readOnlyHint` for both built-in
  backend inspection tools.
- Next: none

## 2026-08-09 — Prepared the 0.1.2.2 PR train for final merge

- Task: direct request — merge PRs #83, #84, #87, and #88 in order and unify
  HeavenBase at version 0.1.2.2.
- Changed: Sequentially merged #83, #84, and #87 into `master`, reconciled #88
  against that exact history, and promoted the package source, generated module
  compatibility headers, packaging metadata, changelog, and current-status
  documentation to 0.1.2.2. Version-sensitive tests now distinguish the live
  package version from deliberately pinned compatibility-boundary fixtures.
- Verified: The combined fast and full release gate passed with 2,095 tests and
  61 explicit external/environment skips. Fourteen focused version and Registry
  checks, Black, Flake8, MyPy, Heaven changed-source import scanning, dashboard
  formatting/typecheck/tests/build, documentation, environment/README sync, and
  generated-metadata checks passed. The 0.1.2.2 wheel and source distribution
  built successfully and passed Twine validation.
- Next: none

## 2026-08-09 — Prepared vector-replica row merging for merge

- Task: PR #88 merge-readiness review
- Changed: Integrated the prepared #84 → #87 stack and constrained soft row
  merging to true vector replicas that have a non-vector source of truth.
  Authoritative inline nulls now remain null, vector-only routes retain normal
  disagreement detection, and strategy matching uses the registered vector
  identifier without masking invalid workspace state. Near-query hydration now
  preserves the same field authority instead of restoring a stale vector value
  over an authoritative null.
- Verified: Eight direct regressions, 496 changed-surface tests, the 940-test fast
  suite, and 1,153 full opt-in tests passed. One fast environment case, seven
  focused external-service cases, and 60 full external-service cases were skipped
  by explicit policy. Black, Flake8, MyPy, Heaven changed-source import scanning,
  documentation, environment/README-sync, and generated-metadata gates passed;
  details are recorded in the PR review report.
- Next: none

## 2026-08-09 — Prepared exact aggregation and dashboard results for merge

- Task: PR #87 merge-readiness review
- Changed: Integrated prepared PR #84; preserved complete result totals across
  nested paging and serialization; retained parameterized reducer output types
  for result validation and typed `HAVING`; bounded Python-tool output during
  capture; bound caller-code permission to the executing workspace Context;
  emitted tool-budget exhaustion on default text streams; rejected invalid
  dashboard query limits; and made row ranges and JSON-statistics truncation labels reflect the exact returned evidence.
  Corrected stale capability documentation and regenerated the dashboard UI.
- Verified: After integrating the final prepared PR #84 head, 177 affected-surface
  tests, the 940-test fast suite, and 1,145 full opt-in tests passed; one fast
  environment case and 60 explicit external-service cases were skipped. Dashboard
  OpenAPI generation, formatting, lint, typecheck, tests, generated-JavaScript
  drift, and production build passed. Black, Flake8, MyPy, Heaven changed-source
  import scanning, documentation, environment/README-sync, capability-report,
  and generated-metadata gates passed; details are recorded in the PR review report.
- Next: none

## 2026-08-09 — Prepared Catalog projection isolation for merge

- Task: GitHub issue #81
- Changed: Merged current `master`; same-named application fields now project implicitly into Catalog
  only when their complete logical type descriptors match. Incompatible fields
  remain valid application data and leave Catalog defaults unchanged, avoiding
  late Catalog coercion failures such as `ShortText confidence="high"` entering
  the float-valued Catalog projection. Explicit derives remain the normalization
  path. Updated inherited test contracts for the Agent extension now enabled by
  default and retained scoped Agent-free coverage where that is the behavior
  under test. Clarified that an incompatible native `name` may seed fallback
  identity without projecting into Catalog, and restored dashboard assistant
  config after its invalid-model contract test.
- Verified: Fifteen focused Catalog, default-Agent, and isolation regressions
  and the 905-test fast suite passed with one environment skip. Black, Flake8, MyPy,
  documentation, environment/README-sync, and generated-metadata gates passed;
  details are recorded in the PR review report.
- Next: none

## 2026-08-09 — Prepared PR #83 for merge

- Task: HB-101
- Changed: Merged current `master` into the callable-restoration fix and
  preserved both durable log histories. Kept resolvable callable references
  separate from Context-independent structural fingerprints, aligned inherited
  tests with Agent's configured default enablement, scoped tests that require an
  Agent-free workspace, and restored dashboard assistant config after its error
  contract test.
- Verified: The 14 Agent-extension contracts, 12 inherited default-Agent and
  isolation regressions, two focused structural-change guards, and the
  905-test fast suite passed with one environment skip. Black, Flake8, MyPy,
  documentation, environment/README, and generated-metadata gates passed.
- Next: none

## 2026-08-04 — Keep an absent list distinct from an empty one on a side table

- Task: direct request — the SQL backend read a null Array as `[]`, so a null
  count over a side-routed column was wrong. Fix it here rather than leave it.
- Cause: a side table holds one row per item, so an absent value and an empty
  list both store nothing. Both ends erased what was left of the difference —
  the write coerced `None` to `[]` before lowering, and the read decoded an
  empty aggregate back to `[]`. No amount of changing one end helps; the
  representation had nowhere to put the distinction.
- Changed: the main row now carries one presence marker per side-routed field.
  A marker of `0` reads back as `None`; anything else decodes the side rows,
  including to `[]`. Rows written before the column existed have no marker and
  are treated as present, so stored values keep reading exactly as they did.
  The column name is prefixed with an underscore, which `check_py_name` rejects
  for declared fields, so it cannot collide with one.
- This was not introduced by the statistics rewrite — `master` reports the same
  wrong null count from its own scan — but `column_stats` is what surfaced it,
  and a null count that is wrong on one backend and right on another is exactly
  the class of defect this branch exists to remove.
- Verified: `bash scripts/test.bash tests/backends tests/placement
  tests/execution -m ""` — 435 passed; the three failures are pre-existing
  `master` breakage, confirmed by running them against a `git archive
  origin/master` export. New coverage:
  `test_side_table_keeps_an_absent_list_distinct_from_an_empty_one` asserts
  inmem and SQLite agree on all three shapes and that the aggregate null count
  is right.
- Next: none

## 2026-08-03 — Register the column-summary folds so statistics need no permission

- Task: direct request — `column_stats` should not depend on the switch that
  exists to refuse caller-authored code.
- Changed: the seven summary functions were reached through a `code_reduce`
  expression that did nothing but import and call them, so an operator refusing
  caller code — which those functions are not — also lost every column
  statistic, exactly as `default.yaml` invited them to. They now move to
  `heavenbase/query/summary.py` and register as one named `column_summary`
  reducer taking the statistics `kind`; naming a kind it cannot summarize raises
  rather than returning an empty payload. Summarizing a column is a query
  capability, so the query layer owns it and the record sits beside every other
  reducer instead of pointing back into a caller.
- The switch is meaningful for the first time. Before, it gated repository code
  and caller code through one door and had no usable position: on refused
  nobody, off broke a feature. It now answers exactly one question — whether
  whoever can reach the `query` tool may execute code as this process — while
  the ten named reducers, the whole declarative spec, and `column_stats` keep
  working underneath it.
- Verified: `bash scripts/test.bash tests/interfaces tests/query tests/registry
  -m ""` — the only failures are the pre-existing `master` breakage recorded
  below. Registry pins move by the usual two records per operator
  (`package_rows` 306 -> 308, `claims` 231 -> 233); the definition fingerprint
  is unchanged, which is the guard that matters. New coverage:
  `test_column_stats_survives_refusing_caller_authored_code` disables the switch
  and asserts both halves — statistics still computed, a caller's expression
  still refused. Five static gates pass.
- Next: none

## 2026-08-03 — Teach the assistant code_reduce rather than hiding it

- Task: direct request — keep the operator available to the model and steer it
  in the prompt instead of closing it off.
- Changed: the assistant's system prompt never mentioned `code_reduce`, so the
  model could not use it and could not be warned about it. It now names the
  operator with a worked example, states that the engine hands the expression
  every non-null value so coverage matches any other aggregate, and says plainly
  what a hand-written fold gives up: a registered op inspects the payload and
  raises on a mismatch, while an expression folds whatever it is handed — a
  Boolean summed as 1, or a list where an object was assumed, yields a plausible
  wrong number and no error. The op list also grew the ops that were already
  registered but unnamed there (`distinct`, `median`, `percentile`), with a note
  that one aggregate carries many outputs, so the model asks once and does
  arithmetic on exact numbers rather than over rows.
- Also corrected stale guidance: the prompt told the model to read
  `keys_exact`/`elements_exact` to learn whether a distribution came from the
  engine. Since `column_stats` folds the whole column, that is now always true;
  the line instead points at `column_stats` for quartiles, histograms, length
  distributions, and time ranges, so the model reaches for it before computing
  any of them itself.
- Verified: `bash scripts/test.bash tests/interfaces/test_dashboard.py
  tests/interfaces/test_column_summaries.py -m ""` — 83 passed, one pre-existing
  `master` failure. New coverage:
  `test_assistant_guidance_examples_actually_run` executes both specs the prompt
  quotes and asserts every op it names resolves through the reducer registry, so
  guidance cannot drift from the engine without a test failing.
- Next: none

## 2026-08-03 — Fold column statistics inside the engine instead of paging a sample

- Task: direct request — `column_stats` should compute over every row, not a
  scanned prefix.
- Changed: the paging loop is gone. Each dtype kind now folds through one
  aggregate query, so the engine feeds the fold every value of the column and
  only the summary travels back. The statistics themselves moved to
  `heavenbase.api.stats` as ordinary functions — `numeric_summary`,
  `bool_summary`, `time_summary`, `vector_summary`, `json_summary`,
  `text_summary` — so they are linted, typed, and unit-tested rather than living
  inside expression strings; the `code_reduce` expression only names which one
  to run. A single aggregate carries the fold beside a `count`, which is where
  the null count comes from: the engine drops nulls before a fold, so no fold
  can see them.
- The old bound cost more than it saved. `limit`/`offset` apply after
  `universe()` has scanned and sorted the whole table, so each 1,000-row page
  re-read everything — up to ten full reads to look at 10,000 rows. One
  aggregate is one scan, and the native reducers in it can push down.
- Removed the `sample` argument and its `ColumnStatsQuery` HTTP model: a knob
  that no longer changes the answer is the defect this work exists to remove.
  `scanned` now equals `total` and `sampled` is always `False`, both kept so a
  caller reading coverage need not know which release stopped sampling. The
  frontend also dropped `keys_truncated`/`elements_truncated`, flags it read but
  the service never set.
- Deliberate consequence: `column_stats` now depends on the fold being allowed.
  With `heavenbase.query.allow_code_reduce` disabled it raises and names the
  key, rather than answering from a sample — there is no partial answer left to
  give, and a raise cannot be mistaken for one.
- Verified: `bash scripts/test.bash tests/interfaces/test_dashboard.py
  tests/interfaces/test_column_summaries.py tests/interfaces/test_mcp.py -m ""`
  — 141 passed, and the four failures are the pre-existing `master` breakage
  recorded in the entry below. New coverage: 13 unit contracts in
  `tests/interfaces/test_column_summaries.py`, plus dashboard tests for the
  refusal path and the removed sampling knob. Five static gates pass; frontend
  typecheck, biome, and the committed `ui/js` freshness check are clean.
- Next: none

## 2026-08-03 — Rebase the Json aggregation work onto master, and close three unstated bounds

- Task: direct request — collapse the four stacked branches into one PR against
  `master`, then fix everything an audit of the result turned up.
- Changed: the eight commits now sit directly on `master`, replayed across two
  upstream rewrites — `#76` moved `dashboard/api.py` to `api/service.py` and
  replaced the dashboard JavaScript with TypeScript under `apps/gui/ui/src/`.
  The four frontend changes were ported to the typed sources, `SearchResponse`
  became an object carrying `{rows, n, total, truncated}`, `AssistFrame` gained
  `total`/`truncated`, and the OpenAPI document plus the generated
  `schema.d.ts` and `ui/js` build output were regenerated from them.
- Also closed three bounded results that did not state their bound: the MCP
  `query` tool never passed `near_bounded`, so an aggregate under a `near`
  clause counted its top-k window and read as a whole-entity count;
  `suggest_sql` reported a post-cap `n` with no `total`; and `column_stats`
  published 80-character example prefixes unmarked while deduplicating on the
  prefix, so two distinct long values sharing one silently collapsed into a
  single example. Value cells now elide through one helper, so `top`,
  `elements`, and `examples` all carry `value_truncated`/`value_len`.
- Also: `test_assist_scoped_model_error` left an unregistered assistant model in
  shared config, which made every later `assist` call raise. It now restores the
  keys in a `finally`, since a failing assertion is exactly when the leak
  happened.
- Verified: five static gates (flake, openapi, capabilities, docs, sync-env)
  pass, and each of the eight commits passes the capability gate individually.
  Frontend typecheck, biome format and lint, the OpenAPI regeneration check, and
  the committed `ui/js` freshness check are clean; `node --test
  tests/interfaces/dashboard_ui.mjs` is 20/20. New coverage:
  `test_query_tool_marks_a_near_bounded_result` and
  `test_column_stats_examples_never_quote_a_prefix_as_the_value`.
- Known: six tests fail on this branch and fail identically on `master`
  (`5d59408a` added `agent` to the default extension roots without updating the
  assertions that no agent extension is enabled by default). They are not this
  branch's to fix.
- Next: none

## 2026-08-03 — Enable code_reduce by default and let column_stats use it

- Task: direct request — reconsider the gate and have `column_stats` go through
  the operator an agent is told to use.
- Changed: `heavenbase.query.allow_code_reduce` defaults to `true`. The gate was
  justified by reach — the operator serves every caller, not only the local
  dashboard — and with code execution already an accepted capability that is
  not enough to make it opt-in. Setting the key to `false` still refuses it
  engine-wide. `column_stats` now tallies Json keys and list elements with
  `code_reduce` expressions instead of the `key_counts`/`value_counts` folds,
  so the endpoint exercises what agents are pointed at. Its payload is
  unchanged, and the existing scan fallback covers a workspace that refuses
  code, reporting that through `keys_exact`/`elements_exact` as before.
- The named folds stay. Writing the replacement expressions demonstrated why:
  the first version dropped the descending-count ordering that `key_counts`
  guarantees, and the test caught it. A fold's contract is not only what it
  computes but its ordering, what it skips, and what it refuses — `key_sums`
  rejects a Boolean that would otherwise total as 1, which a hand-written
  expression silently accepts. Those three names carry semantics a caller
  writing an expression each time will not reproduce, and the failure mode is a
  believable wrong number rather than an error.
- Verified: `bash scripts/test.bash tests/query
  tests/interfaces/test_dashboard.py tests/interfaces/test_mcp.py -m ""`.
- Next: none

## 2026-08-03 — Close a monkeypatch that escaped into seven module globals

- Task: direct request — resolve the three items the aggregation branches
  carried forward rather than leaving them as known gaps.
- Root cause, one defect behind two of them: a capability test installed a fake
  selector on the `heavenbase.modules` facade and, inside that window, made the
  first call to `hb.capabilities._default_index()`. That call imports
  `capabilities.index` in-function, which in a cold process transitively
  imports seven more modules — including `backends.registry` — each doing a
  module-level `from ..modules import module_resolver`. Every one copied the
  fake into its own globals permanently. `monkeypatch.undo()` restores the
  facade attribute and cannot reach a name already bound elsewhere, so
  `backends.registry.module_resolver()` kept returning a dead test double and
  the unbound-backend guard rejected every later workspace construction.
- The product is correct and unchanged. Module-scope `from x import y` copying
  a reference is ordinary import semantics; the test's blast radius reached
  modules that did not exist yet when it patched. The test now perturbs
  `DEFAULT_CONTEXT._modules` instead, which is also a truer simulation of the
  event it documents — a Context retiring its memoized resolver.
- This explains the six failures that disappeared when the `python` extension
  was renamed from `pyexec`: the rename changed how the parallel runner
  distributed tests, so the poisoning test and its victims landed on different
  workers. The symptom moved; the defect did not. They are fixed now, not
  hidden.
- Four hypotheses were tested and refuted along the way — `Context.close()`
  clearing the default resolver, the capability index memo leaking, the
  registry holding foreign authorities, and resetting that memo as a fix. The
  third looked decisive against the real cause: an instrumented print showed
  the compared identities were equal, because the probe read a freshly imported
  reference while the guard read its own poisoned module global. Same name,
  different objects.
- `BENCHMARK.md` now declares itself stale rather than being regenerated. It is
  a 2026-05-08 artifact from a different machine, and `docs/README.md` commits
  an updated report only when fresh results are part of the change — which
  these are not. The banner names the two figures the benchmark fixes above
  have since corrected, so a reader is not left comparing against numbers that
  contain the defect.
- Verified: `bash scripts/test.bash tests/capabilities tests/benchmark
  tests/scripts tests/core tests/registry -m ""` — 673 passed, in both parallel
  and sequential mode; the previously failing suites now pass together. A
  regression test scans loaded modules for a selector binding that is not the
  canonical one.
- Next: none

## 2026-08-03 — Carry a one-off reduction as code instead of registering it

- Task: direct request — registering a reducer needs an importable module-level
  function, which is right for a permanent operation and heavy for a question
  asked once.
- Changed: `code_reduce` carries the expression itself as an ordinary metric
  option, so nothing about the aggregate grammar changes — the op slot already
  chose the computation and the field slot already chose the column, and `code`
  sits beside `q` and `distinct` as one more reducer parameter. Compilation is
  cached by expression, since a grouped query folds once per group.
- Naming: the operator is `code_reduce` rather than `reduce` because it is the
  one operator that executes caller-supplied code. `{"reduce": ...}` reads like
  an ordinary aggregate; `{"code_reduce": ...}` says what it is, and an audit
  for code execution is one search. `fold` and `expr` were rejected for
  colliding with this codebase's existing words — `fold` is the engine's own
  term for the reduction step, and `Expr` is the filter expression tree.
- `dtype` declares what the output column is and the result normalizes into it
  exactly as a declared field normalizes a write, so an `Integer` column
  truncates rather than erroring. Omitting it leaves the output untyped, which
  the existing `dtype is None` paths already handle without a special case.
  Both a logical type and an identifier are accepted; the fluent surface
  normalizes to the identifier because metric options must stay JSON, so the
  two surfaces carry the same metric.
- Scope: the expression runs in the query process with no restriction, and
  unlike the dashboard's Python tool it would reach every caller — MCP, API,
  and CLI alike. `heavenbase.query.allow_code_reduce` gates it, off by default.
  A reducer record cannot be gated by `workspace_requires`, because
  `reducer_for` resolves through the module resolver rather than the
  contribution path that checks it, so the guard lives in the fold itself.
- Verified: `bash scripts/test.bash tests/query tests/registry/test_tags.py
  tests/interfaces/test_mcp.py -m ""` — 379 passed. Registry pins move by two
  records; the tag definition fingerprint is unchanged.
- Next: none

## 2026-08-03 — Canonicalized cross-Context artifact callable structure

- Task: GitHub issue #82
- Changed: Entity structural comparison now canonicalizes source-backed
  artifact callables by relative module, qualified name, and source hash rather
  than Context-local private package namespace. Public `hb.Prompt`, `hb.Skill`,
  and `hb.Agent` objects can therefore use a workspace's equivalent restored
  entity generation, while changed callable source remains incompatible.
- Verified: All 14 Agent-extension tests and the 896-test fast suite passed with
  10 expected optional-dependency/environment skips. Black/Flake8/MyPy,
  documentation, environment, README, and generated-metadata gates passed.
- Next: none

## 2026-08-01 — Updated DeepSeek V4 Flash provider identifiers

- Task: direct request — update DeepSeek V4 Flash routes and repair default CLI
  chat persistence
- Changed: Pointed OpenRouter flash and thinking-flash routes at
  `deepseek/deepseek-v4-flash-0731`, retained the official DeepSeek reasoner
  identifier, and corrected the official non-thinking identifier to
  `deepseek-chat`. Added `agent` to new workspaces' bundled default extension
  roots, and updated this machine's durable config plus its existing default and
  active workspaces.
- Verified: focused LLM contracts, formatting, and documentation checks pass.
- Next: none

## 2026-07-31 — Count Json list elements, and refuse a fold that found nothing

- Task: direct request — check the folds against the real trace data and the
  other Json columns beside `tool_calls`.
- Changed: running them over 1,916 real traces showed the pair was incomplete.
  Two of that entity's Json columns hold tag lists, not objects, and neither
  fold could read them. `value_counts` counts how often each element occurs in
  a list payload, completing the set: `key_sums` for values under a key,
  `key_counts` for rows carrying a key, `value_counts` for elements. The same
  run also exposed a silent hole in my own work: a fold over objects whose
  values are not numeric returned an empty mapping, which reads exactly like
  totals of zero. Both folds now raise when they matched nothing, naming the
  first payload they saw, while a sparse column — some rows foldable, some not
  — still folds normally. `column_stats` derives its element distribution from
  `value_counts` with an `elements_exact` flag, matching what `keys_exact`
  already did for keys.
- Verified against ground truth: every fold recomputed from the source database
  with plain SQL and compared key by key — `tool_calls` (15 tools, `run_sql`
  20,988), `keywords` (23 tags), `reasons` (8), `scores` — all exact over all
  1,916 rows. Using the wrong fold for a payload raises rather than returning
  an empty result.
- Also: the assistant's system prompt conflated "which keys exist" with "sum of
  the values", so an end-to-end run watched the model call `column_stats`,
  notice the mismatch itself, and switch to `key_sums`. It reached the right
  answer, but the prompt sent it the wrong way first; it now names the three
  folds by what each one counts.
- Next: none

## 2026-07-31 — Fold Json key aggregation through the reducer registry

- Task: direct request — the `explode` clause was the wrong shape for this. The
  aggregate stage already has an extension seam, and a new spec clause is a
  worse answer than using it.
- Changed: `explode` and dotted-path metric sources are removed; the query
  language returns to what it was. Two reducers take their place through the
  same registry every built-in uses: `key_sums` totals each key's values across
  a group and `key_counts` counts the rows carrying each key. Both fold a whole
  `Json` column, so the keys come from the data rather than the query, and both
  cover every row exactly. `column_stats` now derives its exact key
  distribution from `key_counts` instead of an explode aggregate, with no
  change in behaviour.
- Trade-off, stated where callers read it: the result is one mapping per group.
  `group_by` on declared columns works normally, but `order` and `having`
  cannot reach inside a mapping, so ranking happens in the caller. That is the
  cost of not growing the query language, and the workspace guide, the `query`
  tool docstring, and the Python guide all say so.
- Verified: `bash scripts/test.bash tests/query tests/registry/test_tags.py
  tests/interfaces/test_dashboard.py tests/extensions/test_python_extension.py
  -m ""` — 360 passed. Registry pins move by four records (two operations, two
  reducers); the tag definition fingerprint is unchanged.
- Note: the guide-executability test earned its place immediately — it caught a
  `register_aggregate_reducer` example that cannot work from `run_python`,
  because the registry stores an importable target and a function defined there
  has no module path. The guide now shows the direct computation instead.
- Next: none

## 2026-07-31 — Optional in-process Python execution over a workspace

- Task: direct request — the declarative query spec cannot express every
  question, so the vocabulary will eventually miss a shape. Give a model a way
  to write real code rather than approximate the answer.
- Changed: new optional `python` extension. `run_python(code)` executes the
  program in this process against the open workspace: statements run through
  `exec` and a trailing expression is evaluated, so a call behaves like a
  notebook cell, and the namespace persists between calls with `hb`, `ws`, and
  every registered entity class already bound. A `ResultFrame` result is
  rendered through the workspace payload builder, so a paginated result still
  reports `total`/`truncated` — a program must not be able to hand back a page
  that reads as a whole, least of all one the model wrote itself. Output is
  capped and the cap is stated. A companion `python_guide` tool returns the
  query surface to write against; every fenced example in it is executed by the
  test suite, because that text is the model's only source for the API and a
  stale example teaches a wrong one. The declarative `query` tool stays and
  remains the recommended path: the engine validates a spec and names what is
  legal when it cannot, while a wrong program returns a plausible number with
  no such signal.
- Scope: the executed program is not sandboxed and is bounded by nothing the
  process controls. The dashboard assistant therefore gets the tools only under
  `heavenbase.dashboard.assistant.run_python`, off by default, and
  `_assist_tools` no longer claims its surface is read-only without that
  qualification. ADR 0015 recorded the sandboxed and `eval` designs as
  rejected on a security argument; that argument was explicitly set aside for
  this build, and the ADR is annotated rather than deleted so the reasoning and
  the decision to override it both remain visible.
- Verified: `bash scripts/test.bash tests/extensions tests/interfaces
  tests/core tests/registry tests/typing -m ""`; new coverage in
  `tests/extensions/test_python_extension.py` (statement/expression semantics,
  failures returned as data, frame coverage, namespace binding, MCP surface,
  guide executability) and an opt-in test in `tests/interfaces/test_dashboard.py`.
  Registry pins move by one module and three records; the tag definition
  fingerprint is unchanged.
- Next: none

## 2026-07-31 — Result frames report the exact count pagination withheld

- Task: follow-up to the dynamic Json aggregate sources below — the assistant
  could say only "more than N groups matched", because nothing carried a
  pre-limit count.
- Changed: `ResultFrame` gained `total` and `truncated`. Query execution
  paginates last, on a complete result — no backend pushes `LIMIT` — so
  `limit()` records the exact row count it sliced from. Row-preserving
  derivations (`sort`, `project`) carry that total forward; anything that drops
  rows deliberately does not inherit it, since the old count would no longer
  describe the frame. The MCP result payload publishes `total`/`truncated` when
  pagination withheld rows, and the dashboard assistant drops its `limit + 1`
  probe in favour of the engine's own number: the model is now told the exact
  size of what it did not receive, and the UI caption reads `1–n / total ·
  truncated` instead of `n+`.
- Verified: `bash scripts/test.bash tests/execution/ tests/query/
  tests/interfaces/ tests/workspace/ -m ""`; new frame-contract coverage in
  `tests/execution/test_frame.py`, updated dashboard pins in
  `tests/interfaces/test_dashboard.py` and `tests/interfaces/dashboard_ui.mjs`.
- Docs: dashboard architecture page, MCP `query` docstring, and the assistant
  system prompt now state the exact-total contract.
- Follow-up: the dashboard `explain` tool now resolves through a registered
  `workspace_explain_tool` contribution, like `query`, instead of importing the
  workspace family's builders directly. The first cut reached past the Registry
  because no contribution existed; adding one keeps the extension seam single.
- Follow-up: `column_stats` now derives Json key counts from an engine
  `explode` + `group_by` aggregate instead of a Python-side counter capped at
  20 keys over a bounded row sample. The counts are exact and cover every row
  whatever `sample` is, so a rare key is no longer silently dropped from the
  list the assistant reads. A column the engine cannot explode falls back to
  the scan's view, and `keys_exact` says which happened rather than leaving the
  two identically-shaped lists indistinguishable. Any most-common list cut at
  its limit now sets `<key>_truncated`.
- Follow-up: an audit for the same defect class — a partial result shaped like
  a complete one — found four cases inside this work's blast radius. The exact
  key counts were being divided by the sampled scan size in the stats panel,
  which produced a plausible but wrong share; the panel also never rendered the
  new truncation flags. The MCP markdown serializer discarded `total`/
  `truncated`, rendering a page as a whole table. And `ResultFrame.total`
  claimed pagination always runs on a complete result, which is false under a
  `near` clause: vector search feeds the pipeline only its top-k window, so the
  payload now carries `near_bounded` and the contract says so.
- Follow-up: the rest of that audit's findings are closed too, so the rule now
  holds across the codebase rather than in one endpoint. Card search sliced with
  raw Python and lost the count it had just computed, so a cut search reported
  itself complete; it now slices through the frame contract, and the palette and
  the assistant's `search_workspace` both state what they withheld. Text
  statistics keyed their counters on a 120-character prefix, so `distinct`
  merged values that differ later — counters now key on whole values and elide
  only for display, marking the elision. `count()` is documented as the maximum
  across routed backends, which is what it is. The assistant discloses a trimmed
  history window, and asks for the tool-loop `truncated` verdict so an answer
  cut short by its budget is not shown as finished. Memory tools publish
  `limit`/`total`/`truncated` and say so in the descriptions an agent reads, and
  `MemoryVersions.rows()` scopes its query instead of capping at 10,000 — which
  also stopped `append` from silently overwriting version history. Benchmark
  averages no longer fabricate 0.0 s repeats for stages a failed run never
  executed, publish their sample counts, and refuse a delta that mixes
  populations. Capability totals now partition the matrix they summarise.
- Next: none

## 2026-07-31 — Adapted assistant interruption to the typed dashboard stack

- Task: direct request — review and prepare PR #77 after PR #76 replaced the
  dashboard implementation
- Changed: Merged current `master`; ported the interrupt flow from deleted
  package-local JavaScript and server paths into strict TypeScript under
  `apps/gui/` and the canonical `heavenbase.api.http` transport. One
  `AbortSignal` now owns streaming and blocking-fallback cancellation, aborted
  turns never retry, partial evidence remains in history, and closing/reopening
  the panel preserves the active Stop affordance. The HTTP bridge validates
  OpenAPI events, explicitly cancels client readers, preserves ContextVars and
  response backpressure through a bounded acknowledged queue, returns
  immediately on disconnect, and reports teardown failures without hanging.
  Retained the macOS Bash 3.2-safe scalar xdist cap and synchronized generated
  JavaScript plus user and dashboard architecture documentation.
- Verified: 58 dashboard/API/UI tests passed; the Bun OpenAPI-generation,
  format, lint, strict-typecheck, unit-test, and build gate passed; the fast
  suite passed 896 tests with 10 environment skips; and the full tier passed
  1,112 tests with 60 external-service skips after syncing declared extras.
  Black/Flake8, Heaven-style utility scanning, documentation, OpenAPI,
  environment/README, and generated-metadata freshness gates passed.
- Next: none

## 2026-07-30 — Dynamic Json aggregate sources: dotted metric paths and `explode`

- Task: direct request — an assistant asked to total a `Json` dict field across
  ~1,900 rows had no engine path for it, paged rows into its context, and
  fabricated the totals.
- Changed: The aggregate stage now accepts dynamic sources. A dotted Json path
  may be a metric source, not only a group/order/select name, and the new
  `explode` clause expands one Json object field into a `key`/`value` column
  pair so an aggregate can group by keys discovered in the data. The motivating
  question is now one call:
  `{"explode": {"field": "tool_calls", "key": "tool", "value": "calls"},
  "group_by": ["tool"], "aggregate": {"total": {"sum": {"field": "calls"}}}}`.
  Dynamic sources carry no declared logical type, so `validate_aggregate` skips
  the type check and each group proves its values against the reducer's
  registered input modes at fold time: a missing key is skipped rather than
  folded as zero, while a Boolean under `sum` or mixed kinds under `min`/`max`
  raise. Two latent correctness bugs were fixed alongside: `fold_backend_names`
  mapped only dotted *groups* to their base field route, so a dotted metric on
  an optional field routed to a secondary backend could miss that route, and
  the router derived only group paths before folding, so a dotted metric column
  would have read as all-nulls and returned a silent `None`. The explain reason
  `json_path_group_by_present` became `json_path_clause_present` now that
  metric sources trigger it too.
- Verified: `bash scripts/test.bash tests/query/ -m ""`; new coverage in
  `tests/query/test_query_explode.py` (7 cases: per-key totals, non-object
  payload drops, filter/dotted/HAVING composition, explain, clause guards,
  value guards) and a dotted-metric cluster in `tests/query/test_json_filters.py`
  parametrized over `sqlite` and `inmem` with parity against a lifted declared
  column.
- Reviewed: an adversarial pass over the landed diff found four real defects,
  all fixed here. The `explode` base field was only proven when the name
  contained a dot, so a plain non-Json or unknown field crashed with a raw
  `AttributeError`/`KeyError` inside the frame transform after a full scan —
  `resolve_json_source` now proves any payload clause name, and the new
  `validate_explode` runs during planning. `explode` output names could shadow a
  declared column, and the transform overwrites it, so an aggregate over the
  real field silently returned exploded values; shadowing is now rejected. The
  fold-time guard tested a fixed JSON kind list, so payload values `Json`
  deliberately round-trips — a `date` under `min`, a `Decimal` under `sum` —
  were rejected while their declared twins folded fine; the guard now tests what
  the reducer needs. Finally the docs claimed dotted metric sources without the
  Python surface supporting them: `_JsonPathRef` gained the reducer intents, so
  `Doc.meta["review"]["score"].sum()` works, and the reserved-name contract now
  covers both reference namespaces.
- Docs: ADR 0009 revised with the dynamic-source contract; ADR 0015 records the
  rejected sandboxed-REPL and `eval()` alternatives; routing-and-explain,
  the MCP `query` docstring, and the workspace guide updated.
- Next: none

## 2026-07-30 — Prepared the dashboard tech-stack PR for merge

- Task: direct request — final review of PR #76 against `master`
- Changed: Merged current `master`; restored selector focus before workspace
  menu actions; constrained matched menus to their trigger width; rejected
  malformed assistant events without duplicating model turns; tightened
  assistant runtime guards and macOS host URL policy; documented HTTP 500
  responses in the generated protocol; made explicit host flags open unless
  `--no-open` is supplied; preserved live workspace state when durable
  unregister fails; promoted the whole deterministic dashboard contract suite
  into the fast tier; kept its concurrency deadline robust under hosted xdist
  scheduling; and retired the completed branch-specific goal in favor of the
  durable dashboard architecture page.
- Verified: 54 dashboard tests, 901 fast tests with one environment skip, and
  1,112 full tests with 60 expected external-service skips passed. Bun
  generation/format/lint/typecheck/test/build, Black/Flake8, documentation,
  environment/OpenAPI drift checks, responsive browser interaction QA,
  distribution build, and 38-module wheel/sdist inventory also passed.
- Next: none

## 2026-07-30 — Clarified and completed the workspace picker

- Task: direct request — distinguish workspace selection and add safe removal
- Changed: Replaced the fake empty workspace row with a `+ Open workspace…`
  action state; matched the menu width to its trigger; kept active status
  right-aligned; and added keyboard-accessible per-row trash actions with a
  confirmation dialog. Added a typed `DELETE /api/workspaces/{ws_id}`
  operation that unregisters only the workspace record, releases a matching
  live dashboard facade, and leaves backend data untouched.
- Verified: The installed app rendered the full-width picker, right-aligned
  active marker, trash controls, and destructive confirmation in the Warm
  theme. A temporary registration was removed through the real UI while the
  open `glosswise` workspace remained healthy. The 48-test dashboard suite,
  838-test fast suite with one expected skip, Bun
  generation/format/lint/typecheck/test/build, Black/Flake8, documentation,
  environment/OpenAPI drift checks, distribution build, and 38-module
  inventory passed.
- Next: none

## 2026-07-30 — Unified installed dashboard state and reinstall behavior

- Task: HB-009
- Changed: Initialized the Finder app against the persistent default Context;
  added shared dashboard appearance preferences with one-time browser-cache
  migration; persisted successful workspace activation; and stopped
  auto-opening arbitrary registered workspaces. Workspace-open failures now
  preserve the prior state and return contextual JSON errors. Reinstalling with
  `--install-app` now confirms replacement by default, while `--yes`/`-y`
  provides the non-interactive path consistently across CLI backends.
- Verified: A real `~/Applications/HeavenBase.app` reinstall, code-signature
  check, quit, and LaunchServices relaunch preserved the Warm theme and opened
  the `glass` workspace with nine entities and no error toast. The 46-test
  dashboard suite and 838-test fast suite passed with one expected skip, along
  with Bun generation/format/lint/typecheck/test/build, Black/Flake8,
  environment and documentation drift checks, and the distribution inventory.
- Next: none

## 2026-07-30 — Repaired installed macOS dashboard launch

- Task: HB-008
- Changed: Replaced the script executable inside generated
  `HeavenBase.app` bundles with a compiled, stay-open JXA applet and an ad-hoc
  signature; preserved virtual-environment Python symlinks; resolved bundle
  metadata independently of the interpreter; and retained the applet runtime's
  lifecycle delegate while observing window and application termination for
  child-server cleanup. Centralized GUI asset lookup so wheel `.data` payloads
  also resolve under uv overlay environments.
- Verified: The real `~/Applications/HeavenBase.app` and a clean installed-wheel
  bundle both checked in through LaunchServices, served `/api/meta` from their
  bound Python environments, and terminated app and server processes on Quit.
  A CLI-owned loopback URL opened without spawning a second server. The 38-test
  dashboard suite, 838-test fast suite, Bun format/lint/typecheck/test/build
  gate, Black/Flake8, environment and documentation drift checks, distribution
  build, code-signature verification, and 38-module wheel/sdist inventory
  passed.
- Next: none

## 2026-07-29 — Completed the dashboard protocol, TypeScript, and macOS app delivery

- Task: HB-007
- Changed: Bound the 21 dashboard operations to shared strict Pydantic models
  and one served, committed, freshness-checked OpenAPI 3.1 document; generated
  the frontend contract from that document and migrated every owned UI source
  module to strict TypeScript with runtime validation at the network boundary.
  Added an installable `HeavenBase.app` bundle with a stable identifier, native
  icon, AppKit/WebKit host, loopback-only owned server lifecycle, explicit
  replacement semantics, and installed-app launch preference. React remains an
  allowed tool when future component or state pressure justifies it; the current
  UI did not require a second component architecture. Windows packaging and
  signing/notarization remain explicitly deferred.
- Verified: 829 fast tests passed with 10 expected optional-dependency skips;
  the focused dashboard all-marker suite passed 38 tests; the Node UI harness
  passed 14 tests; Bun formatting, lint, strict typecheck, three frontend tests,
  and generated-client freshness passed. Black/Flake8, environment drift,
  OpenAPI freshness, documentation contracts, macOS bundle plist/JXA/icon
  checks, distribution build and 38-module wheel/sdist inventory passed.
  Rendered Light, Dark, and Warm reviews passed at wide width, 110% zoom, and an
  actual 720px responsive viewport without horizontal overflow or layout
  regressions.
- Next: none
