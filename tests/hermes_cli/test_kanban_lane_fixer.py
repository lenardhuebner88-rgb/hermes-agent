"""Focused contract tests for bounded lane-scope fixer routing."""

from __future__ import annotations

import logging
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
    lane_parent, monkeypatch, caplog
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
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_lane_fixer"):
        assert lane_fixer.maybe_route_lane_scope_fixer(
            conn,
            second_parent,
            violating_paths=["web/src/control/Other.tsx"],
            expected_lane="coder-frontend",
            now=102,
        ) is None
    assert not _events(conn, second_parent_id, lane_fixer.LANE_FIXER_DISPATCHED_EVENT)
    assert "lane-scope fixer creation failed" in caplog.text


# ---------------------------------------------------------------------------
# V7 — resume parked parent when lane-scope fixer completes
# ---------------------------------------------------------------------------


def test_successful_lane_fixer_resumes_parked_parent(lane_parent, monkeypatch):
    """RED without resume helper: parent stays blocked after fixer done."""
    conn, parent, _info = lane_parent
    parent_id = parent["id"]
    violating = ["web/src/control/Panel.tsx"]

    # Park parent the same way production does (integration parked + blocked).
    kb.claim_task(conn, parent_id)
    reason = (
        "integration parked: lane-scope violation: assignee 'coder' changed "
        "paths outside its lane: web/src/control/Panel.tsx — this task should "
        "have been assigned to lane 'coder-frontend'."
    )
    assert kb.block_task(conn, parent_id, reason=reason, kind="integration")
    # Structured park event — same payload ``_enforce_lane_scope_on_complete``
    # writes; resume binds to its fingerprint (B4).
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            parent_id,
            "worker_gate_blocked",
            {
                "gate": "lane_scope",
                "violating_paths": violating,
                "expected_lane": "coder-frontend",
            },
        )

    child_id = lane_fixer.maybe_route_lane_scope_fixer(
        conn,
        conn.execute("SELECT * FROM tasks WHERE id = ?", (parent_id,)).fetchone(),
        violating_paths=violating,
        expected_lane="coder-frontend",
        now=int(time.time()),
    )
    assert child_id
    assert kb.get_task(conn, parent_id).status == "blocked"

    # Avoid re-entering the integrator (mirrors conflict-fixer resume tests).
    monkeypatch.setattr(
        kwt, "maybe_integrate_on_complete", lambda *a, **k: None,
    )
    lane_fixer.ensure_lifecycle_hooks_registered()

    assert kb.complete_task(conn, child_id, summary="lane scope fixed")
    parent_after = kb.get_task(conn, parent_id)
    resumed = _events(conn, parent_id, lane_fixer.LANE_FIXER_PARENT_RESUMED_EVENT)

    assert parent_after.status == "ready"
    assert len(resumed) == 1
    payload = resumed[0].payload
    if isinstance(payload, str):
        import json as _json
        payload = _json.loads(payload)
    assert payload["child_id"] == child_id


def test_lane_fixer_resume_skips_when_parent_context_changed(lane_parent):
    conn, parent, _ = lane_parent
    parent_id = parent["id"]
    child_id = lane_fixer.maybe_route_lane_scope_fixer(
        conn,
        parent,
        violating_paths=["web/src/control/Panel.tsx"],
        expected_lane="coder-frontend",
        now=100,
    )
    assert child_id
    # Parent not in a matching lane-scope park → no-op.
    assert lane_fixer.resume_parent_for_completed_lane_fixer(conn, child_id) is False
    assert kb.get_task(conn, parent_id).status != "blocked"


def test_lane_fixer_resume_skips_when_current_park_fingerprint_differs(lane_parent):
    """B4: fixer F1 must not resume a later park for different paths (fp Y)."""
    conn, parent, info = lane_parent
    parent_id = parent["id"]
    paths_x = ["web/src/control/Panel.tsx"]
    child_x = lane_fixer.maybe_route_lane_scope_fixer(
        conn,
        parent,
        violating_paths=paths_x,
        expected_lane="coder-frontend",
        now=100,
    )
    assert child_x

    # Record the original park event for fingerprint X, then re-park for Y.
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            parent_id,
            "worker_gate_blocked",
            {
                "gate": "lane_scope",
                "violating_paths": paths_x,
                "expected_lane": "coder-frontend",
            },
        )
    kb.claim_task(conn, parent_id)
    reason_y = (
        "integration parked: lane-scope violation: assignee 'coder' changed "
        "paths outside its lane: hermes_cli/cli.py — this task should "
        "have been assigned to lane 'coder'."
    )
    assert kb.block_task(conn, parent_id, reason=reason_y, kind="integration")
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            parent_id,
            "worker_gate_blocked",
            {
                "gate": "lane_scope",
                "violating_paths": ["hermes_cli/cli.py"],
                "expected_lane": "coder",
            },
        )

    # F1 completes while parent is parked for Y → must not resume.
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (child_x,))
    assert lane_fixer.resume_parent_for_completed_lane_fixer(conn, child_x) is False
    assert kb.get_task(conn, parent_id).status == "blocked"
    assert not _events(conn, parent_id, lane_fixer.LANE_FIXER_PARENT_RESUMED_EVENT)


def test_allowlisted_paths_for_parent_only_includes_done_fixer(lane_parent):
    conn, parent, _ = lane_parent
    parent_id = parent["id"]
    paths = ["web/src/control/Panel.tsx", "web/src/control/App.tsx"]
    child_id = lane_fixer.maybe_route_lane_scope_fixer(
        conn,
        parent,
        violating_paths=paths,
        expected_lane="coder-frontend",
        now=200,
    )
    assert child_id
    # Legacy call without current-violation binding: empty until done, then
    # returns the done-fixer set (tests that do not pass violating_paths).
    assert lane_fixer.allowlisted_paths_for_parent(conn, parent_id) == set()

    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (child_id,))
    assert lane_fixer.allowlisted_paths_for_parent(conn, parent_id) == set(paths)

    # Fingerprint-bound call without a matching current park → empty (B5).
    assert lane_fixer.allowlisted_paths_for_parent(
        conn,
        parent_id,
        violating_paths=paths,
        expected_lane="coder-frontend",
    ) == set()

    # Matching park event unlocks the allowlist for that fingerprint only.
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            parent_id,
            "worker_gate_blocked",
            {
                "gate": "lane_scope",
                "violating_paths": paths,
                "expected_lane": "coder-frontend",
            },
        )
    assert lane_fixer.allowlisted_paths_for_parent(
        conn,
        parent_id,
        violating_paths=paths,
        expected_lane="coder-frontend",
    ) == set(paths)


# --- mutation-hardening tests (night-run 2026-07-29) ---


def test_event_payloads_parses_real_payload(lane_parent):
    """Kill bool_op_swap L54: or "{}" -> and "{}" in _event_payloads."""
    conn, parent, info = lane_parent
    parent_id = parent["id"]
    fp = "test-fingerprint-abc"
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            parent_id,
            lane_fixer.LANE_FIXER_DISPATCHED_EVENT,
            {"fingerprint": fp, "extra": "data"},
        )
    result = lane_fixer._event_payloads(conn, parent_id, fp)
    assert len(result) == 1
    assert result[0][1]["fingerprint"] == fp
    assert result[0][1]["extra"] == "data"


def test_lane_fingerprint_from_scope_payload_returns_fingerprint():
    """Kill bool_op_swap L93: or "" -> and "" on expected_lane."""
    payload = {
        "violating_paths": ["a.py", "b.py"],
        "expected_lane": "coder",
    }
    fp = lane_fixer._lane_fingerprint_from_scope_payload(payload)
    assert fp != ""
    assert isinstance(fp, str)


def test_resume_tip_for_fingerprint_returns_tip(lane_parent):
    """Kill bool_op_swap L147 and L149: or "" -> and "" on fingerprint/tip."""
    conn, parent, info = lane_parent
    parent_id = parent["id"]
    fp = "resume-fp-xyz"
    tip_sha = "abc123def456"
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            parent_id,
            lane_fixer.LANE_FIXER_PARENT_RESUMED_EVENT,
            {"fingerprint": fp, "resume_branch_tip": tip_sha},
        )
    result = lane_fixer._resume_tip_for_fingerprint(conn, parent_id, fp)
    assert result == tip_sha
