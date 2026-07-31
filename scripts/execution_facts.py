#!/usr/bin/env python3
"""Offline operator CLI for execution-facts schema, replay, and validation."""

from __future__ import annotations

# ruff: noqa: E402

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from hermes_cli.execution_facts_contract import (
    ExecutionEvent,
    ExecutionSurface,
    LifecyclePhase,
    Validity,
    contract_schema,
)
from hermes_cli.execution_facts_ledger import ExecutionFactsLedger
from hermes_cli.execution_facts_instrumentation import (
    ShadowValidationObservation,
    shadow_validation_event,
)
from hermes_cli.execution_facts_projection import rebuild_projections
from hermes_cli.execution_facts_readmodel import build_execution_facts_payload
from hermes_cli.execution_facts_reconcile import (
    append_reconciled,
    reconcile_cron,
    reconcile_kanban,
    reconcile_loop_records,
    reconcile_system_invocations,
    reconcile_usage_facts,
    SystemInvocationObservation,
)
from hermes_cli.execution_facts_shadow import (
    DEFAULT_EVIDENCE_DIR,
    ShadowCollectionConfig,
    collect_shadow,
)
from hermes_cli.execution_facts_terminal import (
    SqliteIdentityStore,
    TmuxReconciliationAdapter,
    parse_tmux_list_panes,
)

_SHADOW_SOURCES = (
    "kanban_timeline",
    "hermes_cron",
    "loop_ledger",
    "tmux_reconciliation",
    "systemd_invocation",
    "crontab_invocation",
    "usage_facts",
)


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _read_only_sqlite(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: JSON object required")
        records.append(value)
    return records


def _sha256_digest(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise argparse.ArgumentTypeError(
            "evidence digest must be exactly 64 hexadecimal characters"
        )
    return normalized


def _validate(path: Path) -> tuple[dict[str, Any], int]:
    if not path.is_file():
        return {"computed_status": "invalid", "reason": "database_missing"}, 2
    connection = _read_only_sqlite(path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "execution_events" not in tables:
            return {
                "computed_status": "invalid",
                "reason": "execution_events_missing",
            }, 2
        invalid: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        count = 0
        for row in connection.execute(
            """
            SELECT event_id, source, idempotency_key, event_json
              FROM execution_events
             ORDER BY observed_at_ms, event_id
            """
        ):
            count += 1
            try:
                event = ExecutionEvent.from_dict(json.loads(row["event_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                invalid.append(
                    {
                        "event_id": row["event_id"],
                        "error_class": type(exc).__name__,
                    }
                )
                continue
            identity = (event.source, event.idempotency_key)
            if identity in seen:
                invalid.append(
                    {
                        "event_id": event.event_id,
                        "error_class": "duplicate_source_idempotency_key",
                    }
                )
            seen.add(identity)
        meta = {
            str(row["key"]): str(row["value"])
            for row in connection.execute(
                "SELECT key, value FROM execution_projection_meta"
            )
        } if "execution_projection_meta" in tables else {}
        projection_current = int(meta.get("raw_event_count", "-1")) == count
        result = {
            "computed_status": (
                "valid" if not invalid and projection_current else "invalid"
            ),
            "event_count": count,
            "invalid_count": len(invalid),
            "invalid": invalid,
            "projection_current": projection_current,
            "projection_digest": meta.get("digest"),
        }
        return result, 0 if result["computed_status"] == "valid" else 2
    finally:
        connection.close()


def _cmd_reconcile(args: argparse.Namespace) -> dict[str, Any]:
    target = Path(args.db)
    ledger = ExecutionFactsLedger(target)
    ledger.initialize()
    events = []
    source_counts: dict[str, int] = {}

    for label, source_path, adapter in (
        ("kanban", args.kanban_db, reconcile_kanban),
        ("cron", args.cron_db, reconcile_cron),
    ):
        if not source_path:
            continue
        connection = _read_only_sqlite(Path(source_path))
        try:
            adapted = adapter(connection)
        finally:
            connection.close()
        source_counts[label] = len(adapted)
        events.extend(adapted)

    for loop_path in args.loop_ledger:
        adapted = reconcile_loop_records(_read_jsonl(Path(loop_path)))
        source_counts[f"loop:{loop_path}"] = len(adapted)
        events.extend(adapted)

    if args.usage_db:
        fees = (
            json.loads(
                Path(args.subscription_fees).read_text(encoding="utf-8")
            )
            if args.subscription_fees
            else None
        )
        if fees is not None and not isinstance(fees, dict):
            raise ValueError("subscription fee file must contain a JSON object")
        connection = _read_only_sqlite(Path(args.usage_db))
        try:
            adapted = reconcile_usage_facts(
                connection,
                subscription_fees=fees,
                fee_version=args.fee_version,
            )
        finally:
            connection.close()
        source_counts["usage"] = len(adapted)
        events.extend(adapted)

    if args.tmux_panes:
        observed_at_ms = (
            args.observed_at_ms
            if args.observed_at_ms is not None
            else int(datetime.now(UTC).timestamp() * 1000)
        )
        observations = parse_tmux_list_panes(
            Path(args.tmux_panes).read_text(encoding="utf-8"),
            observed_at_ms=observed_at_ms,
            server_id=args.tmux_server_id,
        )
        store = SqliteIdentityStore(target)
        store.initialize()
        adapted = TmuxReconciliationAdapter(store).reconcile(observations)
        source_counts["tmux"] = len(adapted)
        events.extend(adapted)

    for system_path in args.system_observations:
        observations = [
            SystemInvocationObservation(
                source_execution_id=str(record["source_execution_id"]),
                surface=ExecutionSurface(str(record["surface"])),
                observed_at_ms=int(record["observed_at_ms"]),
                phase=LifecyclePhase(str(record["phase"])),
                status=record.get("status"),
                error_class=record.get("error_class"),
                exit_code=(
                    int(record["exit_code"])
                    if record.get("exit_code") is not None
                    else None
                ),
                validity=Validity(str(record.get("validity", "exact"))),
            )
            for record in _read_jsonl(Path(system_path))
        ]
        adapted = reconcile_system_invocations(observations)
        source_counts[f"system:{system_path}"] = len(adapted)
        events.extend(adapted)

    results = append_reconciled(ledger, events)
    projection = rebuild_projections(target)
    return {
        "computed_status": "ready",
        "sources": source_counts,
        "submitted": len(results),
        "inserted": sum(result.inserted for result in results),
        "deduped": sum(not result.inserted for result in results),
        "projection": projection.to_dict(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="print the content-free V1 schema")

    show = commands.add_parser("show", help="print the read-only P0 read model")
    show.add_argument("--db", type=Path)

    validate = commands.add_parser(
        "validate", help="validate raw events and projection freshness"
    )
    validate.add_argument("--db", type=Path, required=True)

    initialize = commands.add_parser(
        "init", help="initialize an explicitly named offline/shadow database"
    )
    initialize.add_argument("--db", type=Path, required=True)

    rebuild = commands.add_parser(
        "rebuild", help="replay immutable raw facts into derived projections"
    )
    rebuild.add_argument("--db", type=Path, required=True)

    retention = commands.add_parser(
        "retention", help="aggregate and prune raw facts older than N days"
    )
    retention.add_argument("--db", type=Path, required=True)
    retention.add_argument("--raw-days", type=int, default=90)

    reconcile = commands.add_parser(
        "reconcile", help="read sources and append to an explicit shadow DB"
    )
    reconcile.add_argument("--db", type=Path, required=True)
    reconcile.add_argument("--kanban-db", type=Path)
    reconcile.add_argument("--cron-db", type=Path)
    reconcile.add_argument("--usage-db", type=Path)
    reconcile.add_argument(
        "--subscription-fees",
        type=Path,
        help="JSON provider/origin to monthly USD fee mapping",
    )
    reconcile.add_argument("--fee-version")
    reconcile.add_argument("--loop-ledger", type=Path, action="append", default=[])
    reconcile.add_argument(
        "--tmux-panes",
        type=Path,
        help="neutral six-field list-panes output; never pane content",
    )
    reconcile.add_argument("--tmux-server-id", default="default")
    reconcile.add_argument("--observed-at-ms", type=int)
    reconcile.add_argument(
        "--system-observations",
        type=Path,
        action="append",
        default=[],
        help="content-free systemd/crontab observation JSONL",
    )

    collect = commands.add_parser(
        "collect-shadow",
        help="read live sources without controlling them and append Shadow facts",
    )
    collect.add_argument("--db", type=Path, required=True)
    collect.add_argument(
        "--hermes-home",
        type=Path,
        default=Path(
            os.environ.get(
                "HERMES_HOME",
                str(Path.home() / ".hermes"),
            )
        ),
    )
    collect.add_argument("--usage-db", type=Path)
    collect.add_argument(
        "--evidence-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
    )
    collect.add_argument("--tmux-server-id", default="default")
    collect.add_argument("--skip-systemd", action="store_true")
    collect.add_argument("--skip-crontab", action="store_true")
    collect.add_argument("--crontab-window-days", type=int, default=30)
    collect.add_argument("--subscription-fees", type=Path)
    collect.add_argument("--fee-version")
    collect.add_argument(
        "--usage-sample-limit",
        type=int,
        default=200,
        help="usage rows to read; 0 reads every row",
    )
    collect.add_argument(
        "--repository",
        type=Path,
        help="git checkout supplying landing evidence (omit to skip)",
    )
    collect.add_argument("--integration-ref", default="main")

    proof = commands.add_parser(
        "record-shadow-proof",
        help="append explicit source-gate evidence to a named shadow DB",
    )
    proof.add_argument("--db", type=Path, required=True)
    proof.add_argument("--source", required=True, choices=_SHADOW_SOURCES)
    proof.add_argument("--sample-size", type=int, required=True)
    proof.add_argument("--capture-p95-us", type=int, required=True)
    proof.add_argument("--identity-coverage-passed", action="store_true")
    proof.add_argument("--metric-coverage-passed", action="store_true")
    proof.add_argument("--dedupe-reconciled", action="store_true")
    proof.add_argument("--behavior-equivalent", action="store_true")
    proof.add_argument(
        "--evidence-sha256",
        type=_sha256_digest,
        required=True,
        help="content-free SHA-256 of the external proof artifact",
    )
    proof.add_argument("--observed-at-ms", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "schema":
        _print(contract_schema())
        return 0
    if args.command == "show":
        _print(build_execution_facts_payload(args.db))
        return 0
    if args.command == "validate":
        result, exit_code = _validate(args.db)
        _print(result)
        return exit_code
    if args.command == "init":
        ledger = ExecutionFactsLedger(args.db)
        ledger.initialize()
        _print({"computed_status": "ready", "path": str(args.db)})
        return 0
    if args.command == "rebuild":
        _print(rebuild_projections(args.db).to_dict())
        return 0
    if args.command == "retention":
        ledger = ExecutionFactsLedger(args.db)
        result = ledger.apply_retention(raw_days=args.raw_days)
        projection = rebuild_projections(args.db)
        _print(
            {
                "computed_status": "ready",
                "cutoff_ms": result.cutoff_ms,
                "aggregated_rows": result.aggregated_rows,
                "deleted_events": result.deleted_events,
                "projection": projection.to_dict(),
            }
        )
        return 0
    if args.command == "reconcile":
        _print(_cmd_reconcile(args))
        return 0
    if args.command == "collect-shadow":
        fees = (
            json.loads(
                Path(args.subscription_fees).read_text(encoding="utf-8")
            )
            if args.subscription_fees
            else None
        )
        if fees is not None and not isinstance(fees, dict):
            raise ValueError(
                "subscription fee file must contain a JSON object"
            )
        result = collect_shadow(
            ShadowCollectionConfig(
                database=args.db,
                hermes_home=args.hermes_home,
                evidence_dir=args.evidence_dir,
                usage_database=args.usage_db,
                tmux_server_id=args.tmux_server_id,
                include_systemd=not args.skip_systemd,
                include_crontab=not args.skip_crontab,
                crontab_window_days=args.crontab_window_days,
                subscription_fees=fees,
                fee_version=args.fee_version,
                usage_sample_limit=args.usage_sample_limit,
                repository=args.repository,
                integration_ref=args.integration_ref,
            )
        )
        _print(result.to_dict())
        return 0
    if args.command == "record-shadow-proof":
        observed_at_ms = (
            args.observed_at_ms
            if args.observed_at_ms is not None
            else int(datetime.now(UTC).timestamp() * 1000)
        )
        event = shadow_validation_event(
            ShadowValidationObservation(
                validated_source=args.source,
                observed_at_ms=observed_at_ms,
                sample_size=args.sample_size,
                identity_coverage_passed=args.identity_coverage_passed,
                metric_coverage_passed=args.metric_coverage_passed,
                capture_p95_us=args.capture_p95_us,
                dedupe_reconciled=args.dedupe_reconciled,
                behavior_equivalent=args.behavior_equivalent,
                evidence_ref=(
                    f"shadow-proof:{args.source}:sha256:"
                    f"{args.evidence_sha256}"
                ),
            )
        )
        ledger = ExecutionFactsLedger(args.db)
        ledger.initialize()
        append = ledger.append(event)
        projection = rebuild_projections(args.db)
        _print(
            {
                "computed_status": "recorded",
                "inserted": append.inserted,
                "event_id": append.event_id,
                "projection": projection.to_dict(),
            }
        )
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
