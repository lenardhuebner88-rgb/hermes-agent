"""Tests for ``HERMES_SANDBOX_MODE`` opt-in worker-DB isolation.

The motivating incident (feedback memory ``hermes-worker-env-live-db-leak``,
2026-05-27): coder workers inherit ``HERMES_KANBAN_DB`` /
``HERMES_KANBAN_BOARD`` from the dispatcher, so any
``hermes kanban create`` invoked from a sample/test script ends up on
the LIVE production board. The hardening-sprint introduces
``HERMES_SANDBOX_MODE=1`` as an explicit opt-in redirect to an
ephemeral per-``HERMES_HOME`` sandbox DB so worker scripts can call the
kanban CLI without the live-DB-leak footgun.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _clean_kanban_env(monkeypatch):
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_SANDBOX_MODE",
    ):
        monkeypatch.delenv(var, raising=False)


def test_sandbox_mode_disabled_uses_default_db_path(hermes_home, monkeypatch):
    _clean_kanban_env(monkeypatch)
    path = kb.kanban_db_path()
    assert path == hermes_home / "kanban.db"
    assert ".kanban-sandbox" not in str(path)


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "Yes", "on"])
def test_sandbox_mode_truthy_values_redirect(hermes_home, monkeypatch, truthy):
    _clean_kanban_env(monkeypatch)
    monkeypatch.setenv("HERMES_SANDBOX_MODE", truthy)
    path = kb.kanban_db_path()
    assert path == hermes_home / ".kanban-sandbox" / "default.db"


@pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off", "random"])
def test_sandbox_mode_falsy_values_stay_live(hermes_home, monkeypatch, falsy):
    _clean_kanban_env(monkeypatch)
    monkeypatch.setenv("HERMES_SANDBOX_MODE", falsy)
    path = kb.kanban_db_path()
    assert path == hermes_home / "kanban.db"


def test_sandbox_mode_overrides_inherited_hermes_kanban_db(hermes_home, monkeypatch):
    """The whole point of sandbox-mode: an inherited live-board pin
    (HERMES_KANBAN_DB pointing at ~/.hermes/kanban.db) must NOT win
    over the sandbox redirect. Otherwise the worker-env leak still
    bites scripts running under HERMES_SANDBOX_MODE=1.
    """
    _clean_kanban_env(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_DB", "/some/inherited/live/kanban.db")
    monkeypatch.setenv("HERMES_SANDBOX_MODE", "1")

    path = kb.kanban_db_path()
    assert path == hermes_home / ".kanban-sandbox" / "default.db"
    assert str(path) != "/some/inherited/live/kanban.db"


def test_sandbox_mode_ignores_inherited_hermes_kanban_board(hermes_home, monkeypatch):
    """The inherited HERMES_KANBAN_BOARD env var must NOT pull the
    live-board name into the sandbox file path. Sandbox-mode is opt-in
    isolation; it has its own default-board namespace.
    """
    _clean_kanban_env(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "production-fleet")
    monkeypatch.setenv("HERMES_SANDBOX_MODE", "1")

    path = kb.kanban_db_path()
    assert path == hermes_home / ".kanban-sandbox" / "default.db"
    assert "production-fleet" not in str(path)


def test_sandbox_mode_workspaces_root_also_redirects(hermes_home, monkeypatch):
    """The workspaces root must also redirect under sandbox-mode, so
    that scratch workspaces created by sandboxed worker-scripts don't
    pollute the production ``~/.hermes/kanban/workspaces/`` tree.
    """
    _clean_kanban_env(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", "/inherited/live/workspaces")
    monkeypatch.setenv("HERMES_SANDBOX_MODE", "1")

    root = kb.workspaces_root()
    assert root == hermes_home / ".kanban-sandbox" / "workspaces"
    assert "/inherited/live" not in str(root)


def test_sandbox_create_and_list_round_trip(hermes_home, monkeypatch):
    """End-to-end: enable sandbox-mode, create a task, list tasks via
    the DB layer — the task must be visible inside the sandbox and the
    LIVE kanban.db at ``<root>/kanban.db`` must not exist on disk.
    """
    _clean_kanban_env(monkeypatch)
    monkeypatch.setenv("HERMES_SANDBOX_MODE", "1")

    # Force re-init so any previously cached path doesn't shadow the
    # sandbox redirect.
    sandbox_path = kb.kanban_db_path()
    kb._INITIALIZED_PATHS.discard(str(sandbox_path.resolve()))
    kb.init_db()

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="sandbox-smoke", body="from a test")
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.title == "sandbox-smoke"

    # Sandbox file exists; live kanban.db does NOT.
    assert sandbox_path.exists()
    assert sandbox_path == hermes_home / ".kanban-sandbox" / "default.db"
    assert not (hermes_home / "kanban.db").exists()


def test_unset_sandbox_does_not_see_sandboxed_task(hermes_home, monkeypatch):
    """Symmetric guard: a task created in the sandbox must NOT show up
    in the live DB after unsetting HERMES_SANDBOX_MODE. This is what
    protects production from the worker-env leak.
    """
    _clean_kanban_env(monkeypatch)

    # Step 1: create a task inside the sandbox.
    monkeypatch.setenv("HERMES_SANDBOX_MODE", "1")
    sandbox_path = kb.kanban_db_path()
    kb._INITIALIZED_PATHS.discard(str(sandbox_path.resolve()))
    kb.init_db()
    with kb.connect() as conn:
        sandbox_tid = kb.create_task(conn, title="sandboxed-only")

    # Step 2: turn sandbox off; live DB is a different file, no task.
    monkeypatch.delenv("HERMES_SANDBOX_MODE", raising=False)
    live_path = kb.kanban_db_path()
    assert live_path != sandbox_path
    kb._INITIALIZED_PATHS.discard(str(live_path.resolve()))
    kb.init_db()
    with kb.connect() as conn:
        assert kb.get_task(conn, sandbox_tid) is None


def test_sandbox_mode_helper_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("HERMES_SANDBOX_MODE", "TrUe")
    assert kb._sandbox_mode_enabled() is True

    monkeypatch.setenv("HERMES_SANDBOX_MODE", "  on  ")
    assert kb._sandbox_mode_enabled() is True

    monkeypatch.setenv("HERMES_SANDBOX_MODE", "nope")
    assert kb._sandbox_mode_enabled() is False



# ---------------------------------------------------------------------------
# Foreign inherited overrides (live-DB-leak recurrence 2026-07-22)
# ---------------------------------------------------------------------------


def test_foreign_inherited_db_override_is_dropped(hermes_home, monkeypatch):
    """A probe that moved HERMES_HOME must not write through the inherited
    live ``HERMES_KANBAN_DB`` — the foreign override is dropped (recurrence
    2026-07-22: worker probe created t/w/w2 on the production board)."""
    _clean_kanban_env(monkeypatch)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", "/live/root/kanban.db")
    assert kb.kanban_db_path() == hermes_home / "kanban.db"


def test_native_inherited_db_override_is_honoured(hermes_home, monkeypatch):
    """The dispatcher→worker handoff pins a DB *inside* the active home —
    that override stays authoritative."""
    _clean_kanban_env(monkeypatch)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    native = hermes_home / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(native))
    assert kb.kanban_db_path() == native


def test_db_override_honoured_without_hermes_home(monkeypatch, tmp_path):
    """Legacy callers that pin the DB directly without HERMES_HOME keep working."""
    _clean_kanban_env(monkeypatch)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    foreign = tmp_path / "elsewhere" / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(foreign))
    assert kb.kanban_db_path() == foreign


def test_db_override_honoured_with_explicit_kanban_home(hermes_home, monkeypatch, tmp_path):
    """Docker/custom deployments that explicitly pin HERMES_KANBAN_HOME may
    place the DB outside the active home."""
    _clean_kanban_env(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "umbrella"))
    foreign = tmp_path / "elsewhere" / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(foreign))
    assert kb.kanban_db_path() == foreign


def test_foreign_workspaces_override_is_dropped(hermes_home, monkeypatch):
    _clean_kanban_env(monkeypatch)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", "/live/root/kanban/workspaces")
    assert kb.workspaces_root() == hermes_home / "kanban" / "workspaces"


def test_foreign_attachments_override_is_dropped(hermes_home, monkeypatch):
    _clean_kanban_env(monkeypatch)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", "/live/root/kanban/attachments")
    assert kb.attachments_root() == hermes_home / "kanban" / "attachments"


def test_worker_probe_cannot_leak_into_live_board(hermes_home, monkeypatch, tmp_path):
    """End-to-end regression for the t/w/w2 incident: inherited live
    ``HERMES_KANBAN_DB`` + temp HERMES_HOME → all writes land in the temp
    home; the live file is never even created."""
    _clean_kanban_env(monkeypatch)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    live = tmp_path / "live"
    live.mkdir()
    live_db = live / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live_db))
    kb.init_db()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="probe", assignee="coder")
        assert kb.get_task(conn, tid) is not None
    assert not live_db.exists()
