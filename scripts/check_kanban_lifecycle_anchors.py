#!/usr/bin/env python3
"""Verify source anchors in docs/kanban/LIFECYCLE.md."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DOCUMENT = Path("docs/kanban/LIFECYCLE.md")
ANCHOR_RE = re.compile(
    r"\[(?P<label>[^\]\n]+)\]"
    r"\((?P<target>[^)\n#]+\.py)\)"
)
SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
BANNER_RE = re.compile(r"# -{10,}\s*$")
HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*$")


@dataclass(frozen=True)
class Anchor:
    label: str
    target: str
    document_line: int
    section_index: bool


@dataclass(frozen=True)
class Resolution:
    anchor: Anchor
    source_path: Path
    ok: bool
    message: str


@dataclass(frozen=True)
class SymbolIndex:
    definitions: dict[str, int]
    error: str | None = None


def _section_index_span(text: str) -> tuple[int, int] | None:
    """Return character offsets occupied by the ``Section index`` section."""
    offsets: list[int] = []
    total = 0
    lines = text.splitlines(keepends=True)
    for line in lines:
        offsets.append(total)
        total += len(line)
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line.rstrip("\r\n"))
        if not match or "Section index" not in match.group("title"):
            continue
        level = len(match.group("level"))
        end = len(text)
        for later in range(index + 1, len(lines)):
            next_heading = HEADING_RE.match(lines[later].rstrip("\r\n"))
            if next_heading and len(next_heading.group("level")) <= level:
                end = offsets[later]
                break
        return offsets[index], end
    return None


def _symbol_label(label: str) -> str | None:
    if label.startswith("`") and label.endswith("`") and len(label) > 2:
        label = label[1:-1]
    return label if SYMBOL_RE.fullmatch(label) else None


def parse_anchors(document: Path) -> tuple[str, list[Anchor]]:
    text = document.read_text(encoding="utf-8")
    section_span = _section_index_span(text)
    anchors: list[Anchor] = []
    for match in ANCHOR_RE.finditer(text):
        label = match.group("label")
        anchor_offset = match.start()
        section_index = bool(
            section_span
            and section_span[0] <= anchor_offset < section_span[1]
        )
        # A normal Markdown link to a Python file is not automatically a
        # lifecycle anchor. Outside the Section index, anchors name bare
        # top-level symbols; links whose labels are paths remain ordinary prose
        # links. The anchor class itself is still determined only by position.
        if not section_index and _symbol_label(label) is None:
            continue
        anchors.append(
            Anchor(
                label=label,
                target=match.group("target"),
                document_line=text.count("\n", 0, anchor_offset) + 1,
                section_index=section_index,
            )
        )
    return text, anchors


def _is_banner(lines: list[str], index: int, title: str) -> bool:
    if not (0 <= index < len(lines)) or lines[index] != f"# {title}":
        return False
    before = index > 0 and BANNER_RE.fullmatch(lines[index - 1])
    after = index + 1 < len(lines) and BANNER_RE.fullmatch(lines[index + 1])
    return bool(before or after)


def _assigned_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [
            name
            for element in target.elts
            for name in _assigned_names(element)
        ]
    return []


def _build_symbol_index(source_path: Path, text: str) -> SymbolIndex:
    try:
        tree = ast.parse(text, filename=str(source_path))
    except SyntaxError as exc:
        detail = f"syntax error at line {exc.lineno}" if exc.lineno else "syntax error"
        return SymbolIndex({}, detail)

    definitions: dict[str, int] = {}
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [
                name
                for target in node.targets
                for name in _assigned_names(target)
            ]
        elif isinstance(node, ast.AnnAssign):
            names = _assigned_names(node.target)
        for name in names:
            definitions[name] = definitions.get(name, 0) + 1
    return SymbolIndex(definitions)


def _source_path(document: Path, target: str) -> Path:
    return (document.parent / target).resolve()


def resolve_anchor(
    document: Path,
    anchor: Anchor,
    source_cache: dict[Path, str],
    symbol_cache: dict[Path, SymbolIndex],
) -> Resolution:
    source_path = _source_path(document, anchor.target)
    kind = "banner" if anchor.section_index else "symbol"
    name = anchor.label
    if not anchor.section_index:
        symbol = _symbol_label(anchor.label)
        if symbol is None:
            return Resolution(
                anchor,
                source_path,
                False,
                f"symbol label {anchor.label!r} is not a bare top-level name "
                f"for {_display_path(source_path)}",
            )
        name = symbol
    if not source_path.is_file():
        return Resolution(
            anchor,
            source_path,
            False,
            f"target file for {kind} {name!r} does not exist: "
            f"{_display_path(source_path)}",
        )
    if source_path not in source_cache:
        source_cache[source_path] = source_path.read_text(encoding="utf-8")
    text = source_cache[source_path]
    if anchor.section_index:
        lines = text.splitlines()
        candidate_count = sum(
            1
            for index in range(len(lines))
            if _is_banner(lines, index, anchor.label)
        )
    else:
        if source_path not in symbol_cache:
            symbol_cache[source_path] = _build_symbol_index(source_path, text)
        index = symbol_cache[source_path]
        if index.error:
            return Resolution(
                anchor,
                source_path,
                False,
                f"cannot resolve symbol {name!r} in "
                f"{_display_path(source_path)}: {index.error}",
            )
        candidate_count = index.definitions.get(name, 0)

    if candidate_count == 0:
        return Resolution(
            anchor,
            source_path,
            False,
            f"{kind} {name!r} does not exist in {_display_path(source_path)}",
        )
    if candidate_count > 1:
        return Resolution(
            anchor,
            source_path,
            False,
            f"{kind} {name!r} is ambiguous in {_display_path(source_path)}: "
            f"{candidate_count} exact definitions",
        )
    return Resolution(anchor, source_path, True, "resolved")


def verify(document: Path) -> tuple[list[Resolution], list[Resolution]]:
    _, anchors = parse_anchors(document)
    source_cache: dict[Path, str] = {}
    symbol_cache: dict[Path, SymbolIndex] = {}
    results = [
        resolve_anchor(document, anchor, source_cache, symbol_cache)
        for anchor in anchors
    ]
    failures = [result for result in results if not result.ok]
    return results, failures


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _report_failure(document: Path, result: Resolution) -> None:
    anchor = result.anchor
    print(
        f"{_display_path(document)}:{anchor.document_line}: "
        f"{result.message}",
        file=sys.stderr,
    )


def check_document(document: Path) -> bool:
    if not document.is_file():
        print(f"document does not exist: {document}", file=sys.stderr)
        return False
    results, failures = verify(document)
    if failures:
        for result in failures:
            _report_failure(document, result)
        return False
    print(f"OK: {len(results)} anchors resolved in {_display_path(document)}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--document",
        type=Path,
        default=DEFAULT_DOCUMENT,
        help="lifecycle document to check (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    document = args.document.resolve()
    return 0 if check_document(document) else 1


if __name__ == "__main__":
    raise SystemExit(main())
