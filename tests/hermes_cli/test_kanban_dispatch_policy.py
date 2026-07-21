"""Tests for the SQL-backed policy helpers in hermes_cli/kanban_dispatch_policy.

The pure helpers (positive_int/positive_number) and the inflight-count helpers
are already covered by test_kanban_extracted_helpers.py and
test_chain_worktree_serialization.py. This file targets the three functions
with ZERO prior coverage: profile_running_counts, capped_profiles_for_window,
and per_task_input_usage. They run real SQL, so we build a minimal in-memory
schema mirroring the columns they query from kanban_db.py (tasks, task_runs).
"""

from __future__ import annotations

import sqlite3

from hermes_cli.kanban_dispatch_policy import (
    capped_profiles_for_window,
    per_task_input_usage,
    profile_running_counts,
    task_is_read_only,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks ("
        "  id TEXT PRIMARY KEY,"
        "  assignee TEXT,"
        "  status TEXT NOT NULL,"
        "  workspace_kind TEXT NOT NULL DEFAULT 'scratch',"
        "  workspace_path TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE task_runs ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  task_id TEXT NOT NULL,"
        "  profile TEXT,"
        "  input_tokens INTEGER,"
        "  output_tokens INTEGER,"
        "  cost_usd REAL,"
        "  started_at INTEGER NOT NULL"
        ")"
    )
    return conn


def _add_task(conn, task_id, assignee, status):
    conn.execute(
        "INSERT INTO tasks (id, assignee, status) VALUES (?, ?, ?)",
        (task_id, assignee, status),
    )


def _add_run(conn, task_id, profile, inp, out, cost, started_at):
    conn.execute(
        "INSERT INTO task_runs "
        "(task_id, profile, input_tokens, output_tokens, cost_usd, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, profile, inp, out, cost, started_at),
    )


class TestProfileRunningCounts:
    def test_counts_only_running_tasks_per_assignee(self):
        conn = _make_conn()
        _add_task(conn, "t1", "coder", "running")
        _add_task(conn, "t2", "coder", "running")
        _add_task(conn, "t3", "reviewer", "running")
        _add_task(conn, "t4", "coder", "done")      # not running
        _add_task(conn, "t5", "coder", "review")    # not running
        _add_task(conn, "t6", None, "running")      # null assignee excluded

        assert profile_running_counts(conn) == {"coder": 2, "reviewer": 1}

    def test_empty_board_returns_empty_dict(self):
        conn = _make_conn()
        assert profile_running_counts(conn) == {}


class TestCappedProfilesForWindow:
    NOW = 1_000_000
    WINDOW_START = NOW - 86_400  # 913_600

    def test_profiles_at_or_over_token_cap_are_capped(self):
        conn = _make_conn()
        # profile "heavy": input+output = 600 + 500 = 1100 (>= 1000 cap)
        _add_run(conn, "t1", "heavy", 600, 500, 0.0, self.WINDOW_START + 10)
        # profile "light": 100 + 50 = 150 (below cap)
        _add_run(conn, "t2", "light", 100, 50, 0.0, self.WINDOW_START + 20)

        capped, cost_exceeded = capped_profiles_for_window(
            conn, token_cap=1000, cost_cap=None, now=self.NOW
        )

        assert capped == {"heavy"}
        assert cost_exceeded is False

    def test_runs_outside_window_are_excluded(self):
        conn = _make_conn()
        # Large usage but BEFORE the window start -> must not count.
        _add_run(conn, "t1", "heavy", 9999, 9999, 0.0, self.WINDOW_START - 1)

        capped, _ = capped_profiles_for_window(
            conn, token_cap=1000, cost_cap=None, now=self.NOW
        )

        assert capped == set()

    def test_global_cost_cap_exceeded(self):
        conn = _make_conn()
        _add_run(conn, "t1", "a", 0, 0, 3.0, self.WINDOW_START + 10)
        _add_run(conn, "t2", "b", 0, 0, 2.5, self.WINDOW_START + 20)  # total 5.5

        _, cost_exceeded = capped_profiles_for_window(
            conn, token_cap=None, cost_cap=5.0, now=self.NOW
        )

        assert cost_exceeded is True

    def test_global_cost_cap_not_exceeded(self):
        conn = _make_conn()
        _add_run(conn, "t1", "a", 0, 0, 1.0, self.WINDOW_START + 10)

        _, cost_exceeded = capped_profiles_for_window(
            conn, token_cap=None, cost_cap=5.0, now=self.NOW
        )

        assert cost_exceeded is False

    def test_none_caps_disable_both_checks(self):
        conn = _make_conn()
        _add_run(conn, "t1", "heavy", 10_000, 10_000, 999.0, self.WINDOW_START + 10)

        capped, cost_exceeded = capped_profiles_for_window(
            conn, token_cap=None, cost_cap=None, now=self.NOW
        )

        assert capped == set()
        assert cost_exceeded is False

    def test_null_tokens_coalesce_to_zero(self):
        conn = _make_conn()
        _add_run(conn, "t1", "heavy", None, None, 0.0, self.WINDOW_START + 10)

        capped, _ = capped_profiles_for_window(
            conn, token_cap=1, cost_cap=None, now=self.NOW
        )

        # 0 tokens is below a cap of 1 -> not capped.
        assert capped == set()


class TestPerTaskInputUsage:
    def test_sums_tokens_and_counts_runs_per_task(self):
        conn = _make_conn()
        _add_run(conn, "t1", "p", 100, 0, 0.0, 1)
        _add_run(conn, "t1", "p", 250, 0, 0.0, 2)   # t1: 350 tokens, 2 runs
        _add_run(conn, "t2", "p", 42, 0, 0.0, 3)    # t2: 42 tokens, 1 run

        usage = per_task_input_usage(conn, ["t1", "t2"])

        assert usage == {"t1": (350, 2), "t2": (42, 1)}

    def test_task_with_no_runs_is_absent(self):
        conn = _make_conn()
        _add_run(conn, "t1", "p", 10, 0, 0.0, 1)

        usage = per_task_input_usage(conn, ["t1", "never-ran"])

        assert usage == {"t1": (10, 1)}
        assert "never-ran" not in usage

    def test_empty_ready_ids_returns_empty_dict(self):
        conn = _make_conn()
        assert per_task_input_usage(conn, []) == {}

    def test_chunking_handles_more_than_500_ids(self):
        # per_task_input_usage chunks queries at 500 ids; 501 ids exercises
        # the second chunk boundary without dropping any task.
        conn = _make_conn()
        ids = [f"t{i}" for i in range(501)]
        for tid in ids:
            _add_run(conn, tid, "p", 5, 0, 0.0, 1)

        usage = per_task_input_usage(conn, ids)

        assert len(usage) == 501
        assert usage["t0"] == (5, 1)
        assert usage["t500"] == (5, 1)


def test_task_is_read_only_contract():
    assert task_is_read_only("review", "coder")
    assert task_is_read_only("research", "researcher-a")
    assert task_is_read_only("code", "critic")
    assert not task_is_read_only("code", "coder")
