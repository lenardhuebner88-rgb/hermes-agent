#!/usr/bin/env python3
"""Verify source anchors in docs/kanban/LIFECYCLE.md."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DOCUMENT = Path("docs/kanban/LIFECYCLE.md")
WINDOW = 2
ANCHOR_RE = re.compile(
    r"\[(?P<label>[^\]\n]+)\]"
    r"\((?P<target>[^)\n#]+)#L(?P<line>\d+)\)"
)
SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
BANNER_RE = re.compile(r"# -{10,}\s*$")
HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*$")


@dataclass(frozen=True)
class Anchor:
    label: str
    target: str
    line: int
    document_line: int
    line_span: tuple[int, int]
    section_index: bool


@dataclass(frozen=True)
class Resolution:
    anchor: Anchor
    source_path: Path
    true_line: int | None
    ok: bool
    message: str


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


def parse_anchors(document: Path) -> tuple[str, list[Anchor]]:
    text = document.read_text(encoding="utf-8")
    section_span = _section_index_span(text)
    anchors: list[Anchor] = []
    for match in ANCHOR_RE.finditer(text):
        label = match.group("label")
        anchor_offset = match.start()
        anchors.append(
            Anchor(
                label=label,
                target=match.group("target"),
                line=int(match.group("line")),
                document_line=text.count("\n", 0, anchor_offset) + 1,
                line_span=match.span("line"),
                section_index=bool(
                    section_span
                    and section_span[0] <= anchor_offset < section_span[1]
                ),
            )
        )
    return text, anchors


def _symbol_label(label: str) -> str | None:
    if label.startswith("`") and label.endswith("`") and len(label) > 2:
        label = label[1:-1]
    return label if SYMBOL_RE.fullmatch(label) else None


def _definition_pattern(symbol: str) -> re.Pattern[str]:
    escaped = re.escape(symbol)
    return re.compile(
        rf"^(?:async\s+def|def)\s+{escaped}\s*\("
        rf"|^class\s+{escaped}(?:\s*[\(:])"
        rf"|^{escaped}\s*="
    )


def _banner_title(line: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith("# ") or BANNER_RE.fullmatch(stripped):
        return None
    return stripped[2:].strip()


def _is_banner(lines: list[str], index: int, title: str) -> bool:
    if not (0 <= index < len(lines)) or _banner_title(lines[index]) != title:
        return False
    before = index > 0 and BANNER_RE.fullmatch(lines[index - 1].strip())
    after = index + 1 < len(lines) and BANNER_RE.fullmatch(lines[index + 1].strip())
    return bool(before or after)


def _candidate_lines(lines: list[str], anchor: Anchor) -> tuple[list[int], str | None]:
    if anchor.section_index:
        return (
            [
                index + 1
                for index in range(len(lines))
                if _is_banner(lines, index, anchor.label)
            ],
            None,
        )
    symbol = _symbol_label(anchor.label)
    if symbol is None:
        return [], (
            "non-section anchor text must be one bare symbol, optionally "
            "wrapped in backticks"
        )
    pattern = _definition_pattern(symbol)
    return (
        [
            index + 1
            for index, source_line in enumerate(lines)
            if pattern.search(source_line)
        ],
        None,
    )


def _source_path(document: Path, target: str) -> Path:
    return (document.parent / target).resolve()


def resolve_anchor(
    document: Path,
    anchor: Anchor,
    source_cache: dict[Path, list[str]],
) -> Resolution:
    source_path = _source_path(document, anchor.target)
    kind = "section" if anchor.section_index else "symbol"
    if not source_path.is_file():
        return Resolution(
            anchor,
            source_path,
            None,
            False,
            f"target file does not exist: {source_path}",
        )
    lines = source_cache.setdefault(
        source_path, source_path.read_text(encoding="utf-8").splitlines()
    )
    candidates, label_error = _candidate_lines(lines, anchor)
    if label_error:
        return Resolution(anchor, source_path, None, False, label_error)
    if not candidates:
        return Resolution(
            anchor,
            source_path,
            None,
            False,
            f"{kind} {anchor.label!r} no longer exists; refusing to guess",
        )
    nearby = [
        candidate
        for candidate in candidates
        if abs(candidate - anchor.line) <= WINDOW
    ]
    if len(nearby) == 1:
        true_line = nearby[0]
        detail = (
            "exact"
            if true_line == anchor.line
            else f"within ±{WINDOW}; true line is L{true_line}"
        )
        return Resolution(anchor, source_path, true_line, True, detail)
    if len(candidates) == 1:
        true_line = candidates[0]
        return Resolution(
            anchor,
            source_path,
            true_line,
            False,
            f"drifted from L{anchor.line} to L{true_line}",
        )
    locations = ", ".join(f"L{line}" for line in candidates)
    return Resolution(
        anchor,
        source_path,
        None,
        False,
        f"{kind} {anchor.label!r} is ambiguous at {locations}; refusing to guess",
    )


def verify(document: Path) -> tuple[list[Resolution], list[Resolution]]:
    _, anchors = parse_anchors(document)
    source_cache: dict[Path, list[str]] = {}
    results = [
        resolve_anchor(document, anchor, source_cache) for anchor in anchors
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
        f"{anchor.label}: anchor L{anchor.line}: {result.message}",
        file=sys.stderr,
    )


def fix_document(document: Path) -> bool:
    text, anchors = parse_anchors(document)
    source_cache: dict[Path, list[str]] = {}
    resolutions = [
        resolve_anchor(document, anchor, source_cache) for anchor in anchors
    ]
    unresolved = [result for result in resolutions if result.true_line is None]
    if unresolved:
        for result in unresolved:
            _report_failure(document, result)
        return False

    replacements = [
        (result.anchor.line_span, str(result.true_line), result)
        for result in resolutions
        if result.true_line != result.anchor.line
    ]
    for (start, end), replacement, _result in reversed(replacements):
        text = text[:start] + replacement + text[end:]
    if replacements:
        document.write_text(text, encoding="utf-8")
        for _span, _replacement, result in replacements:
            print(
                f"fixed {result.anchor.label}: "
                f"L{result.anchor.line} -> L{result.true_line}"
            )

    results, failures = verify(document)
    if failures:
        for result in failures:
            _report_failure(document, result)
        return False
    print(f"OK: {len(results)} anchors resolved in {_display_path(document)}")
    return True


def check_document(document: Path) -> bool:
    if not document.is_file():
        print(f"document does not exist: {document}", file=sys.stderr)
        return False
    results, failures = verify(document)
    for result in results:
        if result.ok and result.true_line != result.anchor.line:
            anchor = result.anchor
            print(
                f"{_display_path(document)}:{anchor.document_line}: "
                f"{anchor.label}: anchor L{anchor.line}; {result.message}"
            )
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
    parser.add_argument(
        "--fix",
        action="store_true",
        help="rewrite drifted line numbers, then re-verify",
    )
    args = parser.parse_args(argv)
    document = args.document.resolve()
    return 0 if (fix_document(document) if args.fix else check_document(document)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
