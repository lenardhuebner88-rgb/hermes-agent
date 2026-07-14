"""Exit-category diagnostic on the PRODUCTION gateway entrypoint.

Why this file exists
--------------------
``gateway/run.py`` emits the ``Gateway exit diagnostic:`` line from
``_exit_after_graceful_shutdown``, and ``tests/gateway/test_gateway_shutdown.py``
proves that works — but only for the ``python gateway/run.py`` entrypoint.

The service runs a *different* entrypoint::

    ExecStart=… -m hermes_cli.main … gateway run   →   hermes_cli.gateway.run_gateway()

which calls ``asyncio.run(start_gateway(...))`` and then returns / ``sys.exit(1)``
without ever reaching that backstop. Live evidence: 48h of real exits produced
289 ``gateway.exit_nonzero`` records in ``logs/gateway-exit-diag.log`` and
**zero** ``Gateway exit diagnostic:`` lines in any sink (journal, agent.log,
errors.log, gateway.log). The line was never emitted — it was not a lost flush.

What is real here vs. stubbed
-----------------------------
The tests below run the REAL ``run_gateway()`` body in a REAL subprocess and let
it exit for real, so the proof is "the line is on disk after the process is
gone" — no sleeps, no polling. Stubbed, and only to keep a test from mutating
the host:

* the four ``_guard_*`` conflict guards — they can signal a live gateway;
* ``supports_systemd_services`` — otherwise ``run_gateway`` refreshes the
  *real* systemd unit file for the profile;
* ``gateway.run.start_gateway`` — the network/platform-bound part, replaced by
  the failure injection under test (returns False / returns True / raises
  SystemExit(75)).

Everything that is actually under test — the exit-path wiring in
``run_gateway``, ``shutdown_forensics.emit_exit_category``, the classifier, the
logging sinks, and the process exit code — is the production code.
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

import gateway.run as gateway_run
from gateway import shutdown_forensics

REPO_ROOT = pathlib.Path(gateway_run.__file__).resolve().parents[1]


def _driver(start_gateway_body: str, breadcrumb: str = "None") -> str:
    """Build a subprocess driver that runs the real run_gateway() to exit."""
    return f"""
import asyncio, logging, os, sys

# Production installs a WARNING stderr handler (verbosity=0) plus the rotating
# file handlers; mirror both so every sink the operator reads is exercised.
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

import gateway.run as gr
import hermes_cli.gateway as hg
from hermes_logging import setup_logging

hermes_home = os.environ["HERMES_HOME"]

async def _start_gateway(*a, **kw):
    # Production's start_gateway configures logging before it can fail; mirror
    # that, then inject the exit under test.
    setup_logging(hermes_home=__import__("pathlib").Path(hermes_home), mode="gateway")
    gr._LAST_SHUTDOWN_CONTEXT = {breadcrumb}
{start_gateway_body}

gr.start_gateway = _start_gateway

# Host-safety stubs (see module docstring) — never the code under test.
hg.supports_systemd_services = lambda *a, **kw: False
for _guard in (
    "_guard_official_docker_root_gateway",
    "_guard_named_profile_under_multiplexer",
    "_guard_supervised_gateway_conflict",
    "_guard_existing_gateway_process_conflict",
):
    setattr(hg, _guard, lambda *a, **kw: None)

hg.run_gateway()
"""


def _run(driver: str, tmp_path) -> subprocess.CompletedProcess:
    env = dict(
        os.environ,
        PYTHONPATH=str(REPO_ROOT),
        HERMES_HOME=str(tmp_path),
        HERMES_GATEWAY_EXIT_DIAG="1",
    )
    return subprocess.run(
        [sys.executable, "-c", driver],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )


def _exit_category_records(tmp_path) -> list:
    path = tmp_path / "logs" / "gateway-exit-diag.log"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("tag") == "gateway.exit_category":
            records.append(rec)
    return records


@pytest.mark.parametrize(
    "name, body, breadcrumb, expected_code, expected_category",
    [
        # start_gateway returns False → run_gateway() → sys.exit(1).
        ("failed_start", "    return False", "None", 1, "unknown"),
        # Clean run: start_gateway returns True → falls off the end → exit 0.
        ("clean_exit", "    return True", "None", 0, "regular"),
        # Planned service restart: SystemExit(75) is re-raised untouched.
        (
            "restart_exit_75",
            "    raise SystemExit(75)",
            "None",
            75,
            "regular",
        ),
        # Unexpected signal-driven death: breadcrumb says signal_initiated and
        # nothing explains it → the classifier must fail closed to 'unknown'.
        (
            "signal_initiated",
            "    return False",
            "{'planned': False, 'signal_initiated': True, 'ctx': {'signal': 'SIGTERM'}}",
            1,
            "unknown",
        ),
        # Operator-planned stop → 'regular' even though the exit code is 1.
        (
            "planned_stop",
            "    return False",
            "{'planned': True, 'signal_initiated': False, 'ctx': {'signal': 'SIGTERM'}}",
            1,
            "regular",
        ),
    ],
)
def test_production_entrypoint_flushes_exit_category_before_process_end(
    tmp_path, name, body, breadcrumb, expected_code, expected_category
):
    """`hermes gateway run` emits + flushes the exit category on every exit path.

    The assertions read the sinks AFTER the process is gone, so they can only
    pass if the line was flushed before process end.
    """
    proc = _run(_driver(body, breadcrumb), tmp_path)

    # AC3: exit codes are untouched by the diagnostic. 1 and 75 in particular
    # can only come from run_gateway's own sys.exit / re-raised SystemExit.
    assert proc.returncode == expected_code, (
        f"[{name}] exit code changed: {proc.returncode} "
        f"(stderr tail: {proc.stderr[-1500:]})"
    )

    # Sink 1 — structured JSON, independent of the logging config.
    records = _exit_category_records(tmp_path)
    assert len(records) == 1, f"[{name}] expected exactly one exit-category record"
    assert records[0]["category"] == expected_category
    assert records[0]["exit_code"] == expected_code

    # Sink 2 — the rotating file handler the operator greps (errors.log, WARNING+).
    errors_log = (tmp_path / "logs" / "errors.log").read_text(encoding="utf-8")
    assert f"category={expected_category}" in errors_log
    assert f"exit_code={expected_code}" in errors_log

    # Sink 3 — stderr, i.e. what StandardError=journal captures.
    assert "Gateway exit diagnostic:" in proc.stderr, proc.stderr[-1500:]
    assert f"category={expected_category}" in proc.stderr


def test_emit_exit_category_is_idempotent_and_flushes(tmp_path, caplog):
    """Exactly one line per process, and the handlers are flushed explicitly."""
    import logging

    shutdown_forensics.reset_exit_category_state()

    flushed = []

    class _RecordingHandler(logging.Handler):
        def emit(self, record):
            pass

        def flush(self):
            flushed.append(True)

    log = logging.getLogger("gateway.run.test_emit")
    handler = _RecordingHandler()
    log.addHandler(handler)
    try:
        with caplog.at_level(logging.WARNING):
            first = shutdown_forensics.emit_exit_category(
                0, logger=log, hermes_home=tmp_path
            )
            second = shutdown_forensics.emit_exit_category(
                1, logger=log, hermes_home=tmp_path
            )
    finally:
        log.removeHandler(handler)

    assert first is True
    assert second is False, "the exit category must be emitted at most once per process"
    assert flushed, "handlers must be flushed — os._exit skips logging.shutdown()"

    lines = [r.getMessage() for r in caplog.records if "Gateway exit diagnostic" in r.getMessage()]
    assert len(lines) == 1
    assert "category=regular exit_code=0" in lines[0]
    assert len(_exit_category_records(tmp_path)) == 1


def test_json_sink_honours_exit_diag_optout_but_logging_sink_does_not(
    tmp_path, monkeypatch, caplog
):
    """HERMES_GATEWAY_EXIT_DIAG=0 silences the shared JSON log, not the warning.

    That file is `hermes_cli.gateway`'s opt-out-able scaffolding sink; the
    operator-facing WARNING is the permanent one and must always be emitted.
    """
    import logging

    shutdown_forensics.reset_exit_category_state()
    monkeypatch.setenv("HERMES_GATEWAY_EXIT_DIAG", "0")

    with caplog.at_level(logging.WARNING):
        assert shutdown_forensics.emit_exit_category(1, hermes_home=tmp_path) is True

    assert _exit_category_records(tmp_path) == []
    assert any("Gateway exit diagnostic" in r.getMessage() for r in caplog.records)


def test_emit_exit_category_never_raises(tmp_path):
    """A broken sink must never be able to stop the process from exiting."""
    shutdown_forensics.reset_exit_category_state()

    class _ExplodingLogger:
        handlers = []

        def warning(self, *a, **kw):
            raise RuntimeError("sink is down")

    # Unwritable hermes_home → the JSON sink fails too. Still must not raise.
    assert (
        shutdown_forensics.emit_exit_category(
            1, logger=_ExplodingLogger(), hermes_home=tmp_path / "nope" / "\0bad"
        )
        is True
    )
