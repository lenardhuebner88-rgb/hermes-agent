"""Focused contract tests for bounded lane-scope fixer routing."""

from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lane_fixer as lane_fixer
from hermes_cli import kanban_worktrees as kwt
from tests.hermes_cli._kanban_test_helpers import _git


@pytest.fixture
def lane_parent(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    info = kwt.ensure_worktree(repo, "t_lane_parent")

    with kb.connect_closing() as conn:
        parent_id = kb.create_task(
            conn,
            title="Lane scope parent",
            assignee="coder",
            created_by="tester",
            workspace_kind="dir",
            workspace_path=str(info["path"]),
            tenant="lane-tenant",
            priority=73,
            kind="code",
        )
        parent = conn.execute("SELECT * FROM tasks WHERE id = ?", (parent_id,)).fetchone()
        yield conn, parent, info


def _events(conn, task_id, kind):
    return [event for event in kb.list_events(conn, task_id) if event.kind == kind]


def test_routes_bounded_fixer_to_expected_lane_with_exact_scope(lane_parent):
    conn, parent, info = lane_parent
    violating = ["web/src/control/Panel.tsx", "web/src/control/App.tsx"]

    child_id = lane_fixer.maybe_route_lane_scope_fixer(
        conn,
        parent,
        violating_paths=violating,
        expected_lane="coder-frontend",
        now=int(time.time()),
    )

    assert child_id
    child = kb.get_task(conn, child_id)
    assert child.assignee == "coder-frontend"
    assert child.workspace_kind == "dir"
    assert child.workspace_path == str(info["path"])
    assert child.tenant == "lane-tenant"
    assert child.priority == 73
    assert child.max_retries == 1
    assert child.kind == "code"
    assert all(path in child.body for path in violating)
    assert parent["id"] in child.body
    assert "already committed" in child.body.lower()
    assert _events(conn, parent["id"], lane_fixer.LANE_FIXER_DISPATCHED_EVENT)
    assert _events(conn, child_id, lane_fixer.LANE_FIXER_FOR_EVENT)


def test_same_fingerprint_is_idempotent_and_open_fixer_blocks_second(lane_parent):
    conn, parent, _ = lane_parent
    first = lane_fixer.maybe_route_lane_scope_fixer(
        conn,
        parent,
        violating_paths=["web/src/control/Panel.tsx"],
        expected_lane="coder-frontend",
        now=100,
    )
    second = lane_fixer.maybe_route_lane_scope_fixer(
        conn,
        parent,
        violating_paths=["web/src/control/Panel.tsx"],
        expected_lane="coder-frontend",
        now=101,
    )

    assert first
    assert second is None
    assert len(_events(conn, parent["id"], lane_fixer.LANE_FIXER_DISPATCHED_EVENT)) == 1


def test_exhausted_fixer_budget_emits_operator_escalation(lane_parent):
    conn, parent, _ = lane_parent
    now = int(time.time())
    first = lane_fixer.maybe_route_lane_scope_fixer(
        conn,
        parent,
        violating_paths=["web/src/control/Panel.tsx"],
        expected_lane="coder-frontend",
        now=now,
    )
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (first,))
    second = lane_fixer.maybe_route_lane_scope_fixer(
        conn,
        parent,
        violating_paths=["web/src/control/Panel.tsx"],
        expected_lane="coder-frontend",
        now=now + lane_fixer.kb.CONFLICT_FIXER_BACKOFF_SECONDS,
    )
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (second,))

    assert lane_fixer.maybe_route_lane_scope_fixer(
        conn,
        parent,
        violating_paths=["web/src/control/Panel.tsx"],
        expected_lane="coder-frontend",
        now=now + 2 * lane_fixer.kb.CONFLICT_FIXER_BACKOFF_SECONDS,
    ) is None
    assert _events(conn, parent["id"], lane_fixer.kb.OPERATOR_ESCALATION_EVENT)


def test_lane_fixer_never_routes_a_fixer_and_creation_failure_is_soft(
    lane_parent, monkeypatch
):
    conn, parent, _ = lane_parent
    child_id = lane_fixer.maybe_route_lane_scope_fixer(
        conn,
        parent,
        violating_paths=["web/src/control/Panel.tsx"],
        expected_lane="coder-frontend",
        now=100,
    )
    child = conn.execute("SELECT * FROM tasks WHERE id = ?", (child_id,)).fetchone()

    assert lane_fixer.maybe_route_lane_scope_fixer(
        conn,
        child,
        violating_paths=["hermes_cli/cli.py"],
        expected_lane="coder",
        now=101,
    ) is None

    second_parent_id = kb.create_task(
        conn,
        title="Second lane parent",
        assignee="coder",
        workspace_kind="dir",
        workspace_path=parent["workspace_path"],
        kind="code",
    )
    second_parent = conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (second_parent_id,)
    ).fetchone()

    def raise_create(*args, **kwargs):
        raise RuntimeError("creation failed")

    monkeypatch.setattr(lane_fixer.kb, "create_task", raise_create)
    assert lane_fixer.maybe_route_lane_scope_fixer(
        conn,
        second_parent,
        violating_paths=["web/src/control/Other.tsx"],
        expected_lane="coder-frontend",
        now=102,
    ) is None
    assert not _events(conn, second_parent_id, lane_fixer.LANE_FIXER_DISPATCHED_EVENT)
