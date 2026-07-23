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
Known monoliths use the explicit feature-test patterns below. When neither a
1:1 file nor an explicit mapping exists, fall back to the entire
``tests/<pkg>/`` directory so regressions are caught at the merge gate instead
of only in the nightly full suite.

The generic selection logic mirrors
``hermes_cli.kanban_worktrees._affected_pytest_modules`` on purpose,
reimplemented with pure stdlib so it also runs in a bare worktree that has no
venv. The run-affected-specific monolith table is kept here at the gate edge.

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

# Source paths with feature-split test suites. Keep this small and explicit:
# these entries prevent a package-wide fallback without relying on test files
# importing every code path exercised through fixtures or indirect callers.
_MONOLITH_TEST_PATTERNS: dict[str, tuple[str, ...]] = {
    "hermes_cli/strategist.py": ("tests/hermes_cli/test_strategist*.py",),
    "hermes_cli/kanban_db.py": (
        "tests/hermes_cli/test_kanban_db*.py",
        "tests/hermes_cli/test_kanban_lanes.py",
        "tests/hermes_cli/test_kanban_block_kind*.py",
    ),
    "hermes_cli/kanban_worktrees.py": (
        "tests/hermes_cli/test_kanban_worktrees*.py",
        "tests/hermes_cli/test_visual_gate.py",
    ),
}


def _mapped_monolith_tests(repo_root: Path, source_path: str) -> list[str]:
    """Expand a known monolith's maintained test patterns to existing files."""
    return sorted(
        {
            str(path.relative_to(repo_root))
            for pattern in _MONOLITH_TEST_PATTERNS.get(source_path, ())
            for path in repo_root.glob(pattern)
            if path.is_file()
        }
    )


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
    # Generic logic mirrors hermes_cli.kanban_worktrees._affected_pytest_modules.
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
        mapped_tests = _mapped_monolith_tests(repo_root, f)
        if mapped_tests:
            # Known monoliths are feature-split: the maintained pattern set is
            # the complete blast radius. Adding import-based siblings on top
            # re-selects the whole package directory (hundreds of unrelated
            # tests), defeating the explicit mapping.
            modules.update(mapped_tests)
            continue
        if (repo_root / candidate).is_file():
            modules.update(_feature_named_sibling_tests(repo_root, rel_dir, source))
            continue
        modules.update(_feature_named_sibling_tests(repo_root, rel_dir, source))
        # Last-resort fallback: no 1:1 test and no explicit mapping. Select the
        # package directory (within its safety cap) rather than losing coverage.
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
