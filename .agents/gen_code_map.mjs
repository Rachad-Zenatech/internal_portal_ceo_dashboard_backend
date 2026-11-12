#!/usr/bin/env node
// Generates:
// 1. .agents/project-index.md: High-level overview index containing every source file with its description and line count.
// 2. .agents/code-map/<folder>.md: Detailed symbol breakdown (functions, classes, routes, components) with each symbol's
//    start line, one small file per source folder, so an agent's lookup never loads the whole map.
// 3. .agents/code-map.md: The list of those folder files (where to grep).
//
// Regenerate after changing code:   node .agents/gen_code_map.mjs
import { execSync } from "node:child_process";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT_MAP = join(ROOT, ".agents", "code-map.md");
const OUT_MAP_DIR = join(ROOT, ".agents", "code-map");
const OUT_INDEX = join(ROOT, ".agents", "project-index.md");
const FILES_CMD = "git ls-files";
const SOURCE = /\.(py|ts|tsx|js|jsx|mjs)$/;
const SKIP = /(^|\/)(node_modules|dist|build|coverage|migrations|alembic|__pycache__|\.agents|public)\//;
const SKIP_FILE = /(\.d\.ts|\.min\.js|vite-env\.d\.ts|\.test\.|\.spec\.|setupTests)/;
// Files above this size also list their nested handlers/helpers, because those are what agents hunt for.
const NESTED_THRESHOLD = 400;

const files = execSync(FILES_CMD, { cwd: ROOT, encoding: "utf8" })
  .split("\n")
  .filter((f) => SOURCE.test(f) && !SKIP.test(f) && !SKIP_FILE.test(f))
  .sort();

function summary(text) {
  const s = text.replace(/^[\s*/#"'`]+|[\s*/"'`]+$/g, "").trim();
  const first = s.split(/(?<=\.)\s/)[0];
  return first.length > 140 ? first.slice(0, 137) + "..." : first;
}

function fileSummary(text, filename) {
  // Python docstring at top
  const pyMatch = text.match(/^\s*(?:"""|''')([\s\S]*?)(?:"""|''')/);
  if (pyMatch) {
    const s = pyMatch[1].replace(/^\s*\*|\s\*\s/g, " ").replace(/\s+/g, " ").trim();
    if (s) {
      const first = s.split(/(?<=\.)\s/)[0];
      return first.length > 140 ? first.slice(0, 137) + "..." : first;
    }
  }

  // JS/TS block comment at top
  const jsMatch = text.match(/^\s*\/\*\*?([\s\S]*?)\*\//);
  if (jsMatch) {
    const s = jsMatch[1].replace(/^\s*\*|\s\*\s/g, " ").replace(/\s+/g, " ").trim();
    if (s) {
      const first = s.split(/(?<=\.)\s/)[0];
      return first.length > 140 ? first.slice(0, 137) + "..." : first;
    }
  }

  // Any comment in the first 25 lines (including comments right after imports)
  const lines = text.split(/\r?\n/).slice(0, 25);
  for (const l of lines) {
    const m = l.match(/^\s*(?:\/\/|#|\/\*\*?)\s*([A-Za-z0-9].*)/);
    if (m && !m[1].startsWith("eslint") && !m[1].startsWith("@ts-") && !m[1].startsWith("!/") && !m[1].startsWith("import")) {
      const clean = m[1].replace(/\*\/$/, "").trim();
      if (clean.length > 10) return summary(clean);
    }
  }

  // Meaningful human-readable fallback with directory context
  const parts = filename.split("/");
  const name = parts.pop().replace(/\.[^.]+$/, "");
  const dir = parts.slice(1).join(" ");
  const cleanName = name.replace(/([A-Z])/g, " $1").replace(/[_-]+/g, " ").toLowerCase().trim();
  const cleanDir = dir.replace(/([A-Z])/g, " $1").replace(/[_-]+/g, " ").toLowerCase().trim();
  return cleanDir ? `${cleanName} (${cleanDir})` : cleanName;
}

function pyEntries(lines) {
  const out = [];
  let decorators = [];
  let decoratorLine = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const dec = line.match(/^(\s{0,4})@(\w+)\.(get|post|put|patch|delete|websocket)\(\s*["']([^"']*)["']/);
    if (dec) {
      decorators.push(`${dec[3].toUpperCase()} ${dec[4]}`);
      if (decoratorLine === null) decoratorLine = i + 1;
      continue;
    }
    const m = line.match(/^(\s{0,4})(async\s+def|def|class)\s+(\w+)/);
    if (!m) {
      if (!/^\s*@/.test(line)) {
        decorators = [];
        decoratorLine = null;
      }
      continue;
    }
    const indent = m[1].length;
    const kind = m[2] === "class" ? "class" : "def";
    let j = i;
    while (j < lines.length && !/:\s*(#.*)?$/.test(lines[j])) j++;
    let doc = "";
    let k = j + 1;
    while (k < lines.length && lines[k].trim() === "") k++;
    if (k < lines.length && /^\s*[rbu]?("""|''')/.test(lines[k])) {
      let body = lines[k].trim().replace(/^[rbu]?("""|''')/, "");
      while (!body.trim() && k + 1 < lines.length) body = lines[++k].trim();
      doc = summary(body.replace(/("""|''').*$/, ""));
    }
    out.push({ depth: indent ? 1 : 0, name: m[3], kind, route: decorators.join(", "), doc, line: decoratorLine ?? i + 1 });
    decorators = [];
    decoratorLine = null;
  }
  return out;
}

function tsEntries(lines, nested) {
  const out = [];
  const top =
    /^(export\s+)?(default\s+)?(async\s+)?(function\*?|class|interface|type|enum|const|let)\s+([A-Za-z_$][\w$]*)/;
  const inner = /^\s{2,4}(const|function|async function)\s+([A-Za-z_$][\w$]*)\s*(=\s*(async\s*)?(\(|use[A-Z]\w*\(|function)|\()/;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let m = line.match(top);
    let depth = 0;
    let name, kind;
    if (m) {
      kind = m[4];
      name = m[5];
      if ((kind === "const" || kind === "let") && !m[1] && !/=\s*(async\s*)?(\(|function|React\.|memo|forwardRef|styled|create)/.test(line))
        continue;
      if (m[1]) kind = "export " + kind;
    } else if (nested && (m = line.match(inner))) {
      depth = 1;
      kind = "fn";
      name = m[2];
    } else continue;
    let doc = "";
    let k = i - 1;
    if (k >= 0 && /\*\/\s*$/.test(lines[k])) {
      while (k > 0 && !/\/\*\*?/.test(lines[k])) k--;
      const block = lines.slice(k, i).join(" ").replace(/\/\*\*?|\*\/|^\s*\*|\s\*\s/g, " ");
      doc = summary(block.replace(/@\w+.*$/, ""));
    } else if (k >= 0 && /^\s*\/\/\s*\S/.test(lines[k])) {
      doc = summary(lines[k].replace(/^\s*\/\/\s*/, ""));
    }
    out.push({ depth, name, kind, route: "", doc, line: i + 1 });
  }
  return out;
}

const fileData = [];

for (const f of files) {
  let text;
  try {
    text = readFileSync(join(ROOT, f), "utf8");
  } catch {
    continue;
  }
  const lines = text.split(/\r?\n/);
  const isPy = f.endsWith(".py");
  const entries = isPy ? pyEntries(lines) : tsEntries(lines, lines.length > NESTED_THRESHOLD);
  if (!entries.length && lines.length < 50) continue;
  const fileDesc = fileSummary(text, f);
  fileData.push({ f, lineCount: lines.length, fileDesc, entries });
}

const GENERATED = "<!-- Generated by `node .agents/gen_code_map.mjs` - do not edit by hand; regenerate after changes. -->";

// 1. Output project-index.md: Only file paths and their descriptions
const indexParts = [
  "# Project Index",
  "",
  GENERATED,
  "",
  "Scan this index first to locate the right file and description without loading symbol trees or line numbers.",
  "For function and route definitions with their line numbers, grep `.agents/code-map/` (listed in `.agents/code-map.md`).",
  "",
];

for (const item of fileData) {
  const desc = item.fileDesc ? ` - ${item.fileDesc}` : "";
  indexParts.push(`- ${item.f} (${item.lineCount} lines)${desc}`);
}
indexParts.push("");
writeFileSync(OUT_INDEX, indexParts.join("\n"));
console.log(`Wrote ${OUT_INDEX} (${fileData.length} files)`);

// 2. Output .agents/code-map/<folder>.md: one small symbol map per source folder, with each symbol's start line
const folderName = (f) => {
  const dir = f.includes("/") ? f.slice(0, f.lastIndexOf("/")) : "";
  return dir ? dir.replace(/[\/\\]/g, "__") : "_root";
};
const byFolder = new Map();
for (const item of fileData) {
  const key = folderName(item.f);
  if (!byFolder.has(key)) byFolder.set(key, []);
  byFolder.get(key).push(item);
}

rmSync(OUT_MAP_DIR, { recursive: true, force: true }); // folders that no longer exist leave no stale map behind
mkdirSync(OUT_MAP_DIR, { recursive: true });
const written = [];
for (const [key, items] of [...byFolder.entries()].sort(([a], [b]) => a.localeCompare(b))) {
  const parts = [
    `# Code map: ${key === "_root" ? "(project root)" : items[0].f.slice(0, items[0].f.lastIndexOf("/"))}`,
    "",
    GENERATED,
    "",
    "Each symbol shows its start line (`L<n>`); read from there to the next symbol's line, not the whole file.",
    "",
  ];
  for (const item of items) {
    const descSuffix = item.fileDesc ? ` - ${item.fileDesc}` : "";
    parts.push(`## ${item.f} (${item.lineCount} lines)${descSuffix}`);
    for (const e of item.entries) {
      const pad = e.depth ? "    " : "";
      const route = e.route ? ` [${e.route}]` : "";
      const doc = e.doc ? ` - ${e.doc}` : "";
      parts.push(`${pad}- \`${e.name}\` (${e.kind}) L${e.line}${route}${doc}`);
    }
    parts.push("");
  }
  const text = parts.join("\n");
  writeFileSync(join(OUT_MAP_DIR, `${key}.md`), text);
  written.push({ key, files: items.length, kb: Math.max(1, Math.round(Buffer.byteLength(text) / 1024)) });
}
console.log(`Wrote ${OUT_MAP_DIR} (${written.length} folder maps, ${fileData.length} files)`);

// 3. Output code-map.md: where the folder maps are, and how to use them
const mapParts = [
  "# Code map",
  "",
  GENERATED,
  "",
  "The symbol map is split into one small file per source folder in `.agents/code-map/`, so a lookup never loads the",
  "whole map. Do not read these files whole: grep them, e.g. `grep -rn \"my_function\" .agents/code-map/`, then read",
  "only the lines the match gives (each symbol shows its start line, `L<n>`). Check `.agents/project-index.md` first",
  "to find the right file.",
  "",
  "| Folder map | Files | Size |",
  "| --- | --- | --- |",
  ...written.map((w) => `| .agents/code-map/${w.key}.md | ${w.files} | ${w.kb} KB |`),
  "",
];
writeFileSync(OUT_MAP, mapParts.join("\n"));
console.log(`Wrote ${OUT_MAP}`);
