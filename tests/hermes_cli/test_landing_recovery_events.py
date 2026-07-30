"""Landing-recovery event ledger and privileged same-card revision tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _request(conn, task_id: str, fingerprint: str = "fp-gate-a") -> bool:
    return kb.request_landing_recovery(
        conn,
        task_id,
        fingerprint=fingerprint,
        candidate_commit="abc123",
        failure_class="gate_failed",
        failing_gate="tests/hermes_cli/test_target.py",
        blocking_findings=["targeted gate failed"],
        required_verification=["rerun targeted gate"],
    )


def _events(conn, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def test_request_landing_recovery_deduplicates_open_fingerprint(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="landing candidate", assignee="coder")

        assert {
            kb.LANDING_RECOVERY_REQUESTED_EVENT,
            kb.LANDING_RECOVERY_STARTED_EVENT,
            kb.LANDING_RECOVERY_EXHAUSTED_EVENT,
        }.isdisjoint(kb.VALID_WAIT_EVENT_KINDS)

        assert _request(conn, task_id)
        assert not _request(conn, task_id)

        requested = _events(conn, task_id, kb.LANDING_RECOVERY_REQUESTED_EVENT)
        assert len(requested) == 1
        assert requested[0] == {
            "blocking_findings": ["targeted gate failed"],
            "candidate_commit": "abc123",
            "failing_gate": "tests/hermes_cli/test_target.py",
            "failure_class": "gate_failed",
            "fingerprint": "fp-gate-a",
            "required_verification": ["rerun targeted gate"],
            "source": "landing_loop",
        }


def test_landing_recovery_event_payloads_fail_closed(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="landing candidate", assignee="coder")

        with pytest.raises(ValueError, match="fingerprint"):
            kb.request_landing_recovery(
                conn,
                task_id,
                fingerprint="",
                candidate_commit="abc123",
                failure_class="gate_failed",
                failing_gate="pytest",
                blocking_findings=["failed"],
                required_verification=["rerun"],
            )

        assert not _events(conn, task_id, kb.LANDING_RECOVERY_REQUESTED_EVENT)


def test_start_landing_recovery_revision_is_one_shot_and_preserves_task(kanban_home):
    with kb.connect() as conn:
        workspace = kanban_home / "candidate-worktree"
        task_id = kb.create_task(
            conn,
            title="landing candidate",
            assignee="coder",
            workspace_kind="worktree",
            workspace_path=str(workspace),
        )
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        assert _request(conn, task_id)
        before = kb.get_task(conn, task_id)

        assert kb.start_landing_recovery_revision(
            conn, task_id, fingerprint="fp-gate-a", source="gateway"
        )

        blocked = kb.get_task(conn, task_id)
        assert blocked.status == "blocked"
        assert blocked.block_kind == "review_revision"
        assert blocked.assignee == before.assignee
        assert blocked.workspace_kind == before.workspace_kind
        assert blocked.workspace_path == before.workspace_path
        assert not kb.start_landing_recovery_revision(
            conn, task_id, fingerprint="fp-gate-a", source="gateway"
        )

        started = _events(conn, task_id, kb.LANDING_RECOVERY_STARTED_EVENT)
        assert len(started) == 1
        assert started[0]["fingerprint"] == "fp-gate-a"
        assert started[0]["source"] == "gateway"

        blocked_payload = _events(conn, task_id, "blocked")[-1]
        assert blocked_payload["kind"] == "review_revision"
        assert blocked_payload["review_revision"] == {
            "blocking_findings": ["targeted gate failed"],
            "required_verification": ["rerun targeted gate"],
            "resume_stage": 0,
            "review_tier": "standard",
            "reviewed_commit": "abc123",
            "source": "landing_recovery",
        }

        assert kb.auto_retry_blocked_tasks(conn, backoff_seconds=0) == [(task_id, 1)]
        ready = kb.get_task(conn, task_id)
        assert ready.status == "ready"
        assert ready.assignee == before.assignee
        assert ready.workspace_kind == before.workspace_kind
        assert ready.workspace_path == before.workspace_path


def test_started_fingerprint_exhausts_but_changed_fingerprint_is_allowed(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="landing candidate", assignee="coder")
        assert _request(conn, task_id, "fp-gate-a")
        assert kb.start_landing_recovery_revision(conn, task_id, fingerprint="fp-gate-a")

        assert not _request(conn, task_id, "fp-gate-a")
        exhausted = _events(conn, task_id, kb.LANDING_RECOVERY_EXHAUSTED_EVENT)
        assert len(exhausted) == 1
        assert exhausted[0]["fingerprint"] == "fp-gate-a"
        assert exhausted[0]["held"] is True
        assert exhausted[0]["escalated"] is True

        assert _request(conn, task_id, "fp-gate-b")
        assert len(_events(conn, task_id, kb.LANDING_RECOVERY_REQUESTED_EVENT)) == 2
