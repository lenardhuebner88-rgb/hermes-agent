"""Tests for ``hermes kanban close-sprint`` and the underlying
``kanban_close_sprint`` module.

The sprint-closure pattern (memory: ``hermes-sprint-closure-pattern``)
sets a comprehensive SPRINT CLOSURE comment on a decomposed parent
before completing it, so kid receipts survive scratch-workspace
cleanup and DB recovery. This helper automates the pattern.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_close_sprint as kcs


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


def _make_sprint(*, n_kids: int = 2, mark_done: bool = True, with_runs: bool = True) -> tuple[str, list[str]]:
    """Create a parent + N kids and (optionally) close kid runs to feed
    the closer's aggregation logic. Returns ``(parent_id, kid_ids)``.
    """
    with kb.connect() as conn:
        parent = kb.create_task(
            conn, title="hardening sprint test", body="root", assignee="orchestrator",
        )
        kids: list[str] = []
        for i in range(n_kids):
            kid = kb.create_task(
                conn,
                title=f"kid {i}",
                body=f"do thing {i}",
                assignee=("coder" if i % 2 == 0 else "reviewer"),
                parents=[parent],
            )
            kids.append(kid)

        # Insert closed runs for each kid so list_runs returns something.
        if with_runs:
            now = int(time.time())
            for kid in kids:
                conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, "
                    "started_at, ended_at, outcome, summary, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        kid,
                        "coder",
                        "ended",
                        now - 90,
                        now - 10,
                        "completed",
                        f"finished {kid} cleanly",
                        json.dumps({
                            "artifacts": [f"/tmp/hermes-closure-test/{kid}.md"],
                        }),
                    ),
                )

        if mark_done:
            for kid in kids:
                conn.execute(
                    "UPDATE tasks SET status='done' WHERE id=?", (kid,),
                )
        conn.commit()
    return parent, kids


# ---------------------------------------------------------------------------
# build_closure_comment
# ---------------------------------------------------------------------------


def test_build_closure_comment_contains_required_anchors(kanban_home):
    parent, kids = _make_sprint(n_kids=2)
    with kb.connect() as conn:
        body, receipts = kcs.build_closure_comment(conn, parent)

    assert "SPRINT CLOSURE" in body
    assert "KID RECEIPTS:" in body
    assert "DELIVERY:" in body
    for kid in kids:
        assert kid in body
    # The aggregated artifact paths from runs.metadata.artifacts must appear.
    for kid in kids:
        assert f"/tmp/hermes-closure-test/{kid}.md" in body
    assert len(receipts) == 2
    assert all(r.status == "done" for r in receipts)


def test_build_closure_comment_unknown_parent(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError):
            kcs.build_closure_comment(conn, "t_does_not_exist")


def test_build_closure_comment_no_kids_still_works(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="solo task")
    with kb.connect() as conn:
        body, receipts = kcs.build_closure_comment(conn, parent)
    assert "SPRINT CLOSURE" in body
    assert "no kids linked" in body.lower()
    assert receipts == []


def test_build_closure_comment_runtime_formatted(kanban_home):
    parent, kids = _make_sprint(n_kids=1)
    with kb.connect() as conn:
        body, _ = kcs.build_closure_comment(conn, parent)
    # Run was 80 seconds → "1m20s"
    assert "1m20s" in body


# ---------------------------------------------------------------------------
# close_sprint (the main entry)
# ---------------------------------------------------------------------------


def test_close_sprint_sets_comment_and_completes_parent(kanban_home):
    parent, kids = _make_sprint(n_kids=2)
    with kb.connect() as conn:
        outcome = kcs.close_sprint(conn, parent, author="tester")

    assert outcome.ok
    assert outcome.completed
    assert outcome.comment_id is not None
    assert outcome.comment_body is not None
    assert "SPRINT CLOSURE" in outcome.comment_body
    for kid in kids:
        assert kid in outcome.comment_body

    with kb.connect() as conn:
        parent_task = kb.get_task(conn, parent)
        comments = kb.list_comments(conn, parent)
    assert parent_task.status == "done"
    assert any("SPRINT CLOSURE" in c.body for c in comments)


def test_close_sprint_refuses_open_kids_by_default(kanban_home):
    parent, kids = _make_sprint(n_kids=2, mark_done=False)
    with kb.connect() as conn:
        outcome = kcs.close_sprint(conn, parent, author="tester")
    assert outcome.ok is False
    assert "still open" in outcome.reason
    # The receipts must still be returned so the operator can see what's
    # blocking the closure.
    assert outcome.kid_receipts is not None
    assert len(outcome.kid_receipts) == 2

    with kb.connect() as conn:
        parent_task = kb.get_task(conn, parent)
    assert parent_task.status != "done"


def test_close_sprint_allow_open_kids_overrides_guard(kanban_home):
    parent, _ = _make_sprint(n_kids=2, mark_done=False)
    with kb.connect() as conn:
        outcome = kcs.close_sprint(
            conn, parent, author="tester", require_kids_done=False,
        )
    assert outcome.ok
    assert outcome.completed


def test_close_sprint_refuses_already_terminal_parent(kanban_home):
    parent, _ = _make_sprint(n_kids=1)
    with kb.connect() as conn:
        kb.complete_task(conn, parent, summary="manual close")
        outcome = kcs.close_sprint(conn, parent, author="tester")
    assert outcome.ok is False
    assert "already terminal" in outcome.reason


def test_close_sprint_uses_comment_override(kanban_home):
    parent, _ = _make_sprint(n_kids=1)
    custom = (
        "SPRINT CLOSURE\n\n"
        "Operator-written body. Skipping auto-aggregation entirely."
    )
    with kb.connect() as conn:
        outcome = kcs.close_sprint(
            conn, parent, author="tester", comment_override=custom,
        )
    assert outcome.ok and outcome.completed
    assert outcome.comment_body == custom

    with kb.connect() as conn:
        comments = kb.list_comments(conn, parent)
    assert comments[-1].body == custom


def test_close_sprint_unknown_parent(kanban_home):
    with kb.connect() as conn:
        outcome = kcs.close_sprint(conn, "t_nope", author="tester")
    assert outcome.ok is False
    assert "unknown parent" in outcome.reason


def test_close_sprint_completion_summary_lifts_first_line(kanban_home):
    parent, _ = _make_sprint(n_kids=1)
    with kb.connect() as conn:
        outcome = kcs.close_sprint(conn, parent, author="tester")
    assert outcome.ok and outcome.completed

    with kb.connect() as conn:
        run_after = kb.latest_run(conn, parent)
    # complete_task creates / closes a run; summary should be a sentence
    # from the closure body.
    if run_after is not None:
        # The lift-from-comment heuristic produces a non-empty summary.
        assert run_after.summary


# ---------------------------------------------------------------------------
# CLI surface — parser + dispatch
# ---------------------------------------------------------------------------


def _parse_kanban(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser.parse_args(["kanban", *argv])


def test_close_sprint_help_exposes_flags():
    ns = _parse_kanban(["close-sprint", "t_abc", "--auto-summary"])
    assert ns.task_id == "t_abc"
    assert ns.auto_summary is True
    assert ns.comment_file is None
    assert ns.dry_run is False


def test_close_sprint_help_with_comment_file_path():
    ns = _parse_kanban(["close-sprint", "t_abc", "--comment", "/tmp/notes.md"])
    assert ns.comment_file == "/tmp/notes.md"
    assert ns.auto_summary is False


def test_close_sprint_cli_dry_run_prints_body(kanban_home, capsys):
    parent, kids = _make_sprint(n_kids=2)
    ns = argparse.Namespace(
        task_id=parent,
        auto_summary=False,
        comment_file=None,
        result=None,
        summary=None,
        allow_open_kids=False,
        dry_run=True,
        json=False,
    )
    rc = kc._cmd_close_sprint(ns)
    assert rc == 0
    out = capsys.readouterr().out
    assert "SPRINT CLOSURE" in out
    for kid in kids:
        assert kid in out

    # Dry-run must NOT have completed the parent or added a comment.
    with kb.connect() as conn:
        parent_task = kb.get_task(conn, parent)
        comments = kb.list_comments(conn, parent)
    assert parent_task.status != "done"
    assert comments == []


def test_close_sprint_cli_end_to_end(kanban_home, capsys):
    parent, kids = _make_sprint(n_kids=2)
    ns = argparse.Namespace(
        task_id=parent,
        auto_summary=False,
        comment_file=None,
        result=None,
        summary=None,
        allow_open_kids=False,
        dry_run=False,
        json=True,
    )
    rc = kc._cmd_close_sprint(ns)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["completed"] is True
    assert payload["kid_count"] == 2
    assert "SPRINT CLOSURE" in payload["comment_body"]
    assert all(kid in payload["comment_body"] for kid in kids)


def test_close_sprint_cli_refuses_conflicting_flags(kanban_home, capsys):
    parent, _ = _make_sprint(n_kids=1)
    ns = argparse.Namespace(
        task_id=parent,
        auto_summary=True,
        comment_file="/tmp/x.md",
        result=None,
        summary=None,
        allow_open_kids=False,
        dry_run=False,
        json=False,
    )
    rc = kc._cmd_close_sprint(ns)
    assert rc == 2
    err = capsys.readouterr().err
    assert "mutually exclusive" in err


def test_read_comment_file_round_trip(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("  closure notes here  \n", encoding="utf-8")
    assert kcs.read_comment_file(str(p)) == "closure notes here"
