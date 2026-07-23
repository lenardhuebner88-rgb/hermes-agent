#!/usr/bin/env python3
"""Read-only, deterministic Kanban blocked-notification scanner.

The scanner opens the source SQLite DB in read-only mode, copies a consistent
snapshot into memory, and emits only aggregate data plus bounded structural
metadata. It never writes to the board or reproduces free-text event payloads.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

AUTONOMOUS_KINDS = {
    "dependency",
    "review_revision",
    "integration",
    "capacity",
    "transient",
    "iteration_budget",
}
EXPLICIT_HUMAN_MARKERS = {
    "block_answered",
    "operator_escalation",
    "operator_repair",
    "promoted_manual",
    "wait_overridden",
}
RECOVERY_KINDS = {"unblocked", "completed", "archived"}
FOCUS_KINDS = {
    "submitted_for_review",
    "blocked",
    "unblocked",
    "integration_parked",
    "integration_clean",
    "review_released",
    "completed",
    "task_ping_sent",
    "operator_escalation",
}


def parse_payload(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def iso(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return dt.datetime.fromtimestamp(int(timestamp), tz=dt.timezone.utc).isoformat()


def copy_readonly_snapshot(path: Path) -> sqlite3.Connection:
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    snapshot = sqlite3.connect(":memory:")
    try:
        source.backup(snapshot)
    finally:
        source.close()
    snapshot.row_factory = sqlite3.Row
    return snapshot


def candidate_for_event(events: list[sqlite3.Row], index: int) -> str | None:
    for row in reversed(events[: index + 1]):
        if row["kind"] != "submitted_for_review":
            continue
        payload = parse_payload(row["payload"])
        candidate = payload.get("diff_candidate_commit") or payload.get("reviewed_commit")
        if isinstance(candidate, str) and candidate:
            return candidate[:64]
    return None


def review_stage_for_event(events: list[sqlite3.Row], index: int) -> str:
    """Return the preceding review stage, preserving missing-data semantics."""
    for row in reversed(events[: index + 1]):
        if row["kind"] != "submitted_for_review":
            continue
        stage = parse_payload(row["payload"]).get("review_stage")
        return str(stage) if stage not in (None, "") else "unknown"
    return "unmatched"


def run_field_bucket(
    run: sqlite3.Row | None, task_id: str, field: str
) -> str:
    """Distinguish an unmatched run from a known run with an unknown field."""
    if run is None or str(run["task_id"]) != task_id:
        return "unmatched"
    value = run[field]
    return str(value) if value not in (None, "") else "unknown"


def class_for_block(payload: dict[str, Any], following: list[sqlite3.Row]) -> str:
    kind = str(payload.get("kind") or "unclassified")
    reason = str(payload.get("reason") or "").lower()
    if kind == "needs_input" or "secret" in reason or "irreversible" in reason:
        return "human_action"
    if kind in AUTONOMOUS_KINDS:
        return "autonomous_or_transient"
    return "unclassified"


def run_report(conn: sqlite3.Connection, days: int, focus_task: str) -> dict[str, Any]:
    max_row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS id, COALESCE(MAX(created_at), 0) AS created_at FROM task_events"
    ).fetchone()
    window_end = int(max_row["created_at"])
    cutoff = window_end - days * 86400
    events = conn.execute(
        "SELECT id, task_id, kind, payload, created_at, run_id FROM task_events "
        "WHERE created_at >= ? ORDER BY task_id, created_at, id",
        (cutoff,),
    ).fetchall()
    events_by_task: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
    for event in events:
        events_by_task[str(event["task_id"])].append(event)
    runs_by_id = {
        int(row["id"]): row
        for row in conn.execute("SELECT id, task_id, profile, outcome FROM task_runs")
    }

    blocks: list[dict[str, Any]] = []
    task_ping_count = 0
    ping_by_task: collections.Counter[str] = collections.Counter()
    for task_id, task_events in events_by_task.items():
        for index, event in enumerate(task_events):
            if event["kind"] == "task_ping_sent":
                task_ping_count += 1
                ping_by_task[task_id] += 1
            if event["kind"] != "blocked":
                continue
            payload = parse_payload(event["payload"])
            following = task_events[index + 1 :]
            recovery = next((r for r in following if r["kind"] in RECOVERY_KINDS), None)
            markers = [r["kind"] for r in following if r["kind"] in EXPLICIT_HUMAN_MARKERS]
            run = runs_by_id.get(int(event["run_id"])) if event["run_id"] is not None else None
            blocks.append(
                {
                    "event_id": int(event["id"]),
                    "task_id": task_id,
                    "created_at": int(event["created_at"]),
                    "kind": str(payload.get("kind") or "unclassified"),
                    "class": class_for_block(payload, following),
                    "candidate": candidate_for_event(task_events, index),
                    "run_outcome": run_field_bucket(run, task_id, "outcome"),
                    "profile": run_field_bucket(run, task_id, "profile"),
                    "review_stage": review_stage_for_event(task_events, index),
                    "recovery_kind": recovery["kind"] if recovery else None,
                    "recovery_at": int(recovery["created_at"]) if recovery else None,
                    "explicit_human_marker_before_recovery": bool(
                        markers
                        and (recovery is None or any(r["kind"] in EXPLICIT_HUMAN_MARKERS for r in task_events[index + 1 : task_events.index(recovery) if recovery in task_events else None]))
                    ),
                }
            )

    # Recompute marker presence within the exact block-to-recovery range without
    # emitting comments/reasons (comments cannot reliably identify a human).
    for block in blocks:
        task_events = events_by_task[block["task_id"]]
        start = next(i for i, row in enumerate(task_events) if int(row["id"]) == block["event_id"])
        end = next((i for i in range(start + 1, len(task_events)) if task_events[i]["kind"] in RECOVERY_KINDS), len(task_events))
        block["explicit_human_marker_before_recovery"] = any(
            row["kind"] in EXPLICIT_HUMAN_MARKERS for row in task_events[start + 1 : end]
        )

    by_kind = collections.Counter(block["kind"] for block in blocks)
    by_class = collections.Counter(block["class"] for block in blocks)
    by_run_outcome = collections.Counter(block["run_outcome"] for block in blocks)
    by_profile = collections.Counter(block["profile"] for block in blocks)
    by_review_stage = collections.Counter(block["review_stage"] for block in blocks)
    for counter in (by_run_outcome, by_profile, by_review_stage):
        counter.setdefault("unknown", 0)
        counter.setdefault("unmatched", 0)
    recoveries = [b for b in blocks if b["recovery_kind"]]
    autonomous_recoveries = [
        b for b in recoveries if not b["explicit_human_marker_before_recovery"]
    ]
    repeats: dict[str, int] = {}
    for minutes in (5, 15, 60):
        count = 0
        grouped: dict[tuple[str, str | None], list[dict[str, Any]]] = collections.defaultdict(list)
        for block in blocks:
            grouped[(block["task_id"], block["candidate"])].append(block)
        for group in grouped.values():
            if any(
                later["created_at"] - earlier["created_at"] <= minutes * 60
                for earlier, later in zip(group, group[1:])
            ):
                count += 1
        repeats[f"tasks_with_repeated_blocks_within_{minutes}m"] = count

    focus_events = conn.execute(
        "SELECT id, task_id, kind, payload, created_at, run_id FROM task_events "
        "WHERE task_id = ? ORDER BY created_at, id",
        (focus_task,),
    ).fetchall()
    runs = {
        int(row["id"]): dict(row)
        for row in conn.execute(
            "SELECT id, profile, outcome, verdict, status FROM task_runs WHERE task_id = ?", (focus_task,)
        ).fetchall()
    }
    timeline: list[dict[str, Any]] = []
    for index, event in enumerate(focus_events):
        if event["kind"] not in FOCUS_KINDS:
            continue
        payload = parse_payload(event["payload"])
        row: dict[str, Any] = {
            "event_id": int(event["id"]),
            "at": iso(int(event["created_at"])),
            "event": event["kind"],
            "run_id": event["run_id"],
        }
        if event["kind"] == "blocked":
            row["block_kind"] = payload.get("kind") or "unclassified"
            row["class"] = class_for_block(payload, focus_events[index + 1 :])
        if event["kind"] == "submitted_for_review":
            row["review_stage"] = payload.get("review_stage")
            row["review_tier"] = payload.get("review_tier")
            row["candidate"] = (payload.get("diff_candidate_commit") or payload.get("reviewed_commit") or "")[:64]
        if event["kind"] == "task_ping_sent":
            row["notification_channel"] = payload.get("channel")
            row["notification_skipped"] = payload.get("skipped")
        run_id = event["run_id"]
        if run_id in runs:
            row["profile"] = runs[run_id].get("profile")
            row["outcome"] = runs[run_id].get("outcome")
            row["verdict"] = runs[run_id].get("verdict")
        timeline.append(row)

    snapshot_digest = hashlib.sha256(
        "\n".join(
            f"{e['id']}|{e['task_id']}|{e['kind']}|{e['created_at']}|{e['run_id'] or ''}"
            for e in events
        ).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "method": {
            "source": "read-only SQLite backup snapshot",
            "window_end": iso(window_end),
            "window_days": days,
            "cutoff": iso(cutoff),
            "classification": "block_kind plus bounded secret/irreversible keyword check; no free-text reasons or comments are emitted",
            "human_input_limit": "No explicit human-action marker is not proof that no human spoke; comments are intentionally not attributed.",
        },
        "snapshot": {
            "max_event_id": int(max_row["id"]),
            "max_event_created_at": iso(window_end),
            "window_event_header_sha256": snapshot_digest,
        },
        "metrics": {
            "block_transitions": len(blocks),
            "task_ping_sent": task_ping_count,
            "tasks_with_task_ping_sent": len(ping_by_task),
            "blocks_by_kind": dict(sorted(by_kind.items())),
            "blocks_by_class": dict(sorted(by_class.items())),
            "blocks_by_run_outcome": dict(sorted(by_run_outcome.items())),
            "blocks_by_profile": dict(sorted(by_profile.items())),
            "blocks_by_review_stage": dict(sorted(by_review_stage.items())),
            "blocks_recovered_later": len(recoveries),
            "blocks_recovered_without_explicit_human_marker": len(autonomous_recoveries),
            **repeats,
        },
        "focus_task": {"id": focus_task, "timeline": timeline},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path.home() / ".hermes/kanban.db")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--focus-task", default="t_cd570b8a")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be positive")
    conn = copy_readonly_snapshot(args.db)
    try:
        report = run_report(conn, args.days, args.focus_task)
    finally:
        conn.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    print(report["snapshot"]["window_event_header_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
