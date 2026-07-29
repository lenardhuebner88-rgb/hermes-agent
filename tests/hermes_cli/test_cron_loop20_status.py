"""Audit loop 20 / F2 (CLI half): `hermes cron status` shows the sending
state of the delivery outbox.

The most crash-critical outbox state — ``sending`` — used to be visible
only in the JSONL itself. _print_outbox_summary() now shows the sending
count with the oldest live lease age, plus a ⚠ alarm line for leases
already older than the TTL (orphaned: the holder crashed mid-send).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import cron.delivery_outbox as outbox
from cron.jobs import use_cron_store
from hermes_cli import cron as cron_cli


def _target(chat_id="1"):
    return {"platform": "telegram", "chat_id": chat_id, "thread_id": None}


def test_status_shows_sending_count_and_oldest_lease_age(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / "home"
    base = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    clock = {"now": base}
    monkeypatch.setattr(outbox, "_hermes_now", lambda: clock["now"])

    with use_cron_store(home):
        entry = outbox.enqueue("job", _target(), "p", "err", execution_id="e1")
        assert outbox.begin_replay(entry["id"]) is not None
        clock["now"] = base + timedelta(seconds=30)  # lease 30s old, TTL 600

        cron_cli._print_outbox_summary()
        out = capsys.readouterr().out
        assert "1 report(s) currently sending" in out
        assert "oldest lease 30s old" in out
        # Fresh lease: no orphan alarm, no queued/dead lines.
        assert "OLDER than the TTL" not in out
        assert "queued" not in out
        assert "DEAD" not in out


def test_status_alarms_on_expired_send_lease(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    base = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    clock = {"now": base}
    monkeypatch.setattr(outbox, "_hermes_now", lambda: clock["now"])
    monkeypatch.setenv("HERMES_CRON_OUTBOX_LEASE_TTL_SECONDS", "60")

    with use_cron_store(home):
        entry = outbox.enqueue("job", _target(), "p", "err", execution_id="e1")
        assert outbox.begin_replay(entry["id"]) is not None
        clock["now"] = base + timedelta(seconds=300)  # 300s > TTL 60s

        cron_cli._print_outbox_summary()
        out = capsys.readouterr().out
        assert "currently sending" in out
        assert "1 send lease(s) OLDER than the TTL" in out
        assert "crashed mid-send" in out


def test_status_keeps_queued_and_dead_lines(tmp_path, monkeypatch, capsys):
    """The loop-6 lines are unchanged; sending only ADDS to the summary."""
    home = tmp_path / "home"
    with use_cron_store(home):
        outbox.enqueue("job", _target("1"), "p", "err", execution_id="e1")
        outbox.enqueue(
            "job", _target("2"), "p", "err",
            execution_id="e2", error_class=outbox.ERROR_CLASS_CONFIG,
        )

        cron_cli._print_outbox_summary()
        out = capsys.readouterr().out
        assert "1 report(s) queued" in out
        assert "1 DEAD report(s)" in out
        assert "currently sending" not in out


def test_status_silent_on_empty_outbox(tmp_path, capsys):
    home = tmp_path / "home"
    with use_cron_store(home):
        cron_cli._print_outbox_summary()
    assert capsys.readouterr().out == ""


def test_status_survives_unreadable_outbox(monkeypatch, capsys):
    """Best-effort contract: a broken outbox must never break cron status."""
    monkeypatch.setattr(
        outbox, "_read_entries",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")),
    )
    cron_cli._print_outbox_summary()
    assert capsys.readouterr().out == ""


def test_cron_status_end_to_end_includes_sending_line(
    tmp_path, monkeypatch, capsys,
):
    """Full cron_status() run with a leased outbox entry shows the line."""
    home = tmp_path / "home"
    monkeypatch.setattr(cron_cli, "_active_cron_provider_name", lambda: "builtin")
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
    monkeypatch.setattr("cron.jobs.list_jobs", lambda include_disabled=False: [])

    with use_cron_store(home):
        entry = outbox.enqueue("job", _target(), "p", "err", execution_id="e1")
        assert outbox.begin_replay(entry["id"]) is not None

        cron_cli.cron_status()
        out = capsys.readouterr().out
        assert "1 report(s) currently sending" in out
