"""Tests for the per-task iteration-budget override and the bumped
dispatcher continuation cap (hardening sprint TASK 8).

Two concerns:
1. ``kanban_db.DEFAULT_ITERATION_BUDGET_CONTINUATION_LIMIT`` must be
   >= 3 so audit-class tasks get more than one dispatcher-driven
   retry chunk before the hard cap fires
   (see ``feedback_hermes_iteration_budget_cap.md``).
2. ``kanban create --max-iterations N`` persists N on the task row,
   and the worker-env builder injects
   ``HERMES_MAX_ITERATIONS=N`` so the spawned worker honours the
   per-task override instead of the profile default.
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
# (b) dispatcher continuation cap
# ---------------------------------------------------------------------------


def test_iteration_budget_continuation_limit_is_at_least_3():
    """The cap of 1 reproducibly blocked audit-class tasks on
    2026-05-27 (t_440c967d / t_9caa1873). Hardening sprint TASK 8
    bumps it to 3.
    """
    assert kb.DEFAULT_ITERATION_BUDGET_CONTINUATION_LIMIT >= 3


# ---------------------------------------------------------------------------
# (c) per-task --max-iterations
# ---------------------------------------------------------------------------


def test_max_iterations_column_exists_in_fresh_db(kanban_home):
    """init_db on a fresh HERMES_HOME must create the
    `tasks.max_iterations` column.  Old DBs go through the
    `_migrate_add_optional_columns` add-if-missing branch.
    """
    with kb.connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "max_iterations" in cols


def test_create_task_persists_max_iterations(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="audit-with-budget", max_iterations=120,
        )
        task = kb.get_task(conn, tid)
    assert task.max_iterations == 120


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
         "--max-iterations", "120"],
    )
    assert ns.max_iterations == 120


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

    with kb.connect() as conn:
        task = kb.get_task(conn, payload["id"])
    assert task.max_iterations == 90


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
