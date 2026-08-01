"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifications_watcher",
    "_kanban_alert_rules_tick",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_checkpoint",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_gateway_runner_inherits_mixin():
    # Import here so a heavy gateway import only happens if the first test passed.
    from gateway.run import GatewayRunner

    assert issubclass(GatewayRunner, GatewayKanbanWatchersMixin)
    # Each kanban method resolves to the mixin's implementation via the MRO.
    for m in KANBAN_METHODS:
        owner = next(c for c in GatewayRunner.__mro__ if m in c.__dict__)
        assert owner is GatewayKanbanWatchersMixin, (
            f"{m} resolved to {owner.__name__}, expected the mixin"
        )


def test_watcher_loops_are_coroutines():
    # The two long-running watchers are async loops.
    assert inspect.iscoroutinefunction(
        GatewayKanbanWatchersMixin._kanban_notifications_watcher
    )
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)


def test_notifications_are_owned_by_one_production_watcher():
    from gateway.run import GatewayRunner

    startup = inspect.getsource(GatewayRunner.start)
    notifications = inspect.getsource(
        GatewayKanbanWatchersMixin._kanban_notifications_watcher
    )
    alert_hook = inspect.getsource(
        GatewayKanbanWatchersMixin._kanban_alert_rules_tick
    )
    mixin_source = inspect.getsource(GatewayKanbanWatchersMixin)

    assert startup.count("_kanban_notifications_watcher") == 1
    assert "_kanban_notifier_watcher" not in startup
    assert "_kanban_alerts_watcher" not in startup
    assert not hasattr(GatewayKanbanWatchersMixin, "_kanban_notifier_watcher")
    assert not hasattr(GatewayKanbanWatchersMixin, "_kanban_alerts_watcher")
    assert "_kanban_alert_rules_tick" in notifications
    assert "_kanban_send_confirmed" in alert_hook
    assert mixin_source.count("await adapter.send(") == 1


def test_dispatcher_watcher_surfaces_review_wait_attention():
    source = inspect.getsource(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)

    assert "emit_review_wait_attention" in source


class _InjectRecordingAdapter:
    def __init__(self) -> None:
        self.handled: list = []

    async def handle_message(self, event) -> None:
        self.handled.append(event)


async def _inline_off_loop(fn, /, *args, **kwargs):
    return fn(*args, **kwargs)


@pytest.mark.parametrize(
    ("classification", "expected_injections"),
    [(False, 0), (None, 1)],
    ids=["noninjectable", "legacy-default-injectable"],
)
def test_escalation_triage_inject_honors_alert_classification(
    monkeypatch, classification, expected_injections,
):
    """A false classification blocks the synthetic turn while an alert
    without the new field keeps the pre-S8 behavior."""
    from gateway.config import Platform

    adapter = _InjectRecordingAdapter()
    runner = GatewayKanbanWatchersMixin()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._kanban_off_loop = _inline_off_loop
    connection = type("Connection", (), {"close": lambda self: None})()
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: connection)
    alert = {"rule": "operator_escalation", "text": "release gate held"}
    if classification is not None:
        alert["orchestrator_injectable"] = classification
    monkeypatch.setattr(
        "gateway.kanban_alerts.evaluate_alerts",
        lambda *_args, **_kwargs: [alert],
    )
    acfg = {
        "channel_id": "alerts",
        "escalation_channel_id": "escalation",
        "escalation_triage_inject": True,
        "thread_id": None,
    }

    returned = asyncio.run(runner._kanban_alert_rules_tick(acfg, {}))

    assert returned == [alert]
    assert len(adapter.handled) == expected_injections


def test_singleton_dispatcher_lock_is_exclusive(tmp_path):
    """Only one holder of the dispatcher lock at a time — the backstop that
    stops concurrent dispatchers double reclaiming and corrupting shared
    kanban SQLite index pages under wal_autocheckpoint=0."""
    import os

    from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock

    lock = tmp_path / "kanban" / ".dispatcher.lock"

    h1, st1 = _acquire_singleton_lock(lock)
    assert st1 == "held" and h1 is not None

    # A second acquire while the first is held must be refused, not granted.
    h2, st2 = _acquire_singleton_lock(lock)
    assert st2 == "contended" and h2 is None

    # Releasing the first lets a fresh acquire succeed (lock is reusable).
    _release_singleton_lock(h1)
    h3, st3 = _acquire_singleton_lock(lock)
    assert st3 == "held" and h3 is not None
    _release_singleton_lock(h3)
