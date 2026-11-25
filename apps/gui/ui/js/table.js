// DataTable: dense typed grid with sort, resize, reorder, pin, copy, and inspect.
import { icon } from "./icons.js";
import { t } from "./i18n.js";
import { el, attachTooltip, copyText, previewValue, showMenu } from "./ui.js";
const NUMERIC = /^(int|integer|float|double|number|timestamp)/i;
const IDENT = /^(identifier)/i;
export function dtypeName(dtype) {
    if (!dtype)
        return "";
    if (typeof dtype === "string")
        return dtype;
    return dtype.identifier || dtype.type || "";
}
function cellClass(dtypeId, fieldName) {
    if (fieldName === "object_id" || IDENT.test(dtypeId))
        return "cell-id";
    if (NUMERIC.test(dtypeId))
        return "cell-num";
    if (/^bool/i.test(dtypeId))
        return "cell-bool";
    return "";
}
function renderCell(value, dtypeId, _fieldName) {
    if (value == null)
        return el("span.cell-null", {}, "null");
    if (typeof value === "boolean" || /^bool/i.test(dtypeId))
        return el("span.cell-bool", {}, String(value));
    if (Array.isArray(value)) {
        const isVector = /^vector/i.test(dtypeId) || value.every((item) => typeof item === "number");
        const shown = isVector ? 4 : 5;
        const head = value
            .slice(0, shown)
            .map((item) => previewValue(item, 12))
            .join(", ");
        return el("span.cell-preview", {}, el("span.brace", {}, "["), head, value.length > shown ? el("span.brace", {}, ", …") : null, el("span.brace", {}, "]"), el("span.cell-dim", {}, `${value.length}`));
    }
    if (typeof value === "object")
        return el("span.cell-preview", {}, previewValue(value, 100));
    if (typeof value === "number")
        return el("span", {}, Number.isInteger(value) ? String(value) : value.toPrecision(6).replace(/\.?0+$/, ""));
    return document.createTextNode(String(value));
}
// options: {columns: [{name, dtypeId, tooltip()}], onSort(name, dir), sort: {name, dir},
//           onRowClick(row), onOrderChange(names), pinned: Set, onPinChange(set),
//           widths: {name: px}, order: [names]}
// widths/pinned are mutated in place so a caller-owned object survives table rebuilds.
export class DataTable {
    options;
    widths;
    order;
    pinned;
    rows;
    selectedKey;
    node;
    hidden;
    constructor(options) {
        this.options = options;
        this.widths = options.widths || {};
        const known = options.columns.map((column) => column.name);
        const requestedOrder = options.order;
        this.order = requestedOrder
            ? [
                ...requestedOrder.filter((name) => known.includes(name)),
                ...known.filter((name) => !requestedOrder.includes(name)),
            ]
            : known;
        this.pinned = options.pinned || new Set();
        this.rows = [];
        this.selectedKey = null;
        this.node = el("div.table-wrap");
        this.node.setAttribute("role", "region");
        this.node.setAttribute("aria-label", options.label || t("Data table"));
    }
    setColumns(columns) {
        this.options.columns = columns;
        const known = new Set(columns.map((column) => column.name));
        this.order = [
            ...this.order.filter((name) => known.has(name)),
            ...columns.map((c) => c.name).filter((name) => !this.order.includes(name)),
        ];
        this.render();
    }
    setRows(rows) {
        this.rows = [...rows];
        this.render();
    }
    visibleColumns() {
        const byName = Object.fromEntries(this.options.columns.map((column) => [column.name, column]));
        const names = this.order.filter((name) => byName[name] && !this.hidden?.has(name));
        const pinnedFirst = [
            ...names.filter((name) => this.pinned.has(name)),
            ...names.filter((name) => !this.pinned.has(name)),
        ];
        return pinnedFirst.flatMap((name) => (byName[name] ? [byName[name]] : []));
    }
    render() {
        const columns = this.visibleColumns();
        this.node.replaceChildren();
        const thead = el("tr");
        thead.append(el("th.row-num", { scope: "col" })); // header-less row-number gutter
        for (const column of columns)
            thead.append(this.buildHeader(column));
        const body = el("tbody");
        this.rows.forEach((row, index) => {
            body.append(this.buildRow(row, columns, index));
        });
        this.node.append(el("table.grid", {}, el("thead", {}, thead), body));
        // Headers stay visible on an empty page so the entity shape is still inspectable.
        if (!this.rows.length)
            this.node.append(el("div.table-empty", {}, icon("table"), el("span", {}, this.options.emptyText || t("No rows"))));
    }
    buildHeader(column) {
        const sort = this.options.sort;
        const isSorted = sort?.name === column.name;
        const toggleSort = () => {
            const dir = isSorted && sort.dir === "asc" ? "desc" : isSorted && sort.dir === "desc" ? null : "asc";
            this.options.onSort?.(column.name, dir);
        };
        const th = el(`th${this.pinned.has(column.name) ? ".pinned" : ""}`, {
            scope: "col",
            tabindex: 0,
            "aria-sort": isSorted ? (sort.dir === "asc" ? "ascending" : "descending") : "none",
            draggable: true,
            style: this.widths[column.name]
                ? { width: `${this.widths[column.name]}px`, maxWidth: `${this.widths[column.name]}px` }
                : null,
            onclick: (event) => {
                if (event.target instanceof Element && event.target.closest(".resize-handle"))
                    return;
                toggleSort();
            },
            onkeydown: (event) => {
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    toggleSort();
                }
                else if (event.key === "F10" && event.shiftKey) {
                    event.preventDefault();
                    this.headerMenu(th, column);
                }
                else if (event.altKey && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
                    event.preventDefault();
                    this.moveColumnBy(column.name, event.key === "ArrowLeft" ? -1 : 1);
                }
            },
            oncontextmenu: (event) => {
                event.preventDefault();
                this.headerMenu(th, column);
            },
        }, el("div.th-inner", {}, this.pinned.has(column.name) ? icon("pin") : null, el("span.th-name", {}, column.name), column.dtypeId ? el("span.th-type", {}, column.dtypeId) : null, isSorted ? el("span.sort-mark", {}, icon(sort.dir === "asc" ? "arrowUp" : "arrowDown")) : null), el("div.resize-handle", {
            role: "separator",
            tabindex: 0,
            "aria-label": t("Resize {name}", { name: column.name }),
            "aria-orientation": "vertical",
            "aria-valuemin": "56",
            "aria-valuenow": String(this.widths[column.name] || 120),
            onpointerdown: (event) => this.startResize(event, th, column.name),
            onkeydown: (event) => {
                if (event.key !== "ArrowLeft" && event.key !== "ArrowRight")
                    return;
                event.preventDefault();
                const next = Math.max(56, (this.widths[column.name] || th.offsetWidth) + (event.key === "ArrowLeft" ? -10 : 10));
                this.widths[column.name] = next;
                this.render();
                queueMicrotask(() => this.node.querySelector(`th[data-column="${CSS.escape(column.name)}"] .resize-handle`)?.focus());
            },
        }));
        th.dataset.column = column.name;
        if (column.tooltip)
            attachTooltip(th, column.tooltip);
        th.addEventListener("dragstart", (event) => {
            event.dataTransfer?.setData("text/column", column.name);
            th.classList.add("dragging");
        });
        th.addEventListener("dragend", () => th.classList.remove("dragging"));
        th.addEventListener("dragover", (event) => {
            event.preventDefault();
            th.classList.add("drag-over");
        });
        th.addEventListener("dragleave", () => th.classList.remove("drag-over"));
        th.addEventListener("drop", (event) => {
            event.preventDefault();
            th.classList.remove("drag-over");
            const source = event.dataTransfer?.getData("text/column") ?? "";
            if (source && source !== column.name)
                this.moveColumn(source, column.name);
        });
        return th;
    }
    headerMenu(anchor, column) {
        const index = this.order.indexOf(column.name);
        const items = [
            {
                label: this.pinned.has(column.name) ? t("Unpin column") : t("Pin column"),
                icon: "pin",
                onclick: () => this.togglePin(column.name),
            },
            { label: t("Sort ascending"), icon: "arrowUp", onclick: () => this.options.onSort?.(column.name, "asc") },
            { label: t("Sort descending"), icon: "arrowDown", onclick: () => this.options.onSort?.(column.name, "desc") },
            { sep: true },
        ];
        if (index > 0) {
            items.push({
                label: t("Move column left"),
                icon: "arrowLeft",
                onclick: () => this.moveColumnBy(column.name, -1),
            });
        }
        if (index < this.order.length - 1) {
            items.push({
                label: t("Move column right"),
                icon: "arrowRight",
                onclick: () => this.moveColumnBy(column.name, 1),
            });
        }
        items.push({ label: t("Copy column name"), icon: "copy", onclick: () => copyText(column.name) });
        if (this.options.headerMenuItems) {
            items.push({ sep: true }, ...this.options.headerMenuItems(column));
        }
        showMenu(anchor, items);
    }
    togglePin(name) {
        if (this.pinned.has(name))
            this.pinned.delete(name);
        else
            this.pinned.add(name);
        this.options.onPinChange?.(this.pinned);
        this.render();
    }
    moveColumn(source, target) {
        const order = this.order.filter((name) => name !== source);
        order.splice(order.indexOf(target), 0, source);
        this.order = order;
        this.options.onOrderChange?.(order);
        this.render();
    }
    moveColumnBy(name, delta) {
        const index = this.order.indexOf(name);
        const target = Math.max(0, Math.min(this.order.length - 1, index + delta));
        if (index < 0 || target === index)
            return;
        const order = [...this.order];
        order.splice(index, 1);
        order.splice(target, 0, name);
        this.order = order;
        this.options.onOrderChange?.(order);
        this.render();
        queueMicrotask(() => this.node.querySelector(`th[data-column="${CSS.escape(name)}"]`)?.focus());
    }
    startResize(event, th, name) {
        event.preventDefault();
        event.stopPropagation();
        const handle = event.currentTarget;
        if (!(handle instanceof Element))
            return;
        const startX = event.clientX;
        const startW = th.offsetWidth;
        // Pointer capture keeps the drag alive outside the handle and guarantees release
        // (pointerup/pointercancel) even when the pointer leaves the window mid-drag.
        try {
            handle.setPointerCapture(event.pointerId);
        }
        catch {
            // synthetic events carry no active pointer; the move/up listeners still work
        }
        const move = (ev) => {
            if (!(ev instanceof PointerEvent))
                return;
            this.widths[name] = Math.max(56, startW + ev.clientX - startX);
            th.style.width = `${this.widths[name]}px`;
            th.style.maxWidth = `${this.widths[name]}px`;
        };
        const up = () => {
            handle.removeEventListener("pointermove", move);
            handle.removeEventListener("pointerup", up);
            handle.removeEventListener("pointercancel", up);
            this.render();
        };
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", up);
        handle.addEventListener("pointercancel", up);
    }
    buildRow(row, columns, index) {
        const key = String(row.object_id ?? JSON.stringify(row));
        const tr = el(`tr${key === this.selectedKey ? ".selected" : ""}`);
        tr.append(el("td.row-num", {}, String((this.options.rowOffset || 0) + index + 1)));
        for (const column of columns) {
            const value = row[column.name];
            const td = el(`td${this.pinned.has(column.name) ? ".pinned" : ""}${column.cls ? `.${column.cls}` : ""}`, {
                tabindex: 0,
                style: this.widths[column.name]
                    ? { width: `${this.widths[column.name]}px`, maxWidth: `${this.widths[column.name]}px` }
                    : null,
                onclick: () => {
                    this.selectedKey = key;
                    for (const selected of tr.parentElement?.querySelectorAll("tr.selected") || [])
                        selected.classList.remove("selected");
                    tr.classList.add("selected");
                    this.options.onRowClick?.(row, column.name);
                },
                ondblclick: () => {
                    copyText(typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? ""), t("Copied {name}", { name: column.name }));
                    td.classList.remove("copy-flash");
                    void td.offsetWidth;
                    td.classList.add("copy-flash");
                },
                onkeydown: (event) => {
                    if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        td.click();
                    }
                    else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "c") {
                        event.preventDefault();
                        copyText(typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? ""), t("Copied {name}", { name: column.name }));
                    }
                },
                title: value == null ? "" : previewValue(value, 200),
            }, renderCell(value, column.dtypeId || "", column.name));
            td.classList.toggle("cell-num", NUMERIC.test(column.dtypeId || "") && typeof value === "number");
            if (column.name === "object_id" || IDENT.test(column.dtypeId || ""))
                td.classList.add("cell-id");
            tr.append(td);
        }
        return tr;
    }
}
export { cellClass, renderCell };
