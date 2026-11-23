#!/usr/bin/env python3
"""Create a local, safe, reviewable module-splitting plan for large source files."""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}


def _changed_files(base: str | None, files: list[str]) -> list[Path]:
    if files:
        candidates = [ROOT / item for item in files]
    elif base:
        output = subprocess.check_output(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD"],
            cwd=ROOT,
            text=True,
        )
        candidates = [ROOT / item for item in output.splitlines()]
    else:
        candidates = []
    return [path for path in candidates if path.suffix in SOURCE_SUFFIXES and path.is_file()]


def _python_symbols(path: Path) -> list[dict[str, str]]:
    if path.suffix != ".py":
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    symbols = []
    for item in tree.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(item, ast.ClassDef) else "function"
            symbols.append({"name": item.name, "kind": kind, "line": str(item.lineno)})
    return symbols


def _section_groups(source: str, symbols: list[dict[str, str]]) -> dict[str, list[str]]:
    """Group symbols by the nearest explicit source section, then name prefix."""
    headings: list[tuple[int, str]] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        match = re.match(r"\s*#\s*-{2,}\s*(.+?)\s*-{2,}\s*$", line)
        if match:
            label = re.sub(r"[^a-z0-9]+", "_", match.group(1).lower()).strip("_")
            headings.append((line_number, label or "core"))
    groups: dict[str, list[str]] = {}
    for symbol in symbols:
        section = next((name for line, name in reversed(headings) if line < int(symbol["line"])), "")
        prefix = symbol["name"].split("_", 1)[0].lstrip("_") or "core"
        groups.setdefault(section or prefix, []).append(symbol["name"])
    return groups


def _deterministic_plan(path: Path, source: str, symbols: list[dict[str, str]]) -> dict:
    stem = path.stem.replace("_service", "").replace("_router", "")
    groups = _section_groups(source, symbols)
    modules = [
        {
            "path": str((path.parent / stem / f"{group}.py").relative_to(ROOT)).replace("\\", "/"),
            "responsibility": f"{group.replace('_', ' ')} responsibilities from {path.name}",
            "symbols": names,
        }
        for group, names in sorted(groups.items())
        if names
    ]
    return {
        "source_file": str(path.relative_to(ROOT)).replace("\\", "/"),
        "summary": f"{path.name} has {len(source.splitlines())} lines and {len(symbols)} top-level Python symbols.",
        "modules": modules,
        "safe_sequence": [
            "Extract one cohesive group while keeping the existing public imports as re-exports.",
            "Run the focused tests and py_compile after each extraction.",
            "Only remove the debt-baseline entry after the source is below the configured limit.",
        ],
        "planner": "local source sections and Python symbols; no AI model or API call",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--changed-from")
    parser.add_argument("--file", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    plans = []
    for path in _changed_files(args.changed_from, args.file):
        source = path.read_text(encoding="utf-8")
        plans.append(_deterministic_plan(path, source, _python_symbols(path)))
    result = {"plans": plans}
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
