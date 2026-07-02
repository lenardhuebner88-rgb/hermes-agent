"""Tests for Lever-Outcomes anchor file (LEVER-OUTCOMES-S1).

TDD-first: all tests use board_home-style temp dirs with explicit path injection
(same pattern as test_strategist_harness.py). No real LLM calls, no real
usage-API calls. Records have fake root_task_ids where the task need not exist.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import strategist


# --------------------------------------------------------------------------- #
# Shared fixtures (mirrors test_strategist_harness.py)
# --------------------------------------------------------------------------- #
@pytest.fixture
def board_home(tmp_path, monkeypatch, all_assignees_spawnable):
    """Isolated temp board + hermes-home."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_VISION_METRICS_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return tmp_path


def _fake_usage(used_percent):
    window = SimpleNamespace(window_key="weekly", used_percent=used_percent, label="Current week")
    return SimpleNamespace(windows=(window,), provider="anthropic")


def _patch_budget(monkeypatch, used_percent):
    monkeypatch.setattr(
        "agent.account_usage.fetch_account_usage",
        lambda provider, **kw: _fake_usage(used_percent),
    )


def _seed_ledger(conn, error, *, outcome="crashed"):
    task = kb.create_task(conn, title=f"seed:{error[:20]}", assignee="coder")
    kb.claim_task(conn, task)
    kb._record_task_failure(
        conn, task, error, outcome=outcome, failure_limit=5, release_claim=True, end_run=True
    )
    return task


_BASE_METRICS = {
    "generated_at": 1_700_000_000,  # fixed timestamp (not stale relative to recent now)
    "autonomy_pct": 75.0,
    "escalations_per_week": 8.0,
    "green_gate_streak": {"streak": 3},
    "fail_nights": 2,
    "recent_avg_cost_per_task": 1.5,
}


def _make_held_and_released_done(conn, key, *, now_ts: int):
    """Create a strategist root, release it, and mark it done."""
    root = kb.create_task(
        conn,
        title=f"PlanSpec {key}: test lever",
        body="held",
        assignee=None,
        created_by=strategist.STRATEGIST_AUTHOR,
    )
    conn.execute(
        "UPDATE tasks SET status='scheduled', freigabe='operator' WHERE id=?",
        (root,),
    )
    conn.commit()
    kb.release_freigabe_hold(conn, root, author="operator")
    # mark done explicitly
    conn.execute(
        "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
        (now_ts, root),
    )
    conn.commit()
    return root


def _make_held_and_released_not_done(conn, key):
    """Create a strategist root, release it but leave it NOT done."""
    root = kb.create_task(
        conn,
        title=f"PlanSpec {key}: test lever",
        body="held",
        assignee=None,
        created_by=strategist.STRATEGIST_AUTHOR,
    )
    conn.execute(
        "UPDATE tasks SET status='scheduled', freigabe='operator' WHERE id=?",
        (root,),
    )
    conn.commit()
    kb.release_freigabe_hold(conn, root, author="operator")
    # deliberately NOT marking done
    return root


# --------------------------------------------------------------------------- #
# Helper: build a shipped-state record (no task needs to exist in DB)
# --------------------------------------------------------------------------- #
def _shipped_record(lever_key, baseline, *, metric_key=None, shipped_at):
    return {
        "schema_version": 1,
        "lever_key": lever_key,
        "root_task_id": 9999,   # non-existent task → safe in reflect() shipped-stamp path
        "proposed_at": shipped_at - 86400,
        "baseline": baseline,
        "metric_key": metric_key,
        "shipped_at": shipped_at,
        "measured_at": None,
        "current": None,
        "delta": None,
        "verdict": None,
        "status": "shipped",
    }


# --------------------------------------------------------------------------- #
# 1. Ingest writes baseline record
# --------------------------------------------------------------------------- #
def test_ingest_writes_baseline_record(board_home, monkeypatch, tmp_path):
    """propose() writes a baseline record with flat numeric keys + root_task_id."""
    _patch_budget(monkeypatch, 30.0)
    with kb.connect() as conn:
        _seed_ledger(conn, "dirty-overlap git lock contention")

    out_dir = board_home / "specs"
    outcomes_path = tmp_path / "lever-outcomes.json"
    result = strategist.propose(
        board=None,
        out_dir=out_dir,
        metrics=_BASE_METRICS,
        outcomes_path=outcomes_path,
    )

    assert len(result["ingested"]) >= 1
    assert outcomes_path.exists()
    records = json.loads(outcomes_path.read_text(encoding="utf-8"))
    assert isinstance(records, list)
    assert len(records) >= 1

    rec = records[0]
    assert rec["schema_version"] == 1
    assert rec["lever_key"]
    assert rec["root_task_id"] is not None
    assert rec["status"] == "proposed"
    assert "baseline" in rec
    assert rec["proposed_at"]
    # baseline must contain only numeric float values
    baseline = rec["baseline"]
    assert all(isinstance(v, float) for v in baseline.values())
    # dotted keys for nested values
    assert "autonomy_pct" in baseline
    assert "green_gate_streak.streak" in baseline
    assert baseline["autonomy_pct"] == pytest.approx(75.0)
    assert baseline["green_gate_streak.streak"] == pytest.approx(3.0)
    # non-numeric / nested dict values must NOT appear as raw values
    assert not any(isinstance(v, dict) for v in baseline.values())


def test_ingest_skips_duplicate_for_already_ingested_lever(board_home, monkeypatch, tmp_path):
    """A re-ingest (already_ingested=True) must not append a duplicate baseline record."""
    _patch_budget(monkeypatch, 30.0)
    with kb.connect() as conn:
        _seed_ledger(conn, "dirty-overlap git lock contention")

    out_dir = board_home / "specs"
    outcomes_path = tmp_path / "lever-outcomes.json"

    # first run — ingest
    strategist.propose(board=None, out_dir=out_dir, metrics=_BASE_METRICS, outcomes_path=outcomes_path)
    first_records = json.loads(outcomes_path.read_text(encoding="utf-8"))

    # second run — already_ingested=True for the same lever
    strategist.propose(board=None, out_dir=out_dir, metrics=_BASE_METRICS, outcomes_path=outcomes_path)
    second_records = json.loads(outcomes_path.read_text(encoding="utf-8"))

    # must not duplicate
    assert len(second_records) == len(first_records)


def test_ingest_noop_when_no_outcomes_path(board_home, monkeypatch):
    """propose() without outcomes_path is a clean no-op — no crash, no file."""
    _patch_budget(monkeypatch, 30.0)
    with kb.connect() as conn:
        _seed_ledger(conn, "dirty-overlap git lock contention")
    out_dir = board_home / "specs"
    result = strategist.propose(board=None, out_dir=out_dir, metrics=_BASE_METRICS)
    assert len(result["ingested"]) >= 1  # still ingests normally


# --------------------------------------------------------------------------- #
# 2. reflect() stamps shipped_at
# --------------------------------------------------------------------------- #
def test_reflect_stamps_shipped_at_when_task_done_and_released(board_home, tmp_path):
    """reflect() stamps shipped_at on proposed records whose root is done+released."""
    now_ts = int(time.time())
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"

    with kb.connect() as conn:
        root = _make_held_and_released_done(conn, "HEILER-TRANSIENT", now_ts=now_ts)

    record = {
        "schema_version": 1,
        "lever_key": "HEILER-TRANSIENT",
        "root_task_id": root,
        "proposed_at": now_ts - 86400,
        "baseline": {"autonomy_pct": 75.0},
        "metric_key": "autonomy_pct",
        "shipped_at": None,
        "measured_at": None,
        "current": None,
        "delta": None,
        "verdict": None,
        "status": "proposed",
    }
    outcomes_path.write_text(json.dumps([record]), encoding="utf-8")

    with kb.connect() as conn:
        result = strategist.reflect(
            conn, since=0, notes_path=notes_path,
            outcomes_path=outcomes_path, now=float(now_ts),
        )

    records = json.loads(outcomes_path.read_text(encoding="utf-8"))
    assert len(records) == 1
    rec = records[0]
    assert rec["status"] == "shipped"
    assert rec["shipped_at"] is not None and rec["shipped_at"] > 0
    # reflect note must carry outcomes counts
    assert result["note"]["outcomes"]["shipped_stamped"] == 1
    assert result["note"]["outcomes"]["measured"] == 0


def test_reflect_does_not_stamp_if_task_not_done(board_home, tmp_path):
    """reflect() leaves proposed records alone when the root task is NOT done."""
    now_ts = int(time.time())
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"

    with kb.connect() as conn:
        root = _make_held_and_released_not_done(conn, "HEILER-TRANSIENT")

    record = {
        "schema_version": 1,
        "lever_key": "HEILER-TRANSIENT",
        "root_task_id": root,
        "proposed_at": now_ts - 86400,
        "baseline": {"autonomy_pct": 75.0},
        "metric_key": "autonomy_pct",
        "shipped_at": None,
        "measured_at": None,
        "current": None,
        "delta": None,
        "verdict": None,
        "status": "proposed",
    }
    outcomes_path.write_text(json.dumps([record]), encoding="utf-8")

    with kb.connect() as conn:
        strategist.reflect(
            conn, since=0, notes_path=notes_path,
            outcomes_path=outcomes_path, now=float(now_ts),
        )

    rec = json.loads(outcomes_path.read_text(encoding="utf-8"))[0]
    assert rec["status"] == "proposed"
    assert rec["shipped_at"] is None


# --------------------------------------------------------------------------- #
# 3. reflect() respects maturity window
# --------------------------------------------------------------------------- #
def test_reflect_does_not_measure_before_maturity(board_home, tmp_path):
    """Shipped record is NOT measured when shipped_at is < MATURITY_DAYS days ago."""
    now_ts = int(time.time())
    # shipped 1 day ago — still inside the maturity window
    shipped_at = now_ts - 86400
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"

    record = _shipped_record(
        "HEILER-TRANSIENT", {"autonomy_pct": 75.0},
        metric_key="autonomy_pct", shipped_at=shipped_at
    )
    outcomes_path.write_text(json.dumps([record]), encoding="utf-8")

    current_metrics = {"generated_at": now_ts, "autonomy_pct": 80.0}
    with kb.connect() as conn:
        strategist.reflect(
            conn, since=0, notes_path=notes_path,
            outcomes_path=outcomes_path, metrics=current_metrics, now=float(now_ts),
        )

    rec = json.loads(outcomes_path.read_text(encoding="utf-8"))[0]
    assert rec["status"] == "shipped"
    assert rec["measured_at"] is None


def test_reflect_measures_after_maturity_window(board_home, tmp_path):
    """After MATURITY_DAYS, reflect() computes current, delta, status=measured."""
    now_ts = int(time.time())
    # shipped just past the maturity window
    shipped_at = now_ts - (strategist.MATURITY_DAYS * 86400) - 3600
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"

    record = _shipped_record(
        "HEILER-TRANSIENT", {"autonomy_pct": 75.0},
        metric_key="autonomy_pct", shipped_at=shipped_at
    )
    outcomes_path.write_text(json.dumps([record]), encoding="utf-8")

    current_metrics = {"generated_at": now_ts, "autonomy_pct": 80.0}
    with kb.connect() as conn:
        result = strategist.reflect(
            conn, since=0, notes_path=notes_path,
            outcomes_path=outcomes_path, metrics=current_metrics, now=float(now_ts),
        )

    rec = json.loads(outcomes_path.read_text(encoding="utf-8"))[0]
    assert rec["status"] == "measured"
    assert rec["measured_at"] is not None
    # current is the full flattened snapshot — at minimum the tested key is present
    assert isinstance(rec["current"], dict)
    assert rec["current"]["autonomy_pct"] == pytest.approx(80.0)
    # delta only contains keys that appear in BOTH current and baseline
    assert rec["delta"]["autonomy_pct"] == pytest.approx(5.0)
    assert result["note"]["outcomes"]["measured"] == 1


# --------------------------------------------------------------------------- #
# 4. Delta + verdict direction map
# --------------------------------------------------------------------------- #
def test_verdict_improved_for_autonomy_pct_up(board_home, tmp_path):
    """autonomy_pct ↑ → verdict=improved."""
    now_ts = int(time.time())
    shipped_at = now_ts - (strategist.MATURITY_DAYS * 86400) - 3600
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"

    record = _shipped_record(
        "X", {"autonomy_pct": 70.0}, metric_key="autonomy_pct", shipped_at=shipped_at
    )
    outcomes_path.write_text(json.dumps([record]), encoding="utf-8")

    with kb.connect() as conn:
        strategist.reflect(
            conn, since=0, notes_path=notes_path, outcomes_path=outcomes_path,
            metrics={"generated_at": now_ts, "autonomy_pct": 80.0}, now=float(now_ts),
        )
    rec = json.loads(outcomes_path.read_text(encoding="utf-8"))[0]
    assert rec["verdict"] == "improved"


def test_verdict_worsened_for_escalations_per_week_up(board_home, tmp_path):
    """escalations_per_week ↑ → verdict=worsened (lower is better)."""
    now_ts = int(time.time())
    shipped_at = now_ts - (strategist.MATURITY_DAYS * 86400) - 3600
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"

    record = _shipped_record(
        "X", {"escalations_per_week": 5.0},
        metric_key="escalations_per_week", shipped_at=shipped_at
    )
    outcomes_path.write_text(json.dumps([record]), encoding="utf-8")

    with kb.connect() as conn:
        strategist.reflect(
            conn, since=0, notes_path=notes_path, outcomes_path=outcomes_path,
            metrics={"generated_at": now_ts, "escalations_per_week": 8.0}, now=float(now_ts),
        )
    rec = json.loads(outcomes_path.read_text(encoding="utf-8"))[0]
    assert rec["verdict"] == "worsened"


def test_verdict_unchanged_for_zero_delta(board_home, tmp_path):
    """Zero delta on a known metric → verdict=unchanged."""
    now_ts = int(time.time())
    shipped_at = now_ts - (strategist.MATURITY_DAYS * 86400) - 3600
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"

    record = _shipped_record(
        "X", {"autonomy_pct": 80.0}, metric_key="autonomy_pct", shipped_at=shipped_at
    )
    outcomes_path.write_text(json.dumps([record]), encoding="utf-8")

    with kb.connect() as conn:
        strategist.reflect(
            conn, since=0, notes_path=notes_path, outcomes_path=outcomes_path,
            metrics={"generated_at": now_ts, "autonomy_pct": 80.0}, now=float(now_ts),
        )
    rec = json.loads(outcomes_path.read_text(encoding="utf-8"))[0]
    assert rec["verdict"] == "unchanged"


def test_verdict_unknown_for_unrecognised_metric_key(board_home, tmp_path):
    """An unrecognised metric_key → verdict=unknown."""
    now_ts = int(time.time())
    shipped_at = now_ts - (strategist.MATURITY_DAYS * 86400) - 3600
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"

    record = _shipped_record(
        "X", {"some_custom_metric": 5.0},
        metric_key="some_custom_metric", shipped_at=shipped_at
    )
    outcomes_path.write_text(json.dumps([record]), encoding="utf-8")

    with kb.connect() as conn:
        strategist.reflect(
            conn, since=0, notes_path=notes_path, outcomes_path=outcomes_path,
            metrics={"generated_at": now_ts, "some_custom_metric": 7.0}, now=float(now_ts),
        )
    rec = json.loads(outcomes_path.read_text(encoding="utf-8"))[0]
    assert rec["verdict"] == "unknown"


def test_verdict_null_when_metric_key_is_none(board_home, tmp_path):
    """No metric_key → verdict stays None (direction cannot be determined)."""
    now_ts = int(time.time())
    shipped_at = now_ts - (strategist.MATURITY_DAYS * 86400) - 3600
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"

    record = _shipped_record(
        "X", {"autonomy_pct": 70.0}, metric_key=None, shipped_at=shipped_at
    )
    outcomes_path.write_text(json.dumps([record]), encoding="utf-8")

    with kb.connect() as conn:
        strategist.reflect(
            conn, since=0, notes_path=notes_path, outcomes_path=outcomes_path,
            metrics={"generated_at": now_ts, "autonomy_pct": 80.0}, now=float(now_ts),
        )
    rec = json.loads(outcomes_path.read_text(encoding="utf-8"))[0]
    assert rec["verdict"] is None


# --------------------------------------------------------------------------- #
# 5. gather_context + dry-run JSON
# --------------------------------------------------------------------------- #
def test_gather_context_returns_lever_outcomes(board_home):
    """gather_context() includes lever_outcomes when outcomes_path is provided."""
    now_ts = int(time.time())
    outcomes_path = board_home / ".hermes" / "lever-outcomes.json"
    outcomes_path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "schema_version": 1, "lever_key": "HEILER-TRANSIENT",
            "root_task_id": 1, "proposed_at": now_ts - 86400,
            "baseline": {"autonomy_pct": 75.0}, "metric_key": "autonomy_pct",
            "shipped_at": None, "measured_at": None,
            "current": None, "delta": None, "verdict": None, "status": "proposed",
        },
        {
            "schema_version": 1, "lever_key": "AUTON-UPLIFT",
            "root_task_id": 2, "proposed_at": now_ts - 2 * 86400,
            "baseline": {"autonomy_pct": 70.0}, "metric_key": "autonomy_pct",
            "shipped_at": now_ts - 86400, "measured_at": now_ts - 3600,
            "current": {"autonomy_pct": 85.0}, "delta": {"autonomy_pct": 15.0},
            "verdict": "improved", "status": "measured",
        },
    ]
    outcomes_path.write_text(json.dumps(records), encoding="utf-8")

    with kb.connect() as conn:
        ctx = strategist.gather_context(conn, outcomes_path=outcomes_path)

    assert "lever_outcomes" in ctx
    lo = ctx["lever_outcomes"]
    assert isinstance(lo, list)
    assert len(lo) == 2
    for item in lo:
        assert "lever_key" in item
        assert "status" in item
        assert "verdict" in item
        assert "metric_key" in item
        assert "proposed_at" in item


def test_dry_run_json_contains_lever_outcomes(board_home, monkeypatch, tmp_path):
    """propose(do_ingest=False) result carries lever_outcomes from the outcomes file."""
    _patch_budget(monkeypatch, 30.0)
    with kb.connect() as conn:
        _seed_ledger(conn, "dirty-overlap git lock contention")

    now_ts = int(time.time())
    outcomes_path = tmp_path / "lever-outcomes.json"
    outcomes_path.write_text(json.dumps([
        {
            "schema_version": 1, "lever_key": "OLD-LEVER", "root_task_id": 1,
            "proposed_at": now_ts - 86400, "baseline": {"autonomy_pct": 70.0},
            "metric_key": "autonomy_pct", "shipped_at": None, "measured_at": None,
            "current": None, "delta": None, "verdict": None, "status": "proposed",
        }
    ]), encoding="utf-8")

    result = strategist.propose(
        board=None,
        out_dir=board_home / "specs",
        metrics=_BASE_METRICS,
        outcomes_path=outcomes_path,
        do_ingest=False,
    )

    assert "lever_outcomes" in result
    assert isinstance(result["lever_outcomes"], list)
    assert len(result["lever_outcomes"]) == 1
    assert result["lever_outcomes"][0]["lever_key"] == "OLD-LEVER"


# --------------------------------------------------------------------------- #
# 6. Atomicity — write via tmp+rename
# --------------------------------------------------------------------------- #
def test_atomic_write_uses_os_replace(board_home, monkeypatch, tmp_path):
    """Outcomes writes go through os.replace (tmp+rename), not direct open+write."""
    replaced: list[tuple] = []
    real_replace = os.replace

    def _spy_replace(src, dst):
        replaced.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy_replace)
    _patch_budget(monkeypatch, 30.0)

    with kb.connect() as conn:
        _seed_ledger(conn, "dirty-overlap git lock contention")

    outcomes_path = tmp_path / "lever-outcomes.json"
    strategist.propose(
        board=None, out_dir=board_home / "specs",
        metrics=_BASE_METRICS, outcomes_path=outcomes_path,
    )

    # at least one os.replace call whose destination is the outcomes file
    assert any(dst == str(outcomes_path) for (_, dst) in replaced), (
        f"No os.replace to {outcomes_path}; calls: {replaced}"
    )


# --------------------------------------------------------------------------- #
# 7. stale_metrics flag
# --------------------------------------------------------------------------- #
def test_stale_metrics_flagged_when_generated_at_is_old(board_home, tmp_path):
    """If generated_at is > 24 h old, still measure but stale_metrics=True."""
    now_ts = int(time.time())
    shipped_at = now_ts - (strategist.MATURITY_DAYS * 86400) - 3600
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"

    record = _shipped_record(
        "X", {"autonomy_pct": 70.0}, metric_key="autonomy_pct", shipped_at=shipped_at
    )
    outcomes_path.write_text(json.dumps([record]), encoding="utf-8")

    stale_metrics = {
        "generated_at": now_ts - (25 * 3600),  # 25 hours ago → stale
        "autonomy_pct": 82.0,
    }
    with kb.connect() as conn:
        strategist.reflect(
            conn, since=0, notes_path=notes_path, outcomes_path=outcomes_path,
            metrics=stale_metrics, now=float(now_ts),
        )

    rec = json.loads(outcomes_path.read_text(encoding="utf-8"))[0]
    assert rec["status"] == "measured"
    assert rec.get("stale_metrics") is True


def test_non_stale_metrics_no_stale_flag(board_home, tmp_path):
    """Fresh metrics (generated_at < 24 h ago) must NOT set stale_metrics."""
    now_ts = int(time.time())
    shipped_at = now_ts - (strategist.MATURITY_DAYS * 86400) - 3600
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"

    record = _shipped_record(
        "X", {"autonomy_pct": 70.0}, metric_key="autonomy_pct", shipped_at=shipped_at
    )
    outcomes_path.write_text(json.dumps([record]), encoding="utf-8")

    fresh_metrics = {
        "generated_at": now_ts - 3600,  # 1 hour ago → fresh
        "autonomy_pct": 80.0,
    }
    with kb.connect() as conn:
        strategist.reflect(
            conn, since=0, notes_path=notes_path, outcomes_path=outcomes_path,
            metrics=fresh_metrics, now=float(now_ts),
        )

    rec = json.loads(outcomes_path.read_text(encoding="utf-8"))[0]
    assert rec["status"] == "measured"
    assert rec.get("stale_metrics") is not True


def test_stale_metrics_with_iso_generated_at_regression(board_home, tmp_path):
    """Das echte vision-metrics.json schreibt ISO-8601-Strings als generated_at
    (z.B. "2026-07-02T04:00:50+00:00"). Die Messung darf daran nicht crashen
    (Regression: int() auf ISO-String → ValueError) und muss stale korrekt flaggen."""
    from datetime import datetime, timedelta, timezone

    now_ts = int(time.time())
    shipped_at = now_ts - (strategist.MATURITY_DAYS * 86400) - 3600
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"

    record = _shipped_record(
        "X", {"autonomy_pct": 70.0}, metric_key="autonomy_pct", shipped_at=shipped_at
    )
    outcomes_path.write_text(json.dumps([record]), encoding="utf-8")

    stale_iso = (
        datetime.fromtimestamp(now_ts, tz=timezone.utc) - timedelta(hours=25)
    ).isoformat()
    metrics = {"generated_at": stale_iso, "autonomy_pct": 82.0}
    with kb.connect() as conn:
        strategist.reflect(
            conn, since=0, notes_path=notes_path, outcomes_path=outcomes_path,
            metrics=metrics, now=float(now_ts),
        )

    rec = json.loads(outcomes_path.read_text(encoding="utf-8"))[0]
    assert rec["status"] == "measured"
    assert rec.get("stale_metrics") is True


def test_verdict_resolves_fully_qualified_flat_metric_key():
    """Echte geflattete Pfade (autonomy.autonomy_pct etc.) müssen via
    Basename-Fallback auf die Richtungs-Map aufgelöst werden — sonst wären
    4 von 5 Kern-Metriken immer 'unknown' (Regression)."""
    assert strategist._compute_verdict(2.0, "autonomy.autonomy_pct") == "improved"
    assert strategist._compute_verdict(3.0, "escalation_rate.escalations_per_week") == "worsened"
    assert strategist._compute_verdict(1.0, "green_gate_streak.streak") == "improved"
    assert strategist._compute_verdict(-2.0, "green_gate_streak.fail_nights") == "improved"
    assert strategist._compute_verdict(-0.01, "cost_per_task.recent_avg_cost_per_task") == "improved"
    assert strategist._compute_verdict(1.0, "voellig.unbekannter_pfad") == "unknown"


def test_flatten_unwraps_h1_wrapper_shape():
    """Das echte vision-metrics.json ist ein Wrapper {schema_version, generated_at,
    generated_epoch, window_days, metrics: {...}} — Flatten muss die inneren
    Metrik-Pfade ohne 'metrics.'-Präfix und ohne Meta-Felder liefern (Regression:
    Wrapper-Flatten ergab metrics.autonomy.autonomy_pct + schema_version-Müll)."""
    wrapper = {
        "schema_version": 3,
        "generated_at": "2026-07-02T04:00:50+00:00",
        "generated_epoch": 1_782_957_650,
        "window_days": 14,
        "metrics": {"autonomy": {"autonomy_pct": 81.1}, "green_gate_streak": {"streak": 0}},
    }
    flat = strategist._flatten_numeric(strategist._metrics_payload(wrapper))
    assert flat == {"autonomy.autonomy_pct": 81.1, "green_gate_streak.streak": 0.0}


def test_measurement_with_h1_wrapper_yields_delta_and_verdict(board_home, tmp_path):
    """E2E über reflect(): Wrapper-förmige Metriken → Delta auf sauberem Pfad
    + verdict via Richtungs-Map (improved bei autonomy-Anstieg)."""
    now_ts = int(time.time())
    shipped_at = now_ts - (strategist.MATURITY_DAYS * 86400) - 3600
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"

    record = _shipped_record(
        "X", {"autonomy.autonomy_pct": 70.0},
        metric_key="autonomy.autonomy_pct", shipped_at=shipped_at,
    )
    outcomes_path.write_text(json.dumps([record]), encoding="utf-8")

    wrapper = {
        "schema_version": 3,
        "generated_at": now_ts - 3600,
        "metrics": {"autonomy": {"autonomy_pct": 81.5}},
    }
    with kb.connect() as conn:
        strategist.reflect(
            conn, since=0, notes_path=notes_path, outcomes_path=outcomes_path,
            metrics=wrapper, now=float(now_ts),
        )

    rec = json.loads(outcomes_path.read_text(encoding="utf-8"))[0]
    assert rec["status"] == "measured"
    assert rec["delta"]["autonomy.autonomy_pct"] == pytest.approx(11.5)
    assert rec["verdict"] == "improved"


def test_unparseable_generated_at_measures_without_flag(board_home, tmp_path):
    """Unparsebares generated_at → Messung läuft durch, stale-Flag wird nur übersprungen."""
    now_ts = int(time.time())
    shipped_at = now_ts - (strategist.MATURITY_DAYS * 86400) - 3600
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"

    record = _shipped_record(
        "X", {"autonomy_pct": 70.0}, metric_key="autonomy_pct", shipped_at=shipped_at
    )
    outcomes_path.write_text(json.dumps([record]), encoding="utf-8")

    metrics = {"generated_at": "kein-zeitstempel", "autonomy_pct": 82.0}
    with kb.connect() as conn:
        strategist.reflect(
            conn, since=0, notes_path=notes_path, outcomes_path=outcomes_path,
            metrics=metrics, now=float(now_ts),
        )

    rec = json.loads(outcomes_path.read_text(encoding="utf-8"))[0]
    assert rec["status"] == "measured"
    assert rec.get("stale_metrics") is not True


# --------------------------------------------------------------------------- #
# 8. Existing reflect() tests must still pass (non-regression)
# --------------------------------------------------------------------------- #
def test_reflect_without_outcomes_path_is_backward_compatible(board_home, tmp_path):
    """reflect() without outcomes_path must work exactly as before — outcomes key
    still present in note (with zeros) so callers can read it safely."""
    notes_path = tmp_path / "reflections.jsonl"
    with kb.connect() as conn:
        result = strategist.reflect(conn, since=0, notes_path=notes_path)

    assert result["note"]["outcomes"] == {"shipped_stamped": 0, "measured": 0}
