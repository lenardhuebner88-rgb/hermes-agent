"""Tests for scripts/gate_load_stamp.py — load/dilation stamp for run-affected.

Pure observability: the stamp must never change the exit code of run-affected.sh.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
STAMP_SCRIPT = SCRIPTS / "gate_load_stamp.py"
_REAL = (
    "run-affected.sh",
    "affected-tests.sh",
    "affected_tests.py",
    "gate_load_stamp.py",
    # run-affected.sh invokes this before pytest (added 2026-07-31); without
    # it the throwaway fixture aborts with exit 2 and never reaches the runner.
    "check_test_wallclock_expiry.py",
)
_MAPPING_MODULE = REPO_ROOT / "hermes_cli" / "affected_test_mapping.py"


def _mapping_module_closure() -> list[Path]:
    """Every hermes_cli module the mapping entrypoint needs, resolved from imports.

    The throwaway repo below used to copy affected_test_mapping.py alone. That
    silently rots: on 2026-07-28 the mapper gained symbol_test_narrowing and
    affected_test_budget, and these tests went red with ModuleNotFoundError in
    the merged tree although each branch was green on its own. Walking the real
    import graph keeps the fixture correct when the next module arrives.
    """
    pending = [_MAPPING_MODULE]
    seen: dict[str, Path] = {_MAPPING_MODULE.stem: _MAPPING_MODULE}
    while pending:
        tree = ast.parse(pending.pop().read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("hermes_cli."):
                continue
            name = node.module.split(".", 1)[1]
            if name in seen:
                continue
            candidate = REPO_ROOT / "hermes_cli" / f"{name}.py"
            if candidate.is_file():
                seen[name] = candidate
                pending.append(candidate)
    return sorted(seen.values())

# Real-shaped fragment from test_durations.json (repo-relative + absolute junk).
_REAL_DURATION_FRAGMENT = {
    "tests/tools/test_code_execution.py": 12.34,
    "tests/scripts/test_affected_tests.py": 7.4,
    "tests/acp/test_auth.py": 1.685,
    "/tmp/cg_probe_fail.py": 0.684,
    "/tmp/claude-1000/pytest-of-piet/pytest-1768/test_grandchild_leak_is_killed0/probe/test_probe_leaker.py": 13.002,
    "/tmp/pytest-of-piet/pytest-100955/test_grandchild_leak_is_killed0/probe/test_probe_leaker.py": 0.328,
}


def _load_stamp_module():
    spec = importlib.util.spec_from_file_location(
        "gate_load_stamp_under_test",
        STAMP_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gls = _load_stamp_module()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_run_affected_repo(
    tmp_path: Path,
    *,
    test_exit: int = 0,
    stamp_mode: str = "real",
) -> tuple[Path, Path]:
    """Throwaway repo with real run-affected wiring and a stub runner.

    stamp_mode:
      - ``real``: copy the real gate_load_stamp.py
      - ``fail``: stamp script always exits 1 (proves shell || true)
      - ``raise``: stamp script raises before exiting (still exit 1 after traceback)
    """
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in _REAL:
        if name == "gate_load_stamp.py" and stamp_mode != "real":
            continue
        shutil.copy2(SCRIPTS / name, scripts / name)

    if stamp_mode == "fail":
        (scripts / "gate_load_stamp.py").write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('stamp deliberately failed', file=sys.stderr)\n"
            "sys.exit(1)\n"
        )
    elif stamp_mode == "raise":
        (scripts / "gate_load_stamp.py").write_text(
            "#!/usr/bin/env python3\n"
            "raise RuntimeError('stamp boom')\n"
        )

    (repo / "hermes_cli").mkdir()
    (repo / "hermes_cli" / "__init__.py").write_text("")
    for module_path in _mapping_module_closure():
        shutil.copy2(module_path, repo / "hermes_cli" / module_path.name)
    (repo / "config").mkdir()
    (repo / "config" / "affected-test-exceptions.json").write_text(
        '{"schema_version": 1, "exceptions": []}\n'
    )

    (scripts / "check-branch-age.sh").write_text("#!/bin/sh\nexit 0\n")
    (scripts / "run_tests.sh").write_text(
        "#!/usr/bin/env bash\n"
        "echo \"$*\" >> \"$(dirname \"$0\")/_run_tests_called\"\n"
        "echo '=== Summary: stub run ==='\n"
        # Spend a tiny bit of wall time so the stamp actual is non-zero.
        "sleep 0.05\n"
        f"exit {test_exit}\n"
    )
    for name in (
        "run-affected.sh",
        "affected-tests.sh",
        "affected_tests.py",
        "gate_load_stamp.py",
        "check-branch-age.sh",
        "run_tests.sh",
    ):
        (scripts / name).chmod(0o755)

    (repo / "pkg").mkdir()
    (repo / "pkg" / "foo.py").write_text("VALUE = 1\n")
    (repo / "tests" / "pkg").mkdir(parents=True)
    (repo / "tests" / "pkg" / "test_foo.py").write_text(
        "def test_foo():\n    assert True\n"
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "pkg" / "foo.py").write_text("VALUE = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "affected change")
    return repo, scripts / "_run_tests_called"


# --- pure prediction / parsing -------------------------------------------------


def test_predict_serial_filters_absolute_foreign_paths_and_sums_repo_relative(
    tmp_path: Path,
) -> None:
    """Real fragment shape: absolute probe paths must not enter the sum."""
    durations_path = tmp_path / "test_durations.json"
    durations_path.write_text(json.dumps(_REAL_DURATION_FRAGMENT), encoding="utf-8")

    loaded = gls.load_duration_map(durations_path)
    assert loaded is not None
    assert "/tmp/cg_probe_fail.py" not in loaded
    assert "tests/tools/test_code_execution.py" in loaded

    files = [
        "tests/tools/test_code_execution.py",  # 12.34
        "tests/scripts/test_affected_tests.py",  # 7.4
        "tests/does_not_exist_in_cache.py",  # missing
    ]
    predicted, matched, missing = gls.predict_serial(files, loaded)
    assert matched == 2
    assert missing == 1
    assert predicted is not None
    assert abs(predicted - (12.34 + 7.4)) < 1e-9

    # Absolute keys in the *selection* also do not get a hit from the filtered map
    # even if the raw JSON had them — the map is already filtered.
    predicted2, matched2, missing2 = gls.predict_serial(
        ["/tmp/cg_probe_fail.py", "tests/acp/test_auth.py"],
        loaded,
    )
    assert matched2 == 1
    assert missing2 == 1
    assert predicted2 is not None
    assert abs(predicted2 - 1.685) < 1e-9


def test_missing_durations_file_yields_unknown_prediction_without_exception(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "nope.json"
    assert gls.load_duration_map(missing) is None

    predicted, matched, absent = gls.predict_serial(
        ["tests/a.py", "tests/b.py"],
        None,
    )
    assert predicted is None
    assert matched == 0
    assert absent == 2

    line = gls.format_stamp_line(
        file_count=2,
        predicted_serial=None,
        missing_count=2,
        actual_wall=10.0,
        load_before=1.0,
        load_after=2.0,
        cores=12,
        workers=8,
    )
    assert "predicted serial unknown" in line
    assert "dilation n/a" in line
    # main() must not raise / non-zero
    state = tmp_path / "state.json"
    assert gls.main(["start", "--state", str(state), "--durations", str(missing), "--", "tests/a.py"]) == 0
    # stdout of finish names unknown
    proc = subprocess.run(
        [sys.executable, str(STAMP_SCRIPT), "finish", "--state", str(state)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "predicted serial unknown" in proc.stdout


def test_corrupt_json_yields_unknown_prediction_without_exception(
    tmp_path: Path,
) -> None:
    corrupt = tmp_path / "test_durations.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    assert gls.load_duration_map(corrupt) is None

    state = tmp_path / "state.json"
    assert (
        gls.main(
            [
                "start",
                "--state",
                str(state),
                "--durations",
                str(corrupt),
                "--",
                "tests/tools/test_code_execution.py",
            ]
        )
        == 0
    )
    proc = subprocess.run(
        [sys.executable, str(STAMP_SCRIPT), "finish", "--state", str(state)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "predicted serial unknown" in proc.stdout
    assert proc.returncode == 0


def test_files_without_history_are_counted_and_named_in_stamp(tmp_path: Path) -> None:
    durations_path = tmp_path / "test_durations.json"
    durations_path.write_text(
        json.dumps({"tests/known.py": 3.0, "/tmp/foreign.py": 99.0}),
        encoding="utf-8",
    )
    files = ["tests/known.py", "tests/unknown_a.py", "tests/unknown_b.py"]
    predicted, matched, missing = gls.predict_serial(
        files, gls.load_duration_map(durations_path)
    )
    assert predicted == 3.0
    assert matched == 1
    assert missing == 2

    line = gls.format_stamp_line(
        file_count=3,
        predicted_serial=predicted,
        missing_count=missing,
        actual_wall=1.5,
        load_before=4.0,
        load_after=5.0,
        cores=12,
        workers=8,
    )
    assert "3 files" in line
    assert "2 no-hist" in line
    assert "predicted serial" in line


def test_parse_loadavg_against_real_content_format() -> None:
    """Field 1 of the live /proc/loadavg shape."""
    sample = "8.01 13.60 13.82 7/2204 3449717\n"
    assert gls.parse_loadavg_1min(sample) == pytest.approx(8.01)
    assert gls.parse_loadavg_1min("0.00 0.01 0.05 1/100 1") == pytest.approx(0.0)
    assert gls.parse_loadavg_1min("") is None
    assert gls.parse_loadavg_1min("not-a-load") is None

    # Live file when present (Linux CI/homeserver)
    live = Path("/proc/loadavg")
    if live.is_file():
        value = gls.read_load_1min(live)
        assert value is not None
        assert value >= 0.0


def test_dilation_is_wall_over_serial_prediction() -> None:
    line = gls.format_stamp_line(
        file_count=10,
        predicted_serial=100.0,
        missing_count=0,
        actual_wall=40.0,
        load_before=8.0,
        load_after=12.0,
        cores=12,
        workers=8,
    )
    assert "dilation 0.40x (wall/serial-pred; parallel×8)" in line
    assert "load before 8.0 after 12.0 / 12 cores" in line
    assert "0.67→1.00 per core" in line


# --- shell integration / rule 1 -----------------------------------------------


def test_stamp_failure_does_not_change_run_affected_exit_code(tmp_path: Path) -> None:
    """Rule 1: stamp exits non-zero → run-affected still returns the runner code."""
    repo, sentinel = _make_run_affected_repo(
        tmp_path, test_exit=7, stamp_mode="fail"
    )

    proc = subprocess.run(
        [str(repo / "scripts" / "run-affected.sh"), "HEAD~1"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 7, (
        f"expected runner exit 7 unchanged, got {proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert sentinel.exists(), "runner must still have been invoked"
    # Stamp complained, but must not have aborted the gate
    assert "stamp deliberately failed" in proc.stderr


def test_stamp_raise_does_not_change_run_affected_exit_code(tmp_path: Path) -> None:
    """Rule 1 variant: uncaught exception in stamp script (exit 1 via traceback)."""
    repo, sentinel = _make_run_affected_repo(
        tmp_path, test_exit=9, stamp_mode="raise"
    )

    proc = subprocess.run(
        [str(repo / "scripts" / "run-affected.sh"), "HEAD~1"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 9, (
        f"expected runner exit 9 unchanged, got {proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert sentinel.exists()


def test_run_affected_emits_stamp_line_and_preserves_green_exit(tmp_path: Path) -> None:
    repo, sentinel = _make_run_affected_repo(
        tmp_path, test_exit=0, stamp_mode="real"
    )
    # Provide a durations cache so the stamp has a real prediction field
    (repo / "test_durations.json").write_text(
        json.dumps({"tests/pkg/test_foo.py": 1.25}),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [str(repo / "scripts" / "run-affected.sh"), "HEAD~1"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert sentinel.exists()
    assert "run-affected:" in proc.stdout
    assert "predicted serial" in proc.stdout
    assert "actual wall" in proc.stdout
    assert "dilation" in proc.stdout
    assert "load before" in proc.stdout
    assert "wall/serial-pred" in proc.stdout


def test_main_never_exits_nonzero_on_internal_errors(tmp_path: Path) -> None:
    """The Python entry point itself always returns 0 (belt under shell || true)."""
    # Completely broken argv
    assert gls.main([]) == 0
    assert gls.main(["finish"]) == 0  # missing --state
    # finish with missing state file still prints a degraded line
    missing_state = tmp_path / "absent.json"
    proc = subprocess.run(
        [sys.executable, str(STAMP_SCRIPT), "finish", "--state", str(missing_state)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "run-affected:" in proc.stdout


def test_start_finish_roundtrip_wall_clock(tmp_path: Path) -> None:
    durations = tmp_path / "d.json"
    durations.write_text(json.dumps(_REAL_DURATION_FRAGMENT), encoding="utf-8")
    state = tmp_path / "state.json"

    assert (
        gls.main(
            [
                "start",
                "--state",
                str(state),
                "--durations",
                str(durations),
                "--",
                "tests/tools/test_code_execution.py",
                "tests/no_hist.py",
            ]
        )
        == 0
    )
    time.sleep(0.08)
    proc = subprocess.run(
        [sys.executable, str(STAMP_SCRIPT), "finish", "--state", str(state)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    line = proc.stdout.strip()
    assert "1 no-hist" in line
    assert "2 files" in line
    # wall should be at least the sleep
    assert "actual wall" in line


# ---------------------------------------------------------------------------
# Pure-helper boundaries
# ---------------------------------------------------------------------------

def test_filter_duration_map_keeps_zero_second_entries() -> None:
    """A measured 0.0s run is HISTORY, not junk — dropping the boundary
    would make fast files 'no-hist' forever and bloat the prediction."""
    assert gls.filter_duration_map({"tests/a.py": 0}) == {"tests/a.py": 0.0}
    assert gls.filter_duration_map({"tests/a.py": -1}) == {}


def test_predict_serial_empty_selection_is_unknown_without_cache() -> None:
    """No files + no cache must predict None (unknown), not 0.0 — a zero
    prediction would print a bogus 'zero serial-pred' dilation line."""
    assert gls.predict_serial([], None) == (None, 0, 0)
    assert gls.predict_serial([], {}) == (0.0, 0, 0)


def test_workers_default_never_returns_zero(monkeypatch) -> None:
    """0 workers would deadlock the runner — the default must kick in for
    every non-positive value, zero included."""
    monkeypatch.setenv("HERMES_TEST_WORKERS", "0")
    assert gls.workers_default() == 8


def test_fmt_seconds_boundary_formats() -> None:
    """The format steps down exactly AT 100 and 10 — the boundary values
    keep the coarser format."""
    assert gls._fmt_seconds(100.0) == "100s"
    assert gls._fmt_seconds(99.9) == "99.9s"
    assert gls._fmt_seconds(10.0) == "10.0s"
    assert gls._fmt_seconds(9.99) == "9.99s"


def test_zero_serial_prediction_does_not_divide() -> None:
    """A zero serial prediction must render the 'zero serial-pred' branch,
    never a wall/0 division — the stamp is observability and may not
    crash the gate."""
    line = gls.format_stamp_line(
        file_count=1,
        predicted_serial=0.0,
        missing_count=0,
        actual_wall=5.0,
        load_before=1.0,
        load_after=1.0,
        cores=4,
        workers=8,
    )
    assert "zero serial-pred" in line


# ---------------------------------------------------------------------------
# Second pass: guards and fallbacks
# ---------------------------------------------------------------------------

def test_repo_relative_test_key_rejects_empty_and_absolute():
    mod = _load_stamp_module()
    assert mod.is_repo_relative_test_key("") is False
    assert mod.is_repo_relative_test_key("/abs/tests/x.py") is False
    assert mod.is_repo_relative_test_key("tests/x.py") is True


def test_cpu_count_is_none_for_non_positive(monkeypatch):
    """A non-positive cpu count (0 or negative) is 'unknown' (None) —
    never 0 or a negative core count in the stamp."""
    mod = _load_stamp_module()
    monkeypatch.setattr(mod.os, "cpu_count", lambda: 0)
    assert mod.cpu_count() is None
    monkeypatch.setattr(mod.os, "cpu_count", lambda: -1)
    assert mod.cpu_count() is None


def test_stamp_line_unknown_prediction_with_wall_is_plain_na():
    """predicted=None with a measured wall reads as the plain
    'dilation n/a (parallel×N)' — NOT the zero-serial-pred variant,
    which is reserved for an actual 0.0 prediction."""
    mod = _load_stamp_module()
    line = mod.format_stamp_line(
        file_count=1,
        predicted_serial=None,
        missing_count=0,
        actual_wall=5.0,
        load_before=1.0,
        load_after=1.0,
        cores=4,
        workers=8,
    )
    assert "dilation n/a (parallel×8)" in line
    assert "zero serial-pred" not in line


def test_cmd_finish_falls_back_to_files_list_length(tmp_path, capsys):
    """A state without file_count derives the count from the files list —
    int(None) must never crash the finish stamp."""
    import types

    mod = _load_stamp_module()
    state = {"files": ["tests/a.py", "tests/b.py"], "t0": time.monotonic() - 3.0}
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")

    rc = mod.cmd_finish(types.SimpleNamespace(state=path, durations=None))

    assert rc == 0
    assert "2 files" in capsys.readouterr().out
