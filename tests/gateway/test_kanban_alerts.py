"""Tests for the kanban alert engine + watcher (night-sprint F2).

Rule evaluation + rate limiting are pure (gateway/kanban_alerts.py) and run
against a real tmp SQLite board; the watcher e2e test mocks the Discord
adapter (RecordingAdapter) — no real Discord call anywhere.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from gateway.kanban_alerts import (
    evaluate_alerts,
    load_alerts_config,
    new_alert_state,
)
from hermes_cli import kanban_db as kb

NOW = 1_900_000_000


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()
    return home


def _acfg(**overrides) -> dict:
    cfg = {
        "kanban": {
            "alerts": {
                "enabled": True,
                "channel_id": "123",
                **overrides,
            }
        }
    }
    return load_alerts_config(cfg)


def _insert_run(
    conn,
    *,
    task_id="t_x",
    profile="coder",
    status="done",
    outcome=None,
    error=None,
    summary=None,
    cost_usd=None,
    started_at=NOW - 600,
    ended_at=NOW - 60,
):
    with kb.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, error, "
            "summary, cost_usd, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, profile, status, outcome, error, summary, cost_usd,
             started_at, ended_at),
        )
    return cur.lastrowid


def _primed_state(conn) -> dict:
    """State after one initial tick (cursor at current MAX(id), no alerts)."""
    state = new_alert_state()
    assert evaluate_alerts(conn, _acfg(), state, now=NOW) == []
    return state


# ---------------------------------------------------------------------------
# Rule (a): failed/blocked runs
# ---------------------------------------------------------------------------


def test_run_failure_alert_carries_title_profile_and_error_snippet(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Deploy der Galerie")
        state = _primed_state(conn)
        _insert_run(
            conn, task_id=tid, profile="premium", status="failed",
            error="ValueError: boom in pipeline step 3",
        )
        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW)
    assert [a["rule"] for a in alerts] == ["run_failed"]
    text = alerts[0]["text"]
    assert "Deploy der Galerie" in text
    assert "premium" in text
    assert "ValueError: boom" in text


def test_first_tick_never_replays_historic_failures(kanban_home):
    with kb.connect() as conn:
        _insert_run(conn, status="failed", error="alt")
        state = new_alert_state()
        # First tick only primes the cursor — old failures stay silent.
        assert evaluate_alerts(conn, _acfg(), state, now=NOW) == []
        # And an immediately following tick without new runs stays silent too.
        assert evaluate_alerts(conn, _acfg(), state, now=NOW + 1) == []


def test_same_failure_not_alerted_twice(kanban_home):
    with kb.connect() as conn:
        state = _primed_state(conn)
        _insert_run(conn, status="blocked", error="hängt")
        first = evaluate_alerts(conn, _acfg(), state, now=NOW)
        assert len(first) == 1
        # Cooldown over, but no NEW failure → no repeat for the same run.
        again = evaluate_alerts(conn, _acfg(), state, now=NOW + 3600)
    assert again == []


def test_run_failed_rate_limit_suppresses_within_cooldown(kanban_home):
    with kb.connect() as conn:
        state = _primed_state(conn)
        _insert_run(conn, status="failed", error="eins")
        assert len(evaluate_alerts(conn, _acfg(), state, now=NOW)) == 1
        # A SECOND fresh failure inside the 15-min window is suppressed ...
        _insert_run(conn, status="failed", error="zwei")
        assert evaluate_alerts(conn, _acfg(), state, now=NOW + 60) == []
        # ... and a third failure after the cooldown alerts again.
        _insert_run(conn, status="failed", error="drei")
        late = evaluate_alerts(conn, _acfg(), state, now=NOW + 60 + 900)
    assert [a["rule"] for a in late] == ["run_failed"]


def test_outcome_classification_counts_gave_up(kanban_home):
    with kb.connect() as conn:
        state = _primed_state(conn)
        _insert_run(conn, status="released", outcome="gave_up", summary="3 Versuche")
        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW)
    assert len(alerts) == 1
    assert "gave_up" in alerts[0]["text"] or "released" in alerts[0]["text"]


# ---------------------------------------------------------------------------
# Rule (3B): operator escalation events
# ---------------------------------------------------------------------------


def test_operator_escalation_alert_uses_event_cursor_and_escalation_channel(
    kanban_home,
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Human needs to decide", assignee="coder")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                tid,
                kb.OPERATOR_ESCALATION_EVENT,
                {
                    "task": {"id": tid, "title": "historic"},
                    "why_now": "old retry ladder exhausted",
                    "attempts_already_made": 2,
                    "evidence": {},
                    "recommended_human_action": "old action",
                    "blocked_action_boundary": list(kb.OPERATOR_ONLY_ACTIONS),
                },
            )
        state = new_alert_state()
        assert evaluate_alerts(conn, _acfg(escalation_channel_id="999"), state, now=NOW) == []

        with kb.write_txn(conn):
            kb._append_event(
                conn,
                tid,
                kb.OPERATOR_ESCALATION_EVENT,
                {
                    "task": {"id": tid, "title": "Human needs to decide"},
                    "why_now": "retry ladder exhausted",
                    "attempts_already_made": 3,
                    "evidence": {"last_error": "boom"},
                    "recommended_human_action": "inspect and unblock if safe",
                    "blocked_action_boundary": list(kb.OPERATOR_ONLY_ACTIONS),
                },
            )
        alerts = evaluate_alerts(conn, _acfg(escalation_channel_id="999"), state, now=NOW + 1)
        repeat = evaluate_alerts(conn, _acfg(escalation_channel_id="999"), state, now=NOW + 2)

    assert [a["rule"] for a in alerts] == [kb.OPERATOR_ESCALATION_EVENT]
    assert alerts[0]["channel_id"] == "999"
    assert "Human needs to decide" in alerts[0]["text"]
    assert "retry ladder exhausted" in alerts[0]["text"]
    assert "inspect and unblock" in alerts[0]["text"]
    assert repeat == []


# ---------------------------------------------------------------------------
# Rule (b): error rate over rolling window
# ---------------------------------------------------------------------------


def test_error_rate_alert_fires_above_threshold(kanban_home):
    with kb.connect() as conn:
        state = _primed_state(conn)
        # 3 failed / 6 total = 50% > 30% inside the 30-min window. The
        # failures land in the SAME tick as the run_failed rule — both fire.
        for _ in range(3):
            _insert_run(conn, status="failed", error="x", ended_at=NOW - 30)
        for _ in range(3):
            _insert_run(conn, status="done", outcome="completed", ended_at=NOW - 30)
        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW)
    rules = {a["rule"] for a in alerts}
    assert "error_rate" in rules
    text = next(a["text"] for a in alerts if a["rule"] == "error_rate")
    assert "50%" in text and "3/6" in text


def test_error_rate_respects_min_runs(kanban_home):
    with kb.connect() as conn:
        state = _primed_state(conn)
        # 2/2 failed = 100%, but below the min-run sample gate (default 5).
        for _ in range(2):
            _insert_run(conn, status="failed", error="x", ended_at=NOW - 30)
        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW)
    assert all(a["rule"] != "error_rate" for a in alerts)


def test_error_rate_ignores_runs_outside_window(kanban_home):
    with kb.connect() as conn:
        state = _primed_state(conn)
        # Plenty of old failures (outside 30 min) + healthy recent runs.
        for _ in range(6):
            _insert_run(conn, status="failed", error="alt", ended_at=NOW - 7200)
        for _ in range(6):
            _insert_run(conn, status="done", outcome="completed", ended_at=NOW - 30)
        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW)
    assert all(a["rule"] != "error_rate" for a in alerts)


# ---------------------------------------------------------------------------
# Rule (c): daily cost threshold
# ---------------------------------------------------------------------------


def test_daily_cost_alert_fires_above_threshold_and_rate_limits(kanban_home):
    with kb.connect() as conn:
        state = _primed_state(conn)
        _insert_run(conn, status="done", outcome="completed", cost_usd=7.5,
                    started_at=NOW - 3600)
        acfg = _acfg(daily_cost_threshold_usd=5.0)
        alerts = evaluate_alerts(conn, acfg, state, now=NOW)
        assert [a["rule"] for a in alerts if a["rule"] == "daily_cost"] == ["daily_cost"]
        assert "$7.50" in next(a["text"] for a in alerts if a["rule"] == "daily_cost")
        # Still above threshold a minute later — suppressed by the cooldown.
        assert evaluate_alerts(conn, acfg, state, now=NOW + 60) == []
        # After the cooldown the standing condition alerts again.
        late = evaluate_alerts(conn, acfg, state, now=NOW + 60 + 900)
    assert [a["rule"] for a in late] == ["daily_cost"]


def test_daily_cost_rule_off_without_threshold(kanban_home):
    with kb.connect() as conn:
        state = _primed_state(conn)
        _insert_run(conn, status="done", outcome="completed", cost_usd=999.0,
                    started_at=NOW - 3600)
        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW)
    assert all(a["rule"] != "daily_cost" for a in alerts)


# ---------------------------------------------------------------------------
# Config normalization
# ---------------------------------------------------------------------------


def test_load_alerts_config_defaults_off_and_falls_back_to_reporting_channel():
    assert load_alerts_config({})["enabled"] is False
    assert load_alerts_config(None)["enabled"] is False
    cfg = load_alerts_config({
        "kanban": {
            "reporting_channel_id": "777",
            "alerts": {"enabled": True},
        }
    })
    assert cfg["enabled"] is True
    assert cfg["channel_id"] == "777"
    assert cfg["interval_seconds"] == 300
    assert cfg["cooldown_seconds"] == 900
    assert cfg["error_rate_threshold"] == pytest.approx(0.30)
    assert cfg["daily_cost_threshold_usd"] is None
    assert cfg["escalation_channel_id"] == "777"

    cfg2 = load_alerts_config({
        "kanban": {
            "alerts": {
                "enabled": True,
                "channel_id": "123",
                "escalation_channel_id": "999",
            },
        },
    })
    assert cfg2["channel_id"] == "123"
    assert cfg2["escalation_channel_id"] == "999"


# ---------------------------------------------------------------------------
# Watcher e2e (adapter mocked — no real Discord)
# ---------------------------------------------------------------------------


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, reply_to=None, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata})


def test_alerts_watcher_sends_via_discord_adapter(kanban_home, monkeypatch):
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda: {"kanban": {"alerts": {"enabled": True, "channel_id": "555"}}},
    )

    # Seed: one historic run primes nothing (cursor init happens on tick 1);
    # the failure is inserted between tick 1 and tick 2.
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Nachtjob")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}

    real_sleep = asyncio.sleep
    tick = {"n": 0}

    async def fake_sleep(delay):
        if delay == 10:  # initial adapter-connect delay
            return None
        # interval slice sleep → between ticks. After tick 1, inject the
        # failure; after tick 2 stop the loop.
        tick["n"] += 1
        if tick["n"] == 1:
            conn = kb.connect()
            try:
                with kb.write_txn(conn):
                    conn.execute(
                        "INSERT INTO task_runs (task_id, profile, status, error, "
                        "started_at, ended_at) VALUES (?, 'premium', 'failed', "
                        "'Explosion im Schritt 2', ?, ?)",
                        (tid, int(time.time()) - 60, int(time.time()) - 1),
                    )
            finally:
                conn.close()
            return None
        if tick["n"] > 600:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(runner._kanban_alerts_watcher())

    assert len(adapter.sent) == 1
    msg = adapter.sent[0]
    assert msg["chat_id"] == "555"
    assert "Nachtjob" in msg["text"]
    assert "premium" in msg["text"]
    assert "Explosion im Schritt 2" in msg["text"]


def test_alerts_watcher_noop_when_disabled(kanban_home, monkeypatch):
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(hermes_config, "load_config", lambda: {"kanban": {}})

    adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}

    asyncio.run(runner._kanban_alerts_watcher())  # returns immediately
    assert adapter.sent == []


def test_alerts_watcher_uses_alert_specific_channel(kanban_home, monkeypatch):
    from gateway import kanban_alerts
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {
            "kanban": {
                "alerts": {
                    "enabled": True,
                    "escalation_channel_id": "999",
                }
            }
        },
    )
    monkeypatch.setattr(
        kanban_alerts,
        "evaluate_alerts",
        lambda conn, acfg, state: [
            {
                "rule": kb.OPERATOR_ESCALATION_EVENT,
                "text": "human needed",
                "channel_id": acfg["escalation_channel_id"],
            }
        ],
    )

    adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}

    async def fake_sleep(delay):
        if delay != 10:
            runner._running = False

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(runner._kanban_alerts_watcher())

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "999"
    assert adapter.sent[0]["text"] == "human needed"
