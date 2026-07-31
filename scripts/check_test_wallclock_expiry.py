#!/usr/bin/env python3
"""Reject tests whose persisted fixed timestamps expire against the real clock.

The check is deliberately structural rather than a ban on fixed datetimes.  A
finding needs all three parts of the failure mode:

* a test persists a fixed datetime under a timestamp-like state key;
* production code reads that key in a function that also uses the wall clock
  and performs a comparison; and
* the test does not inject an explicit ``now=`` value.

This keeps ordinary deterministic datetime fixtures valid while catching tests
that become stale merely because wall time advances.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Sequence


_FIXED_ISO_RE = re.compile(r"^20\d\d-\d\d-\d\d[T ]\d\d:\d\d")
_EXCLUDED_PARTS = {
    ".claude",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    ".worktrees",
    "node_modules",
    "scripts",
    "tests",
    "venv",
}


class Hazard(NamedTuple):
    path: Path
    line: int
    test_name: str
    state_key: str


def _call_name(node: ast.Call) -> str:
    parts: list[str] = []
    current: ast.expr = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _fixed_datetime_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node)
    if name.endswith("datetime.fromisoformat"):
        return bool(
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and _FIXED_ISO_RE.match(node.args[0].value)
        )
    if not (name == "datetime" or name.endswith(".datetime")):
        return False
    year: object | None = None
    if node.args and isinstance(node.args[0], ast.Constant):
        year = node.args[0].value
    for keyword in node.keywords:
        if keyword.arg == "year" and isinstance(keyword.value, ast.Constant):
            year = keyword.value.value
    return isinstance(year, int) and 2000 <= year <= 2099


def _fixed_origin(node: ast.AST, fixed_names: dict[str, int]) -> int | None:
    if isinstance(node, ast.Name):
        return fixed_names.get(node.id)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return getattr(node, "lineno", None) if _FIXED_ISO_RE.match(node.value) else None
    if _fixed_datetime_call(node):
        return getattr(node, "lineno", None)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr in {"isoformat", "timestamp"}:
            return _fixed_origin(node.func.value, fixed_names)
    if isinstance(node, ast.BinOp):
        return _fixed_origin(node.left, fixed_names) or _fixed_origin(
            node.right, fixed_names
        )
    return None


def _fixed_names(test_node: ast.AST) -> dict[str, int]:
    assignments: list[tuple[str, ast.AST]] = []
    for node in ast.walk(test_node):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                assignments.append((target.id, value))

    fixed: dict[str, int] = {}
    changed = True
    while changed:
        changed = False
        for name, value in assignments:
            if name in fixed:
                continue
            origin = _fixed_origin(value, fixed)
            if origin is not None:
                fixed[name] = origin
                changed = True
    return fixed


def _has_explicit_clock(test_node: ast.AST) -> bool:
    return any(
        keyword.arg == "now"
        for node in ast.walk(test_node)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    )


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    result: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            result[child] = parent
    return result


def _is_persisted(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = node
    while current in parents:
        current = parents[current]
        if not isinstance(current, ast.Call):
            continue
        name = _call_name(current)
        if name.endswith((".write_text", ".write_bytes")) or name in {
            "json.dump",
            "json.dumps",
        }:
            return True
    return False


def _wall_clock_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node)
    return name in {
        "datetime.now",
        "datetime.utcnow",
        "time.time",
    } or name.endswith((".datetime.now", ".datetime.utcnow"))


def _expr_sources(
    node: ast.AST,
    *,
    state_key: str,
    names: dict[str, frozenset[str]],
) -> frozenset[str]:
    sources: set[str] = set()
    if isinstance(node, ast.Name):
        sources.update(names.get(node.id, ()))
    if _wall_clock_call(node):
        sources.add("wall")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if (
            node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == state_key
        ):
            sources.add("state")
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == state_key
    ):
        sources.add("state")
    for child in ast.iter_child_nodes(node):
        sources.update(_expr_sources(child, state_key=state_key, names=names))
    return frozenset(sources)


def _function_expires_key(function: ast.AST, state_key: str) -> bool:
    assignments: list[tuple[str, ast.AST]] = []
    for node in ast.walk(function):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                assignments.append((target.id, node.value))

    names: dict[str, frozenset[str]] = {}
    changed = True
    while changed:
        changed = False
        for name, value in assignments:
            sources = _expr_sources(value, state_key=state_key, names=names)
            combined = names.get(name, frozenset()) | sources
            if combined != names.get(name, frozenset()):
                names[name] = combined
                changed = True

    return any(
        {"state", "wall"}.issubset(
            _expr_sources(node, state_key=state_key, names=names)
        )
        for node in ast.walk(function)
        if isinstance(node, ast.Compare)
    )


def _production_python_paths(root: Path) -> list[Path]:
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if tracked.returncode == 0:
        return [root / relative for relative in tracked.stdout.split("\0") if relative]
    return list(root.rglob("*.py"))


def _production_expiry_keys(root: Path, candidate_keys: set[str]) -> set[str]:
    if not candidate_keys:
        return set()
    matched: set[str] = set()
    for path in _production_python_paths(root):
        if any(part in _EXCLUDED_PARTS for part in path.relative_to(root).parts):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        possible = {key for key in candidate_keys if repr(key) in source or f'"{key}"' in source}
        if not possible or "now" not in source:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            matched.update(
                key for key in possible if _function_expires_key(function, key)
            )
    return matched


def find_wallclock_expiry_hazards(
    root: Path, test_paths: Sequence[Path]
) -> list[Hazard]:
    candidates: list[Hazard] = []
    for supplied_path in test_paths:
        path = supplied_path if supplied_path.is_absolute() else root / supplied_path
        if not path.is_file() or path.suffix != ".py" or not path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        parents = _parents(tree)
        tests = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ]
        for test in tests:
            if _has_explicit_clock(test):
                continue
            fixed = _fixed_names(test)
            if not fixed:
                continue
            for node in ast.walk(test):
                if not isinstance(node, ast.Dict) or not _is_persisted(node, parents):
                    continue
                for key_node, value_node in zip(node.keys, node.values):
                    if not (
                        isinstance(key_node, ast.Constant)
                        and isinstance(key_node.value, str)
                    ):
                        continue
                    origin = _fixed_origin(value_node, fixed)
                    if origin is None:
                        continue
                    candidates.append(
                        Hazard(path, origin, test.name, key_node.value)
                    )

    expiry_keys = _production_expiry_keys(
        root, {candidate.state_key for candidate in candidates}
    )
    return sorted(
        {
            candidate
            for candidate in candidates
            if candidate.state_key in expiry_keys
        },
        key=lambda hazard: (str(hazard.path), hazard.line, hazard.state_key),
    )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="selected Python test files (relative to --root or absolute)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root (default: current directory)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    root = args.root.resolve()
    hazards = find_wallclock_expiry_hazards(root, args.paths)
    for hazard in hazards:
        try:
            display_path = hazard.path.resolve().relative_to(root)
        except ValueError:
            display_path = hazard.path
        print(
            f"{display_path}:{hazard.line}: fixed timestamp for "
            f"{hazard.state_key!r} in {hazard.test_name} is persisted, but "
            "production expiry logic uses the wall clock; inject now= or "
            "derive the state timestamp from the current clock",
            file=sys.stderr,
        )
    return 1 if hazards else 0


if __name__ == "__main__":
    raise SystemExit(main())
