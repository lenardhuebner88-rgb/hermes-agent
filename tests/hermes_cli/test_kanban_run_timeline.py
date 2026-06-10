"""Tests for the per-run timeline (night-sprint F3) — kanban_db.run_timeline."""

from __future__ import annotations

import json
import time as _time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

T0 = 1_900_000_000


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


def _mk_run(conn, task_id, *, started_at=T0, ended_at=T0 + 600,
            status="done", outcome="completed", error=None, profile="coder"):
    with kb.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, error, "
            "started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, profile, status, outcome, error, started_at, ended_at),
        )
    return cur.lastrowid


def _mk_event(conn, task_id, kind, *, at, run_id=None, payload=None):
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, run_id,
             kind, json.dumps(payload) if payload is not None else None, at),
        )


def test_timeline_sorts_and_computes_offsets_and_deltas(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Zeitleiste")
        rid = _mk_run(conn, tid, started_at=T0, ended_at=T0 + 300)
        # Inserted out of order — the timeline must sort by time.
        _mk_event(conn, tid, "heartbeat", at=T0 + 120, run_id=rid)
        _mk_event(conn, tid, "spawned", at=T0 + 5, run_id=rid)
        _mk_event(conn, tid, "commented", at=T0 + 200, run_id=rid)
        tl = kb.run_timeline(conn, rid)

    kinds = [it["kind"] for it in tl["items"]]
    assert kinds == ["run_started", "spawned", "heartbeat", "commented", "run_ended"]
    offsets = [it["offset_seconds"] for it in tl["items"]]
    assert offsets == [0, 5, 120, 200, 300]
    deltas = [it["delta_seconds"] for it in tl["items"]]
    assert deltas == [0, 5, 115, 80, 100]
    assert tl["count"] == 5
    assert tl["run"]["duration_seconds"] == 300


def test_timeline_frames_run_with_synthetic_start_and_end(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Rahmen")
        rid = _mk_run(conn, tid, status="failed", outcome="crashed", error="Boom")
        tl = kb.run_timeline(conn, rid)

    assert tl["items"][0]["kind"] == "run_started"
    assert tl["items"][0]["payload"]["profile"] == "coder"
    end = tl["items"][-1]
    assert end["kind"] == "run_ended"
    assert end["payload"]["outcome"] == "crashed"
    assert end["payload"]["error"] == "Boom"


def test_timeline_includes_window_scoped_null_run_events(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Legacy")
        rid = _mk_run(conn, tid, started_at=T0, ended_at=T0 + 100)
        _mk_event(conn, tid, "claimed", at=T0 + 10, run_id=None)      # in window
        _mk_event(conn, tid, "created", at=T0 - 500, run_id=None)     # before
        _mk_event(conn, tid, "archived", at=T0 + 999, run_id=None)    # after
        tl = kb.run_timeline(conn, rid)

    kinds = [it["kind"] for it in tl["items"]]
    assert "claimed" in kinds
    assert "created" not in kinds and "archived" not in kinds
    claimed = next(it for it in tl["items"] if it["kind"] == "claimed")
    assert claimed["source"] == "task"


def test_timeline_excludes_events_of_other_runs(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Zwei Runs")
        rid1 = _mk_run(conn, tid, started_at=T0, ended_at=T0 + 100)
        rid2 = _mk_run(conn, tid, started_at=T0 + 10, ended_at=T0 + 90)
        _mk_event(conn, tid, "spawned", at=T0 + 20, run_id=rid2)
        tl = kb.run_timeline(conn, rid1)

    # rid2's run-scoped event must not bleed into rid1's timeline even
    # though its timestamp lies inside rid1's window.
    assert all(it["kind"] != "spawned" for it in tl["items"])


def test_timeline_returns_none_for_unknown_run(kanban_home):
    with kb.connect() as conn:
        assert kb.run_timeline(conn, 424242) is None


def test_timeline_caps_events_and_flags_truncation(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Cap")
        rid = _mk_run(conn, tid, started_at=T0, ended_at=T0 + 5000)
        for i in range(30):
            _mk_event(conn, tid, "heartbeat", at=T0 + i, run_id=rid)
        tl = kb.run_timeline(conn, rid, max_events=10)

    assert tl["truncated"] is True
    # 10 events + synthetic start/end
    assert tl["count"] == 12


def test_timeline_with_200_events_is_fast(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Last")
        rid = _mk_run(conn, tid, started_at=T0, ended_at=T0 + 10_000)
        with kb.write_txn(conn):
            conn.executemany(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, 'heartbeat', NULL, ?)",
                [(tid, rid, T0 + i * 10) for i in range(200)],
            )
        t_start = _time.monotonic()
        tl = kb.run_timeline(conn, rid)
        elapsed = _time.monotonic() - t_start

    assert tl["count"] == 202
    assert elapsed < 0.5, f"timeline took {elapsed:.3f}s (budget 0.5s)"


def test_timeline_open_run_has_no_end_item(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Offen")
        rid = _mk_run(conn, tid, started_at=int(_time.time()) - 60,
                      ended_at=None, status="running", outcome=None)
        _mk_event(conn, tid, "spawned", at=int(_time.time()) - 50, run_id=rid)
        tl = kb.run_timeline(conn, rid)

    assert tl["items"][0]["kind"] == "run_started"
    assert all(it["kind"] != "run_ended" for it in tl["items"])
    assert tl["run"]["ended_at"] is None
