"""Tests for scripts/check_module_shadowing.py."""

from __future__ import annotations

import subprocess
import sys
import json
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
