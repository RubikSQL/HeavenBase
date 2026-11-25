# Deferred LLM Session Persistence

Status: deferred target for a future LLM-related extension; the dashboard
assistant session log resolves the authority question for its own domain
(see Partial Resolution below).

This page records the accepted memory-first persistence boundary for future
LLM-related extensions. It is not an active implementation plan and does not
change current behavior.

## Current

- `hb.LLMSession` owns an in-memory LLM conversation and supports explicit JSON
  file persistence.
- The optional `agent` extension exposes `hb.Session` and `hb.Message` rows.
  Session mutation, `Session.from_llm_session()`, `Agent.new_session()`,
  `hb llm chat`, and `hb llm session` currently persist workspace rows
  implicitly.
- MCP session logging is already disabled by default and writes only after
  deliberate configuration and agent-extension enablement.

## Accepted Target

- Session construction, replay, conversion, and mutation operate in memory
  without resolving a workspace, enabling an extension, reading workspace
  configuration, or performing backend IO.
- Durable workspace persistence is an explicit lifecycle operation. Its
  implementation must use ordinary `HeavenBase` CRUD so the workspace remains
  the single persistence and configuration authority.
- An omitted SDK workspace may use the object's originating workspace and then
  the configured default; reusable Python objects must not follow mutable active
  selection implicitly. Interfaces may deliberately resolve the active
  workspace and pass the resulting concrete object.
- Workspace-sensitive defaults resolve only at the persistence boundary under
  the stable workspace-id-derived config layer.
- Automatic recording is disabled by default, binds one concrete workspace when
  enabled, and reuses the same manual persistence path at explicit checkpoints.
  Later workspace activation must not redirect an existing recorder.
- No generic persistence Registry, repository abstraction, background scheduler,
  or core `ws.upsert` cascade is justified by this finding.

## Deferred Decisions

The future LLM-related extension must decide these together:

1. Whether separately queryable `agent-message` rows remain authoritative, which
   requires one aggregate persistence verb, or the complete event log becomes
   one self-contained Session row that ordinary `ws.upsert(session)` can persist.
   Do not keep both as competing authorities.
2. Whether `hb.Session` and `hb.LLMSession` remain separate public concepts or
   converge on one runtime model plus a persistence projection.
3. Whether durable identifiers are fixed in memory or allocated under workspace
   configuration at first persistence. An identifier must never change after
   publication.
4. Whether opt-in recording is enabled per session, per interface, or through a
   workspace policy; which completed events create checkpoints; and which
   automatic recording failures are best-effort.
5. How the RAW append-only event-log target represents history replacement
   without treating destructive Message-row replacement as the final model.

## Partial Resolution: the dashboard assistant session log

The assistant session log (`heavenbase.dashboard.assistant.session_log`,
`api/assist_log.py`) resolves the authority half of these decisions for its
own domain, without touching the generic `hb.Session`/`hb.LLMSession`
boundary:

- **Authority.** The `agent-trace` rows (`AgentTrace`, in the optional
  `agent-log` extension) hold one immutable canonical `request`
  row written before each provider call, terminal `response`/`error` rows,
  and one `turn` summary per turn. These rows are the authoritative record.
  A successful summary's `usage` merges the usage of every provider
  response of its attempt: each tool-loop round and the turn's compaction
  summaries (`turn_compaction`, `replay_compaction`). A response counts when
  it arrives, so under degraded capture the usage can include a response
  whose row failed to write. An errored summary records no usage, and a
  successful retry records only its own attempt's usage. An injected `llm`
  runtime is not traced and records the usage it returns. The projected
  session's usage merges its turns' usage.
  `agent-session`/`agent-message` rows are a readable projection,
  rebuildable at any time via `AssistSessionLog.rebuild_session()` for every
  conversation the log holds; they are never a competing authority (decision
  1, for this domain). Only the whole-session retention cap deletes trace
  rows, always together with the evicted conversation's projection.
- **Identifiers.** Provider-call event ids derive from conversation, turn,
  sequence, and kind (decision 3). Retried provider calls also include the attempt number
  in their event identity. When the client provides only `turn_id`, that id
  determines the logged conversation. One turn runs at a time per
  conversation in a log workspace across a parent Context and its children.
  A streaming request that disconnects while waiting
  leaves before retry admission. A retained completed turn replays without
  provider calls. A retry of a failed or
  interrupted attempt can make new provider calls under a fresh attempt identity.
  A successful retry supersedes the provisional failed summary at the
  same ID. The summary's `seq` records seal order for transcript repair.
- **Recording policy.** Opt-in through the dashboard's Context
  configuration, not a per-workspace layer: the sibling keys `session_log`
  (the switch, default `false`), `session_log_workspace`,
  `session_log_backend` (a backend declaration), `session_log_max_sessions`
  and `session_log_allow_credentials` directly under
  `heavenbase.dashboard.assistant`, read from the ConfigManager of the
  Context the `DashboardAPI` serves (`context.config`). Only the literal
  boolean `true` switches logging on. Any other value except `false` or
  null, such as `"true"` or `1`, keeps it off: neither boot nor a turn
  raises, no reply carries a `capture_warning`, and the process emits a
  single `UserWarning` the first time it reads such a value. With the
  switch on, the other settings are validated when `serve()`/`run_server`
  boot, which raises on an invalid one, and checked again at the start of
  each turn. That per-turn check does not
  raise: when it fails, the turn goes unrecorded, the reply carries a
  `capture_warning`, and the process emits one `UserWarning` per kind of
  failure. An admission read used to decide whether to replay or start a turn
  is retried once. If both reads fail, the request fails before provider I/O.
  Event and projection writes are best-effort (decision 4).
- **History replacement.** The append-only event log never replaces history;
  projection rebuilds use destructive Message-row replacement only on the
  derived rows, exactly as decision 5's warning requires.

Decision 2 (whether `hb.Session` and `hb.LLMSession` converge) remains
deferred and untouched.

## Resume Conditions

The issue is genuine but explicitly deferred for the generic LLM-session
boundary. Resume it only under a future LLM-related extension task, with the
decisions above reviewed one at a time. Do not introduce generic persistence
machinery or expand an automation extension merely to solve this boundary.
