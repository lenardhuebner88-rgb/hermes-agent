"""Regression test for FINDING #6: duplicate Discord reports on mid-batch send failure.

Scenario
--------
A subscription has two pending events (event A, event B).
claim_unseen_events_for_sub atomically advances the cursor to cover BOTH events.
On tick 1 the adapter delivers A successfully, then raises on B.
The existing code rewinds to old_cursor (before A), so tick 2 re-delivers A as
a duplicate.

Fix contract: on failure of event N, rewind to delivered_cursor (= id of the
last successfully sent event), not old_cursor.  After the fix:
- tick 2 re-delivers only B (the failed event).
- event A is delivered exactly once across both ticks.
"""

import asyncio

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Helpers (mirror test_kanban_notifier.py style)
# ---------------------------------------------------------------------------


def _make_runner(adapter, platform=Platform.TELEGRAM):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {platform: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifications_watcher(interval=1)


class PartiallyFailingAdapter:
    """Succeeds for the first ``fail_on`` deliveries, then raises once.

    Tracks every (chat_id, text) pair that was successfully sent.
    """

    def __init__(self, *, fail_on_call: int):
        """fail_on_call: 1-based index of the send() call that should raise."""
        self.sent: list[dict] = []
        self._call_count = 0
        self._fail_on = fail_on_call

    async def send(self, chat_id, text, metadata=None):
        self._call_count += 1
        if self._call_count == self._fail_on:
            raise RuntimeError(f"simulated send failure on call {self._call_count}")
        self.sent.append({"chat_id": chat_id, "text": text})


# ---------------------------------------------------------------------------
# The regression test
# ---------------------------------------------------------------------------


def test_no_duplicate_on_mid_batch_send_failure(tmp_path, monkeypatch):
    """Event A must be delivered exactly once even when event B fails.

    Reproduces FINDING #6 (duplicate Discord reports): before the fix,
    rewinding to old_cursor caused event A to be re-claimed and re-delivered
    on the next tick.
    """
    db_path = tmp_path / "mid-batch-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    # Build a task with two consecutive events so both land in one batch.
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="batch failure test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # Two non-terminal events so the subscription stays alive after tick 1.
        kb._append_event(conn, tid, kind="crashed")   # event A — will succeed
        kb._append_event(conn, tid, kind="crashed")   # event B — will fail on tick 1
    finally:
        conn.close()

    # Tick 1: adapter succeeds on send #1 (event A), raises on send #2 (event B).
    adapter = PartiallyFailingAdapter(fail_on_call=2)
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # After tick 1: exactly one successful delivery (event A).
    assert len(adapter.sent) == 1, (
        f"tick 1 should deliver event A only; got {len(adapter.sent)} deliveries"
    )
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Tick 2: a fresh adapter that succeeds on everything.
    adapter2 = PartiallyFailingAdapter(fail_on_call=999)
    runner2 = _make_runner(adapter2)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner2))

    # CRITICAL ASSERTION: event A must NOT appear again.
    # Before the fix the rewind went to old_cursor (before A), so adapter2
    # would also receive event A — a duplicate.
    # After the fix the rewind goes to delivered_cursor (= A's id), so only
    # event B is retried.
    all_deliveries = adapter.sent + adapter2.sent
    assert len(all_deliveries) == 2, (
        f"total deliveries across both ticks should be 2 (A once + B once); "
        f"got {len(all_deliveries)}: tick1={[d['text'][:40] for d in adapter.sent]}, "
        f"tick2={[d['text'][:40] for d in adapter2.sent]}"
    )
    # Tick 2 should have delivered exactly 1 event (event B retry).
    assert len(adapter2.sent) == 1, (
        f"tick 2 should retry only event B; got {len(adapter2.sent)} deliveries "
        f"(duplicate of A = BUG): {[d['text'][:60] for d in adapter2.sent]}"
    )
