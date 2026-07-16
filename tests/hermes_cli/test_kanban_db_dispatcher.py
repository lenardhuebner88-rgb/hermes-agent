"""Kanban DB tests: dispatcher.

Split from test_kanban_db.py (pure move; no test logic changes).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import types
import unittest.mock
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb

from tests.hermes_cli._kanban_test_helpers import (
    _operator_escalations,
)

def test_dispatch_treats_openclaw_assignee_as_nonspawnable(kanban_home):
    """Native dispatcher must fail closed for legacy OpenClaw lanes.

    Even if stale DB rows mention ``openclaw:<agent>``, an autonomous Gateway
    tick must not sign or submit Mission-Control envelopes.
    """
    assert not hasattr(kb, "_dispatch_to_openclaw")
    assert not hasattr(kb, "poll_openclaw_results")
    conn = kb.connect()
    with conn:
        tid = kb.create_task(conn, title="legacy lane", assignee="openclaw:lens")
        res = kb.dispatch_once(conn, dry_run=False)

    assert tid in res.skipped_nonspawnable
    assert not res.spawned
    task = kb.get_task(conn, tid)
    assert task.status == "ready"
    events = [e.kind for e in kb.list_events(conn, tid)]
    assert "nonspawnable" in events


def test_dispatch_dry_run_does_not_claim(kanban_home, all_assignees_spawnable):
    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="a", assignee="alice")
        t2 = kb.create_task(conn, title="b", assignee="bob")
        res = kb.dispatch_once(conn, dry_run=True)
    assert {s[0] for s in res.spawned} == {t1, t2}
    with kb.connect_closing() as conn:
        # Dry run must NOT mutate status.
        assert kb.get_task(conn, t1).status == "ready"
        assert kb.get_task(conn, t2).status == "ready"


def test_dispatch_skips_unassigned(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="floater")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_unassigned
    assert t not in res.skipped_nonspawnable
    assert not res.spawned


def test_dispatch_skips_nonspawnable_into_separate_bucket(kanban_home, monkeypatch):
    """Tasks whose assignee fails profile_exists() must NOT land in
    ``skipped_unassigned`` (which is operator-actionable) — they go in
    the dedicated ``skipped_nonspawnable`` bucket so health telemetry
    can suppress false-positive "stuck" warnings."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="for-terminal", assignee="orion-cc")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_nonspawnable
    assert t not in res.skipped_unassigned
    assert not res.spawned


def test_has_spawnable_ready_false_when_only_terminal_lanes(kanban_home, monkeypatch):
    """``has_spawnable_ready`` returns False when every ready task is
    assigned to a control-plane lane — used by gateway/CLI dispatchers
    to silence the stuck-warn while terminals still have queued work."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="t1", assignee="orion-cc")
        kb.create_task(conn, title="t2", assignee="orion-research")
        assert kb.has_spawnable_ready(conn) is False


def test_has_spawnable_ready_true_when_real_profile_present(kanban_home, monkeypatch):
    """``has_spawnable_ready`` returns True as soon as ANY ready task
    has an assignee that maps to a real Hermes profile — preserves the
    real "stuck" signal when a daily/agent task is queued."""
    from hermes_cli import profiles
    monkeypatch.setattr(
        profiles, "profile_exists", lambda name: name == "daily"
    )
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="terminal-task", assignee="orion-cc")
        kb.create_task(conn, title="hermes-task", assignee="daily")
        assert kb.has_spawnable_ready(conn) is True


def test_has_spawnable_ready_false_on_empty_queue(kanban_home):
    """Empty queue is the trivial false case — no ready tasks at all."""
    with kb.connect_closing() as conn:
        assert kb.has_spawnable_ready(conn) is False


def test_dispatch_promotes_ready_and_spawns(kanban_home, all_assignees_spawnable):
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append((task.id, task.assignee, workspace))

    with kb.connect_closing() as conn:
        p = kb.create_task(conn, title="p", assignee="alice")
        c = kb.create_task(conn, title="c", assignee="bob", parents=[p])
        # Finish parent outside dispatch; promotion happens inside.
        kb.complete_task(conn, p)
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
    # Spawned c (a was already done when dispatch was called).
    assert len(spawns) == 1
    assert spawns[0][0] == c
    assert spawns[0][1] == "bob"
    # c is now running
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, c).status == "running"


def test_dispatch_spawn_failure_releases_claim(kanban_home, all_assignees_spawnable):
    def boom(task, workspace):
        raise RuntimeError("spawn failed")

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="boom", assignee="alice")
        kb.dispatch_once(conn, spawn_fn=boom)
        # Must return to ready so the next tick can retry.
        assert kb.get_task(conn, t).status == "ready"
        assert kb.get_task(conn, t).claim_lock is None


def test_dispatch_holds_reviewer_role_execution_mismatch(
    kanban_home, all_assignees_spawnable
):
    """K3: a reviewer task that asks the verdict-only lane to run repo gates is
    HELD at dispatch (not spawned) and left in ``ready`` for re-shaping."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append((task.id, task.assignee))

    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn,
            title="Review the gate output",
            assignee="reviewer",
            body="Bitte führe reale gates aus und run pytest im Repo.",
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

        # Not spawned; stays ready (advisory hold, NOT blocked).
        assert spawns == []
        assert all(s[0] != t for s in res.spawned)
        held_ids = [tid for tid, _ in res.held_role_mismatch]
        assert t in held_ids
        assert kb.get_task(conn, t).status == "ready"
        # Operator-visible diagnosis event was emitted.
        kinds = [e.kind for e in kb.list_events(conn, t)]
        assert "role_fit_held" in kinds


def test_dispatch_role_fit_held_event_is_deduped_across_ticks(
    kanban_home, all_assignees_spawnable
):
    """F2: a held reviewer task is re-evaluated every dispatch tick, but the
    ``role_fit_held`` diagnosis event is emitted only once while the hold
    state is unchanged — the hold itself stays reported every tick."""
    def fake_spawn(task, workspace):  # noqa: ARG001 - never invoked for a held task
        raise AssertionError("held task must not spawn")

    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn,
            title="Review the gate output",
            assignee="reviewer",
            body="Bitte führe reale gates aus und run pytest im Repo.",
        )

        res1 = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        res2 = kb.dispatch_once(conn, spawn_fn=fake_spawn)

        # Hold behaviour is byte-identical every tick (still reported, ready).
        assert t in [tid for tid, _ in res1.held_role_mismatch]
        assert t in [tid for tid, _ in res2.held_role_mismatch]
        assert kb.get_task(conn, t).status == "ready"

        # But the diagnosis event fired exactly once across the two ticks.
        held_events = [
            e for e in kb.list_events(conn, t) if e.kind == "role_fit_held"
        ]
        assert len(held_events) == 1


def test_dispatch_spawns_verdict_only_reviewer(
    kanban_home, all_assignees_spawnable
):
    """K3: a verdict-only reviewer task is exempt from the role-fit hold and
    dispatches normally even though it mentions gates."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append((task.id, task.assignee))

    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn,
            title="Verdict over parent evidence",
            assignee="reviewer",
            body=(
                "Verdict-only: prüfe die Parent-Belege und gib ein Verdict ab. "
                "Do not run tests selbst."
            ),
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

        assert (t, "reviewer") in spawns
        assert res.held_role_mismatch == []
        assert kb.get_task(conn, t).status == "running"


def test_dispatch_auto_retry_blocked_is_opt_in(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="transient MCP unavailable")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, max_spawn=0)

        assert res.auto_retried_blocked == []
        row = conn.execute(
            "SELECT status, auto_retry_count FROM tasks WHERE id = ?", (t,),
        ).fetchone()
        assert row["status"] == "blocked"
        assert row["auto_retry_count"] == 0


def test_dispatch_auto_retries_blocked_after_backoff_with_feedback_comment(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="transient MCP unavailable")

        monkeypatch.setattr(kb.time, "time", lambda: base + 299)
        early = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)
        assert early.auto_retried_blocked == []
        assert kb.get_task(conn, t).status == "blocked"

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == [(t, 1)]
        row = conn.execute(
            "SELECT status, auto_retry_count, model_override FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["auto_retry_count"] == 1
        assert row["model_override"] is None
        comments = kb.list_comments(conn, t)
        assert comments[-1].author == "dispatcher"
        assert "transient MCP unavailable" in comments[-1].body
        events = [e for e in kb.list_events(conn, t) if e.kind == "auto_retried"]
        assert len(events) == 1
        assert events[0].payload["attempt"] == 1


def test_dispatch_auto_retry_allows_first_request_changes_block(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="coder", body="AC v1")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Verifier found which assertion is missing")
        conn.execute(
            "UPDATE task_runs SET verdict = 'REQUEST_CHANGES' "
            "WHERE task_id = ? AND outcome = 'blocked'",
            (t,),
        )

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == [(t, 1)]
        row = conn.execute(
            "SELECT status, auto_retry_count FROM tasks WHERE id = ?", (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["auto_retry_count"] == 1


def test_dispatch_auto_retry_keeps_operator_hold_blocked(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="coder")
        kb.claim_task(conn, t)
        assert kb.hold_task(conn, t, reason="operator hold") is True

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        assert kb.get_task(conn, t).status == "blocked"
        event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retry_skipped"][-1]
        # hold_task stamps block_kind=needs_input (typed system park); that is
        # the authoritative skip signal (not the prose question-regex).
        assert event.payload["blocked_kind"] == "needs_input"
        assert kb.get_task(conn, t).block_kind == "needs_input"


def test_operator_hold_never_duplicates_run_and_answer_resumes_once(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    clock = {"now": base}
    monkeypatch.setattr(kb.time, "time", lambda: clock["now"])
    spawned: list[str] = []

    def fake_spawn(task, workspace):
        spawned.append(task.id)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="choose an authorized release scope",
            assignee="coder",
            body="Operator must choose before the implementation continues.",
        )
        assert kb.claim_task(conn, task_id) is not None
        assert kb.hold_task(
            conn,
            task_id,
            reason="REQUEST_CHANGES — operator must choose the allowed scope",
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == 1

        for tick_at in (base + 301, base + 602):
            clock["now"] = tick_at
            result = kb.dispatch_once(
                conn,
                spawn_fn=fake_spawn,
                auto_retry_blocked=True,
                max_spawn=1,
            )
            assert result.auto_retried_blocked == []
            assert result.spawned == []
        assert spawned == []
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == 1

        assert kb.answer_operator_question(
            conn,
            task_id,
            answer="Proceed locally without any remote push.",
        ) == "ready"
        clock["now"] = base + 603
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=1)

        assert [item[0] for item in result.spawned] == [task_id]
        assert spawned == [task_id]
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == 2


def test_dispatch_auto_retry_harmless_prose_without_verdict_is_retryable(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="coder")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Worker noted which assertion failed")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == [(t, 1)]
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_auto_retry_escalates_repeated_request_changes_on_unchanged_body(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="coder", body="AC v1")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Verifier found issue one")
        conn.execute(
            "UPDATE task_runs SET verdict = 'REQUEST_CHANGES' "
            "WHERE task_id = ? AND outcome = 'blocked'",
            (t,),
        )

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        monkeypatch.setattr(kb.time, "time", lambda: base + 302)
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Verifier found issue two")
        conn.execute(
            "UPDATE task_runs SET verdict = 'REQUEST_CHANGES' "
            "WHERE task_id = ? AND outcome = 'blocked' AND verdict IS NULL",
            (t,),
        )

        monkeypatch.setattr(kb.time, "time", lambda: base + 603)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        row = conn.execute(
            "SELECT status, auto_retry_count, assignee, model_override "
            "FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        assert row["status"] == "blocked"
        assert row["auto_retry_count"] == 1
        assert row["assignee"] == "coder"
        assert row["model_override"] is None
        comments = kb.list_comments(conn, t)
        assert comments[-1].author == "dispatcher"
        assert "Verifier-Content-Block nach Retry auf unverändertem Body" in comments[-1].body
        event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retry_skipped"][-1]
        assert event.payload["blocked_kind"] == "needs_operator"


def test_dispatch_auto_retry_retries_transient_second_block_even_when_body_unchanged(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="research", body="AC v1")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="transient MCP unavailable")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        monkeypatch.setattr(kb.time, "time", lambda: base + 302)
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="tool crashed")

        monkeypatch.setattr(kb.time, "time", lambda: base + 603)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == [(t, 2)]
        row = conn.execute(
            "SELECT status, auto_retry_count, assignee FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["auto_retry_count"] == 2
        assert row["assignee"] == kb.AUTO_RETRY_ESCALATION_PROFILE


def test_dispatch_auto_retry_allows_request_changes_after_body_changes(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="coder", body="AC v1")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Verifier found issue one")
        conn.execute(
            "UPDATE task_runs SET verdict = 'REQUEST_CHANGES' "
            "WHERE task_id = ? AND outcome = 'blocked'",
            (t,),
        )

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", ("AC v2", t))
        monkeypatch.setattr(kb.time, "time", lambda: base + 302)
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Verifier found issue two")
        conn.execute(
            "UPDATE task_runs SET verdict = 'REQUEST_CHANGES' "
            "WHERE task_id = ? AND outcome = 'blocked' AND verdict IS NULL",
            (t,),
        )

        monkeypatch.setattr(kb.time, "time", lambda: base + 603)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == [(t, 2)]
        row = conn.execute(
            "SELECT status, auto_retry_count, assignee FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["auto_retry_count"] == 2
        assert row["assignee"] == kb.AUTO_RETRY_ESCALATION_PROFILE


def test_dispatch_auto_retry_second_attempt_escalates(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="research")
        conn.execute("UPDATE tasks SET auto_retry_count = 1 WHERE id = ?", (t,))
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="tool crashed")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == [(t, 2)]
        row = conn.execute(
            "SELECT status, auto_retry_count, assignee, model_override FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["auto_retry_count"] == 2
        assert row["assignee"] == kb.AUTO_RETRY_ESCALATION_PROFILE
        # Escalation model is stored as provider/model when the blocked run's
        # route provider is a different family (poison-pill prevention).
        assert row["model_override"] in {
            kb.AUTO_RETRY_ESCALATION_MODEL,
            f"anthropic/{kb.AUTO_RETRY_ESCALATION_MODEL}",
        }
        event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retried"][-1]
        assert event.payload["escalated"] is True
        assert event.payload["model_override"] == row["model_override"]


def test_dispatch_auto_retry_stops_after_limit(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="alice")
        conn.execute("UPDATE tasks SET auto_retry_count = 2 WHERE id = ?", (t,))
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="still broken")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        assert kb.get_task(conn, t).status == "blocked"
        assert [e.kind for e in kb.list_events(conn, t)].count("auto_retry_exhausted") == 1


def test_dispatch_auto_retry_leaves_question_blocks_untouched(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Which credential should I use?")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        assert kb.get_task(conn, t).status == "blocked"
        event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retry_skipped"][-1]
        assert event.payload["blocked_kind"] == "operator_question"


def test_dispatch_auto_retry_honors_explicit_needs_input_without_reason_keywords(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="tool crashed", kind="needs_input")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        task = kb.get_task(conn, t)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert task.auto_retry_count == 0
        event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retry_skipped"][-1]
        assert event.payload["blocked_kind"] == "needs_input"


def test_dispatch_auto_retry_honors_explicit_transient_over_reason_keywords(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(
            conn,
            t,
            reason="operator question while a temporary tool is unavailable?",
            kind="transient",
        )

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == [(t, 1)]
        task = kb.get_task(conn, t)
        assert task is not None
        assert task.status == "ready"
        assert task.block_kind is None
        assert task.auto_retry_count == 1


def test_dispatch_auto_retry_transient_survives_silent_sweep_before_backoff(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(
            conn,
            t,
            reason="Which credential should I use?",
            kind="transient",
        )

        monkeypatch.setattr(kb.time, "time", lambda: base + 60)
        before_backoff = kb.dispatch_once(
            conn, auto_retry_blocked=True, max_spawn=0
        )
        sweep = kb.escalate_silent_blocks_sweep(conn, now=base + 60)

        assert before_backoff.auto_retried_blocked == []
        assert sweep["escalated"] == []
        assert _operator_escalations(conn, t) == []

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        after_backoff = kb.dispatch_once(
            conn, auto_retry_blocked=True, max_spawn=0
        )

        assert after_backoff.auto_retried_blocked == [(t, 1)]
        assert kb.get_task(conn, t).status == "ready"


def test_silent_block_sweep_escalates_explicit_needs_input_immediately(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="tool crashed", kind="needs_input")

        sweep = kb.escalate_silent_blocks_sweep(conn, now=base)
        dispatch = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert [entry["task_id"] for entry in sweep["escalated"]] == [t]
        assert len(_operator_escalations(conn, t)) == 1
        assert dispatch.auto_retried_blocked == []
        assert kb.get_task(conn, t).status == "blocked"


def test_dispatch_auto_retry_leaves_secret_and_irreversible_blocks_untouched(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    reasons = [
        "Need secret token before continuing",
        "Please approve git push to origin/main",
        "Requires deploy after migration",
        "Need DB ALTER TABLE decision",
        "Freigabe zum Löschen fehlt",
    ]
    with kb.connect_closing() as conn:
        task_ids = []
        for reason in reasons:
            t = kb.create_task(conn, title=f"blocked {reason}", assignee="alice")
            kb.claim_task(conn, t)
            kb.block_task(conn, t, reason=reason)
            task_ids.append(t)

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        for t in task_ids:
            task = kb.get_task(conn, t)
            assert task is not None
            assert task.status == "blocked"
            event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retry_skipped"][-1]
            assert event.payload["blocked_kind"] == "operator_question"


def test_dispatch_auto_retry_respects_failure_breaker(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="crashy")
        conn.execute("UPDATE tasks SET consecutive_failures = 3 WHERE id = ?", (t,))

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(
            conn, auto_retry_blocked=True, failure_limit=3, max_spawn=0,
        )

        assert res.auto_retried_blocked == []
        assert kb.get_task(conn, t).status == "blocked"
        event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retry_skipped"][-1]
        assert event.payload["reason"] == "failure_limit"


def test_dispatch_auto_retry_completes_when_result_comment_arrived_after_block(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="research")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="MCP unreachable")
        monkeypatch.setattr(kb.time, "time", lambda: base + 60)
        kb.add_comment(conn, t, "research", "RESULT: full answer delivered here")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        task = kb.get_task(conn, t)
        assert task.status == "done"
        assert "full answer" in (task.result or "")
        event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retry_completed"][-1]
        assert event.payload["source"] == "result_comment"


def test_dispatch_auto_retry_result_comment_does_not_wait_for_backoff(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="research")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="MCP unreachable")
        monkeypatch.setattr(kb.time, "time", lambda: base + 60)
        kb.add_comment(conn, t, "research", "RESULT: complete answer arrived fast")

        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        task = kb.get_task(conn, t)
        assert task is not None
        assert task.status == "done"
        assert "complete answer" in (task.result or "")


def test_dispatch_auto_retry_needs_input_result_after_sweep_completes_immediately(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="research")
        kb.claim_task(conn, t)
        kb.block_task(
            conn,
            t,
            reason="waiting for clarification",
            kind="needs_input",
        )
        first_dispatch = kb.dispatch_once(
            conn, auto_retry_blocked=True, max_spawn=0
        )
        sweep = kb.escalate_silent_blocks_sweep(conn, now=base)

        assert first_dispatch.auto_retried_blocked == []
        assert [entry["task_id"] for entry in sweep["escalated"]] == [t]
        escalation = _operator_escalations(conn, t)[0]
        blocked_event = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'blocked' "
            "ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert escalation.payload["evidence"]["blocked_event_id"] == blocked_event["id"]

        kb.add_comment(conn, t, "research", "RESULT: full answer delivered here")

        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        task = kb.get_task(conn, t)
        assert task is not None
        assert task.status == "done"
        assert "full answer" in (task.result or "")
        event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retry_completed"][-1]
        assert event.payload is not None
        assert event.payload["source"] == "result_comment"


def test_dispatch_auto_retry_ignores_same_second_result_from_before_block(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="research")
        kb.add_comment(conn, t, "research", "RESULT: stale answer from before block")
        kb.claim_task(conn, t)
        kb.block_task(
            conn,
            t,
            reason="waiting for clarification",
            kind="needs_input",
        )
        kb.escalate_silent_blocks_sweep(conn, now=base)

        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        task = kb.get_task(conn, t)
        assert task is not None
        assert task.status == "blocked"
        assert not any(
            event.kind == "auto_retry_completed" for event in kb.list_events(conn, t)
        )


def test_dispatch_auto_retry_needs_input_respects_foreign_operator_hold(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="research")
        kb.claim_task(conn, t)
        kb.block_task(
            conn, t, reason="waiting for clarification", kind="needs_input"
        )
        kb._append_event(
            conn,
            t,
            kb.OPERATOR_ESCALATION_EVENT,
            {"why_now": "operator explicitly parked this task"},
        )
        kb.add_comment(conn, t, "research", "RESULT: arrived after foreign hold")

        kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert kb.get_task(conn, t).status == "blocked"
        assert not any(
            event.kind == "auto_retry_completed" for event in kb.list_events(conn, t)
        )


def test_dispatch_auto_retry_needs_input_requires_same_run_silent_escalation(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        foreign = kb.create_task(conn, title="foreign run", assignee="research")
        kb.claim_task(conn, foreign)
        foreign_run = conn.execute(
            "SELECT id FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (foreign,),
        ).fetchone()["id"]
        t = kb.create_task(conn, title="blocked", assignee="research")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="blocked episode", kind="needs_input")
        payload = kb._silent_block_escalation_payload(
            row=conn.execute("SELECT * FROM tasks WHERE id = ?", (t,)).fetchone(),
            reason="blocked episode",
            blocked_kind="needs_input",
        )
        kb._append_event(
            conn,
            t,
            kb.OPERATOR_ESCALATION_EVENT,
            payload,
            run_id=foreign_run,
        )
        kb.add_comment(conn, t, "research", "RESULT: arrived after foreign run hold")

        kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert kb.get_task(conn, t).status == "blocked"
        assert not any(
            event.kind == "auto_retry_completed" for event in kb.list_events(conn, t)
        )


def test_dispatch_auto_retry_needs_input_requires_same_silent_sweep_episode(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="research")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="blocked episode", kind="needs_input")
        kb.escalate_silent_blocks_sweep(conn, now=base)
        escalation = _operator_escalations(conn, t)[0]
        payload = escalation.payload
        payload["evidence"]["blocked_event_id"] += 1
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE id = ?",
            (json.dumps(payload), escalation.id),
        )
        kb.add_comment(conn, t, "research", "RESULT: arrived after stale sweep hold")

        kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert kb.get_task(conn, t).status == "blocked"
        assert not any(
            event.kind == "auto_retry_completed" for event in kb.list_events(conn, t)
        )


def test_dispatch_auto_retry_needs_input_legacy_watermark_fails_closed(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="research")
        kb.claim_task(conn, t)
        kb.block_task(
            conn, t, reason="waiting for clarification", kind="needs_input"
        )
        blocked_event = conn.execute(
            "SELECT id, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        payload = json.loads(blocked_event["payload"])
        payload.pop("comment_id_watermark")
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE id = ?",
            (json.dumps(payload), blocked_event["id"]),
        )
        kb.escalate_silent_blocks_sweep(conn, now=base)
        monkeypatch.setattr(kb.time, "time", lambda: base + 60)
        kb.add_comment(conn, t, "research", "RESULT: legacy late answer")

        kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert kb.get_task(conn, t).status == "blocked"
        assert not any(
            event.kind == "auto_retry_completed" for event in kb.list_events(conn, t)
        )


@pytest.mark.parametrize("invalid_watermark", [False, -1])
def test_dispatch_auto_retry_needs_input_invalid_watermark_fails_closed(
    kanban_home, all_assignees_spawnable, monkeypatch, invalid_watermark
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="research")
        kb.add_comment(conn, t, "research", "RESULT: stale answer from before block")
        kb.claim_task(conn, t)
        kb.block_task(
            conn, t, reason="waiting for clarification", kind="needs_input"
        )
        blocked_event = conn.execute(
            "SELECT id, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        payload = json.loads(blocked_event["payload"])
        payload["comment_id_watermark"] = invalid_watermark
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE id = ?",
            (json.dumps(payload), blocked_event["id"]),
        )
        kb.escalate_silent_blocks_sweep(conn, now=base)

        kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert kb.get_task(conn, t).status == "blocked"
        assert not any(
            event.kind == "auto_retry_completed" for event in kb.list_events(conn, t)
        )

