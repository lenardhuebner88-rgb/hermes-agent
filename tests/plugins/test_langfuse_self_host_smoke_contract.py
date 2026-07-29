from __future__ import annotations

import json
import sqlite3
import urllib.error
from email.message import Message
from pathlib import Path

from scripts.langfuse_worker_audit import build_live_smoke_contract


def _seed_databases(
    tmp_path: Path,
    *,
    include_first_token: bool = True,
    worker_runtime: str = "hermes",
) -> tuple[Path, Path]:
    usage_path = tmp_path / "usage.db"
    with sqlite3.connect(usage_path) as conn:
        conn.execute(
            "CREATE TABLE run_usage_facts (run_id TEXT PRIMARY KEY, task_run_id TEXT, "
            "task_id TEXT, captured_at TEXT)"
        )
        conn.execute(
            "INSERT INTO run_usage_facts VALUES ('usage-1', '42', 'task-1', "
            "'2026-07-29T20:00:00+00:00')"
        )

    kanban_path = tmp_path / "kanban.db"
    with sqlite3.connect(kanban_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT,
                worker_exit_code INTEGER, started_at INTEGER, ended_at INTEGER,
                metadata TEXT
            );
            CREATE TABLE worker_run_timeline_events (
                task_run_id INTEGER, event_kind TEXT, observed_at_ms INTEGER,
                source TEXT, task_id TEXT, board TEXT, chain_root_id TEXT, profile TEXT
            );
            CREATE TABLE worker_run_terminal_facts (
                task_run_id INTEGER, worker_exit_kind TEXT, worker_exit_code INTEGER,
                worker_protocol_state TEXT, task_outcome TEXT, end_reason TEXT,
                task_id TEXT, board TEXT
            );
            INSERT INTO worker_run_terminal_facts VALUES
                (42, 'exited', 0, 'complete', 'completed', 'completed', 'task-1', 'default');
            """
        )
        conn.execute(
            "INSERT INTO task_runs VALUES (42, 'task-1', 'completed', 0, 1, 2, ?)",
            (json.dumps({"worker_runtime": worker_runtime}),),
        )
        kinds = [
            "queued",
            "claimed",
            "spawn_started",
            "process_started",
            "first_llm_request",
            "ended",
        ]
        if include_first_token:
            kinds.insert(-1, "first_token")
        conn.executemany(
            "INSERT INTO worker_run_timeline_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (42, kind, index, "test", "task-1", "default", "task-1", "coder")
                for index, kind in enumerate(kinds, start=1)
            ],
        )
    return usage_path, kanban_path


def test_live_smoke_contract_passes_only_with_exact_trace_usage_and_lifecycle(tmp_path: Path) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [
            {"id": "trace-1234567890", "metadata": {"task_run_id": "42", "task_id": "task-1"}}
        ],
    )

    assert report["status"] == "pass"
    assert report["worker_runtime"] == "hermes"
    assert report["langfuse"] == {
        "credentials_configured": True,
        "tcp_http_reachable": True,
        "authenticated_public_api_readable": True,
        "exact_trace_link": True,
        "trace_id_short": "trace-12…7890",
    }
    assert report["usage_fact"]["exact_rows"] == 1
    assert report["lifecycle"]["missing_events"] == []
    assert report["terminal"]["worker_exit_code"] == 0


def test_live_smoke_contract_is_structurally_red_when_langfuse_is_unreachable(tmp_path: Path) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)

    def unreachable() -> list[dict[str, object]]:
        raise RuntimeError("secret-key-value must never be copied")

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=unreachable,
    )

    assert report["status"] == "fail"
    assert report["langfuse"] == {
        "credentials_configured": None,
        "tcp_http_reachable": False,
        "authenticated_public_api_readable": False,
        "exact_trace_link": False,
        "trace_id_short": None,
        "error_type": "RuntimeError",
    }
    assert "secret-key-value" not in json.dumps(report)


def test_live_smoke_contract_distinguishes_http_auth_failure(tmp_path: Path) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)

    def unauthorized() -> list[dict[str, object]]:
        cause = urllib.error.HTTPError("https://langfuse.invalid", 401, "secret", Message(), None)
        raise RuntimeError("secret-key-value must never be copied") from cause

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=unauthorized,
    )

    assert report["status"] == "fail"
    assert report["langfuse"] == {
        "credentials_configured": True,
        "tcp_http_reachable": True,
        "authenticated_public_api_readable": False,
        "exact_trace_link": False,
        "trace_id_short": None,
        "error_type": "HTTPError",
        "http_status": 401,
    }
    assert "secret" not in json.dumps(report)


def test_live_smoke_contract_distinguishes_url_failure(tmp_path: Path) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)

    def unreachable() -> list[dict[str, object]]:
        raise RuntimeError("secret-key-value must never be copied") from urllib.error.URLError(
            "secret-network-reason"
        )

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=unreachable,
    )

    assert report["status"] == "fail"
    assert report["langfuse"]["credentials_configured"] is True
    assert report["langfuse"]["tcp_http_reachable"] is False
    assert report["langfuse"]["authenticated_public_api_readable"] is False
    assert report["langfuse"]["error_type"] == "URLError"
    assert "secret-network-reason" not in json.dumps(report)


def test_live_smoke_contract_reports_missing_credentials(tmp_path: Path, monkeypatch) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)
    for name in (
        "HERMES_LANGFUSE_BASE_URL",
        "HERMES_LANGFUSE_HOST",
        "HERMES_LANGFUSE_PUBLIC_KEY",
        "HERMES_LANGFUSE_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
    )

    assert report["status"] == "fail"
    assert report["langfuse"]["credentials_configured"] is False
    assert report["langfuse"]["tcp_http_reachable"] is False
    assert report["langfuse"]["authenticated_public_api_readable"] is False
    assert report["langfuse"]["error_type"] == "LangfuseCredentialsMissing"


def test_live_smoke_contract_requires_first_token_after_model_request(tmp_path: Path) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path, include_first_token=False)

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [
            {"id": "trace-1", "metadata": {"task_run_id": "42", "task_id": "task-1"}}
        ],
    )

    assert report["status"] == "fail"
    assert report["lifecycle"]["missing_events"] == ["first_token"]
    assert report["lifecycle"]["first_token_required"] is True


def test_live_smoke_contract_does_not_require_first_token_before_model_request(
    tmp_path: Path,
) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path, include_first_token=False)
    with sqlite3.connect(kanban_path) as conn:
        conn.execute(
            "DELETE FROM worker_run_timeline_events WHERE event_kind = 'first_llm_request'"
        )

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [],
    )

    assert report["status"] == "fail"
    assert report["lifecycle"]["missing_events"] == ["first_llm_request"]
    assert report["lifecycle"]["first_token_required"] is False


def test_live_smoke_contract_marks_claude_cli_run_ineligible_not_lifecycle_defective(
    tmp_path: Path,
) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path, worker_runtime="claude-cli")
    with sqlite3.connect(kanban_path) as conn:
        conn.execute("DELETE FROM worker_run_timeline_events WHERE event_kind = 'first_llm_request'")
        conn.execute("DELETE FROM worker_run_timeline_events WHERE event_kind = 'first_token'")

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [],
    )

    assert report["status"] == "fail"
    assert report["worker_runtime"] == "claude-cli"
    assert report["runtime_eligible"] is False
    assert report["lifecycle"]["assessment"] == "not_applicable"
    assert report["lifecycle"]["missing_events"] == []
