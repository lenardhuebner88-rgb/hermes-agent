#!/usr/bin/env python3
"""Classify a git diff and print its affected pytest targets.

Use ``scripts/run-affected.sh`` to execute the selection.  The classifier is
fail-closed: changed in-scope production Python without a selected test or an
audited exception exits 4 before pytest.  Exit 3 remains reserved for the
branch-age preflight in ``run-affected.sh``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Bare worktrees do not need a venv, but the repository root must be importable
# when this file is executed directly from scripts/.
_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_IMPORT_ROOT))

from hermes_cli.affected_test_mapping import (  # noqa: E402
    EXPLICIT_TEST_PATTERNS,
    INTEGRATION_FALLBACK_MAX_TEST_FILES,
    MappingError,
    UNMAPPED_EXIT_CODE,
    affected_pytest_modules as _shared_affected_pytest_modules,
    changed_paths,
    changed_paths_with_diff_spec,
    census_repository,
    classify_changed_paths,
)

# Compatibility aliases retained for callers/tests while the implementation
# lives in one fork-owned module.
_FALLBACK_MAX_TEST_FILES = INTEGRATION_FALLBACK_MAX_TEST_FILES
_MONOLITH_TEST_PATTERNS = EXPLICIT_TEST_PATTERNS


def _repo_root() -> Path:
    import subprocess

    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise MappingError("not inside a readable git worktree")
    return Path(completed.stdout.strip())


def _changed_files(repo_root: Path, ref: str | None) -> list[str]:
    return changed_paths(repo_root, ref)


def _mapped_monolith_tests(repo_root: Path, source_path: str) -> list[str]:
    return sorted(
        {
            str(path.relative_to(repo_root))
            for pattern in _MONOLITH_TEST_PATTERNS.get(source_path, ())
            for path in repo_root.glob(pattern)
            if path.is_file()
        }
    )


def affected_pytest_modules(repo_root: Path, changed_files: list[str]) -> list[str]:
    return _shared_affected_pytest_modules(
        repo_root,
        changed_files,
        mode="integration",
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("worker", "integration"),
        default="integration",
        help=(
            "worker caps package fallback at 200 and focused unions at 217; "
            "integration applies only the package-fallback cap 800"
        ),
    )
    parser.add_argument(
        "--format",
        choices=("paths", "json"),
        default="paths",
        dest="output_format",
    )
    parser.add_argument(
        "--census",
        "--audit-all",
        action="store_true",
        dest="census",
        help="classify the complete tracked/untracked non-test Python inventory",
    )
    parser.add_argument("ref", nargs="?", help="git ref/range used as diff base")
    return parser.parse_args(argv[1:])


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        repo_root = _repo_root()
        if args.census:
            plan = census_repository(repo_root, mode=args.mode)
        else:
            changed, diff_spec = changed_paths_with_diff_spec(repo_root, args.ref)
            plan = classify_changed_paths(
                repo_root,
                changed,
                mode=args.mode,
                diff_spec=diff_spec,
            )
    except MappingError as exc:
        print(f"affected-tests: mapping error: {exc}", file=sys.stderr)
        return 2

    if args.output_format == "json":
        print(json.dumps(plan.to_dict(), sort_keys=True, indent=2))
    else:
        print(" ".join(plan.selected_tests))

    for record in plan.records:
        for warning in record.warnings:
            print(f"affected-tests: {record.path}: {warning}", file=sys.stderr)

    if plan.unmapped_paths:
        print(
            "affected-tests: unmapped production Python paths: "
            + ", ".join(plan.unmapped_paths),
            file=sys.stderr,
        )
        return UNMAPPED_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
