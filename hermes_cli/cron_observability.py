"""Read-only cron observability endpoints for the control dashboard.

The dashboard needs to see *what the scheduled jobs actually produced*, not just
``last_status: ok``. This module exposes two loopback/session-gated read
endpoints, modelled on :mod:`hermes_cli.health_status` (never raise 5xx — wrap
failures into a structured payload):

* ``GET /api/cron/observability`` — redacted cron jobs across all profiles
  (no ``prompt``/``script`` leak) bundled with gateway liveness and per-job
  latest-output metadata.
* ``GET /api/cron/observability/output/{job_id}`` — the real rendered run output
  (``~/.hermes/cron/output/<job_id>/<newest>.md``), clipped. This is the
  "output, not status" view that lets the operator verify content.

Heavy lifting (profile retargeting under lock, path-escape protection) is reused
from :mod:`hermes_cli.web_server` and :mod:`cron.jobs` rather than reimplemented.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException

_log = logging.getLogger(__name__)

_SCHEMA = "hermes-cron-obs-v1"

# Whitelist of job fields safe to expose to the dashboard. Deliberately excludes
# prompt, script, base_url, workdir, context_from, enabled_toolsets — these are
# either secret-ish or operationally irrelevant for an at-a-glance view.
_ALLOWED_JOB_FIELDS = (
    "id",
    "name",
    "enabled",
    "state",
    "paused_at",
    "paused_reason",
    "schedule_display",
    "repeat",
    "next_run_at",
    "last_run_at",
    "last_status",
    "last_error",
    "last_delivery_error",
    "deliver",
    "skill",
    "model",
    "profile",
    "profile_name",
    "is_default_profile",
)


def redact_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Project a cron job onto the safe whitelist (no prompt/script leak)."""
    redacted: Dict[str, Any] = {k: job.get(k) for k in _ALLOWED_JOB_FIELDS}
    # Derived booleans so the UI can hint "has a script/prompt" without content.
    redacted["has_script"] = bool(job.get("script"))
    redacted["has_prompt"] = bool(job.get("prompt"))
    return redacted


def _latest_output_meta(call_cron, profile: Optional[str], job_id: str) -> Optional[Dict[str, Any]]:
    """Return {filename, mtime, size_bytes, run_count} for a job, or None."""
    try:
        files = call_cron(profile, "list_output_files", job_id)
    except Exception:
        _log.exception("cron observability: list_output_files failed for %s", job_id)
        return None
    if not files:
        return None
    newest = files[0]
    return {
        "filename": newest.get("filename"),
        "mtime": newest.get("mtime"),
        "size_bytes": newest.get("size_bytes"),
        "run_count": len(files),
    }


def _collect_observability() -> Dict[str, Any]:
    """Build the observability bundle. Never raises — failures degrade in place."""
    from hermes_cli.web_server import _call_cron_for_profile, _cron_profile_dicts

    gateway: Dict[str, Any] = {"running": False, "pids": []}
    try:
        from hermes_cli.gateway import find_gateway_pids

        pids = list(find_gateway_pids() or [])
        gateway = {"running": bool(pids), "pids": pids}
    except Exception as exc:  # pragma: no cover - defensive
        _log.exception("cron observability: gateway probe failed")
        gateway = {"running": False, "pids": [], "error": str(exc)}

    jobs: List[Dict[str, Any]] = []
    error: Optional[str] = None
    try:
        for item in _cron_profile_dicts():
            name = str(item.get("name") or "")
            if not name:
                continue
            try:
                raw_jobs = _call_cron_for_profile(name, "list_jobs", True)
            except Exception:
                _log.exception("cron observability: list_jobs failed for profile %s", name)
                continue
            for job in raw_jobs:
                redacted = redact_job(job)
                redacted["latest_output"] = _latest_output_meta(
                    _call_cron_for_profile, name, str(job.get("id") or "")
                )
                jobs.append(redacted)
    except Exception as exc:
        _log.exception("cron observability: bundle failed")
        error = str(exc)

    payload: Dict[str, Any] = {
        "schema": _SCHEMA,
        "checked_at": int(time.time()),
        "gateway": gateway,
        "jobs": jobs,
    }
    if error is not None:
        payload["error"] = error
    return payload


def register_cron_observability_routes(app: FastAPI) -> None:
    """Register the read-only cron observability endpoints before the SPA catch-all."""

    @app.get("/api/cron/observability")
    async def cron_observability() -> Dict[str, Any]:
        try:
            # _collect_observability iterates all profiles + per-job filesystem
            # listing under a lock (~1.3s for 33 jobs). Offload so it never
            # blocks the event loop / other dashboard requests.
            return await asyncio.to_thread(_collect_observability)
        except Exception as exc:  # pragma: no cover - belt and suspenders
            _log.exception("GET /api/cron/observability failed")
            return {
                "schema": _SCHEMA,
                "checked_at": int(time.time()),
                "gateway": {"running": False, "pids": []},
                "jobs": [],
                "error": str(exc),
            }

    @app.get("/api/cron/observability/output/{job_id}")
    async def cron_observability_output(
        job_id: str,
        profile: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        from hermes_cli.web_server import _call_cron_for_profile, _find_cron_job_profile

        selected = profile or await asyncio.to_thread(_find_cron_job_profile, job_id)
        if not selected:
            raise HTTPException(status_code=404, detail="Job not found")
        try:
            result = await asyncio.to_thread(_call_cron_for_profile, selected, "read_output_file", job_id, filename)
        except ValueError as exc:
            # Path-escape / bad filename → explicit client error, never a 5xx leak.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            _log.exception("GET /api/cron/observability/output failed for %s", job_id)
            raise HTTPException(status_code=502, detail="Failed to read cron output")
        if not result:
            return {
                "job_id": job_id,
                "filename": None,
                "text": None,
                "truncated": False,
                "mtime": None,
            }
        return {
            "job_id": job_id,
            "filename": result.get("filename"),
            "text": result.get("text"),
            "truncated": bool(result.get("truncated")),
            "mtime": result.get("mtime"),
        }
