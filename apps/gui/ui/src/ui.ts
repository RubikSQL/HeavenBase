// Small typed DOM helpers shared by every dashboard view.

import { icon } from "./icons.js";
import { t } from "./i18n.js";

export interface UiElement extends HTMLElement {
  checked: boolean;
  disabled: boolean;
  href: string;
  max: string;
  min: string;
  name: string;
  placeholder: string;
  readOnly: boolean;
  rel: string;
  rows: number;
  step: string;
  type: string;
  value: string;
  select(): void;
  setSelectionRange(start: number, end: number): void;
  querySelector<E extends Element = UiElement>(selectors: string): E | null;
  querySelectorAll<E extends Element = UiElement>(selectors: string): NodeListOf<E>;
  closest<E extends Element = UiElement>(selectors: string): E | null;
}

export type UiChild = Node | string | number | boolean | null | undefined | readonly UiChild[];

export interface UiProps {
  [key: string]: unknown;
  onclick?: (event: MouseEvent) => unknown;
  onchange?: (event: Event) => unknown;
  oninput?: (event: InputEvent) => unknown;
  onkeydown?: (event: KeyboardEvent) => unknown;
  onkeyup?: (event: KeyboardEvent) => unknown;
  onpointerdown?: (event: PointerEvent) => unknown;
  onpointermove?: (event: PointerEvent) => unknown;
  onpointerup?: (event: PointerEvent) => unknown;
  ondblclick?: (event: MouseEvent) => unknown;
  ondragstart?: (event: DragEvent) => unknown;
  ondragover?: (event: DragEvent) => unknown;
  ondrop?: (event: DragEvent) => unknown;
  oncontextmenu?: (event: MouseEvent) => unknown;
  onmouseenter?: (event: MouseEvent) => unknown;
  onmouseleave?: (event: MouseEvent) => unknown;
  onfocus?: (event: FocusEvent) => unknown;
  onblur?: (event: FocusEvent) => unknown;
}

function appendChild(node: UiElement, child: UiChild): void {
  if (Array.isArray(child)) {
    for (const nested of child) appendChild(node, nested);
  } else if (child !== null && child !== undefined && child !== false) {
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

// Hyperscript: el("button.icon-btn", {onclick, title}, child, ...).
export function el(tag: string, props: UiProps = {}, ...children: UiChild[]): UiElement {
  const [name, ...classes] = tag.split(".");
  const node = document.createElement(name || "div") as UiElement;
  if (classes.length) node.className = classes.join(" ");
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined) continue;
    if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value as EventListener);
    } else if (key === "dataset" && typeof value === "object") {
      Object.assign(node.dataset, value);
    } else if (key === "style" && typeof value === "object") {
      Object.assign(node.style, value);
    } else if (key in node && key !== "list" && key !== "form") {
      Reflect.set(node, key, value);
    } else {
      node.setAttribute(key, String(value));
    }
  }
  for (const child of children) appendChild(node, child);
  return node;
}

export function iconBtn(name: string, title: string, onclick: (event: MouseEvent) => unknown, cls = ""): UiElement {
  return el(`button.icon-btn${cls ? `.${cls}` : ""}`, { title, "aria-label": title, onclick }, icon(name));
}

export function debounce<Args extends unknown[]>(fn: (...args: Args) => void, ms = 250): (...args: Args) => void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  return (...args: Args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// ------------------------------------------------------------------ toast

const toastZone = el("div.toast-zone");
document.addEventListener("DOMContentLoaded", () => document.body.append(toastZone));

export function toast(message: string, kind: "success" | "error" = "success"): void {
  const node = el(`div.toast.${kind}`, {}, icon(kind === "error" ? "alert" : "check"), el("span", {}, message));
  toastZone.append(node);
  setTimeout(
    () => {
      node.style.transition = "opacity 0.3s";
      node.style.opacity = "0";
      setTimeout(() => node.remove(), 320);
    },
    kind === "error" ? 4200 : 1800,
  );
}

export async function copyText(text: string, label?: string): Promise<void> {
  const resolvedLabel = label ?? t("Copied");
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = el("textarea", {
      value: text,
      style: { position: "fixed", opacity: "0" },
    });
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast(resolvedLabel);
}

// ------------------------------------------------------------------- busy

export function setBusy(on: boolean): void {
  const overlay = document.getElementById("boot");
  const shell = document.getElementById("app");
  overlay?.classList.toggle("off", !on);
  if (shell) {
    shell.inert = on;
    shell.setAttribute("aria-busy", String(on));
  }
}

// ------------------------------------------------------------------- menu

interface MenuSeparator {
  sep: true;
}

interface MenuHeader {
  header: true;
  label: string;
}

export interface MenuTrailingAction {
  label: string;
  icon: string;
  danger?: boolean;
  onclick: () => unknown;
}

export interface MenuAction {
  label: string;
  icon?: string;
  hint?: string;
  selected?: boolean;
  danger?: boolean;
  keepOpen?: boolean;
  onclick?: () => unknown;
  trailingAction?: MenuTrailingAction;
}

export type MenuItem = MenuSeparator | MenuHeader | MenuAction;
type MenuSource = readonly MenuItem[] | (() => readonly MenuItem[]);

interface MenuOptions {
  matchAnchorWidth?: boolean;
  searchable?: boolean;
  searchLabel?: string;
}

let openMenu: UiElement | null = null;
let openMenuAnchor: UiElement | null = null;

export function closeMenu(restoreFocus = false): void {
  if (!openMenu) return;
  openMenu.remove();
  openMenu = null;
  openMenuAnchor?.setAttribute("aria-expanded", "false");
  if (restoreFocus) openMenuAnchor?.focus();
  openMenuAnchor = null;
  document.removeEventListener("pointerdown", onMenuPointer, true);
  document.removeEventListener("keydown", onMenuKey, true);
}

function onMenuPointer(event: PointerEvent): void {
  if (openMenu && event.target instanceof Node && !openMenu.contains(event.target)) closeMenu();
}

function onMenuKey(event: KeyboardEvent): void {
  if (!openMenu) return;
  const items = [...openMenu.querySelectorAll(".menu-item:not([disabled]), .menu-action:not([disabled])")];
  const index = items.indexOf(document.activeElement as UiElement);
  let next: UiElement | undefined;
  if (event.key === "ArrowDown") next = items[(index + 1 + items.length) % items.length];
  else if (event.key === "ArrowUp") next = items[(index - 1 + items.length) % items.length];
  else if (event.key === "Home") next = items[0];
  else if (event.key === "End") next = items.at(-1);
  else if (event.key === "Escape") {
    event.preventDefault();
    closeMenu(true);
    return;
  } else if (event.key === "Tab") {
    closeMenu();
    return;
  }
  if (next) {
    event.preventDefault();
    next.focus();
  }
}

export function showMenu(anchor: UiElement, items: MenuSource, options: MenuOptions = {}): UiElement {
  closeMenu();
  const buildItems = typeof items === "function" ? items : () => items;
  const menu = el("div.menu", { role: "menu" });
  const results = el("div.menu-results");
  let query = "";
  const renderItems = (): void => {
    results.replaceChildren();
    let actions = 0;
    for (const item of buildItems()) {
      if ("sep" in item) {
        if (!query) results.append(el("div.menu-sep"));
        continue;
      }
      if ("header" in item) {
        if (!query) results.append(el("div.menu-label", {}, item.label));
        continue;
      }
      const haystack = `${item.label} ${item.hint ?? ""}`.toLowerCase();
      if (query && !haystack.includes(query)) continue;
      actions += 1;
      const main = el(
        `button.menu-item${item.selected ? ".selected" : ""}`,
        {
          role: "menuitem",
          tabindex: -1,
          onclick: () => {
            if (item.keepOpen) {
              item.onclick?.();
              renderItems();
              return;
            }
            closeMenu(true);
            item.onclick?.();
          },
        },
        item.icon ? icon(item.icon) : null,
        el("span", { style: { flex: "1", overflow: "hidden", textOverflow: "ellipsis" } }, item.label),
        item.hint ? el("span.hint", {}, item.hint) : null,
      );
      if (item.danger) main.style.color = "var(--danger)";
      if (!item.trailingAction) {
        results.append(main);
        continue;
      }
      const trailing = item.trailingAction;
      results.append(
        el(
          "div.menu-row",
          {},
          main,
          el(
            `button.menu-action${trailing.danger ? ".danger" : ""}`,
            {
              role: "menuitem",
              tabindex: -1,
              title: trailing.label,
              "aria-label": trailing.label,
              onclick: () => {
                closeMenu(true);
                trailing.onclick();
              },
            },
            icon(trailing.icon),
          ),
        ),
      );
    }
    if (!actions) results.append(el("div.menu-empty", {}, t("No matching values")));
  };
  if (options.searchable) {
    const input = el("input.input", {
      type: "search",
      placeholder: options.searchLabel ?? t("Search"),
      "aria-label": options.searchLabel ?? t("Search"),
      oninput: (event) => {
        if (!(event.target instanceof HTMLInputElement)) return;
        query = event.target.value.trim().toLowerCase();
        renderItems();
      },
    });
    menu.append(el("div.search-box.menu-search", {}, icon("search"), input));
  }
  menu.append(results);
  renderItems();
  document.body.append(menu);
  const rect = anchor.getBoundingClientRect();
  if (options.matchAnchorWidth) {
    const available = Math.max(0, window.innerWidth - 16);
    const width = Math.min(rect.width, available);
    menu.style.width = `${width}px`;
    menu.style.minWidth = `${width}px`;
    menu.style.maxWidth = `${width}px`;
  }
  const mw = menu.offsetWidth;
  const mh = menu.offsetHeight;
  menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - mw - 8))}px`;
  menu.style.top = rect.bottom + mh + 8 > window.innerHeight ? `${rect.top - mh - 4}px` : `${rect.bottom + 4}px`;
  openMenu = menu;
  openMenuAnchor = anchor;
  anchor.setAttribute("aria-haspopup", "menu");
  anchor.setAttribute("aria-expanded", "true");
  document.addEventListener("pointerdown", onMenuPointer, true);
  document.addEventListener("keydown", onMenuKey, true);
  queueMicrotask(() => {
    const search = menu.querySelector(".menu-search .input");
    (search ?? menu.querySelector(".menu-item.selected") ?? menu.querySelector(".menu-item"))?.focus();
  });
  return menu;
}

export interface SelectOption<T extends string | number> {
  value: T;
  label?: string;
  hint?: string;
  icon?: string;
  trailingAction?: MenuTrailingAction;
}

export interface SelectElement<T extends string | number> extends UiElement {
  getValue(): T;
  setValue(next: T): void;
}

interface SelectConfig<T extends string | number> {
  value: T;
  options: readonly SelectOption<T>[];
  onchange?: (value: T) => unknown;
  compact?: boolean;
  width?: string;
  label?: string;
  searchable?: boolean;
  placeholder?: {
    label: string;
    icon?: string;
  };
}

export function select<T extends string | number>({
  value,
  options,
  onchange,
  compact = false,
  width,
  label: ariaLabel,
  searchable = false,
  placeholder,
}: SelectConfig<T>): SelectElement<T> {
  let current = value;
  const placeholderIcon = placeholder?.icon ? icon(placeholder.icon) : null;
  const label = el("span.value");
  const node = el(
    `button.select${compact ? ".compact" : ""}`,
    {
      type: "button",
      "aria-haspopup": "menu",
      "aria-expanded": "false",
      style: width ? { width } : null,
    },
    placeholderIcon,
    label,
    icon("chevronDown"),
  ) as SelectElement<T>;
  const syncValue = (): void => {
    const hasValue = options.some((option) => option.value === current);
    label.textContent = displayOf(options, current, placeholder?.label);
    if (placeholderIcon instanceof HTMLElement || placeholderIcon instanceof SVGElement) {
      placeholderIcon.style.display = hasValue ? "none" : "";
    }
    node.classList.toggle("placeholder", !hasValue && placeholder !== undefined);
    node.setAttribute("aria-label", ariaLabel || label.textContent || t("Select"));
  };
  node.onclick = () => {
    showMenu(
      node,
      options.map(
        (option): MenuAction => ({
          label: option.label ?? String(option.value),
          ...(option.hint === undefined ? {} : { hint: option.hint }),
          ...(option.icon === undefined ? {} : { icon: option.icon }),
          ...(option.trailingAction === undefined ? {} : { trailingAction: option.trailingAction }),
          selected: option.value === current,
          onclick: () => {
            const previous = current;
            node.setValue(option.value);
            const result = onchange?.(current);
            if (result instanceof Promise) result.catch(() => node.setValue(previous));
          },
        }),
      ),
      { matchAnchorWidth: true, searchable, searchLabel: t("Search") },
    );
  };
  node.getValue = () => current;
  node.setValue = (next: T) => {
    current = next;
    syncValue();
  };
  syncValue();
  return node;
}

function displayOf<T extends string | number>(
  options: readonly SelectOption<T>[],
  value: T,
  fallback?: string,
): string {
  const option = options.find((candidate) => candidate.value === value);
  return option?.label ?? (option ? String(option.value) : (fallback ?? String(value ?? "")));
}

// ---------------------------------------------------------------- tooltip

let tipNode: UiElement | null = null;
let tipTarget: UiElement | null = null;
let tooltipId = 0;

export function attachTooltip(target: UiElement, build: () => UiChild): void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  if (!/^(BUTTON|A|INPUT|SELECT|TEXTAREA)$/.test(target.tagName) && !target.hasAttribute("tabindex")) {
    target.tabIndex = 0;
  }
  const show = (delay = 350): void => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (!target.isConnected) return;
      const content = build();
      if (content === null || content === undefined || content === false) return;
      tipNode?.remove();
      tipTarget?.removeAttribute("aria-describedby");
      const id = `hb-tooltip-${++tooltipId}`;
      const nextTip = el("div.tooltip", { id, role: "tooltip" }, content);
      tipNode = nextTip;
      tipTarget = target;
      target.setAttribute("aria-describedby", id);
      document.body.append(nextTip);
      const rect = target.getBoundingClientRect();
      const tw = nextTip.offsetWidth;
      const th = nextTip.offsetHeight;
      nextTip.style.left = `${Math.max(6, Math.min(rect.left, window.innerWidth - tw - 8))}px`;
      nextTip.style.top =
        rect.bottom + th + 10 > window.innerHeight ? `${rect.top - th - 6}px` : `${rect.bottom + 6}px`;
    }, delay);
  };
  target.addEventListener("pointerenter", () => show());
  const hide = (): void => {
    clearTimeout(timer);
    if (tipTarget !== target) return;
    tipNode?.remove();
    tipNode = null;
    tipTarget = null;
    target.removeAttribute("aria-describedby");
  };
  target.addEventListener("pointerleave", hide);
  target.addEventListener("pointerdown", hide);
  target.addEventListener("focusin", () => show(0));
  target.addEventListener("focusout", hide);
}

// ------------------------------------------------------------------ modal

export interface ModalAction {
  label: string;
  primary?: boolean;
  danger?: boolean;
  onclick?: () => unknown | Promise<unknown>;
}

interface ModalConfig {
  title: string;
  body: UiChild;
  actions?: readonly ModalAction[];
  onClose?: () => void;
}

export function modal({ title, body, actions = [], onClose }: ModalConfig): { close: () => void; dialog: UiElement } {
  const previousFocus = document.activeElement;
  const titleId = `hb-modal-title-${++tooltipId}`;
  const back = el("div.modal-back");
  const close = (): void => {
    back.remove();
    document.removeEventListener("keydown", onKey, true);
    onClose?.();
    if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
  };
  back.onpointerdown = (event) => {
    if (event.target === back) close();
  };
  const onKey = (event: KeyboardEvent): void => {
    if (event.key === "Escape") close();
    if (event.key !== "Tab") return;
    const focusable = [
      ...dialog.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    ];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  };
  const dialog = el(
    "div.modal",
    { role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
    el("div.modal-head", {}, el("span.title", { id: titleId }, title), iconBtn("x", t("Close"), close)),
    el("div.modal-body", {}, body),
    actions.length
      ? el(
          "div.modal-foot",
          {},
          actions.map((action) =>
            el(
              `button.btn${action.primary ? ".primary" : ""}${action.danger ? ".danger" : ""}`,
              {
                onclick: async () => {
                  if ((await action.onclick?.()) !== false) close();
                },
              },
              action.label,
            ),
          ),
        )
      : null,
  );
  back.append(dialog);
  document.body.append(back);
  document.addEventListener("keydown", onKey, true);
  queueMicrotask(() => dialog.querySelector("input, select, textarea, button")?.focus());
  return { close, dialog };
}

// -------------------------------------------------------------- formatting

export function fmtCount(n: number | null | undefined): string {
  if (n == null) return "";
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e4) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

export function previewValue(value: unknown, max = 80): string {
  if (value == null) return "null";
  if (typeof value === "string") return value.length > max ? `${value.slice(0, max)}…` : value;
  if (Array.isArray(value)) {
    const inner = value
      .slice(0, 6)
      .map((item) => previewValue(item, 14))
      .join(", ");
    return `[${inner}${value.length > 6 ? ", …" : ""}] (${value.length})`;
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const keys = Object.keys(record);
    const inner = keys
      .slice(0, 5)
      .map((key) => `${key}: ${previewValue(record[key], 12)}`)
      .join(", ");
    return `{${inner}${keys.length > 5 ? ", …" : ""}}`;
  }
  return String(value);
}

export function fullValue(value: unknown): string {
  if (value == null) return "null";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

export function inlineValue(value: unknown): string {
  if (value == null) return String(value);
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export const SECRET_KEY = /(api_key|apikey|token|password|secret|credential)/i;
