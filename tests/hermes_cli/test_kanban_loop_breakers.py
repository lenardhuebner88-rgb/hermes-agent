"""Regression coverage for the three bounded Kanban loop breakers."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

import hermes_cli.profiles as profiles_mod
from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated board initialized through the production DB helper."""
    home = tmp_path / ".hermes"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    assert home.resolve() in db_path.resolve().parents
    assert db_path.resolve() != Path("/home/piet/.hermes/kanban.db").resolve()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def review_gate(monkeypatch):
    config = {
        "enabled": True,
        "code_roles": frozenset({"coder", "premium"}),
        "acceptance_roles": frozenset(),
        "verifier_profile": "verifier",
        "review_profile": "reviewer",
        "critic_profile": "critic",
        "auto_tier": False,
        "standard_uses_llm_verifier": True,
        "judge_at_chain_tip": False,
        "critical_reviews_each_slice": True,
        "max_review_rounds": 2,
    }
    monkeypatch.setattr(kb, "_review_gate_config", lambda: dict(config))
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda _name: True)
    return config


def _event_payloads(conn, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"] or "{}") for row in rows]


def _route_rebase_conflict(conn, task_id: str) -> None:
    run_id = kb._current_run_id(conn, task_id)
    assert run_id is not None
    assert kb._route_rebase_conflict_to_coder(
        conn,
        task_id,
        {
            "action": "rebase_conflict",
            "branch": f"kanban/{task_id}",
            "target": "main",
            "reason": (
                f"rebase of kanban/{task_id} onto main hit a conflict "
                "(aborted, returned to coder)"
            ),
        },
        expected_run_id=run_id,
    )


def test_rebase_conflict_is_sticky_and_not_repromoted(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="conflicted merge", assignee="coder")
        assert kb.claim_task(conn, task_id) is not None

        _route_rebase_conflict(conn, task_id)

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "rebase_conflict"
        assert task.integration_retry_count == 1
        blocked = _event_payloads(conn, task_id, "blocked")
        assert blocked[-1]["kind"] == "rebase_conflict"

        kb.recompute_ready(conn)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, task_id).status == "blocked"


def test_third_rebase_conflict_is_terminal_and_escalates(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="repeated conflict", assignee="coder")
        assert kb.claim_task(conn, task_id) is not None

        for attempt in range(1, kb.INTEGRATION_RETRY_LIMIT + 2):
            _route_rebase_conflict(conn, task_id)
            if attempt <= kb.INTEGRATION_RETRY_LIMIT:
                retried = kb.auto_retry_blocked_tasks(conn, backoff_seconds=0)
                assert any(item[0] == task_id for item in retried)
                assert kb.claim_task(conn, task_id) is not None

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "integration"
        assert task.integration_retry_count == 3
        escalations = _event_payloads(conn, task_id, kb.OPERATOR_ESCALATION_EVENT)
        assert escalations[-1]["rule"] == "rebase_conflict_cap"


def _submit_review_rejection(conn, task_id: str, summary: str) -> None:
    assert kb.claim_review_task(conn, task_id) is not None
    assert kb.complete_task(
        conn,
        task_id,
        summary=summary,
        metadata={"review_verdict": "REQUEST_CHANGES"},
        review_gate=True,
    )


def _revise_and_resubmit(conn, task_id: str, summary: str) -> None:
    conn.execute(
        "UPDATE tasks SET body = COALESCE(body, '') || ? WHERE id = ?",
        (f"\nrevision marker: {summary}", task_id),
    )
    conn.commit()
    retried = kb.auto_retry_blocked_tasks(conn, backoff_seconds=0)
    assert any(item[0] == task_id for item in retried)
    assert kb.claim_task(conn, task_id) is not None
    assert kb.complete_task(conn, task_id, summary=summary, review_gate=True)


def test_review_round_cap_routes_to_needs_input(
    kanban_home, review_gate, monkeypatch
):
    config = dict(review_gate)
    config["max_review_rounds"] = 3
    monkeypatch.setattr(kb, "_review_gate_config", lambda: dict(config))

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="review ping-pong", assignee="coder")
        assert kb.claim_task(conn, task_id) is not None
        assert kb.complete_task(conn, task_id, summary="candidate 1", review_gate=True)

        for round_number in (1, 2):
            _submit_review_rejection(conn, task_id, f"changes round {round_number}")
            task = kb.get_task(conn, task_id)
            assert task.block_kind == "review_revision"
            assert task.block_recurrences == round_number
            _revise_and_resubmit(conn, task_id, f"candidate {round_number + 1}")

        _submit_review_rejection(conn, task_id, "changes round 3")
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert task.block_recurrences == 3
        escalations = _event_payloads(conn, task_id, kb.OPERATOR_ESCALATION_EVENT)
        assert escalations[-1]["rule"] == "review_pingpong"
        assert "3 REQUEST_CHANGES rounds" in escalations[-1]["reason"]


def _dead_worker_run(conn, task_id: str, pid: int, raw_status: int) -> None:
    host = kb._claimer_id().split(":", 1)[0]
    claimed = kb.claim_task(conn, task_id, claimer=f"{host}:w{pid}")
    assert claimed is not None
    conn.execute(
        "UPDATE tasks SET worker_pid = ? WHERE id = ?",
        (pid, task_id),
    )
    conn.execute(
        "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
        (pid, claimed.current_run_id),
    )
    conn.commit()
    kb._record_worker_exit(pid, raw_status)
    kb.detect_crashed_workers(conn)


def test_identical_fingerprint_respawns_trip_but_mixed_streak_does_not(
    kanban_home, monkeypatch
):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect_closing() as conn:
        # The deployed board has this legacy column; fresh test boards do not.
        # Adding it only to this isolated fixture proves the production stamp.
        conn.execute(
            "ALTER TABLE task_runs "
            "ADD COLUMN worker_failure_fingerprint TEXT"
        )
        conn.commit()

        # Identical crash fingerprints across dispatcher ticks trip the
        # streak breaker. max_retries lifts the per-task consecutive-failure
        # cap so the test isolates the streak path (the incident lanes never
        # incremented that counter at all).
        repeated = kb.create_task(conn, title="dead gateway", assignee="verifier")
        conn.execute(
            "UPDATE tasks SET max_retries = 99 WHERE id = ?", (repeated,)
        )
        conn.commit()
        for pid in (81001, 81002, 81003):
            _dead_worker_run(conn, repeated, pid, 1 << 8)

        task = kb.get_task(conn, repeated)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "dependency"
        escalation = _event_payloads(
            conn, repeated, kb.OPERATOR_ESCALATION_EVENT
        )[-1]
        assert escalation["rule"] == "respawn_streak"
        fingerprints = conn.execute(
            "SELECT worker_failure_fingerprint FROM task_runs "
            "WHERE task_id = ? AND outcome = 'crashed' ORDER BY id",
            (repeated,),
        ).fetchall()
        assert len(fingerprints) == 3
        assert len({row["worker_failure_fingerprint"] for row in fingerprints}) == 1
        assert fingerprints[0]["worker_failure_fingerprint"]

        mixed = kb.create_task(conn, title="mixed failures", assignee="verifier")
        conn.execute(
            "UPDATE tasks SET max_retries = 99 WHERE id = ?", (mixed,)
        )
        conn.commit()
        _dead_worker_run(conn, mixed, 82001, 1 << 8)
        _dead_worker_run(conn, mixed, 82002, 2 << 8)
        _dead_worker_run(conn, mixed, 82003, 1 << 8)
        mixed_task = kb.get_task(conn, mixed)
        assert mixed_task is not None
        assert mixed_task.status == "ready"
        assert not _event_payloads(conn, mixed, kb.OPERATOR_ESCALATION_EVENT)


def test_rate_limited_streak_never_trips_the_breaker(kanban_home, monkeypatch):
    """Quota walls self-heal via the respawn-guard cooldown; three identical
    rate-limited exits must requeue, not terminal-block (streak exemption)."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    rate_limited_status = kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="quota wall", assignee="verifier")
        for pid in (83001, 83002, 83003):
            _dead_worker_run(conn, task_id, pid, rate_limited_status)

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert not _event_payloads(conn, task_id, kb.OPERATOR_ESCALATION_EVENT)


def test_single_request_changes_still_uses_auto_retry_lane(
    kanban_home, review_gate
):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="one review fix", assignee="coder")
        assert kb.claim_task(conn, task_id) is not None
        assert kb.complete_task(conn, task_id, summary="candidate", review_gate=True)
        _submit_review_rejection(conn, task_id, "one fix needed")

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.block_kind == "review_revision"
        retried = kb.auto_retry_blocked_tasks(conn, backoff_seconds=0)
        assert retried == [(task_id, 1)]
        assert kb.get_task(conn, task_id).status == "ready"


def test_complete_cas_mismatch_is_explicit_and_nonzero(
    kanban_home, monkeypatch, capsys
):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="stale completion", assignee="coder")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        stale_run_id = int(claimed.current_run_id) + 1

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(stale_run_id))
    rc = kanban_cli._cmd_complete(
        argparse.Namespace(
            task_ids=[task_id],
            summary="stale result",
            metadata=None,
            result=None,
        )
    )

    assert rc != 0
    assert "NOT completed" in capsys.readouterr().err
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, task_id).status == "running"
