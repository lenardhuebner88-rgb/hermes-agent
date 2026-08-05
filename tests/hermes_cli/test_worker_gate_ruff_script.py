"""Tests for scripts/worker-gate-ruff.sh

Four behavioural contracts:
  1. Changed .py with a ruff violation (F821 undefined name) → exit != 0
  2. Changed .py with clean code → exit 0
  3. Only non-.py change → exit 0 with "skip" message
  4. Deleted .py in the diff → no crash, exit 0 (when nothing else changed)

Uses a real temp git repo + worktree (identical setup to test_cse_013_gates.py).
Ruff is found via the same probe order the script uses: main-repo venv first.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    # commit.gpgsign=false: this checkout signs commits through a helper that
    # needs a key from the environment, and the test env scrubs *_KEY vars.
    # Without the override the fixture dies at `git commit` with exit 128 —
    # a red that says nothing about the script under test.
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args], cwd=repo,
        check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def _find_ruff() -> str | None:
    """Locate ruff using the same probe order as the script.

    a. common-dir → main-repo venv  (tests run inside the worktree)
    b. shutil.which("ruff")
    c. python3 -m ruff (module fallback — not relevant for test; just note if absent)
    """
    # Probe: resolve this file's own git common-dir → main-repo venv
    here = Path(__file__).resolve().parent
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=here, check=True, capture_output=True, text=True,
        ).stdout.strip()
        if common.endswith("/.git"):
            main_repo = common[: -len("/.git")]
            for candidate in (
                Path(main_repo) / "venv" / "bin" / "ruff",
                Path(main_repo) / ".venv" / "bin" / "ruff",
            ):
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
    except subprocess.CalledProcessError:
        pass
    return shutil.which("ruff")


RUFF_PATH = _find_ruff()

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "worker-gate-ruff.sh"
# Helper scripts worker-gate-ruff.sh invokes via "$SCRIPT_DIR/<name>"; they must
# travel with it into the fixture worktree or the script runs degraded.
#
# DERIVED from the script source, never hand-listed: the hand-listed variant is
# exactly how this file went red on 2026-08-05 — the script grew a
# ``$SCRIPT_DIR/gate_diff_base.py`` call, the fixture kept shipping only the
# script, and the resulting "[Errno 2] No such file or directory" tripped the
# crash guard in contract 4. A future helper must not be able to repeat that
# silently; ``test_fixture_ships_every_helper_the_script_calls`` below turns a
# drifted list into a loud, self-explaining failure.
#
# The name must carry an extension (``gate_diff_base.py``): ``$SCRIPT_DIR/..``
# is the script's own repo-root walk-up, not a helper, and copying it would
# make the fixture blow up on a directory instead of failing usefully.
_SCRIPT_DIR_CALL_RE = re.compile(
    r"\$(?:\{)?SCRIPT_DIR(?:\})?/([A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+)"
)


def _discover_script_helpers() -> tuple[Path, ...]:
    names = sorted(set(_SCRIPT_DIR_CALL_RE.findall(SCRIPT.read_text(encoding="utf-8"))))
    return tuple(SCRIPT.parent / name for name in names)


SCRIPT_HELPERS = _discover_script_helpers()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def main_repo(tmp_path):
    """A bare-minimum git repo with a committed Python file on branch 'main'."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "tester")
    # Seed a clean base file so HEAD~0 has something committed
    (r / "base.py").write_text("x = 1\n", encoding="utf-8")
    # The helpers worker-gate-ruff.sh shells out to must exist next to it, and
    # they must be COMMITTED: dropping them in untracked would make them part
    # of the very diff the script lints. Without them the script fell back to
    # an empty base and printed its own "No such file or directory", which the
    # crash guard in contract 4 mistook for ruff choking on the deleted path —
    # a green-gate red on 2026-08-05 with no code defect behind it.
    (r / "scripts").mkdir(exist_ok=True)
    for helper in SCRIPT_HELPERS:
        shutil.copy2(str(helper), str(r / "scripts" / helper.name))
    _git(r, "add", "base.py", "scripts")
    _git(r, "commit", "-m", "base")
    return r


@pytest.fixture
def worktree(main_repo, tmp_path):
    """A git worktree branched off main_repo, with the script available."""
    wt = tmp_path / "worktree"
    _git(main_repo, "worktree", "add", str(wt), "-b", "worker-gate-ruff-test")
    # Put the real script into the worktree's scripts/ dir
    scripts_dir = wt / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    shutil.copy2(str(SCRIPT), str(scripts_dir / "worker-gate-ruff.sh"))
    (scripts_dir / "worker-gate-ruff.sh").chmod(0o755)
    return wt


def _run_script(worktree: Path, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Execute the script with cwd = the worktree (as the kanban worker_gate does).

    Injects the real venv's bin/ dir at the front of PATH so the script's
    probe-step (b) — command -v ruff — finds the real binary even when the
    tmp test repo has no venv of its own.  This mirrors what systemd does for
    the real worker (it sets PATH with venv/bin first).
    """
    env = os.environ.copy()
    if RUFF_PATH is not None:
        ruff_bin_dir = str(Path(RUFF_PATH).parent)
        env["PATH"] = ruff_bin_dir + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [str(worktree / "scripts" / "worker-gate-ruff.sh")],
        cwd=str(cwd or worktree),
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(RUFF_PATH is None, reason="ruff binary not found — skip integration test")
def test_ruff_gate_violation_exits_nonzero(main_repo, worktree):
    """Contract 1: a changed .py with F821 (undefined name) → ruff exits != 0."""
    # Add a file with an obvious ruff violation (F821: undefined name)
    bad_py = worktree / "bad.py"
    bad_py.write_text("print(UNDEFINED_NAME_XYZ)\n", encoding="utf-8")
    # Stage the file so it appears in git diff (but don't commit — uncommitted
    # changes also match the affected-file selection).
    # Actually, just leave it untracked — the script picks up ls-files --others too.
    # Verify it shows up as untracked in the worktree
    ls_out = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=worktree, capture_output=True, text=True, check=True,
    ).stdout
    assert "bad.py" in ls_out, "bad.py must appear as untracked for the test to be valid"

    result = _run_script(worktree)
    assert result.returncode != 0, (
        f"Expected non-zero exit for ruff violation; got 0.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.skipif(RUFF_PATH is None, reason="ruff binary not found — skip integration test")
def test_ruff_gate_clean_file_exits_zero(main_repo, worktree):
    """Contract 2: a changed .py with clean code → exit 0."""
    clean_py = worktree / "clean.py"
    clean_py.write_text(
        "# clean file\nx = 1\n",
        encoding="utf-8",
    )

    result = _run_script(worktree)
    assert result.returncode == 0, (
        f"Expected exit 0 for clean .py; got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.skipif(RUFF_PATH is None, reason="ruff binary not found — skip integration test")
def test_ruff_gate_no_py_change_skips(main_repo, worktree):
    """Contract 3: only a non-.py file changed → exit 0 with skip message."""
    (worktree / "README.md").write_text("# hello\n", encoding="utf-8")

    result = _run_script(worktree)
    assert result.returncode == 0, (
        f"Expected exit 0 for non-.py change; got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "skip" in combined.lower(), (
        f"Expected 'skip' in output for no-.py change; got:\n{combined}"
    )


@pytest.mark.skipif(RUFF_PATH is None, reason="ruff binary not found — skip integration test")
def test_ruff_gate_deleted_py_no_crash(main_repo, worktree):
    """Contract 4: a .py file deleted from the working tree appears in the diff
    but the script must NOT pass the missing path to ruff (crash guard).
    Result: exit 0 if no other .py files changed."""
    # Delete a file that exists in the merge-base (base.py is committed on main):
    # it is guaranteed to show up in `git diff --name-only <base>` as deleted.
    # (A file committed only on the worktree branch and then unlinked would not
    # appear in the diff at all and would never exercise the [ -f ] guard.)
    deleted = worktree / "base.py"
    deleted.unlink()
    diff_out = subprocess.run(
        ["git", "diff", "--name-only", "main"],
        cwd=worktree, capture_output=True, text=True, check=True,
    ).stdout
    assert "base.py" in diff_out, "base.py must appear as deleted in the diff for the test to be valid"

    result = _run_script(worktree)
    # The deleted file must not cause a crash (ruff: "No such file or directory")
    # and since no other .py is changed, we expect clean exit 0.
    assert result.returncode == 0, (
        f"Expected exit 0 after deleting .py (no other changes); got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # Must not contain typical ruff crash message about missing file
    combined = result.stdout + result.stderr
    assert "No such file" not in combined, (
        f"Script passed deleted file to ruff:\n{combined}"
    )


# ---------------------------------------------------------------------------
# Contract 5: the fixture must not silently drift away from the script
# ---------------------------------------------------------------------------

def test_fixture_ships_every_helper_the_script_calls():
    """Every ``$SCRIPT_DIR/<helper>`` the script shells out to must exist and
    be shipped into the fixture worktree.

    Guards the 2026-08-05 green-gate red: the script gained a helper call
    (``gate_diff_base.py``), the hand-listed fixture did not, so the script ran
    degraded and its own "[Errno 2] No such file or directory" tripped the
    crash guard in contract 4 — a red night with no product defect behind it.
    Deriving the list makes that impossible; this test makes the derivation
    itself falsifiable (an empty match set would silently ship nothing).
    """
    assert SCRIPT_HELPERS, (
        "no $SCRIPT_DIR/<helper> call found in scripts/worker-gate-ruff.sh — "
        "either the script stopped using helpers (drop this guard) or the "
        "regex no longer matches how they are invoked"
    )
    missing = [str(h) for h in SCRIPT_HELPERS if not h.is_file()]
    assert not missing, (
        "worker-gate-ruff.sh calls helper(s) that do not exist next to it: "
        f"{missing}"
    )
