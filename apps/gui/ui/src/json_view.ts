// Collapsible syntax-highlighted JSON tree for inspector values (TypeScript source).

import { el, type UiElement } from "./ui.js";

const INLINE_ITEMS = 10;
const INLINE_CHARS = 56;

function scalar(value: unknown): boolean {
  return value === null || typeof value !== "object";
}

function scalarNode(value: unknown): UiElement {
  if (value === null) return el("span.jv-null", {}, "null");
  if (typeof value === "boolean") return el("span.jv-bool", {}, String(value));
  if (typeof value === "number") return el("span.jv-num", {}, String(value));
  return el("span.jv-str", {}, JSON.stringify(value));
}

function inlineArray(value: unknown): UiElement | null {
  if (!Array.isArray(value) || value.length > INLINE_ITEMS || !value.every(scalar)) return null;
  const text = JSON.stringify(value);
  if (text.length > INLINE_CHARS) return null;
  const node = el("span", {}, el("span.jv-punc", {}, "["));
  value.forEach((item, index) => {
    if (index) node.append(el("span.jv-punc", {}, ", "));
    node.append(scalarNode(item));
  });
  node.append(el("span.jv-punc", {}, "]"));
  return node;
}

// One expandable container line: entries render lazily on first expand.
function containerNode(
  value: readonly unknown[] | Record<string, unknown>,
  depth: number,
  openDepth: number,
): UiElement {
  const isArray = Array.isArray(value);
  const entries: readonly (readonly [string | number, unknown])[] = isArray
    ? value.map((item, index) => [index, item] as const)
    : Object.entries(value);
  const [open, close] = isArray ? ["[", "]"] : ["{", "}"];
  if (!entries.length) return el("span.jv-punc", {}, open + close);
  const short = inlineArray(value);
  if (short) return short;

  let expanded = depth < openDepth; // levels below openDepth start collapsed
  let built = false;
  const children = el("div.jv-children");
  const summary = el(
    "span",
    {},
    el("span.jv-punc", {}, `${open}…${close}`),
    el("span.jv-count", {}, ` ${entries.length}`),
  );
  const chevron = el("span.jv-chevron", { "aria-hidden": "true" }, "▸");
  const tail = el("span.jv-punc", {}, close);
  const node = el("div.jv-node");
  const head = el(
    "span.jv-toggle",
    {
      role: "button",
      tabindex: "0",
      onclick: () => setExpanded(!expanded),
      onkeydown: (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          setExpanded(!expanded);
        }
      },
    },
    chevron,
    summary,
  );

  function setExpanded(next: boolean): void {
    expanded = next;
    if (expanded && !built) {
      built = true;
      for (const [key, item] of entries) {
        const row = el(
          "div.jv-row",
          {},
          isArray
            ? null
            : el("span", {}, el("span.jv-key", {}, JSON.stringify(String(key))), el("span.jv-punc", {}, ": ")),
        );
        row.append(
          scalar(item)
            ? scalarNode(item)
            : containerNode(item as readonly unknown[] | Record<string, unknown>, depth + 1, openDepth),
        );
        children.append(row);
      }
    }
    chevron.textContent = expanded ? "▾" : "▸";
    const parts = [el("span.jv-punc", {}, expanded ? open : `${open}…${close}`)];
    if (!expanded) parts.push(el("span.jv-count", {}, ` ${entries.length}`));
    summary.replaceChildren(...parts);
    children.style.display = expanded ? "" : "none";
    tail.style.display = expanded ? "" : "none";
  }

  node.append(head, children, tail);
  setExpanded(expanded);
  return node;
}

/**
 * Render one JSON value as a themed, collapsible tree.
 *
 * Levels below ``openDepth`` start collapsed (default: root expanded, nested
 * containers collapsed). Short scalar arrays render inline.
 */
export function jsonView(value: unknown, openDepth = 1): UiElement {
  return el(
    "div.json-view",
    {},
    scalar(value)
      ? scalarNode(value)
      : containerNode(value as readonly unknown[] | Record<string, unknown>, 0, openDepth),
  );
}
