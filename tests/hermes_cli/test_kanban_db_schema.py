"""Kanban DB tests: schema.

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
from hermes_cli import kanban_db as kb

from tests.hermes_cli._kanban_test_helpers import (
    _write_state_session,
)

_PLANSPEC_COLS = [
    "planspec_subtask_id",
    "planspec_source",
    "freigabe",
    "live_test_depth",
]


def _insert_profile_outcome_run(
    conn,
    *,
    profile: str,
    outcome: str,
    started_at: int,
    runtime_s: int = 10,
    input_tokens: int | None = 100,
    output_tokens: int | None = 0,
    verdict: str | None = None,
):
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, "
        "outcome, input_tokens, output_tokens, verdict) "
        "VALUES (?, ?, 'done', ?, ?, ?, ?, ?, ?)",
        (
            f"t_{profile}_{started_at}_{outcome}",
            profile,
            started_at,
            started_at + runtime_s,
            outcome,
            input_tokens,
            output_tokens,
            verdict,
        ),
    )


def _insert_token_run(
    conn,
    *,
    task_id: str,
    profile: str,
    started_at: int,
    ended_at: int,
    input_tokens: int | None,
    output_tokens: int | None,
    metadata: dict | None = None,
    outcome: str = "completed",
):
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, "
        "outcome, input_tokens, output_tokens, metadata) "
        "VALUES (?, ?, 'done', ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            profile,
            started_at,
            ended_at,
            outcome,
            input_tokens,
            output_tokens,
            json.dumps(metadata) if metadata is not None else None,
        ),
    )


# ---------------------------------------------------------------------------
# Schema / init
# ---------------------------------------------------------------------------

def test_init_db_is_idempotent(kanban_home):
    # Second call should not error or drop data.
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="persisted")
    kb.init_db()
    with kb.connect_closing() as conn:
        tasks = kb.list_tasks(conn)
    assert len(tasks) == 1
    assert tasks[0].title == "persisted"


def test_task_kind_column_migration_is_idempotent(kanban_home):
    with kb.connect_closing() as conn:
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)")]
    assert cols.count("kind") == 1


def test_review_tier_column_exists_and_defaults_null(kanban_home):
    """B-T1: additive review_tier column present on every board, default NULL."""
    with kb.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "review_tier" in cols
        tid = kb.create_task(conn, title="t", assignee="coder")
        row = conn.execute(
            "SELECT review_tier FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["review_tier"] is None


def test_review_tier_column_migration_is_idempotent(kanban_home):
    with kb.connect_closing() as conn:
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)")]
    assert cols.count("review_tier") == 1


def test_create_task_persists_and_reads_review_tier(kanban_home):
    """B-T2: create_task accepts review_tier and get_task reads it back; default NULL."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder", review_tier="critical")
        assert kb.get_task(conn, tid).review_tier == "critical"
        tid2 = kb.create_task(conn, title="t2", assignee="coder")
        assert kb.get_task(conn, tid2).review_tier is None


def test_create_task_rejects_unknown_review_tier(kanban_home):
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="unknown review_tier"):
            kb.create_task(conn, title="bad tier", assignee="coder", review_tier="bogus")


def test_create_task_validates_and_persists_acceptance_criteria(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="task with AC",
            acceptance_criteria="- AC-DIRECT: focused behavior is proven",
        )
        row = conn.execute(
            "SELECT acceptance_criteria FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    assert json.loads(row["acceptance_criteria"]) == [
        "AC-DIRECT: focused behavior is proven"
    ]


@pytest.mark.parametrize("criteria", ["", "- behavior without stable id"])
def test_create_task_rejects_invalid_acceptance_criteria(kanban_home, criteria):
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="acceptance_criteria"):
            kb.create_task(conn, title="bad AC", acceptance_criteria=criteria)


# --- ui_impact: additive column + accessor + setter (PlanSpec AD-S1) ---

def test_ui_impact_column_exists_and_defaults_null(kanban_home):
    """AC-1: additive ui_impact column present on every board, default NULL."""
    with kb.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "ui_impact" in cols
        tid = kb.create_task(conn, title="t", assignee="coder")
        row = conn.execute(
            "SELECT ui_impact FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["ui_impact"] is None


def test_ui_impact_column_migration_is_idempotent(kanban_home):
    with kb.connect_closing() as conn:
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)")]
    assert cols.count("ui_impact") == 1


def test_create_task_persists_and_reads_ui_impact(kanban_home):
    """AC-2: create_task accepts ui_impact and get_task reads it back; default NULL."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder", ui_impact="redesign")
        assert kb.get_task(conn, tid).ui_impact == "redesign"
        tid2 = kb.create_task(conn, title="t2", assignee="coder")
        assert kb.get_task(conn, tid2).ui_impact is None


def test_create_task_rejects_unknown_ui_impact(kanban_home):
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="unknown ui_impact"):
            kb.create_task(conn, title="bad impact", assignee="coder", ui_impact="bogus")


def test_create_task_normalises_ui_impact_case(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder", ui_impact="  Redesign  ")
        assert kb.get_task(conn, tid).ui_impact == "redesign"


def test_effective_ui_impact_mapping(kanban_home):
    """AC-2: NULL/none/minor → autonomous; redesign → operator-gated."""
    with kb.connect_closing() as conn:
        tid_none = kb.create_task(conn, title="n", assignee="coder", ui_impact=None)
        tid_minor = kb.create_task(conn, title="m", assignee="coder", ui_impact="minor")
        tid_redesign = kb.create_task(conn, title="r", assignee="coder", ui_impact="redesign")
        tid_explicit_none = kb.create_task(conn, title="en", assignee="coder", ui_impact="none")
    with kb.connect_closing() as conn:
        assert kb.effective_ui_impact(kb.get_task(conn, tid_none)) == "autonomous"
        assert kb.effective_ui_impact(kb.get_task(conn, tid_minor)) == "autonomous"
        assert kb.effective_ui_impact(kb.get_task(conn, tid_redesign)) == "operator-gated"
        assert kb.effective_ui_impact(kb.get_task(conn, tid_explicit_none)) == "autonomous"


def test_effective_ui_impact_none_task_is_autonomous():
    """A missing task must not block callers — treated as autonom-capable."""
    assert kb.effective_ui_impact(None) == "autonomous"


def test_set_task_ui_impact_updates_and_clears(kanban_home):
    """AC-2: set_task_ui_impact sets, then clears (NULL → treated as none)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder")
        assert kb.effective_ui_impact(kb.get_task(conn, tid)) == "autonomous"
        assert kb.set_task_ui_impact(conn, tid, "redesign") is True
        assert kb.get_task(conn, tid).ui_impact == "redesign"
        assert kb.effective_ui_impact(kb.get_task(conn, tid)) == "operator-gated"
        # clear back to NULL
        assert kb.set_task_ui_impact(conn, tid, None) is True
        assert kb.get_task(conn, tid).ui_impact is None
        assert kb.effective_ui_impact(kb.get_task(conn, tid)) == "autonomous"


def test_set_task_ui_impact_rejects_unknown(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder")
        with pytest.raises(ValueError, match="unknown ui_impact"):
            kb.set_task_ui_impact(conn, tid, "bogus")


def test_set_task_ui_impact_missing_task_returns_false(kanban_home):
    with kb.connect_closing() as conn:
        assert kb.set_task_ui_impact(conn, "does-not-exist", "redesign") is False


def test_respec_preserves_ui_impact(kanban_home):
    """AC-3 sanity: respec copies ui_impact from the source task."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="orig", assignee="coder", ui_impact="redesign",
            initial_status="blocked",
        )
        new_id = kb.respec_task(conn, tid, body="respunned", author="operator")
        assert new_id is not None
        assert kb.get_task(conn, new_id).ui_impact == "redesign"


def test_parse_vault_memory_links_recognizes_obsidian_and_memory_targets(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    canon = vault / "00-Canon"
    canon.mkdir(parents=True)
    note = canon / "vision.md"
    note.write_text("# Vision\n", encoding="utf-8")
    spaced_note = canon / "my note.md"
    spaced_note.write_text("# Note with spaces\n", encoding="utf-8")
    pdf_note = canon / "brief.pdf"
    pdf_note.write_bytes(b"%PDF-1.4\n")

    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    memory_root = hermes_home / "memories"
    memory_root.mkdir(parents=True)
    memory_note = memory_root / "MEMORY.md"
    memory_note.write_text("# Memory\n", encoding="utf-8")
    other_memory_note = memory_root / "OTHER.md"
    other_memory_note.write_text("# Other Memory\n", encoding="utf-8")
    plain_memory_note = memory_root / "PLAIN.txt"
    plain_memory_note.write_text("Plain memory\n", encoding="utf-8")

    links = kb.parse_vault_memory_links(
        (
            "See [[00-Canon/vision|Vision]], [[#Local Heading]], [[00-Canon/brief.pdf|Brief]], "
            "[space note](00-Canon/my note.md \"title\"), "
            "[manual memory](${HERMES_HOME}/memories/MEMORY.md), "
            "${HERMES_HOME}/memories/OTHER.md, $HERMES_HOME/memories/PLAIN.txt, vault/00-Canon, "
            "and memsearch:abcdef1234567890."
        ),
        source="body",
        vault_root=vault,
        memory_roots=[memory_root],
    )

    assert [link["kind"] for link in links] == ["vault", "vault", "vault", "memory", "memory", "memory", "memory"]
    assert links[0]["path"] == str(note)
    assert links[0]["display_path"] == "00-Canon/vision.md"
    assert links[0]["obsidian_url"].startswith("obsidian://open?")
    assert links[0]["exists"] is True
    assert links[1]["path"] == str(pdf_note)
    assert links[1]["display_path"] == "00-Canon/brief.pdf"
    assert links[1]["obsidian_url"].startswith("obsidian://open?")
    assert links[1]["url"] is None
    assert links[2]["path"] == str(spaced_note)
    assert links[2]["display_path"] == "00-Canon/my note.md"
    assert links[3]["path"] == str(memory_note)
    assert links[3]["display_path"] == "MEMORY.md"
    assert links[3]["url"] is None
    assert links[4]["path"] == str(other_memory_note)
    assert links[4]["display_path"] == "OTHER.md"
    assert links[5]["path"] == str(plain_memory_note)
    assert links[5]["display_path"] == "PLAIN.txt"
    assert links[6]["path"] is None
    assert links[6]["target"] == "memsearch:abcdef1234567890"
    assert all(link["path"] != str(vault) for link in links)


def test_planspec_columns_exist_after_migration(kanban_home):
    """A1: all four planspec columns must be present after init/migration."""
    with kb.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    for col in _PLANSPEC_COLS:
        assert col in cols, f"column '{col}' missing from tasks after migration"


def test_planspec_columns_migration_is_idempotent(kanban_home):
    """A1: running the migration twice must be a no-op (no exception, no duplicate)."""
    with kb.connect_closing() as conn:
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)")]
    for col in _PLANSPEC_COLS:
        assert cols.count(col) == 1, (
            f"column '{col}' appears {cols.count(col)} times after double migration"
        )


def test_create_task_persists_optional_kind(kanban_home):
    with kb.connect_closing() as conn:
        code_id = kb.create_task(conn, title="code task", kind="code")
        plain_id = kb.create_task(conn, title="plain task")
        rows = conn.execute(
            "SELECT id, kind FROM tasks WHERE id IN (?, ?)",
            (code_id, plain_id),
        ).fetchall()
    by_id = {row["id"]: row["kind"] for row in rows}
    assert by_id[code_id] == "code"
    assert by_id[plain_id] is None


def test_init_creates_expected_tables(kanban_home):
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert {"tasks", "task_links", "task_comments", "task_events"} <= names


def test_k5a_task_runs_cost_columns_present_and_migrate_idempotently(kanban_home):
    """K5a: task_runs gains input_tokens/output_tokens/cost_usd, and re-running
    the additive migration is a no-op (idempotent, no duplicate-column crash)."""
    with kb.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")}
        assert {"input_tokens", "output_tokens", "cost_usd"} <= cols
        # Re-running the migration twice on the live connection must not raise.
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        cols2 = [
            r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")
        ]
        # Still exactly one of each column — no duplicates introduced.
        for name in ("input_tokens", "output_tokens", "cost_usd"):
            assert cols2.count(name) == 1


def test_execution_capsule_column_present_and_migrates_idempotently(kanban_home):
    with kb.connect_closing() as conn:
        cols = [row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")]
        assert cols.count("execution_capsule") == 1
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        migrated = [
            row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")
        ]
        assert migrated.count("execution_capsule") == 1


def test_k11_decompose_failed_column_present_and_defaults_zero(kanban_home):
    """K11: tasks gains ``decompose_failed`` (additive), defaulting to 0 on a
    fresh task."""
    with kb.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "decompose_failed" in cols
        tid = kb.create_task(conn, title="fresh")
        row = conn.execute(
            "SELECT decompose_failed FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["decompose_failed"] == 0


def test_k11_record_and_reset_decompose_failure(kanban_home):
    """K11: ``record_decompose_failure`` increments and returns the new value;
    ``reset_decompose_failed`` clears it back to 0."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="flaky")
        assert kb.record_decompose_failure(conn, tid) == 1
        assert kb.record_decompose_failure(conn, tid) == 2
        row = conn.execute(
            "SELECT decompose_failed FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["decompose_failed"] == 2
        kb.reset_decompose_failed(conn, tid)
        row = conn.execute(
            "SELECT decompose_failed FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["decompose_failed"] == 0


def test_k11_record_decompose_failure_missing_row_returns_zero(kanban_home):
    """K11: a counter bump on a non-existent task is a no-op returning 0."""
    with kb.connect_closing() as conn:
        assert kb.record_decompose_failure(conn, "t_ghost") == 0


def test_k11_decompose_failed_migration_idempotent(kanban_home):
    """K11: re-running the additive migration is a no-op — no duplicate-column
    crash and the column survives + preserves its value."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="survivor")
        assert kb.record_decompose_failure(conn, tid) == 1
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)")]
        assert cols.count("decompose_failed") == 1
        row = conn.execute(
            "SELECT decompose_failed FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["decompose_failed"] == 1


def test_k5a_end_run_writes_back_tokens_cost_from_metadata(kanban_home):
    """K5a: _end_run persists usage from the in-process metadata dict."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="costed", assignee="worker")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(
            conn, tid,
            result="done",
            metadata={
                "usage": {"input_tokens": 1200, "output_tokens": 340},
                "cost_usd": 0.0153,
            },
        )
        assert ok
        runs = kb.list_runs(conn, tid)
        assert len(runs) == 1
        assert runs[0].input_tokens == 1200
        assert runs[0].output_tokens == 340
        assert runs[0].cost_usd == pytest.approx(0.0153)
        # K6 per-task cost sum now reflects the written-back value.
        assert kb.task_runs_cost_usd_sum(conn, task_id=tid) == pytest.approx(0.0153)


def test_k5a_end_run_leaves_cost_null_without_usage(kanban_home):
    """K5a: a run with no usage metadata writes NULLs, never crashes."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="no-usage", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, result="done")
        runs = kb.list_runs(conn, tid)
        assert len(runs) == 1
        assert runs[0].input_tokens is None
        assert runs[0].output_tokens is None
        assert runs[0].cost_usd is None
        assert kb.task_runs_cost_usd_sum(conn, task_id=tid) is None


def test_profile_outcome_stats_fails_soft_when_task_runs_absent():
    conn = sqlite3.connect(":memory:")
    assert kb.profile_outcome_stats(conn) == {}


def test_subscription_token_totals_since_epoch_includes_lower_boundary(
    kanban_home, monkeypatch,
):
    """Kimi Abo-Limits: since_epoch is an inclusive rolling-window lower bound."""
    monkeypatch.setattr(
        kb,
        "_profile_subscription",
        lambda profile: "kimi" if profile == "reviewer" else None,
    )
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="token window")
        with kb.write_txn(conn):
            _insert_token_run(
                conn, task_id=task_id, profile="reviewer",
                started_at=999, ended_at=1009, input_tokens=100, output_tokens=10,
            )
            _insert_token_run(
                conn, task_id=task_id, profile="reviewer",
                started_at=1000, ended_at=1010, input_tokens=200, output_tokens=20,
            )
            _insert_token_run(
                conn, task_id=task_id, profile="reviewer",
                started_at=1001, ended_at=1011, input_tokens=300, output_tokens=30,
            )

        totals = kb.subscription_token_totals(
            conn, subscription="kimi", since_epoch=1000,
        )

    assert totals == {
        "subscription": "kimi",
        "since_epoch": 1000,
        "runs": 2,
        "input_tokens": 500,
        "output_tokens": 50,
        "total_tokens": 550,
    }


def test_subscription_token_totals_excludes_non_kimi_profiles(
    kanban_home, monkeypatch,
):
    """Kimi Abo-Limits: only profiles resolved to subscription='kimi' count."""
    subscriptions = {
        "reviewer": "kimi",
        "coder": "chatgpt",
        "premium": "claude",
        "critic": None,
    }
    monkeypatch.setattr(kb, "_profile_subscription", subscriptions.get)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="token subscriptions")
        with kb.write_txn(conn):
            for profile, tokens_in, tokens_out in [
                ("reviewer", 100, 10),
                ("coder", 200, 20),
                ("premium", 300, 30),
                ("critic", 400, 40),
            ]:
                _insert_token_run(
                    conn, task_id=task_id, profile=profile,
                    started_at=2000, ended_at=2010,
                    input_tokens=tokens_in, output_tokens=tokens_out,
                )

        totals = kb.subscription_token_totals(
            conn, subscription="kimi", since_epoch=2000,
        )

    assert totals["runs"] == 1
    assert totals["input_tokens"] == 100
    assert totals["output_tokens"] == 10
    assert totals["total_tokens"] == 110


def test_subscription_token_totals_prefers_run_metadata_over_live_profile(
    kanban_home, monkeypatch,
):
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: "kimi")
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="token metadata subscription")
        with kb.write_txn(conn):
            _insert_token_run(
                conn, task_id=task_id, profile="verifier",
                started_at=3000, ended_at=3010,
                input_tokens=100, output_tokens=10,
                metadata={"subscription": "chatgpt"},
            )
            _insert_token_run(
                conn, task_id=task_id, profile="verifier",
                started_at=3000, ended_at=3010,
                input_tokens=900, output_tokens=90,
                metadata={"subscription": "kimi"},
            )

        totals = kb.subscription_token_totals(
            conn, subscription="chatgpt", since_epoch=3000,
        )

    assert totals["runs"] == 1
    assert totals["input_tokens"] == 100
    assert totals["output_tokens"] == 10
    assert totals["total_tokens"] == 110


def test_subscription_token_burn_batches_by_lane_class_and_day(
    kanban_home, monkeypatch,
):
    """Abo-Burn aggregation uses one read-only batch SELECT, then folds by axes."""
    now = 1_700_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setattr(
        kb,
        "_profile_subscription",
        {"coder-claude": "claude", "premium": "chatgpt", "critic": None}.get,
    )
    with kb.connect_closing() as conn:
        user_task = kb.create_task(conn, title="[FO] Family Organizer export", assignee="coder")
        hardening_task = kb.create_task(conn, title="review gate", assignee="reviewer")
        meta_task = kb.create_task(conn, title="platform cleanup", assignee="coder")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET created_by='kanban-review-chain' WHERE id=?",
                (hardening_task,),
            )
            for task_id, profile, tin, tout in [
                (user_task, "coder-claude", 100, 10),
                (user_task, "coder-claude", 200, 20),
                (hardening_task, "premium", 300, 30),
                (meta_task, "critic", 999, 999),
            ]:
                _insert_token_run(
                    conn,
                    task_id=task_id,
                    profile=profile,
                    started_at=now - 60,
                    ended_at=now - 30,
                    input_tokens=tin,
                    output_tokens=tout,
                )

        statements: list[str] = []
        conn.set_trace_callback(lambda stmt: statements.append(stmt.strip().upper()))
        burn = kb.subscription_token_burn(conn, days=7)
        conn.set_trace_callback(None)

    selects = [stmt for stmt in statements if stmt.startswith("SELECT")]
    writes = [stmt for stmt in statements if stmt.startswith(("INSERT", "UPDATE", "DELETE"))]
    assert len(selects) == 1
    assert writes == []
    assert burn["totals"] == {
        "runs": 3,
        "completed_runs": 3,
        "failed_runs": 0,
        "blocked_runs": 0,
        "input_tokens": 600,
        "output_tokens": 60,
        "total_tokens": 660,
    }
    assert burn["by_lane"] == [
        {
            "subscription": "chatgpt",
            "profile": "premium",
            "runs": 1,
            "completed_runs": 1,
            "failed_runs": 0,
            "blocked_runs": 0,
            "input_tokens": 300,
            "output_tokens": 30,
            "total_tokens": 330,
        },
        {
            "subscription": "claude",
            "profile": "coder-claude",
            "runs": 2,
            "completed_runs": 2,
            "failed_runs": 0,
            "blocked_runs": 0,
            "input_tokens": 300,
            "output_tokens": 30,
            "total_tokens": 330,
        },
    ]
    assert {row["value_class"]: row["total_tokens"] for row in burn["by_class"]} == {
        "haertung": 330,
        "nutzer": 330,
    }
    assert burn["buckets"] == [
        {
            "subscription": "chatgpt",
            "profile": "premium",
            "value_class": "haertung",
            "date": "2023-11-14",
            "runs": 1,
            "completed_runs": 1,
            "failed_runs": 0,
            "blocked_runs": 0,
            "input_tokens": 300,
            "output_tokens": 30,
            "total_tokens": 330,
        },
        {
            "subscription": "claude",
            "profile": "coder-claude",
            "value_class": "nutzer",
            "date": "2023-11-14",
            "runs": 2,
            "completed_runs": 2,
            "failed_runs": 0,
            "blocked_runs": 0,
            "input_tokens": 300,
            "output_tokens": 30,
            "total_tokens": 330,
        },
    ]


def test_subscription_token_burn_prefers_run_metadata_after_lane_flip(
    kanban_home, monkeypatch,
):
    now = 1_700_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: "kimi")
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="metadata burn", assignee="verifier")
        with kb.write_txn(conn):
            _insert_token_run(
                conn,
                task_id=task_id,
                profile="verifier",
                started_at=now - 60,
                ended_at=now - 30,
                input_tokens=123,
                output_tokens=45,
                metadata={"subscription": "chatgpt"},
            )

        burn = kb.subscription_token_burn(conn, days=7)

    assert burn["totals"] == {
        "runs": 1,
        "completed_runs": 1,
        "failed_runs": 0,
        "blocked_runs": 0,
        "input_tokens": 123,
        "output_tokens": 45,
        "total_tokens": 168,
    }
    assert burn["by_class"][0]["subscription"] == "chatgpt"
    assert burn["by_lane"][0]["subscription"] == "chatgpt"


def test_subscription_token_burn_exposes_outcomes_per_provider(
    kanban_home, monkeypatch,
):
    now = 1_700_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: "claude")
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="provider outcomes", assignee="coder")
        with kb.write_txn(conn):
            for offset, outcome in enumerate(("completed", "blocked", "timed_out")):
                _insert_token_run(
                    conn,
                    task_id=task_id,
                    profile="premium",
                    started_at=now - 90 + offset,
                    ended_at=now - 60 + offset,
                    input_tokens=100,
                    output_tokens=10,
                    outcome=outcome,
                )

        burn = kb.subscription_token_burn(conn, days=7)

    assert burn["totals"] == {
        "runs": 3,
        "completed_runs": 1,
        "failed_runs": 1,
        "blocked_runs": 1,
        "input_tokens": 300,
        "output_tokens": 30,
        "total_tokens": 330,
    }
    assert burn["by_lane"][0]["completed_runs"] == 1
    assert burn["by_lane"][0]["failed_runs"] == 1
    assert burn["by_lane"][0]["blocked_runs"] == 1


def test_profile_outcome_stats_aggregates_recent_profile_runs(kanban_home):
    with kb.connect_closing() as conn:
        base = 1_700_000_000
        for i in range(8):
            _insert_profile_outcome_run(
                conn,
                profile="coder",
                outcome="completed",
                started_at=base + i,
                runtime_s=20,
                input_tokens=70,
                output_tokens=30,
                verdict="APPROVED" if i < 3 else None,
            )
        _insert_profile_outcome_run(
            conn,
            profile="coder",
            outcome="blocked",
            started_at=base + 8,
            runtime_s=20,
            input_tokens=70,
            output_tokens=30,
            verdict="REQUEST_CHANGES",
        )
        _insert_profile_outcome_run(
            conn,
            profile="coder",
            outcome="timed_out",
            started_at=base + 9,
            runtime_s=20,
            input_tokens=70,
            output_tokens=30,
        )
        conn.commit()

        stats = kb.profile_outcome_stats(conn)["coder"]

    assert stats["runs"] == 10
    assert stats["done_pct"] == pytest.approx(80.0)
    assert stats["blocked_pct"] == pytest.approx(10.0)
    assert stats["timeout_pct"] == pytest.approx(10.0)
    assert stats["avg_tokens"] == 100
    assert stats["avg_runtime_s"] == 20
    assert stats["verdict_n"] == 4
    assert stats["approved_pct"] == pytest.approx(75.0)


def test_profile_outcome_stats_last_n_is_per_profile_window(kanban_home):
    with kb.connect_closing() as conn:
        base = 1_700_001_000
        for i in range(2):
            _insert_profile_outcome_run(
                conn,
                profile="coder",
                outcome="completed",
                started_at=base + i,
            )
        for i, outcome in enumerate(["blocked", "timed_out", "timed_out"], start=2):
            _insert_profile_outcome_run(
                conn,
                profile="coder",
                outcome=outcome,
                started_at=base + i,
            )
        for i in range(3):
            _insert_profile_outcome_run(
                conn,
                profile="researcher",
                outcome="completed",
                started_at=base + i,
            )
        conn.commit()

        stats = kb.profile_outcome_stats(conn, last_n=3)

    assert stats["coder"]["runs"] == 3
    assert stats["coder"]["done_pct"] == pytest.approx(0.0)
    assert stats["coder"]["blocked_pct"] == pytest.approx(100.0 / 3.0)
    assert stats["coder"]["timeout_pct"] == pytest.approx(200.0 / 3.0)
    assert stats["researcher"]["runs"] == 3
    assert stats["researcher"]["done_pct"] == pytest.approx(100.0)


def test_cost_pipeline_glm52_pricing_and_suffix_fallback(kanban_home):
    assert kb._equiv_from_tokens(None, "glm-5.2", 1_000_000, 1_000_000) == pytest.approx(2.80)
    assert kb._equiv_from_tokens(None, "glm-5.2-fast", 1_000_000, 1_000_000) == pytest.approx(2.80)
    assert kb._equiv_from_tokens(None, "glm-5.2-short", 1_000_000, 1_000_000) == pytest.approx(2.80)
    assert kb._equiv_from_tokens(None, "glm-5.2-short-fast", 1_000_000, 1_000_000) == pytest.approx(2.80)


def test_cost_pipeline_unknown_neuralwatt_cost_status_is_metadata_only(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="unknown-cost", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn,
            tid,
            result="done",
            metadata={
                "provider": "neuralwatt",
                "model": "no-such-model",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
        row = conn.execute(
            "SELECT cost_usd, cost_status, metadata FROM task_runs WHERE task_id=?",
            (tid,),
        ).fetchone()
        assert row["cost_usd"] is None
        assert row["cost_status"] is None
        assert json.loads(row["metadata"])["cost"]["cost_status"] == "unknown"


def test_k5b_backfills_cost_from_state_db_on_session_match(kanban_home):
    """K5b: a run with only worker_session_id (no in-process usage) gets its
    token/cost backfilled from the matching state.db session."""
    sid = "sess-abc"
    _write_state_session(
        kanban_home, sid, input_tokens=900, output_tokens=120, actual_cost=0.044,
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="acp-work", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn, tid, result="done", metadata={"worker_session_id": sid},
        )
        run = kb.list_runs(conn, tid)[0]
        assert run.input_tokens == 900
        assert run.output_tokens == 120
        assert run.cost_usd == pytest.approx(0.044)


def test_k5b_uses_estimated_cost_when_actual_is_null(kanban_home):
    """K5b: estimated_cost_usd is the fallback when actual_cost_usd is NULL."""
    sid = "sess-est"
    _write_state_session(
        kanban_home, sid, input_tokens=1, output_tokens=2,
        actual_cost=None, estimated_cost=0.02,
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="acp-work", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn, tid, result="done", metadata={"worker_session_id": sid},
        )
        assert kb.list_runs(conn, tid)[0].cost_usd == pytest.approx(0.02)


def test_k5b_in_process_metadata_wins_over_backfill(kanban_home):
    """K5b only fills GAPS: an in-process cost is kept; only the missing
    token columns are backfilled from state.db."""
    sid = "sess-mix"
    _write_state_session(
        kanban_home, sid, input_tokens=700, output_tokens=80, actual_cost=9.99,
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="acp-work", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn, tid, result="done",
            metadata={"worker_session_id": sid, "cost_usd": 0.01},
        )
        run = kb.list_runs(conn, tid)[0]
        assert run.cost_usd == pytest.approx(0.01)  # in-process value kept
        assert run.input_tokens == 700  # gap backfilled
        assert run.output_tokens == 80


def test_k5b_no_session_match_is_noop(kanban_home):
    """K5b: a worker_session_id absent from state.db leaves cost NULL."""
    _write_state_session(
        kanban_home, "other-sess", input_tokens=10, output_tokens=5, actual_cost=0.01,
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn, tid, result="done", metadata={"worker_session_id": "missing-sess"},
        )
        run = kb.list_runs(conn, tid)[0]
        assert run.cost_usd is None
        assert run.input_tokens is None


def test_k5b_missing_state_db_is_fail_soft(kanban_home):
    """K5b: no state.db on disk → NO-OP, _end_run never raises."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn, tid, result="done", metadata={"worker_session_id": "any"},
        )
        assert kb.list_runs(conn, tid)[0].cost_usd is None


def test_k5b_state_db_without_sessions_table_is_fail_soft(kanban_home):
    """K5b: a state.db lacking the sessions table → caught, NO-OP."""
    db = Path(kanban_home) / "state.db"
    c0 = sqlite3.connect(str(db))
    c0.execute("CREATE TABLE other (x)")
    c0.commit()
    c0.close()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn, tid, result="done", metadata={"worker_session_id": "any"},
        )
        assert kb.list_runs(conn, tid)[0].cost_usd is None


def test_runs_issues_explains_review_and_dependency_causes(kanban_home):
    now = int(time.time())
    with kb.connect_closing() as conn:
        review_task = kb.create_task(conn, title="Prüfung reparieren", assignee="coder")
        dependency_task = kb.create_task(conn, title="Nach P1 integrieren", assignee="premium")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked', block_kind='review_revision' WHERE id=?",
                (review_task,),
            )
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, summary, metadata) "
                "VALUES (?, 'reviewer', 'done', 'blocked', ?, ?, ?, ?)",
                (
                    review_task,
                    now - 30,
                    now - 20,
                    "REQUEST_CHANGES: Nachweis für den Grenzfall fehlt",
                    json.dumps({"review_verdict": "REQUEST_CHANGES", "blocking_findings": ["Grenzfall"]}),
                ),
            )
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, summary) "
                "VALUES (?, 'premium', 'done', 'blocked', ?, ?, ?)",
                (
                    dependency_task,
                    now - 10,
                    now - 5,
                    "Wartet auf vorgelagerte P1-Integration",
                ),
            )

        payload = kb.runs_issues(conn, days=1)

    by_cause = {issue["cause_key"]: issue for issue in payload["issues"]}
    assert by_cause["review"]["cause_label"] == "Review-Korrektur"
    assert by_cause["review"]["example_task_title"] == "Prüfung reparieren"
    assert by_cause["review"]["example_assignee"] == "coder"
    assert by_cause["review"]["example_block_kind"] == "review_revision"
    assert by_cause["dependency"]["cause_label"] == "Abhängigkeit"
    assert "vorgelagerte Arbeit" in by_cause["dependency"]["cause_hint"]


def test_connect_migrates_legacy_task_attachments_before_unique_index(
    kanban_home, tmp_path,
):
    """Regression 2026-07-23: boards whose ``task_attachments`` table
    predates the W3-S2 sha256/artifact_kind/immutable columns crashed every
    connect() with ``sqlite3.OperationalError: no such column: sha256``
    because SCHEMA_SQL created the unique index over columns the additive
    migration had not backfilled yet (``CREATE TABLE IF NOT EXISTS`` no-ops
    on the legacy table, then the index blew up before
    ``_migrate_add_optional_columns`` ran). The index now lives in the
    migration pass, after the column backfill."""
    legacy_db = tmp_path / "legacy.db"
    raw = sqlite3.connect(legacy_db)
    raw.execute(
        "CREATE TABLE task_attachments ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT NOT NULL, "
        "filename TEXT NOT NULL, "
        "stored_path TEXT NOT NULL, "
        "content_type TEXT, "
        "size INTEGER NOT NULL DEFAULT 0, "
        "uploaded_by TEXT, "
        "created_at INTEGER NOT NULL)"
    )
    raw.commit()
    raw.close()

    with kb.connect_closing(db_path=legacy_db) as conn:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(task_attachments)")
        }
        assert {"sha256", "artifact_kind", "immutable"} <= cols
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_attachments_task_kind_sha'"
        ).fetchone()
        assert idx is not None
        # And the fresh-DB fast path still works on the next connect.
        assert conn.execute(
            "SELECT count(*) FROM task_attachments"
        ).fetchone()[0] == 0


def test_connect_migrates_real_health_track_board_copy(kanban_home, tmp_path):
    """Same legacy-schema regression against a COPY of the production
    ``health-track`` board DB — the Alt-Schema DB whose dispatcher tick
    failed every minute since 96f0eee43. Read-only backup into tmp; the
    live file is never opened for writing. Skipped on hosts without the
    live board."""
    src = Path("/home/piet/.hermes/kanban/boards/health-track/kanban.db")
    if not src.exists():
        pytest.skip("live health-track board DB not present on this host")
    copy = tmp_path / "health-track-copy.db"
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(copy)
        try:
            src_conn.backup(dst)
        finally:
            dst.close()
    finally:
        src_conn.close()

    with kb.connect_closing(db_path=copy) as conn:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(task_attachments)")
        }
        assert {"sha256", "artifact_kind", "immutable"} <= cols
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_attachments_task_kind_sha'"
        ).fetchone()
        assert idx is not None
