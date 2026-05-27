"""Operationally read-only daily monitoring digest for Hermes Kanban boards.

The module aggregates currently active kanban diagnostics plus optional
JSONL signals written by local monitors.  The digest builder deliberately
performs no task/run/event/dispatch/cron/delivery writes: callers pass an
already-open SQLite connection and the signal queue is only read, never
truncated or acknowledged.  The CLI command may still use the standard Kanban
startup path, which initializes or migrates the local DB before the builder runs.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd

DEFAULT_WINDOW_SECONDS = 24 * 60 * 60
DEFAULT_SIGNAL_PATH = Path.home() / ".hermes" / "state" / "daily_digest_signals.jsonl"
NON_ACTIONS = [
    "standard_kanban_cli_startup_may_init_or_migrate_db",
    "no_task_changes",
    "no_run_changes",
    "no_event_changes",
    "no_dispatch",
    "no_cron_activation",
    "no_delivery",
]
_SEVERITY_RANK = {name: idx for idx, name in enumerate(kd.SEVERITY_ORDER)}


def _parse_signal_ts(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    raw = str(value).strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return None


def load_daily_digest_signals(path: Path, *, since_ts: int | None = None) -> list[dict[str, Any]]:
    """Load JSONL monitor signals without consuming or mutating the queue.

    Missing files, malformed lines, and non-object JSON records are ignored.
    If a record has a parseable ``ts`` and ``since_ts`` is set, older records
    are filtered out. Records without parseable timestamps are kept because
    they may still carry useful durable operator evidence.
    """
    if not path.exists():
        return []

    signals: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _parse_signal_ts(rec.get("ts"))
                if since_ts is not None and ts is not None and ts < int(since_ts):
                    continue
                normalized = dict(rec)
                if ts is not None:
                    normalized["ts_epoch"] = ts
                signals.append(normalized)
    except OSError:
        return []
    return signals


def _signal_summary(signals: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for rec in signals:
        kind = str(rec.get("kind") or "unknown")
        bucket = buckets.setdefault(
            kind,
            {"kind": kind, "count": 0, "latest_ts": None, "latest_ts_epoch": None},
        )
        bucket["count"] += 1
        ts_epoch = _parse_signal_ts(rec.get("ts_epoch") or rec.get("ts"))
        if ts_epoch is not None and (
            bucket["latest_ts_epoch"] is None or ts_epoch > bucket["latest_ts_epoch"]
        ):
            bucket["latest_ts_epoch"] = ts_epoch
            bucket["latest_ts"] = rec.get("ts") or dt.datetime.fromtimestamp(
                ts_epoch, tz=dt.timezone.utc
            ).isoformat().replace("+00:00", "Z")
    out = list(buckets.values())
    out.sort(key=lambda item: (-int(item["count"]), item["kind"]))
    return out


def _recommended_action(diag: kd.Diagnostic) -> str:
    for action in diag.actions:
        if action.suggested:
            return action.label
    if diag.actions:
        return diag.actions[0].label
    return "Inspect task diagnostics before taking action"


def _task_row(task: Any) -> dict[str, Any]:
    return {
        "id": getattr(task, "id", None),
        "title": getattr(task, "title", None),
        "status": getattr(task, "status", None),
        "assignee": getattr(task, "assignee", None),
        "priority": getattr(task, "priority", 0),
        "created_at": getattr(task, "created_at", None),
    }


def _compute_diags_for_tasks(
    conn,
    tasks: Iterable[kb.Task],
    *,
    now_ts: int,
    config: Optional[dict[str, Any]],
) -> dict[str, tuple[kb.Task, list[kd.Diagnostic]]]:
    out: dict[str, tuple[kb.Task, list[kd.Diagnostic]]] = {}
    for task in tasks:
        diagnostics = kd.compute_task_diagnostics(
            task,
            kb.list_events(conn, task.id),
            kb.list_runs(conn, task.id),
            now=now_ts,
            config=config,
        )
        if diagnostics:
            out[task.id] = (task, diagnostics)
    return out


def build_daily_digest(
    conn,
    *,
    board: str | None = None,
    since_ts: int | None = None,
    now_ts: int | None = None,
    signal_path: Path | None = None,
    max_tasks_per_group: int = 5,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build a bounded, read-only digest from active diagnostics and signals."""
    now = int(now_ts if now_ts is not None else time.time())
    since = int(since_ts if since_ts is not None else now - DEFAULT_WINDOW_SECONDS)
    window_seconds = max(0, now - since)
    max_tasks = max(0, int(max_tasks_per_group))

    tasks = kb.list_tasks(conn, include_archived=False)
    by_task = _compute_diags_for_tasks(conn, tasks, now_ts=now, config=config)

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    tasks_with_diagnostics: set[str] = set()
    severity_counts: Counter[str] = Counter()
    total = 0
    for task_id, (task, diagnostics) in by_task.items():
        for diag in diagnostics:
            total += 1
            tasks_with_diagnostics.add(task_id)
            severity_counts[diag.severity] += 1
            key = (diag.kind, diag.severity)
            group = grouped.setdefault(
                key,
                {
                    "kind": diag.kind,
                    "severity": diag.severity,
                    "count": 0,
                    "tasks": [],
                    "_task_ids": set(),
                    "_actions": Counter(),
                    "latest_seen_at": 0,
                },
            )
            group["count"] += 1
            group["latest_seen_at"] = max(group["latest_seen_at"], int(diag.last_seen_at or 0))
            action = _recommended_action(diag)
            if action:
                group["_actions"][action] += 1
            if task_id not in group["_task_ids"]:
                group["_task_ids"].add(task_id)
                if len(group["tasks"]) < max_tasks:
                    group["tasks"].append(_task_row(task))

    groups: list[dict[str, Any]] = []
    for group in grouped.values():
        action_counts: Counter[str] = group.pop("_actions")
        task_ids: set[str] = group.pop("_task_ids")
        group["omitted_tasks"] = max(0, len(task_ids) - len(group["tasks"]))
        group["recommended_action"] = (
            action_counts.most_common(1)[0][0]
            if action_counts
            else "Inspect task diagnostics before taking action"
        )
        groups.append(group)
    groups.sort(
        key=lambda g: (
            -_SEVERITY_RANK.get(str(g["severity"]), -1),
            -int(g["count"]),
            str(g["kind"]),
        )
    )

    raw_signals = load_daily_digest_signals(signal_path or DEFAULT_SIGNAL_PATH, since_ts=since)
    external_signals = _signal_summary(raw_signals)
    highest_severity = None
    for severity in reversed(kd.SEVERITY_ORDER):
        if severity_counts.get(severity):
            highest_severity = severity
            break

    return {
        "generated_at": now,
        "board": board,
        "window": {"since_ts": since, "hours": round(window_seconds / 3600, 2)},
        "summary": {
            "tasks_total": len(tasks),
            "tasks_with_diagnostics": len(tasks_with_diagnostics),
            "diagnostics_total": total,
            "highest_severity": highest_severity,
            "severity_counts": dict(severity_counts),
            "external_signals_total": len(raw_signals),
        },
        "groups": groups,
        "external_signals": external_signals,
        "non_actions": list(NON_ACTIONS),
    }


def render_daily_digest_markdown(digest: dict[str, Any]) -> str:
    """Render a compact operator-facing Markdown digest."""
    summary = digest.get("summary") or {}
    highest = summary.get("highest_severity") or "clean"
    total = int(summary.get("diagnostics_total") or 0)
    tasks = int(summary.get("tasks_with_diagnostics") or 0)
    signal_total = int(summary.get("external_signals_total") or 0)

    lines = [
        f"# Daily Monitoring Digest — {highest.upper()}",
        "",
        f"Status: {total} active diagnostic(s) across {tasks} task(s); external signals: {signal_total}.",
    ]

    groups = digest.get("groups") or []
    if groups:
        lines.extend(["", "## Active Diagnostics"])
        for group in groups:
            lines.append(
                f"- **{group.get('severity')} / {group.get('kind')}** "
                f"×{group.get('count')}: {group.get('recommended_action')}"
            )
            for task in group.get("tasks") or []:
                assignee = task.get("assignee") or "unassigned"
                lines.append(
                    f"  - `{task.get('id')}` [{task.get('status')}] @{assignee}: {task.get('title')}"
                )
            omitted = int(group.get("omitted_tasks") or 0)
            if omitted:
                lines.append(f"  - … {omitted} more task(s) omitted")
    else:
        lines.extend(["", "## Active Diagnostics", "- None"])

    signals = digest.get("external_signals") or []
    if signals:
        lines.extend(["", "## External Signals"])
        for signal in signals:
            latest = signal.get("latest_ts") or "unknown-ts"
            lines.append(f"- `{signal.get('kind')}` ×{signal.get('count')} (latest: {latest})")

    lines.extend(["", "## Non-actions", *(f"- {item}" for item in digest.get("non_actions") or [])])
    return "\n".join(lines).rstrip() + "\n"
