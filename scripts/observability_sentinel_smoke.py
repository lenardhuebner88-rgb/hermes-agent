#!/usr/bin/env python3
"""Run one bounded, non-agentic Execution-Facts sentinel."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from hermes_cli.execution_facts_contract import (  # noqa: E402
    EventType,
    ExecutionSurface,
    LifecyclePhase,
    TriggerKind,
    Validity,
    WorkloadKind,
    make_event,
    stable_idempotency_key,
)
from hermes_cli.execution_facts_ledger import (  # noqa: E402
    ExecutionFactsLedger,
)
from hermes_cli.execution_facts_projection import (  # noqa: E402
    rebuild_projections,
)
from hermes_cli.execution_facts_readmodel import (  # noqa: E402
    build_execution_facts_payload,
)

CONTRACT_VERSION = "observability-sentinel.v2"
DEFAULT_DATABASE = Path(
    "/mnt/data/hermes-observability/execution_facts.db"
)
DEFAULT_STATUS_PATH = Path(
    "/mnt/data/hermes-observability/sentinel-status.json"
)
DEFAULT_LOCK_PATH = Path(
    "/mnt/data/hermes-observability/sentinel-smoke.lock"
)


def sentinel_idempotency_key(now: datetime) -> str:
    """One stable probe per ISO week."""
    year, week, _ = now.isocalendar()
    return f"observability-sentinel:{year}-W{week:02d}"


def sentinel_events(now: datetime):
    """Return content-free facts stamped at the real probe observation time."""
    key = sentinel_idempotency_key(now)
    started_ms = int(now.astimezone(timezone.utc).timestamp() * 1000)
    common = {
        "source": "observability_sentinel",
        "source_execution_id": key,
        "observed_at_ms": started_ms,
        "recorded_at_ms": started_ms,
        "validity": Validity.EXACT,
        "workload_kind": WorkloadKind.SYSTEM_JOB,
        "execution_surface": ExecutionSurface.PROCESS,
        "trigger_kind": TriggerKind.SENTINEL,
        "evidence_ref": f"sentinel:{key.split(':', 1)[1]}:v2",
    }
    start = make_event(
        **common,
        idempotency_key=stable_idempotency_key(key, "started"),
        event_type=EventType.LIFECYCLE,
        lifecycle_phase=LifecyclePhase.PROCESS_STARTED,
        status="sentinel_started",
    )
    end = make_event(
        **{
            **common,
            "observed_at_ms": started_ms + 1,
            "recorded_at_ms": started_ms + 1,
        },
        idempotency_key=stable_idempotency_key(key, "ended"),
        event_type=EventType.OUTCOME_OBSERVED,
        lifecycle_phase=LifecyclePhase.ENDED,
        status="sentinel_ok",
        exit_code=0,
    )
    return start, end


def run_probe(database: Path, *, now: datetime) -> dict[str, Any]:
    """Exercise append, durable dedupe, replay, outcome, and unknown-cost truth."""
    ledger = ExecutionFactsLedger(database)
    ledger.initialize()
    key = sentinel_idempotency_key(now)
    existing = tuple(
        event
        for event in ledger.iter_events(source="observability_sentinel")
        if event.source_execution_id == key
    )
    if existing and len(existing) != 2:
        raise ValueError("weekly sentinel event set is incomplete")
    events = (
        tuple(
            sorted(
                existing,
                key=lambda event: (event.observed_at_ms, event.event_id),
            )
        )
        if existing
        else sentinel_events(now)
    )
    first = ledger.append_batch(events)
    second = ledger.append_batch(events)
    projection = rebuild_projections(database)
    payload = build_execution_facts_payload(database, now=now)
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        projected = connection.execute(
            """
            SELECT status, lifecycle_json
              FROM execution_projection
             WHERE execution_id = ?
            """,
            (events[0].execution_id,),
        ).fetchone()
    outcome_ok = (
        projected is not None
        and projected[0] == "sentinel_ok"
        and "ended" in json.loads(str(projected[1]))
    )
    alert_reasons = payload["alert_gate"]["reasons"]
    operational_isolation = (
        payload["outcomes"]["by_status"].get("sentinel_ok") is None
        and all(
            row["source"] != "observability_sentinel"
            for row in payload["shadow_gates"]["sources"]
        )
        and not any(
            "observability_sentinel" in str(reason)
            for reason in alert_reasons
        )
    )
    costs = payload["usage"]["costs"]
    false_zero_absent = all(
        costs.get(total) is None or costs.get(population, 0) > 0
        for total, population in (
            ("api_equivalent_total", "api_equivalent_population"),
            (
                "allocated_subscription_total",
                "allocated_subscription_population",
            ),
            ("marginal_total", "marginal_population"),
            ("metered_total", "metered_population"),
        )
    )
    checks = {
        "append": len(first) == 2,
        "dedupe": all(not result.inserted for result in second),
        "projection": (
            payload["database"]["computed_status"] == "ready"
            and projection.digest
            == payload["database"]["projection_digest"]
        ),
        "outcome": outcome_ok,
        "operational_isolation": operational_isolation,
        "unknown_cost_not_zero": false_zero_absent,
    }
    return {
        "contract_version": CONTRACT_VERSION,
        "status": "pass" if all(checks.values()) else "fail",
        "execution_id": events[0].execution_id,
        "checks": checks,
    }


def status_receipt(
    report: Mapping[str, Any],
    *,
    checked_at: datetime,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    checked = checked_at.astimezone(timezone.utc).isoformat()
    checks = {
        str(key): bool(value)
        for key, value in (report.get("checks") or {}).items()
    }
    passed = report.get("status") == "pass" and bool(checks) and all(
        checks.values()
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "status": "passed" if passed else "failed",
        "checked_at": checked,
        "last_success_at": (
            checked
            if passed
            else (previous or {}).get("last_success_at")
        ),
        "execution_id": report.get("execution_id"),
        "checks": checks,
        "activation_effect": "execution_facts_only",
    }


def write_status(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically replace the small content-free status receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_previous_status(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another sentinel smoke is already running") from exc
        yield


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--status-path",
        type=Path,
        default=DEFAULT_STATUS_PATH,
    )
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the deterministic content-free probe contract only.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    now = datetime.now(timezone.utc)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "idempotency_key": sentinel_idempotency_key(now),
                    "event_count": len(sentinel_events(now)),
                    "activation_effect": "none",
                },
                indent=2,
            )
        )
        return 0
    previous = read_previous_status(args.status_path)
    try:
        with exclusive_lock(args.lock_path):
            report = run_probe(args.db, now=now)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        report = {
            "contract_version": CONTRACT_VERSION,
            "status": "fail",
            "checks": {"probe": False},
            "error": type(exc).__name__,
        }
    receipt = status_receipt(report, checked_at=now, previous=previous)
    write_status(args.status_path, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
