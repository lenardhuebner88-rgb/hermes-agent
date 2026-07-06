"""Regression tests for scripts/run-affected.sh — the empty-affected-tests trap.

The unsafe one-liner ``scripts/run_tests.sh $(scripts/affected-tests.sh)``
expands to a bare ``run_tests.sh`` when the diff maps no test files, which runs
the WHOLE suite (~31k tests, timeout / EXIT 124). ``run-affected.sh`` must
instead SKIP pytest (exit 0) and never reach the canonical runner.

These drive the real shell scripts in a throwaway git repo with a *stubbed*
``run_tests.sh`` that records its invocation in a sentinel file — so we can
assert positively that the full-suite runner is never reached on an empty diff.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
# The real scripts under test (copied verbatim); run_tests.sh is stubbed.
_REAL = ("run-affected.sh", "affected-tests.sh", "affected_tests.py")
_STUB_RUN_TESTS = """#!/usr/bin/env bash
# Test stub: record the args we were called with, then exit 0 WITHOUT running
# any real test suite. Its mere existence in the sentinel = "full runner reached".
echo "$*" >> "$(dirname "$0")/_run_tests_called"
exit 0
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway repo with the real wrapper + a stubbed run_tests.sh.

    Initial commit ships a source/test pair so the mapped case has a real
    target. Returns (repo_root, sentinel_path)."""
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in _REAL:
        shutil.copy2(SCRIPTS / name, scripts / name)
    (scripts / "run_tests.sh").write_text(_STUB_RUN_TESTS)
    for name in (*_REAL, "run_tests.sh"):
        (scripts / name).chmod(0o755)

    (repo / "pkg").mkdir()
    (repo / "pkg" / "foo.py").write_text("VALUE = 1\n")
    (repo / "tests" / "pkg").mkdir(parents=True)
    (repo / "tests" / "pkg" / "test_foo.py").write_text("def test_foo():\n    assert True\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "keep.md").write_text("# keep\n")

    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, scripts / "_run_tests_called"


def _run_affected(repo: Path, ref: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / "scripts" / "run-affected.sh"), ref],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_empty_diff_skips_pytest_and_never_runs_full_suite(tmp_path: Path) -> None:
    repo, sentinel = _make_repo(tmp_path)
    # A docs-only change maps to no .py test file -> affected-tests prints nothing.
    (repo / "docs" / "more.md").write_text("# more\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs only")

    start = time.monotonic()
    proc = _run_affected(repo, "HEAD~1")
    elapsed = time.monotonic() - start

    assert proc.returncode == 0, proc.stderr
    assert "skipping" in proc.stdout, proc.stdout
    # The decisive assertion: the canonical runner was NEVER invoked.
    assert not sentinel.exists(), f"full-suite runner was reached: {sentinel.read_text()!r}"
    assert elapsed < 10, f"empty-diff path took {elapsed:.1f}s — suspiciously slow"


def test_mapped_diff_forwards_exactly_the_affected_test(tmp_path: Path) -> None:
    repo, sentinel = _make_repo(tmp_path)
    # Touch the source file -> maps to its existing test file.
    (repo / "pkg" / "foo.py").write_text("VALUE = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change source")

    proc = _run_affected(repo, "HEAD~1")

    assert proc.returncode == 0, proc.stderr
    assert sentinel.exists(), "run_tests.sh was not invoked for a mapped diff"
    forwarded = sentinel.read_text().split()
    assert forwarded == ["tests/pkg/test_foo.py"], forwarded


def test_red_is_held_only_after_reproduced_second_run(tmp_path: Path) -> None:
    repo, sentinel = _make_repo(tmp_path)
    (repo / "scripts" / "run_tests.sh").write_text(
        "#!/usr/bin/env bash\n"
        "echo \"$*\" >> \"$(dirname \"$0\")/_run_tests_called\"\n"
        "exit 7\n"
    )
    (repo / "scripts" / "run_tests.sh").chmod(0o755)
    (repo / "pkg" / "foo.py").write_text("VALUE = 3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change source")

    proc = _run_affected(repo, "HEAD~1")

    assert proc.returncode == 7
    assert "rerunning once" in proc.stdout
    assert sentinel.read_text().splitlines() == [
        "tests/pkg/test_foo.py",
        "tests/pkg/test_foo.py",
    ]
