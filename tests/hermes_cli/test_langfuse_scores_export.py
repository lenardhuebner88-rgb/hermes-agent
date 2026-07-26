from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import hermes_cli.langfuse_scores_export as langfuse_export
from hermes_cli.langfuse_scores_export import export_scores, synthesize_traces, trace_id_for_run


class _FakeLangfuseHandler(BaseHTTPRequestHandler):
    traces = [
        {"id": "trace-run", "metadata": {"kanban_run_id": 7}},
        {"id": "trace-task", "metadata": json.dumps({"kanban_task_id": "t-task"})},
    ]
    scores: list[dict] = []
    remote_scores: list[dict] = []
    ingestions: list[dict] = []
    fail_ingestion = 0

    def do_GET(self) -> None:  # noqa: N802
        data = self.remote_scores if self.path.startswith("/api/public/scores") else self.traces
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"data": data, "meta": {"totalPages": 1}}).encode())

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        if self.path == "/api/public/ingestion":
            if self.fail_ingestion:
                type(self).fail_ingestion -= 1
                self.send_response(503)
                self.end_headers()
                return
            self.ingestions.append(payload)
        else:
            self.scores.append(payload)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE scores (id INTEGER PRIMARY KEY, run_id INTEGER, task_id TEXT NOT NULL,
          name TEXT NOT NULL, value REAL, value_type TEXT NOT NULL, source TEXT, created_at INTEGER);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT,
          active_model TEXT, outcome TEXT);
        INSERT INTO task_runs VALUES (7, 't-task', 'coder', 'model-x', 'completed');
        INSERT INTO scores VALUES (123, 7, 't-task', 'review_verdict', 1.0, 'binary', 'review_gate', 1);
        INSERT INTO scores VALUES (124, 7, 't-task', 'run_outcome_kind', 1.0, 'numeric', 'finalizer', 1);
        INSERT INTO scores VALUES (125, NULL, 't-task', 'review_iterations_to_approval', 2.0, 'numeric', 'review_gate', 1);
        INSERT INTO scores VALUES (126, NULL, 't-missing', 'review_verdict', 0.0, 'binary', 'review_gate', 1);
    """)
    conn.close()


def _make_empty_db(path: Path) -> None:
    """Database with scores but none matching any trace (all unmatched)."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE scores (id INTEGER PRIMARY KEY, run_id INTEGER, task_id TEXT NOT NULL,
          name TEXT NOT NULL, value REAL, value_type TEXT NOT NULL, source TEXT, created_at INTEGER);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT,
          active_model TEXT, outcome TEXT);
        INSERT INTO scores VALUES (200, NULL, 't-unmatched', 'review_verdict', 1.0, 'binary', 'review_gate', 1);
        INSERT INTO scores VALUES (201, NULL, 't-also-unmatched', 'run_cost_usd', 0.5, 'numeric', 'finalizer', 1);
    """)
    conn.close()


def _make_historical_score_db(path: Path, score_count: int) -> None:
    """Generate a large logical backfill without a large checked-in fixture."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE scores (id INTEGER PRIMARY KEY, run_id INTEGER, task_id TEXT NOT NULL,
          name TEXT NOT NULL, value REAL, value_type TEXT NOT NULL, source TEXT, created_at INTEGER);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT,
          active_model TEXT, outcome TEXT);
    """)
    conn.executemany(
        "INSERT INTO scores VALUES (?, 999, 't-large-historical', 'run_cost_usd', 0.25, "
        "'numeric', 'backfill-test', ?)",
        [(score_id, score_id) for score_id in range(1, score_count + 1)],
    )
    conn.commit()
    conn.close()


def _start_server() -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLangfuseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _env(server: ThreadingHTTPServer) -> dict[str, str]:
    return {
        "HERMES_LANGFUSE_BASE_URL": f"http://127.0.0.1:{server.server_port}",
        "HERMES_LANGFUSE_PUBLIC_KEY": "pk-test",
        "HERMES_LANGFUSE_SECRET_KEY": "sk-test",
    }


def test_export_posts_shaped_scores_idempotently_and_skips_unmatched(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _make_db(db_path)
    _FakeLangfuseHandler.scores = []
    server, thread = _start_server()
    try:
        env = _env(server)
        result = export_scores(db_path=db_path, env=env)
        again = export_scores(db_path=db_path, env=env)
    finally:
        server.shutdown()
        thread.join()

    assert result == {"matched": 3, "unmatched": 1, "posted": 3,
                      "names": {"review_verdict": 2, "run_outcome_kind": 1,
                                "review_iterations_to_approval": 1}}
    assert again["posted"] == 3
    assert len(_FakeLangfuseHandler.scores) == 6
    verdict = _FakeLangfuseHandler.scores[0]
    assert verdict == {"id": "hermes-board-score-123", "traceId": "trace-run",
                       "name": "review_verdict", "value": "APPROVED",
                       "dataType": "CATEGORICAL"}
    outcome = _FakeLangfuseHandler.scores[1]
    assert outcome["value"] == "completed"
    assert outcome["dataType"] == "CATEGORICAL"
    assert _FakeLangfuseHandler.scores[2]["dataType"] == "NUMERIC"
    assert _FakeLangfuseHandler.scores[2]["traceId"] == "trace-task"


def test_export_dry_run_does_not_write(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _make_db(db_path)
    _FakeLangfuseHandler.scores = []
    server, thread = _start_server()
    try:
        env = _env(server)
        result = export_scores(db_path=db_path, env=env, dry_run=True)
    finally:
        server.shutdown()
        thread.join()
    assert result["matched"] == 3
    assert result["unmatched"] == 1
    assert result["posted"] == 0
    assert _FakeLangfuseHandler.scores == []


def test_backfill_seeds_ledger_batches_ingestion_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    ledger_path = tmp_path / "ledger.json"
    _make_db(db_path)
    _FakeLangfuseHandler.remote_scores = [{"id": "hermes-board-score-123"}]
    _FakeLangfuseHandler.ingestions = []
    server, thread = _start_server()
    try:
        env = _env(server)
        result = export_scores(db_path=db_path, env=env, backfill=True,
                               ledger_path=ledger_path, batch_size=2)
        again = export_scores(db_path=db_path, env=env, backfill=True,
                              ledger_path=ledger_path, batch_size=2)
    finally:
        server.shutdown()
        thread.join()
    assert result["total_rows"] == 4
    assert result["matchable"] == 4
    assert result["matched"] == 4
    assert result["unmatched"] == 0
    assert result["posted_new"] == 3
    assert result["skipped_seen"] == 1
    assert result["anchor_count"] == 1
    assert result["anchored_rows"] == 1
    assert result["retention"]["checked"] is True
    assert result["retention"]["active"] is False
    assert again["posted_new"] == 0
    assert again["skipped_seen"] == 4
    assert again["anchor_count"] == 0
    events = [
        event for request in _FakeLangfuseHandler.ingestions for event in request["batch"]
    ]
    assert all(len(request["batch"]) <= 2 for request in _FakeLangfuseHandler.ingestions)
    assert {event["body"]["id"] for event in events if event["type"] == "score-create"} == {
        "hermes-board-score-124", "hermes-board-score-125", "hermes-board-score-126",
    }
    assert sum(event["type"] == "trace-create" for event in events) == 1
    assert json.loads(ledger_path.read_text())["exported_score_ids"] == [
        "hermes-board-score-123", "hermes-board-score-124", "hermes-board-score-125",
        "hermes-board-score-126",
    ]


def test_backfill_anchors_historical_scores_without_live_trace(tmp_path: Path) -> None:
    """Historical score rows get one deterministic task anchor before ingestion."""
    db_path = tmp_path / "kanban.db"
    ledger_path = tmp_path / "ledger.json"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE scores (id INTEGER PRIMARY KEY, run_id INTEGER, task_id TEXT NOT NULL,
          name TEXT NOT NULL, value REAL, value_type TEXT NOT NULL, source TEXT, created_at INTEGER);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT,
          active_model TEXT, outcome TEXT);
        INSERT INTO task_runs VALUES (7, 't-live', 'coder', 'model-x', 'completed');
        INSERT INTO scores VALUES
          (1, 7, 't-live', 'review_verdict', 1.0, 'binary', 'review_gate', 1),
          (2, NULL, 't-live', 'review_iterations_to_approval', 2.0, 'numeric', 'review_gate', 2),
          (3, 999, 't-historical', 'run_cost_usd', 0.25, 'numeric', 'finalizer', 3),
          (4, 999, 't-historical', 'run_outcome_kind', 1.0, 'numeric', 'finalizer', 4),
          (5, NULL, 't-historical', 'run_duration_seconds', 4.0, 'numeric', 'board-metrics', 5),
          (6, NULL, 't-historical', 'run_attempt_index', 1.0, 'numeric', 'board-metrics', 6),
          (7, NULL, 't-historical', 'review_verdict', 1.0, 'binary', 'review_gate', 7),
          (8, NULL, 't-historical', 'review_iterations_to_approval', 2.0, 'numeric', 'review_gate', 8),
          (9, NULL, 't-historical', 'run_cost_usd', 0.5, 'numeric', 'finalizer', 9),
          (10, NULL, 't-historical', 'run_duration_seconds', 5.0, 'numeric', 'board-metrics', 10),
          (11, NULL, 't-historical', 'run_attempt_index', 2.0, 'numeric', 'board-metrics', 11),
          (12, NULL, 't-historical', 'review_verdict', 0.0, 'binary', 'review_gate', 12);
    """)
    conn.close()
    original_traces = _FakeLangfuseHandler.traces
    _FakeLangfuseHandler.traces = [
        {"id": "trace-live-run", "metadata": {"kanban_run_id": 7}},
        {"id": "trace-live-task", "metadata": {"kanban_task_id": "t-live"}},
    ]
    _FakeLangfuseHandler.remote_scores = []
    _FakeLangfuseHandler.ingestions = []
    server, thread = _start_server()
    try:
        result = export_scores(db_path=db_path, env=_env(server), backfill=True,
                               ledger_path=ledger_path, batch_size=2)
        again = export_scores(db_path=db_path, env=_env(server), backfill=True,
                              ledger_path=ledger_path, batch_size=2)
    finally:
        server.shutdown()
        thread.join()
        _FakeLangfuseHandler.traces = original_traces

    assert result["total_rows"] == 12
    assert result["matched"] == 12
    assert result["unmatched"] == 0
    assert result["matched"] / result["total_rows"] >= 0.9
    assert result["anchor_count"] == 1
    assert result["anchored_rows"] == 10
    assert result["posted_new"] == 12
    assert again["posted_new"] == 0
    assert again["anchor_count"] == 0
    assert len(json.loads(ledger_path.read_text())["exported_score_ids"]) == 12
    assert all(len(request["batch"]) <= 2 for request in _FakeLangfuseHandler.ingestions)
    anchors = [
        event["body"]
        for request in _FakeLangfuseHandler.ingestions
        for event in request["batch"]
        if event["type"] == "trace-create"
    ]
    assert len(anchors) == 1
    anchor = anchors[0]
    assert anchor["id"] == langfuse_export.trace_id_for_task("t-historical")
    assert anchor["metadata"] == {
        "kanban_task_id": "t-historical",
        "kanban_score_anchor": True,
        "score_cost_usd": 0.25,
    }
    assert anchor["sessionId"] == "t-historical"
    assert anchor["userId"] == "unknown"


def test_backfill_retries_transient_ingestion_failure(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "kanban.db"
    _make_db(db_path)
    _FakeLangfuseHandler.remote_scores = []
    _FakeLangfuseHandler.ingestions = []
    _FakeLangfuseHandler.fail_ingestion = 1
    sleeps: list[float] = []
    monkeypatch.setattr(langfuse_export.time, "sleep", sleeps.append)
    server, thread = _start_server()
    try:
        result = export_scores(db_path=db_path, env=_env(server), backfill=True,
                               ledger_path=tmp_path / "ledger.json", batch_size=50)
    finally:
        server.shutdown()
        thread.join()
    assert result["posted_new"] == 4
    assert sleeps == [0.5]


def test_backfill_event_limit_plans_resumes_without_orphaned_scores(tmp_path: Path) -> None:
    """A 100-event canary includes its anchor and resumes at the first unsent score."""
    db_path = tmp_path / "kanban.db"
    ledger_path = tmp_path / "ledger.json"
    _make_historical_score_db(db_path, 150)
    original_traces = _FakeLangfuseHandler.traces
    _FakeLangfuseHandler.traces = []
    _FakeLangfuseHandler.remote_scores = []
    _FakeLangfuseHandler.ingestions = []
    _FakeLangfuseHandler.fail_ingestion = 0
    server, thread = _start_server()
    try:
        env = _env(server)
        preview = export_scores(
            db_path=db_path, env=env, backfill=True, dry_run=True,
            ledger_path=ledger_path, event_limit=100,
        )
        first = export_scores(
            db_path=db_path, env=env, backfill=True,
            ledger_path=ledger_path, event_limit=100, batch_size=30,
        )
        first_events = [
            event for request in _FakeLangfuseHandler.ingestions for event in request["batch"]
        ]
        anchor = next(event["body"] for event in first_events if event["type"] == "trace-create")
        _FakeLangfuseHandler.traces = [
            {"id": anchor["id"], "metadata": anchor["metadata"]},
        ]
        second = export_scores(
            db_path=db_path, env=env, backfill=True,
            ledger_path=ledger_path, event_limit=100, batch_size=30,
        )
    finally:
        server.shutdown()
        thread.join()
        _FakeLangfuseHandler.traces = original_traces

    assert preview["would_post"] == 100
    assert preview["planned_anchors"] == 1
    assert preview["planned_scores"] == 99
    assert preview["remaining_events"] == 51
    assert len(first_events) == 100
    assert first_events[0]["type"] == "trace-create"
    assert all(
        event["body"]["traceId"] == anchor["id"]
        for event in first_events[1:]
        if event["type"] == "score-create"
    )
    assert first["posted_new"] == 99
    assert second["posted_new"] == 51
    assert second["planned_anchors"] == 0
    assert second["planned_scores"] == 51
    assert len(json.loads(ledger_path.read_text())["exported_score_ids"]) == 150


def test_cron_backfill_without_explicit_limit_uses_default_cap_and_reports_remaining(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kanban.db"
    default_limit = langfuse_export.DEFAULT_CRON_BACKFILL_EVENT_LIMIT
    _make_historical_score_db(db_path, default_limit + 5)
    original_traces = _FakeLangfuseHandler.traces
    _FakeLangfuseHandler.traces = []
    _FakeLangfuseHandler.remote_scores = []
    server, thread = _start_server()
    try:
        result = export_scores(
            db_path=db_path, env=_env(server), backfill=True, cron=True, dry_run=True,
        )
    finally:
        server.shutdown()
        thread.join()
        _FakeLangfuseHandler.traces = original_traces

    assert result["planned_events"] == default_limit
    assert result["remaining_events"] == 6  # five scores plus the task anchor
    assert result["backfill_limit_message"] == (
        f"cron backfill capped at {default_limit} events; 6 event(s) remain"
    )


def test_cron_backfill_explicit_limit_overrides_default_cap(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    default_limit = langfuse_export.DEFAULT_CRON_BACKFILL_EVENT_LIMIT
    explicit_limit = default_limit + 5
    _make_historical_score_db(db_path, explicit_limit)
    original_traces = _FakeLangfuseHandler.traces
    _FakeLangfuseHandler.traces = []
    _FakeLangfuseHandler.remote_scores = []
    server, thread = _start_server()
    try:
        result = export_scores(
            db_path=db_path, env=_env(server), backfill=True, cron=True, dry_run=True,
            event_limit=explicit_limit,
        )
    finally:
        server.shutdown()
        thread.join()
        _FakeLangfuseHandler.traces = original_traces

    assert result["planned_events"] == explicit_limit
    assert result["remaining_events"] == 1
    assert "backfill_limit_message" not in result


def test_non_cron_backfill_without_explicit_limit_remains_unbounded(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    default_limit = langfuse_export.DEFAULT_CRON_BACKFILL_EVENT_LIMIT
    _make_historical_score_db(db_path, default_limit + 5)
    original_traces = _FakeLangfuseHandler.traces
    _FakeLangfuseHandler.traces = []
    _FakeLangfuseHandler.remote_scores = []
    server, thread = _start_server()
    try:
        result = export_scores(
            db_path=db_path, env=_env(server), backfill=True, dry_run=True,
        )
    finally:
        server.shutdown()
        thread.join()
        _FakeLangfuseHandler.traces = original_traces

    assert result["planned_events"] == default_limit + 6
    assert result["remaining_events"] == 0
    assert "backfill_limit_message" not in result


def test_cron_backfill_cap_reads_environment_override(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _make_historical_score_db(db_path, 12)
    original_traces = _FakeLangfuseHandler.traces
    _FakeLangfuseHandler.traces = []
    _FakeLangfuseHandler.remote_scores = []
    server, thread = _start_server()
    try:
        env = _env(server)
        env["HERMES_LANGFUSE_CRON_BACKFILL_EVENT_LIMIT"] = "7"
        result = export_scores(
            db_path=db_path, env=env, backfill=True, cron=True, dry_run=True,
        )
    finally:
        server.shutdown()
        thread.join()
        _FakeLangfuseHandler.traces = original_traces

    assert result["planned_events"] == 7
    assert result["remaining_events"] == 6
    assert result["backfill_limit_message"] == "cron backfill capped at 7 events; 6 event(s) remain"


def test_backfill_event_limit_failure_does_not_advance_ledger(tmp_path: Path, monkeypatch) -> None:
    """A failed canary batch resumes from its first event rather than skipping it."""
    db_path = tmp_path / "kanban.db"
    ledger_path = tmp_path / "ledger.json"
    _make_historical_score_db(db_path, 2)
    original_traces = _FakeLangfuseHandler.traces
    _FakeLangfuseHandler.traces = []
    _FakeLangfuseHandler.remote_scores = []
    _FakeLangfuseHandler.ingestions = []
    _FakeLangfuseHandler.fail_ingestion = 5
    monkeypatch.setattr(langfuse_export.time, "sleep", lambda _seconds: None)
    server, thread = _start_server()
    try:
        with pytest.raises(RuntimeError):
            export_scores(
                db_path=db_path, env=_env(server), backfill=True,
                ledger_path=ledger_path, event_limit=100,
            )
        assert not ledger_path.exists()
        _FakeLangfuseHandler.fail_ingestion = 0
        retry = export_scores(
            db_path=db_path, env=_env(server), backfill=True,
            ledger_path=ledger_path, event_limit=100,
        )
    finally:
        server.shutdown()
        thread.join()
        _FakeLangfuseHandler.traces = original_traces
        _FakeLangfuseHandler.fail_ingestion = 0

    assert retry["posted_new"] == 2
    assert retry["planned_anchors"] == 1
    assert retry["planned_scores"] == 2
    assert len(json.loads(ledger_path.read_text())["exported_score_ids"]) == 2


def test_export_accepts_legacy_host_env_name(tmp_path: Path) -> None:
    """AC-3: HERMES_LANGFUSE_HOST accepted as backward-compatible fallback."""
    db_path = tmp_path / "kanban.db"
    _make_db(db_path)
    _FakeLangfuseHandler.scores = []
    server, thread = _start_server()
    try:
        # Only the legacy HOST name is set — no BASE_URL
        env = {"HERMES_LANGFUSE_HOST": f"http://127.0.0.1:{server.server_port}",
               "HERMES_LANGFUSE_PUBLIC_KEY": "pk-test", "HERMES_LANGFUSE_SECRET_KEY": "sk-test"}
        result = export_scores(db_path=db_path, env=env, dry_run=True)
    finally:
        server.shutdown()
        thread.join()
    assert result["matched"] == 3
    assert result["posted"] == 0


def test_export_no_op_when_nothing_matches(tmp_path: Path) -> None:
    """AC-2: matched=0/posted=0 is a valid no-op with no error raised."""
    db_path = tmp_path / "kanban.db"
    _make_empty_db(db_path)
    _FakeLangfuseHandler.scores = []
    server, thread = _start_server()
    try:
        env = _env(server)
        result = export_scores(db_path=db_path, env=env, dry_run=True)
    finally:
        server.shutdown()
        thread.join()
    assert result["matched"] == 0
    assert result["unmatched"] == 2
    assert result["posted"] == 0
    assert _FakeLangfuseHandler.scores == []


def test_cron_mode_silent_when_zero_posted(tmp_path: Path, capsys, monkeypatch) -> None:
    """AC-1 --cron: empty stdout when 0 scores posted (silent contract)."""
    from hermes_cli.kanban import _cmd_export_langfuse_scores
    import argparse
    import os

    db_path = tmp_path / "kanban.db"
    _make_empty_db(db_path)
    _FakeLangfuseHandler.scores = []
    server, thread = _start_server()
    # Pin HERMES_HOME so kanban_db_path() honours HERMES_KANBAN_DB.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_LANGFUSE_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "sk-test")
    try:
        args = argparse.Namespace(dry_run=True, cron=True)
        rc = _cmd_export_langfuse_scores(args)
    finally:
        server.shutdown()
        thread.join()
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "", f"stdout must be empty (silent), got: {captured.out!r}"


def test_cron_mode_one_line_when_n_posted(tmp_path: Path, capsys, monkeypatch) -> None:
    """AC-1 --cron: exactly one stdout line when N>0 scores posted."""
    from hermes_cli.kanban import _cmd_export_langfuse_scores
    import argparse
    import os

    db_path = tmp_path / "kanban.db"
    _make_db(db_path)
    _FakeLangfuseHandler.scores = []
    server, thread = _start_server()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_LANGFUSE_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "sk-test")
    try:
        args = argparse.Namespace(dry_run=False, cron=True)
        rc = _cmd_export_langfuse_scores(args)
    finally:
        server.shutdown()
        thread.join()
    captured = capsys.readouterr()
    assert rc == 0
    lines = [l for l in captured.out.splitlines() if l.strip()]
    assert len(lines) == 1, f"expected exactly 1 stdout line, got {len(lines)}: {captured.out!r}"
    assert "posted 3" in lines[0]


def test_cron_mode_error_exit_nonzero(tmp_path: Path, capsys, monkeypatch) -> None:
    """AC-1 --cron: error on stdout + non-zero exit when export fails."""
    from hermes_cli.kanban import _cmd_export_langfuse_scores
    import argparse
    import os

    db_path = tmp_path / "kanban.db"
    _make_db(db_path)
    _FakeLangfuseHandler.scores = []
    server, thread = _start_server()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    # Missing base URL and host — export_scores will raise RuntimeError.
    monkeypatch.delenv("HERMES_LANGFUSE_BASE_URL", raising=False)
    monkeypatch.delenv("HERMES_LANGFUSE_HOST", raising=False)
    monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "sk-test")
    try:
        args = argparse.Namespace(dry_run=False, cron=True)
        rc = _cmd_export_langfuse_scores(args)
    finally:
        server.shutdown()
        thread.join()
    captured = capsys.readouterr()
    assert rc != 0
    assert "error" in captured.out.lower()
    # No key/token values must appear.
    assert "sk-test" not in captured.out
    assert "pk-test" not in captured.out


def _make_trace_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, tenant TEXT);
        CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL);
        CREATE TABLE task_runs (
          id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT, status TEXT,
          started_at INTEGER, outcome TEXT, metadata TEXT, active_model TEXT,
          cost_usd REAL
        );
        INSERT INTO tasks VALUES ('root', 'default'), ('child', 'default');
        INSERT INTO task_links VALUES ('root', 'child');
        INSERT INTO task_runs VALUES
          (1, 'child', 'coder', 'done', 1764028800, 'completed',
           '{"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 2,
             "reasoning_tokens": 1, "cost_usd_equivalent": 0.25}', 'model-x', 0.1);
        INSERT INTO task_runs VALUES
          (2, 'root', 'reviewer', 'done', 1764028801, 'completed', '{}', 'model-y', 0.0);
    """)
    conn.close()


def test_synthesize_traces_is_deterministic_and_uses_generation_observation(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "kanban.db"
    _make_trace_db(db_path)
    requests: list[tuple[str, dict]] = []
    existing = [{"id": "live-trace", "environment": "production",
                 "metadata": {"kanban_run_id": 2}}]

    def fake_request(url: str, _authorization: str, *, method: str = "GET", payload=None):
        if url.endswith("/retention"):
            return {"active": False}
        if "/traces?" in url:
            return {"data": existing, "meta": {"totalPages": 1}}
        assert payload is not None
        requests.append((url, payload))
        existing.append(payload["batch"][0]["body"])
        return {}

    monkeypatch.setattr(langfuse_export, "_request", fake_request)
    env = {"HERMES_LANGFUSE_BASE_URL": "http://fake", "HERMES_LANGFUSE_PUBLIC_KEY": "pk",
           "HERMES_LANGFUSE_SECRET_KEY": "sk", "HERMES_KANBAN_BOARD": "default"}
    report = synthesize_traces(db_path=db_path, env=env)
    assert report["synthesized"] == 1
    assert report["skipped_live"] == 1
    assert len(requests) == 1
    trace, generation = requests[0][1]["batch"]
    assert trace["type"] == "trace-create"
    assert trace["body"]["id"] == trace_id_for_run(1)
    assert trace["body"]["sessionId"] == "root"
    assert trace["body"]["environment"] == "backfill"
    assert trace["body"]["timestamp"] == "2025-11-25T00:00:00Z"
    assert {"kanban-worker", "board:default", "profile:coder", "model:model-x",
            "outcome:completed"}.issubset(trace["body"]["tags"])
    assert generation["type"] == "generation-create"
    assert generation["body"]["usageDetails"] == {
        "input": 10, "output": 4, "cache_read_input_tokens": 2, "reasoning_tokens": 1}
    assert generation["body"]["costDetails"] == {"total": 0.25}
    second = synthesize_traces(db_path=db_path, env=env)
    assert second["synthesized"] == 0
    assert second["skipped_seen"] == 1
    assert len(requests) == 1


def test_synthesize_traces_batches_and_resumes(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "kanban.db"
    _make_trace_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO task_runs VALUES (3, 'root', 'coder', 'done', 1764028802, 'completed', '{}', 'm', 0)")
    conn.commit()
    conn.close()
    requests: list[dict] = []

    def fake_request(url: str, _authorization: str, *, method: str = "GET", payload=None):
        if url.endswith("/retention"):
            return {"active": False}
        if "/traces?" in url:
            return {"data": [], "meta": {"totalPages": 1}}
        assert payload is not None
        requests.append(payload)
        return {}

    monkeypatch.setattr(langfuse_export, "_request", fake_request)
    env = {"HERMES_LANGFUSE_BASE_URL": "http://fake", "HERMES_LANGFUSE_PUBLIC_KEY": "pk",
           "HERMES_LANGFUSE_SECRET_KEY": "sk"}
    report = synthesize_traces(db_path=db_path, env=env, batch_size=1,
                               resume_path=tmp_path / "resume")
    assert len(requests) == 3
    assert all(len(request["batch"]) == 2 for request in requests)
    assert report["resume_after"] == 3
    assert (tmp_path / "resume").read_text() == "3"


def test_synthesize_traces_does_not_write_when_retention_is_active(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "kanban.db"
    _make_trace_db(db_path)
    calls: list[str] = []

    def fake_request(url: str, _authorization: str, *, method: str = "GET", payload=None):
        calls.append(url)
        if url.endswith("/retention"):
            return {"retention": {"enabled": True}}
        raise AssertionError("trace or ingestion must not be queried after active retention")

    monkeypatch.setattr(langfuse_export, "_request", fake_request)
    env = {"HERMES_LANGFUSE_BASE_URL": "http://fake", "HERMES_LANGFUSE_PUBLIC_KEY": "pk",
           "HERMES_LANGFUSE_SECRET_KEY": "sk"}
    report = synthesize_traces(db_path=db_path, env=env)
    assert report["stopped"] == "active_retention_policy"
    assert report["posted"] == 0
    assert calls == ["http://fake/api/public/retention"]
