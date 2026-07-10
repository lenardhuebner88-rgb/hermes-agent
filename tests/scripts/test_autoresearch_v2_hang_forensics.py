"""Hang forensics for the autoresearch-v2 nightly sweep.

8 of 17 nights the sweep hung inside a lane's blocking call with ZERO
journal output and was killed by the unit's start timeout at exactly
40:00 — the wall-clock budget is only checked BETWEEN lane steps, and
stdout was block-buffered so even pre-hang prints died with the SIGKILL.

Pinned here: the watchdog thread self-aborts (with a stack dump) at
1.5x the configured budget, and does nothing when no budget is set.
"""

from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture()
def mod():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "autoresearch_v2_nightly.py"
    spec = importlib.util.spec_from_file_location("ar_v2_nightly_under_test", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_main_actually_installs_hang_forensics(mod):
    """Integration guard: main() must WIRE IN the forensics, not just define
    it. The unit tests called _install_hang_forensics directly and stayed
    green while main() never invoked it — the fix was dead code until this
    test forced the call site to exist."""
    from unittest.mock import patch

    called = {}

    def _spy(started, budget_seconds, **k):
        called["started"] = started
        called["budget"] = budget_seconds

    # Run only the arg-parse + forensics-install prologue; make the first
    # lane selection raise so we don't drive real lanes / Discord.
    with (
        patch.object(mod, "_install_hang_forensics", side_effect=_spy),
        patch.object(mod, "select_subsystem", side_effect=RuntimeError("stop here")),
        patch.object(mod, "post_summary"),
    ):
        try:
            mod.main(["--no-send", "--lanes", "deep-audit"])
        except RuntimeError:
            pass

    assert "started" in called, "main() never called _install_hang_forensics"
    assert called["budget"] is not None


def test_watchdog_aborts_past_deadline(mod):
    fired = threading.Event()
    codes: list[int] = []

    def _fake_exit(code):
        codes.append(code)
        fired.set()
        raise SystemExit(code)  # ends the watchdog thread loop

    # started far enough in the past that 1.5x budget is already exceeded.
    mod._install_hang_forensics(
        time.monotonic() - 100.0,
        1.0,
        _exit=_fake_exit,
        poll_seconds=0.01,
    )

    assert fired.wait(timeout=5.0), "watchdog never fired past its deadline"
    assert codes == [mod._WATCHDOG_EXIT_CODE]


def test_watchdog_not_started_without_budget(mod):
    before = {t.name for t in threading.enumerate()}
    mod._install_hang_forensics(time.monotonic(), 0.0, poll_seconds=0.01)
    after = {
        t.name for t in threading.enumerate()
        if t.name == "ar-v2-watchdog" and t.name not in before
    }
    assert not after


def test_watchdog_quiet_within_budget(mod):
    fired = threading.Event()

    def _fake_exit(code):
        fired.set()
        raise SystemExit(code)

    mod._install_hang_forensics(
        time.monotonic(),
        3600.0,  # deadline far away
        _exit=_fake_exit,
        poll_seconds=0.01,
    )
    assert not fired.wait(timeout=0.3)
