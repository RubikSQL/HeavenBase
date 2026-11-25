---
name: heavenbase-mcp
description: "Use when: working with a HeavenBase MCP toolkit, querying HeavenBase through agent tools, inspecting sys-metaschema or sys-catalog, choosing logical types, defining entities, or mutating HeavenBase rows."
---

# HeavenBase MCP

HeavenBase is a semantic data workspace: entities are defined once, routed to one or more backends, and queried through one JSON/ORM-style surface. The MCP toolkit exposes a generic toolset over one bounded backend or one existing workspace. Use `ws.to_mcp(profile="agent")` after `ws.enable_extension("agent")` for the recommended first-agent surface with skill linkage.

## First Move

Call `list_entities` first when the entity surface is unknown, then use `describe_entity` for a known entity or query `sys-metaschema` for detailed fields, types, routes, backends, strategies, and capabilities. Query `sys-catalog` to discover concrete objects by `target_entity`, `target_id`, `name`, `desc`, and `tags`; set `available=true` in query specs (or use `.available()` in Python) so inactive, hidden, and expired rows are excluded unless the user explicitly asks for them. Pass `stale=true` to include stale rows or `invisible=true` to include hidden rows. Hydrate the real row with `get(object_id=<target_id>, entity=<target_entity>)`.

When the `agent` extension is enabled and the active profile declares `skills`, call `read_skill("workspace-guide")` for live workspace guidance (entities, profile, query shape, and `object_id` semantics) before touching user data. Use `list_skills` to see persisted skills in scope.

For a backend-only toolkit, define or describe the entity definition before reading existing rows. Backend-only toolkits are bounded to one backend instance and do not automatically inherit the source workspace's entity registry.

Use the `query` tool like this:

```json
{
  "entity": "sys-metaschema",
  "spec": {
    "filter": {"kind": "field"},
    "select": ["subject_id", "field", "dtype", "desc", "backend", "strategy"]
  }
}
```

For concrete object discovery:

```json
{
  "entity": "sys-catalog",
  "spec": {
    "available": true,
    "filter": {"target_entity": "note"},
    "select": ["target_id", "target_entity", "name", "desc", "tags"],
    "limit": 10
  }
}
```

Use `"available": {"stale": true, "invisible": true}` to include stale or hidden rows. Mongo-style filters may embed `"$available": true` instead of the top-level key.

## Tool Choice

Recommended first-run profile (`profile="agent"` with `ws.enable_extension("agent")`):

- Start with `read_skill("workspace-guide")` when skill tools are exposed.
- Inspect schema with `list_entities` and `describe_entity`.
- Inspect metadata with `query` on `sys-metaschema`.
- Discover objects with `query` on `sys-catalog`.
- Read rows with `query` or `get`.
- Write one row with `upsert` or `set`.
- Use `count` for row counts and `explain` before trusting route/performance-sensitive answers.
- Use `list_skills` / `read_skill` for persisted skill folders in profile scope.

Use the default `full` profile only when the task specifically needs ordered bulk mutation, existence checks, or deletion.

- Use `profile="memory"` when the task is simple persistent memory. It exposes only `remember`, `recall`, `search_memory`, `list_memory`, and `set_memory`, and it auto-creates the configured memory entity on first use.
- Use `query` for reads.
- Use `explain` before trusting backend routing, pushdown, or fallback behavior.
- Use `describe_entity(entity)` when you already know an entity type and need its definition and route plan.
- Use `upsert(entity, row, overwrite=true)` for one insert/replacement.
- Use `upsert_many(entity, rows, overwrite=true)` for multiple inserts/replacements for one entity type.
- Use `get(object_id, entity=null)` to fetch one row by identity.
- Use `get_many(object_ids, entity=null)` to fetch by row identity.
- Use `set(row, entity=null, overwrite=true)` to patch one row.
- Use `set_many(rows, entity=null, overwrite=true)` to patch multiple rows and preserve input order.
- Under an entity-scoped profile, a non-empty `GraphEdge` write may target only another entity in scope;
  out-of-scope target types are rejected before endpoint lookup so hidden object IDs cannot be probed.
- Use `exists(object_id, entity=null)` before mutating one row when the user asks for an existence check.
- Use `exists_many(object_ids, entity=null)` before mutating when the user asks for existence checks.
- Use `count(entity)` for direct entity row counts; use `query` when filters are needed.
- Use `delete(target, edges="schema")` to remove one row. `schema` uses each incoming `GraphEdge.on_delete`; `restrict` forces rejection when surviving references exist; `detach` removes those relationship values without deleting neighbouring rows.
- Use `delete_many(targets, edges="schema")` to apply the same GraphEdge policy to one logical batch. A target can be a raw object ID when unique, `[entity, object_id]`, or `{ "entity": "entity-id", "object_id": "row-id" }`. Under an entity-scoped profile, every surviving source row that detach would rewrite must also be in scope.
- Use `define_entity` to create a new entity from a JSON definition.

There is no current MCP `alter_entity` tool. Do not invent one; ask the user to change entity definitions through HeavenBase code when an existing entity must be altered.

## Memory Profile

For a Memstate-like key/value memory workflow, prefer the memory profile over the generic agent profile:

```python
memory = ws.to_mcp(profile="memory")
memory.run("remember", key="task-notes", text="Run tests with conda run -n hvnb.", tags=["setup"])
memory.run("search_memory", text="tests")
memory.run("recall", key="task-notes")
```

Use `remember` to store a note, `recall` for exact keys, `search_memory` for text search, `list_memory` for browsing, and `set_memory` for patches. Switch back to `profile="agent"` when the agent needs custom entities, Catalog/MetaSchema discovery, or structured queries.

Memstate-style tools commonly separate remember/search/get/set/history/delete. In HeavenBase terms, use `recall` or `list_memory` for get/browse, `search_memory` for fuzzy lookup, `remember` for markdown summaries and decisions, and `set_memory` only when the agent already has the exact key and wants to patch that one note. The memory profile intentionally omits history and delete tools so a first-run agent sees only safe persistent-note verbs; use `profile="full"` for deletes after the application has a retention policy.

## Python Session Integration

When building a Python agent, attach HeavenBase MCP and external MCP servers once at session setup:

```python
session = hb.LLMSession(hb.LLM(preset="chat", cache=False))
ws.enable_extension("agent")
session.add_mcp(ws.to_mcp(profile="agent").to_fastmcp(), name="heavenbase-client")
session.add_mcp("http://127.0.0.1:7001/mcp", name="external-tools")
session.list_tools()
```

If `add_mcp` raises a duplicate tool name error, narrow one toolkit (`profile="agent"` or `tools=[...]`) or rename the external tool before handing the surface to the agent.

## Entity Definition Shape

```json
{
  "entity_id": "support-ticket",
  "name": "SupportTicket",
  "desc": "Customer support ticket",
  "fields": {
    "object_id": {"type": "Identifier", "pk": true},
    "summary": {"type": "LongText", "desc": "Ticket summary"},
    "status": {"type": "ShortText", "default": "open"}
  }
}
```

## Query Shape

HeavenBase JSON queries use Mongo-style operators:

```json
{
  "filter": {
    "$and": [
      {"status": "open"},
      {"summary": {"$match": "refund"}}
    ]
  },
  "select": ["summary", "status"],
  "limit": 10
}
```

Supported common operators include `$eq`, `$ne`, `$lt`, `$lte`, `$gt`, `$gte`, `$in`, `$match`, `$like`, `$ilike`, `$wildcard`, `$regex`, `$contains`, and `$array_contains`.
