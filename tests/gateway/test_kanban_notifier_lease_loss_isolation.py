import asyncio
import logging
from unittest.mock import MagicMock

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifications_watcher(interval=1)


def test_lease_loss_isolated_to_one_delivery(tmp_path, monkeypatch, caplog):
    db_path = tmp_path / "lease-loss-isolation.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        lost_task = kb.create_task(conn, title="lost lease", assignee="worker-a")
        next_task = kb.create_task(conn, title="next delivery", assignee="worker-b")
        kb.add_notify_sub(
            conn, task_id=lost_task, platform="telegram", chat_id="chat-lost"
        )
        kb.add_notify_sub(
            conn, task_id=next_task, platform="telegram", chat_id="chat-next"
        )
        kb._append_event(conn, lost_task, kind="crashed", payload={"pid": 101})
        kb._append_event(conn, next_task, kind="crashed", payload={"pid": 202})
    finally:
        conn.close()

    real_list_notify_subs = kb.list_notify_subs

    def lost_delivery_first(conn, task_id=None):
        subscriptions = real_list_notify_subs(conn, task_id)
        return sorted(subscriptions, key=lambda sub: sub["task_id"] != lost_task)

    real_ack = kb.ack_notify_delivery_claim
    acked_tasks = []

    def lose_first_lease(conn, **kwargs):
        acked_tasks.append(kwargs["task_id"])
        if kwargs["task_id"] == lost_task:
            return False
        return real_ack(conn, **kwargs)

    direct_cursor_advance = MagicMock(wraps=kb.advance_notify_cursor)
    monkeypatch.setattr(kb, "list_notify_subs", lost_delivery_first)
    monkeypatch.setattr(kb, "ack_notify_delivery_claim", lose_first_lease)
    monkeypatch.setattr(kb, "advance_notify_cursor", direct_cursor_advance)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )

    adapter = RecordingAdapter()
    with caplog.at_level(logging.WARNING, logger="gateway.kanban_watchers"):
        asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert [delivery["chat_id"] for delivery in adapter.sent] == [
        "chat-lost",
        "chat-next",
    ]
    assert acked_tasks == [lost_task, next_task]
    direct_cursor_advance.assert_not_called()
    assert lost_task in caplog.text
    assert "notification delivery lease was lost" in caplog.text
    assert "kanban notifier tick failed" not in caplog.text


def test_notifier_tick_keeps_outer_exception_backstop(monkeypatch, caplog):
    runner = _make_runner(RecordingAdapter())
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )
    monkeypatch.setattr(runner, "_kanban_flush_stalled_trees", lambda *_args: None)

    async def fail_collection(fn, *args, **kwargs):
        if fn.__name__ == "_collect":
            raise RuntimeError("outer backstop probe")
        return fn(*args, **kwargs)

    monkeypatch.setattr(runner, "_kanban_off_loop", fail_collection)

    with caplog.at_level(logging.WARNING, logger="gateway.kanban_watchers"):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert "kanban notifier tick failed: outer backstop probe" in caplog.text
