/**
 * Generated-type and runtime-validation boundary for the dashboard protocol.
 *
 * `generated/schema.d.ts` is produced from the server's committed OpenAPI
 * document. Network JSON remains `unknown` until a route-specific guard
 * validates the shape used by the UI.
 */

import type { components } from "./generated/schema.js";

type Schemas = components["schemas"];
type Guard<T> = (value: unknown) => value is T;
type QueryValue = string | number | boolean | null | undefined;
export type QueryParams = Readonly<Record<string, QueryValue>>;
export type JsonValue = Schemas["JsonValue"];
export type JsonRecord = Readonly<Record<string, JsonValue>>;
export type MetaResponse = Schemas["MetaResponse"];
export type ConfigMetaResponse = Schemas["ConfigMetaResponse"];
export type ConfigTreeResponse = Schemas["ConfigTreeResponse"];
export type ConfigValueResponse = Schemas["ConfigValueResponse"];
export type ConfigMutationResponse = Schemas["ConfigMutationResponse"];
export type ConfigResetResponse = Schemas["ConfigResetResponse"];
export type ConfigHistoryResponse = Schemas["ConfigHistoryResponse"];
export type ConfigDiffResponse = Schemas["ConfigDiffResponse"];
export type DashboardPreferencesRequest = Schemas["DashboardPreferencesRequest"];
export type DashboardPreferencesResponse = Schemas["DashboardPreferencesResponse"];
export type WorkspacesResponse = Schemas["WorkspacesResponse"];
export type WorkspaceRemoveResponse = Schemas["WorkspaceRemoveResponse"];
export type WorkspaceHealthResponse = Schemas["WorkspaceHealthResponse"];
export type EntitiesResponse = Schemas["EntitiesResponse"];
export type EntitySchemaResponse = Schemas["EntitySchemaResponse"];
export type EntityRowsRequest = Schemas["EntityRowsRequest"];
export type EntityRowsResponse = Schemas["EntityRowsResponse"];
export type EntityRowResponse = Schemas["EntityRowResponse"];
export type ColumnStatsResponse = Schemas["ColumnStatsResponse"];
export type SearchResponse = Schemas["SearchResponse"];
export type AssistantKeyResponse = Schemas["AssistantKeyResponse"];
export type AssistTurn = Schemas["AssistTurn"];
export type AssistFrame = Schemas["AssistFrame"];
export type AssistSQLFrame = Schemas["AssistSQLFrame"];
export type AssistTimelineEvent = Schemas["AssistTimelineEvent"];
export type AssistResponse = Schemas["AssistResponse"];
export type AssistProgressResponse = Schemas["AssistProgressResponse"];
export type AssistEvent = Schemas["AssistEvent"];
export type AssistEventHandler = (event: AssistEvent) => void;

export interface AssistRunOptions {
  readonly preset?: string;
  readonly readOnly?: boolean;
  readonly progressId?: string;
  readonly signal?: AbortSignal;
  /** Durable-session-log identity of the whole browser conversation. */
  readonly conversationId?: string;
  /** Per-submission identity; the blocking fallback reuses the streaming
   * attempt's id so a retried turn writes the same log rows, not duplicates. */
  readonly turnId?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStringArray(value: unknown): value is readonly string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isRecordArray(value: unknown): value is readonly Record<string, unknown>[] {
  return Array.isArray(value) && value.every(isRecord);
}

function isMeta(value: unknown): value is MetaResponse {
  return isRecord(value) && typeof value.name === "string" && typeof value.version === "string";
}

function isConfigMeta(value: unknown): value is ConfigMetaResponse {
  return (
    isRecord(value) &&
    isStringArray(value.scopes) &&
    typeof value.scope === "string" &&
    isStringArray(value.scope_chain) &&
    isRecord(value.versions) &&
    typeof value.root === "string"
  );
}

function isConfigTree(value: unknown): value is ConfigTreeResponse {
  return (
    isRecord(value) &&
    isRecord(value.tree) &&
    isRecord(value.origins) &&
    typeof value.scope === "string" &&
    (value.version === null || typeof value.version === "number")
  );
}

function isMutation(value: unknown): value is ConfigMutationResponse {
  return isRecord(value) && value.ok === true && typeof value.key === "string";
}

function isReset(value: unknown): value is ConfigResetResponse {
  if (!isRecord(value)) return false;
  const origin = value.origin;
  return isMutation(value) && (origin === "builtin" || origin === "custom");
}

function isHistory(value: unknown): value is ConfigHistoryResponse {
  return (
    Array.isArray(value) &&
    value.every(
      (row) =>
        isRecord(row) && typeof row.scope === "string" && typeof row.ver === "number" && typeof row.gen === "number",
    )
  );
}

function isDiff(value: unknown): value is ConfigDiffResponse {
  return isRecord(value) && typeof value.diff === "string";
}

function isPreferences(value: unknown): value is DashboardPreferencesResponse {
  return (
    isRecord(value) &&
    typeof value.theme === "string" &&
    ["theme-light", "theme-dark", "theme-anthropic"].includes(value.theme) &&
    typeof value.language === "string" &&
    ["en", "zh"].includes(value.language) &&
    typeof value.font === "string" &&
    ["small", "default", "large"].includes(value.font) &&
    typeof value.font_scale === "string" &&
    ["small", "default", "large", "xlarge"].includes(value.font_scale) &&
    typeof value.configured === "boolean"
  );
}

function isWorkspaces(value: unknown): value is WorkspacesResponse {
  return (
    Array.isArray(value) &&
    value.every(
      (row) =>
        isRecord(row) && typeof row.id === "string" && typeof row.active === "boolean" && typeof row.open === "boolean",
    )
  );
}

function isWorkspaceRemove(value: unknown): value is WorkspaceRemoveResponse {
  return (
    isRecord(value) &&
    typeof value.ws_id === "string" &&
    typeof value.removed === "boolean" &&
    typeof value.was_open === "boolean"
  );
}

function isHealth(value: unknown): value is WorkspaceHealthResponse {
  return (
    isRecord(value) &&
    typeof value.ws_id === "string" &&
    typeof value.workspace === "string" &&
    isStringArray(value.entities) &&
    Array.isArray(value.backends) &&
    value.backends.every(
      (backend) => isRecord(backend) && typeof backend.name === "string" && isRecord(backend.capabilities),
    )
  );
}

function isEntities(value: unknown): value is EntitiesResponse {
  return (
    Array.isArray(value) &&
    value.every(
      (row) =>
        isRecord(row) &&
        typeof row.entity_id === "string" &&
        typeof row.name === "string" &&
        isStringArray(row.tags) &&
        typeof row.system === "boolean",
    )
  );
}

function isEntitySchema(value: unknown): value is EntitySchemaResponse {
  return (
    isRecord(value) &&
    isRecord(value.schema) &&
    isRecord(value.storage) &&
    isRecord(value.capabilities) &&
    isRecordArray(value.backends)
  );
}

function isEntityRows(value: unknown): value is EntityRowsResponse {
  return (
    isRecord(value) &&
    isRecordArray(value.rows) &&
    isStringArray(value.columns) &&
    typeof value.total === "number" &&
    typeof value.has_more === "boolean" &&
    typeof value.offset === "number" &&
    typeof value.limit === "number"
  );
}

function isColumnStats(value: unknown): value is ColumnStatsResponse {
  return (
    isRecord(value) &&
    typeof value.field === "string" &&
    typeof value.dtype === "string" &&
    typeof value.kind === "string" &&
    typeof value.total === "number" &&
    typeof value.scanned === "number" &&
    typeof value.sampled === "boolean" &&
    typeof value.nulls === "number" &&
    typeof value.null_pct === "number"
  );
}

function isSearch(value: unknown): value is SearchResponse {
  return (
    isRecord(value) &&
    isRecordArray(value.rows) &&
    typeof value.n === "number" &&
    typeof value.total === "number" &&
    typeof value.truncated === "boolean"
  );
}

function isAssistantKey(value: unknown): value is AssistantKeyResponse {
  return (
    isRecord(value) &&
    typeof value.provider === "string" &&
    (value.field === "api_key" || value.field === "base_url") &&
    typeof value.set === "boolean" &&
    typeof value.tail === "string" &&
    (value.source === "environment" || value.source === "fallback" || value.source === "literal") &&
    (value.env_var === undefined || value.env_var === null || typeof value.env_var === "string") &&
    (value.env_set === undefined || value.env_set === null || typeof value.env_set === "boolean")
  );
}

function isConfigValue(value: unknown): value is ConfigValueResponse {
  return (
    isRecord(value) &&
    typeof value.key === "string" &&
    typeof value.set === "boolean" &&
    typeof value.tail === "string" &&
    (value.source === "environment" || value.source === "manual") &&
    (value.env_var === undefined || value.env_var === null || typeof value.env_var === "string") &&
    (value.env_set === undefined || value.env_set === null || typeof value.env_set === "boolean") &&
    (value.value === undefined || value.value === null || typeof value.value === "string")
  );
}

function isAssistFrame(value: unknown): value is AssistFrame {
  return (
    isRecord(value) &&
    typeof value.entity === "string" &&
    isRecord(value.spec) &&
    isStringArray(value.columns) &&
    isRecordArray(value.rows) &&
    typeof value.n === "number"
  );
}

function isAssistSQLFrame(value: unknown): value is AssistSQLFrame {
  return (
    isRecord(value) &&
    typeof value.database_id === "string" &&
    typeof value.sql === "string" &&
    isRecord(value.params) &&
    isStringArray(value.columns) &&
    isRecordArray(value.rows) &&
    typeof value.n === "number" &&
    (value.total == null || typeof value.total === "number") &&
    (value.limit == null || typeof value.limit === "number") &&
    typeof value.truncated === "boolean" &&
    typeof value.readonly === "boolean"
  );
}

function isAssistTimelineEvent(value: unknown): value is AssistTimelineEvent {
  return (
    isRecord(value) &&
    (value.type === "think" || value.type === "tool") &&
    (value.text == null || typeof value.text === "string") &&
    (value.name == null || typeof value.name === "string") &&
    (value.args == null || typeof value.args === "string") &&
    (value.result == null || typeof value.result === "string") &&
    (value.frame == null || isAssistFrame(value.frame)) &&
    (value.sql_frame == null || isAssistSQLFrame(value.sql_frame))
  );
}

function isAssist(value: unknown): value is AssistResponse {
  return (
    isRecord(value) &&
    typeof value.text === "string" &&
    typeof value.think === "string" &&
    typeof value.served_model === "string" &&
    Array.isArray(value.frames) &&
    value.frames.every(isAssistFrame) &&
    Array.isArray(value.sql_frames) &&
    value.sql_frames.every(isAssistSQLFrame) &&
    Array.isArray(value.events) &&
    value.events.every(isAssistTimelineEvent) &&
    typeof value.elapsed === "number" &&
    typeof value.truncated === "boolean"
  );
}

function isAssistProgress(value: unknown): value is AssistProgressResponse {
  return (
    isRecord(value) &&
    Array.isArray(value.lines) &&
    value.lines.every(
      (line) => isRecord(line) && (line.type === "tool" || line.type === "think") && typeof line.text === "string",
    )
  );
}

const ASSIST_EVENT_KINDS = new Set(["think", "text", "tool", "tool_result", "done", "error"]);

function isAssistEvent(value: unknown): value is AssistEvent {
  return (
    isRecord(value) &&
    typeof value.kind === "string" &&
    ASSIST_EVENT_KINDS.has(value.kind) &&
    (value.text == null || typeof value.text === "string") &&
    (value.think == null || typeof value.think === "string") &&
    (value.served_model == null || typeof value.served_model === "string") &&
    (value.frames == null || (Array.isArray(value.frames) && value.frames.every(isAssistFrame))) &&
    (value.sql_frames == null || (Array.isArray(value.sql_frames) && value.sql_frames.every(isAssistSQLFrame))) &&
    (value.events == null || (Array.isArray(value.events) && value.events.every(isAssistTimelineEvent))) &&
    (value.elapsed == null || typeof value.elapsed === "number") &&
    (value.truncated == null || typeof value.truncated === "boolean") &&
    (value.name == null || typeof value.name === "string") &&
    (value.args == null || typeof value.args === "string") &&
    (value.frame == null || isAssistFrame(value.frame)) &&
    (value.sql_frame == null || isAssistSQLFrame(value.sql_frame))
  );
}

function isError(value: unknown): value is Schemas["ErrorResponse"] {
  return isRecord(value) && typeof value.error === "string";
}

export class ProtocolResponseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ProtocolResponseError";
  }
}

export class AssistResponseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AssistResponseError";
  }
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

// Inactivity budget for the assist stream, not a cap on the turn: a real
// answer legitimately runs for minutes, so only silence is evidence of a dead
// transport. The server beats `ASSIST_KEEPALIVE_SECONDS` (15s comment frames,
// src/heavenbase/api/http.py), so this window is several missed heartbeats
// wide before it gives up.
const ASSIST_STREAM_IDLE_MS = 90_000;

// Total-duration cap for the blocking /api/assist fallback — one long POST
// with no traffic to arm an idle window on, and the every-turn path on
// engines without streaming. The server's LLM call runs ~120s per attempt
// with retries disabled (see DashboardAPI._assist_llm_options), so a response
// still pending after this window is a hung transport, not a slow answer.
const ASSIST_REQUEST_TIMEOUT_MS = 150_000;

// AbortSignal.any needs Safari 17.4+/Chrome 116+ and this UI ships as plain
// untranspiled browser modules, so composition is done by hand. The returned
// cleanup must run once the request settles, or the listeners outlive it on
// long-lived caller signals.
function forwardAborts(target: AbortController, sources: readonly (AbortSignal | undefined)[]): () => void {
  const live = sources.filter((signal): signal is AbortSignal => signal !== undefined);
  const forward = (): void => target.abort();
  for (const signal of live) {
    if (signal.aborted) forward();
    else signal.addEventListener("abort", forward);
  }
  return () => {
    for (const signal of live) signal.removeEventListener("abort", forward);
  };
}

export function shouldRetryAssist(error: unknown): boolean {
  return !(isAbortError(error) || error instanceof AssistResponseError || error instanceof ProtocolResponseError);
}

async function request<T>(
  method: "DELETE" | "GET" | "POST",
  path: string,
  guard: Guard<T>,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const headers = new Headers();
  const options: RequestInit = { method, headers };
  if (signal !== undefined) options.signal = signal;
  if (body !== undefined) {
    headers.set("Content-Type", "application/json");
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const text = await response.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { error: text };
  }
  if (!response.ok) {
    const message = isError(data) ? data.error : `${method} ${path} failed (${response.status})`;
    throw new Error(message);
  }
  if (!guard(data)) {
    throw new ProtocolResponseError(`${method} ${path} returned data outside its OpenAPI contract`);
  }
  return data;
}

function qs(params: QueryParams = {}): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

export const api = {
  meta: (): Promise<MetaResponse> => request("GET", "/api/meta", isMeta),
  configMeta: (): Promise<ConfigMetaResponse> => request("GET", "/api/config/meta", isConfigMeta),
  configTree: (params: QueryParams): Promise<ConfigTreeResponse> =>
    request("GET", `/api/config/tree${qs(params)}`, isConfigTree),
  configValueStatus: (key: string, reveal = false): Promise<ConfigValueResponse> =>
    request("GET", `/api/config/value-status${qs({ key, reveal: reveal ? 1 : undefined })}`, isConfigValue),
  configSet: (
    key: string,
    value: JsonValue,
    scope?: string,
    parse: "auto" | "json" | "raw" = "raw",
  ): Promise<ConfigMutationResponse> =>
    request("POST", "/api/config/set", isMutation, { key, value, scope: scope ?? null, parse }),
  configUnset: (key: string, scope?: string): Promise<ConfigMutationResponse> =>
    request("POST", "/api/config/unset", isMutation, { key, scope: scope ?? null }),
  configReset: (key: string, scope?: string): Promise<ConfigResetResponse> =>
    request("POST", "/api/config/reset", isReset, { key, scope: scope ?? null }),
  configHistory: (params: QueryParams): Promise<ConfigHistoryResponse> =>
    request("GET", `/api/config/history${qs(params)}`, isHistory),
  configDiff: (params: QueryParams): Promise<ConfigDiffResponse> =>
    request("GET", `/api/config/diff${qs(params)}`, isDiff),
  preferences: (): Promise<DashboardPreferencesResponse> => request("GET", "/api/preferences", isPreferences),
  updatePreferences: (preferences: DashboardPreferencesRequest): Promise<DashboardPreferencesResponse> =>
    request("POST", "/api/preferences", isPreferences, preferences),
  workspaces: (): Promise<WorkspacesResponse> => request("GET", "/api/workspaces", isWorkspaces),
  removeWorkspace: (wsId: string): Promise<WorkspaceRemoveResponse> =>
    request("DELETE", `/api/workspaces/${encodeURIComponent(wsId)}`, isWorkspaceRemove),
  openWorkspace: (wsId: string): Promise<WorkspaceHealthResponse> =>
    request("POST", "/api/workspace/open", isHealth, { ws_id: wsId }),
  health: (): Promise<WorkspaceHealthResponse> => request("GET", "/api/workspace/health", isHealth),
  entities: (params: QueryParams): Promise<EntitiesResponse> =>
    request("GET", `/api/workspace/entities${qs(params)}`, isEntities),
  entitySchema: (entityId: string): Promise<EntitySchemaResponse> =>
    request("GET", `/api/workspace/entity/${encodeURIComponent(entityId)}/schema`, isEntitySchema),
  entityRows: (entityId: string, body: Partial<EntityRowsRequest>): Promise<EntityRowsResponse> =>
    request("POST", `/api/workspace/entity/${encodeURIComponent(entityId)}/rows`, isEntityRows, body),
  entityRow: (entityId: string, objectId: string): Promise<EntityRowResponse> =>
    request(
      "GET",
      `/api/workspace/entity/${encodeURIComponent(entityId)}/row/${encodeURIComponent(objectId)}`,
      isRecord,
    ),
  columnStats: (entityId: string, field: string): Promise<ColumnStatsResponse> =>
    request(
      "GET",
      `/api/workspace/entity/${encodeURIComponent(entityId)}/stats/${encodeURIComponent(field)}`,
      isColumnStats,
    ),
  search: (params: QueryParams): Promise<SearchResponse> =>
    request("GET", `/api/workspace/search${qs(params)}`, isSearch),
  assistantKey: (provider: string, reveal: boolean, field: "api_key" | "base_url"): Promise<AssistantKeyResponse> =>
    request("GET", `/api/assistant/key${qs({ provider, reveal: reveal ? 1 : undefined, field })}`, isAssistantKey),
  assist: async (
    question: string,
    context: JsonRecord | null,
    history: readonly AssistTurn[],
    options: AssistRunOptions = {},
  ): Promise<AssistResponse> => {
    const deadline = new AbortController();
    const unforward = forwardAborts(deadline, [options.signal]);
    const timer = setTimeout(() => deadline.abort(), ASSIST_REQUEST_TIMEOUT_MS);
    try {
      return await request(
        "POST",
        "/api/assist",
        isAssist,
        {
          question,
          context,
          history,
          progress_id: options.progressId ?? null,
          preset: options.preset ?? null,
          read_only: options.readOnly ?? null,
          conversation_id: options.conversationId ?? null,
          turn_id: options.turnId ?? null,
        },
        deadline.signal,
      );
    } catch (error) {
      // Our own deadline surfaces as a failure; only the caller's signal
      // reads as the stop button.
      if (deadline.signal.aborted && options.signal?.aborted !== true) {
        throw new AssistResponseError(`assist request exceeded ${ASSIST_REQUEST_TIMEOUT_MS / 1000}s`);
      }
      throw error;
    } finally {
      clearTimeout(timer);
      unforward();
    }
  },
  assistProgress: (id: string): Promise<AssistProgressResponse> =>
    request("GET", `/api/assist/progress${qs({ id })}`, isAssistProgress),
  assistStream: async (
    question: string,
    context: JsonRecord | null,
    history: readonly AssistTurn[],
    onEvent: AssistEventHandler,
    options: AssistRunOptions = {},
  ): Promise<void> => {
    options.signal?.throwIfAborted();
    const idle = new AbortController();
    let idleTimer: ReturnType<typeof setTimeout> | undefined;
    const armIdle = (): void => {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(() => idle.abort(), ASSIST_STREAM_IDLE_MS);
    };
    // Nothing has streamed until the response headers land; the idle error
    // must not claim a stream that never opened went silent.
    let opened = false;
    // Both awaits route their rejection here. The idle abort rides its own
    // controller so it can be told apart from the caller's stop button, which
    // the panel reads as a user interrupt rather than a failure.
    const rethrowStalled = (error: unknown): never => {
      if (idle.signal.aborted && options.signal?.aborted !== true) {
        throw new AssistResponseError(
          opened
            ? `assist stream went silent for ${ASSIST_STREAM_IDLE_MS / 1000}s`
            : `assist stream did not respond within ${ASSIST_STREAM_IDLE_MS / 1000}s`,
        );
      }
      throw error;
    };
    // Composed, not replaced: the stop button and the idle guard must both
    // be able to tear down the same request.
    const teardown = new AbortController();
    const unforward = forwardAborts(teardown, [options.signal, idle.signal]);
    const requestOptions: RequestInit = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        context,
        history,
        preset: options.preset ?? null,
        read_only: options.readOnly ?? null,
        conversation_id: options.conversationId ?? null,
        turn_id: options.turnId ?? null,
      }),
      signal: teardown.signal,
    };
    try {
      armIdle();
      const response = await fetch("/api/assist/stream", requestOptions).catch(rethrowStalled);
      // The window measures transport silence, never how long the answer
      // takes: the timer re-arms when the response headers land (here) and
      // after every chunk read below, keepalive comment frames included.
      opened = true;
      armIdle();
      if (!response.ok || !response.body) throw new Error(`stream unavailable (${response.status})`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      try {
        for (;;) {
          const { done, value } = await reader.read().catch(rethrowStalled);
          armIdle(); // any chunk resets the window (see the comment above)
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let cut = buffer.indexOf("\n\n");
          while (cut >= 0) {
            const line = buffer.slice(0, cut).trim();
            buffer = buffer.slice(cut + 2);
            if (line.startsWith("data: ")) {
              let event: unknown;
              try {
                event = JSON.parse(line.slice(6));
              } catch {
                throw new ProtocolResponseError("assist stream returned malformed JSON");
              }
              if (!isAssistEvent(event)) {
                throw new ProtocolResponseError("assist stream returned an event outside its OpenAPI contract");
              }
              if (event.kind === "error") {
                throw new AssistResponseError(typeof event.text === "string" ? event.text : "assist failed");
              }
              onEvent(event);
            }
            cut = buffer.indexOf("\n\n");
          }
        }
      } finally {
        try {
          await reader.cancel();
        } catch {
          // Abort or transport teardown may already have closed the body. Keep
          // the original stream result or error authoritative.
        }
        reader.releaseLock();
      }
    } finally {
      clearTimeout(idleTimer);
      unforward();
    }
  },
};
