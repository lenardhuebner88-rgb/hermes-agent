"""Tests for the Kanban DB layer (hermes_cli.kanban_db)."""

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


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "kanban@example.com"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Kanban Test"], check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)


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


_PLANSPEC_COLS = [
    "planspec_subtask_id",
    "planspec_source",
    "freigabe",
    "live_test_depth",
]


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


def test_profile_outcome_stats_fails_soft_when_task_runs_absent():
    conn = sqlite3.connect(":memory:")
    assert kb.profile_outcome_stats(conn) == {}


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
):
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, "
        "outcome, input_tokens, output_tokens, metadata) "
        "VALUES (?, ?, 'done', ?, ?, 'completed', ?, ?, ?)",
        (
            task_id,
            profile,
            started_at,
            ended_at,
            input_tokens,
            output_tokens,
            json.dumps(metadata) if metadata is not None else None,
        ),
    )


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
        "input_tokens": 600,
        "output_tokens": 60,
        "total_tokens": 660,
    }
    assert burn["by_lane"] == [
        {
            "subscription": "chatgpt",
            "profile": "premium",
            "runs": 1,
            "input_tokens": 300,
            "output_tokens": 30,
            "total_tokens": 330,
        },
        {
            "subscription": "claude",
            "profile": "coder-claude",
            "runs": 2,
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
        "input_tokens": 123,
        "output_tokens": 45,
        "total_tokens": 168,
    }
    assert burn["by_class"][0]["subscription"] == "chatgpt"
    assert burn["by_lane"][0]["subscription"] == "chatgpt"


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


def _write_state_session(
    home, session_id, *,
    input_tokens=None, output_tokens=None,
    actual_cost=None, estimated_cost=None,
    model=None, billing_provider=None,
    cache_read_tokens=None, cache_write_tokens=None,
):
    """Create a minimal state.db with a single sessions row (K5b fixture)."""
    db = Path(home) / "state.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "id TEXT PRIMARY KEY, input_tokens INTEGER, output_tokens INTEGER, "
            "actual_cost_usd REAL, estimated_cost_usd REAL, "
            "model TEXT, billing_provider TEXT, "
            "cache_read_tokens INTEGER, cache_write_tokens INTEGER)"
        )
        conn.execute(
            "INSERT INTO sessions "
            "(id, input_tokens, output_tokens, actual_cost_usd, estimated_cost_usd, "
            "model, billing_provider, cache_read_tokens, cache_write_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, input_tokens, output_tokens, actual_cost, estimated_cost,
                model, billing_provider, cache_read_tokens, cache_write_tokens,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db


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


# ---------------------------------------------------------------------------
# K16: deferred, profile-aware cost backfill
# ---------------------------------------------------------------------------

def _write_profile_state_session(
    profile_dir, session_id, *,
    input_tokens=None, output_tokens=None,
    actual_cost=None, estimated_cost=None,
    model=None, billing_provider=None,
    cache_read_tokens=None, cache_write_tokens=None,
):
    """Create a profile-local state.db with a single sessions row (K16)."""
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    db = profile_dir / "state.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "id TEXT PRIMARY KEY, input_tokens INTEGER, output_tokens INTEGER, "
            "actual_cost_usd REAL, estimated_cost_usd REAL, "
            "model TEXT, billing_provider TEXT, "
            "cache_read_tokens INTEGER, cache_write_tokens INTEGER)"
        )
        conn.execute(
            "INSERT INTO sessions "
            "(id, input_tokens, output_tokens, actual_cost_usd, estimated_cost_usd, "
            "model, billing_provider, cache_read_tokens, cache_write_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, input_tokens, output_tokens, actual_cost, estimated_cost,
                model, billing_provider, cache_read_tokens, cache_write_tokens,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _insert_ended_run(conn, task_id, *, profile, metadata, ended_at=None):
    """Insert a closed run row directly (cost_usd NULL). Returns run id."""
    import json as _json
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, profile, status, started_at, ended_at, outcome, metadata) "
        "VALUES (?, ?, 'done', ?, ?, 'completed', ?)",
        (
            task_id, profile, now, ended_at if ended_at is not None else now,
            _json.dumps(metadata) if metadata is not None else None,
        ),
    )
    conn.commit()
    return cur.lastrowid


def test_k16_backfill_cost_profile_aware_lookup(kanban_home, tmp_path, monkeypatch):
    """K16: with profile=… the per-profile state.db is used (not the hub one)."""
    profile_dir = tmp_path / "profiles" / "critic"
    sid = "sess-prof"
    _write_profile_state_session(
        profile_dir, sid, input_tokens=850, output_tokens=130, estimated_cost=0.0522,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(profile_dir),
    )
    in_tok, out_tok, cost = kb._backfill_cost_from_state_db(sid, profile="critic")
    assert in_tok == 850
    assert out_tok == 130
    assert cost == pytest.approx(0.0522)


def test_k16_backfill_cost_prefers_actual_over_estimated(kanban_home, tmp_path, monkeypatch):
    """K16: actual_cost_usd wins over estimated_cost_usd when both present."""
    profile_dir = tmp_path / "profiles" / "coder"
    sid = "sess-both"
    _write_profile_state_session(
        profile_dir, sid, input_tokens=10, output_tokens=20,
        actual_cost=0.07, estimated_cost=0.05,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(profile_dir),
    )
    _, _, cost = kb._backfill_cost_from_state_db(sid, profile="coder")
    assert cost == pytest.approx(0.07)


def test_k16_backfill_run_costs_sets_cost_and_counts(kanban_home, tmp_path, monkeypatch):
    """K16: an ended run with NULL cost + worker_session_id gets its cost
    backfilled from the run's per-profile state.db; idempotent on re-run."""
    profile_dir = tmp_path / "profiles" / "critic"
    sid = "S1"
    _write_profile_state_session(
        profile_dir, sid, input_tokens=900, output_tokens=140, estimated_cost=0.033,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(profile_dir),
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="costed", assignee="critic")
        run_id = _insert_ended_run(
            conn, tid, profile="critic",
            metadata={"worker_session_id": sid},
        )

        n = kb.backfill_run_costs(conn, limit=50)
        assert n == 1

        row = conn.execute(
            "SELECT input_tokens, output_tokens, cost_usd FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert row["input_tokens"] == 900
        assert row["output_tokens"] == 140
        assert row["cost_usd"] == pytest.approx(0.033)

        # Idempotent: cost is no longer NULL → the row is no longer a candidate.
        assert kb.backfill_run_costs(conn, limit=50) == 0


def test_k16_backfill_subscription_stamps_cache_inclusive_equivalent(
    kanban_home, tmp_path, monkeypatch
):
    """K16 must not freeze subscription rows before the API equivalent lands."""
    profile_dir = tmp_path / "profiles" / "reviewer"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda p: "kimi")
    sid = "S-kimi-k27"
    _write_profile_state_session(
        profile_dir, sid,
        input_tokens=1000,
        output_tokens=2000,
        estimated_cost=0.0,
        model="kimi-k2.7",
        billing_provider="kimi",
        cache_read_tokens=3000,
        cache_write_tokens=4000,
    )

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="sub", assignee="reviewer")
        run_id = _insert_ended_run(
            conn, tid, profile="reviewer", metadata={"worker_session_id": sid},
        )

        assert kb.backfill_run_costs(conn, limit=50) == 1
        row = conn.execute(
            "SELECT input_tokens, output_tokens, cost_usd, metadata "
            "FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert row["input_tokens"] == 1000
        assert row["output_tokens"] == 2000
        assert row["cost_usd"] == pytest.approx(0.0)
        meta = json.loads(row["metadata"])
        assert meta["billing_mode"] == "subscription_included"
        assert meta["subscription"] == "kimi"
        assert meta["model"] == "kimi-k2.7"
        assert meta["cost_usd_equivalent"] == pytest.approx(0.01095)

        # Idempotent: K16 already moved the row out of the cost_usd-NULL gate.
        assert kb.backfill_run_costs(conn, limit=50) == 0
        meta2 = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()["metadata"]
        assert json.loads(meta2)["cost_usd_equivalent"] == pytest.approx(0.01095)


def test_k16_kimi_k27_price_override_is_available():
    assert kb._lookup_model_price_per_mtok("kimi", "kimi-k2.7") == pytest.approx(
        (0.67, 3.50, 0.20, 0.67)
    )


def test_b1_glm52_price_override_entries():
    """AC-1: _PRICE_OVERRIDES_PER_MTOK has explicit entries for the glm-5.2 family."""
    assert "glm-5.2" in kb._PRICE_OVERRIDES_PER_MTOK
    assert "glm-5.2-fast" in kb._PRICE_OVERRIDES_PER_MTOK
    assert "glm-5.2-short" in kb._PRICE_OVERRIDES_PER_MTOK
    # All variants inherit base pricing: input $0.60/M, output $2.20/M
    for model in ("glm-5.2", "glm-5.2-fast", "glm-5.2-short"):
        rates = kb._PRICE_OVERRIDES_PER_MTOK[model]
        assert rates[0] == pytest.approx(0.60)  # input
        assert rates[1] == pytest.approx(2.20)  # output


def test_b1_glm52_price_override_via_lookup():
    """AC-1: the override dict is consulted by _lookup_model_price_per_mtok."""
    rates = kb._lookup_model_price_per_mtok("neuralwatt", "glm-5.2")
    assert rates is not None
    assert rates[0] == pytest.approx(0.60)
    assert rates[1] == pytest.approx(2.20)


def test_b2_strip_model_variant_suffix():
    """AC-2: suffix truncation for -fast, -short, -short-fast variants."""
    assert kb._strip_model_variant_suffix("glm-5.2-fast") == "glm-5.2"
    assert kb._strip_model_variant_suffix("glm-5.2-short") == "glm-5.2"
    assert kb._strip_model_variant_suffix("glm-5.2-short-fast") == "glm-5.2"
    # No known suffix → None (caller should not retry)
    assert kb._strip_model_variant_suffix("gpt-5.5") is None
    assert kb._strip_model_variant_suffix("") is None


def test_b3_neuralwatt_cost_block_extraction():
    """AC-3: _extract_run_cost_tokens reads metadata.cost.request_cost_usd."""
    metadata = {
        "cost": {
            "request_cost_usd": 0.0042,
            "cost_status": "actual",
        }
    }
    in_tok, out_tok, cost = kb._extract_run_cost_tokens(metadata)
    assert cost == pytest.approx(0.0042)
    status = kb._extract_run_cost_status(metadata)
    assert status == "actual"


def test_b3_neuralwatt_cost_status_estimated_fallback(kanban_home, tmp_path, monkeypatch):
    """AC-3: when response cost is missing, _end_run falls back to estimated."""
    monkeypatch.setattr(
        kb, "_lookup_model_price_per_mtok",
        lambda provider, model: (0.60, 2.20, 0.0, 0.0) if model and "glm-5.2" in model else None,
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="nw-fallback", assignee="coder")
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn, tid, result="done",
            metadata={
                "provider": "neuralwatt",
                "model": "glm-5.2-fast",
                "input_tokens": 10000,
                "output_tokens": 5000,
            },
        )
        row = conn.execute(
            "SELECT cost_usd, cost_status FROM task_runs WHERE task_id=?", (tid,)
        ).fetchone()
        assert row["cost_usd"] is not None
        assert row["cost_usd"] > 0
        assert row["cost_status"] == "estimated"


def test_b3_neuralwatt_cost_status_unknown_when_no_pricing(kanban_home, tmp_path, monkeypatch):
    """AC-3: when both response cost and models.dev pricing are unavailable,
    cost_status is 'unknown' in the metadata (never hard-error). The
    task_runs.cost_status column stays NULL because its CHECK constraint
    only accepts actual/estimated."""
    monkeypatch.setattr(
        kb, "_lookup_model_price_per_mtok", lambda provider, model: None,
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="nw-unknown", assignee="coder")
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn, tid, result="done",
            metadata={"provider": "neuralwatt", "model": "unknown-model"},
        )
        row = conn.execute(
            "SELECT cost_usd, cost_status, metadata FROM task_runs WHERE task_id=?", (tid,)
        ).fetchone()
        assert row["cost_usd"] is None
        # 'unknown' is in the metadata.cost.cost_status, not the column.
        import json as _json
        meta = _json.loads(row["metadata"]) if row["metadata"] else {}
        cost_block = meta.get("cost", {})
        assert cost_block.get("cost_status") == "unknown"


def test_b4_openrouter_generation_id_extraction():
    """AC-4: _extract_openrouter_generation_id reads response.id."""
    from agent.conversation_loop import _extract_openrouter_generation_id
    resp = type("R", (), {"id": "gen-abc123", "_openrouter_generation_id": None})()
    assert _extract_openrouter_generation_id(resp) == "gen-abc123"
    resp2 = type("R", (), {"id": None})()
    assert _extract_openrouter_generation_id(resp2) is None


def test_b4_openrouter_generation_id_persisted(tmp_path):
    """AC-4: openrouter_generation_id column exists and is writable in state.db."""
    import sqlite3 as _sqlite3
    from hermes_state import SessionDB
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.update_token_counts(
        "sess-or-1", input_tokens=100, output_tokens=50,
        model="glm-5.2", openrouter_generation_id="gen-xyz789",
    )
    conn = _sqlite3.connect(str(db_path))
    try:
        conn.row_factory = _sqlite3.Row
        row = conn.execute(
            "SELECT openrouter_generation_id FROM sessions WHERE id=?", ("sess-or-1",)
        ).fetchone()
        assert row["openrouter_generation_id"] == "gen-xyz789"
    finally:
        conn.close()


def test_k16_backfill_run_costs_skips_run_without_session_id(kanban_home, tmp_path, monkeypatch):
    """K16: a run with no worker_session_id is skipped, never crashes."""
    profile_dir = tmp_path / "profiles" / "critic"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(profile_dir),
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="no-sess", assignee="critic")
        run_id = _insert_ended_run(
            conn, tid, profile="critic", metadata={"other": "x"},
        )
        assert kb.backfill_run_costs(conn, limit=50) == 0
        row = conn.execute(
            "SELECT cost_usd FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()
        assert row["cost_usd"] is None


def test_k16_backfill_run_costs_fail_soft_missing_profile_db(kanban_home, monkeypatch):
    """K16: a profile whose state.db is absent → 0 backfilled, no raise."""
    def _raise(name):
        raise FileNotFoundError(f"Profile '{name}' does not exist.")
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", _raise)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="ghost", assignee="ghost")
        run_id = _insert_ended_run(
            conn, tid, profile="ghost",
            metadata={"worker_session_id": "S-ghost"},
        )
        assert kb.backfill_run_costs(conn, limit=50) == 0
        row = conn.execute(
            "SELECT cost_usd FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()
        assert row["cost_usd"] is None


def test_k16_backfill_cost_falls_back_to_hub_when_profile_none(kanban_home):
    """K16 regression: profile=None preserves the legacy hub-state.db path —
    the _end_run caller (which never passes profile) is unaffected."""
    sid = "hub-sess"
    _write_state_session(
        kanban_home, sid, input_tokens=11, output_tokens=22, actual_cost=0.009,
    )
    in_tok, out_tok, cost = kb._backfill_cost_from_state_db(sid)
    assert in_tok == 11
    assert out_tok == 22
    assert cost == pytest.approx(0.009)


def _write_claude_result_log(task_id, *, total_cost_usd=0.28, input_tokens=11529,
                             cache_creation=24778, cache_read=93776,
                             output_tokens=861, session_id="sess-claude-1"):
    """Append a realistic ``claude -p --output-format json`` result line to the
    per-task worker log (the shape the live CLI v2.1.x emits)."""
    import json as _json
    log_dir = kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": 3, "result": "done", "stop_reason": "end_turn",
        "session_id": session_id, "total_cost_usd": total_cost_usd,
        "usage": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
            "output_tokens": output_tokens,
        },
    }
    with open(log_dir / f"{task_id}.log", "a", encoding="utf-8") as fh:
        fh.write("some non-json worker chatter\n")
        fh.write(_json.dumps(result) + "\n")


def test_k17_backfill_claude_cli_run_stamps_tokens_from_log(kanban_home, monkeypatch):
    """K17: a claude-CLI run (NULL metadata, no state.db session) gets tokens
    from the worker log's result JSON; cost_usd=0.0 (subscription_included)
    with the API-equivalent preserved in metadata. Idempotent on re-run."""
    import json as _json
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cli-costed", assignee="coder-claude")
        run_id = _insert_ended_run(conn, tid, profile="coder-claude", metadata=None)
        _write_claude_result_log(tid)

        assert kb.backfill_run_costs(conn, limit=50) == 1

        row = conn.execute(
            "SELECT input_tokens, output_tokens, cost_usd, metadata "
            "FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        # fresh tokens = input + cache_creation; cache READS excluded
        assert row["input_tokens"] == 11529 + 24778
        assert row["output_tokens"] == 861
        assert row["cost_usd"] == pytest.approx(0.0)
        meta = _json.loads(row["metadata"])
        assert meta["billing_mode"] == "subscription_included"
        assert meta["cost_usd_equivalent"] == pytest.approx(0.28)
        assert meta["claude_session_id"] == "sess-claude-1"
        assert meta["usage"]["input_tokens"] == 11529

        # Idempotent: cost_usd is no longer NULL → no longer a candidate.
        assert kb.backfill_run_costs(conn, limit=50) == 0


def test_k17_backfill_claude_cli_missing_or_garbled_log_fail_soft(kanban_home, monkeypatch):
    """K17: missing log or log without a result line → skipped, no raise,
    run stays NULL (re-scanned next tick)."""
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cli-no-log", assignee="coder-claude")
        run_id = _insert_ended_run(conn, tid, profile="coder-claude", metadata=None)
        assert kb.backfill_run_costs(conn, limit=50) == 0

        log_dir = kb.worker_logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{tid}.log").write_text("plain text\n{broken json\n")
        assert kb.backfill_run_costs(conn, limit=50) == 0

        row = conn.execute(
            "SELECT cost_usd FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()
        assert row["cost_usd"] is None


def test_k17_backfill_claude_cli_skips_stale_run(kanban_home, monkeypatch):
    """K17: only the task's LATEST run is stamped from the shared per-task
    log — an older run never inherits a newer run's result JSON."""
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cli-retry", assignee="coder-claude")
        old_run = _insert_ended_run(conn, tid, profile="coder-claude", metadata=None)
        new_run = _insert_ended_run(conn, tid, profile="coder-claude", metadata=None)
        _write_claude_result_log(tid, output_tokens=42)

        assert kb.backfill_run_costs(conn, limit=50) == 1

        rows = {
            r["id"]: r for r in conn.execute(
                "SELECT id, output_tokens, cost_usd FROM task_runs "
                "WHERE task_id = ?",
                (tid,),
            )
        }
        assert rows[new_run]["output_tokens"] == 42
        assert rows[new_run]["cost_usd"] == pytest.approx(0.0)
        assert rows[old_run]["cost_usd"] is None


def test_k17_backfill_claude_cli_stamps_despite_later_verifier_run(kanban_home, monkeypatch):
    """K17 regression (review-gate): the verifier opens a NEWER run on the same
    task after the claude-cli worker run — that run must not shadow the worker
    run out of the backfill. Only a newer claude-cli run owns the log's last
    result JSON; non-cli runs (verifier, hermes-runtime) never write one."""
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cli-gated", assignee="coder-claude")
        worker_run = _insert_ended_run(conn, tid, profile="coder-claude", metadata=None)
        verifier_run = _insert_ended_run(
            conn, tid, profile="verifier",
            metadata={"verdict": "APPROVED"},
        )
        _write_claude_result_log(tid, output_tokens=77)

        assert kb.backfill_run_costs(conn, limit=50) == 1

        rows = {
            r["id"]: r for r in conn.execute(
                "SELECT id, output_tokens, cost_usd FROM task_runs "
                "WHERE task_id = ?",
                (tid,),
            )
        }
        assert rows[worker_run]["output_tokens"] == 77
        assert rows[worker_run]["cost_usd"] == pytest.approx(0.0)
        # The verifier run has no claude session — untouched.
        assert rows[verifier_run]["cost_usd"] is None


def test_k17_backfill_non_claude_profile_unaffected(kanban_home, monkeypatch):
    """K17 regression: a non-claude-cli run without worker_session_id keeps
    the legacy skip behavior even when a stray log file exists."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="api-lane", assignee="critic")
        run_id = _insert_ended_run(conn, tid, profile="critic", metadata={"other": "x"})
        _write_claude_result_log(tid)
        assert kb.backfill_run_costs(conn, limit=50) == 0
        row = conn.execute(
            "SELECT cost_usd FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()
        assert row["cost_usd"] is None


def test_k17_extract_claude_cli_cost_shapes():
    """K17 unit: fresh-token sum, cache-read exclusion, missing-field tolerance."""
    full = kb._extract_claude_cli_cost({
        "total_cost_usd": 1.5,
        "usage": {"input_tokens": 100, "cache_creation_input_tokens": 50,
                  "cache_read_input_tokens": 9000, "output_tokens": 7},
    })
    assert full == (150, 7, 1.5)
    no_usage = kb._extract_claude_cli_cost({"total_cost_usd": 0.1})
    assert no_usage == (None, None, 0.1)
    zero_cost_partial_usage = kb._extract_claude_cli_cost({
        "total_cost_usd": 0,
        "usage": {"cache_creation_input_tokens": 12, "output_tokens": 4},
    })
    assert zero_cost_partial_usage == (12, 4, 0.0)
    empty = kb._extract_claude_cli_cost({})
    assert empty == (None, None, None)


# ---------------------------------------------------------------------------
# COST-VISIBILITY-WORKERS-S1: session-correlated cost backfill
# ---------------------------------------------------------------------------

def _write_session_rows(db_path, rows):
    """Create a realistic ``state.db`` sessions table (with cwd/source/
    started_at) and insert ``rows`` (list of dicts). Used by the
    session-correlation backfill tests."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "id TEXT PRIMARY KEY, source TEXT, started_at REAL, ended_at REAL, "
            "input_tokens INTEGER, output_tokens INTEGER, "
            "actual_cost_usd REAL, estimated_cost_usd REAL, cwd TEXT, "
            "model TEXT, billing_provider TEXT, cost_status TEXT, "
            "openrouter_generation_id TEXT, "
            "cache_read_tokens INTEGER, cache_write_tokens INTEGER)"
        )
        for r in rows:
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, ended_at, "
                "input_tokens, output_tokens, actual_cost_usd, "
                "estimated_cost_usd, cwd, model, billing_provider, "
                "cost_status, openrouter_generation_id, cache_read_tokens, cache_write_tokens) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r["id"], r.get("source", "cli"), r.get("started_at"),
                    r.get("ended_at"), r.get("input_tokens"),
                    r.get("output_tokens"), r.get("actual_cost_usd"),
                    r.get("estimated_cost_usd"), r.get("cwd"), r.get("model"),
                    r.get("billing_provider"), r.get("cost_status"),
                    r.get("openrouter_generation_id"),
                    r.get("cache_read_tokens"), r.get("cache_write_tokens"),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_run_window(conn, task_id, *, profile, started_at, ended_at,
                       outcome="completed", metadata=None):
    """Insert a closed run with explicit start/end window (cost_usd NULL)."""
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, profile, status, started_at, ended_at, outcome, metadata) "
        "VALUES (?, ?, 'done', ?, ?, ?, ?)",
        (
            task_id, profile, started_at, ended_at, outcome,
            json.dumps(metadata) if metadata is not None else None,
        ),
    )
    conn.commit()
    return cur.lastrowid


def test_batch_task_costs_sums_runs_and_omits_runless_tasks(kanban_home):
    """batch_task_costs: one query sums cost/tokens per task, folds the
    subscription $-equivalent into cost_effective_usd, and omits tasks with no
    runs (so their board cards render no cost footer)."""
    with kb.connect_closing() as conn:
        ran = kb.create_task(conn, title="ran twice", assignee="coder")
        sub = kb.create_task(conn, title="subscription run", assignee="coder-claude")
        idle = kb.create_task(conn, title="never ran")
        with kb.write_txn(conn):
            for cusd, tin, tout in [(0.10, 1000, 200), (0.05, 500, 100)]:
                conn.execute(
                    "INSERT INTO task_runs "
                    "(task_id, profile, status, started_at, ended_at, outcome, "
                    "input_tokens, output_tokens, cost_usd, cost_status, metadata) "
                    "VALUES (?, 'coder', 'done', 1000, 1010, 'completed', ?, ?, ?, 'actual', NULL)",
                    (ran, tin, tout, cusd),
                )
            # Subscription run: metered cost_usd 0, but an estimated $-equivalent.
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, started_at, ended_at, outcome, "
                "input_tokens, output_tokens, cost_usd, cost_status, metadata) "
                "VALUES (?, 'coder-claude', 'done', 1000, 1010, 'completed', ?, ?, ?, 'actual', ?)",
                (sub, 3000, 400, 0.0, json.dumps({"cost_usd_equivalent": 0.42})),
            )

        costs = kb.batch_task_costs(conn, [ran, sub, idle])

    # Metered task: tokens + $ summed across both runs; no subscription equivalent.
    assert costs[ran]["input_tokens"] == 1500
    assert costs[ran]["output_tokens"] == 300
    assert costs[ran]["cost_usd"] == pytest.approx(0.15)
    assert costs[ran]["cost_usd_equivalent"] == pytest.approx(0.0)
    assert costs[ran]["cost_effective_usd"] == pytest.approx(0.15)
    assert costs[ran]["cost_status"] == "actual"
    # Subscription task: metered $0 but the estimated equivalent is the effective $.
    assert costs[sub]["cost_usd"] == pytest.approx(0.0)
    assert costs[sub]["cost_usd_equivalent"] == pytest.approx(0.42)
    assert costs[sub]["cost_effective_usd"] == pytest.approx(0.42)
    assert costs[sub]["cost_status"] == "actual"
    assert costs[sub]["input_tokens"] == 3000
    # A task with no runs is omitted entirely → its card renders no cost footer.
    assert idle not in costs


def test_batch_task_costs_empty_input_returns_empty(kanban_home):
    with kb.connect_closing() as conn:
        assert kb.batch_task_costs(conn, []) == {}


def test_s1_cwd_match_stamps_real_tokens_and_cost(kanban_home, tmp_path, monkeypatch):
    """S1 tier-1 (deterministic): a session whose cwd contains the task_id is
    attributed to the run — real tokens + real cost, provenance recorded."""
    profile_dir = tmp_path / "profiles" / "coder"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda p: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cwd-link", assignee="coder")
        run_id = _insert_run_window(
            conn, tid, profile="coder", started_at=1000, ended_at=2000,
        )
        _write_session_rows(profile_dir / "state.db", [
            {"id": "S-match", "source": "cli", "started_at": 1500,
             "input_tokens": 500, "output_tokens": 40, "actual_cost_usd": 0.12,
             "cwd": f"/home/x/.hermes/kanban/workspaces/{tid}"},
            {"id": "S-other", "source": "cli", "started_at": 1500,
             "input_tokens": 9, "output_tokens": 9, "actual_cost_usd": 9.0,
             "cwd": "/home/x/.hermes/kanban/workspaces/t_deadbeef"},
        ])
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 1
        row = conn.execute(
            "SELECT input_tokens, output_tokens, cost_usd, cost_status, metadata "
            "FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert row["input_tokens"] == 500
        assert row["output_tokens"] == 40
        assert row["cost_usd"] == pytest.approx(0.12)
        assert row["cost_status"] == "actual"
        meta = json.loads(row["metadata"])
        assert meta["cost_source"] == "session_cwd"
        assert any("S-match" in e for e in meta["cost_session_ids"])
        # Idempotent: stamped run is no longer a candidate.
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 0


def test_s1_window_match_in_own_profile_consumed_once(kanban_home, tmp_path, monkeypatch):
    """S1 tier-2 (window): a cli session whose started_at falls in the run's
    active window is attributed; each session is consumed by exactly one run
    (no double-count), and a session outside the window is ignored."""
    profile_dir = tmp_path / "profiles" / "coder"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda p: None)
    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="win-a", assignee="coder")
        t2 = kb.create_task(conn, title="win-b", assignee="coder")
        r1 = _insert_run_window(conn, t1, profile="coder", started_at=1000, ended_at=2000)
        r2 = _insert_run_window(conn, t2, profile="coder", started_at=3000, ended_at=4000)
        _write_session_rows(profile_dir / "state.db", [
            {"id": "S-in1", "source": "cli", "started_at": 1500,
             "input_tokens": 100, "output_tokens": 10, "actual_cost_usd": 0.05},
            {"id": "S-in2", "source": "cli", "started_at": 3500,
             "input_tokens": 200, "output_tokens": 20, "estimated_cost_usd": 0.07},
            {"id": "S-outside", "source": "cli", "started_at": 9999,
             "input_tokens": 1, "output_tokens": 1, "actual_cost_usd": 5.0},
        ])
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 2
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, input_tokens, cost_usd, cost_status, metadata FROM task_runs")}
        assert rows[r1]["input_tokens"] == 100
        assert rows[r1]["cost_usd"] == pytest.approx(0.05)
        assert rows[r1]["cost_status"] == "actual"
        assert rows[r2]["input_tokens"] == 200
        assert rows[r2]["cost_usd"] == pytest.approx(0.07)
        assert rows[r2]["cost_status"] == "estimated"
        assert json.loads(rows[r1]["metadata"])["cost_source"] == "session_window"
        # S-outside never attributed → its $5.0 never enters any run.
        total = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM task_runs").fetchone()[0]
        assert total == pytest.approx(0.12)


def test_s1_window_does_not_cross_profiles(kanban_home, tmp_path, monkeypatch):
    """S1: window correlation reads ONLY the run's own profile state.db — a
    session in a different profile's db is never window-matched."""
    def _resolve(name):
        return str(tmp_path / "profiles" / name)
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", _resolve)
    monkeypatch.setattr(kb, "_profile_subscription", lambda p: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x-prof", assignee="coder")
        run_id = _insert_run_window(conn, tid, profile="coder", started_at=1000, ended_at=2000)
        # session lives in critic's db, not coder's → must not match
        _write_session_rows(tmp_path / "profiles" / "critic" / "state.db", [
            {"id": "S-critic", "source": "cli", "started_at": 1500,
             "input_tokens": 5, "output_tokens": 5, "actual_cost_usd": 1.0},
        ])
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 0
        assert conn.execute(
            "SELECT cost_usd FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] is None


def test_s1_subscription_zero_metered_when_no_session(kanban_home, tmp_path, monkeypatch):
    """S1 tier-3: a run on a provable subscription lane with no recoverable
    session is stamped cost_usd=0.0 (real metered cost), billing_mode recorded,
    tokens left NULL — and cost_usd_total does NOT rise."""
    profile_dir = tmp_path / "profiles" / "coder-claude"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription",
                        lambda p: "claude" if p == "coder-claude" else None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="sub-zero", assignee="coder-claude")
        run_id = _insert_run_window(
            conn, tid, profile="coder-claude", started_at=1000, ended_at=2000)
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 1
        row = conn.execute(
            "SELECT input_tokens, cost_usd, metadata FROM task_runs WHERE id=?",
            (run_id,)).fetchone()
        assert row["cost_usd"] == pytest.approx(0.0)
        assert row["input_tokens"] is None
        meta = json.loads(row["metadata"])
        assert meta["cost_source"] == "subscription_zero_metered"
        assert meta["billing_mode"] == "subscription_included"
        assert meta["subscription"] == "claude"


def test_s1_subscription_match_stamps_equivalent_not_metered(kanban_home, tmp_path, monkeypatch):
    """COST-VISIBILITY-WORKERS-S2: a subscription lane that DOES match a session
    keeps cost_usd=0.0 (real metered — the burn rides the subscription) but
    surfaces the session's estimated_cost_usd as metadata.cost_usd_equivalent
    (generalising K17 beyond claude-cli) and stamps the session's model. Tokens
    are still attributed. This is what lights up the 'teure' lanes (Codex/
    verifier/coder) that today show $0/'—' while burning real value."""
    profile_dir = tmp_path / "profiles" / "coder-claude"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription",
                        lambda p: "claude" if p == "coder-claude" else None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="sub-match", assignee="coder-claude")
        run_id = _insert_run_window(
            conn, tid, profile="coder-claude", started_at=1000, ended_at=2000)
        _write_session_rows(profile_dir / "state.db", [
            {"id": "S-sub", "source": "cli", "started_at": 1500,
             "input_tokens": 800, "output_tokens": 60, "actual_cost_usd": None,
             "estimated_cost_usd": 0.20, "model": "claude-opus-4-8",
             "cwd": f"/x/kanban/workspaces/{tid}"},
        ])
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 1
        row = conn.execute(
            "SELECT input_tokens, output_tokens, cost_usd, metadata "
            "FROM task_runs WHERE id=?", (run_id,)).fetchone()
        # real metered cost stays $0 — the metric-integrity invariant holds.
        assert row["cost_usd"] == pytest.approx(0.0)
        assert row["input_tokens"] == 800
        assert row["output_tokens"] == 60
        meta = json.loads(row["metadata"])
        assert meta["cost_usd_equivalent"] == pytest.approx(0.20)
        assert meta["model"] == "claude-opus-4-8"
        assert meta["billing_mode"] == "subscription_included"
        assert meta["subscription"] == "claude"
        assert meta["cost_source"] == "session_cwd"


def test_s1_subscription_actual_does_not_leak_into_metered(kanban_home, tmp_path, monkeypatch):
    """COST-VISIBILITY-WORKERS-S2 invariant (Codex cross-family catch): a
    subscription-lane session carrying a stray actual_cost_usd>0 (a metered
    fallback leg / misconfig) must NOT leak into real cost_usd — that would
    contradict billing_mode=subscription_included and corrupt the
    tasks_without_cost_data metric. cost_usd stays $0; the actual surfaces only as
    the labeled equivalent."""
    profile_dir = tmp_path / "profiles" / "coder-claude"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription",
                        lambda p: "claude" if p == "coder-claude" else None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="sub-actual", assignee="coder-claude")
        run_id = _insert_run_window(
            conn, tid, profile="coder-claude", started_at=1000, ended_at=2000)
        _write_session_rows(profile_dir / "state.db", [
            {"id": "S-leak", "source": "cli", "started_at": 1500,
             "input_tokens": 100, "output_tokens": 10, "actual_cost_usd": 0.40,
             "estimated_cost_usd": None, "model": "claude-opus-4-8",
             "cwd": f"/x/kanban/workspaces/{tid}"},
        ])
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 1
        row = conn.execute(
            "SELECT cost_usd, metadata FROM task_runs WHERE id=?",
            (run_id,)).fetchone()
        assert row["cost_usd"] == pytest.approx(0.0)  # NOT 0.40 — no leak
        meta = json.loads(row["metadata"])
        assert meta["cost_usd_equivalent"] == pytest.approx(0.40)
        assert meta["billing_mode"] == "subscription_included"


def test_s1_claude_included_session_priced_despite_mismatched_billing_provider(
    kanban_home, tmp_path, monkeypatch, caplog,
):
    """Real Claude subscription sessions can carry billing_provider=openai-codex.

    Pricing must key on the claude-* model label instead of trusting the mismatched
    billing provider, otherwise cost_usd_equivalent stays empty for real Opus runs.
    """
    import logging

    from agent.models_dev import ModelInfo

    profile_dir = tmp_path / "profiles" / "premium"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription",
                        lambda p: "claude" if p == "premium" else None)

    def fake_get_model_info(provider, model):
        if (provider, model) == ("anthropic", "claude-opus-4-8"):
            return ModelInfo(
                id="claude-opus-4-8",
                name="Claude Opus 4.8",
                family="claude-opus",
                provider_id="anthropic",
                cost_input=5.0,
                cost_output=25.0,
                cost_cache_read=0.5,
                cost_cache_write=6.25,
            )
        return None

    monkeypatch.setattr("agent.models_dev.get_model_info", fake_get_model_info)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="claude-mismatch", assignee="premium")
        run_id = _insert_run_window(
            conn, tid, profile="premium", started_at=1000, ended_at=2000)
        _write_session_rows(profile_dir / "state.db", [
            {"id": "S-claude-mismatch", "source": "claude-cli", "started_at": 1500,
             "input_tokens": 1_000_000, "output_tokens": 100_000,
             "cache_read_tokens": 2_000_000, "cache_write_tokens": 100_000,
             "actual_cost_usd": None, "estimated_cost_usd": 0.0,
             "model": "claude-opus-4-8", "billing_provider": "openai-codex",
             "cwd": f"/x/kanban/workspaces/{tid}"},
        ])
        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
            assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 1
        row = conn.execute(
            "SELECT cost_usd, metadata FROM task_runs WHERE id=?",
            (run_id,)).fetchone()
        assert row["cost_usd"] == pytest.approx(0.0)
        meta = json.loads(row["metadata"])
        # 1M in × $5 + 0.1M out × $25 + 2M cr × $0.5 + 0.1M cw × $6.25
        assert meta["cost_usd_equivalent"] == pytest.approx(9.125)
        assert meta["model"] == "claude-opus-4-8"
        assert meta["billing_mode"] == "subscription_included"
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "model/billing_provider family mismatch" in message
            and f"run_id={run_id}" in message
            and "model=claude-opus-4-8" in message
            and "billing_provider=openai-codex" in message
            for message in warnings
        ), warnings


def test_s1_codex_included_session_priced_from_models_dev(kanban_home, tmp_path, monkeypatch):
    """COST-VISIBILITY-WORKERS-S2: a codex subscription session stamps
    estimated_cost_usd=0 ('included'), so the runtime leaves it unpriced. The
    backfill then computes the API-equivalent as tokens × online price (models.
    dev) for the session's model — this is what finally lights up the teure
    Codex lanes that otherwise show $0/'—'. Real cost_usd stays $0."""
    profile_dir = tmp_path / "profiles" / "coder"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription",
                        lambda p: "chatgpt" if p == "coder" else None)
    # Pin the price so the test is hermetic (no models.dev network/cache dep):
    # gpt-5.5 = $5/Mtok in, $30/Mtok out.
    monkeypatch.setattr(
        kb, "_lookup_model_price_per_mtok",
        lambda provider, model: (5.0, 30.0, 0.5, 6.25) if model == "gpt-5.5" else None,
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="codex-burn", assignee="coder")
        run_id = _insert_run_window(
            conn, tid, profile="coder", started_at=1000, ended_at=2000)
        _write_session_rows(profile_dir / "state.db", [
            {"id": "S-codex", "source": "cli", "started_at": 1500,
             "input_tokens": 1_000_000, "output_tokens": 100_000,
             "actual_cost_usd": None, "estimated_cost_usd": 0.0,
             "model": "gpt-5.5", "billing_provider": "openai-codex",
             "cwd": f"/x/kanban/workspaces/{tid}"},
        ])
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 1
        row = conn.execute(
            "SELECT cost_usd, metadata FROM task_runs WHERE id=?",
            (run_id,)).fetchone()
        assert row["cost_usd"] == pytest.approx(0.0)  # real metered stays $0
        meta = json.loads(row["metadata"])
        # 1M in × $5 + 0.1M out × $30 = 5.0 + 3.0 = $8.00
        assert meta["cost_usd_equivalent"] == pytest.approx(8.0)
        assert meta["model"] == "gpt-5.5"
        assert meta["billing_mode"] == "subscription_included"


def test_s1_codex_equivalent_includes_cache_read(kanban_home, tmp_path, monkeypatch):
    """COST-VISIBILITY-WORKERS-S3: codex burns mostly cache-read tokens (a
    separate, additive column — prompt = input + cache_read + cache_write per the
    runtime's CanonicalUsage). The equivalent must price cache_read/cache_write at
    their own rates, else the Codex lane is under-counted by ~half. cost_usd stays
    $0."""
    profile_dir = tmp_path / "profiles" / "coder"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription",
                        lambda p: "chatgpt" if p == "coder" else None)
    # gpt-5.5: in $5, out $30, cache_read $0.5, cache_write $6.25 (per Mtok)
    monkeypatch.setattr(
        kb, "_lookup_model_price_per_mtok",
        lambda provider, model: (5.0, 30.0, 0.5, 6.25) if model == "gpt-5.5" else None,
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="codex-cache", assignee="coder")
        run_id = _insert_run_window(
            conn, tid, profile="coder", started_at=1000, ended_at=2000)
        _write_session_rows(profile_dir / "state.db", [
            {"id": "S-cache", "source": "cli", "started_at": 1500,
             "input_tokens": 1_000_000, "output_tokens": 100_000,
             "cache_read_tokens": 10_000_000, "cache_write_tokens": 200_000,
             "actual_cost_usd": None, "estimated_cost_usd": 0.0,
             "model": "gpt-5.5", "billing_provider": "openai-codex",
             "cwd": f"/x/kanban/workspaces/{tid}"},
        ])
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 1
        meta = json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()[0])
        # 1M×$5 + 0.1M×$30 + 10M×$0.5 + 0.2M×$6.25 = 5 + 3 + 5 + 1.25 = $14.25
        assert meta["cost_usd_equivalent"] == pytest.approx(14.25)


def test_s1b_audited_claude_equivalent_dry_run_and_apply(kanban_home, tmp_path, monkeypatch):
    """S1b: stamp missing Claude cost_usd_equivalent only from session-log evidence.

    Golden Opus run: 131747 input, 4793 output, 350208 cache-read -> $0.953664.
    Dry-run reports the candidate but does not mutate; apply writes only metadata.
    """
    profile_dir = tmp_path / "profiles" / "premium"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(
        kb,
        "_lookup_model_price_per_mtok",
        lambda provider, model: (5.0, 25.0, 0.5, 6.25)
        if model == "claude-opus-4-8" else None,
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="s1b-golden", assignee="premium")
        run_id = _insert_run_window(
            conn,
            tid,
            profile="premium",
            started_at=1000,
            ended_at=2000,
            metadata={"worker_session_id": "S-golden"},
        )
        _write_session_rows(profile_dir / "state.db", [{
            "id": "S-golden", "source": "claude-cli", "started_at": 1500,
            "input_tokens": 131_747, "output_tokens": 4_793,
            "cache_read_tokens": 350_208, "cache_write_tokens": 0,
            "actual_cost_usd": None, "estimated_cost_usd": 0.0,
            "model": "claude-opus-4-8", "billing_provider": "anthropic",
            "cwd": f"/x/kanban/workspaces/{tid}",
        }])
        dry = kb.audit_claude_cost_equivalent_backfill(conn, limit=50)
        assert dry["mode"] == "dry_run"
        assert dry["classes"]["worker_receipt_without_cost_stamp"] == 1
        assert dry["classes"]["provider_model_without_equiv"] == 0
        assert dry["classes"]["null_cost_no_cost_evidence"] == 0
        assert dry["classes"]["operator_integration"] == 0
        assert dry["updated"] == 0
        assert json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()[0]).get("cost_usd_equivalent") is None

        applied = kb.audit_claude_cost_equivalent_backfill(conn, limit=50, apply=True)
        assert applied["updated"] == 1
        meta = json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()[0])
        assert meta["cost_usd_equivalent"] == pytest.approx(0.953664)
        assert meta["cost_equivalent_model"] == "claude-opus-4-8"
        assert meta["cost_equivalent_provider"] == "anthropic"
        assert meta["provider_model_source"] == "session_log"
        assert meta["cost_equivalent_source"] == "s1b_audited_session_usage"
        assert meta["billing_mode"] == "subscription_included"


def test_s1b_audited_claude_equivalent_requires_model_label(kanban_home, tmp_path, monkeypatch):
    """S1b: a Claude-like run with tokens but no persisted model label is classified, not stamped."""
    profile_dir = tmp_path / "profiles" / "premium"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(
        kb,
        "_lookup_model_price_per_mtok",
        lambda provider, model: (5.0, 25.0, 0.5, 6.25)
        if model == "claude-opus-4-8" else None,
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="s1b-missing-model", assignee="premium")
        run_id = _insert_run_window(
            conn,
            tid,
            profile="premium",
            started_at=1000,
            ended_at=2000,
            metadata={"worker_session_id": "S-no-model"},
        )
        _write_session_rows(profile_dir / "state.db", [{
            "id": "S-no-model", "source": "claude-cli", "started_at": 1500,
            "input_tokens": 131_747, "output_tokens": 4_793,
            "cache_read_tokens": 350_208, "actual_cost_usd": None,
            "estimated_cost_usd": 0.0, "model": None, "billing_provider": "anthropic",
            "cwd": f"/x/kanban/workspaces/{tid}",
        }])
        report = kb.audit_claude_cost_equivalent_backfill(conn, limit=50, apply=True)
        assert report["updated"] == 0
        assert report["classes"]["provider_model_without_equiv"] == 1
        meta = json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()[0])
        assert "cost_usd_equivalent" not in meta


def test_s1b_audited_claude_equivalent_classifies_non_workers(kanban_home):
    """S1b dry-run includes operator integration and no-evidence classes in its bounded report."""
    with kb.connect_closing() as conn:
        op_tid = kb.create_task(conn, title="operator", assignee="operator")
        null_tid = kb.create_task(conn, title="no evidence", assignee="premium")
        _insert_run_window(conn, op_tid, profile="operator", started_at=1000, ended_at=2000)
        _insert_run_window(conn, null_tid, profile="premium", started_at=1000, ended_at=2000)
        report = kb.audit_claude_cost_equivalent_backfill(conn, limit=50)
        assert report["classes"]["operator_integration"] == 1
        assert report["classes"]["null_cost_no_cost_evidence"] == 1
        assert report["updated"] == 0


def test_s1c_audited_non_claude_equivalent_dry_run_and_apply(kanban_home, tmp_path, monkeypatch):
    """S1c: stamp non-Claude rows from session evidence including cache tokens.

    Golden GPT-5.5 run: 979746 input, 26557 output, 4464640 cache-read -> $7.92776.
    Dry-run reports the candidate but does not mutate; apply writes only the stampable row.
    """
    profile_dir = tmp_path / "profiles" / "coder"
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="s1c-golden", assignee="coder")
        run_id = _insert_run_window(
            conn,
            tid,
            profile="coder",
            started_at=1000,
            ended_at=2000,
            metadata={"worker_session_id": "S-gpt"},
        )
        _write_session_rows(profile_dir / "state.db", [{
            "id": "S-gpt", "source": "cli", "started_at": 1500,
            "input_tokens": 979_746, "output_tokens": 26_557,
            "cache_read_tokens": 4_464_640, "cache_write_tokens": 0,
            "actual_cost_usd": None, "estimated_cost_usd": 0.0,
            "model": "gpt-5.5", "billing_provider": "openai-codex",
            "cwd": f"/x/kanban/workspaces/{tid}",
        }])

        dry = kb.audit_non_claude_cost_equivalent_backfill(conn, limit=50)
        assert dry["mode"] == "dry_run"
        assert dry["classes"]["stampable_with_model_and_price"] == 1
        assert dry["classes"]["null_cost_no_cost_evidence"] == 0
        assert dry["updated"] == 0
        assert json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()[0]).get("cost_usd_equivalent") is None

        applied = kb.audit_non_claude_cost_equivalent_backfill(conn, limit=50, apply=True)
        assert applied["updated"] == 1
        meta = json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()[0])
        assert meta["cost_usd_equivalent"] == pytest.approx(7.92776)
        assert meta["cost_equivalent_model"] == "gpt-5.5"
        assert meta["cost_equivalent_provider"] == "openai-codex"
        assert meta["provider_model_source"] == "session_log"
        assert meta["cost_equivalent_source"] == "s1c_audited_session_usage"
        assert meta["cost_equivalent_cache_read_tokens"] == 4_464_640


def test_s1c_audited_non_claude_equivalent_skips_no_model_claude_and_metered(
    kanban_home, tmp_path, monkeypatch
):
    """S1c: no model stays null; Claude lanes and metered OpenRouter runs are untouched."""
    profile_dir = tmp_path / "profiles" / "coder"
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir))
    with kb.connect_closing() as conn:
        no_model_tid = kb.create_task(conn, title="s1c-no-model", assignee="coder")
        claude_tid = kb.create_task(conn, title="s1c-claude", assignee="premium")
        metered_tid = kb.create_task(conn, title="s1c-openrouter", assignee="coder")
        no_model_run = _insert_run_window(
            conn,
            no_model_tid,
            profile="coder",
            started_at=1000,
            ended_at=2000,
            metadata={"worker_session_id": "S-no-model"},
        )
        claude_run = _insert_run_window(
            conn,
            claude_tid,
            profile="premium",
            started_at=1000,
            ended_at=2000,
            metadata={"worker_session_id": "S-claude", "cost_usd_equivalent": 0.953664},
        )
        metered_run = _insert_run_window(
            conn,
            metered_tid,
            profile="coder",
            started_at=1000,
            ended_at=2000,
            metadata={"worker_session_id": "S-metered"},
        )
        conn.execute("UPDATE task_runs SET cost_usd = 0.25 WHERE id = ?", (metered_run,))
        _write_session_rows(profile_dir / "state.db", [{
            "id": "S-no-model", "source": "cli", "started_at": 1500,
            "input_tokens": 979_746, "output_tokens": 26_557,
            "cache_read_tokens": 4_464_640, "cache_write_tokens": 0,
            "actual_cost_usd": None, "estimated_cost_usd": 0.0,
            "model": None, "billing_provider": "openai-codex",
            "cwd": f"/x/kanban/workspaces/{no_model_tid}",
        }, {
            "id": "S-metered", "source": "cli", "started_at": 1500,
            "input_tokens": 1_000, "output_tokens": 100,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "actual_cost_usd": 0.25, "estimated_cost_usd": 0.25,
            "model": "openrouter/paid", "billing_provider": "openrouter",
            "cwd": f"/x/kanban/workspaces/{metered_tid}",
        }])

        report = kb.audit_non_claude_cost_equivalent_backfill(conn, limit=50, apply=True)
        assert report["updated"] == 0
        assert report["classes"]["stampable_with_model_and_price"] == 0
        assert report["classes"]["null_cost_no_cost_evidence"] == 1

        no_model_meta = json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE id=?", (no_model_run,)
        ).fetchone()[0])
        claude_meta = json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE id=?", (claude_run,)
        ).fetchone()[0])
        metered_meta = json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE id=?", (metered_run,)
        ).fetchone()[0])
        assert "cost_usd_equivalent" not in no_model_meta
        assert claude_meta["cost_usd_equivalent"] == pytest.approx(0.953664)
        assert "cost_usd_equivalent" not in metered_meta


def test_s1d_non_claude_equivalent_restamps_csi_and_missing_models(
    kanban_home, tmp_path, monkeypatch
):
    """S1d: corrected prices, CSI-only lookup, missing models, and S1b guardrails."""
    profile_dir = tmp_path / "profiles" / "coder"
    state_db = profile_dir / "state.db"
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir))
    with kb.connect_closing() as conn:
        restamp_tid = kb.create_task(conn, title="s1d-restamp", assignee="coder")
        mini_tid = kb.create_task(conn, title="s1d-restamp-mini", assignee="coder")
        csi_tid = kb.create_task(conn, title="s1d-csi", assignee="coder")
        missing_tid = kb.create_task(conn, title="s1d-missing", assignee="coder")
        s1b_tid = kb.create_task(conn, title="s1d-s1b-skip", assignee="coder")

        restamp_run = _insert_run_window(
            conn,
            restamp_tid,
            profile="coder",
            started_at=1000,
            ended_at=2000,
            metadata={
                "cost_usd_equivalent": 6.011145,
                "cost_equivalent_model": "gpt-5.4",
                "cost_equivalent_provider": "openai-codex",
                "cost_equivalent_input_tokens": 780_435,
                "cost_equivalent_output_tokens": 70_299,
                "cost_equivalent_cache_read_tokens": 0,
                "cost_equivalent_cache_write_tokens": 0,
            },
        )
        mini_run = _insert_run_window(
            conn,
            mini_tid,
            profile="coder",
            started_at=1000,
            ended_at=2000,
            metadata={
                "cost_usd_equivalent": 0.008,
                "cost_equivalent_model": "gpt-5.4-mini",
                "cost_equivalent_provider": "openai-codex",
                "cost_equivalent_input_tokens": 1000,
                "cost_equivalent_output_tokens": 100,
                "cost_equivalent_cache_read_tokens": 0,
                "cost_equivalent_cache_write_tokens": 0,
            },
        )
        csi_run = _insert_run_window(
            conn,
            csi_tid,
            profile="coder",
            started_at=1000,
            ended_at=2000,
            metadata={"cost_session_ids": [f"{state_db}::S-csi"]},
        )
        missing_run = _insert_run_window(
            conn,
            missing_tid,
            profile="coder",
            started_at=1000,
            ended_at=2000,
            metadata={"worker_session_id": "S-kimi"},
        )
        s1b_run = _insert_run_window(
            conn,
            s1b_tid,
            profile="coder",
            started_at=1000,
            ended_at=2000,
            metadata={
                "cost_usd_equivalent": 99.0,
                "cost_equivalent_model": "gpt-5.4",
                "cost_equivalent_source": "s1b_audited_session_usage",
            },
        )
        _write_session_rows(state_db, [
            {"id": "S-csi", "source": "cli", "started_at": 1500,
             "input_tokens": 1000, "output_tokens": 100,
             "cache_read_tokens": 0, "cache_write_tokens": 0,
             "actual_cost_usd": None, "estimated_cost_usd": 0.0,
             "model": "glm-5.2", "billing_provider": "zai",
             "cwd": f"/x/kanban/workspaces/{csi_tid}"},
            {"id": "S-kimi", "source": "cli", "started_at": 1500,
             "input_tokens": 1000, "output_tokens": 100,
             "cache_read_tokens": 0, "cache_write_tokens": 0,
             "actual_cost_usd": None, "estimated_cost_usd": 0.0,
             "model": "kimi-for-coding", "billing_provider": "moonshot",
             "cwd": f"/x/kanban/workspaces/{missing_tid}"},
        ])

        dry = kb.audit_non_claude_cost_equivalent_backfill(conn, limit=50)
        assert dry["updated"] == 0
        assert dry["classes"]["restamp_price_correction"] == 2
        assert dry["classes"]["new_stamp_csi"] == 1
        assert dry["classes"]["new_stamp_missing_model"] == 1

        applied = kb.audit_non_claude_cost_equivalent_backfill(conn, limit=50, apply=True)
        assert applied["updated"] == 4

        rows = conn.execute(
            "SELECT id, metadata FROM task_runs WHERE id IN (?, ?, ?, ?, ?)",
            (restamp_run, mini_run, csi_run, missing_run, s1b_run),
        ).fetchall()
        by_id = {row["id"]: json.loads(row["metadata"]) for row in rows}
        assert by_id[restamp_run]["cost_usd_equivalent"] == pytest.approx(2.6540775)
        assert by_id[restamp_run]["cost_usd_equivalent_s1c_pre_s1d"] == pytest.approx(6.011145)
        assert by_id[restamp_run]["provider_model_source"] == "unknown"
        assert by_id[mini_run]["cost_usd_equivalent"] == pytest.approx(0.0012)
        assert by_id[mini_run]["cost_usd_equivalent_s1c_pre_s1d"] == pytest.approx(0.008)
        assert by_id[csi_run]["cost_usd_equivalent"] == pytest.approx(0.000098)
        assert by_id[csi_run]["provider_model_source"] == "session_log"
        assert by_id[missing_run]["cost_usd_equivalent"] == pytest.approx(0.000829)
        assert by_id[missing_run]["provider_model_source"] == "session_log"
        assert by_id[s1b_run]["cost_usd_equivalent"] == pytest.approx(99.0)
        assert "cost_usd_equivalent_s1c_pre_s1d" not in by_id[s1b_run]


def test_repair_frozen_equivalent_stamps_codex_lane_tokens(kanban_home, monkeypatch):
    """Opt-in repair: old subscription runs frozen at cost_usd=0.0 with
    tokens but no worker_session_id can still get a bounded API-equivalent
    from the active lane preset. The metered cost remains zero."""
    monkeypatch.setattr(
        kb, "_lookup_model_price_per_mtok",
        lambda provider, model: (5.0, 30.0, 0.5, 6.25)
        if (provider, model) == ("openai", "gpt-5.5") else None,
    )
    with kb.connect_closing() as conn:
        lane = kb.create_lane(
            conn,
            name="codex-subscription",
            profiles={"coder": {
                "worker_runtime": "hermes",
                "provider": "openai",
                "model": "gpt-5.5",
            }},
        )
        kb.activate_lane(conn, lane["id"])
        tid = kb.create_task(conn, title="old-codex", assignee="coder")
        with kb.write_txn(conn):
            cur = conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, started_at, ended_at, outcome, "
                "input_tokens, output_tokens, cost_usd, metadata) "
                "VALUES (?, 'coder', 'done', 1000, 1010, 'completed', "
                "1000, 100, 0.0, ?)",
                (tid, json.dumps({"note": "frozen-subscription"})),
            )
            run_id = cur.lastrowid

        assert kb.repair_cost_equivalent_for_frozen_runs(conn, limit=50) == 1
        row = conn.execute(
            "SELECT input_tokens, output_tokens, cost_usd, metadata "
            "FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert row["cost_usd"] == pytest.approx(0.0)
        assert meta["note"] == "frozen-subscription"
        assert meta["cost_usd_equivalent"] == pytest.approx(0.008)
        assert meta["cost_equivalent_model"] == "gpt-5.5"
        assert meta["cost_equivalent_provider"] == "openai"
        assert meta["billing_mode"] == "subscription_included"

        assert kb.repair_cost_equivalent_for_frozen_runs(conn, limit=50) == 0


def test_claim_stamps_billing_identity_for_metered_lane(kanban_home, monkeypatch):
    monkeypatch.setattr(
        kb,
        "_active_lane_entry_for_profile_from_conn",
        lambda conn, profile: {
            "worker_runtime": "hermes",
            "provider": "openrouter",
            "model": "openai/gpt-5-mini",
        },
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="metered-claim", assignee="verifier")
        assert kb.claim_task(conn, tid)
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ?",
            (tid,),
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert meta["worker_runtime"] == "hermes"
        assert meta["provider"] == "openrouter"
        assert meta["model"] == "openai/gpt-5-mini"
        assert meta["billing_mode"] == "metered"
        assert meta["cost_source"] == "dispatch_metered_stamp"


def test_repair_frozen_equivalent_uses_stamped_provider_model_after_lane_flip(
    kanban_home, monkeypatch,
):
    seen = []

    def fake_equivalent(provider, model, in_tok, out_tok, *, cache=None):
        seen.append((provider, model))
        if (provider, model) == ("openrouter", "openai/gpt-5-mini"):
            return 0.0012
        return None

    monkeypatch.setattr(kb, "_equiv_from_tokens", fake_equivalent)
    with kb.connect_closing() as conn:
        lane = kb.create_lane(
            conn,
            name="flipped-live-lane",
            profiles={"verifier": {
                "worker_runtime": "hermes",
                "provider": "anthropic",
                "model": "claude-opus-live-now",
            }},
        )
        kb.activate_lane(conn, lane["id"])
        tid = kb.create_task(conn, title="stamped-history", assignee="verifier")
        with kb.write_txn(conn):
            cur = conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, started_at, ended_at, outcome, "
                "input_tokens, output_tokens, cost_usd, metadata) "
                "VALUES (?, 'verifier', 'done', 1000, 1010, 'completed', "
                "1000, 100, 0.0, ?)",
                (tid, json.dumps({
                    "provider": "openrouter",
                    "model": "openai/gpt-5-mini",
                    "worker_runtime": "hermes",
                    "billing_mode": "metered",
                })),
            )
            run_id = cur.lastrowid

        assert kb.repair_cost_equivalent_for_frozen_runs(conn, limit=50) == 1
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert seen == [("openrouter", "openai/gpt-5-mini")]
        assert meta["cost_equivalent_provider"] == "openrouter"
        assert meta["cost_equivalent_model"] == "openai/gpt-5-mini"


def test_repair_frozen_equivalent_skips_metered_claude_and_prestamped(
    kanban_home, monkeypatch,
):
    monkeypatch.setattr(
        kb, "_lookup_model_price_per_mtok",
        lambda provider, model: (5.0, 30.0, 0.5, 6.25)
        if (provider, model) == ("openai", "gpt-5.5") else None,
    )
    with kb.connect_closing() as conn:
        lane = kb.create_lane(
            conn,
            name="mixed",
            profiles={
                "coder": {
                    "worker_runtime": "hermes",
                    "provider": "openai",
                    "model": "gpt-5.5",
                },
                "coder-claude": {
                    "worker_runtime": "claude-cli",
                    "model": "claude-fable-5",
                },
            },
        )
        kb.activate_lane(conn, lane["id"])
        metered = kb.create_task(conn, title="metered", assignee="coder")
        claude = kb.create_task(conn, title="claude", assignee="coder-claude")
        prestamped = kb.create_task(conn, title="prestamped", assignee="coder")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, started_at, ended_at, outcome, "
                "input_tokens, output_tokens, cost_usd, metadata) "
                "VALUES (?, 'coder', 'done', 1000, 1010, 'completed', "
                "1000, 100, 0.25, NULL)",
                (metered,),
            )
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, started_at, ended_at, outcome, "
                "input_tokens, output_tokens, cost_usd, metadata) "
                "VALUES (?, 'coder-claude', 'done', 1000, 1010, 'completed', "
                "1000, 100, 0.0, NULL)",
                (claude,),
            )
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, started_at, ended_at, outcome, "
                "input_tokens, output_tokens, cost_usd, metadata) "
                "VALUES (?, 'coder', 'done', 1000, 1010, 'completed', "
                "1000, 100, 0.0, ?)",
                (prestamped, json.dumps({"cost_usd_equivalent": 123.0})),
            )

        assert kb.repair_cost_equivalent_for_frozen_runs(conn, limit=50) == 0
        rows = conn.execute(
            "SELECT task_id, cost_usd, metadata FROM task_runs "
            "ORDER BY task_id"
        ).fetchall()
        by_task = {row["task_id"]: row for row in rows}
        assert by_task[metered]["cost_usd"] == pytest.approx(0.25)
        assert by_task[metered]["metadata"] is None
        assert by_task[claude]["cost_usd"] == pytest.approx(0.0)
        assert by_task[claude]["metadata"] is None
        assert json.loads(by_task[prestamped]["metadata"])["cost_usd_equivalent"] == 123.0


def test_s1_codex_included_no_price_leaves_equivalent_unset(kanban_home, tmp_path, monkeypatch):
    """COST-VISIBILITY-WORKERS-S2 guardrail: when no price is resolvable the
    fallback returns None and NO cost_usd_equivalent is invented — honesty over
    coverage. cost_usd still stamped $0 (subscription) so the run is no longer a
    NULL candidate."""
    profile_dir = tmp_path / "profiles" / "coder"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription",
                        lambda p: "chatgpt" if p == "coder" else None)
    monkeypatch.setattr(kb, "_lookup_model_price_per_mtok",
                        lambda provider, model: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="codex-noprice", assignee="coder")
        run_id = _insert_run_window(
            conn, tid, profile="coder", started_at=1000, ended_at=2000)
        _write_session_rows(profile_dir / "state.db", [
            {"id": "S-np", "source": "cli", "started_at": 1500,
             "input_tokens": 5000, "output_tokens": 500,
             "estimated_cost_usd": 0.0, "model": "mystery-model",
             "billing_provider": "internal",
             "cwd": f"/x/kanban/workspaces/{tid}"},
        ])
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 1
        meta = json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()[0])
        assert "cost_usd_equivalent" not in meta
        assert meta["model"] == "mystery-model"


def test_s1_metered_match_keeps_cost_and_stamps_model(kanban_home, tmp_path, monkeypatch):
    """COST-VISIBILITY-WORKERS-S2: a metered (non-subscription) lane is
    unchanged — actual_cost_usd lands in cost_usd — but the session's model is
    now also stamped, and no cost_usd_equivalent is invented (the real metered
    cost already IS the effective cost)."""
    profile_dir = tmp_path / "profiles" / "research"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda p: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="metered", assignee="research")
        run_id = _insert_run_window(
            conn,
            tid,
            profile="research",
            started_at=1000,
            ended_at=2000,
            metadata={"provider": "dispatch-provider", "model": "dispatch-model"},
        )
        _write_session_rows(profile_dir / "state.db", [
            {"id": "S-met", "source": "cli", "started_at": 1500,
             "input_tokens": 400, "output_tokens": 30, "actual_cost_usd": 0.15,
             "estimated_cost_usd": 0.15, "model": "gpt-5.5",
             "billing_provider": "openrouter",
             "cwd": f"/x/kanban/workspaces/{tid}"},
        ])
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 1
        row = conn.execute(
            "SELECT cost_usd, metadata FROM task_runs WHERE id=?",
            (run_id,)).fetchone()
        assert row["cost_usd"] == pytest.approx(0.15)
        meta = json.loads(row["metadata"])
        assert meta["model"] == "gpt-5.5"
        assert meta["provider"] == "openrouter"
        assert "cost_usd_equivalent" not in meta


def test_s1_api_billed_lane_without_session_stays_null(kanban_home, tmp_path, monkeypatch):
    """S1 guardrail: an API-billed lane (no subscription) with a real-duration
    run and no recoverable session is NEVER fabricated to $0 — cost stays NULL.
    """
    profile_dir = tmp_path / "profiles" / "research"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda p: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="api-lane", assignee="research")
        run_id = _insert_run_window(
            conn, tid, profile="research", started_at=1000, ended_at=2000)
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 0
        assert conn.execute(
            "SELECT cost_usd FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] is None


def test_s1_unlinkable_non_subscription_run_stays_null(kanban_home, tmp_path, monkeypatch):
    """S1 guardrail: a run with no recoverable session and no provable
    subscription lane (incl. an instantaneous, profile-less run) is LEFT NULL —
    never invented to $0. Honesty over coverage."""
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(tmp_path / "profiles" / str(name)))
    monkeypatch.setattr(kb, "_profile_subscription", lambda p: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="never-ran", assignee="x")
        run_id = _insert_run_window(
            conn, tid, profile=None, started_at=5000, ended_at=5000)
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 0
        assert conn.execute(
            "SELECT cost_usd FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] is None


def test_s1_does_not_double_count_across_calls(kanban_home, tmp_path, monkeypatch):
    """S1 AC-2: a session already attributed to one run (recorded in
    cost_session_ids) is NEVER re-counted onto a later candidate run, even
    across separate backfill calls."""
    profile_dir = tmp_path / "profiles" / "coder"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda p: None)
    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="first", assignee="coder")
        r1 = _insert_run_window(conn, t1, profile="coder", started_at=1000, ended_at=2000)
        _write_session_rows(profile_dir / "state.db", [
            {"id": "S-shared", "source": "cli", "started_at": 1500,
             "input_tokens": 300, "output_tokens": 30, "actual_cost_usd": 0.09},
        ])
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 1
        # A second task whose window ALSO contains S-shared appears later.
        t2 = kb.create_task(conn, title="second", assignee="coder")
        r2 = _insert_run_window(conn, t2, profile="coder", started_at=1400, ended_at=1600)
        # S-shared is already consumed by r1 → r2 must NOT re-claim it.
        kb.backfill_run_costs_from_sessions(conn, limit=50)
        total = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM task_runs").fetchone()[0]
        assert total == pytest.approx(0.09)  # counted once, not 0.18
        r2_cost = conn.execute(
            "SELECT cost_usd FROM task_runs WHERE id=?", (r2,)).fetchone()[0]
        assert r2_cost in (None, pytest.approx(0.0))


def test_s1_since_seconds_bounds_scan(kanban_home, tmp_path, monkeypatch):
    """S1: since_seconds restricts the scan to recently-ended runs (the
    heartbeat path) — an old run outside the window is not even considered."""
    profile_dir = tmp_path / "profiles" / "coder-claude"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription",
                        lambda p: "claude" if p == "coder-claude" else None)
    now = int(time.time())
    with kb.connect_closing() as conn:
        t_old = kb.create_task(conn, title="old", assignee="coder-claude")
        t_new = kb.create_task(conn, title="new", assignee="coder-claude")
        _insert_run_window(conn, t_old, profile="coder-claude",
                           started_at=now - 100_000, ended_at=now - 99_000)
        r_new = _insert_run_window(conn, t_new, profile="coder-claude",
                                   started_at=now - 100, ended_at=now - 50)
        # only the recent run is in the 1h window → exactly one stamp
        assert kb.backfill_run_costs_from_sessions(conn, limit=50, since_seconds=3600) == 1
        assert conn.execute(
            "SELECT cost_usd FROM task_runs WHERE id=?", (r_new,)).fetchone()[0] == pytest.approx(0.0)


def test_s1_fail_soft_missing_profile_db(kanban_home, monkeypatch):
    """S1: a profile whose state.db can't be resolved never raises — the run
    falls through to the subscription/no-execution tiers or stays NULL."""
    def _raise(name):
        raise FileNotFoundError(name)
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", _raise)
    monkeypatch.setattr(kb, "_profile_subscription", lambda p: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="ghost", assignee="ghost")
        _insert_run_window(conn, tid, profile="ghost", started_at=1000, ended_at=2000)
        # No raise; nothing to stamp (real duration, no sub, no session).
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 0


def test_s1_reduces_tasks_without_cost_data_metric(kanban_home, tmp_path, monkeypatch):
    """S1 + COST-METRIC-INTEGRITY end-to-end: the backfill drives the vision
    metric ``tasks_without_cost_data`` down ONLY for tasks that gained a real
    metered cost. A subscription-``$0`` stamp is *no metered cost*, so it stays
    inside the coverage counter (surfaced as ``subscription_only``) and
    ``cost_usd_total`` rises only by real, once-counted session cost."""
    from hermes_cli import vision_metrics as vm
    profile_dir = tmp_path / "profiles" / "coder"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription",
                        lambda p: "claude" if p == "coder-claude" else None)
    with kb.connect_closing() as conn:
        # three done tasks, all without cost: one cwd-linked, one subscription,
        # one API-billed-no-session (stays blind).
        t_cwd = kb.create_task(conn, title="cwd", assignee="coder")
        t_sub = kb.create_task(conn, title="sub", assignee="coder-claude")
        t_api = kb.create_task(conn, title="api", assignee="research")
        for t, prof in ((t_cwd, "coder"), (t_sub, "coder-claude"), (t_api, "research")):
            _insert_run_window(conn, t, profile=prof, started_at=1000, ended_at=2000)
            conn.execute("UPDATE tasks SET status='done', completed_at=1500 WHERE id=?", (t,))
        conn.commit()
        _write_session_rows(profile_dir / "state.db", [
            {"id": "S-cwd", "source": "cli", "started_at": 1500,
             "input_tokens": 1000, "output_tokens": 50, "actual_cost_usd": 0.30,
             "cwd": f"/x/kanban/workspaces/{t_cwd}"},
        ])
        before = vm._cost_per_task_metric(conn, now=1600, window_days=7)
        assert before["counter"]["value"] == 3  # all blind

        kb.backfill_run_costs_from_sessions(conn, limit=50)

        after = vm._cost_per_task_metric(conn, now=1600, window_days=7)
        # Only the cwd task gained a *real metered* cost and leaves the blind
        # spot. The subscription task was stamped $0 (no metered cost) and the
        # API-billed task stays NULL — both remain in the counter (honest
        # coverage, not a phantom drop from hiding subscription tasks).
        assert after["counter"]["value"] == 2
        assert after["counter"]["value"] < before["counter"]["value"]
        assert after["tasks_with_cost"] == 1  # cwd only
        assert after["coverage"]["subscription_only"] == 1  # t_sub, still blind
        assert after["coverage"]["no_cost_data"] == 1  # t_api, still blind
        # cost_usd_total rose only by the one real $0.30 session.
        assert after["cost_usd_total"] == pytest.approx(0.30)


def test_connect_honors_kanban_busy_timeout_env(kanban_home, monkeypatch):
    """All kanban connections should use the explicit busy-timeout knob.

    A worker stampede should wait for SQLite's writer lock instead of failing
    immediately with ``database is locked`` during first-connect/WAL/schema
    setup.  The timeout must be queryable via PRAGMA so CLI, gateway, and tool
    connections behave the same way.
    """
    monkeypatch.setenv("HERMES_KANBAN_BUSY_TIMEOUT_MS", "123456")

    with kb.connect_closing() as conn:
        row = conn.execute("PRAGMA busy_timeout").fetchone()

    assert row[0] == 123456


def test_cross_process_init_lock_uses_windows_byte_range_lock(tmp_path, monkeypatch):
    """Windows must use a real (non-blocking) process lock, not a no-op open.

    The init lock acquires with LK_NBLCK in a bounded retry loop (#36644) so a
    wedged holder can never block connect() forever; a clean acquire takes the
    lock once and releases it once.
    """
    calls: list[tuple[int, int, int]] = []
    fake_msvcrt = types.SimpleNamespace(
        LK_NBLCK=3,
        LK_UNLCK=2,
        locking=lambda fd, mode, nbytes: calls.append((fd, mode, nbytes)),
    )
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    db_path = tmp_path / "kanban.db"
    with kb._cross_process_init_lock(db_path):
        # Acquired exactly once via the non-blocking byte-range lock.
        assert [call[1:] for call in calls] == [(fake_msvcrt.LK_NBLCK, 1)]

    # Released once on exit.
    assert [call[1:] for call in calls] == [
        (fake_msvcrt.LK_NBLCK, 1),
        (fake_msvcrt.LK_UNLCK, 1),
    ]


def test_connect_rejects_tls_record_in_sqlite_header(tmp_path, monkeypatch):
    """Kanban should classify TLS-looking page-0 clobbers before WAL setup."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    corrupt = home / "kanban.db"
    corrupt.write_bytes(b"SQLit" + bytes.fromhex("17 03 03 00 13") + b"x" * 32)

    with pytest.raises(sqlite3.DatabaseError) as exc_info:
        kb.connect(board="default")

    msg = str(exc_info.value)
    assert "file is not a database" in msg
    assert "TLS record header detected at byte offset 5" in msg
    assert "53 51 4c 69 74 17 03 03 00 13" in msg


def test_connect_migrates_legacy_db_before_optional_column_indexes(tmp_path):
    """Legacy DBs missing additive indexed columns must migrate cleanly.

    SCHEMA_SQL runs in ``connect()`` before ``_migrate_add_optional_columns``.
    Indexes over additive columns therefore must be created after the
    migration adds those columns, or boards predating the column fail to
    open before migration can run.

    Covers all four indexes that sit on additive columns:
    - ``tasks.session_id``       -> ``idx_tasks_session_id``    (#28447)
    - ``tasks.tenant``           -> ``idx_tasks_tenant``        (#16081)
    - ``tasks.idempotency_key``  -> ``idx_tasks_idempotency``   (#17805)
    - ``task_events.run_id``     -> ``idx_events_run``          (#17805)
    """
    db_path = tmp_path / "legacy-kanban.db"
    conn = sqlite3.connect(str(db_path))
    # Pre-#16081 ``tasks`` shape: missing tenant, idempotency_key, session_id.
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
    """)
    # Pre-#17805 ``task_events`` shape: missing run_id. Required because
    # ``_migrate_add_optional_columns`` unconditionally runs PRAGMA on
    # ``task_events`` for run_id back-fill.
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
        "VALUES ('legacy', 'old board task', 'ready', 1)"
    )
    conn.commit()
    conn.close()

    with kb.connect(db_path) as migrated:
        task_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(tasks)")
        }
        event_columns = {
            row["name"]
            for row in migrated.execute("PRAGMA table_info(task_events)")
        }
        indexes = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    # Additive columns added by migration:
    assert "session_id" in task_columns
    assert "tenant" in task_columns
    assert "idempotency_key" in task_columns
    assert "run_id" in event_columns
    # And their indexes — the regression scope of this test:
    assert "idx_tasks_session_id" in indexes
    assert "idx_tasks_tenant" in indexes
    assert "idx_tasks_idempotency" in indexes
    assert "idx_events_run" in indexes


# ---------------------------------------------------------------------------
# Task creation + status inference
# ---------------------------------------------------------------------------

def test_create_task_no_parents_is_ready(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="ship it", assignee="alice")
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.status == "ready"
    assert t.assignee == "alice"
    assert t.workspace_kind == "scratch"


def test_create_task_with_parent_is_todo_until_parent_done(kanban_home):
    with kb.connect_closing() as conn:
        p = kb.create_task(conn, title="parent")
        c = kb.create_task(conn, title="child", parents=[p])
        assert kb.get_task(conn, c).status == "todo"
        kb.complete_task(conn, p, result="ok")
        assert kb.get_task(conn, c).status == "ready"


def test_create_task_unknown_parent_errors(kanban_home):
    with kb.connect_closing() as conn, pytest.raises(ValueError, match="unknown parent"):
        kb.create_task(conn, title="orphan", parents=["t_ghost"])


def test_workspace_kind_validation(kanban_home):
    with kb.connect_closing() as conn, pytest.raises(ValueError, match="workspace_kind"):
        kb.create_task(conn, title="bad ws", workspace_kind="cloud")


def test_create_task_persists_worktree_branch_name(kanban_home, tmp_path):
    target = tmp_path / ".worktrees" / "t6-wire"
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="ship worktree",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name=" wt/t6-wire ",
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        context = kb.build_worker_context(conn, tid)

    assert task.branch_name == "wt/t6-wire"
    assert events[0].payload["branch_name"] == "wt/t6-wire"
    assert "Branch:   wt/t6-wire" in context


def test_branch_name_requires_worktree_workspace(kanban_home):
    with kb.connect_closing() as conn, pytest.raises(ValueError, match="worktree"):
        kb.create_task(
            conn,
            title="bad branch",
            workspace_kind="scratch",
            branch_name="wt/bad",
        )


def test_build_worker_context_includes_knowledge_pointers(kanban_home):
    """build_worker_context must include the static knowledge-pointer section
    so workers know where to look for model-landscape and canonical facts."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="test knowledge pointers")
        ctx = kb.build_worker_context(conn, tid)

    assert "Knowledge pointers" in ctx
    assert "/home/piet/llm-wiki/wiki/models/model-landscape.md" in ctx
    assert "/home/piet/vault/00-Canon/" in ctx


def test_build_worker_context_uses_shared_knowledge_pointer_renderer(
    kanban_home, monkeypatch
):
    """The native context consumes the shared renderer instead of duplicated
    literal pointer strings, keeping worker-runtime prompts in parity."""
    monkeypatch.setattr(
        kb,
        "_render_knowledge_pointers",
        lambda: ["## Knowledge pointers", "- shared-renderer-sentinel", ""],
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="test shared knowledge renderer")
        ctx = kb.build_worker_context(conn, tid)

    assert "shared-renderer-sentinel" in ctx


# ---------------------------------------------------------------------------
# Links + dependency resolution
# ---------------------------------------------------------------------------

def test_link_demotes_ready_child_to_todo_when_parent_not_done(kanban_home):
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
        assert kb.get_task(conn, b).status == "ready"
        kb.link_tasks(conn, a, b)
        assert kb.get_task(conn, b).status == "todo"


def test_link_keeps_ready_child_when_parent_already_done(kanban_home):
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="a")
        kb.complete_task(conn, a)
        b = kb.create_task(conn, title="b")
        assert kb.get_task(conn, b).status == "ready"
        kb.link_tasks(conn, a, b)
        assert kb.get_task(conn, b).status == "ready"


def test_link_rejects_self_loop(kanban_home):
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="a")
        with pytest.raises(ValueError, match="itself"):
            kb.link_tasks(conn, a, a)


def test_link_detects_cycle(kanban_home):
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b", parents=[a])
        c = kb.create_task(conn, title="c", parents=[b])
        with pytest.raises(ValueError, match="cycle"):
            kb.link_tasks(conn, c, a)
        with pytest.raises(ValueError, match="cycle"):
            kb.link_tasks(conn, b, a)


def test_recompute_ready_cascades_through_chain(kanban_home):
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b", parents=[a])
        c = kb.create_task(conn, title="c", parents=[b])
        assert [kb.get_task(conn, x).status for x in (a, b, c)] == \
               ["ready", "todo", "todo"]
        kb.complete_task(conn, a)
        assert kb.get_task(conn, b).status == "ready"
        kb.complete_task(conn, b)
        assert kb.get_task(conn, c).status == "ready"


def test_recompute_ready_promotes_blocked_with_done_parents(kanban_home):
    """blocked tasks with all parents done should be promoted to ready,
    unless the circuit-breaker failure limit has been reached."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        # Complete the parent
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        # Manually block the child with zero failures (simulates a
        # dependency block, not a circuit-breaker block).
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=0, "
            "last_failure_error=NULL WHERE id=?",
            (child,),
        )
        conn.commit()
        assert kb.get_task(conn, child).status == "blocked"
        # recompute_ready should promote blocked → ready
        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        task = kb.get_task(conn, child)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


def test_recompute_ready_fan_in_waits_for_all_parents(kanban_home):
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
        c = kb.create_task(conn, title="c", parents=[a, b])
        kb.complete_task(conn, a)
        assert kb.get_task(conn, c).status == "todo"
        kb.complete_task(conn, b)
        assert kb.get_task(conn, c).status == "ready"


def test_archived_parent_does_not_satisfy_dependency(kanban_home):
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )

        kb.archive_task(conn, parent)
        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, child)
        assert task is not None
        assert task.status == "todo"

        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        conn.commit()
        assert kb.claim_task(conn, child, claimer="host:1") is None
        task = kb.get_task(conn, child)
        assert task is not None
        assert task.status == "todo"


# ---------------------------------------------------------------------------
# Atomic claim (CAS)
# ---------------------------------------------------------------------------

def test_claim_once_wins_second_loses(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        first = kb.claim_task(conn, t, claimer="host:1")
        assert first is not None and first.status == "running"
        second = kb.claim_task(conn, t, claimer="host:2")
        assert second is None


def test_claim_uses_env_default_ttl(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_TTL_SECONDS", "3600")
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t, claimer="host:1")
        expires = kb.get_task(conn, t).claim_expires
    assert expires is not None
    assert expires > int(time.time()) + 3000


def test_claim_fails_on_non_ready(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        # Move to todo by introducing an unsatisfied parent.
        p = kb.create_task(conn, title="p")
        kb.link_tasks(conn, p, t)
        assert kb.get_task(conn, t).status == "todo"
        assert kb.claim_task(conn, t) is None


def test_schedule_task_parks_time_delay_without_dispatching(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="delayed recheck", assignee="ops")
        assert kb.schedule_task(conn, t, reason="run next week") is True
        task = kb.get_task(conn, t)
        assert task.status == "scheduled"
        assert kb.claim_task(conn, t) is None

        events = kb.list_events(conn, t)
        assert any(e.kind == "scheduled" and e.payload == {"reason": "run next week"} for e in events)


def test_unblock_scheduled_rechecks_parent_gate(kanban_home):
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"
        assert kb.schedule_task(conn, child, reason="wait until tomorrow") is True

        assert kb.unblock_task(conn, child) is True
        assert kb.get_task(conn, child).status == "todo"

        kb.complete_task(conn, parent)
        assert kb.schedule_task(conn, child, reason="second timer") is True
        assert kb.unblock_task(conn, child) is True
        assert kb.get_task(conn, child).status == "ready"


def test_stale_claim_reclaimed(kanban_home, monkeypatch):
    import signal
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        killed: list[int] = []

        def _signal(_pid, sig):
            killed.append(sig)

        kb._set_worker_pid(conn, t, 12345)
        # Rewind claim_expires so it looks stale.
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 3600, t),
        )
        # Worker PID has died — exactly the case ``release_stale_claims``
        # should still reclaim (post-#23025: live PIDs are now extended).
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        reclaimed = kb.release_stale_claims(conn, signal_fn=_signal)
        assert reclaimed == 1
        assert kb.get_task(conn, t).status == "ready"
        assert killed == [signal.SIGTERM]


def test_stale_claim_with_live_pid_extends_instead_of_reclaiming(
    kanban_home, monkeypatch,
):
    """A stale-by-TTL claim whose worker PID is still alive should be
    extended, not reclaimed (#23025). Slow models can spend longer than
    ``DEFAULT_CLAIM_TTL_SECONDS`` inside a single tool-free LLM call;
    killing those healthy workers produces a respawn loop with zero
    progress."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)

        old_expires = int(time.time()) - 60
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (old_expires, t),
        )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        killed: list[int] = []
        reclaimed = kb.release_stale_claims(
            conn, signal_fn=lambda _p, sig: killed.append(sig),
        )
        assert reclaimed == 0
        task = kb.get_task(conn, t)
        assert task.status == "running"
        assert task.claim_expires is not None
        assert task.claim_expires > old_expires
        assert killed == []  # live worker not killed

        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (t,),
            ).fetchall()
        ]
        assert "claim_extended" in kinds
        assert "reclaimed" not in kinds


def test_stale_claim_with_live_pid_uses_env_ttl_override(
    kanban_home, monkeypatch,
):
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_CLAIM_TTL_SECONDS", "3600")

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 60, t),
        )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        assert reclaimed == 0

        task = kb.get_task(conn, t)
        assert task is not None
        assert task.claim_expires is not None
        assert task.claim_expires > int(time.time()) + 3000


def test_stale_claim_deferred_when_live_worker_survives_termination(
    kanban_home, monkeypatch,
):
    """A TTL-expired claim whose worker survives the kill must NOT be released.

    Releasing would let the dispatcher spawn a duplicate beside the still-alive
    worker — the runaway seen when a cgroup memory.high throttle parks a worker
    in uninterruptible (D) state, where a pending SIGKILL cannot land. The claim
    is held (extended) and retried next tick instead.
    """
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)

        old_expires = int(time.time()) - 60
        # Heartbeat stale by > 1h so the live-pid EXTEND branch is skipped and
        # the terminate path (the wedged-worker case) runs.
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (old_expires, int(time.time()) - 7200, t),
        )
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(
            _kb, "_terminate_reclaimed_worker",
            lambda *a, **k: {
                "termination_attempted": True,
                "host_local": True,
                "terminated": False,
            },
        )
        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        assert reclaimed == 0

        assert kb.get_task(conn, t).status == "running"
        worker_pid = conn.execute(
            "SELECT worker_pid FROM tasks WHERE id = ?", (t,),
        ).fetchone()[0]
        assert worker_pid == 12345  # worker not orphaned
        claim_expires = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (t,),
        ).fetchone()[0]
        assert claim_expires > old_expires  # claim held, not released

        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (t,),
            ).fetchall()
        ]
        assert "reclaim_deferred" in kinds
        assert "reclaimed" not in kinds


def test_stale_claim_reclaimed_when_termination_succeeds(
    kanban_home, monkeypatch,
):
    """When the worker is actually killed, the claim is released as before."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (int(time.time()) - 60, int(time.time()) - 7200, t),
        )
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(
            _kb, "_terminate_reclaimed_worker",
            lambda *a, **k: {
                "termination_attempted": True,
                "host_local": True,
                "terminated": True,
            },
        )
        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        assert reclaimed == 1
        assert kb.get_task(conn, t).status == "ready"


def test_stale_claim_released_when_worker_not_host_local(
    kanban_home, monkeypatch,
):
    """The defer guard only holds OUR own surviving workers.

    A claim we cannot manage (different host, or no kill attempted) must still
    be released, otherwise a foreign-host claim could strand a task forever.
    """
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (int(time.time()) - 60, int(time.time()) - 7200, t),
        )
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(
            _kb, "_terminate_reclaimed_worker",
            lambda *a, **k: {
                "termination_attempted": False,
                "host_local": False,
                "terminated": False,
            },
        )
        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        assert reclaimed == 1
        assert kb.get_task(conn, t).status == "ready"


def test_detect_stale_defers_when_live_worker_survives(kanban_home, monkeypatch):
    """detect_stale_running must also hold the claim when the worker survives."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="wedged", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ?, last_heartbeat_at = NULL "
                "WHERE id = ?",
                (five_hours_ago, t),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(
            _kb, "_terminate_reclaimed_worker",
            lambda *a, **k: {
                "termination_attempted": True,
                "host_local": True,
                "terminated": False,
            },
        )
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert stale == []
        assert kb.get_task(conn, t).status == "running"
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (t,),
            ).fetchall()
        ]
        assert "reclaim_deferred" in kinds


def test_stale_claim_reclaim_event_records_diagnostic_payload(
    kanban_home, monkeypatch,
):
    """``reclaimed`` events should carry claim_expires, last_heartbeat_at,
    and worker_pid so operators can diagnose why a claim went stale
    (#23025: previous payload only had ``stale_lock`` which gives no
    timing context)."""
    import json
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        old_expires = int(time.time()) - 3600
        hb_at = int(time.time()) - 1800
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (old_expires, hb_at, t),
        )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'reclaimed'",
            (t,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["claim_expires"] == old_expires
        assert payload["last_heartbeat_at"] == hb_at
        assert payload["worker_pid"] == 12345
        assert payload["host_local"] is True


def test_detect_crashed_workers_systemic_failure_fast_block(
    kanban_home, monkeypatch,
):
    """When many tasks crash with the same error, trip the breaker faster."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        task_ids = []
        for i in range(4):
            tid = kb.create_task(conn, title=f"task-{i}", assignee="a")
            host = _kb._claimer_id().split(":", 1)[0]
            conn.execute(
                "UPDATE tasks SET status='running', worker_pid=?, "
                "claim_lock=? WHERE id=?",
                (90000 + i, f"{host}:w{i}", tid),
            )
            task_ids.append(tid)
        conn.commit()

        crashed = kb.detect_crashed_workers(conn)
        assert len(crashed) == 4

        for tid in task_ids:
            task = kb.get_task(conn, tid)
            assert task.status == "blocked", (
                f"task {tid} should be blocked (systemic), got {task.status}"
            )


def test_detect_crashed_workers_isolated_failure_normal_retry(
    kanban_home, monkeypatch,
):
    """Below the systemic threshold, tasks retain normal retry budget."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        task_ids = []
        for i in range(2):
            tid = kb.create_task(conn, title=f"iso-{i}", assignee="a")
            host = _kb._claimer_id().split(":", 1)[0]
            conn.execute(
                "UPDATE tasks SET status='running', worker_pid=?, "
                "claim_lock=? WHERE id=?",
                (80000 + i, f"{host}:w{i}", tid),
            )
            task_ids.append(tid)
        conn.commit()

        crashed = kb.detect_crashed_workers(conn)
        assert len(crashed) == 2

        for tid in task_ids:
            task = kb.get_task(conn, tid)
            assert task.status == "ready", (
                f"task {tid} should stay ready (isolated), got {task.status}"
            )


def test_detect_crashed_workers_skips_freshly_claimed_tasks(
    kanban_home, monkeypatch,
):
    """Grace period prevents reclaim of freshly-started tasks."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.delenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", raising=False)

    now = 1_000_000.0
    monkeypatch.setattr(_kb.time, "time", lambda: now)

    with kb.connect_closing() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="grace test", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, "
            "claim_lock=?, started_at=? WHERE id=?",
            (99999, f"{host}:w", int(now), tid),
        )
        conn.commit()

        # With time = now (just claimed), grace period should suppress reclaim.
        crashed = kb.detect_crashed_workers(conn)
        assert tid not in crashed, "should not reclaim freshly-started task"

        # With time = now + 60 (past default 30s grace), should reclaim.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 60)
        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed, "should reclaim task past grace period"


def test_detect_crashed_workers_grace_period_env_override(
    kanban_home, monkeypatch,
):
    """HERMES_KANBAN_CRASH_GRACE_SECONDS env var adjusts the window."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "5")

    now = 2_000_000.0

    with kb.connect_closing() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="env override test", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, "
            "claim_lock=?, started_at=? WHERE id=?",
            (99999, f"{host}:w", int(now), tid),
        )
        conn.commit()

        # 3s after claim: within 5s grace → no reclaim.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 3)
        assert tid not in kb.detect_crashed_workers(conn)

        # 6s after claim: past 5s grace → reclaim.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 6)
        assert tid in kb.detect_crashed_workers(conn)


def test_resolve_crash_grace_seconds_handles_bad_env(monkeypatch):
    """Bad env values fall back to DEFAULT_CRASH_GRACE_SECONDS."""
    import hermes_cli.kanban_db as _kb

    for bad_val in ("notanumber", "-5", ""):
        monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", bad_val)
        result = _kb._resolve_crash_grace_seconds()
        assert result == _kb.DEFAULT_CRASH_GRACE_SECONDS, (
            f"expected default for {bad_val!r}, got {result}"
        )


# ---------------------------------------------------------------------------
# Rate-limit requeue: a worker that bails on a provider quota wall must be
# released back to ``ready`` WITHOUT counting a failure, so a long (e.g.
# 5-hour) quota window can't trip the circuit breaker and permanently block
# the card. The respawn guard then defers it on a cooldown until quota
# returns. Regression coverage for the kanban-rate-limit-failure report.
# ---------------------------------------------------------------------------


def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code."""
    return code << 8


def test_classify_worker_exit_recognizes_rate_limit_sentinel(kanban_home):
    import hermes_cli.kanban_db as _kb

    pid = 31337
    _kb._record_worker_exit(pid, _exited_status(_kb.KANBAN_RATE_LIMIT_EXIT_CODE))
    kind, code = _kb._classify_worker_exit(pid)
    assert kind == "rate_limited"
    assert code == _kb.KANBAN_RATE_LIMIT_EXIT_CODE

    # Plain non-zero exit is still a normal crash, not rate-limited.
    _kb._record_worker_exit(pid + 1, _exited_status(1))
    assert _kb._classify_worker_exit(pid + 1) == ("nonzero_exit", 1)


def test_rate_limit_exit_requeues_without_counting_failure(
    kanban_home, monkeypatch,
):
    """A rate-limit sentinel exit releases the task to ``ready`` and leaves
    ``consecutive_failures`` untouched — the breaker must never trip on a
    transient throttle, even across many quota-wall hits."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect_closing() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="rl", assignee="a")

        # Simulate FAR more quota-wall hits than DEFAULT_FAILURE_LIMIT (2).
        # If any of these counted as a failure the task would be blocked.
        for i in range(6):
            pid = 70000 + i
            # Claim to open a real run (so detect_crashed_workers can close
            # it with a rate_limited outcome), then point the claim at this
            # host + a dead pid so the crash path acts on it.
            kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            conn.execute(
                "UPDATE tasks SET worker_pid=?, consecutive_failures=? "
                "WHERE id=?",
                (pid, 0, tid),
            )
            conn.commit()
            _kb._record_worker_exit(
                pid, _exited_status(_kb.KANBAN_RATE_LIMIT_EXIT_CODE)
            )

            crashed = kb.detect_crashed_workers(conn)
            # Rate-limited requeues are NOT crashes.
            assert tid not in crashed
            rl = getattr(_kb.detect_crashed_workers, "_last_rate_limited", [])
            assert tid in rl

            task = kb.get_task(conn, tid)
            assert task.status == "ready", (
                f"hit {i}: should requeue ready, got {task.status}"
            )
            assert task.consecutive_failures == 0, (
                f"hit {i}: rate-limit must not count a failure, "
                f"got {task.consecutive_failures}"
            )

        # Last failure error stamped so the respawn guard recognizes the
        # quota wall.
        assert task.last_failure_error and "rate-limited" in task.last_failure_error

        # A ``rate_limited`` run outcome was recorded (not ``crashed``).
        outcomes = [
            r["outcome"] for r in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id=?", (tid,),
            ).fetchall()
        ]
        assert "rate_limited" in outcomes
        assert "crashed" not in outcomes


def test_real_crash_still_counts_and_trips_breaker(kanban_home, monkeypatch):
    """Sanity: a genuine non-zero crash (not the sentinel) still increments
    the failure counter and trips the breaker — the rate-limit carve-out is
    surgical, not a blanket "never count crashes"."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="crash", assignee="a")

        for i in range(2):  # DEFAULT_FAILURE_LIMIT == 2
            pid = 60000 + i
            conn.execute(
                "UPDATE tasks SET status='running', worker_pid=?, "
                "claim_lock=? WHERE id=?",
                (pid, f"{host}:w{i}", tid),
            )
            conn.commit()
            _kb._record_worker_exit(pid, _exited_status(1))  # generic failure
            kb.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"genuine crashes should still trip the breaker, got {task.status}"
        )


def test_respawn_guard_defers_rate_limited_within_cooldown(
    kanban_home, monkeypatch,
):
    """Within the cooldown after a rate-limit requeue, the guard defers the
    respawn; after the cooldown it allows a probe — and crucially does NOT
    fall into ``blocker_auth`` (which would defer forever)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rl-guard", assignee="a")
        # Seed a rate_limited run that just ended + the stamped error.
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=? WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error=? WHERE id=?",
            ("pid 1 exited rate-limited (quota wall) — requeued", tid),
        )
        conn.commit()

        # Inside cooldown → defer with the rate-limit-specific reason.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kb.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

        # Past cooldown → allowed (None), NOT trapped by blocker_auth even
        # though last_failure_error contains "rate-limited".
        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        assert kb.check_respawn_guard(conn, tid) is None


def test_respawn_guard_rate_limit_cooldown_zero_allows_immediately(
    kanban_home, monkeypatch,
):
    """Cooldown of 0 disables the wait — task is spawnable on the next tick,
    and the stamped rate-limit text does not re-trap it via blocker_auth."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    now = 6_000_000

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rl-zero", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=? WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, last_failure_error=? WHERE id=?",
            ("pid 1 exited rate-limited (quota wall)", tid),
        )
        conn.commit()

        monkeypatch.setattr(_kb.time, "time", lambda: now + 1)
        assert kb.check_respawn_guard(conn, tid) is None


def test_park_integration_records_parked_outcome_not_completed(kanban_home):
    """C-2: a parked integration is stamped INTEGRATION_PARKED_OUTCOME, NOT
    'completed' — so it falls out of every ``outcome = 'completed'`` filter
    (recent_success guard, success-rate stats) while cost stays attributed."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="park-outcome", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb._park_integration(
            conn, tid, {"reason": "dirty worktree"}, expected_run_id=run_id,
        )
        row = conn.execute(
            "SELECT outcome, status FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()
        assert row["outcome"] == kb.INTEGRATION_PARKED_OUTCOME
        assert row["outcome"] != "completed"
        assert kb.get_task(conn, tid).status == "blocked"


def test_respawn_guard_recent_success_fires_without_unblock(kanban_home, monkeypatch):
    """Baseline (no regression): a genuine completed run inside the window with
    no operator unblock still defers as 'recent_success'."""
    import hermes_cli.kanban_db as _kb

    now = 7_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rs-baseline", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='completed', status='review', "
            "ended_at=? WHERE id=?", (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL WHERE id=?", (tid,),
        )
        conn.commit()
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kb.check_respawn_guard(conn, tid) == "recent_success"


def test_respawn_guard_unblock_clears_recent_success(kanban_home, monkeypatch):
    """C-1 (operator override): an explicit unblock AFTER a completed run beats
    the success cooldown — the deliberate "run this again" must not stall for
    the rest of the guard window."""
    import hermes_cli.kanban_db as _kb

    now = 7_100_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rs-unblock", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='completed', status='blocked', "
            "ended_at=? WHERE id=?", (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='blocked', current_run_id=NULL, "
            "claim_lock=NULL, worker_pid=NULL WHERE id=?", (tid,),
        )
        conn.commit()
        # Operator unblocks AFTER the completed run.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 50)
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "ready"
        # Still inside the guard window, but the unblock must clear it.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kb.check_respawn_guard(conn, tid) is None


def test_parked_then_unblocked_task_is_respawnable(kanban_home, monkeypatch):
    """C-1 + C-2 end-to-end: park an integration, operator unblocks → the task
    is dispatchable on the next tick (no 'recent_success' stall). The 1h-stall
    bug this guards against had both a relabel and an unblock-override fix."""
    import hermes_cli.kanban_db as _kb

    now = 7_200_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="park-respawn", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        monkeypatch.setattr(_kb.time, "time", lambda: now)
        assert kb._park_integration(
            conn, tid, {"reason": "dirty overlap"}, expected_run_id=run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"
        monkeypatch.setattr(_kb.time, "time", lambda: now + 30)
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "ready"
        monkeypatch.setattr(_kb.time, "time", lambda: now + 60)
        assert kb.check_respawn_guard(conn, tid) is None


def test_summarize_dispatch_holds_empty_and_no_holds():
    """B: nothing held → (0, {}, None), for both an empty list and a result
    that spawned nothing but recorded no advisory holds (the genuine stuck
    signal: total_held == 0)."""
    assert kb.summarize_dispatch_holds([]) == (0, {}, None)
    assert kb.summarize_dispatch_holds([kb.DispatchResult()]) == (0, {}, None)


def test_summarize_dispatch_holds_single_bucket():
    """B: one bucket → counts + dominant name it."""
    res = kb.DispatchResult()
    res.respawn_guarded = [("t1", "recent_success"), ("t2", "recent_success")]
    total, counts, dominant = kb.summarize_dispatch_holds([res])
    assert total == 2
    assert counts == {"respawn_guarded": 2}
    assert dominant == "respawn_guarded"


def test_summarize_dispatch_holds_aggregates_and_picks_dominant():
    """B: aggregate across passes (gateway is multi-board); dominant = the
    bucket holding the most tasks."""
    a = kb.DispatchResult()
    a.skipped_repo_serialized = [("t1", "/repo")]
    a.respawn_guarded = [("t2", "recent_success")]
    b = kb.DispatchResult()
    b.skipped_repo_serialized = [("t3", "/repo"), ("t4", "/repo")]
    b.budget_held = [("t5", "premium", "daily_token_cap")]
    total, counts, dominant = kb.summarize_dispatch_holds([a, b])
    assert total == 5
    assert counts == {"repo_serialized": 3, "respawn_guarded": 1, "budget_held": 1}
    assert dominant == "repo_serialized"


def test_summarize_dispatch_holds_ignores_none_and_non_hold_buckets():
    """B: None entries are skipped; spawned / skipped_unassigned are NOT
    expected-hold buckets (unassigned stays operator-actionable / stuck)."""
    res = kb.DispatchResult()
    res.spawned = [("t1", "a", "/ws")]
    res.skipped_unassigned = ["t2"]
    assert kb.summarize_dispatch_holds([None, res]) == (0, {}, None)


def test_resolve_rate_limit_cooldown_handles_bad_env(monkeypatch):
    import hermes_cli.kanban_db as _kb

    for bad_val in ("notanumber", "-5", ""):
        monkeypatch.setenv(
            "HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", bad_val
        )
        assert (
            _kb._resolve_rate_limit_cooldown_seconds()
            == _kb.DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
        )


def test_max_runtime_uses_current_run_start_after_retry(kanban_home, monkeypatch):
    """A retry should get a fresh max-runtime window.

    ``tasks.started_at`` intentionally records the first time the task ever
    started. Runtime enforcement must therefore use the active
    ``task_runs.started_at`` row; otherwise every retry of an old task is
    immediately timed out again.
    """
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        t = kb.create_task(
            conn, title="retry", assignee="a", max_runtime_seconds=10,
        )

        kb.claim_task(conn, t, claimer=f"{host}:first")
        first_run_id = kb.latest_run(conn, t).id
        old_started = int(time.time()) - 20
        conn.execute(
            "UPDATE tasks SET started_at = ?, worker_pid = ? WHERE id = ?",
            (old_started, 999999, t),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ?, worker_pid = ? WHERE id = ?",
            (old_started, 999999, first_run_id),
        )

        timed_out = kb.enforce_max_runtime(conn, signal_fn=lambda _pid, _sig: None)
        assert timed_out == [t]
        assert kb.get_task(conn, t).status == "ready"

        kb.claim_task(conn, t, claimer=f"{host}:retry")
        retry_run = kb.latest_run(conn, t)
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (999999, t),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
            (999999, retry_run.id),
        )

        timed_out = kb.enforce_max_runtime(conn, signal_fn=lambda _pid, _sig: None)
        assert timed_out == []
        assert kb.get_task(conn, t).status == "running"


def test_heartbeat_extends_claim(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        claimer = "host:hb"
        kb.claim_task(conn, t, claimer=claimer, ttl_seconds=60)
        original = kb.get_task(conn, t).claim_expires
        # Rewind then heartbeat.
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (0, t))
        ok = kb.heartbeat_claim(conn, t, claimer=claimer, ttl_seconds=3600)
        assert ok
        new = kb.get_task(conn, t).claim_expires
        assert new > int(time.time()) + 3000


def test_heartbeat_uses_env_default_ttl(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_TTL_SECONDS", "3600")
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        claimer = "host:hb"
        kb.claim_task(conn, t, claimer=claimer, ttl_seconds=60)
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (0, t))
        ok = kb.heartbeat_claim(conn, t, claimer=claimer)
        assert ok
        new = kb.get_task(conn, t).claim_expires
        assert new is not None
        assert new > int(time.time()) + 3000


def test_concurrent_claims_only_one_wins(kanban_home):
    """Fire N threads claiming the same task; exactly one must win."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="race", assignee="a")

    def attempt(i):
        with kb.connect_closing() as c:
            return kb.claim_task(c, t, claimer=f"host:{i}")

    n_workers = 8
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as ex:
        results = list(ex.map(attempt, range(n_workers)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].status == "running"


# ---------------------------------------------------------------------------
# Complete / block / unblock / archive / assign
# ---------------------------------------------------------------------------

def test_complete_records_result(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        assert kb.complete_task(conn, t, result="done and dusted")
        task = kb.get_task(conn, t)
    assert task.status == "done"
    assert task.result == "done and dusted"
    assert task.completed_at is not None


def test_block_then_unblock(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason="need input")
        assert kb.get_task(conn, t).status == "blocked"
        assert kb.unblock_task(conn, t)
        assert kb.get_task(conn, t).status == "ready"


def test_block_task_stores_reviewer_metadata(kanban_home):
    """B-T10: block_task persists structured reviewer_metadata into
    task_runs.metadata (no second migration). Default None = byte-identical."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="coder")
        kb.claim_task(conn, tid)
        rid = kb._current_run_id(conn, tid)
        meta = {"verdict": "REQUEST_CHANGES",
                "blocking_findings": ["null deref in foo()", "missing test for bar"]}
        assert kb.block_task(conn, tid, reason="changes needed", reviewer_metadata=meta)
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (rid,)
        ).fetchone()
        stored = json.loads(row["metadata"])
        assert stored["blocking_findings"][0].startswith("null deref")
        assert stored["verdict"] == "REQUEST_CHANGES"


def test_block_task_without_metadata_is_unchanged(kanban_home):
    """Default None reviewer_metadata leaves the run metadata empty (today)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="coder")
        kb.claim_task(conn, tid)
        rid = kb._current_run_id(conn, tid)
        assert kb.block_task(conn, tid, reason="plain block")
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (rid,)
        ).fetchone()
        assert row["metadata"] in (None, "", "{}", "null")


def test_unblock_resets_failure_counters(kanban_home):
    """unblock_task must reset consecutive_failures and last_failure_error."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason="need input")
        # Simulate accumulated failures from the circuit breaker
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 5, "
            "last_failure_error = 'test error' WHERE id = ?",
            (t,),
        )
        conn.commit()
        assert kb.unblock_task(conn, t)
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


def test_recompute_ready_skips_tasks_at_failure_limit(kanban_home):
    """recompute_ready must not auto-recover tasks whose consecutive_failures
    has reached the circuit-breaker limit (#35072).

    Without this guard, a task that repeatedly exhausts its iteration
    budget would cycle forever: block → auto-recover (counter reset)
    → respawn → budget exhausted → block → …
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a",
                               parents=[parent])
        # Complete the parent so the child's dependencies are satisfied.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, summary="done")

        # Simulate the child having exhausted its budget twice,
        # hitting the default failure limit (2).
        kb.claim_task(conn, child)
        kb._record_task_failure(
            conn, child, error="budget exhausted 1",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        kb._record_task_failure(
            conn, child, error="budget exhausted 2",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        task = kb.get_task(conn, child)
        assert task.status == "blocked"
        assert task.consecutive_failures >= 2

        # recompute_ready must NOT promote this task — the circuit
        # breaker has tripped and it should stay blocked.
        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "blocked"

        # Explicit unblock should still work and reset the counter.
        assert kb.unblock_task(conn, child)
        task = kb.get_task(conn, child)
        assert task.status == "ready"
        assert task.consecutive_failures == 0


def test_recompute_ready_recovers_below_limit(kanban_home):
    """recompute_ready auto-recovers blocked tasks that haven't hit the
    failure limit yet — the counter is preserved across recovery."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="task", assignee="a")
        kb.claim_task(conn, t)
        # One failure, below the default limit of 2.
        kb._record_task_failure(
            conn, t, error="budget exhausted 1",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == 1

        # Simulate being blocked by something else (not circuit breaker).
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,),
        )
        conn.commit()

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        # Counter must be preserved, not reset.
        assert task.consecutive_failures == 1


def test_recompute_ready_honours_dispatcher_failure_limit(kanban_home):
    """The guard's effective limit must follow the same resolution order
    as the circuit breaker (#35072): per-task max_retries → dispatcher
    failure_limit → DEFAULT_FAILURE_LIMIT.

    Without threading the dispatcher's ``kanban.failure_limit`` through,
    the guard falls back to DEFAULT_FAILURE_LIMIT and disagrees with the
    breaker — sticking a task prematurely (config limit > default) or
    letting a tripped task escape (config limit < default).
    """
    with kb.connect_closing() as conn:
        # Config allows MORE retries than the default. A task blocked
        # with failures below the configured limit must still recover.
        t = kb.create_task(conn, title="lenient", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=? "
            "WHERE id=?",
            (kb.DEFAULT_FAILURE_LIMIT, t),
        )
        conn.commit()
        # Default-limit call would stick it (failures >= default).
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "blocked"
        # Dispatcher configured a higher limit → recover, preserve counter.
        promoted = kb.recompute_ready(
            conn, failure_limit=kb.DEFAULT_FAILURE_LIMIT + 2
        )
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == kb.DEFAULT_FAILURE_LIMIT

        # Config allows FEWER retries than the default. A task at the
        # stricter limit must stay blocked even though it's below default.
        t2 = kb.create_task(conn, title="strict", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=1 "
            "WHERE id=?",
            (t2,),
        )
        conn.commit()
        # Default-limit (2) would recover it (1 < 2).
        # Stricter config limit (1) must keep it blocked (1 >= 1).
        assert kb.recompute_ready(conn, failure_limit=1) == 0
        assert kb.get_task(conn, t2).status == "blocked"


def test_recompute_ready_honours_persisted_gave_up_effective_limit(kanban_home):
    """A later recompute without dispatcher config must not reopen a task
    that was parked by a stricter failure_limit in ``_record_task_failure``.
    """
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="strict breaker", assignee="a")
        assert kb.claim_task(conn, task_id, claimer="host:1") is not None

        kb._record_task_failure(
            conn,
            task_id,
            error="spawn boom",
            outcome="spawn_failed",
            failure_limit=1,
            release_claim=True,
            end_run=True,
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.consecutive_failures == 1

        # No failure_limit argument here: this simulates a later dashboard or
        # maintenance recompute pass that only has DEFAULT_FAILURE_LIMIT (2).
        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"


def test_recompute_ready_per_task_max_retries_overrides_dispatcher(kanban_home):
    """A per-task ``max_retries`` wins over the dispatcher failure_limit,
    matching ``_record_task_failure``'s resolution order."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="per-task", assignee="a")
        # Per-task allows 4 retries; dispatcher config says 2.
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=2, "
            "max_retries=4 WHERE id=?",
            (t,),
        )
        conn.commit()
        # failures(2) < per-task limit(4) → recover, despite dispatcher=2.
        promoted = kb.recompute_ready(conn, failure_limit=2)
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == 2


# ---------------------------------------------------------------------------
# Parent-completion invariant at the claim gate (RCA t_a6acd07d)
# ---------------------------------------------------------------------------

def test_claim_rejects_when_parents_not_done(kanban_home):
    """claim_task must refuse ready->running if any parent isn't 'done'.

    Simulates the create-then-link race: a task gets status='ready' via a
    racy writer while it still has undone parents. The claim gate must
    detect the violation, demote the child back to 'todo', append a
    'claim_rejected' event, and return None. Covers Fix 1 of the RCA.
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        # Child correctly starts 'todo' because parent is not 'done'.
        assert kb.get_task(conn, child).status == "todo"
        # Simulate the race: a racy writer force-promotes the child to
        # 'ready' while parent is still pending.
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=?", (child,),
        )
        conn.commit()
        assert kb.get_task(conn, child).status == "ready"

        result = kb.claim_task(conn, child, claimer="host:1")

    assert result is None
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, child).status == "todo"
        events = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? ORDER BY id",
            (child,),
        ).fetchall()
    kinds = [e["kind"] for e in events]
    assert "claim_rejected" in kinds
    # No 'claimed' event was emitted for the blocked attempt.
    assert "claimed" not in kinds


def test_claim_succeeds_once_parents_done(kanban_home):
    """After parents complete, recompute_ready -> claim_task must succeed."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        kb.claim_task(conn, parent)
        assert kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        claimed = kb.claim_task(conn, child, claimer="host:1")
    assert claimed is not None
    assert claimed.status == "running"


def test_create_with_parents_stays_todo_until_parents_done(kanban_home):
    """kanban_create(parents=[...]) must land in 'todo' and only promote on parent done."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        assert kb.get_task(conn, child).status == "todo"
        # Dispatcher tick between create and some later event must NOT
        # produce a winner for this child.
        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "todo"
        # Complete parent; complete_task internally runs recompute_ready,
        # which promotes the child to 'ready'.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        assert kb.get_task(conn, child).status == "ready"


def test_unblock_with_pending_parents_goes_to_todo(kanban_home):
    """unblock_task must re-gate on parent completion (Fix 3).

    A task blocked while parents are still in progress must return to
    'todo' (not 'ready') on unblock. Otherwise the dispatcher will claim
    it immediately, repeating Bug 2 from the RCA.
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        # Force child into 'blocked' regardless of parent progress
        # (simulates a worker that self-blocked, or an operator block).
        conn.execute(
            "UPDATE tasks SET status='blocked' WHERE id=?", (child,),
        )
        conn.commit()
        assert kb.unblock_task(conn, child)
        assert kb.get_task(conn, child).status == "todo"
        # After parent completes + recompute, the child is ready.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_unblock_without_parents_goes_to_ready(kanban_home):
    """Parent-free unblock still produces 'ready' (behavior preserved)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="lone", assignee="a")
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason="need input")
        assert kb.unblock_task(conn, t)
        assert kb.get_task(conn, t).status == "ready"


# ---------------------------------------------------------------------------
# D1 block-semantics regression slice (disposition-di_1bd971c1-S1)
#
# These lock the block_task / complete_task / recompute_ready contracts that
# carry the "dependency wait" and "no tight redispatch loop" behaviour:
#   * block_task fires from BOTH 'running' and 'ready' and synthesizes an
#     ended run when blocking a never-claimed ('ready') task so the reason
#     survives; it refuses a 'todo' task and a stale expected_run_id (CAS).
#   * complete_task wipes the circuit-breaker counters on success.
#   * recompute_ready must NOT promote a dependency-blocked child while its
#     parent is still open — promoting it back to 'ready' is exactly what
#     would let the dispatcher reclaim-then-reblock in a tight loop.
# (NB: there are no `block_kind`/`dependency_wait`/`block_loop_detected`
# symbols in the tree; those names from the task framing were never shipped.
# The behaviour they describe lives in the functions exercised below.)
# ---------------------------------------------------------------------------


def test_block_task_from_ready_state_synthesizes_run(kanban_home):
    """A never-claimed 'ready' task can be blocked; because no run is open the
    reason is preserved on a synthesized ended run rather than dropped."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="lone ready", assignee="a")
        assert kb.get_task(conn, t).status == "ready"  # parent-free -> ready
        # No claim => current_run_id is NULL, so block must synthesize a run.
        assert kb.block_task(conn, t, reason="operator hold")
        assert kb.get_task(conn, t).status == "blocked"
        row = conn.execute(
            "SELECT outcome, summary FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert row is not None  # synthesized, not silently dropped
        assert row["outcome"] == "blocked"
        assert row["summary"] == "operator hold"


def test_block_task_on_todo_task_is_rejected(kanban_home):
    """block_task only fires from 'running'/'ready'. A gated 'todo' child
    (open parent) cannot be force-blocked, and no orphan run is synthesized."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"  # gated by open parent
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM task_runs WHERE task_id = ?", (child,)
        ).fetchone()["c"]
        assert kb.block_task(conn, child, reason="noop") is False
        assert kb.get_task(conn, child).status == "todo"  # unchanged
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM task_runs WHERE task_id = ?", (child,)
        ).fetchone()["c"]
        assert after == before  # rejected block synthesizes nothing


def test_block_task_expected_run_id_mismatch_is_rejected(kanban_home):
    """A stale worker's expected_run_id must not block the live attempt
    (compare-and-swap guard). Mismatch -> no transition, status stays running;
    the matching id blocks as expected."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        live_run = kb._current_run_id(conn, t)
        assert live_run is not None
        assert kb.block_task(conn, t, reason="stale", expected_run_id=live_run + 1) is False
        assert kb.get_task(conn, t).status == "running"  # untouched
        assert kb.block_task(conn, t, reason="real", expected_run_id=live_run) is True
        assert kb.get_task(conn, t).status == "blocked"


def test_complete_task_clears_consecutive_failures(kanban_home):
    """complete_task wipes the circuit-breaker counters on success (the
    'complete_task reset' path) — symmetric with unblock_task."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 4, transient_retry_count = 2, "
            "last_failure_error = 'boom' WHERE id = ?",
            (t,),
        )
        conn.commit()
        assert kb.complete_task(conn, t, summary="green")
        task = kb.get_task(conn, t)
        assert task.status == "done"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


def test_recompute_ready_leaves_blocked_child_gated_while_parent_open(kanban_home):
    """A dependency-blocked child whose parent is still open must NOT be
    promoted by recompute_ready. Promoting it back to 'ready' here is exactly
    what would let the dispatcher reclaim-then-reblock in a tight loop; the
    child only becomes eligible once the parent is done."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
        # Parent in progress (claimed, not done); force the child to blocked.
        kb.claim_task(conn, parent)
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=0, "
            "last_failure_error=NULL WHERE id=?",
            (child,),
        )
        conn.commit()
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "blocked"  # still gated
        # Only once the parent is done does the child become eligible.
        kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_dependency_wait_block_kind_is_todo_not_reclaimable(kanban_home):
    """AC-1: A child with an open parent is naturally gated to 'todo' by
    create_task/claim_task. Additionally, a running child whose parent is
    reopened can be blocked with kind='dependency' and lands in 'todo'; it is
    not claimable or runnable while the parent is still open."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
        # Natural gate: child is created as todo, cannot be claimed.
        assert kb.get_task(conn, child).status == "todo"
        assert kb.claim_task(conn, child) is None
        ok, msg = kb.promote_task(conn, child, actor="test")
        assert ok is False
        assert msg is not None and "unsatisfied parent" in msg

        # Simulate the rare dispatcher path: parent is done, child runs,
        # parent somehow becomes un-done again (e.g. rollback/reopen), and the
        # run realises it needs to dependency-wait. Block with kind='dependency'
        # must park the child back on todo, not blocked/triage.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, child) is not None
        # Reopen parent manually to simulate the dependency-wait trigger.
        conn.execute(
            "UPDATE tasks SET status='todo' WHERE id=?", (parent,)
        )
        conn.commit()
        assert kb.block_task(conn, child, reason="waiting for parent", kind="dependency")
        task = kb.get_task(conn, child)
        assert task.status == "todo"
        assert task.block_kind == "dependency"
        # claim_task and promote_task must refuse while parent is open.
        assert kb.claim_task(conn, child) is None
        ok, msg = kb.promote_task(conn, child, actor="test")
        assert ok is False
        assert msg is not None and (
            "dependency wait" in msg or "unsatisfied parent dependencies" in msg
        )


def test_dependency_wait_promotes_when_parent_done_and_resets_block_kind(kanban_home):
    """AC-2: Once the parent completes, the dependency-wait child is promoted to
    'ready' and its block_kind/recurrences are cleared."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
        # Parent must be done before the child can run and then be parked.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        assert kb.claim_task(conn, child) is not None
        # Reopen parent to create the dependency-wait condition.
        conn.execute(
            "UPDATE tasks SET status='todo' WHERE id=?", (parent,)
        )
        conn.commit()
        assert kb.block_task(conn, child, reason="waiting for parent", kind="dependency")
        task = kb.get_task(conn, child)
        assert task.status == "todo"
        assert task.block_kind == "dependency"
        # Re-complete the parent; child should be promoted and block_kind reset.
        kb.recompute_ready(conn)  # parent was reopened; get it back to ready first
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        kb.recompute_ready(conn)
        task = kb.get_task(conn, child)
        assert task is not None
        assert task.status == "ready"
        assert task.block_kind is None
        assert task.block_recurrences == 0
        # claim should now succeed.
        assert kb.claim_task(conn, child) is not None


def test_dependency_wait_does_not_escalate_loop_or_recurrence(kanban_home):
    """AC-3: Pure dependency waits must never be counted as loop/recurrence
    escalation and never produce triage, even when the dependency-wait
    pattern repeats on the same task."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a", parents=[parent])

        def block_dependency_wait():
            # Make the parent ready/claimed/done, then promote child to ready,
            # claim it, reopen the parent and dependency-wait the child.
            kb.recompute_ready(conn)  # parent may be todo after prior reopen
            kb.claim_task(conn, parent)
            kb.complete_task(conn, parent, result="ok")
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, child) is not None
            # Reopen parent to trigger the dependency wait.
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (parent,))
            conn.commit()
            assert kb.block_task(conn, child, reason="waiting", kind="dependency")
            assert kb.get_task(conn, child).status == "todo"
            assert kb.get_task(conn, child).block_recurrences == 1
            event_count = conn.execute(
                "SELECT COUNT(*) AS c FROM task_events "
                "WHERE task_id = ? AND kind = 'block_loop_detected'",
                (child,),
            ).fetchone()["c"]
            assert event_count == 0

        # Repeat the dependency-wait cycle several times.  Recurrences must
        # stay pinned to 1 and triage/loop-detected events must never fire.
        for _ in range(3):
            block_dependency_wait()
        # While parent is still open, unblock_task must not promote it either.
        kb.unblock_task(conn, child)
        assert kb.get_task(conn, child).status == "todo"


def test_assign_refuses_while_running(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        with pytest.raises(RuntimeError, match="currently running"):
            kb.assign_task(conn, t, "b")


def test_assign_reassigns_when_not_running(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        assert kb.assign_task(conn, t, "b")
        assert kb.get_task(conn, t).assignee == "b"


def test_assignee_normalized_to_lowercase_on_create_and_assign(kanban_home):
    """Dashboard/CLI may pass title-cased profile labels; DB + spawn use canonical id."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cased", assignee="Jules")
        assert kb.get_task(conn, tid).assignee == "jules"
        assert kb.assign_task(conn, tid, "Librarian")
        assert kb.get_task(conn, tid).assignee == "librarian"


def test_list_tasks_assignee_filter_case_insensitive(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="q", assignee="jules")
        found = kb.list_tasks(conn, assignee="Jules")
        assert len(found) == 1 and found[0].id == tid


def test_archive_hides_from_default_list(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        kb.complete_task(conn, t)
        assert kb.archive_task(conn, t)
        assert len(kb.list_tasks(conn)) == 0
        assert len(kb.list_tasks(conn, include_archived=True)) == 1


def test_delete_archived_task_removes_related_rows(kanban_home):
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent")
        tid = kb.create_task(conn, title="child", parents=[parent], assignee="worker")
        kb.add_comment(conn, tid, "user", "cleanup me")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="done")
        assert kb.archive_task(conn, tid)
        conn.execute(
            "INSERT INTO kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, created_at, last_event_id) "
            "VALUES (?, 'telegram', '123', '', 'u', 0, 0)",
            (tid,),
        )
        conn.commit()

        assert kb.delete_archived_task(conn, tid) is True
        assert kb.get_task(conn, tid) is None
        assert conn.execute("SELECT COUNT(*) FROM task_links WHERE child_id = ? OR parent_id = ?", (tid, tid)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?", (tid,)).fetchone()[0] == 0


def test_delete_archived_task_rejects_non_archived_rows(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="live")
        assert kb.delete_archived_task(conn, tid) is False
        assert kb.get_task(conn, tid) is not None


def test_list_tasks_order_by(kanban_home):
    with kb.connect_closing() as conn:
        # Create tasks with different titles and priorities
        t_a = kb.create_task(conn, title="alpha", priority=1)
        t_b = kb.create_task(conn, title="beta", priority=2)
        t_c = kb.create_task(conn, title="gamma", priority=1)

        # Default sort: priority DESC, created ASC
        default = kb.list_tasks(conn)
        assert [t.id for t in default] == [t_b, t_a, t_c]

        # Sort by title ASC
        by_title = kb.list_tasks(conn, order_by="title")
        assert [t.id for t in by_title] == [t_a, t_b, t_c]

        # Sort by assignee
        kb.assign_task(conn, t_a, "alice")
        kb.assign_task(conn, t_b, "bob")
        kb.assign_task(conn, t_c, "alice")
        by_assignee = kb.list_tasks(conn, order_by="assignee")
        # alice's tasks first (alphabetically), then bob's
        assignees = [t.assignee for t in by_assignee]
        assert assignees[:2] == ["alice", "alice"]
        assert assignees[2] == "bob"

        # Invalid sort order raises ValueError
        try:
            kb.list_tasks(conn, order_by="bogus")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "order_by must be one of" in str(e)

def test_delete_task_removes_task_and_cascades(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="to-delete", assignee="alice")
        kb.add_comment(conn, t, "user", "comment")
        kb.add_comment(conn, t, "user", "another")
        assert kb.delete_task(conn, t)
        assert kb.get_task(conn, t) is None
        assert len(kb.list_comments(conn, t)) == 0
        assert len(kb.list_events(conn, t)) == 0
        assert len(kb.list_runs(conn, t)) == 0


def test_delete_task_returns_false_for_missing_task(kanban_home):
    with kb.connect_closing() as conn:
        assert not kb.delete_task(conn, "t_nonexistent")


def test_delete_task_cascades_links(kanban_home):
    with kb.connect_closing() as conn:
        p = kb.create_task(conn, title="parent")
        c = kb.create_task(conn, title="child", parents=[p])
        child = kb.get_task(conn, c)
        assert child is not None and child.status == "todo"
        kb.delete_task(conn, p)
        assert kb.get_task(conn, p) is None
        child_after = kb.get_task(conn, c)
        assert child_after is not None and child_after.status == "ready"


# ---------------------------------------------------------------------------
# Comments / events / worker context
# ---------------------------------------------------------------------------

def test_comments_recorded_in_order(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        kb.add_comment(conn, t, "user", "first")
        kb.add_comment(conn, t, "researcher", "second")
        comments = kb.list_comments(conn, t)
    assert [c.body for c in comments] == ["first", "second"]
    assert [c.author for c in comments] == ["user", "researcher"]


def test_empty_comment_rejected(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        with pytest.raises(ValueError, match="body is required"):
            kb.add_comment(conn, t, "user", "")


def test_events_capture_lifecycle(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        kb.complete_task(conn, t, result="ok")
        events = kb.list_events(conn, t)
    kinds = [e.kind for e in events]
    assert "created" in kinds
    assert "claimed" in kinds
    assert "completed" in kinds


def test_worker_context_includes_parent_results_and_comments(kanban_home):
    with kb.connect_closing() as conn:
        p = kb.create_task(conn, title="p")
        kb.complete_task(conn, p, result="PARENT_RESULT_MARKER")
        c = kb.create_task(conn, title="child", parents=[p])
        kb.add_comment(conn, c, "user", "CLARIFICATION_MARKER")
        ctx = kb.build_worker_context(conn, c)
    assert "PARENT_RESULT_MARKER" in ctx
    assert "CLARIFICATION_MARKER" in ctx
    assert c in ctx
    assert "child" in ctx




def test_worker_context_worker_slim_uses_tighter_caps(kanban_home):
    big_body = "BODY-" + ("x" * 9000)
    big_comment = "COMMENT-" + ("y" * 3000)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="caps", body=big_body, assignee="coder")
        for idx in range(12):
            kb.add_comment(conn, t, "worker", f"{idx}-" + big_comment)
        now = 1_800_000_000
        for idx in range(5):
            conn.execute(
                """
                INSERT INTO task_runs (
                    task_id, profile, status, started_at, ended_at, outcome, summary
                ) VALUES (?, ?, 'done', ?, ?, 'completed', ?)
                """,
                (t, "coder", now + idx, now + idx + 1, f"summary-{idx}"),
            )
        conn.commit()
        full = kb.build_worker_context(conn, t)
        slim = kb.build_worker_context(conn, t, profile="worker_slim")

    assert len(slim) < len(full)
    assert "showing most recent 8" in slim
    assert "showing most recent 30" not in slim
    assert "showing most recent 3" in slim
    assert "summary-0" not in slim
    assert "summary-4" in slim
    assert "[truncated," in slim



def test_worker_context_reviewer_review_uses_larger_body_cap(kanban_home):
    """Reviewer code-review cards keep a larger diff/test body visible.

    Regression: compact code-review cards exceeded the default 8 KiB body cap,
    so the verdict-only reviewer could see instructions but not the full
    implementation/test evidence and exhausted its iteration budget asking for
    more context. Ordinary tasks keep the default cap; only assignee=reviewer +
    kind=review gets the larger opening-body window.
    """
    default_cap = kb._CTX_CAP_PROFILES["full"]["body_bytes"]
    reviewer_cap = kb._CTX_CAP_PROFILES["reviewer_review"]["body_bytes"]
    assert reviewer_cap > default_cap
    body = "BEGIN\n" + ("x" * (default_cap + 100)) + "\nVISIBLE_REVIEW_EVIDENCE"

    with kb.connect_closing() as conn:
        reviewer_task = kb.create_task(
            conn, title="review patch", body=body, assignee="reviewer", kind="review"
        )
        coder_task = kb.create_task(conn, title="ordinary", body=body, assignee="coder")
        reviewer_ctx = kb.build_worker_context(conn, reviewer_task, profile="full")
        coder_ctx = kb.build_worker_context(conn, coder_task, profile="full")

    assert "VISIBLE_REVIEW_EVIDENCE" in reviewer_ctx
    assert "VISIBLE_REVIEW_EVIDENCE" not in coder_ctx
    assert "[truncated," in coder_ctx


def test_worker_context_reviewer_review_continuation_uses_retry_caps(kanban_home):
    """The larger reviewer body cap must not apply to continuation retries."""
    retry_cap = kb._CTX_CAP_PROFILES["retry"]["body_bytes"]
    body = "BEGIN\n" + ("x" * (retry_cap + 100)) + "\nHIDDEN_ON_RETRY"
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="review retry", body=body, assignee="reviewer", kind="review"
        )
        conn.execute("UPDATE tasks SET continuation_count=1 WHERE id=?", (task_id,))
        conn.commit()
        ctx = kb.build_worker_context(conn, task_id, profile="full")

    assert "This is continuation run 1/" in ctx
    assert "HIDDEN_ON_RETRY" not in ctx
    assert "[truncated," in ctx

def test_worker_context_worker_slim_retry_uses_retry_profile(kanban_home):
    """Continuation workers on worker_slim use the tighter retry caps."""
    big_body = "BODY-" + ("x" * 3000)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="retry caps", body=big_body, assignee="coder")
        for idx in range(6):
            kb.add_comment(conn, t, "worker", f"comment-{idx}")
        now = 1_800_000_000
        for idx in range(3):
            conn.execute(
                """
                INSERT INTO task_runs (
                    task_id, profile, status, started_at, ended_at, outcome, summary
                ) VALUES (?, ?, 'done', ?, ?, 'completed', ?)
                """,
                (t, "coder", now + idx, now + idx + 1, f"summary-{idx}"),
            )
        conn.execute("UPDATE tasks SET continuation_count=1 WHERE id=?", (t,))
        conn.commit()

        ctx = kb.build_worker_context(conn, t, profile="worker_slim")

    assert "This is continuation run 1/" in ctx
    assert "showing most recent 1" in ctx
    assert "summary-0" not in ctx
    assert "summary-1" not in ctx
    assert "summary-2" in ctx
    assert "showing most recent 4" in ctx
    assert "comment-1" not in ctx
    assert "comment-2" in ctx
    assert "[truncated," in ctx


def test_worker_context_full_retry_uses_retry_profile_caps(kanban_home):
    """Continuation workers on full context also use the retry caps.

    Regression: verifier review runs request ``profile='full'``. Continuation
    review runs must still get the small retry caps, keyed by the context
    profile parameter rather than by task assignee.
    """
    big_body = "BODY-" + ("x" * (kb._CTX_CAP_PROFILES["retry"]["body_bytes"] + 500))
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="retry caps", body=big_body, assignee="verifier")
        for idx in range(kb._CTX_CAP_PROFILES["retry"]["comments"] + 2):
            kb.add_comment(conn, t, "worker", f"comment-{idx}")
        for idx in range(kb._CTX_CAP_PROFILES["retry"]["prior_attempts"] + 2):
            _seed_completed_run(conn, t, "verifier", 1_800_000_000 + idx, f"summary-{idx}")
        conn.execute("UPDATE tasks SET continuation_count=1 WHERE id=?", (t,))
        conn.commit()

        ctx = kb.build_worker_context(conn, t, profile="full")

    retry_caps = kb._CTX_CAP_PROFILES["retry"]
    full_caps = kb._CTX_CAP_PROFILES["full"]
    assert retry_caps["prior_attempts"] < full_caps["prior_attempts"]
    assert retry_caps["comments"] < full_caps["comments"]
    assert "This is continuation run 1/" in ctx
    assert f"showing most recent {retry_caps['prior_attempts']}" in ctx
    assert "summary-0" not in ctx
    assert "summary-2" in ctx
    assert f"showing most recent {retry_caps['comments']}" in ctx
    assert "comment-0" not in ctx
    assert "comment-5" in ctx
    assert "[truncated," in ctx


def test_worker_context_full_without_continuation_keeps_full_profile_caps(kanban_home):
    """Non-continuation full contexts must not be downgraded to retry caps."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="full caps", assignee="verifier")
        for idx in range(kb._CTX_CAP_PROFILES["retry"]["prior_attempts"] + 2):
            _seed_completed_run(conn, t, "verifier", 1_800_000_000 + idx, f"full-summary-{idx}")
        conn.commit()

        ctx = kb.build_worker_context(conn, t, profile="full")

    assert "This is continuation run" not in ctx
    assert f"showing most recent {kb._CTX_CAP_PROFILES['retry']['prior_attempts']}" not in ctx
    assert "full-summary-0" in ctx
    assert "full-summary-2" in ctx


def test_worker_context_prior_attempts_unchanged_by_shared_renderer_refactor(kanban_home):
    """Parity guard: build_worker_context's 'Prior attempts on this task'
    section (now backed by the shared _render_prior_attempts helper) must
    keep the exact same strings/ordering as before the refactor that
    extracted it for reuse by the claude-CLI worker path."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="ship the widget", assignee="coder")
        kb.claim_task(conn, t)
        meta = {
            "verdict": "REQUEST_CHANGES",
            "blocking_findings": ["null deref in foo()", "missing test for bar"],
        }
        kb.block_task(conn, t, reason="lint failed, see foo()", reviewer_metadata=meta)
        kb.unblock_task(conn, t)

        ctx = kb.build_worker_context(conn, t, profile="full")

    assert "## Prior attempts on this task" in ctx
    assert "### Attempt 1 —" in ctx
    assert "lint failed, see foo()" in ctx
    assert "null deref in foo()" in ctx
    assert "REQUEST_CHANGES" in ctx
    # Ordering unchanged: prior attempts render after the header/knowledge
    # pointers block and before the end of the context (no parents/comments
    # here, so this pins the section stays where it always has).
    assert ctx.index("## Knowledge pointers") < ctx.index(
        "## Prior attempts on this task"
    )


def test_worker_context_retry_suppresses_recent_work(kanban_home):
    """Continuation workers do not receive cross-task recent-work history."""
    with kb.connect_closing() as conn:
        previous = kb.create_task(conn, title="previous", assignee="coder")
        _seed_completed_run(conn, previous, "coder", 1_800_000_000, "PRIOR_RECENT_WORK")

        t = kb.create_task(conn, title="retry followup", assignee="coder")
        conn.execute("UPDATE tasks SET continuation_count=1 WHERE id=?", (t,))
        conn.commit()

        ctx = kb.build_worker_context(conn, t, profile="worker_slim")

    assert "## Recent work by @coder" not in ctx
    assert "PRIOR_RECENT_WORK" not in ctx


def _seed_completed_run(conn, task_id, profile, ended_at, summary):
    """Insert a completed task_runs row for role-history."""
    conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, status, started_at, ended_at, outcome, summary
        ) VALUES (?, ?, 'done', ?, ?, 'completed', ?)
        """,
        (task_id, profile, ended_at - 1, ended_at, summary),
    )


def test_worker_context_recent_work_tenant_scoped(kanban_home):
    """AC-TENANT-SCOPED: on a multi-tenant board the recent-work section
    must only surface completed runs for the active tenant, not cross-tenant."""
    with kb.connect_closing() as conn:
        # Two tenants, same assignee, completed runs in both.
        t_a1 = kb.create_task(conn, title="tenant-A task 1", assignee="coder", tenant="tenantA")
        t_a2 = kb.create_task(conn, title="tenant-A task 2", assignee="coder", tenant="tenantA")
        t_b1 = kb.create_task(conn, title="tenant-B task", assignee="coder", tenant="tenantB")
        now = 1_800_000_000
        _seed_completed_run(conn, t_a1, "coder", now + 10, "SUMMARY_TENANT_A1")
        _seed_completed_run(conn, t_a2, "coder", now + 20, "SUMMARY_TENANT_A2")
        _seed_completed_run(conn, t_b1, "coder", now + 30, "SUMMARY_TENANT_B")
        conn.commit()
        ctx_a = kb.build_worker_context(conn, t_a1)
    # Extract just the "Recent work" section.
    rw_start = ctx_a.find("## Recent work by @coder")
    rw_section = ctx_a[rw_start:] if rw_start >= 0 else ""
    assert rw_section, "Recent work section should be present"
    # Tenant A's other task surfaces in recent work.
    assert "SUMMARY_TENANT_A2" in rw_section
    assert "tenant-A task 2" in rw_section
    # Cross-tenant history must NOT leak into tenant A's worker context.
    assert "SUMMARY_TENANT_B" not in rw_section
    assert "tenant-B task" not in rw_section


def test_worker_context_recent_work_untenanted_stable(kanban_home):
    """AC-UNTENANTED-STABLE: on an untenanted board the recent-work output
    is byte-identical to the pre-fix behavior (no tenant filter applied)."""
    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="task-1", assignee="coder")
        t2 = kb.create_task(conn, title="task-2", assignee="coder")
        now = 1_800_000_000
        _seed_completed_run(conn, t1, "coder", now + 10, "SUMMARY_ONE")
        _seed_completed_run(conn, t2, "coder", now + 20, "SUMMARY_TWO")
        conn.commit()
        ctx = kb.build_worker_context(conn, t2)
    # Extract just the "Recent work" section.
    rw_start = ctx.find("## Recent work by @coder")
    rw_section = ctx[rw_start:] if rw_start >= 0 else ""
    assert rw_section, "Recent work section should be present"
    # The other task's completed run surfaces (no tenant scoping on untenanted board).
    assert "SUMMARY_ONE" in rw_section
    assert "task-1" in rw_section
    # The current task t2 is excluded from its own recent-work (r.task_id != ?).
    assert "SUMMARY_TWO" not in rw_section


# ---------------------------------------------------------------------------
# F4: operator directives (kind='directive')
# ---------------------------------------------------------------------------

def test_add_comment_defaults_to_comment_kind(kanban_home):
    """Existing callers (and inline INSERTs) keep the 'comment' kind."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        kb.add_comment(conn, t, "user", "ordinary note")
        comments = kb.list_comments(conn, t)
    assert [c.kind for c in comments] == ["comment"]


def test_add_comment_directive_kind_persists(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        kb.add_comment(conn, t, "operator", "switch to plan B", kind="directive")
        comments = kb.list_comments(conn, t)
    assert [c.kind for c in comments] == ["directive"]


def test_add_comment_rejects_unknown_kind(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        with pytest.raises(ValueError, match="kind"):
            kb.add_comment(conn, t, "operator", "body", kind="bogus")


def test_directive_renders_as_priority_block(kanban_home):
    """A directive surfaces in build_worker_context as a distinct ⚠️ block,
    NOT under the 'comment from worker' framing."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", body="ORIGINAL_BODY_INSTRUCTION")
        kb.add_comment(conn, t, "operator", "STOP — do C instead", kind="directive")
        ctx = kb.build_worker_context(conn, t)
    assert "⚠️ OPERATOR DIRECTIVE — supersedes the task body above" in ctx
    assert "STOP — do C instead" in ctx
    # Distinct framing — a directive must not be rendered as a worker comment.
    assert "comment from worker `operator`" not in ctx


def test_directive_kept_separate_from_regular_comment_thread(kanban_home):
    """Directives go in the priority block; ordinary comments stay in the
    '## Comment thread' section under the worker-comment framing."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        kb.add_comment(conn, t, "worker", "REGULAR_WORKER_NOTE")
        kb.add_comment(conn, t, "operator", "DIRECTIVE_PAYLOAD", kind="directive")
        ctx = kb.build_worker_context(conn, t)
    # The directive block sits ABOVE the regular comment thread.
    assert ctx.index("OPERATOR DIRECTIVE") < ctx.index("## Comment thread")
    assert "comment from worker `worker`" in ctx
    assert "REGULAR_WORKER_NOTE" in ctx
    # The directive body is not duplicated into the worker-comment thread.
    assert ctx.count("DIRECTIVE_PAYLOAD") == 1


def test_no_directive_block_without_directives(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        kb.add_comment(conn, t, "worker", "just a note")
        ctx = kb.build_worker_context(conn, t)
    assert "OPERATOR DIRECTIVE" not in ctx


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------



def test_dispatch_treats_openclaw_assignee_as_nonspawnable(kanban_home, monkeypatch):
    """Native dispatcher must fail closed for legacy OpenClaw lanes.

    Even if stale DB rows mention ``openclaw:<agent>``, an autonomous Gateway
    tick must not sign or submit Mission-Control envelopes.
    """
    monkeypatch.setattr(kb, "_dispatch_to_openclaw", lambda *a, **k: (_ for _ in ()).throw(AssertionError("legacy dispatch touched")))
    conn = kb.connect()
    with conn:
        tid = kb.create_task(conn, title="legacy lane", assignee="openclaw:lens")
        res = kb.dispatch_once(conn, dry_run=False)

    assert tid in res.skipped_nonspawnable
    assert not res.openclaw_dispatched
    assert not res.spawned
    task = kb.get_task(conn, tid)
    assert task.status == "ready"
    events = [e.kind for e in kb.list_events(conn, tid)]
    assert "nonspawnable" in events

def test_dispatch_dry_run_does_not_claim(kanban_home, all_assignees_spawnable):
    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="a", assignee="alice")
        t2 = kb.create_task(conn, title="b", assignee="bob")
        res = kb.dispatch_once(conn, dry_run=True)
    assert {s[0] for s in res.spawned} == {t1, t2}
    with kb.connect_closing() as conn:
        # Dry run must NOT mutate status.
        assert kb.get_task(conn, t1).status == "ready"
        assert kb.get_task(conn, t2).status == "ready"


def test_dispatch_skips_unassigned(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="floater")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_unassigned
    assert t not in res.skipped_nonspawnable
    assert not res.spawned


def test_dispatch_skips_nonspawnable_into_separate_bucket(kanban_home, monkeypatch):
    """Tasks whose assignee fails profile_exists() must NOT land in
    ``skipped_unassigned`` (which is operator-actionable) — they go in
    the dedicated ``skipped_nonspawnable`` bucket so health telemetry
    can suppress false-positive "stuck" warnings."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="for-terminal", assignee="orion-cc")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_nonspawnable
    assert t not in res.skipped_unassigned
    assert not res.spawned


def test_has_spawnable_ready_false_when_only_terminal_lanes(kanban_home, monkeypatch):
    """``has_spawnable_ready`` returns False when every ready task is
    assigned to a control-plane lane — used by gateway/CLI dispatchers
    to silence the stuck-warn while terminals still have queued work."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="t1", assignee="orion-cc")
        kb.create_task(conn, title="t2", assignee="orion-research")
        assert kb.has_spawnable_ready(conn) is False


def test_has_spawnable_ready_true_when_real_profile_present(kanban_home, monkeypatch):
    """``has_spawnable_ready`` returns True as soon as ANY ready task
    has an assignee that maps to a real Hermes profile — preserves the
    real "stuck" signal when a daily/agent task is queued."""
    from hermes_cli import profiles
    monkeypatch.setattr(
        profiles, "profile_exists", lambda name: name == "daily"
    )
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="terminal-task", assignee="orion-cc")
        kb.create_task(conn, title="hermes-task", assignee="daily")
        assert kb.has_spawnable_ready(conn) is True


def test_has_spawnable_ready_false_on_empty_queue(kanban_home):
    """Empty queue is the trivial false case — no ready tasks at all."""
    with kb.connect_closing() as conn:
        assert kb.has_spawnable_ready(conn) is False


def test_dispatch_promotes_ready_and_spawns(kanban_home, all_assignees_spawnable):
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append((task.id, task.assignee, workspace))

    with kb.connect_closing() as conn:
        p = kb.create_task(conn, title="p", assignee="alice")
        c = kb.create_task(conn, title="c", assignee="bob", parents=[p])
        # Finish parent outside dispatch; promotion happens inside.
        kb.complete_task(conn, p)
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
    # Spawned c (a was already done when dispatch was called).
    assert len(spawns) == 1
    assert spawns[0][0] == c
    assert spawns[0][1] == "bob"
    # c is now running
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, c).status == "running"


def test_dispatch_spawn_failure_releases_claim(kanban_home, all_assignees_spawnable):
    def boom(task, workspace):
        raise RuntimeError("spawn failed")

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="boom", assignee="alice")
        kb.dispatch_once(conn, spawn_fn=boom)
        # Must return to ready so the next tick can retry.
        assert kb.get_task(conn, t).status == "ready"
        assert kb.get_task(conn, t).claim_lock is None


def test_dispatch_holds_reviewer_role_execution_mismatch(
    kanban_home, all_assignees_spawnable
):
    """K3: a reviewer task that asks the verdict-only lane to run repo gates is
    HELD at dispatch (not spawned) and left in ``ready`` for re-shaping."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append((task.id, task.assignee))

    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn,
            title="Review the gate output",
            assignee="reviewer",
            body="Bitte führe reale gates aus und run pytest im Repo.",
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

        # Not spawned; stays ready (advisory hold, NOT blocked).
        assert spawns == []
        assert all(s[0] != t for s in res.spawned)
        held_ids = [tid for tid, _ in res.held_role_mismatch]
        assert t in held_ids
        assert kb.get_task(conn, t).status == "ready"
        # Operator-visible diagnosis event was emitted.
        kinds = [e.kind for e in kb.list_events(conn, t)]
        assert "role_fit_held" in kinds


def test_dispatch_role_fit_held_event_is_deduped_across_ticks(
    kanban_home, all_assignees_spawnable
):
    """F2: a held reviewer task is re-evaluated every dispatch tick, but the
    ``role_fit_held`` diagnosis event is emitted only once while the hold
    state is unchanged — the hold itself stays reported every tick."""
    def fake_spawn(task, workspace):  # noqa: ARG001 - never invoked for a held task
        raise AssertionError("held task must not spawn")

    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn,
            title="Review the gate output",
            assignee="reviewer",
            body="Bitte führe reale gates aus und run pytest im Repo.",
        )

        res1 = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        res2 = kb.dispatch_once(conn, spawn_fn=fake_spawn)

        # Hold behaviour is byte-identical every tick (still reported, ready).
        assert t in [tid for tid, _ in res1.held_role_mismatch]
        assert t in [tid for tid, _ in res2.held_role_mismatch]
        assert kb.get_task(conn, t).status == "ready"

        # But the diagnosis event fired exactly once across the two ticks.
        held_events = [
            e for e in kb.list_events(conn, t) if e.kind == "role_fit_held"
        ]
        assert len(held_events) == 1


def test_dispatch_spawns_verdict_only_reviewer(
    kanban_home, all_assignees_spawnable
):
    """K3: a verdict-only reviewer task is exempt from the role-fit hold and
    dispatches normally even though it mentions gates."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append((task.id, task.assignee))

    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn,
            title="Verdict over parent evidence",
            assignee="reviewer",
            body=(
                "Verdict-only: prüfe die Parent-Belege und gib ein Verdict ab. "
                "Do not run tests selbst."
            ),
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

        assert (t, "reviewer") in spawns
        assert res.held_role_mismatch == []
        assert kb.get_task(conn, t).status == "running"



def test_dispatch_auto_retry_blocked_is_opt_in(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="transient MCP unavailable")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, max_spawn=0)

        assert res.auto_retried_blocked == []
        row = conn.execute(
            "SELECT status, auto_retry_count FROM tasks WHERE id = ?", (t,),
        ).fetchone()
        assert row["status"] == "blocked"
        assert row["auto_retry_count"] == 0



def test_dispatch_auto_retries_blocked_after_backoff_with_feedback_comment(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="transient MCP unavailable")

        monkeypatch.setattr(kb.time, "time", lambda: base + 299)
        early = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)
        assert early.auto_retried_blocked == []
        assert kb.get_task(conn, t).status == "blocked"

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == [(t, 1)]
        row = conn.execute(
            "SELECT status, auto_retry_count, model_override FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["auto_retry_count"] == 1
        assert row["model_override"] is None
        comments = kb.list_comments(conn, t)
        assert comments[-1].author == "dispatcher"
        assert "transient MCP unavailable" in comments[-1].body
        events = [e for e in kb.list_events(conn, t) if e.kind == "auto_retried"]
        assert len(events) == 1
        assert events[0].payload["attempt"] == 1


def test_dispatch_auto_retry_allows_first_request_changes_block(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="coder", body="AC v1")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Verifier found which assertion is missing")
        conn.execute(
            "UPDATE task_runs SET verdict = 'REQUEST_CHANGES' "
            "WHERE task_id = ? AND outcome = 'blocked'",
            (t,),
        )

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == [(t, 1)]
        row = conn.execute(
            "SELECT status, auto_retry_count FROM tasks WHERE id = ?", (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["auto_retry_count"] == 1


def test_dispatch_auto_retry_keeps_operator_hold_blocked(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="coder")
        kb.claim_task(conn, t)
        assert kb.hold_task(conn, t, reason="operator hold") is True

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        assert kb.get_task(conn, t).status == "blocked"
        event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retry_skipped"][-1]
        assert event.payload["blocked_kind"] == "operator_question"


def test_dispatch_auto_retry_harmless_prose_without_verdict_is_retryable(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="coder")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Worker noted which assertion failed")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == [(t, 1)]
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_auto_retry_escalates_repeated_request_changes_on_unchanged_body(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="coder", body="AC v1")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Verifier found issue one")
        conn.execute(
            "UPDATE task_runs SET verdict = 'REQUEST_CHANGES' "
            "WHERE task_id = ? AND outcome = 'blocked'",
            (t,),
        )

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        monkeypatch.setattr(kb.time, "time", lambda: base + 302)
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Verifier found issue two")
        conn.execute(
            "UPDATE task_runs SET verdict = 'REQUEST_CHANGES' "
            "WHERE task_id = ? AND outcome = 'blocked' AND verdict IS NULL",
            (t,),
        )

        monkeypatch.setattr(kb.time, "time", lambda: base + 603)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        row = conn.execute(
            "SELECT status, auto_retry_count, assignee, model_override "
            "FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        assert row["status"] == "blocked"
        assert row["auto_retry_count"] == 1
        assert row["assignee"] == "coder"
        assert row["model_override"] is None
        comments = kb.list_comments(conn, t)
        assert comments[-1].author == "dispatcher"
        assert "Verifier-Content-Block nach Retry auf unverändertem Body" in comments[-1].body
        event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retry_skipped"][-1]
        assert event.payload["blocked_kind"] == "needs_operator"


def test_dispatch_auto_retry_retries_transient_second_block_even_when_body_unchanged(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="research", body="AC v1")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="transient MCP unavailable")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        monkeypatch.setattr(kb.time, "time", lambda: base + 302)
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="tool crashed")

        monkeypatch.setattr(kb.time, "time", lambda: base + 603)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == [(t, 2)]
        row = conn.execute(
            "SELECT status, auto_retry_count, assignee FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["auto_retry_count"] == 2
        assert row["assignee"] == kb.AUTO_RETRY_ESCALATION_PROFILE


def test_dispatch_auto_retry_allows_request_changes_after_body_changes(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="coder", body="AC v1")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Verifier found issue one")
        conn.execute(
            "UPDATE task_runs SET verdict = 'REQUEST_CHANGES' "
            "WHERE task_id = ? AND outcome = 'blocked'",
            (t,),
        )

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", ("AC v2", t))
        monkeypatch.setattr(kb.time, "time", lambda: base + 302)
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Verifier found issue two")
        conn.execute(
            "UPDATE task_runs SET verdict = 'REQUEST_CHANGES' "
            "WHERE task_id = ? AND outcome = 'blocked' AND verdict IS NULL",
            (t,),
        )

        monkeypatch.setattr(kb.time, "time", lambda: base + 603)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == [(t, 2)]
        row = conn.execute(
            "SELECT status, auto_retry_count, assignee FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["auto_retry_count"] == 2
        assert row["assignee"] == kb.AUTO_RETRY_ESCALATION_PROFILE



def test_dispatch_auto_retry_second_attempt_escalates(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="research")
        conn.execute("UPDATE tasks SET auto_retry_count = 1 WHERE id = ?", (t,))
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="tool crashed")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == [(t, 2)]
        row = conn.execute(
            "SELECT status, auto_retry_count, assignee, model_override FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["auto_retry_count"] == 2
        assert row["assignee"] == kb.AUTO_RETRY_ESCALATION_PROFILE
        assert row["model_override"] == kb.AUTO_RETRY_ESCALATION_MODEL
        event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retried"][-1]
        assert event.payload["escalated"] is True
        assert event.payload["model_override"] == kb.AUTO_RETRY_ESCALATION_MODEL



def test_dispatch_auto_retry_stops_after_limit(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="alice")
        conn.execute("UPDATE tasks SET auto_retry_count = 2 WHERE id = ?", (t,))
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="still broken")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        assert kb.get_task(conn, t).status == "blocked"
        assert [e.kind for e in kb.list_events(conn, t)].count("auto_retry_exhausted") == 1



def test_dispatch_auto_retry_leaves_question_blocks_untouched(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Which credential should I use?")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        assert kb.get_task(conn, t).status == "blocked"
        event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retry_skipped"][-1]
        assert event.payload["blocked_kind"] == "operator_question"


def test_dispatch_auto_retry_leaves_secret_and_irreversible_blocks_untouched(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    reasons = [
        "Need secret token before continuing",
        "Please approve git push to origin/main",
        "Requires deploy after migration",
        "Need DB ALTER TABLE decision",
        "Freigabe zum Löschen fehlt",
    ]
    with kb.connect_closing() as conn:
        task_ids = []
        for reason in reasons:
            t = kb.create_task(conn, title=f"blocked {reason}", assignee="alice")
            kb.claim_task(conn, t)
            kb.block_task(conn, t, reason=reason)
            task_ids.append(t)

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        for t in task_ids:
            task = kb.get_task(conn, t)
            assert task is not None
            assert task.status == "blocked"
            event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retry_skipped"][-1]
            assert event.payload["blocked_kind"] == "operator_question"



def test_dispatch_auto_retry_respects_failure_breaker(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="crashy")
        conn.execute("UPDATE tasks SET consecutive_failures = 3 WHERE id = ?", (t,))

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(
            conn, auto_retry_blocked=True, failure_limit=3, max_spawn=0,
        )

        assert res.auto_retried_blocked == []
        assert kb.get_task(conn, t).status == "blocked"
        event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retry_skipped"][-1]
        assert event.payload["reason"] == "failure_limit"



def test_dispatch_auto_retry_completes_when_result_comment_arrived_after_block(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="research")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="MCP unreachable")
        monkeypatch.setattr(kb.time, "time", lambda: base + 60)
        kb.add_comment(conn, t, "research", "RESULT: full answer delivered here")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        task = kb.get_task(conn, t)
        assert task.status == "done"
        assert "full answer" in (task.result or "")
        event = [e for e in kb.list_events(conn, t) if e.kind == "auto_retry_completed"][-1]
        assert event.payload["source"] == "result_comment"


def test_dispatch_auto_retry_result_comment_does_not_wait_for_backoff(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked", assignee="research")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="MCP unreachable")
        monkeypatch.setattr(kb.time, "time", lambda: base + 60)
        kb.add_comment(conn, t, "research", "RESULT: complete answer arrived fast")

        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == []
        task = kb.get_task(conn, t)
        assert task is not None
        assert task.status == "done"
        assert "complete answer" in (task.result or "")



# ---------------------------------------------------------------------------
# Silent-block guard (SILENT-BLOCK-GUARD-S1): escalate_silent_blocks_sweep +
# silent_block_task_ids — every *settled* block surfaces an operator_escalation
# (AC-1) while transient self-healing retries stay silent (AC-2).
# ---------------------------------------------------------------------------

def _operator_escalations(conn, task_id):
    return [
        e for e in kb.list_events(conn, task_id)
        if e.kind == kb.OPERATOR_ESCALATION_EVENT
    ]


def test_silent_block_sweep_escalates_operator_question_block(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="needs op", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Which credential should I use?")
        # operator_question → settled, no escalation yet → silent
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        assert _operator_escalations(conn, t) == []

        res = kb.escalate_silent_blocks_sweep(conn, now=base)

        assert [e["task_id"] for e in res["escalated"]] == [t]
        assert len(_operator_escalations(conn, t)) == 1
        # silent set drained + idempotent re-run adds nothing
        assert kb.silent_block_task_ids(conn, now=base) == []
        kb.escalate_silent_blocks_sweep(conn, now=base)
        assert len(_operator_escalations(conn, t)) == 1


def test_silent_block_sweep_skips_transient_retryable_block(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="transient", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="transient MCP unavailable")
        # within retry budget + recent → the auto-retry lane is still on it
        assert kb.silent_block_task_ids(conn, now=base) == []
        res = kb.escalate_silent_blocks_sweep(conn, now=base)
        assert res["escalated"] == []
        assert _operator_escalations(conn, t) == []


def test_silent_block_sweep_escalates_when_retry_budget_exhausted(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="exhausted", assignee="alice")
        conn.execute(
            "UPDATE tasks SET auto_retry_count = ? WHERE id = ?",
            (kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT, t),
        )
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="still broken")
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        kb.escalate_silent_blocks_sweep(conn, now=base)
        assert len(_operator_escalations(conn, t)) == 1


def test_silent_block_sweep_escalates_block_without_run(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="parked", assignee="alice")
        # raw flip to blocked, no blocked run (mirrors contract/integration park)
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,))
        conn.commit()
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        kb.escalate_silent_blocks_sweep(conn, now=base)
        assert len(_operator_escalations(conn, t)) == 1


def test_silent_block_sweep_escalates_transient_past_grace(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A retryable block inside budget but blocked far longer than self-heal
    could take (lane disabled/stuck) must still surface — the guarantee holds
    independent of the auto_retry_blocked config flag."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="stale", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="transient MCP unavailable")
        grace = kb._self_heal_grace_seconds(
            kb.DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS,
            kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT,
        )
        # still inside grace → transient, not surfaced
        assert kb.silent_block_task_ids(conn, now=base + grace) == []
        # past grace → settled, surfaced
        assert kb.silent_block_task_ids(conn, now=base + grace + 1) == [t]
        kb.escalate_silent_blocks_sweep(conn, now=base + grace + 1)
        assert len(_operator_escalations(conn, t)) == 1


def test_silent_block_sweep_does_not_re_escalate_existing(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="already", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Which path?")
        kb._append_event(conn, t, kb.OPERATOR_ESCALATION_EVENT, {"why_now": "x"})
        conn.commit()
        assert kb.silent_block_task_ids(conn, now=base) == []
        res = kb.escalate_silent_blocks_sweep(conn, now=base)
        assert res["escalated"] == []
        assert len(_operator_escalations(conn, t)) == 1


def test_silent_block_sweep_writes_inline_heiler_classification(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """ESCALATION-INLINE-CLASSIFY-S1 (defense-in-depth): the silent-block sweep
    pairs a heiler_classification AT the escalation site, in the same write_txn,
    so coverage is complete the instant the escalation is written — no separate
    classify_escalations_sweep poll required. Exactly one classification,
    referencing the escalation event, tagged with the inline silent-block
    source, with a belegter (signal-source) evidence reference, not a guess
    (AC-2)."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="classify", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Which credential?")
        # Only the silent-block sweep runs — deliberately NOT the classify sweep.
        kb.escalate_silent_blocks_sweep(conn, now=base)
        esc = _escalation_event(conn, t)
        heilers = _heiler_events(conn, t)

    assert len(heilers) == 1
    assert heilers[0].payload["escalation_event_id"] == esc.id
    assert heilers[0].payload["source"] == kb.HEILER_SOURCE_SILENT_BLOCK
    assert heilers[0].payload["class"] in kb.HEILER_CLASSES
    assert heilers[0].payload["blocked"] is True
    assert heilers[0].payload["evidence"].get("signal_source")


def test_silent_block_sweep_inline_matches_sweep_and_sweep_skips(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """The inline class is byte-identical to what the backfill sweep would
    derive from the same persisted escalation payload (defense-in-depth, NOT
    divergence), and classify_escalations_sweep then adds nothing because the
    escalation is already paired."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="classify", assignee="alice")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Which credential?")
        kb.escalate_silent_blocks_sweep(conn, now=base)
        esc = _escalation_event(conn, t)
        inline = _heiler_events(conn, t)[0]
        expected_class, _ = kb._classify_escalation_payload(esc.payload)

        summary = kb.classify_escalations_sweep(conn, now=base)
        heilers = _heiler_events(conn, t)

    assert inline.payload["class"] == expected_class
    assert summary["classified"] == []
    assert len(heilers) == 1


def test_silent_block_sweep_carries_real_run_outcome(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """HEILER-SILENTBLOCK-REASON-FIDELITY-S1: a block settled via a non-blocked
    path (budget exhaustion) has NO blocked run, so the old payload sent
    trigger_outcome='blocked' + an empty error -> real-bug default. The sweep now
    carries the genuine last *ended* run outcome + message, so the operator sees
    the real reason and the classifier lands it honestly (capacity, not the
    real-bug default)."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="budget", assignee="alice")
        kb.claim_task(conn, t)
        # close the run as budget-exhausted (NOT 'blocked') then flip to blocked,
        # mirroring the iteration-budget park: no blocked run for the lane.
        with kb.write_txn(conn):
            kb._end_run(
                conn, t, outcome="iteration_budget_exhausted",
                status="iteration_budget_exhausted",
                summary="iteration budget exhausted; continuation limit "
                        "exhausted (60/60)",
            )
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,))
        conn.commit()
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        kb.escalate_silent_blocks_sweep(conn, now=base)
        esc = _escalation_event(conn, t)
        heiler = _heiler_events(conn, t)[0]

    assert esc.payload["evidence"]["trigger_outcome"] == "iteration_budget_exhausted"
    assert "iteration_budget_exhausted" in esc.payload["why_now"]
    # the real run message is carried, not the old empty string
    assert "iteration budget exhausted" in esc.payload["evidence"]["last_error"]
    assert heiler.payload["class"] == kb.HEILER_CLASS_CAPACITY


def test_silent_block_sweep_classifies_missing_spec_block_as_bad_spec(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A settled block whose reason is a spec gap classifies bad-spec, not the
    real-bug default — the dominant live silent-block mislabel: a real
    block-error IS carried, the classifier just lacked the spec-gap signal."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="vague", assignee="alice")
        conn.execute(
            "UPDATE tasks SET auto_retry_count = ? WHERE id = ?",
            (kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT, t),
        )
        kb.claim_task(conn, t)
        kb.block_task(
            conn, t,
            reason="No actionable implementation spec: title is too vague",
        )
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        kb.escalate_silent_blocks_sweep(conn, now=base)
        heiler = _heiler_events(conn, t)[0]

    assert heiler.payload["class"] == kb.HEILER_CLASS_BAD_SPEC


def test_silent_block_sweep_classifies_superseded_block_as_operator_intent(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """HEILER-CLASSIFY-SIGNAL-GAP-S2: a settled block whose reason records a
    deliberate operator supersede is not a product defect — it classifies
    operator-intent, not the real-bug default (live: t_2491b29e, reason
    'Superseded: operator requested direct Claude CLI review instead of
    Kanban reviewer.')."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="superseded review", assignee="alice")
        conn.execute(
            "UPDATE tasks SET auto_retry_count = ? WHERE id = ?",
            (kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT, t),
        )
        kb.claim_task(conn, t)
        kb.block_task(
            conn, t,
            reason="Superseded: operator requested direct Claude CLI review "
                   "instead of Kanban reviewer.",
        )
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        kb.escalate_silent_blocks_sweep(conn, now=base)
        esc = _escalation_event(conn, t)
        heiler = _heiler_events(conn, t)[0]

    assert esc.payload["evidence"]["last_error"].startswith("Superseded:")
    assert heiler.payload["class"] == kb.HEILER_CLASS_OPERATOR_INTENT


def test_silent_block_sweep_completed_outcome_avoids_default_bucket(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """HEILER-CLASSIFY-SIGNAL-GAP-S2: a settled block parked via a raw status
    flip AFTER a green run (release-gate park: t_76401275/t_6931affd) has no
    blocked run, so the escalation falls back to the completed run's summary
    — and a passing run is not a product defect, so it must not land in the
    real-bug default."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="release gate park", assignee="verifier")
        kb.claim_task(conn, t)
        with kb.write_txn(conn):
            kb._end_run(
                conn, t, outcome="completed", status="completed",
                summary="release gate green after 0 fixer attempt(s)",
            )
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,))
        conn.commit()
        assert kb.silent_block_task_ids(conn, now=base) == [t]
        kb.escalate_silent_blocks_sweep(conn, now=base)
        esc = _escalation_event(conn, t)
        heiler = _heiler_events(conn, t)[0]

    assert esc.payload["evidence"]["trigger_outcome"] == "completed"
    assert "release gate green" in esc.payload["evidence"]["last_error"]
    assert heiler.payload["class"] == kb.HEILER_CLASS_OPERATOR_INTENT


def test_silent_block_sweep_carves_out_strategist_meta_task(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """STRATEGIST-META-CARVEOUT: a blocked strategist-cron task (the loop's own
    output) is NOT swept — so the self-improvement loop never reads its own
    parked proposal back as a real-bug product-defect signal. A real code task
    blocked the same way IS still surfaced (AC-2: carve-out strictly scoped to
    created_by=strategist-cron, not real code tasks)."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    with kb.connect_closing() as conn:
        meta = kb.create_task(
            conn, title="strategist proposal", assignee="alice",
            created_by=kb.STRATEGIST_CREATED_BY,
        )
        kb.claim_task(conn, meta)
        kb.block_task(conn, meta, reason="Which lever should I pull?")
        real = kb.create_task(
            conn, title="real code task", assignee="bob", created_by="user",
        )
        kb.claim_task(conn, real)
        kb.block_task(conn, real, reason="Which credential should I use?")

        ids = kb.silent_block_task_ids(conn, now=base)
        assert meta not in ids
        assert real in ids

        res = kb.escalate_silent_blocks_sweep(conn, now=base)

        meta_operator_escalations = _operator_escalations(conn, meta)
        meta_heiler_events = _heiler_events(conn, meta)
        real_operator_escalations = _operator_escalations(conn, real)

    assert meta not in [e["task_id"] for e in res["escalated"]]
    assert meta_operator_escalations == []
    assert meta_heiler_events == []
    # the real code task is untouched by the carve-out — still surfaced
    assert len(real_operator_escalations) == 1


def test_dispatch_max_spawn_counts_existing_running_tasks(
    kanban_home, all_assignees_spawnable
):
    """max_spawn is a live concurrency cap, not a per-tick spawn cap.

    Without counting tasks already in ``running``, every dispatcher tick can
    launch up to ``max_spawn`` more workers while previous workers are still
    alive. Long-running boards then accumulate unbounded worker subprocesses.
    """
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect_closing() as conn:
        running_a = kb.create_task(conn, title="running-a", assignee="alice")
        running_b = kb.create_task(conn, title="running-b", assignee="bob")
        ready = kb.create_task(conn, title="ready", assignee="carol")
        kb.claim_task(conn, running_a)
        kb.claim_task(conn, running_b)

        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=2)

        assert res.spawned == []
        assert spawns == []
        assert kb.get_task(conn, ready).status == "ready"


def test_dispatch_max_spawn_fills_remaining_capacity(
    kanban_home, all_assignees_spawnable
):
    """When below cap, dispatch only fills available worker slots."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect_closing() as conn:
        running = kb.create_task(conn, title="running", assignee="alice")
        ready_a = kb.create_task(conn, title="ready-a", assignee="bob")
        ready_b = kb.create_task(conn, title="ready-b", assignee="carol")
        kb.claim_task(conn, running)

        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=2)

        assert len(res.spawned) == 1
        assert spawns == [ready_a]
        assert kb.get_task(conn, ready_a).status == "running"
        assert kb.get_task(conn, ready_b).status == "ready"


def test_dispatch_dry_run_max_spawn_counts_would_be_spawns(
    kanban_home, all_assignees_spawnable
):
    """Dry-run dispatch must stop after the max_spawn would-be spawns."""
    with kb.connect_closing() as conn:
        first = kb.create_task(conn, title="first", assignee="alice")
        second = kb.create_task(conn, title="second", assignee="bob")
        third = kb.create_task(conn, title="third", assignee="carol")

        res = kb.dispatch_once(conn, dry_run=True, max_spawn=1)

        assert res.spawned == [(first, "alice", "")]
        assert kb.get_task(conn, first).status == "ready"
        assert kb.get_task(conn, second).status == "ready"
        assert kb.get_task(conn, third).status == "ready"


def test_dispatch_max_in_progress_with_max_spawn_fills_remaining_capacity(
    kanban_home, all_assignees_spawnable
):
    """max_in_progress and max_spawn combine as live concurrency caps."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect_closing() as conn:
        running = kb.create_task(conn, title="running", assignee="alice")
        ready_a = kb.create_task(conn, title="ready-a", assignee="bob")
        ready_b = kb.create_task(conn, title="ready-b", assignee="carol")
        ready_c = kb.create_task(conn, title="ready-c", assignee="alice")
        kb.claim_task(conn, running)

        res = kb.dispatch_once(
            conn,
            spawn_fn=fake_spawn,
            max_in_progress=3,
            max_spawn=10,
        )

        assert len(res.spawned) == 2
        assert spawns == [ready_a, ready_b]
        assert kb.get_task(conn, ready_a).status == "running"
        assert kb.get_task(conn, ready_b).status == "running"
        assert kb.get_task(conn, ready_c).status == "ready"


def test_dispatch_reclaims_stale_before_spawning(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="alice")
        kb.claim_task(conn, t)
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 1, t),
        )
        res = kb.dispatch_once(conn, dry_run=True)
    assert res.reclaimed == 1


# ---------------------------------------------------------------------------
# Respawn guard (check_respawn_guard + dispatch_once integration)
# ---------------------------------------------------------------------------

def test_respawn_guard_none_on_fresh_task(kanban_home):
    """A fresh task with no failures or runs is not guarded."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="fresh", assignee="alice")
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_blocker_auth_on_quota_error(kanban_home):
    """'quota' in last_failure_error triggers blocker_auth."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="quota-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("API quota exceeded: rate limit hit", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_blocker_auth_on_auth_error(kanban_home):
    """'unauthorized' in last_failure_error triggers blocker_auth."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="auth-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("403 Forbidden: unauthorized to access resource", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_blocker_auth_on_authentication_error(kanban_home):
    """Full word 'Authentication' triggers blocker_auth (regex covers auth\\w*)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="authn-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("Authentication failed: invalid credentials", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_blocker_auth_on_authorization_error(kanban_home):
    """Full word 'authorization' triggers blocker_auth (regex covers auth\\w*)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="authz-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("authorization denied for scope repo", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_recent_success(kanban_home):
    """A completed run within the guard window triggers recent_success."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="already-done", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 120, now - 60),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "recent_success"


def test_respawn_guard_newer_timeout_supersedes_recent_success(kanban_home):
    """A newer failed attempt must clear the prior success guard.

    Regression evidence from the live board: a task had earlier completed runs,
    then a newer timeout requeued it to ready; dispatch still emitted
    respawn_guarded/recent_success and left the task idle.
    """
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="timeout-after-success", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 600, now - 540),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'failed', 'timed_out', ?, ?)",
            (t, now - 120, now - 60),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_rejected_verdict_allows_fix_run(kanban_home):
    """K3 regression: a verifier REQUEST_CHANGES on the latest run invalidates
    recent_success — the review happened and DEMANDED a fix run. Without this
    the CommandHome inline-resolve (unblock + tick) silently stalls for the
    full success window. An APPROVED verdict keeps the guard."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="rejected-task", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'alice', 'review', 'completed', ?, ?)",
            (t, now - 240, now - 180),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, verdict, started_at, ended_at) "
            "VALUES (?, 'verifier', 'done', 'completed', 'REQUEST_CHANGES', ?, ?)",
            (t, now - 120, now - 60),
        )
        assert kb.check_respawn_guard(conn, t) is None

        # Control: APPROVED on the latest run keeps recent_success.
        t2 = kb.create_task(conn, title="approved-task", assignee="alice")
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'alice', 'review', 'completed', ?, ?)",
            (t2, now - 240, now - 180),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, verdict, started_at, ended_at) "
            "VALUES (?, 'verifier', 'done', 'completed', 'APPROVED', ?, ?)",
            (t2, now - 120, now - 60),
        )
        assert kb.check_respawn_guard(conn, t2) == "recent_success"


def test_respawn_guard_stale_success_not_guarded(kanban_home):
    """A completed run outside the guard window does not block re-spawn."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="old-done", assignee="alice")
        old_end = int(time.time()) - kb._RESPAWN_GUARD_SUCCESS_WINDOW - 60
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, old_end - 300, old_end),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_active_pr_in_comment(kanban_home):
    """A GitHub PR URL in a recent comment triggers active_pr."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        kb.add_comment(
            conn, t, "worker",
            "PR created: https://github.com/totemx-AI/subsidysmart/pull/42",
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "active_pr"


def test_respawn_guard_old_pr_comment_not_guarded(kanban_home):
    """A GitHub PR URL in a comment older than the PR window does not block."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="old-pr", assignee="alice")
        old_ts = int(time.time()) - kb._RESPAWN_GUARD_PR_WINDOW - 60
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'worker', "
            "'PR: https://github.com/totemx-AI/subsidysmart/pull/10', ?)",
            (t, old_ts),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_dispatch_respawn_guard_defers_auth_error_without_auto_block(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once defers (does NOT auto-block) a ready task whose last
    error is a blocker_auth.

    The old behaviour auto-blocked on first occurrence, which was too
    aggressive: a transient 429 rate-limit (which typically clears in
    seconds to minutes) would end up requiring manual unblock. The new
    behaviour defers the spawn this tick; the task stays in ``ready``
    and gets another chance next tick. If the auth error genuinely
    persists, the existing ``consecutive_failures`` circuit breaker
    will auto-block via the normal failure-limit path.
    """
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="quota-storm", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("rate limit exceeded: 429 Too Many Requests", t),
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    # Critical: task is NOT auto-blocked on first occurrence.
    assert t not in res.auto_blocked, (
        f"blocker_auth should defer, not auto-block on first occurrence; "
        f"got auto_blocked={res.auto_blocked!r}"
    )
    # It IS recorded as respawn_guarded with the reason.
    assert (t, "blocker_auth") in res.respawn_guarded, (
        f"expected (task_id, 'blocker_auth') in respawn_guarded; "
        f"got {res.respawn_guarded!r}"
    )
    # And it's NOT spawned this tick.
    assert t not in spawned_ids
    # Status stays ``ready`` so a future tick (or operator action) can
    # retry without manual unblock.
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_respawn_guard_skips_recent_success(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once skips (but does not block) a task with a recent completed run."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="recent-winner", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 300, now - 60),
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "recent_success") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "ready"  # not blocked, just skipped


def test_dispatch_respawn_guard_skips_active_pr(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once skips (but does not block) a task with an active PR comment."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        kb.add_comment(
            conn, t, "worker",
            "Opened https://github.com/totemx-AI/subsidysmart/pull/99",
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "active_pr") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_respawn_guard_dry_run_no_auto_block(
    kanban_home, all_assignees_spawnable
):
    """In dry_run mode, blocker_auth tasks are recorded in respawn_guarded (not auto-blocked)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="dry-quota", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("quota exceeded", t),
        )
        res = kb.dispatch_once(conn, dry_run=True)

    assert (t, "blocker_auth") in res.respawn_guarded
    assert t not in res.auto_blocked
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "ready"  # dry_run: no writes


def test_dispatch_respawn_guard_allows_clean_task(
    kanban_home, all_assignees_spawnable
):
    """A task with no guard triggers is spawned normally."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="clean-task", assignee="alice")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert t in spawned_ids
    assert not res.respawn_guarded
    assert t not in res.auto_blocked


def test_dispatch_respawn_guard_emits_event_for_skipped_task(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once emits a respawn_guarded task_event so operators can diagnose stuck-ready tasks."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="event-check", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 300, now - 60),
        )
        kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        events = kb.list_events(conn, t)

    kinds = [e.kind for e in events]
    assert "respawn_guarded" in kinds
    guarded_evt = next(e for e in events if e.kind == "respawn_guarded")
    # Event.payload is already parsed as a dict by list_events.
    assert isinstance(guarded_evt.payload, dict)
    assert guarded_evt.payload.get("reason") == "recent_success"


# ---------------------------------------------------------------------------
# G1 per-task cumulative input-token runaway guard (per_task_input_token_cap)
# ---------------------------------------------------------------------------

def _seed_input_token_run(conn, task_id, *, input_tokens, profile="alice"):
    """Insert a completed task_run stamped with ``input_tokens`` (K5a).

    The run is dated OUTSIDE the respawn-guard success window so the
    pre-existing ``recent_success`` guard does not interfere — the per-task
    token sum is age-independent (it spans ALL runs), so a stale run still
    counts toward the G1 cap while leaving the task otherwise spawnable."""
    end = int(time.time()) - kb._RESPAWN_GUARD_SUCCESS_WINDOW - 300
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, "
        "started_at, ended_at, input_tokens) "
        "VALUES (?, ?, 'done', 'completed', ?, ?, ?)",
        (task_id, profile, end - 300, end, input_tokens),
    )


def test_dispatch_per_task_input_token_guard_parks_over_threshold(
    kanban_home, all_assignees_spawnable
):
    """AC1: when the cumulative input_tokens across all runs exceeds
    ``per_task_input_token_cap`` the task is PARKED (blocked, not re-spawned),
    bucketed in ``budget_runaway_parked``, and gets both a
    ``budget_runaway_parked`` event (with the token sum) and an
    ``operator_escalation`` event."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="runaway", assignee="alice")
        # Two runs that sum over the 1000-token cap (700 + 600 = 1300).
        _seed_input_token_run(conn, t, input_tokens=700)
        _seed_input_token_run(conn, t, input_tokens=600)
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, per_task_input_token_cap=1000
        )

    # Not spawned this tick.
    assert t not in spawned_ids
    # Bucketed with the summed input tokens.
    assert (t, 1300) in res.budget_runaway_parked
    # Hard-parked to blocked (not left advisory-ready).
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "blocked"
        events = kb.list_events(conn, t)
    kinds = [e.kind for e in events]
    assert "budget_runaway_parked" in kinds
    assert "operator_escalation" in kinds
    parked_evt = next(e for e in events if e.kind == "budget_runaway_parked")
    assert parked_evt.payload.get("input_token_sum") == 1300
    assert parked_evt.payload.get("cap") == 1000


def test_dispatch_per_task_input_token_guard_under_threshold_spawns(
    kanban_home, all_assignees_spawnable
):
    """AC2: a task whose cumulative input_tokens stay under the cap is
    untouched — spawned normally, not parked, no runaway event."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="frugal", assignee="alice")
        _seed_input_token_run(conn, t, input_tokens=400)
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, per_task_input_token_cap=1000
        )

    assert t in spawned_ids
    assert not res.budget_runaway_parked
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "running"
        kinds = [e.kind for e in kb.list_events(conn, t)]
    assert "budget_runaway_parked" not in kinds


def test_dispatch_per_task_input_token_guard_inert_when_cap_none(
    kanban_home, all_assignees_spawnable
):
    """AC3: with the cap unset (None — the dispatch_once default) the guard is
    inert even for a task far over any sane threshold."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="uncapped", assignee="alice")
        _seed_input_token_run(conn, t, input_tokens=9_000_000)
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)  # no cap kwarg

    assert t in spawned_ids
    assert not res.budget_runaway_parked
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "running"


def test_dispatch_per_task_input_token_guard_inert_when_cap_zero(
    kanban_home, all_assignees_spawnable
):
    """AC3: an explicit cap of 0 disables the guard (same as None)."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="zero-cap", assignee="alice")
        _seed_input_token_run(conn, t, input_tokens=9_000_000)
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, per_task_input_token_cap=0
        )

    assert t in spawned_ids
    assert not res.budget_runaway_parked
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "running"


def test_dispatch_per_task_input_token_guard_surfaces_in_decision_queue(
    kanban_home, all_assignees_spawnable
):
    """A parked runaway uses the operator_escalation path, so it appears in the
    decision_queue (Sprint 2 4B wired operator_escalation → decision_queue)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="runaway-q", assignee="alice")
        _seed_input_token_run(conn, t, input_tokens=2_500_000)
        kb.dispatch_once(
            conn, spawn_fn=lambda task, ws: None, per_task_input_token_cap=1_000_000
        )
        dq = kb.decision_queue(conn)

    ids = [item["task_id"] for item in dq.get("decisions", [])]
    assert t in ids
    item = next(i for i in dq["decisions"] if i["task_id"] == t)
    assert item["kind"] == "operator_escalation"
    assert item["operator_escalation"]["evidence"]["input_token_sum"] == 2_500_000


def test_dispatch_per_task_input_token_guard_skips_null_token_runs(
    kanban_home, all_assignees_spawnable
):
    """Runs with NULL input_tokens (no usage data) count as 0 and never trip
    the guard on their own — fail-soft like the C1 budget caps."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="no-usage", assignee="alice")
        _seed_input_token_run(conn, t, input_tokens=None)
        _seed_input_token_run(conn, t, input_tokens=None)
        res = kb.dispatch_once(
            conn, spawn_fn=fake_spawn, per_task_input_token_cap=1000
        )

    assert t in spawned_ids
    assert not res.budget_runaway_parked


def test_per_task_input_token_cap_config_default_is_two_million():
    """The config default ships the guard ON at 2_000_000 input tokens."""
    from hermes_cli.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["kanban"]["per_task_input_token_cap"] == 2_000_000


def test_dispatch_nonspawnable_emits_one_diagnostic_event(kanban_home, monkeypatch):
    """A ready task whose assignee is not a runnable profile leaves a single
    ``nonspawnable`` event so the skip is visible on the board timeline,
    instead of the task silently rotting in ``ready`` with no diagnosis.
    Deduped (F2 pattern): a second dispatch tick does not duplicate it."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="visual check", assignee="ui-verifier")
        kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        events = kb.list_events(conn, t)

    kinds = [e.kind for e in events]
    assert kinds.count("nonspawnable") == 1
    evt = next(e for e in events if e.kind == "nonspawnable")
    assert isinstance(evt.payload, dict)
    assert evt.payload.get("assignee") == "ui-verifier"


def test_dispatch_nonspawnable_misassignment_escalates_to_operator(kanban_home, monkeypatch):
    """A ready task whose assignee is neither a profile nor a known terminal
    lane raises ONE operator escalation (decision-inbox + Discord path)
    alongside the diagnostic event — mis-assignments must not rot silently
    (2026-06 finding: assignee ``ui-verifier`` sat in ready with no alarm).

    The dedup must survive the REAL gateway tick, which interleaves
    ``classify_escalations_sweep`` after every dispatch (review finding
    2026-07-02: a latest-event-kind guard alone re-fires once the sweep
    appends its classification, paging Discord every tick). Hence the
    escalation classifies INLINE (one paired ``heiler_classification``)
    and its dedup is durable across arbitrary later events."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="visual check", assignee="ui-verifier")
        for _ in range(3):
            kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
            kb.classify_escalations_sweep(conn)
        events = kb.list_events(conn, t)

    kinds = [e.kind for e in events]
    assert kinds.count("operator_escalation") == 1
    assert kinds.count(kb.HEILER_CLASSIFICATION_EVENT) == 1
    assert kinds.count("nonspawnable") == 1
    esc = next(e for e in events if e.kind == "operator_escalation")
    assert esc.payload["evidence"]["trigger_outcome"] == "nonspawnable_assignee"
    assert esc.payload["task"]["assignee"] == "ui-verifier"
    diag = next(e for e in events if e.kind == "nonspawnable")
    assert diag.payload.get("escalated") is True


def test_nonspawnable_escalation_does_not_exempt_later_silent_block(
    kanban_home, monkeypatch
):
    """A ready-stage mis-assignment escalation must NOT count as "this task's
    block was escalated": after the operator fixes the assignee and the task
    later genuinely silent-blocks, the silent-block guard still catches it."""
    from hermes_cli import profiles

    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="misassigned then blocked", assignee="ui-verifier")
        kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        assert len(_operator_escalations(conn, t)) == 1
        # The ready-stage escalation parks nothing: it must not register as an
        # ACTIVE escalation, or every later unrelated block of this task would
        # be held out of the self-heal lanes forever (resolved by reassign,
        # never by an ``unblocked`` event).
        assert kb._operator_escalation_is_active(conn, t) is False

        # Operator "fixes" the assignee; later the task blocks on a question.
        monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="Which credential should I use?")
        assert kb.silent_block_task_ids(conn, now=base) == [t]


def test_dispatch_nonspawnable_terminal_lane_stays_quiet(kanban_home, monkeypatch):
    """Known terminal lanes (pulled via ``claim_task`` by interactive
    terminals) are intentionally non-spawnable: diagnostic event only,
    NO operator escalation — otherwise every orion-cc task would page the
    operator."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="terminal work", assignee="orion-cc")
        kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        events = kb.list_events(conn, t)

    kinds = [e.kind for e in events]
    assert "operator_escalation" not in kinds
    assert kinds.count("nonspawnable") == 1
    diag = next(e for e in events if e.kind == "nonspawnable")
    assert diag.payload.get("escalated") is False


def test_terminal_lane_allowlist_env_extends_default(monkeypatch):
    """HERMES_KANBAN_TERMINAL_LANES EXTENDS the built-in default (it must not
    replace it — an operator adding one lane would silently de-list orion-cc)."""
    monkeypatch.setenv("HERMES_KANBAN_TERMINAL_LANES", "my-lane, other ,")
    lanes = kb._terminal_lane_assignees()
    assert {"my-lane", "other"} <= lanes
    assert {"orion-cc", "orion-research"} <= lanes
    monkeypatch.delenv("HERMES_KANBAN_TERMINAL_LANES")
    assert "orion-cc" in kb._terminal_lane_assignees()


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------

def test_scratch_workspace_created_under_hermes_home(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x")
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)
    assert ws.exists()
    assert ws.is_dir()
    assert "kanban" in str(ws)


def test_dir_workspace_honors_given_path(kanban_home, tmp_path):
    target = tmp_path / "my-vault"
    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn, title="biz", workspace_kind="dir", workspace_path=str(target)
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)
    assert ws == target
    assert ws.exists()


def test_worktree_workspace_repo_root_anchor_materializes_linked_worktree(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn, title="ship", workspace_kind="worktree", workspace_path=str(repo)
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)

    expected = repo / ".worktrees" / t
    assert ws == expected
    assert ws.exists()
    repo_common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ws_common = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ws_common == repo_common
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {expected}" in listed
    assert f"branch refs/heads/wt/{t}" in listed


def test_worktree_no_path_anchors_on_board_default_workdir(kanban_home, tmp_path):
    """A worktree task created with no explicit path inherits the board's
    default_workdir as its anchor and materializes a per-task linked worktree
    at ``<repo>/.worktrees/<id>`` — NOT the dispatcher's CWD, and NOT the
    shared default_workdir verbatim (which would collapse every task into one
    directory)."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    kb.create_board("wt-default-board", default_workdir=str(repo))
    with kb.connect(board="wt-default-board") as conn:
        t = kb.create_task(
            conn, title="ship", workspace_kind="worktree", board="wt-default-board"
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task, board="wt-default-board")

    expected = repo / ".worktrees" / t
    assert ws == expected
    assert ws.exists()
    assert ws != repo  # not the shared default verbatim


def test_worktree_no_path_no_board_default_raises(kanban_home, tmp_path, monkeypatch):
    """With neither an explicit workspace_path nor a board default_workdir,
    resolution fails loudly pointing at default_workdir / worktree:<path> —
    rather than silently materializing under the dispatcher's CWD (the old
    behavior that scattered worktrees under whatever dir launched the
    gateway)."""
    # Park the dispatcher CWD inside a real git repo so the OLD cwd-anchored
    # code would have "succeeded" — proving the new code does NOT use cwd.
    decoy_repo = tmp_path / "decoy"
    _init_git_repo(decoy_repo)
    monkeypatch.chdir(decoy_repo)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="ship", workspace_kind="worktree")
        task = kb.get_task(conn, t)
        assert task is not None
        with pytest.raises(ValueError, match="default_workdir"):
            kb.resolve_workspace(task)


def test_worktree_workspace_explicit_target_materializes_linked_worktree(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    target = repo / ".worktrees" / "custom-task"
    branch = "wt/custom-task"
    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn,
            title="ship",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name=branch,
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)

    assert ws == target
    assert ws.exists()
    repo_common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ws_common = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ws_common == repo_common
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {target}" in listed
    assert f"branch refs/heads/{branch}" in listed


def test_dispatch_worktree_task_persists_materialized_workspace_and_branch(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    kb.create_board("worktree-board", default_workdir=str(repo))
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return None

    with kb.connect(board="worktree-board") as conn:
        tid = kb.create_task(
            conn,
            title="ship",
            assignee="sentinel",
            workspace_kind="worktree",
            board="worktree-board",
        )
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="worktree-board")
        task = kb.get_task(conn, tid)

    expected = repo / ".worktrees" / tid
    assert result.spawned == [(tid, "sentinel", str(expected))]
    assert spawns == [(tid, str(expected))]
    assert task is not None
    assert task.workspace_path == str(expected)
    assert task.branch_name == f"wt/{tid}"
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {expected}" in listed
    assert f"branch refs/heads/wt/{tid}" in listed


def test_dispatch_worktree_task_rerun_reuses_existing_linked_worktree_and_branch(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    kb.create_board("worktree-rerun-board", default_workdir=str(repo))
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return None

    with kb.connect(board="worktree-rerun-board") as conn:
        tid = kb.create_task(
            conn,
            title="ship",
            assignee="sentinel",
            workspace_kind="worktree",
            board="worktree-rerun-board",
        )
        first = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="worktree-rerun-board")
        first_task = kb.get_task(conn, tid)
        assert first_task is not None
        expected = repo / ".worktrees" / tid
        assert first_task.workspace_path == str(expected)
        assert first_task.branch_name == f"wt/{tid}"

        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (tid,),
        )
        conn.commit()

        second = kb.dispatch_once(conn, spawn_fn=fake_spawn, board="worktree-rerun-board")
        second_task = kb.get_task(conn, tid)

    assert first.spawned == [(tid, "sentinel", str(expected))]
    assert second.spawned == [(tid, "sentinel", str(expected))]
    assert spawns == [(tid, str(expected)), (tid, str(expected))]
    assert second_task is not None
    assert second_task.workspace_path == str(expected)
    actual_branch = subprocess.run(
        ["git", "-C", str(expected), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert actual_branch == f"wt/{tid}"
    assert second_task.branch_name == actual_branch
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert listed.count(f"worktree {expected}\n") == 1
    assert f"worktree {expected}/.worktrees/{tid}" not in listed
    assert f"branch refs/heads/{actual_branch}" in listed


# ---------------------------------------------------------------------------
# Scratch cleanup containment (#28818)
# ---------------------------------------------------------------------------

def test_cleanup_workspace_removes_managed_scratch_dir(kanban_home):
    """A scratch workspace under the kanban workspaces root is removed."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="scratchy")
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)
        assert ws.is_dir()
        kb.complete_task(conn, t, result="ok")
    assert not ws.exists(), "Hermes-managed scratch dir should be cleaned up"


def test_cleanup_workspace_refuses_path_outside_scratch_root(kanban_home, tmp_path):
    """A scratch task with a user path outside the workspaces root must NOT be deleted (#28818).

    Reproduces the data-loss vector where a board's ``default_workdir`` is set
    to a real source directory; tasks created without an explicit
    ``workspace_kind`` inherit ``scratch`` semantics, and the old cleanup path
    would ``shutil.rmtree`` the user's source tree on task completion.
    """
    real_source = tmp_path / "real-source"
    real_source.mkdir()
    (real_source / ".git").mkdir()
    (real_source / "README.md").write_text("important", encoding="utf-8")

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="ship")
        # Simulate the bad state directly: workspace_kind='scratch' (default)
        # but workspace_path pointing at the user's real source tree, which is
        # exactly what board.default_workdir produces when the task is created
        # without an explicit workspace_kind.
        conn.execute(
            "UPDATE tasks SET workspace_kind=?, workspace_path=? WHERE id=?",
            ("scratch", str(real_source), t),
        )
        conn.commit()
        kb.complete_task(conn, t, result="ok")

    assert real_source.exists(), "User source tree must not be deleted by scratch cleanup"
    assert (real_source / ".git").exists()
    assert (real_source / "README.md").read_text(encoding="utf-8") == "important"


def test_cleanup_workspace_honors_workspaces_root_env_override(tmp_path, monkeypatch):
    """``HERMES_KANBAN_WORKSPACES_ROOT`` extends the managed-scratch set.

    Worker subprocesses run with this env var injected by the dispatcher. The
    cleanup containment check must treat paths under it as managed even when
    they sit outside the active kanban home.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspaces_override = tmp_path / "ext-workspaces"
    workspaces_override.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(workspaces_override))
    kb.init_db()

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="ext")
        scratch_dir = workspaces_override / t
        scratch_dir.mkdir()
        conn.execute(
            "UPDATE tasks SET workspace_kind=?, workspace_path=? WHERE id=?",
            ("scratch", str(scratch_dir), t),
        )
        conn.commit()
        kb.complete_task(conn, t, result="ok")

    assert not scratch_dir.exists(), "Override-root scratch dir should be cleaned up"


# ---------------------------------------------------------------------------
# Deferred scratch cleanup for parent/child handoff (#33774)
# ---------------------------------------------------------------------------

def test_cleanup_workspace_deferred_while_child_active(kanban_home):
    """A scratch parent's workspace survives completion while a child is still active.

    The dependency chain (parents=[A]) must guarantee child B can read A's
    handoff artifacts. The old cleanup deleted A's scratch dir immediately on
    A's completion, before B ever ran.
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child")
        kb.link_tasks(conn, parent, child)  # child depends on parent
        p_task = kb.get_task(conn, parent)
        parent_ws = kb.resolve_workspace(p_task)
        kb.set_workspace_path(conn, parent, parent_ws)
        assert parent_ws.is_dir()
        # Parent completes; child is still 'todo' -> cleanup must be deferred.
        kb.complete_task(conn, parent, result="handoff written")

    assert parent_ws.exists(), (
        "Parent scratch workspace must survive while a linked child is active"
    )


def test_cleanup_workspace_swept_after_last_child_completes(kanban_home):
    """Once all children are terminal, the deferred parent scratch dir is removed."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child")
        kb.link_tasks(conn, parent, child)
        p_task = kb.get_task(conn, parent)
        parent_ws = kb.resolve_workspace(p_task)
        kb.set_workspace_path(conn, parent, parent_ws)
        # Give the child its own scratch dir too.
        c_task = kb.get_task(conn, child)
        child_ws = kb.resolve_workspace(c_task)
        kb.set_workspace_path(conn, child, child_ws)

        kb.complete_task(conn, parent, result="ok")
        assert parent_ws.exists(), "deferred while child active"

        # Child completes -> recompute promotes nothing new; the child's
        # cleanup sweep should now reap the parent's deferred workspace.
        kb.complete_task(conn, child, result="done")

    assert not parent_ws.exists(), (
        "Parent scratch workspace should be swept once all children are terminal"
    )
    assert not child_ws.exists(), "Child scratch workspace should be cleaned up too"


def test_dir_child_completion_unblocks_deferred_scratch_parent(kanban_home, tmp_path):
    """A non-scratch ('dir') child completing must still sweep its scratch parent.

    Regression for the gap where ``_cleanup_workspace`` returned early for a
    non-scratch task and never ran the parent sweep — leaking the parent's
    deferred scratch dir forever.
    """
    child_dir = tmp_path / "persistent-child"
    child_dir.mkdir()
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="scratch parent")
        child = kb.create_task(
            conn, title="dir child", workspace_kind="dir",
            workspace_path=str(child_dir),
        )
        kb.link_tasks(conn, parent, child)
        p_task = kb.get_task(conn, parent)
        parent_ws = kb.resolve_workspace(p_task)
        kb.set_workspace_path(conn, parent, parent_ws)

        kb.complete_task(conn, parent, result="handoff")
        assert parent_ws.exists(), "deferred while dir child active"

        kb.complete_task(conn, child, result="built")

    assert not parent_ws.exists(), (
        "A 'dir' child completing must trigger the parent scratch sweep"
    )
    assert child_dir.exists(), "Non-scratch 'dir' child workspace is never deleted"


def test_is_managed_scratch_path_accepts_per_board_workspaces(kanban_home, tmp_path):
    """Per-board scratch dirs under ``<kanban_home>/kanban/boards/<slug>/workspaces`` are managed."""
    board_scratch = kanban_home / "kanban" / "boards" / "my-board" / "workspaces" / "task-1"
    board_scratch.mkdir(parents=True)
    assert kb._is_managed_scratch_path(board_scratch)


def test_is_managed_scratch_path_rejects_real_source_tree(kanban_home, tmp_path):
    """A path outside any managed root (e.g. a user's repo) is NOT managed."""
    real = tmp_path / "code" / "my-project"
    real.mkdir(parents=True)
    assert not kb._is_managed_scratch_path(real)


def test_is_managed_scratch_path_rejects_kanban_metadata_subtrees(kanban_home):
    """Hermes' own DB/metadata/log subtrees under ``<kanban_home>/kanban`` are NOT managed.

    Regression guard for the Copilot finding on #28819: a scratch task whose
    ``workspace_path`` was mis-set to the kanban home, the logs dir, or a
    board's metadata dir (i.e. the board root itself, not its ``workspaces/``
    child) must be refused. Without this, the containment check would happily
    ``shutil.rmtree`` Hermes' DB/metadata/logs on task completion.
    """
    kanban_root = kanban_home / "kanban"
    kanban_root.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(kanban_root)

    logs_dir = kanban_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(logs_dir)

    board_root = kanban_root / "boards" / "my-board"
    board_root.mkdir(parents=True, exist_ok=True)
    # The board root itself is NOT a managed scratch dir — only the
    # ``workspaces/`` child (and its descendants) are.
    assert not kb._is_managed_scratch_path(board_root)

    # Sibling subtrees of ``workspaces/`` under a board (e.g. its kanban.db
    # or board.json living next to ``workspaces/``) are also not managed.
    board_logs = board_root / "logs"
    board_logs.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(board_logs)

    # Now create the board's workspaces dir and a task scratch dir under it —
    # the latter is the only thing the guard should allow.
    board_workspaces = board_root / "workspaces"
    board_workspaces.mkdir(parents=True, exist_ok=True)
    # The workspaces root itself is also NOT managed — deleting it would
    # wipe every task's scratch dir at once.
    assert not kb._is_managed_scratch_path(board_workspaces)
    task_dir = board_workspaces / "task-42"
    task_dir.mkdir(parents=True, exist_ok=True)
    assert kb._is_managed_scratch_path(task_dir)


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
        assert f"{task.body}\n\n## Knowledge pointers" in prompt
        assert prompt.index("## Knowledge pointers") < prompt.index(
            "Work in the current directory."
        )

    def test_claude_worker_comment_thread_caps_at_ctx_max(self, kanban_home, monkeypatch):
        """More than _CTX_MAX_COMMENTS comments → only the most-recent N are
        shown in full, with the same 'earlier comment(s) omitted' marker the
        Hermes-worker path emits (token cap parity)."""
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

        assert f"showing most recent {kb._CTX_MAX_COMMENTS}" in prompt
        # Oldest 3 dropped; newest retained.
        assert "comment-number-0\n" not in prompt
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


# ---------------------------------------------------------------------------
# latest_summary / latest_summaries — surface task_runs.summary handoffs
# ---------------------------------------------------------------------------

def test_latest_summary_returns_none_when_no_runs(kanban_home):
    """A freshly-created task has no runs and therefore no summary."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="fresh", assignee="alice")
        assert kb.latest_summary(conn, t) is None


def test_latest_summary_returns_summary_after_complete(kanban_home):
    """``complete_task(summary=...)`` is the canonical kanban-worker
    handoff; ``latest_summary`` must surface it so dashboards/CLI can
    render what the worker actually did."""
    handoff = "shipped 3 files, ran tests, opened PR #42"
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="work", assignee="alice")
        kb.complete_task(conn, t, summary=handoff)
        assert kb.latest_summary(conn, t) == handoff


def test_latest_summary_picks_newest_when_multiple_runs(kanban_home):
    """When a task has been re-run (block → unblock → complete), the
    newest run's summary wins. We unblock to take the task back to
    ``ready``, then complete a second time and verify the second
    summary surfaces."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="retry", assignee="alice")
        kb.complete_task(conn, t, summary="first attempt")
        # Move back to ready by direct SQL — block_task / unblock_task
        # paths require an active claim, but we just want a second run
        # row to exist with a later ended_at.
        conn.execute(
            "UPDATE tasks SET status='ready', completed_at=NULL WHERE id=?",
            (t,),
        )
        # Sleep 1s so the second run's ended_at is provably later than
        # the first (complete_task uses int(time.time())).
        time.sleep(1.05)
        kb.complete_task(conn, t, summary="second attempt — final")
        assert kb.latest_summary(conn, t) == "second attempt — final"


def test_latest_summary_skips_empty_string(kanban_home):
    """A run with an empty-string summary should not mask an earlier
    populated one — empty strings carry no information."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="t", assignee="alice")
        kb.complete_task(conn, t, summary="real handoff")
        # Inject a later run with empty summary directly. Workers
        # writing "" instead of None is a real shape we want to ignore.
        conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at, ended_at, "
            "outcome, summary) VALUES (?, 'done', ?, ?, 'completed', ?)",
            (t, int(time.time()) + 1, int(time.time()) + 2, ""),
        )
        conn.commit()
        assert kb.latest_summary(conn, t) == "real handoff"


def test_latest_summaries_batch_omits_tasks_without_summary(kanban_home):
    """``latest_summaries`` is the dashboard's N+1 escape hatch — it
    must return only entries for tasks that actually have a summary,
    keep the per-task latest, and accept an empty input gracefully."""
    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="a", assignee="alice")
        t2 = kb.create_task(conn, title="b", assignee="bob")
        t3 = kb.create_task(conn, title="c", assignee="carol")
        kb.complete_task(conn, t1, summary="alpha")
        kb.complete_task(conn, t3, summary="charlie")
        out = kb.latest_summaries(conn, [t1, t2, t3])
        assert out == {t1: "alpha", t3: "charlie"}
        # Empty input → empty dict, no SQL syntax error from "IN ()".
        assert kb.latest_summaries(conn, []) == {}



# ---------------------------------------------------------------------------
# NFS / network-filesystem fallback (see hermes_state.apply_wal_with_fallback)
# ---------------------------------------------------------------------------

def test_connect_falls_back_to_delete_on_locking_protocol(tmp_path, monkeypatch, caplog):
    """kanban_db.connect() must handle ``locking protocol`` on NFS/SMB.

    Without this fallback, the gateway's kanban dispatcher crashes every
    60s and the kanban migration (``consecutive_failures`` ADD COLUMN) is
    retried forever — which is what the real-world user report shows
    (see hermes-agent issue #22032).

    NOTE: We do NOT use the ``kanban_home`` fixture here because that
    fixture pre-initializes the DB via ``kb.init_db()`` — putting the
    file in WAL on disk. The Bug D safety guard now refuses to downgrade
    to DELETE when the on-disk header is already WAL, so testing the
    NFS-fallback path requires a truly-fresh DB file (NFS scenario in
    production: first connection of the first process ever to touch the
    file, where downgrading is safe because nobody else has WAL state
    yet).
    """
    import sqlite3 as _sqlite3
    from unittest.mock import patch as _patch

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Clear module cache so a fresh connect() is attempted
    kb._INITIALIZED_PATHS.clear()

    real_connect = _sqlite3.connect

    class _WalBlockingConnection(_sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if "journal_mode=wal" in sql.lower().replace(" ", ""):
                raise _sqlite3.OperationalError("locking protocol")
            return super().execute(sql, *args, **kwargs)

    def wal_blocking_connect(*args, **kwargs):
        return real_connect(
            *args, factory=_WalBlockingConnection, **kwargs
        )

    with _patch("hermes_cli.kanban_db.sqlite3.connect", side_effect=wal_blocking_connect):
        with caplog.at_level("WARNING", logger="hermes_state"):
            conn = kb.connect()

    # One fallback warning, naming kanban.db
    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "kanban.db" in r.getMessage()
    ]
    assert len(warnings) >= 1, (
        f"Expected a kanban.db WARNING, got: {[r.getMessage() for r in caplog.records]}"
    )

    # DB still usable end-to-end — create + list a task
    t = kb.create_task(conn, title="post-fallback task")
    tasks = kb.list_tasks(conn)
    assert any(row.id == t for row in tasks)
    conn.close()


def test_unlink_tasks_triggers_recompute_ready(kanban_home):
    """Regression test for issue #22459.

    Removing a dependency via unlink_tasks must immediately promote the child
    to ready when all remaining parents are done — same contract as
    complete_task and unblock_task.

    Before the fix, child stayed 'todo' indefinitely after unlink; only the
    next dispatcher tick or a manual 'hermes kanban recompute' would promote it.
    """
    with kb.connect_closing() as conn:
        # A is done.
        a = kb.create_task(conn, title="parent-done")
        kb.complete_task(conn, a)

        # C is running (not done) — blocks child B.
        c = kb.create_task(conn, title="parent-running")
        kb.claim_task(conn, c, claimer="worker:1")

        # B depends on both A (done) and C (running) → stays todo.
        b = kb.create_task(conn, title="child", parents=[a, c])
        assert kb.get_task(conn, b).status == "todo"

        # Remove the blocking dependency C → B.
        removed = kb.unlink_tasks(conn, c, b)
        assert removed is True

        # B's only remaining parent is A (done) → must be ready immediately.
        assert kb.get_task(conn, b).status == "ready", (
            "child should promote to ready immediately after unlink_tasks "
            "removes its last blocking dependency"
        )


def test_archive_task_does_not_satisfy_dependent_children(kanban_home):
    """Archiving a parent does not count as dependency completion."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="obsolete parent")
        child = kb.create_task(conn, title="child", parents=[parent])

        assert kb.get_task(conn, child).status == "todo"
        assert kb.archive_task(conn, parent) is True

        task = kb.get_task(conn, child)
        assert task is not None
        assert task.status == "todo"

# ---------------------------------------------------------------------------
# _add_column_if_missing / _migrate_add_optional_columns idempotency (#21708)
# ---------------------------------------------------------------------------

def test_add_column_if_missing_is_idempotent_on_race(kanban_home):
    """``_add_column_if_missing`` must swallow 'duplicate column name' errors.

    Regression for #21708: the kanban dispatcher opens the DB twice per tick
    (once via _tick_once_for_board, once via init_db's discard-and-reconnect
    path).  A second concurrent connection runs _migrate_add_optional_columns
    before the first one commits, so ALTER TABLE raises OperationalError with
    'duplicate column name: consecutive_failures'.  Without the idempotency
    guard that crashes the dispatcher on the first tick after every restart.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"
    )

    # First call adds the column — returns True.
    added = kb._add_column_if_missing(conn, "tasks", "extra_col", "extra_col TEXT")
    assert added is True
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "extra_col" in cols

    # Second call on same connection — column already exists — must return
    # False without raising, simulating the race the dispatcher hits.
    added_again = kb._add_column_if_missing(
        conn, "tasks", "extra_col", "extra_col TEXT"
    )
    assert added_again is False

    conn.close()


def test_migrate_add_optional_columns_tolerates_concurrent_migration(kanban_home):
    """Full _migrate_add_optional_columns must not raise when columns already
    exist (issue #21708 race window — two connections migrate concurrently)."""
    import sqlite3

    # Schema already in fully-migrated state (all optional columns present).
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            tenant TEXT,
            result TEXT,
            idempotency_key TEXT,
            branch_name TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            worker_pid INTEGER,
            last_failure_error TEXT,
            max_runtime_seconds INTEGER,
            last_heartbeat_at INTEGER,
            current_run_id INTEGER,
            workflow_template_id TEXT,
            current_step_key TEXT,
            skills TEXT,
            max_retries INTEGER,
            session_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE task_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL DEFAULT '',
            run_id     INTEGER,
            kind       TEXT NOT NULL DEFAULT '',
            payload    TEXT,
            created_at INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    # Running migration on an already-migrated schema must not raise.
    kb._migrate_add_optional_columns(conn)
    conn.close()


# ---------------------------------------------------------------------------
# Dispatcher spawn invocation — _resolve_hermes_argv()
#
# Workers spawned by the dispatcher must use a `hermes` invocation that does
# not depend on PATH being set up correctly. cron jobs, systemd User= services,
# launchd jobs, and other detached processes routinely run with a stripped
# $PATH that doesn't include the venv's bin/, so a bare `["hermes", ...]`
# spawn fails with FileNotFoundError and the task gets stuck. The resolver
# prefers the PATH shim (familiar `ps` output) but falls back to the module
# form so the spawn keeps working when PATH is missing the shim.
# ---------------------------------------------------------------------------


def test_resolve_hermes_argv_prefers_path_shim(monkeypatch):
    """When `hermes` is on PATH, use the shim — preserves familiar ps output."""
    import shutil
    import hermes_cli.kanban_db as kb

    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/hermes")
    argv = kb._resolve_hermes_argv()
    assert argv == ["/usr/local/bin/hermes"]


def test_resolve_hermes_argv_absolutizes_relative_exe_shim(monkeypatch, tmp_path):
    """A relative executable override must not remain workspace-cwd-dependent."""
    import hermes_cli.kanban_db as kb

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_BIN", ".\\hermes.exe")
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)

    assert kb._resolve_hermes_argv() == [os.path.abspath(".\\hermes.exe")]


def test_resolve_hermes_argv_avoids_implicit_windows_batch_shim(monkeypatch, tmp_path):
    """Implicit .cmd/.bat shims use the module fallback, not batch argv[0]."""
    import sys
    import hermes_cli.kanban_db as kb

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "hermes.CMD").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)

    assert kb._resolve_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_honors_hermes_bin_path_override(monkeypatch, tmp_path):
    """An explicit path-like HERMES_BIN lets service managers pin the executable."""
    import shutil
    import hermes_cli.kanban_db as kb

    shim = tmp_path / "bin" / "hermes"
    shim.parent.mkdir()
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_BIN", str(shim))
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert kb._resolve_hermes_argv() == [str(shim)]


def test_resolve_hermes_argv_hermes_bin_bare_name_uses_path(monkeypatch, tmp_path):
    """Bare HERMES_BIN values keep PATH semantics instead of cwd shadowing."""
    import stat
    import hermes_cli.kanban_db as kb

    cwd_hermes = tmp_path / "hermes"
    cwd_hermes.write_text("wrong\n", encoding="utf-8")
    cwd_hermes.chmod(cwd_hermes.stat().st_mode | stat.S_IXUSR)
    path_hermes = tmp_path / "bin" / "hermes"
    path_hermes.parent.mkdir()
    path_hermes.write_text("right\n", encoding="utf-8")
    path_hermes.chmod(path_hermes.stat().st_mode | stat.S_IXUSR)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(path_hermes.parent))
    monkeypatch.setenv("HERMES_BIN", "hermes")

    assert kb._resolve_hermes_argv() == [str(path_hermes)]


def test_resolve_hermes_argv_hermes_bin_bare_name_ignores_cwd(monkeypatch, tmp_path):
    """Bare HERMES_BIN does not accept current-directory shadow executables."""
    import sys
    import hermes_cli.kanban_db as kb

    (tmp_path / "hermes.exe").write_text("wrong\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HERMES_BIN", "hermes")
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)

    assert kb._resolve_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_hermes_bin_bare_cmd_uses_module_fallback(monkeypatch, tmp_path):
    """A PATH-resolved HERMES_BIN batch shim is not used as worker argv[0]."""
    import sys
    import hermes_cli.kanban_db as kb

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "hermes.CMD").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.setenv("HERMES_BIN", "hermes")
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)

    assert kb._resolve_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_hermes_bin_unresolved_bare_name_falls_back(monkeypatch):
    """Unresolved HERMES_BIN command names do not delegate cwd search to Popen."""
    import sys
    import hermes_cli.kanban_db as kb

    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HERMES_BIN", "hermes")

    assert kb._resolve_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_falls_back_to_module_form_when_no_path_shim(monkeypatch):
    """When the shim is not on PATH, fall back to `python -m hermes_cli.main`.

    Pins the correct module name (NOT `hermes` — there is no top-level
    `hermes` package). Regression for #23198: the original PR shipped
    `python -m hermes` which fails with `No module named hermes` on every
    invocation.
    """
    import shutil
    import sys
    import hermes_cli.kanban_db as kb

    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    argv = kb._resolve_hermes_argv()
    assert argv == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_module_actually_runs():
    """The fallback module name must be importable + runnable.

    A unit test that pins the literal string is necessary but not
    sufficient — if `hermes_cli.main` ever loses `if __name__ == "__main__"`
    handling or its argparse setup, `python -m hermes_cli.main --version`
    would fail and so would every dispatcher spawn that hits the fallback.
    Run it as a real subprocess to catch that regression.
    """
    import subprocess
    import hermes_cli.kanban_db as kb
    import shutil
    import unittest.mock as mock

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_BIN", None)
        with mock.patch.object(shutil, "which", return_value=None):
            argv = kb._resolve_hermes_argv()
    r = subprocess.run(argv + ["--version"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (
        f"`{' '.join(argv)} --version` failed (rc={r.returncode}); "
        f"stderr={r.stderr[:200]!r}"
    )
    assert "Hermes Agent" in r.stdout, f"unexpected output: {r.stdout[:200]!r}"


# ---------------------------------------------------------------------------
# task_age — guard against corrupt timestamp values
#
# The Task dataclass declares ``created_at: int`` but rows come from sqlite
# without coercion at the boundary. A row that ever held a non-int (e.g. an
# unsubstituted ``'%s'`` from a logged format string, ``None``, an arbitrary
# string, or a float-as-string) used to crash ``task_age`` with ``ValueError``
# and turn ``GET /api/plugins/kanban/board`` into a 500 because the dashboard
# calls ``task_age`` unguarded for every task in the response.
#
# After the fix, ``_safe_int`` returns ``None`` on bad input and ``task_age``
# degrades gracefully (per-field ``None`` rather than a hard crash).
# ---------------------------------------------------------------------------


def _make_task(**overrides) -> "kb.Task":
    """Minimal Task with all required fields filled in. Override anything."""
    defaults = dict(
        id="t_age",
        title="x",
        body=None,
        assignee=None,
        status="ready",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )
    defaults.update(overrides)
    return kb.Task(**defaults)


def test_safe_int_accepts_int_and_int_string():
    """Sanity: well-typed values pass through."""
    # PR d8ad431de renamed _safe_int → _to_epoch (now also handles ISO-8601).
    assert kb._to_epoch(0) == 0
    assert kb._to_epoch(1700000000) == 1700000000
    assert kb._to_epoch("1700000000") == 1700000000


def test_safe_int_returns_none_on_corrupt_inputs():
    """All the failure modes that used to crash task_age."""
    # None — common when the column was never written
    assert kb._to_epoch(None) is None
    # Unsubstituted format string — the literal case the PR title cites
    assert kb._to_epoch("%s") is None
    # Arbitrary non-numeric strings
    assert kb._to_epoch("abc") is None
    assert kb._to_epoch("") is None
    # Float-ish strings: int("1.5") raises ValueError too — caller wants None.
    assert kb._to_epoch("1.5") is None
    # Random object — covered by TypeError branch
    assert kb._to_epoch(object()) is None


def test_task_age_handles_corrupt_created_at():
    """Pre-fix this raised ValueError and 500'd /api/plugins/kanban/board."""
    t = _make_task(created_at="%s")
    age = kb.task_age(t)
    assert age["created_age_seconds"] is None
    assert age["started_age_seconds"] is None
    assert age["time_to_complete_seconds"] is None


def test_task_age_handles_corrupt_started_and_completed():
    """All three timestamp fields share the same _safe_int treatment."""
    t = _make_task(
        created_at=1700000000,
        started_at="garbage",
        completed_at=None,
    )
    age = kb.task_age(t)
    assert isinstance(age["created_age_seconds"], int)
    assert age["started_age_seconds"] is None
    assert age["time_to_complete_seconds"] is None


def test_task_age_well_formed_task():
    """Regression: the safe-int path must not change behavior for normal data."""
    import time
    now = int(time.time())
    t = _make_task(
        created_at=now - 60,
        started_at=now - 30,
        completed_at=now,
    )
    age = kb.task_age(t)
    assert 55 <= age["created_age_seconds"] <= 65
    assert 25 <= age["started_age_seconds"] <= 35
    assert 25 <= age["time_to_complete_seconds"] <= 35


def test_task_dict_survives_corrupt_created_at(tmp_path, monkeypatch):
    """Defense in depth: even if task_age ever raised, plugin_api must not 500.

    The PR also added a try/except around the task_age call in
    `plugins/kanban/dashboard/plugin_api.py::_task_dict`. Verify a single
    corrupt row doesn't turn the whole board response into an error.
    """
    # Set up an isolated kanban home so we can write a corrupt created_at.
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    # Insert a row with a non-int created_at (simulates the historical
    # bug that produced corrupt rows).
    conn = kb.connect()
    try:
        good_id = kb.create_task(conn, title="good")
        # Now write a row with corrupt created_at directly.
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ("%s", good_id),
        )
    finally:
        conn.close()

    # Re-read and pass through task_age — must not raise.
    conn = kb.connect()
    try:
        task = kb.get_task(conn, good_id)
    finally:
        conn.close()
    age = kb.task_age(task)
    assert age["created_age_seconds"] is None


# ---------------------------------------------------------------------------
# Board-level default_workdir
# ---------------------------------------------------------------------------


def test_create_task_scratch_without_workspace_ignores_board_default_workdir(kanban_home, monkeypatch):
    """Scratch tasks must NOT inherit board.default_workdir — would point auto-cleanup
    at the user's source tree on completion (#28818)."""
    default_wd = "/home/user/project"
    kb.create_board("work-proj", default_workdir=default_wd)

    with kb.connect(board="work-proj") as conn:
        tid = kb.create_task(conn, title="scratch-task", board="work-proj")
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.workspace_kind == "scratch"
    assert t.workspace_path is None


def test_create_task_dir_without_workspace_inherits_board_default_workdir(kanban_home, monkeypatch):
    """Board default_workdir is for persistent dir/worktree workspaces, not scratch."""
    default_wd = "/home/user/project"
    kb.create_board("work-proj-dir", default_workdir=default_wd)

    with kb.connect(board="work-proj-dir") as conn:
        tid = kb.create_task(
            conn,
            title="inherited",
            workspace_kind="dir",
            board="work-proj-dir",
        )
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.workspace_path == default_wd


def test_create_task_without_workspace_no_default_stays_none(kanban_home):
    """Board without default_workdir → create_task without workspace_path → stays None."""
    kb.create_board("empty-board")

    with kb.connect(board="empty-board") as conn:
        tid = kb.create_task(conn, title="none", board="empty-board")
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.workspace_path is None


def test_create_task_with_explicit_workspace_ignores_board_default(kanban_home):
    """create_task with explicit workspace_path → ignores board default."""
    kb.create_board("custom-ws-board", default_workdir="/board/default")

    explicit = "/my/explicit/path"
    with kb.connect(board="custom-ws-board") as conn:
        tid = kb.create_task(conn, title="explicit", workspace_path=explicit, board="custom-ws-board")
        t = kb.get_task(conn, tid)
    assert t is not None
    assert t.workspace_path == explicit
    assert t.workspace_path != "/board/default"


def test_create_task_code_role_gets_coder_contract(
    kanban_home, monkeypatch, tmp_path
):
    """Code-role cards get compact scope/deps/test/handoff rails."""
    monkeypatch.setattr(
        kb, "_review_gate_config", lambda: {"code_roles": ["coder"]}
    )
    repo = tmp_path / "family-organizer"
    repo.mkdir()
    from hermes_cli import kanban_worktrees

    monkeypatch.setattr(kanban_worktrees, "FO_REPO_PATH", repo)

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="[FO] ship chips",
            body="Implement favorite chips.",
            assignee="coder",
            tenant="family-organizer",
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.workspace_kind == "dir"
    assert task.workspace_path == str(repo)
    assert task.body is not None
    assert task.body.startswith("Implement favorite chips.")
    assert "## Hermes Coder Contract v1" in task.body
    assert f"Workspace: dir:{repo}" in task.body
    assert "Dependency gate:" in task.body
    assert "Kanban CLI self-tests:" in task.body
    assert "HERMES_SANDBOX_MODE=1" in task.body
    assert "Completion metadata:" in task.body


def test_code_task_contract_body_has_no_duplicate_workspace_or_assignee_lines(
    kanban_home, tmp_path
):
    """Contract body must have exactly one Workspace: and one Assignee: line (no Repo/workspace, no Assignee/lane)."""
    repo = tmp_path / "repo"
    repo.mkdir()

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="fix the bug",
            body="Do the work.",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.body is not None
    body = task.body
    workspace_lines = [ln for ln in body.splitlines() if ln.startswith("- Workspace:")]
    assignee_lines = [ln for ln in body.splitlines() if ln.startswith("- Assignee:")]
    assert len(workspace_lines) == 1, f"Expected exactly 1 Workspace: line, got: {workspace_lines}"
    assert len(assignee_lines) == 1, f"Expected exactly 1 Assignee: line, got: {assignee_lines}"
    assert not any("Repo/workspace:" in ln for ln in body.splitlines()), (
        "Duplicate 'Repo/workspace:' line found in contract body"
    )
    assert not any("Assignee/lane:" in ln for ln in body.splitlines()), (
        "Duplicate 'Assignee/lane:' line found in contract body"
    )


def test_code_task_contract_body_risk_is_low_for_scratch_workspace(kanban_home):
    """A scratch-workspace task's contract body Risk line must say 'low', not 'medium'."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="analyse scratch results",
            body="Run some analysis.",
            assignee="coder",
            workspace_kind="scratch",
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.body is not None
    body = task.body
    risk_lines = [ln for ln in body.splitlines() if ln.startswith("- Risk:")]
    assert risk_lines, "No Risk: line found in contract body"
    assert "low" in risk_lines[0], (
        f"Expected 'low' risk for scratch workspace, got: {risk_lines[0]}"
    )
    assert "medium" not in risk_lines[0], (
        f"scratch-workspace body should say 'low' not 'medium': {risk_lines[0]}"
    )



def test_create_task_non_code_role_body_unchanged(kanban_home, monkeypatch):
    monkeypatch.setattr(
        kb, "_review_gate_config", lambda: {"code_roles": ["coder"]}
    )

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="read docs",
            body="Summarize the release notes.",
            assignee="research",
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.body == "Summarize the release notes."


def test_code_task_missing_contract_blocks_before_claim(kanban_home, monkeypatch):
    monkeypatch.setattr(
        kb, "_review_gate_config", lambda: {"code_roles": ["coder"]}
    )

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="ambiguous repo",
            assignee="coder",
            workspace_kind="dir",
        )
        assert kb.claim_task(conn, tid) is None
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task.status == "blocked"
    kinds = [e.kind for e in events]
    assert "needs_contract" in kinds
    assert "needs_contract_blocked" in kinds
    blocked = [e for e in events if e.kind == "blocked"][-1]
    assert "repo_workspace" in (blocked.payload or {}).get("reason", "")


def test_code_task_safe_contract_is_auto_enriched_before_pickup(
    kanban_home, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        kb, "_review_gate_config", lambda: {"code_roles": ["coder"]}
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="explicit repo",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        with kb.write_txn(conn):
            conn.execute(
                "DELETE FROM task_events WHERE task_id = ? AND kind = ?",
                (tid, "code_task_contract_inferred"),
            )
        task = kb.claim_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task is not None
    contract_events = [e for e in events if e.kind == "code_task_contract_inferred"]
    assert contract_events
    payload = contract_events[-1].payload
    assert payload["repo_workspace"] == f"dir:{repo}"
    assert payload["allowed_paths"] == [str(repo)]


# ---------------------------------------------------------------------------
# B2: contract-inference hygiene (allowed_paths parser + reason_for_lane)
# ---------------------------------------------------------------------------


def test_absolute_paths_from_text_rejects_single_segment_prose_token():
    """B2.1: the allowed-paths parser must not scoop a single-segment slash token
    out of prose. The observed defect: a body mentioning the dispatcher action
    `action=="merged"/integration_merged` produced allowed_paths
    ['/integration_merged'] (the slash after the closing quote passed the
    negative lookbehind). A 1-segment absolute token is never a real
    repo/allowed path, so it is dropped."""
    body = (
        'The integrator returns action=="merged"/integration_merged when the '
        "rebase applies cleanly; otherwise \"rebase_conflict\"/integration_parked."
    )
    paths = kb._absolute_paths_from_text(body)
    assert "/integration_merged" not in paths
    assert "/integration_parked" not in paths
    assert paths == []


def test_absolute_paths_from_text_keeps_real_multi_segment_paths():
    """B2.1: genuine multi-segment absolute paths still survive (no regression for
    real allowed-path extraction)."""
    body = (
        "Edit /home/piet/.hermes/hermes-agent/hermes_cli/kanban_db.py and the "
        "test at /home/piet/.hermes/hermes-agent/tests/stress/conftest.py only."
    )
    paths = kb._absolute_paths_from_text(body)
    assert "/home/piet/.hermes/hermes-agent/hermes_cli/kanban_db.py" in paths
    assert "/home/piet/.hermes/hermes-agent/tests/stress/conftest.py" in paths


def test_reason_for_lane_coder_makes_no_false_model_claim():
    """B2.2: the `coder` lane reason must not assert a fixed model/provider — the
    lane resolves to whatever the lane config routes to (e.g. glm/neuralwatt),
    so the old hardcoded '(OpenAI-Codex/GPT)' was actively misleading."""
    reason = kb._reason_for_lane("coder")
    assert "OpenAI-Codex/GPT" not in reason
    assert "OpenAI" not in reason and "GPT" not in reason
    # the lane PURPOSE is still described
    assert "code" in reason.lower()
    # the canonical Claude lane reason is untouched (regression guard)
    assert "chain-critical" in kb._reason_for_lane("premium")


def test_code_task_contract_allowed_paths_excludes_prose_token():
    """B2.1 end-to-end at the payload builder: a scratch code task whose body
    mentions `"merged"/integration_merged` must NOT infer that prose token as an
    allowed path."""
    payload, _missing = kb._code_task_contract_payload(
        assignee="coder",
        workspace_kind="scratch",
        workspace_path=None,
        tenant="default",
        body='returns action=="merged"/integration_merged on a clean rebase',
        created_by="tester",
        protected_funnel_root=False,
        source="test",
    )
    assert "/integration_merged" not in payload["allowed_paths"]
    assert "OpenAI" not in payload["reason_for_lane"]


@pytest.mark.parametrize("assignee", ["reviewer", "critic", "research"])
def test_3a_code_task_rejects_verdict_only_roles_at_create(
    kanban_home, assignee
):
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="role_misuse"):
            kb.create_task(
                conn,
                title="implement widget",
                assignee=assignee,
                kind="code",
            )


def test_3a_existing_code_task_with_verdict_role_blocks_before_claim(
    kanban_home, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        kb, "_review_gate_config",
        lambda: {"code_roles": ["coder", "coder-claude", "premium"]},
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="legacy wrong-role code task",
            assignee="coder",
            kind="code",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = ? WHERE id = ?",
                ("reviewer", tid),
            )

        assert kb.claim_task(conn, tid) is None
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task is not None
    assert task.status == "blocked"
    needs = [e for e in events if e.kind == "needs_contract"][-1]
    assert (needs.payload or {})["issue"] == "role_misuse"
    blocked = [e for e in events if e.kind == "blocked"][-1]
    assert "role_misuse" in (blocked.payload or {})["reason"]


@pytest.mark.parametrize(
    ("assignee", "kind"),
    [("reviewer", "review"), ("critic", "review"), ("research", "research")],
)
def test_3a_verdict_and_research_tasks_still_claim_when_not_code(
    kanban_home, assignee, kind
):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title=f"{assignee} lane task",
            assignee=assignee,
            kind=kind,
        )
        claimed = kb.claim_task(conn, tid)

    assert claimed is not None
    assert claimed.assignee == assignee


def test_3a_coder_claude_contract_uses_canonical_lane_reason(
    kanban_home, tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="reason through chain-critical change",
            body="Implement the careful fix.",
            assignee="coder-claude",
            kind="code",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task is not None
    assert task.body is not None
    # Phase A: coder-claude folds into the canonical Claude coder lane `premium`.
    assert task.assignee == "premium"
    assert "Reason for lane: the Claude code lane (claude-cli/Opus)" in task.body
    contract = [e for e in events if e.kind == "code_task_contract_inferred"][-1]
    payload = contract.payload or {}
    assert payload["assignee_lane"] == "premium"
    assert "chain-critical" in payload["reason_for_lane"]
    assert "cross-family review" in payload["reason_for_lane"]


def test_complete_task_records_self_verification_event(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="verify self", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn,
            tid,
            result="done",
            summary="ran focused gate",
            metadata={"self_verification": kb.SELF_VERIFIED},
        )
        kinds = [e.kind for e in kb.list_events(conn, tid)]

    assert kb.SELF_VERIFIED in kinds


def test_deliverable_posted_not_completed_is_recoverable_and_repairable(
    kanban_home, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="render quarterly report", assignee="coder")
        kb.claim_task(conn, tid)
        kb.add_comment(
            conn,
            tid,
            "coder",
            (
                "# Deliverable: render quarterly report\n\n"
                "The quarterly report is complete and mapped to the requested "
                "objective. Evidence includes the final section list, validation "
                "notes, and remaining risk. " + "x" * 120
            ),
        )
        pid = 424242
        kb._set_worker_pid(conn, tid, pid)
        kb._record_worker_exit(pid, 0)

        crashed = kb.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

        assert tid not in crashed
        assert task.status == "blocked"
        kinds = [e.kind for e in events]
        assert kb.DELIVERABLE_POSTED_NOT_COMPLETED in kinds
        assert "gave_up" not in kinds

        assert kb.repair_deliverable_posted_not_completed(
            conn, tid, actor="integrator",
        )
        repaired = kb.get_task(conn, tid)
        repair_events = [
            e for e in kb.list_events(conn, tid)
            if e.kind == "deliverable_protocol_repaired"
        ]
        verdicts = conn.execute(
            "SELECT verdict FROM task_runs WHERE task_id = ?", (tid,),
        ).fetchall()

    assert repaired.status == "done"
    assert repair_events
    assert repair_events[-1].payload["actor"] == "integrator"
    assert all(row["verdict"] is None for row in verdicts)


def test_protocol_miss_without_deliverable_still_hard_blocks(
    kanban_home, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="silent protocol miss", assignee="worker")
        kb.claim_task(conn, tid)
        pid = 424243
        kb._set_worker_pid(conn, tid, pid)
        kb._record_worker_exit(pid, 0)

        crashed = kb.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]

    assert tid in crashed
    assert task.status == "blocked"
    assert "protocol_violation" in kinds
    assert "gave_up" in kinds
    assert kb.DELIVERABLE_POSTED_NOT_COMPLETED not in kinds


def test_3b_operator_escalation_emitted_once_when_failure_ladder_exhausts(
    kanban_home,
):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="needs human decision", assignee="coder")
        assert kb.claim_task(conn, tid) is not None
        assert not kb._record_task_failure(
            conn,
            tid,
            "first spawn failure",
            outcome="spawn_failed",
            failure_limit=2,
            release_claim=True,
            end_run=True,
        )
        assert [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ] == []

        assert kb.claim_task(conn, tid) is not None
        assert kb._record_task_failure(
            conn,
            tid,
            "second spawn failure",
            outcome="spawn_failed",
            failure_limit=2,
            release_claim=True,
            end_run=True,
            event_payload_extra={"pid": 1234},
        )
        events = kb.list_events(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.status == "blocked"
    assert len([e for e in events if e.kind == "gave_up"]) == 1
    escalations = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
    assert len(escalations) == 1
    payload = escalations[0].payload or {}
    assert set(payload) == {
        "task",
        "why_now",
        "attempts_already_made",
        "evidence",
        "recommended_human_action",
        "blocked_action_boundary",
    }
    assert payload["attempts_already_made"] == 2
    assert payload["task"]["id"] == tid
    assert payload["evidence"]["trigger_outcome"] == "spawn_failed"
    assert payload["evidence"]["context"] == {"pid": 1234}
    assert payload["blocked_action_boundary"] == list(kb.OPERATOR_ONLY_ACTIONS)
    boundary = " ".join(payload["blocked_action_boundary"]).lower()
    assert "push" not in boundary
    assert "deploy" not in boundary
    assert "restart" not in boundary


# ---------------------------------------------------------------------------
# ESCALATION-IDEMPOTENT-COALESCE-S1: the inline gave_up escalation write path
# is idempotent per Heiler class (≤ 1 raw operator_escalation event per class
# per root), while every breaker-trip cycle still leaves a gave_up event and a
# heiler_classification so nothing is silently dropped.
# ---------------------------------------------------------------------------

def _redispatch(conn, task_id):
    """Simulate an operator unblock + re-dispatch: counter reset, re-claimed."""
    assert kb.unblock_task(conn, task_id)
    assert kb.claim_task(conn, task_id) is not None


def test_escalation_coalesce_same_class_writes_one_raw_event(kanban_home):
    """A re-dispatched root that exhausts its ladder again with the SAME class
    must NOT append a duplicate raw operator_escalation event — at most one raw
    event per class — yet every cycle still records a gave_up + classification
    so the repetition is never invisibly dropped."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="loops", assignee="coder")
        kb.claim_task(conn, tid)
        # cycle 1: spawn_failed -> transient class -> first escalation written
        assert kb._record_task_failure(
            conn, tid, "spawn boom", outcome="spawn_failed",
            failure_limit=1, release_claim=True, end_run=True,
        )
        _redispatch(conn, tid)
        # cycle 2: spawn_failed again -> SAME transient class -> coalesced
        assert kb._record_task_failure(
            conn, tid, "spawn boom 2", outcome="spawn_failed",
            failure_limit=1, release_claim=True, end_run=True,
        )
        events = kb.list_events(conn, tid)

    escalations = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
    gave_ups = [e for e in events if e.kind == "gave_up"]
    heiler = [e for e in events if e.kind == kb.HEILER_CLASSIFICATION_EVENT]
    # one RAW event for the (single) class — the repeat is coalesced away
    assert len(escalations) == 1
    # but BOTH cycles left a gave_up (the counter source) ...
    assert len(gave_ups) == 2
    # ... and BOTH cycles were classified into the by-class ledger (the count
    # of reported real problems must NOT shrink because of the coalesce)
    assert len(heiler) == 2


def test_escalation_coalesce_new_class_stays_visible(kanban_home):
    """A genuinely NEW failure class on the same root is NOT suppressed: it
    writes its own raw escalation event and stays visible, while same-class
    repeats are still coalesced to one raw event per class."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="loops", assignee="coder")
        kb.claim_task(conn, tid)
        kb._record_task_failure(
            conn, tid, "spawn boom", outcome="spawn_failed",
            failure_limit=1, release_claim=True, end_run=True,
        )
        _redispatch(conn, tid)
        kb._record_task_failure(  # same transient class -> coalesced
            conn, tid, "spawn boom 2", outcome="spawn_failed",
            failure_limit=1, release_claim=True, end_run=True,
        )
        _redispatch(conn, tid)
        kb._record_task_failure(  # NEW real-bug class -> fresh raw event
            conn, tid, "tests failed: assertion", outcome="crashed",
            failure_limit=1, release_claim=True, end_run=True,
        )
        events = kb.list_events(conn, tid)

    escalations = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
    gave_ups = [e for e in events if e.kind == "gave_up"]
    classes = sorted(
        {kb._classify_escalation_payload(e.payload or {})[0] for e in escalations}
    )
    assert len(gave_ups) == 3              # every cycle recorded
    assert len(escalations) == 2           # one per class (transient + real-bug)
    assert classes == ["real-bug", "transient"]


def test_escalation_coalesce_decision_queue_counter(kanban_home):
    """decision_queue surfaces the full escalation count (every cycle, incl. the
    coalesced ones) plus the distinct classes and how many repeats were
    coalesced — so the operator sees N escalations, nothing silently dropped."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="loops", assignee="coder")
        cycles = [
            ("spawn boom", "spawn_failed"),       # transient
            ("spawn boom 2", "spawn_failed"),     # same class -> coalesced
            ("tests failed: assertion", "crashed"),  # new real-bug class
        ]
        for i, (err, oc) in enumerate(cycles):
            kb.claim_task(conn, tid)
            assert kb._record_task_failure(
                conn, tid, err, outcome=oc, failure_limit=1,
                release_claim=True, end_run=True,
            )
            if i < len(cycles) - 1:
                assert kb.unblock_task(conn, tid)
        result = kb.decision_queue(conn)

    row = next(d for d in result["decisions"] if d["task_id"] == tid)
    assert row["kind"] == "operator_escalation"
    # N escalations of this root = every breaker-trip cycle, not just the raw
    # events left in the ledger after coalescing
    assert row["escalation_count"] == 3
    assert sorted(row["escalation_classes"]) == ["real-bug", "transient"]
    # one same-class duplicate was coalesced (3 cycles -> 2 raw events)
    assert row["coalesced_repeats"] == 1


def test_escalation_coalesce_counts_gave_up_after_non_gave_up_writer(kanban_home):
    """Mixed writer + gave_up regression: when a NON-gave_up escalation writer
    (here budget-runaway) already escalated a class, a later breaker-trip cycle
    of the SAME class is coalesced — and that suppressed cycle must STILL be
    explicit in decision_queue. The raw event from the non-gave_up writer shares
    no event with the coalesced gave_up, so a max(raw, gave_up) counter would
    silently lose the second cycle (escalation_count=1, coalesced_repeats=0)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="loops", assignee="coder")
        # NON-gave_up writer: a budget-runaway park writes a raw operator_escalation
        # (HEILER-CLASSIFY-SIGNAL-GAP-S2: classifies capacity, not unclassified)
        # without going through the gave_up branch.
        fresh = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert kb._park_budget_runaway(conn, fresh, token_sum=999, cap=10, runs=3)
        # re-dispatch, then trip the breaker with the SAME (capacity) class
        _redispatch(conn, tid)
        assert kb._record_task_failure(
            conn, tid, "iteration budget exhausted", outcome="unknown",
            failure_limit=1, release_claim=True, end_run=True,
        )
        events = kb.list_events(conn, tid)
        result = kb.decision_queue(conn)

    escalations = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
    gave_ups = [e for e in events if e.kind == "gave_up"]
    # one RAW event (the non-gave_up writer); the same-class gave_up is coalesced
    assert len(escalations) == 1
    assert len(gave_ups) == 1
    # the coalesced gave_up cycle carries the explicit flag so the counter sees it
    assert gave_ups[0].payload.get("escalation_coalesced") is True

    row = next(d for d in result["decisions"] if d["task_id"] == tid)
    assert row["kind"] == "operator_escalation"
    # 2 escalation cycles total: the budget-runaway park + the coalesced gave_up
    assert row["escalation_count"] == 2
    assert row["escalation_classes"] == ["capacity"]
    # exactly one suppressed repeat, made explicit (was invisibly dropped before)
    assert row["coalesced_repeats"] == 1


def test_4a_scheduled_overdue_is_unblocked_once(kanban_home):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="wake later", assignee="coder")
        assert kb.schedule_task(conn, tid, reason="timer") is True
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id = ? AND kind = 'scheduled'",
                (now - 7200, tid),
            )

        first = kb.no_silent_stall_sweep(
            conn, now=now, min_age_seconds=3600,
        )
        second = kb.no_silent_stall_sweep(
            conn, now=now + 10, min_age_seconds=3600,
        )
        task = kb.get_task(conn, tid)
        markers = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.NO_SILENT_STALL_EVENT
        ]

    assert first["self_healed"] == [
        {"task_id": tid, "class": "scheduled_overdue", "action": "unblocked"}
    ]
    assert second["self_healed"] == []
    assert task.status == "ready"
    assert len(markers) == 1
    assert markers[0].payload["action"] == "nudged"


def test_4a_scheduled_overdue_skips_operator_held_chain(kanban_home):
    # A freigabe:operator PlanSpec chain is held in 'scheduled' for explicit
    # operator release (propose-and-wait). The no-silent-stall sweep must NOT
    # mistake that intentional hold for a stall and nudge it live — neither the
    # held root NOR its held build children (a dep-free build child would
    # dispatch behind the operator's back if nudged to ready). Built via the
    # REAL decompose topology (links: parent_id=child, child_id=root), not a
    # hand-rolled link, so the child->root walk is exercised in production shape.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn, title="held root", assignee="orchestrator", triage=True,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET freigabe = 'operator' WHERE id = ?", (root,)
            )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[{"title": "build child"}],
            initial_child_status="scheduled",
        )
        assert child_ids is not None and len(child_ids) == 1
        build_child = child_ids[0]

        # Real F1 hold: both root and build child land held in 'scheduled'.
        assert kb.get_task(conn, root).status == "scheduled"
        assert kb.get_task(conn, build_child).status == "scheduled"

        # Age past the no-silent-stall window — both the 'scheduled' event and the
        # created_at fallback the sweep reads, so age is never the reason to skip.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id IN (?, ?) AND kind = 'scheduled'",
                (now - 7200, root, build_child),
            )
            conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id IN (?, ?)",
                (now - 7200, root, build_child),
            )

        summary = kb.no_silent_stall_sweep(conn, now=now, min_age_seconds=3600)
        root_task = kb.get_task(conn, root)
        child_task = kb.get_task(conn, build_child)

    # The intentional hold survived: neither root nor child was nudged.
    assert root_task.status == "scheduled"
    assert child_task.status == "scheduled"
    # Recorded as a deliberate skip, not a self-heal.
    assert root in summary.get("skipped_held", [])
    assert build_child in summary.get("skipped_held", [])
    assert summary["self_healed"] == []


def test_4a_scheduled_overdue_skips_ui_real_held_root(kanban_home):
    # The ui-real operator hold (Phase 4 A) shares the scheduled-park mechanism
    # and must be exempt from the stall nudge for the same reason.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="ui-real held root", assignee="coder")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET live_test_depth = 'ui-real' WHERE id = ?",
                (root,),
            )
        assert kb.schedule_task(conn, root, reason="ui-real hold") is True
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id = ? AND kind = 'scheduled'",
                (now - 7200, root),
            )
        summary = kb.no_silent_stall_sweep(conn, now=now, min_age_seconds=3600)
        root_task = kb.get_task(conn, root)

    assert root_task.status == "scheduled"
    assert root in summary.get("skipped_held", [])
    assert summary["self_healed"] == []


def test_4a_decompose_failure_parks_once_and_skips_funnel_root(kanban_home):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        normal = kb.create_task(
            conn, title="normal triage", assignee="coder", triage=True,
        )
        funnel = kb.create_task(
            conn,
            title="funnel triage",
            assignee="coder",
            triage=True,
            created_by="family",
        )
        for _ in range(3):
            kb.record_decompose_failure(conn, normal)
            kb.record_decompose_failure(conn, funnel)

        first = kb.no_silent_stall_sweep(conn, now=now)
        second = kb.no_silent_stall_sweep(conn, now=now + 1)
        normal_task = kb.get_task(conn, normal)
        funnel_task = kb.get_task(conn, funnel)
        normal_escalations = [
            e for e in kb.list_events(conn, normal)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]
        funnel_escalations = [
            e for e in kb.list_events(conn, funnel)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert {"task_id": normal, "class": "triage_decompose_failed"} in first["parked"]
    assert second["parked"] == []
    assert normal_task.status == "blocked"
    assert funnel_task.status == "triage"
    assert len(normal_escalations) == 1
    assert funnel_escalations == []


def test_4a_decompose_failure_skips_operator_held_chain(kanban_home):
    # The triage-decompose-failed branch parks any task with
    # decompose_failed >= limit whose status is not done/archived — and
    # 'scheduled' is in that set. A freigabe:operator chain is held in
    # 'scheduled' for explicit operator release; the held root carries the
    # decompose_failed counter (the counter reset on a successful decompose is
    # fail-soft and runs in a SEPARATE txn after the scheduled-flip commits, so
    # a crash / swallowed-error window can leave a held root 'scheduled' with a
    # non-zero counter). The sweep must NOT mistake that intentional hold for a
    # decompose stall and park it to 'blocked' — that strips the operator hold
    # and makes the chain eligible for auto_retry_blocked_tasks -> 'ready',
    # building the held proposal behind the operator's back. Built via the REAL
    # decompose topology (links: parent_id=child, child_id=root) so the
    # child->root walk in _is_operator_held is exercised in production shape.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn, title="held root", assignee="orchestrator", triage=True,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET freigabe = 'operator' WHERE id = ?", (root,)
            )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[{"title": "build child"}],
            initial_child_status="scheduled",
        )
        assert child_ids is not None and len(child_ids) == 1
        build_child = child_ids[0]

        # Real F1 hold: both root and build child land held in 'scheduled'.
        assert kb.get_task(conn, root).status == "scheduled"
        assert kb.get_task(conn, build_child).status == "scheduled"

        # Push BOTH past the decompose-failure limit so the §3 query would
        # select them absent the hold exemption (root = realistic vector,
        # child = exercises the child->root walk).
        for _ in range(3):
            kb.record_decompose_failure(conn, root)
            kb.record_decompose_failure(conn, build_child)

        summary = kb.no_silent_stall_sweep(conn, now=now, min_age_seconds=3600)
        root_task = kb.get_task(conn, root)
        child_task = kb.get_task(conn, build_child)
        root_escalations = [
            e for e in kb.list_events(conn, root)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    # The intentional hold survived: neither was parked to 'blocked'.
    assert root_task.status == "scheduled"
    assert child_task.status == "scheduled"
    # Recorded as a deliberate skip, not a stall park, and no escalation fired.
    assert root in summary.get("skipped_held", [])
    assert build_child in summary.get("skipped_held", [])
    assert summary["parked"] == []
    assert root_escalations == []


def test_4a_decompose_failure_exemption_is_scoped_to_active_hold(kanban_home):
    # The §3 hold exemption must protect ONLY a chain still actively held in
    # 'scheduled'. Once the operator RELEASES (release_freigabe_hold flips the
    # root 'scheduled' -> 'todo' but never clears the freigabe column), the row
    # is no longer held and must regain its pre-exemption behaviour — i.e. a
    # released root carrying decompose_failed >= limit is park-eligible again,
    # exactly as a plain non-held 'todo' root would be. Otherwise the
    # exemption would permanently shield a real decompose stall behind a stale
    # freigabe='operator' tag. Guards the asymmetry in _is_operator_held (the
    # direct _held check must be scheduled-gated like the child->root walk).
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn, title="released root", assignee="orchestrator", triage=True,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET freigabe = 'operator' WHERE id = ?", (root,)
            )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[{"title": "build child"}],
            initial_child_status="scheduled",
        )
        assert child_ids is not None and len(child_ids) == 1

        for _ in range(3):
            kb.record_decompose_failure(conn, root)

        # Operator releases: root 'scheduled' -> 'todo', freigabe still 'operator'.
        assert kb.release_freigabe_hold(conn, root) is True
        assert kb.get_task(conn, root).status == "todo"

        summary = kb.no_silent_stall_sweep(conn, now=now, min_age_seconds=3600)
        root_task = kb.get_task(conn, root)

    # The released root is no longer exempt: a genuine decompose stall is parked.
    assert root not in summary.get("skipped_held", [])
    assert {"task_id": root, "class": "triage_decompose_failed"} in summary["parked"]
    assert root_task.status == "blocked"



def test_release_freigabe_hold_releases_transitive_chain_members(kanban_home):
    # Regression for operator PlanSpec approval: a sink/root can have only the
    # final review task as a direct parent while earlier build tasks are ancestors
    # of that review. Releasing only parent_ids(root) leaves the real first task
    # stuck in scheduled, so the chain appears to start as a single task.
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="held planspec root", triage=True, freigabe="operator")
        build = kb.create_task(conn, title="build", assignee="premium")
        review = kb.create_task(conn, title="review", assignee="reviewer", parents=[build])
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (review, root))
            conn.execute(
                "UPDATE tasks SET status='scheduled' WHERE id IN (?, ?, ?)",
                (root, build, review),
            )

        assert kb.release_freigabe_hold(conn, root) is True

        assert kb.get_task(conn, root).status == "todo"
        assert kb.get_task(conn, build).status == "ready"
        assert kb.get_task(conn, review).status == "todo"

def test_decompose_failure_is_transient_pure_rule():
    # HEILER-DECOMPOSE-FALLBACK-S1: pure classifier that tells a transient/infra
    # decompose failure (aux client down, LLM error, benign race) from a genuine
    # spec defect. The transient set is sourced from the ok=False reason strings
    # in kanban_decompose.decompose_task. Case-insensitive substring match; a
    # None/empty/unknown reason is NOT transient (defaults to the unchanged
    # bad-spec escalation, so a counter bumped without a reason behaves as before).
    transient = [
        "no auxiliary client configured",
        "auxiliary client unavailable",
        "LLM error: APITimeoutError",
        "LLM returned malformed JSON",
        "DB error: OperationalError",
        "task moved out of triage before promotion",
        "task moved out of triage before decomposition",
        "unknown task id",
        "task is not in triage (status='todo')",
        "decompose_task crashed: RuntimeError",
    ]
    for r in transient:
        assert kb._decompose_failure_is_transient(r), r
    genuine = [
        "decomposer returned fanout=false with no title/body",
        "decomposer returned fanout=true with empty tasks list",
        "tasks[0].title is missing or empty",
        "DB rejected graph: invalid assignee",
    ]
    for r in genuine:
        assert not kb._decompose_failure_is_transient(r), r
    assert not kb._decompose_failure_is_transient(None)
    assert not kb._decompose_failure_is_transient("")


def test_decompose_transient_failure_retries_then_parks_transient(kanban_home):
    # HEILER-DECOMPOSE-FALLBACK-S1 (AC-1): a decompose failure whose recorded
    # reason is transient/infra must NOT escalate as bad-spec. It runs through the
    # SAME bounded, backoff-spaced transient-retry budget the rate-limit loop uses
    # (the task stays in triage so the decomposer re-attempts it once the aux
    # client recovers — a success would reset the counter and escalate nothing);
    # only an EXHAUSTED budget escalates, and then classified TRANSIENT (infra),
    # never bad-spec. Sweeps step the logical clock; the per-attempt backoff is
    # keyed on the real-time event stamp (moot under a far-future ``now``, exactly
    # like the rate-limit test).
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="decompose triage", assignee="coder", triage=True,
        )
        # Record limit failures carrying a transient reason (aux client down).
        for _ in range(3):
            kb.record_decompose_failure(
                conn, tid, reason="no auxiliary client configured",
            )

        s1 = kb.no_silent_stall_sweep(conn, now=now)
        s2 = kb.no_silent_stall_sweep(conn, now=now + 1)
        # TRANSIENT_RETRY_LIMIT (=2) rounds spent → budget exhausted → parks.
        s3 = kb.no_silent_stall_sweep(conn, now=now + 2)

        task = kb.get_task(conn, tid)
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]
        heiler = [
            (e.payload or {}).get("class")
            for e in kb.list_events(conn, tid)
            if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]

    # Never parked or classified as bad-spec on any sweep.
    for s in (s1, s2, s3):
        assert {"task_id": tid, "class": "triage_decompose_failed"} not in s["parked"]
    # Retried within budget, then transient-parked exactly once.
    assert {"task_id": tid, "class": "triage_decompose_transient"} in s3["parked"]
    assert task.status == "blocked"
    assert len(escalations) == 1
    assert kb.HEILER_CLASS_TRANSIENT in heiler
    assert kb.HEILER_CLASS_BAD_SPEC not in heiler


def test_decompose_genuine_defect_still_parks_bad_spec(kanban_home):
    # HEILER-DECOMPOSE-FALLBACK-S1 (AC-2 guard): a genuine spec defect (the
    # decomposer engaged but the spec cannot be turned into work) must STILL
    # escalate as bad-spec — never silently pushed through as a single atomic
    # task. Unchanged path; this pins that the discrimination did not weaken it.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="vague triage", assignee="coder", triage=True,
        )
        for _ in range(3):
            kb.record_decompose_failure(
                conn, tid,
                reason="decomposer returned fanout=false with no title/body",
            )
        s = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        heiler = [
            (e.payload or {}).get("class")
            for e in kb.list_events(conn, tid)
            if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]
    assert {"task_id": tid, "class": "triage_decompose_failed"} in s["parked"]
    assert task.status == "blocked"
    assert kb.HEILER_CLASS_BAD_SPEC in heiler
    assert kb.HEILER_CLASS_TRANSIENT not in heiler


def test_decompose_bad_spec_park_reason_carries_cause(kanban_home):
    # The bad-spec park used to discard the already-read latest_reason,
    # escalating with an ursachenlose "auto_decompose failed N times" signature
    # (11x identical signatures on the live board in one week — the operator had
    # to open events every time to triage). _latest_decompose_failure_reason is
    # already read in this block for the transient check just above; surface it
    # in reason + evidence for the (non-transient) bad-spec park too.
    now = 1_900_000_000
    cause = "decomposer returned fanout=false with no title/body"
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="vague triage cause", assignee="coder", triage=True,
        )
        for _ in range(3):
            kb.record_decompose_failure(conn, tid, reason=cause)
        s = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]
        heiler = [
            (e.payload or {}).get("class")
            for e in kb.list_events(conn, tid)
            if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]

    assert {"task_id": tid, "class": "triage_decompose_failed"} in s["parked"]
    assert task.status == "blocked"
    assert len(escalations) == 1
    why_now = escalations[0].payload["why_now"]
    assert f"auto_decompose failed 3 times ({cause})" in why_now
    assert escalations[0].payload["evidence"]["latest_reason"] == cause
    # Classification stays bad-spec: the stall_class STRONG mapping (checked
    # before free-text signals in _classify_failure) wins regardless of what
    # the now-enriched reason text says.
    assert kb.HEILER_CLASS_BAD_SPEC in heiler
    assert kb.HEILER_CLASS_TRANSIENT not in heiler


def test_decompose_bad_spec_park_reason_truncates_long_cause(kanban_home):
    # done_when requires the surfaced cause to be truncated (~200 chars) so a
    # verbose decomposer error can't blow up the reason/evidence text.
    now = 1_900_000_000
    long_cause = (
        "decomposer returned fanout=false with no title/body: " + "x" * 250
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="vague triage long cause", assignee="coder", triage=True,
        )
        for _ in range(3):
            kb.record_decompose_failure(conn, tid, reason=long_cause)
        s = kb.no_silent_stall_sweep(conn, now=now)
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert {"task_id": tid, "class": "triage_decompose_failed"} in s["parked"]
    excerpt = escalations[0].payload["evidence"]["latest_reason"]
    assert excerpt == long_cause[:200]
    assert len(excerpt) == 200


def test_decompose_no_reason_event_preserves_bad_spec_park(kanban_home):
    # Back-compat: a decompose_failed counter bumped WITHOUT a reason (older code
    # path / direct counter use) has no decompose_attempt_failed event, so the
    # latest-reason lookup returns None → not transient → unchanged bad-spec park.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="no-reason triage", assignee="coder", triage=True,
        )
        for _ in range(3):
            kb.record_decompose_failure(conn, tid)  # no reason
        s = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
    assert {"task_id": tid, "class": "triage_decompose_failed"} in s["parked"]
    assert task.status == "blocked"


def test_decompose_no_reason_after_reset_overrides_stale_transient_reason(
    kanban_home,
):
    # Mixed-history guard: a successful/reset decompose attempt must create a new
    # boundary. Later no-reason failures are legacy/genuine-defect bumps and must
    # not inherit an older transient reason from before the reset.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="mixed-history triage", assignee="coder", triage=True,
        )
        kb.record_decompose_failure(
            conn, tid, reason="no auxiliary client configured",
        )
        kb.reset_decompose_failed(conn, tid)

        for _ in range(3):
            kb.record_decompose_failure(conn, tid)  # no reason after reset

        s = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        heiler = [
            (e.payload or {}).get("class")
            for e in kb.list_events(conn, tid)
            if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]

    assert {"task_id": tid, "class": "triage_decompose_failed"} in s["parked"]
    assert {"task_id": tid, "class": "triage_decompose_transient"} not in s["transient_retried"]
    assert task.status == "blocked"
    assert kb.HEILER_CLASS_BAD_SPEC in heiler
    assert kb.HEILER_CLASS_TRANSIENT not in heiler


def test_4a_rate_limited_loop_retries_then_parks(kanban_home):
    # HEILER-TRANSIENT-RETRY-BUDGET-S1: a persistent rate-limit loop is transient
    # infra, so it now runs through a BOUNDED, backoff-spaced retry budget before
    # paging the operator (instead of escalating on the first detection). Each
    # retry round emits a ``transient_retry`` event (NOT a heiler_classification),
    # so a root that self-heals within budget never lands a transient escalation;
    # only the EXHAUSTED budget escalates — exactly once.
    # Sweeps step the logical clock forward; the per-attempt backoff is keyed on
    # the real-time event stamp (so it bites in production but is moot under a
    # far-future test ``now``, exactly like the §5 integration-retry tests). The
    # backoff rule itself is covered by test_transient_retry_phase_pure_rule.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="quota loop", assignee="coder")
        with kb.write_txn(conn):
            for i in range(3):
                conn.execute(
                    "INSERT INTO task_runs "
                    "(task_id, profile, status, outcome, error, started_at, ended_at) "
                    "VALUES (?, 'coder', 'rate_limited', 'rate_limited', "
                    "'429 quota', ?, ?)",
                    (tid, now - 100 - i, now - 90 - i),
                )
        # Rounds 1..LIMIT: retry, NOT park.
        retried = []
        for k in range(kb.TRANSIENT_RETRY_LIMIT):
            s = kb.no_silent_stall_sweep(
                conn, now=now + k, rate_limit_attempt_limit=3,
            )
            retried.append(s)
        t_mid = kb.get_task(conn, tid)
        # Budget exhausted: escalate exactly once.
        s_park = kb.no_silent_stall_sweep(
            conn, now=now + kb.TRANSIENT_RETRY_LIMIT, rate_limit_attempt_limit=3,
        )
        t_park = kb.get_task(conn, tid)
        # Idempotent: no second escalation.
        s_after = kb.no_silent_stall_sweep(
            conn, now=now + kb.TRANSIENT_RETRY_LIMIT + 1, rate_limit_attempt_limit=3,
        )
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]
        heiler = [e for e in kb.list_events(conn, tid)
                  if e.kind == kb.HEILER_CLASSIFICATION_EVENT]

    # Every round below the limit retried, stayed ready, no escalation/park.
    for s in retried:
        assert {"task_id": tid, "class": "rate_limited_loop"} in s["transient_retried"]
        assert s["parked"] == []
    assert t_mid.status == "ready"
    assert t_mid.transient_retry_count == kb.TRANSIENT_RETRY_LIMIT
    # Budget spent → escalate once, byte-identically to the old park path.
    assert {"task_id": tid, "class": "rate_limited_loop"} in s_park["parked"]
    assert t_park.status == "blocked"
    assert s_after["parked"] == [] and s_after["transient_retried"] == []
    assert len(escalations) == 1
    assert escalations[0].payload["attempts_already_made"] == 3
    # The retries themselves never classified the root as a transient escalation;
    # only the final park did (the AC-1 ledger reduction lever).
    assert kb.TRANSIENT_RETRY_EVENT in kinds
    assert len(heiler) == 1
    assert heiler[0].payload["class"] == "transient"


# ---------------------------------------------------------------------------
# Heiler: transient re-integration retry lane (no_silent_stall_sweep §5)
# ---------------------------------------------------------------------------

def _make_integration_parked(conn, reason_suffix, *, title="parked finalizer"):
    """Create a task blocked with an ``integration parked: <reason>`` event."""
    tid = kb.create_task(conn, title=title, assignee="coder")
    kb.claim_task(conn, tid)
    kb.block_task(conn, tid, reason=f"integration parked: {reason_suffix}")
    return tid


def test_park_integration_comments_dirty_artifact_recovery(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="artifact policy miss", assignee="coder")
        kb.claim_task(conn, tid)
        task = kb.get_task(conn, tid)
        assert task is not None
        ok = kb._park_integration(
            conn,
            tid,
            {
                "action": "parked",
                "reason": (
                    "ARTIFACT_POLICY_MISSING: coverage/index.html. "
                    "Recovery: extend the artifact policy."
                ),
                "branch": "kanban/t_artifact_policy",
                "park_class": "ARTIFACT_POLICY_MISSING",
                "dirty_files": ["coverage/index.html"],
            },
            expected_run_id=task.current_run_id,
        )
        comments = conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ?",
            (tid,),
        ).fetchall()

    assert ok is True
    assert comments[-1]["author"] == "integrator"
    assert "ARTIFACT_POLICY_MISSING" in comments[-1]["body"]
    assert "coverage/index.html" in comments[-1]["body"]
    assert "Recovery: extend the artifact policy" in comments[-1]["body"]
    assert "worker contract" not in comments[-1]["body"]


def _patch_integrate(monkeypatch, outcomes):
    """Patch maybe_integrate_on_complete; record call task_ids.

    ``outcomes`` may be a list (popped per call) or a callable(task_id)->dict.
    """
    import hermes_cli.kanban_worktrees as kwt
    calls = []

    def fake(conn, task_id, **kw):
        calls.append(task_id)
        if callable(outcomes):
            return outcomes(task_id)
        return outcomes.pop(0) if outcomes else None

    monkeypatch.setattr(kwt, "maybe_integrate_on_complete", fake)
    return calls


def test_integration_retry_skips_active_operator_escalation(
    kanban_home, monkeypatch,
):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = _make_integration_parked(
            conn, "chain worktree has uncommitted changes: foo.py",
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                tid,
                kb.OPERATOR_ESCALATION_EVENT,
                {
                    "task": {"id": tid, "title": "parked finalizer"},
                    "why_now": "operator must decide whether to retry integration",
                    "attempts_already_made": 1,
                    "evidence": {"reason": "integration parked"},
                    "recommended_human_action": "inspect held integration park",
                },
            )
        calls = _patch_integrate(monkeypatch, [{
            "action": "merged", "branch": "kanban/chain-x",
            "merge_commit": "abc123def456", "target": "main",
        }])
        summary = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)

    assert calls == []
    assert task.status == "blocked"
    assert tid in summary.get("skipped_held", [])
    assert summary["self_healed"] == []


def test_integration_retry_transient_park_reintegrates_and_completes(
    kanban_home, monkeypatch,
):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = _make_integration_parked(
            conn, "chain worktree has uncommitted changes: foo.py",
        )
        calls = _patch_integrate(monkeypatch, [{
            "action": "merged", "branch": "kanban/chain-x",
            "merge_commit": "abc123def456", "target": "main",
        }])
        summary = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]

    assert calls == [tid]                       # re-integration WAS attempted
    assert task.status == "done"                # completed, NOT ready
    assert task.integration_retry_count == 1
    assert kb.INTEGRATION_RETRY_EVENT in kinds
    assert kb.INTEGRATION_RETRY_SUCCEEDED_EVENT in kinds
    assert "operator_escalation" not in kinds   # no premature escalation
    assert {
        "task_id": tid, "class": "integration_retry", "action": "reintegrated",
    } in summary["self_healed"]


@pytest.mark.parametrize("reason_suffix", [
    "merge conflict/failure (aborted): foo.py",
    "post-merge gate failed: vitest 3 failing",
])
def test_integration_retry_non_transient_no_worktree_escalates(
    kanban_home, monkeypatch, reason_suffix,
):
    # needs_orchestrator park WITHOUT a provisioned worktree to fix in (the
    # park reason here is on a scratch finalizer): no transient retry AND no
    # fixer to route to → escalate byte-identically to the needs_operator path.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = _make_integration_parked(conn, reason_suffix)
        calls = _patch_integrate(monkeypatch, [])  # any call would be a bug
        summary = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        escalations = [k for k in kinds if k == kb.OPERATOR_ESCALATION_EVENT]

    assert calls == []                          # merge conflict/red gate: NO retry
    assert task.status == "blocked"
    assert task.integration_retry_count == 0
    assert len(escalations) == 1               # classified + escalated
    # No worktree → no fixer dispatched (byte-equal to the old escalate path).
    assert kb.CONFLICT_FIXER_DISPATCHED_EVENT not in kinds
    assert summary["conflict_fixer_dispatched"] == []
    assert {"task_id": tid, "class": "integration_parked"} in summary["parked"]


def test_integration_retry_count_separate_from_auto_retry_count(
    kanban_home, monkeypatch,
):
    now = 1_900_000_000
    reason = "dirty files in live checkout overlap the branch diff: a.py"
    with kb.connect_closing() as conn:
        tid = _make_integration_parked(conn, reason)
        # Re-park (still transient) so the task stays blocked and we can read
        # the counters without it completing.
        _patch_integrate(monkeypatch, [{"action": "parked", "reason": reason}])
        kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)

    assert task.integration_retry_count == 1    # OWN counter advanced
    assert task.auto_retry_count == 0           # shared premium/opus ladder untouched
    assert task.status == "blocked"             # re-parked, NOT ready


def test_integration_retry_bounded_escalates_after_two_rounds(
    kanban_home, monkeypatch,
):
    reason = (
        "live checkout has an operation in progress (rebase): "
        ".git/rebase-merge"
    )
    with kb.connect_closing() as conn:
        tid = _make_integration_parked(conn, reason)
        calls = _patch_integrate(
            monkeypatch, lambda _t: {"action": "parked", "reason": reason},
        )
        s1 = kb.no_silent_stall_sweep(conn, now=1_900_000_000)
        t1 = kb.get_task(conn, tid)
        s2 = kb.no_silent_stall_sweep(conn, now=1_900_000_100)
        t2 = kb.get_task(conn, tid)
        s3 = kb.no_silent_stall_sweep(conn, now=1_900_000_200)
        t3 = kb.get_task(conn, tid)
        s4 = kb.no_silent_stall_sweep(conn, now=1_900_000_300)
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert t1.integration_retry_count == 1 and t1.status == "blocked"
    assert t2.integration_retry_count == 2 and t2.status == "blocked"
    assert len(calls) == 2                       # only 2 transient retry rounds
    assert t3.status == "blocked"                # bounded — never ready
    assert len(escalations) == 1                 # escalated exactly once (round 3)
    assert {
        "task_id": tid, "class": "integration_retry_exhausted",
    } in s3["parked"]
    assert s4["parked"] == []                    # idempotent: no 2nd escalation
    assert s1["parked"] == [] and s2["parked"] == []


def test_integration_retry_repark_turned_non_transient_escalates(
    kanban_home, monkeypatch,
):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = _make_integration_parked(
            conn, "chain worktree has uncommitted changes: foo.py",
        )
        # First (and only) attempt re-parks with a NON-transient reason.
        calls = _patch_integrate(monkeypatch, [{
            "action": "parked",
            "reason": "merge conflict/failure (aborted): foo.py",
        }])
        s1 = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert calls == [tid]                        # one attempt happened
    assert task.integration_retry_count == 1
    assert task.status == "blocked"
    assert len(escalations) == 1                 # reclassified → escalate, stop
    assert {"task_id": tid, "class": "integration_parked"} in s1["parked"]


# ---------------------------------------------------------------------------
# Heiler: GENERAL bounded transient-INFRA retry lane (HEILER-TRANSIENT-RETRY-
# BUDGET-S1). spawn_failed / rate_limited_loop / scheduled_overdue run through a
# bounded, backoff-spaced retry budget (OWN counter, never auto_retry_count /
# integration_retry_count) before paging the operator.
# ---------------------------------------------------------------------------

def test_transient_retry_phase_pure_rule():
    bo = kb.TRANSIENT_RETRY_BACKOFF_SECONDS
    lim = kb.TRANSIENT_RETRY_LIMIT
    # No prior attempt → retry.
    assert kb._transient_retry_phase(0, None, 1000) == "retry"
    # Inside backoff window → backoff.
    assert kb._transient_retry_phase(1, 1000, 1000 + bo - 1) == "backoff"
    # Backoff elapsed → retry.
    assert kb._transient_retry_phase(1, 1000, 1000 + bo + 1) == "retry"
    # Budget spent → exhausted (even with no recent event).
    assert kb._transient_retry_phase(lim, None, 10**12) == "exhausted"


def test_transient_retry_spawn_bounded_then_escalates(kanban_home):
    # The spawn-dispatch helper re-queues a claimed (running) task running→ready
    # up to TRANSIENT_RETRY_LIMIT times — emitting a transient_retry event and
    # NOT a heiler_classification — then falls back to the normal spawn-failure
    # escalation. Driven directly (re-claim each round) so the test does not
    # depend on the real-time backoff (that lives in check_respawn_guard).
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="flaky spawn", assignee="coder", max_retries=1)
        phases = []
        for _ in range(kb.TRANSIENT_RETRY_LIMIT + 1):
            assert kb.claim_task(conn, tid) is not None      # ready → running + run
            phase, auto = kb._spawn_failure_or_transient_retry(
                conn, tid, "spawn boom", failure_limit=1, now=now,
            )
            phases.append((phase, auto))
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        tretry_runs = conn.execute(
            "SELECT COUNT(*) AS n FROM task_runs "
            "WHERE task_id = ? AND outcome = ?",
            (tid, kb.TRANSIENT_RETRY_OUTCOME),
        ).fetchone()["n"]

    # First N rounds re-queued; the (N+1)-th exhausted the budget and escalated.
    assert phases[:-1] == [("retried", False)] * kb.TRANSIENT_RETRY_LIMIT
    assert phases[-1][0] == "escalated"
    assert task.transient_retry_count == kb.TRANSIENT_RETRY_LIMIT
    # OWN counter only — the premium/opus + re-integration ladders are untouched.
    assert task.auto_retry_count == 0
    assert task.integration_retry_count == 0
    assert task.status == "blocked"                          # finally escalated
    assert tretry_runs == kb.TRANSIENT_RETRY_LIMIT
    # Bounded retries emit transient_retry, NOT heiler_classification: a self-
    # heal within budget never lands a transient escalation in the ledger. The
    # single heiler_classification belongs to the final (exhausted) escalation.
    assert kb.TRANSIENT_RETRY_EVENT in kinds
    heiler = [e for e in events if e.kind == kb.HEILER_CLASSIFICATION_EVENT]
    assert len(heiler) == 1 and heiler[0].payload["class"] == "transient"
    assert len([e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]) == 1


def test_transient_retry_self_heal_leaves_no_transient_escalation(kanban_home):
    # A spawn blip that self-heals on the next attempt must NOT show up as a
    # transient escalation in the ledger — the whole AC-1 reduction lever.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="blip", assignee="coder")
        assert kb.claim_task(conn, tid) is not None
        phase, _auto = kb._spawn_failure_or_transient_retry(
            conn, tid, "spawn boom", failure_limit=2, now=now,
        )
        assert phase == "retried"
        task = kb.get_task(conn, tid)
        # Next attempt succeeds.
        assert kb.claim_task(conn, tid) is not None
        kb.complete_task(conn, tid, summary="ok")
        done = kb.get_task(conn, tid)
        ledger = kb.read_escalation_ledger(conn)
        kinds = [e.kind for e in kb.list_events(conn, tid)]

    assert task.status == "ready" and task.transient_retry_count == 1
    # Completed: the transient budget resets so a later, unrelated blip starts
    # from a clean slate.
    assert done.status == "done" and done.transient_retry_count == 0
    assert "operator_escalation" not in kinds
    assert kb.HEILER_CLASSIFICATION_EVENT not in kinds
    assert ledger["roots_by_class"].get("transient", 0) == 0


def test_transient_retry_backoff_guard_defers_then_releases(kanban_home):
    # check_respawn_guard spaces the bounded spawn retries apart: a task whose
    # latest run ended transient_retry is deferred inside the backoff window and
    # respawnable once it elapses.
    real_now = int(time.time())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, outcome, started_at, ended_at) "
                "VALUES (?, 'coder', ?, ?, ?, ?)",
                (tid, kb.TRANSIENT_RETRY_OUTCOME, kb.TRANSIENT_RETRY_OUTCOME,
                 real_now - 5, real_now - 5),
            )
        assert kb.check_respawn_guard(conn, tid) == "transient_retry_backoff"
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET ended_at = ? WHERE task_id = ?",
                (real_now - kb.TRANSIENT_RETRY_BACKOFF_SECONDS - 5, tid),
            )
        assert kb.check_respawn_guard(conn, tid) is None


def test_transient_retry_dispatch_end_to_end(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    # End-to-end through dispatch_once: a persistently-raising spawn_fn is
    # re-queued a bounded number of times, then escalates. Backoff is collapsed
    # so the loop can exhaust the budget without wall-clock waits.
    monkeypatch.setattr(kb, "TRANSIENT_RETRY_BACKOFF_SECONDS", 0)

    def boom(task, workspace):
        raise RuntimeError("spawn boom")

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="flaky", assignee="alice", max_retries=1)
        for _ in range(kb.TRANSIENT_RETRY_LIMIT + 3):
            kb.dispatch_once(conn, spawn_fn=boom)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    tretries = [e for e in events if e.kind == kb.TRANSIENT_RETRY_EVENT]
    escalations = [e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT]
    assert len(tretries) == kb.TRANSIENT_RETRY_LIMIT     # bounded re-queues
    assert task.status == "blocked"                      # then escalated
    assert len(escalations) == 1


def test_scheduled_overdue_failed_nudge_retries_then_escalates(
    kanban_home, monkeypatch,
):
    # When the deterministic scheduled-overdue nudge cannot apply (forced here),
    # the sweep runs a bounded transient-retry budget before escalating, instead
    # of paging on the first failed nudge.
    now = 1_900_000_000
    bo = kb.TRANSIENT_RETRY_BACKOFF_SECONDS
    monkeypatch.setattr(kb, "unblock_task", lambda conn, task_id: False)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="overdue", assignee="coder")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'scheduled', created_at = ? WHERE id = ?",
                (now - 10_000, tid),
            )
        s1 = kb.no_silent_stall_sweep(conn, now=now, min_age_seconds=3600)
        t1 = kb.get_task(conn, tid)
        s2 = kb.no_silent_stall_sweep(conn, now=now + bo + 1, min_age_seconds=3600)
        s3 = kb.no_silent_stall_sweep(conn, now=now + 2 * bo + 2, min_age_seconds=3600)
        t3 = kb.get_task(conn, tid)
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert {"task_id": tid, "class": "scheduled_overdue"} in s1["transient_retried"]
    assert t1.status == "scheduled" and t1.transient_retry_count == 1
    assert {"task_id": tid, "class": "scheduled_overdue"} in s2["transient_retried"]
    assert {"task_id": tid, "class": "scheduled_overdue"} in s3["parked"]
    assert t3.status == "blocked"
    assert len(escalations) == 1


# ---------------------------------------------------------------------------
# CONFLICT-PARK-FIXER-ROUTING: needs_orchestrator parks (real merge conflict /
# red post-merge gate) WITH a provisioned worktree route to a BOUNDED
# coder-claude fixer instead of escalating straight to the operator.
# ---------------------------------------------------------------------------

def _make_integration_parked_in_worktree(
    conn,
    reason_suffix,
    *,
    repo=None,
    root="t_chainroot",
    create_worktree=True,
):
    """A parked finalizer whose workspace_path is a provisioned chain worktree,
    so the non-transient branch can route a fixer into it."""
    tid = kb.create_task(conn, title="parked finalizer", assignee="coder")
    kb.claim_task(conn, tid)
    kb.block_task(conn, tid, reason=f"integration parked: {reason_suffix}")
    repo_path = Path(repo) if repo is not None else Path(os.environ["HERMES_HOME"]) / "repo"
    wt = str(repo_path / ".worktrees" / "kanban" / root)
    if create_worktree:
        Path(wt).mkdir(parents=True, exist_ok=True)
    kb.set_workspace_path(conn, tid, wt)
    return tid, wt, root


def _close_task(conn, task_id, status="failed"):
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id),
        )


@pytest.mark.parametrize("reason_suffix", [
    "merge conflict/failure (aborted): foo.py",
    "post-merge gate failed: vitest 3 failing",
])
def test_conflict_park_routes_bounded_fixer_not_escalation(
    kanban_home, monkeypatch, reason_suffix,
):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid, wt, root = _make_integration_parked_in_worktree(conn, reason_suffix)
        calls = _patch_integrate(monkeypatch, [])  # transient retry would be a bug
        summary = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        dispatched = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.CONFLICT_FIXER_DISPATCHED_EVENT
        ]
        # The fixer subtask itself (payload is already a parsed dict).
        child_id = dispatched[0].payload["child_id"]
        child = kb.get_task(conn, child_id)
        child_kinds = [e.kind for e in kb.list_events(conn, child_id)]

    assert calls == []                                 # no transient retry
    assert task.status == "blocked"                    # parked chain stays blocked
    assert task.integration_retry_count == 0           # transient counter untouched
    # Routed to a fixer, NOT escalated.
    assert kb.OPERATOR_ESCALATION_EVENT not in kinds
    assert {"task_id": tid, "class": "integration_parked"} not in summary["parked"]
    assert len(dispatched) == 1
    assert summary["conflict_fixer_dispatched"] == [
        {"task_id": tid, "child_id": child_id, "attempt": 1}
    ]
    # The fixer is a dispatchable Claude-coder task pinned to the chain worktree.
    # Phase A: the coder-claude lane folds into premium (same claude-cli/Opus runtime).
    assert child.assignee == "premium"
    assert child.status == "ready"
    assert child.workspace_kind == "dir"
    assert child.workspace_path == wt
    assert root in child.title
    # Linked back to the stalled chain on both ends.
    assert f"kanban/{root}" in (child.body or "")   # chain branch in context
    assert "conflict_fixer_for" in child_kinds


def test_conflict_park_missing_worktree_escalates_not_fixer(
    kanban_home, monkeypatch, tmp_path,
):
    """A stale provisioned-path string is not enough to route a fixer.

    Live failure evidence showed workers launched against
    ``.worktrees/kanban/<root>`` paths that no longer existed, then failing with
    ``fatal: cannot change to ...``. The sweep should page the operator instead
    of creating another task pinned to a missing cwd.
    """
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        missing_repo = tmp_path / "repo"
        tid, _wt, _root = _make_integration_parked_in_worktree(
            conn,
            "merge conflict/failure (aborted): foo.py",
            repo=str(missing_repo),
            create_worktree=False,
        )
        calls = _patch_integrate(monkeypatch, [])
        summary = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        escalations = [k for k in kinds if k == kb.OPERATOR_ESCALATION_EVENT]

    assert calls == []
    assert task.status == "blocked"
    assert len(escalations) == 1
    assert kb.CONFLICT_FIXER_DISPATCHED_EVENT not in kinds
    assert summary["conflict_fixer_dispatched"] == []
    assert {"task_id": tid, "class": "integration_parked"} in summary["parked"]


def test_conflict_park_fixer_not_stacked_while_in_flight(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid, _wt, _root = _make_integration_parked_in_worktree(
            conn, "merge conflict/failure (aborted): foo.py",
        )
        _patch_integrate(monkeypatch, [])
        s1 = kb.no_silent_stall_sweep(conn, now=1_900_000_000)
        # The fixer from round 1 is still open (ready) → round 2 must NOT
        # dispatch a second one.
        s2 = kb.no_silent_stall_sweep(conn, now=1_900_000_500)
        dispatched = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.CONFLICT_FIXER_DISPATCHED_EVENT
        ]

    assert len(s1["conflict_fixer_dispatched"]) == 1
    assert s2["conflict_fixer_dispatched"] == []        # waited on the in-flight fixer
    assert s2["parked"] == []                            # not escalated yet
    assert len(dispatched) == 1                          # exactly one fixer exists


def test_conflict_park_fixer_bounded_then_escalates(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "CONFLICT_FIXER_MAX_ATTEMPTS", 2)
    with kb.connect_closing() as conn:
        tid, _wt, _root = _make_integration_parked_in_worktree(
            conn, "merge conflict/failure (aborted): foo.py",
        )
        _patch_integrate(monkeypatch, [])

        def _sweep_and_close(ts):
            s = kb.no_silent_stall_sweep(conn, now=ts)
            for entry in s["conflict_fixer_dispatched"]:
                _close_task(conn, entry["child_id"])  # fixer ran, didn't resolve
            return s

        s1 = _sweep_and_close(1_900_000_000)            # attempt 1
        s2 = _sweep_and_close(1_900_000_500)            # attempt 2
        s3 = kb.no_silent_stall_sweep(conn, now=1_900_001_000)  # budget spent
        s4 = kb.no_silent_stall_sweep(conn, now=1_900_001_500)  # idempotent
        task = kb.get_task(conn, tid)
        dispatched = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.CONFLICT_FIXER_DISPATCHED_EVENT
        ]
        escalations = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]

    assert len(s1["conflict_fixer_dispatched"]) == 1     # attempt 1
    assert len(s2["conflict_fixer_dispatched"]) == 1     # attempt 2
    assert len(dispatched) == 2                          # bounded at MAX attempts
    assert s3["conflict_fixer_dispatched"] == []         # no 3rd fixer
    assert {"task_id": tid, "class": "integration_parked"} in s3["parked"]
    assert len(escalations) == 1                         # unresolvable → escalate once
    assert task.status == "blocked"
    assert s4["parked"] == []                            # idempotent: no 2nd escalation


def test_conflict_park_needs_operator_unchanged(kanban_home, monkeypatch):
    # An unknown (needs_operator) park is byte-unchanged: it escalates with NO
    # fixer routed, even when a worktree is present.
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid, _wt, _root = _make_integration_parked_in_worktree(
            conn, "some entirely unrecognized park reason",
        )
        calls = _patch_integrate(monkeypatch, [])
        summary = kb.no_silent_stall_sweep(conn, now=now)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        escalations = [k for k in kinds if k == kb.OPERATOR_ESCALATION_EVENT]

    assert calls == []
    assert task.status == "blocked"
    assert len(escalations) == 1
    assert kb.CONFLICT_FIXER_DISPATCHED_EVENT not in kinds
    assert summary["conflict_fixer_dispatched"] == []
    assert {"task_id": tid, "class": "integration_parked"} in summary["parked"]


def test_4a_funnel_root_skipped_but_funnel_build_child_dispatches(
    kanban_home, all_assignees_spawnable, tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    spawned = []

    def fake_spawn(task, workspace):
        spawned.append(task.id)

    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn, title="funnel root", assignee="research", created_by="family",
        )
        done_root = kb.create_task(
            conn, title="approved root", assignee="research", created_by="family",
        )
        kb.claim_task(conn, done_root)
        kb.complete_task(conn, done_root, summary="draft done")
        child = kb.create_task(
            conn,
            title="approved build child",
            assignee="coder",
            created_by="family",
            parents=(done_root,),
            kind="code",
            workspace_kind="dir",
            workspace_path=str(repo),
        )

        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        root_task = kb.get_task(conn, root)
        child_task = kb.get_task(conn, child)

    assert root_task.status == "ready"
    assert child_task.status == "running"
    assert child in spawned
    assert (root, "funnel_protected") in result.respawn_guarded


def test_4a_funnel_build_child_not_blocked_by_root_contract_rule(kanban_home):
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn,
            title="approved root",
            assignee="research",
            created_by="discord-idee",
        )
        kb.claim_task(conn, root)
        kb.complete_task(conn, root, summary="draft approved")
        child = kb.create_task(
            conn,
            title="approved scratch build child",
            assignee="coder",
            created_by="discord-idee",
            parents=(root,),
            kind="code",
        )
        task = kb.get_task(conn, child)
        events = kb.list_events(conn, child)

    assert task is not None
    assert task.status == "ready"
    assert [e for e in events if e.kind == "needs_contract"] == []
    assert [e for e in events if e.kind == "code_task_contract_inferred"]


def test_4a_auto_retry_skips_funnel_root_but_not_funnel_child(
    kanban_home,
):
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn,
            title="blocked funnel root",
            assignee="research",
            created_by="family",
        )
        kb.claim_task(conn, root)
        kb.block_task(conn, root, reason="transient")

        done_root = kb.create_task(
            conn,
            title="done funnel root",
            assignee="research",
            created_by="family",
        )
        kb.claim_task(conn, done_root)
        kb.complete_task(conn, done_root, summary="draft done")
        child = kb.create_task(
            conn,
            title="blocked funnel child",
            assignee="research",
            created_by="family",
            parents=(done_root,),
        )
        kb.claim_task(conn, child)
        kb.block_task(conn, child, reason="transient")

        retried = kb.auto_retry_blocked_tasks(conn, backoff_seconds=0)
        root_task = kb.get_task(conn, root)
        child_task = kb.get_task(conn, child)

    assert retried == [(child, 1)]
    assert root_task.status == "blocked"
    assert child_task.status == "ready"


def test_4a_dispatcher_heartbeat_file_written(kanban_home):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="needs operator", assignee="coder")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                tid,
                kb.OPERATOR_ESCALATION_EVENT,
                {
                    "task": {"id": tid, "title": "needs operator"},
                    "why_now": "test",
                    "attempts_already_made": 1,
                    "evidence": {},
                    "recommended_human_action": "inspect",
                    "blocked_action_boundary": list(kb.OPERATOR_ONLY_ACTIONS),
                },
            )

    payload = kb.write_kanban_dispatcher_heartbeat(now=now, tick_health="ok")
    path = kb.kanban_dispatcher_heartbeat_path()
    written = json.loads(path.read_text(encoding="utf-8"))

    assert path.name == kb.KANBAN_DISPATCHER_HEARTBEAT_FILENAME
    assert payload["last_tick_at"] == now
    assert payload["last_green_gate_at"] == now
    assert written["counts"]["open_escalations"] == 1


# ---------------------------------------------------------------------------
# dispatch_once — max_in_progress
# ---------------------------------------------------------------------------


def test_dispatch_max_in_progress_skips_when_at_limit(kanban_home, all_assignees_spawnable):
    """When max_in_progress=N and N tasks are already running, spawn nothing."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect_closing() as conn:
        # Two running tasks.
        t1 = kb.create_task(conn, title="a", assignee="alice")
        t2 = kb.create_task(conn, title="b", assignee="bob")
        kb.claim_task(conn, t1)
        kb.claim_task(conn, t2)
        # Two more ready to spawn — but cap is 2 so none should fire.
        kb.create_task(conn, title="c", assignee="bob")
        kb.create_task(conn, title="d", assignee="alice")
        kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=2)

    assert len(spawns) == 0, f"expected 0 spawns, got {len(spawns)}"


def test_dispatch_max_in_progress_spawns_up_to_cap(kanban_home, all_assignees_spawnable):
    """When max_in_progress=3 and only 1 is running, spawn up to 2 more."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect_closing() as conn:
        # One running task.
        t1 = kb.create_task(conn, title="a", assignee="alice")
        kb.claim_task(conn, t1)
        # Three ready tasks — only the first 2 should be spawned.
        kb.create_task(conn, title="b", assignee="bob")
        kb.create_task(conn, title="c", assignee="bob")
        kb.create_task(conn, title="d", assignee="bob")
        kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=3)

    assert len(spawns) == 2, f"expected 2 spawns (cap 3 - 1 running), got {len(spawns)}"


def test_dispatch_max_in_progress_none_is_unlimited(kanban_home, all_assignees_spawnable):
    """Default None means no limit — all ready tasks are spawned."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect_closing() as conn:
        for title in ["a", "b", "c", "d"]:
            kb.create_task(conn, title=title, assignee="alice")
        kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=None)

    assert len(spawns) == 4, f"expected 4 spawns (unlimited), got {len(spawns)}"

# Review column dispatch
# ---------------------------------------------------------------------------


def _set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    """Test helper: set a task's status directly."""
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def test_claim_review_task_transitions_to_running(kanban_home):
    """claim_review_task atomically transitions review -> running."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.claim_lock is not None


@pytest.mark.parametrize(
    ("provider", "model", "expected_billing_mode", "expected_subscription", "expected_cost_source"),
    [
        ("openrouter", "openai/gpt-5-mini", "metered", None, "dispatch_metered_stamp"),
        ("openai-codex", "gpt-5.5", "subscription_included", "chatgpt", "dispatch_subscription_stamp"),
    ],
)
def test_claim_review_task_stamps_billing_identity_from_reviewer_lane(
    kanban_home,
    monkeypatch,
    provider,
    model,
    expected_billing_mode,
    expected_subscription,
    expected_cost_source,
):
    """review -> running verifier runs must be self-describing too."""
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: None)
    with kb.connect_closing() as conn:
        lane = kb.create_lane(
            conn,
            name=f"review-{expected_billing_mode}",
            profiles={"verifier": {
                "worker_runtime": "hermes",
                "provider": provider,
                "model": model,
            }},
        )
        kb.activate_lane(conn, lane["id"])
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")

        claimed = kb.claim_review_task(conn, t, reviewer_profile="verifier")
        assert claimed is not None
        row = conn.execute(
            "SELECT profile, metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()

    assert row["profile"] == "verifier"
    meta = json.loads(row["metadata"])
    assert meta["worker_runtime"] == "hermes"
    assert meta["provider"] == provider
    assert meta["model"] == model
    assert meta["billing_mode"] == expected_billing_mode
    assert meta["cost_source"] == expected_cost_source
    if expected_subscription is None:
        assert "subscription" not in meta
    else:
        assert meta["subscription"] == expected_subscription


def test_review_claimed_full_context_retry_uses_retry_profile_caps(kanban_home):
    """review -> running verifier continuations use retry caps with profile='full'."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review continuation", assignee="coder")
        _set_task_status(conn, t, "review")
        conn.execute("UPDATE tasks SET continuation_count=1 WHERE id=?", (t,))
        for idx in range(kb._CTX_CAP_PROFILES["retry"]["prior_attempts"] + 2):
            _seed_completed_run(conn, t, "verifier", 1_800_000_000 + idx, f"review-summary-{idx}")
        conn.commit()

        claimed = kb.claim_review_task(conn, t, reviewer_profile="verifier")
        ctx = kb.build_worker_context(conn, t, profile="full")

    assert claimed is not None
    assert claimed.assignee == "coder"
    assert "This is continuation run 1/" in ctx
    assert f"showing most recent {kb._CTX_CAP_PROFILES['retry']['prior_attempts']}" in ctx
    assert "review-summary-0" not in ctx
    assert "review-summary-2" in ctx


def test_claim_review_task_clears_inherited_heartbeat(kanban_home):
    """review -> running must reset last_heartbeat_at.

    Regression: a stage whose worker does not self-heartbeat (the claude-CLI
    verifier/reviewer runs) otherwise inherits the previous (coder) stage's
    last beat. That stale timestamp ages past the dashboard's stuck threshold
    and shows an actively-running review as "Hängt". A fresh run must start
    with a NULL heartbeat (liveness via claim_expires, like any other
    non-self-beating worker)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        # Previous stage's lingering heartbeat.
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (1_000_000, t),
        )
        conn.commit()
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None and claimed.status == "running"
        hb = conn.execute(
            "SELECT last_heartbeat_at FROM tasks WHERE id = ?", (t,)
        ).fetchone()[0]
    assert hb is None


def test_claim_task_clears_inherited_heartbeat(kanban_home):
    """ready -> running starts the run with a clean heartbeat slate."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="claim me", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (1_000_000, t),
        )
        conn.commit()
        claimed = kb.claim_task(conn, t)
        assert claimed is not None and claimed.status == "running"
        hb = conn.execute(
            "SELECT last_heartbeat_at FROM tasks WHERE id = ?", (t,)
        ).fetchone()[0]
    assert hb is None


def test_claim_review_task_fails_on_non_review(kanban_home):
    """claim_review_task returns None if task is not in review status."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="ready task", assignee="alice")
        # Task is in 'ready', not 'review'
        claimed = kb.claim_review_task(conn, t)
    assert claimed is None


def test_claim_review_task_fails_when_already_claimed(kanban_home):
    """claim_review_task returns None if the task was already claimed."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        first = kb.claim_review_task(conn, t)
        assert first is not None
        second = kb.claim_review_task(conn, t)
    assert second is None


def test_dispatch_review_dry_run(kanban_home, all_assignees_spawnable):
    """dispatch_once dry-run sees review tasks and reports them as spawned."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, dry_run=True)
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == t
    # Dry run must NOT mutate status.
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, t).status == "review"


def test_dispatch_review_spawns_as_verifier_profile(
    kanban_home, all_assignees_spawnable,
):
    """Review tasks spawn as the independent ``verifier`` profile — not the
    task's own (code-writing) assignee — and without forcing the historical
    ``sdlc-review`` skill (which does not exist in this tree). The DB
    ``assignee`` is left unchanged so a REQUEST_CHANGES keeps the task owned
    by the original coder for the follow-up fix."""
    spawned_tasks = []

    def capture_spawn(task, workspace, board=None):
        spawned_tasks.append(task)
        return 42  # fake PID

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, spawn_fn=capture_spawn)
        # DB assignee is unchanged (override is in-memory, for the spawn only).
        assert kb.get_task(conn, t).assignee == "alice"
        run = kb.list_runs(conn, t)[0]
        assert run.profile == "verifier"
    assert len(res.spawned) == 1
    assert len(spawned_tasks) == 1
    assert spawned_tasks[0].assignee == "verifier"
    assert spawned_tasks[0].skills == []


def test_dispatch_review_never_falls_back_to_coder_when_verifier_missing(
    kanban_home, monkeypatch,
):
    """A missing verifier is retryable review infrastructure, not self-review."""
    from hermes_cli import profiles
    # The task's assignee resolves, but 'verifier' does not.
    monkeypatch.setattr(
        profiles, "profile_exists", lambda name: name != "verifier"
    )
    spawned_tasks = []

    def capture_spawn(task, workspace, board=None):
        spawned_tasks.append(task)
        return 42  # fake PID

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, spawn_fn=capture_spawn)
        assert kb.get_task(conn, t).status == "review"
        unavailable = [e for e in kb.list_events(conn, t) if e.kind == "review_unavailable"]
    assert spawned_tasks == []
    assert t in res.skipped_nonspawnable
    assert len(unavailable) == 1
    assert unavailable[0].payload["target_profile"] == "verifier"
    assert unavailable[0].payload["retryable"] is True


def test_review_unavailable_auto_spawns_when_verifier_returns(
    kanban_home, monkeypatch,
):
    """Every dispatcher tick re-resolves the frozen stage without operator input."""
    from hermes_cli import profiles

    available = False
    monkeypatch.setattr(
        profiles,
        "profile_exists",
        lambda name: available and name == "verifier",
    )
    spawned_tasks = []

    def capture_spawn(task, workspace, board=None):
        spawned_tasks.append(task)
        return 42

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")

        first = kb.dispatch_once(conn, spawn_fn=capture_spawn)
        assert t in first.skipped_nonspawnable
        assert spawned_tasks == []
        assert kb.get_task(conn, t).status == "review"

        available = True
        second = kb.dispatch_once(conn, spawn_fn=capture_spawn)
        assert t not in second.skipped_nonspawnable
        assert kb.get_task(conn, t).status == "running"

    assert len(spawned_tasks) == 1
    assert spawned_tasks[0].assignee == "verifier"


def test_dispatch_review_skips_unassigned(kanban_home):
    """Unassigned review tasks go to skipped_unassigned, not spawned."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review floater")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_unassigned
    assert not res.spawned


def test_dispatch_review_counts_toward_max_spawn(
    kanban_home, all_assignees_spawnable,
):
    """Review spawns count against max_spawn alongside ready tasks."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect_closing() as conn:
        # Create 2 ready tasks + 1 review task, max_spawn=2
        t1 = kb.create_task(conn, title="ready 1", assignee="alice")
        t2 = kb.create_task(conn, title="ready 2", assignee="bob")
        t3 = kb.create_task(conn, title="review", assignee="alice")
        _set_task_status(conn, t3, "review")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=2)
    # Only 2 should spawn (ready tasks get priority in the loop)
    assert len(res.spawned) == 2
    assert len(spawns) == 2


def test_dispatch_review_spawns_when_ready_empty(
    kanban_home, all_assignees_spawnable,
):
    """When only review tasks exist, they still get dispatched."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
    assert len(res.spawned) == 1
    assert spawns[0] == t


def test_has_spawnable_review_true(kanban_home, monkeypatch):
    """Spawnability follows the independent stage target, not the assignee."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "verifier")
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="default")
        _set_task_status(conn, t, "review")
        assert kb.has_spawnable_review(conn) is True


def test_has_spawnable_review_false_on_empty(kanban_home):
    """has_spawnable_review returns False when no review tasks exist."""
    with kb.connect_closing() as conn:
        assert kb.has_spawnable_review(conn) is False


def test_has_spawnable_review_false_when_only_terminal_lanes(
    kanban_home, monkeypatch,
):
    """has_spawnable_review returns False when review tasks are terminal lanes."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review", assignee="orion-cc")
        _set_task_status(conn, t, "review")
        assert kb.has_spawnable_review(conn) is False


def test_dispatch_review_skips_nonspawnable(kanban_home, monkeypatch):
    """Review tasks with non-existent profiles go to skipped_nonspawnable."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review", assignee="orion-cc")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_nonspawnable
    assert not res.spawned


def test_dispatch_review_spawns_stage_profile_when_assignee_profile_missing(
    kanban_home, monkeypatch,
):
    """B cross-family-review fix: spawnability keys off the CURRENT stage target
    (verifier→reviewer→critic), not the original coder assignee. A review task
    whose coder-lane profile is gone is still spawnable via its stage profile —
    not stranded as nonspawnable."""
    from hermes_cli import profiles
    # only the verifier stage profile exists; the coder assignee profile is gone
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "verifier")
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review", assignee="removed-lane")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t not in res.skipped_nonspawnable
    assert any(tid == t and prof == "verifier" for (tid, prof, _) in res.spawned)


def test_has_spawnable_review_true_via_stage_profile(kanban_home, monkeypatch):
    """has_spawnable_review agrees with dispatch: a stage target that exists
    makes the review task spawnable even when the assignee profile is gone."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "verifier")
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review", assignee="removed-lane")
        _set_task_status(conn, t, "review")
        assert kb.has_spawnable_review(conn) is True


def test_blank_assignee_review_spawnability_is_consistent(kanban_home, monkeypatch):
    """A blank-assignee review task is bucketed skipped_unassigned by dispatch,
    so the stage-aware spawnability helper must NOT report it spawnable — else
    health/sweep disagree with dispatch and it sits unspawned-and-unparked."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "verifier")
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review", assignee="coder")
        _set_task_status(conn, t, "review")
        conn.execute("UPDATE tasks SET assignee = '' WHERE id = ?", (t,))
        res = kb.dispatch_once(conn, dry_run=True)
        assert t in res.skipped_unassigned
        assert t not in res.skipped_nonspawnable
        assert not any(tid == t for (tid, _p, _w) in res.spawned)
        # health must agree with dispatch (not spawnable here)
        assert kb.has_spawnable_review(conn) is False
        # and the helper itself returns None for a blank assignee
        assert kb._review_spawn_profile_for(conn, t, "", kb._review_gate_config()) is None


def test_review_status_in_valid_statuses():
    """'review' is a valid task status."""
    assert "review" in kb.VALID_STATUSES


def test_dispatch_review_does_not_claim_ready_tasks(
    kanban_home, all_assignees_spawnable,
):
    """Review dispatch uses claim_review_task, which only claims review tasks."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="ready task", assignee="alice")
        # claim_review_task should NOT claim a ready task
        claimed = kb.claim_review_task(conn, t)
    assert claimed is None

# Stale detection — detect_stale_running
# ---------------------------------------------------------------------------

def test_detect_stale_returns_running_task_with_no_heartbeat(kanban_home, monkeypatch):
    """A task running > timeout with zero heartbeats gets reclaimed as stale."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="stale-no-hb", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        # Rewind started_at so the task appears to have been running for 5 hours.
        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (five_hours_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )
        # No heartbeat set — last_heartbeat_at stays NULL.

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        killed = []
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: killed.append(s),
        )
        assert t in stale, "Task with no heartbeat for >4h should be reclaimed"
        task = kb.get_task(conn, t)
        assert task.status == "ready"


def test_detect_stale_returns_task_with_stale_heartbeat(kanban_home, monkeypatch):
    """A task running > timeout with a heartbeat older than 1h gets reclaimed."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="stale-hb", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        heartbeat_2h_ago = int(time.time()) - (2 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ?, last_heartbeat_at = ? "
                "WHERE id = ?",
                (five_hours_ago, heartbeat_2h_ago, t),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert t in stale, (
            "Task with heartbeat >1h old and started >4h ago should be stale"
        )
        assert kb.get_task(conn, t).status == "ready"


def test_detect_stale_skips_task_with_recent_heartbeat(kanban_home, monkeypatch):
    """A task running > timeout but with a recent heartbeat is NOT reclaimed."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="alive-hb", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        heartbeat_now = int(time.time())  # heartbeat just happened
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ?, last_heartbeat_at = ? "
                "WHERE id = ?",
                (five_hours_ago, heartbeat_now, t),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert stale == [], "Task with recent heartbeat should not be reclaimed"
        assert kb.get_task(conn, t).status == "running"


def test_detect_stale_skips_recently_started_task(kanban_home, monkeypatch):
    """A task started < timeout ago is NOT reclaimed even with no heartbeat."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="fresh", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        # Started only 1 hour ago — well within the 4h threshold.
        one_hour_ago = int(time.time()) - 3600
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (one_hour_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (one_hour_ago, t),
            )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert stale == [], "Task started <4h ago should not be reclaimed"
        assert kb.get_task(conn, t).status == "running"


def test_detect_stale_skips_when_timeout_zero(kanban_home, monkeypatch):
    """stale_timeout_seconds=0 disables stale detection entirely."""

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="disabled", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (five_hours_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )

        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=0, signal_fn=lambda p, s: None,
        )
        assert stale == [], "timeout=0 should disable stale detection"
        assert kb.get_task(conn, t).status == "running"


def test_detect_stale_skips_blocked_tasks(kanban_home, monkeypatch):
    """Blocked tasks are NOT reclaimed by stale detection."""
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="blocked-task", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (five_hours_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )
        # Block the task explicitly.
        kb.block_task(conn, t, reason="human requested block")

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert stale == [], "Blocked task should not be reclaimed by stale detection"
        assert kb.get_task(conn, t).status == "blocked"


def test_detect_stale_does_not_tick_failure_counter(kanban_home, monkeypatch):
    """Stale reclaim must NOT tick consecutive_failures.

    Stale detection is dispatcher-side absence-of-heartbeat detection,
    not a worker failure. Counting it as a failure would let two
    legitimately-long-running tasks (>4h without explicit heartbeat) trip
    the circuit breaker and auto-block at the default failure_limit=2,
    even though no worker actually failed. The 'stale' event in
    task_events is the right audit surface; the consecutive_failures
    counter is reserved for spawn_failed / timed_out / crashed.
    """
    import hermes_cli.kanban_db as _kb

    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="stale-no-counter-tick", assignee="worker")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (five_hours_ago, t)
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, t),
            )
            # Counter starts at 0; assert that's our baseline.
            row = conn.execute(
                "SELECT consecutive_failures FROM tasks WHERE id = ?", (t,)
            ).fetchone()
            assert row["consecutive_failures"] in (0, None)

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda p, s: None,
        )
        assert t in stale, "Task should be reclaimed by stale detection"

        # Critical assertion: the failure counter MUST NOT have ticked.
        # Stale reclaim resets to ready for re-dispatch without penalty.
        row = conn.execute(
            "SELECT consecutive_failures FROM tasks WHERE id = ?", (t,)
        ).fetchone()
        assert row["consecutive_failures"] in (0, None), (
            f"Stale reclaim ticked consecutive_failures to "
            f"{row['consecutive_failures']!r}; should remain 0/NULL."
        )

        # And the audit trail still records the stale event so operators
        # can see what happened.
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t,),
        ).fetchall()
        kinds = [e["kind"] for e in events]
        assert "stale" in kinds, (
            f"Expected 'stale' event in task_events; got {kinds!r}"
        )


# ---------------------------------------------------------------------------
# Corruption guard (issue #30687)
# ---------------------------------------------------------------------------

def _write_corrupt_db(path: Path) -> bytes:
    """Write a kanban DB with a VALID SQLite header but malformed page content.

    This is the corruption shape the integrity guard specifically targets
    (e.g. issue #29507 follow-up reports where the file's first 16 bytes
    pass the header byte check but ``PRAGMA integrity_check`` then fails
    because the internal pages are damaged). It's what main's header-only
    validator was letting through, and what this PR adds the full guard
    for.
    """
    # 100-byte SQLite header (magic + minimal valid-looking fields) so the
    # cheap header check passes, then deliberate garbage so sqlite refuses
    # to read the file past the header.
    header = b"SQLite format 3\x00" + b"\x10\x00\x02\x02\x00\x40\x20\x20"
    header += b"\x00\x00\x00\x0c\x00\x00\x23\x46\x00\x00\x00\x00"
    header = header.ljust(100, b"\x00")
    payload = b"definitely not a valid sqlite page \x00\x01\x02\x03" * 64
    blob = header + payload
    path.write_bytes(blob)
    return blob


def test_init_db_refuses_corrupt_existing_file(tmp_path):
    db_path = tmp_path / "kanban.db"
    original = _write_corrupt_db(db_path)
    # Ensure the cache doesn't mask the guard.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
        kb.init_db(db_path=db_path)

    err = excinfo.value
    assert err.db_path == db_path
    assert err.backup_path is not None
    assert err.backup_path.exists()
    assert err.backup_path.read_bytes() == original
    # Original bytes untouched — no schema was written on top.
    assert db_path.read_bytes() == original
    assert str(db_path) in str(err)
    assert str(err.backup_path) in str(err)


def test_connect_refuses_corrupt_existing_file(tmp_path):
    db_path = tmp_path / "kanban.db"
    _write_corrupt_db(db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    with pytest.raises(kb.KanbanDbCorruptError):
        kb.connect(db_path=db_path)


def test_repeated_corrupt_open_reuses_single_backup(tmp_path):
    """Repeated quarantines of the same corrupt bytes must not amplify disk usage.

    Regression for the gateway dispatcher's 5-min retry loop on shared kanban
    DBs across multi-profile fleets: each retry on an unchanged corrupt file
    used to create a fresh ``.corrupt.<timestamp>.bak`` until disk filled. The
    content-addressed backup name is deterministic in the DB's sha256, so
    N retries of the same bytes share one backup.
    """
    db_path = tmp_path / "kanban.db"
    original = _write_corrupt_db(db_path)

    backups: set[Path] = set()
    for _ in range(10):
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
        with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
            kb.connect(db_path=db_path)
        assert excinfo.value.backup_path is not None
        backups.add(excinfo.value.backup_path)

    assert len(backups) == 1, f"expected 1 deterministic backup, got {len(backups)}"
    (backup,) = backups
    assert backup.exists()
    assert backup.read_bytes() == original

    # Mutate the corrupt bytes — fingerprint changes, separate backup preserved.
    with db_path.open("r+b") as f:
        f.seek(4096)
        f.write(b"\xAB" * 64)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with pytest.raises(kb.KanbanDbCorruptError) as excinfo2:
        kb.connect(db_path=db_path)
    second_backup = excinfo2.value.backup_path
    assert second_backup is not None
    assert second_backup != backup
    assert second_backup.exists()


def test_locked_healthy_db_does_not_classify_as_corrupt(tmp_path, monkeypatch):
    """A transient lock during the probe must not produce a .corrupt backup
    and must not be reported as :class:`KanbanDbCorruptError`. Raw sqlite
    ``OperationalError`` (lock/busy) is acceptable and expected."""
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    real_connect = sqlite3.connect

    def flaky_connect(*args, **kwargs):
        # First call is the integrity probe — simulate a lock.
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(kb.sqlite3, "connect", flaky_connect)

    with pytest.raises(sqlite3.OperationalError):
        kb.connect(db_path=db_path)

    # No .corrupt backup may be produced for a healthy-but-locked DB.
    backups = list(tmp_path.glob("*.corrupt.*"))
    assert backups == [], f"unexpected corrupt backups: {backups}"

    # And once the lock clears, normal access still works.
    monkeypatch.setattr(kb.sqlite3, "connect", real_connect)
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="still here")
        titles = [t.title for t in kb.list_tasks(conn)]
    assert "still here" in titles


class _MalformedProbe:
    """Stand-in connection whose integrity probe always reports malformed."""

    def execute(self, *_a, **_k):
        raise sqlite3.DatabaseError("database disk image is malformed")

    def close(self):
        pass


def test_guard_reprobes_transient_malformed_then_recovers(tmp_path, monkeypatch):
    """A one-shot 'database disk image is malformed' that clears on the next
    probe must NOT quarantine a healthy DB.

    Reproduces the 2026-05-28 storm: under multi-process WAL/SHM coordination
    the integrity probe occasionally read a torn page and the guard copied the
    whole DB to a ``.corrupt`` backup and killed the dispatcher, even though
    ``integrity_check`` passed moments later.
    """
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    real_sqlite_connect = kb._sqlite_connect
    calls = {"n": 0}

    def flaky_connect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _MalformedProbe()  # transient torn read on first probe only
        return real_sqlite_connect(*args, **kwargs)

    monkeypatch.setattr(kb, "_sqlite_connect", flaky_connect)

    # Must return cleanly — no exception, no quarantine.
    kb._guard_existing_db_is_healthy(db_path, attempts=3, backoff_s=0)

    assert calls["n"] >= 2, "guard did not re-probe after a transient malformed read"
    assert list(tmp_path.glob("*.corrupt.*")) == [], "transient blip must not back up the DB"


def test_guard_quarantines_persistent_malformed(tmp_path, monkeypatch):
    """If every re-probe still reports malformed, the guard must still
    quarantine (backup + raise) — retries cannot mask real corruption."""
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    calls = {"n": 0}

    def always_malformed(*_args, **_kwargs):
        calls["n"] += 1
        return _MalformedProbe()

    monkeypatch.setattr(kb, "_sqlite_connect", always_malformed)

    with pytest.raises(kb.KanbanDbCorruptError):
        kb._guard_existing_db_is_healthy(db_path, attempts=3, backoff_s=0)

    assert calls["n"] == 3, "guard should re-probe exactly `attempts` times before quarantining"
    assert list(tmp_path.glob("*.corrupt.*")), "persistent corruption must still produce a backup"


def test_init_db_allows_missing_then_healthy(tmp_path):
    db_path = tmp_path / "fresh.db"
    assert not db_path.exists()
    kb.init_db(db_path=db_path)
    assert db_path.exists() and db_path.stat().st_size > 0

    # Idempotent on a healthy DB: data survives a second init.
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="keeps")
    kb.init_db(db_path=db_path)
    with kb.connect(db_path=db_path) as conn:
        tasks = kb.list_tasks(conn)
    assert [t.title for t in tasks] == ["keeps"]


# ---------------------------------------------------------------------------
# First-use tip for scratch workspaces
# ---------------------------------------------------------------------------

def test_maybe_emit_scratch_tip_fires_once_per_install(kanban_home, caplog):
    """First scratch workspace materialization warns + emits an event.

    Subsequent scratch workspaces on the SAME install stay silent — the
    sentinel file under kanban_home() flips after the first emit.
    """
    import logging

    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="first scratch")
        t2 = kb.create_task(conn, title="second scratch")

    # Sentinel must not exist yet on a fresh install.
    assert not kb._scratch_tip_shown()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect_closing() as conn:
            kb._maybe_emit_scratch_tip(conn, t1, "scratch")

    # Sentinel is now set.
    assert kb._scratch_tip_shown()
    assert kb._scratch_tip_sentinel_path().exists()

    # Warning was logged exactly once.
    tip_records = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert len(tip_records) == 1, (
        f"Expected exactly one tip warning, got {len(tip_records)}: "
        f"{[r.getMessage() for r in tip_records]!r}"
    )

    # An event row was appended on the first task.
    with kb.connect_closing() as conn:
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t1,),
        ).fetchall()
    kinds = [e["kind"] for e in events]
    assert "tip_scratch_workspace" in kinds, (
        f"Expected tip_scratch_workspace event on first scratch task; "
        f"got {kinds!r}"
    )

    # Second scratch materialization on the same install stays silent.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect_closing() as conn:
            kb._maybe_emit_scratch_tip(conn, t2, "scratch")
    tip_records2 = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert tip_records2 == [], (
        f"Tip should not re-fire after sentinel is set; got "
        f"{[r.getMessage() for r in tip_records2]!r}"
    )
    with kb.connect_closing() as conn:
        events2 = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t2,),
        ).fetchall()
    assert "tip_scratch_workspace" not in [e["kind"] for e in events2], (
        "Tip event should not be appended for subsequent scratch tasks."
    )


def test_maybe_emit_scratch_tip_skips_non_scratch_workspaces(kanban_home, caplog):
    """worktree/dir workspaces are preserved on completion and must not
    trigger the scratch-cleanup tip."""
    import logging

    with kb.connect_closing() as conn:
        t_wt = kb.create_task(conn, title="worktree task")
        t_dir = kb.create_task(conn, title="dir task")

    assert not kb._scratch_tip_shown()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect_closing() as conn:
            kb._maybe_emit_scratch_tip(conn, t_wt, "worktree")
            kb._maybe_emit_scratch_tip(conn, t_dir, "dir")

    # Sentinel stays unset — these workspaces are preserved by design,
    # so the warning is irrelevant for them and we save the one-shot
    # for a real scratch user.
    assert not kb._scratch_tip_shown()
    tip_records = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert tip_records == []
    with kb.connect_closing() as conn:
        for tid in (t_wt, t_dir):
            events = conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (tid,),
            ).fetchall()
            assert "tip_scratch_workspace" not in [e["kind"] for e in events]


# ---------------------------------------------------------------------------
# Connection pragmas (secure_delete, cell_size_check, synchronous=FULL)
# ---------------------------------------------------------------------------


def test_connect_sets_secure_delete_on(tmp_path):
    """secure_delete=ON must be active on every new connection."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        row = conn.execute("PRAGMA secure_delete").fetchone()
    assert row[0] == 1, f"expected secure_delete=1, got {row[0]}"


def test_connect_sets_cell_size_check_on(tmp_path):
    """cell_size_check=ON must be active on every new connection."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        row = conn.execute("PRAGMA cell_size_check").fetchone()
    assert row[0] == 1, f"expected cell_size_check=1, got {row[0]}"


def test_connect_sets_synchronous_full(tmp_path):
    """synchronous must be FULL (=2), not NORMAL (=1)."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        row = conn.execute("PRAGMA synchronous").fetchone()
    assert row[0] == 2, f"expected synchronous=2 (FULL), got {row[0]}"


def test_connect_pragmas_applied_on_reconnect(tmp_path):
    """All three pragmas must be re-applied on every connect(), not just the first."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    # First connection: write a task and close.
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="reconnect-check")
    # Force re-init path by discarding path cache.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    # Second connection: pragmas must still be applied.
    with kb.connect(db_path=db_path) as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert conn.execute("PRAGMA cell_size_check").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2



def test_pragmas_not_accidentally_disabled_by_migrate_path(tmp_path):
    """Migration path must not reset connection pragmas."""
    db_path = tmp_path / "legacy.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    # Initialise with a fresh connect so schema + init run.
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="pre-migration-task")
    # Simulate a re-entry through the init/migration path by discarding path cache.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert conn.execute("PRAGMA cell_size_check").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2

# write_txn — rollback handler must not mask the original exception
# ---------------------------------------------------------------------------


def test_write_txn_preserves_original_exception_when_rollback_fails(kanban_home):
    """When a write inside write_txn raises an OperationalError that SQLite
    has already auto-rolled-back (e.g. ``disk I/O error``,
    ``database is locked``, ``database disk image is malformed``), the
    explicit ROLLBACK in ``write_txn.__exit__`` itself raises
    ``cannot rollback - no transaction is active``. The original cause
    must NOT be masked by the secondary rollback failure — operators rely
    on the original cause to diagnose the underlying issue.
    """

    class FailingConnWrapper:
        """Delegate to a real connection, simulating an EIO during an INSERT
        that SQLite has already auto-rolled-back."""

        def __init__(self, real):
            self._real = real
            self._fail_armed = True

        def execute(self, sql, *args, **kwargs):
            if (
                self._fail_armed
                and sql.lstrip().upper().startswith("INSERT")
                and "task_events" in sql.lower()
            ):
                self._fail_armed = False  # one-shot
                # Simulate SQLite auto-rolling back the transaction by
                # issuing a real ROLLBACK now. After this, BEGIN IMMEDIATE
                # is no longer active and an explicit ROLLBACK would error.
                try:
                    self._real.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise sqlite3.OperationalError("disk I/O error")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with kb.connect_closing() as conn:
        wrapper = FailingConnWrapper(conn)
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            with kb.write_txn(wrapper):
                kb._append_event(wrapper, "t_bogus", "promoted", None)

    msg = str(excinfo.value)
    assert "disk I/O error" in msg, (
        f"write_txn masked the original exception with rollback failure; "
        f"got {msg!r} (expected to contain 'disk I/O error')"
    )
    assert "cannot rollback" not in msg, (
        f"write_txn surfaced the rollback failure instead of the original "
        f"OperationalError; got {msg!r}"
    )
def test_write_txn_healthy_commit_no_exception(tmp_path):
    """Normal commit does not trigger the torn-extend check."""
    from hermes_cli.kanban_db import connect, write_txn
    db = tmp_path / "test.db"
    conn = connect(db_path=db)
    # Should not raise
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO tasks (id, title, assignee, status, priority, created_at) "
            "VALUES ('t_test01', 'test task', 'tester', 'todo', 0, 1234567890)"
        )
    row = conn.execute("SELECT title FROM tasks WHERE id='t_test01'").fetchone()
    assert row["title"] == "test task"
    conn.close()


def test_write_txn_raises_on_truncated_file(tmp_path):
    """A mocked smaller file size triggers the torn-extend check."""
    from hermes_cli.kanban_db import connect, write_txn
    db = tmp_path / "test.db"
    conn = connect(db_path=db)
    conn.execute("PRAGMA journal_mode=DELETE")
    # Get actual page size so we can fake a smaller file
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    original_getsize = os.path.getsize

    def fake_getsize(path):
        # Return a size that implies at least 1 fewer page than header claims
        real_size = original_getsize(path)
        return max(0, real_size - page_size)

    with pytest.raises(sqlite3.DatabaseError, match="torn-extend|page count mismatch"):
        with unittest.mock.patch("hermes_cli.kanban_db.os.path.getsize", side_effect=fake_getsize):
            with write_txn(conn) as c:
                c.execute(
                    "INSERT INTO tasks (id, title, assignee, status, priority, created_at) "
                    "VALUES ('t_test02', 'test task 2', 'tester', 'todo', 0, 1234567890)"
                )
    conn.close()


def test_write_txn_wal_mode_ignores_transient_main_file_size_lag(tmp_path):
    """WAL commits must not treat an uncheckpointed main DB as torn-extend."""
    from hermes_cli.kanban_db import connect, write_txn

    db = tmp_path / "test.db"
    conn = connect(db_path=db)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    original_getsize = os.path.getsize

    def fake_getsize(path):
        real_size = original_getsize(path)
        return max(0, real_size - page_size)

    with unittest.mock.patch("hermes_cli.kanban_db.os.path.getsize", side_effect=fake_getsize):
        with write_txn(conn) as c:
            c.execute(
                "INSERT INTO tasks (id, title, assignee, status, priority, created_at) "
                "VALUES ('t_wal001', 'wal task', 'tester', 'todo', 0, 1234567890)"
            )
    row = conn.execute("SELECT title FROM tasks WHERE id='t_wal001'").fetchone()
    assert row["title"] == "wal task"
    conn.close()


def test_write_txn_post_commit_check_fires_every_call(tmp_path):
    """The invariant check runs on every write_txn call."""
    from hermes_cli.kanban_db import connect, write_txn
    import hermes_cli.kanban_db as kanban_db_module
    db = tmp_path / "test.db"
    conn = connect(db_path=db)
    call_count = 0
    real_check = kanban_db_module._check_file_length_invariant

    def counting_check(c):
        nonlocal call_count
        call_count += 1
        real_check(c)

    with unittest.mock.patch.object(kanban_db_module, "_check_file_length_invariant", counting_check):
        for i in range(3):
            with write_txn(conn) as c:
                c.execute(
                    f"INSERT INTO tasks (id, title, assignee, status, priority, created_at) "
                    f"VALUES ('t_fire{i:02d}', 'task {i}', 'tester', 'todo', 0, 1234567890)"
                )
    assert call_count == 3
    conn.close()


def test_connect_sets_wal_autocheckpoint_100(tmp_path):
    """connect() sets wal_autocheckpoint to 100."""
    from hermes_cli.kanban_db import connect
    db = tmp_path / "test.db"
    conn = connect(db_path=db)
    val = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    assert val == 100
    conn.close()


def test_write_txn_check_reads_correct_header_fields(tmp_path):
    """Synthetic DB file with mismatched header page_count triggers the check."""
    import struct
    from hermes_cli.kanban_db import _check_file_length_invariant

    class _Cursor:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class _FakeConn:
        def __init__(self, db_path: Path, page_size: int):
            self._db_path = db_path
            self._page_size = page_size

        def execute(self, sql):
            sql = sql.lower()
            if "journal_mode" in sql:
                return _Cursor(("delete",))
            if "database_list" in sql:
                return _Cursor((0, "main", str(self._db_path)))
            if "page_size" in sql:
                return _Cursor((self._page_size,))
            raise AssertionError(f"unexpected SQL: {sql}")

    db = tmp_path / "synthetic.db"
    page_size = 4096
    header = bytearray(b"SQLite format 3\x00" + (b"\x00" * (page_size - 16)))
    header[16:18] = struct.pack(">H", page_size)
    header[28:32] = struct.pack(">I", 2)
    db.write_bytes(header)
    with pytest.raises(sqlite3.DatabaseError, match="torn-extend|page count mismatch"):
        _check_file_length_invariant(_FakeConn(db, page_size))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# reap_worker_zombies() tests
# ---------------------------------------------------------------------------


def test_reap_worker_zombies_returns_count():
    """reap_worker_zombies() returns the list of reaped PIDs."""
    from unittest.mock import patch

    fake_pids = [12345, 67890, 11111]
    call_count = [0]

    def fake_waitpid(pid, flags):
        if call_count[0] < len(fake_pids):
            p = fake_pids[call_count[0]]
            call_count[0] += 1
            return p, 0
        return 0, 0

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=fake_waitpid):
        with patch("hermes_cli.kanban_db._record_worker_exit"):
            pids = kb.reap_worker_zombies()
    assert pids == [12345, 67890, 11111]


def test_reap_worker_zombies_noop_on_windows(monkeypatch):
    """reap_worker_zombies() returns 0 and never calls os.waitpid on Windows."""
    from unittest.mock import patch

    monkeypatch.setattr("hermes_cli.kanban_db.os.name", "nt")
    with patch("hermes_cli.kanban_db.os.waitpid") as mock_waitpid:
        result = kb.reap_worker_zombies()
    mock_waitpid.assert_not_called()
    assert result == []


def test_reap_worker_zombies_noop_no_children():
    """reap_worker_zombies() returns 0 without error when there are no children."""
    from unittest.mock import patch

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=ChildProcessError):
        result = kb.reap_worker_zombies()
    assert result == []


def test_reap_worker_zombies_records_exit_status():
    """reap_worker_zombies() calls _record_worker_exit for each reaped pid."""
    from unittest.mock import patch

    calls = []
    call_count = [0]

    def fake_waitpid(pid, flags):
        call_count[0] += 1
        if call_count[0] == 1:
            return 12345, 0
        return 0, 0

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=fake_waitpid):
        with patch(
            "hermes_cli.kanban_db._record_worker_exit",
            side_effect=lambda p, s: calls.append((p, s)),
        ):
            kb.reap_worker_zombies()

    assert calls == [(12345, 0)]


def test_reap_worker_zombies_handles_waitpid_os_error():
    """reap_worker_zombies() does not propagate generic OSError from os.waitpid."""
    from unittest.mock import patch

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=OSError("test error")):
        result = kb.reap_worker_zombies()
    assert result == []


def test_zombie_reaper_runs_despite_board_connect_failure():
    """reap_worker_zombies runs even when a board tick raises an error."""
    from unittest.mock import patch

    call_count = [0]

    def fake_waitpid(pid, flags):
        call_count[0] += 1
        if call_count[0] <= 2:
            return [12345, 67890][call_count[0] - 1], 0
        return 0, 0

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=fake_waitpid):
        with patch("hermes_cli.kanban_db._record_worker_exit"):
            # Simulate a board tick failure before reaping
            try:
                raise sqlite3.OperationalError("disk I/O error")
            except sqlite3.OperationalError:
                pass

            # Reaper still runs independently
            pids = kb.reap_worker_zombies()

    assert pids == [12345, 67890]


def test_zombie_reaper_survives_all_boards_failing():
    """reap_worker_zombies runs each tick regardless of board tick failures."""
    from unittest.mock import patch

    total_reaped = 0

    def make_fake_waitpid(zombie_pids):
        call_count = [0]

        def fake_waitpid(pid, flags):
            if call_count[0] < len(zombie_pids):
                p = zombie_pids[call_count[0]]
                call_count[0] += 1
                return p, 0
            return 0, 0

        return fake_waitpid

    # 5 ticks, 2 zombies per tick = 10 total
    for tick in range(5):
        pids = [tick * 100 + 1, tick * 100 + 2]
        with patch(
            "hermes_cli.kanban_db.os.waitpid", side_effect=make_fake_waitpid(pids)
        ):
            with patch("hermes_cli.kanban_db._record_worker_exit"):
                pids = kb.reap_worker_zombies()
        total_reaped += len(pids)

    assert total_reaped == 10


def test_dispatch_once_still_reaps_via_extracted_fn(kanban_home):
    """The reaper inside dispatch_once still works after refactor to reap_worker_zombies()."""
    from unittest.mock import patch

    call_count = [0]

    def fake_waitpid(pid, flags):
        call_count[0] += 1
        if call_count[0] == 1:
            return 99999, 0
        return 0, 0

    with patch("hermes_cli.kanban_db.os.waitpid", side_effect=fake_waitpid):
        with patch("hermes_cli.kanban_db._record_worker_exit"):
            with patch("hermes_cli.kanban_db.os.name", "posix"):
                pids = kb.reap_worker_zombies()

    assert pids == [99999]



# ---------------------------------------------------------------------------
# connect_closing(): context manager that actually closes the FD
# Regression coverage for #33159 (kanban.db FD leak — gateway crashes after
# ~4 days). sqlite3.Connection's built-in __exit__ commits/rollbacks but
# does NOT close, so `with kb.connect_closing() as conn:` leaks the FD in
# long-lived processes (gateway run_slash, dashboard decompose handler).
# `connect_closing()` is the leak-safe replacement.
# ---------------------------------------------------------------------------


def test_connect_closing_closes_connection_on_exit(tmp_path):
    """The new context manager MUST actually close the underlying FD."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect_closing(db_path=db_path) as conn:
        conn.execute("SELECT 1").fetchone()
    # After exit, the connection MUST be closed — subsequent execute
    # should raise ProgrammingError.
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_connect_closing_closes_on_exception(tmp_path):
    """Connection closed even when the body raises."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    captured = []
    with pytest.raises(RuntimeError, match="boom"):
        with kb.connect_closing(db_path=db_path) as conn:
            captured.append(conn)
            raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")


def test_connect_closing_yields_usable_connection(tmp_path):
    """Smoke test: schema is initialized and basic ops work."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect_closing(db_path=db_path) as conn:
        tid = kb.create_task(conn, title="closing-cm test")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.title == "closing-cm test"


def test_bare_connect_does_not_close_on_context_exit(tmp_path):
    """Document the leak that connect_closing exists to prevent.

    sqlite3.Connection's __exit__ commits/rollbacks but doesn't close.
    This is the upstream behaviour we cannot change; the regression
    guard is to make sure connect_closing() does the right thing.
    """
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        pass
    # Still usable after with-block exit (the leak).
    conn.execute("SELECT 1").fetchone()
    conn.close()  # explicit close to avoid leaking THIS test


# --- Recovered: superseding-review / needs_revision tests (orig commit 92d8e718a) ---
def test_superseding_review_rewire_helper_is_explicit_and_audited(kanban_home):
    with kb.connect_closing() as conn:
        source = kb.create_task(conn, title="source waiting on old review", assignee="coder")
        old_review = kb.create_task(conn, title="old review", assignee="reviewer")
        new_review = kb.create_task(conn, title="new review", assignee="reviewer")
        kb.link_tasks(conn, old_review, source)

        result = kb.rewire_superseding_review_parent(
            conn,
            source_task=source,
            old_review_task=old_review,
            new_review_task=new_review,
            reason="NEEDS_REVISION fixed and re-reviewed",
        )

        assert result == {
            "source_task": source,
            "old_review_task": old_review,
            "new_review_task": new_review,
            "old_parent_removed": True,
            "new_parent_added": True,
            "reason": "NEEDS_REVISION fixed and re-reviewed",
        }
        assert kb.parent_ids(conn, source) == [new_review]
        events = [
            e for e in kb.list_events(conn, source)
            if e.kind == "superseding_review_rewired"
        ]
        assert len(events) == 1
        assert events[0].payload == result


def test_superseding_review_rewire_is_noop_without_old_edge(kanban_home):
    with kb.connect_closing() as conn:
        source = kb.create_task(conn, title="source", assignee="coder")
        old_review = kb.create_task(conn, title="old review", assignee="reviewer")
        new_review = kb.create_task(conn, title="new review", assignee="reviewer")

        result = kb.rewire_superseding_review_parent(
            conn,
            source_task=source,
            old_review_task=old_review,
            new_review_task=new_review,
            reason="operator requested audit-only check",
        )

        assert result["old_parent_removed"] is False
        assert result["new_parent_added"] is True
        assert kb.parent_ids(conn, source) == [new_review]
        event = [
            e for e in kb.list_events(conn, source)
            if e.kind == "superseding_review_rewired"
        ][-1]
        assert event.payload["old_parent_removed"] is False
        assert event.payload["new_parent_added"] is True


def test_needs_revision_fix_task_is_deterministic_idempotent_and_keeps_source_blocked(kanban_home):
    with kb.connect_closing() as conn:
        source = kb.create_task(conn, title="implement lifecycle", assignee="coder")
        kb.claim_task(conn, source)
        # main renamed active_run() → latest_run(); after claim the latest run is the active one
        run = kb.latest_run(conn, source)
        assert run is not None
        assert kb.block_task(
            conn,
            source,
            reason="review-required: implementation ready for verdict",
            expected_run_id=run.id,
        )
        old_review = kb.create_task(conn, title="review implementation", assignee="reviewer")
        reviewer_metadata = {
            "verdict": "NEEDS_REVISION",
            "blocking_findings": ["missing supersedes relation"],
            "required_verification": ["pytest tests/hermes_cli/test_kanban_db.py -q"],
            "evidence_audited": [source, old_review],
            "residual_risk": "source must remain blocked until finalization gate",
        }

        first = kb.ensure_needs_revision_fix_task(
            conn,
            source_task=source,
            review_task=old_review,
            reviewer_metadata=reviewer_metadata,
            reason="Reviewer requested deterministic fix",
        )
        second = kb.ensure_needs_revision_fix_task(
            conn,
            source_task=source,
            review_task=old_review,
            reviewer_metadata=reviewer_metadata,
            reason="Reviewer requested deterministic fix",
        )

        assert second == first
        fix = kb.get_task(conn, first["fix_task"])
        assert fix is not None
        assert fix.assignee == "coder"
        assert fix.status == "ready"
        assert kb.parent_ids(conn, fix.id) == []
        assert "verdict: NEEDS_REVISION" in (fix.body or "")
        assert "source remains blocked" in (fix.body or "")
        assert kb.get_task(conn, source).status == "blocked"
        events = [
            e for e in kb.list_events(conn, source)
            if e.kind == "needs_revision_fix_task_ensured"
        ]
        assert len(events) == 1
        assert events[0].payload["source_task"] == source
        assert events[0].payload["review_task"] == old_review
        assert events[0].payload["fix_task"] == fix.id
        assert events[0].payload["created"] is True


# ---------------------------------------------------------------------------
# B1 (N-B1): diff snapshot captured at the review handoff
# ---------------------------------------------------------------------------

import shutil as _shutil  # noqa: E402

_GIT = _shutil.which("git")
requires_git = pytest.mark.skipif(_GIT is None, reason="git not installed")


def _init_git_repo_with_changes(path: Path) -> None:
    """Init a git repo at *path* with one committed file modified + one
    untracked file, so ``status --porcelain`` and ``diff --stat`` both report."""
    import subprocess

    def run(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    run("init")
    (path / "tracked.py").write_text("original = 1\n", encoding="utf-8")
    run("add", "tracked.py")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "base")
    # Modify the tracked file (→ diff --stat) and add an untracked one (→ porcelain).
    (path / "tracked.py").write_text("original = 2\n", encoding="utf-8")
    (path / "untracked.py").write_text("brand_new = True\n", encoding="utf-8")


@requires_git
def test_b1_capture_diff_snapshot_git_workspace(kanban_home, tmp_path):
    repo = tmp_path / "ws"
    repo.mkdir()
    _init_git_repo_with_changes(repo)
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="b1", workspace_kind="dir", workspace_path=str(repo)
        )
        snap = kb._capture_review_diff_snapshot(conn, tid)
    assert set(snap.get("changed_files", [])) == {"tracked.py", "untracked.py"}
    assert "tracked.py" in snap.get("diff_stat", "")


def test_b1_capture_diff_snapshot_non_git_scratch(kanban_home, tmp_path):
    """A plain (non-git) workspace yields an empty snapshot, never a crash."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "file.txt").write_text("hi", encoding="utf-8")
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="b1", workspace_kind="dir", workspace_path=str(scratch)
        )
        snap = kb._capture_review_diff_snapshot(conn, tid)
    assert snap == {}


def test_b1_capture_diff_snapshot_missing_workspace(kanban_home, tmp_path):
    """workspace_path pointing at a vanished directory → empty, no crash."""
    gone = tmp_path / "gone"
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="b1", workspace_kind="dir", workspace_path=str(gone)
        )
        snap = kb._capture_review_diff_snapshot(conn, tid)
    assert snap == {}


def test_b1_capture_diff_snapshot_no_workspace(kanban_home):
    """A scratch task with no workspace_path → empty snapshot."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="b1")
        snap = kb._capture_review_diff_snapshot(conn, tid)
    assert snap == {}


@requires_git
def test_b1_submit_for_review_event_and_metadata_carry_snapshot(
    kanban_home, tmp_path
):
    import json
    repo = tmp_path / "ws"
    repo.mkdir()
    _init_git_repo_with_changes(repo)
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="b1", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
            initial_status="running",
        )
        ok = kb._submit_for_review(
            conn, tid, result="done", summary="all done",
            metadata={"artifacts": ["tracked.py"]}, verified_cards=[],
            expected_run_id=None,
        )
        assert ok is True
        ev = [
            e for e in kb.list_events(conn, tid)
            if e.kind == "submitted_for_review"
        ]
        assert len(ev) == 1
        payload = ev[0].payload
        # Additive snapshot keys present...
        assert set(payload["changed_files"]) == {"tracked.py", "untracked.py"}
        assert "tracked.py" in payload["diff_stat"]
        # ...and the pre-existing keys are untouched (byte-identical contract).
        assert payload["result_len"] == len("done")
        assert payload["summary"] == "all done"
        assert payload["artifacts"] == ["tracked.py"]
        # Snapshot also rides the run metadata.
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        md = json.loads(row["metadata"])
        assert set(md["changed_files"]) == {"tracked.py", "untracked.py"}


def test_b1_submit_for_review_non_git_payload_has_no_snapshot_keys(
    kanban_home, tmp_path
):
    """Regression guard: with no git workspace, the event payload carries NONE
    of the new keys — the pre-B1 shape is preserved exactly."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="b1", assignee="coder",
            workspace_kind="dir", workspace_path=str(scratch),
            initial_status="running",
        )
        kb._submit_for_review(
            conn, tid, result="done", summary="done", metadata=None,
            verified_cards=[], expected_run_id=None,
        )
        ev = [
            e for e in kb.list_events(conn, tid)
            if e.kind == "submitted_for_review"
        ]
        assert len(ev) == 1
        assert "changed_files" not in ev[0].payload
        assert "diff_stat" not in ev[0].payload


# ---------------------------------------------------------------------------
# B2 (N-B2): structured verdict column on task_runs (review lane only)
# ---------------------------------------------------------------------------

def _latest_run_verdict(conn, task_id):
    row = conn.execute(
        "SELECT verdict FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["verdict"] if row else None


def test_b2_verdict_column_present_and_migrate_idempotently(kanban_home):
    """task_runs gains a ``verdict`` column; re-running the additive migration
    is a no-op (idempotent, no duplicate-column crash)."""
    with kb.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")}
        assert "verdict" in cols
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")]
        assert cols2.count("verdict") == 1


def test_b2_approved_verdict_on_review_complete(kanban_home):
    """A verifier completing a task it reviewed → verdict APPROVED."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None
        ok = kb.complete_task(
            conn,
            t,
            result="lgtm",
            summary="lgtm",
            metadata={"review_verdict": "APPROVED"},
            review_gate=True,
        )
        assert ok is True
        assert _latest_run_verdict(conn, t) == "APPROVED"


def test_b2_request_changes_verdict_on_review_block(kanban_home):
    """A verifier blocking a task it reviewed → verdict REQUEST_CHANGES."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None
        ok = kb.block_task(conn, t, reason="missing tests")
        assert ok is True
        assert _latest_run_verdict(conn, t) == "REQUEST_CHANGES"


def test_b2_review_complete_rejects_free_text_verdict(kanban_home):
    """A verdict in prose cannot authorize a review transition."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None
        with pytest.raises(kb.ReviewVerdictRequiredError):
            kb.complete_task(
                conn,
                t,
                result="reviewed",
                summary="Verdict: NEEDS_REVISION",
                review_gate=True,
            )
        assert kb.get_task(conn, t).status == "running"
        assert _latest_run_verdict(conn, t) is None


def test_b2_review_complete_extracts_metadata_verdict_synonym(kanban_home):
    """Structured reviewer metadata is normalized before column write."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None
        ok = kb.complete_task(
            conn,
            t,
            result="reviewed",
            summary="done",
            metadata={"review_verdict": "changes-requested"},
            review_gate=True,
        )
        assert ok is True
        assert _latest_run_verdict(conn, t) == "REQUEST_CHANGES"
        assert kb.get_task(conn, t).status == "blocked"


def test_b2_review_block_extracts_metadata_verdict_synonym(kanban_home):
    """Block path uses the same reviewer verdict normalization."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None
        ok = kb.block_task(
            conn,
            t,
            reason="blocking after review",
            reviewer_metadata={"review": {"verdict": "needs revision"}},
        )
        assert ok is True
        assert _latest_run_verdict(conn, t) == "REQUEST_CHANGES"


def test_b2_set_run_verdict_requires_existing_run_row(kanban_home):
    """The verdict update is atomic: exactly one task_runs row must change."""
    with kb.connect_closing() as conn:
        assert kb._set_run_verdict(conn, 999_999_999, "APPROVED") is False


def test_b2_explicit_approved_not_overwritten_by_later_verdict(kanban_home, monkeypatch):
    """The first structured run verdict remains immutable."""
    monkeypatch.setattr(
        kb,
        "_review_stages_for_tier",
        lambda tier, cfg: ["verifier", "critic"],
    )
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder", review_tier="critical")
        assert kb._submit_for_review(
            conn,
            t,
            verified_cards=[],
            target_profile="verifier",
            stage=0,
            effective_tier="critical",
            result=None,
            summary=None,
            metadata=None,
            expected_run_id=None,
        )
        assert kb.claim_review_task(conn, t, reviewer_profile="verifier") is not None
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()["current_run_id"]

        assert kb.complete_task(
            conn,
            t,
            summary="verifier approved",
            metadata={"review_verdict": "APPROVED"},
            review_gate=True,
        ) is True
        assert _latest_run_verdict(conn, t) == "APPROVED"

        assert kb._set_run_verdict(conn, run_id, "REQUEST_CHANGES") is False
        assert _latest_run_verdict(conn, t) == "APPROVED"


def test_b2_non_review_complete_leaves_verdict_null(kanban_home):
    """An ordinary coder completion leaves task_runs.verdict NULL."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="code", assignee="coder")
        kb.claim_task(conn, t)
        kb.complete_task(conn, t, result="done", summary="done")
        assert _latest_run_verdict(conn, t) is None


def test_b2_non_review_block_leaves_verdict_null(kanban_home):
    """An ordinary block (coder hit a wall) leaves task_runs.verdict NULL."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="code", assignee="coder")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="stuck")
        assert _latest_run_verdict(conn, t) is None


def test_b2_metadata_verdict_field_is_untouched(kanban_home):
    """Back-compat: an existing metadata['verdict'] free-form value is NOT
    promoted into the new column, and stays intact on the run metadata."""
    import json
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="code", assignee="coder")
        kb.claim_task(conn, t)
        kb.complete_task(
            conn, t, result="done", summary="done",
            metadata={"verdict": "free-form-note"},
        )
        # Column stays NULL (non-review run)...
        assert _latest_run_verdict(conn, t) is None
        # ...and the metadata key is preserved verbatim.
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert json.loads(row["metadata"])["verdict"] == "free-form-note"


# ---------------------------------------------------------------------------
# Family Organizer backlog write-back on terminal done
# ---------------------------------------------------------------------------

def _frontmatter_dict(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    for line in lines[1:end]:
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip()
    return data


def _write_fo_backlog_item(path: Path, *, status: str = "next") -> None:
    path.write_text(
        "---\n"
        "id: 0141\n"
        "title: Shopping-Favoriten Chips\n"
        f"status: {status}\n"
        "owner: hermes\n"
        "risk: medium\n"
        "area: shopping\n"
        "updated: 2026-06-01\n"
        "---\n\n"
        "## Kontext\n\n"
        "Analog zu FO Beispiel 141.\n",
        encoding="utf-8",
    )


def test_fo_backlog_item_closes_only_on_terminal_flow_done(
    kanban_home, tmp_path, monkeypatch
):
    """Regression: FO tasks copied into Fleet close their source backlog item
    only once the flow reaches terminal done, not at coder->review handoff."""
    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path))
    monkeypatch.setattr(kb.time, "time", lambda: 1781049600)  # 2026-06-10 UTC
    item = tmp_path / "0141-shopping-favoriten-chips-aus-historie.md"
    _write_fo_backlog_item(item)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="[FO] Favoriten-Chips",
            assignee="coder",
            tenant="family-organizer",
            idempotency_key="fo-backlog:0141",
        )
        kb.claim_task(conn, task_id)
        assert kb._submit_for_review(
            conn,
            task_id,
            result=None,
            summary="Implemented favorite chips from history",
            metadata={"changed_files": ["web/src/shopping.tsx"]},
            verified_cards=[],
            expected_run_id=None,
        )
        assert _frontmatter_dict(item)["status"] == "next"

        assert kb.claim_review_task(conn, task_id) is not None
        assert kb.complete_task(
            conn,
            task_id,
            result="APPROVED",
            summary="APPROVED",
            metadata={"review_verdict": "APPROVED"},
            review_gate=True,
        )

        fm = _frontmatter_dict(item)
        assert fm["status"] == "done"
        assert fm["updated"] == "2026-06-10"
        assert fm["result"] == "Implemented favorite chips from history"
        events = [
            e for e in kb.list_events(conn, task_id)
            if e.kind == "family_organizer_backlog_closed"
        ]
        assert len(events) == 1
        assert events[0].payload is not None
        assert events[0].payload["item_id"] == "0141"


def test_fo_backlog_close_ignores_unlinked_family_organizer_tasks(
    kanban_home, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path))
    item = tmp_path / "0141-shopping-favoriten-chips-aus-historie.md"
    _write_fo_backlog_item(item)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="[FO] unrelated",
            assignee="coder",
            tenant="family-organizer",
        )
        kb.claim_task(conn, task_id)
        assert kb.complete_task(conn, task_id, summary="unrelated done")

    assert _frontmatter_dict(item)["status"] == "next"


# ---------------------------------------------------------------------------
# A1 (N-A1): acceptance-criteria column + body parser
# ---------------------------------------------------------------------------

def test_a1_acceptance_criteria_column_present_and_migrate_idempotently(
    kanban_home,
):
    with kb.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "acceptance_criteria" in cols
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)")]
        assert cols2.count("acceptance_criteria") == 1


def test_a1_parse_extracts_ac_bullets():
    import json
    body = (
        "Goal: ship it.\n"
        "- AC-1: endpoint returns 200 — verification: curl\n"
        "* AC-2: row persisted — done_signal: row present\n"
        "- a non-AC bullet that should be ignored\n"
    )
    raw = kb._parse_acceptance_criteria(body)
    parsed = json.loads(raw)
    assert len(parsed) == 2
    assert "AC-1" in parsed[0]
    assert "AC-2" in parsed[1]


def test_a1_parse_none_for_empty_or_missing():
    assert kb._parse_acceptance_criteria(None) is None
    assert kb._parse_acceptance_criteria("") is None
    assert kb._parse_acceptance_criteria("   \n  ") is None


def test_a1_parse_none_when_no_ac_ids():
    body = (
        "Just prose.\n"
        "- implement the feature\n"
        "- tests run\n"
        "- documentation updated\n"
    )
    assert kb._parse_acceptance_criteria(body) is None


def test_a1_parse_numbered_bullets():
    import json
    body = "1. AC-1: works — verification: test\n2) AC-2: persists\n"
    parsed = json.loads(kb._parse_acceptance_criteria(body))
    assert len(parsed) == 2


# ---------------------------------------------------------------------------
# A2 (N-A2): verifier binding — review context + acceptance_roles config
# ---------------------------------------------------------------------------

@requires_git
def test_a2_review_context_has_checklist_and_changed_files(kanban_home, tmp_path):
    import json
    repo = tmp_path / "ws"
    repo.mkdir()
    _init_git_repo_with_changes(repo)
    with kb.connect_closing() as conn:
        t = kb.create_task(
            conn, title="widget", assignee="coder",
            workspace_kind="dir", workspace_path=str(repo),
            initial_status="running",
        )
        # A1 column is normally filled at decompose; set it directly here.
        conn.execute(
            "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
            (json.dumps(["AC-1: endpoint returns 200",
                         "AC-2: widget row persisted"]), t),
        )
        # Coder submits → B1 snapshot rides the submitted_for_review event.
        kb._submit_for_review(
            conn, t, result="done", summary="done", metadata=None,
            verified_cards=[], expected_run_id=None,
        )
        # Verifier claims the review lane → its run is the current run.
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None
        ctx = kb.build_worker_context(conn, t)
    assert "Acceptance checklist" in ctx
    assert "AC-1: endpoint returns 200" in ctx
    assert "AC-2: widget row persisted" in ctx
    assert "Changed files at submit" in ctx
    assert "tracked.py" in ctx
    assert "caller" in ctx.lower()


def test_a2_review_context_fallbacks_when_no_acs_no_snapshot(kanban_home):
    """Review run with NULL acceptance_criteria and no diff snapshot → both
    fallback notes render, no crash."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="x", assignee="coder")
        _set_task_status(conn, t, "review")
        claimed = kb.claim_review_task(conn, t)
        assert claimed is not None
        ctx = kb.build_worker_context(conn, t)
    assert "No structured acceptance criteria" in ctx
    assert "No machine diff snapshot" in ctx


def test_a2_non_review_context_has_no_review_section(kanban_home):
    """Regression: an ordinary worker's context carries NONE of the A2 section,
    preserving the pre-A2 output."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="code", assignee="coder")
        kb.claim_task(conn, t)
        ctx = kb.build_worker_context(conn, t)
    assert "Acceptance checklist" not in ctx
    assert "Changed files at submit" not in ctx


# ---------------------------------------------------------------------------
# A1-classaware: task-class-aware verifier context header
# (EXPLICIT kind='analysis' = read-only; everything else stays default-strict)
# ---------------------------------------------------------------------------

def _claimed_review_section(conn, *, kind=None, acceptance=None):
    """Create a task (optionally kind-marked), drive it into the review lane,
    claim it as the verifier, and return its rendered review-section as text."""
    t = kb.create_task(conn, title="probe", assignee="coder-claude", kind=kind)
    if acceptance is not None:
        conn.execute(
            "UPDATE tasks SET acceptance_criteria = ? WHERE id = ?",
            (json.dumps(acceptance), t),
        )
    _set_task_status(conn, t, "review")
    assert kb.claim_review_task(conn, t) is not None
    return t, "\n".join(kb._render_review_verifier_section(conn, t))


def test_verifier_section_analysis_kind_emits_class_header(kanban_home):
    """kind='analysis' surfaces the read-only task-class header in the verifier
    Acceptance checklist block; AC items still render. End-to-end through
    build_worker_context so the header actually reaches the verifier."""
    with kb.connect_closing() as conn:
        t, section = _claimed_review_section(
            conn, kind="analysis",
            acceptance=["AC-1: report the bound type + lever"],
        )
        ctx = kb.build_worker_context(conn, t)
    assert "Task-Klasse: analysis" in section
    assert "BEOBACHTUNGEN, KEINE Blocker" in section
    # header lives inside the acceptance-checklist block, AC items still render
    assert "Acceptance checklist" in section
    assert "AC-1: report the bound type + lever" in section
    # and it survives into the full worker context the verifier actually sees
    assert "Task-Klasse: analysis" in ctx


def test_verifier_section_code_kind_has_no_class_header(kanban_home):
    """kind='code' (a build task) must NOT emit the analysis header —
    default-strict is preserved for everything that is not explicit analysis."""
    with kb.connect_closing() as conn:
        _t, section = _claimed_review_section(
            conn, kind="code",
            acceptance=["AC-1: endpoint returns 200"],
        )
    assert "Task-Klasse: analysis" not in section
    assert "Acceptance checklist" in section
    assert "AC-1: endpoint returns 200" in section


def test_verifier_section_unmarked_identical_to_code_default_strict(kanban_home):
    """Default-strict invariant: an UNMARKED task renders byte-identically to a
    kind='code' task. The marker only ever ADDS the analysis header; it never
    changes the strict default rendering."""
    acceptance = ["AC-1: endpoint returns 200", "AC-2: row persisted"]
    with kb.connect_closing() as conn:
        _tu, section_unmarked = _claimed_review_section(
            conn, kind=None, acceptance=acceptance,
        )
        _tc, section_code = _claimed_review_section(
            conn, kind="code", acceptance=acceptance,
        )
    assert "Task-Klasse: analysis" not in section_unmarked
    assert section_unmarked == section_code


def test_a2_acceptance_roles_default_empty_is_noop(kanban_home):
    cfg = kb._review_gate_config()
    assert cfg["acceptance_roles"] == frozenset()
    # Default code_roles unchanged (union with ∅).
    assert cfg["code_roles"] == frozenset(kb._DEFAULT_REVIEW_CODE_ROLES)
    assert "coder-claude" in cfg["code_roles"]


def test_review_gate_config_string_false_flags_are_disabled(kanban_home):
    import yaml
    (kanban_home / "config.yaml").write_text(
        yaml.safe_dump({
            "kanban": {"review_gate": {
                "enabled": "false",
                "auto_tier": "false",
                "auto_scout_on_critical": "false",
            }}
        }),
        encoding="utf-8",
    )
    cfg = kb._review_gate_config()
    assert cfg["enabled"] is False
    assert cfg["auto_tier"] is False
    assert cfg["auto_scout_on_critical"] is False


def test_worker_gate_config_string_false_flag_is_disabled(kanban_home):
    import yaml
    (kanban_home / "config.yaml").write_text(
        yaml.safe_dump({
            "kanban": {"worker_gate": {"enabled": "false"}}
        }),
        encoding="utf-8",
    )
    cfg = kb._worker_gate_config()
    assert cfg["enabled"] is False


def test_a2_acceptance_roles_union_into_code_roles(kanban_home):
    import yaml
    (kanban_home / "config.yaml").write_text(
        yaml.safe_dump({
            "kanban": {"review_gate": {
                "enabled": True, "acceptance_roles": ["docs", "qa"],
            }}
        }),
        encoding="utf-8",
    )
    cfg = kb._review_gate_config()
    assert cfg["acceptance_roles"] == frozenset({"docs", "qa"})
    assert {"docs", "qa"} <= cfg["code_roles"]
    # Defaults preserved alongside the additions.
    assert frozenset(kb._DEFAULT_REVIEW_CODE_ROLES) <= cfg["code_roles"]





# ---------------------------------------------------------------------------
# E1 (N-E1): consolidated decision queue
# ---------------------------------------------------------------------------

def _kinds_for(task_id, result):
    return [d["kind"] for d in result["decisions"] if d["task_id"] == task_id]


def test_e1_decision_queue_empty_board(kanban_home):
    with kb.connect_closing() as conn:
        result = kb.decision_queue(conn)
    assert result["decisions"] == []
    assert result["count"] == 0
    assert "checked_at" in result


def test_e1_decision_queue_sticky_blocked_appears_once(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="stuck", assignee="coder")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="needs human eyes")
        result = kb.decision_queue(conn)
    assert _kinds_for(t, result) == ["sticky_blocked"]
    row = next(d for d in result["decisions"] if d["task_id"] == t)
    assert row["suggested_command"] == f"hermes kanban unblock {t}"
    assert row["age_seconds"] is not None


def test_e1_decision_queue_review_rejection_outranks_sticky(kanban_home):
    """A blocked task whose latest run was a verifier REQUEST_CHANGES is
    classified as review_rejected, not the generic sticky_blocked — appears
    exactly once."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="review me", assignee="coder")
        _set_task_status(conn, t, "review")
        kb.claim_review_task(conn, t)
        kb.block_task(conn, t, reason="missing tests")
        assert _latest_run_verdict(conn, t) == "REQUEST_CHANGES"
        result = kb.decision_queue(conn)
    assert _kinds_for(t, result) == ["review_rejected"]


def test_4b_decision_queue_operator_escalation_outranks_sticky(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="needs operator", assignee="coder")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="needs human eyes")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                t,
                kb.OPERATOR_ESCALATION_EVENT,
                {
                    "task": {"id": t, "title": "needs operator"},
                    "why_now": "retry ladder exhausted",
                    "attempts_already_made": 2,
                    "evidence": {},
                    "recommended_human_action": "inspect",
                    "blocked_action_boundary": list(kb.OPERATOR_ONLY_ACTIONS),
                },
            )
        result = kb.decision_queue(conn)

    assert _kinds_for(t, result) == ["operator_escalation"]
    row = next(d for d in result["decisions"] if d["task_id"] == t)
    assert row["reason"] == "retry ladder exhausted"
    assert row["suggested_command"] == f"hermes kanban show {t}"


def test_4b_decision_queue_specific_recovery_classes_beat_generic_escalation(
    kanban_home,
):
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        parked = kb.create_task(conn, title="merge parked", assignee="coder")
        kb.claim_task(conn, parked)
        kb.block_task(conn, parked, reason="integration parked: merge gate red")

        limited = kb.create_task(conn, title="quota loop", assignee="coder")
        with kb.write_txn(conn):
            for i in range(3):
                conn.execute(
                    "INSERT INTO task_runs "
                    "(task_id, profile, status, outcome, error, started_at, ended_at) "
                    "VALUES (?, 'coder', 'rate_limited', 'rate_limited', "
                    "'429 quota', ?, ?)",
                    (limited, now - 100 - i, now - 90 - i),
                )

        # The rate-limit loop now runs through the bounded transient-retry
        # budget first (HEILER-TRANSIENT-RETRY-BUDGET-S1); sweep until it is
        # exhausted and escalates, so the decision queue sees the recovery class.
        for k in range(kb.TRANSIENT_RETRY_LIMIT + 1):
            kb.no_silent_stall_sweep(conn, now=now + k, rate_limit_attempt_limit=3)
        result = kb.decision_queue(conn, now=now + 10)

    assert _kinds_for(parked, result) == ["integration_parked"]
    assert _kinds_for(limited, result) == ["rate_limited_loop"]
    parked_row = next(d for d in result["decisions"] if d["task_id"] == parked)
    limited_row = next(d for d in result["decisions"] if d["task_id"] == limited)
    assert "integration parked:" in parked_row["reason"]
    assert "rate-limit loop" in limited_row["reason"]


def test_4b_decision_queue_skips_funnel_root_but_not_child(kanban_home):
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn, title="funnel root", assignee="research", created_by="family",
        )
        done_root = kb.create_task(
            conn, title="approved root", assignee="research", created_by="family",
        )
        kb.claim_task(conn, done_root)
        kb.complete_task(conn, done_root, summary="draft approved")
        child = kb.create_task(
            conn,
            title="approved build child",
            assignee="coder",
            created_by="family",
            parents=(done_root,),
        )
        with kb.write_txn(conn):
            for task_id in (root, child):
                kb._append_event(
                    conn,
                    task_id,
                    kb.OPERATOR_ESCALATION_EVENT,
                    {
                        "task": {"id": task_id},
                        "why_now": "operator must decide",
                        "attempts_already_made": 1,
                        "evidence": {},
                        "recommended_human_action": "inspect",
                        "blocked_action_boundary": list(kb.OPERATOR_ONLY_ACTIONS),
                    },
                )
        result = kb.decision_queue(conn)

    assert _kinds_for(root, result) == []
    assert _kinds_for(child, result) == ["operator_escalation"]


def test_e1_decision_queue_role_fit_held(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="reviewer probe", assignee="reviewer")
        _set_task_status(conn, t, "ready")
        with kb.write_txn(conn):
            kb._append_event(conn, t, "role_fit_held", {"reason": "wants repo gates"})
        result = kb.decision_queue(conn)
    assert _kinds_for(t, result) == ["role_fit_held"]
    row = next(d for d in result["decisions"] if d["task_id"] == t)
    assert "wants repo gates" in row["reason"]


def test_e1_decision_queue_decompose_failed(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="undecomposable", assignee="coder")
        kb.record_decompose_failure(conn, t)
        kb.record_decompose_failure(conn, t)
        result = kb.decision_queue(conn)
    assert _kinds_for(t, result) == ["decompose_failed"]
    row = next(d for d in result["decisions"] if d["task_id"] == t)
    assert "2" in row["reason"]


def test_e1_decision_queue_done_task_with_decompose_failed_excluded(kanban_home):
    """A completed task that once failed decompose is not a pending decision."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="done now", assignee="coder")
        kb.record_decompose_failure(conn, t)
        kb.claim_task(conn, t)
        kb.complete_task(conn, t, result="done", summary="done")
        result = kb.decision_queue(conn)
    assert _kinds_for(t, result) == []


def test_e1_decision_queue_failsoft_on_corrupt_event_payload(kanban_home):
    """A blocked task with a non-JSON event payload must not crash the queue."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="stuck", assignee="coder")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="x")
        # Corrupt the blocked-event payload directly.
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'blocked'",
            ("{not json", t),
        )
        conn.commit()
        result = kb.decision_queue(conn)
    # Still surfaces (fail-soft reason fallback), no exception.
    assert _kinds_for(t, result) == ["sticky_blocked"]


# ---------------------------------------------------------------------------
# E3 (N-E3): durable epics + tasks.epic_id + propagation
# ---------------------------------------------------------------------------

def test_e3_epic_id_column_and_table_migrate_idempotently(kanban_home):
    with kb.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "epic_id" in cols
        tables = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "epics" in tables
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)")]
        assert cols2.count("epic_id") == 1


def test_e3_create_and_list_epic(kanban_home):
    with kb.connect_closing() as conn:
        eid = kb.create_epic(conn, title="Q3 reliability", body="close the loops")
        assert eid.startswith("e_")
        epics = kb.list_epics(conn)
    assert len(epics) == 1
    assert epics[0]["id"] == eid
    assert epics[0]["title"] == "Q3 reliability"
    assert epics[0]["status"] == "open"
    assert epics[0]["task_count"] == 0


def test_e3_create_task_with_epic_sets_column(kanban_home):
    with kb.connect_closing() as conn:
        eid = kb.create_epic(conn, title="epic")
        t = kb.create_task(conn, title="member", assignee="coder", epic_id=eid)
        task = kb.get_task(conn, t)
        assert task.epic_id == eid


def test_e3_task_without_epic_is_null(kanban_home):
    """Regression guard: the common path leaves epic_id NULL (pre-E3)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="loner", assignee="coder")
        assert kb.get_task(conn, t).epic_id is None


def test_e3_decompose_propagates_epic_to_children(kanban_home):
    with kb.connect_closing() as conn:
        eid = kb.create_epic(conn, title="epic")
        root = kb.create_task(
            conn, title="root", assignee="orchestrator",
            triage=True, epic_id=eid,
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[
                {"title": "child A"},
                {"title": "child B", "parents": [0]},
            ],
        )
        assert child_ids is not None and len(child_ids) == 2
        for cid in child_ids:
            assert kb.get_task(conn, cid).epic_id == eid


def test_e3_decompose_without_epic_leaves_children_null(kanban_home):
    """Regression guard: a root with no epic → children stay NULL."""
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn, title="root", assignee="orchestrator", triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[{"title": "child A"}],
        )
        assert kb.get_task(conn, child_ids[0]).epic_id is None


def test_e3_epic_stats_count_and_cost(kanban_home):
    with kb.connect_closing() as conn:
        eid = kb.create_epic(conn, title="epic")
        t1 = kb.create_task(conn, title="t1", assignee="coder", epic_id=eid)
        t2 = kb.create_task(conn, title="t2", assignee="coder", epic_id=eid)
        # t1 completes, t2 stays open.
        kb.claim_task(conn, t1)
        kb.complete_task(conn, t1, result="done", summary="done")
        # Attribute some cost to t2's run.
        kb.claim_task(conn, t2)
        conn.execute(
            "UPDATE task_runs SET cost_usd = 0.5, input_tokens = 100, "
            "output_tokens = 40 WHERE task_id = ?",
            (t2,),
        )
        conn.commit()
        epic = kb.get_epic(conn, eid)
    assert epic["task_count"] == 2
    assert epic["done_tasks"] == 1
    assert epic["open_tasks"] == 1
    assert epic["cost_usd"] == 0.5
    assert epic["input_tokens"] == 100
    assert {row["id"] for row in epic["tasks"]} == {t1, t2}


def test_e3_close_epic(kanban_home):
    with kb.connect_closing() as conn:
        eid = kb.create_epic(conn, title="epic")
        assert kb.close_epic(conn, eid) is True
        assert kb.get_epic(conn, eid)["status"] == "closed"
        assert kb.close_epic(conn, "e_ghost") is False


def test_e3_get_missing_epic_returns_none(kanban_home):
    with kb.connect_closing() as conn:
        assert kb.get_epic(conn, "e_nope") is None


def test_e3_set_task_epic_attach_and_detach(kanban_home):
    with kb.connect_closing() as conn:
        eid = kb.create_epic(conn, title="epic")
        t = kb.create_task(conn, title="late member", assignee="coder")
        assert kb.set_task_epic(conn, t, eid) is True
        assert kb.get_task(conn, t).epic_id == eid
        # Detach (explicit None) always works.
        assert kb.set_task_epic(conn, t, None) is True
        assert kb.get_task(conn, t).epic_id is None
        # Both moves leave an audit event.
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
                (t,),
            )
        ]
        assert kinds.count("epic_changed") == 2


def test_e3_set_task_epic_validates_target(kanban_home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="member", assignee="coder")
        # Unknown task → False, no crash.
        assert kb.set_task_epic(conn, "t_ghost", None) is False
        # Unknown epic → ValueError.
        with pytest.raises(ValueError, match="not found"):
            kb.set_task_epic(conn, t, "e_ghost")
        # Closed epic → ValueError on attach …
        eid = kb.create_epic(conn, title="done epic")
        kb.close_epic(conn, eid)
        with pytest.raises(ValueError, match="closed"):
            kb.set_task_epic(conn, t, eid)
        assert kb.get_task(conn, t).epic_id is None
        # … but detaching from a since-closed epic is allowed.
        eid2 = kb.create_epic(conn, title="open then closed")
        kb.set_task_epic(conn, t, eid2)
        kb.close_epic(conn, eid2)
        assert kb.set_task_epic(conn, t, None) is True
        assert kb.get_task(conn, t).epic_id is None


# ---------------------------------------------------------------------------
# C1 (N-C1): daily budget gate in dispatch preflight (off by default)
# ---------------------------------------------------------------------------

def _seed_run(conn, task_id, *, profile, tokens=0, cost=None, age_seconds=10):
    """Insert a synthetic ended run with token/cost accounting for budget tests."""
    started = int(time.time()) - age_seconds
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, "
        "outcome, input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, 'done', ?, ?, 'completed', ?, 0, ?)",
        (task_id, profile, started, started, tokens, cost),
    )
    conn.commit()


def test_c1_caps_off_is_byte_identical(kanban_home, all_assignees_spawnable):
    """Caps unset (the live default) → no hold even with heavy prior usage."""
    spawns = []
    with kb.connect_closing() as conn:
        prior = kb.create_task(conn, title="prior", assignee="alice")
        _seed_run(conn, prior, profile="alice", tokens=10_000_000)
        t = kb.create_task(conn, title="ready", assignee="alice")
        res = kb.dispatch_once(conn, spawn_fn=lambda task, ws: spawns.append(task.id))
        assert res.budget_held == []
        assert t in spawns
        assert kb.get_task(conn, t).status == "running"


def test_c1_token_cap_holds_only_over_budget_profile(
    kanban_home, all_assignees_spawnable
):
    spawns = []
    with kb.connect_closing() as conn:
        # alice has blown her token budget; bob has not.
        prior = kb.create_task(conn, title="prior", assignee="alice")
        _seed_run(conn, prior, profile="alice", tokens=5000)
        ta = kb.create_task(conn, title="alice task", assignee="alice")
        tb = kb.create_task(conn, title="bob task", assignee="bob")
        res = kb.dispatch_once(
            conn, spawn_fn=lambda task, ws: spawns.append(task.id),
            daily_token_cap_per_profile=1000,
        )
        held_ids = [x[0] for x in res.budget_held]
        assert ta in held_ids and tb not in held_ids
        assert ta not in spawns and tb in spawns
        assert kb.get_task(conn, ta).status == "ready"  # held, not blocked
        # Exactly one budget_held event.
        n = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'budget_held'",
            (ta,),
        ).fetchone()[0]
        assert n == 1


def test_c1_token_cap_event_deduped_across_ticks(
    kanban_home, all_assignees_spawnable
):
    with kb.connect_closing() as conn:
        prior = kb.create_task(conn, title="prior", assignee="alice")
        _seed_run(conn, prior, profile="alice", tokens=5000)
        ta = kb.create_task(conn, title="alice task", assignee="alice")
        kb.dispatch_once(conn, spawn_fn=lambda t, ws: None, daily_token_cap_per_profile=1000)
        kb.dispatch_once(conn, spawn_fn=lambda t, ws: None, daily_token_cap_per_profile=1000)
        n = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'budget_held'",
            (ta,),
        ).fetchone()[0]
        assert n == 1


def test_c1_cost_cap_holds_board_wide(kanban_home, all_assignees_spawnable):
    spawns = []
    with kb.connect_closing() as conn:
        prior = kb.create_task(conn, title="prior", assignee="alice")
        _seed_run(conn, prior, profile="alice", cost=2.50)
        ta = kb.create_task(conn, title="alice task", assignee="alice")
        tb = kb.create_task(conn, title="bob task", assignee="bob")
        res = kb.dispatch_once(
            conn, spawn_fn=lambda task, ws: spawns.append(task.id),
            daily_cost_cap_usd=1.0,
        )
        held_ids = {x[0] for x in res.budget_held}
        assert {ta, tb} <= held_ids  # board-wide hold (prior is held too)
        assert spawns == []


def test_c1_null_tokens_count_as_zero(kanban_home, all_assignees_spawnable):
    spawns = []
    with kb.connect_closing() as conn:
        prior = kb.create_task(conn, title="prior", assignee="alice")
        # A run with NULL tokens contributes 0 → under any positive cap.
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
            "VALUES (?, 'alice', 'done', ?, ?, 'completed')",
            (prior, int(time.time()) - 5, int(time.time()) - 5),
        )
        conn.commit()
        t = kb.create_task(conn, title="ready", assignee="alice")
        res = kb.dispatch_once(
            conn, spawn_fn=lambda task, ws: spawns.append(task.id),
            daily_token_cap_per_profile=1000,
        )
        assert res.budget_held == []
        assert t in spawns


def test_c1_budget_held_surfaces_in_decision_queue(
    kanban_home, all_assignees_spawnable
):
    with kb.connect_closing() as conn:
        prior = kb.create_task(conn, title="prior", assignee="alice")
        _seed_run(conn, prior, profile="alice", tokens=5000)
        ta = kb.create_task(conn, title="alice task", assignee="alice")
        kb.dispatch_once(conn, spawn_fn=lambda t, ws: None, daily_token_cap_per_profile=1000)
        result = kb.decision_queue(conn)
    assert "budget_held" in _kinds_for(ta, result)


# ---------------------------------------------------------------------------
# C1 kanban-chain-haertung: tree_root_woke + release_gate_parked
# ---------------------------------------------------------------------------


def test_c1_tree_root_woke_all_children_done(kanban_home):
    """A decompose root that is 'ready' with all children 'done' surfaces
    as tree_root_woke. Reuses the same all-children-done predicate as
    recompute_ready (only 'done' counts; not archived/failed)."""
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="root task", assignee="orchestrator")
        child1 = kb.create_task(conn, title="child A", assignee="coder")
        child2 = kb.create_task(conn, title="child B", assignee="coder")
        # A decompose root DEPENDS ON its subtasks: the root is the child_id and
        # each subtask is a parent_id (the same direction decompose_triage_task
        # creates and recompute_ready reads). Building the links the other way
        # round would make this test pass while the production query never fires.
        with kb.write_txn(conn):
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (child1, root),
            )
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (child2, root),
            )
        kb._append_event(conn, root, "decomposed", {"child_ids": [child1, child2]})
        # Complete both children
        _set_task_status(conn, child1, "done")
        _set_task_status(conn, child2, "done")
        # Root is now ready (woken up by completion)
        _set_task_status(conn, root, "ready")

        result = kb.decision_queue(conn)

    assert _kinds_for(root, result) == ["tree_root_woke"]
    row = next(d for d in result["decisions"] if d["task_id"] == root)
    assert row["suggested_command"] == f"hermes kanban show {root}"
    assert row["age_seconds"] is not None
    # Same shape as existing kinds
    for key in ("kind", "task_id", "title", "reason", "age_seconds", "suggested_command"):
        assert key in row, f"missing key {key!r} in tree_root_woke entry"


def test_c1_tree_root_woke_not_emitted_if_child_not_done(kanban_home):
    """Root must NOT appear when even one child is not yet done."""
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="root task", assignee="orchestrator")
        child1 = kb.create_task(conn, title="child A", assignee="coder")
        child2 = kb.create_task(conn, title="child B", assignee="coder")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (child1, root),
            )
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (child2, root),
            )
        # Only child1 done; child2 still todo
        _set_task_status(conn, child1, "done")
        # child2 stays in 'todo' (default)
        _set_task_status(conn, root, "ready")

        result = kb.decision_queue(conn)

    assert _kinds_for(root, result) == []


def test_c1_tree_root_woke_no_children_excluded(kanban_home):
    """A ready task with NO children must NOT appear as tree_root_woke
    (it was never decomposed)."""
    with kb.connect_closing() as conn:
        leaf = kb.create_task(conn, title="plain ready", assignee="coder")
        _set_task_status(conn, leaf, "ready")
        result = kb.decision_queue(conn)
    assert "tree_root_woke" not in _kinds_for(leaf, result)


def test_c1_release_gate_parked_surfaces_in_decision_queue(kanban_home):
    """A non-terminal task with a release_gate_parked event surfaces in the
    decision queue with a suggested_command from _RELEASE_GATE_COMMANDS."""
    from hermes_cli.kanban_worktrees import _RELEASE_GATE_COMMANDS

    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="release gate task", assignee="verifier")
        # Record the release_gate_parked event (status stays blocked/non-terminal)
        _set_task_status(conn, task, "blocked")
        with kb.write_txn(conn):
            kb._append_event(
                conn, task, "release_gate_parked",
                {
                    "state": "GREEN_CODE_NOT_RUNTIME_ACTIVATED",
                    "reason": "awaiting release-gate GO",
                    "commands": list(_RELEASE_GATE_COMMANDS),
                },
            )

        result = kb.decision_queue(conn)

    assert _kinds_for(task, result) == ["release_gate_parked"]
    row = next(d for d in result["decisions"] if d["task_id"] == task)
    # suggested_command must carry the FULL gate sequence, not just the bare cd
    assert row["suggested_command"]
    for cmd in _RELEASE_GATE_COMMANDS:
        assert cmd in row["suggested_command"]
    assert row["reason"] == "awaiting release-gate GO"
    # Same shape as existing kinds
    for key in ("kind", "task_id", "title", "reason", "age_seconds", "suggested_command"):
        assert key in row, f"missing key {key!r} in release_gate_parked entry"


def test_c1_release_gate_parked_excluded_when_done(kanban_home):
    """A task that carries release_gate_parked but is already done must NOT
    appear in the decision queue."""
    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="done gate", assignee="verifier")
        with kb.write_txn(conn):
            kb._append_event(conn, task, "release_gate_parked", {"reason": "GO"})
        _set_task_status(conn, task, "done")

        result = kb.decision_queue(conn)

    assert _kinds_for(task, result) == []


def test_c1_release_gate_suggested_command_carries_full_sequence(kanban_home):
    """#7: the suggested_command for a release_gate_parked decision must carry the
    FULL command sequence from the event payload — not just the first bare ``cd``.

    Regression for the original ``next(iter(_RELEASE_GATE_COMMANDS))`` which
    surfaced only ``cd .../web`` (a no-op alone) instead of the whole gate."""
    commands = [
        "cd /home/piet/.hermes/hermes-agent/web",
        "npm run build",
        "test -f /home/piet/.hermes/hermes-agent/hermes_cli/web_dist/index.html",
        "curl -fsS http://127.0.0.1:9119/control >/dev/null",
    ]
    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="gate task", assignee="verifier")
        _set_task_status(conn, task, "blocked")
        with kb.write_txn(conn):
            kb._append_event(
                conn, task, "release_gate_parked",
                {"reason": "awaiting release-gate GO", "commands": commands},
            )

        result = kb.decision_queue(conn)

    row = next(d for d in result["decisions"] if d["task_id"] == task)
    suggested = row["suggested_command"]
    # Every command from the payload must be present, chained — not just the cd.
    for cmd in commands:
        assert cmd in suggested, f"{cmd!r} missing from suggested_command {suggested!r}"
    assert "npm run build" in suggested
    assert suggested != commands[0]  # not the bare leading cd


def test_c1_release_gate_suggested_command_falls_back_without_payload_commands(kanban_home):
    """#7: when the event payload has no ``commands`` list, fall back to the
    canonical _RELEASE_GATE_COMMANDS sequence (still the full gate, not a bare cd)."""
    from hermes_cli.kanban_worktrees import _RELEASE_GATE_COMMANDS

    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="gate task no cmds", assignee="verifier")
        _set_task_status(conn, task, "blocked")
        with kb.write_txn(conn):
            kb._append_event(conn, task, "release_gate_parked", {"reason": "GO"})

        result = kb.decision_queue(conn)

    row = next(d for d in result["decisions"] if d["task_id"] == task)
    suggested = row["suggested_command"]
    assert suggested
    for cmd in _RELEASE_GATE_COMMANDS:
        assert cmd in suggested


def test_c1_release_gate_parked_beats_generic_operator_escalation(kanban_home):
    """Regression (2026-07-07 live find): the no-silent-stall safety net
    (``escalate_silent_blocks_sweep``) auto-emits a GENERIC ``operator_escalation``
    event for every settled blocked task — including a release-gate child, which
    has no task_runs at all and so is treated as settled immediately. On the live
    board this lands ~1min after the gate parks, giving the task BOTH a
    ``release_gate_parked`` event (real payload shape from
    ``_create_parked_release_gate_child``) and a real ``operator_escalation``
    event (real payload shape from ``escalate_silent_blocks_sweep`` itself, not a
    hand-authored fake). Before the fix, decision_queue's seen-set let the
    earlier-running generic operator_escalation _add() claim the row first, so
    the more specific release_gate_parked decision — and the ``release_gate``
    button metadata the frontend renders "Release-Gate ausführen" from — never
    surfaced for that task again."""
    from hermes_cli.kanban_worktrees import _RELEASE_GATE_COMMANDS

    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="root task", assignee="orchestrator")
        task = kb.create_task(
            conn,
            title="release gate task",
            assignee="verifier",
            parents=(root,),
            initial_status="blocked",
        )
        merge_commit = "abc123def4560"
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task,
                "release_gate_parked",
                {
                    "state": "GREEN_CODE_NOT_RUNTIME_ACTIVATED",
                    "source_task": root,
                    "root_id": root,
                    "merge_commit": merge_commit,
                    "reason": "awaiting release-gate GO",
                    "commands": list(_RELEASE_GATE_COMMANDS),
                },
            )

        # Real production write path (not a hand-authored event): the silent-
        # block safety net escalates any settled blocked task it finds.
        sweep_summary = kb.escalate_silent_blocks_sweep(conn)
        assert any(e["task_id"] == task for e in sweep_summary["escalated"]), (
            "test setup invalid: the sweep did not escalate the parked task — "
            "the scenario this regression guards against was not reproduced"
        )
        # Confirm the real operator_escalation event actually landed, so the
        # test is provably exercising the precedence race, not a no-op sweep.
        assert conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'operator_escalation'",
            (task,),
        ).fetchone() is not None

        result = kb.decision_queue(conn)

    # The specific release_gate_parked decision must win — not the generic
    # operator_escalation the sweep also wrote for the very same task.
    assert _kinds_for(task, result) == ["release_gate_parked"]
    row = next(d for d in result["decisions"] if d["task_id"] == task)
    assert row["kind"] == "release_gate_parked"
    assert "release_gate" in row, "release-gate button metadata missing from row"
    assert row["release_gate"]["root_id"] == root
    assert row["release_gate"]["source_task_id"] == root
    assert row["release_gate"]["merge_commit"] == merge_commit


# ---------------------------------------------------------------------------
# F5 (night-sprint): scores-Tabelle + Review-Verdicts als Eval-Baseline
# ---------------------------------------------------------------------------


def _insert_bare_run(conn, task_id, *, started_at, ended_at=None, verdict=None):
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, verdict) "
        "VALUES (?, 'coder', 'done', ?, ?, ?)",
        (task_id, started_at, ended_at, verdict),
    )
    return cur.lastrowid


def test_set_run_verdict_records_binary_score(kanban_home):
    """APPROVED→1.0 / REQUEST_CHANGES→0.0 landen automatisch in scores;
    erneutes Verdict auf demselben Run erzeugt keine zweite Zeile."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="judged work")
        with kb.write_txn(conn):
            r_ok = _insert_bare_run(conn, t, started_at=1000, ended_at=1300)
            r_bad = _insert_bare_run(conn, t, started_at=2000, ended_at=2300)
            kb._set_run_verdict(conn, r_ok, "APPROVED")
            kb._set_run_verdict(conn, r_bad, "REQUEST_CHANGES")
            kb._set_run_verdict(conn, r_ok, "APPROVED")  # idempotent
        rows = conn.execute(
            "SELECT run_id, task_id, name, value, value_type, source "
            "FROM scores ORDER BY run_id",
        ).fetchall()
    assert [(r["run_id"], r["value"]) for r in rows] == [(r_ok, 1.0), (r_bad, 0.0)]
    for r in rows:
        assert r["task_id"] == t
        assert r["name"] == "review_verdict"
        assert r["value_type"] == "binary"
        assert r["source"] == "review_gate"


def test_set_run_verdict_score_fails_soft_without_table(kanban_home):
    """Score-Spiegelung darf einen Abschluss nie brechen (Legacy-DB ohne
    scores-Tabelle): Verdict bleibt gesetzt, kein Raise."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="legacy")
        with kb.write_txn(conn):
            r = _insert_bare_run(conn, t, started_at=1000)
            conn.execute("DROP TABLE scores")
            kb._set_run_verdict(conn, r, "APPROVED")
        row = conn.execute(
            "SELECT verdict FROM task_runs WHERE id = ?", (r,)
        ).fetchone()
    assert row["verdict"] == "APPROVED"


def test_backfill_verdict_scores_idempotent_with_run_timestamps(kanban_home):
    """Backfill spiegelt historische Verdicts mit Run-Endzeit als created_at,
    überspringt verdictlose Runs und ist wiederholbar (0 beim 2. Lauf)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="history")
        with kb.write_txn(conn):
            r1 = _insert_bare_run(conn, t, started_at=1000, ended_at=1500, verdict="APPROVED")
            r2 = _insert_bare_run(conn, t, started_at=2000, ended_at=None, verdict="REQUEST_CHANGES")
            _insert_bare_run(conn, t, started_at=3000, ended_at=3100)  # kein Verdict
        assert kb.backfill_verdict_scores(conn) == 2
        assert kb.backfill_verdict_scores(conn) == 0  # idempotent
        rows = {
            r["run_id"]: r for r in conn.execute(
                "SELECT run_id, value, created_at FROM scores",
            ).fetchall()
        }
    assert rows[r1]["value"] == 1.0 and rows[r1]["created_at"] == 1500
    # ohne ended_at fällt der Zeitstempel ehrlich auf started_at zurück
    assert rows[r2]["value"] == 0.0 and rows[r2]["created_at"] == 2000


def test_scores_name_created_query_uses_index(kanban_home):
    """Trend-Queries (name + Zeitfenster) laufen über idx_scores_name_created —
    der Index ist die <200ms@10k-Garantie, deterministischer als Timing."""
    with kb.connect_closing() as conn:
        plan = " ".join(
            row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT AVG(value) FROM scores "
                "WHERE name = 'review_verdict' AND created_at >= 0",
            )
        )
    assert "idx_scores_name_created" in plan


# ---------------------------------------------------------------------------
# F6 (night-sprint): Issue-Gruppierung über Fehler-Signaturen
# ---------------------------------------------------------------------------


def test_issue_signature_normalisation_cases():
    """Mind. 5 Fälle: PIDs/Zähler/IDs/Hex maskiert, Whitespace kollabiert,
    erste nicht-leere Zeile zählt, leerer Text wird ehrlich benannt."""
    sig = kb._issue_signature
    # 1+2: gleiche PID-Fehlerklasse → identische Signatur trotz anderer PID
    assert sig("pid 4053999 exited with code 1") == "pid N exited with code N"
    assert sig("pid 12 exited with code 1") == "pid N exited with code N"
    # 3: Iterations-Zähler maskiert
    assert sig("Iteration budget exhausted (60/60) — task could not complete") \
        == "Iteration budget exhausted (N/N) — task could not complete"
    # 4: Task-IDs maskiert
    assert sig("worker for t_82c04f63 vanished") == "worker for t_… vanished"
    # 5: lange Hex-IDs maskiert
    assert sig("session 0123456789abcdef crashed") == "session … crashed"
    # 6: Mehrzeiler → erste nicht-leere Zeile, Whitespace kollabiert
    assert sig("\n\n  Error:   boom   \nTraceback ...") == "Error: boom"
    # 7: leer/None
    assert sig("") == "(kein Fehlertext)"
    assert sig(None) == "(kein Fehlertext)"


def test_runs_issues_groups_by_profile_and_signature(kanban_home):
    """Gleicher Fehlertyp + gleiches Profil = ein Issue mit Zähler; blocked
    fällt auf summary zurück; Beispiel-Run ist das jüngste Auftreten."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="flaky")
        with kb.write_txn(conn):
            def run(profile, outcome, started, error=None, summary=None):
                return conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, outcome, "
                    "started_at, ended_at, error, summary) VALUES (?,?,?,?,?,?,?,?)",
                    (t, profile, "done", outcome, started, started + 10, error, summary),
                ).lastrowid
            run("coder", "crashed", now - 500, error="pid 111 exited with code 1")
            newest = run("coder", "crashed", now - 100, error="pid 222 exited with code 1")
            run("research", "crashed", now - 300, error="pid 333 exited with code 1")
            run("coder", "blocked", now - 200, error="  ",
                summary="Edit-risk blocked by open overlapping session")
            # außerhalb des Fensters → unsichtbar
            run("coder", "crashed", now - 40 * 86400, error="pid 9 exited with code 1")
        data = kb.runs_issues(conn, days=30)
    assert data["total_failed_runs"] == 4
    assert data["group_count"] == 3
    top = data["issues"][0]
    assert top["profile"] == "coder"
    assert top["signature"] == "pid N exited with code N"
    assert top["count"] == 2
    assert top["outcomes"] == {"crashed": 2}
    assert top["example_run_id"] == newest  # jüngstes Auftreten als Beispiel
    assert top["last_seen"] == now - 100
    # research-PID-Crash ist ein EIGENES Issue (Profil gehört zum Schlüssel)
    profiles = {(i["profile"], i["signature"]) for i in data["issues"]}
    assert ("research", "pid N exited with code N") in profiles
    # blocked ohne error nutzt die summary
    blocked = next(i for i in data["issues"] if i["outcomes"].get("blocked"))
    assert "Edit-risk blocked" in blocked["example_text"]


# ---------------------------------------------------------------------------
# Phase A (Programm 3): Heartbeat-Note + Dauer-Perzentile (ehrliche ETA)
# ---------------------------------------------------------------------------


def test_heartbeat_worker_persists_note_as_event_payload(kanban_home):
    """Die Activity-Note landet als heartbeat-Event-Payload am Run — das ist
    die Quelle für last_heartbeat_note in /workers/active."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="busy")
        with kb.write_txn(conn):
            run_id = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at) "
                "VALUES (?, 'coder', 'running', 1000)", (t,),
            ).lastrowid
            conn.execute(
                "UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?",
                (run_id, t),
            )
        assert kb.heartbeat_worker(conn, t, note="Bash: npm test", expected_run_id=run_id)
        row = conn.execute(
            "SELECT json_extract(payload, '$.note') AS note FROM task_events "
            "WHERE task_id = ? AND kind = 'heartbeat' AND run_id = ? "
            "ORDER BY id DESC LIMIT 1", (t, run_id),
        ).fetchone()
    assert row["note"] == "Bash: npm test"


def test_run_duration_percentiles_per_profile_with_min_n(kanban_home):
    """p50/p90 nur aus completed-Runs des Profils; unter min_n ehrlich None."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="timed")
        with kb.write_txn(conn):
            for dur in (100, 200, 300, 400, 1000):
                conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, outcome, "
                    "started_at, ended_at) VALUES (?, 'coder', 'done', 'completed', ?, ?)",
                    (t, now - 5000, now - 5000 + dur),
                )
            # failed-Run desselben Profils zählt NICHT in die ETA
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, "
                "started_at, ended_at) VALUES (?, 'coder', 'done', 'crashed', ?, ?)",
                (t, now - 5000, now - 5000 + 9999),
            )
            # dünnes Profil: nur 1 completed-Run
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, "
                "started_at, ended_at) VALUES (?, 'research', 'done', 'completed', ?, ?)",
                (t, now - 5000, now - 4900),
            )
        stats = kb.run_duration_percentiles(conn, ["coder", "research", "verifier"])
    assert stats["coder"]["p50"] == 300
    assert stats["coder"]["p90"] == 1000
    assert stats["coder"]["n"] == 5
    assert stats["research"] == {"p50": None, "p90": None, "n": 1}
    assert stats["verifier"] == {"p50": None, "p90": None, "n": 0}


def test_runs_failures_dedupes_per_task_and_filters_recovered(kanban_home):
    """Phase F: jüngster Fehl-Run pro Task; bereits fertige/laufende Tasks
    erscheinen nicht mehr in der Triage."""
    now = int(time.time())
    with kb.connect_closing() as conn:
        t_open = kb.create_task(conn, title="kaputt und wartet")
        t_done = kb.create_task(conn, title="kaputt aber erledigt")
        kb.block_task(conn, t_open, reason="worker crashed")
        with kb.write_txn(conn):
            # beide Crash-Runs enden NACH dem block_task-eigenen blocked-Run,
            # damit "jüngster Run gewinnt" über die pid-Runs läuft
            for started, ended, err in (
                (now - 7200, now + 60, "pid 1 exited with code 1"),
                (now - 3600, now + 120, "pid 2 exited with code 1"),
            ):
                conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, outcome, "
                    "started_at, ended_at, error) VALUES (?, 'coder', 'done', 'crashed', ?, ?, ?)",
                    (t_open, started, ended, err),
                )
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, "
                "started_at, ended_at, error) VALUES (?, 'coder', 'done', 'crashed', ?, ?, 'x')",
                (t_done, now - 1800, now - 1700),
            )
        kb.complete_task(conn, t_done, summary="doch geschafft")
        data = kb.runs_failures(conn, hours=48)
    assert data["count"] == 1  # t_done ist raus (status done), t_open dedupliziert
    f = data["failures"][0]
    assert f["task_id"] == t_open
    assert f["reason"] == "pid 2 exited with code 1"  # jüngster Run gewinnt
    assert f["task_status"] == "blocked"


# ---------------------------------------------------------------------------
# Spawn-resilience: transient worktree-provisioning timeouts re-queue instead
# of blocking; permanent provisioning errors still block (plan 2026-06-15-001,
# Task 4). ``all_assignees_spawnable`` makes the ``coder-claude`` profile pass
# the dispatcher's profile-exists guard so dispatch reaches the worktree
# provisioning hook.
# ---------------------------------------------------------------------------

def _count_spawn_retry_events(conn, tid, kind):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?", (tid, kind)
    ).fetchone()[0]


def test_transient_provisioning_timeout_requeues_without_burning_budget(
    kanban_home, monkeypatch, all_assignees_spawnable, tmp_path
):
    from hermes_cli import kanban_worktrees as kwt
    monkeypatch.setattr(kwt, "isolation_mode", lambda: "worktree")
    def boom(*a, **k):
        raise kwt.WorktreeTimeout("contention")
    monkeypatch.setattr(kwt, "provision_for_task", boom)
    repo = tmp_path / "repo"
    repo.mkdir()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder-claude",
                             workspace_kind="worktree", workspace_path=str(repo),
                             max_retries=1)
        kb.dispatch_once(conn, board="default")
        row = conn.execute(
            "SELECT status, consecutive_failures FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert row["status"] == "ready"            # re-queued, not blocked
        assert row["consecutive_failures"] == 0    # budget NOT consumed
        assert _count_spawn_retry_events(conn, tid, "spawn_retry") == 1


def test_spawn_retry_budget_exhaustion_blocks(
    kanban_home, monkeypatch, all_assignees_spawnable, tmp_path
):
    from hermes_cli import kanban_worktrees as kwt
    monkeypatch.setattr(kwt, "isolation_mode", lambda: "worktree")
    monkeypatch.setattr(
        kwt, "provision_for_task",
        lambda *a, **k: (_ for _ in ()).throw(kwt.WorktreeTimeout("x")),
    )
    monkeypatch.setenv("HERMES_SPAWN_RETRY_LIMIT", "2")
    repo = tmp_path / "repo"
    repo.mkdir()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder-claude",
                             workspace_kind="worktree", workspace_path=str(repo),
                             max_retries=1)
        for _ in range(3):
            kb.dispatch_once(conn, board="default")
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "blocked"          # spawn budget spent → normal block


def test_permanent_provisioning_error_blocks_immediately(
    kanban_home, monkeypatch, all_assignees_spawnable, tmp_path
):
    from hermes_cli import kanban_worktrees as kwt
    monkeypatch.setattr(kwt, "isolation_mode", lambda: "worktree")
    monkeypatch.setattr(
        kwt, "provision_for_task",
        lambda *a, **k: (_ for _ in ()).throw(kwt.WorktreeError("disk full")),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder-claude",
                             workspace_kind="worktree", workspace_path=str(repo),
                             max_retries=1)
        kb.dispatch_once(conn, board="default")
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "blocked"          # permanent error: unchanged behavior


# ---------------------------------------------------------------------------
# Dispatcher-side heartbeat for claude-CLI workers
# (heartbeat_live_claude_cli_workers)
# ---------------------------------------------------------------------------

def _make_running_worker(
    conn, *, profile, pid, claim_lock=None, last_heartbeat_at=None,
    started_at=None, title="claude-cli-live", workspace_path=None,
):
    """Set up a ``running`` task + matching ``task_runs`` row directly.

    Mirrors the raw-SQL setup used by the dashboard worker tests so we can
    pin ``profile`` / ``worker_pid`` / ``claim_lock`` / ``last_heartbeat_at``
    (and optionally ``workspace_path`` for the claude-CLI transcript probe)
    without going through the code-task contract gate in ``claim_task``.
    Returns ``(task_id, run_id)``.
    """
    now = int(time.time())
    t = kb.create_task(conn, title=title)
    lock = claim_lock if claim_lock is not None else kb._claimer_id()
    start = started_at if started_at is not None else now
    with kb.write_txn(conn):
        run_id = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, "
            "claim_lock, worker_pid) VALUES (?, ?, 'running', ?, ?, ?)",
            (t, profile, start, lock, pid),
        ).lastrowid
        conn.execute(
            "UPDATE tasks SET status = 'running', current_run_id = ?, "
            "claim_lock = ?, worker_pid = ?, started_at = ?, "
            "last_heartbeat_at = ?, "
            "workspace_kind = CASE WHEN ? IS NULL THEN workspace_kind ELSE 'worktree' END, "
            "workspace_path = COALESCE(?, workspace_path) "
            "WHERE id = ?",
            (run_id, lock, pid, start, last_heartbeat_at,
             workspace_path, workspace_path, t),
        )
    return t, run_id


def test_claude_cli_heartbeat_refreshes_and_emits_honest_note(
    kanban_home, monkeypatch,
):
    """A live claude-CLI run with no prior heartbeat gets last_heartbeat_at
    refreshed, a heartbeat event appended, and an honest note (criteria 1/2/4)."""
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)

    with kb.connect_closing() as conn:
        t, run_id = _make_running_worker(conn, profile="coder-claude", pid=4242)
        # Worker log present → note carries the honest log detail.
        log_dir = kb.worker_logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{t}.log").write_text("claude working...\n" * 100)

        beat = kb.heartbeat_live_claude_cli_workers(conn)
        assert beat == [t]

        task = kb.get_task(conn, t)
        assert task.last_heartbeat_at is not None
        run_hb = conn.execute(
            "SELECT last_heartbeat_at FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()["last_heartbeat_at"]
        assert run_hb is not None

        ev = conn.execute(
            "SELECT json_extract(payload, '$.note') AS note FROM task_events "
            "WHERE task_id = ? AND kind = 'heartbeat' ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert ev is not None
        note = ev["note"]
        assert note.startswith("claude-cli running")
        assert "log" in note  # honest log detail, no fake percentage
        assert "%" not in note


def test_claude_cli_heartbeat_skips_hermes_runtime_worker(kanban_home, monkeypatch):
    """Hermes-runtime workers self-heartbeat; the dispatcher must NOT touch
    their heartbeat or it would mask a genuine stall (criterion 5)."""
    import hermes_cli.kanban_db as _kb
    # "worker" is deliberately NOT in HERMES_CLAUDE_CLI_PROFILES.
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)

    with kb.connect_closing() as conn:
        t, _ = _make_running_worker(conn, profile="worker", pid=4243)
        beat = kb.heartbeat_live_claude_cli_workers(conn)
        assert beat == []
        assert kb.get_task(conn, t).last_heartbeat_at is None
        n_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'heartbeat'",
            (t,),
        ).fetchone()[0]
        assert n_events == 0


def test_claude_cli_heartbeat_skips_dead_pid(kanban_home, monkeypatch):
    """A dead PID is detect_crashed_workers' job — the heartbeat step leaves
    it alone so a crashed worker is not falsely kept alive."""
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        t, _ = _make_running_worker(conn, profile="coder-claude", pid=4244)
        beat = kb.heartbeat_live_claude_cli_workers(conn)
        assert beat == []
        assert kb.get_task(conn, t).last_heartbeat_at is None


def test_claude_cli_heartbeat_skips_other_host_claim(kanban_home, monkeypatch):
    """Only host-local claims are candidates — a claim owned by another host
    is checked by that host's dispatcher, not ours."""
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)

    with kb.connect_closing() as conn:
        t, _ = _make_running_worker(
            conn, profile="coder-claude", pid=4245,
            claim_lock="someotherhost:9999",
        )
        beat = kb.heartbeat_live_claude_cli_workers(conn)
        assert beat == []
        assert kb.get_task(conn, t).last_heartbeat_at is None


def test_claude_cli_heartbeat_rate_limited(kanban_home, monkeypatch):
    """A fresh heartbeat is not re-emitted (no timeline spam); a stale one is
    refreshed."""
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
    now = int(time.time())

    with kb.connect_closing() as conn:
        # Fresh heartbeat (10s ago) → skipped.
        t_fresh, _ = _make_running_worker(
            conn, profile="coder-claude", pid=4246,
            last_heartbeat_at=now - 10, title="fresh",
        )
        # Stale heartbeat (well beyond the min gap) → refreshed.
        t_stale, _ = _make_running_worker(
            conn, profile="coder-claude", pid=4247,
            last_heartbeat_at=now - (kb._CLAUDE_CLI_HEARTBEAT_MIN_GAP_SECONDS + 60),
            title="stale",
        )
        beat = kb.heartbeat_live_claude_cli_workers(conn)
        assert t_stale in beat
        assert t_fresh not in beat
        # Fresh run keeps its original beat untouched.
        assert kb.get_task(conn, t_fresh).last_heartbeat_at == now - 10
        # Stale run advanced to ~now.
        assert kb.get_task(conn, t_stale).last_heartbeat_at >= now


def test_claude_cli_heartbeat_note_failsoft_without_log(kanban_home, monkeypatch):
    """No worker log → the note degrades to the honest base, never raises."""
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
    with kb.connect_closing() as conn:
        t, _ = _make_running_worker(conn, profile="coder-claude", pid=4248)
        beat = kb.heartbeat_live_claude_cli_workers(conn)
        assert beat == [t]
        ev = conn.execute(
            "SELECT json_extract(payload, '$.note') AS note FROM task_events "
            "WHERE task_id = ? AND kind = 'heartbeat' ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert ev["note"] == "claude-cli running"


def _seed_claude_transcript(
    monkeypatch, tmp_path, workspace_path, *, body, mtime,
    filename="34ffd866-1d4b-49c8-81ea-8e7c0cca07c9.jsonl",
):
    """Plant a Claude Code session transcript for ``workspace_path`` under an
    isolated CLAUDE_CONFIG_DIR and return the file path.

    Claude Code stores each session under ``<config>/projects/<munged-cwd>/``
    where the munged name is the absolute cwd with every non-alphanumeric char
    replaced by ``-``. We reproduce that mapping so the heartbeat probe finds it.
    """
    config_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    munged = re.sub(r"[^a-zA-Z0-9]", "-", str(workspace_path))
    proj_dir = config_dir / "projects" / munged
    proj_dir.mkdir(parents=True, exist_ok=True)
    jsonl = proj_dir / filename
    jsonl.write_text(body)
    os.utime(jsonl, (mtime, mtime))
    return jsonl


def test_claude_cli_heartbeat_note_surfaces_jsonl_when_log_stale(
    kanban_home, tmp_path, monkeypatch,
):
    """AC-1: an empty/stale stdout log but a freshly-written Claude transcript
    must surface the transcript activity, not only the misleading ``log 0B``.

    Reproduces the t_c16549e9 incident: ``claude -p`` writes its real output to
    a session JSONL, leaving the per-task stdout log at 0B; the old note read
    ``claude-cli running · log 0B · last output 1080s`` and looked hung.
    """
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
    now = int(time.time())
    workspace = "/home/x/.hermes/hermes-agent/.worktrees/kanban/t_360a4052"
    secret = "ANTHROPIC_API_KEY=sk-ant-shouldnotleak"

    with kb.connect_closing() as conn:
        t, _ = _make_running_worker(
            conn, profile="coder-claude", pid=5721, workspace_path=workspace,
        )
        # Per-task stdout log present but empty + stale (the misleading signal).
        log_dir = kb.worker_logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{t}.log"
        log_path.write_text("")
        os.utime(log_path, (now - 1080, now - 1080))
        # Live Claude transcript: freshly modified, holds a secret in its body.
        _seed_claude_transcript(
            monkeypatch, tmp_path, workspace,
            body='{"type":"assistant"}\n' + secret + "\n", mtime=now - 3,
        )

        beat = kb.heartbeat_live_claude_cli_workers(conn)
        assert beat == [t]
        note = conn.execute(
            "SELECT json_extract(payload, '$.note') AS note FROM task_events "
            "WHERE task_id = ? AND kind = 'heartbeat' ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()["note"]

    # Existing wording preserved (prefix + honest stdout-log detail)…
    assert note.startswith("claude-cli running")
    # …but the live-session signal is now present so operators can tell a live
    # claude session from a genuinely hung process.
    assert "claude session" in note
    # AC-2: only stat metadata is reported — never transcript contents.
    assert secret not in note
    assert "sk-ant" not in note


def test_claude_cli_heartbeat_note_unchanged_without_transcript(
    kanban_home, tmp_path, monkeypatch,
):
    """AC-3: when no Claude transcript exists for the workspace, the note is
    byte-for-byte the pre-existing wording (no spurious session clause)."""
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    # Point CLAUDE_CONFIG_DIR at an empty dir → no transcript for this workspace.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-claude"))
    now = int(time.time())
    workspace = "/home/x/.hermes/hermes-agent/.worktrees/kanban/t_nope"

    with kb.connect_closing() as conn:
        t, _ = _make_running_worker(
            conn, profile="coder-claude", pid=5722, workspace_path=workspace,
        )
        log_dir = kb.worker_logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{t}.log").write_text("x" * 2048)
        os.utime(log_dir / f"{t}.log", (now - 5, now - 5))
        note = _kb._claude_cli_heartbeat_note(t, workspace_path=workspace)

    assert note == "claude-cli running · log 2KB · last output 5s"
    assert "claude session" not in note


def test_claude_jsonl_activity_reads_only_metadata(
    kanban_home, tmp_path, monkeypatch,
):
    """AC-2: the transcript probe returns (mtime, size) only — it never reads
    the JSONL body into the result, and ignores non-jsonl noise files."""
    import hermes_cli.kanban_db as _kb
    now = int(time.time())
    workspace = "/home/x/work/t_probe"
    jsonl = _seed_claude_transcript(
        monkeypatch, tmp_path, workspace,
        body="secret-line\n" * 50, mtime=now - 7,
    )
    # A newer non-jsonl sibling must be ignored (only *.jsonl counts).
    (jsonl.parent / "notes.txt").write_text("ignore me")
    os.utime(jsonl.parent / "notes.txt", (now, now))

    activity = _kb._claude_jsonl_activity(workspace)
    assert activity is not None
    mtime, size = activity
    assert mtime == now - 7
    assert size == jsonl.stat().st_size
    # Unknown / missing workspace → None (AC-3 fail-soft path).
    assert _kb._claude_jsonl_activity(None) is None
    assert _kb._claude_jsonl_activity("/no/such/workspace/here") is None


def test_dispatch_once_heartbeats_live_claude_cli_and_prevents_false_stale(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """End-to-end: dispatch_once refreshes a live claude-CLI heartbeat BEFORE
    the stale reclaimer runs, so a healthy long run (no self-heartbeat) is not
    false-positive reclaimed, and the run shows up in result.heartbeated."""
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
    five_hours_ago = int(time.time()) - (5 * 3600)

    with kb.connect_closing() as conn:
        t, _ = _make_running_worker(
            conn, profile="coder-claude", pid=4249,
            started_at=five_hours_ago, last_heartbeat_at=None,
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *a, **k: None,
            stale_timeout_seconds=14400,  # 4h — would reclaim a NULL-hb run
            board="default",
        )
        assert t in result.heartbeated
        assert t not in result.stale
        assert kb.get_task(conn, t).status == "running"


def test_phase4_tree_root_woke_excludes_plain_dependency_task(kanban_home):
    """Phase4 F: tree_root_woke only reports real decomposed roots, not any dependent ready task."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="Parent")
        child = kb.create_task(conn, title="Plain dependent")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        kb.link_tasks(conn, parent, child)
        result = kb.decision_queue(conn)
    assert "tree_root_woke" not in _kinds_for(child, result)


# ---------------------------------------------------------------------------
# S4 Heiler: structured failure-classification + escalation ledger
# ---------------------------------------------------------------------------

def test_s4_classify_failure_transient():
    """dirty-overlap / git-op / wrong-branch and provisioning outcomes ->
    transient. Pure function, no DB."""
    cls, ev = kb._classify_failure(error="dirty worktree overlap on branch X")
    assert cls == kb.HEILER_CLASS_TRANSIENT
    assert ev["signal_source"] == "text"
    assert ev["matched"]

    cls, ev = kb._classify_failure(error="checkout is on the wrong branch")
    assert cls == kb.HEILER_CLASS_TRANSIENT

    # Structural outcome mapping wins without any error wording.
    cls, ev = kb._classify_failure(outcome="spawn_retry")
    assert cls == kb.HEILER_CLASS_TRANSIENT
    assert ev["signal_source"] == "outcome"


def test_s4_classify_failure_real_bug_and_default():
    """Red gate / reviewer findings -> real-bug, but an opaque failure with no
    transient/spec/flaky signal defaults to unclassified."""
    cls, _ = kb._classify_failure(error="gate failed: pytest 3 tests failed")
    assert cls == kb.HEILER_CLASS_REAL_BUG

    cls, _ = kb._classify_failure(error="reviewer findings: REQUEST_CHANGES")
    assert cls == kb.HEILER_CLASS_REAL_BUG

    cls, ev = kb._classify_failure(error="something entirely opaque happened")
    assert cls == kb.HEILER_CLASS_UNCLASSIFIED
    assert ev["signal_source"] == "default"


def test_s4_classify_failure_flaky():
    cls, _ = kb._classify_failure(error="test flake: passed on retry")
    assert cls == kb.HEILER_CLASS_FLAKY


def test_s4_classify_failure_bad_spec():
    cls, _ = kb._classify_failure(error="acceptance criteria cannot be met")
    assert cls == kb.HEILER_CLASS_BAD_SPEC

    # Structural stall_class mapping: repeated decompose failure = spec gap.
    cls, ev = kb._classify_failure(stall_class="triage_decompose_failed")
    assert cls == kb.HEILER_CLASS_BAD_SPEC
    assert ev["signal_source"] == "stall_class"


def test_s4_classify_failure_conflict_wins_over_stall_class():
    """Unambiguous merge-conflict markers win even on the integration_parked
    stall path (which otherwise has no structural mapping)."""
    cls, _ = kb._classify_failure(error="CONFLICT (content): merge conflict in api.ts")
    assert cls == kb.HEILER_CLASS_CONFLICT

    cls, _ = kb._classify_failure(
        stall_class="integration_parked",
        reason="integration parked: merge conflict in web/src/App.tsx",
    )
    assert cls == kb.HEILER_CLASS_CONFLICT


# HEILER-OUTCOME-RECLASSIFY-S1 ------------------------------------------------

def test_capacity_class_registered():
    """The capacity class exists and is a valid Heiler class, but is NOT counted
    as a non-transient 'real problem' (it is pure observability/routing)."""
    assert kb.HEILER_CLASS_CAPACITY == "capacity"
    assert kb.HEILER_CLASS_CAPACITY in kb.HEILER_CLASSES
    from hermes_cli import vision_metrics as vm
    assert kb.HEILER_CLASS_CAPACITY not in vm._NON_TRANSIENT_HEILER_CLASSES


def test_unclassified_class_registered_but_not_non_transient():
    """The opaque default class is valid, but not a known defect signal."""
    assert kb.HEILER_CLASS_UNCLASSIFIED == "unclassified"
    assert kb.HEILER_CLASS_UNCLASSIFIED in kb.HEILER_CLASSES
    from hermes_cli import vision_metrics as vm
    assert kb.HEILER_CLASS_UNCLASSIFIED not in vm._NON_TRANSIENT_HEILER_CLASSES


def test_operator_intent_class_registered_but_not_non_transient():
    """A deliberate operator/hold state (supersede, green-run-yet-still-
    blocked) is not a self-healing signal but also not a product defect —
    like capacity, it is pure observability (HEILER-CLASSIFY-SIGNAL-GAP-S2)."""
    assert kb.HEILER_CLASS_OPERATOR_INTENT == "operator-intent"
    assert kb.HEILER_CLASS_OPERATOR_INTENT in kb.HEILER_CLASSES
    from hermes_cli import vision_metrics as vm
    assert kb.HEILER_CLASS_OPERATOR_INTENT not in vm._NON_TRANSIENT_HEILER_CLASSES


# ESCALATION-OPERATOR-GATE-DECLASSIFY-S1 -------------------------------------

def test_operator_gated_class_registered_but_not_non_transient():
    """A held-before-release / operator-question gate (the operator must
    release/answer) is a deliberate operator state, not a product defect — like
    capacity/operator-intent it is a terminal NON-error class and must NOT be a
    non-transient 'real problem' signal (else it would inflate the autonomy
    counter)."""
    assert kb.HEILER_CLASS_OPERATOR_GATED == "operator-gated"
    assert kb.HEILER_CLASS_OPERATOR_GATED in kb.HEILER_CLASSES
    from hermes_cli import vision_metrics as vm
    assert kb.HEILER_CLASS_OPERATOR_GATED not in vm._NON_TRANSIENT_HEILER_CLASSES


def test_classify_operator_gate_held_before_release_is_operator_gated():
    """The canonical freigabe hold reason (planspecs.py:
    'Planspec ingest: held before release') classifies as operator-gated, not
    the opaque default — it is the dominant live unclassified cluster (AC-1)."""
    cls, ev = kb._classify_failure(error="Planspec ingest: held before release")
    assert cls == kb.HEILER_CLASS_OPERATOR_GATED
    assert ev["signal_source"] == "text"
    assert ev["matched"] == "held before release"


def test_classify_operator_gate_operator_hold_and_human_input():
    """A manual operator hold (hold_task synthesizes summary='operator hold')
    and an explicit human-input/manual-completion park are operator gates, not
    defaults."""
    for reason in (
        "operator hold",
        "Operator manual completion in progress; do not redispatch",
        "need human input on the credential rotation",
        "awaiting operator decision before proceeding",
    ):
        cls, _ = kb._classify_failure(error=reason)
        assert cls == kb.HEILER_CLASS_OPERATOR_GATED, reason


def test_classify_operator_gate_does_not_mask_real_defect():
    """AC-2 guardrail: the operator-gate signals sit BELOW every real-defect
    signal, so an escalation that mentions an operator gate but also carries a
    genuine defect signal stays in its real class — no masking of real defects
    as operator-gated."""
    # red gate wins over an operator-hold mention
    cls, _ = kb._classify_failure(
        error="operator hold pending; but gate failed: 3 tests failed"
    )
    assert cls == kb.HEILER_CLASS_REAL_BUG
    # bad-spec wins over a held-before-release mention
    cls, _ = kb._classify_failure(
        error="held before release; acceptance criteria cannot be met"
    )
    assert cls == kb.HEILER_CLASS_BAD_SPEC
    # a plain opaque failure is still unclassified (no over-firing)
    cls, _ = kb._classify_failure(error="something entirely opaque happened")
    assert cls == kb.HEILER_CLASS_UNCLASSIFIED


def test_classify_escalation_held_before_release_silent_block_is_operator_gated():
    """End-to-end via the silent-block escalation payload shape: a settled
    freigabe hold (last_error='Planspec ingest: held before release',
    trigger_outcome='scheduled', blocked_kind='operator_question') classifies as
    operator-gated instead of unclassified."""
    cls, _ = kb._classify_escalation_payload({
        "why_now": "settled block (last run outcome: scheduled) with no "
                   "operator_escalation — the self-healing retry lane will not "
                   "(further) act on it",
        "evidence": {
            "trigger_outcome": "scheduled",
            "last_error": "Planspec ingest: held before release",
            "blocked_kind": "operator_question",
        },
    })
    assert cls == kb.HEILER_CLASS_OPERATOR_GATED


def test_classify_escalation_operator_question_with_real_defect_stays_defect():
    """AC-2: an operator_question-kind escalation whose real reason is a genuine
    defect (a placeholder/null-body spec gap that also trips the question regex)
    stays bad-spec — the block kind does NOT override the defect signal."""
    cls, _ = kb._classify_escalation_payload({
        "why_now": "settled block (last run outcome: blocked) with no "
                   "operator_escalation",
        "evidence": {
            "trigger_outcome": "blocked",
            "last_error": "Task body is a placeholder: it contains only the "
                          "generic Hermes Coder Contract template",
            "blocked_kind": "operator_question",
        },
    })
    assert cls == kb.HEILER_CLASS_BAD_SPEC


def test_classify_nonspawnable_assignee_is_bad_spec():
    """A ready-stage mis-assignment (outcome='nonspawnable_assignee') is a
    structural config/spec gap, not the opaque default (live: t_23415f60,
    assignee 'ui-verifier')."""
    cls, ev = kb._classify_escalation_payload({
        "why_now": "assignee 'ui-verifier' is neither a spawnable Hermes "
                   "profile nor a known terminal lane — the task can never "
                   "auto-dispatch and would rot in ready without this "
                   "escalation",
        "evidence": {"trigger_outcome": "nonspawnable_assignee",
                     "assignee": "ui-verifier"},
    })
    assert cls == kb.HEILER_CLASS_BAD_SPEC
    assert ev["signal_source"] == "outcome"


def test_classify_input_token_runaway_is_capacity():
    """A per-task input-token runaway park reuses the existing capacity class
    (HEILER-CLASSIFY-SIGNAL-GAP-S2: no new class per anti-scope), not the
    opaque default (live budget-runaway escalation why_now shape)."""
    cls, _ = kb._classify_escalation_payload({
        "why_now": "per-task input-token runaway: 2718064 cumulative input "
                   "tokens across 4 run(s) exceeded the cap of 2000000",
        "evidence": {},
    })
    assert cls == kb.HEILER_CLASS_CAPACITY


def test_s4_classify_crashed_worker_is_transient():
    """A bare crashed-worker outcome (dead pid, no content defect) reclassifies
    from the real-bug default to transient, so it flows into the bounded
    transient-retry budget and self-heals (HEILER-OUTCOME-RECLASSIFY-S1 AC-1).
    Reclassification is a fallback: it only fires when no real-bug/flaky/bad-spec
    signal is present in the error text."""
    for err in (
        "pid 12345 exited with code 1",
        "pid 999 not alive",
        "pid 7 killed by signal 9",
    ):
        cls, ev = kb._classify_failure(outcome="crashed", error=err)
        assert cls == kb.HEILER_CLASS_TRANSIENT, err
        assert ev["signal_source"] == "outcome_fallback"
        assert ev["matched"] == "crashed"


def test_s4_classify_crashed_with_real_defect_text_stays_triagierbar():
    """AC-2: a crash whose error text reveals a genuine defect (red gate /
    reviewer findings) is NOT masked as transient — the real-bug text signal
    wins over the crashed->transient fallback, so it stays triagierbar."""
    cls, _ = kb._classify_failure(
        outcome="crashed", error="gate failed: pytest reported 2 tests failed",
    )
    assert cls == kb.HEILER_CLASS_REAL_BUG

    cls, _ = kb._classify_failure(
        outcome="crashed", error="reviewer findings: REQUEST_CHANGES",
    )
    assert cls == kb.HEILER_CLASS_REAL_BUG


def test_s4_classify_iteration_budget_exhausted_is_capacity():
    """iteration_budget_exhausted reclassifies from the real-bug default into a
    distinct capacity class (AC-1), whether the signal arrives as a stall_class
    or as a run outcome (robust to the carrier)."""
    cls, ev = kb._classify_failure(stall_class="iteration_budget_exhausted")
    assert cls == kb.HEILER_CLASS_CAPACITY
    assert ev["signal_source"] == "stall_fallback"
    assert ev["matched"] == "iteration_budget_exhausted"

    cls, ev = kb._classify_failure(outcome="iteration_budget_exhausted")
    assert cls == kb.HEILER_CLASS_CAPACITY
    assert ev["signal_source"] == "outcome_fallback"


def test_s4_classify_iteration_budget_real_defect_stays_triagierbar():
    """AC-2: a task that exhausts its iteration budget BECAUSE of a real defect
    (a red gate / reviewer finding surfaced in the text) stays a real-bug — the
    capacity reclassification is a fallback that text signals override, so the
    genuinely broken task remains triagierbar instead of hidden as capacity."""
    cls, _ = kb._classify_failure(
        outcome="iteration_budget_exhausted",
        error="gate failed: assertion failed in test_loop",
    )
    assert cls == kb.HEILER_CLASS_REAL_BUG


def test_s4_strong_outcome_mapping_still_wins_over_text():
    """Regression guard: the pre-existing STRONG outcome mappings (spawn_retry /
    spawn_failed / rate_limited) still win over error text — only the new
    crashed/iteration_budget fallbacks sit below the text signals."""
    cls, ev = kb._classify_failure(
        outcome="spawn_retry", error="gate failed: 3 tests failed",
    )
    assert cls == kb.HEILER_CLASS_TRANSIENT
    assert ev["signal_source"] == "outcome"


def test_release_gate_red_outcome_is_real_bug():
    """ESCALATION-RELEASE-GATE-ERROR-CONTEXT-S1: a persistent-red release gate,
    carried as the structural ``release_gate_red`` trigger_outcome, classifies
    real-bug even when the gate output text (opaque / visual-gate / empty) matches
    no free-text signal — closing the ``unclassified`` gap that starved by_class."""
    for err in ("", "still broken", "visual-gate: scrollWidth exceeds viewport",
                "visual-gate: dashboard unreachable: Connection refused",
                "error TS2304: Cannot find name Foo"):
        cls, ev = kb._classify_failure(outcome="release_gate_red", error=err)
        assert cls == kb.HEILER_CLASS_REAL_BUG, err
        assert ev["signal_source"] == "outcome_fallback"
        assert ev["matched"] == "release_gate_red"


def test_release_gate_infra_outcome_is_transient():
    """ESCALATION-RELEASE-GATE-ERROR-CONTEXT-S1: a gate the runner could not
    complete (timeout / launch error, carried as ``release_gate_infra``) is
    operational, not a candidate defect → transient, not real-bug."""
    for err in ("release-gate timed out after 1800s",
                "release-gate command error: [Errno 2] No such file"):
        cls, ev = kb._classify_failure(outcome="release_gate_infra", error=err)
        assert cls == kb.HEILER_CLASS_TRANSIENT, err
        assert ev["signal_source"] == "outcome_fallback"


def test_release_gate_outcome_fallback_yields_to_real_text_signal():
    """AC-2 over-mapping guard: the release_gate_* outcome mappings are WEAK
    fallbacks, so a genuine free-text signal in the gate output still classifies
    first — a red gate whose log carries a merge conflict / flaky / reviewer
    finding is NOT force-labelled real-bug by the structural default."""
    cls, ev = kb._classify_failure(
        outcome="release_gate_red",
        error="CONFLICT (content): merge conflict in web/src/App.tsx",
    )
    assert cls == kb.HEILER_CLASS_CONFLICT
    assert ev["signal_source"] == "text"

    cls, _ = kb._classify_failure(
        outcome="release_gate_red", error="flaky: passed on retry",
    )
    assert cls == kb.HEILER_CLASS_FLAKY


def test_s4_crashed_reclassify_stays_bounded(kanban_home):
    """AC-2: crashed->transient is a relabel only — repeated crashes of the same
    task still trip the consecutive-failure breaker and escalate (the bounded
    retry limit is untouched), so there is no unbounded retry storm."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="flapping worker", assignee="coder")
        # First crash: below the DEFAULT_FAILURE_LIMIT=2 breaker -> requeued.
        assert kb.claim_task(conn, tid) is not None
        blocked1 = kb._record_task_failure(
            conn, tid, "pid 111 not alive",
            outcome="crashed", release_claim=True, end_run=True,
        )
        assert blocked1 is False
        # Second crash at the same root: breaker trips -> blocked + escalated.
        assert kb.claim_task(conn, tid) is not None
        blocked2 = kb._record_task_failure(
            conn, tid, "pid 222 not alive",
            outcome="crashed", release_claim=True, end_run=True,
        )
        assert blocked2 is True

        events = kb.list_events(conn, tid)
        heilers = [e for e in events if e.kind == kb.HEILER_CLASSIFICATION_EVENT]
        escalations = [
            e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]
        task = kb.get_task(conn, tid)

    # Every crash classified transient (not real-bug), yet the breaker still
    # blocked the task and raised exactly one operator escalation.
    assert heilers, "expected heiler_classification events"
    assert all(e.payload["class"] == kb.HEILER_CLASS_TRANSIENT for e in heilers)
    assert task.status == "blocked"
    assert len(escalations) == 1


def test_s4_classify_failure_structural_resource_outcomes_do_not_default_real_bug():
    """REASON-FIDELITY-S1: terminal run outcomes that are operational/resource
    limits — not product defects — map structurally, so a settled block carrying
    one as its real trigger_outcome stops defaulting to real-bug."""
    cls, ev = kb._classify_failure(outcome="iteration_budget_exhausted")
    assert cls == kb.HEILER_CLASS_CAPACITY
    assert ev["signal_source"] == "outcome_fallback"

    for outcome in ("timed_out", "reclaimed"):
        cls, ev = kb._classify_failure(outcome=outcome)
        assert cls == kb.HEILER_CLASS_TRANSIENT, outcome
        assert ev["signal_source"] == "outcome"


def test_s4_classify_failure_budget_text_capacity_and_protocol_text_transient():
    """Free-text budget exhaustion -> capacity, while worker-protocol signals
    remain transient harness faults. This covers gave_up budget paths that keep
    their 'gave_up' outcome but carry the budget message."""
    cls, _ = kb._classify_failure(
        error="iteration budget exhausted; continuation limit exhausted (60/60)")
    assert cls == kb.HEILER_CLASS_CAPACITY
    cls, _ = kb._classify_failure(
        error="worker exited cleanly (rc=0) without calling kanban_complete "
              "or kanban_block — protocol violation")
    assert cls == kb.HEILER_CLASS_TRANSIENT


def test_s4_classify_failure_missing_spec_bad_spec():
    """A park reason describing a spec gap -> bad-spec, not the real-bug default
    (the true class of the live silent-block real-bug cluster)."""
    cls, _ = kb._classify_failure(
        error="No actionable implementation spec (3rd run, auto-retry 2/2 "
              "exhausted): title is too vague")
    assert cls == kb.HEILER_CLASS_BAD_SPEC
    cls, _ = kb._classify_failure(
        error="Missing task spec: the card body does not describe what to change")
    assert cls == kb.HEILER_CLASS_BAD_SPEC


# HEILER-CLASSIFY-SIGNAL-GAP-S1 ----------------------------------------------
# Close the classify-coverage hole where settled-block / circuit-breaker
# escalations fell through to unclassified. The genuine signal is the block
# REASON (REASON-FIDELITY design), not the universal "settled block" /
# "retry ladder exhausted" wrappers: a spec-gap reason -> bad-spec, a reviewer
# NEEDS_REVISION verdict -> real-bug. The wrappers themselves are deliberately
# NOT mapped — a wrapper signal would reclassify every bare gave_up (incl.
# genuinely-opaque ones that must stay unclassified), the over-mapping AC-2
# forbids.

def test_s4_classify_reviewer_needs_revision_is_real_bug():
    """A settled block whose reason is a reviewer NEEDS_REVISION verdict is a
    reviewer finding -> real-bug (parallel to request_changes), not the opaque
    default."""
    cls, _ = kb._classify_failure(
        error="Urteil: NEEDS_REVISION\nWarum: die Belege sind widerspruechlich")
    assert cls == kb.HEILER_CLASS_REAL_BUG
    cls, _ = kb._classify_failure(error="reviewer says this needs revision")
    assert cls == kb.HEILER_CLASS_REAL_BUG


def test_s4_classify_no_actionable_spec_beats_broad_transient():
    """A spec-gap reason that incidentally mentions a branch/git must classify
    bad-spec, NOT transient — bad-spec sits ahead of the deliberately-last broad
    git/branch transient catch-alls (the documented precedence intent)."""
    cls, _ = kb._classify_failure(
        error="No actionable review scope (premium/opus, auto-retries exhausted "
              "2/2): title is placeholder 'review'; branch kanban/t_x is empty")
    assert cls == kb.HEILER_CLASS_BAD_SPEC


def test_s4_classify_placeholder_body_is_bad_spec():
    """A settled block whose reason says the task body itself is a placeholder /
    null / empty is a spec gap -> bad-spec."""
    for err in (
        "Task body is a placeholder: it contains only the generic Hermes Coder "
        "Contract v1 template",
        "Unblockable placeholder: body contains only boilerplate",
        "BLOCKED: task body is null — title alone is not an actionable contract",
        "Blocked: current task body is empty/null",
    ):
        cls, _ = kb._classify_failure(error=err)
        assert cls == kb.HEILER_CLASS_BAD_SPEC, err


def test_s4_no_actionable_without_spec_context_stays_unclassified():
    """AC-2 over-mapping guard: bare 'no actionable' is too broad. Only
    concrete scope/body/spec-gap phrases classify bad-spec; opaque missing-proof
    wording remains unclassified until a better signal exists."""
    cls, _ = kb._classify_failure(
        error="settled block: no actionable evidence was provided by worker"
    )
    assert cls == kb.HEILER_CLASS_UNCLASSIFIED


def test_s4_request_changes_mentioning_placeholders_stays_real_bug():
    """AC-2 over-mapping guard: a genuine reviewer REQUEST_CHANGES that merely
    MENTIONS 'placeholders' (e.g. unchecked receipt placeholders) must stay
    real-bug — the placeholder bad-spec signals are precise enough ('body is a
    placeholder') to not hijack a real defect into bad-spec."""
    cls, _ = kb._classify_failure(
        error="REQUEST_CHANGES — AC-3 UNMET: the receipts are still unchecked "
              "OPERATOR-FILL placeholders (`receipt: ____`)")
    assert cls == kb.HEILER_CLASS_REAL_BUG


def test_s4_settled_block_classifies_by_reason_not_wrapper():
    """The 'settled block (last run outcome: …)' why_now is a universal wrapper:
    the class comes from the block REASON in last_error, not the wrapper. A
    spec-gap reason -> bad-spec; a bare wrapper with an opaque reason and a
    trigger_outcome carrying no signal stays honestly unclassified (NOT
    over-mapped, AC-2)."""
    spec = kb._classify_escalation_payload({
        "why_now": "settled block (last run outcome: blocked) with no "
                   "operator_escalation",
        "evidence": {
            "trigger_outcome": "blocked",
            "last_error": "Task body is a placeholder: only boilerplate, no "
                          "actionable specification",
        },
    })
    assert spec[0] == kb.HEILER_CLASS_BAD_SPEC
    opaque = kb._classify_escalation_payload({
        "why_now": "settled block (last run outcome: blocked) with no "
                   "operator_escalation",
        "evidence": {"trigger_outcome": "blocked", "last_error": ""},
    })
    assert opaque[0] == kb.HEILER_CLASS_UNCLASSIFIED


def test_s4_record_task_failure_writes_heiler_classification(kanban_home):
    """A simulated transient block and a red-gate block each write a
    heiler_classification ledger event with the right class + evidence."""
    with kb.connect_closing() as conn:
        transient = kb.create_task(conn, title="transient block", assignee="coder")
        assert kb.claim_task(conn, transient) is not None
        kb._record_task_failure(
            conn, transient,
            "dirty-overlap: worktree had uncommitted foreign work",
            outcome="crashed", failure_limit=5,
            release_claim=True, end_run=True,
        )
        real = kb.create_task(conn, title="red gate", assignee="coder")
        assert kb.claim_task(conn, real) is not None
        kb._record_task_failure(
            conn, real,
            "gate failed: pytest reported 2 tests failed",
            outcome="crashed", failure_limit=5,
            release_claim=True, end_run=True,
        )
        t_events = [
            e for e in kb.list_events(conn, transient)
            if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]
        r_events = [
            e for e in kb.list_events(conn, real)
            if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]

    assert len(t_events) == 1
    assert t_events[0].payload["class"] == kb.HEILER_CLASS_TRANSIENT
    assert t_events[0].payload["source"] == "record_task_failure"
    assert t_events[0].payload["evidence"]["matched"]

    assert len(r_events) == 1
    assert r_events[0].payload["class"] == kb.HEILER_CLASS_REAL_BUG


def test_s4_stall_park_writes_heiler_classification(kanban_home):
    """no_silent_stall_sweep parking a decompose-failed task writes a
    bad-spec heiler_classification event alongside the operator_escalation."""
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="undecomposable", assignee="coder", triage=True,
        )
        for _ in range(3):
            kb.record_decompose_failure(conn, tid)
        kb.no_silent_stall_sweep(conn, now=now)
        events = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]

    assert len(events) == 1
    assert events[0].payload["class"] == kb.HEILER_CLASS_BAD_SPEC
    assert events[0].payload["source"] == "stall_park"
    assert events[0].payload["evidence"]["stall_class"] == "triage_decompose_failed"


def test_s4_read_escalation_ledger_returns_entries_and_rollup(kanban_home):
    """read_escalation_ledger returns the classified entries (newest first),
    a per-class rollup, and honours class/task/limit filters. This is the
    Stratege's (Phase 1.5) input."""
    with kb.connect_closing() as conn:
        transient = kb.create_task(conn, title="transient", assignee="coder")
        kb.claim_task(conn, transient)
        kb._record_task_failure(
            conn, transient, "dirty-overlap git lock contention",
            outcome="crashed", failure_limit=5,
            release_claim=True, end_run=True,
        )
        real = kb.create_task(conn, title="red", assignee="coder")
        kb.claim_task(conn, real)
        kb._record_task_failure(
            conn, real, "gate failed: tests failed",
            outcome="crashed", failure_limit=5,
            release_claim=True, end_run=True,
        )

        ledger = kb.read_escalation_ledger(conn)
        by_task = kb.read_escalation_ledger(conn, task_id=transient)
        only_real = kb.read_escalation_ledger(conn, classes=[kb.HEILER_CLASS_REAL_BUG])
        limited = kb.read_escalation_ledger(conn, limit=1)

    assert ledger["total"] == 2
    assert ledger["by_class"] == {
        kb.HEILER_CLASS_TRANSIENT: 1,
        kb.HEILER_CLASS_REAL_BUG: 1,
    }
    classes_in_order = [e["class"] for e in ledger["entries"]]
    # newest-first ordering
    assert classes_in_order[0] == kb.HEILER_CLASS_REAL_BUG
    assert all("task_title" in e for e in ledger["entries"])

    assert by_task["total"] == 1
    assert by_task["entries"][0]["task_id"] == transient
    assert by_task["entries"][0]["class"] == kb.HEILER_CLASS_TRANSIENT

    assert only_real["total"] == 1
    assert only_real["by_class"] == {kb.HEILER_CLASS_REAL_BUG: 1}

    # limit caps returned entries but the rollup stays over the full window
    assert len(limited["entries"]) == 1
    assert limited["total"] == 2
    assert limited["by_class"] == {
        kb.HEILER_CLASS_TRANSIENT: 1,
        kb.HEILER_CLASS_REAL_BUG: 1,
    }


def test_s4_ledger_by_class_counts_distinct_roots_not_raw_events(kanban_home):
    """LEDGER-BYCLASS-DISTINCT-ROOTS-S1: the read/aggregation path must expose,
    next to the raw event count, a per-class count of *distinct chain roots* so
    one root that escalates repeatedly cannot over-inflate its class. Defense in
    depth complementary to the write-path idempotence: even if some other writer
    duplicates events, the Stratege's input signal stays honest. The raw event
    count is preserved alongside (both values exposed) so recurrence stays
    visible and the class ranking remains explainable."""
    with kb.connect_closing() as conn:
        # Chain A: leaf_a -> mid_a -> root_a. The K2/F1 convention links a leaf
        # (parent) to the orchestration sink/root (child), so root_a is the sink
        # reached by walking child edges downward.
        root_a = kb.create_task(conn, title="root A", assignee="coder")
        mid_a = kb.create_task(conn, title="mid A", assignee="coder")
        leaf_a = kb.create_task(conn, title="leaf A", assignee="coder")
        kb.link_tasks(conn, mid_a, root_a)
        kb.link_tasks(conn, leaf_a, mid_a)
        # The same chain A escalates transient FOUR times across its tasks.
        for _ in range(3):
            kb.add_event(conn, leaf_a, kb.HEILER_CLASSIFICATION_EVENT,
                         {"class": kb.HEILER_CLASS_TRANSIENT})
        kb.add_event(conn, mid_a, kb.HEILER_CLASSIFICATION_EVENT,
                     {"class": kb.HEILER_CLASS_TRANSIENT})

        # Chain B: a second, distinct root that also hits transient once.
        root_b = kb.create_task(conn, title="root B", assignee="coder")
        leaf_b = kb.create_task(conn, title="leaf B", assignee="coder")
        kb.link_tasks(conn, leaf_b, root_b)
        kb.add_event(conn, leaf_b, kb.HEILER_CLASSIFICATION_EVENT,
                     {"class": kb.HEILER_CLASS_TRANSIENT})

        # A standalone (un-linked) task escalates real-bug once → its own root.
        solo = kb.create_task(conn, title="solo", assignee="coder")
        kb.add_event(conn, solo, kb.HEILER_CLASSIFICATION_EVENT,
                     {"class": kb.HEILER_CLASS_REAL_BUG})

        ledger = kb.read_escalation_ledger(conn)
        only_transient = kb.read_escalation_ledger(
            conn, classes=[kb.HEILER_CLASS_TRANSIENT]
        )

    # Raw event count is preserved (guardrail: recurrence stays visible).
    assert ledger["by_class"][kb.HEILER_CLASS_TRANSIENT] == 5
    assert ledger["by_class"][kb.HEILER_CLASS_REAL_BUG] == 1
    assert ledger["total"] == 6

    # Distinct roots: only TWO roots escalated transient (chain A + chain B);
    # the four chain-A events collapse onto root_a. real-bug has one root (solo).
    assert ledger["roots_by_class"][kb.HEILER_CLASS_TRANSIENT] == 2
    assert ledger["roots_by_class"][kb.HEILER_CLASS_REAL_BUG] == 1
    # root_total = distinct roots across all classes (root_a, root_b, solo).
    assert ledger["root_total"] == 3

    # Class filter applies to the distinct-root rollup too.
    assert only_transient["roots_by_class"] == {kb.HEILER_CLASS_TRANSIENT: 2}
    assert only_transient["by_class"] == {kb.HEILER_CLASS_TRANSIENT: 5}
    assert only_transient["root_total"] == 2


# ---------------------------------------------------------------------------
# REALBUG-DETOX: default-sourced real-bug rows are reclassified to unclassified
# at read time so by_class/roots_by_class reflect the true defect signal.
# ---------------------------------------------------------------------------

def test_realbug_detox_default_sourced_rows_reclassified_read_time(kanban_home):
    """REALBUG-DETOX: read_escalation_ledger must re-map real-bug events whose
    evidence.signal_source == 'default' to 'unclassified' at read time, so the
    by_class rollup reflects the true defect signal and not the default-bucket
    residue written by the pre-b2e387669 else-branch.

    Three events:
      (a) real-bug, signal_source='text'  -> stays real-bug
      (b) real-bug, signal_source='default' -> reclassified to unclassified
      (c) transient (no evidence)         -> stays transient

    After the rollup: by_class[real-bug]==1, by_class[unclassified]==1,
    by_class[transient]==1, and event (b)'s task root must NOT appear in
    roots_by_class[real-bug].
    """
    with kb.connect_closing() as conn:
        # (a) legitimate real-bug: signal came from a text match, not the default
        task_a = kb.create_task(conn, title="real gate fail", assignee="coder")
        kb.add_event(conn, task_a, kb.HEILER_CLASSIFICATION_EVENT, {
            "class": kb.HEILER_CLASS_REAL_BUG,
            "evidence": {"signal_source": "text", "matched": "request_changes"},
        })

        # (b) default-bucket residue: written by the pre-fix else-branch
        task_b = kb.create_task(conn, title="default bucket", assignee="coder")
        kb.add_event(conn, task_b, kb.HEILER_CLASSIFICATION_EVENT, {
            "class": kb.HEILER_CLASS_REAL_BUG,
            "evidence": {"signal_source": "default", "matched": "default"},
        })

        # (c) genuine transient: unrelated class, no evidence key
        task_c = kb.create_task(conn, title="transient lock", assignee="coder")
        kb.add_event(conn, task_c, kb.HEILER_CLASSIFICATION_EVENT, {
            "class": kb.HEILER_CLASS_TRANSIENT,
        })

        # (d) default-bucket residue WITH a stamped fingerprint: operational error
        # text (e.g. "iteration budget exhausted") got an excerpt -> a fingerprint,
        # but no real-bug signal. Regression-lock: a fingerprint must NOT exempt a
        # default-sourced row from detox (live evidence: these are operational noise,
        # not code defects).
        task_d = kb.create_task(conn, title="default w/ fingerprint", assignee="coder")
        kb.add_event(conn, task_d, kb.HEILER_CLASSIFICATION_EVENT,
                     kb._heiler_classification_payload(
                         heiler_class=kb.HEILER_CLASS_REAL_BUG,
                         evidence={"signal_source": "default", "matched": "default",
                                   "excerpt": "Iteration budget exhausted (90/90)"},
                         source="test", blocked=True,
                     ))

        ledger = kb.read_escalation_ledger(conn)

    # Raw by_class counts after detox
    assert ledger["by_class"].get(kb.HEILER_CLASS_REAL_BUG, 0) == 1, (
        "only the text-sourced event should count as real-bug"
    )
    assert ledger["by_class"].get(kb.HEILER_CLASS_UNCLASSIFIED, 0) == 2, (
        "both default-sourced rows (b: no fingerprint, d: fingerprinted) must "
        "reclassify to unclassified — a fingerprint does not exempt detox"
    )
    assert ledger["by_class"].get(kb.HEILER_CLASS_TRANSIENT, 0) == 1

    # roots_by_class returns counts of distinct roots per class.
    # task_b (default-sourced) must be counted under unclassified, not real-bug.
    # Each task is unlinked so it is its own chain root -> 1 distinct root each.
    assert ledger["roots_by_class"].get(kb.HEILER_CLASS_REAL_BUG, 0) == 1, (
        "only task_a's root should count under real-bug"
    )
    assert ledger["roots_by_class"].get(kb.HEILER_CLASS_UNCLASSIFIED, 0) == 2, (
        "task_b + task_d roots counted under unclassified, not real-bug"
    )
    assert ledger["roots_by_class"].get(kb.HEILER_CLASS_TRANSIENT, 0) == 1


# ---------------------------------------------------------------------------
# REALBUG-RECURRENCE-CLUSTER-S1: the existing _error_fingerprint is stamped into
# the heiler_classification payload, and read_escalation_ledger groups recurring
# real-bug escalations by that signature (observability rollup, no gate). The
# raw by_class / roots_by_class rollups stay byte-for-byte unchanged.
# ---------------------------------------------------------------------------

def _emit_real_bug(conn, task_id, excerpt):
    """Append a real-bug heiler_classification whose payload is built by the
    production helper (so the fingerprint stamping is exercised, not faked)."""
    payload = kb._heiler_classification_payload(
        heiler_class=kb.HEILER_CLASS_REAL_BUG,
        evidence={"matched": "tests failed", "signal_source": "text",
                  "excerpt": excerpt},
        source="test", blocked=True,
    )
    kb.add_event(conn, task_id, kb.HEILER_CLASSIFICATION_EVENT, payload)


def test_heiler_classification_payload_stamps_error_fingerprint():
    """The heiler_classification payload carries a normalized error fingerprint
    derived from evidence.excerpt via the existing _error_fingerprint. Two
    excerpts that differ only in host-specific noise (pid / timestamp) collapse
    onto one fingerprint; genuinely distinct excerpts differ; an excerpt-less
    evidence carries no fingerprint. class/evidence are left untouched."""
    ev_a = {"matched": "default", "signal_source": "default",
            "excerpt": "pid 4242 AssertionError: total mismatch at 1718000000000"}
    ev_b = {"matched": "default", "signal_source": "default",
            "excerpt": "pid 9999 AssertionError: total mismatch at 1719999999999"}
    ev_c = {"matched": "default", "signal_source": "default",
            "excerpt": "TypeError: NoneType has no attribute foo"}

    p_a = kb._heiler_classification_payload(
        heiler_class=kb.HEILER_CLASS_REAL_BUG, evidence=ev_a,
        source="test", blocked=True)
    p_b = kb._heiler_classification_payload(
        heiler_class=kb.HEILER_CLASS_REAL_BUG, evidence=ev_b,
        source="test", blocked=True)
    p_c = kb._heiler_classification_payload(
        heiler_class=kb.HEILER_CLASS_REAL_BUG, evidence=ev_c,
        source="test", blocked=True)

    # Same root cause modulo pid/timestamp → one fingerprint.
    assert p_a["fingerprint"] == p_b["fingerprint"]
    assert p_a["fingerprint"] == kb._error_fingerprint(ev_a["excerpt"])
    # Distinct root cause → distinct fingerprint.
    assert p_a["fingerprint"] != p_c["fingerprint"]
    # Additive only: the signal the Stratege already reads is unchanged.
    assert p_a["class"] == kb.HEILER_CLASS_REAL_BUG
    assert p_a["evidence"] is ev_a

    # No excerpt → no fingerprint key (nothing to fingerprint).
    p_none = kb._heiler_classification_payload(
        heiler_class=kb.HEILER_CLASS_REAL_BUG,
        evidence={"matched": "default", "signal_source": "default"},
        source="test", blocked=True)
    assert "fingerprint" not in p_none


def test_s4_ledger_clusters_recurring_real_bugs_by_fingerprint(kanban_home):
    """AC-1: read_escalation_ledger groups real-bug classifications by error
    signature (the stamped _error_fingerprint over evidence.excerpt). Two
    escalations with the same normalized error text form ONE cluster with
    count=2; a distinct error text stays its own cluster. The cluster rollup is
    scoped to real-bug and is additive: by_class / roots_by_class are unchanged
    (AC-2 guardrail)."""
    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="bug one a", assignee="coder")
        t2 = kb.create_task(conn, title="bug one b", assignee="coder")
        t3 = kb.create_task(conn, title="bug two", assignee="coder")
        tt = kb.create_task(conn, title="transient noise", assignee="coder")
        # Two distinct roots hit the SAME normalized error (pid/ts differ).
        _emit_real_bug(
            conn, t1, "pid 11 AssertionError: balance != expected at 1700000000001")
        _emit_real_bug(
            conn, t2, "pid 22 AssertionError: balance != expected at 1700000000002")
        # A third task hits a genuinely different error.
        _emit_real_bug(conn, t3, "TypeError: cannot read property 'id' of undefined")
        # A transient classification WITH an excerpt must not enter the rollup.
        kb.add_event(conn, tt, kb.HEILER_CLASSIFICATION_EVENT, {
            "class": kb.HEILER_CLASS_TRANSIENT,
            "evidence": {"excerpt": "pid 5 dirty-overlap git lock contention"},
        })
        ledger = kb.read_escalation_ledger(conn)

    clusters = ledger["real_bug_signatures"]
    # Two real-bug signatures: the recurring one (count=2) and the distinct one.
    assert len(clusters) == 2
    # Most-recurrent first.
    assert clusters[0]["count"] == 2
    assert set(clusters[0]["example_roots"]) == {t1, t2}
    distinct = [c for c in clusters if c["count"] == 1]
    assert len(distinct) == 1
    assert distinct[0]["example_roots"] == [t3]
    # Cluster rollup is real-bug-only: the transient excerpt's signature is absent.
    sigs = {c["signature"] for c in clusters}
    assert kb._error_fingerprint("pid 5 dirty-overlap git lock contention") not in sigs

    # Guardrail (AC-2): the existing rollups are unchanged by the addition.
    assert ledger["by_class"] == {
        kb.HEILER_CLASS_REAL_BUG: 3, kb.HEILER_CLASS_TRANSIENT: 1}
    assert ledger["roots_by_class"] == {
        kb.HEILER_CLASS_REAL_BUG: 3, kb.HEILER_CLASS_TRANSIENT: 1}
    assert ledger["total"] == 4


def test_s4_ledger_real_bug_clusters_no_false_collision(kanban_home):
    """AC-2 cluster purity: a fixture of genuinely distinct error texts must NOT
    be collapsed. Each distinct normalized signature stays its own cluster (zero
    fingerprint collisions across the fixture), so distinct root causes are never
    merged into one recurrence count."""
    distinct_errors = [
        "AssertionError: expected 200 got 500 in test_login",
        "TypeError: cannot read property 'id' of undefined in cart",
        "KeyError: 'profile' while building the dashboard payload",
        "ValueError: invalid literal for int() with base 10: 'abc'",
        "sqlite3.IntegrityError: UNIQUE constraint failed tasks.id",
        "ModuleNotFoundError: No module named 'hermes_cli.flow'",
        "tsc error TS2345: argument of type string is not assignable",
        "lint error: 'x' is assigned a value but never used",
        "RecursionError: maximum recursion depth exceeded in resolve",
        "ZeroDivisionError: division by zero in cost-per-token rollup",
    ]
    # Sanity: the fixture itself has no two entries sharing a fingerprint.
    assert len({kb._error_fingerprint(e) for e in distinct_errors}) == len(distinct_errors)

    with kb.connect_closing() as conn:
        for i, err in enumerate(distinct_errors):
            tid = kb.create_task(conn, title=f"bug {i}", assignee="coder")
            _emit_real_bug(conn, tid, err)
        ledger = kb.read_escalation_ledger(conn)

    clusters = ledger["real_bug_signatures"]
    # No false merges: one cluster per distinct error, each count=1.
    assert len(clusters) == len(distinct_errors)
    assert all(c["count"] == 1 for c in clusters)
    assert ledger["by_class"] == {kb.HEILER_CLASS_REAL_BUG: len(distinct_errors)}


def test_s4_ledger_clusters_recompute_fingerprint_for_unstamped_events(kanban_home):
    """The signature rollup also covers legacy real-bug events written before the
    fingerprint was stamped: the reader recomputes the signature from
    evidence.excerpt, so two unstamped events with the same normalized error
    still cluster together."""
    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="legacy a", assignee="coder")
        t2 = kb.create_task(conn, title="legacy b", assignee="coder")
        # Raw payloads WITHOUT a stamped fingerprint (pre-S1 shape).
        kb.add_event(conn, t1, kb.HEILER_CLASSIFICATION_EVENT, {
            "class": kb.HEILER_CLASS_REAL_BUG,
            "evidence": {"excerpt": "pid 1 build failed: missing symbol at 1700000000000"},
        })
        kb.add_event(conn, t2, kb.HEILER_CLASSIFICATION_EVENT, {
            "class": kb.HEILER_CLASS_REAL_BUG,
            "evidence": {"excerpt": "pid 2 build failed: missing symbol at 1700000000009"},
        })
        ledger = kb.read_escalation_ledger(conn)

    clusters = ledger["real_bug_signatures"]
    assert len(clusters) == 1
    assert clusters[0]["count"] == 2
    assert set(clusters[0]["example_roots"]) == {t1, t2}


# ---------------------------------------------------------------------------
# HEILER-CLASSIFY-COVERAGE-S1: every operator_escalation must end up with a
# paired heiler_classification (the Stratege's by_class input). The inline
# failure/park paths classify atomically; classify_escalations_sweep is the
# deterministic, idempotent safety net for every other escalation writer.
# ---------------------------------------------------------------------------

def _escalation_event(conn, task_id):
    """Return the (single) operator_escalation Event for a task."""
    return next(
        e for e in kb.list_events(conn, task_id)
        if e.kind == kb.OPERATOR_ESCALATION_EVENT
    )


def _heiler_events(conn, task_id):
    return [
        e for e in kb.list_events(conn, task_id)
        if e.kind == kb.HEILER_CLASSIFICATION_EVENT
    ]


def _raw_escalation(conn, task_id, *, why_now="legacy escalation", evidence=None):
    """Emit a bare ``operator_escalation`` with NO inline classification.

    Stands in for a legacy/forgotten/future escalation writer the safety-net
    ``classify_escalations_sweep`` must still cover. Every *known* inline writer
    (failure breaker, stall park, budget-runaway park, release-gate) now
    classifies atomically — see ESCALATION-INLINE-CLASSIFY-S1 — so the sweep's
    own derivation can no longer be exercised through one of them.
    """
    payload = {
        "task": {"id": task_id},
        "why_now": why_now,
        "evidence": evidence or {},
    }
    with kb.write_txn(conn):
        return kb._append_event(
            conn, task_id, kb.OPERATOR_ESCALATION_EVENT, payload,
        )


def test_record_task_failure_escalation_carries_escalation_event_id(kanban_home):
    """When the breaker trips, the inline heiler_classification references the
    escalation event it pairs with (the AC-2 documented ledger reference)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="red gate", assignee="coder")
        assert kb.claim_task(conn, tid) is not None
        kb._record_task_failure(
            conn, tid, "gate failed: tests failed",
            outcome="crashed", failure_limit=1,
            release_claim=True, end_run=True,
        )
        esc = _escalation_event(conn, tid)
        heilers = _heiler_events(conn, tid)

    assert len(heilers) == 1
    assert heilers[0].payload["escalation_event_id"] == esc.id
    assert heilers[0].payload["class"] == kb.HEILER_CLASS_REAL_BUG


def test_park_budget_runaway_writes_inline_heiler_classification(kanban_home):
    """ESCALATION-INLINE-CLASSIFY-S1 (defense-in-depth): the budget-runaway park
    classifies atomically AT the escalation site — exactly one
    heiler_classification, referencing the escalation event, tagged with the
    inline budget-runaway source, with a belegter (signal-source) evidence
    reference rather than a guess (AC-2). No sweep poll required."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="runaway loop", assignee="coder")
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        parked = kb._park_budget_runaway(
            conn, row, token_sum=5000, cap=1000, runs=3,
        )
        esc = _escalation_event(conn, tid)
        heilers = _heiler_events(conn, tid)

    assert parked is True
    assert len(heilers) == 1
    assert heilers[0].payload["escalation_event_id"] == esc.id
    assert heilers[0].payload["source"] == kb.HEILER_SOURCE_BUDGET_RUNAWAY
    assert heilers[0].payload["class"] in kb.HEILER_CLASSES
    assert heilers[0].payload["blocked"] is True
    assert heilers[0].payload["evidence"].get("signal_source")


def test_park_budget_runaway_inline_matches_sweep_and_sweep_skips(kanban_home):
    """The inline class is byte-identical to what the backfill sweep would
    derive from the same persisted payload (defense-in-depth, NOT divergence),
    and the sweep then adds nothing because the escalation is already paired."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="runaway loop", assignee="coder")
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        kb._park_budget_runaway(conn, row, token_sum=5000, cap=1000, runs=3)
        esc = _escalation_event(conn, tid)
        inline = _heiler_events(conn, tid)[0]
        expected_class, _ = kb._classify_escalation_payload(esc.payload)

        summary = kb.classify_escalations_sweep(conn)
        heilers = _heiler_events(conn, tid)

    assert inline.payload["class"] == expected_class
    assert summary["classified"] == []
    assert len(heilers) == 1


def test_classify_escalations_sweep_classifies_unpaired_escalation(kanban_home):
    """A bare escalation from a writer that did NOT classify inline gets exactly
    one backfilled classification from the sweep, referencing the escalation
    event and deriving the class from its evidence."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy escalation", assignee="coder")
        _raw_escalation(conn, tid, why_now="gate failed: tests failed")
        # Pre-sweep: escalation present, no classification.
        assert _heiler_events(conn, tid) == []
        esc = _escalation_event(conn, tid)

        summary = kb.classify_escalations_sweep(conn)

        heilers = _heiler_events(conn, tid)

    assert len(heilers) == 1
    assert heilers[0].payload["escalation_event_id"] == esc.id
    assert heilers[0].payload["source"] == kb.HEILER_SOURCE_ESCALATION_SWEEP
    assert heilers[0].payload["class"] in kb.HEILER_CLASSES
    assert any(c["escalation_event_id"] == esc.id for c in summary["classified"])


def test_classify_escalations_sweep_is_idempotent(kanban_home):
    """Re-running the sweep adds no second classification for the same
    escalation."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy escalation", assignee="coder")
        _raw_escalation(conn, tid, why_now="merge conflict in api.ts")
        first = kb.classify_escalations_sweep(conn)
        second = kb.classify_escalations_sweep(conn)
        heilers = _heiler_events(conn, tid)

    assert len(heilers) == 1
    assert len(first["classified"]) == 1
    assert second["classified"] == []


def test_classify_escalations_sweep_skips_inline_paired(kanban_home):
    """An escalation already paired inline (record_task_failure) is left
    untouched by the sweep — no duplicate classification."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="red gate", assignee="coder")
        assert kb.claim_task(conn, tid) is not None
        kb._record_task_failure(
            conn, tid, "gate failed: tests failed",
            outcome="crashed", failure_limit=1,
            release_claim=True, end_run=True,
        )
        before = len(_heiler_events(conn, tid))
        summary = kb.classify_escalations_sweep(conn)
        after = len(_heiler_events(conn, tid))

    assert before == 1
    assert after == 1
    assert summary["classified"] == []


def test_classify_escalations_sweep_derives_class_from_evidence(kanban_home):
    """The sweep reuses the deterministic classifier over the escalation's own
    persisted evidence — a merge-conflict park is classed 'conflict'."""
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="merge mess", assignee="coder")
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        kb._park_stall_once(
            conn, row,
            stall_class="integration_parked",
            reason="integration parked: merge conflict in api.ts",
            evidence={"attempts": 2},
            now=now,
        )
        # _park_stall_once classifies inline; strip it so we test the sweep's
        # own derivation path on a genuinely unpaired escalation.
        conn.execute(
            "DELETE FROM task_events WHERE task_id = ? AND kind = ?",
            (tid, kb.HEILER_CLASSIFICATION_EVENT),
        )
        conn.commit()
        assert _heiler_events(conn, tid) == []

        kb.classify_escalations_sweep(conn)
        heilers = _heiler_events(conn, tid)

    assert len(heilers) == 1
    assert heilers[0].payload["class"] == kb.HEILER_CLASS_CONFLICT


def test_record_classification_correction_records_event(kanban_home):
    """An operator correction is stored as a distinct
    heiler_classification_corrected event referencing the escalation, leaving
    the auto by_class ledger untouched."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="runaway loop", assignee="coder")
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        kb._park_budget_runaway(conn, row, token_sum=5000, cap=1000, runs=3)
        kb.classify_escalations_sweep(conn)
        esc = _escalation_event(conn, tid)

        ok = kb.record_classification_correction(
            conn, esc.id,
            corrected_to=kb.HEILER_CLASS_BAD_SPEC,
            reason="operator: this was an underspecified AC, not a runaway",
        )
        corrections = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.HEILER_CLASSIFICATION_CORRECTED_EVENT
        ]
        # auto ledger unchanged (still exactly one auto classification)
        autos = _heiler_events(conn, tid)

    assert ok is True
    assert len(corrections) == 1
    assert corrections[0].payload["escalation_event_id"] == esc.id
    assert corrections[0].payload["corrected_to"] == kb.HEILER_CLASS_BAD_SPEC
    assert len(autos) == 1


def test_record_classification_correction_rejects_unknown_class(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="coder")
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        kb._park_budget_runaway(conn, row, token_sum=5000, cap=1000, runs=3)
        esc = _escalation_event(conn, tid)
        with pytest.raises(ValueError):
            kb.record_classification_correction(
                conn, esc.id, corrected_to="not-a-class",
            )
        # a non-existent escalation id is a no-op, not a crash
        assert kb.record_classification_correction(
            conn, 999_999, corrected_to=kb.HEILER_CLASS_REAL_BUG,
        ) is False


# ---------------------------------------------------------------------------
# chain_cost_breakdown
# ---------------------------------------------------------------------------

def _insert_run_cost(conn, task_id, *, profile, input_tokens, output_tokens, cost_usd):
    """Insert a closed run with explicit cost/token data (no auto-commit; caller manages txn)."""
    conn.execute(
        "INSERT INTO task_runs "
        "(task_id, profile, status, outcome, started_at, ended_at, "
        "input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, 'done', 'completed', 1000, 2000, ?, ?, ?)",
        (task_id, profile, input_tokens, output_tokens, cost_usd),
    )


def test_chain_cost_breakdown_aggregates_by_lane(kanban_home):
    """chain_cost_breakdown returns totals + per-profile breakdown for a chain."""
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="chain-root", assignee="orchestrator",
                              triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "build A", "assignee": "coder", "parents": []},
                {"title": "build B", "assignee": "verifier", "parents": []},
            ],
            author="decomposer",
        )
        a, b = child_ids
        with kb.write_txn(conn):
            # Two runs on profile "coder" for task A
            _insert_run_cost(conn, a, profile="coder", input_tokens=1000, output_tokens=200, cost_usd=0.01)
            _insert_run_cost(conn, a, profile="coder", input_tokens=500, output_tokens=100, cost_usd=0.005)
            # One run on profile "verifier" for task B
            _insert_run_cost(conn, b, profile="verifier", input_tokens=300, output_tokens=50, cost_usd=0.003)

    with kb.connect_closing() as conn:
        result = kb.chain_cost_breakdown(conn, root)

    assert result["root_id"] == root
    assert result["schema"] == "kanban-chain-costs-v1"

    totals = result["totals"]
    assert totals["run_count"] == 3
    assert totals["input_tokens"] == 1800
    assert totals["output_tokens"] == 350
    assert abs(totals["cost_usd"] - 0.018) < 1e-9

    by_lane = result["by_lane"]
    # by_lane is sorted descending by cost_usd
    assert len(by_lane) == 2
    coder_lane = next(l for l in by_lane if l["profile"] == "coder")
    assert coder_lane["run_count"] == 2
    assert coder_lane["input_tokens"] == 1500
    assert coder_lane["output_tokens"] == 300
    assert abs(coder_lane["cost_usd"] - 0.015) < 1e-9

    verifier_lane = next(l for l in by_lane if l["profile"] == "verifier")
    assert verifier_lane["run_count"] == 1
    assert verifier_lane["input_tokens"] == 300
    assert verifier_lane["output_tokens"] == 50
    assert abs(verifier_lane["cost_usd"] - 0.003) < 1e-9

    # descending cost order: coder (0.015) > verifier (0.003)
    assert by_lane[0]["profile"] == "coder"
    assert by_lane[1]["profile"] == "verifier"


def test_chain_cost_breakdown_null_cost_robust(kanban_home):
    """chain_cost_breakdown handles NULL cost_usd rows without crashing.

    Runs without cost data (pre-K5a / unattributed) produce cost_usd=0.0 in
    the aggregate totals — the presence of a NULL-cost run is indicated only by
    a non-zero run_count with zero cost, not a crash.
    """
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="null-cost-root", assignee="orchestrator",
                              triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "task X", "assignee": "coder", "parents": []},
            ],
            author="decomposer",
        )
        (x,) = child_ids
        with kb.write_txn(conn):
            # Run with NULL cost
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, "
                "started_at, ended_at, input_tokens, output_tokens, cost_usd) "
                "VALUES (?, 'coder', 'done', 'completed', 1000, 2000, 400, 80, NULL)",
                (x,),
            )

    with kb.connect_closing() as conn:
        result = kb.chain_cost_breakdown(conn, root)

    assert result["totals"]["run_count"] == 1
    assert result["totals"]["input_tokens"] == 400
    assert result["totals"]["output_tokens"] == 80
    # A NULL-only cost SUM is normalised to 0.0 via COALESCE — the NULL-cost run
    # shows up as a non-zero run_count with zero cost, never None and never a crash.
    assert result["totals"]["cost_usd"] == 0.0
    assert len(result["by_lane"]) == 1
    assert result["by_lane"][0]["cost_usd"] == 0.0
    assert result["by_lane"][0]["run_count"] == 1


def test_chain_cost_breakdown_empty_chain(kanban_home):
    """chain_cost_breakdown for a root with no runs returns zeroed totals."""
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="empty-chain", assignee="orchestrator")

    with kb.connect_closing() as conn:
        result = kb.chain_cost_breakdown(conn, root)

    assert result["root_id"] == root
    assert result["totals"]["run_count"] == 0
    assert result["totals"]["input_tokens"] == 0
    assert result["totals"]["output_tokens"] == 0
    assert result["totals"]["cost_usd"] == 0.0
    assert result["by_lane"] == []


def _insert_run_cost_with_meta(conn, task_id, *, profile, input_tokens, output_tokens,
                               cost_usd, metadata=None):
    """Insert a closed run with explicit cost/token data and optional metadata JSON."""
    import json as _json
    meta_str = _json.dumps(metadata) if metadata is not None else None
    conn.execute(
        "INSERT INTO task_runs "
        "(task_id, profile, status, outcome, started_at, ended_at, "
        "input_tokens, output_tokens, cost_usd, metadata) "
        "VALUES (?, ?, 'done', 'completed', 1000, 2000, ?, ?, ?, ?)",
        (task_id, profile, input_tokens, output_tokens, cost_usd, meta_str),
    )


def test_claude_opus_equivalent_uses_anthropic_model_label_prices():
    """G5: Claude equivalent cost is priced from the model label including cache read."""
    equivalent = kb._equiv_from_tokens(
        None,
        "claude-opus-4-8",
        131_747,
        4_793,
        cache_read=350_208,
    )
    assert equivalent is not None
    assert equivalent == pytest.approx(0.953664)
    assert equivalent > 0


def test_codex_gpt55_equivalent_golden_reproduces_7_92776():
    """S5 golden: Codex gpt-5.5 (run 4828) reproduces $7.92776 exactly from the
    models.dev prices ($5/$30/cr$0.5 per Mtok). 979746 in / 26557 out / 4464640
    cache_read; the 2999 reasoning tokens are ALREADY inside the 26557
    output_tokens and must never be added a second time (would double-count)."""
    equivalent = kb._equiv_from_tokens(
        "openai", "gpt-5.5",
        979_746, 26_557,
        cache_read=4_464_640,
    )
    assert equivalent is not None
    # 979746·$5 + 26557·$30 + 4464640·$0.5 (per Mtok) = $7.92776
    assert equivalent == pytest.approx(7.92776)
    assert equivalent > 0


def test_chain_cost_breakdown_subscription_run_cost_usd_equivalent(kanban_home):
    """A claude-cli run with cost_usd=0 + metadata.cost_usd_equivalent=0.42 →
    by_lane cost_usd_equivalent==0.42, cost_effective_usd==0.42, cost_usd==0.0."""
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="sub-chain", assignee="orchestrator",
                              triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "sub-task", "assignee": "claude-cli", "parents": []}],
            author="decomposer",
        )
        (task_a,) = child_ids
        with kb.write_txn(conn):
            _insert_run_cost_with_meta(
                conn, task_a,
                profile="claude-cli",
                input_tokens=1000,
                output_tokens=200,
                cost_usd=0.0,
                metadata={"cost_usd_equivalent": 0.42},
            )

    with kb.connect_closing() as conn:
        result = kb.chain_cost_breakdown(conn, root)

    lane = next(l for l in result["by_lane"] if l["profile"] == "claude-cli")
    assert lane["cost_usd"] == pytest.approx(0.0)
    assert lane["cost_usd_equivalent"] == pytest.approx(0.42)
    assert lane["cost_effective_usd"] == pytest.approx(0.42)

    totals = result["totals"]
    assert totals["cost_usd"] == pytest.approx(0.0)
    assert totals["cost_usd_equivalent"] == pytest.approx(0.42)
    assert totals["cost_effective_usd"] == pytest.approx(0.42)


def test_runs_windowed_rollup_caches_lane_lookup_per_profile(kanban_home, monkeypatch):
    """Windowed rollup resolves active lane provider/model once per profile."""
    calls = []

    def fake_lane_provider_model(profile, *, board=None):
        calls.append((profile, board))
        return f"{profile}-provider", f"{profile}-model"

    monkeypatch.setattr(kb, "_lane_provider_model_for_profile", fake_lane_provider_model)

    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="many runner root", assignee="orchestrator")
        with kb.write_txn(conn):
            for index in range(30):
                profile = "coder" if index % 2 == 0 else "verifier"
                conn.execute(
                    "INSERT INTO task_runs "
                    "(task_id, profile, status, outcome, started_at, ended_at, "
                    "input_tokens, output_tokens, cost_usd) "
                    "VALUES (?, ?, 'done', 'completed', ?, ?, 10, 2, 0.01)",
                    (root, profile, 1000 + index, 1010 + index),
                )
        kb.complete_task(conn, root, summary="done")

    with kb.connect_closing() as conn:
        result = kb.runs_windowed_rollup(
            conn, since_hours=24, max_roots=5, board="default"
        )

    assert len(calls) == len(set(calls))
    assert len(calls) <= 3
    assert ("coder", "default") in calls
    assert ("verifier", "default") in calls
    root_row = next(row for row in result["roots"] if row["id"] == root)
    workers = {worker["profile"]: worker for worker in root_row["workers"]}
    assert workers["coder"]["provider"] == "coder-provider"
    assert workers["verifier"]["provider"] == "verifier-provider"
    runners_by_profile = {
        runner["profile"]: runner for runner in root_row["runners"]
    }
    assert runners_by_profile["coder"]["provider"] == "coder-provider"
    assert runners_by_profile["verifier"]["provider"] == "verifier-provider"


def test_runs_windowed_rollup_exposes_source_and_unknown_counts(kanban_home, monkeypatch):
    """S1a contract: provider/model source is explicit and missing price evidence stays null."""
    monkeypatch.setattr(
        kb,
        "_lane_provider_model_for_profile",
        lambda profile, *, board=None: (f"{profile}-provider", f"{profile}-model"),
    )
    with kb.connect_closing() as conn:
        metered_root = kb.create_task(conn, title="metered root", assignee="orchestrator")
        zero_root = kb.create_task(conn, title="known zero root", assignee="orchestrator")
        unknown_root = kb.create_task(conn, title="unknown root", assignee="orchestrator")
        with kb.write_txn(conn):
            conn.execute(
                "DELETE FROM task_runs WHERE task_id IN (?, ?, ?)",
                (metered_root, zero_root, unknown_root),
            )
            _insert_run_cost_with_meta(
                conn,
                metered_root,
                profile="coder",
                input_tokens=81_750,
                output_tokens=2_226,
                cost_usd=0.03760227,
                metadata={"provider": "openrouter", "model": "deepseek/deepseek-chat-v3.1"},
            )
            _insert_run_cost_with_meta(
                conn,
                zero_root,
                profile="free-lane",
                input_tokens=10,
                output_tokens=1,
                cost_usd=0.0,
                metadata={"provider": "local", "model": "noop"},
            )
            _insert_run_cost_with_meta(
                conn,
                unknown_root,
                profile="claude-cli",
                input_tokens=100,
                output_tokens=10,
                cost_usd=None,
                metadata={},
            )

        kb.complete_task(conn, metered_root, summary="done")
        kb.complete_task(conn, zero_root, summary="done")
        kb.complete_task(conn, unknown_root, summary="done")

    with kb.connect_closing() as conn:
        result = kb.runs_windowed_rollup(conn, since_hours=24, max_roots=10, board="default")

    roots = [root for root in result["roots"] if root["id"] in {metered_root, zero_root, unknown_root}]
    assert [root["id"] for root in roots] == [metered_root, zero_root, unknown_root]

    metered = roots[0]
    assert metered["cost_usd"] == pytest.approx(0.03760227)
    assert metered["cost_effective_usd"] == pytest.approx(0.03760227)
    assert metered["unknown_run_count"] == 0
    assert metered["workers"][0]["provider_model_source"] == "run_metadata"
    assert metered["runners"][0]["provider_model_source"] == "run_metadata"

    zero = roots[1]
    assert zero["cost_effective_usd"] == pytest.approx(0.0)
    assert zero["unknown_run_count"] == 0

    unknown = roots[2]
    assert unknown["cost_usd"] is None
    assert unknown["cost_usd_equivalent"] is None
    assert unknown["cost_effective_usd"] is None
    assert unknown["unknown_run_count"] == 1
    assert unknown["workers"][0]["cost_effective_usd"] is None
    assert unknown["workers"][0]["unknown_run_count"] == 1
    assert unknown["workers"][0]["provider_model_source"] == "lane_current_fallback"
    assert unknown["runners"][0]["provider_model_source"] == "lane_current_fallback"


def test_runs_windowed_rollup_emits_neuralwatt_request_cost_detail(kanban_home, monkeypatch):
    """NeuralWatt detail is sourced from metadata.cost.request_cost_usd, not kWh × rate."""
    monkeypatch.setattr(
        kb,
        "_lane_provider_model_for_profile",
        lambda profile, *, board=None: (f"{profile}-provider", f"{profile}-model"),
    )

    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="neuralwatt detail root", assignee="orchestrator")
        with kb.write_txn(conn):
            _insert_run_cost_with_meta(
                conn,
                root,
                profile="coder",
                input_tokens=100,
                output_tokens=20,
                cost_usd=0.40,
                metadata={},
            )
            _insert_run_cost_with_meta(
                conn,
                root,
                profile="neuralwatt",
                input_tokens=200,
                output_tokens=50,
                cost_usd=0.0,
                metadata={
                    "energy": {"energy_kwh": 0.03, "usd_per_kwh": 999.0},
                    "cost": {"request_cost_usd": 0.12},
                },
            )
        kb.complete_task(conn, root, summary="done")

    with kb.connect_closing() as conn:
        result = kb.runs_windowed_rollup(conn, since_hours=24, max_roots=5, board="default")

    root_row = next(row for row in result["roots"] if row["id"] == root)
    assert root_row["neuralwatt"] == {
        "energy_kwh": pytest.approx(0.03),
        "request_cost_usd": pytest.approx(0.12),
    }
    workers = {worker["profile"]: worker for worker in root_row["workers"]}
    assert workers["coder"]["neuralwatt"] is None
    assert workers["neuralwatt"]["neuralwatt"] == {
        "energy_kwh": pytest.approx(0.03),
        "request_cost_usd": pytest.approx(0.12),
    }
    runners = {runner["profile"]: runner for runner in root_row["runners"]}
    assert runners["coder"]["neuralwatt"] is None
    assert runners["neuralwatt"]["neuralwatt"] == {
        "energy_kwh": pytest.approx(0.03),
        "request_cost_usd": pytest.approx(0.12),
    }


def test_chain_cost_breakdown_emits_actual_and_neuralwatt(kanban_home):
    """chain_cost_breakdown exposes actual API + NeuralWatt billing fields."""
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="actual-cost-chain", assignee="orchestrator")
        with kb.write_txn(conn):
            _insert_run_cost_with_meta(
                conn,
                root,
                profile="coder",
                input_tokens=1000,
                output_tokens=200,
                cost_usd=0.40,
                metadata={},
            )
            _insert_run_cost_with_meta(
                conn,
                root,
                profile="neuralwatt",
                input_tokens=2000,
                output_tokens=500,
                cost_usd=0.0,
                metadata={
                    "cost_usd_equivalent": 0.90,
                    "energy": {"energy_kwh": 0.03, "usd_per_kwh": 999.0},
                    "cost": {"request_cost_usd": 0.12},
                },
            )

    with kb.connect_closing() as conn:
        result = kb.chain_cost_breakdown(conn, root)

    lanes = {lane["profile"]: lane for lane in result["by_lane"]}
    assert lanes["coder"]["actual_cost_usd"] == pytest.approx(0.40)
    assert lanes["coder"]["billing_neuralwatt_kwh"] == pytest.approx(0.0)

    neuralwatt = lanes["neuralwatt"]
    assert neuralwatt["billing_neuralwatt_kwh"] == pytest.approx(0.03)
    assert neuralwatt["billing_neuralwatt_cost_usd"] == pytest.approx(0.12)
    assert neuralwatt["actual_cost_usd"] == pytest.approx(0.12)
    assert neuralwatt["api_equivalent_usd"] == pytest.approx(0.90)

    totals = result["totals"]
    assert totals["actual_cost_usd"] == pytest.approx(0.52)
    assert totals["billing_neuralwatt_cost_usd"] == pytest.approx(0.12)
    assert totals["api_equivalent_usd"] == pytest.approx(0.90)


def test_chain_cost_breakdown_real_cost_no_equivalent(kanban_home):
    """A run with real cost_usd>0 and no equivalent → cost_effective_usd==cost_usd."""
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="real-cost-chain", assignee="orchestrator")
        with kb.write_txn(conn):
            _insert_run_cost_with_meta(
                conn, root,
                profile="openrouter",
                input_tokens=500,
                output_tokens=100,
                cost_usd=0.03,
                metadata=None,
            )

    with kb.connect_closing() as conn:
        result = kb.chain_cost_breakdown(conn, root)

    lane = result["by_lane"][0]
    assert lane["cost_usd"] == pytest.approx(0.03)
    assert lane["cost_usd_equivalent"] == pytest.approx(0.0)
    assert lane["cost_effective_usd"] == pytest.approx(0.03)

    totals = result["totals"]
    assert totals["cost_usd"] == pytest.approx(0.03)
    assert totals["cost_usd_equivalent"] == pytest.approx(0.0)
    assert totals["cost_effective_usd"] == pytest.approx(0.03)


def test_chain_cost_breakdown_sort_by_cost_effective(kanban_home):
    """by_lane is sorted descending by cost_effective_usd so subscription lanes
    with cost_usd=0 but positive equivalent rank above zero-cost API lanes."""
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="sort-chain", assignee="orchestrator",
                              triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "sub-task", "assignee": "claude-cli", "parents": []},
                {"title": "api-task", "assignee": "openrouter", "parents": []},
            ],
            author="decomposer",
        )
        sub_task, api_task = child_ids
        with kb.write_txn(conn):
            # subscription run: cost_usd=0, equivalent=1.00 → effective=1.00
            _insert_run_cost_with_meta(
                conn, sub_task,
                profile="claude-cli",
                input_tokens=2000,
                output_tokens=400,
                cost_usd=0.0,
                metadata={"cost_usd_equivalent": 1.00},
            )
            # API run: cost_usd=0.005, no equivalent → effective=0.005
            _insert_run_cost_with_meta(
                conn, api_task,
                profile="openrouter",
                input_tokens=100,
                output_tokens=20,
                cost_usd=0.005,
                metadata=None,
            )

    with kb.connect_closing() as conn:
        result = kb.chain_cost_breakdown(conn, root)

    by_lane = result["by_lane"]
    # claude-cli (effective=1.00) must rank above openrouter (effective=0.005)
    assert by_lane[0]["profile"] == "claude-cli"
    assert by_lane[1]["profile"] == "openrouter"


def test_recompute_ready_uses_tripped_event_limit_without_dispatcher_config(kanban_home):
    """A task blocked by a stricter dispatcher limit must not escape when a
    later generic recompute call does not pass that dispatcher config.
    """
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="strict dispatcher", assignee="a")
        kb.claim_task(conn, t)
        tripped = kb._record_task_failure(
            conn,
            t,
            error="spawn boom",
            outcome="spawn_failed",
            release_claim=True,
            end_run=True,
            failure_limit=1,
        )
        assert tripped is True
        task = kb.get_task(conn, t)
        assert task is not None
        assert task.status == "blocked"
        assert task.consecutive_failures == 1

        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, t)
        assert task is not None
        assert task.status == "blocked"


def test_s1_claude_included_session_priced_without_task_run_cache_columns(
    kanban_home, tmp_path, monkeypatch,
):
    """Claude subscription sessions can be unpriced and omit billing_provider.

    ``task_runs`` deliberately has no cache-token columns; the fallback must use
    the matched state.db session's model/tokens plus models.dev pricing and infer
    Anthropic for bare ``claude-*`` model names.
    """
    profile_dir = tmp_path / "profiles" / "coder-claude"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription",
                        lambda p: "claude" if p == "coder-claude" else None)

    calls: list[tuple[str, str]] = []

    class _FakeModelInfo:
        cost_input = 15.0
        cost_output = 75.0
        cost_cache_read = 1.50
        cost_cache_write = 18.75

        def has_cost_data(self):
            return True

    def fake_get_model_info(provider, model):
        calls.append((provider, model))
        if (provider, model) == ("anthropic", "claude-opus-4-8"):
            return _FakeModelInfo()
        return None

    monkeypatch.setattr("agent.models_dev.get_model_info", fake_get_model_info)

    with kb.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")}
        assert "cache_read_tokens" not in cols
        assert "cache_write_tokens" not in cols

        tid = kb.create_task(conn, title="claude-unpriced", assignee="coder-claude")
        run_id = _insert_run_window(
            conn, tid, profile="coder-claude", started_at=1000, ended_at=2000)
        _write_session_rows(profile_dir / "state.db", [
            {"id": "S-claude-unpriced", "source": "cli", "started_at": 1500,
             "input_tokens": 1_000_000, "output_tokens": 100_000,
             "actual_cost_usd": None, "estimated_cost_usd": 0.0,
             "model": "claude-opus-4-8", "cwd": f"/x/kanban/workspaces/{tid}"},
        ])

        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 1
        row = conn.execute(
            "SELECT input_tokens, output_tokens, cost_usd, metadata "
            "FROM task_runs WHERE id=?", (run_id,)).fetchone()

    assert row["cost_usd"] == pytest.approx(0.0)
    assert row["input_tokens"] == 1_000_000
    assert row["output_tokens"] == 100_000
    meta = json.loads(row["metadata"])
    assert meta["cost_usd_equivalent"] == pytest.approx(22.5)
    assert meta["model"] == "claude-opus-4-8"
    assert meta["billing_mode"] == "subscription_included"
    assert meta["subscription"] == "claude"
    assert calls == [("anthropic", "claude-opus-4-8")]


def test_s1_openrouter_estimated_cost_status_propagates(kanban_home, tmp_path, monkeypatch):
    """OpenRouter state.db estimated cost stays value-identical and is labeled."""
    profile_dir = tmp_path / "profiles" / "coder"
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda name: str(profile_dir),
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda p: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="openrouter-estimated", assignee="coder")
        run_id = _insert_run_window(
            conn, tid, profile="coder", started_at=800, ended_at=900,
        )
        _write_session_rows(profile_dir / "state.db", [
            {"id": "837", "source": "cli", "started_at": 837,
             "input_tokens": 1000, "output_tokens": 200,
             "estimated_cost_usd": 0.03760227, "cost_status": "estimated",
             "model": "deepseek/deepseek-chat", "billing_provider": "openrouter"},
        ])
        assert kb.backfill_run_costs_from_sessions(conn, limit=50) == 1
        row = conn.execute(
            "SELECT cost_usd, metadata FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        costs = kb.batch_task_costs(conn, [tid])

    meta = json.loads(row["metadata"] or "{}")
    assert row["cost_usd"] == pytest.approx(0.03760227)
    assert meta.get("cost_status") == "estimated"
    assert costs[tid]["cost_usd"] == pytest.approx(0.03760227)
    assert costs[tid]["cost_status"] == "estimated"





def _close_claimed_run_for_backfill(conn, task_id):
    now = int(time.time())
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row is not None
    run_id = row["current_run_id"]
    assert run_id is not None
    conn.execute(
        """
        UPDATE task_runs
           SET status = 'done', ended_at = ?, outcome = 'completed'
         WHERE id = ?
        """,
        (now, run_id),
    )
    conn.commit()
    return run_id


def test_k17_backfill_claude_cli_uses_spawn_identity_after_lane_switch(
    kanban_home,
):
    """Backfill must use spawn-time claude-cli identity, not the active
    lane at backfill time. Otherwise lane/model changes after spawn skip
    the run or stamp the wrong model.
    """
    import json as _json
    with kb.connect_closing() as conn:
        claude_lane = kb.create_lane(
            conn,
            name="spawn-claude",
            profiles={"premium": {
                "worker_runtime": "claude-cli",
                "model": "claude-fable-5",
            }},
        )
        kb.activate_lane(conn, claude_lane["id"])
        tid = kb.create_task(conn, title="cli-spawn", assignee="premium")
        assert kb.claim_task(conn, tid, claimer="test-claimer") is not None
        run_id = _close_claimed_run_for_backfill(conn, tid)

        spawn_meta = _json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()["metadata"])
        assert spawn_meta["worker_runtime"] == "claude-cli"
        assert spawn_meta["model"] == "claude-fable-5"
        assert spawn_meta["provider"] is None

        hermes_lane = kb.create_lane(
            conn,
            name="later-hermes",
            profiles={"premium": {
                "worker_runtime": "hermes",
                "provider": "openrouter",
                "model": "openai/gpt-5-mini",
            }},
        )
        kb.activate_lane(conn, hermes_lane["id"])
        _write_claude_result_log(tid, total_cost_usd=0.42, output_tokens=33)

        assert kb.backfill_run_costs(conn, limit=50) == 1

        row = conn.execute(
            "SELECT output_tokens, cost_usd, metadata FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert row["output_tokens"] == 33
        assert row["cost_usd"] == pytest.approx(0.0)
        meta = _json.loads(row["metadata"])
        assert meta["worker_runtime"] == "claude-cli"
        assert meta["model"] == "claude-fable-5"
        assert meta["provider"] is None
        assert meta["cost_usd_equivalent"] == pytest.approx(0.42)


def test_k17_backfill_claude_cli_spawn_identity_prefers_model_override_after_lane_switch(
    kanban_home,
):
    """Per-task model_override is spawn-time identity and must survive a
    later active-lane model change before claude-cli log backfill.
    """
    import json as _json
    with kb.connect_closing() as conn:
        claude_lane = kb.create_lane(
            conn,
            name="override-claude",
            profiles={"premium": {
                "worker_runtime": "claude-cli",
                "model": "claude-fable-5",
            }},
        )
        kb.activate_lane(conn, claude_lane["id"])
        tid = kb.create_task(
            conn,
            title="cli-override",
            assignee="premium",
            model_override="claude-opus-4-1",
        )
        assert kb.claim_task(conn, tid, claimer="test-claimer") is not None
        run_id = _close_claimed_run_for_backfill(conn, tid)

        hermes_lane = kb.create_lane(
            conn,
            name="override-later-hermes",
            profiles={"premium": {
                "worker_runtime": "hermes",
                "provider": "openrouter",
                "model": "openai/gpt-5-mini",
            }},
        )
        kb.activate_lane(conn, hermes_lane["id"])
        _write_claude_result_log(tid, total_cost_usd=0.55)

        assert kb.backfill_run_costs(conn, limit=50) == 1

        meta = _json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()["metadata"])
        assert meta["worker_runtime"] == "claude-cli"
        assert meta["model"] == "claude-opus-4-1"
        assert meta["provider"] is None
        assert meta["cost_usd_equivalent"] == pytest.approx(0.55)


def test_k17_backfill_claude_cli_lane_metadata_preserves_existing_keys(
    kanban_home,
):
    """Active claude-cli lanes stamp identity metadata without clobbering
    pre-existing run metadata, including future fallback evidence.
    """
    import json as _json
    with kb.connect_closing() as conn:
        lane = kb.create_lane(
            conn,
            name="claude-max",
            profiles={"coder-claude": {
                "worker_runtime": "claude-cli",
                "model": "claude-fable-5",
            }},
        )
        kb.activate_lane(conn, lane["id"])
        tid = kb.create_task(conn, title="cli-lane", assignee="coder-claude")
        run_id = _insert_ended_run(
            conn,
            tid,
            profile="coder-claude",
            metadata={"note": "keep", "fallback_used": True},
        )
        _write_claude_result_log(tid, total_cost_usd=0.42)

        assert kb.backfill_run_costs(conn, limit=50) == 1

        meta = _json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()["metadata"])
        assert meta["note"] == "keep"
        assert meta["worker_runtime"] == "claude-cli"
        assert meta["model"] == "claude-fable-5"
        assert meta["provider"] is None
        assert meta["fallback_used"] is True
        assert meta["billing_mode"] == "subscription_included"
        assert meta["cost_usd_equivalent"] == pytest.approx(0.42)
