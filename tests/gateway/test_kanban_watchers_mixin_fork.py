"""Fork-eigene Zusatz-Invarianten des Kanban-Watcher-Mixins.

Ausgelagert beim Upstream-Vollmerge 2026-08-04. tests/gateway/test_kanban_watchers_mixin.py
gehoert Upstream und wurde dort auf Upstreams Fassung zurueckgesetzt; hier stehen nur die
Tests (samt ihrer Helfer), die es bei Upstream nicht gibt. Methodenname folgt Upstream:
_kanban_notifier_watcher.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_alert_rules_tick",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_checkpoint",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]








def test_notifications_are_owned_by_one_production_watcher():
    from gateway.run import GatewayRunner

    startup = inspect.getsource(GatewayRunner.start)
    notifications = inspect.getsource(
        GatewayKanbanWatchersMixin._kanban_notifier_watcher
    )
    alert_hook = inspect.getsource(
        GatewayKanbanWatchersMixin._kanban_alert_rules_tick
    )
    mixin_source = inspect.getsource(GatewayKanbanWatchersMixin)

    assert startup.count("_kanban_notifier_watcher") == 1
    assert "_kanban_alerts_watcher" not in startup
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


