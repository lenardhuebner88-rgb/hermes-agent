"""Tests for the distilled vision-metrics CLIs (hermes_cli.vision_metrics).

Covers ``hermes vision metrics-snapshot`` and ``hermes vision
record-gate-result``: the precomputed metric distillation written to
``~/.hermes/state/vision-metrics.json`` plus the structured green-gate
ledger the streak is derived from.

Every test writes to a TEMP state dir (``HERMES_VISION_STATE_DIR``) and an
isolated kanban DB — never the live state.
"""

from __future__ import annotations

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
             status="done"):
    conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, "
        "cost_usd) VALUES (?, ?, ?, ?, ?)",
        (tid, status, started_at, ended_at, cost_usd),
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
    # B: done but had failures -> not autonomous
    _add_task(conn, "B", consecutive_failures=2, completed_at=now - DAY)
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


def test_autonomy_percent_null_when_no_done(conn):
    snap = vm.compute_metrics_snapshot(conn, now=100 * DAY)
    a = snap["metrics"]["autonomy"]
    assert a["total_done"] == 0
    assert a["autonomy_pct"] is None


# ---------------------------------------------------------------------------
# Escalation-rate metric + paired counter
# ---------------------------------------------------------------------------

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
    assert c["pct_change"] is None
    assert c["coverage"]["subscription_only"] == 1


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
