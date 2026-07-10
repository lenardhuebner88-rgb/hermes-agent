from __future__ import annotations

import concurrent.futures
import hashlib
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_closeout as closeout
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_AUTO_RECEIPT_DIR", str(tmp_path / "receipts"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _queue_done(conn, *, title="closeout task", release_context=None):
    task_id = kb.create_task(conn, title=title, assignee="coder")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'done', result = ? WHERE id = ?",
            ("result evidence", task_id),
        )
        kb._append_event(conn, task_id, "completed", {"summary": "done"})
        closeout.enqueue_closeout_pending_in_txn(
            conn,
            task_id,
            summary="done",
            board="default",
            release_context=release_context,
        )
    return task_id


def _event_kinds(conn, task_id):
    return [
        row["kind"]
        for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        )
    ]


def test_pending_enqueue_is_deduplicated_inside_callers_transaction(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="dedupe")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,)
            )
            first = closeout.enqueue_closeout_pending_in_txn(conn, task_id)
            second = closeout.enqueue_closeout_pending_in_txn(conn, task_id)
        assert first == second
        count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, closeout.CLOSEOUT_PENDING),
        ).fetchone()[0]
        assert count == 1


def test_non_done_task_cannot_enqueue_or_process_closeout(kanban_home):
    release_calls = []
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="not terminal")
        with kb.write_txn(conn):
            with pytest.raises(ValueError, match="to be done"):
                closeout.enqueue_closeout_pending_in_txn(conn, task_id)
        # Even a malformed/legacy premature event cannot bypass actionability.
        kb.add_event(conn, task_id, closeout.CLOSEOUT_PENDING, {"version": 1})
        assert closeout.closeout_sweep(
            conn,
            release_runner=lambda _conn, tid: release_calls.append(tid),
        ) == []
        assert release_calls == []
        assert closeout.CLOSEOUT_RECEIPT_WRITTEN not in _event_kinds(conn, task_id)


def test_default_lease_exceeds_release_timeout():
    assert closeout.DEFAULT_CLOSEOUT_LEASE_SECONDS >= 1800


def test_spawn_closeout_unit_uses_stable_board_scoped_systemd_command(
    kanban_home, monkeypatch
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=0, stdout="started", stderr="")

    first = closeout.spawn_closeout_unit(
        "t_gate/9", board="ops", runner=fake_run, hermes_bin="/opt/hermes"
    )
    second = closeout.spawn_closeout_unit(
        "t_gate/9", board="ops", runner=fake_run, hermes_bin="/opt/hermes"
    )

    assert first["unit"] == second["unit"]
    assert first["unit"] == "hermes-kanban-closeout-ops-t_gate_9"
    argv = calls[0][0]
    assert argv[:3] == ["systemd-run", "--user", "--collect"]
    assert f"--unit={first['unit']}" in argv
    assert any(item.startswith("--setenv=PATH=") for item in argv)
    assert "--setenv=XDG_RUNTIME_DIR=/run/user/1000" in argv
    assert "--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus" in argv
    index = argv.index("closeout")
    assert argv[index - 3 : index + 4] == [
        "kanban",
        "--board",
        "ops",
        "closeout",
        "t_gate/9",
        "--inline",
        "--json",
    ]


def test_spawn_pending_failure_is_retryable_and_never_claims(kanban_home):
    calls = []

    def reject(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=1, stdout="", stderr="Unit busy")

    with kb.connect_closing() as conn:
        task_id = _queue_done(conn)
        first = closeout.spawn_pending_closeouts(
            conn, "default", runner=reject, hermes_bin="/opt/hermes"
        )
        second = closeout.spawn_pending_closeouts(
            conn, "default", runner=reject, hermes_bin="/opt/hermes"
        )
        assert first[0]["ok"] is False
        assert second[0]["ok"] is False
        assert first[0]["unit"] == second[0]["unit"]
        assert len(calls) == 2
        assert closeout.CLOSEOUT_CLAIMED not in _event_kinds(conn, task_id)


def test_atomic_receipt_returns_hash_and_preserves_old_file_on_replace_error(
    tmp_path, monkeypatch
):
    receipt_dir = tmp_path / "receipts"
    artifact = closeout.write_receipt_atomic(
        "t_123", "first receipt\n", receipt_dir=receipt_dir
    )
    target = Path(artifact.path)
    assert target.read_text(encoding="utf-8") == "first receipt\n"
    assert artifact.sha256 == hashlib.sha256(b"first receipt\n").hexdigest()

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        closeout.write_receipt_atomic(
            "t_123", "partial replacement", receipt_dir=receipt_dir
        )
    assert target.read_text(encoding="utf-8") == "first receipt\n"
    assert list(receipt_dir.glob("*.tmp")) == []


def test_no_subscription_not_required_writes_receipt_and_delivers(kanban_home):
    with kb.connect_closing() as conn:
        task_id = _queue_done(conn, title="no subscription")
        assert kb.list_notify_subs(conn, task_id) == []

        results = closeout.closeout_sweep(
            conn, release_runner=lambda _conn, _task_id: None
        )

        assert [result.state for result in results] == ["delivered"]
        kinds = _event_kinds(conn, task_id)
        assert closeout.CLOSEOUT_RELEASE_STARTED in kinds
        assert closeout.CLOSEOUT_RELEASE_NOT_REQUIRED in kinds
        assert closeout.CLOSEOUT_RECEIPT_WRITTEN in kinds
        assert closeout.CLOSEOUT_DELIVERED in kinds
        receipt = Path(os.environ["HERMES_AUTO_RECEIPT_DIR"]) / f"{task_id}.md"
        assert receipt.is_file()
        assert "no subscription" in receipt.read_text(encoding="utf-8")


def test_receipt_failure_retries_without_rerunning_release(kanban_home):
    release_calls = []
    receipt_calls = []

    def release_runner(_conn, task_id):
        release_calls.append(task_id)
        return None

    def receipt_writer(receipt):
        receipt_calls.append(receipt.task_id)
        if len(receipt_calls) == 1:
            raise OSError("vault unavailable")
        return closeout.write_receipt_atomic(receipt.task_id, "retried receipt")

    with kb.connect_closing() as conn:
        task_id = _queue_done(conn)
        first = closeout.closeout_sweep(
            conn, release_runner=release_runner, receipt_writer=receipt_writer
        )
        assert first[0].state == "failed"
        assert "vault unavailable" in (first[0].error or "")
        assert closeout.CLOSEOUT_DELIVERED not in _event_kinds(conn, task_id)

        second = closeout.closeout_sweep(
            conn, release_runner=release_runner, receipt_writer=receipt_writer
        )
        assert second[0].state == "delivered"
        assert release_calls == [task_id]
        assert receipt_calls == [task_id, task_id]


def test_unexpired_claim_holds_and_expired_lease_is_reclaimed(kanban_home):
    release_calls = []
    with kb.connect_closing() as conn:
        task_id = _queue_done(conn)
        claim = closeout.claim_closeout(
            conn, task_id, now=100, lease_seconds=50, token="dead-worker"
        )
        assert claim is not None

        held = closeout.closeout_sweep(
            conn,
            now=120,
            lease_seconds=50,
            release_runner=lambda _conn, tid: release_calls.append(tid),
        )
        assert held == []
        assert release_calls == []

        recovered = closeout.closeout_sweep(
            conn,
            now=151,
            lease_seconds=50,
            release_runner=lambda _conn, tid: release_calls.append(tid),
        )
        assert recovered[0].state == "delivered"
        assert release_calls == [task_id]


def test_two_concurrent_sweeps_make_one_claim_and_one_release(kanban_home):
    with kb.connect_closing() as conn:
        task_id = _queue_done(conn)

    entered = threading.Event()
    release_worker = threading.Event()
    release_calls = []

    def release_runner(_conn, tid):
        release_calls.append(tid)
        entered.set()
        assert release_worker.wait(timeout=5)
        return {"outcome": "deployed"}

    def run_first():
        with kb.connect_closing() as conn:
            return closeout.closeout_sweep(
                conn, now=100, lease_seconds=60, release_runner=release_runner
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_first)
        assert entered.wait(timeout=5)
        with kb.connect_closing() as conn:
            second = closeout.closeout_sweep(
                conn, now=101, lease_seconds=60, release_runner=release_runner
            )
        release_worker.set()
        first = future.result(timeout=5)

    assert second == []
    assert first[0].state == "delivered"
    assert release_calls == [task_id]
    with kb.connect_closing() as conn:
        claims = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, closeout.CLOSEOUT_CLAIMED),
        ).fetchone()[0]
        assert claims == 1


def test_expired_worker_loses_release_cas_and_cannot_finish_new_claim(kanban_home):
    release_calls = []
    with kb.connect_closing() as conn:
        task_id = _queue_done(conn)
        stale = closeout.claim_closeout(
            conn, task_id, now=100, lease_seconds=1, token="stale"
        )
        current = closeout.claim_closeout(
            conn, task_id, now=102, lease_seconds=60, token="current"
        )
        assert stale is not None and current is not None

        stale_state = closeout._drive_release(
            conn,
            stale,
            lambda _conn, tid: release_calls.append(("stale", tid)),
        )
        assert stale_state == closeout.CLOSEOUT_RELEASE_WAITING
        assert closeout._finish_claim(conn, stale, "pending") is False

        current_state = closeout._drive_release(
            conn,
            current,
            lambda _conn, tid: (
                release_calls.append(("current", tid)) or {"outcome": "deployed"}
            ),
        )
        assert current_state == closeout.CLOSEOUT_RELEASE_COMPLETE
        assert closeout._finish_claim(conn, current, "delivered") is True
        assert release_calls == [("current", task_id)]

        finished = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, closeout.CLOSEOUT_CLAIM_FINISHED),
        ).fetchall()
        assert [closeout._json_payload(row["payload"])["token"] for row in finished] == [
            "current"
        ]


def test_explicit_held_release_writes_receipt_but_never_delivers(kanban_home):
    with kb.connect_closing() as conn:
        task_id = _queue_done(conn)
        result = closeout.closeout_sweep(
            conn,
            release_runner=lambda _conn, _task_id: {
                "outcome": "held_critical",
                "detail": "critical tier",
            },
        )[0]
        assert result.state == "held"
        kinds = _event_kinds(conn, task_id)
        assert closeout.CLOSEOUT_RELEASE_HELD in kinds
        assert "auto_release" in kinds
        assert closeout.CLOSEOUT_RECEIPT_WRITTEN in kinds
        assert closeout.CLOSEOUT_DELIVERED not in kinds
        assert closeout.pending_closeouts(conn) == []


def test_preexisting_release_started_becomes_ambiguous_without_second_deploy(
    kanban_home,
):
    release_calls = []
    with kb.connect_closing() as conn:
        task_id = _queue_done(conn)
        kb.add_event(
            conn,
            task_id,
            closeout.CLOSEOUT_RELEASE_STARTED,
            {"claim_token": "crashed"},
        )
        result = closeout.closeout_sweep(
            conn,
            release_runner=lambda _conn, tid: release_calls.append(tid),
        )[0]
        assert result.state == "ambiguous"
        assert release_calls == []
        kinds = _event_kinds(conn, task_id)
        assert closeout.CLOSEOUT_RELEASE_AMBIGUOUS in kinds
        assert closeout.CLOSEOUT_RECEIPT_WRITTEN in kinds
        assert closeout.CLOSEOUT_DELIVERED not in kinds
        assert closeout.pending_closeouts(conn) == []


def test_successful_release_is_explicit_and_delivered(kanban_home):
    with kb.connect_closing() as conn:
        task_id = _queue_done(conn)
        result = closeout.process_closeout(
            conn,
            task_id,
            release_runner=lambda _conn, _task_id: {"outcome": "deployed"},
        )
        assert result.state == "delivered"
        kinds = _event_kinds(conn, task_id)
        assert closeout.CLOSEOUT_RELEASE_COMPLETE in kinds
        assert "auto_release" in kinds
        assert closeout.CLOSEOUT_DELIVERED in kinds


def test_auto_executed_release_gate_waits_then_completes_without_duplicate_runner(
    kanban_home,
):
    release_calls = []
    with kb.connect_closing() as conn:
        child_id = kb.create_task(conn, title="release gate", initial_status="blocked")
        kb.add_event(
            conn,
            child_id,
            "release_gate_auto_execute_started",
            {"mode": "autonomous"},
        )
        task_id = _queue_done(
            conn,
            release_context={
                "release_gate_child_id": child_id,
                "release_gate_auto_executed": True,
            },
        )

        waiting = closeout.closeout_sweep(
            conn,
            release_runner=lambda _conn, tid: release_calls.append(tid),
        )[0]
        waiting_kinds = _event_kinds(conn, task_id)
        assert waiting.state == "pending"
        assert closeout.CLOSEOUT_RELEASE_WAITING in waiting_kinds
        assert closeout.CLOSEOUT_RELEASE_STARTED not in waiting_kinds
        assert closeout.CLOSEOUT_RECEIPT_WRITTEN not in waiting_kinds

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (child_id,))
            kb._append_event(
                conn, child_id, "release_gate_activated", {"ok": True}
            )
        completed = closeout.closeout_sweep(
            conn,
            release_runner=lambda _conn, tid: release_calls.append(tid),
        )[0]
        assert completed.state == "delivered"
        assert release_calls == []
        completed_kinds = _event_kinds(conn, task_id)
        assert closeout.CLOSEOUT_RELEASE_COMPLETE in completed_kinds
        assert closeout.CLOSEOUT_RECEIPT_WRITTEN in completed_kinds
        assert closeout.CLOSEOUT_DELIVERED in completed_kinds


def test_parked_release_gate_is_held_without_generic_release(kanban_home):
    release_calls = []
    with kb.connect_closing() as conn:
        child_id = kb.create_task(conn, title="parked gate", initial_status="blocked")
        kb.add_event(conn, child_id, "release_gate_parked", {"state": "held"})
        kb.add_event(
            conn,
            child_id,
            "release_gate_auto_execute_held",
            {"outcome": "held_critical"},
        )
        task_id = _queue_done(
            conn,
            release_context={
                "release_gate_child_id": child_id,
                "release_gate_auto_executed": False,
            },
        )
        result = closeout.closeout_sweep(
            conn,
            release_runner=lambda _conn, tid: release_calls.append(tid),
        )[0]
        assert result.state == "held"
        assert release_calls == []
        kinds = _event_kinds(conn, task_id)
        assert closeout.CLOSEOUT_RELEASE_HELD in kinds
        assert closeout.CLOSEOUT_RELEASE_STARTED not in kinds
        assert closeout.CLOSEOUT_DELIVERED not in kinds

        # A plain task completion is not deployment evidence and must not revive
        # the source closeout.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (child_id,))
        assert closeout.pending_closeouts(conn) == []
        assert closeout.CLOSEOUT_DELIVERED not in _event_kinds(conn, task_id)

        # Explicit activation evidence monotonically lifts the prior hold,
        # rewrites the receipt against the new release event, and delivers.
        kb.add_event(conn, child_id, "release_gate_activated", {"ok": True})
        assert [item[0] for item in closeout.pending_closeouts(conn)] == [task_id]
        completed = closeout.closeout_sweep(
            conn,
            release_runner=lambda _conn, tid: release_calls.append(tid),
        )[0]
        assert completed.state == "delivered"
        assert release_calls == []
        receipt_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, closeout.CLOSEOUT_RECEIPT_WRITTEN),
        ).fetchone()[0]
        assert receipt_count == 2
        assert closeout.CLOSEOUT_RELEASE_COMPLETE in _event_kinds(conn, task_id)
        assert closeout.CLOSEOUT_DELIVERED in _event_kinds(conn, task_id)


def test_release_gate_spawn_failure_is_ambiguous_without_generic_fallback(
    kanban_home,
):
    release_calls = []
    with kb.connect_closing() as conn:
        child_id = kb.create_task(conn, title="failed gate", initial_status="blocked")
        kb.add_event(conn, child_id, "release_gate_parked", {})
        kb.add_event(
            conn,
            child_id,
            "release_gate_auto_execute_failed",
            {"error": "systemd-run failed"},
        )
        task_id = _queue_done(
            conn,
            release_context={
                "release_gate_child_id": child_id,
                "release_gate_auto_executed": False,
            },
        )
        result = closeout.closeout_sweep(
            conn,
            release_runner=lambda _conn, tid: release_calls.append(tid),
        )[0]
        assert result.state == "ambiguous"
        assert release_calls == []
        kinds = _event_kinds(conn, task_id)
        assert closeout.CLOSEOUT_RELEASE_AMBIGUOUS in kinds
        assert closeout.CLOSEOUT_DELIVERED not in kinds


def test_required_release_gate_without_child_is_held_without_generic_fallback(
    kanban_home,
):
    release_calls = []
    with kb.connect_closing() as conn:
        task_id = _queue_done(
            conn,
            release_context={"release_gate_required": True},
        )
        result = closeout.closeout_sweep(
            conn,
            release_runner=lambda _conn, tid: release_calls.append(tid),
        )[0]
        assert result.state == "held"
        assert release_calls == []
        held = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, closeout.CLOSEOUT_RELEASE_HELD),
        ).fetchone()
        assert held is not None
        assert closeout._json_payload(held["payload"])["outcome"] == (
            "held_required_release_gate_missing"
        )


def test_release_gate_needs_start_owns_deploy_without_auto_release_fallback(
    kanban_home, monkeypatch,
):
    """Mutex on the previously-uncovered ``needs_start`` path: when a parked
    release-gate child still has to be started, the gate path spawns the detached
    activation (the backend restart) and ``maybe_auto_release`` MUST NOT also run
    — otherwise flipping ``release.autonomous`` ON would risk a double backend
    restart per completion."""
    from hermes_cli import kanban_worktrees

    release_calls = []
    gate_starts = []

    def _fake_start(conn, child_id):
        gate_starts.append(child_id)
        return "started"

    monkeypatch.setattr(kanban_worktrees, "start_parked_release_gate", _fake_start)

    with kb.connect_closing() as conn:
        # A parked child with no reconciled events yet → _release_gate_context_state
        # returns "release_gate_needs_start", the branch that actually spawns.
        child_id = kb.create_task(
            conn, title="needs start gate", initial_status="blocked"
        )
        task_id = _queue_done(
            conn,
            release_context={"release_gate_child_id": child_id},
        )

        result = closeout.closeout_sweep(
            conn,
            release_runner=lambda _conn, tid: release_calls.append(tid),
        )[0]

        # Exactly ONE deploy path drove: the gate spawn, never maybe_auto_release.
        assert gate_starts == [child_id]
        assert release_calls == []
        assert result.state == "pending"
        kinds = _event_kinds(conn, task_id)
        assert closeout.CLOSEOUT_RELEASE_WAITING in kinds
        assert closeout.CLOSEOUT_RECEIPT_WRITTEN not in kinds
        assert closeout.CLOSEOUT_DELIVERED not in kinds


def test_gate_child_present_but_no_gate_state_refuses_auto_release_fallback(
    kanban_home, monkeypatch,
):
    """Defensive backstop: even if the gate reconciler regressed to return no
    state while a release-gate child is recorded, the mutex refuses the
    ``maybe_auto_release`` fallback (ambiguous, no second deploy) rather than let
    both hook paths deploy for one completion."""
    monkeypatch.setattr(closeout, "_release_gate_context_state", lambda *a, **k: None)

    release_calls = []
    with kb.connect_closing() as conn:
        task_id = _queue_done(
            conn,
            release_context={"release_gate_child_id": "t_ghost_child"},
        )
        result = closeout.closeout_sweep(
            conn,
            release_runner=lambda _conn, tid: release_calls.append(tid),
        )[0]

        assert result.state == "ambiguous"
        assert release_calls == []
        kinds = _event_kinds(conn, task_id)
        assert closeout.CLOSEOUT_RELEASE_AMBIGUOUS in kinds
        assert closeout.CLOSEOUT_RELEASE_STARTED not in kinds
        assert closeout.CLOSEOUT_DELIVERED not in kinds
