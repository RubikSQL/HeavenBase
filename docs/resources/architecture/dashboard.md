# Dashboard

`hb dashboard` serves a local workbench for HeavenBase: a configuration workbench
over one `Context.config`, a read-only data browser over that Context's registered
workspaces, and an optional streamed assistant grounded by read-only workspace
tools. The UI and desktop host are interfaces over one application API; they own
no planner, config store, module resolver, or row persistence.

## Architecture Status

- **Current:** Presentation lives under `apps/gui/ui/`, the thin macOS host under
  `apps/gui/host/`, application orchestration and HTTP transport under
  `src/heavenbase/api/`, and desktop-launch policy under
  `src/heavenbase/dashboard/`. Default launch prefers the macOS system WebView,
  falls back to the normal browser, and keeps explicit `--browser` and Chromium
  `--app` paths. The UI is fully strict TypeScript with generated OpenAPI types,
  runtime response validation, and zero-bundler ES-module output. One shared
  Pydantic operation/model catalog owns route binding, response validation, the
  served OpenAPI 3.1 document, and the committed client-generation input. Wheels
  install the UI, WebView hosts, and macOS icon as `share/heavenbase/gui` data,
  plus the published schema under `share/heavenbase/openapi`.
  `hb dashboard --install-app` generates a Finder/Dock-launchable application.
  Appearance and successful workspace selection persist through the same
  Context-owned `~/.heavenbase` profile for every local host.
- **Target:** Preserve that one-service, thin-interface, generated-protocol, and
  installable-macOS-application baseline.
- **Gap:** The generated local `.app` is not signed/notarized, and there is no
  Windows shortcut package. Both remain explicitly outside this completed
  macOS-first slice.
- **Non-goal:** Tauri, Electron, Neutralino, Wails, or similar desktop web-runtime
  frameworks as the default shell; React rewrite unless UI scale later needs it;
  moving MCP/CLI into `apps/`.

## Problem And Success Criteria

- Browsing scoped/versioned Context configuration otherwise requires the `hb config`
  CLI or programmatic Registry inspection; editing provider/model/preset trees
  as raw values is error-prone.
- Inspecting workspace rows requires Python; there is no dense, typed,
  repeat-inspection surface for entity data, schema, storage placement, or
  capability metadata.

**Current done means:** `hb dashboard` starts a local UI on `127.0.0.1:42777`, the
config workbench edits its Context's scoped config with history/diff/reset, the
data browser lists `WorkspaceSpec` rows from the same Context, opens one through
that Context, and browses entities with schema-aware tables, and streams
evidence-backed assistant turns with query frames and structured tool results —
all without new persistence or write access to workspace rows.

**Accepted layout done means:** the product outcome above remains true with the
completed extract to `apps/gui/`, WebView-default host, and `api/` promotion.

## Non-Goals

- No workspace entity-row writes (upsert/delete/alter) in this version.
- No new planner, config store, or row export system.
- No second SDK: every dashboard behavior is a call into existing public
  surfaces (`Context`, `ConfigOps`, `WorkspaceSpec`, `HeavenBase`,
  `Catalog`, `MetaSchema`).
- No bundler/Vite/Webpack chain. Bun checks and `tsc` emit typed ES modules
  directly; committed runtime JavaScript remains wheel-ready.
- No server-side assistant state: the chat panel stores history per workspace
  in the browser, resends the active workspace's history with each request,
  and calls the model configured under
  ``heavenbase.dashboard.assistant`` (``preset`` wins, else
  ``model``/``provider``/``gateway``; the shipped default is the ordinary
  ``chat`` preset), falling back to ``heavenbase.llm.default_preset``,
  through ``heavenbase.llm`` with response caching disabled.
- No Tauri-class default shell and no `--tauri` flag. System WebView is the
  accepted host target; browser and Chromium `--app` remain fallbacks.

## Layer Placement

### Current layout

`src/heavenbase/api/`, `src/heavenbase/dashboard/`, `src/heavenbase/cli/`, and
`apps/gui/` are interfaces outside the layer-0..7 core:

- `api/` may orchestrate `heavenbase.context`, `heavenbase.utils.config`
  (`ConfigOps`), `heavenbase.workspace` (`WorkspaceSpec`, `HeavenBase`), root
  facade symbols (`Catalog`, `MetaSchema`), stdlib, `starlette`, and `uvicorn`.
- `dashboard/` may import `api/` and host utilities; it does not reimplement
  config, workspace, assistant, or HTTP behavior.
- `cli/` and `apps/gui/` consume those interfaces. No core module imports
  `api/`, `dashboard/`, `cli/`, or `apps/gui/`.
- `starlette` and `uvicorn` are declared runtime dependencies; both already
  ship transitively via `fastmcp → mcp`, so the declared surface adds no new
  installed packages.

The split is one page of mental model:

| Module | Owns |
|--------|------|
| `api/service.py` | `DashboardAPI`: JSON-in/JSON-out application methods over existing surfaces (metadata, config, workspace, blocking/streamed assistant, progress); holds the open workspace handle; no HTTP types |
| `api/models.py` | Strict request/response/error/event models, the canonical route operation catalog, and OpenAPI 3.1 generation |
| `api/http.py` | Catalog-driven Starlette adaptation onto `DashboardAPI`, model validation, `LocalGuard`, external UI serving, `build_app()` + `resolve_bind()` + `run_server()` |
| `dashboard/launch.py` | Host selection, server readiness, WebView/browser/Chromium fallbacks, and public `serve()` lifecycle |
| `apps/gui/host/macos.js` | Minimal AppKit + WebKit system host for the loopback URL; no business logic |
| `dashboard/macos.py` + `apps/gui/host/macos_app.js` | Atomic `.app` installation plus Finder-owned server/WebView lifecycle |
| `apps/gui/ui/` | Heaven-style three-theme frontend; fully strict `src/*.ts`, generated OpenAPI types, checked-in `js/` runtime; no bundler |

`DashboardAPI` is independently testable without HTTP; `api/http.py` is a thin
route adapter; the host only opens the URL; the frontend talks only to the JSON
API. This keeps GUI, host, transport, and application behavior strictly
separated.

The current dashboard contract, including the streamed assistant and UI
hardening, is recorded on this page and protected by the dashboard API/UI tests.
The configuration workbench uses schema-owned field semantics rather than
provider-name branches: LLM providers, models, presets, gateways, defaults, and
database providers share grouped rows, searchable references, explicit
inherited-value labels, and open-ended key/value editors with a permanent draft
row. Credential and location fields persist one environment-first reference
with a manual fallback; the field reports the actual winning source and final
resolved value while keeping credentials masked. Model/provider IDs have a
structured common editor, while provider-specific parameters remain in JSON
mode. Thinking and reasoning effort are dedicated model/preset controls. Each
entry switches in place between the structured form and a pinned, vendored
CodeMirror JSON editor whose copy, format, validation, and save actions remain
inside editor mode. Provider backend mappings are edited only in JSON mode;
configuration mode shows the derived backend for the default route and every
registered gateway. Generic defaults, MCP, workspace, storage, and dashboard
forms reuse the same grouped section language. Built-in database providers cover
every concrete SQL backend, and every bundled backend type has a stable brand or
semantic icon. The toolbar stays on the active Context scope instead of exposing
an unrelated version selector.

The assistant composer owns its per-conversation controls. It resolves the
shipped ``chat`` preset by default, shows each preset's model and effective
thinking/effort metadata, and keeps the read-only/full-access switch beside the
input. Starting a new conversation clears only browser-owned history; it does
not create or mutate a special assistant preset in the configuration catalog.

### Remaining target layout

Optional follow-up work may sign/notarize the generated macOS application or add
Windows packaging. Neither creates a second service boundary or changes the UI
framework policy.

## Public Surface

```python
import heavenbase as hb
from heavenbase.api import DashboardAPI, build_app, openapi_schema, resolve_bind
from heavenbase.dashboard import install_macos_app, serve

context = hb.Context.load(home_path="bootstrap.yaml")
api = DashboardAPI(context=context)
api.meta()                    # {"name": "HeavenBase", "version": "..."}
app = build_app(api)          # starlette.applications.Starlette
openapi_schema()              # generated OpenAPI 3.1 mapping
resolve_bind()                # ("127.0.0.1", 42777) from heavenbase.dashboard.*
serve(host="127.0.0.1", port=42777)   # uvicorn, blocks
install_macos_app()           # ~/Applications/HeavenBase.app
```

`DashboardAPI()` is the unbound convenience and selects `hb.DEFAULT_CONTEXT`.
Passing a Context is the isolation contract: config, workspace listing/opening,
module resolution, and assistant tools remain in that composition.

`build_app(api)` calls the methods of the instance it is given, so a
`DashboardAPI` subclass may override them. The session-log identifiers
`conversation_id` and `turn_id` of `assist`/`assist_stream`, added in 0.1.2.3,
are passed to an override only when it accepts them (a named parameter or
`**kwargs`), whatever value the client sent. An override written with the
parameter list of 0.1.2.2 therefore keeps serving every client, including the
bundled UI, which sends both on every chat turn; the override does not receive
them and records no session log. Every other request field is always passed:
an override that cannot accept one, such as a config `scope` or an assist
`read_only`, makes the request fail with HTTP 400 on both the blocking and the
streaming route rather than run without it.

CLI: `hb dashboard [--host H] [--port P] [--no-open] [--browser] [--app]
[--allow-network] [--install-app] [--yes|-y]` registered as a root
`CommandSpec` in
`cli/groups/root.py`; the callback lazy-imports `heavenbase.dashboard.serve`.
Port `0` requests an OS-assigned port. The Python launcher and Finder app
open the bound port after the HTTP service starts.
**Current launch:** the command prefers the 1280 × 800 system WebView host on
macOS and falls back automatically to a normal browser when unavailable.
`--browser` selects the normal browser directly; `--app` selects a 1280 × 800
Chromium-family chromeless window and falls back to the browser when no candidate
exists. Non-loopback binds are refused unless the user supplies the explicit
`--allow-network` security opt-in. `--install-app` generates the user-local
Finder/Dock application and exits. An existing bundle triggers a confirmation;
`--yes`/`-y` skips it for non-interactive installation.

## API Standard Table

| Symbol | Kind | Layer | Inputs | Returns | Raises | Doc page | Test anchor |
|--------|------|-------|--------|---------|--------|----------|-------------|
| `DashboardAPI` | class | application API | `context: Context = DEFAULT_CONTEXT` | — | `TypeError` invalid Context | this page | `tests/interfaces/test_dashboard.py::test_config_tree_origins` |
| `DashboardAPI.meta` | method | application API | — | package `name` and running `version` | — | this page | `tests/interfaces/test_dashboard.py::test_http_routes` |
| `DashboardAPI.config_meta` | method | application API | — | scopes, chain, versions, resource root | — | this page | `tests/interfaces/test_dashboard.py::test_config_tree_origins` |
| `DashboardAPI.config_tree` | method | application API | `scope`, `version` | merged raw tree + per-key origin | `ValueError` bad scope | this page | `tests/interfaces/test_dashboard.py::test_config_tree_origins` |
| `DashboardAPI.config_set` | method | application API | `key`, `value`, `scope`, `parse` | `{ok}` | `ValueError` | this page | `tests/interfaces/test_dashboard.py::test_config_set_unset_reset` |
| `DashboardAPI.config_unset` | method | application API | `key`, `scope` | `{ok}` | `ValueError` builtin key | this page | same |
| `DashboardAPI.config_history` | method | application API | `scope`, `limit` | version rows | — | this page | `tests/interfaces/test_dashboard.py::test_config_history_versions_and_safe_diff` |
| `DashboardAPI.config_diff` | method | application API | scope/version pairs | unified diff text | — | this page | same |
| `DashboardAPI.config_reset` | method | application API | `key`, `scope` | `{ok}` | `ValueError` | this page | `tests/interfaces/test_dashboard.py::test_config_set_unset_reset` |
| `DashboardAPI.preferences` | method | application API | — | shared appearance preferences + configured marker | — | this page | `tests/interfaces/test_dashboard.py::test_dashboard_preferences_are_shared_and_validated` |
| `DashboardAPI.update_preferences` | method | application API | optional theme/language/font/font-scale values | complete shared appearance preferences | `ValueError` through HTTP model validation | this page | same |
| `DashboardAPI.workspaces` | method | application API | — | `WorkspaceSpec.to_dict()` list | — | this page | `tests/interfaces/test_dashboard.py::test_workspace_flow` |
| `DashboardAPI.remove_workspace` | method | application API | `ws_id` | unregister result + prior open state | `ValueError` invalid id, `RuntimeError` live release failure | this page | `tests/interfaces/test_dashboard.py::test_dashboard_remove_workspace_releases_session_without_dropping_data` |
| `DashboardAPI.open` | method | application API | `ws_id` | health payload; persists active workspace after successful health | `KeyError` unknown id, `RuntimeError` backend failure | this page | `tests/interfaces/test_dashboard.py::test_dashboard_open_persists_workspace_selection` |
| `DashboardAPI.health` | method | application API | — | `ws.health()` + spec meta | `ValueError` none open | this page | same |
| `DashboardAPI.entities` | method | application API | `include_system`, `include_counts` | entity cards (id, desc, optional count, system, extension) | — | this page | same |
| `DashboardAPI.entity_schema` | method | application API | `entity_id` | fields + storage + per-field query capabilities + backends | `KeyError` | this page | `tests/interfaces/test_dashboard.py::test_entity_schema` |
| `DashboardAPI.entity_rows` | method | application API | `entity_id`, `filter`, `order`, `offset`, `limit`, `select` | `{rows, total, columns}` | `KeyError`/`ValueError` | this page | `tests/interfaces/test_dashboard.py::test_entity_rows_paging` |
| `DashboardAPI.entity_row` | method | application API | `entity_id`, `object_id` | one row dict | `KeyError` | this page | same |
| `DashboardAPI.column_stats` | method | application API | `entity_id`, `field` | statistics folded inside the aggregate stage over every row, so `scanned` equals `total` and `sampled` is `False`; `<key>_truncated` marks any cut most-common list, `scalars` counts Json rows that were neither object nor list, and each published value cell carries `value_truncated`/`value_len` when elided | `KeyError`, `ValueError` when the fold is refused engine-wide | this page | `tests/interfaces/test_dashboard.py::test_column_stats` |
| `DashboardAPI.search` | method | application API | `text`, `limit` | `{rows, n, total, truncated}`; ranking scores every match before the cap, so `total` is the exact match count | `ValueError` | this page | `tests/interfaces/test_dashboard.py::test_entity_row_and_search` |
| `DashboardAPI.assistant_key_status` | method | application API | `provider`, `reveal`, `field` | masked or explicitly revealed local editor value plus literal/environment/fallback source and environment detection | `KeyError`/`ValueError` | this page | `tests/interfaces/test_dashboard.py::test_assistant_key_status` |
| `DashboardAPI.config_value_status` | method | application API | `key`, `reveal` | effective environment/manual source, mask tail, and optional resolved value | — | this page | `tests/interfaces/test_dashboard.py::test_assistant_key_status` |
| `DashboardAPI.assist` | method | application API | `question`, `context`, `history`, `llm`, `progress_id`, `preset`, `read_only`, `conversation_id`, `turn_id` | `{text, think, served_model, frames, sql_frames, events, elapsed}`, plus `capture_warning` when the session log recorded the turn incompletely; a declared SQL evidence frame carries the exact statement, parameters, bounded actual rows, counts, and truncation/read-only markers | `ValueError` empty/LLM failure | this page | `tests/interfaces/test_dashboard.py::test_assist_returns_lossless_sql_execution_evidence` |
| `DashboardAPI.assist_stream` | method | application API | `question`, `context`, `history`, `llm`, `preset`, `read_only`, `conversation_id`, `turn_id` | generator of reasoning/text/tool/result/done events, the `done` event carrying `capture_warning` when the session log recorded the turn incompletely; SQL tool and done events preserve the same `sql_frame`/`sql_frames` evidence as blocking responses | `ValueError` empty input | this page | `tests/interfaces/test_dashboard.py::test_assist_returns_lossless_sql_execution_evidence` |
| `DashboardAPI.assist_progress` | method | application API | `progress_id` | active blocking-call activity lines | — | this page | `tests/interfaces/test_dashboard.py::test_assist_progress_registration_is_collision_safe_and_always_cleaned` |
| `openapi_schema` | function | protocol models | — | OpenAPI 3.1 mapping from the canonical operation/model catalog | — | this page | `tests/interfaces/test_dashboard.py::test_openapi_is_served_from_the_runtime_operation_catalog` |
| `build_app` | function | HTTP adapter | `api: DashboardAPI`, `allowed_hosts`, `static_dir` | Starlette app in `LocalGuard` | `FileNotFoundError` missing UI | this page | `tests/interfaces/test_dashboard.py::test_http_routes` |
| `resolve_bind` | function | HTTP adapter | `cm`, `host`, `port` | `tuple[str, int]` | — | this page | `tests/interfaces/test_dashboard.py::test_dashboard_launch_contract` |
| `serve` | function | desktop launcher | `host`, `port`, `open_window`, `api`, `host_mode`, `allow_network`, keyword-only `on_started` | `None` (blocks) | `ValueError` unsafe bind or host mode; `SystemExit` with status 1 when the address cannot be bound (port busy), with status 3 when application startup fails | this page | `tests/interfaces/test_dashboard.py::test_network_bind_requires_explicit_opt_in_and_handles_ipv6` |
| `install_macos_app` | function | desktop packaging | exact optional `.app` destination, `replace`, optional Python runtime | installed bundle path | `RuntimeError`, `FileExistsError`, `ValueError` | this page | `tests/interfaces/test_dashboard.py::test_macos_app_bundle_installation` |

## Data And Control Flow

- Application metadata path: `GET /api/meta` → `DashboardAPI.meta()` → the
  installed package name and running `heavenbase.version.__version__`. The
  About dialog reads this endpoint so it cannot drift from the server package.
  `GET`/`POST /api/preferences` read and write appearance through that same
  Context's base profile; local storage is only a host cache and a one-time
  source for importing legacy non-default browser preferences.
- Protocol path: `DASHBOARD_OPERATIONS` + the Pydantic models in
  `api/models.py` → catalog-built Starlette routes and request/response
  validation → served `/openapi.json` and the freshness-checked
  `docs/resources/reference/dashboard-openapi.json` → generated
  `ui/src/generated/schema.d.ts`. Network JSON still enters the frontend as
  `unknown` and passes route-shaped runtime guards before typed UI code sees it.
- Config read path: `api.context.config.load_default()` plus raw
  `api.context.config.layer(scope)` per chain
  scope, merged with `dmerge` — never the interpolated snapshot, so
  `${env:...}` secrets are shown as written, not resolved. Per-key origin is
  computed by flattening the default layer vs user layers: `builtin`
  (default only), `override` (default + user), `custom` (user only). The UI
  locks delete/rename for `builtin`/`override` keys and offers reset
  (unset override) instead.
- Config write path: `ConfigOps.set` / `ConfigOps.unset` into the owning Context's
  ConfigManager scoped Registry layer; nothing writes `default.yaml`, and
  the dashboard does not assume a particular Registry backend.
- Data read path: `WorkspaceSpec.list(context=api.context)` →
  `WorkspaceSpec.load(ws_id, context=api.context).open(context=api.context)` →
  successful `ws.health()` → `api.context.activate_workspace(ws_id)`. The
  activation is committed only after the workspace is healthy and becomes the
  next host's selected workspace; when no explicit selection exists, the
  configured default wins instead of an alphabetically first registration.
  The picker exposes `DELETE /api/workspaces/{ws_id}` behind a confirmation;
  it unregisters the `WorkspaceSpec`, releases a matching live dashboard
  facade, and never drops backing database data.
  The opened workspace then supplies one immutable filtered query branched into the ordered/projected/paginated
  page query and a source-only terminal `count().execute().scalar()` query (or
  `ws.count(entity)` without a filter); `entity.schema().to_dict()` plus
  MetaSchema rows (`kind` in `field`/`storage`/`backend`) for column typing,
  the Schema view, and field tooltips including per-field query capabilities;
  `ws.health()` for the status strip and the health inspector;
  `ws.search(text)` for the ⌘K palette over Catalog/MetaSchema cards.
- Assistant path: `POST /api/assist/stream` → `DashboardAPI.assist_stream` → the
  LLM settings resolved from `api.context.config` →
  `heavenbase.llm.LLM(..., cache=False).stream`.
  Server-sent events carry reasoning, spoken text, tool calls, bounded
  structure-preserving results, lossless query frames, errors, and the final
  response envelope. The HTTP adapter validates every event against the
  OpenAPI model, then bridges the synchronous generator through a dedicated
  daemon thread and a bounded, acknowledged event-loop queue. Client
  cancellation therefore closes the response immediately without consuming an
  anyio limiter token; a stalled provider isolates only its daemon thread until
  it reaches the next yield boundary, where the generator closes. While the
  provider is silent the adapter beats `: keepalive` SSE comments every 15s
  (`ASSIST_KEEPALIVE_SECONDS`); the client aborts a stream silent for 90s and
  caps the blocking fallback at 150s total, so a dead transport surfaces as a
  failed turn instead of hanging the panel. The UI turns
  Send into Stop while a request is active, forwards one `AbortSignal` through
  both streaming and blocking-fallback paths, never retries an abort, and
  retains already streamed evidence as an interrupted assistant turn. A
  mid-stream failure likewise keeps the streamed reply visible with a failure
  footnote; failed and error turns are never replayed to the model — each is
  dropped together with its user question to preserve role alternation.
  `POST /api/assist` remains the blocking equivalent and
  `/api/assist/progress` exposes collision-safe transient activity for clients
  that cannot consume the native stream. Both paths use the same core
  inspection callables (`list_entities`, `entity_schema`, `sample_rows`,
  `column_stats`, `query`, `explain`, `search_workspace`, `get_config`) so the
  model fetches real workspace/config data via the LLM tool loop instead of
  guessing. Eligible MCP profiles may opt into dashboard chat through their
  `meta.dashboard_assistant` descriptor. The service generically resolves those
  profiles against the immutable open-workspace snapshot, adds only their
  declared read-only or interactive allowlist, and folds their instructions
  into the system context. Beside profiles, any installed module may
  contribute plain assistant tools through the open `assistant_tools`
  contribution kind: a Registry row whose target resolves to
  `provider(workspace) -> [callables]` (schema from `__signature__` or an
  explicit `__tool_schema__`; a `read_only = True` attribute or standard
  `readOnlyHint` annotation opts a tool into read-only mode;
  `meta.workspace_requires` gates activation like every other contribution).
  Provider code owns ordinary callable composition, including partial binding,
  selection, documentation, and result provenance. Contributions append after
  the built-ins, ride the same narration and evidence capture, and lose name
  collisions to earlier tools with a warning. External MCP servers plug in
  through `heavenbase.dashboard.assistant.mcp_servers` (name plus stdio command
  or HTTP URL, with an optional allowlist). The shared FastMCP adapter owns
  protocol negotiation and result conversion;
  the dashboard only namespaces imported tools as
  `mcp__<server>__<tool>` and applies its ordinary tool pipeline. Official
  per-tool `readOnlyHint` annotations govern read-only mode unless the operator
  supplies an explicit server-level `read_only` override. A malformed or dead
  server contributes no tools and warns once per exact configuration. The
  Database profile uses this seam for
  credential-safe named-source setup, scoped entity edits, guarded live SQL,
  verified snippets, and rule retrieval; the dashboard has no Database import
  branch.
  Profiles may also declare generic evidence-frame mappings for tools whose
  native result must remain first-class. The Database profile maps `run_sql`
  arguments and results into `sql_frames`; each frame preserves the executed
  statement, bind parameters, source id, columns, bounded actual rows, exact
  available count, limit/truncation, and read-only status. Provider scalars are
  detached to lossless JSON boundary values (for example ISO timestamps and
  decimal strings). Blocking responses, streamed tool/done events, and the UI
  use the same frame, so the final analysis cannot replace or hide its SQL and
  evidence. The chat renders a copyable SQL block and result table beside the
  assistant narrative.
  The canonical `query` tool supports filters, Json dotted paths, grouping,
  registered aggregation, ordering, and bounded pages; `explain` returns the
  router's plan for the same spec without reading rows. Executed calls are
  matched back to their declared arguments so malformed tool calls cannot
  steal a later call's evidence frame. Per-turn query evidence is capped by
  `heavenbase.dashboard.assistant.max_frames`; non-query tool payloads are
  bounded by `heavenbase.dashboard.assistant.max_tool_result_chars`; the
  tool loop's round budget is `heavenbase.dashboard.assistant.max_tool_turns`
  (default `8`). Query frames default to `5` per response and non-query tool
  payloads default to `6000` displayed characters. Model-side capture clipping
  is separately controlled by `result_capture_chars` and is disabled by
  default (`0`); when enabled, query frames drop rows outside the same
  serialized prefix so the displayed evidence matches what the model saw.
  The dashboard opts its LLM calls into in-turn compaction at 92% of the
  configured model context window, or a conservative 128000-token fallback.
  Provider-reported prompt usage governs fullness when available and the
  character estimate covers only the unmeasured delta. Compaction folds the
  oldest tool rounds into one working-memory message while the newest two stay
  verbatim and the recorded response delta remains complete. Generic LLM calls
  do not clip or compact unless their caller supplies those policies.
  Query results are bounded separately per unit, because aggregation and
  `HAVING` run before ordering, offset, and limit (ADR 0009) — a limit on an
  aggregate spec truncates **result groups**, not source rows. Row reads are
  capped by `heavenbase.dashboard.assistant.max_query_rows` (default `100`,
  default limit `50`) and aggregate reads by
  `heavenbase.dashboard.assistant.max_group_rows` (default `1000`, also the
  default limit, with an explicit caller limit preserved up to the cap).
  Pagination runs last on a complete result, so a truncated frame reports its
  exact pre-limit count (`ResultFrame.total`): the payload always carries the
  applied `limit` and a `truncated` flag, plus `total` and a `note` naming the
  number that actually matched, and the client renders that count in a
  `1–n / total · truncated` caption instead of inferring loss from row count.
  `get_config` recursively masks credential-bearing keys, strings, and URL
  passwords as `***` by default. A user may explicitly enable
  `heavenbase.dashboard.assistant.share_credentials`, but the GUI requires a
  serious disclosure acknowledgment because raw values can leave the machine
  for the external model provider. Workspace tools bind to one workspace
  snapshot for the whole model loop. Replies render through a safe DOM-only
  Markdown/math renderer; arrays, mappings, and frame-shaped tool results become
  inspectable tables with lossless copy actions. The server remains stateless
  per request (the browser keeps histories isolated by workspace and resends
  only the active one); provider/config failures surface
  as one clean error event or `ValueError` message in the chat panel.
  The browser and service both retain at most `history_turns` prior turns
  (default `40`). Cross-turn tool evidence carry is opt-in through
  `heavenbase.dashboard.assistant.evidence_total` (default `0`): enabled
  evidence is cached per service instance under the workspace, user question,
  and answer text, then reattached newest-first within the configured budget.
  `history_chars` is also opt-in (default `0`). With a positive threshold,
  requests under it replay full evidence; requests over it compact the oldest
  turns through the selected route and keep the remaining suffix verbatim.
  Compact summaries are cached by workspace, route, and exact turn prefix.
  Failed compaction falls back to the full → brief → drop evidence ladder and
  never prevents the current question from being answered.
- Server binds `127.0.0.1` by default and serves only the local user; when an API is
  supplied, the port default `42777` reads from its Context's ConfigManager. The
  no-argument server path builds one `DashboardAPI()` over the process default
  Context and reads the boot check, host, port and `open` from its ConfigManager:
  `CM_HVNB`, unless `set_default_context` installed a Context with its own
  ConfigManager. `resolve_bind()` without a `cm` reads the same ConfigManager.
  `LocalGuard` rejects non-allowlisted `Host` headers (DNS rebinding) and, for
  state-changing methods, any `Origin` whose full `host:port` differs from the
  request host — a page on another loopback port cannot write config. Broad
  loopback aliases and IPv6 URL formatting are handled explicitly. Every
  non-loopback bind (broad or concrete LAN address) is rejected by default;
  `--allow-network` permits it with a serious warning that the unauthenticated
  config API is exposed. Synchronous service work runs in Starlette's worker
  pool so an LLM call or statistics scan does not block unrelated requests.
- macOS application path: `hb dashboard --install-app` atomically writes
  `~/Applications/HeavenBase.app` with the stable bundle identifier, compiled
  stay-open JXA applet, ad-hoc signature, `.icns` icon, and the installing
  virtual-environment runtime path. An existing target requires interactive
  confirmation unless `--yes`/`-y` is supplied. Finder launch starts a
  loopback-only server child and waits for that child to report a successful
  bind before opening the WKWebView. Closing its final window terminates the
  child. CLI launch sends the
  already-running loopback URL to a fresh app instance, so the CLI retains
  server ownership. The app never grants the CLI's `--allow-network` waiver.

## Assistant Session Log

Three record concepts, named once so product wording never conflates them:

- **Readable transcript** — the user-facing conversation projection
  (`agent-session`/`agent-message` rows), never an authority: for every
  conversation the log still holds it can be rebuilt from the events
  (`AssistSessionLog.rebuild_session`, which only reads them, replaces the
  conversation's own message rows and recreates a missing session row).
- **Canonical provider-bound trace** — what this subsystem records: the
  complete normalized request HeavenBase hands its gateway (messages as
  sent, offered tool schemas, request-shaping options including the
  effective stream mode, resolved route), captured BEFORE gateway-native
  compilation.
- **Wire request** — the gateway-compiled network payload. Not recorded;
  it is a deterministic function of the canonical trace plus the gateway
  code, and claims must not be worded as raw wire bytes.

`heavenbase.dashboard.assistant.session_log` (default **off**; `true` opts a
deployment in) writes every
provider-bound model call of an assistant turn as immutable `agent-trace`
rows (`AgentTrace`, owned by the optional `agent-log` extension) in a
dedicated log workspace. The recorder enables the extension there, and
other workspaces can opt in independently. While the recorder
is healthy, each `request` row is persisted synchronously BEFORE provider
I/O (write-ahead, never memory-batched); `request`/`response`/`error` rows
pair by `seq`; a `request` with no terminal sibling is itself the durable
record of an interrupted call, never a fabricated response. A cache-served
reply emits no events — no provider saw a request — which is correct
semantics, not a gap. Provider-call rows are immutable. Retries that call a provider
use event IDs that include the attempt number and display the attempt as
`<turn>-aN`. One turn runs at a time per conversation in a log workspace
within a parent Context and its children. Other conversations can run
concurrently. A streaming request that disconnects while waiting stops before
admission. A retry of a completed turn replays its stored answer without
provider I/O. A recorded `turn` row binds
its question and workspace, and changing either on retry is rejected. The
first successful retry supersedes a failed or interrupted `turn` row and
preserves the prior failure in the new row's `superseded` field.

The `seq` of a sealed `turn` row records its completion order within the
conversation. The summary ID remains fixed across retries. Sealing advances
one durable counter per conversation in the log workspace's own
`agent-log-seal` table. When the counter is missing, sealing initializes it
from that conversation's turn summaries.
After initialization, sealing allocates the next sequence without querying
previous summaries. A failed summary write can leave a gap in the sequence.
Rebuild orders turns by sequence. For older rows without a recorded sequence, it
preserves the relative order of turns already listed in the session and uses
timestamps to place the rest. The transcript retains full questions and
answers, including when their searchable text projections are shortened.
Replay rebuilds a missing session even when the stored answer is blank, and
rebuilds an existing session when a blank answer's usage was never recorded.
For a nonblank answer, replay also rebuilds the transcript if the projected
question or answer is missing or differs from the trace. Blank answers stay
out of the transcript. A failed append or replay
repair carries `capture_warning` with a `projection:<Error>` reason.

Doctrine split: `serve()`/`run_server` refuse to boot on a session-log
backend declared in memory or on `session_log` + `share_credentials`
(credentials at rest) without the explicit `session_log_allow_credentials`
opt-in, re-checked per turn so a post-boot flip stops recording visibly. A
declaration is in memory when its `type` names the in-memory backend by any
alias in any case (`inmem`, `memory`, `InMemBackend`), when its database says `in_memory`, names
`:memory:`, or is a `url` that selects memory mode (`sqlite://`,
`sqlite:///:memory:`), when its `url` or `uri` is on the `mem://` or
`memory://` scheme, or when its `type` names Chroma by any alias in any case
(`chroma`, `ChromaBackend`) and it has no `path`. Backend types and registered SQL dialects resolve through the module registry.
The first lookup miss per module registry in a process indexes the packaged
modules. Later misses reuse that setup, including calls that overlap it. URLs
without a registered SQL dialect, such as `file://` or `redis://`, are left
to construction. A declaration that
reaches an in-memory store only through resolution, such as a database
preset, passes the boot check but is refused when the log workspace first
opens, so each such turn goes unrecorded with a `capture_warning`. A
workspace registration that the refused open created is removed again, so
declaring a durable backend under the same `session_log_workspace` works
next turn; a registration that existed before is kept, and the refusal names
`hb ws unset <id>`. Reads that determine whether a turn should replay or
start are retried once. If the retry also fails, the request fails before
provider I/O, and any admission slot is released. A failed history lookup
cannot establish that a conversation is new for retention. Other recording
failures are reported once per failure class and surface as `capture_warning`
on the reply and the stream `done` event. Only
a turn whose `capture` reads `complete` may be described as fully traced, so
never word this as "every model request is always persisted". Credential and
client-wiring channels (api keys, auth tokens, base URLs, proxies, headers)
structurally never enter the canonical trace, and outside the model-visible
options any other value under a credential-looking name (`*key*`, `*token*`,
`*secret*`, `*password*`, `*passwd*`, `*credential*`, `auth*`, at any depth,
`extra_query` and `extra_body` included) is stored as `"***"` unless it is
null, a boolean, a number, or a list, tuple or set of only those (the LLM
reference lists the exact rules), but messages and tool results
are business data and stay under ordinary workspace data governance — do not
claim the log is free of sensitive content. Client identity: the GUI mints
one `conversation_id` per transcript (rotated by "New conversation") and one
`turn_id` per submission, reused by the blocking fallback so retries are
idempotent. When a client sends only `turn_id`, that id determines the
conversation within the log workspace. With neither id, each call opens a
new conversation. Run one dashboard process per log workspace (single writer).

Storage and reads. Each `request` row stores the complete message list of
that provider call, inline images included, so a turn with k tool rounds
over a growing context stores k near-copies of it: a turn's log grows
roughly with the square of its tool rounds (for example, 20 rounds over a
context that grows steadily to 400 KB store about 4 MB, half of
20 × 400 KB, because the early requests are small). Retention is
whole-session only. When the recorder admits a new conversation,
`session_log_max_sessions` (default 256) evicts the oldest eligible idle
conversations with their message, trace and seal counter rows. There is no byte budget or time-based
expiry; size the log volume for `max_sessions` × your longest turns, or
lower the cap. Retention counts a conversation by its session row while
that row's `state.conversation_id` is intact (other `state` keys,
`state.source` included, may be edited); a row whose
`state.conversation_id` was changed or removed is no longer recognized, so
retention never counts or deletes it or that conversation's trace rows. A
conversation whose session row is missing is neither counted nor deleted
until `rebuild_session`, a retry that restores one of its completed turns,
or its next turn recreates the row. Rebuild and eviction find the
conversation's message rows by session ID, including rows written by failed
appends. A rebuild reads the trace and replaces only that conversation's
projection. An explicit rebuild, a replay repair, or a new turn in a logged
conversation recreates a missing session row from the trace. The row uses
the first retained turn summary's timestamp when available. These restores
can take the log over the cap. New admissions apply retention by session age.
Conversations running or waiting for turns count toward the cap when they
have a session row. They are ineligible for eviction, so the cap can also be
exceeded when too few idle conversations are available. An admission
prepares the retention batch before it publishes the new session row.
Admissions of new conversations run their retention passes one at a time.
A turn on a conversation being evicted waits for the pass. Turns of other
conversations already in the log start and finish while the pass reads and
plans; its single bulk delete of the evicted rows is a workspace write, so
they wait for it like any concurrent write to the log workspace. Admission, retry-attempt detection, eviction and rebuild query trace
rows by `conversation_id`. When the log backend groups those rows natively,
as SQLite does, eviction and retry-attempt detection read only row ids and
coordinates, never request payloads or message text. The log is an ordinary registered
workspace (`session_log_workspace`, default `assistant-log`): it appears in
the workspace list and can be opened from the dashboard or MCP, and it holds
copies of whatever the assistant's tools returned, including rows read from
external databases.

## Extension Seam

The dashboard adds no registry of its own. New entity types, backends, and
capabilities appear automatically because the browser renders from
`MetaSchema`/`Catalog`/schema exports rather than hard-coded entity lists.
Config domains are driven by the default-layer tree, so new `default.yaml`
domains appear without dashboard edits.

## Tests

`tests/interfaces/test_dashboard.py`, fast markers, no external services:
compressed behavioral journeys exercise `DashboardAPI` against a temporary
Context whose ConfigManager and Registry are scoped to a temporary root, plus a
Context-owned workspace with a small entity. HTTP tests cover routing,
host/origin guards, worker concurrency, bind security, IPv6, CLI, host
preference/fallbacks, `.app` generation/lifecycle metadata, and the 1280 × 800
window contract. The existing Node
built-in gate exercises pure frontend transitions in
`tests/interfaces/dashboard_ui.mjs`, including structured tables, copy fidelity,
panel geometry, and Markdown/math edge cases. `apps/gui` additionally runs
OpenAPI type freshness, Biome formatting/linting, strict TypeScript checking,
Bun tests, and deterministic zero-bundler emission. Distribution verification
installs the built wheel in an isolated environment and loads
`share/heavenbase/gui/ui`, both macOS host scripts, and the packaged `.icns`.

## Maintenance

- Keep blocking and streamed assistant envelopes behaviorally equivalent.
- Keep assistant cancellation single-owner and non-retrying across streaming,
  blocking fallback, partial-turn persistence, and server generator teardown.
- Keep query evidence matched to the executed call, not positional model output.
- Keep frontend parsing DOM-only and cover hostile or malformed Markdown/math.
- Keep workspace tools read-only and bound to one snapshot per model loop.

## Risks And Waivers

- Config values containing secrets remain raw in the configuration tree. The
  environment-first field editor separately asks the local server for the
  resolved source and value, keeps credentials masked in its status line, and
  never requests a revealed environment credential. Assistant `get_config`
  output is recursively masked
  unless the user explicitly grants raw credential access after the serious
  external-provider warning. Per-chat read-only mode overrides that grant,
  removes Python tools, and rejects executable `code_reduce` aggregates. The
  check resolves every key of a query spec, and the comparison a Json `get`
  filter names, through the planner's operation normalization, so other case,
  surrounding space, and aliases such as `$code_reduce` are refused too, as are
  specs and nested values written as JSON text.
- Workspaces with in-memory backends show only rows written in the dashboard
  process; the health panel labels backend types so this is visible.
- `ws.rows()`-scale entities are never fully materialized; the browser always
  pages through `query_json` limit/offset.
