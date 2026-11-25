# Workspace Backend Presets

Workspace presets package the backend map a new project needs into one named config choice. They are additive: explicit `backends={...}` still wins when you need full control.

## Quick Start

```python
import heavenbase as hb

ws = hb.HeavenBase("shop", preset="debug")
print(ws.backends.names())  # ["main", "vec", "search"]
```

Use CLI inspection before writing code:

```bash
hb ws presets list
hb ws presets show debug
```

## Built-In Presets

| Preset | Backends | Use case |
| --- | --- | --- |
| `debug` | SQLite `main`, in-memory `vec`, in-memory `search` | Demos, smoke tests, and first-run development with no Docker. Sparse-GRAM keyword indexes are hosted by `main`. |
| `local-lts` | Postgres `main`, LanceDB `vec`, Elasticsearch `search` | Self-hosted development with durable rows and local vector/search services. |
| `web-lts` | Supabase `main`, pgvector `vec`, hosted Elasticsearch-compatible `search` | Managed or shared development. Supabase and pgvector use distinct table scopes inside the configured PostgreSQL database. |

## Configuration

Presets live under `heavenbase.workspace.presets` and resolve to the same backend dictionaries accepted by `HeavenBase(..., backends=...)`.

```yaml
heavenbase:
  workspace:
    default_preset: debug
    presets:
      debug:
        desc: Dependency-light local workspace.
        backends:
          main: {type: sqlite, name: main, database: ":memory:"}
          vec: {type: inmem, name: vec}
          search: {type: inmem, name: search}
```

Workspace presets use the universal config pipeline. `resolve_workspace_preset`
returns an immutable `ConfigSpec`; its default `backends` Apply profile returns
an independent backend mapping. The optional `preset` profile returns the full
resolved template. `{ws_id}` placeholders are substituted only during Apply,
so the resolved spec retains portable raw configuration. A version-2 workspace
manifest records the canonical preset identifier in its single construction
envelope; callers may replace that envelope with `preset=` or `backends=` when
replaying in another environment.

Explicit local-file locators use the shared URI grammar:

- `file:///absolute/path`
- `file://%/path` relative to the HeavenBase data root
- `file://&/path` relative to packaged resources

The `&` alias is useful for read-only resources. Writable database and backend
storage rejects packaged-resource targets contextually.

Legacy `file:` spellings and bare native paths are rejected by runtime config.
Use `scripts/migrate_config_locations.py` to inspect and rewrite persisted
legacy locators.

## Local LTS Environment

`local-lts` expects the repo Docker stack or equivalent services:

```bash
bash ./scripts/docker-restart.bash /d/databases/
```

Override connection details with environment variables:

```bash
HVNB_LOCAL_LTS_POSTGRES_DB=rubik
HVNB_LOCAL_LTS_POSTGRES_HOST=localhost
HVNB_LOCAL_LTS_POSTGRES_PORT=5432
HVNB_LOCAL_LTS_POSTGRES_USER=rubik
HVNB_LOCAL_LTS_POSTGRES_PASSWORD=password
HVNB_LOCAL_LTS_POSTGRES_SUPERUSER=rubik
HVNB_LOCAL_LTS_POSTGRES_SUPERUSER_PASSWORD=password
HVNB_LOCAL_LTS_POSTGRES_MAINTENANCE_DB=postgres
HVNB_LOCAL_LTS_ES_URL=http://localhost:9200
```

The superuser values are used only for creating and dropping the workspace-scoped Postgres database. They default to the repo Docker stack's `rubik` user because that user can create databases in the local stack.

The local Lance base is `file://%/workspaces/lance`. The Lance backend appends
the workspace id exactly once. Remote Lance URIs remain provider-native and
cannot be combined with a workspace id until a remote namespace strategy is
registered.

Verify preset resolution with:

```bash
hb ws presets show local-lts
bash scripts/test.bash tests/workspace/test_workspace_presets.py -q
```

Run the external backend tests when the local services are available to cover CRUD, vector near, and text search behavior on the durable stack.

## Web LTS Environment

`web-lts` is intentionally env-only for secrets:

```bash
HVNB_WEB_LTS_POSTGRES_DB=postgres
HVNB_WEB_LTS_POSTGRES_HOST=...
HVNB_WEB_LTS_POSTGRES_PORT=5432
HVNB_WEB_LTS_POSTGRES_USER=postgres
HVNB_WEB_LTS_POSTGRES_PASSWORD=...
HVNB_WEB_LTS_POSTGRES_SSLMODE=require
HVNB_WEB_LTS_ES_URL=...
```

Both PostgreSQL-backed routes keep `HVNB_WEB_LTS_POSTGRES_DB` intact. Their
workspace and backend names are encoded into bounded table prefixes, so the
managed database is shared without one workspace dropping another's tables.

## Override Rule

When both `preset` and `backends` are passed, `backends` is used and the preset resolver is skipped:

```python
ws = hb.HeavenBase("tmp", preset="debug", backends={"main": {"type": "inmem"}})
```

This keeps old code stable while allowing new projects to start from `preset="debug"` or `preset="local-lts"`.
