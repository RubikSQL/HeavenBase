# Agent Extension

The optional `agent` extension stores agent-facing runtime history and recipes as normal HeavenBase entities. It does not replace `hb.LLM`, `LLMSession`, prompts, Toolkits, or MCP. It persists the pieces those systems already use.

Enable it explicitly on a workspace when you want to query the rows yourself:

```python
import heavenbase as hb

ws = hb.HeavenBase("agent-memory", preset="debug")
ws.enable_extension("agent")
```

The extension registers four entities:

- `hb.Message` (`agent-message`): one OpenAI-format message payload plus small query projections.
- `hb.Session` (`agent-session`): ordered message ids, aggregate usage, tool-call counts, and runtime state.
- `hb.Skill` (`agent-skill`): a deterministic archive of an Anthropic-style Skill folder plus parsed `SKILL.md` metadata.
- `hb.Agent` (`agent-agent`): a recipe that composes an LLM preset, prompt, skills, registered Toolkit refs, and runtime args.

## Logged LLM Runs

`hb llm chat` writes one `agent-session` row and ordered `agent-message` rows after the response is complete. `hb llm session` creates one persistent session when the interactive session starts, then appends each completed user/assistant/tool iteration before reading the next prompt.

Interactive history edits stay persistent. `/back`, `/regen`, `/load`, and `/clear` replace the stored `agent-message` list with the kept `LLMSession.messages` history through `Session.set_messages(...)`; replacement history resets aggregate usage unless a complete replacement usage payload is supplied.

The CLI output shape is unchanged. Inspect history through the active workspace:

```python
sessions = ws.rows(hb.Session)
messages = ws.rows(hb.Message)
payloads = hb.Session.load(sessions[0]["object_id"], ws=ws).messages()
```

The stored `Message.payload` is the canonical OpenAI-format dictionary. Projections such as `role`, `content_text`, and `tool_call_count` are for query and Catalog discovery.

## Skills

Create a Skill from a folder containing `SKILL.md`:

```python
skill = hb.Skill.from_path("skills/sql-analyst")
skill.register(ws=ws)
```

`SKILL.md` frontmatter may declare Toolkit refs with `toolkit:`:

```yaml
---
name: sql-analyst
description: Analyze SQL-backed product data.
toolkit:
  - analytics/sql-tools:1
---
```

Skill folders are stored as deterministic zip bytes under `archive`. Use the runtime read tool when an agent needs file contents:

```python
toolkit = hb.Skill.toolkit([skill], ws=ws)
text = toolkit.run("read_skill", skill_name=skill.name, path="SKILL.md")
```

## Agent Recipes

An Agent row is a recipe, not a second runtime stack:

```python
agent = hb.Agent(
    name="demo-agent",
    llm_preset="chat",
    prompt_ref="demo.agent.system",
    skill_refs=["sql-analyst"],
    toolkit_refs=["analytics/sql-tools:-1"],
    args={"max_steps": 8},
    ws=ws,
)
agent.register()

runtime = hb.Agent.load("demo-agent", ws=ws).build()
runtime.session.send("Use the available skills.", max_tool_turns=8)
```

`build()` resolves `hb.LLM`, `hb.Prompt`, `hb.Skill`, registered `hb.Toolkit` refs, and returns an `LLMSession` with tools attached. Use `agent.new_session()` when you want a persistent `hb.Session` row for a future run.

## MCP Tool-call Logging

Set `heavenbase.mcp.log_calls` to `true` when you want MCP Toolkit builds to
persist one call-log group per Toolkit and one `agent-message` row per tool
call. The current Agent extension stores that group in an `agent-session` row
with `state.source="mcp"` and `state.scope="toolkit-exposure"`, plus Toolkit
name and profile. This reuses Agent's ordered-message storage; it is not an MCP
protocol session and does not imply connection-scoped state. Logging is off by
default and requires the `agent` extension on the workspace. Arg summaries are
redacted with the same secret patterns used for MCP error messages; logging
failures never fail tool calls.

```python
import heavenbase as hb

hb.CM_HVNB.set("heavenbase.mcp.log_calls", True)
ws = hb.HeavenBase("ops", preset="debug")
ws.enable_extension("agent")
toolkit = ws.to_mcp(name="ops-mcp", profile="agent")
toolkit.run("query", entity="sys-catalog", spec={"available": True, "limit": 5})
```

Each logged tool message stores JSON in `content` with `args`, `result_size`,
`duration_ms`, `error`, and `cached`. Query the rows with normal workspace
queries — no separate aggregation API.

### Meta-analysis examples

Tool frequency by name:

```python
import heavenbase as hb

ws = hb.HeavenBase("ops", preset="debug")
ws.enable_extension("agent")
rows = ws.query(hb.Message).where({"role": "tool"}).execute().rows()
counts: dict[str, int] = {}
for row in rows:
    payload = row.get("payload") or {}
    name = str(payload.get("name") or "")
    counts[name] = counts.get(name, 0) + 1
```

Error rate across MCP tool calls:

```python
import json

import heavenbase as hb

ws = hb.HeavenBase("ops", preset="debug")
ws.enable_extension("agent")
rows = ws.query(hb.Message).where({"role": "tool"}).execute().rows()
total = len(rows)
errors = 0
for row in rows:
    payload = row.get("payload") or {}
    meta = json.loads(str(payload.get("content") or "{}"))
    if meta.get("error"):
        errors += 1
rate = (errors / total) if total else 0.0
```

Slow calls (duration above a threshold):

```python
import json

import heavenbase as hb

ws = hb.HeavenBase("ops", preset="debug")
ws.enable_extension("agent")
threshold_ms = 250.0
rows = ws.query(hb.Message).where({"role": "tool"}).execute().rows()
slow = []
for row in rows:
    payload = row.get("payload") or {}
    meta = json.loads(str(payload.get("content") or "{}"))
    duration_ms = float(meta.get("duration_ms") or 0.0)
    if duration_ms >= threshold_ms:
        slow.append({"tool": payload.get("name"), "duration_ms": duration_ms, "args": meta.get("args")})
```
