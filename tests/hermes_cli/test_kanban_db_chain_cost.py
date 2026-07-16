"""Kanban DB tests: chain cost.

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
    _insert_ended_run,
    _write_claude_result_log,
    _write_session_rows,
    _insert_run_window,
)

def _insert_run_cost(conn, task_id, *, profile, input_tokens, output_tokens, cost_usd):
    """Insert a closed run with explicit cost/token data (no auto-commit; caller manages txn)."""
    conn.execute(
        "INSERT INTO task_runs "
        "(task_id, profile, status, outcome, started_at, ended_at, "
        "input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, 'done', 'completed', 1000, 2000, ?, ?, ?)",
        (task_id, profile, input_tokens, output_tokens, cost_usd),
    )


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

