"""Pure Kanban gate decision builder.

This module intentionally has no database access and emits no events. Callers
build snapshots from their own storage layer, pass them through
``decide_for_run``, and decide separately whether/how to act.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


DEFAULT_STALE_HEARTBEAT_GAP_SECONDS = 3600
DEFAULT_FAILURE_LIMIT = 2


@dataclass(frozen=True)
class TaskSnapshot:
    id: str
    status: str
    claim_lock: Optional[str] = None
    claim_expires: Optional[int] = None
    worker_pid: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    started_at: Optional[int] = None
    current_run_id: Optional[int] = None
    max_runtime_seconds: Optional[int] = None
    consecutive_failures: int = 0
    max_retries: Optional[int] = None
    assignee: Optional[str] = None
    completed_at: Optional[int] = None
    body: Optional[str] = None
    review_required_handoff_exists: bool = False
    auto_reviewer_child_exists: bool = False
    auto_reviewer_child_suppressed: bool = False
    needs_revision_fix_exists: bool = False
    notified: bool = False
    iteration_budget_exhausted: bool = False
    iteration_continuations: int = 0
    iteration_continuation_cap: int = 1


@dataclass(frozen=True)
class TaskRunSnapshot:
    id: int
    task_id: str
    status: str
    claim_lock: Optional[str] = None
    claim_expires: Optional[int] = None
    worker_pid: Optional[int] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    started_at: Optional[int] = None
    ended_at: Optional[int] = None
    outcome: Optional[str] = None
    summary: Optional[str] = None
    metadata: Optional[Mapping[str, Any]] = None
    pid_alive: Optional[bool] = None
    exit_kind: Optional[str] = None
    exit_code: Optional[int] = None
    host_local: bool = True


@dataclass(frozen=True)
class GateDecision:
    action: str
    task_id: str
    run_id: Optional[int] = None
    reason: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[Any, ...]:
        return (
            self.action,
            self.reason,
            tuple(sorted((str(k), _stable_value(v)) for k, v in self.details.items())),
        )

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action,
            "task_id": self.task_id,
            "run_id": self.run_id,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.details:
            payload["details"] = dict(self.details)
        return payload


def _stable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((str(k), _stable_value(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_stable_value(v) for v in value)
    return value


class NoOp(GateDecision):
    def __init__(self, task_id: str, run_id: Optional[int] = None, reason: str = "no_op"):
        super().__init__("no_op", task_id, run_id, reason)


class KeepRunning(GateDecision):
    def __init__(self, task_id: str, run_id: Optional[int], reason: str = "running_ok"):
        super().__init__("keep_running", task_id, run_id, reason)


class ExtendStaleClaim(GateDecision):
    def __init__(self, task_id: str, run_id: Optional[int], *, claim_expires: Optional[int]):
        super().__init__(
            "extend_stale_claim",
            task_id,
            run_id,
            "claim_expired_but_pid_alive",
            {"claim_expires": claim_expires},
        )


class ReclaimStale(GateDecision):
    def __init__(self, task_id: str, run_id: Optional[int], reason: str, details: Optional[Mapping[str, Any]] = None):
        super().__init__("reclaim_stale", task_id, run_id, reason, details or {})


class ClassifyCrash(GateDecision):
    def __init__(
        self,
        task_id: str,
        run_id: Optional[int],
        *,
        exit_kind: str,
        exit_code: Optional[int],
        protocol_violation: bool = False,
        will_block: bool = False,
    ):
        super().__init__(
            "classify_crash",
            task_id,
            run_id,
            "protocol_violation" if protocol_violation else "worker_not_alive",
            {
                "exit_kind": exit_kind,
                "exit_code": exit_code,
                "protocol_violation": protocol_violation,
                "will_block": will_block,
            },
        )


class EnforceTimeout(GateDecision):
    def __init__(
        self,
        task_id: str,
        run_id: Optional[int],
        *,
        elapsed_seconds: int,
        limit_seconds: int,
        will_block: bool = False,
    ):
        super().__init__(
            "enforce_timeout",
            task_id,
            run_id,
            "max_runtime_exceeded",
            {
                "elapsed_seconds": int(elapsed_seconds),
                "limit_seconds": int(limit_seconds),
                "will_block": will_block,
            },
        )


class RequestReviewRequiredHandoff(GateDecision):
    def __init__(self, task_id: str, run_id: Optional[int]):
        super().__init__("request_review_required_handoff", task_id, run_id, "review_required_block")


class RequestAutoReviewerChild(GateDecision):
    def __init__(self, task_id: str, run_id: Optional[int]):
        super().__init__("request_auto_reviewer_child", task_id, run_id, "standard_review_needed")


class RequestNeedsRevisionFix(GateDecision):
    def __init__(self, task_id: str, run_id: Optional[int]):
        super().__init__("request_needs_revision_fix", task_id, run_id, "reviewer_needs_revision")


class RequestNotifierEmit(GateDecision):
    def __init__(self, task_id: str, run_id: Optional[int]):
        super().__init__("request_notifier_emit", task_id, run_id, "done_unnotified")


class RequestIterationBudgetContinuation(GateDecision):
    def __init__(self, task_id: str, run_id: Optional[int]):
        super().__init__("request_iteration_budget_continuation", task_id, run_id, "iteration_budget_exhausted")


class IterationBudgetContinuationCapped(GateDecision):
    def __init__(self, task_id: str, run_id: Optional[int]):
        super().__init__("iteration_budget_continuation_capped", task_id, run_id, "iteration_budget_cap_reached")


def decide_for_run(
    run: TaskRunSnapshot,
    task: TaskSnapshot,
    now: int,
    *,
    stale_timeout_seconds: int = 0,
    stale_heartbeat_gap_seconds: int = DEFAULT_STALE_HEARTBEAT_GAP_SECONDS,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
) -> GateDecision:
    """Return the next gate decision for a task/run snapshot.

    The priority order mirrors the dispatcher family that can otherwise race:
    dead worker classification wins over timeout, timeout wins over heartbeat
    staleness, and heartbeat staleness wins over plain claim expiry.
    """

    if task.status == "running" and run.status == "running":
        return _decide_running_family(
            run,
            task,
            now,
            stale_timeout_seconds=stale_timeout_seconds,
            stale_heartbeat_gap_seconds=stale_heartbeat_gap_seconds,
            failure_limit=failure_limit,
        )

    if task.status == "blocked" and _is_review_required(run.summary):
        if not task.review_required_handoff_exists:
            return RequestReviewRequiredHandoff(task.id, run.id)
        return NoOp(task.id, run.id, "review_required_handoff_exists")

    if task.status == "blocked" and task.iteration_budget_exhausted:
        if task.iteration_continuations >= task.iteration_continuation_cap:
            return IterationBudgetContinuationCapped(task.id, run.id)
        return RequestIterationBudgetContinuation(task.id, run.id)

    if _run_verdict(run) == "NEEDS_REVISION" and not task.needs_revision_fix_exists:
        return RequestNeedsRevisionFix(task.id, run.id)

    if (
        task.status == "done"
        and (task.assignee or "").strip().lower() == "coder"
        and run.outcome == "completed"
        and not task.auto_reviewer_child_exists
        and not task.auto_reviewer_child_suppressed
    ):
        return RequestAutoReviewerChild(task.id, run.id)

    if task.status == "done" and run.outcome == "completed" and not task.notified:
        return RequestNotifierEmit(task.id, run.id)

    return NoOp(task.id, run.id)


def _decide_running_family(
    run: TaskRunSnapshot,
    task: TaskSnapshot,
    now: int,
    *,
    stale_timeout_seconds: int,
    stale_heartbeat_gap_seconds: int,
    failure_limit: int,
) -> GateDecision:
    run_id = run.id
    started_at = run.started_at if run.started_at is not None else task.started_at
    worker_pid = run.worker_pid if run.worker_pid is not None else task.worker_pid
    claim_expires = run.claim_expires if run.claim_expires is not None else task.claim_expires
    last_heartbeat_at = (
        run.last_heartbeat_at
        if run.last_heartbeat_at is not None
        else task.last_heartbeat_at
    )

    if not run.host_local:
        return KeepRunning(task.id, run_id, "remote_claim")

    if worker_pid is not None and run.pid_alive is False:
        exit_kind = run.exit_kind or "pid_not_alive"
        protocol_violation = exit_kind == "clean_exit"
        return ClassifyCrash(
            task.id,
            run_id,
            exit_kind=exit_kind,
            exit_code=run.exit_code,
            protocol_violation=protocol_violation,
            will_block=protocol_violation or _will_trip_failure_counter(task, failure_limit),
        )

    if (
        task.max_runtime_seconds is not None
        and worker_pid is not None
        and started_at is not None
    ):
        elapsed = int(now) - int(started_at)
        if elapsed >= int(task.max_runtime_seconds):
            return EnforceTimeout(
                task.id,
                run_id,
                elapsed_seconds=elapsed,
                limit_seconds=int(task.max_runtime_seconds),
                will_block=_will_trip_failure_counter(task, failure_limit),
            )

    if stale_timeout_seconds > 0 and started_at is not None:
        elapsed = int(now) - int(started_at)
        heartbeat_age = (
            int(now) - int(last_heartbeat_at)
            if last_heartbeat_at is not None
            else None
        )
        if elapsed >= int(stale_timeout_seconds) and (
            heartbeat_age is None or heartbeat_age >= int(stale_heartbeat_gap_seconds)
        ):
            return ReclaimStale(
                task.id,
                run_id,
                "heartbeat_stale" if heartbeat_age is not None else "heartbeat_missing",
                {
                    "elapsed_seconds": elapsed,
                    "heartbeat_age_seconds": heartbeat_age,
                    "timeout_seconds": int(stale_timeout_seconds),
                },
            )

    if claim_expires is not None and int(claim_expires) < int(now):
        if worker_pid is not None and run.pid_alive is True:
            return ExtendStaleClaim(task.id, run_id, claim_expires=int(claim_expires))
        return ReclaimStale(
            task.id,
            run_id,
            "claim_expired",
            {"claim_expires": int(claim_expires), "worker_pid": worker_pid},
        )

    return KeepRunning(task.id, run_id)


def _will_trip_failure_counter(task: TaskSnapshot, failure_limit: int) -> bool:
    effective_limit = int(task.max_retries) if task.max_retries is not None else int(failure_limit)
    return int(task.consecutive_failures) + 1 >= effective_limit


def _is_review_required(summary: Optional[str]) -> bool:
    return "review-required" in (summary or "").strip().lower()


def _run_verdict(run: TaskRunSnapshot) -> Optional[str]:
    metadata = run.metadata or {}
    verdict = metadata.get("verdict") if isinstance(metadata, Mapping) else None
    return str(verdict).strip().upper() if verdict else None
