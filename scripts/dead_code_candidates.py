#!/usr/bin/env python3
"""Report vulture findings that also have no repository-wide text reference.

This is deliberately a scout: it never edits or deletes source files. The checked-in
allowlist is the authority for which fork-owned paths may produce candidates.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


STATIC_TOOL = "vulture"
STATIC_TOOL_VERSION = "2.14"
MIN_CONFIDENCE = 60
_VULTURE_LINE = re.compile(
    r"^(?P<path>.+):(?P<line>\d+): unused (?P<kind>[^']+) "
    r"'(?P<name>[^']+)' \((?P<confidence>\d+)% confidence\)$"
)


@dataclass(frozen=True)
class StaticFinding:
    path: str
    line: int
    name: str
    kind: str
    confidence: int
    static_evidence: str


@dataclass(frozen=True)
class _RepositoryDocument:
    path: str
    text: str


@dataclass(frozen=True)
class _RepositoryIndex:
    documents: tuple[_RepositoryDocument, ...]
    symbol_matches: dict[str, tuple[dict[str, object], ...]]


def _normalise_repo_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"repository path must be relative and may not contain '..': {value}")
    normalised = path.as_posix().lstrip("./")
    if not normalised or normalised == ".":
        raise ValueError(f"repository path may not be empty: {value}")
    return normalised


def load_allowlist(path: Path) -> tuple[str, ...]:
    entries: list[str] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = raw_line.split("#", 1)[0].strip()
        if not value:
            continue
        is_directory = value.endswith("/")
        normalised = _normalise_repo_path(value)
        if is_directory:
            normalised += "/"
        if normalised in entries:
            raise ValueError(f"duplicate allowlist entry at {path}:{line_number}: {normalised}")
        entries.append(normalised)
    if not entries:
        raise ValueError(f"allowlist is empty: {path}")
    return tuple(entries)


def path_is_allowed(path: str, entries: Iterable[str]) -> bool:
    try:
        normalised = _normalise_repo_path(path)
    except ValueError:
        return False
    for entry in entries:
        if entry.endswith("/"):
            if normalised.startswith(entry):
                return True
        elif normalised == entry:
            return True
    return False


def _allowed_python_paths(repo_root: Path, entries: Sequence[str]) -> list[str]:
    paths: set[str] = set()
    for entry in entries:
        target = repo_root / entry.rstrip("/")
        if entry.endswith("/"):
            if not target.is_dir():
                raise FileNotFoundError(f"allowlisted directory does not exist: {entry}")
            paths.update(
                path.relative_to(repo_root).as_posix()
                for path in target.rglob("*.py")
                if path.is_file()
            )
        elif target.suffix == ".py":
            if not target.is_file():
                raise FileNotFoundError(f"allowlisted Python file does not exist: {entry}")
            paths.add(entry)
    return sorted(paths)


def parse_vulture_output(output: str, repo_root: Path) -> list[StaticFinding]:
    findings: list[StaticFinding] = []
    for line in output.splitlines():
        match = _VULTURE_LINE.match(line.strip())
        if match is None:
            raise ValueError(f"unrecognised {STATIC_TOOL} output: {line}")
        raw_path = Path(match.group("path"))
        if raw_path.is_absolute():
            try:
                raw_path = raw_path.relative_to(repo_root)
            except ValueError:
                pass
        findings.append(
            StaticFinding(
                path=raw_path.as_posix(),
                line=int(match.group("line")),
                name=match.group("name"),
                kind=match.group("kind").strip(),
                confidence=int(match.group("confidence")),
                static_evidence=line.strip(),
            )
        )
    return findings


def run_static_analysis(repo_root: Path, entries: Sequence[str]) -> list[StaticFinding]:
    installed_version = importlib.metadata.version(STATIC_TOOL)
    if installed_version != STATIC_TOOL_VERSION:
        raise RuntimeError(
            f"{STATIC_TOOL} version mismatch: expected {STATIC_TOOL_VERSION}, "
            f"found {installed_version}"
        )
    paths = _allowed_python_paths(repo_root, entries)
    if not paths:
        return []
    command = [
        sys.executable,
        "-m",
        STATIC_TOOL,
        *paths,
        "--min-confidence",
        str(MIN_CONFIDENCE),
    ]
    result = subprocess.run(
        command,
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode not in (0, 3):
        raise RuntimeError(
            f"{STATIC_TOOL} failed with exit code {result.returncode}: {result.stderr.strip()}"
        )
    if result.stderr.strip():
        raise RuntimeError(f"{STATIC_TOOL} wrote to stderr: {result.stderr.strip()}")
    return parse_vulture_output(result.stdout, repo_root)


def _repository_files(repo_root: Path) -> list[Path]:
    git_result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if git_result.returncode == 0:
        return [
            repo_root / item.decode("utf-8", errors="surrogateescape")
            for item in git_result.stdout.split(b"\0")
            if item
        ]
    return [
        path
        for path in repo_root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo_root).parts
    ]


def _module_alias(path: str) -> str | None:
    source_path = PurePosixPath(path)
    if source_path.suffix != ".py":
        return None
    parts = list(source_path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or None


def _needles_for(finding: StaticFinding) -> tuple[tuple[str, str], ...]:
    needles: list[tuple[str, str]] = [("symbol", finding.name)]
    if finding.path.endswith(".py"):
        needles.append(("source_path", finding.path))
        module = _module_alias(finding.path)
        if module:
            needles.append(("python_module", module))
    return tuple(dict.fromkeys(needles))


def _build_repository_index(
    repo_root: Path,
    *,
    ignored_paths: set[str],
    symbol_names: set[str],
) -> _RepositoryIndex:
    documents: list[_RepositoryDocument] = []
    symbol_matches: dict[str, list[dict[str, object]]] = defaultdict(list)
    token_pattern = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    for path in _repository_files(repo_root):
        try:
            relative = path.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        if relative in ignored_paths or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        documents.append(_RepositoryDocument(path=relative, text=text))
        for line_number, line in enumerate(text.splitlines(), 1):
            for match in token_pattern.finditer(line):
                name = match.group(0)
                if name in symbol_names:
                    symbol_matches[name].append(
                        {
                            "path": relative,
                            "line": line_number,
                            "needle": name,
                            "kind": "symbol",
                        }
                    )
    return _RepositoryIndex(
        documents=tuple(documents),
        symbol_matches={name: tuple(matches) for name, matches in symbol_matches.items()},
    )


def _repository_references(
    index: _RepositoryIndex,
    finding: StaticFinding,
) -> dict[str, object]:
    needles = _needles_for(finding)
    matches = [
        match
        for match in index.symbol_matches.get(finding.name, ())
        if not (match["path"] == finding.path and match["line"] == finding.line)
    ]
    for needle_kind, needle in needles:
        if needle_kind == "symbol":
            continue
        for document in index.documents:
            if needle not in document.text:
                continue
            for line_number, line in enumerate(document.text.splitlines(), 1):
                if needle in line:
                    matches.append(
                        {
                            "path": document.path,
                            "line": line_number,
                            "needle": needle,
                            "kind": needle_kind,
                        }
                    )
    matches.sort(
        key=lambda item: (
            str(item["path"]),
            str(item["line"]).zfill(12),
            str(item["needle"]),
        )
    )
    return {
        "contract": "tracked-and-untracked non-binary repository files; declaration excluded",
        "needles": [needle for _, needle in needles],
        "matches": matches,
    }


def _finding_payload(finding: StaticFinding) -> dict[str, object]:
    return asdict(finding)


def build_report(
    *,
    repo_root: Path,
    allowlist_path: Path,
    findings: Iterable[StaticFinding] | None = None,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    allowlist_path = allowlist_path.resolve()
    entries = load_allowlist(allowlist_path)
    static_findings = list(findings) if findings is not None else run_static_analysis(repo_root, entries)
    try:
        ignored_allowlist = allowlist_path.relative_to(repo_root).as_posix()
    except ValueError:
        ignored_allowlist = ""
    ignored_paths = {ignored_allowlist} if ignored_allowlist else set()
    allowed_findings = [
        finding for finding in static_findings if path_is_allowed(finding.path, entries)
    ]
    repository_index = _build_repository_index(
        repo_root,
        ignored_paths=ignored_paths,
        symbol_names={finding.name for finding in allowed_findings},
    )

    candidates: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    for finding in sorted(static_findings, key=lambda item: (item.path, item.line, item.name)):
        payload = _finding_payload(finding)
        if not path_is_allowed(finding.path, entries):
            rejected.append({**payload, "reason": "outside_allowlist"})
            continue
        search_evidence = _repository_references(repository_index, finding)
        payload["repo_search_evidence"] = search_evidence
        if search_evidence["matches"]:
            rejected.append({**payload, "reason": "repository_reference"})
        else:
            candidates.append(payload)

    return {
        "schema_version": 1,
        "mode": "report_only",
        "static_tool": {"name": STATIC_TOOL, "version": STATIC_TOOL_VERSION},
        "allowlist": entries,
        "search_contract": (
            "A candidate requires a vulture finding and zero repository-wide references "
            "to its symbol, source path, or Python module alias outside its declaration."
        ),
        "candidates": candidates,
        "rejected": rejected,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=Path("scripts/dead_code_revier_allowlist.txt"),
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    allowlist = args.allowlist
    if not allowlist.is_absolute():
        allowlist = repo_root / allowlist
    report = build_report(repo_root=repo_root, allowlist_path=allowlist)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
