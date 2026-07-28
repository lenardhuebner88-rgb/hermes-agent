"""Hard process boundary for Autoresearch lane wall-clock budgets."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class BoundedProcessResult:
    """Observed outcome of one independently supervised lane process."""

    returncode: int | None
    timed_out: bool
    elapsed_seconds: float


def _signal_process(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, sig)
        else:  # pragma: no cover - Hermes production is Linux
            proc.send_signal(sig)
    except ProcessLookupError:
        pass


def run_bounded_process(
    command: Sequence[str],
    *,
    budget_seconds: float,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    terminate_grace_seconds: float = 2.0,
) -> BoundedProcessResult:
    """Run ``command`` in its own process group and enforce a hard deadline.

    The parent, not cooperative lane code, owns the clock. On expiry the whole
    group receives SIGTERM so nested test commands stop too; SIGKILL follows
    after a short cleanup grace if the worker remains stuck.
    """
    started = time.monotonic()
    proc = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        start_new_session=(os.name == "posix"),
    )
    timeout = budget_seconds if budget_seconds > 0 else None
    try:
        returncode = proc.wait(timeout=timeout)
        return BoundedProcessResult(
            returncode=returncode,
            timed_out=False,
            elapsed_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired:
        _signal_process(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=max(0.0, terminate_grace_seconds))
        except subprocess.TimeoutExpired:
            _signal_process(proc, signal.SIGKILL)
            proc.wait()
        return BoundedProcessResult(
            returncode=proc.returncode,
            timed_out=True,
            elapsed_seconds=time.monotonic() - started,
        )
