"""Bind kanban executions to the commit that actually shipped their work.

Until now "landed" was inferred from status transitions only, which produced a
0.17% landing rate with zero commit evidence -- the system could not say which
execution corresponds to which shipped change.

Git already records that link: the repository's landing convention names the
task in the commit subject (``kanban(t_ab12cd34): ...``,
``kanban: merge kanban/t_ab12cd34 (...)``). A commit reachable from the
integration branch is hard evidence that the task's work shipped, and it
carries the SHA.

This reader is read-only in both directions: it runs ``git log`` and reads the
kanban database through a read-only connection. It emits a LANDED lifecycle
event carrying ``landed_sha``, reusing the *kanban* execution identity so the
evidence attaches to the execution that produced the work instead of forming a
parallel population.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from hermes_cli.execution_facts_contract import (
    EventType,
    ExecutionEvent,
    ExecutionSurface,
    LifecyclePhase,
    TriggerKind,
    Validity,
    WorkloadKind,
    make_event,
    stable_execution_id,
    stable_idempotency_key,
)

SOURCE = "git_landing"
# The identity kanban_timeline uses, so landing evidence lands on the same
# execution rather than beside it.
KANBAN_SOURCE = "kanban_timeline"
DEFAULT_INTEGRATION_REF = "main"
TASK_ID_PATTERN = re.compile(r"\b(t_[0-9a-f]{8})\b")
_RECORD_SEPARATOR = "\x1f"


@dataclass(frozen=True, slots=True)
class LandingObservation:
    """One task proven to have shipped, with the commit that shipped it."""

    task_id: str
    landed_sha: str
    landed_at_ms: int


def _run_git(
    args: Sequence[str], *, repo: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def read_landing_observations(
    repo: Path,
    *,
    integration_ref: str = DEFAULT_INTEGRATION_REF,
    timeout: float = 60.0,
) -> tuple[LandingObservation, ...] | None:
    """Read tasks whose work is reachable from the integration branch.

    Returns None when git cannot be read, so the caller can report the source
    as unknown instead of as zero.
    """
    result = _run_git(
        [
            "log",
            integration_ref,
            f"--format=%H{_RECORD_SEPARATOR}%ct{_RECORD_SEPARATOR}%s",
            "--no-color",
        ],
        repo=repo,
        timeout=timeout,
    )
    if result.returncode != 0:
        return None

    # Walk newest-first and keep the FIRST commit seen per task, which is the
    # most recent one that touched it. Re-running must give the same answer,
    # so the choice may not depend on scan order beyond git's own ordering.
    observations: dict[str, LandingObservation] = {}
    for line in result.stdout.splitlines():
        parts = line.split(_RECORD_SEPARATOR, 2)
        if len(parts) != 3:
            continue
        sha, committed_at, subject = parts
        try:
            landed_at_ms = int(committed_at) * 1000
        except ValueError:
            continue
        for task_id in TASK_ID_PATTERN.findall(subject):
            if task_id in observations:
                continue
            observations[task_id] = LandingObservation(
                task_id=task_id,
                landed_sha=sha,
                landed_at_ms=landed_at_ms,
            )
    return tuple(
        observations[task_id] for task_id in sorted(observations)
    )


def task_runs_by_task(
    connection: sqlite3.Connection, task_ids: Iterable[str]
) -> Mapping[str, tuple[str, ...]]:
    """Map each task to its recorded runs, read-only."""
    wanted = sorted(set(task_ids))
    if not wanted:
        return {}
    runs: dict[str, list[str]] = {}
    # Chunked so a large task set cannot exceed SQLite's parameter limit.
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = connection.execute(
            "SELECT task_id, id FROM task_runs "
            f"WHERE task_id IN ({placeholders}) ORDER BY task_id, id",
            tuple(chunk),
        ).fetchall()
        for row in rows:
            runs.setdefault(str(row[0]), []).append(str(row[1]))
    return {task_id: tuple(values) for task_id, values in runs.items()}


def reconcile_landing(
    observations: Sequence[LandingObservation],
    runs_by_task: Mapping[str, Sequence[str]],
) -> list[ExecutionEvent]:
    """Emit LANDED events carrying the shipping commit."""
    events: list[ExecutionEvent] = []
    for observation in observations:
        for task_run_id in runs_by_task.get(observation.task_id, ()):
            source_execution_id = f"task_run:{task_run_id}"
            events.append(
                make_event(
                    source=SOURCE,
                    source_execution_id=source_execution_id,
                    # Deliberately the kanban execution identity.
                    execution_id=stable_execution_id(
                        KANBAN_SOURCE, source_execution_id
                    ),
                    idempotency_key=stable_idempotency_key(
                        SOURCE, source_execution_id, "landed"
                    ),
                    event_type=EventType.OUTCOME_OBSERVED,
                    # The commit is evidence, not an inference.
                    validity=Validity.EXACT,
                    observed_at_ms=observation.landed_at_ms,
                    recorded_at_ms=observation.landed_at_ms,
                    workload_kind=WorkloadKind.KANBAN_TASK,
                    execution_surface=ExecutionSurface.HERMES,
                    trigger_kind=TriggerKind.KANBAN,
                    lifecycle_phase=LifecyclePhase.LANDED,
                    task_id=observation.task_id,
                    task_run_id=task_run_id,
                    attributes={"landed_sha": observation.landed_sha},
                    evidence_ref=f"git:{observation.landed_sha}:landed",
                )
            )
    return events
