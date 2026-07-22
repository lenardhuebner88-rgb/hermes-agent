"""Tests for the distilled vision-metrics CLIs (hermes_cli.vision_metrics).

Covers ``hermes vision metrics-snapshot`` and ``hermes vision
record-gate-result``: the precomputed metric distillation written to
``~/.hermes/state/vision-metrics.json`` plus the structured green-gate
ledger the streak is derived from.

Every test writes to a TEMP state dir (``HERMES_VISION_STATE_DIR``) and an
isolated kanban DB — never the live state.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import vision_metrics as vm

DAY = 86_400


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect the vision state dir to a temp path (never the live state)."""
    d = tmp_path / "state"
    monkeypatch.setenv("HERMES_VISION_STATE_DIR", str(d))
    return d


@pytest.fixture
def conn(tmp_path):
    """An isolated kanban DB connection."""
    c = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        yield c
    finally:
        c.close()


def _add_task(conn, tid, *, status="done", consecutive_failures=0,
              completed_at=None, created_at=1_000):
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, completed_at, "
        "consecutive_failures) VALUES (?, ?, ?, ?, ?, ?)",
        (tid, f"task {tid}", status, created_at, completed_at,
         consecutive_failures),
    )
    conn.commit()


def _add_event(conn, tid, kind, *, payload=None, created_at=1_000):
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (tid, kind, json.dumps(payload) if payload else None, created_at),
    )
    conn.commit()


def _add_run(conn, tid, *, cost_usd=None, started_at=1_000, ended_at=1_000,
             status="done", outcome=None):
    conn.execute(
        "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at, "
        "cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
        (tid, status, outcome, started_at, ended_at, cost_usd),
    )
    conn.commit()


def _add_blocked_run(conn, tid, *, summary="boom", ended_at):
    """A finished blocked run the auto-retry lane keys off of."""
    conn.execute(
        "INSERT INTO task_runs (task_id, status, outcome, started_at, "
        "ended_at, summary) VALUES (?, 'blocked', 'blocked', ?, ?, ?)",
        (tid, ended_at - 10, ended_at, summary),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Autonomy metric + paired counter
# ---------------------------------------------------------------------------

def test_autonomy_percent_and_counter(conn):
    now = 100 * DAY
    # A: clean autonomous done
    _add_task(conn, "A", consecutive_failures=0, completed_at=now - DAY)
    # B: done but had a failed run -> not autonomous
    _add_task(conn, "B", consecutive_failures=0, completed_at=now - DAY)
    _add_run(conn, "B", outcome="timed_out")
    # C: done but operator escalated -> not autonomous
    _add_task(conn, "C", consecutive_failures=0, completed_at=now - DAY)
    _add_event(conn, "C", kb.OPERATOR_ESCALATION_EVENT, created_at=now - DAY)
    # D: counted autonomous BUT heiler saw a real bug -> counter +1
    _add_task(conn, "D", consecutive_failures=0, completed_at=now - DAY)
    _add_event(conn, "D", kb.HEILER_CLASSIFICATION_EVENT,
               payload={"class": kb.HEILER_CLASS_REAL_BUG}, created_at=now - DAY)
    # E: not done -> ignored entirely
    _add_task(conn, "E", status="ready", completed_at=None)

    snap = vm.compute_metrics_snapshot(conn, now=now)
    a = snap["metrics"]["autonomy"]

    assert a["total_done"] == 4
    assert a["autonomous_done"] == 2  # A and D
    assert a["autonomy_pct"] == 50.0
    assert a["counter"]["name"] == "should_have_escalated_but_didnt"
    assert a["counter"]["value"] == 1  # D


def test_autonomy_excludes_done_tasks_with_failed_runs(conn):
    now = 100 * DAY
    _add_task(conn, "A", completed_at=now - DAY)
    _add_run(conn, "A", outcome="completed")
    _add_task(conn, "B", completed_at=now - DAY)
    _add_run(conn, "B", outcome="gave_up")

    snap = vm.compute_metrics_snapshot(conn, now=now)
    a = snap["metrics"]["autonomy"]

    assert a["total_done"] == 2
    assert a["autonomous_done"] == 1
    assert a["autonomy_pct"] == 50.0


def test_autonomy_percent_null_when_no_done(conn):
    snap = vm.compute_metrics_snapshot(conn, now=100 * DAY)
    a = snap["metrics"]["autonomy"]
    assert a["total_done"] == 0
    assert a["autonomy_pct"] is None


# ---------------------------------------------------------------------------
# Escalation-rate metric + paired counter
# ---------------------------------------------------------------------------

def test_escalation_rate_counts_distinct_tasks_not_events(conn):
    now = 100 * DAY
    _add_task(conn, "T1", status="blocked")
    _add_task(conn, "T2", status="blocked")

    # Four escalation events in the window, but only two distinct tasks.
    _add_event(conn, "T1", kb.OPERATOR_ESCALATION_EVENT, created_at=now - DAY)
    _add_event(conn, "T1", kb.OPERATOR_ESCALATION_EVENT, created_at=now - DAY + 1)
    _add_event(conn, "T1", kb.OPERATOR_ESCALATION_EVENT, created_at=now - DAY + 2)
    _add_event(conn, "T2", kb.OPERATOR_ESCALATION_EVENT, created_at=now - 2 * DAY)

    snap = vm.compute_metrics_snapshot(conn, now=now, window_days=7)

    assert snap["metrics"]["escalation_rate"]["escalations_per_week"] == 2


def test_escalation_rate_window_and_silent_blocks_counter(conn):
    now = 100 * DAY
    _add_task(conn, "T1", status="done", completed_at=now - DAY)
    _add_task(conn, "T2", status="done", completed_at=now - DAY)
    # two escalations inside the 7d window
    _add_event(conn, "T1", kb.OPERATOR_ESCALATION_EVENT, created_at=now - DAY)
    _add_event(conn, "T2", kb.OPERATOR_ESCALATION_EVENT, created_at=now - 2 * DAY)
    # one escalation OUTSIDE the window (10 days ago)
    _add_event(conn, "T1", kb.OPERATOR_ESCALATION_EVENT, created_at=now - 10 * DAY)
    # a blocked task with no escalation -> silent block
    _add_task(conn, "B1", status="blocked", completed_at=None)
    # a blocked task WITH an escalation -> not silent
    _add_task(conn, "B2", status="blocked", completed_at=None)
    _add_event(conn, "B2", kb.OPERATOR_ESCALATION_EVENT, created_at=now - DAY)

    snap = vm.compute_metrics_snapshot(conn, now=now, window_days=7)
    e = snap["metrics"]["escalation_rate"]

    # 4 escalation events total; the 10-days-ago one falls outside the 7d
    # window. The remaining three (T1, T2, and the blocked B2) are counted.
    assert e["escalations_per_week"] == 3
    assert e["window_days"] == 7
    assert e["counter"]["name"] == "silent_blocks"
    assert e["counter"]["value"] == 1  # B1 (settled: no blocked run to retry)


def test_silent_blocks_settled_counts_then_zeroed_by_guard_sweep(conn):
    """A settled block is silent until the guard sweep surfaces it; afterwards
    the metric reads 0 — the guard drives silent_blocks to 0 (AC-1)."""
    now = 100 * DAY
    _add_task(conn, "S1", status="blocked", completed_at=None)  # no blocked run

    before = vm.compute_metrics_snapshot(conn, now=now)
    assert before["metrics"]["escalation_rate"]["counter"]["value"] == 1

    kb.escalate_silent_blocks_sweep(conn, now=now)

    after = vm.compute_metrics_snapshot(conn, now=now)
    assert after["metrics"]["escalation_rate"]["counter"]["value"] == 0


def test_silent_blocks_excludes_transient_self_healing_retry(conn):
    """A retryable block the auto-retry lane is still working is NOT silent and
    is NOT escalated by the guard — transient retries must not flood the
    operator (AC-2)."""
    now = 100 * DAY
    _add_task(conn, "TR", status="blocked", completed_at=None)
    _add_blocked_run(conn, "TR", summary="transient MCP unavailable",
                     ended_at=now - 60)

    snap = vm.compute_metrics_snapshot(conn, now=now)
    assert snap["metrics"]["escalation_rate"]["counter"]["value"] == 0

    res = kb.escalate_silent_blocks_sweep(conn, now=now)
    assert res["escalated"] == []


# ---------------------------------------------------------------------------
# Classification-coverage metric (HEILER-CLASSIFY-COVERAGE-S1) + corrected counter
# ---------------------------------------------------------------------------

def _add_escalation(conn, tid, *, created_at):
    cur = conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (tid, kb.OPERATOR_ESCALATION_EVENT,
         json.dumps({"why_now": "x"}), created_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def _add_classification(conn, tid, *, escalation_event_id, created_at,
                        cls=kb.HEILER_CLASS_REAL_BUG):
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (tid, kb.HEILER_CLASSIFICATION_EVENT,
         json.dumps({"class": cls,
                     "escalation_event_id": escalation_event_id}),
         created_at),
    )
    conn.commit()


def test_classification_coverage_full_within_24h(conn):
    now = 100 * DAY
    _add_task(conn, "T1", status="blocked")
    _add_task(conn, "T2", status="blocked")
    e1 = _add_escalation(conn, "T1", created_at=now - DAY)
    e2 = _add_escalation(conn, "T2", created_at=now - 2 * DAY)
    # both classified at the same instant as the escalation (the sweep runs
    # every dispatcher tick → seconds, far inside 24h)
    _add_classification(conn, "T1", escalation_event_id=e1, created_at=now - DAY)
    _add_classification(conn, "T2", escalation_event_id=e2,
                        created_at=now - 2 * DAY)

    snap = vm.compute_metrics_snapshot(conn, now=now, window_days=7)
    c = snap["metrics"]["classification_coverage"]

    assert c["escalations"] == 2
    assert c["classified_within_24h"] == 2
    assert c["coverage_pct"] == 100.0
    assert c["counter"]["name"] == "operator_corrected_pct"
    assert c["counter"]["value"] == 0.0


def test_classification_coverage_excludes_late_and_unclassified(conn):
    now = 100 * DAY
    _add_task(conn, "T1", status="blocked")
    _add_task(conn, "T2", status="blocked")
    _add_task(conn, "T3", status="blocked")
    e1 = _add_escalation(conn, "T1", created_at=now - DAY)
    e2 = _add_escalation(conn, "T2", created_at=now - 3 * DAY)
    _add_escalation(conn, "T3", created_at=now - DAY)  # never classified
    # T1: classified within 24h -> covered
    _add_classification(conn, "T1", escalation_event_id=e1, created_at=now - DAY)
    # T2: classified 2 days after the escalation -> outside the 24h bound
    _add_classification(conn, "T2", escalation_event_id=e2,
                        created_at=now - DAY)

    snap = vm.compute_metrics_snapshot(conn, now=now, window_days=7)
    c = snap["metrics"]["classification_coverage"]

    assert c["escalations"] == 3
    assert c["classified_within_24h"] == 1
    assert c["coverage_pct"] == 33.3


def test_classification_coverage_counter_counts_operator_corrections(conn):
    now = 100 * DAY
    _add_task(conn, "T1", status="blocked")
    _add_task(conn, "T2", status="blocked")
    e1 = _add_escalation(conn, "T1", created_at=now - DAY)
    e2 = _add_escalation(conn, "T2", created_at=now - DAY)
    _add_classification(conn, "T1", escalation_event_id=e1, created_at=now - DAY)
    _add_classification(conn, "T2", escalation_event_id=e2, created_at=now - DAY)
    # operator corrected one of the two classified escalations
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("T1", kb.HEILER_CLASSIFICATION_CORRECTED_EVENT,
         json.dumps({"escalation_event_id": e1,
                     "corrected_to": kb.HEILER_CLASS_BAD_SPEC}),
         now - DAY),
    )
    conn.commit()

    snap = vm.compute_metrics_snapshot(conn, now=now, window_days=7)
    c = snap["metrics"]["classification_coverage"]

    assert c["coverage_pct"] == 100.0
    assert c["counter"]["value"] == 50.0  # 1 of 2 classified escalations


def test_classification_coverage_null_when_no_escalations(conn):
    snap = vm.compute_metrics_snapshot(conn, now=100 * DAY, window_days=7)
    c = snap["metrics"]["classification_coverage"]
    assert c["escalations"] == 0
    assert c["coverage_pct"] is None
    assert c["counter"]["value"] == 0.0


def test_classification_coverage_full_from_inline_park_without_sweep(conn):
    """ESCALATION-INLINE-CLASSIFY-S1 (AC-1): the budget-runaway park now classifies
    INLINE, so the coverage metric reads 100% the instant the escalation is
    written — no classify_escalations_sweep poll in between. This is the atomic,
    immediately-complete cause-evidence the Stratege gets instead of a
    poll-dependent approximation."""
    import time

    tid = kb.create_task(conn, title="runaway loop", assignee="coder")
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    kb._park_budget_runaway(conn, row, token_sum=5000, cap=1000, runs=3)
    # Deliberately do NOT run any sweep.

    snap = vm.compute_metrics_snapshot(
        conn, now=int(time.time()) + 1, window_days=7,
    )
    c = snap["metrics"]["classification_coverage"]

    assert c["escalations"] == 1
    assert c["classified_within_24h"] == 1
    assert c["coverage_pct"] == 100.0
    assert c["counter"]["value"] == 0.0


def test_classification_coverage_full_from_silent_block_sweep_without_classify(conn):
    """ESCALATION-INLINE-CLASSIFY-S1 (AC-1): the silent-block sweep now pairs a
    heiler_classification at the escalation site, so the coverage metric reads
    100% after escalate_silent_blocks_sweep alone — without the separate
    classify_escalations_sweep poll that used to be the only classifier for this
    path. Closes the last escalation writer without inline classification."""
    import time

    # A settled block (no blocked run) the self-healing lane will not move.
    _add_task(conn, "SB", status="blocked")
    kb.escalate_silent_blocks_sweep(conn)
    # Deliberately do NOT run classify_escalations_sweep.

    snap = vm.compute_metrics_snapshot(
        conn, now=int(time.time()) + 1, window_days=7,
    )
    c = snap["metrics"]["classification_coverage"]

    assert c["escalations"] == 1
    assert c["classified_within_24h"] == 1
    assert c["coverage_pct"] == 100.0
    assert c["counter"]["value"] == 0.0


# ---------------------------------------------------------------------------
# Cost-per-task metric + paired counter
# ---------------------------------------------------------------------------

def test_cost_per_task_sum_trend_and_counter(conn):
    now = 100 * DAY
    # recent window (completed in last 7d): two runs on one task = 0.30
    _add_task(conn, "R1", status="done", completed_at=now - DAY)
    _add_run(conn, "R1", cost_usd=0.10)
    _add_run(conn, "R1", cost_usd=0.20)
    # prior window (8-14d ago): one task, 0.10
    _add_task(conn, "P1", status="done", completed_at=now - 9 * DAY)
    _add_run(conn, "P1", cost_usd=0.10)
    # done task with NO cost data -> counter
    _add_task(conn, "N1", status="done", completed_at=now - DAY)

    snap = vm.compute_metrics_snapshot(conn, now=now, window_days=7)
    c = snap["metrics"]["cost_per_task"]

    assert c["cost_usd_total"] == pytest.approx(0.40)
    assert c["tasks_with_cost"] == 2  # R1, P1
    assert c["recent_avg_cost_per_task"] == pytest.approx(0.30)
    assert c["prior_avg_cost_per_task"] == pytest.approx(0.10)
    assert c["trend"] == "up"
    assert c["counter"]["name"] == "tasks_without_cost_data"
    assert c["counter"]["value"] == 1  # N1
    # coverage breakdown: 3 done, 2 metered, 0 subscription, 1 NULL
    cov = c["coverage"]
    assert cov["total_done"] == 3
    assert cov["with_metered_cost"] == 2
    assert cov["subscription_only"] == 0
    assert cov["no_cost_data"] == 1
    assert cov["coverage_pct"] == pytest.approx(66.7)


def test_cost_metric_excludes_subscription_zero_from_average(conn):
    """AC-1: subscription-stamped $0 runs are NOT averaged in (no phantom
    drop). They are surfaced as explicit coverage, not as a $0 saving."""
    now = 100 * DAY
    # prior window: a real metered task @ 0.20
    _add_task(conn, "P", status="done", completed_at=now - 9 * DAY)
    _add_run(conn, "P", cost_usd=0.20)
    # recent window: a real metered task @ 0.30 ...
    _add_task(conn, "M", status="done", completed_at=now - DAY)
    _add_run(conn, "M", cost_usd=0.30)
    # ... plus a subscription-stamped task (two runs, both $0 -> rides quota)
    _add_task(conn, "S", status="done", completed_at=now - DAY)
    _add_run(conn, "S", cost_usd=0.0)
    _add_run(conn, "S", cost_usd=0.0)

    c = vm.compute_metrics_snapshot(conn, now=now, window_days=7)[
        "metrics"]["cost_per_task"]

    # average over metered (>0) tasks ONLY -> 0.30, NOT (0.30 + 0.0)/2 = 0.15
    assert c["recent_avg_cost_per_task"] == pytest.approx(0.30)
    assert c["prior_avg_cost_per_task"] == pytest.approx(0.20)
    assert c["trend"] == "up"
    assert c["pct_change"] == pytest.approx(50.0)
    # S is subscription coverage, not a metered task
    assert c["tasks_with_cost"] == 2  # P, M
    assert c["coverage"]["subscription_only"] == 1
    assert c["coverage"]["with_metered_cost"] == 2
    # cost_usd_total unchanged by the $0 stamp (AC-2 guardrail)
    assert c["cost_usd_total"] == pytest.approx(0.50)


def test_cost_metric_all_subscription_recent_is_na_not_minus_100(conn):
    """AC-1: a recent window that is *only* subscription-$0 work reports n/a,
    never the misleading -100% 'savings' the old average manufactured."""
    now = 100 * DAY
    # prior window: real metered 0.20
    _add_task(conn, "P", status="done", completed_at=now - 9 * DAY)
    _add_run(conn, "P", cost_usd=0.20)
    # recent window: ONLY a subscription-stamped task ($0)
    _add_task(conn, "S1", status="done", completed_at=now - DAY)
    _add_run(conn, "S1", cost_usd=0.0)

    c = vm.compute_metrics_snapshot(conn, now=now, window_days=7)[
        "metrics"]["cost_per_task"]

    # the old code averaged 0.0 -> pct_change = -100.0; the honest answer is n/a
    assert c["recent_avg_cost_per_task"] is None
    assert c["trend"] == "n/a"
    assert c["trend_basis"] == "insufficient_metered_data"
    assert c["pct_change"] is None
    assert c["coverage"]["subscription_only"] == 1


def test_cost_metric_empty_prior_window_reports_insufficient_trend_basis(conn):
    """A missing prior metered window makes the trend explicitly data-limited."""
    now = 100 * DAY
    _add_task(conn, "M", status="done", completed_at=now - DAY)
    _add_run(conn, "M", cost_usd=0.30)
    _add_task(conn, "S", status="done", completed_at=now - 9 * DAY)
    _add_run(conn, "S", cost_usd=0.0)

    c = vm.compute_metrics_snapshot(conn, now=now, window_days=7)[
        "metrics"]["cost_per_task"]

    assert c["prior_avg_cost_per_task"] is None
    assert c["trend"] == "n/a"
    assert c["trend_basis"] == "insufficient_metered_data"


def test_coverage_counter_unmoved_by_subscription_stamp(conn):
    """AC-2: the coverage counter shrinks ONLY when a task gains real metered
    cost — never merely because a NULL run was stamped $0 (subscription)."""
    now = 100 * DAY
    _add_task(conn, "T", status="done", completed_at=now - DAY)
    _add_run(conn, "T", cost_usd=None)  # NULL: no cost data at all

    def _cost():
        return vm.compute_metrics_snapshot(conn, now=now, window_days=7)[
            "metrics"]["cost_per_task"]

    c0 = _cost()
    assert c0["counter"]["value"] == 1
    assert c0["coverage"]["no_cost_data"] == 1
    assert c0["coverage"]["subscription_only"] == 0
    assert c0["tasks_with_cost"] == 0

    # subscription stamp: NULL -> 0.0. Coverage MUST NOT improve.
    conn.execute("UPDATE task_runs SET cost_usd = 0.0 WHERE task_id = 'T'")
    conn.commit()
    c1 = _cost()
    assert c1["counter"]["value"] == 1  # still blind to real consumption
    assert c1["coverage"]["subscription_only"] == 1
    assert c1["coverage"]["no_cost_data"] == 0
    assert c1["tasks_with_cost"] == 0  # NOT magically covered

    # real metered stamp: 0.0 -> 0.15. NOW coverage genuinely improves.
    conn.execute("UPDATE task_runs SET cost_usd = 0.15 WHERE task_id = 'T'")
    conn.commit()
    c2 = _cost()
    assert c2["counter"]["value"] == 0
    assert c2["coverage"]["subscription_only"] == 0
    assert c2["tasks_with_cost"] == 1


# ---------------------------------------------------------------------------
# Green-gate streak derivation
# ---------------------------------------------------------------------------

def _rec(date, result, *, ts=None):
    return {"date": date, "result": result, "ts": ts or f"{date}T03:00:00+00:00"}


def test_streak_counts_consecutive_green_nights():
    records = [
        _rec("2026-06-15", "pass"),
        _rec("2026-06-16", "fail"),
        _rec("2026-06-17", "pass"),
        _rec("2026-06-18", "pass"),
        _rec("2026-06-19", "pass"),
    ]
    out = vm.derive_gate_streak(records)
    assert out["streak"] == 3  # 19, 18, 17
    assert out["green_nights"] == 4
    assert out["fail_nights"] == 1
    assert out["last_result"] == "pass"


def test_streak_zero_when_latest_night_failed():
    records = [_rec("2026-06-18", "pass"), _rec("2026-06-19", "fail")]
    out = vm.derive_gate_streak(records)
    assert out["streak"] == 0
    assert out["last_result"] == "fail"


def test_streak_empty_ledger():
    out = vm.derive_gate_streak([])
    assert out["streak"] == 0
    assert out["green_nights"] == 0
    assert out["last_result"] is None


def test_streak_night_with_any_fail_is_red():
    # same date, one pass one fail -> the night is red
    records = [
        _rec("2026-06-19", "pass", ts="2026-06-19T03:00:00+00:00"),
        _rec("2026-06-19", "fail", ts="2026-06-19T04:00:00+00:00"),
    ]
    out = vm.derive_gate_streak(records)
    assert out["streak"] == 0
    assert out["fail_nights"] == 1
    assert out["green_nights"] == 0


# ---------------------------------------------------------------------------
# red_streak_from_head (S3: release.pause_on_red_streak's ledger-side check)
# ---------------------------------------------------------------------------

def test_red_streak_from_head_counts_consecutive_red_nights():
    records = [
        _rec("2026-06-17", "pass"),
        _rec("2026-06-18", "fail"),
        _rec("2026-06-19", "fail"),
        _rec("2026-06-20", "fail"),
    ]
    assert vm.red_streak_from_head(records) == 3


def test_red_streak_from_head_zero_when_head_green():
    records = [_rec("2026-06-19", "fail"), _rec("2026-06-20", "pass")]
    assert vm.red_streak_from_head(records) == 0


def test_red_streak_from_head_empty_ledger():
    assert vm.red_streak_from_head([]) == 0


# ---------------------------------------------------------------------------
# Recurring same-cause red detection (GREEN-GATE-AUTOHEAL-LOOP-S1)
# ---------------------------------------------------------------------------

def _red(date, *, gate=None, detail=None, ts=None):
    rec = {"date": date, "result": "fail", "ts": ts or f"{date}T03:00:00+00:00"}
    ff = {}
    if gate is not None:
        ff["gate"] = gate
    if detail is not None:
        ff["detail"] = detail
    if ff:
        rec["first_fail"] = ff
    return rec


def test_red_cause_none_when_head_is_green():
    records = [
        _red("2026-06-18", gate="python", detail="boom"),
        _red("2026-06-19", gate="python", detail="boom"),
        _rec("2026-06-20", "pass"),
    ]
    assert vm.derive_consecutive_red_cause(records) is None


def test_red_cause_none_on_single_red_night():
    records = [_rec("2026-06-19", "pass"), _red("2026-06-20", gate="python", detail="boom")]
    assert vm.derive_consecutive_red_cause(records) is None


def test_red_cause_fires_on_two_consecutive_same_cause():
    records = [
        _red("2026-06-20", gate="python", detail="E assert foo == bar in test_x"),
        _red("2026-06-21", gate="python", detail="E assert foo == bar in test_x"),
    ]
    cause = vm.derive_consecutive_red_cause(records)
    assert cause is not None
    assert cause["gate"] == "python"
    assert cause["red_nights"] == 2
    assert cause["dates"] == ["2026-06-21", "2026-06-20"]
    assert cause["fingerprint"].startswith("python|")


def test_red_cause_fingerprint_stable_across_volatile_detail():
    # same root cause but the tail varies (line numbers, counts, paths) night to
    # night -> the normalized fingerprint must still match so the cause coalesces.
    records = [
        _red("2026-06-20", gate="python", detail="E assert 1 == 2 at /a/b/test_x.py:41"),
        _red("2026-06-21", gate="python", detail="E assert 7 == 9 at /a/b/test_x.py:88"),
    ]
    cause = vm.derive_consecutive_red_cause(records)
    assert cause is not None
    assert cause["red_nights"] == 2


def test_red_cause_none_when_gates_differ():
    records = [
        _red("2026-06-20", gate="python", detail="boom"),
        _red("2026-06-21", gate="vitest", detail="boom"),
    ]
    assert vm.derive_consecutive_red_cause(records) is None


def test_red_cause_none_when_normalized_detail_differs():
    records = [
        _red("2026-06-20", gate="python", detail="assertion error in test_alpha"),
        _red("2026-06-21", gate="python", detail="import error in module_beta"),
    ]
    assert vm.derive_consecutive_red_cause(records) is None


def test_red_cause_counts_three_consecutive():
    records = [
        _red("2026-06-19", gate="build", detail="tsc error"),
        _red("2026-06-20", gate="build", detail="tsc error"),
        _red("2026-06-21", gate="build", detail="tsc error"),
    ]
    cause = vm.derive_consecutive_red_cause(records)
    assert cause["red_nights"] == 3


def test_red_cause_stops_at_interrupting_green():
    # red, green, red -> the head run is only one night long -> below threshold
    records = [
        _red("2026-06-19", gate="python", detail="boom"),
        _rec("2026-06-20", "pass"),
        _red("2026-06-21", gate="python", detail="boom"),
    ]
    assert vm.derive_consecutive_red_cause(records) is None


def test_red_cause_unattributed_reds_coalesce_as_unknown():
    # two red nights with no first_fail payload still coalesce (sentinel cause),
    # so a repeated unattributed failure is not silently ignored.
    records = [_red("2026-06-20"), _red("2026-06-21")]
    cause = vm.derive_consecutive_red_cause(records)
    assert cause is not None
    assert cause["gate"] == "unknown"
    assert cause["fingerprint"] == "unknown"


def test_red_cause_unknown_does_not_merge_with_attributed():
    records = [
        _red("2026-06-20", gate="python", detail="boom"),
        _red("2026-06-21"),  # head is unattributed -> different fingerprint
    ]
    assert vm.derive_consecutive_red_cause(records) is None


def test_red_cause_min_nights_override():
    records = [
        _red("2026-06-20", gate="python", detail="boom"),
        _red("2026-06-21", gate="python", detail="boom"),
    ]
    # default (2) fires, but raising the bar to 3 does not
    assert vm.derive_consecutive_red_cause(records, min_nights=3) is None
    assert vm.derive_consecutive_red_cause(records, min_nights=2) is not None


def test_red_cause_mixed_records_same_night_is_red():
    # one pass + one fail on the same head date -> red night; the fail's cause is
    # the representative, and the prior night matches -> fires.
    records = [
        _red("2026-06-20", gate="python", detail="boom"),
        _rec("2026-06-21", "pass", ts="2026-06-21T02:00:00+00:00"),
        _red("2026-06-21", gate="python", detail="boom", ts="2026-06-21T04:00:00+00:00"),
    ]
    cause = vm.derive_consecutive_red_cause(records)
    assert cause is not None
    assert cause["red_nights"] == 2


def test_red_cause_empty_ledger():
    assert vm.derive_consecutive_red_cause([]) is None


# ---------------------------------------------------------------------------
# Legacy-night log backfill (GREEN-GATE-AUTOHEAL-LEGACY-NIGHT-S1)
#
# The live 06-20/06-21 case: an OLDER red night predates the first_fail format
# (un-attributed) while the head is attributed. Reading the predecessor's
# on-disk gate log to confirm it shares the head's failing-test signature lets
# the chain heal, instead of breaking at length 1 (idle) because "unknown"
# never matches the attributed head fingerprint.
# ---------------------------------------------------------------------------

# the head's stored first_fail.detail (run_tests.sh tail — 6 failing files)
_HEAD_DETAIL = (
    "Python (run_tests.sh):\n\n"
    "=== 6 files with test failures (9 tests failed) ===\n"
    "  tests/agent/test_copilot_acp_client.py  (1 test failed)\n"
    "  tests/agent/transports/test_codex_transport.py  (1 test failed)\n"
    "  tests/hermes_cli/test_dashboard_admin_endpoints.py  (3 tests failed)\n"
    "  tests/hermes_cli/test_redact_config_bridge.py  (1 test failed)\n"
    "  tests/hermes_cli/test_startup_plugin_gating.py  (1 test failed)\n"
    "  tests/tools/test_voice_mode.py  (2 tests failed)\n"
    "Volles Log: /x/20260621-052029/python.log"
)
# the predecessor night's FULL python.log: thousands of PASSED lines plus the
# trailing failure-summary block (5 of the head's 6 files -> a subset).
_PREV_LOG_SAME = (
    "[  0.0% |    7/34736 | OK7 | x0] ok tests/acp/test_auth.py (7ok, 1.0s)\n"
    "[  0.1% |   35/34736 | OK35 | x0] ok tests/acp/test_events.py (19ok, 1.5s)\n"
    "FAILED tests/agent/test_copilot_acp_client.py::TestA::test_x\n"
    "=== 5 files with test failures (8 tests failed) ===\n"
    "  tests/agent/test_copilot_acp_client.py  (1 test failed)\n"
    "  tests/agent/transports/test_codex_transport.py  (1 test failed)\n"
    "  tests/hermes_cli/test_dashboard_admin_endpoints.py  (3 tests failed)\n"
    "  tests/hermes_cli/test_startup_plugin_gating.py  (1 test failed)\n"
    "  tests/tools/test_voice_mode.py  (2 tests failed)\n"
)
# a wholly different failure domain (no overlap with the head's files)
_PREV_LOG_DIFFERENT = (
    "=== 1 files with test failures (1 tests failed) ===\n"
    "  tests/totally/test_other_thing.py  (1 test failed)\n"
)


def test_extract_failing_files_ignores_passed_lines():
    # only failure-context lines feed the signature — never the thousands of
    # passed lines a full run_tests.sh log carries.
    files = vm._extract_failing_test_files(_PREV_LOG_SAME)
    assert files == {
        "tests/agent/test_copilot_acp_client.py",
        "tests/agent/transports/test_codex_transport.py",
        "tests/hermes_cli/test_dashboard_admin_endpoints.py",
        "tests/hermes_cli/test_startup_plugin_gating.py",
        "tests/tools/test_voice_mode.py",
    }


def test_extract_failing_files_empty_when_no_failure_block():
    assert vm._extract_failing_test_files("all green, nothing to see") == set()
    assert vm._extract_failing_test_files(None) == set()


def test_red_cause_backfills_legacy_unattributed_predecessor():
    # head attributed (python, 6 files); the older night is red but un-attributed.
    records = [
        _red("2026-06-20"),  # legacy: no first_fail payload
        _red("2026-06-21", gate="python", detail=_HEAD_DETAIL),
    ]
    # WITHOUT a reader the current behaviour is preserved (idle).
    assert vm.derive_consecutive_red_cause(records) is None
    # WITH a reader whose log proves the same failing-file signature -> fires.
    reader = lambda date, fails: _PREV_LOG_SAME if date == "2026-06-20" else None
    cause = vm.derive_consecutive_red_cause(records, night_log_reader=reader)
    assert cause is not None
    assert cause["gate"] == "python"
    assert cause["fingerprint"].startswith("python|")
    assert cause["red_nights"] == 2
    assert cause["dates"] == ["2026-06-21", "2026-06-20"]


def test_red_cause_backfill_rejects_different_cause_predecessor():
    # AC-2: the predecessor log shows a demonstrably DIFFERENT failure -> the
    # chain breaks (the two reds are NOT merged into one series).
    records = [
        _red("2026-06-20"),
        _red("2026-06-21", gate="python", detail=_HEAD_DETAIL),
    ]
    reader = lambda date, fails: _PREV_LOG_DIFFERENT if date == "2026-06-20" else None
    assert vm.derive_consecutive_red_cause(records, night_log_reader=reader) is None


def test_red_cause_backfill_rejects_when_log_missing():
    # the reader cannot locate the predecessor log -> no false heal (idle stays
    # the safe default rather than guessing the cause matched).
    records = [
        _red("2026-06-20"),
        _red("2026-06-21", gate="python", detail=_HEAD_DETAIL),
    ]
    reader = lambda date, fails: None
    assert vm.derive_consecutive_red_cause(records, night_log_reader=reader) is None


def test_red_cause_backfill_never_fires_on_single_red_night():
    # AC-2: min_nights stays 2 — a single attributed red night never fires even
    # with a reader present.
    records = [_red("2026-06-21", gate="python", detail=_HEAD_DETAIL)]
    reader = lambda date, fails: _PREV_LOG_SAME
    assert vm.derive_consecutive_red_cause(records, night_log_reader=reader) is None


def test_red_cause_backfill_unattributed_head_unaffected():
    # reader present, but the HEAD is un-attributed -> legacy 'unknown' coalescing
    # applies (NOT a log backfill); the attributed predecessor breaks the chain
    # exactly as before, so no merge happens.
    records = [
        _red("2026-06-20", gate="python", detail="boom"),
        _red("2026-06-21"),  # un-attributed head
    ]
    reader = lambda date, fails: _PREV_LOG_SAME
    assert vm.derive_consecutive_red_cause(records, night_log_reader=reader) is None


def test_red_cause_backfill_three_nights_middle_legacy():
    # attributed head + attributed oldest, with a legacy un-attributed night in
    # the middle -> the log backfill bridges it so all three coalesce.
    records = [
        _red("2026-06-19", gate="python", detail=_HEAD_DETAIL),
        _red("2026-06-20"),  # legacy middle night
        _red("2026-06-21", gate="python", detail=_HEAD_DETAIL),
    ]
    reader = lambda date, fails: _PREV_LOG_SAME if date == "2026-06-20" else None
    cause = vm.derive_consecutive_red_cause(records, night_log_reader=reader)
    assert cause is not None
    assert cause["red_nights"] == 3
    assert cause["dates"] == ["2026-06-21", "2026-06-20", "2026-06-19"]


def test_default_night_log_reader_locates_log_by_date(tmp_path):
    root = tmp_path / "green-gate"
    run = root / "20260620-052029"
    run.mkdir(parents=True)
    (run / "python.log").write_text(
        "=== 1 files with test failures (1 tests failed) ===\n"
        "  tests/x/test_a.py  (1 test failed)\n",
        encoding="utf-8",
    )
    reader = vm.default_night_log_reader(log_root=root)
    text = reader("2026-06-20", [])
    assert text is not None
    assert "test_a.py" in text
    # a night with no matching run dir is unreadable -> None (no guess)
    assert reader("2026-06-19", []) is None


def test_default_night_log_reader_picks_latest_run_that_night(tmp_path):
    root = tmp_path / "green-gate"
    (root / "20260620-052029").mkdir(parents=True)
    (root / "20260620-052029" / "python.log").write_text("EARLY run\n", encoding="utf-8")
    (root / "20260620-231500").mkdir(parents=True)
    (root / "20260620-231500" / "python.log").write_text("LATE run\n", encoding="utf-8")
    reader = vm.default_night_log_reader(log_root=root)
    assert "LATE run" in reader("2026-06-20", [])


# ---------------------------------------------------------------------------
# record-gate-result CLI logic
# ---------------------------------------------------------------------------

def test_record_gate_result_appends_readable_record(state_dir):
    rec = vm.record_gate_result("pass", ts="2026-06-19T03:00:00+00:00")
    assert rec["result"] == "pass"
    assert rec["date"] == "2026-06-19"
    assert "epoch" in rec

    path = vm.gate_ledger_path()
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    loaded = json.loads(lines[0])
    assert loaded["result"] == "pass"
    assert loaded["date"] == "2026-06-19"

    # second append keeps the first
    vm.record_gate_result("fail", ts="2026-06-20T03:00:00+00:00")
    records = vm.read_gate_records()
    assert [r["result"] for r in records] == ["pass", "fail"]


def test_record_gate_result_rejects_invalid_result(state_dir):
    with pytest.raises(ValueError):
        vm.record_gate_result("green")


# ---------------------------------------------------------------------------
# record-gate-result: machine-readable first-failure forensics
# (GREEN-GATE-FAIL-FORENSICS-S1)
# ---------------------------------------------------------------------------

def test_fail_record_carries_first_fail_gate_and_detail(state_dir):
    rec = vm.record_gate_result(
        "fail",
        ts="2026-06-20T03:00:00+00:00",
        first_fail_gate="python",
        first_fail_detail="Python (run_tests.sh):\nE   assert 1 == 2\nVolles Log: /x.log",
    )
    assert rec["result"] == "fail"
    ff = rec["first_fail"]
    assert ff["gate"] == "python"
    assert "assert 1 == 2" in ff["detail"]

    # persisted to the ledger, machine-readable on re-read
    loaded = vm.read_gate_records()[-1]
    assert loaded["first_fail"]["gate"] == "python"
    assert "assert 1 == 2" in loaded["first_fail"]["detail"]


def test_pass_record_omits_first_fail_even_when_payload_passed(state_dir):
    # pass-Verhalten unveraendert: a pass never carries a first_fail field,
    # even if a caller erroneously supplies one.
    rec = vm.record_gate_result(
        "pass",
        ts="2026-06-20T03:00:00+00:00",
        first_fail_gate="python",
        first_fail_detail="should be ignored",
    )
    assert "first_fail" not in rec
    loaded = vm.read_gate_records()[-1]
    assert "first_fail" not in loaded


def test_fail_without_payload_omits_first_fail(state_dir):
    # backward compat: the pre-existing `record-gate-result fail` (no payload)
    # call still works and writes no first_fail field.
    rec = vm.record_gate_result("fail", ts="2026-06-20T03:00:00+00:00")
    assert "first_fail" not in rec


def test_first_fail_detail_redacts_secrets(state_dir):
    # AC-2: the captured tail runs through the existing response redaction so
    # no token reaches the on-disk ledger.
    detail = (
        "Frontend vitest run:\n"
        "FAIL using key sk-liveSECRETMIDDLE0000 and ghp_GH0000SECRETTOKEN00\n"
        "Volles Log: /x.log"
    )
    rec = vm.record_gate_result(
        "fail", ts="2026-06-20T03:00:00+00:00",
        first_fail_gate="vitest", first_fail_detail=detail,
    )
    stored = rec["first_fail"]["detail"]
    assert "SECRETMIDDLE" not in stored
    assert "SECRETTOKEN" not in stored

    # and the same holds for what's actually on disk
    raw = vm.gate_ledger_path().read_text()
    assert "SECRETMIDDLE" not in raw
    assert "SECRETTOKEN" not in raw


def test_first_fail_detail_is_capped(state_dir):
    # AC-2: the stderr tail is bounded so the ledger entry stays small.
    huge = "A" * 10_000 + "\nTRAILING_MARKER\n"
    rec = vm.record_gate_result(
        "fail", ts="2026-06-20T03:00:00+00:00",
        first_fail_gate="build", first_fail_detail=huge,
    )
    stored = rec["first_fail"]["detail"]
    assert len(stored.encode("utf-8")) <= vm.GATE_FIRST_FAIL_MAX_BYTES
    # the tail (most-relevant end of the log) is what's kept
    assert "TRAILING_MARKER" in stored


def test_first_fail_gate_is_normalized(state_dir):
    rec = vm.record_gate_result(
        "fail", ts="2026-06-20T03:00:00+00:00",
        first_fail_gate="  TSC  ", first_fail_detail="boom",
    )
    assert rec["first_fail"]["gate"] == "tsc"


def test_cli_record_gate_result_fail_with_first_fail(tmp_path, monkeypatch, state_dir):
    db_path = tmp_path / "kanban.db"
    rc = _run_cli(
        [
            "vision", "record-gate-result", "fail",
            "--first-fail-gate", "python",
            "--first-fail-detail", "boom: assert 1 == 2",
        ],
        monkeypatch, db_path,
    )
    assert rc == 0
    rec = vm.read_gate_records()[-1]
    assert rec["first_fail"]["gate"] == "python"
    assert "assert 1 == 2" in rec["first_fail"]["detail"]


# ---------------------------------------------------------------------------
# S3: commit attribution — record_gate_result(head_sha=...) + CLI --head-sha
# ---------------------------------------------------------------------------

def test_record_gate_result_stores_head_sha(state_dir):
    rec = vm.record_gate_result(
        "pass", ts="2026-06-19T03:00:00+00:00", head_sha="abc123"
    )
    assert rec["head_sha"] == "abc123"
    loaded = vm.read_gate_records()[-1]
    assert loaded["head_sha"] == "abc123"


def test_record_gate_result_omits_head_sha_when_not_given(state_dir):
    rec = vm.record_gate_result("pass", ts="2026-06-19T03:00:00+00:00")
    assert "head_sha" not in rec
    loaded = vm.read_gate_records()[-1]
    assert "head_sha" not in loaded


def test_cli_record_gate_result_head_sha_roundtrip(tmp_path, monkeypatch, state_dir):
    db_path = tmp_path / "kanban.db"
    rc = _run_cli(
        ["vision", "record-gate-result", "pass", "--head-sha", "deadbeef123"],
        monkeypatch, db_path,
    )
    assert rc == 0
    rec = vm.read_gate_records()[-1]
    assert rec["head_sha"] == "deadbeef123"


# ---------------------------------------------------------------------------
# Snapshot file: valid JSON, all fields, counters, streak wired in
# ---------------------------------------------------------------------------

def test_write_metrics_snapshot_produces_valid_file_with_all_fields(
    conn, state_dir
):
    now = 100 * DAY
    _add_task(conn, "A", status="done", consecutive_failures=0,
              completed_at=now - DAY)
    _add_run(conn, "A", cost_usd=0.05)
    # green-gate ledger feeds the streak
    vm.record_gate_result("pass", ts="2026-06-18T03:00:00+00:00")
    vm.record_gate_result("pass", ts="2026-06-19T03:00:00+00:00")

    path, snap = vm.write_metrics_snapshot(conn=conn, now=now)

    # file exists, is valid JSON, under the TEMP state dir (not live state)
    assert path.exists()
    assert path.is_relative_to(state_dir)
    on_disk = json.loads(path.read_text())
    assert on_disk == snap

    assert snap["schema_version"] >= 1
    assert "generated_at" in snap
    metrics = snap["metrics"]
    # all headline metrics present
    for key in ("autonomy", "cost_per_task", "escalation_rate",
                "green_gate_streak", "classification_coverage"):
        assert key in metrics, key
        # each headline metric carries a paired counter metric
        assert "counter" in metrics[key], key
        assert metrics[key]["counter"]["name"]
        assert "value" in metrics[key]["counter"]

    assert metrics["green_gate_streak"]["streak"] == 2
    assert metrics["green_gate_streak"]["counter"]["name"] == "fail_nights"


def test_write_metrics_snapshot_not_a_raw_db_dump(conn, state_dir):
    now = 100 * DAY
    _add_task(conn, "A", status="done", completed_at=now - DAY)
    _, snap = vm.write_metrics_snapshot(conn=conn, now=now)
    # distilled: no raw per-row task/event/run arrays leak into the snapshot
    blob = json.dumps(snap)
    assert "task_events" not in blob
    assert "task_runs" not in blob


# ---------------------------------------------------------------------------
# CLI wiring (parser -> handler -> side effects)
# ---------------------------------------------------------------------------

def _run_cli(argv, monkeypatch, db_path):
    import argparse

    from hermes_cli.subcommands.vision import build_vision_parser

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_vision_parser(sub)
    args = parser.parse_args(argv)
    return args.func(args)


def test_cli_metrics_snapshot_writes_file(tmp_path, monkeypatch, state_dir):
    db_path = tmp_path / "kanban.db"
    c = kb.connect(db_path=db_path)
    c.close()
    rc = _run_cli(["vision", "metrics-snapshot"], monkeypatch, db_path)
    assert rc == 0
    assert vm.metrics_snapshot_path().exists()


def test_cli_record_gate_result_appends(tmp_path, monkeypatch, state_dir):
    db_path = tmp_path / "kanban.db"
    rc = _run_cli(["vision", "record-gate-result", "pass"], monkeypatch, db_path)
    assert rc == 0
    assert len(vm.read_gate_records()) == 1


def test_cli_gate_fix_check_dry_run(tmp_path, monkeypatch, state_dir, capsys):
    # dry-run never writes/ingests, so it is safe to exercise the parser wiring
    # without an isolated HERMES_HOME for the spec out_dir.
    db_path = tmp_path / "kanban.db"
    vm.record_gate_result(
        "fail", ts="2026-06-20T03:00:00+00:00",
        first_fail_gate="python", first_fail_detail="assert boom",
    )
    vm.record_gate_result(
        "fail", ts="2026-06-21T03:00:00+00:00",
        first_fail_gate="python", first_fail_detail="assert boom",
    )
    rc = _run_cli(["vision", "gate-fix-check", "--dry-run", "--json"], monkeypatch, db_path)
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["triggered"] is True
    assert out["gate"] == "python"
    assert out["red_nights"] == 2
    assert out["ingested"]["dry_run"] is True


def test_cli_gate_fix_check_idle_on_green_head(tmp_path, monkeypatch, state_dir, capsys):
    db_path = tmp_path / "kanban.db"
    vm.record_gate_result("pass", ts="2026-06-21T03:00:00+00:00")
    rc = _run_cli(["vision", "gate-fix-check", "--json"], monkeypatch, db_path)
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["triggered"] is False
    assert out["ingested"] is None


def test_cli_gate_fix_check_backfills_legacy_night_from_log(
    tmp_path, monkeypatch, state_dir, capsys
):
    """E2E (GREEN-GATE-AUTOHEAL-LEGACY-NIGHT-S1): run_gate_fix wires the real
    filesystem reader, so a legacy un-attributed 06-20 night whose on-disk
    python.log shares the attributed 06-21 head's failing-file signature heals
    (the live case). Dry-run keeps it hermetic (no board ingest needed)."""
    db_path = tmp_path / "kanban.db"
    # ledger: 06-20 red but un-attributed (legacy), 06-21 red + attributed
    vm.record_gate_result("fail", ts="2026-06-20T03:31:14+00:00")
    vm.record_gate_result(
        "fail", ts="2026-06-21T03:31:15+00:00",
        first_fail_gate="python",
        first_fail_detail=(
            "=== 2 files with test failures (3 tests failed) ===\n"
            "  tests/agent/test_copilot_acp_client.py  (1 test failed)\n"
            "  tests/tools/test_voice_mode.py  (2 tests failed)\n"
        ),
    )
    # on-disk green-gate log root the reader resolves via GREEN_GATE_LOG_DIR
    log_root = tmp_path / "green-gate"
    run = log_root / "20260620-052029"
    run.mkdir(parents=True)
    (run / "python.log").write_text(
        "lots of ✓ tests/acp/test_auth.py (10✓) passing lines\n"
        "=== 1 files with test failures (1 tests failed) ===\n"
        "  tests/agent/test_copilot_acp_client.py  (1 test failed)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GREEN_GATE_LOG_DIR", str(log_root))

    rc = _run_cli(["vision", "gate-fix-check", "--dry-run", "--json"], monkeypatch, db_path)
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["triggered"] is True
    assert out["gate"] == "python"
    assert out["red_nights"] == 2
    assert out["dates"] == ["2026-06-21", "2026-06-20"]


def test_cli_gate_fix_check_idle_when_legacy_log_differs(
    tmp_path, monkeypatch, state_dir, capsys
):
    """AC-2 counter at the CLI seam: the legacy night's on-disk log shows a
    DIFFERENT failure than the head -> no merge, stays idle."""
    db_path = tmp_path / "kanban.db"
    vm.record_gate_result("fail", ts="2026-06-20T03:31:14+00:00")
    vm.record_gate_result(
        "fail", ts="2026-06-21T03:31:15+00:00",
        first_fail_gate="python",
        first_fail_detail=(
            "=== 1 files with test failures (1 tests failed) ===\n"
            "  tests/agent/test_copilot_acp_client.py  (1 test failed)\n"
        ),
    )
    log_root = tmp_path / "green-gate"
    run = log_root / "20260620-052029"
    run.mkdir(parents=True)
    (run / "python.log").write_text(
        "=== 1 files with test failures (1 tests failed) ===\n"
        "  tests/unrelated/test_other.py  (1 test failed)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GREEN_GATE_LOG_DIR", str(log_root))

    rc = _run_cli(["vision", "gate-fix-check", "--dry-run", "--json"], monkeypatch, db_path)
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["triggered"] is False


# ---------------------------------------------------------------------------
# N-of-M persistent-red triage (GREEN-GATE-PERSISTENT-RED-TRIAGE-S1)
#
# Orthogonal to derive_consecutive_red_cause (same-cause): fires when the head
# is red AND >=N reds in the last M nights, REGARDLESS of whether the first_fail
# cause changed between nights. The changing-cause case is exactly what the
# same-cause path skips, leaving the operator with a persistent red head and no
# triage item. The fingerprint is the CURRENT red file set, so re-runs dedup.
# ---------------------------------------------------------------------------

def test_persistent_red_triage_none_on_green_head():
    records = [
        _red("2026-06-19", gate="python", detail="boom"),
        _red("2026-06-20", gate="python", detail="boom"),
        _rec("2026-06-21", "pass"),
    ]
    assert vm.derive_persistent_red_triage(records) is None


def test_persistent_red_triage_none_on_single_red():
    # AC-2 guard: a single isolated flake-night must NOT fire
    records = [
        _rec("2026-06-19", "pass"),
        _rec("2026-06-20", "pass"),
        _red("2026-06-21", gate="python", detail="boom"),
    ]
    assert vm.derive_persistent_red_triage(records) is None


def test_persistent_red_triage_fires_on_two_of_three_changing_causes():
    # AC-1: head red AND >=2 reds in last 3 nights, with DIFFERENT first_fail
    # causes — exactly what derive_consecutive_red_cause skips.
    records = [
        _rec("2026-06-19", "pass"),
        _red("2026-06-20", gate="python", detail="assertion error in test_alpha"),
        _red("2026-06-21", gate="python", detail="import error in module_beta"),
    ]
    cause = vm.derive_persistent_red_triage(records)
    assert cause is not None
    assert cause["gate"] == "python"
    assert cause["red_count"] == 2
    assert cause["window"] == 3
    assert "2026-06-21" in cause["dates"]
    assert "2026-06-20" in cause["dates"]


def test_persistent_red_triage_fingerprint_stable_on_same_red_files():
    # AC-2 dedup: same red file set on follow-up night → same fingerprint
    detail = (
        "=== 2 files with test failures (2 tests failed) ===\n"
        "  tests/agent/test_copilot_acp_client.py  (1 test failed)\n"
        "  tests/tools/test_voice_mode.py  (1 test failed)\n"
    )
    records_a = [
        _red("2026-06-20", gate="python", detail=detail),
        _red("2026-06-21", gate="python", detail=detail),
    ]
    records_b = [
        _red("2026-06-20", gate="python", detail=detail),
        _red("2026-06-21", gate="python", detail=detail),
        _red("2026-06-22", gate="python", detail=detail),  # third night, same set
    ]
    fp_a = vm.derive_persistent_red_triage(records_a)["fingerprint"]
    fp_b = vm.derive_persistent_red_triage(records_b)["fingerprint"]
    assert fp_a == fp_b  # identical file set → identical fingerprint → dedup


def test_persistent_red_triage_fingerprint_changes_when_red_files_change():
    # When the red file set changes (a new test broke), the fingerprint must
    # change so a fresh triage chain opens — the operator SHOULD see a new item.
    detail_a = (
        "=== 1 files with test failures (1 tests failed) ===\n"
        "  tests/agent/test_copilot_acp_client.py  (1 test failed)\n"
    )
    detail_b = (
        "=== 1 files with test failures (1 tests failed) ===\n"
        "  tests/tools/test_voice_mode.py  (1 test failed)\n"
    )
    records = [
        _red("2026-06-20", gate="python", detail=detail_a),
        _red("2026-06-21", gate="python", detail=detail_b),  # different file
    ]
    cause = vm.derive_persistent_red_triage(records)
    assert cause is not None
    fp_head = cause["fingerprint"]
    # Swap the head's detail to a different file set
    records2 = [
        _red("2026-06-20", gate="python", detail=detail_b),
        _red("2026-06-21", gate="python", detail=detail_a),
    ]
    cause2 = vm.derive_persistent_red_triage(records2)
    assert cause2 is not None
    assert fp_head != cause2["fingerprint"]


def test_persistent_red_triage_min_reds_override():
    records = [
        _rec("2026-06-19", "pass"),
        _red("2026-06-20", gate="python", detail="boom"),
        _red("2026-06-21", gate="python", detail="boom"),
    ]
    # min_reds=3 → not enough reds in the window
    assert vm.derive_persistent_red_triage(records, min_reds=3) is None
    # min_reds=1 → fires
    assert vm.derive_persistent_red_triage(records, min_reds=1) is not None


def test_persistent_red_triage_fires_with_all_three_red():
    records = [
        _red("2026-06-19", gate="build", detail="tsc error"),
        _red("2026-06-20", gate="build", detail="tsc error"),
        _red("2026-06-21", gate="build", detail="tsc error"),
    ]
    cause = vm.derive_persistent_red_triage(records)
    assert cause is not None
    assert cause["red_count"] == 3
    assert cause["window"] == 3


def test_persistent_red_triage_empty_ledger():
    assert vm.derive_persistent_red_triage([]) is None


# ---------------------------------------------------------------------------
# S3 chronic-red-refinement: red_files-honesty (union across the window,
# red_files_by_night) + suspect_range (commit attribution) + fingerprint
# anchored to the head night only (anti-flood).
#
# Detail strings below are trimmed from REAL ~/.hermes/state/green-gate-ledger.jsonl
# entries (2026-06-27 / 2026-06-28 / 2026-06-30 / 2026-07-05 — copied 2026-07-06,
# each night a DIFFERENT failing file: the live chronic-drift pattern), not
# hand-invented shapes.
# ---------------------------------------------------------------------------

_REAL_DETAIL_A = (
    "python (isolated): tests/hermes_cli/test_kanban_core_functionality.py\n"
    "FAILED tests/hermes_cli/test_kanban_core_functionality.py::"
    "test_multiple_attempts_preserved_as_runs\n"
    "================== 4 failed, 189 passed, 3 warnings in 26.47s ==================\n"
    "\n"
    "=== 1 file with test failures (4 tests failed) ===\n"
    "  tests/hermes_cli/test_kanban_core_functionality.py  (4 tests failed)\n"
)
_REAL_DETAIL_B = (
    "python (isolated): tests/hermes_cli/test_kanban_worker_env_allowlist.py\n"
    "FAILED tests/hermes_cli/test_kanban_worker_env_allowlist.py::"
    "test_hermes_worker_env_strips_inherited_secrets\n"
    "========================= 2 failed, 3 passed in 1.16s ==========================\n"
    "\n"
    "=== 1 file with test failures (2 tests failed) ===\n"
    "  tests/hermes_cli/test_kanban_worker_env_allowlist.py  (2 tests failed)\n"
)
_REAL_DETAIL_C = (
    "python (isolated): tests/hermes_cli/test_kanban_workflow_routing.py\n"
    "FAILED tests/hermes_cli/test_kanban_workflow_routing.py::"
    "test_no_template_completes_straight_to_done\n"
    "========================= 2 failed, 14 passed in 3.39s =========================\n"
    "\n"
    "=== 1 file with test failures (2 tests failed) ===\n"
    "  tests/hermes_cli/test_kanban_workflow_routing.py  (2 tests failed)\n"
)
_REAL_DETAIL_D = (
    "python (isolated): tests/hermes_cli/test_web_server_fs.py\n"
    "FAILED tests/hermes_cli/test_web_server_fs.py::"
    "test_fs_git_root_returns_null_outside_repo\n"
    "========================= 1 failed, 12 passed in 1.69s =========================\n"
    "\n"
    "=== 1 file with test failures (1 test failed) ===\n"
    "  tests/hermes_cli/test_web_server_fs.py  (1 test failed)\n"
)


def test_persistent_red_triage_red_files_is_union_across_drifting_nights():
    # Three consecutive red nights, three DIFFERENT root causes (the live
    # 2026-07-05 log-forensics pattern: 13-14/17-18 nights red, a DIFFERENT
    # cause almost every time). Pre-S3, red_files reported ONLY the head
    # night's file -- reading as "this ONE file keeps failing" when the true
    # story is three unrelated breakages.
    records = [
        _red("2026-06-27", gate="python", detail=_REAL_DETAIL_A),
        _red("2026-06-28", gate="python", detail=_REAL_DETAIL_B),
        _red("2026-07-05", gate="python", detail=_REAL_DETAIL_C),
    ]
    cause = vm.derive_persistent_red_triage(records)
    assert cause is not None
    # Codex review 2026-07-06 finding 1: red_files KEEPS head-night semantics
    # (the PlanSpec body renders it; a union would drift the body bytes under
    # a stable fingerprint key and break planspecs' byte idempotency). The
    # window-wide honesty lives in the ADDITIVE red_files_window_union.
    assert cause["red_files"] == {"tests/hermes_cli/test_kanban_workflow_routing.py"}
    assert cause["red_files_window_union"] == {
        "tests/hermes_cli/test_kanban_core_functionality.py",
        "tests/hermes_cli/test_kanban_worker_env_allowlist.py",
        "tests/hermes_cli/test_kanban_workflow_routing.py",
    }
    by_night = cause["red_files_by_night"]
    assert [n["date"] for n in by_night] == ["2026-06-27", "2026-06-28", "2026-07-05"]
    assert by_night[0]["files"] == ["tests/hermes_cli/test_kanban_core_functionality.py"]
    assert by_night[1]["files"] == ["tests/hermes_cli/test_kanban_worker_env_allowlist.py"]
    assert by_night[2]["files"] == ["tests/hermes_cli/test_kanban_workflow_routing.py"]
    # fingerprint stays anchored to the HEAD night's own file only (see the
    # function docstring's anti-flood rationale) -- NOT the 3-file union.
    expected_fp = hashlib.sha1(
        "tests/hermes_cli/test_kanban_workflow_routing.py".encode("utf-8")
    ).hexdigest()
    assert cause["fingerprint"] == expected_fp


def test_persistent_red_triage_fingerprint_ignores_non_head_window_drift():
    # AC-2 anti-flood (S3): the fingerprint must not churn merely because an
    # OLDER night inside the window has a different cause -- only a change in
    # the HEAD night's own red files may open a new triage chain. red_files
    # (the union) DOES still change when the window's older content differs.
    records_a = [
        _red("2026-06-27", gate="python", detail=_REAL_DETAIL_A),
        _red("2026-06-28", gate="python", detail=_REAL_DETAIL_B),
        _red("2026-07-05", gate="python", detail=_REAL_DETAIL_C),  # head
    ]
    records_b = [
        _red("2026-06-27", gate="python", detail=_REAL_DETAIL_D),  # different
        _red("2026-06-28", gate="python", detail=_REAL_DETAIL_B),
        _red("2026-07-05", gate="python", detail=_REAL_DETAIL_C),  # SAME head
    ]
    cause_a = vm.derive_persistent_red_triage(records_a)
    cause_b = vm.derive_persistent_red_triage(records_b)
    assert cause_a is not None and cause_b is not None
    assert cause_a["fingerprint"] == cause_b["fingerprint"]
    # red_files (head-anchored, PlanSpec-body-stable) must be IDENTICAL under
    # non-head drift; only the additive union reflects it (Codex finding 1).
    assert cause_a["red_files"] == cause_b["red_files"]
    assert cause_a["red_files_window_union"] != cause_b["red_files_window_union"]


def test_persistent_red_triage_one_noisy_night_does_not_satisfy_min_reds():
    # Codex review 2026-07-06 finding 2: three fail RECORDS on one UTC date
    # (e.g. manual heartbeat re-runs) are ONE red night — an N-of-M *nights*
    # trigger must not fire from a single noisy night.
    records = [
        _red("2026-07-05", gate="python", detail=_REAL_DETAIL_A,
             ts="2026-07-05T03:30:00+00:00"),
        _red("2026-07-05", gate="python", detail=_REAL_DETAIL_B,
             ts="2026-07-05T09:00:00+00:00"),
        _red("2026-07-05", gate="python", detail=_REAL_DETAIL_C,
             ts="2026-07-05T15:00:00+00:00"),
    ]
    assert vm.derive_persistent_red_triage(records, min_reds=3, window=5) is None


def test_persistent_red_triage_same_date_records_merge_into_one_night():
    # Two fail records on the SAME date merge into one by_night entry (files
    # unioned); a second distinct date completes min_reds=2.
    records = [
        _red("2026-07-04", gate="python", detail=_REAL_DETAIL_A,
             ts="2026-07-04T03:30:00+00:00"),
        _red("2026-07-04", gate="python", detail=_REAL_DETAIL_B,
             ts="2026-07-04T09:00:00+00:00"),
        _red("2026-07-05", gate="python", detail=_REAL_DETAIL_C,
             ts="2026-07-05T03:30:00+00:00"),
    ]
    cause = vm.derive_persistent_red_triage(records, min_reds=2, window=5)
    assert cause is not None
    assert cause["red_count"] == 2
    by_night = cause["red_files_by_night"]
    assert [n["date"] for n in by_night] == ["2026-07-04", "2026-07-05"]
    assert len(by_night[0]["files"]) == 2  # merged union of the noisy night


def test_persistent_red_triage_suspect_range_from_consecutive_shas():
    records = [
        {**_rec("2026-06-26", "pass", ts="2026-06-26T03:00:00+00:00"), "head_sha": "aaa111"},
        {
            **_red("2026-06-27", gate="python", detail=_REAL_DETAIL_A),
            "head_sha": "bbb222",
        },
        {
            **_red("2026-06-28", gate="python", detail=_REAL_DETAIL_B),
            "head_sha": "ccc333",
        },
    ]
    cause = vm.derive_persistent_red_triage(records, min_reds=2, window=3)
    assert cause is not None
    by_night = {n["date"]: n["suspect_range"] for n in cause["red_files_by_night"]}
    assert by_night["2026-06-27"] == "aaa111..bbb222"
    assert by_night["2026-06-28"] == "bbb222..ccc333"


def test_persistent_red_triage_suspect_range_none_without_sha():
    # Graceful: a ledger that predates head_sha attribution yields range=None,
    # never a crash or a guessed range.
    records = [
        _red("2026-06-27", gate="python", detail=_REAL_DETAIL_A),
        _red("2026-06-28", gate="python", detail=_REAL_DETAIL_B),
    ]
    cause = vm.derive_persistent_red_triage(records, min_reds=2, window=2)
    assert cause is not None
    assert all(n["suspect_range"] is None for n in cause["red_files_by_night"])


# ---------------------------------------------------------------------------
# Actionable-anchor at an unattributed head (leaker-only / 'unknown' gate).
# The LIVE 2026-07-06 shape: the head night is leaker-only harness noise (no
# product cause -> gate 'unknown', no files) while the nights behind it failed
# on concrete files with CHANGING causes. The triage still fires (persistent
# red head), but its file list + gate + fingerprint must anchor on the most
# recent night that HAS concrete files so the auto-opened PlanSpec is
# actionable -- never "(unbekannt)" / 'Gate unknown'.
# ---------------------------------------------------------------------------

def test_persistent_red_triage_leaker_head_anchors_on_last_attributed_night():
    records = [
        _red("2026-07-04", gate="python", detail=_REAL_DETAIL_A),
        _red("2026-07-05", gate="python", detail=_REAL_DETAIL_C),
        _red_leaker_only("2026-07-06"),  # head: leaker-only, no product cause
    ]
    cause = vm.derive_persistent_red_triage(records)
    assert cause is not None
    # Anchored on the most recent ATTRIBUTED night (07-05), not the leaker head:
    assert cause["gate"] == "python"  # not the 'unknown' sentinel
    assert cause["red_files"] == {"tests/hermes_cli/test_kanban_workflow_routing.py"}
    expected_fp = hashlib.sha1(
        "tests/hermes_cli/test_kanban_workflow_routing.py".encode("utf-8")
    ).hexdigest()
    assert cause["fingerprint"] == expected_fp
    # the full window still surfaces additively for dashboards
    assert cause["red_files_window_union"] == {
        "tests/hermes_cli/test_kanban_core_functionality.py",
        "tests/hermes_cli/test_kanban_workflow_routing.py",
    }


def test_persistent_red_triage_leaker_head_fingerprint_stable_across_more_noise():
    # AC-2 (no spam): a SECOND leaker-only night after the same attributed anchor
    # must dedup -- the fingerprint stays put, so re-runs hit already_ingested
    # instead of minting a fresh chain from pure harness noise.
    base = [
        _red("2026-07-04", gate="python", detail=_REAL_DETAIL_A),
        _red("2026-07-05", gate="python", detail=_REAL_DETAIL_C),
        _red_leaker_only("2026-07-06"),
    ]
    later = [
        _red("2026-07-04", gate="python", detail=_REAL_DETAIL_A),
        _red("2026-07-05", gate="python", detail=_REAL_DETAIL_C),
        _red_leaker_only("2026-07-06"),
        _red_leaker_only("2026-07-07"),  # new head, still noise; anchor unchanged
    ]
    fp_a = vm.derive_persistent_red_triage(base)["fingerprint"]
    fp_b = vm.derive_persistent_red_triage(later)["fingerprint"]
    assert fp_a == fp_b


def test_persistent_red_triage_all_leaker_window_is_idle():
    # AC: An all-leaker-only window has zero RED nights. With min_reds=2 the
    # trigger correctly returns None — pure harness noise never opens a triage.
    # (Under NEUTRAL classification, leaker-only nights are not RED.)
    records = [_red_leaker_only("2026-07-05"), _red_leaker_only("2026-07-06")]
    cause = vm.derive_persistent_red_triage(records, min_reds=2, window=2)
    assert cause is None


# Vitest-style detail: no Python ``tests/*.py`` failure tokens for the extractor.
_VITEST_DETAIL_A = (
    "FAIL  src/components/JarvisGraph.test.tsx > renders nodes\n"
    "AssertionError: expected 1 to be 2"
)
_VITEST_DETAIL_B = (
    "FAIL  src/components/OtherPanel.test.tsx > loads data\n"
    "Error: Network Error"
)


def test_persistent_red_triage_mixed_gates_does_not_blame_older_python():
    # RCA 2026-07-21: Python fully green while head was Vitest without extractable
    # ``tests/*.py`` paths. Cross-gate file fallback must NOT mint gate=python.
    records = [
        _red("2026-07-18", gate="python", detail=_REAL_DETAIL_A),
        _red("2026-07-19", gate="python", detail=_REAL_DETAIL_B),
        _red("2026-07-20", gate="vitest", detail=_VITEST_DETAIL_A),
    ]
    cause = vm.derive_persistent_red_triage(records, min_reds=2, window=3)
    # Single vitest red night → below gate-local threshold; never python.
    assert cause is None


def test_persistent_red_triage_gate_local_min_reds_ignores_other_gates():
    # AC-2: >=N reds must be for the same head/anchor gate. Older python reds
    # in the window must not help a lone vitest head clear the threshold.
    records = [
        _red("2026-07-18", gate="python", detail=_REAL_DETAIL_A),
        _red("2026-07-19", gate="python", detail=_REAL_DETAIL_B),
        _red("2026-07-20", gate="vitest", detail=_VITEST_DETAIL_A),
    ]
    assert vm.derive_persistent_red_triage(records, min_reds=2, window=3) is None


def test_persistent_red_triage_same_gate_changing_causes_still_fire():
    # AC-2: same-gate changing first-fail causes continue to trigger.
    records = [
        _red("2026-07-18", gate="vitest", detail=_VITEST_DETAIL_A),
        _red("2026-07-19", gate="vitest", detail=_VITEST_DETAIL_B),
    ]
    cause = vm.derive_persistent_red_triage(records, min_reds=2, window=3)
    assert cause is not None
    assert cause["gate"] == "vitest"
    assert cause["red_count"] == 2
    assert cause["dates"] == ["2026-07-18", "2026-07-19"]
    # No Python files smuggled in; extractor still finds none for vitest text.
    assert cause["red_files"] == set()
    assert cause["red_files_window_union"] == set()


def test_persistent_red_triage_attributed_head_no_cross_gate_file_fallback():
    # AC-3: unextractable attributed head must keep its own gate and must not
    # fall back to an older different-gate file set (even when min_reds=1).
    records = [
        _red("2026-07-19", gate="python", detail=_REAL_DETAIL_A),
        _red("2026-07-20", gate="vitest", detail=_VITEST_DETAIL_A),
    ]
    cause = vm.derive_persistent_red_triage(records, min_reds=1, window=3)
    assert cause is not None
    assert cause["gate"] == "vitest"
    assert cause["red_files"] == set()
    assert "tests/hermes_cli/test_planspecs.py" not in cause["red_files_window_union"]
    assert cause["red_count"] == 1
    assert cause["dates"] == ["2026-07-20"]


def test_persistent_red_triage_mixed_window_counts_only_head_gate_nights():
    # Two vitest reds fire even when the window also holds a python red; dates
    # and file surfaces stay gate-local to the head.
    records = [
        _red("2026-07-18", gate="python", detail=_REAL_DETAIL_A),
        _red("2026-07-19", gate="vitest", detail=_VITEST_DETAIL_A),
        _red("2026-07-20", gate="vitest", detail=_VITEST_DETAIL_B),
    ]
    cause = vm.derive_persistent_red_triage(records, min_reds=2, window=3)
    assert cause is not None
    assert cause["gate"] == "vitest"
    assert cause["red_count"] == 2
    assert cause["dates"] == ["2026-07-19", "2026-07-20"]
    assert "tests/hermes_cli/test_planspecs.py" not in cause["red_files_window_union"]
    assert all(n["date"] != "2026-07-18" for n in cause["red_files_by_night"])


def test_consecutive_red_cause_suspect_range_mixed_with_and_without_sha():
    # Mixed ledger: an older pass record with NO head_sha, then two same-cause
    # red nights that DO carry one.
    records = [
        _rec("2026-06-18", "pass"),
        {**_red("2026-06-19", gate="python", detail="boom"), "head_sha": "sha0619"},
        {**_red("2026-06-20", gate="python", detail="boom"), "head_sha": "sha0620"},
    ]
    cause = vm.derive_consecutive_red_cause(records, min_nights=2)
    assert cause is not None
    ranges = {r["date"]: r["range"] for r in cause["suspect_ranges"]}
    # no earlier sha-carrying record precedes 06-19 (06-18 has none) -> null
    assert ranges["2026-06-19"] is None
    assert ranges["2026-06-20"] == "sha0619..sha0620"


def test_consecutive_red_cause_suspect_range_none_when_night_missing_sha():
    records = [
        {**_red("2026-06-19", gate="python", detail="boom"), "head_sha": "sha0619"},
        _red("2026-06-20", gate="python", detail="boom"),  # this night has no sha
    ]
    cause = vm.derive_consecutive_red_cause(records, min_nights=2)
    assert cause is not None
    ranges = {r["date"]: r["range"] for r in cause["suspect_ranges"]}
    assert ranges["2026-06-20"] is None


def test_cli_triage_check_dry_run(tmp_path, monkeypatch, state_dir, capsys):
    db_path = tmp_path / "kanban.db"
    vm.record_gate_result(
        "fail", ts="2026-06-20T03:00:00+00:00",
        first_fail_gate="python", first_fail_detail="assert boom in test_alpha",
    )
    vm.record_gate_result(
        "fail", ts="2026-06-21T03:00:00+00:00",
        first_fail_gate="python", first_fail_detail="import error in module_beta",
    )
    rc = _run_cli(["vision", "triage-check", "--dry-run", "--json"], monkeypatch, db_path)
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["triggered"] is True
    assert out["gate"] == "python"
    assert out["red_count"] == 2
    assert out["window"] == 3
    assert out["ingested"]["dry_run"] is True


def test_cli_triage_check_idle_on_green_head(tmp_path, monkeypatch, state_dir, capsys):
    db_path = tmp_path / "kanban.db"
    vm.record_gate_result("pass", ts="2026-06-21T03:00:00+00:00")
    rc = _run_cli(["vision", "triage-check", "--json"], monkeypatch, db_path)
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["triggered"] is False
    assert out["ingested"] is None


def test_cli_triage_check_idle_on_single_isolated_red(
    tmp_path, monkeypatch, state_dir, capsys
):
    """AC-2 guard: a single isolated flake-night must NOT open a triage spec."""
    db_path = tmp_path / "kanban.db"
    vm.record_gate_result("pass", ts="2026-06-20T03:00:00+00:00")
    vm.record_gate_result(
        "fail", ts="2026-06-21T03:00:00+00:00",
        first_fail_gate="python", first_fail_detail="assert boom",
    )
    rc = _run_cli(["vision", "triage-check", "--dry-run", "--json"], monkeypatch, db_path)
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["triggered"] is False


def test_cli_triage_check_fires_on_changing_causes(
    tmp_path, monkeypatch, state_dir, capsys
):
    """AC-1 E2E: two red nights with DIFFERENT first_fail causes — the exact
    case the same-cause path skips, and this triage path must catch."""
    db_path = tmp_path / "kanban.db"
    vm.record_gate_result(
        "fail", ts="2026-06-20T03:00:00+00:00",
        first_fail_gate="python",
        first_fail_detail=(
            "=== 1 files with test failures (1 tests failed) ===\n"
            "  tests/agent/test_copilot_acp_client.py  (1 test failed)\n"
        ),
    )
    vm.record_gate_result(
        "fail", ts="2026-06-21T03:00:00+00:00",
        first_fail_gate="python",
        first_fail_detail=(
            "=== 1 files with test failures (1 tests failed) ===\n"
            "  tests/tools/test_voice_mode.py  (1 test failed)\n"
        ),
    )
    rc = _run_cli(["vision", "triage-check", "--dry-run", "--json"], monkeypatch, db_path)
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["triggered"] is True
    assert out["red_count"] == 2
    assert "tests/tools/test_voice_mode.py" in out["red_files"]


#
# A red gate's first_fail must name a real (in-isolation reproducible) failure,
# never a test-isolation leaker. The ledger entry carries the demoted leaker
# list for operator visibility (AC-2) and a `leaker_only` flag when the whole
# night was harness noise — and the autoheal walk must NOT open a fix for it.
# ---------------------------------------------------------------------------

def test_fail_record_stores_leaker_list(state_dir):
    rec = vm.record_gate_result(
        "fail", ts="2026-06-21T03:00:00+00:00",
        first_fail_gate="python",
        first_fail_detail="isolated tests/b.py: FAILED",
        leakers=["python: tests/a.py", "python: tests/flaky.py"],
    )
    assert rec["leakers"] == ["python: tests/a.py", "python: tests/flaky.py"]
    # the real (reproduced) cause is still attached as first_fail
    assert rec["first_fail"]["gate"] == "python"
    loaded = vm.read_gate_records()[-1]
    assert loaded["leakers"] == ["python: tests/a.py", "python: tests/flaky.py"]


def test_fail_record_leaker_only_omits_first_fail(state_dir):
    # every reported fail passed alone -> there is no product cause; the night
    # stays red (result=fail) but carries no first_fail and is flagged.
    rec = vm.record_gate_result(
        "fail", ts="2026-06-21T03:00:00+00:00",
        first_fail_gate="python", first_fail_detail="should be ignored",
        leakers=["python: tests/a.py"], leaker_only=True,
    )
    assert rec["result"] == "fail"
    assert "first_fail" not in rec
    assert rec["leaker_only"] is True
    assert rec["leakers"] == ["python: tests/a.py"]


def test_fail_record_leakers_redacted_and_capped(state_dir):
    secret = "python: tests/a.py token=sk-ABCDEF1234567890SECRETVALUE"
    rec = vm.record_gate_result(
        "fail", ts="2026-06-21T03:00:00+00:00",
        first_fail_gate="python", first_fail_detail="boom",
        leakers=[secret] + [f"python: tests/t{i}.py" for i in range(100)],
    )
    # secret never reaches the on-disk ledger
    assert "sk-ABCDEF1234567890SECRETVALUE" not in json.dumps(rec)
    # list is capped so a pathological run can't balloon the ledger entry
    assert len(rec["leakers"]) <= vm.GATE_LEAKERS_MAX


def test_pass_record_ignores_leakers(state_dir):
    rec = vm.record_gate_result(
        "pass", ts="2026-06-21T03:00:00+00:00",
        leakers=["python: tests/a.py"], leaker_only=True,
    )
    assert "leakers" not in rec
    assert "leaker_only" not in rec


def _red_leaker_only(date, *, ts=None):
    return {
        "date": date, "result": "fail", "ts": ts or f"{date}T03:00:00+00:00",
        "leaker_only": True, "leakers": [f"python: tests/{date}.py"],
    }


def test_red_cause_skips_leaker_only_head():
    # head night is red but pure harness noise (every fail was a leaker) -> the
    # autoheal loop must NOT open a fix; there is no product cause to heal.
    records = [
        _red("2026-06-20", gate="python", detail="real boom"),
        _red_leaker_only("2026-06-21"),
    ]
    assert vm.derive_consecutive_red_cause(records) is None


def test_red_cause_leaker_only_night_breaks_run():
    # a real recurring cause is interrupted by a leaker-only night -> the head
    # run is too short to fire (the leaker night is not "the same cause").
    records = [
        _red("2026-06-19", gate="python", detail="real boom"),
        _red_leaker_only("2026-06-20"),
        _red("2026-06-21", gate="python", detail="real boom"),
    ]
    assert vm.derive_consecutive_red_cause(records) is None


def test_red_cause_fires_when_real_cause_also_carries_leakers():
    # leakers demoted on the side must not stop a genuine same-cause streak.
    records = [
        {"date": "2026-06-20", "result": "fail", "ts": "2026-06-20T03:00:00+00:00",
         "first_fail": {"gate": "python", "detail": "assert 1 == 2 in test_real"},
         "leakers": ["python: tests/flaky.py"]},
        {"date": "2026-06-21", "result": "fail", "ts": "2026-06-21T03:00:00+00:00",
         "first_fail": {"gate": "python", "detail": "assert 7 == 9 in test_real"},
         "leakers": ["python: tests/other_flaky.py"]},
    ]
    cause = vm.derive_consecutive_red_cause(records)
    assert cause is not None
    assert cause["gate"] == "python"
    assert cause["red_nights"] == 2


def test_red_cause_leaker_only_does_not_coalesce_as_unknown():
    # two consecutive leaker-only nights are NOT a recurring product cause (this
    # is the whole point: harness noise must never mint a fix-PlanSpec), unlike
    # genuinely unattributed reds which still coalesce as "unknown".
    records = [_red_leaker_only("2026-06-20"), _red_leaker_only("2026-06-21")]
    assert vm.derive_consecutive_red_cause(records) is None


# ---------------------------------------------------------------------------
# M2: unclassified_share + classified_total in classification_coverage metric
# ---------------------------------------------------------------------------

def test_classification_coverage_unclassified_share(conn):
    """M2: _classification_coverage_metric exposes unclassified_share (percent
    of heiler_classification events in the window whose class is 'unclassified')
    and classified_total (raw count). This is the real trust signal for the
    Stratege — the 24h-coverage headline is trivially saturated by the auto-sweep,
    but a high unclassified_share reveals that by_class is still untrustworthy.

    Fixture: 10 heiler_classification events in the window, 3 unclassified,
    7 of other classes. Assert: unclassified_share==30.0, classified_total==10,
    coverage_pct present.
    """
    now = 100 * DAY
    cutoff = now - 7 * DAY

    # Insert tasks (required for FK-less inserts to avoid integrity errors; tasks
    # table allows unknown ids via deferred FK, so direct insert is fine here).
    for i in range(10):
        _add_task(conn, f"U{i}", status="blocked", created_at=now - DAY)

    # 3 unclassified events inside the window
    for i in range(3):
        _add_event(conn, f"U{i}", kb.HEILER_CLASSIFICATION_EVENT,
                   payload={"class": kb.HEILER_CLASS_UNCLASSIFIED},
                   created_at=now - DAY)

    # 5 real-bug events inside the window
    for i in range(3, 8):
        _add_event(conn, f"U{i}", kb.HEILER_CLASSIFICATION_EVENT,
                   payload={"class": kb.HEILER_CLASS_REAL_BUG},
                   created_at=now - DAY)

    # 2 transient events inside the window
    for i in range(8, 10):
        _add_event(conn, f"U{i}", kb.HEILER_CLASSIFICATION_EVENT,
                   payload={"class": kb.HEILER_CLASS_TRANSIENT},
                   created_at=now - DAY)

    # One extra event OUTSIDE the window — must not count
    _add_task(conn, "OLD", status="blocked", created_at=cutoff - 1)
    _add_event(conn, "OLD", kb.HEILER_CLASSIFICATION_EVENT,
               payload={"class": kb.HEILER_CLASS_UNCLASSIFIED},
               created_at=cutoff - 1)

    snap = vm.compute_metrics_snapshot(conn, now=now, window_days=7)
    c = snap["metrics"]["classification_coverage"]

    assert c["unclassified_share"] == 30.0, (
        f"expected 30.0, got {c['unclassified_share']!r}"
    )
    assert c["classified_total"] == 10, (
        f"expected 10, got {c['classified_total']!r}"
    )
    # Existing key must still be present (non-regression)
    assert "coverage_pct" in c


def test_classification_coverage_unclassified_share_null_when_no_events(conn):
    """M2: unclassified_share is None when there are no heiler_classification
    events in the window (classified_total == 0), mirroring the coverage_pct
    null-on-empty contract."""
    snap = vm.compute_metrics_snapshot(conn, now=100 * DAY, window_days=7)
    c = snap["metrics"]["classification_coverage"]
    assert c["unclassified_share"] is None
    assert c["classified_total"] == 0


# ---------------------------------------------------------------------------
# ESCALATION-OPERATOR-GATE-DECLASSIFY-S1: operator gates get a terminal
# non-error class instead of landing in unclassified (AC-1/AC-2).
# ---------------------------------------------------------------------------

def _add_operator_escalation(conn, tid, *, last_error, trigger_outcome="blocked",
                             created_at):
    """An operator_escalation event shaped like the silent-block sweep writes,
    so classify_escalations_sweep can derive its Heiler class from evidence."""
    payload = {
        "why_now": f"settled block (last run outcome: {trigger_outcome}) with "
                   "no operator_escalation",
        "evidence": {"trigger_outcome": trigger_outcome, "last_error": last_error},
    }
    cur = conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (tid, kb.OPERATOR_ESCALATION_EVENT, json.dumps(payload), created_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_operator_gates_declassified_cuts_unclassified_share(conn, state_dir):
    """AC-1: operator-gated holds (held-before-release / operator hold) get their
    own terminal non-error class via classify_escalations_sweep, so the
    unclassified_share drops by >=30% vs the pre-change behaviour (where they all
    fell through to unclassified). AC-2: the real-error classes (real-bug /
    bad-spec / transient) are untouched — their counts do not drop."""
    from collections import Counter

    now = 100 * DAY
    specs = (
        # 6 held-before-release freigabe holds -> operator-gated (were unclassified)
        [("Planspec ingest: held before release", "scheduled")] * 6
        # 2 genuinely-opaque -> stay unclassified (before AND after)
        + [("something entirely opaque happened", "blocked")] * 2
        # 5 real-bug / 4 bad-spec / 3 transient -> must be preserved (AC-2)
        + [("gate failed: 3 tests failed", "blocked")] * 5
        + [("acceptance criteria cannot be met", "blocked")] * 4
        + [("dirty worktree overlap on branch X", "blocked")] * 3
    )
    for i, (last_error, outcome) in enumerate(specs):
        _add_task(conn, f"E{i}", status="blocked", created_at=now - DAY)
        _add_operator_escalation(conn, f"E{i}", last_error=last_error,
                                 trigger_outcome=outcome, created_at=now - DAY)

    kb.classify_escalations_sweep(conn, now=now)

    cls_counts: Counter = Counter()
    for r in conn.execute(
        "SELECT payload FROM task_events WHERE kind = ?",
        (kb.HEILER_CLASSIFICATION_EVENT,),
    ).fetchall():
        cls_counts[json.loads(r["payload"])["class"]] += 1

    # AC-1: the 6 operator gates are declassified out of unclassified.
    assert cls_counts[kb.HEILER_CLASS_OPERATOR_GATED] == 6
    assert cls_counts[kb.HEILER_CLASS_UNCLASSIFIED] == 2
    # AC-2: real-error classes preserved, not masked as operator-gated.
    assert cls_counts[kb.HEILER_CLASS_REAL_BUG] == 5
    assert cls_counts[kb.HEILER_CLASS_BAD_SPEC] == 4
    assert cls_counts[kb.HEILER_CLASS_TRANSIENT] == 3

    total = sum(cls_counts.values())
    old_share = 100.0 * (6 + 2) / total   # pre-change: the 6 gates were unclassified too
    new_share = 100.0 * cls_counts[kb.HEILER_CLASS_UNCLASSIFIED] / total
    assert (old_share - new_share) / old_share >= 0.30, (
        f"expected >=30% reduction, old={old_share} new={new_share}"
    )

    # The metric surfaces the reduced share.
    c = vm.compute_metrics_snapshot(conn, now=now, window_days=7)[
        "metrics"]["classification_coverage"]
    assert c["classified_total"] == total
    assert c["unclassified_share"] == new_share


def test_escalation_rate_relieved_of_operator_gates(conn):
    """AC-1: escalation_rate exposes error_escalations_per_week — the headline
    relieved of the false-positive operator gates. The operator-facing
    escalation is preserved (AC-2), so operator gates still count in the raw
    escalations_per_week; they are only excluded from the error rate."""
    now = 100 * DAY
    for i, err in enumerate((
        "gate failed: 3 tests failed",
        "acceptance criteria cannot be met",
        "dirty worktree overlap on branch X",
    )):
        _add_task(conn, f"R{i}", status="blocked", created_at=now - DAY)
        _add_operator_escalation(conn, f"R{i}", last_error=err, created_at=now - DAY)
    for i, (err, outcome) in enumerate((
        ("Planspec ingest: held before release", "scheduled"),
        ("operator hold", "blocked"),
    )):
        _add_task(conn, f"G{i}", status="blocked", created_at=now - DAY)
        _add_operator_escalation(conn, f"G{i}", last_error=err,
                                 trigger_outcome=outcome, created_at=now - DAY)

    e = vm.compute_metrics_snapshot(conn, now=now, window_days=7)[
        "metrics"]["escalation_rate"]
    assert e["escalations_per_week"] == 5          # all distinct tasks (preserved)
    assert e["error_escalations_per_week"] == 3    # relieved of the 2 operator gates


# ---------------------------------------------------------------------------
# operator_load metric (OPERATOR-LOAD-S1)
# ---------------------------------------------------------------------------

OL_NOW = 2_000_000


def _add_strategist_task(conn, tid, *, status="scheduled", created_at,
                         decompose_of=None):
    """A strategist-cron task incl. its 'created' event — the event payload
    shape ({"by": ...} / {"from_decompose_of": ...}) mirrors the live DB
    (sampled 2026-07-06, e.g. child t_271c5c17 of root t_50bf2f83)."""
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_by, created_at) "
        "VALUES (?, ?, ?, 'strategist-cron', ?)",
        (tid, f"lever {tid}", status, created_at),
    )
    payload = {"by": "strategist-cron"}
    if decompose_of:
        payload["from_decompose_of"] = decompose_of
    _add_event(conn, tid, "created", payload=payload, created_at=created_at)


def _add_comment(conn, tid, author, *, created_at):
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at, kind) "
        "VALUES (?, ?, 'x', ?, 'comment')",
        (tid, author, created_at),
    )
    conn.commit()


def test_operator_load_counts_only_operator_authors(conn):
    """flow-gate releases and worker comments are NOT operator touches; the
    payload author shapes match the live DB ({"author": "flow-gate"} vs
    {"author": "operator"}, sampled 2026-07-06)."""
    _add_strategist_task(conn, "t_root1", created_at=OL_NOW - 4 * DAY)
    _add_event(conn, "t_root1", "freigabe_vetoed",
               payload={"author": "operator"}, created_at=OL_NOW - 1 * DAY)
    conn.execute("UPDATE tasks SET status='archived' WHERE id='t_root1'")
    _add_task(conn, "t_auto", status="done", created_at=OL_NOW - 3 * DAY)
    _add_event(conn, "t_auto", "freigabe_released",
               payload={"author": "flow-gate"}, created_at=OL_NOW - 1 * DAY)
    _add_comment(conn, "t_root1", "operator", created_at=OL_NOW - 1 * DAY)
    _add_comment(conn, "t_root1", "coder", created_at=OL_NOW - 1 * DAY)
    # idempotent re-release replay (real payload shape from
    # _release_freigabe_hold_root_in_txn) is not a second operator touch
    _add_event(conn, "t_root1", "freigabe_released",
               payload={"author": "operator", "idempotent": True},
               created_at=OL_NOW - 1 * DAY)

    m = vm._operator_load_metric(conn, now=OL_NOW, window_days=7)
    assert m["freigabe_decisions"] == 1
    assert m["operator_comments"] == 1
    assert m["touches_per_week"] == 2
    assert m["decision_latency_days_median"] == 3.0
    assert m["held_open"] == 0


def test_operator_load_held_queue_and_antagonist_counter(conn):
    _add_strategist_task(conn, "t_fresh", created_at=OL_NOW - 2 * DAY)
    _add_strategist_task(conn, "t_old", created_at=OL_NOW - 9 * DAY)
    # decompose child of a held chain must NOT count as a root
    _add_strategist_task(conn, "t_child", created_at=OL_NOW - 9 * DAY,
                         decompose_of="t_old")
    _add_strategist_task(conn, "t_done", created_at=OL_NOW - 9 * DAY)
    _add_event(conn, "t_done", "freigabe_released",
               payload={"author": "operator"}, created_at=OL_NOW - 8 * DAY)

    m = vm._operator_load_metric(conn, now=OL_NOW, window_days=7)
    assert m["held_open"] == 2
    assert m["counter"]["name"] == "held_over_7d"
    assert m["counter"]["value"] == 1
    roots = {r["id"] for r in vm.held_strategist_roots(conn)}
    assert roots == {"t_fresh", "t_old"}


def test_operator_load_in_snapshot_and_summary(conn):
    snap = vm.compute_metrics_snapshot(conn, now=OL_NOW, window_days=7)
    ol = snap["metrics"]["operator_load"]
    assert set(ol) >= {"touches_per_week", "freigabe_decisions",
                       "operator_comments", "decision_latency_days_median",
                       "held_open", "counter"}
    assert "operator load" in vm.render_snapshot_summary(snap)


# ---------------------------------------------------------------------------
# GATE-LEAKER-STREAK-HONESTY-V2: leaker-only nights are NEUTRAL — transparent to
# the green streak, the release-brake red streak and the triage red-count, and
# tracked only as a low-severity, visible leaker-debt channel.
#
# The fixture below is a faithful slice of the live green-gate ledger tail
# (copied 2026-07-06): product-red nights culminating in the REAL 2026-07-06
# leaker-only night whose sole "failure" was tests/hermes_cli/test_planspecs.py
# passing when re-run alone.
# ---------------------------------------------------------------------------

def _leaker(date, files, *, ts=None):
    """A leaker-only fail record (the whole night was test-isolation noise)."""
    return {
        "date": date,
        "result": "fail",
        "ts": ts or f"{date}T03:34:13+00:00",
        "leaker_only": True,
        "leakers": list(files),
    }


def _real_ledger_with_leaker_head():
    # Two real product-red nights + the real 2026-07-06 leaker-only night.
    return [
        _red("2026-07-04", gate="python", detail=_REAL_DETAIL_A,
             ts="2026-07-04T03:34:29+00:00"),
        _red("2026-07-05", gate="python", detail=_REAL_DETAIL_C,
             ts="2026-07-05T03:35:51+00:00"),
        _leaker("2026-07-06", ["python: tests/hermes_cli/test_planspecs.py"],
                ts="2026-07-06T03:34:13+00:00"),
    ]


def test_classify_gate_nights_green_red_neutral():
    records = [
        _rec("2026-07-03", "pass"),
        _red("2026-07-04", gate="python", detail="boom"),
        _leaker("2026-07-05", ["python: tests/x.py"]),
    ]
    assert vm.classify_gate_nights(records) == {
        "2026-07-03": vm.NIGHT_GREEN,
        "2026-07-04": vm.NIGHT_RED,
        "2026-07-05": vm.NIGHT_NEUTRAL,
    }


def test_classify_night_with_survivor_and_leaker_stays_red():
    # AC-1 symmetry: a surviving product fail sharing a night with a leaker must
    # keep the night RED — leaker demotion may never hide a real regression.
    records = [
        _leaker("2026-07-05", ["python: tests/x.py"], ts="2026-07-05T03:00:00+00:00"),
        _red("2026-07-05", gate="python", detail="real boom",
             ts="2026-07-05T04:00:00+00:00"),
    ]
    assert vm.classify_gate_nights(records) == {"2026-07-05": vm.NIGHT_RED}


def test_streak_leaker_night_is_neutral_not_green_not_red():
    # AC-1: a leaker-only night at the head does NOT break the green streak and
    # is NOT counted as a green night.
    records = [
        _rec("2026-07-03", "pass"),
        _rec("2026-07-04", "pass"),
        _leaker("2026-07-05", ["python: tests/x.py"]),
    ]
    out = vm.derive_gate_streak(records)
    assert out["streak"] == 2          # two greens survive the transparent leaker
    assert out["green_nights"] == 2    # leaker night is NOT a green night
    assert out["fail_nights"] == 0     # ...nor a red night
    assert out["neutral_nights"] == 1


def test_streak_leaker_between_greens_does_not_advance():
    # A leaker night embedded between greens is skipped, not counted as green.
    records = [
        _rec("2026-07-03", "pass"),
        _leaker("2026-07-04", ["python: tests/x.py"]),
        _rec("2026-07-05", "pass"),
    ]
    out = vm.derive_gate_streak(records)
    assert out["streak"] == 2
    assert out["green_nights"] == 2
    assert out["neutral_nights"] == 1


def test_streak_survivor_night_stays_fully_red():
    # AC-1 regression: a night with a surviving product first_fail stays fully
    # red even if a leaker was also recorded that night.
    records = [
        _rec("2026-07-03", "pass"),
        _leaker("2026-07-04", ["python: tests/x.py"], ts="2026-07-04T03:00:00+00:00"),
        _red("2026-07-04", gate="python", detail="real boom",
             ts="2026-07-04T04:00:00+00:00"),
    ]
    out = vm.derive_gate_streak(records)
    assert out["streak"] == 0
    assert out["fail_nights"] == 1
    assert out["neutral_nights"] == 0


def test_red_streak_leaker_head_does_not_extend_or_reset():
    # AC-1: a leaker-only night at the head neither extends the release-brake red
    # streak nor resets it — it is transparent, so a standing hold is preserved.
    records = [
        _red("2026-07-03", gate="python", detail="boom"),
        _red("2026-07-04", gate="python", detail="boom"),
        _red("2026-07-05", gate="python", detail="boom"),
        _leaker("2026-07-06", ["python: tests/x.py"]),
    ]
    # 3 reds → a pause_on_red_streak=3 hold holds; the leaker keeps it at 3
    # (not 4 = extended, not 0 = dissolved).
    assert vm.red_streak_from_head(records) == 3


def test_red_streak_leaker_head_over_green_stays_zero():
    # A leaker-only night after a green night must not manufacture a red streak.
    records = [
        _rec("2026-07-04", "pass"),
        _leaker("2026-07-05", ["python: tests/x.py"]),
    ]
    assert vm.red_streak_from_head(records) == 0


def test_persistent_red_triage_leaker_head_is_idle():
    # AC-2: a lone leaker-only night at the head over a green history is neutral,
    # not a red head — no triage opens.
    records = [
        _rec("2026-07-03", "pass"),
        _rec("2026-07-04", "pass"),
        _leaker("2026-07-05", ["python: tests/x.py"]),
    ]
    assert vm.derive_persistent_red_triage(records) is None


def test_persistent_red_triage_single_red_plus_leaker_head_is_idle():
    # AC-2 guard survives neutrality: one real red + a leaker head is still a
    # single red night, not a persistent-red trigger.
    records = [
        _rec("2026-07-03", "pass"),
        _red("2026-07-04", gate="python", detail="boom"),
        _leaker("2026-07-05", ["python: tests/x.py"]),
    ]
    assert vm.derive_persistent_red_triage(records, min_reds=2, window=3) is None


def test_persistent_red_triage_leaker_night_not_counted_in_min_reds():
    # AC-2 core: the real 2026-07-06 leaker night must NOT count toward min_reds.
    # The head is the most recent MEANINGFUL night (07-05, red); the two real
    # product reds fire the trigger, and the leaker is absent from the dates.
    records = _real_ledger_with_leaker_head()
    cause = vm.derive_persistent_red_triage(records, min_reds=2, window=3)
    assert cause is not None
    assert cause["red_count"] == 2
    assert cause["dates"] == ["2026-07-04", "2026-07-05"]
    assert "2026-07-06" not in cause["dates"]
    # The leaker file never enters the triage surface.
    assert "tests/hermes_cli/test_planspecs.py" not in cause["red_files_window_union"]


def test_green_gate_metric_surfaces_leaker_debt_from_real_leaker_night():
    # AC-2: the leaker-debt counter appears in its own channel (which the
    # dashboard tile reads) while the red-night counter excludes it.
    records = _real_ledger_with_leaker_head()
    m = vm._green_gate_metric(records)
    assert m["streak"] == 0
    assert m["counter"]["name"] == "fail_nights"
    assert m["counter"]["value"] == 2          # only the two product-red nights
    assert m["neutral_nights"] == 1
    assert m["leaker_debt_nights"] == 1        # flat mirror the tile pulls by path
    assert m["leaker_debt"]["value"] == 1
    assert m["leaker_debt"]["severity"] == "low"


def test_full_snapshot_carries_leaker_debt_channel(conn):
    # End-to-end: the written snapshot's green_gate_streak block exposes both the
    # flat tile field and the structured low-severity debt entry.
    records = _real_ledger_with_leaker_head()
    snap = vm.compute_metrics_snapshot(conn, now=OL_NOW, gate_records=records)
    g = snap["metrics"]["green_gate_streak"]
    assert g["leaker_debt_nights"] == 1
    assert g["leaker_debt"]["severity"] == "low"
    assert g["counter"]["value"] == 2


# ---------------------------------------------------------------------------
# Flaky de-flake debt (GATE-FLAKY-RETRY-HONESTY-S1)
# ---------------------------------------------------------------------------

def _leaker_fail(date, leakers, *, leaker_only=True, first_fail=None):
    """A red ledger record carrying demoted leaker (flaky) files."""
    rec = {"date": date, "result": "fail", "ts": f"{date}T03:00:00+00:00",
           "leakers": list(leakers)}
    if leaker_only:
        rec["leaker_only"] = True
    if first_fail is not None:
        rec["first_fail"] = first_fail
    return rec


def test_derive_flaky_candidates_groups_by_file_and_counts_nights():
    # The live example: test_delegate flaky-neutralized on 3 distinct nights.
    records = [
        _leaker_fail("2026-07-10", ["python: tests/agent/test_delegate.py"]),
        _leaker_fail("2026-07-12", ["python: tests/agent/test_delegate.py"]),
        # partially-leaky RED night: a real first_fail + the same flake demoted.
        _leaker_fail(
            "2026-07-13",
            ["python: tests/agent/test_delegate.py"],
            leaker_only=False,
            first_fail={"gate": "python", "detail": "assert real regression"},
        ),
    ]
    cands = vm.derive_flaky_deflake_candidates(records)
    assert len(cands) == 1
    c = cands[0]
    assert c["file"] == "tests/agent/test_delegate.py"
    assert c["gate"] == "python"
    assert c["nights"] == 3
    assert c["dates"] == ["2026-07-10", "2026-07-12", "2026-07-13"]
    assert c["recurring"] is True  # >= RECURRING_FLAKE_MIN_NIGHTS (3)


def test_derive_flaky_candidates_single_night_is_not_recurring():
    records = [_leaker_fail("2026-07-18", ["vitest: src/foo.test.ts"])]
    cands = vm.derive_flaky_deflake_candidates(records)
    assert len(cands) == 1
    assert cands[0]["nights"] == 1
    assert cands[0]["recurring"] is False
    assert cands[0]["gate"] == "vitest"


def test_derive_flaky_candidates_dedups_same_file_within_one_night():
    # A file listed twice the same night is ONE flaky night, not two.
    records = [
        _leaker_fail(
            "2026-07-18",
            ["python: tests/x.py", "python: tests/x.py"],
        )
    ]
    cands = vm.derive_flaky_deflake_candidates(records)
    assert len(cands) == 1
    assert cands[0]["nights"] == 1


def test_derive_flaky_candidates_none_when_no_leakers():
    # A real fail->fail red night carries no leakers -> no de-flake candidate
    # (a reproduced regression is never de-flaked away).
    records = [
        {"date": "2026-07-18", "result": "fail", "ts": "2026-07-18T03:00:00+00:00",
         "first_fail": {"gate": "python", "detail": "block_kind transient"}},
        _rec("2026-07-17", "pass"),
    ]
    assert vm.derive_flaky_deflake_candidates(records) == []


def test_derive_flaky_candidates_sorted_worst_first():
    records = [
        _leaker_fail("2026-07-10", ["python: tests/a.py"]),
        _leaker_fail("2026-07-11", ["python: tests/b.py"]),
        _leaker_fail("2026-07-12", ["python: tests/b.py"]),
    ]
    cands = vm.derive_flaky_deflake_candidates(records)
    assert [c["file"] for c in cands] == ["tests/b.py", "tests/a.py"]  # 2 nights, then 1


def test_flaky_file_key_is_stable_and_gate_scoped():
    k1 = vm.flaky_file_key("python", "tests/x.py")
    k2 = vm.flaky_file_key("python", "tests/x.py")
    assert k1 == k2
    assert k1.startswith("GATE-DEFLAKE-PYTHON-")
    # different file -> different key; different gate -> different token
    assert vm.flaky_file_key("python", "tests/y.py") != k1
    assert vm.flaky_file_key("vitest", "tests/x.py").startswith("GATE-DEFLAKE-VITEST-")


def test_green_gate_metric_flake_debt_counter_unfiled_vs_filed():
    records = [
        _leaker_fail("2026-07-10", ["python: tests/agent/test_delegate.py"]),
        _leaker_fail("2026-07-12", ["python: tests/agent/test_delegate.py"]),
        _leaker_fail("2026-07-13", ["python: tests/agent/test_delegate.py"]),
    ]
    # Nothing filed yet -> the guardrail counter is non-zero, high severity.
    m = vm._green_gate_metric(records, deflake_filed=set())
    fd = m["flake_debt"]
    assert fd["name"] == "flaky_neutralized_without_filed_deflake_task"
    assert fd["value"] == 1
    assert fd["severity"] == "high"
    assert fd["recurring_flakes"] == 1
    assert fd["recurring_flake_files"] == ["tests/agent/test_delegate.py"]

    # Once the file's key is in the filed set, the counter reaches 0 (AC-2b).
    key = vm.flaky_file_key("python", "tests/agent/test_delegate.py")
    m2 = vm._green_gate_metric(records, deflake_filed={key})
    assert m2["flake_debt"]["value"] == 0
    assert m2["flake_debt"]["severity"] == "low"
    # recurring escalation is independent of filing (AC-2c): still surfaced.
    assert m2["flake_debt"]["recurring_flakes"] == 1


def test_deflake_filed_roundtrip(tmp_path):
    path = tmp_path / "deflake-filed.json"
    assert vm.read_deflake_filed(path) == set()  # missing -> empty
    vm.write_deflake_filed({"GATE-DEFLAKE-PYTHON-aaaa", "GATE-DEFLAKE-VITEST-bbbb"}, path)
    assert vm.read_deflake_filed(path) == {
        "GATE-DEFLAKE-PYTHON-aaaa", "GATE-DEFLAKE-VITEST-bbbb"
    }
