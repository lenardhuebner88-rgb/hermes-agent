#!/usr/bin/env python3
"""Instrument untested dead-code candidates before eventually removing them.

The scout report is not proof that a candidate is dead.  Call ``instrument`` only
for a candidate proven *not* to be covered by tests.  The inserted one-line
warning is a runtime tombstone.  A candidate that emits the warning receives a
permanent watermark and is never removed automatically.  A never-hit candidate
is removed only after a configurable number of distinct silent nights.

Known limit: a rarely executed path can remain silent for two weeks and still be
live.  The configured waiting period reduces risk; it cannot prove absence.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

STATE_SCHEMA_VERSION = 1
DEFAULT_SILENT_NIGHTS = 14
HIT_PREFIX = "DEAD_CODE_TOMBSTONE_HIT"
_MARKER_PREFIX = "dead-code-tombstone:"
_SUPPORTED_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
DefinitionNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
StatementNode = ast.Assign | ast.AnnAssign | ast.Import | ast.ImportFrom
CandidateNode = DefinitionNode | StatementNode


@dataclass(frozen=True)
class Candidate:
    path: str
    line: int
    name: str
    kind: str

    def validate(self) -> None:
        relative = Path(self.path)
        if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
            raise ValueError(
                f"candidate path must be repository-relative: {self.path!r}"
            )
        if self.line < 1:
            raise ValueError("candidate line must be positive")
        if not self.name or not self.kind:
            raise ValueError("candidate name and kind must be non-empty")


def candidate_id(candidate: Candidate) -> str:
    """Return a stable, log-safe identifier for one scanner finding."""

    candidate.validate()
    identity = f"{candidate.path}:{candidate.line}:{candidate.name}:{candidate.kind}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _empty_state() -> dict[str, Any]:
    return {"schema_version": STATE_SCHEMA_VERSION, "candidates": {}}


def _load_state(path: Path, *, missing_ok: bool) -> dict[str, Any]:
    if not path.exists():
        if missing_ok:
            return _empty_state()
        raise FileNotFoundError(f"tombstone state does not exist: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ValueError(f"unsupported tombstone state schema in {path}")
    candidates = raw.get("candidates")
    if not isinstance(candidates, dict):
        raise ValueError(f"invalid tombstone candidates in {path}")
    return raw


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _save_state(path: Path, state: dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _append_ledger(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")


def _source_path(repo_root: Path, relative_path: str) -> Path:
    root = repo_root.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"candidate escapes repository root: {relative_path!r}"
        ) from exc
    if not candidate.is_file():
        raise FileNotFoundError(f"candidate source does not exist: {candidate}")
    return candidate


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in node.elts:
            names.update(_target_names(element))
        return names
    return set()


def _statement_name(node: StatementNode) -> str | None:
    if isinstance(node, ast.Assign):
        names: set[str] = set()
        for target in node.targets:
            names.update(_target_names(target))
        return next(iter(names)) if len(names) == 1 else None
    if isinstance(node, ast.AnnAssign):
        names = _target_names(node.target)
        return next(iter(names)) if len(names) == 1 else None
    if len(node.names) != 1:
        return None
    imported = node.names[0]
    if isinstance(node, ast.Import):
        return imported.asname or imported.name.split(".", 1)[0]
    return imported.asname or imported.name


def _node_matches_candidate(node: ast.AST, candidate: Candidate) -> bool:
    if isinstance(node, _SUPPORTED_NODES):
        return node.name == candidate.name
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom)):
        return _statement_name(node) == candidate.name
    return False


def _candidate_node_at(tree: ast.AST, candidate: Candidate) -> CandidateNode:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(
            node,
            (*_SUPPORTED_NODES, ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom),
        )
        and node.lineno == candidate.line
        and _node_matches_candidate(node, candidate)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one safely removable candidate for {candidate.path}:"
            f"{candidate.line}:{candidate.name}, found {len(matches)}"
        )
    return matches[0]


def _insertion_index(node: DefinitionNode) -> int:
    body = node.body
    if not body:
        raise ValueError("cannot instrument a definition without a body")
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return first.end_lineno or first.lineno
    return first.lineno - 1


def _body_indent(lines: list[str], node: DefinitionNode) -> str:
    first_body_line = node.body[0].lineno - 1
    prefix = lines[first_body_line][
        : len(lines[first_body_line]) - len(lines[first_body_line].lstrip())
    ]
    if prefix:
        return prefix
    definition_line = lines[node.lineno - 1]
    definition_indent = definition_line[
        : len(definition_line) - len(definition_line.lstrip())
    ]
    return definition_indent + "    "


def _line_indent(lines: list[str], line_number: int) -> str:
    line = lines[line_number - 1]
    return line[: len(line) - len(line.lstrip())]


def _current_candidate_line(state: dict[str, Any], candidate: Candidate) -> int:
    prior_markers = sum(
        1
        for item in state["candidates"].values()
        if item.get("status") in {"instrumented", "protected"}
        and item.get("candidate", {}).get("path") == candidate.path
        and int(item["candidate"]["line"]) < candidate.line
    )
    return candidate.line + prior_markers


def _marker_line(identifier: str, indent: str) -> str:
    message = f"{HIT_PREFIX} {identifier}"
    return (
        f'{indent}__import__("logging").getLogger("hermes.dead_code_tombstone")'
        f'.warning("{message}")  # {_MARKER_PREFIX}{identifier}\n'
    )


def instrument_candidate(
    *,
    repo_root: Path,
    state_path: Path,
    ledger_path: Path,
    candidate: Candidate,
    night: date,
) -> str:
    """Insert one runtime warning and persist a never-hit tombstone state."""

    identifier = candidate_id(candidate)
    state = _load_state(state_path, missing_ok=True)
    existing = state["candidates"].get(identifier)
    if existing is not None:
        if existing.get("candidate") != asdict(candidate):
            raise ValueError(f"candidate id collision for {identifier}")
        if existing.get("status") in {"instrumented", "protected"}:
            return identifier
        raise ValueError(
            f"candidate {identifier} already has status {existing.get('status')!r}"
        )

    source_path = _source_path(repo_root, candidate.path)
    original = source_path.read_text(encoding="utf-8")
    tree = ast.parse(original, filename=str(source_path))
    located_candidate = replace(
        candidate,
        line=_current_candidate_line(state, candidate),
    )
    node = _candidate_node_at(tree, located_candidate)
    lines = original.splitlines(keepends=True)
    if isinstance(node, _SUPPORTED_NODES):
        insertion = _insertion_index(node)
        marker = _marker_line(identifier, _body_indent(lines, node))
        marker_position = "inside"
    else:
        insertion = node.lineno - 1
        marker = _marker_line(identifier, _line_indent(lines, node.lineno))
        marker_position = "before"
    if f"{_MARKER_PREFIX}{identifier}" in original:
        raise ValueError(
            f"source already contains marker for {identifier} without state"
        )
    lines.insert(insertion, marker)
    instrumented = "".join(lines)
    ast.parse(instrumented, filename=str(source_path))

    item = {
        "candidate": asdict(candidate),
        "status": "instrumented",
        "instrumented_night": night.isoformat(),
        "hit_watermark_night": None,
        "silent_nights": 0,
        "last_counted_night": night.isoformat(),
        "marker": f"{HIT_PREFIX} {identifier}",
        "marker_position": marker_position,
    }
    state["candidates"][identifier] = item
    _atomic_write(source_path, instrumented)
    try:
        _save_state(state_path, state)
    except BaseException:
        _atomic_write(source_path, original)
        raise
    _append_ledger(
        ledger_path,
        f"{night.isoformat()} INSTRUMENTED candidate={identifier} "
        f"path={candidate.path} line={candidate.line} name={candidate.name}",
    )
    return identifier


def _definition_containing_marker(
    tree: ast.AST,
    *,
    marker_line: int,
    name: str,
) -> DefinitionNode:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, _SUPPORTED_NODES)
        and node.name == name
        and node.lineno <= marker_line <= (node.end_lineno or node.lineno)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one definition containing tombstone marker, found {len(matches)}"
        )
    return matches[0]


def _statement_after_marker(
    tree: ast.AST,
    *,
    marker_line: int,
    candidate: Candidate,
) -> StatementNode:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom))
        and node.lineno == marker_line + 1
        and _node_matches_candidate(node, candidate)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one statement after tombstone marker, found {len(matches)}"
        )
    return matches[0]


def _parent_body(tree: ast.AST, target: CandidateNode) -> ast.AST | None:
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and target in body:
            return node
    return None


def _delete_instrumented_candidate(
    repo_root: Path,
    identifier: str,
    item: dict[str, Any],
) -> tuple[Path, str, str]:
    candidate = Candidate(**item["candidate"])
    source_path = _source_path(repo_root, candidate.path)
    original = source_path.read_text(encoding="utf-8")
    marker_text = f"{_MARKER_PREFIX}{identifier}"
    marker_lines = [
        index
        for index, line in enumerate(original.splitlines(), start=1)
        if marker_text in line
    ]
    if len(marker_lines) != 1:
        raise ValueError(
            f"expected one source marker for {identifier}, found {len(marker_lines)}"
        )

    tree = ast.parse(original, filename=str(source_path))
    if item.get("marker_position") == "inside":
        node: CandidateNode = _definition_containing_marker(
            tree,
            marker_line=marker_lines[0],
            name=candidate.name,
        )
        marker_start = None
    elif item.get("marker_position") == "before":
        node = _statement_after_marker(
            tree,
            marker_line=marker_lines[0],
            candidate=candidate,
        )
        marker_start = marker_lines[0]
    else:
        raise ValueError(f"invalid marker position for {identifier}")
    start = min(
        [node.lineno]
        + [decorator.lineno for decorator in getattr(node, "decorator_list", [])]
    )
    if marker_start is not None:
        start = marker_start
    end = node.end_lineno
    if end is None:
        raise ValueError(f"definition end is unknown for {identifier}")
    lines = original.splitlines(keepends=True)
    parent = _parent_body(tree, node)
    replacement: list[str] = []
    parent_body = getattr(parent, "body", None)
    if (
        parent is not None
        and not isinstance(parent, ast.Module)
        and parent_body == [node]
    ):
        original_line = lines[start - 1]
        indent = original_line[: len(original_line) - len(original_line.lstrip())]
        replacement = [f"{indent}pass\n"]
    updated = "".join(lines[: start - 1] + replacement + lines[end:])
    ast.parse(updated, filename=str(source_path))
    _atomic_write(source_path, updated)
    return source_path, original, updated


def observe_night(
    *,
    repo_root: Path,
    state_path: Path,
    ledger_path: Path,
    night: date,
    hit_ids: set[str],
    silent_nights_threshold: int,
) -> list[str]:
    """Apply one distinct night's observations and return performed actions.

    Once a hit is observed, ``status=protected`` and ``hit_watermark_night`` are
    permanent.  Protected candidates keep ``silent_nights=0`` on every later run,
    so waiting another full threshold can never make them deletion-eligible.
    """

    if silent_nights_threshold < 1:
        raise ValueError("silent_nights_threshold must be at least 1")
    state = _load_state(state_path, missing_ok=False)
    actions: list[str] = []
    current_night = night.isoformat()

    for identifier, item in sorted(state["candidates"].items()):
        status = item.get("status")
        if status == "deleted":
            continue
        if status not in {"instrumented", "protected"}:
            raise ValueError(
                f"unsupported candidate status for {identifier}: {status!r}"
            )

        if identifier in hit_ids:
            item["status"] = "protected"
            item["hit_watermark_night"] = current_night
            item["silent_nights"] = 0
            item["last_counted_night"] = current_night
            _save_state(state_path, state)
            _append_ledger(
                ledger_path,
                f"{current_night} HIT candidate={identifier} watermark={current_night} "
                "silent_nights=0 permanently_protected=true",
            )
            actions.append(f"protected:{identifier}")
            continue

        if status == "protected":
            item["silent_nights"] = 0
            _save_state(state_path, state)
            continue

        last_counted = item.get("last_counted_night")
        if not isinstance(last_counted, str):
            raise ValueError(f"missing last_counted_night for {identifier}")
        if current_night < last_counted:
            raise ValueError(
                f"night {current_night} precedes last counted night {last_counted} for {identifier}"
            )
        if current_night == last_counted:
            continue

        item["silent_nights"] = int(item.get("silent_nights", 0)) + 1
        item["last_counted_night"] = current_night
        if item["silent_nights"] < silent_nights_threshold:
            _save_state(state_path, state)
            actions.append(f"silent:{identifier}:{item['silent_nights']}")
            continue

        source_path, original, _updated = _delete_instrumented_candidate(
            repo_root,
            identifier,
            item,
        )
        item["status"] = "deleted"
        item["deleted_night"] = current_night
        try:
            _save_state(state_path, state)
        except BaseException:
            _atomic_write(source_path, original)
            raise
        silent_nights = item["silent_nights"]
        _append_ledger(
            ledger_path,
            f"{current_night} DELETED candidate={identifier} "
            f"path={item['candidate']['path']} silent_nights={silent_nights}",
        )
        actions.append(f"deleted:{identifier}:{silent_nights}")

    return actions


def _parse_night(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("night must use YYYY-MM-DD") from exc


def _hit_ids_from_file(path: Path | None) -> set[str]:
    if path is None:
        return set()
    pattern = re.compile(rf"\b{re.escape(HIT_PREFIX)} ([0-9a-f]{{16}})\b")
    return set(pattern.findall(path.read_text(encoding="utf-8", errors="replace")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    instrument = subparsers.add_parser(
        "instrument",
        help="instrument one candidate already proven uncovered by tests",
    )
    instrument.add_argument("--repo-root", type=Path, default=Path.cwd())
    instrument.add_argument("--state", type=Path, required=True)
    instrument.add_argument("--ledger", type=Path, required=True)
    instrument.add_argument("--path", required=True)
    instrument.add_argument("--line", type=int, required=True)
    instrument.add_argument("--name", required=True)
    instrument.add_argument("--kind", required=True)
    instrument.add_argument("--night", type=_parse_night, required=True)
    instrument.add_argument(
        "--uncovered",
        action="store_true",
        required=True,
        help="required attestation: tests do not cover this candidate",
    )

    observe = subparsers.add_parser("observe", help="apply one night's runtime hits")
    observe.add_argument("--repo-root", type=Path, default=Path.cwd())
    observe.add_argument("--state", type=Path, required=True)
    observe.add_argument("--ledger", type=Path, required=True)
    observe.add_argument("--night", type=_parse_night, required=True)
    observe.add_argument(
        "--silent-nights",
        type=int,
        default=DEFAULT_SILENT_NIGHTS,
    )
    observe.add_argument("--hits-file", type=Path)
    observe.add_argument("--hit", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "instrument":
        identifier = instrument_candidate(
            repo_root=args.repo_root,
            state_path=args.state,
            ledger_path=args.ledger,
            candidate=Candidate(
                path=args.path,
                line=args.line,
                name=args.name,
                kind=args.kind,
            ),
            night=args.night,
        )
        print(f"INSTRUMENTED {identifier}")
        return 0

    hits = _hit_ids_from_file(args.hits_file)
    hits.update(args.hit)
    actions = observe_night(
        repo_root=args.repo_root,
        state_path=args.state,
        ledger_path=args.ledger,
        night=args.night,
        hit_ids=hits,
        silent_nights_threshold=args.silent_nights,
    )
    for action in actions:
        print(action)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"dead_code_tombstone: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
