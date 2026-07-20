from __future__ import annotations

import json
import os
import sqlite3
from datetime import timezone
from pathlib import Path

import pytest

import gateway.pa_watcher as watcher
from hermes_cli.pa_chat import PAStore
from hermes_cli.pa_reminders import create_reminder


@pytest.fixture
def isolated_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    receipts_root = tmp_path / "vault" / "03-Agents"
    receipt_dir = receipts_root / "Codex" / "receipts"
    db_path = hermes_home / "kanban.db"
    hermes_home.mkdir(parents=True)
    receipt_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            status TEXT,
            block_kind TEXT,
            freigabe TEXT,
            live_test_depth TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO tasks(id, title, status) VALUES ('t1', 'Build', 'running')"
    )
    conn.execute(
        "INSERT INTO task_events(task_id, kind, payload, created_at) "
        "VALUES ('t1', 'claimed', '{}', 100)"
    )
    conn.commit()
    conn.close()
    store = PAStore()
    store.ensure_schema()
    return {
        "store": store,
        "db_path": db_path,
        "receipts_root": receipts_root,
        "receipt_dir": receipt_dir,
    }


def _insert_event(
    db_path: Path,
    *,
    task_id: str = "t1",
    kind: str,
    payload: dict[str, object] | None = None,
    created_at: int,
) -> int:
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "INSERT INTO task_events(task_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (task_id, kind, json.dumps(payload or {}), created_at),
    )
    conn.commit()
    event_id = int(cursor.lastrowid)
    conn.close()
    return event_id


def _event(
    name: str,
    *,
    kind: str = "completed",
    source: str = "kanban_status",
    severity: str = "info",
    expected: bool = False,
    occurred_at: int = 1_700_000_000,
) -> watcher.WatchEvent:
    fingerprint = watcher._fingerprint("test", name)
    return watcher.WatchEvent(
        event_id=watcher._event_id(fingerprint),
        source=source,
        kind=kind,
        severity=severity,
        title=f"Ereignis {name}",
        ref=f"ref-{name}",
        occurred_at=occurred_at,
        detail=f"detail-{name}",
        fingerprint=fingerprint,
        expected=expected,
    )


def _ingest_and_accept(
    store: PAStore,
    events: list[watcher.WatchEvent],
    *,
    now: int,
) -> None:
    watcher._ingest_events(store, events=events, state_updates={}, now=now)

    def accept_all(
        engine: str,
        prompt: str,
        *,
        model: str,
        image_paths: list[Path],
    ) -> str:
        del engine, prompt, model, image_paths
        return json.dumps(
            {
                "significant": [event.event_id for event in events],
                "reason": "operator-relevant",
            }
        )

    outcome = watcher.judge_candidates(
        store,
        now=now,
        engine_runner=accept_all,
        zone=timezone.utc,
    )
    assert outcome["fallback"] is False


def test_kanban_status_cursor_baselines_then_advances_without_replay(
    isolated_watcher: dict[str, object],
) -> None:
    db_path = isolated_watcher["db_path"]
    assert isinstance(db_path, Path)
    cursor, baseline = watcher.collect_kanban_status_events(
        db_path, board="default", cursor=None
    )
    assert cursor == 1
    assert baseline == []

    raw_id = _insert_event(
        db_path,
        kind="completed",
        payload={"summary": "green"},
        created_at=101,
    )
    next_cursor, events = watcher.collect_kanban_status_events(
        db_path, board="default", cursor=cursor
    )
    assert next_cursor == raw_id
    assert [(event.kind, event.ref) for event in events] == [("completed", "t1")]
    final_cursor, replay = watcher.collect_kanban_status_events(
        db_path, board="default", cursor=next_cursor
    )
    assert final_cursor == next_cursor
    assert replay == []


def test_red_gate_cursor_uses_own_stream_and_concrete_predicate(
    isolated_watcher: dict[str, object],
) -> None:
    db_path = isolated_watcher["db_path"]
    assert isinstance(db_path, Path)
    cursor, baseline = watcher.collect_gate_events(
        db_path, board="default", cursor=None
    )
    assert baseline == []

    raw_id = _insert_event(
        db_path,
        kind="worker_gate_blocked",
        payload={"command": "gate", "returncode": 1},
        created_at=102,
    )
    next_cursor, events = watcher.collect_gate_events(
        db_path, board="default", cursor=cursor
    )
    assert next_cursor == raw_id
    assert len(events) == 1
    assert events[0].source == "red_gate"
    assert events[0].severity == "critical"

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE tasks SET status='scheduled', freigabe='operator' WHERE id='t1'"
    )
    conn.commit()
    conn.close()
    held_id = _insert_event(
        db_path,
        kind="status",
        payload={"to": "scheduled"},
        created_at=103,
    )
    held_cursor, held = watcher.collect_gate_events(
        db_path, board="default", cursor=next_cursor
    )
    assert held_cursor == held_id
    assert [event.kind for event in held] == ["operator_release_required"]


def test_session_cursor_diff_marks_expected_and_unexpected_exits() -> None:
    current = [
        {
            "source": "tmux",
            "label": "work:1 codex",
            "tmux_session": "work",
            "tmux_window": "1",
            "since": 100,
        },
        {
            "source": "kanban",
            "label": "t_done",
            "task_id": "t_done",
            "since": 101,
        },
    ]
    snapshot, baseline = watcher.diff_agent_sessions(
        None, current, terminal_task_ids=set(), now=200
    )
    assert baseline == []

    next_snapshot, exits = watcher.diff_agent_sessions(
        snapshot,
        [],
        terminal_task_ids={"t_done"},
        now=300,
    )
    assert json.loads(next_snapshot) == {}
    by_ref = {event.ref: event for event in exits}
    assert by_ref["t_done"].expected is True
    assert by_ref["work:1 codex"].expected is False


def test_receipt_cursor_is_strict_mtime_and_scan_is_bounded(
    isolated_watcher: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = isolated_watcher["receipts_root"]
    receipt_dir = isolated_watcher["receipt_dir"]
    assert isinstance(root, Path)
    assert isinstance(receipt_dir, Path)
    first = receipt_dir / "first-receipt.md"
    first.write_text("# first\n", encoding="utf-8")
    os.utime(first, ns=(100_000_000_000, 100_000_000_000))
    cursor, baseline = watcher.scan_new_receipts(root, cursor_mtime_ns=None)
    assert baseline == []

    second = receipt_dir / "second-receipt.md"
    second.write_text("# second\n", encoding="utf-8")
    os.utime(second, ns=(101_000_000_000, 101_000_000_000))
    next_cursor, events = watcher.scan_new_receipts(
        root, cursor_mtime_ns=cursor
    )
    assert next_cursor == 101_000_000_000
    assert [event.ref for event in events] == [str(second.resolve())]
    final_cursor, replay = watcher.scan_new_receipts(
        root, cursor_mtime_ns=next_cursor
    )
    assert final_cursor == next_cursor
    assert replay == []
    assert watcher.RECEIPT_SCAN_MAX_FILES == 5_000


def test_prefilter_drops_heartbeats_and_expected_exits_only() -> None:
    heartbeat = _event("heartbeat", kind="heartbeat")
    expected = _event(
        "expected",
        kind="session_exit",
        source="session_exit",
        expected=True,
    )
    unexpected = _event(
        "unexpected",
        kind="session_exit",
        source="session_exit",
        expected=False,
    )
    assert watcher.prefilter_event(heartbeat) == (False, "heartbeat")
    assert watcher.prefilter_event(expected) == (False, "expected_exit")
    assert watcher.prefilter_event(unexpected) == (True, "rule_candidate")


def test_same_kanban_event_prefers_red_gate_representation() -> None:
    fingerprint = watcher._fingerprint("kanban", "same-row")
    status = watcher.WatchEvent(
        event_id=watcher._event_id(fingerprint),
        source="kanban_status",
        kind="blocked",
        severity="warning",
        title="blocked",
        ref="t1",
        occurred_at=100,
        detail="status",
        fingerprint=fingerprint,
    )
    gate = watcher.WatchEvent(
        event_id=watcher._event_id(fingerprint),
        source="red_gate",
        kind="blocked:review_revision",
        severity="warning",
        title="review gate",
        ref="t1",
        occurred_at=100,
        detail="gate",
        fingerprint=fingerprint,
    )
    assert watcher._merge_events([status, gate]) == [gate]


def test_dedup_quiet_queue_bundle_and_ten_minute_rate_limit(
    isolated_watcher: dict[str, object],
) -> None:
    store = isolated_watcher["store"]
    assert isinstance(store, PAStore)
    first = _event("first", severity="warning", occurred_at=23 * 3_600)
    inserted = watcher._ingest_events(
        store,
        events=[first, first],
        state_updates={},
        now=23 * 3_600,
    )
    assert inserted == 1
    _ingest_and_accept(store, [first], now=23 * 3_600)
    quiet = watcher.deliver_pending_bundle(
        store, now=23 * 3_600, zone=timezone.utc
    )
    assert quiet == {"delivered": 0, "reason": "quiet_hours"}
    still_quiet = watcher.deliver_pending_bundle(
        store, now=7 * 3_600 + 30 * 60, zone=timezone.utc
    )
    assert still_quiet == {"delivered": 0, "reason": "quiet_hours"}

    first_delivery_at = 24 * 3_600 + 7 * 3_600 + 31 * 60
    delivered = watcher.deliver_pending_bundle(
        store, now=first_delivery_at, zone=timezone.utc
    )
    assert delivered["delivered"] == 1
    second = _event("second", severity="critical", occurred_at=first_delivery_at + 1)
    _ingest_and_accept(store, [second], now=first_delivery_at + 1)
    limited = watcher.deliver_pending_bundle(
        store,
        now=first_delivery_at + watcher.DELIVERY_RATE_LIMIT_SECONDS - 1,
        zone=timezone.utc,
    )
    assert limited == {"delivered": 0, "reason": "rate_limited"}
    delivered_again = watcher.deliver_pending_bundle(
        store,
        now=first_delivery_at + watcher.DELIVERY_RATE_LIMIT_SECONDS,
        zone=timezone.utc,
    )
    assert delivered_again["delivered"] == 1

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pa_feed").fetchone()[0] == 2
        messages = conn.execute(
            "SELECT engine, content FROM pa_messages WHERE engine='pa-watcher' "
            "ORDER BY id"
        ).fetchall()
        assert len(messages) == 2
        assert all(row["engine"] == "pa-watcher" for row in messages)
        assert "Task-ID ref-first" in messages[0]["content"]


def test_engine_failure_and_daily_cap_fall_back_to_rule_prefilter(
    isolated_watcher: dict[str, object],
) -> None:
    store = isolated_watcher["store"]
    assert isinstance(store, PAStore)
    failed = _event("engine-failure")
    watcher._ingest_events(store, events=[failed], state_updates={}, now=100)

    def explode(*args: object, **kwargs: object) -> str:
        raise RuntimeError("lane down")

    outcome = watcher.judge_candidates(
        store,
        now=100,
        engine_runner=explode,
        zone=timezone.utc,
    )
    assert outcome == {"judged": 1, "engine_called": True, "fallback": True}
    with store.connect() as conn:
        row = conn.execute(
            "SELECT status, reason FROM pa_watcher_events WHERE fingerprint=?",
            (failed.fingerprint,),
        ).fetchone()
        assert row["status"] == "pending"
        assert "engine_error_prefilter_fallback" in row["reason"]

        watcher._state_set(conn, "engine_day", "1970-01-01", now=100)
        watcher._state_set(
            conn,
            "engine_calls",
            watcher.ENGINE_DAILY_CALL_CAP,
            now=100,
        )
    capped = _event("capped")
    watcher._ingest_events(store, events=[capped], state_updates={}, now=101)
    called = False

    def should_not_run(*args: object, **kwargs: object) -> str:
        nonlocal called
        called = True
        return "{}"

    cap_outcome = watcher.judge_candidates(
        store,
        now=101,
        engine_runner=should_not_run,
        zone=timezone.utc,
    )
    assert cap_outcome == {
        "judged": 1,
        "engine_called": False,
        "fallback": True,
    }
    assert called is False


def test_full_tick_baselines_all_sources_and_records_health_timestamp(
    isolated_watcher: dict[str, object],
) -> None:
    store = isolated_watcher["store"]
    db_path = isolated_watcher["db_path"]
    receipts_root = isolated_watcher["receipts_root"]
    assert isinstance(store, PAStore)
    assert isinstance(db_path, Path)
    assert isinstance(receipts_root, Path)

    outcome = watcher.run_watcher_tick(
        now=1_700_000_000,
        store=store,
        engine_runner=lambda *args, **kwargs: '{"significant":[],"reason":"none"}',
        zone=timezone.utc,
        agents_builder=lambda: {
            "agents": [{"source": "tmux", "label": "work:1 codex", "since": 1}],
            "errors": [],
        },
        receipts_root=receipts_root,
        board_paths=[("default", db_path)],
    )
    assert outcome["collected"] == 0
    with store.connect() as conn:
        assert watcher._state_int(conn, "last_tick_at") == 1_700_000_000
        assert watcher._state_int(conn, "interval_seconds") == 60
        assert watcher._state_int(conn, "enabled") == 1


def test_due_reminders_fire_once_outside_quiet_hours_and_skip_future(
    isolated_watcher: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = isolated_watcher["store"]
    assert isinstance(store, PAStore)
    quiet_now = 23 * 3_600
    due_id = create_reminder(
        due_at_utc="1970-01-01T22:59:00Z",
        title="Medikament",
        body="Jetzt einnehmen",
        store=store,
    )
    future_id = create_reminder(
        due_at_utc="1970-01-02T00:00:00Z",
        title="Morgen",
        store=store,
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "hermes_cli.pa_push.send_pa_push",
        lambda **kwargs: calls.append(kwargs) or {"sent": 1},
    )

    first = watcher.fire_due_reminders(store, now=quiet_now)
    second = watcher.fire_due_reminders(store, now=quiet_now)

    assert watcher.is_quiet_time(quiet_now, zone=timezone.utc) is True
    assert first == {"fired": 1, "errors": []}
    assert second == {"fired": 0, "errors": []}
    assert calls == [
        {
            "title": "Medikament",
            "body": "Jetzt einnehmen",
            "tag": f"reminder:{due_id}",
            "url": "/control/projekte?inbox=open",
        }
    ]
    with store.connect() as conn:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM reminders")
        }
    assert statuses == {due_id: "fired", future_id: "pending"}


def test_reminder_push_failure_does_not_kill_tick_or_mark_fired(
    isolated_watcher: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = isolated_watcher["store"]
    db_path = isolated_watcher["db_path"]
    receipts_root = isolated_watcher["receipts_root"]
    assert isinstance(store, PAStore)
    assert isinstance(db_path, Path)
    assert isinstance(receipts_root, Path)
    reminder_id = create_reminder(
        due_at_utc="2023-11-14T22:12:00Z",
        title="Fehlerfall",
        store=store,
    )

    def explode(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("push down")

    monkeypatch.setattr("hermes_cli.pa_push.send_pa_push", explode)
    outcome = watcher.run_watcher_tick(
        now=1_700_000_000,
        store=store,
        engine_runner=lambda *args, **kwargs: '{"significant":[],"reason":"none"}',
        zone=timezone.utc,
        agents_builder=lambda: {"agents": [], "errors": []},
        receipts_root=receipts_root,
        board_paths=[("default", db_path)],
    )

    assert outcome["reminders"]["fired"] == 0
    assert "push down" in outcome["reminders"]["errors"][0]
    assert outcome["collected"] == 0
    with store.connect() as conn:
        row = conn.execute(
            "SELECT status, fired_at FROM reminders WHERE id=?", (reminder_id,)
        ).fetchone()
        assert dict(row) == {"status": "pending", "fired_at": None}
        assert watcher._state_int(conn, "last_tick_at") == 1_700_000_000


def test_config_defaults_enabled_and_can_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    assert watcher.load_watcher_config() == {
        "enabled": True,
        "interval_seconds": 60,
    }
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"pa_watcher": {"enabled": False, "interval_seconds": 75}},
    )
    assert watcher.load_watcher_config() == {
        "enabled": False,
        "interval_seconds": 75,
    }
