"""Tests for B1–B4 backend slices (workers/active + activity + heartbeat tokens + hold/resume/restart).

Covers:
  B1 – GET /workers/active returns step_key, model_override, effective_model
  B2 – GET /tasks/{id}/activity returns newest-first; limit cap enforced
  B3 – heartbeat_worker with tokens updates task_runs; NULL heartbeat does NOT clobber
  B4 – worker action hold parks task (no re-dispatch), resume releases, restart+model_override sets override
"""
from __future__ import annotations

import importlib.util
import json
import signal
import secrets
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    mod_name = "hermes_dashboard_plugin_kanban_b1b4_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(mod_name, plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_plugin_router():
    return _load_plugin_module().router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _insert_run(conn, task_id, *, worker_pid=None, ended_at=None, profile=None, step_key=None):
    """Insert a task_runs row directly and return run_id."""
    lock = secrets.token_hex(8)
    future = int(time.time()) + 3600
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, status, claim_lock, claim_expires, worker_pid, started_at, ended_at, profile, step_key) "
        "VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?)",
        (task_id, lock, future, worker_pid, int(time.time()), ended_at, profile, step_key),
    )
    conn.commit()
    return cur.lastrowid


def _make_running_task(*, title="test-task", assignee="coder", model_override=None):
    """Create a task, set it running, and insert a run. Return (task_id, run_id)."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title=title, assignee=assignee)
        if model_override:
            kb.set_task_model_override(conn, task_id, model_override)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        run_id = _insert_run(conn, task_id, worker_pid=9999, profile=assignee, step_key="step-build")
    finally:
        conn.close()
    return task_id, run_id


def _local_claim_lock(suffix: str) -> str:
    return f"{kb._claimer_id().split(':', 1)[0]}:{suffix}"


def _insert_claimed_running_task(
    conn,
    task_id: str,
    *,
    worker_pid: int = 43210,
    claim_lock: str | None = None,
) -> int:
    now = int(time.time())
    lock = claim_lock or _local_claim_lock(f"chain-{secrets.token_hex(4)}")
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
        "worker_pid=?, started_at=? WHERE id=?",
        (lock, now + 3600, worker_pid, now, task_id),
    )
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, status, claim_lock, claim_expires, worker_pid, started_at, profile) "
        "VALUES (?, 'running', ?, ?, ?, ?, 'coder')",
        (task_id, lock, now + 3600, worker_pid, now),
    )
    run_id = int(cur.lastrowid or 0)
    conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
    conn.commit()
    return run_id


def _make_cancel_chain(conn):
    root = kb.create_task(conn, title="chain sink", assignee="coder")
    running = kb.create_task(conn, title="running parent", assignee="coder")
    ready = kb.create_task(conn, title="ready parent", assignee="coder")
    kb.link_tasks(conn, running, root)
    kb.link_tasks(conn, ready, root)
    now = int(time.time())
    conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (now, root))
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (ready,))
    conn.commit()
    _insert_claimed_running_task(conn, running)
    return root, running, ready


def _dead_signal_recorder(calls: list[tuple[int, signal.Signals]]):
    def _signal(pid: int, sig: signal.Signals) -> None:
        calls.append((pid, sig))
        raise ProcessLookupError(pid)
    return _signal


# ---------------------------------------------------------------------------
# B1 — workers/active: step_key + model_override + effective_model
# ---------------------------------------------------------------------------

def test_workers_active_b1_fields_present(client):
    """B1: workers/active includes step_key, model_override, effective_model keys."""
    task_id, run_id = _make_running_task(title="b1-worker")
    r = client.get("/api/plugins/kanban/workers/active")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    w = body["workers"][0]
    assert "step_key" in w
    assert "model_override" in w
    assert "effective_model" in w


def test_workers_active_b1_step_key_value(client):
    """B1: step_key from task_runs.step_key is passed through."""
    task_id, run_id = _make_running_task(title="b1-step")
    r = client.get("/api/plugins/kanban/workers/active")
    assert r.status_code == 200
    w = r.json()["workers"][0]
    assert w["step_key"] == "step-build"


def test_workers_active_b1_model_override(client):
    """B1: model_override from tasks.model_override is passed through; effective_model matches."""
    task_id, run_id = _make_running_task(title="b1-model", model_override="claude-opus-4")
    r = client.get("/api/plugins/kanban/workers/active")
    assert r.status_code == 200
    w = r.json()["workers"][0]
    assert w["model_override"] == "claude-opus-4"
    # effective_model = model_override when set
    assert w["effective_model"] == "claude-opus-4"


def test_workers_active_b1_no_model_override(client):
    """B1: when model_override is NULL, effective_model falls back to lane model (may be None in test env)."""
    task_id, run_id = _make_running_task(title="b1-nomodel")
    r = client.get("/api/plugins/kanban/workers/active")
    assert r.status_code == 200
    w = r.json()["workers"][0]
    assert w["model_override"] is None
    # effective_model is None or a string — must be present as key
    assert "effective_model" in w


def test_workers_active_token_status_no_live_sample(client):
    """workers/active is explicit when a running worker has no live token sample yet."""
    task_id, run_id = _make_running_task(title="b1-token-null")
    r = client.get("/api/plugins/kanban/workers/active")
    assert r.status_code == 200
    w = r.json()["workers"][0]
    assert w["run_id"] == run_id
    assert w["input_tokens"] is None
    assert w["output_tokens"] is None
    assert w["token_status"] == "no_live_sample"
    assert "live token" in w["token_status_reason"]


def test_workers_active_token_status_live_values(client):
    """workers/active passes through live token counters when the run has them."""
    task_id, run_id = _make_running_task(title="b1-token-live")
    conn = kb.connect()
    try:
        conn.execute(
            "UPDATE task_runs SET input_tokens=?, output_tokens=? WHERE id=?",
            (1234, 56, run_id),
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/workers/active")
    assert r.status_code == 200
    w = r.json()["workers"][0]
    assert w["run_id"] == run_id
    assert w["input_tokens"] == 1234
    assert w["output_tokens"] == 56
    assert w["token_status"] == "live"
    assert w["token_status_reason"] is None


# ---------------------------------------------------------------------------
# B2 — Task activity endpoint
# ---------------------------------------------------------------------------

def _append_events(task_id: str, n: int, *, note_prefix="note"):
    """Append n heartbeat events to task_id using kanban_db directly."""
    conn = kb.connect()
    try:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        for i in range(n):
            with kb.write_txn(conn):
                conn.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, "heartbeat", json.dumps({"note": f"{note_prefix}-{i}"}), int(time.time()) + i),
                )
    finally:
        conn.close()


def test_activity_empty(client):
    """B2: activity endpoint responds 200 with task_id and a list (create_task emits a 'created' event)."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="b2-empty")
    finally:
        conn.close()
    r = client.get(f"/api/plugins/kanban/tasks/{task_id}/activity")
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == task_id
    assert isinstance(body["events"], list)
    # Verify response shape: every entry has the required keys
    for ev in body["events"]:
        assert "id" in ev and "run_id" in ev and "kind" in ev and "note" in ev and "at" in ev


def test_activity_newest_first(client):
    """B2: events are returned newest-first (by id DESC)."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="b2-order")
    finally:
        conn.close()
    _append_events(task_id, 3, note_prefix="ev")
    r = client.get(f"/api/plugins/kanban/tasks/{task_id}/activity")
    assert r.status_code == 200
    events = r.json()["events"]
    # At least 3 heartbeat events (plus a 'created' event from create_task)
    assert len(events) >= 3
    ids = [e["id"] for e in events]
    assert ids == sorted(ids, reverse=True), "Events must be newest-first (id DESC)"
    # The most recent events must be heartbeats with note fields
    heartbeats = [e for e in events if e["kind"] == "heartbeat"]
    assert len(heartbeats) == 3
    assert heartbeats[0]["note"] == "ev-2"  # newest heartbeat first


def test_activity_note_extracted(client):
    """B2: note field is extracted from payload.note."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="b2-note")
    finally:
        conn.close()
    _append_events(task_id, 1, note_prefix="mynoteval")
    r = client.get(f"/api/plugins/kanban/tasks/{task_id}/activity")
    assert r.status_code == 200
    ev = r.json()["events"][0]
    assert ev["note"] == "mynoteval-0"
    assert ev["kind"] == "heartbeat"
    assert "at" in ev
    assert "run_id" in ev


def test_activity_limit_default(client):
    """B2: default limit is 12."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="b2-limit")
    finally:
        conn.close()
    _append_events(task_id, 20)
    r = client.get(f"/api/plugins/kanban/tasks/{task_id}/activity")
    assert r.status_code == 200
    assert len(r.json()["events"]) == 12


def test_activity_limit_cap_at_50(client):
    """B2: limit is hard-capped at 50 regardless of query param."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="b2-cap")
    finally:
        conn.close()
    _append_events(task_id, 60)
    r = client.get(f"/api/plugins/kanban/tasks/{task_id}/activity?limit=50")
    assert r.status_code == 200
    assert len(r.json()["events"]) == 50


def test_activity_limit_above_max_rejected(client):
    """B2: limit>50 is rejected by the query param validator (422)."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="b2-reject")
    finally:
        conn.close()
    r = client.get(f"/api/plugins/kanban/tasks/{task_id}/activity?limit=51")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# B3 — heartbeat_worker with token sampling
# ---------------------------------------------------------------------------

def test_heartbeat_worker_writes_tokens(kanban_home):
    """B3: heartbeat_worker with input/output_tokens updates task_runs columns."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="b3-tokens", assignee="coder")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        run_id = _insert_run(conn, task_id, worker_pid=1111)
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
        conn.commit()

        ok = kb.heartbeat_worker(conn, task_id, note="step1", input_tokens=100, output_tokens=50)
        assert ok is True

        row = conn.execute(
            "SELECT input_tokens, output_tokens FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50
    finally:
        conn.close()


def test_heartbeat_worker_no_tokens_no_clobber(kanban_home):
    """B3: a later heartbeat without tokens does NOT overwrite previously stored values (COALESCE)."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="b3-coalesce", assignee="coder")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        run_id = _insert_run(conn, task_id, worker_pid=2222)
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
        conn.commit()

        # First heartbeat sets tokens
        kb.heartbeat_worker(conn, task_id, note="step1", input_tokens=200, output_tokens=80)
        # Second heartbeat — no tokens — must NOT clobber
        kb.heartbeat_worker(conn, task_id, note="step2")

        row = conn.execute(
            "SELECT input_tokens, output_tokens FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert row["input_tokens"] == 200, "input_tokens must not be clobbered by None"
        assert row["output_tokens"] == 80, "output_tokens must not be clobbered by None"
    finally:
        conn.close()


def test_heartbeat_worker_partial_tokens(kanban_home):
    """B3: only input_tokens provided leaves output_tokens untouched."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="b3-partial", assignee="coder")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        run_id = _insert_run(conn, task_id, worker_pid=3333)
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
        conn.commit()

        kb.heartbeat_worker(conn, task_id, input_tokens=999, output_tokens=111)
        # Second heartbeat: update only input_tokens
        kb.heartbeat_worker(conn, task_id, input_tokens=1500)

        row = conn.execute(
            "SELECT input_tokens, output_tokens FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert row["input_tokens"] == 1500
        assert row["output_tokens"] == 111, "output_tokens must not be clobbered when only input_tokens changes"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B4 — worker actions: hold / resume / restart with model_override
# ---------------------------------------------------------------------------

def _make_running_task_with_run(client, *, title="b4-task", assignee="coder"):
    """Create a running task with a live open run and return (task_id, run_id)."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title=title, assignee=assignee)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        run_id = _insert_run(conn, task_id, worker_pid=5555)
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
        conn.commit()
    finally:
        conn.close()
    return task_id, run_id


def test_b4_hold_parks_task_blocked(client):
    """B4 hold: reclaims worker and parks task as blocked (status='blocked')."""
    task_id, run_id = _make_running_task_with_run(client, title="b4-hold")
    r = client.post(
        f"/api/plugins/kanban/workers/{run_id}/action",
        json={"action": "hold", "confirm": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "hold"

    conn = kb.connect()
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["status"] == "blocked", f"Expected blocked, got {row['status']}"
    finally:
        conn.close()


def test_b4_hold_no_redispatch(client):
    """B4 hold: a held (blocked) task is NOT re-dispatched by auto_retry_blocked_tasks
    because the reason 'operator hold' contains the word 'operator' which matches
    _AUTO_RETRY_QUESTION_RE (\\boperator\\b), classifying it as operator_question."""
    task_id, run_id = _make_running_task_with_run(client, title="b4-nodeispatch")
    client.post(
        f"/api/plugins/kanban/workers/{run_id}/action",
        json={"action": "hold", "confirm": True},
    )

    conn = kb.connect()
    try:
        # Calling auto_retry_blocked_tasks should not flip the held task back to ready.
        kb.auto_retry_blocked_tasks(conn, backoff_seconds=0, retry_limit=99)
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["status"] == "blocked", "operator_hold blocked task must not be auto-retried"
    finally:
        conn.close()


def test_b4_resume_releases_hold(client):
    """B4 resume: unblocks the held task back to ready/todo."""
    task_id, run_id = _make_running_task_with_run(client, title="b4-resume")
    client.post(
        f"/api/plugins/kanban/workers/{run_id}/action",
        json={"action": "hold", "confirm": True},
    )

    r = client.post(
        f"/api/plugins/kanban/workers/{run_id}/action",
        json={"action": "resume", "confirm": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "resume"

    conn = kb.connect()
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["status"] in ("ready", "todo"), f"Expected ready/todo after resume, got {row['status']}"
    finally:
        conn.close()


def test_b4_restart_with_model_override(client):
    """B4 restart: model_override is set on the task before re-dispatch is attempted."""
    task_id, run_id = _make_running_task_with_run(client, title="b4-restart-model")

    r = client.post(
        f"/api/plugins/kanban/workers/{run_id}/action",
        json={"action": "restart", "confirm": True, "model_override": "claude-sonnet-4"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "restart"

    conn = kb.connect()
    try:
        row = conn.execute("SELECT model_override FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["model_override"] == "claude-sonnet-4", (
            f"model_override not set before re-dispatch: {row['model_override']}"
        )
    finally:
        conn.close()


def test_b4_hold_requires_confirm(client):
    """B4: hold without confirm:true is refused."""
    task_id, run_id = _make_running_task_with_run(client, title="b4-noconfirm")
    r = client.post(
        f"/api/plugins/kanban/workers/{run_id}/action",
        json={"action": "hold", "confirm": False},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_b4_unknown_action_rejected(client):
    """B4: unknown action name returns 400."""
    task_id, run_id = _make_running_task_with_run(client, title="b4-badaction")
    r = client.post(
        f"/api/plugins/kanban/workers/{run_id}/action",
        json={"action": "explode", "confirm": True},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# FIX 1 — hold_task atomicity tests
# ---------------------------------------------------------------------------

def test_hold_task_goes_directly_to_blocked(kanban_home):
    """hold_task: a running task transitions directly to 'blocked' — never observable as 'ready'."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="hold-atomic", assignee="coder")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        run_id = _insert_run(conn, task_id, worker_pid=7001)
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
        conn.commit()

        # States observed while hold_task is running (we cannot interleave from
        # the outside, but we can confirm the post-condition without 'ready' ever
        # appearing in the event log or the task row).
        ok = kb.hold_task(conn, task_id, reason="operator hold")
        assert ok is True

        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["status"] == "blocked", f"Expected blocked immediately, got {row['status']}"

        # Confirm no 'ready' event was appended (the transition bypasses it).
        ready_events = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id=? AND kind='reclaimed'",
            (task_id,),
        ).fetchone()["n"]
        assert ready_events == 0, "hold_task must not emit a 'reclaimed' event"

        # Confirm a 'blocked' event was emitted (required for _has_sticky_block).
        blocked_event = conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert blocked_event is not None and blocked_event["kind"] == "blocked"
    finally:
        conn.close()


def test_hold_task_not_running_returns_false(kanban_home):
    """hold_task: returns False when task is not running (e.g. todo/ready/blocked/done)."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="hold-nonrunning", assignee="coder")
        # Capture status before the hold attempt.
        before = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()["status"]
        # Task is not running — hold must fail.
        ok = kb.hold_task(conn, task_id, reason="operator hold")
        assert ok is False

        after = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()["status"]
        # Status must be unchanged (hold is a no-op on a non-running task).
        assert after == before, f"hold_task must not change status; was {before!r}, now {after!r}"
        assert after != "blocked", "hold_task on a non-running task must not block it"
    finally:
        conn.close()


def test_hold_task_blocks_auto_retry(kanban_home):
    """hold_task: auto_retry_blocked_tasks does NOT re-dispatch a held task."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="hold-noretry", assignee="coder")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        run_id = _insert_run(conn, task_id, worker_pid=7002)
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
        conn.commit()

        ok = kb.hold_task(conn, task_id, reason="operator hold")
        assert ok is True

        # After hold the auto-retry sweep must leave the task in 'blocked'.
        kb.auto_retry_blocked_tasks(conn, backoff_seconds=0, retry_limit=99)
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["status"] == "blocked", (
            "operator_hold task must not be auto-retried by auto_retry_blocked_tasks"
        )
    finally:
        conn.close()


def test_hold_task_resume_makes_ready(kanban_home):
    """hold_task + unblock_task: resume releases the hold back to ready/todo."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="hold-then-resume", assignee="coder")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        run_id = _insert_run(conn, task_id, worker_pid=7003)
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
        conn.commit()

        ok = kb.hold_task(conn, task_id, reason="operator hold")
        assert ok is True

        released = kb.unblock_task(conn, task_id)
        assert released is True

        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["status"] in ("ready", "todo"), (
            f"Expected ready/todo after resume, got {row['status']}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# S4 — chain actions: cancel_chain + extend
# ---------------------------------------------------------------------------

def test_cancel_chain_holds_open_nodes_terminates_running_skips_done(kanban_home):
    conn = kb.connect()
    try:
        root, running, ready = _make_cancel_chain(conn)
        calls: list[tuple[int, signal.Signals]] = []

        result = kb.cancel_chain(conn, root, signal_fn=_dead_signal_recorder(calls))

        assert set(result["held"]) == {running, ready}
        assert result["terminated"] == [running]
        assert result["skipped"] == [root]
        assert calls == [(43210, signal.SIGTERM)]

        rows = {
            row["id"]: row["status"]
            for row in conn.execute(
                "SELECT id, status FROM tasks WHERE id IN (?, ?, ?)",
                (root, running, ready),
            ).fetchall()
        }
        assert rows[root] == "done"
        assert rows[running] == "blocked"
        assert rows[ready] == "blocked"

        root_event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='chain_cancelled' "
            "ORDER BY id DESC LIMIT 1",
            (root,),
        ).fetchone()
        assert root_event is not None
        payload = json.loads(root_event["payload"])
        assert payload["chain_cancel_root"] == root
        assert set(payload["held"]) == {running, ready}
    finally:
        conn.close()


def test_cancel_chain_endpoint_requires_confirm_without_mutation(kanban_home):
    conn = kb.connect()
    try:
        root, running, ready = _make_cancel_chain(conn)
    finally:
        conn.close()

    plugin = _load_plugin_module()
    result = plugin.cancel_chain_endpoint(root, plugin.ChainCancelBody(confirm=False))
    assert result == {"ok": False, "detail": "confirm required"}

    conn = kb.connect()
    try:
        rows = {
            row["id"]: row["status"]
            for row in conn.execute(
                "SELECT id, status FROM tasks WHERE id IN (?, ?, ?)",
                (root, running, ready),
            ).fetchall()
        }
        assert rows[root] == "done"
        assert rows[running] == "running"
        assert rows[ready] == "ready"
        assert conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='chain_cancelled'",
            (root,),
        ).fetchone() is None
    finally:
        conn.close()


def test_cancel_chain_is_idempotent_on_second_call(kanban_home):
    conn = kb.connect()
    try:
        root, running, ready = _make_cancel_chain(conn)
        calls: list[tuple[int, signal.Signals]] = []

        first = kb.cancel_chain(conn, root, signal_fn=_dead_signal_recorder(calls))
        second = kb.cancel_chain(conn, root, signal_fn=_dead_signal_recorder(calls))

        assert set(first["held"]) == {running, ready}
        assert first["terminated"] == [running]
        assert second["held"] == []
        assert second["terminated"] == []
        assert set(second["skipped"]) == {root, running, ready}
        assert calls == [(43210, signal.SIGTERM)]
    finally:
        conn.close()


def test_cancel_chain_blocks_auto_retry_and_recompute_promotion(kanban_home):
    conn = kb.connect()
    try:
        root, running, ready = _make_cancel_chain(conn)
        kb.cancel_chain(conn, root, signal_fn=_dead_signal_recorder([]))

        blocked_run = kb._latest_blocked_run_for_auto_retry(conn, running)
        assert blocked_run is not None
        reason = (blocked_run["summary"] or blocked_run["error"] or "").strip()
        assert kb._blocked_kind_for_auto_retry(reason) == "operator_question"
        assert kb._has_sticky_block(conn, ready) is True

        kb.auto_retry_blocked_tasks(conn, backoff_seconds=0, retry_limit=99)
        kb.recompute_ready(conn)
        rows = {
            row["id"]: row["status"]
            for row in conn.execute(
                "SELECT id, status FROM tasks WHERE id IN (?, ?)",
                (running, ready),
            ).fetchall()
        }
        assert rows == {running: "blocked", ready: "blocked"}
    finally:
        conn.close()


def test_create_task_with_parent_extends_chain_graph(kanban_home):
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="extend root", assignee="default")
    finally:
        conn.close()

    plugin = _load_plugin_module()
    created = plugin.create_task(
        plugin.CreateTaskBody(
            title="extended chain task",
            body="follow-up body",
            assignee="default",
            parents=[root],
            park=True,
        ),
        board=None,
    )
    new_id = created["task"]["id"]

    conn = kb.connect()
    try:
        link = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
            (root, new_id),
        ).fetchone()
        assert link is not None
    finally:
        conn.close()

    graph = plugin.get_chain_graph(new_id, board=None)
    node_ids = {node["id"] for node in graph["nodes"]}
    assert {root, new_id} <= node_ids


# ---------------------------------------------------------------------------
# FIX 2 — token monotonicity tests
# ---------------------------------------------------------------------------

def test_heartbeat_token_monotone_no_decrease(kanban_home):
    """FIX 2: a later heartbeat with a smaller token count does NOT lower the stored value."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="tok-monotone", assignee="coder")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        run_id = _insert_run(conn, task_id, worker_pid=8001)
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
        conn.commit()

        kb.heartbeat_worker(conn, task_id, input_tokens=500, output_tokens=200)
        # Second heartbeat with smaller values — must NOT lower the stored figures.
        kb.heartbeat_worker(conn, task_id, input_tokens=300, output_tokens=100)

        row = conn.execute(
            "SELECT input_tokens, output_tokens FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert row["input_tokens"] == 500, (
            f"input_tokens must not decrease: expected 500, got {row['input_tokens']}"
        )
        assert row["output_tokens"] == 200, (
            f"output_tokens must not decrease: expected 200, got {row['output_tokens']}"
        )
    finally:
        conn.close()


def test_heartbeat_token_monotone_null_unchanged(kanban_home):
    """FIX 2: a heartbeat with None for tokens leaves the stored value unchanged."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="tok-null", assignee="coder")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        run_id = _insert_run(conn, task_id, worker_pid=8002)
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
        conn.commit()

        kb.heartbeat_worker(conn, task_id, input_tokens=750, output_tokens=300)
        # Heartbeat with no token args (both default to None) — stored values must survive.
        kb.heartbeat_worker(conn, task_id, note="no-tokens")

        row = conn.execute(
            "SELECT input_tokens, output_tokens FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert row["input_tokens"] == 750, "NULL heartbeat must not overwrite stored input_tokens"
        assert row["output_tokens"] == 300, "NULL heartbeat must not overwrite stored output_tokens"
    finally:
        conn.close()


def test_heartbeat_token_monotone_increase_ok(kanban_home):
    """FIX 2: a later heartbeat with a larger token count updates the stored value."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="tok-increase", assignee="coder")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        run_id = _insert_run(conn, task_id, worker_pid=8003)
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
        conn.commit()

        kb.heartbeat_worker(conn, task_id, input_tokens=100, output_tokens=40)
        kb.heartbeat_worker(conn, task_id, input_tokens=800, output_tokens=250)

        row = conn.execute(
            "SELECT input_tokens, output_tokens FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert row["input_tokens"] == 800
        assert row["output_tokens"] == 250
    finally:
        conn.close()
