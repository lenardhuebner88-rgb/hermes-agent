#!/usr/bin/env python3
"""Print the pytest test files affected by a git diff — the *targeted* test
scope the interactive path uses (worker build, verifier re-check, pre-deploy
smoke). The full suite runs only nightly (green-gate-heartbeat); see
``AGENTS.md`` → Testing and ``00-Canon/conventions-gates.md`` → Test-Scope.

Run the affected tests via the safe wrapper — it skips pytest when nothing is
affected, instead of letting an empty ``$(...)`` collapse into a bare
``run_tests.sh`` = the full suite::

    scripts/run-affected.sh

Mapping rule (changed source -> its test file): ``<pkg>/<name>.py`` maps to
``tests/<pkg>/test_<name>.py``; changed ``test_*.py`` files run themselves.
When the 1:1 test file does not exist (common for monolith source files whose
tests are named by feature, e.g. ``gateway/run.py`` -> no ``test_run.py`` but
``tests/gateway/test_shutdown_cache_cleanup.py`` etc.), fall back to the entire
``tests/<pkg>/`` directory so regressions are caught at the merge gate instead
of only in the nightly full suite.

This MIRRORS ``hermes_cli.kanban_worktrees._affected_pytest_modules`` on
purpose, reimplemented with pure stdlib so it also runs in a bare worktree that
has no venv. If you change the mapping here, change it there too.

Usage::

    scripts/affected_tests.py                # vs merge-base with main (committed + working tree + untracked)
    scripts/affected_tests.py HEAD~1         # everything changed since HEAD~1
    scripts/affected_tests.py main...HEAD    # explicit range / ref

Prints a single space-separated line (empty when no ``.py`` changed). Consume it
through ``scripts/run-affected.sh``, which skips pytest on empty output — a bare
``run_tests.sh $(scripts/affected-tests.sh)`` would instead run the FULL suite.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _git(repo_root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    return Path(out.stdout.strip() or ".")


def _changed_files(repo_root: Path, ref: str | None) -> list[str]:
    """Changed paths. With an explicit ref/range: everything that differs from
    it. Default: vs the merge-base with ``main`` plus the working tree and
    untracked files (so a worker's in-progress edits count)."""
    files: set[str] = set()
    if ref:
        files.update(l for l in _git(repo_root, "diff", "--name-only", ref).splitlines() if l)
        return sorted(files)
    base = _git(repo_root, "merge-base", "HEAD", "main").strip()
    base_ref = base or "HEAD"  # no main / orphan -> fall back to working-tree-vs-HEAD
    files.update(l for l in _git(repo_root, "diff", "--name-only", base_ref).splitlines() if l)
    files.update(
        l for l in _git(repo_root, "ls-files", "--others", "--exclude-standard").splitlines() if l
    )
    return sorted(files)


def affected_pytest_modules(repo_root: Path, changed_files: list[str]) -> list[str]:
    # MIRROR of hermes_cli.kanban_worktrees._affected_pytest_modules — keep in sync.
    modules: set[str] = set()
    for f in changed_files:
        if not f.endswith(".py"):
            continue
        name = Path(f).name
        if f.startswith("tests/stress/") and name.startswith("test_"):
            # Stress scripts use their own @scenario registry, not pytest funcs.
            continue
        if f.startswith("tests/") and name.startswith("test_"):
            if (repo_root / f).is_file():
                modules.add(f)
            continue
        rel_dir = str(Path(f).parent)
        candidate = Path("tests") / rel_dir / f"test_{name}"
        if (repo_root / candidate).is_file():
            modules.add(str(candidate))
        else:
            # Fallback: no 1:1 test file. Monolith source files like
            # gateway/run.py or hermes_cli/kanban_db.py have feature-named
            # tests (test_shutdown_cache_cleanup.py, test_kanban_core*.py),
            # not test_<module>.py.  Select the entire package test directory
            # so regressions are caught at the merge gate, not only nightly.
            pkg_test_dir = Path("tests") / rel_dir
            if pkg_test_dir != Path("tests") and (repo_root / pkg_test_dir).is_dir():
                modules.add(str(pkg_test_dir) + "/")
    return sorted(modules)


def main(argv: list[str]) -> int:
    ref = argv[1] if len(argv) > 1 else None
    repo_root = _repo_root()
    changed = _changed_files(repo_root, ref)
    print(" ".join(affected_pytest_modules(repo_root, changed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
