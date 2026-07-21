"""Deterministic pins for the terminal_tool transient retry/backoff loop (BR4).

The foreground retry loop (``tools/terminal_tool.py:2847-2893``) re-runs a
command when ``env.execute`` raises a *transient* error, backing off
exponentially (``2 ** retry_count`` → 2s, 4s, 8s), gives up after
``max_retries = 3`` (4 attempts total → ``exit_code -1``), and SHORT-CIRCUITS
without retry when the error mentions "timeout" (``exit_code 124``). It is
SIDE-EFFECTING on ``env.execute``, so a silent misfire — retrying a permanent
error, wrong delay math, giving up one attempt too early/late, or retrying a
real timeout — wastes budget or drops real work.

Every behavior here is pinned deterministically: ``LocalEnvironment.execute`` is
faked (fail N times then succeed / always fail / raise a timeout error) and
``time.sleep`` is captured rather than slept. The only pre-existing coverage
(``test_approved_command_clean_slate.py``) uses the loop merely as a vehicle for
interrupt semantics and asserts none of the retry contract below.
"""
from __future__ import annotations

import json
import time as _time_mod

from tools import terminal_tool as tt
from tools.environments.local import LocalEnvironment
from tools.interrupt import set_interrupt

CMD = "sleep 1"

# The retry backoff sleeps are ``2 ** retry_count`` with ``retry_count`` bumped
# to >=1 before the wait → always in {2, 4, 8} (>= 2). Everything else that
# shares the global ``time.sleep`` we patch is smaller: the daemon
# session-cleanup loop sleeps 1s (``terminal_tool.py:1786``) and env/process
# polling sleeps <1s (``base.py::_wait_for_process``). Those must keep REALLY
# sleeping — if we no-op them the cleanup loop spins CPU-bound and floods the
# capture with thousands of 1s. So: record+skip only sleeps >= 2 (the retry
# backoffs); delegate anything < 2 to the real sleep so those loops behave.
_RETRY_BACKOFF_MIN = 2


def _run(monkeypatch, execute_side_effect):
    """Invoke terminal_tool with a faked ``LocalEnvironment.execute`` and a
    sleep recorder that isolates the retry backoff sequence.

    The recorder records and SKIPS sleeps >= 2s (the retry backoffs 2/4/8, so
    the test is fast) and delegates sleeps < 2s to the real ``time.sleep`` (the
    daemon cleanup loop's 1s tick and sub-second polling — so they sleep
    normally instead of spinning). The returned ``sleep_sequence`` contains only
    the retry backoffs.

    ``execute_side_effect`` receives the 1-based attempt number for calls that
    carry ``CMD`` and must either return an ``{"output", "returncode"}`` dict or
    raise. Returns ``(result_dict, attempt_count, retry_backoff_sleeps)``.
    """
    calls = {"n": 0}

    def fake_execute(self, command, **kw):
        if not isinstance(command, str) or CMD not in command:
            # Ignore incidental execute calls (env probes etc.)
            return {"output": "", "returncode": 0}
        calls["n"] += 1
        return execute_side_effect(calls["n"])

    real_sleep = _time_mod.sleep
    backoffs: list = []

    def recording_sleep(seconds, *a, **k):
        if seconds >= _RETRY_BACKOFF_MIN:
            backoffs.append(seconds)  # retry backoff — record, skip the delay
        else:
            real_sleep(seconds, *a, **k)  # cleanup/polling — sleep normally

    monkeypatch.setattr(LocalEnvironment, "execute", fake_execute)
    monkeypatch.setattr("tools.terminal_tool.time.sleep", recording_sleep)
    set_interrupt(False)

    raw = tt.terminal_tool(command=CMD, force=True, task_id="retry-backoff-test")
    return json.loads(raw), calls["n"], backoffs


def test_retry_backoff_delay_sequence_is_exponential(monkeypatch):
    """3 transient failures then success → sleeps exactly [2, 4, 8] (2**n),
    4 execute attempts, and the real result is surfaced."""

    def side_effect(n):
        if n <= 3:
            raise RuntimeError("flaky backend")
        return {"output": "DONE", "returncode": 0}

    result, n, sleeps = _run(monkeypatch, side_effect)
    assert n == 4, f"expected 4 attempts, got {n}"
    assert sleeps == [2, 4, 8], f"backoff must be 2**retry_count, got {sleeps}"
    assert result["exit_code"] == 0, result
    assert "DONE" in result["output"], result


def test_retry_exhausted_returns_failure_after_max_attempts(monkeypatch):
    """An always-failing transient error → exactly 4 attempts (initial + 3
    retries), backoff [2, 4, 8], then exit -1 with the error surfaced. Pins the
    max_retries boundary (an off-by-one would retry forever or give up early)."""

    def side_effect(n):
        raise RuntimeError("permanent backend error")

    result, n, sleeps = _run(monkeypatch, side_effect)
    assert n == 4, f"expected 4 attempts (initial + 3 retries), got {n}"
    assert sleeps == [2, 4, 8], sleeps
    assert result["exit_code"] == -1, result
    assert "RuntimeError" in result["error"], result
    assert "permanent backend error" in result["error"], result


def test_timeout_error_short_circuits_without_retry(monkeypatch):
    """An error mentioning 'timeout' is classified permanent: single attempt, no
    backoff sleep, exit 124. Retrying a real timeout would burn the budget."""

    def side_effect(n):
        raise RuntimeError("command timeout after 120s")

    result, n, sleeps = _run(monkeypatch, side_effect)
    assert n == 1, f"timeout must not retry, got {n} attempts"
    assert sleeps == [], f"timeout must not backoff-sleep, got {sleeps}"
    assert result["exit_code"] == 124, result
    assert "timed out" in result["error"], result


def test_retry_then_succeed_on_second_attempt(monkeypatch):
    """1 transient failure then success → 2 attempts, a single backoff [2], and
    the recovered real result is surfaced (retry heals a flaky call)."""

    def side_effect(n):
        if n == 1:
            raise RuntimeError("transient blip")
        return {"output": "RECOVERED", "returncode": 0}

    result, n, sleeps = _run(monkeypatch, side_effect)
    assert n == 2, f"expected 2 attempts, got {n}"
    assert sleeps == [2], sleeps
    assert result["exit_code"] == 0, result
    assert "RECOVERED" in result["output"], result
