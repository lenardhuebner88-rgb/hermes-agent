"""Verify that the canonical test runner isolates HERMES_HOME per file.

This prevents test logging (e.g. ``logging.exception("boom")`` from a
gateway test lambda) from polluting the live ``~/.hermes/logs/errors.log``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_runner_does_not_pollute_live_errors_log(tmp_path: Path) -> None:
    """Running a test through ``run_tests_parallel.py`` must not touch live logs.

    The fixture simulates the real contamination path described in the bug:

      * a ``~/.hermes`` directory is prepared as the "live" home
      * it contains a pre-existing ``logs/errors.log`` with known bytes
      * a child test file is run through ``scripts/run_tests_parallel.py``
      * that child calls ``hermes_logging.setup_logging()`` and logs an
        exception exactly as ``tests/gateway/test_platform_registry.py``
        did when it polluted the live log.

    With the fix, ``run_tests_parallel.py`` injects ``HERMES_HOME`` pointing
    at a per-run temp directory, so the live ``errors.log`` remains
    byte-identical before and after the run.
    """
    # "Live" home: this is what would be ~/.hermes if HERMES_HOME is not set.
    live_home_parent = tmp_path / "live_home"
    live_home_parent.mkdir()
    live_hermes = live_home_parent / ".hermes"
    live_logs = live_hermes / "logs"
    live_logs.mkdir(parents=True)
    live_errors = live_logs / "errors.log"
    marker = "=== pre-existing live errors.log contents ===\n"
    live_errors.write_text(marker)

    # Child test file that reproduces the real contamination path.
    child = tmp_path / "log_spam_child.py"
    child.write_text(
        textwrap.dedent(
            """
            import logging
            import hermes_logging

            def test_logs_to_errors_log():
                hermes_logging.setup_logging()
                try:
                    raise RuntimeError("boom")
                except Exception:
                    logging.exception("gateway lambda exploded")
            """
        )
    )

    # Environment for the runner subprocess: HOME points at the fake live
    # home, HERMES_HOME is *not* set (simulates the old runner), and
    # PYTHONPATH lets the child import hermes_logging.
    env: dict[str, str] = {
        k: v
        for k, v in os.environ.items()
        if k not in {"HERMES_HOME", "HERMES_KANBAN_DB", "PYTHONPATH"}
    }
    env["HOME"] = str(live_home_parent)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PATH"] = os.environ.get("PATH", "")

    before = live_errors.read_bytes()

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "run_tests_parallel.py"),
            str(child),
            "-q",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    after = live_errors.read_bytes()

    # We expect the runner to succeed and the live log to be untouched.
    assert result.returncode == 0, result.stdout + result.stderr
    assert after == before, (
        "Live errors.log was modified by a test run; HERMES_HOME isolation failed.\n"
        f"Added bytes: {after[len(before):]!r}"
    )


def test_runner_injects_hermes_home_into_child(tmp_path: Path) -> None:
    """``run_tests_parallel.py`` must set HERMES_HOME in each child process."""
    child = tmp_path / "hermes_home_probe.py"
    child.write_text(
        textwrap.dedent(
            """
            import os

            def test_hermes_home_is_set():
                assert os.environ.get("HERMES_HOME"), "HERMES_HOME was not injected"
            """
        )
    )

    env: dict[str, str] = {
        k: v
        for k, v in os.environ.items()
        if k != "HERMES_HOME"
    }
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PATH"] = os.environ.get("PATH", "")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "run_tests_parallel.py"),
            str(child),
            "-q",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
