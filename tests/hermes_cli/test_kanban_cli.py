"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


def _review_efficiency_fixture(name: str) -> dict:
    path = Path(__file__).parent / "fixtures" / "review_efficiency_live_fixtures.json"
    return json.loads(path.read_text(encoding="utf-8"))[name]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("scratch",              ("scratch", None)),
        ("worktree",              ("worktree", None)),
        ("worktree:/tmp/wt",       ("worktree", "/tmp/wt")),
        ("dir:/tmp/work",         ("dir", "/tmp/work")),
    ],
)
def test_parse_workspace_flag_valid(value, expected):
    assert kc._parse_workspace_flag(value) == expected


def test_parse_workspace_flag_expands_user():
    kind, path = kc._parse_workspace_flag("dir:~/vault")
    assert kind == "dir"
    assert path.endswith("/vault")
    assert not path.startswith("~")

    kind, path = kc._parse_workspace_flag("worktree:~/trees/t6-wire")
    assert kind == "worktree"
    assert path.endswith("/trees/t6-wire")
    assert not path.startswith("~")

@pytest.mark.parametrize("bad", ["cloud", "dir:", "worktree:", ""])
def test_parse_workspace_flag_rejects(bad):
    if not bad:
        # Empty -> defaults; not an error.
        assert kc._parse_workspace_flag(bad) == ("scratch", None)
        return
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_workspace_flag(bad)


def test_parse_branch_flag_rejects_empty_and_option_like():
    assert kc._parse_branch_flag(None) is None
    assert kc._parse_branch_flag(" wt/t6-wire ") == "wt/t6-wire"
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_branch_flag("   ")
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_branch_flag("-bad")
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_branch_flag("bad branch")


# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------

def test_run_slash_no_args_shows_usage(kanban_home):
    out = kc.run_slash("")
    assert "kanban" in out.lower()
    assert "create" in out.lower() or "subcommand" in out.lower() or "action" in out.lower()


def test_run_slash_create_and_list(kanban_home):
    out = kc.run_slash("create 'ship feature' --assignee alice")
    assert "Created" in out
    out = kc.run_slash("list")
    assert "ship feature" in out
    assert "alice" in out


def test_run_slash_complete_freigabe_closes_live_fixture_hold(kanban_home):
    fixture = _review_efficiency_fixture("complete_freigabe")
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title=fixture["title"],
            body=f"Live fixture root {fixture['root_task_id']}",
            triage=True,
            freigabe=fixture["freigabe"],
            created_by=fixture["created_by"],
        )
        kids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="premium",
            children=[
                {
                    "title": child["title"],
                    "assignee": child["assignee"],
                }
                for child in fixture["children"]
            ],
            initial_child_status="scheduled",
            expected_root_status="triage",
        )
        assert kids is not None

    out = kc.run_slash(
        f"complete-freigabe {root} --author pytest --note '{fixture['note']}' --json"
    )
    payload = json.loads(out)
    assert payload == {"task_id": root, "completed": True, "author": "pytest"}

    with kb.connect() as conn:
        assert kb.get_task(conn, root).status == "archived"
        assert all(kb.get_task(conn, child).status == "archived" for child in kids)
        assert any(
            fixture["note"] in comment.body
            for comment in kb.list_comments(conn, root)
        )


def test_run_slash_create_worktree_path_and_branch(kanban_home, tmp_path):
    target = tmp_path / ".worktrees" / "t6-wire"
    target_arg = target.as_posix()
    out = kc.run_slash(
        f"create 'ship worktree' --workspace worktree:{target_arg} --branch wt/t6-wire"
    )
    assert "Created" in out

    with kb.connect() as conn:
        tasks = kb.list_tasks(conn)
    task = tasks[0]
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == target_arg
    assert task.branch_name == "wt/t6-wire"


def test_run_slash_rejects_branch_without_worktree(kanban_home):
    out = kc.run_slash("create 'bad branch' --workspace scratch --branch wt/bad")
    assert "--branch is only valid with --workspace worktree" in out


def test_run_slash_create_with_parent_and_cascade(kanban_home):
    # Parent then child via --parent
    out1 = kc.run_slash("create 'parent' --assignee alice")
    # Extract the "t_xxxx" id from "Created t_xxxx (ready, ...)"
    import re
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    p = m.group(1)
    out2 = kc.run_slash(f"create 'child' --assignee bob --parent {p}")
    assert "todo" in out2  # child starts as todo

    # Complete parent; list should promote child to ready
    kc.run_slash(f"complete {p}")
    # Explicit filter: child should now be ready (was todo before complete).
    ready_list = kc.run_slash("list --status ready")
    assert "child" in ready_list


def test_run_slash_show_includes_comments(kanban_home):
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'remember to include performance section'")
    show = kc.run_slash(f"show {tid}")
    assert "performance section" in show


def test_run_slash_comment_max_len_trims_long_body(kanban_home):
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} '{'x' * 30}' --max-len 20")
    show = kc.run_slash(f"show {tid}")
    assert "trimmed to 20 chars by --max-len" in show
    assert "x" * 30 not in show


def test_run_slash_comment_directive_sets_kind(kanban_home, monkeypatch):
    """`comment <tid> --directive` lands an operator directive (kind=directive)
    when run by an operator (no HERMES_KANBAN_TASK in env)."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'switch to plan B' --directive")
    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
    assert [c.kind for c in comments] == ["directive"]


def test_run_slash_comment_directive_rejected_inside_worker_cage(kanban_home, monkeypatch):
    """Cage model: a spawned worker (HERMES_KANBAN_TASK set) cannot grant
    itself an operator directive — the comment is rejected and nothing is
    written."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    result = kc.run_slash(f"comment {tid} 'self-granted directive' --directive")
    assert "operator-only" in result or "cannot be set" in result
    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
    assert comments == []


def test_run_slash_comment_without_directive_is_plain_comment(kanban_home, monkeypatch):
    """A worker may still post ordinary comments inside the cage — only
    --directive is gated."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    kc.run_slash(f"comment {tid} 'progress update from worker'")
    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
    assert [c.kind for c in comments] == ["comment"]


def test_run_slash_block_unblock_cycle(kanban_home):
    out = kc.run_slash("create 'x' --assignee alice")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    # Claim first so block() finds it running
    kc.run_slash(f"claim {tid}")
    assert "Blocked" in kc.run_slash(f"block {tid} 'need decision'")
    assert "Unblocked" in kc.run_slash(f"unblock {tid}")


def _seed_exhausted_budget_runaway(conn):
    task_id = kb.create_task(conn, title="runaway", assignee="alice")
    conn.execute(
        """
        UPDATE tasks
        SET status = 'blocked', block_kind = 'capacity', budget_extension_count = ?
        WHERE id = ?
        """,
        (kb.BUDGET_PROGRESS_EXTENSION_LIMIT, task_id),
    )
    conn.execute(
        """
        INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, input_tokens)
        VALUES (?, 'alice', 'blocked', 'blocked', 1, 2, ?)
        """,
        (task_id, 4_148_125),
    )
    # Match the production stream emitted by _park_budget_runaway(), rather
    # than a shortened test-only marker.
    kb._append_event(
        conn,
        task_id,
        "blocked",
        {
            "reason": "per-task input-token cap exceeded: 4148125 > 4000000 "
            "(cumulative input across 3 run(s))",
            "kind": "capacity",
            "source": "system_park",
            "input_token_sum": 4_148_125,
            "cap": 4_000_000,
            "runs": 3,
        },
    )
    kb._append_event(
        conn,
        task_id,
        "budget_runaway_parked",
        {
            "input_token_sum": 4_148_125,
            "cap": 4_000_000,
            "runs": 3,
            "stall_class": "budget_runaway",
        },
    )
    return task_id


def test_unblock_refuses_exhausted_budget_runaway_unless_forced(kanban_home, monkeypatch):
    """A real budget-park event stream must not be silently revived forever."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"per_task_input_token_cap": 4_000_000}},
    )
    with kb.connect() as conn:
        task_id = _seed_exhausted_budget_runaway(conn)

    refused = kc.run_slash(f"unblock {task_id}")
    assert "Refusing to unblock" in refused
    assert "input_token_sum=4148125" in refused
    assert "cap=4000000" in refused
    assert "budget_extension_count=1/1" in refused
    assert "--force" in refused
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
        assert [event.kind for event in kb.list_events(conn, task_id)].count("unblocked") == 0

    assert "Unblocked" in kc.run_slash(f"unblock --force {task_id}")
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "ready"
        assert [event.kind for event in kb.list_events(conn, task_id)].count("unblocked") == 1


def test_unblock_allows_budget_park_after_current_cap_is_raised(kanban_home, monkeypatch):
    """A raised live cap makes a previously parked task actionable again."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"per_task_input_token_cap": 5_000_000}},
    )
    with kb.connect() as conn:
        task_id = _seed_exhausted_budget_runaway(conn)

    assert "Unblocked" in kc.run_slash(f"unblock {task_id}")
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "ready"
        assert [event.kind for event in kb.list_events(conn, task_id)].count("unblocked") == 1


@pytest.mark.parametrize("state", ["blocked", "scheduled"])
def test_unblock_non_budget_parks_preserves_existing_behavior(kanban_home, state):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=f"ordinary-{state}", assignee="alice")
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (state, task_id))
        kb._append_event(
            conn,
            task_id,
            "blocked" if state == "blocked" else "scheduled",
            {"reason": "ordinary operator hold", "source": "operator"},
        )

    assert "Unblocked" in kc.run_slash(f"unblock {task_id}")
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb.list_events(conn, task_id)[-1].kind == "unblocked"


def test_run_slash_block_with_kind_sets_block_kind_column(kanban_home):
    """Regression for the v0.18 upstream merge (413638a28) dropping the CLI's
    ``--kind`` flag on ``block``: kb.block_task has taken a typed ``kind``
    since e2bb46738 (dependency/needs_input/capability/transient), but
    `hermes kanban block` had no flag to reach it."""
    out = kc.run_slash("create 'x' --assignee alice")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"claim {tid}")
    assert "Blocked" in kc.run_slash(f"block {tid} 'no access' --kind capability")
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert task.block_kind == "capability"


def test_run_slash_block_with_dependency_kind_routes_to_todo(kanban_home):
    """A ``--kind dependency`` block routes to ``todo`` (parent-gated,
    auto-resumed) instead of the human ``blocked`` bucket — and the CLI
    reports where it actually landed."""
    out = kc.run_slash("create 'x' --assignee alice")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"claim {tid}")
    result = kc.run_slash(f"block {tid} 'waiting on sibling' --kind dependency")
    assert "todo" in result
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "todo"
    assert task.block_kind == "dependency"


def _goal_mode_worker_task(conn):
    tid = kb.create_task(
        conn, title="goal-mode-cli-test", assignee="test-worker",
        body="Must achieve X with verified evidence.", goal_mode=True,
    )
    kb.claim_task(conn, tid)
    return tid


def test_run_slash_complete_goal_mode_worker_rejected_by_judge(monkeypatch, kanban_home):
    """The CLI `complete` verb is the documented worker completion path
    (kb.create_task -> kb.claim_task -> `hermes kanban complete
    "$HERMES_KANBAN_TASK"`); it must hit the SAME judge gate as the
    kanban_complete model tool when the caller is the task's own scoped
    worker. Regression: the CLI path previously called kb.complete_task
    directly, bypassing the gate entirely."""
    with kb.connect() as conn:
        tid = _goal_mode_worker_task(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    def mock_judge_goal(goal, last_response, **kwargs):
        return "continue", "missing verification evidence", False, None

    monkeypatch.setattr("hermes_cli.goals.judge_goal", mock_judge_goal)
    monkeypatch.setattr("hermes_cli.goals.goal_judge_available", lambda: True)

    out = kc.run_slash(f"complete {tid} --summary 'I did some stuff but not X'")
    assert "Goal completion rejected by judge" in out
    assert "missing verification evidence" in out
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "running"


def test_run_slash_complete_goal_mode_operator_override_ungated(monkeypatch, kanban_home):
    """An operator running `hermes kanban complete` by hand (no
    HERMES_KANBAN_TASK env marker) must NOT be gated by the judge — even for
    a goal_mode task. The judge stub raises if consulted, so reaching 'done'
    proves the gate was skipped entirely (not just fail-open on the
    verdict)."""
    with kb.connect() as conn:
        tid = _goal_mode_worker_task(conn)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    def fail_if_called(goal, last_response, **kwargs):
        raise AssertionError("judge_goal must not run for an operator override")

    monkeypatch.setattr("hermes_cli.goals.judge_goal", fail_if_called)
    monkeypatch.setattr("hermes_cli.goals.goal_judge_available", lambda: True)

    out = kc.run_slash(f"complete {tid} --summary 'dispositioned done-elsewhere'")
    assert "Completed" in out
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "done"


def test_run_slash_complete_goal_mode_worker_fail_open_when_judge_unavailable(
    monkeypatch, kanban_home
):
    """Fail-open: a scoped worker completing a goal_mode task must not be
    wedged when no judge is configured — the gate probes availability first
    (same contract as the kanban_complete model tool)."""
    with kb.connect() as conn:
        tid = _goal_mode_worker_task(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    def fail_if_called(goal, last_response, **kwargs):
        raise AssertionError("judge_goal must not run when no judge is available")

    monkeypatch.setattr("hermes_cli.goals.judge_goal", fail_if_called)
    monkeypatch.setattr("hermes_cli.goals.goal_judge_available", lambda: False)

    out = kc.run_slash(f"complete {tid} --summary 'done enough'")
    assert "Completed" in out
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "done"


def test_run_slash_review_worker_requires_structured_verdict(
    monkeypatch, kanban_home
):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="review-cli-verdict",
            assignee="coder",
            initial_status="blocked",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review', claim_lock = NULL, "
                "claim_expires = NULL WHERE id = ?",
                (tid,),
            )
        claimed = kb.claim_review_task(
            conn,
            tid,
            claimer="cli-review-test",
            reviewer_profile="verifier",
        )
        assert claimed is not None
        run_id = kb.get_task(conn, tid).current_run_id

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    rejected = kc.run_slash(
        f"complete {tid} --summary 'APPROVED in prose only'"
    )
    assert "requires explicit metadata.review_verdict" in rejected
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"
        assert kb.get_run(conn, run_id).status == "running"

    approved = kc.run_slash(
        f"complete {tid} --summary 'verified' "
        "--metadata '{\"review_verdict\":\"APPROVED\"}'"
    )
    assert "Completed" in approved
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "done"
        verdict = conn.execute(
            "SELECT verdict FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()["verdict"]
        assert verdict == "APPROVED"


def test_run_slash_json_output(kanban_home):
    out = kc.run_slash("create 'jsontask' --assignee alice --json")
    payload = json.loads(out)
    assert payload["title"] == "jsontask"
    assert payload["assignee"] == "alice"
    assert payload["status"] == "ready"


def test_run_slash_create_with_kind_sets_column(kanban_home):
    out = kc.run_slash("create 'classified task' --kind code --json")
    payload = json.loads(out)
    assert payload["kind"] == "code"
    with kb.connect() as conn:
        task = kb.get_task(conn, payload["id"])
    assert task.kind == "code"


def test_run_slash_create_with_kind_analysis_sets_column(kanban_home):
    """Operator-directive: ``analysis`` is a valid PUBLIC ``--kind`` choice (the
    read-only counter-class to ``code``). The CLI must accept it (no argparse
    ``invalid choice`` exit 2) and stamp the marker on the task row, so the
    verifier emits its task-class-aware analysis header."""
    out = kc.run_slash("create 'analysis task' --kind analysis --json")
    payload = json.loads(out)
    assert payload["kind"] == "analysis"
    with kb.connect() as conn:
        task = kb.get_task(conn, payload["id"])
    assert task.kind == "analysis"


def test_run_slash_create_with_project_links_task(kanban_home):
    """Regression for the v0.18 upstream merge (413638a28) dropping the CLI's
    ``--project`` flag: kanban_db.create_task has taken ``project_id`` since
    e2bb46738, but `hermes kanban create` had no flag to reach it. Also
    guards ``project_id`` surfacing on ``_task_to_dict``."""
    from hermes_cli import projects_db as pdb

    with pdb.connect_closing() as pconn:
        pid = pdb.create_project(pconn, name="Web App", folders=["/tmp/webapp"])
        proj = pdb.get_project(pconn, pid)

    out = kc.run_slash(f"create 'linked task' --assignee alice --project {proj.slug} --json")
    payload = json.loads(out)
    assert payload["project_id"] == proj.id
    with kb.connect() as conn:
        task = kb.get_task(conn, payload["id"])
    assert task.project_id == proj.id
    # project linkage also switches an unspecified workspace to a worktree
    # anchored under the project's primary repo (see kb.create_task).
    assert task.workspace_kind == "worktree"


def test_create_kind_argparse_choices_include_analysis():
    """Lock the public ``--kind`` choices at their source: the argparse choices are
    ``sorted(_VALID_TASK_KINDS)``, so ``analysis`` being in that set is what makes
    the creation path accept it. Guards against a regression to ``invalid choice``."""
    from hermes_cli.kanban_decompose import _VALID_TASK_KINDS

    assert "analysis" in _VALID_TASK_KINDS

    # And exercised end-to-end: argparse would SystemExit(2) on an invalid choice;
    # reaching a usage-error string (not a created card) would fail the assert above.
    parser = argparse.ArgumentParser(add_help=False)
    parser.exit_on_error = False  # type: ignore[attr-defined]
    top = parser.add_subparsers(dest="_top")
    kc.build_parser(top)
    ns = parser.parse_args(["kanban", "create", "probe", "--kind", "analysis"])
    assert ns.kind == "analysis"


def test_create_ui_impact_flag_argparse_and_roundtrip(kanban_home):
    """AC-2: ``--ui-impact`` is exposed on ``kanban create``, choices validated,
    and the value reaches the stored row + kanban show read-back."""
    # argparse surface: flag accepts the allowed choices.
    parser = argparse.ArgumentParser(add_help=False)
    parser.exit_on_error = False  # type: ignore[attr-defined]
    top = parser.add_subparsers(dest="_top")
    kc.build_parser(top)
    ns = parser.parse_args(
        ["kanban", "create", "probe", "--ui-impact", "redesign"]
    )
    assert ns.ui_impact == "redesign"

    # invalid choice is rejected by argparse
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        parser.parse_args(["kanban", "create", "probe", "--ui-impact", "bogus"])

    # end-to-end: create + read-back via show --json
    out = kc.run_slash("create 'ui task' --assignee alice --ui-impact redesign --json")
    payload = json.loads(out)
    tid = payload["id"]
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.ui_impact == "redesign"
    assert kb.effective_ui_impact(task) == "operator-gated"

    show_out = kc.run_slash(f"show {tid} --json")
    show = json.loads(show_out)
    assert show["ui_impact"] == "redesign"
    assert show["effective_ui_impact"] == "operator-gated"


def test_run_slash_dispatch_dry_run_counts(kanban_home):
    kc.run_slash("create 'a' --assignee alice")
    kc.run_slash("create 'b' --assignee bob")
    out = kc.run_slash("dispatch --dry-run")
    assert "Spawned:" in out


def test_cli_dispatch_spawns_durable_closeouts_for_external_dispatcher(
    kanban_home, monkeypatch
):
    from hermes_cli import kanban_closeout as closeout

    calls = []
    monkeypatch.setattr(
        closeout,
        "spawn_pending_closeouts",
        lambda conn, board, limit=10: calls.append((board, limit)) or [],
    )
    kc.run_slash("dispatch")
    assert calls == [("default", 10)]


def test_run_slash_context_output_format(kanban_home):
    out = kc.run_slash("create 'tech spec' --assignee alice --body 'write an RFC'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'remember to include performance section'")
    ctx = kc.run_slash(f"context {tid}")
    assert "tech spec" in ctx
    assert "write an RFC" in ctx
    assert "performance section" in ctx


def test_run_slash_tenant_filter(kanban_home):
    kc.run_slash("create 'biz-a task' --tenant biz-a --assignee alice")
    kc.run_slash("create 'biz-b task' --tenant biz-b --assignee alice")
    a = kc.run_slash("list --tenant biz-a")
    b = kc.run_slash("list --tenant biz-b")
    assert "biz-a task" in a and "biz-b task" not in a
    assert "biz-b task" in b and "biz-a task" not in b


def test_run_slash_session_filter(kanban_home):
    """`hermes kanban list --session <id>` filters by the originating
    chat session id stamped on tasks created from inside an ACP loop."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="from sess-1 a", assignee="alice", session_id="sess-1"
        )
        kb.create_task(
            conn, title="from sess-1 b", assignee="alice", session_id="sess-1"
        )
        kb.create_task(
            conn, title="from sess-2", assignee="alice", session_id="sess-2"
        )
        kb.create_task(conn, title="cli only", assignee="alice")
    out_1 = kc.run_slash("list --session sess-1")
    out_2 = kc.run_slash("list --session sess-2")
    assert "from sess-1 a" in out_1
    assert "from sess-1 b" in out_1
    assert "from sess-2" not in out_1
    assert "cli only" not in out_1
    assert "from sess-2" in out_2
    assert "from sess-1 a" not in out_2


def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_run_slash_assignee_filter(kanban_home):
    """`hermes kanban list --assignee <name>` keeps only tasks whose
    assignee/lane matches, dropping the other lanes."""
    kc.run_slash("create 'alice lane' --assignee alice")
    kc.run_slash("create 'bob lane' --assignee bob")
    out = kc.run_slash("list --assignee alice")
    assert "alice lane" in out
    assert "bob lane" not in out


def test_run_slash_created_by_filter(kanban_home):
    """`hermes kanban list --created-by <author>` keeps only tasks whose
    `created_by` exactly matches the argument (different lanes, same author)."""
    kc.run_slash("create 'authored by alice' --created-by alice --assignee worker")
    kc.run_slash("create 'authored by bob' --created-by bob --assignee worker")
    out = kc.run_slash("list --created-by alice")
    assert "authored by alice" in out
    assert "authored by bob" not in out


def test_run_slash_created_by_empty_result_is_clean(kanban_home):
    """An unmatched `--created-by` yields an empty list (and `[]` for
    `--json`), never an error."""
    kc.run_slash("create 'present task' --created-by alice")
    out = kc.run_slash("list --created-by nobody")
    assert "present task" not in out
    assert "no matching tasks" in out.lower()
    raw = kc.run_slash("list --created-by nobody --json")
    assert json.loads(raw) == []


def test_run_slash_created_by_combines_with_status(kanban_home):
    """`--created-by` composes with `--status`: a task must satisfy BOTH
    predicates to be listed."""
    import re
    kc.run_slash("create 'alice ready task' --created-by alice")
    out = kc.run_slash("create 'alice blocked task' --created-by alice")
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"claim {tid}")
    kc.run_slash(f"block {tid} 'hold'")
    kc.run_slash("create 'bob ready task' --created-by bob")
    filtered = kc.run_slash("list --created-by alice --status ready")
    assert "alice ready task" in filtered
    assert "alice blocked task" not in filtered  # excluded by --status
    assert "bob ready task" not in filtered       # excluded by --created-by


def test_run_slash_usage_error_returns_message(kanban_home):
    # Missing required argument for create
    out = kc.run_slash("create")
    assert "usage" in out.lower() or "error" in out.lower()


def test_run_slash_assign_reassigns(kanban_home):
    out = kc.run_slash("create 'x' --assignee alice")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    assert "Assigned" in kc.run_slash(f"assign {tid} bob")
    show = kc.run_slash(f"show {tid}")
    assert "bob" in show


def test_run_slash_link_unlink(kanban_home):
    a = kc.run_slash("create 'a'")
    b = kc.run_slash("create 'b'")
    import re
    ta = re.search(r"(t_[a-f0-9]+)", a).group(1)
    tb = re.search(r"(t_[a-f0-9]+)", b).group(1)
    assert "Linked" in kc.run_slash(f"link {ta} {tb}")
    # After link, b is todo
    show = kc.run_slash(f"show {tb}")
    assert "todo" in show
    assert "Unlinked" in kc.run_slash(f"unlink {ta} {tb}")


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kb.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kb.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------

def test_kanban_is_resolvable():
    from hermes_cli.commands import resolve_command

    cmd = resolve_command("kanban")
    assert cmd is not None
    assert cmd.name == "kanban"


def test_kanban_bypasses_active_session_guard():
    from hermes_cli.commands import should_bypass_active_session

    assert should_bypass_active_session("kanban")


def test_kanban_in_autocomplete_table():
    from hermes_cli.commands import COMMANDS, SUBCOMMANDS

    assert "/kanban" in COMMANDS
    subs = SUBCOMMANDS.get("/kanban") or []
    assert "create" in subs
    assert "dispatch" in subs


def test_kanban_autocomplete_includes_live_subcommands():
    from prompt_toolkit.document import Document

    from hermes_cli.commands import SlashCommandCompleter

    completer = SlashCommandCompleter()
    doc = Document("/kanban sp", cursor_position=len("/kanban sp"))
    texts = {c.text for c in completer.get_completions(doc, None)}

    assert "specify" in texts

    doc = Document("/kanban re", cursor_position=len("/kanban re"))
    texts = {c.text for c in completer.get_completions(doc, None)}

    assert "reclaim" in texts
    assert "reassign" in texts


def test_kanban_not_gateway_only():
    # kanban is available in BOTH CLI and gateway surfaces.
    from hermes_cli.commands import COMMAND_REGISTRY

    cmd = next(c for c in COMMAND_REGISTRY if c.name == "kanban")
    assert not cmd.cli_only
    assert not cmd.gateway_only


# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()


def test_run_slash_reassign_with_reclaim_flag(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'switch model' --assignee orig")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    tid = m.group(1)

    # Simulate a running claim.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reassign {tid} newbie --reclaim --reason 'switch'")
    assert "Reassigned" in out, out
    out2 = kc.run_slash(f"show {tid}")
    assert "newbie" in out2


# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------

def test_run_slash_specify_end_to_end(kanban_home, monkeypatch):
    """The /kanban specify slash command routes through run_slash, which
    both the interactive CLI and every gateway platform use. This test
    covers both surfaces."""
    from unittest.mock import MagicMock

    # Create a triage task via the same slash surface.
    create_out = kc.run_slash("create 'rough idea' --triage")
    import re
    m = re.search(r"(t_[a-f0-9]+)", create_out)
    assert m, f"no task id in: {create_out!r}"
    tid = m.group(1)

    # Mock the auxiliary client so we don't hit a real provider.
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = (
        '{"title": "Spec: rough idea", "body": "**Goal**\\nShip it."}'
    )
    # specify_task routes through call_llm now (#35566) — mock it directly.
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        MagicMock(return_value=resp),
    )

    # Specify via slash.
    out = kc.run_slash(f"specify {tid}")
    assert "Specified" in out
    assert tid in out

    # Task is promoted and retitled.
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status in {"todo", "ready"}
    assert task.title == "Spec: rough idea"


def test_run_slash_specify_help_is_reachable(kanban_home):
    """`-h`/`--help` on a subcommand returns the actual help text — see
    issue #21794. argparse writes help to stdout and exits 0; run_slash
    must capture both streams and treat exit 0 as success, not error."""
    out = kc.run_slash("specify --help")
    assert "specify" in out.lower()
    # Help dump should NOT come back wrapped as a usage error.
    assert not out.startswith("⚠")


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------

def test_run_slash_bare_returns_curated_help(kanban_home):
    """Bare `/kanban` returns the curated short-help block — not a 5KB
    argparse usage dump."""
    out = kc.run_slash("")
    assert "/kanban" in out
    assert "list" in out
    assert "show" in out
    # Sanity: should be a chat-friendly size, not the raw usage tree.
    assert len(out) < 2000
    # Shouldn't surface argparse's usage-error sentinel.
    assert "usage error" not in out.lower()


@pytest.mark.parametrize("alias", ["help", "--help", "-h", "?"])
def test_run_slash_help_aliases_match_bare(kanban_home, alias):
    """Every documented help alias produces the same curated output."""
    bare = kc.run_slash("")
    out = kc.run_slash(alias)
    assert out == bare


def test_run_slash_subcommand_help_returns_help_text(kanban_home):
    """`/kanban show -h` returns the actual subcommand help, not a
    fake `(usage error: 0)` sentinel."""
    out = kc.run_slash("show -h")
    assert "task_id" in out
    assert "/kanban show" in out
    assert not out.startswith("⚠")


def test_run_slash_unknown_action_friendly_error(kanban_home):
    """Unknown subcommand surfaces a single-line usage error prefixed
    with our marker — no `(usage error: 2)` wrapping, no doubled
    `kanban kanban` prog string."""
    out = kc.run_slash("frobnicate")
    assert "/kanban" in out
    assert "frobnicate" in out
    assert "/kanban-wrap" not in out
    assert "/kanban kanban" not in out
    assert "(usage error: " not in out


def test_run_slash_missing_required_arg_friendly_error(kanban_home):
    """Missing positional argument shows the subcommand-scoped usage
    line, not the top-level kanban tree."""
    out = kc.run_slash("show")
    assert "/kanban show" in out
    assert "task_id" in out


def test_run_slash_board_override_restores_prior_env(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "beta")

    kc.run_slash("--board alpha list")

    assert os.environ.get("HERMES_KANBAN_BOARD") == "beta"


def test_run_slash_board_override_does_not_change_boards_show_current(kanban_home):
    kb.create_board("alpha")
    kb.create_board("beta")
    kb.set_current_board("alpha")

    out = kc.run_slash("--board beta boards show")

    assert "Current board: alpha" in out


# ---------------------------------------------------------------------------
# durable closeout subcommand wiring
# ---------------------------------------------------------------------------

def _closeout_args(task_id, *, inline=False, json_output=False, board=None):
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    argv = ["kanban"]
    if board:
        argv += ["--board", board]
    argv += ["closeout", task_id]
    if inline:
        argv.append("--inline")
    if json_output:
        argv.append("--json")
    return parser.parse_args(argv)


def test_cli_closeout_default_launches_detached_unit(kanban_home, monkeypatch):
    from hermes_cli import kanban_closeout as closeout

    calls = []
    monkeypatch.setattr(
        closeout,
        "spawn_closeout_unit",
        lambda task_id, board=None: calls.append((task_id, board))
        or {"ok": True, "unit": "hermes-kanban-closeout-default-t_x", "detail": "started"},
    )
    monkeypatch.setattr(
        closeout,
        "process_closeout",
        lambda *_args, **_kwargs: pytest.fail("detached path processed inline"),
    )

    rc = kc.kanban_command(_closeout_args("t_x"))

    assert rc == 0
    assert calls == [("t_x", None)]


def test_cli_closeout_inline_processes_exactly_one_task_as_json(
    kanban_home, monkeypatch, capsys
):
    from hermes_cli import kanban_closeout as closeout

    calls = []

    def fake_process(conn, task_id):
        calls.append((conn is not None, task_id))
        return closeout.CloseoutResult(
            task_id=task_id,
            state="delivered",
            release_state=closeout.CLOSEOUT_RELEASE_NOT_REQUIRED,
            receipt_path="/tmp/receipt.md",
            delivered=True,
        )

    monkeypatch.setattr(closeout, "process_closeout", fake_process)
    rc = kc.kanban_command(
        _closeout_args("t_one", inline=True, json_output=True)
    )

    assert rc == 0
    assert calls == [(True, "t_one")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "delivered"
    assert payload["delivered"] is True


@pytest.mark.parametrize("state", ["pending", "not_claimed"])
def test_cli_closeout_inline_pending_check_exits_zero(
    kanban_home, monkeypatch, state
):
    from hermes_cli import kanban_closeout as closeout

    monkeypatch.setattr(
        closeout,
        "process_closeout",
        lambda _conn, task_id: closeout.CloseoutResult(
            task_id=task_id,
            state=state,
            release_state=closeout.CLOSEOUT_RELEASE_WAITING,
        ),
    )

    assert kc.kanban_command(_closeout_args("t_wait", inline=True)) == 0


# ---------------------------------------------------------------------------
# release-gate subcommand wiring (R2)
# ---------------------------------------------------------------------------

def _release_gate_args(task_id, *, inline=False, **extra):
    """Build a parsed release-gate Namespace. ``inline`` and any store_true
    flags become bare ``--inline`` etc.; keyword extras with a value are
    emitted as ``--key value``."""
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    argv = ["kanban", "release-gate", task_id]
    if inline:
        argv.append("--inline")
    for k, v in extra.items():
        argv += [f"--{k.replace('_','-')}", str(v)]
    return parser.parse_args(argv)


def test_cli_release_gate_green_exit_zero(kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban_worktrees as kwt

    captured = {}

    def fake_exec(conn, task_id, *, max_retries=None):
        captured["task_id"] = task_id
        captured["max_retries"] = max_retries
        return {"status": "green", "fixer_attempts": 0, "root_id": task_id}

    monkeypatch.setattr(kwt, "execute_release_gate", fake_exec)
    args = _release_gate_args("t_gate", inline=True, max_retries=3)
    rc = kc.kanban_command(args)
    assert rc == 0
    assert captured == {"task_id": "t_gate", "max_retries": 3}
    assert "green" in capsys.readouterr().out


def test_cli_release_gate_escalated_exit_two(kanban_home, monkeypatch):
    from hermes_cli import kanban_worktrees as kwt

    monkeypatch.setattr(
        kwt, "execute_release_gate",
        lambda conn, task_id, *, max_retries=None: {
            "status": "escalated", "fixer_attempts": 2, "root_id": task_id,
        },
    )
    rc = kc.kanban_command(_release_gate_args("t_gate", inline=True))
    assert rc == 2


def test_cli_release_gate_precondition_error_exit_one(kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban_worktrees as kwt

    def boom(conn, task_id, *, max_retries=None):
        raise kwt.ReleaseGateError("not a release-gate child")

    monkeypatch.setattr(kwt, "execute_release_gate", boom)
    rc = kc.kanban_command(_release_gate_args("t_gate", inline=True))
    assert rc == 1
    assert "not a release-gate child" in capsys.readouterr().err


def test_cli_release_gate_calls_pre_deploy_backup(kanban_home, monkeypatch):
    """_cmd_release_gate --inline calls create_pre_deploy_backup before the gate."""
    from hermes_cli import kanban_worktrees as kwt
    import hermes_cli.backup as backup_mod

    backup_calls = []

    def fake_backup(**_kw):
        backup_calls.append(True)
        return None  # simulate no files / fresh install

    monkeypatch.setattr(backup_mod, "create_pre_deploy_backup", fake_backup)
    monkeypatch.setattr(
        kwt, "execute_release_gate",
        lambda conn, task_id, *, max_retries=None: {
            "status": "green", "fixer_attempts": 0, "root_id": task_id,
        },
    )

    rc = kc.kanban_command(_release_gate_args("t_gate", inline=True))
    assert rc == 0
    assert backup_calls, "create_pre_deploy_backup was not called"


def test_cli_release_gate_continues_when_backup_raises(kanban_home, monkeypatch, capsys):
    """A crashing create_pre_deploy_backup must NOT block the release gate."""
    from hermes_cli import kanban_worktrees as kwt
    import hermes_cli.backup as backup_mod

    def exploding_backup(**_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(backup_mod, "create_pre_deploy_backup", exploding_backup)
    monkeypatch.setattr(
        kwt, "execute_release_gate",
        lambda conn, task_id, *, max_retries=None: {
            "status": "green", "fixer_attempts": 0, "root_id": task_id,
        },
    )

    rc = kc.kanban_command(_release_gate_args("t_gate", inline=True))
    # Gate still returns green exit code despite backup failure
    assert rc == 0


def test_cli_release_gate_default_launches_detached_unit(kanban_home, monkeypatch, capsys):
    """AC-1: WITHOUT --inline, _cmd_release_gate hands off to
    spawn_release_gate_activation — the detached transient unit that survives
    the dashboard restart the gate itself triggers — and never touches
    execute_release_gate directly."""
    from hermes_cli import kanban_worktrees as kwt

    captured = {}
    exec_called = []

    def fake_spawn(task_id, *, board=None, **_kw):
        captured["task_id"] = task_id
        captured["board"] = board
        return {"ok": True, "unit": "hermes-release-gate-t_deadbeef", "detail": "started"}

    monkeypatch.setattr(kwt, "spawn_release_gate_activation", fake_spawn)
    # If the inline path fires by mistake, this must blow up the test.
    monkeypatch.setattr(
        kwt, "execute_release_gate",
        lambda *a, **k: exec_called.append(1),
    )

    args = _release_gate_args("t_deadbeef", inline=False)  # default: detached
    rc = kc.kanban_command(args)
    assert rc == 0
    assert captured == {"task_id": "t_deadbeef", "board": None}
    assert exec_called == [], "detached path must NOT call execute_release_gate"
    out = capsys.readouterr().out
    assert "detached activation" in out and "hermes-release-gate-t_deadbeef" in out


def test_cli_release_gate_default_reports_spawn_failure(kanban_home, monkeypatch, capsys):
    """AC-1: a failed launch (precondition/spawn error) returns rc=1 and ok=False."""
    from hermes_cli import kanban_worktrees as kwt

    monkeypatch.setattr(
        kwt, "spawn_release_gate_activation",
        lambda task_id, **_kw: {"ok": False, "unit": None, "detail": "Unit already exists."},
    )
    args = _release_gate_args("t_x", inline=False)
    rc = kc.kanban_command(args)
    assert rc == 1
    assert "FAILED" in capsys.readouterr().out


def test_cli_release_gate_default_json(kanban_home, monkeypatch, capsys):
    """AC-1: --json on the detached path emits the spawn result dict as JSON."""
    from hermes_cli import kanban_worktrees as kwt
    import json

    monkeypatch.setattr(
        kwt, "spawn_release_gate_activation",
        lambda task_id, **_kw: {"ok": True, "unit": "u", "detail": "started"},
    )
    args = _release_gate_args("t_j", inline=False)
    # _release_gate_args appends --json as "--json True" (str); that would be
    # mis-parsed by argparse store_true. Build it via the parser directly.
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban", "release-gate", "t_j", "--json"])
    rc = kc.kanban_command(args)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert json.loads(out)["ok"] is True


# ---------------------------------------------------------------------------
# respec CLI smoke tests (kanban respec <id> --body/--ac)
# ---------------------------------------------------------------------------

def _respec_parser():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser


def _respec_stdout_task_id(capsys):
    out = capsys.readouterr().out.strip()
    assert re.fullmatch(r"t_[0-9a-f]+", out)
    return out


def test_respec_cli_updates_body(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", body="old")
    parser = _respec_parser()
    args = parser.parse_args(["kanban", "respec", tid, "--body", "new body"])
    rc = kc.kanban_command(args)
    assert rc == 0
    new_id = _respec_stdout_task_id(capsys)
    with kb.connect() as conn:
        old = kb.get_task(conn, tid)
        new = kb.get_task(conn, new_id)
    assert old is not None
    assert new is not None
    assert old.status == "archived"
    assert old.body == "old"
    assert new.body == "new body"


def test_respec_cli_rejects_running_task(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", body="old")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        conn.commit()
    parser = _respec_parser()
    args = parser.parse_args(["kanban", "respec", tid, "--body", "nope"])
    rc = kc.kanban_command(args)
    assert rc == 1
    assert "cannot respec" in capsys.readouterr().err
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).body == "old"


def test_respec_cli_requires_a_field(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", body="old")
    parser = _respec_parser()
    args = parser.parse_args(["kanban", "respec", tid])
    rc = kc.kanban_command(args)
    assert rc == 2
    assert "--body, --body-file, and/or --ac" in capsys.readouterr().err


def test_respec_cli_bad_ac_reports_error(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", body="old")
    parser = _respec_parser()
    args = parser.parse_args(["kanban", "respec", tid, "--ac", "just prose"])
    rc = kc.kanban_command(args)
    assert rc == 1
    # The ValueError message surfaces via the dispatch wrapper.
    assert "AC-" in capsys.readouterr().err


def test_respec_cli_updates_acceptance_criteria(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", body="old")
    parser = _respec_parser()
    args = parser.parse_args(
        ["kanban", "respec", tid, "--ac", "- AC-1: do the thing"]
    )
    assert kc.kanban_command(args) == 0
    new_id = _respec_stdout_task_id(capsys)
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT acceptance_criteria FROM tasks WHERE id = ?", (new_id,)
        ).fetchone()
    parsed = json.loads(row["acceptance_criteria"])
    assert any("do the thing" in str(i) for i in parsed)


def test_respec_cli_blank_ac_exits_one_with_speaking_error(kanban_home, capsys):
    """hermes kanban respec <id> --ac "" must exit 1 with a speaking message.

    Regression for the DATA-LOSS BLOCKER (cross-family review): a blank --ac
    previously silently cleared the acceptance_criteria column. The guard in
    respec_task now raises ValueError before any DB write, which the dispatch
    wrapper surfaces as "kanban: …" on stderr and exits 1.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", body="b")
        conn.execute(
            "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
            (json.dumps(["AC-1: do not clear me"]), tid),
        )
        conn.commit()
    parser = _respec_parser()
    args = parser.parse_args(["kanban", "respec", tid, "--ac", ""])
    rc = kc.kanban_command(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "blank" in err.lower()
    # AC column must be untouched.
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT acceptance_criteria FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert row["acceptance_criteria"] is not None
    assert "do not clear me" in row["acceptance_criteria"]
