"""Tests for per-task Kanban iteration-budget knobs.

``kanban create --max-iterations N`` persists N on the task row, and the
worker-env builder injects ``HERMES_MAX_ITERATIONS=N`` so the spawned
worker honours the per-task override instead of the profile default.

``--max-continuations`` is covered here at the create/validation layer;
run-state behaviour is covered by ``test_kanban_auto_continuation.py``.
"""

from __future__ import annotations

import argparse
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
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# (c) per-task --max-iterations
# ---------------------------------------------------------------------------


def test_budget_columns_exist_in_fresh_db(kanban_home):
    """init_db on a fresh HERMES_HOME must create iteration-budget
    and auto-continuation columns. Old DBs go through the additive
    `_migrate_add_optional_columns` branches.
    """
    with kb.connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "max_iterations" in cols
    assert "continuation_count" in cols
    assert "max_continuations" in cols
    assert "last_continuation_reason" in cols


def test_create_task_persists_max_iterations(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="audit-with-budget", max_iterations=120,
        )
        task = kb.get_task(conn, tid)
    assert task.max_iterations == 120


def test_create_task_persists_max_continuations_zero(kanban_home):
    """0 is meaningful: disable auto-continuation for this task."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="no-auto-continue", max_continuations=0,
        )
        task = kb.get_task(conn, tid)
    assert task.max_continuations == 0
    assert task.continuation_count == 0


def test_create_task_default_max_iterations_is_none(kanban_home):
    """NULL = inherit the profile default, the safe back-compat
    behaviour for any existing automation that doesn't pass the flag.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="default-budget")
        task = kb.get_task(conn, tid)
    assert task.max_iterations is None


def test_cli_create_flag_parses():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    ns = parser.parse_args(
        ["kanban", "create", "audit",
         "--body", "audit body",
         "--max-iterations", "120",
         "--max-continuations", "2"],
    )
    assert ns.max_iterations == 120
    assert ns.max_continuations == 2


def test_cli_create_rejects_zero_max_iterations(kanban_home, capsys, monkeypatch):
    """`--max-iterations 0` is nonsensical — refuse rather than
    creating a guaranteed-to-fail task.
    """
    ns = argparse.Namespace(
        title="zero",
        body="b",
        assignee=None,
        priority=0,
        parent=None,
        tenant=None,
        created_by=None,
        workspace="scratch",
        branch=None,
        triage=False,
        max_runtime=None,
        max_retries=None,
        max_iterations=0,
        max_continuations=None,
        skills=None,
        idempotency_key=None,
        initial_status="running",
        json=False,
        scope_contract_json=None,
        allowed_tool=[],
        forbidden_system=[],
        report_contract_version=1,
        unsafe=False,
        raw_create=False,
    )
    rc = kc._cmd_create(ns)
    assert rc == 2
    err = capsys.readouterr().err
    assert "--max-iterations must be >= 1" in err


def test_cli_create_rejects_negative_max_continuations(kanban_home, capsys):
    ns = argparse.Namespace(
        title="negative-continuations",
        body="b",
        assignee=None,
        priority=0,
        parent=None,
        tenant=None,
        created_by=None,
        workspace="scratch",
        branch=None,
        triage=False,
        max_runtime=None,
        max_retries=None,
        max_iterations=None,
        max_continuations=-1,
        skills=None,
        idempotency_key=None,
        initial_status="running",
        json=False,
        scope_contract_json=None,
        allowed_tool=[],
        forbidden_system=[],
        report_contract_version=1,
        unsafe=False,
        raw_create=False,
    )
    rc = kc._cmd_create(ns)
    assert rc == 2
    err = capsys.readouterr().err
    assert "--max-continuations must be >= 0" in err


def test_cli_create_end_to_end_persists(kanban_home, capsys):
    """End-to-end: pass --max-iterations through _cmd_create and read
    back via `get_task`.
    """
    ns = argparse.Namespace(
        title="end-to-end",
        body="body",
        assignee=None,
        priority=0,
        parent=None,
        tenant=None,
        created_by=None,
        workspace="scratch",
        branch=None,
        triage=False,
        max_runtime=None,
        max_retries=None,
        max_iterations=90,
        max_continuations=2,
        skills=None,
        idempotency_key=None,
        initial_status="running",
        json=True,
        scope_contract_json=None,
        allowed_tool=[],
        forbidden_system=[],
        report_contract_version=1,
        unsafe=False,
        raw_create=False,
    )
    rc = kc._cmd_create(ns)
    assert rc == 0
    import json as _json
    payload = _json.loads(capsys.readouterr().out)
    assert payload["max_iterations"] == 90
    assert payload["max_continuations"] == 2
    assert payload["continuation_count"] == 0

    with kb.connect() as conn:
        task = kb.get_task(conn, payload["id"])
    assert task.max_iterations == 90
    assert task.max_continuations == 2


def test_worker_env_injects_hermes_max_iterations(kanban_home, monkeypatch):
    """The worker-env builder must export HERMES_MAX_ITERATIONS=N
    when the task has a non-null max_iterations. NULL = no export
    (worker inherits the profile/global default).

    Verified by capturing the env dict via a monkey-patched
    ``subprocess.Popen``.
    """
    captured: dict[str, dict] = {}

    class _FakePopen:
        def __init__(self, cmd, env=None, **kwargs):
            captured["env"] = dict(env or {})
            self.pid = 12345

        def wait(self, *a, **kw):
            return 0

    import subprocess
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(kanban_home),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.normalize_profile_name",
        lambda name: name,
    )

    # With per-task override:
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="audit", assignee="coder",
            max_iterations=150,
        )
        task = kb.get_task(conn, tid)
    kb._default_spawn(task, "/tmp/ws", board="default")
    assert captured["env"].get("HERMES_MAX_ITERATIONS") == "150"

    captured.clear()

    # Without per-task override:
    with kb.connect() as conn:
        tid2 = kb.create_task(conn, title="default-budget", assignee="coder")
        task2 = kb.get_task(conn, tid2)
    kb._default_spawn(task2, "/tmp/ws", board="default")
    assert "HERMES_MAX_ITERATIONS" not in captured["env"]


def test_worker_cmd_passes_max_turns_flag(kanban_home, monkeypatch):
    """Per-task max_iterations must reach the worker as a ``--max-turns N``
    chat flag, not just the ``HERMES_MAX_ITERATIONS`` env var.

    The env var ALONE is a no-op in production: the worker resolves
    max_turns as "CLI arg > config > env > default" (cli.py:3052) and
    ``load_cli_config`` always injects ``agent.max_turns=90``, so config
    shadows the env var. The ``--max-turns`` chat flag hits the
    top-precedence CLI-arg branch (cli.py:3053) and actually beats the
    profile default — the whole point of the per-task override for
    audit-class tasks. Regression guard for the post-rebaseline gap where
    only the (shadowed) env var was injected.
    """
    captured: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, cmd, env=None, **kwargs):
            captured["cmd"] = list(cmd)
            captured["env"] = dict(env or {})
            self.pid = 12345

        def wait(self, *a, **kw):
            return 0

    import subprocess
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(kanban_home),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.normalize_profile_name",
        lambda name: name,
    )

    # With per-task override: `--max-turns 150` must appear as a chat flag.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="audit", assignee="coder", max_iterations=150,
        )
        task = kb.get_task(conn, tid)
    kb._default_spawn(task, "/tmp/ws", board="default")
    cmd = captured["cmd"]
    assert "chat" in cmd, f"worker cmd has no chat subcommand: {cmd}"
    assert "--max-turns" in cmd, f"worker cmd missing --max-turns: {cmd}"
    flag_idx = cmd.index("--max-turns")
    assert cmd[flag_idx + 1] == "150", f"--max-turns value wrong: {cmd}"
    # Must be a chat-subcommand arg (after `chat`) so argparse routes it to
    # chat_parser.max_turns -> HermesCLI(max_turns=...), the branch that
    # outranks agent.max_turns from the profile config.
    assert flag_idx > cmd.index("chat"), f"--max-turns placed before chat: {cmd}"
    # Env var still injected for consistency / non-load_cli_config consumers.
    assert captured["env"].get("HERMES_MAX_ITERATIONS") == "150"

    captured.clear()

    # Without per-task override: no --max-turns flag (inherit profile default).
    with kb.connect() as conn:
        tid2 = kb.create_task(conn, title="default-budget", assignee="coder")
        task2 = kb.get_task(conn, tid2)
    kb._default_spawn(task2, "/tmp/ws", board="default")
    assert "--max-turns" not in captured["cmd"]
