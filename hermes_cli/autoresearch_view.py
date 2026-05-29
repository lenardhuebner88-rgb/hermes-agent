#!/usr/bin/env python3
"""Read-only Autoresearch view for the live Hermes dashboard (9119).

Phase 4 of the autoresearch-dashboard plan: add a ``/autoresearch`` surface to
the existing FastAPI dashboard that answers "läuft ein Loop gerade — ja/nein?"
plus the audit history, with **no mutation**.

Routes registered by :func:`register_autoresearch_routes`:

* ``GET  /autoresearch``         — self-contained HTML view (polls the JSON below)
* ``GET  /autoresearch/status``  — live loop state from lock + heartbeat (OPEN)
* ``GET  /autoresearch/audit``   — inventory counts + results history (OPEN)
* ``POST /autoresearch/trigger`` — TOKEN-GATED; Phase 4 has no runner → 503
* ``POST /autoresearch/stop``    — TOKEN-GATED; Phase 4 has no runner → 503

The GET routes are intentionally open: they are read-only and safe over the
tailnet (the dashboard is fronted by Tailscale Serve). The POST routes require a
local token (``HERMES_AUTORESEARCH_TOKEN``, injected at dashboard start, never
written to vault/git/logs/HTML). Without a valid token they return **403**. In
Phase 4 there is deliberately no runner, so even a valid token yields **503**
(the applying runner + Trigger/Stop wiring is Phase 5).

The runner-state contract (written by the Phase 5 runner, or by the tiny
``--dry-run`` heartbeat stub for UI testing) lives under::

    <audit>/runner-state/
        current.lock        {pid, request_id, started_at}    presence == a loop intends to run
        current.heartbeat   {pid, request_id, iteration, max, last_step, last_eval, ts}
        current.status      {state, route_status, last_receipt, updated_at}
"""
from __future__ import annotations

import csv
import hmac
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Imported at module top level (not inside register_autoresearch_routes) so that
# with ``from __future__ import annotations`` FastAPI's get_type_hints can
# resolve handler annotations like ``request: Request`` against module globals.
# This module is only imported by the dashboard, where fastapi is guaranteed.
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

# hermes-agent repo root (this file lives in hermes_cli/).
_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_AUDIT = _REPO / ".hermes" / "skill-audit"

# Heartbeat older than this (seconds) means the loop is no longer alive →
# a present lock with a stale heartbeat is reported as "crashed".
_DEFAULT_HEARTBEAT_TTL = 30.0

_TOKEN_HEADER = "X-Autoresearch-Token"
_DATA_SCRIPT_RE = re.compile(
    r'<script type="application/json" id="data-autoresearch">(.+?)</script>',
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Path resolution (env-overridable so tests can point at a tmp dir)
# ---------------------------------------------------------------------------
def _audit_dir() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_AUDIT_DIR")
    return Path(override) if override else _DEFAULT_AUDIT


def _state_dir() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_STATE_DIR")
    return Path(override) if override else (_audit_dir() / "runner-state")


def _heartbeat_ttl() -> float:
    try:
        return float(os.environ.get("HERMES_AUTORESEARCH_HEARTBEAT_TTL", _DEFAULT_HEARTBEAT_TTL))
    except (TypeError, ValueError):
        return _DEFAULT_HEARTBEAT_TTL


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _age_seconds(ts: Any, *, now: float | None = None) -> float | None:
    """Seconds elapsed since heartbeat ``ts`` (epoch float/int or ISO8601)."""
    if ts is None:
        return None
    now = time.time() if now is None else now
    # epoch seconds
    if isinstance(ts, (int, float)):
        return max(0.0, now - float(ts))
    if isinstance(ts, str):
        s = ts.strip()
        try:
            return max(0.0, now - float(s))
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, now - parsed.timestamp())
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Status (the "läuft / läuft nicht" bridge)
# ---------------------------------------------------------------------------
def read_runner_status(*, now: float | None = None) -> dict[str, Any]:
    """Derive live loop status from lock + heartbeat + status files.

    State machine:
      no lock file                              → idle
      lock + fresh heartbeat + status=stopping  → stopping
      lock + fresh heartbeat                    → running
      lock + stale / missing heartbeat          → crashed
    """
    state_dir = _state_dir()
    lock = _read_json(state_dir / "current.lock")
    heartbeat = _read_json(state_dir / "current.heartbeat")
    status_file = _read_json(state_dir / "current.status") or {}

    # route_status: from status file when written; the request generator default
    # is "configured" (MiniMax-M2.7 is configured in config.yaml).
    route_status = status_file.get("route_status") or "configured"

    base: dict[str, Any] = {
        "schema": "autoresearch-runner-status-v1",
        "state": "idle",
        "pid": None,
        "request_id": None,
        "iteration": None,
        "max": None,
        "last_step": None,
        "last_eval": None,
        "route_status": route_status,
        "heartbeat_age_s": None,
        "heartbeat_fresh": False,
        "last_receipt": status_file.get("last_receipt"),
        "state_dir": str(state_dir),
    }

    if not lock:
        return base

    ttl = _heartbeat_ttl()
    age = _age_seconds((heartbeat or {}).get("ts"), now=now)
    fresh = age is not None and age < ttl

    base.update(
        {
            "pid": lock.get("pid"),
            "request_id": lock.get("request_id"),
            "iteration": (heartbeat or {}).get("iteration"),
            "max": (heartbeat or {}).get("max"),
            "last_step": (heartbeat or {}).get("last_step"),
            "last_eval": (heartbeat or {}).get("last_eval"),
            "heartbeat_age_s": round(age, 1) if age is not None else None,
            "heartbeat_fresh": fresh,
        }
    )

    declared = str(status_file.get("state") or "").lower()
    if not fresh:
        base["state"] = "crashed"
    elif declared == "stopping":
        base["state"] = "stopping"
    else:
        base["state"] = "running"
    return base


# ---------------------------------------------------------------------------
# Audit (inventory counts + results history)
# ---------------------------------------------------------------------------
def _parse_results(audit_dir: Path) -> list[dict[str, str]]:
    path = audit_dir / "autoresearch_results.tsv"
    try:
        if not path.exists() or path.stat().st_size == 0:
            return []
        with path.open("r", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh, delimiter="\t"))
    except (OSError, ValueError):
        return []


def _embedded_inventory(audit_dir: Path) -> dict[str, Any] | None:
    """Pull the precomputed inventory/counts JSON from the rendered dashboard.html.

    The standalone renderer (Phase 1-3) embeds an
    ``autoresearch-dashboard-data-v1`` blob with priority/area/weakness counts.
    Reusing it avoids re-parsing the rubric here. Returns None if absent.
    """
    path = audit_dir / "dashboard.html"
    try:
        if not path.exists():
            return None
        match = _DATA_SCRIPT_RE.search(path.read_text(encoding="utf-8", errors="replace"))
        if not match:
            return None
        data = json.loads(match.group(1))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def read_audit() -> dict[str, Any]:
    audit_dir = _audit_dir()
    results = _parse_results(audit_dir)
    decision_counts: dict[str, int] = {}
    for row in results:
        decision = (row.get("decision") or "unknown").strip().lower() or "unknown"
        decision_counts[decision] = decision_counts.get(decision, 0) + 1

    embedded = _embedded_inventory(audit_dir)
    inventory: dict[str, Any] | None = None
    if embedded:
        inventory = {
            "priority_counts": embedded.get("priority_counts"),
            "area_counts": embedded.get("area_counts"),
            "weakness_counts": embedded.get("weakness_counts"),
            "model_preference": embedded.get("model_preference"),
            "model_route_status": embedded.get("model_route_status"),
            "inventory_summary": embedded.get("inventory_summary"),
            "generated_at": embedded.get("generated_at"),
        }

    return {
        "schema": "autoresearch-audit-v1",
        "audit_dir": str(audit_dir),
        "results_count": len(results),
        "decision_counts": decision_counts,
        "results": results,
        "inventory": inventory,
    }


# ---------------------------------------------------------------------------
# Token gate for the mutate routes (Phase 4: deny without token; 503 with)
# ---------------------------------------------------------------------------
def _read_token(request: Any) -> str:
    header = request.headers.get(_TOKEN_HEADER, "") or ""
    if header:
        return header
    auth = request.headers.get("authorization", "") or ""
    prefix = "Bearer "
    return auth[len(prefix):] if auth.startswith(prefix) else ""


def _token_ok(request: Any) -> bool:
    expected = os.environ.get("HERMES_AUTORESEARCH_TOKEN", "") or ""
    if not expected:
        # No token configured → the mutate capability is not enabled at all.
        return False
    provided = _read_token(request)
    if not provided:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


# ---------------------------------------------------------------------------
# Self-contained HTML view (polls the JSON routes; no mutation forms)
# ---------------------------------------------------------------------------
_HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Autoresearch — Live Loop</title>
<style>
:root { color-scheme: light; --ink:#162016; --muted:#5f6f63; --line:#d8ded3; --panel:#f6f8f2; --accent:#126b54; --warn:#a9501a; --bad:#9b2635; --bg:#fbfcf8; }
* { box-sizing:border-box; }
body { margin:0; font-family:Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; background:var(--bg); color:var(--ink); }
header { padding:22px 26px 14px; border-bottom:1px solid var(--line); background:#fff; }
h1 { margin:0 0 6px; font-size:24px; }
header p { margin:0; color:var(--muted); max-width:960px; }
main { padding:20px 26px 36px; display:grid; gap:18px; }
.panel { border:1px solid var(--line); background:#fff; border-radius:8px; padding:16px; }
h2 { margin:0 0 12px; font-size:17px; }
.pill { display:inline-block; padding:4px 12px; border-radius:999px; font-weight:700; font-size:14px; }
.pill-idle { background:#eef1ec; color:var(--muted); }
.pill-running { background:#e3f4ec; color:var(--accent); }
.pill-stopping { background:#fff1dc; color:var(--warn); }
.pill-crashed { background:#fbe3e6; color:var(--bad); }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; }
.kv { border:1px solid var(--line); border-radius:8px; padding:10px 12px; background:var(--panel); }
.kv span { display:block; color:var(--muted); font-size:12px; margin-bottom:4px; }
.kv strong { font-size:16px; }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; background:#e8f1ec; color:var(--accent); }
.badge-yellow { background:#fff1dc; color:var(--warn); }
.badge-bad { background:#fbe3e6; color:var(--bad); }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; border-bottom:1px solid var(--line); padding:8px; vertical-align:top; }
th { color:var(--muted); background:var(--panel); }
.banner { border:1px solid #efcf9d; background:#fff8e9; color:#5d3b00; border-radius:8px; padding:12px 14px; }
.muted { color:var(--muted); }
</style>
</head>
<body data-autoresearch="live-v1">
<header>
  <h1>Hermes Autoresearch — Live Loop</h1>
  <p>Read-only status of the bounded skill-research runner. No mutation here:
     Trigger/Stop are token-gated and the applying runner is a separate Go (Phase 5).</p>
</header>
<main>
  <section class="banner"><b>Safety:</b> read-only view. No secrets, no provider routing change,
     no skill mutation, no push/merge. Trigger/Stop require a local token (403 without it).</section>
  <section class="panel">
    <h2>Loop status <span id="pill" class="pill pill-idle">…</span></h2>
    <div class="grid">
      <div class="kv"><span>Request</span><strong id="req">—</strong></div>
      <div class="kv"><span>Iteration</span><strong id="iter">—</strong></div>
      <div class="kv"><span>Last step</span><strong id="step">—</strong></div>
      <div class="kv"><span>Last eval</span><strong id="eval">—</strong></div>
      <div class="kv"><span>Model route</span><strong id="route">—</strong></div>
      <div class="kv"><span>Heartbeat age</span><strong id="age">—</strong></div>
    </div>
    <p class="muted" id="updated" style="margin-top:10px;">polling…</p>
  </section>
  <section class="panel">
    <h2>Audit summary</h2>
    <div class="grid" id="auditCards"></div>
  </section>
  <section class="panel">
    <h2>Recent results</h2>
    <div id="resultsTable"><p class="muted">loading…</p></div>
  </section>
</main>
<script>
const BASE = (window.__HERMES_BASE_PATH__ || "");
function el(id){ return document.getElementById(id); }
function esc(s){ const d=document.createElement('div'); d.textContent = (s==null?'':String(s)); return d.innerHTML; }
function setRoute(node, status){
  const s = (status||'unknown');
  node.innerHTML = '<span class="badge'+(s==='yellow'?' badge-yellow':(s==='unavailable'?' badge-bad':''))+'">'+esc(s)+'</span>';
}
async function poll(){
  try {
    const r = await fetch(BASE + '/autoresearch/status', {headers:{'Accept':'application/json'}});
    const d = await r.json();
    const pill = el('pill');
    pill.className = 'pill pill-' + (d.state||'idle');
    pill.textContent = (d.state||'idle');
    el('req').textContent = d.request_id || '—';
    el('iter').textContent = (d.iteration!=null && d.max!=null) ? (d.iteration + ' / ' + d.max) : '—';
    el('step').textContent = d.last_step || '—';
    el('eval').textContent = d.last_eval || '—';
    setRoute(el('route'), d.route_status);
    el('age').textContent = (d.heartbeat_age_s!=null) ? (d.heartbeat_age_s + 's' + (d.heartbeat_fresh?'':' (stale)')) : '—';
    el('updated').textContent = 'updated ' + new Date().toLocaleTimeString();
  } catch (e) {
    el('updated').textContent = 'status fetch failed: ' + e;
  }
}
async function loadAudit(){
  try {
    const r = await fetch(BASE + '/autoresearch/audit', {headers:{'Accept':'application/json'}});
    const d = await r.json();
    const cards = [];
    const inv = d.inventory || {};
    const pc = inv.priority_counts || {};
    cards.push(['Iterations logged', d.results_count]);
    cards.push(['Kept', (d.decision_counts||{}).keep || 0]);
    cards.push(['Blocked', (d.decision_counts||{}).blocked || 0]);
    cards.push(['Discarded', (d.decision_counts||{}).discard || 0]);
    if (pc.high!=null) cards.push(['High-priority skills', pc.high]);
    if (inv.model_route_status) cards.push(['Model route', inv.model_route_status]);
    el('auditCards').innerHTML = cards.map(c =>
      '<div class="kv"><span>'+esc(c[0])+'</span><strong>'+esc(c[1])+'</strong></div>').join('');
    const rows = (d.results||[]).slice(-15).reverse();
    if (!rows.length){ el('resultsTable').innerHTML = '<p class="muted">No iterations logged yet.</p>'; return; }
    const cols = ['timestamp','mode','target','hypothesis','decision','risk'];
    let html = '<table><thead><tr>'+cols.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr></thead><tbody>';
    for (const row of rows){ html += '<tr>'+cols.map(c=>'<td>'+esc(row[c])+'</td>').join('')+'</tr>'; }
    el('resultsTable').innerHTML = html + '</tbody></table>';
  } catch (e) {
    el('resultsTable').innerHTML = '<p class="muted">audit fetch failed: '+esc(e)+'</p>';
  }
}
poll(); loadAudit();
setInterval(poll, 4000);
setInterval(loadAudit, 30000);
</script>
</body>
</html>
"""


def render_autoresearch_html() -> str:
    return _HTML_PAGE


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------
def register_autoresearch_routes(app: Any) -> None:
    """Register the read-only /autoresearch view + token-gated POST stubs.

    Must be called before the SPA catch-all (``/{full_path:path}``) is mounted
    so these explicit paths take precedence.
    """

    @app.get("/autoresearch", include_in_schema=False)
    @app.get("/autoresearch/", include_in_schema=False)
    async def autoresearch_view() -> HTMLResponse:
        return HTMLResponse(
            render_autoresearch_html(),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/autoresearch/status")
    async def autoresearch_status() -> dict[str, Any]:
        return read_runner_status()

    @app.get("/autoresearch/audit")
    async def autoresearch_audit() -> dict[str, Any]:
        return read_audit()

    @app.post("/autoresearch/trigger")
    async def autoresearch_trigger(request: Request) -> dict[str, Any]:
        # Token gate FIRST (no body parsing, so a missing body can't pre-empt
        # the 403 with a 422). Phase 4 has no runner → 503 even with a token.
        if not _token_ok(request):
            raise HTTPException(status_code=403, detail="Forbidden: local autoresearch token required")
        raise HTTPException(
            status_code=503,
            detail="Autoresearch runner is not enabled in this build (Phase 5).",
        )

    @app.post("/autoresearch/stop")
    async def autoresearch_stop(request: Request) -> dict[str, Any]:
        if not _token_ok(request):
            raise HTTPException(status_code=403, detail="Forbidden: local autoresearch token required")
        raise HTTPException(
            status_code=503,
            detail="Autoresearch runner is not enabled in this build (Phase 5).",
        )
