#!/usr/bin/env node
// Keeps code split by functionality, with three measurable guards (see "Organize Code by Functionality" in AGENTS.md):
//   - a React component's JSX `return` stays short (sections become their own components)
//   - a Python function stays short (steps become their own functions or modules)
//   - no source file grows past the file limit (the backstop for everything piling into one file)
// What is already over a limit is listed in .agents/file_size_baseline.json and may only shrink.
//
//   node .agents/check_file_sizes.mjs            check (fails on anything new over a limit, or a baselined item that grew)
//   node .agents/check_file_sizes.mjs --update   lower baselined ceilings to today's sizes and drop items back under the
//                                                limit (never raises a ceiling: split the code instead)
//   node .agents/check_file_sizes.mjs --init     baseline everything over a limit now (first setup only)
import { execSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const BASELINE = join(ROOT, ".agents", "file_size_baseline.json");
const SOURCE = /\.(py|ts|tsx|js|jsx|mjs|cjs)$/;
const SKIP = /(^|\/)(node_modules|dist|build|coverage|migrations|alembic|__pycache__|\.agents|public|vendor)\//;
const SKIP_FILE = /(\.d\.ts|\.min\.js|vite-env\.d\.ts|package-lock\.json)$/;

const baseline = existsSync(BASELINE) ? JSON.parse(readFileSync(BASELINE, "utf8")) : {};
const LIMIT = baseline.limit || 500;
const BLOCK_LIMITS = { jsx_return: 80, py_function: 120, ...(baseline.block_limits || {}) };
const files = baseline.files || {};
const blocks = baseline.blocks || {};
const mode = process.argv.includes("--init") ? "init" : process.argv.includes("--update") ? "update" : "check";
const HOW = {
  file: "split it by functionality into smaller modules",
  jsx_return: "move each section of the JSX into its own component (and logic into a hook)",
  py_function: "split it into smaller functions, or move a step into its own module",
};

const sources = execSync("git ls-files --cached --others --exclude-standard", { cwd: ROOT, encoding: "utf8" })
  .split("\n")
  .filter((f) => f && SOURCE.test(f) && !SKIP.test(f) && !SKIP_FILE.test(f) && existsSync(join(ROOT, f)));

const indentOf = (l) => l.match(/^\s*/)[0].length;

// JSX: `return (` ... the `)` / `);` at the same indent. Keyed by the component the return belongs to.
function jsxReturns(file, lines) {
  const out = {};
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(/^(\s*)return \($/);
    if (!m) continue;
    const ind = m[1].length;
    let end = i + 1;
    while (end < lines.length && !(indentOf(lines[end]) === ind && /^\s*\);?\s*$/.test(lines[end]))) end++;
    if (end >= lines.length) continue;
    let name = "component";
    for (let k = i - 1; k >= 0; k--) {
      const c = lines[k].match(/^\s*(?:export\s+)?(?:default\s+)?(?:function\s+([A-Z]\w*)|const\s+([A-Z]\w*)\s*[:=])/);
      if (c && indentOf(lines[k]) < ind) { name = c[1] || c[2]; break; }
    }
    const key = `${file}#${name}`;
    out[key] = Math.max(out[key] || 0, end - i + 1);
  }
  return out;
}

// Python: a def runs until the next non-blank line at its indent or less (decorators and comments aside).
function pyFunctions(file, lines) {
  const out = {};
  const classes = [];
  for (let i = 0; i < lines.length; i++) {
    const c = lines[i].match(/^(\s*)class\s+(\w+)/);
    if (c) {
      while (classes.length && classes.at(-1).ind >= c[1].length) classes.pop();
      classes.push({ ind: c[1].length, name: c[2] });
      continue;
    }
    const m = lines[i].match(/^(\s*)(?:async\s+)?def\s+(\w+)/);
    if (!m) continue;
    const ind = m[1].length;
    let j = i;
    while (j < lines.length && !/:\s*(#.*)?$/.test(lines[j])) j++; // multi-line signature
    let last = j;
    for (let k = j + 1; k < lines.length; k++) {
      const t = lines[k];
      if (!t.trim() || /^\s*#/.test(t)) continue;
      if (indentOf(t) <= ind) break;
      last = k;
    }
    while (classes.length && classes.at(-1).ind >= ind) classes.pop();
    const key = `${file}#${classes.length ? classes.at(-1).name + "." : ""}${m[2]}`;
    out[key] = Math.max(out[key] || 0, last - i + 1);
  }
  return out;
}

const fileSizes = {};
const blockSizes = {}; // key -> { n, kind }
for (const f of sources) {
  const text = readFileSync(join(ROOT, f), "utf8");
  const lines = text.split(/\r?\n/);
  if (text.endsWith("\n")) lines.pop();
  fileSizes[f] = lines.length;
  if (/\.(tsx|jsx)$/.test(f)) for (const [k, n] of Object.entries(jsxReturns(f, lines))) blockSizes[k] = { n, kind: "jsx_return" };
  if (f.endsWith(".py")) for (const [k, n] of Object.entries(pyFunctions(f, lines))) blockSizes[k] = { n, kind: "py_function" };
}

const save = () => {
  const sort = (o) => Object.fromEntries(Object.entries(o).sort(([a], [b]) => a.localeCompare(b)));
  writeFileSync(BASELINE, JSON.stringify({ limit: LIMIT, block_limits: BLOCK_LIMITS, files: sort(files), blocks: sort(blocks) }, null, 2) + "\n");
};

if (mode === "init") {
  let nf = 0, nb = 0;
  for (const [f, n] of Object.entries(fileSizes))
    if (n > LIMIT && !files[f]) { files[f] = { max_lines: n, reason: "Over the limit when the check was introduced; split it when you next work on it." }; nf++; }
  for (const [k, { n, kind }] of Object.entries(blockSizes))
    if (n > BLOCK_LIMITS[kind] && !blocks[k]) { blocks[k] = { max_lines: n, kind }; nb++; }
  save();
  console.log(`Baselined ${nf} file(s) over ${LIMIT} lines and ${nb} long JSX return(s)/function(s) (${Object.keys(files).length} files, ${Object.keys(blocks).length} blocks in total).`);
  process.exit(0);
}

const errors = [];
const stale = { files: [], blocks: [] };
const judge = (key, n, limit, entry, what, how, store, staleList) => {
  if (n <= limit) { if (entry) staleList.push(key); return; }
  if (!entry) errors.push(`${key}: ${what} is ${n} lines, over the ${limit}-line limit: ${how}.`);
  else if (n > entry.max_lines) errors.push(`${key}: ${what} grew to ${n} lines, past its ceiling of ${entry.max_lines}: ${how}.`);
  else if (mode === "update" && n < entry.max_lines) store[key].max_lines = n;
};
for (const [f, n] of Object.entries(fileSizes)) judge(f, n, LIMIT, files[f], "the file", HOW.file, files, stale.files);
for (const [k, { n, kind }] of Object.entries(blockSizes))
  judge(k, n, BLOCK_LIMITS[kind], blocks[k], kind === "jsx_return" ? "its JSX return" : "the function", HOW[kind], blocks, stale.blocks);
for (const f of Object.keys(files)) if (!(f in fileSizes)) stale.files.push(f);
for (const k of Object.keys(blocks)) if (!(k in blockSizes)) stale.blocks.push(k);

if (mode === "update") {
  stale.files.forEach((f) => delete files[f]);
  stale.blocks.forEach((k) => delete blocks[k]);
  save();
  console.log(`Baseline updated: ${Object.keys(files).length} file(s) and ${Object.keys(blocks).length} block(s) still over their limits.`);
}
if (errors.length) {
  console.error(`Code size check failed:\n  ${errors.join("\n  ")}`);
  process.exit(1);
}
if (mode === "check") {
  const n = stale.files.length + stale.blocks.length;
  console.log(`Code size check passed (${sources.length} files; limits: file ${LIMIT}, JSX return ${BLOCK_LIMITS.jsx_return}, Python function ${BLOCK_LIMITS.py_function}).` +
    (n ? ` ${n} baselined item(s) are now small enough: run with --update to drop them.` : ""));
}
