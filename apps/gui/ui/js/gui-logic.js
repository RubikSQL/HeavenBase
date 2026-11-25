/** Pure GUI value transitions shared by browser controls and contract tests. */
function record(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value) ? value : {};
}
/** Return user-facing preset metadata with inherited reasoning settings resolved. */
export function assistantPresetSummaries(tree) {
    const root = record(tree);
    const heavenbase = record(root.heavenbase ?? root);
    const llm = record(heavenbase.llm);
    const presets = record(llm.presets);
    const models = record(llm.models);
    const globalArgs = record(llm.default_args);
    const modelByName = (name) => {
        if (models[name])
            return record(models[name]);
        for (const spec of Object.values(models)) {
            const aliases = record(spec).aliases;
            if (Array.isArray(aliases) && aliases.includes(name))
                return record(spec);
        }
        return {};
    };
    return Object.entries(presets).map(([name, rawPreset]) => {
        const preset = record(rawPreset);
        const model = typeof preset.model === "string" ? preset.model : "";
        const modelArgs = record(modelByName(model).default_args);
        const presetArgs = record(preset.default_args);
        const think = presetArgs.think ?? modelArgs.think ?? globalArgs.think ?? false;
        const reasoningEffort = presetArgs.reasoning_effort ?? modelArgs.reasoning_effort ?? globalArgs.reasoning_effort ?? "";
        return {
            value: name,
            label: name,
            model,
            think: think === true,
            reasoningEffort: reasoningEffort == null ? "" : String(reasoningEffort),
        };
    });
}
/** Resolve the assistant composer preset from saved state and durable config. */
export function assistantPresetSelection(tree, saved = "") {
    const root = record(tree);
    const heavenbase = record(root.heavenbase ?? root);
    const llm = record(heavenbase.llm);
    const assistant = record(record(heavenbase.dashboard).assistant);
    const names = new Set(assistantPresetSummaries(tree).map((item) => item.value));
    const configured = typeof assistant.preset === "string" ? assistant.preset : "";
    const llmDefault = typeof llm.default_preset === "string" ? llm.default_preset : "";
    for (const candidate of [saved, configured, "chat", llmDefault]) {
        if (candidate && names.has(candidate))
            return candidate;
    }
    return names.values().next().value ?? "";
}
// Match the server's default replay window. The payload guard remains the
// final backstop for unusually large prose turns.
export const HISTORY_TURN_CEILING = 40;
/** Bound the stored chat history payload: a turn-count sanity ceiling under the 2MB guard that really binds. */
export function boundedHistoryPayload(turns) {
    let kept = turns.slice(-HISTORY_TURN_CEILING);
    let payload = JSON.stringify(kept);
    while (payload.length > 2_000_000 && kept.length > 2) {
        kept = kept.slice(2); // drop whole user/assistant pairs from the front
        payload = JSON.stringify(kept);
    }
    return payload;
}
/** Strip stored chat turns down to the role/content pairs worth resending to the model. */
export function replayableHistory(turns) {
    // Error turns, and failed turns that streamed nothing, are dropped rather
    // than stripped: their content is our own failure string (or empty), and
    // replaying it as an assistant message makes the model read a transport
    // error as something it once said. A failed turn with real content keeps
    // its answer; the failure stays a panel-side footnote.
    const kept = [];
    for (const turn of turns) {
        if (turn.error || (turn.failure && !turn.content)) {
            // The server appends history verbatim with no same-role merging, so a
            // dropped assistant turn takes its user question with it — otherwise
            // two user turns land adjacent and break strict role alternation.
            if (kept[kept.length - 1]?.role === "user")
                kept.pop();
            continue;
        }
        kept.push({ role: turn.role, content: turn.content });
    }
    return kept;
}
export function renameConfigKey(current, source, target) {
    const name = String(target || "").trim();
    if (!name || name === source)
        return { ...current };
    if (Object.hasOwn(current, name))
        throw new Error(`${name} already exists`);
    return Object.fromEntries(Object.entries(current).map(([key, value]) => [key === source ? name : key, value]));
}
export function parseEnvReference(value) {
    if (typeof value !== "string")
        return null;
    const match = value.match(/^\$\{env:([A-Za-z_][A-Za-z0-9_]*)(?:,(.*))?\}$/s);
    if (!match)
        return null;
    const name = match[1];
    if (!name)
        return null;
    const encodedFallback = (match[2] ?? "").trim();
    if (!encodedFallback)
        return { name, fallback: "" };
    if (encodedFallback.startsWith('"') && encodedFallback.endsWith('"')) {
        try {
            const parsed = JSON.parse(encodedFallback);
            return { name, fallback: typeof parsed === "string" ? parsed : String(parsed ?? "") };
        }
        catch {
            return { name, fallback: encodedFallback.slice(1, -1) };
        }
    }
    return { name, fallback: encodedFallback };
}
export function defaultEnvVariable(entryName, fieldName) {
    const normalize = (value) => value
        .trim()
        .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
        .replace(/[^A-Za-z0-9]+/g, "_")
        .replace(/^_+|_+$/g, "")
        .toUpperCase();
    return [normalize(entryName), normalize(fieldName)].filter(Boolean).join("_");
}
export function parseTypedScalar(raw, dtypeId) {
    if (/^(int|integer|timestamp)/i.test(dtypeId))
        return /^-?\d+$/.test(raw) ? Number(raw) : raw;
    if (/^(float|double|number)/i.test(dtypeId)) {
        const number = Number(raw);
        return Number.isNaN(number) ? raw : number;
    }
    if (/^bool/i.test(dtypeId))
        return raw === "true" || raw === "1";
    if (/^(identifier|short-text|medium-text|long-text|text|categorical|date)/i.test(dtypeId))
        return raw;
    if (raw === "true" || raw === "false")
        return raw === "true";
    if (raw !== "" && /^-?(\d+\.?\d*|\.\d+)$/.test(raw))
        return Number(raw);
    return raw;
}
export function browserPanelBounds(stacked, viewportHeight) {
    const min = stacked ? 120 : 180;
    return {
        min,
        max: stacked ? Math.max(min, Math.round(viewportHeight * 0.8)) : 420,
        axis: stacked ? "--browser-h" : "--browser-w",
        orientation: stacked ? "horizontal" : "vertical",
    };
}
export function statsSeparatorValues({ stacked, viewportHeight, columnHeight, treeHeight, panelHeight, treeMin = 24, panelMin = 40, }) {
    const remainingBand = stacked ? Math.max(0, Math.round(viewportHeight * 0.8) - columnHeight) : 0;
    return {
        tree: {
            min: treeMin,
            max: Math.max(treeMin, treeHeight + (stacked ? remainingBand : Math.max(0, panelHeight - panelMin))),
            now: treeHeight,
        },
        panel: { min: panelMin, max: Math.max(panelMin, panelHeight + remainingBand), now: panelHeight },
    };
}
