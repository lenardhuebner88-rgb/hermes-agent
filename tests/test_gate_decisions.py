from hermes_cli.control_plane.gate_decisions import (
    ClassifyCrash,
    EnforceTimeout,
    ExtendStaleClaim,
    IterationBudgetContinuationCapped,
    KeepRunning,
    NoOp,
    ReclaimStale,
    RequestAutoReviewerChild,
    RequestIterationBudgetContinuation,
    RequestNeedsRevisionFix,
    RequestNotifierEmit,
    RequestReviewRequiredHandoff,
    TaskRunSnapshot,
    TaskSnapshot,
    decide_for_run,
)


NOW = 1_000


def task(**kwargs):
    base = {
        "id": "t1",
        "status": "running",
        "claim_lock": "host:worker",
        "claim_expires": NOW + 60,
        "worker_pid": 123,
        "started_at": NOW - 30,
        "current_run_id": 7,
    }
    base.update(kwargs)
    return TaskSnapshot(**base)


def run(**kwargs):
    base = {
        "id": 7,
        "task_id": "t1",
        "status": "running",
        "claim_lock": "host:worker",
        "claim_expires": NOW + 60,
        "worker_pid": 123,
        "started_at": NOW - 30,
        "pid_alive": True,
        "host_local": True,
    }
    base.update(kwargs)
    return TaskRunSnapshot(**base)


def test_running_ok_keeps_running():
    decision = decide_for_run(run(), task(), NOW)
    assert isinstance(decision, KeepRunning)
    assert decision.reason == "running_ok"


def test_remote_claim_keeps_running():
    decision = decide_for_run(run(host_local=False), task(), NOW)
    assert isinstance(decision, KeepRunning)
    assert decision.reason == "remote_claim"


def test_expired_claim_with_live_pid_extends():
    decision = decide_for_run(
        run(claim_expires=NOW - 1, pid_alive=True),
        task(claim_expires=NOW - 1),
        NOW,
    )
    assert isinstance(decision, ExtendStaleClaim)


def test_expired_claim_without_pid_reclaims():
    decision = decide_for_run(
        run(claim_expires=NOW - 1, worker_pid=None, pid_alive=None),
        task(claim_expires=NOW - 1, worker_pid=None),
        NOW,
    )
    assert isinstance(decision, ReclaimStale)
    assert decision.reason == "claim_expired"


def test_expired_claim_with_dead_pid_classifies_crash_first():
    decision = decide_for_run(
        run(claim_expires=NOW - 1, pid_alive=False, exit_kind="pid_not_alive"),
        task(claim_expires=NOW - 1),
        NOW,
    )
    assert isinstance(decision, ClassifyCrash)


def test_dead_pid_nonzero_exit_classifies_crash():
    decision = decide_for_run(
        run(pid_alive=False, exit_kind="nonzero_exit", exit_code=2),
        task(),
        NOW,
    )
    assert isinstance(decision, ClassifyCrash)
    assert decision.details["exit_kind"] == "nonzero_exit"
    assert decision.details["exit_code"] == 2


def test_dead_pid_signaled_classifies_crash():
    decision = decide_for_run(
        run(pid_alive=False, exit_kind="signaled", exit_code=9),
        task(),
        NOW,
    )
    assert isinstance(decision, ClassifyCrash)
    assert decision.details["exit_kind"] == "signaled"


def test_dead_pid_unknown_defaults_to_pid_not_alive():
    decision = decide_for_run(run(pid_alive=False), task(), NOW)
    assert isinstance(decision, ClassifyCrash)
    assert decision.details["exit_kind"] == "pid_not_alive"


def test_clean_exit_is_protocol_violation_and_will_block():
    decision = decide_for_run(
        run(pid_alive=False, exit_kind="clean_exit", exit_code=0),
        task(),
        NOW,
    )
    assert isinstance(decision, ClassifyCrash)
    assert decision.reason == "protocol_violation"
    assert decision.details["will_block"] is True


def test_crash_will_block_when_failure_limit_reached():
    decision = decide_for_run(
        run(pid_alive=False, exit_kind="nonzero_exit", exit_code=1),
        task(consecutive_failures=1),
        NOW,
        failure_limit=2,
    )
    assert isinstance(decision, ClassifyCrash)
    assert decision.details["will_block"] is True


def test_crash_respects_task_max_retries_override():
    decision = decide_for_run(
        run(pid_alive=False, exit_kind="nonzero_exit", exit_code=1),
        task(consecutive_failures=0, max_retries=1),
        NOW,
        failure_limit=10,
    )
    assert isinstance(decision, ClassifyCrash)
    assert decision.details["will_block"] is True


def test_timeout_enforced_after_limit():
    decision = decide_for_run(
        run(started_at=NOW - 61),
        task(started_at=NOW - 61, max_runtime_seconds=60),
        NOW,
    )
    assert isinstance(decision, EnforceTimeout)
    assert decision.details["elapsed_seconds"] == 61


def test_timeout_not_enforced_before_limit():
    decision = decide_for_run(
        run(started_at=NOW - 59),
        task(started_at=NOW - 59, max_runtime_seconds=60),
        NOW,
    )
    assert isinstance(decision, KeepRunning)


def test_timeout_will_block_when_failure_counter_reaches_limit():
    decision = decide_for_run(
        run(started_at=NOW - 61),
        task(started_at=NOW - 61, max_runtime_seconds=60, consecutive_failures=1),
        NOW,
        failure_limit=2,
    )
    assert isinstance(decision, EnforceTimeout)
    assert decision.details["will_block"] is True


def test_crash_wins_over_timeout():
    decision = decide_for_run(
        run(started_at=NOW - 100, pid_alive=False, exit_kind="nonzero_exit"),
        task(started_at=NOW - 100, max_runtime_seconds=60),
        NOW,
    )
    assert isinstance(decision, ClassifyCrash)


def test_stale_missing_heartbeat_reclaims_after_timeout():
    decision = decide_for_run(
        run(started_at=NOW - 500, last_heartbeat_at=None),
        task(started_at=NOW - 500, last_heartbeat_at=None),
        NOW,
        stale_timeout_seconds=300,
    )
    assert isinstance(decision, ReclaimStale)
    assert decision.reason == "heartbeat_missing"


def test_stale_old_heartbeat_reclaims_after_gap():
    decision = decide_for_run(
        run(started_at=NOW - 500, last_heartbeat_at=NOW - 400),
        task(started_at=NOW - 500, last_heartbeat_at=NOW - 400),
        NOW,
        stale_timeout_seconds=300,
        stale_heartbeat_gap_seconds=300,
    )
    assert isinstance(decision, ReclaimStale)
    assert decision.reason == "heartbeat_stale"


def test_recent_heartbeat_keeps_running_despite_elapsed_stale_window():
    decision = decide_for_run(
        run(started_at=NOW - 500, last_heartbeat_at=NOW - 10),
        task(started_at=NOW - 500, last_heartbeat_at=NOW - 10),
        NOW,
        stale_timeout_seconds=300,
    )
    assert isinstance(decision, KeepRunning)


def test_timeout_wins_over_stale_heartbeat():
    decision = decide_for_run(
        run(started_at=NOW - 500, last_heartbeat_at=None),
        task(started_at=NOW - 500, last_heartbeat_at=None, max_runtime_seconds=60),
        NOW,
        stale_timeout_seconds=300,
    )
    assert isinstance(decision, EnforceTimeout)


def test_stale_window_disabled_does_not_reclaim_for_heartbeat():
    decision = decide_for_run(
        run(started_at=NOW - 500, last_heartbeat_at=None),
        task(started_at=NOW - 500, last_heartbeat_at=None),
        NOW,
        stale_timeout_seconds=0,
    )
    assert isinstance(decision, KeepRunning)


def test_review_required_handoff_requested_for_blocked_run():
    decision = decide_for_run(
        run(status="blocked", outcome="blocked", summary="review-required: inspect"),
        task(status="blocked"),
        NOW,
    )
    assert isinstance(decision, RequestReviewRequiredHandoff)


def test_review_required_handoff_existing_is_noop():
    decision = decide_for_run(
        run(status="blocked", outcome="blocked", summary="review-required: inspect"),
        task(status="blocked", review_required_handoff_exists=True),
        NOW,
    )
    assert isinstance(decision, NoOp)


def test_review_required_wins_over_iteration_budget():
    decision = decide_for_run(
        run(status="blocked", outcome="blocked", summary="review-required: inspect"),
        task(status="blocked", iteration_budget_exhausted=True),
        NOW,
    )
    assert isinstance(decision, RequestReviewRequiredHandoff)


def test_iteration_budget_continuation_requested():
    decision = decide_for_run(
        run(status="blocked", outcome="blocked", summary="iteration budget"),
        task(status="blocked", iteration_budget_exhausted=True),
        NOW,
    )
    assert isinstance(decision, RequestIterationBudgetContinuation)


def test_iteration_budget_capped():
    decision = decide_for_run(
        run(status="blocked", outcome="blocked", summary="iteration budget"),
        task(
            status="blocked",
            iteration_budget_exhausted=True,
            iteration_continuations=1,
            iteration_continuation_cap=1,
        ),
        NOW,
    )
    assert isinstance(decision, IterationBudgetContinuationCapped)


def test_needs_revision_fix_requested():
    decision = decide_for_run(
        run(status="done", outcome="completed", metadata={"verdict": "NEEDS_REVISION"}),
        task(status="done"),
        NOW,
    )
    assert isinstance(decision, RequestNeedsRevisionFix)


def test_needs_revision_existing_fix_is_noop_when_not_notifier_candidate():
    decision = decide_for_run(
        run(status="done", outcome="completed", metadata={"verdict": "NEEDS_REVISION"}),
        task(status="done", needs_revision_fix_exists=True, notified=True),
        NOW,
    )
    assert isinstance(decision, NoOp)


def test_auto_reviewer_child_requested_for_done_coder():
    decision = decide_for_run(
        run(status="done", outcome="completed"),
        task(status="done", assignee="coder", notified=True),
        NOW,
    )
    assert isinstance(decision, RequestAutoReviewerChild)


def test_auto_reviewer_child_suppressed_is_noop_when_not_notifier_candidate():
    decision = decide_for_run(
        run(status="done", outcome="completed"),
        task(status="done", assignee="coder", auto_reviewer_child_suppressed=True, notified=True),
        NOW,
    )
    assert isinstance(decision, NoOp)


def test_notifier_emit_requested_for_done_unnotified_non_coder():
    decision = decide_for_run(
        run(status="done", outcome="completed"),
        task(status="done", assignee="planner"),
        NOW,
    )
    assert isinstance(decision, RequestNotifierEmit)


def test_done_notified_is_noop():
    decision = decide_for_run(
        run(status="done", outcome="completed"),
        task(status="done", assignee="planner", notified=True),
        NOW,
    )
    assert isinstance(decision, NoOp)


def test_decision_payload_is_stable():
    decision = decide_for_run(
        run(pid_alive=False, exit_kind="nonzero_exit", exit_code=2),
        task(),
        NOW,
    )
    assert decision.as_payload()["action"] == "classify_crash"
    assert decision.key() == decision.key()
