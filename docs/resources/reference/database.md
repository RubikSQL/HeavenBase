# Database Facade and Extension

HeavenBase has two deliberately separate database surfaces:

- `heavenbase.database` owns reusable SQLAlchemy configuration, physical-dialect
  resolution, engine lifecycle, readonly SQL policy, reflection, and response
  values. The root facade exposes its common entry points as `hb.Database`,
  `hb.DBEngine`, and `hb.DBSpec`.
- `heavenbase.extensions.database` is an optional workspace bundle that consumes
  that low-level facade to publish `db-*` entities and database-focused tools.

The extension is not a second database implementation. It adds HeavenBase schema,
ingestion, and MCP policy around the root database owner.

The optional `database` extension introspects external SQL databases into rich,
editable `db-*` entities. Its scoped MCP profile can discover and edit that
context, execute guarded read-only SQL against the real source, and save a query
only after successful execution.

Enable it explicitly when ingesting or querying schema rows from Python:

```python
import heavenbase as hb

ws = hb.HeavenBase("warehouse", preset="debug")
ws.enable_extension("database")
ids = ws.database.ingest(
    "/path/to/warehouse.db",
    name="warehouse",
    tables=["customers", "orders"],
    columns={"customers": ["customer_id", "segment"]},
)
```

## Entities

The extension registers seven entities:

| Entity | Id | Purpose |
|--------|----|---------|
| `DbDatabase` | `db-database` | Redacted source, dialect, imported schemas, selected relations, and coverage statistics |
| `DbTable` | `db-table` | Standalone qualified identity, relation kind, PK/FK/index metadata, description, and statistics |
| `DbColumn` | `db-column` | Standalone table context, physical and analytical types, description, profile, and statistics |
| `DbEnum` | `db-enum` | Native enum or profiled low-cardinality value context |
| `DbSnippet` | `db-snippet` | Saved SQL, natural-language context, verification status, and latest success time |
| `DbPredicate` | `db-predicate` | Reusable SQL predicate with standalone database and column context |
| `DbRule` | `db-rule` | Editable natural-language business or SQL rule, scope, triggers, examples, and optional SQL |

Every row uses the standard `object_id: Identifier` primary key. Each entity
has editable `synonyms` and derived `semantic_text`; Catalog projects names,
descriptions, synonyms, statistics, and semantic text for normal workspace
search. `semantic_text` is the provider-neutral embedding source. The extension
does not fix a model or vector dimension, so a deployment may add SparseGram or
another embedding strategy later without changing the entity vocabulary.

Rows deliberately repeat useful database, schema, table, and column names.
`database_id` and `table_id` remain convenient filters, but agents do not need
to traverse a dense graph before understanding or editing one row.

## Ingest

`ws.database.ingest(db, name=..., tables=..., columns=..., profile=True)` accepts:

- `hb.Database`
- a database config mapping with `type`
- a workspace SQL backend name
- a DSN URL
- a local SQLite path such as `/path/to/warehouse.db`

The native-path form is an ingest-boundary convenience: the extension converts
it to the shared file URI before database resolution. Database config mappings
must use the canonical locator contract described below.

Omit `tables` and `columns` to import every reflected table and view. Relation
selectors may be unqualified (`orders`), schema-qualified (`sales.orders`), or
catalog-qualified. A `columns` mapping restricts only the named relations; when
`tables` is omitted, its keys also select the relations. Unknown relations or
columns fail before any `db-*` row is written.

Ingestion uses SQLAlchemy reflection to upsert database, relation, column, and
enum rows. On DuckDB the `Database` reads a table's indexes from the database
catalog, and its primary key from the catalog when SQLAlchemy reports none.
With profiling enabled it performs exact row/null/distinct counts,
numeric or temporal ranges, text lengths, and bounded common-value queries.
Low-cardinality text-like columns also produce `DbEnum` context. These queries
can be expensive on a large source, so select the analytical surface first or
pass `profile=False` for a fast structure-only import.

Refreshing the same source preserves agent-authored `desc`, `synonyms`, and
column `semantic_type` values while refreshing physical metadata and profiles.
Credentials are always redacted in stored summaries and diagnostic source
config. The API retains the original live `Database` handle only in the current
process. A named workspace SQL backend can reconnect after restart; a secret
DSN supplied directly must be re-ingested because redacted credentials are not
reconnectable. Secretless local sources may reconnect from stored config.

The implementation protocol is intentionally small:

- `extensions/database/source.py` resolves input sources and redacts diagnostic config.
- `extensions/database/rows.py` builds deterministic ids and row dictionaries.
- `extensions/database/ingest.py` orchestrates inspection and workspace upserts.

Add new source forms in `source.py`, stored metadata shapes in `rows.py`, and new inspection passes in `ingest.py`.

## SQL Configuration

`DBEngine` uses the universal
`Preset -> Normalize -> Validate -> Apply(profile)` pipeline. `Database(...)`
omits unsupplied constructor fields, so provider and preset defaults survive.
An explicitly bound engine receives either a `ResolverDialectCatalog` for normal
Context-owned resolution or the immutable `BootstrapDialectCatalog` used only while
the control Registry is being opened; it never switches dialect authority through
ambient state. `Database(spec=...)` reuses one already-resolved immutable snapshot.
The resolved `DBSpec` is immutable and contains flat provider-native fields:

- `database` is a server database for PostgreSQL/MySQL-like providers and a
  canonical file URI for SQLite/DuckDB;
- `catalog` and `schema` remain distinct for providers such as Trino;
- Oracle canonical config uses exactly one of `service`, `sid`, or `dsn`;
- `in_memory: true` represents memory databases and clears `database`.

Examples:

```python
db = hb.Database("my-database", preset="default")
assert db.spec.preset == "default"
assert db.spec.provider == "sqlite"
assert db.spec.database.startswith("file:///")

db = hb.Database(
    provider="postgres",
    database="app",
    schema="public",
)
assert db.spec.database == "app"
assert db.spec.schema == "public"
```

A separator-free logical SQLite name is resolved beneath
`heavenbase.db.local_root`. Explicit local locations accept only:

- `file:///absolute/path`
- `file://%/relative/path` for the HeavenBase data root
- `file://&/relative/path` is part of the shared read-only codec, but writable
  databases reject packaged-resource targets

Legacy `file:` spellings, bare relative/native paths, home expansion, network
file hosts, credentials, query strings, fragments, and traversal through an
alias root are rejected. SQLAlchemy URL input is Normalize-only shorthand;
its driver and multi-value query parameters are preserved in canonical fields.
Without `provider`, a URL selects the one provider whose dialect and driver
match it; MySQL-compatible engines share a URL form, so a `mysql` URL needs an
explicit `provider`. A provider that only changes another's defaults sets
`infer_from_url: false`: `supabase` does, so a `postgresql` URL selects
`postgres` and Supabase needs `provider="supabase"`.

The registered Apply profiles are:

| Profile | Result |
|---|---|
| `url` | Immutable structured SQLAlchemy `URL`. |
| `uri` | Lossless provider-native URI string. |
| `engine` | Defensive SQLAlchemy engine-construction arguments; the default. |
| `superuser` | Lifecycle-only superuser URL/config values. |
| `cache-key` | Complete applied-engine identity, including ordered connection initialization statements. |
| `pragmas` | Connection initialization statements, run on each new DBAPI connection and committed before it is used. PostgreSQL also runs individual `SET LOCAL` statements from this list at the start of each transaction. A statement naming `autocommit` or `implicit_transactions` withdraws a SQL backend's `atomic-upsert` claim. |
| `print` | Redacted diagnostic data and URI. |

`url` and `uri` may contain secrets and are not display APIs. Use `print` for
logs, CLI output, and persisted diagnostic summaries. Live SQLAlchemy engines
remain owned by `DatabaseEngineRegistry`, keyed by the applied engine configuration
and the ordered `pragmas` list. Facades with the same key share an engine, except a facade whose engine
serves one thread at a time (an in-memory database, or `pool_class` `static`,
`assertion`, or `singleton`): that engine is private to the facade, so the lock
of the backend that owns it covers every statement on it.
Persistent facades with the same profile and Context authority share a schema
index. Facades with different `pragmas` have separate schema indexes because
initialization can change the visible schema. Within one Context authority,
schema invalidation and pool disposal for drops are grouped by the database a
facade opens, whatever its pool settings, engine arguments, driver or `pragmas`.
A local file database (SQLite or DuckDB) is identified by its resolved path,
including symbolic links. Each SQLite connection opens the file by the path
its symbolic links resolve to as it opens, so every symbolic-link name of one
file reaches one database and its `-wal` and `-shm` files sit beside the file
itself, also on Windows, where SQLite does not resolve links. Hard links stay
separate names; SQLite does not support them. A DuckDB connection opens the
configured path, as in 0.1.2.2: DuckDB runs one database per path it opens and
names its catalog and `.wal` file after it, so two names of one DuckDB file,
such as a symbolic link and its target, used at once in one process are two
databases, and commits made through one of them can be lost. Use one name per
DuckDB file in a process. The engine URL and printed config keep the configured
name. A SQLite link that changes target while a facade is open is followed by
the facade's next new connection, while its grouping for schema invalidation
and drops keeps the target resolved when the facade was created, as in 0.1.2.2. A server database is identified by its dialect, host,
port, URL database and query parameters, and the namespace that
`drop_database()` removes. The user name and password do not separate groups,
except on Oracle, where the namespace is the target user/schema. Every URL query
parameter counts, because some select the target, such as a PostgreSQL `host`
socket directory. Facades that differ only in a connection option such as
`sslmode`, `charset` or `application_name` therefore form separate groups. To keep
them in one group, pass such options through `pool.connect_args` instead.
Standalone DDL invalidates all setup variants after the statement's
transaction ends. DDL in `db.tx()`
invalidates them when the statement runs and again when the transaction commits
or rolls back. Reflection started before invalidation can return to its caller
but cannot replace a shared index afterward. `drop_database()` disposes all
the cached engines in the group before removing the namespace, and the group's
facades report `disposed` afterwards, also when the file or namespace was
already missing. The drop also removes the engines and schema indexes that
other Contexts cached on the same database, for a local file also through
another path to it, and closes their idle connections; their facades open a
fresh engine at their next use and see the database as the drop left it. Every
LLM cache and Capsule registry store of any Context that has the database open
is detached and recreates its tables on fresh storage at its next use, and one
that is opening it meanwhile waits for the drop to end and then builds on fresh
storage; `drop()`, which drops the tables only, detaches them too. These drops of
one database run one after another when they overlap, from any Context, also
from two threads of one Context, and for a local file through any name of it
that the process opened or dropped while the file existed (a hard link, or
another letter case on a case-insensitive file system). The module-level
`heavenbase.database.drop_database(spec)` deletes the database without this
coordination, as in 0.1.2.2, except that it too refuses a thread with a
connection to the database in use and removes a DuckDB file only once
HeavenBase's connections to it are closed, as below. A thread that has a
connection to the database in use, such as an open transaction or a response it
has neither consumed nor closed, cannot drop it: `drop_database()`, `drop()` and
the module-level function raise `DropRefusedError` (a `RuntimeError`) at once,
because the drop's statement or file removal would wait for that connection.
This applies to a stored database; an in-memory database belongs to one facade,
so no check is made for it (and `drop_database()` and the module-level function
remove nothing from it, see below). A
response dropped unconsumed releases its connection at once. A response that a
garbage collection frees together with an iterator over it keeps its connection
until the `heavenbase-sqlresponse-settler` thread has read its remaining rows
and ended its transaction. That connection does not count as in use by the
thread that executed the response: that thread's calls may wait for a drop, and
its own drop first settles such responses itself. Once the stores are detached,
a `drop_database()` closes the same Context's scope on the database until it has
removed it. Meanwhile that Context's facade, engine and schema-index calls on
the database wait for the removal, also through an engine or index cached
before the drop, unless the calling thread holds a Context or workspace lock,
runs a drop itself, or has a connection to the database in use: the drop may
wait for such a thread, so its call raises `Database is being dropped by this
Context` at once. Retry such a call after the drop. A workspace creates its
facade under the Context lock, so a workspace opened on the database meanwhile
raises it. Its LLM cache and Capsule registry calls on the database run
normally until the drop has detached their stores; a call that starts after
that waits for the drop to end and then reopens the store, unless its thread
has a connection to the dropped database in use, which raises `RuntimeError`
instead because the drop may wait for that connection. A thread with such a
connection also raises it instead of waiting for the Context lock while an LLM
cache or Capsule registry drop (`LLMCache.reset(drop=True)`,
`reset_capsule_registry(drop=True)`), which holds that lock, drops the
database. Afterwards the
facades the Context had on the database fail with `Engine has been disposed`,
as after any drop, and newer ones open the database as the drop left it. A
local file is removed together with its `-wal` and `-shm` files, and a DuckDB
file also with its `.wal` file, inside the drop, and nothing is deleted after
it returns, so a client that creates the database again meanwhile keeps its
writes. PostgreSQL still refuses the drop while another session is connected
to the database, for example a connection another Context has in use, unless
the drop passes `force=True`, which ends those sessions first. DuckDB keeps a
database open in the process while any connection to its file is open, so a
DuckDB file is removed only once no connection of HeavenBase's engines to it,
or to another name of it, counts as open or being opened, in any Context: the drop
closes the idle connections, in whichever pool they sit, and each one returned
meanwhile, and opening a connection to the file waits for the drop before
DuckDB opens the file (or raises at once, as above) and then reaches the file as
the drop left it. A connection stops counting when its close begins, so a call that
opens the file before that close ends can still reach the removed database, as in
0.1.2.2. When connections stay in use for 30 seconds, such as one in
another thread's long transaction, the drop raises `DropRefusedError` and
removes nothing. A workspace drop checks every route for a connection of the
dropping thread before it drops any, so that refusal drops no route. It then
waits, without holding a lock and without closing a file, for each DuckDB file in
turn until every connection that was in use when the wait for that file began has
been returned, and raises `DropRefusedError` with no route dropped when one stays
in use for 30 seconds; a connection through a hard link of the file is waited for
only by the drop itself. It drops its routes in order and stops at a refusal, which
a connection lent after the wait began can still cause: the routes after it are
kept, and the error says which routes were dropped and which routes' drops failed
before, or that no route was dropped when neither happened. Connections of
engines that the caller created itself are not seen. A `drop_database()` that cannot
remove anything disposes only the caller's own engine: an in-memory database, a facade without a lifecycle
namespace (a server URL without a database, or Trino), or a dialect without a
registered database drop (such as ClickHouse). For a local
file opened through a symbolic link, dropping through that link removes its
target. In-memory facades have separate databases and schema indexes.

SQLite runs assignments to `busy_timeout` for the default, `main`, or `temp`
schema first and keeps the other statements in their configured order. If
an assignment names an attached schema, SQLite runs the entire list in its
configured order. The attachment must exist before its assignment runs, and
every assignment changes the same connection-wide timeout.

Inside an existing SQLite transaction, a failed upsert rolls back to a
savepoint while the outer transaction remains active. If SQLite rolls back
the outer transaction itself, as with a trigger that calls `RAISE(ROLLBACK, ...)`,
the caller receives the original write error and the outer writes are rolled
back too. To resume writing in the same `Database.tx()` context, call
`rollback()` before continuing.
A top-level SQLite upsert also reports the original write error when a trigger
rolls back its transaction. The failed batch leaves no partial writes.

A `pool` mapping on `Database(...)` or on a SQL backend map sizes the engine's
connection pool and therefore how many threads can read one backend at once,
because SQL backend reads run their statements on their own pooled connection
outside the backend lock. On a bounded `queue` pool every SQL backend on the engine
that has read or upserted keeps one connection for the thread holding its
lock, and the reads outside their locks share the rest, less the connections
other users of the engine hold (statements run directly on a `Database` for
the same database, or a backend that has not yet read or upserted): a read
beyond them waits for a free connection without a time limit. A read made while
holding the backend's lock (the sparse-GRAM adapter reads rows that way) runs
under the lock on its reserved connection. When no connection is left for
ordinary reads (`pool_size + max_overflow` in all), they keep the backend lock
and run one at a time, as in 0.1.2.2.

A synchronous nested read on the same engine goes directly to the pool when
an outer read on its thread is using a read slot. It can take a connection
reserved for a backend lock holder and follows `pool_timeout` if the pool is
exhausted. Nested reads and statements run directly on the engine can both
make a lock holder wait for a connection and reach `pool_timeout`. Size the
pool for that use.
It accepts `pool_class` (`queue` by default; `static`, `null`, `singleton`,
and `assertion` are the other names), `pool_size`,
`max_overflow`, and `pool_timeout` (accepted for `queue` only), `pool_pre_ping`,
`pool_recycle`, and `connect_args`; an unknown key is rejected. Keys left out
keep the provider defaults, `pool_size: 5` and `max_overflow: 10` for SQLite.
`static`, `assertion`, and `singleton` cannot serve two threads at once
(`singleton` closes other threads' connections, even one running a statement,
once more threads than its `pool_size` use it), so reads on such a backend keep the backend
lock across their statements and run one at a time, and two backends on the
same database each get their own engine.
Reads on an in-memory database keep the lock whatever its `pool_class`: the
database lives inside its first connection and a second connection is a
separate empty database, so `static` is its default, `singleton` gives each
thread a private database, and `queue` and `null` are not meaningful for it.
Reads keep the lock as well on a backend whose type does not claim the
`atomic-upsert` tag: the generic `sql` type, ClickHouse, StarRocks, Trino, or
a type the Registry does not know. A SQL engine that may autocommit also
withdraws the tag, and so does a MySQL-family backend with a table whose
storage engine keeps no transactions. These backends may commit the delete and insert as separate
statements, so their reads keep the lock. The built-in SQL types eligible to read outside
the lock are `sqlite`, `duckdb`, `postgres`, `supabase`, `mysql`, `seekdb`,
`dolt`, `oceanbase`, `mssql`, `oracle` and `pgvector`, subject to these
conditions. Reads also keep the lock where one statement may see part of another
transaction: when the connections read at `READ UNCOMMITTED`, and on SQL
Server unless they read at `SNAPSHOT`, or at `READ COMMITTED` (the default) on
a database with `READ_COMMITTED_SNAPSHOT` on; its locking `READ COMMITTED`
lets one scan read some rows of an upsert before it and others after it, and
`REPEATABLE READ` and `SERIALIZABLE` keep the lock whatever the database
option. The level is the engine's `isolation_level`; on MySQL, MariaDB, SQL
Server and SQLite the backend asks the database, once each time it connects,
for the level of a pooled connection, so a server default (MySQL's
`transaction_isolation`) or a level set as each connection opens counts too,
when the connect sets it. If the probe fails, the configured isolation level
serves as a fallback. If neither source establishes the level, reads keep the lock. Elsewhere
each statement a read runs outside the lock sees an upsert whole or not at
all. `get_many` and `exists_many` over more ids than one batch (a batch
holds at most `heavenbase.backend_defaults.sql.id_lookup.batch_size` ids,
500 by default) run their batches under the backend lock, as in 0.1.2.2, so
they see a write of the backend whole or not at all too; `iter_scan` runs one
statement per page and may see a write between two. `drop` waits up to 30
seconds for the reads running outside the lock, then removes the database
(0.1.2.2 waited without a limit). A read that would run after that, including
one planned before the drop, raises `RuntimeError` ("unavailable after drop"),
as does a read still running then if it fails; one that succeeds returns its
result. A read of any pool class still waits for a mutation in progress, which
holds the backend lock for its whole call:

```yaml
heavenbase:
  backends:
    main:
      type: sqlite
      name: main
      database: "file:///data/app.db"
      pool:
        pool_class: queue
        pool_size: 16
        max_overflow: 16
```

Use `scripts/migrate_config_locations.py` in dry-run mode before rewriting
persisted legacy local locators. The report shows each source field, legacy
base, old/new token, and old/new native target; `--write` is blocked if any
entry is ambiguous or changes its native target.

### Vector Storage Form

A `Vector` field routed to a SQL backend is stored in the physical form its
bound dialect record declares: a native float array where
`vector_native_type` names one (PostgreSQL/pgvector), otherwise the form
named by `vector_packed_type`, and otherwise JSON text. No packaged dialect
record declares a packed form, so SQLite Vector columns keep the JSON text of
`json_text_type` by default, which every HeavenBase release reads. On SQLite,
`json_text` (or unset) reads vectors from TEXT values and raises `ValueError`
for every BLOB, including a BLOB whose bytes are valid JSON. SQLAlchemy Core
projections distinguish the two storage classes with `text_factory=bytes`
or with a SQLite converter registered for the column's declared type.
With the standard `sqlite3` driver, typed textual statements distinguish the
storage classes with both `text_factory=bytes` and callable text factories that
return bytes. This applies to direct and precompiled executions of
`text(...).columns(...)` and to ORM `from_statement()`. SQLite row factories
receive their usual values. Use a Core projection if a SQLite converter or
row factory replaces a textual statement's vector values with bytes without
preserving their origin. On other dialects that store vectors as
JSON text, the reader also accepts UTF-8 bytes returned by the driver for a
text column.

Packed storage is an opt-in. `float64_blob` packs each vector as a
little-endian IEEE-754 float64 BLOB using eight bytes per component. Every
double round-trips bit-exactly. A `float64_blob` database also reads JSON text
through the same column, so an existing database keeps reading after opting
in: an existing TEXT column is not retyped, new writes and rewritten rows are
stored packed, and untouched rows stay text. HeavenBase 0.1.2.2 may silently
return an incorrect vector if a packed row's bytes also form valid JSON.
The current SQLite JSON-text reader raises `ValueError` for packed rows.
No migration rewrites a whole column in either direction. Opt in only where
every reader of the database runs a release that supports packed storage
with the same setting.

Packed storage needs a dialect whose columns are dynamically typed (the
`database-dynamic-typing` dialect tag, which only SQLite claims), because an
existing Vector column was created as text and is not retyped. Every other
dialect without a native vector type refuses `float64_blob` with
`ValueError` when the database, backend or provider config is resolved: a
typed JSON or TEXT column there would store the packed bytes as escaped text
that no reader decodes back into a vector.

Set `vector_packed_type: float64_blob` on the backend or Database
(`hb.backends.SQLiteBackend("main", uri, vector_packed_type="float64_blob")`,
`{"type": "sqlite", "database": uri, "vector_packed_type": "float64_blob"}`,
`hb.Database(..., vector_packed_type="float64_blob")`) or for every database
of a provider under `heavenbase.db.providers.<provider>.vector_packed_type`
(the packaged `default.yaml` shows the switch, commented out, under
`db.providers.sqlite`). `json_text` selects the text form explicitly. The
value overrides the dialect record for that database, is validated against
the same tokens, and is part of the resolved `DBSpec` and its engine identity.

A registered workspace's backend definitions are part of its construction
contract, so constructing an existing registered workspace with
`vector_packed_type` added to a backend definition is refused with
`FileExistsError` ("different construction contract"). An existing registered
workspace opts in through the provider config instead
(`heavenbase.db.providers.sqlite.vector_packed_type: float64_blob`), which
every SQLite database of that Context resolves; a new or detached workspace
can use either route.

## Configuration

| Key | Default | Purpose |
|-----|---------|---------|
| `heavenbase.db.default_preset` | `default` | Preset selected when neither a preset nor provider is supplied. |
| `heavenbase.db.presets.default.provider` | `sqlite` | Current provider selected by the universal default database preset. |
| `heavenbase.db.providers.<provider>.vector_packed_type` | unset (dialect record; JSON text on every packaged dialect without a native vector type) | Vector storage form for every database of one provider: `float64_blob` (opt-in packed BLOBs; SQLite only) or `json_text`; see [Vector Storage Form](#vector-storage-form). |
| `heavenbase.database.allow_write` | `false` | Allow non-readonly `db-snippet` execution |
| `heavenbase.database.max_query_rows` | `200` | Maximum rows returned by database SQL tools |
| `heavenbase.database.profile.max_enum_values` | `50` | Maximum cardinality eligible for imported value-set context |
| `heavenbase.database.profile.top_values` | `10` | Common values retained in a column profile |

Write access stays off by default. Do not enable it unless the deployment explicitly accepts SQL mutation risk.

## MCP Profile

The `database` profile is scoped to the seven `db-*` entities. It exposes
`list_entities`, `describe_entity`, normal read/write/query groups (without
delete), and six database-owned tools:

- `list_sources()` - list credential-safe named workspace SQL backends, connection state, and existing ingested ids
- `ingest_database(backend_ref, name, tables, columns, profile)` - introspect only a backend already configured on the workspace; DSNs and credentials are not accepted
- `run_sql(database_id, sql, params, limit)` - execute caller-authored read-only SQL against the live source
- `run_snippet(snippet_id, params, limit)` - execute a registered `db-snippet` under its stored policy
- `save_snippet(database_id, title, sql, ...)` - execute read-only SQL, then persist a deterministic verified snippet only on success
- `suggest_sql(question)` - search relevant snippets, predicates, and enabled rules without executing SQL or invoking an LLM

`suggest_sql` uses entity-scoped Catalog ranking rather than requiring the
whole user question to be one literal substring. Long analytical questions can
therefore retrieve matching semantic text and synonyms without letting large
table/column catalogs crowd SQL context out of the result cap.

The same operations are available from Python through the extension namespace:

```python
ws.enable_extension("database")
sources = ws.database.sources()
database_id = ws.database.ingest("/path/to/warehouse.db", tables=["customers"])["database"][0]
frame = ws.database.run_sql(database_id, "SELECT COUNT(*) AS n FROM customers")
saved = ws.database.save_snippet(
    database_id,
    "Customer count",
    "SELECT COUNT(*) AS n FROM customers",
    card_text="How many customers are there?",
)
matches = ws.database.suggest_sql("count customers")
```

Agents edit imported descriptions and rules through the same scoped generic
tools used for other HeavenBase entities. For example, `set` can summarize a
`DbColumn.desc`, and `upsert` can add or revise a `DbRule`. Derived semantic text
and Catalog cards are recomputed by normal workspace mutation.

There is no direct `ws.suggest_sql(...)` method; database-specific operations
stay grouped under `ws.database`.

Entity scope is limited to `db-*` ids. The `database` extension is optional and is not enabled on fresh workspaces. Enable it explicitly before browsing `db-*` rows from Python:

```python
ws.enable_extension("database")
tables = ws.query("db-table").execute().rows()
```

The database MCP profile and Toolkit family are conditional contributions.
Their `workspace_requires` predicates do not activate missing Extensions, so
database-owned tools and generic access to `db-*` rows become available
only after `ws.enable_extension("database")`.

## Dashboard Assistant

The dashboard assistant discovers extension capabilities from MCP profile
metadata. It does not contain a database-specific tool list or prompt branch.
When the open workspace has the `database` extension:

- read-only chat receives `list_sources`, `run_sql`, and `suggest_sql` in
  addition to the dashboard's core inspection tools;
- interactive chat additionally receives `ingest_database`, scoped `upsert`
  and `set`, `run_snippet`, and `save_snippet`;
- extension-owned instructions make the assistant search stored rules/snippets
  before SQL authoring, inspect `db-*` schema context, execute actual data
  questions on the source, and persist SQL/rules only when the user asks;
- `run_sql` is declared as structured dashboard evidence, so blocking replies,
  streamed tool/done events, and the chat UI preserve the exact SQL, bind
  parameters, bounded actual rows, counts, and truncation/read-only markers
  beside the final analysis;
- setup accepts only a named workspace backend, so credentials stay in the
  local workspace configuration and never become model tool arguments.

This is the supported chat path for setup, schema-description edits, real SQL
analysis, verified snippet persistence, and explicit rule curation. The server
remains stateless between turns; the browser owns conversation history, while
`DbSnippet` and `DbRule` are durable workspace rows available to later chats
and processes.

The same profile can be installed as an agent-ready skill without a
Database-specific generator:

```python
skill = ws.to_skill(profile="database", port=7001)
skill.install()
```

```bash
hb ws skill warehouse --profile database --port 7001
hb mcp workspace warehouse --profile database --transport http --port 7001
```

The skill includes Database-owned workflow guidance but no workspace
construction or source credentials. See the [MCP reference](mcp.md#workspace-skills).

Readonly enforcement:

1. `db-snippet.readonly=true` is the default stored shape.
2. `heavenbase.database.allow_write=false` refuses write snippets and non-read-only SQL.
3. Tool and execution errors are redacted before they reach MCP clients.

`max_query_rows` bounds the returned tool payload, not the source database's
work. Operators should still grant a read-only, least-privilege database role
and enforce provider-native time, memory, and workload limits.

The shipped extension has no dedicated dashboard page, autonomous learning or
judge loop, or implicit conversation-memory policy. Dashboard chat already
loads this entity/tool contract through the registered MCP profile; memory
remains a separate extension and should store durable conversational knowledge
only when its authority is explicitly designed.

## Workspace Manifests

Include `database` in a workspace manifest when the target environment should replay database enablement:

```yaml
extensions:
  - database
```

Re-ingest source databases after import when live connection metadata is required.
