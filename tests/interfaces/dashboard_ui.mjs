import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  api,
  AssistResponseError,
  isAbortError,
  ProtocolResponseError,
  shouldRetryAssist,
} from "../../apps/gui/ui/js/api.js";
import {
  browserPanelBounds,
  parseTypedScalar,
  renameConfigKey,
  statsSeparatorValues,
} from "../../apps/gui/ui/js/gui-logic.js";

class FakeNode {}
globalThis.Node = FakeNode;

class FakeText extends FakeNode {
  constructor(text) {
    super();
    this.nodeType = 3;
    this.parentElement = null;
    this.textContent = String(text);
  }
}

class FakeElement extends FakeNode {
  constructor(tagName) {
    super();
    this.nodeType = 1;
    this.tagName = tagName.toUpperCase();
    this.parentElement = null;
    this.childNodes = [];
    this.className = "";
    this.dataset = {};
    this.style = {};
    this.attributes = new Map();
    this.listeners = new Map();
    if (this.tagName === "TEMPLATE") this.content = { firstChild: null };
  }

  append(...children) {
    for (const child of children) {
      const node = child?.nodeType ? child : new FakeText(child);
      node.parentElement = this;
      this.childNodes.push(node);
    }
  }

  replaceChildren(...children) {
    this.childNodes = [];
    this.append(...children);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  querySelectorAll(selector) {
    const excluded = selector.match(/:not\(\.([a-z0-9-]+)\)/i)?.[1];
    const required = [...selector.replace(/:not\([^)]*\)/g, "").matchAll(/\.([a-z0-9-]+)/gi)].map((match) => match[1]);
    return elements(this).filter((node) => {
      const classes = node.className.split(/\s+/);
      return required.every((name) => classes.includes(name)) && (!excluded || !classes.includes(excluded));
    });
  }

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  removeEventListener(name, listener) {
    this.listeners.set(
      name,
      (this.listeners.get(name) || []).filter((candidate) => candidate !== listener),
    );
  }

  click() {
    for (const listener of this.listeners.get("click") || []) listener({ target: this, currentTarget: this });
  }

  focus() {
    globalThis.document.activeElement = this;
  }

  contains(candidate) {
    return candidate === this || this.childNodes.some((node) => node.nodeType === 1 && node.contains(candidate));
  }

  remove() {
    if (!this.parentElement) return;
    this.parentElement.childNodes = this.parentElement.childNodes.filter((node) => node !== this);
    this.parentElement = null;
  }

  get children() {
    return this.childNodes.filter((node) => node.nodeType === 1);
  }

  get childElementCount() {
    return this.children.length;
  }

  get offsetHeight() {
    const height = Number.parseInt(this.style.height || "", 10);
    if (Number.isFinite(height)) return height;
    if (this.className.split(/\s+/).includes("stats-splitter")) return 9;
    return 400;
  }

  get offsetWidth() {
    return 260;
  }

  getBoundingClientRect() {
    return { top: 0, right: this.offsetWidth, bottom: this.offsetHeight, left: 0, width: this.offsetWidth, height: this.offsetHeight };
  }

  get textContent() {
    return this.childNodes.map((node) => node.textContent).join("");
  }

  set textContent(value) {
    this.childNodes = [];
    if (value !== "") this.append(new FakeText(value));
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
    if (this.tagName === "TEMPLATE") this.content.firstChild = new FakeElement("svg");
  }

  get innerHTML() {
    return this._innerHTML || "";
  }
}

globalThis.HTMLElement = FakeElement;
globalThis.HTMLInputElement = FakeElement;
globalThis.Element = FakeElement;

const documentElement = new FakeElement("html");
const documentBody = new FakeElement("body");
globalThis.document = {
  activeElement: documentBody,
  body: documentBody,
  documentElement,
  createElement: (name) => new FakeElement(name),
  createTextNode: (text) => new FakeText(text),
  getElementById: () => null,
  addEventListener() {},
  removeEventListener() {},
};
const storedValues = new Map();
globalThis.localStorage = {
  getItem: (key) => storedValues.get(key) ?? null,
  setItem: (key, value) => storedValues.set(key, String(value)),
};
let narrowLayout = false;
globalThis.window = {
  innerWidth: 1024,
  innerHeight: 768,
  addEventListener() {},
  removeEventListener() {},
  matchMedia: () => ({ matches: narrowLayout }),
};
globalThis.getComputedStyle = (node) => node.style;

const { markdown } = await import("../../apps/gui/ui/js/markdown.js");
const { closeMenu, showMenu } = await import("../../apps/gui/ui/js/ui.js");
const {
  assistantHistoryKey,
  clampPanelGeometry,
  frameBody,
  pendingTurn,
  setAssistantScope,
  sqlFrameBody,
  tableTsv,
  toolResultBody,
} = await import("../../apps/gui/ui/js/assistant.js");
const { buildStatsBody } = await import("../../apps/gui/ui/js/col_stats.js");
storedValues.set("hb-stats-open", "1");
const { renderBrowser } = await import("../../apps/gui/ui/js/view_data.js");
storedValues.delete("hb-stats-open");

function elements(root) {
  const found = [];
  const visit = (node) => {
    if (node.nodeType !== 1) return;
    found.push(node);
    node.childNodes.forEach(visit);
  };
  visit(root);
  return found;
}

function withClass(root, name) {
  return elements(root).filter((node) => node.className.split(/\s+/).includes(name));
}

test("assistant application errors are surfaced without a blocking retry", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });
  let cancelled = false;
  globalThis.fetch = async () =>
    new Response(
      new ReadableStream({
        start(stream) {
          stream.enqueue(new TextEncoder().encode('data: {"kind":"error","text":"provider failed"}\n\n'));
        },
        cancel() {
          cancelled = true;
        },
      }),
    );

  await assert.rejects(
    api.assistStream("hello", {}, [], () => {}),
    (error) => error instanceof AssistResponseError && error.message === "provider failed" && !shouldRetryAssist(error),
  );
  assert.equal(cancelled, true);
  assert.equal(shouldRetryAssist(new TypeError("network failed")), true);
});

test("aborting the assistant stream rejects with a non-retried AbortError", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });
  const controller = new AbortController();
  globalThis.fetch = async (url, { signal }) => {
    const body = new ReadableStream({
      start(stream) {
        stream.enqueue(new TextEncoder().encode('data: {"kind":"text","text":"partial"}\n\n'));
        signal.addEventListener("abort", () => stream.error(Object.assign(new Error("aborted"), { name: "AbortError" })));
      },
    });
    return new Response(body);
  };

  const seen = [];
  const pending = api.assistStream(
    "hello",
    {},
    [],
    (event) => {
      seen.push(event);
      controller.abort(); // interrupt as soon as the first chunk arrives
    },
    { signal: controller.signal },
  );
  await assert.rejects(pending, (error) => isAbortError(error) && !shouldRetryAssist(error));
  assert.deepEqual(seen, [{ kind: "text", text: "partial" }]);
  assert.equal(isAbortError(new TypeError("network failed")), false);
});

test("aborting the blocking assistant fallback cancels its request", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });
  const controller = new AbortController();
  globalThis.fetch = async (_url, { signal }) =>
    new Promise((_resolve, reject) => {
      signal.addEventListener(
        "abort",
        () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })),
        { once: true },
      );
    });

  const pending = api.assist("hello", {}, [], { signal: controller.signal });
  controller.abort();

  await assert.rejects(pending, (error) => isAbortError(error) && !shouldRetryAssist(error));
});

test("assistant requests carry the explicit preset and read-only mode", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });
  let body;
  globalThis.fetch = async (_url, options) => {
    body = JSON.parse(options.body);
    return new Response(
      JSON.stringify({
        text: "ok",
        think: "",
        served_model: "test-model",
        frames: [],
        sql_frames: [],
        events: [],
        elapsed: 0,
        truncated: false,
      }),
      { headers: { "Content-Type": "application/json" } },
    );
  };

  await api.assist("hello", {}, [], { preset: "reason-pro", readOnly: true });
  assert.equal(body.preset, "reason-pro");
  assert.equal(body.read_only, true);
});

test("interrupted streams finalize a faithful partial turn", () => {
  // Aborting before anything streamed still records the interrupted marker so
  // no user question is left dangling (and roles keep alternating next call).
  assert.deepEqual(pendingTurn().turn(), { role: "assistant", content: "", events: [], interrupted: true });

  const partial = pendingTurn();
  partial.feed({ kind: "think", text: "step " });
  partial.feed({ kind: "think", text: "one" });
  partial.feed({ kind: "text", text: "Checking the schema." }); // pre-tool narrative, not the answer
  const frame = { entity: "products", spec: {}, columns: [], rows: [], n: 0 };
  partial.feed({ kind: "tool", name: "query", args: "{}", frame });
  partial.feed({ kind: "tool_result", text: "ignored: the frame is the query result" });
  partial.feed({ kind: "tool", name: "entity_schema", args: '{"entity_id":"products"}' });
  partial.feed({ kind: "tool_result", text: '{"fields":{}}' });
  const sqlFrame = {
    database_id: "db-warehouse",
    sql: "SELECT COUNT(*) AS n FROM orders",
    params: {},
    columns: ["n"],
    rows: [{ n: 12 }],
    n: 1,
    total: 1,
    limit: 200,
    truncated: false,
    readonly: true,
  };
  partial.feed({ kind: "tool", name: "run_sql", args: "{}", sql_frame: sqlFrame });
  partial.feed({ kind: "tool_result", text: "ignored: the SQL frame is the result" });
  partial.feed({ kind: "text", text: "Hel" });
  partial.feed({ kind: "text", text: "lo" });

  assert.deepEqual(partial.turn(), {
    role: "assistant",
    content: "Hello",
    interrupted: true,
    events: [
      { type: "think", text: "step one" },
      { type: "think", text: "Checking the schema." },
      { type: "tool", name: "query", args: "{}", frame },
      { type: "tool", name: "entity_schema", args: '{"entity_id":"products"}', result: '{"fields":{}}' },
      { type: "tool", name: "run_sql", args: "{}", sql_frame: sqlFrame },
    ],
  });
});

test("assistant protocol errors are surfaced without duplicating a model turn", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async () => new Response("data: not-json\n\n");

  await assert.rejects(
    api.assistStream("hello", {}, [], () => {}),
    (error) => error instanceof ProtocolResponseError && !shouldRetryAssist(error),
  );
});

test("matched menus stay anchored and restore focus before actions", async () => {
  const anchor = new FakeElement("button");
  documentBody.append(anchor);
  anchor.focus();
  let actionFocus = null;
  const menu = showMenu(
    anchor,
    [
      {
        label: "A workspace label wider than its selector",
        trailingAction: {
          label: "Remove",
          icon: "trash",
          onclick: () => {
            actionFocus = document.activeElement;
          },
        },
      },
    ],
    { matchAnchorWidth: true },
  );
  await Promise.resolve();

  assert.equal(menu.style.width, "260px");
  assert.equal(menu.style.minWidth, "260px");
  assert.equal(menu.style.maxWidth, "260px");
  assert.notEqual(document.activeElement, anchor);
  withClass(menu, "menu-action")[0].click();
  assert.equal(actionFocus, anchor);
  assert.equal(document.activeElement, anchor);
  assert.equal(anchor.getAttribute("aria-expanded"), "false");

  closeMenu();
  anchor.remove();
});

test("assistant history is isolated by workspace", () => {
  storedValues.set(assistantHistoryKey("alpha"), JSON.stringify([{ role: "assistant", content: "alpha-only" }]));
  storedValues.set(assistantHistoryKey("beta"), JSON.stringify([{ role: "assistant", content: "beta-only" }]));

  assert.deepEqual(setAssistantScope("alpha"), [{ role: "assistant", content: "alpha-only" }]);
  assert.deepEqual(setAssistantScope("beta"), [{ role: "assistant", content: "beta-only" }]);
  assert.equal(storedValues.get(assistantHistoryKey("alpha")), JSON.stringify([{ role: "assistant", content: "alpha-only" }]));
  assert.notEqual(assistantHistoryKey("alpha"), assistantHistoryKey("beta"));
});

test("config key rename preserves values and rejects collisions", () => {
  assert.deepEqual(renameConfigKey({ first: 1, second: 2 }, "first", "renamed"), { renamed: 1, second: 2 });
  assert.throws(() => renameConfigKey({ first: 1, second: 2 }, "first", "second"), /already exists/);
});

test("typed filters never partially parse invalid numbers", () => {
  assert.equal(parseTypedScalar("12", "integer"), 12);
  assert.equal(parseTypedScalar("12x", "integer"), "12x");
  assert.equal(parseTypedScalar("12", "short-text"), "12");
  assert.equal(parseTypedScalar("false", "boolean"), false);
});

test("responsive browser and stats splitter ranges stay internally consistent", () => {
  assert.deepEqual(browserPanelBounds(false, 796), { min: 180, max: 420, axis: "--browser-w", orientation: "vertical" });
  assert.deepEqual(browserPanelBounds(true, 796), { min: 120, max: 637, axis: "--browser-h", orientation: "horizontal" });
  assert.deepEqual(browserPanelBounds(true, 100), { min: 120, max: 120, axis: "--browser-h", orientation: "horizontal" });

  assert.deepEqual(
    statsSeparatorValues({ stacked: true, viewportHeight: 796, columnHeight: 302, treeHeight: 117, panelHeight: 40 }),
    {
      tree: { min: 24, max: 452, now: 117 },
      panel: { min: 40, max: 375, now: 40 },
    },
  );
  assert.deepEqual(
    statsSeparatorValues({ stacked: false, viewportHeight: 796, columnHeight: 770, treeHeight: 500, panelHeight: 220 }),
    {
      tree: { min: 24, max: 680, now: 500 },
      panel: { min: 40, max: 220, now: 220 },
    },
  );
});

test("stats splitter DOM survives responsive breakpoint changes", () => {
  const originalWorkspaces = api.workspaces;
  const browser = new FakeElement("aside");
  const styles = readFileSync(new URL("../../apps/gui/ui/styles.css", import.meta.url), "utf8");
  const narrowStyles = styles.slice(styles.indexOf("@media (max-width: 760px)"));

  api.workspaces = async () => [];
  try {
    renderBrowser(browser, { route: "data", wsId: null });
    const [foot] = withClass(browser, "stats-splitter-foot");
    narrowLayout = true;

    assert.ok(foot);
    assert.equal(withClass(browser, "stats-splitter-foot")[0], foot);
    assert.match(styles, /\.stats-splitter-foot\s*\{\s*display:\s*none;\s*\}/);
    assert.match(narrowStyles, /\.stats-splitter-foot\s*\{\s*display:\s*block;\s*\}/);
  } finally {
    narrowLayout = false;
    api.workspaces = originalWorkspaces;
  }
});

test("literal UI translation keys are covered exactly once", () => {
  const sourceDirectory = fileURLToPath(new URL("../../apps/gui/ui/src/", import.meta.url));
  const i18nSource = readFileSync(new URL("../../apps/gui/ui/src/i18n.ts", import.meta.url), "utf8");
  const keys = [...i18nSource.matchAll(/^  (?:"((?:[^"\\]|\\.)*)"|([A-Za-z_$][A-Za-z0-9_$]*)):/gm)].map((match) =>
    match[1] === undefined ? match[2] : JSON.parse(`"${match[1]}"`),
  );
  const translations = new Set(keys);
  const duplicates = keys.filter((key, index) => keys.indexOf(key) !== index);
  const missing = [];
  for (const file of readdirSync(sourceDirectory).filter((name) => name.endsWith(".ts"))) {
    const source = readFileSync(new URL(`../../apps/gui/ui/src/${file}`, import.meta.url), "utf8");
    for (const match of source.matchAll(/\bt\("((?:[^"\\]|\\.)*)"/g)) {
      const key = JSON.parse(`"${match[1]}"`);
      if (!translations.has(key)) missing.push(`${file}: ${key}`);
    }
  }
  const configSource = readFileSync(new URL("../../apps/gui/ui/src/view_config.ts", import.meta.url), "utf8");
  const sourceBetween = (start, end) => configSource.slice(configSource.indexOf(start), configSource.indexOf(end, configSource.indexOf(start)));
  const catalogValues = [
    ...sourceBetween("const DOMAINS", "const local").matchAll(/\blabel:\s*"((?:[^"\\]|\\.)*)"/g),
    ...sourceBetween("const LABEL_OVERRIDES", "function prettyLabel").matchAll(/:\s*"((?:[^"\\]|\\.)*)"/g),
    ...sourceBetween("const FIELD_HELP", "// Unified field schema").matchAll(/:\s*"((?:[^"\\]|\\.)*)"/g),
  ].map((match) => JSON.parse(`"${match[1]}"`));
  for (const key of catalogValues) {
    if (!translations.has(key)) missing.push(`view_config.ts catalog: ${key}`);
  }

  assert.deepEqual([...new Set(duplicates)].sort(), []);
  assert.deepEqual(missing.sort(), []);
});

test("math parsing preserves escaped currency and exposes accessible formulas", () => {
  const root = markdown(String.raw`Cost \$100 and tax \$20; let $x^2$.`);
  const formulas = withClass(root, "math");

  assert.equal(root.textContent, "Cost $100 and tax $20; let x2.");
  assert.equal(formulas.length, 1);
  assert.equal(formulas[0].getAttribute("role"), "math");
  assert.equal(formulas[0].getAttribute("aria-label"), "x^2");
});

test("display math is structural and does not swallow surrounding blocks", () => {
  const root = markdown("Intro\n$$E=mc^2$$.\nAfter");
  const displays = withClass(root, "md-math");

  assert.equal(displays.length, 1);
  assert.equal(displays[0].textContent, "E=mc2");
  assert.equal(withClass(root, "md-p").length, 3);
  assert.match(root.textContent, /^IntroE=mc2\.After$/);

  const bracketed = markdown(String.raw`\[\frac{a}{b}\] trailing`);
  assert.equal(withClass(bracketed, "md-math").length, 1);
  assert.equal(withClass(bracketed, "math-frac").length, 1);
  assert.match(bracketed.textContent, /trailing$/);
});

test("malformed and over-budget math falls back to safe literal text", () => {
  const malformed = String.raw`Bad $\frac{a$ after`;
  const malformedRoot = markdown(malformed);
  assert.equal(malformedRoot.textContent, malformed);
  assert.equal(withClass(malformedRoot, "math").length, 0);

  const oversized = `$$${"x".repeat(2001)}$$`;
  const oversizedRoot = markdown(oversized);
  assert.equal(oversizedRoot.textContent, oversized);
  assert.equal(withClass(oversizedRoot, "md-math").length, 0);

  const unclosed = markdown("$$broken\n# Heading");
  assert.equal(unclosed.textContent, "$$brokenHeading");
  assert.equal(withClass(unclosed, "md-h1").length, 1);

  const unmatchedTex = String.raw`Keep \[broken and \(also broken`;
  assert.equal(markdown(unmatchedTex).textContent, unmatchedTex);

  const hostile = markdown(String.raw`$x<img src=x onerror=alert(1)>$`);
  assert.equal(elements(hostile).some((node) => node.tagName === "IMG"), false);
  assert.equal(hostile.textContent, "x<img src=x onerror=alert(1)>");
});

test("table copy is exact and quotes TSV control characters", () => {
  assert.equal(
    tableTsv([
      ["name", "note"],
      [1.23456789, "tab\tvalue"],
      ["line\nbreak", 'say "hi"'],
    ]),
    'name\tnote\n1.23456789\t"tab\tvalue"\n"line\nbreak"\t"say ""hi"""',
  );
});

test("query frames announce an engine-side cut the table clip cannot show", () => {
  const rows = Array.from({ length: 3 }, (_, index) => ({ year: 2020 + index, papers: index }));
  const complete = frameBody({ columns: ["year", "papers"], rows, n: 3, truncated: false });
  assert.equal(elements(complete).some((node) => node.className === "chat-frame-note"), false);

  // Every returned row is rendered, so only the engine's own flag can signal
  // that more groups matched — and it carries the exact count, never a floor.
  const cut = frameBody({ columns: ["year", "papers"], rows, n: 3, total: 17, truncated: true });
  const note = elements(cut).find((node) => node.className === "chat-frame-note");
  assert.match(note.textContent, /1–3 \/ 17/);
  assert.match(note.textContent, /truncated/);
  assert.equal(note.textContent.includes("+"), false);

  const offset = frameBody({ columns: ["year", "papers"], rows, n: 3, total: 17, truncated: true, spec: { offset: 10 } });
  assert.match(elements(offset).find((node) => node.className === "chat-frame-note").textContent, /11–13 \/ 17/);

  const clipped = frameBody({ columns: ["year"], rows: Array.from({ length: 60 }, (_, index) => ({ year: index })), n: 60 });
  assert.match(elements(clipped).find((node) => node.className === "chat-frame-note").textContent, /1–50 \/ 60$/);
});

test("SQL evidence renders the executed statement and actual result table", () => {
  const body = sqlFrameBody({
    database_id: "db-reporting",
    sql: "SELECT month, growth FROM report",
    params: {},
    columns: ["month", "growth"],
    rows: [{ month: "2024-08", growth: 4.45 }],
    n: 1,
    total: 1,
    limit: 200,
    truncated: false,
    readonly: true,
  });

  assert.match(body.textContent, /SELECT month, growth FROM report/);
  assert.equal(elements(body).filter((node) => node.tagName === "TABLE").length, 1);
  assert.match(body.textContent, /2024-08/);
  assert.match(body.textContent, /4.45/);
});

test("column statistics disclose display-bounded exact distributions", () => {
  const body = buildStatsBody({
    field: "meta",
    dtype: "json",
    kind: "json",
    total: 500,
    scanned: 500,
    sampled: false,
    nulls: 0,
    null_pct: 0,
    keys: [{ key: "shared", count: 500 }],
    keys_exact: true,
    keys_truncated: true,
  });
  assert.match(body.textContent, /top keys \(all rows, truncated\)/);
});

test("structured tool results preserve metadata and lossless cell titles", () => {
  const frame = toolResultBody(
    JSON.stringify({ columns: ["score"], rows: [[1.23456789]], meta: { source: "demo" }, truncated: true }),
  );
  const frameCells = elements(frame).filter((node) => node.tagName === "TD");
  assert.equal(frameCells[0].getAttribute("title"), "1.23456789");
  assert.match(frame.textContent, /source/);
  assert.match(frame.textContent, /truncatedtrue/);

  const failed = toolResultBody(JSON.stringify({ ok: false, error: { type: "ValueError", message: "bad", code: 7 }, retryable: false }));
  assert.match(failed.textContent, /ValueError: bad/);
  assert.match(failed.textContent, /code7/);
  assert.match(failed.textContent, /retryablefalse/);
});

test("restored assistant geometry remains fully inside the viewport", () => {
  const geometry = clampPanelGeometry({ width: 1000, height: 900, right: 200, bottom: 200 }, 320, 240);
  assert.equal(geometry.right + geometry.width, 312);
  assert.equal(geometry.bottom + geometry.height, 232);
  assert.ok(geometry.width >= 300);
  assert.ok(geometry.height > 0);

  const shifted = clampPanelGeometry({ width: 360, height: 460, right: 700, bottom: 500 }, 800, 700);
  assert.deepEqual(shifted, { width: 360, height: 460, right: 432, bottom: 232 });

  // Narrow viewports keep the compact defaults when geometry is missing.
  const corrupt = clampPanelGeometry({ width: null, height: "", right: null, bottom: false }, 800, 700);
  assert.deepEqual(corrupt, { width: 360, height: 460, right: 16, bottom: 92 });

  const tiny = clampPanelGeometry({ width: Infinity, height: NaN, right: -10, bottom: -20 }, 100, 80);
  assert.deepEqual(tiny, { width: 92, height: 72, right: 0, bottom: 0 });

  // Large viewports: default and cap are both ~half width/height (~¼ screen).
  const capped = clampPanelGeometry({ width: 1800, height: 1200, right: 0, bottom: 0 }, 1400, 1000);
  assert.deepEqual(capped, { width: 700, height: 550, right: 0, bottom: 0 });

  const fresh = clampPanelGeometry({}, 1400, 1000);
  assert.deepEqual(fresh, { width: 700, height: 550, right: 16, bottom: 92 });
});
