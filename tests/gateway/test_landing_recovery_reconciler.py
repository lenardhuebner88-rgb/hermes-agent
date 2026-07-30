"""Gateway landing-recovery reconciler tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.kanban_watchers import reconcile_landing_recoveries
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
        failure_class="candidate_regression",
        failing_gate="tests/gateway/test_target.py",
        blocking_findings=["targeted gate failed"],
        required_verification=["rerun targeted gate"],
    )


def _event_payloads(conn, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def test_reconciler_is_fail_closed_unless_manual_override(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="landing candidate", assignee="coder")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        assert _request(conn, task_id)

        disabled = reconcile_landing_recoveries(
            conn, automation_enabled=False, limit=10
        )
        assert disabled.enabled is False
        assert disabled.scanned == 0
        assert disabled.started == ()
        assert kb.get_task(conn, task_id).status == "done"
        assert not _event_payloads(conn, task_id, kb.LANDING_RECOVERY_STARTED_EVENT)

        forced = reconcile_landing_recoveries(
            conn,
            automation_enabled=False,
            manual_override=True,
            limit=10,
        )
        assert forced.enabled is True
        assert forced.started == ((task_id, "fp-gate-a"),)
        assert kb.get_task(conn, task_id).block_kind == "review_revision"


def test_reconciler_starts_then_existing_claim_path_keeps_original_assignee(
    kanban_home,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="landing candidate", assignee="coder")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        assert _request(conn, task_id)

        result = reconcile_landing_recoveries(
            conn, automation_enabled=True, limit=10
        )
        assert result.started == ((task_id, "fp-gate-a"),)
        assert kb.auto_retry_blocked_tasks(conn, backoff_seconds=0) == [(task_id, 1)]

        claimed = kb.claim_task(conn, task_id, claimer="gateway-test")
        assert claimed is not None
        assert claimed.status == "running"
        assert claimed.assignee == "coder"

        repeated = reconcile_landing_recoveries(
            conn, automation_enabled=True, limit=10
        )
        assert repeated.started == ()
        assert len(
            _event_payloads(conn, task_id, kb.LANDING_RECOVERY_STARTED_EVENT)
        ) == 1

        assert not _request(conn, task_id)
        exhausted = _event_payloads(
            conn, task_id, kb.LANDING_RECOVERY_EXHAUSTED_EVENT
        )
        assert len(exhausted) == 1
        assert exhausted[0]["fingerprint"] == "fp-gate-a"


def test_reconciler_records_malformed_request_without_mutating_task(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="landing candidate", assignee="coder")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        before = kb.get_task(conn, task_id)
        kb.add_event(
            conn,
            task_id,
            kb.LANDING_RECOVERY_REQUESTED_EVENT,
            {"fingerprint": "fp-invalid"},
        )

        result = reconcile_landing_recoveries(
            conn, automation_enabled=True, limit=10
        )
        assert result.started == ()
        assert result.failed == ((task_id, "fp-invalid", "unclear"),)
        assert kb.get_task(conn, task_id) == before

        failed = _event_payloads(
            conn, task_id, "landing_recovery_reconcile_failed"
        )
        assert failed == [
            {
                "error_type": "invalid_requested_payload",
                "failure_class": "unclear",
                "fingerprint": "fp-invalid",
                "requested_event_id": result.failed_request_ids[0],
                "source": "gateway_reconciler",
            }
        ]

        repeated = reconcile_landing_recoveries(
            conn, automation_enabled=True, limit=10
        )
        assert repeated.scanned == 0
        assert len(
            _event_payloads(conn, task_id, "landing_recovery_reconcile_failed")
        ) == 1


def test_reconciler_records_infra_failure_without_mutating_task(
    kanban_home, monkeypatch
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="landing candidate", assignee="coder")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        assert _request(conn, task_id)
        before = kb.get_task(conn, task_id)

        def fail_start(*_args, **_kwargs):
            raise RuntimeError("sensitive backend detail must not enter the ledger")

        monkeypatch.setattr(kb, "start_landing_recovery_revision", fail_start)
        result = reconcile_landing_recoveries(
            conn, automation_enabled=True, limit=10
        )

        assert result.failed == ((task_id, "fp-gate-a", "infra"),)
        assert kb.get_task(conn, task_id) == before
        [failed] = _event_payloads(
            conn, task_id, "landing_recovery_reconcile_failed"
        )
        assert failed["failure_class"] == "infra"
        assert failed["error_type"] == "RuntimeError"
        assert "sensitive" not in json.dumps(failed)
