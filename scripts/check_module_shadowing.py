#!/usr/bin/env python3
"""Fail when a watched module defines the same top-level name twice.

A second ``def``/``class`` at module level silently replaces the first one, so
the earlier body becomes dead code that still reads as live. That is how
``_worktree_deps_tree`` was defined twice in ``hermes_cli/kanban_worktrees.py``
and broke a guard test without any import error.

This is a pure AST walk: it parses the watched files, imports no repository
module, and writes nothing. Deliberate duplicates must be listed in the
allowlist file so they stay a visible, reviewed decision.

Allowlist format -- a JSON list of objects, each with the keys ``path``
(repo-relative, e.g. ``hermes_cli/kanban_db.py``), ``name`` (the shadowed
top-level symbol) and ``reason`` (why the duplicate is intended)::

    [
      {"path": "hermes_cli/kanban_db.py", "name": "helper", "reason": "..."}
    ]
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = DEFAULT_REPO_ROOT / "scripts" / "module_shadowing_allowlist.json"
WATCHED_MODULES = (
    "hermes_cli/kanban_db.py",
    "hermes_cli/kanban_worktrees.py",
)
ALLOWLIST_KEYS = ("path", "name", "reason")

EXIT_OK = 0
EXIT_FOUND = 1
EXIT_USAGE = 2


@dataclass(frozen=True)
class Duplicate:
    """One top-level name defined more than once in a single module."""

    path: str
    name: str
    lines: tuple[int, ...]


class AllowlistError(Exception):
    """The allowlist file is missing or does not follow the documented shape."""


def load_allowlist(allowlist: Path) -> set[tuple[str, str]]:
    """Return the ``(path, name)`` pairs whose duplication is a reviewed decision."""
    try:
        raw = allowlist.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AllowlistError(f"allowlist does not exist: {allowlist}") from exc
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AllowlistError(f"allowlist is not valid JSON: {allowlist}: {exc}") from exc
    if not isinstance(entries, list):
        raise AllowlistError(
            f"allowlist must be a JSON list of objects: {allowlist}"
        )
    allowed: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise AllowlistError(
                f"allowlist entry {index} must be an object with keys "
                f"{', '.join(ALLOWLIST_KEYS)}: {allowlist}"
            )
        missing = [
            key
            for key in ALLOWLIST_KEYS
            if not isinstance(entry.get(key), str) or not entry[key].strip()
        ]
        if missing:
            raise AllowlistError(
                f"allowlist entry {index} is missing non-empty "
                f"{', '.join(missing)}: {allowlist}"
            )
        allowed.add((entry["path"], entry["name"]))
    return allowed


def find_duplicates(source_path: Path, display_path: str) -> list[Duplicate]:
    """Return every top-level function/class name defined more than once."""
    text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(source_path))
    definitions: dict[str, list[int]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions.setdefault(node.name, []).append(node.lineno)
    return [
        Duplicate(display_path, name, tuple(lines))
        for name, lines in definitions.items()
        if len(lines) > 1
    ]


def check_repo(repo_root: Path, allowed: set[tuple[str, str]]) -> list[Duplicate]:
    """Check every watched module and drop the allowlisted duplicates."""
    duplicates: list[Duplicate] = []
    for relative in WATCHED_MODULES:
        source_path = repo_root / relative
        if not source_path.is_file():
            raise AllowlistError(f"watched module does not exist: {source_path}")
        for duplicate in find_duplicates(source_path, relative):
            if (duplicate.path, duplicate.name) in allowed:
                continue
            duplicates.append(duplicate)
    return sorted(duplicates, key=lambda item: (item.path, item.lines[0], item.name))


def report(duplicates: list[Duplicate]) -> None:
    """Print one stderr line per definition site, so every line is visible."""
    for duplicate in duplicates:
        total = len(duplicate.lines)
        for position, line in enumerate(duplicate.lines, start=1):
            print(
                f"{duplicate.path}:{line}: duplicate top-level definition "
                f"{duplicate.name!r} (definition {position} of {total}; the last "
                f"one wins, the earlier bodies are dead)",
                file=sys.stderr,
            )
    print(
        f"FAIL: {len(duplicates)} shadowed top-level definition(s); "
        "fix the duplicate or record the decision in the allowlist",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=DEFAULT_REPO_ROOT,
        help="repository root holding the watched modules (default: %(default)s)",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=DEFAULT_ALLOWLIST,
        help="JSON allowlist of reviewed duplicates (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    try:
        allowed = load_allowlist(args.allowlist.resolve())
        duplicates = check_repo(args.repo_root.resolve(), allowed)
    except AllowlistError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except SyntaxError as exc:
        print(
            f"{exc.filename}:{exc.lineno}: cannot parse watched module: {exc.msg}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if duplicates:
        report(duplicates)
        return EXIT_FOUND
    print(
        f"OK: {len(WATCHED_MODULES)} modules checked; "
        "no duplicate top-level definitions"
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
