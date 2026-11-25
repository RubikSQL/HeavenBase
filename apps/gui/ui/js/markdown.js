// Minimal, XSS-safe Markdown renderer for assistant replies, authored in TypeScript.
// Builds DOM nodes directly (never innerHTML with model text), covering the
// subset LLMs actually emit: headings, bold/italic/code, lists, tables, code fences.
import { el } from "./ui.js";
// ------------------------------------------------------------------- math
// Tiny dependency-free TeX subset. Parsing is deliberately bounded because
// assistant output is untrusted and live replies re-render while streaming.
const MAX_INLINE_MATH_CHARS = 200;
const MAX_MATH_CHARS = 2000;
const MAX_TEX_DEPTH = 24;
const MAX_TEX_NODES = 512;
const TEX_SYMBOLS = {
    alpha: "α",
    beta: "β",
    gamma: "γ",
    delta: "δ",
    epsilon: "ε",
    varepsilon: "ε",
    zeta: "ζ",
    eta: "η",
    theta: "θ",
    iota: "ι",
    kappa: "κ",
    lambda: "λ",
    mu: "μ",
    nu: "ν",
    xi: "ξ",
    pi: "π",
    rho: "ρ",
    sigma: "σ",
    tau: "τ",
    upsilon: "υ",
    phi: "φ",
    varphi: "φ",
    chi: "χ",
    psi: "ψ",
    omega: "ω",
    Gamma: "Γ",
    Delta: "Δ",
    Theta: "Θ",
    Lambda: "Λ",
    Xi: "Ξ",
    Pi: "Π",
    Sigma: "Σ",
    Upsilon: "Υ",
    Phi: "Φ",
    Psi: "Ψ",
    Omega: "Ω",
    cdot: "·",
    times: "×",
    div: "÷",
    pm: "±",
    mp: "∓",
    le: "≤",
    leq: "≤",
    ge: "≥",
    geq: "≥",
    ne: "≠",
    neq: "≠",
    approx: "≈",
    sim: "∼",
    equiv: "≡",
    propto: "∝",
    infty: "∞",
    sum: "∑",
    prod: "∏",
    int: "∫",
    partial: "∂",
    nabla: "∇",
    hbar: "ℏ",
    ell: "ℓ",
    degree: "°",
    to: "→",
    rightarrow: "→",
    leftarrow: "←",
    Rightarrow: "⇒",
    Leftarrow: "⇐",
    mapsto: "↦",
    in: "∈",
    notin: "∉",
    subset: "⊂",
    supset: "⊃",
    cup: "∪",
    cap: "∩",
    forall: "∀",
    exists: "∃",
    emptyset: "∅",
    ldots: "…",
    cdots: "⋯",
    dots: "…",
    circ: "∘",
    perp: "⊥",
    parallel: "∥",
    angle: "∠",
    langle: "⟨",
    rangle: "⟩",
    quad: " ",
    qquad: "  ",
};
function isEscaped(text, index) {
    let slashes = 0;
    for (let i = index - 1; i >= 0 && text[i] === "\\"; i--)
        slashes++;
    return slashes % 2 === 1;
}
function findDelimiter(text, start, delimiter) {
    for (let i = start; i <= text.length - delimiter.length; i++) {
        if (text.startsWith(delimiter, i) && !isEscaped(text, i))
            return i;
    }
    return -1;
}
function closingBrace(tex, start) {
    let depth = 1;
    for (let i = start + 1; i < tex.length; i++) {
        if (isEscaped(tex, i))
            continue;
        if (tex[i] === "{")
            depth++;
        else if (tex[i] === "}" && --depth === 0)
            return i;
    }
    return -1;
}
function texGroup(tex, index, state, depth) {
    let i = index;
    while (/\s/.test(tex[i] || ""))
        i++;
    if (i >= tex.length)
        return { ok: false, nodes: [], next: i };
    if (tex[i] === "{") {
        const close = closingBrace(tex, i);
        if (close < 0)
            return { ok: false, nodes: [], next: tex.length };
        const parsed = texNodes(tex.slice(i + 1, close), state, depth + 1);
        return { ...parsed, next: close + 1 };
    }
    if (tex[i] === "\\") {
        const command = tex.slice(i).match(/^\\([A-Za-z]+|.)/);
        if (!command)
            return { ok: false, nodes: [], next: tex.length };
        const parsed = texNodes(command[0], state, depth + 1);
        return { ...parsed, next: i + command[0].length };
    }
    const parsed = texNodes(tex[i] ?? "", state, depth + 1);
    return { ...parsed, next: i + 1 };
}
function texNodes(tex, state = { count: 0 }, depth = 0) {
    if (depth > MAX_TEX_DEPTH)
        return { ok: false, nodes: [] };
    const nodes = [];
    let i = 0;
    let text = "";
    const flush = () => {
        if (!text)
            return true;
        if (++state.count > MAX_TEX_NODES)
            return false;
        nodes.push(document.createTextNode(text));
        text = "";
        return true;
    };
    const appendElement = (node) => {
        if (!flush() || ++state.count > MAX_TEX_NODES)
            return false;
        nodes.push(node);
        return true;
    };
    while (i < tex.length) {
        const ch = tex[i] ?? "";
        if (ch === "\\") {
            const cmd = tex.slice(i).match(/^\\([A-Za-z]+|.)/);
            if (!cmd)
                return { ok: false, nodes: [] };
            const name = cmd[1] ?? "";
            i += cmd[0].length;
            if (name === "frac" || name === "dfrac" || name === "tfrac") {
                const num = texGroup(tex, i, state, depth);
                const den = num.ok
                    ? texGroup(tex, num.next, state, depth)
                    : { ok: false, nodes: [], next: num.next };
                if (!num.ok || !den.ok)
                    return { ok: false, nodes: [] };
                i = den.next;
                if (!appendElement(el("span.math-frac", {}, el("span.math-num", {}, num.nodes), el("span.math-den", {}, den.nodes))))
                    return { ok: false, nodes: [] };
            }
            else if (name === "sqrt") {
                const body = texGroup(tex, i, state, depth);
                if (!body.ok)
                    return { ok: false, nodes: [] };
                i = body.next;
                text += "√";
                if (!appendElement(el("span.math-sqrt", {}, body.nodes)))
                    return { ok: false, nodes: [] };
            }
            else if (name === "text" || name === "mathrm" || name === "mathbf" || name === "operatorname") {
                const body = texGroup(tex, i, state, depth);
                if (!body.ok)
                    return { ok: false, nodes: [] };
                i = body.next;
                const tag = name === "mathbf" ? "span.math-up.math-bold" : "span.math-up";
                if (!appendElement(el(tag, {}, body.nodes)))
                    return { ok: false, nodes: [] };
            }
            else if (name === "vec" || name === "boldsymbol") {
                const body = texGroup(tex, i, state, depth);
                if (!body.ok)
                    return { ok: false, nodes: [] };
                i = body.next;
                if (!appendElement(el(name === "vec" ? "span.math-vec" : "span.math-bold", {}, body.nodes)))
                    return { ok: false, nodes: [] };
            }
            else if (name === "left" || name === "right" || name === "displaystyle") {
                // Layout-only commands do not change the textual expression.
            }
            else if (TEX_SYMBOLS[name] != null) {
                text += TEX_SYMBOLS[name];
            }
            else if (name === "," || name === ";" || name === " ") {
                text += " ";
            }
            else if (name.length === 1 && "{}_$%#&\\".includes(name)) {
                text += name;
            }
            else {
                text += `\\${name}`;
            }
        }
        else if (ch === "^" || ch === "_") {
            const body = texGroup(tex, i + 1, state, depth);
            if (!body.ok)
                return { ok: false, nodes: [] };
            i = body.next;
            if (!appendElement(el(ch === "^" ? "sup" : "sub", {}, body.nodes)))
                return { ok: false, nodes: [] };
        }
        else if (ch === "{") {
            const body = texGroup(tex, i, state, depth);
            if (!body.ok || !flush())
                return { ok: false, nodes: [] };
            nodes.push(...body.nodes);
            i = body.next;
        }
        else if (ch === "}") {
            return { ok: false, nodes: [] };
        }
        else {
            text += ch;
            i++;
        }
    }
    return { ok: flush(), nodes };
}
function mathNode(tex, { display = false, source = tex } = {}) {
    const body = tex.trim();
    if (body.length > MAX_MATH_CHARS)
        return display ? el("div.md-p", {}, source) : document.createTextNode(source);
    const parsed = texNodes(body);
    if (!parsed.ok)
        return display ? el("div.md-p", {}, source) : document.createTextNode(source);
    return el(display ? "div.md-math" : "span.math", { role: "math", "aria-label": body || source }, parsed.nodes);
}
// A single-$ span counts as math only when it looks like a formula, so prices
// in plain prose ("$100 and $200") stay literal text.
function isFormula(body) {
    return body.length <= MAX_INLINE_MATH_CHARS && !/^\s|\s$/.test(body) && /[\\^_=]|[A-Za-z]/.test(body);
}
function inline(text) {
    // Scan delimiters so escaped currency signs and code spans cannot be mistaken
    // for math. All content remains text nodes or fixed-name elements.
    const nodes = [];
    let plain = "";
    let i = 0;
    const missingClosers = new Set();
    const flush = () => {
        if (plain)
            nodes.push(document.createTextNode(plain));
        plain = "";
    };
    const closeFor = (start, delimiter) => {
        if (missingClosers.has(delimiter))
            return -1;
        const close = findDelimiter(text, start, delimiter);
        if (close < 0)
            missingClosers.add(delimiter);
        return close;
    };
    const appendMath = (body, source) => {
        flush();
        nodes.push(mathNode(body, { source }));
    };
    while (i < text.length) {
        if (text[i] === "`" && !isEscaped(text, i)) {
            const close = closeFor(i + 1, "`");
            if (close >= 0) {
                flush();
                nodes.push(el("code", {}, text.slice(i + 1, close)));
                i = close + 1;
                continue;
            }
        }
        if ((text.startsWith("\\(", i) || text.startsWith("\\[", i)) && !isEscaped(text, i)) {
            const opening = text.slice(i, i + 2);
            const closing = opening === "\\(" ? "\\)" : "\\]";
            const close = closeFor(i + 2, closing);
            if (close >= 0) {
                const source = text.slice(i, close + 2);
                appendMath(text.slice(i + 2, close), source);
                i = close + 2;
                continue;
            }
            plain += opening;
            i += 2;
            continue;
        }
        if (text[i] === "\\" && "\\$*_`[]()".includes(text[i + 1] || "")) {
            plain += text[i + 1];
            i += 2;
            continue;
        }
        if (text.startsWith("$$", i) && !isEscaped(text, i)) {
            const close = closeFor(i + 2, "$$");
            if (close >= 0) {
                const source = text.slice(i, close + 2);
                appendMath(text.slice(i + 2, close), source);
                i = close + 2;
                continue;
            }
        }
        if (text[i] === "$" && text[i - 1] !== "$" && text[i + 1] !== "$" && !isEscaped(text, i)) {
            let close = i + 1;
            while (close >= 0) {
                close = text.indexOf("$", close);
                if (close < 0)
                    break;
                if (!isEscaped(text, close) && text[close - 1] !== "$" && text[close + 1] !== "$")
                    break;
                close++;
            }
            if (close >= 0) {
                const body = text.slice(i + 1, close);
                if (isFormula(body)) {
                    appendMath(body, text.slice(i, close + 1));
                    i = close + 1;
                    continue;
                }
            }
        }
        if ((text[i] === "*" || text[i] === "_") && !isEscaped(text, i)) {
            const strong = text.slice(i).match(/^(\*\*|__)(.+?)\1/);
            if (strong) {
                flush();
                nodes.push(el("strong", {}, strong[2]));
                i += strong[0].length;
                continue;
            }
            const emphasis = text.slice(i).match(/^(\*|_)(.+?)\1/);
            if (emphasis) {
                flush();
                nodes.push(el("em", {}, emphasis[2]));
                i += emphasis[0].length;
                continue;
            }
        }
        plain += text[i++];
    }
    flush();
    return nodes;
}
function displayMathBlock(lines, index) {
    const opening = (lines[index] ?? "").match(/^\s*(\$\$|\\\[)(.*)$/);
    if (!opening)
        return null;
    const open = opening[1] ?? "";
    const closeToken = open === "$$" ? "$$" : "\\]";
    const body = [];
    let lineIndex = index;
    let remainder = opening[2] ?? "";
    for (;;) {
        const close = findDelimiter(remainder, 0, closeToken);
        if (close >= 0) {
            body.push(remainder.slice(0, close));
            const source = `${open}${body.join("\n")}${closeToken}`;
            const nodes = [mathNode(body.join("\n"), { display: true, source })];
            const suffix = remainder.slice(close + closeToken.length).trimStart();
            if (suffix)
                nodes.push(el("div.md-p", {}, inline(suffix)));
            return { next: lineIndex + 1, nodes };
        }
        body.push(remainder);
        if (++lineIndex >= lines.length)
            return null;
        remainder = lines[lineIndex] ?? "";
    }
}
function splitRow(line) {
    return line
        .replace(/^\||\|$/g, "")
        .split("|")
        .map((cell) => cell.trim());
}
export function markdown(text) {
    const root = el("div.md");
    const lines = String(text).replace(/\r\n/g, "\n").split("\n");
    let i = 0;
    let list = null;
    const flushList = () => {
        if (list)
            root.append(list.node);
        list = null;
    };
    while (i < lines.length) {
        const line = lines[i] ?? "";
        // Display math is structural: it cannot be swallowed by paragraph merging,
        // and an unmatched opener remains literal text.
        const displayMath = displayMathBlock(lines, i);
        if (displayMath) {
            flushList();
            root.append(...displayMath.nodes);
            i = displayMath.next;
            continue;
        }
        // Code fence
        if (/^```/.test(line)) {
            flushList();
            const body = [];
            i++;
            while (i < lines.length && !/^```/.test(lines[i] ?? ""))
                body.push(lines[i++] ?? "");
            i++; // closing fence
            root.append(el("pre.md-code", {}, body.join("\n")));
            continue;
        }
        // Table: header row + separator row of dashes
        if (line.includes("|") &&
            i + 1 < lines.length &&
            /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1] ?? "") &&
            (lines[i + 1] ?? "").includes("-")) {
            flushList();
            const headers = splitRow(line);
            i += 2;
            const bodyRows = [];
            while (i < lines.length && (lines[i] ?? "").includes("|") && (lines[i] ?? "").trim()) {
                bodyRows.push(splitRow(lines[i++] ?? ""));
            }
            const wrap = el("div.md-table-wrap");
            const table = el("table.md-table");
            table.append(el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, inline(h))))));
            table.append(el("tbody", {}, bodyRows.map((row) => el("tr", {}, headers.map((_, c) => el("td", {}, inline(row[c] ?? "")))))));
            wrap.append(table);
            root.append(wrap);
            continue;
        }
        // Heading
        const heading = line.match(/^(#{1,6})\s+(.*)$/);
        if (heading) {
            flushList();
            root.append(el(`div.md-h${(heading[1] ?? "").length}`, {}, inline(heading[2] ?? "")));
            i++;
            continue;
        }
        // List item (ordered or unordered)
        const li = line.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
        if (li) {
            const ordered = /\d/.test(li[1] ?? "");
            if (!list || list.ordered !== ordered) {
                flushList();
                list = { node: el(ordered ? "ol.md-list" : "ul.md-list"), ordered };
            }
            list.node.append(el("li", {}, inline(li[2] ?? "")));
            i++;
            continue;
        }
        // Blank line ends a list/paragraph
        if (!line.trim()) {
            flushList();
            i++;
            continue;
        }
        // Paragraph (merge consecutive non-empty non-structural lines)
        flushList();
        const para = [line];
        i++;
        while (i < lines.length &&
            (lines[i] ?? "").trim() &&
            !/^(#{1,6}\s|```|\s*(\$\$|\\\[)|\s*([-*+]|\d+[.)])\s)/.test(lines[i] ?? "") &&
            !((lines[i] ?? "").includes("|") && (lines[i] ?? "").includes("-"))) {
            para.push(lines[i++] ?? "");
        }
        root.append(el("div.md-p", {}, inline(para.join(" "))));
    }
    flushList();
    return root;
}
