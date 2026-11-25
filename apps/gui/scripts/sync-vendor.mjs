import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const check = process.argv.includes("--check");
const assets = [
  ["node_modules/codemirror/lib/codemirror.js", "ui/js/codemirror.js"],
  ["node_modules/codemirror/lib/codemirror.css", "ui/codemirror.css"],
  ["node_modules/codemirror/mode/javascript/javascript.js", "ui/js/codemirror-json.js"],
  ["node_modules/codemirror/addon/edit/matchbrackets.js", "ui/js/codemirror-matchbrackets.js"],
  ["node_modules/codemirror/addon/edit/closebrackets.js", "ui/js/codemirror-closebrackets.js"],
  ["node_modules/codemirror/LICENSE", "ui/assets/codemirror-LICENSE.txt"],
];

for (const [sourceName, targetName] of assets) {
  const source = resolve(root, sourceName);
  const target = resolve(root, targetName);
  const next = readFileSync(source);
  if (check) {
    let current;
    try {
      current = readFileSync(target);
    } catch {
      throw new Error(`Missing vendored CodeMirror asset: ${targetName}`);
    }
    if (!current.equals(next)) throw new Error(`Vendored CodeMirror asset is stale: ${targetName}`);
    continue;
  }
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, next);
}
