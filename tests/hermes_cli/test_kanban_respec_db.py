"""Tests for kb.respec_task — the DB-layer in-place edit of a non-running
task's body / acceptance criteria. AC-B-respec-nonrunning. LLM-free.

The editable sibling of ``specify_triage_task``: ``specify`` only touches the
``triage`` column (and promotes to ``todo``); ``respec`` rewrites ``body`` /
``acceptance_criteria`` *in place* on any task that is not currently being
executed, WITHOUT changing its column.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_with_status(conn, status, *, title="task", body="old body"):
    """Create a task and force it into ``status`` via a raw UPDATE.

    create_task lands a normal task in ``ready``; for the status-guard tests we
    just stamp the column directly — the guard reads ``status``, nothing else.
    """
    tid = kb.create_task(conn, title=title, body=body)
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    return tid


# --- AC-B: allowed statuses are editable -----------------------------------


@pytest.mark.parametrize(
    "status", ["triage", "todo", "scheduled", "ready", "blocked"]
)
def test_respec_allowed_statuses_edit_body(kanban_home, status):
    with kb.connect() as conn:
        tid = _create_with_status(conn, status, body="old body")
    with kb.connect() as conn:
        ok = kb.respec_task(conn, tid, body="new body", author="op")
    assert ok is True
    with kb.connect() as conn:
        t = kb.get_task(conn, tid)
    assert t.body == "new body"
    # respec NEVER changes the column — that's the whole point vs. specify.
    assert t.status == status


# --- AC-B: guarded statuses are rejected -----------------------------------


@pytest.mark.parametrize("status", ["running", "review", "done", "archived"])
def test_respec_rejects_guarded_statuses(kanban_home, status):
    with kb.connect() as conn:
        tid = _create_with_status(conn, status, body="old body")
    with kb.connect() as conn:
        ok = kb.respec_task(conn, tid, body="MUST NOT APPLY", author="op")
    assert ok is False
    with kb.connect() as conn:
        t = kb.get_task(conn, tid)
    # Nothing touched.
    assert t.body == "old body"
    assert t.status == status


def test_respec_allowlist_is_exactly_the_non_running_columns(kanban_home):
    # Lock the contract: allowed == {triage,todo,scheduled,ready,blocked} and
    # nothing else, so a future status defaults to rejected (allowlist, not
    # denylist). 'review' must be excluded — the verifier runs against the AC.
    assert kb.RESPEC_ALLOWED_STATUSES == {
        "triage", "todo", "scheduled", "ready", "blocked"
    }
    assert "running" not in kb.RESPEC_ALLOWED_STATUSES
    assert "review" not in kb.RESPEC_ALLOWED_STATUSES
    # Every allowed status is a real status.
    assert kb.RESPEC_ALLOWED_STATUSES <= kb.VALID_STATUSES


# --- field selectivity ------------------------------------------------------


def test_respec_only_body_preserves_ac(kanban_home):
    with kb.connect() as conn:
        tid = _create_with_status(conn, "blocked", body="b0")
        # Seed structured AC directly.
        conn.execute(
            "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
            (json.dumps(["AC-1: keep me"]), tid),
        )
    with kb.connect() as conn:
        kb.respec_task(conn, tid, body="b1")
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT body, acceptance_criteria FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert row["body"] == "b1"
    assert json.loads(row["acceptance_criteria"]) == ["AC-1: keep me"]


def test_respec_only_ac_preserves_body(kanban_home):
    with kb.connect() as conn:
        tid = _create_with_status(conn, "todo", body="keep this body")
    with kb.connect() as conn:
        ok = kb.respec_task(
            conn, tid, acceptance_criteria="- AC-1: do the new thing"
        )
    assert ok is True
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT body, acceptance_criteria FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert row["body"] == "keep this body"
    # Stored as verifier-readable structured JSON.
    parsed = json.loads(row["acceptance_criteria"])
    assert isinstance(parsed, list)
    assert any("do the new thing" in str(item) for item in parsed)


def test_respec_ac_text_normalized_to_structured_json(kanban_home):
    with kb.connect() as conn:
        tid = _create_with_status(conn, "ready")
    with kb.connect() as conn:
        kb.respec_task(
            conn,
            tid,
            acceptance_criteria="- AC-1: alpha\n- AC-2: beta",
        )
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT acceptance_criteria FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    parsed = json.loads(row["acceptance_criteria"])  # must be valid JSON
    flat = " ".join(str(i) for i in parsed)
    assert "alpha" in flat and "beta" in flat


def test_respec_blank_ac_with_unparseable_text_raises(kanban_home):
    # Non-blank AC that has no 'AC-<id>:' bullet must fail loud rather than
    # silently clearing the column.
    with kb.connect() as conn:
        tid = _create_with_status(conn, "todo")
    with kb.connect() as conn, pytest.raises(ValueError):
        kb.respec_task(conn, tid, acceptance_criteria="just some prose")
    # Original untouched.
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).body == "old body"


# --- bookkeeping: events, comments, unknown id ------------------------------


def test_respec_returns_false_for_unknown_id(kanban_home):
    with kb.connect() as conn:
        ok = kb.respec_task(conn, "t_nope", body="x")
    assert ok is False


def test_respec_emits_respecified_event(kanban_home):
    with kb.connect() as conn:
        tid = _create_with_status(conn, "todo", body="b0")
    with kb.connect() as conn:
        kb.respec_task(conn, tid, body="b1", author="op")
    with kb.connect() as conn:
        events = kb.list_events(conn, tid)
    ev = next((e for e in events if e.kind == "respecified"), None)
    assert ev is not None
    assert "body" in (ev.payload or {}).get("changed_fields", [])


def test_respec_audit_comment_only_with_author(kanban_home):
    # With author → audit comment.
    with kb.connect() as conn:
        tid1 = _create_with_status(conn, "todo", body="b0")
        kb.respec_task(conn, tid1, body="b1", author="ace")
        c1 = kb.list_comments(conn, tid1)
    assert len(c1) == 1
    assert "Respecified" in c1[0].body
    assert c1[0].author == "ace"

    # Without author → silent (no comment).
    with kb.connect() as conn:
        tid2 = _create_with_status(conn, "todo", body="b0")
        kb.respec_task(conn, tid2, body="b1")
        c2 = kb.list_comments(conn, tid2)
    assert c2 == []


def test_respec_noop_when_nothing_changed(kanban_home):
    # Passing the identical body is a successful no-op: editable status, but no
    # comment / event spam.
    with kb.connect() as conn:
        tid = _create_with_status(conn, "todo", body="same")
    with kb.connect() as conn:
        ok = kb.respec_task(conn, tid, body="same", author="ace")
    assert ok is True
    with kb.connect() as conn:
        assert kb.list_comments(conn, tid) == []
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "respecified" not in kinds
