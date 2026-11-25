// Floating assistant chat panel over the /api/assist endpoint (TypeScript source).
import { api, isAbortError, shouldRetryAssist, } from "./api.js";
import { icon } from "./icons.js";
import { t } from "./i18n.js";
import { markdown } from "./markdown.js";
import { assistantPresetSelection, assistantPresetSummaries, boundedHistoryPayload, replayableHistory, } from "./gui-logic.js";
import { copyText, el, iconBtn, select } from "./ui.js";
function historyTurn(value) {
    if (typeof value !== "object" || value === null || Array.isArray(value))
        return false;
    const record = value;
    return (record.role === "user" || record.role === "assistant") && typeof record.content === "string";
}
function errorMessage(error) {
    return error instanceof Error ? error.message : String(error);
}
// Conversation persistence: the server is stateless, so the browser owns one
// transcript per workspace. Turns (including tool events and frames, so tables
// re-render) survive reloads via localStorage, trimmed oldest-first under the quota.
const CHAT_STORE_PREFIX = "hb-chat-history:";
export function assistantHistoryKey(scope) {
    return `${CHAT_STORE_PREFIX}${encodeURIComponent(scope ? `workspace:${scope}` : "no-workspace")}`;
}
function loadHistory(scope) {
    try {
        const saved = JSON.parse(localStorage.getItem(assistantHistoryKey(scope)) || "[]");
        return Array.isArray(saved) ? saved.filter(historyTurn) : [];
    }
    catch {
        return [];
    }
}
function saveHistory(scope, history) {
    try {
        localStorage.setItem(assistantHistoryKey(scope), boundedHistoryPayload(history));
    }
    catch {
        // quota or serialization failure: this reload just starts fresh
    }
}
// Durable-session-log identity: one id per transcript, stored beside the
// history array (a sibling key keeps old transcripts loading unchanged).
// "New conversation" rotates it; the server only reads it when its
// session_log config is on.
const CHAT_CONVERSATION_PREFIX = "hb-chat-conversation:";
function conversationKey(scope) {
    return `${CHAT_CONVERSATION_PREFIX}${encodeURIComponent(scope ? `workspace:${scope}` : "no-workspace")}`;
}
// Page-lifetime fallback when localStorage is unavailable, so one visible
// transcript still maps to one logged conversation instead of one per turn.
const conversationFallback = new Map();
function conversationId(scope) {
    const key = conversationKey(scope);
    try {
        const existing = localStorage.getItem(key);
        if (existing)
            return existing;
        const minted = freshId();
        localStorage.setItem(key, minted);
        return minted;
    }
    catch {
        const held = conversationFallback.get(key) ?? freshId();
        conversationFallback.set(key, held);
        return held;
    }
}
function rotateConversationId(scope) {
    const key = conversationKey(scope);
    const minted = freshId();
    conversationFallback.set(key, minted);
    try {
        localStorage.setItem(key, minted);
    }
    catch {
        // storage unavailable: the rotated in-memory id above still applies
    }
}
function freshId() {
    return typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
const chat = {
    scope: undefined,
    history: [],
    open: false,
    busy: false,
    abort: null,
    preset: localStorage.getItem("hb-chat-preset") || "",
    readOnly: localStorage.getItem("hb-chat-read-only") !== "false",
};
const PANEL_EDGE = 8;
const PANEL_MIN_WIDTH = 300;
const PANEL_MIN_HEIGHT = 280;
// Resting geometry: docked just above the launcher bubble in the bottom-right
// corner; the status strip (--status-h) stays clear underneath the bubble.
const PANEL_DEFAULT_RIGHT = 16;
const PANEL_DEFAULT_BOTTOM = 92;
// Compact fallbacks for narrow viewports; large screens start near half the
// window (~¼ of the screen area) so the chat reads as a side tool, not a stub.
const PANEL_COMPACT_WIDTH = 360;
const PANEL_COMPACT_HEIGHT = 460;
const LIVE_RENDER_DELAY = 80;
const LIVE_MARKDOWN_LIMIT = 4000;
function finiteNumber(value, fallback) {
    if (value == null || typeof value === "boolean" || (typeof value === "string" && !value.trim()))
        return fallback;
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
}
function clampAxis(size, offset, viewport, minSize, fallbackSize, fallbackOffset, maxSize = Infinity) {
    const wantedSize = Math.max(1, finiteNumber(size, fallbackSize));
    const wantedOffset = Math.max(0, finiteNumber(offset, fallbackOffset));
    if (!Number.isFinite(viewport) || viewport <= 0)
        return { size: Math.max(minSize, wantedSize), offset: wantedOffset };
    const usable = Math.max(1, viewport - PANEL_EDGE);
    const minimum = Math.min(minSize, usable);
    const boundedSize = Math.max(minimum, Math.min(wantedSize, usable, maxSize));
    const boundedOffset = Math.min(wantedOffset, Math.max(0, usable - boundedSize));
    return { size: boundedSize, offset: boundedOffset };
}
// A chat panel is a side tool, not a workspace: on large screens its resting
// size (and max) is near half the viewport (~¼ of the screen) so it never
// owns the window. Small viewports keep the compact / near-full range.
const largeWidth = (viewport) => viewport >= 1100;
const largeHeight = (viewport) => viewport >= 800;
const halfWidth = (viewport) => Math.round(viewport * 0.5);
const halfHeight = (viewport) => Math.round(viewport * 0.55);
const defaultWidth = (viewport) => (largeWidth(viewport) ? halfWidth(viewport) : PANEL_COMPACT_WIDTH);
const defaultHeight = (viewport) => largeHeight(viewport) ? halfHeight(viewport) : PANEL_COMPACT_HEIGHT;
const cappedWidth = (viewport) => (largeWidth(viewport) ? halfWidth(viewport) : Infinity);
const cappedHeight = (viewport) => (largeHeight(viewport) ? halfHeight(viewport) : Infinity);
export function clampPanelGeometry(geometry = {}, width, height) {
    const horizontal = clampAxis(geometry.width, geometry.right, width, PANEL_MIN_WIDTH, defaultWidth(width), PANEL_DEFAULT_RIGHT, cappedWidth(width));
    const vertical = clampAxis(geometry.height, geometry.bottom, height, PANEL_MIN_HEIGHT, defaultHeight(height), PANEL_DEFAULT_BOTTOM, cappedHeight(height));
    return { width: horizontal.size, height: vertical.size, right: horizontal.offset, bottom: vertical.offset };
}
let panel = null;
let panelCleanup = null;
let fab = null;
// Tracks how much right-offset the open panel already absorbed for the
// docked inspector, so open/close can nudge by the delta instead of guessing.
let appliedInspectorClearance = 0;
// Bottom-right chat bubble: the persistent, always-visible entry point. The
// panel appears where the bubble lives, so opening feels like the bubble
// expanding rather than a window spawning from the far-left rail.
export function mountAssistant(state) {
    if (fab)
        return fab;
    fab = el("button.chat-fab", {
        type: "button",
        title: t("Open assistant"),
        "aria-label": t("Open assistant"),
        "aria-expanded": "false",
        onclick: () => toggleAssistant(state),
    }, icon("messageCircle"));
    document.body.append(fab);
    return fab;
}
function syncFab() {
    if (!fab)
        return;
    const label = chat.open ? t("Close assistant") : t("Open assistant");
    fab.replaceChildren(icon(chat.open ? "x" : "messageCircle"));
    fab.setAttribute("aria-expanded", String(chat.open));
    fab.setAttribute("aria-label", label);
    fab.title = label;
    fab.classList.toggle("open", chat.open);
}
function onEscape(event) {
    if (event.key !== "Escape" || !chat.open)
        return;
    if (document.querySelector(".modal-back, .palette-back"))
        return; // top-layer overlays own Escape first
    event.preventDefault();
    closeAssistant();
}
export function setAssistantScope(scope) {
    const next = scope || null;
    if (chat.scope === next)
        return chat.history.slice();
    if (chat.scope !== undefined)
        saveHistory(chat.scope, chat.history);
    chat.scope = next;
    chat.history = loadHistory(next);
    const log = panel?.querySelector(".chat-log");
    if (log)
        paintHistory(log);
    return chat.history.slice();
}
export function toggleAssistant(state) {
    setAssistantScope(state.wsId);
    if (chat.open) {
        closeAssistant();
        return;
    }
    chat.open = true;
    panel = buildPanel(state);
    document.body.append(panel);
    syncSendButton(); // a call may still be running from before the panel was closed
    panelCleanup = watchViewport(panel);
    // Capture phase + registered before any later overlay: when a modal/palette
    // sits above the chat it still exists at capture time, so Escape closes only
    // the top layer instead of both.
    document.addEventListener("keydown", onEscape, true);
    syncFab();
    panel.querySelector(".chat-input")?.focus();
}
function closeAssistant() {
    chat.open = false;
    panelCleanup?.();
    panelCleanup = null;
    panel?.remove();
    panel = null;
    appliedInspectorClearance = 0;
    document.removeEventListener("keydown", onEscape, true);
    syncFab();
    fab?.focus();
}
function inspectorClearance() {
    if (!document.body.classList.contains("inspector-open"))
        return 0;
    return finiteNumber(parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--inspector-w")), 0);
}
// Keep an already-open panel clear of the docked inspector (CSS only shifts
// the launcher bubble). Called whenever inspector-open toggles.
export function syncAssistantForInspector() {
    const next = inspectorClearance();
    const delta = next - appliedInspectorClearance;
    appliedInspectorClearance = next;
    if (!panel || !delta)
        return;
    const geometry = currentPanelGeometry(panel);
    geometry.right = Math.max(0, geometry.right + delta);
    applyPanelGeometry(panel, clampPanelGeometry(geometry, viewportWidth(), viewportHeight()));
}
export function pendingTurn() {
    const events = [];
    let text = "";
    const feed = (event) => {
        if (event.kind === "think") {
            const last = events[events.length - 1];
            if (last?.type === "think")
                last.text = `${last.text ?? ""}${event.text ?? ""}`;
            else
                events.push({ type: "think", text: event.text ?? "" });
        }
        else if (event.kind === "text") {
            text += event.text ?? "";
        }
        else if (event.kind === "tool") {
            // a preamble before tool calls is narrative, not the final answer: fold
            // it into the timeline (mirrors the server's done shape) instead of
            // gluing rounds together in the content
            if (text) {
                events.push({ type: "think", text });
                text = "";
            }
            const tool = {
                type: "tool",
                name: event.name ?? "",
                args: event.args ?? "",
            };
            if (event.frame != null)
                tool.frame = event.frame;
            if (event.sql_frame != null)
                tool.sql_frame = event.sql_frame;
            events.push(tool);
        }
        else if (event.kind === "tool_result") {
            // results directly follow their call in stream order; query calls carry
            // their lossless frame instead, so the text result only fills frame-less steps
            const last = events[events.length - 1];
            if (last?.type === "tool" && !last.frame && !last.sql_frame && last.result === undefined)
                last.result = event.text ?? "";
        }
    };
    // Always a turn — an abort before anything streamed still records an
    // "(interrupted)" marker, so no user question is left dangling in history
    // (which would also send consecutive user roles on the next call).
    const turn = () => ({
        role: "assistant",
        content: text,
        events: events.map((event) => ({ ...event })),
        interrupted: true,
    });
    return { feed, turn };
}
// One send/stop button per panel: while a call runs it becomes the interrupt
// affordance, aborting the in-flight request.
function syncSendButton() {
    const send = panel?.querySelector(".chat-send");
    if (!send)
        return;
    const label = chat.busy ? t("Stop") : t("Send");
    send.replaceChildren(icon(chat.busy ? "stop" : "send"));
    send.title = label;
    send.setAttribute("aria-label", label);
}
function buildPanel(state) {
    const log = el("div.chat-log");
    paintHistory(log);
    const presetZone = el("div.chat-preset-control", {}, el("span.chat-config-loading", {}, t("Loading presets…")));
    api
        .configTree({})
        .then((response) => {
        if (!presetZone.isConnected)
            return;
        const summaries = assistantPresetSummaries(response.tree);
        chat.preset = assistantPresetSelection(response.tree, chat.preset);
        localStorage.setItem("hb-chat-preset", chat.preset);
        const selectedMeta = el("span.chat-model-meta");
        const paintMeta = (preset) => {
            const summary = summaries.find((item) => item.value === preset);
            selectedMeta.textContent = summary
                ? [
                    summary.model || t("No model"),
                    summary.think ? t("Thinking on") : t("Thinking off"),
                    summary.reasoningEffort ? t("Effort {name}", { name: summary.reasoningEffort }) : "",
                ]
                    .filter(Boolean)
                    .join(" · ")
                : "";
        };
        const presetSelect = select({
            value: chat.preset,
            label: t("Assistant preset"),
            compact: true,
            searchable: true,
            options: summaries.map((item) => ({
                value: item.value,
                label: item.label,
                hint: [
                    item.model || t("No model"),
                    item.think ? t("Thinking on") : t("Thinking off"),
                    item.reasoningEffort ? t("Effort {name}", { name: item.reasoningEffort }) : "",
                ]
                    .filter(Boolean)
                    .join(" · "),
            })),
            onchange: (next) => {
                chat.preset = next;
                localStorage.setItem("hb-chat-preset", next);
                paintMeta(next);
            },
        });
        paintMeta(chat.preset);
        presetZone.replaceChildren(selectedMeta, presetSelect);
    })
        .catch(() => presetZone.replaceChildren(el("span.chat-config-loading", {}, t("Could not load presets"))));
    const input = el("textarea.textarea.chat-input", {
        placeholder: t("Ask about the current screen…"),
        rows: 2,
        "aria-label": t("Assistant"),
        onkeydown: (event) => {
            if (event.key === "Enter" && !event.shiftKey) {
                if (event.isComposing || event.keyCode === 229)
                    return; // IME confirm, not a send
                event.preventDefault();
                submit();
            }
        },
    });
    const accessToggle = el("button.chat-access", {
        role: "switch",
        type: "button",
        onclick: () => {
            chat.readOnly = !chat.readOnly;
            localStorage.setItem("hb-chat-read-only", String(chat.readOnly));
            paintAccess();
        },
    });
    const paintAccess = () => {
        const label = chat.readOnly ? t("Read-only") : t("Full access");
        accessToggle.dataset.readOnly = String(chat.readOnly);
        accessToggle.setAttribute("aria-checked", String(chat.readOnly));
        accessToggle.setAttribute("aria-label", label);
        accessToggle.replaceChildren(icon(chat.readOnly ? "lock" : "shield"), el("span", {}, label));
    };
    paintAccess();
    async function submit() {
        const question = input.value.trim();
        if (!question || chat.busy)
            return;
        const scope = chat.scope;
        const history = chat.history;
        input.value = "";
        history.push({ role: "user", content: question });
        chat.busy = true;
        const abort = new AbortController();
        chat.abort = abort;
        const runOptions = {
            readOnly: chat.readOnly,
            signal: abort.signal,
            conversationId: conversationId(scope),
            // One id per submission: the blocking fallback below reuses it, so a
            // retried turn is idempotent in the server's session log.
            turnId: freshId(),
            ...(chat.preset ? { preset: chat.preset } : {}),
        };
        syncSendButton();
        const started = Date.now();
        // The server only reads role/content; frames and tool events would ride
        // along for free and grow the upload every turn, so strip them here.
        // Error turns and content-less failed turns are dropped entirely — each
        // one together with its user question, to preserve role alternation. They
        // stay in chat.history, so the panel keeps showing them.
        const priorHistory = replayableHistory(history.slice(0, -1));
        paintHistory(log);
        const live = liveMessage();
        log.append(live.node);
        log.scrollTop = log.scrollHeight;
        let finalized = false;
        const partial = pendingTurn();
        const pushInterrupted = () => {
            history.push({ ...partial.turn(), elapsed: Math.round((Date.now() - started) / 100) / 10 });
        };
        // A stream that dies mid-turn has usually already produced reasoning, tool
        // calls and answer text; that work is the turn, and the failure is a note
        // on it. Replacing all of it with the error string threw the answer away.
        const pushFailed = (error) => {
            history.push({
                ...partial.turn(),
                interrupted: false,
                failure: errorMessage(error),
                elapsed: Math.round((Date.now() - started) / 100) / 10,
            });
        };
        try {
            // Native streaming: render the model's own chunks as they arrive.
            await api.assistStream(question, state.assistContext(), priorHistory, (event) => {
                if (event.kind === "done") {
                    finalized = true;
                    pushReply(event, history);
                }
                else {
                    partial.feed(event);
                    live.feed(event);
                }
            }, runOptions);
            if (!finalized)
                throw new Error(t("(empty reply)"));
        }
        catch (streamError) {
            // A final event is authoritative even if the transport closes uncleanly
            // afterwards; retrying would duplicate the completed model turn.
            if (finalized) {
                // keep the reply already recorded by the done event
            }
            else if (isAbortError(streamError)) {
                // user interrupt: keep whatever streamed, never restart the turn
                pushInterrupted();
            }
            else if (!shouldRetryAssist(streamError)) {
                pushFailed(streamError);
            }
            else {
                // Fall back only when streaming is unavailable or unsupported.
                try {
                    const reply = await api.assist(question, state.assistContext(), priorHistory, runOptions);
                    pushReply(reply, history);
                }
                catch (error) {
                    if (isAbortError(error))
                        pushInterrupted();
                    else
                        history.push({ role: "assistant", content: errorMessage(error), error: true });
                }
            }
        }
        finally {
            live.stop();
            chat.busy = false;
            if (chat.abort === abort)
                chat.abort = null;
            syncSendButton();
            saveHistory(scope, history);
            // The panel may have been closed and rebuilt while the call ran; paint
            // the log that is on screen now, not the one captured at submission.
            const currentLog = panel?.querySelector(".chat-log");
            if (chat.scope === scope && currentLog)
                paintHistory(currentLog);
        }
    }
    function pushReply(reply, history) {
        const events = reply.events?.length
            ? reply.events
            : [
                ...(reply.think ? [{ type: "think", text: reply.think }] : []),
                ...(reply.frames || []).map((frame) => ({ type: "tool", name: "query", frame })),
                ...(reply.sql_frames || []).map((sql_frame) => ({ type: "tool", name: "run_sql", sql_frame })),
            ];
        history.push({
            role: "assistant",
            content: reply.text || t("(empty reply)"),
            served: reply.served_model || "",
            events,
            elapsed: reply.elapsed || 0,
        });
    }
    // No close button here on purpose: the launcher bubble that opened the
    // panel is the single close affordance (plus Escape) — a second ✕ inside
    // the dialog duplicated it.
    const head = el("div.panel-head.chat-drag", { style: { borderBottom: "1px solid var(--border)" }, title: t("Drag to move") }, icon("sparkles"), el("span.title", {}, t("Assistant")), iconBtn("edit", t("New conversation"), () => {
        chat.history = [];
        saveHistory(chat.scope, chat.history);
        rotateConversationId(chat.scope); // a cleared panel is a new logged conversation, not a continuation
        paintHistory(log);
        input.focus();
    }));
    const node = el("div.chat-panel", { role: "dialog", "aria-label": t("Assistant") }, resizeHandle(() => node), head, log, el("div.chat-foot", {}, input, el("div.chat-composer-tools", {}, accessToggle, el("div.spacer"), presetZone, iconBtn("send", t("Send"), () => (chat.busy ? chat.abort?.abort() : submit()), "chat-send"))));
    enableDrag(head, () => node);
    applySavedSize(node);
    return node;
}
// ------------------------------------------------------------------- drag
// Embedded webviews can report a zero-size window while layout still has real
// geometry; treat that as "no bound" instead of pinning the panel to a corner.
const viewportWidth = () => window.innerWidth || document.documentElement.clientWidth || Infinity;
const viewportHeight = () => window.innerHeight || document.documentElement.clientHeight || Infinity;
function panelOffset(node, key, fallback) {
    const value = parseFloat(getComputedStyle(node)[key]);
    return Number.isFinite(value) ? value : fallback;
}
function applyPanelGeometry(node, geometry) {
    node.style.width = `${geometry.width}px`;
    node.style.height = `${geometry.height}px`;
    node.style.right = `${geometry.right}px`;
    node.style.bottom = `${geometry.bottom}px`;
}
function currentPanelGeometry(node, width = node.offsetWidth, height = node.offsetHeight) {
    return clampPanelGeometry({
        width,
        height,
        right: panelOffset(node, "right", PANEL_DEFAULT_RIGHT),
        bottom: panelOffset(node, "bottom", PANEL_DEFAULT_BOTTOM),
    }, viewportWidth(), viewportHeight());
}
function watchViewport(node) {
    const clamp = () => applyPanelGeometry(node, currentPanelGeometry(node));
    window.addEventListener("resize", clamp);
    return () => window.removeEventListener("resize", clamp);
}
// The panel is fixed with right/bottom offsets, so dragging the header moves
// it by adjusting those offsets; the position persists like the size does.
function enableDrag(head, getPanel) {
    head.addEventListener("pointerdown", (event) => {
        if (event.target instanceof Element && event.target.closest("button"))
            return;
        event.preventDefault();
        const node = getPanel();
        const rect = node.getBoundingClientRect();
        const startX = event.clientX;
        const startY = event.clientY;
        const styles = getComputedStyle(node);
        const startRight = Number.isFinite(parseFloat(styles.right)) ? parseFloat(styles.right) : PANEL_DEFAULT_RIGHT;
        const startBottom = Number.isFinite(parseFloat(styles.bottom)) ? parseFloat(styles.bottom) : PANEL_DEFAULT_BOTTOM;
        let right = startRight;
        let bottom = startBottom;
        try {
            head.setPointerCapture(event.pointerId);
        }
        catch {
            // synthetic events carry no active pointer; the move/up listeners still work
        }
        const move = (ev) => {
            right = Math.max(0, Math.min(viewportWidth() - rect.width - PANEL_EDGE, startRight - (ev.clientX - startX)));
            bottom = Math.max(0, Math.min(viewportHeight() - rect.height - PANEL_EDGE, startBottom - (ev.clientY - startY)));
            node.style.right = `${right}px`;
            node.style.bottom = `${bottom}px`;
        };
        const up = () => {
            head.removeEventListener("pointermove", move);
            head.removeEventListener("pointerup", up);
            head.removeEventListener("pointercancel", up);
            localStorage.setItem("hb-chat-pos", JSON.stringify({ r: right, b: bottom }));
        };
        head.addEventListener("pointermove", move);
        head.addEventListener("pointerup", up);
        head.addEventListener("pointercancel", up);
    });
}
// ------------------------------------------------------------------ resize
function storedObject(key) {
    try {
        const parsed = JSON.parse(localStorage.getItem(key) || "null");
        return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
            ? parsed
            : null;
    }
    catch {
        return null;
    }
}
function applySavedSize(node) {
    const saved = storedObject("hb-chat-size");
    const pos = storedObject("hb-chat-pos");
    // With the inspector docked open, the resting spot sidesteps it (same rule as
    // the bubble) so the panel never covers the inspector; a dragged position is
    // the user's explicit choice and is kept as-is.
    appliedInspectorClearance = inspectorClearance();
    applyPanelGeometry(node, clampPanelGeometry({
        width: finiteNumber(saved?.w, node.offsetWidth),
        height: finiteNumber(saved?.h, node.offsetHeight),
        right: finiteNumber(pos?.r, PANEL_DEFAULT_RIGHT + appliedInspectorClearance),
        bottom: finiteNumber(pos?.b, PANEL_DEFAULT_BOTTOM),
    }, viewportWidth(), viewportHeight()));
}
// The panel is anchored bottom-right, so dragging the top-left handle outward grows it.
function resizeHandle(getPanel) {
    return el("div.chat-resize", {
        role: "separator",
        tabindex: 0,
        "aria-label": t("Resize assistant panel"),
        onkeydown: (event) => {
            if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key))
                return;
            event.preventDefault();
            const node = getPanel();
            const geometry = currentPanelGeometry(node, node.offsetWidth + (event.key === "ArrowLeft" ? 10 : event.key === "ArrowRight" ? -10 : 0), node.offsetHeight + (event.key === "ArrowUp" ? 10 : event.key === "ArrowDown" ? -10 : 0));
            applyPanelGeometry(node, geometry);
            localStorage.setItem("hb-chat-size", JSON.stringify({ w: geometry.width, h: geometry.height }));
        },
        onpointerdown: (event) => {
            event.preventDefault();
            const handle = event.currentTarget;
            if (!(handle instanceof Element))
                return;
            const node = getPanel();
            const startX = event.clientX;
            const startY = event.clientY;
            const startW = node.offsetWidth;
            const startH = node.offsetHeight;
            let width = startW;
            let height = startH;
            try {
                handle.setPointerCapture(event.pointerId);
            }
            catch {
                // synthetic events carry no active pointer; the move/up listeners still work
            }
            const move = (ev) => {
                if (!(ev instanceof PointerEvent))
                    return;
                const geometry = currentPanelGeometry(node, startW - (ev.clientX - startX), startH - (ev.clientY - startY));
                width = geometry.width;
                height = geometry.height;
                applyPanelGeometry(node, geometry);
            };
            const up = () => {
                handle.removeEventListener("pointermove", move);
                handle.removeEventListener("pointerup", up);
                handle.removeEventListener("pointercancel", up);
                localStorage.setItem("hb-chat-size", JSON.stringify({ w: width, h: height }));
            };
            handle.addEventListener("pointermove", move);
            handle.addEventListener("pointerup", up);
            handle.addEventListener("pointercancel", up);
        },
    });
}
// Hover-only copy affordance: a small floating button that lives OUTSIDE the
// content it copies (message bubble corner or table top-right) so the content
// itself stays clean until the pointer asks for actions.
function floatCopy(label, getText) {
    return el("button.chat-copy-float", { type: "button", title: label, "aria-label": label, onclick: () => copyText(getText(), t("Copied")) }, icon("copy"));
}
function paintHistory(log) {
    // every mutation flows through a repaint, so this is the one persistence point
    saveHistory(chat.scope, chat.history);
    log.replaceChildren();
    if (!chat.history.length) {
        log.append(el("div.palette-hint", {}, t("Answers use the configured heavenbase.llm preset and the current screen context.")));
    }
    for (const turn of chat.history) {
        // Assistant replies render as Markdown (safe DOM build); user/error text stays plain.
        const content = turn.role === "assistant" && !turn.error ? withTableCopy(markdown(turn.content)) : turn.content;
        const msg = el(`div.chat-msg.${turn.role}${turn.error ? ".error" : ""}`);
        if (turn.events?.length)
            msg.append(timelineBlock(turn.events, turn.elapsed));
        msg.append(content instanceof Node ? content : document.createTextNode(content));
        if (!turn.error && turn.content)
            msg.append(floatCopy(t("Copy reply"), () => turn.content));
        if (turn.interrupted)
            msg.append(el("div.chat-foot-row", {}, el("div.chat-served", {}, t("(interrupted)"))));
        if (turn.failure)
            msg.append(el("div.chat-foot-row", {}, el("div.chat-served.chat-failure", {}, `${t("(failed)")} ${turn.failure}`)));
        // Wire-level identity: the model id reported by the provider's response.
        if (turn.served)
            msg.append(el("div.chat-foot-row", {}, el("div.chat-served", {}, `${t("served by")} ${turn.served}`)));
        log.append(msg);
    }
    log.scrollTop = log.scrollHeight;
}
function disclosure(headParts, body, { headClass = "think-head", initiallyOpen = false } = {}) {
    let open = initiallyOpen;
    const chevron = el("span.think-chevron", { "aria-hidden": "true" }, open ? "\u25be" : "\u25b8");
    body.style.display = open ? "" : "none";
    const head = el(`button.${headClass}`, {
        "aria-expanded": String(open),
        onclick: () => {
            open = !open;
            body.style.display = open ? "" : "none";
            chevron.textContent = open ? "\u25be" : "\u25b8";
            head.setAttribute("aria-expanded", String(open));
        },
    }, chevron, ...headParts);
    return { head, body };
}
// Claude-style thinking: ONE collapsed block per reply whose body interleaves
// the per-round reasoning with the tool calls exactly in execution order; tool
// steps nest their own disclosure (engine table for query, text otherwise).
function timelineBlock(events, elapsed = 0) {
    const body = el("div.think-body", {}, events.map((event) => event.type === "think"
        ? el("div.timeline-think", {}, withTableCopy(markdown(event.text || "")))
        : toolStep(event)));
    const label = elapsed >= 1 ? t("Thought for {n}s", { n: elapsed >= 10 ? Math.round(elapsed) : elapsed }) : t("Thinking");
    const { head } = disclosure([el("span", {}, label)], body);
    return el("div.think", {}, head, body);
}
// One tool call inside the thinking stream: a compact head line expanding to
// the lossless query table or the tool's truncated text result.
function toolStep(event) {
    const frame = event.frame;
    if (frame) {
        const body = el("div.chat-frame-section-body", {}, frameBody(frame));
        // A capped result still knows its exact match count, so the head shows it.
        const count = (frame.truncated ? frame.total : null) ?? frame.n ?? frame.rows.length;
        const { head } = disclosure([
            el("span", {}, `${event.name ?? "tool"} \u00b7 ${frame.entity} \u00b7 ${t("{name} rows", { name: count })}`),
            el("span.chat-frame-caption", {}, JSON.stringify(frame.spec)),
        ], body);
        return el("div.chat-frame", {}, head, body);
    }
    const sqlFrame = event.sql_frame;
    if (sqlFrame) {
        const body = el("div.chat-frame-section-body", {}, sqlFrameBody(sqlFrame));
        const count = (sqlFrame.truncated ? sqlFrame.total : null) ?? sqlFrame.n ?? sqlFrame.rows.length;
        const { head } = disclosure([
            el("span", {}, `${event.name ?? "run_sql"} · ${sqlFrame.database_id} · ${t("{name} rows", { name: count })}`),
            el("span.chat-frame-caption", {}, sqlFrame.sql),
        ], body);
        return el("div.chat-frame", {}, head, body);
    }
    const body = el("div.chat-frame-section-body", {}, toolResultBody(event.result || event.args || ""));
    const { head } = disclosure([el("span", {}, `${event.name}(${(event.args || "").slice(0, 60)})`)], body);
    return el("div.chat-frame", {}, head, body);
}
// Give every Markdown-rendered table in a chat message its own copy button;
// the TSV is read from the rendered cells so it always matches what is shown.
// The scroll wrap is boxed in a .table-copy-wrap so the floating button stays
// pinned to the table's top-right corner while the table scrolls underneath.
function withTableCopy(node) {
    for (const table of [...node.querySelectorAll("table")]) {
        const tsv = tableTsv([...table.rows].map((row) => [...row.cells].map((cell) => cell.textContent)));
        const wrap = table.closest(".md-table-wrap") || table;
        const box = el("div.table-copy-wrap");
        wrap.parentNode?.insertBefore(box, wrap);
        box.append(wrap, floatCopy(t("Copy table"), () => tsv));
    }
    return node;
}
function exactCell(value) {
    if (value === null || value === undefined)
        return "";
    return typeof value === "object" ? JSON.stringify(value) : String(value);
}
function tsvCell(value) {
    const text = exactCell(value);
    return /[\t\r\n"]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}
export function tableTsv(rows) {
    return rows.map((row) => row.map(tsvCell).join("\t")).join("\n");
}
function cellText(value) {
    if (value === null || value === undefined)
        return "";
    if (typeof value === "number" && !Number.isInteger(value))
        return String(Math.round(value * 10000) / 10000);
    return typeof value === "object" ? JSON.stringify(value) : String(value);
}
const isPlainObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
// Own-property read: JSON.parse objects still inherit Object.prototype, so a
// union column named e.g. "constructor" must not leak inherited members.
const own = (obj, key) => (Object.hasOwn(obj, key) ? obj[key] : undefined);
function resultTable(columns, rows) {
    const table = el("table.chat-frame-table", {}, el("thead", {}, el("tr", {}, columns.map((name) => el("th", {}, name)))), el("tbody", {}, rows.map((cells) => el("tr", {}, cells.map((value) => el("td", { title: exactCell(value) }, cellText(value)))))));
    const tsv = tableTsv([columns, ...rows]);
    return el("div.table-copy-wrap", {}, el("div.chat-frame-scroll", {}, table), floatCopy(t("Copy table"), () => tsv));
}
function keyValueBody(pairs) {
    return el("div.chat-kv", {}, pairs.map(([key, value]) => [
        el("span.k", {}, key),
        isPlainObject(value) || Array.isArray(value)
            ? el("span.v.mono-v", {}, JSON.stringify(value, null, 1))
            : el("span.v", {}, cellText(value)),
    ]));
}
// Structured render for a non-query tool result: arrays of records and dicts
// of records (e.g. entity_schema fields) become tables, flat keys become
// key/value rows, anything else pretty-prints as indented JSON; text that is
// not JSON (including backend-truncated payloads) stays monospace as-is.
export function toolResultBody(text) {
    const raw = String(text ?? "");
    let parsed;
    try {
        parsed = JSON.parse(raw);
    }
    catch {
        return el("div.chat-tool-result", {}, raw);
    }
    if (isPlainObject(parsed) && parsed.ok === false && parsed.error !== undefined) {
        // failed tool call: one compact error line instead of a kv breakdown
        const error = parsed.error;
        const message = isPlainObject(error) ? [error.type, error.message].filter(Boolean).join(": ") : String(error);
        const metadata = [
            ...(isPlainObject(error) ? Object.entries(error).filter(([key]) => key !== "type" && key !== "message") : []),
            ...Object.entries(parsed).filter(([key]) => key !== "ok" && key !== "error"),
        ];
        return el("div.chat-tool-structured", {}, el("div.chat-tool-error", {}, message || t("(failed)")), metadata.length ? keyValueBody(metadata) : null);
    }
    if (Array.isArray(parsed) && parsed.length && parsed.every(isPlainObject)) {
        const columns = [...new Set(parsed.flatMap((row) => Object.keys(row)))];
        if (columns.length)
            return resultTable(columns, parsed.map((row) => columns.map((name) => own(row, name))));
    }
    if (isPlainObject(parsed) && Object.keys(parsed).length) {
        // Frame-shaped payloads (sample_rows, query-like {columns, rows, ...})
        // render as one table in declared column order; other keys become kv rows.
        const declared = Array.isArray(parsed.columns) && parsed.columns.length && parsed.columns.every((name) => typeof name === "string")
            ? parsed.columns
            : null;
        if (declared && Array.isArray(parsed.rows)) {
            const cells = parsed.rows.map((row) => isPlainObject(row) ? declared.map((name) => own(row, name)) : Array.isArray(row) ? row : [row]);
            const rest = Object.entries(parsed).filter(([key]) => key !== "columns" && key !== "rows");
            return el("div.chat-tool-structured", {}, rest.length ? keyValueBody(rest) : null, resultTable(declared, cells));
        }
        const pairs = [];
        const sections = [];
        for (const [key, value] of Object.entries(parsed)) {
            const entries = isPlainObject(value) ? Object.entries(value) : [];
            const rows = Array.isArray(value) ? value : [];
            if (entries.length && entries.every(([, inner]) => isPlainObject(inner))) {
                // dict of records, e.g. entity_schema fields: first column is the key
                // (blank header — the section label above already names it)
                const records = entries.flatMap(([name, inner]) => (isPlainObject(inner) ? [[name, inner]] : []));
                const columns = [...new Set(records.flatMap(([, inner]) => Object.keys(inner)))];
                sections.push([
                    key,
                    resultTable(["", ...columns], records.map(([name, inner]) => [name, ...columns.map((column) => own(inner, column))])),
                ]);
            }
            else if (entries.length &&
                entries.every(([, inner]) => Array.isArray(inner) && inner.length && inner.every(isPlainObject))) {
                // dict of record lists, e.g. entity_schema storage: one row per record
                const flat = entries.flatMap(([name, list]) => Array.isArray(list)
                    ? list.flatMap((row) => (isPlainObject(row) ? [[name, row]] : []))
                    : []);
                const columns = [...new Set(flat.flatMap(([, row]) => Object.keys(row)))];
                sections.push([
                    key,
                    resultTable(["", ...columns], flat.map(([name, row]) => [name, ...columns.map((column) => own(row, column))])),
                ]);
            }
            else if (rows.length && rows.every(isPlainObject) && rows.some((row) => Object.keys(row).length)) {
                const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))];
                sections.push([
                    key,
                    resultTable(columns, rows.map((row) => columns.map((column) => own(row, column)))),
                ]);
            }
            else {
                pairs.push([key, value]);
            }
        }
        return el("div.chat-tool-structured", {}, pairs.length ? keyValueBody(pairs) : null, sections.map(([key, table]) => el("div.chat-tool-section", {}, el("div.chat-tool-section-label", {}, key), table)));
    }
    return el("div.chat-tool-result", {}, JSON.stringify(parsed, null, 2));
}
export function frameBody(frame) {
    const rows = frame.rows;
    const columns = frame.columns?.length ? frame.columns : Object.keys(rows[0] || {});
    const shown = rows.slice(0, 50);
    const table = el("table.chat-frame-table", {}, el("thead", {}, el("tr", {}, columns.map((name) => el("th", {}, name)))), el("tbody", {}, shown.map((row) => el("tr", {}, columns.map((name) => el("td", { title: exactCell(row[name]) }, cellText(row[name])))))));
    // frame.total is the engine's exact match count when the server cut rows;
    // otherwise every matching row is here and n is already the whole result.
    const total = frame.truncated ? (frame.total ?? frame.n ?? rows.length) : (frame.n ?? rows.length);
    const rawOffset = "spec" in frame ? frame.spec?.offset : 0;
    const offset = typeof rawOffset === "number" && Number.isInteger(rawOffset) && rawOffset >= 0 ? rawOffset : 0;
    const start = shown.length ? offset + 1 : 0;
    const end = offset + shown.length;
    const note = frame.truncated || rows.length > shown.length
        ? el("div.chat-frame-note", {}, frame.truncated
            ? t("{start}\u2013{end} / {total} \u00b7 truncated", { start, end, total })
            : t("{start}\u2013{end} / {total}", { start, end, total }))
        : null;
    const tsv = tableTsv([columns, ...rows.map((row) => columns.map((name) => row[name]))]);
    return el("div", { style: { display: "flex", flexDirection: "column", gap: "4px" } }, el("div.table-copy-wrap", {}, el("div.chat-frame-scroll", {}, table), floatCopy(t("Copy table"), () => tsv)), note);
}
export function sqlFrameBody(frame) {
    return el("div.chat-sql-evidence", {}, el("div.table-copy-wrap", {}, el("pre.chat-sql", {}, el("code", {}, frame.sql)), floatCopy(t("Copy SQL"), () => frame.sql)), frameBody(frame));
}
function liveMessage() {
    const body = el("div.think-body");
    const { head } = disclosure([el("span.think-pulse", {}, t("Thinking\u2026"))], body, { initiallyOpen: true });
    const node = el("div.chat-msg.assistant.busy", {}, el("div.think", {}, head, body));
    // Everything streams inside the thinking container while the call runs —
    // reasoning muted, spoken text in normal color — so nothing ever jumps
    // between areas; the finalized turn renders the real answer below.
    // Native chunks are painted at a bounded cadence. Reduced-motion users and
    // long replies get stable plain text while streaming; final content is
    // rendered as Markdown once when the segment closes.
    let segment = null;
    let segmentKind = "";
    let lastToolBody = null;
    let queue = "";
    let timer = null;
    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches === true;
    const scroll = () => {
        const parent = node.parentElement;
        if (parent && parent.scrollHeight - parent.scrollTop - parent.clientHeight < 120)
            parent.scrollTop = parent.scrollHeight;
    };
    const renderSegment = (final = false) => {
        if (!segment)
            return;
        if (!final && (reducedMotion || segment._raw.length > LIVE_MARKDOWN_LIMIT)) {
            segment.textContent = segment._raw;
            return;
        }
        segment.replaceChildren(withTableCopy(markdown(segment._raw)));
    };
    const drain = () => {
        if (!queue.length || !segment)
            return;
        segment._raw += queue;
        queue = "";
        renderSegment();
        scroll();
    };
    const flush = () => {
        if (timer !== null)
            clearTimeout(timer);
        timer = null;
        drain();
        renderSegment(true);
        scroll();
    };
    const write = (kind, text) => {
        if (!segment || segmentKind !== kind) {
            flush();
            segment = el(kind === "say" ? "div.timeline-say" : "div.timeline-think", {});
            segment._raw = "";
            segmentKind = kind;
            body.append(segment);
        }
        queue += String(text ?? "");
        if (reducedMotion)
            drain();
        else if (!timer) {
            timer = setTimeout(() => {
                timer = null;
                drain();
            }, LIVE_RENDER_DELAY);
        }
    };
    const feed = (event) => {
        if (event.kind === "think")
            write("think", event.text);
        else if (event.kind === "text")
            write("say", event.text);
        else if (event.kind === "tool") {
            flush();
            segment = null;
            const step = toolStep({
                type: "tool",
                ...(event.name == null ? {} : { name: event.name }),
                ...(event.args == null ? {} : { args: event.args }),
                ...(event.frame == null ? {} : { frame: event.frame }),
                ...(event.sql_frame == null ? {} : { sql_frame: event.sql_frame }),
            });
            lastToolBody = event.frame || event.sql_frame ? null : step.querySelector(".chat-frame-section-body");
            body.append(step);
            scroll();
        }
        else if (event.kind === "tool_result") {
            if (lastToolBody)
                lastToolBody.replaceChildren(toolResultBody(event.text || ""));
            lastToolBody = null;
            scroll();
        }
    };
    const stop = () => {
        flush();
    };
    return { node, feed, stop };
}
