#!/usr/bin/env python3
"""Symbol-level divergence between a fork file and its upstream counterpart.

Answers the two questions that matter for merge capability:

  * which upstream symbols are missing or stale in the fork
    ("what did we never take?")
  * how many fork lines sit INSIDE upstream-owned symbols
    ("what will collide on the next sync?")

Usage:
    python3 scripts/refactor/upstream_divergence.py <path> [<upstream-ref>] [--json]

Defaults to ``origin/main``. Human summary by default; ``--json`` emits the
full per-symbol record.

Why AST and not indentation: ``create_task`` has a multi-line signature whose
closing ``) -> str:`` sits back at indent 0. An indentation scan stops there
and measures 36 lines instead of 491. Always ``ast.parse`` with
``lineno``/``end_lineno``.

Note on the ruler: once ``origin/main`` becomes an ancestor of ``main``, a
merge is trivially up to date and ``git merge-tree`` reports zero conflicts.
That is an artefact. This script does not depend on merge state — it compares
file contents directly — which is exactly why it stays valid after a sync.

See docs/refactor/UPSTREAM-STRATEGY.md.
"""
from __future__ import annotations

import ast
import difflib
import json
import subprocess
import sys
from collections import OrderedDict


def top_level_symbols(src: str) -> "OrderedDict[str, tuple[int, int, str]]":
    """name -> (start_line, end_line, verbatim text), in file order.

    Decorators are folded into the span. Class methods are additionally
    reported as ``Class.method`` so a big class does not hide the one method
    that actually diverged.
    """
    tree = ast.parse(src)
    lines = src.splitlines()
    out: "OrderedDict[str, tuple[int, int, str]]" = OrderedDict()

    def span(node):
        start = node.lineno
        for dec in getattr(node, "decorator_list", []):
            start = min(start, dec.lineno)
        return start, node.end_lineno

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            s, e = span(node)
            out[node.name] = (s, e, "\n".join(lines[s - 1:e]))
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        ss, se = span(sub)
                        out[f"{node.name}.{sub.name}"] = (
                            ss, se, "\n".join(lines[ss - 1:se])
                        )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    out[t.id] = (
                        node.lineno, node.end_lineno,
                        "\n".join(lines[node.lineno - 1:node.end_lineno]),
                    )
    return out


def diff_counts(up_text: str, fork_text: str) -> tuple[int, int]:
    """(lines only upstream has, lines only the fork has)."""
    a, b = up_text.splitlines(), fork_text.splitlines()
    only_up = only_fork = 0
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            only_up += i2 - i1
        if tag in ("replace", "insert"):
            only_fork += j2 - j1
    return only_up, only_fork


def analyse(path: str, up_ref: str) -> dict:
    up_src = subprocess.run(
        ["git", "show", f"{up_ref}:{path}"],
        capture_output=True, text=True, check=True,
    ).stdout
    with open(path, encoding="utf-8") as handle:
        fork_src = handle.read()

    up, fork = top_level_symbols(up_src), top_level_symbols(fork_src)
    report: dict = {
        "path": path,
        "upstream_ref": up_ref,
        "upstream_only": [],
        "diverged": [],
        "identical": [],
        "fork_only_count": sum(1 for n in fork if n not in up),
    }

    for name, (s, e, text) in up.items():
        is_method = "." in name
        if name not in fork:
            report["upstream_only"].append(
                {"name": name, "upstream_lines": e - s + 1, "method": is_method}
            )
            continue
        fs, fe, ftext = fork[name]
        if text == ftext:
            report["identical"].append(name)
            continue
        only_up, only_fork = diff_counts(text, ftext)
        report["diverged"].append({
            "name": name,
            "method": is_method,
            "upstream_lines": e - s + 1,
            "fork_lines": fe - fs + 1,
            "upstream_only_lines": only_up,
            "fork_only_lines": only_fork,
            "fork_start": fs,
        })
    return report


def print_summary(rep: dict) -> None:
    top = [x for x in rep["diverged"] if not x["method"]]
    dropped = sum(x["upstream_lines"] for x in rep["upstream_only"])
    stale = sum(x["upstream_only_lines"] for x in top)
    collide = sum(x["fork_only_lines"] for x in top)

    print(f"{rep['path']}  vs  {rep['upstream_ref']}\n")
    print(f"  upstream symbols missing from the fork : {len(rep['upstream_only']):5d}"
          f"  ({dropped} lines)")
    print(f"  symbols diverged (top-level)           : {len(top):5d}")
    print(f"  symbols byte-identical                 : {len(rep['identical']):5d}")
    print(f"  fork-only symbols                      : {rep['fork_only_count']:5d}")
    print()
    print(f"  upstream lines NOT applied  (backlog)  : {stale:5d}")
    print(f"  fork lines INSIDE upstream symbols     : {collide:5d}"
          "   <- the merge-cost metric")
    print()
    print("  worst offenders (fork lines inside an upstream symbol):")
    for x in sorted(top, key=lambda y: -y["fork_only_lines"])[:10]:
        print(f"    {x['fork_only_lines']:5d}  {x['name']}  (line {x['fork_start']})")


def main() -> int:
    argv = sys.argv[1:]
    as_json = "--json" in argv
    args = [a for a in argv if a != "--json"]
    if not args:
        print("usage: upstream_divergence.py <path> [<upstream-ref>] [--json]",
              file=sys.stderr)
        return 2
    rep = analyse(args[0], args[1] if len(args) > 1 else "origin/main")
    if as_json:
        print(json.dumps(rep, indent=1))
    else:
        print_summary(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
