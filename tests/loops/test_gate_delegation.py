"""Tests für loops/gate.sh — pytest-Teil delegiert vollständig an den
kanonischen Per-File-Runner scripts/run-affected.sh (2026-07-16, löst die
Waiver-Logik aus 89256b1e2 ab: die war das richtige Urteil im Raw-pytest-
Modell, wird mit der Delegation aber strukturell überflüssig, weil
run-affected.sh selbst schon pro Datei isoliert und bei Rot einmal
reproduziert, bevor es zählt).

Echtes Verhalten statt Synthetik: gate.sh wird als echter Subprozess gegen
ein echtes tmp-Git-Repo aufgerufen (git diff, git rev-parse). Nur die
Delegations-Schnittstelle selbst (scripts/run-affected.sh) wird durch ein
Fake ersetzt — dessen Vertrag ist Aufruf + Exit-Code, kein pytest-Output-
Format mehr, das gate.sh parsen müsste (die Waiver-Parser-Logik ist weg).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_SH = REPO_ROOT / "loops" / "gate.sh"


def g(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, encoding="utf-8", check=False,
    )


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    g(repo, "init", "-b", "main")
    g(repo, "config", "user.email", "gate@test")
    g(repo, "config", "user.name", "gate-test")
    (repo / "README.md").write_text("hallo\n", encoding="utf-8")
    g(repo, "add", "-A")
    commit = g(repo, "commit", "-m", "init")
    assert commit.returncode == 0, commit.stdout + commit.stderr
    base_sha = g(repo, "rev-parse", "HEAD").stdout.strip()
    assert base_sha
    return repo, base_sha


def _write_fake_run_affected(repo: Path, log_path: Path, exit_code: int) -> None:
    """Fake scripts/run-affected.sh — protokolliert Aufruf ($1 = REF) +
    PYTHONPATH in log_path, dann exit_code. Prüft die Delegations-
    Schnittstelle (Aufruf passiert, REF wird durchgereicht, PYTHONPATH bleibt
    exportiert), nicht run-affected.sh selbst (eigene Tests, anti_scope)."""
    script_dir = repo / "scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    script = script_dir / "run-affected.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "CALLED ref=$1 PYTHONPATH=$PYTHONPATH" >> "{log_path}"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _write_fake_ruff(repo: Path, exit_code: int) -> Path:
    path = repo / "fake-ruff.sh"
    path.write_text(f"#!/usr/bin/env bash\nexit {exit_code}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_gate(repo: Path, ref: str, ruff_exit: int = 0) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GATE_PY": sys.executable,
        "GATE_RUFF": str(_write_fake_ruff(repo, ruff_exit)),
    }
    return subprocess.run(
        [str(GATE_SH), ref],
        cwd=repo, env=env, capture_output=True, encoding="utf-8", check=False,
        timeout=60,
    )


# ── (a) grüner Fall: run-affected.sh exit 0 -> GATE_PASS, Aufruf protokolliert,
#        REF und PYTHONPATH=Worktree kommen an ─────────────────────────────

def test_delegates_to_run_affected_and_passes(tmp_path):
    repo, base_sha = _init_repo(tmp_path)
    log = tmp_path / "call.log"
    _write_fake_run_affected(repo, log, exit_code=0)

    result = _run_gate(repo, base_sha)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "pytest (affected, per-file isoliert via run-affected.sh)" in result.stdout
    assert "GATE_PASS" in result.stdout
    assert "GATE_FAIL" not in result.stdout

    assert log.exists(), "run-affected.sh wurde nicht aufgerufen"
    call_line = log.read_text(encoding="utf-8").strip()
    assert f"ref={base_sha}" in call_line
    assert f"PYTHONPATH={repo}" in call_line


# ── (b) roter Fall: run-affected.sh exit != 0 (nach seinem eigenen rerun-once)
#        -> GATE_FAIL: pytest, exit 12 ──────────────────────────────────────

def test_run_affected_failure_is_gate_fail(tmp_path):
    repo, base_sha = _init_repo(tmp_path)
    log = tmp_path / "call.log"
    _write_fake_run_affected(repo, log, exit_code=3)

    result = _run_gate(repo, base_sha)

    assert result.returncode == 12, result.stdout + result.stderr
    assert "GATE_FAIL: pytest" in result.stdout
    assert log.exists()


# ── (c) ruff-Fail bleibt exit 11, run-affected.sh wird gar nicht erst
#        aufgerufen (kurzschließt vor dem pytest-Block) ─────────────────────

def test_ruff_failure_short_circuits_before_run_affected(tmp_path):
    repo, base_sha = _init_repo(tmp_path)
    (repo / "bad.py").write_text("x = 1\n", encoding="utf-8")
    g(repo, "add", "-A")
    commit = g(repo, "commit", "-m", "diff: bad.py")
    assert commit.returncode == 0, commit.stdout + commit.stderr

    log = tmp_path / "call.log"
    _write_fake_run_affected(repo, log, exit_code=0)

    result = _run_gate(repo, base_sha, ruff_exit=1)

    assert result.returncode == 11, result.stdout + result.stderr
    assert "GATE_FAIL: ruff" in result.stdout
    assert not log.exists(), "run-affected.sh haette nach ruff-Fail nicht laufen duerfen"
