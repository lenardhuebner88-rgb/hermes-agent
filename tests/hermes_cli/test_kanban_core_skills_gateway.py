"""Kanban core functionality tests: skills gateway.

Split from test_kanban_core_functionality.py (pure move; no test logic changes).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import run_slash

from tests.hermes_cli._kanban_test_helpers import (
    _write_test_profile,
)

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Existing crash-detection tests pre-date the grace window; pin to 0
    # so they keep their immediate-reclaim semantics.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Disable the detect_crashed_workers grace period for legacy tests in
    # this file that claim a task and immediately expect
    # ``detect_crashed_workers`` to act on it. The grace period (30s by
    # default, see ``DEFAULT_CRASH_GRACE_SECONDS``) prevents the
    # multi-dispatcher reap race in production; setting it to 0 here
    # restores the pre-fix instant-reclaim semantics these tests were
    # written against. The grace-period itself is covered by dedicated
    # tests in tests/hermes_cli/test_kanban_db.py.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    return home


def _make_create_ns(**overrides):
    """Build a Namespace suitable for kb_cli._cmd_create()."""
    ns = argparse.Namespace(
        title="x", body=None, assignee="worker",
        created_by="user", workspace="scratch", tenant=None,
        priority=0, parent=None, triage=False,
        idempotency_key=None, max_runtime=None, skills=None,
        json=False,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _make_decompose_ns(task_id, **overrides):
    """Build a Namespace suitable for kb_cli._cmd_decompose()."""
    ns = argparse.Namespace(
        task_id=task_id, all_triage=False, tenant=None,
        author="tester", json=False,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# Per-task force-loaded skills
# ---------------------------------------------------------------------------

def test_create_task_persists_skills(kanban_home):
    """Task.skills round-trips through create -> get_task."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="skilled task",
            assignee="linguist",
            skills=["translation", "github-code-review"],
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.skills == ["translation", "github-code-review"]
    finally:
        conn.close()


def test_create_task_skills_none_stays_none(kanban_home):
    """Default behavior: no skills arg means Task.skills is None."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="plain task", assignee="someone")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.skills is None
    finally:
        conn.close()


def test_create_task_skills_deduplicates_and_strips(kanban_home):
    """Dup names collapse; whitespace is stripped; empties dropped."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dedupe",
            assignee="x",
            skills=["  translation  ", "translation", "", None, "review"],
        )
        task = kb.get_task(conn, tid)
        assert task.skills == ["translation", "review"]
    finally:
        conn.close()


def test_create_task_skills_rejects_comma_embedded(kanban_home):
    """Comma in a skill name is rejected — force caller to pass a list."""
    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="cannot contain comma"):
            kb.create_task(
                conn,
                title="bad",
                assignee="x",
                skills=["a,b"],
            )
    finally:
        conn.close()


def test_create_task_skills_rejects_toolset_names(kanban_home):
    """Toolset names belong in profile config, not per-task skills."""
    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="toolset name"):
            kb.create_task(
                conn,
                title="bad toolset skill",
                assignee="x",
                skills=["web", "translation"],
            )
    finally:
        conn.close()


def test_create_task_skills_lists_all_toolset_typos(kanban_home):
    """When several toolset names are passed, the error names every one.

    Agents that confuse skills with toolsets usually pass several at once
    (``skills=["web", "browser", "terminal"]``). Listing only the first
    mistake forces serial fix-then-retry; listing all of them lets the
    caller correct in one round-trip.
    """
    conn = kb.connect()
    try:
        with pytest.raises(ValueError) as exc_info:
            kb.create_task(
                conn,
                title="three bad",
                assignee="x",
                skills=["web", "browser", "terminal"],
            )
        msg = str(exc_info.value)
        assert "'web'" in msg
        assert "'browser'" in msg
        assert "'terminal'" in msg
        # Plural noun form when multiple toolsets are flagged.
        assert "are toolset names" in msg
    finally:
        conn.close()


def test_default_spawn_appends_per_task_skills(kanban_home, monkeypatch):
    """Dispatcher argv must carry one `--skills X` pair per task skill,
    in declared order. No skill is auto-loaded anymore."""
    # Per-task skills are gated on resolvability (skip-missing-not-crash);
    # pretend these resolve in the empty isolated home.
    monkeypatch.setattr(kb, "_skill_available_for_home", lambda _n, _h: True)
    captured = {}

    class FakeProc:
        def __init__(self):
            self.pid = 42

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="multi-skill worker",
            assignee="linguist",
            skills=["translation", "github-code-review"],
        )
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task)
        kb._default_spawn(task, str(workspace))
    finally:
        conn.close()

    cmd = captured["cmd"]
    # Count every --skills pair and gather the skill names.
    skill_names = []
    for i, tok in enumerate(cmd):
        if tok == "--skills" and i + 1 < len(cmd):
            skill_names.append(cmd[i + 1])
    # Only the per-task skills, in declared order — nothing auto-loaded.
    assert skill_names == ["translation", "github-code-review"], skill_names
    # --skills must appear BEFORE the `chat` subcommand so argparse
    # attaches them to the top-level parser, not the subcommand.
    chat_idx = cmd.index("chat")
    last_skills_idx = max(
        i for i, tok in enumerate(cmd) if tok == "--skills"
    )
    assert last_skills_idx < chat_idx, (
        f"--skills must come before 'chat' in argv: {cmd}"
    )


def test_default_spawn_passes_task_skills_verbatim(kanban_home, monkeypatch):
    """Per-task skills are passed through verbatim — there is no built-in
    kanban skill to dedupe against anymore."""
    # Per-task skills are gated on resolvability (skip-missing-not-crash);
    # pretend these resolve in the empty isolated home.
    monkeypatch.setattr(kb, "_skill_available_for_home", lambda _n, _h: True)
    captured = {}

    class FakeProc:
        pid = 1

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="dup", assignee="x",
            skills=["translation", "github-code-review"],
        )
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task)
        kb._default_spawn(task, str(workspace))
    finally:
        conn.close()

    cmd = captured["cmd"]
    skill_names = [
        cmd[i + 1]
        for i, tok in enumerate(cmd)
        if tok == "--skills" and i + 1 < len(cmd)
    ]
    # Exactly the task's skills, once each, in order — no auto-loaded extras.
    assert skill_names == ["translation", "github-code-review"], (
        f"unexpected --skills in argv: {cmd}"
    )


def test_cli_assign_rejects_non_spawnable_assignee(kanban_home):
    tid = run_slash("create 'assign-target' --json")
    task_id = json.loads(tid)["id"]

    out = run_slash(f"assign {task_id} no-such-profile")

    assert "no-such-profile" in out
    assert "not spawnable" in out


def test_cli_reassign_normalizes_legacy_assignee_alias(kanban_home):
    _write_test_profile(kanban_home, "premium")
    tid = run_slash("create 'reassign-target' --json")
    task_id = json.loads(tid)["id"]

    out = run_slash(f"reassign {task_id} coder-claude")

    assert "premium" in out
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task.assignee == "premium"
    finally:
        conn.close()


def test_cli_create_rejects_non_spawnable_assignee(kanban_home):
    """CLI create rejects assignee typos before they become unspawnable ready cards."""
    out = run_slash("create 'bad-lane' --assignee no-such-profile --json")

    assert "no-such-profile" in out
    assert "not spawnable" in out


def test_cli_create_normalizes_legacy_assignee_alias(kanban_home):
    """CLI create normalizes deprecated lane aliases to canonical profiles."""
    profiles = kanban_home / "profiles"
    (profiles / "premium").mkdir(parents=True, exist_ok=True)
    (profiles / "premium" / "config.yaml").write_text("model: {}\n")

    out = run_slash("create 'hard-lane' --assignee coder-claude --json")
    tid = json.loads(out)["id"]
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.assignee == "premium"
    finally:
        conn.close()


def test_cli_create_skill_flag_repeatable(kanban_home):
    """`hermes kanban create --skill a --skill b` persists the list."""
    out = run_slash(
        "create 'multi-skill' --assignee default "
        "--skill translation --skill github-code-review --json"
    )
    tid = json.loads(out)["id"]
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.skills == ["translation", "github-code-review"]


def test_cli_create_without_skill_flag_leaves_none(kanban_home):
    """No --skill on the CLI means Task.skills stays None (not []) —
    we don't want to silently write [] when the user didn't opt in."""
    out = run_slash("create 'no-skill' --assignee default --json")
    tid = json.loads(out)["id"]
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.skills is None


def test_cli_show_renders_skills(kanban_home):
    """`hermes kanban show <id>` prints a skills row when present."""
    out = run_slash(
        "create 'show-test' --assignee default "
        "--skill translation --json"
    )
    tid = json.loads(out)["id"]
    shown = run_slash(f"show {tid}")
    assert "skills:" in shown
    assert "translation" in shown


def test_legacy_db_without_skills_column_migrates(tmp_path):
    """_migrate_add_optional_columns is idempotent and adds skills
    when absent. Run it twice on a pared-down schema to confirm."""
    import sqlite3
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Build a pared-down legacy tasks table that lacks all the
    # optional columns _migrate_add_optional_columns knows how to
    # add. We deliberately omit `skills` so we can observe its
    # introduction.
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)
    # task_events is also touched by the migrator for run_id backfill.
    conn.execute("""
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy', 'old task', 'ready', 1)"
    )
    conn.commit()

    before = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "skills" not in before

    # Run the migrator directly — the same function connect() calls.
    kb._migrate_add_optional_columns(conn)
    after = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "skills" in after, f"migration did not add skills column: {after}"

    # Idempotent: running again must not raise.
    kb._migrate_add_optional_columns(conn)

    # Legacy row has skills=NULL -> Task.skills=None.
    row = conn.execute("SELECT * FROM tasks WHERE id = 'legacy'").fetchone()
    # from_row needs additional columns; build a Task manually via the
    # path from_row takes for a skills NULL/missing.
    keys = set(row.keys())
    assert "skills" in keys
    assert row["skills"] is None
    conn.close()


def test_legacy_spawn_failure_columns_are_copied_not_renamed(tmp_path):
    """Legacy failure counters survive migration without fragile column renames."""
    import sqlite3
    db_path = tmp_path / "legacy-failures.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER,
            tenant TEXT,
            result TEXT,
            idempotency_key TEXT,
            spawn_failures INTEGER NOT NULL DEFAULT 0,
            worker_pid INTEGER,
            last_spawn_error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    # task_events is required: _migrate_add_optional_columns also runs a
    # PRAGMA on it to back-fill the run_id column and raises
    # OperationalError if the table is absent.
    conn.execute(
        "INSERT INTO tasks "
        "(id, title, body, assignee, status, priority, created_by, created_at, "
        "started_at, completed_at, workspace_kind, workspace_path, claim_lock, "
        "claim_expires, tenant, result, idempotency_key, spawn_failures, "
        "worker_pid, last_spawn_error) "
        "VALUES ('legacy', 'old task', NULL, 'default', 'ready', 0, NULL, 1, "
        "NULL, NULL, 'scratch', NULL, NULL, NULL, NULL, NULL, NULL, 4, NULL, "
        "'missing profile')"
    )
    conn.commit()

    kb._migrate_add_optional_columns(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "spawn_failures" in cols
    assert "consecutive_failures" in cols
    assert "last_spawn_error" in cols
    assert "last_failure_error" in cols

    row = conn.execute("SELECT * FROM tasks WHERE id = 'legacy'").fetchone()
    assert row["consecutive_failures"] == 4
    assert row["last_failure_error"] == "missing profile"
    task = kb.Task.from_row(row)
    assert task.consecutive_failures == 4
    assert task.last_failure_error == "missing profile"

    kb._migrate_add_optional_columns(conn)
    row_again = conn.execute("SELECT * FROM tasks WHERE id = 'legacy'").fetchone()
    assert row_again["consecutive_failures"] == 4
    assert row_again["last_failure_error"] == "missing profile"
    conn.close()


def test_legacy_migration_no_legacy_columns_at_all(tmp_path):
    """Scenario A: DB has neither spawn_failures nor consecutive_failures.

    This is the exact crash scenario from issue #20842 — a very old DB that
    predates the spawn_failures column entirely.  The old RENAME COLUMN path
    raised ``sqlite3.OperationalError: no such column: spawn_failures``.
    The ADD-first approach adds consecutive_failures with default 0.
    """
    import sqlite3

    db_path = tmp_path / "ancient.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)
    # task_events is required: _migrate_add_optional_columns also runs a
    # PRAGMA on it to back-fill the run_id column and raises
    # OperationalError if the table is absent.
    conn.execute("""
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('t1', 'ancient task', 'ready', 1)"
    )
    conn.commit()

    # Must not raise (this was the crash before this fix).
    kb._migrate_add_optional_columns(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "consecutive_failures" in cols, "migration must add consecutive_failures"
    assert "last_failure_error" in cols, "migration must add last_failure_error"
    assert "spawn_failures" not in cols, "no legacy column should be synthesised"

    row = conn.execute("SELECT * FROM tasks WHERE id = 't1'").fetchone()
    assert row["consecutive_failures"] == 0
    assert row["last_failure_error"] is None

    # Idempotent second run must not raise either.
    kb._migrate_add_optional_columns(conn)
    row_again = conn.execute("SELECT * FROM tasks WHERE id = 't1'").fetchone()
    assert row_again["consecutive_failures"] == 0
    assert row_again["last_failure_error"] is None
    conn.close()


def test_legacy_migration_both_columns_already_present(tmp_path):
    """Scenario D: DB already has both spawn_failures AND consecutive_failures.

    Represents a partially-migrated DB (e.g. user recovered manually after the
    #20842 crash).  The migration must be a complete no-op and must not
    zero-out the existing counter.
    """
    import sqlite3

    db_path = tmp_path / "partial.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            spawn_failures INTEGER NOT NULL DEFAULT 0,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_spawn_error TEXT,
            last_failure_error TEXT
        )
    """)
    # task_events required for the run_id back-fill PRAGMA inside the migrator.
    conn.execute("""
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, spawn_failures, "
        "consecutive_failures, last_spawn_error, last_failure_error) "
        "VALUES ('t2', 'partial task', 'ready', 1, 2, 3, 'old error', 'new error')"
    )
    conn.commit()

    kb._migrate_add_optional_columns(conn)

    row = conn.execute("SELECT * FROM tasks WHERE id = 't2'").fetchone()
    # consecutive_failures must not be reset by the migration.
    assert row["consecutive_failures"] == 3, "migration must not overwrite existing counter"
    assert row["last_failure_error"] == "new error", "migration must not overwrite existing error"
    # Legacy column is preserved harmlessly.
    assert row["spawn_failures"] == 2

    # Schema must be unchanged — no spurious ADD or DROP.
    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "consecutive_failures" in cols_after
    assert "last_failure_error" in cols_after
    assert "spawn_failures" in cols_after  # legacy preserved

    # Idempotent second run must not modify values or raise.
    kb._migrate_add_optional_columns(conn)
    row_again = conn.execute("SELECT * FROM tasks WHERE id = 't2'").fetchone()
    assert row_again["consecutive_failures"] == 3
    assert row_again["last_failure_error"] == "new error"
    conn.close()


# ---------------------------------------------------------------------------
# Gateway-embedded dispatcher: config, CLI warnings, daemon deprecation stub
# ---------------------------------------------------------------------------

def test_config_default_dispatch_in_gateway_is_true():
    """Default config must enable gateway-embedded dispatch out of the box.
    Flipping this default to false is a user-visible behaviour change and
    should require a conscious migration."""
    from hermes_cli.config import DEFAULT_CONFIG
    kanban = DEFAULT_CONFIG.get("kanban", {})
    assert kanban.get("dispatch_in_gateway") is True, (
        "kanban.dispatch_in_gateway default should be True; got "
        f"{kanban.get('dispatch_in_gateway')!r}"
    )
    interval = kanban.get("dispatch_interval_seconds")
    assert isinstance(interval, (int, float)) and interval >= 1, (
        f"dispatch_interval_seconds must be a positive number, got {interval!r}"
    )


def test_check_dispatcher_presence_silent_when_gateway_running(monkeypatch):
    from hermes_cli import kanban as kb_cli
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 12345)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )
    running, msg = kb_cli._check_dispatcher_presence()
    assert running is True
    # Either empty (if import failed defensively) or includes the pid.
    assert msg == "" or "12345" in msg


def test_check_dispatcher_presence_warns_when_no_gateway(monkeypatch):
    from hermes_cli import kanban as kb_cli
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )
    running, msg = kb_cli._check_dispatcher_presence()
    assert running is False
    assert "hermes gateway start" in msg


def test_check_dispatcher_presence_warns_when_flag_off(monkeypatch):
    """Gateway is up but dispatch_in_gateway=false -> warning."""
    from hermes_cli import kanban as kb_cli
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 999)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"dispatch_in_gateway": False}},
    )
    running, msg = kb_cli._check_dispatcher_presence()
    assert running is False
    assert "dispatch_in_gateway" in msg


def test_check_dispatcher_presence_warns_when_flag_string_false(monkeypatch):
    """Quoted false still means dispatch is off for create warnings."""
    from hermes_cli import kanban as kb_cli
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 999)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"dispatch_in_gateway": "false"}},
    )
    running, msg = kb_cli._check_dispatcher_presence()
    assert running is False
    assert "dispatch_in_gateway" in msg


def test_check_dispatcher_presence_silent_on_probe_error(monkeypatch):
    """If the probe itself errors, we stay silent."""
    from hermes_cli import kanban as kb_cli
    def _raise():
        raise RuntimeError("boom")
    monkeypatch.setattr("gateway.status.get_running_pid", _raise)
    running, msg = kb_cli._check_dispatcher_presence()
    assert running is True
    assert msg == ""


def test_cli_create_warns_when_no_gateway(kanban_home, monkeypatch, capsys):
    """ready+assigned task + no gateway -> warning on stderr."""
    from hermes_cli import kanban as kb_cli
    _write_test_profile(kanban_home, "worker")
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )
    ns = _make_create_ns(title="warn-me", assignee="worker")
    assert kb_cli._cmd_create(ns) == 0
    captured = capsys.readouterr()
    # Stderr has the warning prefix + guidance.
    assert "hermes gateway start" in captured.err


def test_cli_create_silent_when_gateway_up(kanban_home, monkeypatch, capsys):
    """gateway running + dispatch enabled -> no warning."""
    from hermes_cli import kanban as kb_cli
    _write_test_profile(kanban_home, "worker")
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 4242)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )
    ns = _make_create_ns(title="silent", assignee="worker")
    assert kb_cli._cmd_create(ns) == 0
    captured = capsys.readouterr()
    assert "hermes gateway start" not in captured.err


def test_cli_create_no_warn_on_triage(kanban_home, monkeypatch, capsys):
    """Triage tasks can't be dispatched -> no warning."""
    from hermes_cli import kanban as kb_cli
    _write_test_profile(kanban_home, "worker")
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )
    ns = _make_create_ns(title="triage-task", assignee=None, triage=True)
    assert kb_cli._cmd_create(ns) == 0
    err = capsys.readouterr().err
    assert "hermes gateway start" not in err


def test_cli_create_no_warn_unassigned(kanban_home, monkeypatch, capsys):
    """Unassigned tasks can't be dispatched -> no warning."""
    from hermes_cli import kanban as kb_cli
    _write_test_profile(kanban_home, "worker")
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )
    ns = _make_create_ns(title="nobody", assignee=None)
    assert kb_cli._cmd_create(ns) == 0
    err = capsys.readouterr().err
    assert "hermes gateway start" not in err


def test_cli_dispatch_string_false_auto_retry_disabled(monkeypatch):
    """Quoted false must not opt the CLI dispatch path into blocked retries."""
    from hermes_cli import kanban as kb_cli

    class _ConnCtx:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    captured = {}
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"auto_retry_blocked": "false"}},
    )
    monkeypatch.setattr(kb_cli.kb, "connect_closing", lambda: _ConnCtx())

    def _dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            reclaimed=0,
            crashed=[],
            timed_out=[],
            stale=[],
            auto_blocked=[],
            auto_retried_blocked=[],
            promoted=0,
            spawned=[],
            skipped_unassigned=[],
            skipped_nonspawnable=[],
            skipped_per_profile_capped=[],
            auto_assigned_default=[],
        )

    monkeypatch.setattr(kb_cli.kb, "dispatch_once", _dispatch_once)
    args = argparse.Namespace(
        dry_run=False,
        max=None,
        failure_limit=kb.DEFAULT_SPAWN_FAILURE_LIMIT,
        json=False,
    )

    assert kb_cli._cmd_dispatch(args) == 0
    assert captured["auto_retry_blocked"] is False


def test_k11_cli_decompose_failure_increments_counter(kanban_home, capsys):
    """K11 wiring: a deterministic ok=False decompose (task NOT in triage —
    no LLM/network needed) bumps the task's ``decompose_failed`` counter via
    the _cmd_decompose code path."""
    from hermes_cli import kanban as kb_cli
    with kb.connect() as conn:
        # A normal assigned task is created in ``ready`` (not triage), so
        # decompose_task short-circuits to ok=False "not in triage".
        tid = kb.create_task(conn, title="not-triage", assignee="worker")
        assert kb.get_task(conn, tid).status != "triage"
        before = conn.execute(
            "SELECT decompose_failed FROM tasks WHERE id = ?", (tid,),
        ).fetchone()["decompose_failed"]
    assert before == 0

    # Non-success decompose returns 1 (per-task failure rc), not a crash.
    rc = kb_cli._cmd_decompose(_make_decompose_ns(tid))
    assert rc == 1
    assert "not in triage" in capsys.readouterr().err

    with kb.connect() as conn:
        after = conn.execute(
            "SELECT decompose_failed FROM tasks WHERE id = ?", (tid,),
        ).fetchone()["decompose_failed"]
    assert after == 1


def test_cli_daemon_without_force_prints_deprecation_exits_2(kanban_home, capsys):
    """`hermes kanban daemon` (no --force) is a deprecation stub."""
    from hermes_cli import kanban as kb_cli
    ns = argparse.Namespace(
        force=False, interval=60.0, max=None, failure_limit=3,
        pidfile=None, verbose=False,
    )
    rc = kb_cli._cmd_daemon(ns)
    assert rc == 2
    err = capsys.readouterr().err
    assert "DEPRECATED" in err
    assert "hermes gateway start" in err


def test_cli_daemon_help_marks_deprecated():
    """The argparse help string on `daemon` mentions deprecation so users
    scanning `--help` see the migration before running the stub."""
    import argparse as _ap
    from hermes_cli import kanban as kb_cli
    root = _ap.ArgumentParser()
    subs = root.add_subparsers()
    kb_cli.build_parser(subs)
    # Walk the subparser tree to find the daemon action.
    daemon_help = None
    for action in root._actions:
        if isinstance(action, _ap._SubParsersAction):
            for name, parser in action.choices.items():
                if name == "kanban":
                    for sub_action in parser._actions:
                        if isinstance(sub_action, _ap._SubParsersAction):
                            for sname, _ in sub_action.choices.items():
                                if sname == "daemon":
                                    daemon_help = sub_action._choices_actions
                                    break
    # _choices_actions is a list of _ChoicesPseudoAction-like objects with .help
    found_deprecation = False
    if daemon_help:
        for act in daemon_help:
            if getattr(act, "dest", "") == "daemon":
                if "DEPRECATED" in (act.help or ""):
                    found_deprecation = True
                    break
    assert found_deprecation, (
        "daemon subparser help should be marked DEPRECATED so users see "
        "the migration guidance in `hermes kanban --help` output"
    )


# ---------------------------------------------------------------------------
# Gateway embedded dispatcher watcher
# ---------------------------------------------------------------------------

def test_gateway_dispatcher_watcher_respects_config_flag_off(monkeypatch):
    """dispatch_in_gateway=false -> watcher exits fast, no loop."""
    import asyncio
    from gateway.run import GatewayRunner
    import hermes_cli.config as _cfg_mod

    runner = object.__new__(GatewayRunner)
    runner._running = True

    monkeypatch.setattr(
        _cfg_mod, "load_config",
        lambda: {"kanban": {"dispatch_in_gateway": False}},
    )
    asyncio.run(
        asyncio.wait_for(
            runner._kanban_dispatcher_watcher(),
            timeout=3.0,
        )
    )


def test_gateway_dispatcher_watcher_respects_env_override(monkeypatch):
    """HERMES_KANBAN_DISPATCH_IN_GATEWAY=0 disables without touching config."""
    import asyncio
    from gateway.run import GatewayRunner
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "0")

    runner = object.__new__(GatewayRunner)
    runner._running = True
    asyncio.run(
        asyncio.wait_for(
            runner._kanban_dispatcher_watcher(),
            timeout=3.0,
        )
    )


def test_gateway_dispatcher_watcher_env_truthy_uses_config(monkeypatch):
    """Truthy env value doesn't force-enable — config still decides.
    (We only treat explicit falses as an override; unset or truthy
    defers to config.)"""
    import asyncio
    from gateway.run import GatewayRunner
    import hermes_cli.config as _cfg_mod

    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "yes")
    monkeypatch.setattr(
        _cfg_mod, "load_config",
        lambda: {"kanban": {"dispatch_in_gateway": False}},
    )

    runner = object.__new__(GatewayRunner)
    runner._running = True
    # config says false, env is truthy — watcher should still exit
    # (because config is authoritative when env isn't a falsey override).
    asyncio.run(
        asyncio.wait_for(
            runner._kanban_dispatcher_watcher(),
            timeout=3.0,
        )
    )


def test_gateway_dispatcher_invalid_repo_cap_uses_default(
    kanban_home, monkeypatch, caplog
):
    """Bad kanban.max_concurrent_per_repo config must not kill the dispatcher."""
    import asyncio
    import logging

    from gateway.run import GatewayRunner
    import hermes_cli.config as _cfg_mod

    runner = object.__new__(GatewayRunner)
    runner._running = True
    captured = {}

    monkeypatch.setattr(
        _cfg_mod,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
                "max_concurrent_per_repo": "many",
            }
        },
    )
    monkeypatch.setattr(
        kb,
        "list_boards",
        lambda include_archived=False: [{"slug": kb.DEFAULT_BOARD}],
    )

    def _dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        runner._running = False
        return SimpleNamespace(
            spawned=[],
            reclaimed=0,
            crashed=[],
            timed_out=[],
            promoted=0,
            auto_blocked=[],
        )

    async def _sleep(_delay):
        return None

    monkeypatch.setattr(kb, "dispatch_once", _dispatch_once)
    monkeypatch.setattr("gateway.kanban_watchers.asyncio.sleep", _sleep)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        asyncio.run(
            asyncio.wait_for(
                runner._kanban_dispatcher_watcher(),
                timeout=3.0,
            )
        )

    assert captured["max_concurrent_per_repo"] == 1
    assert any(
        "invalid kanban.max_concurrent_per_repo='many'; using default 1"
        in record.getMessage()
        for record in caplog.records
    )


def test_gateway_dispatcher_invalid_max_spawn_uses_default(kanban_home, monkeypatch):
    """Bad kanban.max_spawn config must not flow into dispatch comparisons."""
    import asyncio

    from gateway.run import GatewayRunner
    import hermes_cli.config as _cfg_mod

    runner = object.__new__(GatewayRunner)
    runner._running = True
    captured = {}

    monkeypatch.setattr(
        _cfg_mod,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
                "max_spawn": "many",
            }
        },
    )
    monkeypatch.setattr(
        kb,
        "list_boards",
        lambda include_archived=False: [{"slug": kb.DEFAULT_BOARD}],
    )

    def _dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        runner._running = False
        return SimpleNamespace(
            spawned=[],
            reclaimed=0,
            crashed=[],
            timed_out=[],
            promoted=0,
            auto_blocked=[],
        )

    async def _sleep(_delay):
        return None

    monkeypatch.setattr(kb, "dispatch_once", _dispatch_once)
    monkeypatch.setattr("gateway.kanban_watchers.asyncio.sleep", _sleep)

    asyncio.run(
        asyncio.wait_for(
            runner._kanban_dispatcher_watcher(),
            timeout=3.0,
        )
    )

    assert captured["max_spawn"] is None


def test_gateway_dispatcher_string_false_booleans_disable_flags(
    kanban_home, monkeypatch
):
    """Quoted false values must not enable retry/serialization flags."""
    import asyncio

    from gateway.run import GatewayRunner
    import hermes_cli.config as _cfg_mod

    runner = object.__new__(GatewayRunner)
    runner._running = True
    captured = {}

    monkeypatch.setattr(
        _cfg_mod,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
                "auto_retry_blocked": "false",
                "serialize_by_repo": "false",
            }
        },
    )
    monkeypatch.setattr(
        kb,
        "list_boards",
        lambda include_archived=False: [{"slug": kb.DEFAULT_BOARD}],
    )

    def _dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        runner._running = False
        return SimpleNamespace(
            spawned=[],
            reclaimed=0,
            crashed=[],
            timed_out=[],
            promoted=0,
            auto_blocked=[],
        )

    async def _sleep(_delay):
        return None

    monkeypatch.setattr(kb, "dispatch_once", _dispatch_once)
    monkeypatch.setattr("gateway.kanban_watchers.asyncio.sleep", _sleep)

    asyncio.run(
        asyncio.wait_for(
            runner._kanban_dispatcher_watcher(),
            timeout=3.0,
        )
    )

    assert captured["auto_retry_blocked"] is False
    assert captured["serialize_by_repo"] is False


@pytest.mark.parametrize("corrupt_exc", ["sqlite", "guard"])
def test_gateway_dispatcher_disables_corrupt_board_without_traceback(
    monkeypatch, tmp_path, caplog, corrupt_exc
):
    """Corrupt board DBs log one actionable error and stop retrying per tick."""
    import asyncio
    import logging
    import sqlite3

    from gateway.run import GatewayRunner
    import hermes_cli.config as _cfg_mod
    import hermes_cli.kanban_db as _kb

    runner = object.__new__(GatewayRunner)
    runner._running = True
    corrupt_db = tmp_path / "kanban.db"
    corrupt_db.write_text("not sqlite", encoding="utf-8")

    monkeypatch.setattr(
        _cfg_mod,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
            }
        },
    )
    monkeypatch.setattr(
        _kb,
        "list_boards",
        lambda include_archived=False: [{"slug": _kb.DEFAULT_BOARD}],
    )
    monkeypatch.setattr(
        _kb,
        "read_board_metadata",
        lambda slug: {"slug": slug},
    )
    monkeypatch.setattr(_kb, "kanban_db_path", lambda board=None: corrupt_db)

    calls = {"connect": 0, "to_thread": 0}

    def _connect(*args, **kwargs):
        calls["connect"] += 1
        if corrupt_exc == "guard":
            raise _kb.KanbanDbCorruptError(
                corrupt_db,
                corrupt_db.with_suffix(".db.corrupt.test.bak"),
                "sqlite refused to open file: database disk image is malformed",
            )
        raise sqlite3.DatabaseError("file is not a database")

    async def _to_thread(fn, *args, **kwargs):
        # PR salvage (#32857 commit 7): the dispatcher now reaps zombies at
        # the top of each tick via ``asyncio.to_thread(_kb.reap_worker_zombies)``
        # BEFORE the per-board tick work. K16 added cost backfill and Sprint2
        # adds the dispatcher heartbeat, so each full tick now issues 5
        # ``to_thread`` calls (reaper + ``_tick_once`` + ``_ready_nonempty`` +
        # cost backfill + heartbeat). This counter must reach 10 to allow the
        # same 2 dispatch ticks the pre-reaper test expected at 4.
        calls["to_thread"] += 1
        result = fn(*args, **kwargs)
        if calls["to_thread"] >= 10:
            runner._running = False
        return result

    async def _sleep(_delay):
        return None

    monkeypatch.setattr(_kb, "connect", _connect)
    monkeypatch.setattr("gateway.run.asyncio.to_thread", _to_thread)
    monkeypatch.setattr("gateway.run.asyncio.sleep", _sleep)

    with caplog.at_level(logging.ERROR, logger="gateway.run"):
        asyncio.run(
            asyncio.wait_for(
                runner._kanban_dispatcher_watcher(),
                timeout=3.0,
            )
        )

    messages = [record.getMessage() for record in caplog.records]
    assert sum("not a valid SQLite database" in msg for msg in messages) == 1
    assert not any("tick failed on board" in msg for msg in messages)
    assert not any(record.exc_info for record in caplog.records)
    # First tick connect (dispatch) + two probes per `_has_ready_work` call
    # (ready then review, both via _kb.connect). The second dispatch tick
    # skips the dispatch connect because the corrupt board fingerprint is
    # disabled, but the ready/review probes still each connect. PR f55d94a1e
    # added the review-column probe alongside the existing ready-column
    # probe, bumping this from 3 → 5. K16 added a per-tick, fail-soft cost
    # backfill (`connect_closing` → one more `_kb.connect` per tick); on a
    # corrupt board that connect raises and is swallowed, so it adds exactly
    # one connect to each of the two ticks: 5 → 7. Sprint2's heartbeat writes
    # one aggregate state file per tick and probes the same board list
    # fail-soft, adding one swallowed connect per tick: 7 → 9.
    assert calls["connect"] == 9


def test_gateway_dispatcher_retries_corrupt_board_after_quarantine(
    monkeypatch, tmp_path, caplog
):
    """A corrupt-looking board is retried after the quarantine TTL expires."""
    import asyncio
    import inspect
    import logging
    import sqlite3

    from gateway.run import GatewayRunner
    import hermes_cli.config as _cfg_mod
    import hermes_cli.kanban_db as _kb

    runner = object.__new__(GatewayRunner)
    runner._running = True
    corrupt_db = tmp_path / "kanban.db"
    corrupt_db.write_text("not sqlite", encoding="utf-8")

    monkeypatch.setattr(
        _cfg_mod,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
            }
        },
    )
    monkeypatch.setattr(
        _kb,
        "list_boards",
        lambda include_archived=False: [{"slug": _kb.DEFAULT_BOARD}],
    )
    monkeypatch.setattr(
        _kb,
        "read_board_metadata",
        lambda slug: {"slug": slug},
    )
    monkeypatch.setattr(_kb, "kanban_db_path", lambda board=None: corrupt_db)

    real_monotonic = time.monotonic
    time_values = iter([1000.0, 1001.0, 1301.0, 1301.0])

    def _monotonic_for_gateway_dispatcher():
        caller = inspect.currentframe().f_back  # type: ignore[union-attr]
        code = caller.f_code if caller is not None else None
        filename = code.co_filename if code is not None else ""
        # The kanban dispatcher/notifier watcher loops were extracted from
        # gateway/run.py into gateway/kanban_watchers.py (god-file Phase 3),
        # so accept either filename for the time-travel mock.
        if filename.endswith("gateway/run.py") or filename.endswith("gateway/kanban_watchers.py"):
            return next(time_values, 1301.0)
        return real_monotonic()

    monkeypatch.setattr("gateway.run.time.monotonic", _monotonic_for_gateway_dispatcher)
    monkeypatch.setattr("gateway.kanban_watchers.time.monotonic", _monotonic_for_gateway_dispatcher)

    calls = {"tick": 0}

    def _connect(*args, **kwargs):
        raise sqlite3.DatabaseError("file is not a database")

    async def _to_thread(fn, *args, **kwargs):
        result = fn(*args, **kwargs)
        if getattr(fn, "__name__", "") == "_tick_once":
            calls["tick"] += 1
            if calls["tick"] >= 3:
                runner._running = False
        return result

    async def _sleep(_delay):
        return None

    monkeypatch.setattr(_kb, "connect", _connect)
    monkeypatch.setattr("gateway.run.asyncio.to_thread", _to_thread)
    monkeypatch.setattr("gateway.run.asyncio.sleep", _sleep)

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        asyncio.run(
            asyncio.wait_for(
                runner._kanban_dispatcher_watcher(),
                timeout=3.0,
            )
        )

    messages = [record.getMessage() for record in caplog.records]
    assert sum("not a valid SQLite database" in msg for msg in messages) == 2
    assert any("database fingerprint unchanged" in msg for msg in messages)
    assert calls["tick"] == 3

