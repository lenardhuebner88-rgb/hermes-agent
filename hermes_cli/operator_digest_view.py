#!/usr/bin/env python3
"""Read-only Operator-Digest view for the live Hermes dashboard (9119).

Surfaces ONLY what the operator must decide — open decisions from the
registry (``~/.hermes/state/open-decisions.json``) plus real alerts (failed
systemd user units, a red Nacht-Gate run). Nothing an agent could resolve on
its own. An empty digest means an empty payload — the frontend renders
nothing (calm = empty).

Route (under ``/api/`` so the existing auth gate applies):

* ``GET /api/operator/digest`` —
  ``{generated_at, decisions[], alerts[], degraded[]}``

Read-only and defensive: each probe is isolated in its own try/except; a
failing probe adds its source name to ``degraded`` instead of raising, so the
dashboard tile degrades gracefully rather than crashing.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI

_DECISIONS_PATH = Path("/home/piet/.hermes/state/open-decisions.json")
_GREEN_GATE_ROOT = Path("/home/piet/.hermes/logs/green-gate")
_PROBE_TIMEOUT = 5
_NIGHTGATE_STALE_HOURS = 26


def _age_days(opened_at: str, *, now: datetime) -> int:
    parsed = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, (now - parsed).days)


def _read_decisions() -> tuple[list[dict[str, Any]], list[str]]:
    """Open decisions from the registry, newest-first age. Corrupt/missing → degraded."""
    try:
        if not _DECISIONS_PATH.exists() or _DECISIONS_PATH.stat().st_size == 0:
            return [], ["open-decisions"]
        data = json.loads(_DECISIONS_PATH.read_text(encoding="utf-8"))
        entries = data.get("decisions") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return [], ["open-decisions"]
    except (OSError, ValueError):
        return [], ["open-decisions"]

    now = datetime.now(timezone.utc)
    decisions: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "open":
            continue
        opened_at = entry.get("opened_at")
        try:
            age = _age_days(str(opened_at), now=now) if opened_at else 0
        except ValueError:
            age = 0
        decisions.append({
            "id": entry.get("id"),
            "title": entry.get("title"),
            "action": entry.get("action"),
            "source": entry.get("source"),
            "opened_at": opened_at,
            "age_days": age,
        })
    decisions.sort(key=lambda d: d["age_days"], reverse=True)
    return decisions, []


def _systemd_failed_alerts() -> tuple[list[dict[str, Any]], list[str]]:
    """One red alert per failed systemd --user unit."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "--failed", "--no-legend", "--plain"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return [], ["systemd"]

    alerts: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        unit = line.strip().split()[0] if line.strip() else ""
        if not unit:
            continue
        alerts.append({
            "id": f"systemd-{unit}",
            "severity": "red",
            "title": unit,
            "detail": "systemd user unit failed",
        })
    return alerts, []


def _nightgate_alert() -> tuple[list[dict[str, Any]], list[str]]:
    """Red Nacht-Gate detection, ported from operator-morning-digest.py::section_nightgate.

    Green/unknown (no run dir, stale run, no triggered gate) → no alert.
    """
    try:
        if not _GREEN_GATE_ROOT.exists():
            return [], []
        run_dirs = sorted((d for d in _GREEN_GATE_ROOT.iterdir() if d.is_dir()), key=lambda d: d.name)
        if not run_dirs:
            return [], []
        latest = run_dirs[-1]

        try:
            run_dt = datetime.strptime(latest.name, "%Y%m%d-%H%M%S")
            age_h = (datetime.now() - run_dt).total_seconds() / 3600
        except ValueError:
            age_h = None
        if age_h is not None and age_h > _NIGHTGATE_STALE_HOURS:
            return [], []

        triggered_gates: list[str] = []
        log_path = latest / "autoheal.log"
        if log_path.exists() and log_path.stat().st_size > 0:
            for line in log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("triggered") is not True:
                    continue
                gate = str(obj.get("gate", "?"))
                if gate not in triggered_gates:
                    triggered_gates.append(gate)

        if not triggered_gates:
            return [], []
        return [{
            "id": "nightgate",
            "severity": "red",
            "title": "Nacht-Gate rot",
            "detail": ", ".join(triggered_gates),
        }], []
    except (OSError, ValueError):
        return [], ["nightgate"]


def register_operator_digest_routes(app: FastAPI) -> None:
    """Register the read-only Operator-Digest endpoint before the SPA catch-all."""

    @app.get("/api/operator/digest")
    def operator_digest() -> dict[str, Any]:
        # Bewusst sync (kein async): die Probes blocken (subprocess bis 5s) —
        # als sync-Handler läuft das im FastAPI-Threadpool statt im Event-Loop.
        decisions, decisions_degraded = _read_decisions()
        systemd_alerts, systemd_degraded = _systemd_failed_alerts()
        nightgate_alerts, nightgate_degraded = _nightgate_alert()
        return {
            "generated_at": int(time.time()),
            "decisions": decisions,
            "alerts": nightgate_alerts + systemd_alerts,
            "degraded": decisions_degraded + systemd_degraded + nightgate_degraded,
        }
