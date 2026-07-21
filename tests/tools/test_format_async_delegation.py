"""Characterization tests for ``tools.process_registry._format_async_delegation``.

Pins the exact rendered re-injection text for BOTH the single-delegation and
batch (fan-out) branches, with fixed ``dispatched_at``/``completed_at`` so the
output is deterministic (the function otherwise falls back to ``time.time()``).

This is the behavior contract protecting the R1 refactor that extracts the
batch branch into its own helper — output must stay byte-identical.
"""
from __future__ import annotations

import time

from tools.process_registry import _format_age, _format_async_delegation

DISPATCHED = 1_700_000_000
COMPLETED = 1_700_000_060  # 60s later → age "1m"


def _ts(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


# ─── _format_age (deterministic dependency) ─────────────────────────────────


def test_format_age_buckets():
    assert _format_age(45) == "45s"
    assert _format_age(60) == "1m"
    assert _format_age(125) == "2m5s"
    assert _format_age(3600) == "1h"
    assert _format_age(3660) == "1h1m"
    assert _format_age(None) == "?"  # type: ignore[arg-type]


# ─── single-delegation branch ───────────────────────────────────────────────


def test_single_completed_exact_lines():
    evt = {
        "delegation_id": "dlg-1",
        "goal": "do thing",
        "context": "ctx",
        "toolsets": ["bash", "read"],
        "role": "leaf",
        "model": "gpt-5",
        "status": "completed",
        "summary": "did the thing",
        "api_calls": 3,
        "duration_seconds": 12,
        "dispatched_at": DISPATCHED,
        "completed_at": COMPLETED,
    }
    lines = _format_async_delegation(evt).split("\n")
    assert lines[0] == "[ASYNC DELEGATION COMPLETE — dlg-1]"
    assert f"Dispatched: {_ts(DISPATCHED)} (1m ago)" in lines
    assert "Original goal: do thing" in lines
    assert "Context you provided: ctx" in lines
    assert "Toolsets: bash, read" in lines
    assert "Role: leaf   Model: gpt-5" in lines
    assert "Status: completed   API calls: 3   Duration: 12s" in lines
    assert "--- RESULT ---" in lines
    assert "did the thing" in lines
    # Ordering: header before result, result summary is the final line.
    assert lines.index("--- RESULT ---") < lines.index("did the thing")
    assert lines[-1] == "did the thing"


def test_single_without_dispatched_at_has_no_dispatched_line():
    evt = {"delegation_id": "d", "goal": "g", "status": "completed", "summary": "s"}
    out = _format_async_delegation(evt)
    assert "Dispatched:" not in out
    assert "Original goal: g" in out


def test_single_interrupted_with_partial():
    evt = {
        "delegation_id": "d",
        "goal": "g",
        "status": "interrupted",
        "error": "stopped",
        "summary": "partial text",
    }
    out = _format_async_delegation(evt)
    assert "The subagent was interrupted before completing: stopped" in out
    assert "Partial output:" in out
    assert "partial text" in out


def test_single_error_with_partial():
    evt = {
        "delegation_id": "d",
        "goal": "g",
        "status": "failed",
        "error": "boom",
        "summary": "half done",
    }
    out = _format_async_delegation(evt)
    assert "The subagent did not complete successfully (status=failed)." in out
    assert "boom" in out
    assert "Partial output:" in out
    assert "half done" in out


# ─── batch (fan-out) branch ─────────────────────────────────────────────────


def _batch_evt(**over) -> dict:
    evt = {
        "delegation_id": "batch-1",
        "is_batch": True,
        "context": "ctx",
        "toolsets": ["bash"],
        "role": "leaf",
        "model": "gpt-5",
        "goals": ["g1", "g2"],
        "results": [
            {"task_index": 0, "status": "completed", "summary": "ok0",
             "api_calls": 2, "duration_seconds": 5},
            {"task_index": 1, "status": "failed", "error": "boom",
             "summary": "partial1"},
        ],
        "total_duration_seconds": 10,
        "dispatched_at": DISPATCHED,
        "completed_at": COMPLETED,
    }
    evt.update(over)
    return evt


def test_batch_detected_by_is_batch_flag_exact_lines():
    lines = _format_async_delegation(_batch_evt()).split("\n")
    assert lines[0] == "[ASYNC DELEGATION BATCH COMPLETE — batch-1]"
    assert "A background fan-out of 2 subagent(s) you dispatched earlier has finished." in lines[1]
    assert f"Dispatched: {_ts(DISPATCHED)} (1m ago)" in lines
    assert "Context you provided: ctx" in lines
    assert "Toolsets: bash" in lines
    assert "Role: leaf   Model: gpt-5   Total duration: 10s" in lines
    # Per-task headers in task_index order.
    assert "--- ✓ TASK 1/2: g1  (status=completed, api_calls=2, 5s) ---" in lines
    assert "--- ✗ TASK 2/2: g2  (status=failed) ---" in lines
    assert "ok0" in lines
    assert "(failed: boom)" in lines
    assert "Partial output:" in lines
    assert "partial1" in lines
    # Ordering: task 1 header before task 2 header.
    i1 = lines.index("--- ✓ TASK 1/2: g1  (status=completed, api_calls=2, 5s) ---")
    i2 = lines.index("--- ✗ TASK 2/2: g2  (status=failed) ---")
    assert i1 < i2


def test_batch_detected_by_results_list_without_flag():
    evt = _batch_evt()
    del evt["is_batch"]
    out = _format_async_delegation(evt)
    assert "[ASYNC DELEGATION BATCH COMPLETE — batch-1]" in out


def test_batch_error_without_results_short_circuits():
    evt = _batch_evt(results=[], error="whole batch failed", goals=["g1"])
    out = _format_async_delegation(evt)
    assert "--- ERROR ---" in out
    assert "The batch did not complete successfully: whole batch failed" in out
    # No per-task rendering when there are no results.
    assert "TASK 1/" not in out


def test_batch_task_without_goal_uses_result_goal():
    evt = _batch_evt(
        goals=[],
        results=[{"task_index": 0, "status": "completed", "summary": "s",
                  "goal": "inline-goal"}],
    )
    out = _format_async_delegation(evt)
    assert "--- ✓ TASK 1/1: inline-goal  (status=completed) ---" in out
