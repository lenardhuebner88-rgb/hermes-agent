from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hermes_cli.langfuse_scores_export import export_scores


class _FakeLangfuseHandler(BaseHTTPRequestHandler):
    traces = [
        {"id": "trace-run", "metadata": {"kanban_run_id": 7}},
        {"id": "trace-task", "metadata": json.dumps({"kanban_task_id": "t-task"})},
    ]
    scores: list[dict] = []

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"data": self.traces, "meta": {"totalPages": 1}}).encode())

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers["Content-Length"])
        self.scores.append(json.loads(self.rfile.read(length)))
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
