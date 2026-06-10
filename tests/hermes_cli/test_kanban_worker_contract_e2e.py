"""End-to-end coverage for the worker-contract handoff (commit 606070539).

The spawn prompt + KANBAN_GUIDANCE require a dispatched worker to, in order:
  1. post its end RESULT as a self-contained Markdown comment, then
  2. ``complete --metadata '<json>'`` with a structured handoff carrying
     ``residual_risk`` (+ optional ``changed_files`` / ``artifacts``).

The individual pieces (comment storage, metadata round-trip, artifacts→event)
are unit-tested elsewhere, but nothing asserted the FULL contract as one flow:
deliverable comment AND structured handoff surviving together on a completed
task. This test locks that contract as an executable spec so a future prompt
or completion-path change that silently drops either half is caught.
"""

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
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


DELIVERABLE = (
    "## Result\n\n"
    "Added an env allowlist to the worker spawn path. The dispatcher no "
    "longer forwards Discord tokens or the gateway API key to workers.\n\n"
    "- changed `_default_spawn` to use `_build_worker_env`\n"
    "- added regression tests\n"
)
HANDOFF = {
    "residual_risk": "A rarely-used lane key not in the allowlist could be dropped.",
    "changed_files": ["hermes_cli/kanban_db.py"],
    "tests_run": 5,
    "artifacts": ["/tmp/report.md"],
}


def test_worker_contract_deliverable_and_handoff_round_trip(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="harden workers", assignee="coder")
        kb.claim_task(conn, tid)
        # Step 1 of the contract: post the deliverable as a Markdown comment.
        kb.add_comment(conn, tid, author="coder", body=DELIVERABLE)
        # Step 2: complete with the structured handoff.
        kb.complete_task(
            conn, tid,
            summary="worker env hardened",
            metadata=HANDOFF,
        )

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        comments = kb.list_comments(conn, tid)

    # Task reached a terminal state.
    assert task.status == "done"

    # The deliverable comment survives verbatim and is the substantial
    # last comment (this is what the Discord receipt renders).
    bodies = [c.body for c in comments]
    assert DELIVERABLE.strip() in bodies
    last_substantial = [b for b in bodies if len(b) >= 200]
    assert last_substantial, "deliverable comment (>=200 chars) not found"

    # The structured handoff persisted on the closing run, residual_risk and
    # all, and is real JSON (not a stringified blob).
    assert run.summary == "worker env hardened"
    assert isinstance(run.metadata, dict)
    assert run.metadata["residual_risk"] == HANDOFF["residual_risk"]
    assert run.metadata["changed_files"] == ["hermes_cli/kanban_db.py"]
    assert run.metadata["tests_run"] == 5


def test_worker_contract_artifacts_promoted_to_completed_event(kanban_home):
    """`artifacts` in the handoff are promoted onto the completed event so the
    dashboard/receipt can surface preserved files (commit 606070539 relies on
    this to show what the worker kept from the deleted scratch workspace).
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="with artifacts", assignee="coder")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="ok", metadata=HANDOFF)
        events = kb.list_events(conn, tid)

    completed = [e for e in events if e.kind == "completed"]
    assert completed, "no completed event emitted"
    payload = completed[-1].payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload.get("artifacts") == ["/tmp/report.md"], (
        f"artifacts not promoted to completed event: {payload}"
    )
