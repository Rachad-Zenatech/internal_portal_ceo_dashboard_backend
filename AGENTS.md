# Agent Guidelines & Repository Rules

This document defines the execution guidelines for all AI agents working on **Internal Portal Ceo Dashboard Backend** (FastAPI/PostgreSQL backend (pytest)).
Architectural standards are in `.agents/rules/` and loaded only when relevant:

- `.agents/rules/project-guidelines.md` - Apply when modifying backend code: stack, routers/services/models layout, DB conventions, real-time/SSE rules and checks for this project.

---

## 0. MANDATORY ORIENTATION GATE (READ BEFORE ANY FILE ACCESS)

Every AI agent working in this repository MUST follow this sequence before touching or reading code:
1. **Never scan, glob, or read arbitrary source files or directories.**
2. **First step for orientation:** Read `.agents/project-index.md` to identify the relevant module and directory layout.
3. **Second step for symbols & functions:** Grep `.agents/code-map/` (one small file per source folder, listed in `.agents/code-map.md`) for the exact function, class, or route name. Never read a map file whole.
4. **Third step for reading code:** Read ONLY the line range the map gives (each symbol shows its start line, `L<n>`; read to the next symbol's line). Never read entire files >400 lines.
5. **After adding, moving, or renaming functions:** Regenerate the map immediately via `node .agents/gen_code_map.mjs`.

Violating this order wastes context budget and violates repository execution rules.

---

## 0.1 AI Agent Efficiency & Execution Budget

These rules apply to every AI coding agent working in this repository.

### 0.1.1 Minimize Context Consumption

- Do not scan the entire repository for individual assignments.
- Use the existing Project Structure documentation to locate relevant files.
- Read only files necessary to complete the requested task.
- Do not repeatedly read files already inspected.
- Do not load large documentation files unless directly relevant.
- Prefer targeted searches over broad searches.
- Before opening a source file, grep `.agents/code-map/` for the file or function name (do not read map files whole) and read only the line range it gives (`L<n>`).
- For orientation (directory layout, what each area does), read `.agents/project-index.md` first; it is a short overview. Use `.agents/code-map/` only for symbol-level lookups.
- Do not read files over 400 lines in full, and do not re-read a line range already read in the same task.
- After adding, moving or renaming functions, regenerate the map: `node .agents/gen_code_map.mjs`.
- Do not summarize or re-analyze the entire codebase unnecessarily.

### 0.2 Planning Restrictions

- Use a short implementation plan.
- Identify the specific files requiring changes.
- Do not perform extensive architectural analysis for routine changes.
- Do not propose unrelated improvements.
- Do not create multiple alternative implementation plans unless requested.

### 0.3 Agent and Subagent Restrictions

- Do not spawn subagents by default.
- Do not use parallel agents for routine assignments.
- Only use subagents when explicitly authorized.
- Do not delegate simple code changes or file inspections.

### 0.4 Tool Execution Restrictions

- Avoid repeated tool calls that retrieve identical information.
- Do not repeatedly execute the same failed command without changing the underlying cause.
- Do not run the entire test suite for small changes.
- Do not repeatedly inspect unchanged files.
- Do not perform unnecessary browser research.
- Do not automatically expand the scope of an assignment.

### 0.4.1 Search and Read With Built-in Tools, Not the Shell

- Search, read and find files with your agent's own built-in tools when it has them (e.g. Cursor: codebase search and read file; Copilot: search and read tools), not the shell.
- Read only the line range the code map gives, never whole files over 400 lines.
- If your agent only has a shell, search with `rg` (or `git grep`) limited to the paths you need, and print only the lines you need (`sed -n 'START,ENDp'`), never whole files or the whole map.
- Use the shell for work that needs it: tests, builds, type checks, running servers, installs and git.
- Why: built-in tools run without an approval prompt and return trimmed results; shell searches can wait for approval and flood the context.

### 0.5 Implementation Scope

- Modify only files necessary for the assignment.
- Preserve existing architecture and coding conventions.
- Do not refactor unrelated services.
- Do not rename or reorganize existing modules without authorization.
- Do not introduce additional dependencies without justification.
- Ask before performing destructive database or infrastructure operations.

### 0.5.1 Keep Files Small and Modular

- No source file over **500 lines**; aim for under 300. Keep functions and components under about 80 lines.
- Adding a feature means adding a module, not appending to a big file: put it in its own file (or a folder named after the feature) next to the code it belongs with, and import it.
- Before an edit would push a file past 500 lines, split it first: move each cohesive part (a group of related functions, a sub-component, a hook, the types) into its own module, and keep the original file's exports working (re-export) so imports elsewhere don't break.
- One job per file. Backend: routers stay thin, logic lives in services, queries in the service or repository, request/response shapes in models. Frontend: pages compose components, data fetching and state live in hooks, API calls in services, shared types in types.
- Follow the layout in `.agents/rules/project-structure.md` (when present) and the folders that already exist; never create a parallel structure.
- Files already over the limit are listed in `.agents/file_size_baseline.json` and may only shrink. Never raise a ceiling or add a file to the baseline: split the file instead. After shrinking one, run `node .agents/check_file_sizes.mjs --update`.
- Before finishing, run `node .agents/check_file_sizes.mjs`. The pre-commit hook and CI run it too, and fail when a file is too big.

### 0.6 Verification

- Run `py_compile` for touched backend files.
- Run targeted `pytest` tests for modified functionality.
- Run git diff --check.
- Do not repeat successful verification commands.
- Report pre-existing failures separately from new failures.

### 0.7 Completion Rules

Stop when:
1. The requested implementation is complete.
2. Relevant verification has been performed.
3. Outstanding failures have been documented.

Do not continue exploring, optimizing, refactoring or improving the application without authorization.

### 0.8 Final Response

Provide a concise report containing:
- Files modified.
- Changes implemented.
- Tests executed.
- Test results.
- Outstanding issues.

Do not produce lengthy explanations unless requested.
