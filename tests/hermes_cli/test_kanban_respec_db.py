"""Tests for ``kb.respec_task`` replacement semantics."""

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


def _create_with_status(conn, status, *, title="task", body="old body"):
    tid = kb.create_task(conn, title=title, body=body)
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    return tid


def _links(conn) -> set[tuple[str, str]]:
    rows = conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()
    return {(row["parent_id"], row["child_id"]) for row in rows}


@pytest.mark.parametrize(
    "status", ["triage", "todo", "scheduled", "ready", "blocked"]
)
def test_respec_allowed_statuses_create_replacement_and_archive_old(
    kanban_home, status
):
    with kb.connect() as conn:
        tid = _create_with_status(
            conn,
            status,
            title="old title",
            body="old body",
        )
        conn.execute(
            "UPDATE tasks SET priority = 5, kind = 'code', epic_id = 'epic-1' "
            "WHERE id = ?",
            (tid,),
        )
    with kb.connect() as conn:
        new_id = kb.respec_task(
            conn,
            tid,
            title="new title",
            body="new body",
            author="op",
        )
    assert new_id
    assert new_id != tid
    with kb.connect() as conn:
        old = kb.get_task(conn, tid)
        new = kb.get_task(conn, new_id)
    assert old is not None
    assert new is not None
    assert old.status == "archived"
    assert old.completed_at is not None
    assert old.body == "old body"
    assert new.title == "new title"
    assert new.body == "new body"
    assert new.status == status
    assert new.priority == 5
    assert new.kind == "code"
    assert new.epic_id == "epic-1"


@pytest.mark.parametrize("status", ["running", "review", "done", "archived"])
def test_respec_rejects_guarded_statuses(kanban_home, status):
    with kb.connect() as conn:
        tid = _create_with_status(conn, status, body="old body")
    with kb.connect() as conn:
        new_id = kb.respec_task(conn, tid, body="MUST NOT APPLY", author="op")
    assert new_id is None
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.body == "old body"
        assert task.status == status
        assert conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 1


def test_respec_allowlist_is_exactly_the_non_running_columns(kanban_home):
    assert kb.RESPEC_ALLOWED_STATUSES == {
        "triage", "todo", "scheduled", "ready", "blocked"
    }
    assert "running" not in kb.RESPEC_ALLOWED_STATUSES
    assert "review" not in kb.RESPEC_ALLOWED_STATUSES
    assert kb.RESPEC_ALLOWED_STATUSES <= kb.VALID_STATUSES


def test_respec_copies_parent_links_and_adds_provenance_link(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", body="p")
        assert kb.complete_task(conn, parent, result="done")
        old = kb.create_task(conn, title="old", body="old", parents=[parent])
        child = kb.create_task(conn, title="child", body="child", parents=[old])
    with kb.connect() as conn:
        new = kb.respec_task(conn, old, body="new body")
    assert new
    with kb.connect() as conn:
        links = _links(conn)
    assert (parent, new) in links
    assert (old, new) in links
    assert (old, child) in links
    assert (new, child) not in links


def test_respec_only_body_preserves_ac_on_new_task(kanban_home):
    with kb.connect() as conn:
        tid = _create_with_status(conn, "blocked", body="b0")
        conn.execute(
            "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
            (json.dumps(["AC-1: keep me"]), tid),
        )
    with kb.connect() as conn:
        new_id = kb.respec_task(conn, tid, body="b1")
    with kb.connect() as conn:
        old = conn.execute(
            "SELECT body, acceptance_criteria FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        new = conn.execute(
            "SELECT body, acceptance_criteria FROM tasks WHERE id = ?", (new_id,)
        ).fetchone()
    assert old["body"] == "b0"
    assert new["body"] == "b1"
    assert json.loads(new["acceptance_criteria"]) == ["AC-1: keep me"]


def test_respec_only_ac_preserves_body_on_new_task(kanban_home):
    with kb.connect() as conn:
        tid = _create_with_status(conn, "todo", body="keep this body")
    with kb.connect() as conn:
        new_id = kb.respec_task(
            conn, tid, acceptance_criteria="- AC-1: do the new thing"
        )
    assert new_id
    with kb.connect() as conn:
        old = conn.execute(
            "SELECT body, acceptance_criteria FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        new = conn.execute(
            "SELECT body, acceptance_criteria FROM tasks WHERE id = ?", (new_id,)
        ).fetchone()
    assert old["body"] == "keep this body"
    assert old["acceptance_criteria"] is None
    assert new["body"] == "keep this body"
    parsed = json.loads(new["acceptance_criteria"])
    assert any("do the new thing" in str(item) for item in parsed)


def test_respec_ac_text_normalized_to_structured_json(kanban_home):
    with kb.connect() as conn:
        tid = _create_with_status(conn, "ready")
    with kb.connect() as conn:
        new_id = kb.respec_task(
            conn,
            tid,
            acceptance_criteria="- AC-1: alpha\n- AC-2: beta",
        )
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT acceptance_criteria FROM tasks WHERE id = ?", (new_id,)
        ).fetchone()
    parsed = json.loads(row["acceptance_criteria"])
    flat = " ".join(str(i) for i in parsed)
    assert "alpha" in flat and "beta" in flat


def test_respec_blank_ac_with_unparseable_text_raises(kanban_home):
    with kb.connect() as conn:
        tid = _create_with_status(conn, "todo")
    with kb.connect() as conn, pytest.raises(ValueError):
        kb.respec_task(conn, tid, acceptance_criteria="just some prose")
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).body == "old body"
        assert conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 1


def test_respec_returns_none_for_unknown_id(kanban_home):
    with kb.connect() as conn:
        new_id = kb.respec_task(conn, "t_nope", body="x")
    assert new_id is None


def test_respec_emits_events_and_pointer_comment(kanban_home):
    with kb.connect() as conn:
        tid = _create_with_status(conn, "todo", body="b0")
    with kb.connect() as conn:
        new_id = kb.respec_task(conn, tid, body="b1", author="op")
    with kb.connect() as conn:
        old_events = kb.list_events(conn, tid)
        new_events = kb.list_events(conn, new_id)
        comments = kb.list_comments(conn, tid)
    assert any(e.kind == "completed" for e in old_events)
    assert any(e.kind == "archived" for e in old_events)
    assert any(e.kind == "respecced" for e in old_events)
    assert any(e.kind == "created" for e in new_events)
    assert any(f"respecced → {new_id}" in c.body for c in comments)
    assert comments[0].author == "op"


def _seed_ac(conn, tid, ac_text: str) -> None:
    conn.execute(
        "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
        (json.dumps([ac_text]), tid),
    )


def test_respec_blank_ac_empty_string_raises_and_ac_unchanged(kanban_home):
    with kb.connect() as conn:
        tid = _create_with_status(conn, "todo", body="b0")
        _seed_ac(conn, tid, "AC-1: must survive")
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="blank"):
            kb.respec_task(conn, tid, acceptance_criteria="")
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT acceptance_criteria FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 1
    assert row["acceptance_criteria"] is not None
    assert "must survive" in row["acceptance_criteria"]


def test_respec_blank_ac_whitespace_raises_and_ac_unchanged(kanban_home):
    with kb.connect() as conn:
        tid = _create_with_status(conn, "todo", body="b0")
        _seed_ac(conn, tid, "AC-1: must survive")
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="blank"):
            kb.respec_task(conn, tid, acceptance_criteria="   ")
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT acceptance_criteria FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 1
    assert row["acceptance_criteria"] is not None
    assert "must survive" in row["acceptance_criteria"]
