"""Regression coverage for pytest ERROR accounting in the parallel runner."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_runner_counts_fixture_errors_as_red_tests(tmp_path: Path) -> None:
    probe = tmp_path / "test_fixture_error_probe.py"
    probe.write_text(
        "import pytest\n\n"
        "@pytest.fixture\n"
        "def broken_fixture():\n"
        "    raise RuntimeError('simulated fixture setup error')\n\n"
        "def test_passes():\n"
        "    assert True\n\n"
        "def test_errors_one(broken_fixture):\n"
        "    pass\n\n"
        "def test_errors_two(broken_fixture):\n"
        "    pass\n",
        encoding="utf-8",
    )

    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_tests_parallel.py"),
            "--files",
            str(probe),
            "--file-retries",
            "0",
            "-j",
            "1",
            "-q",
        ],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 1, proc.stdout
    assert "1 tests passed, 2 failed/errors" in proc.stdout
    assert "with pytest setup/teardown/collection errors (2 errors)" in proc.stdout
    assert "where all tests passed but pytest exited non-zero" not in proc.stdout
