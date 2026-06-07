#!/usr/bin/env python3
"""Read-only Vault provenance view for the live Hermes dashboard (9119).

Surfaces "wer arbeitet gerade / wer hat zuletzt was geliefert" from the shared
Vault — i.e. open coordination check-ins (with stale flag) + recent receipts.
It delegates to the canonical Vault helper ``activity-overview.py --json`` so the
dashboard and the CLI show the exact same truth (one source of logic).

Route (under ``/api/`` so the existing auth gate applies):

* ``GET /api/vault/provenance`` — ``{schema, error, stale_count, open_sessions[], recent_receipts[]}``

Read-only and defensive: any failure returns a structured ``error`` payload with
empty lists instead of raising, so the dashboard tile degrades gracefully.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI

_SCRIPT = Path("/home/piet/vault/_agents/_shared/scripts/activity-overview.py")
_SCHEMA = "hermes-vault-provenance-v1"
_TIMEOUT = 8


def _empty(error: str | None) -> dict[str, Any]:
    return {
        "schema": _SCHEMA,
        "error": error,
        "stale_count": 0,
        "open_sessions": [],
        "recent_receipts": [],
    }


def _collect_sync() -> dict[str, Any]:
    if not _SCRIPT.exists():
        return _empty(f"helper fehlt: {_SCRIPT}")
    try:
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT), "--json", "--receipts", "8"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return _empty(f"timeout >{_TIMEOUT}s")
    except Exception as exc:  # pragma: no cover - defensive
        return _empty(str(exc)[:200])

    if proc.returncode != 0:
        return _empty((proc.stderr or f"exit {proc.returncode}").strip()[:200])
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return _empty(f"bad json: {exc}")

    opens = data.get("open_sessions", []) or []
    return {
        "schema": _SCHEMA,
        "error": None,
        "stale_count": sum(1 for s in opens if s.get("stale")),
        "open_sessions": opens,
        "recent_receipts": data.get("recent_receipts", []) or [],
    }


async def _collect() -> dict[str, Any]:
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        return await loop.run_in_executor(executor, _collect_sync)
    finally:
        executor.shutdown(wait=True)


def register_vault_provenance_routes(app: FastAPI) -> None:
    """Register the read-only Vault provenance endpoint before the SPA catch-all."""

    @app.get("/api/vault/provenance")
    async def vault_provenance() -> dict[str, Any]:
        return await _collect()
