from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from hermes_cli.autoresearch_lane_runner import run_bounded_process


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="process-group supervision is a POSIX contract",
)


def _assert_process_not_running(pid: int, *, timeout_seconds: float = 2.0) -> None:
    """Accept a reaped process or a terminated process awaiting reaping."""
    proc_stat = Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            stat = proc_stat.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        state = stat[stat.rfind(")") + 2]
        if state == "Z":
            return
        time.sleep(0.01)
    pytest.fail(f"process {pid} is still running")


def test_command_finishing_under_budget_returns_its_status(tmp_path: Path) -> None:
    result = run_bounded_process(
        [sys.executable, "-c", "raise SystemExit(7)"],
        budget_seconds=0.8,
        cwd=tmp_path,
    )

    assert result.returncode == 7
    assert result.timed_out is False
    assert 0 < result.elapsed_seconds < 0.6


def test_command_exceeding_budget_is_terminated(tmp_path: Path) -> None:
    pid_file = tmp_path / "direct.pid"
    code = (
        "import os, time\n"
        "from pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )

    result = run_bounded_process(
        [sys.executable, "-c", code],
        budget_seconds=0.4,
        cwd=tmp_path,
        terminate_grace_seconds=0.1,
    )

    assert result.timed_out is True
    assert result.returncode == -signal.SIGTERM
    _assert_process_not_running(int(pid_file.read_text(encoding="utf-8")))


def test_timeout_terminates_nested_process_in_same_group(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        f"Path({str(child_pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(30)\n"
    )

    result = run_bounded_process(
        [sys.executable, "-c", parent_code],
        budget_seconds=0.4,
        cwd=tmp_path,
        terminate_grace_seconds=0.1,
    )

    assert result.timed_out is True
    _assert_process_not_running(int(child_pid_file.read_text(encoding="utf-8")))


def test_sigterm_ignoring_command_is_killed_after_grace(tmp_path: Path) -> None:
    pid_file = tmp_path / "stubborn.pid"
    code = (
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )

    result = run_bounded_process(
        [sys.executable, "-c", code],
        budget_seconds=0.4,
        cwd=tmp_path,
        terminate_grace_seconds=0.1,
    )

    assert result.timed_out is True
    assert result.returncode == -signal.SIGKILL
    assert 0.4 <= result.elapsed_seconds < 1.5
    _assert_process_not_running(int(pid_file.read_text(encoding="utf-8")))


@pytest.mark.parametrize("budget_seconds", [0.0, -0.1])
def test_non_positive_budget_has_no_deadline(
    tmp_path: Path,
    budget_seconds: float,
) -> None:
    result = run_bounded_process(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(0.1); raise SystemExit(3)",
        ],
        budget_seconds=budget_seconds,
        cwd=tmp_path,
    )

    assert result.returncode == 3
    assert result.timed_out is False
    assert 0.08 <= result.elapsed_seconds < 1.0
