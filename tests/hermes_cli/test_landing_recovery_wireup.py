from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli.landing_loop import (
    FailureClass,
    LL2Candidate,
    request_candidate_recovery,
)


def test_candidate_recovery_wireup_is_idempotent_across_restart(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="landing candidate", assignee="coder")

    candidate = LL2Candidate(
        task_or_branch_id=f"loop/{task_id}",
        candidate_commit="a" * 40,
        failing_gate="affected",
        failure_class=FailureClass.CANDIDATE_REGRESSION,
    )

    assert request_candidate_recovery(candidate) == "requested"
    assert request_candidate_recovery(candidate) == "deduplicated"

    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "AND kind = 'landing_recovery_requested'",
            (task_id,),
        ).fetchall()
    assert len(rows) == 1


def test_pack_branch_recovery_creates_one_task_and_requests(kanban_home):
    candidate = LL2Candidate(
        task_or_branch_id="loop/error-sweep",
        candidate_commit="b" * 40,
        failing_gate="merge",
        failure_class=FailureClass.MERGE_CONFLICT,
    )

    assert request_candidate_recovery(candidate) == "requested"
    assert request_candidate_recovery(candidate) == "deduplicated"

    with kb.connect() as conn:
        tasks = conn.execute(
            "SELECT id, idempotency_key, title, body, skills FROM tasks "
            "WHERE idempotency_key LIKE 'landing-recovery:%'"
        ).fetchall()
        assert len(tasks) == 1
        assert tasks[0]["idempotency_key"].startswith(
            "landing-recovery:loop/error-sweep:"
        )
        assert "loop/error-sweep" in tasks[0]["title"]
        assert "git rebase main" in tasks[0]["body"]
        assert "scripts/run-affected.sh main" in tasks[0]["body"]
        assert "Pack-Lock" in tasks[0]["body"]
        import json

        assert json.loads(tasks[0]["skills"]) == ["loop-branch-repair"]
        rows = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? "
            "AND kind = 'landing_recovery_requested'",
            (tasks[0]["id"],),
        ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["failure_class"] == "merge_conflict"
    assert payload["failing_gate"] == "merge"
    assert "Rebase" in payload["blocking_findings"][0]


def test_terminal_recovery_card_never_absorbs_a_new_request(kanban_home):
    # GLEICHER Fingerprint, Karte auf done: eine neue Karte muss entstehen.
    # (Mutationssicher: ohne den Terminal-Filter hängt der Request an der
    # done-Karte und das zweite Ergebnis waere "deduplicated".)
    candidate = LL2Candidate(
        task_or_branch_id="loop/error-sweep",
        candidate_commit="b" * 40,
        failing_gate="merge",
        failure_class=FailureClass.MERGE_CONFLICT,
    )
    assert request_candidate_recovery(candidate) == "requested"
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key LIKE 'landing-recovery:%'"
        ).fetchone()
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (row["id"],))
        conn.commit()

    assert request_candidate_recovery(candidate) == "requested"

    with kb.connect() as conn:
        cards = conn.execute(
            "SELECT id, status FROM tasks "
            "WHERE idempotency_key LIKE 'landing-recovery:%'"
        ).fetchall()
        open_cards = [
            card for card in cards if card["status"] not in ("done", "archived")
        ]
        assert len(cards) == 2
        assert len(open_cards) == 1
        rows = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "AND kind = 'landing_recovery_requested'",
            (open_cards[0]["id"],),
        ).fetchall()
    assert len(rows) == 1
