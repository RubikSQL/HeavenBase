// Data browser: workspace picker, entity tree, typed row tables, schema view, and inspector.

import {
  api,
  type ColumnStatsResponse,
  type EntitiesResponse,
  type EntityRowsResponse,
  type EntitySchemaResponse,
  type JsonRecord,
  type JsonValue,
  type WorkspacesResponse,
} from "./api.js";
import { icon } from "./icons.js";
import { t } from "./i18n.js";
import {
  el,
  iconBtn,
  select,
  toast,
  debounce,
  fmtCount,
  fullValue,
  inlineValue,
  copyText,
  showMenu,
  attachTooltip,
  modal,
  setBusy,
  type UiElement,
} from "./ui.js";
import { jsonView } from "./json_view.js";
import { buildStatsBody } from "./col_stats.js";
import { DataTable, dtypeName, type SortDirection, type TableRow } from "./table.js";
import { parseTypedScalar, statsSeparatorValues } from "./gui-logic.js";
import type { DashboardState, EntityOpenOptions } from "./types.js";

const PAGE_SIZES = [25, 50, 100, 250, 500] as const;

type WorkspaceCard = WorkspacesResponse[number];
type EntityCard = EntitiesResponse[number];
type TabMode = "data" | "schema";

interface EntityField {
  dtype?: string | Readonly<Record<string, unknown>> | undefined;
  desc?: string | undefined;
  pk?: boolean | undefined;
  required?: boolean | undefined;
  has_default?: boolean | undefined;
  default?: JsonValue | undefined;
}

interface EntitySchema {
  desc?: string | undefined;
  fields: Record<string, EntityField>;
}

interface StorageBinding {
  backend?: string | undefined;
  type?: string | undefined;
  strategy?: string | undefined;
}

interface BackendMetadata {
  subject_id?: string | undefined;
  name?: string | undefined;
  type?: string | undefined;
  meta?: { capabilities?: JsonRecord | undefined } | undefined;
}

interface FilterState {
  field: string;
  op: string;
  value: JsonValue;
}

interface SortState {
  name: string;
  dir: Exclude<SortDirection, null>;
}

interface TabState {
  entityId: string;
  mode: TabMode;
  modeTouched: boolean;
  schema: EntitySchema | null;
  storage: Record<string, StorageBinding[]>;
  capabilities: Record<string, unknown>;
  backends: BackendMetadata[];
  sort: SortState | null;
  filters: FilterState[];
  offset: number;
  limit: number;
  hidden: Set<string>;
  pinned: Set<string>;
  widths: Record<string, number>;
  order: string[] | undefined;
  page: EntityRowsResponse | null;
  loading: boolean;
  seq: number;
}

interface StatsSelection {
  entityId: string;
  field: string;
  data: ColumnStatsResponse;
}

interface LocalState {
  workspaces: WorkspaceCard[];
  entities: EntityCard[];
  showSystem: boolean;
  treeFilter: string;
  tabs: Map<string, TabState>;
  activeTab: string | null;
  collapsed: Set<string>;
  autoOpened: boolean;
  stats: StatsSelection | null;
  statsCollapsed: boolean;
  statsSeq: number;
  switchingWorkspace: boolean;
}

const local: LocalState = {
  workspaces: [],
  entities: [],
  showSystem: false,
  treeFilter: "",
  tabs: new Map<string, TabState>(),
  activeTab: null,
  collapsed: new Set<string>(),
  autoOpened: false,
  stats: null, // { entityId, field, data } shown in the bottom-left stats panel
  statsCollapsed: localStorage.getItem("hb-stats-open") !== "1",
  statsSeq: 0,
  switchingWorkspace: false,
};

function requiredElement(id: string): UiElement {
  const node = document.getElementById(id);
  if (!(node instanceof HTMLElement)) throw new Error(`dashboard shell is missing #${id}`);
  return node as UiElement;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function normalizeSchema(info: EntitySchemaResponse): {
  schema: EntitySchema;
  storage: Record<string, StorageBinding[]>;
  capabilities: Record<string, unknown>;
  backends: BackendMetadata[];
} {
  const schemaRecord = record(info.schema);
  const fields = Object.fromEntries(
    Object.entries(record(schemaRecord.fields)).map(([name, raw]) => {
      const field = record(raw);
      return [
        name,
        {
          ...(typeof field.dtype === "string" || (typeof field.dtype === "object" && field.dtype !== null)
            ? { dtype: field.dtype as string | Readonly<Record<string, unknown>> }
            : {}),
          ...(optionalString(field.desc) === undefined ? {} : { desc: optionalString(field.desc) }),
          ...(typeof field.pk === "boolean" ? { pk: field.pk } : {}),
          ...(typeof field.required === "boolean" ? { required: field.required } : {}),
          ...(typeof field.has_default === "boolean" ? { has_default: field.has_default } : {}),
          ...(field.default === undefined ? {} : { default: field.default as JsonValue }),
        },
      ];
    }),
  );
  const storage = Object.fromEntries(
    Object.entries(info.storage).map(([name, values]) => [
      name,
      values.map((value) => {
        const binding = record(value);
        return {
          ...(optionalString(binding.backend) === undefined ? {} : { backend: optionalString(binding.backend) }),
          ...(optionalString(binding.type) === undefined ? {} : { type: optionalString(binding.type) }),
          ...(optionalString(binding.strategy) === undefined ? {} : { strategy: optionalString(binding.strategy) }),
        };
      }),
    ]),
  );
  const backends = info.backends.map((value) => {
    const backend = record(value);
    const meta = record(backend.meta);
    return {
      ...(optionalString(backend.subject_id) === undefined ? {} : { subject_id: optionalString(backend.subject_id) }),
      ...(optionalString(backend.name) === undefined ? {} : { name: optionalString(backend.name) }),
      ...(optionalString(backend.type) === undefined ? {} : { type: optionalString(backend.type) }),
      ...(Object.keys(meta).length ? { meta: { capabilities: record(meta.capabilities) as JsonRecord } } : {}),
    };
  });
  return {
    schema: {
      ...(optionalString(schemaRecord.desc) === undefined ? {} : { desc: optionalString(schemaRecord.desc) }),
      fields,
    },
    storage,
    capabilities: { ...info.capabilities },
    backends,
  };
}

const STATS_MIN_H = 40; // the panel head stays visible even at the smallest size
const STATS_TREE_MIN_H = 24; // one entity row the tree keeps when the stats panel grows

const STATS_OPEN_H = 220; // readable height for a panel that actually has stats in it

window.addEventListener("hb-browser-resize", () => {
  const column = document.getElementById("browser");
  if (column instanceof HTMLElement && column.querySelector(".stats-panel")) fitStatsPanel(column as UiElement);
});

// An empty stats panel is a one-line hint, so it starts at head height and the
// entity tree keeps the room. Loading stats opens it to STATS_OPEN_H; either way
// a height the user set wins.
function statsHeight(): number {
  const stored = parseInt(localStorage.getItem("hb-stats-h") || "", 10);
  if (Number.isFinite(stored)) return Math.max(STATS_MIN_H, stored);
  return local.stats ? STATS_OPEN_H : STATS_MIN_H;
}

// Measure the browser column rather than the viewport: in the stacked narrow
// layout the column is a short band, so a viewport-derived bound collapses the
// drag range to one value and the splitter stops following the pointer.
function clampStatsHeight(height: number, column: UiElement | null): number {
  if (!column) return Math.round(Math.max(STATS_MIN_H, height));
  const box = column.getBoundingClientRect();
  const foot = column.querySelector(".panel-foot");
  // The splitters sit between the tree and the panel and below it; leaving them out
  // of the budget is what let the panel overrun the tree's minimum by their height.
  const splitters = [...column.querySelectorAll(".stats-splitter")].reduce(
    (total, node) => total + node.offsetHeight,
    0,
  );
  const ceiling = treeTop(column) + STATS_TREE_MIN_H + splitters;
  const max = Math.max(0, box.bottom - (foot ? foot.getBoundingClientRect().height : 0) - ceiling);
  return Math.round(Math.max(Math.min(STATS_MIN_H, max), Math.min(max, height)));
}

function treeTop(column: UiElement): number {
  const tree = column.querySelector(".panel-body");
  return (tree || column).getBoundingClientRect().top;
}

function stackedLayout(): boolean {
  return window.matchMedia("(max-width: 760px)").matches;
}

// Stacked: the splitter marks where the entity tree ends, and the stats panel
// keeps the height it has. Everything below the grabbed line — the panel, the
// foot, the band's own bottom edge — therefore moves with it as one block, which
// is what dragging that line looks like it should do.
function setBandHeight(height: number, column: UiElement): number {
  const panel = column.querySelector(".stats-panel");
  const foot = column.querySelector(".panel-foot");
  const splitters = [...column.querySelectorAll(".stats-splitter")].reduce(
    (total, node) => total + node.offsetHeight,
    0,
  );
  const chrome = treeTop(column) - column.getBoundingClientRect().top;
  const floor =
    chrome + STATS_TREE_MIN_H + splitters + (panel ? panel.offsetHeight : 0) + (foot ? foot.offsetHeight : 0);
  const bounded = Math.min(Math.round(window.innerHeight * 0.8), Math.max(floor, Math.round(height)));
  document.documentElement.style.setProperty("--browser-h", `${bounded}px`);
  return bounded;
}

function syncStatsSeparators(column: UiElement): void {
  const tree = column.querySelector(".panel-body");
  const panel = column.querySelector(".stats-panel");
  const splitter = column.querySelector(".stats-splitter:not(.stats-splitter-foot)");
  if (!tree || !panel || !splitter) return;
  const treeHeight = Math.round(tree.offsetHeight);
  const panelHeight = Math.round(panel.offsetHeight);
  const values = statsSeparatorValues({
    stacked: stackedLayout(),
    viewportHeight: window.innerHeight,
    columnHeight: column.offsetHeight,
    treeHeight,
    panelHeight,
    treeMin: STATS_TREE_MIN_H,
    panelMin: STATS_MIN_H,
  });
  splitter.setAttribute("aria-valuemin", String(values.tree.min));
  splitter.setAttribute("aria-valuemax", String(values.tree.max));
  splitter.setAttribute("aria-valuenow", String(values.tree.now));
  const footSplitter = column.querySelector(".stats-splitter-foot");
  if (footSplitter) {
    footSplitter.setAttribute("aria-valuemin", String(values.panel.min));
    footSplitter.setAttribute("aria-valuemax", String(values.panel.max));
    footSplitter.setAttribute("aria-valuenow", String(values.panel.now));
  }
}

// ------------------------------------------------------------- workspace

function clearWorkspaceState(state: DashboardState): void {
  local.entities = [];
  local.tabs.clear();
  local.activeTab = null;
  local.stats = null;
  local.statsSeq += 1;
  state.closeInspector();
  state.health = null;
  state.wsId = null;
  state.setAssistantScope(null);
}

export async function openWorkspace(state: DashboardState, wsId: string): Promise<void> {
  if (local.switchingWorkspace) throw new Error(t("A workspace switch is already in progress"));
  local.switchingWorkspace = true;
  // Block the shell behind the busy overlay until the workspace is fully
  // loaded — half-open state (old tabs against a new backend) is never
  // interactive, at boot or on any later switch.
  setBusy(true);
  try {
    const health = await api.openWorkspace(wsId);
    // The workspace boundary is atomic from the user's perspective: no tabs,
    // inspector, or statistics from the previous workspace survive it.
    clearWorkspaceState(state);
    state.health = health;
    state.wsId = health.ws_id;
    state.setAssistantScope(state.wsId);
    state.refreshStatus();
    renderBrowser(requiredElement("browser"), state);
    renderMain(requiredElement("workspace"), state);
    try {
      local.entities = [...(await api.entities({ system: 1 }))];
    } catch (error) {
      toast(errorMessage(error), "error");
      local.entities = [];
    }
    // The renders above ran against an empty list, so the tree — and the band
    // fitted to it — only reflect the workspace once the entities are in.
    renderBrowser(requiredElement("browser"), state);
  } finally {
    local.switchingWorkspace = false;
    setBusy(false);
  }
}

function confirmRemoveWorkspace(state: DashboardState, spec: WorkspaceCard): void {
  modal({
    title: t("Remove workspace?"),
    body: el(
      "div",
      {},
      el("div", {}, t("Remove {name} from the workspace list?", { name: spec.id })),
      el(
        "div",
        { style: { marginTop: "8px", color: "var(--muted)" } },
        t("This only unregisters the workspace. Its backing database is not deleted."),
      ),
      spec.open
        ? el(
            "div",
            { style: { marginTop: "8px", color: "var(--muted)" } },
            t("The currently open workspace will close."),
          )
        : null,
    ),
    actions: [
      { label: t("Cancel") },
      {
        label: t("Remove"),
        danger: true,
        onclick: async () => {
          try {
            const result = await api.removeWorkspace(spec.id);
            if (result.was_open) {
              clearWorkspaceState(state);
              local.autoOpened = true;
              state.refreshStatus();
              renderMain(requiredElement("workspace"), state);
            }
            renderBrowser(requiredElement("browser"), state);
            toast(
              result.removed
                ? t("Removed {name}", { name: spec.id })
                : t("Workspace {name} was already removed", { name: spec.id }),
            );
          } catch (error) {
            toast(errorMessage(error), "error");
            return false;
          }
          return undefined;
        },
      },
    ],
  });
}

async function refreshEntities(state: DashboardState): Promise<void> {
  local.entities = [...(await api.entities({ system: 1 }))];
  renderBrowser(requiredElement("browser"), state);
}

// Palette/assistant entry: open (or focus) one entity tab by id.
// options.mode forces the Data/Schema view; options.objectId jumps to one
// record by pinning an object_id filter.
export async function openEntityById(
  state: DashboardState,
  entityId: string,
  options: EntityOpenOptions = {},
): Promise<void> {
  const tab = tabState(entityId);
  if (options.mode) {
    tab.mode = options.mode;
    tab.modeTouched = true;
  }
  if (options.objectId) {
    tab.mode = "data";
    tab.modeTouched = true;
    tab.filters = [{ field: "object_id", op: "$eq", value: options.objectId }];
    tab.offset = 0;
  }
  const card = local.entities.find((item) => item.entity_id === entityId);
  await openTab(state, card || { entity_id: entityId });
}

// Screen context handed to the assistant: enough to answer "what am I looking at".
export function assistContext(): JsonRecord {
  const tab = local.activeTab ? local.tabs.get(local.activeTab) : null;
  const context: Record<string, JsonValue> = { route: "data" };
  if (local.activeTab) context.entity = local.activeTab;
  if (tab?.schema) {
    context.schema = {
      desc: tab.schema.desc,
      fields: Object.fromEntries(
        Object.entries(tab.schema.fields).map(([name, field]) => [name, dtypeName(field.dtype)]),
      ),
    };
  }
  return context;
}

// --------------------------------------------------------------- browser

export function renderBrowser(container: UiElement, state: DashboardState): void {
  if (state.route !== "data") return; // a late async callback must not paint over another view
  const head = el(
    "div.panel-head",
    {},
    el("span.title", {}, t("Data")),
    iconBtn("refresh", t("Refresh entities"), () => refreshEntities(state).catch((e) => toast(e.message, "error"))),
  );
  const wsSelect = el("div", { style: { padding: "0 10px 6px" } });
  api
    .workspaces()
    .then(async (specs) => {
      local.workspaces = [...specs];
      // Arriving on the data view without an open workspace (e.g. the page booted on #/config):
      // resume only a server-selected workspace. An arbitrary alphabetically
      // first registration may depend on unavailable external backends; users
      // with no valid active/default selection get the explicit picker instead.
      // boot() may already be opening one, so never race an in-flight switch.
      const target = specs.find((spec) => spec.open) || specs.find((spec) => spec.active);
      if (!state.wsId && target && !local.autoOpened && !local.switchingWorkspace) {
        local.autoOpened = true;
        try {
          await openWorkspace(state, target.id);
          renderBrowser(container, state);
          renderMain(requiredElement("workspace"), state);
          return;
        } catch (error) {
          toast(errorMessage(error), "error");
        }
      }
      // No auto-open happened (no workspaces, or one is already open): nothing
      // left to wait for, so the boot overlay can lift — unless a switch is
      // still in flight, which owns the overlay and clears it in its finally.
      if (!local.switchingWorkspace) setBusy(false);
      wsSelect.replaceChildren(
        select({
          value: state.wsId ?? "",
          width: "100%",
          label: t("Workspace"),
          placeholder: { label: t("Open workspace…"), icon: "plus" },
          options: specs.map((spec) => ({
            value: spec.id,
            label: spec.id,
            ...(spec.active ? { hint: t("active") } : {}),
            icon: "database",
            trailingAction: {
              label: t("Remove {name}", { name: spec.id }),
              icon: "trash",
              danger: true,
              onclick: () => confirmRemoveWorkspace(state, spec),
            },
          })),
          // Rethrow after the toast so the select control rolls its label back to the active workspace.
          onchange: async (wsId) => {
            try {
              await openWorkspace(state, String(wsId));
              renderBrowser(container, state);
              renderMain(requiredElement("workspace"), state);
            } catch (error) {
              toast(errorMessage(error), "error");
              throw error;
            }
          },
        }),
      );
      if (!specs.length)
        wsSelect.replaceChildren(
          el(
            "div",
            { style: { color: "var(--muted)", fontSize: "11px" } },
            t("No registered workspaces — run hb setup"),
          ),
        );
      if (wsSelect.parentElement === container) fitStatsPanel(container);
    })
    .catch((error) => {
      toast(errorMessage(error), "error");
      if (!local.switchingWorkspace) setBusy(false);
    });

  const searchInput = el("input.input", {
    placeholder: t("Filter entities"),
    value: local.treeFilter,
    "aria-label": t("Filter entities"),
    oninput: debounce((event) => {
      if (!(event.target instanceof HTMLInputElement)) return;
      local.treeFilter = event.target.value.trim().toLowerCase();
      renderTree(tree, state);
    }, 150),
  });
  const tree = el("div.panel-body");
  renderTree(tree, state);

  const systemToggle = el("button.toggle", {
    role: "switch",
    "aria-checked": String(local.showSystem),
    "aria-label": t("System entities"),
    onclick: () => {
      local.showSystem = !local.showSystem;
      systemToggle.setAttribute("aria-checked", String(local.showSystem));
      renderTree(tree, state);
    },
  });
  // The count mirrors the tree: visible entities first, total (with system
  // entities) second — a bare total next to the toggle read as if hidden
  // system entities were being listed.
  const visibleCount = local.entities.filter((card) => local.showSystem || !card.system).length;
  const countChip = el(
    "span.mono",
    { title: t("{visible} of {total} entities shown", { visible: visibleCount, total: local.entities.length }) },
    visibleCount === local.entities.length ? String(visibleCount) : `${visibleCount} / ${local.entities.length}`,
  );
  container.replaceChildren(
    head,
    wsSelect,
    el("div", { style: { padding: "0 10px 6px" } }, el("div.search-box", {}, icon("search"), searchInput)),
    tree,
    ...buildStatsZone(state),
    el(
      "div.panel-foot",
      {},
      systemToggle,
      el("span", {}, t("System entities")),
      el("span", { style: { flex: 1 } }),
      countChip,
    ),
  );
  fitStatsPanel(container); // the column can only be measured once every sibling is attached
}

// Re-apply the stored stats height against the column as it now stands, so a
// height stored in a tall window does not stay oversized in a short one.
function fitStatsPanel(column: UiElement): void {
  const panel = column.querySelector(".stats-panel");
  if (!panel?.style.height) return;
  // Two passes: the first resize moves the tree, which is what the bound is measured against.
  for (let pass = 0; pass < 2; pass += 1)
    panel.style.height = `${clampStatsHeight(parseInt(panel.style.height, 10), column)}px`;
  syncStatsSeparators(column);
}

// -------------------------------------------------------- column stats panel

async function loadColumnStats(state: DashboardState, tab: TabState, name: string, force = false): Promise<void> {
  if (!force && local.stats && local.stats.entityId === tab.entityId && local.stats.field === name) return;
  const seq = ++local.statsSeq;
  try {
    const data = await api.columnStats(tab.entityId, name);
    if (seq !== local.statsSeq || state.wsId !== state.health?.ws_id || !local.tabs.has(tab.entityId)) return;
    local.stats = { entityId: tab.entityId, field: name, data };
    local.statsCollapsed = false;
    localStorage.setItem("hb-stats-open", "1");
    // Asking for stats opens the panel: the empty panel sits at head height so the
    // entity tree keeps the room, and a height stored from that state would leave
    // the stats with nowhere to render.
    const column = requiredElement("browser");
    const wasH = column.querySelector(".stats-panel")?.offsetHeight ?? STATS_MIN_H;
    const wasBand = column?.offsetHeight ?? 0;
    if (statsHeight() < STATS_OPEN_H) localStorage.setItem("hb-stats-h", String(STATS_OPEN_H));
    renderBrowser(column, state);
    // The panel appears in addition to the tree, so its new height comes out of the
    // band rather than out of the entity list, which would otherwise collapse.
    const panel = column?.querySelector(".stats-panel");
    if (panel && stackedLayout() && wasH < STATS_OPEN_H) {
      resizeStatsFromFoot(panel, column, STATS_OPEN_H - wasH, { startH: wasH, startBand: wasBand });
    }
  } catch (error) {
    toast(errorMessage(error), "error");
  }
}

// Bottom-left panel below the entity tree: header (collapse/expand) + stats
// body, with a draggable horizontal splitter above it for sizing.
function buildStatsZone(state: DashboardState): UiElement[] {
  const collapsed = local.statsCollapsed;
  const panel = el("div.stats-panel");
  const rerender = (): void => renderBrowser(requiredElement("browser"), state);

  const head = el(
    "div.stats-head",
    {},
    el(
      "button.stats-head-main",
      {
        type: "button",
        "aria-expanded": String(!collapsed),
        onclick: () => {
          local.statsCollapsed = !local.statsCollapsed;
          localStorage.setItem("hb-stats-open", local.statsCollapsed ? "0" : "1");
          rerender();
        },
      },
      icon("chart"),
      el("span", {}, t("Column stats")),
      local.stats
        ? el("span.mono", { style: { color: "var(--subtle)" } }, `${local.stats.entityId}.${local.stats.field}`)
        : null,
      el("span.spacer"),
      el(
        "span.stats-chevron",
        { "aria-hidden": "true", style: { transform: collapsed ? "rotate(-90deg)" : "none" } },
        icon("chevronDown"),
      ),
    ),
  );
  panel.append(head);

  if (!collapsed) {
    const body = el("div.stats-body");
    if (local.stats) body.append(buildStatsBody(local.stats.data));
    else
      body.append(
        el("div.palette-hint", { style: { padding: "6px 0" } }, t("Right-click a column header → Column stats")),
      );
    panel.append(body);
    panel.style.height = `${statsHeight()}px`;
  }

  const splitter = el("div.stats-splitter", {
    role: "separator",
    tabindex: 0,
    "aria-orientation": "horizontal",
    "aria-label": t("Resize stats panel"),
    "aria-valuemin": String(STATS_TREE_MIN_H),
    "aria-valuenow": String(STATS_TREE_MIN_H),
    onkeydown: (event) => {
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
      event.preventDefault();
      const column = panel.parentElement;
      if (!(column instanceof HTMLElement)) return;
      const browserColumn = column as UiElement;
      if (stackedLayout()) {
        setBandHeight(browserColumn.offsetHeight + (event.key === "ArrowDown" ? 10 : -10), browserColumn);
      } else {
        const wanted = (panel.offsetHeight || statsHeight()) + (event.key === "ArrowUp" ? 10 : -10);
        const height = clampStatsHeight(wanted, browserColumn);
        panel.style.height = `${height}px`;
        localStorage.setItem("hb-stats-h", String(height));
      }
      syncStatsSeparators(browserColumn);
    },
    onpointerdown: (event) => {
      if (collapsed) return;
      event.preventDefault();
      // Track the pointer as a delta from where the drag started, so the grabbed
      // edge is the edge that moves. Stacked, the whole block below the line rides
      // along on the band; side-by-side the column is already full height, so the
      // drag redistributes it between the tree and the panel instead.
      const startY = event.clientY;
      const startH = panel.offsetHeight;
      const column = panel.parentElement;
      if (!(column instanceof HTMLElement)) return;
      const browserColumn = column as UiElement;
      const startBand = browserColumn.offsetHeight;
      const stacked = stackedLayout();
      let height = startH;
      const move = (ev: PointerEvent): void => {
        if (stacked) {
          setBandHeight(startBand + (ev.clientY - startY), browserColumn);
          syncStatsSeparators(browserColumn);
          return;
        }
        height = clampStatsHeight(startH + (startY - ev.clientY), browserColumn);
        panel.style.height = `${height}px`;
        syncStatsSeparators(browserColumn);
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        window.removeEventListener("pointercancel", up);
        localStorage.setItem("hb-stats-h", String(Math.round(height)));
        syncStatsSeparators(browserColumn);
      };
      // Listen on the window, not the 9px handle: pointer capture is refused for
      // pointers the browser does not consider active, and without it the very
      // first move off the handle would end the drag.
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      window.addEventListener("pointercancel", up);
    },
  });

  // Bottom edge of the panel. Its DOM remains stable across the responsive
  // breakpoint; CSS exposes it only in the stacked layout, where the band can
  // grow underneath it.
  const footSplitter = el("div.stats-splitter.stats-splitter-foot", {
    role: "separator",
    tabindex: 0,
    "aria-orientation": "horizontal",
    "aria-label": t("Resize stats panel bottom edge"),
    "aria-valuemin": String(STATS_MIN_H),
    "aria-valuenow": String(Math.round(panel.offsetHeight || statsHeight())),
    onkeydown: (event) => {
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
      event.preventDefault();
      const column = panel.parentElement;
      if (!(column instanceof HTMLElement)) return;
      resizeStatsFromFoot(panel, column as UiElement, event.key === "ArrowDown" ? 10 : -10);
    },
    onpointerdown: (event) => {
      event.preventDefault();
      const column = panel.parentElement;
      if (!(column instanceof HTMLElement)) return;
      const browserColumn = column as UiElement;
      const startY = event.clientY;
      const startH = panel.offsetHeight;
      const startBand = browserColumn.offsetHeight;
      let height = startH;
      const move = (ev: PointerEvent): void => {
        height = resizeStatsFromFoot(panel, browserColumn, ev.clientY - startY, { startH, startBand });
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        window.removeEventListener("pointercancel", up);
        localStorage.setItem("hb-stats-h", String(Math.round(height)));
        syncStatsSeparators(browserColumn);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      window.addEventListener("pointercancel", up);
    },
  });

  if (collapsed) return [panel];
  return [splitter, panel, footSplitter];
}

// Drag the panel's bottom edge: the panel takes the delta and the band grows with
// it, so the foot below follows the grabbed line instead of the panel eating the
// entity tree above.
function resizeStatsFromFoot(
  panel: UiElement,
  column: UiElement,
  delta: number,
  start: { startH?: number; startBand?: number } = {},
): number {
  const startH = start.startH ?? panel.offsetHeight;
  const startBand = start.startBand ?? column.offsetHeight;
  const wanted = Math.max(STATS_MIN_H, Math.round(startH + delta));
  panel.style.height = `${wanted}px`; // before the band, so its floor is measured against the new panel
  setBandHeight(startBand + (wanted - startH), column);
  const height = clampStatsHeight(wanted, column);
  panel.style.height = `${height}px`;
  localStorage.setItem("hb-stats-h", String(height));
  syncStatsSeparators(column);
  return height;
}

function renderTree(tree: UiElement, state: DashboardState): void {
  const visible = local.entities
    .filter((card) => local.showSystem || !card.system)
    .filter(
      (card) =>
        !local.treeFilter ||
        card.entity_id.includes(local.treeFilter) ||
        (card.name || "").toLowerCase().includes(local.treeFilter),
    );
  const groups = new Map<string, EntityCard[]>();
  for (const card of visible) {
    const group = card.system
      ? `system · ${card.extension || "core"}`
      : card.extension && card.extension !== "system"
        ? card.extension
        : "entities";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group)?.push(card);
  }
  tree.replaceChildren();
  if (!visible.length) {
    tree.append(
      el(
        "div",
        { style: { padding: "12px 10px", color: "var(--muted)", fontSize: "12px" } },
        state.wsId ? t("No entities") : t("Open a workspace"),
      ),
    );
    return;
  }
  for (const [group, cards] of [...groups.entries()].sort()) {
    const collapsed = local.collapsed.has(group);
    tree.append(
      el(
        `button.tree-group${collapsed ? ".collapsed" : ""}`,
        {
          onclick: () => {
            local.collapsed.has(group) ? local.collapsed.delete(group) : local.collapsed.add(group);
            renderTree(tree, state);
          },
        },
        icon("chevronDown"),
        el("span", {}, group),
      ),
    );
    if (collapsed) continue;
    for (const card of cards) {
      const item = el(
        `button.tree-item${local.activeTab === card.entity_id ? ".selected" : ""}`,
        { onclick: () => openTab(state, card) },
        icon(card.system ? "box" : "table"),
        el("span.name", {}, card.entity_id),
        card.system ? el("span.sys-mark", {}, t("sys")) : null,
        el("span.count", {}, fmtCount(card.count)),
      );
      if (card.desc)
        attachTooltip(item, () =>
          el("div", {}, el("div.tt-title", {}, card.entity_id), el("div.tt-desc", {}, card.desc)),
        );
      tree.append(item);
    }
  }
  // The entity list arrives after the first paint and this is also the redraw for
  // filtering and the system toggle, so refit here rather than in renderBrowser only.
  if (tree.parentElement instanceof HTMLElement) fitStatsPanel(tree.parentElement as UiElement);
}

// ------------------------------------------------------------------ tabs

function tabState(entityId: string): TabState {
  const existing = local.tabs.get(entityId);
  if (existing) return existing;
  const created: TabState = {
    entityId,
    mode: "data", // "data" | "schema"
    modeTouched: false,
    schema: null,
    storage: {},
    capabilities: {},
    backends: [],
    sort: null,
    filters: [],
    offset: 0,
    limit: 50,
    hidden: new Set(),
    pinned: new Set(["object_id"]),
    widths: {},
    order: undefined,
    page: null,
    loading: false,
    seq: 0,
  };
  local.tabs.set(entityId, created);
  return created;
}

async function openTab(state: DashboardState, card: Pick<EntityCard, "entity_id"> | EntityCard): Promise<void> {
  const tab = tabState(card.entity_id);
  local.activeTab = card.entity_id;
  renderBrowser(requiredElement("browser"), state);
  renderMain(requiredElement("workspace"), state);
  if (!tab.schema) {
    try {
      const info = await api.entitySchema(card.entity_id);
      const normalized = normalizeSchema(info);
      tab.schema = normalized.schema;
      tab.storage = normalized.storage;
      tab.capabilities = normalized.capabilities;
      tab.backends = normalized.backends;
    } catch (error) {
      toast(errorMessage(error), "error");
    }
  }
  const firstLoad = !tab.page;
  await loadRows(state, tab);
  // An empty entity has nothing to show in the grid; open on its schema instead
  // (unless the user already picked a mode for this tab).
  if (firstLoad && !tab.modeTouched && tab.page && tab.page.total === 0) {
    tab.mode = "schema";
    renderMain(requiredElement("workspace"), state);
  }
}

function closeTab(state: DashboardState, entityId: string): void {
  local.tabs.delete(entityId);
  if (local.activeTab === entityId) local.activeTab = local.tabs.keys().next().value ?? null;
  renderBrowser(requiredElement("browser"), state);
  renderMain(requiredElement("workspace"), state);
}

// ------------------------------------------------------------------ rows

function buildFilter(tab: TabState): JsonRecord | null {
  if (!tab.filters.length) return null;
  const parts = tab.filters.map(({ field, op, value }) => ({ [field]: op === "$eq" ? value : { [op]: value } }));
  return parts.length === 1 ? (parts[0] ?? null) : { $and: parts };
}

function buildOrder(tab: TabState): string[] {
  if (!tab.sort) return ["object_id"];
  const primary = tab.sort.dir === "desc" ? `-${tab.sort.name}` : tab.sort.name;
  return tab.sort.name === "object_id" ? [primary] : [primary, "object_id"];
}

async function loadRows(state: DashboardState, tab: TabState): Promise<void> {
  const seq = ++tab.seq; // late responses from an older request must not overwrite newer state
  tab.loading = true;
  renderMain(requiredElement("workspace"), state);
  try {
    const page = await api.entityRows(tab.entityId, {
      filter: buildFilter(tab),
      order: buildOrder(tab),
      offset: tab.offset,
      limit: tab.limit,
    });
    if (seq !== tab.seq) return;
    tab.page = page;
    // Live data can shrink under the current offset; snap back to the last real page.
    if (!page.rows.length && tab.offset > 0 && page.total > 0 && !tab.filters.length) {
      tab.offset = Math.floor(Math.max(0, page.total - 1) / tab.limit) * tab.limit;
      return loadRows(state, tab);
    }
  } catch (error) {
    if (seq !== tab.seq) return;
    toast(errorMessage(error), "error");
    tab.page = tab.page || {
      rows: [],
      columns: [],
      total: 0,
      has_more: false,
      offset: tab.offset,
      limit: tab.limit,
    };
  }
  tab.loading = false;
  renderMain(requiredElement("workspace"), state);
}

// ------------------------------------------------------------------ main

export function renderMain(container: UiElement, state: DashboardState): void {
  if (state.route !== "data") return; // a late async callback (loadRows/openTab) must not paint over another view
  container.replaceChildren();
  const tabsBar = el("div.tabs", { role: "tablist" });
  for (const entityId of local.tabs.keys()) {
    const activate = async () => {
      local.activeTab = entityId;
      renderMain(container, state);
      const tab = tabState(entityId);
      if (!tab.page) await loadRows(state, tab);
    };
    tabsBar.append(
      el(
        `div.tab${entityId === local.activeTab ? ".active" : ""}`,
        {},
        el(
          "button.tab-main",
          {
            type: "button",
            role: "tab",
            tabindex: entityId === local.activeTab ? 0 : -1,
            "aria-selected": String(entityId === local.activeTab),
            onclick: activate,
            onkeydown: (event) => {
              if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
              event.preventDefault();
              const tabs = [...tabsBar.querySelectorAll('[role="tab"]')];
              const index =
                event.currentTarget instanceof HTMLElement ? tabs.indexOf(event.currentTarget as UiElement) : -1;
              if (index < 0) return;
              tabs[(index + (event.key === "ArrowLeft" ? -1 : 1) + tabs.length) % tabs.length]?.focus();
            },
          },
          icon("table"),
          el("span", { style: { overflow: "hidden", textOverflow: "ellipsis" } }, entityId),
        ),
        el(
          "button.close",
          {
            type: "button",
            "aria-label": t("Close {name}", { name: entityId }),
            onclick: () => closeTab(state, entityId),
          },
          icon("x"),
        ),
      ),
    );
  }
  container.append(tabsBar);

  if (!local.activeTab) {
    container.append(
      el(
        "div.view",
        {},
        el(
          "div.empty-state",
          {},
          icon("table"),
          el("span", {}, state.wsId ? t("Select an entity to browse") : t("Open a workspace to browse data")),
          state.wsId ? el("span.empty-hint", {}, t("⌘K to search the workspace")) : null,
        ),
      ),
    );
    return;
  }

  const tab = tabState(local.activeTab);
  const view = el("div.view");
  view.append(buildToolbar(state, tab));
  if (tab.mode === "schema") {
    view.append(buildSchemaView(tab));
  } else {
    const tableZone = el("div", {
      style: { flex: "1", display: "flex", flexDirection: "column", minHeight: "0", position: "relative" },
    });
    if (tab.loading) tableZone.append(el("div.loading-bar"));
    tableZone.append(buildTable(state, tab));
    view.append(tableZone, buildPager(state, tab));
  }
  container.append(view);
}

// --------------------------------------------------------------- toolbar

function fieldOptions(tab: TabState): { value: string; label: string }[] {
  const fields = tab.schema ? Object.keys(tab.schema.fields) : tab.page?.columns || [];
  return fields.map((name) => ({ value: name, label: name }));
}

function opsFor(dtypeId: string): string[] {
  if (/^(int|float|timestamp)/i.test(dtypeId)) return ["$eq", "$ne", "$gt", "$gte", "$lt", "$lte"];
  if (/^bool/i.test(dtypeId)) return ["$eq", "$ne"];
  return ["$contains", "$eq", "$ne", "$in"];
}

function addFilterDialog(state: DashboardState, tab: TabState): void {
  const fields = fieldOptions(tab);
  if (!fields.length) return;
  const initialField = fields[0]?.value;
  if (!initialField) return;
  let field: string = initialField;
  let op = opsFor(fieldDtype(tab, field))[0] ?? "$eq";
  const valueInput = el("input.input", {
    placeholder: t("Value"),
    style: { width: "100%" },
    "aria-label": t("Filter value"),
  });
  // The operator set depends on the field dtype, so the dropdown is rebuilt whenever the field changes.
  const opHolder = el("span", { style: { display: "inline-flex" } });
  const buildOpSelect = () => {
    op = opsFor(fieldDtype(tab, field))[0] ?? "$eq";
    opHolder.replaceChildren(
      select<string>({
        value: op,
        options: opsFor(fieldDtype(tab, field)).map((value) => ({ value, label: value })),
        onchange: (value) => {
          op = value;
        },
      }),
    );
  };
  buildOpSelect();
  const fieldSelect = select<string>({
    value: field,
    options: fields,
    onchange: (value) => {
      field = value;
      buildOpSelect();
    },
  });
  modal({
    title: t("Filter {name}", { name: tab.entityId }),
    body: el(
      "div",
      { style: { display: "flex", gap: "8px", alignItems: "center" } },
      fieldSelect,
      opHolder,
      valueInput,
    ),
    actions: [
      {
        label: t("Apply"),
        primary: true,
        onclick: async () => {
          const raw = valueInput.value;
          if (!raw) return false;
          let value: JsonValue = parseScalar(raw, fieldDtype(tab, field));
          if (op === "$in") value = raw.split(",").map((item) => parseScalar(item.trim(), fieldDtype(tab, field)));
          tab.filters.push({ field, op, value });
          tab.offset = 0;
          await loadRows(state, tab);
          return undefined;
        },
      },
    ],
  });
  setTimeout(() => valueInput.focus(), 30);
}

function fieldDtype(tab: TabState, name: string): string {
  return dtypeName(tab.schema?.fields?.[name]?.dtype) || "";
}

function parseScalar(raw: string, dtypeId: string): string | number | boolean {
  return parseTypedScalar(raw, dtypeId);
}

function buildToolbar(state: DashboardState, tab: TabState): UiElement {
  const chips = tab.filters.map((filter, index) =>
    el(
      "span.filter-chip",
      {},
      `${filter.field} ${filter.op.slice(1)} ${JSON.stringify(filter.value)}`,
      el(
        "button",
        {
          "aria-label": t("Remove filter"),
          onclick: async () => {
            tab.filters.splice(index, 1);
            tab.offset = 0;
            await loadRows(state, tab);
          },
        },
        icon("x"),
      ),
    ),
  );
  const modeBtn = (mode: TabMode, iconName: string, label: string): UiElement =>
    el(
      `button.seg-btn${tab.mode === mode ? ".active" : ""}`,
      {
        title: label,
        "aria-label": label,
        "aria-pressed": String(tab.mode === mode),
        onclick: () => {
          if (tab.mode === mode) return;
          tab.mode = mode;
          tab.modeTouched = true;
          renderMain(requiredElement("workspace"), state);
        },
      },
      icon(iconName),
      el("span", {}, label),
    );
  return el(
    "div.toolbar",
    {},
    el("div.seg", {}, modeBtn("data", "table", t("Data")), modeBtn("schema", "braces", t("Schema"))),
    el("div.sep"),
    tab.mode === "data" ? iconBtn("filter", t("Add filter"), () => addFilterDialog(state, tab)) : null,
    ...(tab.mode === "data" ? chips : []),
    el("div.spacer"),
    tab.mode === "data"
      ? iconBtn("columns", t("Show or hide columns"), (event) => {
          if (event.currentTarget instanceof HTMLElement) {
            columnsMenu(event.currentTarget as UiElement, state, tab);
          }
        })
      : null,
    tab.mode === "data" ? iconBtn("refresh", t("Reload rows"), () => loadRows(state, tab)) : null,
  );
}

function columnsMenu(anchor: UiElement, state: DashboardState, tab: TabState): void {
  // keepOpen: hiding several columns should not force reopening the menu per column.
  showMenu(anchor, () =>
    orderedColumns(tab).map((name) => ({
      label: name,
      icon: tab.hidden.has(name) ? "eyeOff" : "eye",
      selected: !tab.hidden.has(name),
      keepOpen: true,
      onclick: () => {
        tab.hidden.has(name) ? tab.hidden.delete(name) : tab.hidden.add(name);
        renderMain(requiredElement("workspace"), state);
      },
    })),
  );
}

// ----------------------------------------------------------------- table

// Explicit labeled binding line, big to small: "backend type: inmem · instance: main · strategy: inline"
// — mirrors the Config → Storage sections Types | Instances | Strategies one to one.
function storageLabel(store: StorageBinding): string {
  const parts: string[] = [];
  if (store.type) parts.push(`${t("backend type")}: ${store.type}`);
  parts.push(`${t("instance")}: ${store.backend || "?"}`, `${t("strategy")}: ${store.strategy || "?"}`);
  return parts.join(" · ");
}

function capabilitySummary(tab: TabState, name: string): string | null {
  const caps = tab.capabilities?.[name];
  if (!caps) return null;
  if (Array.isArray(caps)) return caps.join(", ");
  if (typeof caps === "object") {
    const on = Object.entries(caps)
      .filter(([, value]) => value)
      .map(([key]) => key);
    return on.length ? on.join(", ") : null;
  }
  return String(caps);
}

function columnTooltip(tab: TabState, name: string): () => UiElement {
  return (): UiElement => {
    const field = tab.schema?.fields?.[name];
    if (!field) return el("div", {}, el("div.tt-title", {}, name));
    const stores = tab.storage?.[name] || [];
    const query = capabilitySummary(tab, name);
    const rows: [string, string][] = [
      [t("dtype"), dtypeName(field.dtype) || "?"],
      [t("required"), String(field.required)],
      ...(field.has_default ? ([[t("default"), JSON.stringify(field.default)]] as [string, string][]) : []),
      ...stores.map((store): [string, string] => [t("storage"), storageLabel(store)]),
      ...(query ? ([[t("query"), query]] as [string, string][]) : []),
    ];
    return el(
      "div",
      {},
      el("div.tt-title", {}, name, field.pk ? el("span.badge.plain", {}, "pk") : null),
      field.desc ? el("div.tt-desc", {}, field.desc) : null,
      rows.map(([key, value]) => el("div.tt-row", {}, el("span.k", {}, key), el("span.v", {}, value))),
    );
  };
}

function orderedColumns(tab: TabState): string[] {
  const names = [...(tab.page?.columns || [])];
  if (!tab.schema) return names;
  const rank: Record<string, number> = Object.fromEntries(
    Object.keys(tab.schema.fields).map((name, index) => [name, index]),
  );
  return [...names].sort((a, b) => (rank[a] ?? 999) - (rank[b] ?? 999));
}

function buildTable(state: DashboardState, tab: TabState): UiElement {
  const names = orderedColumns(tab).filter((name) => !tab.hidden.has(name));
  const table = new DataTable({
    label: `${tab.entityId} rows`,
    emptyText: !tab.page ? t("Loading") : tab.filters.length ? t("No rows match the filter") : t("No rows"),
    columns: names.map((name) => ({ name, dtypeId: fieldDtype(tab, name), tooltip: columnTooltip(tab, name) })),
    ...(tab.sort ? { sort: tab.sort } : {}),
    pinned: tab.pinned,
    widths: tab.widths,
    ...(tab.order ? { order: tab.order } : {}),
    rowOffset: tab.offset,
    onOrderChange: (order) => {
      tab.order = order;
    },
    onSort: async (name, dir) => {
      tab.sort = dir ? { name, dir } : null;
      tab.offset = 0;
      // While the stats panel is open, clicking a header also retargets the stats to that column.
      if (!local.statsCollapsed) loadColumnStats(state, tab, name);
      await loadRows(state, tab);
    },
    onRowClick: (row, columnName) => {
      showRowInspector(state, tab, row);
      // Clicking a cell always shows that column's stats (expands the panel when collapsed).
      if (columnName) loadColumnStats(state, tab, columnName);
    },
    headerMenuItems: (column) => [
      { label: t("Column stats"), icon: "chart", onclick: () => loadColumnStats(state, tab, column.name, true) },
    ],
  });
  table.hidden = tab.hidden;
  table.setRows(tab.page?.rows || []);
  return table.node;
}

// ---------------------------------------------------------------- schema

function buildSchemaView(tab: TabState): UiElement {
  const zone = el("div.schema-view");
  if (!tab.schema) {
    zone.append(el("div.empty-state", {}, icon("braces"), el("span", {}, t("Loading schema"))));
    return zone;
  }
  if (tab.schema.desc) zone.append(el("div.schema-desc", {}, tab.schema.desc));
  const head = el(
    "tr",
    {},
    el("th.row-num", { scope: "col" }), // header-less row-number gutter
    ...["field", "dtype", "flags", "storage", "query", "description"].map((title) =>
      el("th", { scope: "col" }, t(title)),
    ),
  );
  const body = el("tbody");
  Object.entries(tab.schema.fields).forEach(([name, field], index) => {
    const stores = tab.storage?.[name] || [];
    // Storage is a list of binding dictionaries per field. Compact chips keep
    // the schema grid scannable while the full dictionary remains in the title.
    body.append(
      el(
        "tr",
        {},
        el("td.row-num", {}, String(index + 1)),
        el("td.cell-id", {}, name),
        el("td", {}, el("span.th-type", {}, dtypeName(field.dtype))),
        el(
          "td",
          {},
          field.pk ? el("span.badge.plain", {}, "pk") : null,
          field.required ? el("span.badge.plain", {}, t("required")) : null,
          field.has_default ? el("span.badge.plain", { title: JSON.stringify(field.default) }, t("default")) : null,
        ),
        el(
          "td.storage-cell",
          {},
          stores.length
            ? stores.map((store) =>
                el(
                  "span.store-chip",
                  { title: JSON.stringify({ backend: store.backend, type: store.type, strategy: store.strategy }) },
                  `${store.backend || "?"} · ${store.type || "?"} · ${store.strategy || "?"}`,
                ),
              )
            : "—",
        ),
        el("td.cell-preview", {}, capabilitySummary(tab, name) || "—"),
        el("td", { style: { whiteSpace: "normal", maxWidth: "380px" } }, field.desc || ""),
      ),
    );
  });
  zone.append(
    el(
      "div.table-wrap",
      { style: { flex: "none", maxHeight: "55%" } },
      el("table.grid.schema-grid", {}, el("thead", {}, head), body),
    ),
  );

  if (tab.backends?.length) {
    zone.append(el("div.panel-label", {}, t("backends")));
    for (const backend of tab.backends) {
      const capabilities = backend.meta?.capabilities || {};
      zone.append(
        el(
          "div.backend-card",
          {},
          el(
            "div",
            { style: { display: "flex", alignItems: "center", gap: "6px" } },
            icon("server"),
            el("span", { style: { fontWeight: 650 } }, backend.subject_id || backend.name),
            el("span.badge.plain", {}, backend.type || "?"),
          ),
          el(
            "div.backend-caps",
            {},
            Object.entries(capabilities).map(([key, value]) =>
              el(
                "div.tt-row",
                {},
                el("span.k", { style: { minWidth: "170px" } }, key),
                el("span.v", {}, inlineValue(value)),
              ),
            ),
          ),
        ),
      );
    }
  }
  return zone;
}

// ----------------------------------------------------------------- pager

function buildPager(state: DashboardState, tab: TabState): UiElement {
  const page = tab.page || {
    rows: [],
    columns: [],
    total: 0,
    has_more: false,
    offset: tab.offset,
    limit: tab.limit,
  };
  const start = page.rows.length ? tab.offset + 1 : 0;
  const end = tab.offset + page.rows.length;
  const filtered = tab.filters.length > 0;
  const label = filtered
    ? t("{start}–{end}{more} · filtered · {total} total", {
        start,
        end,
        more: page.has_more ? "+" : "",
        total: page.total,
      })
    : t("{start}–{end} / {total}", { start, end, total: page.total });
  const pageCount = Math.max(1, Math.ceil(page.total / tab.limit));
  const currentPage = Math.floor(tab.offset / tab.limit) + 1;
  const lastOffset = (pageCount - 1) * tab.limit;
  const jumpTo = async (raw: string): Promise<void> => {
    const target = Math.min(pageCount, Math.max(1, parseInt(raw, 10) || currentPage));
    if (target === currentPage) return;
    tab.offset = (target - 1) * tab.limit;
    await loadRows(state, tab);
  };
  const pageInput = el("input.input.pager-jump", {
    value: String(currentPage),
    "aria-label": t("Page"),
    title: t("Jump to a page: type the number and press Enter"),
    onchange: (event) => {
      if (event.target instanceof HTMLInputElement) void jumpTo(event.target.value);
    },
    onkeydown: (event) => {
      if (event.key === "Enter" && event.target instanceof HTMLInputElement) {
        void jumpTo(event.target.value);
      }
    },
  });
  // The server counts under the active filter, so the page jump is exact.
  return el(
    "div.pager",
    {},
    el("div.spacer"),
    select({
      compact: true,
      value: tab.limit,
      options: PAGE_SIZES.map((size) => ({ value: size, label: t("{name} rows", { name: size }) })),
      onchange: async (size) => {
        tab.limit = size;
        tab.offset = 0;
        await loadRows(state, tab);
      },
    }),
    el("span.mono", {}, label),
    iconBtn("chevronsLeft", t("First page"), async () => {
      tab.offset = 0;
      await loadRows(state, tab);
    }),
    iconBtn("chevronLeft", t("Previous page"), async () => {
      tab.offset = Math.max(0, tab.offset - tab.limit);
      await loadRows(state, tab);
    }),
    el(
      "span.pager-jump-group",
      {},
      el("span.pager-jump-label", {}, t("Page")),
      pageInput,
      el("span.mono", {}, `/ ${pageCount}`),
    ),
    iconBtn("chevronRight", t("Next page"), async () => {
      if (!page.has_more) return;
      tab.offset += tab.limit;
      await loadRows(state, tab);
    }),
    iconBtn("chevronsRight", t("Last page"), async () => {
      if (tab.offset === lastOffset) return;
      tab.offset = lastOffset;
      await loadRows(state, tab);
    }),
  );
}

// ------------------------------------------------------------- inspector

// Long strings read better as a code block; short scalars stay a plain line.
const LONG_TEXT = 120;

function inspectorValue(value: unknown): UiElement {
  if (value == null) return el("div.fr-value.null", {}, "null");
  if (typeof value === "object") return jsonView(value);
  if (typeof value === "string" && (value.length > LONG_TEXT || value.includes("\n")))
    return el("pre.code-block", {}, value);
  return el("div.fr-value", {}, fullValue(value));
}

function showRowInspector(state: DashboardState, tab: TabState, row: TableRow): void {
  const body = el("div.panel-body", { style: { padding: "0" } });
  const rank: Record<string, number> = tab.schema
    ? Object.fromEntries(Object.keys(tab.schema.fields).map((name, index) => [name, index]))
    : {};
  const entries = Object.entries(row).sort(([a], [b]) => (rank[a] ?? 999) - (rank[b] ?? 999));
  for (const [name, value] of entries) {
    const field = tab.schema?.fields?.[name];
    body.append(
      el(
        "div.field-row",
        {},
        el(
          "div.fr-head",
          {},
          el("span.fr-name", {}, name),
          el("span.fr-type", {}, dtypeName(field?.dtype) || typeof value),
          iconBtn("copy", t("Copy {name}", { name }), () =>
            copyText(
              typeof value === "object" && value !== null ? JSON.stringify(value, null, 2) : String(value ?? ""),
              t("Copied {name}", { name }),
            ),
          ),
        ),
        inspectorValue(value),
      ),
    );
  }
  state.openInspector(body, row.object_id ? `${tab.entityId} · ${row.object_id}` : tab.entityId);
}
