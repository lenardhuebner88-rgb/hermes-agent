"""Regression tests for scripts/run-affected.sh — the empty-affected-tests trap.

The unsafe one-liner ``scripts/run_tests.sh $(scripts/affected-tests.sh)``
expands to a bare ``run_tests.sh`` when the diff maps no test files, which runs
the WHOLE suite (~44k tests, timeout / EXIT 124). ``run-affected.sh`` must
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
_MAPPING_MODULES = ("affected_test_mapping.py", "symbol_test_narrowing.py")
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
    (repo / "hermes_cli").mkdir()
    (repo / "hermes_cli" / "__init__.py").write_text("")
    for name in _MAPPING_MODULES:
        shutil.copy2(REPO_ROOT / "hermes_cli" / name, repo / "hermes_cli" / name)
    (repo / "config").mkdir()
    (repo / "config" / "affected-test-exceptions.json").write_text(
        '{"schema_version": 1, "exceptions": []}\n'
    )
    (scripts / "run_tests.sh").write_text(_STUB_RUN_TESTS)
    # The real helper checks commit drift against main; the fake repository
    # only needs an executable no-op so the wrapper can reach the behavior tested.
    (scripts / "check-branch-age.sh").write_text("#!/bin/sh\nexit 0\n")
    for name in (*_REAL, "run_tests.sh", "check-branch-age.sh"):
        (scripts / name).chmod(0o755)

    (repo / "pkg").mkdir()
    (repo / "pkg" / "foo.py").write_text("VALUE = 1\n")
    (repo / "obsolete.py").write_text("VALUE = 1\n")
    (repo / "tests" / "pkg").mkdir(parents=True)
    (repo / "tests" / "pkg" / "test_foo.py").write_text(
        "from pkg import foo\n\n"
        "def test_foo():\n"
        "    assert foo.VALUE == 1\n"
    )
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


def _install_sequenced_runner(
    repo: Path,
    *,
    first_output: str,
    first_exit: int,
    second_exit: int,
) -> None:
    runner = repo / "scripts" / "run_tests.sh"
    runner.write_text(
        "#!/usr/bin/env bash\n"
        'sentinel="$(dirname "$0")/_run_tests_called"\n'
        'echo "$*" >> "$sentinel"\n'
        'call_count=$(wc -l < "$sentinel")\n'
        'if [ "$call_count" -eq 1 ]; then\n'
        "cat <<'RUN_AFFECTED_FIRST_OUTPUT'\n"
        f"{first_output.rstrip()}\n"
        "RUN_AFFECTED_FIRST_OUTPUT\n"
        f"exit {first_exit}\n"
        "fi\n"
        f"exit {second_exit}\n"
    )
    runner.chmod(0o755)


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


def test_unmapped_production_change_exits_four_without_running_pytest(
    tmp_path: Path,
) -> None:
    repo, sentinel = _make_repo(tmp_path)
    (repo / "orphan").mkdir()
    (repo / "orphan" / "new_runtime.py").write_text("VALUE = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "unmapped source")

    proc = _run_affected(repo, "HEAD~1")

    assert proc.returncode == 4
    assert "mapping is incomplete" in proc.stderr
    assert "NO test ran" in proc.stderr
    assert not sentinel.exists()


def test_deleted_production_file_selects_surviving_importer(
    tmp_path: Path,
) -> None:
    repo, sentinel = _make_repo(tmp_path)
    (repo / "pkg" / "foo.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "delete source only")

    proc = _run_affected(repo, "HEAD~1")

    assert proc.returncode == 0, proc.stderr
    assert sentinel.read_text().split() == ["tests/pkg/test_foo.py"]


def test_deleted_production_and_its_test_do_not_block_gate(
    tmp_path: Path,
) -> None:
    repo, sentinel = _make_repo(tmp_path)
    (repo / "pkg" / "foo.py").unlink()
    (repo / "tests" / "pkg" / "test_foo.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "delete source and test")

    proc = _run_affected(repo, "HEAD~1")

    assert proc.returncode == 0, proc.stderr
    assert "skipping" in proc.stdout
    assert "unmapped" not in proc.stderr
    assert not sentinel.exists()


def test_red_summary_reruns_only_files_from_the_four_failure_blocks(
    tmp_path: Path,
) -> None:
    repo, sentinel = _make_repo(tmp_path)
    summary = """\
=== Summary: 7 files, 12 tests passed, 5 failed/errors (100% complete) in 8.3s (8 workers) ===

=== ⚠ 1 FLAKY file (failed once, passed on retry — fix these) ===
  tests/flaky/test_green.py
  Durations cached to /tmp/test_durations.json (7 files)

=== Per-file subprocess time distribution ===
  Files:   7
  Top 10 slowest:
      8.30s  tests/slow/test_not_a_failure.py
      7.10s  tests/flaky/test_green.py

=== Failure output ===

--- tests/hermes_cli/test_kanban_db.py ---
FAILED tests/hermes_cli/test_kanban_db.py::test_one

=== 2 files with test failures (3 tests failed) ===
  tests/hermes_cli/test_kanban_db.py  (2 tests failed)
  tests/tools/test_kanban_tools.py  (1 test failed)
=== 2 files with pytest setup/teardown/collection errors (2 errors) ===
  tests/hermes_cli/test_kanban_db.py  (1 error, 3 passed)
  tests/agent/test_foo.py  (1 error, 4 passed)
=== 1 file where all tests passed but pytest exited non-zero (warnings-as-errors, hook failures, etc.) ===
  tests/acp/test_bar.py  (7 passed)
=== 1 file where no tests ran (collection/import error, timeout before collection, etc.) ===
  tests/scripts/test_baz.py
"""
    _install_sequenced_runner(
        repo,
        first_output=summary,
        first_exit=1,
        second_exit=0,
    )
    (repo / "pkg" / "foo.py").write_text("VALUE = 4\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change source")

    proc = _run_affected(repo, "HEAD~1")

    assert proc.returncode == 0, proc.stderr
    assert "tests/flaky/test_green.py" in proc.stdout
    assert sentinel.read_text().splitlines() == [
        "tests/pkg/test_foo.py",
        (
            "tests/hermes_cli/test_kanban_db.py "
            "tests/tools/test_kanban_tools.py "
            "tests/agent/test_foo.py "
            "tests/acp/test_bar.py "
            "tests/scripts/test_baz.py"
        ),
    ]
    assert "rerunning once (5 failed files)" in proc.stdout


def test_runner_without_failure_list_reruns_full_selection(tmp_path: Path) -> None:
    repo, sentinel = _make_repo(tmp_path)
    _install_sequenced_runner(
        repo,
        first_output="Killed",
        first_exit=137,
        second_exit=0,
    )
    (repo / "pkg" / "foo.py").write_text("VALUE = 5\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change source")

    proc = _run_affected(repo, "HEAD~1")

    assert proc.returncode == 0, proc.stderr
    assert sentinel.read_text().splitlines() == [
        "tests/pkg/test_foo.py",
        "tests/pkg/test_foo.py",
    ]
    assert "runner produced no failure list" in proc.stderr
    assert "rerunning once (1 file, full selection)" in proc.stdout


def test_green_run_remains_single_pass(tmp_path: Path) -> None:
    repo, sentinel = _make_repo(tmp_path)
    _install_sequenced_runner(
        repo,
        first_output="=== Summary: 1 file, 1 test passed, 0 failed/errors ===",
        first_exit=0,
        second_exit=9,
    )
    (repo / "pkg" / "foo.py").write_text("VALUE = 6\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change source")

    proc = _run_affected(repo, "HEAD~1")

    assert proc.returncode == 0, proc.stderr
    assert sentinel.read_text().splitlines() == ["tests/pkg/test_foo.py"]


def test_targeted_rerun_exit_code_is_the_wrapper_exit_code(tmp_path: Path) -> None:
    summary = """\
=== 1 file with test failures (1 test failed) ===
  tests/pkg/test_foo.py  (1 test failed)
"""
    for second_exit in (0, 9):
        case_path = tmp_path / f"exit-{second_exit}"
        case_path.mkdir()
        repo, sentinel = _make_repo(case_path)
        _install_sequenced_runner(
            repo,
            first_output=summary,
            first_exit=1,
            second_exit=second_exit,
        )
        (repo / "pkg" / "foo.py").write_text(f"VALUE = {second_exit + 10}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "change source")

        proc = _run_affected(repo, "HEAD~1")

        assert proc.returncode == second_exit, proc.stderr
        assert sentinel.read_text().splitlines() == [
            "tests/pkg/test_foo.py",
            "tests/pkg/test_foo.py",
        ]
