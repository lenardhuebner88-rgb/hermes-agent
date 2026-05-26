"""Tests for the read-only Kanban report contract helpers."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_report as kr


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _full_report_metadata() -> dict:
    return {
        "report_contract_version": kr.REPORT_CONTRACT_VERSION,
        "verification_evidence": ["scripts/run_tests.sh tests/hermes_cli/test_kanban_report.py"],
        "receipt_reference": "vault/03-Agents/Hermes/receipts/demo.md",
        "scope_contract_read": True,
        "scope_contract_version": 2,
        "scope_attestation": True,
        "forbidden_actions_taken": 0,
        "effective_toolsets": ["kanban"],
    }


def test_latest_report_for_task_normalizes_full_contract(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="reportable", assignee="alice")
        assert kb.complete_task(
            conn,
            tid,
            summary="done with evidence",
            metadata=_full_report_metadata(),
        )

        report = kr.latest_report_for_task(conn, tid)

    assert report is not None
    assert report["contract"]["version"] == kr.REPORT_CONTRACT_VERSION
    assert report["quality"]["missing"] == []
    assert report["quality"]["inconsistencies"] == []
    assert report["quality"]["ok"] is True
    assert report["task"]["id"] == tid
    assert report["run"]["outcome"] == "completed"


def test_report_quality_lists_missing_contract_fields(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="alice")
        assert kb.complete_task(conn, tid, summary="legacy handoff", metadata={})

        report = kr.latest_report_for_task(conn, tid)

    assert report is not None
    assert report["quality"]["ok"] is False
    assert report["quality"]["missing"] == [
        "report_contract_version",
        "verification_evidence",
        "receipt_reference",
        "scope_attestation",
    ]


def test_reports_for_task_is_read_only_and_filters_to_completed_runs(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="running", assignee="alice")
        kb.claim_task(conn, tid)

        reports = kr.reports_for_task(conn, tid)

    assert reports == []


def test_reports_for_task_includes_done_status(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="status filter", assignee="alice")
        conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, status, started_at, ended_at,
                outcome, summary, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tid, "alice", "done", 10, 20, None, "done status", json.dumps({})),
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, status, started_at, ended_at,
                outcome, summary, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tid, "alice", "completed", 30, 40, None, "completed status", json.dumps({})),
        )
        conn.commit()

        reports = kr.reports_for_task(conn, tid)

    assert [report["run"]["status"] for report in reports] == ["done", "completed"]


def test_reports_for_task_excludes_running(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="exclude running", assignee="alice")
        conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, status, started_at, ended_at,
                outcome, summary, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tid, "alice", "running", 10, None, None, "running", json.dumps({})),
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, status, started_at, ended_at,
                outcome, summary, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tid, "alice", "done", 20, 30, None, "done", json.dumps({})),
        )
        conn.commit()

        reports = kr.reports_for_task(conn, tid)

    assert [report["run"]["status"] for report in reports] == ["done"]


def test_report_is_missing_accepts_contract_alias_paths(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="legacy alias", assignee="alice")
        assert kb.complete_task(conn, tid, summary="legacy", metadata={})

        report = kr.latest_report_for_task(conn, tid)

    assert report is not None
    assert kr.report_is_missing(report, "evidence.tests") is True
    assert kr.report_is_missing(report, "evidence.receipt_path") is True
    assert kr.report_is_missing(report, "scope.scope_attestation") is True
    assert kr.report_is_missing(report, "report_contract_version") is True


def test_reports_for_fleet_filters_since_cutoff(kanban_home):
    now = int(time.time())
    with kb.connect() as conn:
        old_tid = kb.create_task(conn, title="old report", assignee="alice")
        new_tid = kb.create_task(conn, title="new report", assignee="alice")
        conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, status, started_at, ended_at,
                outcome, summary, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                old_tid,
                "alice",
                "done",
                now - 100000,
                now - 90000,
                "completed",
                "old",
                json.dumps({}),
            ),
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, status, started_at, ended_at,
                outcome, summary, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_tid,
                "alice",
                "done",
                now - 120,
                now - 60,
                "completed",
                "new",
                json.dumps({}),
            ),
        )
        conn.commit()

        reports = kr.reports_for_fleet(conn, since=now - 3600)

    assert [report["task"]["id"] for report in reports] == [new_tid]


def test_reports_for_fleet_filters_missing_alias_path(kanban_home):
    with kb.connect() as conn:
        complete_tid = kb.create_task(conn, title="complete report", assignee="alice")
        missing_tid = kb.create_task(conn, title="missing report", assignee="alice")
        assert kb.complete_task(
            conn,
            complete_tid,
            summary="complete",
            metadata=_full_report_metadata(),
        )
        assert kb.complete_task(conn, missing_tid, summary="missing", metadata={})

        reports = kr.reports_for_fleet(conn, missing=["evidence.tests"])

    assert [report["task"]["id"] for report in reports] == [missing_tid]
