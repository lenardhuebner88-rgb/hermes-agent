"""Read-only operator inventory for Hermes dashboard.

This endpoint connects three things the operator cares about when the host feels
busy: git worktrees, running actors, and actionable read-only levers. It never
returns raw filesystem paths, command lines, environment values, or secrets.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

from fastapi import FastAPI

_SCHEMA = "hermes-operator-inventory-v1"
_CACHE_TTL_SECONDS = 20.0
_WORKTREE_STATUS_TIMEOUT_SECONDS = 0.45
_WORKTREE_STATUS_BUDGET_SECONDS = 4.0
_GIT_LIST_TIMEOUT_SECONDS = 1.2
_PROCESS_CPU_CACHE: dict[int, tuple[float, float]] = {}
_CACHE: tuple[float, dict[str, Any]] | None = None

_SECRET_MARKERS = (
    "bearer",
    "basic",
    "token=",
    "api_key=",
    "apikey=",
    "sk-",
    "ghp_",
    ".env",
)

_ROLE_LABELS = {
    "kanban_worker": "Kanban Worker",
    "codex": "Codex",
    "claude_code": "Claude Code",
    "kimi": "Kimi",
    "hermes_daemon": "Hermes Daemon",
    "test_runner": "Tests",
    "browser_test": "Browser-Tests",
}

_ROLE_ORDER = {
    "kanban_worker": 0,
    "codex": 1,
    "claude_code": 2,
    "kimi": 3,
    "hermes_daemon": 4,
    "test_runner": 5,
    "browser_test": 6,
}



def _scrub(value: object) -> str:
    text = str(value)
    lowered = text.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return "redacted"
    if "/home/" in text or "\\Users\\" in text or ".worktrees/" in text:
        return "redacted"
    return text[:117] + "..." if len(text) > 120 else text



def _error(errors: list[str], label: str, exc: BaseException) -> None:
    errors.append(f"{label}: {_scrub(type(exc).__name__)}")



def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default



def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default



def _cpu_seconds(cpu_times: Any) -> float | None:
    try:
        return float(getattr(cpu_times, "user", 0.0)) + float(getattr(cpu_times, "system", 0.0))
    except Exception:
        return None



def _cpu_percent_from_sample(pid: int, now: float, cpu_seconds: float) -> float:
    previous = _PROCESS_CPU_CACHE.get(pid)
    _PROCESS_CPU_CACHE[pid] = (now, cpu_seconds)
    if previous is None:
        return 0.0
    elapsed = now - previous[0]
    if elapsed <= 0:
        return 0.0
    delta = max(0.0, cpu_seconds - previous[1])
    return round((delta / elapsed) * 100.0, 1)



def _prune_cpu_cache(seen_pids: set[int]) -> None:
    stale = [pid for pid in _PROCESS_CPU_CACHE if pid not in seen_pids]
    for pid in stale:
        _PROCESS_CPU_CACHE.pop(pid, None)



def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent



def _git_common_root(cwd: Path) -> Path:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_LIST_TIMEOUT_SECONDS,
        )
        common = (completed.stdout or "").strip()
        if completed.returncode == 0 and common:
            common_path = Path(common)
            if not common_path.is_absolute():
                common_path = cwd / common_path
            common_path = common_path.resolve()
            return common_path.parent if common_path.name == ".git" else common_path
    except Exception:
        pass
    return cwd



def _run_git(args: list[str], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )



def _parse_worktree_porcelain(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                entries.append(current)
                current = None
            continue
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = {
                "path": line.removeprefix("worktree ").strip(),
                "head": None,
                "branch": None,
                "detached": False,
                "bare": False,
                "locked": False,
                "locked_reason": None,
                "prunable": False,
                "prunable_reason": None,
            }
            continue
        if current is None:
            continue
        if line.startswith("HEAD "):
            current["head"] = line.removeprefix("HEAD ").strip()
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ").strip()
            current["branch"] = branch.removeprefix("refs/heads/")
        elif line == "detached":
            current["detached"] = True
        elif line == "bare":
            current["bare"] = True
        elif line.startswith("locked"):
            current["locked"] = True
            reason = line.removeprefix("locked").strip()
            current["locked_reason"] = _scrub(reason) if reason else None
        elif line.startswith("prunable"):
            current["prunable"] = True
            reason = line.removeprefix("prunable").strip()
            current["prunable_reason"] = _scrub(reason) if reason else None
    if current:
        entries.append(current)
    return entries



def _worktree_label(path_value: object, repo_root: Path) -> str:
    try:
        path = Path(str(path_value)).resolve()
    except Exception:
        return "unknown"
    try:
        if path == repo_root.resolve():
            return "main checkout"
    except Exception:
        pass
    parts = list(path.parts)
    if ".worktrees" in parts:
        idx = parts.index(".worktrees")
        tail = parts[idx + 1:]
        if not tail:
            return "worktree"
        if tail[0] == "kanban" and len(tail) > 1:
            return f"kanban:{tail[1]}"
        leaf = tail[0]
        if leaf.startswith("codex-"):
            return f"codex:{leaf.removeprefix('codex-') or 'worktree'}"
        return f"worktree:{leaf}"
    return f"external:{path.name}" if path.name else "external:worktree"



def _relation_for(label: str, branch: str | None) -> str:
    branch = branch or ""
    if label == "main checkout":
        return "main"
    if branch.startswith("kanban/") or label.startswith("kanban:"):
        return "kanban"
    if branch.startswith("codex/") or label.startswith("codex:"):
        return "codex"
    return "manual"



def _task_hint_for(label: str, branch: str | None, relation: str) -> str | None:
    if relation != "kanban":
        return None
    branch = branch or ""
    if branch.startswith("kanban/"):
        return branch.removeprefix("kanban/") or None
    if label.startswith("kanban:"):
        return label.split(":", 1)[1] or None
    return None



def _state_for(item: dict[str, Any]) -> str:
    if item.get("prunable"):
        return "prunable"
    if _safe_int(item.get("dirty_count"), 0) > 0:
        return "dirty"
    if item.get("locked"):
        return "locked"
    if not item.get("status_checked", True):
        return "unknown"
    return "clean"



def _status_counts(path_value: object, deadline: float) -> dict[str, Any]:
    if time.monotonic() >= deadline:
        return {"status_checked": False, "dirty_count": None, "untracked_count": None}
    try:
        path = Path(str(path_value))
        completed = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain=v1", "--branch"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_WORKTREE_STATUS_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            return {"status_checked": False, "dirty_count": None, "untracked_count": None}
        dirty = 0
        untracked = 0
        staged = 0
        unstaged = 0
        for line in (completed.stdout or "").splitlines():
            if not line or line.startswith("##"):
                continue
            dirty += 1
            if line.startswith("??"):
                untracked += 1
                continue
            if line[0] != " ":
                staged += 1
            if len(line) > 1 and line[1] != " ":
                unstaged += 1
        return {
            "status_checked": True,
            "dirty_count": dirty,
            "untracked_count": untracked,
            "staged_count": staged,
            "unstaged_count": unstaged,
        }
    except Exception:
        return {"status_checked": False, "dirty_count": None, "untracked_count": None}



def _collect_worktrees(repo_root: Path, errors: list[str]) -> list[dict[str, Any]]:
    try:
        completed = _run_git(["worktree", "list", "--porcelain"], cwd=repo_root, timeout=_GIT_LIST_TIMEOUT_SECONDS)
        if completed.returncode != 0:
            raise RuntimeError("git worktree list failed")
        items = _parse_worktree_porcelain(completed.stdout or "")
    except Exception as exc:
        _error(errors, "worktrees", exc)
        return []

    deadline = time.monotonic() + _WORKTREE_STATUS_BUDGET_SECONDS
    for item in items:
        item.update(_status_counts(item.get("path"), deadline))
    return items



def _normalize_worktree(item: dict[str, Any], repo_root: Path, active_worker_task_ids: set[str]) -> dict[str, Any]:
    label = _worktree_label(item.get("path"), repo_root)
    branch = item.get("branch")
    branch = str(branch) if branch else ("detached" if item.get("detached") else "unknown")
    relation = _relation_for(label, branch)
    task_hint = _task_hint_for(label, branch, relation)
    status_checked = bool(item.get("status_checked", False))
    dirty_count = item.get("dirty_count") if status_checked else None
    untracked_count = item.get("untracked_count") if status_checked else None
    normalized = {
        "id": f"{relation}:{label}",
        "path_label": label,
        "branch": _scrub(branch),
        "head": str(item.get("head") or "")[:12] or None,
        "relation": relation,
        "task_hint": _scrub(task_hint) if task_hint else None,
        "state": "unknown",
        "locked": bool(item.get("locked")),
        "prunable": bool(item.get("prunable")),
        "detached": bool(item.get("detached")),
        "dirty_count": _safe_int(dirty_count, 0) if dirty_count is not None else None,
        "untracked_count": _safe_int(untracked_count, 0) if untracked_count is not None else None,
        "status_checked": status_checked,
        "orphaned": bool(relation == "kanban" and task_hint and task_hint not in active_worker_task_ids),
    }
    normalized["state"] = _state_for(normalized)
    return normalized



def _classify_process(name: str, cmdline: Iterable[str]) -> str | None:
    base = (name or "").lower()
    text = " ".join(cmdline).lower()
    spaced = f" {text} "
    if "pytest" in text or base in {"pytest", "py.test", "tox", "nox"}:
        return "test_runner"
    if "playwright" in text or "ms-playwright" in text or base in {"chromium", "chrome"}:
        return "browser_test"
    if "claude" in base or " claude " in spaced or "claude-code" in text:
        return "claude_code"
    if "kimi" in base or " kimi " in spaced or "moonshot" in text:
        return "kimi"
    if base.startswith("codex") or " codex " in spaced:
        return "codex"
    if "hermes_cli.main dashboard" in text or " dashboard " in spaced or "gateway run" in text:
        return "hermes_daemon"
    return None



def _collect_process_actor_groups(errors: list[str]) -> list[dict[str, Any]]:
    try:
        import psutil  # type: ignore
    except Exception as exc:
        _error(errors, "processes", exc)
        return []

    grouped: dict[str, dict[str, Any]] = {}
    seen: set[int] = set()
    now = time.monotonic()
    wall_now = time.time()
    try:
        iterator = psutil.process_iter(["pid", "name", "cmdline", "memory_info", "cpu_times", "create_time"])
    except Exception as exc:
        _error(errors, "processes", exc)
        return []

    for proc in iterator:
        try:
            info = proc.info
            pid = int(info.get("pid") or proc.pid)
            role = _classify_process(str(info.get("name") or ""), info.get("cmdline") or [])
            if role is None:
                continue
            seen.add(pid)
            bucket = grouped.setdefault(
                role,
                {
                    "role": role,
                    "label": _ROLE_LABELS.get(role, role.replace("_", " ").title()),
                    "count": 0,
                    "cpu_percent": 0.0,
                    "rss_mb": 0.0,
                    "oldest_age_seconds": None,
                    "source": "process",
                    "confidence": "medium",
                    "stale_count": 0,
                    "target": "/control/ops",
                },
            )
            bucket["count"] += 1
            cpu_seconds = _cpu_seconds(info.get("cpu_times"))
            if cpu_seconds is not None:
                bucket["cpu_percent"] += _cpu_percent_from_sample(pid, now, cpu_seconds)
            memory_info = info.get("memory_info")
            bucket["rss_mb"] += float(getattr(memory_info, "rss", 0) or 0) / 1024 / 1024
            create_time = info.get("create_time")
            if create_time:
                age = max(0, int(wall_now - float(create_time)))
                current = bucket.get("oldest_age_seconds")
                bucket["oldest_age_seconds"] = age if current is None else max(_safe_int(current), age)
        except Exception:
            continue
    _prune_cpu_cache(seen)
    out = list(grouped.values())
    for item in out:
        item["cpu_percent"] = round(float(item.get("cpu_percent") or 0.0), 1)
        item["rss_mb"] = round(float(item.get("rss_mb") or 0.0), 1)
    return out



def _collect_kanban_worker_group(errors: list[str]) -> tuple[list[dict[str, Any]], set[str]]:
    try:
        from hermes_cli import kanban_db

        db_path = kanban_db.kanban_db_path()
        if not db_path.exists():
            return [], set()
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=200")
    except Exception as exc:
        _error(errors, "kanban_workers", exc)
        return [], set()

    try:
        rows = conn.execute(
            """
            SELECT
                r.id AS run_id,
                r.task_id,
                r.profile,
                r.worker_pid,
                r.started_at,
                r.last_heartbeat_at,
                r.status AS run_status
            FROM task_runs r
            JOIN tasks t ON t.id = r.task_id
            WHERE r.ended_at IS NULL
              AND r.worker_pid IS NOT NULL
              AND t.status = 'running'
            ORDER BY r.started_at ASC
            """
        ).fetchall()
    except Exception as exc:
        _error(errors, "kanban_workers", exc)
        return [], set()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    workers = [dict(row) for row in rows]
    task_ids = {str(worker.get("task_id")) for worker in workers if worker.get("task_id")}
    if not workers:
        return [], task_ids

    now = int(time.time())
    stale_count = 0
    oldest_age: int | None = None
    for worker in workers:
        started = _safe_int(worker.get("started_at"), now)
        age = max(0, now - started)
        oldest_age = age if oldest_age is None else max(oldest_age, age)
        heartbeat = _safe_int(worker.get("last_heartbeat_at"), now)
        if now - heartbeat > 120:
            stale_count += 1

    return [
        {
            "role": "kanban_worker",
            "label": _ROLE_LABELS["kanban_worker"],
            "count": len(workers),
            "cpu_percent": 0.0,
            "rss_mb": 0.0,
            "oldest_age_seconds": oldest_age,
            "source": "canonical",
            "confidence": "high",
            "stale_count": stale_count,
            "task_ids": sorted(task_ids)[:12],
            "target": "/control/flow",
        }
    ], task_ids


def _collect_actor_groups(errors: list[str]) -> tuple[list[dict[str, Any]], set[str]]:
    worker_groups, active_worker_task_ids = _collect_kanban_worker_group(errors)
    groups = [*worker_groups, *_collect_process_actor_groups(errors)]
    return groups, active_worker_task_ids



def _normalize_actor(actor: dict[str, Any]) -> dict[str, Any]:
    role = str(actor.get("role") or "unknown")
    source = str(actor.get("source") or "process")
    return {
        "role": role,
        "label": _scrub(actor.get("label") or _ROLE_LABELS.get(role, role.replace("_", " ").title())),
        "count": _safe_int(actor.get("count"), 0),
        "cpu_percent": round(_safe_float(actor.get("cpu_percent")), 1),
        "rss_mb": round(_safe_float(actor.get("rss_mb")), 1),
        "oldest_age_seconds": actor.get("oldest_age_seconds"),
        "source": source if source in {"canonical", "process"} else "process",
        "confidence": str(actor.get("confidence") or ("high" if source == "canonical" else "medium")),
        "stale_count": _safe_int(actor.get("stale_count"), 0),
        "target": str(actor.get("target") or "/control/ops"),
        "controllable": False,
    }



def _actor_sort_key(actor: dict[str, Any]) -> tuple[int, int, str]:
    source_rank = 0 if actor.get("source") == "canonical" else 1
    role_rank = _ROLE_ORDER.get(str(actor.get("role") or ""), 99)
    return source_rank, role_rank, str(actor.get("label") or "")



def _lever(action: str, label: str, detail: str, tone: str, count: int, target: str) -> dict[str, Any]:
    return {
        "action": action,
        "label": label,
        "detail": detail,
        "tone": tone,
        "count": count,
        "target": target,
        "mutation": "none",
    }



def _build_levers(summary: dict[str, int], actors: list[dict[str, Any]], errors: list[str]) -> list[dict[str, Any]]:
    levers: list[dict[str, Any]] = []
    if summary["worktrees_dirty"]:
        levers.append(_lever(
            "inspect_dirty_worktrees",
            "Dirty Worktrees",
            f"{summary['worktrees_dirty']} Worktree(s) haben echte Git-Aenderungen.",
            "amber",
            summary["worktrees_dirty"],
            "/control/ops?filter=dirty",
        ))
    if summary["worktrees_locked"]:
        levers.append(_lever(
            "inspect_locked_worktrees",
            "Locked Worktrees",
            f"{summary['worktrees_locked']} Worktree(s) sind gelockt und sollten bewusst bleiben oder aufgeraeumt werden.",
            "cyan",
            summary["worktrees_locked"],
            "/control/ops?filter=locked",
        ))
    if summary["worktrees_orphaned"]:
        levers.append(_lever(
            "inspect_orphan_worktrees",
            "Worktree ohne Worker",
            f"{summary['worktrees_orphaned']} Kanban-Worktree(s) haben keinen aktiven Worker-Match.",
            "rose",
            summary["worktrees_orphaned"],
            "/control/ops?filter=orphaned",
        ))
    stale_workers = sum(_safe_int(actor.get("stale_count"), 0) for actor in actors)
    if stale_workers:
        levers.append(_lever(
            "inspect_stale_workers",
            "Worker Heartbeat",
            f"{stale_workers} Worker melden einen alten Heartbeat.",
            "red",
            stale_workers,
            "/control/flow",
        ))
    if summary["worktrees_status_unknown"]:
        levers.append(_lever(
            "complete_inventory_probe",
            "Inventar unvollstaendig",
            f"{summary['worktrees_status_unknown']} Worktree-Statusproben liefen ausserhalb des Zeitbudgets.",
            "amber",
            summary["worktrees_status_unknown"],
            "/control/ops?filter=unknown",
        ))
    if summary["worktrees_prunable"]:
        levers.append(_lever(
            "inspect_prunable_worktrees",
            "Prunable Worktrees",
            f"{summary['worktrees_prunable']} Worktree(s) meldet Git als prunable.",
            "amber",
            summary["worktrees_prunable"],
            "/control/ops?filter=prunable",
        ))
    process_agents = sum(
        _safe_int(actor.get("count"), 0)
        for actor in actors
        if actor.get("role") in {"codex", "claude_code", "kimi"}
    )
    canonical_workers = sum(
        _safe_int(actor.get("count"), 0)
        for actor in actors
        if actor.get("source") == "canonical"
    )
    if process_agents and not canonical_workers:
        levers.append(_lever(
            "inspect_unbound_agents",
            "Agenten ohne Kanban-Match",
            f"{process_agents} interaktive Agent-Prozesse laufen ausserhalb aktiver Kanban-Worker.",
            "cyan",
            process_agents,
            "/control/ops?filter=agents",
        ))
    test_actors = sum(
        _safe_int(actor.get("count"), 0)
        for actor in actors
        if actor.get("role") in {"test_runner", "browser_test"}
    )
    if test_actors:
        levers.append(_lever(
            "let_tests_finish",
            "Tests laufen",
            f"{test_actors} Test-/Browser-Prozess(e) sind aktiv.",
            "cyan",
            test_actors,
            "/control/pressure",
        ))
    if errors:
        levers.append(_lever(
            "inspect_inventory_errors",
            "Teilwerte unklar",
            f"{len(errors)} Inventarquelle(n) konnten nicht voll gelesen werden.",
            "amber",
            len(errors),
            "/control/ops",
        ))
    if not levers:
        levers.append(_lever(
            "observe",
            "Alles ruhig",
            "Keine Dirty-, Orphan-, Stale- oder Actor-Mismatch-Signale erkannt.",
            "emerald",
            0,
            "/control/ops",
        ))
    return levers[:8]



def build_operator_inventory(payload: dict[str, Any], *, repo_root: Path | None = None) -> dict[str, Any]:
    repo = repo_root or _git_common_root(_project_root())
    errors = list(payload.get("errors") or [])
    active_worker_task_ids = {str(value) for value in (payload.get("active_worker_task_ids") or []) if value}
    actors = [_normalize_actor(actor) for actor in (payload.get("actor_groups") or [])]
    actors.sort(key=_actor_sort_key)
    for actor in actors:
        for key in ("task_ids",):
            actor.pop(key, None)
    worktrees = [
        _normalize_worktree(item, repo, active_worker_task_ids)
        for item in (payload.get("worktrees") or [])
    ]
    summary = {
        "worktrees_total": len(worktrees),
        "worktrees_locked": sum(1 for item in worktrees if item.get("locked")),
        "worktrees_dirty": sum(1 for item in worktrees if _safe_int(item.get("dirty_count"), 0) > 0),
        "worktrees_prunable": sum(1 for item in worktrees if item.get("prunable")),
        "worktrees_orphaned": sum(1 for item in worktrees if item.get("orphaned")),
        "worktrees_status_unknown": sum(1 for item in worktrees if not item.get("status_checked")),
        "actors_total": sum(_safe_int(actor.get("count"), 0) for actor in actors),
        "actors_canonical": sum(_safe_int(actor.get("count"), 0) for actor in actors if actor.get("source") == "canonical"),
    }
    levers = _build_levers(summary, actors, errors)
    return {
        "schema": _SCHEMA,
        "checked_at": _safe_int(payload.get("checked_at"), int(time.time())),
        "summary": summary,
        "next_lever": levers[0],
        "levers": levers,
        "worktrees": worktrees[:80],
        "actors": actors[:24],
        "errors": errors,
    }



def snapshot(*, force: bool = False) -> dict[str, Any]:
    global _CACHE
    now = time.monotonic()
    if not force and _CACHE and now - _CACHE[0] < _CACHE_TTL_SECONDS:
        return json.loads(json.dumps(_CACHE[1]))
    errors: list[str] = []
    repo_root = _git_common_root(_project_root())
    actor_groups, active_worker_task_ids = _collect_actor_groups(errors)
    payload = build_operator_inventory(
        {
            "checked_at": int(time.time()),
            "worktrees": _collect_worktrees(repo_root, errors),
            "actor_groups": actor_groups,
            "active_worker_task_ids": sorted(active_worker_task_ids),
            "errors": errors,
        },
        repo_root=repo_root,
    )
    _CACHE = (now, payload)
    return json.loads(json.dumps(payload))



def _error_envelope(exc: BaseException) -> dict[str, Any]:
    return build_operator_inventory(
        {
            "checked_at": int(time.time()),
            "worktrees": [],
            "actor_groups": [],
            "active_worker_task_ids": [],
            "errors": [f"operator_inventory: {_scrub(type(exc).__name__)}"],
        },
        repo_root=_project_root(),
    )



def register_operator_inventory_routes(app: FastAPI) -> None:
    """Register the protected read-only operator inventory endpoint."""

    @app.get("/api/operator-inventory")
    def operator_inventory() -> dict[str, Any]:
        try:
            return snapshot()
        except Exception as exc:
            return _error_envelope(exc)
