"""Kanban DB tests: costs.

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
    _insert_ended_run,
    _write_claude_result_log,
    _write_session_rows,
    _insert_run_window,
)

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

