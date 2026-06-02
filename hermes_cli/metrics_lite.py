"""In-process self-metrics for the control dashboard (deliberately not Prometheus).

A fixed-size in-memory ring per route-group records (latency_ms, is_error). The
dashboard reads an aggregate (count / error_rate / p50 / p95) from
``GET /api/metrics-lite`` so the operator can tell "backend slow" from "frontend
paused". Zero persistence (counters reset on restart — by design, surfaced via
``uptime_seconds``), zero new dependencies, removable in minutes.

Modelled on hermes_cli/health_status.py: the endpoint never raises 5xx.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, Tuple

from fastapi import FastAPI

_SCHEMA = "hermes-metrics-lite-v1"
_RING_SIZE = 256

# group -> ring of (latency_ms, is_error). Bounded set of groups (route prefixes
# with ids collapsed to "*"), so total memory is fixed: groups × 256 × small tuple.
_RINGS: Dict[str, Deque[Tuple[float, bool]]] = {}
_START_TIME = time.time()


def _looks_like_id(seg: str) -> bool:
    """Heuristic: collapse id-like path segments so /runs/123 and /runs/456 share a group."""
    if seg.isdigit():
        return True
    # Hash/uuid-ish: reasonably long and containing at least a couple of digits
    # (e.g. cron job id "16dd6ac01fc0", run hashes). Plain words like
    # "health-status" / "recent-results" have no digits and are kept.
    if len(seg) >= 10 and sum(c.isdigit() for c in seg) >= 2:
        return True
    return False


def route_group(path: str) -> str:
    """Map a request path to a stable, low-cardinality group key."""
    if not path.startswith("/api/"):
        if path.startswith("/assets/") or path.startswith("/fonts/"):
            return "static"
        return "spa"
    segments = [seg for seg in path.split("/") if seg]
    normalized = ["*" if _looks_like_id(seg) else seg for seg in segments]
    return "/" + "/".join(normalized)


def record(group: str, latency_ms: float, is_error: bool) -> None:
    """O(1) hot-path record. Tolerates bad args (never raises into the request)."""
    try:
        ring = _RINGS.get(group)
        if ring is None:
            ring = deque(maxlen=_RING_SIZE)
            _RINGS[group] = ring
        ring.append((float(latency_ms), bool(is_error)))
    except Exception:
        pass


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct / 100.0 * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def snapshot() -> Dict[str, Any]:
    groups: Dict[str, Any] = {}
    for group, ring in list(_RINGS.items()):
        samples = list(ring)
        count = len(samples)
        if count == 0:
            continue
        error_count = sum(1 for _, is_err in samples if is_err)
        latencies = sorted(lat for lat, _ in samples)
        groups[group] = {
            "count": count,
            "error_count": error_count,
            "error_rate": round(error_count / count, 4),
            "p50_ms": round(_percentile(latencies, 50), 1),
            "p95_ms": round(_percentile(latencies, 95), 1),
        }
    return {
        "schema": _SCHEMA,
        "checked_at": int(time.time()),
        "uptime_seconds": int(time.time() - _START_TIME),
        "groups": groups,
    }


def reset() -> None:
    """Clear all rings (test helper)."""
    _RINGS.clear()


def register_metrics_lite_routes(app: FastAPI) -> None:
    """Register the loopback-gated metrics endpoint before the SPA catch-all."""

    @app.get("/api/metrics-lite")
    async def metrics_lite() -> Dict[str, Any]:
        try:
            return snapshot()
        except Exception as exc:  # never 500 — degrade to an error envelope
            return {
                "schema": _SCHEMA,
                "checked_at": int(time.time()),
                "uptime_seconds": 0,
                "groups": {},
                "error": str(exc),
            }
