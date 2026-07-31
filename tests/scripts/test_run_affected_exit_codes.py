"""Exit-code contract tests for the ``run-affected.sh`` preflight boundary."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
_REAL = (
    "run-affected.sh",
    "affected-tests.sh",
    "affected_tests.py",
    "check_test_wallclock_expiry.py",
)
_MAPPING_MODULES = (
    "affected_test_budget.py",
    "affected_test_mapping.py",
    "symbol_test_narrowing.py",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo(tmp_path: Path, *, branch_age_exit: int, test_exit: int = 0) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in _REAL:
        shutil.copy2(SCRIPTS / name, scripts / name)
    (repo / "hermes_cli").mkdir()
    (repo / "hermes_cli" / "__init__.py").write_text("")
    for name in _MAPPING_MODULES:
        shutil.copy2(REPO_ROOT / "hermes_cli" / name, repo / "hermes_cli" / name)
    (repo / "config").mkdir()
    (repo / "config" / "affected-test-exceptions.json").write_text(
        '{"schema_version": 1, "exceptions": []}\n'
    )

    (scripts / "check-branch-age.sh").write_text(f"#!/bin/sh\nexit {branch_age_exit}\n")
    (scripts / "run_tests.sh").write_text(
        "#!/usr/bin/env bash\n"
        "echo \"$*\" >> \"$(dirname \"$0\")/_run_tests_called\"\n"
        f"exit {test_exit}\n"
    )
    for name in (*_REAL, "check-branch-age.sh", "run_tests.sh"):
        (scripts / name).chmod(0o755)

    (repo / "pkg").mkdir()
    (repo / "pkg" / "foo.py").write_text("VALUE = 1\n")
    (repo / "tests" / "pkg").mkdir(parents=True)
    (repo / "tests" / "pkg" / "test_foo.py").write_text("def test_foo():\n    assert True\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "pkg" / "foo.py").write_text("VALUE = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "affected change")
    return repo, scripts / "_run_tests_called"


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / "scripts" / "run-affected.sh"), "HEAD~1"],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_branch_age_preflight_uses_distinct_exit_code_and_never_runs_tests(tmp_path: Path) -> None:
    repo, runner_sentinel = _make_repo(tmp_path, branch_age_exit=1)

    proc = _run(repo)

    assert proc.returncode == 3
    assert not runner_sentinel.exists(), "preflight failure must prevent pytest execution"
    assert "NO test ran" in proc.stderr
    assert "git merge main" in proc.stderr
    assert "HERMES_GATE_STALE_OK=1" in proc.stderr


def test_real_affected_test_failure_remains_exit_one(tmp_path: Path) -> None:
    repo, runner_sentinel = _make_repo(tmp_path, branch_age_exit=0, test_exit=1)

    proc = _run(repo)

    assert proc.returncode == 1
    assert runner_sentinel.exists(), "a real affected-test failure must execute pytest"
