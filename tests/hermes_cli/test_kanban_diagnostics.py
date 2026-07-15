"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------


def test_hallucinated_cards_fires_on_blocked_event():
    task = _task(status="ready")
    events = [
        _event("created", ts=100),
        _event("completion_blocked_hallucination", ts=200,
               phantom_cards=["t_bad1", "t_bad2"],
               verified_cards=["t_good1"]),
    ]
    # ``now=300`` keeps the synthetic event timestamps in scope without
    # tripping the stranded_in_ready rule (events are 100/200 epoch
    # which time.time() would treat as ~50yr old).
    diags = kd.compute_task_diagnostics(task, events, [], now=300)
    halluc = [d for d in diags if d.kind == "hallucinated_cards"]
    assert len(halluc) == 1
    d = halluc[0]
    assert d.severity == "error"
    assert d.data["phantom_ids"] == ["t_bad1", "t_bad2"]
    # Generic recovery actions always available; comment action too.
    kinds = [a.kind for a in d.actions]
    assert "comment" in kinds
    assert "reassign" in kinds


def test_hallucinated_cards_clears_on_subsequent_completion():
    task = _task(status="done")
    events = [
        _event("completion_blocked_hallucination", ts=100, phantom_cards=["t_x"]),
        _event("completed", ts=200, summary="retry worked"),
    ]
    diags = kd.compute_task_diagnostics(task, events, [])
    assert diags == []


def test_prose_phantom_refs_fires_after_clean_completion():
    # Prose scan emits its event AFTER the completed event in the DB
    # path, but a subsequent clean completion clears it. Phantom id
    # must be valid hex — the scanner regex is ``t_[a-f0-9]{8,}``.
    task = _task(status="done")
    events = [
        _event("completed", ts=100, summary="referenced t_bad", result_len=0),
        _event("suspected_hallucinated_references", ts=101,
               phantom_refs=["t_deadbeef99"], source="completion_summary"),
    ]
    diags = kd.compute_task_diagnostics(task, events, [])
    assert len(diags) == 1
    assert diags[0].kind == "prose_phantom_refs"
    assert diags[0].severity == "warning"
    assert diags[0].data["phantom_refs"] == ["t_deadbeef99"]


def test_prose_phantom_refs_clears_on_later_clean_edit():
    task = _task(status="done")
    events = [
        _event("completed", ts=100, summary="bad"),
        _event("suspected_hallucinated_references", ts=101,
               phantom_refs=["t_ffff0000cc"]),
        _event("edited", ts=200, fields=["result", "summary"]),
    ]
    diags = kd.compute_task_diagnostics(task, events, [])
    assert diags == []


def test_repeated_failures_fires_at_threshold_on_spawn():
    """A task with multiple spawn_failed runs gets a spawn-flavoured
    diagnostic (title mentions 'spawn', suggested action is ``doctor``).
    """
    task = _task(status="ready", consecutive_failures=3,
                 last_failure_error="Profile 'debugger' does not exist")
    runs = [
        _run(outcome="spawn_failed", run_id=1),
        _run(outcome="spawn_failed", run_id=2),
        _run(outcome="spawn_failed", run_id=3),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "repeated_failures"
    assert d.severity == "error"
    # CLI hints are what operators actually need here.
    suggested = [a.label for a in d.actions if a.suggested]
    assert any("doctor" in s for s in suggested)


def test_repeated_failures_fires_on_timeout_loop():
    """The rule surfaces for timeout loops too — that's the point of
    unifying the counter. Suggested action is 'check logs', not
    'fix profile'."""
    task = _task(status="ready", consecutive_failures=3,
                 last_failure_error="elapsed 600s > limit 300s")
    runs = [
        _run(outcome="timed_out", run_id=1),
        _run(outcome="timed_out", run_id=2),
        _run(outcome="timed_out", run_id=3),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "repeated_failures"
    assert d.data["most_recent_outcome"] == "timed_out"
    suggested = [a.label for a in d.actions if a.suggested]
    assert any("log" in s.lower() for s in suggested)


def test_repeated_failures_escalates_to_critical():
    task = _task(consecutive_failures=6, last_failure_error="boom")
    diags = kd.compute_task_diagnostics(task, [], [])
    assert diags[0].severity == "critical"


def test_repeated_failures_below_threshold_silent():
    task = _task(consecutive_failures=1)
    assert kd.compute_task_diagnostics(task, [], []) == []


def test_repeated_failures_default_matches_dispatcher_failure_limit():
    """Default dispatcher auto-blocks at 2 failures, so diagnostics must
    also surface at 2 instead of waiting for the stale threshold of 3.
    """
    task = _task(status="blocked", consecutive_failures=2,
                 last_failure_error="elapsed 600s > limit 300s")
    runs = [_run(outcome="timed_out", run_id=1)]
    diags = kd.compute_task_diagnostics(task, [], runs)
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    d = repeated[0]
    assert d.data["failure_threshold"] == 2
    assert d.data["failure_limit"] == 2
    assert "default 5" not in d.detail
    assert "configured for 2" in d.detail


def test_repeated_failures_derives_threshold_from_kanban_failure_limit():
    task = _task(status="ready", consecutive_failures=2,
                 last_failure_error="Profile 'debugger' does not exist")
    runs = [_run(outcome="spawn_failed", run_id=1)]
    assert kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 4}
    ) == []

    task = _task(status="blocked", consecutive_failures=4,
                 last_failure_error="Profile 'debugger' does not exist")
    diags = kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 4}
    )
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    assert repeated[0].data["failure_threshold"] == 4
    assert repeated[0].data["failure_limit"] == 4


def test_repeated_failures_explicit_threshold_overrides_failure_limit():
    task = _task(status="ready", consecutive_failures=3,
                 last_failure_error="Profile 'debugger' does not exist")
    runs = [_run(outcome="spawn_failed", run_id=1)]
    diags = kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 5, "failure_threshold": 3}
    )
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    assert repeated[0].data["failure_threshold"] == 3
    assert repeated[0].data["failure_limit"] == 5


def test_config_from_kanban_config_preserves_explicit_diagnostics_threshold():
    cfg = kd.config_from_kanban_config({
        "failure_limit": 5,
        "diagnostics": {"failure_threshold": 3},
    })
    assert cfg["failure_threshold"] == 3
    assert cfg["failure_limit"] == 5


def test_repeated_crashes_counts_trailing_streak_only():
    task = _task(status="ready", assignee="crashy")
    runs = [
        _run(outcome="completed", run_id=1),
        _run(outcome="crashed", run_id=2, error="OOM"),
        _run(outcome="crashed", run_id=3, error="OOM again"),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "repeated_crashes"
    # 2 consecutive crashes at the end → default threshold 2 → error severity.
    assert d.severity == "error"
    assert d.data["consecutive_crashes"] == 2


def test_repeated_crashes_breaks_on_recent_success():
    task = _task(status="ready", assignee="fixed")
    runs = [
        _run(outcome="crashed", run_id=1),
        _run(outcome="crashed", run_id=2),
        _run(outcome="completed", run_id=3),
    ]
    assert kd.compute_task_diagnostics(task, [], runs) == []


def test_repeated_crashes_breaks_on_integration_park():
    """Regression: a parked integration (the worker ran to completion; only the
    merge parked) breaks the crash streak — preserving the pre-relabel behavior
    when a park was stamped 'completed'."""
    task = _task(status="ready", assignee="fixed")
    runs = [
        _run(outcome="crashed", run_id=1),
        _run(outcome="crashed", run_id=2),
        _run(outcome="integration_parked", run_id=3),
    ]
    assert kd.compute_task_diagnostics(task, [], runs) == []


def test_repeated_crashes_escalates_on_many_crashes():
    task = _task(status="ready", assignee="x")
    runs = [_run(outcome="crashed", run_id=i) for i in range(1, 6)]  # 5 in a row
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert diags[0].severity == "critical"


def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    # Typed kind so blocked_without_kind does not also fire.
    task = _task(status="blocked", block_kind="needs_input")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48


def test_stuck_in_blocked_silent_with_recent_comment():
    now = int(time.time())
    task = _task(status="blocked", block_kind="needs_input")
    events = [
        _event("blocked", ts=now - 3600 * 48),
        _event("commented", ts=now - 3600 * 2, author="human"),
    ]
    assert kd.compute_task_diagnostics(task, events, [], now=now) == []


def test_stuck_in_blocked_silent_when_not_blocked():
    task = _task(status="ready")
    events = [_event("blocked", ts=1000)]
    assert kd.compute_task_diagnostics(task, events, [], now=9999999) == []


def test_blocked_without_kind_fires_when_kind_missing():
    now = int(time.time())
    task = _task(status="blocked", block_kind=None)
    events = [_event("blocked", ts=now - 60, reason="token cap")]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    kinds = {d.kind for d in diags}
    assert "blocked_without_kind" in kinds
    d = next(d for d in diags if d.kind == "blocked_without_kind")
    assert d.severity == "warning"
    assert d.data.get("requires_operator_classification") is True


def test_blocked_without_kind_silent_when_kind_set():
    now = int(time.time())
    task = _task(status="blocked", block_kind="capacity")
    events = [
        {
            "kind": "blocked",
            "created_at": now - 60,
            "payload": {"reason": "token cap", "kind": "capacity"},
        }
    ]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert all(d.kind != "blocked_without_kind" for d in diags)


def test_reviewer_role_tool_mismatch_fires_on_imperative_gate_request():
    task = _task(
        id="t_rolefit001",
        assignee="reviewer",
        status="ready",
        title="Reviewer verdict",
        body=(
            "Reviewer: führe reale Gates aus: scripts/run_tests.sh, "
            "ruff, git diff --check."
        ),
    )

    diags = kd.compute_task_diagnostics(task, [], [], now=1_000)

    rolefit = [d for d in diags if d.kind == "reviewer_role_tool_mismatch"]
    assert len(rolefit) == 1
    d = rolefit[0]
    assert d.severity == "warning"
    assert d.data["assignee"] == "reviewer"
    assert "führe reale gates aus" in d.data["matched_imperatives"]
    assert d.data["recommended_shape"] == (
        "coder_or_verifier_evidence_then_reviewer_verdict"
    )


def test_reviewer_role_tool_mismatch_fires_on_recovered_reale_gates_wording():
    task = _task(
        id="t_9cbfcbe7",
        assignee="reviewer",
        status="blocked",
        title="Reviewer: recovered gate evidence",
        body=(
            "Reale Gates laufen mindestens: scripts/run_tests.sh --focused, "
            "py_compile, ruff, git diff --check."
        ),
    )

    diags = kd.compute_task_diagnostics(task, [], [], now=1_000)

    rolefit = [d for d in diags if d.kind == "reviewer_role_tool_mismatch"]
    assert len(rolefit) == 1
    assert rolefit[0].severity == "warning"
    assert "reale gates laufen" in rolefit[0].data["matched_imperatives"]


def test_reviewer_role_tool_mismatch_prefers_direct_imperative_over_weak_evidence():
    task = _task(
        assignee="reviewer",
        status="ready",
        title="Reviewer verdict",
        body=(
            "Parent evidence is attached for context. "
            "Reale Gates laufen mindestens: scripts/run_tests.sh, py_compile, "
            "ruff, git diff --check."
        ),
    )

    diags = kd.compute_task_diagnostics(task, [], [], now=1_000)

    rolefit = [d for d in diags if d.kind == "reviewer_role_tool_mismatch"]
    assert len(rolefit) == 1
    assert "reale gates laufen" in rolefit[0].data["matched_imperatives"]


def test_reviewer_role_tool_mismatch_prefers_direct_run_pytest_over_weak_evidence():
    task = _task(
        assignee="reviewer",
        status="ready",
        title="Reviewer verdict",
        body=(
            "Parent evidence is attached for context. "
            "Reviewer: run pytest and git diff --check in the repo."
        ),
    )

    diags = kd.compute_task_diagnostics(task, [], [], now=1_000)

    rolefit = [d for d in diags if d.kind == "reviewer_role_tool_mismatch"]
    assert len(rolefit) == 1
    assert "run pytest" in rolefit[0].data["matched_imperatives"]


def test_reviewer_role_tool_mismatch_ignores_evidence_references():
    task = _task(
        assignee="reviewer",
        status="ready",
        title="Reviewer verdict",
        body=(
            "Parent evidence says run tests passed, scripts/run_tests.sh passed, "
            "and ruff passed. Confirm the evidence supports a verdict."
        ),
    )

    diags = kd.compute_task_diagnostics(task, [], [], now=1_000)

    assert [d for d in diags if d.kind == "reviewer_role_tool_mismatch"] == []


def test_reviewer_role_tool_mismatch_ignores_passed_parent_test_results():
    task = _task(
        assignee="reviewer",
        status="ready",
        title="Reviewer verdict",
        body=(
            "Parent handoff says test results passed: run pytest "
            "tests/hermes_cli/test_kanban_diagnostics.py -q passed, "
            "ruff passed, and git diff --check passed. Decide whether this "
            "evidence supports approval."
        ),
    )

    diags = kd.compute_task_diagnostics(task, [], [], now=1_000)

    assert [d for d in diags if d.kind == "reviewer_role_tool_mismatch"] == []


def test_reviewer_role_tool_mismatch_ignores_verdict_only_cards():
    task = _task(
        assignee="reviewer",
        status="ready",
        title="Reviewer verdict",
        body=(
            "Verdict-only: read parent evidence only. Do not run tests; "
            "only assess the reported pytest and ruff output."
        ),
    )

    diags = kd.compute_task_diagnostics(task, [], [], now=1_000)

    assert [d for d in diags if d.kind == "reviewer_role_tool_mismatch"] == []


def test_reviewer_role_tool_mismatch_works_on_real_db_row(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="Reviewer must run gates",
            body="Reviewer: run pytest and git diff --check in the repo.",
            assignee="reviewer",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        diags = kd.compute_task_diagnostics(row, [], [], now=1_000)
        assert [d.kind for d in diags] == ["reviewer_role_tool_mismatch"]
    finally:
        conn.close()


def test_superseded_blocked_review_artifact_fires_from_event_marker():
    task = _task(
        status="blocked",
        assignee="verifier",
        title="Verifier review artifact",
    )
    events = [
        _event("blocked", ts=100, reason="superseded; audit-only artifact"),
    ]

    diags = kd.compute_task_diagnostics(task, events, [], now=200)

    artifact = [d for d in diags if d.kind == "superseded_blocked_review_artifact"]
    assert len(artifact) == 1
    d = artifact[0]
    assert d.severity == "warning"
    assert "graph state was not checked" in d.detail
    assert d.data["audit_only_marker_present"] is True
    assert d.data["graph_state_checked"] is False
    assert {"superseded", "audit-only"}.issubset(set(d.data["matched_terms"]))


def test_superseded_blocked_review_artifact_silent_without_marker():
    task = _task(status="blocked", assignee="verifier", title="Verifier review")
    events = [_event("blocked", ts=100, reason="needs human decision")]

    diags = kd.compute_task_diagnostics(task, events, [], now=200)

    assert [d for d in diags if d.kind == "superseded_blocked_review_artifact"] == []


def test_stale_review_block_needs_classification_fires_after_threshold():
    now = 20_000
    task = _task(status="blocked", assignee="reviewer", title="Reviewer verdict")
    events = [_event("blocked", ts=now - 3 * 3600, reason="needs review")]

    diags = kd.compute_task_diagnostics(task, events, [], now=now)

    stale = [d for d in diags if d.kind == "stale_review_block_needs_classification"]
    assert len(stale) == 1
    d = stale[0]
    assert d.severity == "warning"
    assert "graph state was not checked" in d.detail
    assert d.data["blocked_age_seconds"] == 3 * 3600
    assert d.data["review_like"] is True
    assert d.data["graph_state_checked"] is False
    assert d.data["requires_operator_classification"] is True


def test_stale_review_block_suppressed_by_supersede_marker():
    now = 10_000
    task = _task(status="blocked", assignee="reviewer", title="Reviewer verdict")
    events = [_event("blocked", ts=now - 3 * 3600, reason="superseded audit-only")]

    diags = kd.compute_task_diagnostics(task, events, [], now=now)

    assert [d for d in diags if d.kind == "superseded_blocked_review_artifact"]
    assert [d for d in diags if d.kind == "stale_review_block_needs_classification"] == []


def test_stale_review_block_escalates_after_error_threshold():
    now = 100_000
    task = _task(status="blocked", assignee="reviewer", title="Reviewer verdict")
    events = [_event("blocked", ts=now - 25 * 3600, reason="needs review")]

    diags = kd.compute_task_diagnostics(task, events, [], now=now)

    stale = [d for d in diags if d.kind == "stale_review_block_needs_classification"]
    assert len(stale) == 1
    assert stale[0].severity == "error"


def test_repeated_crashes_surfaces_actual_error_in_title():
    """The title should lead with the actual error text so operators
    see WHAT broke (e.g. rate-limit, auth, OOM) without opening logs.
    """
    task = _task(status="ready", assignee="x")
    runs = [
        _run(outcome="crashed", run_id=1, error="openai: 429 Too Many Requests"),
        _run(outcome="crashed", run_id=2, error="openai: 429 Too Many Requests"),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert "429" in d.title
    assert "Too Many Requests" in d.title
    # Full error in detail.
    assert "429 Too Many Requests" in d.detail


def test_repeated_crashes_no_error_fallback_title():
    task = _task(status="ready", assignee="x")
    runs = [
        _run(outcome="crashed", run_id=1, error=None),
        _run(outcome="crashed", run_id=2, error=None),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert "no error recorded" in diags[0].title


def test_repeated_failures_surfaces_actual_error_in_title():
    task = _task(consecutive_failures=5,
                 last_failure_error="insufficient_quota: billing limit reached")
    diags = kd.compute_task_diagnostics(task, [], [])
    assert len(diags) == 1
    d = diags[0]
    assert "insufficient_quota" in d.title or "billing limit" in d.title
    assert "insufficient_quota" in d.detail


def test_repeated_crashes_truncates_huge_tracebacks():
    """Full Python tracebacks can be tens of KB. The title stays one
    line (≤160 chars); the detail caps at 500 chars + ellipsis so the
    card doesn't explode visually."""
    huge = "Traceback (most recent call last):\n" + ("  File\n" * 500)
    task = _task(status="ready")
    runs = [
        _run(outcome="crashed", run_id=1, error=huge),
        _run(outcome="crashed", run_id=2, error=huge),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    d = diags[0]
    # Title only the first line, capped.
    assert "\n" not in d.title
    assert len(d.title) < 250
    # Detail contains the snippet with ellipsis.
    assert d.detail.endswith("…") or len(d.detail) < 700


# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------


def test_diagnostics_sorted_critical_first():
    """A task with both a critical (many spawn failures) and a warning
    (prose phantoms) diagnostic should list the critical one first."""
    task = _task(status="done", consecutive_failures=10,
                 last_failure_error="nope")
    events = [
        _event("completed", ts=100, summary="referenced t_missing"),
        _event("suspected_hallucinated_references", ts=101,
               phantom_refs=["t_missing11"]),
    ]
    diags = kd.compute_task_diagnostics(task, events, [])
    kinds = [d.kind for d in diags]
    assert kinds[0] == "repeated_failures"  # critical
    assert "prose_phantom_refs" in kinds


# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------


def test_broken_rule_is_isolated(monkeypatch):
    def _bad_rule(task, events, runs, now, cfg):
        raise RuntimeError("synthetic rule bug")

    # Insert a broken rule at the front of the registry; subsequent
    # rules should still run and produce their diagnostics.
    monkeypatch.setattr(kd, "_RULES", [_bad_rule] + kd._RULES)

    task = _task(consecutive_failures=5, last_failure_error="e")
    diags = kd.compute_task_diagnostics(task, [], [])
    # The broken rule silently drops, the real one still fires.
    kinds = [d.kind for d in diags]
    assert "repeated_failures" in kinds


# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"


def test_stranded_in_ready_silent_below_threshold():
    """A ready task only 10 min old should NOT fire."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [_event("created", ts=now - 10 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_skips_non_ready_status():
    """Tasks not in ready status are out of scope (running tasks have
    their own crash / failure rules)."""
    now = 100_000
    for status in ("running", "blocked", "done", "todo", "triage"):
        task = _task(status=status, assignee="demo")
        events = [_event("created", ts=now - 6 * 3600)]
        diags = kd.compute_task_diagnostics(task, events, [], now=now)
        assert [d for d in diags if d.kind == "stranded_in_ready"] == [], status


def test_stranded_in_ready_skips_unassigned_tasks():
    """Empty assignee = `skipped_unassigned` on the dispatcher already.
    Don't double-flag here."""
    now = 100_000
    task = _task(status="ready", assignee="", claim_lock=None)
    events = [_event("created", ts=now - 6 * 3600)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_skips_claimed_tasks():
    """A live claim_lock means a worker is on it — even an old one. Don't
    second-guess: the run-level liveness signal owns that decision."""
    now = 100_000
    task = _task(
        status="ready", assignee="demo", claim_lock="run_xyz",
    )
    events = [_event("created", ts=now - 6 * 3600)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_uses_latest_ready_transition():
    """When multiple ready-transition events exist, the rule should
    age-from the most recent — a task reclaimed 20 min ago is NOT
    stranded for 6h even if it was first created 6h ago."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    events = [
        _event("created", ts=now - 6 * 3600),       # 6 h ago
        _event("reclaimed", ts=now - 20 * 60),      # 20 min ago — wins
    ]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


@pytest.mark.parametrize(
    "hold_kind",
    (
        "repo_serialized",
        "chain_worktree_serialized",
        "budget_held",
        "role_fit_held",
    ),
)
def test_stranded_in_ready_skips_latest_policy_hold(hold_kind):
    """A dispatcher policy hold is intentional, not a missing worker."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [
        _event("created", ts=now - 6 * 3600),
        _event(hold_kind, ts=now - 5 * 60),
    ]

    diags = kd.compute_task_diagnostics(task, events, [], now=now)

    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_severity_escalates_with_age():
    """warning → error → critical at 2x and 6x threshold."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    # Default threshold = 1800s.
    cases = [
        (45 * 60, "warning"),    # 1.5x → warning
        (90 * 60, "error"),      # 3x → error
        (4 * 3600, "critical"),  # 8x → critical
    ]
    for age, expected in cases:
        events = [_event("created", ts=now - age)]
        diags = kd.compute_task_diagnostics(task, events, [], now=now)
        stranded = [d for d in diags if d.kind == "stranded_in_ready"]
        assert len(stranded) == 1, f"age={age}"
        assert stranded[0].severity == expected, (
            f"age={age} expected {expected}, got {stranded[0].severity}"
        )


def test_stranded_in_ready_respects_config_override():
    """Config override changes the threshold."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    events = [_event("created", ts=now - 10 * 60)]  # 10 min
    # Default 30 min — wouldn't fire.
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []
    # Lower the threshold to 5 min — now it fires.
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
        config={"stranded_threshold_seconds": 5 * 60},
    )
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1


def test_stranded_in_ready_falls_back_to_created_at():
    """When events have no ready-transition kind, the rule falls back
    to the task's ``created_at`` so an ancient stranded task isn't
    invisible just because its events got pruned."""
    now = 100_000
    task = _task(
        status="ready", assignee="demo", created_at=now - 4 * 3600,
    )
    # No qualifying events.
    events = [_event("commented", ts=now - 100)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].data["age_seconds"] == 4 * 3600


def test_stranded_in_ready_works_on_real_db_row(kanban_home):
    """Round-trip through real kanban_db.connect() — confirms the rule
    works on sqlite3.Row objects, not just dicts."""
    import time as _t
    conn = kb.connect()
    try:
        # Create a task and force its created_at into the past.
        tid = kb.create_task(conn, title="stranded one", assignee="ghost")
        old_ts = int(_t.time()) - 90 * 60  # 90 min old
        conn.execute(
            "UPDATE tasks SET status = 'ready', created_at = ? WHERE id = ?",
            (old_ts, tid),
        )
        conn.commit()

        task_row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at",
            (tid,),
        ).fetchall())
        # Override created event timestamps too so age calc lines up.
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ?",
            (old_ts, tid),
        )
        conn.commit()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ?", (tid,),
        ).fetchall())

        diags = kd.compute_task_diagnostics(task_row, events, [])
        stranded = [d for d in diags if d.kind == "stranded_in_ready"]
        assert len(stranded) == 1
        assert stranded[0].data["assignee"] == "ghost"
    finally:
        conn.close()



# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")


def test_triage_aux_unavailable_silent_without_config_context():
    """Low-level callers passing no config dict should not see this rule."""
    diags = kd.compute_task_diagnostics(_triage_task(), [], [])
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_unavailable_silent_when_main_model_visible():
    """Default `provider: auto` falls back to the main model — no warning."""
    config = {
        "auxiliary": {},
        "model": {"provider": "openrouter", "default": "qwen/qwen3"},
        "kanban": {"auto_decompose": True},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_unavailable_silent_when_decomposer_explicit():
    """User explicitly configured decomposer → no warning, even without main."""
    config = {
        "auxiliary": {
            "kanban_decomposer": {"provider": "openrouter", "model": "qwen/qwen3"},
        },
        "kanban": {"auto_decompose": True},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_unavailable_fires_auto_decompose_on_no_fallback():
    """auto_decompose=True, no decomposer, no main model → warn about decomposer."""
    config = {
        "auxiliary": {},
        "kanban": {"auto_decompose": True},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    triage = [d for d in diags if d.kind == "triage_aux_unavailable"]
    assert len(triage) == 1
    d = triage[0]
    assert d.severity == "warning"
    assert "decomposer" in d.title.lower()
    assert d.data["auto_decompose"] is True
    assert d.data["primary_slot"] == "auxiliary.kanban_decomposer"
    suggested = [a for a in d.actions if a.suggested]
    assert suggested
    assert "auxiliary.kanban_decomposer" in suggested[0].payload["command"]


def test_triage_aux_unavailable_fires_auto_decompose_off_points_at_specifier():
    """auto_decompose=False → primary is specifier, not decomposer."""
    config = {
        "auxiliary": {},
        "kanban": {"auto_decompose": False},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    triage = [d for d in diags if d.kind == "triage_aux_unavailable"]
    assert len(triage) == 1
    d = triage[0]
    assert "specifier" in d.title.lower()
    assert d.data["auto_decompose"] is False
    assert d.data["primary_slot"] == "auxiliary.triage_specifier"
    # And it should offer the manual specify command as an action
    labels = [a.label for a in d.actions]
    assert any("hermes kanban specify" in l for l in labels)


def test_triage_aux_unavailable_skips_non_triage_tasks():
    config = {"auxiliary": {}, "kanban": {"auto_decompose": True}}
    task = _task(status="todo")
    diags = kd.compute_task_diagnostics(task, [], [], config=config)
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_status_recognises_auto_default_as_not_explicit():
    """Default `provider: auto` with empty fields → not 'explicit'."""
    status = kd.triage_aux_status({
        "auxiliary": {
            "kanban_decomposer": {"provider": "auto", "model": ""},
        },
        "kanban": {},
    })
    assert status is not None
    assert status["decomposer_explicit"] is False


def test_triage_aux_status_recognises_explicit_model_only():
    """Even with provider=auto, a non-empty model counts as explicit."""
    status = kd.triage_aux_status({
        "auxiliary": {
            "kanban_decomposer": {"provider": "auto", "model": "qwen/qwen3"},
        },
        "kanban": {},
    })
    assert status is not None
    assert status["decomposer_explicit"] is True


def test_config_from_runtime_config_carries_aux_and_model():
    cfg = kd.config_from_runtime_config({
        "kanban": {"failure_limit": 5, "auto_decompose": False},
        "auxiliary": {"kanban_decomposer": {"provider": "openrouter"}},
        "model": {"provider": "openrouter", "default": "qwen/qwen3"},
    })
    assert cfg["failure_threshold"] == 5
    assert cfg["kanban"]["auto_decompose"] is False
    assert cfg["auxiliary"]["kanban_decomposer"]["provider"] == "openrouter"
    assert cfg["model"]["default"] == "qwen/qwen3"


def test_config_from_runtime_config_handles_empty_input():
    assert kd.config_from_runtime_config(None) == {}
    assert kd.config_from_runtime_config({}) == {}


def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True


# ---------------------------------------------------------------------------
# K4 — descendants_blocked_by_stuck_parent (cross-task, conn-bearing)
# ---------------------------------------------------------------------------


def _block_parent_long_ago(conn, parent_id, *, age_hours=48):
    """Claim + block a parent and backdate its blocked event to ``age_hours``."""
    kb.claim_task(conn, parent_id)  # ready -> running (block needs running)
    kb.block_task(conn, parent_id, reason="needs operator input")
    conn.execute(
        "UPDATE task_events SET created_at = ? "
        "WHERE task_id = ? AND kind = 'blocked'",
        (int(time.time()) - 3600 * age_hours, parent_id),
    )
    conn.commit()


def test_descendants_blocked_by_stuck_parent_flags_todo_child(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="blocker work", assignee="coder")
        child = kb.create_task(
            conn, title="waits on parent", assignee="coder", parents=[parent],
        )
        # A linked child of a not-done parent sits in todo.
        assert kb.get_task(conn, child).status == "todo"
        _block_parent_long_ago(conn, parent, age_hours=48)

        out = kd.find_descendants_blocked_by_stuck_parent(conn)

    assert child in out
    diags = out[child]
    assert any(d.kind == "descendants_blocked_by_stuck_parent" for d in diags)
    # The suggested remediation unblocks the stuck PARENT, not the child.
    cmds = [
        a.payload.get("command")
        for d in diags
        for a in d.actions
        if isinstance(a.payload, dict)
    ]
    assert any(f"unblock {parent}" in (c or "") for c in cmds)
    assert parent in diags[0].data["blocked_parents"]


def test_descendants_blocked_by_stuck_parent_transitive(kanban_home):
    """A grandchild two links below the stuck parent is still flagged."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="root blocker", assignee="coder")
        mid = kb.create_task(
            conn, title="mid", assignee="coder", parents=[parent],
        )
        grandchild = kb.create_task(
            conn, title="grandchild", assignee="coder", parents=[mid],
        )
        _block_parent_long_ago(conn, parent, age_hours=72)

        out = kd.find_descendants_blocked_by_stuck_parent(conn)

    # Both the direct todo child and the transitive todo grandchild are stranded.
    assert mid in out
    assert grandchild in out


def test_descendants_blocked_by_stuck_parent_silent_when_block_recent(kanban_home):
    """A freshly blocked parent is below threshold → no stranding flag."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="blocker", assignee="coder")
        child = kb.create_task(
            conn, title="child", assignee="coder", parents=[parent],
        )
        kb.claim_task(conn, parent)
        kb.block_task(conn, parent, reason="just now")  # recent, not "long"

        out = kd.find_descendants_blocked_by_stuck_parent(conn)

    assert child not in out


def test_descendants_blocked_by_stuck_parent_silent_when_parent_recovered(kanban_home):
    """An unblocked (non-sticky) parent strands nobody."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="blocker", assignee="coder")
        child = kb.create_task(
            conn, title="child", assignee="coder", parents=[parent],
        )
        _block_parent_long_ago(conn, parent, age_hours=48)
        kb.unblock_task(conn, parent)  # latest block/unblock event is unblock

        out = kd.find_descendants_blocked_by_stuck_parent(conn)

    assert child not in out


# ---------------------------------------------------------------------------
# orphaned_worktree (worker isolation, Phase 4)
# ---------------------------------------------------------------------------


class TestOrphanedWorktree:
    def _wt(self, tmp_path):
        wt = tmp_path / "repo" / ".worktrees" / "kanban" / "t_orph01"
        wt.mkdir(parents=True)
        return wt

    def test_fires_for_old_terminal_task_with_existing_worktree(self, tmp_path):
        wt = self._wt(tmp_path)
        now = int(time.time())
        task = _task(
            id="t_orph01", status="done",
            workspace_path=str(wt), completed_at=now - 72 * 3600,
        )
        diags = kd.compute_task_diagnostics(task, [], [], now=now)
        kinds = [d.kind for d in diags]
        assert "orphaned_worktree" in kinds
        d = next(d for d in diags if d.kind == "orphaned_worktree")
        assert d.severity == "warning"
        assert str(wt) in d.detail

    def test_silent_when_worktree_already_removed(self, tmp_path):
        wt = tmp_path / "repo" / ".worktrees" / "kanban" / "t_orph02"  # never created
        now = int(time.time())
        task = _task(
            id="t_orph02", status="done",
            workspace_path=str(wt), completed_at=now - 72 * 3600,
        )
        diags = kd.compute_task_diagnostics(task, [], [], now=now)
        assert all(d.kind != "orphaned_worktree" for d in diags)

    def test_silent_within_grace_period_and_for_open_tasks(self, tmp_path):
        wt = self._wt(tmp_path)
        now = int(time.time())
        fresh = _task(
            id="t_orph01", status="done",
            workspace_path=str(wt), completed_at=now - 3600,
        )
        assert all(
            d.kind != "orphaned_worktree"
            for d in kd.compute_task_diagnostics(fresh, [], [], now=now)
        )
        running = _task(
            id="t_orph01", status="running",
            workspace_path=str(wt), completed_at=None,
        )
        assert all(
            d.kind != "orphaned_worktree"
            for d in kd.compute_task_diagnostics(running, [], [], now=now)
        )

    def test_silent_for_plain_workspace(self, tmp_path):
        plain = tmp_path / "somewhere"
        plain.mkdir()
        now = int(time.time())
        task = _task(
            id="t_orph03", status="done",
            workspace_path=str(plain), completed_at=now - 72 * 3600,
        )
        diags = kd.compute_task_diagnostics(task, [], [], now=now)
        assert all(d.kind != "orphaned_worktree" for d in diags)


def _run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.strip()


def _init_repo_with_stranded_root_branch(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-b", "main")
    _run_git(repo, "config", "user.email", "test@example.invalid")
    _run_git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "base")
    _run_git(repo, "checkout", "-b", "kanban/t_root")
    (repo / "work.txt").write_text("recover me\n", encoding="utf-8")
    _run_git(repo, "add", "work.txt")
    _run_git(repo, "commit", "-m", "root work")
    head_sha = _run_git(repo, "rev-parse", "HEAD")
    return repo, head_sha


def _make_blocked_decompose_root(conn, repo: Path, *, status: str = "blocked") -> str:
    root = kb.create_task(
        conn,
        title="decompose root",
        assignee="coder",
        workspace_kind="dir",
        workspace_path=str(repo),
    )
    conn.execute(
        "UPDATE tasks SET status = ?, branch_name = ? WHERE id = ?",
        (status, "kanban/t_root", root),
    )
    kb._append_event(conn, root, "decomposed", {"children": ["t_child"]})
    conn.commit()
    return root


def test_stranded_decompose_root_branch_reports_unmerged_head_sha(kanban_home, tmp_path):
    repo, head_sha = _init_repo_with_stranded_root_branch(tmp_path)
    with kb.connect() as conn:
        root = _make_blocked_decompose_root(conn, repo)

        out = kd.find_stranded_decompose_root_branches(conn, repo_root=repo)

    assert root in out
    diag = out[root][0]
    assert diag.kind == "stranded_decompose_root_branch"
    assert diag.data["branch_name"] == "kanban/t_root"
    assert diag.data["head_sha"] == head_sha
    assert head_sha in diag.detail
    assert "git cherry-pick" in diag.detail
    assert "git cherry-pick" in diag.data["recovery_hint"]


def test_stranded_decompose_root_branch_silent_when_head_reachable_from_main(
    kanban_home, tmp_path,
):
    repo, _head_sha = _init_repo_with_stranded_root_branch(tmp_path)
    _run_git(repo, "checkout", "main")
    _run_git(repo, "merge", "--ff-only", "kanban/t_root")
    with kb.connect() as conn:
        root = _make_blocked_decompose_root(conn, repo)

        out = kd.find_stranded_decompose_root_branches(conn, repo_root=repo)

    assert root not in out


def test_stranded_decompose_root_branch_silent_for_running_root(kanban_home, tmp_path):
    repo, _head_sha = _init_repo_with_stranded_root_branch(tmp_path)
    with kb.connect() as conn:
        root = _make_blocked_decompose_root(conn, repo, status="running")

        out = kd.find_stranded_decompose_root_branches(conn, repo_root=repo)

    assert root not in out
