"""Tests for ``hermes_cli.gate_leaker`` — the green-gate leaker/cause-purity step.

These cover the *pure* logic only (parse failing files from a gate log, then
classify an isolation rerun via an INJECTED runner). The real subprocess runner
is a thin shell exercised by the CLI; the classification rules — what counts as a
leaker, which failure becomes ``first_fail``, when a night is purely harness
noise (``leaker_only``), and that the isolation rerun is bounded — live here.
"""

from __future__ import annotations

from hermes_cli import gate_leaker as gl


# ---------------------------------------------------------------------------
# parse_failed_files — pytest (run_tests_parallel.py summary format)
# ---------------------------------------------------------------------------

PY_LOG = """\
=== Summary: 850 files, 17000 tests passed, 3 failed (100% complete) in 312.4s ===
  Durations cached to test_durations.json (850 files)

  Top 10 slowest:
   120.11s  tests/agent/test_run_agent.py
    56.02s  tests/plugins/test_kanban_dashboard_plugin.py

=== Failure output ===

--- tests/foo/test_alpha.py ---
FAILED tests/foo/test_alpha.py::test_one - assert 1 == 2
  Repro: python -m pytest tests/foo/test_alpha.py

=== 2 files with test failures (3 tests failed) ===
  tests/foo/test_alpha.py  (2 tests failed)
  tests/bar/test_beta.py  (1 test failed)
=== 1 file where no tests ran (collection/import error, timeout before collection, etc.) ===
  tests/baz/test_gamma.py
"""


def test_parse_python_failed_files_from_summary_sections():
    files = gl.parse_failed_files("python", PY_LOG)
    assert files == [
        "tests/foo/test_alpha.py",
        "tests/bar/test_beta.py",
        "tests/baz/test_gamma.py",
    ]


def test_parse_python_ignores_slowest_and_traceback_paths():
    # the Top-10-slowest block and the raw "Failure output" dump both mention
    # tests/*.py paths that are NOT the canonical failing-file list — only the
    # explicit summary sections (and Repro lines) count.
    files = gl.parse_failed_files("python", PY_LOG)
    assert "tests/agent/test_run_agent.py" not in files
    assert "tests/plugins/test_kanban_dashboard_plugin.py" not in files


def test_parse_python_includes_passed_but_nonzero_files():
    log = (
        "=== 1 file where all tests passed but pytest exited non-zero "
        "(warnings-as-errors, hook failures, etc.) ===\n"
        "  tests/x/test_warn.py  (5 passed)\n"
    )
    assert gl.parse_failed_files("python", log) == ["tests/x/test_warn.py"]


def test_parse_python_repro_fallback_when_no_summary():
    log = "  Repro: python -m pytest tests/only/test_repro.py --tb=long\n"
    assert gl.parse_failed_files("python", log) == ["tests/only/test_repro.py"]


# ---------------------------------------------------------------------------
# parse_failed_files — vitest
# ---------------------------------------------------------------------------

def test_parse_vitest_fail_lines():
    log = (
        " FAIL  src/control/Foo.test.tsx > Foo > renders\n"
        " FAIL  src/control/Bar.test.ts > Bar > does thing\n"
        " ✓ src/control/Ok.test.ts (3)\n"
    )
    assert gl.parse_failed_files("vitest", log) == [
        "src/control/Foo.test.tsx",
        "src/control/Bar.test.ts",
    ]


def test_parse_vitest_dedupes_repeated_fail_lines():
    log = (
        " FAIL  src/a.test.ts > one\n"
        " FAIL  src/a.test.ts > two\n"
    )
    assert gl.parse_failed_files("vitest", log) == ["src/a.test.ts"]


def test_parse_non_isolatable_gate_returns_empty():
    # tsc / build are whole-project compiles, not per-file test runners — there
    # is nothing to isolate, so no failing "files" are extracted.
    assert gl.parse_failed_files("tsc", "error TS2322: ...") == []
    assert gl.parse_failed_files("build", "build failed") == []


# ---------------------------------------------------------------------------
# isolate_failures — classification via an injected runner
# ---------------------------------------------------------------------------

def _runner(passing: set[str]):
    """Build a fake run_one: files in ``passing`` pass alone (=> leaker)."""
    def run_one(f):
        if f in passing:
            return (True, f"isolated {f}: 1 passed")
        return (False, f"isolated {f}: FAILED assert")
    return run_one


def test_leaker_is_demoted_and_first_fail_is_first_reproducible():
    files = ["tests/a.py", "tests/b.py", "tests/c.py"]
    # a passes alone (leaker); b and c reproduce
    res = gl.isolate_failures(
        "python", files, run_one=_runner({"tests/a.py"}),
        max_files=10, max_seconds=600,
    )
    assert res["leakers"] == ["tests/a.py"]
    assert {r["file"] for r in res["reproduced"]} == {"tests/b.py", "tests/c.py"}
    # first_fail skips the leaker and names the first reproducible file in order
    assert res["first_fail"]["gate"] == "python"
    assert res["first_fail"]["file"] == "tests/b.py"
    assert "FAILED" in res["first_fail"]["detail"]
    assert res["leaker_only"] is False


def test_all_leakers_marks_leaker_only_and_no_first_fail():
    files = ["tests/a.py", "tests/b.py"]
    res = gl.isolate_failures(
        "python", files, run_one=_runner({"tests/a.py", "tests/b.py"}),
        max_files=10, max_seconds=600,
    )
    assert res["leakers"] == files
    assert res["reproduced"] == []
    assert res["first_fail"] is None
    assert res["leaker_only"] is True


def test_first_fail_keeps_first_file_when_nothing_passes_alone():
    files = ["tests/a.py", "tests/b.py"]
    res = gl.isolate_failures(
        "python", files, run_one=_runner(set()),
        max_files=10, max_seconds=600,
    )
    assert res["leakers"] == []
    assert res["first_fail"]["file"] == "tests/a.py"
    assert res["leaker_only"] is False


def test_isolation_is_bounded_by_max_files():
    files = [f"tests/t{i}.py" for i in range(20)]
    calls = []

    def run_one(f):
        calls.append(f)
        return (True, "passed alone")  # everything would be a leaker if checked

    res = gl.isolate_failures(
        "python", files, run_one=run_one, max_files=5, max_seconds=600,
    )
    assert len(calls) == 5
    assert len(res["checked"]) == 5
    assert len(res["unchecked"]) == 15
    assert res["capped"] is True
    # a capped run can NEVER claim leaker_only (we did not verify every file)
    assert res["leaker_only"] is False
    # an unchecked file must NOT be demoted: the first unchecked file (the cap
    # cut off file index 5) is still a first_fail candidate so a real regression
    # behind the cap is never hidden.
    assert res["first_fail"] is not None


def test_isolation_is_bounded_by_time_budget():
    files = [f"tests/t{i}.py" for i in range(10)]
    calls = []
    clock = {"t": 0.0}

    def fake_monotonic():
        return clock["t"]

    def run_one(f):
        calls.append(f)
        clock["t"] += 100.0  # each rerun "takes" 100s
        return (True, "passed alone")

    res = gl.isolate_failures(
        "python", files, run_one=run_one,
        max_files=100, max_seconds=250, monotonic=fake_monotonic,
    )
    # budget 250s, 100s per file -> 3 reruns then the 4th sees elapsed>=250
    assert len(calls) == 3
    assert res["capped"] is True
    assert res["leaker_only"] is False


def test_capped_with_a_confirmed_leaker_still_not_leaker_only():
    # AC-2 guardrail: even if every CHECKED file is a leaker, an unchecked tail
    # means we cannot dismiss the whole night as harness noise.
    files = ["tests/a.py", "tests/b.py", "tests/c.py"]
    res = gl.isolate_failures(
        "python", files, run_one=_runner({"tests/a.py", "tests/b.py"}),
        max_files=2, max_seconds=600,
    )
    assert res["leakers"] == ["tests/a.py", "tests/b.py"]
    assert res["capped"] is True
    assert res["leaker_only"] is False
    # the unchecked file is the surviving first_fail candidate
    assert res["first_fail"]["file"] == "tests/c.py"


# ---------------------------------------------------------------------------
# isolate_from_logs — cross-gate orchestration via injected runner factory
# ---------------------------------------------------------------------------

def _factory(passing: set[str]):
    """runner_factory: same fake runner for every gate."""
    return lambda gate: _runner(passing)


def _py_log(*files):
    body = "\n".join(f"  {f}  (1 test failed)" for f in files)
    return f"=== {len(files)} files with test failures (1 tests failed) ===\n{body}\n"


def _vitest_log(*files):
    return "\n".join(f" FAIL  {f} > suite > t" for f in files) + "\n"


def test_cross_gate_first_fail_moves_past_all_leaker_python_to_vitest():
    # python's failures are all leakers; vitest reproduces -> the cause must move
    # to vitest, and the night is NOT leaker_only.
    gate_logs = [
        ("python", _py_log("tests/a.py", "tests/b.py")),
        ("vitest", _vitest_log("src/c.test.ts")),
    ]
    res = gl.isolate_from_logs(
        gate_logs,
        runner_factory=_factory({"tests/a.py", "tests/b.py"}),  # src/c reproduces
    )
    assert res["first_fail"]["gate"] == "vitest"
    assert res["first_fail"]["file"] == "src/c.test.ts"
    assert res["leaker_only"] is False
    assert res["leakers"] == ["python: tests/a.py", "python: tests/b.py"]


def test_cross_gate_python_reproducing_wins_over_vitest():
    gate_logs = [
        ("vitest", _vitest_log("src/c.test.ts")),
        ("python", _py_log("tests/a.py")),  # given out of canonical order
    ]
    res = gl.isolate_from_logs(gate_logs, runner_factory=_factory(set()))
    # python is first in canonical order and reproduces -> it wins
    assert res["first_fail"]["gate"] == "python"
    assert res["first_fail"]["file"] == "tests/a.py"


def test_cross_gate_all_leakers_is_leaker_only():
    gate_logs = [
        ("python", _py_log("tests/a.py")),
        ("vitest", _vitest_log("src/c.test.ts")),
    ]
    res = gl.isolate_from_logs(
        gate_logs,
        runner_factory=_factory({"tests/a.py", "src/c.test.ts"}),
    )
    assert res["leaker_only"] is True
    assert res["first_fail"] is None
    assert set(res["leakers"]) == {"python: tests/a.py", "vitest: src/c.test.ts"}


def test_non_isolatable_gate_is_deterministic_not_leaker():
    gate_logs = [("build", "Frontend vite build:\nerror TS2322 boom\n")]
    # runner_factory must never be called for a non-isolatable gate
    def boom_factory(gate):
        raise AssertionError(f"should not build a runner for {gate}")

    res = gl.isolate_from_logs(gate_logs, runner_factory=boom_factory)
    assert res["leaker_only"] is False
    assert res["first_fail"]["gate"] == "build"
    assert res["first_fail"]["file"] is None
    assert "TS2322" in res["first_fail"]["detail"]


def test_unparseable_isolatable_log_is_not_demoted():
    # a red python gate whose file list can't be parsed must NOT be silently
    # cleared — it stays a deterministic cause.
    gate_logs = [("python", "run_tests.sh crashed before summary\n")]

    def boom_factory(gate):
        raise AssertionError("no files parsed -> no rerun expected")

    res = gl.isolate_from_logs(gate_logs, runner_factory=boom_factory)
    assert res["leaker_only"] is False
    assert res["first_fail"]["gate"] == "python"


def test_format_first_fail_detail_with_and_without_file():
    assert gl.format_first_fail_detail(None) == ""
    with_file = gl.format_first_fail_detail(
        {"gate": "python", "file": "tests/a.py", "detail": "FAILED assert"}
    )
    assert with_file == "python (isolated): tests/a.py\nFAILED assert"
    without = gl.format_first_fail_detail(
        {"gate": "build", "file": None, "detail": "tsc boom"}
    )
    assert without == "tsc boom"
