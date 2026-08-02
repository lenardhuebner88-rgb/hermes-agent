from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import hermes_cli.profiles as profiles_mod
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_diagnostics import compute_task_diagnostics
from hermes_cli.kanban_review_authority import (
    NEGATIVE_VERDICT_WITHOUT_AUTHORITY_DIAGNOSTIC_KIND,
    NEGATIVE_VERDICT_WITHOUT_AUTHORITY_EVENT_KIND,
    REVIEW_AUTHORITY_REASON_NOT_REVIEW_CLAIM,
    record_negative_verdict_without_authority,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _authority_events(conn, task_id):
    return [
        event
        for event in kb.list_events(conn, task_id)
        if event.kind == NEGATIVE_VERDICT_WITHOUT_AUTHORITY_EVENT_KIND
    ]


@pytest.mark.parametrize("kind", ["analysis", "code"])
def test_negative_explicit_review_verdict_without_authority_records_event_only(
    kanban_home, kind
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="ordinary worker completion",
            assignee="researcher",
            kind=kind,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        run_id = claimed.current_run_id
        assert _authority_events(conn, task_id) == []

        assert kb.complete_task(
            conn,
            task_id,
            summary="work complete",
            metadata={
                "reviewer_verdict": "needs revision",
                "blocking_findings": ["first", "second"],
            },
            expected_run_id=run_id,
        )

        events = _authority_events(conn, task_id)
        assert len(events) == 1
        assert events[0].payload == {
            "normalized_verdict": "REQUEST_CHANGES",
            "raw_verdict": "needs revision",
            "authority_reason": REVIEW_AUTHORITY_REASON_NOT_REVIEW_CLAIM,
            "blocking_findings_count": 2,
        }
        run = conn.execute(
            "SELECT verdict FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert run is not None and run["verdict"] is None
        assert kb.get_task(conn, task_id).status != "blocked"
        assert not any(
            event.kind == "needs_revision_fix_task"
            for event in kb.list_events(conn, task_id)
        )


@pytest.mark.parametrize(
    ("metadata", "outcome", "originated_from_review"),
    [
        ({"review_verdict": "APPROVED"}, "completed", False),
        ({}, "completed", False),
        ({"decision": "REQUEST_CHANGES"}, "completed", False),
        ({"verdict": "REJECTED"}, "completed", False),
        ({"verdict_status": "FAILED"}, "completed", False),
        ({"review_verdict": "REQUEST_CHANGES"}, "failed", False),
        ({"review_verdict": "REQUEST_CHANGES"}, "completed", True),
    ],
)
def test_negative_authority_event_predicate_is_narrow(
    metadata, outcome, originated_from_review
):
    appended = []

    record_negative_verdict_without_authority(
        None,
        task_id="t_demo",
        run_id=7,
        outcome=outcome,
        metadata=metadata,
        originated_from_review=originated_from_review,
        normalize_verdict=kb._normalize_review_verdict,
        append_event=lambda *args, **kwargs: appended.append((args, kwargs)),
    )

    assert appended == []


def test_real_review_request_changes_keeps_terminal_authority(kanban_home, monkeypatch):
    monkeypatch.setattr(
        kb,
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
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="implementation", assignee="coder")
        assert kb.claim_task(conn, task_id)
        assert kb.complete_task(
            conn, task_id, summary="implementation done", review_gate=True
        )
        claimed = kb.claim_review_task(conn, task_id, reviewer_profile="verifier")
        assert claimed is not None

        assert kb.complete_task(
            conn,
            task_id,
            summary="needs fixes",
            metadata={"review_verdict": "REQUEST_CHANGES"},
            review_gate=True,
        )

        assert kb.get_task(conn, task_id).status == "blocked"
        run = conn.execute(
            "SELECT verdict FROM task_runs WHERE id = ?",
            (claimed.current_run_id,),
        ).fetchone()
        assert run is not None and run["verdict"] == "REQUEST_CHANGES"
        assert _authority_events(conn, task_id) == []


def test_diagnostic_is_warning_deduped_to_latest_trigger_and_archival_clears():
    task = {
        "id": "t_demo",
        "title": "demo",
        "assignee": "researcher",
        "status": "done",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    payload = {
        "normalized_verdict": "REQUEST_CHANGES",
        "raw_verdict": "REJECTED",
        "authority_reason": REVIEW_AUTHORITY_REASON_NOT_REVIEW_CLAIM,
        "blocking_findings_count": 3,
    }
    events = [
        {
            "id": 11,
            "task_id": "t_demo",
            "kind": NEGATIVE_VERDICT_WITHOUT_AUTHORITY_EVENT_KIND,
            "payload": payload,
            "run_id": 4,
            "created_at": 100,
        },
        {
            "id": 12,
            "task_id": "t_demo",
            "kind": NEGATIVE_VERDICT_WITHOUT_AUTHORITY_EVENT_KIND,
            "payload": payload,
            "run_id": 5,
            "created_at": 200,
        },
    ]

    diagnostics = [
        diagnostic
        for diagnostic in compute_task_diagnostics(task, events, [], now=300)
        if diagnostic.kind == NEGATIVE_VERDICT_WITHOUT_AUTHORITY_DIAGNOSTIC_KIND
    ]
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.severity == "warning"
    assert diagnostic.run_id == 5
    assert diagnostic.data == {
        "task_id": "t_demo",
        "event_id": 12,
        "verdict": "REQUEST_CHANGES",
        "blocking_findings_count": 3,
    }

    task["status"] = "archived"
    assert not any(
        diagnostic.kind == NEGATIVE_VERDICT_WITHOUT_AUTHORITY_DIAGNOSTIC_KIND
        for diagnostic in compute_task_diagnostics(task, events, [], now=300)
    )


def test_negative_review_diagnostic_supports_bulk_sqlite_rows(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="row diagnostic", assignee="researcher")
        payload = {
            "normalized_verdict": "REQUEST_CHANGES",
            "raw_verdict": "needs revision",
            "authority_reason": REVIEW_AUTHORITY_REASON_NOT_REVIEW_CLAIM,
            "blocking_findings_count": 2,
        }
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                NEGATIVE_VERDICT_WITHOUT_AUTHORITY_EVENT_KIND,
                payload,
                run_id=41,
            )
            latest_event_id = kb._append_event(
                conn,
                task_id,
                NEGATIVE_VERDICT_WITHOUT_AUTHORITY_EVENT_KIND,
                {**payload, "blocking_findings_count": 3},
                run_id=42,
            )

        task_row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        event_rows = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        assert isinstance(task_row, sqlite3.Row)
        assert all(isinstance(event, sqlite3.Row) for event in event_rows)
        assert isinstance(event_rows[-1]["payload"], str)

        diagnostics = [
            diagnostic
            for diagnostic in compute_task_diagnostics(
                task_row,
                event_rows,
                [],
                now=int(event_rows[-1]["created_at"]),
            )
            if diagnostic.kind == NEGATIVE_VERDICT_WITHOUT_AUTHORITY_DIAGNOSTIC_KIND
        ]
        assert len(diagnostics) == 1
        assert diagnostics[0].severity == "warning"
        assert diagnostics[0].data["event_id"] == latest_event_id
        assert diagnostics[0].data["blocking_findings_count"] == 3

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'archived' WHERE id = ?", (task_id,)
            )
        archived_task_row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert not any(
            diagnostic.kind == NEGATIVE_VERDICT_WITHOUT_AUTHORITY_DIAGNOSTIC_KIND
            for diagnostic in compute_task_diagnostics(
                archived_task_row,
                event_rows,
                [],
                now=int(event_rows[-1]["created_at"]),
            )
        )
