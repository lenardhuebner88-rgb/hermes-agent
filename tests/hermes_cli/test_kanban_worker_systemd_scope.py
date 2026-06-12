"""Tests for the opt-in systemd-scope worker detachment (2026-06-11 memory audit).

Workers spawned from a systemd service (dashboard dispatch/restart, gateway
tick) used to stay in that service's cgroup: a service restart killed every
running worker, and the service's MemoryPeak accounted gigabytes of worker
RAM. With HERMES_WORKER_SYSTEMD_SCOPE=1 the spawn argv is wrapped in
`systemd-run --user --scope`, which re-execs in place (PID/signal semantics
unchanged).
"""
from __future__ import annotations

from hermes_cli import kanban_db as kb

CMD = ["claude", "-p", "prompt", "--output-format", "json"]


def _reset_probe_cache():
    kb._SYSTEMD_SCOPE_USABLE = None


def test_no_wrap_without_env_flag(monkeypatch):
    monkeypatch.delenv("HERMES_WORKER_SYSTEMD_SCOPE", raising=False)
    _reset_probe_cache()
    assert kb._maybe_scope_worker_cmd(CMD) == CMD


def test_wraps_when_enabled_and_usable(monkeypatch):
    monkeypatch.setenv("HERMES_WORKER_SYSTEMD_SCOPE", "1")
    monkeypatch.setattr(kb, "_systemd_scope_usable", lambda: True)
    wrapped = kb._maybe_scope_worker_cmd(CMD)
    assert wrapped[:7] == [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        "--property=CPUWeight=30", "--",
    ]
    assert wrapped[7:] == CMD


def test_no_wrap_when_probe_fails(monkeypatch):
    monkeypatch.setenv("HERMES_WORKER_SYSTEMD_SCOPE", "1")
    monkeypatch.setattr(kb, "_systemd_scope_usable", lambda: False)
    assert kb._maybe_scope_worker_cmd(CMD) == CMD


def test_no_wrap_on_windows(monkeypatch):
    monkeypatch.setenv("HERMES_WORKER_SYSTEMD_SCOPE", "1")
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)
    monkeypatch.setattr(kb, "_systemd_scope_usable", lambda: True)
    assert kb._maybe_scope_worker_cmd(CMD) == CMD


def test_probe_failure_is_cached_and_logged(monkeypatch, caplog):
    monkeypatch.setenv("HERMES_WORKER_SYSTEMD_SCOPE", "1")
    _reset_probe_cache()
    calls = {"n": 0}

    def failing_run(*a, **k):
        calls["n"] += 1
        raise OSError("no systemd here")

    monkeypatch.setattr(kb.subprocess, "run", failing_run)
    assert kb._maybe_scope_worker_cmd(CMD) == CMD
    assert kb._maybe_scope_worker_cmd(CMD) == CMD
    assert calls["n"] == 1, "probe must run once per process, not per spawn"
    _reset_probe_cache()
