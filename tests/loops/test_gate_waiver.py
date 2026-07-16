"""Tests für loops/gate.sh — Baseline-bewusste Waiver-Logik für scope-fremde,
Reihenfolge-abhängige pytest-Fails im kombinierten affected-Lauf (isoliert
grün, vgl. AGENTS.md/gate.sh-Kopfkommentar 2026-07-16).

Echtes Datenformat statt Synthetik: die Mini-Testdateien laufen wirklich unter
pytest in einem echten tmp-Git-Repo, gate.sh wird als Subprozess aufgerufen und
parst echte "FAILED ..."-Zeilen aus echtem pytest-Output — nichts, was das Gate
parst, wird gemockt. scripts/affected-tests.sh wird durch ein festverdrahtetes
Echo ersetzt, damit die Tests die Waiver-Auswertung in gate.sh prüfen statt der
Diff->Test-Mapping-Heuristik aus scripts/affected_tests.py (die hat eigene
Tests).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_SH = REPO_ROOT / "loops" / "gate.sh"

LEAKER_TEST = """\
from libstate import SHARED


def test_leaker():
    SHARED.append("x")
    assert True
"""

TARGET_TEST = """\
from libstate import SHARED


def test_target():
    assert SHARED == []
"""

BROKEN_TEST = """\
def test_real_break():
    assert False, "echter Bruch, kein Order-Leak"
"""

DIFF_OK_TEST = """\
def test_diff_ok():
    assert True
"""

DIFF_BROKEN_TEST = """\
def test_diff_broken():
    assert False
"""


def g(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, encoding="utf-8", check=False,
    )


def _init_repo_with_foreign_fixtures(tmp_path: Path) -> tuple[Path, str]:
    """Commit 1: README + libstate.py (geteilter Modulzustand) + die
    scope-fremden Testdateien test_leaker/test_target/test_broken — die
    bleiben über den gesamten Testverlauf UNVERÄNDERT, sind also nie Teil des
    späteren Diffs. Gibt (repo, base_sha) zurück; base_sha ist der REF, gegen
    den gate.sh den späteren "Diff-Commit" misst (verifier-Modus: Diff seit
    <ref>)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    g(repo, "init", "-b", "main")
    g(repo, "config", "user.email", "gate@test")
    g(repo, "config", "user.name", "gate-test")
    (repo / "README.md").write_text("hallo\n", encoding="utf-8")
    (repo / "libstate.py").write_text("SHARED = []\n", encoding="utf-8")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_leaker.py").write_text(LEAKER_TEST, encoding="utf-8")
    (tests_dir / "test_target.py").write_text(TARGET_TEST, encoding="utf-8")
    (tests_dir / "test_broken.py").write_text(BROKEN_TEST, encoding="utf-8")
    g(repo, "add", "-A")
    commit = g(repo, "commit", "-m", "init: foreign fixtures")
    assert commit.returncode == 0, commit.stdout + commit.stderr
    base_sha = g(repo, "rev-parse", "HEAD").stdout.strip()
    assert base_sha
    return repo, base_sha


def _commit_diff_test(repo: Path, body: str) -> None:
    (repo / "tests" / "test_diff.py").write_text(body, encoding="utf-8")
    g(repo, "add", "-A")
    commit = g(repo, "commit", "-m", "diff: test_diff")
    assert commit.returncode == 0, commit.stdout + commit.stderr


def _write_affected_tests_script(repo: Path, files: list[str]) -> None:
    """Fake scripts/affected-tests.sh — echot eine festverdrahtete Liste statt
    die echte Diff->Test-Mapping-Heuristik zu laufen. Muss nicht getrackt sein,
    gate.sh führt die Datei nur direkt aus."""
    script_dir = repo / "scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    script = script_dir / "affected-tests.sh"
    script.write_text(
        "#!/usr/bin/env bash\necho '" + " ".join(files) + "'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _write_fake_ruff(repo: Path) -> Path:
    """No-op ruff-Stand-in — dieser Testfall prüft die pytest-Waiver-Logik,
    nicht ruff (dessen Pfad bleibt unverändert, siehe done_when #3)."""
    path = repo / "fake-ruff.sh"
    path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_gate(repo: Path, ref: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GATE_PY": sys.executable,
        "GATE_RUFF": str(_write_fake_ruff(repo)),
    }
    return subprocess.run(
        [str(GATE_SH), ref],
        cwd=repo, env=env, capture_output=True, encoding="utf-8", check=False,
        timeout=60,
    )


# ── (a) scope-fremder Order-Leak: isoliert (kombiniert mit den Diff-eigenen
#        Tests, ohne den Leaker) grün -> gewaived, GATE_PASS ──────────────────

def test_scope_foreign_order_leak_is_waived(tmp_path):
    repo, base_sha = _init_repo_with_foreign_fixtures(tmp_path)
    _commit_diff_test(repo, DIFF_OK_TEST)
    _write_affected_tests_script(
        repo, ["tests/test_diff.py", "tests/test_leaker.py", "tests/test_target.py"]
    )

    result = _run_gate(repo, base_sha)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "GATE_PASS" in result.stdout
    assert (
        "GATE_WARN: scope-fremder Order-Leak gewaived: tests/test_target.py"
        in result.stdout
    ), result.stdout
    assert "GATE_FAIL" not in result.stdout


# ── (b) scope-fremder Fail, der auch isoliert (mit Diff-Tests kombiniert) rot
#        bleibt -> echter Bruch, GATE_FAIL trotz "scope-fremd" ────────────────

def test_scope_foreign_real_break_stays_fail(tmp_path):
    repo, base_sha = _init_repo_with_foreign_fixtures(tmp_path)
    _commit_diff_test(repo, DIFF_OK_TEST)
    _write_affected_tests_script(repo, ["tests/test_diff.py", "tests/test_broken.py"])

    result = _run_gate(repo, base_sha)

    assert result.returncode == 12, result.stdout + result.stderr
    assert "GATE_FAIL: pytest" in result.stdout
    assert "reproduziert isoliert: tests/test_broken.py" in result.stdout
    assert "GATE_WARN" not in result.stdout


# ── (c) Fail in einer Testdatei, die selbst Teil des Diffs ist -> sofort
#        GATE_FAIL, kein isolierter Nachlauf ───────────────────────────────

def test_diff_own_test_file_fails_immediately_without_rerun(tmp_path):
    repo, base_sha = _init_repo_with_foreign_fixtures(tmp_path)
    _commit_diff_test(repo, DIFF_BROKEN_TEST)
    _write_affected_tests_script(
        repo, ["tests/test_diff.py", "tests/test_leaker.py", "tests/test_target.py"]
    )

    result = _run_gate(repo, base_sha)

    assert result.returncode == 12, result.stdout + result.stderr
    assert "GATE_FAIL: pytest (eigene Testdatei rot: tests/test_diff.py)" in result.stdout
    assert "isolierter Nachlauf" not in result.stdout
    assert "GATE_WARN" not in result.stdout


# ── (d) grüner Lauf -> unverändertes Verhalten, kein WARN ──────────────────

def test_all_green_stays_unchanged_behavior(tmp_path):
    repo, base_sha = _init_repo_with_foreign_fixtures(tmp_path)
    _commit_diff_test(repo, DIFF_OK_TEST)
    _write_affected_tests_script(repo, ["tests/test_diff.py"])

    result = _run_gate(repo, base_sha)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "GATE_PASS" in result.stdout
    assert "GATE_WARN" not in result.stdout
    assert "GATE_FAIL" not in result.stdout
