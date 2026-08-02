"""Tests for scripts/check_module_shadowing.py."""

from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
import json
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_module_shadowing.py"
ALLOWLIST = REPO_ROOT / "scripts" / "module_shadowing_allowlist.json"


def _run(
    repo_root: Path = REPO_ROOT,
    allowlist: Path = ALLOWLIST,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo_root),
            "--allowlist",
            str(allowlist),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _fixture_repo(
    tmp_path: Path,
    *,
    kanban_db: str,
    kanban_worktrees: str = "def unique_worktree_helper():\n    pass\n",
    allowlist: list[dict[str, str]] | None = None,
) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    module_dir = root / "hermes_cli"
    module_dir.mkdir(parents=True)
    (module_dir / "kanban_db.py").write_text(kanban_db, encoding="utf-8")
    (module_dir / "kanban_worktrees.py").write_text(
        kanban_worktrees, encoding="utf-8"
    )
    allowlist_path = root / "scripts" / "module_shadowing_allowlist.json"
    allowlist_path.parent.mkdir(parents=True)
    allowlist_path.write_text(
        json.dumps(allowlist if allowlist is not None else [], indent=2) + "\n",
        encoding="utf-8",
    )
    return root, allowlist_path


def test_committed_modules_have_no_shadowed_top_level_definitions() -> None:
    result = _run()

    assert result.returncode == 0, result.stderr
    assert "OK: 2 modules checked; no duplicate top-level definitions" in result.stdout


@pytest.mark.parametrize("keyword", ["def", "async def", "class"])
def test_duplicate_top_level_definition_reports_every_line(
    tmp_path: Path,
    keyword: str,
) -> None:
    root, allowlist = _fixture_repo(
        tmp_path,
        kanban_db=(
            f"{keyword} duplicate():\n    pass\n"
            "\n"
            "VALUE = 1\n"
            f"{keyword} duplicate():\n    pass\n"
        ),
    )

    result = _run(root, allowlist)

    assert result.returncode == 1
    assert "hermes_cli/kanban_db.py:1: duplicate top-level definition 'duplicate'" in result.stderr
    assert "hermes_cli/kanban_db.py:5: duplicate top-level definition 'duplicate'" in result.stderr


def test_duplicate_nested_definitions_are_ignored(tmp_path: Path) -> None:
    root, allowlist = _fixture_repo(
        tmp_path,
        kanban_db=(
            "def first():\n"
            "    def nested():\n"
            "        pass\n"
            "\n"
            "def second():\n"
            "    def nested():\n"
            "        pass\n"
        ),
    )

    result = _run(root, allowlist)

    assert result.returncode == 0, result.stderr


def test_explicit_allowlist_suppresses_only_named_file_and_symbol(
    tmp_path: Path,
) -> None:
    duplicate = "def intentional():\n    pass\n\ndef intentional():\n    pass\n"
    root, allowlist = _fixture_repo(
        tmp_path,
        kanban_db=duplicate,
        kanban_worktrees=duplicate,
        allowlist=[
            {
                "path": "hermes_cli/kanban_db.py",
                "name": "intentional",
                "reason": "fixture proves an explicit reviewed decision",
            }
        ],
    )

    result = _run(root, allowlist)

    assert result.returncode == 1
    assert "hermes_cli/kanban_db.py" not in result.stderr
    assert "hermes_cli/kanban_worktrees.py:1" in result.stderr
    assert "hermes_cli/kanban_worktrees.py:4" in result.stderr


def test_committed_allowlist_is_an_explicit_empty_list() -> None:
    """AC-1: the allowlist exists in the repo and starts out empty."""
    assert json.loads(ALLOWLIST.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "entries",
    [
        [{"path": "hermes_cli/kanban_db.py", "name": "intentional"}],
        [{"path": "hermes_cli/kanban_db.py", "name": "intentional", "reason": " "}],
        [["hermes_cli/kanban_db.py", "intentional", "terse"]],
        {"path": "hermes_cli/kanban_db.py"},
    ],
)
def test_allowlist_entry_without_a_reason_fails_loudly(
    tmp_path: Path,
    entries: object,
) -> None:
    """A suppression must stay a visible decision, never a silent typo."""
    root, allowlist = _fixture_repo(
        tmp_path,
        kanban_db="def intentional():\n    pass\n\ndef intentional():\n    pass\n",
    )
    allowlist.write_text(json.dumps(entries), encoding="utf-8")

    result = _run(root, allowlist)

    assert result.returncode == 2, result.stdout
    assert "allowlist" in result.stderr


def test_checker_imports_no_repository_module() -> None:
    """AC-4 counter-metric: a pure AST walk, so a broken repo cannot break it."""
    repo_modules = {
        path.name
        for path in REPO_ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    } | {path.stem for path in REPO_ROOT.glob("*.py")}
    assert "hermes_cli" in repo_modules, "guard lost its subject"

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import ties the checker to a package"
            if node.module:
                imported.add(node.module.split(".")[0])

    assert not imported & repo_modules, sorted(imported & repo_modules)


def test_checker_modifies_no_file(tmp_path: Path) -> None:
    """AC-4 counter-metric: read-only, even on the failing path."""
    root, allowlist = _fixture_repo(
        tmp_path,
        kanban_db="def dupe():\n    pass\n\ndef dupe():\n    pass\n",
    )

    def _snapshot() -> dict[str, tuple[int, str]]:
        return {
            str(path.relative_to(root)): (
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    before = _snapshot()
    result = _run(root, allowlist)

    assert result.returncode == 1
    assert _snapshot() == before


def test_real_module_scan_stays_far_under_the_suite_budget() -> None:
    """AC-4 counter-metric: the whole added gate costs a fraction of 5 s."""
    started = time.perf_counter()
    result = _run()
    elapsed = time.perf_counter() - started

    assert result.returncode == 0, result.stderr
    assert elapsed < 5.0, f"checker took {elapsed:.2f}s"
