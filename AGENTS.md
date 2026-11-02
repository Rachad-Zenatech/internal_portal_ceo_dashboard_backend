# Agent Guidelines & Repository Rules

This document defines the execution guidelines for all AI agents working on this codebase.
Architectural standards are in `.agents/rules/` and loaded only when relevant.

---

## 0. AI Agent Efficiency & Execution Budget

These rules apply to every AI coding agent working in this repository.

### 0.1 Minimize Context Consumption

- Do not scan the entire repository for individual assignments.
- Use the existing Project Structure documentation to locate relevant files.
- Read only files necessary to complete the requested task.
- Do not repeatedly read files already inspected.
- Do not load large documentation files unless directly relevant.
- Prefer targeted searches over broad searches.
- Before opening a source file, search `.agents/code-map.md` for the file or function name (grep it; do not read the whole map) and read only the line range you need.
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

### 0.5 Implementation Scope

- Modify only files necessary for the assignment.
- Preserve existing architecture and coding conventions.
- Do not refactor unrelated services.
- Do not rename or reorganize existing modules without authorization.
- Do not introduce additional dependencies without justification.
- Ask before performing destructive database or infrastructure operations.

### 0.6 Verification

- Run targeted tests for modified functionality.
- Run py_compile or tsc/build for touched files.
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
