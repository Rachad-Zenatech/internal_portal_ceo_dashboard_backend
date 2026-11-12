#!/usr/bin/env node
// Keeps source files small: no first-party source file over the limit (500 lines), except files already that big,
// which are listed in .agents/file_size_baseline.json and may only shrink.
//
//   node .agents/check_file_sizes.mjs            check (fails when a file is too big or a baselined file grew)
//   node .agents/check_file_sizes.mjs --update   lower baselined ceilings to the current sizes, drop files back under
//                                                the limit (never raises a ceiling: split the file instead)
//   node .agents/check_file_sizes.mjs --init     baseline every file over the limit now (first setup only)
import { execSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const BASELINE = join(ROOT, ".agents", "file_size_baseline.json");
const SOURCE = /\.(py|ts|tsx|js|jsx|mjs|cjs)$/;
const SKIP = /(^|\/)(node_modules|dist|build|coverage|migrations|alembic|__pycache__|\.agents|public|vendor)\//;
const SKIP_FILE = /(\.d\.ts|\.min\.js|vite-env\.d\.ts|package-lock\.json)$/;

const baseline = existsSync(BASELINE) ? JSON.parse(readFileSync(BASELINE, "utf8")) : { limit: 500, files: {} };
const LIMIT = baseline.limit || 500;
const entries = baseline.files || {};
const mode = process.argv.includes("--init") ? "init" : process.argv.includes("--update") ? "update" : "check";

const files = execSync("git ls-files --cached --others --exclude-standard", { cwd: ROOT, encoding: "utf8" })
  .split("\n")
  .filter((f) => f && SOURCE.test(f) && !SKIP.test(f) && !SKIP_FILE.test(f) && existsSync(join(ROOT, f)));
const lines = (f) => {
  const text = readFileSync(join(ROOT, f), "utf8");
  return text ? text.split(/\r?\n/).length - (text.endsWith("\n") ? 1 : 0) : 0;
};
const counts = Object.fromEntries(files.map((f) => [f, lines(f)]));

const save = () => {
  const sorted = Object.fromEntries(Object.entries(entries).sort(([a], [b]) => a.localeCompare(b)));
  writeFileSync(BASELINE, JSON.stringify({ limit: LIMIT, files: sorted }, null, 2) + "\n");
};

if (mode === "init") {
  let added = 0;
  for (const [f, n] of Object.entries(counts)) {
    if (n > LIMIT && !entries[f]) {
      entries[f] = { max_lines: n, reason: "Over the limit when the check was introduced; split it when you next work on it." };
      added++;
    }
  }
  save();
  console.log(`Baselined ${added} file(s) over ${LIMIT} lines (${Object.keys(entries).length} in total).`);
  process.exit(0);
}

const errors = [];
const stale = [];
for (const [f, n] of Object.entries(counts).sort()) {
  const entry = entries[f];
  if (n <= LIMIT) {
    if (entry) stale.push(f);
  } else if (!entry) {
    errors.push(`${f}: ${n} lines, over the ${LIMIT}-line limit. Split it into smaller modules (see "Keep Files Small and Modular" in AGENTS.md).`);
  } else if (n > entry.max_lines) {
    errors.push(`${f}: ${n} lines, grew past its ceiling of ${entry.max_lines}. Move the new code into its own module instead.`);
  } else if (mode === "update" && n < entry.max_lines) {
    entry.max_lines = n;
  }
}
for (const f of Object.keys(entries)) if (!(f in counts)) stale.push(f);

if (mode === "update") {
  for (const f of stale) delete entries[f];
  save();
  console.log(`Baseline updated: ${Object.keys(entries).length} file(s) still over ${LIMIT} lines.`);
}
if (errors.length) {
  console.error(`File size check failed:\n  ${errors.join("\n  ")}`);
  process.exit(1);
}
if (mode === "check") {
  const note = stale.length ? ` ${stale.length} baselined file(s) are now small enough: run with --update to drop them.` : "";
  console.log(`File size check passed (${files.length} files, limit ${LIMIT}).${note}`);
}
