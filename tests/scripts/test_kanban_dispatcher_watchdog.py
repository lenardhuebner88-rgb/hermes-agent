"""Tests for the kanban dispatcher heartbeat watchdog (AC3-dispatcher-watchdog).

The watchdog reads ``~/.hermes/state/kanban_dispatcher_heartbeat.json`` (written by
``gateway/kanban_watchers.py`` via ``write_kanban_dispatcher_heartbeat``) and emits at
most ONE Discord alert per calendar day when the heartbeat is stale or unhealthy.
Alert-only — never restarts anything.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from scripts import kanban_dispatcher_watchdog as wd


# A fixed "now" so date buckets are deterministic. 2026-06-18 12:00:00 UTC.
NOW = dt.datetime(2026, 6, 18, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()
NEXT_DAY = dt.datetime(2026, 6, 19, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()


def _write_heartbeat(path: Path, *, last_tick_at: float, tick_health: str = "ok") -> None:
    payload = {
        "last_tick_at": int(last_tick_at),
        "tick_health": tick_health,
        "last_green_gate_at": int(last_tick_at) if tick_health == "ok" else None,
        "counts": {"self_healed_today": 0, "parked_open": 0, "open_escalations": 0, "stranded": 0},
        "boards": [],
    }
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "heartbeat": tmp_path / "kanban_dispatcher_heartbeat.json",
        "state": tmp_path / "kanban_dispatcher_watchdog_state.json",
    }


@pytest.fixture
def captured_posts(monkeypatch) -> list[str]:
    """Replace the network Discord call with an in-memory recorder."""
    posts: list[str] = []

    def _fake_post(body: str, channel: str) -> dict:
        posts.append(body)
        return {"result": "sent", "message_id": f"msg-{len(posts)}"}

    monkeypatch.setattr(wd, "_post_discord", _fake_post)
    return posts


def _run(paths: dict[str, Path], *, now: float, **kw) -> dict:
    return wd.run(
        heartbeat_path=paths["heartbeat"],
        state_path=paths["state"],
        now=now,
        **kw,
    )


def test_fresh_healthy_heartbeat_does_not_alert(paths, captured_posts):
    _write_heartbeat(paths["heartbeat"], last_tick_at=NOW - 60, tick_health="ok")

    result = _run(paths, now=NOW)

    assert result["action"] == "noop"
    assert captured_posts == []


def test_stale_heartbeat_alerts_exactly_once_per_day(paths, captured_posts):
    # Heartbeat last ticked 20 minutes ago (> 15 min default threshold).
    _write_heartbeat(paths["heartbeat"], last_tick_at=NOW - 20 * 60, tick_health="ok")

    first = _run(paths, now=NOW)
    second = _run(paths, now=NOW + 30)  # same calendar day, still stale

    assert first["action"] == "alert_emitted"
    assert first["alert_reason"] == "stale"
    assert second["action"] == "noop"
    assert second["reason"] == "already_alerted_today"
    assert len(captured_posts) == 1


def test_unhealthy_tick_health_alerts_even_when_fresh(paths, captured_posts):
    # Fresh tick (1 min ago) but the dispatcher reported a bad tick.
    _write_heartbeat(paths["heartbeat"], last_tick_at=NOW - 60, tick_health="degraded")

    first = _run(paths, now=NOW)
    second = _run(paths, now=NOW + 30)

    assert first["action"] == "alert_emitted"
    assert first["alert_reason"] == "tick_health"
    assert second["action"] == "noop"
    assert len(captured_posts) == 1


def test_new_calendar_day_alerts_again(paths, captured_posts):
    _write_heartbeat(paths["heartbeat"], last_tick_at=NOW - 20 * 60, tick_health="ok")
    _run(paths, now=NOW)

    # Next day, heartbeat still stale relative to the new "now".
    _write_heartbeat(paths["heartbeat"], last_tick_at=NEXT_DAY - 20 * 60, tick_health="ok")
    third = _run(paths, now=NEXT_DAY)

    assert third["action"] == "alert_emitted"
    assert len(captured_posts) == 2


def test_missing_heartbeat_alerts(paths, captured_posts):
    # No heartbeat file at all → dispatcher never wrote one → alert.
    result = _run(paths, now=NOW)

    assert result["action"] == "alert_emitted"
    assert result["alert_reason"] == "heartbeat_missing"
    assert len(captured_posts) == 1


def test_dry_run_never_posts(paths, captured_posts):
    _write_heartbeat(paths["heartbeat"], last_tick_at=NOW - 20 * 60, tick_health="ok")

    result = _run(paths, now=NOW, dry_run=True)

    assert result["action"] == "alert_would_have_fired"
    assert captured_posts == []
    # Dry-run must NOT persist the alert bucket, so a real run can still fire.
    assert not paths["state"].exists()


def test_evaluate_classifies_states():
    fresh = {"last_tick_at": int(NOW - 60), "tick_health": "ok"}
    healthy, reason, _ = wd.evaluate(fresh, now=NOW, stale_after_seconds=15 * 60)
    assert healthy is True and reason == "ok"

    stale = {"last_tick_at": int(NOW - 16 * 60), "tick_health": "ok"}
    healthy, reason, _ = wd.evaluate(stale, now=NOW, stale_after_seconds=15 * 60)
    assert healthy is False and reason == "stale"

    bad = {"last_tick_at": int(NOW - 60), "tick_health": "error"}
    healthy, reason, _ = wd.evaluate(bad, now=NOW, stale_after_seconds=15 * 60)
    assert healthy is False and reason == "tick_health"


def test_alert_send_failure_does_not_persist_bucket(paths, monkeypatch):
    """If Discord rejects the post, we must retry on the next run, not swallow it."""
    _write_heartbeat(paths["heartbeat"], last_tick_at=NOW - 20 * 60, tick_health="ok")
    monkeypatch.setattr(
        wd, "_post_discord", lambda body, channel: {"result": "error", "error": "http_500"}
    )

    result = _run(paths, now=NOW)

    assert result["action"] == "alert_send_failed"
    assert not paths["state"].exists()
