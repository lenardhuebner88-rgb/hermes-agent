from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
AFFECTED_TESTS_PY = REPO_ROOT / "scripts" / "affected_tests.py"
AFFECTED_TESTS_SH = REPO_ROOT / "scripts" / "affected-tests.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_core_edit_selects_focused_import_test_before_fallback_cap(tmp_path: Path) -> None:
    source = tmp_path / "hermes_cli" / "kanban_db.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")

    test_dir = tmp_path / "tests" / "hermes_cli"
    test_dir.mkdir(parents=True)
    (test_dir / "test_core_contract.py").write_text(
        "from hermes_cli import kanban_db\n\ndef test_contract():\n    assert kanban_db.VALUE\n",
        encoding="utf-8",
    )
    for index in range(200):
        (test_dir / f"test_unrelated_{index:03d}.py").write_text(
            "def test_unrelated():\n    assert True\n", encoding="utf-8"
        )

    # Readable duration cache: the budget check fails closed without one and
    # affected-tests.sh would exit 2 instead of printing the selection
    # (see hermes_cli/affected_test_budget.py).
    (tmp_path / "test_durations.json").write_text("{}\n", encoding="utf-8")

    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline")
    source.write_text("VALUE = 2\n", encoding="utf-8")

    raw = subprocess.run(
        ["python3", str(AFFECTED_TESTS_PY), "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert raw.stdout.split() == ["tests/hermes_cli/test_core_contract.py"]

    bounded = subprocess.run(
        [str(AFFECTED_TESTS_SH), "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    assert bounded.stdout.split() == ["tests/hermes_cli/test_core_contract.py"]
    # A readable cache means the budget check ran and passed: no skip note, no
    # fail-closed error. (Pre-2026-08-01 a missing cache emitted a visible
    # "time-budget check skipped" note; e50e541123 made that case fail closed —
    # pinned end-to-end by
    # tests/scripts/test_run_affected.py::test_missing_duration_cache_fails_closed_without_running_pytest.)
    assert "time-budget check" not in bounded.stderr, bounded.stderr
