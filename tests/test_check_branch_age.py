"""Subprocess tests for scripts/check-branch-age.sh against a temp git repo.

Covers the five brief cases:
  0 behind           → silent, exit 0
  3 behind           → warn on stderr, exit 0
  7 behind           → red on stderr, exit 1
  7 behind + override → warn with (override aktiv), exit 0
  unresolvable main  → silent, exit 0  (also exercised via 0-behind setup)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check-branch-age.sh"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    base_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "branch-age-test",
        "GIT_AUTHOR_EMAIL": "branch-age@test.local",
        "GIT_COMMITTER_NAME": "branch-age-test",
        "GIT_COMMITTER_EMAIL": "branch-age@test.local",
    }
    if env:
        base_env.update(env)
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=base_env,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "base")
    return repo


def _advance_main(repo: Path, n: int) -> None:
    """Create n commits on main that a feature branch will be behind."""
    for i in range(n):
        path = repo / f"main-{i}.txt"
        path.write_text(f"main commit {i}\n", encoding="utf-8")
        _git(repo, "add", path.name)
        _git(repo, "commit", "-m", f"main-{i}")


def _branch_behind_main(repo: Path, behind: int) -> None:
    """Leave HEAD on feature, main advanced by `behind` commits."""
    _git(repo, "checkout", "-b", "feature")
    # feature tip is current main tip; then advance main without moving feature
    _git(repo, "checkout", "main")
    _advance_main(repo, behind)
    _git(repo, "checkout", "feature")


def _run(
    repo: Path,
    *,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HERMES_GATE_STALE_OK": ""}
    # Drop override unless the case sets it.
    env.pop("HERMES_GATE_STALE_OK", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.parametrize(
    "behind,override,want_code,stderr_contains,stderr_not_contains",
    [
        # case 1: 0 behind → silent exit 0
        (0, False, 0, None, "[branch-age]"),
        # case 2: 3 behind → warn exit 0
        (
            3,
            False,
            0,
            "[branch-age] HEAD ist 3 Commits hinter main — rebase empfohlen.",
            None,
        ),
        # case 3: 7 behind → red exit 1
        (
            7,
            False,
            1,
            "[branch-age] HEAD ist 7 Commits hinter main (>5) — ROT. Override: HERMES_GATE_STALE_OK=1",
            "(override aktiv)",
        ),
        # case 4: 7 behind + override → warn exit 0
        (
            7,
            True,
            0,
            "(override aktiv)",
            None,
        ),
    ],
)
def test_branch_age_cases(
    tmp_path: Path,
    behind: int,
    override: bool,
    want_code: int,
    stderr_contains: str | None,
    stderr_not_contains: str | None,
) -> None:
    repo = _init_repo(tmp_path)
    if behind > 0:
        _branch_behind_main(repo, behind)
    # behind==0: still on main at tip

    extra = {"HERMES_GATE_STALE_OK": "1"} if override else None
    result = _run(repo, env_extra=extra)

    assert result.returncode == want_code, (
        f"exit={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.stdout == "", f"stdout must be empty, got {result.stdout!r}"
    if stderr_contains is not None:
        assert stderr_contains in result.stderr
    if stderr_not_contains is not None:
        assert stderr_not_contains not in result.stderr
    if behind == 0:
        assert result.stderr == ""


def test_unresolvable_main_silent_exit_0(tmp_path: Path) -> None:
    """If main is not resolvable, stay quiet and exit 0 (brief contract)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "only-branch")
    (repo / "README").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "base")
    # rename so neither HEAD nor refs/heads/main exists as "main"
    # already on only-branch; no main ref → rev-list fails → BEHIND=0
    result = _run(repo)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_script_is_executable_and_bash_syntax() -> None:
    assert SCRIPT.is_file()
    # bash -n syntax check (gate list)
    check = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr
