#!/usr/bin/env python3
"""Measure how much fork content an upstream merge dropped on the floor.

Answers exactly one question, reproducibly: after a sync, which lines that the
fork had ADDED against the old merge base are no longer in the tree, and which
symbol they belonged to?

Usage:
    python3 scripts/refactor/fork_loss_check.py \\
        --old-base <sha> --fork-tip <sha> --current <sha|HEAD> \\
        --upstream-tip <sha> [--json out.json] [--limit-paths N] [--repo DIR]

Exit code: 0 = no findings, 1 = findings, 2 = usage/ref error. So it can be
called as a gate in a sync flow.

Method (the definition that produces the number — do not widen it silently):

1. Risk surface = files changed by BOTH the fork and upstream against the old
   merge base. A file only the fork touched cannot lose a conflict, so it only
   gets an existence check (``FILE_GONE`` if the merge dropped it).
2. Stage 1, broad: every line the fork ADDED to a risk-surface file
   (``git diff <old_base> <fork_tip>``, ``+`` lines). A line is a candidate when
   its whitespace-normalised form appears nowhere in the current file. Brackets,
   ``pass``, imports and very short lines are noise and are dropped.
   Because the comparison is set-based, a MOVED line is not a candidate.
   A second guard catches REFLOWED lines: the current file collapsed to a single
   whitespace-normalised string must not contain the candidate. That is what
   keeps a reformatting merge from reporting a wall of phantom losses — the
   whole point of the tool is a number someone can check, not a big number.
3. Stage 2, narrow: each surviving candidate is attributed via ``ast`` to its
   enclosing symbol in the FORK TIP, and the verdict depends on that symbol in
   the current tree:
     FILE_GONE       file is gone entirely
     SYMBOL_GONE     symbol does not exist any more
     SYMBOL_CHANGED  symbol exists, but the fork lines are missing from it
     LINE_ONLY       line missing, symbol not attributable (non-Python, or a
                     file that does not parse)
   Non-Python files stay on stage 1 by design; no second parser is built.

The tool MEASURES ONLY. Whether a loss was intentional (upstream superseded the
fork's version) or collateral damage is a human call.

Everything reads through ``git show``/``git diff``; no checkouts, no worktrees,
so a run over a few hundred files stays in the seconds range.

See docs/refactor/UPSTREAM-STRATEGY.md.
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

KIND_FILE_GONE = "FILE_GONE"
KIND_SYMBOL_GONE = "SYMBOL_GONE"
KIND_SYMBOL_CHANGED = "SYMBOL_CHANGED"
KIND_LINE_ONLY = "LINE_ONLY"

# Report order: a whole file gone outranks a symbol gone outranks a symbol that
# merely lost lines; unattributable line losses come last.
SEVERITY = {
    KIND_FILE_GONE: 0,
    KIND_SYMBOL_GONE: 1,
    KIND_SYMBOL_CHANGED: 2,
    KIND_LINE_ONLY: 3,
}

# Below this many characters (whitespace-normalised) a line carries no evidence
# of intent — it is a closing bracket, a `):` or a bare keyword.
MIN_CANDIDATE_LEN = 12

# Bare control-flow lines: they exist in every second function and would match
# or mismatch by accident.
NOISE_LINES = frozenset({
    "pass", "return", "break", "continue", "raise", "else:", "try:",
    "finally:", "yield", "...", '"""', "'''", "#",
})

_PATH_BATCH = 200


class GitError(RuntimeError):
    """A git invocation failed — bad ref, bad repo, no history."""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args[:3])} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout.decode("utf-8", "replace")


def changed_paths(repo: Path, base: str, tip: str) -> dict[str, str]:
    """path -> status letter (A/M/D/R…) for everything that differs.

    The status matters: a file the FORK ITSELF deleted is absent from the merged
    tree by the fork's own decision, and reporting that as a loss is exactly the
    kind of phantom finding that makes the number unusable.
    """
    out = _git(repo, "diff", "--name-status", base, tip)
    status: dict[str, str] = {}
    for line in out.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        # renames carry old and new path; the new path is what the fork owns
        status[fields[-1]] = fields[0][0]
    return status


def show(repo: Path, ref: str, path: str) -> str | None:
    """File content at a ref, or None when the file does not exist there."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def added_lines(
    repo: Path, old_base: str, fork_tip: str, paths: list[str]
) -> dict[str, list[tuple[int, str]]]:
    """path -> [(line number in fork tip, text)] for every fork-added line."""
    result: dict[str, list[tuple[int, str]]] = {p: [] for p in paths}
    for start in range(0, len(paths), _PATH_BATCH):
        batch = paths[start:start + _PATH_BATCH]
        out = _git(
            repo, "diff", "-U0", "--no-ext-diff", "--no-color",
            old_base, fork_tip, "--", *batch,
        )
        _parse_diff(out, result)
    return result


def _parse_diff(diff: str, into: dict[str, list[tuple[int, str]]]) -> None:
    current: str | None = None
    in_header = False
    lineno = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current, in_header = None, True
            continue
        if in_header:
            if line.startswith("+++ "):
                target = line[4:].strip()
                current = None if target == "/dev/null" else target[2:]
            elif line.startswith("@@"):
                in_header = False
                lineno = _hunk_start(line)
            continue
        if line.startswith("@@"):
            lineno = _hunk_start(line)
        elif line.startswith("+"):
            if current is not None and current in into:
                into[current].append((lineno, line[1:]))
            lineno += 1


def _hunk_start(header: str) -> int:
    """``@@ -3,0 +4,2 @@`` -> 4 (first line number on the new side)."""
    try:
        plus = header.split("+", 1)[1]
        return int(plus.split(",", 1)[0].split(" ", 1)[0])
    except (IndexError, ValueError):
        return 0


def normalise(line: str) -> str:
    """Whitespace-insensitive form: indentation and spacing are not content."""
    return " ".join(line.split())


def is_noise(norm: str) -> bool:
    if len(norm) < MIN_CANDIDATE_LEN:
        return True
    if norm in NOISE_LINES:
        return True
    if norm.startswith("import ") or (
        norm.startswith("from ") and " import " in norm
    ):
        return True
    # bracket / punctuation only, e.g. `), key=lambda` never hits this but `}),`
    # does
    return all(ch in "()[]{},:;+-*/= " for ch in norm)


def symbol_spans(src: str) -> list[tuple[int, int, str]]:
    """(start, end, qualified name) for every def/class/module-level constant.

    Nested defs and methods are included with a dotted name so a 500-line class
    cannot hide the one method that lost its fork lines. Decorators are folded
    into the span. Raises SyntaxError for unparseable sources — callers degrade
    to LINE_ONLY rather than pretending every symbol vanished.

    Assignments count only outside function bodies: a LOCAL ``total = ...`` is
    not a symbol, and treating it as one would attribute every lost line to a
    one-line span of its own, splitting a single lost function into a dozen
    findings.
    """
    tree = ast.parse(src)
    spans: list[tuple[int, int, str]] = []

    def walk(body, prefix: str, in_function: bool) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = node.lineno
                for dec in node.decorator_list:
                    start = min(start, dec.lineno)
                name = f"{prefix}{node.name}"
                spans.append((start, node.end_lineno or start, name))
                walk(
                    node.body,
                    f"{name}.",
                    in_function or not isinstance(node, ast.ClassDef),
                )
            elif not in_function and isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        spans.append((
                            node.lineno,
                            node.end_lineno or node.lineno,
                            f"{prefix}{target.id}",
                        ))

    walk(tree.body, "", False)
    return spans


def enclosing_symbol(spans: list[tuple[int, int, str]], lineno: int) -> str | None:
    """Innermost symbol containing a line, or None for module level."""
    best: tuple[int, str] | None = None
    for start, end, name in spans:
        if start <= lineno <= end:
            size = end - start
            if best is None or size < best[0]:
                best = (size, name)
    return None if best is None else best[1]


def analyse_file(
    path: str,
    added: list[tuple[int, str]],
    fork_src: str | None,
    cur_src: str | None,
) -> tuple[list[dict], dict[str, int]]:
    """Findings for one risk-surface file plus its stage-1 statistics."""
    stats = {"candidates": 0, "present": 0, "moved_or_reflowed": 0, "missing": 0}
    if cur_src is None:
        lost = sum(1 for _, text in added if not is_noise(normalise(text)))
        stats["candidates"] = stats["missing"] = lost
        return ([{
            "path": path,
            "kind": KIND_FILE_GONE,
            "scope": "risk_surface",
            "symbol": None,
            "missing_lines": lost,
            "lines": [],
        }], stats)

    cur_lines = {normalise(line) for line in cur_src.splitlines()}
    collapsed = " ".join(cur_src.split())

    missing: list[tuple[int, str]] = []
    for lineno, text in added:
        norm = normalise(text)
        if is_noise(norm):
            continue
        stats["candidates"] += 1
        if norm in cur_lines:
            stats["present"] += 1
            continue
        if norm in collapsed:
            # same content, different line breaks: a reformatting merge, not a
            # loss
            stats["moved_or_reflowed"] += 1
            continue
        stats["missing"] += 1
        missing.append((lineno, text.strip()))

    if not missing:
        return ([], stats)
    return (_classify(path, missing, fork_src, cur_src), stats)


def _classify(
    path: str,
    missing: list[tuple[int, str]],
    fork_src: str | None,
    cur_src: str,
) -> list[dict]:
    fork_spans = cur_spans = None
    if path.endswith(".py") and fork_src is not None:
        try:
            fork_spans = symbol_spans(fork_src)
            cur_spans = symbol_spans(cur_src)
        except SyntaxError:
            fork_spans = cur_spans = None

    if fork_spans is None or cur_spans is None:
        return [{
            "path": path,
            "kind": KIND_LINE_ONLY,
            "scope": "risk_surface",
            "symbol": None,
            "missing_lines": len(missing),
            "lines": [{"lineno": n, "text": t} for n, t in missing],
        }]

    cur_names = {name for _, _, name in cur_spans}
    grouped: dict[str | None, list[tuple[int, str]]] = {}
    for lineno, text in missing:
        grouped.setdefault(enclosing_symbol(fork_spans, lineno), []).append(
            (lineno, text)
        )

    findings = []
    for symbol, lines in grouped.items():
        if symbol is None:
            kind = KIND_LINE_ONLY
        elif symbol in cur_names:
            kind = KIND_SYMBOL_CHANGED
        else:
            kind = KIND_SYMBOL_GONE
        findings.append({
            "path": path,
            "kind": kind,
            "scope": "risk_surface",
            "symbol": symbol,
            "missing_lines": len(lines),
            "lines": [{"lineno": n, "text": t} for n, t in lines],
        })
    return findings


def check(
    repo: Path,
    old_base: str,
    fork_tip: str,
    current: str,
    upstream_tip: str,
    limit_paths: int | None = None,
) -> dict:
    """The whole measurement. Returns the report dict that --json writes out."""
    fork_status = changed_paths(repo, old_base, fork_tip)
    fork_changed = set(fork_status)
    upstream_changed = set(changed_paths(repo, old_base, upstream_tip))
    fork_deleted = {p for p, s in fork_status.items() if s == "D"}
    risk = sorted((fork_changed & upstream_changed) - fork_deleted)
    fork_only = sorted((fork_changed - upstream_changed) - fork_deleted)
    scanned = risk[:limit_paths] if limit_paths is not None else risk

    findings: list[dict] = []
    totals = {"candidates": 0, "present": 0, "moved_or_reflowed": 0, "missing": 0}

    for path in fork_only:
        if show(repo, current, path) is None:
            findings.append({
                "path": path,
                "kind": KIND_FILE_GONE,
                "scope": "fork_only",
                "symbol": None,
                "missing_lines": 0,
                "lines": [],
            })

    added = added_lines(repo, old_base, fork_tip, scanned)
    for path in scanned:
        file_findings, stats = analyse_file(
            path,
            added.get(path, []),
            show(repo, fork_tip, path),
            show(repo, current, path),
        )
        findings.extend(file_findings)
        for key in totals:
            totals[key] += stats[key]

    findings.sort(
        key=lambda f: (SEVERITY[f["kind"]], -f["missing_lines"], f["path"])
    )
    counts = {kind: 0 for kind in SEVERITY}
    for finding in findings:
        counts[finding["kind"]] += 1

    return {
        "refs": {
            "old_base": old_base,
            "fork_tip": fork_tip,
            "current": current,
            "upstream_tip": upstream_tip,
        },
        "risk_surface": {
            "fork_changed": len(fork_changed),
            "upstream_changed": len(upstream_changed),
            "intersection": len(fork_changed & upstream_changed),
            "fork_deleted": len(fork_deleted),
            "scanned": len(scanned),
            "fork_only": len(fork_only),
        },
        "stage1": totals,
        "counts": counts,
        "files_with_findings": len({f["path"] for f in findings}),
        "findings": findings,
    }


def print_report(report: dict) -> None:
    refs, surface, stage1 = report["refs"], report["risk_surface"], report["stage1"]
    print("fork-loss check")
    print(f"  old base     : {refs['old_base']}")
    print(f"  fork tip     : {refs['fork_tip']}")
    print(f"  current      : {refs['current']}")
    print(f"  upstream tip : {refs['upstream_tip']}")
    print()
    print(f"  fork changed          : {surface['fork_changed']:5d} files")
    print(f"  upstream changed      : {surface['upstream_changed']:5d} files")
    print(f"  risk surface (both)   : {surface['intersection']:5d} files"
          f"   <- scanned: {surface['scanned']}")
    print(f"  deleted by the fork   : {surface['fork_deleted']:5d}"
          "   (excluded — the fork's own decision)")
    print(f"  fork-only files       : {surface['fork_only']:5d}"
          "   (existence check only)")
    print()
    print(f"  fork-added candidate lines : {stage1['candidates']:6d}")
    print(f"    still present            : {stage1['present']:6d}")
    print(f"    moved / reflowed         : {stage1['moved_or_reflowed']:6d}"
          "   (not a loss)")
    print(f"    missing                  : {stage1['missing']:6d}")
    print()
    counts = report["counts"]
    print(f"  findings: {len(report['findings'])} in "
          f"{report['files_with_findings']} files  "
          + "  ".join(f"{kind}={counts[kind]}" for kind in SEVERITY))
    if not report["findings"]:
        print("\n  no fork content lost.")
        return
    print()
    for finding in report["findings"]:
        where = finding["symbol"] or "-"
        if finding["kind"] == KIND_FILE_GONE:
            detail = "file absent"
            if finding["missing_lines"]:
                detail += f", {finding['missing_lines']} fork line(s)"
            elif finding["scope"] == "fork_only":
                detail += " (fork-only file)"
        else:
            detail = f"{finding['missing_lines']} line(s)"
        print(f"  {finding['kind']:<15} {finding['path']}  [{where}]  {detail}")
        for line in finding["lines"][:3]:
            print(f"      {line['lineno']:6d}  {line['text'][:100]}")
        if len(finding["lines"]) > 3:
            print(f"      ... {len(finding['lines']) - 3} more")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fork_loss_check.py",
        description="Measure fork content lost by an upstream merge.",
    )
    parser.add_argument("--old-base", required=True, help="merge base before the sync")
    parser.add_argument("--fork-tip", required=True, help="fork commit before the sync")
    parser.add_argument("--current", required=True, help="state to judge, e.g. HEAD")
    parser.add_argument("--upstream-tip", required=True,
                        help="upstream commit (risk surface only)")
    parser.add_argument("--json", dest="json_out", help="also write the report here")
    parser.add_argument("--limit-paths", type=int, default=None,
                        help="scan at most N risk-surface files")
    parser.add_argument("--repo", default=".", help="repository to inspect")
    args = parser.parse_args(argv)

    try:
        report = check(
            Path(args.repo), args.old_base, args.fork_tip, args.current,
            args.upstream_tip, args.limit_paths,
        )
    except GitError as exc:
        print(f"fork_loss_check: {exc}", file=sys.stderr)
        return 2

    print_report(report)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, indent=1), encoding="utf-8"
        )
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
