"""Kanban DB tests: claims.

Split from test_kanban_db.py (pure move; no test logic changes).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import types
import unittest.mock
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb

def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code."""
    return code << 8


# ---------------------------------------------------------------------------
# Task creation + status inference
# ---------------------------------------------------------------------------

def test_create_task_no_parents_is_ready(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="ship it", assignee="alice")
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.status == "ready"
    assert t.assignee == "alice"
    assert t.workspace_kind == "scratch"


def test_create_task_with_parent_is_todo_until_parent_done(kanban_home):
    with kb.connect_closing() as conn:
        p = kb.create_task(conn, title="parent")
        c = kb.create_task(conn, title="child", parents=[p])
        assert kb.get_task(conn, c).status == "todo"
        kb.complete_task(conn, p, result="ok")
        assert kb.get_task(conn, c).status == "ready"


def test_create_task_unknown_parent_errors(kanban_home):
    with kb.connect_closing() as conn, pytest.raises(ValueError, match="unknown parent"):
        kb.create_task(conn, title="orphan", parents=["t_ghost"])


def test_workspace_kind_validation(kanban_home):
    with kb.connect_closing() as conn, pytest.raises(ValueError, match="workspace_kind"):
        kb.create_task(conn, title="bad ws", workspace_kind="cloud")


def test_create_task_persists_worktree_branch_name(kanban_home, tmp_path):
    target = tmp_path / ".worktrees" / "t6-wire"
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="ship worktree",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name=" wt/t6-wire ",
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        context = kb.build_worker_context(conn, tid)

    assert task.branch_name == "wt/t6-wire"
    assert events[0].payload["branch_name"] == "wt/t6-wire"
    assert "Branch:   wt/t6-wire" in context


def test_branch_name_requires_worktree_workspace(kanban_home):
    with kb.connect_closing() as conn, pytest.raises(ValueError, match="worktree"):
        kb.create_task(
            conn,
            title="bad branch",
            workspace_kind="scratch",
            branch_name="wt/bad",
        )


def test_build_worker_context_includes_knowledge_pointers(kanban_home):
    """build_worker_context must include the static knowledge-pointer section
    so workers know where to look for model-landscape and canonical facts."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="test knowledge pointers")
        ctx = kb.build_worker_context(conn, tid)

    assert "Knowledge pointers" in ctx
    assert "/home/piet/llm-wiki/wiki/models/model-landscape.md" in ctx
    assert "/home/piet/vault/00-Canon/" in ctx


def test_build_worker_context_uses_shared_knowledge_pointer_renderer(
    kanban_home, monkeypatch
):
    """The native context consumes the shared renderer instead of duplicated
    literal pointer strings, keeping worker-runtime prompts in parity."""
    monkeypatch.setattr(
        kb,
        "_render_knowledge_pointers",
        lambda: ["## Knowledge pointers", "- shared-renderer-sentinel", ""],
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="test shared knowledge renderer")
        ctx = kb.build_worker_context(conn, tid)

    assert "shared-renderer-sentinel" in ctx


# ---------------------------------------------------------------------------
# Links + dependency resolution
# ---------------------------------------------------------------------------

def test_link_demotes_ready_child_to_todo_when_parent_not_done(kanban_home):
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
        assert kb.get_task(conn, b).status == "ready"
        kb.link_tasks(conn, a, b)
        assert kb.get_task(conn, b).status == "todo"


def test_link_keeps_ready_child_when_parent_already_done(kanban_home):
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="a")
        kb.complete_task(conn, a)
        b = kb.create_task(conn, title="b")
        assert kb.get_task(conn, b).status == "ready"
        kb.link_tasks(conn, a, b)
        assert kb.get_task(conn, b).status == "ready"


def test_link_rejects_self_loop(kanban_home):
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="a")
        with pytest.raises(ValueError, match="itself"):
            kb.link_tasks(conn, a, a)


def test_link_detects_cycle(kanban_home):
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b", parents=[a])
        c = kb.create_task(conn, title="c", parents=[b])
        with pytest.raises(ValueError, match="cycle"):
            kb.link_tasks(conn, c, a)
        with pytest.raises(ValueError, match="cycle"):
            kb.link_tasks(conn, b, a)


def test_recompute_ready_cascades_through_chain(kanban_home):
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b", parents=[a])
        c = kb.create_task(conn, title="c", parents=[b])
        assert [kb.get_task(conn, x).status for x in (a, b, c)] == \
               ["ready", "todo", "todo"]
        kb.complete_task(conn, a)
        assert kb.get_task(conn, b).status == "ready"
        kb.complete_task(conn, b)
        assert kb.get_task(conn, c).status == "ready"


def test_recompute_ready_promotes_blocked_with_done_parents(kanban_home):
    """blocked tasks with all parents done should be promoted to ready,
    unless the circuit-breaker failure limit has been reached."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        # Complete the parent
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        # Manually block the child with zero failures (simulates a
        # dependency block, not a circuit-breaker block).
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=0, "
            "last_failure_error=NULL WHERE id=?",
            (child,),
        )
        conn.commit()
        assert kb.get_task(conn, child).status == "blocked"
        # recompute_ready should promote blocked → ready
        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        task = kb.get_task(conn, child)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


def test_recompute_ready_fan_in_waits_for_all_parents(kanban_home):
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
        c = kb.create_task(conn, title="c", parents=[a, b])
        kb.complete_task(conn, a)
        assert kb.get_task(conn, c).status == "todo"
        kb.complete_task(conn, b)
        assert kb.get_task(conn, c).status == "ready"


def test_archived_parent_does_not_satisfy_dependency(kanban_home):
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )

        kb.archive_task(conn, parent)
        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, child)
        assert task is not None
        assert task.status == "todo"

        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        conn.commit()
        assert kb.claim_task(conn, child, claimer="host:1") is None
        task = kb.get_task(conn, child)
        assert task is not None
        assert task.status == "todo"


# ---------------------------------------------------------------------------
# Atomic claim (CAS)
# ---------------------------------------------------------------------------

def test_claim_once_wins_second_loses(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        first = kb.claim_task(conn, t, claimer="host:1")
        assert first is not None and first.status == "running"
        second = kb.claim_task(conn, t, claimer="host:2")
        assert second is None


def test_claim_uses_env_default_ttl(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_TTL_SECONDS", "3600")
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t, claimer="host:1")
        expires = kb.get_task(conn, t).claim_expires
    assert expires is not None
    assert expires > int(time.time()) + 3000


def test_claim_fails_on_non_ready(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        # Move to todo by introducing an unsatisfied parent.
        p = kb.create_task(conn, title="p")
        kb.link_tasks(conn, p, t)
        assert kb.get_task(conn, t).status == "todo"
        assert kb.claim_task(conn, t) is None


def test_schedule_task_parks_time_delay_without_dispatching(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="delayed recheck", assignee="ops")
        assert kb.schedule_task(conn, t, reason="run next week") is True
        task = kb.get_task(conn, t)
        assert task.status == "scheduled"
        assert kb.claim_task(conn, t) is None

        events = kb.list_events(conn, t)
        assert any(e.kind == "scheduled" and e.payload == {"reason": "run next week"} for e in events)


def test_unblock_scheduled_rechecks_parent_gate(kanban_home):
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"
        assert kb.schedule_task(conn, child, reason="wait until tomorrow") is True

        assert kb.unblock_task(conn, child) is True
        assert kb.get_task(conn, child).status == "todo"

        kb.complete_task(conn, parent)
        assert kb.schedule_task(conn, child, reason="second timer") is True
        assert kb.unblock_task(conn, child) is True
        assert kb.get_task(conn, child).status == "ready"


def test_stale_claim_reclaimed(kanban_home, monkeypatch):
    import signal
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        killed: list[int] = []

        def _signal(_pid, sig):
            killed.append(sig)

        kb._set_worker_pid(conn, t, 12345)
        # Rewind claim_expires so it looks stale.
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 3600, t),
        )
        # Worker PID has died — exactly the case ``release_stale_claims``
        # should still reclaim (post-#23025: live PIDs are now extended).
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        reclaimed = kb.release_stale_claims(conn, signal_fn=_signal)
        assert reclaimed == 1
        assert kb.get_task(conn, t).status == "ready"
        assert killed == [signal.SIGTERM]


def test_stale_claim_with_live_pid_extends_instead_of_reclaiming(
    kanban_home, monkeypatch,
):
    """A stale-by-TTL claim with a live PID and fresh heartbeat is extended.

    Killing an observably active worker produces a respawn loop with zero
    progress (#23025); KI-5 only changes the missing-heartbeat case.
    """
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)

        old_expires = int(time.time()) - 60
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (old_expires, int(time.time()), t),
        )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        killed: list[int] = []
        reclaimed = kb.release_stale_claims(
            conn, signal_fn=lambda _p, sig: killed.append(sig),
        )
        assert reclaimed == 0
        task = kb.get_task(conn, t)
        assert task.status == "running"
        assert task.claim_expires is not None
        assert task.claim_expires > old_expires
        assert killed == []  # live worker not killed

        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (t,),
            ).fetchall()
        ]
        assert "claim_extended" in kinds
        assert "reclaimed" not in kinds


def test_stale_claim_with_null_heartbeat_reclaims_instead_of_extending(
    kanban_home, monkeypatch,
):
    """An expired claim with no heartbeat has no observable progress.

    A live PID therefore enters the terminate/reclaim path instead of getting
    an indefinite TTL extension. The termination-success stub preserves the
    separate invariant that a worker which survives termination keeps its
    claim and cannot be duplicated.
    """
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="missing heartbeat", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        old_expires = int(time.time()) - 60
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = NULL "
            "WHERE id = ?",
            (old_expires, t),
        )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        termination_calls = []

        def terminate(*args, **kwargs):
            termination_calls.append((args, kwargs))
            return {
                "termination_attempted": True,
                "host_local": True,
                "terminated": True,
            }

        monkeypatch.setattr(_kb, "_terminate_reclaimed_worker", terminate)
        reclaimed = kb.release_stale_claims(
            conn, signal_fn=lambda _pid, _signal: None,
        )

        assert reclaimed == 1
        assert termination_calls
        assert kb.get_task(conn, t).status == "ready"
        kinds = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (t,)
            ).fetchall()
        ]
        assert "reclaimed" in kinds
        assert "claim_extended" not in kinds


def test_stale_claim_with_live_pid_uses_env_ttl_override(
    kanban_home, monkeypatch,
):
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_CLAIM_TTL_SECONDS", "3600")

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (int(time.time()) - 60, int(time.time()), t),
        )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        assert reclaimed == 0

        task = kb.get_task(conn, t)
        assert task is not None
        assert task.claim_expires is not None
        assert task.claim_expires > int(time.time()) + 3000


def test_stale_claim_deferred_when_live_worker_survives_termination(
    kanban_home, monkeypatch,
):
    """A TTL-expired claim whose worker survives the kill must NOT be released.

    Releasing would let the dispatcher spawn a duplicate beside the still-alive
    worker — the runaway seen when a cgroup memory.high throttle parks a worker
    in uninterruptible (D) state, where a pending SIGKILL cannot land. The claim
    is held (extended) and retried next tick instead.
    """
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)

        old_expires = int(time.time()) - 60
        # Heartbeat stale by > 1h so the live-pid EXTEND branch is skipped and
        # the terminate path (the wedged-worker case) runs.
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (old_expires, int(time.time()) - 7200, t),
        )
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(
            _kb, "_terminate_reclaimed_worker",
            lambda *a, **k: {
                "termination_attempted": True,
                "host_local": True,
                "terminated": False,
            },
        )
        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        assert reclaimed == 0

        assert kb.get_task(conn, t).status == "running"
        worker_pid = conn.execute(
            "SELECT worker_pid FROM tasks WHERE id = ?", (t,),
        ).fetchone()[0]
        assert worker_pid == 12345  # worker not orphaned
        claim_expires = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (t,),
        ).fetchone()[0]
        assert claim_expires > old_expires  # claim held, not released

        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (t,),
            ).fetchall()
        ]
        assert "reclaim_deferred" in kinds
        assert "reclaimed" not in kinds


def test_stale_claim_reclaimed_when_termination_succeeds(
    kanban_home, monkeypatch,
):
    """When the worker is actually killed, the claim is released as before."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (int(time.time()) - 60, int(time.time()) - 7200, t),
        )
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(
            _kb, "_terminate_reclaimed_worker",
            lambda *a, **k: {
                "termination_attempted": True,
                "host_local": True,
                "terminated": True,
            },
        )
        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        assert reclaimed == 1
        assert kb.get_task(conn, t).status == "ready"


def test_stale_claim_released_when_worker_not_host_local(
    kanban_home, monkeypatch,
):
    """The defer guard only holds OUR own surviving workers.

    A claim we cannot manage (different host, or no kill attempted) must still
    be released, otherwise a foreign-host claim could strand a task forever.
    """
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (int(time.time()) - 60, int(time.time()) - 7200, t),
        )
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(
            _kb, "_terminate_reclaimed_worker",
            lambda *a, **k: {
                "termination_attempted": False,
                "host_local": False,
                "terminated": False,
            },
        )
        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        assert reclaimed == 1
        assert kb.get_task(conn, t).status == "ready"


def test_detect_stale_defers_when_live_worker_survives(kanban_home, monkeypatch):
    """detect_stale_running must also hold the claim when the worker survives."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="wedged", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ?, last_heartbeat_at = NULL "
                "WHERE id = ?",
                (five_hours_ago, t),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(
            _kb, "_terminate_reclaimed_worker",
            lambda *a, **k: {
                "termination_attempted": True,
                "host_local": True,
                "terminated": False,
            },
        )
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert stale == []
        assert kb.get_task(conn, t).status == "running"
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (t,),
            ).fetchall()
        ]
        assert "reclaim_deferred" in kinds


def test_stale_claim_reclaim_event_records_diagnostic_payload(
    kanban_home, monkeypatch,
):
    """``reclaimed`` events should carry claim_expires, last_heartbeat_at,
    and worker_pid so operators can diagnose why a claim went stale
    (#23025: previous payload only had ``stale_lock`` which gives no
    timing context)."""
    import json
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        old_expires = int(time.time()) - 3600
        hb_at = int(time.time()) - 1800
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (old_expires, hb_at, t),
        )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'reclaimed'",
            (t,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["claim_expires"] == old_expires
        assert payload["last_heartbeat_at"] == hb_at
        assert payload["worker_pid"] == 12345
        assert payload["host_local"] is True


def test_detect_crashed_workers_systemic_failure_fast_block(
    kanban_home, monkeypatch,
):
    """When many tasks crash with the same error, trip the breaker faster."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        task_ids = []
        for i in range(4):
            tid = kb.create_task(conn, title=f"task-{i}", assignee="a")
            host = _kb._claimer_id().split(":", 1)[0]
            conn.execute(
                "UPDATE tasks SET status='running', worker_pid=?, "
                "claim_lock=? WHERE id=?",
                (90000 + i, f"{host}:w{i}", tid),
            )
            task_ids.append(tid)
        conn.commit()

        crashed = kb.detect_crashed_workers(conn)
        assert len(crashed) == 4

        for tid in task_ids:
            task = kb.get_task(conn, tid)
            assert task.status == "blocked", (
                f"task {tid} should be blocked (systemic), got {task.status}"
            )


def test_detect_crashed_workers_isolated_failure_normal_retry(
    kanban_home, monkeypatch,
):
    """Below the systemic threshold, tasks retain normal retry budget."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        task_ids = []
        for i in range(2):
            tid = kb.create_task(conn, title=f"iso-{i}", assignee="a")
            host = _kb._claimer_id().split(":", 1)[0]
            conn.execute(
                "UPDATE tasks SET status='running', worker_pid=?, "
                "claim_lock=? WHERE id=?",
                (80000 + i, f"{host}:w{i}", tid),
            )
            task_ids.append(tid)
        conn.commit()

        crashed = kb.detect_crashed_workers(conn)
        assert len(crashed) == 2

        for tid in task_ids:
            task = kb.get_task(conn, tid)
            assert task.status == "ready", (
                f"task {tid} should stay ready (isolated), got {task.status}"
            )


def test_detect_crashed_workers_preserves_review_stage_on_reclaim(
    kanban_home, monkeypatch,
):
    """A crashed reviewer returns to review while a crashed coder returns ready."""
    import hermes_cli.kanban_db as _kb
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        _kb,
        "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder"}),
            "verifier_profile": "verifier",
            "review_profile": "reviewer",
            "critic_profile": "critic",
            "auto_tier": False,
        },
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda _name: True)

    with kb.connect_closing() as conn:
        review_tid = kb.create_task(conn, title="review crash", assignee="coder")
        assert kb.claim_task(conn, review_tid) is not None
        assert kb.complete_task(
            conn,
            review_tid,
            summary="implementation complete",
            review_gate=True,
        )
        assert kb.claim_review_task(
            conn,
            review_tid,
            reviewer_profile="verifier",
        ) is not None
        kb._set_worker_pid(conn, review_tid, 81001)

        coder_tid = kb.create_task(conn, title="coder crash", assignee="coder")
        assert kb.claim_task(conn, coder_tid) is not None
        kb._set_worker_pid(conn, coder_tid, 81002)

        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id IN (?, ?)",
            (int(time.time()) - 60, review_tid, coder_tid),
        )
        conn.commit()

        kb.detect_crashed_workers(conn)

        assert kb.get_task(conn, review_tid).status == "review"
        assert kb.get_task(conn, coder_tid).status == "ready"


def test_detect_crashed_workers_skips_freshly_claimed_tasks(
    kanban_home, monkeypatch,
):
    """Grace period prevents reclaim of freshly-started tasks."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.delenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", raising=False)

    now = 1_000_000.0
    monkeypatch.setattr(_kb.time, "time", lambda: now)

    with kb.connect_closing() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="grace test", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, "
            "claim_lock=?, started_at=? WHERE id=?",
            (99999, f"{host}:w", int(now), tid),
        )
        conn.commit()

        # With time = now (just claimed), grace period should suppress reclaim.
        crashed = kb.detect_crashed_workers(conn)
        assert tid not in crashed, "should not reclaim freshly-started task"

        # With time = now + 60 (past default 30s grace), should reclaim.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 60)
        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed, "should reclaim task past grace period"


def test_detect_crashed_workers_grace_period_env_override(
    kanban_home, monkeypatch,
):
    """HERMES_KANBAN_CRASH_GRACE_SECONDS env var adjusts the window."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "5")

    now = 2_000_000.0

    with kb.connect_closing() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="env override test", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, "
            "claim_lock=?, started_at=? WHERE id=?",
            (99999, f"{host}:w", int(now), tid),
        )
        conn.commit()

        # 3s after claim: within 5s grace → no reclaim.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 3)
        assert tid not in kb.detect_crashed_workers(conn)

        # 6s after claim: past 5s grace → reclaim.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 6)
        assert tid in kb.detect_crashed_workers(conn)


def test_resolve_crash_grace_seconds_handles_bad_env(monkeypatch):
    """Bad env values fall back to DEFAULT_CRASH_GRACE_SECONDS."""
    import hermes_cli.kanban_db as _kb

    for bad_val in ("notanumber", "-5", ""):
        monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", bad_val)
        result = _kb._resolve_crash_grace_seconds()
        assert result == _kb.DEFAULT_CRASH_GRACE_SECONDS, (
            f"expected default for {bad_val!r}, got {result}"
        )


def test_classify_worker_exit_recognizes_rate_limit_sentinel(kanban_home):
    import hermes_cli.kanban_db as _kb

    pid = 31337
    _kb._record_worker_exit(pid, _exited_status(_kb.KANBAN_RATE_LIMIT_EXIT_CODE))
    kind, code = _kb._classify_worker_exit(pid)
    assert kind == "rate_limited"
    assert code == _kb.KANBAN_RATE_LIMIT_EXIT_CODE

    # Plain non-zero exit is still a normal crash, not rate-limited.
    _kb._record_worker_exit(pid + 1, _exited_status(1))
    assert _kb._classify_worker_exit(pid + 1) == ("nonzero_exit", 1)


def test_rate_limit_exit_requeues_without_counting_failure(
    kanban_home, monkeypatch,
):
    """A rate-limit sentinel exit releases the task to ``ready`` and leaves
    ``consecutive_failures`` untouched — the breaker must never trip on a
    transient throttle, even across many quota-wall hits."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect_closing() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="rl", assignee="a")

        # Simulate FAR more quota-wall hits than DEFAULT_FAILURE_LIMIT (2).
        # If any of these counted as a failure the task would be blocked.
        for i in range(6):
            pid = 70000 + i
            # Claim to open a real run (so detect_crashed_workers can close
            # it with a rate_limited outcome), then point the claim at this
            # host + a dead pid so the crash path acts on it.
            kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            conn.execute(
                "UPDATE tasks SET worker_pid=?, consecutive_failures=? "
                "WHERE id=?",
                (pid, 0, tid),
            )
            conn.commit()
            _kb._record_worker_exit(
                pid, _exited_status(_kb.KANBAN_RATE_LIMIT_EXIT_CODE)
            )

            crashed = kb.detect_crashed_workers(conn)
            # Rate-limited requeues are NOT crashes.
            assert tid not in crashed
            rl = getattr(_kb.detect_crashed_workers, "_last_rate_limited", [])
            assert tid in rl

            task = kb.get_task(conn, tid)
            assert task.status == "ready", (
                f"hit {i}: should requeue ready, got {task.status}"
            )
            assert task.consecutive_failures == 0, (
                f"hit {i}: rate-limit must not count a failure, "
                f"got {task.consecutive_failures}"
            )

        # Last failure error stamped so the respawn guard recognizes the
        # quota wall.
        assert task.last_failure_error and "rate-limited" in task.last_failure_error

        # A ``rate_limited`` run outcome was recorded (not ``crashed``).
        outcomes = [
            r["outcome"] for r in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id=?", (tid,),
            ).fetchall()
        ]
        assert "rate_limited" in outcomes
        assert "crashed" not in outcomes


def test_real_crash_still_counts_and_trips_breaker(kanban_home, monkeypatch):
    """Sanity: a genuine non-zero crash (not the sentinel) still increments
    the failure counter and trips the breaker — the rate-limit carve-out is
    surgical, not a blanket "never count crashes"."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="crash", assignee="a")

        for i in range(2):  # DEFAULT_FAILURE_LIMIT == 2
            pid = 60000 + i
            conn.execute(
                "UPDATE tasks SET status='running', worker_pid=?, "
                "claim_lock=? WHERE id=?",
                (pid, f"{host}:w{i}", tid),
            )
            conn.commit()
            _kb._record_worker_exit(pid, _exited_status(1))  # generic failure
            kb.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"genuine crashes should still trip the breaker, got {task.status}"
        )


def test_respawn_guard_defers_rate_limited_within_cooldown(
    kanban_home, monkeypatch,
):
    """Within the cooldown after a rate-limit requeue, the guard defers the
    respawn; after the cooldown it allows a probe — and crucially does NOT
    fall into ``blocker_auth`` (which would defer forever)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rl-guard", assignee="a")
        # Seed a rate_limited run that just ended + the stamped error.
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=? WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error=? WHERE id=?",
            ("pid 1 exited rate-limited (quota wall) — requeued", tid),
        )
        conn.commit()

        # Inside cooldown → defer with the rate-limit-specific reason.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kb.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

        # Past cooldown → allowed (None), NOT trapped by blocker_auth even
        # though last_failure_error contains "rate-limited".
        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        assert kb.check_respawn_guard(conn, tid) is None


def test_respawn_guard_rate_limit_cooldown_zero_allows_immediately(
    kanban_home, monkeypatch,
):
    """Cooldown of 0 disables the wait — task is spawnable on the next tick,
    and the stamped rate-limit text does not re-trap it via blocker_auth."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    now = 6_000_000

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rl-zero", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=? WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, last_failure_error=? WHERE id=?",
            ("pid 1 exited rate-limited (quota wall)", tid),
        )
        conn.commit()

        monkeypatch.setattr(_kb.time, "time", lambda: now + 1)
        assert kb.check_respawn_guard(conn, tid) is None


def test_park_integration_records_parked_outcome_not_completed(kanban_home):
    """C-2: a parked integration is stamped INTEGRATION_PARKED_OUTCOME, NOT
    'completed' — so it falls out of every ``outcome = 'completed'`` filter
    (recent_success guard, success-rate stats) while cost stays attributed."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="park-outcome", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb._park_integration(
            conn, tid, {"reason": "dirty worktree"}, expected_run_id=run_id,
        )
        row = conn.execute(
            "SELECT outcome, status FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()
        assert row["outcome"] == kb.INTEGRATION_PARKED_OUTCOME
        assert row["outcome"] != "completed"
        assert kb.get_task(conn, tid).status == "blocked"


def test_respawn_guard_recent_success_fires_without_unblock(kanban_home, monkeypatch):
    """Baseline (no regression): a genuine completed run inside the window with
    no operator unblock still defers as 'recent_success'."""
    import hermes_cli.kanban_db as _kb

    now = 7_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rs-baseline", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='completed', status='review', "
            "ended_at=? WHERE id=?", (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL WHERE id=?", (tid,),
        )
        conn.commit()
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kb.check_respawn_guard(conn, tid) == "recent_success"


def test_respawn_guard_unblock_clears_recent_success(kanban_home, monkeypatch):
    """C-1 (operator override): an explicit unblock AFTER a completed run beats
    the success cooldown — the deliberate "run this again" must not stall for
    the rest of the guard window."""
    import hermes_cli.kanban_db as _kb

    now = 7_100_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rs-unblock", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='completed', status='blocked', "
            "ended_at=? WHERE id=?", (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='blocked', current_run_id=NULL, "
            "claim_lock=NULL, worker_pid=NULL WHERE id=?", (tid,),
        )
        conn.commit()
        # Operator unblocks AFTER the completed run.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 50)
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "ready"
        # Still inside the guard window, but the unblock must clear it.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kb.check_respawn_guard(conn, tid) is None


def test_respawn_guard_status_requeue_clears_recent_success(kanban_home, monkeypatch):
    """A deliberate done-to-ready status event requests a fresh run."""
    import hermes_cli.kanban_db as _kb

    now = 7_150_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rs-status-requeue", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='completed', status='done', ended_at=? "
            "WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL "
            "WHERE id=?",
            (tid,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) "
            "VALUES (?, 'status', ?)",
            (tid, now + 20),
        )
        conn.commit()
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kb.check_respawn_guard(conn, tid) is None


def test_respawn_guard_manual_promote_clears_recent_success(kanban_home, monkeypatch):
    """The real operator promote event is a deliberate rerun request."""
    import hermes_cli.kanban_db as _kb

    now = 7_175_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rs-manual-promote", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='completed', status='done', ended_at=? "
            "WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='todo', current_run_id=NULL, claim_lock=NULL "
            "WHERE id=?",
            (tid,),
        )
        conn.commit()
        monkeypatch.setattr(_kb.time, "time", lambda: now + 20)
        promoted, reason = kb.promote_task(
            conn, tid, actor="operator", reason="run it again"
        )
        assert promoted is True, reason
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kb.check_respawn_guard(conn, tid) is None


def test_parked_then_unblocked_task_is_respawnable(kanban_home, monkeypatch):
    """C-1 + C-2 end-to-end: park an integration, operator unblocks → the task
    is dispatchable on the next tick (no 'recent_success' stall). The 1h-stall
    bug this guards against had both a relabel and an unblock-override fix."""
    import hermes_cli.kanban_db as _kb

    now = 7_200_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="park-respawn", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        monkeypatch.setattr(_kb.time, "time", lambda: now)
        assert kb._park_integration(
            conn, tid, {"reason": "dirty overlap"}, expected_run_id=run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"
        monkeypatch.setattr(_kb.time, "time", lambda: now + 30)
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "ready"
        monkeypatch.setattr(_kb.time, "time", lambda: now + 60)
        assert kb.check_respawn_guard(conn, tid) is None


def test_summarize_dispatch_holds_empty_and_no_holds():
    """B: nothing held → (0, {}, None), for both an empty list and a result
    that spawned nothing but recorded no advisory holds (the genuine stuck
    signal: total_held == 0)."""
    assert kb.summarize_dispatch_holds([]) == (0, {}, None)
    assert kb.summarize_dispatch_holds([kb.DispatchResult()]) == (0, {}, None)


def test_summarize_dispatch_holds_single_bucket():
    """B: one bucket → counts + dominant name it."""
    res = kb.DispatchResult()
    res.respawn_guarded = [("t1", "recent_success"), ("t2", "recent_success")]
    total, counts, dominant = kb.summarize_dispatch_holds([res])
    assert total == 2
    assert counts == {"respawn_guarded": 2}
    assert dominant == "respawn_guarded"


def test_summarize_dispatch_holds_aggregates_and_picks_dominant():
    """B: aggregate across passes (gateway is multi-board); dominant = the
    bucket holding the most tasks."""
    a = kb.DispatchResult()
    a.skipped_repo_serialized = [("t1", "/repo")]
    a.respawn_guarded = [("t2", "recent_success")]
    b = kb.DispatchResult()
    b.skipped_repo_serialized = [("t3", "/repo"), ("t4", "/repo")]
    b.budget_held = [("t5", "premium", "daily_token_cap")]
    total, counts, dominant = kb.summarize_dispatch_holds([a, b])
    assert total == 5
    assert counts == {"repo_serialized": 3, "respawn_guarded": 1, "budget_held": 1}
    assert dominant == "repo_serialized"


def test_summarize_dispatch_holds_ignores_none_and_non_hold_buckets():
    """B: None entries are skipped; spawned / skipped_unassigned are NOT
    expected-hold buckets (unassigned stays operator-actionable / stuck)."""
    res = kb.DispatchResult()
    res.spawned = [("t1", "a", "/ws")]
    res.skipped_unassigned = ["t2"]
    assert kb.summarize_dispatch_holds([None, res]) == (0, {}, None)


def test_resolve_rate_limit_cooldown_handles_bad_env(monkeypatch):
    import hermes_cli.kanban_db as _kb

    for bad_val in ("notanumber", "-5", ""):
        monkeypatch.setenv(
            "HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", bad_val
        )
        assert (
            _kb._resolve_rate_limit_cooldown_seconds()
            == _kb.DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
        )


def test_max_runtime_uses_current_run_start_after_retry(kanban_home, monkeypatch):
    """A retry should get a fresh max-runtime window.

    ``tasks.started_at`` intentionally records the first time the task ever
    started. Runtime enforcement must therefore use the active
    ``task_runs.started_at`` row; otherwise every retry of an old task is
    immediately timed out again.
    """
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        t = kb.create_task(
            conn, title="retry", assignee="a", max_runtime_seconds=10,
        )

        kb.claim_task(conn, t, claimer=f"{host}:first")
        first_run_id = kb.latest_run(conn, t).id
        old_started = int(time.time()) - 20
        conn.execute(
            "UPDATE tasks SET started_at = ?, worker_pid = ? WHERE id = ?",
            (old_started, 999999, t),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ?, worker_pid = ? WHERE id = ?",
            (old_started, 999999, first_run_id),
        )

        timed_out = kb.enforce_max_runtime(conn, signal_fn=lambda _pid, _sig: None)
        assert timed_out == [t]
        assert kb.get_task(conn, t).status == "ready"

        kb.claim_task(conn, t, claimer=f"{host}:retry")
        retry_run = kb.latest_run(conn, t)
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (999999, t),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
            (999999, retry_run.id),
        )

        timed_out = kb.enforce_max_runtime(conn, signal_fn=lambda _pid, _sig: None)
        assert timed_out == []
        assert kb.get_task(conn, t).status == "running"


def test_heartbeat_extends_claim(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        claimer = "host:hb"
        kb.claim_task(conn, t, claimer=claimer, ttl_seconds=60)
        original = kb.get_task(conn, t).claim_expires
        # Rewind then heartbeat.
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (0, t))
        ok = kb.heartbeat_claim(conn, t, claimer=claimer, ttl_seconds=3600)
        assert ok
        new = kb.get_task(conn, t).claim_expires
        assert new > int(time.time()) + 3000


def test_heartbeat_uses_env_default_ttl(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_TTL_SECONDS", "3600")
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        claimer = "host:hb"
        kb.claim_task(conn, t, claimer=claimer, ttl_seconds=60)
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (0, t))
        ok = kb.heartbeat_claim(conn, t, claimer=claimer)
        assert ok
        new = kb.get_task(conn, t).claim_expires
        assert new is not None
        assert new > int(time.time()) + 3000


def test_concurrent_claims_only_one_wins(kanban_home):
    """Fire N threads claiming the same task; exactly one must win."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="race", assignee="a")

    def attempt(i):
        with kb.connect_closing() as c:
            return kb.claim_task(c, t, claimer=f"host:{i}")

    n_workers = 8
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as ex:
        results = list(ex.map(attempt, range(n_workers)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].status == "running"

