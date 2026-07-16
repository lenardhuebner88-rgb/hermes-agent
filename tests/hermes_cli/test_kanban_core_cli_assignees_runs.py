"""Kanban core functionality tests: cli assignees runs.

Split from test_kanban_core_functionality.py (pure move; no test logic changes).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import run_slash

from tests.hermes_cli._kanban_test_helpers import (
    _write_test_profile,
)

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Existing crash-detection tests pre-date the grace window; pin to 0
    # so they keep their immediate-reclaim semantics.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Disable the detect_crashed_workers grace period for legacy tests in
    # this file that claim a task and immediately expect
    # ``detect_crashed_workers`` to act on it. The grace period (30s by
    # default, see ``DEFAULT_CRASH_GRACE_SECONDS``) prevents the
    # multi-dispatcher reap race in production; setting it to 0 here
    # restores the pre-fix instant-reclaim semantics these tests were
    # written against. The grace-period itself is covered by dedicated
    # tests in tests/hermes_cli/test_kanban_db.py.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# CLI stats / watch / log / notify / daemon parity
# ---------------------------------------------------------------------------

def test_cli_stats_json(kanban_home):
    conn = kb.connect()
    try:
        kb.create_task(conn, title="a", assignee="r")
    finally:
        conn.close()
    out = run_slash("stats --json")
    data = json.loads(out)
    assert "by_status" in data
    assert "by_assignee" in data
    assert "oldest_ready_age_seconds" in data


def test_cli_notify_subscribe_and_list(kanban_home):
    tid = run_slash("create 'x' --json")
    tid = json.loads(tid)["id"]
    out = run_slash(
        f"notify-subscribe {tid} --platform telegram --chat-id 999",
    )
    assert "Subscribed" in out
    lst = run_slash("notify-list --json")
    subs = json.loads(lst)
    assert any(s["task_id"] == tid and s["platform"] == "telegram" for s in subs)
    rm = run_slash(
        f"notify-unsubscribe {tid} --platform telegram --chat-id 999",
    )
    assert "Unsubscribed" in rm


def test_cli_log_missing_task(kanban_home):
    # No such task → exit-style (no log for...) message on stderr, returned
    # in combined output.
    out = run_slash("log t_nope")
    assert "no log" in out.lower()


def test_cli_gc_reports_counts(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x")
        kb.archive_task(conn, tid)
    finally:
        conn.close()
    out = run_slash("gc")
    assert "GC complete" in out


# ---------------------------------------------------------------------------
# run_slash parity — every verb returns a sensible, non-crashy string
# ---------------------------------------------------------------------------

def test_run_slash_every_verb_returns_sensible_output(kanban_home):
    """Smoke-test every verb with minimal args. None may raise, none may
    return the empty string (must either succeed or report a usage error)."""
    # Set up a pair of tasks to reference.
    conn = kb.connect()
    try:
        tid_a = kb.create_task(conn, title="a")
        tid_b = kb.create_task(conn, title="b", parents=[tid_a])
    finally:
        conn.close()

    invocations = [
        "",                                  # no subcommand → help text
        "--help",
        "init",
        "create 'smoke'",
        "list",
        "ls",
        f"show {tid_a}",
        f"assign {tid_a} researcher",
        f"link {tid_a} {tid_b}",
        f"unlink {tid_a} {tid_b}",
        f"claim {tid_a}",
        f"comment {tid_a} hello",
        f"complete {tid_a}",
        f"block {tid_b} need input",
        f"unblock {tid_b}",
        f"archive {tid_a}",
        "dispatch --dry-run --json",
        "stats --json",
        "notify-list",
        f"log {tid_a}",
        f"context {tid_b}",
        "gc",
    ]
    for cmd in invocations:
        out = run_slash(cmd)
        assert out is not None
        assert out.strip() != "", f"empty output for `/kanban {cmd}`"


# ---------------------------------------------------------------------------
# Max-runtime enforcement (item 1 from the Multica audit)
# ---------------------------------------------------------------------------

def test_max_runtime_terminates_overrun_worker(kanban_home):
    """A running task whose elapsed time exceeds max_runtime_seconds gets
    SIGTERM'd, emits a ``timed_out`` event, and goes back to ready."""
    killed = []
    def _signal_fn(pid, sig):
        killed.append((pid, sig))

    # We bypass _pid_alive by stubbing it so the grace-poll exits fast.
    import hermes_cli.kanban_db as _kb
    original_alive = _kb._pid_alive
    _kb._pid_alive = lambda pid: False  # pretend SIGTERM worked immediately

    try:
        conn = kb.connect()
        try:
            tid = kb.create_task(
                conn, title="long job", assignee="worker",
                max_runtime_seconds=1,  # one second cap
            )
            # Spawn by hand: claim + set pid + set active run start to the past.
            kb.claim_task(conn, tid)
            kb._set_worker_pid(conn, tid, os.getpid())   # any live pid works
            # Backdate both the task-level first-start timestamp and the active
            # run timestamp so elapsed > limit under the per-run runtime model.
            old_started = int(time.time()) - 30
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET started_at = ? WHERE id = ?",
                    (old_started, tid),
                )
                conn.execute(
                    "UPDATE task_runs SET started_at = ? "
                    "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                    (old_started, tid),
                )

            timed_out = kb.enforce_max_runtime(conn, signal_fn=_signal_fn)
            assert tid in timed_out
            assert killed and killed[0][0] == os.getpid()

            task = kb.get_task(conn, tid)
            assert task.status == "ready",                 f"timed-out task should reset to ready, got {task.status}"
            assert task.worker_pid is None
            assert task.last_heartbeat_at is None

            events = kb.list_events(conn, tid)
            assert any(e.kind == "timed_out" for e in events)
            to_event = next(e for e in events if e.kind == "timed_out")
            assert to_event.payload["limit_seconds"] == 1
            assert to_event.payload["elapsed_seconds"] >= 30
        finally:
            conn.close()
    finally:
        _kb._pid_alive = original_alive


def test_repeated_timeouts_auto_block_at_default_limit(kanban_home):
    """Two timed_out outcomes on the same task/profile trip the retry guard."""
    import hermes_cli.kanban_db as _kb
    original_alive = _kb._pid_alive
    _kb._pid_alive = lambda pid: False

    def _age_active_run(conn, tid):
        old_started = int(time.time()) - 30
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (old_started, tid),
            )

    try:
        conn = kb.connect()
        try:
            tid = kb.create_task(
                conn, title="long job", assignee="worker",
                max_runtime_seconds=1,
            )
            for expected_failures in (1, 2):
                kb.claim_task(conn, tid)
                kb._set_worker_pid(conn, tid, os.getpid())
                _age_active_run(conn, tid)
                timed_out = kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)
                assert tid in timed_out
                task = kb.get_task(conn, tid)
                assert task.consecutive_failures == expected_failures
            task = kb.get_task(conn, tid)
            assert task.status == "blocked"
            events = kb.list_events(conn, tid)
            assert [e.kind for e in events].count("timed_out") == 2
            gave_up = [e for e in events if e.kind == "gave_up"]
            assert gave_up and gave_up[-1].payload["trigger_outcome"] == "timed_out"
        finally:
            conn.close()
    finally:
        _kb._pid_alive = original_alive


def test_max_runtime_none_means_no_cap(kanban_home):
    """A task with max_runtime_seconds=None is never timed out regardless
    of how long it runs."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="uncapped", assignee="worker")
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, os.getpid())
        # Backdate aggressively; no cap means we don't care.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?",
                (int(time.time()) - 100_000, tid),
            )
        timed_out = kb.enforce_max_runtime(conn)
        assert timed_out == []
        task = kb.get_task(conn, tid)
        assert task.status == "running"
    finally:
        conn.close()


def test_create_task_persists_max_runtime(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", max_runtime_seconds=600)
        task = kb.get_task(conn, tid)
        assert task.max_runtime_seconds == 600
    finally:
        conn.close()


def test_enforce_max_runtime_integrates_with_dispatch(kanban_home, monkeypatch):
    """enforce_max_runtime + dispatch_once integrate cleanly — a timed-out
    task goes through ``timed_out`` → ``ready`` and dispatch_once can then
    re-spawn it without re-reporting the timeout."""
    import hermes_cli.kanban_db as _kb
    # Leave _pid_alive=True so the crash detector doesn't steal the task
    # before timeout enforcement runs. After SIGTERM in enforce_max_runtime,
    # pretend the worker died so the grace wait exits fast.
    state = {"sent_term": False}
    def _alive(pid):
        return not state["sent_term"]
    def _signal(pid, sig):
        import signal as _sig
        if sig == _sig.SIGTERM:
            state["sent_term"] = True
    monkeypatch.setattr(_kb, "_pid_alive", _alive)

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="timeout-me", assignee="worker",
            max_runtime_seconds=1,
        )
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, os.getpid())
        old_started = int(time.time()) - 30
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?",
                (old_started, tid),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (old_started, tid),
            )
        # Use enforce_max_runtime directly with our signal stub — dispatch_once
        # uses the default os.kill, but integration-wise calling
        # enforce_max_runtime directly proves the kernel wiring. For the
        # dispatch_once assertion, rely on its own code path by calling it
        # after forcing SIGTERM via enforce_max_runtime.
        before = kb.enforce_max_runtime(conn, signal_fn=_signal)
        assert tid in before, "kernel enforce_max_runtime should catch the overrun"

        # Now a second dispatch_once run should be a no-op on this task
        # (already released). Confirm the loop doesn't re-report it.
        res = kb.dispatch_once(conn, spawn_fn=lambda t, ws: None)
        task = kb.get_task(conn, tid)
        # After timeout, task is back in 'ready' and will be re-spawned
        # by the same pass. That's the intended behaviour.
        assert task.status in {"ready", "running"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Heartbeat (item 2 from the Multica audit)
# ---------------------------------------------------------------------------

def test_heartbeat_on_running_task(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        ok = kb.heartbeat_worker(conn, tid, note="step 3/10")
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.last_heartbeat_at is not None
        events = kb.list_events(conn, tid)
        hb = [e for e in events if e.kind == "heartbeat"]
        assert len(hb) == 1
        assert hb[0].payload == {"note": "step 3/10"}
    finally:
        conn.close()


def test_heartbeat_refused_when_not_running(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x")   # lands in ready, not running
        ok = kb.heartbeat_worker(conn, tid)
        assert ok is False
        task = kb.get_task(conn, tid)
        assert task.last_heartbeat_at is None
    finally:
        conn.close()


def test_cli_heartbeat_verb(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    out = run_slash(f"heartbeat {tid}")
    assert "Heartbeat recorded" in out

    # With --note.
    out = run_slash(f"heartbeat {tid} --note 'step 42'")
    assert "Heartbeat recorded" in out
    conn = kb.connect()
    try:
        events = kb.list_events(conn, tid)
        notes = [e.payload.get("note") for e in events if e.kind == "heartbeat" and e.payload]
        assert "step 42" in notes
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Event vocab rename + spawned event (item 3 from Multica)
# ---------------------------------------------------------------------------

def test_recompute_ready_emits_promoted_not_ready(kanban_home):
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="p")
        child = kb.create_task(conn, title="c", parents=[parent])
        kb.complete_task(conn, parent, result="ok")
        # recompute_ready runs inside complete_task too, but call it again
        # defensively.
        kb.recompute_ready(conn)
        events = kb.list_events(conn, child)
        kinds = [e.kind for e in events]
        assert "promoted" in kinds
        # Old name must not appear.
        assert "ready" not in kinds
    finally:
        conn.close()


def test_spawn_failure_circuit_breaker_emits_gave_up(kanban_home, all_assignees_spawnable, monkeypatch):
    """The breaker emits ``gave_up`` when it trips — after the transient-retry
    budget is spent (post-f54686689) and ``consecutive_failures`` reaches the
    limit. The transient budget is exercised first (``transient_retry`` events)."""
    base = 1_800_000_000
    clock = [base]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])

    def _bad(task, ws):
        raise RuntimeError("nope")
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        for _ in range(8):
            kb.dispatch_once(conn, spawn_fn=_bad, failure_limit=2)
            if kb.get_task(conn, tid).status == "blocked":
                break
            clock[0] += 301
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "transient_retry" in kinds  # budget exercised before the breaker
        assert "gave_up" in kinds
        assert "spawn_auto_blocked" not in kinds
    finally:
        conn.close()


def test_spawned_event_emitted_with_pid(kanban_home, all_assignees_spawnable):
    """Successful spawn must append a ``spawned`` event with the pid in
    the payload so humans tailing events see pid tracking."""
    def _spawn_returns_pid(task, ws):
        return 98765
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.dispatch_once(conn, spawn_fn=_spawn_returns_pid)
        events = kb.list_events(conn, tid)
        spawned = [e for e in events if e.kind == "spawned"]
        assert len(spawned) == 1
        assert spawned[0].payload == {"pid": 98765}
    finally:
        conn.close()


def test_migration_renames_legacy_event_kinds(tmp_path, monkeypatch):
    """A DB created with the old vocab must have its event rows renamed
    in place on init_db()."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Init fresh.
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x")
        # Inject legacy event kinds directly.
        now = int(time.time())
        with kb.write_txn(conn):
            for old in ("ready", "priority", "spawn_auto_blocked"):
                conn.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "VALUES (?, ?, NULL, ?)",
                    (tid, old, now),
                )
        # Re-run init_db — the migration pass should rename them.
        kb.init_db()
        rows = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,),
        ).fetchall()
        kinds = [r["kind"] for r in rows]
        assert "ready" not in kinds
        assert "priority" not in kinds
        assert "spawn_auto_blocked" not in kinds
        assert "promoted" in kinds
        assert "reprioritized" in kinds
        assert "gave_up" in kinds
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Assignees (item 4 from Multica)
# ---------------------------------------------------------------------------

def test_list_profiles_on_disk(tmp_path, monkeypatch):
    """list_profiles_on_disk returns the implicit default profile plus
    named profiles under ~/.hermes/profiles/ that contain a config.yaml."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    profiles = tmp_path / ".hermes" / "profiles"
    profiles.mkdir(parents=True)
    for name in ("researcher", "writer"):
        d = profiles / name
        d.mkdir()
        (d / "config.yaml").write_text("model: {}\n")
    (profiles / "empty_dir").mkdir()
    # A stray file; should be ignored.
    (profiles / "stray.txt").write_text("noise")

    names = kb.list_profiles_on_disk()
    assert names == ["default", "researcher", "writer"]


def test_list_profiles_on_disk_custom_root(tmp_path, monkeypatch):
    """list_profiles_on_disk respects a custom HERMES_HOME root."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profiles = tmp_path / "profiles"
    profiles.mkdir(parents=True)
    for name in ("researcher", "writer"):
        d = profiles / name
        d.mkdir()
        (d / "config.yaml").write_text("model: {}\n")

    names = kb.list_profiles_on_disk()
    assert names == ["default", "researcher", "writer"]


def test_known_assignees_merges_disk_and_board(tmp_path, monkeypatch):
    """known_assignees unions profiles on disk with currently-assigned
    names, and reports per-status counts."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    profiles = tmp_path / ".hermes" / "profiles"
    profiles.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    for name in ("researcher", "writer"):
        d = profiles / name
        d.mkdir()
        (d / "config.yaml").write_text("model: {}\n")

    kb.init_db()
    conn = kb.connect()
    try:
        # writer has a ready task; on_board_only has a task but no profile dir.
        kb.create_task(conn, title="a", assignee="writer")
        kb.create_task(conn, title="b", assignee="on_board_only")
        data = kb.known_assignees(conn)
    finally:
        conn.close()

    by_name = {d["name"]: d for d in data}
    assert by_name["default"]["on_disk"] is True
    assert by_name["default"]["counts"] == {}
    assert by_name["researcher"]["on_disk"] is True
    assert by_name["researcher"]["counts"] == {}
    assert by_name["writer"]["on_disk"] is True
    assert by_name["writer"]["counts"] == {"ready": 1}
    assert by_name["on_board_only"]["on_disk"] is False
    assert by_name["on_board_only"]["counts"] == {"ready": 1}


def test_decompose_does_not_mutate_children_on_late_validation_error(kanban_home):
    _write_test_profile(kanban_home, "premium")
    conn = kb.connect()
    root = kb.create_task(conn, title="triage", assignee="default", triage=True)
    children = [
        {"title": "alias child", "assignee": "coder-claude"},
        {"title": "bad parent", "assignee": "premium", "parents": [99]},
    ]

    with pytest.raises(ValueError, match="not a valid index"):
        kb.decompose_triage_task(conn, root, root_assignee=None, children=children)

    assert children[0]["assignee"] == "coder-claude"


def test_decompose_rejects_invalid_root_assignee_atomically(kanban_home):
    _write_test_profile(kanban_home, "coder")
    conn = kb.connect()
    root = kb.create_task(conn, title="triage", assignee="default", triage=True)

    with pytest.raises(ValueError, match="not spawnable"):
        kb.decompose_triage_task(
            conn,
            root,
            root_assignee="researcher",
            children=[{"title": "child", "assignee": "coder"}],
            validate_assignees=True,
        )

    tasks = kb.list_tasks(conn)
    assert [t.id for t in tasks] == [root]
    assert tasks[0].status == "triage"
    assert tasks[0].assignee == "default"


def test_decompose_canonicalizes_root_assignee_alias(kanban_home):
    _write_test_profile(kanban_home, "premium")
    _write_test_profile(kanban_home, "coder")
    conn = kb.connect()
    root = kb.create_task(conn, title="triage", assignee="default", triage=True)

    child_ids = kb.decompose_triage_task(
        conn,
        root,
        root_assignee="coder-claude",
        children=[{"title": "child", "assignee": "coder"}],
    )

    assert child_ids
    root_task = kb.get_task(conn, root)
    assert root_task.assignee == "premium"


def test_decompose_normalizes_blank_child_assignee_to_none(kanban_home):
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="root", triage=True)
        children = kb.decompose_triage_task(
            conn,
            root,
            children=[
                {"title": "empty assignee child", "assignee": ""},
                {"title": "blank assignee child", "assignee": "   "},
            ],
            root_assignee=None,
            validate_assignees=True,
        )
        assert children is not None
        for child_id in children:
            child = kb.get_task(conn, child_id)
            assert child is not None
            assert child.assignee is None
    finally:
        conn.close()


def test_decompose_rejects_non_spawnable_child_assignee(kanban_home):
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="root", triage=True)
        with pytest.raises(ValueError, match="not spawnable"):
            kb.decompose_triage_task(
                conn,
                root,
                root_assignee=None,
                children=[
                    {"title": "bad child", "assignee": "researcher"},
                ],
                validate_assignees=True,
            )
        assert conn.execute("SELECT COUNT(*) FROM task_links WHERE child_id = ? OR parent_id = ?", (root, root)).fetchone()[0] == 0
    finally:
        conn.close()


def test_decompose_normalizes_legacy_child_assignee_alias(kanban_home):
    _write_test_profile(kanban_home, "premium")
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="root", triage=True)
        children = kb.decompose_triage_task(
            conn,
            root,
            root_assignee=None,
            children=[
                {"title": "hard child", "assignee": "coder-claude"},
            ],
        )
        assert children
        child = kb.get_task(conn, children[0])
        assert child.assignee == "premium"
    finally:
        conn.close()


def test_kanban_create_rejected_assignee_exits_nonzero(tmp_path, monkeypatch):
    """CLI exit status must reflect rejected Kanban preflight checks.

    Automation, Gateway smoke jobs, and operator scripts should not need to parse
    stderr to know that a task was rejected before creation.
    """
    import os
    import subprocess
    import sys

    hermes_home = tmp_path / ".hermes"
    profile = hermes_home / "profiles" / "coder"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: {}\n")

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "kanban",
            "create",
            "bad lane",
            "--assignee",
            "researcher",
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 2
    assert "not spawnable" in result.stderr
    assert kb.list_tasks(kb.connect()) == []


def test_known_assignees_omits_off_disk_done_only_ghosts(tmp_path, monkeypatch):
    """Stale historical lanes should not stay selectable forever.

    Off-disk assignees with active work remain visible so operators can repair
    or reassign them; off-disk assignees with only completed work are audit
    history, not spawnable lane choices.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    profiles = tmp_path / ".hermes" / "profiles"
    (profiles / "coder").mkdir(parents=True)
    (profiles / "coder" / "config.yaml").write_text("model: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    kb.init_db()
    conn = kb.connect()
    try:
        ghost_done = kb.create_task(conn, title="old alias", assignee="ghost_done")
        kb.complete_task(conn, ghost_done)

        mixed_done = kb.create_task(conn, title="old mixed", assignee="ghost_mixed")
        kb.complete_task(conn, mixed_done)
        kb.create_task(conn, title="needs reassignment", assignee="ghost_mixed")

        coder_done = kb.create_task(conn, title="spawnable history", assignee="coder")
        kb.complete_task(conn, coder_done)
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, created_at) VALUES (?, ?, ?, ?, ?)",
            ("t_blank_ghost", "historical blank", "ready", "", 1),
        )
        conn.commit()

        data = kb.known_assignees(conn)
    finally:
        conn.close()

    by_name = {d["name"]: d for d in data}
    assert "ghost_done" not in by_name
    assert by_name["ghost_mixed"]["on_disk"] is False
    assert by_name["ghost_mixed"]["counts"] == {"done": 1, "ready": 1}
    assert by_name["coder"]["on_disk"] is True
    assert by_name["coder"]["counts"] == {"done": 1}
    assert "" not in by_name


def test_cli_assignees_json(kanban_home):
    conn = kb.connect()
    try:
        kb.create_task(conn, title="x", assignee="someone")
    finally:
        conn.close()
    out = run_slash("assignees --json")
    data = json.loads(out)
    names = [e["name"] for e in data]
    assert "someone" in names


# ---------------------------------------------------------------------------
# CLI --max-runtime flag + duration parser
# ---------------------------------------------------------------------------

def test_parse_duration_accepts_formats():
    from hermes_cli.kanban import _parse_duration
    assert _parse_duration(None) is None
    assert _parse_duration("") is None
    assert _parse_duration("42") == 42
    assert _parse_duration("30s") == 30
    assert _parse_duration("5m") == 300
    assert _parse_duration("2h") == 7200
    assert _parse_duration("1d") == 86400
    assert _parse_duration("1.5h") == 5400


def test_parse_duration_rejects_garbage():
    from hermes_cli.kanban import _parse_duration
    import pytest as _p
    with _p.raises(ValueError):
        _parse_duration("tenminutes")
    with _p.raises(ValueError):
        _parse_duration("fish")


def test_cli_create_max_runtime_via_duration(kanban_home):
    """`hermes kanban create --max-runtime 2h` should persist 7200 seconds."""
    out = run_slash("create 'long task' --max-runtime 2h --json")
    data = json.loads(out)
    tid = data["id"]
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.max_runtime_seconds == 7200
    finally:
        conn.close()


def test_cli_create_max_runtime_bad_format_exits_nonzero(kanban_home):
    out = run_slash("create 'bad' --max-runtime fish")
    assert "max-runtime" in out.lower() or "malformed" in out.lower()


# ---------------------------------------------------------------------------
# Runs as first-class (vulcan-artivus RFC feedback)
# ---------------------------------------------------------------------------

def test_run_created_on_claim(kanban_home):
    """claim_task opens a new task_runs row and points current_run_id at it."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        assert kb.get_task(conn, tid).current_run_id is None

        claimed = kb.claim_task(conn, tid)
        assert claimed is not None

        task = kb.get_task(conn, tid)
        assert task.current_run_id is not None

        runs = kb.list_runs(conn, tid)
        assert len(runs) == 1
        r = runs[0]
        assert r.id == task.current_run_id
        assert r.profile == "worker"
        assert r.status == "running"
        assert r.outcome is None
        assert r.ended_at is None
        assert r.claim_lock is not None and r.claim_expires is not None
    finally:
        conn.close()


def test_run_closed_on_complete_with_summary(kanban_home):
    """complete_task ends the active run with outcome='completed' and
    persists summary + metadata."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(
            conn, tid,
            result="shipped",
            summary="implemented rate limiter, tests pass",
            metadata={"changed_files": ["limiter.py"], "tests_run": 12},
        )
        assert ok is True

        task = kb.get_task(conn, tid)
        assert task.current_run_id is None
        assert task.result == "shipped"

        runs = kb.list_runs(conn, tid)
        assert len(runs) == 1
        r = runs[0]
        assert r.status == "done"
        assert r.outcome == "completed"
        assert r.summary == "implemented rate limiter, tests pass"
        # Run completion now appends cost provenance when no concrete cost is
        # available; caller-provided metadata must remain intact alongside it.
        assert r.metadata["changed_files"] == ["limiter.py"]
        assert r.metadata["tests_run"] == 12
        assert r.metadata["cost"] == {"cost_status": "unknown"}
        assert r.ended_at is not None
    finally:
        conn.close()


def test_run_summary_falls_back_to_result(kanban_home):
    """If the caller doesn't pass summary, we fall back to result so
    single-run workflows don't need to pass the same string twice."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="only-arg")
        r = kb.latest_run(conn, tid)
        assert r.summary == "only-arg"
    finally:
        conn.close()


def test_multiple_attempts_preserved_as_runs(kanban_home):
    """Reclaim / transient-recovery / complete flow produces one run per
    attempt, all visible in list_runs in chronological order.

    Post-1bd00640c an *unknown* dead worker PID (no recorded exit) is treated
    as a bounded transient recovery, so attempt 2's run closes with outcome
    ``transient_retry`` rather than ``crashed`` — but it is still a distinct
    run, which is what this test guards. The real-crash run outcome is covered
    by tests/hermes_cli/test_kanban_death_recovery.py."""
    import hermes_cli.kanban_db as _kb
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")

        # Attempt 1: claim then force the claim to be stale by backdating
        # claim_expires, then let release_stale_claims reclaim it.
        kb.claim_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                (int(time.time()) - 10, tid),
            )
            conn.execute(
                "UPDATE task_runs SET claim_expires = ? WHERE task_id = ?",
                (int(time.time()) - 10, tid),
            )
        kb.release_stale_claims(conn)

        # Attempt 2: claim then lose the worker PID (simulated: pid dead).
        # An unknown dead PID is a transient recovery, not a hard crash.
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, 98765)
        original_alive = _kb._pid_alive
        _kb._pid_alive = lambda pid: False
        try:
            kb.detect_crashed_workers(conn)
        finally:
            _kb._pid_alive = original_alive

        # Attempt 3: claim then complete.
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="finally")

        runs = kb.list_runs(conn, tid)
        assert len(runs) == 3
        assert [r.outcome for r in runs] == [
            "reclaimed", kb.TRANSIENT_RETRY_OUTCOME, "completed",
        ]
        assert runs[-1].summary == "finally"
        assert kb.get_task(conn, tid).current_run_id is None
    finally:
        conn.close()


def test_stale_run_cannot_complete_new_attempt(kanban_home, monkeypatch):
    """A worker from an earlier attempt cannot close a later retry."""
    import hermes_cli.kanban_db as _kb

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="retry guarded", assignee="worker")

        kb.claim_task(conn, tid)
        run1 = kb.latest_run(conn, tid)
        kb._set_worker_pid(conn, tid, 98765)
        monkeypatch.setattr(_kb, "_pid_alive", lambda pid: False)
        # An unknown dead PID is a bounded transient recovery (post-1bd00640c):
        # it closes run1 and requeues, but is reported on the
        # ``_last_transient_recovered`` side-channel, NOT the ``crashed`` return.
        assert kb.detect_crashed_workers(conn) == []
        assert kb.detect_crashed_workers._last_transient_recovered == [tid]

        kb.claim_task(conn, tid)
        run2 = kb.latest_run(conn, tid)
        assert run2.id != run1.id

        assert not kb.complete_task(
            conn,
            tid,
            summary="late stale completion",
            expected_run_id=run1.id,
        )
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.current_run_id == run2.id

        assert kb.complete_task(
            conn,
            tid,
            summary="current completion",
            expected_run_id=run2.id,
        )
        runs = kb.list_runs(conn, tid)
        assert [r.outcome for r in runs] == [
            kb.TRANSIENT_RETRY_OUTCOME, "completed",
        ]
        assert runs[-1].summary == "current completion"
    finally:
        conn.close()


def test_stale_run_cannot_block_or_heartbeat_new_attempt(kanban_home, monkeypatch):
    """Stale retry attempts cannot mutate the active run lifecycle."""
    import hermes_cli.kanban_db as _kb

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="retry heartbeat guarded", assignee="worker")

        kb.claim_task(conn, tid)
        run1 = kb.latest_run(conn, tid)
        kb._set_worker_pid(conn, tid, 98765)
        monkeypatch.setattr(_kb, "_pid_alive", lambda pid: False)
        # An unknown dead PID is a bounded transient recovery (post-1bd00640c):
        # it closes run1 and requeues, but is reported on the
        # ``_last_transient_recovered`` side-channel, NOT the ``crashed`` return.
        assert kb.detect_crashed_workers(conn) == []
        assert kb.detect_crashed_workers._last_transient_recovered == [tid]

        kb.claim_task(conn, tid)
        run2 = kb.latest_run(conn, tid)
        assert run2.id != run1.id

        assert not kb.heartbeat_worker(conn, tid, note="late", expected_run_id=run1.id)
        assert not kb.block_task(conn, tid, reason="late block", expected_run_id=run1.id)
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.current_run_id == run2.id
        assert task.last_heartbeat_at is None

        assert kb.heartbeat_worker(conn, tid, note="current", expected_run_id=run2.id)
        assert kb.block_task(conn, tid, reason="current block", expected_run_id=run2.id)
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


def test_run_on_block_with_reason(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        kb.block_task(conn, tid, reason="needs API key")

        r = kb.latest_run(conn, tid)
        assert r.outcome == "blocked"
        assert r.summary == "needs API key"
        assert r.ended_at is not None
        assert kb.get_task(conn, tid).current_run_id is None
    finally:
        conn.close()


def test_run_on_spawn_failure_records_failed_runs(kanban_home, all_assignees_spawnable, monkeypatch):
    """Post-f54686689 each dispatch attempt closes a run: the first
    ``TRANSIENT_RETRY_LIMIT`` (2) spawn failures close runs with
    outcome='transient_retry'; once the budget is spent a failure closes a run
    with outcome='spawn_failed', and the breaker-tripping one closes a run with
    outcome='gave_up'."""
    base = 1_800_000_000
    clock = [base]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])

    def _bad(task, ws):
        raise RuntimeError("no PATH")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        for _ in range(8):
            kb.dispatch_once(conn, spawn_fn=_bad, failure_limit=2)
            if kb.get_task(conn, tid).status == "blocked":
                break
            clock[0] += 301

        runs = kb.list_runs(conn, tid)
        outcomes = [r.outcome for r in runs]
        assert outcomes == ["transient_retry", "transient_retry", "spawn_failed", "gave_up"]
        assert runs[-1].error and "no PATH" in runs[-1].error
    finally:
        conn.close()


def test_event_rows_carry_run_id(kanban_home):
    """task_events.run_id is populated for run-scoped kinds and NULL for
    task-scoped ones."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        # task-scoped: 'created' — no run yet
        # run-scoped: 'claimed' + 'completed'
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="ok")

        rows = conn.execute(
            "SELECT kind, run_id FROM task_events WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
        by_kind = {r["kind"]: r["run_id"] for r in rows}
        assert by_kind["created"] is None
        assert by_kind["claimed"] is not None
        assert by_kind["completed"] is not None
        # Both belong to the same run.
        assert by_kind["claimed"] == by_kind["completed"]
    finally:
        conn.close()


def test_build_worker_context_includes_prior_attempts(kanban_home):
    """A worker spawned after a prior attempt sees that attempt's outcome
    + summary in its context so it can skip the failed path."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="port x", assignee="worker")

        # Attempt 1: blocked with a reason.
        kb.claim_task(conn, tid)
        kb.block_task(conn, tid, reason="needs clarification on IP vs user_id")
        kb.unblock_task(conn, tid)

        # Attempt 2: claim (but don't complete yet) and read the context
        # as this worker would see it.
        kb.claim_task(conn, tid)
        ctx = kb.build_worker_context(conn, tid)

        assert "Prior attempts on this task" in ctx
        assert "blocked" in ctx
        assert "needs clarification on IP vs user_id" in ctx
    finally:
        conn.close()


def test_build_worker_context_uses_parent_run_summary(kanban_home):
    """Downstream children read the parent's run.summary + metadata, not
    just task.result."""
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="research", assignee="researcher")
        child = kb.create_task(
            conn, title="write", assignee="writer", parents=[parent],
        )

        kb.claim_task(conn, parent)
        kb.complete_task(
            conn, parent,
            result="done",
            summary="three angles explored; B looks strongest",
            metadata={"sources": ["paper A", "paper B", "paper C"]},
        )

        # child becomes ready via recompute_ready (runs inside complete_task)
        ctx = kb.build_worker_context(conn, child)
        assert "Parent task results" in ctx
        assert "three angles explored; B looks strongest" in ctx
        assert '"sources"' in ctx  # metadata JSON serialized
    finally:
        conn.close()


def test_build_worker_context_labels_scout_parent_advisory(kanban_home):
    """A scout parent's result renders under '## Advisory scout notes' with a
    source-of-truth warning, NOT under the equal-weight '## Parent task
    results' — so the coder treats scout recon as hints, not a committed
    parent outcome it must follow (incident: scout t_6661c5cc off-scope rec)."""
    conn = kb.connect()
    try:
        scout = kb.create_task(conn, title="Scout: redaction", assignee="scout")
        child = kb.create_task(
            conn, title="redaction", assignee="coder", parents=[scout],
        )
        kb.claim_task(conn, scout)
        kb.complete_task(
            conn, scout, summary="look at acp_adapter/permissions.py",
        )
        ctx = kb.build_worker_context(conn, child)
        assert "## Advisory scout notes" in ctx
        assert "source of truth" in ctx.lower()
        # the scout's recon text lives ONLY in the advisory section, not under
        # an equal-weight parent-results header.
        assert "## Parent task results" not in ctx
        assert f"### {scout} (scout)" in ctx
    finally:
        conn.close()


def test_build_worker_context_splits_scout_from_real_parents(kanban_home):
    """With both a scout parent and a real (non-scout) parent, the real parent
    stays under '## Parent task results' and the scout moves to advisory."""
    conn = kb.connect()
    try:
        builder = kb.create_task(conn, title="prep", assignee="researcher")
        scout = kb.create_task(conn, title="Scout: prep", assignee="scout")
        child = kb.create_task(
            conn, title="impl", assignee="coder", parents=[builder, scout],
        )
        for pid, summary in ((builder, "authoritative parent result"),
                             (scout, "advisory recon hint")):
            kb.claim_task(conn, pid)
            kb.complete_task(conn, pid, summary=summary)
        ctx = kb.build_worker_context(conn, child)
        assert "## Parent task results" in ctx
        assert "authoritative parent result" in ctx
        assert "## Advisory scout notes" in ctx
        assert "advisory recon hint" in ctx
        # real parent appears before the advisory scout section
        assert ctx.index("## Parent task results") < ctx.index("## Advisory scout notes")
    finally:
        conn.close()


def test_build_worker_context_recent_work_is_tenant_scoped(kanban_home):
    """Recent-work role history must not leak across tenants: a worker in
    tenant A sees its own tenant's history, never tenant B's."""
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="A job", assignee="coder", tenant="tenant-a")
        kb.claim_task(conn, a)
        kb.complete_task(conn, a, summary="did the tenant-A thing")
        b = kb.create_task(conn, title="B job", assignee="coder", tenant="tenant-b")
        kb.claim_task(conn, b)
        kb.complete_task(conn, b, summary="did the tenant-B thing")

        new_a = kb.create_task(
            conn, title="A followup", assignee="coder", tenant="tenant-a",
        )
        ctx = kb.build_worker_context(conn, new_a)
        assert "## Recent work by @coder" in ctx
        assert "did the tenant-A thing" in ctx
        # cross-tenant contamination is the bug we are fixing:
        assert "did the tenant-B thing" not in ctx
        # retained rows are explicitly labelled non-authoritative
        assert "NOT this" in ctx.split("## Recent work by")[1]
    finally:
        conn.close()


def test_build_worker_context_suppresses_recent_work_for_scout(kanban_home):
    """A scout takes its scope from the target task body, never from prior
    (possibly off-scope) scout runs — so it gets no recent-work block."""
    conn = kb.connect()
    try:
        for i in range(3):
            tid = kb.create_task(conn, title=f"Scout: prior #{i}", assignee="scout")
            kb.claim_task(conn, tid)
            kb.complete_task(conn, tid, summary=f"prior scout finding #{i}")
        new_scout = kb.create_task(conn, title="Scout: new", assignee="scout")
        ctx = kb.build_worker_context(conn, new_scout)
        assert "## Recent work by" not in ctx
    finally:
        conn.close()


def test_relative_age_renders_coarse_buckets():
    """Freshness helper turns epoch seconds into coarse human ages, and
    degrades safely on missing / future timestamps."""
    now = 1_000_000
    assert kb._relative_age(now, now) == "just now"
    assert kb._relative_age(now - 30, now) == "just now"
    assert kb._relative_age(now - 5 * 60, now) == "5m ago"
    assert kb._relative_age(now - 18 * 3600, now) == "18h ago"
    assert kb._relative_age(now - 2 * 86400, now) == "2d ago"
    # Clock skew across machines/profiles must not claim "in the future".
    assert kb._relative_age(now + 500, now) == "just now"
    # Missing / unparseable timestamps render empty so callers can append
    # unconditionally.
    assert kb._relative_age(None, now) == ""
    # Defensive: an unparseable value (e.g. a stray string) renders empty
    # rather than raising.
    assert kb._relative_age("garbage", now) == ""  # type: ignore[arg-type]


def test_build_worker_context_stamps_parent_freshness(kanban_home):
    """Parent handoffs carry a relative age + a 'verify against source'
    frame so a worker doesn't read a day-old result as live state.

    This is the multi-agent staleness gap: an orchestrator + sibling
    workers leave reports/handoffs that the next worker reads as current
    truth. The age stamp is the signal that prompts re-verification.
    """
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="research", assignee="researcher")
        child = kb.create_task(
            conn, title="write", assignee="writer", parents=[parent],
        )
        kb.claim_task(conn, parent)
        kb.complete_task(
            conn, parent,
            result="done",
            summary="meeting ingest workflow finished; pipeline ready",
        )
        # Backdate the parent's completion to 18h ago — both the task row
        # and its completed run row, which is where build_worker_context
        # reads the handoff timestamp from.
        old = int(time.time()) - 18 * 3600
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET completed_at = ? WHERE id = ?", (old, parent),
            )
            conn.execute(
                "UPDATE task_runs SET ended_at = ? WHERE task_id = ?",
                (old, parent),
            )

        ctx = kb.build_worker_context(conn, child)
        # The handoff still appears...
        assert "meeting ingest workflow finished" in ctx
        # ...now stamped with its age and framed as a point-in-time snapshot.
        assert "completed 18h ago" in ctx
        assert "point-in-time snapshots, not live state" in ctx
    finally:
        conn.close()


def test_migration_backfills_inflight_run_for_legacy_db(kanban_home):
    """An existing 'running' task from before task_runs existed should
    get a synthesized run row so subsequent operations (complete,
    heartbeat) have something to write to."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="pre-migration", assignee="worker")
        # Simulate legacy: set running + claim_lock directly, leave
        # current_run_id NULL and delete the run row the claim created.
        kb.claim_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_runs WHERE task_id = ?", (tid,))
            conn.execute(
                "UPDATE tasks SET current_run_id = NULL WHERE id = ?",
                (tid,),
            )

        # Sanity: no runs, no pointer.
        assert kb.list_runs(conn, tid) == []
        assert kb.get_task(conn, tid).current_run_id is None

        # Re-run init_db — migration backfill should kick in.
        kb.init_db()
        conn2 = kb.connect()
        try:
            runs = kb.list_runs(conn2, tid)
            assert len(runs) == 1
            assert runs[0].status == "running"
            assert runs[0].profile == "worker"
            task = kb.get_task(conn2, tid)
            assert task.current_run_id == runs[0].id

            # Subsequent complete closes the backfilled run cleanly.
            kb.complete_task(conn2, tid, result="done", summary="ok")
            r = kb.latest_run(conn2, tid)
            assert r.outcome == "completed"
            assert r.summary == "ok"
        finally:
            conn2.close()
    finally:
        conn.close()


def test_forward_compat_columns_writable(kanban_home):
    """v2 will route by workflow_template_id + current_step_key. In v1
    these are nullable, kernel doesn't consult them for routing, but
    they must be writable so a v2 client can populate them without
    schema changes."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workflow_template_id = ?, current_step_key = ? "
                "WHERE id = ?",
                ("code-review-v1", "implement", tid),
            )
        task = kb.get_task(conn, tid)
        assert task.workflow_template_id == "code-review-v1"
        assert task.current_step_key == "implement"
    finally:
        conn.close()


def test_cli_runs_verb(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="ok", summary="shipped")
    finally:
        conn.close()
    out = run_slash(f"runs {tid}")
    assert "completed" in out
    assert "shipped" in out
    assert "worker" in out


def test_cli_runs_json(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        kb.complete_task(
            conn, tid, result="ok", summary="shipped",
            metadata={"files": 1},
        )
    finally:
        conn.close()
    out = run_slash(f"runs {tid} --json")
    data = json.loads(out)
    assert len(data) == 1
    assert data[0]["outcome"] == "completed"
    assert data[0]["metadata"]["files"] == 1
    assert data[0]["metadata"]["cost"] == {"cost_status": "unknown"}


def test_cli_complete_with_summary_and_metadata(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    # JSON metadata must round-trip through shlex + argparse.
    meta = '{"files": 3}'
    out = run_slash(
        "complete " + tid + " --summary \"done it\" --metadata '" + meta + "'"
    )
    assert "Completed" in out
    conn = kb.connect()
    try:
        r = kb.latest_run(conn, tid)
    finally:
        conn.close()
    assert r.summary == "done it"
    assert r.metadata["files"] == 3
    assert r.metadata["cost"] == {"cost_status": "unknown"}


def test_cli_edit_backfills_result_on_done_task(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.complete_task(conn, tid)
    finally:
        conn.close()

    meta = '{"source": "dashboard-recovery"}'
    out = run_slash(
        "edit " + tid
        + " --result \"DECIDED: done\""
        + " --summary \"DECIDED: done\""
        + " --metadata '" + meta + "'"
    )

    assert "Edited" in out
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        events = kb.list_events(conn, tid)
    finally:
        conn.close()
    assert task.result == "DECIDED: done"
    assert run.summary == "DECIDED: done"
    assert run.metadata == {"source": "dashboard-recovery"}
    assert events[-1].kind == "edited"


def test_cli_edit_rejects_non_done_task(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
    finally:
        conn.close()

    out = run_slash(f"edit {tid} --result nope")

    assert "not done" in out


def test_cli_complete_bad_metadata_exits_nonzero(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    out = run_slash(f"complete {tid} --metadata not-json")
    assert "metadata" in out.lower()

