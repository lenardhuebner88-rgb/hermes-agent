"""Subprocess tests for `hermes kanban report` exit-code propagation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_report as kr


_WORKTREE = Path(__file__).resolve().parents[2]


def _report_metadata() -> dict:
    return {
        "report_contract_version": kr.REPORT_CONTRACT_VERSION,
        "verification_evidence": ["scripts/run_tests.sh tests/cli/test_kanban_report_exit_codes.py"],
        "receipt_reference": "vault/03-Agents/Hermes/receipts/demo.md",
        "scope_contract_read": True,
        "scope_contract_version": 2,
        "scope_attestation": True,
        "forbidden_actions_taken": 0,
    }


def _run_report(task_id: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_WORKTREE)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "report", task_id, "--json"],
        cwd=str(_WORKTREE),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_kanban_report_clean_exits_0():
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="clean report", assignee="alice")
        assert kb.complete_task(
            conn,
            tid,
            summary="complete with full report contract",
            metadata=_report_metadata(),
        )

    result = _run_report(tid)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["quality"]["ok"] is True


def test_kanban_report_legacy_completion_exits_0_with_quality_warning():
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="legacy report", assignee="alice")
        assert kb.complete_task(conn, tid, summary="legacy handoff", metadata={})

    result = _run_report(tid)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["quality"]["ok"] is False
    assert "report_contract_version" in payload["quality"]["missing"]


def test_kanban_report_missing_task_exits_2():
    result = _run_report("t_missing")

    assert result.returncode == 2
    assert "no such task: t_missing" in result.stderr
