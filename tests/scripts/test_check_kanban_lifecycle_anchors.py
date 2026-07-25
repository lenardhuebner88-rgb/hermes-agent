"""Tests for scripts/check_kanban_lifecycle_anchors.py."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_kanban_lifecycle_anchors.py"
DOCUMENT = REPO_ROOT / "docs" / "kanban" / "LIFECYCLE.md"


def _run(document: Path = DOCUMENT, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--document", str(document), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _fixture(
    tmp_path: Path,
    *,
    label: str = "dispatch_once",
    anchor_line: int = 1,
) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    document = root / "docs" / "kanban" / "LIFECYCLE.md"
    source = root / "hermes_cli" / "kanban_db.py"
    document.parent.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    source.write_text(
        "\n\n\n\n\n\ndef dispatch_once():\n    return None\n",
        encoding="utf-8",
    )
    document.write_text(
        "# Fixture lifecycle map\n\n"
        f"[`{label}`](../../hermes_cli/kanban_db.py#L{anchor_line})\n",
        encoding="utf-8",
    )
    return document, source


def test_committed_lifecycle_map_resolves_against_real_source() -> None:
    result = _run()

    assert result.returncode == 0, result.stderr
    # Derive the expected count from the document instead of hardcoding it. A
    # literal ("OK: 95 anchors resolved") turns every legitimate addition to the
    # map into a spurious failure, which is how this assertion came to be edited
    # rather than trusted. Counting the anchors still catches the case the
    # literal was there to catch: anchors silently vanishing from the document.
    expected = len(
        re.findall(r"\]\(\.\./\.\./hermes_cli/kanban_db\.py#L\d+\)", DOCUMENT.read_text(encoding="utf-8"))
    )
    # Equality against the document catches the checker silently skipping
    # anchors it used to parse; the floor catches the document losing coverage.
    # 95 is the count when this guard was introduced (2026-07-25) — raise it
    # deliberately, never lower it to make a deletion pass.
    assert expected >= 95, f"lifecycle map lost coverage: only {expected} anchors"
    assert f"OK: {expected} anchors resolved" in result.stdout, result.stdout


def test_drifted_anchor_is_detected(tmp_path: Path) -> None:
    document, _source = _fixture(tmp_path)

    result = _run(document)

    assert result.returncode == 1
    assert "drifted from L1 to L7" in result.stderr


def test_fix_repairs_drift_and_rerun_passes(tmp_path: Path) -> None:
    document, _source = _fixture(tmp_path)

    fixed = _run(document, "--fix")
    rerun = _run(document)

    assert fixed.returncode == 0, fixed.stderr
    assert "fixed `dispatch_once`: L1 -> L7" in fixed.stdout
    assert "#L7)" in document.read_text(encoding="utf-8")
    assert rerun.returncode == 0, rerun.stderr


def test_fix_reports_vanished_symbol_without_guessing(tmp_path: Path) -> None:
    document, _source = _fixture(tmp_path, label="vanished_symbol")
    before = document.read_text(encoding="utf-8")

    result = _run(document, "--fix")

    assert result.returncode == 1
    assert "no longer exists; refusing to guess" in result.stderr
    assert document.read_text(encoding="utf-8") == before
