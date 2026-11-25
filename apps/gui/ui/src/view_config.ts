// Config workbench: typed domain tree, entry editors, origins, history and diff.

import { api, type ConfigHistoryResponse, type ConfigMetaResponse } from "./api.js";
import { brandIcon } from "./brand_icons.js";
import { icon } from "./icons.js";
import { t } from "./i18n.js";
import {
  el,
  iconBtn,
  select,
  toast,
  modal,
  copyText,
  debounce,
  SECRET_KEY,
  attachTooltip,
  type UiChild,
  type UiElement,
} from "./ui.js";
import { defaultEnvVariable, parseEnvReference, renameConfigKey } from "./gui-logic.js";
import type { Language } from "./i18n.js";
import type { DashboardState } from "./types.js";

type ConfigOrigin = "builtin" | "override" | "custom";
type ConfigRecord = Record<string, unknown>;

interface DomainSection {
  key: string;
  label: string;
  path: string;
}

interface Domain {
  key: string;
  label: string;
  icon: string;
  collections?: readonly string[] | null;
  sections?: readonly DomainSection[];
  path?: string;
}

interface ConfigSection {
  key: string;
  label?: string;
  path?: string;
  segs: string[];
}

interface FlatRow {
  segs: string[];
  value: unknown;
}

interface SchemaField {
  key: string;
  control: string;
  path?: readonly string[];
}

interface SchemaGroup {
  label: string;
  fields: readonly string[];
  collapsible?: boolean;
}

interface ReferenceOption {
  value: string;
  label: string;
  hint?: string;
}

type SaveValue = (value: unknown) => void | Promise<void>;
type EditorMode = "config" | "json";

interface CodeMirrorEditor {
  focus(): void;
  getValue(): string;
  on(event: "change", callback: (editor: CodeMirrorEditor) => void): void;
  refresh(): void;
  setSize(width: string | null, height: string | null): void;
  setValue(value: string): void;
}

type CodeMirrorConstructor = (host: HTMLElement, options: Readonly<Record<string, unknown>>) => CodeMirrorEditor;

declare global {
  interface Window {
    CodeMirror?: CodeMirrorConstructor;
  }
}

interface LocalState {
  meta: ConfigMetaResponse | null;
  metaLoading: boolean;
  tree: ConfigRecord | null;
  origins: Record<string, ConfigOrigin>;
  scope: string | null;
  selected: string;
  section: Record<string, string>;
  selectedEntry: string | null;
  entryFilter: string;
  collapsed: Set<string>;
  revealed: Set<string>;
  search: string;
  entryMode: Record<string, EditorMode>;
  jsonDrafts: Record<string, string>;
  jsonDirty: Set<string>;
}

function requiredElement(id: string): UiElement {
  const node = document.getElementById(id);
  if (!(node instanceof HTMLElement)) throw new Error(`dashboard shell is missing #${id}`);
  return node as UiElement;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function configRecord(value: unknown): ConfigRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as ConfigRecord) : {};
}

// Curated domain map; unknown top-level keys fall into "Other" automatically.
// A domain lists either `collections` (subtrees of its own path) or composite
// `sections` (each rooted at its own top-level path, e.g. Backends spanning
// backend instances plus per-type parameter defaults).
const DOMAINS: readonly Domain[] = [
  { key: "llm", label: "LLM", icon: "zap", collections: ["providers", "models", "presets", "gateways"] },
  { key: "db", label: "Database", icon: "database", collections: ["providers"] },
  { key: "workspace", label: "Workspace", icon: "layers", collections: ["presets"] },
  {
    key: "storage",
    label: "Storage",
    icon: "server",
    // The storage axis big to small, mirroring the schema storage label
    // "backend type → backend name → strategy": per-type parameter defaults,
    // then named instances, then per-strategy parameter defaults.
    sections: [
      { key: "backend_defaults", label: "Backend types", path: "backend_defaults" },
      { key: "backends", label: "Instances", path: "backends" },
      { key: "strategy_defaults", label: "Strategies", path: "strategy_defaults" },
    ],
  },
  { key: "mcp", label: "MCP", icon: "box", collections: ["profiles"] },
];

// The dashboard's own settings group: shared appearance plus the server keys.
const APPEARANCE_ID = "·appearance";

const DASHBOARD_DOMAIN: Domain = {
  key: "dashboard",
  label: "Server",
  icon: "sliders",
  collections: [],
};

// Curated small domains living inside the "Other" group (above the auto-discovered long tail).
const OTHER_DOMAINS: readonly Domain[] = [
  { key: "prompt", label: "Prompt", icon: "braces", collections: [] },
  { key: "query", label: "Query", icon: "search", collections: [] },
  { key: "search", label: "Search", icon: "search", collections: [] },
  { key: "cache", label: "Cache", icon: "history", path: "llm.cache" },
];

const ALL_DOMAINS: readonly Domain[] = [...DOMAINS, DASHBOARD_DOMAIN, ...OTHER_DOMAINS];

const local: LocalState = {
  meta: null,
  metaLoading: false,
  tree: null,
  origins: {},
  scope: null,
  selected: "llm",
  section: {}, // domain key -> active section (collection name or "·defaults")
  selectedEntry: null,
  entryFilter: "",
  collapsed: new Set(["·advanced"]), // sidebar groups collapsed by default (long-tail keys)
  revealed: new Set(),
  search: "",
  entryMode: {},
  jsonDrafts: {},
  jsonDirty: new Set(),
};

async function loadTree(): Promise<void> {
  const data = await api.configTree(local.scope ? { scope: local.scope } : {});
  local.tree = configRecord(data.tree.heavenbase);
  local.origins = { ...data.origins };
}

// Config keys can contain dots (e.g. the built-in model "gpt-5.6-luna"), so paths are threaded as
// segment arrays relative to the "heavenbase" root and only joined into the escaped dotted grammar
// (`\.` per _split_path) when talking to the server or matching the dot-escaped origins map.
function escSeg(seg: string): string {
  return seg.replace(/\./g, "\\.");
}

function cmKey(segs: readonly string[]): string {
  return ["heavenbase", ...segs].map(escSeg).join(".");
}

function invalidateJsonDraft(changedKey: string): void {
  for (const entryKey of Object.keys(local.jsonDrafts)) {
    if (changedKey !== entryKey && !changedKey.startsWith(`${entryKey}.`)) continue;
    delete local.jsonDrafts[entryKey];
    local.jsonDirty.delete(entryKey);
  }
}

function walk(segs: readonly string[]): unknown {
  let node: unknown = local.tree;
  for (const seg of segs) {
    if (node === null || typeof node !== "object" || Array.isArray(node)) return undefined;
    node = (node as ConfigRecord)[seg];
  }
  return node;
}

function originOf(segs: readonly string[]): ConfigOrigin {
  const full = cmKey(segs);
  if (local.origins[full]) return local.origins[full];
  // Aggregate over subtree leaves (dict continuation ".", list continuation "["). Order-independent:
  // a builtin entry that also carries a user-added key reads as "override".
  const dotPrefix = `${full}.`;
  const listPrefix = `${full}[`;
  let hasBuiltin = false;
  let hasOverride = false;
  let hasCustom = false;
  for (const [key, origin] of Object.entries(local.origins)) {
    if (!key.startsWith(dotPrefix) && !key.startsWith(listPrefix)) continue;
    if (origin === "override") hasOverride = true;
    else if (origin === "custom") hasCustom = true;
    else hasBuiltin = true;
  }
  if (hasOverride || (hasCustom && hasBuiltin)) return "override";
  if (hasCustom) return "custom";
  return "builtin";
}

function originBadge(origin: ConfigOrigin): UiElement {
  if (origin === "builtin") return el("span.badge.builtin", { title: t("Built-in default (locked)") }, t("builtin"));
  if (origin === "override")
    return el("span.badge.override", { title: t("Built-in value overridden in this scope") }, t("override"));
  return el("span.badge.custom", { title: t("User-defined entry") }, t("custom"));
}

// --------------------------------------------------------------- browser

// Grouped settings sidebar (macOS-Settings-style): domains are group headers,
// sections are the clickable rows — every destination visible in one metaphor.
export function renderBrowser(container: UiElement, state: DashboardState): void {
  if (state.route !== "config") return; // a late async callback must not paint over another view
  const body = el("div.panel-body.config-nav");
  const pick = (domainKey: string, sectionKey?: string): void => {
    local.selected = domainKey;
    if (sectionKey) local.section[domainKey] = sectionKey;
    local.selectedEntry = null;
    local.entryFilter = "";
    local.search = "";
    renderBrowser(container, state);
    renderMain(requiredElement("workspace"), state);
  };
  const navItem = (label: string, selected: boolean, onclick: () => void, count?: number | null): UiElement =>
    el(
      `button.tree-item${selected && !local.search ? ".selected" : ""}`,
      { onclick },
      el("span.name", { style: { paddingLeft: "8px" } }, label),
      count != null ? el("span.count", {}, String(count)) : null,
    );

  for (const domain of DOMAINS) {
    const node = walk((domain.path ?? domain.key).split("."));
    const sections = domainSections(domain, node);
    body.append(el("div.panel-label", {}, t(domain.label)));
    const activeKey = local.section[domain.key] || sections[0]?.key;
    for (const section of sections) {
      const count = section.key === DEFAULTS_ID ? null : Object.keys(configRecord(walk(section.segs))).length;
      body.append(
        navItem(
          sectionLabel(section),
          local.selected === domain.key && activeKey === section.key,
          () => pick(domain.key, section.key),
          count,
        ),
      );
    }
  }

  body.append(el("div.panel-label", {}, t("Other")));
  for (const domain of OTHER_DOMAINS) {
    body.append(navItem(t(domain.label), local.selected === domain.key, () => pick(domain.key)));
  }

  const others = otherDomainKeys();
  if (others.length) {
    const collapsed = local.collapsed.has("·advanced");
    body.append(
      el(
        `button.tree-group${collapsed ? ".collapsed" : ""}`,
        {
          onclick: () => {
            collapsed ? local.collapsed.delete("·advanced") : local.collapsed.add("·advanced");
            renderBrowser(container, state);
          },
        },
        icon("chevronDown"),
        el("span", {}, t("Advanced")),
      ),
    );
    if (!collapsed) for (const key of others) body.append(navItem(key, local.selected === key, () => pick(key)));
  }

  // The dashboard's own settings sit last, below the HeavenBase config groups.
  body.append(el("div.panel-label", {}, t("Dashboard")));
  body.append(navItem(t("Appearance"), local.selected === APPEARANCE_ID, () => pick(APPEARANCE_ID)));
  body.append(navItem(t("Server"), local.selected === DASHBOARD_DOMAIN.key, () => pick(DASHBOARD_DOMAIN.key)));

  container.replaceChildren(
    el("div.panel-head", {}, el("span.title", {}, t("Config"))),
    body,
    el("div.panel-foot", {}, el("span.mono", { title: local.meta?.root || "" }, t("config registry"))),
  );
}

async function reload(state: DashboardState): Promise<void> {
  try {
    await loadTree();
  } catch (error) {
    toast(errorMessage(error), "error");
  }
  renderBrowser(requiredElement("browser"), state);
  renderMain(requiredElement("workspace"), state);
}

// ------------------------------------------------------------------ main

const DEFAULTS_ID = "·defaults";

function currentDomain(): Domain {
  return (
    ALL_DOMAINS.find((item) => item.key === local.selected) ?? {
      key: local.selected,
      label: local.selected,
      collections: null,
      icon: "folder",
    }
  );
}

// Loose scalar leaves of a domain node: not a collection subtree, not owned by another domain page.
function looseLeaves(domain: Domain, node: unknown): [string, unknown][] {
  const baseSegs = (domain.path ?? domain.key).split(".");
  return Object.entries(configRecord(node)).filter(
    ([key]) =>
      !(domain.collections || []).includes(key) &&
      !ALL_DOMAINS.some((item) => item.path === [...baseSegs, key].join(".")),
  );
}

// Sections for the second-level segmented control. Composite domains declare fixed
// sections with their own root paths; collection domains derive sections from
// subtrees plus a Defaults page for loose scalars.
function domainSections(domain: Domain, node: unknown): ConfigSection[] {
  if (domain.sections) {
    return domain.sections.map((section) => ({
      ...section,
      segs: section.path.split("."),
    }));
  }
  if (domain.collections === null || !domain.collections?.length) return [];
  const baseSegs = (domain.path ?? domain.key).split(".");
  const record = configRecord(node);
  const sections: ConfigSection[] = domain.collections
    .filter((collection) => {
      const value = record[collection];
      return value !== null && typeof value === "object";
    })
    .map((collection) => ({ key: collection, segs: [...baseSegs, collection] }));
  if (looseLeaves(domain, node).length) sections.push({ key: DEFAULTS_ID, segs: baseSegs });
  return sections;
}

function sectionLabel(section: ConfigSection): string {
  if (section.label) return t(section.label);
  if (section.key === DEFAULTS_ID) return t("Defaults");
  return t(section.key.charAt(0).toUpperCase() + section.key.slice(1));
}

function otherDomainKeys(): string[] {
  return Object.keys(local.tree ?? {})
    .filter(
      (key) =>
        !ALL_DOMAINS.some(
          (domain) =>
            domain.key === key ||
            domain.path?.startsWith(`${key}.`) ||
            (domain.sections || []).some((section) => section.path === key),
        ),
    )
    .sort();
}

export function renderMain(container: UiElement, state: DashboardState): void {
  if (state.route !== "config") return; // a late async callback must not paint over another view
  container.replaceChildren();
  if (!local.tree) {
    container.append(el("div.view", {}, el("div.empty-state", {}, icon("sliders"), el("span", {}, t("Loading")))));
    loadTree()
      .then(() => {
        if (state.route !== "config") return; // user navigated away while loading
        renderMain(container, state);
        renderBrowser(requiredElement("browser"), state);
      })
      .catch((error) => {
        if (state.route !== "config") return;
        toast(errorMessage(error), "error");
        container.replaceChildren(
          el("div.view", {}, el("div.empty-state", {}, icon("alert"), el("span", {}, errorMessage(error)))),
        );
      });
    return;
  }
  if (!local.meta && !local.metaLoading) {
    local.metaLoading = true;
    api
      .configMeta()
      .then((meta) => {
        local.meta = meta;
        if (!local.scope) local.scope = meta.scope;
        renderMain(requiredElement("workspace"), state);
      })
      .catch((error) => toast(errorMessage(error), "error"));
  }

  const view = el("div.view", { style: { borderTop: "none" } });
  const content = el("div.config-content");
  view.append(buildConfigToolbar(state, content), content);
  renderConfigContent(state, content);
  container.append(view);
}

// Slim toolbar: breadcrumb + key search + history/reload. The server's active
// config scope remains authoritative; exposing version scopes here made an
// ordinary settings page look like a source-control browser.
function buildConfigToolbar(state: DashboardState, content: UiElement): UiElement {
  const domain = currentDomain();
  const node = walk((domain.path ?? domain.key).split("."));
  const sections = domainSections(domain, node);
  const activeKey = local.section[domain.key] || sections[0]?.key;
  const activeSection = sections.find((section) => section.key === activeKey);
  let breadcrumbText = `${t(domain.label)}${activeSection ? ` / ${sectionLabel(activeSection)}` : ""}`;
  let monoText = `heavenbase.${activeSection ? activeSection.segs.join(".") : (domain.path ?? domain.key)}`;
  if (local.selected === APPEARANCE_ID) {
    breadcrumbText = `${t("Dashboard")} / ${t("Appearance")}`;
    monoText = t("shared profile");
  } else if (local.selected === DASHBOARD_DOMAIN.key) {
    breadcrumbText = `${t("Dashboard")} / ${t("Server")}`;
  }
  const breadcrumb = el("span", { style: { fontWeight: 650, fontSize: "12px", whiteSpace: "nowrap" } }, breadcrumbText);
  const searchInput = el("input.input", {
    placeholder: t("Search keys"),
    value: local.search,
    "aria-label": t("Search keys"),
    // Only the content zone re-renders while typing, so the input keeps focus.
    oninput: debounce((event) => {
      if (!(event.target instanceof HTMLInputElement)) return;
      local.search = event.target.value.trim().toLowerCase();
      renderConfigContent(state, content);
    }, 200),
  });
  return el(
    "div.toolbar",
    {},
    breadcrumb,
    el("span.mono", { style: { color: "var(--subtle)" } }, monoText),
    el("div.spacer"),
    el("div.search-box", { style: { width: "190px" } }, icon("search"), searchInput),
    el("div.sep"),
    iconBtn("history", t("History and diff"), () => showHistory()),
    iconBtn("refresh", t("Reload config"), () => reload(state)),
  );
}

// Shared appearance preferences (theme, language, font size) rendered with
// the same form language as config pages and persisted through the dashboard API.
function appearanceForm(state: DashboardState): UiElement {
  const body = el("div.panel-body.form-body");
  const prefRow = (label: string, control: UiElement, help?: UiChild): UiElement =>
    el(
      "div.form-row",
      {},
      el("div.form-label", {}, el("span.form-label-text", {}, label)),
      el("div.form-control", {}, control),
      help ? el("div.form-help", {}, help) : null,
    );
  body.append(
    el(
      "div.form-help",
      { style: { padding: "6px 0 2px" } },
      t("These preferences are shared by all local dashboard hosts."),
    ),
  );
  body.append(
    prefRow(
      t("Theme"),
      select({
        value: state.appearance.theme(),
        options: state.appearance
          .themes()
          .map((theme) => ({ value: theme.cls, label: t(theme.name), icon: theme.icon })),
        onchange: (cls) => state.appearance.setTheme(cls),
      }),
    ),
    prefRow(
      t("Language"),
      select<Language>({
        value: state.appearance.lang(),
        options: [
          { value: "en", label: "English" },
          { value: "zh", label: "中文" },
        ],
        onchange: (next) => state.appearance.setLang(next),
      }),
    ),
    prefRow(
      t("Font size"),
      select({
        value: state.appearance.fontScale(),
        options: [
          { value: "small", label: t("Small") },
          { value: "default", label: t("Default") },
          { value: "large", label: t("Large") },
          { value: "xlarge", label: t("Extra large") },
        ],
        onchange: (next) => state.appearance.setFontScale(next),
      }),
      t("Scales all text on the page, assistant included."),
    ),
    prefRow(
      t("Zoom"),
      select({
        value: state.appearance.font(),
        options: [
          { value: "small", label: "92%" },
          { value: "default", label: "100%" },
          { value: "large", label: "110%" },
        ],
        onchange: (next) => state.appearance.setFont(next),
      }),
    ),
  );
  return body;
}

function renderConfigContent(state: DashboardState, content: UiElement): void {
  content.replaceChildren();
  if (local.search) {
    content.append(searchResultsBody(state));
    return;
  }
  if (local.selected === APPEARANCE_ID) {
    content.append(appearanceForm(state));
    return;
  }
  const domain = currentDomain();
  const baseSegs = (domain.path ?? domain.key).split(".");
  const node = walk(baseSegs);
  const sections = domainSections(domain, node);
  if (!sections.length) {
    // Composite domains carry no node of their own, so the existence check only applies here.
    if (node === undefined) {
      content.append(el("div.empty-state", {}, icon("folder"), el("span", {}, t("No values"))));
      return;
    }
    content.append(entryForm(state, baseSegs, node));
    return;
  }
  const firstSection = sections[0];
  if (!firstSection) return;
  const activeKey = local.section[domain.key] ?? firstSection.key;
  const active = sections.find((section) => section.key === activeKey) ?? firstSection;
  if (active.key === DEFAULTS_ID) {
    content.append(entryForm(state, baseSegs, Object.fromEntries(looseLeaves(domain, node))));
    return;
  }
  content.append(sectionSplit(state, active));
}

// ---------------------------------------------------------------- search

const SEARCH_CAP = 200;

// Leaf rows (scalars and lists) across the whole tree; deep dicts are recursed into.
function flattenDeep(node: unknown, prefix: string[] = []): FlatRow[] {
  if (node === null || typeof node !== "object" || Array.isArray(node)) return [{ segs: prefix, value: node }];
  const rows: FlatRow[] = [];
  for (const [key, value] of Object.entries(node)) rows.push(...flattenDeep(value, [...prefix, key]));
  return rows;
}

function searchResultsBody(state: DashboardState): UiElement {
  const query = local.search;
  const matches: FlatRow[] = [];
  for (const row of flattenDeep(local.tree)) {
    const key = row.segs.join(".").toLowerCase();
    const value = row.value == null ? "" : String(row.value).toLowerCase();
    if (key.includes(query) || value.includes(query)) {
      matches.push(row);
      if (matches.length > SEARCH_CAP) break;
    }
  }
  const capped = matches.length > SEARCH_CAP;
  const body = el("div.panel-body", { style: { padding: "4px 14px 16px" } });
  body.append(
    el(
      "div.panel-label",
      { style: { padding: "6px 0 2px" } },
      t("{n} matches", { n: capped ? `${SEARCH_CAP}+` : matches.length }),
    ),
  );
  if (!matches.length)
    body.append(el("div", { style: { padding: "16px 0", color: "var(--muted)" } }, t("No matching keys")));
  for (const row of matches.slice(0, SEARCH_CAP)) body.append(kvRow(state, row.segs, row.segs.join("."), row.value));
  if (capped) {
    body.append(
      el(
        "div",
        { style: { padding: "8px 0", color: "var(--subtle)", fontSize: "11px" } },
        t("More matches truncated — narrow the search"),
      ),
    );
  }
  return body;
}

function entryModeSwitch(mode: EditorMode, onchange: (next: EditorMode) => void): UiElement {
  const button = (value: EditorMode, label: string): UiElement =>
    el(
      `button.config-mode-option${mode === value ? ".active" : ""}`,
      {
        type: "button",
        role: "tab",
        "aria-selected": String(mode === value),
        onclick: () => onchange(value),
      },
      label,
    );
  return el(
    "div.config-mode-switch",
    { role: "tablist", "aria-label": t("Editor mode"), dataset: { mode } },
    el("span.config-mode-indicator", { "aria-hidden": "true" }),
    button("config", t("Config")),
    button("json", "JSON"),
  );
}

function entryJsonEditor(state: DashboardState, entrySegs: readonly string[], name: string, entry: unknown): UiElement {
  const entryKey = cmKey(entrySegs);
  const persisted = JSON.stringify(entry, null, 2);
  const initial = local.jsonDrafts[entryKey] ?? persisted;
  const body = el("div.panel-body.config-json-mode");
  const editorHost = el("div.config-code-host", { "aria-label": t("Entry JSON") });
  const statusText = el("span", {}, "");
  const status = el("div.code-validity", { role: "status" }, icon("check"), statusText);
  let readText = (): string => local.jsonDrafts[entryKey] ?? initial;
  let writeText = (next: string): void => {
    local.jsonDrafts[entryKey] = next;
  };

  const saveButton = el(
    "button.btn.primary.code-save",
    {
      type: "button",
      onclick: async () => {
        const text = readText();
        let parsed: unknown;
        try {
          parsed = JSON.parse(text);
        } catch {
          toast(t("Invalid JSON"), "error");
          return;
        }
        saveButton.disabled = true;
        try {
          await api.configSet(entryKey, parsed, local.scope ?? undefined);
          const formatted = JSON.stringify(parsed, null, 2);
          local.jsonDrafts[entryKey] = formatted;
          local.jsonDirty.delete(entryKey);
          toast(t("Saved {name}", { name }));
          await loadTree();
          renderMain(requiredElement("workspace"), state);
        } catch (error) {
          saveButton.disabled = false;
          toast(errorMessage(error), "error");
        }
      },
    },
    t("Save"),
  );

  const updateValidity = (text: string): void => {
    local.jsonDrafts[entryKey] = text;
    const dirty = text !== persisted;
    local.jsonDirty[dirty ? "add" : "delete"](entryKey);
    try {
      JSON.parse(text);
      status.className = `code-validity${dirty ? " dirty" : ""}`;
      status.replaceChildren(icon("check"), statusText);
      statusText.textContent = dirty ? t("Valid JSON · unsaved") : t("Valid JSON");
      saveButton.disabled = !dirty;
    } catch {
      status.className = "code-validity invalid";
      status.replaceChildren(icon("alert"), statusText);
      statusText.textContent = t("Invalid JSON");
      saveButton.disabled = true;
    }
  };

  const toolbar = el(
    "div.code-toolbar",
    {},
    status,
    el("span.spacer"),
    el(
      "button.btn.subtle",
      { type: "button", onclick: () => copyText(readText(), t("Copied JSON")) },
      icon("copy"),
      el("span", {}, t("Copy")),
    ),
    el(
      "button.btn.subtle",
      {
        type: "button",
        onclick: () => {
          try {
            writeText(JSON.stringify(JSON.parse(readText()), null, 2));
          } catch {
            toast(t("Invalid JSON"), "error");
          }
        },
      },
      icon("braces"),
      el("span", {}, t("Format")),
    ),
    saveButton,
  );
  body.append(el("div.code-editor-shell", {}, toolbar, editorHost));
  updateValidity(initial);

  queueMicrotask(() => {
    if (!editorHost.isConnected) return;
    const CodeMirror = window.CodeMirror;
    if (!CodeMirror) {
      const fallback = el("textarea.textarea.code-fallback", { value: initial, "aria-label": t("Entry JSON") });
      fallback.addEventListener("input", () => updateValidity(fallback.value));
      editorHost.replaceChildren(fallback);
      readText = () => fallback.value;
      writeText = (next) => {
        fallback.value = next;
        updateValidity(next);
      };
      return;
    }
    const editor = CodeMirror(editorHost, {
      value: initial,
      mode: { name: "javascript", json: true },
      lineNumbers: true,
      lineWrapping: false,
      matchBrackets: true,
      autoCloseBrackets: true,
      indentUnit: 2,
      tabSize: 2,
      autofocus: true,
    });
    editor.setSize(null, "100%");
    readText = () => editor.getValue();
    writeText = (next) => {
      editor.setValue(next);
      updateValidity(next);
    };
    editor.on("change", (instance) => updateValidity(instance.getValue()));
    requestAnimationFrame(() => editor.refresh());
  });
  return body;
}

// ------------------------------------------------------------ section view

function sectionSplit(state: DashboardState, section: ConfigSection): UiElement {
  const split = el("div.config-split");
  const listPanel = el("div.entry-list");
  const detail = el("div.entry-detail");
  const sectionSegs = section.segs;
  // Config order (default.yaml order, merge appends customs), with custom entries grouped last.
  const allNames = Object.keys(configRecord(walk(sectionSegs)));
  const isCustom = (name: string): boolean => originOf([...sectionSegs, name]) === "custom";
  const names = [...allNames.filter((name) => !isCustom(name)), ...allNames.filter(isCustom)];

  if (local.selectedEntry && !names.includes(local.selectedEntry)) local.selectedEntry = null;
  if (!local.selectedEntry && names.length) local.selectedEntry = names[0] ?? null;
  // Builtins carry no lock mark in the list; origin still shows in the detail header.
  const badge = (name: string): UiElement | null => {
    const origin = originOf([...sectionSegs, name]);
    return origin === "builtin" ? null : originBadge(origin);
  };
  const body = el("div.panel-body");

  function renderList(): void {
    body.replaceChildren();
    const visible = local.entryFilter ? names.filter((name) => name.toLowerCase().includes(local.entryFilter)) : names;
    if (!visible.length) {
      body.append(
        el("div", { style: { padding: "12px 10px", color: "var(--muted)", fontSize: "12px" } }, t("No values")),
      );
      return;
    }
    for (const name of visible) {
      const iconName =
        sectionSegs.join(".") === "backends" ? String(configRecord(walk([...sectionSegs, name])).type || name) : name;
      body.append(
        el(
          `button.tree-item${local.selectedEntry === name ? ".selected" : ""}`,
          {
            onclick: () => {
              local.selectedEntry = name;
              renderDetail();
              renderList();
            },
          },
          brandIcon(iconName),
          el("span.name", {}, name),
          badge(name),
        ),
      );
    }
  }
  renderList();

  // Filter box only when the list is long enough to need one; typing re-renders
  // just the list body so the input keeps focus.
  const filterZone =
    names.length > 12
      ? el(
          "div",
          { style: { padding: "0 10px 6px" } },
          el(
            "div.search-box",
            {},
            icon("search"),
            el("input.input", {
              placeholder: t("Filter…"),
              value: local.entryFilter,
              "aria-label": t("Filter…"),
              oninput: debounce((event) => {
                if (!(event.target instanceof HTMLInputElement)) return;
                local.entryFilter = event.target.value.trim().toLowerCase();
                renderList();
              }, 150),
            }),
          ),
        )
      : null;

  function renderDetail(): void {
    detail.replaceChildren();
    if (!local.selectedEntry) {
      detail.append(el("div.empty-state", {}, icon("chevronLeft"), el("span", {}, t("Select an entry"))));
      return;
    }
    const name = local.selectedEntry;
    const entrySegs = [...sectionSegs, name];
    const entry = walk(entrySegs);
    if (entry === undefined) {
      detail.append(el("div.empty-state", {}, icon("folder"), el("span", {}, t("Entry removed"))));
      return;
    }
    const origin = originOf(entrySegs);
    const entryKey = cmKey(entrySegs);
    const mode = local.entryMode[entryKey] ?? "config";
    detail.append(
      el(
        "div.panel-head",
        {},
        el("span.title", { style: { fontFamily: "var(--font-mono)" } }, name),
        originBadge(origin),
        el("div", { style: { flex: 1 } }),
        origin !== "builtin"
          ? iconBtn(
              origin === "custom" ? "trash" : "rotateCcw",
              origin === "custom" ? t("Delete custom entry") : t("Reset entry to defaults"),
              () => confirmReset(state, entrySegs, name, origin),
            )
          : null,
        entryModeSwitch(mode, (next) => {
          local.entryMode[entryKey] = next;
          renderDetail();
        }),
      ),
      mode === "json" ? entryJsonEditor(state, entrySegs, name, entry) : entryForm(state, entrySegs, entry),
    );
  }

  listPanel.append(
    el(
      "div.panel-head",
      {},
      el("span.title", {}, sectionLabel(section)),
      el("span.count.mono", { style: { color: "var(--subtle)" } }, String(names.length)),
      el("div", { style: { flex: 1 } }),
      iconBtn("plus", t("Add custom entry"), () => addEntryDialog(state, section)),
    ),
  );
  if (filterZone) listPanel.append(filterZone);
  listPanel.append(body);
  renderDetail();
  split.append(listPanel, detail);
  return split;
}

// Starter skeletons per section: a new entry begins with the fields that matter,
// filled in through the friendly form editor — no raw JSON in the add dialog.
const ENTRY_TEMPLATES: Record<string, ConfigRecord> = {
  "llm.providers": { api_key: "", base_url: "" },
  "llm.models": { default_provider: "", providers: {}, aliases: [] },
  "llm.presets": { desc: "", model: "" },
  "llm.gateways": { base_url: "", api_key: "" },
  "db.providers": { dialect: "" },
  "workspace.presets": { desc: "", backends: {} },
  backends: { type: "", name: "" },
};

function addEntryDialog(state: DashboardState, section: ConfigSection): void {
  const template = ENTRY_TEMPLATES[section.segs.join(".")] ?? {};
  const nameInput = el("input.input", {
    placeholder: "entry-name",
    style: { width: "100%" },
    "aria-label": t("Entry name"),
  });
  // Prefilled with the template: non-technical users just name the entry and click Add,
  // technical users can extend the JSON in place.
  const jsonInput = el("textarea.textarea", {
    value: JSON.stringify(template, null, 2),
    "aria-label": t("Entry JSON"),
    style: { minHeight: "130px" },
  });
  modal({
    title: t("Add {name} entry", { name: sectionLabel(section) }),
    body: el(
      "div",
      { style: { display: "flex", flexDirection: "column", gap: "8px" } },
      nameInput,
      el(
        "div.form-help",
        {},
        t("Name the entry above and click Add — or, advanced, edit the prefilled JSON below first."),
      ),
      jsonInput,
    ),
    actions: [
      {
        label: t("Add"),
        primary: true,
        onclick: async () => {
          const name = nameInput.value.trim();
          if (!name) return false;
          let value: unknown;
          try {
            value = JSON.parse(jsonInput.value || "{}");
            jsonInput.classList.remove("invalid");
          } catch {
            jsonInput.classList.add("invalid");
            toast(t("Invalid JSON"), "error");
            return false;
          }
          if (value !== null && typeof value === "object" && !Array.isArray(value)) {
            const parsed = value as ConfigRecord;
            if ("name" in parsed && !parsed.name) parsed.name = name;
          }
          try {
            await api.configSet(cmKey([...section.segs, name]), value, local.scope ?? undefined);
            toast(t("Added {name}", { name }));
            local.selectedEntry = name;
            await reload(state);
          } catch (error) {
            toast(errorMessage(error), "error");
            return false;
          }
          return undefined;
        },
      },
    ],
  });
  setTimeout(() => nameInput.focus(), 30);
}

function confirmReset(state: DashboardState, segs: readonly string[], _name: string, origin: ConfigOrigin): void {
  const key = cmKey(segs);
  modal({
    title: origin === "custom" ? t("Delete entry") : t("Reset entry"),
    body: el(
      "div",
      {},
      el(
        "div",
        {},
        origin === "custom"
          ? t("Delete this custom entry from the scoped config layer?")
          : t("Reset every overridden key in this entry to its built-in default?"),
      ),
      el("div.mono", { style: { marginTop: "6px", color: "var(--muted)" } }, key),
    ),
    actions: [
      {
        label: origin === "custom" ? t("Delete") : t("Reset"),
        danger: true,
        onclick: async () => {
          try {
            await api.configReset(key, local.scope ?? undefined);
            toast(origin === "custom" ? t("Deleted") : t("Reset to defaults"));
            if (origin === "custom") local.selectedEntry = null;
            await reload(state);
          } catch (error) {
            toast(errorMessage(error), "error");
          }
        },
      },
    ],
  });
}

// ------------------------------------------------------------- form editor
// Business-friendly entry forms: reference dropdowns, toggles, number inputs,
// secret fields, chips for string lists; open-ended dicts fold under Advanced.

const PRIORITY = [
  "desc",
  "model",
  "default_preset",
  "preset",
  "default_provider",
  "providers",
  "provider",
  "default_gateway",
  "gateway",
  "api_key",
  "base_url",
  "enabled",
];

const LABEL_OVERRIDES = {
  api_key: "API key",
  base_url: "Base URL",
  desc: "Description",
  default_args: "Default args",
  allow_unknown_models: "Unlisted model names",
  thinking_control: "Thinking control",
  default_provider: "Default provider",
  default_gateway: "Default gateway",
  default_preset: "Default preset",
  env_defaults: "Env defaults",
  share_credentials: "Share credentials with assistant",
  max_frames: "Maximum query tables",
  max_tool_result_chars: "Maximum tool result characters",
  read_only: "Read-only by default",
  think: "Thinking",
  reasoning_effort: "Reasoning effort",
  providers: "Provider model IDs",
};

function prettyLabel(name: string): string {
  const overrides: Readonly<Record<string, string>> = LABEL_OVERRIDES;
  const label = overrides[name] ?? (name.charAt(0).toUpperCase() + name.slice(1)).replaceAll("_", " ");
  return t(label);
}

const FIELD_HELP: Readonly<Record<string, string>> = {
  // Full-path keys win over leaf-name keys, so generic names like "port" stay uncaptioned elsewhere.
  "dashboard.host": "Bind host for hb dashboard; keep 127.0.0.1 unless you mean to expose it.",
  "dashboard.port": "Port hb dashboard serves on.",
  "dashboard.open": "Open the browser automatically when hb dashboard starts.",
  "dashboard.assistant.model": "Model the assistant chat uses (a preset overrides this).",
  "dashboard.assistant.provider": "Provider for the assistant model; keeps the call on your own key.",
  "dashboard.assistant.preset": "LLM preset for the assistant; when set it overrides model/provider.",
  "dashboard.assistant.share_credentials":
    "Dangerous: allow get_config to send raw API keys, tokens, passwords, and other credentials to the external assistant model.",
  "dashboard.assistant.max_frames": "Maximum query result tables retained for one assistant turn.",
  "dashboard.assistant.max_tool_result_chars": "Maximum characters retained for each non-query assistant tool result.",
  "dashboard.assistant.read_only":
    "Keep credentials masked and disable Python and code_reduce unless a chat explicitly turns this off.",
  // biome-ignore lint/suspicious/noTemplateCurlyInString: Documents the literal HeavenBase environment-reference syntax.
  api_key: "Provider API key; ${env:VAR} references resolve from the environment at runtime.",
  base_url: "OpenAI-compatible endpoint base URL.",
  backend: "Effective transport backends are read-only here; edit the gateway mapping in JSON mode.",
  gateway: "Gateway that materializes requests; defaults to the LLM default gateway.",
  allow_unknown_models: "Use model names that have not been added to the Models catalog.",
  thinking_control: "How reasoning/thinking is passed to the provider.",
  headers: "Extra HTTP headers sent with every request.",
  env_defaults: "Environment variables set before the gateway loads.",
  model: "Canonical model name or alias this preset resolves to.",
  aliases: "Alternate names that resolve to this entry.",
  providers:
    "Map this model to the native ID used by each provider. Provider-specific extras remain available in JSON mode.",
  think: "Enable provider-supported reasoning output for this model or preset.",
  reasoning_effort: "Optional provider reasoning level, for example low, medium, high, or max.",
  dialect: "SQL dialect this database provider speaks.",
  params: "Extra connection parameters.",
  pool: "Connection pool settings.",
  tools: "Tool groups exposed by this MCP profile.",
  entities: "Entity scope: * for all, or a comma list.",
  serializer: "Row serializer used for tool payloads.",
  default_args: "Extra request arguments merged into every call.",
  default_provider: "Provider used when a call names no provider.",
  default_gateway: "Gateway used when a call names no gateway.",
  default_preset: "Preset used when nothing names one explicitly.",
};

const ENV_FIRST_FIELD = /(?:api_key|apikey|token|password|secret|credential|base_url|url|uri|path|root)$/i;

// Unified field schema per LLM collection: EVERY entry of a collection renders
// the same ordered set of fields (empty when the entry omits one), so all
// providers/models/presets/gateways look identical. Keys not listed fold into
// the Advanced section untouched. Control kinds: text, secret, bool, chips,
// kv (open dict), ref:<provider|model|gateway|preset>.
const ENTRY_SCHEMAS: Readonly<Record<string, readonly SchemaField[]>> = {
  "llm.providers": [
    { key: "backend", control: "backend-summary" },
    { key: "api_key", control: "env-secret" },
    { key: "base_url", control: "env-url" },
    { key: "gateway", control: "ref:gateway" },
    { key: "allow_unknown_models", control: "json-only" },
    { key: "default_args", control: "kv" },
  ],
  "llm.models": [
    { key: "default_provider", control: "ref:provider" },
    { key: "providers", control: "model-providers" },
    { key: "aliases", control: "chips" },
    { key: "think", path: ["default_args", "think"], control: "inherited-bool" },
    { key: "reasoning_effort", path: ["default_args", "reasoning_effort"], control: "inherited-text" },
    { key: "default_args", control: "kv" },
  ],
  "llm.presets": [
    { key: "desc", control: "text" },
    { key: "model", control: "ref:model" },
    { key: "provider", control: "ref:provider" },
    { key: "gateway", control: "ref:gateway" },
    { key: "think", path: ["default_args", "think"], control: "inherited-bool" },
    { key: "reasoning_effort", path: ["default_args", "reasoning_effort"], control: "inherited-text" },
    { key: "default_args", control: "kv" },
  ],
  "llm.gateways": [
    { key: "base_url", control: "env-url" },
    { key: "api_key", control: "env-secret" },
    { key: "headers", control: "kv" },
    { key: "thinking_control", control: "text" },
    { key: "default_args", control: "kv" },
  ],
  "db.providers": [
    { key: "aliases", control: "chips" },
    { key: "dialect", control: "text" },
    { key: "driver", control: "text" },
    { key: "host", control: "env-text" },
    { key: "port", control: "number" },
    { key: "username", control: "text" },
    { key: "password", control: "env-secret" },
    { key: "database", control: "env-text" },
    { key: "schema", control: "text" },
    { key: "url", control: "env-url" },
    { key: "uri", control: "env-url" },
    { key: "service", control: "text" },
    { key: "catalog", control: "text" },
    { key: "params", control: "kv" },
    { key: "pool", control: "kv" },
  ],
  llm: [
    { key: "default_preset", control: "ref:preset" },
    { key: "default_provider", control: "ref:provider" },
    { key: "default_gateway", control: "ref:gateway" },
    { key: "model_mismatch", control: "text" },
    { key: "default_args", control: "kv" },
  ],
  db: [
    { key: "local_root", control: "env-url" },
    { key: "default_preset", control: "text" },
    { key: "default_provider", control: "text" },
    { key: "default_args", control: "kv" },
  ],
  "workspace.presets": [
    { key: "desc", control: "text" },
    { key: "backends", control: "json-only" },
  ],
  "mcp.profiles": [
    { key: "desc", control: "text" },
    { key: "tools", control: "chips" },
    { key: "entities", control: "text" },
    { key: "serializer", control: "text" },
    { key: "skills", control: "text" },
  ],
  backends: [
    { key: "type", control: "text" },
    { key: "name", control: "text" },
    { key: "database", control: "env-text" },
  ],
};

const ENTRY_GROUPS: Readonly<Record<string, readonly SchemaGroup[]>> = {
  "llm.providers": [
    { label: "Connection", fields: ["api_key", "base_url"] },
    { label: "Routing", fields: ["gateway"] },
    { label: "Effective backends", fields: ["backend"] },
    { label: "Request defaults", fields: ["default_args"] },
  ],
  "llm.models": [
    { label: "Model resolution", fields: ["default_provider", "providers", "aliases"] },
    { label: "Reasoning", fields: ["think", "reasoning_effort"] },
    { label: "Additional request defaults", fields: ["default_args"] },
  ],
  "llm.presets": [
    { label: "Preset", fields: ["desc"] },
    { label: "Model routing", fields: ["model", "provider", "gateway"] },
    { label: "Reasoning", fields: ["think", "reasoning_effort"] },
    { label: "Additional request defaults", fields: ["default_args"] },
  ],
  "llm.gateways": [
    { label: "Connection", fields: ["base_url", "api_key"] },
    { label: "Request behavior", fields: ["headers", "thinking_control"] },
    { label: "Request defaults", fields: ["default_args"] },
  ],
  "db.providers": [
    { label: "Driver", fields: ["aliases", "dialect", "driver"] },
    { label: "Connection", fields: ["host", "port", "username", "password", "database", "schema"] },
    { label: "Alternative connection fields", fields: ["url", "uri", "service", "catalog"], collapsible: true },
    { label: "Advanced connection settings", fields: ["params", "pool"], collapsible: true },
  ],
  llm: [
    { label: "Routing defaults", fields: ["default_preset", "default_provider", "default_gateway"] },
    { label: "Behavior", fields: ["model_mismatch"] },
    { label: "Request defaults", fields: ["default_args"] },
  ],
  db: [
    { label: "Local database", fields: ["local_root"] },
    { label: "Routing defaults", fields: ["default_preset", "default_provider"] },
    { label: "Request defaults", fields: ["default_args"] },
  ],
  "workspace.presets": [{ label: "Preset", fields: ["desc"] }],
  "mcp.profiles": [
    { label: "Profile", fields: ["desc"] },
    { label: "Exposure", fields: ["tools", "entities", "skills"] },
    { label: "Serialization", fields: ["serializer"] },
  ],
  backends: [
    { label: "Backend", fields: ["type", "name"] },
    { label: "Connection", fields: ["database"] },
  ],
};

// Options for a schema ref control; inheritance must remain selectable even
// after an explicit value has been chosen.
function aliasesOf(spec: unknown): string[] {
  const aliases = configRecord(spec).aliases;
  return Array.isArray(aliases) ? aliases.filter((value): value is string => typeof value === "string") : [];
}

function inheritedReference(kind: string, entrySegs: readonly string[] = []): string {
  const entry = configRecord(walk(entrySegs));
  const modelName = typeof entry.model === "string" ? entry.model : "";
  const models = configRecord(walk(["llm", "models"]));
  let model = configRecord(models[modelName]);
  if (!Object.keys(model).length && modelName) {
    model = configRecord(Object.values(models).find((spec) => aliasesOf(spec).includes(modelName)));
  }
  if (kind === "provider" && typeof model.default_provider === "string" && model.default_provider) {
    return model.default_provider;
  }
  if (kind === "model") {
    const presetName =
      typeof entry.preset === "string" && entry.preset ? entry.preset : walk(["llm", "default_preset"]);
    const preset = configRecord(walk(["llm", "presets", String(presetName ?? "")]));
    if (typeof preset.model === "string") return preset.model;
  }
  const defaults: Readonly<Record<string, string>> = {
    gateway: "default_gateway",
    preset: "default_preset",
    provider: "default_provider",
  };
  const key = defaults[kind];
  const value = key ? walk(["llm", key]) : "";
  return typeof value === "string" ? value : "";
}

function refOptions(kind: string, value: unknown, entrySegs: readonly string[] = []): ReferenceOption[] {
  const keysOf = (path: readonly string[]): string[] => Object.keys(configRecord(walk(path)));
  let names: string[] = [];
  const hints: Record<string, string> = {};
  if (kind === "model") {
    names = keysOf(["llm", "models"]);
    for (const [key, spec] of Object.entries(configRecord(walk(["llm", "models"])))) {
      for (const alias of aliasesOf(spec)) {
        names.push(alias);
        hints[alias] = `→ ${key}`;
      }
    }
  } else if (kind === "provider") names = keysOf(["llm", "providers"]);
  else if (kind === "gateway") names = keysOf(["llm", "gateways"]);
  else if (kind === "preset") names = keysOf(["llm", "presets"]);
  if (typeof value === "string" && value !== "" && !names.includes(value)) names.unshift(value);
  if (!names.includes("")) names.unshift("");
  const inherited = inheritedReference(kind, entrySegs);
  return names.map((option) => ({
    value: option,
    label: option || (inherited ? t("Inherit · {name}", { name: inherited }) : t("(inherit)")),
    ...(!option && inherited ? { hint: t("Effective value") } : {}),
    ...(hints[option] ? { hint: hints[option] } : {}),
  }));
}

// Dropdown options for fields that reference other config entries (models, providers, gateways, presets).
function referenceOptions(segs: readonly string[], value: unknown): ReferenceOption[] | null {
  if (value !== null && typeof value !== "string") return null;
  const name = segs[segs.length - 1];
  const domain = segs[0];
  const keysOf = (path: readonly string[]): string[] => Object.keys(configRecord(walk(path)));
  let names: string[] | null = null;
  const hints: Record<string, string> = {};
  if (name === "model") {
    names = keysOf(["llm", "models"]);
    for (const [key, spec] of Object.entries(configRecord(walk(["llm", "models"])))) {
      for (const alias of aliasesOf(spec)) {
        names.push(alias);
        hints[alias] = `→ ${key}`;
      }
    }
  } else if (name === "provider" || name === "default_provider") {
    names = domain === "db" ? keysOf(["db", "providers"]) : keysOf(["llm", "providers"]);
  } else if (name === "gateway" || name === "default_gateway") {
    names = keysOf(["llm", "gateways"]);
  } else if (name === "preset" || name === "default_preset") {
    names = domain === "workspace" ? keysOf(["workspace", "presets"]) : keysOf(["llm", "presets"]);
  }
  if (!names?.length) return null;
  if (typeof value === "string" && value !== "" && !names.includes(value)) names.unshift(value);
  return names.map((option) => ({
    value: option,
    label: option,
    ...(hints[option] ? { hint: hints[option] } : {}),
  }));
}

function makeSave(state: DashboardState, fullKey: string, label: string): (nextValue: unknown) => Promise<void> {
  return async (nextValue: unknown): Promise<void> => {
    try {
      await api.configSet(fullKey, nextValue, local.scope ?? undefined);
      invalidateJsonDraft(fullKey);
      toast(t("Saved {name}", { name: label }));
      await loadTree();
      renderMain(requiredElement("workspace"), state);
    } catch (error) {
      toast(errorMessage(error), "error");
    }
  };
}

function chipable(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string" && item.length <= 60);
}

function chipsEditor({
  values,
  label,
  save,
}: {
  values: readonly string[];
  label: string;
  save: SaveValue;
}): UiElement {
  const zone = el("div.chips");
  values.forEach((item, index) => {
    zone.append(
      el(
        "span.chip-tag",
        {},
        item,
        el(
          "button",
          { "aria-label": t("Remove"), onclick: () => save(values.filter((_, i) => i !== index)) },
          icon("x"),
        ),
      ),
    );
  });
  const input = el("input.chip-input", {
    placeholder: t("Add…"),
    "aria-label": label,
    onkeydown: (event) => {
      if (event.key === "Enter" && input.value.trim()) {
        if (event.isComposing || event.keyCode === 229) return; // IME confirm, not a commit
        event.preventDefault();
        save([...values, input.value.trim()]);
      } else if (event.key === "Backspace" && !input.value && values.length) {
        save(values.slice(0, -1));
      }
    },
  });
  zone.append(input);
  return zone;
}

function coerceScalar(raw: string, prev: unknown): string | number | boolean | null {
  if (typeof prev === "number") {
    if (raw === "") return null;
    const n = Number(raw);
    return Number.isNaN(n) ? raw : n;
  }
  if (typeof prev === "boolean") return raw === "true";
  if (raw === "true" || raw === "false") return raw === "true";
  if (raw !== "" && /^-?(\d+\.?\d*|\.\d+)$/.test(raw)) return Number(raw);
  return raw;
}

// Editable key/value rows for an open-ended dict field (default_args, model providers,
// headers, env_defaults). A "+" adds a row; the whole dict saves atomically.
function kvEditor(
  state: DashboardState,
  dictSegs: readonly string[],
  dict: unknown,
  label: string,
  hiddenKeys: readonly string[] = [],
): UiElement {
  const current: ConfigRecord = { ...configRecord(dict) };
  const hidden = new Set(hiddenKeys);
  const save = makeSave(state, cmKey(dictSegs), label);
  const rows = el("div.kv-rows");
  for (const [k, v] of Object.entries(current).filter(([key]) => !hidden.has(key))) {
    const nested = v !== null && typeof v === "object";
    const keyInput = el("input.input.kv-k", { value: k, "aria-label": t("Key") });
    keyInput.addEventListener("change", () => {
      const nk = keyInput.value.trim();
      if (!nk || nk === k) {
        keyInput.value = k;
        return;
      }
      let next: ConfigRecord;
      try {
        next = renameConfigKey(current, k, nk);
      } catch (error) {
        toast(t(errorMessage(error)), "error");
        keyInput.value = k;
        return;
      }
      save(next);
    });
    let valControl: UiElement;
    if (nested) {
      valControl = jsonControl(v, k, (parsed: unknown) => save({ ...current, [k]: parsed }));
    } else if (typeof v === "boolean") {
      valControl = el("button.toggle", {
        role: "switch",
        "aria-checked": String(v),
        "aria-label": k,
        onclick: () => save({ ...current, [k]: !v }),
      });
    } else {
      const vi = el("input.input.kv-v", { value: v == null ? "" : String(v), "aria-label": t("Value") });
      vi.addEventListener("change", () => save({ ...current, [k]: coerceScalar(vi.value, v) }));
      valControl = vi;
    }
    rows.append(
      el(
        "div.kv-editor-row",
        {},
        keyInput,
        valControl,
        iconBtn(
          "trash",
          t("Remove"),
          () => {
            const next = { ...current };
            delete next[k];
            save(next);
          },
          "danger",
        ),
      ),
    );
  }
  // Keep one trailing draft visible. Enter moves key → value, then saves and
  // transfers focus to the next fresh row created by the render.
  const draftRow = (): UiElement => {
    const pathToken = cmKey(dictSegs);
    const keyInput = el("input.input.kv-k", { placeholder: t("Key"), "aria-label": t("Key") });
    const valInput = el("input.input.kv-v", { placeholder: t("Value"), "aria-label": t("Value") });
    let committing = false;
    const commit = async (advance: boolean): Promise<void> => {
      if (committing) return;
      const nk = keyInput.value.trim();
      if (!nk) return;
      if (nk in current) {
        toast(t("{name} already exists", { name: nk }), "error");
        return;
      }
      committing = true;
      await save({ ...current, [nk]: valInput.value === "" ? "" : coerceScalar(valInput.value, null) });
      if (!advance) return;
      queueMicrotask(() =>
        requiredElement("workspace")
          .querySelector(`[data-kv-draft="${CSS.escape(pathToken)}"] .kv-k`)
          ?.focus(),
      );
    };
    keyInput.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || !keyInput.value.trim()) return;
      event.preventDefault();
      valInput.focus();
    });
    valInput.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      void commit(true);
    });
    valInput.addEventListener("change", () => void commit(false));
    const row = el(
      "div.kv-editor-row.draft",
      { dataset: { kvDraft: pathToken } },
      keyInput,
      valInput,
      el("span.kv-draft-tail", { "aria-hidden": "true" }, icon("plus")),
    );
    return row;
  };
  rows.append(draftRow());
  const addBtn = el(
    "button.btn.subtle.kv-add",
    {
      onclick: () => {
        rows.querySelector(".kv-editor-row.draft .kv-k")?.focus();
      },
    },
    icon("plus"),
    el("span", {}, t("Add row")),
  );
  return el("div.kv-editor", {}, rows, addBtn);
}

function modelProvidersEditor(
  state: DashboardState,
  dictSegs: readonly string[],
  dict: unknown,
  label: string,
): UiElement {
  const current: ConfigRecord = { ...configRecord(dict) };
  const save = makeSave(state, cmKey(dictSegs), label);
  const rows = el("div.model-provider-rows");
  const providerNames = Object.keys(configRecord(walk(["llm", "providers"])));
  const optionsFor = (value: string, draft = false): ReferenceOption[] => {
    const names = [...providerNames];
    if (value && !names.includes(value)) names.unshift(value);
    return [
      ...(draft ? [{ value: "", label: t("Select provider") }] : []),
      ...names.map((name) => ({ value: name, label: name })),
    ];
  };
  for (const [provider, rawSpec] of Object.entries(current)) {
    const spec = typeof rawSpec === "string" ? { model_id: rawSpec } : { ...configRecord(rawSpec) };
    const modelId = spec.model_id == null ? "" : String(spec.model_id);
    const extraCount = Object.keys(spec).filter((key) => key !== "model_id").length;
    rows.append(
      el(
        "div.model-provider-row",
        {},
        select<string>({
          value: provider,
          options: optionsFor(provider),
          searchable: true,
          onchange: (next) => {
            if (!next || next === provider) return;
            if (Object.hasOwn(current, next)) {
              toast(t("{name} already exists", { name: next }), "error");
              return;
            }
            save(renameConfigKey(current, provider, next));
          },
        }),
        el("input.input.model-id", {
          value: modelId,
          placeholder: t("Provider model ID"),
          "aria-label": t("Provider model ID"),
          onchange: (event) => {
            if (!(event.target instanceof HTMLInputElement)) return;
            save({ ...current, [provider]: { ...spec, model_id: event.target.value.trim() } });
          },
        }),
        extraCount
          ? el(
              "span.model-provider-extra",
              { title: t("Edit provider-specific parameters in JSON mode") },
              t("{n} extra · JSON", { n: extraCount }),
            )
          : el("span.model-provider-extra.empty", { "aria-hidden": "true" }),
        iconBtn(
          "trash",
          t("Remove"),
          () => {
            const next = { ...current };
            delete next[provider];
            save(next);
          },
          "danger",
        ),
      ),
    );
  }
  let draftProvider = "";
  const modelInput = el("input.input.model-id", {
    placeholder: t("Provider model ID"),
    "aria-label": t("Provider model ID"),
  });
  const commit = async (): Promise<void> => {
    const modelId = modelInput.value.trim();
    if (!draftProvider || !modelId) return;
    await save({ ...current, [draftProvider]: { model_id: modelId } });
    queueMicrotask(() =>
      requiredElement("workspace")
        .querySelector<HTMLElement>(`[data-model-provider-draft="${CSS.escape(cmKey(dictSegs))}"] button`)
        ?.focus(),
    );
  };
  modelInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    void commit();
  });
  modelInput.addEventListener("change", () => void commit());
  rows.append(
    el(
      "div.model-provider-row.draft",
      { dataset: { modelProviderDraft: cmKey(dictSegs) } },
      select<string>({
        value: "",
        options: optionsFor("", true).filter((option) => !option.value || !Object.hasOwn(current, option.value)),
        searchable: true,
        onchange: (next) => {
          draftProvider = next;
          modelInput.focus();
        },
      }),
      modelInput,
      el("span.model-provider-extra.empty", { "aria-hidden": "true" }),
      el("span.kv-draft-tail", { "aria-hidden": "true" }, icon("plus")),
    ),
  );
  return el(
    "div.model-provider-editor",
    {},
    rows,
    el("div.form-help", {}, t("Common model IDs are edited here; provider-specific parameters stay in JSON mode.")),
  );
}

function jsonControl(value: unknown, label: string, save: SaveValue): UiElement {
  const textarea = el("textarea.textarea", { value: JSON.stringify(value, null, 2), "aria-label": label });
  textarea.addEventListener("change", () => {
    try {
      const parsed = JSON.parse(textarea.value);
      textarea.classList.remove("invalid");
      save(parsed);
    } catch {
      textarea.classList.add("invalid");
      toast(t("Invalid JSON"), "error");
    }
  });
  return textarea;
}

function textControl(
  _state: DashboardState,
  fullKey: string,
  label: string,
  value: unknown,
  save: SaveValue,
  isSecret: boolean,
): UiElement {
  const input = el("input.input", {
    value: value == null ? "" : String(value),
    type: isSecret && !local.revealed.has(fullKey) ? "password" : "text",
    "aria-label": label,
    placeholder: value === null ? "null" : "",
  });
  if (isSecret && !local.revealed.has(fullKey)) input.classList.add("secret-mask");
  // Coerce by the CURRENT value type: string keys keep strings, only null keys infer a fresh type.
  input.addEventListener("change", () => {
    const raw = input.value;
    if (typeof value === "string") return save(raw);
    if (raw === "") return value === null ? undefined : save(null);
    if (raw === "true" || raw === "false") return save(raw === "true");
    if (/^-?(\d+\.?\d*|\.\d+)$/.test(raw)) return save(Number(raw));
    save(raw);
  });
  return input;
}

function buildControl(
  state: DashboardState,
  segs: readonly string[],
  label: string,
  value: unknown,
  save: SaveValue,
  isSecret: boolean,
  fullKey: string,
): UiElement {
  const options = referenceOptions(segs, value);
  if (options) {
    return select<string>({
      value: typeof value === "string" ? value : "",
      options,
      searchable: true,
      onchange: (next) => save(next),
    });
  }
  if (typeof value === "boolean") {
    return el("button.toggle", {
      role: "switch",
      "aria-checked": String(value),
      "aria-label": label,
      onclick: () => save(!value),
    });
  }
  if (typeof value === "number") {
    const input = el("input.input", { type: "number", step: "any", value: String(value), "aria-label": label });
    input.addEventListener("change", () => {
      if (input.value === "") return save(null);
      const numeric = Number(input.value);
      if (Number.isNaN(numeric)) {
        toast(t("{name} expects a number", { name: label }), "error");
        input.value = String(value);
        return;
      }
      save(numeric);
    });
    return input;
  }
  if (chipable(value)) return chipsEditor({ values: value, label, save });
  if (value !== null && typeof value === "object") return jsonControl(value, label, save);
  return textControl(state, fullKey, label, value, save, isSecret);
}

function formRow(
  state: DashboardState,
  segs: readonly string[],
  relSegs: readonly string[],
  value: unknown,
  raw = false,
): UiElement {
  const leaf = relSegs[relSegs.length - 1] ?? "";
  if (typeof value === "string" && ENV_FIRST_FIELD.test(leaf)) {
    return envFieldRow(state, segs.slice(0, -1), [leaf], leaf, value, SECRET_KEY.test(leaf));
  }
  // Advanced rows keep raw mono key names; business rows get pretty labels.
  const label = raw || relSegs.length > 1 ? relSegs.join(".") : prettyLabel(leaf);
  const fullKey = cmKey(segs);
  const origin = local.origins[fullKey] || originOf(segs);
  const isSecret = SECRET_KEY.test(leaf);
  const save = makeSave(state, fullKey, label);
  const isComplex = value !== null && typeof value === "object" && !chipable(value);

  const ops = el("div.form-ops");
  if (isSecret && !isComplex) {
    ops.append(
      iconBtn(
        local.revealed.has(fullKey) ? "eyeOff" : "eye",
        local.revealed.has(fullKey) ? t("Hide value") : t("Reveal value"),
        () => {
          local.revealed.has(fullKey) ? local.revealed.delete(fullKey) : local.revealed.add(fullKey);
          renderMain(requiredElement("workspace"), state);
        },
      ),
    );
  }
  ops.append(
    iconBtn("copy", t("Copy {name}", { name: label }), () =>
      copyText(isComplex ? JSON.stringify(value, null, 2) : String(value ?? ""), t("Copied {name}", { name: label })),
    ),
  );
  if (origin === "override") ops.append(iconBtn("rotateCcw", t("Reset to default"), () => resetKey(state, fullKey)));
  if (origin === "custom")
    ops.append(iconBtn("trash", t("Delete custom key"), () => resetKey(state, fullKey), "danger"));

  const labelNode = el(`span.form-label-text${raw || relSegs.length > 1 ? ".mono" : ""}`, {}, label);
  attachTooltip(labelNode, () =>
    el(
      "div",
      {},
      el("div.tt-title", {}, leaf),
      el("div.tt-row", {}, el("span.k", {}, t("path")), el("span.v", {}, fullKey)),
      el("div.tt-row", {}, el("span.k", {}, t("origin")), el("span.v", {}, t(origin))),
    ),
  );
  const help = FIELD_HELP[segs.join(".")] ?? FIELD_HELP[leaf];
  return el(
    "div.form-row",
    {},
    el("div.form-label", {}, labelNode, origin !== "builtin" ? originBadge(origin) : null, el("span.spacer"), ops),
    el("div.form-control", {}, buildControl(state, segs, label, value, save, isSecret, fullKey)),
    help ? el("div.form-help", {}, t(help)) : null,
  );
}

// Read-only glance line for a workspace preset's backends: "main · sqlite | vec · inmem".
function backendsSummary(backends: unknown): UiElement {
  const chips = Object.entries(configRecord(backends)).map(([name, spec]) =>
    el("span.chip-tag", {}, `${name} · ${String(configRecord(spec).type ?? "?")}`),
  );
  return el(
    "div.form-row",
    {},
    el("div.form-label", {}, el("span.form-label-text", {}, t("Backends"))),
    el("div.chips", { style: { border: "none", padding: "0", background: "transparent" } }, chips),
  );
}

// Provider backend routing is a derived runtime view: a gateway-specific
// mapping wins, then `default`, then the provider identifier. Configuration
// mode explains every effective route while JSON mode remains the one editor.
function providerBackendSummary(entrySegs: readonly string[], rawBackend: unknown): UiElement {
  const provider = entrySegs[entrySegs.length - 1] ?? "";
  const mapping = configRecord(rawBackend);
  const scalar = typeof rawBackend === "string" && rawBackend ? rawBackend : "";
  const fallback = String(mapping.default || scalar || provider || "?");
  const gateways = Object.keys(configRecord(walk(["llm", "gateways"])));
  const routes = [
    { gateway: t("Default route"), backend: fallback, inherited: false },
    ...gateways.map((gateway) => ({
      gateway,
      backend: String(mapping[gateway] || fallback),
      inherited: mapping[gateway] === undefined,
    })),
  ];
  return el(
    "div.backend-route-summary",
    {},
    routes.map((route) =>
      el(
        `span.backend-route-chip${route.inherited ? ".inherited" : ""}`,
        {},
        el("span.backend-route-gateway", {}, route.gateway),
        icon("chevronRight"),
        brandIcon(route.backend),
        el("span.backend-route-value", {}, route.backend),
      ),
    ),
  );
}

// One field row driven by a collection schema. `value` is undefined when the entry
// omits the field, in which case the control renders empty and no origin ops show.
function fieldPath(field: SchemaField): readonly string[] {
  return field.path ?? [field.key];
}

function nestedValue(node: unknown, path: readonly string[]): unknown {
  let value = node;
  for (const part of path) value = configRecord(value)[part];
  return value;
}

function reasoningInherited(entrySegs: readonly string[], key: "think" | "reasoning_effort"): unknown {
  const globalValue = configRecord(walk(["llm", "default_args"]))[key];
  const entry = configRecord(walk(entrySegs));
  let modelName = typeof entry.model === "string" ? entry.model : "";
  if (entrySegs[0] === "dashboard" && typeof entry.preset === "string" && entry.preset) {
    const preset = configRecord(walk(["llm", "presets", entry.preset]));
    const presetValue = configRecord(preset.default_args)[key];
    if (presetValue !== undefined) return presetValue;
    modelName = typeof preset.model === "string" ? preset.model : modelName;
  }
  if (entrySegs[0] === "llm" && entrySegs[1] === "models") {
    return globalValue;
  }
  if (modelName) {
    const models = configRecord(walk(["llm", "models"]));
    let model = configRecord(models[modelName]);
    if (!Object.keys(model).length) {
      model = configRecord(Object.values(models).find((spec) => aliasesOf(spec).includes(modelName)));
    }
    const modelValue = configRecord(model.default_args)[key];
    if (modelValue !== undefined) return modelValue;
  }
  return globalValue;
}

function schemaRow(state: DashboardState, entrySegs: readonly string[], field: SchemaField, value: unknown): UiElement {
  if (field.control.startsWith("env-") && (value === undefined || typeof value === "string")) {
    return envFieldRow(state, entrySegs, fieldPath(field), field.key, value, field.control === "env-secret");
  }
  const label = prettyLabel(field.key);
  const segs = [...entrySegs, ...fieldPath(field)];
  const fullKey = cmKey(segs);
  const present = value !== undefined;
  const origin = present ? local.origins[fullKey] || originOf(segs) : null;
  const isSecret = field.control === "secret" || field.control === "env-secret";
  const save = makeSave(state, fullKey, label);

  let control: UiElement;
  if (field.control === "kv") {
    control = kvEditor(state, segs, value, label, field.key === "default_args" ? ["think", "reasoning_effort"] : []);
  } else if (field.control === "backend-summary") {
    control = providerBackendSummary(entrySegs, value);
  } else if (field.control === "model-providers") {
    control = modelProvidersEditor(state, segs, value, label);
  } else if (field.control === "credential-access") {
    control = el("button.toggle", {
      role: "switch",
      "aria-checked": String(value === true),
      "aria-label": label,
      onclick: () => {
        if (value === true) {
          save(false);
          return;
        }
        const acknowledge = el("input", { type: "checkbox" });
        modal({
          title: t("Grant assistant access to credentials?"),
          body: el(
            "div",
            { style: { display: "grid", gap: "14px", maxWidth: "520px" } },
            el(
              "div",
              { role: "alert", style: { color: "var(--danger)", fontWeight: "650", lineHeight: "1.5" } },
              t(
                "Security warning: get_config may send raw API keys, tokens, passwords, private keys, and other credentials to the external model provider.",
              ),
            ),
            el(
              "div",
              { style: { lineHeight: "1.5" } },
              t("Enable this only for a trusted model provider and agent, then disable it when the task finishes."),
            ),
            el(
              "label",
              { style: { display: "flex", gap: "9px", alignItems: "flex-start", cursor: "pointer" } },
              acknowledge,
              el(
                "span",
                {},
                t("I understand credentials may leave this machine and be retained by the external provider."),
              ),
            ),
          ),
          actions: [
            { label: t("Cancel") },
            {
              label: t("Grant credential access"),
              danger: true,
              onclick: async () => {
                if (!acknowledge.checked) {
                  toast(t("Confirm that you understand the credential exposure risk"), "error");
                  acknowledge.focus();
                  return false;
                }
                await save(true);
                return undefined;
              },
            },
          ],
        });
      },
    });
  } else if (field.control === "chips") {
    control = chipsEditor({ values: chipable(value) ? value : [], label, save });
  } else if (field.control === "bool") {
    control = el("button.toggle", {
      role: "switch",
      "aria-checked": String(value === true),
      "aria-label": label,
      onclick: () => save(value !== true),
    });
  } else if (field.control === "inherited-bool") {
    const inherited = reasoningInherited(entrySegs, "think") === true;
    const effective = present ? value === true : inherited;
    control = el(
      "div.inherited-control",
      {},
      el("button.toggle", {
        role: "switch",
        "aria-checked": String(effective),
        "aria-label": label,
        dataset: { inherited: String(!present) },
        onclick: () => save(!effective),
      }),
      !present
        ? el("span.inherited-value", {}, t("Inherited · {name}", { name: effective ? t("On") : t("Off") }))
        : null,
    );
  } else if (field.control === "inherited-text") {
    const inherited = reasoningInherited(entrySegs, "reasoning_effort");
    const input = el("input.input", {
      value: present && value != null ? String(value) : "",
      placeholder:
        inherited == null || inherited === "" ? t("Inherit") : t("Inherited · {name}", { name: String(inherited) }),
      "aria-label": label,
    });
    input.addEventListener("change", () => {
      const next = input.value.trim();
      if (!next) {
        if (present) void resetKey(state, fullKey);
        return;
      }
      void save(next);
    });
    control = input;
  } else if (field.control === "number") {
    const input = el("input.input", {
      type: "number",
      step: "any",
      value: value == null ? "" : String(value),
      "aria-label": label,
    });
    input.addEventListener("change", () => {
      if (input.value === "") {
        if (present) void save(null);
        return;
      }
      const numeric = Number(input.value);
      if (Number.isNaN(numeric)) {
        toast(t("{name} expects a number", { name: label }), "error");
        input.value = value == null ? "" : String(value);
        return;
      }
      save(numeric);
    });
    control = input;
  } else if (field.control.startsWith("ref:")) {
    control = select<string>({
      value: typeof value === "string" ? value : "",
      options: refOptions(field.control.slice(4), value, entrySegs),
      searchable: true,
      onchange: (next) => (next === "" ? resetKey(state, fullKey) : save(next)),
    });
  } else {
    control =
      value !== null && typeof value === "object"
        ? jsonControl(value, label, save)
        : textControl(state, fullKey, label, value ?? null, save, isSecret);
  }

  const ops = el("div.form-ops");
  if (isSecret && present) {
    ops.append(
      iconBtn(
        local.revealed.has(fullKey) ? "eyeOff" : "eye",
        local.revealed.has(fullKey) ? t("Hide value") : t("Reveal value"),
        () => {
          local.revealed.has(fullKey) ? local.revealed.delete(fullKey) : local.revealed.add(fullKey);
          renderMain(requiredElement("workspace"), state);
        },
      ),
    );
  }
  if (present && field.control !== "kv" && field.control !== "backend-summary") {
    ops.append(
      iconBtn("copy", t("Copy {name}", { name: label }), () =>
        copyText(
          typeof value === "object" ? JSON.stringify(value, null, 2) : String(value ?? ""),
          t("Copied {name}", { name: label }),
        ),
      ),
    );
  }
  if (field.control !== "backend-summary" && origin === "override")
    ops.append(iconBtn("rotateCcw", t("Reset to default"), () => resetKey(state, fullKey)));
  if (field.control !== "backend-summary" && origin === "custom")
    ops.append(iconBtn("trash", t("Delete custom key"), () => resetKey(state, fullKey), "danger"));

  const help = FIELD_HELP[[...entrySegs, ...fieldPath(field)].join(".")] || FIELD_HELP[field.key];
  return el(
    "div.form-row",
    {},
    el(
      "div.form-label",
      {},
      el("span.form-label-text", {}, label),
      origin && origin !== "builtin" ? originBadge(origin) : null,
      el("span.spacer"),
      ops,
    ),
    el("div.form-control", {}, control),
    help ? el("div.form-help", {}, t(help)) : null,
  );
}

// Source-aware provider fields share one control family. Their behavior is
// keyed by field semantics, never by a list of built-in provider names.
function envFieldRow(
  state: DashboardState,
  entrySegs: readonly string[],
  relativePath: readonly string[],
  fieldKey: string,
  value: unknown,
  secret: boolean,
): UiElement {
  const entryName = entrySegs[entrySegs.length - 1] ?? entrySegs[0] ?? "";
  const label = prettyLabel(fieldKey);
  const segs = [...entrySegs, ...relativePath];
  const fullKey = cmKey(segs);
  const origin = value !== undefined ? local.origins[fullKey] || originOf(segs) : null;
  const raw = typeof value === "string" ? value : "";
  const reference = parseEnvReference(raw);
  const envVar = reference?.name ?? defaultEnvVariable(entryName, fieldKey);
  const fallback = reference?.fallback ?? raw;
  const save = makeSave(state, fullKey, label);
  const envNameInput = el("input.input.source-name", {
    value: envVar,
    placeholder: t("Environment variable"),
    "aria-label": t("Environment variable"),
  });
  const input = el("input.input.source-manual", {
    type: secret ? "password" : "text",
    value: fallback,
    placeholder: t("Manual fallback"),
    autocomplete: "off",
    "aria-label": t("Manual fallback"),
    dataset: { fieldInput: fullKey },
  });
  const commit = (): void => {
    const name = envNameInput.value.trim() || defaultEnvVariable(entryName, fieldKey);
    void save(`\${env:${name},${JSON.stringify(input.value)}}`);
  };
  envNameInput.addEventListener("change", commit);
  input.addEventListener("change", commit);
  const inputWrap = el("div.source-value");
  inputWrap.append(input);
  if (secret) {
    const revealButton = iconBtn("eye", t("Reveal value"), () => {
      const revealing = input.type === "password";
      input.type = revealing ? "text" : "password";
      const nextLabel = revealing ? t("Hide value") : t("Reveal value");
      revealButton.title = nextLabel;
      revealButton.setAttribute("aria-label", nextLabel);
      revealButton.replaceChildren(icon(revealing ? "eyeOff" : "eye"));
    });
    inputWrap.append(revealButton);
  }
  const status = el(
    "div.source-status.manual",
    { role: "status" },
    icon("info"),
    el("span", {}, t("Resolving current value…")),
  );
  const renderStatus = (kind: "success" | "manual", message: string): void => {
    status.className = `source-status ${kind}`;
    status.replaceChildren(icon(kind === "success" ? "check" : "info"), el("span", {}, message));
  };
  api
    .configValueStatus(fullKey, !secret)
    .then((info) => {
      if (!status.isConnected) return;
      const finalValue = secret
        ? info.set
          ? `•••• ${info.tail || ""}`.trim()
          : t("Not set")
        : info.value || t("Not set");
      if (info.source === "environment") {
        renderStatus(
          "success",
          t("Using environment · {name} · {value}", { name: info.env_var || envVar, value: finalValue }),
        );
      } else {
        renderStatus("manual", t("Using manual value · {value}", { value: finalValue }));
      }
    })
    .catch(() => renderStatus("manual", t("Could not resolve current value")));

  const ops = el("div.form-ops");
  if (origin === "override") ops.append(iconBtn("rotateCcw", t("Reset to default"), () => resetKey(state, fullKey)));
  if (origin === "custom")
    ops.append(iconBtn("trash", t("Delete custom key"), () => resetKey(state, fullKey), "danger"));

  const help = FIELD_HELP[fieldKey];
  return el(
    "div.form-row",
    {},
    el(
      "div.form-label",
      {},
      el("span.form-label-text", {}, label),
      origin && origin !== "builtin" ? originBadge(origin) : null,
      el("span.spacer"),
      ops,
    ),
    el(
      "div.form-control",
      {},
      el(
        "div.source-field",
        {},
        el(
          "div.source-field-labels",
          {},
          el("span", {}, t("Environment variable (preferred)")),
          el("span", {}, t("Manual fallback")),
        ),
        el("div.source-field-row", {}, envNameInput, inputWrap),
        status,
      ),
    ),
    help ? el("div.form-help", {}, t(help)) : null,
  );
}

// Inline "+ Add" row for open settings pages (defaults, dashboard): name a key and create it.
function addKeyRow(state: DashboardState, baseSegs: readonly string[]): UiElement {
  const input = el("input.input", {
    placeholder: t("New key"),
    style: { maxWidth: "220px" },
    "aria-label": t("New key"),
  });
  const add = (): void => {
    const key = input.value.trim();
    if (key) makeSave(state, cmKey([...baseSegs, key]), key)("");
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") add();
  });
  return el(
    "div.form-row.add-key",
    {},
    el("div.form-label", {}, el("span.form-label-text", { style: { color: "var(--muted)" } }, t("Add setting"))),
    el("div.form-control", { style: { gap: "6px", alignItems: "center" } }, input, iconBtn("plus", t("Add"), add)),
  );
}

function entryForm(state: DashboardState, baseSegs: readonly string[], node: unknown): UiElement {
  const body = el("div.panel-body.form-body");
  const isObj = node !== null && typeof node === "object" && !Array.isArray(node);
  const objectNode = configRecord(node);
  const schemaKey = baseSegs.length === 1 ? (baseSegs[0] ?? "") : baseSegs.slice(0, -1).join(".");
  const schema = ENTRY_SCHEMAS[schemaKey]; // collection entries only
  const groups = ENTRY_GROUPS[schemaKey];

  // Schema-driven: every entry of the collection shows the same unified field set.
  if (schema && isObj) {
    body.classList.add("schema-form");
    if (baseSegs[0] === "workspace" && objectNode.backends && typeof objectNode.backends === "object") {
      body.append(backendsSummary(objectNode.backends));
    }
    const known = new Set(schema.map((field) => fieldPath(field)[0] ?? field.key));
    if (groups) {
      const byKey = new Map(schema.map((field) => [field.key, field]));
      for (const group of groups) {
        const groupFields = group.fields
          .map((key) => byKey.get(key))
          .filter((field): field is SchemaField => field !== undefined);
        const rows = groupFields.map((field) =>
          schemaRow(state, baseSegs, field, nestedValue(objectNode, fieldPath(field))),
        );
        const hasConfiguredValue = groupFields.some((field) => nestedValue(objectNode, fieldPath(field)) !== undefined);
        if (group.collapsible) {
          body.append(
            el(
              "details.config-form-section.config-form-advanced",
              { open: (group.label === "Alternative connection fields" && hasConfiguredValue) || undefined },
              el("summary.config-section-head", {}, icon("chevronRight"), el("span", {}, t(group.label))),
              el("div.config-section-rows", {}, rows),
            ),
          );
        } else {
          body.append(
            el(
              "section.config-form-section",
              { "aria-label": t(group.label) },
              el("div.config-section-head", {}, t(group.label)),
              el("div.config-section-rows", {}, rows),
            ),
          );
        }
      }
    } else {
      for (const field of schema)
        body.append(schemaRow(state, baseSegs, field, nestedValue(objectNode, fieldPath(field))));
    }
    if (schemaKey === "llm.providers" && objectNode.allow_unknown_models === true) {
      body.append(
        el(
          "div.config-json-only-note",
          {},
          icon("info"),
          el("span", {}, t("Unlisted model names are enabled · edit this advanced policy in JSON mode.")),
        ),
      );
    }
    const extra = Object.entries(objectNode).filter(([key]) => !known.has(key));
    if (extra.length) {
      body.append(
        el(
          "details.config-form-section.config-form-advanced",
          {},
          el("summary.config-section-head", {}, icon("chevronRight"), el("span", {}, t("Additional settings"))),
          el(
            "div.config-section-rows",
            {},
            extra.map(([key, value]) => formRow(state, [...baseSegs, key], [key], value, true)),
          ),
        ),
      );
    }
    return body;
  }

  // Generic pages (Defaults, dashboard, query, …): one row per leaf plus an add-key affordance.
  body.classList.add("schema-form");
  const hiddenDashboardKeys =
    baseSegs.length === 1 && baseSegs[0] === "dashboard" ? new Set(["assistant", "preferences"]) : null;
  const rows = flatten(isObj ? node : { value: node }).filter((row) => !hiddenDashboardKeys?.has(row.segs[0] ?? ""));
  if (!rows.length && !isObj) {
    body.append(el("div", { style: { padding: "16px 0", color: "var(--muted)" } }, t("No values")));
    return body;
  }
  const simple = rows.filter(({ value }) => value === null || typeof value !== "object" || chipable(value));
  const advanced = rows.filter((row) => !simple.includes(row));
  const rank = (row: FlatRow): number => {
    const index = PRIORITY.indexOf(row.segs[0] ?? "");
    return index === -1 ? PRIORITY.length : index;
  };
  simple.sort((a, b) => rank(a) - rank(b) || a.segs.join(".").localeCompare(b.segs.join(".")));
  if (baseSegs[0] === "workspace" && objectNode.backends && typeof objectNode.backends === "object") {
    body.append(backendsSummary(objectNode.backends));
  }
  if (simple.length) {
    body.append(
      el(
        "section.config-form-section",
        { "aria-label": t("Settings") },
        el("div.config-section-head", {}, t("Settings")),
        el(
          "div.config-section-rows",
          {},
          simple.map((row) => formRow(state, [...baseSegs, ...row.segs], row.segs, row.value)),
        ),
      ),
    );
  }
  if (advanced.length) {
    body.append(
      el(
        "details.config-form-section.config-form-advanced",
        {},
        el("summary.config-section-head", {}, icon("chevronRight"), el("span", {}, t("Additional settings"))),
        el(
          "div.config-section-rows",
          {},
          advanced.map((row) => formRow(state, [...baseSegs, ...row.segs], row.segs, row.value, true)),
        ),
      ),
    );
  }
  if (isObj) {
    body.append(
      el(
        "section.config-form-section.config-form-add",
        { "aria-label": t("Add setting") },
        el("div.config-section-rows", {}, addKeyRow(state, baseSegs)),
      ),
    );
  }
  return body;
}

// Flatten one level deep: scalars and shallow dicts become leaf rows; deep dicts and lists become one
// JSON row. Each row carries its segment array (relative to the editor base) so dotted keys stay intact.
function flatten(node: unknown, prefix: string[] = []): FlatRow[] {
  if (node === null || typeof node !== "object" || Array.isArray(node)) return [{ segs: prefix, value: node }];
  const rows: FlatRow[] = [];
  for (const [key, value] of Object.entries(node)) {
    const segs = [...prefix, key];
    if (value !== null && typeof value === "object" && !Array.isArray(value) && depthOf(value) > 1) {
      rows.push({ segs, value });
    } else if (value !== null && typeof value === "object" && !Array.isArray(value)) {
      rows.push(...Object.entries(value).map(([leaf, leafValue]) => ({ segs: [...segs, leaf], value: leafValue })));
    } else {
      rows.push({ segs, value });
    }
  }
  return rows;
}

function depthOf(node: unknown, depth = 0): number {
  if (node === null || typeof node !== "object" || Array.isArray(node)) return depth;
  let max = depth;
  for (const value of Object.values(configRecord(node))) {
    max = Math.max(max, depthOf(value, depth + 1));
  }
  return max;
}

function kvRow(state: DashboardState, segs: readonly string[], label: string, value: unknown): UiElement {
  const fullKey = cmKey(segs);
  const origin = local.origins[fullKey] || originOf(segs);
  const isSecret = SECRET_KEY.test(label);
  const isComplex = value !== null && typeof value === "object";
  const save = makeSave(state, fullKey, label);
  const editor = buildControl(state, segs, label, value, save, isSecret, fullKey);

  const ops = el("div.kv-ops");
  if (isSecret && !isComplex) {
    ops.append(
      iconBtn(
        local.revealed.has(fullKey) ? "eyeOff" : "eye",
        local.revealed.has(fullKey) ? t("Hide value") : t("Reveal value"),
        () => {
          local.revealed.has(fullKey) ? local.revealed.delete(fullKey) : local.revealed.add(fullKey);
          renderMain(requiredElement("workspace"), state);
        },
      ),
    );
  }
  ops.append(
    iconBtn("copy", t("Copy {name}", { name: label }), () =>
      copyText(isComplex ? JSON.stringify(value, null, 2) : String(value ?? ""), t("Copied {name}", { name: label })),
    ),
  );
  if (origin === "override") ops.append(iconBtn("rotateCcw", t("Reset to default"), () => resetKey(state, fullKey)));
  if (origin === "custom")
    ops.append(iconBtn("trash", t("Delete custom key"), () => resetKey(state, fullKey), "danger"));

  const keyNode = el("span.kv-key", {}, label); // no native title: the themed tooltip below already shows the full path
  attachTooltip(keyNode, () =>
    el(
      "div",
      {},
      el("div.tt-title", {}, label),
      el("div.tt-row", {}, el("span.k", {}, t("path")), el("span.v", {}, fullKey)),
      el("div.tt-row", {}, el("span.k", {}, t("origin")), el("span.v", {}, t(origin))),
    ),
  );
  return el("div.kv-row", {}, keyNode, originBadge(origin), el("div.kv-val", {}, editor), ops);
}

async function resetKey(state: DashboardState, fullKey: string): Promise<void> {
  try {
    await api.configReset(fullKey, local.scope ?? undefined);
    toast(t("Reset"));
    await loadTree();
    renderMain(requiredElement("workspace"), state);
  } catch (error) {
    toast(errorMessage(error), "error");
  }
}

// ---------------------------------------------------------------- history

async function showHistory(): Promise<void> {
  let history: ConfigHistoryResponse = [];
  try {
    history = await api.configHistory({ scope: local.scope, limit: 30 });
  } catch (error) {
    toast(errorMessage(error), "error");
    return;
  }
  const diffZone = el("div.diff-view", { style: { display: "none" } });
  const list = el(
    "div",
    { style: { display: "flex", flexDirection: "column", gap: "2px", maxHeight: "30vh", overflowY: "auto" } },
    history.map((row, index) =>
      el(
        "div.tree-item.history-item",
        {},
        el(
          "button.history-main",
          {
            type: "button",
            onclick: async () => {
              try {
                const data = await api.configDiff({ scope_a: local.scope, version_a: row.ver, scope_b: local.scope });
                renderDiff(diffZone, data.diff || t("No differences with the current layer."));
              } catch (error) {
                toast(errorMessage(error), "error");
              }
            },
          },
          icon("history"),
          el("span.name.mono", {}, `v${row.ver}`),
          el("span.count", {}, `${row.created_at || ""}${index === 0 ? ` · ${t("latest")}` : ""}`),
        ),
        el(
          "button.icon-btn",
          {
            type: "button",
            title: t("Browse v{name} values", { name: row.ver }),
            "aria-label": t("Browse v{name} values", { name: row.ver }),
            style: { width: "20px", height: "20px" },
            onclick: async (event) => {
              event.stopPropagation();
              try {
                const data = await api.configTree({ scope: local.scope, version: row.ver });
                renderDiff(diffZone, JSON.stringify(data.tree, null, 2));
              } catch (error) {
                toast(errorMessage(error), "error");
              }
            },
          },
          icon("eye"),
        ),
      ),
    ),
  );
  modal({
    title: t("History · {name}", { name: local.scope }),
    body: el(
      "div",
      { style: { display: "flex", flexDirection: "column", gap: "10px" } },
      history.length ? list : el("div", { style: { color: "var(--muted)" } }, t("No retained versions")),
      diffZone,
    ),
  });
}

function renderDiff(zone: UiElement, text: string): void {
  zone.style.display = "block";
  zone.replaceChildren(
    ...text.split("\n").map((line) => {
      const cls = line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : "";
      return el(`div${cls ? `.${cls}` : ""}`, {}, line || " ");
    }),
  );
}
