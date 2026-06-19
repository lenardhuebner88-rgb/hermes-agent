"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
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


def test_run_slash_dispatch_dry_run_counts(kanban_home):
    kc.run_slash("create 'a' --assignee alice")
    kc.run_slash("create 'b' --assignee bob")
    out = kc.run_slash("dispatch --dry-run")
    assert "Spawned:" in out


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
    fake_client = MagicMock()
    fake_client.chat.completions.create = MagicMock(return_value=resp)
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda *a, **kw: (fake_client, "test-model"),
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
# release-gate subcommand wiring (R2)
# ---------------------------------------------------------------------------

def _release_gate_args(task_id, **extra):
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    argv = ["kanban", "release-gate", task_id]
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
    args = _release_gate_args("t_gate", max_retries=3)
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
    rc = kc.kanban_command(_release_gate_args("t_gate"))
    assert rc == 2


def test_cli_release_gate_precondition_error_exit_one(kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban_worktrees as kwt

    def boom(conn, task_id, *, max_retries=None):
        raise kwt.ReleaseGateError("not a release-gate child")

    monkeypatch.setattr(kwt, "execute_release_gate", boom)
    rc = kc.kanban_command(_release_gate_args("t_gate"))
    assert rc == 1
    assert "not a release-gate child" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# respec CLI smoke tests (kanban respec <id> --body/--ac)
# ---------------------------------------------------------------------------

def _respec_parser():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser


def test_respec_cli_updates_body(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", body="old")
    parser = _respec_parser()
    args = parser.parse_args(["kanban", "respec", tid, "--body", "new body"])
    rc = kc.kanban_command(args)
    assert rc == 0
    assert "Respecified" in capsys.readouterr().out
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).body == "new body"


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
    assert "--body and/or --ac" in capsys.readouterr().err


def test_respec_cli_bad_ac_reports_error(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", body="old")
    parser = _respec_parser()
    args = parser.parse_args(["kanban", "respec", tid, "--ac", "just prose"])
    rc = kc.kanban_command(args)
    assert rc == 1
    # The ValueError message surfaces via the dispatch wrapper.
    assert "AC-" in capsys.readouterr().err


def test_respec_cli_updates_acceptance_criteria(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", body="old")
    parser = _respec_parser()
    args = parser.parse_args(
        ["kanban", "respec", tid, "--ac", "- AC-1: do the thing"]
    )
    assert kc.kanban_command(args) == 0
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT acceptance_criteria FROM tasks WHERE id = ?", (tid,)
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
