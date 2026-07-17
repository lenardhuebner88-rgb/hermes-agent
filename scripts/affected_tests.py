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

import re
import subprocess
import sys
from pathlib import Path

# Cap for the package-directory fallback: if the package test directory
# contains more than this many test_*.py files, the fallback downgrades to
# no selection (nightly full suite remains the backstop). This prevents a
# monolith edit from turning the targeted gate into a de-facto full-suite
# run, satisfying the AC-2 counter-metric: no gate-tempo-for-coverage trade.
#
# Calibrated against real package directories (2026-07-16):
#   tests/hermes_cli/  592 files / 11276 tests
#   tests/gateway/     460 files / 8647 tests
#   tests/tools/       318 files
# 800 covers all current directories with headroom; anything larger would
# push walltime past the targeted-gate budget.
_FALLBACK_MAX_TEST_FILES = 800


def _imports_changed_module(test_path: Path, module_import: str) -> bool:
    """Return True when ``test_path`` imports the changed source module."""
    try:
        content = test_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    package, _, module_name = module_import.rpartition(".")
    direct_import = rf"^\s*import\s+.*\b{re.escape(module_import)}\b"
    if re.search(direct_import, content, re.MULTILINE):
        return True
    submodule_from_import = rf"^\s*from\s+{re.escape(module_import)}\s+import\b"
    if re.search(submodule_from_import, content, re.MULTILINE):
        return True
    if package:
        package_import = rf"^\s*from\s+{re.escape(package)}\s+import\s+.*\b{re.escape(module_name)}\b"
        if re.search(package_import, content, re.MULTILINE):
            return True
    return False


def _feature_named_sibling_tests(repo_root: Path, rel_dir: str, source: Path) -> list[str]:
    """Bounded import-based sibling tests for a changed module."""
    module_import = str(source.with_suffix("")).replace("/", ".")
    # Feature tests also live directly at tests/ root (e.g.
    # tests/test_design_board_store.py for hermes_cli/design_board_store.py);
    # glob is non-recursive, so the root scan only matches those.
    test_dirs = [repo_root / "tests"]
    pkg_test_dir = Path("tests") / rel_dir
    absolute_pkg_test_dir = repo_root / pkg_test_dir
    if pkg_test_dir != Path("tests") and absolute_pkg_test_dir.is_dir():
        test_dirs.append(absolute_pkg_test_dir)
    return [
        str(path.relative_to(repo_root))
        for test_dir in test_dirs
        for path in sorted(test_dir.glob("test_*.py"))
        if _imports_changed_module(path, module_import)
    ]


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
        source = Path(f)
        rel_dir = str(source.parent)
        candidate = Path("tests") / rel_dir / f"test_{name}"
        if (repo_root / candidate).is_file():
            modules.add(str(candidate))
        modules.update(_feature_named_sibling_tests(repo_root, rel_dir, source))
        if not (repo_root / candidate).is_file():
            # Fallback: no 1:1 test file. Monolith source files like
            # gateway/run.py or hermes_cli/kanban_db.py have feature-named
            # tests (test_shutdown_cache_cleanup.py, test_kanban_core*.py),
            # not test_<module>.py.  Select the entire package test directory
            # so regressions are caught at the merge gate, not only nightly.
            # Cap: if the directory has too many test files, downgrade to no
            # selection — the nightly full suite remains the backstop. This
            # prevents a gate-tempo explosion (AC-2 counter-metric).
            pkg_test_dir = Path("tests") / rel_dir
            if pkg_test_dir != Path("tests") and (repo_root / pkg_test_dir).is_dir():
                test_file_count = sum(
                    1 for _p in (repo_root / pkg_test_dir).glob("test_*.py")
                )
                if test_file_count <= _FALLBACK_MAX_TEST_FILES:
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
