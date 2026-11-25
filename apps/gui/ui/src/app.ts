// Dashboard shell: rail navigation, theme, panels, status strip, boot (TypeScript source).

import {
  api,
  type DashboardPreferencesRequest,
  type DashboardPreferencesResponse,
  type JsonRecord,
  type MetaResponse,
} from "./api.js";
import { icon } from "./icons.js";
import { currentLang, setLang, t } from "./i18n.js";
import { el, iconBtn, inlineValue, modal, setBusy, toast, type UiElement } from "./ui.js";
import type { DashboardState, DashboardView, ThemeSpec } from "./types.js";
import * as dataView from "./view_data.js";
import * as configView from "./view_config.js";
import { openPalette } from "./palette.js";
import { mountAssistant, setAssistantScope, syncAssistantForInspector } from "./assistant.js";
import { browserPanelBounds } from "./gui-logic.js";
import { initializeUiRuntime } from "./main.js";

initializeUiRuntime();

const THEMES = [
  { cls: "theme-light", name: "Light", icon: "sun" },
  { cls: "theme-dark", name: "Dark", icon: "moon" },
  { cls: "theme-anthropic", name: "Warm", icon: "flame" },
] as const satisfies readonly ThemeSpec[];
const DEFAULT_PREFERENCES = {
  theme: "theme-light",
  language: "en",
  font: "default",
  font_scale: "default",
} as const;

function requiredElement(id: string): UiElement {
  const node = document.getElementById(id);
  if (!(node instanceof HTMLElement)) throw new Error(`dashboard shell is missing #${id}`);
  return node as UiElement;
}

// The concentric-wide mark ships a light (light/warm themes) and a dark
// (dark theme) surface; sizes 64 (rail/favicon), 128 (About/apple-touch),
// and squircle .ico for Chromium --app / tab chrome that prefers ICO.
function logoTone(themeCls: string): "dark" | "light" {
  return themeCls === "theme-dark" ? "dark" : "light";
}

function logoPath(themeCls: string, size: 64 | 128): string {
  return `/assets/heavenbase-logo-concentric-wide-${logoTone(themeCls)}-${size}.png`;
}

function logoIco(themeCls: string): string {
  return `/assets/heavenbase-logo-concentric-wide-${logoTone(themeCls)}-squircle.ico`;
}

export const state: DashboardState = {
  route: location.hash.replace(/^#\/?/, "") || "data",
  wsId: null,
  health: null,
  refreshStatus: () => renderStatus(),
  openInspector: (content: Node | string, title?: string) => openInspector(content, title),
  closeInspector: () => closeInspector(),
  openEntity: (entityId: string, options) => dataView.openEntityById(state, entityId, options),
  navigate: (route: string) => navigate(route),
  assistContext: () => (state.route === "data" ? dataView.assistContext() : { route: state.route }),
  setAssistantScope: (scope: string | null) => setAssistantScope(scope),
  // Shared appearance preferences, consumed by the Config → Dashboard → Appearance page.
  appearance: {
    themes: () => THEMES,
    theme: () => currentTheme(),
    setTheme: async (cls) => {
      await savePreferences({ theme: cls });
      renderRail();
    },
    lang: () => currentLang(),
    setLang: async (next) => {
      await savePreferences({ language: next });
      render();
    },
    font: () => currentFont(),
    setFont: async (name) => savePreferences({ font: name }),
    fontScale: () => currentFontScale(),
    setFontScale: async (name) => savePreferences({ font_scale: name }),
  },
};

const VIEWS: Readonly<Record<string, DashboardView>> = { data: dataView, config: configView };

// ------------------------------------------------------------------ theme

function currentTheme(): ThemeSpec["cls"] {
  const stored = localStorage.getItem("hb-theme");
  return THEMES.find((theme) => theme.cls === stored)?.cls ?? "theme-light";
}

function applyTheme(cls: string): void {
  // Swap only the theme token so unrelated body state (e.g. inspector-open,
  // which parks the assistant clear of the docked inspector) survives.
  for (const theme of THEMES) document.body.classList.toggle(theme.cls, theme.cls === cls);
  localStorage.setItem("hb-theme", cls);
  syncFavicon(cls);
}

function applyPreferences(preferences: DashboardPreferencesResponse): void {
  applyTheme(preferences.theme);
  setLang(preferences.language);
  applyFont(preferences.font);
  applyFontScale(preferences.font_scale);
}

function legacyPreferences(): DashboardPreferencesRequest {
  let update: DashboardPreferencesRequest = {};
  const theme = localStorage.getItem("hb-theme");
  const language = localStorage.getItem("hb-lang");
  const font = localStorage.getItem("hb-font");
  const fontScale = localStorage.getItem("hb-font-scale");
  if (theme === "theme-dark" || theme === "theme-anthropic") update = { ...update, theme };
  if ((language === "en" || language === "zh") && language !== DEFAULT_PREFERENCES.language)
    update = { ...update, language };
  if ((font === "small" || font === "default" || font === "large") && font !== DEFAULT_PREFERENCES.font)
    update = { ...update, font };
  if (
    (fontScale === "small" || fontScale === "default" || fontScale === "large" || fontScale === "xlarge") &&
    fontScale !== DEFAULT_PREFERENCES.font_scale
  )
    update = { ...update, font_scale: fontScale };
  return update;
}

async function savePreferences(update: DashboardPreferencesRequest): Promise<void> {
  try {
    applyPreferences(await api.updatePreferences(update));
  } catch (error) {
    toast(error instanceof Error ? error.message : String(error), "error");
    throw error;
  }
}

async function chooseTheme(cls: ThemeSpec["cls"], name: string): Promise<void> {
  try {
    await state.appearance.setTheme(cls);
    toast(t("{name} theme", { name: t(name) }));
  } catch {
    // savePreferences already rendered the contextual failure.
  }
}

// Tab / --app icons follow the theme. Chromium --app windows that honor the
// page favicon pick these up; the OS dock icon still belongs to the browser
// binary when launched via ``--app=URL`` without an installed shortcut.
function syncFavicon(cls: string): void {
  for (const link of document.querySelectorAll<HTMLLinkElement>('link[rel="icon"], link[rel="apple-touch-icon"]')) {
    if (link.type === "image/x-icon" || link.href?.includes(".ico")) link.href = logoIco(cls);
    else if (link.rel === "apple-touch-icon") link.href = logoPath(cls, 128);
    else link.href = logoPath(cls, 64);
  }
}

// Theme slider: one persistent segmented control so the thumb ANIMATES
// between options (renderRail re-appends the same node instead of rebuilding
// it, keeping the transform transition alive across theme/language changes).
let themeSeg: { node: UiElement; thumb: UiElement } | null = null;

function buildThemeSeg(): UiElement {
  const thumb = el("span.theme-seg-thumb", { "aria-hidden": "true" });
  const node = el("div.theme-seg", { role: "radiogroup", "aria-label": t("Theme") }, thumb);
  for (const theme of THEMES) {
    node.append(
      el(
        "button.theme-seg-btn",
        {
          type: "button",
          role: "radio",
          onclick: () => {
            if (currentTheme() === theme.cls) return;
            void chooseTheme(theme.cls, theme.name);
          },
        },
        icon(theme.icon),
      ),
    );
  }
  themeSeg = { node, thumb };
  return node;
}

function syncThemeSeg(): void {
  if (!themeSeg) return;
  const index = Math.max(
    0,
    THEMES.findIndex((theme) => theme.cls === currentTheme()),
  );
  // 32px button + 2px gap per step; the thumb is one element whose transform
  // transitions, which is what makes the slider glide instead of jump.
  themeSeg.thumb.style.transform = `translateY(${index * 34}px)`;
  themeSeg.node.querySelectorAll(".theme-seg-btn").forEach((btn, i) => {
    const theme = THEMES[i];
    if (!theme) return;
    const label = t("{name} theme", { name: t(theme.name) });
    btn.title = label;
    btn.setAttribute("aria-label", label);
    btn.setAttribute("aria-checked", String(i === index));
    btn.classList.toggle("active", i === index);
  });
}

async function cycleLang(): Promise<void> {
  try {
    await state.appearance.setLang(currentLang() === "en" ? "zh" : "en");
    toast(currentLang() === "zh" ? "中文" : "English");
  } catch {
    // savePreferences already rendered the contextual failure.
  }
}

// ------------------------------------------------------------------- font

// Component font sizes are pinned in px, so the size preference scales the whole
// UI uniformly (like browser zoom) instead of only the few inheriting elements.
const FONTS = { small: "0.92", default: "1", large: "1.1" } as const;
type FontName = keyof typeof FONTS;

function currentFont(): FontName {
  const stored = localStorage.getItem("hb-font");
  return stored && stored in FONTS ? (stored as FontName) : "default";
}

function applyFont(name: string): void {
  const next: FontName = name in FONTS ? (name as FontName) : "default";
  document.body.style.zoom = FONTS[next];
  localStorage.setItem("hb-font", next);
}

// True text size: every stylesheet font-size derives from --font-scale, so this
// scales the type across the whole page (assistant included) without zooming layout.
const FONT_SCALES = { small: "0.9", default: "1", large: "1.15", xlarge: "1.3" } as const;
type FontScaleName = keyof typeof FONT_SCALES;

function currentFontScale(): FontScaleName {
  const stored = localStorage.getItem("hb-font-scale");
  return stored && stored in FONT_SCALES ? (stored as FontScaleName) : "default";
}

function applyFontScale(name: string): void {
  const next: FontScaleName = name in FONT_SCALES ? (name as FontScaleName) : "default";
  document.documentElement.style.setProperty("--font-scale", FONT_SCALES[next]);
  localStorage.setItem("hb-font-scale", next);
}

// ------------------------------------------------------------------- rail

function renderRail(): void {
  const rail = requiredElement("rail");
  rail.replaceChildren(
    el(
      "button.rail-logo",
      { type: "button", title: t("About HeavenBase"), "aria-label": t("About HeavenBase"), onclick: showAbout },
      el("img", { src: logoPath(currentTheme(), 64), alt: "HeavenBase", width: "24", height: "24" }),
    ),
    navBtn("data", "table", t("Data browser")),
    navBtn("config", "sliders", t("Config workbench")),
    iconBtn("search", t("Search workspace (⌘K)"), () => openPalette(state)),
    el("div.spacer"),
    iconBtn("globe", t("Language: {name}", { name: currentLang() === "en" ? "English" : "中文" }), cycleLang),
    themeSeg?.node ?? buildThemeSeg(),
  );
  syncThemeSeg();
}

// ------------------------------------------------------------------ about

// Read-only identity card: version comes from the server so it always matches
// the running package, never a stale frontend copy.
async function showAbout(): Promise<void> {
  let meta: MetaResponse | null = null;
  try {
    meta = await api.meta();
  } catch {
    // the card still opens with what the shell already knows (host, logo)
  }
  modal({
    title: t("About HeavenBase"),
    body: el(
      "div.about",
      {},
      el("img.about-logo", { src: logoPath(currentTheme(), 128), alt: "HeavenBase", width: "72", height: "72" }),
      el("div.about-name", {}, "HeavenBase"),
      meta?.version ? el("div.about-version", {}, `${t("Version")} ${meta.version}`) : null,
      el("div.about-desc", {}, t("Local dashboard · {host}", { host: location.host })),
    ),
  });
}

function navBtn(route: string, iconName: string, label: string): UiElement {
  return iconBtn(iconName, label, () => navigate(route), state.route === route ? "active" : "");
}

// ---------------------------------------------------------------- routing

export function navigate(route: string): void {
  state.route = route;
  location.hash = `#/${route}`;
  render();
}

window.addEventListener("hashchange", () => {
  const route = location.hash.replace(/^#\/?/, "") || "data";
  if (route.split("/")[0] !== state.route) {
    state.route = route.split("/")[0] ?? "data";
    render();
  }
});

// -------------------------------------------------------------- inspector

function openInspector(content: Node | string, title?: string): void {
  const panel = requiredElement("inspector");
  panel.hidden = false;
  document.body.classList.add("inspector-open"); // assistant bubble/panel sidestep the docked inspector
  syncAssistantForInspector();
  panel.replaceChildren(
    el(
      "div.panel-head",
      { style: { borderBottom: "1px solid var(--border)" } },
      el("span.title", {}, title || t("Inspector")),
      iconBtn("x", t("Close inspector"), closeInspector),
    ),
    content,
  );
}

function closeInspector(): void {
  const panel = requiredElement("inspector");
  panel.hidden = true;
  document.body.classList.remove("inspector-open");
  syncAssistantForInspector();
  panel.replaceChildren();
}

// ----------------------------------------------------------------- status

function renderStatus(): void {
  const status = requiredElement("status");
  const backends = state.health?.backends || [];
  const wsChip = el(
    "button.chip.chip-btn",
    {
      title: state.wsId ? t("Workspace health") : t("No workspace open"),
      style: { color: state.wsId ? "var(--success)" : "var(--subtle)" },
      onclick: () => state.wsId && showHealthInspector(),
    },
    icon("dot"),
    state.wsId ? `ws ${state.wsId}` : t("no workspace"),
  );
  status.replaceChildren(
    wsChip,
    ...backends.map((backend) =>
      el(
        "button.chip.chip-btn",
        {
          title: t("Backend {name} details", { name: backend.name }),
          onclick: () => showHealthInspector(backend.name),
        },
        icon("server"),
        `${backend.name}:${backend.type || backend.provider || "?"}`,
      ),
    ),
    el("span.spacer"),
    el("span.chip", {}, `${location.host} · ${t("local")}`),
  );
}

// ------------------------------------------------------------------ health

function capabilityRows(capabilities: JsonRecord): UiElement[] {
  const rows: UiElement[] = [];
  for (const [key, value] of Object.entries(capabilities || {})) {
    rows.push(
      el("div.tt-row", {}, el("span.k", { style: { minWidth: "150px" } }, key), el("span.v", {}, inlineValue(value))),
    );
  }
  return rows;
}

function showHealthInspector(focusBackend?: string): void {
  const health = state.health;
  if (!health) return;
  const body = el("div.panel-body", {
    style: { padding: "8px 12px", display: "flex", flexDirection: "column", gap: "10px" },
  });
  body.append(
    el("div.tt-row", {}, el("span.k", {}, t("workspace")), el("span.v", {}, health.ws_id || "?")),
    el("div.tt-row", {}, el("span.k", {}, t("entities")), el("span.v", {}, String(health.entities.length))),
  );
  for (const backend of health.backends) {
    body.append(
      el(
        "div.backend-card",
        { style: { margin: "0" } },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "6px" } },
          icon("server"),
          el("span", { style: { fontWeight: 650 } }, backend.name),
          el("span.badge.plain", {}, backend.type || backend.provider || "?"),
          focusBackend === backend.name ? el("span.badge.ok", {}, t("selected")) : null,
        ),
        el("div.backend-caps", {}, ...capabilityRows(backend.capabilities ?? {})),
      ),
    );
  }
  openInspector(body, t("Workspace health"));
}

// ------------------------------------------------------------------ shell

function render(): void {
  renderRail();
  closeInspector();
  const browser = requiredElement("browser");
  const workspace = requiredElement("workspace");
  browser.replaceChildren();
  workspace.replaceChildren();
  const view = VIEWS[state.route] ?? VIEWS.data;
  if (!view) throw new Error("dashboard data view is unavailable");
  view.renderBrowser(browser, state);
  view.renderMain(workspace, state);
  renderStatus();
}

// -------------------------------------------------------- browser resize

function setupGutter(): void {
  const gutter = requiredElement("gutter");
  const browser = (): UiElement => requiredElement("browser");
  // Below 760px the shell stacks, so the same handle sits under the browser band
  // and sizes its height instead of the column width.
  const stacked = (): boolean => window.matchMedia("(max-width: 760px)").matches;
  const bounds = () => browserPanelBounds(stacked(), window.innerHeight);
  const setSize = (size: number): void => {
    const { min, max, axis, orientation } = bounds();
    const bounded = Math.min(max, Math.max(min, size));
    document.documentElement.style.setProperty(axis, `${bounded}px`);
    gutter.setAttribute("aria-orientation", orientation);
    gutter.setAttribute("aria-valuemin", String(min));
    gutter.setAttribute("aria-valuemax", String(max));
    gutter.setAttribute("aria-valuenow", String(Math.round(bounded)));
    window.dispatchEvent(new Event("hb-browser-resize"));
  };
  const current = (): number => (stacked() ? browser().offsetHeight : browser().offsetWidth);
  gutter.tabIndex = 0;
  setSize(current() || (stacked() ? 230 : 260));
  window.addEventListener("resize", () => setSize(current()));
  gutter.addEventListener("keydown", (event) => {
    const grow = stacked() ? "ArrowDown" : "ArrowRight";
    const shrink = stacked() ? "ArrowUp" : "ArrowLeft";
    if (event.key !== grow && event.key !== shrink) return;
    event.preventDefault();
    setSize(current() + (event.key === grow ? 10 : -10));
  });
  gutter.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    const vertical = stacked();
    const start = vertical ? event.clientY : event.clientX;
    const startSize = current();
    const move = (ev: PointerEvent): void => {
      setSize(startSize + (vertical ? ev.clientY : ev.clientX) - start);
    };
    const up = (): void => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
    // Listen on the window: pointer capture is refused for pointers the browser
    // does not consider active, and the handle is only 5px wide.
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  });
}

// --------------------------------------------------------------- shortcuts

function setupShortcuts(): void {
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      openPalette(state);
    }
  });
}

// ------------------------------------------------------------------- boot

async function boot(): Promise<void> {
  try {
    let preferences = await api.preferences();
    const legacy = legacyPreferences();
    if (!preferences.configured && Object.keys(legacy).length) preferences = await api.updatePreferences(legacy);
    applyPreferences(preferences);
  } catch (error) {
    toast(error instanceof Error ? error.message : String(error), "error");
    applyTheme(currentTheme());
    applyFontScale(currentFontScale());
    applyFont(currentFont());
  }
  setupGutter();
  setupShortcuts();
  mountAssistant(state);
  // The data view's renderBrowser owns the single auto-open path (it also
  // covers landing on #/config and navigating over later); a second open here
  // raced it and surfaced a spurious "switch already in progress" toast.
  render();
  // The boot overlay in index.html blocks interaction from first paint; the
  // data view's auto-open clears it once the workspace is fully loaded.
  // Other routes have no workspace to wait for, so clear it right away.
  if (state.route !== "data") setBusy(false);
}

document.addEventListener("DOMContentLoaded", () => {
  boot().catch((error: unknown) => {
    setBusy(false);
    toast(error instanceof Error ? error.message : String(error), "error");
  });
});
