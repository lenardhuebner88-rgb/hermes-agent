"""Read-only runtime truth report for Hermes Kanban.

The collector intentionally does not call ``kanban_db.connect()`` because that
helper initializes/migrates DBs. Runtime truth must be observational only:
Git/process/config/scheduler state plus read-only SQLite queries.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from hermes_constants import get_hermes_home
from hermes_cli import kanban_db as kb
from hermes_cli.config import load_config_readonly


KANBAN_JOB_KEYWORDS = (
    "kanban",
    "reviewer",
    "sweeper",
    "retry",
    "hub watcher",
    "digest",
)
PROCESS_KEYWORDS = (
    ".hermes/hermes-agent",
    "hermes_cli.main",
)


def _run_git(repo_root: Path, *args: str) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except Exception as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def collect_git_state(repo_root: Path) -> dict[str, Any]:
    branch_rc, branch, branch_err = _run_git(repo_root, "branch", "--show-current")
    head_rc, head, head_err = _run_git(repo_root, "rev-parse", "--short", "HEAD")
    subject_rc, subject, subject_err = _run_git(
        repo_root, "log", "-1", "--pretty=%s"
    )
    status_rc, status_out, status_err = _run_git(repo_root, "status", "--short")
    dirty_files: list[dict[str, Any]] = []
    if status_rc == 0:
        for line in status_out.splitlines():
            if not line:
                continue
            status = line[:2]
            path = line[2:].strip()
            mtime = None
            candidate = repo_root / path
            try:
                if candidate.exists():
                    mtime = int(candidate.stat().st_mtime)
            except OSError:
                mtime = None
            dirty_files.append({"status": status, "path": path, "mtime": mtime})
    return {
        "repo_root": str(repo_root),
        "branch": branch if branch_rc == 0 else None,
        "head": head if head_rc == 0 else None,
        "head_subject": subject if subject_rc == 0 else None,
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files,
        "errors": [
            err
            for rc, err in (
                (branch_rc, branch_err),
                (head_rc, head_err),
                (subject_rc, subject_err),
                (status_rc, status_err),
            )
            if rc != 0 and err
        ],
    }


def _ps_lines(ps_output: Optional[str] = None) -> list[str]:
    if ps_output is not None:
        return ps_output.splitlines()
    try:
        out = subprocess.check_output(["ps", "-eo", "pid,lstart,cmd"], text=True)
    except Exception:
        return []
    return out.splitlines()


def _default_proc_env(pid: int) -> Mapping[str, str]:
    env_path = Path("/proc") / str(pid) / "environ"
    try:
        raw = env_path.read_bytes().split(b"\0")
    except OSError:
        return {}
    env: dict[str, str] = {}
    for item in raw:
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        env[key.decode(errors="ignore")] = value.decode(errors="ignore")
    return env


def _default_proc_cwd(pid: int) -> Optional[str]:
    try:
        return os.readlink(Path("/proc") / str(pid) / "cwd")
    except OSError:
        return None


def collect_processes(
    *,
    ps_output: Optional[str] = None,
    env_reader: Callable[[int], Mapping[str, str]] = _default_proc_env,
    cwd_reader: Callable[[int], Optional[str]] = _default_proc_cwd,
) -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for line in _ps_lines(ps_output):
        if not any(keyword in line for keyword in PROCESS_KEYWORDS):
            continue
        parts = line.split(None, 6)
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        env = env_reader(pid)
        processes.append(
            {
                "pid": pid,
                "start_local": " ".join(parts[1:6]),
                "cmd": parts[6],
                "cwd": cwd_reader(pid),
                "env": {
                    "HERMES_AUTHORING_LINT": "set"
                    if env.get("HERMES_AUTHORING_LINT")
                    else "unset",
                    "HERMES_PROFILE": env.get("HERMES_PROFILE") or None,
                    "HERMES_KANBAN_TASK": "set"
                    if env.get("HERMES_KANBAN_TASK")
                    else "unset",
                },
            }
        )
    processes.sort(key=lambda item: item["pid"])
    return processes


def collect_config_state(config: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    cfg = config if config is not None else load_config_readonly()
    kanban = cfg.get("kanban", {}) if isinstance(cfg, Mapping) else {}
    keys = (
        "dispatch_in_gateway",
        "dispatch_interval_seconds",
        "failure_limit",
        "worker_log_rotate_bytes",
        "worker_log_backup_count",
        "orchestrator_profile",
        "default_assignee",
        "auto_decompose",
        "auto_decompose_per_tick",
        "dispatch_stale_timeout_seconds",
    )
    return {key: kanban.get(key) for key in keys if key in kanban}


def _job_is_kanban_relevant(job: Mapping[str, Any]) -> bool:
    haystack = " ".join(
        str(job.get(key) or "")
        for key in ("name", "script", "prompt", "deliver", "workdir")
    ).lower()
    return any(keyword in haystack for keyword in KANBAN_JOB_KEYWORDS)


def collect_scheduler_state(jobs_path: Optional[Path] = None) -> dict[str, Any]:
    path = jobs_path or (get_hermes_home() / "cron" / "jobs.json")
    if not path.exists():
        return {"path": str(path), "exists": False, "jobs": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"path": str(path), "exists": True, "error": str(exc), "jobs": []}
    jobs = []
    for job in data.get("jobs", []):
        if not isinstance(job, Mapping) or not _job_is_kanban_relevant(job):
            continue
        jobs.append(
            {
                "name": job.get("name"),
                "enabled": bool(job.get("enabled")),
                "state": job.get("state"),
                "schedule": job.get("schedule_display"),
                "last_run_at": job.get("last_run_at"),
                "next_run_at": job.get("next_run_at"),
                "last_status": job.get("last_status"),
                "script": job.get("script"),
                "no_agent": bool(job.get("no_agent")),
                "deliver": job.get("deliver"),
            }
        )
    jobs.sort(key=lambda item: str(item.get("name") or ""))
    return {"path": str(path), "exists": True, "jobs": jobs}


def _count_rows(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    try:
        rows = conn.execute(
            f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column} ORDER BY {column}"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(key): int(count) for key, count in rows}


def collect_db_state(
    *,
    board: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    path = db_path or kb.kanban_db_path(board=board)
    if not path.exists():
        return {"path": str(path), "exists": False}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {"path": str(path), "exists": True, "error": str(exc)}
    try:
        latest = conn.execute(
            """
            SELECT id, task_id, run_id, kind, created_at
              FROM task_events
             ORDER BY created_at DESC, id DESC
             LIMIT 1
            """
        ).fetchone()
        event_counts = {
            "gate_decision_parity": conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE kind = 'gate_decision_parity'"
            ).fetchone()[0],
            "completion_blocked_scope_attestation": conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE kind = 'completion_blocked_scope_attestation'"
            ).fetchone()[0],
            "heartbeat": conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE kind = 'heartbeat'"
            ).fetchone()[0],
        }
        taxonomy_rows = conn.execute(
            """
            SELECT COALESCE(worker_exit_kind, 'NULL') AS kind,
                   COALESCE(worker_protocol_state, 'NULL') AS protocol,
                   COUNT(*) AS count
              FROM task_runs
             GROUP BY kind, protocol
             ORDER BY count DESC, kind ASC, protocol ASC
             LIMIT 20
            """
        ).fetchall()
        return {
            "path": str(path),
            "exists": True,
            "tasks_by_status": _count_rows(conn, "tasks", "status"),
            "runs_by_status": _count_rows(conn, "task_runs", "status"),
            "latest_event": dict(latest) if latest else None,
            "event_counts": {key: int(value) for key, value in event_counts.items()},
            "worker_exit_taxonomy": [
                {
                    "worker_exit_kind": row["kind"],
                    "worker_protocol_state": row["protocol"],
                    "count": int(row["count"]),
                }
                for row in taxonomy_rows
            ],
        }
    except sqlite3.Error as exc:
        return {"path": str(path), "exists": True, "error": str(exc)}
    finally:
        conn.close()


def build_runtime_truth(
    *,
    board: Optional[str] = None,
    repo_root: Optional[Path] = None,
    now_ts: Optional[int] = None,
    ps_output: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    jobs_path: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Collect a read-only runtime-truth snapshot."""
    repo = repo_root or Path(__file__).resolve().parents[1]
    current_board = board or kb.get_current_board()
    return {
        "generated_at": int(now_ts if now_ts is not None else time.time()),
        "board": current_board,
        "git": collect_git_state(repo),
        "processes": collect_processes(ps_output=ps_output),
        "config": {"kanban": collect_config_state(config)},
        "scheduler": collect_scheduler_state(jobs_path),
        "db": collect_db_state(board=current_board, db_path=db_path),
        "non_actions": [
            "no_dispatch",
            "no_db_writes",
            "no_cron_changes",
            "no_service_restart",
            "no_external_delivery",
        ],
    }


def render_runtime_truth_markdown(report: Mapping[str, Any]) -> str:
    git = report.get("git", {})
    db = report.get("db", {})
    scheduler = report.get("scheduler", {})
    processes = report.get("processes", [])
    lines = [
        f"# Kanban Runtime Truth - board `{report.get('board')}`",
        "",
        "## Git",
        f"- Branch: `{git.get('branch')}`",
        f"- HEAD: `{git.get('head')}` {git.get('head_subject') or ''}".rstrip(),
        f"- Dirty: `{bool(git.get('dirty'))}`",
    ]
    dirty_files = git.get("dirty_files") or []
    if dirty_files:
        lines.append("- Dirty files:")
        for item in dirty_files[:20]:
            lines.append(f"  - `{item.get('status')}` `{item.get('path')}`")
        if len(dirty_files) > 20:
            lines.append(f"  - ... {len(dirty_files) - 20} more")

    latest = db.get("latest_event") if isinstance(db, Mapping) else None
    lines.extend(
        [
            "",
            "## Runtime",
            f"- Processes: `{len(processes)}`",
            f"- Scheduler jobs: `{len((scheduler or {}).get('jobs') or [])}`",
            f"- DB path: `{db.get('path')}`",
            f"- Latest event: `{latest}`",
            "",
            "## DB Counts",
            f"- Tasks: `{db.get('tasks_by_status')}`",
            f"- Runs: `{db.get('runs_by_status')}`",
            f"- Events: `{db.get('event_counts')}`",
        ]
    )

    if processes:
        lines.extend(["", "## Processes"])
        for proc in processes:
            env = proc.get("env", {})
            lines.append(
                "- "
                f"`{proc.get('pid')}` {proc.get('start_local')} "
                f"profile=`{env.get('HERMES_PROFILE')}` "
                f"authoring_lint=`{env.get('HERMES_AUTHORING_LINT')}` "
                f"cmd=`{proc.get('cmd')}`"
            )

    jobs = (scheduler or {}).get("jobs") or []
    if jobs:
        lines.extend(["", "## Scheduler"])
        for job in jobs:
            lines.append(
                "- "
                f"{job.get('name')}: enabled=`{job.get('enabled')}` "
                f"schedule=`{job.get('schedule')}` last=`{job.get('last_run_at')}` "
                f"next=`{job.get('next_run_at')}` script=`{job.get('script')}`"
            )

    lines.extend(["", "## Non-Actions"])
    for item in report.get("non_actions", []):
        lines.append(f"- `{item}`")
    return "\n".join(lines) + "\n"
