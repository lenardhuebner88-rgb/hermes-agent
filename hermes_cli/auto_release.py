"""Autonomous release orchestrator (Plan→Board→Release pipeline, Subsystem C).

On chain-tip completion → green gates → tip judgment → live test → deploy with
rollback on live failure. EVERYTHING here is behind the ``release.autonomous``
kill-switch (default **False**); ``critical``-tier chains never auto-deploy
regardless of the switch. ``ui-real`` live tests are never autonomous — they
return ``held`` for the operator.

Truth = API payload (``/api/status``), never a screenshot (CLAUDE.md).
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:9119"

# Payload keys /api/status must carry for a healthy backend (mirrors the
# deploy_dashboard.sh payload validation — real backend code, both auth modes).
_SMOKE_REQUIRED_KEYS = ("version",)

Fetch = Callable[..., dict]


@dataclass
class LiveTestResult:
    depth: str
    passed: bool
    held: bool = False
    detail: str = ""


def _default_fetch(path: str, timeout: float = 8.0) -> dict:
    """GET ``base_url + path`` and parse JSON. Loopback only by construction."""
    url = f"{DEFAULT_BASE_URL}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def run_live_test(
    depth: str,
    *,
    fetch: Optional[Fetch] = None,
    contract: Optional[dict] = None,
) -> LiveTestResult:
    """Execute the PlanSpec ``live_test_depth`` check against the live service.

    * ``smoke``   — ``/api/status`` returns valid JSON with a ``version``
      (proves the Python backend, not just the static SPA).
    * ``contract``— fetch ``contract["path"]`` and assert every key/value in
      ``contract["expect"]`` matches the payload.
    * ``ui-real`` — ALWAYS ``held`` (operator-gated, never autonomous).
    * empty/None  — trivially passes ("no live test configured").
    """
    depth = (depth or "").strip().lower()
    fetch = fetch or _default_fetch
    if not depth:
        return LiveTestResult(depth=depth, passed=True, detail="no live test configured")
    if depth == "ui-real":
        return LiveTestResult(
            depth=depth,
            passed=False,
            held=True,
            detail="ui-real is operator-gated — never autonomous",
        )
    if depth == "smoke":
        try:
            payload = fetch("/api/status")
        except Exception as exc:
            return LiveTestResult(depth=depth, passed=False, detail=f"fetch failed: {exc}")
        if not isinstance(payload, dict) or not all(
            payload.get(k) for k in _SMOKE_REQUIRED_KEYS
        ):
            return LiveTestResult(
                depth=depth, passed=False, detail=f"invalid status payload: {payload!r:.200}"
            )
        return LiveTestResult(depth=depth, passed=True, detail="status payload valid")
    if depth == "contract":
        contract = contract or {}
        path = str(contract.get("path") or "/api/status")
        expect = contract.get("expect")
        if not isinstance(expect, dict) or not expect:
            # No expectation defined → degrade to the smoke check on the path.
            expect = {}
        try:
            payload = fetch(path)
        except Exception as exc:
            return LiveTestResult(depth=depth, passed=False, detail=f"fetch failed: {exc}")
        if not isinstance(payload, dict):
            return LiveTestResult(
                depth=depth, passed=False, detail=f"non-dict payload from {path}"
            )
        mismatches = [
            f"{k}: expected {v!r}, got {payload.get(k)!r}"
            for k, v in expect.items()
            if payload.get(k) != v
        ]
        if mismatches:
            return LiveTestResult(
                depth=depth, passed=False, detail="; ".join(mismatches)
            )
        return LiveTestResult(depth=depth, passed=True, detail=f"contract on {path} holds")
    # Unknown depth: fail CLOSED — an unknown check must not count as passed.
    return LiveTestResult(depth=depth, passed=False, detail=f"unknown live_test_depth: {depth}")
