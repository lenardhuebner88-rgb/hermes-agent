"""Kanban DB tests: paths spawn.

Split from test_kanban_db.py (pure move; no test logic changes).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import types
import unittest.mock
from pathlib import Path
import pytest
from hermes_cli import kanban_context as context
from hermes_cli import kanban_db as kb

# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------

def test_tenant_column_filters_listings(kanban_home):
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="a1", tenant="biz-a")
        kb.create_task(conn, title="b1", tenant="biz-b")
        kb.create_task(conn, title="shared")  # no tenant
        biz_a = kb.list_tasks(conn, tenant="biz-a")
        biz_b = kb.list_tasks(conn, tenant="biz-b")
    assert [t.title for t in biz_a] == ["a1"]
    assert [t.title for t in biz_b] == ["b1"]


def test_list_tasks_filters_workflow_template_and_step(kanban_home):
    with kb.connect_closing() as conn:
        ta = kb.create_task(conn, title="alpha")
        tb = kb.create_task(conn, title="beta")
        conn.execute(
            "UPDATE tasks SET workflow_template_id=?, current_step_key=? WHERE id=?",
            ("wf1", "step_x", ta),
        )
        conn.execute(
            "UPDATE tasks SET workflow_template_id=?, current_step_key=? WHERE id=?",
            ("wf1", "step_y", tb),
        )
        conn.commit()
        by_wf = kb.list_tasks(conn, workflow_template_id="wf1")
        by_step = kb.list_tasks(conn, current_step_key="step_x")
    assert {x.id for x in by_wf} == {ta, tb}
    assert [x.id for x in by_step] == [ta]


def test_list_runs_state_filter_requires_pair_and_valid_type(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="both"):
            kb.list_runs(conn, tid, state_type="status", state_name=None)
        with pytest.raises(ValueError, match="both"):
            kb.list_runs(conn, tid, state_type=None, state_name="done")
        with pytest.raises(ValueError, match="state_type"):
            kb.list_runs(conn, tid, state_type="nope", state_name="done")


def test_list_runs_filters_by_outcome_value(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        kb.complete_task(conn, tid, summary="ok")
        matching = kb.list_runs(conn, tid, state_type="outcome", state_name="completed")
        empty = kb.list_runs(conn, tid, state_type="outcome", state_name="blocked")
    assert matching
    assert not empty


def test_tenant_propagates_to_events(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="tenant-task", tenant="biz-a")
        events = kb.list_events(conn, t)
    # The "created" event should have tenant in its payload.
    created = [e for e in events if e.kind == "created"]
    assert created and created[0].payload.get("tenant") == "biz-a"


# ---------------------------------------------------------------------------
# Originating session id (ACP propagation)
# ---------------------------------------------------------------------------

def test_create_task_stamps_session_id(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="from chat", session_id="acp-sess-123"
        )
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.session_id == "acp-sess-123"


def test_create_task_session_id_defaults_to_none(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cli-created")
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.session_id is None


def test_session_id_filters_listings(kanban_home):
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="s1-a", session_id="sess-1")
        kb.create_task(conn, title="s1-b", session_id="sess-1")
        kb.create_task(conn, title="s2-a", session_id="sess-2")
        kb.create_task(conn, title="cli-only")  # no session
        sess1 = kb.list_tasks(conn, session_id="sess-1")
        sess2 = kb.list_tasks(conn, session_id="sess-2")
        unscoped = kb.list_tasks(conn)
    assert sorted(t.title for t in sess1) == ["s1-a", "s1-b"]
    assert [t.title for t in sess2] == ["s2-a"]
    # Unscoped list still returns everything (legacy NULL rows visible).
    assert len(unscoped) == 4


def test_session_id_index_exists(kanban_home):
    """The migration creates an index on session_id for cheap per-session
    list queries on busy boards. Without it, a chat-scoped poll would
    full-scan the tasks table."""
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='tasks'"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert "idx_tasks_session_id" in names


def test_session_id_compose_with_tenant_filter(kanban_home):
    """A client may want both `tenant=scarf:foo` AND `session=acp-x` —
    the filters must AND, not replace."""
    with kb.connect_closing() as conn:
        kb.create_task(
            conn, title="match", tenant="scarf:foo", session_id="acp-x"
        )
        kb.create_task(
            conn, title="wrong-tenant", tenant="other", session_id="acp-x"
        )
        kb.create_task(
            conn, title="wrong-session",
            tenant="scarf:foo", session_id="acp-y",
        )
        rows = kb.list_tasks(
            conn, tenant="scarf:foo", session_id="acp-x"
        )
    assert [t.title for t in rows] == ["match"]


# ---------------------------------------------------------------------------
# Shared-board path resolution (issue #19348)
#
# The kanban board is a cross-profile coordination primitive: a worker
# spawned with `hermes -p <profile>` must read/write the same kanban.db
# as the dispatcher that claimed the task. These tests exercise the
# path-resolution layer directly and would have caught the regression
# where `kanban_db_path()` resolved to the active profile's HERMES_HOME.
# ---------------------------------------------------------------------------

class TestSharedBoardPaths:
    """`kanban_home`/`kanban_db_path`/`workspaces_root`/`worker_log_path`
    must anchor at the **shared root**, not the active profile's HERMES_HOME."""

    def _set_home(self, monkeypatch, tmp_path, hermes_home):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)

    def test_default_install_anchors_at_home_dot_hermes(
        self, tmp_path, monkeypatch
    ):
        # Standard install: HERMES_HOME == ~/.hermes, no profile active.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        self._set_home(monkeypatch, tmp_path, default_home)

        assert kb.kanban_home() == default_home
        assert kb.kanban_db_path() == default_home / "kanban.db"
        assert kb.workspaces_root() == default_home / "kanban" / "workspaces"
        assert (
            kb.worker_log_path("t_demo")
            == default_home / "kanban" / "logs" / "t_demo.log"
        )

    def test_profile_worker_resolves_to_shared_root(
        self, tmp_path, monkeypatch
    ):
        # Reproduces the bug: dispatcher uses ~/.hermes/kanban.db,
        # worker spawned with -p <profile> previously resolved to
        # ~/.hermes/profiles/<profile>/kanban.db. After the fix both
        # converge on ~/.hermes/kanban.db.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        profile_home = default_home / "profiles" / "nehemiahkanban"
        profile_home.mkdir(parents=True)
        self._set_home(monkeypatch, tmp_path, profile_home)

        # All four resolvers must anchor at the shared root, not the
        # profile-local HERMES_HOME.
        assert kb.kanban_home() == default_home
        assert kb.kanban_db_path() == default_home / "kanban.db"
        assert kb.workspaces_root() == default_home / "kanban" / "workspaces"
        assert (
            kb.worker_log_path("t_0d214f19")
            == default_home / "kanban" / "logs" / "t_0d214f19.log"
        )

        # Sanity: the profile-local path that used to be returned is
        # explicitly NOT what we resolve to anymore.
        assert kb.kanban_db_path() != profile_home / "kanban.db"

    def test_dispatcher_and_profile_worker_converge(
        self, tmp_path, monkeypatch
    ):
        # End-to-end convergence: resolve the path under each side's
        # HERMES_HOME and confirm equality. This is the property the
        # dispatcher/worker handoff actually depends on.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        profile_home = default_home / "profiles" / "coder"
        profile_home.mkdir(parents=True)

        # Dispatcher's perspective.
        self._set_home(monkeypatch, tmp_path, default_home)
        dispatcher_db = kb.kanban_db_path()
        dispatcher_ws = kb.workspaces_root()
        dispatcher_log = kb.worker_log_path("t_handoff")

        # Worker's perspective (profile activated by `hermes -p coder`).
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        worker_db = kb.kanban_db_path()
        worker_ws = kb.workspaces_root()
        worker_log = kb.worker_log_path("t_handoff")

        assert dispatcher_db == worker_db
        assert dispatcher_ws == worker_ws
        assert dispatcher_log == worker_log

    def test_docker_custom_hermes_home_uses_env_path_directly(
        self, tmp_path, monkeypatch
    ):
        # Docker / custom deployment: HERMES_HOME points outside ~/.hermes.
        # `get_default_hermes_root()` returns env_home directly when it
        # is not a `<root>/profiles/<name>` shape and not under
        # `Path.home() / ".hermes"`.
        custom_root = tmp_path / "opt" / "hermes"
        custom_root.mkdir(parents=True)
        self._set_home(monkeypatch, tmp_path, custom_root)

        assert kb.kanban_home() == custom_root
        assert kb.kanban_db_path() == custom_root / "kanban.db"

    def test_docker_profile_layout_uses_grandparent(
        self, tmp_path, monkeypatch
    ):
        # Docker profile shape: HERMES_HOME=/opt/hermes/profiles/coder;
        # `get_default_hermes_root()` walks up to /opt/hermes because
        # the immediate parent dir is named "profiles".
        custom_root = tmp_path / "opt" / "hermes"
        profile = custom_root / "profiles" / "coder"
        profile.mkdir(parents=True)
        self._set_home(monkeypatch, tmp_path, profile)

        assert kb.kanban_home() == custom_root
        assert kb.kanban_db_path() == custom_root / "kanban.db"

    def test_explicit_override_via_hermes_kanban_home(
        self, tmp_path, monkeypatch
    ):
        # Explicit override: HERMES_KANBAN_HOME beats every other
        # resolution rule.
        default_home = tmp_path / ".hermes"
        profile_home = default_home / "profiles" / "any"
        profile_home.mkdir(parents=True)
        override = tmp_path / "shared-board"
        override.mkdir()

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(override))

        assert kb.kanban_home() == override
        assert kb.kanban_db_path() == override / "kanban.db"
        assert kb.workspaces_root() == override / "kanban" / "workspaces"

    def test_empty_override_falls_through(self, tmp_path, monkeypatch):
        # Empty/whitespace override is treated as unset.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(default_home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", "   ")

        assert kb.kanban_home() == default_home

    def test_dispatcher_and_worker_share_a_real_database(
        self, tmp_path, monkeypatch
    ):
        # Belt-and-suspenders: round-trip a task across the two
        # HERMES_HOME perspectives via a real SQLite file. Without the
        # fix the worker would open a different file and see no rows.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        profile_home = default_home / "profiles" / "nehemiahkanban"
        profile_home.mkdir(parents=True)

        # Dispatcher creates the board and a task.
        self._set_home(monkeypatch, tmp_path, default_home)
        kb.init_db()
        with kb.connect_closing() as conn:
            task_id = kb.create_task(conn, title="cross-profile")

        # Worker switches to the profile HERMES_HOME and reads.
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        with kb.connect_closing() as conn:
            task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.title == "cross-profile"

    def test_hermes_kanban_db_pin_beats_kanban_home(
        self, tmp_path, monkeypatch
    ):
        # HERMES_KANBAN_DB pins the file path directly and beats both
        # HERMES_KANBAN_HOME and the `get_default_hermes_root()` path.
        # This is the env the dispatcher injects into workers.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        umbrella = tmp_path / "umbrella"
        umbrella.mkdir()
        pinned_db = tmp_path / "pinned" / "board.db"
        pinned_db.parent.mkdir()

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(default_home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(umbrella))
        monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned_db))

        assert kb.kanban_db_path() == pinned_db
        # workspaces_root still follows HERMES_KANBAN_HOME -- the pins
        # are independent.
        assert kb.workspaces_root() == umbrella / "kanban" / "workspaces"

    def test_hermes_kanban_workspaces_root_pin_beats_kanban_home(
        self, tmp_path, monkeypatch
    ):
        # HERMES_KANBAN_WORKSPACES_ROOT pins the workspaces root directly.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        umbrella = tmp_path / "umbrella"
        umbrella.mkdir()
        pinned_ws = tmp_path / "pinned-workspaces"
        pinned_ws.mkdir()

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(default_home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(umbrella))
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(pinned_ws))

        assert kb.workspaces_root() == pinned_ws
        # kanban_db_path still follows HERMES_KANBAN_HOME.
        assert kb.kanban_db_path() == umbrella / "kanban.db"

    def test_empty_per_path_overrides_fall_through(
        self, tmp_path, monkeypatch
    ):
        # Empty/whitespace pins are treated as unset, same as
        # HERMES_KANBAN_HOME.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(default_home))
        monkeypatch.setenv("HERMES_KANBAN_DB", "   ")
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", "")

        assert kb.kanban_db_path() == default_home / "kanban.db"
        assert kb.workspaces_root() == default_home / "kanban" / "workspaces"

    def test_dispatcher_spawn_injects_kanban_db_and_workspaces_root(
        self, tmp_path, monkeypatch
    ):
        # The dispatcher's `_default_spawn` must inject HERMES_KANBAN_DB
        # and HERMES_KANBAN_WORKSPACES_ROOT into the worker env so the
        # worker converges on the dispatcher's paths even when the
        # `-p <profile>` flag rewrites HERMES_HOME.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        self._set_home(monkeypatch, tmp_path, default_home)

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                captured["env"] = kwargs.get("env", {})
                self.pid = 4242

        monkeypatch.setattr("subprocess.Popen", _FakePopen)

        task = kb.Task(
            id="t_dispatch_env",
            title="x",
            body=None,
            assignee="coder",
            status="ready",
            priority=0,
            created_by=None,
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "ws"),
            claim_lock=None,
            claim_expires=None,
            tenant=None,
            branch_name="wt/t_dispatch_env",
        )
        kb._default_spawn(task, str(tmp_path / "ws"))

        env = captured["env"]
        assert env["HERMES_KANBAN_DB"] == str(default_home / "kanban.db")
        assert env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(
            default_home / "kanban" / "workspaces"
        )
        assert env["HERMES_KANBAN_TASK"] == "t_dispatch_env"
        assert env["HERMES_KANBAN_BRANCH"] == "wt/t_dispatch_env"


# ---------------------------------------------------------------------------
# K13 — claude-CLI worker spawn (claude -p) early branch in _default_spawn
# ---------------------------------------------------------------------------

class TestClaudeCliWorkerSpawn:
    """`_is_claude_cli_profile` / `_spawn_claude_worker` divert flagged
    profiles to the `claude` CLI while leaving the default hermes spawn
    path byte-identical."""

    def _set_home(self, monkeypatch, tmp_path, hermes_home):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)

    def _make_task(self, tmp_path, *, assignee="coder", model_override=None):
        return kb.Task(
            id="t_claude_cli",
            title="ship the widget",
            body="implement the widget and run the tests",
            assignee=assignee,
            status="ready",
            priority=0,
            created_by=None,
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "ws"),
            claim_lock=None,
            claim_expires=None,
            tenant=None,
            branch_name="wt/t_claude_cli",
            model_override=model_override,
        )

    # --- _is_claude_cli_profile -------------------------------------------

    def test_is_claude_cli_profile_true_via_env_allowlist(self, monkeypatch):
        monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
        assert kb._is_claude_cli_profile("coder-claude", None) is True

    def test_is_claude_cli_profile_true_via_config_flag(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERMES_CLAUDE_CLI_PROFILES", raising=False)
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.yaml").write_text(
            "worker_runtime: claude-cli\n", encoding="utf-8"
        )
        assert kb._is_claude_cli_profile("coder", str(home)) is True

    def test_is_claude_cli_profile_false_for_normal_profile(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERMES_CLAUDE_CLI_PROFILES", raising=False)
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.yaml").write_text("worker_runtime: hermes\n", encoding="utf-8")
        assert kb._is_claude_cli_profile("coder", str(home)) is False

    def test_is_claude_cli_profile_false_no_flag_no_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERMES_CLAUDE_CLI_PROFILES", raising=False)
        home = tmp_path / "home"
        home.mkdir()
        # No config.yaml at all.
        assert kb._is_claude_cli_profile("coder", str(home)) is False

    def test_is_claude_cli_profile_false_missing_home(self, monkeypatch):
        monkeypatch.delenv("HERMES_CLAUDE_CLI_PROFILES", raising=False)
        assert kb._is_claude_cli_profile("coder", None) is False

    def test_is_claude_cli_profile_false_on_malformed_yaml(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERMES_CLAUDE_CLI_PROFILES", raising=False)
        home = tmp_path / "home"
        home.mkdir()
        # Unparseable YAML — fail-soft to False, never raise.
        (home / "config.yaml").write_text("worker_runtime: [unclosed\n", encoding="utf-8")
        assert kb._is_claude_cli_profile("coder", str(home)) is False

    # --- claude branch of _default_spawn ----------------------------------

    def test_default_spawn_routes_to_claude_cli(self, tmp_path, monkeypatch):
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        self._set_home(monkeypatch, tmp_path, default_home)

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                captured["env"] = kwargs.get("env", {})
                captured["cwd"] = kwargs.get("cwd")
                self.pid = 7777

        monkeypatch.setattr("subprocess.Popen", _FakePopen)
        # Make claude-bin resolution deterministic regardless of host.
        monkeypatch.setenv("HERMES_CLAUDE_BIN", "/usr/local/bin/claude-test")

        task = self._make_task(tmp_path, assignee="coder")
        # Flag the task's assignee profile as a claude-CLI worker.
        monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder")

        pid = kb._default_spawn(task, str(tmp_path / "ws"))
        assert pid == 7777

        cmd = captured["cmd"]
        assert cmd[0] == "/usr/local/bin/claude-test"
        assert "-p" in cmd
        assert "--dangerously-skip-permissions" in cmd
        # The output-format pair is present and adjacent.
        of_idx = cmd.index("--output-format")
        assert cmd[of_idx + 1] == "json"
        # Prompt arg carries the task id contract.
        prompt = cmd[cmd.index("-p") + 1]
        assert task.id in prompt or "$HERMES_KANBAN_TASK" in prompt
        # This is NOT the hermes path.
        assert "chat" not in cmd
        # Env carries the kanban contract.
        assert captured["env"]["HERMES_KANBAN_TASK"] == task.id

    def test_default_spawn_claude_excludes_memsearch(self, tmp_path, monkeypatch):
        """Headless workers must not load the memsearch memory plugin:
        the --settings disable AND the MEMSEARCH_NO_WATCH belt are both on
        the spawn (Planspec 2026-06-12 memsearch-voll-rollout, T3)."""
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        self._set_home(monkeypatch, tmp_path, default_home)

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                captured["env"] = kwargs.get("env", {})
                self.pid = 7779

        monkeypatch.setattr("subprocess.Popen", _FakePopen)
        monkeypatch.setenv("HERMES_CLAUDE_BIN", "/usr/local/bin/claude-test")
        monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder")

        task = self._make_task(tmp_path, assignee="coder")
        kb._default_spawn(task, str(tmp_path / "ws"))

        cmd = captured["cmd"]
        s_idx = cmd.index("--settings")
        settings = json.loads(cmd[s_idx + 1])
        assert settings["enabledPlugins"]["memsearch@memsearch-plugins"] is False
        # --bare would also drop the guard-dangerous-ops PreToolUse hook (S2);
        # the exclusion must stay a targeted plugin disable.
        assert "--bare" not in cmd
        assert captured["env"]["MEMSEARCH_NO_WATCH"] == "1"

    def test_default_spawn_claude_appends_model_override(self, tmp_path, monkeypatch):
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        self._set_home(monkeypatch, tmp_path, default_home)

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                self.pid = 8888

        monkeypatch.setattr("subprocess.Popen", _FakePopen)
        monkeypatch.setenv("HERMES_CLAUDE_BIN", "/usr/local/bin/claude-test")
        monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder")

        task = self._make_task(tmp_path, assignee="coder", model_override="claude-opus-4-8")
        kb._default_spawn(task, str(tmp_path / "ws"))

        cmd = captured["cmd"]
        m_idx = cmd.index("--model")
        assert cmd[m_idx + 1] == "claude-opus-4-8"

    # --- model routing: per-profile default (claude_model) ----------------

    def _spawn_capture_model(self, tmp_path, monkeypatch, *, config_text, model_override=None):
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        (default_home / "config.yaml").write_text(config_text, encoding="utf-8")
        self._set_home(monkeypatch, tmp_path, default_home)
        monkeypatch.delenv("HERMES_CLAUDE_CLI_PROFILES", raising=False)
        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                self.pid = 9999

        monkeypatch.setattr("subprocess.Popen", _FakePopen)
        monkeypatch.setenv("HERMES_CLAUDE_BIN", "/usr/local/bin/claude-test")
        task = self._make_task(tmp_path, assignee="coder", model_override=model_override)
        kb._default_spawn(task, str(tmp_path / "ws"))
        return captured["cmd"]

    def test_claude_worker_uses_profile_default_model(self, tmp_path, monkeypatch):
        # worker_runtime flag + claude_model default, no per-task override →
        # the profile's claude_model is the --model (routing tier 2).
        cmd = self._spawn_capture_model(
            tmp_path, monkeypatch,
            config_text="worker_runtime: claude-cli\nclaude_model: claude-fable-5\n",
        )
        assert cmd[cmd.index("--model") + 1] == "claude-fable-5"

    def test_claude_worker_override_beats_profile_default(self, tmp_path, monkeypatch):
        # Per-task override (tier 1) wins over the profile default (tier 2).
        cmd = self._spawn_capture_model(
            tmp_path, monkeypatch,
            config_text="worker_runtime: claude-cli\nclaude_model: claude-fable-5\n",
            model_override="claude-opus-4-8",
        )
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"

    def test_claude_worker_no_model_flag_when_unset(self, tmp_path, monkeypatch):
        # Flagged worker, no override, no claude_model → omit --model so claude
        # falls back to the subscription default (routing tier 3).
        cmd = self._spawn_capture_model(
            tmp_path, monkeypatch,
            config_text="worker_runtime: claude-cli\n",
        )
        assert "--model" not in cmd

    # --- comment thread baked into the -p prompt (AC-A) -------------------

    def _capture_claude_prompt(self, monkeypatch, task):
        """Route ``task`` through the claude-CLI branch and return its -p prompt.

        Mirrors the existing claude-spawn tests' Popen capture, but returns the
        prompt string so the comment-thread assertions read cleanly.
        """
        monkeypatch.setenv("HERMES_CLAUDE_BIN", "/usr/local/bin/claude-test")
        monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", task.assignee)

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                self.pid = 4242

        monkeypatch.setattr("subprocess.Popen", _FakePopen)
        kb._default_spawn(task, str(Path(task.workspace_path or ".")))
        cmd = captured["cmd"]
        return cmd[cmd.index("-p") + 1]

    def test_claude_worker_appends_comment_thread(self, kanban_home, monkeypatch):
        """A claude-CLI worker has no kanban tools and never sees comments, so
        the most-recent _CTX_MAX_COMMENTS must be baked into the -p prompt with
        the SAME framing as build_worker_context — AC-A."""
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="ship the widget",
                body="implement the widget", assignee="coder",
                workspace_path=str(kanban_home / "ws"),
            )
            kb.add_comment(conn, tid, "operator", "please update the changelog")
            kb.add_comment(conn, tid, "coder", "first attempt failed on lint")
            task = kb.get_task(conn, tid)

        prompt = self._capture_claude_prompt(monkeypatch, task)

        # Same section header + per-comment framing as build_worker_context.
        assert "## Comment thread" in prompt
        assert "comment from worker `operator` at" in prompt
        assert "please update the changelog" in prompt
        assert "comment from worker `coder` at" in prompt
        assert "first attempt failed on lint" in prompt
        # The block sits AFTER the body and BEFORE the work instruction.
        assert prompt.index("implement the widget") < prompt.index("## Comment thread")
        assert prompt.index("## Comment thread") < prompt.index(
            "Work in the current directory."
        )
        # Preamble + report-back + PROVIDER RULE stay verbatim.
        assert prompt.startswith(
            "You are an autonomous Hermes kanban worker running headless."
        )
        assert "PROVIDER RULE: Never call anthropic/*" in prompt

    def test_claude_worker_no_comment_block_when_no_comments(self, kanban_home, monkeypatch):
        """Zero comments → no comment block at all; the prompt still flows from
        body through knowledge pointers into the work instruction."""
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="no comments",
                body="do the thing", assignee="coder",
                workspace_path=str(kanban_home / "ws"),
            )
            task = kb.get_task(conn, tid)

        prompt = self._capture_claude_prompt(monkeypatch, task)

        assert "## Comment thread" not in prompt
        assert "comment from worker" not in prompt
        assert prompt.index("do the thing") < prompt.index("## Knowledge pointers")
        assert prompt.index("## Knowledge pointers") < prompt.index(
            "Work in the current directory."
        )

    def test_claude_worker_comment_thread_uses_worker_slim_cap(self, kanban_home, monkeypatch):
        """Claude and Hermes launch briefs use the same worker_slim comment cap."""
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        total = kb._CTX_MAX_COMMENTS + 3
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="comment storm",
                body="x", assignee="coder",
                workspace_path=str(kanban_home / "ws"),
            )
            for i in range(total):
                kb.add_comment(conn, tid, "operator", f"comment-number-{i}")
            task = kb.get_task(conn, tid)

        prompt = self._capture_claude_prompt(monkeypatch, task)

        assert "## Relevant comments" in prompt
        assert "earlier comments omitted" in prompt
        assert "comment-number-0" not in prompt
        worker_slim_comments = context.context_caps("worker_slim")["comments"]
        assert f"comment-number-{total - worker_slim_comments}" in prompt
        assert f"comment-number-{total - 1}" in prompt

    def test_claude_worker_renders_operator_directive(self, kanban_home, monkeypatch):
        """A claude-CLI worker inherits the same directive priority block as
        build_worker_context — both paths share _render_comment_thread — so the
        directive lands ABOVE the work instruction and is framed distinctly
        from worker comments (AC-F4-directive)."""
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="ship the widget",
                body="implement the widget", assignee="coder",
                workspace_path=str(kanban_home / "ws"),
            )
            kb.add_comment(conn, tid, "worker", "a normal note", kind="comment")
            kb.add_comment(
                conn, tid, "operator", "ACTUALLY ship the gadget", kind="directive"
            )
            task = kb.get_task(conn, tid)

        prompt = self._capture_claude_prompt(monkeypatch, task)

        assert "⚠️ OPERATOR DIRECTIVE — supersedes the task body above" in prompt
        assert "ACTUALLY ship the gadget" in prompt
        # Distinct from worker-comment framing.
        assert "comment from worker `operator`" not in prompt
        # The directive reaches the worker before the work instruction.
        assert prompt.index("OPERATOR DIRECTIVE") < prompt.index(
            "Work in the current directory."
        )

    # --- prior attempts baked into the -p prompt ---------------------------

    def test_claude_worker_appends_prior_attempts(self, kanban_home, monkeypatch):
        """A retried claude-CLI worker has NO kanban tools and never sees a
        rejected predecessor's reason via kanban_show — unlike the Hermes
        worker path, which gets 'Prior attempts on this task' via
        build_worker_context. Bake the same section into the -p prompt so a
        retried claude-CLI worker sees WHY its predecessor was rejected."""
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="ship the widget",
                body="implement the widget", assignee="coder",
                workspace_path=str(kanban_home / "ws"),
            )
            kb.claim_task(conn, tid)
            meta = {
                "verdict": "REQUEST_CHANGES",
                "blocking_findings": ["null deref in foo()", "missing test for bar"],
            }
            kb.block_task(conn, tid, reason="lint failed, see foo()", reviewer_metadata=meta)
            kb.unblock_task(conn, tid)
            task = kb.get_task(conn, tid)

        prompt = self._capture_claude_prompt(monkeypatch, task)

        assert "## Prior attempts on this task" in prompt
        assert "lint failed, see foo()" in prompt
        assert "null deref in foo()" in prompt
        assert "REQUEST_CHANGES" in prompt
        # The block sits AFTER the body and BEFORE the work instruction.
        assert prompt.index("implement the widget") < prompt.index(
            "## Prior attempts on this task"
        )
        assert prompt.index("## Prior attempts on this task") < prompt.index(
            "Work in the current directory."
        )

    def test_claude_worker_no_prior_attempts_block_on_fresh_task(
        self, kanban_home, monkeypatch
    ):
        """A fresh task (no closed runs) gets no prior-attempts section at
        all — first attempts stay unadorned."""
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="fresh task",
                body="do the thing", assignee="coder",
                workspace_path=str(kanban_home / "ws"),
            )
            task = kb.get_task(conn, tid)

        prompt = self._capture_claude_prompt(monkeypatch, task)

        assert "## Prior attempts on this task" not in prompt
        assert "Attempt 1 —" not in prompt

    def test_claude_worker_appends_knowledge_pointers(self, kanban_home, monkeypatch):
        """claude-CLI workers get the same static Knowledge pointers section as
        build_worker_context."""
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title="model routing task",
                body="pick the right model",
                assignee="coder",
                workspace_path=str(kanban_home / "ws"),
            )
            task = kb.get_task(conn, tid)

        prompt = self._capture_claude_prompt(monkeypatch, task)

        assert "## Knowledge pointers" in prompt
        assert "/home/piet/llm-wiki/wiki/models/model-landscape.md" in prompt
        assert "/home/piet/vault/00-Canon/" in prompt
        assert prompt.index("pick the right model") < prompt.index(
            "## Knowledge pointers"
        )
        assert prompt.index("## Knowledge pointers") < prompt.index(
            "Work in the current directory."
        )

    def test_claude_worker_uses_shared_knowledge_pointer_renderer(
        self, kanban_home, monkeypatch
    ):
        """A sentinel from the shared renderer must reach claude -p; otherwise
        the claude worker prompt has drifted back to duplicated strings."""
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        monkeypatch.setattr(
            kb,
            "_render_knowledge_pointers",
            lambda: ["## Knowledge pointers", "- shared-renderer-sentinel", ""],
        )
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title="shared renderer task",
                body="do the thing",
                assignee="coder",
                workspace_path=str(kanban_home / "ws"),
            )
            task = kb.get_task(conn, tid)

        prompt = self._capture_claude_prompt(monkeypatch, task)

        assert "shared-renderer-sentinel" in prompt

    # --- default (hermes) path stays byte-identical -----------------------

    def test_default_spawn_no_flag_uses_hermes_path(self, tmp_path, monkeypatch):
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        self._set_home(monkeypatch, tmp_path, default_home)
        # Explicitly NO claude-cli flag set.
        monkeypatch.delenv("HERMES_CLAUDE_CLI_PROFILES", raising=False)
        monkeypatch.setenv("HERMES_CLAUDE_BIN", "/usr/local/bin/claude-test")

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                self.pid = 9999

        monkeypatch.setattr("subprocess.Popen", _FakePopen)

        task = self._make_task(tmp_path, assignee="coder")
        kb._default_spawn(task, str(tmp_path / "ws"))

        cmd = captured["cmd"]
        # Hermes path: contains -p, the profile, and the chat subcommand.
        assert "-p" in cmd
        assert "coder" in cmd
        assert "chat" in cmd
        # And it is NOT the claude bin.
        assert cmd[0] != "/usr/local/bin/claude-test"

