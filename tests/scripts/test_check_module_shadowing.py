"""Tests for scripts/check_module_shadowing.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

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


def test_committed_modules_have_no_shadowed_top_level_definitions() -> None:
    result = _run()

    assert result.returncode == 0, result.stderr
    assert "OK: 2 modules checked; no duplicate top-level definitions" in result.stdout
