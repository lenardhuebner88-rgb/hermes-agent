from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


PROTOCOL_REPAIR_FALLBACK = (
    "Protocol repair: deliverable was posted but worker missed kanban_complete."
)

# Structure-preserving excerpt from a production deliverable comment: Markdown
# result heading, completion statement, concrete run evidence, and task terms.
LIVE_DELIVERABLE = """# Conflict-fixer COMPLETE: repeated on-device dictation + durable debug APK

**This completion closes the process loop.** The source fix and worktree cleanup were already verifier-APPROVED; the only open defect was that prior worker attempts posted the deliverable and exited `rc=0` **without calling `kanban_complete`** (attempts 3 & 6 = `deliverable_posted_not_completed`). This run calls complete.

## Final state (verified this run)
- **HEAD commit:** `01f8d2e7b16a8c1e6d340c10b3fa4592bee47434`
- **Worktree:** clean — `git status --porcelain` empty
- **APK:** `android/hermes-dictate/hermes-dictate-debug.apk`
"""


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kb.init_db()
    return home


def _detect_deliverable_protocol_miss(
    conn,
    *,
    pid: int,
    existing_result: str | None = None,
) -> str:
    task_id = kb.create_task(
        conn,
        title="repeated on-device dictation durable debug APK",
        assignee="premium",
    )
    assert kb.claim_task(conn, task_id) is not None
    if existing_result is not None:
        conn.execute(
            "UPDATE tasks SET result = ? WHERE id = ?",
            (existing_result, task_id),
        )
    kb.add_comment(conn, task_id, "premium", LIVE_DELIVERABLE)
    kb._set_worker_pid(conn, task_id, pid)
    kb._record_worker_exit(pid, 0)

    assert task_id not in kb.detect_crashed_workers(conn)
    assert kb.get_task(conn, task_id).status == "blocked"
    return task_id


def _latest_run_summary(conn, task_id: str) -> str | None:
    row = conn.execute(
        "SELECT summary FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row is not None
    return row["summary"]


def test_repair_preserves_deliverable_preview_in_task_and_synthetic_run(
    kanban_home,
):
    with kb.connect_closing() as conn:
        task_id = _detect_deliverable_protocol_miss(conn, pid=424250)

        assert kb.repair_deliverable_posted_not_completed(
            conn,
            task_id,
            actor="integrator",
        )

        repaired = kb.get_task(conn, task_id)
        run_summary = _latest_run_summary(conn, task_id)

    expected_preview = LIVE_DELIVERABLE[:400]
    assert repaired.result == expected_preview
    assert run_summary == expected_preview
    assert PROTOCOL_REPAIR_FALLBACK not in repaired.result
    assert PROTOCOL_REPAIR_FALLBACK not in run_summary


def test_repair_does_not_overwrite_an_existing_task_result(kanban_home):
    existing_result = "Canonical full result already recorded before repair."
    with kb.connect_closing() as conn:
        task_id = _detect_deliverable_protocol_miss(
            conn,
            pid=424251,
            existing_result=existing_result,
        )

        assert kb.repair_deliverable_posted_not_completed(conn, task_id)

        repaired = kb.get_task(conn, task_id)
        run_summary = _latest_run_summary(conn, task_id)

    assert repaired.result == existing_result
    assert run_summary == LIVE_DELIVERABLE[:400]


def test_repair_uses_protocol_fallback_when_evidence_preview_is_missing(
    kanban_home,
):
    with kb.connect_closing() as conn:
        task_id = _detect_deliverable_protocol_miss(conn, pid=424252)
        event = conn.execute(
            "SELECT id, payload FROM task_events "
            "WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (task_id, kb.DELIVERABLE_POSTED_NOT_COMPLETED),
        ).fetchone()
        payload = kb.json.loads(event["payload"])
        payload["evidence"].pop("preview")
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE id = ?",
            (kb.json.dumps(payload), event["id"]),
        )

        assert kb.repair_deliverable_posted_not_completed(conn, task_id)

        repaired = kb.get_task(conn, task_id)
        run_summary = _latest_run_summary(conn, task_id)

    assert repaired.result == PROTOCOL_REPAIR_FALLBACK
    assert run_summary == PROTOCOL_REPAIR_FALLBACK
