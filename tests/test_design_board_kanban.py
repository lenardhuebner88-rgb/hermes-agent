"""Tests for design_board_kanban lifecycle hooks."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from hermes_cli import design_board_kanban as dbk
from hermes_cli import design_board_store as store
from hermes_cli import kanban_db


@pytest.fixture(autouse=True)
def _isolate_design_board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    # Initialise a fresh design-board store and kanban DB.
    store_path = home / "design-board" / "board.json"
    store_path.parent.mkdir()
    store_path.write_text("{\"cards\": [], \"id_counter\": 0}")
    db_path = kanban_db.kanban_db_path(board="default")
    kanban_db._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kanban_db.init_db()
    dbk.register_lifecycle_hooks = lambda: None
    yield


@pytest.fixture
def board_db():
    with kanban_db.connect_closing() as conn:
        yield conn


@pytest.fixture(autouse=True)
def _stub_chromium(monkeypatch):
    monkeypatch.setattr(dbk, "_render_dashboard_view", lambda card: b"png")
    monkeypatch.setenv("HERMES_DESIGN_BOARD_DASHBOARD_BASE_URL", "http://127.0.0.1:9119")


def _create_done_task(conn, task_id: str, completed_at: int, metadata: dict | None = None):
    kanban_db.create_task(
        conn,
        title="Fix gap",
        assignee="coder",
        initial_status="running",
    )
    # Overwrite generated id with the test id.
    conn.execute("UPDATE tasks SET id = ?, status = ?, completed_at = ? WHERE title = ?", (
        task_id,
        "done",
        completed_at,
        "Fix gap",
    ))
    run_id = kanban_db._synthesize_ended_run(
        conn,
        task_id,
        outcome="completed",
        summary="done",
        metadata=metadata,
    )
    kanban_db._append_event(conn, task_id, "completed", {"completed_at": completed_at}, run_id=run_id)
    return run_id


def test_receipt_skipped_when_task_not_terminal():
    card_id = store.create_card(
        kind="bug",
        title="Gap",
        target={"view": "/control/fleet"},
    )
    store.link_task(card_id, "t_running")
    assert dbk.attach_completion_receipts_for_task("t_running", status="running") == []


def test_receipt_written_when_linked_task_done(monkeypatch):
    completed_at = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
    with kanban_db.connect_closing() as conn:
        _create_done_task(conn, "t_done", completed_at, metadata={"commit": "abc123"})

    card_id = store.create_card(
        kind="bug",
        title="Gap",
        target={"view": "/control/fleet"},
    )
    store.link_task(card_id, "t_done")

    entries = dbk.attach_completion_receipts_for_task("t_done", status="done", run_id=1)

    assert len(entries) == 1
    updated = store.get_card(card_id)
    assert updated is not None
    entry = updated["entries"][0]
    assert entry["author"] == "system"
    assert entry["kind"] == "comment"
    assert entry["note"] == "task-receipt task:t_done completed_at:2025-01-01T00:00:00Z commit:abc123"


def test_completion_receipt_is_idempotent_per_task(monkeypatch):
    completed_at = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
    with kanban_db.connect_closing() as conn:
        _create_done_task(conn, "t_done", completed_at)

    card_id = store.create_card(
        kind="bug",
        title="Gap",
        target={"view": "/control/fleet"},
    )
    store.link_task(card_id, "t_done")

    dbk.attach_completion_receipts_for_task("t_done", status="done")
    dbk.attach_completion_receipts_for_task("t_done", status="done")

    updated = store.get_card(card_id)
    assert updated is not None
    assert len(updated["entries"]) == 1


def test_commit_fallback_from_earlier_coder_run(monkeypatch, board_db):
    """Review-gated task: final reviewer run has no commit, but an earlier coder run does."""
    completed_at = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())

    kanban_db.create_task(
        board_db,
        title="Review task",
        assignee="reviewer",
        initial_status="running",
    )
    board_db.execute(
        "UPDATE tasks SET id = ?, status = ?, completed_at = ? WHERE title = ?",
        ("t_reviewed", "done", completed_at, "Review task"),
    )
    # Earlier coder run with the actual fix commit.
    kanban_db._synthesize_ended_run(
        board_db,
        "t_reviewed",
        outcome="completed",
        summary="coder done",
        metadata={"commit": "coderfixabc"},
    )
    # Final reviewer run without commit metadata.
    reviewer_run = kanban_db._synthesize_ended_run(
        board_db,
        "t_reviewed",
        outcome="completed",
        summary="reviewer approved",
        metadata=None,
    )

    card_id = store.create_card(
        kind="bug",
        title="Gap",
        target={"view": "/control/fleet"},
    )
    store.link_task(card_id, "t_reviewed")

    dbk.attach_completion_receipts_for_task("t_reviewed", status="done", run_id=reviewer_run)

    updated = store.get_card(card_id)
    assert updated is not None
    entry = updated["entries"][0]
    assert "commit:coderfixabc" in entry["note"]


def test_commit_fallback_from_submitted_for_review_event(monkeypatch, board_db):
    """Commit is only present in the submitted_for_review worker_gate payload."""
    completed_at = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())

    kanban_db.create_task(
        board_db,
        title="Review task 2",
        assignee="reviewer",
        initial_status="running",
    )
    board_db.execute(
        "UPDATE tasks SET id = ?, status = ?, completed_at = ? WHERE title = ?",
        ("t_reviewed2", "done", completed_at, "Review task 2"),
    )
    # No commit anywhere except the submitted_for_review event.
    kanban_db._synthesize_ended_run(
        board_db,
        "t_reviewed2",
        outcome="completed",
        summary="coder done",
        metadata=None,
    )
    kanban_db._append_event(
        board_db,
        "t_reviewed2",
        "submitted_for_review",
        {"worker_gate": {"commit": "wgcommit123"}},
    )
    reviewer_run = kanban_db._synthesize_ended_run(
        board_db,
        "t_reviewed2",
        outcome="completed",
        summary="reviewer approved",
        metadata=None,
    )

    card_id = store.create_card(
        kind="bug",
        title="Gap",
        target={"view": "/control/fleet"},
    )
    store.link_task(card_id, "t_reviewed2")

    dbk.attach_completion_receipts_for_task("t_reviewed2", status="done", run_id=reviewer_run)

    updated = store.get_card(card_id)
    assert updated is not None
    entry = updated["entries"][0]
    assert "commit:wgcommit123" in entry["note"]


def test_after_screenshot_attaches_png(monkeypatch):
    card_id = store.create_card(
        kind="bug",
        title="Gap",
        target={"view": "/control/fleet"},
    )
    store.link_task(card_id, "t_done")

    calls = []
    monkeypatch.setattr(dbk, "_render_dashboard_view", lambda card: calls.append(card) or b"png")

    entries = dbk.attach_after_screenshots_for_task("t_done", status="done")

    assert len(entries) == 1
    updated = store.get_card(card_id)
    assert updated is not None
    entry = updated["entries"][0]
    assert entry["author"] == "system"
    assert entry["kind"] == "screenshot"


def test_after_screenshot_degrades_to_comment_on_render_failure(monkeypatch):
    card_id = store.create_card(
        kind="bug",
        title="Gap",
        target={"view": "/control/fleet"},
    )
    store.link_task(card_id, "t_done")

    def fail_render(card):
        raise RuntimeError("chromium missing")

    monkeypatch.setattr(dbk, "_render_dashboard_view", fail_render)

    entries = dbk.attach_after_screenshots_for_task("t_done", status="done")

    assert len(entries) == 1
    updated = store.get_card(card_id)
    assert updated is not None
    entry = updated["entries"][0]
    assert entry["author"] == "system"
    assert entry["kind"] == "comment"
    assert "chromium missing" in entry["note"]


def test_after_screenshot_is_idempotent_per_task(monkeypatch):
    card_id = store.create_card(
        kind="bug",
        title="Gap",
        target={"view": "/control/fleet"},
    )
    store.link_task(card_id, "t_done")

    calls = []
    monkeypatch.setattr(dbk, "_render_dashboard_view", lambda card: calls.append(card) or b"png")

    assert len(dbk.attach_after_screenshots_for_task("t_done", status="done")) == 1
    assert dbk.attach_after_screenshots_for_task("t_done", status="done") == []
    assert len(calls) == 1


def test_dashboard_url_for_target_view(monkeypatch):
    monkeypatch.setenv("HERMES_DESIGN_BOARD_DASHBOARD_BASE_URL", "http://127.0.0.1:9119/")

    assert dbk._dashboard_url_for_card({"target": {"view": "/control/fleet"}}) == "http://127.0.0.1:9119/control/fleet"
    assert dbk._dashboard_url_for_card({"target": {"view": "control/fleet"}}) == "http://127.0.0.1:9119/control/fleet"
    assert dbk._dashboard_url_for_card({"target": {"view": "https://example.test/x"}}) == "https://example.test/x"


def test_commit_from_payload_variants():
    assert dbk._commit_from_payload(None) is None
    assert dbk._commit_from_payload('{"commit": "abc"}') == "abc"
    assert dbk._commit_from_payload('{"commit_hash": "def"}') == "def"
    assert dbk._commit_from_payload('{"metadata": {"commit": "ghi"}}') == "ghi"
    assert dbk._commit_from_payload('{"worker_gate": {"commit": "jkl"}}') == "jkl"
    assert dbk._commit_from_payload('{"metadata": {"worker_gate": {"commit": "mno"}}}') == "mno"
    assert dbk._commit_from_payload("not-json") is None
    assert dbk._commit_from_payload('["commit"]') is None
