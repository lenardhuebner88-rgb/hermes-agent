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
# Rule (d): auto_release attention outcomes (Subsystem C3)
# ---------------------------------------------------------------------------


def test_auto_release_rolled_back_alerts_with_task_id_and_detail(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Dashboard chain tip")
        state = _primed_state(conn)
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                tid,
                "auto_release",
                {
                    "outcome": "rolled_back",
                    "detail": "status payload valid → invalid status payload: {}",
                    "rollback_ok": True,
                    "rollback_detail": "target=release/pre-deploy/20260705T000000Z",
                },
            )
        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW)
    assert [a["rule"] for a in alerts] == ["auto_release_attention"]
    text = alerts[0]["text"]
    assert "rolled_back" in text
    assert tid in text
    assert "🔴" in text
    assert "invalid status payload" in text
    assert "/control" in text
    # successful rollback leaves the live checkout DETACHED — the alert must
    # tell the operator/next agent to return it to main.
    assert "DETACHED" in text
    assert "git checkout main" in text


def test_auto_release_deployed_outcome_stays_silent(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Happy path chain")
        state = _primed_state(conn)
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                tid,
                "auto_release",
                {"outcome": "deployed", "detail": "status payload valid"},
            )
        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW)
    assert all(a["rule"] != "auto_release_attention" for a in alerts)


def test_auto_release_held_live_test_and_aborted_stay_silent(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ui-real chain")
        state = _primed_state(conn)
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, "auto_release",
                {"outcome": "held_live_test", "detail": "ui-real is operator-gated"},
            )
            kb._append_event(
                conn, tid, "auto_release",
                {"outcome": "aborted_pre_live_test", "detail": "fetch failed: timeout"},
            )
        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW)
    assert all(a["rule"] != "auto_release_attention" for a in alerts)


def test_auto_release_held_critical_uses_yellow_emoji(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Critical-tier chain")
        state = _primed_state(conn)
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, "auto_release",
                {"outcome": "held_critical", "detail": "chain max tier critical"},
            )
        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW)
    assert [a["rule"] for a in alerts] == ["auto_release_attention"]
    assert "🟡" in alerts[0]["text"]
    assert "held_critical" in alerts[0]["text"]


def test_auto_release_event_cursor_dedupes_same_event(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Deploy failed chain")
        state = _primed_state(conn)
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, "auto_release",
                {"outcome": "deploy_failed", "detail": "deploy script failed"},
            )
        first = evaluate_alerts(conn, _acfg(), state, now=NOW)
        # Re-evaluating without a new event must not re-push the same one.
        second = evaluate_alerts(conn, _acfg(), state, now=NOW + 1)
    assert [a["rule"] for a in first] == ["auto_release_attention"]
    assert all(a["rule"] != "auto_release_attention" for a in second)


# ---------------------------------------------------------------------------
# A1 (2026-07-06) closures: held_red_gate outcome + auto_release_hook_crashed
# kind. Payload shapes copied verbatim from the real producers (S3, merged
# 2125e6041): auto_release.maybe_auto_release()'s held_red_gate dict and
# kanban_db.py's hook-crash-recording block.
# ---------------------------------------------------------------------------


def test_auto_release_held_red_gate_uses_yellow_emoji(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Chronic-red chain")
        state = _primed_state(conn)
        with kb.write_txn(conn):
            # Real shape: hermes_cli/auto_release.py maybe_auto_release(),
            # held_red_gate branch.
            kb._append_event(
                conn, tid, "auto_release",
                {
                    "outcome": "held_red_gate",
                    "detail": "last 3 recorded green-gate nights all red",
                },
            )
        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW)
    assert [a["rule"] for a in alerts] == ["auto_release_attention"]
    assert "🟡" in alerts[0]["text"]
    assert "held_red_gate" in alerts[0]["text"]
    assert "green-gate nights all red" in alerts[0]["text"]


def test_auto_release_hook_crashed_always_alerts_red_with_error_snippet(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Chain tip whose hook crashed")
        state = _primed_state(conn)
        with kb.write_txn(conn):
            # Real shape: hermes_cli/kanban_db.py complete_task()'s
            # auto-release-hook-crashed except-block
            # (`{"error": str(_ar_exc)[:500], "chain_root": chain_root}`).
            # No "outcome" key at all — a crash never produced a verdict.
            kb._append_event(
                conn, tid, "auto_release_hook_crashed",
                {"error": "KeyError: 'planspec_source'", "chain_root": tid},
            )
        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW)
    assert [a["rule"] for a in alerts] == ["auto_release_attention"]
    assert "🔴" in alerts[0]["text"]
    assert "KeyError" in alerts[0]["text"]
    assert tid in alerts[0]["text"]


def test_auto_release_hook_crashed_first_tick_does_not_replay_historic(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Old crash before gateway restart")
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, "auto_release_hook_crashed",
                {"error": "old crash", "chain_root": tid},
            )
        state = new_alert_state()
        assert evaluate_alerts(conn, _acfg(), state, now=NOW) == []
        assert evaluate_alerts(conn, _acfg(), state, now=NOW + 1) == []


def test_auto_release_hook_crashed_and_outcome_event_share_one_cursor(kanban_home):
    """Both kinds ride ``last_seen_auto_release_event_id`` — a batch mixing
    both must alert on both and advance the cursor past the higher id."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Mixed batch chain")
        state = _primed_state(conn)
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, "auto_release",
                {"outcome": "rolled_back", "detail": "invalid status payload"},
            )
            kb._append_event(
                conn, tid, "auto_release_hook_crashed",
                {"error": "boom", "chain_root": tid},
            )
        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW)
        again = evaluate_alerts(conn, _acfg(), state, now=NOW + 1)
    assert [a["rule"] for a in alerts] == ["auto_release_attention"]
    text = alerts[0]["text"]
    assert "rolled_back" in text and "🔴" in text
    assert "Hook Crashed" in text and "boom" in text
    assert again == []  # cursor advanced past BOTH events, no replay


# ---------------------------------------------------------------------------
# A1 (2026-07-06): send-confirmation cursor gating. The watcher (gateway/
# kanban_watchers.py) used to advance the event cursor INSIDE evaluate_alerts
# and only attempt the Discord send afterwards — a failed send permanently
# dropped the alert. ``send_fn`` (opt-in) makes the cursor commit ONLY after
# a confirmed send; these tests drive that mechanic directly through the
# public evaluate_alerts() API (no watcher/asyncio involved).
# ---------------------------------------------------------------------------


class _ScriptedSend:
    """Send stub: fails ``fail_times`` times then succeeds, or always fails."""

    def __init__(self, fail_times: int = 0, always_fail: bool = False):
        self.calls: list[dict] = []
        self.fail_times = fail_times
        self.always_fail = always_fail

    def __call__(self, alert: dict) -> bool:
        self.calls.append(alert)
        if self.always_fail or len(self.calls) <= self.fail_times:
            raise RuntimeError("discord 503")
        return True


def _escalation_payload(title="Human needs to decide", why="retry ladder exhausted"):
    return {
        "task": {"id": "irrelevant", "title": title},
        "why_now": why,
        "attempts_already_made": 3,
        "evidence": {"last_error": "boom"},
        "recommended_human_action": "inspect and unblock if safe",
        "blocked_action_boundary": list(kb.OPERATOR_ONLY_ACTIONS),
    }


def test_send_failure_defers_cursor_and_retries_same_event_next_tick(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Human needs to decide")
        state = new_alert_state()
        assert evaluate_alerts(conn, _acfg(), state, now=NOW) == []  # prime
        cursor_before = state["last_seen_operator_escalation_event_id"]
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, kb.OPERATOR_ESCALATION_EVENT, _escalation_payload(),
            )
        send = _ScriptedSend(always_fail=True)

        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW + 1, send_fn=send)
        assert alerts == []  # deferred — never reached the (mocked) caller
        assert len(send.calls) == 1
        assert state["last_seen_operator_escalation_event_id"] == cursor_before

        # Next tick: cursor never moved, so the SAME event is retried, not lost.
        again = evaluate_alerts(conn, _acfg(), state, now=NOW + 2, send_fn=send)
        assert again == []
        assert len(send.calls) == 2
        assert state["last_seen_operator_escalation_event_id"] == cursor_before


def test_send_success_advances_cursor_exactly_once_no_double_send(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Human needs to decide")
        state = new_alert_state()
        assert evaluate_alerts(conn, _acfg(), state, now=NOW) == []
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, kb.OPERATOR_ESCALATION_EVENT, _escalation_payload(),
            )
        send = _ScriptedSend()

        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW + 1, send_fn=send)
        assert [a["rule"] for a in alerts] == [kb.OPERATOR_ESCALATION_EVENT]
        assert len(send.calls) == 1

        # No new event — a following tick must NOT resend the same one.
        again = evaluate_alerts(conn, _acfg(), state, now=NOW + 2, send_fn=send)
        assert again == []
        assert len(send.calls) == 1  # idempotent — no double-send


def test_send_failure_recovers_on_a_later_tick_once_discord_is_back(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Deploy failed chain")
        state = _primed_state(conn)
        cursor_before = state["last_seen_auto_release_event_id"]
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, "auto_release",
                {"outcome": "deploy_failed", "detail": "deploy script failed"},
            )
        send = _ScriptedSend(fail_times=1)  # fails once, then succeeds

        first = evaluate_alerts(conn, _acfg(), state, now=NOW, send_fn=send)
        assert first == []
        assert state["last_seen_auto_release_event_id"] == cursor_before

        second = evaluate_alerts(conn, _acfg(), state, now=NOW + 1, send_fn=send)
        assert [a["rule"] for a in second] == ["auto_release_attention"]
        assert state["last_seen_auto_release_event_id"] > cursor_before
        assert len(send.calls) == 2


def _insert_failed_run(conn, tid, error="Explosion im Schritt 2"):
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, error, "
            "started_at, ended_at) VALUES (?, 'premium', 'failed', ?, ?, ?)",
            (tid, error, NOW - 60, NOW - 1),
        )


def test_run_failed_send_failure_defers_cursor_and_bypasses_cooldown(kanban_home):
    """Regression: run_failed advanced its monotonic run-id cursor BEFORE the
    send — one failed Discord delivery lost those run failures forever.  The
    rule is now send-gated, and a deferred batch bypasses the cooldown (which
    was stamped on the failed attempt) so the retry isn't swallowed either."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Nachtjob")
        state = new_alert_state()
        assert evaluate_alerts(conn, _acfg(), state, now=NOW) == []  # prime
        cursor_before = state["last_seen_run_id"]
        _insert_failed_run(conn, tid)
        send = _ScriptedSend(always_fail=True)

        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW + 1, send_fn=send)
        assert alerts == []  # deferred
        assert len(send.calls) == 1
        assert state["last_seen_run_id"] == cursor_before

        # Retry on the very next tick — INSIDE the cooldown window that the
        # failed attempt stamped. Pre-bypass this fell into the suppression
        # branch and eagerly committed the cursor (losing the batch).
        again = evaluate_alerts(conn, _acfg(), state, now=NOW + 2, send_fn=send)
        assert again == []
        assert len(send.calls) == 2
        assert state["last_seen_run_id"] == cursor_before


def test_run_failed_send_success_advances_cursor_no_double_send(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Nachtjob")
        state = new_alert_state()
        assert evaluate_alerts(conn, _acfg(), state, now=NOW) == []
        cursor_before = state["last_seen_run_id"]
        _insert_failed_run(conn, tid)
        send = _ScriptedSend()

        alerts = evaluate_alerts(conn, _acfg(), state, now=NOW + 1, send_fn=send)
        assert [a["rule"] for a in alerts] == ["run_failed"]
        assert len(send.calls) == 1
        assert state["last_seen_run_id"] > cursor_before

        # Same failures never re-sent.
        again = evaluate_alerts(conn, _acfg(), state, now=NOW + 2, send_fn=send)
        assert again == []
        assert len(send.calls) == 1


def test_run_failed_recovers_after_transient_send_failure(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Nachtjob")
        state = new_alert_state()
        assert evaluate_alerts(conn, _acfg(), state, now=NOW) == []
        cursor_before = state["last_seen_run_id"]
        _insert_failed_run(conn, tid)
        send = _ScriptedSend(fail_times=1)  # fails once, then succeeds

        first = evaluate_alerts(conn, _acfg(), state, now=NOW + 1, send_fn=send)
        assert first == []
        assert state["last_seen_run_id"] == cursor_before

        second = evaluate_alerts(conn, _acfg(), state, now=NOW + 2, send_fn=send)
        assert [a["rule"] for a in second] == ["run_failed"]
        assert state["last_seen_run_id"] > cursor_before
        assert len(send.calls) == 2


def test_backstop_after_max_send_attempts_writes_log_and_advances_cursor(
    kanban_home,
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Human needs to decide, Discord is down")
        state = new_alert_state()
        assert evaluate_alerts(conn, _acfg(), state, now=NOW) == []
        cursor_before = state["last_seen_operator_escalation_event_id"]
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, kb.OPERATOR_ESCALATION_EVENT, _escalation_payload(),
            )
        send = _ScriptedSend(always_fail=True)

        for i in range(3):  # K == _MAX_SEND_ATTEMPTS default
            alerts = evaluate_alerts(
                conn, _acfg(), state, now=NOW + i, send_fn=send,
                max_send_attempts=3,
            )
            assert alerts == []
        assert len(send.calls) == 3

        # The 3rd failure gives up retrying: cursor commits (documented via
        # backstop, not lost) and a later tick does not retry the same batch.
        assert state["last_seen_operator_escalation_event_id"] > cursor_before
        again = evaluate_alerts(
            conn, _acfg(), state, now=NOW + 100, send_fn=send, max_send_attempts=3,
        )
        assert again == []
        assert len(send.calls) == 3  # no further attempts

        # Default backstop writer: real file, real content — kanban_home's
        # fixture redirects Path.home() to tmp_path, so this stays sandboxed.
        backstop_log = kanban_home / "reports" / "kanban-alerts-backstop.log"
        assert backstop_log.exists()
        content = backstop_log.read_text(encoding="utf-8")
        assert "operator_escalation" in content
        assert "Human needs to decide" in content
        assert "attempts=3" in content


def test_backstop_fn_override_receives_rule_text_and_attempts(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Deploy failed, Discord is down")
        state = _primed_state(conn)
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, "auto_release",
                {"outcome": "rolled_back", "detail": "invalid status payload"},
            )
        send = _ScriptedSend(always_fail=True)
        backstopped: list[dict] = []

        for i in range(2):  # small K to keep the test snappy
            evaluate_alerts(
                conn, _acfg(), state, now=NOW + i, send_fn=send,
                max_send_attempts=2, backstop_fn=backstopped.append,
            )
        assert len(backstopped) == 1
        entry = backstopped[0]
        assert entry["rule"] == "auto_release_attention"
        assert entry["attempts"] == 2
        assert "rolled_back" in entry["text"]


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
    """Mirrors the REAL DiscordAdapter contract: ``send`` returns
    ``SendResult(success=True)`` on delivery.  ``run_failed`` is send-gated
    (2026-07-10), so a bare ``None`` return would read as "delivery
    unconfirmed" and trigger the retry path instead of a single send."""

    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, reply_to=None, metadata=None):
        from gateway.platforms.base import SendResult

        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata})
        return SendResult(success=True)


async def _exercise_alert_rule_hook(runner) -> None:
    """Drive the production rule hook across ticks without a second watcher."""
    from hermes_cli.config import load_config

    acfg = load_alerts_config(load_config())
    if not acfg["enabled"]:
        return
    if not (acfg["channel_id"] or acfg.get("escalation_channel_id")):
        return
    state = new_alert_state()
    await asyncio.sleep(10)
    while runner._running:
        await runner._kanban_alert_rules_tick(acfg, state)
        slept = 0.0
        while runner._running and slept < acfg["interval_seconds"]:
            await asyncio.sleep(1.0)
            slept += 1.0


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
    asyncio.run(_exercise_alert_rule_hook(runner))

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

    asyncio.run(_exercise_alert_rule_hook(runner))  # returns immediately
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
    # A1 (2026-07-06): operator_escalation/auto_release_attention are now
    # send-gated — evaluate_alerts() delivers them itself via send_fn, and
    # the watcher filters their rule names out of THIS post-hoc loop (see
    # test_watcher_* below for that path). This test's actual target — an
    # alert carrying its own ``channel_id`` overriding the default — still
    # flows through the post-hoc loop for the three cooldown rules, so
    # ``daily_cost`` stands in for "any non-gated rule" here. The mocked
    # ``evaluate_alerts`` also needs to accept the new kwargs (send_fn/
    # max_send_attempts/backstop_fn) the watcher now always passes.
    monkeypatch.setattr(
        kanban_alerts,
        "evaluate_alerts",
        lambda conn, acfg, state, **kwargs: [
            {
                "rule": "daily_cost",
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
    asyncio.run(_exercise_alert_rule_hook(runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "999"
    assert adapter.sent[0]["text"] == "human needed"


# ---------------------------------------------------------------------------
# A1 (2026-07-06): watcher-level send-confirmation wiring. Exercises the REAL
# _kanban_alerts_watcher() (asyncio.run, no mocked evaluate_alerts) with a
# scriptable Discord-adapter double, driven by the same fake-``asyncio.sleep``
# tick-counting harness as test_alerts_watcher_sends_via_discord_adapter
# above. ``interval_seconds`` is set to the config floor (30 — see
# load_alerts_config's ``max(30.0, ...)`` clamp) purely to keep the number of
# fake-sleep iterations per real tick small; it changes no behavior under
# test.
# ---------------------------------------------------------------------------


class ScriptedAdapter:
    """Discord-adapter-shaped double whose ``send`` can be scripted to fail
    ``fail_times`` times before succeeding, or fail forever.

    Mirrors the REAL DiscordAdapter contract (adapter trace 2026-07-06):
    ``send`` never raises on delivery failure — it returns
    ``SendResult(success=False, error=...)`` (plugins/platforms/discord/
    adapter.py:2044-2046). ``raise_mode=True`` additionally covers the
    schedule/timeout failure class where an exception DOES reach the caller.
    """

    def __init__(self, fail_times: int = 0, always_fail: bool = False,
                 raise_mode: bool = False):
        self.sent: list[dict] = []
        self.attempts = 0
        self.fail_times = fail_times
        self.always_fail = always_fail
        self.raise_mode = raise_mode

    async def send(self, chat_id, text, reply_to=None, metadata=None):
        from gateway.platforms.base import SendResult

        self.attempts += 1
        if self.always_fail or self.attempts <= self.fail_times:
            if self.raise_mode:
                raise RuntimeError("discord 503")
            return SendResult(success=False, error="discord 503")
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata})
        return SendResult(success=True, message_id=f"m{self.attempts}")


def _watcher_escalation_payload(tid: str) -> dict:
    return {
        "task": {"id": tid, "title": "Human needs to decide"},
        "why_now": "retry ladder exhausted",
        "attempts_already_made": 3,
        "evidence": {},
        "recommended_human_action": "inspect",
        "blocked_action_boundary": list(kb.OPERATOR_ONLY_ACTIONS),
    }


def test_watcher_send_gated_delivery_has_no_posthoc_double_send(
    kanban_home, monkeypatch,
):
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda: {
            "kanban": {
                "alerts": {
                    "enabled": True, "channel_id": "555", "interval_seconds": 30,
                },
            },
        },
    )
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Human needs to decide")
    finally:
        conn.close()

    adapter = ScriptedAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}

    real_sleep = asyncio.sleep
    tick = {"n": 0}

    async def fake_sleep(delay):
        if delay == 10:
            return None
        tick["n"] += 1
        if tick["n"] == 1:
            conn = kb.connect()
            try:
                with kb.write_txn(conn):
                    kb._append_event(
                        conn, tid, kb.OPERATOR_ESCALATION_EVENT,
                        _watcher_escalation_payload(tid),
                    )
            finally:
                conn.close()
        if adapter.sent or tick["n"] > 200:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(_exercise_alert_rule_hook(runner))

    # Delivered exactly once via the send-gated path (send_fn confirmed it
    # INSIDE evaluate_alerts()) — the post-hoc loop must not resend it.
    assert adapter.attempts == 1
    assert len(adapter.sent) == 1
    assert "Human needs to decide" in adapter.sent[0]["text"]
    assert adapter.sent[0]["chat_id"] == "555"


def test_watcher_send_gated_failure_defers_and_retries_without_crashing_tick(
    kanban_home, monkeypatch,
):
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda: {
            "kanban": {
                "alerts": {
                    "enabled": True, "channel_id": "555", "interval_seconds": 30,
                },
            },
        },
    )
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Human needs to decide, Discord down")
    finally:
        conn.close()

    # raise_mode: this test's intent is the EXCEPTION failure class
    # (schedule/timeout/crash) — the soft-fail SendResult class has its
    # own dedicated test below.
    adapter = ScriptedAdapter(always_fail=True, raise_mode=True)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}

    real_sleep = asyncio.sleep
    tick = {"n": 0}

    async def fake_sleep(delay):
        if delay == 10:
            return None
        tick["n"] += 1
        if tick["n"] == 1:
            conn = kb.connect()
            try:
                with kb.write_txn(conn):
                    kb._append_event(
                        conn, tid, kb.OPERATOR_ESCALATION_EVENT,
                        _watcher_escalation_payload(tid),
                    )
            finally:
                conn.close()
        if adapter.attempts >= 2 or tick["n"] > 200:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    # A crashing send_fn must never crash the watcher's tick loop — if it
    # did, this asyncio.run() call itself would raise and fail the test.
    asyncio.run(_exercise_alert_rule_hook(runner))

    assert adapter.attempts >= 2  # retried across multiple real ticks
    assert adapter.sent == []  # never confirmed — nothing pushed


def test_watcher_send_gated_backstop_after_max_attempts_writes_log(
    kanban_home, monkeypatch,
):
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda: {
            "kanban": {
                "alerts": {
                    "enabled": True, "channel_id": "555", "interval_seconds": 30,
                },
            },
        },
    )
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Human needs to decide, Discord stays down")
    finally:
        conn.close()

    adapter = ScriptedAdapter(always_fail=True)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}

    real_sleep = asyncio.sleep
    tick = {"n": 0}

    async def fake_sleep(delay):
        if delay == 10:
            return None
        tick["n"] += 1
        if tick["n"] == 1:
            conn = kb.connect()
            try:
                with kb.write_txn(conn):
                    kb._append_event(
                        conn, tid, kb.OPERATOR_ESCALATION_EVENT,
                        _watcher_escalation_payload(tid),
                    )
            finally:
                conn.close()
        # Run a bit past the 3rd (K==_MAX_SEND_ATTEMPTS) failed attempt so a
        # FOLLOWING tick can prove the cursor already committed via the
        # backstop (no further attempts).
        if adapter.attempts >= 4 or tick["n"] > 300:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(_exercise_alert_rule_hook(runner))

    # The backstop takes over after exactly K=3 consecutive failures — a
    # later tick (cursor already committed) makes no further attempt.
    assert adapter.attempts == 3
    assert adapter.sent == []

    backstop_log = kanban_home / "reports" / "kanban-alerts-backstop.log"
    assert backstop_log.exists()
    content = backstop_log.read_text(encoding="utf-8")
    assert "operator_escalation" in content
    assert "Human needs to decide" in content
    assert "attempts=3" in content


def test_auto_release_alert_prefers_escalation_channel(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Chain tip with escalation channel")
        state = _primed_state(conn)
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                tid,
                "auto_release",
                {"outcome": "held_critical", "detail": "critical tier reached"},
            )
        alerts = evaluate_alerts(
            conn, _acfg(escalation_channel_id="999"), state, now=NOW
        )
    assert [a["rule"] for a in alerts] == ["auto_release_attention"]
    assert alerts[0]["channel_id"] == "999"


def test_watcher_soft_fail_sendresult_defers_cursor_not_confirmed(
    kanban_home, monkeypatch,
):
    """Adapter trace 2026-07-06: the REAL DiscordAdapter returns
    SendResult(success=False) on delivery failure instead of raising — and a
    bare dataclass is always truthy. The confirmed-send contract must read
    .success: a soft-fail must defer the cursor (retry next tick, then
    deliver), never confirm on the failed attempt."""
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda: {
            "kanban": {
                "alerts": {
                    "enabled": True, "channel_id": "555", "interval_seconds": 30,
                },
            },
        },
    )
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Human needs to decide, soft-fail")
    finally:
        conn.close()

    adapter = ScriptedAdapter(fail_times=1)  # 1st send: SendResult(success=False)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}

    real_sleep = asyncio.sleep
    tick = {"n": 0}

    async def fake_sleep(delay):
        if delay == 10:
            return None
        tick["n"] += 1
        if tick["n"] == 1:
            conn = kb.connect()
            try:
                with kb.write_txn(conn):
                    kb._append_event(
                        conn, tid, kb.OPERATOR_ESCALATION_EVENT,
                        _watcher_escalation_payload(tid),
                    )
            finally:
                conn.close()
        if len(adapter.sent) >= 1 or tick["n"] > 200:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(_exercise_alert_rule_hook(runner))

    # Attempt 1 returned SendResult(success=False) -> NOT confirmed; the
    # cursor stayed put and attempt 2 (next tick) delivered the SAME event.
    assert adapter.attempts == 2
    assert len(adapter.sent) == 1


def test_new_event_mid_retry_resets_attempt_budget(kanban_home):
    """Codex review 2026-07-06 finding 2: the retry budget is keyed by batch
    identity (rule + new_cursor). A batch that failed twice must NOT pass its
    attempts on to a GROWN batch (new event arrived mid-retry) — the grown
    batch gets a fresh budget instead of being backstopped after one more
    attempt."""
    backstopped: list[dict] = []
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Human needs to decide")
        state = new_alert_state()
        assert evaluate_alerts(conn, _acfg(), state, now=NOW) == []  # prime
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, kb.OPERATOR_ESCALATION_EVENT, _escalation_payload(),
            )
        send = _ScriptedSend(always_fail=True)
        kw = {"send_fn": send, "backstop_fn": backstopped.append}

        # Two failed attempts on the original batch (budget K=3).
        evaluate_alerts(conn, _acfg(), state, now=NOW + 1, **kw)
        evaluate_alerts(conn, _acfg(), state, now=NOW + 2, **kw)
        assert len(send.calls) == 2 and backstopped == []

        # A NEW event arrives -> batch identity (max id) changes.
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, kb.OPERATOR_ESCALATION_EVENT, _escalation_payload(),
            )

        # Third overall failure, but FIRST for the grown batch: no backstop.
        evaluate_alerts(conn, _acfg(), state, now=NOW + 3, **kw)
        assert len(send.calls) == 3
        assert backstopped == []  # fresh budget — not inherited

        # Two more failures exhaust the grown batch's own budget -> backstop.
        evaluate_alerts(conn, _acfg(), state, now=NOW + 4, **kw)
        evaluate_alerts(conn, _acfg(), state, now=NOW + 5, **kw)
        assert len(backstopped) == 1
        assert backstopped[0]["attempts"] == 3
