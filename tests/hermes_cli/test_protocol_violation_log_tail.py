"""protocol_violation escalations must carry a bounded worker-log tail.

Live board: rc=0 protocol-violation crashes leave the operator with only the
fixed template string. The per-task worker log already sits on disk at
worker_logs_dir/{task_id}.log the whole time (spawn opens it at
kanban_db.py ~25460/25165); this plan lifts a bounded tail into the
protocol_violation event and into operator_escalation evidence.context.

Production path under test: detect_crashed_workers(conn) — same call the
dispatcher tick drives. Fixture/driver pattern mirrors
_drive_protocol_violation in test_kanban_core_recovery_failure.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

# Exact last_error string stamped by the protocol-violation branch
# (kanban_db.py:_PROTOCOL_VIOLATION_ERROR). Heiler classifies on this text.
PROTOCOL_VIOLATION_ERROR = (
    "worker exited cleanly (rc=0) without calling "
    "kanban_complete or kanban_block — protocol violation"
)

# Distinctive markers for bounding assertions. Production worker logs are
# multi-line CLI transcripts; shape mirrors real spawn output (timestamps +
# tool chatter), not a synthetic single word.
LOG_HEAD_MARKER = "=== WORKER START pid=4242 task=t_fixture ==="
LOG_TAIL_MARKER = "FINAL: model returned rc=0 without kanban_complete/block"


def _realistic_worker_log(*, size_target: int = 2500) -> str:
    """Multi-line log >2 KB with fixed head/tail markers (live-shaped)."""
    lines = [LOG_HEAD_MARKER]
    filler = (
        "[worker] tool_call terminal_exec cwd=/home/piet/.hermes "
        "cmd='rg --files' exit=0\n"
    )
    while sum(len(line) + 1 for line in lines) + len(filler) + len(LOG_TAIL_MARKER) < size_target:
        lines.append(filler.rstrip("\n") + f" n={len(lines)}")
    lines.append(LOG_TAIL_MARKER)
    text = "\n".join(lines) + "\n"
    assert len(text) > 2000, f"fixture must be >2KB, got {len(text)}"
    return text


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Crash-detection grace pre-dates these tests; pin to 0 so reclaim is
    # immediate (same as test_kanban_core_recovery_failure).
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _drive_protocol_violation(conn, tid: str, fake_pid: int):
    """One clean-exit (rc=0) protocol-violation reaper pass for ``tid``.

    Resolves hermes_cli.kanban_db fresh and uses that single module object
    for the exit registry, the liveness patch, AND the reaper — same
    pattern as test_kanban_core_recovery_failure._drive_worker_exit so a
    reloaded module can't turn a clean exit into an unknown crash.
    """
    import hermes_cli.kanban_db as _kb

    host_prefix = _kb._claimer_id().split(":", 1)[0]
    claimed = _kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock")
    assert claimed is not None, "task was not claimable for the next attempt"
    _kb._set_worker_pid(conn, tid, fake_pid)
    # os.W_EXITCODE(status=0, signal=0) == 0 on POSIX.
    _kb._record_worker_exit(fake_pid, 0)
    original_alive = _kb._pid_alive
    _kb._pid_alive = lambda p: False
    try:
        return _kb.detect_crashed_workers(conn)
    finally:
        _kb._pid_alive = original_alive


def _write_worker_log(task_id: str, content: str) -> Path:
    """Write at the exact spawn path: worker_logs_dir() / f'{task_id}.log'."""
    path = kb.worker_log_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_protocol_violation_event_carries_bounded_worker_log_tail(
    kanban_home: Path,
) -> None:
    """done_when (1) first half: protocol_violation event has bounded tail."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="quiet-exit", assignee="worker")
        log_path = _write_worker_log(tid, _realistic_worker_log())

        crashed = _drive_protocol_violation(conn, tid, 880001)
        assert tid in crashed

        events = kb.list_events(conn, tid)
        pv = [e for e in events if e.kind == "protocol_violation"]
        assert len(pv) == 1, f"expected one protocol_violation, got {[e.kind for e in events]}"
        payload = pv[0].payload or {}

        assert "worker_log_tail" in payload, (
            "protocol_violation payload must include worker_log_tail from "
            f"the on-disk log at {log_path}"
        )
        tail = payload["worker_log_tail"]
        assert isinstance(tail, str) and tail, "worker_log_tail must be a non-empty str"
        assert LOG_TAIL_MARKER in tail, (
            f"tail must include the END of the log ({LOG_TAIL_MARKER!r}); got {tail[:200]!r}…"
        )
        assert LOG_HEAD_MARKER not in tail, (
            "tail must be bounded (~1500 chars) and NOT include the log head "
            f"({LOG_HEAD_MARKER!r})"
        )
        assert len(tail) <= 1600, (
            f"tail must stay hard-capped (~1500 chars), got len={len(tail)}"
        )
        assert payload.get("worker_log_path") == str(log_path)
        # last_error / failure stamp remains the fixed template string.
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.last_failure_error == PROTOCOL_VIOLATION_ERROR
    finally:
        conn.close()


def test_protocol_violation_escalation_carries_worker_log_tail_in_context(
    kanban_home: Path,
) -> None:
    """done_when (1) second half: after streak-trip, escalation evidence has tail.

    Production: detect_crashed_workers → streak >= limit →
    _record_task_failure(..., end_run=False, event_payload_extra=...) →
    _operator_escalation_payload puts event_payload_extra into evidence.context.
    """
    import hermes_cli.kanban_db as _kb

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="repeat-quiet", assignee="worker")
        log_path = _write_worker_log(tid, _realistic_worker_log())
        limit = _kb._PROTOCOL_VIOLATION_FAILURE_LIMIT

        for i in range(limit - 1):
            _drive_protocol_violation(conn, tid, 881000 + i)
            assert kb.get_task(conn, tid).status == "ready"

        # Refresh log just before the trip so the final reap reads current content.
        _write_worker_log(tid, _realistic_worker_log())
        _drive_protocol_violation(conn, tid, 881900)

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert task.last_failure_error == PROTOCOL_VIOLATION_ERROR

        events = kb.list_events(conn, tid)
        esc = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
        assert len(esc) == 1, f"expected one operator_escalation, got {[e.kind for e in events]}"
        payload = esc[0].payload or {}
        evidence = payload.get("evidence") or {}
        assert evidence.get("last_error") == PROTOCOL_VIOLATION_ERROR, (
            "evidence.last_error must stay EXACTLY the fixed "
            f"_PROTOCOL_VIOLATION_ERROR string, got {evidence.get('last_error')!r}"
        )
        context = evidence.get("context") or {}
        assert "worker_log_tail" in context, (
            "operator_escalation evidence.context must include worker_log_tail "
            f"(got context keys={sorted(context)!r})"
        )
        tail = context["worker_log_tail"]
        assert LOG_TAIL_MARKER in tail
        assert LOG_HEAD_MARKER not in tail
        assert context.get("worker_log_path") == str(log_path)
    finally:
        conn.close()


def test_protocol_violation_missing_log_is_fail_soft(kanban_home: Path) -> None:
    """done_when (2): missing/empty log must not raise; fields absent or empty."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="no-log", assignee="worker")
        # No worker log written — path does not exist.
        assert not kb.worker_log_path(tid).exists()

        crashed = _drive_protocol_violation(conn, tid, 882001)
        assert tid in crashed

        events = kb.list_events(conn, tid)
        pv = [e for e in events if e.kind == "protocol_violation"]
        assert len(pv) == 1
        payload = pv[0].payload or {}
        tail = payload.get("worker_log_tail")
        assert tail in (None, ""), (
            f"missing log must omit or empty worker_log_tail, got {tail!r}"
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"
        assert task.last_failure_error == PROTOCOL_VIOLATION_ERROR
    finally:
        conn.close()


def test_protocol_violation_empty_log_is_fail_soft(kanban_home: Path) -> None:
    """done_when (2): empty log file is also fail-soft."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="empty-log", assignee="worker")
        _write_worker_log(tid, "")

        crashed = _drive_protocol_violation(conn, tid, 883001)
        assert tid in crashed

        events = kb.list_events(conn, tid)
        pv = [e for e in events if e.kind == "protocol_violation"]
        assert len(pv) == 1
        payload = pv[0].payload or {}
        tail = payload.get("worker_log_tail")
        assert tail in (None, ""), (
            f"empty log must omit or empty worker_log_tail, got {tail!r}"
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.last_failure_error == PROTOCOL_VIOLATION_ERROR
    finally:
        conn.close()
