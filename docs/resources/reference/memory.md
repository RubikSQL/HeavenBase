# Memory Extension

The optional `memory` extension stores agent-facing memory notes as normal HeavenBase entities. It powers the MCP `memory` and `memstate` profiles without requiring users to define an entity schema themselves.

Enable it explicitly when you want to query or write memory rows from Python:

```python
import heavenbase as hb

ws = hb.HeavenBase("agent-memory", preset="debug")
ws.enable_extension("memory")
```

The extension registers one entity:

- `hb.MemoryNote` (`memory-note`): versioned text notes with optional tags, metadata, and memstate-style `project_id` / `keypath` fields.

## Configuration

Memory behavior is tuned through `CM_HVNB` keys:

| Key | Default | Purpose |
|-----|---------|---------|
| `heavenbase.mcp.memory.default_source` | `agent` | Default `source` field for new rows |
| `heavenbase.mcp.memory.list_limit` | `20` | Default cap for `list_memory` / `memstate_get` |
| `heavenbase.mcp.memory.search_limit` | `10` | Default cap for `search_memory` / `memstate_search` |

Every capped tool states its own coverage, so a caller never mistakes a page for the whole set. `list_memory`, `search_memory`, `memstate_get`, and `memstate_search` publish `limit` alongside their rows, and add `total` and `truncated` when the cap withheld entries — `total` is the exact number that matched, not a floor. Their tool descriptions say the same, because that is the text an agent reads. Neither list tool ranks: `list_memory` does not return the newest notes and `memstate_search` orders by keypath rather than relevance.

The entity identity is part of the module contract and is therefore not a runtime setting. Install a distinct entity/module definition when a different identity is required.

## MCP Profiles

One default-disabled `memory` extension owns two MCP profiles on top of the same entity:

- `memory`: flat key/value notes (`remember`, `recall`, `search_memory`, `list_memory`, `set_memory`).
- `memstate`: project-scoped version chains (`memstate_remember`, `memstate_set`, `memstate_get`, `memstate_search`, `memstate_history`, `memstate_delete`).

Expose them through the extension namespace after enablement:

```python
ws.enable_extension("memory")

memory = ws.memory.to_mcp(name="agent-memory", profile="memory")
memstate = ws.memory.to_mcp(name="agent-memstate", profile="memstate")
```

Use `memory` for ordinary task summaries, decisions, and notes. Use `memstate` when an agent needs project-scoped keypaths, current-state browsing, conflict visibility, history, or soft delete. Both profiles store rows in the same registered memory entity, so one extension is enough.

## Version Chains

Memstate tools append a new row per keypath change instead of overwriting history. Each version stores `version`, `previous_id`, and optional `deleted` tombstones. Helpers in `heavenbase.extensions.memory.versions` resolve the latest row per keypath for reads and searches.

## Workspace Manifests

Include `memory` in manifest `extensions` to replay enablement on import:

```yaml
extensions:
  - memory
```

Extension-owned entities are reconstructed when the extension enables; you do not need a separate entity definition in the manifest.
