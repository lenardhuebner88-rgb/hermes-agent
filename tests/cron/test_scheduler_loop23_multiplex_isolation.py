"""Audit loop 3 (A-H3): multiplex ticker — one broken profile must not block
or blame the others.

Before the fix, ``InProcessCronScheduler._start_multiplex`` wrapped the WHOLE
per-tick profile loop in a single ``except BaseException``: an exception in
profile A aborted the tick for profiles B..N, and the resulting
``_tick_error`` / ``success=False`` heartbeat was recorded against ALL
profiles (wrong status attribution in `hermes cron status`).

The fix isolates each profile's tick in its own ``except Exception``: the
failure is recorded against that profile only and the remaining profiles tick
normally. BaseException (KeyboardInterrupt/SystemExit) is deliberately NOT
caught per profile — it propagates to the outer safety net, which keeps the
ticker alive and attributes the cycle failure to every profile that did not
record its own outcome (preserving the previous whole-cycle semantics for
process-global exceptions).

Hermetic: cron_tick and the ticker marker functions are patched; profile homes
are tmp dirs.
"""

import threading
import time
from pathlib import Path

import pytest

import cron.jobs as jobs_mod
from cron.scheduler_provider import InProcessCronScheduler


def _wait_until(predicate, timeout=10.0, interval=0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


@pytest.fixture
def multiplex_env(tmp_path, monkeypatch):
    """Three tmp profile homes + patched tick/markers recording per-profile.

    Returns a namespace dict with the homes, the recorded calls, and a
    ``fail_profiles`` set the fake tick raises for (matched by active
    HERMES_HOME override).
    """
    from hermes_constants import get_hermes_home

    homes = {name: tmp_path / name for name in ("alpha", "beta", "gamma")}
    for home in homes.values():
        home.mkdir()

    state = {
        "homes": homes,
        "tick_calls": [],          # profile names in the order ticked
        "heartbeats": [],          # (profile, success)
        "errors": [],              # (profile, error_string)
        "clears": [],              # profile names
        "fail_profiles": set(),    # names whose tick raises RuntimeError
        "raise_base": {},          # name -> BaseException instance (raised once)
    }

    def fake_tick(*args, **kwargs):
        active = str(get_hermes_home())
        name = next(n for n, h in homes.items() if str(h) == active)
        state["tick_calls"].append(name)
        if name in state["fail_profiles"]:
            raise RuntimeError(f"profile {name} store is corrupted")
        base = state["raise_base"].pop(name, None)
        if base is not None:
            raise base
        return 0

    def fake_heartbeat(success=True):
        active = str(get_hermes_home())
        name = next(n for n, h in homes.items() if str(h) == active)
        state["heartbeats"].append((name, success))

    def fake_error(msg):
        active = str(get_hermes_home())
        name = next(n for n, h in homes.items() if str(h) == active)
        state["errors"].append((name, msg))

    def fake_clear():
        active = str(get_hermes_home())
        name = next(n for n, h in homes.items() if str(h) == active)
        state["clears"].append(name)

    monkeypatch.setattr("cron.scheduler.tick", fake_tick)
    monkeypatch.setattr(jobs_mod, "record_ticker_heartbeat", fake_heartbeat)
    monkeypatch.setattr(jobs_mod, "record_ticker_error", fake_error)
    monkeypatch.setattr(jobs_mod, "clear_ticker_error", fake_clear)
    # Keep recovery out of scope — it is not what this test isolates.
    monkeypatch.setattr(InProcessCronScheduler, "recover_interrupted", lambda self: 0)
    return state


def _run_multiplex(state, stop_when):
    """Drive _start_multiplex in a daemon thread until ``stop_when()`` holds,
    then stop it. Returns the thread."""
    scheduler = InProcessCronScheduler()
    stop = threading.Event()
    profile_homes = [(name, home) for name, home in state["homes"].items()]
    thread = threading.Thread(
        target=scheduler._start_multiplex,
        args=(stop,),
        kwargs={"profile_homes": profile_homes, "interval": 0},
        daemon=True,
    )
    thread.start()
    assert _wait_until(stop_when), (
        f"condition not met; tick_calls={state['tick_calls']!r} "
        f"errors={state['errors']!r}"
    )
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), "multiplex ticker did not exit on stop_event"
    return thread


class TestPerProfileIsolation:
    def test_broken_profile_does_not_block_or_blame_others(self, multiplex_env):
        """Profile alpha's tick raises every cycle: beta and gamma still tick,
        and only alpha gets the error attribution / success=False heartbeat."""
        state = multiplex_env
        state["fail_profiles"].add("alpha")

        _run_multiplex(state, stop_when=lambda: state["tick_calls"].count("gamma") >= 1)

        # One full cycle ran despite alpha raising every time.
        assert "beta" in state["tick_calls"]
        assert "gamma" in state["tick_calls"]

        # Error attribution: ONLY alpha.
        assert state["errors"], "alpha's failure was never recorded"
        assert {name for name, _ in state["errors"]} == {"alpha"}
        error_text = state["errors"][0][1]
        assert "RuntimeError" in error_text and "corrupted" in error_text

        # Heartbeats: alpha success=False, beta/gamma success=True.
        assert ("alpha", False) in state["heartbeats"]
        assert ("beta", True) in state["heartbeats"]
        assert ("gamma", True) in state["heartbeats"]
        assert ("beta", False) not in state["heartbeats"]
        assert ("gamma", False) not in state["heartbeats"]

        # Healthy profiles get their stale error marker cleared; alpha does not.
        assert "beta" in state["clears"]
        assert "gamma" in state["clears"]
        assert "alpha" not in state["clears"]

    def test_all_healthy_cycle_clears_errors_for_all(self, multiplex_env):
        """Baseline: no failures -> every profile ticks, all heartbeats succeed,
        no error recorded."""
        state = multiplex_env

        _run_multiplex(state, stop_when=lambda: state["tick_calls"].count("gamma") >= 1)

        assert set(state["tick_calls"]) == {"alpha", "beta", "gamma"}
        assert state["errors"] == []
        assert all(success for _, success in state["heartbeats"])

    def test_base_exception_propagates_to_safety_net_and_ticker_survives(
        self, multiplex_env,
    ):
        """A BaseException (SystemExit) escaping a profile tick is NOT isolated
        per profile: it hits the outer safety net, attributes the cycle failure
        to every un-ticked profile, and the ticker keeps looping."""
        state = multiplex_env
        state["raise_base"]["alpha"] = SystemExit("provider SDK called sys.exit")

        # Survive the poisoned first cycle and complete a later clean cycle.
        _run_multiplex(
            state,
            stop_when=lambda: state["tick_calls"].count("gamma") >= 2,
        )

        # The poisoned cycle attributed one error to every profile (none had
        # recorded an outcome yet when alpha blew up).
        assert any(name == "alpha" and "SystemExit" in msg
                   for name, msg in state["errors"])
        assert any(name == "beta" and "SystemExit" in msg
                   for name, msg in state["errors"])
        # Later clean cycles succeeded for all three.
        assert ("beta", True) in state["heartbeats"]
        assert ("gamma", True) in state["heartbeats"]
        assert state["tick_calls"].count("beta") >= 2
