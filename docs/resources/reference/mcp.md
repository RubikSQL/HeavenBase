# HeavenBase MCP Toolkit

HeavenBase can expose an already configured workspace or one backend instance as a HeavenBase-native `Toolkit`. The MCP path uses the root imports `hb.Capsule` and `hb.Toolkit`; their canonical owners are `src/heavenbase/capsule/` and `src/heavenbase/toolkit/`, while Toolkit's Entity class is resolved through the module Registry.

Toolkit is optional workspace policy even though it is in the current packaged
default root list. A workspace created with Toolkit omitted must call
`ws.enable_extension("toolkit")` before using workspace Toolkit persistence or
MCP profiles. Backend-local in-memory Toolkit composition does not require a
user workspace activation.

The generated workspace toolkit is intentionally small and provider-agnostic. Agents inspect entity structure through `list_entities`, `describe_entity`, and MetaSchema (`sys-metaschema`), discover concrete objects through Catalog (`sys-catalog`), mutate rows with single-row or batch tools, query rows, and ask for storage/backend explanations without knowing the underlying backend class.

## FastMCP 4 Runtime

HeavenBase requires FastMCP `4.0.0b1` or newer. FastMCP 4 is built on MCP
Python SDK v2, so HeavenBase reads protocol model fields directly by their
Python snake_case names, including `input_schema` and `structured_content`.

FastMCP clients now negotiate the newest shared protocol by default. Against a
FastMCP 4 server, that is normally the sessionless `2026-07-28` protocol.
HeavenBase Toolkit calls are request-local and do not use cross-request MCP
context state, server-initiated sampling, roots, elicitation, or background-task
extensions. The generated server does use FastMCP's lifespan for server-owned
resources such as the optional result cache. An MCP protocol connection is not
a HeavenBase state or persistence authority.

FastMCP 4's main architectural value for HeavenBase is a cleaner adapter
boundary, not a new application architecture:

- a successful MCP call carries the JSON-compatible native value as structured
  output while the Tool serializer supplies separate agent-readable text;
- FastMCP owns server startup and shutdown instead of a HeavenBase background
  thread wrapper;
- optional FastMCP transforms can shape the exposed component names without
  changing Toolkit ownership or persistence.

HeavenBase does not add a second FastMCP middleware or authentication layer.
Workspace profiles, entity scopes, Toolkits, Capsules, Registry records, and
Backend policy remain the authorities already responsible for exposure and
execution.

The preview floor also needs a matching `fastmcp-slim` uv constraint because
uv does not opt transitive packages into prereleases automatically. This
repository owns that resolver constraint in `pyproject.toml`.

## Bounded Backend

Create a normal `HeavenBase` workspace, then expose one backend with `ws.to_mcp(backend=...)`. The backend can be a backend name from the workspace or a backend instance. Backend MCP is not the workspace schema/CRUD/query surface; it exposes physical backend inspection plus provider-specific tools.

Every backend owns this surface through `Backend.mcp()`. The method returns one `hb.ext.BackendMCP` spec that keeps tool callables, prompts/descriptions, profiles, default profile, and serializer choice together. New backend classes can override `mcp()`, call `super().mcp()`, and add provider-specific tools/profiles without touching workspace MCP registries.

```python
import heavenbase as hb

ws = hb.HeavenBase("demo", backends={"main": {"type": "inmem"}})
toolkit = ws.to_mcp(backend="main", name="demo-mcp")
print(toolkit.list_tools())
print(toolkit.run("describe_backend"))
```

For SQLite, the default backend MCP surface is still inspection-first:

```python
ws = hb.HeavenBase("sqlite-demo", backends={"main": {"type": "sqlite", "database": "file://%/.tmp/mcp-demo.db"}})
toolkit = ws.to_mcp(backend="main")
print(toolkit.run("backend_tags"))
```

You can also expose an already-created backend instance directly:

```python
backend = ws.backends.get("main")
toolkit = backend.to_mcp(name="sqlite-demo")
```

A backend-only toolkit is scoped to that backend object. It shares the backend instance with the caller, does not register a hidden `HeavenBase` workspace, and does not replace the process default workspace.

Provider backends can contribute narrower tools by overriding `Backend.mcp()` and adding provider-specific tools after calling `super().mcp()`. Keep those tools about the physical provider; schema-aware row access and sparse-GRAM search stay on the workspace toolkit and query surface.

```python
class MyBackend(hb.ext.Backend):
    def mcp(self) -> hb.ext.BackendMCP:
        spec = super().mcp()
        spec.tool("provider_probe", self.provider_probe, desc="Inspect provider-specific state.")
        return spec
```

Callers can pass a custom backend profile object or mapping when an agent should see a smaller tool surface:

```python
tags_only = hb.ext.BackendMCPProfile(
    "tags-only",
    tools=("backend_tags",),
    desc="Expose only effective backend Tag metadata.",
)
toolkit = backend.to_mcp(profile=tags_only)

describe_only = backend.to_mcp(profile={"name": "describe-only", "tools": ["describe_backend"]})
explicit = backend.to_mcp(tools=["describe_backend", "backend_tags"])
```

Provider authors keep related MCP functions, descriptions, prompts, and profiles in the backend method:

```python
class MyBackend(hb.ext.Backend):
    def mcp(self) -> hb.ext.BackendMCP:
        spec = super().mcp()
        spec.tool("provider_probe", self.provider_probe, desc="Inspect provider-specific state.")
        spec.profile("inspect", ("describe_backend", "backend_tags", "provider_probe"))
        return spec
```

## Workspace Toolkit

If a `HeavenBase` workspace already has backends, storage placements, entities, and data, bind the toolkit to that exact object:

```python
import heavenbase as hb

workspace = hb.HeavenBase("commerce", backends={"main": {"type": "inmem"}})
toolkit = workspace.to_mcp(name="commerce-mcp")
print(workspace.to_mcp_json(name="commerce-mcp", transport="http", host="127.0.0.1", port=7001))
```

Use the smaller agent profile when an agent should start with inspect, read, single-row write, count, explain, and optional skill linkage:

```python
ws.enable_extension("agent")
agent_toolkit = workspace.to_mcp(name="commerce-agent-mcp", profile="agent")
print(agent_toolkit.list_tools())
```

The built-in `agent` profile sets `skills: "*"` so `list_skills` and `read_skill` appear once the optional `agent` extension is enabled. Profiles without a `skills` scope omit those tools.

Pass `tools=[...]` when a specific agent should see an even narrower allowlist.

## Workspace Profiles

Workspace MCP exposure is configured through declarative `ProfileSpec` objects instead of bare tool-name lists. `ProfileSpec` is the value contract in `src/heavenbase/toolkit/profile.py`; Registry and resolution behavior lives in `src/heavenbase/toolkit/profiles.py`. Register profiles in code with `hb.ext.register_profile(...)` or define them under `heavenbase.mcp.profiles.<name>` in config. Backend MCP profiles are backend-local `BackendMCPProfile` selections as shown above; they do not use entity scopes or `sys-metaschema` overlays. Built-in workspace profiles:

| Profile | Tools | Serializer | Owner |
| --- | --- | --- | --- |
| `full` | tool groups `schema`, `read`, `write`, `delete`, `query` | `jstr` | base Toolkit extension |
| `agent` | curated nine-tool surface plus optional `list_skills` / `read_skill` when `agent` extension + `skills` scope | `markdown` | system profile plus `agent` extension skill tools |
| `memory` | remember/recall/search/list/set | `jstr` | `memory` extension |
| `memstate` | memstate_* verbs | `jstr` | `memory` extension |
| `database` | scoped list/describe/read/write/query plus `run_sql`, `run_snippet`, `save_snippet`, `suggest_sql` | `jstr` | `database` extension |

`ProfileSpec` fields:

- `name`: profile id referenced by `to_mcp(profile=...)`.
- `extends`: parent profile names merged before this profile's overrides.
- `tools`: tool names and/or tool-group ids (`schema`, `read`, `write`, `delete`, `query`).
- `entities`: `"*"` or an explicit entity-id allowlist that filters `list_entities`, `describe_entity`, and data tools. When a scoped profile omits `entity` on object-id tools, HeavenBase only infers matches from the allowed entity ids.
- `skills`: `"*"`, an explicit skill-name allowlist, or empty. When non-empty and the `agent` extension is enabled, the workspace toolkit adds `list_skills` and `read_skill`. The built-in `workspace-guide` skill is generated on demand and always readable through `read_skill`.
- `serializer`: registered serializer name (`jstr` default, `markdown` for table-shaped reads).
- `desc`: optional human-readable summary.

`profiles.py` owns profile policy: registry lookup, config-backed profiles, inheritance, serializer
validation, entity scope, skill scope, and workspace-local overlays. `profile_tools.py` owns the
accepted concrete tool names and built-in groups (`schema`, `read`, `write`, `delete`, `query`).
Toolkit family registration publishes contributed tool names to `profile_tools.py`; it does not
change profile resolution. Add a new `ProfileSpec` only when the new tools need a named access
policy, not merely because a family exists.

Example config profile composing the agent surface for one entity with markdown reads and skill linkage:

```yaml
heavenbase:
  mcp:
    profiles:
      product-agent:
        extends: [agent]
        entities: [product]
        skills: "*"
        serializer: markdown
```

Bare YAML lists such as `profiles: { agent: [query, get] }` are rejected. Errors point at the ProfileSpec shape above.

Extension authors register additional profiles the same way the built-in `memory` extension registers `memory` and `memstate`:

```python
import heavenbase.ext as ext

ext.register_profile(
    ext.ProfileSpec(
        name="readonly-catalog",
        extends=("agent",),
        tools=("read", "query"),
        entities=("sys-catalog", "sys-metaschema"),
        serializer="markdown",
    )
)
```

`to_mcp(profile=..., tools=[...])` keeps its public signature. When `tools` is passed, it overrides profile tool selection while the profile still supplies serializer and entity scope defaults.

The `database` profile and its Toolkit family carry exact `workspace_requires`
predicates. They become eligible only after both `database` and `toolkit` are
active; profile resolution never enables a missing Extension. Call
`ws.enable_extension("database")` before selecting the profile or accessing
`db-*` entities.

During workspace construction, HeavenBase mirrors MCP registry state into `sys-metaschema` for agent discovery. When `ws.to_mcp(...)` builds a workspace toolkit, it first reloads active workspace-owned `mcp_profile`, `mcp_serializer`, and safe `toolkit_family` rows from `sys-metaschema` before resolving the profile:

| Kind | Example row id | Payload |
| --- | --- | --- |
| `mcp_profile` | `mcp_profile:agent` | raw and resolved profile tools, entity scope, skill scope, serializer, and source (`registry`, `config`, or `workspace`) |
| `toolkit_family` | `toolkit_family:workspace` | family tool names, workspace-bound builder reference, description, and `runtime_only` marker |
| `mcp_serializer` | `mcp_serializer:markdown` | serializer callable reference, description, source, and reload/runtime marker |

Workspace-owned `mcp_profile` rows can define ordinary `ProfileSpec` fields (`extends`, `tools`, `entities`, `skills`, `serializer`, `desc`, and `meta`) and override process/config profiles for that toolkit build without registering globally. Workspace-owned `mcp_serializer` rows can define serializer aliases whose `target`, `meta.serializer`, `meta.alias_of`, or `meta.callable` points to an already registered serializer name or callable reference; HeavenBase resolves those aliases locally for the toolkit build without importing arbitrary row-defined callables or mutating process registries. Workspace-owned `toolkit_family` rows support two safe forms: local aliases whose `target`, `meta.family`, `meta.alias_of`, or `meta.builder` points to an already registered family name or builder callable reference, and metadata-only families with no builder target that group existing workspace tools by `meta.tool_names` for profile selection and descriptions. Aliases may narrow `meta.tool_names` to tools provided by their target family; metadata-only families may only name already registered/core workspace tools. Neither form can replace registered families or import arbitrary builders.

The MCP registry reload path caches decoded `sys-metaschema` rows on the workspace so profile, serializer, and toolkit-family loaders share one backend read and repeated toolkit builds do not re-scan unchanged metadata. Direct MetaSchema mutations through workspace CRUD and catalog metadata writes invalidate that cache with a version stamp. MCP registry mirror refreshes do not invalidate the cache because they write process/config discovery rows; workspace-owned rows remain the reload source for local overrides.

## Serializers

Profiles name a registered serializer through `ProfileSpec.serializer`. Built-ins:

| Name | Behavior |
| --- | --- |
| `jstr` | compact JSON via `dumps_jstr` (default for `full`, memory profiles) |
| `markdown` | query/get/list frame output as capped markdown tables (`heavenbase.mcp.display.max_rows`, default 50); other payloads fall back to `jstr` |

Register custom serializers with `hb.ext.ToolSerializer(name, fn).register()`.
The callable contract is `fn(output, *, config) -> str`; HeavenBase injects the
owning workspace or Backend's exact config authority. Standalone descriptor calls
use `CM_HVNB`. Workspace MCP toolkits assign the resolved profile serializer to
every exposed tool, so `toolkit.run_to_str("query", ...)` under `profile="agent"`
returns markdown tables while `profile="full"` stays byte-for-byte with today's
JSON output for the same raw `run(...)` result.

```python
def compact_names(output, *, config):
    limit = int(config.get("acme.mcp.name_limit", default=20))
    return ", ".join(str(value) for value in output[:limit])


hb.ext.ToolSerializer("compact-names", compact_names).register()
```

```python
import heavenbase as hb

ws = hb.HeavenBase("demo", backends={"main": {"type": "inmem"}})
agent = ws.to_mcp(profile="agent")
full = ws.to_mcp(profile="full")
# agent.run_to_str("query", ...) -> markdown table
# full.run_to_str("query", ...) -> compact JSON
```

The programmatic server path does not require persisting the toolkit first:

```python
workspace.serve(name="commerce-mcp", transport="http", host="127.0.0.1", port=7001)
```

`serve(...)` and the `hb mcp` commands are blocking server entry points.
Embedded applications should obtain `toolkit.to_fastmcp()` and compose the
returned FastMCP server through FastMCP's async runner or ASGI application
instead of asking HeavenBase to start a daemon thread.

## Workspace Skills

Any live registered workspace, including the configured active/default
workspace, can export one selected MCP profile as an installable agent skill.
The generator is profile-neutral: extension-owned profile metadata contributes
domain guidance, while the common artifact owns discovery, safe read/write
rules, the declared streamable-HTTP dependency, and a fallback client.

```python
import heavenbase as hb

ws = hb.HeavenBase.load("commerce")
skill = ws.to_skill(profile="agent", port=7001)
print(skill.to_dict())
installed = skill.install()  # $CODEX_HOME/skills or ~/.codex/skills

# Start the exact workspace/profile endpoint in a separate process.
ws.serve(profile="agent", transport="http", host="127.0.0.1", port=7001)
```

The CLI provides the same two explicit operations. Omitting `WS_ID` resolves
the active workspace and then the configured default; it never creates one.

```bash
hb ws skill commerce --profile agent --port 7001
hb mcp workspace commerce --profile agent --transport http --host 127.0.0.1 --port 7001
```

`hb ws skill` installs by default. Use `--output PATH` for an exact export
folder, `--root PATH` for another skill root, `--name NAME` for a different
skill id, `--url https://host/mcp` for an already-served endpoint, and `--force`
to replace only the exact target folder. `WorkspaceSkill.save(...)` and
`.install(...)` perform the equivalent atomic writes.

The artifact includes `SKILL.md`, `agents/openai.yaml`, and
`scripts/client.py`. It records only the workspace id, selected profile,
credential-free public MCP URL, exposed tool names, entity scope, and
profile-owned instructions. It never serializes workspace construction,
backend configuration, or source credentials; URLs containing user info are
rejected. The server remains a separately managed blocking process, and its
workspace owns every read and write. The fallback client requires a compatible
HeavenBase runtime and is only for hosts that cannot dynamically attach the
declared MCP dependency.

The Database profile uses this generic contract to teach schema/rule/snippet
discovery, guarded live SQL, explicit context edits, and save-after-success SQL
memory. No Database branch exists in `WorkspaceSkill`.

Registered Capsule Toolkits can also be served from the CLI:

```bash
hb mcp list
hb mcp tools demo/math-tools:1
hb mcp serve demo/math-tools:1 --transport http --host 127.0.0.1 --port 7001
hb mcp stdio demo/math-tools:1
```

## Native vs serialized output

Every Tool owns two Capsules: one for execution and one for serialization. Workspace MCP toolkits inherit serializer choice from the active profile (`jstr` for `full`, `markdown` for `agent`). Use the path that matches your caller:

| Caller | Method | Returns |
| --- | --- | --- |
| Local Python | `toolkit.run("query", ...)` | Native Python value (for workspace tools, usually a `dict`) |
| Direct agent or LLM adapter | `toolkit.run_to_str("query", ...)` | Serialized `str` through the Tool's serializer Capsule |
| FastMCP client | `client.call_tool(...)` | Native JSON-compatible `data`/`structured_content` plus serializer-produced text `content` |

The default serializer is ``None`` on each Tool and resolves to the shared `jstr_serializer` Capsule at execution time via :func:`dumps_jstr` when no profile serializer is set. Workspace MCP toolkits set the profile serializer at build time. Pass a custom serializer while adding a tool or call `toolkit.set_serializer(tool_name, serializer)` later:

```python
def exec_sql() -> list[dict[str, object]]:
    return [{"name": "Desk", "stock": 7}]


def rows_markdown(rows: list[dict[str, object]]) -> str:
    lines = ["| name | stock |", "| --- | --- |"]
    lines.extend(f"| {row['name']} | {row['stock']} |" for row in rows)
    return "\n".join(lines)


toolkit = hb.Toolkit("tables", namespace="demo")
toolkit.add(exec_sql, name="exec_sql", serializer=rows_markdown)

rows = toolkit.run("exec_sql")  # raw Python list
text = toolkit.run_to_str("exec_sql")  # markdown table for agents
```

Workspace and backend MCP toolkits still return native values from `run(...)`.
FastMCP serving does not replace that value with serializer text. It returns
both channels: structured output is the machine-readable result, while text
content is the serializer's LLM presentation. `LLM.chat(..., tools=...)`
continues to use the text channel, defaulting to `dumps_jstr`.

MCP output schemas must have an object root. HeavenBase keeps each Tool's
native schema unchanged and wraps scalar, array, and null schemas under a
protocol-only `result` property when exporting to FastMCP. The client import
path removes that wrapper, so `Toolkit.from_fastmcp(...)` retains the native
Tool output schema. Explicit Tool input schemas, output schemas, metadata, and
serializer references also survive Toolkit registry persistence. A Python
value that cannot be represented as MCP structured data is exposed as
serializer text only; local `run(...)` still returns the original object.

`LLM.chat` and `LLMSession.send` always use each Tool's serializer. There is no separate LLM-level tool-result serializer.

Tool-call failures are intentionally separate from Tool success serializers. Unknown tools, sync/async boundary mistakes, and exceptions raised before a raw result exists use a stable JSON error envelope such as `{"ok": false, "error": ...}`. Custom Tool serializers format successful raw outputs only.

`Toolkit.from_fastmcp(...)` imports remote MCP tools as runtime-only proxy tools, not as original remote Python functions. For those proxies, `run(...)` returns the FastMCP client payload: `data` when FastMCP provides it, otherwise `structured_content`, then text content. `run_to_str(...)` applies the local proxy Tool serializer to that payload.

## Lifespan, caching, and transforms

Every `toolkit.to_fastmcp()` server owns a HeavenBase lifespan. The lifespan
opens server-scoped resources once and clears them at shutdown. It does not
make connection state, client ids, or FastMCP context into a workspace
authority.

Native-result caching is available but deliberately disabled by default. It is
an in-memory, lifespan-scoped, TTL-bounded LRU and requires both `enabled: true`
and an explicit tool allowlist. Cache only deterministic reads whose result is
safe to reuse for the configured TTL; do not select writes, deletes, mutable
workflow steps, credential-sensitive calls, or reads whose authorization can
change between calls.

```yaml
heavenbase:
  mcp:
    cache:
      enabled: true
      tools: [list_entities, describe_entity]
      ttl: 60
      max_entries: 128
```

`tools: "*"` is supported but is intentionally explicit. Arguments form the
cache key; allowlist entries use the original Toolkit names before any exposure
transform. Native values are copied on insertion and retrieval, serializers
run on every response, cache hits remain visible to call logging, and all
entries are discarded when the server lifespan ends.

FastMCP transforms are useful for exposure-time naming and component views, but
they do not become a second Toolkit registry. Pass live transform objects
programmatically:

```python
from fastmcp.server.transforms import Namespace

server = toolkit.to_fastmcp(transforms=[Namespace("public")])
```

Persistent configuration remains JSON-only and therefore names Registry
builders. HeavenBase ships the `mcp_transform:namespace` builder:

```yaml
heavenbase:
  mcp:
    transforms:
      - name: namespace
        args:
          prefix: public
```

External transform builders use the same `kind: mcp_transform` module-record
contract as the built-in. This preserves installed/external parity and lets
configuration resolve live FastMCP objects without serializing them.

FastMCP Providers are not adopted yet. A Provider can own dynamic tools,
resources, and prompts, while HeavenBase's current authority is an ordered,
persistable Tool-only Toolkit with profiles and serializers. Introducing a
Provider now would either bypass those controls or create a parallel component
registry. Revisit an `mcp_provider` Registry seam only when HeavenBase owns a
real multi-component or dynamic-discovery requirement.

## Tool annotations

MCP defines advisory `ToolAnnotations` for client presentation and risk hints:
`title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`, and
`openWorldHint`. They are not an authorization or enforcement mechanism, and
clients must treat annotations from untrusted servers as untrusted hints.

HeavenBase stores these protocol fields separately from arbitrary Tool
`metadata`. Pass the official wire names through `Tool.annotations` or the
`annotations=` convenience argument on `Toolkit.add(...)` and
`Toolkit.tool(...)`:

```python
toolkit = hb.Toolkit("catalog", namespace="demo")
toolkit.add(
    lookup_product,
    annotations={
        "title": "Look up product",
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
```

Annotations survive Toolkit Registry persistence and FastMCP export/import.
Backend MCP tools also project their existing `read_only` contract to
`readOnlyHint`. Missing hints keep MCP's conservative defaults: non-read-only,
potentially destructive, non-idempotent, and open-world.

## Tools

The generated workspace toolkit has sixteen tools:

- `define_entity(entity, replace=False)` registers a new JSON entity definition through `workspace.register`, so the definition may state `catalog` and `gram` as a `register` definition does. Pass an entity object such as `{"name": "Product", "fields": {"object_id": {"type": "identifier"}, "name": {"type": "short-text"}}}`. Common JSON-Schema scalar aliases are accepted for agent ergonomics: `string` → `short-text`, `number` → `float`, `object` → `json`; `object_id` with `string` becomes `identifier`. After definition, entity arguments accept either the canonical entity id or the class-style name, for example `product` or `Product`. It does not replace an existing entity definition unless `replace=True`; an enabled extension's own exported definition is accepted unchanged, as `register` accepts it.
- `list_entities(include_system=False)` lists registered entities.
- `describe_entity(entity)` returns the schema, derives, resolved storage plan, query-compute capabilities, and
  per-field `filter_ops` for an entity type. Each `filter_ops` list is intersected with the field's resolved
  backend/strategy routes, so unsupported logical types and unroutable operations are not advertised. An
  operation the entity's registration options rule out is not advertised either: with `gram=False`,
  `contained_in` on an `Array[ShortText]` field has no keyword index, and the query refuses it.
- `upsert(entity, row, overwrite=True)` inserts or replaces one row.
- `upsert_many(entity, rows, overwrite=True)` inserts or replaces rows for one entity type and returns object IDs in input order.
- `get(object_id, entity=None)` fetches one row by object ID.
- `get_many(object_ids, entity=None)` fetches rows by object IDs in input order.
- `set(row, entity=None, overwrite=True)` patches one row.
- `set_many(rows, entity=None, overwrite=True)` patches rows and returns IDs in input order.
- `delete(target, edges="schema")` deletes one target and returns whether it was deleted. `edges` accepts
  `schema`, `restrict`, or `detach` with the same strong GraphEdge policy as `ws.delete(...)`.
- `delete_many(targets, edges="schema")` applies that policy to one logical batch and returns a boolean
  list aligned with input order.
- `exists(object_id, entity=None)` returns whether one row exists.
- `exists_many(object_ids, entity=None)` returns a boolean list aligned with input order.
- `count(entity)` returns the row count for one entity type.
- `query(entity, spec=None)` runs the Mongo-style JSON query surface for an entity type.
- `explain(entity, spec=None)` reports storage placement, backend grouping, handler, and pushdown/fallback decisions for an entity type.

For an entity-scoped profile, deletion authorization covers the complete logical mutation closure, not
only the explicit target. If detach would rewrite a surviving source outside the profile, the whole call
fails before mutation with a generic scope error that does not disclose the hidden reference. Invalid
`edges` modes are rejected consistently even when every target is absent or filtered by scope.
Non-empty GraphEdge writes also require the endpoint entity type to be inside the profile scope. The
write is rejected with the same kind of generic scope error before endpoint existence is checked, so an
allowed source cannot be used to probe object IDs in a hidden target entity.

There is intentionally no MCP `alter_entity` or `undefine_entity` tool yet. Entity definition changes and removals need explicit workspace-level data policy for MetaSchema updates, Catalog updates, storage placement changes, physical backend cleanup, and recovery before agents can safely perform them.

`define_entity` is the consistency-guaranteeing MCP path and delegates to
`workspace.register`. A generic write to `sys-metaschema` can also carry an
active Entity-kind definition, but it establishes a new class only when both
`sys-metaschema` and the declared entity id are inside the active profile's
entity scope (or the profile uses `"*"`). Stored manual metadata never replaces
an existing class or acts as a later resolver.

Use the single-row tools for one row and the `*_many` tools when ordered bulk results matter.

## Toolkit Families

Extension-owned workspace MCP tools register through one declarative pair: `hb.ext.WorkspaceToolkitFamilySpec` and `hb.ext.register_workspace_toolkit_family(...)`. `WorkspaceToolkitFamilySpec` lives in `toolkit/families/family.py` as the value contract: stable name, concrete tool names, workspace-bound builder, and optional metadata used by Toolkit descriptions and profile-aware builders. `toolkit/families/registry.py` owns registration, discovery snapshots, and profile tool-name visibility.

The base workspace family uses `toolkit/families/workspace/` as the reference layout: shared workspace/profile context in `context.py`, generated workspace-guide text in `guide.py`, group assembly in `factory.py`, and one tool module per callable under `tools/`. Optional Extension contributions follow the same shape inside their self-contained artifact folders, for example `extensions/agent/toolkit/`, `extensions/memory/toolkit/`, and `extensions/database/toolkit/`.

```python
def demo_tools(ws):
    def ping() -> dict:
        return {"workspace": ws.id, "ok": True}

    return {"demo_ping": ping}


hb.ext.register_workspace_toolkit_family(
    hb.ext.WorkspaceToolkitFamilySpec(
        name="demo",
        tool_names=("demo_ping",),
        builder=demo_tools,
        desc="Demo MCP tools for one workspace.",
    ),
    replace=True,
)
```

Profiles can include family tools by name after registration. Each family is an
ordinary `toolkit_family` record in the Context-owned module Registry;
programmatic publication stores a durable executable locator through the same
ModuleService used by artifact manifests. MetaSchema projects those registered
definitions directly. Workspace-owned rows may safely alias an eligible
registered family for one toolkit build, but they cannot publish or import an
arbitrary row-defined builder.

By default, family builders receive the live workspace object. Families that need active profile context can set `meta={"builder_context": "workspace_tool_context"}` on `WorkspaceToolkitFamilySpec`; the built-in `agent` family uses that mode for skill tools so `list_skills` and `read_skill` can enforce the active profile `skills` scope and render the live workspace guide.

`WorkspaceToolContext` is exported from `heavenbase.toolkit` for annotations.
Artifact builders that support an explicit non-default `Context` should keep
that dynamic facade import behind `TYPE_CHECKING`; the runtime context object is
supplied by family assembly. A builder may return shared helper functions, but
only selected names declared by its own `tool_names` are projected into the
Toolkit. Unselected builders do not run. Missing selected names fail assembly,
and duplicate selected declarations report both contributing family owners.
Workspace-owned aliases and metadata-only families group existing tools; they
do not become a second callable provider.

External tool adapters can import
`heavenbase.toolkit.errors.raise_tool_error`. After catching an implementation
exception, call that helper to raise one redacted public `RuntimeError` without
an implementation exception chain. Successful outputs still use the Tool
serializer; error sanitization is a separate boundary.

## Minimal Agent Profile

For a first agent pass, enable the optional `agent` extension and start with `profile="agent"` instead of the full 16-tool menu:

| Intent | Tool |
| --- | --- |
| Live workspace guidance | `read_skill("workspace-guide")` when profile `skills` is set |
| Browse persisted skills | `list_skills`, then `read_skill(skill_name=...)` |
| Find the available schema surface | `list_entities`, then `describe_entity` |
| Inspect routes and field metadata | `query` on `sys-metaschema` |
| Discover concrete objects | `query` on `sys-catalog` |
| Read data | `query` for filtered reads, `get` for known IDs |
| Write one row | `upsert` for create/replace, `set` for patch |
| Count rows | `count` |
| Explain trust/performance | `explain` |

The `agent` profile exposes `define_entity`, `list_entities`, `describe_entity`, `upsert`, `get`, `set`, `count`, `query`, and `explain`, uses the `markdown` serializer for agent-readable query output, and sets `skills: "*"` so `list_skills` and `read_skill` appear once `ws.enable_extension("agent")` has run. Bulk tools, existence tools, and delete tools stay in the default `full` profile. This follows the same product lesson as narrow memory MCPs such as [Memstate MCP](https://memstate.ai/docs/mcp): a small set of obvious verbs beats a large undifferentiated tool list for first-run agents.

### Skill linkage

When a profile declares a non-empty `skills` scope and the workspace has the `agent` extension enabled:

- `list_skills()` returns the generated `workspace-guide` entry plus persisted `agent-skill` rows in scope.
- `read_skill(skill_name="workspace-guide")` returns a generated SKILL.md covering registered entities, the active profile, Mongo-style query shape, and `object_id` semantics for the live workspace.
- `read_skill(skill_name=<name>)` reads archived skill files through the same path as `hb.Skill.toolkit(...)`.

Skill tools are omitted when the `agent` extension is disabled, when `skills` is empty, or when an explicit `tools=[...]` allowlist overrides profile tool selection without naming them.

## Memory Profile

Use `profile="memory"` when the agent only needs a small persistent note surface and should not see the generic schema/query tools on its first turn:

```python
workspace.enable_extension("memory")
memory = workspace.memory.to_mcp(name="agent-memory", profile="memory")
memory.run("remember", key="repo-context", text="Use bash scripts/test.bash for HeavenBase tests.", tags=["setup"])
memory.run("search_memory", text="tests")
memory.run("recall", key="repo-context")
```

The simple memory profile exposes five tools:

- `remember(text, key=None, title="", tags=None, metadata=None, source=None)` stores one memory note. If no key is supplied, HeavenBase creates one.
- `recall(key)` fetches one memory note by key.
- `search_memory(text, limit=None)` searches memory note text.
- `list_memory(limit=None)` lists memory notes.
- `set_memory(key, text=None, title=None, tags=None, metadata=None, source=None)` patches one memory note.

Compared with Memstate-style MCPs, map the verbs this way: `remember` stores summaries or decisions, `recall`/`list_memory` cover exact retrieval and browsing, `search_memory` covers fuzzy lookup, and `set_memory` covers exact-key correction. HeavenBase keeps the first-run profile smaller than hosted memory servers that also expose history or delete tools, because those operations need a retention policy before an agent should see them.

`remember` is the default for task summaries, notes, and other freeform memory. Use `set_memory` only when the agent already knows the exact key and wants to replace or patch that one note. Compared with hosted memory MCPs that add version-history or deletion verbs, HeavenBase keeps the first-run profile smaller and stores every note as a normal typed row with `tags`, `metadata`, `source`, and timestamps.

Enabling the optional `memory` extension creates its registered `memory-note` entity and attaches `workspace.memory`. Runtime defaults live under `heavenbase.mcp.memory`: `default_source` defaults to `agent`, `list_limit` to `20`, and `search_limit` to `10`.

Use `profile="memstate"` when an agent needs Memstate-style project keypaths, current-state browsing, conflict visibility, history, and soft deletes while keeping data inside the same HeavenBase workspace:

```python
workspace.enable_extension("memory")
memstate = workspace.memory.to_mcp(name="agent-memstate", profile="memstate")
memstate.run("memstate_get", project_id="heavenbase-demo")
memstate.run("memstate_remember", project_id="heavenbase-demo", content="## Harness\nUse host-side BaseAgent logging.")
memstate.run("memstate_history", project_id="heavenbase-demo", keypath="harness")
```

The `memstate` profile exposes `memstate_remember`, `memstate_set`, `memstate_get`, `memstate_search`, `memstate_history`, and `memstate_delete`. It stores versioned rows in the same configured memory entity and uses `project.<project_id>.<keypath>` keypaths. Use the generic `agent` or `full` profile when you need custom schemas, Catalog/MetaSchema inspection, structured queries, bulk operations, or non-memory deletes.

## LLMSession Integration

Use `LLMSession.add_mcp(...)` when a Python agent should combine a HeavenBase workspace MCP with another MCP server:

```python
import heavenbase as hb

ws = hb.HeavenBase("agent-memory", preset="debug")
ws.enable_extension("agent")
session = hb.LLMSession(hb.LLM(preset="chat", cache=False))

session.add_mcp(ws.to_mcp(profile="agent").to_fastmcp(), name="agent-memory-client")
session.add_mcp("http://127.0.0.1:7001/mcp", name="external-tools")

print(session.list_tools())
session.send("Use memory and the external tool.", max_tool_turns=8)
```

`add_mcp(...)` imports the server as a runtime-only `Toolkit`. It accepts a URL, transport, FastMCP server object, or anything accepted by `fastmcp.Client`. Session setup raises a duplicate-name error if the external MCP exports a tool name already exposed by the workspace profile; rename or narrow one of the tool surfaces before handing it to the agent.

## Agent Workflow

Agents should inspect the workspace surface before touching user data:

```python
toolkit.run("list_entities")
toolkit.run("describe_entity", entity="note")
```

For richer entity and storage placement metadata, query MetaSchema (`sys-metaschema`):

```python
toolkit.run(
    "query",
    entity="sys-metaschema",
    spec={"filter": {"kind": "field"}, "select": ["subject_id", "field", "dtype", "desc", "backend", "strategy"]},
)
```

For concrete object discovery, query Catalog (`sys-catalog`), then hydrate with `get`. Set `available` so inactive, hidden, and expired rows stay out of ordinary discovery; use `stale: true` or `invisible: true` when those rows should remain visible:

```python
catalog = toolkit.run(
    "query",
    entity="sys-catalog",
    spec={
        "available": True,
        "filter": {"target_entity": "note"},
        "select": ["target_id", "target_entity", "name", "desc", "tags"],
    },
)
row = toolkit.run("get", object_id=catalog["rows"][0]["target_id"], entity=catalog["rows"][0]["target_entity"])
```

Python ORM equivalent:

```python
rows = (
    ws.query(hb.Catalog)
    .available()
    .where(hb.Catalog.target_entity == "note")
    .execute()
    .rows()
)
```

Mongo-style filters can also embed `$available`:

```json
{"filter": {"$and": [{"target_entity": "note"}, {"$available": {"stale": false}}]}}
```

Use exact logical type names such as `Identifier`, `ShortText`, `LongText`, `Integer`, `Float`, `Boolean`, `Json`, `Array`, and `Vector`. Use `query` for reads. Use `upsert`, `set`, and `delete` for one-row mutations, and `upsert_many`, `set_many`, or `delete_many` for ordered bulk mutations. Use `explain` whenever the result depends on storage placement, backend pushdown, or scan fallback.

## Demos

Serve a registered toolkit from the CLI:

```bash
hb mcp serve demo/math-tools:1 --transport http --host 127.0.0.1 --port 7001
```

## Verification

The focused MCP regression suite is:

```bash
bash scripts/test.bash tests/interfaces/test_mcp.py -q
```

Relevant coverage:

- Workspace/backend MCP toolkits do not import removed facade toolkit packages.
- Workspace-bound Toolkits are runtime-only and cannot be persisted as durable Capsules.
- FastMCP in-process and stdio roundtrips can list and call tools.
- Tool input schemas include annotated parameters and parsed docstring descriptions.
- Generic workspace skills validate their selected profile, remain credential
  free, install atomically, and expose the same designated server through SDK
  and parser-neutral CLI paths.
