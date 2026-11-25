// Column statistics body over /api/workspace/entity/{id}/stats/{field}; the typed
// data view hosts it in the bottom-left stats panel. Rendering is kind-aware:
// numeric columns get quartiles + a histogram, text columns a length
// distribution, time columns format/range, JSON columns key frequencies,
// vectors dims/norm, lists item counts + top elements.
import { t } from "./i18n.js";
import { el } from "./ui.js";
function statRow(key, value) {
    return el("div.tt-row", {}, el("span.k", { style: { minWidth: "90px" } }, key), el("span.v", {}, value));
}
function label(text) {
    return el("div.panel-label", { style: { padding: "0" } }, text);
}
// A list heading that carries the list's own coverage. Counts can cover every
// row while the published distribution is still display-bounded, so each fact
// is stated independently where the reader sees the numbers.
function sectionLabel(text, { truncated = false, allRows = false } = {}) {
    const notes = [allRows ? t("all rows") : null, truncated ? t("truncated") : null].filter(Boolean);
    return label(notes.length ? `${text} (${notes.join(", ")})` : text);
}
// An elided cell says so in its tooltip, since the shown text itself cannot.
function cellTitle(cell) {
    return cell.value_truncated
        ? `${cell.value}\u2026 (${t("{n} chars", { n: cell.value_len ?? cell.value.length })})`
        : cell.value;
}
// Horizontal value·bar·count rows, scaled to the largest count.
function barRows(pairs, denom = 0) {
    const maxCount = Math.max(...pairs.map((pair) => pair.count), 1);
    return el("div.stat-rows", {}, pairs.map((pair) => el("div.stat-top-row", {}, 
    // every published cell carries its own elision markers; dropping them
    // here would show a prefix as if it were the whole value
    el("span.stat-top-value", { title: cellTitle(pair) }, pair.value), el("div.stat-top-bar", {}, el("div.stat-top-fill", { style: { width: `${(pair.count / maxCount) * 100}%` } })), el("span.stat-top-count", {}, denom ? `${pair.count} · ${Math.round((pair.count / denom) * 100)}%` : String(pair.count)))));
}
// Mini histogram with an instant readout line: hovering a bin immediately
// shows its range and count above the bars (no native-tooltip delay).
function histogram(bins) {
    const maxCount = Math.max(...bins.map((bin) => bin.count), 1);
    const readout = el("div.stat-hist-readout", {}, "\u00a0");
    const bars = el("div.stat-hist", { role: "group", "aria-label": t("Distribution histogram"), onmouseleave: () => (readout.textContent = "\u00a0") }, bins.map((bin) => {
        const summary = `${trimNum(bin.lo)} \u2013 ${trimNum(bin.hi)}: ${bin.count}`;
        return el("div.stat-hist-bin", {
            role: "img",
            tabindex: 0,
            "aria-label": summary,
            onmouseenter: () => (readout.textContent = summary),
            onfocus: () => (readout.textContent = summary),
            onblur: () => (readout.textContent = "\u00a0"),
            style: { height: `${Math.max((bin.count / maxCount) * 100, bin.count ? 4 : 0)}%` },
        });
    }));
    return el("div", {}, readout, bars);
}
function trimNum(value) {
    return typeof value === "number" ? String(Math.round(value * 100) / 100) : String(value);
}
// One {min, mean, max} box as its own labelled group of rows; collapses to a
// single row when the column is constant.
function rangeRows(name, box) {
    if (box.min != null && box.min === box.max)
        return el("div.stat-rows", {}, statRow(name, trimNum(box.min)));
    return el("div", { style: { display: "flex", flexDirection: "column", gap: "4px" } }, label(name), el("div.stat-rows", {}, box.min != null ? statRow(t("min"), trimNum(box.min)) : null, box.mean != null ? statRow(t("avg"), trimNum(box.mean)) : null, box.max != null ? statRow(t("max"), trimNum(box.max)) : null));
}
/** Render one column's statistics; layout follows the backend's ``kind``. */
export function buildStatsBody(stats) {
    const zone = el("div", { style: { display: "flex", flexDirection: "column", gap: "10px" } });
    const nullPct = stats.null_pct ?? (stats.scanned ? Math.round((stats.nulls / stats.scanned) * 1000) / 10 : 0);
    // Common header: dtype, totals, and the always-visible null count + ratio bar.
    zone.append(el("div.stat-rows", {}, statRow(t("dtype"), stats.dtype || "?"), statRow(t("total"), String(stats.total)), stats.sampled ? statRow(t("scanned"), `${stats.scanned} · ${t("sampled")}`) : null, statRow(t("nulls"), `${stats.nulls} (${nullPct}%)`), stats.distinct != null ? statRow(t("distinct"), String(stats.distinct)) : null));
    if (stats.kind === "numeric") {
        const q = stats.percentiles || {};
        zone.append(el("div.stat-rows", {}, stats.min != null ? statRow(t("p0 (min)"), trimNum(stats.min)) : null, q.p25 != null ? statRow("p25", trimNum(q.p25)) : null, q.p50 != null ? statRow(t("p50 (med)"), trimNum(q.p50)) : null, q.p75 != null ? statRow("p75", trimNum(q.p75)) : null, stats.max != null ? statRow(t("p100 (max)"), trimNum(stats.max)) : null, stats.mean != null ? statRow(t("avg"), trimNum(stats.mean)) : null));
        const numericHistogram = stats.histogram;
        if (numericHistogram && numericHistogram.length > 1) {
            zone.append(label(t("distribution")), histogram(numericHistogram));
        }
    }
    else if (stats.kind === "time") {
        zone.append(el("div.stat-rows", {}, stats.format ? statRow(t("format"), stats.format) : null, stats.earliest ? statRow(t("earliest"), stats.earliest) : null, stats.latest ? statRow(t("latest"), stats.latest) : null, stats.span_days != null ? statRow(t("span"), t("{n} days", { n: stats.span_days })) : null, stats.unparsed ? statRow(t("unparsed"), String(stats.unparsed)) : null));
    }
    else if (stats.kind === "vector") {
        if (stats.dims)
            zone.append(el("div.stat-rows", {}, statRow(t("dims"), stats.dims)));
        if (stats.norm)
            zone.append(rangeRows(t("norm (L2)"), stats.norm));
    }
    else if (stats.kind === "json" || stats.kind === "list") {
        // rows that are neither object nor list contribute to none of the numbers
        // below, so they carry their own count and distribution
        if (stats.scalars)
            zone.append(el("div.stat-rows", {}, statRow(t("scalar rows"), String(stats.scalars))));
        if (stats.scalar_values?.length)
            zone.append(sectionLabel(t("scalar values"), {
                allRows: !stats.sampled,
                truncated: stats.scalar_values_truncated,
            }), barRows(stats.scalar_values, stats.scalars ?? 0));
        if (stats.key_count)
            zone.append(rangeRows(t("keys per row"), stats.key_count));
        if (stats.items)
            zone.append(rangeRows(t("items per row"), stats.items));
        if (stats.size)
            zone.append(rangeRows(t("size (chars)"), stats.size));
        if (stats.keys?.length) {
            // Engine-computed key counts span every row, so they must be shown against
            // the entity total; only the scan-derived fallback shares the scan's denominator.
            const keyDenom = stats.keys_exact ? stats.total : stats.scanned - stats.nulls;
            zone.append(sectionLabel(t("top keys"), { allRows: stats.keys_exact, truncated: stats.keys_truncated }), barRows(
            // carry the payload's own elision markers through, or a cut key
            // renders as a silent prefix of the real one
            stats.keys.map((pair) => ({
                value: pair.key,
                count: pair.count,
                ...(pair.key_elided ? { value_truncated: true, value_len: pair.key_len } : {}),
            })), keyDenom));
        }
        if (stats.elements?.length) {
            zone.append(sectionLabel(t("top elements"), {
                allRows: stats.elements_exact,
                truncated: stats.elements_truncated,
            }), barRows(stats.elements));
        }
    }
    else {
        // text / identifier: length distribution first, values second.
        if (stats.length)
            zone.append(rangeRows(t("length"), stats.length));
        const lengthHistogram = stats.length_histogram;
        if (lengthHistogram && lengthHistogram.length > 1) {
            zone.append(label(t("length distribution")), histogram(lengthHistogram));
        }
        if (stats.examples?.length) {
            zone.append(label(t("examples")), el("div.stat-rows", {}, stats.examples.map((example) => 
            // an elided example must not be quoted back as if it were the value
            el("div.stat-example", { title: cellTitle(example) }, example.value))));
        }
    }
    if (stats.top?.length && stats.kind !== "json" && stats.kind !== "list") {
        zone.append(sectionLabel(t("top values"), { truncated: stats.top_truncated }), barRows(stats.top, stats.kind === "bool" ? stats.scanned - stats.nulls : 0));
    }
    return zone;
}
