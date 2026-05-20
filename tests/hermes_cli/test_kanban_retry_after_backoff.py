"""Tests for C2 Backoff-Sleep Enforcement (followup-2026-05-17 §2).

The dispatcher must respect a per-task ``retry_after_ts`` window before
re-claiming a task that just failed below the breaker threshold. The
window is stamped on ``tasks.last_failure_error`` as a
``; retry_after=<unix-ts>`` suffix; the dispatcher Python-filters
ready tasks against ``now`` each tick.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def all_spawnable(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda _name: True, raising=False,
    )


def test_retry_after_skipped_when_future(kanban_home, all_spawnable):
    """A task with ``retry_after`` in the future is held by the dispatcher
    and surfaced in ``result.retry_deferred``; no worker is spawned."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="future-retry", assignee="alice")
        future_ts = int(time.time()) + 60
        stamped = kb._stamp_retry_after("spawn_failed: broken pipe", future_ts)
        conn.execute(
            "UPDATE tasks SET status = 'ready', last_failure_error = ? WHERE id = ?",
            (stamped, t),
        )
        conn.commit()

        spawned = []
        res = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: spawned.append(task.id) or 9999)

    assert spawned == []
    assert t in res.retry_deferred


def test_retry_after_eligible_when_past(kanban_home, all_spawnable):
    """Once the retry_after window has passed, the dispatcher claims the
    task on the next tick as usual."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="past-retry", assignee="alice")
        past_ts = int(time.time()) - 1
        stamped = kb._stamp_retry_after("crashed: worker exited 1", past_ts)
        conn.execute(
            "UPDATE tasks SET status = 'ready', last_failure_error = ? WHERE id = ?",
            (stamped, t),
        )
        conn.commit()

        spawned = []
        res = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: spawned.append(task.id) or 9999)

    assert t in spawned
    assert t not in res.retry_deferred


def test_retry_after_stamped_on_failure(kanban_home, all_spawnable):
    """When ``_record_task_failure`` records a below-threshold spawn
    failure, the task gets a ``retry_after`` stamp matching the backoff
    schedule index ``failures-1`` (1-based)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="stamp-on-fail", assignee="alice")
        kb.claim_task(conn, t)

        before_ts = int(time.time())
        kb._record_task_failure(
            conn, t, "spawn_failed: ECONNREFUSED",
            outcome="spawn_failed", failure_limit=5,
            release_claim=True, end_run=True,
        )
        row = conn.execute(
            "SELECT consecutive_failures, last_failure_error, status FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()

    assert row["status"] == "ready"
    assert int(row["consecutive_failures"]) == 1
    stamped_ts = kb._retry_after_ts_from_error(row["last_failure_error"])
    assert stamped_ts is not None
    delta = stamped_ts - before_ts
    expected = kb._RETRY_AFTER_BACKOFF_SEC[0]
    assert expected - 2 <= delta <= expected + 5, (
        f"backoff delta {delta}s outside [{expected-2},{expected+5}]; "
        f"err={row['last_failure_error']!r}"
    )


def test_retry_after_helper_roundtrip():
    """Helpers stamp/parse symmetrically; second stamp replaces prior."""
    err1 = kb._stamp_retry_after("foo bar", 1234567)
    assert err1.endswith("; retry_after=1234567")
    assert kb._retry_after_ts_from_error(err1) == 1234567

    err2 = kb._stamp_retry_after(err1, 9999999)
    assert err2.count("; retry_after=") == 1
    assert kb._retry_after_ts_from_error(err2) == 9999999

    assert kb._retry_after_ts_from_error(None) is None
    assert kb._retry_after_ts_from_error("plain error") is None


def test_retry_after_backoff_index_schedule():
    """Backoff index is 1-based; clamps to last entry past the table."""
    assert kb._backoff_sec_for_failure(0) == 0
    assert kb._backoff_sec_for_failure(1) == kb._RETRY_AFTER_BACKOFF_SEC[0]
    assert kb._backoff_sec_for_failure(2) == kb._RETRY_AFTER_BACKOFF_SEC[1]
    assert kb._backoff_sec_for_failure(99) == kb._RETRY_AFTER_BACKOFF_SEC[-1]
