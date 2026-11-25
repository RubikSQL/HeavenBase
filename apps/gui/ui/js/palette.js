// Global search palette over workspace Catalog/MetaSchema cards (typed ws.search client).
import { api } from "./api.js";
import { icon } from "./icons.js";
import { t } from "./i18n.js";
import { el, debounce, toast } from "./ui.js";
const KIND_ICONS = {
    entity: "table",
    field: "columns",
    object: "box",
    storage: "database",
    backend: "server",
    capability: "zap",
    workspace: "layers",
};
const KIND_ORDER = ["entity", "field", "object", "storage", "backend", "capability", "workspace"];
const KIND_LABELS = { object: "records" };
function searchCard(value) {
    if (typeof value.kind !== "string")
        return null;
    const optional = (name) => (typeof value[name] === "string" ? value[name] : undefined);
    return {
        kind: value.kind,
        score: typeof value.score === "number" ? value.score : 0,
        ...(optional("name") === undefined ? {} : { name: optional("name") }),
        ...(optional("desc") === undefined ? {} : { desc: optional("desc") }),
        ...(optional("field") === undefined ? {} : { field: optional("field") }),
        ...(optional("subject_id") === undefined ? {} : { subject_id: optional("subject_id") }),
        ...(optional("object_id") === undefined ? {} : { object_id: optional("object_id") }),
        ...(optional("target_entity") === undefined ? {} : { target_entity: optional("target_entity") }),
        ...(optional("target_id") === undefined ? {} : { target_id: optional("target_id") }),
        ...(optional("type") === undefined ? {} : { type: optional("type") }),
    };
}
function errorMessage(error) {
    return error instanceof Error ? error.message : String(error);
}
// Cards carry their identity in kind-specific columns; never render a blank row.
function cardLabel(card) {
    if (card.kind === "object")
        return `${card.target_entity} · ${card.target_id}`;
    if (card.field)
        return `${card.subject_id}.${card.field}`;
    return card.name || card.subject_id || card.object_id || "";
}
function cardDesc(card) {
    if (card.kind === "storage") {
        // name is entity.field.instance.strategy; type is the backend type
        const [, , instance, strategy] = (card.name || "").split(".");
        return [
            card.type && `${t("backend type")}: ${card.type}`,
            instance && `${t("instance")}: ${instance}`,
            strategy && `${t("strategy")}: ${strategy}`,
        ]
            .filter(Boolean)
            .join(" · ");
    }
    return card.desc || "";
}
let overlay = null;
export function closePalette() {
    overlay?.remove();
    overlay = null;
}
export function openPalette(state) {
    if (overlay) {
        closePalette();
        return;
    }
    if (!state.wsId) {
        toast(t("Open a workspace to search"), "error");
        return;
    }
    let items = [];
    let active = 0;
    const results = el("div.palette-results");
    const input = el("input.input.palette-input", {
        placeholder: t("Search entities, fields, backends…"),
        "aria-label": t("Search workspace"),
        oninput: debounce(() => runSearch(input.value.trim()), 180),
        onkeydown: (event) => {
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                if (!items.length)
                    return;
                active = (active + (event.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
                paintActive();
            }
            else if (event.key === "Enter") {
                if (event.isComposing || event.keyCode === 229)
                    return; // IME confirm, not a pick
                event.preventDefault();
                const selected = items[active];
                if (selected)
                    pick(selected);
            }
        },
    });
    function paintActive() {
        [...results.querySelectorAll(".palette-item")].forEach((node, index) => {
            node.classList.toggle("active", index === active);
        });
        results.querySelector(".palette-item.active")?.scrollIntoView({ block: "nearest" });
    }
    function pick(card) {
        closePalette();
        const toData = () => state.route !== "data" && state.navigate("data");
        if (card.kind === "object" && card.target_entity) {
            toData();
            state.openEntity(card.target_entity, card.target_id ? { objectId: card.target_id } : undefined);
        }
        else if (card.kind === "storage" && card.subject_id) {
            toData();
            state.openEntity(card.subject_id, { mode: "schema" }); // bindings live in the Schema view
        }
        else if ((card.kind === "entity" || card.kind === "field") && card.subject_id) {
            toData();
            state.openEntity(card.subject_id);
        }
        else {
            toast(`${card.kind} · ${cardLabel(card)}`);
        }
    }
    async function runSearch(text) {
        if (!text) {
            items = [];
            results.replaceChildren(el("div.palette-hint", {}, t("Type to search Catalog and MetaSchema cards")));
            return;
        }
        let rows = [];
        let withheld = 0;
        try {
            const found = await api.search({ q: text, limit: 30 });
            rows = found.rows.map(searchCard).filter((card) => card !== null);
            // ranking sees every match before the cap, so the shortfall is exact
            if (found.truncated)
                withheld = found.total - found.rows.length;
        }
        catch (error) {
            results.replaceChildren(el("div.palette-hint", {}, errorMessage(error)));
            return;
        }
        // Group by kind in a fixed order (score order kept within each group) so
        // one interleaved score list cannot repeat group headers.
        const rank = (card) => {
            const index = KIND_ORDER.indexOf(card.kind);
            return index < 0 ? KIND_ORDER.length : index;
        };
        items = [...rows].sort((a, b) => rank(a) - rank(b) || (b.score ?? 0) - (a.score ?? 0));
        active = 0;
        results.replaceChildren();
        if (!items.length) {
            results.replaceChildren(el("div.palette-hint", {}, t("No matches")));
            return;
        }
        let lastKind = null;
        if (withheld > 0)
            results.append(el("div.palette-hint", {}, t("{n} more matches — narrow the search", { n: withheld })));
        items.forEach((card, index) => {
            if (card.kind !== lastKind) {
                results.append(el("div.menu-label", {}, t(KIND_LABELS[card.kind] ?? card.kind ?? "other")));
                lastKind = card.kind;
            }
            const desc = cardDesc(card);
            results.append(el(`button.palette-item${index === 0 ? ".active" : ""}`, { onclick: () => pick(card) }, icon(KIND_ICONS[card.kind] ?? "dot"), el("span.pi-name", {}, cardLabel(card)), desc ? el("span.pi-desc", {}, desc) : null, el("span.hint", {}, String(card.score ?? ""))));
        });
    }
    overlay = el("div.palette-back", {
        onpointerdown: (event) => {
            if (event.target === overlay)
                closePalette();
        },
        onkeydown: (event) => {
            if (event.key === "Escape")
                closePalette();
        },
    }, el("div.palette", { role: "dialog", "aria-label": t("Search workspace") }, el("div.palette-head", {}, icon("search"), input), results));
    document.body.append(overlay);
    runSearch("");
    input.focus();
}
