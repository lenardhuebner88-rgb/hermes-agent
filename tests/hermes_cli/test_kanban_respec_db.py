"""Tests for ``kb.respec_task`` replacement semantics."""

from __future__ import annotations

import json
import sqlite3
import time
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
    # triage/scheduled pass through unchanged; every other allowed status
    # becomes a fresh executable node whose readiness derives from its true
    # parents (here: none) — so it lands 'ready'.
    expected_status = status if status in {"triage", "scheduled"} else "ready"
    assert new.status == expected_status
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


def test_respec_rewires_parent_and_child_links_without_source_dependency(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", body="p")
        assert kb.complete_task(conn, parent, result="done")
        old = kb.create_task(conn, title="old", body="old", parents=[parent])
        children = [
            kb.create_task(conn, title=f"child {index}", body="child", parents=[old])
            for index in range(2)
        ]
    with kb.connect() as conn:
        new = kb.respec_task(conn, old, body="new body")
    assert new
    with kb.connect() as conn:
        links = _links(conn)
    # Replacement inherits the true parent, drops the archived-source edge, and
    # every downstream child is moved onto the replacement so the chain stays
    # executable.
    assert (parent, new) in links
    assert (old, new) not in links
    for child in children:
        assert (old, child) not in links
        assert (new, child) in links


def test_respec_replacement_waits_only_on_unsatisfied_true_parents(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", body="p")
        old = kb.create_task(conn, title="old", body="old", parents=[parent])

    with kb.connect() as conn:
        new = kb.respec_task(conn, old, body="replacement")
    assert new is not None

    with kb.connect() as conn:
        replacement = kb.get_task(conn, new)
        links = _links(conn)
    # Parent is not done → replacement must wait in 'todo', never inherit a
    # spurious 'ready' from the source, and never depend on the archived source.
    assert replacement is not None
    assert replacement.status == "todo"
    assert (parent, new) in links
    assert (old, new) not in links


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


def test_respec_review_gated_blocked_task_avoids_terminal_review_authority(
    kanban_home, monkeypatch
):
    monkeypatch.setattr(kb, "_review_gate_should_apply", lambda conn, task_id, run_id: True)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review gated", body="old", assignee="coder")
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
        with pytest.raises(
            sqlite3.IntegrityError,
            match="review-gated task done transition requires terminal review authority",
        ):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))

        new_id = kb.respec_task(
            conn,
            tid,
            body="replacement",
            acceptance_criteria="- AC-1: replacement AC",
            author="operator",
        )

    with kb.connect() as conn:
        old = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        new = conn.execute("SELECT * FROM tasks WHERE id = ?", (new_id,)).fetchone()
    assert old["status"] == "archived"
    assert old["result"] == f"respecced → {new_id}"
    assert new["body"] == "replacement"
    assert "replacement AC" in new["acceptance_criteria"]


def test_respec_rolls_back_source_and_replacement_on_link_failure(
    kanban_home, monkeypatch
):
    with kb.connect() as conn:
        tid = _create_with_status(conn, "blocked", body="old")
        child = kb.create_task(conn, title="child", body="child", parents=[tid])
        before_tasks = [
            tuple(row)
            for row in conn.execute(
                "SELECT id, status, completed_at, result FROM tasks ORDER BY id"
            ).fetchall()
        ]
        before_links = _links(conn)

    def fail_link(conn, parent_id, child_id):
        raise RuntimeError("link failed")

    monkeypatch.setattr(kb, "_link_tasks_in_txn", fail_link)
    with pytest.raises(RuntimeError, match="link failed"):
        with kb.connect() as conn:
            kb.respec_task(conn, tid, body="replacement")

    with kb.connect() as conn:
        after_tasks = [
            tuple(row)
            for row in conn.execute(
                "SELECT id, status, completed_at, result FROM tasks ORDER BY id"
            ).fetchall()
        ]
        after_links = _links(conn)
    # A mid-rewire failure must leave the whole graph untouched: the source
    # stays blocked, no replacement leaks, and the child stays linked to the
    # source (the DELETE of its edge is rolled back with everything else).
    assert before_tasks == after_tasks
    assert before_links == after_links == {(tid, child)}


def test_respec_future_due_at_lands_todo_and_unclaimable(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", body="p")
        assert kb.complete_task(conn, parent, result="done")
        old = kb.create_task(conn, title="old", body="old", parents=[parent])
        future = int(time.time()) + 3600
        conn.execute(
            "UPDATE tasks SET status = 'ready', due_at = ? WHERE id = ?",
            (future, old),
        )
    with kb.connect() as conn:
        new = kb.respec_task(conn, old, body="replacement")
    assert new
    with kb.connect() as conn:
        replacement = kb.get_task(conn, new)
        # even a recompute pass must not promote a not-yet-due replacement
        promoted = kb.recompute_ready(conn)
        after_recompute = kb.get_task(conn, new)
        # claim must fail while the task is not 'ready'
        claimed = kb.claim_task(conn, new)
    assert replacement is not None
    assert replacement.due_at == future
    # copied future due_at → held in 'todo', never 'ready', despite done parent
    assert replacement.status == "todo"
    assert promoted == 0
    assert after_recompute.status == "todo"
    assert claimed is None


def test_respec_future_due_at_promotes_and_claims_once_due(kanban_home, monkeypatch):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", body="p")
        assert kb.complete_task(conn, parent, result="done")
        old = kb.create_task(conn, title="old", body="old", parents=[parent])
        future = int(time.time()) + 3600
        conn.execute(
            "UPDATE tasks SET status = 'ready', due_at = ? WHERE id = ?",
            (future, old),
        )
    with kb.connect() as conn:
        new = kb.respec_task(conn, old, body="replacement")
    assert new
    with kb.connect() as conn:
        assert kb.get_task(conn, new).status == "todo"
        assert kb.claim_task(conn, new) is None
    # advance the wall clock past the due time: recompute now promotes it and
    # claim succeeds.
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 7200)
    with kb.connect() as conn:
        promoted = kb.recompute_ready(conn)
        after = kb.get_task(conn, new)
        claimed = kb.claim_task(conn, new)
    assert promoted == 1
    assert after.status == "ready"
    assert claimed is not None
    assert claimed.status == "running"


def test_respec_past_due_at_is_immediately_ready(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", body="p")
        assert kb.complete_task(conn, parent, result="done")
        old = kb.create_task(conn, title="old", body="old", parents=[parent])
        past = int(time.time()) - 3600
        conn.execute(
            "UPDATE tasks SET status = 'ready', due_at = ? WHERE id = ?",
            (past, old),
        )
    with kb.connect() as conn:
        new = kb.respec_task(conn, old, body="replacement")
    assert new
    with kb.connect() as conn:
        replacement = kb.get_task(conn, new)
        claimed = kb.claim_task(conn, new)
    # a past due_at is no gate at all: the replacement is ready and claimable
    # immediately (parent already done).
    assert replacement.due_at == past
    assert replacement.status == "ready"
    assert claimed is not None
    assert claimed.status == "running"


def test_respec_replacement_chain_blocks_child_until_replacement_done(kanban_home):
    # Execution-semantics graph test: parent(done) -> old(ready) -> child(todo).
    # Respec must rewire so the child depends on the *replacement*, stays held
    # until the replacement completes, then promotes and claims.
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", body="p")
        assert kb.complete_task(conn, parent, result="done")
        old = kb.create_task(conn, title="old", body="old", parents=[parent])
        child = kb.create_task(conn, title="child", body="child", parents=[old])
        assert kb.get_task(conn, old).status == "ready"
        assert kb.get_task(conn, child).status == "todo"

    with kb.connect() as conn:
        new = kb.respec_task(conn, old, body="replacement")
    assert new
    with kb.connect() as conn:
        links = _links(conn)
        replacement = kb.get_task(conn, new)
        # replacement is executable now; child is rewired onto it and blocked
        assert (new, child) in links
        assert (old, child) not in links
        assert replacement.status == "ready"
        # child cannot be claimed while the replacement is not done
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"
        assert kb.claim_task(conn, child) is None

    # claim + complete the replacement; complete_task recomputes readiness, so
    # the child promotes off the now-done replacement and becomes claimable.
    with kb.connect() as conn:
        assert kb.claim_task(conn, new) is not None
        assert kb.complete_task(conn, new, result="replacement done")
    with kb.connect() as conn:
        after_child = kb.get_task(conn, child)
        claimed_child = kb.claim_task(conn, child)
    assert after_child.status == "ready"
    assert claimed_child is not None
    assert claimed_child.status == "running"
