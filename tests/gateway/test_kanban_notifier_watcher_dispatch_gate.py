"""Ownership tests for the unified Kanban notifications watcher.

- Non-dispatch gateways do not poll subscriptions.
- Alert rules remain available when dispatch ownership is external.
- Only one gateway process evaluates the in-memory alert rules.
- Dispatch-owning gateways (dispatch_in_gateway=true) proceed past the gate.
"""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gateway.config import Platform
from gateway.run import GatewayRunner


def _make_runner(with_adapter=False):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: MagicMock()} if with_adapter else {}
    runner._kanban_sub_fail_counts = {}
    return runner


def _fake_config(dispatch_in_gateway):
    return {"kanban": {"dispatch_in_gateway": dispatch_in_gateway}}


def test_notifier_watcher_skips_when_dispatch_disabled():
    """dispatch_in_gateway=false returns before opening any board DB."""
    runner = _make_runner()
    with patch("hermes_cli.config.load_config", return_value=_fake_config(False)):
        with patch("hermes_cli.kanban_db.connect") as mock_connect:
            asyncio.run(runner._kanban_notifications_watcher())
    mock_connect.assert_not_called()


def test_notifier_watcher_env_override_disables(monkeypatch):
    """The env override disables subscription polling, but config still gates alerts."""
    runner = _make_runner()
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "false")
    with patch(
        "hermes_cli.config.load_config",
        return_value={"kanban": {"alerts": {"enabled": False}}},
    ) as mock_load_config:
        with patch("hermes_cli.kanban_db.connect") as mock_connect:
            asyncio.run(runner._kanban_notifications_watcher())
    mock_load_config.assert_called_once()
    mock_connect.assert_not_called()


def test_alert_rules_run_when_subscription_dispatch_is_external(tmp_path, monkeypatch):
    """dispatch=false must gate DB subscription polling, not the alert rule hook."""
    from gateway import kanban_watchers as watchers
    from hermes_cli import kanban_db as _kb

    runner = _make_runner()
    alert_ticks = []
    sleep_calls = []

    async def fake_sleep(_delay):
        sleep_calls.append(True)
        if len(sleep_calls) >= 2:
            runner._running = False

    async def alert_tick(config, state):
        alert_ticks.append((config, state))

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": False,
                "alerts": {
                    "enabled": True,
                    "channel_id": "operator-room",
                    "interval_seconds": 300,
                },
            }
        },
    )
    monkeypatch.setattr(_kb, "kanban_home", lambda: tmp_path)
    list_boards = MagicMock(return_value=[])
    monkeypatch.setattr(_kb, "list_boards", list_boards)
    monkeypatch.setattr(
        watchers, "_acquire_singleton_lock", lambda _path: (object(), "held")
    )
    monkeypatch.setattr(watchers, "_release_singleton_lock", lambda _handle: None)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(runner, "_kanban_alert_rules_tick", alert_tick)

    asyncio.run(runner._kanban_notifications_watcher(interval=1))

    assert len(alert_ticks) == 1
    list_boards.assert_not_called()
    assert runner._kanban_alerts_lock_handle is None


def test_alert_rule_failure_keeps_configured_cadence(tmp_path, monkeypatch):
    """A failing rule tick is retried at its deadline, not every watcher tick."""
    from gateway import kanban_watchers as watchers
    from hermes_cli import kanban_db as _kb

    runner = _make_runner()
    sleep_calls = []
    alert_ticks = []

    async def fake_sleep(_delay):
        sleep_calls.append(True)
        if len(sleep_calls) >= 4:
            runner._running = False

    async def failing_alert_tick(_config, _state):
        alert_ticks.append(True)
        raise RuntimeError("synthetic alert failure")

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": False,
                "alerts": {
                    "enabled": True,
                    "channel_id": "operator-room",
                    "interval_seconds": 300,
                },
            }
        },
    )
    monkeypatch.setattr(_kb, "kanban_home", lambda: tmp_path)
    monkeypatch.setattr(
        watchers, "_acquire_singleton_lock", lambda _path: (object(), "held")
    )
    monkeypatch.setattr(watchers, "_release_singleton_lock", lambda _handle: None)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(runner, "_kanban_alert_rules_tick", failing_alert_tick)

    asyncio.run(runner._kanban_notifications_watcher(interval=1))

    assert alert_ticks == [True]


def test_second_gateway_cannot_evaluate_alert_rules(tmp_path, monkeypatch):
    """A contended alert lease disables the second in-memory evaluator."""
    from gateway import kanban_watchers as watchers
    from hermes_cli import kanban_db as _kb

    first = _make_runner()
    second = _make_runner()
    first._running = False
    second_tick = MagicMock()
    lock_states = iter([(object(), "held"), (None, "contended"), (None, "contended")])
    sleep_calls = []

    async def fake_sleep(_delay):
        sleep_calls.append(True)
        if len(sleep_calls) >= 2:
            second._running = False

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": False,
                "alerts": {"enabled": True, "channel_id": "operator-room"},
            }
        },
    )
    monkeypatch.setattr(_kb, "kanban_home", lambda: tmp_path)
    monkeypatch.setattr(
        watchers, "_acquire_singleton_lock", lambda _path: next(lock_states)
    )
    monkeypatch.setattr(watchers, "_release_singleton_lock", lambda _handle: None)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(second, "_kanban_alert_rules_tick", second_tick)

    asyncio.run(first._kanban_notifications_watcher())
    asyncio.run(second._kanban_notifications_watcher())

    second_tick.assert_not_called()


def test_contended_alert_gateway_takes_over_after_owner_exits(tmp_path, monkeypatch):
    """A live standby retries the singleton lock and becomes alert leader."""
    from gateway import kanban_watchers as watchers
    from hermes_cli import kanban_db as _kb

    runner = _make_runner()
    alert_ticks = []
    lock_states = iter([(None, "contended"), (object(), "held")])
    sleep_calls = []

    async def fake_sleep(_delay):
        sleep_calls.append(True)
        if alert_ticks:
            runner._running = False

    async def alert_tick(config, state):
        alert_ticks.append((config, state))

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": False,
                "alerts": {"enabled": True, "channel_id": "operator-room"},
            }
        },
    )
    monkeypatch.setattr(_kb, "kanban_home", lambda: tmp_path)
    monkeypatch.setattr(
        watchers, "_acquire_singleton_lock", lambda _path: next(lock_states)
    )
    monkeypatch.setattr(watchers, "_release_singleton_lock", lambda _handle: None)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(runner, "_kanban_alert_rules_tick", alert_tick)

    asyncio.run(runner._kanban_notifications_watcher(interval=1))

    assert len(alert_ticks) == 1
    assert runner._kanban_alerts_lock_handle is None


def test_notifier_watcher_runs_when_dispatch_enabled():
    """dispatch_in_gateway=true proceeds past the gate to the board fan-out."""
    runner = _make_runner(with_adapter=True)
    past_gate = []
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        # Stop after the initial delay + first per-interval sleep so the loop
        # body runs exactly once.
        if len(sleep_calls) >= 2:
            runner._running = False

    async def fake_kanban_off_loop(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    import hermes_cli.kanban_db as _kb

    with patch("hermes_cli.config.load_config", return_value=_fake_config(True)):
        with patch.object(
            _kb, "list_boards",
            side_effect=lambda *a, **kw: past_gate.append(True) or [],
        ):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                with patch.object(
                    GatewayRunner, "_kanban_off_loop", fake_kanban_off_loop,
                ):
                    asyncio.run(runner._kanban_notifications_watcher())

    assert past_gate, "list_boards should be called when dispatch_in_gateway=true"


def test_dispatcher_watcher_spawns_closeout_units_per_board(tmp_path, monkeypatch):
    """The per-board dispatch tick launches closeout units, never inline work."""
    from gateway import kanban_watchers as watchers
    from hermes_cli import config as config_mod
    from hermes_cli import kanban_closeout as closeout
    from hermes_cli import kanban_db as _kb

    runner = _make_runner()
    boards = [{"slug": "alpha"}, {"slug": "beta"}]
    closeout_calls = []
    sleep_calls = []

    async def fake_sleep(_delay):
        sleep_calls.append(True)
        # Initial 5s delay, then one interval sleep after the first full tick.
        if len(sleep_calls) >= 2:
            runner._running = False

    async def fake_kanban_off_loop(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    dispatch_result = SimpleNamespace(
        spawned=[], reclaimed=0, crashed=[], timed_out=[], promoted=0, auto_blocked=[]
    )
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
            }
        },
    )
    lock_handle = object()
    monkeypatch.setattr(
        watchers, "_acquire_singleton_lock", lambda _path: (lock_handle, "held")
    )
    monkeypatch.setattr(watchers, "_release_singleton_lock", lambda _handle: None)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(GatewayRunner, "_kanban_off_loop", fake_kanban_off_loop)
    monkeypatch.setattr(_kb, "kanban_home", lambda: tmp_path)
    monkeypatch.setattr(_kb, "kanban_db_path", lambda board=None: tmp_path / f"{board}.db")
    monkeypatch.setattr(_kb, "list_boards", lambda include_archived=False: boards)
    monkeypatch.setattr(_kb, "connect", lambda board=None: MagicMock(name=f"conn-{board}"))
    monkeypatch.setattr(_kb, "connect_closing", lambda *a, **k: nullcontext(MagicMock()))
    monkeypatch.setattr(_kb, "dispatch_once", lambda *a, **k: dispatch_result)
    monkeypatch.setattr(_kb, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(_kb, "no_silent_stall_sweep", lambda *a, **k: None)
    monkeypatch.setattr(_kb, "escalate_silent_blocks_sweep", lambda *a, **k: None)
    monkeypatch.setattr(_kb, "escalate_blocking_scouts_sweep", lambda *a, **k: None)
    monkeypatch.setattr(_kb, "classify_escalations_sweep", lambda *a, **k: None)
    monkeypatch.setattr(_kb, "has_spawnable_ready", lambda *a, **k: False)
    monkeypatch.setattr(_kb, "has_spawnable_review", lambda *a, **k: False)
    monkeypatch.setattr(_kb, "backfill_run_costs", lambda *a, **k: 0)
    monkeypatch.setattr(_kb, "backfill_run_costs_from_sessions", lambda *a, **k: 0)
    monkeypatch.setattr(_kb, "write_kanban_dispatcher_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(
        closeout,
        "spawn_pending_closeouts",
        lambda conn, board, limit=10: closeout_calls.append((board, limit)) or [],
    )

    asyncio.run(runner._kanban_dispatcher_watcher())

    assert closeout_calls == [("alpha", 10), ("beta", 10)]


def test_dispatcher_watcher_skips_when_singleton_lock_unavailable(
    tmp_path, monkeypatch,
):
    """An unavailable advisory lock must disable this dispatch owner."""
    from gateway import kanban_watchers as watchers
    from hermes_cli import config as config_mod
    from hermes_cli import kanban_db as _kb

    runner = _make_runner()
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )
    monkeypatch.setattr(
        watchers, "_acquire_singleton_lock", lambda _path: (None, "unavailable")
    )
    monkeypatch.setattr(_kb, "kanban_home", lambda: tmp_path)
    list_boards = MagicMock(return_value=[])
    monkeypatch.setattr(_kb, "list_boards", list_boards)

    async def no_wait(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)

    asyncio.run(runner._kanban_dispatcher_watcher())

    list_boards.assert_not_called()
    assert runner._kanban_dispatcher_lock_handle is None
