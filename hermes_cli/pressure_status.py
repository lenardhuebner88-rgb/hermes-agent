"""Read-only host pressure snapshot for the Hermes control dashboard.

The endpoint answers one operator question: is the dashboard slow because the
host is busy, the API is slow, or the tailnet path is degraded? It deliberately
returns roles and counters, not raw command lines, paths, environment values, or
operator data.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

from fastapi import FastAPI

_SCHEMA = "hermes-pressure-v1"
_TAILSCALE_CACHE_TTL_SECONDS = 30.0
_TAILSCALE_TIMEOUT_SECONDS = 0.8
_PRESSURE_CACHE_TTL_SECONDS = 8.0
_TAILSCALE_CACHE: tuple[float, dict[str, Any]] | None = None
_PRESSURE_CACHE: tuple[float, dict[str, Any]] | None = None
_DASHBOARD_CPU_CACHE: dict[int, tuple[float, float]] = {}
_PROCESS_CPU_CACHE: dict[int, tuple[float, float]] = {}
_SOURCE_KIND_PRIORITY = {
    "test": 0,
    "browser_test": 1,
    "agent": 2,
    "hermes_service": 3,
}

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


def _scrub(value: object) -> str:
    text = str(value)
    lowered = text.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return "redacted"
    if "/home/" in text or "\\Users\\" in text or ".worktrees/" in text:
        return "redacted"
    if len(text) > 120:
        return text[:117] + "..."
    return text


def _error(errors: list[str], label: str, exc: BaseException) -> None:
    errors.append(f"{label}: {_scrub(type(exc).__name__)}")


def _safe_round(value: Any, digits: int = 1) -> float | None:
    try:
        return round(float(value), digits)
    except Exception:
        return None


def _cpu_seconds(cpu_times: Any) -> float | None:
    try:
        return float(getattr(cpu_times, "user", 0.0)) + float(getattr(cpu_times, "system", 0.0))
    except Exception:
        return None


def _cpu_percent_from_sample(
    cache: dict[int, tuple[float, float]],
    pid: int,
    now: float,
    cpu_seconds: float,
) -> float:
    previous = cache.get(pid)
    cache[pid] = (now, cpu_seconds)
    if previous is None:
        return 0.0
    elapsed = now - previous[0]
    if elapsed <= 0:
        return 0.0
    delta = max(0.0, cpu_seconds - previous[1])
    return round((delta / elapsed) * 100.0, 1)


def _prune_cpu_cache(cache: dict[int, tuple[float, float]], seen_pids: set[int]) -> None:
    stale = [pid for pid in cache if pid not in seen_pids]
    for pid in stale:
        cache.pop(pid, None)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return None


def _cgroup_path_for_pid(pid: int) -> str | None:
    text = _read_text(Path(f"/proc/{pid}/cgroup"))
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            return line.split("0::", 1)[1] or "/"
    return None


def _cgroup_file(cgroup_path: str | None, filename: str) -> Path | None:
    if not cgroup_path:
        return None
    rel = cgroup_path.lstrip("/")
    return Path("/sys/fs/cgroup") / rel / filename


def _read_cgroup_value(cgroup_path: str | None, filename: str) -> str | None:
    path = _cgroup_file(cgroup_path, filename)
    return _read_text(path) if path else None


def _cpu_quota_from_cpu_max(raw: str | None) -> str:
    if not raw:
        return "unknown"
    quota = raw.split()[0] if raw.split() else "max"
    return "infinity" if quota == "max" else quota


def _is_throttled(cgroup_path: str | None) -> bool:
    cpu_max = _read_cgroup_value(cgroup_path, "cpu.max")
    if cpu_max and cpu_max.split() and cpu_max.split()[0] != "max":
        return True
    return False


def _scope_kind(cgroup_path: str | None) -> tuple[str, str]:
    if not cgroup_path:
        return "unknown", "unknown"
    leaf = cgroup_path.rsplit("/", 1)[-1]
    if leaf.endswith(".service"):
        return "service", "service"
    if leaf.startswith("session-") and leaf.endswith(".scope"):
        return "session", "session scope"
    if leaf.endswith(".scope"):
        return "scope", "systemd scope"
    return "cgroup", "cgroup"


def _collect_host(errors: list[str]) -> dict[str, Any]:
    host: dict[str, Any] = {
        "cpu_count": os.cpu_count() or 1,
        "cpu_percent": None,
        "load_avg": [],
        "memory_percent": None,
    }
    try:
        import psutil  # type: ignore

        host["cpu_percent"] = _safe_round(psutil.cpu_percent(interval=None))
        load_avg = getattr(psutil, "getloadavg", None)
        if load_avg:
            host["load_avg"] = [round(float(v), 2) for v in load_avg()]
        memory = psutil.virtual_memory()
        host["memory_percent"] = _safe_round(memory.percent)
    except Exception as exc:
        _error(errors, "host", exc)
        try:
            host["load_avg"] = [round(float(v), 2) for v in os.getloadavg()]
        except Exception:
            pass
    return host


def _collect_dashboard(errors: list[str]) -> dict[str, Any]:
    pid = os.getpid()
    cgroup_path = _cgroup_path_for_pid(pid)
    dashboard: dict[str, Any] = {
        "pid": pid,
        "rss_mb": None,
        "cpu_percent": None,
        "cpu_weight": None,
        "cpu_quota": _cpu_quota_from_cpu_max(_read_cgroup_value(cgroup_path, "cpu.max")),
        "tasks_current": None,
    }
    weight = _read_cgroup_value(cgroup_path, "cpu.weight")
    if weight and weight.isdigit():
        dashboard["cpu_weight"] = int(weight)
    pids_current = _read_cgroup_value(cgroup_path, "pids.current")
    if pids_current and pids_current.isdigit():
        dashboard["tasks_current"] = int(pids_current)
    try:
        import psutil  # type: ignore

        proc = psutil.Process(pid)
        dashboard["rss_mb"] = round(proc.memory_info().rss / 1024 / 1024, 1)
        cpu_seconds = _cpu_seconds(proc.cpu_times())
        if cpu_seconds is not None:
            dashboard["cpu_percent"] = _cpu_percent_from_sample(
                _DASHBOARD_CPU_CACHE,
                pid,
                time.monotonic(),
                cpu_seconds,
            )
        dashboard["num_threads"] = proc.num_threads()
    except Exception as exc:
        _error(errors, "dashboard", exc)
    return dashboard


def _classify_process(name: str, cmdline: Iterable[str]) -> tuple[str, str] | None:
    base = (name or "").lower()
    text = " ".join(cmdline).lower()
    if base in {"rg", "grep", "sed", "awk", "head", "tail"}:
        return None
    if base in {"bash", "sh"} and (" rg " in f" {text} " or " grep " in f" {text} "):
        return None
    if "run_tests_parallel.py" in text:
        return "test", "parallel tests"
    if "pytest" in text or base in {"pytest", "py.test"}:
        return "test", "pytest"
    if "tox" in text or base == "tox":
        return "test", "tox"
    if "nox" in text or base == "nox":
        return "test", "nox"
    if "playwright" in text or "ms-playwright" in text or base in {"chromium", "chrome"}:
        return "browser_test", "browser test"
    if "claude remote-control" in text:
        return "agent", "claude remote"
    if "hermes" in text and " chat " in f" {text} ":
        return "agent", "hermes chat"
    if base.startswith("codex") or " codex " in f" {text} ":
        return "agent", "codex"
    if "hermes_cli.main dashboard" in text or " dashboard " in f" {text} ":
        return "hermes_service", "dashboard"
    if "gateway run" in text:
        return "hermes_service", "gateway"
    return None


def _collect_pressure_sources(errors: list[str]) -> list[dict[str, Any]]:
    try:
        import psutil  # type: ignore
    except Exception as exc:
        _error(errors, "processes", exc)
        return []

    grouped: dict[tuple[str, str, str, bool], dict[str, Any]] = {}
    try:
        iterator = psutil.process_iter(["pid", "name", "cmdline", "memory_info", "cpu_times"])
    except Exception as exc:
        _error(errors, "processes", exc)
        return []

    now = time.monotonic()
    seen_pids: set[int] = set()
    for proc in iterator:
        try:
            info = proc.info
            pid = int(info.get("pid") or proc.pid)
            cmdline = info.get("cmdline") or []
            classified = _classify_process(str(info.get("name") or ""), cmdline)
            if classified is None:
                continue
            kind, label = classified
            cgroup_path = _cgroup_path_for_pid(pid)
            scope_kind, scope_label = _scope_kind(cgroup_path)
            throttled = _is_throttled(cgroup_path)
            key = (kind, label, scope_label, throttled)
            bucket = grouped.setdefault(
                key,
                {
                    "kind": kind,
                    "label": label,
                    "count": 0,
                    "cpu_percent": 0.0,
                    "rss_mb": 0.0,
                    "scope": scope_label,
                    "scope_kind": scope_kind,
                    "throttled": throttled,
                },
            )
            bucket["count"] += 1
            seen_pids.add(pid)
            cpu_seconds = _cpu_seconds(info.get("cpu_times"))
            if cpu_seconds is not None:
                bucket["cpu_percent"] += _cpu_percent_from_sample(_PROCESS_CPU_CACHE, pid, now, cpu_seconds)
            memory_info = info.get("memory_info")
            rss = getattr(memory_info, "rss", 0) if memory_info is not None else 0
            bucket["rss_mb"] += float(rss) / 1024 / 1024
        except Exception:
            continue

    _prune_cpu_cache(_PROCESS_CPU_CACHE, seen_pids)
    sources = list(grouped.values())
    for source in sources:
        source["cpu_percent"] = round(float(source["cpu_percent"]), 1)
        source["rss_mb"] = round(float(source["rss_mb"]), 1)
    sources.sort(key=_pressure_source_sort_key)
    return sources[:8]


def _api_latency_ms() -> float | None:
    try:
        from hermes_cli.metrics_lite import snapshot as metrics_snapshot

        groups = metrics_snapshot().get("groups", {})
        status = groups.get("/api/status") or groups.get("/api/pressure-status")
        if status:
            return _safe_round(status.get("p95_ms"))
        api_groups = [group for key, group in groups.items() if str(key).startswith("/api/")]
        values = [float(group.get("p95_ms") or 0) for group in api_groups]
        return round(max(values), 1) if values else None
    except Exception:
        return None


def _tailnet_status(errors: list[str]) -> dict[str, Any]:
    global _TAILSCALE_CACHE
    now = time.monotonic()
    if _TAILSCALE_CACHE and now - _TAILSCALE_CACHE[0] < _TAILSCALE_CACHE_TTL_SECONDS:
        return dict(_TAILSCALE_CACHE[1])
    if os.environ.get("HERMES_PRESSURE_TAILSCALE", "1") == "0":
        result = {"tailnet": "unknown", "detail": "tailnet probe disabled"}
        _TAILSCALE_CACHE = (now, result)
        return dict(result)
    try:
        completed = subprocess.run(
            ["tailscale", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TAILSCALE_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise RuntimeError("tailscale status unavailable")
        data = json.loads(completed.stdout or "{}")
        peers = data.get("Peer") or {}
        active = [peer for peer in peers.values() if isinstance(peer, dict) and peer.get("Active")]
        if not active:
            result = {"tailnet": "inactive", "detail": "no active tailnet peers"}
        elif any(peer.get("Relay") for peer in active):
            result = {"tailnet": "relay", "detail": "tailnet relay path active"}
        else:
            result = {"tailnet": "direct", "detail": "tailnet direct"}
    except Exception as exc:
        _error(errors, "tailnet", exc)
        result = {"tailnet": "unknown", "detail": "tailnet probe unavailable"}
    _TAILSCALE_CACHE = (now, result)
    return dict(result)


def _collect_access(errors: list[str]) -> dict[str, Any]:
    access = _tailnet_status(errors)
    access["api_latency_ms"] = _api_latency_ms()
    return access


def _collect_token_pressure() -> dict[str, Any]:
    try:
        from gateway.status import read_runtime_status

        runtime = read_runtime_status() or {}
        token_usage = runtime.get("token_usage") or {}
        return {
            "class": token_usage.get("pressure_class") or "unknown",
            "pct": token_usage.get("pressure_pct"),
            "updated_at": runtime.get("updated_at"),
        }
    except Exception:
        return {"class": "unknown", "pct": None}


def _load_values(host: dict[str, Any]) -> tuple[float, float, int]:
    load = host.get("load_avg") or []
    load1 = float(load[0]) if len(load) > 0 and load[0] is not None else 0.0
    load5 = float(load[1]) if len(load) > 1 and load[1] is not None else 0.0
    cores = int(host.get("cpu_count") or os.cpu_count() or 1)
    return load1, load5, max(1, cores)


def _has_unthrottled_tests(sources: list[dict[str, Any]]) -> bool:
    return any(
        source.get("kind") in {"test", "browser_test"}
        and source.get("scope_kind") == "session"
        and not source.get("throttled")
        for source in sources
    )


def _pressure_source_sort_key(item: dict[str, Any]) -> tuple[int, float, int]:
    priority = _SOURCE_KIND_PRIORITY.get(str(item.get("kind") or ""), 9)
    cpu_percent = _safe_round(item.get("cpu_percent")) or 0.0
    try:
        count = int(item.get("count") or 0)
    except Exception:
        count = 0
    return priority, -cpu_percent, -count


def _max_source_cpu(sources: list[dict[str, Any]]) -> float:
    values = [_safe_round(source.get("cpu_percent")) or 0.0 for source in sources]
    return max(values) if values else 0.0


def _source_count(sources: list[dict[str, Any]], *kinds: str) -> int:
    allowed = set(kinds)
    total = 0
    for source in sources:
        if source.get("kind") not in allowed:
            continue
        try:
            total += int(source.get("count") or 0)
        except Exception:
            continue
    return total


def _active_process_detail(test_count: int, browser_count: int) -> str:
    total = test_count + browser_count
    if test_count and browser_count:
        noun = "Test-/Browser-Prozesse"
    elif browser_count:
        noun = "Browser-Testprozess" if total == 1 else "Browser-Testprozesse"
    else:
        noun = "Testprozess" if total == 1 else "Testprozesse"
    return f"{total} {noun} aktiv."


def _recommendation(payload: dict[str, Any]) -> dict[str, str]:
    host = payload.get("host") or {}
    access = payload.get("access") or {}
    sources = payload.get("pressure_sources") or []
    load1, load5, cores = _load_values(host)
    cpu = _safe_round(host.get("cpu_percent"))
    api_latency = _safe_round(access.get("api_latency_ms"))
    test_count = _source_count(sources, "test")
    browser_count = _source_count(sources, "browser_test")
    agent_count = _source_count(sources, "agent")
    service_count = _source_count(sources, "hermes_service")

    if access.get("tailnet") == "relay":
        return {"label": "Tailnet relay", "detail": "Tailnet nutzt Relay; Direktpfad pruefen.", "tone": "amber"}
    if test_count or browser_count:
        return {"label": "Tests laufen", "detail": _active_process_detail(test_count, browser_count), "tone": "amber"}
    if api_latency is not None and api_latency >= 500:
        tone = "red" if api_latency > 1500 else "amber"
        return {"label": "API p95 hoch", "detail": f"{int(round(api_latency))}ms p95 aus Selbstmetriken.", "tone": tone}
    if load1 >= cores * 0.5 or load5 >= cores * 0.67 or (cpu is not None and cpu >= 70):
        tone = "red" if load1 > cores or load5 > cores or (cpu is not None and cpu >= 90) else "amber"
        return {"label": "Host Load hoch", "detail": f"Load {load1:.1f}/{cores}; Host-CPU {int(round(cpu or 0))}%.", "tone": tone}
    if agent_count:
        return {"label": "Agents aktiv", "detail": f"{agent_count} Agent-Prozess{'e' if agent_count != 1 else ''} erkannt.", "tone": "cyan"}
    if service_count:
        return {"label": "Hermes-Dienste", "detail": f"{service_count} Hermes-Dienstprozess{'e' if service_count != 1 else ''} aktiv.", "tone": "cyan"}
    if payload.get("errors"):
        return {"label": "Teilwerte unklar", "detail": "Pressure bleibt vorsichtig, weil einzelne Werte fehlen.", "tone": "amber"}
    return {"label": "Kein Hebel", "detail": "Keine auffaellige Last erkannt.", "tone": "emerald"}


def _classify(payload: dict[str, Any]) -> tuple[str, str]:
    host = payload.get("host") or {}
    access = payload.get("access") or {}
    sources = payload.get("pressure_sources") or []
    load1, load5, cores = _load_values(host)
    cpu = _safe_round(host.get("cpu_percent")) or 0.0
    api_latency = _safe_round(access.get("api_latency_ms")) or 0.0
    max_source_cpu = _max_source_cpu(sources)

    if (
        load1 > cores
        or load5 > cores
        or cpu >= 90
        or api_latency > 1500
        or max_source_cpu >= cores * 80
    ):
        return "saturated", "Host und API sind unter deutlichem Druck"

    if _has_unthrottled_tests(sources):
        return "busy", "Ungedrosselte Testprozesse in Session-Scope"

    if (
        load1 >= cores * 0.5
        or load5 >= cores * 0.67
        or cpu >= 70
        or api_latency >= 500
        or access.get("tailnet") == "relay"
        or max_source_cpu >= 100
    ):
        return "busy", "Host ist beschaeftigt, Dashboard antwortet"

    if payload.get("errors"):
        return "unknown", "Pressure teilweise unbekannt"
    return "ok", "Keine auffaellige Last"


def build_pressure_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize collected counters into the API response contract."""
    normalized = {
        "schema": _SCHEMA,
        "checked_at": int(payload.get("checked_at") or time.time()),
        "host": payload.get("host") or {},
        "dashboard": payload.get("dashboard") or {},
        "pressure_sources": payload.get("pressure_sources") or [],
        "access": payload.get("access") or {"tailnet": "unknown", "api_latency_ms": None},
        "token_pressure": payload.get("token_pressure") or {"class": "unknown", "pct": None},
        "errors": list(payload.get("errors") or []),
    }
    overall, cause = _classify(normalized)
    normalized["overall"] = overall
    normalized["cause"] = cause
    normalized["recommendation"] = _recommendation(normalized)
    return normalized


def _clone_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload))


def snapshot(*, force: bool = False) -> dict[str, Any]:
    global _PRESSURE_CACHE
    now = time.monotonic()
    if not force and _PRESSURE_CACHE and now - _PRESSURE_CACHE[0] < _PRESSURE_CACHE_TTL_SECONDS:
        return _clone_payload(_PRESSURE_CACHE[1])
    errors: list[str] = []
    payload = {
        "checked_at": int(time.time()),
        "host": _collect_host(errors),
        "dashboard": _collect_dashboard(errors),
        "pressure_sources": _collect_pressure_sources(errors),
        "access": _collect_access(errors),
        "token_pressure": _collect_token_pressure(),
        "errors": errors,
    }
    result = build_pressure_status(payload)
    _PRESSURE_CACHE = (now, result)
    return _clone_payload(result)


def _error_envelope(exc: BaseException) -> dict[str, Any]:
    return {
        "schema": _SCHEMA,
        "checked_at": int(time.time()),
        "overall": "unknown",
        "cause": "Pressure konnte nicht vollstaendig gelesen werden",
        "recommendation": {"label": "Teilwerte unklar", "detail": "Pressure konnte nicht vollstaendig gelesen werden.", "tone": "amber"},
        "host": {},
        "dashboard": {},
        "pressure_sources": [],
        "access": {"tailnet": "unknown", "api_latency_ms": None},
        "token_pressure": {"class": "unknown", "pct": None},
        "errors": [f"pressure: {_scrub(type(exc).__name__)}"],
    }


def register_pressure_status_routes(app: FastAPI) -> None:
    """Register the protected read-only pressure endpoint before the SPA catch-all."""

    @app.get("/api/pressure-status")
    def pressure_status() -> dict[str, Any]:
        try:
            return snapshot()
        except Exception as exc:
            return _error_envelope(exc)
